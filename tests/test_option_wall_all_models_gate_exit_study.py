import numpy as np
import pandas as pd

from scripts.option_wall_all_models_gate_exit_study import (
    _concentration,
    _proximity_target,
)


def test_proximity_target_moves_toward_entry_for_both_directions():
    assert _proximity_target(100.0, 110.0, 20.0, 5.0) == 107.5
    assert _proximity_target(100.0, 90.0, -20.0, 5.0) == 92.5


def test_proximity_target_rejects_wall_inside_buffer():
    assert _proximity_target(100.0, 101.0, 5.0, 5.0) is None
    assert _proximity_target(100.0, 101.0, 4.0, 5.0) is None


def test_concentration_preserves_same_day_trade_clustering():
    frame = pd.DataFrame({"date": ["2026-01-02", "2026-01-02", "2026-01-03"]})
    result = _concentration(frame, np.asarray([10.0, -4.0, 5.0]))
    assert result["largest_trade_pnl"] == 10.0
    assert result["largest_session_pnl"] == 6.0
    assert result["net_without_largest_trade"] == 1.0
    assert result["net_without_largest_session"] == 5.0
    assert result["multi_trade_sessions"] == 1
