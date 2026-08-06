import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from backend.db.models import Candle, Direction, StrategyParams
from backend.live.warmup import completed_signal_bars, signal_warmup_progress
from backend.strategy.factor import FactorSignalStrategy
from backend.strategy.pmo import EMAPMOStrategy
from backend.terminal_live import _fetch_warmup_candles


UTC = timezone.utc


def _params(**overrides):
    values = {
        "strategy": "factor",
        "candle_seconds": 60,
        "factor_timeframe_minutes": 5,
        "factor_warmup_bars": 150,
        "factor_signal_family": "emapmo",
        "factor_side_mode": "long_only",
        "factor_pmo_signal_mode": "early",
        "tr_allowed_sessions": ["ASIA"],
        "contract_id": "CON.F.US.MNQ.U26",
    }
    values.update(overrides)
    return StrategyParams(**values)


def _candles(start, count, start_price=20000.0):
    candles = []
    for i in range(count):
        close = start_price + i * 0.25
        candles.append(Candle(
            timestamp=start + timedelta(minutes=i),
            open=close - 0.25,
            high=close + 0.50,
            low=close - 0.50,
            close=close,
            volume=100 + i,
            symbol="MNQ",
            interval="1m",
        ))
    return candles


def _asia_history(days=5, minutes_per_day=150):
    candles = []
    base = datetime(2026, 7, 6, 22, 0, tzinfo=UTC)
    for day in range(days):
        candles.extend(_candles(base + timedelta(days=day), minutes_per_day, 20000 + day * 10))
    return candles


class LiveWarmupTests(unittest.IsolatedAsyncioTestCase):
    def test_weekend_nonempty_window_is_still_insufficient(self):
        params = _params()
        candles = _candles(datetime(2026, 7, 12, 22, 0, tzinfo=UTC), 60)
        self.assertEqual(signal_warmup_progress(candles, params), (12, 320))

    def test_progress_matches_factor_strategy_ingest(self):
        params = _params()
        candles = _asia_history()
        strategy = FactorSignalStrategy(params)
        for candle in candles:
            strategy.observe(candle, [], True)
        self.assertEqual(completed_signal_bars(candles, params), len(strategy._bars))
        self.assertEqual(len(strategy._bars), 150)

    def test_gap_on_bucket_close_matches_strategy_overwrite_semantics(self):
        params = _params()
        candles = [
            _candles(datetime(2026, 7, 12, 22, 3, tzinfo=UTC), 1)[0],
            _candles(datetime(2026, 7, 12, 22, 9, tzinfo=UTC), 1)[0],
        ]
        strategy = FactorSignalStrategy(params)
        for candle in candles:
            strategy.observe(candle, [], True)
        self.assertEqual(len(strategy._bars), 1)
        self.assertEqual(completed_signal_bars(candles, params), 1)

    def test_progress_also_matches_standalone_pmo_strategy(self):
        params = _params(
            strategy="pmo",
            pmo_timeframe_minutes=5,
            pmo_warmup_bars=150,
        )
        candles = _asia_history()
        strategy = EMAPMOStrategy(params)
        for candle in candles:
            strategy.observe(candle, [], True)
        self.assertEqual(completed_signal_bars(candles, params), len(strategy._bars))
        self.assertEqual(len(strategy._bars), 150)

    async def test_terminal_fetch_expands_nonempty_short_window(self):
        params = _params()
        short = _candles(datetime(2026, 7, 12, 22, 0, tzinfo=UTC), 60)
        # 1.0.9: EMAPMO 的暖機門檻提到 320(見 factor.effective_warmup),
        # 所以「夠」的 fixture 必須跨更多天,否則擴窗迴圈會一直往下試。
        enough = _asia_history(days=12)
        client = unittest.mock.Mock()
        client.get_historical_bars_paginated = AsyncMock(side_effect=[short, enough])

        result = await _fetch_warmup_candles(client, "CON.F.US.MNQ.U26", params)

        self.assertEqual(client.get_historical_bars_paginated.await_count, 2)
        completed, required = signal_warmup_progress(result, params)
        self.assertEqual(required, 320)
        self.assertGreaterEqual(completed, required)

    async def test_terminal_fetch_keeps_fullest_nonempty_candidate(self):
        params = _params()
        first = _candles(datetime(2026, 7, 12, 22, 0, tzinfo=UTC), 60)
        fuller = _candles(datetime(2026, 7, 6, 22, 0, tzinfo=UTC), 500)
        regressed = _candles(datetime(2026, 7, 12, 22, 0, tzinfo=UTC), 30)
        client = unittest.mock.Mock()
        client.get_historical_bars_paginated = AsyncMock(
            side_effect=[first, fuller, regressed]
        )

        result = await _fetch_warmup_candles(client, "CON.F.US.MNQ.U26", params)

        self.assertEqual(client.get_historical_bars_paginated.await_count, 3)
        self.assertEqual(signal_warmup_progress(result, params), (100, 320))


class FactorStatusTests(unittest.TestCase):
    def test_warmup_status_keeps_current_pmo_and_signal(self):
        strategy = FactorSignalStrategy(_params())
        for candle in _candles(datetime(2026, 7, 12, 22, 0, tzinfo=UTC), 55):
            strategy.observe(candle, [], True)

        label = strategy.get_phase_label()

        lines = label.splitlines()
        self.assertIn("EMAPMO WARM-UP: 11/320 completed 5m bars", lines[0])
        self.assertIn("309 remaining; trading disabled", lines[0])
        self.assertRegex(lines[1], r"^SIG: -?\d+\.\d{5}$")
        self.assertRegex(lines[2], r"^PMO: -?\d+\.\d{5}$")
        self.assertNotRegex(label, r"[\u3400-\u9fff]")

    def test_best_wait_status_lists_early_long_conditions_and_values(self):
        strategy = FactorSignalStrategy(_params())
        strategy._bars.extend(_candles(datetime(2026, 7, 6, 22, 0, tzinfo=UTC), 320))
        snapshot = {
            "pmo": -0.08,
            "signal": -0.07,
            "prev_pmo": -0.09,
            "prev_signal": -0.08,
            "p_gap_now": -0.01,
            "p_gap_prev": -0.02,
            "p_gap_prev2": -0.03,
            "q_gap_now": 0.01,
            "q_gap_prev": 0.02,
            "q_gap_prev2": 0.03,
            "normal_short": False,
            "normal_long": False,
            "early_short": False,
            "early_long": False,
        }
        with patch.object(strategy, "_emapmo_snapshot", return_value=snapshot):
            label = strategy.get_phase_label()

        lines = label.splitlines()
        self.assertEqual(lines[0], "EMAPMO 5m")
        self.assertEqual(lines[1], "SIG: -0.07000")
        self.assertEqual(lines[2], "PMO: -0.08000")
        self.assertIn("Waiting for:", lines)
        self.assertIn("LONG EARLY", lines)
        self.assertIn("SIG < -0.10000: current=-0.07000 [WAIT]", lines)
        self.assertIn("PMO < SIG: -0.08000 < -0.07000 [PASS]", lines)
        self.assertIn(
            "SIG - PMO gap shrinking: 0.03000 -> 0.02000 -> 0.01000 [PASS]",
            lines,
        )
        self.assertNotIn("SHORT EARLY", lines)
        self.assertNotRegex(label, r"[\u3400-\u9fff]")

    def test_long_only_status_does_not_advertise_short_as_tradeable(self):
        strategy = FactorSignalStrategy(_params())
        strategy._bars.extend(_candles(datetime(2026, 7, 6, 22, 0, tzinfo=UTC), 320))
        snapshot = {
            "pmo": 0.08,
            "signal": 0.07,
            "prev_pmo": 0.10,
            "prev_signal": 0.08,
            "p_gap_now": 0.01,
            "p_gap_prev": 0.02,
            "p_gap_prev2": 0.03,
            "q_gap_now": -0.01,
            "q_gap_prev": -0.02,
            "q_gap_prev2": -0.03,
            "normal_short": False,
            "normal_long": False,
            "early_short": True,
            "early_long": False,
        }
        with patch.object(strategy, "_emapmo_snapshot", return_value=snapshot):
            label = strategy.get_phase_label()

        self.assertNotIn("Signal: SHORT", label)
        self.assertIn("LONG EARLY", label.splitlines())
        self.assertIn("Blocked: SHORT signal ignored (long_only)", label.splitlines())

    def test_short_early_status_displays_sig_pmo_relation_on_separate_lines(self):
        strategy = FactorSignalStrategy(_params(factor_side_mode="short_only"))
        strategy._bars.extend(_candles(datetime(2026, 7, 6, 22, 0, tzinfo=UTC), 320))
        snapshot = {
            "pmo": 0.08,
            "signal": 0.07,
            "prev_pmo": 0.09,
            "prev_signal": 0.08,
            "p_gap_now": 0.01,
            "p_gap_prev": 0.02,
            "p_gap_prev2": 0.03,
            "q_gap_now": -0.01,
            "q_gap_prev": -0.02,
            "q_gap_prev2": -0.03,
            "normal_short": False,
            "normal_long": False,
            "early_short": False,
            "early_long": False,
        }
        with patch.object(strategy, "_emapmo_snapshot", return_value=snapshot):
            lines = strategy.get_phase_label().splitlines()

        self.assertEqual(lines[1], "SIG: 0.07000")
        self.assertEqual(lines[2], "PMO: 0.08000")
        self.assertIn("SIG < PMO: 0.07000 < 0.08000 [PASS]", lines)

    def test_refactored_direction_matches_original_formulas(self):
        strategy = FactorSignalStrategy(_params(tr_allowed_sessions=None))
        closes = [20000 + ((i % 17) - 8) * 0.75 + i * 0.03 for i in range(150)]
        start = datetime(2026, 7, 6, 0, 0, tzinfo=UTC)
        for i, close in enumerate(closes):
            strategy._bars.append(Candle(
                timestamp=start + timedelta(minutes=5 * i),
                open=close - 0.25,
                high=close + 0.5,
                low=close - 0.5,
                close=close,
                volume=100,
                symbol="MNQ",
                interval="5m",
            ))

        pmo, sig = strategy._pmo_series()
        p0, p1, p2 = pmo[-2], pmo[-1], pmo[-3]
        s0, s1, s2 = sig[-2], sig[-1], sig[-3]
        self.assertNotIn(None, (p0, p1, p2, s0, s1, s2))
        normal_short = p1 > 0.06 and p1 < s1 and p0 >= s0
        normal_long = p1 < -0.10 and p1 > s1 and p0 <= s0
        p_gap = [a - b for a, b in zip(pmo[-3:], sig[-3:])]
        q_gap = [b - a for a, b in zip(pmo[-3:], sig[-3:])]
        early_short = s1 > 0.06 and p_gap[-1] < p_gap[-2] and p1 > s1 and p_gap[-2] < p_gap[-3]
        early_long = s1 < -0.10 and q_gap[-1] < q_gap[-2] and p1 < s1 and q_gap[-2] < q_gap[-3]

        for mode in ("normal", "early", "both"):
            strategy.pmo_signal_mode = mode
            expected = None
            if (mode in {"normal", "both"} and normal_short) or (mode in {"early", "both"} and early_short):
                expected = Direction.SELL
            elif (mode in {"normal", "both"} and normal_long) or (mode in {"early", "both"} and early_long):
                expected = Direction.BUY
            self.assertEqual(strategy._factor_direction()[0], expected)


if __name__ == "__main__":
    unittest.main()
