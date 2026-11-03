"""Columnar store sidecar and read-only chart view (DATA-016).

The pickle stays canonical; the ``.columns.npz`` sidecar is derived.  These
tests pin: exact round trip of every Candle field, a stale or corrupt sidecar
can never serve different bars than the pickle, chart reads overlay the
pending journal without writing anything, and the columnar aggregation equals
the Candle-list aggregation.
"""
from __future__ import annotations

import os
import pickle
import tempfile
import time
import unittest
import unittest.mock
from datetime import datetime, timedelta
from pathlib import Path

from backend.data import candle_store as cs
from backend.data.columnar_store import CandleColumns, ColumnarBars, sidecar_path
from backend.data.timeframes import aggregate_columns, aggregate_many
from backend.db.models import Candle
from backend.timebase import UTC

START = datetime(2026, 9, 8, 22, 0, tzinfo=UTC)
FIELDS = ("timestamp", "open", "high", "low", "close", "volume", "symbol",
          "interval", "source", "contract_id", "price_adjustment")


def _bars(count, start=START, base=20000.0, contract="CON.F.US.MNQ.Z26", adjustment=0.0):
    return [
        Candle(start + timedelta(minutes=i), base + i, base + i + 2.25, base + i - 1.5,
               base + i + 0.75, 100 + i, "MNQ", "1m", "databento", contract, adjustment)
        for i in range(count)
    ]


def _same(a, b):
    return all(getattr(a, f) == getattr(b, f) for f in FIELDS)


class ColumnsRoundTripTests(unittest.TestCase):
    def test_every_field_round_trips_including_unknowns_and_mixed_tables(self):
        bars = _bars(5) + _bars(3, start=START + timedelta(minutes=5), contract=None,
                                adjustment=None)
        bars[1].symbol = "NQ"
        bars[2].source = "topstepx"
        columns = CandleColumns.from_bars(bars)
        again = columns.candles()
        self.assertEqual(len(again), 8)
        for a, b in zip(bars, again):
            self.assertTrue(_same(a, b), (a, b))
        self.assertIsNone(again[-1].contract_id)
        self.assertIsNone(again[-1].price_adjustment)
        self.assertIs(type(again[0].volume), int)

    def test_sidecar_is_stamped_with_the_pickle_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.columns.npz"
            CandleColumns.from_bars(_bars(10)).save(path, (111, 222))
            self.assertIsNotNone(CandleColumns.load(path, (111, 222)))
            self.assertIsNone(CandleColumns.load(path, (111, 223)))     # other generation
            path.write_bytes(b"not a zip")
            self.assertIsNone(CandleColumns.load(path, (111, 222)))     # corrupt

    def test_columns_reject_unsorted_bars_and_are_read_only(self):
        bars = _bars(3)
        with self.assertRaises(ValueError):
            CandleColumns.from_bars([bars[1], bars[0]])
        columns = CandleColumns.from_bars(bars)
        with self.assertRaises(ValueError):
            columns.close[0] = 1.0
        view = ColumnarBars(columns)
        with self.assertRaises(TypeError):
            view[0] = bars[0]

    def test_appended_keeps_old_rows_and_extends_the_tables(self):
        first = CandleColumns.from_bars(_bars(4))
        later = _bars(2, start=START + timedelta(minutes=4), contract="CON.F.US.MNQ.H27",
                      adjustment=12.5)
        both = first.appended(later).candles()
        self.assertEqual([b.contract_id for b in both][-3:],
                         ["CON.F.US.MNQ.Z26", "CON.F.US.MNQ.H27", "CON.F.US.MNQ.H27"])
        self.assertEqual(both[-1].price_adjustment, 12.5)
        with self.assertRaises(ValueError):
            first.appended(_bars(1))                        # not newer than the last row


class StoreSidecarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = cs.STORE_DIR
        cs.STORE_DIR = Path(self.tmp.name)
        cs.invalidate_cache()

    def tearDown(self):
        cs.STORE_DIR = self.original
        cs.invalidate_cache()
        self.tmp.cleanup()

    def _pickle_path(self):
        return cs._store_path("MNQ", 1)

    def test_save_writes_a_sidecar_that_a_cold_load_uses_instead_of_the_pickle(self):
        bars = _bars(50)
        cs.save(bars, "MNQ", 1)
        self.assertTrue(sidecar_path(self._pickle_path()).exists())
        cs.invalidate_cache()
        real_load = pickle.load
        with unittest.mock.patch.object(cs.pickle, "load",
                                        side_effect=AssertionError("pickle must not be decoded")):
            snapshot = cs.load_snapshot("MNQ", 1)
        self.assertIsInstance(snapshot.bars, ColumnarBars)
        self.assertEqual(len(snapshot.bars), 50)
        self.assertTrue(all(_same(a, b) for a, b in zip(bars, snapshot.bars)))
        self.assertIs(pickle.load, real_load)

    def test_a_pickle_rewritten_by_another_writer_is_never_hidden_by_the_old_sidecar(self):
        cs.save(_bars(20), "MNQ", 1)
        cs.invalidate_cache()
        # Another tool rewrites the canonical pickle directly (no sidecar).
        replacement = _bars(7, base=30000.0)
        time.sleep(0.01)
        with self._pickle_path().open("wb") as fh:
            pickle.dump(replacement, fh)
        os.utime(self._pickle_path(), None)
        snapshot = cs.load_snapshot("MNQ", 1)
        self.assertEqual(len(snapshot.bars), 7)
        self.assertEqual(snapshot.bars[0].open, 30000.0)
        # ... and the rebuilt sidecar now matches the new generation.
        version = cs._path_version(self._pickle_path())
        self.assertIsNotNone(CandleColumns.load(sidecar_path(self._pickle_path()), version))

    def test_chart_view_overlays_newer_pending_bars_without_writing(self):
        stored = _bars(30)
        cs.save(stored, "MNQ", 1)
        later = _bars(5, start=START + timedelta(minutes=30), base=20030.0)
        revision = [Candle(stored[3].timestamp, 1.0, 1.0, 1.0, 1.0, 1, "MNQ", "1m",
                           "databento", "CON.F.US.MNQ.Z26", 0.0)]
        cs.append_pending(revision + later, "MNQ", 1)
        pickle_version = cs._path_version(self._pickle_path())
        pending_before = cs._pending_path("MNQ", 1).read_bytes()

        view = cs.load_view("MNQ", 1)

        self.assertEqual(len(view.bars), 35)                        # positive: tail visible
        self.assertEqual(view.bars[-1].timestamp, later[-1].timestamp)
        self.assertEqual(view.bars[3].open, stored[3].open)          # revision left to the merge
        self.assertEqual(cs._path_version(self._pickle_path()), pickle_version)
        self.assertEqual(cs._pending_path("MNQ", 1).read_bytes(), pending_before)
        self.assertIs(view, cs.load_view("MNQ", 1))                  # cached per generation
        # The real merge (backtest/store path) still applies everything.
        cs.merge_pending("MNQ", 1)
        merged = cs.load_snapshot("MNQ", 1)
        self.assertEqual(len(merged.bars), 35)
        self.assertEqual(merged.bars[3].open, 1.0)


class ColumnarAggregationTests(unittest.TestCase):
    def test_columns_aggregate_exactly_like_candles(self):
        bars = _bars(5000, start=datetime(2026, 9, 1, 21, 0, tzinfo=UTC))
        for i in range(0, 5000, 997):
            bars[i].symbol = "NQ"                                    # per-bucket first symbol
        frames = ("5m", "1h", "4h", "1d", "1w")
        expected = aggregate_many(bars, frames)
        got = aggregate_columns(CandleColumns.from_bars(bars), frames)
        for tf in frames:
            self.assertGreater(len(expected[tf]), 0, tf)
            self.assertEqual(len(got[tf]), len(expected[tf]), tf)
            for a, b in zip(expected[tf], got[tf]):
                self.assertEqual(
                    (a.timestamp, a.open, a.high, a.low, a.close, a.volume, a.symbol, a.interval),
                    (b.timestamp, b.open, b.high, b.low, b.close, b.volume, b.symbol, b.interval),
                    tf)


if __name__ == "__main__":
    unittest.main()


class SeedTests(unittest.TestCase):
    def test_loading_from_a_seed_never_writes_a_sidecar_beside_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = cs.STORE_DIR
            cs.STORE_DIR = Path(tmp)
            cs.invalidate_cache()
            try:
                seed = cs._seed_path("MNQ", 1)
                seed.parent.mkdir(parents=True, exist_ok=True)
                with seed.open("wb") as fh:
                    pickle.dump(_bars(12), fh)
                snapshot = cs.load_snapshot("MNQ", 1)
                self.assertEqual(len(snapshot.bars), 12)             # positive: seed served
                self.assertFalse(sidecar_path(seed).exists())
            finally:
                cs.STORE_DIR = original
                cs.invalidate_cache()
