import asyncio
from datetime import datetime, timedelta, timezone
import threading
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
    """zone 偵測必須跑在 executor 執行緒上,不能佔住 event loop。

    1.0.10:這個測試原本用時間賽跑當代理 —— 讓假的 `_detect_zones_sync`
    sleep 50ms,然後 `await asyncio.sleep(0.01)` 後斷言 task 還沒完成。
    在負載高的 CI runner 上,那個 10ms 的 sleep 會超時到 50ms 以上,
    工作已經做完 → `task.done()` 是 True → 紅。(1.0.10f 就是這樣掛的。)

    **會偶爾紅的測試比沒有測試更糟** —— 它訓練人忽略紅燈。

    現在直接驗真正的不變量:`_detect_zones_sync` 是在**哪一條執行緒**上被
    呼叫的。用 Event 同步而不是 sleep,完全不依賴時間。
    """

    async def test_zone_detection_runs_off_the_event_loop(self):
        loop_thread = threading.get_ident()
        seen: dict[str, int] = {}
        started = threading.Event()
        release = threading.Event()

        def fake_detect(*_args):
            seen["thread"] = threading.get_ident()
            started.set()
            # 卡住工作執行緒。如果這段是跑在 loop 上,下面的 await 就再也
            # 拿不回控制權 —— 而 thread id 也會相等,兩邊都會抓到。
            release.wait(10)
            return []

        original = routes._historical_candles
        routes._historical_candles = ChartZoneCacheTests()._candles(1)
        try:
            with patch("backend.api.routes._detect_zones_sync", side_effect=fake_detect):
                task = asyncio.create_task(
                    routes.detect_zones(DetectZonesRequest(all_timeframes=True))
                )
                # 等工作真的開始 —— 不是等一段固定時間
                for _ in range(2000):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.001)
                self.assertTrue(started.is_set(), "_detect_zones_sync 沒有被呼叫")

                # 工作執行緒此刻被 release 卡住。能跑到這裡就代表 loop 沒被佔住。
                self.assertFalse(task.done())

                release.set()
                result = await asyncio.wait_for(task, timeout=10)
        finally:
            release.set()
            routes._historical_candles = original

        self.assertNotEqual(
            seen.get("thread"), loop_thread,
            "_detect_zones_sync 跑在 event loop 的執行緒上 —— 會卡住整個伺服器")
        self.assertEqual(result["zones"], [])

    async def test_route_uses_to_thread(self):
        """結構性斷言:少了它,一個「同步呼叫但很快」的實作也會讓上面通過。

        (上面那條靠 release.wait 卡住來製造差異;若有人把工作改成同步且瞬間
        完成,thread id 會相等而被抓到 —— 這條是第二道,直接看原始碼。)
        """
        import inspect
        src = inspect.getsource(routes.detect_zones)
        self.assertIn("to_thread", src,
                      "detect_zones 不再把工作丟到執行緒 —— zone 偵測會阻塞 event loop")


if __name__ == "__main__":
    unittest.main()
