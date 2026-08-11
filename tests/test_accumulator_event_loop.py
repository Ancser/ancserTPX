from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.data import accumulator
from backend.data import candle_store
from backend.db.models import Candle


UTC = timezone.utc


def _bar(ts: datetime, close: float = 20000.0) -> Candle:
    return Candle(
        timestamp=ts,
        open=close - 0.25,
        high=close + 0.50,
        low=close - 0.50,
        close=close,
        volume=100,
        symbol="MNQ",
        interval="1m",
    )


def _status(last: datetime | None = None, bars: int = 10) -> dict:
    return {
        "symbol": "MNQ",
        "bars": bars,
        "first": (last - timedelta(minutes=bars - 1)) if last else None,
        "last": last,
        "age_days": 0 if last else None,
        "state": "FRESH" if last else "EMPTY",
    }


class _NoIterationBars:
    def __bool__(self):
        return True

    def __len__(self):
        return 2_331_102

    def __iter__(self):
        raise AssertionError("store_status must not iterate the snapshot")

    def __getitem__(self, _index):
        raise AssertionError("store_status must use snapshot bounds")


class _StatusSnapshot:
    bars = _NoIterationBars()
    first_time = datetime(2020, 1, 1, tzinfo=UTC)
    last_time = datetime.now(UTC) - timedelta(minutes=1)


class StoreStatusSnapshotTests(unittest.TestCase):
    def test_status_uses_snapshot_metadata_without_full_list_copy_or_iteration(self):
        with patch.object(
            accumulator.candle_store, "load_snapshot", return_value=_StatusSnapshot()
        ) as load_snapshot, patch.object(
            accumulator.candle_store, "load",
            side_effect=AssertionError("full list copy is forbidden"),
        ):
            result = accumulator.store_status("MNQ")

        load_snapshot.assert_called_once_with("MNQ", 1)
        self.assertEqual(result["bars"], 2_331_102)
        self.assertEqual(result["first"], _StatusSnapshot.first_time)
        self.assertEqual(result["last"], _StatusSnapshot.last_time)
        self.assertEqual(result["state"], "FRESH")


class AccumulatorEventLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_cold_status_inspection_runs_off_the_event_loop_thread(self):
        event_loop_thread = threading.current_thread()
        status_threads: list[threading.Thread] = []
        now = datetime.now(UTC)

        def cold_status(_symbol):
            status_threads.append(threading.current_thread())
            return _status(now)

        with patch.object(accumulator, "store_status", side_effect=cold_status), \
                patch.object(accumulator, "_fetch", new=AsyncMock(return_value=[])):
            await accumulator.accumulate_once(
                ["MNQ"], client=object(), log=lambda _message: None
            )

        self.assertEqual(len(status_threads), 1)
        self.assertIsNot(status_threads[0], event_loop_thread)

    async def test_blocked_merge_does_not_block_event_loop_heartbeat(self):
        event_loop_thread = threading.current_thread()
        merge_entered = threading.Event()
        release_merge = threading.Event()
        merge_threads: list[threading.Thread] = []
        now = datetime.now(UTC)
        incoming = [_bar(now)]

        def blocking_merge(_bars, _symbol, _base):
            merge_threads.append(threading.current_thread())
            merge_entered.set()
            release_merge.wait()
            return 11, 1

        task = None
        try:
            with patch.object(accumulator, "store_status", return_value=_status(now)), \
                    patch.object(accumulator, "_fetch", new=AsyncMock(return_value=incoming)), \
                    patch.object(accumulator.candle_store, "merge", side_effect=blocking_merge):
                task = asyncio.create_task(accumulator.accumulate_once(
                    ["MNQ"], client=object(), log=lambda _message: None
                ))
                await asyncio.to_thread(merge_entered.wait)

                heartbeat = asyncio.get_running_loop().create_future()
                asyncio.get_running_loop().call_soon(heartbeat.set_result, True)
                self.assertTrue(await heartbeat)
                self.assertFalse(task.done())

                release_merge.set()
                result = await task
        finally:
            release_merge.set()
            if task is not None and not task.done():
                await task

        self.assertEqual(result["MNQ"]["added"], 1)
        self.assertEqual(len(merge_threads), 1)
        self.assertIsNot(merge_threads[0], event_loop_thread)


class ConcurrentStoreTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_store = candle_store.STORE_DIR
        candle_store.STORE_DIR = Path(self.tmp.name)
        candle_store.invalidate_cache()

    async def asyncTearDown(self):
        candle_store.STORE_DIR = self.original_store
        candle_store.invalidate_cache()
        self.tmp.cleanup()

    async def test_same_symbol_merges_are_serialized_and_preserve_union(self):
        start = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
        first_bar = _bar(start, 20000.0)
        second_bar = _bar(start + timedelta(minutes=1), 20001.0)
        first_load_entered = threading.Event()
        release_first_load = threading.Event()
        second_lock_lookup = threading.Event()
        second_thread_id: list[int] = []
        load_calls = 0
        load_calls_guard = threading.Lock()
        original_load = candle_store.load
        original_store_lock = candle_store._store_lock

        def controlled_load(symbol="MNQ", base=1, use_cache=True):
            nonlocal load_calls
            with load_calls_guard:
                load_calls += 1
                call_number = load_calls
            if symbol == "MNQ" and call_number == 1:
                first_load_entered.set()
                release_first_load.wait()
            return original_load(symbol, base, use_cache)

        def observed_store_lock(symbol="MNQ", base=1):
            if second_thread_id and threading.get_ident() == second_thread_id[0]:
                second_lock_lookup.set()
            return original_store_lock(symbol, base)

        def second_merge():
            second_thread_id.append(threading.get_ident())
            return candle_store.merge([second_bar], "MNQ", 1)

        first_task = None
        second_task = None
        try:
            with patch.object(candle_store, "load", side_effect=controlled_load), \
                    patch.object(candle_store, "_store_lock", side_effect=observed_store_lock):
                first_task = asyncio.create_task(asyncio.to_thread(
                    candle_store.merge, [first_bar], "MNQ", 1
                ))
                await asyncio.to_thread(first_load_entered.wait)

                # The merge owns the per-store lock for the entire read/merge/save
                # transaction, not only around the final atomic rename.
                lock = original_store_lock("MNQ", 1)
                acquired = lock.acquire(blocking=False)
                if acquired:
                    lock.release()
                self.assertFalse(acquired)

                second_task = asyncio.create_task(asyncio.to_thread(second_merge))
                await asyncio.to_thread(second_lock_lookup.wait)
                with load_calls_guard:
                    self.assertEqual(load_calls, 1)

                release_first_load.set()
                await asyncio.gather(first_task, second_task)
        finally:
            release_first_load.set()
            pending = [t for t in (first_task, second_task) if t is not None and not t.done()]
            if pending:
                await asyncio.gather(*pending)

        stored = candle_store.load("MNQ", 1)
        self.assertEqual(
            [bar.timestamp for bar in stored],
            [first_bar.timestamp, second_bar.timestamp],
        )

    def test_different_symbols_have_independent_transaction_locks(self):
        self.assertIsNot(
            candle_store._store_lock("MNQ", 1),
            candle_store._store_lock("MES", 1),
        )
        self.assertIs(
            candle_store._store_lock("MNQ", 1),
            candle_store._store_lock("mnq", 1),
        )


if __name__ == "__main__":
    unittest.main()
