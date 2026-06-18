import unittest

from backend.api.routes import BacktestRequest, LiveStartRequest
from backend.db.models import StrategyParams
from backend.terminal_live import _build_strategy_params


class StrategyDefaultTests(unittest.TestCase):
    def test_public_defaults_use_confluence(self):
        strategy = StrategyParams()
        backtest = BacktestRequest()
        live = LiveStartRequest(account_id=1)
        self.assertEqual(strategy.strategy, "confluence")
        self.assertEqual(backtest.strategy, "confluence")
        self.assertEqual(live.strategy, "confluence")
        for params in (strategy, backtest, live):
            self.assertEqual(params.conf_band_ticks, 4.0)
            self.assertEqual(params.conf_min_distinct_tf, 2)
            self.assertEqual(params.conf_rr, 3.0)
            self.assertIsNone(params.conf_rr_grid)
            self.assertFalse(params.conf_enable_breakout)

    def test_terminal_preserves_new_confluence_fields(self):
        params = _build_strategy_params({
            "strategy": "confluence",
            "conf_ev_floor": 0.2,
            "conf_rr_grid": [1.0, 1.5, 2.0],
            "conf_enable_breakout": False,
            "conf_trail_trigger_pct": 0.5,
            "conf_trail_lock_pct": 0.1,
            "conf_full_tp_lock": 2,
            "conf_session_limit": False,
        }, "CON.F.US.MNQ.M26")
        self.assertEqual(params.strategy, "confluence")
        self.assertEqual(params.conf_ev_floor, 0.2)
        self.assertIsNone(params.conf_rr_grid)
        self.assertFalse(params.conf_enable_breakout)
        self.assertEqual(params.conf_trail_trigger_pct, 0.5)
        self.assertEqual(params.conf_trail_lock_pct, 0.1)
        self.assertEqual(params.conf_full_tp_lock, 2)
        self.assertFalse(params.conf_session_limit)


if __name__ == "__main__":
    unittest.main()
