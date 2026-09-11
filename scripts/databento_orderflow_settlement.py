"""Settle recent Databento MNQ MBO days after the Topstep trading session.

The live feed is provisional.  This job is the small, idempotent hand-off to
historical data: choose only completed Topstep dates, locate the exact raw MBO
day, validate its manifest and the derived 390-bar RTH cache, repair a missing
or invalid request when explicitly run with ``--download``, then compare the
saved live checkpoint with the historical result.

The default mode is a report-only plan.  ``--download`` is required for API
quotes/transfers and the default ``--max-cost 0`` refuses any non-zero quote.
Raw and derived order-flow data stay on the canonical F: drive.  A replaced
raw file is renamed beside itself with a ``.replaced-*`` suffix; it is never
silently deleted.

Examples (``--end`` in the underlying request is exclusive)::

    python scripts/databento_orderflow_settlement.py
    python scripts/databento_orderflow_settlement.py --download
    python scripts/databento_orderflow_settlement.py --trade-date 2026-09-09

The Windows scheduled wrapper passes ``--download --max-cost 0``.  A normal
subscription-only response therefore transfers data without permitting an
unexpected usage-based charge; a non-zero quote becomes a visible failure.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.data import market_data, orderflow  # noqa: E402
from backend.data.candle_store import _is_us_futures_holiday  # noqa: E402
from backend.db.models import current_quarterly_contract_id  # noqa: E402
from backend.live.databento_orderflow import databento_raw_symbol  # noqa: E402
from backend.strategy.session_filter import rth_session_bounds  # noqa: E402
from backend.timebase import (  # noqa: E402
    NEW_YORK,
    UTC,
    as_utc,
    topstep_trade_date,
    utc_now,
)
from scripts.databento_orderflow_download import (  # noqa: E402
    DATASET,
    _acquire_download_lock,
    _base_symbol,
    _ensure_footprint_cache,
    _existing_inventory,
    _file_name,
    _folder_name,
    _instant,
    _load_api_key,
    _sha256,
    download_request,
    quote_request,
    request_payload,
)


SCHEMA = "mbo"
EXPECTED_RTH_BARS = 390
DEFAULT_LOOKBACK_TRADING_DAYS = 5
COMPARISON_FIELDS = (
    "buy", "sell", "delta", "trades", "adds", "cancels", "fills", "ofi",
)


def is_settlement_trading_date(value: date) -> bool:
    """Return whether a calendar date can carry a completed Topstep RTH day."""
    return value.weekday() < 5 and not _is_us_futures_holiday(value)


def previous_completed_topstep_date(now: datetime | None = None) -> date:
    """Return the most recent completed weekday Topstep trade date.

    ``topstep_trade_date`` owns the Chicago 17:00 boundary.  The settlement
    target is always one date behind that boundary, then skips weekends and
    the shared US-futures holiday set used by the candle store.
    """
    current = date.fromisoformat(topstep_trade_date(now or utc_now()))
    candidate = current - timedelta(days=1)
    while not is_settlement_trading_date(candidate):
        candidate -= timedelta(days=1)
    return candidate


def completed_topstep_dates(
    now: datetime | None = None,
    *,
    count: int = DEFAULT_LOOKBACK_TRADING_DAYS,
    anchor: date | None = None,
) -> list[date]:
    """Return recent completed Topstep dates, newest first."""
    requested = max(1, int(count))
    cursor = anchor or previous_completed_topstep_date(now)
    if not is_settlement_trading_date(cursor):
        while not is_settlement_trading_date(cursor):
            cursor -= timedelta(days=1)
    dates: list[date] = []
    while len(dates) < requested:
        if is_settlement_trading_date(cursor):
            dates.append(cursor)
        cursor -= timedelta(days=1)
    return dates


def mbo_request_range(session_date: date) -> tuple[date, date]:
    """Return the UTC calendar-day request containing one New York RTH."""
    start, end = rth_session_bounds(session_date)
    # The RTH window is within one UTC date for CME equity-index futures, but
    # derive the bounds from the timezone-aware session instead of hard-coding
    # a DST offset.  End is exclusive for Databento.
    return start.date(), end.date() + timedelta(days=1)


def expected_rth_epochs(session_date: date) -> list[int]:
    """Return the exact expected one-minute RTH epoch sequence."""
    start, end = rth_session_bounds(session_date)
    first = int(start.timestamp())
    last_exclusive = int(end.timestamp())
    return list(range(first, last_exclusive, 60))


def raw_symbol_candidates(session_date: date, override: str = "auto") -> list[str]:
    """Prefer an explicit symbol, otherwise the date-appropriate front month."""
    value = str(override or "auto").strip().upper()
    if value and value != "AUTO":
        return [value]
    configured = os.environ.get("DATABENTO_MNQ_SYMBOL", "").strip().upper()
    contract = current_quarterly_contract_id(
        "MNQ",
        now=datetime.combine(session_date, time(12, 0), tzinfo=NEW_YORK),
    )
    dynamic = databento_raw_symbol(contract)
    candidates: list[str] = []
    for symbol in (configured, dynamic, "MNQU6"):
        if symbol and symbol not in candidates:
            candidates.append(symbol)
    return candidates


def _metadata_query(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None, "metadata_unreadable"
    if not isinstance(payload, dict):
        return None, "metadata_not_object"
    query = payload.get("query") or payload.get("request")
    if not isinstance(query, dict):
        return None, "metadata_query_missing"
    return query, None


def _find_existing_raw(
    source_root: Path,
    session_date: date,
    candidates: Iterable[str],
) -> tuple[Path, str] | None:
    """Find an exact day regardless of whether it was saved under old symbol."""
    start, end = mbo_request_range(session_date)
    for symbol in candidates:
        inventory = _existing_inventory(source_root, symbol)
        path = inventory.get((SCHEMA, start, end))
        if path is not None:
            return path, symbol
        expected = source_root / _folder_name(symbol, SCHEMA, start, end) / _file_name(
            SCHEMA, start, end,
        )
        if expected.is_file():
            return expected, symbol

    # A contract rollover can leave a valid prior-symbol file.  Prefer that
    # file before asking the API for a duplicate under the new front month.
    if not source_root.is_dir():
        return None
    for metadata_path in sorted(source_root.rglob("metadata.json")):
        query, error = _metadata_query(metadata_path)
        if error or str(query.get("schema") or "") != SCHEMA:
            continue
        query_start = _instant(query.get("start"))
        query_end = _instant(query.get("end"))
        if query_start is None or query_end is None:
            continue
        if query_start.date() != start or query_end.date() != end:
            continue
        symbols = query.get("symbols") or []
        if isinstance(symbols, str):
            symbols = [symbols]
        raw_symbol = next(
            (str(value).upper() for value in symbols if str(value).upper().startswith("MNQ")),
            None,
        )
        if not raw_symbol:
            continue
        raw_paths = sorted(metadata_path.parent.glob("*.dbn.zst"))
        raw_paths.extend(sorted(metadata_path.parent.glob("*.dbn")))
        for path in raw_paths:
            if path.is_file() and path.stat().st_size > 0:
                return path, raw_symbol
    return None


def validate_raw_file(
    source: Path | None,
    raw_symbol: str,
    session_date: date,
    *,
    verify_hash: bool = True,
) -> dict[str, Any]:
    """Validate identity, size and manifest without replaying a huge DBN file."""
    start, end = mbo_request_range(session_date)
    result: dict[str, Any] = {
        "status": "missing",
        "complete": False,
        "path": str(source) if source else None,
        "symbol": raw_symbol,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "hash_checked": False,
        "errors": [],
    }
    if source is None or not source.is_file():
        result["errors"] = ["raw_file_missing"]
        return result
    try:
        size = int(source.stat().st_size)
    except OSError:
        result["errors"] = ["raw_file_unstatable"]
        return result
    result["bytes"] = size
    errors: list[str] = []
    if size <= 0:
        errors.append("raw_file_empty")

    metadata_path = source.parent / "metadata.json"
    query, metadata_error = _metadata_query(metadata_path)
    if metadata_error:
        errors.append(metadata_error)
    else:
        if str(query.get("dataset") or "") != DATASET:
            errors.append("metadata_dataset_mismatch")
        if str(query.get("schema") or "") != SCHEMA:
            errors.append("metadata_schema_mismatch")
        symbols = query.get("symbols") or []
        if isinstance(symbols, str):
            symbols = [symbols]
        if raw_symbol.upper() not in {str(value).upper() for value in symbols}:
            errors.append("metadata_symbol_mismatch")
        query_start = _instant(query.get("start"))
        query_end = _instant(query.get("end"))
        if query_start is None or query_start.date() != start:
            errors.append("metadata_start_mismatch")
        if query_end is None or query_end.date() != end:
            errors.append("metadata_end_mismatch")

    manifest_path = source.parent / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        manifest = None
        errors.append("manifest_unreadable")
    entry: Mapping[str, Any] | None = None
    if isinstance(manifest, dict):
        entries = manifest.get("files") or []
        if isinstance(entries, list):
            entry = next(
                (item for item in entries if isinstance(item, dict) and item.get("name") == source.name),
                None,
            )
        if entry is None:
            errors.append("manifest_file_missing")
        else:
            try:
                manifest_bytes = int(entry.get("bytes") or -1)
            except (TypeError, ValueError, OverflowError):
                manifest_bytes = -1
            if manifest_bytes != size:
                errors.append("manifest_size_mismatch")
            expected_hash = str(entry.get("sha256") or "").strip().lower()
            if len(expected_hash) != 64:
                errors.append("manifest_hash_missing")
            elif verify_hash:
                actual_hash = _sha256(source)
                result["hash_checked"] = True
                result["sha256"] = actual_hash
                if actual_hash.lower() != expected_hash:
                    errors.append("manifest_hash_mismatch")

    result["errors"] = errors
    result["status"] = "ok" if not errors else "invalid"
    result["complete"] = not errors
    return result


def validate_cache_payload(
    payload: Mapping[str, Any] | None,
    session_date: date,
) -> dict[str, Any]:
    """Validate the production schema and exact one-minute RTH coverage."""
    expected = expected_rth_epochs(session_date)
    result: dict[str, Any] = {
        "status": "missing",
        "complete": False,
        "expected_bars": len(expected),
        "bar_count": 0,
        "missing_bars": len(expected),
        "extra_bars": 0,
        "duplicate_bars": 0,
        "invalid_price_bars": 0,
        "missing_examples": [],
        "extra_examples": [],
    }
    if not isinstance(payload, Mapping):
        result["status"] = "invalid"
        result["errors"] = ["cache_unreadable"]
        return result
    meta = payload.get("meta") or {}
    bars = payload.get("bars")
    if not isinstance(meta, Mapping) or not isinstance(bars, list):
        result["status"] = "invalid"
        result["errors"] = ["cache_shape_invalid"]
        return result
    actual_epochs: list[int] = []
    invalid_prices = 0
    for row in bars:
        if not isinstance(row, Mapping):
            continue
        try:
            epoch = int(row.get("epoch"))
        except (TypeError, ValueError, OverflowError):
            continue
        actual_epochs.append(epoch)
        if any(row.get(key) is None for key in ("open", "high", "low", "close")):
            invalid_prices += 1
    expected_set = set(expected)
    actual_set = set(actual_epochs)
    missing = sorted(expected_set - actual_set)
    extra = sorted(actual_set - expected_set)
    duplicates = max(0, len(actual_epochs) - len(actual_set))
    errors: list[str] = []
    try:
        version = int(meta.get("schema_version") or 0)
    except (TypeError, ValueError, OverflowError):
        version = 0
    if version != orderflow.CACHE_SCHEMA_VERSION:
        errors.append("cache_schema_version")
    if str(meta.get("trade_date") or "") != session_date.isoformat():
        errors.append("cache_trade_date")
    if len(actual_epochs) != len(expected):
        errors.append("cache_bar_count")
    if missing:
        errors.append("cache_missing_minutes")
    if extra:
        errors.append("cache_extra_minutes")
    if duplicates:
        errors.append("cache_duplicate_minutes")
    if invalid_prices:
        errors.append("cache_invalid_price")
    result.update({
        "status": "ok" if not errors else "invalid",
        "complete": not errors,
        "bar_count": len(actual_epochs),
        "missing_bars": len(missing),
        "extra_bars": len(extra),
        "duplicate_bars": duplicates,
        "invalid_price_bars": invalid_prices,
        "missing_examples": missing[:10],
        "extra_examples": extra[:10],
        "schema_version": version,
        "source_records": meta.get("source_records"),
        "errors": errors,
    })
    if actual_epochs:
        result["first_epoch"] = min(actual_epochs)
        result["last_epoch"] = max(actual_epochs)
    return result


def inspect_settlement_day(
    session_date: date,
    *,
    root: str | Path | None = None,
    symbol_override: str = "auto",
    verify_hash: bool = True,
) -> dict[str, Any]:
    """Return a JSON-safe status and next action for one completed day."""
    primary = market_data.configured_market_data_root() if root is None else Path(root).resolve()
    source_root = market_data.orderflow_root(primary)
    candidates = raw_symbol_candidates(session_date, symbol_override)
    located = _find_existing_raw(source_root, session_date, candidates)
    planned_symbol = candidates[0] if candidates else "MNQU6"
    raw_path, actual_symbol = located if located else (None, planned_symbol)
    raw = validate_raw_file(
        raw_path, actual_symbol, session_date, verify_hash=verify_hash,
    )
    cache_path = orderflow.footprint_cache_path(
        session_date.isoformat(), _base_symbol(actual_symbol), root=primary,
    )
    cache_payload: dict[str, Any] | None = None
    cache_error: str | None = None
    if cache_path.is_file():
        try:
            cache_payload = orderflow.read_footprint_cache(cache_path)
        except (OSError, ValueError, EOFError):
            cache_error = "cache_unreadable"
    cache = validate_cache_payload(cache_payload, session_date)
    if cache_error:
        cache["status"] = "invalid"
        cache["complete"] = False
        cache.setdefault("errors", []).append(cache_error)
    if raw["status"] == "missing":
        action = "download_required"
    elif raw["status"] != "ok":
        action = "repair_raw_required"
    elif cache["status"] != "ok":
        action = "rebuild_cache_required"
    else:
        action = "complete"
    start, end = mbo_request_range(session_date)
    return {
        "trade_date": session_date.isoformat(),
        "rth_date": session_date.isoformat(),
        "raw_symbol": actual_symbol,
        "raw_symbol_candidates": candidates,
        "request": request_payload(SCHEMA, actual_symbol, start, end),
        "raw": raw,
        "cache": {
            **cache,
            "path": str(cache_path),
        },
        "action": action,
    }


def reconcile_payloads(
    live_payload: Mapping[str, Any] | None,
    history_payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Compare aggregate completed-minute fields without treating live as truth."""
    if not isinstance(live_payload, Mapping):
        return {"status": "no_live_checkpoint", "available": False}
    if not isinstance(history_payload, Mapping):
        return {"status": "history_unavailable", "available": True}
    live_rows = {
        int(row.get("epoch")): row
        for row in (live_payload.get("bars") or [])
        if isinstance(row, Mapping) and str(row.get("epoch", "")).lstrip("-").isdigit()
    }
    history_rows = {
        int(row.get("epoch")): row
        for row in (history_payload.get("bars") or [])
        if isinstance(row, Mapping) and str(row.get("epoch", "")).lstrip("-").isdigit()
    }
    overlap = sorted(set(live_rows) & set(history_rows))
    mismatched = 0
    max_abs_delta_diff = 0
    total_abs_delta_diff = 0
    for epoch in overlap:
        left = live_rows[epoch]
        right = history_rows[epoch]
        if any(left.get(key) != right.get(key) for key in COMPARISON_FIELDS):
            mismatched += 1
        try:
            delta_diff = abs(int(left.get("delta") or 0) - int(right.get("delta") or 0))
        except (TypeError, ValueError, OverflowError):
            delta_diff = 0
        max_abs_delta_diff = max(max_abs_delta_diff, delta_diff)
        total_abs_delta_diff += delta_diff
    status = "match" if overlap and mismatched == 0 else (
        "mismatch" if overlap else "no_overlap"
    )
    return {
        "status": status,
        "available": True,
        "live_bars": len(live_rows),
        "history_bars": len(history_rows),
        "overlap_bars": len(overlap),
        "live_only_bars": len(set(live_rows) - set(history_rows)),
        "history_only_bars": len(set(history_rows) - set(live_rows)),
        "mismatched_bars": mismatched,
        "max_abs_delta_diff": max_abs_delta_diff,
        "total_abs_delta_diff": total_abs_delta_diff,
    }


def _live_checkpoint(
    session_date: date,
    *,
    root: str | Path | None = None,
) -> tuple[Path, dict[str, Any] | None]:
    primary = market_data.configured_market_data_root() if root is None else Path(root).resolve()
    path = market_data.runtime_path(
        "state", f"databento_live_footprint_mnq_{session_date.isoformat()}.json.gz",
        root=primary,
    )
    if not path.is_file():
        return path, None
    try:
        return path, orderflow.read_footprint_cache(path)
    except (OSError, ValueError, EOFError):
        return path, None


def _report_copy(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _report_copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_report_copy(item) for item in value]
    return value


def _write_report(report: dict[str, Any], primary: Path, now: datetime) -> Path:
    output_root = market_data.derived_path("orderflow", "reconciliation", root=primary)
    output_root.mkdir(parents=True, exist_ok=True)
    stem = f"settlement_{now.strftime('%Y%m%dT%H%M%SZ')}"
    output = output_root / f"{stem}.json"
    latest = output_root / "settlement_latest.json"
    payload = _report_copy(report)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output)
    latest_tmp = latest.with_name(latest.name + ".tmp")
    latest_tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    latest_tmp.replace(latest)
    return latest


def _quote_pending(
    client: Any,
    plans: list[dict[str, Any]],
    *,
    repair_incomplete: bool,
) -> tuple[float, int]:
    total_cost = 0.0
    total_records = 0
    for plan in plans:
        action = str(plan.get("action"))
        if action == "repair_raw_required" and not repair_incomplete:
            plan["action"] = "manual_repair_required"
            continue
        if action not in {"download_required", "repair_raw_required"}:
            continue
        count, cost = quote_request(client, dict(plan["request"]))
        plan["quote"] = {
            "record_count": count,
            "quoted_cost_usd": cost,
        }
        if count <= 0:
            plan["action"] = "empty_source"
            continue
        plan["request"].update({
            "record_count": count,
            "quoted_cost_usd": cost,
        })
        total_cost += cost
        total_records += count
    return total_cost, total_records


def _execute_plan(
    plan: dict[str, Any],
    *,
    client: Any | None,
    primary: Path,
    repair_incomplete: bool,
) -> None:
    action = str(plan.get("action"))
    if action == "repair_raw_required" and not repair_incomplete:
        plan["action"] = "manual_repair_required"
        return
    if action not in {"download_required", "repair_raw_required"}:
        if action == "rebuild_cache_required" and plan["raw"].get("status") == "ok":
            source = Path(str(plan["raw"]["path"]))
            _ensure_footprint_cache(
                source,
                date.fromisoformat(str(plan["trade_date"])),
                "MNQ",
                force=True,
                root=primary,
            )
            plan["action"] = "cache_rebuilt"
        return
    quote = plan.get("quote") or {}
    if int(quote.get("record_count") or 0) <= 0:
        return
    source_root = market_data.orderflow_root(primary)
    target, digest = download_request(
        client,
        dict(plan["request"]),
        source_root,
        replace_existing=True,
    )
    plan["download"] = {
        "path": str(target),
        "bytes": target.stat().st_size,
        "sha256": digest,
        "primary_only": True,
    }
    _ensure_footprint_cache(
        target,
        date.fromisoformat(str(plan["trade_date"])),
        "MNQ",
        force=True,
        root=primary,
    )
    plan["action"] = "downloaded_and_rebuilt"


def run_settlement(
    *,
    now: datetime | None = None,
    dates: Iterable[date] | None = None,
    count: int = DEFAULT_LOOKBACK_TRADING_DAYS,
    root: str | Path | None = None,
    symbol_override: str = "auto",
    download: bool = False,
    max_cost: float = 0.0,
    repair_incomplete: bool = True,
    verify_hash: bool = True,
) -> dict[str, Any]:
    """Plan and optionally execute settlement; returns the persisted report."""
    # ``as_utc`` makes the legacy-naive input contract explicit: a naive
    # datetime is a UTC instant, never the machine's local wall clock.
    run_now = as_utc(now or utc_now())
    primary = market_data.ensure_layout() if root is None else Path(root).resolve()
    selected_dates = list(dates) if dates is not None else completed_topstep_dates(
        run_now, count=count,
    )
    plans = [
        inspect_settlement_day(
            day,
            root=primary,
            symbol_override=symbol_override,
            verify_hash=verify_hash,
        )
        for day in selected_dates
    ]
    report: dict[str, Any] = {
        "version": 1,
        "generated_at": run_now.astimezone(UTC).isoformat(),
        "mode": "download" if download else "plan_only",
        "dataset": DATASET,
        "schema": SCHEMA,
        "primary": str(primary),
        "orderflow_backup": "disabled_primary_F_only",
        "topstep_boundary": "America/Chicago 17:00",
        "rth_boundary": "America/New_York 09:30-16:00",
        "lookback_trading_days": len(plans),
        "plans": plans,
    }

    pending = [
        plan for plan in plans
        if plan["action"] in {"download_required", "repair_raw_required"}
    ]
    cost_allowed = True
    client: Any | None = None
    if pending and download:
        _acquire_download_lock(
            market_data.runtime_path("databento_orderflow_download.lock", root=primary),
        )
        import databento as db

        client = db.Historical(_load_api_key())
        total_cost, total_records = _quote_pending(
            client, plans, repair_incomplete=repair_incomplete,
        )
        report["quote"] = {
            "pending_requests": len(pending),
            "record_count": total_records,
            "quoted_cost_usd": total_cost,
            "max_cost_usd": float(max_cost),
        }
        if total_cost > float(max_cost) + 1e-9:
            report["status"] = "blocked_cost"
            cost_allowed = False
    elif not download:
        report["quote"] = {
            "pending_requests": len(pending),
            "record_count": None,
            "quoted_cost_usd": None,
            "note": "plan only; no Databento API call",
        }
    elif download:
        report["quote"] = {
            "pending_requests": 0,
            "record_count": 0,
            "quoted_cost_usd": 0.0,
            "max_cost_usd": float(max_cost),
        }

    # A cache rebuild is local and remains safe even if a new raw transfer is
    # blocked by the cost guard.  Raw transfers require the historical client.
    if download and cost_allowed:
        failures: list[str] = []
        for plan in plans:
            if plan.get("action") not in {
                "download_required", "repair_raw_required", "rebuild_cache_required",
            }:
                continue
            if plan.get("action") in {"download_required", "repair_raw_required"} and client is None:
                continue
            try:
                _execute_plan(
                    plan,
                    client=client,
                    primary=primary,
                    repair_incomplete=repair_incomplete,
                )
            except Exception as exc:  # keep other dates auditable
                plan["action"] = "failed"
                plan["error"] = f"{type(exc).__name__}: {exc}"
                failures.append(str(plan["trade_date"]))
        report["execution_failures"] = failures

    # Re-inspect after local rebuild/download and compare history against the
    # live checkpoint.  History remains the settled source of truth.
    final_plans: list[dict[str, Any]] = []
    for plan in plans:
        day = date.fromisoformat(str(plan["trade_date"]))
        final = inspect_settlement_day(
            day,
            root=primary,
            symbol_override=symbol_override,
            verify_hash=verify_hash,
        )
        if plan.get("action") == "failed":
            final["action"] = "failed"
            final["error"] = plan.get("error")
        live_path, live_payload = _live_checkpoint(day, root=primary)
        history_payload = None
        history_path = final["cache"].get("path")
        if final["cache"].get("status") == "ok" and history_path:
            try:
                history_payload = orderflow.read_footprint_cache(history_path)
            except (OSError, ValueError, EOFError):
                history_payload = None
        final["live_checkpoint"] = {
            "path": str(live_path),
            "available": live_payload is not None,
        }
        final["reconciliation"] = reconcile_payloads(live_payload, history_payload)
        final_plans.append(final)
    report["plans"] = final_plans

    unresolved = [
        plan for plan in final_plans
        if plan.get("action") not in {"complete", "cache_rebuilt", "downloaded_and_rebuilt"}
        or plan.get("cache", {}).get("status") != "ok"
    ]
    report["status"] = report.get("status") or (
        "ok" if not unresolved else "needs_attention"
    )
    report["summary"] = {
        "dates": len(final_plans),
        "complete": len(final_plans) - len(unresolved),
        "needs_attention": len(unresolved),
        "downloaded": sum(
            1 for plan in final_plans if plan.get("action") == "downloaded_and_rebuilt"
        ),
        "cache_rebuilt": sum(
            1 for plan in final_plans if plan.get("action") == "cache_rebuilt"
        ),
    }
    report["report_path"] = str(_write_report(report, primary, run_now))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--trade-date",
        help="settlement anchor YYYY-MM-DD; defaults to previous completed Topstep date",
    )
    parser.add_argument(
        "--lookback-trading-days",
        type=int,
        default=DEFAULT_LOOKBACK_TRADING_DAYS,
        help="completed trading days to inspect (default: 5)",
    )
    parser.add_argument(
        "--symbol",
        default="auto",
        help="Databento raw symbol, or auto for date-appropriate front month",
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="quote and transfer missing/invalid raw MBO files",
    )
    parser.add_argument(
        "--max-cost",
        type=float,
        default=0.0,
        help="maximum total USD quote accepted (default: 0)",
    )
    parser.add_argument(
        "--no-repair-incomplete",
        action="store_true",
        help="report invalid raw files without replacing them",
    )
    parser.add_argument(
        "--skip-hash",
        action="store_true",
        help="skip SHA-256 reads for existing files (size/manifest still checked)",
    )
    args = parser.parse_args(argv)
    anchor = date.fromisoformat(args.trade_date) if args.trade_date else None
    selected_dates = (
        [anchor]
        if anchor is not None
        else completed_topstep_dates(count=args.lookback_trading_days)
    )
    result = run_settlement(
        dates=selected_dates,
        symbol_override=args.symbol,
        download=args.download,
        max_cost=args.max_cost,
        repair_incomplete=not args.no_repair_incomplete,
        verify_hash=not args.skip_hash,
    )
    print(json.dumps({
        "status": result.get("status"),
        "summary": result.get("summary"),
        "quote": result.get("quote"),
        "report_path": result.get("report_path"),
    }, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
