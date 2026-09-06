import numpy as np
import pandas as pd

from scripts.option_wall_meta_filter_study import meta_feature_frame


def test_meta_features_orient_price_wall_and_imbalance_by_direction():
    frame = pd.DataFrame({
        "direction": [1, -1],
        "minutes_since_open": [30.0, 60.0],
        "volume_gamma_state": [1, -1], "oi_gamma_state": [1, -1],
        "book_gamma_consensus": [True, True],
        "article_price_vwap_distance_bps": [5.0, 5.0],
        "article_price_return_5m_bps": [2.0, 2.0],
        "article_price_return_15m_bps": [3.0, 3.0],
        "article_price_vwap_slope_15m_bps": [1.0, 1.0],
        "dashboard_vol_call_wall_bps": [20.0, 20.0],
        "dashboard_vol_put_wall_bps": [-30.0, -30.0],
        "dashboard_vol_call_wall_share": [0.12, 0.12],
        "dashboard_vol_put_wall_share": [0.08, 0.08],
        "article_dashboard_vol_call_wall_share_delta": [0.01, 0.01],
        "article_dashboard_vol_put_wall_share_delta": [-0.02, -0.02],
        "article_dashboard_vol_call_wall_migration_bps_per_hour": [4.0, 4.0],
        "article_dashboard_vol_put_wall_migration_bps_per_hour": [-6.0, -6.0],
        "book_wall_tension": [0.2, 0.2],
        "oi_peak1_bps": [10.0, 10.0], "oi_peak1_share": [0.2, 0.2],
        "oi_peak_count_20pct": [1.0, 1.0],
        "oi_side_imbalance": [0.3, 0.3], "vol_side_imbalance": [0.4, 0.4],
        "article_iv_atm_pct": [25.0, 25.0],
        "book_article_iv_atm_pct_delta": [-1.0, -1.0],
        "book_vol_abs_gex_delta": [-2.0, -2.0],
        "article_iv_downside_minus_upside_pct": [5.0, 5.0],
        "article_event_opex_week": [0.0, 0.0],
        "article_event_month_end_friday": [0.0, 0.0],
    })
    result = meta_feature_frame(frame)
    assert result["oriented_vwap_distance"].tolist() == [5.0, -5.0]
    assert result["target_wall_room_bps"].tolist() == [20.0, 30.0]
    assert result["target_wall_share"].tolist() == [0.12, 0.08]
    assert result["oriented_oi_side_imbalance"].tolist() == [0.3, -0.3]
