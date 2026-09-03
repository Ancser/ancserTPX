from __future__ import annotations

import inspect
import re
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from backend.api import routes
from backend.db.models import Candle


UTC = timezone.utc
CONTRACT = "CON.F.US.MNQ.U26"


def _bars(count: int, start: datetime | None = None) -> list[Candle]:
    first = start or datetime(2026, 8, 1, tzinfo=UTC)
    return [
        Candle(
            timestamp=first + timedelta(minutes=i),
            open=100.0 + i,
            high=101.0 + i,
            low=99.0 + i,
            close=100.5 + i,
            volume=10 + i,
            symbol="MNQ",
            interval="1m",
        )
        for i in range(count)
    ]


class WorksetLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.original_candles = routes._historical_candles
        self.original_snapshot = routes._historical_working_snapshot
        routes._historical_candles = []
        routes._historical_working_snapshot = None

    def tearDown(self):
        routes._historical_candles = self.original_candles
        routes._historical_working_snapshot = self.original_snapshot

    def _publish(self, bars: list[Candle]):
        return routes._publish_historical_candles(
            bars,
            source_key=routes._historical_source_key(CONTRACT, 2, 1, False),
            requested_start=bars[0].timestamp,
            requested_end=bars[-1].timestamp,
            contract_id=CONTRACT,
            contracts=[CONTRACT],
            continuous_meta={},
            from_store=False,
        )

    def test_only_current_token_is_owned_and_superseded_token_is_rejected(self):
        old = self._publish(_bars(5000))
        child = self._publish(list(old.candles[-100:]))

        self.assertIs(routes._historical_working_snapshot, child)
        self.assertEqual(len(child.candles), 100)
        self.assertNotEqual(old.token, child.token)
        with self.assertRaises(HTTPException) as caught:
            routes._resolve_backtest_workset(old.token)
        self.assertEqual(caught.exception.status_code, 409)

    def test_live_tail_mutation_cannot_change_token_bound_input(self):
        bars = _bars(20)
        snapshot = self._publish(bars)
        replacement = Candle(
            timestamp=bars[-1].timestamp,
            open=1, high=2, low=0, close=1.5, volume=999,
            symbol="MNQ", interval="1m",
        )
        outside = _bars(1, bars[-1].timestamp + timedelta(minutes=1))[0]
        routes._upsert_historical_candles([replacement, outside])

        self.assertIs(routes._resolve_backtest_workset(snapshot.token)[-1], bars[-1])
        refreshed = routes._merge_refresh_into_workset(
            snapshot.candles,
            [replacement, outside],
            snapshot.requested_start,
            snapshot.requested_end,
        )
        self.assertIs(refreshed[-1], replacement)
        self.assertNotIn(outside, refreshed)

    def test_workset_contract_binds_economics_and_rejects_root_mismatch(self):
        snapshot = self._publish(_bars(5))
        requested = routes.BacktestRequest(
            workset_token=snapshot.token,
            contract_id="MNQ",
            contract_size=5,
        )
        bound = routes._bind_backtest_request_to_workset(requested, snapshot)
        self.assertEqual(bound.contract_id, CONTRACT)
        self.assertEqual(bound.contract_size, 5)

        mismatch = routes.BacktestRequest(
            workset_token=snapshot.token,
            contract_id="CON.F.US.ENQ.U26",
        )
        with self.assertRaises(HTTPException) as caught:
            routes._bind_backtest_request_to_workset(mismatch, snapshot)
        self.assertEqual(caught.exception.status_code, 409)


class BacktestRetentionTests(unittest.TestCase):
    def setUp(self):
        self.original = list(routes._backtest_results)
        routes._backtest_results.clear()

    def tearDown(self):
        routes._backtest_results.clear()
        routes._backtest_results.extend(self.original)

    def test_result_retention_is_bounded_and_contains_only_scalar_summary(self):
        for index in range(routes._BACKTEST_SUMMARY_LIMIT + 3):
            metrics = SimpleNamespace(
                total_trades=index,
                win_rate=float(index),
                total_pnl=float(index * 2),
                max_drawdown=float(index * 3),
            )
            routes._remember_backtest_summary(SimpleNamespace(
                metrics=metrics,
                trades=[object()],
                equity_curve=[object()],
            ))

        self.assertEqual(len(routes._backtest_results), routes._BACKTEST_SUMMARY_LIMIT)
        self.assertTrue(all(isinstance(row, dict) for row in routes._backtest_results))
        self.assertEqual(routes._backtest_results[0]["total_trades"], 3)
        self.assertNotIn("trades", routes._backtest_results[-1])
        self.assertNotIn("equity_curve", routes._backtest_results[-1])

    def test_equity_compaction_is_bounded_and_preserves_endpoints(self):
        start = datetime(2020, 1, 1, tzinfo=UTC)
        curve = [(start + timedelta(minutes=i), float(i)) for i in range(10_001)]
        compact = routes._compact_equity_curve(curve)

        self.assertEqual(len(compact), 5000)
        self.assertEqual(compact[0], curve[0])
        self.assertEqual(compact[-1], curve[-1])


class ChartHistoryPaginationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_candles = routes._historical_candles
        self.original_contract = routes._live_contract_id
        routes._historical_candles = _bars(20)
        routes._live_contract_id = CONTRACT

    def tearDown(self):
        routes._historical_candles = self.original_candles
        routes._live_contract_id = self.original_contract

    async def test_before_returns_bounded_older_page_and_flags_more(self):
        cutoff = routes._historical_candles[12].timestamp
        response = await routes.get_stored_candles(
            limit=5, before=cutoff.isoformat(),
        )

        returned = [row["time"] for row in response["candles"]]
        expected = [
            routes._historical_candles[index].timestamp.isoformat()
            for index in range(7, 12)
        ]
        self.assertEqual(returned, expected)
        self.assertTrue(response["has_more_before"])
        self.assertFalse(response["has_more_after"])
        self.assertEqual(response["source"], "working_set")

    async def test_before_falls_back_to_persistent_store_at_memory_boundary(self):
        from types import SimpleNamespace

        memory = routes._historical_candles
        persistent = _bars(
            40, start=datetime(2026, 7, 1, tzinfo=UTC),
        )
        routes._historical_candles = memory[-5:]
        cutoff = routes._historical_candles[0].timestamp
        snapshot = SimpleNamespace(bars=tuple(persistent))
        with patch.object(routes, "_store_load_snapshot", return_value=snapshot):
            response = await routes.get_stored_candles(
                limit=3, before=cutoff.isoformat(),
            )

        returned = [row["time"] for row in response["candles"]]
        expected = [
            persistent[index].timestamp.isoformat()
            for index in range(37, 40)
        ]
        self.assertEqual(returned, expected)
        self.assertEqual(response["source"], "persistent_store")


class TerminalProgressTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_candles = routes._historical_candles
        self.original_snapshot = routes._historical_working_snapshot
        self.original_sweep = routes._sweep_running
        bars = _bars(2)
        self.snapshot = routes._publish_historical_candles(
            bars,
            source_key=routes._historical_source_key(CONTRACT, 2, 1, False),
            requested_start=bars[0].timestamp,
            requested_end=bars[-1].timestamp,
            contract_id=CONTRACT,
            contracts=[CONTRACT],
            continuous_meta={},
            from_store=False,
        )

    def tearDown(self):
        routes._historical_candles = self.original_candles
        routes._historical_working_snapshot = self.original_snapshot
        routes._sweep_running = self.original_sweep

    async def test_single_failure_publishes_terminal_error(self):
        req = routes.BacktestRequest(workset_token=self.snapshot.token)
        with patch.object(
            routes, "_run_trend_backtest",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ), patch.object(routes, "_update_bt_progress") as progress:
            with self.assertRaisesRegex(RuntimeError, "boom"):
                await routes.run_backtest(req)

        progress.assert_called_with("error", 0, 0, "boom", status="error")

    async def test_sweep_stale_token_publishes_error_and_releases_lock(self):
        req = routes.BacktestRequest(workset_token="superseded")
        with patch.object(routes, "_update_bt_progress") as progress:
            with self.assertRaises(HTTPException):
                await routes.run_backtest_sweep(req)

        self.assertFalse(routes._sweep_running)
        self.assertEqual(progress.call_args.kwargs["status"], "error")
        self.assertEqual(progress.call_args.args[0], "error")

    def test_done_is_published_after_response_construction(self):
        source = inspect.getsource(routes._run_trend_backtest)
        self.assertGreater(source.index('_update_bt_progress("done"'),
                           source.index("response = BacktestResponse("))


class ThreadingStructureTests(unittest.TestCase):
    def test_heavy_store_calls_are_structurally_offloaded(self):
        source = inspect.getsource(routes.fetch_historical)
        for name in (
            "_store_load_snapshot", "_store_save", "_store_merge",
            "_store_detect_gaps", "_store_advance_frozen",
            "_merge_store_and_fresh", "_merge_candle_lists",
        ):
            self.assertRegex(
                source,
                re.compile(rf"asyncio\.to_thread\(\s*{name}"),
            )
        publish = inspect.getsource(routes._publish_historical_candles_async)
        select = inspect.getsource(routes._select_working_historical_range_async)
        self.assertIn("asyncio.to_thread(_prepare_historical_publication", publish)
        self.assertIn("asyncio.to_thread(", select)
        self.assertIn("_select_working_historical_range", select)


if __name__ == "__main__":
    unittest.main()
