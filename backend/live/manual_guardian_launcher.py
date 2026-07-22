"""Detached launcher and read-only status helpers for the manual-position guardian.

This module is intentionally only a hand-off layer.  It never talks to the
broker and never contains credentials.  The detached process imports its own
credentials from the normal project ``.env`` and the guardian's state-file lock
remains the single source of process ownership.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import errno
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parents[2]
GUARDIAN_SCRIPT = REPO_ROOT / "scripts" / "manual_position_guardian.py"
GUARDIAN_DATA_DIR = REPO_ROOT / "data" / "position_guardian"
GUARDIAN_OWNER = "ancserTPX-position-guardian-v1"
DEFAULT_MAX_LOG_BYTES = 512 * 1024
WINDOWS = os.name == "nt"


def guardian_state_path(account_id: int, position_id: int) -> Path:
    """Return the exact default state path used by the sidecar."""
    return (
        GUARDIAN_DATA_DIR
        / f"account_{int(account_id)}_position_{int(position_id)}.json"
    )


def guardian_lock_path(state_path: Path, account_id: Optional[int] = None) -> Path:
    """One account-wide lock prevents concurrent guardians and API overuse."""
    path = Path(state_path)
    resolved_account = _positive_int(account_id)
    if resolved_account is None:
        match = re.search(r"account_(\d+)_position_", path.name)
        resolved_account = _positive_int(match.group(1)) if match else None
    if resolved_account is not None:
        return path.parent / f"account_{resolved_account}.guardian.lock"
    return path.with_suffix(path.suffix + ".lock")


def guardian_log_path(state_path: Path) -> Path:
    return state_path.with_suffix(".log")


def guardian_lock_owner_path(lock_path: Path) -> Path:
    """Atomic metadata companion for the OS-held account lock."""
    path = Path(lock_path)
    return path.with_suffix(path.suffix + ".json")


class GuardianLaunchStatus(str, Enum):
    LAUNCHED = "launched"
    ALREADY_RUNNING = "already_running"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(frozen=True)
class ManualGuardianLaunchSpec:
    """Exact position and price identity handed to the detached guardian."""

    account_id: int
    position_id: int
    contract_id: str
    side: str
    size: int
    entry_price: float
    sl_price: float
    tp_price: float
    creation_timestamp: str = ""
    adopt_sl_order_id: Optional[int] = None
    adopt_tp_order_id: Optional[int] = None
    # Two supported live accounts can each own a guardian. At 2.5s, both
    # guardians consume about 96 reads/min combined, leaving material room
    # below ProjectX's 200/minute budget for both engine loops and UI refreshes.
    poll_seconds: float = 2.5
    confirm_timeout: float = 5.0
    state_path: Optional[Path] = None


@dataclass(frozen=True)
class GuardianLaunchResult:
    status: GuardianLaunchStatus
    message: str
    account_id: int
    position_id: int
    state_path: str
    lock_path: str
    log_path: str
    pid: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.status in {
            GuardianLaunchStatus.LAUNCHED,
            GuardianLaunchStatus.ALREADY_RUNNING,
        }

    def as_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["ok"] = self.ok
        return payload


@dataclass(frozen=True)
class GuardianStateSnapshot:
    """Sanitized local state; this performs no broker request."""

    account_id: int
    position_id: int
    state_path: str
    lock_path: str
    log_path: str
    state_exists: bool
    lock_exists: bool
    running: bool
    pid: Optional[int]
    lock_status: str
    status: str
    contract_id: Optional[str] = None
    side: Optional[str] = None
    creation_timestamp: Optional[str] = None
    size: Optional[int] = None
    entry_price: Optional[float] = None
    sl_price: Optional[float] = None
    tp_price: Optional[float] = None
    sl_order_id: Optional[int] = None
    tp_order_id: Optional[int] = None
    updated_at: Optional[str] = None
    error: Optional[str] = None
    account_busy: bool = False
    lock_position_id: Optional[int] = None
    lock_state_path: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _positive_int(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _finite_float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _normalized_side(value: Any) -> Optional[str]:
    raw = getattr(value, "value", value)
    text = str(raw or "").strip().lower()
    if text in {"long", "buy", "bid", "0"}:
        return "long"
    if text in {"short", "sell", "ask", "1"}:
        return "short"
    return None


def _validate_spec(spec: ManualGuardianLaunchSpec) -> Tuple[Optional[str], Optional[str]]:
    side = _normalized_side(spec.side)
    if not _positive_int(spec.account_id):
        return None, "account_id must be a positive integer"
    if not _positive_int(spec.position_id):
        return None, "position_id must be a positive integer"
    if not str(spec.contract_id or "").strip():
        return None, "contract_id is required"
    if side is None:
        return None, "side must be long/buy or short/sell"
    if not _positive_int(spec.size):
        return None, "size must be a positive integer"

    entry = _finite_float(spec.entry_price)
    sl = _finite_float(spec.sl_price)
    tp = _finite_float(spec.tp_price)
    if entry is None or sl is None or tp is None or min(entry, sl, tp) <= 0:
        return None, "entry, SL and TP must be finite positive prices"
    geometry_ok = sl < entry < tp if side == "long" else tp < entry < sl
    if not geometry_ok:
        expected = "SL < entry < TP" if side == "long" else "TP < entry < SL"
        return None, f"invalid {side} protection geometry; expected {expected}"

    sl_id = _positive_int(spec.adopt_sl_order_id)
    tp_id = _positive_int(spec.adopt_tp_order_id)
    if spec.adopt_sl_order_id is not None and sl_id is None:
        return None, "adopt_sl_order_id must be a positive integer"
    if spec.adopt_tp_order_id is not None and tp_id is None:
        return None, "adopt_tp_order_id must be a positive integer"
    if sl_id is not None and sl_id == tp_id:
        return None, "SL and TP order IDs must be different"
    return side, None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("JSON root is not an object")
    return payload


def _os_lock_held(path: Path) -> bool:
    """Probe the byte-range lock without trusting a PID or stale JSON file."""
    if not path.exists():
        return False
    handle = None
    try:
        handle = path.open("r+b")
        if WINDOWS:
            import msvcrt

            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK, 13, 36}:
                    return True
                raise
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    return True
                raise
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    except FileNotFoundError:
        return False
    finally:
        if handle is not None:
            handle.close()


def _lock_payload(path: Path) -> Dict[str, Any]:
    owner_path = guardian_lock_owner_path(path)
    if owner_path.exists():
        return _read_json(owner_path)
    # Compatibility with the original lock format during a rolling upgrade.
    if path.exists():
        return _read_json(path)
    return {}


def _inspect_lock(path: Path) -> Tuple[bool, Optional[int], str, Optional[str]]:
    owner_path = guardian_lock_owner_path(path)
    exists = path.exists() or owner_path.exists()
    if not exists:
        return False, None, "missing", None
    try:
        held = _os_lock_held(path)
    except OSError:
        return True, None, "invalid", "lock probe failed"
    if not held:
        try:
            payload = _lock_payload(path)
            pid = _positive_int(payload.get("pid"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pid = None
        return True, pid, "stale", None
    try:
        payload = _lock_payload(path)
        pid = _positive_int(payload.get("pid"))
        if pid is None:
            return True, None, "invalid", "live lock has no valid owner metadata"
        return True, pid, "live", None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return True, None, "invalid", "live lock owner metadata is unreadable"


def inspect_manual_position_guardian(
    account_id: int,
    position_id: int,
    *,
    state_path: Optional[Path] = None,
) -> GuardianStateSnapshot:
    """Inspect deterministic state/lock files without polling the broker."""
    path = Path(state_path or guardian_state_path(account_id, position_id)).resolve()
    lock = guardian_lock_path(path, account_id)
    log = guardian_log_path(path)
    lock_exists, pid, lock_status, lock_error = _inspect_lock(lock)
    lock_payload: Dict[str, Any] = {}
    if lock_status in {"live", "invalid"}:
        try:
            lock_payload = _lock_payload(lock)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            lock_payload = {}
    lock_position_id = _positive_int(lock_payload.get("position_id"))
    raw_lock_state = str(lock_payload.get("state_path") or "").strip()
    lock_state_path = str(Path(raw_lock_state).resolve()) if raw_lock_state else None
    owner_matches = bool(
        lock_status == "live"
        and (
            (lock_state_path and lock_state_path == str(path))
            or (lock_position_id and lock_position_id == int(position_id))
        )
    )
    account_busy = lock_status == "live" and not owner_matches

    payload: Dict[str, Any] = {}
    state_error: Optional[str] = None
    if path.exists():
        try:
            payload = _read_json(path)
            if payload.get("owner") != GUARDIAN_OWNER:
                payload = {}
                state_error = "state owner is unrecognized"
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            state_error = "state is unreadable"

    error = state_error or lock_error
    status = str(payload.get("status") or ("invalid" if state_error else "missing"))
    state_size = _positive_int(
        payload.get("protected_size") or payload.get("initial_size")
    )
    return GuardianStateSnapshot(
        account_id=int(account_id),
        position_id=int(position_id),
        state_path=str(path),
        lock_path=str(lock),
        log_path=str(log),
        state_exists=path.exists(),
        lock_exists=lock_exists,
        running=owner_matches,
        pid=pid,
        lock_status=lock_status,
        status=status,
        contract_id=(str(payload.get("contract_id")) if payload.get("contract_id") else None),
        side=(str(payload.get("direction")) if payload.get("direction") else None),
        creation_timestamp=(
            str(payload.get("creation_timestamp"))
            if payload.get("creation_timestamp") else None
        ),
        size=state_size,
        entry_price=_finite_float(payload.get("average_price")),
        sl_price=_finite_float(payload.get("sl_price")),
        tp_price=_finite_float(payload.get("tp_price")),
        sl_order_id=_positive_int(payload.get("sl_order_id")),
        tp_order_id=_positive_int(payload.get("tp_order_id")),
        updated_at=(str(payload.get("updated_at")) if payload.get("updated_at") else None),
        error=error,
        account_busy=account_busy,
        lock_position_id=lock_position_id,
        lock_state_path=lock_state_path,
    )


def list_manual_position_guardians(
    account_id: int,
    *,
    data_dir: Optional[Path] = None,
) -> list[GuardianStateSnapshot]:
    """List recognized per-position states for one account without broker I/O."""
    account = int(account_id)
    root = Path(data_dir or GUARDIAN_DATA_DIR)
    snapshots: list[GuardianStateSnapshot] = []
    for path in sorted(root.glob(f"account_{account}_position_*.json")):
        match = re.fullmatch(rf"account_{account}_position_(\d+)\.json", path.name)
        if not match:
            continue
        snapshots.append(
            inspect_manual_position_guardian(
                account,
                int(match.group(1)),
                state_path=path,
            )
        )
    return snapshots


def _existing_state_conflict(
    spec: ManualGuardianLaunchSpec,
    side: str,
    path: Path,
) -> Optional[str]:
    """Reject a relaunch the sidecar itself could not safely resume.

    This is deliberately read-only.  Only the sidecar may create or update its
    ownership state after it has acquired its lock.
    """
    if not path.exists():
        return None
    try:
        existing = _read_json(path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return "existing guardian state is unreadable"
    if existing.get("owner") != GUARDIAN_OWNER:
        return "existing guardian state has an unrecognized owner"

    expected = {
        "account_id": int(spec.account_id),
        "position_id": int(spec.position_id),
        "contract_id": str(spec.contract_id),
        "direction": side,
    }
    for key, value in expected.items():
        if existing.get(key) != value:
            return f"existing guardian state conflicts on {key}"
    for key, value in (("sl_price", spec.sl_price), ("tp_price", spec.tp_price)):
        current = _finite_float(existing.get(key))
        if current is None or abs(current - float(value)) > 1e-6:
            return f"existing guardian state conflicts on {key}"
    if (
        spec.creation_timestamp
        and existing.get("creation_timestamp")
        and str(existing["creation_timestamp"]) != str(spec.creation_timestamp)
    ):
        return "existing guardian state belongs to another position creation"
    for key, requested in (
        ("sl_order_id", _positive_int(spec.adopt_sl_order_id)),
        ("tp_order_id", _positive_int(spec.adopt_tp_order_id)),
    ):
        if requested is not None and _positive_int(existing.get(key)) != requested:
            return f"existing guardian state conflicts on {key}"
    return None


def _prepare_log(path: Path, max_bytes: int):
    """Rotate to one capped backup, then open the quiet append target."""
    cap = max(1024, int(max_bytes))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size >= cap:
        backup = path.with_suffix(path.suffix + ".1")
        tail_size = max(512, cap // 2)
        with path.open("rb") as source:
            source.seek(max(0, path.stat().st_size - tail_size))
            tail = source.read(tail_size)
        temp = backup.with_name(f".{backup.name}.{os.getpid()}.tmp")
        try:
            with temp.open("wb") as target:
                target.write(tail)
            os.replace(temp, backup)
            path.unlink(missing_ok=True)
        finally:
            temp.unlink(missing_ok=True)
    return path.open("ab", buffering=0)


def _detached_kwargs(
    log_handle,
    broker_env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {
        "cwd": str(REPO_ROOT),
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
        "close_fds": True,
        "shell": False,
    }
    if broker_env:
        child_env = os.environ.copy()
        child_env.update(
            {
                str(key): str(value)
                for key, value in broker_env.items()
                if value is not None
            }
        )
        kwargs["env"] = child_env
    if WINDOWS:
        flags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
        kwargs["creationflags"] = flags
        startup_cls = getattr(subprocess, "STARTUPINFO", None)
        if startup_cls is not None:
            startup = startup_cls()
            startup.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
            startup.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
            kwargs["startupinfo"] = startup
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _command(
    spec: ManualGuardianLaunchSpec,
    state_path: Path,
    script_path: Path,
) -> list[str]:
    command = [
        sys.executable,
        str(script_path),
        "--execute",
        "--account-id",
        str(int(spec.account_id)),
        "--contract-id",
        str(spec.contract_id),
        "--position-id",
        str(int(spec.position_id)),
        "--sl",
        format(float(spec.sl_price), ".12g"),
        "--tp",
        format(float(spec.tp_price), ".12g"),
        "--poll-seconds",
        format(max(1.0, float(spec.poll_seconds)), ".6g"),
        "--confirm-timeout",
        format(max(0.2, float(spec.confirm_timeout)), ".6g"),
        "--state-file",
        str(state_path),
    ]
    if spec.adopt_sl_order_id is not None:
        command.extend(["--adopt-sl-order-id", str(int(spec.adopt_sl_order_id))])
    if spec.adopt_tp_order_id is not None:
        command.extend(["--adopt-tp-order-id", str(int(spec.adopt_tp_order_id))])
    return command


def launch_manual_position_guardian(
    spec: ManualGuardianLaunchSpec,
    *,
    script_path: Optional[Path] = None,
    max_log_bytes: int = DEFAULT_MAX_LOG_BYTES,
    broker_env: Optional[Dict[str, str]] = None,
) -> GuardianLaunchResult:
    """Launch one detached sidecar and return immediately.

    ``LAUNCHED`` means the operating system accepted the detached child.  The
    sidecar's state/lock, exposed by :func:`inspect_manual_position_guardian`,
    is authoritative for whether broker validation reached ``guarding``.
    """
    path = Path(spec.state_path or guardian_state_path(spec.account_id, spec.position_id)).resolve()
    lock = guardian_lock_path(path, spec.account_id)
    log = guardian_log_path(path)

    def result(status: GuardianLaunchStatus, message: str, pid: Optional[int] = None):
        return GuardianLaunchResult(
            status=status,
            message=message,
            account_id=int(spec.account_id),
            position_id=int(spec.position_id),
            state_path=str(path),
            lock_path=str(lock),
            log_path=str(log),
            pid=pid,
        )

    side, error = _validate_spec(spec)
    if error or side is None:
        return result(GuardianLaunchStatus.BLOCKED, error or "invalid launch specification")

    lock_exists, lock_pid, lock_status, lock_error = _inspect_lock(lock)
    if lock_status == "live":
        try:
            owner = _lock_payload(lock)
            owner_position = _positive_int(owner.get("position_id"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            owner_position = None
        return result(
            GuardianLaunchStatus.ALREADY_RUNNING,
            "guardian account lock is already owned"
            + (f" for position {owner_position}" if owner_position else " by a live process"),
            lock_pid,
        )
    if lock_exists and lock_status == "invalid":
        return result(
            GuardianLaunchStatus.BLOCKED,
            lock_error or "guardian lock is invalid",
        )

    child_script = Path(script_path or GUARDIAN_SCRIPT).resolve()
    if not child_script.is_file():
        return result(GuardianLaunchStatus.FAILED, "guardian script was not found")

    state_conflict = _existing_state_conflict(spec, side, path)
    if state_conflict:
        return result(GuardianLaunchStatus.BLOCKED, state_conflict)

    command = _command(spec, path, child_script)
    log_handle = None
    try:
        log_handle = _prepare_log(log, max_log_bytes)
        process = subprocess.Popen(
            command,
            **_detached_kwargs(log_handle, broker_env),
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return result(
            GuardianLaunchStatus.FAILED,
            f"detached launch failed ({exc.__class__.__name__})",
        )
    finally:
        if log_handle is not None:
            log_handle.close()

    return result(
        GuardianLaunchStatus.LAUNCHED,
        "detached guardian started; state/lock reports readiness",
        _positive_int(getattr(process, "pid", None)),
    )


__all__ = [
    "GuardianLaunchResult",
    "GuardianLaunchStatus",
    "GuardianStateSnapshot",
    "ManualGuardianLaunchSpec",
    "guardian_lock_path",
    "guardian_lock_owner_path",
    "guardian_log_path",
    "guardian_state_path",
    "inspect_manual_position_guardian",
    "list_manual_position_guardians",
    "launch_manual_position_guardian",
]
