import asyncio
from datetime import datetime, timedelta, timezone
import time
import unittest
from unittest.mock import patch

from backend.api import routes
from backend.api.routes import DetectZonesRequest
from backend.db.models import Candle


class _Detector:
    def __init__(self):
        self.updates = []

    def update(self, candle):
        self.updates.append(candle)

    def get_all_zones(self):
        return []


class ChartZoneCacheTests(unittest.TestCase):
    def setUp(self):
        routes._chart_zone_cache.clear()

    def _candles(self, count):
        start = datetime(2026, 6, 1, tzinfo=timezone.utc)
        return [
            Candle(
                start + timedelta(minutes=i),
                100 + i,
                101 + i,
                99 + i,
                100 + i,
                1,
                interval="1m",
            )
            for i in range(count)
        ]

    def test_detector_advances_only_new_candles(self):
        created = []

        def build(**_kwargs):
            detector = _Detector()
            created.append(detector)
            return detector

        candles = self._candles(3)
        with patch("backend.strategy.consolidation.build_zone_detector", side_effect=build):
            routes._detect_zones_sync(candles[:2], ("5m",), 0.8)
            routes._detect_zones_sync(candles, ("5m",), 0.8)

        self.assertEqual(len(created), 1)
        self.assertEqual(len(created[0].updates), 3)

    def test_changed_cached_candle_rebuilds_detector(self):
        created = []

        def build(**_kwargs):
            detector = _Detector()
            created.append(detector)
            return detector

        candles = self._candles(3)
        with patch("backend.strategy.consolidation.build_zone_detector", side_effect=build):
            routes._detect_zones_sync(candles[:2], ("5m",), 0.8)
            candles[1].close += 1
            routes._detect_zones_sync(candles, ("5m",), 0.8)

        self.assertEqual(len(created), 2)
        self.assertEqual(len(created[1].updates), 3)


class ChartZoneRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_zone_detection_does_not_block_event_loop(self):
        original = routes._historical_candles
        routes._historical_candles = ChartZoneCacheTests()._candles(1)
        try:
            with patch(
                "backend.api.routes._detect_zones_sync",
                side_effect=lambda *_args: (time.sleep(0.05), [])[1],
            ):
                task = asyncio.create_task(
                    routes.detect_zones(DetectZonesRequest(all_timeframes=True))
                )
                await asyncio.sleep(0.01)
                self.assertFalse(task.done())
                result = await task
        finally:
            routes._historical_candles = original

        self.assertEqual(result["zones"], [])


if __name__ == "__main__":
    unittest.main()
