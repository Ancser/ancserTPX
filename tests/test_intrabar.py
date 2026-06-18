from datetime import datetime, timedelta, timezone
import unittest

from backend.backtest.intrabar import resolve_same_bar_exit
from backend.db.models import Candle, Direction
from scripts.confluence_label import simulate_outcomes


def candle(ts, open_, high, low, close=None):
    return Candle(
        timestamp=ts,
        open=open_,
        high=high,
        low=low,
        close=open_ if close is None else close,
        volume=1,
        interval="1m",
    )


class IntrabarRuleTests(unittest.TestCase):
    def test_nearest_level_wins_and_tie_is_stop(self):
        self.assertEqual(resolve_same_bar_exit(91, 90, 110), "sl")
        self.assertEqual(resolve_same_bar_exit(109, 90, 110), "tp")
        self.assertEqual(resolve_same_bar_exit(100, 90, 110), "sl")

    def test_training_label_uses_shared_rule(self):
        t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
        prefix = [
            candle(t0, 101, 102, 100.5),
            candle(t0 + timedelta(minutes=1), 100, 101, 99),
        ]

        sl_first = prefix + [
            candle(t0 + timedelta(minutes=2), 91, 111, 89),
        ]
        tp_first = prefix + [
            candle(t0 + timedelta(minutes=2), 109, 111, 89),
        ]

        self.assertEqual(
            simulate_outcomes(
                sl_first, 0, Direction.BUY, 100, 90, 10, [1.0],
                wait=2, horizon=5,
            )[1.0][0],
            0,
        )
        self.assertEqual(
            simulate_outcomes(
                tp_first, 0, Direction.BUY, 100, 90, 10, [1.0],
                wait=2, horizon=5,
            )[1.0][0],
            1,
        )


if __name__ == "__main__":
    unittest.main()
