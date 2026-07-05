"""Monte Carlo test for random 1 MNQ account survival.

Research-only. It simulates a very simple random policy:

  - One trade per Topstep trading day.
  - Direction is random long/short.
  - 1 MNQ contract.
  - Exit when net PnL is approximately +$1000 or -$1000, or flatten at the
    end of the selected trading day.
  - Account passes if equity reaches +$3000 before a $2000 trailing drawdown.
  - Account reaches payout if equity reaches +$4000 before a $2000 trailing
    drawdown.

Entry styles tested:
  - topstep_open: first candle of the Topstep trading day.
  - rth_open: first candle at/after 13:30 UTC.
  - random_bar: random candle inside the day, excluding the final 30 minutes.

Run:
  PYTHONIOENCODING=utf-8 python -m scripts.random_account_distribution
"""
from __future__ import annotations

import csv
import json
import random
import statistics
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Optional

from backend.backtest.engine import _topstep_trade_date
from backend.backtest.intrabar import resolve_same_bar_exit
from backend.data import candle_store
from backend.db.models import (
    Candle,
    Direction,
    current_quarterly_contract_id,
    get_commission_rt,
    get_fees_rt,
    get_point_value,
)


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "machinelearning"
OUT_TXT = OUT_DIR / "random_account_distribution.txt"
OUT_JSON = OUT_DIR / "random_account_distribution.json"
OUT_DAILY = OUT_DIR / "random_account_daily_edges.csv"

CONTRACT_ID = current_quarterly_contract_id("MNQ")
POINT_VALUE = get_point_value(CONTRACT_ID)
COMMISSION_RT = get_commission_rt(CONTRACT_ID)
FEES_RT = get_fees_rt(CONTRACT_ID)
ROUND_TURN_COST = COMMISSION_RT + FEES_RT

TRIALS = 100_000
SEED = 109
EXAM_TARGET = 3_000.0
PAYOUT_TARGET = 4_000.0
MAX_DD_LIMIT = 2_000.0
INTRADAY_FLATTEN_BUFFER_BARS = 30

# Choose tick-rounded distances so net PnL is very close to +/- 1000.
TP_POINTS = round(((1_000.0 + ROUND_TURN_COST) / POINT_VALUE) / 0.25) * 0.25
SL_POINTS = round(((1_000.0 - ROUND_TURN_COST) / POINT_VALUE) / 0.25) * 0.25


@dataclass
class DayData:
    trade_date: str
    candles: list[Candle]
    topstep_idx: int
    rth_idx: Optional[int]
    random_indices: list[int]


@dataclass
class TradeOutcome:
    pnl: float
    reason: str
    entry_time: datetime
    exit_time: datetime
    entry: float
    exit: float
    direction: str


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _group_days(candles: list[Candle]) -> list[DayData]:
    groups: "OrderedDict[str, list[Candle]]" = OrderedDict()
    for candle in sorted(candles, key=lambda c: c.timestamp):
        groups.setdefault(_topstep_trade_date(candle.timestamp), []).append(candle)

    days: list[DayData] = []
    for trade_date, bars in groups.items():
        if len(bars) < 60:
            continue
        rth_idx = None
        for i, candle in enumerate(bars):
            tod = _utc(candle.timestamp).time()
            if time(13, 30) <= tod < time(20, 0):
                rth_idx = i
                break
        max_random = max(1, len(bars) - INTRADAY_FLATTEN_BUFFER_BARS)
        random_indices = list(range(0, max_random))
        days.append(
            DayData(
                trade_date=trade_date,
                candles=bars,
                topstep_idx=0,
                rth_idx=rth_idx,
                random_indices=random_indices,
            )
        )
    return days


def _net_pnl(direction: Direction, entry: float, exit_price: float) -> float:
    if direction == Direction.BUY:
        gross = (exit_price - entry) * POINT_VALUE
    else:
        gross = (entry - exit_price) * POINT_VALUE
    return gross - ROUND_TURN_COST


def _simulate_trade(day: DayData, direction: Direction, entry_idx: int) -> TradeOutcome:
    bars = day.candles[entry_idx:]
    first = bars[0]
    entry = float(first.open)
    if direction == Direction.BUY:
        tp = entry + TP_POINTS
        sl = entry - SL_POINTS
    else:
        tp = entry - TP_POINTS
        sl = entry + SL_POINTS

    for candle in bars:
        if direction == Direction.BUY:
            hit_tp = candle.high >= tp
            hit_sl = candle.low <= sl
            if hit_tp and hit_sl:
                first_hit = resolve_same_bar_exit(candle.open, sl, tp)
                exit_price = sl if first_hit == "sl" else tp
                return TradeOutcome(
                    pnl=_net_pnl(direction, entry, exit_price),
                    reason=first_hit,
                    entry_time=first.timestamp,
                    exit_time=candle.timestamp,
                    entry=entry,
                    exit=exit_price,
                    direction=direction.value,
                )
            if hit_tp:
                return TradeOutcome(_net_pnl(direction, entry, tp), "tp", first.timestamp, candle.timestamp, entry, tp, direction.value)
            if hit_sl:
                return TradeOutcome(_net_pnl(direction, entry, sl), "sl", first.timestamp, candle.timestamp, entry, sl, direction.value)
        else:
            hit_tp = candle.low <= tp
            hit_sl = candle.high >= sl
            if hit_tp and hit_sl:
                first_hit = resolve_same_bar_exit(candle.open, sl, tp)
                exit_price = sl if first_hit == "sl" else tp
                return TradeOutcome(
                    pnl=_net_pnl(direction, entry, exit_price),
                    reason=first_hit,
                    entry_time=first.timestamp,
                    exit_time=candle.timestamp,
                    entry=entry,
                    exit=exit_price,
                    direction=direction.value,
                )
            if hit_tp:
                return TradeOutcome(_net_pnl(direction, entry, tp), "tp", first.timestamp, candle.timestamp, entry, tp, direction.value)
            if hit_sl:
                return TradeOutcome(_net_pnl(direction, entry, sl), "sl", first.timestamp, candle.timestamp, entry, sl, direction.value)

    last = bars[-1]
    return TradeOutcome(
        pnl=_net_pnl(direction, entry, last.close),
        reason="flatten",
        entry_time=first.timestamp,
        exit_time=last.timestamp,
        entry=entry,
        exit=float(last.close),
        direction=direction.value,
    )


def _build_outcomes(days: list[DayData]) -> dict[str, dict[tuple[int, str, int], TradeOutcome]]:
    cache: dict[str, dict[tuple[int, str, int], TradeOutcome]] = {
        "topstep_open": {},
        "rth_open": {},
        "random_bar": {},
    }
    for day_i, day in enumerate(days):
        for direction in (Direction.BUY, Direction.SELL):
            cache["topstep_open"][(day_i, direction.value, day.topstep_idx)] = _simulate_trade(day, direction, day.topstep_idx)
            if day.rth_idx is not None:
                cache["rth_open"][(day_i, direction.value, day.rth_idx)] = _simulate_trade(day, direction, day.rth_idx)
            for idx in day.random_indices:
                cache["random_bar"][(day_i, direction.value, idx)] = _simulate_trade(day, direction, idx)
    return cache


def _outcome_for(
    style: str,
    day_i: int,
    days: list[DayData],
    cache: dict[str, dict[tuple[int, str, int], TradeOutcome]],
    rng: random.Random,
) -> Optional[TradeOutcome]:
    direction = Direction.BUY if rng.random() < 0.5 else Direction.SELL
    day = days[day_i]
    if style == "topstep_open":
        idx = day.topstep_idx
    elif style == "rth_open":
        if day.rth_idx is None:
            return None
        idx = day.rth_idx
    else:
        if not day.random_indices:
            return None
        idx = rng.choice(day.random_indices)
    return cache[style].get((day_i, direction.value, idx))


def _simulate_accounts(
    style: str,
    days: list[DayData],
    cache: dict[str, dict[tuple[int, str, int], TradeOutcome]],
    trials: int,
    seed: int,
) -> tuple[dict, list[dict]]:
    rng = random.Random(seed)
    rows: list[dict] = []
    pass_count = 0
    payout_count = 0
    pass_then_payout = 0
    fail_count = 0
    unresolved_count = 0
    days_to_pass: list[int] = []
    days_to_payout: list[int] = []

    for trial in range(trials):
        start_i = rng.randrange(0, len(days))
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        passed = False
        payout = False
        failed = False
        pass_day = None
        payout_day = None
        trades = 0

        for day_i in range(start_i, len(days)):
            outcome = _outcome_for(style, day_i, days, cache, rng)
            if outcome is None:
                continue
            trades += 1
            equity += outcome.pnl
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)

            if max_dd >= MAX_DD_LIMIT:
                failed = True
                break
            if not passed and equity >= EXAM_TARGET:
                passed = True
                pass_day = trades
            if equity >= PAYOUT_TARGET:
                payout = True
                payout_day = trades
                break

        if passed:
            pass_count += 1
            if pass_day is not None:
                days_to_pass.append(pass_day)
        if payout:
            payout_count += 1
            if passed:
                pass_then_payout += 1
            if payout_day is not None:
                days_to_payout.append(payout_day)
        elif failed:
            fail_count += 1
        else:
            unresolved_count += 1

        rows.append(
            {
                "trial": trial,
                "style": style,
                "start_date": days[start_i].trade_date,
                "trades": trades,
                "equity": round(equity, 2),
                "max_dd": round(max_dd, 2),
                "passed_3k": passed,
                "payout_4k": payout,
                "failed_dd": failed,
                "pass_day": pass_day,
                "payout_day": payout_day,
            }
        )

    stats = {
        "style": style,
        "trials": trials,
        "pass_3k_count": pass_count,
        "pass_3k_rate": pass_count / trials,
        "payout_4k_count": payout_count,
        "payout_4k_rate": payout_count / trials,
        "pass_to_payout_rate": (pass_then_payout / pass_count) if pass_count else 0.0,
        "fail_dd_count": fail_count,
        "fail_dd_rate": fail_count / trials,
        "unresolved_count": unresolved_count,
        "unresolved_rate": unresolved_count / trials,
        "median_days_to_pass": statistics.median(days_to_pass) if days_to_pass else None,
        "median_days_to_payout": statistics.median(days_to_payout) if days_to_payout else None,
        "avg_final_equity": sum(r["equity"] for r in rows) / len(rows),
        "avg_max_dd": sum(r["max_dd"] for r in rows) / len(rows),
    }
    return stats, rows


def _daily_edge_summary(days: list[DayData], cache: dict[str, dict[tuple[int, str, int], TradeOutcome]]) -> list[dict]:
    rows: list[dict] = []
    for style in ("topstep_open", "rth_open"):
        pnls = []
        reasons: dict[str, int] = {}
        for day_i, day in enumerate(days):
            idx = day.topstep_idx if style == "topstep_open" else day.rth_idx
            if idx is None:
                continue
            for direction in ("buy", "sell"):
                out = cache[style].get((day_i, direction, idx))
                if out is None:
                    continue
                pnls.append(out.pnl)
                reasons[out.reason] = reasons.get(out.reason, 0) + 1
                rows.append(
                    {
                        "style": style,
                        "trade_date": day.trade_date,
                        "direction": direction,
                        "entry_time": out.entry_time.isoformat(),
                        "exit_time": out.exit_time.isoformat(),
                        "reason": out.reason,
                        "pnl": round(out.pnl, 2),
                    }
                )
        if pnls:
            print(
                f"{style}: n={len(pnls)} mean={statistics.mean(pnls):+.2f} "
                f"median={statistics.median(pnls):+.2f} win%={sum(1 for p in pnls if p > 0)/len(pnls):.1%} "
                f"reasons={reasons}",
                flush=True,
            )
    return rows


def _fmt_pct(x: float) -> str:
    return f"{100.0 * x:.2f}%"


def main() -> None:
    candles = sorted(candle_store.load("MNQ", 1), key=lambda c: c.timestamp)
    if not candles:
        raise SystemExit("No MNQ 1m candles in local candle store")
    days = _group_days(candles)
    print(
        f"candles={len(candles)} days={len(days)} "
        f"range={candles[0].timestamp} -> {candles[-1].timestamp}",
        flush=True,
    )
    print(
        f"contract={CONTRACT_ID} point_value={POINT_VALUE} "
        f"commission={COMMISSION_RT:.2f} fees={FEES_RT:.2f} "
        f"tp_points={TP_POINTS:.2f} sl_points={SL_POINTS:.2f}",
        flush=True,
    )
    cache = _build_outcomes(days)
    daily_rows = _daily_edge_summary(days, cache)

    all_stats = []
    sample_rows = []
    for offset, style in enumerate(("topstep_open", "rth_open", "random_bar")):
        stats, rows = _simulate_accounts(style, days, cache, TRIALS, SEED + offset * 10_000)
        all_stats.append(stats)
        sample_rows.extend(rows[:200])
        print(
            f"{style}: pass3k={_fmt_pct(stats['pass_3k_rate'])} "
            f"payout4k={_fmt_pct(stats['payout_4k_rate'])} "
            f"failDD={_fmt_pct(stats['fail_dd_rate'])} "
            f"unresolved={_fmt_pct(stats['unresolved_rate'])}",
            flush=True,
        )

    lines = [
        "Random 1 MNQ Account Distribution",
        f"created_at_utc: {datetime.now(timezone.utc).isoformat()}",
        f"candles: {len(candles)}  {candles[0].timestamp} -> {candles[-1].timestamp}",
        f"trade_days: {len(days)}",
        f"trials_per_style: {TRIALS}",
        f"contract: {CONTRACT_ID} point_value={POINT_VALUE:.2f} commission={COMMISSION_RT:.2f} fees={FEES_RT:.2f}",
        f"tp_points={TP_POINTS:.2f}, sl_points={SL_POINTS:.2f}; target net is approximately +/- $1000.",
        f"exam target: +{EXAM_TARGET:.0f}; payout target: +{PAYOUT_TARGET:.0f}; max trailing DD breach: {MAX_DD_LIMIT:.0f}.",
        "",
        f"{'style':<14} {'pass3k':>9} {'payout4k':>9} {'pass->pay':>10} {'failDD':>9} {'unresolved':>10} {'medPass':>8} {'medPay':>8} {'avgEq':>9} {'avgDD':>8}",
        "-" * 102,
    ]
    for s in all_stats:
        lines.append(
            f"{s['style']:<14} {_fmt_pct(s['pass_3k_rate']):>9} "
            f"{_fmt_pct(s['payout_4k_rate']):>9} {_fmt_pct(s['pass_to_payout_rate']):>10} "
            f"{_fmt_pct(s['fail_dd_rate']):>9} {_fmt_pct(s['unresolved_rate']):>10} "
            f"{str(s['median_days_to_pass']):>8} {str(s['median_days_to_payout']):>8} "
            f"{s['avg_final_equity']:>+9.1f} {s['avg_max_dd']:>8.1f}"
        )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    OUT_JSON.write_text(
        json.dumps(
            {
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
                "trials": TRIALS,
                "seed": SEED,
                "contract_id": CONTRACT_ID,
                "point_value": POINT_VALUE,
                "commission_rt": COMMISSION_RT,
                "fees_rt": FEES_RT,
                "tp_points": TP_POINTS,
                "sl_points": SL_POINTS,
                "exam_target": EXAM_TARGET,
                "payout_target": PAYOUT_TARGET,
                "max_dd_limit": MAX_DD_LIMIT,
                "stats": all_stats,
                "sample_accounts": sample_rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    with OUT_DAILY.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["style", "trade_date", "direction", "entry_time", "exit_time", "reason", "pnl"],
        )
        writer.writeheader()
        writer.writerows(daily_rows)

    print("\n".join(lines), flush=True)
    print(f"wrote {OUT_TXT}", flush=True)
    print(f"wrote {OUT_JSON}", flush=True)
    print(f"wrote {OUT_DAILY}", flush=True)


if __name__ == "__main__":
    main()
