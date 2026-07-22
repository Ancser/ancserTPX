import unittest
from datetime import datetime, timedelta, timezone

from backend.api.routes import _collect_pmo_markers
from backend.db.models import Candle, StrategyParams
from backend.strategy.factor import (
    FACTOR_EMAPMO_HISTORY_BARS,
    FactorSignalStrategy,
    calculate_emapmo_snapshot,
)


UTC = timezone.utc


def _seed_sensitive_closes(recent_step: float) -> list[float]:
    """A compact deterministic series whose SIG sits on the -0.10 boundary."""
    closes = [30000.0]
    for _ in range(20):
        closes.append(closes[-1] - 5.0)
    for _ in range(314):
        closes.append(closes[-1] - recent_step)
    for multiplier in (2.0, 1.6, 1.2, 0.8, 0.4, 0.0):
        closes.append(closes[-1] - recent_step * multiplier)
    return closes


def _five_minute_bars(closes: list[float]) -> list[Candle]:
    start = datetime(2026, 7, 15, 0, 0, tzinfo=UTC)
    return [
        Candle(
            timestamp=start + timedelta(minutes=5 * i),
            open=close,
            high=close + 1.0,
            low=close - 1.0,
            close=close,
            volume=100,
            symbol="MNQ",
            interval="5m",
        )
        for i, close in enumerate(closes)
    ]


class EMAPMOOverlayParityTests(unittest.TestCase):
    def test_full_history_false_positive_is_absent_from_factor_overlay(self):
        closes = _seed_sensitive_closes(2.91)
        self.assertEqual(len(closes), 341)

        full = calculate_emapmo_snapshot(closes)
        rolling = calculate_emapmo_snapshot(closes[-FACTOR_EMAPMO_HISTORY_BARS:])

        # Regression for the July 16 live miss: a full-history EMA seed crosses
        # SIG below -0.10, while FACTOR's rolling 320-bar seed does not.
        self.assertTrue(full["early_long"])
        self.assertAlmostEqual(full["pmo"], -0.1003892216, places=9)
        self.assertAlmostEqual(full["signal"], -0.1001739585, places=9)
        self.assertFalse(rolling["early_long"])
        self.assertAlmostEqual(rolling["pmo"], -0.1001610550, places=9)
        self.assertAlmostEqual(rolling["signal"], -0.0999229987, places=9)

        strategy = FactorSignalStrategy(StrategyParams(
            strategy="factor",
            candle_seconds=300,
            factor_signal_family="emapmo",
            factor_pmo_signal_mode="early",
            factor_warmup_bars=150,
        ))
        strategy._bars.extend(_five_minute_bars(closes))
        self.assertEqual(strategy._bars.maxlen, FACTOR_EMAPMO_HISTORY_BARS)
        live_snapshot = strategy._emapmo_snapshot()
        self.assertAlmostEqual(live_snapshot["pmo"], rolling["pmo"], places=12)
        self.assertAlmostEqual(live_snapshot["signal"], rolling["signal"], places=12)
        self.assertFalse(live_snapshot["early_long"])

        signal_bars = _five_minute_bars(closes)
        source_time = signal_bars[-1].timestamp
        entry_close = closes[-1] + 0.25
        signal_bars.append(Candle(
            timestamp=source_time + timedelta(minutes=5),
            open=entry_close,
            high=entry_close + 1.0,
            low=entry_close - 1.0,
            close=entry_close,
            volume=100,
            symbol="MNQ",
            interval="5m",
        ))

        markers = _collect_pmo_markers(signal_bars, cutoff=None)
        self.assertFalse(any(row["source_time"] == source_time.isoformat() for row in markers))

    def test_real_rolling_signal_is_placed_on_next_five_minute_open(self):
        closes = _seed_sensitive_closes(2.92)
        rolling = calculate_emapmo_snapshot(closes[-FACTOR_EMAPMO_HISTORY_BARS:])
        self.assertTrue(rolling["early_long"])

        bars = _five_minute_bars(closes)
        source_time = bars[-1].timestamp
        entry_time = source_time + timedelta(minutes=5)
        entry_open = closes[-1] + 0.25
        bars.append(Candle(
            timestamp=entry_time,
            open=entry_open,
            high=entry_open + 1.0,
            low=entry_open - 1.0,
            close=entry_open,
            volume=100,
            symbol="MNQ",
            interval="5m",
        ))

        markers = _collect_pmo_markers(bars, cutoff=None)
        marker = next(row for row in markers if row["source_time"] == source_time.isoformat())

        self.assertEqual(marker["time"], entry_time.isoformat())
        self.assertEqual(marker["subtype"], "early")
        self.assertEqual(marker["direction"], "long")
        self.assertEqual(marker["price"], round(entry_open, 4))
        self.assertEqual(marker["detail"]["pmo"], round(rolling["pmo"], 5))
        self.assertEqual(marker["detail"]["signal"], round(rolling["signal"], 5))


if __name__ == "__main__":
    unittest.main()
