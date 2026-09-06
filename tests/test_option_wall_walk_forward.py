import pandas as pd
import pytest

from scripts.option_wall_walk_forward import (
    _annotate_strategy_signals,
    _calendar_event_dates,
    walk_forward_boundaries,
)


def test_walk_forward_boundaries_use_prior_sessions_only():
    dates = [f"2026-01-{day:02d}" for day in range(1, 26)]
    boundaries = walk_forward_boundaries(dates, 20)
    assert len(boundaries) == 5
    for history, test_day in boundaries:
        assert test_day not in history
        assert max(history) < test_day


def test_walk_forward_rejects_too_few_sessions():
    with pytest.raises(ValueError, match="not enough sessions"):
        walk_forward_boundaries([f"2026-01-{day:02d}" for day in range(1, 21)], 20)


def test_calendar_events_mark_opex_week_and_last_friday():
    sessions = [
        "2026-03-16", "2026-03-17", "2026-03-18", "2026-03-19", "2026-03-20",
        "2026-03-27",
    ]
    events = _calendar_event_dates(sessions)
    assert events["opex_day"] == {"2026-03-20"}
    assert events["opex_week"] == set(sessions[:5])
    assert events["month_end_friday"] == {"2026-03-27"}


def test_calendar_events_move_opex_to_prior_session_when_friday_is_closed():
    sessions = ["2026-06-15", "2026-06-16", "2026-06-17", "2026-06-18"]
    events = _calendar_event_dates(sessions)
    assert events["opex_day"] == {"2026-06-18"}


def test_first_observation_has_no_previous_wall_confirmation():
    frame = pd.DataFrame({
        "date": ["2026-06-18", "2026-06-18"],
        "as_of": ["2026-06-18T13:35:00Z", "2026-06-18T14:00:00Z"],
        "wf_prediction": [1, 1],
        "wf_confidence": [0.8, 0.8],
        "oi_peak1_bps": [20.0, 20.0],
        "oi_peak1_share": [0.2, 0.2],
        "oi_side_imbalance": [0.2, 0.2],
        "oi_peak_count_20pct": [1, 1],
    })
    annotated = _annotate_strategy_signals(frame, 0.55)
    assert annotated.iloc[0]["primary_single_wall_stable_signal"] == 0
    assert annotated.iloc[1]["primary_single_wall_stable_signal"] == 1
