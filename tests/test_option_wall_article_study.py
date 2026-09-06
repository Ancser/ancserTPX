import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from scripts.option_wall_article_walk_forward import (
    _layered_signals,
    run_article_walk_forward,
)
from scripts.option_wall_demo import _gamma, _gamma_price_profile
from scripts.option_wall_ml_study import (
    LEGACY_WALL_FEATURES,
    _article_price_features,
    _feature_columns,
    _future_path_targets,
    ablation_feature_columns,
    add_article_temporal_features,
    extract_dashboard_features,
)


UTC = timezone.utc


def _profile() -> pd.DataFrame:
    rows = []
    for strike, call, put, oi, volume in [
        (98.0, 2.0, -40.0, 10, 2),
        (99.0, 5.0, -70.0, 20, 7),
        (100.0, 10.0, -20.0, 30, 3),
        (101.0, 80.0, -5.0, 40, 9),
        (102.0, 45.0, -2.0, 50, 4),
    ]:
        rows.append({
            "strike": strike, "class": "C", "oi_gex": call,
            "volume_gex": call * volume, "oi": oi, "volume": volume,
            "iv": 0.20, "gamma": 0.01, "sign": 1,
        })
        rows.append({
            "strike": strike, "class": "P", "oi_gex": put,
            "volume_gex": put * volume, "oi": oi, "volume": volume,
            "iv": 0.22, "gamma": 0.01, "sign": -1,
        })
    return pd.DataFrame(rows)


def test_legacy_feature_set_remains_exactly_42_after_article_columns_exist():
    row = {name: 1.0 for name in LEGACY_WALL_FEATURES}
    row.update({
        "dashboard_oi_net_gex_signed_log": 3.0,
        "article_price_vwap_distance_bps": 4.0,
        "target_expansion_30m": 1,
        "qqq_spot": 100.0,
    })
    frame = pd.DataFrame([row])
    legacy = _feature_columns(frame, include_price=False)
    combined = ablation_feature_columns(frame, "combined_0dte")
    assert len(legacy) == 42
    assert set(legacy) == set(LEGACY_WALL_FEATURES)
    assert "dashboard_oi_net_gex_signed_log" in combined
    assert "article_price_vwap_distance_bps" in combined
    assert "target_expansion_30m" not in combined


def test_dashboard_features_add_volume_walls_gex_scale_and_oi_counts():
    as_of = datetime(2026, 9, 2, 14, 0, tzinfo=UTC)
    expiry = datetime(2026, 9, 2, 20, 15, tzinfo=UTC)
    features = extract_dashboard_features(_profile(), 100.0, as_of, expiry)
    assert features["dashboard_oi_net_gex_signed_log"] == math.copysign(
        math.log1p(abs(142.0 - 137.0)), 142.0 - 137.0,
    )
    assert features["dashboard_total_oi_log"] == math.log1p(300.0)
    assert features["dashboard_vol_call_wall_bps"] == pytest.approx(100.0)
    assert features["dashboard_vol_put_wall_bps"] == pytest.approx(-100.0)
    assert "dashboard_vol_gamma_flip_proxy_bps" in features

    zero_volume = _profile()
    zero_volume[["volume", "volume_gex"]] = 0
    without_flow = extract_dashboard_features(zero_volume, 100.0, as_of, expiry)
    assert "dashboard_vol_call_wall_bps" not in without_flow
    assert "dashboard_vol_put_wall_bps" not in without_flow


def test_vectorized_gamma_price_profile_matches_scalar_definition():
    profile = _profile()
    years = 1.0 / 365.0
    grid, totals = _gamma_price_profile(profile, 100.0, years, "oi")
    for index in (0, len(grid) // 2, len(grid) - 1):
        trial = float(grid[index])
        expected = sum(
            float(row.sign) * _gamma(trial, float(row.strike), years, float(row.iv))
            * float(row.oi) * 100.0 * trial * trial * 0.01
            for row in profile.itertuples()
        )
        assert totals[index] == pytest.approx(expected, rel=1e-12)


def _qqq_rows(start: datetime, closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({
        "available_at": [start + timedelta(minutes=i + 1) for i in range(len(closes))],
        "high": [value + 0.05 for value in closes],
        "low": [value - 0.05 for value in closes],
        "close": closes,
        "volume": [100 + i for i in range(len(closes))],
    })


def test_article_price_features_do_not_read_bars_after_as_of():
    open_at = datetime(2026, 9, 2, 13, 30, tzinfo=UTC)
    as_of = open_at + timedelta(minutes=10)
    past = _qqq_rows(open_at, [100.0 + i * 0.01 for i in range(10)])
    future = _qqq_rows(as_of, [150.0, 160.0])
    walls = {
        "oi_gamma_flip_bps": -5.0,
        "oi_call_wall_bps": 20.0,
        "oi_put_wall_bps": -20.0,
    }
    expected = _article_price_features(past, as_of, open_at, 100.09, walls)
    actual = _article_price_features(
        pd.concat([past, future], ignore_index=True), as_of, open_at, 100.09, walls,
    )
    assert actual == expected
    assert actual["article_price_above_vwap_fraction_10m"] > 0


def test_future_path_target_uses_first_wall_and_treats_same_bar_as_ambiguous():
    as_of = datetime(2026, 9, 2, 14, 0, tzinfo=UTC)
    future = pd.DataFrame({
        "available_at": [as_of + timedelta(minutes=1), as_of + timedelta(minutes=2)],
        "high": [100.4, 101.2],
        "low": [99.7, 100.0],
        "close": [100.3, 101.1],
    })
    target = _future_path_targets(
        future, as_of, as_of + timedelta(minutes=30), 100.0, 100.0, -100.0, 10.0,
    )
    assert target["target_expansion_30m"] == 1
    assert target["target_wall_first_30m"] == 1
    assert target["target_wall_hit_minutes_30m"] == 2.0

    ambiguous = future.iloc[[0]].copy()
    ambiguous["high"] = 101.2
    ambiguous["low"] = 98.8
    target = _future_path_targets(
        ambiguous, as_of, as_of + timedelta(minutes=30), 100.0, 100.0, -100.0, 10.0,
    )
    assert target["target_wall_first_30m"] == 0


def test_temporal_features_reset_at_each_session_and_never_backfill_first_row():
    frame = pd.DataFrame({
        "date": ["2026-09-01", "2026-09-01", "2026-09-02", "2026-09-02"],
        "as_of": [
            "2026-09-01T14:00:00Z", "2026-09-01T15:00:00Z",
            "2026-09-02T14:00:00Z", "2026-09-02T15:00:00Z",
        ],
        "qqq_spot": [100.0, 101.0, 200.0, 202.0],
        "oi_call_wall_bps": [100.0, 100.0, 50.0, 50.0],
        "oi_put_wall_bps": [-100.0, -100.0, -50.0, -50.0],
        "oi_gross_log": [1.0, 1.1, 2.0, 2.1],
        "oi_net_balance": [0.1, 0.2, -0.1, -0.2],
        "article_gvp_price_flip_alignment": [1.0, 1.0, -1.0, -1.0],
    })
    result = add_article_temporal_features(frame)
    assert result["article_has_previous_snapshot"].tolist() == [0.0, 1.0, 0.0, 1.0]
    migration = result["article_oi_call_wall_migration_bps_per_hour"]
    assert np.isnan(migration.iloc[0])
    assert np.isnan(migration.iloc[2])
    assert migration.iloc[1] > 0
    assert migration.iloc[3] > 0


def test_three_layer_target_confirmation_only_keeps_agreeing_direction():
    regime, confirmed = _layered_signals(
        np.array([0.8, 0.8, 0.4]),
        np.array([1, -1, 1]),
        np.array([0.7, 0.7, 0.9]),
        np.array([1, 1, 1]),
        np.array([0.6, 0.8, 0.9]),
        0.55, 0.55, 0.45,
    )
    assert regime.tolist() == [1, -1, 0]
    assert confirmed.tolist() == [1, 0, 0]


def test_article_walk_forward_executes_all_ablations_and_layers(tmp_path):
    rows = []
    for day_index, day in enumerate(pd.date_range("2026-01-05", periods=12, freq="B")):
        for hour in (10, 11):
            label = (-1, 0, 1)[(day_index + hour) % 3]
            row = {
                name: float(label) + (index % 3) * 0.01
                for index, name in enumerate(sorted(LEGACY_WALL_FEATURES))
            }
            row.update({
                "date": day.date().isoformat(),
                "as_of": f"{day.date().isoformat()}T{hour + 5:02d}:00:00Z",
                "as_of_et": f"{hour:02d}:00",
                "label_30m": label,
                "target_expansion_30m": int(label != 0),
                "target_wall_first_30m": label,
                "mnq_points_30m": float(label * 5 if label else 1),
                "dashboard_probe": float(label),
                "article_probe": float(label),
            })
            rows.append(row)
    pd.DataFrame(rows).to_csv(
        tmp_path / "option_wall_ml_dataset.csv.gz", index=False, compression="gzip",
    )
    report = run_article_walk_forward(
        tmp_path, min_train_sessions=10, simulations=20, monte_carlo_horizon=5,
    )
    assert set(report["ablations"]) == {
        "legacy_wall", "dashboard", "article_state", "combined_0dte",
    }
    assert report["oos_sessions"] == 2
    assert report["three_layer"]["strategies"]["regime_direction"]["trades"] > 0
    assert (tmp_path / "option_wall_article_walk_forward_signals.csv.gz").is_file()
