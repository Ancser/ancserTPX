"""Offline per-mark ATM option replay for a saved PI cohort month.

The runner reads only saved Theta quote/first-order Greek files.  It derives
the first eligible minute from canonical PI source time, selects the listed
ATM contract from a point-in-time Greek underlying spot, and compares exact
next-session 10:00 and 15:30 ET bids.  It has no network or authentication
path.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from statistics import median
from typing import Any, Iterable
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.backtest.robustness import series_stats  # noqa: E402
from backend.data import pi_history  # noqa: E402
from scripts import theta_data_ingest as ingest  # noqa: E402
from scripts.pi_theta_exit_path_study import _entry_ask_quote  # noqa: E402
from scripts.pi_theta_option_pilot import (  # noqa: E402
    CONTRACT_MULTIPLIER_ASSUMPTION,
    DEFAULT_DATA_ROOT,
    ROUND_TRIP_COMMISSION_SENSITIVITY,
    _bid_mark_quality,
    _number,
    _quote_quality,
    _stamp_text,
    _timestamp,
)
from scripts.pi_underlying_path_study_20260921 import expected_entry  # noqa: E402
from scripts.theta_pi_cohort_ingest import canonical_marks, cohort_requests  # noqa: E402


ET = ZoneInfo("America/New_York")
MONTH = "2026-03"
ENTRY_DELAYS_MINUTES = (0, 1, 5)
UNDERLYING_STALE_SECONDS = 60
UNDERLYING_VERY_STALE_SECONDS = 5 * 60
EXIT_LABELS = ("next_day_10_00", "next_day_15_30")
EXIT_TIMES = {
    "next_day_10_00": time(10, 0),
    "next_day_15_30": time(15, 30),
}
COHORT_MANIFEST_NAME = "theta_pi_cohort_manifest.json"
OUTSIDE_REQUESTED_WINDOW_STATUS = "outside_requested_1600_window"


def _parse_month(value: str) -> str:
    try:
        parsed = date.fromisoformat(f"{value}-01")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("month must be YYYY-MM") from exc
    return parsed.strftime("%Y-%m")


def _as_date(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return date.fromisoformat(str(value)[:10])


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return _timestamp(str(value))


def _mark_identity(mark: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(mark.get("symbol", "")).upper(),
        str(mark.get("source_timestamp_utc", "")),
        str(mark.get("kind", "")),
        str(mark.get("level", "")),
    )


def mark_timing(mark: dict[str, Any]) -> dict[str, Any]:
    """Resolve canonical source time through the existing expected-entry helper."""
    source_ts = pi_history.parse_ts(str(mark["source_timestamp_utc"]))
    source_et = source_ts.astimezone(ET)
    event_day, entry_ts = expected_entry(source_ts)
    local_time = source_et.timetz().replace(tzinfo=None)
    outside_requested_window = local_time > time(16, 0)
    if entry_ts is None:
        status = OUTSIDE_REQUESTED_WINDOW_STATUS if outside_requested_window else "no_eligible_minute"
    else:
        status = "immediate_minute_eligible"
    return {
        "source_timestamp_utc": source_ts.isoformat(),
        "source_timestamp_et": source_et.isoformat(),
        "event_date": event_day.isoformat() if event_day else None,
        "entry_timestamp_utc": entry_ts.isoformat() if entry_ts else None,
        "entry_timestamp_et": entry_ts.astimezone(ET).isoformat() if entry_ts else None,
        "timing_status": status,
        "outside_requested_1600_window": outside_requested_window,
        "extended_session_status": (
            "extended_session_unobserved" if outside_requested_window else "within_requested_window"
        ),
        "derivation": "canonical_marks -> expected_entry(source_timestamp)",
    }


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def load_month_cohorts(
    root: Path,
    month: str = MONTH,
    *,
    pi_path: Path | None = None,
    cohort_manifest_path: Path | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Load canonical marks and saved cohort resolution for one month.

    The saved cohort manifest supplies the already-ingested next-session
    expiration resolution.  Canonical marks are reloaded through the shared
    loader and checked against the manifest identities before replay.
    """
    manifest_path = cohort_manifest_path or root / COHORT_MANIFEST_NAME
    payload = _load_json(manifest_path)
    all_marks = canonical_marks(pi_path=pi_path)
    marks_by_key: defaultdict[tuple[str, date], list[dict[str, Any]]] = defaultdict(list)
    for mark in all_marks:
        marks_by_key[(str(mark["symbol"]).upper(), _as_date(mark["event_date"]))].append(mark)

    selected_manifest = [
        item for item in payload.get("cohorts", [])
        if str(item.get("event_date", ""))[:7] == month
    ]
    if not selected_manifest:
        raise ValueError(f"no saved cohorts for month {month}: {manifest_path}")

    cohorts: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, date]] = set()
    for item in selected_manifest:
        symbol = str(item["symbol"]).upper()
        event_date = _as_date(item["event_date"])
        key = (symbol, event_date)
        if key in seen_keys:
            raise ValueError(f"duplicate saved cohort key: {key}")
        seen_keys.add(key)
        canonical = sorted(
            marks_by_key.get(key, []),
            key=lambda mark: _mark_identity(mark),
        )
        manifest_marks = sorted(
            item.get("marks", []),
            key=lambda mark: _mark_identity(mark),
        )
        canonical_ids = [_mark_identity(mark) for mark in canonical]
        manifest_ids = [_mark_identity(mark) for mark in manifest_marks]
        if canonical_ids != manifest_ids:
            raise ValueError(f"canonical mark identity mismatch: {item.get('cohort_id')}")
        cohorts.append({
            "cohort_id": str(item["cohort_id"]),
            "symbol": symbol,
            "event_date": event_date,
            "exit_date": _as_date(item["exit_date"]),
            "expiration": _as_date(item["expiration"]),
            "marks": canonical,
            "mark_count": len(canonical),
            "directions": sorted({int(mark["direction"]) for mark in canonical}),
            "batch_role": "pilot" if str(item["cohort_id"]) in {
                str(value)
                for value in (payload.get("actual_selected_pilot") or {}).values()
            } else "march_monthly_batch",
        })

    selected_marks = [mark for cohort in cohorts for mark in cohort["marks"]]
    expected_marks = [
        mark for mark in all_marks
        if str(mark["event_date"])[:7] == month
    ]
    if sorted(_mark_identity(mark) for mark in selected_marks) != sorted(
        _mark_identity(mark) for mark in expected_marks
    ):
        raise ValueError(f"saved cohort set does not cover canonical {month} marks")
    return all_marks, sorted(cohorts, key=lambda item: item["cohort_id"]), {
        "path": str(manifest_path),
        "schema_version": payload.get("schema_version"),
        "source_pi_path": payload.get("source_pi_path"),
        "saved_total_cohorts": payload.get("cohort_count"),
        "saved_total_marks": payload.get("canonical_mark_count"),
        "selected_month": month,
        "selected_cohorts": len(cohorts),
        "selected_marks": len(selected_marks),
        "pilot_cohort_ids": sorted({
            str(value)
            for value in (payload.get("actual_selected_pilot") or {}).values()
            if str(value).split(":", 1)[0] in {"QQQ", "SPY"}
        }),
    }


def validate_manifest_files(
    root: Path,
    cohorts: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Validate every saved cohort file through the raw coverage hash manifest."""
    latest = ingest.read_latest_manifest(root)
    inventory: list[dict[str, Any]] = []
    failures: list[str] = []
    seen_requests: set[str] = set()
    for cohort in cohorts:
        requests = cohort_requests(cohort)
        for request in requests:
            request_id = ingest.request_id(request)
            if request_id in seen_requests:
                continue
            seen_requests.add(request_id)
            record = latest.get(request_id)
            cached = ingest._cached_result(request, root, latest)
            if record is None:
                failures.append(f"{request_id}:manifest_missing")
                continue
            if cached is None or cached.get("status") != "cached":
                failures.append(f"{request_id}:hash_or_file_mismatch")
                continue
            path = root / request["relative_path"]
            inventory.append({
                "request_id": request_id,
                "dataset": request["key"],
                "symbol": request["symbol"],
                "request_date": request["request_date"].isoformat(),
                "expiration": str(request["expiration"]),
                "path": str(path),
                "manifest_status": record.get("status"),
                "hash_validated": True,
                "rows": int(record.get("rows", 0)),
                "contracts": int(record.get("contracts", 0)),
                "compressed_bytes": int(record.get("compressed_bytes", path.stat().st_size)),
                "sha256_raw": record.get("sha256_raw"),
                "sha256_gzip": record.get("sha256_gzip"),
            })
    if failures:
        raise ValueError("saved cohort manifest validation failed: " + ";".join(failures[:8]))
    role_counts = Counter(
        "pilot" if cohort["batch_role"] == "pilot" else "march_monthly_batch"
        for cohort in cohorts
    )
    files_by_role = {
        "pilot": role_counts["pilot"] * 4,
        "march_monthly_batch": role_counts["march_monthly_batch"] * 4,
    }
    return inventory, {
        "coverage_manifest_path": str(root / "coverage_manifest.jsonl"),
        "hash_validation": "theta_data_ingest._cached_result recomputed sha256_gzip for every required file",
        "files_requested": len(seen_requests),
        "files_validated": len(inventory),
        "files_by_role": files_by_role,
        "cohorts_by_role": dict(role_counts),
        "rows_from_manifest": sum(item["rows"] for item in inventory),
        "compressed_bytes": sum(item["compressed_bytes"] for item in inventory),
        "status_counts": dict(Counter(item["manifest_status"] for item in inventory)),
    }


def _read_filtered_file(
    path: Path,
    *,
    dataset: str,
    target_times: set[datetime],
    rights: set[str],
) -> tuple[dict[Any, Any], dict[str, Any]]:
    """Read one all-strike file once, retaining only replay timestamps."""
    total_rows = 0
    selected_rows = 0
    invalid_timestamps = 0
    output: dict[Any, Any] = defaultdict(list) if dataset == "greeks_first_order_1m" else {}
    with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            total_rows += 1
            try:
                stamp = _timestamp(row["timestamp"])
            except (KeyError, TypeError, ValueError):
                invalid_timestamps += 1
                continue
            right = str(row.get("right", "")).upper()
            if stamp not in target_times or right not in rights:
                continue
            strike = _number(row.get("strike"))
            if strike is None:
                continue
            selected_rows += 1
            if dataset == "greeks_first_order_1m":
                output[(stamp, right)].append(row)
            else:
                output[(stamp, strike, right)] = row
    return dict(output), {
        "path": str(path),
        "rows_scanned": total_rows,
        "rows_retained_at_replay_timestamps": selected_rows,
        "invalid_timestamp_rows": invalid_timestamps,
    }


def _entry_times_for_cohort(cohort: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], set[datetime]]:
    timings: dict[str, dict[str, Any]] = {}
    targets: set[datetime] = set()
    for mark in cohort["marks"]:
        timing = mark_timing(mark)
        identity = "|".join(_mark_identity(mark))
        timings[identity] = timing
        if timing["entry_timestamp_et"]:
            entry = _as_datetime(timing["entry_timestamp_et"])
            targets.update(entry + timedelta(minutes=delay) for delay in ENTRY_DELAYS_MINUTES)
    return timings, targets


def load_cohort_cache(
    root: Path,
    cohort: dict[str, Any],
) -> dict[str, Any]:
    """Memoize the four filtered files for one symbol-date cohort."""
    timings, event_times = _entry_times_for_cohort(cohort)
    exit_day = cohort["exit_date"]
    exit_times = {
        datetime.combine(exit_day, value, tzinfo=ET)
        for value in EXIT_TIMES.values()
    }
    rights = {
        str(mark["option_right"]).upper()
        for mark in cohort["marks"]
    }
    requests = {request["key"] + request["request_date"].isoformat(): request for request in cohort_requests(cohort)}
    event_request = requests["quote_1m" + cohort["event_date"].isoformat()]
    event_greek_request = requests["greeks_first_order_1m" + cohort["event_date"].isoformat()]
    exit_request = requests["quote_1m" + cohort["exit_date"].isoformat()]

    event_quotes, event_quote_diag = _read_filtered_file(
        root / event_request["relative_path"],
        dataset="quote_1m",
        target_times=event_times | exit_times,
        rights=rights,
    )
    event_greeks, event_greek_diag = _read_filtered_file(
        root / event_greek_request["relative_path"],
        dataset="greeks_first_order_1m",
        target_times=event_times,
        rights=rights,
    )
    exit_quotes, exit_quote_diag = _read_filtered_file(
        root / exit_request["relative_path"],
        dataset="quote_1m",
        target_times=exit_times,
        rights=rights,
    )
    return {
        "event_quotes": event_quotes,
        "event_greeks": event_greeks,
        "exit_quotes": exit_quotes,
        "timings": timings,
        "diagnostics": {
            "event_quote": event_quote_diag,
            "event_greeks": event_greek_diag,
            "exit_quote": exit_quote_diag,
            "memoized_reads": 3,
            "cohort_id": cohort["cohort_id"],
        },
    }


def _greek_candidate(row: dict[str, str], entry_time: datetime) -> tuple[bool, str | None, dict[str, Any]]:
    values = {
        "strike": _number(row.get("strike")),
        "bid": _number(row.get("bid")),
        "ask": _number(row.get("ask")),
        "spot": _number(row.get("underlying_price")),
    }
    try:
        underlying_time = _timestamp(row["underlying_timestamp"])
    except (KeyError, TypeError, ValueError):
        underlying_time = None
    if values["strike"] is None or values["spot"] is None:
        return False, "missing_strike_or_underlying_spot", {**values, "underlying_timestamp": None}
    if underlying_time is None:
        return False, "missing_or_invalid_underlying_timestamp", {**values, "underlying_timestamp": None}
    if underlying_time > entry_time:
        return False, "underlying_timestamp_after_entry", {**values, "underlying_timestamp": underlying_time}
    if values["bid"] is None or values["ask"] is None:
        return False, "missing_or_invalid_greek_quote", {**values, "underlying_timestamp": underlying_time}
    if values["bid"] < 0 or values["ask"] <= 0 or values["bid"] > values["ask"]:
        return False, "zero_negative_or_crossed_greek_quote", {**values, "underlying_timestamp": underlying_time}
    return True, None, {**values, "underlying_timestamp": underlying_time}


def select_atm_from_greeks(
    rows: Iterable[dict[str, str]],
    entry_time: datetime,
    right: str,
) -> dict[str, Any]:
    """Select one actual listed strike from exact-minute, point-in-time Greek rows."""
    right = right.upper()
    exact_rows: list[dict[str, str]] = []
    for row in rows:
        try:
            stamp = _timestamp(row["timestamp"])
        except (KeyError, TypeError, ValueError):
            continue
        if stamp == entry_time and str(row.get("right", "")).upper() == right:
            exact_rows.append(row)
    accepted: list[dict[str, Any]] = []
    rejected = Counter()
    for row in exact_rows:
        valid, reason, values = _greek_candidate(row, entry_time)
        if valid:
            accepted.append({"row": row, "values": values})
        elif reason:
            rejected[reason] += 1
    if not accepted:
        status = "missing" if not exact_rows else "invalid"
        return {
            "status": status,
            "right": right,
            "exact_greek_rows": len(exact_rows),
            "quality_accepted_rows": 0,
            "rejected_reason_counts": dict(rejected),
            "spot_at_entry": None,
            "selected_strike": None,
            "underlying_timestamp": None,
            "underlying_timestamp_valid": False,
            "underlying_timestamp_lag_seconds": None,
        }

    spot_values = {round(float(item["values"]["spot"]), 8) for item in accepted}
    if len(spot_values) != 1:
        return {
            "status": "invalid",
            "right": right,
            "exact_greek_rows": len(exact_rows),
            "quality_accepted_rows": len(accepted),
            "rejected_reason_counts": dict(rejected),
            "spot_at_entry": None,
            "selected_strike": None,
            "spot_values": sorted(spot_values),
            "underlying_timestamp_valid": False,
            "underlying_timestamp_lag_seconds": None,
            "selection_invalid_reason": "conflicting_synchronized_spots",
        }
    spot = float(next(iter(spot_values)))
    selected = min(
        accepted,
        key=lambda item: (
            abs(float(item["values"]["strike"]) - spot),
            float(item["values"]["strike"]),
        ),
    )
    underlying_time = selected["values"]["underlying_timestamp"]
    lag_seconds = (entry_time - underlying_time).total_seconds()
    return {
        "status": "ok",
        "right": right,
        "exact_greek_rows": len(exact_rows),
        "quality_accepted_rows": len(accepted),
        "rejected_reason_counts": dict(rejected),
        "spot_at_entry": spot,
        "selected_strike": float(selected["values"]["strike"]),
        "underlying_timestamp": underlying_time.isoformat(),
        "underlying_timestamp_valid": underlying_time <= entry_time,
        "underlying_timestamp_lag_seconds": round(lag_seconds, 6),
        "underlying_timestamp_rule": "exact Greek minute; underlying timestamp <= entry; later spot rows excluded",
        "selected_implied_vol": _number(selected["row"].get("implied_vol")),
        "selected_iv_error": _number(selected["row"].get("iv_error")),
    }


def _entry_quote_result(
    quote_row: dict[str, str] | None,
    entry_time: datetime,
    strike: float,
    right: str,
) -> dict[str, Any]:
    if quote_row is None:
        return {
            "status": "missing",
            "strike": strike,
            "right": right,
            "entry_timestamp_et": _stamp_text(entry_time),
            "entry_invalid_reasons": [],
        }
    _, reasons, values = _quote_quality(quote_row)
    _, reasons = _entry_ask_quote({entry_time: quote_row}, entry_time)
    if reasons:
        return {
            "status": "invalid",
            "strike": strike,
            "right": right,
            "entry_timestamp_et": _stamp_text(entry_time),
            "entry_invalid_reasons": reasons,
            "entry_bid": values.get("bid"),
            "entry_ask": values.get("ask"),
            "entry_bid_size": values.get("bid_size"),
            "entry_ask_size": values.get("ask_size"),
        }
    return {
        "status": "ok",
        "strike": strike,
        "right": right,
        "entry_timestamp_et": _stamp_text(entry_time),
        "entry_invalid_reasons": [],
        "entry_bid": values.get("bid"),
        "entry_ask": values.get("ask"),
        "entry_bid_size": values.get("bid_size"),
        "entry_ask_size": values.get("ask_size"),
        "entry_ask_executable": bool(values.get("ask_size") and values["ask_size"] > 0),
    }


def _exit_result(
    quote_row: dict[str, str] | None,
    exit_time: datetime,
    entry_ask: float,
    commission: float,
) -> dict[str, Any]:
    base = {
        "scheduled_time_et": _stamp_text(exit_time),
        "entry_ask": entry_ask,
        "commission_round_trip_usd": commission,
    }
    if quote_row is None:
        return {**base, "status": "missing", "invalid_reasons": [], "exit_execution_status": "missing"}
    _, reasons, values = _quote_quality(quote_row)
    mark, executable, economic_zero, no_executable, mark_values = _bid_mark_quality(quote_row)
    if not mark:
        return {
            **base,
            "status": "invalid",
            "invalid_reasons": reasons,
            "exit_bid": mark_values.get("bid"),
            "exit_ask": mark_values.get("ask"),
            "exit_bid_size": mark_values.get("bid_size"),
            "exit_ask_size": mark_values.get("ask_size"),
            "exit_execution_status": "invalid",
        }
    bid = float(mark_values["bid"])
    gross = (bid - entry_ask) * CONTRACT_MULTIPLIER_ASSUMPTION
    net = gross - commission
    return {
        **base,
        "status": "mark",
        "invalid_reasons": [],
        "exit_bid": bid,
        "exit_ask": mark_values.get("ask"),
        "exit_bid_size": mark_values.get("bid_size"),
        "exit_ask_size": mark_values.get("ask_size"),
        "exit_mark_eligible": True,
        "quote_executable_exit": bool(executable),
        "exit_execution_status": "quote_executable_bid" if executable else "mark_only_economic_zero_or_no_size",
        "mark_only": bool(not executable),
        "economic_zero_bid": bool(economic_zero),
        "no_executable_bid": bool(no_executable),
        "gross_pnl_per_contract_usd": round(gross, 2),
        "net_pnl_per_contract_usd": round(net, 2),
        "premium_return_pct_gross": round((bid / entry_ask - 1) * 100, 4),
        "net_pnl_at_1_30_commission_usd": round(gross - ROUND_TRIP_COMMISSION_SENSITIVITY, 2),
    }


def replay_mark(
    mark: dict[str, Any],
    cohort: dict[str, Any],
    cache: dict[str, Any],
    *,
    delay_minutes: int = 0,
    commission: float = ROUND_TRIP_COMMISSION_SENSITIVITY,
) -> dict[str, Any]:
    """Replay one mark at one exact entry-delay minute."""
    timing = mark_timing(mark)
    identity = "|".join(_mark_identity(mark))
    result: dict[str, Any] = {
        "mark_identity": identity,
        "symbol": mark["symbol"],
        "event_date": _as_date(mark["event_date"]).isoformat(),
        "cohort_id": cohort["cohort_id"],
        "direction": "long" if int(mark["direction"]) > 0 else "short",
        "kind": mark["kind"],
        "level": mark.get("level"),
        "right": str(mark["option_right"]).upper(),
        "source_timestamp_utc": timing["source_timestamp_utc"],
        "source_timestamp_et": timing["source_timestamp_et"],
        "primary_entry_timestamp_et": timing["entry_timestamp_et"],
        "entry_delay_minutes": delay_minutes,
        "status": timing["timing_status"] if timing["entry_timestamp_et"] is None else None,
        "timing": timing,
        "selection": None,
        "entry": None,
        "exits": {},
    }
    if timing["entry_timestamp_et"] is None:
        return result

    base_entry = _as_datetime(timing["entry_timestamp_et"])
    entry_time = base_entry + timedelta(minutes=delay_minutes)
    right = str(mark["option_right"]).upper()
    greek_rows: list[dict[str, str]] = cache["event_greeks"].get((entry_time, right), [])
    selection = select_atm_from_greeks(greek_rows, entry_time, right)
    result["entry_timestamp_et"] = _stamp_text(entry_time)
    result["selection"] = selection
    if selection.get("status") != "ok":
        result["status"] = f"entry_greek_{selection.get('status')}"
        return result

    strike = float(selection["selected_strike"])
    quote_row = cache["event_quotes"].get((entry_time, strike, right))
    entry = _entry_quote_result(quote_row, entry_time, strike, right)
    result["entry"] = entry
    if entry["status"] != "ok":
        result["status"] = f"entry_quote_{entry['status']}"
        return result

    result["status"] = "entry_valid"
    entry_ask = float(entry["entry_ask"])
    exit_day = cohort["exit_date"]
    for label, exit_clock in EXIT_TIMES.items():
        exit_time = datetime.combine(exit_day, exit_clock, tzinfo=ET)
        row = cache["exit_quotes"].get((exit_time, strike, right))
        result["exits"][label] = _exit_result(row, exit_time, entry_ask, commission)
    result["contract"] = {
        "strike": strike,
        "right": right,
        "expiration": cohort["expiration"].isoformat(),
        "multiplier_assumption": CONTRACT_MULTIPLIER_ASSUMPTION,
    }
    return result


def _pnl_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [float(row["net_pnl_per_contract_usd"]) for row in rows]
    gross = [float(row["gross_pnl_per_contract_usd"]) for row in rows]
    shared = series_stats(pnls)
    return {
        "n": len(rows),
        "positive_n": sum(pnl > 0 for pnl in pnls),
        "negative_n": sum(pnl < 0 for pnl in pnls),
        "gross_pnl_sum_usd": round(sum(gross), 2),
        "net_pnl_sum_usd": round(sum(pnls), 2),
        "net_pnl_mean_usd": round(sum(pnls) / len(pnls), 2) if pnls else None,
        "net_pnl_median_usd": round(median(pnls), 2) if pnls else None,
        "pf": round(float(shared["pf"]), 4),
        "win_rate": round(float(shared["win"]), 4),
    }


def aggregate_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize exact scheduled exits while preserving quality states."""
    entry_valid = [case for case in cases if case.get("status") == "entry_valid"]
    output: dict[str, Any] = {
        "marks_denominator": len(cases),
        "entry_valid_marks": len(entry_valid),
        "entry_status_counts": dict(Counter(str(case.get("status")) for case in cases)),
        "exits": {},
    }
    for label in EXIT_LABELS:
        all_exit_rows = [case["exits"].get(label, {}) for case in entry_valid]
        marks = [row for row in all_exit_rows if row.get("status") == "mark"]
        executable = [row for row in marks if row.get("quote_executable_exit")]
        mark_only = [row for row in marks if row.get("mark_only")]
        zeros = [row for row in marks if row.get("economic_zero_bid")]
        output["exits"][label] = {
            "entry_valid_marks": len(entry_valid),
            "mark_eligible_n": len(marks),
            "quote_executable_bid_n": len(executable),
            "mark_only_n": len(mark_only),
            "economic_zero_bid_n": len(zeros),
            "missing_n": sum(row.get("status") == "missing" for row in all_exit_rows),
            "invalid_n": sum(row.get("status") == "invalid" for row in all_exit_rows),
            "all_mark_stats": _pnl_stats(marks),
            "quote_executable_stats": _pnl_stats(executable),
        }
    return output


def _nearest_rank(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def underlying_lag_summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Report Greek underlying timestamp age and a predeclared stale exclusion."""
    selected = [
        case for case in cases
        if (case.get("selection") or {}).get("status") == "ok"
        and _number((case.get("selection") or {}).get("underlying_timestamp_lag_seconds")) is not None
    ]
    lags = [
        float(case["selection"]["underlying_timestamp_lag_seconds"])
        for case in selected
    ]
    stale_ids = {
        str(case["mark_identity"])
        for case in selected
        if float(case["selection"]["underlying_timestamp_lag_seconds"]) > UNDERLYING_STALE_SECONDS
    }
    entry_valid = [case for case in cases if case.get("status") == "entry_valid"]
    nonstale = [case for case in entry_valid if str(case["mark_identity"]) not in stale_ids]
    return {
        "selection_rows_with_lag": len(lags),
        "min_seconds": round(min(lags), 6) if lags else None,
        "max_seconds": round(max(lags), 6) if lags else None,
        "p95_nearest_rank_seconds": round(_nearest_rank(lags, 0.95), 6) if lags else None,
        "over_60_seconds": sum(lag > UNDERLYING_STALE_SECONDS for lag in lags),
        "over_5_minutes": sum(lag > UNDERLYING_VERY_STALE_SECONDS for lag in lags),
        "stale_exclusion_threshold_seconds": UNDERLYING_STALE_SECONDS,
        "stale_mark_identities": sorted(stale_ids),
        "sensitivity_excluding_stale": {
            "status": "evaluated" if stale_ids else "no_stale_rows",
            "excluded_entry_valid_marks": len(entry_valid) - len(nonstale),
            "aggregate": aggregate_cases(nonstale),
        },
    }


def _partition_cases(cases: list[dict[str, Any]], field: str) -> dict[str, Any]:
    groups: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        groups[str(case.get(field))].append(case)
    return {key: aggregate_cases(value) for key, value in sorted(groups.items())}


def _market_date_clusters(cases: list[dict[str, Any]], cohorts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_date: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        by_date[str(case["event_date"])].append(case)
    cohort_by_date: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for cohort in cohorts:
        cohort_by_date[cohort["event_date"].isoformat()].append(cohort)
    return [
        {
            "market_date": market_date,
            "marks": len(rows),
            "symbols": dict(Counter(str(row["symbol"]) for row in rows)),
            "directions": dict(Counter(str(row["direction"]) for row in rows)),
            "mixed_direction_marks": len({str(row["direction"]) for row in rows}) > 1,
            "symbol_date_cohorts": len(cohort_by_date[market_date]),
            "cohort_ids": sorted(cohort["cohort_id"] for cohort in cohort_by_date[market_date]),
        }
        for market_date, rows in sorted(by_date.items())
    ]


def _leave_one_market_date_out(cases: list[dict[str, Any]]) -> dict[str, Any]:
    dates = sorted({str(case["event_date"]) for case in cases})
    full = aggregate_cases(cases)
    by_date: dict[str, Any] = {}
    for market_date in dates:
        remaining = [case for case in cases if str(case["event_date"]) != market_date]
        remaining_aggregate = aggregate_cases(remaining)
        by_date[market_date] = {
            "excluded_marks": len(cases) - len(remaining),
            "remaining_market_dates": len({str(case["event_date"]) for case in remaining}),
            "remaining": remaining_aggregate,
            "net_pnl_delta_vs_full_by_exit": {
                label: round(
                    remaining_aggregate["exits"][label]["all_mark_stats"]["net_pnl_sum_usd"]
                    - full["exits"][label]["all_mark_stats"]["net_pnl_sum_usd"],
                    2,
                )
                for label in EXIT_LABELS
            },
        }
    return {
        "market_dates": len(dates),
        "full": full,
        "by_excluded_market_date": by_date,
    }


def _delay_cases(
    marks: list[dict[str, Any]],
    cohorts_by_id: dict[str, dict[str, Any]],
    caches_by_id: dict[str, dict[str, Any]],
    delay: int,
    commission: float,
) -> list[dict[str, Any]]:
    return [
        replay_mark(
            mark,
            cohorts_by_id[str(mark["cohort_id"])],
            caches_by_id[str(mark["cohort_id"])],
            delay_minutes=delay,
            commission=commission,
        )
        for mark in marks
    ]


def build_replay(
    data_root: Path = DEFAULT_DATA_ROOT,
    *,
    month: str = MONTH,
    commission: float = ROUND_TRIP_COMMISSION_SENSITIVITY,
    pi_path: Path | None = None,
    cohort_manifest_path: Path | None = None,
) -> dict[str, Any]:
    if not math.isfinite(commission) or commission < 0:
        raise ValueError("commission must be finite and nonnegative")
    month = _parse_month(month)
    root = Path(data_root)
    all_marks, cohorts, cohort_manifest_info = load_month_cohorts(
        root,
        month,
        pi_path=pi_path,
        cohort_manifest_path=cohort_manifest_path,
    )
    inventory, validation = validate_manifest_files(root, cohorts)
    cohorts_by_id = {str(cohort["cohort_id"]): cohort for cohort in cohorts}
    caches_by_id: dict[str, dict[str, Any]] = {}
    for cohort in cohorts:
        caches_by_id[str(cohort["cohort_id"])] = load_cohort_cache(root, cohort)

    marks = [mark for cohort in cohorts for mark in cohort["marks"]]
    for mark in marks:
        mark["cohort_id"] = next(
            cohort["cohort_id"]
            for cohort in cohorts
            if (cohort["symbol"], cohort["event_date"]) == (
                str(mark["symbol"]).upper(), _as_date(mark["event_date"]),
            )
        )
    timings_all = [mark_timing(mark) for mark in all_marks]
    outside_window_all = [
        timing for timing in timings_all
        if timing["timing_status"] == OUTSIDE_REQUESTED_WINDOW_STATUS
    ]
    immediate_count = sum(
        timing["timing_status"] == "immediate_minute_eligible"
        for timing in timings_all
    )
    no_eligible_count = sum(
        timing["timing_status"] == "no_eligible_minute"
        for timing in timings_all
    )
    primary_cases = _delay_cases(marks, cohorts_by_id, caches_by_id, 0, commission)
    delay_cases = {
        delay: _delay_cases(marks, cohorts_by_id, caches_by_id, delay, commission)
        for delay in ENTRY_DELAYS_MINUTES
    }
    delay_sensitivity = {
        str(delay): aggregate_cases(cases)
        for delay, cases in delay_cases.items()
    }
    primary_aggregate = aggregate_cases(primary_cases)
    march_dates = sorted({str(mark["event_date"]) for mark in marks})
    direction_counts = Counter(str(case["direction"]) for case in primary_cases)
    kind_counts = Counter(str(case["kind"]) for case in primary_cases)
    level_counts = Counter(str(case["level"]) for case in primary_cases)
    cohort_counts = Counter(str(cohort["batch_role"]) for cohort in cohorts)
    primary_by_direction = _partition_cases(primary_cases, "direction")
    primary_by_kind = _partition_cases(primary_cases, "kind")
    primary_by_level = _partition_cases(primary_cases, "level")
    mixed_direction_cohorts = [
        cohort["cohort_id"] for cohort in cohorts if len(cohort["directions"]) > 1
    ]
    return {
        "study": "offline per-mark next-session-expiry ATM option replay",
        "version": "2026-09-22-pi-theta-cohort-replay-v1",
        "month": month,
        "data_root": str(root),
        "source_time_status": "retrospective; canonical source timestamp retained; receipt/availability timestamp unavailable",
        "input": {
            "cohort_manifest": cohort_manifest_info,
            "coverage_validation": validation,
            "inventory_files": inventory,
        },
        "global_canonical_inventory": {
            "marks": len(all_marks),
            "immediate_minute_eligible": immediate_count,
            "outside_requested_1600_window": len(outside_window_all),
            "extended_session_unobserved": len(outside_window_all),
            "no_eligible_minute_other": no_eligible_count,
            "outside_window_rows": outside_window_all,
        },
        "trade_inventory": {
            "cohorts": len(cohorts),
            "marks": len(marks),
            "independent_symbol_dates": len(cohorts),
            "independent_trading_dates": len(march_dates),
            "cohort_roles": dict(cohort_counts),
            "directions": dict(direction_counts),
            "kinds": dict(kind_counts),
            "levels": dict(level_counts),
            "trading_dates": march_dates,
            "mixed_direction_cohorts": mixed_direction_cohorts,
            "market_date_clusters": _market_date_clusters(primary_cases, cohorts),
            "date_dependency": "marks sharing a symbol-date cohort and marks on one trading date are dependent; no portfolio equity curve is produced",
        },
        "predeclared_replay": {
            "primary_entry": "first eligible minute from expected_entry(canonical source timestamp)",
            "entry_delays_minutes": list(ENTRY_DELAYS_MINUTES),
            "entry_delay_definition": "exact event-session minute at primary expected_entry plus 1 or 5 minutes; no forward-fill",
            "right_rule": "CALL for bullish/long marks and PUT for bearish/short marks",
            "selection": "ATM minimizes absolute listed-strike distance to synchronized first-order Greek underlying spot at the exact entry minute",
            "underlying_timestamp_rule": "Greek underlying_timestamp <= entry; later underlying spots excluded; lag retained in each case",
            "scheduled_exits": ["next-session exact 10:00 ET bid", "next-session exact 15:30 ET bid"],
            "entry_quote_rule": "exact minute quote with ask > 0 and ask_size > 0; bid_size is not required for buy-at-ask entry",
            "exit_quality": "positive bid_size is quote-executable evidence; bid=0 is retained as an economic mark when the quote is otherwise valid; bid_size=0 is mark-only; missing and invalid are separate",
            "commission_round_trip_usd_per_contract": commission,
            "commission_sensitivity_usd_per_contract": ROUND_TRIP_COMMISSION_SENSITIVITY,
            "contract_multiplier_assumption": CONTRACT_MULTIPLIER_ASSUMPTION,
            "contract_multiplier_definition_verified": False,
            "quote_age_status": "unknown",
            "requested_window_rule": "the saved requests end at 16:00 ET; marks after 16:00 remain in the denominator and are excluded from immediate-entry replay under the cached data; extended-session tradability is unobserved and no fill is invented",
            "threshold_optimization": "none",
        },
        "primary": {
            "aggregate": primary_aggregate,
            "by_direction": primary_by_direction,
            "by_kind": primary_by_kind,
            "by_level": primary_by_level,
            "leave_one_market_date_out": _leave_one_market_date_out(primary_cases),
            "underlying_lag": underlying_lag_summary(primary_cases),
            "cases": primary_cases,
        },
        "entry_delay_sensitivity": delay_sensitivity,
        "underlying_lag_by_entry_delay": {
            str(delay): underlying_lag_summary(cases)
            for delay, cases in delay_cases.items()
        },
        "interpretation": "Descriptive source-time retrospective for saved event marks. No option portfolio PnL, equity curve, best contract, best delay, or strategy recommendation.",
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--month", type=_parse_month, default=MONTH)
    parser.add_argument("--pi-path", type=Path, default=None)
    parser.add_argument("--cohort-manifest", type=Path, default=None)
    parser.add_argument("--commission-round-trip", type=float, default=ROUND_TRIP_COMMISSION_SENSITIVITY)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    result = build_replay(
        args.data_root,
        month=args.month,
        commission=args.commission_round_trip,
        pi_path=args.pi_path,
        cohort_manifest_path=args.cohort_manifest,
    )
    print(json.dumps(result, ensure_ascii=True, indent=None if args.compact else 2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
