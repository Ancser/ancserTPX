"""Contracts for the one-process native desktop launcher."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from backend.desktop_app import (
    APP_HOST,
    APP_ICON_PATH,
    APP_PORT,
    APP_TITLE,
    WEBSITE_ICON_PATH,
    SingleInstanceLock,
    application_url,
    port_is_available,
    wait_for_backend,
)


ROOT = Path(__file__).resolve().parents[1]


def test_desktop_launcher_owns_native_window_and_fixed_loopback_server():
    launcher = (ROOT / "backend" / "desktop_app.py").read_text(encoding="utf-8")
    web_launcher = (ROOT / "windows web.bat").read_text(encoding="utf-8")
    vbs_launcher = (ROOT / "windows app.vbs").read_text(encoding="utf-8")

    assert "uvicorn.Server" in launcher
    assert "webview.create_window" in launcher
    assert "log_config=None" in launcher
    assert 'gui = "edgechromium" if os.name == "nt" else None' in launcher
    assert "icon=str(APP_ICON_PATH)" in launcher
    assert APP_ICON_PATH.is_file()
    assert "SingleInstanceLock" in launcher
    assert "kill_old" not in launcher
    assert "start \"\" http" not in web_launcher
    assert "backend.desktop_app" in vbs_launcher
    assert "stop_legacy_instances.ps1" in vbs_launcher
    assert "shell.Run cleanup, 0, True" in vbs_launcher
    assert "shell.Run command, 1, False" in vbs_launcher
    assert vbs_launcher.index("shell.Run cleanup, 0, True") < vbs_launcher.index(
        "pythonw = FindPythonW"
    )
    assert APP_HOST in launcher
    assert str(APP_PORT) in launcher


def test_application_url_is_loopback_and_same_origin():
    assert application_url() == f"http://{APP_HOST}:{APP_PORT}/"
    assert application_url("localhost", 8123) == "http://localhost:8123/"


def test_native_app_icon_is_the_website_favicon():
    html = (ROOT / "frontend" / "static" / "ancserTPX.html").read_text(encoding="utf-8")
    assert WEBSITE_ICON_PATH == ROOT / "frontend" / "static" / "favicon.ico"
    assert APP_ICON_PATH == WEBSITE_ICON_PATH
    assert APP_ICON_PATH.is_file()
    assert 'href="/static/favicon.ico"' in html


def test_wait_for_backend_requires_ancsertpx_health_payload():
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"status": "ok", "service": APP_TITLE}).encode()

    alive = type("AliveThread", (), {"is_alive": lambda self: True})()
    calls = []
    wait_for_backend(
        alive,
        opener=lambda *_args, **_kwargs: Response(),
        sleeper=lambda seconds: calls.append(seconds),
    )
    assert calls == []


def test_single_instance_lock_releases_for_reopen(tmp_path):
    path = tmp_path / "desktop.lock"
    first = SingleInstanceLock(path)
    second = SingleInstanceLock(path)

    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True
    second.release()


def test_port_probe_uses_the_expected_loopback_port():
    assert port_is_available(APP_HOST, 0) is True


def test_backend_shutdown_stops_web_owned_engines_and_client(monkeypatch):
    import backend.api.routes as routes

    class Engine:
        account_id = 42

        def __init__(self):
            self.stopped = 0

        async def stop(self):
            self.stopped += 1

    class Client:
        def __init__(self):
            self.disconnected = 0

        async def disconnect(self):
            self.disconnected += 1

    engine = Engine()
    client = Client()
    monkeypatch.setattr(routes, "_live_engines", {42: engine})
    monkeypatch.setattr(routes, "_live_engine", engine)
    monkeypatch.setattr(routes, "_topstepx_client", client)

    asyncio.run(routes.shutdown_live_engines())

    assert engine.stopped == 1
    assert client.disconnected == 1
    assert routes._live_engines == {}
    assert routes._live_engine is None
