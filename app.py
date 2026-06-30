"""
ancserTPX desktop launcher — native window, no Chrome.

Starts the existing FastAPI/uvicorn server (backend.main:app) in a background
thread and opens it in a native OS window via pywebview (Edge WebView2 on
Windows 11 — no Google Chrome required). Closing the window stops the server.

Run directly during development:
    python app.py
Double-click target: "ancserTPX app win.bat" (handles deps + kills old instances).
Frozen build: PyInstaller bundles this file; see ancserTPX.spec.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

# Where user data lives (data/, .env). In a frozen build this is the folder the
# .exe sits in — so data stays EXTERNAL and editable without repackaging. In dev
# it's the project root next to this file.
if getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(sys.executable).resolve().parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parent
os.chdir(PROJECT_ROOT)
# In dev, the project root must be importable so "backend.main:app" resolves.
# In a frozen build the backend package is bundled, so no path insert is needed.
if not getattr(sys, "frozen", False) and str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _redirect_frozen_output() -> None:
    """In a windowed (console=False) PyInstaller build, sys.stdout/sys.stderr are
    None. uvicorn and the backend log to those streams — writing to None raises,
    which can break logging mid-request and leaves us with zero diagnostics when
    something like /live/start fails ("Failed to fetch" on the frontend). Point
    both streams at a log file beside the exe so the app is debuggable and so
    backend logging never blows up on a missing stream.
    """
    if not getattr(sys, "frozen", False):
        return
    log_path = PROJECT_ROOT / "ancserTPX.log"
    try:
        f = open(log_path, "a", encoding="utf-8", buffering=1)
    except Exception:
        return
    sys.stdout = f
    sys.stderr = f


_redirect_frozen_output()

HOST = "127.0.0.1"
PORT_RANGE = range(8001, 8011)
WINDOW_TITLE = "ancserTPX"


def find_free_port() -> int:
    for port in PORT_RANGE:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((HOST, port))
                return port
            except OSError:
                continue
    raise SystemExit(f"No free port in {PORT_RANGE.start}-{PORT_RANGE.stop - 1}")


def kill_old_instances() -> None:
    """Prevent dual-engine trading: kill any prior ancserTPX server before we
    bind. We have not bound yet, so nothing of ours is listening — this only
    targets a *previous* instance.

    Targets (best-effort, Windows):
      - python processes running uvicorn / terminal_live  (the old web/terminal)
      - anything LISTENING on ports 8000-8010              (any prior server)
    Deliberately does NOT blanket-kill ancserTPX.exe: in a PyInstaller build the
    current app is itself ancserTPX.exe (bootstrap + child share the name), so
    name-based killing could take down our own process. Port-based killing is
    safe because a healthy prior instance is always listening.
    """
    if os.name != "nt":
        return
    self_pid = os.getpid()
    ps = (
        "$self={pid};"
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" -ErrorAction SilentlyContinue |"
        " Where-Object {{ $_.ProcessId -ne $self -and ($_.CommandLine -match 'uvicorn' -or $_.CommandLine -match 'terminal_live') }} |"
        " ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }};"
        "8000..8010 | ForEach-Object {{"
        " Get-NetTCPConnection -LocalPort $_ -State Listen -ErrorAction SilentlyContinue |"
        " Select-Object -ExpandProperty OwningProcess -Unique |"
        " Where-Object {{ $_ -ne 0 -and $_ -ne $self }} |"
        " ForEach-Object {{ Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }} }}"
    ).format(pid=self_pid)
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass


def reset_zone_cache() -> None:
    """Match the .bat: clear stale live zones so warm-up starts from a safe state."""
    zone_file = PROJECT_ROOT / "data" / "live_zones.json"
    if zone_file.exists():
        zone_file.write_text(
            json.dumps({"saved_at": "", "active_zone_id": None, "zones": []}),
            encoding="utf-8",
        )


def wait_until_ready(port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex((HOST, port)) == 0:
                return True
        time.sleep(0.2)
    return False


def main() -> None:
    import uvicorn
    import webview  # pywebview

    kill_old_instances()
    reset_zone_cache()
    port = find_free_port()
    url = f"http://{HOST}:{port}"

    # Import the ASGI app as an OBJECT, not the "backend.main:app" string.
    # PyInstaller statically analyses this entry script: a real import gets the
    # whole backend package traced and bundled, whereas a string is opaque to it
    # (collect_submodules in the spec runs in an isolated subprocess without the
    # project root on sys.path, so it bundled nothing → "No module named
    # 'backend'" in the frozen build). Passing the object also skips uvicorn's own
    # import_from_string, which can't resolve "backend" inside the frozen bundle.
    # Imported here (after reset) to keep the original lazy load timing.
    from backend.main import app as asgi_app

    config = uvicorn.Config(
        asgi_app,
        host=HOST,
        port=port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)

    # uvicorn must run off the main thread — pywebview needs the main thread.
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    if not wait_until_ready(port):
        server.should_exit = True
        raise SystemExit("Server failed to start within timeout")

    webview.create_window(WINDOW_TITLE, url, width=1480, height=920, min_size=(1024, 700))
    # Blocks until the window is closed.
    webview.start()

    # Window closed → stop the server, then force-exit. WebView2 / pythonnet
    # leave non-daemon helper threads (and msedgewebview2 children) alive, so a
    # plain return hangs the process — a zombie that blocks the next launch and
    # the WebView2 data-dir lock. os._exit terminates immediately and reliably.
    server.should_exit = True
    server_thread.join(timeout=3)
    os._exit(0)


if __name__ == "__main__":
    main()
