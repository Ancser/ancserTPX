from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from backend.api import routes
from backend.db.models import Candle
from backend.strategy.volume_profile import (
    PREVIOUS_DAY_VALUE_AREA_PCT,
    VolumeProfileCalculator,
    calculate_previous_day_value_areas,
)


UTC = timezone.utc


def _bars(start: datetime, prices: list[float]) -> list[Candle]:
    return [
        Candle(
            timestamp=start + timedelta(minutes=index),
            open=price,
            high=price + 0.25,
            low=price - 0.25,
            close=price,
            volume=100 + index * 10,
            symbol="MNQ",
            interval="1m",
        )
        for index, price in enumerate(prices)
    ]


def test_previous_day_area_uses_the_completed_prior_trade_day_and_70_percent():
    first_day = _bars(datetime(2026, 7, 15, 13, 30, tzinfo=UTC), [100, 100.25, 101])
    second_day = _bars(datetime(2026, 7, 16, 13, 30, tzinfo=UTC), [102, 102.25, 102.5])
    overnight = _bars(datetime(2026, 7, 15, 20, 0, tzinfo=UTC), [150, 150.25])
    candles = first_day + second_day + overnight

    areas = calculate_previous_day_value_areas(candles, tick_size=0.25)

    assert len(areas) == 1
    area = areas[0]
    assert area["trade_date"] == "2026-07-16"
    assert area["source_trade_date"] == "2026-07-15"
    assert area["value_area_pct"] == PREVIOUS_DAY_VALUE_AREA_PCT == 0.70

    expected = VolumeProfileCalculator(
        tick_size=0.25,
        value_area_pct=PREVIOUS_DAY_VALUE_AREA_PCT,
    ).calculate(first_day)
    assert area["poc"] == expected.poc
    assert area["vah_70"] == expected.vah
    assert area["val_70"] == expected.val
    assert area["start_at"] == "2026-07-16T13:30:00+00:00"
    assert area["end_at"] == "2026-07-16T20:00:00+00:00"
    assert area["source_candles"] == len(first_day)


def test_previous_day_area_skips_empty_calendar_days():
    friday = _bars(datetime(2026, 7, 17, 13, 30, tzinfo=UTC), [100, 100.25])
    monday = _bars(datetime(2026, 7, 20, 13, 30, tzinfo=UTC), [101, 101.25])

    areas = calculate_previous_day_value_areas(friday + monday, tick_size=0.25)

    assert [(row["trade_date"], row["source_trade_date"]) for row in areas] == [
        ("2026-07-20", "2026-07-17"),
    ]
    assert all(c.timestamp.date().isoformat() in {"2026-07-17", "2026-07-20"}
               for c in friday + monday)


def test_previous_day_area_route_publishes_fixed_70_percent_without_changing_strategy_defaults():
    original = routes._historical_candles
    candles = (
        _bars(datetime(2026, 7, 15, 13, 30, tzinfo=UTC), [100, 100.25, 101])
        + _bars(datetime(2026, 7, 16, 13, 30, tzinfo=UTC), [102, 102.25, 102.5])
    )
    routes._historical_candles = candles
    try:
        result = asyncio.run(routes.get_previous_day_value_areas(
            start=candles[0].timestamp.isoformat(),
            end=candles[-1].timestamp.isoformat(),
            symbol="MNQ",
        ))
    finally:
        routes._historical_candles = original

    assert result["value_area_pct"] == 0.70
    assert result["source"] == "working_set"
    assert result["trade_date_boundary"] == "09:30-16:00 America/New_York"
    assert len(result["areas"]) == 1
    assert result["areas"][0]["value_area_pct"] == 0.70
