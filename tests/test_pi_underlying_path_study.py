from datetime import date, timedelta

from scripts.pi_underlying_path_study_20260921 import (
    Bar,
    NY,
    analyze_signal,
    canonical_buy_signals,
    expected_entry,
    expected_session_times,
    first_pass_category,
    following_sessions,
    recovery_flags,
    target_hit,
    validated_horizon_path,
)


def _bars(times, price=100.0):
    return {
        stamp: Bar(stamp, price, price, price, price)
        for stamp in times
    }


def test_first_pass_up_down_and_same_bar_ambiguity():
    start = expected_session_times(date(2026, 3, 30))[0]
    up = Bar(start, 100, 101, 100, 101)
    down = Bar(start + timedelta(minutes=1), 100, 100, 99, 99)
    assert first_pass_category([up, down], 100, 0.005) == "up_first_then_down"
    assert first_pass_category([down, up], 100, 0.005) == "down_first_then_up"
    both = Bar(start, 100, 101, 99, 100)
    assert first_pass_category([both], 100, 0.005) == "same_bar_ambiguous"


def test_symmetric_recovery_requires_a_later_minute():
    start = expected_session_times(date(2026, 3, 30))[0]
    bars = [
        Bar(start, 100, 100.6, 100, 100.5),
        Bar(start + timedelta(minutes=1), 100.5, 100.6, 99.9, 100),
        Bar(start + timedelta(minutes=2), 100, 100, 99.4, 99.5),
        Bar(start + timedelta(minutes=3), 99.5, 100.1, 99.4, 100),
    ]
    assert recovery_flags(bars, 100, 0.005) == {
        "up_hit": True,
        "up_then_fallback_p0": True,
        "down_hit": True,
        "down_then_reclaim_p0": True,
    }


def test_target_bar_low_is_an_uncertain_bound_not_strict_pre_hit_dip():
    start = expected_session_times(date(2026, 3, 30))[0]
    bars = [
        Bar(start, 100, 101, 99, 100),
        Bar(start + timedelta(minutes=1), 100, 103.2, 98.5, 103),
    ]
    result = target_hit(bars, 100, 3)
    assert result["strict_prior_min_low"] == 99
    assert result["pre_target_min_low_price_bound"] == [98.5, 99]
    assert result["hit_bar_low_target_order_unknown"] is True


def test_entry_stays_in_source_session_and_excludes_prior_minute():
    source = expected_session_times(date(2026, 3, 30))[0] + timedelta(hours=5, minutes=31)
    entry_day, entry_ts = expected_entry(source)
    assert entry_day == date(2026, 3, 30)
    assert entry_ts == source + timedelta(minutes=1)

    after_close = source.replace(hour=20, minute=1)
    rejected_day, rejected_entry = expected_entry(after_close)
    assert rejected_day == date(2026, 3, 30)
    assert rejected_entry is None


def test_incomplete_horizon_is_rejected_and_exchange_sessions_are_used():
    day0 = date(2026, 3, 30)
    entry_ts = expected_session_times(day0)[-2]
    next_day = following_sessions(day0, 1)[0]
    day0_bars = _bars([stamp for stamp in expected_session_times(day0) if stamp >= entry_ts])
    next_times = expected_session_times(next_day)
    next_bars = _bars(next_times)
    del next_bars[next_times[40]]
    path, missing = validated_horizon_path(
        day0, entry_ts, 1, {day0: day0_bars, next_day: next_bars},
    )
    assert path is None
    assert len(missing) == 1
    assert following_sessions(date(2026, 3, 30), 5) == [
        date(2026, 3, 31), date(2026, 4, 1), date(2026, 4, 2),
        date(2026, 4, 6), date(2026, 4, 7),
    ]
    assert len(expected_session_times(date(2026, 7, 2))) == 390
    assert following_sessions(date(2026, 7, 2), 1) == [date(2026, 7, 6)]


def test_source_level_is_not_inferred_from_visual_size_and_exact_duplicates_drop():
    rows = [
        {
            "id": "a",
            "ts": "2026-03-30T19:01:00+00:00",
            "symbol": "QQQ",
            "marks": [{"kind": "深蓝圈", "level": None, "size": "Level 2"}],
        },
        {
            "id": "b",
            "ts": "2026-03-30T19:01:30+00:00",
            "symbol": "QQQ",
            "marks": [{"kind": "深蓝圈", "level": None, "size": "Level 2"}],
        },
    ]
    signals, duplicates = canonical_buy_signals(rows)
    assert len(signals) == 1
    assert duplicates == 1
    assert signals[0].level == "unknown"

