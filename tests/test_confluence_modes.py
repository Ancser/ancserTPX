import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from backend.backtest.confluence_backtest import ConfluenceBacktester
from backend.db.models import Direction
from backend.strategy.confluence import ConfluenceConfig, evaluate_confluence_scored


class _Scorer:
    pass


class ConfluenceModeTests(unittest.TestCase):
    def test_backtest_session_lock_is_zone_direction_scoped_like_live(self):
        bt = ConfluenceBacktester(ConfluenceConfig())
        ts = datetime(2026, 6, 19, 6, 0, tzinfo=timezone.utc)
        buy_4h = SimpleNamespace(direction=Direction.BUY, cluster=SimpleNamespace(largest_tf="4h"))
        buy_10m = SimpleNamespace(direction=Direction.BUY, cluster=SimpleNamespace(largest_tf="10m"))
        buy_4h_again = SimpleNamespace(direction=Direction.BUY, cluster=SimpleNamespace(largest_tf="4h"))
        sell_4h = SimpleNamespace(direction=Direction.SELL, cluster=SimpleNamespace(largest_tf="4h"))

        self.assertEqual(bt._session_lock_key(ts, buy_4h), bt._session_lock_key(ts, buy_4h_again))
        self.assertNotEqual(bt._session_lock_key(ts, buy_4h), bt._session_lock_key(ts, buy_10m))
        self.assertNotEqual(bt._session_lock_key(ts, buy_4h), bt._session_lock_key(ts, sell_4h))

    def test_default_modes_exclude_breakout(self):
        self.assertEqual(
            ConfluenceConfig().auto_modes(),
            ("momentum", "reversion"),
        )

    def test_scored_evaluation_honors_breakout_disabled_by_default(self):
        cfg = ConfluenceConfig(enable_breakout=False)
        seen = []

        def fake_geometry(cluster, current_price, zones, mode, config, recent_candles=None):
            seen.append(mode)
            return None

        with (
            patch("backend.strategy.confluence.extract_levels", return_value=[object()]),
            patch("backend.strategy.confluence.cluster_levels", return_value=[object()]),
            patch("backend.strategy.confluence._signal_geometry", side_effect=fake_geometry),
        ):
            result = evaluate_confluence_scored(
                {"5m": [object()]}, 100.0, cfg, _Scorer(),
            )

        self.assertEqual(result, [])
        self.assertEqual(seen, ["momentum", "reversion"])

    def test_scored_evaluation_includes_breakout_when_enabled(self):
        cfg = ConfluenceConfig(enable_breakout=True)
        seen = []

        def fake_geometry(cluster, current_price, zones, mode, config, recent_candles=None):
            seen.append(mode)
            return None

        with (
            patch("backend.strategy.confluence.extract_levels", return_value=[object()]),
            patch("backend.strategy.confluence.cluster_levels", return_value=[object()]),
            patch("backend.strategy.confluence._signal_geometry", side_effect=fake_geometry),
        ):
            evaluate_confluence_scored(
                {"5m": [object()]}, 100.0, cfg, _Scorer(),
            )

        self.assertEqual(seen, ["momentum", "reversion", "breakout"])


if __name__ == "__main__":
    unittest.main()
