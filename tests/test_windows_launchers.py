"""Windows launcher safety contracts."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_terminal_cleanup_does_not_target_desktop_app_port():
    script = (ROOT / "backend" / "stop_legacy_instances.ps1").read_text(encoding="utf-8")
    launcher = (ROOT / "ancserTPX terminal win.bat").read_text(encoding="utf-8")

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
