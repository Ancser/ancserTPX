"""The SWEEP MODEL dropdown must not offer a model the backend cannot run.

1.0.10p: `TREND ONLY` sat in the dropdown while `run_model_sweep()` only ever
dispatched DAY ZONE / DISTRIBUTION / FACTOR.  Selecting it sent
`sweep_models=['TREND']`, every `_on()` returned False, and the sweep finished
with zero results — no error, no warning, just an empty PRESETS tab after a
5-25 minute wait.  Nothing failed, so nothing caught it.

This binds the three places that have to agree:

    frontend SWEEP_MODEL_ORDER  ==  the dropdown's data-sweep-model switches
                                ==  the names run_model_sweep() branches on
"""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "frontend" / "static" / "ancserTPX.html").read_text(encoding="utf-8")
JS = (ROOT / "frontend" / "static" / "ancserTPX.js").read_text(encoding="utf-8")
SWEEP_PY = (ROOT / "backend" / "backtest" / "sweep.py").read_text(encoding="utf-8")


def _backend_dispatched_models() -> set[str]:
    """Names run_model_sweep() actually branches on, via `if _on("NAME")`."""
    start = SWEEP_PY.index("def run_model_sweep(")
    body = SWEEP_PY[start:SWEEP_PY.index("\ndef ", start + 1)]
    found = set(re.findall(r'_on\("([^"]+)"\)', body))
    # Positive assertion: if the regex or the function shape ever drifts, this
    # returning an empty set must fail here rather than silently making every
    # comparison below trivially pass.
    assert found, "found no _on(...) dispatch in run_model_sweep — parser drifted"
    return found


def _frontend_order() -> list[str]:
    match = re.search(
        r"const SWEEP_MODEL_ORDER = Object\.freeze\(\[(.*?)\]\)", JS, re.DOTALL)
    assert match, "SWEEP_MODEL_ORDER not found in ancserTPX.js"
    models = re.findall(r"'([^']+)'", match.group(1))
    assert models, "SWEEP_MODEL_ORDER parsed empty"
    return models


def _dropdown_models() -> list[str]:
    # Anchor on the surrounding row, not on any one model's label: naming the
    # last model as the end marker makes this silently truncate the moment a
    # model is appended after it.
    start = HTML.index('<div class="sweep-action-row"')
    end = HTML.index('<div id="backtest-progress-wrap"', start)
    models = re.findall(r'data-sweep-model="([^"]+)"', HTML[start:end])
    assert models, "no data-sweep-model switches found in the dropdown"
    return models


def test_every_dropdown_model_has_a_backend_sweep():
    dispatched = _backend_dispatched_models()
    offered = [m for m in _dropdown_models() if m != "ALL"]
    dead = [m for m in offered if m not in dispatched]
    assert not dead, (
        f"dropdown offers {dead} but run_model_sweep() never dispatches it; "
        f"selecting it returns zero results. Backend dispatches: {sorted(dispatched)}"
    )


def test_frontend_order_matches_the_dropdown_and_the_backend():
    order = _frontend_order()
    offered = [m for m in _dropdown_models() if m != "ALL"]
    assert sorted(order) == sorted(offered), (
        "SWEEP_MODEL_ORDER and the dropdown switches disagree; "
        "_sweepModelSelection() filters through SWEEP_MODEL_ORDER, so a model "
        "present only in the markup is silently dropped from the request"
    )
    assert sorted(order) == sorted(_backend_dispatched_models())


def test_all_selected_sends_no_scope_rather_than_a_hardcoded_count():
    """`ALL` must be expressed as "omit sweep_models", not "length < 4".

    The magic number silently desynchronises the moment the model list changes:
    with 3 models, `< 4` is true for a full selection, so ALL would start
    sending a partial scope instead of None.
    """
    start = JS.index("async function runBacktestSweep(")
    body = JS[start:JS.index("\nasync function ", start + 1)]
    assert "body.sweep_models = _mm;" in body
    assert "_mm.length < SWEEP_MODEL_ORDER.length" in body
    assert not re.search(r"_mm\.length\s*<\s*\d", body), \
        "hardcoded model count in runBacktestSweep — use SWEEP_MODEL_ORDER.length"
