"""Causal context audit for the prior-RTH Volume Profile model.

This runner is deliberately research-only.  It replays the selected Volume
Profile candidate through the production ``BacktestEngine`` and joins only
data that was available by the completed entry minute:

* the compact Databento MBO/footprint cache;
* five-minute delta, OFI, volume, imbalance, passive-rejection and delta
  pressure-change features; and
* the frozen QQQ option-wall/GEX snapshot when the trade occurs after the
  snapshot time.

The context fields are diagnostic labels, not a second entry engine.  No
production preset, live configuration, or strategy code is changed here.
The GEX join is explicitly a QQQ-to-MNQ proxy and is never treated as a live
feed.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import logging
import math
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.backtest.engine import BacktestConfig, BacktestEngine  # noqa: E402
from backend.backtest.robustness import series_stats, slip_injection  # noqa: E402
from backend.data import candle_store, market_data  # noqa: E402
from backend.db.models import get_commission_rt, get_fees_rt  # noqa: E402
from backend.timebase import UTC, as_utc  # noqa: E402
from scripts.orderflow_context_combination_study import _prepare_days  # noqa: E402
from scripts.volume_profile_research import _params  # noqa: E402


VERSION = "2026-09-15-volume-profile-context-v1"
SYMBOL = "MNQ"
TICK_SIZE = 0.25
MBO_DISCOVERY_END = date(2026, 8, 27)
STRESS_TICKS = 14
INITIAL_CAPITAL = 50_000.0
GEX_ROWS_PATH = Path(
    "F:/ancserQuant/ancserMarketData/source/options/qqq_option_ml/"
    "option_wall_value_area_gamma_rows.csv.gz"
)


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _sign(value: Any) -> int:
    number = _finite(value)
    return 1 if number > 0 else -1 if number < 0 else 0


def _majority(values: Iterable[Any]) -> int:
    counts = Counter(int(value or 0) for value in values)
    counts.pop(0, None)
    if not counts:
        return 0
    best = max(counts.values())
    winners = [side for side, count in counts.items() if count == best]
    return winners[0] if len(winners) == 1 else 0


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return as_utc(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return as_utc(parsed)


def _trade_direction(value: Any) -> int:
    raw = getattr(value, "value", value)
    text = str(raw or "").lower()
    if text in {"buy", "long", "1", "direction.buy"}:
        return 1
    if text in {"sell", "short", "-1", "direction.sell"}:
        return -1
    return _sign(raw)


def _consecutive(bars: Sequence[dict[str, Any]]) -> bool:
    return bool(bars) and all(
        int(bars[index]["epoch"]) - int(bars[index - 1]["epoch"]) == 60
        for index in range(1, len(bars))
    )


def _delta_state(
    direction: int,
    delta_side: int,
    decay_ratio: float | None,
) -> str:
    """Classify pressure without using any post-entry values."""
    if direction == 0 or delta_side == 0:
        return "neutral"
    if delta_side != direction:
        return "opposing"
    if decay_ratio is not None and decay_ratio < 0.75:
        return "aligned_decaying"
    if decay_ratio is not None and decay_ratio >= 1.25:
        return "aligned_accelerating"
    return "aligned_stable"


def _flow_features(
    bars: Sequence[dict[str, Any]],
    index: int,
    direction: int,
) -> dict[str, Any] | None:
    """Return a causal five-minute MBO context ending at ``index``.

    ``bars[index]`` is the completed minute used by the production replay.
    The decay ratio compares the last two completed minutes with the first
    three minutes of the same five-minute window.  It therefore describes
    pressure changing into the entry and never reads after the entry minute.
    """
    if index < 0 or index >= len(bars):
        return None
    window = list(bars[max(0, index - 4): index + 1])
    prior = list(bars[max(0, index - 9): max(0, index - 4)])
    if len(window) != 5 or len(prior) != 5 or not _consecutive(window) or not _consecutive(prior):
        return None
    if int(window[0]["epoch"]) - int(prior[-1]["epoch"]) != 60:
        return None

    def volume(row: dict[str, Any]) -> int:
        if row.get("volume") is not None:
            return int(row.get("volume") or 0)
        return int(row.get("buy") or 0) + int(row.get("sell") or 0)

    deltas = [int(row.get("delta") if row.get("delta") is not None else int(row.get("buy") or 0) - int(row.get("sell") or 0)) for row in window]
    current_abs = sum(abs(value) for value in deltas)
    local_early_abs = sum(abs(value) for value in deltas[:3])
    local_late_abs = sum(abs(value) for value in deltas[3:])
    decay_ratio = local_late_abs / max(1, local_early_abs)
    prior_blocks: list[int] = []
    block_end = index - 5
    while block_end >= 4 and len(prior_blocks) < 5:
        block_start = block_end - 4
        block = list(bars[block_start: block_end + 1])
        if len(block) != 5 or not _consecutive(block):
            break
        prior_blocks.append(sum(volume(row) for row in block))
        block_end -= 5
    current_volume = sum(volume(row) for row in window)
    volume_baseline = statistics.median(prior_blocks) if prior_blocks else 0.0
    volume_ratio = current_volume / volume_baseline if volume_baseline > 0 else None
    delta_5m = sum(deltas)
    ofi_5m = sum(int(row.get("ofi") or 0) for row in window)
    delta_side = _sign(delta_5m)
    ofi_side = _sign(ofi_5m)
    imbalance_side = _majority(row.get("imbalance_1_10_side") for row in window)
    passive_side = _majority(row.get("passive_rejection_side") for row in window)
    queue_side = _majority(row.get("queue_side") for row in window)
    volume_burst = bool(volume_ratio is not None and volume_ratio >= 1.25)
    pressure_state = _delta_state(direction, delta_side, decay_ratio)
    breakout_flow = bool(
        direction != 0
        and delta_side == direction
        and ofi_side == direction
        and volume_burst
        and decay_ratio >= 0.75
    )
    consolidation_flow = bool(
        direction != 0
        and passive_side == direction
        and (decay_ratio < 0.75 or delta_side != direction)
    )
    return {
        "mbo_window_start_epoch": int(window[0]["epoch"]),
        "mbo_window_end_epoch": int(window[-1]["epoch"]),
        "mbo_volume_5m": current_volume,
        "mbo_volume_ratio_5m": round(volume_ratio, 6) if volume_ratio is not None else None,
        "mbo_volume_burst": volume_burst,
        "mbo_delta_5m": delta_5m,
        "mbo_delta_side": delta_side,
        "mbo_delta_abs_5m": current_abs,
        "mbo_delta_decay_ratio": round(decay_ratio, 6),
        "mbo_delta_state": pressure_state,
        "mbo_ofi_5m": ofi_5m,
        "mbo_ofi_side": ofi_side,
        "mbo_imbalance_side": imbalance_side,
        "mbo_passive_side": passive_side,
        "mbo_queue_side": queue_side,
        "mbo_breakout_flow": breakout_flow,
        "mbo_consolidation_flow": consolidation_flow,
    }


def _mbo_index(days: Sequence[dict[str, Any]]) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    index: dict[int, dict[str, Any]] = {}
    valid_days = 0
    date_keys: list[str] = []
    for day in days:
        bars = [bar for bar in day.get("bars") or [] if bar.get("ohlc_valid", True)]
        bars.sort(key=lambda row: int(row.get("epoch") or 0))
        if not bars:
            continue
        valid_days += 1
        date_keys.append(str(day.get("date")))
        for row_index, bar in enumerate(bars):
            features = dict(bar)
            features["_day_bars"] = bars
            features["_day_index"] = row_index
            index[int(bar["epoch"])] = features
    return index, {
        "rth_days": valid_days,
        "dates": sorted(date_keys),
        "schema_versions": dict(Counter(int(day.get("schema_version") or 0) for day in days)),
    }


def _context_for_trade(
    trade: Any,
    mbo_by_epoch: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    entry_time = as_utc(trade.entry_time)
    epoch = int(entry_time.timestamp())
    row = mbo_by_epoch.get(epoch)
    direction = _trade_direction(trade.direction)
    if row is None:
        return {"mbo_available": False, "mbo_missing_reason": "entry_epoch_not_in_cache"}
    features = _flow_features(row["_day_bars"], int(row["_day_index"]), direction)
    if features is None:
        return {"mbo_available": False, "mbo_missing_reason": "less_than_ten_consecutive_completed_minutes"}
    return {"mbo_available": True, **features}


def _load_gex_rows(path: Path = GEX_ROWS_PATH) -> dict[str, dict[str, dict[str, Any]]]:
    """Load only the frozen snapshot rows; no future outcome columns are used."""
    grouped: dict[str, dict[str, Any]] = defaultdict(dict)
    if not path.exists():
        return grouped
    with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
        for raw in csv.DictReader(handle):
            family = str(raw.get("gamma_family") or "").lower()
            trade_date = str(raw.get("date") or "")
            as_of = _parse_timestamp(raw.get("option_as_of"))
            if family not in {"oi", "volume"} or not trade_date or as_of is None:
                continue
            grouped[trade_date][family] = {
                "gex_snapshot_available": True,
                "gex_family": family,
                "gex_label": str(raw.get("gamma_label") or "unknown").lower(),
                "gex_state": int(_finite(raw.get("gamma_state"), 0.0)),
                "gex_value_shift": str(raw.get("value_shift") or "unknown"),
                "gex_entry_location": str(raw.get("entry_location") or "unknown"),
                "gex_snapshot_epoch": int(as_of.timestamp()),
                "gex_is_qqq_mnq_proxy": True,
            }
    return grouped


def _join_gex(
    row: dict[str, Any],
    gex_rows: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, Any]:
    trade_date = str(row.get("trade_date") or "")
    entry_epoch = int(row.get("entry_epoch") or 0)
    families = gex_rows.get(trade_date) or {}
    result: dict[str, Any] = {"gex_available": False, "gex_available_families": []}
    for family in ("oi", "volume"):
        snapshot = families.get(family)
        if not snapshot or entry_epoch < int(snapshot["gex_snapshot_epoch"]):
            continue
        result[f"gex_{family}_label"] = snapshot["gex_label"]
        result[f"gex_{family}_state"] = snapshot["gex_state"]
        result[f"gex_{family}_value_shift"] = snapshot["gex_value_shift"]
        result[f"gex_{family}_entry_location"] = snapshot["gex_entry_location"]
        result["gex_available"] = True
        result["gex_available_families"].append(family)
    if result["gex_available"]:
        result["gex_expected_mode_oi"] = (
            "breakout" if result.get("gex_oi_label") == "negative"
            else "consolidation" if result.get("gex_oi_label") == "positive"
            else "unknown"
        )
        result["gex_expected_mode_volume"] = (
            "breakout" if result.get("gex_volume_label") == "negative"
            else "consolidation" if result.get("gex_volume_label") == "positive"
            else "unknown"
        )
    return result


def _stats(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    values = [float(row.get("pnl") or 0.0) for row in rows]
    raw = series_stats(values)
    return {
        "n": int(raw["n"]),
        "pnl": round(float(raw["pnl"]), 2),
        "pf": round(float(raw["pf"]), 4),
        "win_rate": round(float(raw["win"]), 4),
        "max_dd": round(float(raw["max_dd"]), 2),
    }


def _grouped(rows: Sequence[dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        value = row.get(field)
        groups["unknown" if value is None else str(value)].append(row)
    return {key: _stats(groups[key]) for key in sorted(groups)}


def _trade_rows(trades: Iterable[Any], start: date, end: date) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for trade in sorted(trades, key=lambda item: as_utc(item.entry_time)):
        entry_time = as_utc(trade.entry_time)
        if entry_time.date() < start or entry_time.date() > end:
            continue
        meta = getattr(trade, "meta", {}) or {}
        rows.append(
            {
                "entry_time": entry_time.isoformat(),
                "entry_epoch": int(entry_time.timestamp()),
                "trade_date": entry_time.date().isoformat(),
                "direction": "long" if _trade_direction(trade.direction) > 0 else "short",
                "direction_int": _trade_direction(trade.direction),
                "edge": str(meta.get("edge") or "unknown"),
                "setup": str(meta.get("setup") or "unknown"),
                "regime": str(meta.get("regime") or "unknown"),
                "pnl": round(_finite(trade.pnl), 2),
            }
        )
    return rows


def _build_rows(
    trades: Iterable[Any],
    start: date,
    end: date,
    mbo_by_epoch: dict[int, dict[str, Any]],
    gex_rows: dict[str, dict[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    ordered = sorted(
        (
            trade for trade in trades
            if start <= as_utc(trade.entry_time).date() <= end
        ),
        key=lambda item: as_utc(item.entry_time),
    )
    rows = _trade_rows(ordered, start, end)
    for row, trade in zip(rows, ordered):
        row.update(_context_for_trade(trade, mbo_by_epoch))
        row.update(_join_gex(row, gex_rows))
        row["period"] = "discovery" if date.fromisoformat(row["trade_date"]) <= MBO_DISCOVERY_END else "evaluation"
        direction = int(row["direction_int"])
        edge_direction = 1 if row["edge"] == "vah" else -1 if row["edge"] == "val" else 0
        row["edge_direction_matches"] = bool(edge_direction and direction == edge_direction)
        for family in ("oi", "volume"):
            label = row.get(f"gex_{family}_label")
            row[f"gex_{family}_breakout_aligned"] = label == "negative" and row["edge_direction_matches"]
            row[f"gex_{family}_consolidation_aligned"] = label == "positive"
    return rows


def _run_vp(symbol: str, values: dict[str, Any], start: date, end: date) -> tuple[list[Any], int, int]:
    params = _params(symbol, values)
    snapshot = candle_store.load_snapshot(symbol, 1)
    run_start = datetime.combine(start - timedelta(days=45), datetime.min.time(), tzinfo=UTC)
    run_end = datetime.combine(end + timedelta(days=2), datetime.max.time(), tzinfo=UTC)
    candles = candle_store.select_range(snapshot, run_start, run_end)
    config = BacktestConfig(
        strategies=["trend"],
        symbol=symbol,
        interval="1m",
        initial_capital=INITIAL_CAPITAL,
        commission_rt=get_commission_rt(params.contract_id),
        fees_rt=get_fees_rt(params.contract_id),
        value_area_pct=0.80,
    )
    result = BacktestEngine(config=config, strategy_params=params, record_equity=False).run(candles)
    return result.trades, len(candles), len(result.trades)


def _load_mbo() -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    days = _prepare_days()
    return _mbo_index(days)


def _report_stats(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    available = [row for row in rows if row.get("mbo_available")]
    gex_available = [row for row in rows if row.get("gex_available")]
    contexts: dict[str, Any] = {
        "all": _stats(rows),
        "mbo_available": _stats(available),
        "mbo_breakout_flow": _stats([row for row in available if row.get("mbo_breakout_flow")]),
        "mbo_consolidation_flow": _stats([row for row in available if row.get("mbo_consolidation_flow")]),
        "mbo_delta_state": _grouped(available, "mbo_delta_state"),
        "by_edge": _grouped(available, "edge"),
        "by_period": _grouped(available, "period"),
        "gex_available": _stats(gex_available),
        "gex_oi_label": _grouped(gex_available, "gex_oi_label"),
        "gex_volume_label": _grouped(gex_available, "gex_volume_label"),
        "gex_oi_breakout_aligned": _grouped(gex_available, "gex_oi_breakout_aligned"),
        "gex_volume_breakout_aligned": _grouped(gex_available, "gex_volume_breakout_aligned"),
    }
    return contexts


def _stress_stats(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    payload = [
        {
            "pnl": row.get("pnl", 0.0),
            "direction": "buy" if int(row.get("direction_int") or 0) > 0 else "sell",
            "size": 1,
        }
        for row in rows
    ]
    if not payload:
        return {"n": 0, "pnl": 0.0, "pf": 0.0, "max_dd": 0.0}
    stressed = slip_injection(payload, [STRESS_TICKS])
    levels = (stressed or {}).get("levels") or []
    return levels[0].get("stats", {}) if levels else {"n": 0, "pnl": 0.0, "pf": 0.0, "max_dd": 0.0}


def _write(report: dict[str, Any], path: Path) -> tuple[Path, Path, Path]:
    path.parent.mkdir(parents=True, exist_ok=True)
    compact = json.loads(json.dumps(report, default=str))
    path.write_text(json.dumps(compact, ensure_ascii=False, indent=2), encoding="utf-8")
    rows = report.get("trades") or []
    csv_path = path.with_suffix(".csv")
    fields = [
        "trade_date", "entry_time", "direction", "edge", "setup", "period", "pnl",
        "mbo_available", "mbo_delta_5m", "mbo_delta_decay_ratio", "mbo_delta_state",
        "mbo_ofi_5m", "mbo_volume_ratio_5m", "mbo_volume_burst", "mbo_imbalance_side",
        "mbo_passive_side", "mbo_breakout_flow", "mbo_consolidation_flow",
        "gex_oi_label", "gex_volume_label", "gex_oi_breakout_aligned",
        "gex_volume_breakout_aligned",
    ]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    md_path = path.with_suffix(".md")
    summary = report["summary"]
    base = summary["all"]
    lines = [
        "# Volume Profile context audit",
        "",
        f"- Version: `{report['version']}`",
        f"- Candidate: `{json.dumps(report['candidate'], sort_keys=True)}`",
        f"- MBO coverage: `{report['coverage']['mbo_days']}` RTH days / `{report['coverage']['mbo_date_start']}` → `{report['coverage']['mbo_date_end']}`",
        f"- VP trades in coverage: `{base['n']}` / PnL `${base['pnl']:,.2f}` / PF `{base['pf']:.4f}`",
        f"- MBO-joinable trades: `{summary['mbo_available']['n']}`",
        f"- GEX-joinable trades: `{summary['gex_available']['n']}` (QQQ→MNQ proxy, frozen snapshot only)",
        "",
        "## Definitions",
        "",
        "- `mbo_delta_decay_ratio` = absolute delta in the last two completed minutes divided by absolute delta in the first three minutes of the causal five-minute window.",
        "- `mbo_breakout_flow` = direction-aligned delta and OFI, volume ratio ≥ 1.25, and no local delta decay (< 0.75).",
        "- `mbo_consolidation_flow` = direction-aligned passive rejection plus either local delta decay or opposing delta.",
        "- Negative gamma is labelled a breakout/continuation environment; positive gamma is labelled a consolidation/mean-reversion environment. This is a frozen research interpretation, not a production rule.",
        "",
        "## Context outcomes",
        "",
        "| Context | n | PnL | PF | Max DD |",
        "|---|---:|---:|---:|---:|",
    ]
    context_rows = (
        ("all", summary["all"]),
        ("MBO available", summary["mbo_available"]),
        ("MBO breakout flow", summary["mbo_breakout_flow"]),
        ("MBO consolidation flow", summary["mbo_consolidation_flow"]),
        ("GEX available", summary["gex_available"]),
    )
    for label, stats in context_rows:
        lines.append(f"| {label} | {stats['n']} | ${stats['pnl']:,.2f} | {stats['pf']:.4f} | ${stats['max_dd']:,.2f} |")
    lines.extend(["", "## Delta state", "", "| State | n | PnL | PF |", "|---|---:|---:|---:|"])
    for state, stats in summary["mbo_delta_state"].items():
        lines.append(f"| {state} | {stats['n']} | ${stats['pnl']:,.2f} | {stats['pf']:.4f} |")
    lines.extend(["", "## VP edge", "", "| Edge | n | PnL | PF |", "|---|---:|---:|---:|"])
    for edge, stats in summary["by_edge"].items():
        lines.append(f"| {edge} | {stats['n']} | ${stats['pnl']:,.2f} | {stats['pf']:.4f} |")
    lines.extend(["", "## GEX label", "", "| OI gamma label | n | PnL | PF |", "|---|---:|---:|---:|"])
    for label, stats in summary["gex_oi_label"].items():
        lines.append(f"| {label} | {stats['n']} | ${stats['pnl']:,.2f} | {stats['pf']:.4f} |")
    lines.extend(
        [
            "",
            "## Research decision",
            "",
            "- The context is useful for explaining whether an edge event had continuation pressure or defensive/decaying pressure.",
            "- It is not sufficient to promote a live gate: the MBO sample is short, GEX is a QQQ proxy, and the context labels were not tuned on an untouched long holdout.",
            "- Keep Volume Profile as the primary state machine.  Treat MBO/delta/GEX as an observation layer until a larger, cross-symbol, out-of-sample audit passes the same stress and stability gates.",
            "",
        ]
    )
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return path, md_path, csv_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default=SYMBOL, choices=(SYMBOL,))
    parser.add_argument("--report", default="", help="source Volume Profile research JSON")
    parser.add_argument("--out", default="", help="external JSON output path")
    parser.add_argument("--gex", default=str(GEX_ROWS_PATH), help="option-wall value-area gamma rows CSV.GZ")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.disable(logging.CRITICAL)
    symbol = args.symbol.upper()
    source_path = Path(args.report) if args.report else market_data.derived_path(
        "research", "volume_profile_research_mnq_2021_2025.json"
    )
    source = json.loads(source_path.read_text(encoding="utf-8"))
    selected = source.get("selected_training_candidate") or {}
    candidate = selected.get("parameters") or {}
    if not candidate:
        raise SystemExit("source report has no selected candidate")

    mbo_by_epoch, mbo_meta = _load_mbo()
    if not mbo_meta["dates"]:
        raise SystemExit("no MBO footprint cache available")
    start = date.fromisoformat(mbo_meta["dates"][0])
    end = date.fromisoformat(mbo_meta["dates"][-1])
    started = time.perf_counter()
    trades, candle_count, raw_trade_count = _run_vp(symbol, candidate, start, end)
    gex_rows = _load_gex_rows(Path(args.gex))
    rows = _build_rows(trades, start, end, mbo_by_epoch, gex_rows)
    summary = _report_stats(rows)
    summary["stress_14t_all"] = _stress_stats(rows)
    summary["stress_14t_mbo_available"] = _stress_stats([row for row in rows if row.get("mbo_available")])
    report = {
        "version": VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "symbol": symbol,
        "candidate": candidate,
        "source_report": str(source_path),
        "coverage": {
            "mbo_days": mbo_meta["rth_days"],
            "mbo_date_start": start.isoformat(),
            "mbo_date_end": end.isoformat(),
            "mbo_dates": mbo_meta["dates"],
            "mbo_schema_versions": mbo_meta["schema_versions"],
            "gex_rows": sum(len(value) for value in gex_rows.values()),
            "gex_date_start": min(gex_rows) if gex_rows else None,
            "gex_date_end": max(gex_rows) if gex_rows else None,
            "gex_source": str(Path(args.gex)),
            "gex_mapping": "QQQ option-wall/GEX snapshot mapped to MNQ; research proxy only",
        },
        "method": {
            "engine": "backend.backtest.engine.BacktestEngine",
            "mbo_window": "five completed one-minute bars ending at the completed entry minute",
            "delta_decay": "last two completed minutes absolute delta / first three completed minutes absolute delta",
            "breakout_flow_gate": "delta + OFI aligned, volume ratio >= 1.25, decay ratio >= 0.75",
            "consolidation_flow_gate": "passive rejection aligned and (decay ratio < 0.75 or delta opposing)",
            "gex_snapshot_rule": "attach only when entry epoch >= option_as_of on the same date",
            "mbo_discovery_end": MBO_DISCOVERY_END.isoformat(),
            "stress_ticks_round_trip": STRESS_TICKS,
            "production_changed": False,
        },
        "replay": {
            "candles": candle_count,
            "raw_trades_in_window": raw_trade_count,
            "trades_reported": len(rows),
            "elapsed_seconds": round(time.perf_counter() - started, 2),
        },
        "summary": summary,
        "trades": rows,
    }
    output = Path(args.out) if args.out else market_data.derived_path(
        "research", "volume_profile_context_mnq_mbo_gex.json"
    )
    json_path, md_path, csv_path = _write(report, output)
    print(f"Context audit: {len(rows)} VP trades; candidate={candidate}", flush=True)
    print(f"JSON: {json_path}", flush=True)
    print(f"Report: {md_path}", flush=True)
    print(f"CSV: {csv_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
