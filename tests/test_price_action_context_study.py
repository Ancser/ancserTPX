"""Contracts for the offline PA context research vocabulary."""

from __future__ import annotations

from datetime import date

from scripts.price_action_context_study import (
    DAY_FILTERS,
    ContextSpec,
    SessionMeta,
    SignalContext,
    _calendar_event_dates,
    _context_passes,
    _day_filter_passes,
    _screen_verdict,
    context_catalog,
)


def _meta(*, quality: bool = True, opex_day: bool = False, opex_week: bool = False):
    context = SignalContext(
        long_gates=frozenset({"vwap_aligned", "prior_breakout", "regime_trend", "daily_aligned"}),
        short_gates=frozenset({"vwap_reclaim", "prior_rejection", "regime_range", "daily_opposed"}),
        vwap_slope_ticks=8.0,
        regime="trend",
    )
    return SessionMeta(
        session_date=date(2026, 9, 1),
        quality_ok=quality,
        opex_day=opex_day,
        opex_week=opex_week,
        previous_day_direction=1,
        previous_day_body_fraction=0.4,
        previous_day_range=100.0,
        previous_day_large=False,
        signal_contexts={10: context},
    )


def test_catalog_contains_the_requested_context_dimensions_without_duplicates():
    catalog = context_catalog()
    names = [item.name for item in catalog]

    assert len(names) == len(set(names))
    assert {"vwap_aligned", "vwap_reclaim", "vwap_deviation"}.issubset(names)
    assert {"prior_rejection", "prior_breakout"}.issubset(names)
    assert {"regime_trend", "regime_range"}.issubset(names)
    assert {"daily_aligned", "daily_opposed", "daily_neutral", "daily_large"}.issubset(names)
    assert any(len(item.gates) == 3 for item in catalog)


def test_context_gate_is_directional_and_causal_signal_key_is_required():
    meta = _meta()
    signal = type("Signal", (), {"signal_index": 10})()

    assert _context_passes(ContextSpec("long", ("vwap_aligned",)), meta, signal, 1)
    assert not _context_passes(ContextSpec("long", ("vwap_aligned",)), meta, signal, -1)
    assert _context_passes(ContextSpec("short", ("vwap_reclaim",)), meta, signal, -1)
    assert not _context_passes(ContextSpec("missing", ("vwap_aligned",)), meta, type("S", (), {"signal_index": 11})(), 1)


def test_calendar_marks_third_friday_and_week_with_holiday_fallback():
    dates = [
        date(2026, 9, 14),
        date(2026, 9, 15),
        date(2026, 9, 16),
        date(2026, 9, 17),
        date(2026, 9, 18),
        date(2026, 10, 12),
        date(2026, 10, 13),
        date(2026, 10, 14),
        date(2026, 10, 15),
    ]
    events = _calendar_event_dates(dates)

    assert date(2026, 9, 18) in events["opex_day"]
    assert events["opex_week"] >= {
        date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16), date(2026, 9, 17), date(2026, 9, 18)
    }
    # October's third Friday is absent from the supplied sessions, so the
    # Thursday session is the explicit day proxy rather than a future date.
    assert date(2026, 10, 15) in events["opex_day"]
    assert date(2026, 10, 16) not in events["opex_day"]


def test_day_filters_only_remove_the_declared_quality_or_calendar_days():
    assert all(_day_filter_passes(name, _meta()) for name in DAY_FILTERS[:1])
    assert not _day_filter_passes("quality_only", _meta(quality=False))
    assert not _day_filter_passes("exclude_opex_day", _meta(opex_day=True))
    assert not _day_filter_passes("exclude_opex_week", _meta(opex_week=True))
    assert not _day_filter_passes(
        "quality_exclude_opex_day", _meta(quality=False, opex_day=True)
    )
    assert _day_filter_passes("quality_exclude_opex_week", _meta())


def test_screen_requires_both_validation_and_holdout_not_just_full_sample():
    good = {
        "trades": 100,
        "pnl": 1000.0,
        "pf": 1.2,
        "slip14_pf": 1.1,
    }
    thin = {"trades": 5, "pnl": 100.0, "pf": 2.0, "slip14_pf": 1.5}

    assert _screen_verdict(good, good, good) == "OOS_SCREEN_LEAD"
    assert _screen_verdict(good, good, thin) == "THIN_OOS"
    assert _screen_verdict(good, {**good, "pnl": -1.0}, good) == "REJECT"
