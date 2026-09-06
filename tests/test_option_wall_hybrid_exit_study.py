import pandas as pd

from scripts.option_wall_hybrid_exit_study import simulate_wall_transition


def _path(rows):
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"])


def test_same_bar_stop_and_wall_resolves_to_stop_conservatively():
    result = simulate_wall_transition(
        _path([[100.0, 111.0, 94.0, 105.0]]), 1, 100.0, 95.0, 110.0,
        2.0, "breakeven",
    )
    assert result["exit_reason"] == "sl_before_wall"
    assert result["exit_price"] == 95.0


def test_breakeven_activates_after_wall_bar_then_protects_retrace():
    result = simulate_wall_transition(
        _path([
            [100.0, 111.0, 99.0, 109.0],
            [109.0, 109.5, 99.5, 100.5],
        ]),
        1, 100.0, 95.0, 110.0, 2.0, "breakeven",
    )
    assert result["wall_touched"] is True
    assert result["exit_reason"] == "sl_after_wall"
    assert result["exit_price"] == 100.0


def test_full_wall_action_exits_at_exact_target():
    result = simulate_wall_transition(
        _path([[100.0, 110.5, 99.0, 109.0]]),
        1, 100.0, 95.0, 110.0, 2.0, "full",
    )
    assert result["exit_reason"] == "wall_tp"
    assert result["exit_price"] == 110.0
