"""Headless live-trading host: LiveTradingEngine outside the UI process.

Opt-in with ``ANCSERTPX_SEPARATE_LIVE_PROCESS=true`` in ``.env``.  In the
default layout the live engines run inside the desktop app's API process, so
closing the window, restarting the backend after a code change, or a heavy UI
job (full-history load, backtest serialisation) stops or competes with live
trading.  In separate mode the UI forwards the engine endpoints here
(``backend/live_host_client.py``) and this process keeps trading when the UI
goes away.

The engine endpoints are the web routes' own handlers
(``backend.api.routes.live_start`` / ``live_stop`` / ...), so engine
construction, the account lease, warm-up and every safety check are the same
code as the in-process mode.  The deliberate differences:

* No accumulator, Databento recorder, shadow replay or PI record-only worker;
  those stay in the UI process.  ``run_live_host`` disables the PI recorder
  (``ANCSERTPX_PI_RECORDER=off``) because ``live_stop`` resumes it, which would
  otherwise start a second Discord poller in this process.
* No candle-store writes.  The host connects its own broker client and keeps
  recent bars in memory only (``ensure_connected``).
* ``delta_absorption`` is refused: its engine borrows the UI process's
  Databento feed and would open a second paid subscription here.
* Loopback only, one random token per start, and any request carrying an
  ``Origin`` header (i.e. sent by a browser page) is rejected.
* It exits by itself once no engine is running and no request has arrived for
  ``IDLE_EXIT_SECONDS``.  While the UI is open it polls status every second,
  so this only happens after the window is closed with nothing trading.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from backend.desktop_app import (
    SingleInstanceLock,
    _configure_file_logging,
    port_is_available,
    runtime_directory,
)

logger = logging.getLogger(__name__)

HOST = "127.0.0.1"
DEFAULT_PORT = 8002
SERVICE = "ancserTPX-live-host"
TOKEN_HEADER = "X-AncserTPX-Live-Token"
STATE_FILE = "live-host.json"
LOCK_FILE = "live-host.lock"
LOG_FILE = "live-host.log"
IDLE_EXIT_SECONDS = 600.0
IDLE_CHECK_SECONDS = 30.0
REFUSED_STRATEGIES = {
    "delta_absorption": (
        "DELTA ABSORPTION borrows the app's Databento feed; run it with "
        "ANCSERTPX_SEPARATE_LIVE_PROCESS=false"
    ),
}


def host_port() -> int:
    try:
        return int(os.getenv("ANCSERTPX_LIVE_HOST_PORT", DEFAULT_PORT))
    except ValueError:
        return DEFAULT_PORT


def state_path() -> Path:
    return runtime_directory() / STATE_FILE


def write_state(token: str, port: int, path: Optional[Path] = None) -> Path:
    """Publish how the UI can reach this host (same-user runtime directory)."""
    path = path or state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "service": SERVICE,
        "pid": os.getpid(),
        "port": int(port),
        "token": token,
        "started_at": time.time(),
    }
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(temp, path)
    return path


# ── broker connection (memory only) ─────────────────────────────────────────

_connect_lock: Optional[asyncio.Lock] = None


async def ensure_connected(contract_id: str) -> None:
    """Give this process a broker client and recent bars without store writes.

    The UI's connect path (``/data/fetch-historical``) also journals candles
    into the persistent store.  Two processes appending the same journal is
    not safe (the store locks are process-local), so the host keeps its bars
    in memory: the same broker calls, none of the persistence.
    """
    global _connect_lock
    from backend.api import routes
    from backend.broker.topstepx import TopstepXClient
    from backend.db.models import BarUnit
    from backend.timebase import utc_now

    if _connect_lock is None:
        _connect_lock = asyncio.Lock()
    async with _connect_lock:
        client = routes._topstepx_client
        if client is None or not getattr(client, "token", None):
            username = routes._env("TOPSTEPX_USERNAME").strip()
            api_key = routes._env("TOPSTEPX_API_KEY").strip()
            if not username or not api_key:
                raise HTTPException(
                    400,
                    "Missing credentials: set TOPSTEPX_USERNAME and TOPSTEPX_API_KEY in .env",
                )
            use_demo = routes._env("TOPSTEPX_USE_DEMO", "false").lower() == "true"
            client = TopstepXClient(username=username, api_key=api_key, use_demo=use_demo)
            await client.authenticate()
            routes._topstepx_client = client
            logger.info("[LIVE HOST] TopstepX client authenticated")

        resolved = contract_id
        try:
            resolved = await client.get_front_month_contract_id(contract_id or "MNQ") or contract_id
        except Exception as exc:  # live_start resolves again and logs
            logger.warning("[LIVE HOST] front-month resolve failed: %s", exc)
        if resolved:
            routes._live_contract_id = resolved

        if not routes._historical_candles:
            end = utc_now()
            start = end - timedelta(days=2)
            bars = await client.get_historical_bars_paginated(
                contract_id=resolved,
                unit=BarUnit.MINUTE,
                unit_number=1,
                start_time=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                end_time=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            routes._upsert_historical_candles(list(bars or []))
            if not routes._historical_candles:
                raise HTTPException(503, "TopstepX returned no recent candles")
            logger.info("[LIVE HOST] %d recent bars in memory", len(routes._historical_candles))


# ── app ─────────────────────────────────────────────────────────────────────

def _running_engines() -> int:
    from backend.api import routes

    count = sum(
        1 for engine in routes._live_engines.values()
        if getattr(engine, "is_running", False)
    )
    return count + len(routes._live_start_client_refs)


def create_live_host_app(token: str) -> FastAPI:
    from backend.api import routes

    async def host_live_start(req):
        strategy = routes._normalize_strategy_name(req.strategy)
        if strategy in REFUSED_STRATEGIES:
            raise HTTPException(400, REFUSED_STRATEGIES[strategy])
        await ensure_connected(req.contract_id)
        return await routes.live_start(req)

    # FastAPI reads the body model from the annotation (a string under
    # ``from __future__ import annotations`` would not resolve in a closure).
    host_live_start.__annotations__["req"] = routes.LiveStartRequest

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.last_request = time.monotonic()
        watcher = asyncio.create_task(_idle_watch(app), name="live-host-idle")
        yield
        watcher.cancel()
        await routes.shutdown_live_engines()
        logger.info("[LIVE HOST] stopped")

    app = FastAPI(title=SERVICE, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.last_request = time.monotonic()
    app.state.server = None

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        if request.headers.get("origin") is not None:
            return JSONResponse({"detail": "browser requests are not accepted"}, status_code=403)
        supplied = request.headers.get(TOKEN_HEADER, "")
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            return JSONResponse({"detail": "live host token required"}, status_code=403)
        app.state.last_request = time.monotonic()
        return await call_next(request)

    router = APIRouter()
    router.add_api_route("/api/live/start", host_live_start, methods=["POST"])
    router.add_api_route("/api/live/stop", routes.live_stop, methods=["POST"])
    router.add_api_route("/api/live/cancel-pending", routes.live_cancel_pending, methods=["POST"])
    router.add_api_route("/api/live/flatten", routes.live_flatten, methods=["POST"])
    router.add_api_route("/api/live/status", routes.live_status, methods=["GET"])
    router.add_api_route("/api/live/status-all", routes.live_status_all, methods=["GET"])

    @router.get("/api/live/engine-state")
    async def engine_state():
        return {"engine": routes._engine_state_snapshot(routes._live_engine)}

    @router.get("/host/health")
    async def health():
        return {
            "status": "ok",
            "service": SERVICE,
            "pid": os.getpid(),
            "running_engines": _running_engines(),
            "connected": routes._topstepx_client is not None,
        }

    @router.post("/host/shutdown")
    async def shutdown(force: bool = False):
        if _running_engines() and not force:
            raise HTTPException(409, "A live engine is running; stop it first")
        server = app.state.server
        if server is not None:
            server.should_exit = True
        return {"success": True}

    app.include_router(router)
    return app


async def _idle_watch(app: FastAPI) -> None:
    while True:
        await asyncio.sleep(IDLE_CHECK_SECONDS)
        if _running_engines():
            continue
        if time.monotonic() - app.state.last_request < IDLE_EXIT_SECONDS:
            continue
        server = app.state.server
        if server is not None:
            logger.info("[LIVE HOST] idle with no engine running; exiting")
            server.should_exit = True
            return


def run_live_host() -> int:
    import uvicorn

    # Before anything can construct the PI recorder: live_stop resumes it,
    # which in this process would start a second Discord poller.
    os.environ["ANCSERTPX_PI_RECORDER"] = "off"
    # Broker/Discord credentials come from the project .env, as in backend.main.
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    runtime = runtime_directory()
    lock = SingleInstanceLock(runtime / LOCK_FILE)
    if not lock.acquire():
        return 0  # another host owns the engines; the UI talks to that one
    try:
        _configure_file_logging(runtime / LOG_FILE)
        port = host_port()
        if not port_is_available(HOST, port):
            logger.error("[LIVE HOST] port %s is in use", port)
            return 1
        token = secrets.token_urlsafe(32)
        app = create_live_host_app(token)
        server = uvicorn.Server(uvicorn.Config(
            app, host=HOST, port=port, lifespan="on",
            log_level="info", access_log=False, log_config=None,
        ))
        app.state.server = server
        write_state(token, port)
        logger.info("[LIVE HOST] listening on %s:%s pid=%s", HOST, port, os.getpid())
        server.run()
        return 0
    except Exception:
        logger.exception("[LIVE HOST] failed")
        return 1
    finally:
        lock.release()


def main() -> None:
    raise SystemExit(run_live_host())


if __name__ == "__main__":
    main()
