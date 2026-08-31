from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.web_security import (
    CSRF_HEADER,
    csrf_cookie_name,
    install_local_web_security,
    session_cookie_name,
)


ROOT = Path(__file__).resolve().parents[1]


def _secured_app() -> tuple[FastAPI, dict[str, int]]:
    app = FastAPI()
    calls = {"writes": 0}

    @app.get("/")
    async def index():
        return {"ok": True}

    @app.post("/api/write")
    async def write():
        calls["writes"] += 1
        return {"ok": True}

    @app.get("/static/app.js")
    async def static_asset():
        return {"asset": True}

    install_local_web_security(app)
    return app, calls


def _bootstrap(client: TestClient, port: int = 8001) -> str:
    response = client.get("/")
    assert response.status_code == 200
    assert client.cookies.get(session_cookie_name(port))
    csrf = client.cookies.get(csrf_cookie_name(port))
    assert csrf
    return csrf


def test_local_get_bootstraps_port_scoped_session_and_security_headers():
    app, _ = _secured_app()
    with TestClient(app, base_url="http://127.0.0.1:8001") as client:
        response = client.get("/")
        static_response = client.get("/static/app.js")

    assert response.status_code == 200
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "access-control-allow-origin" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    assert static_response.headers["cache-control"] == "no-store"
    assert response.cookies.get(session_cookie_name(8001))
    assert response.cookies.get(csrf_cookie_name(8001))
    set_cookies = response.headers.get_list("set-cookie")
    session_line = next(
        row for row in set_cookies if row.startswith(session_cookie_name(8001) + "=")
    )
    csrf_line = next(
        row for row in set_cookies if row.startswith(csrf_cookie_name(8001) + "=")
    )
    assert "HttpOnly" in session_line and "SameSite=strict" in session_line
    assert "HttpOnly" not in csrf_line and "SameSite=strict" in csrf_line


def test_mutating_api_is_rejected_before_route_without_session_and_csrf(caplog):
    app, calls = _secured_app()
    with caplog.at_level(logging.WARNING, logger="backend.web_security"):
        with TestClient(app, base_url="http://127.0.0.1:8001") as client:
            response = client.post("/api/write")

    assert response.status_code == 403
    assert response.json()["code"] == "WEB_CSRF_REQUIRED"
    assert calls["writes"] == 0
    assert "code=WEB_CSRF_REQUIRED" in caplog.text
    assert "method=POST path=/api/write" in caplog.text


def test_same_origin_session_and_csrf_allow_mutating_api():
    app, calls = _secured_app()
    with TestClient(app, base_url="http://127.0.0.1:8001") as client:
        csrf = _bootstrap(client)
        response = client.post(
            "/api/write",
            headers={
                CSRF_HEADER: csrf,
                "Origin": "http://127.0.0.1:8001",
            },
        )

    assert response.status_code == 200
    assert calls["writes"] == 1


def test_cross_origin_is_rejected_even_with_valid_cookie_and_header():
    app, calls = _secured_app()
    with TestClient(app, base_url="http://127.0.0.1:8001") as client:
        csrf = _bootstrap(client)
        response = client.post(
            "/api/write",
            headers={CSRF_HEADER: csrf, "Origin": "https://evil.example"},
        )

    assert response.status_code == 403
    assert response.json()["code"] == "WEB_ORIGIN_REJECTED"
    assert calls["writes"] == 0


def test_untrusted_and_synthetic_test_hosts_are_rejected_before_routes():
    app, _ = _secured_app()
    for base_url in ("http://evil.example:8001", "http://testserver:8001"):
        with TestClient(app, base_url=base_url) as client:
            response = client.get("/")

        assert response.status_code == 400
        assert response.json()["code"] == "WEB_HOST_REJECTED"
        assert not response.cookies


def test_production_launchers_are_loopback_only_and_cors_wildcard_is_removed():
    main = (ROOT / "backend" / "main.py").read_text(encoding="utf-8")
    win = (ROOT / "ancserTPX web win.bat").read_text(encoding="utf-8")
    mac = (ROOT / "ancserTPX web mac.command").read_text(encoding="utf-8")

    for source in (main, win, mac):
        assert "0.0.0.0" not in source
        assert "127.0.0.1" in source
    assert "CORSMiddleware" not in main
    assert 'allow_origins=["*"]' not in main


def test_frontend_adds_csrf_header_to_same_origin_mutations():
    js = (ROOT / "frontend" / "static" / "ancserTPX.js").read_text(
        encoding="utf-8",
    )

    assert "X-AncserTPX-CSRF" in js
    assert "ancsertpx_csrf_" in js
    assert "window.fetch" in js


def test_production_api_docs_are_disabled_by_default():
    from backend.main import app

    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None
