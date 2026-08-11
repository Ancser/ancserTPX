from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.api import routes
from backend.data import candle_store as cs
from backend.db.models import Candle


UTC = timezone.utc
CONTRACT = "CON.F.US.MNQ.U26"


def _bars(count: int, start: datetime | None = None) -> list[Candle]:
    first = start or datetime(2020, 1, 1, tzinfo=UTC)
    return [
        Candle(
            timestamp=first + timedelta(minutes=i),
            open=20000.0 + i,
            high=20001.0 + i,
            low=19999.0 + i,
            close=20000.5 + i,
            volume=100 + i,
            symbol="MNQ",
            interval="1m",
        )
        for i in range(count)
    ]


class CandleSnapshotRangeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_store = cs.STORE_DIR
        cs.STORE_DIR = Path(self.tmp.name)
        cs.invalidate_cache()

    def tearDown(self):
        cs.STORE_DIR = self.original_store
        cs.invalidate_cache()
        self.tmp.cleanup()

    def test_cached_snapshot_is_immutable_and_range_selection_uses_bisect(self):
        bars = _bars(1000)
        cs.save(bars, "MNQ", 1)

        snapshot = cs.load_snapshot("MNQ", 1)
        self.assertIs(snapshot, cs.load_snapshot("MNQ", 1))
        self.assertIsInstance(snapshot.bars, tuple)

        start = bars[400].timestamp
        end = bars[409].timestamp
        with patch.object(cs, "bisect_left", wraps=cs.bisect_left) as left, \
                patch.object(cs, "bisect_right", wraps=cs.bisect_right) as right:
            selected = cs.select_range(snapshot, start, end)

        self.assertEqual([c.timestamp for c in selected],
                         [c.timestamp for c in bars[400:410]])
        self.assertEqual(left.call_count, 1)
        self.assertEqual(right.call_count, 1)
        selected.pop()
        self.assertEqual(len(snapshot.bars), 1000)

    def test_save_publishes_a_new_generation_without_truncating_prior_bars(self):
        initial = _bars(20)
        cs.save(initial, "MNQ", 1)
        first = cs.load_snapshot("MNQ", 1)

        cs.merge(_bars(5), "MNQ", 1)
        second = cs.load_snapshot("MNQ", 1)

        self.assertIsNot(first, second)
        self.assertEqual(len(second.bars), 20)
        self.assertEqual(second.bars[0].timestamp, initial[0].timestamp)
        self.assertEqual(second.bars[-1].timestamp, initial[-1].timestamp)


class _FakeTopstepXClient:
    historical_calls = 0
    instances = []

    def __init__(self, **_kwargs):
        self.disconnect_calls = 0
        type(self).instances.append(self)

    async def authenticate(self):
        return True

    async def disconnect(self):
        self.disconnect_calls += 1
        return None

    async def get_front_month_contract_id(self, _contract_id):
        return CONTRACT

    async def get_nq_contract_id(self):
        return CONTRACT

    async def get_previous_quarter_contract_id(self, _contract_id):
        raise AssertionError("contained working-set hit must precede contract expansion")

    async def get_historical_bars_paginated(self, **_kwargs):
        type(self).historical_calls += 1
        raise AssertionError("contained range must not call the broker")


class _PersistentLiveClient:
    def __init__(self):
        self.disconnect_calls = 0

    async def disconnect(self):
        self.disconnect_calls += 1


class HistoricalRangeRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_candles = routes._historical_candles
        self.original_snapshot = routes._historical_working_snapshot
        self.original_client = routes._topstepx_client
        self.original_contract = routes._live_contract_id
        self.original_live_engine = routes._live_engine
        self.original_live_engines = routes._live_engines
        self.original_live_start_refs = routes._live_start_client_refs
        routes._historical_candles = []
        routes._historical_working_snapshot = None
        routes._topstepx_client = None
        routes._live_engine = None
        routes._live_engines = {}
        routes._live_start_client_refs = {}
        _FakeTopstepXClient.historical_calls = 0
        _FakeTopstepXClient.instances = []

    def tearDown(self):
        routes._historical_candles = self.original_candles
        routes._historical_working_snapshot = self.original_snapshot
        routes._topstepx_client = self.original_client
        routes._live_contract_id = self.original_contract
        routes._live_engine = self.original_live_engine
        routes._live_engines = self.original_live_engines
        routes._live_start_client_refs = self.original_live_start_refs

    async def test_five_year_generation_to_pi_subrange_avoids_all_full_store_work(self):
        all_bars = _bars(5000)
        source_key = routes._historical_source_key(CONTRACT, 2, 1, True)
        routes._publish_historical_candles(
            all_bars,
            source_key=source_key,
            requested_start=all_bars[0].timestamp,
            requested_end=all_bars[-1].timestamp,
            contract_id=CONTRACT,
            contracts=["CON.F.US.MNQ.M26", CONTRACT],
            continuous_meta={"roll_at": "2026-06-12T00:00:00+00:00"},
            from_store=True,
        )
        token = routes._historical_working_snapshot.token
        start = all_bars[4700].timestamp
        end = all_bars[4799].timestamp
        req = routes.FetchHistoricalRequest(
            contract_id=CONTRACT, workset_token=token,
            unit=2, unit_number=1,
            start_time=start.isoformat(), end_time=end.isoformat(),
            continuous_contract=True,
        )

        with patch("backend.broker.topstepx.TopstepXClient",
                   side_effect=AssertionError("pre-auth hit must not construct broker")), \
                patch.object(routes, "_store_load_snapshot",
                             side_effect=AssertionError("must not load full store")), \
                patch.object(routes, "_store_save",
                             side_effect=AssertionError("must not write store")), \
                patch.object(routes, "_store_detect_gaps",
                             side_effect=AssertionError("must not scan gaps")):
            result = await routes.fetch_historical(req)

        self.assertTrue(result["range_cache_hit"])
        self.assertEqual(result["cache_kind"], "working_set")
        self.assertEqual(result["candles_count"], 100)
        self.assertEqual(_FakeTopstepXClient.historical_calls, 0)
        self.assertEqual([c.timestamp for c in routes._historical_candles],
                         [c.timestamp for c in all_bars[4700:4800]])
        self.assertIsNot(routes._historical_candles, all_bars)

    async def test_persistent_snapshot_hit_selects_exact_range_without_merge_or_write(self):
        bars = _bars(1000)
        snapshot = cs.CandleSnapshot(
            "MNQ", 1, Path("unused.pkl"), (1, 1), tuple(bars)
        )
        start = bars[600].timestamp
        end = bars[649].timestamp
        req = routes.FetchHistoricalRequest(
            username="test", api_key="test", contract_id=CONTRACT,
            unit=2, unit_number=1,
            start_time=start.isoformat(), end_time=end.isoformat(),
            continuous_contract=False,
        )
        event_thread = threading.get_ident()
        load_threads = []

        def load_snapshot(_symbol):
            load_threads.append(threading.get_ident())
            return snapshot

        with patch("backend.broker.topstepx.TopstepXClient", _FakeTopstepXClient), \
                patch.object(routes, "_store_load_snapshot", side_effect=load_snapshot), \
                patch.object(routes, "_store_save",
                             side_effect=AssertionError("must not write store")), \
                patch.object(routes, "_store_detect_gaps",
                             side_effect=AssertionError("must not scan gaps")):
            result = await routes.fetch_historical(req)

        self.assertEqual(result["cache_kind"], "store_snapshot")
        self.assertEqual(result["candles_count"], 50)
        self.assertEqual([c.timestamp for c in routes._historical_candles],
                         [c.timestamp for c in bars[600:650]])
        self.assertEqual(_FakeTopstepXClient.historical_calls, 0)
        self.assertEqual(len(load_threads), 1)
        self.assertNotEqual(load_threads[0], event_thread)

    async def test_append_uses_only_grows_store_merge_not_delta_overwrite(self):
        bars = _bars(100)
        source_key = routes._historical_source_key(CONTRACT, 2, 1, False)
        routes._publish_historical_candles(
            bars,
            source_key=source_key,
            requested_start=bars[0].timestamp,
            requested_end=bars[-1].timestamp,
            contract_id=CONTRACT,
            contracts=[CONTRACT],
            continuous_meta={},
            from_store=True,
        )
        appended = _bars(2, bars[-1].timestamp + timedelta(minutes=1))
        new_end = appended[-1].timestamp
        req = routes.FetchHistoricalRequest(
            username="test", api_key="test", contract_id=CONTRACT,
            unit=2, unit_number=1,
            start_time=appended[0].timestamp.isoformat(),
            end_time=new_end.isoformat(), append=True,
            continuous_contract=False,
        )
        event_thread = threading.get_ident()
        merge_threads = []

        def merge_store(_candles, _symbol):
            merge_threads.append(threading.get_ident())
            return 102, 2

        with patch("backend.broker.topstepx.TopstepXClient", _FakeTopstepXClient), \
                patch.object(
                    _FakeTopstepXClient, "get_historical_bars_paginated",
                    new=AsyncMock(return_value=appended),
                ), patch.object(routes, "_store_merge", side_effect=merge_store) as merge, \
                patch.object(routes, "_store_save",
                             side_effect=AssertionError("delta must never overwrite store")):
            result = await routes.fetch_historical(req)

        merge.assert_called_once_with(appended, "MNQ")
        self.assertEqual(len(merge_threads), 1)
        self.assertNotEqual(merge_threads[0], event_thread)
        self.assertEqual(result["candles_count"], 102)
        self.assertEqual(len(routes._historical_candles), 102)
        self.assertEqual(routes._historical_working_snapshot.requested_end, new_end)

    async def test_noncontained_request_preserves_normal_broker_fetch_fallback(self):
        fetched = _bars(25, datetime(2026, 8, 1, tzinfo=UTC))
        req = routes.FetchHistoricalRequest(
            username="test", api_key="test", contract_id=CONTRACT,
            unit=2, unit_number=1,
            start_time=fetched[0].timestamp.isoformat(),
            end_time=fetched[-1].timestamp.isoformat(),
            force_full=True, continuous_contract=False,
        )
        broker_fetch = AsyncMock(return_value=fetched)

        with patch("backend.broker.topstepx.TopstepXClient", _FakeTopstepXClient), \
                patch.object(
                    _FakeTopstepXClient, "get_historical_bars_paginated",
                    new=broker_fetch,
                ), patch.object(routes, "_store_save") as save, \
                patch.object(routes, "_store_detect_gaps", return_value=[]), \
                patch.object(routes, "_store_advance_frozen"):
            result = await routes.fetch_historical(req)

        broker_fetch.assert_awaited_once()
        save.assert_called_once()
        self.assertFalse(result["range_cache_hit"])
        self.assertEqual(result["cache_kind"], "fallback")
        self.assertEqual(result["candles_count"], len(fetched))
        self.assertEqual(routes._historical_candles, fetched)

    async def test_active_live_client_and_contract_are_never_replaced_by_history_fetch(self):
        fetched = _bars(4, datetime(2026, 8, 3, tzinfo=UTC))
        live_client = _PersistentLiveClient()
        live_contract = "CON.F.US.MNQ.M26"
        routes._topstepx_client = live_client
        routes._live_contract_id = live_contract
        routes._live_engines = {7: type("Engine", (), {"is_running": True})()}
        req = routes.FetchHistoricalRequest(
            username="test", api_key="test", contract_id=CONTRACT,
            unit=2, unit_number=1,
            start_time=fetched[0].timestamp.isoformat(),
            end_time=fetched[-1].timestamp.isoformat(),
            force_full=True, continuous_contract=False,
        )

        with patch("backend.broker.topstepx.TopstepXClient", _FakeTopstepXClient), \
                patch.object(
                    _FakeTopstepXClient, "get_historical_bars_paginated",
                    new=AsyncMock(return_value=fetched),
                ), patch.object(routes, "_store_save"), \
                patch.object(routes, "_store_detect_gaps", return_value=[]), \
                patch.object(routes, "_store_advance_frozen"):
            await routes.fetch_historical(req)

        self.assertIs(routes._topstepx_client, live_client)
        self.assertEqual(routes._live_contract_id, live_contract)
        self.assertEqual(live_client.disconnect_calls, 0)
        self.assertEqual(len(_FakeTopstepXClient.instances), 1)
        self.assertEqual(_FakeTopstepXClient.instances[0].disconnect_calls, 1)

    async def test_live_start_can_only_capture_new_owner_while_old_disconnect_waits(self):
        fetched = _bars(3, datetime(2026, 8, 3, tzinfo=UTC))

        class BlockingOldClient:
            def __init__(self):
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def disconnect(self):
                self.started.set()
                await self.release.wait()

        old_client = BlockingOldClient()
        routes._topstepx_client = old_client
        req = routes.FetchHistoricalRequest(
            username="test", api_key="test", contract_id=CONTRACT,
            unit=2, unit_number=1,
            start_time=fetched[0].timestamp.isoformat(),
            end_time=fetched[-1].timestamp.isoformat(),
            force_full=True, continuous_contract=False,
        )
        captured = []

        async def capture_start(_req, client):
            captured.append(client)
            return {"success": True}

        with patch("backend.broker.topstepx.TopstepXClient", _FakeTopstepXClient), \
                patch.object(
                    _FakeTopstepXClient, "get_historical_bars_paginated",
                    new=AsyncMock(return_value=fetched),
                ), patch.object(routes, "_store_save"), \
                patch.object(routes, "_store_detect_gaps", return_value=[]), \
                patch.object(routes, "_store_advance_frozen"), \
                patch.object(routes, "_live_start_impl", side_effect=capture_start):
            fetch_task = asyncio.create_task(routes.fetch_historical(req))
            await old_client.started.wait()
            new_client = routes._topstepx_client
            self.assertIsNot(new_client, old_client)

            await routes.live_start(routes.LiveStartRequest(account_id=1))
            old_client.release.set()
            await fetch_task

        self.assertEqual(captured, [new_client])
        self.assertEqual(routes._live_start_client_refs, {})

    async def test_two_starts_keep_refcount_until_both_finish(self):
        client = _PersistentLiveClient()
        routes._topstepx_client = client
        releases = [asyncio.Event(), asyncio.Event()]
        entered = asyncio.Queue()

        async def blocked_start(req, captured_client):
            await entered.put((req.account_id, captured_client))
            await releases[req.account_id - 1].wait()
            return {"success": True}

        with patch.object(routes, "_live_start_impl", side_effect=blocked_start):
            first = asyncio.create_task(routes.live_start(routes.LiveStartRequest(account_id=1)))
            second = asyncio.create_task(routes.live_start(routes.LiveStartRequest(account_id=2)))
            await entered.get()
            await entered.get()
            self.assertEqual(routes._live_start_client_refs[id(client)], 2)
            releases[0].set()
            await first
            self.assertEqual(routes._live_start_client_refs[id(client)], 1)
            self.assertTrue(routes._has_running_live_engine())
            releases[1].set()
            await second

        self.assertEqual(routes._live_start_client_refs, {})

    async def test_store_operations_run_off_the_event_loop_thread(self):
        fetched = _bars(4, datetime(2026, 8, 4, tzinfo=UTC))
        event_thread = threading.get_ident()
        worker_threads = []

        def record_save(_candles, _symbol):
            worker_threads.append(threading.get_ident())

        def record_gaps(_candles):
            worker_threads.append(threading.get_ident())
            return []

        def record_advance(_candles, _symbol):
            worker_threads.append(threading.get_ident())

        req = routes.FetchHistoricalRequest(
            username="test", api_key="test", contract_id=CONTRACT,
            unit=2, unit_number=1,
            start_time=fetched[0].timestamp.isoformat(),
            end_time=fetched[-1].timestamp.isoformat(),
            force_full=True, continuous_contract=False,
        )
        with patch("backend.broker.topstepx.TopstepXClient", _FakeTopstepXClient), \
                patch.object(
                    _FakeTopstepXClient, "get_historical_bars_paginated",
                    new=AsyncMock(return_value=fetched),
                ), patch.object(routes, "_store_save", side_effect=record_save), \
                patch.object(routes, "_store_detect_gaps", side_effect=record_gaps), \
                patch.object(routes, "_store_advance_frozen", side_effect=record_advance):
            await routes.fetch_historical(req)

        self.assertEqual(len(worker_threads), 3)
        self.assertTrue(all(tid != event_thread for tid in worker_threads))

    def test_live_tail_replacement_and_append_never_call_full_rebuild(self):
        bars = _bars(10_000)
        routes._historical_candles = list(bars)
        replacement = Candle(
            timestamp=bars[-1].timestamp,
            open=1.0, high=2.0, low=0.0, close=1.5, volume=999,
            symbol="MNQ", interval="1m",
        )
        appended = _bars(2, bars[-1].timestamp + timedelta(minutes=1))

        with patch.object(
            routes, "_rebuild_historical_candles",
            side_effect=AssertionError("normal live tail must stay bounded"),
        ):
            routes._upsert_historical_candles([replacement, *appended])

        self.assertEqual(len(routes._historical_candles), 10_002)
        self.assertIs(routes._historical_candles[0], bars[0])
        self.assertIs(routes._historical_candles[-3], replacement)
        self.assertEqual(routes._historical_candles[-1].timestamp,
                         appended[-1].timestamp)
        self.assertIsNone(routes._historical_working_snapshot)

    def test_live_tail_refresh_isolated_and_does_not_bless_missing_tail(self):
        bars = _bars(1000)
        source_key = routes._historical_source_key(CONTRACT, 2, 1, True)
        requested_end = bars[-1].timestamp + timedelta(hours=2)
        routes._publish_historical_candles(
            bars,
            source_key=source_key,
            requested_start=bars[0].timestamp,
            requested_end=requested_end,
            contract_id=CONTRACT,
            contracts=[CONTRACT],
            continuous_meta={},
            from_store=True,
        )
        generation = routes._historical_working_snapshot
        replacement = Candle(
            timestamp=bars[-1].timestamp,
            open=1.0, high=2.0, low=0.0, close=1.75, volume=999,
            symbol="MNQ", interval="1m",
        )
        appended = _bars(1, bars[-1].timestamp + timedelta(minutes=1))[0]

        routes._upsert_historical_candles([replacement, appended])
        selected = routes._select_working_historical_range(
            source_key, bars[-10].timestamp, requested_end
        )

        self.assertIs(routes._historical_working_snapshot, generation)
        self.assertIsNone(selected)
        contained = routes._select_working_historical_range(
            source_key, bars[-10].timestamp, bars[-1].timestamp
        )
        self.assertIsNotNone(contained)
        selected_bars, _ = contained
        self.assertIs(selected_bars[-1], bars[-1])
        self.assertIs(generation.candles[-1], bars[-1])
        self.assertIs(routes._historical_candles[-2], replacement)
        self.assertIs(routes._historical_candles[-1], appended)

    def test_seed_generation_expires_when_canonical_store_appears(self):
        bars = _bars(10)
        source_key = routes._historical_source_key(CONTRACT, 2, 1, False)
        with tempfile.TemporaryDirectory() as tmp:
            seed_path = Path(tmp) / "seed.pkl"
            canonical_path = Path(tmp) / "MNQ.pkl"
            seed_path.write_bytes(b"seed")
            stat = seed_path.stat()
            routes._publish_historical_candles(
                bars,
                source_key=source_key,
                requested_start=bars[0].timestamp,
                requested_end=bars[-1].timestamp,
                contract_id=CONTRACT,
                contracts=[CONTRACT],
                continuous_meta={},
                from_store=True,
                store_symbol="MNQ",
                store_path=seed_path,
                store_version=(stat.st_mtime_ns, stat.st_size),
            )
            with patch.object(routes, "_store_file_path", return_value=canonical_path):
                self.assertIsNotNone(routes._select_working_historical_range(
                    source_key, bars[0].timestamp, bars[-1].timestamp,
                ))
                canonical_path.write_bytes(b"canonical")
                self.assertIsNone(routes._select_working_historical_range(
                    source_key, bars[0].timestamp, bars[-1].timestamp,
                ))


if __name__ == "__main__":
    unittest.main()
