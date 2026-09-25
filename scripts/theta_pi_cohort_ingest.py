"""Research-only Theta minute quote/first-order Greek ingestion for PI cohorts.

The runner reads the canonical PI history and the already downloaded monthly
EOD chain to derive one exact next-session expiration per symbol/event date.
It requests only two days of all-strike 1m quote and first-order Greek data.
``--plan`` is fully offline; ``--pilot`` and ``--run-remaining`` are the only
network-capable modes and use the isolated Theta SDK through the existing
ingest adapter.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import shutil
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.data import pi_history  # noqa: E402
from backend.live.pi_listener import DIRECTION  # noqa: E402
from scripts import theta_data_ingest as ingest  # noqa: E402


EXPECTED_COHORTS = 240
EXPECTED_BY_SYMBOL = {"QQQ": 122, "SPY": 118}
EXPECTED_REQUESTS = EXPECTED_COHORTS * 4
NY = ZoneInfo("America/New_York")
UTC = timezone.utc
DATASET_KEYS = ("quote_1m", "greeks_first_order_1m")
ACCEPTED_STATUSES = {"downloaded", "cached", "missing"}


def _json(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json(item) for item in value]
    return ingest._json_value(value)


def _parse_vendor_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _source_time_values(row: dict[str, Any]) -> tuple[datetime, str, str]:
    raw = str(row["ts"])
    parsed = pi_history.parse_ts(raw)
    local = parsed.astimezone(NY)
    return parsed, raw, local.isoformat()


def canonical_marks(*, pi_path: Path | None = None) -> list[dict[str, Any]]:
    """Return canonical, loader-filtered PI marks with source provenance."""
    rows = pi_history.load_rows(path=pi_path)
    marks: list[dict[str, Any]] = []
    seen: set[tuple[str, date, str, str, Any]] = set()
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        if symbol not in EXPECTED_BY_SYMBOL:
            continue
        try:
            parsed, raw_source_time, source_time_et = _source_time_values(row)
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        event_date = parsed.astimezone(NY).date()
        for mark in row.get("marks") or []:
            if not isinstance(mark, dict):
                continue
            kind = mark.get("kind")
            if kind not in DIRECTION:
                continue
            level = mark.get("level")
            identity = (symbol, event_date, raw_source_time, str(kind), level)
            if identity in seen:
                continue
            seen.add(identity)
            direction = int(DIRECTION[kind])
            marks.append({
                "symbol": symbol,
                "event_date": event_date,
                "kind": kind,
                "level": level,
                "source_level": level,
                "direction": direction,
                "option_right": "CALL" if direction > 0 else "PUT",
                "source_timestamp_utc": raw_source_time,
                "source_timestamp_et": source_time_et,
                "source_row_id": row.get("id"),
                "source_size": mark.get("size"),
                "source_count": mark.get("count"),
            })
    marks.sort(key=lambda item: (
        item["symbol"], item["event_date"], item["source_timestamp_utc"],
        str(item["kind"]), str(item["level"]),
    ))
    return marks


def _month_slugs(dates: Iterable[date]) -> list[str]:
    return sorted({item.strftime("%Y-%m") for item in dates})


def _load_eod_index(
    root: Path,
    *,
    symbols: Iterable[str],
    event_dates: dict[str, set[date]],
) -> tuple[dict[str, set[date]], dict[tuple[str, date], set[date]], dict[tuple[str, date], str]]:
    """Index observed EOD dates and listed expirations without a calendar."""
    observed: dict[str, set[date]] = defaultdict(set)
    expirations: dict[tuple[str, date], set[date]] = defaultdict(set)
    source_paths: dict[tuple[str, date], str] = {}
    for symbol in sorted(set(symbols)):
        months = _month_slugs(event_dates[symbol])
        for month_slug in months:
            path = root / "raw" / symbol / "monthly_eod_dte_0_60" / f"month={month_slug}.csv.gz"
            if not path.is_file():
                raise FileNotFoundError(f"missing_eod_archive:{path}")
            relative = str(path.relative_to(root)).replace("\\", "/")
            with gzip.open(path, "rt", encoding="utf-8", newline="") as stream:
                for row in csv.DictReader(stream):
                    created_date = _parse_vendor_date(row.get("created"))
                    expiration = _parse_vendor_date(row.get("expiration"))
                    if created_date is None:
                        continue
                    observed[symbol].add(created_date)
                    if created_date in event_dates[symbol] and expiration is not None:
                        expirations[(symbol, created_date)].add(expiration)
                        source_paths[(symbol, created_date)] = relative
    return observed, expirations, source_paths


def _resolve_expirations(
    root: Path,
    *,
    symbols: Iterable[str],
    event_dates: dict[str, set[date]],
) -> dict[tuple[str, date], dict[str, Any]]:
    observed, expirations, source_paths = _load_eod_index(
        root, symbols=symbols, event_dates=event_dates,
    )
    resolved: dict[tuple[str, date], dict[str, Any]] = {}
    for symbol in sorted(set(symbols)):
        for event_date in sorted(event_dates[symbol]):
            next_session = min(
                (candidate for candidate in observed[symbol] if candidate > event_date),
                default=None,
            )
            listed = expirations[(symbol, event_date)]
            if next_session is None:
                raise ValueError(f"next_session_not_observed:{symbol}:{event_date.isoformat()}")
            if next_session not in listed:
                listed_text = ",".join(item.isoformat() for item in sorted(listed)[:8])
                raise ValueError(
                    f"next_session_expiry_missing:{symbol}:{event_date.isoformat()}"
                    f":expected={next_session.isoformat()}:listed_sample={listed_text}"
                )
            resolved[(symbol, event_date)] = {
                "exit_date": next_session,
                "expiration": next_session,
                "eod_source_path": source_paths[(symbol, event_date)],
                "resolution": "next_observed_EOD_session_and_exact_listed_expiration",
            }
    return resolved


def build_cohorts(
    *,
    root: Path | None = None,
    pi_path: Path | None = None,
    expected_total: int | None = EXPECTED_COHORTS,
    expected_by_symbol: dict[str, int] | None = EXPECTED_BY_SYMBOL,
) -> list[dict[str, Any]]:
    """Build the canonical symbol/event/next-session-expiry cohort list."""
    data_root = root or ingest.default_data_root()
    marks = canonical_marks(pi_path=pi_path)
    grouped: dict[tuple[str, date], list[dict[str, Any]]] = defaultdict(list)
    for mark in marks:
        grouped[(mark["symbol"], mark["event_date"])].append(mark)
    event_dates: dict[str, set[date]] = defaultdict(set)
    for symbol, event_date in grouped:
        event_dates[symbol].add(event_date)
    resolved = _resolve_expirations(
        data_root, symbols=event_dates, event_dates=event_dates,
    )
    cohorts: list[dict[str, Any]] = []
    for (symbol, event_date), cohort_marks in sorted(grouped.items()):
        resolution = resolved[(symbol, event_date)]
        directions = sorted({int(mark["direction"]) for mark in cohort_marks})
        cohort_id = f"{symbol}:{event_date.isoformat()}:{resolution['expiration'].isoformat()}"
        cohorts.append({
            "cohort_id": cohort_id,
            "symbol": symbol,
            "event_date": event_date,
            "exit_date": resolution["exit_date"],
            "expiration": resolution["expiration"],
            "marks": cohort_marks,
            "mark_count": len(cohort_marks),
            "directions": directions,
            "has_long": 1 in directions,
            "has_short": -1 in directions,
            "option_rights_by_direction": {"long": "CALL", "short": "PUT"},
            "expiration_resolution": resolution,
        })
    counts = Counter(item["symbol"] for item in cohorts)
    if expected_total is not None and len(cohorts) != expected_total:
        raise ValueError(f"unexpected_cohort_count:{len(cohorts)}:expected={expected_total}")
    if expected_by_symbol is not None and dict(counts) != dict(expected_by_symbol):
        raise ValueError(f"unexpected_cohort_symbol_counts:{dict(counts)}")
    return cohorts


def cohort_requests(cohort: dict[str, Any]) -> list[dict[str, Any]]:
    return ingest.selected_expiry_requests(
        symbol=cohort["symbol"],
        event_date=cohort["event_date"],
        exit_date=cohort["exit_date"],
        expiration=cohort["expiration"],
    )


def _cohort_json(cohort: dict[str, Any]) -> dict[str, Any]:
    return _json(cohort)


def _cache_state(request: dict[str, Any], root: Path, latest: dict[str, dict[str, Any]]) -> str:
    cached = ingest._cached_result(request, root, latest)
    return str(cached.get("status")) if cached else "not_present"


def _request_plan_entry(
    cohort: dict[str, Any],
    request: dict[str, Any],
    *,
    root: Path,
    latest: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    rid = ingest.request_id(request)
    previous = latest.get(rid) or {}
    return {
        "cohort_id": cohort["cohort_id"],
        "dataset": request["key"],
        "method": request["method"],
        "request_id": rid,
        "request_date": request["request_date"],
        "expiration": request["expiration"],
        "params": request["params"],
        "response_path": str(request["relative_path"]),
        "cache_status": _cache_state(request, root, latest),
        "manifest_status": previous.get("status"),
        "manifest_sha256_raw": previous.get("sha256_raw"),
        "manifest_sha256_gzip": previous.get("sha256_gzip"),
    }


def _capacity_snapshot(root: Path) -> dict[str, Any]:
    usage = shutil.disk_usage(ingest._nearest_existing(root))
    tree_bytes = ingest._tree_bytes(root)
    return {
        "tree_bytes": tree_bytes,
        "tree_gib": round(tree_bytes / 1024**3, 3),
        "free_bytes": usage.free,
        "free_gib": round(usage.free / 1024**3, 3),
        "tree_budget_bytes": ingest.MAX_TREE_BYTES,
        "free_reserve_bytes": ingest.MIN_FREE_BYTES,
        "within_budget": ingest.check_capacity(root) is None,
    }


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(_json(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def build_request_manifest(
    cohorts: list[dict[str, Any]],
    *,
    root: Path,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    """Write and return the derived cohort/request manifest.

    Every cache decision rechecks the latest request manifest and the raw gzip
    hash through the existing adapter before reporting ``cached``.
    """
    latest = ingest.read_latest_manifest(root)
    entries: list[dict[str, Any]] = []
    for cohort in cohorts:
        entries.extend(
            _request_plan_entry(cohort, request, root=root, latest=latest)
            for request in cohort_requests(cohort)
        )
    cache_counts = Counter(item["cache_status"] for item in entries)
    earliest_qqq_long = next((item["cohort_id"] for item in cohorts if item["symbol"] == "QQQ" and item["has_long"]), None)
    earliest_spy_short = next((item["cohort_id"] for item in cohorts if item["symbol"] == "SPY" and item["has_short"]), None)
    try:
        actual_selected = choose_pilot_cohorts(cohorts, root=root)
    except ValueError:
        # Small offline fixtures may intentionally contain one symbol or one
        # direction; retain the request plan without inventing a pilot.
        actual_selected = None
    payload = {
        "schema_version": 1,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_pi_path": str(pi_history.HIST_PATH),
        "data_root": str(root),
        "cohort_count": len(cohorts),
        "cohort_counts_by_symbol": dict(Counter(item["symbol"] for item in cohorts)),
        "canonical_mark_count": sum(int(item["mark_count"]) for item in cohorts),
        "planned_requests": len(entries),
        "expected_requests": EXPECTED_REQUESTS,
        "cache_status_counts": dict(cache_counts),
        "capacity": _capacity_snapshot(root),
        "pilot_candidates": {
            "earliest_spy_short": earliest_spy_short,
            "earliest_qqq_long": earliest_qqq_long,
        },
        "actual_selected_pilot": (
            None
            if actual_selected is None
            else {role: cohort["cohort_id"] for role, cohort in actual_selected}
        ),
        "cohorts": [_cohort_json(item) for item in cohorts],
        "requests": [_json(item) for item in entries],
    }
    if manifest_path is not None:
        _write_json_atomic(manifest_path, payload)
    return payload


def _cohort_all_cached(cohort: dict[str, Any], *, root: Path, latest: dict[str, dict[str, Any]]) -> bool:
    return all(_cache_state(request, root, latest) == "cached" for request in cohort_requests(cohort))


def choose_pilot_cohorts(
    cohorts: list[dict[str, Any]], *, root: Path,
) -> list[tuple[str, dict[str, Any]]]:
    latest = ingest.read_latest_manifest(root)
    qqq_long = [item for item in cohorts if item["symbol"] == "QQQ" and item["has_long"]]
    qqq_long.sort(key=lambda item: (not _cohort_all_cached(item, root=root, latest=latest), item["event_date"], item["cohort_id"]))
    spy_short = sorted(
        (item for item in cohorts if item["symbol"] == "SPY" and item["has_short"]),
        key=lambda item: (item["event_date"], item["cohort_id"]),
    )
    if not qqq_long or not spy_short:
        raise ValueError("required_pilot_direction_missing")
    return [("spy_short", spy_short[0]), ("qqq_long", qqq_long[0])]


def verify_cohort_coverage(
    cohort: dict[str, Any], result: dict[str, Any],
) -> dict[str, Any]:
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for item in result.get("requests", []):
        by_key[(str(item.get("key")), str(item.get("request_id")))] = item
    coverage: dict[str, dict[str, Any]] = defaultdict(dict)
    missing: list[str] = []
    for request in cohort_requests(cohort):
        item = next(
            (candidate for candidate in result.get("requests", [])
             if candidate.get("request_id") == ingest.request_id(request)),
            None,
        )
        day = request["request_date"].isoformat()
        dataset = request["key"]
        if item is None:
            missing.append(f"{day}:{dataset}:not_attempted")
            continue
        coverage[day][dataset] = {
            "status": item.get("status"),
            "rows": item.get("rows", 0),
            "contracts": item.get("contracts", 0),
            "path": item.get("path", str(request["relative_path"])),
        }
        if item.get("status") not in {"downloaded", "cached"} or int(item.get("rows", 0)) <= 0 or int(item.get("contracts", 0)) <= 0:
            missing.append(f"{day}:{dataset}:{item.get('status')}")
    return {
        "cohort_id": cohort["cohort_id"],
        "coverage_by_date": dict(coverage),
        "complete_quote_and_first_order_both_days": not missing,
        "missing_or_invalid": missing,
    }


def _cached_only_result(cohort: dict[str, Any], *, root: Path) -> dict[str, Any] | None:
    latest = ingest.read_latest_manifest(root)
    requests = cohort_requests(cohort)
    cached_items = []
    for request in requests:
        cached = ingest._cached_result(request, root, latest)
        if cached is None or cached.get("status") != "cached":
            return None
        cached_items.append(cached)
    return {
        "status": "complete",
        "planned_requests": len(requests),
        "attempted_or_cached_requests": len(requests),
        "status_counts": {"cached": len(requests)},
        "rows": sum(int(item.get("rows", 0)) for item in cached_items),
        "contracts": sum(int(item.get("contracts", 0)) for item in cached_items),
        "compressed_bytes": sum(int(item.get("compressed_bytes", 0)) for item in cached_items),
        "requests": cached_items,
        "pid": os.getpid(),
    }


def _progress(path: Path, payload: dict[str, Any], *, emit: bool = True) -> None:
    encoded = json.dumps(_json(payload), sort_keys=True, separators=(",", ":"))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as stream:
        stream.write(encoded + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    if emit:
        print(encoded, flush=True)


def _run_cohort(
    cohort: dict[str, Any], *, root: Path, progress_log: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    requests = cohort_requests(cohort)
    _progress(progress_log, {
        "event": "cohort_started",
        "pid": os.getpid(),
        "cohort_id": cohort["cohort_id"],
        "symbol": cohort["symbol"],
        "event_date": cohort["event_date"],
        "exit_date": cohort["exit_date"],
        "expiration": cohort["expiration"],
        "planned_requests": len(requests),
    })
    result = _cached_only_result(cohort, root=root)
    if result is None:
        result = ingest.run_requests(
            requests,
            root=root,
            client=None,
            progress_log=progress_log,
            emit_progress=True,
        )
    verification = verify_cohort_coverage(cohort, result)
    _progress(progress_log, {
        "event": "cohort_complete",
        "pid": os.getpid(),
        "cohort_id": cohort["cohort_id"],
        "role": None,
        "status": result.get("status"),
        "status_counts": result.get("status_counts", {}),
        "rows": result.get("rows", 0),
        "contracts": result.get("contracts", 0),
        "complete_quote_and_first_order_both_days": verification["complete_quote_and_first_order_both_days"],
        "missing_or_invalid": verification["missing_or_invalid"],
    })
    return result, verification


def _write_summary(path: Path, payload: dict[str, Any]) -> None:
    _write_json_atomic(path, payload)


def run_pilot(
    *, root: Path, progress_log: Path, summary_path: Path, manifest_path: Path,
    pi_path: Path | None = None,
) -> dict[str, Any]:
    cohorts = build_cohorts(root=root, pi_path=pi_path)
    manifest = build_request_manifest(cohorts, root=root, manifest_path=manifest_path)
    selected = choose_pilot_cohorts(cohorts, root=root)
    outputs: list[dict[str, Any]] = []
    overall_status = "complete"
    for role, cohort in selected:
        result, verification = _run_cohort(cohort, root=root, progress_log=progress_log)
        outputs.append({
            "role": role,
            "cohort": _cohort_json(cohort),
            "result": result,
            "verification": verification,
        })
        if result.get("status") != "complete" or not verification["complete_quote_and_first_order_both_days"]:
            overall_status = "partial"
            break
    summary = {
        "schema_version": 1,
        "mode": "pilot",
        "status": overall_status,
        "pid": os.getpid(),
        "data_root": root,
        "manifest": manifest_path,
        "progress_log": progress_log,
        "summary_path": summary_path,
        "planned_cohorts": len(selected),
        "completed_cohorts": sum(1 for item in outputs if item["verification"]["complete_quote_and_first_order_both_days"]),
        "planned_requests": sum(len(cohort_requests(cohort)) for _, cohort in selected),
        "completed_requests": sum(int(item["result"].get("attempted_or_cached_requests", 0)) for item in outputs),
        "cohorts": outputs,
        "archive_cache_status_counts": manifest["cache_status_counts"],
        "capacity": _capacity_snapshot(root),
    }
    _write_summary(summary_path, summary)
    print(json.dumps(_json(summary), ensure_ascii=True, sort_keys=True), flush=True)
    return summary


def _month_batches(cohorts: Iterable[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for cohort in cohorts:
        grouped[cohort["event_date"].strftime("%Y-%m")].append(cohort)
    return [(month, sorted(items, key=lambda item: (item["event_date"], item["symbol"]))) for month, items in sorted(grouped.items())]


def run_remaining(
    *, root: Path, progress_log: Path, summary_path: Path, manifest_path: Path,
    pi_path: Path | None = None,
) -> dict[str, Any]:
    cohorts = build_cohorts(root=root, pi_path=pi_path)
    manifest = build_request_manifest(cohorts, root=root, manifest_path=manifest_path)
    selected = choose_pilot_cohorts(cohorts, root=root)
    pilot_ids = {cohort["cohort_id"] for _, cohort in selected}
    latest = ingest.read_latest_manifest(root)
    remaining = [
        cohort for cohort in cohorts
        if cohort["cohort_id"] not in pilot_ids
        or not _cohort_all_cached(cohort, root=root, latest=latest)
    ]
    batches = _month_batches(remaining)
    batch_outputs: list[dict[str, Any]] = []
    overall_status = "complete"
    for month, batch in batches:
        requests = [request for cohort in batch for request in cohort_requests(cohort)]
        _progress(progress_log, {
            "event": "monthly_batch_started",
            "pid": os.getpid(),
            "archive_month": month,
            "cohort_count": len(batch),
            "planned_requests": len(requests),
        })
        # run_requests(client=None) creates and closes one fresh SDK session
        # for this month, then the next month receives a new session.
        result = ingest.run_requests(
            requests,
            root=root,
            client=None,
            progress_log=progress_log,
            emit_progress=True,
        )
        batch_output = {
            "archive_month": month,
            "cohort_count": len(batch),
            "planned_requests": len(requests),
            "result": result,
        }
        batch_outputs.append(batch_output)
        _progress(progress_log, {
            "event": "monthly_batch_complete",
            "pid": os.getpid(),
            "archive_month": month,
            "cohort_count": len(batch),
            "planned_requests": len(requests),
            "status": result.get("status"),
            "status_counts": result.get("status_counts", {}),
            "rows": result.get("rows", 0),
            "contracts": result.get("contracts", 0),
        })
        if result.get("status") != "complete":
            overall_status = "partial"
            break
        _write_summary(summary_path, {
            "schema_version": 1,
            "mode": "run_remaining",
            "status": "in_progress",
            "pid": os.getpid(),
            "data_root": root,
            "manifest": manifest_path,
            "progress_log": progress_log,
            "completed_batches": len(batch_outputs),
            "total_batches": len(batches),
            "batches": batch_outputs,
            "capacity": _capacity_snapshot(root),
        })
    summary = {
        "schema_version": 1,
        "mode": "run_remaining",
        "status": overall_status,
        "pid": os.getpid(),
        "data_root": root,
        "manifest": manifest_path,
        "progress_log": progress_log,
        "summary_path": summary_path,
        "pilot_cohort_ids": sorted(pilot_ids),
        "remaining_cohort_count": len(remaining),
        "total_batches": len(batches),
        "completed_batches": len(batch_outputs),
        "planned_requests": sum(item["planned_requests"] for item in batch_outputs),
        "attempted_or_cached_requests": sum(int(item["result"].get("attempted_or_cached_requests", 0)) for item in batch_outputs),
        "batches": batch_outputs,
        "archive_cache_status_counts": manifest["cache_status_counts"],
        "capacity": _capacity_snapshot(root),
    }
    _write_summary(summary_path, summary)
    print(json.dumps(_json(summary), ensure_ascii=True, sort_keys=True), flush=True)
    return summary


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected_date_in_YYYY_MM_DD_format") from exc


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--plan", action="store_true", help="build the offline canonical cohort/request manifest")
    modes.add_argument("--pilot", action="store_true", help="run one SPY short and one QQQ long cohort")
    modes.add_argument("--run-remaining", action="store_true", help="run remaining cohorts in serial monthly batches")
    parser.add_argument("--root", type=Path, default=ingest.default_data_root())
    parser.add_argument("--pi-path", type=Path, default=None)
    parser.add_argument("--progress-log", type=Path)
    parser.add_argument("--summary-path", type=Path)
    parser.add_argument("--manifest-path", type=Path)
    args = parser.parse_args(argv)

    root = args.root
    progress_log = args.progress_log or root / "theta_pi_cohort_progress.jsonl"
    summary_path = args.summary_path or root / "theta_pi_cohort_summary.json"
    manifest_path = args.manifest_path or root / "theta_pi_cohort_manifest.json"

    if args.plan:
        cohorts = build_cohorts(root=root, pi_path=args.pi_path)
        payload = build_request_manifest(cohorts, root=root, manifest_path=manifest_path)
        print(json.dumps(_json({
            "mode": "plan",
            "status": "ready",
            "pid": os.getpid(),
            "manifest": manifest_path,
            "cohort_count": payload["cohort_count"],
            "cohort_counts_by_symbol": payload["cohort_counts_by_symbol"],
            "canonical_mark_count": payload["canonical_mark_count"],
            "planned_requests": payload["planned_requests"],
            "cache_status_counts": payload["cache_status_counts"],
            "pilot_candidates": payload["pilot_candidates"],
            "actual_selected_pilot": payload["actual_selected_pilot"],
            "capacity": payload["capacity"],
        }), ensure_ascii=False, sort_keys=True), flush=True)
        return 0

    if args.pilot:
        summary = run_pilot(
            root=root, progress_log=progress_log, summary_path=summary_path,
            manifest_path=manifest_path, pi_path=args.pi_path,
        )
    else:
        summary = run_remaining(
            root=root, progress_log=progress_log, summary_path=summary_path,
            manifest_path=manifest_path, pi_path=args.pi_path,
        )
    return 0 if summary.get("status") == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
