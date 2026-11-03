"""The 5-second chart poll may only journal minutes of the store's contract.

2026-09-10/11: a chart still connected to U26 after the calendar roll
(2026-09-10 00:00Z, third Friday - 8 days) journaled its raw bars into the
store's Z26 minutes, leaving 133 isolated raw-U26 minutes ~295 points below
the series.  The store follows ``current_quarterly_contract_id`` per minute
(the same rule the accumulator and the continuous roll use), so the live tail
must persist only bars whose minute belongs to the connected expiry.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from backend.api import routes
from backend.db.models import Candle, current_quarterly_contract_id

UTC = timezone.utc
ROLL = datetime(2026, 9, 10, tzinfo=UTC)


def _bars(first: datetime, count: int) -> list[Candle]:
    return [
        Candle(
            timestamp=first + timedelta(minutes=i),
            open=29000.0, high=29001.0, low=28999.0, close=29000.5,
            volume=10, symbol="MNQ", interval="1m",
        )
        for i in range(count)
    ]


class _FakeClient:
    def __init__(self, bars):
        self.get_historical_bars = AsyncMock(return_value=bars)


class LiveTailContractGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # 6 minutes before the roll + 6 minutes after; the poll drops the
        # newest bar as possibly partial, so 11 closed bars remain.
        self.bars = _bars(ROLL - timedelta(minutes=6), 12)
        self.saved = {
            name: getattr(routes, name)
            for name in ("_topstepx_client", "_live_contract_id", "_candle_cache", "_pending_live_last")
        }
        routes._candle_cache = {"data": None, "time": 0}
        routes._pending_live_last = {}

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(routes, name, value)

    async def _poll(self, contract_id: str):
        routes._topstepx_client = _FakeClient(self.bars)
        routes._live_contract_id = contract_id
        with patch.object(routes, "_upsert_historical_candles"), \
                patch.object(routes, "_store_append_pending", return_value=(0, 0)) as append:
            result = await routes.get_latest_candles()
        return result, append

    def test_roll_boundary_matches_the_store_rule(self):
        # Precondition for the scenario: the calendar front flips at ROLL.
        self.assertTrue(current_quarterly_contract_id("MNQ", ROLL - timedelta(minutes=1)).endswith(".U26"))
        self.assertTrue(current_quarterly_contract_id("MNQ", ROLL).endswith(".Z26"))

    async def test_old_contract_after_roll_journals_only_its_pre_roll_minutes(self):
        result, append = await self._poll("CON.F.US.MNQ.U26")
        self.assertEqual(result["count"], 11)          # chart still shows all of them
        append.assert_called_once()
        persisted, symbol, _base = append.call_args.args
        self.assertEqual(symbol, "MNQ")
        self.assertEqual([c.timestamp for c in persisted],
                         [b.timestamp for b in self.bars[:6]])   # positive: pre-roll U26 kept
        self.assertTrue(all(c.timestamp < ROLL for c in persisted))

    async def test_new_contract_journals_only_its_post_roll_minutes(self):
        _result, append = await self._poll("CON.F.US.MNQ.Z26")
        append.assert_called_once()
        persisted = append.call_args.args[0]
        self.assertEqual([c.timestamp for c in persisted],
                         [b.timestamp for b in self.bars[6:11]])
        self.assertTrue(all(c.timestamp >= ROLL for c in persisted))

    async def test_a_wholly_foreign_window_journals_nothing(self):
        self.bars = _bars(ROLL + timedelta(hours=2), 12)     # all minutes are Z26
        result, append = await self._poll("CON.F.US.MNQ.U26")
        self.assertEqual(result["count"], 11)
        append.assert_not_called()


if __name__ == "__main__":
    unittest.main()
