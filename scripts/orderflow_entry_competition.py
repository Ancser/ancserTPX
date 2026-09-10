"""Compare order-flow entry hypotheses with the current PI exit contract.

This file is deliberately an offline research runner.  It does not alter the
live engine, presets, chart, or order routing.

The comparison contract is frozen to the active ``PI 2MNQ BOTH BEST`` preset:

* signal/event -> next one-minute bar open;
* one position at a time and a ten-minute same-direction signal cooldown;
* long: ATR-blend SL 4x / TP 12x, no time stop before the RTH close;
* short: ATR-blend SL 1.5x, no hard TP, time exit after 60 minutes;
* one MNQ round-turn commission plus fee from the canonical contract table.

Raw MBO candidates use only information available by the decision point.  In
particular, the event's forward outcomes and descriptive labels are ignored.
The three-second passive fields are allowed because the entry is the next
minute open.  Context filters are taken from the last completed one-minute
bar, never from the still-forming event minute.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import statistics
import sys
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.data import market_data
from backend.db.models import get_commission_rt, get_fees_rt, get_point_value
from scripts.orderflow_context_combination_study import (
    CONTEXTS,
    _build_entries,
    _context_matches,
    _prepare_days,
)
from scripts.orderflow_filter_exit_study import _atr_blend_at, _atr_history
from scripts.orderflow_pi_relationship_study import (
    DISCOVERY_END,
    _has_ohlc,
    _load_pi,
)


UTC = timezone.utc
TICK_SIZE = 0.25
POINT_VALUE = float(get_point_value("MNQ"))
ROUND_TURN_COST = float(get_commission_rt("MNQ") + get_fees_rt("MNQ"))
LONG_SL_ATR = 4.0
LONG_TP_ATR = 12.0
SHORT_SL_ATR = 1.5
SHORT_HOLD_MINUTES = 60
SIGNAL_COOLDOWN_SECONDS = 10 * 60
CURRENT_LONG_KINDS = frozenset(("青π", "深蓝圈"))
CURRENT_SHORT_KINDS = frozenset(("粉π",))
MIN_EVALUATION_TRADES = 20
MAX_DRAWDOWN_GATE = 2_000.0

# Contexts already have fixed definitions in the order-flow context study.
# Keeping the complete set here makes the result auditable; promotion is still
# decided from the untouched evaluation dates, not from the best row.
RAW_CONTEXTS = tuple(CONTEXTS)


@dataclass(frozen=True, slots=True)
class RawSignal:
    """Small raw-event index entry; bars are attached only while evaluating."""

    date: str
    event_sec: int
    pattern: str
    family: str
    rule: str
    direction: int
    event_impact_ticks: int
    actual_qty: int
    opposite_add_3s: int
    opposite_fill_3s: int


def _round_tick(value: float) -> float:
    return round(float(value) / TICK_SIZE) * TICK_SIZE


def _levels(entry: float, direction: int, atr: float | None) -> tuple[float | None, float | None]:
    if atr is None or not math.isfinite(float(atr)) or atr <= 0:
        return None, None
    if direction > 0:
        return (
            _round_tick(entry - LONG_SL_ATR * atr),
            _round_tick(entry + LONG_TP_ATR * atr),
        )
    return _round_tick(entry + SHORT_SL_ATR * atr), None


def _simulate_shared(
    bars: list[dict[str, Any]],
    entry_index: int,
    direction: int,
    entry: float,
    sl: float,
    tp: float | None,
) -> dict[str, Any] | None:
    """Simulate the active PI exit contract on one RTH bar sequence."""

    if entry_index < 0 or entry_index >= len(bars):
        return None
    entry_epoch = int(bars[entry_index]["epoch"])
    deadline = entry_epoch + SHORT_HOLD_MINUTES * 60 if direction < 0 else None
    path: list[tuple[int, dict[str, Any]]] = []
    for index in range(entry_index, len(bars)):
        bar = bars[index]
        if any(bar.get(field) is None for field in ("open", "high", "low", "close")):
            break
        path.append((index, bar))
        if deadline is not None and int(bar["epoch"]) >= deadline:
            break
    if not path:
        return None

    for elapsed, (index, bar) in enumerate(path, 1):
        if direction > 0:
            sl_hit = float(bar["low"]) <= float(sl)
            tp_hit = tp is not None and float(bar["high"]) >= float(tp)
        else:
            sl_hit = float(bar["high"]) >= float(sl)
            tp_hit = False
        if not (sl_hit or tp_hit):
            continue
        if sl_hit and tp_hit:
            sl_first = abs(float(bar["open"]) - float(sl)) <= abs(float(bar["open"]) - float(tp))
        else:
            sl_first = bool(sl_hit)
        if sl_first:
            exit_price = (
                min(float(sl), float(bar["open"]))
                if direction > 0
                else max(float(sl), float(bar["open"]))
            )
            reason = "sl"
        else:
            exit_price = float(tp)
            reason = "tp"
        return {
            "exit_index": index,
            "exit_price": exit_price,
            "exit_reason": reason,
            "bars_held": elapsed,
        }

    index, bar = path[-1]
    return {
        "exit_index": index,
        "exit_price": float(bar["close"]),
        "exit_reason": "time" if direction < 0 else "rth_close",
        "bars_held": len(path),
    }


def _metrics(trades: list[dict[str, Any]], day_count: int) -> dict[str, Any]:
    ordered = sorted(trades, key=lambda row: (str(row["date"]), int(row["entry_epoch"])))
    pnls = [float(row["net_pnl"]) for row in ordered]
    if not pnls:
        return {
            "trades": 0,
            "trades_per_day": 0.0,
            "net_pnl": 0.0,
            "max_drawdown_abs": 0.0,
            "pf": 0.0,
            "win_rate": None,
            "expectancy": 0.0,
            "qualified": False,
        }
    gains = sum(value for value in pnls if value > 0)
    losses = -sum(value for value in pnls if value < 0)
    running = 0.0
    peak = 0.0
    max_dd = 0.0
    for value in pnls:
        running += value
        peak = max(peak, running)
        max_dd = min(max_dd, running - peak)
    pf = gains / losses if losses else (math.inf if gains else 0.0)
    return {
        "trades": len(pnls),
        "trades_per_day": round(len(pnls) / max(1, day_count), 2),
        "net_pnl": round(sum(pnls), 2),
        "max_drawdown_abs": round(abs(max_dd), 2),
        "pf": round(pf, 4) if math.isfinite(pf) else "inf",
        "win_rate": round(sum(value > 0 for value in pnls) / len(pnls), 4),
        "expectancy": round(sum(pnls) / len(pnls), 2),
        "gross_profit": round(gains, 2),
        "gross_loss": round(losses, 2),
        "exit_reasons": dict(Counter(row["exit_reason"] for row in ordered)),
        "qualified": bool(sum(pnls) > 0 and (pf == math.inf or pf > 1.0)),
    }


def _apply_cooldown(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    accepted: list[dict[str, Any]] = []
    last_signal: dict[tuple[str, int], int] = {}
    for row in sorted(rows, key=lambda value: (str(value["date"]), int(value["signal_epoch"]), int(value["direction"]))):
        key = (str(row["date"]), int(row["direction"]))
        signal_epoch = int(row["signal_epoch"])
        if signal_epoch - last_signal.get(key, -10**12) < SIGNAL_COOLDOWN_SECONDS:
            continue
        accepted.append(row)
        last_signal[key] = signal_epoch
    return accepted


def _run_strategy(
    rows: Iterable[dict[str, Any]],
    atr_history: list[tuple[int, float]],
    day_count: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    trades: list[dict[str, Any]] = []
    occupied_until: dict[str, int] = {}
    for row in _apply_cooldown(rows):
        date_key = str(row["date"])
        entry_index = int(row["entry_index"])
        if entry_index <= occupied_until.get(date_key, -1):
            continue
        bars = row["bars"]
        entry_bar = bars[entry_index]
        entry = float(entry_bar["open"])
        direction = int(row["direction"])
        atr = _atr_blend_at(atr_history, int(entry_bar["epoch"]))
        sl, tp = _levels(entry, direction, atr)
        if sl is None:
            continue
        outcome = _simulate_shared(bars, entry_index, direction, entry, sl, tp)
        if outcome is None:
            continue
        directional_points = direction * (float(outcome["exit_price"]) - entry)
        gross_pnl = directional_points * POINT_VALUE
        net_pnl = gross_pnl - ROUND_TURN_COST
        trade = {
            "candidate": row["candidate"],
            "context": row.get("context", "all"),
            "family": row.get("family", "unknown"),
            "rule": row.get("rule", "unknown"),
            "pattern": row.get("pattern"),
            "date": date_key,
            "signal_epoch": int(row["signal_epoch"]),
            "entry_epoch": int(entry_bar["epoch"]),
            "entry_index": entry_index,
            "exit_index": int(outcome["exit_index"]),
            "direction": direction,
            "entry_price": entry,
            "exit_price": float(outcome["exit_price"]),
            "sl_price": sl,
            "tp_price": tp,
            "atr_blend": atr,
            "exit_reason": outcome["exit_reason"],
            "bars_held": int(outcome["bars_held"]),
            "directional_points": round(directional_points, 4),
            "gross_pnl": round(gross_pnl, 2),
            "costs": round(ROUND_TURN_COST, 2),
            "net_pnl": round(net_pnl, 2),
            "pi_message_id": row.get("pi_message_id"),
        }
        trades.append(trade)
        occupied_until[date_key] = int(outcome["exit_index"])
    return _metrics(trades, day_count), trades


def _split_metrics(trades: list[dict[str, Any]], days: list[dict[str, Any]]) -> dict[str, Any]:
    discovery_days = {day["date"] for day in days if day["date"] <= DISCOVERY_END}
    evaluation_days = {day["date"] for day in days if day["date"] > DISCOVERY_END}
    return {
        "all": _metrics(trades, len(days)),
        "discovery": _metrics([row for row in trades if row["date"] <= DISCOVERY_END], len(discovery_days)),
        "evaluation": _metrics([row for row in trades if row["date"] > DISCOVERY_END], len(evaluation_days)),
    }


def _day_index(days: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for day in days:
        bars = day["bars"]
        epochs = [int(bar["epoch"]) for bar in bars]
        result[day["date"]] = {"day": day, "bars": bars, "epochs": epochs}
    return result


def _current_pi_entries(days: list[dict[str, Any]], pi_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    index = _day_index(days)
    rows: list[dict[str, Any]] = []
    for pi in pi_rows:
        kind = str(pi.get("kind") or "")
        direction = int(pi.get("direction") or 0)
        allowed = CURRENT_LONG_KINDS if direction > 0 else CURRENT_SHORT_KINDS
        if kind not in allowed:
            continue
        try:
            signal_dt = datetime.fromisoformat(str(pi["ts"]))
            date_key = signal_dt.date().isoformat()
            signal_epoch = int(signal_dt.timestamp())
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        location = index.get(date_key)
        if not location:
            continue
        epochs = location["epochs"]
        bars = location["bars"]
        signal_index = bisect_right(epochs, signal_epoch) - 1
        if signal_index < 0 or signal_index + 1 >= len(bars):
            continue
        if not _has_ohlc(bars[signal_index]) or bars[signal_index + 1].get("open") is None:
            continue
        rows.append({
            "candidate": "PI current preset",
            "context": "all",
            "family": "pi",
            "rule": "kind_filter",
            "pattern": kind,
            "date": date_key,
            "signal_epoch": int(bars[signal_index]["epoch"]),
            "entry_index": signal_index + 1,
            "direction": direction,
            "pi_message_id": str(pi.get("message_id")),
            "bars": bars,
        })
    return rows


def _context_bar_for_raw(location: dict[str, Any], event_sec: int) -> tuple[int, int] | None:
    epochs = location["epochs"]
    event_index = bisect_right(epochs, int(event_sec)) - 1
    if event_index <= 0 or event_index + 1 >= len(epochs):
        return None
    if int(event_sec) >= epochs[event_index] + 60:
        return None
    # event_index is still forming; event_index - 1 is the last complete bar.
    return event_index, event_index - 1


def _raw_rule_matches(event: dict[str, Any]) -> list[tuple[str, int]]:
    """Return rule/direction pairs using only causal event fields."""
    direction = int(event.get("direction") or 0)
    if direction not in {-1, 1}:
        return []
    impact = int(event.get("event_impact_ticks") or 0)
    result: list[tuple[str, int]] = []
    if impact >= 2:
        result.append(("continuation_impact2", direction))
    if impact >= 4:
        result.append(("continuation_impact4", direction))

    opposite_fill = int(event.get("opposite_fill_3s") or 0)
    opposite_add = int(event.get("opposite_add_3s") or 0)
    actual_qty = max(1, int(event.get("actual_qty") or 0))
    same_price_ratio = float(event.get("same_price_qty_ratio") or 0.0)
    if abs(impact) <= 1 and opposite_fill > 0 and opposite_add >= max(50, actual_qty // 2):
        result.append(("absorption_passive3", -direction))
    if abs(impact) <= 1 and opposite_fill > 0 and same_price_ratio >= 0.25:
        result.append(("absorption_same_price", -direction))
    pretrend = event.get("pretrend_30s_ticks")
    if (
        abs(impact) <= 1
        and opposite_fill > 0
        and pretrend is not None
        and float(pretrend) >= 8
    ):
        result.append(("failed_effort_reversal", -direction))
    return result


def _load_raw_entries(
    days: list[dict[str, Any]],
    events_path: Path,
) -> dict[str, list[RawSignal]]:
    result: dict[str, list[RawSignal]] = defaultdict(list)
    valid_dates = {str(day["date"]) for day in days}
    if not events_path.exists():
        return result
    with gzip.open(events_path, "rt", encoding="utf-8") as handle:
        for line in handle:
            try:
                event = json.loads(line)
                date_key = str(event["trade_date"])
                event_sec = int(event["event_sec"])
                pattern = str(event["pattern"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            if date_key not in valid_dates:
                continue
            for rule, direction in _raw_rule_matches(event):
                signal = RawSignal(
                    date=date_key,
                    event_sec=event_sec,
                    pattern=pattern,
                    family=str(event.get("family") or "unknown"),
                    rule=rule,
                    direction=direction,
                    event_impact_ticks=int(event.get("event_impact_ticks") or 0),
                    actual_qty=int(event.get("actual_qty") or 0),
                    opposite_add_3s=int(event.get("opposite_add_3s") or 0),
                    opposite_fill_3s=int(event.get("opposite_fill_3s") or 0),
                )
                base_name = f"raw:{pattern}:{rule}"
                result[base_name].append(signal)
                # Aggregate by MBO detector family and across all raw events.
                for group in (f"raw_family:{signal.family}:{rule}", f"raw_all:{rule}"):
                    result[group].append(signal)
    return result


def _materialize_raw_rows(
    signals: Iterable[RawSignal],
    candidate: str,
    locations: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for signal in signals:
        location = locations.get(signal.date)
        if not location:
            continue
        mapped = _context_bar_for_raw(location, signal.event_sec)
        if mapped is None:
            continue
        event_index, context_index = mapped
        bars = location["bars"]
        rows.append({
            "candidate": candidate,
            "context": "all",
            "family": "raw_mbo",
            "rule": signal.rule,
            "pattern": signal.pattern,
            "date": signal.date,
            "signal_epoch": signal.event_sec,
            "entry_index": event_index + 1,
            "direction": signal.direction,
            "bars": bars,
            "context_bar": bars[context_index],
            "event_impact_ticks": signal.event_impact_ticks,
            "actual_qty": signal.actual_qty,
            "opposite_add_3s": signal.opposite_add_3s,
            "opposite_fill_3s": signal.opposite_fill_3s,
        })
    return _dedupe_rows(rows)


def _dedupe_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, int, int, str]] = set()
    result: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda value: (str(value["date"]), int(value["signal_epoch"]), int(value["direction"]))):
        key = (str(row["date"]), int(row["signal_epoch"]), int(row["direction"]), str(row["rule"]))
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def _with_context(rows: Iterable[dict[str, Any]], context: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        if context == "all":
            result.append(dict(row, context=context))
            continue
        context_bar = row.get("context_bar")
        if context_bar is None:
            # PI rows do not carry an MBO context snapshot; PI itself is the
            # baseline, not a PI+context fitted candidate in this runner.
            continue
        if _context_matches(context, context_bar, int(row["direction"])):
            result.append(dict(row, context=context))
    return result


def _compact_entries(
    days: list[dict[str, Any]], pi_rows: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    entries = _build_entries(days, pi_rows=[])
    compact: dict[str, list[dict[str, Any]]] = {}
    for family in ("mbo_turnover", "passive_touch_reject", "large_150_reversal"):
        rows = entries.get(family) or []
        for row in rows:
            row["candidate"] = f"compact:{family}"
            row["context_bar"] = row["bars"][int(row["entry_index"]) - 1]
        compact[f"compact:{family}"] = rows
    return compact


def _post_compare(results: dict[str, Any], baseline: dict[str, Any]) -> None:
    baseline_eval = baseline["evaluation"]
    baseline_pf = baseline_eval.get("pf")
    baseline_net = float(baseline_eval.get("net_pnl") or 0.0)
    for result in results.values():
        evaluation = result["metrics"]["evaluation"]
        candidate_pf = evaluation.get("pf")
        pf_beats = (
            candidate_pf == "inf"
            if baseline_pf != "inf" and candidate_pf == "inf"
            else False
        )
        if not pf_beats and baseline_pf != "inf" and candidate_pf != "inf":
            try:
                pf_beats = float(candidate_pf) > float(baseline_pf)
            except (TypeError, ValueError):
                pf_beats = False
        result["beats_baseline"] = bool(
            evaluation.get("trades", 0) >= MIN_EVALUATION_TRADES
            and pf_beats
            and float(evaluation.get("net_pnl") or 0.0) > baseline_net
            and float(evaluation.get("max_drawdown_abs") or 0.0) <= MAX_DRAWDOWN_GATE
        )


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _markdown(report: dict[str, Any]) -> str:
    baseline = report["baseline"]
    baseline_eval = baseline["evaluation"]
    ranked = sorted(
        report["results"].values(),
        key=lambda row: (
            bool(row.get("beats_baseline")),
            float(row["metrics"]["evaluation"].get("pf") or 0.0)
            if row["metrics"]["evaluation"].get("pf") != "inf" else 10**9,
            float(row["metrics"]["evaluation"].get("net_pnl") or 0.0),
        ),
        reverse=True,
    )
    lines = [
        "# Order-flow entry competition against current PI",
        "",
        "Offline research only. No live, preset, frontend, or order-routing code was changed.",
        "",
        f"Common RTH dates: **{report['coverage']['rth_days']}**; discovery through **{DISCOVERY_END}**; "
        f"canonical MNQ round-turn cost: **${ROUND_TURN_COST:.2f}**.",
        "",
        "## Frozen comparison contract",
        "",
        "Every row enters at the next one-minute open, permits one position at a time, and uses a ten-minute same-direction cooldown. "
        "The current PI preset uses long ATR-blend SL 4x / TP 12x with no long time stop; short ATR-blend SL 1.5x with a 60-minute time exit and no hard TP.",
        "",
        "## Baseline",
        "",
        "| Candidate | Split | N | Net P&L | Max DD | PF | Win | Expectancy |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for split in ("discovery", "evaluation", "all"):
        row = baseline[split]
        lines.append(
            f"| PI current preset | {split} | {row.get('trades', 0)} | ${_fmt(row.get('net_pnl'))} | "
            f"${_fmt(row.get('max_drawdown_abs'))} | {_fmt(row.get('pf'))} | {_fmt(row.get('win_rate'))} | ${_fmt(row.get('expectancy'))} |"
        )
    lines.extend([
        "",
        "## Evaluation ranking (top 40 by PF/net, not a promotion by itself)",
        "",
        "| Candidate | Context | N | Net P&L | Max DD | PF | Win | Beats baseline gate |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ])
    for row in ranked[:40]:
        metrics = row["metrics"]["evaluation"]
        lines.append(
            f"| `{row['candidate']}` | `{row.get('context', 'all')}` | {metrics.get('trades', 0)} | "
            f"${_fmt(metrics.get('net_pnl'))} | ${_fmt(metrics.get('max_drawdown_abs'))} | {_fmt(metrics.get('pf'))} | "
            f"{_fmt(metrics.get('win_rate'))} | {'YES' if row.get('beats_baseline') else 'NO'} |"
        )
    passing = [row for row in ranked if row.get("beats_baseline")]
    lines.extend([
        "",
        "## Gate result",
        "",
        f"Promotion gate: evaluation N >= {MIN_EVALUATION_TRADES}, PF above PI, net P&L above PI, and max drawdown <= ${MAX_DRAWDOWN_GATE:.0f}.",
        "",
    ])
    if passing:
        lines.append("The following candidate(s) pass the mechanical gate; they still require a longer unseen sample before production:")
        for row in passing[:10]:
            lines.append(f"- `{row['candidate']}` + `{row.get('context', 'all')}`")
    else:
        lines.append("No order-flow candidate passes all promotion gates on the current common sample.")
    lines.extend([
        "",
        "## Causal-data guardrails",
        "",
        "Raw MBO forward returns and descriptive labels were not used. A raw event uses its own completed sequence, its 30-second pretrend, and post-event passive fields only through three seconds; the compact context comes from the previous completed one-minute bar.",
        "A 3x50 or 5x30 detector identifies an observable execution pattern, not proof of one hidden parent order. MBO is order-by-order market data, but participant identity and intent remain unobservable.",
        "",
        "Research references: [Databento MBO schema](https://databento.com/docs/schemas-and-data-formats/mbo), [Databento datasets](https://databento.com/docs/knowledge-base/datasets), [Jesse Rogers CZT](https://jesserogerstrading.com/czt/what-is-condition-zone-trigger), [Jesse Rogers order flow](https://jesserogerstrading.com/order-flow).",
        "",
    ])
    return "\n".join(lines)


def run() -> dict[str, Any]:
    days = _prepare_days()
    if not days:
        return {"status": "no_cache", "results": {}}
    pi_rows = _load_pi()
    atr_history = _atr_history(days)
    event_path = market_data.derived_path("research", "orderflow_microburst_study.events.jsonl.gz")

    baseline_rows = _current_pi_entries(days, pi_rows)
    _, baseline_trades = _run_strategy(baseline_rows, atr_history, len(days))
    baseline_split = _split_metrics(baseline_trades, days)

    # Existing compact MBO feature families are evaluated with the same exit,
    # then raw sequence candidates are added from the full MBO event file.
    # Raw events stay as compact slotted records; full bar references are
    # materialized for one candidate at a time to keep research memory bounded.
    compact = _compact_entries(days, pi_rows)
    raw = _load_raw_entries(days, event_path)
    locations = _day_index(days)

    results: dict[str, Any] = {}
    archived_trades: list[dict[str, Any]] = list(baseline_trades)
    archived_strategy_count = 1

    def evaluate_candidate(candidate: str, rows: list[dict[str, Any]], contexts: tuple[str, ...]) -> None:
        nonlocal archived_strategy_count
        rows = _dedupe_rows(rows)
        for context in contexts:
            selected = _with_context(rows, context)
            if not selected:
                continue
            for row in selected:
                row["candidate"] = candidate
            metrics, trades = _run_strategy(selected, atr_history, len(days))
            key = f"{candidate}::{context}"
            results[key] = {
                "candidate": candidate,
                "context": context,
                "input_candidates": len(selected),
                "metrics": _split_metrics(trades, days),
            }
            evaluation = results[key]["metrics"]["evaluation"]
            if evaluation.get("trades", 0) >= MIN_EVALUATION_TRADES:
                archived_trades.extend(trades)
                archived_strategy_count += 1

    for candidate, rows in sorted(compact.items()):
        evaluate_candidate(candidate, rows, ("all",))
    for candidate, signals in sorted(raw.items()):
        rows = _materialize_raw_rows(signals, candidate, locations)
        # Compare every detector pattern directly.  Context combinations are
        # reserved for family/all aggregates; this prevents thousands of
        # overlapping, low-sample rows from looking like independent tests.
        contexts = RAW_CONTEXTS if candidate.startswith(("raw_family:", "raw_all:")) else ("all",)
        evaluate_candidate(candidate, rows, contexts)
        del rows

    _post_compare(results, baseline_split)
    coverage_dates = [day["date"] for day in days]
    return {
        "status": "provisional_research_only",
        "generated_at": datetime.now(UTC).isoformat(),
        "symbol": "MNQ",
        "coverage": {
            "rth_days": len(days),
            "dates": coverage_dates,
            "discovery_end": DISCOVERY_END,
            "pi_rows_loaded": len(pi_rows),
            "current_pi_common_rows": len(baseline_rows),
            "raw_events_path": str(event_path),
            "raw_event_candidates": sum(len(rows) for rows in raw.values()),
            "strategy_rows_tested": len(results),
            "trade_rows_archived": len(archived_trades),
            "strategies_with_trade_archive": archived_strategy_count,
        },
        "contract": {
            "entry": "signal/event -> next one-minute bar open",
            "position": "one position at a time; ten-minute same-direction cooldown",
            "long_exit": "ATR-blend SL 4x / TP 12x; RTH close if neither is hit",
            "short_exit": "ATR-blend SL 1.5x; no hard TP; 60-minute time exit",
            "point_value": POINT_VALUE,
            "round_turn_cost": ROUND_TURN_COST,
            "current_pi_long_kinds": sorted(CURRENT_LONG_KINDS),
            "current_pi_short_kinds": sorted(CURRENT_SHORT_KINDS),
        },
        "baseline": baseline_split,
        "results": results,
        "notes": [
            "The current PI baseline is restricted to the current preset's 青π/深蓝圈 long and 粉π short kinds.",
            "Raw MBO context is read from the last completed bar before the event minute; no event-minute future OHLC is used.",
            "High PF with a small evaluation sample is not a promotion decision.",
        ],
        "trades": archived_trades,
    }


def _write_outputs(report: dict[str, Any], stem: str) -> tuple[Path, Path, Path]:
    root = market_data.derived_path("research")
    root.mkdir(parents=True, exist_ok=True)
    json_path = root / f"{stem}.json"
    md_path = root / f"{stem}.md"
    csv_path = root / f"{stem}_trades.csv.gz"
    report_without_trades = dict(report)
    report_without_trades.pop("trades", None)
    json_path.write_text(json.dumps(report_without_trades, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_markdown(report), encoding="utf-8")
    trades = report.get("trades") or []
    fields = sorted({key for row in trades for key in row})
    with gzip.open(csv_path, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(trades)
    return json_path, md_path, csv_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-stem", default="orderflow_entry_competition")
    args = parser.parse_args()
    report = run()
    json_path, md_path, csv_path = _write_outputs(report, args.output_stem)
    print(json.dumps({
        "status": report.get("status"),
        "coverage": report.get("coverage"),
        "baseline": report.get("baseline"),
        "json": str(json_path),
        "markdown": str(md_path),
        "trades": str(csv_path),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
