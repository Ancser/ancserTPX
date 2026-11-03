"""Persistent terminal scheduling and same-user App/Web control.

Importing this module never starts a worker or connects to a broker. Windows
invokes --tick at the configured time; only an explicitly enabled, due configuration
can claim a terminal launch. The terminal owns the existing account lease.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from backend.data import market_data
from backend.timebase import LOS_ANGELES

ROOT = Path(__file__).resolve().parents[1]
ZONE = LOS_ANGELES
DEFAULT = {"enabled": False, "time": "06:30", "timezone": str(ZONE),
           "account_id": 0, "preset": "", "revision": 0, "last_day": "",
           "launch_token": "", "launch_revision": -1, "last_error": "",
           "attempt_state": "idle", "attempt_count": 0, "attempt_run_id": "",
           "restart_pause_run": "", "restart_owner": "", "replacement_day": "",
           "replacement_state": "idle", "restart_expires": 0.0}
MAX_START_ATTEMPTS = 3


def state_path() -> Path:
    return market_data.runtime_path("state", "daily_start.sqlite3")


@contextmanager
def database():
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        db.execute("CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS terminals (id TEXT PRIMARY KEY, pid INTEGER, account_id INTEGER, stop INTEGER, updated REAL, status TEXT)")
        db.execute("INSERT OR IGNORE INTO settings VALUES (1, ?)", (json.dumps(DEFAULT),))
        db.commit()
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def _read(db):
    return {**DEFAULT, **json.loads(db.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])}


def _write(db, value):
    db.execute("UPDATE settings SET value=? WHERE id=1", (json.dumps(value),))


def settings():
    if not state_path().exists():
        return dict(DEFAULT)
    with database() as db:
        return _read(db)


def configure(*, enabled: bool, wall_time: str, account_id: int, preset: str):
    datetime.strptime(wall_time, "%H:%M")
    if len(wall_time) != 5 or (enabled and (account_id <= 0 or not preset)):
        raise ValueError("Select an account and preset, and use HH:MM")
    with database() as db:
        cfg = _read(db)
        cfg.update(enabled=enabled, time=wall_time, account_id=account_id,
                   preset=preset, revision=cfg["revision"] + 1,
                   launch_token="", last_error="", attempt_state="idle",
                   attempt_count=0, attempt_run_id="", restart_pause_run="",
                   restart_owner="", restart_expires=0.0, replacement_state="idle", replacement_day="")
        _write(db, cfg)
    return cfg


def install_task(wall_time: str):
    """Called only by an explicit enable request from the local UI."""
    if os.name != "nt":
        raise ValueError("Daily start installation currently supports Windows")
    python = Path(sys.executable).with_name("python.exe")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(ROOT / "scripts" / "install_daily_start.ps1"),
         "-Python", str(python), "-ProjectRoot", str(ROOT), "-StartTime", wall_time],
        capture_output=True, text=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError("Windows task installation failed: " + (result.stderr or result.stdout)[-800:])


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _process_started_at(pid: int):
    """Windows creation time in Unix seconds; unknown keeps the safety block.

    A process born after a terminal's last heartbeat cannot own that record,
    even when Windows has reused the same PID. Works for legacy records too.
    """
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                      ctypes.byref(system), ctypes.byref(user)):
            return None
        return ((created.dwHighDateTime << 32) | created.dwLowDateTime) / 10_000_000 - 11644473600
    finally:
        kernel.CloseHandle(handle)


def terminal_statuses(account_id: int = 0):
    if not state_path().exists():
        return []
    with database() as db:
        rows = list(db.execute("SELECT * FROM terminals"))
    out = []
    for row in rows:
        if account_id and row["account_id"] not in (0, account_id):
            continue
        status = json.loads(row["status"])
        if status.get("terminal_stopped") or not _pid_alive(row["pid"]):
            continue
        started = _process_started_at(row["pid"])
        if started is not None and started > row["updated"]:
            continue
        if time.time() - row["updated"] > 10:
            status.update(health="stale", phase="TERMINAL STATUS STALE", status_stale=True)
        out.append({"account_id": row["account_id"], "status": status, "run_id": row["id"]})
    return out


def begin_terminal(token: str = ""):
    with database() as db:
        cfg = _read(db)
        if token:
            if not cfg["enabled"] or token != cfg["launch_token"] or cfg["revision"] != cfg["launch_revision"]:
                raise RuntimeError("Scheduled launch was cancelled; Daily start remains stopped")
            cfg["launch_token"] = ""
            _write(db, cfg)
        run_id = uuid.uuid4().hex
        if token:
            cfg.update(attempt_state="starting", attempt_run_id=run_id)
            _write(db, cfg)
        initial = {"running": True, "starting": True, "phase": "TERMINAL STARTING",
                   "terminal": True, "scheduled": bool(token)}
        db.execute("INSERT INTO terminals VALUES (?,?,?,?,?,?)",
                   (run_id, os.getpid(), cfg["account_id"] if token else 0, 0, time.time(), json.dumps(initial)))
    return run_id, cfg


def publish(run_id: str, status: dict, account_id: int = 0, *, include_pause: bool = False):
    with database() as db:
        db.execute("UPDATE terminals SET status=?, updated=?, account_id=? WHERE id=?",
                   (json.dumps({**status, "terminal": True}, default=str), time.time(), account_id, run_id))
        row = db.execute("SELECT stop FROM terminals WHERE id=?", (run_id,)).fetchone()
        cfg = _read(db)
        if cfg.get("restart_owner") and time.time() >= cfg.get("restart_expires", 0):
            cfg.update(restart_pause_run="", restart_owner="", restart_expires=0.0,
                       replacement_state="unknown", last_error="Daily restart supervisor expired")
            _write(db, cfg)
        if (row and not row[0] and cfg["enabled"]
                and cfg["revision"] == cfg["launch_revision"]
                and cfg.get("attempt_run_id") == run_id
                and status.get("running") and not status.get("starting")
                and status.get("health") == "ok"):
            cfg.update(attempt_state="running", last_error="")
            _write(db, cfg)
        stop = not row or bool(row[0])
        pause = bool(cfg["enabled"] and cfg.get("restart_pause_run") == run_id)
        return {"stop": stop, "pause": pause} if include_pause else stop


def stop_requested(run_id: str):
    with database() as db:
        row = db.execute("SELECT stop FROM terminals WHERE id=?", (run_id,)).fetchone()
        return not row or bool(row[0])


def mark_engine_starting(run_id: str) -> bool:
    """Reserve the non-retryable engine-start boundary for a scheduled run."""
    with database() as db:
        cfg = _read(db)
        row = db.execute("SELECT stop FROM terminals WHERE id=?", (run_id,)).fetchone()
        if (not row or row[0] or not cfg["enabled"]
                or cfg["revision"] != cfg["launch_revision"]
                or cfg.get("attempt_run_id") != run_id):
            return False
        cfg["attempt_state"] = "engine_starting"
        _write(db, cfg)
        return True


def request_stop(account_id: int = 0):
    with database() as db:
        cfg = _read(db)
        cfg.update(enabled=False, revision=cfg["revision"] + 1, launch_token="",
                   attempt_state="stopped", restart_pause_run="", restart_owner="", restart_expires=0.0)
        _write(db, cfg)
        if account_id:
            db.execute("UPDATE terminals SET stop=1 WHERE account_id IN (0, ?)", (account_id,))
        else:
            db.execute("UPDATE terminals SET stop=1")


def record_error(message: str, *, token: str = "", run_id: str = ""):
    with database() as db:
        cfg = _read(db)
        if not cfg["enabled"] or cfg["revision"] != cfg["launch_revision"]:
            return
        if token and token != cfg["launch_token"]:
            return
        if run_id and run_id != cfg.get("attempt_run_id"):
            return
        cfg["last_error"] = message
        current = cfg.get("attempt_state")
        cfg["attempt_state"] = ("runtime_failed" if current == "running" else
                                "unknown" if current in {"engine_starting", "unknown"} else "failed")
        _write(db, cfg)


async def stop_terminal(account_id: int = 0):
    request_stop(account_id)
    deadline = time.monotonic() + 20
    while terminal_statuses(account_id):
        if time.monotonic() >= deadline:
            raise TimeoutError("Daily start is OFF; terminal stop acknowledgement is pending")
        await asyncio.sleep(0.25)


async def _broker_is_flat(account_id: int) -> bool:
    """Confirm positions and open orders using successful broker responses."""
    from dotenv import load_dotenv
    from backend.broker.topstepx import TopstepXClient
    load_dotenv(ROOT / ".env")
    user = os.getenv("TOPSTEPX_USERNAME", "").strip()
    key = os.getenv("TOPSTEPX_API_KEY", "").strip()
    if not user or not key:
        raise RuntimeError("Broker credentials unavailable")
    demo = os.getenv("TOPSTEPX_USE_DEMO", "").strip().lower() in {"1", "true", "yes", "y", "on"}
    client = TopstepXClient(username=user, api_key=key, use_demo=demo)
    try:
        await client.authenticate()
        accounts = await client.get_accounts()
        if not any(account.get("id") == account_id for account in accounts):
            raise RuntimeError("Configured account unavailable in broker environment")
        positions = await client._request("POST", "/api/Position/searchOpen", json={"accountId": account_id})
        orders = await client._request("POST", "/api/Order/searchOpen", json={"accountId": account_id})
        if (not isinstance(positions, dict) or positions.get("success") is not True
                or not isinstance(positions.get("positions"), list)
                or not isinstance(orders, dict) or orders.get("success") is not True
                or not isinstance(orders.get("orders"), list)):
            raise RuntimeError("Broker position/order response unavailable")
        return not positions["positions"] and not orders["orders"]
    finally:
        await client.disconnect()


def _replacement_result(owner: str, revision: int, state: str, message: str = ""):
    with database() as db:
        cfg = _read(db)
        if cfg["revision"] != revision or cfg.get("restart_owner") != owner:
            return
        cfg.update(restart_pause_run="", restart_owner="", restart_expires=0.0, replacement_state=state,
                   last_error=message)
        _write(db, cfg)


def _replacement_blocked(revision: int, today: str, message: str):
    with database() as db:
        cfg = _read(db)
        if cfg["enabled"] and cfg["revision"] == revision and not cfg.get("restart_owner"):
            cfg.update(replacement_day=today, replacement_state="blocked", last_error=message)
            _write(db, cfg)


def prepare_daily_replacement(now: datetime) -> bool:
    """Pause entry, verify flat, stop the old terminal, then allow one launch."""
    from backend.options.pi_options_account import options_market_closed
    local = now.astimezone(ZONE)
    if options_market_closed(local.date()):
        return False
    started = time.monotonic()
    cfg = settings()
    hour, minute = map(int, cfg["time"].split(":"))
    due = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    today = str(local.date())
    if (not cfg["enabled"] or cfg.get("replacement_state") == "unknown"
            or cfg["last_day"] == today or not 0 <= (local - due).total_seconds() < 600):
        return False
    terminals = terminal_statuses()
    if not terminals:
        if cfg.get("restart_owner"):
            with database() as db:
                live = _read(db)
                if live.get("restart_owner") == cfg["restart_owner"] and time.time() >= live.get("restart_expires", 0):
                    live.update(replacement_state="unknown", last_error="Previous restart interrupted; inspect broker and terminal before enabling")
                    _write(db, live)
            return False
        return True
    if len(terminals) != 1 or terminals[0]["account_id"] != cfg["account_id"]:
        _replacement_blocked(cfg["revision"], today, "Restart skipped: terminal account or ownership is uncertain")
        return False
    current = terminals[0]
    if current["status"].get("status_stale") or not current["status"].get("running"):
        _replacement_blocked(cfg["revision"], today, "Restart skipped: terminal status is stale")
        return False
    owner = uuid.uuid4().hex
    revision = cfg["revision"]
    with database() as db:
        live = _read(db)
        if (not live["enabled"] or live["revision"] != revision or live["last_day"] == today
                or live.get("restart_owner") or live.get("replacement_day") == today):
            return False
        live.update(restart_owner=owner, restart_pause_run=current["run_id"], restart_expires=time.time() + 120,
                    replacement_day=today, replacement_state="pausing", last_error="")
        _write(db, live)
    stopped = False
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            current_cfg = settings()
            if not current_cfg["enabled"] or current_cfg["revision"] != revision or current_cfg.get("restart_owner") != owner:
                return False
            rows = terminal_statuses(cfg["account_id"])
            if len(rows) != 1 or rows[0]["run_id"] != current["run_id"]:
                _replacement_result(owner, revision, "unknown", "Terminal changed while preparing restart")
                return False
            status = rows[0]["status"]
            if (status.get("restart_pause_ack") and not status.get("status_stale")
                    and status.get("running") and not status.get("starting")
                    and status.get("tick_in_progress") is False):
                if status.get("position") is not None or status.get("pending_order_id") is not None:
                    _replacement_result(owner, revision, "blocked", "Restart skipped: terminal has a position or pending order")
                    return False
                break
            time.sleep(0.25)
        else:
            _replacement_result(owner, revision, "blocked", "Restart skipped: terminal pause was not confirmed")
            return False
        if not asyncio.run(asyncio.wait_for(_broker_is_flat(cfg["account_id"]), timeout=20)):
            _replacement_result(owner, revision, "blocked", "Restart skipped: broker has a position or open order")
            return False
        if (local - due).total_seconds() + (time.monotonic() - started) >= 510:
            _replacement_result(owner, revision, "blocked", "Restart skipped: insufficient time in startup window")
            return False
        with database() as db:
            live = _read(db)
            row = db.execute("SELECT stop FROM terminals WHERE id=? AND account_id=?", (current["run_id"], cfg["account_id"])).fetchone()
            if (not live["enabled"] or live["revision"] != revision
                    or live.get("restart_owner") != owner or not row or row[0]):
                return False
            db.execute("UPDATE terminals SET stop=1 WHERE id=?", (current["run_id"],))
            live["replacement_state"] = "stopping"
            _write(db, live)
        stopped = True
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline and terminal_statuses(cfg["account_id"]):
            if not settings()["enabled"] or settings()["revision"] != revision:
                return False
            time.sleep(0.25)
        if terminal_statuses(cfg["account_id"]):
            _replacement_result(owner, revision, "unknown", "Terminal stop acknowledgement timed out")
            return False
        if not asyncio.run(asyncio.wait_for(_broker_is_flat(cfg["account_id"]), timeout=20)):
            _replacement_result(owner, revision, "unknown", "Broker position or order appeared during restart; inspect account")
            return False
        _replacement_result(owner, revision, "stopped", "")
        return True
    except Exception as exc:
        _replacement_result(owner, revision, "unknown" if stopped else "blocked",
                            "Daily restart failed: " + type(exc).__name__)
        return False


def claim_due(now: datetime):
    # The PI source follows the cash equity session. Reuse the existing
    # shared holiday/Good-Friday policy, including its early-close exclusions.
    from backend.options.pi_options_account import options_market_closed
    local = now.astimezone(ZONE)
    if options_market_closed(local.date()):
        return None
    if terminal_statuses():
        return None
    with database() as db:
        cfg = _read(db)
        hour, minute = map(int, cfg["time"].split(":"))
        due = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        today = str(local.date())
        same_day = cfg["last_day"] == today
        retry = same_day and cfg.get("attempt_state") == "failed" and cfg.get("attempt_count", 0) < MAX_START_ATTEMPTS
        if (not cfg["enabled"] or cfg.get("restart_owner") or cfg.get("replacement_state") == "unknown"
                or (same_day and not retry) or not 0 <= (local - due).total_seconds() < 600):
            return None
        cfg.update(last_day=today, launch_token=uuid.uuid4().hex,
                   launch_revision=cfg["revision"], last_error="",
                   attempt_state="claimed", attempt_count=(cfg.get("attempt_count", 0) + 1 if same_day else 1),
                   attempt_run_id="")
        _write(db, cfg)
        return cfg["launch_token"]


def tick():
    if not settings()["enabled"]:
        return
    if not prepare_daily_replacement(datetime.now(ZONE)):
        return
    while True:
        token = claim_due(datetime.now(ZONE))
        if not token:
            return
        try:
            process = subprocess.Popen([str(Path(sys.executable).with_name("python.exe")), "-m",
                                        "backend.terminal_live", "--scheduled-token", token],
                                       cwd=ROOT, creationflags=subprocess.CREATE_NEW_CONSOLE)
        except Exception as exc:
            record_error(type(exc).__name__ + ": " + str(exc), token=token)
        else:
            # The launcher waits only for a confirmed engine or process exit;
            # an unresponsive but live process is never duplicated.
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                cfg = settings()
                if not cfg["enabled"] or cfg["revision"] != cfg["launch_revision"]:
                    return
                if cfg.get("attempt_state") == "running":
                    return
                code = process.poll()
                if code is not None:
                    if cfg.get("attempt_state") not in {"failed", "unknown", "runtime_failed"}:
                        record_error(f"Terminal exited before ready (exit {code})", run_id=cfg.get("attempt_run_id", ""))
                    break
                time.sleep(1)
            else:
                with database() as db:
                    cfg = _read(db)
                    if cfg["enabled"] and cfg["revision"] == cfg["launch_revision"] and cfg.get("attempt_state") in {"claimed", "starting", "engine_starting"}:
                        cfg.update(attempt_state="unknown", last_error="Terminal startup unconfirmed; inspect the running process")
                        _write(db, cfg)
                return
        time.sleep(3)


if __name__ == "__main__":
    if sys.argv[1:] != ["--tick"]:
        raise SystemExit("Use --tick (disabled until explicitly configured)")
    tick()
