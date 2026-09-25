"""Read-only factual audit of canonical PI paths and saved VP/GEX rows."""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.data import market_data  # noqa: E402
from backend.data.pi_history import (  # noqa: E402
    load_rows,
    parse_ts,
    row_has_multiple_signals,
    row_is_pre_session,
)
from backend.live.pi_listener import DIRECTION  # noqa: E402
from scripts.pi_underlying_path_study_20260921 import (  # noqa: E402
    FOCUS_DATES,
    NY,
    analyze_signal,
    canonical_buy_signals,
    load_qqq_rth_bars,
    pooled_horizon_summary,
)
from scripts.volume_profile_context_study import (  # noqa: E402
    _join_gex,
    _load_gex_rows,
    _parse_timestamp,
    _stats,
)

HORIZONS = (1, 2, 3, 5)
FAMILIES = ("oi", "volume")
GEX_ROWS_NAME = "option_wall_value_area_gamma_rows.csv.gz"
VP_REPORT_NAME = "volume_profile_context_mnq_mbo_gex.json"
VP_ROWS_NAME = "volume_profile_context_mnq_mbo_gex.csv"


def _first_pass_counts(events: list[dict[str, Any]], horizon: int) -> dict[str, Any]:
    categories = Counter(
        event["horizons"][str(horizon)]["first_pass_0_5"]
        for event in events
        if event.get("horizons", {}).get(str(horizon), {}).get("valid")
    )
    return {
        "down_first": categories.get("down_only", 0) + categories.get("down_first_then_up", 0),
        "up_first": categories.get("up_only", 0) + categories.get("up_first_then_down", 0),
        "same_bar_ambiguous": categories.get("same_bar_ambiguous", 0),
        "neither_threshold_hit": categories.get("neither", 0),
        "categories": dict(sorted(categories.items())),
    }


def _path_summary(events: list[dict[str, Any]], horizon: int) -> dict[str, Any]:
    raw = pooled_horizon_summary(events, str(horizon))
    valid = [
        event for event in events
        if event.get("horizons", {}).get(str(horizon), {}).get("valid")
    ]
    return {
        "n": raw["n"],
        "independent_entry_dates": len({event.get("entry_session") for event in valid}),
        "positive_close_count": raw["positive_close_count"],
        "negative_or_flat_close_count": raw["n"] - raw["positive_close_count"],
        "mean_return_pct": raw["mean_return_pct"],
        "median_return_pct": raw["median_return_pct"],
        "mean_return_dollars": raw["mean_return_dollars"],
        "median_return_dollars": raw["median_return_dollars"],
        "mean_mfe_pct": raw["mean_mfe_pct"],
        "median_mfe_pct": raw["median_mfe_pct"],
        "mean_mfe_dollars": raw["mean_mfe_dollars"],
        "median_mfe_dollars": raw["median_mfe_dollars"],
        "mean_mae_pct": raw["mean_mae_pct"],
        "median_mae_pct": raw["median_mae_pct"],
        "mean_mae_dollars": raw["mean_mae_dollars"],
        "median_mae_dollars": raw["median_mae_dollars"],
        "first_pass_at_plus_minus_0_50_percent": _first_pass_counts(events, horizon),
    }


def _pi_inventory(rows: list[dict[str, Any]], raw_rows: list[dict[str, Any]]) -> dict[str, Any]:
    marks: Counter[tuple[str, str, str]] = Counter()
    symbol_marks: Counter[str] = Counter()
    source_days: dict[str, set[str]] = defaultdict(set)
    stamps = []
    for row in rows:
        symbol = str(row.get("symbol") or "unknown").upper()
        try:
            stamp = parse_ts(row["ts"])
        except Exception:
            continue
        stamps.append(stamp)
        for mark in row.get("marks") or []:
            if not isinstance(mark, dict):
                continue
            key = (symbol, str(mark.get("level") if mark.get("level") is not None else "unknown"), str(mark.get("kind") or "unknown"))
            marks[key] += 1
            symbol_marks[symbol] += 1
            source_days[symbol].add(stamp.astimezone(NY).date().isoformat())

    kept_ids = {str(row.get("id") or "") for row in rows}
    excluded_focus = []
    for row in raw_rows:
        try:
            stamp = parse_ts(row["ts"])
        except Exception:
            continue
        day = stamp.astimezone(NY).date()
        if day not in FOCUS_DATES or str(row.get("symbol") or "").upper() != "QQQ":
            continue
        if not any(
            mark.get("kind") == "深蓝圈" and str(mark.get("level")) == "2"
            for mark in (row.get("marks") or []) if isinstance(mark, dict)
        ):
            continue
        if str(row.get("id") or "") in kept_ids:
            continue
        reason = (
            "pre_session_replay"
            if row_is_pre_session(row)
            else "multi_mark_aggregate"
            if row_has_multiple_signals(row)
            else "other_loader_filter"
        )
        excluded_focus.append({
            "date_ny": day.isoformat(),
            "source_ts_utc": stamp.isoformat(),
            "symbol": "QQQ",
            "message_id": str(row.get("id") or ""),
            "marks": [
                {"kind": mark.get("kind"), "source_level": mark.get("level")}
                for mark in (row.get("marks") or []) if isinstance(mark, dict)
            ],
            "canonical_exclusion_reason": reason,
        })

    return {
        "raw_message_rows": len(raw_rows),
        "canonical_message_rows": len(rows),
        "filtered_message_rows": len(raw_rows) - len(rows),
        "canonical_mark_rows": sum(marks.values()),
        "marks_by_symbol": dict(sorted(symbol_marks.items())),
        "independent_source_dates_by_symbol": {
            symbol: len(days) for symbol, days in sorted(source_days.items())
        },
        "mark_counts": [
            {"symbol": symbol, "source_level": level, "kind": kind, "marks": count}
            for (symbol, level, kind), count in sorted(marks.items())
        ],
        "source_timestamp_provenance": {
            "field": "ts",
            "first_utc": min(stamps).isoformat() if stamps else None,
            "last_utc": max(stamps).isoformat() if stamps else None,
            "received_at_present": any("received_at" in row for row in rows),
            "discord_timestamp_present": any("discord_timestamp" in row for row in rows),
            "loader": "backend.data.pi_history.load_rows(); filters pre-session replay and multi-mark aggregate rows",
        },
        "excluded_focus_qqq_level2_deepblue_rows": excluded_focus,
    }


def _pi_focus_gex(events: list[dict[str, Any]], gex_rows: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
    joined_events = []
    timestamp_violations = Counter()
    join_status = {family: Counter() for family in FAMILIES}
    for event in events:
        stamp = datetime.fromisoformat(event["source_ts_utc"].replace("Z", "+00:00"))
        day = stamp.astimezone(NY).date().isoformat()
        joined = _join_gex({"trade_date": day, "entry_epoch": int(stamp.timestamp())}, gex_rows)
        for family in FAMILIES:
            snapshot = (gex_rows.get(day) or {}).get(family)
            if family in joined["gex_available_families"] and snapshot:
                join_status[family]["causal_timestamp_join"] += 1
                timestamp_violations[family] += int(snapshot["gex_snapshot_epoch"] > stamp.timestamp())
            elif snapshot:
                join_status[family]["source_before_option_as_of"] += 1
            else:
                join_status[family]["no_same_date_snapshot"] += 1
        joined_events.append({**event, "source_day": day, **joined})

    by_family = {}
    for family in FAMILIES:
        rows = []
        for label in ("negative", "positive", "unknown"):
            group = [row for row in joined_events if (row.get(f"gex_{family}_label") or "unknown") == label]
            if not group:
                continue
            rows.append({
                "gamma_label": label,
                "expected_mode": (group[0].get(f"gex_expected_mode_{family}") or "unknown"),
                "pi_direction": "long",
                "signal_marks": len(group),
                "independent_source_dates": len({row["source_day"] for row in group}),
                "horizons": {
                    str(horizon): _path_summary(group, horizon)
                    for horizon in HORIZONS
                },
            })
        by_family[family] = {
            "causally_joined_marks": sum(family in row.get("gex_available_families", []) for row in joined_events),
            "causally_joined_dates": len({row["source_day"] for row in joined_events if family in row.get("gex_available_families", [])}),
            "join_status_counts": dict(sorted(join_status[family].items())),
            "future_timestamp_violations": timestamp_violations[family],
            "groups": rows,
        }
    return {
        "cohort": "canonical QQQ source-level-2 deep-blue marks on the six previously identified focus dates",
        "marks": len(joined_events),
        "independent_source_dates": len({row["source_day"] for row in joined_events}),
        "causal_join_clock": "same New York session date and option_as_of <= PI source ts",
        "received_at_available": False,
        "by_family": by_family,
    }


def _gex_coverage(path: Path, gex_rows: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
    snapshots = [item for families in gex_rows.values() for item in families.values()]
    epochs = [item["gex_snapshot_epoch"] for item in snapshots]
    with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as handle:
        columns = next(csv.reader(handle), [])
    return {
        "snapshot_rows": len(snapshots),
        "independent_dates": len(gex_rows),
        "first_date": min(gex_rows) if gex_rows else None,
        "last_date": max(gex_rows) if gex_rows else None,
        "rows_by_family": dict(sorted(Counter(item["gex_family"] for item in snapshots).items())),
        "option_as_of_utc_min": datetime.fromtimestamp(min(epochs), timezone.utc).isoformat() if epochs else None,
        "option_as_of_utc_max": datetime.fromtimestamp(max(epochs), timezone.utc).isoformat() if epochs else None,
        "future_outcome_columns_present_but_unused_by_join": [
            column for column in ("price_30m", "price_60m", "price_close", "boundary_event_status")
            if column in columns
        ],
        "semantics": {
            "oi": "QQQ 0DTE OI-derived gamma proxy; no dealer-side/inventory observation",
            "volume": "QQQ 0DTE volume-derived proxy; unsigned volume with assumed calls-positive/puts-negative signs",
        },
    }


def _load_saved_vp_rows(path: Path, gex_rows: dict[str, dict[str, dict[str, Any]]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        saved = list(csv.DictReader(handle))
    rows = []
    mismatches = Counter()
    for raw in saved:
        entry_time = _parse_timestamp(raw.get("entry_time"))
        if entry_time is None:
            continue
        joined = _join_gex(
            {"trade_date": raw.get("trade_date"), "entry_epoch": int(entry_time.timestamp())},
            gex_rows,
        )
        row = {**raw, "pnl": float(raw.get("pnl") or 0.0), "gex_join": joined}
        row["gex_available_families"] = joined["gex_available_families"]
        row["gex_join_reason"] = (
            "causal_timestamp_join"
            if joined["gex_available"]
            else "entry_before_option_as_of"
            if gex_rows.get(str(raw.get("trade_date") or ""))
            else "no_same_date_snapshot"
        )
        for family in FAMILIES:
            derived = joined.get(f"gex_{family}_label")
            stored = raw.get(f"gex_{family}_label") or None
            if derived != stored:
                mismatches[family] += 1
            row[f"audit_{family}_label"] = derived or "unknown"
            row[f"audit_{family}_mode"] = joined.get(f"gex_expected_mode_{family}") or "unknown"
        rows.append(row)
    return rows, {
        "saved_csv_rows": len(saved),
        "timestamp_rejoined_rows": len(rows),
        "stored_label_mismatches": dict(sorted(mismatches.items())),
    }


def _vp_summary(rows: list[dict[str, Any]], saved_report: dict[str, Any], checks: dict[str, Any]) -> dict[str, Any]:
    matched = [row for row in rows if row["gex_join"]["gex_available"]]
    result = {
        "saved_candidate": saved_report.get("candidate"),
        "trades": len(rows),
        "independent_trade_dates": len({row["trade_date"] for row in rows}),
        "baseline_all_rows": _stats(rows),
        "baseline_on_common_gex_matched_rows": _stats(matched),
        "baseline_on_gex_unmatched_rows": _stats([row for row in rows if not row["gex_join"]["gex_available"]]),
        "matched_trade_rows": len(matched),
        "unmatched_trade_rows": len(rows) - len(matched),
        "matched_independent_trade_dates": len({row["trade_date"] for row in matched}),
        "gex_join_status_counts": dict(Counter(row["gex_join_reason"] for row in rows)),
        "direction_counts": dict(Counter(str(row.get("direction") or "unknown") for row in rows)),
        "setup_counts": dict(Counter(str(row.get("setup") or "unknown") for row in rows)),
        "period_counts": dict(Counter(str(row.get("period") or "unknown") for row in rows)),
        "gex_families": {},
        "paired_gate_on_off_rows_saved": False,
        "comparison_limit": "Saved rows are baseline trades with context tags; no GEX-gated counterfactual trade stream exists. All groups retain the same saved trade PnL and cost assumptions.",
        "saved_report_baseline_n": (saved_report.get("summary", {}).get("all") or {}).get("n"),
        "saved_report_gex_available_n": (saved_report.get("summary", {}).get("gex_available") or {}).get("n"),
        "timestamp_rejoin_checks": checks,
    }
    for family in FAMILIES:
        by_label = {}
        for label in ("positive", "negative", "unknown"):
            group = [row for row in rows if row[f"audit_{family}_label"] == label]
            modes = sorted({row[f"audit_{family}_mode"] for row in group})
            by_direction_mode = {}
            for direction in ("long", "short"):
                for mode in modes:
                    subset = [
                        row for row in group
                        if str(row.get("direction") or "").lower() == direction
                        and row[f"audit_{family}_mode"] == mode
                    ]
                    if subset:
                        by_direction_mode[f"{direction}/{mode}"] = {
                            **_stats(subset),
                            "independent_trade_dates": len({row["trade_date"] for row in subset}),
                        }
            by_label[label] = {
                **_stats(group),
                "independent_trade_dates": len({row["trade_date"] for row in group}),
                "matched_rows": sum(family in row["gex_available_families"] for row in group),
                "unmatched_rows": sum(family not in row["gex_available_families"] for row in group),
                "expected_mode": {"positive": "consolidation", "negative": "breakout", "unknown": "unknown"}[label],
                "by_direction_mode": by_direction_mode,
            }
        result["gex_families"][family] = {"matched_trade_rows": sum(family in row["gex_available_families"] for row in rows), "by_label": by_label}
    return result


def build_audit() -> dict[str, Any]:
    root = market_data.configured_market_data_root()
    pi_path = market_data.pi_source_root(root) / "pi_signals.json"
    option_root = market_data.option_wall_root(root)
    report_path = market_data.derived_path("research", VP_REPORT_NAME)
    vp_rows_path = report_path.with_suffix(".csv")
    gex_path = option_root / GEX_ROWS_NAME

    raw_pi = load_rows(include_pre_session=True, path=pi_path)
    pi_rows = load_rows(path=pi_path)
    inventory = _pi_inventory(pi_rows, raw_pi)
    buy_signals, buy_dedup_removed = canonical_buy_signals(pi_rows)
    qqq_bars, bar_coverage = load_qqq_rth_bars(option_root)
    focus_all = [
        signal for signal in buy_signals
        if signal.kind == "深蓝圈" and signal.level == 2
        and signal.ts.astimezone(NY).date() in FOCUS_DATES
    ]
    focus_qqq = [signal for signal in focus_all if signal.symbol == "QQQ"]
    focus_events = [analyze_signal(signal, qqq_bars) for signal in focus_qqq]
    gex_rows = _load_gex_rows(gex_path)
    pi_gex = _pi_focus_gex(focus_events, gex_rows)
    saved_report = json.loads(report_path.read_text(encoding="utf-8"))
    vp_rows, vp_checks = _load_saved_vp_rows(vp_rows_path, gex_rows)

    return {
        "audit_date": "2026-09-21",
        "scope": "read-only saved-data audit; underlying returns remain spot-path outcomes, with no option PnL inference",
        "source_paths": {
            "market_data_root": str(root),
            "pi_history": str(pi_path),
            "qqq_underlying": str(option_root / "raw" / "YYYY-MM-DD" / "qqq_ohlcv_1m.csv.gz"),
            "option_gamma_rows": str(gex_path),
            "saved_vp_report": str(report_path),
            "saved_vp_trade_rows": str(vp_rows_path),
        },
        "pi_inventory": inventory,
        "qqq_underlying_paths": {
            "bar_coverage": bar_coverage,
            "focus_source_level2_deepblue_total_across_symbols": len(focus_all),
            "focus_qqq_marks": len(focus_events),
            "independent_qqq_event_dates": len({event["entry_session"] for event in focus_events}),
            "entry_rule": "existing pi_underlying_path_study: next eligible 1-minute QQQ bar after source ts",
            "threshold_rule": "first touch of reference × (1 ± 0.50%); 1-minute OHLC; dual-touch bars stay ambiguous",
            "horizons": {
                str(horizon): _path_summary(focus_events, horizon)
                for horizon in HORIZONS
            },
            "retrospective_only": True,
            "option_pnl_computed": False,
        },
        "gex_coverage_and_semantics": _gex_coverage(gex_path, gex_rows),
        "pi_focus_gex_partition": pi_gex,
        "saved_volume_profile_gex_rows": _vp_summary(vp_rows, saved_report, vp_checks),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(build_audit(), ensure_ascii=False, indent=None if args.compact else 2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
