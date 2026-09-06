import pandas as pd
import pytest

from scripts.option_wall_sltp_study import (
    _active_wall_bps,
    _atr_blend_at,
    _levels_for_policy,
    _mapped_wall_price,
    _pi_atr_levels,
    _simulate_ohlc_exit,
)


def _row(**overrides):
    values = {
        "qqq_spot": 100.0,
        "mnq_entry": 20_000.0,
        "dashboard_vol_call_wall_bps": 50.0,
        "dashboard_vol_put_wall_bps": -40.0,
        "oi_call_wall_bps": 80.0,
        "oi_put_wall_bps": -70.0,
    }
    values.update(overrides)
    return pd.Series(values)


def test_active_wall_uses_volume_then_correctly_sided_oi_fallback():
    assert _active_wall_bps(_row(), 1, "target") == (50.0, "volume")
    fallback = _row(dashboard_vol_call_wall_bps=-10.0)
    assert _active_wall_bps(fallback, 1, "target") == (80.0, "oi")
    missing = _row(dashboard_vol_put_wall_bps=5.0, oi_put_wall_bps=10.0)
    assert _active_wall_bps(missing, 1, "stop") == (None, None)


def test_wall_tp_2r_places_levels_on_correct_sides():
    sl, tp, metadata = _levels_for_policy(_row(), 1, "wall_tp_2r", 1.0, 20.0)
    assert tp == 20_100.0
    assert sl == 19_950.0
    assert metadata["reward_risk"] == 2.0

    sl, tp, _ = _levels_for_policy(_row(), -1, "wall_tp_2r", 1.0, 20.0)
    assert tp == 19_920.0
    assert sl == 20_040.0


def test_non_finite_beta_cannot_create_a_synthetic_wall_fill():
    assert pd.isna(_mapped_wall_price(_row(), 50.0, float("nan")))


def test_pi_asymmetric_exit_can_isolate_stop_from_three_r_target():
    sl, tp, metadata = _levels_for_policy(
        _row(), -1, "pi_asymmetric_sl_only", 1.0, 20.0,
    )
    assert sl == 20_030.0
    assert tp is None
    assert metadata["sl_atr_multiple"] == 1.5


def test_pi_atr_levels_size_sl_and_tp_independently_by_direction():
    assert _pi_atr_levels(20_000.0, 1, 10.0, 2.0, 6.0) == (19_980.0, 20_060.0)
    assert _pi_atr_levels(20_000.0, -1, 10.0, 1.0, None) == (20_010.0, None)


def test_same_bar_equal_distance_uses_conservative_sl_tie_break():
    path = pd.DataFrame([{
        "open": 100.0, "high": 102.0, "low": 98.0, "close": 101.0,
    }])
    outcome = _simulate_ohlc_exit(path, 1, 100.0, 99.0, 101.0)
    assert outcome["exit_reason"] == "sl"
    assert outcome["exit_price"] == 99.0


def test_stop_gap_gets_worse_open_fill():
    path = pd.DataFrame([
        {"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0},
        {"open": 97.0, "high": 98.0, "low": 96.0, "close": 97.5},
    ])
    outcome = _simulate_ohlc_exit(path, 1, 100.0, 99.0, 105.0)
    assert outcome["exit_reason"] == "sl"
    assert outcome["exit_price"] == 97.0


def test_atr_blend_reads_only_completed_five_minute_bars():
    start = pd.Timestamp("2026-09-01T12:00:00Z")
    bars = pd.DataFrame({
        "high": [101.0] * 30,
        "low": [99.0] * 30,
        "close": [100.0] * 30,
        "available_at": [start + pd.Timedelta(minutes=5 * (i + 1)) for i in range(30)],
    })
    assert _atr_blend_at(bars, start + pd.Timedelta(minutes=150)) == pytest.approx(2.0)
    bars.loc[29, ["high", "low"]] = [200.0, 0.0]
    assert _atr_blend_at(bars, start + pd.Timedelta(minutes=145)) == pytest.approx(2.0)
