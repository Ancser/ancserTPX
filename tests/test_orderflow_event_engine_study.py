from datetime import datetime, timedelta

from backend.db.models import Candle, StrategyParams, Direction
from backend.timebase import UTC
from scripts.orderflow_event_engine_study import EventStrategy, map_events


def test_conflicting_events_abstain_and_never_map_early():
    mapped, conflicts = map_events([(61, 1), (65, -1), (121, 1)], [60, 120, 180])
    assert mapped == {(180, 1)}
    assert conflicts == 1


def test_event_availability_and_pi_exit_geometry(monkeypatch):
    monkeypatch.setattr("backend.strategy.pi_signal._load_history", lambda *a: [])
    start = datetime(2026, 8, 10, 14, 0, tzinfo=UTC)
    due = start + timedelta(minutes=150)
    params = StrategyParams(strategy="pi", contract_id="MNQ", pi_long_only=False,
                            pi_long_kinds=["青π"], pi_short_kinds=["粉π"],
                            factor_sl_value=4, pi_short_sl_value=1.5, rr_ratio=3)
    strategy = EventStrategy(params, [(int(due.timestamp()), -1)])
    def bar(t):
        return Candle(timestamp=t, open=100, high=102, low=98, close=100, volume=10)
    for minute in range(150):
        assert strategy.evaluate(bar(start + timedelta(minutes=minute))) is None
    signal = strategy.evaluate(bar(due))
    assert signal is not None
    assert signal.direction == Direction.SELL
    assert signal.sl_price == 106
    assert signal.tp_price == 82
    assert strategy.evaluate(bar(due + timedelta(minutes=1))) is None


def test_event_blocked_for_a_minute_is_not_replayed(monkeypatch):
    monkeypatch.setattr("backend.strategy.pi_signal._load_history", lambda *a: [])
    due = datetime(2026, 8, 10, 15, 0, tzinfo=UTC)
    strategy = EventStrategy(StrategyParams(strategy="pi"), [(int(due.timestamp()), 1)])
    candle = Candle(timestamp=due + timedelta(minutes=1), open=100,
                    high=102, low=98, close=100, volume=10)
    assert strategy.evaluate(candle) is None
    assert strategy.pending() == 0
    assert strategy.next_event is None
