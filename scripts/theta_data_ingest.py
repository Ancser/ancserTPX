"""Bounded Theta Data SDK pilot for the PI next-session option replay.

The CLI is deliberately opt-in: ``--plan`` is read-only and ``--pilot`` runs
one fixed six-request case. Credentials are loaded by ThetaClient from the
project .env; they never enter command-line arguments or log output.
"""
from __future__ import annotations

import argparse
import contextlib
import gzip
import hashlib
import io
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.data import market_data  # noqa: E402

PILOT_EVENT_DATE = date(2026, 3, 30)
PILOT_EXIT_DATE = date(2026, 3, 31)
PILOT_EXPIRATION = date(2026, 3, 31)
PILOT_OI_EFFECTIVE_SESSION = date(2026, 3, 27)
PILOT_SPOT_REFERENCE = 557.87
MAX_TREE_BYTES = 300 * 1024**3
MIN_FREE_BYTES = 80 * 1024**3
MAX_ATTEMPTS = 3
REQUEST_GAP_SECONDS = 1.25
BACKOFF_SECONDS = (0.5, 1.0)


def default_data_root() -> Path:
    return market_data.configured_market_data_root() / "source" / "options" / "thetadata"


def _json_value(value: Any) -> Any:
    if isinstance(value, (date,)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _request(
    *,
    key: str,
    method: str,
    params: dict[str, Any],
    request_date: date,
    expiration: date | str,
    scope: str,
    symbol: str = "QQQ",
    event_date: date | None = None,
    effective_session: date | None = None,
    event_context: dict[str, Any] | None = None,
    relative_path: Path | None = None,
    coverage_start: date | None = None,
    coverage_end: date | None = None,
    archive_month: str | None = None,
    oi_effective_session_status: str | None = None,
) -> dict[str, Any]:
    expiry_slug = expiration.isoformat() if isinstance(expiration, date) else expiration
    relative = relative_path or (Path("raw") / symbol / key / f"date={request_date.isoformat()}" / f"expiration={expiry_slug}.csv.gz")
    return {
        "key": key,
        "method": method,
        "params": params,
        "request_date": request_date,
        "expiration": expiration,
        "scope": scope,
        "symbol": symbol,
        "event_date": event_date,
        "effective_session": effective_session,
        "event_context": event_context,
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
        "archive_month": archive_month,
        "oi_effective_session_status": oi_effective_session_status,
        "relative_path": relative,
    }


def pilot_requests() -> list[dict[str, Any]]:
    """Return the exact bounded QQQ 2026-03-30 -> 2026-03-31 pilot."""
    requests = [
        _request(
            key="eod_dte_0_60",
            method="option_history_eod",
            params={
                "start_date": PILOT_EVENT_DATE,
                "end_date": PILOT_EVENT_DATE,
                "symbol": "QQQ",
                "expiration": "*",
                "max_dte": 60,
            },
            request_date=PILOT_EVENT_DATE,
            expiration="ALL",
            scope="partial_chain_calendar_dte_0_60",
            event_date=PILOT_EVENT_DATE,
        ),
        _request(
            key="open_interest_dte_0_60",
            method="option_history_open_interest",
            params={
                "symbol": "QQQ",
                "expiration": "*",
                "date": PILOT_EVENT_DATE,
                "max_dte": 60,
            },
            request_date=PILOT_EVENT_DATE,
            expiration="ALL",
            scope="partial_chain_calendar_dte_0_60",
            event_date=PILOT_EVENT_DATE,
            effective_session=PILOT_OI_EFFECTIVE_SESSION,
        ),
    ]

    requests.extend(
        selected_expiry_requests(
            symbol="QQQ",
            event_date=PILOT_EVENT_DATE,
            exit_date=PILOT_EXIT_DATE,
            expiration=PILOT_EXPIRATION,
            event_context={
                "event_date": PILOT_EVENT_DATE,
                "event_time_et": "15:01:00",
                "earliest_entry_time_et": "15:02:00",
                "reference_spot_from_existing_study": PILOT_SPOT_REFERENCE,
            },
        )
    )
    return requests


def selected_expiry_requests(
    *,
    symbol: str,
    event_date: date,
    exit_date: date,
    expiration: date,
    event_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build the four all-strike 1m quote/first-order requests for one cohort.

    The parameter surface and raw paths intentionally match the selected-
    expiry portion of :func:`episode_requests`, so an existing response is
    resumed by the same request ID and immutable file path.
    """
    symbol = symbol.upper()
    if not symbol.isalnum() or not 1 <= len(symbol) <= 8:
        raise ValueError("symbol_must_be_1_to_8_alphanumeric_characters")
    if exit_date <= event_date or expiration < exit_date:
        raise ValueError("exit_date_must_follow_event_and_not_follow_expiration")
    common = {
        "symbol": symbol,
        "expiration": expiration,
        "interval": "1m",
        "strike": "*",
        "right": "both",
        "start_time": "09:30:00",
        "end_time": "16:00:00",
    }
    requests: list[dict[str, Any]] = []
    for request_day in (event_date, exit_date):
        for key, method, extra in (
            ("quote_1m", "option_history_quote", {}),
            (
                "greeks_first_order_1m",
                "option_history_greeks_first_order",
                {"rate_type": "sofr", "version": "latest"},
            ),
        ):
            requests.append(
                _request(
                    key=key,
                    method=method,
                    params={**common, "date": request_day, **extra},
                    request_date=request_day,
                    expiration=expiration,
                    scope="selected_expiry_all_strikes_1m",
                    symbol=symbol,
                    event_date=event_date,
                    event_context=event_context,
                )
            )
    return requests


def episode_requests(
    *,
    symbol: str,
    event_date: date,
    exit_date: date,
    expiration: date,
    oi_effective_session: date,
) -> list[dict[str, Any]]:
    """Build one explicitly specified event/next-session-expiry coverage set."""
    symbol = symbol.upper()
    if not symbol.isalnum() or not 1 <= len(symbol) <= 8:
        raise ValueError("symbol_must_be_1_to_8_alphanumeric_characters")
    if exit_date <= event_date or expiration < exit_date:
        raise ValueError("exit_date_must_follow_event_and_not_follow_expiration")
    if oi_effective_session >= event_date:
        raise ValueError("oi_effective_session_must_precede_publication_date")

    snapshot_params = {
        "symbol": symbol,
        "expiration": "*",
        "max_dte": 60,
    }
    requests = [
        _request(
            key="eod_dte_0_60",
            method="option_history_eod",
            params={**snapshot_params, "start_date": event_date, "end_date": event_date},
            request_date=event_date,
            expiration="ALL",
            scope="partial_chain_calendar_dte_0_60",
            symbol=symbol,
            event_date=event_date,
        ),
        _request(
            key="open_interest_dte_0_60",
            method="option_history_open_interest",
            params={**snapshot_params, "date": event_date},
            request_date=event_date,
            expiration="ALL",
            scope="partial_chain_calendar_dte_0_60",
            symbol=symbol,
            event_date=event_date,
            effective_session=oi_effective_session,
        ),
    ]
    requests.extend(
        selected_expiry_requests(
            symbol=symbol,
            event_date=event_date,
            exit_date=exit_date,
            expiration=expiration,
        )
    )
    return requests


def daily_snapshot_requests(
    *, symbol: str, snapshot_date: date, oi_effective_session: date, max_dte: int = 60
) -> list[dict[str, Any]]:
    """Build one configurable EOD/OI snapshot date for a bounded archive batch."""
    symbol = symbol.upper()
    if not symbol.isalnum() or not 1 <= len(symbol) <= 8:
        raise ValueError("symbol_must_be_1_to_8_alphanumeric_characters")
    if oi_effective_session >= snapshot_date:
        raise ValueError("oi_effective_session_must_precede_publication_date")
    if not 0 <= max_dte <= 366:
        raise ValueError("max_dte_must_be_between_0_and_366")
    common = {"symbol": symbol, "expiration": "*", "max_dte": max_dte}
    return [
        _request(
            key="eod_dte_0_60" if max_dte == 60 else f"eod_dte_0_{max_dte}",
            method="option_history_eod",
            params={**common, "start_date": snapshot_date, "end_date": snapshot_date},
            request_date=snapshot_date,
            expiration="ALL",
            scope=f"partial_chain_calendar_dte_0_{max_dte}",
            symbol=symbol,
        ),
        _request(
            key="open_interest_dte_0_60" if max_dte == 60 else f"open_interest_dte_0_{max_dte}",
            method="option_history_open_interest",
            params={**common, "date": snapshot_date},
            request_date=snapshot_date,
            expiration="ALL",
            scope=f"partial_chain_calendar_dte_0_{max_dte}",
            symbol=symbol,
            effective_session=oi_effective_session,
        ),
    ]


def _normalize_symbols(symbols: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in symbols:
        symbol = str(value).strip().upper()
        if not symbol.isalnum() or not 1 <= len(symbol) <= 8:
            raise ValueError("symbol_must_be_1_to_8_alphanumeric_characters")
        if symbol not in normalized:
            normalized.append(symbol)
    if not normalized:
        raise ValueError("at_least_one_symbol_is_required")
    return tuple(normalized)


def _month_end(month_start: date) -> date:
    if month_start.month == 12:
        next_start = date(month_start.year + 1, 1, 1)
    else:
        next_start = date(month_start.year, month_start.month + 1, 1)
    return date.fromordinal(next_start.toordinal() - 1)


def monthly_archive_requests(
    *,
    start_date: date,
    end_date: date,
    symbols: Iterable[str] = ("QQQ", "SPY"),
    max_dte: int = 60,
) -> list[dict[str, Any]]:
    """Build month-keyed EOD/OI requests without assigning row-level OI dates."""
    if start_date > end_date:
        raise ValueError("start_date_must_not_follow_end_date")
    if not 0 <= max_dte <= 366:
        raise ValueError("max_dte_must_be_between_0_and_366")
    normalized_symbols = _normalize_symbols(symbols)
    eod_key = "monthly_eod_dte_0_60" if max_dte == 60 else f"monthly_eod_dte_0_{max_dte}"
    oi_key = "monthly_open_interest_dte_0_60" if max_dte == 60 else f"monthly_open_interest_dte_0_{max_dte}"
    requests: list[dict[str, Any]] = []
    cursor = date(start_date.year, start_date.month, 1)
    while cursor <= end_date:
        month_end = _month_end(cursor)
        period_start = max(start_date, cursor)
        period_end = min(end_date, month_end)
        month_slug = cursor.strftime("%Y-%m")
        for symbol in normalized_symbols:
            common = {
                "symbol": symbol,
                "expiration": "*",
                "max_dte": max_dte,
                "start_date": period_start,
                "end_date": period_end,
            }
            requests.append(
                _request(
                    key=eod_key,
                    method="option_history_eod",
                    params=common,
                    request_date=period_start,
                    expiration="ALL",
                    scope=f"monthly_partial_chain_calendar_dte_0_{max_dte}",
                    symbol=symbol,
                    relative_path=Path("raw") / symbol / eod_key / f"month={month_slug}.csv.gz",
                    coverage_start=period_start,
                    coverage_end=period_end,
                    archive_month=month_slug,
                )
            )
            requests.append(
                _request(
                    key=oi_key,
                    method="option_history_open_interest",
                    params=common,
                    request_date=period_start,
                    expiration="ALL",
                    scope=f"monthly_partial_chain_calendar_dte_0_{max_dte}",
                    symbol=symbol,
                    relative_path=Path("raw") / symbol / oi_key / f"month={month_slug}.csv.gz",
                    coverage_start=period_start,
                    coverage_end=period_end,
                    archive_month=month_slug,
                    oi_effective_session_status="unknown_until_calendar_join",
                )
            )
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    return requests


def monthly_pilot_requests() -> list[dict[str, Any]]:
    return monthly_archive_requests(
        start_date=date(2021, 1, 1), end_date=date(2021, 1, 31),
        symbols=("QQQ", "SPY"), max_dte=60,
    )


def request_id(request: dict[str, Any]) -> str:
    identity = {"method": request["method"], "params": _json_value(request["params"])}
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _manifest_path(root: Path) -> Path:
    return root / "coverage_manifest.jsonl"


def read_latest_manifest(root: Path) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    path = _manifest_path(root)
    if not path.exists():
        return latest
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if record.get("request_id"):
                latest[str(record["request_id"])] = record
    return latest


def _append_manifest(root: Path, record: dict[str, Any]) -> None:
    path = _manifest_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(_json_value(record), sort_keys=True, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8", newline="") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def _tree_bytes(root: Path) -> int:
    if not root.exists():
        return 0
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _nearest_existing(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def capacity_error(
    *,
    current_tree_bytes: int,
    free_bytes: int,
    incoming_bytes: int,
    max_tree_bytes: int = MAX_TREE_BYTES,
    min_free_bytes: int = MIN_FREE_BYTES,
) -> str | None:
    if current_tree_bytes + incoming_bytes > max_tree_bytes:
        return "tree_budget_exceeded"
    if free_bytes - incoming_bytes < min_free_bytes:
        return "free_space_reserve_would_be_breached"
    return None


def check_capacity(root: Path, incoming_bytes: int = 0) -> str | None:
    usage = shutil.disk_usage(_nearest_existing(root))
    return capacity_error(
        current_tree_bytes=_tree_bytes(root),
        free_bytes=usage.free,
        incoming_bytes=incoming_bytes,
    )


def _frame_csv_bytes(frame: Any) -> bytes:
    if hasattr(frame, "write_csv"):
        payload = frame.write_csv()
    elif hasattr(frame, "to_csv"):
        payload = frame.to_csv(index=False)
    else:
        raise TypeError("sdk_result_does_not_expose_dataframe_csv")
    if isinstance(payload, bytes):
        return payload
    return str(payload).encode("utf-8")


def _frame_height(frame: Any) -> int:
    height = getattr(frame, "height", None)
    if height is not None:
        return int(height)
    shape = getattr(frame, "shape", None)
    if shape is not None:
        return int(shape[0])
    try:
        return len(frame)
    except TypeError:
        return 0


def _contract_count(frame: Any) -> int:
    columns = {str(name).lower(): name for name in getattr(frame, "columns", ())}
    key_names = [columns[name] for name in ("symbol", "expiration", "strike", "right") if name in columns]
    if not {"expiration", "strike", "right"}.issubset(columns):
        return 0
    try:
        return int(frame.select(key_names).unique().height)
    except (AttributeError, TypeError, ValueError):
        return 0


def _timestamp_bounds(frame: Any) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    columns = {str(name).lower(): name for name in getattr(frame, "columns", ())}
    for field in ("timestamp", "underlying_timestamp"):
        column = columns.get(field)
        if column is None:
            continue
        try:
            series = frame.get_column(column)
            minimum, maximum = series.min(), series.max()
            result[f"{field}_min_raw"] = None if minimum is None else str(minimum)
            result[f"{field}_max_raw"] = None if maximum is None else str(maximum)
        except (AttributeError, TypeError, ValueError):
            continue
    return result


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cached_result(request: dict[str, Any], root: Path, latest: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    rid = request_id(request)
    previous = latest.get(rid)
    path = root / request["relative_path"]
    if previous and previous.get("status") in {"downloaded", "missing"} and path.is_file():
        try:
            actual = _file_sha256(path)
        except OSError:
            return None
        if actual == previous.get("sha256_gzip"):
            return {
                "request_id": rid,
                "key": request["key"],
                "status": "cached",
                "rows": previous.get("rows", 0),
                "contracts": previous.get("contracts", 0),
                "compressed_bytes": previous.get("compressed_bytes", 0),
                "path": str(request["relative_path"]),
            }
    if path.exists():
        return {
            "request_id": rid,
            "key": request["key"],
            "status": "orphaned_existing_file",
            "rows": 0,
            "contracts": 0,
            "compressed_bytes": path.stat().st_size if path.is_file() else 0,
            "path": str(request["relative_path"]),
        }
    return None


@contextlib.contextmanager
def _exclusive_run_lock(root: Path):
    lock_path = root / ".theta_data_ingest.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    stream = lock_path.open("a+b")
    try:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        stream.close()
        raise RuntimeError("another_theta_data_ingest_run_holds_the_lock") from exc
    try:
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            stream.close()


def _write_immutable(path: Path, compressed: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError("raw_response_path_already_exists")
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".theta-", suffix=".part", delete=False) as stream:
            temp_path = Path(stream.name)
            stream.write(compressed)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            raise FileExistsError("raw_response_path_already_exists")
        os.rename(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


def _status_code(exc: Exception) -> str | None:
    candidates: list[Any] = []
    for name in ("status_code", "status", "code"):
        value = getattr(exc, name, None)
        if callable(value):
            try:
                value = value()
            except Exception:
                continue
        if value is not None:
            candidates.append(value)
    for value in candidates:
        named = getattr(value, "name", None)
        if named:
            return str(named).upper()
        if isinstance(value, int):
            return str(value)
        if isinstance(value, str):
            safe = value.upper().split(".")[-1]
            if safe.replace("_", "").isalnum():
                return safe
    return None


def classify_exception(exc: Exception) -> tuple[str, str | None]:
    code = _status_code(exc)
    if type(exc).__name__ == "NoDataFoundError" and type(exc).__module__.startswith("thetadata"):
        return "missing", "NO_DATA_FOUND"
    if type(exc).__name__ == "AuthenticationError" and type(exc).__module__.startswith("thetadata"):
        return "forbidden", "AUTHENTICATION_ERROR"
    if code in {"UNAUTHENTICATED", "PERMISSION_DENIED", "401", "403"}:
        return "forbidden", code
    if code in {"NOT_FOUND", "404"}:
        return "missing", code
    return "error", code


def _retryable(exc: Exception, code: str | None) -> bool:
    if code in {"UNAVAILABLE", "DEADLINE_EXCEEDED", "RESOURCE_EXHAUSTED", "429", "500", "502", "503", "504"}:
        return True
    return isinstance(exc, (TimeoutError, ConnectionError))


def call_with_retries(
    call: Callable[[], Any],
    *,
    sleep: Callable[[float], None] = time.sleep,
    max_attempts: int = MAX_ATTEMPTS,
) -> tuple[Any | None, str, str | None, int]:
    for attempt in range(1, max_attempts + 1):
        try:
            return call(), "ok", None, attempt
        except Exception as exc:
            status, code = classify_exception(exc)
            if status == "forbidden" or status == "missing":
                return None, status, code, attempt
            if attempt >= max_attempts or not _retryable(exc, code):
                return None, "error", code, attempt
            sleep(BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)])
    return None, "error", None, max_attempts


def _record_base(request: dict[str, Any]) -> dict[str, Any]:
    record = {
        "schema_version": 1,
        "request_id": request_id(request),
        "dataset": request["key"],
        "sdk_method": request["method"],
        "symbol": request["symbol"],
        "request_date": request["request_date"],
        "expiration": request["expiration"],
        "scope": request["scope"],
        "request_params_redacted": _json_value(request["params"]),
        "response_path": str(request["relative_path"]),
        "completed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if request.get("event_date") is not None:
        record["event_date"] = request["event_date"]
    if request.get("coverage_start") is not None:
        record["coverage_start_date"] = request["coverage_start"]
    if request.get("coverage_end") is not None:
        record["coverage_end_date"] = request["coverage_end"]
    if request.get("archive_month") is not None:
        record["archive_month"] = request["archive_month"]
    if request.get("oi_effective_session_status") is not None:
        record["oi_effective_session_status"] = request["oi_effective_session_status"]
    return record


def _oi_provenance(request: dict[str, Any], timestamp_bounds: dict[str, Any]) -> dict[str, Any] | None:
    if request["method"] != "option_history_open_interest":
        return None
    unknown_status = request.get("oi_effective_session_status")
    if unknown_status == "unknown_until_calendar_join":
        effective_session: str | date | None = unknown_status
        effective_basis = "each row maps to the prior trading session; join publication date/timestamp to the exchange calendar before assigning effective_session"
    else:
        effective_session = request["effective_session"]
        effective_basis = "previous XNYS session from request date, per Theta OI prior-session semantics"
    return {
        "request_session": request["request_date"],
        "effective_session": effective_session,
        "effective_session_basis": effective_basis,
        "expected_publication_time_et": "approximately 06:30 America/New_York per Theta documentation",
        "vendor_timestamps_preserved_raw": timestamp_bounds,
        "timestamp_shift_applied": False,
        "publication_date_mapping": "request date and vendor timestamp are both retained; no second shift is applied to the vendor timestamp",
    }


def _run_request(
    client: Any,
    request: dict[str, Any],
    root: Path,
    latest: dict[str, dict[str, Any]],
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    cached = _cached_result(request, root, latest)
    if cached is not None:
        return cached

    call = lambda: getattr(client, request["method"])(**request["params"])
    frame, call_status, code, attempts = call_with_retries(call, sleep=sleep)
    base = _record_base(request)
    if call_status != "ok":
        record = {
            **base,
            "status": call_status,
            "error_code": code,
            "error_type": None,
            "attempts": attempts,
            "rows": 0,
            "contracts": 0,
            "raw_bytes": 0,
            "compressed_bytes": 0,
        }
        _append_manifest(root, record)
        latest[record["request_id"]] = record
        return {
            "request_id": record["request_id"],
            "key": request["key"],
            "status": call_status,
            "error_code": code,
            "rows": 0,
            "contracts": 0,
            "compressed_bytes": 0,
        }

    try:
        raw = _frame_csv_bytes(frame)
        rows = _frame_height(frame)
        contracts = _contract_count(frame)
        timestamp_bounds = _timestamp_bounds(frame)
    except Exception as exc:
        record = {
            **base,
            "status": "error",
            "error_code": None,
            "error_type": type(exc).__name__,
            "attempts": attempts,
            "rows": 0,
            "contracts": 0,
            "raw_bytes": 0,
            "compressed_bytes": 0,
        }
        _append_manifest(root, record)
        latest[record["request_id"]] = record
        return {
            "request_id": record["request_id"],
            "key": request["key"],
            "status": "error",
            "error_type": type(exc).__name__,
            "rows": 0,
            "contracts": 0,
            "compressed_bytes": 0,
        }

    compressed = gzip.compress(raw, compresslevel=6, mtime=0)
    capacity = check_capacity(root, len(compressed))
    if capacity:
        record = {
            **base,
            "status": "capacity_blocked",
            "capacity_reason": capacity,
            "attempts": attempts,
            "rows": rows,
            "contracts": contracts,
            "raw_bytes": len(raw),
            "compressed_bytes": len(compressed),
            "sha256_raw": hashlib.sha256(raw).hexdigest(),
            **timestamp_bounds,
        }
        oi = _oi_provenance(request, timestamp_bounds)
        if oi:
            record["oi_provenance"] = oi
        _append_manifest(root, record)
        latest[record["request_id"]] = record
        return {
            "request_id": record["request_id"],
            "key": request["key"],
            "status": "capacity_blocked",
            "rows": rows,
            "contracts": contracts,
            "compressed_bytes": len(compressed),
        }

    path = root / request["relative_path"]
    try:
        _write_immutable(path, compressed)
    except FileExistsError:
        return {
            "request_id": request_id(request),
            "key": request["key"],
            "status": "orphaned_existing_file",
            "rows": rows,
            "contracts": contracts,
            "compressed_bytes": 0,
        }

    status = "downloaded" if rows else "missing"
    record = {
        **base,
        "status": status,
        "attempts": attempts,
        "rows": rows,
        "contracts": contracts,
        "raw_bytes": len(raw),
        "compressed_bytes": len(compressed),
        "sha256_raw": hashlib.sha256(raw).hexdigest(),
        "sha256_gzip": hashlib.sha256(compressed).hexdigest(),
        "response_encoding": "unmodified SDK dataframe columns/values serialized as CSV, gzip-compressed with mtime=0",
        "timestamp_fields_raw": timestamp_bounds,
    }
    if request.get("event_context") is not None:
        record["event_context"] = request["event_context"]
    oi = _oi_provenance(request, timestamp_bounds)
    if oi:
        record["oi_provenance"] = oi
    _append_manifest(root, record)
    latest[record["request_id"]] = record
    return {
        "request_id": record["request_id"],
        "key": request["key"],
        "status": status,
        "rows": rows,
        "contracts": contracts,
        "raw_bytes": len(raw),
        "compressed_bytes": len(compressed),
        "sha256_raw": record["sha256_raw"],
        "sha256_gzip": record["sha256_gzip"],
        "path": str(request["relative_path"]),
    }


def _create_client() -> Any:
    from thetadata import ThetaClient

    return ThetaClient(dotenv_path=str(PROJECT_ROOT / ".env"), dataframe_type="polars")


@contextlib.contextmanager
def _quiet_sdk():
    previous_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            yield
        finally:
            logging.disable(previous_disable)


def _safe_progress_event(
    *, event: str, pid: int, request: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None, summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"event": event, "pid": pid}
    if request is not None:
        payload.update({
            "request_id": request_id(request),
            "dataset": request["key"],
            "symbol": request["symbol"],
            "request_date": request["request_date"],
            "coverage_start_date": request.get("coverage_start"),
            "coverage_end_date": request.get("coverage_end"),
            "archive_month": request.get("archive_month"),
            "path": str(request["relative_path"]),
        })
    if result is not None:
        payload.update({
            "status": result.get("status"),
            "rows": result.get("rows", 0),
            "contracts": result.get("contracts", 0),
            "compressed_bytes": result.get("compressed_bytes", 0),
        })
    if summary is not None:
        payload.update({
            "planned_requests": summary.get("planned_requests", 0),
            "attempted_or_cached_requests": summary.get("attempted_or_cached_requests", 0),
            "status": summary.get("status"),
            "status_counts": summary.get("status_counts", {}),
            "rows": summary.get("rows", 0),
            "contracts": summary.get("contracts", 0),
            "compressed_bytes": summary.get("compressed_bytes", 0),
            "data_root": summary.get("data_root"),
            "manifest": summary.get("manifest"),
        })
    return _json_value(payload)


def _write_progress(
    progress_log: Path | None,
    payload: dict[str, Any],
    *,
    emit: bool,
) -> None:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if progress_log is not None:
        progress_log.parent.mkdir(parents=True, exist_ok=True)
        with progress_log.open("a", encoding="utf-8", newline="") as stream:
            stream.write(encoded + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    if emit:
        print(encoded, flush=True)


def run_requests(
    requests: list[dict[str, Any]],
    *,
    root: Path | None = None,
    client: Any | None = None,
    sleep: Callable[[float], None] = time.sleep,
    request_gap_seconds: float = REQUEST_GAP_SECONDS,
    progress_log: Path | None = None,
    emit_progress: bool = False,
) -> dict[str, Any]:
    pid = os.getpid()
    data_root = root or default_data_root()
    data_root.mkdir(parents=True, exist_ok=True)
    _write_progress(
        progress_log,
        _safe_progress_event(
            event="run_started",
            pid=pid,
            summary={
                "planned_requests": len(requests),
                "data_root": str(data_root),
                "manifest": str(_manifest_path(data_root)),
            },
        ),
        emit=emit_progress,
    )
    initial_capacity = check_capacity(data_root)
    if initial_capacity:
        result = {"status": "capacity_blocked", "capacity_reason": initial_capacity, "requests": [], "pid": pid}
        _write_progress(progress_log, _safe_progress_event(event="run_summary", pid=pid, summary=result), emit=emit_progress)
        return result

    owns_client = client is None
    if owns_client:
        try:
            with _quiet_sdk():
                client = _create_client()
        except Exception as exc:
            status, code = classify_exception(exc)
            result = {
                "status": status if status == "forbidden" else "sdk_initialization_error",
                "error_type": type(exc).__name__,
                "error_code": code,
                "requests": [],
                "pid": pid,
            }
            _write_progress(progress_log, _safe_progress_event(event="run_summary", pid=pid, summary=result), emit=emit_progress)
            return result

    results: list[dict[str, Any]] = []
    latest = read_latest_manifest(data_root)
    previous_request_at: float | None = None
    try:
        with _exclusive_run_lock(data_root):
            for request in requests:
                if previous_request_at is not None:
                    delay = request_gap_seconds - (time.monotonic() - previous_request_at)
                    if delay > 0:
                        sleep(delay)
                with _quiet_sdk():
                    result = _run_request(client, request, data_root, latest, sleep=sleep)
                results.append(result)
                _write_progress(
                    progress_log,
                    _safe_progress_event(event="request_complete", pid=pid, request=request, result=result),
                    emit=emit_progress,
                )
                previous_request_at = time.monotonic()
                if result["status"] not in {"downloaded", "missing", "cached"}:
                    break
    finally:
        if owns_client:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    with _quiet_sdk():
                        close()
                except Exception:
                    pass

    counts = Counter(item["status"] for item in results)
    summary = {
        "status": "complete" if len(results) == len(requests) and set(counts).issubset({"downloaded", "missing", "cached"}) else "partial",
        "planned_requests": len(requests),
        "attempted_or_cached_requests": len(results),
        "status_counts": dict(counts),
        "rows": sum(int(item.get("rows", 0)) for item in results),
        "contracts": sum(int(item.get("contracts", 0)) for item in results),
        "compressed_bytes": sum(int(item.get("compressed_bytes", 0)) for item in results),
        "data_root": str(data_root),
        "manifest": str(_manifest_path(data_root)),
        "requests": results,
        "pid": pid,
    }
    _write_progress(progress_log, _safe_progress_event(event="run_summary", pid=pid, summary=summary), emit=emit_progress)
    return summary


def run_pilot(
    *,
    root: Path | None = None,
    client: Any | None = None,
    sleep: Callable[[float], None] = time.sleep,
    request_gap_seconds: float = REQUEST_GAP_SECONDS,
) -> dict[str, Any]:
    return run_requests(
        pilot_requests(), root=root, client=client, sleep=sleep,
        request_gap_seconds=request_gap_seconds,
    )


def run_episode(
    *,
    symbol: str,
    event_date: date,
    exit_date: date,
    expiration: date,
    oi_effective_session: date,
    root: Path | None = None,
    client: Any | None = None,
    sleep: Callable[[float], None] = time.sleep,
    request_gap_seconds: float = REQUEST_GAP_SECONDS,
) -> dict[str, Any]:
    requests = episode_requests(
        symbol=symbol,
        event_date=event_date,
        exit_date=exit_date,
        expiration=expiration,
        oi_effective_session=oi_effective_session,
    )
    result = run_requests(
        requests, root=root, client=client, sleep=sleep,
        request_gap_seconds=request_gap_seconds,
    )
    by_request = {item.get("request_id"): item for item in result["requests"]}
    selected_expiry = {}
    for request in requests:
        if request["method"] != "option_history_quote":
            continue
        item = by_request.get(request_id(request), {})
        selected_expiry[request["request_date"].isoformat()] = {
            "status": item.get("status", "not_attempted"),
            "rows": item.get("rows", 0),
            "contracts": item.get("contracts", 0),
        }
    pair_verified = len(selected_expiry) == 2 and all(
        item["status"] in {"downloaded", "cached"} and item["rows"] > 0 and item["contracts"] > 0
        for item in selected_expiry.values()
    )
    result["episode"] = {
        "symbol": symbol.upper(),
        "event_date": event_date.isoformat(),
        "exit_date": exit_date.isoformat(),
        "selected_expiration": expiration.isoformat(),
        "oi_effective_session": oi_effective_session.isoformat(),
        "selected_expiry_quotes_by_date": selected_expiry,
        "selected_expiry_available_with_quotes_on_both_days": pair_verified,
    }
    if result["status"] != "complete" or not pair_verified:
        result["status"] = "partial"
    return result


def run_daily_snapshot(
    *,
    symbol: str,
    snapshot_date: date,
    oi_effective_session: date,
    max_dte: int = 60,
    root: Path | None = None,
    client: Any | None = None,
    sleep: Callable[[float], None] = time.sleep,
    request_gap_seconds: float = REQUEST_GAP_SECONDS,
) -> dict[str, Any]:
    return run_requests(
        daily_snapshot_requests(
            symbol=symbol, snapshot_date=snapshot_date,
            oi_effective_session=oi_effective_session, max_dte=max_dte,
        ),
        root=root, client=client, sleep=sleep,
        request_gap_seconds=request_gap_seconds,
    )


def run_monthly_archive(
    *,
    start_date: date,
    end_date: date,
    symbols: Iterable[str] = ("QQQ", "SPY"),
    max_dte: int = 60,
    root: Path | None = None,
    client: Any | None = None,
    sleep: Callable[[float], None] = time.sleep,
    request_gap_seconds: float = REQUEST_GAP_SECONDS,
    progress_log: Path | None = None,
    emit_progress: bool = False,
) -> dict[str, Any]:
    return run_requests(
        monthly_archive_requests(
            start_date=start_date, end_date=end_date,
            symbols=symbols, max_dte=max_dte,
        ),
        root=root,
        client=client,
        sleep=sleep,
        request_gap_seconds=request_gap_seconds,
        progress_log=progress_log,
        emit_progress=emit_progress,
    )


def _parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected_date_in_YYYY_MM_DD_format") from exc


def _plan_json(requests: list[dict[str, Any]], *, mode: str) -> str:
    data_root = default_data_root()
    return json.dumps({
        "mode": mode,
        "planned_requests": len(requests),
        "data_root": str(data_root),
        "manifest": str(_manifest_path(data_root)),
        "requests": [
            {
                "dataset": request["key"],
                "method": request["method"],
                "request_date": request["request_date"].isoformat(),
                "expiration": _json_value(request["expiration"]),
                "scope": request["scope"],
                "params": _json_value(request["params"]),
            }
            for request in requests
        ],
    }, sort_keys=True)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true", help="print a request plan without authentication or network calls")
    mode.add_argument("--pilot", action="store_true", help="run the fixed, resumable 6-request pilot")
    mode.add_argument("--episode", action="store_true", help="run one explicit event/next-session-expiry pair")
    mode.add_argument("--snapshot", action="store_true", help="run one explicit daily EOD/OI snapshot")
    mode.add_argument("--monthly-pilot", action="store_true", help="run the fixed January 2021 QQQ/SPY four-request archive pilot")
    mode.add_argument("--monthly-batch", action="store_true", help="run an explicit bounded monthly QQQ/SPY EOD/OI archive")
    parser.add_argument("--symbol")
    parser.add_argument("--event-date", type=_parse_iso_date)
    parser.add_argument("--exit-date", type=_parse_iso_date)
    parser.add_argument("--expiration", type=_parse_iso_date)
    parser.add_argument("--snapshot-date", type=_parse_iso_date)
    parser.add_argument("--oi-effective-session", type=_parse_iso_date)
    parser.add_argument("--start-date", type=_parse_iso_date)
    parser.add_argument("--end-date", type=_parse_iso_date)
    parser.add_argument("--symbols", default="QQQ,SPY")
    parser.add_argument("--progress-log")
    parser.add_argument("--max-dte", type=int, default=60)
    args = parser.parse_args(argv)

    episode_fields = (args.event_date, args.exit_date, args.expiration)
    any_episode_field = any(value is not None for value in episode_fields)
    any_snapshot_field = args.snapshot_date is not None
    any_monthly_field = args.start_date is not None or args.end_date is not None or args.progress_log is not None
    if any_episode_field and any_snapshot_field:
        parser.error("choose episode dates or snapshot-date, not both")
    if any_monthly_field and not (args.monthly_batch or args.monthly_pilot):
        parser.error("start/end/progress-log options require monthly archive mode")

    if args.plan:
        if any_episode_field:
            if not all(episode_fields) or not args.symbol or not args.oi_effective_session:
                parser.error("episode planning requires --symbol, --event-date, --exit-date, --expiration, and --oi-effective-session")
            requests = episode_requests(
                symbol=args.symbol, event_date=args.event_date, exit_date=args.exit_date,
                expiration=args.expiration, oi_effective_session=args.oi_effective_session,
            )
            print(_plan_json(requests, mode="episode"))
        elif any_snapshot_field:
            if not args.symbol or not args.oi_effective_session:
                parser.error("snapshot planning requires --symbol, --snapshot-date, and --oi-effective-session")
            requests = daily_snapshot_requests(
                symbol=args.symbol, snapshot_date=args.snapshot_date,
                oi_effective_session=args.oi_effective_session, max_dte=args.max_dte,
            )
            print(_plan_json(requests, mode="snapshot"))
        else:
            if args.symbol or args.oi_effective_session:
                parser.error("pass episode or snapshot dates when planning a non-default request")
            print(_plan_json(pilot_requests(), mode="pilot"))
        return 0

    if args.pilot:
        if any_episode_field or any_snapshot_field or args.symbol or args.oi_effective_session or any_monthly_field:
            parser.error("--pilot is the fixed baseline case and accepts no episode/snapshot parameters")
        result = run_pilot()
    elif args.episode:
        if not all(episode_fields) or not args.symbol or not args.oi_effective_session or any_monthly_field:
            parser.error("--episode requires --symbol, --event-date, --exit-date, --expiration, and --oi-effective-session")
        result = run_episode(
            symbol=args.symbol, event_date=args.event_date, exit_date=args.exit_date,
            expiration=args.expiration, oi_effective_session=args.oi_effective_session,
        )
    elif args.snapshot:
        if any_episode_field or not any_snapshot_field or not args.symbol or not args.oi_effective_session or any_monthly_field:
            parser.error("--snapshot requires --symbol, --snapshot-date, and --oi-effective-session")
        result = run_daily_snapshot(
            symbol=args.symbol, snapshot_date=args.snapshot_date,
            oi_effective_session=args.oi_effective_session, max_dte=args.max_dte,
        )
    elif args.monthly_pilot:
        if any_episode_field or any_snapshot_field or any_monthly_field or args.symbol or args.oi_effective_session or args.max_dte != 60:
            parser.error("--monthly-pilot is fixed to January 2021, max DTE 60, for QQQ and SPY")
        result = run_monthly_archive(
            start_date=date(2021, 1, 1), end_date=date(2021, 1, 31),
            symbols=("QQQ", "SPY"), max_dte=60,
            progress_log=Path(args.progress_log) if args.progress_log else default_data_root() / "monthly_archive_progress.jsonl",
            emit_progress=True,
        )
    else:
        if args.start_date is None or args.end_date is None:
            parser.error("--monthly-batch requires --start-date and --end-date")
        if any_episode_field or any_snapshot_field or args.symbol or args.oi_effective_session:
            parser.error("--monthly-batch accepts --start-date, --end-date, --symbols, --max-dte, and --progress-log")
        result = run_monthly_archive(
            start_date=args.start_date, end_date=args.end_date,
            symbols=tuple(value for value in args.symbols.split(",") if value.strip()),
            max_dte=args.max_dte,
            progress_log=Path(args.progress_log) if args.progress_log else default_data_root() / "monthly_archive_progress.jsonl",
            emit_progress=True,
        )
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
