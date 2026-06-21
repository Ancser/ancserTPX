from datetime import datetime, timedelta, timezone
from concurrent.futures import ProcessPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest

from backend.backtest.confluence_backtest import build_zone_timeline
from backend.backtest.confluence_worker import run_job
from backend.db.models import Candle


class BacktestProgressTests(unittest.TestCase):
    def test_zone_timeline_reports_start_and_completion(self):
        t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
        candles = [
            Candle(t0 + timedelta(minutes=i), 100, 101, 99, 100, 1, interval="1m")
            for i in range(3)
        ]
        updates = []
        timeline = build_zone_timeline(
            candles, ("5m",), 0.25, 2,
            progress_callback=lambda *args: updates.append(args),
        )
        self.assertEqual(len(timeline), len(candles))
        self.assertEqual(updates[0][1:3], (0, len(candles)))
        self.assertEqual(updates[-1][1:3], (len(candles), len(candles)))

    def test_worker_publishes_complete_progress(self):
        t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
        candles = [
            Candle(t0 + timedelta(minutes=i), 100, 101, 99, 100, 1, interval="1m")
            for i in range(30)
        ]
        progress = {}
        result = run_job(
            (len(candles), candles[0].timestamp.isoformat(), candles[-1].timestamp.isoformat()),
            candles,
            {
                "contract_id": "CON.F.US.MNQ.M26",
                "contract_size": 1,
                "rr_grid": None,
                "conf_use_scorer": False,
                "conf_min_prob": 0.0,
                "conf_band_ticks": 8.0,
                "conf_min_distinct_tf": 3,
                "conf_rr": 1.5,
                "conf_wait_minutes": 1,
                "conf_base_minutes": 1,
                "conf_session_limit": True,
                "conf_enable_breakout": False,
                "initial_capital": 50000.0,
            },
            progress,
        )
        self.assertEqual(progress["status"], "complete")
        self.assertEqual(progress["stage"], "complete")
        self.assertEqual(progress["current"], len(candles))
        self.assertIn("metrics", result)

    def test_process_pool_publishes_progress_file(self):
        t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
        candles = [
            Candle(t0 + timedelta(minutes=i), 100, 101, 99, 100, 1, interval="1m")
            for i in range(30)
        ]
        params = {
            "contract_id": "CON.F.US.MNQ.M26",
            "contract_size": 1,
            "rr_grid": None,
            "conf_use_scorer": False,
            "conf_min_prob": 0.0,
            "conf_band_ticks": 8.0,
            "conf_min_distinct_tf": 3,
            "conf_rr": 1.5,
            "conf_wait_minutes": 1,
            "conf_base_minutes": 1,
            "conf_session_limit": True,
            "conf_enable_breakout": False,
            "initial_capital": 50000.0,
        }
        key = (
            len(candles),
            candles[0].timestamp.isoformat(),
            candles[-1].timestamp.isoformat(),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            progress_path = Path(temp_dir) / "progress.json"
            with ProcessPoolExecutor(max_workers=1) as executor:
                result = executor.submit(
                    run_job, key, candles, params, str(progress_path),
                ).result(timeout=30)
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
        self.assertEqual(progress["status"], "complete")
        self.assertEqual(progress["current"], len(candles))
        self.assertIn("metrics", result)


if __name__ == "__main__":
    unittest.main()
