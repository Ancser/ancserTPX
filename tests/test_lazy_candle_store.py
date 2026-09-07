from __future__ import annotations

import tempfile
import unittest
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from backend.data import accumulator
from backend.data import candle_store as cs
from backend.db.models import Candle


UTC = timezone.utc


def _bars(count: int, start: datetime | None = None) -> list[Candle]:
    first = start or datetime(2026, 8, 1, tzinfo=UTC)
    return [
        Candle(
            timestamp=first + timedelta(minutes=i),
            open=20000 + i,
            high=20001 + i,
            low=19999 + i,
            close=20000.5 + i,
            volume=100 + i,
            symbol="MNQ",
            interval="1m",
        )
        for i in range(count)
    ]


class LazyCandleJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_store = cs.STORE_DIR
        cs.STORE_DIR = Path(self.tmp.name)
        cs.invalidate_cache()

    def tearDown(self):
        cs.STORE_DIR = self.original_store
        cs.invalidate_cache()
        self.tmp.cleanup()

    def test_append_pending_never_opens_the_canonical_pickle(self):
        bars = _bars(3)
        with patch.object(
            cs, "load_snapshot",
            side_effect=AssertionError("pending save must not load canonical store"),
        ):
            pending_count, changed = cs.append_pending(bars, "MNQ", 1)

        self.assertEqual((pending_count, changed), (3, 3))
        self.assertFalse(cs.has_persistent_store("MNQ", 1))
        status = cs.pending_status("MNQ", 1)
        self.assertEqual(status["count"], 3)
        self.assertEqual(status["first"], bars[0].timestamp)
        self.assertEqual(status["last"], bars[-1].timestamp)

    def test_pending_is_deduplicated_and_revisions_replace_by_timestamp(self):
        bars = _bars(3)
        cs.append_pending(bars, "MNQ", 1)
        revision = _bars(1, bars[1].timestamp)
        revision[0].close = 20999.0

        pending_count, changed = cs.append_pending(
            [revision[0], bars[2]], "MNQ", 1
        )

        self.assertEqual(pending_count, 3)
        self.assertEqual(changed, 1)
        stored = cs._read_pending_locked("MNQ", 1)
        self.assertEqual(len(stored), 3)
        self.assertEqual(stored[1].close, 20999.0)

    def test_explicit_merge_flushes_pending_into_canonical_store(self):
        initial = _bars(2)
        cs.save(initial, "MNQ", 1)
        pending = _bars(2, initial[-1].timestamp + timedelta(minutes=1))
        cs.append_pending(pending, "MNQ", 1)

        total, added = cs.merge_pending("MNQ", 1)

        self.assertEqual((total, added), (4, 2))
        self.assertEqual(len(cs.load("MNQ", 1)), 4)
        self.assertEqual(cs.pending_status("MNQ", 1)["count"], 0)
        self.assertTrue(cs.has_persistent_store("MNQ", 1))

    def test_lightweight_status_uses_bounds_without_unpickling(self):
        bars = _bars(2)
        cs.save(bars, "MNQ", 1)
        with patch.object(
            cs, "load_snapshot",
            side_effect=AssertionError("status must not load canonical store"),
        ):
            status = cs.lightweight_status("MNQ", 1)

        self.assertEqual(status["bars"], 2)
        self.assertEqual(status["first"], bars[0].timestamp)
        self.assertEqual(status["last"], bars[-1].timestamp)
        self.assertTrue(status["canonical_exists"])

    def test_missing_canonical_store_uses_seed_metadata_not_stale_accumulated_meta(self):
        seed_dir = cs.STORE_DIR / "seed"
        seed_dir.mkdir(parents=True)
        (seed_dir / "MNQ_seed_1m.pkl").write_bytes(b"seed")
        (seed_dir / "MNQ_seed_1m.meta.json").write_text(
            json.dumps({"role": "seed", "total_bars": 3}), encoding="utf-8"
        )
        (cs.STORE_DIR / "MNQ_accumulated_1m.meta.json").write_text(
            json.dumps({"role": "private-full", "total_bars": 2_359_843}),
            encoding="utf-8",
        )

        meta = cs.load_meta("MNQ", 1)

        self.assertEqual(meta["role"], "seed")
        self.assertEqual(meta["total_bars"], 3)

    def test_mes_is_not_active_until_an_es_contract_is_used(self):
        with accumulator._ACTIVE_SYMBOLS_LOCK:
            original = set(accumulator._ACTIVE_SYMBOLS)
            accumulator._ACTIVE_SYMBOLS.clear()
            accumulator._ACTIVE_SYMBOLS.update({"MNQ"})
        try:
            self.assertEqual(accumulator.active_symbols(), ("MNQ",))
            self.assertEqual(accumulator.activate_symbol("ES"), "MES")
            self.assertEqual(accumulator.active_symbols(), ("MES", "MNQ"))
        finally:
            with accumulator._ACTIVE_SYMBOLS_LOCK:
                accumulator._ACTIVE_SYMBOLS.clear()
                accumulator._ACTIVE_SYMBOLS.update(original)


if __name__ == "__main__":
    unittest.main()
