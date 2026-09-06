import numpy as np
import pandas as pd

from scripts.option_wall_book_rules_study import (
    augment_book_features,
    standalone_book_signals,
)


def _rows():
    return pd.DataFrame({
        "date": ["2026-01-02", "2026-01-02"],
        "as_of": pd.to_datetime(["2026-01-02T15:00Z", "2026-01-02T16:00Z"], utc=True),
        "as_of_et": ["10:00", "11:00"],
        "dashboard_oi_net_gex_signed_log": [-10.0, -8.0],
        "dashboard_vol_net_gex_signed_log": [-9.0, -6.0],
        "oi_gamma_flip_bps": [5.0, 5.0],
        "dashboard_vol_gamma_flip_proxy_bps": [4.0, 4.0],
        "article_iv_atm_pct": [30.0, 28.0],
        "qqq_spot": [100.0, 101.0],
        "dashboard_vol_call_wall_bps": [20.0, 25.0],
        "dashboard_vol_put_wall_bps": [-20.0, -25.0],
        "dashboard_vol_call_wall_share": [0.10, 0.12],
        "dashboard_vol_put_wall_share": [0.10, 0.08],
        "article_price_return_15m_bps": [-5.0, 10.0],
        "article_event_opex_day": [0.0, 0.0],
        "article_event_opex_week": [0.0, 0.0],
        "article_event_month_end_friday": [0.0, 0.0],
        "article_event_friday": [0.0, 0.0],
        "article_event_late_month": [0.0, 0.0],
    })


def test_augmentation_uses_same_session_lags_for_gex_iv_and_walls():
    result = augment_book_features(_rows())
    assert np.isnan(result.iloc[0]["book_vol_abs_gex_delta"])
    assert result.iloc[1]["book_vol_abs_gex_delta"] == -3.0
    assert result.iloc[1]["book_article_iv_atm_pct_delta"] == -2.0
    assert result.iloc[1]["book_previous_vol_call_wall_level"] == 100.2


def test_deep_v_requires_contracting_gex_reclaim_and_optional_iv():
    frame = augment_book_features(_rows())
    frame["article_price_below_put_wall_fraction_5m"] = [0.0, 0.5]
    frame["article_price_return_5m_bps"] = [-2.0, 3.0]
    frame["oi_peak1_bps"] = [-10.0, 10.0]
    frame["oi_peak1_share"] = [0.2, 0.2]
    signals = standalone_book_signals(frame)
    assert signals["deep_v_gex_price"].tolist() == [0, 1]
    assert signals["deep_v_gex_iv_price"].tolist() == [0, 1]
