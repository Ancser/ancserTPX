"""UI-process side of the separate live host (see ``backend/live_host.py``).

Only active with ``ANCSERTPX_SEPARATE_LIVE_PROCESS=true``.  The UI keeps its
own endpoints and security; the engine endpoints in ``FORWARDED`` are relayed
to the host, which is started on demand by the first ``/live/start`` and then
outlives the UI.  Status reads never start a host: with none running they
answer exactly what the in-process mode answers with no engine.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response

from backend.live_host import SERVICE, TOKEN_HEADER, state_path

logger = logging.getLogger(__name__)

ENV_FLAG = "ANCSERTPX_SEPARATE_LIVE_PROCESS"
ROOT = Path(__file__).resolve().parents[1]
FORWARDED: dict[tuple[str, str], float] = {
    # (method, path): request timeout in seconds
    ("POST", "/api/live/start"): 180.0,   # warm-up fetch + engine start
    ("POST", "/api/live/stop"): 60.0,
    ("POST", "/api/live/cancel-pending"): 30.0,
    ("POST", "/api/live/flatten"): 30.0,
    ("GET", "/api/live/status"): 5.0,
    ("GET", "/api/live/status-all"): 5.0,
}
# What the in-process routes answer when no engine exists.
NOT_RUNNING: dict[str, tuple[int, dict]] = {
    "/api/live/status": (200, {"running": False}),
    "/api/live/status-all": (200, {"success": True, "engines": [], "count": 0}),
    "/api/live/stop": (200, {"success": True, "message": "Not running"}),
    "/api/live/cancel-pending": (400, {"detail": "Live engine not started"}),
    "/api/live/flatten": (400, {"detail": "Live engine not started"}),
}
SPAWN_TIMEOUT_SECONDS = 20.0
SNAPSHOT_MAX_AGE_SECONDS = 1.0


def enabled() -> bool:
    return os.getenv(ENV_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}


def is_forwarded(method: str, path: str) -> bool:
    return (method.upper(), path) in FORWARDED


def read_state(path: Optional[Path] = None) -> Optional[dict]:
    try:
        state = json.loads((path or state_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if state.get("service") != SERVICE or not state.get("token") or not state.get("port"):
        return None
    return state


async def _call(state: dict, method: str, path: str, *, params=None,
                content: bytes = b"", timeout: float = 5.0) -> httpx.Response:
    url = f"http://127.0.0.1:{int(state['port'])}{path}"
    headers = {TOKEN_HEADER: str(state["token"])}
    if content:
        headers["Content-Type"] = "application/json"
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.request(method, url, params=params, content=content,
                                    headers=headers)


async def alive_state() -> Optional[dict]:
    """The published host state if that host answers its health check."""
    state = read_state()
    if state is None:
        return None
    try:
        response = await _call(state, "GET", "/host/health", timeout=2.0)
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    return state if payload.get("service") == SERVICE else None


def _host_python() -> str:
    """pythonw next to the current interpreter, so the host has no console."""
    current = Path(sys.executable)
    candidate = current.with_name("pythonw.exe")
    return str(candidate if os.name == "nt" and candidate.exists() else current)


def spawn_host() -> subprocess.Popen:
    """Start the host detached from this process so it survives the UI."""
    command = [_host_python(), "-m", "backend.live_host"]
    kwargs: dict[str, Any] = {
        "cwd": str(ROOT),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        try:
            return subprocess.Popen(
                command, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB,
                **kwargs)
        except OSError:
            # A job that forbids breakaway still lets a detached child run.
            return subprocess.Popen(command, creationflags=flags, **kwargs)
    return subprocess.Popen(command, start_new_session=True, **kwargs)


async def ensure_running(timeout: float = SPAWN_TIMEOUT_SECONDS) -> dict:
    state = await alive_state()
    if state is not None:
        return state
    logger.info("[LIVE HOST] starting separate live process")
    process = await asyncio.to_thread(spawn_host)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(0.25)
        state = await alive_state()
        if state is not None:
            return state
        if process.poll() is not None and read_state() is None:
            break
    raise RuntimeError("the separate live process did not start (see live-host.log)")


async def forward(request: Request) -> Response:
    """Relay one engine request; the UI's own security already ran."""
    method = request.method.upper()
    path = request.url.path
    timeout = FORWARDED[(method, path)]
    if path == "/api/live/stop":
        from backend import daily_start
        try:
            await daily_start.stop_terminal(int(request.query_params.get("account_id") or 0))
        except (TimeoutError, ValueError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=503)
    if path == "/api/live/start":
        try:
            state = await ensure_running()
        except RuntimeError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=503)
    else:
        state = await alive_state()
        if state is None:
            if path in {"/api/live/status", "/api/live/status-all"}:
                from backend.api import routes
                if path.endswith("status-all"):
                    return JSONResponse(await routes.live_status_all())
                return JSONResponse(await routes.live_status(int(request.query_params.get("account_id") or 0)))
            status, payload = NOT_RUNNING[path]
            return JSONResponse(payload, status_code=status)
    body = await request.body()
    try:
        response = await _call(state, method, path, params=list(request.query_params.multi_items()),
                               content=body, timeout=timeout)
    except httpx.HTTPError as exc:
        return JSONResponse({"detail": f"Live process unavailable: {type(exc).__name__}"},
                            status_code=502)
    _snapshot_cache.clear()
    return Response(response.content, status_code=response.status_code,
                    media_type=response.headers.get("content-type", "application/json"))


_snapshot_cache: dict[str, Any] = {}


async def engine_snapshots(max_age: float = SNAPSHOT_MAX_AGE_SECONDS) -> Optional[list[dict]]:
    """``[{"account_id", "status"}]`` from the host; ``[]`` with no host.

    ``None`` means the host exists but could not be read, so callers can keep
    their previous view instead of treating a hiccup as "nothing running".
    """
    cached = _snapshot_cache.get("value")
    if cached is not None and time.monotonic() - _snapshot_cache.get("at", 0.0) < max_age:
        return cached
    state = await alive_state()
    if state is None:
        from backend import daily_start
        value: Optional[list[dict]] = daily_start.terminal_statuses()
    else:
        try:
            response = await _call(state, "GET", "/api/live/status-all", timeout=5.0)
            value = list(response.json().get("engines") or []) if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            value = None
    if value is not None:
        _snapshot_cache.update(value=value, at=time.monotonic())
    return value


async def engine_state() -> Optional[dict]:
    """The host's primary-engine block for ``/live/account-state``."""
    state = await alive_state()
    if state is None:
        return None
    try:
        response = await _call(state, "GET", "/api/live/engine-state", timeout=5.0)
        return response.json().get("engine") if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


def pi_engine_running(snapshots: list[dict]) -> bool:
    return any(
        (item.get("status") or {}).get("strategy_mode") == "pi"
        and (item.get("status") or {}).get("running")
        for item in snapshots
    )


async def sync_pi_recorder_once(held: bool, snapshots: Optional[list[dict]]) -> bool:
    """One step of ``pi_recorder_sync_task``; returns the new ``held`` flag.

    ``None`` (host unreadable) keeps the current state: a hiccup must not
    resume a second Discord poller while the host's PI engine is still up.
    """
    from backend.live.pi_recorder import pause_pi_recorder, resume_pi_recorder

    if snapshots is None:
        return held
    running = pi_engine_running(snapshots)
    if running and not held:
        await pause_pi_recorder()
        return True
    if not running and held:
        await resume_pi_recorder()
        return False
    return held


async def pi_recorder_sync_task(interval: float = 5.0, initial_held: bool = False) -> None:
    """Pause this process's record-only PI listener while the host's PI
    engine owns Discord polling, and resume it when that engine stops -- the
    cross-process version of what ``live_start``/``live_stop`` do in-process."""
    held = initial_held
    while True:
        try:
            held = await sync_pi_recorder_once(held, await engine_snapshots(max_age=0.0))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[LIVE HOST] PI recorder sync failed")
        await asyncio.sleep(interval)
