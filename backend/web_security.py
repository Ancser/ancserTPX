"""Local-browser security boundary for the ancserTPX control plane.

The Web UI and API are intentionally a same-origin, loopback-only application.
This middleware prevents a random Web page from driving state-changing API
routes in the user's browser and rejects DNS-rebinding/unexpected Host headers.
It is not an account-type gate: Main/Express live trading remains supported.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import hmac
import logging
import secrets
from typing import Final
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse


logger = logging.getLogger(__name__)

CSRF_HEADER: Final[str] = "X-AncserTPX-CSRF"
_SESSION_COOKIE_PREFIX: Final[str] = "ancsertpx_session_"
_CSRF_COOKIE_PREFIX: Final[str] = "ancsertpx_csrf_"
_MUTATING_METHODS: Final[frozenset[str]] = frozenset(
    {"POST", "PUT", "PATCH", "DELETE"}
)
_ALLOWED_HOSTS: Final[frozenset[str]] = frozenset(
    {"127.0.0.1", "localhost", "::1"}
)

# Rotated on every backend process start. The HttpOnly session value proves the
# browser first visited this process; the readable CSRF value must also be
# copied into a custom header, which cross-origin pages cannot do.
_SESSION_SECRET = secrets.token_urlsafe(32)
_CSRF_SECRET = secrets.token_urlsafe(32)


def session_cookie_name(port: int) -> str:
    """Port-scoped name so parallel local dev servers cannot clobber sessions."""
    return f"{_SESSION_COOKIE_PREFIX}{int(port)}"


def csrf_cookie_name(port: int) -> str:
    """Readable double-submit token paired with the HttpOnly session cookie."""
    return f"{_CSRF_COOKIE_PREFIX}{int(port)}"


def _request_port(request: Request) -> int:
    if request.url.port is not None:
        return int(request.url.port)
    return 443 if request.url.scheme == "https" else 80


def _request_host(request: Request) -> str:
    host_header = str(request.headers.get("host") or "").strip()
    if not host_header:
        return ""
    try:
        return str(urlsplit(f"//{host_header}").hostname or "").lower()
    except ValueError:
        return ""


def _origin_matches_request(request: Request) -> bool:
    """Accept absent Origin (non-browser client) or the request's exact origin."""
    origin = str(request.headers.get("origin") or "").strip()
    if not origin:
        return True
    try:
        parsed = urlsplit(origin)
        origin_host = str(parsed.hostname or "").lower()
        origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return False
    return (
        parsed.scheme == request.url.scheme
        and origin_host == _request_host(request)
        and origin_port == _request_port(request)
    )


def _secure_equals(left: str | None, right: str) -> bool:
    return bool(left) and hmac.compare_digest(str(left), right)


def _apply_security_headers(response: Response, path: str) -> None:
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    )
    response.headers["Content-Security-Policy"] = (
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    response.headers["X-AncserTPX-Security"] = "loopback-same-origin"
    if path == "/" or path.startswith("/api/") or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"


def _deny(
    status: int,
    code: str,
    detail: str,
    path: str,
    request: Request,
) -> Response:
    client = request.client.host if request.client is not None else "unknown"
    logger.warning(
        "Web security rejected request: code=%s status=%s method=%s path=%s client=%s",
        code,
        status,
        request.method,
        path,
        client,
    )
    response = JSONResponse(
        status_code=status,
        content={"detail": detail, "code": code},
    )
    _apply_security_headers(response, path)
    return response


async def _local_web_security(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Enforce local Host and same-origin CSRF on mutating API requests."""
    path = request.url.path
    host = _request_host(request)
    if host not in _ALLOWED_HOSTS:
        return _deny(
            400,
            "WEB_HOST_REJECTED",
            "Web control accepts localhost only",
            path,
            request,
        )

    if path.startswith("/api/") and request.method.upper() in _MUTATING_METHODS:
        if not _origin_matches_request(request):
            return _deny(
                403,
                "WEB_ORIGIN_REJECTED",
                "Cross-origin Web control request rejected",
                path,
                request,
            )

        port = _request_port(request)
        session_ok = _secure_equals(
            request.cookies.get(session_cookie_name(port)),
            _SESSION_SECRET,
        )
        csrf_cookie_ok = _secure_equals(
            request.cookies.get(csrf_cookie_name(port)),
            _CSRF_SECRET,
        )
        csrf_header_ok = _secure_equals(
            request.headers.get(CSRF_HEADER),
            _CSRF_SECRET,
        )
        if not (session_ok and csrf_cookie_ok and csrf_header_ok):
            return _deny(
                403,
                "WEB_CSRF_REQUIRED",
                "Refresh the local ancserTPX page before sending control commands",
                path,
                request,
            )

    response = await call_next(request)
    _apply_security_headers(response, path)

    # Any successful local GET repairs cookies after a backend restart.
    if request.method.upper() == "GET" and response.status_code < 500:
        port = _request_port(request)
        secure = request.url.scheme == "https"
        if not _secure_equals(
            request.cookies.get(session_cookie_name(port)),
            _SESSION_SECRET,
        ):
            response.set_cookie(
                session_cookie_name(port),
                _SESSION_SECRET,
                httponly=True,
                secure=secure,
                samesite="strict",
                path="/",
            )
        if not _secure_equals(
            request.cookies.get(csrf_cookie_name(port)),
            _CSRF_SECRET,
        ):
            response.set_cookie(
                csrf_cookie_name(port),
                _CSRF_SECRET,
                httponly=False,
                secure=secure,
                samesite="strict",
                path="/",
            )
    return response


def install_local_web_security(app: FastAPI) -> None:
    """Install the single local-control security middleware."""
    app.middleware("http")(_local_web_security)
