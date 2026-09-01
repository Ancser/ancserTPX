"""Live status health and PI listener recovery contracts.

These tests intentionally stay outside trading decisions.  They prove that the
existing ``running`` intent is accompanied by truthful worker/listener health,
and that malformed Discord transport data cannot terminate the PI feed.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from backend.db.models import StrategyParams
from backend.live.engine import LiveTradingEngine
from backend.live.pi_listener import BOT_ID, PiSignal, PiListener, parse_message


CONTRACT = "CON.F.US.MNQ.U26"


class _TaskState:
    def __init__(self, *, done: bool):
        self._done = done

    def done(self) -> bool:
        return self._done


class _ListenerHealth:
    def __init__(self, errors: int = 0, *, in_window: bool = True):
        self.errors = errors
        self.in_window = in_window

    def get_health(self) -> dict:
        return {
            "in_window": self.in_window,
            "consecutive_errors": self.errors,
            "last_error": "http_500" if self.errors else None,
        }


def _engine(strategy: str = "factor") -> LiveTradingEngine:
    params = StrategyParams(
        strategy=strategy,
        contract_id=CONTRACT,
        contract_size=1,
    )
    with patch("backend.live.engine.EMAPMOSignalMessenger.from_env", return_value=MagicMock()):
        return LiveTradingEngine(
            MagicMock(),
            22373660,
            CONTRACT,
            contract_size=1,
            strategy_params=params,
        )


def test_running_intent_reports_healthy_only_while_main_task_is_alive():
    engine = _engine()
    engine._running = True
    engine._task = _TaskState(done=False)

    status = engine.get_status()

    assert status["running"] is True
    assert status["health"] == "ok"
    assert status["task_alive"] is True
    assert status["health_reasons"] == []
    assert status["pi_listener_alive"] is None


def test_normal_warmup_before_task_creation_is_starting_not_degraded():
    engine = _engine("pi")
    engine._running = True
    engine._starting = True
    engine._task = None
    engine._pi_listener = None
    engine._pi_task = None

    status = engine.get_status()

    assert status["running"] is True
    assert status["starting"] is True
    assert status["health"] == "starting"
    assert status["task_alive"] is False
    assert status["pi_listener_alive"] is False
    assert status["health_reasons"] == []


def test_start_exception_rolls_back_workers_and_lifecycle_state():
    engine = _engine("pi")
    engine._emapmo_messenger.stop = AsyncMock()
    listener = MagicMock()
    created = {}

    async def fail_after_workers(_historical_candles):
        engine._pi_listener = listener
        created["pi"] = asyncio.create_task(asyncio.Event().wait())
        created["main"] = asyncio.create_task(asyncio.Event().wait())
        engine._pi_task = created["pi"]
        engine._task = created["main"]
        raise RuntimeError("startup failed")

    engine._start_impl = fail_after_workers

    async def exercise():
        try:
            await engine.start([])
        except RuntimeError as exc:
            assert str(exc) == "startup failed"
        else:
            raise AssertionError("startup exception was swallowed")

        assert engine._running is False
        assert engine._starting is False
        assert engine._pi_listener is None
        assert engine._pi_task is None
        assert engine._task is None
        assert created["pi"].cancelled()
        assert created["main"].cancelled()

    asyncio.run(exercise())
    listener.stop.assert_called_once_with()
    engine._emapmo_messenger.stop.assert_awaited_once_with()


def test_failed_start_is_retryable_without_reusing_orphan_tasks():
    engine = _engine()
    engine._emapmo_messenger.stop = AsyncMock()
    attempts = 0
    successful_task = None

    async def fail_once_then_start(_historical_candles):
        nonlocal attempts, successful_task
        attempts += 1
        if attempts == 1:
            raise ValueError("first attempt")
        successful_task = asyncio.create_task(asyncio.Event().wait())
        engine._task = successful_task

    engine._start_impl = fail_once_then_start

    async def exercise():
        try:
            await engine.start([])
        except ValueError:
            pass
        else:
            raise AssertionError("first startup should fail")

        await engine.start([])
        assert attempts == 2
        assert engine._running is True
        assert engine._starting is False
        assert engine._task is successful_task
        assert successful_task is not None and not successful_task.done()

        await engine._rollback_failed_start()
        assert successful_task.cancelled()

    asyncio.run(exercise())


def test_done_main_task_and_disconnect_are_explicitly_degraded():
    engine = _engine()
    engine._running = True
    engine._task = _TaskState(done=True)
    engine._disconnected = True

    status = engine.get_status()

    assert status["running"] is True  # backward-compatible intent, not false STOPPED
    assert status["health"] == "degraded"
    assert status["task_alive"] is False
    assert status["health_reasons"] == [
        "engine_task_not_running",
        "broker_disconnected",
    ]


def test_alive_main_task_becomes_degraded_at_exactly_45_seconds_in_one_tick():
    engine = _engine()
    engine._running = True
    engine._task = _TaskState(done=False)
    engine._tick_in_progress = True
    engine._tick_started_monotonic = 100.0

    with patch("backend.live.engine.time_mod.monotonic", return_value=144.999):
        before = engine.get_status()
    with patch("backend.live.engine.time_mod.monotonic", return_value=145.0):
        overdue = engine.get_status()

    assert before["health"] == "ok"
    assert "engine_tick_overdue" not in before["health_reasons"]
    assert overdue["health"] == "degraded"
    assert overdue["health_reasons"] == ["engine_tick_overdue"]
    assert overdue["tick_in_progress"] is True
    assert overdue["tick_started_age_seconds"] == 45.0
    assert overdue["tick_overdue_threshold_seconds"] == 45.0


def test_completed_tick_heartbeat_is_healthy_and_reports_sequence_without_sleep():
    engine = _engine()
    engine._running = True

    async def complete_one_tick():
        engine._running = False

    engine._tick = complete_one_tick
    asyncio.run(engine._main_loop())

    assert engine._last_tick_completed_monotonic is not None
    engine._running = True
    engine._task = _TaskState(done=False)
    engine._last_tick_completed_monotonic = 201.5
    with patch("backend.live.engine.time_mod.monotonic", return_value=206.5):
        status = engine.get_status()

    assert status["health"] == "ok"
    assert status["tick_in_progress"] is False
    assert status["tick_started_age_seconds"] is None
    assert status["last_tick_completed_age_seconds"] == 5.0
    assert status["tick_sequence"] == 1


def test_pi_phase_and_listener_liveness_use_the_pi_strategy_path():
    engine = _engine("pi")
    engine._running = True
    engine._task = _TaskState(done=False)
    engine._pi_listener = _ListenerHealth()
    engine._pi_task = _TaskState(done=True)

    status = engine.get_status()

    assert status["phase"] == engine.trend_follow.get_phase_label()
    assert status["phase"].startswith("PI ")
    assert status["pi_listener_alive"] is False
    assert status["health"] == "degraded"
    assert "pi_listener_not_running" in status["health_reasons"]


def test_pi_poll_errors_degrade_an_alive_listener_until_next_success():
    engine = _engine("pi")
    engine._running = True
    engine._task = _TaskState(done=False)
    engine._pi_listener = _ListenerHealth(errors=2)
    engine._pi_task = _TaskState(done=False)

    status = engine.get_status()

    assert status["pi_listener_alive"] is True
    assert status["pi_listener"]["last_error"] == "http_500"
    assert status["health"] == "degraded"
    assert status["health_reasons"] == ["pi_listener_poll_errors"]


def test_pi_poll_errors_remain_diagnostic_but_do_not_degrade_off_hours():
    engine = _engine("pi")
    engine._running = True
    engine._task = _TaskState(done=False)
    engine._pi_listener = _ListenerHealth(errors=2, in_window=False)
    engine._pi_task = _TaskState(done=False)

    status = engine.get_status()

    assert status["pi_listener_alive"] is True
    assert status["pi_listener"]["consecutive_errors"] == 2
    assert status["pi_listener"]["last_error"] == "http_500"
    assert status["pi_listener"]["in_window"] is False
    assert status["health"] == "ok"
    assert status["health_reasons"] == []


class _Response:
    def __init__(self, status_code: int, payload=None, error: Exception | None = None):
        self.status_code = status_code
        self._payload = payload
        self._error = error
        self.text = "response"

    def json(self):
        if self._error:
            raise self._error
        return self._payload


def test_pi_fetch_contains_bad_json_and_payload_shapes():
    listener = PiListener("token", lambda _sig: None)
    client = MagicMock()
    client.get = AsyncMock(side_effect=[
        _Response(200, error=ValueError("bad json")),
        _Response(200, payload={"unexpected": "object"}),
        _Response(200, payload=[]),
    ])

    async def exercise():
        assert await listener._fetch(client, {"limit": 1}) is None
        assert await listener._fetch(client, {"limit": 1}) is None
        assert await listener._fetch(client, {"limit": 1}) == []

    asyncio.run(exercise())
    health = listener.get_health()
    assert health["consecutive_errors"] == 0
    assert health["last_error"] is None
    assert health["last_success_age_seconds"] is not None


def test_pi_window_entry_seeds_latest_then_polls_after_cursor(monkeypatch):
    """The listener does not replay unbounded Discord history on startup."""
    listener = PiListener("token", lambda _sig: None)
    seed = {
        "id": "900",
        "timestamp": "2026-08-10T13:33:00+00:00",  # 06:33 PT replay
        "author": {"id": BOT_ID},
        "content": "@everyone (QQQ)\n• 淡蓝圈 ×1（大）",
    }
    fetch = AsyncMock(side_effect=[[seed], []])
    listener._fetch = fetch
    monkeypatch.setattr(listener, "in_window", lambda: True)

    asyncio.run(listener._poll_once(MagicMock()))

    assert [call.args[1] for call in fetch.await_args_list] == [
        {"limit": 1},
        {"limit": 50, "after": "900"},
    ]
    assert listener.get_health()["history_fetch_mode"] == "seed_latest_then_after_cursor"


def test_record_only_backfill_walks_history_until_durable_duplicate(monkeypatch):
    """Catch-up records every mark and never invokes a trading callback."""
    delivered = []
    audited = []
    messages = []
    monkeypatch.setattr("backend.live.pi_listener.load_message_ids", lambda: {"298"})
    monkeypatch.setattr("backend.live.pi_listener.load_message_timestamps", lambda: set())
    monkeypatch.setattr(
        "backend.live.pi_listener.append_signal_event",
        lambda sig, **kwargs: audited.append((sig, kwargs)) or True,
    )
    monkeypatch.setattr(
        "backend.live.pi_listener.append_message_event",
        lambda msg, **kwargs: messages.append((msg, kwargs)) or True,
    )
    monkeypatch.setattr(
        "backend.live.pi_listener.parse_message",
        lambda msg: [PiSignal(
            message_id=str(msg["id"]),
            ts=datetime.fromisoformat(msg["timestamp"]),
            equity="QQQ",
            future="MNQ",
            direction=1,
            kind="青π",
            size="中",
            pos=None,
            raw=msg.get("content", ""),
        )],
    )
    listener = PiListener("token", delivered.append, record_only=True)
    page_one = [
        {"id": "300", "timestamp": "2026-08-11T16:00:00+00:00", "author": {"id": BOT_ID}},
        {"id": "299", "timestamp": "2026-08-11T15:00:00+00:00", "author": {"id": BOT_ID}},
    ]
    page_two = [
        # The durable duplicate is the stopping boundary; older pages are not
        # requested, while all newer records in page one are retained.
        {"id": "298", "timestamp": "2026-08-10T16:00:00+00:00", "author": {"id": BOT_ID}},
        {"id": "297", "timestamp": "2026-08-10T15:00:00+00:00", "author": {"id": BOT_ID}},
    ]
    fetch = AsyncMock(side_effect=[page_one, page_two])
    listener._fetch = fetch

    result = asyncio.run(listener.backfill_recent(
        now=datetime(2026, 8, 11, 17, 0, tzinfo=timezone.utc),
    ))

    assert result["duplicate_boundary"] is True
    assert result["new_messages"] == 2
    assert [call.args[1] for call in fetch.await_args_list] == [
        {"limit": 100},
        {"limit": 100, "before": "299"},
    ]
    assert delivered == []
    assert [kwargs["event"] for _, kwargs in audited] == ["recorded", "recorded"]


def test_record_only_backfill_keeps_pre_session_out_of_signal_audit(monkeypatch):
    """06:33 replay is retained as a diagnostic message, never a chart mark."""
    audited = []
    messages = []
    monkeypatch.setattr("backend.live.pi_listener.load_message_ids", lambda: set())
    monkeypatch.setattr("backend.live.pi_listener.load_message_timestamps", lambda: set())
    monkeypatch.setattr(
        "backend.live.pi_listener.append_signal_event",
        lambda sig, **kwargs: audited.append((sig, kwargs)) or True,
    )
    monkeypatch.setattr(
        "backend.live.pi_listener.append_message_event",
        lambda msg, **kwargs: messages.append((msg, kwargs)) or True,
    )
    listener = PiListener("token", lambda _sig: (_ for _ in ()).throw(AssertionError()), record_only=True)
    replay = {
        "id": "301",
        "timestamp": "2026-08-11T13:33:00+00:00",  # 06:33 PT
        "author": {"id": BOT_ID},
        "content": "@everyone (QQQ)",
    }
    listener._fetch = AsyncMock(side_effect=[[replay], []])
    result = asyncio.run(listener.backfill_recent(
        now=datetime(2026, 8, 11, 17, 0, tzinfo=timezone.utc),
    ))

    assert result["cutoff_reached"] is False
    assert audited == []
    assert messages and messages[0][1]["event"] == "pre_session_skip"


def test_pi_dispatch_skips_bad_messages_and_delivers_later_valid_signal(monkeypatch):
    delivered = []
    audited = []
    monkeypatch.setattr(
        "backend.live.pi_listener.append_signal_event",
        lambda sig, **kwargs: audited.append((sig, kwargs)) or True,
    )
    listener = PiListener("token", delivered.append)
    valid = {
        "id": "123456789",
        "author": {"id": BOT_ID},
        "timestamp": "2026-08-10T17:00:00+00:00",
        "content": "@everyone (QQQ)\n• 青π×1 (中)",
    }

    # Discord batches are newest-first and the listener reverses them.  The
    # malformed entries therefore execute first, proving they do not block the
    # later valid message.
    asyncio.run(listener._dispatch_messages([valid, {"id": "not-a-snowflake"}, None]))

    assert len(delivered) == 1
    assert delivered[0].message_id == "123456789"
    assert delivered[0].future == "MNQ"
    assert delivered[0].received_at is not None
    assert [event[1]["event"] for event in audited] == ["received", "callback"]
    assert listener._last_id == "123456789"


def test_pi_dispatch_rejects_multi_mark_message_before_callback(monkeypatch):
    """A profitable mark cannot rescue an invalid aggregate message."""
    delivered = []
    audited = []
    message_events = []
    monkeypatch.setattr(
        "backend.live.pi_listener.append_signal_event",
        lambda sig, **kwargs: audited.append((sig, kwargs)) or True,
    )
    monkeypatch.setattr(
        "backend.live.pi_listener.append_message_event",
        lambda msg, **kwargs: message_events.append((msg, kwargs)) or True,
    )
    listener = PiListener("token", delivered.append)
    multi = {
        "id": "123456790",
        "author": {"id": BOT_ID},
        "timestamp": "2026-08-10T17:00:00+00:00",
        "content": (
            "@everyone 🚨 π信号出现（QQQ）\n"
            "• 淡蓝圈 ×1（大）\n"
            "• 青π ×1（中）"
        ),
    }

    asyncio.run(listener._dispatch_messages([multi]))

    assert len(parse_message(multi)) == 2  # positive parser assertion
    assert delivered == []
    assert audited == []
    assert [kwargs["event"] for _, kwargs in message_events] == ["multi_signal_skip"]
    assert listener.get_health()["messages_multi_signal_skipped"] == 1


def test_pi_run_recovers_after_an_unexpected_poll_cycle_exception():
    listener = PiListener("token", lambda _sig: None)
    listener._poll = 0.0  # deterministic test only; production constructor clamps to >= 1s
    calls = 0

    async def poll_once(_client):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("one bad cycle")
        listener.stop()
        return 0.0

    class _ClientContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return False

    listener._poll_once = poll_once

    async def exercise():
        with patch("httpx.AsyncClient", return_value=_ClientContext()):
            await asyncio.wait_for(listener.run(), timeout=1)

    asyncio.run(exercise())
    assert calls == 2
    assert listener.get_health()["last_error"] == "loop_ValueError"
