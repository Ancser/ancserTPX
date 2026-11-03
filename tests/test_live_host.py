"""Separate live process (backend/live_host.py + backend/live_host_client.py).

Opt-in (ANCSERTPX_SEPARATE_LIVE_PROCESS=true).  These tests pin the contract
the split must keep: the host serves exactly the engine endpoints the UI
relays, only to the UI (token, no browser Origin), the UI's own CSRF still
guards relayed requests, a missing host answers like "no engine", the PI
recorder is never run twice, and the launcher's legacy cleanup can never kill
the host.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import live_host, live_host_client
from backend.live import pi_recorder

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "t" * 43


@pytest.fixture
def host_app():
    return live_host.create_live_host_app(TOKEN)


def _auth():
    return {live_host.TOKEN_HEADER: TOKEN}


# ── host boundary ──────────────────────────────────────────────────────────

def test_host_rejects_missing_token_and_browser_origin(host_app):
    with TestClient(host_app) as client:
        assert client.get("/host/health").status_code == 403
        wrong = client.get("/host/health", headers={live_host.TOKEN_HEADER: "nope"})
        assert wrong.status_code == 403
        browser = client.get("/host/health", headers={**_auth(), "Origin": "http://evil.test"})
        assert browser.status_code == 403
        ok = client.get("/host/health", headers=_auth())
        assert ok.status_code == 200
        assert ok.json()["service"] == live_host.SERVICE
        assert ok.json()["running_engines"] == 0


def test_host_serves_every_endpoint_the_ui_relays(host_app):
    served = {
        (method, route.path)
        for route in host_app.routes
        for method in getattr(route, "methods", ()) or ()
    }
    for method, path in live_host_client.FORWARDED:
        assert (method, path) in served, (method, path)


def test_host_answers_status_with_the_real_route_handlers(host_app):
    with TestClient(host_app) as client:
        assert client.get("/api/live/status", headers=_auth()).json() == {"running": False}
        body = client.get("/api/live/status-all", headers=_auth()).json()
        assert body == {"success": True, "engines": [], "count": 0}
        assert client.get("/api/live/engine-state", headers=_auth()).json() == {"engine": None}


def test_host_start_refuses_delta_absorption_and_connects_before_starting(host_app, monkeypatch):
    from backend.api import routes

    calls = []

    async def fake_connect(contract_id):
        calls.append(("connect", contract_id))

    async def fake_start(req):
        calls.append(("start", req.strategy))
        return {"success": True}

    monkeypatch.setattr(live_host, "ensure_connected", fake_connect)
    monkeypatch.setattr(routes, "live_start", fake_start)
    with TestClient(host_app) as client:
        refused = client.post("/api/live/start", headers=_auth(),
                              json={"account_id": 1, "strategy": "delta_absorption",
                                    "contract_id": "CON.F.US.MNQ.Z26"})
        assert refused.status_code == 400
        assert "Databento" in refused.json()["detail"]
        assert calls == []
        started = client.post("/api/live/start", headers=_auth(),
                              json={"account_id": 1, "strategy": "pi",
                                    "contract_id": "CON.F.US.MNQ.Z26"})
        assert started.status_code == 200, started.text
    assert calls == [("connect", "CON.F.US.MNQ.Z26"), ("start", "pi")]


def test_host_refuses_shutdown_while_an_engine_runs(host_app, monkeypatch):
    monkeypatch.setattr(live_host, "_running_engines", lambda: 1)
    with TestClient(host_app) as client:
        assert client.post("/host/shutdown", headers=_auth()).status_code == 409


# ── UI relay ───────────────────────────────────────────────────────────────

def _ui_app(monkeypatch, forwarded):
    monkeypatch.setenv(live_host_client.ENV_FLAG, "true")

    async def fake_forward(request):
        forwarded.append((request.method, request.url.path))
        from fastapi.responses import JSONResponse
        return JSONResponse({"relayed": True})

    monkeypatch.setattr(live_host_client, "forward", fake_forward)
    from backend.main import create_app
    return create_app()


def test_ui_relays_engine_endpoints_only_after_its_own_csrf_check(monkeypatch):
    forwarded: list = []
    app = _ui_app(monkeypatch, forwarded)
    client = TestClient(app, base_url="http://127.0.0.1:8001")
    # Mutating request without the local session + CSRF never reaches the host.
    denied = client.post("/api/live/flatten")
    assert denied.status_code == 403
    assert forwarded == []
    # A read is relayed; an endpoint that is not an engine endpoint is not.
    assert client.get("/api/live/status").json() == {"relayed": True}
    assert forwarded == [("GET", "/api/live/status")]
    client.get("/api/health")
    assert forwarded == [("GET", "/api/live/status")]
    # With the session/CSRF cookies from that GET, a mutation is relayed.
    csrf = next(v for k, v in client.cookies.items() if k.startswith("ancsertpx_csrf_"))
    relayed = client.post("/api/live/flatten", headers={"X-AncserTPX-CSRF": csrf,
                                                        "Origin": "http://127.0.0.1:8001"})
    assert relayed.json() == {"relayed": True}
    assert forwarded[-1] == ("POST", "/api/live/flatten")


def test_ui_without_the_flag_keeps_engines_in_process(monkeypatch):
    monkeypatch.delenv(live_host_client.ENV_FLAG, raising=False)
    called = []
    monkeypatch.setattr(live_host_client, "forward", lambda request: called.append(1))
    from backend.main import create_app
    client = TestClient(create_app(), base_url="http://127.0.0.1:8001")
    assert client.get("/api/live/status").json() == {"running": False}
    assert called == []


def test_missing_host_answers_like_no_engine_and_never_spawns_for_reads(monkeypatch):
    async def no_host():
        return None

    spawned = []
    monkeypatch.setattr(live_host_client, "alive_state", no_host)
    monkeypatch.setattr(live_host_client, "spawn_host", lambda: spawned.append(1))

    class _Req:
        def __init__(self, method, path):
            self.method = method
            self.url = type("U", (), {"path": path})()

    async def run(method, path):
        response = await live_host_client.forward(_Req(method, path))
        return response.status_code, json.loads(response.body)

    assert asyncio.run(run("GET", "/api/live/status")) == (200, {"running": False})
    assert asyncio.run(run("GET", "/api/live/status-all"))[1]["engines"] == []
    assert asyncio.run(run("POST", "/api/live/flatten"))[0] == 400
    assert asyncio.run(run("POST", "/api/live/stop")) == (200, {"success": True, "message": "Not running"})
    assert spawned == []


# ── PI recorder: never two Discord pollers ────────────────────────────────

def test_recorder_switch_disables_start_pause_and_resume(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "token-value")
    monkeypatch.setattr(pi_recorder, "_recorder", None)
    assert pi_recorder._get_recorder().enabled is True        # positive control
    monkeypatch.setenv("ANCSERTPX_PI_RECORDER", "off")
    recorder = pi_recorder._get_recorder()
    assert recorder.enabled is False
    assert asyncio.run(pi_recorder.resume_pi_recorder()) is False
    assert recorder.task is None


def test_ui_recorder_follows_the_host_pi_engine(monkeypatch):
    events = []

    async def pause():
        events.append("pause")

    async def resume():
        events.append("resume")

    monkeypatch.setattr(pi_recorder, "pause_pi_recorder", pause)
    monkeypatch.setattr(pi_recorder, "resume_pi_recorder", resume)
    pi_on = [{"account_id": 1, "status": {"strategy_mode": "pi", "running": True}}]
    other = [{"account_id": 1, "status": {"strategy_mode": "factor", "running": True}}]

    async def run():
        held = False
        held = await live_host_client.sync_pi_recorder_once(held, pi_on)
        held = await live_host_client.sync_pi_recorder_once(held, pi_on)
        held = await live_host_client.sync_pi_recorder_once(held, None)   # host hiccup
        assert held is True
        held = await live_host_client.sync_pi_recorder_once(held, other)
        held = await live_host_client.sync_pi_recorder_once(held, [])
        return held

    assert asyncio.run(run()) is False
    assert events == ["pause", "resume"]


# ── launcher cleanup must never match the host ────────────────────────────

def _ps_pattern(name: str) -> str:
    script = (ROOT / "backend" / "stop_legacy_instances.ps1").read_text(encoding="utf-8")
    return re.search(rf"^\${name}\s*=\s*'([^']+)'", script, re.M).group(1)


def test_legacy_cleanup_patterns_cannot_match_the_live_host():
    host_command = f'"{sys.executable}" -m backend.live_host'
    for name in ("terminalPattern", "webPattern", "launcherPattern"):
        assert not re.search(_ps_pattern(name), host_command), name
    # Positive control: the patterns do match what they are meant to stop.
    assert re.search(_ps_pattern("terminalPattern"), "python -m backend.terminal_live")
    assert re.search(_ps_pattern("webPattern"), "python -m backend.main")


# ── the real process ──────────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_real_host_process_starts_publishes_state_answers_and_shuts_down(tmp_path):
    port = _free_port()
    env = dict(os.environ, LOCALAPPDATA=str(tmp_path), XDG_STATE_HOME=str(tmp_path),
               ANCSERTPX_LIVE_HOST_PORT=str(port))
    process = subprocess.Popen([sys.executable, "-m", "backend.live_host"], cwd=str(ROOT),
                               env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    state_file = tmp_path / "ancserTPX" / live_host.STATE_FILE
    try:
        deadline = time.monotonic() + 60
        health = None
        while time.monotonic() < deadline and process.poll() is None:
            if state_file.exists():
                state = json.loads(state_file.read_text(encoding="utf-8"))
                try:
                    health = httpx.get(f"http://127.0.0.1:{port}/host/health",
                                       headers={live_host.TOKEN_HEADER: state["token"]}, timeout=2)
                    break
                except httpx.HTTPError:
                    pass
            time.sleep(0.25)
        assert health is not None and health.status_code == 200, "host never answered"
        assert state["port"] == port and state["pid"] == process.pid
        headers = {live_host.TOKEN_HEADER: state["token"]}
        status = httpx.get(f"http://127.0.0.1:{port}/api/live/status", headers=headers, timeout=5)
        assert status.json() == {"running": False}
        # A second host refuses to start while this one holds the lock.
        second = subprocess.run([sys.executable, "-m", "backend.live_host"], cwd=str(ROOT),
                                env=env, timeout=60, capture_output=True)
        assert second.returncode == 0
        assert process.poll() is None
        httpx.post(f"http://127.0.0.1:{port}/host/shutdown", headers=headers, timeout=5)
        assert process.wait(timeout=30) == 0
    finally:
        if process.poll() is None:
            process.kill()


def test_ui_spawns_a_detached_host_and_relays_to_it(tmp_path, monkeypatch):
    import psutil

    port = _free_port()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("ANCSERTPX_LIVE_HOST_PORT", str(port))

    class _Req:
        method = "GET"
        url = type("U", (), {"path": "/api/live/status"})()
        query_params = type("Q", (), {"multi_items": staticmethod(lambda: [])})()

        async def body(self):
            return b""

    state = asyncio.run(live_host_client.ensure_running(timeout=60))
    pid = int(state["pid"])
    try:
        host = psutil.Process(pid)
        assert "backend.live_host" in " ".join(host.cmdline())
        assert host.pid != os.getpid()
        response = asyncio.run(live_host_client.forward(_Req()))
        assert response.status_code == 200
        assert json.loads(response.body) == {"running": False}
        assert asyncio.run(live_host_client.engine_snapshots(max_age=0.0)) == []
        # Asking again reuses the running host instead of spawning another.
        assert int(asyncio.run(live_host_client.ensure_running())["pid"]) == pid
        httpx.post(f"http://127.0.0.1:{port}/host/shutdown",
                   headers={live_host.TOKEN_HEADER: state["token"]}, timeout=5)
        psutil.Process(pid).wait(timeout=30)
    finally:
        if psutil.pid_exists(pid):
            try:
                psutil.Process(pid).kill()
            except psutil.NoSuchProcess:
                pass


def test_host_keeps_running_after_the_ui_process_that_started_it_exits(tmp_path):
    import psutil

    port = _free_port()
    env = dict(os.environ, LOCALAPPDATA=str(tmp_path), XDG_STATE_HOME=str(tmp_path),
               ANCSERTPX_LIVE_HOST_PORT=str(port))
    parent = subprocess.run(
        [sys.executable, "-c",
         "import asyncio; from backend import live_host_client as c; "
         "print(asyncio.run(c.ensure_running(timeout=60))['pid'])"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120,
    )
    assert parent.returncode == 0, parent.stderr
    pid = int(parent.stdout.strip().splitlines()[-1])
    try:
        assert psutil.pid_exists(pid)                 # the "UI" is gone, the host is not
        state = json.loads((tmp_path / "ancserTPX" / live_host.STATE_FILE).read_text("utf-8"))
        headers = {live_host.TOKEN_HEADER: state["token"]}
        health = httpx.get(f"http://127.0.0.1:{port}/host/health", headers=headers, timeout=5)
        assert health.json()["pid"] == pid
        httpx.post(f"http://127.0.0.1:{port}/host/shutdown", headers=headers, timeout=5)
        psutil.Process(pid).wait(timeout=30)
    finally:
        if psutil.pid_exists(pid):
            try:
                psutil.Process(pid).kill()
            except psutil.NoSuchProcess:
                pass
