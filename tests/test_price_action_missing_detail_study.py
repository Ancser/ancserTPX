from datetime import datetime, timezone
from types import SimpleNamespace

from backend.db.models import Candle
from scripts.price_action_missing_detail_study import (
    _acceptance,
    _management_candidate,
    _overnight_ranges,
    _touch_reclaim,
    detail_context_catalog,
)
from scripts.price_action_study import SignalSpec


def test_detail_catalog_is_frozen_and_covers_the_missing_dimensions():
    contexts = detail_context_catalog()
    names = [context.name for context in contexts]

    assert len(names) == len(set(names))
    assert {
        "overnight_rejection",
        "value_acceptance",
        "open_initiative",
        "relative_volume_high",
        "displacement",
        "follow_through",
        "opening_range_acceptance",
    }.issubset(names)
    assert any(len(context.gates) == 2 for context in contexts)


def test_overnight_window_maps_18_to_0930_et_to_the_rth_session_date():
    candles = [
        Candle(
            timestamp=datetime(2026, 9, 10, 22, 0, tzinfo=timezone.utc),
            open=100,
            high=105,
            low=99,
            close=104,
            volume=10,
        ),
        Candle(
            timestamp=datetime(2026, 9, 11, 13, 29, tzinfo=timezone.utc),
            open=104,
            high=106,
            low=101,
            close=102,
            volume=20,
        ),
        Candle(
            timestamp=datetime(2026, 9, 11, 13, 30, tzinfo=timezone.utc),
            open=102,
            high=107,
            low=100,
            close=106,
            volume=30,
        ),
    ]

    ranges = _overnight_ranges(candles)

    assert ranges.keys() == {datetime(2026, 9, 11, tzinfo=timezone.utc).date()}
    assert ranges[datetime(2026, 9, 11, tzinfo=timezone.utc).date()].high == 106
    assert ranges[datetime(2026, 9, 11, tzinfo=timezone.utc).date()].low == 99
    assert ranges[datetime(2026, 9, 11, tzinfo=timezone.utc).date()].bars == 2


def test_level_reclaim_and_two_bar_acceptance_are_directional_and_causal():
    reclaim = SimpleNamespace(low=99.25, high=101.0, close=100.5)
    assert _touch_reclaim(reclaim, 100.0, 1, 0.25)
    assert not _touch_reclaim(reclaim, 100.0, -1, 0.25)

    bars = [SimpleNamespace(close=100.5), SimpleNamespace(close=100.75)]
    assert _acceptance(bars, 1, 100.0, 1, 0.25)
    assert not _acceptance(bars, 1, 100.0, -1, 0.25)
    assert not _acceptance(bars, 0, 100.0, 1, 0.25)


def test_management_grid_sets_conservative_partial_and_time_stop_fields():
    spec = SignalSpec(
        signal_index=10,
        signal_time=datetime(2026, 9, 11, 16, 0, tzinfo=timezone.utc),
        stop_price=99.0,
        reason="test",
    )

    partial = _management_candidate("outside_reversal", 1, spec, "partial_1R_2R_BE", 0.25)
    timed = _management_candidate("outside_reversal", -1, spec, "fixed_2R_time60", 0.25)

    assert partial.target1_value == 1.0
    assert partial.target2_value == 2.0
    assert partial.target1_fraction == 0.5
    assert partial.move_stop_to_breakeven
    assert timed.target1_value == 2.0
    assert timed.max_hold_minutes == 60
