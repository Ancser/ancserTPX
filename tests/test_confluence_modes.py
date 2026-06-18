import unittest
from unittest.mock import patch

from backend.strategy.confluence import ConfluenceConfig, evaluate_confluence_scored


class _Scorer:
    pass


class ConfluenceModeTests(unittest.TestCase):
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
