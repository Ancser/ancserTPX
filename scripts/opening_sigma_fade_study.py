"""Opening-session sigma fade / acceptance-switch research.

Research-only script. It does not touch live trading or the main backtest path.

Idea under test:
  1) Take the first N minutes of a session.
  2) Build a distribution center/sigma from that opening window.
  3) Place fade limits at +/- k sigma, targeting either one sigma inward,
     halfway to center, or center.
  4) If price is accepted outside the band, either stop fading that side or
     switch to a trend-pullback trade.

Run:
  PYTHONIOENCODING=utf-8 python -m scripts.opening_sigma_fade_study
"""
from __future__ import annotations

import csv
import json
import math
from collections import OrderedDict
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from backend.backtest.engine import _topstep_trade_date
from backend.backtest.intrabar import resolve_same_bar_exit
from backend.backtest.metrics import MetricsCalculator
from backend.data import candle_store
from backend.db.models import (
    Candle,
    Direction,
    ExitReason,
    StrategyType,
    Trade,
    current_quarterly_contract_id,
    get_commission_rt,
    get_fees_rt,
    get_point_value,
)


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "machinelearning"
OUT_TXT = OUT_DIR / "opening_sigma_fade_study.txt"
OUT_JSON = OUT_DIR / "opening_sigma_fade_study.json"
OUT_TRADES = OUT_DIR / "opening_sigma_fade_study_best_trades.csv"

INITIAL_CAPITAL = 50_000.0
TICK = 0.25
CONTRACT_ID = current_quarterly_contract_id("MNQ")
POINT_VALUE = get_point_value(CONTRACT_ID)
COMMISSION_RT = get_commission_rt(CONTRACT_ID)
FEES_RT = get_fees_rt(CONTRACT_ID)
ROUND_TURN_COST = COMMISSION_RT + FEES_RT

SESSION_SETS = {
    "ASIA": {"ASIA"},
    "RTH": {"RTH"},
    "ASIA_RTH": {"ASIA", "RTH"},
    "ALL": {"ASIA", "EURO", "PRE", "RTH", "AH"},
}

OPENING_MINUTES = (15, 30)
SIGMA_METHODS = ("std", "mad")
ENTRY_MODES = ("blind", "reject")
ACCEPT_MODES = ("none", "filter", "switch")
START_SIGMAS = (1.0, 1.5, 2.0)
MAX_SIGMAS = (3.0, 4.0)
TARGET_MODES = ("inner1", "half", "center")
STOP_SPANS = (1.0, 1.5)
SESSION_LOSS_STOPS = (0, 1)

ACCEPT_SIGMA = 2.0
ACCEPT_BARS = 2
MIN_SIGMA_POINTS = 1.0
MIN_OPENING_BARS = 8


@dataclass(frozen=True)
class Variant:
    session_set: str
    opening_minutes: int
    sigma_method: str
    entry_mode: str
    accept_mode: str
    start_sigma: float
    max_sigma: float
    target_mode: str
    stop_span: float
    session_loss_stop: int

    @property
    def name(self) -> str:
        return (
            f"{self.session_set}|open{self.opening_minutes}|{self.sigma_method}|"
            f"{self.entry_mode}|{self.accept_mode}|"
            f"L{self.start_sigma:g}-{self.max_sigma:g}|"
            f"tp={self.target_mode}|sl={self.stop_span:g}|"
            f"sLoss={self.session_loss_stop}"
        )


@dataclass
class SessionInfo:
    code: str
    start: datetime
    candles: list[Candle]


@dataclass
class Position:
    trade: Trade
    entry_bar_ts: datetime


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _round_tick(price: float) -> float:
    return round(float(price) / TICK) * TICK


def _session_for(ts: datetime) -> Optional[tuple[str, datetime]]:
    ts = _utc(ts)
    d = ts.date()
    tod = ts.time()
    if tod >= time(22, 0) or tod < time(7, 0):
        start_day = d if tod >= time(22, 0) else d - timedelta(days=1)
        return "ASIA", datetime.combine(start_day, time(22, 0), tzinfo=timezone.utc)
    if time(7, 0) <= tod < time(11, 0):
        return "EURO", datetime.combine(d, time(7, 0), tzinfo=timezone.utc)
    if time(11, 0) <= tod < time(13, 30):
        return "PRE", datetime.combine(d, time(11, 0), tzinfo=timezone.utc)
    if time(13, 30) <= tod < time(20, 0):
        return "RTH", datetime.combine(d, time(13, 30), tzinfo=timezone.utc)
    if time(20, 0) <= tod < time(22, 0):
        return "AH", datetime.combine(d, time(20, 0), tzinfo=timezone.utc)
    return None


def _build_sessions(candles: list[Candle]) -> list[SessionInfo]:
    groups: "OrderedDict[tuple[str, datetime], list[Candle]]" = OrderedDict()
    for candle in sorted(candles, key=lambda c: c.timestamp):
        sess = _session_for(candle.timestamp)
        if sess is None:
            continue
        groups.setdefault(sess, []).append(candle)
    sessions = [
        SessionInfo(code=code, start=start, candles=bars)
        for (code, start), bars in groups.items()
        if len(bars) >= MIN_OPENING_BARS + 1
    ]
    sessions.sort(key=lambda s: s.start)
    return sessions


def _weighted_median(values: list[float], weights: list[float]) -> float:
    pairs = sorted(zip(values, weights), key=lambda x: x[0])
    total = sum(w for _, w in pairs)
    if total <= 0:
        return pairs[len(pairs) // 2][0]
    halfway = total / 2.0
    acc = 0.0
    for value, weight in pairs:
        acc += weight
        if acc >= halfway:
            return value
    return pairs[-1][0]


def _opening_distribution(candles: list[Candle], method: str) -> Optional[tuple[float, float]]:
    if len(candles) < MIN_OPENING_BARS:
        return None
    prices = [(c.high + c.low + c.close) / 3.0 for c in candles]
    weights = [max(float(c.volume or 0), 1.0) for c in candles]
    total_w = sum(weights)
    if total_w <= 0:
        return None
    if method == "mad":
        center = _weighted_median(prices, weights)
        deviations = [abs(p - center) for p in prices]
        sigma = 1.4826 * _weighted_median(deviations, weights)
    else:
        center = sum(p * w for p, w in zip(prices, weights)) / total_w
        var = sum(w * (p - center) ** 2 for p, w in zip(prices, weights)) / total_w
        sigma = math.sqrt(max(var, 0.0))

    opening_range = max(c.high for c in candles) - min(c.low for c in candles)
    if not math.isfinite(center) or not math.isfinite(sigma):
        return None
    if sigma < MIN_SIGMA_POINTS:
        sigma = max(opening_range / 4.0, sigma)
    if sigma < MIN_SIGMA_POINTS:
        return None
    return _round_tick(center), max(_round_tick(sigma), TICK)


def _sigma_levels(start: float, max_sigma: float) -> list[float]:
    levels: list[float] = []
    cur = float(start)
    while cur <= max_sigma + 1e-9:
        levels.append(round(cur, 2))
        cur += 1.0
    return levels


def _target_level(level: float, mode: str) -> float:
    if mode == "center":
        return 0.0
    if mode == "half":
        return level / 2.0
    return max(0.0, level - 1.0)


def _new_trade(
    trade_id: str,
    direction: Direction,
    entry: float,
    sl: float,
    tp: float,
    entry_time: datetime,
    meta: dict,
) -> Trade:
    return Trade(
        trade_id=trade_id,
        strategy=StrategyType.TREND_FOLLOW,
        direction=direction,
        entry_price=_round_tick(entry),
        entry_time=entry_time,
        sl_price=_round_tick(sl),
        tp_price=_round_tick(tp),
        original_sl_price=_round_tick(sl),
        original_tp_price=_round_tick(tp),
        zone_id=str(meta.get("session_start", "")),
        zone_source="opening_sigma",
        contracts=1,
        point_value=POINT_VALUE,
        contract_id=CONTRACT_ID,
        meta=meta,
    )


def _close_trade(trade: Trade, exit_price: float, exit_time: datetime, reason: ExitReason) -> None:
    exit_price = _round_tick(exit_price)
    if trade.direction == Direction.BUY:
        gross = (exit_price - trade.entry_price) * POINT_VALUE * trade.contracts
    else:
        gross = (trade.entry_price - exit_price) * POINT_VALUE * trade.contracts
    commission = COMMISSION_RT * trade.contracts
    fees = FEES_RT * trade.contracts
    trade.exit_price = exit_price
    trade.exit_time = exit_time
    trade.commission = commission
    trade.fees = fees
    trade.pnl = gross - commission - fees
    trade.exit_reason = reason


def _check_exit(pos: Position, candle: Candle) -> bool:
    t = pos.trade
    entry_bar = _utc(candle.timestamp) == _utc(pos.entry_bar_ts)

    if t.direction == Direction.BUY:
        hit_sl = candle.low <= t.sl_price
        hit_tp = candle.high >= t.tp_price
        if entry_bar:
            if hit_sl:
                _close_trade(t, t.sl_price, candle.timestamp, ExitReason.SL)
                return True
            return False
        if hit_sl and hit_tp:
            first = resolve_same_bar_exit(candle.open, t.sl_price, t.tp_price)
            if first == "sl":
                _close_trade(t, t.sl_price, candle.timestamp, ExitReason.SL)
            else:
                _close_trade(t, t.tp_price, candle.timestamp, ExitReason.TP)
            return True
        if hit_sl:
            _close_trade(t, t.sl_price, candle.timestamp, ExitReason.SL)
            return True
        if hit_tp:
            _close_trade(t, t.tp_price, candle.timestamp, ExitReason.TP)
            return True
        return False

    hit_sl = candle.high >= t.sl_price
    hit_tp = candle.low <= t.tp_price
    if entry_bar:
        if hit_sl:
            _close_trade(t, t.sl_price, candle.timestamp, ExitReason.SL)
            return True
        return False
    if hit_sl and hit_tp:
        first = resolve_same_bar_exit(candle.open, t.sl_price, t.tp_price)
        if first == "sl":
            _close_trade(t, t.sl_price, candle.timestamp, ExitReason.SL)
        else:
            _close_trade(t, t.tp_price, candle.timestamp, ExitReason.TP)
        return True
    if hit_sl:
        _close_trade(t, t.sl_price, candle.timestamp, ExitReason.SL)
        return True
    if hit_tp:
        _close_trade(t, t.tp_price, candle.timestamp, ExitReason.TP)
        return True
    return False


def _fade_candidate(
    candle: Candle,
    center: float,
    sigma: float,
    levels: list[float],
    variant: Variant,
    used: set[tuple[str, float]],
    accepted_up: bool,
    accepted_down: bool,
) -> Optional[tuple[Direction, float, float, float, float]]:
    disable_short = variant.accept_mode in {"filter", "switch"} and accepted_up
    disable_long = variant.accept_mode in {"filter", "switch"} and accepted_down

    short_pick = None
    if not disable_short:
        for level in levels:
            if ("short", level) in used:
                continue
            entry = _round_tick(center + level * sigma)
            touched = candle.high >= entry
            rejected = candle.close <= entry
            if touched and (variant.entry_mode == "blind" or rejected):
                target_l = _target_level(level, variant.target_mode)
                tp = _round_tick(center + target_l * sigma)
                sl = _round_tick(entry + variant.stop_span * sigma)
                if sl > entry and tp < entry:
                    short_pick = (Direction.SELL, entry, sl, tp, level)
                break

    long_pick = None
    if not disable_long:
        for level in levels:
            if ("long", level) in used:
                continue
            entry = _round_tick(center - level * sigma)
            touched = candle.low <= entry
            rejected = candle.close >= entry
            if touched and (variant.entry_mode == "blind" or rejected):
                target_l = _target_level(level, variant.target_mode)
                tp = _round_tick(center - target_l * sigma)
                sl = _round_tick(entry - variant.stop_span * sigma)
                if sl < entry and tp > entry:
                    long_pick = (Direction.BUY, entry, sl, tp, level)
                break

    if short_pick and long_pick:
        return None
    return short_pick or long_pick


def _trend_candidate(
    candle: Candle,
    center: float,
    sigma: float,
    variant: Variant,
    accepted_up: bool,
    accepted_down: bool,
    trend_used: set[str],
) -> Optional[tuple[Direction, float, float, float, float]]:
    if variant.accept_mode != "switch":
        return None
    up_entry = _round_tick(center + ACCEPT_SIGMA * sigma)
    dn_entry = _round_tick(center - ACCEPT_SIGMA * sigma)
    buy_pick = None
    sell_pick = None
    if accepted_up and "up" not in trend_used and candle.low <= up_entry <= candle.high:
        sl = _round_tick(up_entry - variant.stop_span * sigma)
        tp = _round_tick(up_entry + sigma)
        if sl < up_entry < tp:
            buy_pick = (Direction.BUY, up_entry, sl, tp, ACCEPT_SIGMA)
    if accepted_down and "down" not in trend_used and candle.low <= dn_entry <= candle.high:
        sl = _round_tick(dn_entry + variant.stop_span * sigma)
        tp = _round_tick(dn_entry - sigma)
        if tp < dn_entry < sl:
            sell_pick = (Direction.SELL, dn_entry, sl, tp, ACCEPT_SIGMA)
    if buy_pick and sell_pick:
        return None
    return buy_pick or sell_pick


def _simulate_session(session: SessionInfo, variant: Variant, trade_counter: int) -> tuple[list[Trade], int]:
    window_end = session.start + timedelta(minutes=variant.opening_minutes)
    opening = [c for c in session.candles if _utc(c.timestamp) < window_end]
    active = [c for c in session.candles if _utc(c.timestamp) >= window_end]
    dist = _opening_distribution(opening, variant.sigma_method)
    if dist is None or not active:
        return [], trade_counter

    center, sigma = dist
    levels = _sigma_levels(variant.start_sigma, variant.max_sigma)
    accepted_up = False
    accepted_down = False
    up_count = 0
    down_count = 0
    upper_accept = _round_tick(center + ACCEPT_SIGMA * sigma)
    lower_accept = _round_tick(center - ACCEPT_SIGMA * sigma)
    used_levels: set[tuple[str, float]] = set()
    trend_used: set[str] = set()
    session_losses = 0
    pos: Optional[Position] = None
    trades: list[Trade] = []

    for candle in active:
        if pos and _check_exit(pos, candle):
            if (pos.trade.pnl or 0.0) < 0:
                session_losses += 1
            trades.append(pos.trade)
            pos = None

        if candle.close > upper_accept:
            up_count += 1
        else:
            up_count = 0
        if candle.close < lower_accept:
            down_count += 1
        else:
            down_count = 0
        if up_count >= ACCEPT_BARS:
            accepted_up = True
        if down_count >= ACCEPT_BARS:
            accepted_down = True

        if pos is not None:
            continue
        if variant.session_loss_stop and session_losses >= variant.session_loss_stop:
            continue

        candidate = _trend_candidate(
            candle, center, sigma, variant, accepted_up, accepted_down, trend_used
        )
        trade_kind = "trend"
        if candidate is None:
            candidate = _fade_candidate(
                candle, center, sigma, levels, variant, used_levels, accepted_up, accepted_down
            )
            trade_kind = "fade"
        if candidate is None:
            continue

        direction, entry, sl, tp, level = candidate
        if trade_kind == "trend":
            trend_used.add("up" if direction == Direction.BUY else "down")
        else:
            used_levels.add(("long" if direction == Direction.BUY else "short", level))

        trade_counter += 1
        meta = {
            "variant": variant.name,
            "session_code": session.code,
            "session_start": session.start.isoformat(),
            "center": center,
            "sigma": sigma,
            "level": level,
            "kind": trade_kind,
            "accepted_up": accepted_up,
            "accepted_down": accepted_down,
        }
        trade = _new_trade(
            f"OSF-{trade_counter}",
            direction,
            entry,
            sl,
            tp,
            candle.timestamp,
            meta,
        )
        pos = Position(trade=trade, entry_bar_ts=candle.timestamp)
        if _check_exit(pos, candle):
            if (pos.trade.pnl or 0.0) < 0:
                session_losses += 1
            trades.append(pos.trade)
            pos = None

    if pos is not None:
        last = session.candles[-1]
        _close_trade(pos.trade, last.close, last.timestamp, ExitReason.FLATTEN)
        trades.append(pos.trade)
    return trades, trade_counter


def _split_segments(trades: list[Trade]) -> tuple[list[float], bool]:
    if not trades:
        return [0.0, 0.0, 0.0], False
    dates = sorted({_topstep_trade_date(t.entry_time) for t in trades})
    if len(dates) < 3:
        total = sum(t.pnl or 0.0 for t in trades)
        return [total, 0.0, 0.0], False
    cuts = [len(dates) // 3, (2 * len(dates)) // 3]
    seg_dates = [set(dates[: cuts[0]]), set(dates[cuts[0] : cuts[1]]), set(dates[cuts[1] :])]
    pnls = []
    for bucket in seg_dates:
        pnls.append(sum((t.pnl or 0.0) for t in trades if _topstep_trade_date(t.entry_time) in bucket))
    return pnls, all(p > 0 for p in pnls)


def _monthly_average(trades: list[Trade]) -> float:
    months: dict[str, float] = {}
    for t in trades:
        key = _topstep_trade_date(t.entry_time)[:7]
        months[key] = months.get(key, 0.0) + (t.pnl or 0.0)
    if not months:
        return 0.0
    return sum(months.values()) / len(months)


def _worst_day(trades: list[Trade]) -> float:
    days: dict[str, float] = {}
    for t in trades:
        key = _topstep_trade_date(t.entry_time)
        days[key] = days.get(key, 0.0) + (t.pnl or 0.0)
    return min(days.values()) if days else 0.0


def _simulate_variant(variant: Variant, sessions: list[SessionInfo]) -> tuple[dict, list[Trade]]:
    allowed = SESSION_SETS[variant.session_set]
    trades: list[Trade] = []
    trade_counter = 0
    for session in sessions:
        if session.code not in allowed:
            continue
        session_trades, trade_counter = _simulate_session(session, variant, trade_counter)
        trades.extend(session_trades)

    trades.sort(key=lambda t: t.entry_time)
    metrics = MetricsCalculator().calculate_all(trades, INITIAL_CAPITAL)
    segs, wf_pass = _split_segments(trades)
    pnl = float(metrics.total_pnl)
    max_dd = float(metrics.max_drawdown)
    pf = float(metrics.profit_factor)
    trades_n = int(metrics.total_trades)
    goal = pnl > 6000 and max_dd < 1000
    accepted = bool(
        goal
        and trades_n >= 80
        and pf >= 1.25
        and float(metrics.expectancy) > 0
        and wf_pass
    )
    score = (pnl / max(max_dd, 100.0)) if pnl > 0 else pnl / 1000.0
    row = {
        **asdict(variant),
        "name": variant.name,
        "trades": trades_n,
        "wins": int(metrics.wins),
        "losses": int(metrics.losses),
        "win_rate": float(metrics.win_rate),
        "pnl": pnl,
        "max_dd": max_dd,
        "pf": pf,
        "expectancy": float(metrics.expectancy),
        "total_gain": float(metrics.total_gain),
        "total_loss": float(metrics.total_loss),
        "avg_win": float(metrics.avg_win),
        "avg_loss": float(metrics.avg_loss),
        "max_consecutive_losses": int(metrics.max_consecutive_losses),
        "worst_day": _worst_day(trades),
        "monthly_avg": _monthly_average(trades),
        "seg1": segs[0],
        "seg2": segs[1],
        "seg3": segs[2],
        "wf_pass": wf_pass,
        "goal": goal,
        "accepted": accepted,
        "score": score,
    }
    return row, trades


def _variants() -> Iterable[Variant]:
    for session_set in SESSION_SETS:
        for opening_minutes in OPENING_MINUTES:
            for sigma_method in SIGMA_METHODS:
                for entry_mode in ENTRY_MODES:
                    for accept_mode in ACCEPT_MODES:
                        for start_sigma in START_SIGMAS:
                            for max_sigma in MAX_SIGMAS:
                                if max_sigma < start_sigma:
                                    continue
                                for target_mode in TARGET_MODES:
                                    for stop_span in STOP_SPANS:
                                        for session_loss_stop in SESSION_LOSS_STOPS:
                                            yield Variant(
                                                session_set=session_set,
                                                opening_minutes=opening_minutes,
                                                sigma_method=sigma_method,
                                                entry_mode=entry_mode,
                                                accept_mode=accept_mode,
                                                start_sigma=start_sigma,
                                                max_sigma=max_sigma,
                                                target_mode=target_mode,
                                                stop_span=stop_span,
                                                session_loss_stop=session_loss_stop,
                                            )


def _fmt(row: dict) -> str:
    goal = "G" if row["goal"] else "-"
    acc = "*" if row["accepted"] else "-"
    wf = "Y" if row["wf_pass"] else "-"
    return (
        f"{goal}{acc} {row['name']:<68} "
        f"{row['trades']:>5} {100 * row['win_rate']:>6.1f}% "
        f"{row['pnl']:>+9.1f} {row['max_dd']:>7.1f} "
        f"{row['pf']:>5.2f} {row['expectancy']:>+7.2f} "
        f"{row['total_loss']:>+9.1f} {row['worst_day']:>+8.1f} "
        f"{wf} {row['seg1']:>+8.0f}/{row['seg2']:>+8.0f}/{row['seg3']:>+8.0f}"
    )


def _write_best_trades(trades: list[Trade]) -> None:
    OUT_TRADES.parent.mkdir(parents=True, exist_ok=True)
    with OUT_TRADES.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "trade_id",
                "entry_time",
                "exit_time",
                "trade_date",
                "session",
                "kind",
                "direction",
                "entry",
                "sl",
                "tp",
                "exit",
                "reason",
                "pnl",
                "commission",
                "fees",
                "center",
                "sigma",
                "level",
                "variant",
            ]
        )
        for t in trades:
            writer.writerow(
                [
                    t.trade_id,
                    t.entry_time.isoformat(),
                    t.exit_time.isoformat() if t.exit_time else "",
                    _topstep_trade_date(t.entry_time),
                    t.meta.get("session_code", ""),
                    t.meta.get("kind", ""),
                    t.direction.value,
                    f"{t.entry_price:.2f}",
                    f"{t.sl_price:.2f}",
                    f"{t.tp_price:.2f}",
                    f"{(t.exit_price or 0.0):.2f}",
                    t.exit_reason.value if t.exit_reason else "",
                    f"{(t.pnl or 0.0):.2f}",
                    f"{t.commission:.2f}",
                    f"{t.fees:.2f}",
                    f"{float(t.meta.get('center', 0.0)):.2f}",
                    f"{float(t.meta.get('sigma', 0.0)):.2f}",
                    t.meta.get("level", ""),
                    t.meta.get("variant", ""),
                ]
            )


def main() -> None:
    candles = sorted(candle_store.load("MNQ", 1), key=lambda c: c.timestamp)
    if not candles:
        raise SystemExit("No MNQ 1m candles in local candle store")
    sessions = _build_sessions(candles)
    variants = list(_variants())

    print(
        f"candles={len(candles)} sessions={len(sessions)} variants={len(variants)} "
        f"range={candles[0].timestamp} -> {candles[-1].timestamp}",
        flush=True,
    )
    print(
        f"contract={CONTRACT_ID} point_value={POINT_VALUE} "
        f"commission={COMMISSION_RT:.2f} fees={FEES_RT:.2f}",
        flush=True,
    )

    rows: list[dict] = []
    best_trades: list[Trade] = []
    best_row: Optional[dict] = None
    total = len(variants)
    for i, variant in enumerate(variants, 1):
        row, trades = _simulate_variant(variant, sessions)
        rows.append(row)
        if best_row is None or (row["accepted"], row["goal"], row["score"], row["pnl"]) > (
            best_row["accepted"],
            best_row["goal"],
            best_row["score"],
            best_row["pnl"],
        ):
            best_row = row
            best_trades = trades
        if i == 1 or i % 100 == 0 or i == total:
            print(
                f"[{i}/{total}] best={best_row['pnl']:+.1f}/DD{best_row['max_dd']:.1f} "
                f"goal={sum(1 for r in rows if r['goal'])} acc={sum(1 for r in rows if r['accepted'])}",
                flush=True,
            )

    rows.sort(key=lambda r: (r["accepted"], r["goal"], r["score"], r["pnl"]), reverse=True)
    goal_rows = [r for r in rows if r["goal"]]
    accepted_rows = [r for r in rows if r["accepted"]]
    positive_rows = [r for r in rows if r["pnl"] > 0]

    lines = [
        "Opening Sigma Fade Study",
        f"created_at_utc: {datetime.now(timezone.utc).isoformat()}",
        f"candles: {len(candles)}  {candles[0].timestamp} -> {candles[-1].timestamp}",
        f"sessions: {len(sessions)}",
        f"variants: {len(variants)}",
        f"contract: {CONTRACT_ID}  point_value={POINT_VALUE:.2f}  commission={COMMISSION_RT:.2f}  fees={FEES_RT:.2f}",
        "cost: PnL is NET after commission+fees, 1 MNQ contract.",
        "goal: PNL > 6000 and maxDD < 1000.  accepted: goal + trades>=80 + PF>=1.25 + WF all three segments positive.",
        "",
        f"goal_count: {len(goal_rows)}",
        f"accepted_count: {len(accepted_rows)}",
        f"positive_count: {len(positive_rows)}",
        "",
        f"{'OK':<2} {'variant':<68} {'n':>5} {'win%':>7} {'pnl':>9} {'maxDD':>7} {'PF':>5} {'expect':>7} {'loss':>9} {'wDay':>8} WF {'seg1/seg2/seg3':>26}",
        "-" * 168,
    ]
    lines.extend(_fmt(r) for r in rows[:60])

    def best_of(label: str, candidates: list[dict], key) -> None:
        if not candidates:
            lines.append(f"{label}: none")
            return
        r = max(candidates, key=key)
        lines.append(f"{label}: {_fmt(r)}")

    lines.append("")
    best_of("best_accepted", accepted_rows, lambda r: (r["score"], r["pnl"]))
    best_of("best_goal", goal_rows, lambda r: (r["score"], r["pnl"]))
    best_of("best_positive_score", positive_rows, lambda r: (r["score"], r["pnl"]))
    if positive_rows:
        low_dd = min(positive_rows, key=lambda r: (r["max_dd"], -r["pnl"]))
        lines.append(f"lowest_dd_positive: {_fmt(low_dd)}")

    summary_by_accept: dict[str, dict[str, float]] = {}
    for mode in ACCEPT_MODES:
        bucket = [r for r in rows if r["accept_mode"] == mode]
        if not bucket:
            continue
        summary_by_accept[mode] = {
            "n": len(bucket),
            "positive": sum(1 for r in bucket if r["pnl"] > 0),
            "goal": sum(1 for r in bucket if r["goal"]),
            "accepted": sum(1 for r in bucket if r["accepted"]),
            "best_pnl": max(r["pnl"] for r in bucket),
            "best_score": max(r["score"] for r in bucket),
        }
    lines.append("")
    lines.append("accept_mode_summary:")
    for mode, s in summary_by_accept.items():
        lines.append(
            f"  {mode:<6} n={int(s['n'])} positive={int(s['positive'])} "
            f"goal={int(s['goal'])} acc={int(s['accepted'])} "
            f"best_pnl={s['best_pnl']:+.1f} best_score={s['best_score']:.2f}"
        )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    OUT_JSON.write_text(
        json.dumps(
            {
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
                "contract_id": CONTRACT_ID,
                "point_value": POINT_VALUE,
                "commission_rt": COMMISSION_RT,
                "fees_rt": FEES_RT,
                "candles": len(candles),
                "range": [candles[0].timestamp.isoformat(), candles[-1].timestamp.isoformat()],
                "sessions": len(sessions),
                "variants": len(variants),
                "rows": rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    _write_best_trades(best_trades)

    print("\n".join(lines), flush=True)
    print(f"wrote {OUT_TXT}", flush=True)
    print(f"wrote {OUT_JSON}", flush=True)
    print(f"wrote {OUT_TRADES}", flush=True)


if __name__ == "__main__":
    main()
