"""Single-process Windows desktop launcher for the ancserTPX control plane.

The API server and the native WebView2 window are owned by this one process.
Closing the window therefore follows the same shutdown path as stopping the
server, instead of leaving a browser tab or a second command window behind.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.error import URLError
from urllib.request import Request, urlopen


APP_HOST = "127.0.0.1"
APP_PORT = 8001
APP_TITLE = "ancserTPX"
APP_ICON_PATH = Path(__file__).resolve().parents[1] / "frontend" / "static" / "favicon.ico"
APP_WIDTH = 1440
APP_HEIGHT = 900
APP_MIN_SIZE = (1024, 700)
STARTUP_TIMEOUT_SECONDS = 30.0
SHUTDOWN_TIMEOUT_SECONDS = 15.0
HEALTH_POLL_SECONDS = 0.1

logger = logging.getLogger(__name__)


class DesktopAppError(RuntimeError):
    """A user-actionable desktop launcher failure."""


def runtime_directory() -> Path:
    """Return private runtime storage outside the repository."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA", "").strip()
    else:
        base = os.environ.get("XDG_STATE_HOME", "").strip()
    return Path(base or tempfile.gettempdir()) / "ancserTPX"


def application_url(host: str = APP_HOST, port: int = APP_PORT) -> str:
    return f"http://{host}:{int(port)}/"


class SingleInstanceLock:
    """Hold an OS lock so a second desktop window cannot start another API."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._handle = None
        self._locked = False

    @property
    def held(self) -> bool:
        return self._locked

    def acquire(self) -> bool:
        if self._locked:
            return True

        handle = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)

            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            self._handle = handle
            self._locked = True
            self._write_metadata()
            return True
        except (OSError, ValueError):
            if handle is not None:
                try:
                    handle.close()
                except (OSError, ValueError):
                    pass
            self._handle = None
            self._locked = False
            return False

    def _write_metadata(self) -> None:
        handle = self._handle
        if not self._locked or handle is None:
            return
        try:
            payload = json.dumps(
                {"pid": os.getpid(), "started_at": time.time()},
                separators=(",", ":"),
            ).encode("utf-8")
            handle.seek(0)
            handle.truncate()
            handle.write(payload)
            handle.flush()
        except (OSError, ValueError):
            logger.debug("Could not write desktop instance metadata", exc_info=True)

    def release(self) -> None:
        handle = self._handle
        was_locked = self._locked
        self._handle = None
        self._locked = False
        if handle is None:
            return
        try:
            handle.seek(0)
            if was_locked:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (OSError, ValueError):
            pass
        finally:
            try:
                handle.close()
            except (OSError, ValueError):
                pass

    def __enter__(self) -> "SingleInstanceLock":
        if not self.acquire():
            raise DesktopAppError("ancserTPX is already running")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def port_is_available(host: str = APP_HOST, port: int = APP_PORT) -> bool:
    """Check the exact loopback port before starting a new server."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def wait_for_backend(
    server_thread: threading.Thread,
    *,
    host: str = APP_HOST,
    port: int = APP_PORT,
    timeout: float = STARTUP_TIMEOUT_SECONDS,
    opener: Callable[..., Any] = urlopen,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Wait until this process's API answers the local health endpoint."""
    health_url = application_url(host, port) + "api/health"
    deadline = clock() + max(0.0, float(timeout))
    last_error: Exception | None = None

    while True:
        if not server_thread.is_alive():
            raise DesktopAppError("The ancserTPX backend stopped during startup")
        try:
            request = Request(health_url, headers={"Cache-Control": "no-cache"})
            with opener(request, timeout=0.75) as response:
                status = getattr(response, "status", None)
                if status is None:
                    status = response.getcode()
                payload = json.loads(response.read().decode("utf-8"))
                if (
                    status == 200
                    and payload.get("status") == "ok"
                    and payload.get("service") == APP_TITLE
                ):
                    return
        except (OSError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc

        remaining = deadline - clock()
        if remaining <= 0:
            detail = f" ({type(last_error).__name__})" if last_error else ""
            raise DesktopAppError(
                f"The ancserTPX backend did not become ready{detail}"
            )
        sleeper(min(HEALTH_POLL_SECONDS, remaining))


def start_backend() -> tuple[Any, threading.Thread, list[BaseException]]:
    """Start Uvicorn in a daemon worker owned by this launcher process."""
    import uvicorn

    from backend.main import app

    config = uvicorn.Config(
        app,
        host=APP_HOST,
        port=APP_PORT,
        lifespan="on",
        log_level="info",
        access_log=False,
        # pythonw.exe has no stdout/stderr. Uvicorn's default formatter calls
        # sys.stdout.isatty(), which crashes before the native window opens.
        # backend.main / _configure_file_logging own the hidden-app logs.
        log_config=None,
    )
    server = uvicorn.Server(config)
    errors: list[BaseException] = []

    def run() -> None:
        try:
            server.run()
        except BaseException as exc:  # the GUI thread needs a visible failure
            errors.append(exc)
            logger.exception("Desktop backend stopped unexpectedly")

    thread = threading.Thread(
        target=run,
        name="ancserTPX-api",
        daemon=True,
    )
    thread.start()
    return server, thread, errors


def stop_backend(server: Any, server_thread: threading.Thread) -> None:
    """Request graceful Uvicorn/lifespan shutdown, then use its force flag."""
    server.should_exit = True
    server_thread.join(timeout=SHUTDOWN_TIMEOUT_SECONDS)
    if server_thread.is_alive():
        logger.warning("Backend did not stop within the graceful shutdown window")
        server.force_exit = True
        server_thread.join(timeout=2.0)


def show_error(message: str) -> None:
    """Show startup failures without requiring a console window."""
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(0, message, APP_TITLE, 0x10)
            return
        except (AttributeError, OSError):
            pass
    try:
        print(message)
    except (OSError, RuntimeError):
        pass


def _configure_file_logging(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    if any(isinstance(handler, logging.FileHandler) for handler in root_logger.handlers):
        return
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s: %(message)s"))
    root_logger.addHandler(handler)


def run_desktop_app() -> int:
    """Run the native app until its one window is closed."""
    runtime = runtime_directory()
    lock = SingleInstanceLock(runtime / "desktop-app.lock")
    if not lock.acquire():
        logger.info("ancserTPX desktop app is already running")
        return 0

    server = None
    server_thread: threading.Thread | None = None
    try:
        _configure_file_logging(runtime / "desktop-app.log")
        if not port_is_available():
            raise DesktopAppError(
                f"Port {APP_PORT} is still in use by another local process. "
                "Close that process, then open ancserTPX again."
            )

        server, server_thread, _errors = start_backend()
        wait_for_backend(server_thread)

        try:
            import webview
        except ImportError as exc:
            raise DesktopAppError(
                "Desktop window support is missing. Run the installer again."
            ) from exc

        logger.info("Creating native WebView2 window")
        webview.create_window(
            APP_TITLE,
            application_url(),
            width=APP_WIDTH,
            height=APP_HEIGHT,
            min_size=APP_MIN_SIZE,
            resizable=True,
            confirm_close=False,
            background_color="#090c12",
            text_select=True,
            zoomable=True,
        )
        logger.info("Native WebView2 window created; starting GUI loop")
        gui = "edgechromium" if os.name == "nt" else None
        webview.start(
            gui=gui,
            debug=False,
            private_mode=False,
            storage_path=str(runtime / "webview"),
            icon=str(APP_ICON_PATH) if APP_ICON_PATH.is_file() else None,
        )
        logger.info("Native WebView2 GUI loop ended")
        return 0
    except DesktopAppError as exc:
        logger.error("Desktop app could not start: %s", exc)
        show_error(str(exc))
        return 1
    except Exception as exc:
        logger.exception("Desktop app failed")
        show_error(f"ancserTPX could not start:\n{type(exc).__name__}: {exc}")
        return 1
    finally:
        logger.info("Stopping desktop backend and releasing instance lock")
        if server is not None and server_thread is not None:
            stop_backend(server, server_thread)
        lock.release()


def main() -> None:
    raise SystemExit(run_desktop_app())


if __name__ == "__main__":
    main()
