"""Durable, local-only audit records for live Discord PI signals.

The historical PI signal file is the shared backtest/chart source and must
stay immutable during a live run.  Live reception therefore writes a
separate JSONL stream.  It records the Discord event timestamp *and* the local
dispatch timestamp so a missing marker can be separated into a stale source
event, a poll gap, a parser problem, or a strategy filter.

The file lives under ``data/`` (which is intentionally ignored by git) and is
best-effort: a full/read-only disk must never stop the PI listener.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

AUDIT_PATH = Path(__file__).resolve().parents[2] / "data" / "logs" / "pi_live_signals.jsonl"


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        stamp = value
    else:
        try:
            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return str(value)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).isoformat()


def _row_for_signal(signal: Any, *, event: str, received_at: Any = None,
                    accepted: bool | None = None, error: str | None = None) -> dict:
    """Build a JSON-safe row without importing ``PiSignal`` (avoids a cycle)."""
    row = {
        "event": str(event),
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "message_id": str(getattr(signal, "message_id", "")),
        # ``ts`` is Discord's source/event timestamp; ``received_at`` is the
        # local time at which this process dispatched the parsed signal.
        "ts": _iso(getattr(signal, "ts", None)),
        "received_at": _iso(received_at or getattr(signal, "received_at", None)),
        "equity": getattr(signal, "equity", None),
        "future": getattr(signal, "future", None),
        "side": getattr(signal, "side", None),
        "direction": getattr(signal, "direction", None),
        "kind": getattr(signal, "kind", None),
        "size": getattr(signal, "size", None),
        "pos": getattr(signal, "pos", None),
        "raw": getattr(signal, "raw", ""),
    }
    if accepted is not None:
        row["accepted"] = bool(accepted)
    if error:
        row["error"] = str(error)
    return row


def append_signal_event(signal: Any, *, event: str, received_at: Any = None,
                        accepted: bool | None = None, error: str | None = None,
                        path: Path | None = None) -> bool:
    """Append one parsed-signal event and contain all filesystem failures.

    ``event`` is normally ``received`` (Live), ``recorded`` (record-only),
    ``callback`` or ``callback_error``.  The listener calls this before any
    strategy callback, so a filtered or failed signal still has a durable
    record.
    """
    target = path or AUDIT_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        row = _row_for_signal(
            signal,
            event=event,
            received_at=received_at,
            accepted=accepted,
            error=error,
        )
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        return True
    except Exception as exc:  # pragma: no cover - exercised with OS failures in production
        logger.warning("[PI] 無法寫入 live signal audit %s: %s", target, exc)
        return False


def append_message_event(message: Any, *, event: str, error: str | None = None,
                         path: Path | None = None) -> bool:
    """Append a transport/parser event when no ``PiSignal`` was produced."""
    target = path or AUDIT_PATH
    row = {
        "event": str(event),
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "message_id": str((message or {}).get("id") or ""),
        "ts": _iso((message or {}).get("timestamp")),
        "received_at": datetime.now(timezone.utc).isoformat(),
        "raw": (message or {}).get("content") or "",
    }
    if error:
        row["error"] = str(error)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        return True
    except Exception as exc:  # pragma: no cover - exercised with OS failures in production
        logger.warning("[PI] 無法寫入 live message audit %s: %s", target, exc)
        return False


def append_status_event(event: str, *, path: Path | None = None,
                        **fields: Any) -> bool:
    """Append a transport/listener health event to the live audit stream.

    Signal rows answer *what* was parsed.  Status rows answer *whether the
    poller was alive and what it fetched* (cursor movement, batch sizes,
    window transitions, and request failures).  The stream remains separate
    from the immutable historical signal source.
    """
    target = path or AUDIT_PATH
    row = {
        "event": str(event),
        "logged_at": datetime.now(timezone.utc).isoformat(),
    }
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, datetime):
            row[str(key)] = _iso(value)
        elif isinstance(value, (str, int, float, bool)):
            row[str(key)] = value
        else:
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                row[str(key)] = str(value)
            else:
                row[str(key)] = value
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        return True
    except Exception as exc:  # pragma: no cover - exercised with OS failures in production
        logger.warning("[PI] unable to write live status audit %s: %s", target, exc)
        return False


def load_recent_events(limit: int = 200, *, path: Path | None = None) -> list[dict]:
    """Read the newest valid audit rows for diagnostics/API consumers."""
    try:
        count = max(1, min(2000, int(limit)))
    except (TypeError, ValueError):
        count = 200
    target = path or AUDIT_PATH
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except Exception as exc:
        logger.warning("[PI] 無法讀取 live signal audit %s: %s", target, exc)
        return []
    rows: list[dict] = []
    for line in lines[-count:]:
        try:
            row = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def load_message_ids(*, path: Path | None = None) -> set[str]:
    """Return message ids already represented in the local audit stream.

    The record-only Discord catch-up uses this as a durable boundary after a
    process restart.  It deliberately scans the JSONL stream instead of using
    ``load_recent_events``' bounded API window: a busy day can contain more
    than the UI diagnostic limit, and stopping at that limit would make a
    restart fetch the same day repeatedly.
    """
    target = path or AUDIT_PATH
    try:
        handle = target.open("r", encoding="utf-8")
    except FileNotFoundError:
        return set()
    except Exception as exc:
        logger.warning("[PI] unable to read audit message ids %s: %s", target, exc)
        return set()

    ids: set[str] = set()
    try:
        for line in handle:
            try:
                row = json.loads(line)
            except (TypeError, ValueError):
                continue
            if not isinstance(row, dict):
                continue
            # A cursor seed proves only that the poller observed the newest
            # row; it is not a durable parse/record boundary.  Exclude it so
            # a later record-only restart can still repair messages behind a
            # Live engine's seed.
            if row.get("event") not in {
                "received", "recorded", "callback", "callback_error",
                "pre_session_skip", "unparsed", "parse_error",
            }:
                continue
            message_id = str(row.get("message_id") or "")
            if message_id:
                ids.add(message_id)
    except Exception as exc:  # pragma: no cover - filesystem failure is best effort
        logger.warning("[PI] unable to scan audit message ids %s: %s", target, exc)
    finally:
        handle.close()
    return ids


def load_message_timestamps(*, path: Path | None = None) -> set[str]:
    """Return source timestamps represented by parsed/message audit rows."""
    target = path or AUDIT_PATH
    try:
        handle = target.open("r", encoding="utf-8")
    except FileNotFoundError:
        return set()
    except Exception as exc:
        logger.warning("[PI] unable to read audit timestamps %s: %s", target, exc)
        return set()

    timestamps: set[str] = set()
    allowed = {
        "received", "recorded", "callback", "callback_error",
        "pre_session_skip", "unparsed", "parse_error",
    }
    try:
        for line in handle:
            try:
                row = json.loads(line)
            except (TypeError, ValueError):
                continue
            if not isinstance(row, dict) or row.get("event") not in allowed:
                continue
            stamp = row.get("ts")
            if stamp:
                timestamps.add(str(stamp))
    except Exception as exc:  # pragma: no cover - filesystem failure is best effort
        logger.warning("[PI] unable to scan audit timestamps %s: %s", target, exc)
    finally:
        handle.close()
    return timestamps
