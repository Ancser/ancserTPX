"""Causal VWAP/level/regime context sweep for the mechanical PA study.

This is an offline research runner.  It does not register a production model,
change Backtest/Live behaviour, or place orders.

The existing :mod:`scripts.price_action_study` is the baseline candle/market-
structure library.  This runner reuses its signal generation and execution
adapter, then tests a predeclared context vocabulary:

* RTH cumulative VWAP: aligned, reclaim, and one-standard-deviation extension;
* previous complete RTH high/low: rejection and breakout/acceptance;
* causal intraday regime: EMA20/EMA50 plus VWAP slope trend, or balanced range;
* previous-day candle state: aligned, opposed, neutral, and large-range;
* objective session filters: exact 390-minute data quality, OPEX day, and OPEX
  week (the latter two are calendar proxies, not an options feed).

The full matrix is screened with a fixed 2R target.  A validation-only
shortlist is then rerun across the baseline 1R/1.5R/2R target grid and the
shared robustness evaluator.  The 2026 holdout is not used for shortlist
selection.

Example::

    python scripts/price_action_context_study.py --symbols MNQ MES --mc-iters 1000

Outputs are written to the external ``ancserMarketData/derived/research`` tree.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import json
import math
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.backtest.robustness import evaluate, series_stats, slip_injection  # noqa: E402
from backend.data import market_data  # noqa: E402
from backend.db.models import current_quarterly_contract_id, get_tick_size  # noqa: E402
from scripts.price_action_study import (  # noqa: E402
    SETUPS,
    SIDES,
    TARGET_R,
    SignalSpec,
    _as_et,
    _atr,
    _candidate,
    _ema,
    _generate_specs,
    load_symbol_sessions,
    simulate_candidates,
)


# The date split is fixed before inspecting this run's results.  The current
# 2026 partial year is therefore a real holdout, not a ranking feature.
DISCOVERY_END = date(2023, 12, 31)
VALIDATION_START = date(2024, 1, 1)
VALIDATION_END = date(2025, 12, 31)
HOLDOUT_START = date(2026, 1, 1)

SCREEN_TARGET_R = 2.0
SIGNAL_MINUTES = 5
MAX_TRADES_PER_DAY = 2
STOP_BUFFER_TICKS = 2
RISK_DOLLARS = 200.0
SLIPPAGE_STRESS_TICKS = 14
MC_DD_THRESHOLD = 2000.0
MC_SEED = 20260913

DAY_FILTERS = (
    "all",
    "quality_only",
    "exclude_opex_day",
    "exclude_opex_week",
    "quality_exclude_opex_day",
    "quality_exclude_opex_week",
)


@dataclass(frozen=True)
class ContextSpec:
    name: str
    gates: tuple[str, ...]


@dataclass(frozen=True)
class SignalContext:
    long_gates: frozenset[str]
    short_gates: frozenset[str]
    vwap_slope_ticks: Optional[float]
    regime: str


@dataclass(frozen=True)
class SessionMeta:
    session_date: date
    quality_ok: bool
    opex_day: bool
    opex_week: bool
    previous_day_direction: int
    previous_day_body_fraction: Optional[float]
    previous_day_range: Optional[float]
    previous_day_large: bool
    signal_contexts: dict[int, SignalContext]


def context_catalog() -> tuple[ContextSpec, ...]:
    """Return the frozen context matrix used by the study.

    Combinations are deliberately explicit.  This makes the search space
    auditable and prevents an accidental post-hoc Cartesian product from
    silently becoming another data-mining sweep.
    """

    names = [
        ("baseline", ()),
        ("vwap_aligned", ("vwap_aligned",)),
        ("vwap_reclaim", ("vwap_reclaim",)),
        ("vwap_deviation", ("vwap_deviation",)),
        ("prior_rejection", ("prior_rejection",)),
        ("prior_breakout", ("prior_breakout",)),
        ("regime_trend", ("regime_trend",)),
        ("regime_range", ("regime_range",)),
        ("daily_aligned", ("daily_aligned",)),
        ("daily_opposed", ("daily_opposed",)),
        ("daily_neutral", ("daily_neutral",)),
        ("daily_large", ("daily_large",)),
        ("vwap_aligned+prior_rejection", ("vwap_aligned", "prior_rejection")),
        ("vwap_reclaim+prior_rejection", ("vwap_reclaim", "prior_rejection")),
        ("vwap_aligned+prior_breakout", ("vwap_aligned", "prior_breakout")),
        ("vwap_aligned+regime_trend", ("vwap_aligned", "regime_trend")),
        ("vwap_reclaim+regime_range", ("vwap_reclaim", "regime_range")),
        ("prior_rejection+regime_trend", ("prior_rejection", "regime_trend")),
        ("prior_rejection+regime_range", ("prior_rejection", "regime_range")),
        ("prior_breakout+regime_trend", ("prior_breakout", "regime_trend")),
        ("daily_aligned+regime_trend", ("daily_aligned", "regime_trend")),
        ("daily_opposed+regime_range", ("daily_opposed", "regime_range")),
        ("daily_aligned+vwap_aligned", ("daily_aligned", "vwap_aligned")),
        ("daily_opposed+vwap_reclaim", ("daily_opposed", "vwap_reclaim")),
        ("daily_aligned+prior_breakout", ("daily_aligned", "prior_breakout")),
        (
            "vwap_aligned+prior_breakout+regime_trend",
            ("vwap_aligned", "prior_breakout", "regime_trend"),
        ),
        (
            "vwap_reclaim+prior_rejection+regime_range",
            ("vwap_reclaim", "prior_rejection", "regime_range"),
        ),
        (
            "vwap_aligned+prior_rejection+regime_trend",
            ("vwap_aligned", "prior_rejection", "regime_trend"),
        ),
    ]
    return tuple(ContextSpec(name=name, gates=tuple(gates)) for name, gates in names)


def _bar_range(bar) -> float:
    return max(0.0, float(bar.high) - float(bar.low))


def _session_quality(session) -> bool:
    """Require one unique, consecutive 1m bar for every 09:30–15:59 minute."""

    minute_keys = [
        (ts.hour * 60 + ts.minute) - (9 * 60 + 30)
        for ts in session.et_times
    ]
    return (
        len(minute_keys) == 390
        and len(set(minute_keys)) == 390
        and sorted(minute_keys) == list(range(390))
    )


def _calendar_event_dates(session_dates: Sequence[date]) -> dict[str, set[date]]:
    """Return fixed calendar OPEX-day and OPEX-week proxy dates.

    The third Friday is used when it is a session.  If it is a holiday, the
    last observed session in Monday–Friday of that standard week is used as
    the day proxy.  This is intentionally a date-only calendar classification;
    it does not claim to reproduce the exact expiration schedule of every
    futures option series.
    """

    dates = sorted(set(session_dates))
    by_month: dict[tuple[int, int], set[date]] = defaultdict(set)
    for value in dates:
        by_month[(value.year, value.month)].add(value)
    opex_day: set[date] = set()
    opex_week: set[date] = set()
    for (year, month), month_sessions in sorted(by_month.items()):
        fridays = [
            date(year, month, day)
            for week in calendar.monthcalendar(year, month)
            if (day := week[calendar.FRIDAY])
        ]
        if len(fridays) < 3:
            continue
        third_friday = fridays[2]
        week_start = third_friday - timedelta(days=4)
        week_sessions = {
            value for value in month_sessions if week_start <= value <= third_friday
        }
        if not week_sessions:
            continue
        opex_day.add(third_friday if third_friday in week_sessions else max(week_sessions))
        opex_week.update(week_sessions)
    return {"opex_day": opex_day, "opex_week": opex_week}


def _previous_day_state(sessions, index: int) -> tuple[int, Optional[float], Optional[float], bool]:
    if index <= 0:
        return 0, None, None, False
    previous = sessions[index - 1]
    opening = float(previous.bars[0].open)
    closing = float(previous.bars[-1].close)
    high = max(float(bar.high) for bar in previous.bars)
    low = min(float(bar.low) for bar in previous.bars)
    price_range = max(0.0, high - low)
    body_fraction = abs(closing - opening) / price_range if price_range else 0.0
    direction = 1 if closing > opening else -1 if closing < opening else 0

    prior_ranges = []
    for row in sessions[max(0, index - 21):index]:
        prior_ranges.append(
            max(float(bar.high) for bar in row.bars)
            - min(float(bar.low) for bar in row.bars)
        )
    prior_ranges = prior_ranges[:-1] if prior_ranges else []
    median_range = sorted(prior_ranges)[len(prior_ranges) // 2] if prior_ranges else None
    large = bool(median_range and len(prior_ranges) >= 10 and price_range >= 1.25 * median_range)
    return direction, body_fraction, price_range, large


def _build_signal_contexts(session, previous_session, *, tick: float, daily_state) -> dict[int, SignalContext]:
    """Build only information known at each completed 5m signal bar."""

    signal_bars = session.signal_bars(SIGNAL_MINUTES)
    closes = [float(row.close) for row in signal_bars]
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    atr14 = _atr(signal_bars, 14)
    prior_high = prior_low = None
    if previous_session is not None:
        prior_high = max(float(bar.high) for bar in previous_session.bars)
        prior_low = min(float(bar.low) for bar in previous_session.bars)
    daily_direction, body_fraction, _, daily_large = daily_state

    result: dict[int, SignalContext] = {}
    for pos, current in enumerate(signal_bars):
        long_gates: set[str] = set()
        short_gates: set[str] = set()
        current_close = float(current.close)
        current_vwap = float(current.vwap)

        if current_close > current_vwap + tick:
            long_gates.add("vwap_aligned")
        if current_close < current_vwap - tick:
            short_gates.add("vwap_aligned")

        if pos > 0:
            previous = signal_bars[pos - 1]
            long_reclaim = (
                float(previous.close) <= float(previous.vwap) + tick
                and float(current.low) <= current_vwap + tick
                and current_close > current_vwap + tick
            )
            short_reclaim = (
                float(previous.close) >= float(previous.vwap) - tick
                and float(current.high) >= current_vwap - tick
                and current_close < current_vwap - tick
            )
            if long_reclaim:
                long_gates.add("vwap_reclaim")
            if short_reclaim:
                short_gates.add("vwap_reclaim")

        if float(current.vwap_std) > tick:
            if current_close <= current_vwap - float(current.vwap_std):
                long_gates.add("vwap_deviation")
            if current_close >= current_vwap + float(current.vwap_std):
                short_gates.add("vwap_deviation")

        if prior_high is not None and prior_low is not None:
            tolerance = 4.0 * tick
            if (
                float(current.low) <= prior_low + tolerance
                and float(current.low) >= prior_low - 2.0 * tolerance
                and current_close > prior_low
            ):
                long_gates.add("prior_rejection")
            if (
                float(current.high) >= prior_high - tolerance
                and float(current.high) <= prior_high + 2.0 * tolerance
                and current_close < prior_high
            ):
                short_gates.add("prior_rejection")
            if current_close > prior_high + tick:
                long_gates.add("prior_breakout")
            if current_close < prior_low - tick:
                short_gates.add("prior_breakout")

        slope_ticks: Optional[float] = None
        regime = "unknown"
        if pos >= 6 and atr14[pos] is not None:
            slope = current_vwap - float(signal_bars[pos - 6].vwap)
            slope_ticks = slope / tick if tick else None
            atr = float(atr14[pos])
            last_rows = signal_bars[max(0, pos - 2):pos + 1]
            trend_long = (
                ema20[pos] > ema50[pos]
                and slope >= 0.10 * atr
                and all(float(row.close) > float(row.vwap) for row in last_rows)
            )
            trend_short = (
                ema20[pos] < ema50[pos]
                and slope <= -0.10 * atr
                and all(float(row.close) < float(row.vwap) for row in last_rows)
            )
            balanced_range = (
                abs(slope) <= 0.15 * atr
                and float(current.vwap_std) > 0
                and abs(current_close - current_vwap) <= 0.75 * float(current.vwap_std)
            )
            if trend_long:
                long_gates.add("regime_trend")
            if trend_short:
                short_gates.add("regime_trend")
            if balanced_range:
                long_gates.add("regime_range")
                short_gates.add("regime_range")
            if trend_long or trend_short:
                regime = "trend"
            elif balanced_range:
                regime = "range"
            else:
                regime = "transition"

        if daily_direction > 0:
            long_gates.add("daily_aligned")
            short_gates.add("daily_opposed")
        elif daily_direction < 0:
            short_gates.add("daily_aligned")
            long_gates.add("daily_opposed")
        if body_fraction is not None and body_fraction <= 0.25:
            long_gates.add("daily_neutral")
            short_gates.add("daily_neutral")
        if daily_large:
            long_gates.add("daily_large")
            short_gates.add("daily_large")

        result[signal_bars[pos].end_index] = SignalContext(
            long_gates=frozenset(long_gates),
            short_gates=frozenset(short_gates),
            vwap_slope_ticks=slope_ticks,
            regime=regime,
        )
    return result


def build_session_metadata(sessions, *, tick: float) -> tuple[list[SessionMeta], dict[str, int]]:
    dates = [session.session_date for session in sessions]
    events = _calendar_event_dates(dates)
    metadata: list[SessionMeta] = []
    for index, session in enumerate(sessions):
        previous = sessions[index - 1] if index else None
        daily_state = _previous_day_state(sessions, index)
        metadata.append(
            SessionMeta(
                session_date=session.session_date,
                quality_ok=_session_quality(session),
                opex_day=session.session_date in events["opex_day"],
                opex_week=session.session_date in events["opex_week"],
                previous_day_direction=daily_state[0],
                previous_day_body_fraction=daily_state[1],
                previous_day_range=daily_state[2],
                previous_day_large=daily_state[3],
                signal_contexts=_build_signal_contexts(
                    session,
                    previous,
                    tick=tick,
                    daily_state=daily_state,
                ),
            )
        )
    return metadata, {
        "sessions": len(sessions),
        "quality_sessions": sum(item.quality_ok for item in metadata),
        "opex_days": sum(item.opex_day for item in metadata),
        "opex_week_sessions": sum(item.opex_week for item in metadata),
    }


def _day_filter_passes(name: str, meta: SessionMeta) -> bool:
    if name == "all":
        return True
    quality = meta.quality_ok
    day = not meta.opex_day
    week = not meta.opex_week
    if name == "quality_only":
        return quality
    if name == "exclude_opex_day":
        return day
    if name == "exclude_opex_week":
        return week
    if name == "quality_exclude_opex_day":
        return quality and day
    if name == "quality_exclude_opex_week":
        return quality and week
    raise ValueError(f"unknown day filter: {name}")


def _context_passes(spec: ContextSpec, meta: SessionMeta, signal: SignalSpec, direction: int) -> bool:
    context = meta.signal_contexts.get(signal.signal_index)
    if context is None:
        return False
    gates = context.long_gates if direction > 0 else context.short_gates
    return all(gate in gates for gate in spec.gates)


def _simple_summary(trades, *, symbol: str) -> dict:
    pnls = [float(trade.pnl) for trade in trades]
    stats = series_stats(pnls)
    stress = slip_injection(
        [trade.robustness_row() for trade in trades],
        levels=(SLIPPAGE_STRESS_TICKS,),
        symbol=symbol,
    )
    stress_stats = stress["levels"][0]["stats"]
    by_day: dict[str, float] = defaultdict(float)
    by_year: dict[str, list[float]] = defaultdict(list)
    for trade in trades:
        entry = _as_et(trade.entry_time)
        by_day[entry.date().isoformat()] += float(trade.pnl)
        by_year[str(entry.year)].append(float(trade.pnl))
    planned = [float(trade.planned_risk_dollars) for trade in trades if trade.planned_risk_dollars > 0]
    avg_net_r = (
        sum(float(trade.pnl) / float(trade.planned_risk_dollars) for trade in trades if trade.planned_risk_dollars > 0)
        / len(planned)
        if planned
        else 0.0
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
        "slip14_pnl": round(float(stress_stats["pnl"]), 2),
        "slip14_pf": round(float(stress_stats["pf"]), 4),
        "trading_days": len(by_day),
        "positive_days": sum(value > 0 for value in by_day.values()),
        "positive_years": sum(series_stats(values)["pnl"] > 0 for values in by_year.values()),
        "years": len(by_year),
    }


def _split_trades(trades: Sequence, name: str) -> list:
    out = []
    for trade in trades:
        day = _as_et(trade.entry_time).date()
        if name == "discovery" and day <= DISCOVERY_END:
            out.append(trade)
        elif name == "validation" and VALIDATION_START <= day <= VALIDATION_END:
            out.append(trade)
        elif name == "holdout" and day >= HOLDOUT_START:
            out.append(trade)
    return out


def _screen_verdict(full: dict, validation: dict, holdout: dict) -> str:
    if full["trades"] < 40 or validation["trades"] < 15 or holdout["trades"] < 10:
        return "THIN_OOS"
    all_positive = all(
        block["pnl"] > 0 and block["pf"] > 1.0
        for block in (full, validation, holdout)
    )
    if all_positive and full["slip14_pf"] > 1.0 and holdout["slip14_pf"] > 1.0:
        return "OOS_SCREEN_LEAD"
    return "REJECT"


def _flat_metrics(prefix: str, summary: dict) -> dict[str, object]:
    return {f"{prefix}_{key}": value for key, value in summary.items()}


def _variant_key(row: dict) -> tuple[str, str, str, float, str, str]:
    return (
        str(row["symbol"]),
        str(row["setup"]),
        str(row["side"]),
        float(row["target_r"]),
        str(row["context"]),
        str(row["day_filter"]),
    )


def _run_variant(
    symbol: str,
    sessions,
    metadata: Sequence[SessionMeta],
    specs_by_key: dict[tuple[str, int], list[list[SignalSpec]]],
    *,
    setup: str,
    direction: int,
    target_r: float,
    context: ContextSpec,
    day_filter: str,
    tick: float,
) -> tuple[list, int, int]:
    all_trades = []
    selected_signals = 0
    used_sessions = 0
    per_session = specs_by_key[(setup, direction)]
    for session, meta, specs in zip(sessions, metadata, per_session):
        if not _day_filter_passes(day_filter, meta):
            continue
        selected = [
            spec for spec in specs
            if _context_passes(context, meta, spec, direction)
        ]
        selected_signals += len(selected)
        if not selected:
            continue
        used_sessions += 1
        candidates = [
            _candidate(setup, direction, spec, target_r, tick)
            for spec in selected
        ]
        trades, _ = simulate_candidates(
            session,
            candidates,
            symbol,
            max_trades_per_day=MAX_TRADES_PER_DAY,
        )
        all_trades.extend(trades)
    return all_trades, selected_signals, used_sessions


def _row(
    symbol: str,
    setup: str,
    direction: int,
    target_r: float,
    context: ContextSpec,
    day_filter: str,
    sessions_count: int,
    selected_signals: int,
    used_sessions: int,
    trades,
) -> dict:
    full = _simple_summary(trades, symbol=symbol)
    discovery = _simple_summary(_split_trades(trades, "discovery"), symbol=symbol)
    validation = _simple_summary(_split_trades(trades, "validation"), symbol=symbol)
    holdout = _simple_summary(_split_trades(trades, "holdout"), symbol=symbol)
    row = {
        "symbol": symbol,
        "setup": setup,
        "side": "long" if direction > 0 else "short",
        "target_r": target_r,
        "context": context.name,
        "day_filter": day_filter,
        "sessions": sessions_count,
        "used_sessions": used_sessions,
        "signals": selected_signals,
        "trades_per_session": round(len(trades) / sessions_count, 4) if sessions_count else 0.0,
        "context_gates": "+".join(context.gates) or "none",
    }
    row.update(_flat_metrics("full", full))
    row.update(_flat_metrics("discovery", discovery))
    row.update(_flat_metrics("validation", validation))
    row.update(_flat_metrics("holdout", holdout))
    row["screen_verdict"] = _screen_verdict(full, validation, holdout)
    return row


def _load_baseline_csv(symbol: str) -> dict[tuple[str, str, float], dict]:
    path = market_data.derived_path("research", "price_action_study_current.csv")
    if not path.exists():
        return {}
    result = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            if str(raw.get("symbol", "")).upper() != symbol:
                continue
            result[(str(raw["setup"]), str(raw["side"]), float(raw["target_r"]))] = raw
    return result


def _baseline_check(rows: Sequence[dict], *, symbol: str) -> dict:
    expected = _load_baseline_csv(symbol)
    generated = {
        (str(row["setup"]), str(row["side"]), float(row["target_r"])): row
        for row in rows
        if row["context"] == "baseline" and row["day_filter"] == "all"
    }
    if not expected:
        return {"status": "unavailable", "reason": "price_action_study_current.csv not found"}
    mismatches = []
    for key, source in sorted(expected.items()):
        actual = generated.get(key)
        if actual is None:
            mismatches.append({"key": key, "reason": "missing_generated_variant"})
            continue
        checks = (
            ("trades", int(source["trades"]), int(actual["full_trades"])),
            ("pnl", float(source["pnl"]), float(actual["full_pnl"])),
            ("pf", float(source["pf"]), float(actual["full_pf"])),
            ("slip14_pf", float(source["slip14_pf"]), float(actual["full_slip14_pf"])),
        )
        for field, expected_value, actual_value in checks:
            tolerance = 0 if field == "trades" else 1e-3 if field.endswith("pf") else 0.01
            if abs(expected_value - actual_value) > tolerance:
                mismatches.append({
                    "key": key,
                    "field": field,
                    "expected": expected_value,
                    "actual": actual_value,
                })
    return {
        "status": "pass" if not mismatches else "fail",
        "source": str(market_data.derived_path("research", "price_action_study_current.csv")),
        "checked": len(expected),
        "mismatches": mismatches[:20],
        "mismatch_count": len(mismatches),
    }


def _shortlist(rows: Sequence[dict]) -> list[dict]:
    """Select on validation only; holdout fields are never used here."""

    selected: dict[tuple, dict] = {}
    for symbol in sorted({str(row["symbol"]) for row in rows}):
        pool = [
            row for row in rows
            if str(row["symbol"]) == symbol
            and int(row["validation_trades"]) >= 10
            and int(row["discovery_trades"]) >= 20
        ]
        for key_row in sorted(
            pool,
            key=lambda row: (
                float(row["validation_pnl"]),
                float(row["validation_pf"]),
                -float(row["validation_max_dd"]),
            ),
            reverse=True,
        )[:20]:
            selected[_variant_key(key_row)] = key_row
        for key_row in sorted(
            pool,
            key=lambda row: (
                float(row["validation_pf"]),
                float(row["validation_pnl"]),
                -float(row["validation_max_dd"]),
            ),
            reverse=True,
        )[:20]:
            selected[_variant_key(key_row)] = key_row
    return list(selected.values())


def _robustness_summary(trades, *, symbol: str, mc_iters: int) -> dict:
    result = evaluate(
        [trade.robustness_row() for trade in trades],
        iters=mc_iters,
        seed=MC_SEED,
        dd_threshold=MC_DD_THRESHOLD,
        slip_levels=(1, 2, 4, 8, SLIPPAGE_STRESS_TICKS),
    )
    mc = result.get("monte_carlo") or {}
    wf = result.get("walk_forward") or {}
    stress = next(
        item["stats"] for item in result["slip"]["levels"]
        if item["level"] == SLIPPAGE_STRESS_TICKS
    )
    return {
        "robust_trades": len(trades),
        "robust_pf": result["stats"]["pf"],
        "robust_pnl": result["stats"]["pnl"],
        "robust_slip14_pf": stress["pf"],
        "mc_p_loss": mc.get("p_loss"),
        "mc_dd_p95": mc.get("dd_p95"),
        "mc_pf_p5": mc.get("pf_p5"),
        "mc_pass": bool(result.get("monte_carlo_pass")),
        "wf_pass": bool(wf.get("pass")),
        "wf_pf": [segment.get("pf") for segment in wf.get("segments", [])],
        "wf_pnl": [segment.get("pnl") for segment in wf.get("segments", [])],
    }


def run_symbol(symbol: str, *, mc_iters: int) -> tuple[dict, list[dict], dict]:
    started = time.time()
    sessions, info = load_symbol_sessions(symbol)
    tick = get_tick_size(current_quarterly_contract_id(symbol))
    metadata, coverage = build_session_metadata(sessions, tick=tick)
    contexts = context_catalog()
    print(
        f"[{symbol}] {info.total_bars:,} bars | {len(sessions):,} full RTH sessions | "
        f"quality={coverage['quality_sessions']:,} | "
        f"OPEX day/week={coverage['opex_days']}/{coverage['opex_week_sessions']} | tick={tick:g}"
    )

    specs_by_key: dict[tuple[str, int], list[list[SignalSpec]]] = {}
    for setup in SETUPS:
        for direction in SIDES:
            per_session = []
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
    total = len(contexts) * len(DAY_FILTERS) * len(SETUPS) * len(SIDES)
    completed = 0
    for context in contexts:
        for day_filter in DAY_FILTERS:
            for setup in SETUPS:
                for direction in SIDES:
                    trades, selected_signals, used_sessions = _run_variant(
                        symbol,
                        sessions,
                        metadata,
                        specs_by_key,
                        setup=setup,
                        direction=direction,
                        target_r=SCREEN_TARGET_R,
                        context=context,
                        day_filter=day_filter,
                        tick=tick,
                    )
                    rows.append(
                        _row(
                            symbol,
                            setup,
                            direction,
                            SCREEN_TARGET_R,
                            context,
                            day_filter,
                            len(sessions),
                            selected_signals,
                            used_sessions,
                            trades,
                        )
                    )
                    completed += 1
            if completed % max(1, total // 10) == 0:
                print(f"  screen {completed}/{total}")

    # Re-run the full no-context target grid first.  This is the reproducibility
    # gate: context comparisons must not proceed if the known baseline changed.
    baseline_grid: list[dict] = []
    for setup in SETUPS:
        for direction in SIDES:
            for target_r in TARGET_R:
                trades, selected_signals, used_sessions = _run_variant(
                    symbol,
                    sessions,
                    metadata,
                    specs_by_key,
                    setup=setup,
                    direction=direction,
                    target_r=float(target_r),
                    context=contexts[0],
                    day_filter="all",
                    tick=tick,
                )
                baseline_grid.append(
                    _row(
                        symbol,
                        setup,
                        direction,
                        float(target_r),
                        contexts[0],
                        "all",
                        len(sessions),
                        selected_signals,
                        used_sessions,
                        trades,
                    )
                )

    # Only validation-ranked context/day combinations receive the target grid,
    # but any OOS screen lead is also audited at 2R below so a promising
    # holdout row cannot disappear merely because it ranked outside the fixed
    # validation shortlist.
    screen_shortlist = _shortlist(rows)
    robustness_shortlist = list(screen_shortlist)
    known_robust_keys = {_variant_key(row) for row in robustness_shortlist}
    for row in rows:
        if row["screen_verdict"] != "OOS_SCREEN_LEAD":
            continue
        key = _variant_key(row)
        if key not in known_robust_keys:
            robustness_shortlist.append(row)
            known_robust_keys.add(key)
    target_rows: list[dict] = []
    robust: dict[str, dict] = {}
    for base_row in robustness_shortlist:
        setup = str(base_row["setup"])
        direction = 1 if str(base_row["side"]) == "long" else -1
        context = next(item for item in contexts if item.name == base_row["context"])
        day_filter = str(base_row["day_filter"])
        for target_r in TARGET_R:
            trades, selected_signals, used_sessions = _run_variant(
                symbol,
                sessions,
                metadata,
                specs_by_key,
                setup=setup,
                direction=direction,
                target_r=float(target_r),
                context=context,
                day_filter=day_filter,
                tick=tick,
            )
            row = _row(
                symbol,
                setup,
                direction,
                float(target_r),
                context,
                day_filter,
                len(sessions),
                selected_signals,
                used_sessions,
                trades,
            )
            row["screen_selected_from_target"] = SCREEN_TARGET_R
            target_rows.append(row)
            if float(target_r) == SCREEN_TARGET_R:
                robust[_row_key_string(row)] = _robustness_summary(
                    trades,
                    symbol=symbol,
                    mc_iters=mc_iters,
                )

    # A selected 2R row exists in both the screen and target-grid passes.  Keep
    # one row per complete variant key so CSV consumers do not see a false
    # duplicate; the baseline check still uses its independent full grid.
    by_key: dict[tuple, dict] = {}
    for row in rows:
        by_key[_variant_key(row)] = row
    for row in baseline_grid:
        by_key.setdefault(_variant_key(row), row)
    for row in target_rows:
        by_key[_variant_key(row)] = row
    all_rows = list(by_key.values())
    baseline = _baseline_check(baseline_grid, symbol=symbol)
    payload = {
        "symbol": symbol,
        "elapsed_seconds": time.time() - started,
        "dataset": {
            "info": info.__dict__,
            "coverage": coverage,
            "tick": tick,
        },
        "study": {
            "signal_timeframe_minutes": SIGNAL_MINUTES,
            "source_timeframe_minutes": 1,
            "screen_target_r": SCREEN_TARGET_R,
            "target_grid": list(TARGET_R),
            "risk_dollars": RISK_DOLLARS,
            "max_trades_per_day": MAX_TRADES_PER_DAY,
            "stop_buffer_ticks": STOP_BUFFER_TICKS,
            "stress_slippage_ticks_round_trip": SLIPPAGE_STRESS_TICKS,
            "entry": "next available 1m open after completed 5m signal",
            "one_position_at_a_time": True,
            "context_count": len(contexts),
            "day_filters": list(DAY_FILTERS),
            "date_split": {
                "discovery_end": DISCOVERY_END.isoformat(),
                "validation": [VALIDATION_START.isoformat(), VALIDATION_END.isoformat()],
                "holdout_start": HOLDOUT_START.isoformat(),
            },
            "definitions": {
                "vwap_aligned": "completed 5m close > RTH cumulative VWAP + 1 tick for long; inverse for short",
                "vwap_reclaim": "prior completed 5m close is on/through the opposite VWAP side, current range touches VWAP and closes one tick beyond it",
                "vwap_deviation": "completed 5m close is at least one causal RTH VWAP standard deviation away, in the candidate direction",
                "prior_rejection": "current completed 5m range reaches the previous complete RTH high/low within a four-tick tolerance and closes back inside",
                "prior_breakout": "current completed 5m close accepts beyond the previous complete RTH high/low by one tick",
                "regime_trend": "EMA20/EMA50 and the last 3 closes agree with a 30-minute VWAP slope of at least 0.10 current 5m ATR",
                "regime_range": "30-minute VWAP slope is within 0.15 current 5m ATR and close is within 0.75 cumulative VWAP standard deviation",
                "daily_aligned": "candidate side agrees with the previous complete RTH candle close versus open",
                "daily_opposed": "candidate side is opposite the previous complete RTH candle close versus open",
                "daily_neutral": "previous complete RTH candle body is <=25% of its high-low range",
                "daily_large": "previous complete RTH range is >=1.25x the median of at least 10 earlier complete RTH ranges",
                "quality_only": "exactly 390 unique consecutive RTH one-minute timestamps; no performance-based day deletion",
                "opex_day": "third Friday calendar proxy; if holiday, last observed session in Mon-Fri expiration week",
                "opex_week": "Monday through Friday of the third-Friday calendar week",
            },
        },
        "baseline_check": baseline,
        "shortlist_size": len(screen_shortlist),
        "robustness_audit_size": len(robustness_shortlist),
        "robustness": robust,
    }
    return payload, all_rows, {
        "sessions": sessions,
        "metadata": metadata,
        "specs_by_key": specs_by_key,
        "contexts": contexts,
        "shortlist": screen_shortlist,
    }


def _row_key_string(row: dict) -> str:
    return "|".join(
        (
            str(row["symbol"]),
            str(row["setup"]),
            str(row["side"]),
            f"{float(row['target_r']):g}R",
            str(row["context"]),
            str(row["day_filter"]),
        )
    )


def _write_csv(path: Path, rows: Sequence[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    if not fields:
        fields = ["symbol"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: row.get(key, "") for key in fields} for row in rows)


def _fmt(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _markdown(payload: dict, rows: Sequence[dict]) -> str:
    lines = [
        "# Price-action context research: VWAP, prior levels, regimes, and calendar filters",
        "",
        "Status: **offline exploratory research only; no production strategy changed**",
        f"Generated: `{payload['created_at']}`",
        "",
        "The screen covers every frozen PA setup from the baseline study, all "
        f"{len(context_catalog())} context definitions, and all {len(DAY_FILTERS)} "
        "data/OPEX filters. The screen uses 2R; target-grid and robustness results "
        "are rerun only for combinations selected on 2024–2025 validation data.",
        "",
        "## Coverage and baseline reproducibility",
        "",
        "| Symbol | Full RTH sessions | Exact-quality sessions | OPEX day | OPEX-week sessions | Baseline check |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for symbol, item in sorted(payload["datasets"].items()):
        coverage = item["coverage"]
        check = payload["baseline_checks"].get(symbol, {})
        lines.append(
            f"| {symbol} | {coverage['sessions']} | {coverage['quality_sessions']} | "
            f"{coverage['opex_days']} | {coverage['opex_week_sessions']} | "
            f"{check.get('status', '-')} ({check.get('checked', 0)}) |"
        )
    lines += [
        "",
        "The baseline check compares the full no-context/all-days 1R/1.5R/2R "
        "target grid with the existing `price_action_study_current.csv` on trade "
        "count, net P&L, PF, and 14-tick stress PF before interpreting any context result.",
        "",
        "## Validation-selected rows and untouched 2026 holdout",
        "",
        "| Symbol | Setup | Side | Context | Day filter | Val n | Val PF | Val PnL | Holdout n | Holdout PF | Holdout PnL | Stress PF | Screen |",
        "|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    shortlist = sorted(
        [row for row in rows if row.get("screen_selected_from_target") == SCREEN_TARGET_R],
        key=lambda row: (
            str(row["symbol"]),
            -float(row.get("validation_pnl", 0)),
            -float(row.get("validation_pf", 0)),
        ),
    )
    for row in shortlist[:40]:
        lines.append(
            f"| {row['symbol']} | {row['setup']} | {row['side']} | {row['context']} | "
            f"{row['day_filter']} | {row['validation_trades']} | {row['validation_pf']} | "
            f"${row['validation_pnl']} | {row['holdout_trades']} | {row['holdout_pf']} | "
            f"${row['holdout_pnl']} | {row['holdout_slip14_pf']} | {row['screen_verdict']} |"
        )
    if not shortlist:
        lines.append("| - | - | - | - | - | 0 | - | - | 0 | - | - | - | - |")
    lines += [
        "",
        "`OOS_SCREEN_LEAD` is only a screening label: it does not mean live-ready. "
        "Robustness fields are stored in the JSON for the validation-selected "
        "2R rows plus every OOS screen lead; the holdout was not used to select "
        "the main shortlist.",
        "",
        "## Interpretation rules",
        "",
        "- A VWAP gate is a location/trend condition; it is not a signal by itself.",
        "- Prior high/low rejection is a failed auction; prior breakout is acceptance. They are tested separately because their exits and regimes differ.",
        "- Regime is causal and intraday: no final daily close or later session data enters a signal.",
        "- Daily-candle states use only the previous complete RTH candle. `quality_only` removes missing/duplicate-minute sessions by timestamp structure, never because the P&L was bad.",
        "- OPEX labels are calendar proxies. A good/bad OPEX result is not proof of an options-flow mechanism.",
        "- All matrix rows are retained in the CSV. The large number of comparisons is itself a multiple-testing risk; no single best row is promoted.",
        "",
        "## Reproducible artifacts",
        "",
        f"- Full scalar matrix: `{payload['outputs']['csv']}`",
        f"- Full JSON plus robustness shortlist: `{payload['outputs']['json']}`",
        f"- This report: `{payload['outputs']['markdown']}`",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default=["MNQ", "MES"])
    parser.add_argument("--mc-iters", type=int, default=1000)
    parser.add_argument(
        "--output",
        default=str(market_data.derived_path("research", "price_action_context_study_current.json")),
    )
    args = parser.parse_args()
    symbols = [str(value).upper() for value in args.symbols]
    if not symbols or any(value not in {"MNQ", "MES"} for value in symbols):
        parser.error("--symbols supports MNQ and MES")
    if args.mc_iters <= 0:
        parser.error("--mc-iters must be positive")

    started = time.time()
    all_rows: list[dict] = []
    symbol_payloads: dict[str, dict] = {}
    baseline_checks: dict[str, dict] = {}
    for symbol in symbols:
        payload, rows, _ = run_symbol(symbol, mc_iters=args.mc_iters)
        all_rows.extend(rows)
        symbol_payloads[symbol] = payload
        baseline_checks[symbol] = payload["baseline_check"]
        if payload["baseline_check"].get("status") == "fail":
            raise RuntimeError(f"baseline reproduction failed for {symbol}: {payload['baseline_check']}")

    output = Path(args.output)
    if not output.is_absolute():
        output = (ROOT / output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    csv_path = output.with_suffix(".csv")
    markdown_path = output.with_suffix(".md")
    aggregate = {
        "created_at": datetime.now().astimezone().isoformat(),
        "elapsed_seconds": time.time() - started,
        "symbols": symbols,
        "study": symbol_payloads[symbols[0]]["study"] if symbol_payloads else {},
        "datasets": {symbol: payload["dataset"] for symbol, payload in symbol_payloads.items()},
        "baseline_checks": baseline_checks,
        "results": all_rows,
        "robustness": {
            symbol: payload["robustness"] for symbol, payload in symbol_payloads.items()
        },
        "shortlist_size": {
            symbol: payload["shortlist_size"] for symbol, payload in symbol_payloads.items()
        },
        "outputs": {
            "json": str(output),
            "csv": str(csv_path),
            "markdown": str(markdown_path),
        },
        "notes": [
            "The full screen is fixed at 2R; target grid is validation-selected only.",
            "The current 2026 partial year is holdout and is not used for selecting the shortlist.",
            "This is evidence about the frozen OHLC implementation, not a claim that discretionary order-flow PA is impossible.",
        ],
    }
    output.write_text(json.dumps(aggregate, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    _write_csv(csv_path, all_rows)
    markdown_path.write_text(_markdown(aggregate, all_rows), encoding="utf-8")
    print(f"\nJSON: {output}")
    print(f"CSV : {csv_path}")
    print(f"MD  : {markdown_path}")
    print(f"Rows: {len(all_rows):,}")
    print(f"Elapsed: {time.time() - started:.1f}s")
    print("\nOOS screen leads:")
    for row in all_rows:
        if row["screen_verdict"] == "OOS_SCREEN_LEAD":
            print(
                f"  {row['symbol']} {row['setup']} {row['side']} {row['context']} "
                f"{row['day_filter']} valPF={row['validation_pf']:.2f} "
                f"holdoutPF={row['holdout_pf']:.2f} stress={row['holdout_slip14_pf']:.2f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
