from __future__ import annotations

from scripts import orderflow_context_combination_study as study


def _bar(cells, *, close=100.0, low=99.0, high=101.0):
    return {
        "epoch": 1_700_000_000,
        "open": 100.0,
        "high": high,
        "low": low,
        "close": close,
        "cells": cells,
    }


def test_diagonal_imbalance_uses_exact_1_to_10_ratio():
    # Buy at 100.25 versus sell one tick below: 20:1.  The reverse side is
    # below the minimum dominant quantity and must not qualify.
    bar = _bar([
        [400, 1, 1, 0, 0, 0, 0, 0, 0],
        [401, 20, 0, 0, 0, 0, 0, 0, 0],
    ])

    result = study._imbalance_1_10(bar)

    assert result["imbalance_1_10_side"] == 1
    assert result["imbalance_1_10_buy_levels"] == 1
    assert result["imbalance_1_10_sell_levels"] == 0


def test_context_filters_align_vwap_value_wave_and_imbalance():
    row = {
        "vwap_state": "above",
        "value_location": "below_val",
        "value_zone": "below_val",
        "profile_wave": "up",
        "imbalance_1_10_side": 1,
        "passive_rejection_side": 1,
        "cvd_state_side": 1,
        "cvd_divergence_side": 1,
    }

    assert study._context_matches("vwap_aligned", row, 1)
    assert study._context_matches("value_reversion", row, 1)
    assert study._context_matches("vwap+value+wave+imbalance", row, 1)
    assert study._context_matches("cvd_aligned", row, 1)
    assert study._context_matches("cvd_divergence", row, 1)
    assert study._context_matches("cvd+vwap", row, 1)
    assert study._context_matches("cvd+value", row, 1)
    assert study._context_matches("cvd+imbalance", row, 1)
    assert study._context_matches("cvd+passive", row, 1)
    assert study._context_matches("vwap+value+wave+imbalance+cvd", row, 1)
    assert not study._context_matches("value_breakout", row, 1)
    assert not study._context_matches("vwap_aligned", row, -1)


def test_rth_cvd_is_cumulative_and_has_directional_windows():
    bars = [
        _bar([], close=100.0),
        _bar([], close=100.0),
        _bar([], close=100.0),
        _bar([], close=100.0),
        _bar([], close=100.0),
        _bar([], close=100.0),
        _bar([], close=100.0),
        _bar([], close=100.0),
        _bar([], close=100.0),
        _bar([], close=100.0),
        _bar([], close=100.0),
        _bar([], close=100.0),
        _bar([], close=100.0),
        _bar([], close=100.0),
        _bar([], close=100.0),
        _bar([], close=100.0),
    ]
    for index, bar in enumerate(bars):
        bar["epoch"] += index * 60
        bar["buy"] = 4 if index >= 11 else 1
        bar["sell"] = 0

    study._enrich_context_day({"bars": bars})

    assert bars[0]["rth_cvd"] == 1
    assert bars[-1]["rth_cvd"] == 31
    assert bars[-1]["cvd_5_delta"] == 20
    assert bars[-1]["cvd_15_delta"] == 30
    assert bars[-1]["cvd_state_side"] == 1


def test_passive_level_touch_and_rejection_are_directional():
    # v5 row: tick, buy, sell, passive bid fill, passive ask fill,
    # max bid depth, max ask depth, bid refill, ask refill, six size bands.
    bar = _bar([
        [400, 0, 0, 4, 0, 20, 0, 6, 0, 0, 0, 0, 0, 0, 0],
    ], close=100.25, low=99.9, high=100.5)
    study._enrich_context_day({"bars": [bar]})

    assert bar["passive_bid_level_tick"] == 400
    assert bar["passive_bid_touch"] is True
    assert bar["passive_rejection_side"] == 1


def test_large_reversal_requires_persistent_active_level():
    bars = []
    for index in range(11):
        level = [400, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
        if index == 0:
            level[11] = 150  # buy 150+
        if index == 10:
            level[14] = 150  # sell 150+
        level[3] = 1
        level[7] = 1
        bars.append(_bar([level], close=100.25))
    for index, bar in enumerate(bars):
        bar["epoch"] += index * 60

    study._enrich_context_day({"bars": bars})

    events = bars[-1]["large_reversal_events"]
    assert any(event["direction"] == -1 for event in events)
    assert any(event["persistence"] >= study.PERSISTENCE_MIN_BARS for event in events)


def test_entry_family_report_keeps_zero_observation_families_visible():
    entries = study._build_entries([], [])

    assert set(entries) == set(study.ENTRY_FAMILIES)
    assert entries["large_150_reversal"] == []
