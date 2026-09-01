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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

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


def load_recent_events(limit: int = 200, *, path: Path | None = None,
                       events: Iterable[str] | None = None) -> list[dict]:
    """Read the newest valid audit rows for diagnostics/API consumers.

    ``events`` restricts which ``event`` values count toward ``limit``, and
    that ordering is the whole point. This stream is dominated by heartbeat:
    the listener writes ``poll_complete`` plus ``fetch_success`` every 30-60s,
    so on 2026-08-12 the newest 2000 rows covered only 11 hours and contained
    1829 heartbeat rows against a single signal — 11 of the 12 signals in the
    file were invisible to the chart and to the backtest replay that shares
    this reader. Taking a raw tail and filtering afterwards silently loses
    them; filtering first keeps ``limit`` meaning "this many signals".
    """
    try:
        count = max(1, min(2000, int(limit)))
    except (TypeError, ValueError):
        count = 200
    wanted = {str(e) for e in events} if events else None
    target = path or AUDIT_PATH
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except Exception as exc:
        logger.warning("[PI] 無法讀取 live signal audit %s: %s", target, exc)
        return []
    rows: list[dict] = []
    # Walk backwards so a filtered read stops as soon as it has enough rows
    # instead of parsing the whole file every poll.
    for line in reversed(lines):
        if len(rows) >= count:
            break
        try:
            row = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(row, dict):
            continue
        if wanted is not None and str(row.get("event")) not in wanted:
            continue
        rows.append(row)
    rows.reverse()
    return rows


def filter_multi_signal_events(events: Iterable[dict]) -> list[dict]:
    """Remove signal rows belonging to a message with 2+ parsed marks.

    Live rows are written once per parsed mark, with a later callback row for
    that same mark.  Therefore the filter counts each distinct
    ``(kind, symbol, source timestamp, size, position)`` mark and collapses
    ``received``/``recorded``/callback variants with ``max`` before deciding.
    Two identical marks still count as two because their event rows are
    counted twice.  Raw audit storage is intentionally left untouched; this
    policy is for chart/replay consumers and protects old audit rows produced
    before the live listener gained the message-level guard.
    """
    rows = list(events)
    signal_events = {"received", "recorded", "callback", "callback_error"}
    by_message: dict[str, dict[tuple[str, str, str, str, str, str], dict[str, int]]] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("event") not in signal_events:
            continue
        message_id = str(row.get("message_id") or "")
        kind = str(row.get("kind") or "")
        if not message_id or not kind:
            continue
        mark_key = (
            kind,
            str(row.get("equity") or "").upper(),
            str(row.get("future") or "").upper(),
            str(row.get("ts") or ""),
            str(row.get("size") or ""),
            str(row.get("pos") or ""),
        )
        counts = by_message.setdefault(message_id, {}).setdefault(
            mark_key,
            {event: 0 for event in signal_events},
        )
        counts[str(row.get("event"))] += 1

    blocked: set[str] = set()
    for message_id, marks in by_message.items():
        mark_count = sum(max(counts.values()) for counts in marks.values())
        if mark_count >= 2:
            blocked.add(message_id)

    if not blocked:
        return rows
    return [
        row for row in rows
        if not (
            isinstance(row, dict)
            and str(row.get("message_id") or "") in blocked
            and row.get("event") in signal_events
        )
    ]


def load_replay_rows(
    start: datetime,
    end: datetime,
    *,
    future: str = "",
    limit: int = 2000,
    path: Path | None = None,
) -> list[dict]:
    """Return in-range PI audit marks as a run-scoped backtest input.

    This deliberately does **not** append to the immutable history file.  It
    lets a Backtest button replay signals received today without changing the
    dataset used by normal research/backtests.  A
    ``received`` and a later ``recorded`` row for the same Discord mark are
    collapsed to one mark, and the shared 07:00 PT pre-session rule is kept.
    """
    try:
        lo = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
        hi = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
        lo = lo.astimezone(timezone.utc) - timedelta(minutes=2)
        hi = hi.astimezone(timezone.utc) + timedelta(minutes=2)
    except (AttributeError, TypeError, ValueError):
        return []

    want_future = str(future or "").upper()
    symbol_by_future = {"MNQ": "QQQ", "MES": "SPY"}
    try:
        from backend.live.pi_listener import DIRECTION, is_pre_session
    except Exception:
        return []

    rows: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    # Filter in the reader, not here: heartbeat rows would otherwise consume
    # the whole window and the replay would silently see almost no signals.
    audit_events = filter_multi_signal_events(
        load_recent_events(limit, path=path, events=("received", "recorded"))
    )
    for event in audit_events:
        if event.get("event") not in {"received", "recorded"}:
            continue
        kind = str(event.get("kind") or "")
        if kind not in DIRECTION:
            continue
        try:
            ts = datetime.fromisoformat(
                str(event.get("ts") or "").replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts = ts.astimezone(timezone.utc)
        if ts < lo or ts > hi or is_pre_session(ts):
            continue

        event_future = str(event.get("future") or "").upper()
        if want_future and event_future != want_future:
            continue
        symbol = str(event.get("equity") or "").upper()
        if not symbol:
            symbol = symbol_by_future.get(event_future, "")
        if symbol not in {"QQQ", "SPY"}:
            continue

        message_id = str(event.get("message_id") or "")
        key = (message_id or ts.isoformat(), kind, symbol)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "id": message_id,
            "ts": ts.isoformat(),
            "symbol": symbol,
            "marks": [{
                "kind": kind,
                "size": event.get("size") or "?",
                "count": 1,
                "pos": event.get("pos"),
            }],
            "content": event.get("raw") or "",
        })

    rows.sort(key=lambda row: str(row.get("ts") or ""))
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
                "pre_session_skip", "multi_signal_skip", "unparsed", "parse_error",
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
        "pre_session_skip", "multi_signal_skip", "unparsed", "parse_error",
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
