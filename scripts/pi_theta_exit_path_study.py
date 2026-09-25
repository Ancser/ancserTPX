"""Bounded offline exit-path study for the five saved QQQ PI episodes."""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import fmean, median
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.pi_theta_option_pilot import (
    CONTRACT_MULTIPLIER_ASSUMPTION,
    DEFAULT_DATA_ROOT,
    _bid_mark_quality,
    _entry_selection,
    _path_excursions,
    _quote_file,
    _quote_quality,
    _stamp_text,
    _verify_inventory,
    _manifest_records,
    canonical_episode_specs,
    pilot_files,
)


ROUND_TRIP_COMMISSION = 1.30
PREMIUM_BUDGET = 1000.0
BRACKET_RULES: dict[str, dict[str, float]] = {
    "A": {"stop_multiple": 0.75, "take_multiple": 1.50},
    "B": {"stop_multiple": 0.50, "take_multiple": 2.00},
}


def _usable_bid_observations(
    event_quotes: dict[datetime, dict[str, str]],
    next_quotes: dict[datetime, dict[str, str]],
    entry_time: datetime,
    cap_time: datetime,
    entry_row: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Return the entry bid and later mark-eligible bids through the cap."""
    observations: list[dict[str, Any]] = []

    if entry_row is not None:
        mark, executable, economic_zero, no_executable, values = _bid_mark_quality(entry_row)
        if mark and values["bid"] is not None:
            observations.append({
                "status": "mark",
                "timestamp": entry_time,
                "timestamp_et": _stamp_text(entry_time),
                "source": "entry",
                "bid": values["bid"],
                "ask": values["ask"],
                "bid_size": values["bid_size"],
                "ask_size": values["ask_size"],
                "executable_bid": executable,
                "mark_only": not executable,
                "economic_zero_bid": economic_zero,
                "no_executable_bid": no_executable,
            })

    for source, quotes in (("event", event_quotes), ("next_session", next_quotes)):
        for stamp, row in quotes.items():
            if not entry_time < stamp <= cap_time:
                continue
            mark, executable, economic_zero, no_executable, values = _bid_mark_quality(row)
            if not mark or values["bid"] is None:
                continue
            observations.append({
                "status": "mark",
                "timestamp": stamp,
                "timestamp_et": _stamp_text(stamp),
                "source": source,
                "bid": values["bid"],
                "ask": values["ask"],
                "bid_size": values["bid_size"],
                "ask_size": values["ask_size"],
                "executable_bid": executable,
                "mark_only": not executable,
                "economic_zero_bid": economic_zero,
                "no_executable_bid": no_executable,
            })
    observations.sort(key=lambda item: item["timestamp"])
    return observations


def first_touch(
    observations: list[dict[str, Any]],
    entry_ask: float,
    stop_multiple: float,
    take_multiple: float,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return the first actual observed bid crossing either fixed threshold."""
    stop_price = entry_ask * stop_multiple
    take_price = entry_ask * take_multiple
    for observation in observations:
        bid = float(observation["bid"])
        if bid <= stop_price:
            return observation, "stop"
        if bid >= take_price:
            return observation, "take"
    return None, None


def _quote_observation(
    quotes: dict[datetime, dict[str, str]], stamp: datetime,
) -> dict[str, Any]:
    row = quotes.get(stamp)
    if row is None:
        return {
            "status": "missing",
            "source": "next_session",
            "timestamp_et": _stamp_text(stamp),
            "quote_missing": True,
            "quote_invalid": False,
            "invalid_reasons": [],
        }
    _, reasons, values = _quote_quality(row)
    mark, executable, economic_zero, no_executable, mark_values = _bid_mark_quality(row)
    return {
        "status": "mark" if mark else "invalid",
        "source": "next_session",
        "timestamp_et": _stamp_text(stamp),
        "quote_missing": False,
        "quote_invalid": bool(reasons) or not mark,
        "invalid_reasons": reasons,
        "bid": mark_values["bid"],
        "ask": mark_values["ask"],
        "bid_size": mark_values["bid_size"],
        "ask_size": mark_values["ask_size"],
        "mark_eligible": mark,
        "executable_bid": executable,
        "mark_only": bool(mark and not executable),
        "economic_zero_bid": economic_zero,
        "no_executable_bid": no_executable,
    }


def _entry_ask_quote(
    quotes: dict[datetime, dict[str, str]], stamp: datetime,
) -> tuple[dict[str, str] | None, list[str]]:
    """Validate a buy-at-ask row with ask-side size as the entry requirement."""
    row = quotes.get(stamp)
    if row is None:
        return None, []
    _, reasons, values = _quote_quality(row)
    if (
        "nonpositive_quote_size" in reasons
        and values["bid_size"] == 0
        and values["ask_size"] is not None
        and values["ask_size"] > 0
    ):
        reasons = [reason for reason in reasons if reason != "nonpositive_quote_size"]
    return row, reasons


def _budget_contracts(entry_ask: float, budget: float) -> tuple[int, str | None]:
    premium_cost = entry_ask * CONTRACT_MULTIPLIER_ASSUMPTION
    if not math.isfinite(premium_cost) or premium_cost <= 0:
        return 0, "invalid_entry_premium"
    count = math.floor(budget / premium_cost + 1e-12)
    return count, None if count > 0 else "premium_exceeds_budget"


def _case_result(
    *,
    scenario: str,
    entry_ask: float,
    observation: dict[str, Any],
    reason: str,
    commission: float,
    budget: float,
    trigger_multiple: float | None = None,
    scanned_observations: int | None = None,
) -> dict[str, Any]:
    if observation.get("status") != "mark" or observation.get("bid") is None:
        return {
            "scenario": scenario,
            "status": "skipped",
            "skip_reason": "exit_quote_missing" if observation.get("quote_missing") else "exit_quote_unusable",
            "exit_time_et": observation.get("timestamp_et"),
            "exit_reason": reason,
            "exit_quote_missing": observation.get("quote_missing", False),
            "exit_quote_invalid": observation.get("quote_invalid", False),
            "invalid_reasons": observation.get("invalid_reasons", []),
        }
    bid = float(observation["bid"])
    gross = (bid - entry_ask) * CONTRACT_MULTIPLIER_ASSUMPTION
    net = gross - commission
    budget_contracts, budget_skip = _budget_contracts(entry_ask, budget)
    result = {
        "scenario": scenario,
        "status": "ok",
        "skip_reason": None,
        "exit_time_et": observation["timestamp_et"],
        "exit_reason": reason,
        "exit_source": observation.get("source"),
        "exit_bid": bid,
        "exit_ask": observation.get("ask"),
        "exit_bid_size": observation.get("bid_size"),
        "exit_ask_size": observation.get("ask_size"),
        "exit_quote_missing": False,
        "exit_quote_invalid": observation.get("quote_invalid", False),
        "invalid_reasons": observation.get("invalid_reasons", []),
        "exit_mark_eligible": True,
        "exit_bid_executable": observation.get("executable_bid", False),
        "exit_mark_only": observation.get("mark_only", False),
        "exit_execution_status": (
            "executable_bid" if observation.get("executable_bid", False) else "mark_only_bid"
        ),
        "quote_executable_exit": bool(observation.get("executable_bid", False)),
        "economic_zero_bid_observed": observation.get("economic_zero_bid", False),
        "no_executable_bid": observation.get("no_executable_bid", False),
        "gross_pnl_per_contract_usd": round(gross, 2),
        "net_pnl_per_contract_usd": round(net, 2),
        "premium_return_pct_gross": round((bid / entry_ask - 1) * 100, 4),
        "commission_round_trip_usd": commission,
        "budget_usd": budget,
        "budget_contracts": budget_contracts,
        "budget_skip_reason": budget_skip,
        "budget_net_pnl_usd": round(net * budget_contracts, 2) if budget_contracts else None,
        "trigger_threshold_multiple": trigger_multiple,
        "trigger_actual_multiple": round(bid / entry_ask, 8),
        "gap_from_trigger_pct": round((bid / (entry_ask * trigger_multiple) - 1) * 100, 4)
        if trigger_multiple is not None else None,
        "scanned_observations": scanned_observations,
    }
    return result


def _entry_skip(selection: str, entry_row: dict[str, str] | None, reasons: list[str]) -> dict[str, Any]:
    return {
        "selection": selection,
        "status": "skipped",
        "skip_reason": "entry_quote_missing" if entry_row is None else "entry_quote_unusable",
        "entry_invalid_reasons": reasons,
        "scenarios": [],
    }


def run_episode(
    data_root: Path,
    spec: Any,
    provenance: dict[str, str],
    commission: float,
    budget: float,
) -> dict[str, Any]:
    root = Path(data_root)
    relative_files = pilot_files(spec)
    manifest = _manifest_records(root)
    inventory = _verify_inventory(root, manifest, relative_files)
    event_greeks = root / relative_files[3]
    spot, selected, selection_details, _ = _entry_selection(
        event_greeks,
        entry_time=spec.entry_datetime,
        expiration=spec.expiration,
    )
    quote_diagnostics: dict[str, dict[str, Any]] = {}
    quote_series: dict[str, dict[tuple[float, str], dict[datetime, dict[str, str]]]] = {}
    for label, relative in (
        (spec.event_date, relative_files[2]),
        (spec.exit_date, relative_files[4]),
    ):
        quote_diagnostics[label], quote_series[label] = _quote_file(root / relative, set(selected.values()))

    cap_time = spec.exit_datetimes["next_day_15_30_et"]
    cases: list[dict[str, Any]] = []
    for label, strike in selected.items():
        key = (strike, "CALL")
        event_quotes = quote_series[spec.event_date].get(key, {})
        next_quotes = quote_series[spec.exit_date].get(key, {})
        entry_row, entry_reasons = _entry_ask_quote(event_quotes, spec.entry_datetime)
        if entry_row is None or entry_reasons:
            cases.append(_entry_skip(label, entry_row, entry_reasons))
            continue
        entry_values = _quote_quality(entry_row)[2]
        entry_ask = entry_values["ask"]
        entry_bid = entry_values["bid"]
        if entry_ask is None or entry_ask <= 0:
            cases.append(_entry_skip(label, entry_row, ["nonpositive_entry_ask"]))
            continue
        full_path = _path_excursions(
            float(entry_ask),
            spec.entry_datetime,
            cap_time,
            [event_quotes, next_quotes],
            entry_bid=float(entry_bid) if entry_bid is not None else None,
        )
        observations = _usable_bid_observations(
            event_quotes,
            next_quotes,
            spec.entry_datetime,
            cap_time,
            entry_row=entry_row,
        )
        ten_time = spec.exit_datetimes["next_day_10_00_et"]
        thirty_time = spec.exit_datetimes["next_day_15_30_et"]
        scenarios: list[dict[str, Any]] = []
        for name, exit_time in (
            ("baseline_10_00", ten_time),
            ("baseline_15_30", thirty_time),
        ):
            observation = _quote_observation(next_quotes, exit_time)
            scenarios.append(_case_result(
                scenario=name,
                entry_ask=float(entry_ask),
                observation=observation,
                reason=name,
                commission=commission,
                budget=budget,
            ))
        for bracket_name, rules in BRACKET_RULES.items():
            touch, touch_reason = first_touch(
                observations,
                float(entry_ask),
                rules["stop_multiple"],
                rules["take_multiple"],
            )
            if touch is None:
                observation = _quote_observation(next_quotes, thirty_time)
                reason = f"bracket_{bracket_name}_cap_15_30"
                trigger_multiple = None
            else:
                observation = touch
                reason = f"bracket_{bracket_name}_{touch_reason}"
                trigger_multiple = rules["stop_multiple"] if touch_reason == "stop" else rules["take_multiple"]
                observation = {**observation, "status": "mark"}
            scenarios.append(_case_result(
                scenario=f"bracket_{bracket_name}",
                entry_ask=float(entry_ask),
                observation=observation,
                reason=reason,
                commission=commission,
                budget=budget,
                trigger_multiple=trigger_multiple,
                scanned_observations=len(observations),
            ))
        cases.append({
            "selection": label,
            "right": "CALL",
            "strike": strike,
            "spot_at_entry": spot,
            "moneyness_dollars_at_entry": round(strike - spot, 4),
            "entry_time_et": _stamp_text(spec.entry_datetime),
            "entry_bid": entry_bid,
            "entry_ask": entry_ask,
            "entry_bid_size": entry_values.get("bid_size"),
            "entry_ask_size": entry_values.get("ask_size"),
            "entry_ask_executable": entry_values.get("ask_size", 0) > 0,
            "entry_bid_executable": entry_values.get("bid_size", 0) > 0,
            "full_cap_path": full_path,
            "scenarios": scenarios,
        })

    return {
        "event_date": spec.event_date,
        "source_ts_et": provenance["source_ts_et"],
        "source_ts_utc": provenance["source_ts_utc"],
        "derived_entry_ts_et": provenance["derived_entry_ts_et"],
        "derived_entry_ts_utc": provenance["derived_entry_ts_utc"],
        "signal_id": provenance["signal_id"],
        "exit_date": spec.exit_date,
        "expiration": spec.expiration,
        "selection": selection_details,
        "inventory_rows": sum(item["rows"] for item in inventory),
        "compressed_bytes": sum(item["compressed_bytes"] for item in inventory),
        "quote_diagnostics": quote_diagnostics,
        "cases": cases,
    }


def _aggregate(episodes: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for episode in episodes:
        for case in episode["cases"]:
            if case.get("status") == "skipped":
                for scenario in ("baseline_10_00", "baseline_15_30", "bracket_A", "bracket_B"):
                    grouped[(case["selection"], scenario)].append({
                        "status": "skipped",
                        "skip_reason": case.get("skip_reason"),
                    })
            else:
                for scenario in case["scenarios"]:
                    grouped[(case["selection"], scenario["scenario"])].append(scenario)
    output: dict[str, Any] = {}
    for (selection, scenario), rows in sorted(grouped.items()):
        valid = [row for row in rows if row.get("status") == "ok"]
        executable = [row for row in valid if row.get("exit_bid_executable")]
        mark_only = [row for row in valid if row.get("exit_mark_only")]
        budget_valid = [row for row in valid if row.get("budget_contracts", 0) > 0]
        budget_executable = [row for row in executable if row.get("budget_contracts", 0) > 0]
        output.setdefault(selection, {})[scenario] = {
            "independent_event_dates": len(valid),
            "ok_cases": len(valid),
            "skipped_cases": len(rows) - len(valid),
            "quote_executable_exit_cases": len(executable),
            "mark_only_bid_cases": len(mark_only),
            "positive_net_cases": sum(row["net_pnl_per_contract_usd"] > 0 for row in valid),
            "positive_quote_executable_net_cases": sum(row["net_pnl_per_contract_usd"] > 0 for row in executable),
            "one_contract_gross_sum_usd": round(sum(row["gross_pnl_per_contract_usd"] for row in valid), 2),
            "one_contract_net_sum_usd": round(sum(row["net_pnl_per_contract_usd"] for row in valid), 2),
            "one_contract_net_mean_usd": round(fmean(row["net_pnl_per_contract_usd"] for row in valid), 2) if valid else None,
            "one_contract_net_median_usd": round(median(row["net_pnl_per_contract_usd"] for row in valid), 2) if valid else None,
            "one_contract_quote_executable_net_sum_usd": round(sum(row["net_pnl_per_contract_usd"] for row in executable), 2),
            "one_contract_quote_executable_net_mean_usd": round(fmean(row["net_pnl_per_contract_usd"] for row in executable), 2) if executable else None,
            "one_contract_quote_executable_net_median_usd": round(median(row["net_pnl_per_contract_usd"] for row in executable), 2) if executable else None,
            "budget_ok_cases": len(budget_valid),
            "budget_skipped_cases": len(valid) - len(budget_valid),
            "budget_net_sum_usd": round(sum(row["budget_net_pnl_usd"] for row in budget_valid), 2),
            "budget_contracts_sum": sum(row["budget_contracts"] for row in budget_valid),
            "budget_quote_executable_net_sum_usd": round(sum(row["budget_net_pnl_usd"] for row in budget_executable), 2),
        }
    return output


def build_study(
    data_root: Path = DEFAULT_DATA_ROOT,
    commission: float = ROUND_TRIP_COMMISSION,
    budget: float = PREMIUM_BUDGET,
    pi_path: Path | None = None,
) -> dict[str, Any]:
    if not math.isfinite(commission) or commission < 0:
        raise ValueError("commission must be finite and nonnegative")
    if not math.isfinite(budget) or budget <= 0:
        raise ValueError("budget must be finite and positive")
    episodes = [
        run_episode(data_root, spec, provenance, commission, budget)
        for spec, provenance in canonical_episode_specs(pi_path)
    ]
    return {
        "study": "offline QQQ PI fixed exit-path risk study",
        "independent_event_dates": len(episodes),
        "event_dates": [episode["event_date"] for episode in episodes],
        "predeclared_rules": {
            "entry": "same next-session expiry ATM/$3/$10 call; entry ask at canonical PI source next-minute timestamp",
            "baseline_exits": ["next_day_10_00", "next_day_15_30"],
            "bracket_A": BRACKET_RULES["A"],
            "bracket_B": BRACKET_RULES["B"],
            "bracket_cap": "next_day_15_30",
            "trigger_price": "entry bid at the signal-next-minute timestamp, followed by each subsequent mark-eligible observed bid; actual observed bid is used for gap outcomes",
            "empty_marker": "09:30 rows with zero/invalid ask book are excluded from mark path",
            "mark_only_definition": "positive ask/ask_size with bid_size <= 0; retained as an economic mark and excluded from executable-bid counts",
        },
        "commission_round_trip_usd_per_contract": commission,
        "premium_budget_usd": budget,
        "contract_multiplier_assumption": CONTRACT_MULTIPLIER_ASSUMPTION,
        "contract_multiplier_definition_verified": False,
        "quote_age_status": "unknown",
        "execution_definition": "executable_bid means positive bid_size at a mark-eligible bid; historical data cannot prove a filled order",
        "equal_capital_comparison": "whole-contract budget sensitivity only; no portfolio claim",
        "episodes": episodes,
        "aggregate_by_selection_and_scenario": _aggregate(episodes),
        "interpretation": "Five-date descriptive risk study; no best bracket, preset, or strategy recommendation.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--commission-round-trip", type=float, default=ROUND_TRIP_COMMISSION)
    parser.add_argument("--budget", type=float, default=PREMIUM_BUDGET)
    parser.add_argument("--pi-path", type=Path, default=None)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    result = build_study(args.data_root, args.commission_round_trip, args.budget, args.pi_path)
    print(json.dumps(result, ensure_ascii=True, indent=None if args.compact else 2, sort_keys=True))


if __name__ == "__main__":
    main()
