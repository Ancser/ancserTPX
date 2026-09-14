"""Mechanical price-action setup study on the canonical 1-minute futures store.

This is a research runner only.  It does not register a production strategy and
does not change the application's trading behaviour.

The study turns commonly described price-action ideas into a small, frozen
rule library so that visual labels do not become post-hoc discretion:

* pin-bar rejection at a prior local extreme;
* two-bar engulfing reversal;
* outside-bar reversal;
* inside-bar breakout;
* NR7 compression breakout;
* failed breakout of a prior 20-bar range;
* trend pullback continuation;
* opening-range breakout and retest;
* rejection of the previous RTH high/low.

All signals are formed on completed RTH 5-minute bars built from the canonical
1-minute candles.  A signal fills at the next available 1-minute open.  The
existing research execution and robustness functions provide the shared
commission/fee, position sizing, one-position-at-a-time, same-bar ambiguity,
walk-forward, bootstrap, and slippage handling.

Example:

    python scripts/price_action_study.py --symbols MNQ MES --mc-iters 1000

The generated JSON/CSV are written to the external market-data derived tree,
not into the source repository.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.backtest.prop_intraday_research import (  # noqa: E402
    ET,
    EntryCandidate,
    RthSession,
    _as_et,
    load_symbol_sessions,
    simulate_candidates,
)
from backend.backtest.robustness import evaluate, series_stats  # noqa: E402
from backend.data import market_data  # noqa: E402
from backend.db.models import current_quarterly_contract_id, get_tick_size  # noqa: E402


SETUPS = (
    "pin_rejection",
    "engulfing_reversal",
    "outside_reversal",
    "inside_breakout",
    "nr7_breakout",
    "failed_breakout",
    "trend_pullback",
    "opening_range_retest",
    "prior_day_rejection",
)
SIDES = (1, -1)
TARGET_R = (1.0, 1.5, 2.0)
SIGNAL_MINUTES = 5
RISK_DOLLARS = 200.0
MAX_TRADES_PER_DAY = 2
MAX_HOLD_MINUTES = 120
STOP_BUFFER_TICKS = 2
SLIPPAGE_STRESS_TICKS = 14
MC_ITERS = 1000
MC_SEED = 20260913
MC_DD_THRESHOLD = 2000.0


SETUP_DESCRIPTIONS = {
    "pin_rejection": (
        "上一段同向延伸後，5m K 棒以長影線掃過前 10 根極值，"
        "收在反轉端 25% 內；影線至少為實體兩倍。"
    ),
    "engulfing_reversal": (
        "上一根與當前根方向相反，當前實體完全包住上一根實體，"
        "且前方三根有反向淨移動並觸及前 10 根極值。"
    ),
    "outside_reversal": (
        "當前 5m 高低點同時超過上一根，前方三根有反向淨移動，"
        "收盤位於外包 K 的反轉端 30% 內。"
    ),
    "inside_breakout": (
        "前一根為母 K 內的 inside bar；當前完成 K 收盤越過 inside bar 邊界，"
        "以突破方向進場。"
    ),
    "nr7_breakout": (
        "前一根是最近七根中最窄的 NR7 K；當前完成 K 收盤突破 NR7 高/低。"
    ),
    "failed_breakout": (
        "當前 K 刺穿前 20 根區間的一側但收回區間內，"
        "形成失敗突破／failed auction。"
    ),
    "trend_pullback": (
        "20 EMA 位於 50 EMA 同方向，前兩至三根反向回撤，"
        "當前 K 收盤重新越過前一根高/低並站回 20 EMA。"
    ),
    "opening_range_retest": (
        "09:30–09:45 ET opening range 先被突破，之後回踩邊界並重新收回，"
        "只取每個方向第一個 retest。"
    ),
    "prior_day_rejection": (
        "當前 5m K 觸及前一完整 RTH 的 high/low，"
        "但收回其內側並以反轉方向收盤。"
    ),
}


@dataclass(frozen=True)
class SignalSpec:
    """A causal signal before the exit target is selected."""

    signal_index: int
    signal_time: datetime
    stop_price: float
    reason: str


def _bar_range(bar) -> float:
    return max(0.0, float(bar.high) - float(bar.low))


def _body(bar) -> float:
    return abs(float(bar.close) - float(bar.open))


def _close_location(bar) -> float:
    span = _bar_range(bar)
    if span <= 0:
        return 0.5
    return (float(bar.close) - float(bar.low)) / span


def _is_bull(bar) -> bool:
    return float(bar.close) > float(bar.open)


def _is_bear(bar) -> bool:
    return float(bar.close) < float(bar.open)


def _ema(values: list[float], length: int) -> list[float]:
    alpha = 2.0 / (float(length) + 1.0)
    out: list[float] = []
    previous: Optional[float] = None
    for value in values:
        previous = float(value) if previous is None else alpha * float(value) + (1.0 - alpha) * previous
        out.append(previous)
    return out


def _atr(signal_bars, length: int = 14) -> list[Optional[float]]:
    """Causal true-range mean; value at i includes no bar after i."""

    tr: list[float] = []
    for i, bar in enumerate(signal_bars):
        previous_close = float(signal_bars[i - 1].close) if i else float(bar.open)
        tr.append(
            max(
                _bar_range(bar),
                abs(float(bar.high) - previous_close),
                abs(float(bar.low) - previous_close),
            )
        )
    out: list[Optional[float]] = [None] * len(tr)
    for i in range(length - 1, len(tr)):
        out[i] = sum(tr[i - length + 1 : i + 1]) / float(length)
    return out


def _prior_extreme(signal_bars, i: int, direction: int, lookback: int = 10) -> bool:
    if i < lookback:
        return False
    window = signal_bars[i - lookback : i]
    if direction > 0:
        return float(signal_bars[i].low) <= min(float(row.low) for row in window)
    return float(signal_bars[i].high) >= max(float(row.high) for row in window)


def _opposite_context(signal_bars, i: int, direction: int, lookback: int = 3) -> bool:
    """Require a net move against the proposed reversal over prior bars."""

    if i < lookback:
        return False
    window = signal_bars[i - lookback : i]
    closes = [float(row.close) for row in window]
    changes = [closes[j] - closes[j - 1] for j in range(1, len(closes))]
    if direction > 0:
        return closes[-1] < closes[0] and sum(change < 0 for change in changes) >= 2
    return closes[-1] > closes[0] and sum(change > 0 for change in changes) >= 2


def _candidate(
    setup: str,
    direction: int,
    spec: SignalSpec,
    target_r: float,
    tick: float,
) -> EntryCandidate:
    return EntryCandidate(
        strategy=setup,
        variant=f"{setup}_{'long' if direction > 0 else 'short'}_{target_r:g}R",
        direction=direction,
        signal_index=spec.signal_index,
        signal_time=spec.signal_time,
        stop_price=spec.stop_price,
        target1_kind="risk",
        target1_value=target_r,
        max_hold_minutes=MAX_HOLD_MINUTES,
        risk_dollars=RISK_DOLLARS,
        reason=spec.reason,
        meta={"target_r": target_r, "stop_buffer_ticks": STOP_BUFFER_TICKS, "tick": tick},
    )


def _generate_specs(
    session: RthSession,
    setup: str,
    direction: int,
    *,
    previous_session: Optional[RthSession],
    tick: float,
) -> list[SignalSpec]:
    """Generate one setup's causal signal specs for one session and side."""

    bars = session.signal_bars(SIGNAL_MINUTES)
    if not bars:
        return []
    closes = [float(bar.close) for bar in bars]
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    atr14 = _atr(bars, 14)
    buffer = STOP_BUFFER_TICKS * tick
    specs: list[SignalSpec] = []
    fired_retest = False

    # The first three 5-minute bars are the fixed 09:30-09:45 ET opening range.
    opening_high = max((float(bar.high) for bar in bars[:3]), default=math.nan)
    opening_low = min((float(bar.low) for bar in bars[:3]), default=math.nan)

    for i, current in enumerate(bars):
        if i < 1:
            continue
        previous = bars[i - 1]
        current_range = _bar_range(current)
        current_body = _body(current)
        if current_range <= 0:
            continue

        if setup == "pin_rejection":
            atr = atr14[i]
            if atr is None or current_range < 0.60 * atr:
                continue
            lower_wick = min(float(current.open), float(current.close)) - float(current.low)
            upper_wick = float(current.high) - max(float(current.open), float(current.close))
            if direction > 0:
                ok = (
                    _opposite_context(bars, i, direction)
                    and _prior_extreme(bars, i, direction)
                    and _is_bull(current)
                    and _close_location(current) >= 0.75
                    and lower_wick >= 2.0 * max(current_body, tick)
                    and upper_wick <= max(current_body, tick)
                )
                stop = float(current.low) - buffer
            else:
                ok = (
                    _opposite_context(bars, i, direction)
                    and _prior_extreme(bars, i, direction)
                    and _is_bear(current)
                    and _close_location(current) <= 0.25
                    and upper_wick >= 2.0 * max(current_body, tick)
                    and lower_wick <= max(current_body, tick)
                )
                stop = float(current.high) + buffer
            if ok:
                specs.append(SignalSpec(current.end_index, current.timestamp, stop, "pin rejection at local extreme"))

        elif setup == "engulfing_reversal":
            if not _opposite_context(bars, i, direction) or not _prior_extreme(bars, i, direction):
                continue
            if direction > 0:
                ok = (
                    _is_bear(previous)
                    and _is_bull(current)
                    and float(current.open) <= float(previous.close)
                    and float(current.close) >= float(previous.open)
                    and _close_location(current) >= 0.60
                )
                stop = min(float(current.low), float(previous.low)) - buffer
            else:
                ok = (
                    _is_bull(previous)
                    and _is_bear(current)
                    and float(current.open) >= float(previous.close)
                    and float(current.close) <= float(previous.open)
                    and _close_location(current) <= 0.40
                )
                stop = max(float(current.high), float(previous.high)) + buffer
            if ok:
                specs.append(SignalSpec(current.end_index, current.timestamp, stop, "engulfing reversal at local extreme"))

        elif setup == "outside_reversal":
            if not _opposite_context(bars, i, direction):
                continue
            outside = float(current.high) > float(previous.high) and float(current.low) < float(previous.low)
            if not outside:
                continue
            if direction > 0:
                ok = _is_bull(current) and _close_location(current) >= 0.70
                stop = float(current.low) - buffer
            else:
                ok = _is_bear(current) and _close_location(current) <= 0.30
                stop = float(current.high) + buffer
            if ok:
                specs.append(SignalSpec(current.end_index, current.timestamp, stop, "outside-bar reversal after opposing move"))

        elif setup == "inside_breakout":
            if i < 2:
                continue
            mother = bars[i - 2]
            inside = previous
            is_inside = float(inside.high) <= float(mother.high) and float(inside.low) >= float(mother.low)
            if not is_inside:
                continue
            if direction > 0:
                ok = _is_bull(current) and float(current.close) > float(inside.high)
                stop = float(inside.low) - buffer
            else:
                ok = _is_bear(current) and float(current.close) < float(inside.low)
                stop = float(inside.high) + buffer
            if ok:
                specs.append(SignalSpec(current.end_index, current.timestamp, stop, "inside-bar boundary breakout"))

        elif setup == "nr7_breakout":
            if i < 7:
                continue
            narrow = _bar_range(previous)
            prior_ranges = [_bar_range(row) for row in bars[i - 7 : i]]
            if narrow > min(prior_ranges) + 1e-9:
                continue
            if direction > 0:
                ok = _is_bull(current) and float(current.close) > float(previous.high)
                stop = float(previous.low) - buffer
            else:
                ok = _is_bear(current) and float(current.close) < float(previous.low)
                stop = float(previous.high) + buffer
            if ok:
                specs.append(SignalSpec(current.end_index, current.timestamp, stop, "NR7 compression breakout"))

        elif setup == "failed_breakout":
            if i < 20:
                continue
            window = bars[i - 20 : i]
            prior_high = max(float(row.high) for row in window)
            prior_low = min(float(row.low) for row in window)
            if direction > 0:
                ok = (
                    _is_bull(current)
                    and float(current.low) < prior_low
                    and float(current.close) > prior_low
                    and _close_location(current) >= 0.55
                )
                stop = float(current.low) - buffer
            else:
                ok = (
                    _is_bear(current)
                    and float(current.high) > prior_high
                    and float(current.close) < prior_high
                    and _close_location(current) <= 0.45
                )
                stop = float(current.high) + buffer
            if ok:
                specs.append(SignalSpec(current.end_index, current.timestamp, stop, "failed 20-bar range breakout"))

        elif setup == "trend_pullback":
            if i < 4:
                continue
            pullback = bars[i - 3 : i]
            pullback_closes = [float(row.close) for row in pullback]
            changes = [pullback_closes[j] - pullback_closes[j - 1] for j in range(1, len(pullback_closes))]
            if direction > 0:
                ok = (
                    ema20[i - 1] > ema50[i - 1]
                    and pullback_closes[-1] < pullback_closes[0]
                    and sum(change < 0 for change in changes) >= 1
                    and _is_bull(current)
                    and float(current.close) > float(previous.high)
                    and float(current.close) > ema20[i]
                )
                stop = min(float(row.low) for row in pullback) - buffer
            else:
                ok = (
                    ema20[i - 1] < ema50[i - 1]
                    and pullback_closes[-1] > pullback_closes[0]
                    and sum(change > 0 for change in changes) >= 1
                    and _is_bear(current)
                    and float(current.close) < float(previous.low)
                    and float(current.close) < ema20[i]
                )
                stop = max(float(row.high) for row in pullback) + buffer
            if ok:
                specs.append(SignalSpec(current.end_index, current.timestamp, stop, "20/50 EMA trend pullback"))

        elif setup == "opening_range_retest":
            if i < 3 or not math.isfinite(opening_high) or not math.isfinite(opening_low) or fired_retest:
                continue
            start_et = _as_et(session.bars[current.start_index].timestamp)
            if start_et.hour == 9 and start_et.minute < 45:
                continue
            tolerance = 3.0 * tick
            earlier = bars[3:i]
            if direction > 0:
                broke = any(float(row.close) > opening_high for row in earlier)
                ok = (
                    broke
                    and _is_bull(current)
                    and float(current.low) <= opening_high + tolerance
                    and float(current.low) >= opening_high - 2.0 * tolerance
                    and float(current.close) > opening_high
                )
                stop = opening_high - 2.0 * tolerance
            else:
                broke = any(float(row.close) < opening_low for row in earlier)
                ok = (
                    broke
                    and _is_bear(current)
                    and float(current.high) >= opening_low - tolerance
                    and float(current.high) <= opening_low + 2.0 * tolerance
                    and float(current.close) < opening_low
                )
                stop = opening_low + 2.0 * tolerance
            if ok:
                specs.append(SignalSpec(current.end_index, current.timestamp, stop, "opening-range boundary retest"))
                fired_retest = True

        elif setup == "prior_day_rejection":
            if previous_session is None:
                continue
            prior_high = max(float(row.high) for row in previous_session.bars)
            prior_low = min(float(row.low) for row in previous_session.bars)
            if direction > 0:
                ok = (
                    _is_bull(current)
                    and float(current.low) <= prior_low
                    and float(current.close) > prior_low
                    and _close_location(current) >= 0.60
                )
                stop = float(current.low) - buffer
            else:
                ok = (
                    _is_bear(current)
                    and float(current.high) >= prior_high
                    and float(current.close) < prior_high
                    and _close_location(current) <= 0.40
                )
                stop = float(current.high) + buffer
            if ok:
                specs.append(SignalSpec(current.end_index, current.timestamp, stop, "previous RTH extreme rejection"))

    return specs


def _stats_for_trades(trades, *, mc_iters: int) -> dict:
    rows = [trade.robustness_row() for trade in trades]
    robust = evaluate(
        rows,
        iters=mc_iters,
        seed=MC_SEED,
        dd_threshold=MC_DD_THRESHOLD,
        slip_levels=(1, 2, 4, 8, SLIPPAGE_STRESS_TICKS),
    )
    stats = robust["stats"]
    slip14 = next(
        row["stats"]
        for row in robust["slip"]["levels"]
        if row["level"] == SLIPPAGE_STRESS_TICKS
    )
    mc = robust.get("monte_carlo") or {}
    wf = robust.get("walk_forward") or {}
    planned = [float(trade.planned_risk_dollars) for trade in trades if trade.planned_risk_dollars > 0]
    avg_r = (
        sum(float(trade.pnl) / float(trade.planned_risk_dollars) for trade in trades if trade.planned_risk_dollars > 0)
        / len(planned)
        if planned
        else 0.0
    )
    by_year: dict[str, list[float]] = defaultdict(list)
    by_entry_bucket: dict[str, list[float]] = defaultdict(list)
    for trade in trades:
        et = _as_et(trade.entry_time)
        by_year[str(et.year)].append(float(trade.pnl))
        minute = et.hour * 60 + et.minute
        bucket = "open_0930_1030" if minute < 630 else "mid_1030_1300" if minute < 780 else "late_1300_1550"
        by_entry_bucket[bucket].append(float(trade.pnl))
    yearly = {year: series_stats(values) for year, values in sorted(by_year.items())}
    time_buckets = {name: series_stats(values) for name, values in sorted(by_entry_bucket.items())}
    positive_years = sum(1 for value in yearly.values() if value["n"] > 0 and value["pnl"] > 0)
    wf_segments = wf.get("segments", [])
    wf_pass = bool(wf.get("pass"))
    mc_pass = bool(
        mc
        and mc.get("p_loss", 1.0) <= 0.05
        and mc.get("dd_p95", float("inf")) < MC_DD_THRESHOLD
        and mc.get("pf_p5", 0.0) > 1.0
    )
    # A "stable" label is deliberately stricter than a positive backtest:
    # enough trades, all three date blocks profitable, positive 14-tick PF,
    # and the shared bootstrap gates.  Rare is reported independently.
    stable = bool(
        len(trades) >= 40
        and wf_pass
        and mc_pass
        and slip14["pf"] > 1.0
        and positive_years >= max(3, math.ceil(len(yearly) * 0.60))
    )
    return {
        "stats": stats,
        "slip14": slip14,
        "monte_carlo": mc,
        "walk_forward": wf,
        "yearly": yearly,
        "entry_time_buckets": time_buckets,
        "positive_years": positive_years,
        "avg_net_r": avg_r,
        "gross_pnl": sum(float(trade.gross_pnl) for trade in trades),
        "costs": sum(float(trade.costs) for trade in trades),
        "stable": stable,
    }


def _compact_walk_forward(value: Optional[dict]) -> Optional[dict]:
    if not value:
        return None
    return {
        "pass": bool(value.get("pass")),
        "segments": value.get("segments", []),
    }


def _compact_monte_carlo(value: Optional[dict]) -> Optional[dict]:
    if not value:
        return None
    keep = (
        "iters",
        "seed",
        "n",
        "p_loss",
        "dd_p95",
        "dd_threshold",
        "pf_p5",
    )
    return {key: value[key] for key in keep if key in value}


def _read_meta(symbol: str) -> dict:
    path = market_data.candle_store_dir() / f"{symbol}_accumulated_1m.meta.json"
    if not path.exists():
        return {"path": str(path), "exists": False}
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {"path": str(path), "exists": True, "error": str(exc)}
    body["path"] = str(path)
    body["exists"] = True
    return body


def _variant_row(
    symbol: str,
    setup: str,
    direction: int,
    target_r: float,
    *,
    sessions_count: int,
    signals: list[SignalSpec],
    trades,
    skipped: dict,
    score: dict,
) -> dict:
    stats = score["stats"]
    rate = len(trades) / sessions_count if sessions_count else 0.0
    rare = bool(rate <= 0.20 and len(trades) >= 30)
    if score["stable"] and rare:
        verdict = "RARE_STABLE"
    elif score["stable"]:
        verdict = "STABLE_NOT_RARE"
    elif rare and len(trades) >= 20 and score["slip14"]["pf"] > 1.0:
        verdict = "RARE_BUT_NOT_STABLE"
    elif len(trades) < 30:
        verdict = "THIN_SAMPLE"
    else:
        verdict = "REJECT"
    wf_segments = score["walk_forward"].get("segments", []) if score["walk_forward"] else []
    return {
        "symbol": symbol,
        "setup": setup,
        "side": "long" if direction > 0 else "short",
        "target_r": target_r,
        "signal_bars": len(signals),
        "signal_days": len({_as_et(spec.signal_time).date() for spec in signals}),
        "trades": len(trades),
        "trades_per_session": round(rate, 4),
        "pnl": round(float(stats["pnl"]), 2),
        "pf": round(float(stats["pf"]), 4),
        "win": round(float(stats["win"]), 4),
        "max_dd": round(float(stats["max_dd"]), 2),
        "expectancy": round(float(stats["pnl"]) / len(trades), 2) if trades else 0.0,
        "avg_net_r": round(float(score["avg_net_r"]), 5),
        "gross_pnl": round(float(score["gross_pnl"]), 2),
        "costs": round(float(score["costs"]), 2),
        "slip14_pnl": round(float(score["slip14"]["pnl"]), 2),
        "slip14_pf": round(float(score["slip14"]["pf"]), 4),
        "wf_pass": bool(score["walk_forward"].get("pass")) if score["walk_forward"] else False,
        "wf_pf": [round(float(segment["pf"]), 4) for segment in wf_segments],
        "mc_pass": bool(
            score["monte_carlo"]
            and score["monte_carlo"].get("p_loss", 1.0) <= 0.05
            and score["monte_carlo"].get("dd_p95", float("inf")) < MC_DD_THRESHOLD
            and score["monte_carlo"].get("pf_p5", 0.0) > 1.0
        ),
        "mc_p_loss": round(float(score["monte_carlo"].get("p_loss", 1.0)), 4) if score["monte_carlo"] else None,
        "mc_dd_p95": round(float(score["monte_carlo"].get("dd_p95", 0.0)), 2) if score["monte_carlo"] else None,
        "mc_pf_p5": round(float(score["monte_carlo"].get("pf_p5", 0.0)), 4) if score["monte_carlo"] else None,
        "positive_years": score["positive_years"],
        "years": len(score["yearly"]),
        "skipped": dict(skipped),
        "verdict": verdict,
    }


def run_symbol(symbol: str, *, mc_iters: int) -> tuple[dict, list[dict]]:
    started = time.time()
    sessions, info = load_symbol_sessions(symbol)
    tick = get_tick_size(current_quarterly_contract_id(symbol))
    meta = _read_meta(symbol)
    print(
        f"[{symbol}] {info.total_bars:,} bars | {info.rth_sessions:,} full RTH sessions | "
        f"{info.first_timestamp} -> {info.last_timestamp} | tick={tick:g}"
    )

    # Generate every setup once.  The target-R variants share precisely the
    # same signal tape and differ only in the predeclared exit multiple.
    specs_by_key: dict[tuple[str, int], list[list[SignalSpec]]] = {}
    for setup in SETUPS:
        for direction in SIDES:
            per_session: list[list[SignalSpec]] = []
            for index, session in enumerate(sessions):
                previous = sessions[index - 1] if index else None
                per_session.append(
                    _generate_specs(
                        session,
                        setup,
                        direction,
                        previous_session=previous,
                        tick=tick,
                    )
                )
            specs_by_key[(setup, direction)] = per_session

    rows: list[dict] = []
    detailed: dict[str, dict] = {}
    for setup in SETUPS:
        for direction in SIDES:
            per_session = specs_by_key[(setup, direction)]
            for target_r in TARGET_R:
                all_trades = []
                skipped = defaultdict(int)
                signals: list[SignalSpec] = []
                for session, specs in zip(sessions, per_session):
                    signals.extend(specs)
                    candidates = [
                        _candidate(setup, direction, spec, target_r, tick)
                        for spec in specs
                    ]
                    trades, local_skipped = simulate_candidates(
                        session,
                        candidates,
                        symbol,
                        max_trades_per_day=MAX_TRADES_PER_DAY,
                    )
                    all_trades.extend(trades)
                    for name, count in local_skipped.items():
                        skipped[name] += int(count)
                score = _stats_for_trades(all_trades, mc_iters=mc_iters)
                row = _variant_row(
                    symbol,
                    setup,
                    direction,
                    target_r,
                    sessions_count=len(sessions),
                    signals=signals,
                    trades=all_trades,
                    skipped=skipped,
                    score=score,
                )
                rows.append(row)
                detailed[f"{setup}|{row['side']}|{target_r:g}R"] = {
                    "summary": row,
                    "yearly": score["yearly"],
                    "entry_time_buckets": score["entry_time_buckets"],
                    # The shared robustness function also returns chart curves
                    # for the UI.  They are intentionally omitted here; the
                    # scalar gates and date blocks are the auditable output
                    # and keep this report compact.
                    "walk_forward": _compact_walk_forward(score["walk_forward"]),
                    "monte_carlo": _compact_monte_carlo(score["monte_carlo"]),
                }
                print(
                    f"  {setup:<24} {'L' if direction > 0 else 'S'} {target_r:g}R "
                    f"signals={len(signals):4d} trades={len(all_trades):4d} "
                    f"PF={row['pf']:5.2f} PnL={row['pnl']:+9.0f} "
                    f"14tPF={row['slip14_pf']:5.2f} {row['verdict']}"
                )

    payload = {
        "symbol": symbol,
        "created_at": datetime.now().astimezone().isoformat(),
        "elapsed_seconds": time.time() - started,
        "study": {
            "signal_timeframe_minutes": SIGNAL_MINUTES,
            "source_timeframe_minutes": 1,
            "timezone": str(ET),
            "risk_dollars": RISK_DOLLARS,
            "max_trades_per_day": MAX_TRADES_PER_DAY,
            "max_hold_minutes": MAX_HOLD_MINUTES,
            "stop_buffer_ticks": STOP_BUFFER_TICKS,
            "target_r": list(TARGET_R),
            "baseline_costs": "canonical commission_rt + fees_rt",
            "stress_slippage_ticks_round_trip": SLIPPAGE_STRESS_TICKS,
            "entry": "next available 1m open after completed 5m signal",
            "same_bar_ambiguity": "shared resolve_same_bar_exit heuristic; conservative existing runner",
            "one_position_at_a_time": True,
            "definitions": SETUP_DESCRIPTIONS,
        },
        "dataset": {
            "info": info.__dict__,
            "metadata": meta,
        },
        "results": rows,
        "details": detailed,
    }
    return payload, rows


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0]) if rows else ["symbol"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default=["MNQ", "MES"])
    parser.add_argument("--mc-iters", type=int, default=MC_ITERS)
    parser.add_argument(
        "--output",
        default=str(market_data.derived_path("research", "price_action_study_current.json")),
    )
    args = parser.parse_args()
    symbols = [str(symbol).upper() for symbol in args.symbols]
    if not symbols or any(symbol not in {"MNQ", "MES"} for symbol in symbols):
        parser.error("--symbols supports MNQ and MES")
    if args.mc_iters <= 0:
        parser.error("--mc-iters must be positive")

    started = time.time()
    all_payloads = []
    all_rows = []
    for symbol in symbols:
        payload, rows = run_symbol(symbol, mc_iters=args.mc_iters)
        all_payloads.append(payload)
        all_rows.extend(rows)

    output = Path(args.output)
    if not output.is_absolute():
        output = (ROOT / output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    aggregate = {
        "created_at": datetime.now().astimezone().isoformat(),
        "elapsed_seconds": time.time() - started,
        "symbols": symbols,
        "study": all_payloads[0]["study"] if all_payloads else {},
        "datasets": {payload["symbol"]: payload["dataset"] for payload in all_payloads},
        "results": all_rows,
        "details": {
            payload["symbol"]: payload["details"]
            for payload in all_payloads
        },
    }
    output.write_text(json.dumps(aggregate, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    _write_csv(output.with_suffix(".csv"), all_rows)

    print(f"\nJSON: {output}")
    print(f"CSV : {output.with_suffix('.csv')}")
    print(f"Elapsed: {time.time() - started:.1f}s")
    print("\nShortlist:")
    for row in sorted(
        all_rows,
        key=lambda item: (item["verdict"] not in {"RARE_STABLE", "STABLE_NOT_RARE"}, -item["slip14_pf"], -item["trades"]),
    ):
        if row["verdict"] in {"RARE_STABLE", "STABLE_NOT_RARE", "RARE_BUT_NOT_STABLE"}:
            print(
                f"  {row['symbol']} {row['setup']} {row['side']} {row['target_r']:g}R "
                f"n={row['trades']} PF={row['pf']:.2f} 14tPF={row['slip14_pf']:.2f} "
                f"WF={'Y' if row['wf_pass'] else 'N'} MC={'Y' if row['mc_pass'] else 'N'} "
                f"{row['verdict']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
