from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import unittest

from backend.data.candle_store import is_expected_gap


class CandleStoreGapTests(unittest.TestCase):
    def test_daily_maintenance_handles_us_daylight_time(self):
        self.assertTrue(is_expected_gap(
            datetime(2026, 6, 17, 20, 59, tzinfo=timezone.utc),
            datetime(2026, 6, 17, 22, 0, tzinfo=timezone.utc),
        ))

    def test_daily_maintenance_handles_us_standard_time(self):
        self.assertTrue(is_expected_gap(
            datetime(2026, 1, 15, 21, 59, tzinfo=timezone.utc),
            datetime(2026, 1, 15, 23, 0, tzinfo=timezone.utc),
        ))

    def test_midday_missing_bars_are_not_ignored(self):
        self.assertFalse(is_expected_gap(
            datetime(2026, 6, 17, 15, 0, tzinfo=timezone.utc),
            datetime(2026, 6, 17, 15, 20, tzinfo=timezone.utc),
        ))

    def test_memorial_day_early_close_is_expected(self):
        self.assertTrue(is_expected_gap(
            datetime(2026, 5, 25, 16, 59, tzinfo=timezone.utc),
            datetime(2026, 5, 25, 22, 0, tzinfo=timezone.utc),
        ))


class CandleStore2020HistoryGapTests(unittest.TestCase):
    """1.0.10: 補進 Databento 2020 起的歷史後,偵測器一次誤報 546 個破洞。

    逐一測繪後全部是可解釋的休市。修好後降到 67 個,其中 65 個是 2020 年 3 月
    COVID 崩盤的熔斷停牌 —— 那些**應該**被標記。

    誤報的代價不只是 log 雜訊:routes.py 的自動回補只取前 5 個破洞,而破洞按
    時間排序 —— 2020 年那批會永遠佔住名額,讓真正的近期破洞修不到。

    本組測試的重點是「放寬規則之後,真實的資料遺失仍然抓得到」。
    """

    ET = ZoneInfo("America/New_York")

    def _et(self, y, mo, d, h, mi):
        return datetime(y, mo, d, h, mi, tzinfo=self.ET).astimezone(timezone.utc)

    def test_1615_et_close_halt_is_expected(self):
        # CME 股指期貨 16:15–16:30 ET 收盤休止。546 個誤報裡佔 370 個,
        # 只出現在 2020(249)與 2021(121),之後 CME 改時程就沒有了。
        self.assertTrue(is_expected_gap(
            self._et(2020, 5, 5, 16, 14), self._et(2020, 5, 5, 16, 30)))

    def test_long_gap_starting_at_1614_is_still_flagged(self):
        # 同樣 16:14 起但長達 2 小時 —— 不是那個休止,必須標記
        self.assertFalse(is_expected_gap(
            self._et(2020, 5, 5, 16, 14), self._et(2020, 5, 5, 18, 14)))

    def test_thin_overnight_gap_is_expected(self):
        # 盤外幾分鐘沒成交不是資料遺失。實測 101 個 <10 分鐘的破洞全在盤外。
        self.assertTrue(is_expected_gap(
            self._et(2024, 3, 5, 2, 0), self._et(2024, 3, 5, 2, 5)))

    def test_short_gap_inside_rth_is_still_flagged(self):
        # 放寬規則後最重要的一條:RTH 內即使 1 分鐘的洞也不能被遮蔽
        self.assertFalse(is_expected_gap(
            self._et(2024, 3, 5, 14, 0), self._et(2024, 3, 5, 14, 1)))
        self.assertFalse(is_expected_gap(
            self._et(2024, 3, 5, 11, 0), self._et(2024, 3, 5, 11, 5)))

    def test_long_overnight_gap_is_still_flagged(self):
        # 盤外但 30 分鐘 —— 超過「沒成交」的合理範圍
        self.assertFalse(is_expected_gap(
            self._et(2024, 3, 5, 2, 0), self._et(2024, 3, 5, 2, 30)))

    def test_christmas_eve_closure_is_expected(self):
        # 13:14 ET 收、隔日 18:00 開 = 28.8h,落在舊規則 8h~36h 的死角
        self.assertTrue(is_expected_gap(
            self._et(2024, 12, 24, 13, 14), self._et(2024, 12, 25, 18, 0)))

    def test_new_year_eve_closure_is_expected(self):
        self.assertTrue(is_expected_gap(
            self._et(2024, 12, 31, 16, 59), self._et(2025, 1, 1, 18, 0)))

    def test_ordinary_weekday_multi_hour_gap_is_flagged(self):
        self.assertFalse(is_expected_gap(
            self._et(2024, 3, 5, 1, 0), self._et(2024, 3, 5, 4, 0)))


if __name__ == "__main__":
    unittest.main()
