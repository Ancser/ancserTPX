from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.live import manual_guardian_launcher as launcher


def _spec(tmp_path: Path, **overrides) -> launcher.ManualGuardianLaunchSpec:
    values = {
        "account_id": 22373660,
        "position_id": 790338415,
        "contract_id": "CON.F.US.MNQ.U26",
        "side": "long",
        "size": 3,
        "entry_price": 29062.0,
        "sl_price": 28986.75,
        "tp_price": 29287.75,
        "creation_timestamp": "2026-07-17T00:53:22.058325Z",
        "state_path": tmp_path / "guardian.json",
    }
    values.update(overrides)
    return launcher.ManualGuardianLaunchSpec(**values)


def _valid_state(spec: launcher.ManualGuardianLaunchSpec, **overrides) -> dict:
    state = {
        "owner": launcher.GUARDIAN_OWNER,
        "guardian_id": "guardian-test",
        "status": "guarding",
        "account_id": spec.account_id,
        "position_id": spec.position_id,
        "contract_id": spec.contract_id,
        "direction": "long",
        "creation_timestamp": spec.creation_timestamp,
        "initial_size": spec.size,
        "protected_size": spec.size,
        "average_price": spec.entry_price,
        "sl_price": spec.sl_price,
        "tp_price": spec.tp_price,
        "sl_order_id": None,
        "tp_order_id": None,
        "updated_at": "2026-07-17T01:00:00Z",
    }
    state.update(overrides)
    return state


def test_windows_launch_is_hidden_detached_and_never_serializes_token(tmp_path, monkeypatch):
    secret = "discord-token-must-not-appear"
    monkeypatch.setenv("DISCORD_TOKEN", secret)
    monkeypatch.setattr(launcher, "WINDOWS", True)
    spec = _spec(
        tmp_path,
        adopt_sl_order_id=111,
        adopt_tp_order_id=222,
    )

    with patch.object(
        launcher.subprocess,
        "Popen",
        return_value=SimpleNamespace(pid=43210),
    ) as popen:
        result = launcher.launch_manual_position_guardian(spec)

    assert result.status is launcher.GuardianLaunchStatus.LAUNCHED
    assert result.pid == 43210
    assert result.ok
    command = popen.call_args.args[0]
    kwargs = popen.call_args.kwargs
    assert command[0] == launcher.sys.executable
    assert command[1] == str(launcher.GUARDIAN_SCRIPT)
    assert command[command.index("--poll-seconds") + 1] == "2.5"
    assert command[command.index("--adopt-sl-order-id") + 1] == "111"
    assert command[command.index("--adopt-tp-order-id") + 1] == "222"
    assert "env" not in kwargs  # inherit privately; never serialize credentials
    assert secret not in repr(popen.call_args)
    assert secret not in repr(result.as_dict())
    assert kwargs["cwd"] == str(launcher.REPO_ROOT)
    assert kwargs["close_fds"] is True
    assert kwargs["shell"] is False
    assert kwargs["stdin"] == launcher.subprocess.DEVNULL
    assert kwargs["stderr"] == launcher.subprocess.STDOUT
    assert kwargs["creationflags"] & getattr(
        launcher.subprocess,
        "CREATE_NEW_PROCESS_GROUP",
        0x00000200,
    )
    assert kwargs["creationflags"] & getattr(
        launcher.subprocess,
        "CREATE_NO_WINDOW",
        0x08000000,
    )
    assert not Path(result.state_path).exists()  # launcher never claims ownership
    assert Path(result.log_path).exists()


def test_posix_launch_uses_new_session(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "WINDOWS", False)
    spec = _spec(tmp_path, side="buy")

    with patch.object(
        launcher.subprocess,
        "Popen",
        return_value=SimpleNamespace(pid=9876),
    ) as popen:
        result = launcher.launch_manual_position_guardian(spec)

    assert result.status is launcher.GuardianLaunchStatus.LAUNCHED
    kwargs = popen.call_args.kwargs
    assert kwargs["start_new_session"] is True
    assert "creationflags" not in kwargs
    assert "startupinfo" not in kwargs


def test_exact_broker_identity_is_passed_only_through_child_environment(tmp_path):
    spec = _spec(tmp_path)
    secret = "request-specific-api-key"
    broker_env = {
        "TOPSTEPX_USERNAME": "request-user",
        "TOPSTEPX_API_KEY": secret,
        "TOPSTEPX_USE_DEMO": "true",
        "TOPSTEPX_BASE_URL": "https://demo.invalid",
    }

    with patch.object(
        launcher.subprocess,
        "Popen",
        return_value=SimpleNamespace(pid=9876),
    ) as popen:
        result = launcher.launch_manual_position_guardian(spec, broker_env=broker_env)

    command = popen.call_args.args[0]
    child_env = popen.call_args.kwargs["env"]
    assert child_env["TOPSTEPX_API_KEY"] == secret
    assert child_env["TOPSTEPX_USERNAME"] == "request-user"
    assert secret not in " ".join(command)
    assert secret not in repr(result.as_dict())


def test_live_sidecar_lock_returns_already_running_without_spawning(tmp_path, monkeypatch):
    spec = _spec(tmp_path)
    lock_path = launcher.guardian_lock_path(spec.state_path, spec.account_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_bytes(b"\0")
    launcher.guardian_lock_owner_path(lock_path).write_text(
        json.dumps(
            {
                "pid": 2468,
                "token": "not-a-credential",
                "account_id": spec.account_id,
                "position_id": spec.position_id,
                "state_path": str(spec.state_path.resolve()),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(launcher, "_os_lock_held", lambda path: True)

    with patch.object(launcher.subprocess, "Popen") as popen:
        result = launcher.launch_manual_position_guardian(spec)

    assert result.status is launcher.GuardianLaunchStatus.ALREADY_RUNNING
    assert result.pid == 2468
    popen.assert_not_called()
    snapshot = launcher.inspect_manual_position_guardian(
        spec.account_id,
        spec.position_id,
        state_path=spec.state_path,
    )
    assert snapshot.running is True
    assert snapshot.lock_status == "live"
    assert snapshot.pid == 2468


def test_unreadable_stale_lock_recovers_instead_of_blocking_forever(tmp_path):
    spec = _spec(tmp_path)
    lock_path = launcher.guardian_lock_path(spec.state_path, spec.account_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("not-json", encoding="utf-8")

    with patch.object(launcher.subprocess, "Popen") as popen:
        result = launcher.launch_manual_position_guardian(spec)

    assert result.status is launcher.GuardianLaunchStatus.LAUNCHED
    popen.assert_called_once()


def test_unreadable_live_lock_blocks_process_storm(tmp_path, monkeypatch):
    spec = _spec(tmp_path)
    lock_path = launcher.guardian_lock_path(spec.state_path, spec.account_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_bytes(b"\0")
    launcher.guardian_lock_owner_path(lock_path).write_text("not-json", encoding="utf-8")
    monkeypatch.setattr(launcher, "_os_lock_held", lambda path: True)

    with patch.object(launcher.subprocess, "Popen") as popen:
        result = launcher.launch_manual_position_guardian(spec)

    assert result.status is launcher.GuardianLaunchStatus.BLOCKED
    assert "lock" in result.message
    popen.assert_not_called()


def test_read_only_snapshot_is_sanitized_and_includes_position_identity(tmp_path, monkeypatch):
    spec = _spec(tmp_path)
    state = _valid_state(
        spec,
        sl_order_id=111,
        tp_order_id=222,
        DISCORD_TOKEN="must-never-leak",
    )
    spec.state_path.write_text(json.dumps(state), encoding="utf-8")
    lock_path = launcher.guardian_lock_path(spec.state_path, spec.account_id)
    lock_path.write_bytes(b"\0")
    launcher.guardian_lock_owner_path(lock_path).write_text(
        json.dumps({"pid": 1357, "token": "lock-token"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(launcher, "_os_lock_held", lambda path: False)

    snapshot = launcher.inspect_manual_position_guardian(
        spec.account_id,
        spec.position_id,
        state_path=spec.state_path,
    )

    assert snapshot.status == "guarding"
    assert snapshot.running is False
    assert snapshot.lock_status == "stale"
    assert snapshot.contract_id == spec.contract_id
    assert snapshot.side == "long"
    assert snapshot.creation_timestamp == spec.creation_timestamp
    assert snapshot.size == spec.size
    assert snapshot.entry_price == spec.entry_price
    assert snapshot.sl_order_id == 111
    assert snapshot.tp_order_id == 222
    assert "must-never-leak" not in repr(snapshot.as_dict())
    assert "lock-token" not in repr(snapshot.as_dict())


def test_existing_state_conflict_blocks_without_mutating_state(tmp_path):
    spec = _spec(tmp_path, adopt_sl_order_id=111)
    state = _valid_state(
        spec,
        creation_timestamp="another-position",
        sl_order_id=111,
    )
    original = json.dumps(state, sort_keys=True)
    spec.state_path.write_text(original, encoding="utf-8")

    with patch.object(launcher.subprocess, "Popen") as popen:
        result = launcher.launch_manual_position_guardian(spec)

    assert result.status is launcher.GuardianLaunchStatus.BLOCKED
    assert "another position" in result.message
    assert spec.state_path.read_text(encoding="utf-8") == original
    popen.assert_not_called()


def test_existing_state_requires_exact_explicit_adoption_ids(tmp_path):
    spec = _spec(tmp_path, adopt_sl_order_id=111, adopt_tp_order_id=222)
    spec.state_path.write_text(
        json.dumps(_valid_state(spec, sl_order_id=111, tp_order_id=999)),
        encoding="utf-8",
    )

    with patch.object(launcher.subprocess, "Popen") as popen:
        result = launcher.launch_manual_position_guardian(spec)

    assert result.status is launcher.GuardianLaunchStatus.BLOCKED
    assert "tp_order_id" in result.message
    popen.assert_not_called()


def test_log_is_rotated_to_one_capped_backup_before_append(tmp_path):
    spec = _spec(tmp_path)
    log_path = launcher.guardian_log_path(spec.state_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_bytes(b"x" * 2048)

    with patch.object(
        launcher.subprocess,
        "Popen",
        return_value=SimpleNamespace(pid=99),
    ):
        result = launcher.launch_manual_position_guardian(spec, max_log_bytes=1024)

    backup = log_path.with_suffix(log_path.suffix + ".1")
    assert result.status is launcher.GuardianLaunchStatus.LAUNCHED
    assert log_path.exists()
    assert log_path.stat().st_size == 0
    assert backup.exists()
    assert backup.stat().st_size <= 512


def test_invalid_geometry_and_spawn_failure_return_clear_status(tmp_path):
    invalid = _spec(tmp_path, sl_price=29100.0)
    with patch.object(launcher.subprocess, "Popen") as popen:
        blocked = launcher.launch_manual_position_guardian(invalid)
    assert blocked.status is launcher.GuardianLaunchStatus.BLOCKED
    popen.assert_not_called()

    valid = _spec(tmp_path)
    with patch.object(launcher.subprocess, "Popen", side_effect=OSError("boom")):
        failed = launcher.launch_manual_position_guardian(valid)
    assert failed.status is launcher.GuardianLaunchStatus.FAILED
    assert failed.pid is None
    assert not valid.state_path.exists()


def test_account_lock_is_shared_by_every_position_state(tmp_path):
    first = tmp_path / "account_123_position_7001.json"
    second = tmp_path / "account_123_position_7002.json"

    assert launcher.guardian_lock_path(first, 123) == launcher.guardian_lock_path(
        second,
        123,
    )
    assert launcher.guardian_lock_path(first, 123).name == "account_123.guardian.lock"


def test_list_guardians_only_returns_recognized_account_state_files(tmp_path):
    first = _spec(
        tmp_path,
        account_id=123,
        position_id=7001,
        state_path=tmp_path / "account_123_position_7001.json",
    )
    second = _spec(
        tmp_path,
        account_id=123,
        position_id=7002,
        state_path=tmp_path / "account_123_position_7002.json",
    )
    first.state_path.write_text(json.dumps(_valid_state(first)), encoding="utf-8")
    second.state_path.write_text(
        json.dumps(_valid_state(second, position_id=7002)),
        encoding="utf-8",
    )
    (tmp_path / "account_999_position_1.json").write_text("{}", encoding="utf-8")
    (tmp_path / "account_123_position_bad.json").write_text("{}", encoding="utf-8")

    snapshots = launcher.list_manual_position_guardians(123, data_dir=tmp_path)

    assert [item.position_id for item in snapshots] == [7001, 7002]
    assert all(item.account_id == 123 for item in snapshots)


def test_two_account_poll_budget_leaves_engine_headroom():
    # Each cycle performs one position and one open-order read. Two supported
    # accounts at the production interval stay well below the 200/minute cap.
    reads_per_minute = 2 * 2 * (60 / launcher.ManualGuardianLaunchSpec(
        account_id=1,
        position_id=1,
        contract_id="MNQ",
        side="long",
        size=1,
        entry_price=100,
        sl_price=90,
        tp_price=110,
    ).poll_seconds)

    assert reads_per_minute == 96
    assert reads_per_minute < 120


def test_account_lock_for_other_position_is_busy_not_currently_running(tmp_path, monkeypatch):
    spec = _spec(tmp_path)
    lock_path = launcher.guardian_lock_path(spec.state_path, spec.account_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_bytes(b"\0")
    other_state = tmp_path / "account_22373660_position_7002.json"
    launcher.guardian_lock_owner_path(lock_path).write_text(
        json.dumps(
            {
                "pid": 2468,
                "account_id": spec.account_id,
                "position_id": 7002,
                "state_path": str(other_state.resolve()),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(launcher, "_os_lock_held", lambda path: True)

    current = launcher.inspect_manual_position_guardian(
        spec.account_id,
        spec.position_id,
        state_path=spec.state_path,
    )

    assert current.running is False
    assert current.account_busy is True
    assert current.lock_position_id == 7002
    assert current.lock_state_path == str(other_state.resolve())
