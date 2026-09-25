"""Research-only OI gate replay for the frozen MNQ Volume Profile strategy.

The OI rule is frozen from the prior 2021-2023 retained-trade sensitivity.
This runner applies it before the production strategy materializes a signal,
then lets the production BacktestEngine continue through its normal state,
entry, exit, and risk path. It writes no preset or live-trading state.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import logging
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.backtest.engine import BacktestConfig, BacktestEngine  # noqa: E402
from backend.db.models import get_commission_rt, get_fees_rt  # noqa: E402
from scripts.volume_profile_research import (  # noqa: E402
    INITIAL_CAPITAL,
    _attach_regimes,
    _load_window,
    _params,
    _robustness,
    _trade_rows,
)
from scripts.vp_theta_oi_5yr_study import (  # noqa: E402
    COMPLETE_CHAIN_STATES,
    FEATURE_FIELDS,
    OI_ROOT,
    SAVED_REPORT,
    TRAIN_START,
    _frozen_parameters,
    _join_trade_snapshot,
    _parse_oi_row,
    _stats,
    choose_snapshot_date,
    discover_oi_files,
    scan_publication_index,
)
from backend.timebase import as_utc  # noqa: E402


ET = ZoneInfo("America/New_York")
VERSION = "2026-09-22-vp-theta-oi-engine-gate-v1"
OI_RULE = {
    "name": "frozen_train_median_three_feature_gate",
    "calibration_origin": "2021-2023 complete-chain baseline-trade sensitivity",
    "conditions": {
        "oi_unsigned_total_lte": 6_916_378.5,
        "oi_put_call_ratio_lte": 1.64158113,
        "oi_near_expiry_share_calendar_dte_le_1_gt": 0.05444089,
    },
    "missing_or_partial_policy": "reject candidate; no OI evidence means no OI-confirmed entry",
}


def _rule_allows(context: dict[str, Any] | None, rule: dict[str, Any] = OI_RULE) -> bool:
    if not context or context.get("oi_chain_completeness") not in COMPLETE_CHAIN_STATES:
        return False
    values = {field: context.get(field) for field in FEATURE_FIELDS}
    if any(value is None for value in values.values()):
        return False
    conditions = rule["conditions"]
    return (
        float(values["oi_unsigned_total"]) <= conditions["oi_unsigned_total_lte"]
        and float(values["oi_put_call_ratio"]) <= conditions["oi_put_call_ratio_lte"]
        and float(values["oi_near_expiry_share_calendar_dte_le_1"])
        > conditions["oi_near_expiry_share_calendar_dte_le_1_gt"]
    )


class CausalOIProvider:
    """Load one monthly partition at a time and join only causal OI rows."""

    def __init__(
        self,
        root: Path = OI_ROOT,
        start: date = TRAIN_START,
        end: date = date(2026, 9, 15),
    ) -> None:
        self.paths = discover_oi_files(root, start, end)
        self.path_by_month = {
            path.name[len("month=") : -len(".csv.gz")]: path for path in self.paths
        }
        self.session_index = scan_publication_index(self.paths, start, end)["_sessions"]
        self._month_cache: dict[str, dict[date, list[dict[str, Any]]]] = {}
        self._context_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self.loaded_months: set[str] = set()
        self.parsed_rows = 0

    def _rows_for_month(self, month: str) -> dict[date, list[dict[str, Any]]]:
        if month in self._month_cache:
            return self._month_cache[month]
        path = self.path_by_month.get(month)
        if path is None:
            self._month_cache[month] = {}
            return {}
        grouped: dict[date, list[dict[str, Any]]] = {}
        with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as handle:
            for raw in csv.DictReader(handle):
                row = _parse_oi_row(raw, path)
                if row is None:
                    continue
                grouped.setdefault(row["timestamp_et_date"], []).append(row)
                self.parsed_rows += 1
        self.loaded_months.add(month)
        self._month_cache[month] = grouped
        # The replay is chronological, with one holdout rerun that starts its
        # warmup earlier. Keep only a small bounded cache across month changes.
        while len(self._month_cache) > 2:
            oldest = next(iter(self._month_cache))
            if oldest == month:
                break
            self._month_cache.pop(oldest)
        return grouped

    def context_at(self, timestamp: datetime) -> dict[str, Any]:
        entry = as_utc(timestamp)
        trade_date = entry.astimezone(ET).date()
        cache_key = (entry.isoformat(), trade_date.isoformat())
        cached = self._context_cache.get(cache_key)
        if cached is not None:
            return cached
        snapshot_date, status, reason = choose_snapshot_date(
            trade_date, entry, self.session_index
        )
        if snapshot_date is None:
            result = {
                "oi_join_status": "missing",
                "oi_snapshot_date_et": None,
                "oi_chain_completeness": "missing",
                "oi_missing_reason": reason or "no_causal_vendor_publication",
                **{field: None for field in FEATURE_FIELDS},
            }
        else:
            month = snapshot_date.strftime("%Y-%m")
            snapshot_rows = self._rows_for_month(month).get(snapshot_date, [])
            trade = {
                "entry_time_utc": entry.isoformat(),
                "entry_date_et": trade_date.isoformat(),
                "year": str(trade_date.year),
            }
            result = _join_trade_snapshot(
                trade,
                snapshot_rows,
                snapshot_date=snapshot_date,
                preferred_status=status,
                same_date_session_rows=self.session_index.get(trade_date, {}).get("rows", 0),
                same_date_reason=reason,
            )
        self._context_cache[cache_key] = result
        return result

    def metadata(self) -> dict[str, Any]:
        return {
            "oi_month_partitions": len(self.paths),
            "oi_months_loaded_for_join": len(self.loaded_months),
            "oi_rows_parsed_for_join": self.parsed_rows,
            "publication_dates_indexed": len(self.session_index),
            "context_queries_cached": len(self._context_cache),
            "context_policy": "same-date preferred; prior-date fallback; vendor timestamps <= candidate bar; complete-chain gate required",
        }


def install_gate(
    strategy: Any,
    provider: CausalOIProvider,
    *,
    active_start: date,
    active_end: date,
    rule: dict[str, Any] = OI_RULE,
) -> Counter[str]:
    """Wrap this run's strategy evaluator; production source stays untouched."""
    original_evaluate = strategy.evaluate
    counts: Counter[str] = Counter()

    def gated_evaluate(candle: Any, zones=None, is_mature: bool = True):
        local_date = as_utc(candle.timestamp).astimezone(ET).date()
        if not (active_start <= local_date <= active_end):
            return original_evaluate(candle, zones, is_mature)

        candidates = strategy._candidate_set(candle)
        if len(candidates) != 1:
            return original_evaluate(candle, zones, is_mature)

        counts["candidate_bars"] += 1
        context = provider.context_at(candle.timestamp)
        if context.get("oi_chain_completeness") not in COMPLETE_CHAIN_STATES:
            counts["rejected_missing_or_partial"] += 1
            return None
        if not _rule_allows(context, rule):
            counts["rejected_rule"] += 1
            return None

        counts["rule_pass_candidate_bars"] += 1
        signal = original_evaluate(candle, zones, is_mature)
        if signal is not None:
            signal.meta = dict(signal.meta or {})
            signal.meta["research_oi_gate"] = {
                "rule": rule["name"],
                "snapshot_date_et": context.get("oi_snapshot_date_et"),
                "chain_completeness": context.get("oi_chain_completeness"),
                "publication_max_utc": context.get("oi_publication_max_utc"),
                **{field: context.get(field) for field in FEATURE_FIELDS},
            }
            counts["signals_materialized"] += 1
        return signal

    strategy.evaluate = gated_evaluate
    return counts


def _engine(parameters: dict[str, Any], symbol: str) -> BacktestEngine:
    params = _params(symbol, parameters)
    config = BacktestConfig(
        strategies=["trend"],
        symbol=symbol,
        interval="1m",
        initial_capital=INITIAL_CAPITAL,
        commission_rt=get_commission_rt(params.contract_id),
        fees_rt=get_fees_rt(params.contract_id),
        value_area_pct=0.80,
    )
    return BacktestEngine(
        config=config,
        strategy_params=params,
        record_equity=False,
    )


def _run_gated(
    parameters: dict[str, Any],
    candles: list[Any],
    regimes: dict[str, dict[str, Any]],
    symbol: str,
    start: date,
    end: date,
    provider: CausalOIProvider,
) -> dict[str, Any]:
    engine = _engine(parameters, symbol)
    gate_counts = install_gate(
        engine.trend_follow,
        provider,
        active_start=start,
        active_end=end,
    )
    result = engine.run(candles)
    rows = _trade_rows(result.trades, start, end)
    _attach_regimes(rows, regimes)
    sorted_trades = sorted(result.trades, key=lambda item: as_utc(item.entry_time))
    for row, trade in zip(rows, sorted_trades):
        row["research_oi_gate"] = (getattr(trade, "meta", {}) or {}).get("research_oi_gate")
    return {
        "baseline": _robustness(rows, symbol),
        "by_year": {
            year: _stats([row for row in rows if row["year"] == year])
            for year in sorted({row["year"] for row in rows})
        },
        "trade_rows": rows,
        "gate_counts": dict(gate_counts),
    }


def _baseline_context_filter(
    rows: list[dict[str, Any]], provider: CausalOIProvider
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    eligible: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for row in rows:
        context = provider.context_at(row["entry_time"])
        if context.get("oi_chain_completeness") not in COMPLETE_CHAIN_STATES:
            counts["missing_or_partial"] += 1
            continue
        counts["complete_chain"] += 1
        if _rule_allows(context):
            eligible.append(row)
            counts["rule_pass"] += 1
    return eligible, dict(counts)


def _period_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_year: dict[str, Any] = {}
    for year in sorted({row["year"] for row in rows}):
        group = [row for row in rows if row["year"] == year]
        by_year[year] = {
            **_stats(group),
            "independent_dates": len({row.get("trade_date") for row in group}),
        }
    return {"overall": _stats(rows), "by_year": by_year}


def _entry_stream_comparison(
    baseline_rows: list[dict[str, Any]], gated_rows: list[dict[str, Any]]
) -> dict[str, int]:
    def key(row: dict[str, Any]) -> str:
        value = row.get("entry_time_utc") or row.get("entry_time")
        return as_utc(value).isoformat() if isinstance(value, datetime) else str(value)

    baseline_times = {key(row) for row in baseline_rows}
    gated_times = {key(row) for row in gated_rows}
    return {
        "baseline_entries": len(baseline_times),
        "engine_gate_entries": len(gated_times),
        "same_entry_times": len(baseline_times & gated_times),
        "baseline_entry_times_absent_after_gate": len(baseline_times - gated_times),
        "new_or_shifted_entry_times_after_gate": len(gated_times - baseline_times),
    }


def run_study(
    report_path: Path = SAVED_REPORT,
    oi_root: Path = OI_ROOT,
) -> dict[str, Any]:
    from scripts.vp_theta_oi_5yr_study import replay_frozen_baseline

    parity = replay_frozen_baseline(report_path)
    if parity.get("status") != "parity_passed":
        raise RuntimeError("frozen VP baseline parity failed; stopped before OI-gated replay")

    saved = parity["_replay_report"]
    parameters = _frozen_parameters(saved)
    train_start = date.fromisoformat((saved.get("training_window") or {})["start"])
    train_end = date.fromisoformat((saved.get("training_window") or {})["end"])
    holdout_start = date.fromisoformat((saved.get("holdout_window") or {})["start"])
    holdout_end = date.fromisoformat((saved.get("holdout_window") or {})["end"])
    provider = CausalOIProvider(oi_root, train_start, holdout_end)

    parity_rows = parity["_trade_rows"]
    train_baseline_rows = [row for row in parity_rows if row.get("period") == "training"]
    holdout_baseline_rows = [row for row in parity_rows if row.get("period") == "holdout"]

    train_candles, train_regimes = _load_window("MNQ", train_start, train_end)
    train_gate = _run_gated(
        parameters,
        train_candles,
        train_regimes,
        "MNQ",
        train_start,
        train_end,
        provider,
    )
    train_frozen_filter, train_filter_counts = _baseline_context_filter(
        train_baseline_rows, provider
    )

    holdout_candles, holdout_regimes = _load_window("MNQ", holdout_start, holdout_end)
    holdout_gate = _run_gated(
        parameters,
        holdout_candles,
        holdout_regimes,
        "MNQ",
        holdout_start,
        holdout_end,
        provider,
    )
    holdout_frozen_filter, holdout_filter_counts = _baseline_context_filter(
        holdout_baseline_rows, provider
    )

    parity_summary = {
        key: value for key, value in parity.items() if not key.startswith("_")
    }

    return {
        "status": "completed",
        "version": VERSION,
        "generated_at": datetime.now().astimezone().isoformat(),
        "baseline_parity": parity_summary,
        "symbol": "MNQ",
        "parameters": parameters,
        "oi_rule": OI_RULE,
        "method": {
            "engine": "production BacktestEngine and VolumeProfileStrategy",
            "gate_location": "before TradeSignal creation; rejected candidates leave VP edge/daily locks untouched and later bars are evaluated normally",
            "missing_policy": OI_RULE["missing_or_partial_policy"],
            "windows": {
                "training": [train_start.isoformat(), train_end.isoformat()],
                "holdout": [holdout_start.isoformat(), holdout_end.isoformat()],
            },
        },
        "training": {
            "baseline": {
                **_period_summary(train_baseline_rows),
                "robustness": _robustness(train_baseline_rows, "MNQ"),
            },
            "frozen_row_filter": {
                **_period_summary(train_frozen_filter),
                "coverage": train_filter_counts,
            },
            "engine_gate": {
                **{key: value for key, value in train_gate.items() if key != "trade_rows"},
                "trade_rows": train_gate["trade_rows"],
            },
            "entry_stream_comparison": _entry_stream_comparison(
                train_baseline_rows, train_gate["trade_rows"]
            ),
        },
        "holdout": {
            "baseline": {
                **_period_summary(holdout_baseline_rows),
                "robustness": _robustness(holdout_baseline_rows, "MNQ"),
            },
            "frozen_row_filter": {
                **_period_summary(holdout_frozen_filter),
                "coverage": holdout_filter_counts,
            },
            "engine_gate": {
                **{key: value for key, value in holdout_gate.items() if key != "trade_rows"},
                "trade_rows": holdout_gate["trade_rows"],
            },
            "entry_stream_comparison": _entry_stream_comparison(
                holdout_baseline_rows, holdout_gate["trade_rows"]
            ),
        },
        "oi_input": provider.metadata(),
    }


def _print_summary(result: dict[str, Any]) -> None:
    summary = {key: value for key, value in result.items() if key not in {"training", "holdout"}}
    for period in ("training", "holdout"):
        block = result[period]
        summary[period] = {
            label: {
                "overall": section.get("overall") or section.get("baseline"),
                "robustness": section.get("robustness"),
                "by_year": section.get("by_year"),
                "coverage": section.get("coverage"),
                "gate_counts": section.get("gate_counts"),
            }
            for label, section in block.items()
            if label != "entry_stream_comparison"
        }
        summary[period]["entry_stream_comparison"] = block["entry_stream_comparison"]
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=SAVED_REPORT)
    parser.add_argument("--oi-root", type=Path, default=OI_ROOT)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    result = run_study(args.report, args.oi_root)
    _print_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
