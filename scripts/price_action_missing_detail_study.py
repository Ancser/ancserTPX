"""Offline study of the execution details missing from the mechanical PA test.

The earlier ``price_action_context_study`` tested candle patterns with VWAP,
previous-RTH levels, intraday regime, daily direction, and calendar filters.
This runner keeps that signal library and adds a frozen, causal vocabulary for
the details a discretionary PA trader normally supplies:

* overnight high/low and previous close;
* previous-RTH 70% value area (VAH/VAL/POC);
* opening location and opening-range acceptance;
* relative volume, displacement, and close-to-close follow-through;
* entry-time buckets; and
* exit management (fixed target, scale-out/breakeven, structural trail, and
  time stop).

It is research-only.  It does not register a production model or change live
behaviour.  Every signal uses completed 5-minute data and fills at the next
available 1-minute open.  The 2024-2025 block selects the validation shortlist;
2026 is held out until the final report.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from statistics import median
from typing import Iterable, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.backtest.prop_intraday_research import (  # noqa: E402
    _as_et,
    build_rth_sessions,
    simulate_candidates,
)
from backend.backtest.robustness import series_stats, slip_injection  # noqa: E402
from backend.data import candle_store, market_data  # noqa: E402
from backend.db.models import current_quarterly_contract_id, get_tick_size  # noqa: E402
from backend.strategy.volume_profile import VolumeProfileCalculator  # noqa: E402
from scripts.price_action_context_study import (  # noqa: E402
    DAY_FILTERS,
    ContextSpec,
    SignalContext,
    _as_et as _context_as_et,
    _build_signal_contexts,
    _calendar_event_dates,
    _context_passes,
    _day_filter_passes,
    _previous_day_state,
    _session_quality,
)
from scripts.price_action_study import (  # noqa: E402
    SETUPS,
    SIDES,
    TARGET_R,
    SignalSpec,
    _atr,
    _candidate,
    _generate_specs,
)


DISCOVERY_END = date(2023, 12, 31)
VALIDATION_START = date(2024, 1, 1)
VALIDATION_END = date(2025, 12, 31)
HOLDOUT_START = date(2026, 1, 1)

SIGNAL_MINUTES = 5
SCREEN_MANAGEMENT = "fixed_2R"
SCREEN_DAY_FILTERS = ("all", "quality_only", "exclude_opex_week")
MANAGEMENTS = (
    "fixed_1R",
    "fixed_1_5R",
    "fixed_2R",
    "partial_1R_2R_BE",
    "partial_1R_3R_BE",
    "partial_1R_2R_BE_trail",
    "fixed_2R_time60",
)
RISK_DOLLARS = 200.0
MAX_TRADES_PER_DAY = 2
BASELINE_MAX_HOLD_MINUTES = 120
STOP_BUFFER_TICKS = 2
SLIPPAGE_STRESS_TICKS = 14
MC_SEED = 20260913


@dataclass(frozen=True)
class OvernightRange:
    high: float
    low: float
    volume: int
    bars: int


@dataclass(frozen=True)
class LevelSnapshot:
    previous_high: Optional[float]
    previous_low: Optional[float]
    previous_close: Optional[float]
    overnight_high: Optional[float]
    overnight_low: Optional[float]
    prior_vah: Optional[float]
    prior_val: Optional[float]
    prior_poc: Optional[float]
    opening_high: Optional[float]
    opening_low: Optional[float]
    opening_width: Optional[float]
    open_price: Optional[float]


@dataclass(frozen=True)
class DetailSessionMeta:
    session_date: date
    quality_ok: bool
    opex_day: bool
    opex_week: bool
    previous_day_direction: int
    previous_day_body_fraction: Optional[float]
    previous_day_range: Optional[float]
    previous_day_large: bool
    levels: LevelSnapshot
    signal_contexts: dict[int, SignalContext]


def detail_context_catalog() -> tuple[ContextSpec, ...]:
    """Frozen contexts; no post-result Cartesian-product expansion."""

    names = [
        ("baseline", ()),
        ("overnight_rejection", ("overnight_rejection",)),
        ("overnight_breakout", ("overnight_breakout",)),
        ("overnight_acceptance", ("overnight_acceptance",)),
        ("value_rejection", ("value_rejection",)),
        ("value_breakout", ("value_breakout",)),
        ("value_acceptance", ("value_acceptance",)),
        ("previous_close_rejection", ("previous_close_rejection",)),
        ("level_confluence", ("level_confluence",)),
        ("poc_rejection", ("poc_rejection",)),
        ("open_inside_value", ("open_inside_value",)),
        ("open_initiative", ("open_initiative",)),
        ("open_reversion", ("open_reversion",)),
        ("relative_volume_high", ("relative_volume_high",)),
        ("relative_volume_low", ("relative_volume_low",)),
        ("displacement", ("displacement",)),
        ("follow_through", ("follow_through",)),
        ("opening_range_acceptance", ("opening_range_acceptance",)),
        ("time_open", ("time_open",)),
        ("time_mid", ("time_mid",)),
        ("time_late", ("time_late",)),
        ("vwap_aligned+overnight_rejection", ("vwap_aligned", "overnight_rejection")),
        ("vwap_aligned+value_rejection", ("vwap_aligned", "value_rejection")),
        ("vwap_aligned+level_confluence", ("vwap_aligned", "level_confluence")),
        ("vwap_aligned+relative_volume_high", ("vwap_aligned", "relative_volume_high")),
        ("vwap_aligned+displacement", ("vwap_aligned", "displacement")),
        ("regime_trend+displacement", ("regime_trend", "displacement")),
        ("regime_range+value_rejection", ("regime_range", "value_rejection")),
        ("overnight_rejection+relative_volume_high", ("overnight_rejection", "relative_volume_high")),
        ("value_rejection+relative_volume_high", ("value_rejection", "relative_volume_high")),
        ("value_breakout+relative_volume_high", ("value_breakout", "relative_volume_high")),
        ("value_breakout+displacement", ("value_breakout", "displacement")),
        ("follow_through+relative_volume_high", ("follow_through", "relative_volume_high")),
        ("open_initiative+regime_trend", ("open_initiative", "regime_trend")),
        ("open_reversion+regime_range", ("open_reversion", "regime_range")),
    ]
    return tuple(ContextSpec(name=name, gates=tuple(gates)) for name, gates in names)


def _append_gate(long_gates: set[str], short_gates: set[str], name: str, long_ok: bool, short_ok: bool) -> None:
    if long_ok:
        long_gates.add(name)
    if short_ok:
        short_gates.add(name)


def _touch_reclaim(bar, level: Optional[float], direction: int, tick: float) -> bool:
    if level is None:
        return False
    tolerance = 4.0 * tick
    if direction > 0:
        return float(bar.low) <= level + tolerance and float(bar.low) >= level - 2.0 * tolerance and float(bar.close) > level
    return float(bar.high) >= level - tolerance and float(bar.high) <= level + 2.0 * tolerance and float(bar.close) < level


def _breakout(bar, level: Optional[float], direction: int, tick: float) -> bool:
    if level is None:
        return False
    return float(bar.close) > level + tick if direction > 0 else float(bar.close) < level - tick


def _acceptance(bars, pos: int, level: Optional[float], direction: int, tick: float) -> bool:
    if level is None or pos < 1:
        return False
    current = bars[pos]
    previous = bars[pos - 1]
    return _breakout(current, level, direction, tick) and _breakout(previous, level, direction, tick)


def _any_reclaim(bar, levels: Iterable[Optional[float]], direction: int, tick: float) -> bool:
    return any(_touch_reclaim(bar, level, direction, tick) for level in levels if level is not None)


def _any_breakout(bar, levels: Iterable[Optional[float]], direction: int, tick: float) -> bool:
    return any(_breakout(bar, level, direction, tick) for level in levels if level is not None)


def _profile_snapshot(previous_session, tick: float) -> tuple[Optional[float], Optional[float], Optional[float]]:
    if previous_session is None:
        return None, None, None
    try:
        profile = VolumeProfileCalculator(tick_size=tick, value_area_pct=0.70).calculate(
            list(previous_session.bars)
        )
    except (ValueError, TypeError):
        return None, None, None
    return float(profile.vah), float(profile.val), float(profile.poc)


def _overnight_ranges(candles: Sequence) -> dict[date, OvernightRange]:
    """Return the CME overnight range immediately before each NY RTH date."""

    aggregates: dict[date, list[float | int]] = {}
    for candle in candles:
        et = _as_et(candle.timestamp)
        clock = et.time().replace(tzinfo=None)
        if clock >= dt_time(18, 0):
            session_day = et.date() + timedelta(days=1)
        elif clock < dt_time(9, 30):
            session_day = et.date()
        else:
            continue
        if session_day.weekday() >= 5:
            continue
        row = aggregates.setdefault(session_day, [float(candle.high), float(candle.low), 0, 0])
        row[0] = max(float(row[0]), float(candle.high))
        row[1] = min(float(row[1]), float(candle.low))
        row[2] += int(candle.volume or 0)
        row[3] += 1
    return {
        day: OvernightRange(high=float(values[0]), low=float(values[1]), volume=int(values[2]), bars=int(values[3]))
        for day, values in aggregates.items()
    }


def _add_detail_contexts(
    session,
    previous_session,
    base_contexts: dict[int, SignalContext],
    levels: LevelSnapshot,
    *,
    tick: float,
) -> dict[int, SignalContext]:
    bars = session.signal_bars(SIGNAL_MINUTES)
    atr14 = _atr(bars, 14)
    result: dict[int, SignalContext] = {}
    lower_levels = (levels.previous_low, levels.overnight_low, levels.prior_val, levels.previous_close)
    upper_levels = (levels.previous_high, levels.overnight_high, levels.prior_vah, levels.previous_close)

    open_price = levels.open_price
    if levels.prior_val is not None and levels.prior_vah is not None and open_price is not None:
        open_inside = levels.prior_val <= open_price <= levels.prior_vah
        open_above = open_price > levels.prior_vah + tick
        open_below = open_price < levels.prior_val - tick
    else:
        open_inside = open_above = open_below = False

    for pos, current in enumerate(bars):
        base = base_contexts.get(current.end_index)
        if base is None:
            continue
        long_gates = set(base.long_gates)
        short_gates = set(base.short_gates)
        previous = bars[pos - 1] if pos else None
        et = _as_et(current.timestamp).time().replace(tzinfo=None)

        _append_gate(
            long_gates,
            short_gates,
            "overnight_rejection",
            _touch_reclaim(current, levels.overnight_low, 1, tick),
            _touch_reclaim(current, levels.overnight_high, -1, tick),
        )
        _append_gate(
            long_gates,
            short_gates,
            "overnight_breakout",
            _breakout(current, levels.overnight_high, 1, tick),
            _breakout(current, levels.overnight_low, -1, tick),
        )
        _append_gate(
            long_gates,
            short_gates,
            "overnight_acceptance",
            _acceptance(bars, pos, levels.overnight_high, 1, tick),
            _acceptance(bars, pos, levels.overnight_low, -1, tick),
        )
        _append_gate(
            long_gates,
            short_gates,
            "value_rejection",
            _touch_reclaim(current, levels.prior_val, 1, tick),
            _touch_reclaim(current, levels.prior_vah, -1, tick),
        )
        _append_gate(
            long_gates,
            short_gates,
            "value_breakout",
            _breakout(current, levels.prior_vah, 1, tick),
            _breakout(current, levels.prior_val, -1, tick),
        )
        _append_gate(
            long_gates,
            short_gates,
            "value_acceptance",
            _acceptance(bars, pos, levels.prior_vah, 1, tick),
            _acceptance(bars, pos, levels.prior_val, -1, tick),
        )
        _append_gate(
            long_gates,
            short_gates,
            "previous_close_rejection",
            _touch_reclaim(current, levels.previous_close, 1, tick),
            _touch_reclaim(current, levels.previous_close, -1, tick),
        )
        _append_gate(
            long_gates,
            short_gates,
            "level_confluence",
            _any_reclaim(current, lower_levels, 1, tick),
            _any_reclaim(current, upper_levels, -1, tick),
        )
        _append_gate(
            long_gates,
            short_gates,
            "poc_rejection",
            _touch_reclaim(current, levels.prior_poc, 1, tick),
            _touch_reclaim(current, levels.prior_poc, -1, tick),
        )

        _append_gate(long_gates, short_gates, "open_inside_value", open_inside, open_inside)
        _append_gate(long_gates, short_gates, "open_initiative", open_above, open_below)
        _append_gate(long_gates, short_gates, "open_reversion", open_below, open_above)

        if pos >= 6:
            prior_volumes = [float(row.volume) for row in bars[pos - 6:pos]]
            typical_volume = median(prior_volumes) if prior_volumes else 0.0
            relative_high = typical_volume > 0 and float(current.volume) >= 1.25 * typical_volume
            relative_low = typical_volume > 0 and float(current.volume) <= 0.80 * typical_volume
        else:
            relative_high = relative_low = False
        _append_gate(long_gates, short_gates, "relative_volume_high", relative_high, relative_high)
        _append_gate(long_gates, short_gates, "relative_volume_low", relative_low, relative_low)

        if atr14[pos] is not None:
            atr = float(atr14[pos])
            body = abs(float(current.close) - float(current.open))
            span = max(0.0, float(current.high) - float(current.low))
            location = (float(current.close) - float(current.low)) / span if span else 0.5
            displacement_long = body >= 1.25 * atr and location >= 0.70 and float(current.close) > float(current.open)
            displacement_short = body >= 1.25 * atr and location <= 0.30 and float(current.close) < float(current.open)
        else:
            displacement_long = displacement_short = False
        _append_gate(long_gates, short_gates, "displacement", displacement_long, displacement_short)

        if pos >= 2:
            two_back = bars[pos - 2]
            follow_long = float(current.close) > float(previous.close) > float(two_back.close) and float(current.close) > float(current.open)
            follow_short = float(current.close) < float(previous.close) < float(two_back.close) and float(current.close) < float(current.open)
        else:
            follow_long = follow_short = False
        _append_gate(long_gates, short_gates, "follow_through", follow_long, follow_short)

        if levels.opening_high is not None and levels.opening_low is not None and pos >= 3:
            long_or_accept = _acceptance(bars, pos, levels.opening_high, 1, tick)
            short_or_accept = _acceptance(bars, pos, levels.opening_low, -1, tick)
        else:
            long_or_accept = short_or_accept = False
        _append_gate(long_gates, short_gates, "opening_range_acceptance", long_or_accept, short_or_accept)

        _append_gate(long_gates, short_gates, "time_open", dt_time(9, 35) <= et < dt_time(11, 30), dt_time(9, 35) <= et < dt_time(11, 30))
        _append_gate(long_gates, short_gates, "time_mid", dt_time(11, 30) <= et < dt_time(13, 30), dt_time(11, 30) <= et < dt_time(13, 30))
        _append_gate(long_gates, short_gates, "time_late", dt_time(13, 30) <= et < dt_time(15, 0), dt_time(13, 30) <= et < dt_time(15, 0))

        result[current.end_index] = SignalContext(
            long_gates=frozenset(long_gates),
            short_gates=frozenset(short_gates),
            vwap_slope_ticks=base.vwap_slope_ticks,
            regime=base.regime,
        )
    return result


def _build_levels(session, previous_session, overnight: Optional[OvernightRange], *, tick: float) -> LevelSnapshot:
    previous_high = previous_low = previous_close = None
    if previous_session is not None:
        previous_high = max(float(bar.high) for bar in previous_session.bars)
        previous_low = min(float(bar.low) for bar in previous_session.bars)
        previous_close = float(previous_session.bars[-1].close)
    vah, val, poc = _profile_snapshot(previous_session, tick)
    opening_high = opening_low = None
    first = session.signal_bars(SIGNAL_MINUTES)[:3]
    if len(first) == 3:
        opening_high = max(float(bar.high) for bar in first)
        opening_low = min(float(bar.low) for bar in first)
    return LevelSnapshot(
        previous_high=previous_high,
        previous_low=previous_low,
        previous_close=previous_close,
        overnight_high=overnight.high if overnight else None,
        overnight_low=overnight.low if overnight else None,
        prior_vah=vah,
        prior_val=val,
        prior_poc=poc,
        opening_high=opening_high,
        opening_low=opening_low,
        opening_width=(opening_high - opening_low) if opening_high is not None and opening_low is not None else None,
        open_price=float(session.bars[0].open) if session.bars else None,
    )


def build_detail_metadata(sessions, candles: Sequence, *, tick: float) -> tuple[list[DetailSessionMeta], dict[str, int]]:
    events = _calendar_event_dates([session.session_date for session in sessions])
    overnight = _overnight_ranges(candles)
    metadata: list[DetailSessionMeta] = []
    for index, session in enumerate(sessions):
        previous = sessions[index - 1] if index else None
        daily_state = _previous_day_state(sessions, index)
        levels = _build_levels(session, previous, overnight.get(session.session_date), tick=tick)
        base = _build_signal_contexts(session, previous, tick=tick, daily_state=daily_state)
        contexts = _add_detail_contexts(session, previous, base, levels, tick=tick)
        metadata.append(
            DetailSessionMeta(
                session_date=session.session_date,
                quality_ok=_session_quality(session),
                opex_day=session.session_date in events["opex_day"],
                opex_week=session.session_date in events["opex_week"],
                previous_day_direction=daily_state[0],
                previous_day_body_fraction=daily_state[1],
                previous_day_range=daily_state[2],
                previous_day_large=daily_state[3],
                levels=levels,
                signal_contexts=contexts,
            )
        )
    return metadata, {
        "sessions": len(sessions),
        "quality_sessions": sum(item.quality_ok for item in metadata),
        "opex_days": sum(item.opex_day for item in metadata),
        "opex_week_sessions": sum(item.opex_week for item in metadata),
        "overnight_sessions": sum(bool(item.levels.overnight_high is not None) for item in metadata),
        "prior_value_sessions": sum(bool(item.levels.prior_vah is not None) for item in metadata),
    }


def _management_candidate(setup: str, direction: int, spec: SignalSpec, management: str, tick: float):
    target_r = {
        "fixed_1R": 1.0,
        "fixed_1_5R": 1.5,
        "fixed_2R": 2.0,
    }.get(management)
    if target_r is not None:
        return _candidate(setup, direction, spec, target_r, tick)
    target2 = None
    fraction = 1.0
    move_be = False
    trail = 0
    max_hold = BASELINE_MAX_HOLD_MINUTES
    if management == "partial_1R_2R_BE":
        target1, target2, fraction, move_be = 1.0, 2.0, 0.50, True
    elif management == "partial_1R_3R_BE":
        target1, target2, fraction, move_be = 1.0, 3.0, 0.50, True
    elif management == "partial_1R_2R_BE_trail":
        target1, target2, fraction, move_be, trail = 1.0, 2.0, 0.50, True, 2
    elif management == "fixed_2R_time60":
        target1, max_hold = 2.0, 60
    else:
        raise ValueError(f"unknown management: {management}")
    from backend.backtest.prop_intraday_research import EntryCandidate

    return EntryCandidate(
        strategy=setup,
        variant=f"{setup}_{'long' if direction > 0 else 'short'}_{management}",
        direction=direction,
        signal_index=spec.signal_index,
        signal_time=spec.signal_time,
        stop_price=spec.stop_price,
        target1_kind="risk",
        target1_value=target1,
        target2_kind="risk" if target2 is not None else None,
        target2_value=target2,
        target1_fraction=fraction,
        move_stop_to_breakeven=move_be,
        structural_trail_minutes=SIGNAL_MINUTES if trail else 0,
        trail_buffer_ticks=trail,
        max_hold_minutes=max_hold,
        risk_dollars=RISK_DOLLARS,
        reason=spec.reason,
        meta={"management": management, "stop_buffer_ticks": STOP_BUFFER_TICKS, "tick": tick},
    )


def _split_trades(trades: Sequence, name: str) -> list:
    result = []
    for trade in trades:
        day = _as_et(trade.entry_time).date()
        if name == "discovery" and day <= DISCOVERY_END:
            result.append(trade)
        elif name == "validation" and VALIDATION_START <= day <= VALIDATION_END:
            result.append(trade)
        elif name == "holdout" and day >= HOLDOUT_START:
            result.append(trade)
    return result


def _summary(trades: Sequence, *, symbol: str) -> dict:
    pnls = [float(trade.pnl) for trade in trades]
    stats = series_stats(pnls)
    stress = slip_injection(
        [trade.robustness_row() for trade in trades],
        levels=(SLIPPAGE_STRESS_TICKS,),
        symbol=symbol,
    )["levels"][0]["stats"]
    daily: dict[str, float] = defaultdict(float)
    years: dict[str, list[float]] = defaultdict(list)
    for trade in trades:
        et = _as_et(trade.entry_time)
        daily[et.date().isoformat()] += float(trade.pnl)
        years[str(et.year)].append(float(trade.pnl))
    risks = [float(trade.planned_risk_dollars) for trade in trades if trade.planned_risk_dollars > 0]
    avg_net_r = (
        sum(float(trade.pnl) / float(trade.planned_risk_dollars) for trade in trades if trade.planned_risk_dollars > 0) / len(risks)
        if risks else 0.0
    )
    return {
        "trades": len(trades),
        "pnl": round(float(stats["pnl"]), 2),
        "pf": round(float(stats["pf"]), 4),
        "win": round(float(stats["win"]), 4),
        "max_dd": round(float(stats["max_dd"]), 2),
        "expectancy": round(float(stats["pnl"]) / len(trades), 2) if trades else 0.0,
        "avg_net_r": round(avg_net_r, 5),
        "gross_pnl": round(sum(float(trade.gross_pnl) for trade in trades), 2),
        "costs": round(sum(float(trade.costs) for trade in trades), 2),
        "slip14_pnl": round(float(stress["pnl"]), 2),
        "slip14_pf": round(float(stress["pf"]), 4),
        "trading_days": len(daily),
        "positive_days": sum(value > 0 for value in daily.values()),
        "positive_years": sum(series_stats(values)["pnl"] > 0 for values in years.values()),
        "years": len(years),
    }


def _flat(prefix: str, summary: dict) -> dict:
    return {f"{prefix}_{key}": value for key, value in summary.items()}


def _screen_verdict(full: dict, validation: dict, holdout: dict) -> str:
    if full["trades"] < 40 or validation["trades"] < 15 or holdout["trades"] < 10:
        return "THIN_OOS"
    positive = all(block["pnl"] > 0 and block["pf"] > 1.0 for block in (full, validation, holdout))
    if positive and full["slip14_pf"] > 1.0 and holdout["slip14_pf"] > 1.0:
        return "OOS_SCREEN_LEAD"
    return "REJECT"


def _run_variant(
    symbol: str,
    sessions: Sequence,
    metadata: Sequence[DetailSessionMeta],
    specs_by_key: dict[tuple[str, int], list[list[SignalSpec]]],
    *,
    setup: str,
    direction: int,
    context: ContextSpec,
    day_filter: str,
    management: str,
    tick: float,
) -> tuple[list, int, int]:
    trades = []
    signals = 0
    used_sessions = 0
    for session, meta, specs in zip(sessions, metadata, specs_by_key[(setup, direction)]):
        if not _day_filter_passes(day_filter, meta):
            continue
        selected = [spec for spec in specs if _context_passes(context, meta, spec, direction)]
        signals += len(selected)
        if not selected:
            continue
        used_sessions += 1
        candidates = [_management_candidate(setup, direction, spec, management, tick) for spec in selected]
        session_trades, _ = simulate_candidates(
            session,
            candidates,
            symbol,
            max_trades_per_day=MAX_TRADES_PER_DAY,
        )
        trades.extend(session_trades)
    return trades, signals, used_sessions


def _row(
    symbol: str,
    setup: str,
    direction: int,
    context: ContextSpec,
    day_filter: str,
    management: str,
    sessions_count: int,
    signals: int,
    used_sessions: int,
    trades: Sequence,
) -> dict:
    full = _summary(trades, symbol=symbol)
    discovery = _summary(_split_trades(trades, "discovery"), symbol=symbol)
    validation = _summary(_split_trades(trades, "validation"), symbol=symbol)
    holdout = _summary(_split_trades(trades, "holdout"), symbol=symbol)
    row = {
        "symbol": symbol,
        "setup": setup,
        "side": "long" if direction > 0 else "short",
        "context": context.name,
        "context_gates": "+".join(context.gates) or "none",
        "day_filter": day_filter,
        "management": management,
        "sessions": sessions_count,
        "used_sessions": used_sessions,
        "signals": signals,
        "trades_per_session": round(len(trades) / sessions_count, 4) if sessions_count else 0.0,
    }
    row.update(_flat("full", full))
    row.update(_flat("discovery", discovery))
    row.update(_flat("validation", validation))
    row.update(_flat("holdout", holdout))
    row["screen_verdict"] = _screen_verdict(full, validation, holdout)
    return row


def _load_symbol(symbol: str):
    symbol = str(symbol).upper()
    candles = candle_store.load(symbol, 1)
    if any(candles[i].timestamp > candles[i + 1].timestamp for i in range(len(candles) - 1)):
        candles.sort(key=lambda row: row.timestamp)
    sessions, skipped = build_rth_sessions(candles, require_flatten_bar=True)
    return candles, sessions, {"total_bars": len(candles), "skipped_short_sessions": skipped}


def _load_baseline(symbol: str) -> dict[tuple[str, str, float], dict]:
    path = market_data.derived_path("research", "price_action_study_current.csv")
    if not path.exists():
        return {}
    result = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if str(row.get("symbol", "")).upper() == symbol:
                result[(str(row["setup"]), str(row["side"]), float(row["target_r"]))] = row
    return result


def _baseline_check(symbol: str, sessions, metadata, specs_by_key, tick: float) -> dict:
    expected = _load_baseline(symbol)
    if not expected:
        return {"status": "unavailable", "reason": "price_action_study_current.csv not found"}
    contexts = {item.name: item for item in detail_context_catalog()}
    actual = {}
    management_by_target = {1.0: "fixed_1R", 1.5: "fixed_1_5R", 2.0: "fixed_2R"}
    for setup in SETUPS:
        for direction in SIDES:
            for target in TARGET_R:
                management = management_by_target[float(target)]
                trades, signals, used = _run_variant(
                    symbol, sessions, metadata, specs_by_key,
                    setup=setup, direction=direction, context=contexts["baseline"],
                    day_filter="all", management=management, tick=tick,
                )
                actual[(setup, "long" if direction > 0 else "short", float(target))] = _summary(trades, symbol=symbol)
    mismatches = []
    for key, source in sorted(expected.items()):
        got = actual.get(key)
        if got is None:
            mismatches.append({"key": key, "reason": "missing"})
            continue
        for field in ("trades", "pnl", "pf", "slip14_pf"):
            source_value = float(source[field]) if field != "trades" else int(source[field])
            actual_field = {"pnl": "pnl", "pf": "pf", "slip14_pf": "slip14_pf"}.get(field, field)
            got_value = float(got[actual_field]) if field != "trades" else int(got[actual_field])
            tolerance = 0 if field == "trades" else 1e-3 if field == "pf" or field == "slip14_pf" else 0.01
            if abs(source_value - got_value) > tolerance:
                mismatches.append({"key": key, "field": field, "expected": source_value, "actual": got_value})
    return {
        "status": "pass" if not mismatches else "fail",
        "source": str(market_data.derived_path("research", "price_action_study_current.csv")),
        "checked": len(expected),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:20],
    }


def _shortlist(rows: Sequence[dict], *, limit_each: int = 20) -> list[dict]:
    selected: dict[tuple, dict] = {}
    for symbol in sorted({str(row["symbol"]) for row in rows}):
        pool = [
            row for row in rows
            if row["symbol"] == symbol
            and row["day_filter"] == "all"
            and int(row["validation_trades"]) >= 10
            and int(row["discovery_trades"]) >= 20
        ]
        for row in sorted(pool, key=lambda x: (float(x["validation_pnl"]), float(x["validation_pf"])), reverse=True)[:limit_each]:
            selected[(row["symbol"], row["setup"], row["side"], row["context"], row["day_filter"])] = row
        for row in sorted(pool, key=lambda x: (float(x["validation_pf"]), float(x["validation_pnl"])), reverse=True)[:limit_each]:
            selected[(row["symbol"], row["setup"], row["side"], row["context"], row["day_filter"])] = row
    return list(selected.values())


def _robustness(trades: Sequence, *, symbol: str, mc_iters: int) -> dict:
    from backend.backtest.robustness import evaluate

    result = evaluate(
        [trade.robustness_row() for trade in trades],
        iters=mc_iters,
        seed=MC_SEED,
        dd_threshold=2000.0,
        slip_levels=(1, 2, 4, 8, SLIPPAGE_STRESS_TICKS),
    )
    mc = result.get("monte_carlo") or {}
    wf = result.get("walk_forward") or {}
    stress = next(item["stats"] for item in result["slip"]["levels"] if item["level"] == SLIPPAGE_STRESS_TICKS)
    return {
        "trades": len(trades),
        "pnl": result["stats"]["pnl"],
        "pf": result["stats"]["pf"],
        "max_dd": result["stats"]["max_dd"],
        "slip14_pf": stress["pf"],
        "mc_p_loss": mc.get("p_loss"),
        "mc_dd_p95": mc.get("dd_p95"),
        "mc_pf_p5": mc.get("pf_p5"),
        "mc_pass": bool(result.get("monte_carlo_pass")),
        "wf_pass": bool(wf.get("pass")),
        "wf_pnl": [segment.get("pnl") for segment in wf.get("segments", [])],
        "wf_pf": [segment.get("pf") for segment in wf.get("segments", [])],
    }


def run_symbol(symbol: str, *, mc_iters: int) -> tuple[dict, list[dict]]:
    started = time.time()
    candles, sessions, data_info = _load_symbol(symbol)
    tick = get_tick_size(current_quarterly_contract_id(symbol))
    metadata, coverage = build_detail_metadata(sessions, candles, tick=tick)
    contexts = detail_context_catalog()
    print(
        f"[{symbol}] {data_info['total_bars']:,} bars | {len(sessions):,} sessions | "
        f"quality={coverage['quality_sessions']:,} | overnight={coverage['overnight_sessions']:,} | "
        f"prior_value={coverage['prior_value_sessions']:,} | tick={tick:g}",
        flush=True,
    )

    specs_by_key: dict[tuple[str, int], list[list[SignalSpec]]] = {}
    for setup in SETUPS:
        for direction in SIDES:
            specs_by_key[(setup, direction)] = [
                _generate_specs(
                    session,
                    setup,
                    direction,
                    previous_session=sessions[index - 1] if index else None,
                    tick=tick,
                )
                for index, session in enumerate(sessions)
            ]

    baseline = _baseline_check(symbol, sessions, metadata, specs_by_key, tick)
    if baseline.get("status") == "fail":
        raise RuntimeError(f"baseline reproduction failed for {symbol}: {baseline}")

    rows: list[dict] = []
    total = len(contexts) * len(SCREEN_DAY_FILTERS) * len(SETUPS) * len(SIDES)
    completed = 0
    for context in contexts:
        for day_filter in SCREEN_DAY_FILTERS:
            for setup in SETUPS:
                for direction in SIDES:
                    trades, signals, used = _run_variant(
                        symbol, sessions, metadata, specs_by_key,
                        setup=setup,
                        direction=direction,
                        context=context,
                        day_filter=day_filter,
                        management=SCREEN_MANAGEMENT,
                        tick=tick,
                    )
                    rows.append(_row(symbol, setup, direction, context, day_filter, SCREEN_MANAGEMENT, len(sessions), signals, used, trades))
                    completed += 1
            if completed % max(1, total // 8) == 0:
                print(f"  screen {completed}/{total}", flush=True)

    selected = _shortlist(rows)
    management_rows: list[dict] = []
    robust: dict[str, dict] = {}
    selected_keys = []
    context_map = {item.name: item for item in contexts}
    for base_row in selected:
        selected_keys.append({"setup": base_row["setup"], "side": base_row["side"], "context": base_row["context"], "day_filter": base_row["day_filter"]})
        setup = str(base_row["setup"])
        direction = 1 if base_row["side"] == "long" else -1
        context = context_map[str(base_row["context"])]
        day_filter = str(base_row["day_filter"])
        for management in MANAGEMENTS:
            trades, signals, used = _run_variant(
                symbol, sessions, metadata, specs_by_key,
                setup=setup,
                direction=direction,
                context=context,
                day_filter=day_filter,
                management=management,
                tick=tick,
            )
            management_rows.append(_row(symbol, setup, direction, context, day_filter, management, len(sessions), signals, used, trades))

    # Robustness is deliberately limited to validation-ranked management rows;
    # the holdout is only read here for reporting, never for selection.
    robust_pool = [row for row in management_rows if int(row["validation_trades"]) >= 10 and int(row["discovery_trades"]) >= 20]
    robust_pool = sorted(robust_pool, key=lambda x: (float(x["validation_pnl"]), float(x["validation_pf"])), reverse=True)[:20]
    for row in robust_pool:
        trades, _, _ = _run_variant(
            symbol, sessions, metadata, specs_by_key,
            setup=str(row["setup"]),
            direction=1 if row["side"] == "long" else -1,
            context=context_map[str(row["context"])],
            day_filter=str(row["day_filter"]),
            management=str(row["management"]),
            tick=tick,
        )
        key = "|".join((symbol, row["setup"], row["side"], row["context"], row["day_filter"], row["management"]))
        robust[key] = _robustness(trades, symbol=symbol, mc_iters=mc_iters)

    payload = {
        "symbol": symbol,
        "elapsed_seconds": time.time() - started,
        "dataset": {"info": data_info, "coverage": coverage, "tick": tick},
        "study": {
            "signal_timeframe_minutes": SIGNAL_MINUTES,
            "source_timeframe_minutes": 1,
            "screen_management": SCREEN_MANAGEMENT,
            "management_grid": list(MANAGEMENTS),
            "screen_day_filters": list(SCREEN_DAY_FILTERS),
            "risk_dollars": RISK_DOLLARS,
            "max_trades_per_day": MAX_TRADES_PER_DAY,
            "entry": "next available 1m open after completed 5m signal",
            "one_position_at_a_time": True,
            "context_count": len(contexts),
            "date_split": {
                "discovery_end": DISCOVERY_END.isoformat(),
                "validation": [VALIDATION_START.isoformat(), VALIDATION_END.isoformat()],
                "holdout_start": HOLDOUT_START.isoformat(),
            },
            "definitions": {
                "overnight": "18:00 ET previous calendar day through 09:29 ET current RTH day",
                "prior_value": "production VolumeProfileCalculator on previous complete RTH, value_area_pct=0.70",
                "overnight_rejection": "probe ONL/ONH within 4 ticks and close back through the level",
                "overnight_breakout": "completed 5m close beyond ONH/ONL by one tick",
                "overnight_acceptance": "two consecutive completed 5m closes beyond ONH/ONL by one tick",
                "value_rejection": "probe prior VAL/VAH within 4 ticks and close back inside",
                "value_breakout": "completed 5m close beyond prior VAH/VAL by one tick",
                "value_acceptance": "two consecutive completed 5m closes beyond prior VAH/VAL",
                "level_confluence": "reclaim of any prior low/ONL/VAL/previous-close (or inverse upper level)",
                "open_initiative": "RTH open above prior VAH for long or below prior VAL for short",
                "open_reversion": "RTH open below prior VAL for long or above prior VAH for short",
                "relative_volume_high": "current 5m volume >= 1.25x median of the prior six completed 5m bars",
                "relative_volume_low": "current 5m volume <= 0.80x that causal median",
                "displacement": "body >= 1.25 current 5m ATR and close in directional 30%",
                "follow_through": "three consecutive completed 5m closes advance in the candidate direction",
                "opening_range_acceptance": "two closes beyond the first 15m range boundary",
                "time_buckets": "entry signal timestamp: open 09:35-11:30, mid 11:30-13:30, late 13:30-15:00 ET",
                "screen": "all frozen PA setups/sides/contexts at fixed 2R; validation selects management rows",
            },
        },
        "baseline_check": baseline,
        "screen_rows": rows,
        "management_rows": management_rows,
        "selected_contexts": selected_keys,
        "robustness": robust,
    }
    return payload, rows + management_rows


def _write_csv(path: Path, rows: Sequence[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in fields} for row in rows)


def _markdown(payload: dict) -> str:
    lines = [
        "# Price-action missing-detail study",
        "",
        "Status: **offline exploratory research only; no production strategy changed**",
        f"Generated: `{payload['created_at']}`",
        "",
        "This study asks whether the missing discretionary details—location, acceptance, volume/impulse confirmation, time-of-day, and exit management—add repeatable value to the frozen mechanical PA setup library.",
        "",
        "## Coverage and reproducibility",
        "",
        "| Symbol | Bars | RTH sessions | Quality | Overnight | Prior value | Baseline |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for symbol, block in sorted(payload["datasets"].items()):
        coverage = block["coverage"]
        check = payload["baseline_checks"].get(symbol, {})
        lines.append(
            f"| {symbol} | {block['info']['total_bars']:,} | {coverage['sessions']:,} | {coverage['quality_sessions']:,} | "
            f"{coverage['overnight_sessions']:,} | {coverage['prior_value_sessions']:,} | {check.get('status', '-')} ({check.get('checked', 0)}) |"
        )
    lines += [
        "",
        "The baseline gate compares the no-context 1R/1.5R/2R results with the existing mechanical PA artifact before interpreting any new context.",
        "",
        "## Validation-selected management rows and untouched 2026 holdout",
        "",
        "| Symbol | Setup | Side | Context | Day filter | Management | Val n | Val PF | Val PnL | Holdout n | Holdout PF | Holdout PnL | Stress PF | Verdict |",
        "|---|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for symbol, block in sorted(payload["datasets"].items()):
        rows = [row for row in payload["management_rows"] if row["symbol"] == symbol]
        rows = sorted(rows, key=lambda x: (float(x["validation_pnl"]), float(x["validation_pf"])), reverse=True)[:20]
        for row in rows:
            key = "|".join((symbol, row["setup"], row["side"], row["context"], row["day_filter"], row["management"]))
            robust = payload["robustness"].get(symbol, {}).get(key, {})
            verdict = "AUDITED" if robust else row["screen_verdict"]
            lines.append(
                f"| {symbol} | {row['setup']} | {row['side']} | {row['context']} | {row['day_filter']} | {row['management']} | "
                f"{row['validation_trades']} | {row['validation_pf']} | ${row['validation_pnl']} | {row['holdout_trades']} | "
                f"{row['holdout_pf']} | ${row['holdout_pnl']} | {row['holdout_slip14_pf']} | {verdict} |"
            )
    lines += [
        "",
        "## How to read this",
        "",
        "- A positive full-sample row is not enough. The validation block is used for selection; 2026 is shown only afterward.",
        "- `relative_volume_high`, displacement, and acceptance are confirmations available at the completed signal bar; no future bar is used.",
        "- The prior value area is a completed previous RTH profile. Overnight levels use the fixed 18:00–09:29 ET window.",
        "- Scale-out rows are conservative under 1-minute OHLC: if TP1 and the new breakeven stop are both inside one minute, the stop wins because tick order is unknown.",
        "- Robustness is an audit, not a promotion rule. A large matrix creates multiple-testing risk; discretionary selection cannot be reconstructed from OHLC alone.",
        "",
        "## Results files",
        "",
        f"- JSON: `{payload['outputs']['json']}`",
        f"- CSV: `{payload['outputs']['csv']}`",
        f"- Report: `{payload['outputs']['markdown']}`",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default=["MNQ", "MES"])
    parser.add_argument("--mc-iters", type=int, default=1000)
    parser.add_argument("--output", default=str(market_data.derived_path("research", "price_action_missing_detail_study_current.json")))
    args = parser.parse_args()
    symbols = [str(value).upper() for value in args.symbols]
    if not symbols or any(value not in {"MNQ", "MES"} for value in symbols):
        parser.error("--symbols supports MNQ and MES")
    if args.mc_iters <= 0:
        parser.error("--mc-iters must be positive")

    started = time.time()
    payloads: dict[str, dict] = {}
    all_rows: list[dict] = []
    baseline_checks: dict[str, dict] = {}
    for symbol in symbols:
        payload, rows = run_symbol(symbol, mc_iters=args.mc_iters)
        payloads[symbol] = payload
        all_rows.extend(rows)
        baseline_checks[symbol] = payload["baseline_check"]

    output = Path(args.output)
    if not output.is_absolute():
        output = (ROOT / output).resolve()
    csv_path = output.with_suffix(".csv")
    markdown_path = output.with_suffix(".md")
    aggregate = {
        "created_at": datetime.now().astimezone().isoformat(),
        "elapsed_seconds": time.time() - started,
        "symbols": symbols,
        "study": payloads[symbols[0]]["study"] if payloads else {},
        "datasets": {symbol: block["dataset"] for symbol, block in payloads.items()},
        "baseline_checks": baseline_checks,
        "screen_rows": [row for block in payloads.values() for row in block["screen_rows"]],
        "management_rows": [row for block in payloads.values() for row in block["management_rows"]],
        "selected_contexts": {symbol: block["selected_contexts"] for symbol, block in payloads.items()},
        "robustness": {symbol: block["robustness"] for symbol, block in payloads.items()},
        "outputs": {"json": str(output), "csv": str(csv_path), "markdown": str(markdown_path)},
        "notes": [
            "This is evidence about a frozen OHLC implementation, not a claim that discretionary PA is fully captured.",
            "The matrix is exploratory and subject to multiple-testing/data-snooping risk.",
            "No news/event timestamp filter was applied because no authoritative event tape was supplied to this study.",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(aggregate, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    _write_csv(csv_path, all_rows)
    markdown_path.write_text(_markdown(aggregate), encoding="utf-8")
    print(f"\nJSON: {output}")
    print(f"CSV : {csv_path}")
    print(f"MD  : {markdown_path}")
    print(f"Rows: {len(all_rows):,}")
    print(f"Elapsed: {time.time() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
