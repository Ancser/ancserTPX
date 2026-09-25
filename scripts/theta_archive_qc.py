"""Offline, read-only QC for the completed Theta Data archive.

The audit reads the append-only manifest and immutable gzip CSV responses. It
does not authenticate, contact Theta, alter raw data, or write a report. Use
``--json`` for a machine-readable summary.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import os
import shutil
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


DEFAULT_ROOT = Path(r"F:\ancserQuant\ancserMarketData\source\options\thetadata")
EXPECTED_MONTHLY_REQUESTS = 276
EXPECTED_EPISODE_REQUESTS = 30
OI_REFERENCE_MINUTE_ET = 6 * 60 + 30
OI_AROUND_MINUTES = 90


def _json(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json(item) for item in value]
    return value


def _read_manifest(root: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    path = root / "coverage_manifest.jsonl"
    all_records: list[dict[str, Any]] = []
    latest: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return all_records, latest
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                all_records.append({"_invalid_line": line_number})
                continue
            if not isinstance(record, dict):
                all_records.append({"_invalid_line": line_number})
                continue
            all_records.append(record)
            request_id = record.get("request_id")
            if request_id:
                latest[str(request_id)] = record
    return all_records, latest


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _parse_datetime(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    # Theta responses can use either ISO's ``-04:00`` form or the compact
    # ``-0400`` form.  Normalize the latter without changing the timestamp's
    # instant or timezone semantics.
    if len(value) >= 5 and value[-5] in "+-" and value[-4:].isdigit():
        value = value[:-5] + value[-5:-2] + ":" + value[-2:]
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _observation_date(value: str) -> str | None:
    parsed = _parse_datetime(value)
    return parsed.date().isoformat() if parsed is not None else None


def _numeric(value: str) -> float | None:
    value = value.strip()
    if not value:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _natural_key(header: list[str], fields: list[str], dataset: str) -> tuple[str, ...] | None:
    positions = {name.lower(): index for index, name in enumerate(header)}
    required = ("symbol", "expiration", "strike", "right")
    if any(name not in positions for name in required):
        return None
    names = list(required)
    timestamp_field = "created" if "eod" in dataset else "timestamp"
    if timestamp_field in positions:
        names.append(timestamp_field)
    try:
        return tuple(fields[positions[name]] for name in names)
    except IndexError:
        return None


def _date_field(dataset: str, header: list[str]) -> str | None:
    if "eod" in dataset and "created" in header:
        return "created"
    if "timestamp" in header:
        return "timestamp"
    if "created" in header:
        return "created"
    return None


def _publication_minute_et(value: str, ny: ZoneInfo) -> int | None:
    parsed = _parse_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        local = parsed.replace(tzinfo=ny)
    else:
        local = parsed.astimezone(ny)
    return local.hour * 60 + local.minute + local.second / 60


def _audit_file(root: Path, record: dict[str, Any], *, max_keys_per_file: int) -> dict[str, Any]:
    relative = str(record.get("response_path") or "")
    path = root / relative
    result: dict[str, Any] = {
        "request_id": record.get("request_id"),
        "dataset": record.get("dataset"),
        "symbol": record.get("symbol"),
        "archive_month": record.get("archive_month"),
        "coverage_start_date": record.get("coverage_start_date"),
        "coverage_end_date": record.get("coverage_end_date"),
        "response_path": relative,
        "exists": path.is_file(),
        "manifest_rows": record.get("rows"),
        "manifest_raw_bytes": record.get("raw_bytes"),
        "manifest_compressed_bytes": record.get("compressed_bytes"),
        "manifest_sha256_raw": record.get("sha256_raw"),
        "manifest_sha256_gzip": record.get("sha256_gzip"),
        "rows": 0,
        "raw_bytes": 0,
        "compressed_bytes": 0,
        "sha256_raw": None,
        "sha256_gzip": None,
        "header": [],
        "malformed_rows": 0,
        "exact_duplicate_rows": 0,
        "natural_duplicate_rows": 0,
        "duplicate_check": "complete",
        "date_field": None,
        "observed_dates": [],
        "invalid_date_values": 0,
        "negative_counts": {},
        "zero_counts": {},
        "nan_counts": {},
        "oi_publication": None,
    }
    if not path.is_file():
        result["file_error"] = "missing_response_file"
        return result

    gzip_hash, compressed_size = _sha256_file(path)
    result["sha256_gzip"] = gzip_hash
    result["compressed_bytes"] = compressed_size
    exact_keys: set[bytes] = set()
    natural_keys: set[tuple[str, ...]] = set()
    observed_dates: set[str] = set()
    negative_counts: Counter[str] = Counter()
    zero_counts: Counter[str] = Counter()
    nan_counts: Counter[str] = Counter()
    publication_minutes: list[float] = []
    off_hour_dates: Counter[str] = Counter()
    off_hour_times: Counter[str] = Counter()
    try:
        ny = ZoneInfo("America/New_York")
    except Exception:
        ny = timezone.utc  # Only affects the diagnostic clock check.

    with gzip.open(path, "rb") as stream:
        raw_digest = hashlib.sha256()
        first_line = next(stream, b"")
        raw_digest.update(first_line)
        result["raw_bytes"] += len(first_line)
        try:
            header = next(csv.reader([first_line.decode("utf-8")])) if first_line else []
        except (UnicodeDecodeError, csv.Error):
            header = []
            result["malformed_rows"] += 1
        result["header"] = header
        dataset = str(record.get("dataset") or "")
        date_field = _date_field(dataset, header)
        result["date_field"] = date_field
        positions = {name: index for index, name in enumerate(header)}
        for raw_line in stream:
            raw_digest.update(raw_line)
            result["raw_bytes"] += len(raw_line)
            result["rows"] += 1
            try:
                decoded = raw_line.decode("utf-8")
                fields = next(csv.reader([decoded]))
            except (UnicodeDecodeError, csv.Error, StopIteration):
                result["malformed_rows"] += 1
                continue
            if len(fields) != len(header):
                result["malformed_rows"] += 1
                continue
            exact_key = raw_line.rstrip(b"\r\n")
            if len(exact_keys) < max_keys_per_file:
                if exact_key in exact_keys:
                    result["exact_duplicate_rows"] += 1
                exact_keys.add(exact_key)
            else:
                result["duplicate_check"] = "capped"
            natural_key = _natural_key(header, fields, dataset)
            if natural_key is not None:
                if len(natural_keys) < max_keys_per_file:
                    if natural_key in natural_keys:
                        result["natural_duplicate_rows"] += 1
                    natural_keys.add(natural_key)
                else:
                    result["duplicate_check"] = "capped"
            if date_field is not None:
                raw_date = fields[positions[date_field]]
                observed = _observation_date(raw_date)
                if observed is None:
                    result["invalid_date_values"] += 1
                else:
                    observed_dates.add(observed)
                if "open_interest" in positions and date_field == "timestamp":
                    minute = _publication_minute_et(raw_date, ny)
                    if minute is not None:
                        publication_minutes.append(minute)
                        if abs(minute - OI_REFERENCE_MINUTE_ET) > OI_AROUND_MINUTES:
                            parsed = _parse_datetime(raw_date)
                            if parsed is not None:
                                local = parsed if parsed.tzinfo is None else parsed.astimezone(ny)
                                off_hour_dates[local.date().isoformat()] += 1
                                off_hour_times[local.strftime("%H:%M:%S%z")] += 1
            for name, index in positions.items():
                if name in {"symbol", "expiration", "right", "created", "last_trade", "timestamp", "underlying_timestamp", "bid_exchange", "ask_exchange", "bid_condition", "ask_condition"}:
                    continue
                numeric = _numeric(fields[index])
                if numeric is None:
                    if fields[index].strip().lower() in {"nan", "inf", "+inf", "-inf"}:
                        nan_counts[name] += 1
                    continue
                if numeric < 0:
                    negative_counts[name] += 1
                if numeric == 0:
                    zero_counts[name] += 1
        result["sha256_raw"] = raw_digest.hexdigest()

    result["observed_dates"] = sorted(observed_dates)
    result["negative_counts"] = dict(negative_counts)
    result["zero_counts"] = dict(zero_counts)
    result["nan_counts"] = dict(nan_counts)
    if publication_minutes:
        in_window = [abs(value - OI_REFERENCE_MINUTE_ET) <= OI_AROUND_MINUTES for value in publication_minutes]
        result["oi_publication"] = {
            "rows_with_time": len(publication_minutes),
            "min_et": min(publication_minutes),
            "max_et": max(publication_minutes),
            "median_et": sorted(publication_minutes)[len(publication_minutes) // 2],
            "within_90_minutes_of_0630_et": sum(in_window),
            "outside_90_minutes_of_0630_et": len(publication_minutes) - sum(in_window),
            "off_hour_date_counts": dict(off_hour_dates),
            "off_hour_time_counts_et": dict(off_hour_times),
        }
    result["manifest_rows_match"] = result["rows"] == record.get("rows")
    result["manifest_raw_bytes_match"] = result["raw_bytes"] == record.get("raw_bytes")
    result["manifest_compressed_bytes_match"] = result["compressed_bytes"] == record.get("compressed_bytes")
    result["manifest_sha256_raw_match"] = result["sha256_raw"] == record.get("sha256_raw")
    result["manifest_sha256_gzip_match"] = result["sha256_gzip"] == record.get("sha256_gzip")
    return result


def _discover_calendar() -> tuple[str | None, Any | None, str | None]:
    try:
        import exchange_calendars as exchange_calendars  # type: ignore

        return "exchange_calendars", exchange_calendars.get_calendar("XNYS"), None
    except Exception as exchange_error:
        try:
            import pandas_market_calendars as pandas_market_calendars  # type: ignore

            return "pandas_market_calendars", pandas_market_calendars.get_calendar("XNYS"), None
        except Exception:
            return None, None, f"exchange calendar packages unavailable ({type(exchange_error).__name__})"


def _calendar_sessions(calendar: Any, start: date, end: date) -> set[str]:
    if hasattr(calendar, "schedule"):
        schedule = calendar.schedule(start_date=start.isoformat(), end_date=end.isoformat())
        return {index.date().isoformat() if hasattr(index, "date") else str(index)[:10] for index in schedule.index}
    valid_days = calendar.valid_days(start_date=start.isoformat(), end_date=end.isoformat())
    return {index.date().isoformat() if hasattr(index, "date") else str(index)[:10] for index in valid_days}


def _coverage_compare(
    observed: Iterable[str], start: date, end: date, calendar: Any | None,
) -> dict[str, Any]:
    observed_set = set(observed)
    if calendar is None:
        return {
            "status": "calendar_unavailable",
            "expected_session_count": None,
            "missing_session_count": None,
            "missing_dates": [],
            "observed_outside_expected_count": None,
        }
    expected = _calendar_sessions(calendar, start, end)
    return {
        "status": "compared",
        "expected_session_count": len(expected),
        "missing_session_count": len(expected - observed_set),
        "missing_dates": sorted(expected - observed_set),
        "observed_outside_expected_count": len(observed_set - expected),
    }


def _public_file_result(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if key not in {"header"}}


def audit_archive(
    root: Path = DEFAULT_ROOT,
    *,
    expected_monthly: int = EXPECTED_MONTHLY_REQUESTS,
    expected_episode: int = EXPECTED_EPISODE_REQUESTS,
    max_keys_per_file: int = 1_000_000,
    calendar: Any | None = None,
    calendar_name: str | None = None,
) -> dict[str, Any]:
    all_records, latest = _read_manifest(root)
    monthly = [record for record in latest.values() if record.get("archive_month")]
    episodes = [record for record in latest.values() if not record.get("archive_month")]
    file_results = [_audit_file(root, record, max_keys_per_file=max_keys_per_file) for record in latest.values()]
    expected_paths = {str(record.get("response_path") or "") for record in latest.values()}
    actual_paths = {
        str(path.relative_to(root)).replace(os.sep, "/")
        for path in (root / "raw").rglob("*.gz")
        if path.is_file()
    } if (root / "raw").is_dir() else set()
    hash_mismatches = [item["response_path"] for item in file_results if item.get("exists") and not all(item.get(name) for name in (
        "manifest_rows_match", "manifest_raw_bytes_match", "manifest_compressed_bytes_match",
        "manifest_sha256_raw_match", "manifest_sha256_gzip_match",
    ))]
    missing_files = [item["response_path"] for item in file_results if not item.get("exists")]
    monthly_by_pair: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for item in file_results:
        if item.get("archive_month"):
            monthly_by_pair[(str(item.get("symbol")), str(item["archive_month"]))][str(item["dataset"])] = item
    monthly_coverage: list[dict[str, Any]] = []
    for (symbol, month), datasets in sorted(monthly_by_pair.items()):
        start = date.fromisoformat(f"{month}-01")
        if start.month == 12:
            next_month = date(start.year + 1, 1, 1)
        else:
            next_month = date(start.year, start.month + 1, 1)
        end = next_month.fromordinal(next_month.toordinal() - 1)
        for item in datasets.values():
            coverage_start = item.get("coverage_start_date")
            coverage_end = item.get("coverage_end_date")
            if coverage_start:
                start = date.fromisoformat(str(coverage_start))
            if coverage_end:
                end = date.fromisoformat(str(coverage_end))
        eod = datasets.get("monthly_eod_dte_0_60") or next((item for key, item in datasets.items() if "eod" in key), None)
        oi = datasets.get("monthly_open_interest_dte_0_60") or next((item for key, item in datasets.items() if "open_interest" in key), None)
        monthly_coverage.append({
            "symbol": symbol,
            "month": month,
            "eod_created": {
                "date_count": len((eod or {}).get("observed_dates", [])),
                "first": ((eod or {}).get("observed_dates") or [None])[0],
                "last": ((eod or {}).get("observed_dates") or [None])[-1],
                "dates": (eod or {}).get("observed_dates", []),
                "calendar": _coverage_compare((eod or {}).get("observed_dates", []), start, end, calendar),
            },
            "oi_timestamp": {
                "date_count": len((oi or {}).get("observed_dates", [])),
                "first": ((oi or {}).get("observed_dates") or [None])[0],
                "last": ((oi or {}).get("observed_dates") or [None])[-1],
                "dates": (oi or {}).get("observed_dates", []),
                "calendar": _coverage_compare((oi or {}).get("observed_dates", []), start, end, calendar),
                "effective_session": "unknown_until_calendar_join",
                "publication_time": (oi or {}).get("oi_publication"),
            },
        })
    by_symbol: dict[str, dict[str, Any]] = {}
    for symbol in sorted({str(item.get("symbol")) for item in file_results}):
        items = [item for item in file_results if str(item.get("symbol")) == symbol]
        dates = [value for item in items for value in item.get("observed_dates", [])]
        by_symbol[symbol] = {
            "files": len(items),
            "rows": sum(int(item.get("rows") or 0) for item in items),
            "raw_bytes": sum(int(item.get("raw_bytes") or 0) for item in items),
            "compressed_bytes": sum(int(item.get("compressed_bytes") or 0) for item in items),
            "first_observation_date": min(dates) if dates else None,
            "last_observation_date": max(dates) if dates else None,
        }
    negative_counts: Counter[str] = Counter()
    zero_counts: Counter[str] = Counter()
    nan_counts: Counter[str] = Counter()
    for item in file_results:
        negative_counts.update(item.get("negative_counts", {}))
        zero_counts.update(item.get("zero_counts", {}))
        nan_counts.update(item.get("nan_counts", {}))
    oi_files = [item for item in file_results if "open_interest" in str(item.get("dataset"))]
    publication_rows = sum(int((item.get("oi_publication") or {}).get("rows_with_time", 0)) for item in oi_files)
    publication_in_window = sum(int((item.get("oi_publication") or {}).get("within_90_minutes_of_0630_et", 0)) for item in oi_files)
    publication_outside = sum(int((item.get("oi_publication") or {}).get("outside_90_minutes_of_0630_et", 0)) for item in oi_files)
    off_hour_dates: Counter[str] = Counter()
    off_hour_times: Counter[str] = Counter()
    for item in oi_files:
        publication = item.get("oi_publication") or {}
        off_hour_dates.update(publication.get("off_hour_date_counts", {}))
        off_hour_times.update(publication.get("off_hour_time_counts_et", {}))
    usage = shutil.disk_usage(root if root.exists() else root.parent)
    tree_bytes = sum(path.stat().st_size for path in root.rglob("*") if path.is_file()) if root.exists() else 0
    calendar_info = {
        "name": calendar_name,
        "available": calendar is not None,
        "reason": None if calendar is not None else "No official exchange calendar package is installed locally; session-gap comparison is intentionally indeterminate.",
    }
    return _json({
        "root": str(root),
        "manifest": str(root / "coverage_manifest.jsonl"),
        "manifest_lines": len(all_records),
        "invalid_manifest_lines": sum(1 for record in all_records if record.get("_invalid_line")),
        "latest_unique_requests": len(latest),
        "superseded_manifest_records": len(all_records) - len(latest),
        "latest_status_counts": dict(Counter(str(record.get("status")) for record in latest.values())),
        "request_counts": {
            "monthly": len(monthly),
            "monthly_expected": expected_monthly,
            "monthly_count_match": len(monthly) == expected_monthly,
            "episode": len(episodes),
            "episode_expected": expected_episode,
            "episode_count_match": len(episodes) == expected_episode,
            "total_expected": expected_monthly + expected_episode,
            "total_count_match": len(latest) == expected_monthly + expected_episode,
        },
        "raw_file_counts": {
            "manifest_expected": len(expected_paths),
            "actual_gzip_files": len(actual_paths),
            "missing_files": missing_files,
            "unexpected_files": sorted(actual_paths - expected_paths),
        },
        "integrity": {
            "files_audited": len(file_results),
            "hash_or_row_mismatches": hash_mismatches,
            "all_hashes_and_rows_match": not hash_mismatches and not missing_files,
        },
        "totals": {
            "rows": sum(int(item.get("rows") or 0) for item in file_results),
            "raw_bytes": sum(int(item.get("raw_bytes") or 0) for item in file_results),
            "compressed_bytes": sum(int(item.get("compressed_bytes") or 0) for item in file_results),
            "tree_bytes": tree_bytes,
            "free_bytes": usage.free,
            "free_gib": round(usage.free / 1024**3, 3),
        },
        "calendar": calendar_info,
        "monthly_coverage": monthly_coverage,
        "per_symbol": by_symbol,
        "quality": {
            "exact_duplicate_rows": sum(int(item.get("exact_duplicate_rows") or 0) for item in file_results),
            "natural_duplicate_rows": sum(int(item.get("natural_duplicate_rows") or 0) for item in file_results),
            "duplicate_checks_capped": [item["response_path"] for item in file_results if item.get("duplicate_check") == "capped"],
            "malformed_rows": sum(int(item.get("malformed_rows") or 0) for item in file_results),
            "invalid_date_values": sum(int(item.get("invalid_date_values") or 0) for item in file_results),
            "negative_counts": dict(negative_counts),
            "zero_counts": dict(zero_counts),
            "nan_counts": dict(nan_counts),
            "oi_publication": {
                "rows_with_timestamp": publication_rows,
                "within_90_minutes_of_0630_et": publication_in_window,
                "outside_90_minutes_of_0630_et": publication_outside,
                "top_off_hour_dates": off_hour_dates.most_common(10),
                "top_off_hour_times_et": off_hour_times.most_common(10),
                "effective_session_policy": "unknown_until_calendar_join",
            },
        },
        "files": [_public_file_result(item) for item in file_results],
    })


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--json", action="store_true", help="emit the full machine-readable audit")
    parser.add_argument("--max-duplicate-keys", type=int, default=1_000_000)
    args = parser.parse_args(argv)
    calendar_name, calendar, _reason = _discover_calendar()
    result = audit_archive(
        args.root,
        max_keys_per_file=args.max_duplicate_keys,
        calendar=calendar,
        calendar_name=calendar_name,
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(json.dumps({
            "request_counts": result["request_counts"],
            "integrity": result["integrity"],
            "totals": result["totals"],
            "calendar": result["calendar"],
            "quality": result["quality"],
            "per_symbol": result["per_symbol"],
        }, indent=2, sort_keys=True))
    return 0 if result["integrity"]["all_hashes_and_rows_match"] and result["request_counts"]["total_count_match"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
