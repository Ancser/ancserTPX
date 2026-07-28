import unittest

from backend.api.routes import BacktestRequest, LiveStartRequest
from backend.db.models import StrategyParams
from backend.terminal_live import _build_strategy_params


class StrategyDefaultTests(unittest.TestCase):
    def test_public_defaults_use_factor(self):
        """1.0.9: TREND 已移除,三處公開預設一律 factor。"""
        strategy = StrategyParams()
        backtest = BacktestRequest()
        live = LiveStartRequest(account_id=1)
        self.assertEqual(strategy.strategy, "factor")
        self.assertEqual(backtest.strategy, "factor")
        self.assertEqual(live.strategy, "factor")
        for params in (strategy, backtest, live):
            self.assertEqual(params.sigma_window_minutes, 15)
            self.assertEqual(params.sigma_method, "std")
            self.assertEqual(params.sigma_entry_mode, "blind")
            self.assertEqual(params.sigma_accept_mode, "none")

    def test_terminal_preserves_sigma_fields(self):
        params = _build_strategy_params({
            "strategy": "sigma",
            "sigma_window_minutes": 30,
            "sigma_method": "std",
            "sigma_entry_mode": "blind",
            "sigma_accept_mode": "filter",
            "sigma_stop_span": 1.5,
            "tr_allowed_sessions": ["RTH"],
            "tr_one_trade_per_session": False,
        }, "CON.F.US.MNQ.M26")
        self.assertEqual(params.strategy, "sigma")
        self.assertEqual(params.sigma_window_minutes, 30)
        self.assertEqual(params.sigma_method, "std")
        self.assertEqual(params.sigma_entry_mode, "blind")
        self.assertEqual(params.sigma_accept_mode, "filter")
        self.assertEqual(params.sigma_stop_span, 1.5)
        self.assertEqual(params.tr_allowed_sessions, ["RTH"])
        self.assertFalse(params.tr_one_trade_per_session)


if __name__ == "__main__":
    unittest.main()
