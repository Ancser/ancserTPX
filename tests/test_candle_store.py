from datetime import datetime, timezone
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


if __name__ == "__main__":
    unittest.main()
