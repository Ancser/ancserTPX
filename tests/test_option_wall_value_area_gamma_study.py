from datetime import datetime, timezone

import pytest

from backend.db.models import Candle
from scripts.option_wall_value_area_gamma_study import (
    _first_boundary_event,
    boundary_direction,
    classify_value_position,
    classify_value_shift,
    theory_direction,
)


UTC = timezone.utc


@pytest.mark.parametrize(
    ("price", "expected"),
    [(106.0, "above"), (100.0, "inside"), (94.0, "below")],
)
def test_value_position_has_three_exclusive_states(price, expected):
    assert classify_value_position(price, vah=105.0, val=95.0) == expected


@pytest.mark.parametrize(
    ("first_vah", "first_val", "expected"),
    [
        (115.0, 110.0, "above"),
        (105.0, 95.0, "overlap"),
        (90.0, 85.0, "below"),
    ],
)
def test_opening_value_shift_does_not_force_an_overlap_direction(
    first_vah, first_val, expected,
):
    assert classify_value_shift(
        first_vah,
        first_val,
        prior_vah=105.0,
        prior_val=95.0,
    ) == expected


@pytest.mark.parametrize(
    ("gamma", "value_shift", "expected"),
    [
        (-1, "above", 1),
        (-1, "below", -1),
        (1, "above", -1),
        (1, "below", 1),
        (-1, "overlap", 0),
        (1, "overlap", 0),
        (0, "above", 0),
    ],
)
def test_outside_value_theory_mapping_is_explicit(gamma, value_shift, expected):
    assert theory_direction(gamma, value_shift) == expected


def test_middle_value_mapping_fades_positive_gamma_and_breaks_negative_gamma():
    assert boundary_direction(1, "val") == 1
    assert boundary_direction(1, "vah") == -1
    assert boundary_direction(-1, "val") == -1
    assert boundary_direction(-1, "vah") == 1


def _bar(minute: int, *, high: float, low: float) -> Candle:
    return Candle(
        timestamp=datetime(2026, 1, 2, 15, minute, tzinfo=UTC),
        open=100.0,
        high=high,
        low=low,
        close=100.0,
        volume=10,
        symbol="MNQ",
        interval="1m",
    )


def _snapshot(candles):
    return {
        "value_shift": "overlap",
        "prior_vah_70": 102.0,
        "prior_val_70": 98.0,
        "entry_ts": "2026-01-02T15:30:00Z",
        "_candles": candles,
    }


def test_overlap_event_uses_first_unambiguous_boundary():
    candles = [
        _bar(30, high=101.0, low=99.0),
        _bar(31, high=102.5, low=99.5),
    ]
    event = _first_boundary_event(_snapshot(candles), gamma_state=1)
    assert event["event_status"] == "triggered"
    assert event["boundary"] == "vah"
    assert event["direction"] == -1


def test_overlap_event_skips_ohlc_bars_without_tick_ordering():
    event = _first_boundary_event(
        _snapshot([_bar(30, high=103.0, low=97.0)]),
        gamma_state=-1,
    )
    assert event["event_status"] == "ambiguous_both_boundaries"
