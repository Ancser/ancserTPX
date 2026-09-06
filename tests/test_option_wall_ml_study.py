from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from scripts.option_wall_ml_study import (
    DEFAULT_MNQ_PATH,
    _daily_specs,
    _hourly_forced_direction_report,
    _is_closed_session_metadata_error,
    _sample_times,
    _session_bounds,
    chronological_split,
    extract_wall_features,
    train_models,
)


def _profile():
    rows = []
    for strike, call, put in [
        (98.0, 2.0, -40.0),
        (99.0, 5.0, -70.0),
        (100.0, 10.0, -20.0),
        (101.0, 80.0, -5.0),
        (102.0, 45.0, -2.0),
    ]:
        rows.append({"strike": strike, "class": "C", "oi_gex": call,
                     "volume_gex": call * 2, "oi": 1, "iv": 0.2, "sign": 1})
        rows.append({"strike": strike, "class": "P", "oi_gex": put,
                     "volume_gex": put * 2, "oi": 1, "iv": 0.2, "sign": -1})
    return pd.DataFrame(rows)


def test_default_mnq_source_is_the_current_ancsertpx_store():
    assert DEFAULT_MNQ_PATH == Path(__file__).resolve().parents[1] / "data" / "store" / "MNQ_accumulated_1m.pkl"


def test_closed_market_symbology_error_is_skippable_but_other_errors_are_not():
    closed = RuntimeError("422 symbology_invalid_request: None of the symbols could be resolved")
    assert _is_closed_session_metadata_error(closed)
    assert not _is_closed_session_metadata_error(RuntimeError("403 license_not_found"))


def test_hourly_report_separates_forced_direction_and_confidence_gate():
    class FixedModel:
        classes_ = [-1, 0, 1]

        def predict_proba(self, frame):
            return [[0.2, 0.6, 0.2], [0.7, 0.1, 0.2]]

        def predict(self, frame):
            return [0, -1]

    frame = pd.DataFrame({
        "date": ["2026-01-01", "2026-01-01"],
        "as_of_et": ["10:00", "11:00"],
        "wall": [1.0, 2.0],
        "mnq_points_60m": [10.0, -10.0],
    })
    report = _hourly_forced_direction_report(FixedModel(), frame, ["wall"], 0.55)
    assert report["forced_every_hour"]["trades"] == 2
    assert report["confidence_gated"]["trades"] == 1
    assert report["forced_every_hour"]["directions"] == {"long": 1, "short": 1}


def test_samples_are_causal_whole_hour_observations_across_dst():
    winter = date(2026, 1, 5)
    summer = date(2026, 7, 6)
    winter_samples = _sample_times(winter)
    summer_samples = _sample_times(summer)
    assert winter_samples[0].hour == 14 and summer_samples[0].hour == 13
    assert [ts.astimezone().minute for ts in winter_samples] == [35, 0, 0, 0, 0, 0, 0]
    assert _session_bounds(winter)[0].hour == 14
    assert _session_bounds(summer)[0].hour == 13


def test_hourly_purchase_plan_uses_1h_option_volume_not_expensive_1m(tmp_path):
    specs = _daily_specs(date(2026, 9, 2), tmp_path, ["QQQ TEST"], True)
    schemas = [spec.schema for spec in specs]
    assert schemas == ["statistics", "cbbo-1m", "ohlcv-1h", "ohlcv-1m"]
    assert all(spec.schema != "ohlcv-1m" or spec.dataset == "EQUS.MINI" for spec in specs)
    hourly = next(spec for spec in specs if spec.schema == "ohlcv-1h")
    assert hourly.start == "2026-09-02T13:00:00Z"


def test_wall_features_preserve_multiple_peaks_and_directional_mass():
    as_of = datetime(2026, 9, 2, 14, 0, tzinfo=timezone.utc)
    expiry = datetime(2026, 9, 2, 20, 15, tzinfo=timezone.utc)
    features = extract_wall_features(_profile(), 100.0, as_of, expiry)
    assert features["oi_peak1_bps"] != features["oi_peak2_bps"]
    assert features["oi_peak1_share"] > 0
    assert features["oi_upper_share"] > 0
    assert features["oi_lower_share"] > 0
    assert -1 <= features["oi_side_imbalance"] <= 1


def test_chronological_split_never_mixes_a_session_between_sets():
    frame = pd.DataFrame({
        "date": [f"2026-01-{day:02d}" for day in range(1, 26) for _ in range(2)],
        "value": range(50),
    })
    train, validation, test = chronological_split(frame)
    train_dates = set(train.date)
    validation_dates = set(validation.date)
    test_dates = set(test.date)
    assert train_dates.isdisjoint(validation_dates)
    assert train_dates.isdisjoint(test_dates)
    assert validation_dates.isdisjoint(test_dates)
    assert max(train_dates) < min(validation_dates) < min(test_dates)


def test_chronological_split_rejects_tiny_day_count():
    frame = pd.DataFrame({"date": ["2026-01-01"] * 10})
    with pytest.raises(ValueError, match="20 independent sessions"):
        chronological_split(frame)


def test_train_models_executes_wall_only_and_comparison_paths(tmp_path):
    rows = []
    for day_index, day in enumerate(pd.date_range("2026-01-01", periods=30, freq="D")):
        for sample in range(6):
            label = (-1, 0, 1)[sample % 3]
            rows.append({
                "date": day.date().isoformat(),
                "as_of": f"{day.date().isoformat()}T15:00:00Z",
                "oi_side_imbalance": label + (day_index % 2) * 0.01,
                "oi_peak1_bps": label * 20.0,
                "price_return_30m_bps": label * 5.0,
                "qqq_future_return_bps_60m": label * 25.0,
                "qqq_future_return_bps_close": label * 30.0,
                "label_60m": label,
                "label_close": label,
            })
    pd.DataFrame(rows).to_csv(
        tmp_path / "option_wall_ml_dataset.csv.gz", index=False, compression="gzip",
    )
    report = train_models(tmp_path)
    assert set(report["models"]) == {
        "60m_wall_only", "60m_wall_plus_price", "close_wall_only", "close_wall_plus_price",
    }
    assert (tmp_path / "models" / "60m_wall_only.joblib").is_file()
    assert report["splits"]["train"][2] == 18
