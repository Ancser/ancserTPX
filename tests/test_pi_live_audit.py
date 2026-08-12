from __future__ import annotations

from datetime import datetime, timezone

from backend.data.pi_live_audit import (
    append_message_event,
    append_signal_event,
    append_status_event,
    load_replay_rows,
    load_message_ids,
    load_message_timestamps,
    load_recent_events,
)
from backend.live.pi_listener import PiSignal


def _signal() -> PiSignal:
    return PiSignal(
        message_id="123456789",
        ts=datetime(2026, 8, 10, 16, 0, tzinfo=timezone.utc),
        equity="QQQ",
        future="MNQ",
        direction=1,
        kind="青π",
        size="中",
        pos="上部",
        raw="@everyone (QQQ)",
    )


def test_live_audit_keeps_source_and_local_receive_times(tmp_path):
    signal = _signal()
    received_at = datetime(2026, 8, 10, 16, 0, 2, tzinfo=timezone.utc)

    assert append_signal_event(
        signal,
        event="received",
        received_at=received_at,
        path=tmp_path / "pi.jsonl",
    )
    assert append_signal_event(
        signal,
        event="callback",
        received_at=received_at,
        accepted=False,
        path=tmp_path / "pi.jsonl",
    )

    rows = load_recent_events(path=tmp_path / "pi.jsonl")
    assert [row["event"] for row in rows] == ["received", "callback"]
    assert rows[0]["ts"] == "2026-08-10T16:00:00+00:00"
    assert rows[0]["received_at"] == "2026-08-10T16:00:02+00:00"
    assert rows[1]["accepted"] is False
    assert rows[1]["kind"] == "青π"


def test_live_audit_keeps_parser_failures_even_without_a_signal(tmp_path):
    path = tmp_path / "pi.jsonl"
    assert append_message_event(
        {
            "id": "987654321",
            "timestamp": "2026-08-10T16:01:00+00:00",
            "content": "@everyone (SPY) unsupported mark",
        },
        event="unparsed",
        path=path,
    )
    row = load_recent_events(path=path)[0]
    assert row["event"] == "unparsed"
    assert row["message_id"] == "987654321"
    assert row["raw"].endswith("unsupported mark")


def test_live_audit_status_rows_keep_cursor_and_poll_context(tmp_path):
    path = tmp_path / "pi.jsonl"
    assert append_status_event(
        "poll_complete",
        cursor="123456789",
        batch_size=2,
        in_window=True,
        path=path,
    )
    row = load_recent_events(path=path)[0]
    assert row["event"] == "poll_complete"
    assert row["cursor"] == "123456789"
    assert row["batch_size"] == 2
    assert row["in_window"] is True


def test_live_audit_message_ids_are_unbounded_for_restart_boundary(tmp_path):
    path = tmp_path / "pi.jsonl"
    for idx in range(2105):
        assert append_message_event(
            {"id": str(idx + 1), "timestamp": "2026-08-10T16:00:00+00:00"},
            event="recorded",
            path=path,
        )
    ids = load_message_ids(path=path)
    assert len(ids) == 2105
    assert "1" in ids and "2105" in ids
    stamps = load_message_timestamps(path=path)
    assert stamps == {"2026-08-10T16:00:00+00:00"}


def test_replay_rows_are_in_range_deduped_and_pre_session_filtered(tmp_path):
    path = tmp_path / "pi.jsonl"
    signal = _signal()
    signal.message_id = "replay-1"
    signal.ts = datetime(2026, 8, 11, 17, 12, 49, tzinfo=timezone.utc)
    assert append_signal_event(signal, event="received", path=path)
    # The same Discord mark may later be seen by the record-only catch-up.
    assert append_signal_event(signal, event="recorded", path=path)

    pre_session = _signal()
    pre_session.message_id = "replay-pre"
    pre_session.ts = datetime(2026, 8, 11, 13, 33, tzinfo=timezone.utc)
    assert append_signal_event(pre_session, event="received", path=path)

    rows = load_replay_rows(
        datetime(2026, 8, 11, 17, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 11, 18, 0, tzinfo=timezone.utc),
        future="MNQ",
        path=path,
    )

    assert len(rows) == 1
    assert rows[0]["id"] == "replay-1"
    assert rows[0]["symbol"] == "QQQ"
    assert rows[0]["marks"][0]["kind"] == "青π"
