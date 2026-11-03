"""Causality and mask semantics of scripts/vp_filter_study.py.

The study's verdict is only meaningful if every filter feature for day D is
built from sessions strictly before D (the RTH open of D is the one same-day
input, known at 09:30 before any entry).
"""
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import vp_filter_study as study  # noqa: E402


def _synthetic(monkeypatch, tmp_path, days=25):
    dates = [date(2024, 1, 1) + timedelta(days=i) for i in range(days)]
    sessions = pd.DataFrame({
        "open": [100.0 + i for i in range(days)],
        "high": [110.0 + i for i in range(days)],
        "low": [90.0 + i for i in range(days)],
        "close": [105.0 + i for i in range(days)],
        "bars": [390] * days,
    }, index=dates)
    profiles = pd.DataFrame({
        "symbol": ["MNQ"] * days,
        "source_session_date_et": [d.isoformat() for d in dates],
        "poc": [100.0 + i for i in range(days)],
        "vah": [1000.0 + i for i in range(days)],   # distinct per day
        "val": [-1000.0 - i for i in range(days)],
    })
    path = tmp_path / "profiles.csv"
    profiles.to_csv(path, index=False)
    monkeypatch.setattr(study, "rth_sessions", lambda symbol: sessions)
    monkeypatch.setattr(study, "PROFILES", path)
    return dates


def test_features_for_a_day_come_only_from_earlier_sessions(monkeypatch, tmp_path):
    dates = _synthetic(monkeypatch, tmp_path)
    features = study.session_features("MNQ")
    day = dates[21]
    row = features.loc[day]
    assert row["prior_vah"] == 1000.0 + 20          # day 20's profile, not day 21's
    assert row["prior_val"] == -1000.0 - 20
    assert row["prior_close"] == 105.0 + 20
    assert row["rth_open"] == 100.0 + 21            # same-day open is allowed
    # The 20-session means end at the previous session.
    assert row["mean_close_20"] == sum(105.0 + i for i in range(1, 21)) / 20
    assert pd.isna(features.loc[dates[0], "prior_vah"])


def test_masks_follow_trade_direction():
    frame = pd.DataFrame({
        "is_long": [True, True, False, False],
        "rth_open": [120.0, 100.0, 80.0, 100.0],
        "prior_vah": [110.0, 110.0, 110.0, 110.0],
        "prior_val": [90.0, 90.0, 90.0, 90.0],
        "prior_close": [105.0, 95.0, 95.0, 105.0],
        "mean_close_20": [100.0, 100.0, 100.0, 100.0],
        "va_width_ratio": [0.1, 0.9, 0.1, 0.9],
        "entry_minute_et": [600, 700, 690, 691],
        "trade_of_day": [1, 2, 1, 1],
        "range_expansion": [1.2, 0.8, 1.2, 0.8],
    })
    m = study.masks(frame, narrow_cut=0.5)
    assert m["open_outside"].tolist() == [True, False, True, False]
    assert m["trend_aligned"].tolist() == [True, False, True, False]
    assert m["open_outside_and_trend"].tolist() == [True, False, True, False]
    assert m["narrow_value"].tolist() == [True, False, True, False]
    assert m["morning"].tolist() == [True, False, False, False]   # 11:30 is not morning
    assert m["first_trade"].tolist() == [True, False, True, True]
    assert m["vol_expansion"].tolist() == [True, False, True, False]
