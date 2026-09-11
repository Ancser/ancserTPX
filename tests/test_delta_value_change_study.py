from datetime import datetime
from backend.db.models import Candle
from backend.timebase import UTC
from scripts.delta_value_change_study import changes, generate, partition_delta, minute_grid


def test_off_grid_records_cannot_become_research_execution_candles():
    valid = Candle(timestamp=datetime(2026,9,7,16,25,tzinfo=UTC),open=29590,high=29600,low=29580,close=29595,volume=10)
    bad = Candle(timestamp=datetime(2026,9,7,16,25,30,423317,tzinfo=UTC),open=20000,high=20001,low=19999,close=20000,volume=10)
    original = [valid,bad]
    clean,audit=minute_grid(original)
    assert clean == [valid]
    assert len(original)==2
    assert audit[0]["close"]==20000


def test_partition_conserves_delta_and_keeps_boundaries_inside():
    bar = {"cells": [[399, 2, 12], [400, 20, 1], [440, 4, 9], [441, 30, 3]]}
    parts = partition_delta(bar, {"val": 100, "vah": 110})
    assert parts == {"below": -10, "inside": 14, "above": 27}
    assert sum(parts.values()) == sum(c[1]-c[2] for c in bar["cells"])


def test_signed_changes_handle_weakening_and_zero_crossing():
    assert changes([-800, -500, -150], 2, 1) == (0, 0, 500, 150)
    assert changes([-800, 0, 150], 2, 1) == (0, 150, 0, 0)
    assert changes([10, -20, 30, -40], 3, 2) == (5, 15, 10, 20)


def sample_day():
    bars = [{"epoch": 60*i, "open": 100, "high": 101, "low": 99,
             "close":100, "buy":0, "sell":100, "cells":[[396,0,100]]} for i in range(40)]
    bars[30].update(buy=0,sell=20,close=100.5,cells=[[396,0,20]])
    return {"date":"2026-08-10","bars":bars}


def test_events_are_positive_causal_and_do_not_cross_gaps():
    day = sample_day()
    profiles = {day["date"]:{"val":100,"vah":110}}
    full, _ = generate([day], profiles)
    key = "exhaustion/w1/outside/reclaim"
    assert (1860,1) in full[key]
    prefix, _ = generate([{**day,"bars":day["bars"][:31]}], profiles)
    for name in full:
        assert {e for e in full[name] if e[0] <= 1860} == prefix[name]
    day["bars"][29]["epoch"] -= 30
    broken, _ = generate([day],profiles)
    assert (1860,1) not in broken[key]


def test_missing_profile_disables_only_value_rules():
    events, _ = generate([sample_day()], {})
    assert (1860,1) in events["exhaustion/w1/whole/raw"]
    assert not any(v for k,v in events.items() if "/outside/" in k or not k.endswith("/raw"))
