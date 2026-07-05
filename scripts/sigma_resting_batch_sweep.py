"""Checkpointed rolling-sigma resting-order sweep.

This is a research runner, not a live engine change.

It tests the user's intended sigma idea as resting limit orders:
  - At the close of each candle, compute rolling center/sigma from completed bars.
  - Place resting buy/sell limits at sigma bands for the next candle.
  - If the next candle touches a resting limit, enter at that limit.

The script is resumable. Results are appended to JSONL and batch CSV files so a
timeout or interrupted process does not lose completed variants.

Run examples:
  python -m scripts.sigma_resting_batch_sweep --reset --max-variants 500
  python -m scripts.sigma_resting_batch_sweep --max-variants 500
  python -m scripts.sigma_resting_batch_sweep --summary-only
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import time as time_mod
from collections import OrderedDict
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
from itertools import product
from pathlib import Path
from typing import Optional

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
OUT_DIR = ROOT / "data" / "machinelearning" / "sigma_resting_batch"
RESULTS_JSONL = OUT_DIR / "results.jsonl"
CHECKPOINT = OUT_DIR / "checkpoint.json"
PROGRESS_TXT = OUT_DIR / "progress.txt"
TOP_CSV = OUT_DIR / "top_latest.csv"
DONE_MARKER = OUT_DIR / "done.marker"
BATCH_DIR = OUT_DIR / "batches"


def configure_output(out_name: str) -> None:
    global OUT_DIR, RESULTS_JSONL, CHECKPOINT, PROGRESS_TXT, TOP_CSV, DONE_MARKER, BATCH_DIR
    safe = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(out_name or "sigma_resting_batch"))
    OUT_DIR = ROOT / "data" / "machinelearning" / safe
    RESULTS_JSONL = OUT_DIR / "results.jsonl"
    CHECKPOINT = OUT_DIR / "checkpoint.json"
    PROGRESS_TXT = OUT_DIR / "progress.txt"
    TOP_CSV = OUT_DIR / "top_latest.csv"
    DONE_MARKER = OUT_DIR / "done.marker"
    BATCH_DIR = OUT_DIR / "batches"

INITIAL_CAPITAL = 50_000.0
TICK = 0.25
MIN_SIGMA_POINTS = 1.0
ACCEPT_SIGMA = 2.0
ACCEPT_BARS = 2

CONTRACT_ID = current_quarterly_contract_id("MNQ")
POINT_VALUE = get_point_value(CONTRACT_ID)
COMMISSION_RT = get_commission_rt(CONTRACT_ID)
FEES_RT = get_fees_rt(CONTRACT_ID)

SESSION_SETS = {
    "ASIA": ("ASIA",),
    "RTH": ("RTH",),
    "ASIA_RTH": ("ASIA", "RTH"),
    "ALL": ("ASIA", "EURO", "PRE", "RTH", "AH"),
}

WINDOWS = (15, 30, 60, 120, 240)
METHODS = ("std",)
LAYOUTS = ("single_nearest", "dual_nearest", "grid_all")
LEVEL_SETS = {
    "L0.5": (0.5,),
    "L1": (1.0,),
    "L1.5": (1.5,),
    "L2": (2.0,),
    "L2.5": (2.5,),
    "L3": (3.0,),
    "L4": (4.0,),
    "G1-3": (1.0, 2.0, 3.0),
    "G0.5-2.5": (0.5, 1.5, 2.5),
    "G2-4": (2.0, 3.0, 4.0),
}
TARGET_MODES = ("inner1", "half", "center", "ladder")
STOP_SPANS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
ACCEPT_MODES = ("none", "filter")
DAILY_LOSS_STOPS = (0, 1, 2)
SIZES = (1, 2, 3)


@dataclass(frozen=True)
class Variant:
    session_set: str
    window: int
    method: str
    layout: str
    level_set: str
    target: str
    stop_span: float
    accept_mode: str
    daily_loss_stop: int

    @property
    def levels(self) -> tuple[float, ...]:
        return LEVEL_SETS[self.level_set]

    @property
    def key(self) -> str:
        return (
            f"{self.session_set}|w{self.window}|{self.method}|{self.layout}|"
            f"{self.level_set}|tp={self.target}|sl={self.stop_span:g}|"
            f"accept={self.accept_mode}|loss={self.daily_loss_stop}"
        )


@dataclass
class SessionData:
    code: str
    start: datetime
    candles: list[Candle]
    dists: dict[tuple[int, str], list[Optional[tuple[float, float]]]]


@dataclass
class PendingOrder:
    direction: Direction
    entry: float
    sl: float
    tp: float
    level: float


@dataclass
class Position:
    trade: Trade
    entry_bar_ts: datetime
    ladder_risk: float = 0.0
    ladder_max_r: float = 0.0


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _round_tick(price: float) -> float:
    return round(float(price) / TICK) * TICK


def _session_for(ts: datetime) -> tuple[str, datetime]:
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
    return "AH", datetime.combine(d, time(20, 0), tzinfo=timezone.utc)


def _weighted_median(values: list[float], weights: list[float]) -> float:
    pairs = sorted(zip(values, weights), key=lambda item: item[0])
    total = sum(weight for _, weight in pairs)
    if total <= 0:
        return pairs[len(pairs) // 2][0]
    acc = 0.0
    half = total / 2.0
    for value, weight in pairs:
        acc += weight
        if acc >= half:
            return value
    return pairs[-1][0]


def _dist_for_window(candles: list[Candle], window: int, method: str) -> list[Optional[tuple[float, float]]]:
    n = len(candles)
    out: list[Optional[tuple[float, float]]] = [None] * n
    prices = [(c.high + c.low + c.close) / 3.0 for c in candles]
    weights = [max(float(c.volume or 0), 1.0) for c in candles]

    if method == "std":
        sw = [0.0]
        swp = [0.0]
        swp2 = [0.0]
        for price, weight in zip(prices, weights):
            sw.append(sw[-1] + weight)
            swp.append(swp[-1] + price * weight)
            swp2.append(swp2[-1] + price * price * weight)
        for i in range(window, n):
            if (_utc(candles[i - 1].timestamp) - _utc(candles[i - window].timestamp)).total_seconds() > (window + 5) * 60:
                continue
            wsum = sw[i] - sw[i - window]
            if wsum <= 0:
                continue
            mean = (swp[i] - swp[i - window]) / wsum
            second = (swp2[i] - swp2[i - window]) / wsum
            sigma = math.sqrt(max(0.0, second - mean * mean))
            if sigma < MIN_SIGMA_POINTS:
                lo = min(c.low for c in candles[i - window : i])
                hi = max(c.high for c in candles[i - window : i])
                sigma = max(sigma, (hi - lo) / 4.0)
            if sigma >= MIN_SIGMA_POINTS:
                out[i] = (_round_tick(mean), max(_round_tick(sigma), TICK))
        return out

    for i in range(window, n):
        if (_utc(candles[i - 1].timestamp) - _utc(candles[i - window].timestamp)).total_seconds() > (window + 5) * 60:
            continue
        vals = prices[i - window : i]
        wts = weights[i - window : i]
        center = _weighted_median(vals, wts)
        sigma = 1.4826 * _weighted_median([abs(p - center) for p in vals], wts)
        if sigma < MIN_SIGMA_POINTS:
            lo = min(c.low for c in candles[i - window : i])
            hi = max(c.high for c in candles[i - window : i])
            sigma = max(sigma, (hi - lo) / 4.0)
        if sigma >= MIN_SIGMA_POINTS:
            out[i] = (_round_tick(center), max(_round_tick(sigma), TICK))
    return out


def _build_sessions(include_mad: bool) -> list[SessionData]:
    methods = ("std", "mad") if include_mad else METHODS
    groups: "OrderedDict[tuple[str, datetime], list[Candle]]" = OrderedDict()
    for candle in sorted(candle_store.load("MNQ", 1), key=lambda c: c.timestamp):
        groups.setdefault(_session_for(candle.timestamp), []).append(candle)

    sessions: list[SessionData] = []
    for (code, start), candles in groups.items():
        if len(candles) < max(WINDOWS) + 2:
            continue
        dists: dict[tuple[int, str], list[Optional[tuple[float, float]]]] = {}
        for window in WINDOWS:
            for method in methods:
                dists[(window, method)] = _dist_for_window(candles, window, method)
        sessions.append(SessionData(code=code, start=start, candles=candles, dists=dists))
    return sessions


def _target_level(level: float, mode: str) -> float:
    if mode == "center":
        return 0.0
    if mode == "half":
        return level / 2.0
    return max(0.0, level - 1.0)


def _close_trade(trade: Trade, exit_price: float, exit_time: datetime, reason: ExitReason) -> None:
    exit_price = _round_tick(exit_price)
    gross = (
        (exit_price - trade.entry_price)
        if trade.direction == Direction.BUY
        else (trade.entry_price - exit_price)
    ) * POINT_VALUE * trade.contracts
    trade.exit_price = exit_price
    trade.exit_time = exit_time
    trade.commission = COMMISSION_RT * trade.contracts
    trade.fees = FEES_RT * trade.contracts
    trade.pnl = gross - trade.commission - trade.fees
    trade.exit_reason = reason


def _new_trade(trade_id: str, order: PendingOrder, entry_time: datetime, meta: dict) -> Trade:
    return Trade(
        trade_id=trade_id,
        strategy=StrategyType.TREND_FOLLOW,
        direction=order.direction,
        entry_price=_round_tick(order.entry),
        entry_time=entry_time,
        sl_price=_round_tick(order.sl),
        tp_price=_round_tick(order.tp),
        original_sl_price=_round_tick(order.sl),
        original_tp_price=_round_tick(order.tp),
        zone_id=str(meta.get("variant", "sigma")),
        zone_source="rolling_sigma",
        contracts=1,
        point_value=POINT_VALUE,
        contract_id=CONTRACT_ID,
        meta=meta,
    )


def _check_exit(pos: Position, candle: Candle) -> bool:
    trade = pos.trade
    entry_bar = _utc(candle.timestamp) == _utc(pos.entry_bar_ts)
    if trade.direction == Direction.BUY:
        hit_sl = candle.low <= trade.sl_price
        hit_tp = candle.high >= trade.tp_price
        if entry_bar:
            if hit_sl:
                _close_trade(trade, trade.sl_price, candle.timestamp, ExitReason.SL)
                return True
            return False
        if hit_sl and hit_tp:
            first = resolve_same_bar_exit(candle.open, trade.sl_price, trade.tp_price)
            _close_trade(
                trade,
                trade.sl_price if first == "sl" else trade.tp_price,
                candle.timestamp,
                ExitReason.SL if first == "sl" else ExitReason.TP,
            )
            return True
        if hit_sl:
            _close_trade(trade, trade.sl_price, candle.timestamp, ExitReason.SL)
            return True
        if hit_tp:
            _close_trade(trade, trade.tp_price, candle.timestamp, ExitReason.TP)
            return True
        return False

    hit_sl = candle.high >= trade.sl_price
    hit_tp = candle.low <= trade.tp_price
    if entry_bar:
        if hit_sl:
            _close_trade(trade, trade.sl_price, candle.timestamp, ExitReason.SL)
            return True
        return False
    if hit_sl and hit_tp:
        first = resolve_same_bar_exit(candle.open, trade.sl_price, trade.tp_price)
        _close_trade(
            trade,
            trade.sl_price if first == "sl" else trade.tp_price,
            candle.timestamp,
            ExitReason.SL if first == "sl" else ExitReason.TP,
        )
        return True
    if hit_sl:
        _close_trade(trade, trade.sl_price, candle.timestamp, ExitReason.SL)
        return True
    if hit_tp:
        _close_trade(trade, trade.tp_price, candle.timestamp, ExitReason.TP)
        return True
    return False


def _build_orders(
    candle: Candle,
    center: float,
    sigma: float,
    variant: Variant,
    accepted_up: bool,
    accepted_down: bool,
) -> list[PendingOrder]:
    orders: list[PendingOrder] = []
    disable_short = variant.accept_mode == "filter" and accepted_up
    disable_long = variant.accept_mode == "filter" and accepted_down
    for level in variant.levels:
        target_l = _target_level(level, variant.target)
        if not disable_short:
            entry = _round_tick(center + level * sigma)
            if entry > candle.close:
                tp = _round_tick(entry - 1_000_000.0) if variant.target == "ladder" else _round_tick(center + target_l * sigma)
                sl = _round_tick(entry + variant.stop_span * sigma)
                if sl > entry > tp:
                    orders.append(PendingOrder(Direction.SELL, entry, sl, tp, level))
        if not disable_long:
            entry = _round_tick(center - level * sigma)
            if entry < candle.close:
                tp = _round_tick(entry + 1_000_000.0) if variant.target == "ladder" else _round_tick(center - target_l * sigma)
                sl = _round_tick(entry - variant.stop_span * sigma)
                if sl < entry < tp:
                    orders.append(PendingOrder(Direction.BUY, entry, sl, tp, level))

    if variant.layout == "grid_all":
        return orders

    if variant.layout == "dual_nearest":
        by_side: list[PendingOrder] = []
        for side in (Direction.BUY, Direction.SELL):
            side_orders = [o for o in orders if o.direction == side]
            if side_orders:
                by_side.append(min(side_orders, key=lambda o: abs(o.entry - candle.close)))
        return by_side

    if not orders:
        return []
    return [min(orders, key=lambda o: abs(o.entry - candle.close))]


def _apply_ladder(pos: Position, candle: Candle) -> None:
    """Research copy of the production ladder exit.

    +2R locks to entry, then each extra integer R locks another +1R while
    keeping SL two integer R behind the best close-based favorable excursion.
    """
    if pos.ladder_risk <= 0:
        return
    trade = pos.trade
    fav = (
        candle.close - trade.entry_price
        if trade.direction == Direction.BUY
        else trade.entry_price - candle.close
    )
    r = fav / pos.ladder_risk
    if r > pos.ladder_max_r:
        pos.ladder_max_r = r
    if pos.ladder_max_r < 2.0:
        return
    lock_r = math.floor(pos.ladder_max_r) - 2.0
    if trade.direction == Direction.BUY:
        new_sl = _round_tick(trade.entry_price + lock_r * pos.ladder_risk)
        if new_sl > trade.sl_price:
            trade.sl_price = new_sl
    else:
        new_sl = _round_tick(trade.entry_price - lock_r * pos.ladder_risk)
        if new_sl < trade.sl_price:
            trade.sl_price = new_sl


def _touched_orders(pending: list[PendingOrder], candle: Candle) -> list[PendingOrder]:
    touched: list[PendingOrder] = []
    for order in pending:
        if order.direction == Direction.BUY and candle.low <= order.entry:
            touched.append(order)
        elif order.direction == Direction.SELL and candle.high >= order.entry:
            touched.append(order)
    return touched


def _pick_fill(touched: list[PendingOrder], candle: Candle) -> tuple[PendingOrder, bool]:
    if len(touched) == 1:
        return touched[0], False
    return min(touched, key=lambda order: abs(order.entry - candle.open)), True


def simulate_variant(sessions: list[SessionData], variant: Variant) -> tuple[list[Trade], dict]:
    allowed = set(SESSION_SETS[variant.session_set])
    trades: list[Trade] = []
    trade_counter = 0
    ambiguous_fills = 0
    touches = 0
    loss_count_by_day: dict[str, int] = {}

    for session in sessions:
        if session.code not in allowed:
            continue

        pos: Optional[Position] = None
        pending: list[PendingOrder] = []
        up_count = 0
        down_count = 0
        accepted_up = False
        accepted_down = False
        dists = session.dists.get((variant.window, variant.method))
        if not dists:
            continue

        for i, candle in enumerate(session.candles):
            trade_day = _topstep_trade_date(candle.timestamp)
            if pos is not None:
                if _check_exit(pos, candle):
                    if (pos.trade.pnl or 0.0) < 0:
                        loss_count_by_day[trade_day] = loss_count_by_day.get(trade_day, 0) + 1
                    trades.append(pos.trade)
                    pos = None
                else:
                    if variant.target == "ladder":
                        _apply_ladder(pos, candle)
                    continue

            if pending:
                touched = _touched_orders(pending, candle)
                if touched:
                    touches += len(touched)
                    fill, ambiguous = _pick_fill(touched, candle)
                    ambiguous_fills += int(ambiguous)
                    trade_counter += 1
                    meta = {
                        "variant": variant.key,
                        "session_code": session.code,
                        "session_start": session.start.isoformat(),
                        "level": fill.level,
                        "layout": variant.layout,
                        "ambiguous_fill": ambiguous,
                    }
                    pos = Position(
                        trade=_new_trade(f"SRB-{trade_counter}", fill, candle.timestamp, meta),
                        entry_bar_ts=candle.timestamp,
                        ladder_risk=abs(fill.entry - fill.sl) if variant.target == "ladder" else 0.0,
                    )
                    pending = []
                    if _check_exit(pos, candle):
                        if (pos.trade.pnl or 0.0) < 0:
                            loss_count_by_day[trade_day] = loss_count_by_day.get(trade_day, 0) + 1
                        trades.append(pos.trade)
                        pos = None
                    continue
                pending = []

            if variant.daily_loss_stop and loss_count_by_day.get(trade_day, 0) >= variant.daily_loss_stop:
                continue

            dist = dists[i]
            if dist is None:
                continue
            center, sigma = dist
            upper_accept = _round_tick(center + ACCEPT_SIGMA * sigma)
            lower_accept = _round_tick(center - ACCEPT_SIGMA * sigma)
            up_count = up_count + 1 if candle.close > upper_accept else 0
            down_count = down_count + 1 if candle.close < lower_accept else 0
            if up_count >= ACCEPT_BARS:
                accepted_up = True
            if down_count >= ACCEPT_BARS:
                accepted_down = True
            if candle.close <= center + min(variant.levels) * sigma:
                accepted_up = False
            if candle.close >= center - min(variant.levels) * sigma:
                accepted_down = False

            pending = _build_orders(candle, center, sigma, variant, accepted_up, accepted_down)

        if pos is not None:
            last = session.candles[-1]
            _close_trade(pos.trade, last.close, last.timestamp, ExitReason.FLATTEN)
            trades.append(pos.trade)

    return trades, {
        "ambiguous_fills": ambiguous_fills,
        "touches": touches,
    }


def _filter_values(raw: str | None, allowed) -> list:
    if not raw:
        return list(allowed)
    wanted = [item.strip() for item in str(raw).split(",") if item.strip()]
    return [item for item in allowed if str(item) in set(wanted)]


def build_variants(args) -> list[Variant]:
    include_mad = bool(args.include_mad)
    methods = ("std", "mad") if include_mad else METHODS
    sessions = _filter_values(args.sessions, SESSION_SETS.keys())
    windows = [int(x) for x in _filter_values(args.windows, WINDOWS)]
    layouts = _filter_values(args.layouts, LAYOUTS)
    level_sets = _filter_values(args.level_sets, LEVEL_SETS.keys())
    target_modes = _filter_values(args.targets, TARGET_MODES)
    variants = [
        Variant(*parts)
        for parts in product(
            sessions,
            windows,
            methods,
            layouts,
            level_sets,
            target_modes,
            STOP_SPANS,
            ACCEPT_MODES,
            DAILY_LOSS_STOPS,
        )
    ]
    return variants


def _load_checkpoint() -> int:
    if not CHECKPOINT.exists():
        return 0
    try:
        data = json.loads(CHECKPOINT.read_text(encoding="utf-8"))
        return max(0, int(data.get("next_index", 0)))
    except Exception:
        return 0


def _row_for_result(index: int, total: int, variant: Variant, metrics, extra: dict, size: int) -> dict:
    return {
        "index": index,
        "total": total,
        "variant": variant.key,
        "session_set": variant.session_set,
        "window": variant.window,
        "method": variant.method,
        "layout": variant.layout,
        "level_set": variant.level_set,
        "target": variant.target,
        "stop_span": variant.stop_span,
        "accept_mode": variant.accept_mode,
        "daily_loss_stop": variant.daily_loss_stop,
        "size": size,
        "trades": metrics.total_trades,
        "pnl": round(metrics.total_pnl * size, 2),
        "max_dd": round(metrics.max_drawdown * size, 2),
        "profit_factor": round(metrics.profit_factor, 4),
        "win_rate": round(metrics.win_rate, 4),
        "total_loss": round(metrics.total_loss * size, 2),
        "total_gain": round(metrics.total_gain * size, 2),
        "ambiguous_fills": extra.get("ambiguous_fills", 0),
        "touches": extra.get("touches", 0),
    }


def _read_results() -> list[dict]:
    if not RESULTS_JSONL.exists():
        return []
    rows: list[dict] = []
    with RESULTS_JSONL.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def write_summary(rows: list[dict]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    top = sorted(
        rows,
        key=lambda r: (
            float(r.get("pnl", 0)) / max(float(r.get("max_dd", 0)), 1.0),
            float(r.get("pnl", 0)),
        ),
        reverse=True,
    )[:300]
    with TOP_CSV.open("w", newline="", encoding="utf-8") as fh:
        if top:
            writer = csv.DictWriter(fh, fieldnames=list(top[0].keys()))
            writer.writeheader()
            writer.writerows(top)


def run(args) -> None:
    if args.reset and OUT_DIR.exists():
        resolved = OUT_DIR.resolve()
        if "sigma_resting_batch" not in str(resolved):
            raise RuntimeError(f"Refusing to reset unexpected path: {resolved}")
        shutil.rmtree(resolved)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    BATCH_DIR.mkdir(parents=True, exist_ok=True)

    variants = build_variants(args)
    total = len(variants)
    start_index = _load_checkpoint()
    if start_index >= total:
        write_summary(_read_results())
        DONE_MARKER.write_text("done\n", encoding="utf-8")
        print(f"Already complete: {total}/{total}. Summary: {TOP_CSV}")
        return

    sessions = _build_sessions(args.include_mad)
    calc = MetricsCalculator()
    end_index = min(total, start_index + args.max_variants) if args.max_variants else total
    batch_rows: list[dict] = []
    t0 = time_mod.time()

    with RESULTS_JSONL.open("a", encoding="utf-8") as out:
        for index in range(start_index, end_index):
            variant = variants[index]
            trades, extra = simulate_variant(sessions, variant)
            metrics = calc.calculate_all(trades, INITIAL_CAPITAL)
            for size in SIZES:
                row = _row_for_result(index, total, variant, metrics, extra, size)
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                batch_rows.append(row)
            out.flush()

            done = index + 1
            if done % args.batch_size == 0 or done == end_index:
                batch_no = done // args.batch_size
                batch_path = BATCH_DIR / f"batch_{batch_no:05d}_{start_index:05d}_{done:05d}.csv"
                with batch_path.open("w", newline="", encoding="utf-8") as fh:
                    writer = csv.DictWriter(fh, fieldnames=list(batch_rows[0].keys()))
                    writer.writeheader()
                    writer.writerows(batch_rows)
                elapsed = time_mod.time() - t0
                CHECKPOINT.write_text(
                    json.dumps(
                        {
                            "next_index": done,
                            "total": total,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                            "elapsed_seconds": round(elapsed, 2),
                            "last_batch": str(batch_path),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                PROGRESS_TXT.write_text(
                    f"{done}/{total} variants complete | elapsed {elapsed:.1f}s | last {variant.key}\n",
                    encoding="utf-8",
                )
                batch_rows = []
                print(f"checkpoint {done}/{total} | {elapsed:.1f}s")

    all_rows = _read_results()
    write_summary(all_rows)
    if end_index >= total:
        DONE_MARKER.write_text("done\n", encoding="utf-8")
    print(f"chunk complete {end_index}/{total}; top={TOP_CSV}; checkpoint={CHECKPOINT}")


def summary_only() -> None:
    rows = _read_results()
    write_summary(rows)
    ck = _load_checkpoint()
    print(f"rows={len(rows)} checkpoint={ck} top={TOP_CSV}")
    if rows:
        for row in sorted(
            rows,
            key=lambda r: (
                float(r.get("pnl", 0)) / max(float(r.get("max_dd", 0)), 1.0),
                float(r.get("pnl", 0)),
            ),
            reverse=True,
        )[:20]:
            print(
                f"pnl={row['pnl']} dd={row['max_dd']} pf={row['profit_factor']} "
                f"win={row['win_rate']} loss={row['total_loss']} trades={row['trades']} "
                f"x{row['size']} {row['variant']}"
            )


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-name", default="sigma_resting_batch")
    parser.add_argument("--max-variants", type=int, default=500)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--include-mad", action="store_true")
    parser.add_argument("--sessions", default="")
    parser.add_argument("--windows", default="")
    parser.add_argument("--layouts", default="")
    parser.add_argument("--level-sets", default="")
    parser.add_argument("--targets", default="")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--summary-only", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    ns = parse_args()
    configure_output(ns.out_name)
    if ns.summary_only:
        summary_only()
    else:
        run(ns)
