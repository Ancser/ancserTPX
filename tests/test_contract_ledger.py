"""Contract identity of the continuous 1m store (1.3.0).

The two incidents this pins down:

* 2026-08-31 seam: a batch already back-adjusted to Z26 (+295) was journaled
  into a store still on U26, leaving a +295 step inside the series.
* 2026-09-10/11 mix: after the store rolled to Z26, a chart still on U26
  journaled raw U26 minutes into it, ~295 points low.

The contract ledger owns "which contract, at what price level" for every
minute and every writer goes through it.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from backend.data import accumulator
from backend.data import candle_store as cs
from backend.data import contract_ledger as L
from backend.db.models import Candle

UTC = timezone.utc
ROLL_U26_Z26 = datetime(2026, 9, 10, tzinfo=UTC)
ROLLS = [
    {"at": datetime(2026, 3, 19, tzinfo=UTC), "from": "H26", "to": "M26", "offset": 211.5, "basis": "databento"},
    {"at": datetime(2026, 6, 17, tzinfo=UTC), "from": "M26", "to": "U26", "offset": 316.0, "basis": "databento"},
    {"at": ROLL_U26_Z26, "from": "U26", "to": "Z26", "offset": 295.0, "basis": "verified"},
]


def _bar(ts, price, contract=None, adj=None, volume=5):
    return Candle(timestamp=ts, open=price, high=price + 1, low=price - 1, close=price,
                  volume=volume, symbol="MNQ", interval="1m", contract_id=contract, price_adjustment=adj)


def _minutes(start, n, price, contract=None, adj=None):
    return [_bar(start + timedelta(minutes=i), price + i * 0.25, contract, adj) for i in range(n)]


class LedgerRuleTests(unittest.TestCase):
    def test_contract_names_and_sequence(self):
        self.assertEqual(L.next_contract("CON.F.US.MNQ.Z26"), "CON.F.US.MNQ.H27")
        self.assertEqual(L.next_contract("U26"), "Z26")
        self.assertEqual(L.expiry_code("CON.F.US.ENQ.Z26"), "Z26")
        with self.assertRaises(ValueError):
            L.expiry_code("CON.F.US.MNQ")

    def test_owner_and_back_adjustment_of_each_contract(self):
        rolls = L.normalize_rolls(ROLLS)
        self.assertEqual(L.contract_at(rolls, datetime(2026, 1, 5, tzinfo=UTC)), "H26")
        self.assertEqual(L.contract_at(rolls, ROLL_U26_Z26 - timedelta(minutes=1)), "U26")
        self.assertEqual(L.contract_at(rolls, ROLL_U26_Z26), "Z26")
        self.assertEqual(L.adjustment_for(rolls, "Z26"), 0.0)
        self.assertEqual(L.adjustment_for(rolls, "U26"), 295.0)
        self.assertEqual(L.adjustment_for(rolls, "M26"), 611.0)
        self.assertEqual(L.adjustment_for(rolls, "H26"), 822.5)
        self.assertIsNone(L.adjustment_for(rolls, "H27"))

    def test_roll_offset_uses_first_shared_minute_after_the_roll(self):
        old = _minutes(ROLL_U26_Z26 - timedelta(minutes=3), 6, 29000.0)
        new = [_bar(b.timestamp, b.close + 295.0) for b in old if b.timestamp.minute != 0]
        offset, anchor = L.roll_offset(old, new, ROLL_U26_Z26)
        self.assertEqual(offset, 295.0)
        self.assertEqual(anchor, ROLL_U26_Z26 + timedelta(minutes=1))   # 00:00 had no new-contract trade

    def test_enforce_drops_wrong_contract_and_reexpresses_adjusted_bars(self):
        rolls = L.normalize_rolls(ROLLS)
        before = ROLL_U26_Z26 - timedelta(hours=1)
        bars = [
            _bar(before, 29000.0, "CON.F.US.MNQ.U26", 0.0),            # raw U26 -> +295
            _bar(before + timedelta(minutes=1), 29295.0, "CON.F.US.MNQ.U26", 295.0),  # already Z26 level
            _bar(before + timedelta(minutes=2), 29300.0, "CON.F.US.MNQ.Z26", 0.0),    # Z26 before roll: wrong
            _bar(ROLL_U26_Z26 + timedelta(minutes=5), 29000.0, "CON.F.US.MNQ.U26", 0.0),  # U26 after roll: wrong
            _bar(ROLL_U26_Z26 + timedelta(minutes=6), 29400.0, "CON.F.US.MNQ.Z26", 0.0),
            _bar(before + timedelta(minutes=3), 1.0),                      # legacy unstamped: untouched
        ]
        kept, dropped, reexpressed = L.enforce(bars, rolls)
        self.assertEqual(dropped, 2)
        self.assertEqual(reexpressed, 1)
        self.assertEqual([(b.close, b.price_adjustment) for b in kept],
                         [(29295.0, 295.0), (29295.0, 295.0), (29400.0, 0.0), (1.0, None)])

    def test_empty_ledger_changes_nothing(self):
        bars = _minutes(ROLL_U26_Z26, 3, 29000.0, "CON.F.US.MNQ.H27", 0.0)
        kept, dropped, reexpressed = L.enforce(bars, [])
        self.assertEqual((len(kept), dropped, reexpressed), (3, 0, 0))


class StoreWriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_store = cs.STORE_DIR
        cs.STORE_DIR = Path(self.tmp.name)
        cs.invalidate_cache()
        # Store on U26 (level of U26 = newest in this ledger), history before.
        history = _minutes(ROLL_U26_Z26 - timedelta(hours=2), 60, 29000.0, "CON.F.US.MNQ.U26", 0.0)
        cs.save(history, "MNQ")
        meta = cs.load_meta("MNQ")
        meta["contract_rolls"] = L.rolls_to_meta(ROLLS[:2])       # not rolled to Z26 yet
        cs.save_meta(meta, "MNQ")

    def tearDown(self):
        cs.STORE_DIR = self.original_store
        cs.invalidate_cache()
        self.tmp.cleanup()

    def test_pending_journal_keeps_identity_and_store_level(self):
        # The 08-31 bug class: a continuous batch already adjusted to Z26.
        batch = _minutes(ROLL_U26_Z26 - timedelta(minutes=30), 10, 29295.0, "CON.F.US.MNQ.U26", 295.0)
        count, changed = cs.append_pending(batch, "MNQ")
        self.assertEqual(changed, 10)
        journal = cs._read_pending_locked("MNQ", 1)
        self.assertEqual({b.contract_id for b in journal}, {"CON.F.US.MNQ.U26"})
        self.assertEqual({b.price_adjustment for b in journal}, {0.0})
        self.assertEqual(journal[0].close, 29000.0)                # re-expressed on the U26 store level

    def test_wrong_contract_minutes_never_reach_the_store(self):
        # The 09-10 bug class: raw bars of another contract for the same minutes.
        mixed = _minutes(ROLL_U26_Z26 - timedelta(minutes=20), 5, 29300.0, "CON.F.US.MNQ.Z26", 0.0)
        count, changed = cs.append_pending(mixed, "MNQ")
        self.assertEqual(changed, 0)
        total, added = cs.merge(mixed, "MNQ")
        self.assertEqual(added, 0)

    def test_apply_roll_back_adjusts_history_once_and_clears_old_contract_after_roll(self):
        stale_after = _minutes(ROLL_U26_Z26 + timedelta(minutes=1), 3, 29010.0, "CON.F.US.MNQ.U26", 0.0)
        cs.merge(stale_after, "MNQ")                               # allowed: ledger still on U26
        result = cs.apply_contract_roll("MNQ", ROLL_U26_Z26, "CON.F.US.MNQ.U26",
                                        "CON.F.US.MNQ.Z26", 295.0, "test")
        self.assertTrue(result["applied"])
        self.assertEqual(result["removed"], 3)
        bars = cs.load("MNQ", use_cache=False)
        self.assertTrue(all(b.timestamp < ROLL_U26_Z26 for b in bars))
        self.assertEqual(bars[0].close, 29295.0)
        self.assertEqual(bars[0].price_adjustment, 295.0)
        self.assertEqual(cs.store_contract_at("MNQ", ROLL_U26_Z26), "CON.F.US.MNQ.Z26")
        again = cs.apply_contract_roll("MNQ", ROLL_U26_Z26, "CON.F.US.MNQ.U26",
                                       "CON.F.US.MNQ.Z26", 295.0, "test")
        self.assertFalse(again["applied"])
        self.assertEqual(cs.load("MNQ", use_cache=False)[0].close, 29295.0)   # not shifted twice


class AccumulatorRollTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_store = cs.STORE_DIR
        cs.STORE_DIR = Path(self.tmp.name)
        cs.invalidate_cache()
        self.z26_roll = datetime(2026, 12, 10, tzinfo=UTC)        # Z26 calendar roll
        cs.save(_minutes(self.z26_roll - timedelta(hours=1), 50, 29500.0, "CON.F.US.MNQ.Z26", 0.0), "MNQ")
        meta = cs.load_meta("MNQ")
        meta["contract_rolls"] = L.rolls_to_meta(ROLLS)
        cs.save_meta(meta, "MNQ")

    def tearDown(self):
        cs.STORE_DIR = self.original_store
        cs.invalidate_cache()
        self.tmp.cleanup()

    def test_store_rolls_when_the_calendar_roll_passes_and_then_fetches_the_new_contract(self):
        z26 = _minutes(self.z26_roll - timedelta(minutes=10), 40, 29500.0, "CON.F.US.MNQ.Z26", 0.0)
        h27 = [_bar(b.timestamp, b.close + 250.0, "CON.F.US.MNQ.H27", 0.0) for b in z26]

        async def bars_for(cid, *args, **kwargs):
            return list(h27 if cid.endswith("H27") else z26)

        client = type("C", (), {})()
        client.get_historical_bars_paginated = AsyncMock(side_effect=bars_for)
        now = self.z26_roll + timedelta(minutes=29)
        # The store's own cursor is past the roll; the roll must widen it back.
        got = asyncio.run(accumulator._fetch(client, "MNQ", now - timedelta(minutes=5), now, log=lambda *_: None))
        starts = [call.kwargs.get("start_time") for call in client.get_historical_bars_paginated.await_args_list]
        self.assertIn(self.z26_roll.strftime("%Y-%m-%dT%H:%M:%SZ"), starts)
        self.assertEqual(cs.store_contract_at("MNQ", now), "CON.F.US.MNQ.H27")
        self.assertEqual(cs.contract_rolls("MNQ")[-1]["offset"], 250.0)
        history = cs.load("MNQ", use_cache=False)
        self.assertEqual(history[0].close, 29750.0)                # back-adjusted onto H27
        self.assertTrue(got and all(b.contract_id.endswith("H27") for b in got if b.timestamp >= self.z26_roll))
        count, changed = cs.append_pending(got, "MNQ")
        self.assertGreater(changed, 0)
        self.assertTrue(all(b.contract_id.endswith("H27") for b in cs._read_pending_locked("MNQ", 1)))

    def test_no_roll_before_the_calendar_roll(self):
        client = type("C", (), {})()
        client.get_historical_bars_paginated = AsyncMock(return_value=[])
        now = self.z26_roll - timedelta(days=1)
        asyncio.run(accumulator._fetch(client, "MNQ", now - timedelta(hours=1), now, log=lambda *_: None))
        self.assertEqual(cs.store_contract_at("MNQ", now), "CON.F.US.MNQ.Z26")
        self.assertEqual(len(cs.contract_rolls("MNQ")), 3)


if __name__ == "__main__":
    unittest.main()
