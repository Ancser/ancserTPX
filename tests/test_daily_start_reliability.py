import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from unittest.mock import Mock
import pytest

from backend import daily_start, terminal_live
from backend.live.engine import LiveTradingEngine


def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(daily_start, "state_path", lambda: tmp_path / "daily.sqlite3")
    daily_start.configure(enabled=True, wall_time="06:30", account_id=42, preset="PI")
    return datetime(2026, 10, 27, 6, 30, tzinfo=daily_start.ZONE)


def test_failed_attempt_retries_are_bounded_and_stop_invalidates_token(monkeypatch, tmp_path):
    now = _setup(monkeypatch, tmp_path)
    tokens = []
    for _ in range(daily_start.MAX_START_ATTEMPTS):
        token = daily_start.claim_due(now)
        assert token and token not in tokens
        tokens.append(token)
        daily_start.record_error("startup failed", token=token)
    assert daily_start.claim_due(now) is None
    daily_start.request_stop(42)
    daily_start.record_error("stale", token=tokens[-1])
    cfg = daily_start.settings()
    assert not cfg["enabled"] and cfg["attempt_state"] == "stopped"


def test_concurrent_due_checks_claim_once(monkeypatch, tmp_path):
    now = _setup(monkeypatch, tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(lambda _: daily_start.claim_due(now), range(2)))
    assert sum(bool(token) for token in claimed) == 1


def test_tick_retries_only_after_child_exit(monkeypatch, tmp_path):
    now = _setup(monkeypatch, tmp_path)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    monkeypatch.setattr(daily_start, "datetime", Clock)
    monkeypatch.setattr(daily_start.time, "sleep", lambda _: None)
    popen = Mock(side_effect=lambda *a, **kw: Mock(poll=Mock(return_value=2)))
    monkeypatch.setattr(daily_start.subprocess, "Popen", popen)
    daily_start.tick()
    cfg = daily_start.settings()
    assert popen.call_count == daily_start.MAX_START_ATTEMPTS
    assert cfg["attempt_count"] == daily_start.MAX_START_ATTEMPTS
    assert cfg["attempt_state"] == "failed"
    assert "exit 2" in cfg["last_error"]


def test_engine_start_boundary_forbids_automatic_retry(monkeypatch, tmp_path):
    now = _setup(monkeypatch, tmp_path)
    token = daily_start.claim_due(now)
    run_id, _ = daily_start.begin_terminal(token)
    assert daily_start.mark_engine_starting(run_id)
    daily_start.record_error("uncertain", run_id=run_id)
    daily_start.publish(run_id, {"running": False, "terminal_stopped": True}, 42)
    assert daily_start.settings()["attempt_state"] == "unknown"
    assert daily_start.claim_due(now) is None


def test_ready_is_confirmed_only_after_healthy_engine(monkeypatch, tmp_path):
    now = _setup(monkeypatch, tmp_path)
    token = daily_start.claim_due(now)
    run_id, _ = daily_start.begin_terminal(token)
    assert daily_start.mark_engine_starting(run_id)
    daily_start.publish(run_id, {"running": True, "starting": True, "health": "starting"}, 42)
    assert daily_start.settings()["attempt_state"] == "engine_starting"
    daily_start.publish(run_id, {"running": True, "starting": False, "health": "ok"}, 42)
    assert daily_start.settings()["attempt_state"] == "running"
    daily_start.record_error("runtime error", run_id=run_id)
    assert daily_start.settings()["attempt_state"] == "runtime_failed"


def test_missing_credentials_report_scheduled_failure(monkeypatch, tmp_path):
    now = _setup(monkeypatch, tmp_path)
    token = daily_start.claim_due(now)
    monkeypatch.delenv("TOPSTEPX_USERNAME", raising=False)
    monkeypatch.delenv("TOPSTEPX_API_KEY", raising=False)
    assert asyncio.run(terminal_live.run_terminal_live(token)) == 2
    cfg = daily_start.settings()
    assert cfg["attempt_state"] == "failed"
    assert "credentials" in cfg["last_error"]


def test_monitor_recovers_without_stopping_then_honors_stop(monkeypatch):
    engine = Mock(is_running=True, _terminal_control_healthy=True)
    engine.get_status.return_value = {"running": True, "health": "ok"}
    publish = Mock(side_effect=[OSError("busy"), {"stop": False, "pause": False}, {"stop": True, "pause": False}])
    monkeypatch.setattr(daily_start, "publish", publish)
    states = []
    async def no_wait(_seconds):
        states.append(engine._terminal_control_healthy)
    monkeypatch.setattr(terminal_live.asyncio, "sleep", no_wait)
    async def run():
        stop = asyncio.Event()
        control = {"healthy": True}
        await terminal_live._monitor_terminal_control("r", lambda: engine, lambda: 42, stop, control)
        assert stop.is_set()
        assert not engine._terminal_control_healthy
    asyncio.run(run())
    assert states == [False, True]
    assert publish.call_count == 3


def test_monitor_outage_blocks_both_new_entry_paths():
    engine = object.__new__(LiveTradingEngine)
    engine._terminal_control_healthy = False
    engine._log_event = Mock()
    assert asyncio.run(engine._place_order(None)) is False
    assert asyncio.run(engine._place_market_entry(None)) is False


def _install_fake_terminal(monkeypatch):
    run_id, _ = daily_start.begin_terminal()
    daily_start.publish(run_id, {"running": True, "health": "ok"}, 42)
    def statuses(account_id=0):
        cfg = daily_start.settings()
        with daily_start.database() as db:
            row = db.execute("SELECT stop FROM terminals WHERE id=?", (run_id,)).fetchone()
        if not row or row[0]:
            return []
        return [{"run_id": run_id, "account_id": 42, "status": {
            "running": True, "starting": False, "tick_in_progress": False,
            "restart_pause_ack": cfg.get("restart_pause_run") == run_id,
            "position": None, "pending_order_id": None}}]
    monkeypatch.setattr(daily_start, "terminal_statuses", statuses)
    return run_id


def test_daily_replacement_waits_for_pause_and_checks_broker_twice(monkeypatch, tmp_path):
    now = _setup(monkeypatch, tmp_path)
    run_id = _install_fake_terminal(monkeypatch)
    checked = []
    async def flat(account):
        cfg = daily_start.settings()
        checked.append((account, cfg["replacement_state"]))
        if cfg["replacement_state"] == "stopping":
            assert daily_start.claim_due(now) is None
        return True
    monkeypatch.setattr(daily_start, "_broker_is_flat", flat)
    assert daily_start.prepare_daily_replacement(now)
    assert checked == [(42, "pausing"), (42, "stopping")]
    cfg = daily_start.settings()
    assert cfg["enabled"] and cfg["replacement_state"] == "stopped"
    assert not cfg["restart_pause_run"]
    assert daily_start.claim_due(now)


def test_daily_replacement_preserves_terminal_for_broker_position(monkeypatch, tmp_path):
    now = _setup(monkeypatch, tmp_path)
    run_id = _install_fake_terminal(monkeypatch)
    async def not_flat(_account):
        return False
    monkeypatch.setattr(daily_start, "_broker_is_flat", not_flat)
    assert not daily_start.prepare_daily_replacement(now)
    cfg = daily_start.settings()
    assert cfg["enabled"] and cfg["replacement_state"] == "blocked"
    assert not cfg["restart_pause_run"]
    with daily_start.database() as db:
        assert db.execute("SELECT stop FROM terminals WHERE id=?", (run_id,)).fetchone()[0] == 0


def test_daily_replacement_does_not_override_user_stop(monkeypatch, tmp_path):
    now = _setup(monkeypatch, tmp_path)
    _install_fake_terminal(monkeypatch)
    async def stop_during_check(_account):
        daily_start.request_stop(42)
        return True
    monkeypatch.setattr(daily_start, "_broker_is_flat", stop_during_check)
    assert not daily_start.prepare_daily_replacement(now)
    cfg = daily_start.settings()
    assert not cfg["enabled"] and not cfg["restart_owner"]
    assert daily_start.claim_due(now) is None


def test_daily_replacement_api_failure_keeps_old_terminal(monkeypatch, tmp_path):
    now = _setup(monkeypatch, tmp_path)
    run_id = _install_fake_terminal(monkeypatch)
    async def unavailable(_account):
        raise OSError("network")
    monkeypatch.setattr(daily_start, "_broker_is_flat", unavailable)
    assert not daily_start.prepare_daily_replacement(now)
    cfg = daily_start.settings()
    assert cfg["replacement_state"] == "blocked" and "OSError" in cfg["last_error"]
    with daily_start.database() as db:
        assert db.execute("SELECT stop FROM terminals WHERE id=?", (run_id,)).fetchone()[0] == 0


def test_daily_replacement_stops_if_broker_changes_after_old_terminal_stops(monkeypatch, tmp_path):
    now = _setup(monkeypatch, tmp_path)
    _install_fake_terminal(monkeypatch)
    answers = iter([True, False])
    async def flat(_account):
        return next(answers)
    monkeypatch.setattr(daily_start, "_broker_is_flat", flat)
    assert not daily_start.prepare_daily_replacement(now)
    cfg = daily_start.settings()
    assert cfg["replacement_state"] == "unknown"
    assert "appeared" in cfg["last_error"]
    assert daily_start.claim_due(now) is None
    assert not daily_start.prepare_daily_replacement(now)


def test_daily_replacement_keeps_old_terminal_near_window_end(monkeypatch, tmp_path):
    now = _setup(monkeypatch, tmp_path).replace(minute=39)
    run_id = _install_fake_terminal(monkeypatch)
    async def flat(_account):
        return True
    monkeypatch.setattr(daily_start, "_broker_is_flat", flat)
    assert not daily_start.prepare_daily_replacement(now)
    assert "insufficient time" in daily_start.settings()["last_error"]
    with daily_start.database() as db:
        assert db.execute("SELECT stop FROM terminals WHERE id=?", (run_id,)).fetchone()[0] == 0


@pytest.mark.parametrize("positions,orders,expected", [
    ({"success": True, "positions": []}, {"success": True, "orders": []}, True),
    ({"success": True, "positions": [{"size": 1}]}, {"success": True, "orders": []}, False),
    ({"success": True, "positions": []}, {"success": True, "orders": [{"id": 1}]}, False),
])
def test_broker_flat_requires_both_successful_empty_lists(monkeypatch, positions, orders, expected):
    from backend.broker import topstepx
    monkeypatch.setenv("TOPSTEPX_USERNAME", "test-user")
    monkeypatch.setenv("TOPSTEPX_API_KEY", "test-key")
    class Client:
        def __init__(self, **_kwargs):
            pass
        async def authenticate(self):
            pass
        async def get_accounts(self):
            return [{"id": 42}]
        async def _request(self, _method, path, **_kwargs):
            return positions if "Position" in path else orders
        async def disconnect(self):
            pass
    monkeypatch.setattr(topstepx, "TopstepXClient", Client)
    assert asyncio.run(daily_start._broker_is_flat(42)) is expected


def test_broker_flat_refuses_error_payload(monkeypatch):
    from backend.broker import topstepx
    monkeypatch.setenv("TOPSTEPX_USERNAME", "test-user")
    monkeypatch.setenv("TOPSTEPX_API_KEY", "test-key")
    class Client:
        def __init__(self, **_kwargs):
            pass
        async def authenticate(self):
            pass
        async def get_accounts(self):
            return [{"id": 42}]
        async def _request(self, _method, _path, **_kwargs):
            return {"success": False}
        async def disconnect(self):
            pass
    monkeypatch.setattr(topstepx, "TopstepXClient", Client)
    with pytest.raises(RuntimeError, match="response"):
        asyncio.run(daily_start._broker_is_flat(42))


@pytest.mark.parametrize("field,value", [("position", {"size": 1}), ("pending_order_id", 123)])
def test_daily_replacement_keeps_terminal_when_engine_has_exposure(monkeypatch, tmp_path, field, value):
    now = _setup(monkeypatch, tmp_path)
    run_id = _install_fake_terminal(monkeypatch)
    original = daily_start.terminal_statuses
    def statuses(account_id=0):
        rows = original(account_id)
        for row in rows:
            row["status"][field] = value
        return rows
    monkeypatch.setattr(daily_start, "terminal_statuses", statuses)
    assert not daily_start.prepare_daily_replacement(now)
    assert daily_start.settings()["replacement_state"] == "blocked"
    with daily_start.database() as db:
        assert db.execute("SELECT stop FROM terminals WHERE id=?", (run_id,)).fetchone()[0] == 0


def test_daily_replacement_keeps_old_terminal_when_no_pause_ack(monkeypatch, tmp_path):
    now = _setup(monkeypatch, tmp_path)
    run_id = _install_fake_terminal(monkeypatch)
    original = daily_start.terminal_statuses
    def statuses(account_id=0):
        rows = original(account_id)
        for row in rows:
            row["status"]["restart_pause_ack"] = False
        return rows
    monkeypatch.setattr(daily_start, "terminal_statuses", statuses)
    values = iter([0.0, 0.0, 26.0])
    monkeypatch.setattr(daily_start.time, "monotonic", lambda: next(values))
    assert not daily_start.prepare_daily_replacement(now)
    assert "pause" in daily_start.settings()["last_error"]
    with daily_start.database() as db:
        assert db.execute("SELECT stop FROM terminals WHERE id=?", (run_id,)).fetchone()[0] == 0


def test_pause_monitor_acknowledges_before_stop(monkeypatch):
    engine = Mock(is_running=True, _terminal_control_healthy=True)
    engine.get_status.side_effect = lambda: {"running": True, "health": "ok"}
    publish = Mock(side_effect=[{"stop": False, "pause": True}, {"stop": True, "pause": True}])
    monkeypatch.setattr(daily_start, "publish", publish)
    async def no_wait(_):
        pass
    monkeypatch.setattr(terminal_live.asyncio, "sleep", no_wait)
    async def run():
        stop = asyncio.Event()
        state = {"healthy": True}
        await terminal_live._monitor_terminal_control("r", lambda: engine, lambda: 42, stop, state)
        assert stop.is_set() and not engine._terminal_control_healthy
    asyncio.run(run())
    assert publish.call_args_list[0].args[1]["restart_pause_ack"] is False
    assert publish.call_args_list[1].args[1]["restart_pause_ack"] is True


def test_expired_restart_pause_is_released(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    run_id, _ = daily_start.begin_terminal()
    with daily_start.database() as db:
        cfg = daily_start._read(db)
        cfg.update(restart_owner="expired", restart_pause_run=run_id, restart_expires=1)
        daily_start._write(db, cfg)
    control = daily_start.publish(run_id, {"running": True}, 42, include_pause=True)
    assert control == {"stop": False, "pause": False}
    assert daily_start.settings()["replacement_state"] == "unknown"


def test_daily_replacement_with_real_monitor_and_sqlite_handshake(monkeypatch, tmp_path):
    now = _setup(monkeypatch, tmp_path)
    run_id, _ = daily_start.begin_terminal()
    status = {"running": True, "health": "ok", "starting": False,
              "tick_in_progress": False, "position": None, "pending_order_id": None}
    daily_start.publish(run_id, status, 42)
    engine = Mock(is_running=True, _terminal_control_healthy=True)
    engine.get_status.side_effect = lambda: dict(status)
    calls = []
    async def flat(account):
        calls.append(account)
        return True
    monkeypatch.setattr(daily_start, "_broker_is_flat", flat)
    async def run():
        stop = asyncio.Event()
        control = {"healthy": True}
        monitor = asyncio.create_task(terminal_live._monitor_terminal_control(
            run_id, lambda: engine, lambda: 42, stop, control))
        async def acknowledge_stop():
            await stop.wait()
            daily_start.publish(run_id, {"running": False, "terminal_stopped": True}, 42)
        ack = asyncio.create_task(acknowledge_stop())
        try:
            result = await asyncio.wait_for(asyncio.to_thread(daily_start.prepare_daily_replacement, now), 10)
            assert result and stop.is_set()
            await asyncio.wait_for(ack, 3)
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
    asyncio.run(run())
    assert calls == [42, 42]
    assert daily_start.settings()["replacement_state"] == "stopped"


@pytest.mark.skipif(daily_start.os.name != "nt", reason="Windows Task Scheduler installer")
def test_installed_daily_trigger_is_wall_time_so_it_follows_dst():
    """1.4.1: an offset StartBoundary pinned the task to one UTC instant.

    After the 2026-11-01 DST change the 06:30 trigger fired at 05:30 PST, the
    due check (correctly) refused it, and no terminal started for four days.
    Runs the real installer script in -DryRun mode.
    """
    import re
    import subprocess
    import sys
    from pathlib import Path

    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(daily_start.ROOT / "scripts" / "install_daily_start.ps1"),
         "-Python", str(Path(sys.executable).with_name("python.exe")),
         "-ProjectRoot", str(daily_start.ROOT), "-StartTime", "06:30", "-DryRun"],
        capture_output=True, text=True, timeout=60,
    )
    if "Pacific" in result.stderr and result.returncode:
        pytest.skip("installer requires the Pacific time zone")
    assert result.returncode == 0, result.stderr
    boundary = result.stdout.strip()
    # Positive: the installer produced the 06:30 trigger at all.
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T06:30:00", boundary), boundary
