"""Chart timeframes: one aggregation definition, CME/Topstep-anchored buckets.

The former ``BacktestEngine.aggregate_1m_to_5m`` body is kept here only as a
reference oracle: the shared ``aggregate_candles(..., "5m")`` must reproduce
it exactly (it feeds the MNQ factor chart markers).
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Dict, List

import pytest

from backend.backtest.engine import BacktestEngine
from backend.data.timeframes import aggregate_candles, bucket_start, normalize_timeframe
from backend.db.models import Candle
from backend.timebase import NEW_YORK, topstep_trade_date

UTC = timezone.utc


def _legacy_5m(candles_1m: List[Candle]) -> List[Candle]:
    buckets: Dict[datetime, List[Candle]] = {}
    for c in candles_1m:
        bucket_time = c.timestamp.replace(minute=(c.timestamp.minute // 5) * 5, second=0, microsecond=0)
        buckets.setdefault(bucket_time, []).append(c)
    return [Candle(timestamp=ts, open=g[0].open, high=max(c.high for c in g), low=min(c.low for c in g),
                   close=g[-1].close, volume=sum(c.volume for c in g), symbol=g[0].symbol, interval="5m")
            for ts, g in sorted(buckets.items())]


def _bars(start: datetime, count: int, step_min: int = 1) -> List[Candle]:
    rng = random.Random(7)
    out, price = [], 20000.0
    for i in range(count):
        o = price; c = o + rng.uniform(-3, 3)
        out.append(Candle(timestamp=start + timedelta(minutes=i * step_min), open=o, high=max(o, c) + 1,
                          low=min(o, c) - 1, close=c, volume=rng.randint(1, 50), symbol="MNQ", interval="1m"))
        price = c
    return out


def _key(c: Candle):
    return (c.timestamp, c.open, c.high, c.low, c.close, c.volume, c.symbol, c.interval)


@pytest.mark.parametrize("start", [datetime(2026, 9, 24, 13, 27, tzinfo=UTC), datetime(2026, 9, 24, 13, 27)])
def test_shared_5m_reproduces_the_former_engine_helper(start):
    bars = _bars(start, 997)
    shuffled = bars[:]; random.Random(3).shuffle(shuffled)      # the old helper tolerated disorder
    for source in (bars, shuffled):
        expected = [_key(c) for c in _legacy_5m(source)]
        assert expected                                          # positive: bars were produced
        assert [_key(c) for c in aggregate_candles(source, "5m")] == expected
        assert [_key(c) for c in BacktestEngine.aggregate_1m_to_5m(source)] == expected


def test_real_store_slice_matches_former_helper():
    from backend.data import candle_store
    snap = candle_store.load_snapshot("MNQ")
    if not snap.bars:
        pytest.skip("no local MNQ store")
    sample = list(snap.bars[-5000:])
    assert [_key(c) for c in aggregate_candles(sample, "5m")] == [_key(c) for c in _legacy_5m(sample)]


def test_daily_bucket_is_the_topstep_trade_date_across_dst():
    # Sunday-evening session belongs to Monday; boundary is 17:00 Chicago.
    for sunday_evening in (datetime(2026, 3, 8, 23, 30, tzinfo=UTC), datetime(2026, 11, 1, 23, 30, tzinfo=UTC)):
        start = bucket_start(sunday_evening, "1d")
        assert topstep_trade_date(start) == topstep_trade_date(sunday_evening)
        assert start.astimezone(NEW_YORK).hour == 18 and start.astimezone(NEW_YORK).minute == 0
        assert topstep_trade_date(sunday_evening) == (sunday_evening.astimezone(NEW_YORK).date() + timedelta(days=1)).isoformat()


def test_four_hour_buckets_are_session_anchored_in_new_york():
    ny_hours = set()
    for bar in _bars(datetime(2026, 9, 22, 22, 0, tzinfo=UTC), 24 * 60):
        ny_hours.add(bucket_start(bar.timestamp, "4h").astimezone(NEW_YORK).hour)
    assert ny_hours == {18, 22, 2, 6, 10, 14}


def test_weekly_bucket_starts_sunday_evening_for_the_monday_trade_date():
    wed = datetime(2026, 9, 23, 15, 0, tzinfo=UTC)
    start = bucket_start(wed, "1w").astimezone(NEW_YORK)
    assert (start.weekday(), start.hour) == (6, 18)            # Sunday 18:00 New York


def test_higher_timeframes_conserve_ohlcv_and_never_invent_buckets():
    bars = _bars(datetime(2026, 9, 21, 22, 0, tzinfo=UTC), 3 * 1380)
    for tf in ("15m", "1h", "4h", "1d", "1w"):
        agg = aggregate_candles(bars, tf)
        assert agg and all(a.timestamp < b.timestamp for a, b in zip(agg, agg[1:]))
        assert sum(c.volume for c in agg) == sum(c.volume for c in bars)
        assert max(c.high for c in agg) == max(c.high for c in bars)
        assert agg[0].open == bars[0].open and agg[-1].close == bars[-1].close
    # A 3-hour hole produces no 1h buckets inside it.
    holed = [b for b in bars if not (datetime(2026, 9, 22, 2, tzinfo=UTC) <= b.timestamp < datetime(2026, 9, 22, 5, tzinfo=UTC))]
    hours = {c.timestamp for c in aggregate_candles(holed, "1h")}
    assert not any(datetime(2026, 9, 22, 2, tzinfo=UTC) <= h < datetime(2026, 9, 22, 5, tzinfo=UTC) for h in hours)


def test_timeframe_names_are_validated():
    assert normalize_timeframe("1H") == "1h" and normalize_timeframe("D") == "1d"
    with pytest.raises(ValueError):
        normalize_timeframe("7m")


def test_vectorized_path_equals_scalar_path_on_real_store_and_dst():
    """aggregate_many (numpy) must equal the scalar path bar for bar."""
    from backend.data import timeframes as T
    from backend.data import candle_store

    def key(c):
        return (c.timestamp, c.open, c.high, c.low, c.close, c.volume, c.symbol, c.interval)

    samples = [
        # 2026 spring + autumn DST weeks, synthetic and tz-aware
        _bars(datetime(2026, 3, 5, 22, 0, tzinfo=UTC), 7 * 1380),
        _bars(datetime(2026, 10, 29, 22, 0, tzinfo=UTC), 7 * 1380),
    ]
    snap = candle_store.load_snapshot("MNQ")
    if snap.bars:
        samples.append(list(snap.bars[-150000:]))       # includes MNQ/NQ labelled bars
    for part in samples:
        for tf in ("5m", "15m", "30m", "1h", "4h", "1d", "1w"):
            vec = T.aggregate_many(part, [tf])[tf]
            sca = T._aggregate_unsorted(part, tf, T._bucket_function(tf))
            assert vec, tf                                # positive: bars were produced
            assert [key(c) for c in vec] == [key(c) for c in sca], tf
