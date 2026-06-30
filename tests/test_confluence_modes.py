import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from backend.backtest.confluence_backtest import ConfluenceBacktester
from backend.db.models import ConsolidationZone, Direction, ZoneStatus
from backend.strategy.confluence import ConfluenceConfig, evaluate_confluence, evaluate_confluence_scored


def _zone(tf, zid, poc, vah, val, high, low, profile):
    ts = datetime(2026, 6, 19, 6, 0, tzinfo=timezone.utc)
    return ConsolidationZone(
        zone_id=zid,
        formed_at=ts,
        left_at=ts,
        poc=poc,
        vah_80=vah,
        val_80=val,
        high_100=high,
        low_100=low,
        total_volume=sum(profile.values()),
        duration_minutes=5,
        num_candles=5,
        status=ZoneStatus.LEFT,
        exit_direction=None,
        mature=True,
        timeframe=tf,
        profile=profile,
        va_bands={80: (vah, val)},
    )


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

    def test_sl_reference_can_use_smallest_contributing_timeframe(self):
        zones_by_tf = {
            "5m": [_zone(
                "5m", "S", poc=100.0, vah=105.0, val=95.0, high=106.0, low=94.0,
                profile={100.0: 40, 103.0: 1, 105.0: 20},
            )],
            "15m": [_zone(
                "15m", "L", poc=90.0, vah=105.25, val=94.0, high=108.0, low=88.0,
                profile={90.0: 40, 94.0: 1, 105.25: 20},
            )],
        }

        cfg = ConfluenceConfig(
            band_ticks=2,
            min_distinct_tf=2,
            direction_mode="momentum",
            rr=2.0,
            bands=(80,),
        )
        largest = [
            s for s in evaluate_confluence(zones_by_tf, current_price=100.0, cfg=cfg)
            if s.cluster.side == "VAH"
        ][0]
        self.assertEqual(largest.cluster.largest_tf, "15m")
        self.assertEqual(largest.cluster.smallest_tf, "5m")
        self.assertEqual(largest.sl_price, 94.0)

        cfg.sl_reference_tf = "smallest"
        smallest = [
            s for s in evaluate_confluence(zones_by_tf, current_price=100.0, cfg=cfg)
            if s.cluster.side == "VAH"
        ][0]
        self.assertEqual(smallest.sl_price, 103.0)
        self.assertLess(abs(smallest.entry_price - smallest.sl_price), abs(largest.entry_price - largest.sl_price))


if __name__ == "__main__":
    unittest.main()
