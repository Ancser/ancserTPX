from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from backend.live.pi_listener import (
    BOT_ID,
    CHANNEL_ID,
    HISTORICAL_PI_AUTHOR_ID,
    HISTORICAL_PI_SENDER,
    LIVE_PI_SENDER,
    parse_message,
    pi_sender_role,
)
from scripts.pi_collect_history import build_history_row, dedupe_rows


ROOT = Path(__file__).resolve().parents[1]


def _message(author_id: str, date: str, time: str = "09:45") -> dict:
    return {
        "id": f"{author_id}-{date.replace('/', '')}-{time.replace(':', '')}",
        "timestamp": "2026-09-09T07:58:00+00:00",
        "author": {"id": author_id, "username": "hoppouseiki"},
        "content": (
            f"QQQ π信号出现\nNY {date} {time} 消息时间\n\n"
            "• Level 1 淡蓝圈 ×1"
        ),
    }


def test_historical_seiki_message_is_parsed_with_provenance():
    msg = _message(HISTORICAL_PI_AUTHOR_ID, "03/05/2026")

    assert pi_sender_role(msg) == HISTORICAL_PI_SENDER
    signals = parse_message(msg)
    assert len(signals) == 1
    assert (signals[0].sender, signals[0].sender_id, signals[0].ts) == (
        HISTORICAL_PI_SENDER,
        HISTORICAL_PI_AUTHOR_ID,
        datetime(2026, 3, 5, 14, 45, tzinfo=timezone.utc),
    )

    row = build_history_row(msg)
    assert row is not None
    assert row["channel_id"] == CHANNEL_ID
    assert row["sender"] == HISTORICAL_PI_SENDER
    assert row["timestamp_source"] == "ny_content"
    assert row["ts"].startswith("2026-03-05T14:45:00")
    assert parse_message(msg, allow_historical=False) == []


def test_seiki_after_live_cutover_is_not_a_live_source():
    msg = _message(HISTORICAL_PI_AUTHOR_ID, "09/14/2026")
    assert pi_sender_role(msg) is None
    assert parse_message(msg) == []
    assert build_history_row(msg) is None


def test_pialert_remains_live_source_after_cutover():
    msg = _message(BOT_ID, "09/14/2026", "09:45")
    msg["author"]["username"] = "ancserPiAlert"
    assert pi_sender_role(msg) == LIVE_PI_SENDER
    assert parse_message(msg)[0].sender == LIVE_PI_SENDER


def test_full_channel_dedupe_keeps_pialert_on_same_source_mark():
    historical = build_history_row(_message(
        HISTORICAL_PI_AUTHOR_ID, "06/02/2026", "09:45"
    ))
    live = build_history_row(_message(BOT_ID, "06/02/2026", "09:45"))
    assert historical is not None and live is not None

    rows, duplicates = dedupe_rows([historical, live])
    assert len(rows) == 1
    assert rows[0]["sender"] == LIVE_PI_SENDER
    assert len(duplicates) == 1
    assert duplicates[0]["sender"] == HISTORICAL_PI_SENDER


def test_pi_ui_scope_matches_recollected_history_boundary():
    js = (ROOT / "frontend" / "static" / "ancserTPX.js").read_text(
        encoding="utf-8"
    )
    assert "const PI_SIGNAL_FIRST_DATE = '2026-03-05';" in js


def test_strategy_history_cache_refreshes_after_channel_replacement(tmp_path, monkeypatch):
    import json

    from backend.data import pi_history
    from backend.strategy import pi_signal as strategy_module

    row = {
        "id": "one",
        "ts": "2026-03-05T16:28:00+00:00",
        "symbol": "QQQ",
        "marks": [{"kind": "淡蓝圈", "level": 1}],
    }
    path = tmp_path / "pi_signals.json"
    path.write_text(json.dumps([row], ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(pi_history, "HIST_PATH", path)
    monkeypatch.setattr(strategy_module, "_HIST_CACHE", None)

    assert [signal.message_id for _, signal in strategy_module._load_history()] == ["one"]

    row["id"] = "two-longer"
    path.write_text(json.dumps([row], ensure_ascii=False), encoding="utf-8")
    assert [signal.message_id for _, signal in strategy_module._load_history()] == ["two-longer"]
