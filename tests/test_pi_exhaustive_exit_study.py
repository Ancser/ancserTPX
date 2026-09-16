import json
from pathlib import Path

from backend.db.models import StrategyParams
from scripts.pi_exhaustive_exit_study import (
    CURRENT_EXIT,
    EXIT_VARIANTS,
    FEATURE_SETS,
    SIGNAL_COMBOS,
    _configure_params,
)


def test_pi_matrix_declares_requested_signal_and_exit_dimensions():
    assert len(SIGNAL_COMBOS) == 63
    assert {1, 2}.issubset(
        {
            level
            for spec in SIGNAL_COMBOS
            for level in (spec.short_levels or ())
        }
    )
    assert any(spec.mode == "fixed" for spec in EXIT_VARIANTS)
    assert any(spec.key == "atr05_rr1" for spec in EXIT_VARIANTS)
    assert any(spec.long_hold == 15 and spec.short_hold == 15 for spec in EXIT_VARIANTS)
    assert any(spec.long_hold == 30 and spec.short_hold == 30 for spec in EXIT_VARIANTS)
    assert len(FEATURE_SETS) >= 10


def test_pi_level_selector_is_carried_into_research_params():
    level_one = next(spec for spec in FEATURE_SETS if spec.key == "purple_l1")
    params = _configure_params(
        StrategyParams(strategy="pi", contract_id="CON.F.US.MNQ.Z26"),
        CURRENT_EXIT,
        level_one,
    )
    assert params.pi_short_kinds == ["紫圈"]
    assert params.pi_short_levels == [1]


def test_absorption_best_preset_has_explicit_research_defaults():
    doc = json.loads(Path("data/presets.json").read_text(encoding="utf-8"))
    preset = doc["presets"]["DELTA ABSORPTION BEST"]
    assert preset["strategy"] == "delta_absorption"
    assert preset["delta_pattern"] == "absorption"
    assert preset["delta_window"] == 5
    assert preset["delta_baseline_window"] == 30
    assert preset["factor_sl_rule"] == "atr_blend"
    assert preset["factor_sl_value"] == 4
    assert preset["factor_tp_value"] == 12
    assert preset["tr_allowed_sessions"] == ["RTH"]
