"""Windows launcher safety contracts."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_terminal_cleanup_does_not_target_desktop_app_port():
    script = (ROOT / "backend" / "stop_legacy_instances.ps1").read_text(encoding="utf-8")
    launcher = (ROOT / "windows terminal.bat").read_text(encoding="utf-8")

    assert "stop_legacy_instances.ps1" in launcher
    assert "kill_old.ps1" not in launcher
    assert "backend\\.terminal_live|terminal_live\\.py" in script
    assert "backend\\.main:app|(?:-m\\s+)?backend\\.main\\b" in script
    assert "webPortPattern" in script
    assert "legacyWebBatchParentIds" in script
    assert "ParentProcessId" in script
    assert "cmd.exe" in script
    assert "Get-NetTCPConnection" not in script
    assert "8000..8010" not in script
    assert "pause" not in launcher.lower()


def test_windows_app_shortcut_uses_the_website_favicon():
    installer = (ROOT / "windows install.bat").read_text(encoding="utf-8")
    shortcut = (ROOT / "scripts" / "create_windows_app_shortcut.ps1").read_text(encoding="utf-8")

    assert "create_windows_app_shortcut.ps1" in installer
    assert "ancserTPX desktop shortcut" in installer
    assert '"ancserTPX.lnk"' in shortcut
    assert '"frontend\\static\\favicon.ico"' in shortcut
    assert '$shortcut.IconLocation = "$icon,0"' in shortcut
    assert '"windows app.vbs"' in shortcut


def test_launchers_opt_in_to_stopping_the_terminal_explicitly():
    """1.5.1: only the terminal launcher may replace a running terminal."""
    vbs = (ROOT / "windows app.vbs").read_text(encoding="utf-8")
    bat = (ROOT / "windows terminal.bat").read_text(encoding="utf-8")
    assert "stop_legacy_instances.ps1" in vbs
    assert "-IncludeTerminal" not in vbs
    assert 'stop_legacy_instances.ps1" -IncludeTerminal' in bat


import os  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402

import pytest  # noqa: E402


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher script")
def test_app_cleanup_leaves_a_live_terminal_running():
    """Run the real cleanup script (-DryRun) against real processes.

    2026-11-05 01:38 and 2026-11-06 00:34: opening the App force-killed the
    live terminal (no flatten, no clean stop) because the cleanup matched
    `backend.terminal_live` unconditionally.
    """
    script = str(ROOT / "backend" / "stop_legacy_instances.ps1")
    sleeper = "import time; time.sleep(120)"
    terminal = subprocess.Popen([sys.executable, "-c", sleeper, "backend.terminal_live"])
    web = subprocess.Popen([sys.executable, "-c", sleeper, "backend.main:app", "--port", "8001"])
    try:
        def dry(*extra: str) -> str:
            return subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", script, "-DryRun", *extra],
                capture_output=True, text=True, timeout=120, check=True,
            ).stdout

        app_launch = dry()
        terminal_launch = dry("-IncludeTerminal")
        # Positive controls: the patterns do see both stand-ins.
        assert f"WOULD-STOP {terminal.pid}" in terminal_launch
        assert f"WOULD-STOP {web.pid}" in app_launch
        # The App path leaves the terminal alone.
        assert f"WOULD-STOP {terminal.pid}" not in app_launch
        # -DryRun stopped nothing.
        assert terminal.poll() is None and web.poll() is None
    finally:
        terminal.kill()
        web.kill()


def test_macos_web_launcher_leaves_the_terminal_running():
    web = (ROOT / "macOS web.command").read_text(encoding="utf-8")
    terminal = (ROOT / "macOS terminal.command").read_text(encoding="utf-8")
    code = "\n".join(l for l in web.splitlines() if not l.lstrip().startswith("#"))
    assert 'pkill -f "uvicorn backend.main:app"' in code          # positive: web cleanup kept
    assert "terminal_live" not in code
    assert 'pkill -f "backend.terminal_live"' in terminal          # the terminal launcher still replaces
