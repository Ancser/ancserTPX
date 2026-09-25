"""Offline five-year VP replay with point-in-time QQQ Theta OI context.

This sidecar is deliberately research-only.  It replays the frozen MNQ
Volume Profile candidate through the existing research runner, gates all OI
work on baseline parity, and then reads the already-downloaded QQQ monthly
OI DTE 0-60 archive.  It never initializes the Theta SDK, authenticates, or
writes raw, derived, preset, or live-trading data.

The OI join keeps vendor publication timestamps in UTC for the causal
comparison and keeps the America/New_York publication date for date
preference.  A same-date snapshot can be marked partial when later rows in
that vendor session exist, even when every contract key seen so far is
present.  Partial chains remain visible and are excluded from threshold
calibration.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import importlib.util
import json
import logging
import math
import re
import sys
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Iterator
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.volume_profile_research import (  # noqa: E402
    _load_window,
    _run_candidate,
    _stats,
)
from scripts.vp_theta_oi_context import (  # noqa: E402
    _dedupe_latest,
    _finite,
    _parse_date,
    _parse_timestamp,
    _structure_metrics,
)


VERSION = "2026-09-23-vp-theta-oi-5yr-v1"
ET = ZoneInfo("America/New_York")
UTC = timezone.utc
SAVED_REPORT = Path(
    r"F:\ancserQuant\ancserMarketData\derived\research\volume_profile_research_mnq_2021_2025.json"
)
OI_ROOT = Path(
    r"F:\ancserQuant\ancserMarketData\source\options\thetadata\raw\QQQ\monthly_open_interest_dte_0_60"
)
TRAIN_START = date(2021, 1, 1)
TRAIN_END = date(2025, 12, 31)
HOLDOUT_START = date(2026, 1, 1)
WEEKLY_EXPIRY_REGIME_CUTOFF = date(2022, 11, 15)
OI_PUBLICATION_MINUTE_ET = 6 * 60 + 30
OI_PUBLICATION_TOLERANCE_MINUTES = 90
FEATURE_FIELDS = (
    "oi_unsigned_total",
    "oi_put_call_ratio",
    "oi_near_expiry_share_calendar_dte_le_1",
    "oi_strike_hhi",
)
COMPLETE_CHAIN_STATES = {"complete_same_date_chain", "complete_prior_date_chain"}


class BaselineParityError(RuntimeError):
    """Raised when the frozen VP replay cannot pass the saved evidence gate."""


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"unsupported JSON value: {type(value)!r}")


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)


def _round(value: Any, digits: int = 8) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return round(number, digits) if math.isfinite(number) else None


def _load_saved_report(path: Path = SAVED_REPORT) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"saved VP report missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _frozen_parameters(report: dict[str, Any]) -> dict[str, Any]:
    selected = report.get("selected_training_candidate") or {}
    parameters = dict(selected.get("parameters") or {})
    if not parameters:
        raise ValueError("saved VP report has no selected parameters")
    value_area = _round(parameters.get("value_area_pct"), 4)
    if value_area != 0.70:
        raise ValueError(f"saved VP candidate value_area_pct is {value_area!r}, expected 0.70")
    return parameters


def _fingerprint_rows(rows: Iterable[dict[str, Any]]) -> str:
    material = []
    for row in rows:
        material.append(
            {
                "entry_time": _iso(row.get("entry_time") or row.get("entry_time_utc")),
                "exit_time": _iso(row.get("exit_time") or row.get("exit_time_utc")),
                "pnl": _round(row.get("pnl"), 2),
                "direction": str(row.get("direction") or ""),
                "size": _round(row.get("size"), 6),
                "trade_date": str(row.get("trade_date") or row.get("entry_date_et") or ""),
            }
        )
    payload = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _baseline_values(value: dict[str, Any]) -> dict[str, Any]:
    nested = value.get("baseline") if isinstance(value, dict) else None
    return dict(nested or value or {})


def parity_gate(
    actual: dict[str, Any],
    expected: dict[str, Any],
    *,
    label: str = "baseline",
    pnl_tolerance: float = 0.01,
    pf_tolerance: float = 0.0001,
) -> dict[str, Any]:
    """Compare the saved aggregate evidence with a replay result."""
    actual_values = _baseline_values(actual)
    expected_values = _baseline_values(expected)
    checks = {
        "n": int(actual_values.get("n", -1)) == int(expected_values.get("n", -2)),
        "pnl": abs(float(actual_values.get("pnl", 0.0)) - float(expected_values.get("pnl", 0.0)))
        <= pnl_tolerance,
        "pf": abs(float(actual_values.get("pf", 0.0)) - float(expected_values.get("pf", 0.0)))
        <= pf_tolerance,
    }
    return {
        "label": label,
        "pass": all(checks.values()),
        "checks": checks,
        "actual": {
            "n": int(actual_values.get("n", 0)),
            "pnl": _round(actual_values.get("pnl"), 2),
            "pf": _round(actual_values.get("pf"), 4),
        },
        "expected": {
            "n": int(expected_values.get("n", 0)),
            "pnl": _round(expected_values.get("pnl"), 2),
            "pf": _round(expected_values.get("pf"), 4),
        },
    }


def _group_parity(
    actual: dict[str, Any],
    expected: dict[str, Any],
    *,
    label: str,
) -> dict[str, Any]:
    keys = sorted(set(actual) | set(expected))
    checks: dict[str, Any] = {}
    for key in keys:
        if key not in actual or key not in expected:
            checks[key] = False
            continue
        checks[key] = parity_gate(actual[key], expected[key], label=f"{label}:{key}")["pass"]
    return {"pass": all(checks.values()), "checks": checks}


def _normalize_replay_rows(rows: Iterable[dict[str, Any]], period: str) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        entry = row.get("entry_time")
        entry_utc = entry if isinstance(entry, datetime) else _parse_timestamp(row.get("entry_time_utc") or entry)
        if entry_utc is None:
            raise ValueError(f"replay row has no aware entry timestamp: {row!r}")
        entry_utc = entry_utc.astimezone(UTC)
        entry_et = entry_utc.astimezone(ET)
        exit_value = row.get("exit_time")
        exit_utc = (
            exit_value.astimezone(UTC)
            if isinstance(exit_value, datetime)
            else _parse_timestamp(row.get("exit_time_utc") or exit_value)
        )
        normalized.append(
            {
                **row,
                "entry_time_utc": entry_utc.isoformat(),
                "entry_time_et": entry_et.isoformat(),
                "entry_date_et": entry_et.date().isoformat(),
                "year": str(entry_et.year),
                "period": period,
                "pnl": round(float(row.get("pnl") or 0.0), 2),
                "direction": str(row.get("direction") or "unknown").lower(),
                "exit_time_utc": exit_utc.isoformat() if exit_utc else None,
            }
        )
    normalized.sort(key=lambda item: item["entry_time_utc"])
    return normalized


def _normalize_saved_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        entry = _parse_timestamp(row.get("entry_time_utc") or row.get("entry_time"))
        if entry is None:
            raise ValueError(f"saved holdout row has no aware entry timestamp: {row!r}")
        exit_value = _parse_timestamp(row.get("exit_time_utc") or row.get("exit_time"))
        normalized.append(
            {
                "entry_time_utc": entry.isoformat(),
                "exit_time_utc": exit_value.isoformat() if exit_value else None,
                "pnl": round(float(row.get("pnl") or 0.0), 2),
                "direction": str(row.get("direction") or "unknown").lower(),
                "size": _round(row.get("size"), 6),
            }
        )
    normalized.sort(key=lambda item: item["entry_time_utc"])
    return normalized


def _trade_row_parity(actual: list[dict[str, Any]], expected: list[dict[str, Any]]) -> dict[str, Any]:
    checks: list[bool] = []
    if len(actual) != len(expected):
        return {
            "pass": False,
            "actual_rows": len(actual),
            "expected_rows": len(expected),
            "mismatch_index": None,
            "reason": "row_count_mismatch",
        }
    for index, (got, want) in enumerate(zip(actual, expected)):
        checks.append(
            got["entry_time_utc"] == want["entry_time_utc"]
            and got.get("exit_time_utc") == want.get("exit_time_utc")
            and got["direction"] == want["direction"]
            and abs(float(got["pnl"]) - float(want["pnl"])) <= 0.01
        )
        if not checks[-1]:
            return {
                "pass": False,
                "actual_rows": len(actual),
                "expected_rows": len(expected),
                "mismatch_index": index,
                "actual": {key: got.get(key) for key in ("entry_time_utc", "exit_time_utc", "pnl", "direction")},
                "expected": {key: want.get(key) for key in ("entry_time_utc", "exit_time_utc", "pnl", "direction")},
            }
    return {
        "pass": True,
        "actual_rows": len(actual),
        "expected_rows": len(expected),
        "mismatch_index": None,
    }


def replay_frozen_baseline(report_path: Path = SAVED_REPORT) -> dict[str, Any]:
    """Replay training and holdout before any OI file is opened."""
    report = _load_saved_report(report_path)
    parameters = _frozen_parameters(report)
    training_window = report.get("training_window") or {
        "start": TRAIN_START.isoformat(),
        "end": TRAIN_END.isoformat(),
    }
    holdout_window = report.get("holdout_window") or {
        "start": HOLDOUT_START.isoformat(),
        "end": "2026-09-15",
    }
    train_start = date.fromisoformat(training_window["start"])
    train_end = date.fromisoformat(training_window["end"])
    holdout_start = date.fromisoformat(holdout_window["start"])
    holdout_end = date.fromisoformat(holdout_window["end"])

    train_candles, train_regimes = _load_window("MNQ", train_start, train_end)
    train_replay = _run_candidate(parameters, train_candles, train_regimes, "MNQ", train_start, train_end)
    train_rows = _normalize_replay_rows(train_replay["trade_rows"], "training")

    holdout_candles, holdout_regimes = _load_window("MNQ", holdout_start, holdout_end)
    holdout_replay = _run_candidate(
        parameters,
        holdout_candles,
        holdout_regimes,
        "MNQ",
        holdout_start,
        holdout_end,
    )
    holdout_rows = _normalize_replay_rows(holdout_replay["trade_rows"], "holdout")

    saved_selected = report.get("selected_training_candidate") or {}
    saved_holdout = report.get("holdout") or {}
    train_gate = parity_gate(
        train_replay["baseline"],
        saved_selected.get("baseline") or {},
        label="training",
    )
    holdout_gate = parity_gate(
        holdout_replay["baseline"],
        saved_holdout.get("baseline") or {},
        label="holdout",
    )
    train_group_gate = _group_parity(
        train_replay.get("by_year") or {},
        saved_selected.get("by_year") or {},
        label="training_by_year",
    )
    holdout_group_gate = _group_parity(
        holdout_replay.get("by_year") or {},
        saved_holdout.get("by_year") or {},
        label="holdout_by_year",
    )

    saved_holdout_rows = saved_holdout.get("trade_rows") or []
    holdout_trade_gate = (
        _trade_row_parity(holdout_rows, _normalize_saved_rows(saved_holdout_rows))
        if saved_holdout_rows
        else {
            "pass": None,
            "actual_rows": len(holdout_rows),
            "expected_rows": 0,
            "reason": "saved_holdout_has_no_trade_rows",
        }
    )
    overall = all(
        [
            train_gate["pass"],
            holdout_gate["pass"],
            train_group_gate["pass"],
            holdout_group_gate["pass"],
            holdout_trade_gate["pass"] is not False,
        ]
    )
    result = {
        "status": "parity_passed" if overall else "parity_failed",
        "report_path": str(report_path),
        "symbol": "MNQ",
        "frozen_parameters": parameters,
        "value_area_semantics": {
            "vp_strategy_value_area_pct": 0.70,
            "general_backtest_config_value_area_pct": 0.80,
            "interpretation": "VP strategy previous-RTH VAH/VAL use the candidate's 0.70; the engine config 0.80 is a separate general detector field.",
        },
        "training": {
            "window": {"start": train_start.isoformat(), "end": train_end.isoformat()},
            "candle_count": len(train_candles),
            "trade_row_count": len(train_rows),
            "trade_fingerprint_sha256": _fingerprint_rows(train_rows),
            "recomputed_baseline": train_replay["baseline"],
            "aggregate_gate": train_gate,
            "by_year_gate": train_group_gate,
        },
        "holdout": {
            "window": {"start": holdout_start.isoformat(), "end": holdout_end.isoformat()},
            "candle_count": len(holdout_candles),
            "trade_row_count": len(holdout_rows),
            "trade_fingerprint_sha256": _fingerprint_rows(holdout_rows),
            "recomputed_baseline": holdout_replay["baseline"],
            "aggregate_gate": holdout_gate,
            "by_year_gate": holdout_group_gate,
            "trade_row_gate": holdout_trade_gate,
        },
        "_trade_rows": train_rows + holdout_rows,
        "_replay_report": report,
    }
    return result


def discover_oi_files(
    root: Path = OI_ROOT,
    start: date = TRAIN_START,
    end: date = date(2026, 9, 15),
) -> list[Path]:
    """Return only QQQ monthly partitions overlapping the replay window."""
    paths: list[Path] = []
    pattern = re.compile(r"^month=(\d{4})-(\d{2})\.csv\.gz$")
    for path in sorted(Path(root).glob("month=*.csv.gz")):
        match = pattern.match(path.name)
        if not match:
            continue
        month_start = date(int(match.group(1)), int(match.group(2)), 1)
        month_end = date(
            month_start.year + (month_start.month == 12),
            1 if month_start.month == 12 else month_start.month + 1,
            1,
        )
        if month_start <= end and month_end > start:
            paths.append(path)
    if not paths:
        raise FileNotFoundError(f"no monthly QQQ OI partitions under {root}")
    return paths


def _parse_oi_row(raw: dict[str, Any], source_path: Path) -> dict[str, Any] | None:
    symbol = str(raw.get("symbol") or "").upper()
    timestamp_raw = str(raw.get("timestamp") or "")
    timestamp_utc = _parse_timestamp(timestamp_raw)
    expiration = _parse_date(raw.get("expiration"))
    strike = _finite(raw.get("strike"))
    open_interest = _finite(raw.get("open_interest"))
    right = str(raw.get("right") or "").upper()
    if (
        symbol != "QQQ"
        or timestamp_utc is None
        or expiration is None
        or strike is None
        or open_interest is None
        or open_interest < 0
        or right not in {"CALL", "PUT"}
    ):
        return None
    return {
        "symbol": symbol,
        "expiration": expiration,
        "strike": float(strike),
        "right": right,
        "open_interest": float(open_interest),
        "timestamp": timestamp_utc,
        "timestamp_utc": timestamp_utc.isoformat(),
        "timestamp_et_date": timestamp_utc.astimezone(ET).date(),
        "timestamp_raw": timestamp_raw,
        "source_path": str(source_path),
    }


def _off_hour(timestamp: datetime) -> bool:
    local = timestamp.astimezone(ET)
    minute = local.hour * 60 + local.minute + local.second / 60.0
    return abs(minute - OI_PUBLICATION_MINUTE_ET) > OI_PUBLICATION_TOLERANCE_MINUTES


def _listing_regime_fields(
    trade_date: date,
    same_date_session_present: bool,
) -> dict[str, Any]:
    return {
        "oi_expiry_listing_regime": (
            "pre_2022-11-15_weekly_expiry_regime"
            if trade_date < WEEKLY_EXPIRY_REGIME_CUTOFF
            else "post_2022-11-15_weekly_expiry_regime"
        ),
        "oi_trade_weekday": trade_date.strftime("%A"),
        "oi_weekday_session_availability": (
            "weekend_trade_date"
            if trade_date.weekday() >= 5
            else "same_date_vendor_session"
            if same_date_session_present
            else "weekday_without_same_date_vendor_session",
        ),
    }


def scan_publication_index(
    paths: Iterable[Path],
    start: date,
    end: date,
) -> dict[str, Any]:
    """Scan each archive partition once and index valid vendor sessions."""
    sessions: dict[date, dict[str, Any]] = {}
    files: list[dict[str, Any]] = []
    for path in paths:
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"OI partition missing: {path}")
        raw_rows = 0
        valid_rows = 0
        invalid_rows = 0
        raw_min: datetime | None = None
        raw_max: datetime | None = None
        with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as handle:
            for raw in csv.DictReader(handle):
                raw_rows += 1
                row = _parse_oi_row(raw, path)
                if row is None:
                    invalid_rows += 1
                    continue
                valid_rows += 1
                timestamp = row["timestamp"]
                raw_min = timestamp if raw_min is None else min(raw_min, timestamp)
                raw_max = timestamp if raw_max is None else max(raw_max, timestamp)
                session_date = row["timestamp_et_date"]
                if not (start <= session_date <= end):
                    continue
                info = sessions.setdefault(
                    session_date,
                    {
                        "rows": 0,
                        "off_hour_rows": 0,
                        "min_timestamp": None,
                        "max_timestamp": None,
                        "files": set(),
                    },
                )
                info["rows"] += 1
                info["off_hour_rows"] += int(_off_hour(timestamp))
                info["min_timestamp"] = (
                    timestamp
                    if info["min_timestamp"] is None
                    else min(info["min_timestamp"], timestamp)
                )
                info["max_timestamp"] = (
                    timestamp
                    if info["max_timestamp"] is None
                    else max(info["max_timestamp"], timestamp)
                )
                info["files"].add(str(path))
        files.append(
            {
                "path": str(path),
                "rows_read": raw_rows,
                "valid_qqq_rows": valid_rows,
                "invalid_rows": invalid_rows,
                "compressed_bytes": path.stat().st_size,
                "timestamp_min_utc": raw_min.isoformat() if raw_min else None,
                "timestamp_max_utc": raw_max.isoformat() if raw_max else None,
            }
        )
    session_output = {}
    for session_date, info in sorted(sessions.items()):
        session_output[session_date.isoformat()] = {
            "rows": info["rows"],
            "off_hour_rows": info["off_hour_rows"],
            "min_timestamp_utc": info["min_timestamp"].isoformat(),
            "max_timestamp_utc": info["max_timestamp"].isoformat(),
            "files": sorted(info["files"]),
        }
    return {
        "files": files,
        "sessions": session_output,
        "_sessions": sessions,
        "rows_read": sum(item["rows_read"] for item in files),
        "valid_qqq_rows": sum(item["valid_qqq_rows"] for item in files),
        "invalid_rows": sum(item["invalid_rows"] for item in files),
        "compressed_bytes": sum(item["compressed_bytes"] for item in files),
        "publication_time_reference": "approximately 06:30 America/New_York; raw timestamps preserved",
        "off_hour_rule": "absolute local ET offset greater than 90 minutes from 06:30",
    }


def _publication_dates(sessions: dict[date, dict[str, Any]]) -> list[date]:
    return sorted(sessions)


def choose_snapshot_date(
    trade_date: date,
    entry_utc: datetime,
    sessions: dict[date, dict[str, Any]],
) -> tuple[date | None, str, str | None]:
    """Choose same ET date when any vendor row is causal, else prior date."""
    same = sessions.get(trade_date)
    if same is not None and same["min_timestamp"] <= entry_utc:
        return trade_date, "same_date", None
    dates = _publication_dates(sessions)
    position = bisect_left(dates, trade_date) - 1
    while position >= 0:
        candidate = dates[position]
        candidate_info = sessions[candidate]
        if candidate_info["min_timestamp"] <= entry_utc:
            reason = "same_date_future_only" if same is not None else "no_same_date_vendor_publication"
            return candidate, "prior_date_stale", reason
        position -= 1
    reason = "same_date_future_only" if same is not None else "no_same_date_vendor_publication"
    return None, "missing", reason


def iter_needed_snapshot_rows(
    paths: Iterable[Path],
    wanted_dates: set[date],
) -> Iterator[tuple[date, list[dict[str, Any]]]]:
    """Yield one archive session at a time, avoiding N-trades x archive scans."""
    yielded: set[date] = set()
    for path in paths:
        by_date: defaultdict[date, list[dict[str, Any]]] = defaultdict(list)
        with gzip.open(Path(path), "rt", encoding="utf-8-sig", newline="") as handle:
            for raw in csv.DictReader(handle):
                row = _parse_oi_row(raw, Path(path))
                if row is None:
                    continue
                session_date = row["timestamp_et_date"]
                if session_date in wanted_dates:
                    by_date[session_date].append(row)
        for session_date in sorted(by_date):
            if session_date in yielded:
                raise ValueError(f"duplicate archive session partition: {session_date}")
            yielded.add(session_date)
            yield session_date, by_date[session_date]


def _contract_key(row: dict[str, Any]) -> tuple[str, float, str]:
    return (row["expiration"].isoformat(), float(row["strike"]), str(row["right"]))


def _chain_status(
    *,
    preferred_status: str,
    eligible: list[dict[str, Any]],
    full_rows: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    future_rows: int,
) -> tuple[str, list[str]]:
    if not eligible:
        return "missing", ["no_causal_rows_at_entry"]
    reasons: list[str] = []
    if future_rows:
        reasons.append("rows_after_entry_excluded")
    full_keys = {_contract_key(row) for row in full_rows}
    selected_keys = {_contract_key(row) for row in selected}
    if selected_keys != full_keys:
        reasons.append("contract_keys_missing_at_entry")
    if len(eligible) < len(full_rows):
        reasons.append("raw_rows_after_entry_excluded")
    complete = not reasons
    if complete:
        state = (
            "complete_same_date_chain"
            if preferred_status == "same_date"
            else "complete_prior_date_chain"
        )
    else:
        state = (
            "partial_same_date_chain"
            if preferred_status == "same_date"
            else "partial_prior_date_chain"
        )
    return state, reasons


def _join_trade_snapshot(
    trade: dict[str, Any],
    snapshot_rows: list[dict[str, Any]],
    *,
    snapshot_date: date,
    preferred_status: str,
    same_date_session_rows: int | None,
    same_date_reason: str | None,
) -> dict[str, Any]:
    entry = _parse_timestamp(trade["entry_time_utc"])
    if entry is None:
        raise ValueError(f"trade has invalid entry timestamp: {trade!r}")
    trade_date = date.fromisoformat(trade["entry_date_et"])
    eligible = [row for row in snapshot_rows if row["timestamp"] <= entry]
    future_same = (
        sum(row["timestamp"] > entry for row in snapshot_rows)
        if preferred_status == "same_date"
        else 0
    )
    selected = _dedupe_latest(eligible)
    chain_state, chain_reasons = _chain_status(
        preferred_status=preferred_status,
        eligible=eligible,
        full_rows=snapshot_rows,
        selected=selected,
        future_rows=future_same,
    )
    full_latest = _dedupe_latest(snapshot_rows)
    selected_keys = {_contract_key(row) for row in selected}
    full_keys = {_contract_key(row) for row in full_latest}
    active_full_keys = {
        _contract_key(row)
        for row in full_latest
        if row["expiration"] >= trade_date
    }
    active_selected_keys = {
        _contract_key(row)
        for row in selected
        if row["expiration"] >= trade_date
    }
    same_session_rows = same_date_session_rows if same_date_session_rows is not None else 0
    publication_times = [row["timestamp"] for row in selected]
    selected_off_hour = sum(_off_hour(row["timestamp"]) for row in selected)
    full_off_hour = sum(_off_hour(row["timestamp"]) for row in snapshot_rows)
    selected_active_latest = [
        row for row in selected if row["expiration"] >= trade_date
    ]
    full_active_latest = [
        row for row in full_latest if row["expiration"] >= trade_date
    ]
    selected_positive_oi = sum(
        max(0.0, float(row["open_interest"])) for row in selected_active_latest
    )
    full_positive_oi = sum(
        max(0.0, float(row["open_interest"])) for row in full_active_latest
    )
    positive_oi_shortfall = max(0.0, full_positive_oi - selected_positive_oi)
    positive_oi_surplus = max(0.0, selected_positive_oi - full_positive_oi)
    selected_positive_contracts = sum(
        float(row["open_interest"]) > 0 for row in selected_active_latest
    )
    full_positive_contracts = sum(
        float(row["open_interest"]) > 0 for row in full_active_latest
    )
    result = {
        **trade,
        **_listing_regime_fields(
            trade_date,
            same_date_session_rows is not None and same_date_session_rows > 0,
        ),
        "oi_join_status": preferred_status,
        "oi_snapshot_date_et": snapshot_date.isoformat(),
        "oi_effective_session": "unknown_until_calendar_join",
        "oi_calendar_dte_semantics": "expiration calendar date minus ET trade date; 0 <= DTE <= 1, no exchange-calendar inference",
        "oi_publication_min_utc": min(publication_times).isoformat() if publication_times else None,
        "oi_publication_max_utc": max(publication_times).isoformat() if publication_times else None,
        "oi_publication_raw_min": min(
            (row["timestamp_raw"] for row in selected), default=None
        ),
        "oi_publication_raw_max": max(
            (row["timestamp_raw"] for row in selected), default=None
        ),
        "oi_future_same_date_rows_excluded": future_same,
        "oi_selected_rows": len(selected),
        "oi_selected_raw_rows": len(eligible),
        "oi_snapshot_session_rows": len(snapshot_rows),
        "oi_same_date_session_rows": same_session_rows,
        "oi_selected_raw_row_fraction": round(len(eligible) / len(snapshot_rows), 8)
        if snapshot_rows
        else None,
        "oi_selected_contracts": len(selected_keys),
        "oi_snapshot_contracts": len(full_keys),
        "oi_selected_contract_fraction": round(len(selected_keys) / len(full_keys), 8)
        if full_keys
        else None,
        "oi_selected_active_contracts": len(active_selected_keys),
        "oi_snapshot_active_contracts": len(active_full_keys),
        "oi_selected_active_contract_fraction": round(
            len(active_selected_keys) / len(active_full_keys), 8
        )
        if active_full_keys
        else None,
        "oi_selected_off_hour_rows": selected_off_hour,
        "oi_snapshot_off_hour_rows": full_off_hour,
        "oi_selected_positive_oi_total": round(selected_positive_oi, 4),
        "oi_snapshot_positive_oi_total": round(full_positive_oi, 4),
        "oi_positive_oi_shortfall": round(positive_oi_shortfall, 4),
        "oi_positive_oi_surplus_vs_snapshot": round(positive_oi_surplus, 4),
        "oi_positive_oi_coverage": (
            round(min(1.0, selected_positive_oi / full_positive_oi), 8)
            if full_positive_oi > 0
            else 1.0
            if selected_positive_oi == 0
            else None
        ),
        "oi_selected_positive_contracts": selected_positive_contracts,
        "oi_snapshot_positive_contracts": full_positive_contracts,
        "oi_positive_contract_coverage": (
            round(min(1.0, selected_positive_contracts / full_positive_contracts), 8)
            if full_positive_contracts
            else 1.0
            if selected_positive_contracts == 0
            else None
        ),
        "oi_economic_coverage_semantics": "deduplicated active-contract positive OI totals; row-chain coverage remains separate",
        "oi_chain_completeness": chain_state,
        "oi_chain_incompleteness_reasons": chain_reasons,
        "oi_chain_status_note": same_date_reason,
        "oi_source_months": sorted(
            {
                Path(row["source_path"]).name.replace("month=", "").replace(".csv.gz", "")
                for row in selected
            }
        ),
        "oi_stale_days": (trade_date - snapshot_date).days,
        "oi_missing_reason": None,
    }
    result.update(_structure_metrics(selected, trade_date))
    if not selected:
        result["oi_missing_reason"] = "no causal vendor rows available for selected same/prior publication date"
    return result


def _missing_join_row(
    trade: dict[str, Any],
    *,
    reason: str,
    same_date_session_rows: int | None,
) -> dict[str, Any]:
    return {
        **trade,
        **_listing_regime_fields(
            date.fromisoformat(trade["entry_date_et"]),
            same_date_session_rows is not None and same_date_session_rows > 0,
        ),
        "oi_join_status": "missing",
        "oi_snapshot_date_et": None,
        "oi_effective_session": "unknown_until_calendar_join",
        "oi_calendar_dte_semantics": "expiration calendar date minus ET trade date; 0 <= DTE <= 1, no exchange-calendar inference",
        "oi_publication_min_utc": None,
        "oi_publication_max_utc": None,
        "oi_publication_raw_min": None,
        "oi_publication_raw_max": None,
        "oi_future_same_date_rows_excluded": 0,
        "oi_selected_rows": 0,
        "oi_selected_raw_rows": 0,
        "oi_snapshot_session_rows": 0,
        "oi_same_date_session_rows": same_date_session_rows or 0,
        "oi_selected_raw_row_fraction": None,
        "oi_selected_contracts": 0,
        "oi_snapshot_contracts": 0,
        "oi_selected_contract_fraction": None,
        "oi_selected_active_contracts": 0,
        "oi_snapshot_active_contracts": 0,
        "oi_selected_active_contract_fraction": None,
        "oi_selected_off_hour_rows": 0,
        "oi_snapshot_off_hour_rows": 0,
        "oi_selected_positive_oi_total": 0.0,
        "oi_snapshot_positive_oi_total": 0.0,
        "oi_positive_oi_shortfall": 0.0,
        "oi_positive_oi_surplus_vs_snapshot": 0.0,
        "oi_positive_oi_coverage": None,
        "oi_selected_positive_contracts": 0,
        "oi_snapshot_positive_contracts": 0,
        "oi_positive_contract_coverage": None,
        "oi_economic_coverage_semantics": "deduplicated active-contract positive OI totals; row-chain coverage remains separate",
        "oi_chain_completeness": "missing",
        "oi_chain_incompleteness_reasons": [reason],
        "oi_chain_status_note": reason,
        "oi_source_months": [],
        "oi_stale_days": None,
        "oi_missing_reason": reason,
        "oi_metrics_status": "missing",
    }


def _calendar_capability() -> dict[str, Any]:
    candidates = ("exchange_calendars", "pandas_market_calendars")
    found = [name for name in candidates if importlib.util.find_spec(name)]
    return {
        "official_calendar_package_available": bool(found),
        "packages_detected": found,
        "classification": (
            "available_but_not_used_by_default"
            if found
            else "unavailable; holiday versus non-ETF date remains unclassified"
        ),
    }


def join_oi_context_streamed(
    trades: list[dict[str, Any]],
    paths: Iterable[Path],
    start: date,
    end: date,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Index publication sessions once, then stream only needed sessions."""
    paths = [Path(path) for path in paths]
    index = scan_publication_index(paths, start, end)
    sessions: dict[date, dict[str, Any]] = index["_sessions"]
    planned: dict[date, list[tuple[int, str, date | None, str | None]]] = defaultdict(list)
    planned_rows: list[dict[str, Any] | None] = [None] * len(trades)
    wanted_dates: set[date] = set()
    calendar_flags = Counter()
    for index_value, trade in enumerate(trades):
        trade_date = date.fromisoformat(trade["entry_date_et"])
        entry = _parse_timestamp(trade["entry_time_utc"])
        if entry is None:
            raise ValueError(f"invalid replay trade entry timestamp: {trade!r}")
        same_info = sessions.get(trade_date)
        snapshot_date, status, reason = choose_snapshot_date(
            trade_date,
            entry,
            sessions,
        )
        if trade_date.weekday() >= 5:
            calendar_flag = "weekend_trade_date"
        elif same_info is None:
            calendar_flag = "weekday_without_same_date_vendor_publication_unclassified_holiday_or_missing"
        else:
            calendar_flag = "same_date_vendor_publication_present"
        calendar_flags[calendar_flag] += 1
        same_rows = same_info["rows"] if same_info is not None else 0
        if snapshot_date is None:
            planned_rows[index_value] = _missing_join_row(
                {
                    **trade,
                    "oi_calendar_date_flag": calendar_flag,
                    **_listing_regime_fields(trade_date, same_info is not None),
                },
                reason=reason or "no_prior_vendor_publication",
                same_date_session_rows=same_rows,
            )
            continue
        wanted_dates.add(snapshot_date)
        planned[snapshot_date].append((index_value, status, trade_date, reason))

    seen_snapshot_dates: set[date] = set()
    for snapshot_date, snapshot_rows in iter_needed_snapshot_rows(paths, wanted_dates):
        seen_snapshot_dates.add(snapshot_date)
        for index_value, status, trade_date, reason in planned.get(snapshot_date, []):
            trade = {
                **trades[index_value],
                **_listing_regime_fields(trade_date, same_info is not None),
                "oi_calendar_date_flag": (
                    "same_date_vendor_publication_present"
                    if status == "same_date"
                    else "prior_vendor_publication_used_without_official_calendar_classification"
                ),
            }
            same_info = sessions.get(trade_date)
            planned_rows[index_value] = _join_trade_snapshot(
                trade,
                snapshot_rows,
                snapshot_date=snapshot_date,
                preferred_status=status,
                same_date_session_rows=same_info["rows"] if same_info else 0,
                same_date_reason=reason,
            )
    for index_value, planned_row in enumerate(planned_rows):
        if planned_row is None:
            trade = trades[index_value]
            trade_date = date.fromisoformat(trade["entry_date_et"])
            same_info = sessions.get(trade_date)
            planned_rows[index_value] = _missing_join_row(
                {
                    **trade,
                    "oi_calendar_date_flag": "planned_snapshot_partition_missing",
                },
                reason="planned_snapshot_partition_missing",
                same_date_session_rows=same_info["rows"] if same_info else 0,
            )
    metadata = {
        "files": index["files"],
        "sessions_indexed": len(index["_sessions"]),
        "session_dates_indexed": sorted(index["sessions"]),
        "rows_read": index["rows_read"],
        "valid_qqq_rows": index["valid_qqq_rows"],
        "invalid_rows": index["invalid_rows"],
        "compressed_bytes": index["compressed_bytes"],
        "needed_snapshot_dates": sorted(item.isoformat() for item in wanted_dates),
        "needed_snapshot_date_count": len(wanted_dates),
        "streamed_snapshot_dates": sorted(item.isoformat() for item in seen_snapshot_dates),
        "calendar": _calendar_capability(),
        "calendar_flags": dict(sorted(calendar_flags.items())),
        "effective_session": "unknown_until_calendar_join",
        "future_data_policy": "actual aware vendor timestamp <= entry UTC; later same-date rows are excluded and counted",
        "oi_universe": "QQQ monthly partial chain DTE 0-60; expired contracts excluded from structure metrics",
    }
    return [row for row in planned_rows if row is not None], metadata


def _safe_stats(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    return _stats(list(rows))


def _group_summary(rows: list[dict[str, Any]], field: str) -> dict[str, Any]:
    groups: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(field) or "unknown")].append(row)
    result = {}
    for key, group in sorted(groups.items()):
        statuses = Counter(row.get("oi_join_status") for row in group)
        chain = Counter(row.get("oi_chain_completeness") for row in group)
        fractions = [
            row["oi_selected_raw_row_fraction"]
            for row in group
            if row.get("oi_selected_raw_row_fraction") is not None
        ]
        eligible_features = sum(
            all(row.get(feature) is not None for feature in FEATURE_FIELDS)
            and row.get("oi_chain_completeness") in COMPLETE_CHAIN_STATES
            for row in group
        )
        result[key] = {
            "trade_count": len(group),
            "independent_dates": len({row["entry_date_et"] for row in group}),
            "directions": dict(sorted(Counter(row["direction"] for row in group).items())),
            "stats": _safe_stats(group),
            "oi_join_status": dict(sorted(statuses.items(), key=lambda item: str(item[0]))),
            "oi_chain_completeness": dict(sorted(chain.items(), key=lambda item: str(item[0]))),
            "complete_feature_rows": eligible_features,
            "selected_raw_row_fraction_median": _round(median(fractions), 8)
            if fractions
            else None,
        }
    return result


def _date_clusters(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_date: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_date[row["entry_date_et"]].append(row)
    counts = {key: len(value) for key, value in sorted(by_date.items())}
    status_by_date = {
        key: dict(sorted(Counter(item["oi_join_status"] for item in value).items()))
        for key, value in sorted(by_date.items())
    }
    return {
        "trade_count": len(rows),
        "independent_dates": len(by_date),
        "single_trade_dates": sum(value == 1 for value in counts.values()),
        "multi_trade_dates": sum(value > 1 for value in counts.values()),
        "trade_counts_by_date": counts,
        "oi_join_status_by_date": status_by_date,
    }


def _complete_feature_row(row: dict[str, Any], feature: str) -> bool:
    return (
        row.get("oi_chain_completeness") in COMPLETE_CHAIN_STATES
        and row.get(feature) is not None
    )


def _missed_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "winners": sum(float(row.get("pnl") or 0.0) > 0 for row in rows),
        "losses_or_flat": sum(float(row.get("pnl") or 0.0) <= 0 for row in rows),
    }


def _threshold_analysis(rows: list[dict[str, Any]]) -> dict[str, Any]:
    calibration = [
        row
        for row in rows
        if 2021 <= int(row["year"]) <= 2023
        and row.get("oi_chain_completeness") in COMPLETE_CHAIN_STATES
    ]
    thresholds: dict[str, Any] = {
        "calibration_window": "entry years 2021-2023 in chronological order",
        "calibration_rows_complete_chain": len(calibration),
        "calibration_rows_by_year": dict(
            sorted(Counter(row["year"] for row in calibration).items())
        ),
        "calibration_dates_by_year": {
            year: len({row["entry_date_et"] for row in calibration if row["year"] == year})
            for year in sorted({row["year"] for row in calibration})
        },
        "calibration_regime_mix": dict(
            sorted(Counter(row["oi_expiry_listing_regime"] for row in calibration).items())
        ),
        "calibration_weekday_mix": dict(
            sorted(Counter(row["oi_trade_weekday"] for row in calibration).items())
        ),
        "policy": "median is predeclared from 2021-2023 only; <= median and > median are descriptive splits, not an engine gate",
        "features": {},
    }
    periods = {
        "2024-2025": [row for row in rows if int(row["year"]) in {2024, 2025}],
        "2026": [row for row in rows if int(row["year"]) == 2026],
    }
    for feature in FEATURE_FIELDS:
        training_values = [float(row[feature]) for row in calibration if row.get(feature) is not None]
        if not training_values:
            thresholds["features"][feature] = {
                "status": "no_complete_calibration_values",
                "calibration_rows": 0,
            }
            continue
        threshold = float(median(training_values))
        feature_result: dict[str, Any] = {
            "status": "evaluated",
            "calibration_median": round(threshold, 8),
            "calibration_rows": len(training_values),
            "periods": {},
        }
        for period, period_rows in periods.items():
            eligible = [row for row in period_rows if _complete_feature_row(row, feature)]
            low = [row for row in eligible if float(row[feature]) <= threshold]
            high = [row for row in eligible if float(row[feature]) > threshold]
            low_missed = _missed_counts(high)
            high_missed = _missed_counts(low)
            feature_result["periods"][period] = {
                "eligible_complete_chain_rows": len(eligible),
                "excluded_partial_or_missing_rows": len(period_rows) - len(eligible),
                "low_or_equal": {
                    "n": len(low),
                    "stats": _safe_stats(low),
                    "positive_pnl": _missed_counts(low)["winners"],
                    "nonpositive_pnl": _missed_counts(low)["losses_or_flat"],
                },
                "high": {
                    "n": len(high),
                    "stats": _safe_stats(high),
                    "positive_pnl": _missed_counts(high)["winners"],
                    "nonpositive_pnl": _missed_counts(high)["losses_or_flat"],
                },
                "missed_if_low_or_equal_selected": {
                    "winners_in_high": low_missed["winners"],
                    "losses_in_high": low_missed["losses_or_flat"],
                },
                "missed_if_high_selected": {
                    "winners_in_low": high_missed["winners"],
                    "losses_in_low": high_missed["losses_or_flat"],
                },
            }
        thresholds["features"][feature] = feature_result

    sensitivity = [
        row
        for row in rows
        if 2021 <= int(row["year"]) <= 2023
        and row.get("oi_positive_oi_coverage") == 1.0
    ]
    thresholds["zero_oi_listing_sensitivity"] = {
        "policy": "diagnostic only: permits a partial row chain when deduplicated positive-OI coverage is 100%; primary thresholds remain complete-chain only",
        "eligible_rows_by_year": dict(
            sorted(Counter(row["year"] for row in sensitivity).items())
        ),
        "eligible_dates_by_year": {
            year: len({row["entry_date_et"] for row in sensitivity if row["year"] == year})
            for year in sorted({row["year"] for row in sensitivity})
        },
        "partial_rows_included": sum(
            str(row.get("oi_chain_completeness", "")).startswith("partial")
            for row in sensitivity
        ),
        "features": {
            feature: {
                "rows": len(values),
                "median": round(float(median(values)), 8) if values else None,
            }
            for feature in FEATURE_FIELDS
            for values in [
                [
                    float(row[feature])
                    for row in sensitivity
                    if row.get(feature) is not None
                ]
            ]
        },
    }
    return thresholds


def _listing_regime_diagnostics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in ("oi_expiry_listing_regime", "oi_trade_weekday", "oi_weekday_session_availability"):
        groups: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            groups[str(row.get(field) or "unknown")].append(row)
        result[field] = {}
        for key, group in sorted(groups.items()):
            near_values = [
                row["oi_near_expiry_share_calendar_dte_le_1"]
                for row in group
                if row.get("oi_near_expiry_share_calendar_dte_le_1") is not None
            ]
            result[field][key] = {
                "trade_count": len(group),
                "independent_dates": len({row["entry_date_et"] for row in group}),
                "same_date_vendor_session": sum(
                    row.get("oi_join_status") == "same_date" for row in group
                ),
                "complete_chain_rows": sum(
                    row.get("oi_chain_completeness") in COMPLETE_CHAIN_STATES for row in group
                ),
                "partial_chain_rows": sum(
                    str(row.get("oi_chain_completeness", "")).startswith("partial")
                    for row in group
                ),
                "calendar_dte_le_1_feature_rows": len(near_values),
                "calendar_dte_le_1_share_median": round(float(median(near_values)), 8)
                if near_values
                else None,
            }
    return result


def _same_event_periods(rows: list[dict[str, Any]]) -> dict[str, Any]:
    periods = {
        "2024-2025": [row for row in rows if int(row["year"]) in {2024, 2025}],
        "2026_same_event_previously_inspected": [
            row for row in rows if int(row["year"]) == 2026
        ],
    }
    result = {}
    for name, group in periods.items():
        result[name] = {
            "trade_count": len(group),
            "stats": _safe_stats(group),
            "independent_dates": len({row["entry_date_et"] for row in group}),
            "oi_join_status": dict(sorted(Counter(row["oi_join_status"] for row in group).items())),
            "complete_chain_rows": sum(
                row.get("oi_chain_completeness") in COMPLETE_CHAIN_STATES for row in group
            ),
            "partial_chain_rows": sum(
                str(row.get("oi_chain_completeness", "")).startswith("partial")
                for row in group
            ),
            "missing_chain_rows": sum(row.get("oi_chain_completeness") == "missing" for row in group),
        }
    return result


def _edge_direction_control(rows: list[dict[str, Any]]) -> dict[str, Any]:
    edge_groups: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        edge_groups[str(row.get("edge") or "unknown")].append(row)
    expected_direction = {"vah": "long", "val": "short"}
    mismatches = [
        row
        for row in rows
        if row.get("edge") in expected_direction
        and row.get("direction") != expected_direction[row["edge"]]
    ]
    return {
        "edge_groups": _group_summary(rows, "edge"),
        "expected_breakout_mapping": expected_direction,
        "observed_edge_direction_mismatches": len(mismatches),
        "edge_and_direction_collinear": not mismatches
        and all(edge in expected_direction for edge in edge_groups),
        "interpretation": "VAH/long and VAL/short are collinear in this frozen breakout candidate; edge-only OI splits require same-direction and year controls.",
    }


def build_study(
    report_path: Path = SAVED_REPORT,
    oi_root: Path = OI_ROOT,
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    logging.disable(logging.CRITICAL)
    baseline = baseline or replay_frozen_baseline(report_path)
    if baseline["status"] != "parity_passed":
        return {
            "status": "parity_failed",
            "version": VERSION,
            "baseline": {key: value for key, value in baseline.items() if not key.startswith("_")},
            "oi_analysis": "stopped_before_archive_read",
        }
    rows, replay_metadata = join_oi_context_streamed(
        baseline["_trade_rows"],
        discover_oi_files(oi_root, TRAIN_START, date(2026, 9, 15)),
        TRAIN_START,
        date(2026, 9, 15),
    )
    coverage = {
        "trade_rows": len(rows),
        "join_status": dict(sorted(Counter(row["oi_join_status"] for row in rows).items())),
        "chain_completeness": dict(
            sorted(Counter(row["oi_chain_completeness"] for row in rows).items())
        ),
        "complete_feature_rows": sum(
            all(row.get(field) is not None for field in FEATURE_FIELDS)
            and row.get("oi_chain_completeness") in COMPLETE_CHAIN_STATES
            for row in rows
        ),
        "future_same_date_rows_excluded": sum(
            int(row.get("oi_future_same_date_rows_excluded") or 0) for row in rows
        ),
        "selected_raw_rows": sum(int(row.get("oi_selected_raw_rows") or 0) for row in rows),
        "snapshot_session_rows": sum(int(row.get("oi_snapshot_session_rows") or 0) for row in rows),
        "selected_off_hour_rows": sum(int(row.get("oi_selected_off_hour_rows") or 0) for row in rows),
        "snapshot_off_hour_rows": sum(int(row.get("oi_snapshot_off_hour_rows") or 0) for row in rows),
        "selected_positive_oi_total": round(
            sum(float(row.get("oi_selected_positive_oi_total") or 0.0) for row in rows),
            4,
        ),
        "snapshot_positive_oi_total": round(
            sum(float(row.get("oi_snapshot_positive_oi_total") or 0.0) for row in rows),
            4,
        ),
        "positive_oi_shortfall": round(
            sum(float(row.get("oi_positive_oi_shortfall") or 0.0) for row in rows),
            4,
        ),
        "positive_oi_surplus_vs_snapshot": round(
            sum(float(row.get("oi_positive_oi_surplus_vs_snapshot") or 0.0) for row in rows),
            4,
        ),
        "positive_oi_economic_coverage": (
            round(
                1.0
                - sum(float(row.get("oi_positive_oi_shortfall") or 0.0) for row in rows)
                / sum(float(row.get("oi_snapshot_positive_oi_total") or 0.0) for row in rows),
                8,
            )
            if sum(float(row.get("oi_snapshot_positive_oi_total") or 0.0) for row in rows) > 0
            else None
        ),
    }
    output = {
        "status": "completed",
        "version": VERSION,
        "research_scope": {
            "label": "historical and retrospective offline context study",
            "symbol": "MNQ VP trades joined to QQQ unsigned OI",
            "oi_root": str(oi_root),
            "oi_universe": "QQQ monthly OI partial chain DTE 0-60",
            "no_theta_sdk": True,
            "no_future_eod_greeks_gex_or_dealer_sign": True,
            "2026_label": "same-event context was previously inspected; this run is retrospective descriptive analysis",
        },
        "baseline": {key: value for key, value in baseline.items() if not key.startswith("_")},
        "oi_input": replay_metadata,
        "coverage": coverage,
        "same_event_periods": _same_event_periods(rows),
        "by_year": _group_summary(rows, "year"),
        "by_direction": _group_summary(rows, "direction"),
        "by_edge": _group_summary(rows, "edge"),
        "by_expiry_listing_regime": _group_summary(rows, "oi_expiry_listing_regime"),
        "by_weekday": _group_summary(rows, "oi_trade_weekday"),
        "by_regime_weekday": _group_summary(
            [
                {
                    **row,
                    "regime_weekday": (
                        f"{row['oi_expiry_listing_regime']}__{row['oi_trade_weekday']}"
                    ),
                }
                for row in rows
            ],
            "regime_weekday",
        ),
        "by_year_direction": _group_summary(
            [{**row, "year_direction": f"{row['year']}__{row['direction']}"} for row in rows],
            "year_direction",
        ),
        "edge_direction_control": _edge_direction_control(rows),
        "listing_regime_diagnostics": _listing_regime_diagnostics(rows),
        "day_clusters": _date_clusters(rows),
        "threshold_analysis": _threshold_analysis(rows),
        "join_policy": {
            "entry_comparison": "aware vendor publication timestamp normalized to UTC and required <= VP entry UTC",
            "date_preference": "same America/New_York publication date; latest earlier publication date is retained as prior_date_stale",
            "chain_completeness": "selected raw rows and contract-key coverage are compared with the full vendor session; partial sessions remain labelled and are excluded from threshold calibration",
            "effective_session": "unknown_until_calendar_join",
            "calendar_dte_bucket": "expiration calendar date minus ET trade date, inclusive 0 or 1 calendar day; weekends and holidays are not converted into exchange sessions",
            "oi_sign": "unsigned put/call contract-right totals only",
            "expired_contracts": "excluded from structure metrics and counted in oi_expired_rows_excluded",
            "zero_oi": "retained as valid rows and counted; zero total yields matched_zero_total",
        },
        "interpretation": "Descriptive context for saved VP rows. No threshold was applied to alter trades, no uplift or causal strategy claim is made, and no production gate is changed.",
    }
    return output


def compact_result(result: dict[str, Any]) -> dict[str, Any]:
    """Keep CLI output concise while retaining the evidence needed for the report."""
    if result.get("status") != "completed":
        return result
    return {
        "status": result["status"],
        "version": result["version"],
        "baseline": result["baseline"],
        "oi_input": {
            key: result["oi_input"].get(key)
            for key in (
                "rows_read",
                "valid_qqq_rows",
                "invalid_rows",
                "compressed_bytes",
                "sessions_indexed",
                "needed_snapshot_date_count",
                "calendar",
                "calendar_flags",
            )
        },
        "coverage": result["coverage"],
        "same_event_periods": result["same_event_periods"],
        "by_year": result["by_year"],
        "by_direction": result["by_direction"],
        "by_edge": result["by_edge"],
        "edge_direction_control": result["edge_direction_control"],
        "by_expiry_listing_regime": result["by_expiry_listing_regime"],
        "by_weekday": result["by_weekday"],
        "by_regime_weekday": result["by_regime_weekday"],
        "listing_regime_diagnostics": result["listing_regime_diagnostics"],
        "by_year_direction": result["by_year_direction"],
        "threshold_analysis": result["threshold_analysis"],
        "day_clusters": {
            key: result["day_clusters"][key]
            for key in (
                "trade_count",
                "independent_dates",
                "single_trade_dates",
                "multi_trade_dates",
            )
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=SAVED_REPORT)
    parser.add_argument("--oi-root", type=Path, default=OI_ROOT)
    parser.add_argument("--parity-only", action="store_true")
    parser.add_argument("--full", action="store_true", help="include full date-cluster detail")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.disable(logging.CRITICAL)
    baseline = replay_frozen_baseline(args.report)
    if args.parity_only:
        print(json.dumps({key: value for key, value in baseline.items() if not key.startswith("_")}, indent=2, default=_json_default))
        return 0 if baseline["status"] == "parity_passed" else 2
    if baseline["status"] != "parity_passed":
        output = {
            "status": "parity_failed",
            "version": VERSION,
            "baseline": {key: value for key, value in baseline.items() if not key.startswith("_")},
            "oi_analysis": "stopped_before_archive_read",
        }
        print(json.dumps(output, indent=2, default=_json_default))
        return 2
    result = build_study(args.report, args.oi_root, baseline=baseline)
    printable = result if args.full else compact_result(result)
    print(json.dumps(printable, ensure_ascii=False, indent=2, default=_json_default))
    return 0 if result.get("status") == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
