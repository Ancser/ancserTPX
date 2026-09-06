import numpy as np
import pandas as pd

from scripts.option_wall_gamma_gate_study import (
    _entry_session_block_bootstrap,
    _gamma_state,
    _gate_masks,
)


def test_gamma_state_requires_net_sign_and_flip_side_to_agree():
    state = _gamma_state(
        pd.Series([2.0, -2.0, 2.0, -2.0, np.nan]),
        pd.Series([-1.0, 1.0, 1.0, -1.0, -1.0]),
    )
    assert state.tolist() == [1, -1, 0, 0, 0]


def _gate_frame():
    return pd.DataFrame({
        "direction": [1, 1, -1, -1],
        "oi_gamma_state": [1, 1, -1, 1],
        "volume_gamma_state": [1, 1, -1, -1],
        "article_price_vwap_distance_bps": [-5.0, 5.0, 5.0, -5.0],
        "article_price_return_15m_bps": [2.0, 2.0, -3.0, 3.0],
        "dashboard_vol_call_wall_bps": [10.0, 10.0, 10.0, 10.0],
        "dashboard_vol_put_wall_bps": [-10.0, -10.0, -10.0, -10.0],
    })


def test_article_alignment_uses_mean_reversion_in_positive_gamma_and_momentum_in_negative():
    gates = _gate_masks(_gate_frame())
    assert gates["volume_article_alignment"].tolist() == [True, False, True, False]


def test_consensus_gate_rejects_oi_volume_disagreement():
    gates = _gate_masks(_gate_frame())
    assert gates["consensus_article_alignment"].tolist() == [True, False, True, False]
    assert gates["oi_volume_regime_consensus"].tolist() == [True, True, True, False]


def test_directional_gate_is_distinct_from_article_physics_gate():
    gates = _gate_masks(_gate_frame())
    assert gates["volume_directional"].tolist() == [True, True, True, True]
    assert not np.array_equal(
        gates["volume_directional"], gates["volume_article_alignment"],
    )


def test_session_bootstrap_keeps_rejected_entry_sessions_as_zero_pnl():
    frame = pd.DataFrame({"date": ["2026-01-02", "2026-01-02", "2026-01-03"]})
    result = _entry_session_block_bootstrap(
        frame,
        np.asarray([10.0, -2.0, -50.0]),
        np.asarray([True, True, False]),
        np.asarray([True, True, True]),
        draws=100,
        seed=7,
    )
    assert result["base_entry_sessions"] == 2
    assert result["active_sessions"] == 1
    assert result["net_pnl_without_largest_session"] == 0.0
