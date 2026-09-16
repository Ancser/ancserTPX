"""Study price action with Databento MBO confirmation.

This is an offline, research-only runner.  It joins the existing completed
5-minute price-action signal library to the already settled Databento MBO
compact cache at the signal minute.  It does not download data, register a
model, change a preset, or change live behaviour.

The join is deliberately causal:

* a PA signal is formed from a completed 5-minute bar;
* the order-flow window is the same five completed one-minute bars;
* the trade is filled at the next available one-minute open; and
* no final-day high/low or future MBO minute is used by a gate.

Databento's raw MBO DBN is parsed by the existing order-flow pipeline.  This
runner consumes its schema-v5 one-minute derivative so the research code does
not duplicate the MBO book-state parser.  A real API download remains a
separate, quote-first operation in ``databento_orderflow_download.py`` and
``databento_orderflow_settlement.py``.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.backtest.prop_intraday_research import (  # noqa: E402
    _as_et,
    load_symbol_sessions,
    simulate_candidates,
)
from backend.backtest.robustness import evaluate, series_stats, slip_injection  # noqa: E402
from backend.data import market_data  # noqa: E402
from backend.db.models import current_quarterly_contract_id, get_tick_size  # noqa: E402
from scripts.orderflow_context_combination_study import _prepare_days  # noqa: E402
from scripts.price_action_study import (  # noqa: E402
    SETUPS,
    SIDES,
    SignalSpec,
    _candidate,
    _generate_specs,
)


UTC = timezone.utc
SYMBOL = "MNQ"
SIGNAL_MINUTES = 5
TARGET_R = 2.0
RISK_DOLLARS = 200.0
MAX_TRADES_PER_DAY = 2
SLIPPAGE_STRESS_TICKS = 14
MC_SEED = 20260913

# The MBO cache is a short recent window.  The earlier dates are used only to
# select an audit shortlist; dates after the cutoff are reported as a small
# retrospective evaluation, never as an untouched validation set.
DISCOVERY_END = date(2026, 8, 27)
ROBUSTNESS_LIMIT = 20
DISCOVERY_MIN_TRADES = 5


@dataclass(frozen=True)
class FlowContext:
    """A frozen order-flow gate or a deliberately small combination of gates."""

    name: str
    gates: tuple[str, ...]


def flow_context_catalog() -> tuple[FlowContext, ...]:
    """Return the pre-declared screen; do not expand combinations afterward."""

    return (
        FlowContext("baseline", ()),
        FlowContext("mbo_delta", ("mbo_delta_aligned",)),
        FlowContext("mbo_cvd", ("mbo_cvd_aligned",)),
        FlowContext("mbo_volume_burst", ("mbo_volume_burst",)),
        FlowContext("mbo_imbalance", ("mbo_imbalance_aligned",)),
        FlowContext("mbo_passive_rejection", ("mbo_passive_rejection",)),
        FlowContext("mbo_queue", ("mbo_queue_aligned",)),
        FlowContext("mbo_ofi", ("mbo_ofi_aligned",)),
        FlowContext("mbo_large_trade", ("mbo_large_trade_aligned",)),
        FlowContext("pa_vwap+mbo_delta", ("pa_vwap_aligned", "mbo_delta_aligned")),
        FlowContext("pa_vwap+mbo_cvd", ("pa_vwap_aligned", "mbo_cvd_aligned")),
        FlowContext("pa_vwap+mbo_imbalance", ("pa_vwap_aligned", "mbo_imbalance_aligned")),
        FlowContext("mbo_delta+mbo_cvd", ("mbo_delta_aligned", "mbo_cvd_aligned")),
        FlowContext("mbo_delta+mbo_imbalance", ("mbo_delta_aligned", "mbo_imbalance_aligned")),
        FlowContext("mbo_cvd+mbo_imbalance", ("mbo_cvd_aligned", "mbo_imbalance_aligned")),
        FlowContext(
            "mbo_delta+mbo_cvd+mbo_volume",
            ("mbo_delta_aligned", "mbo_cvd_aligned", "mbo_volume_burst"),
        ),
        FlowContext("mbo_full_confirmation", ("mbo_full_confirmation",)),
    )


def _finite(values: Iterable[Any]) -> list[float]:
    result: list[float] = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            result.append(number)
    return result


def _median(values: Iterable[Any]) -> float | None:
    clean = _finite(values)
    return statistics.median(clean) if clean else None


def _side(value: Any) -> int:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0
    return 1 if number > 0 else -1 if number < 0 else 0


def _majority(values: Iterable[Any]) -> int:
    positive = negative = 0
    for value in values:
        number = _side(value)
        if number > 0:
            positive += 1
        elif number < 0:
            negative += 1
    if positive > negative:
        return 1
    if negative > positive:
        return -1
    return 0


def _volume(bar: dict[str, Any]) -> int:
    value = bar.get("volume")
    if value is not None:
        return max(0, int(value or 0))
    return max(0, int(bar.get("buy") or 0) + int(bar.get("sell") or 0))


def _consecutive(bars: Sequence[dict[str, Any]]) -> bool:
    epochs = [int(bar.get("epoch") or 0) for bar in bars]
    return bool(epochs) and all(right - left == 60 for left, right in zip(epochs, epochs[1:]))


def _epoch_index(day: dict[str, Any]) -> dict[int, int]:
    indexed = day.get("_epoch_index")
    if isinstance(indexed, dict):
        return indexed
    indexed = {int(bar["epoch"]): index for index, bar in enumerate(day.get("bars") or [])}
    day["_epoch_index"] = indexed
    return indexed


def _flow_window(day: dict[str, Any], signal_epoch: int) -> tuple[list[dict[str, Any]], int] | None:
    """Return the completed five-minute MBO window ending at signal_epoch."""

    index = _epoch_index(day).get(int(signal_epoch))
    if index is None or index < SIGNAL_MINUTES - 1:
        return None
    bars = day.get("bars") or []
    start = index - SIGNAL_MINUTES + 1
    window = list(bars[start : index + 1])
    if len(window) != SIGNAL_MINUTES or not _consecutive(window):
        return None
    return window, index


def _pa_vwap_side(signal_bar: Any, tick: float) -> tuple[int, str]:
    vwap = getattr(signal_bar, "vwap", None)
    close = getattr(signal_bar, "close", None)
    if vwap is None or close is None:
        return 0, "unknown"
    if float(close) > float(vwap) + tick:
        return 1, "above"
    if float(close) < float(vwap) - tick:
        return -1, "below"
    return 0, "at"


def _prior_5m_volumes(bars: Sequence[dict[str, Any]], index: int) -> list[int]:
    """Build aligned prior five-minute totals without reading after index."""

    totals: list[int] = []
    for end in range(index - SIGNAL_MINUTES, max(-1, index - 35), -SIGNAL_MINUTES):
        start = end - SIGNAL_MINUTES + 1
        if start < 0:
            break
        block = list(bars[start : end + 1])
        if len(block) != SIGNAL_MINUTES or not _consecutive(block):
            break
        totals.append(sum(_volume(bar) for bar in block))
    return totals


def _flow_features(
    day: dict[str, Any],
    signal_epoch: int,
    signal_bar: Any,
    *,
    tick: float,
) -> dict[str, Any] | None:
    """Derive only signal-time MBO features for one PA signal."""

    located = _flow_window(day, int(signal_epoch))
    if located is None:
        return None
    window, index = located
    last = window[-1]
    volume_5m = sum(_volume(bar) for bar in window)
    delta_5m = sum(int(bar.get("delta") or 0) for bar in window)
    ofi_5m = sum(int(bar.get("ofi") or 0) for bar in window)
    buy_150 = sum(int(bar.get("buy_ge_150") or 0) for bar in window)
    sell_150 = sum(int(bar.get("sell_ge_150") or 0) for bar in window)
    previous_totals = _prior_5m_volumes(day["bars"], index)
    volume_baseline = _median(previous_totals)
    volume_ratio = volume_5m / volume_baseline if volume_baseline and volume_baseline > 0 else None

    queue_values = _finite(bar.get("queue_imbalance") for bar in window)
    queue_median = statistics.median(queue_values) if queue_values else 0.0
    if queue_median >= 0.15:
        queue_side = 1
    elif queue_median <= -0.15:
        queue_side = -1
    else:
        queue_side = 0

    pa_vwap_side, pa_vwap_state = _pa_vwap_side(signal_bar, tick)
    delta_side = _side(delta_5m)
    cvd_delta_side = _side(last.get("cvd_5_delta"))
    cvd_state_side = int(last.get("cvd_state_side") or 0)
    cvd_side = cvd_state_side if cvd_state_side == cvd_delta_side else 0
    imbalance_side = _majority(bar.get("imbalance_1_10_side") for bar in window)
    passive_side = _majority(bar.get("passive_rejection_side") for bar in window)
    ofi_side = _side(ofi_5m)
    large_trade_side = 1 if buy_150 > sell_150 else -1 if sell_150 > buy_150 else 0
    volume_burst = bool(volume_ratio is not None and volume_ratio >= 1.25)
    imbalance_aligned = imbalance_side
    passive_aligned = passive_side
    full_confirmation = bool(
        delta_side != 0
        and cvd_side == delta_side
        and volume_burst
        and (
            imbalance_aligned == delta_side
            or passive_aligned == delta_side
            or large_trade_side == delta_side
        )
    )
    return {
        "signal_epoch": int(signal_epoch),
        "window_start_epoch": int(window[0]["epoch"]),
        "window_end_epoch": int(window[-1]["epoch"]),
        "mbo_volume_5m": volume_5m,
        "mbo_delta_5m": delta_5m,
        "mbo_ofi_5m": ofi_5m,
        "mbo_delta_side": delta_side,
        "mbo_cvd_side": cvd_side,
        "mbo_cvd_state_side": cvd_state_side,
        "mbo_cvd_5_delta": int(last.get("cvd_5_delta") or 0),
        "mbo_imbalance_side": imbalance_side,
        "mbo_passive_side": passive_side,
        "mbo_queue_median": round(queue_median, 6),
        "mbo_queue_side": queue_side,
        "mbo_ofi_side": ofi_side,
        "mbo_buy_150": buy_150,
        "mbo_sell_150": sell_150,
        "mbo_large_trade_side": large_trade_side,
        "mbo_volume_baseline_5m": volume_baseline,
        "mbo_volume_ratio_5m": round(volume_ratio, 6) if volume_ratio is not None else None,
        "mbo_volume_burst": volume_burst,
        "mbo_full_confirmation": full_confirmation,
        "mbo_vwap_state": str(last.get("vwap_state") or "unknown"),
        "pa_vwap_state": pa_vwap_state,
        "pa_vwap_side": pa_vwap_side,
    }


def _gate_matches(gate: str, features: dict[str, Any], direction: int) -> bool:
    if gate == "pa_vwap_aligned":
        return int(features.get("pa_vwap_side") or 0) == direction
    if gate == "mbo_delta_aligned":
        return int(features.get("mbo_delta_side") or 0) == direction
    if gate == "mbo_cvd_aligned":
        return int(features.get("mbo_cvd_side") or 0) == direction
    if gate == "mbo_volume_burst":
        return bool(features.get("mbo_volume_burst"))
    if gate == "mbo_imbalance_aligned":
        return int(features.get("mbo_imbalance_side") or 0) == direction
    if gate == "mbo_passive_rejection":
        return int(features.get("mbo_passive_side") or 0) == direction
    if gate == "mbo_queue_aligned":
        return int(features.get("mbo_queue_side") or 0) == direction
    if gate == "mbo_ofi_aligned":
        return int(features.get("mbo_ofi_side") or 0) == direction
    if gate == "mbo_large_trade_aligned":
        return int(features.get("mbo_large_trade_side") or 0) == direction
    if gate == "mbo_full_confirmation":
        return bool(features.get("mbo_full_confirmation")) and (
            int(features.get("mbo_delta_side") or 0) == direction
        )
    raise ValueError(f"unknown flow gate: {gate}")


def context_matches(context: FlowContext, features: dict[str, Any], direction: int) -> bool:
    return all(_gate_matches(gate, features, direction) for gate in context.gates)


def _session_date(session: Any) -> str:
    value = getattr(session, "session_date", None)
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _signal_rows(
    sessions: Sequence[Any],
    all_sessions: Sequence[Any],
    flow_by_date: dict[str, dict[str, Any]],
    *,
    tick: float,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Generate PA specs and attach a same-window MBO feature snapshot."""

    previous_by_date: dict[str, Any] = {}
    for index, session in enumerate(all_sessions):
        if index:
            previous_by_date[_session_date(session)] = all_sessions[index - 1]

    rows: list[dict[str, Any]] = []
    counts = Counter({"pa_specs": 0, "joined_specs": 0, "missing_flow_window": 0})
    for session in sessions:
        session_day = _session_date(session)
        flow_day = flow_by_date.get(session_day)
        if flow_day is None:
            continue
        signal_bars = {bar.end_index: bar for bar in session.signal_bars(SIGNAL_MINUTES)}
        for setup in SETUPS:
            for direction in SIDES:
                specs = _generate_specs(
                    session,
                    setup,
                    direction,
                    previous_session=previous_by_date.get(session_day),
                    tick=tick,
                )
                for spec in specs:
                    counts["pa_specs"] += 1
                    signal_bar = signal_bars.get(spec.signal_index)
                    if signal_bar is None:
                        counts["missing_flow_window"] += 1
                        continue
                    features = _flow_features(
                        flow_day,
                        int(spec.signal_time.timestamp()),
                        signal_bar,
                        tick=tick,
                    )
                    if features is None:
                        counts["missing_flow_window"] += 1
                        continue
                    counts["joined_specs"] += 1
                    rows.append({
                        "date": session_day,
                        "setup": setup,
                        "direction": int(direction),
                        "signal_spec": spec,
                        "features": features,
                        "session": session,
                    })
    return rows, dict(counts)


def _split_trades(trades: Sequence[Any], split: str) -> list[Any]:
    result = []
    for trade in trades:
        day = _as_et(trade.entry_time).date()
        if split == "discovery" and day <= DISCOVERY_END:
            result.append(trade)
        elif split == "evaluation" and day > DISCOVERY_END:
            result.append(trade)
    return result


def _summary(trades: Sequence[Any]) -> dict[str, Any]:
    pnls = [float(trade.pnl) for trade in trades]
    stats = series_stats(pnls)
    stress = slip_injection(
        [trade.robustness_row() for trade in trades],
        levels=(SLIPPAGE_STRESS_TICKS,),
        symbol=SYMBOL,
    )["levels"][0]["stats"]
    days = {_as_et(trade.entry_time).date().isoformat() for trade in trades}
    return {
        "trades": int(stats["n"]),
        "pnl": round(float(stats["pnl"]), 2),
        "pf": round(float(stats["pf"]), 4),
        "max_dd": round(float(stats["max_dd"]), 2),
        "win": round(float(stats["win"]), 4),
        "expectancy": round(float(stats["pnl"]) / len(trades), 2) if trades else 0.0,
        "slip14_pnl": round(float(stress["pnl"]), 2),
        "slip14_pf": round(float(stress["pf"]), 4),
        "trading_days": len(days),
    }


def _flat(prefix: str, summary: dict[str, Any]) -> dict[str, Any]:
    return {f"{prefix}_{key}": value for key, value in summary.items()}


def _simulate_variant(
    selected_rows: Sequence[dict[str, Any]],
    *,
    sessions_by_date: dict[str, Any],
    setup: str,
    direction: int,
    tick: float,
) -> tuple[list[Any], Counter]:
    grouped: dict[str, list[Any]] = defaultdict(list)
    for row in selected_rows:
        grouped[str(row["date"])].append(
            _candidate(
                setup,
                direction,
                row["signal_spec"],
                TARGET_R,
                tick,
            )
        )
    trades: list[Any] = []
    skipped: Counter = Counter()
    for day in sorted(grouped):
        session = sessions_by_date.get(day)
        if session is None:
            continue
        session_trades, session_skipped = simulate_candidates(
            session,
            grouped[day],
            SYMBOL,
            max_trades_per_day=MAX_TRADES_PER_DAY,
        )
        trades.extend(session_trades)
        skipped.update(session_skipped)
    return trades, skipped


def _robustness(trades: Sequence[Any], *, mc_iters: int) -> dict[str, Any]:
    result = evaluate(
        [trade.robustness_row() for trade in trades],
        iters=mc_iters,
        seed=MC_SEED,
        dd_threshold=2000.0,
        slip_levels=(1, 2, 4, 8, SLIPPAGE_STRESS_TICKS),
    )
    monte_carlo = result.get("monte_carlo") or {}
    walk_forward = result.get("walk_forward") or {}
    stress = next(
        item["stats"]
        for item in result["slip"]["levels"]
        if item["level"] == SLIPPAGE_STRESS_TICKS
    )
    return {
        "trades": len(trades),
        "pnl": result["stats"]["pnl"],
        "pf": result["stats"]["pf"],
        "max_dd": result["stats"]["max_dd"],
        "slip14_pf": stress["pf"],
        "mc_p_loss": monte_carlo.get("p_loss"),
        "mc_dd_p95": monte_carlo.get("dd_p95"),
        "mc_pf_p5": monte_carlo.get("pf_p5"),
        "mc_pass": bool(result.get("monte_carlo_pass")),
        "wf_pass": bool(walk_forward.get("pass")),
        "verdict": (
            "PASS_AUDIT"
            if len(trades) >= 100
            and stress["pf"] > 1.0
            and bool(walk_forward.get("pass"))
            and bool(result.get("monte_carlo_pass"))
            else "INSUFFICIENT_OR_FAIL"
        ),
    }


def _run_with_data(
    flow_days: Sequence[dict[str, Any]],
    *,
    mc_iters: int,
) -> dict[str, Any]:
    if not flow_days:
        return {"status": "no_cache", "results": []}
    schema_versions = Counter(int(day.get("schema_version") or 0) for day in flow_days)
    if any(version < 5 for version in schema_versions):
        return {
            "status": "waiting_for_schema_v5_rebuild",
            "coverage": {
                "flow_days": len(flow_days),
                "cache_dates": [day["date"] for day in flow_days],
                "cache_schema_versions": dict(schema_versions),
            },
            "results": [],
        }

    flow_by_date = {str(day["date"]): day for day in flow_days}
    flow_dates = sorted(date.fromisoformat(day) for day in flow_by_date)
    sessions_start = min(flow_dates) - timedelta(days=7)
    sessions_end = max(flow_dates)
    all_sessions, info = load_symbol_sessions(
        SYMBOL,
        start_date=sessions_start,
        end_date=sessions_end,
    )
    overlap_sessions = [session for session in all_sessions if _session_date(session) in flow_by_date]
    sessions_by_date = {_session_date(session): session for session in overlap_sessions}
    tick = get_tick_size(current_quarterly_contract_id(SYMBOL))
    signal_rows, join_counts = _signal_rows(
        overlap_sessions,
        all_sessions,
        flow_by_date,
        tick=tick,
    )

    context_rows: list[dict[str, Any]] = []
    context_map = {context.name: context for context in flow_context_catalog()}
    trades_by_key: dict[str, list[Any]] = {}
    for context in context_map.values():
        for setup in SETUPS:
            for direction in SIDES:
                selected = [
                    row
                    for row in signal_rows
                    if row["setup"] == setup
                    and int(row["direction"]) == direction
                    and context_matches(context, row["features"], direction)
                ]
                trades, skipped = _simulate_variant(
                    selected,
                    sessions_by_date=sessions_by_date,
                    setup=setup,
                    direction=direction,
                    tick=tick,
                )
                key = "|".join((context.name, setup, "long" if direction > 0 else "short"))
                trades_by_key[key] = trades
                full = _summary(trades)
                discovery = _summary(_split_trades(trades, "discovery"))
                evaluation = _summary(_split_trades(trades, "evaluation"))
                row = {
                    "context": context.name,
                    "context_gates": "+".join(context.gates) or "none",
                    "setup": setup,
                    "side": "long" if direction > 0 else "short",
                    "candidates": len(selected),
                    "skipped": dict(skipped),
                    "sessions": len(overlap_sessions),
                    "key": key,
                }
                row.update(_flat("full", full))
                row.update(_flat("discovery", discovery))
                row.update(_flat("evaluation", evaluation))
                context_rows.append(row)

    shortlist = [
        row
        for row in context_rows
        if int(row["discovery_trades"]) >= DISCOVERY_MIN_TRADES
    ]
    shortlist = sorted(
        shortlist,
        key=lambda row: (
            float(row["discovery_pnl"]),
            float(row["discovery_pf"]),
            int(row["discovery_trades"]),
        ),
        reverse=True,
    )[:ROBUSTNESS_LIMIT]
    robustness: dict[str, dict[str, Any]] = {}
    for row in shortlist:
        robustness[row["key"]] = _robustness(
            trades_by_key[row["key"]],
            mc_iters=mc_iters,
        )

    coverage = {
        "flow_days": len(flow_days),
        "flow_start": min(day["date"] for day in flow_days),
        "flow_end": max(day["date"] for day in flow_days),
        "cache_dates": [day["date"] for day in flow_days],
        "cache_schema_versions": dict(schema_versions),
        "pa_sessions_loaded": len(all_sessions),
        "pa_sessions_with_flow": len(overlap_sessions),
        "pa_dataset_bars": int(info.total_bars),
        "pa_specs": int(join_counts["pa_specs"]),
        "joined_specs": int(join_counts["joined_specs"]),
        "missing_flow_window": int(join_counts["missing_flow_window"]),
        "join_rate": round(
            join_counts["joined_specs"] / join_counts["pa_specs"], 4
        ) if join_counts["pa_specs"] else 0.0,
        "discovery_end": DISCOVERY_END.isoformat(),
    }
    return {
        "status": "provisional_research_only",
        "generated_at": datetime.now(UTC).isoformat(),
        "coverage": coverage,
        "study": {
            "symbol": SYMBOL,
            "signal_timeframe_minutes": SIGNAL_MINUTES,
            "flow_timeframe_minutes": 1,
            "target_r": TARGET_R,
            "risk_dollars": RISK_DOLLARS,
            "max_trades_per_day": MAX_TRADES_PER_DAY,
            "entry": "next available 1m open after completed 5m PA signal",
            "one_position_at_a_time": True,
            "date_split": {
                "discovery_end": DISCOVERY_END.isoformat(),
                "evaluation": "flow-covered dates after discovery_end; not untouched holdout",
            },
            "orderflow_source": "settled Databento GLBX.MDP3 MBO DBN -> schema-v5 compact RTH 1m cache",
            "definitions": {
                "mbo_delta": "sum of aggressive buy minus sell quantity over the completed five-minute signal window",
                "mbo_cvd": "existing causal RTH CVD state and five-minute delta at the signal minute",
                "mbo_volume_burst": "five-minute executed volume >= 1.25x the median of prior aligned five-minute windows",
                "mbo_imbalance": "majority side of completed-minute diagonal 1:10 footprint imbalance in the signal window",
                "mbo_passive_rejection": "majority side of completed-minute passive bid/ask rejection in the signal window",
                "mbo_queue": "median close-BBO queue imbalance >= +0.15 or <= -0.15",
                "mbo_ofi": "sign of summed order-flow imbalance over the completed signal window",
                "mbo_large_trade": "majority executed 150+ size-band quantity by side",
                "pa_vwap": "completed PA signal close versus the canonical RTH VWAP available at that signal bar",
                "full_confirmation": "delta and CVD agree, volume bursts, plus imbalance/passive rejection/large-trade agreement",
                "cost": "canonical MNQ commission and fees through the shared research engine",
            },
        },
        "results": context_rows,
        "robustness": robustness,
        "notes": [
            "This runner uses local settled compact MBO data and makes no paid API request.",
            "Raw anonymous MBO order IDs do not identify institutions; features describe executed/aggressive/passive flow only.",
            "The screen is frozen and small because 26 RTH days is not enough for a credible order-flow edge estimate.",
            "Evaluation rows after the discovery cutoff are retrospective; no row is promoted to production or live.",
        ],
    }


def run(*, mc_iters: int = 1000) -> dict[str, Any]:
    started = time.time()
    result = _run_with_data(_prepare_days(), mc_iters=mc_iters)
    result["elapsed_seconds"] = round(time.time() - started, 3)
    return result


def _write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
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


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Price action × Databento MBO order-flow study",
        "",
        f"Status: **{result.get('status', '—')}**",
        f"Generated: `{result.get('generated_at', '—')}`",
        "",
    ]
    coverage = result.get("coverage") or {}
    if coverage:
        lines.extend([
            f"Flow coverage: **{coverage.get('flow_days', 0)}** RTH days "
            f"({coverage.get('flow_start', '—')} → {coverage.get('flow_end', '—')}); "
            f"joined PA specs: **{coverage.get('joined_specs', 0):,} / {coverage.get('pa_specs', 0):,}** "
            f"({float(coverage.get('join_rate', 0.0)):.1%})",
            "",
            "The MBO side is the existing settled Databento compact cache.  Each gate uses only the five completed one-minute bars ending at the completed PA signal bar; execution is the next one-minute open.",
            "",
            "## Evaluation screen",
            "",
            "Rows are sorted by evaluation P&L for readability.  They were not selected by evaluation performance.",
            "",
            "| Context | PA setup | Side | Candidates | Eval N | Eval P&L | Eval PF | 14t PF |",
            "|---|---|---|---:|---:|---:|---:|---:|",
        ])
        rows = sorted(
            result.get("results") or [],
            key=lambda row: (float(row.get("evaluation_pnl", 0.0)), float(row.get("evaluation_pf", 0.0))),
            reverse=True,
        )
        for row in rows[:40]:
            lines.append(
                f"| {row['context']} | {row['setup']} | {row['side']} | {row['candidates']} | "
                f"{row['evaluation_trades']} | ${_fmt(row['evaluation_pnl'])} | "
                f"{_fmt(row['evaluation_pf'])} | {_fmt(row['evaluation_slip14_pf'])} |"
            )
        lines.extend([
            "",
            "## Discovery-selected audit",
            "",
            "| Context | PA setup | Side | Full N | Full PF | 14t PF | MC loss | WF | Verdict |",
            "|---|---|---|---:|---:|---:|---:|---|---|",
        ])
        by_key = {row.get("key"): row for row in result.get("results") or []}
        for key, audit in sorted(
            (result.get("robustness") or {}).items(),
            key=lambda item: float((by_key.get(item[0]) or {}).get("discovery_pnl", 0.0)),
            reverse=True,
        ):
            row = by_key.get(key) or {}
            lines.append(
                f"| {row.get('context', '—')} | {row.get('setup', '—')} | {row.get('side', '—')} | "
                f"{audit.get('trades', 0)} | {_fmt(audit.get('pf'))} | {_fmt(audit.get('slip14_pf'))} | "
                f"{_fmt(audit.get('mc_p_loss'))} | {'PASS' if audit.get('wf_pass') else 'FAIL'} | {audit.get('verdict', '—')} |"
            )
        lines.extend([
            "",
            "## Limits",
            "",
            "This is a causal integration test, not proof of a tradable edge.  The 26-day cache is a useful wiring check but a small sample for MBO research; the evaluation block is retrospective and has no untouched holdout.  Anonymous MBO records cannot reveal a specific institution.  No API charge, preset change, or live change was made by this study.",
            "",
            "Official implementation references: [Databento Historical API basics](https://databento.com/docs/reference-historical/basics/), [MBO schema](https://databento.com/docs/schemas-and-data-formats/mbo), and [MBO snapshot conventions](https://databento.com/docs/standards-and-conventions/mbo-snapshot).",
            "",
        ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mc-iters", type=int, default=1000)
    parser.add_argument(
        "--output",
        default=str(market_data.derived_path("research", "price_action_databento_orderflow_study_current.json")),
    )
    args = parser.parse_args()
    if args.mc_iters <= 0:
        parser.error("--mc-iters must be positive")
    output = Path(args.output)
    result = run(mc_iters=args.mc_iters)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    csv_path = output.with_suffix(".csv")
    markdown_path = output.with_suffix(".md")
    _write_csv(csv_path, result.get("results") or [])
    markdown_path.write_text(_markdown(result), encoding="utf-8")
    print(f"JSON: {output}")
    print(f"CSV: {csv_path}")
    print(f"Report: {markdown_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
