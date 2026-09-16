from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from backend.db.models import Candle, Direction, StrategyParams
from backend.strategy.volume_profile import (
    PreviousRthValueAreaTracker,
    VolumeProfileStrategy,
)


NY = ZoneInfo("America/New_York")


def _bar(ts, *, close=101.0, high=101.25, low=100.75, volume=100):
    return Candle(
        timestamp=ts,
        open=close,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


def _params(**overrides):
    values = {
        "strategy": "volume_profile",
        "contract_id": "MNQ",
        "tr_allowed_sessions": ["RTH"],
        "vp_min_source_candles": 3,
        "vp_confirm_bars": 2,
    }
    values.update(overrides)
    return StrategyParams(**values)


def _warm_strategy(strategy, start):
    for minute in range(60):
        strategy.observe(
            _bar(
                start + timedelta(minutes=minute),
                close=101.0,
                high=101.25,
                low=100.75,
            ),
            [],
            True,
        )


def _levels(date="2024-01-02"):
    return {
        "date": date,
        "source_trade_date": "2024-01-01",
        "poc": 102.0,
        "vah": 104.0,
        "val": 100.0,
    }


def test_prior_rth_tracker_is_causal_and_uses_previous_session():
    start = datetime(2024, 1, 2, 9, 30, tzinfo=NY)
    tracker = PreviousRthValueAreaTracker(
        tick_size=0.25,
        value_area_pct=0.70,
        min_source_candles=3,
    )

    prior = [
        _bar(start + timedelta(minutes=i), close=100 + i * 0.25,
             high=100.25 + i * 0.25, low=99.75 + i * 0.25)
        for i in range(3)
    ]
    for candle in prior:
        assert tracker.update(candle) is None

    # The first candle of the next RTH day can see the completed prior day,
    # but its own candle is not included in that profile.
    next_day = start + timedelta(days=1)
    levels = tracker.update(_bar(next_day, close=110, high=111, low=109))
    assert levels is not None
    assert levels["date"] == "2024-01-03"
    assert levels["source_trade_date"] == "2024-01-02"
    assert levels["source_candles"] == 3


def test_range_rejection_uses_atr_blend_and_locks_the_edge():
    start = datetime(2024, 1, 2, 9, 30, tzinfo=NY)
    strategy = VolumeProfileStrategy(_params(vp_entry_mode="range"))
    strategy.set_levels(_levels())
    _warm_strategy(strategy, start)

    candle = _bar(
        start + timedelta(minutes=60),
        close=100.5,
        high=101.0,
        low=99.5,
    )
    signal = strategy.evaluate(candle, [], True)

    assert signal is not None
    assert signal.direction == Direction.BUY
    assert signal.zone_source == "volume_profile"
    assert signal.meta["setup"] == "range_rejection"
    assert signal.meta["atr_blend"] > 0
    assert signal.sl_price < signal.entry_price < signal.tp_price

    # Closing the trade does not re-arm the same edge during this profile day.
    strategy.notify_trade_closed("tp")
    later = _bar(
        start + timedelta(minutes=61),
        close=100.4,
        high=100.8,
        low=99.5,
    )
    assert strategy.evaluate(later, [], True) is None


def test_breakout_needs_confirmation_then_a_later_retest():
    start = datetime(2024, 1, 2, 9, 30, tzinfo=NY)
    strategy = VolumeProfileStrategy(_params(vp_entry_mode="breakout"))
    strategy.set_levels(_levels())
    _warm_strategy(strategy, start)

    first = _bar(start + timedelta(minutes=60), close=105.0, high=105.25, low=104.75)
    second = _bar(start + timedelta(minutes=61), close=105.0, high=105.25, low=104.75)
    assert strategy.evaluate(first, [], True) is None
    assert strategy._accepted_breakout is None
    assert strategy.evaluate(second, [], True) is None
    assert strategy._accepted_breakout == "up"

    retest = _bar(
        start + timedelta(minutes=62),
        close=105.0,
        high=105.25,
        low=104.5,
    )
    signal = strategy.evaluate(retest, [], True)
    assert signal is not None
    assert signal.direction == Direction.BUY
    assert signal.meta["setup"] == "breakout_retest"


def test_single_outside_close_and_reclaim_is_not_a_breakout():
    start = datetime(2024, 1, 2, 9, 30, tzinfo=NY)
    strategy = VolumeProfileStrategy(_params(vp_entry_mode="breakout"))
    strategy.set_levels(_levels())
    _warm_strategy(strategy, start)

    outside = _bar(start + timedelta(minutes=60), close=105.0, high=105.5, low=104.75)
    reclaim = _bar(start + timedelta(minutes=61), close=103.5, high=104.0, low=103.0)
    assert strategy.evaluate(outside, [], True) is None
    assert strategy.evaluate(reclaim, [], True) is None
    assert strategy._accepted_breakout is None
    assert strategy._failed_break_side == "up"
