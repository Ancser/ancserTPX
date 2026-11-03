"""Candle is slotted (1.3.0) without changing the persistent pickle format.

A full MNQ store is ~2.4M candles.  With a per-instance __dict__ one load cost
~1.7 KB per bar (+4 GB RSS); slots plus shared low-cardinality values brought
it to ~0.55 KB.  These tests pin the three things that change had to keep:
old stores still load (with the old defaults), the on-disk format is still a
plain field dict (so an older build can read a store this build wrote), and
repeated values are shared rather than copied per bar.
"""
import copy
import dataclasses
import pickle
from datetime import datetime, timezone

import pytest

from backend.db.models import Candle

TS = datetime(2026, 9, 25, 20, 59, tzinfo=timezone.utc)


class _OldPickle:
    """Pickles exactly like a Candle written before a field existed."""

    def __init__(self, state):
        self.state = state

    def __reduce__(self):
        return object.__new__, (Candle,), self.state


def _old_bytes(**state):
    return pickle.dumps(_OldPickle(state), protocol=pickle.HIGHEST_PROTOCOL)


def test_candle_has_no_per_instance_dict():
    candle = Candle(TS, 1.0, 2.0, 0.5, 1.5, 10)
    assert not hasattr(candle, "__dict__")
    with pytest.raises(AttributeError):
        candle.not_a_field = 1


def test_pre_source_pickle_loads_with_the_old_class_defaults():
    # A store written before `source` / `contract_id` / `price_adjustment`.
    bar = pickle.loads(_old_bytes(timestamp=TS, open=1.0, high=2.0, low=0.5,
                                  close=1.5, volume=7, symbol="MNQ", interval="1m"))
    assert (bar.timestamp, bar.open, bar.high, bar.low, bar.close, bar.volume) == (
        TS, 1.0, 2.0, 0.5, 1.5, 7)
    assert (bar.symbol, bar.interval) == ("MNQ", "1m")
    assert bar.source == "topstepx"
    assert bar.contract_id is None and bar.price_adjustment is None


def test_pickle_state_is_still_a_plain_field_dict():
    candle = Candle(TS, 1.0, 2.0, 0.5, 1.5, 10, "MNQ", "1m", "databento",
                    "CON.F.US.MNQ.Z26", 65.5)
    reduced = candle.__reduce_ex__(pickle.HIGHEST_PROTOCOL)
    state = reduced[2]
    assert isinstance(state, dict)
    assert state == {f.name: getattr(candle, f.name) for f in dataclasses.fields(Candle)}


def test_round_trip_and_copies_preserve_every_field():
    candle = Candle(TS, 1.0, 2.0, 0.5, 1.5, 10, "MNQ", "1m", "databento",
                    "CON.F.US.MNQ.Z26", 65.5)
    for clone in (pickle.loads(pickle.dumps(candle)), copy.copy(candle),
                  copy.deepcopy(candle), dataclasses.replace(candle)):
        assert clone == candle
        assert clone is not candle


def test_slot_state_tuple_form_also_loads():
    # The shape Python's default reducer produces for slotted objects.
    bar = Candle.__new__(Candle)
    bar.__setstate__((None, {"timestamp": TS, "open": 1.0, "high": 2.0,
                             "low": 0.5, "close": 1.5, "volume": 3}))
    assert bar.close == 1.5 and bar.source == "topstepx" and bar.symbol == "NQ"


def test_unpickled_bars_share_repeated_values():
    rows = [Candle(TS, 1.0, 2.0, 0.5, 1.5, 10, "MNQ", "1m", "databento",
                   "CON.F.US.MNQ.Z26", 65.5) for _ in range(3)]
    # Give every bar its own equal-but-distinct objects, as a store assembled
    # from many sources would have.
    for row in rows:
        row.contract_id = "".join(["CON.F.US.MNQ.", "Z26"])
        row.price_adjustment = float("65.5")
    assert rows[0].contract_id is not rows[1].contract_id
    loaded = [pickle.loads(pickle.dumps(row)) for row in rows]
    assert loaded[0].contract_id is loaded[1].contract_id is loaded[2].contract_id
    assert loaded[0].price_adjustment is loaded[1].price_adjustment
    assert loaded[0].source is loaded[2].source
    # Sharing must not merge values of different types.
    zero_int = pickle.loads(pickle.dumps(Candle(TS, 1, 1, 1, 1, 1, price_adjustment=0)))
    zero_float = pickle.loads(pickle.dumps(Candle(TS, 1, 1, 1, 1, 1, price_adjustment=0.0)))
    assert type(zero_int.price_adjustment) is int
    assert type(zero_float.price_adjustment) is float


def test_a_store_written_now_loads_in_the_pre_slots_class(monkeypatch):
    """Rolling back to an older build must still read today's store."""
    import backend.db.models as models

    @dataclasses.dataclass
    class OldCandle:                       # the pre-1.3.0 shape
        timestamp: datetime
        open: float
        high: float
        low: float
        close: float
        volume: int
        symbol: str = "NQ"
        interval: str = "5m"
        source: str = "topstepx"

    payload = pickle.dumps([Candle(TS, 1.0, 2.0, 0.5, 1.5, 10, "MNQ", "1m",
                                   "databento", "CON.F.US.MNQ.Z26", 65.5)])
    OldCandle.__qualname__ = "Candle"
    OldCandle.__module__ = models.__name__
    monkeypatch.setattr(models, "Candle", OldCandle)
    (bar,) = pickle.loads(payload)
    assert type(bar) is OldCandle
    assert (bar.close, bar.symbol, bar.source) == (1.5, "MNQ", "databento")
    assert bar.contract_id == "CON.F.US.MNQ.Z26"   # extra fields ride along
