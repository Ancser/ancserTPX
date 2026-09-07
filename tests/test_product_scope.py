"""Removed product features stay absent while supported preset CRUD remains."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "frontend" / "static" / "ancserTPX.html").read_text(encoding="utf-8")
JS = (ROOT / "frontend" / "static" / "ancserTPX.js").read_text(encoding="utf-8")
ROUTES = (ROOT / "backend" / "api" / "routes.py").read_text(encoding="utf-8")


def test_cross_model_sweep_is_completely_removed():
    assert not (ROOT / "backend" / "backtest" / "sweep.py").exists()
    assert not list((ROOT / "scripts").glob("*sweep*.py"))
    assert not [
        path
        for root in (ROOT / "backend", ROOT / "scripts")
        for path in root.rglob("*sweep*")
        if path.is_file()
    ]
    for token in (
        "btn-sweep",
        "sweep-model",
        "runBacktestSweep",
        "renderSweepTable",
        "loadSweepResults",
        "saveSweepPreset",
        "sweep_models",
    ):
        assert token not in HTML
        assert token not in JS
    assert "/backtest/sweep" not in ROUTES


def test_normal_preset_crud_controls_remain_available():
    assert 'id="preset-bt"' in HTML
    assert 'id="preset-live"' in HTML
    assert "savePreset(" in JS
    assert "deletePreset(" in JS
