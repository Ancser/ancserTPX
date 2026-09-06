import pandas as pd

from scripts.option_wall_intrahour_oi_study import (
    _conditional_exit,
    _intrahour_gamma_state,
    _rule_exit_required,
)


def test_intrahour_gamma_state_requires_sign_and_flip_side():
    assert _intrahour_gamma_state(2.0, -1.0) == 1
    assert _intrahour_gamma_state(-2.0, 1.0) == -1
    assert _intrahour_gamma_state(2.0, 1.0) == 0


def test_any_structure_failure_combines_wall_move_room_and_gamma():
    base = pd.Series({
        "direction": 1, "entry_oi_gamma_state": 1,
        "snapshot_oi_gamma_state": 1, "snapshot_peak_direction": 1,
        "oriented_target_move_bps": -6.0, "target_still_beyond_spot": True,
    })
    assert _rule_exit_required(base, "exit_on_any_structure_failure") is True
    base["oriented_target_move_bps"] = 2.0
    assert _rule_exit_required(base, "exit_on_any_structure_failure") is False


def test_conditional_exit_respects_stop_before_decision():
    timestamps = pd.to_datetime([
        "2026-01-02T15:00Z", "2026-01-02T15:01Z", "2026-01-02T15:02Z",
    ], utc=True)
    path = pd.DataFrame({
        "ts": timestamps,
        "open": [100.0, 99.0, 98.0], "high": [101.0, 100.0, 99.0],
        "low": [99.0, 94.0, 97.0], "close": [99.5, 95.0, 98.5],
    })
    result = _conditional_exit(
        path, 1, 100.0, 95.0, pd.Timestamp("2026-01-02T15:02Z"), True,
    )
    assert result["exit_reason"] == "sl"
    assert result["decision_exit"] is False
