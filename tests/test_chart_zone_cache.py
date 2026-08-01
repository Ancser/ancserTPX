import asyncio
from datetime import datetime, timedelta, timezone
import time
import unittest
from unittest.mock import patch

from backend.api import routes
from backend.api.routes import DetectZonesRequest
from backend.db.models import Candle
from backend.strategy.consolidation import build_zone_detector


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

    def test_batch_detector_skips_active_profile_recalculation(self):
        calls = []

        def build(**kwargs):
            calls.append(kwargs)
            return _Detector()

        with patch("backend.strategy.consolidation.build_zone_detector", side_effect=build):
            routes._detect_zones_sync(self._candles(2), ("5m",), 0.8)

        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0]["recalc_active_each_bar"], False)

    def test_factory_keeps_live_default_and_supports_batch_mode(self):
        live = build_zone_detector(area_timeframe="5m")
        batch = build_zone_detector(
            area_timeframe="5m",
            recalc_active_each_bar=False,
        )

        self.assertIs(live.recalc_active_each_bar, True)
        self.assertIs(batch.recalc_active_each_bar, False)

    def test_batch_mode_one_shot_refresh_matches_live_profile(self):
        candles = self._candles(7)
        live = build_zone_detector(area_timeframe="5m")
        batch = build_zone_detector(
            area_timeframe="5m",
            recalc_active_each_bar=False,
        )
        for candle in candles:
            live.update(candle)
            batch.update(candle)

        batch.refresh_forming_zone()
        live_zone = live.get_forming_zone()
        batch_zone = batch.get_forming_zone()

        self.assertIsNotNone(live_zone)
        self.assertIsNotNone(batch_zone)
        self.assertEqual(batch_zone.poc, live_zone.poc)
        self.assertEqual(batch_zone.vah_80, live_zone.vah_80)
        self.assertEqual(batch_zone.val_80, live_zone.val_80)
        self.assertEqual(batch_zone.profile, live_zone.profile)


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
