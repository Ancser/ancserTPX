import pandas as pd

from scripts.option_wall_positive_gamma_reclaim_study import (
    _find_put_wall_event,
    _negative_gamma,
    _positive_gamma,
)


def _bars(closes, lows=None):
    start = pd.Timestamp("2026-09-01T14:00:00Z")
    lows = lows or [value - 0.05 for value in closes]
    return pd.DataFrame({
        "available_at": [start + pd.Timedelta(minutes=i + 1) for i in range(len(closes))],
        "open": closes,
        "high": [value + 0.05 for value in closes],
        "low": lows,
        "close": closes,
    })


def test_close_below_then_reclaim_requires_a_later_confirming_close():
    qqq = _bars([100.1, 99.9, 100.05])
    event = _find_put_wall_event(
        qqq, pd.Timestamp("2026-09-01T14:00:00Z"),
        pd.Timestamp("2026-09-01T14:10:00Z"), 100.0,
        "close_reclaim", reclaim_minutes=3, penetration_bps=2.0,
    )
    assert event is not None
    assert event["breach_at"] == pd.Timestamp("2026-09-01T14:02:00Z")
    assert event["event_at"] == pd.Timestamp("2026-09-01T14:03:00Z")
    assert event["reclaim_minutes"] == 1.0


def test_touch_is_not_mislabeled_as_close_below_reclaim():
    qqq = _bars([100.05, 100.03], lows=[99.9, 100.0])
    event = _find_put_wall_event(
        qqq, pd.Timestamp("2026-09-01T14:00:00Z"),
        pd.Timestamp("2026-09-01T14:10:00Z"), 100.0,
        "close_reclaim", reclaim_minutes=5, penetration_bps=2.0,
    )
    assert event is None


def test_reclaim_after_window_is_rejected():
    qqq = _bars([99.9, 99.9, 99.9, 99.9, 100.1])
    event = _find_put_wall_event(
        qqq, pd.Timestamp("2026-09-01T14:00:00Z"),
        pd.Timestamp("2026-09-01T14:10:00Z"), 100.0,
        "close_reclaim", reclaim_minutes=3, penetration_bps=2.0,
    )
    assert event is None


def test_gamma_proxy_requires_net_sign_and_flip_side_to_agree():
    positive = pd.Series({
        "dashboard_vol_net_gex_signed_log": 2.0,
        "dashboard_vol_gamma_flip_proxy_bps": -5.0,
    })
    negative = pd.Series({
        "dashboard_vol_net_gex_signed_log": -2.0,
        "dashboard_vol_gamma_flip_proxy_bps": 5.0,
    })
    disagreement = pd.Series({
        "dashboard_vol_net_gex_signed_log": 2.0,
        "dashboard_vol_gamma_flip_proxy_bps": 5.0,
    })
    assert _positive_gamma(positive)
    assert _negative_gamma(negative)
    assert not _positive_gamma(disagreement)
    assert not _negative_gamma(disagreement)
