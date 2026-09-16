import json
from pathlib import Path

from scripts.volume_profile_research import _select_training_candidate, candidates
from backend.terminal_live import _build_strategy_params, _load_presets_file


def test_volume_profile_research_matrix_covers_each_state_machine_mode():
    matrix = candidates()
    assert len(matrix) == 12
    assert {row["entry_mode"] for row in matrix} == {
        "auto", "range", "breakout", "failed_break"
    }
    assert all(row["value_area_pct"] == 0.70 for row in matrix)


def test_research_fallback_never_selects_a_zero_trade_candidate():
    empty = {
        "training_eligible": False,
        "ranking_score": [0, 0, 99, 99, 99],
        "baseline": {"baseline": {"n": 0}},
    }
    nonempty = {
        "training_eligible": False,
        "ranking_score": [0, -1, 0.8, 0.9, -100],
        "baseline": {"baseline": {"n": 40}},
    }
    assert _select_training_candidate([empty, nonempty]) is nonempty


def test_volume_profile_research_preset_round_trips_through_terminal_builder():
    root = Path(__file__).resolve().parents[1]
    document = json.loads((root / "data" / "presets.json").read_text(encoding="utf-8"))
    preset = document["presets"]["VOLUME PROFILE RESEARCH"]
    params = _build_strategy_params(preset, "CON.F.US.MNQ.Z26")
    assert params.strategy == "volume_profile"
    assert params.tr_allowed_sessions == ["RTH"]
    assert params.vp_entry_mode == "breakout"
    assert params.vp_target_mode == "atr"
    assert params.vp_sl_atr == 1.5
    assert params.vp_tp_atr == 2.0
    loaded = _load_presets_file()["presets"]["VOLUME PROFILE RESEARCH"]
    assert loaded["strategy"] == "volume_profile"
    assert loaded["tr_allowed_sessions"] == ["RTH"]
