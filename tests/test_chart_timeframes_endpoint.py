"""/data/candles?tf=...: store history + live tail, one aggregation definition."""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from backend.api import routes
from backend.data.timeframes import aggregate_candles
from backend.db.models import Candle

UTC = timezone.utc


def _bars(start: datetime, count: int, base: float = 29000.0) -> list[Candle]:
    return [
        Candle(timestamp=start + timedelta(minutes=i), open=base + i, high=base + i + 2,
               low=base + i - 2, close=base + i + 1, volume=1 + i % 7, symbol="MNQ", interval="1m")
        for i in range(count)
    ]


def _body(response):
    return json.loads(response.body)


class ChartTimeframeEndpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.saved = (routes._historical_candles, routes._live_contract_id, dict(routes._chart_tf_cache))
        routes._chart_tf_cache.clear()
        routes._live_contract_id = "CON.F.US.MNQ.Z26"
        # Store: 2026-09-21 13:00 -> 18:59 UTC; live tail: 19:00 -> 19:44 UTC.
        self.store = _bars(datetime(2026, 9, 21, 13, 0, tzinfo=UTC), 360)
        self.tail = _bars(datetime(2026, 9, 21, 19, 0, tzinfo=UTC), 45, base=30000.0)
        routes._historical_candles = self.store[-30:] + self.tail   # overlaps the store's end

    def tearDown(self):
        routes._historical_candles, routes._live_contract_id, cache = self.saved
        routes._chart_tf_cache.clear(); routes._chart_tf_cache.update(cache)

    async def _get(self, **kwargs):
        snapshot = SimpleNamespace(bars=tuple(self.store), version=("v", 1))
        with patch.object(routes, "_store_merge_pending",
                          side_effect=AssertionError("a chart read must not merge/write the store")), \
                patch.object(routes, "_store_load_view", return_value=snapshot):
            return _body(await routes.get_stored_candles(**kwargs))

    async def test_hourly_bars_cover_store_and_live_tail_exactly(self):
        body = await self._get(tf="1h")
        expected = aggregate_candles(self.store + self.tail, "1h")
        self.assertEqual(body["timeframe"], "1h")
        self.assertEqual(len(body["candles"]), 7)                  # 13:00 .. 19:00
        self.assertEqual([r["time"] for r in body["candles"]], [c.timestamp.isoformat() for c in expected])
        last = body["candles"][-1]
        self.assertEqual((last["open"], last["close"], last["volume"]),
                         (expected[-1].open, expected[-1].close, expected[-1].volume))
        self.assertEqual(sum(r["volume"] for r in body["candles"]),
                         sum(c.volume for c in self.store + self.tail))   # overlap not double-counted

    async def test_windowed_frame_pages_left_into_older_store_history(self):
        body = await self._get(tf="5m", limit=10)
        self.assertEqual(len(body["candles"]), 10)
        self.assertTrue(body["has_more_before"])
        older = await self._get(tf="5m", limit=10, before=body["candles"][0]["time"])
        self.assertEqual(len(older["candles"]), 10)
        self.assertLess(older["candles"][-1]["time"], body["candles"][0]["time"])

    async def test_one_minute_request_keeps_the_existing_working_set_path(self):
        body = await self._get(limit=5)
        self.assertEqual(body["timeframe"], "1m")
        self.assertEqual(body["source"], "working_set")
        self.assertEqual(body["candles"][-1]["time"], self.tail[-1].timestamp.isoformat())

    async def test_unknown_timeframe_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            await self._get(tf="7m")
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
