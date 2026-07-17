from __future__ import annotations

import asyncio
import os
import sqlite3
import struct
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from backend.db.models import Candle, Direction, StrategyType, TradeSignal
from backend.live.emapmo_messenger import (
    DiscordSendError,
    EMAPMOSignalMessenger,
    _DiscordTransport,
    _SendResult,
    _render_signal_chart_png,
)


UTC = timezone.utc


def _bars(*, offset_minutes: int = 0, count: int = 26) -> list[Candle]:
    start = datetime(2026, 7, 15, 22, 0, tzinfo=UTC) + timedelta(minutes=offset_minutes)
    rows = []
    price = 22000.0
    for i in range(count):
        open_price = price + (i % 5) * 0.25
        close = open_price + (0.75 if i % 2 == 0 else -0.5)
        rows.append(Candle(
            timestamp=start + timedelta(minutes=5 * i),
            open=open_price,
            high=max(open_price, close) + 1.25,
            low=min(open_price, close) - 1.0,
            close=close,
            volume=100 + i,
            symbol="MNQ",
            interval="5m",
        ))
        price = close
    return rows


class _Strategy:
    def __init__(self, bars, *, legacy=False):
        self._bars = list(bars)
        self.timeframe_minutes = 5
        self.pmo_signal_mode = "early"
        self.signal_mode = "early" if legacy else "normal"
        self.pmo = [None] + [(-0.2 + i * 0.003) for i in range(1, len(bars))]
        self.sig = [None] + [(-0.18 + i * 0.002) for i in range(1, len(bars))]
        self.series_calls = 0

    def _pmo_series(self):
        self.series_calls += 1
        return list(self.pmo), list(self.sig)


def _signal(
    strategy: _Strategy,
    *,
    meta=None,
    direction=Direction.BUY,
    timestamp: datetime | None = None,
) -> TradeSignal:
    timestamp = timestamp or (strategy._bars[-1].timestamp + timedelta(minutes=4))
    return TradeSignal(
        strategy=StrategyType.TREND_FOLLOW,
        direction=direction,
        entry_price=22010.25,
        sl_price=21990.25,
        tp_price=22070.25,
        zone_id="test-emapmo",
        reason="test",
        zone_source="factor",
        timestamp=timestamp,
        order_type="market",
        meta=dict(meta or {}),
    )


class _CaptureTransport:
    mode = "capture"

    def __init__(self, shared=None):
        self.calls = shared if shared is not None else []
        self.closed = False

    async def send(self, content, image_bytes):
        self.calls.append((content, image_bytes))
        return _SendResult(message_id=f"msg-{len(self.calls)}", attempts=1)

    async def close(self):
        self.closed = True


class _FailTransport(_CaptureTransport):
    async def send(self, content, image_bytes):
        raise DiscordSendError("discord_http_401")


class _BlockingTransport(_CaptureTransport):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def send(self, content, image_bytes):
        self.started.set()
        await self.release.wait()
        return await super().send(content, image_bytes)


def _rows(db_path: Path):
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            "SELECT event_key, status, attempts, discord_message_id, last_error "
            "FROM emapmo_signals ORDER BY created_at_epoch, event_key"
        ).fetchall()


class EMAPMOSignalMessengerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fixed_now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)

    async def asyncTearDown(self):
        self.temp.cleanup()

    def _messenger(self, transport, **kwargs):
        return EMAPMOSignalMessenger(
            root=self.root,
            enabled=True,
            transport=transport,
            now_fn=lambda: self.fixed_now,
            **kwargs,
        )

    async def test_exact_strategy_series_and_both_emapmo_metadata_shapes(self):
        transport = _CaptureTransport()
        messenger = self._messenger(transport, chart_bars=20)
        await messenger.start()
        captured = []

        def render(snapshot):
            captured.append(snapshot)
            return b"png"

        factor_strategy = _Strategy(_bars())
        factor_signal = _signal(factor_strategy, meta={
            "signal_family": "emapmo",
            "trade_tf": "5m",
            "signal_detail": {"pmo": -0.12111, "signal": -0.10999},
        })
        ignored = _signal(factor_strategy, meta={"signal_family": "icefishball"})

        legacy_strategy = _Strategy(_bars(offset_minutes=300), legacy=True)
        legacy_signal = _signal(legacy_strategy, meta={
            "mode": "emapmo",
            "signal_mode": "early",
            "trade_tf": "5m",
            "pmo": -0.13111,
            "pmo_signal": -0.11999,
        })

        with patch("backend.live.emapmo_messenger._render_signal_chart_png", side_effect=render):
            self.assertFalse(messenger.enqueue_from_live(ignored, factor_strategy, "MNQU26", 3))
            self.assertTrue(messenger.enqueue_from_live(factor_signal, factor_strategy, "MNQU26", 3))
            self.assertTrue(messenger.enqueue_from_live(legacy_signal, legacy_strategy, "MNQU26", 3))
            await asyncio.wait_for(messenger._queue.join(), timeout=5)

        await messenger.stop()
        self.assertEqual(factor_strategy.series_calls, 1)
        self.assertEqual(legacy_strategy.series_calls, 1)
        self.assertEqual(len(captured), 2)
        self.assertEqual(captured[0].pmo_series, tuple(factor_strategy.pmo[-20:]))
        self.assertEqual(captured[0].signal_series, tuple(factor_strategy.sig[-20:]))
        self.assertEqual(captured[0].pmo_value, -0.12111)
        self.assertEqual(captured[1].signal_value, -0.11999)
        self.assertEqual([row[1] for row in _rows(messenger.history_path)], ["sent", "sent"])

    async def test_sqlite_deduplicates_same_market_signal_across_instances(self):
        shared_calls = []
        one = self._messenger(_CaptureTransport(shared_calls))
        two = self._messenger(_CaptureTransport(shared_calls))
        await one.start()
        await two.start()
        strategy_one = _Strategy(_bars())
        strategy_two = _Strategy(_bars())
        signal_one = _signal(strategy_one, meta={"signal_family": "emapmo"})
        signal_two = _signal(strategy_two, meta={"signal_family": "emapmo"})

        with patch("backend.live.emapmo_messenger._render_signal_chart_png", return_value=b"png"):
            self.assertTrue(one.enqueue_from_live(signal_one, strategy_one, "CON.F.US.MNQ.U26", 3))
            self.assertTrue(two.enqueue_from_live(signal_two, strategy_two, "CON.F.US.MNQ.U26", 3))
            await asyncio.wait_for(asyncio.gather(one._queue.join(), two._queue.join()), timeout=5)

        await asyncio.gather(one.stop(), two.stop())
        self.assertEqual(len(shared_calls), 1)
        self.assertEqual(len(_rows(one.history_path)), 1)

    async def test_queue_is_bounded_and_enqueue_never_waits(self):
        transport = _BlockingTransport()
        messenger = self._messenger(transport, queue_size=1)
        await messenger.start()
        strategies = [_Strategy(_bars(offset_minutes=400 * i)) for i in range(3)]
        signals = [_signal(s, meta={"signal_family": "emapmo"}) for s in strategies]

        with patch("backend.live.emapmo_messenger._render_signal_chart_png", return_value=b"png"):
            self.assertTrue(messenger.enqueue_from_live(signals[0], strategies[0], "MNQU26", 3))
            await asyncio.wait_for(transport.started.wait(), timeout=5)
            self.assertTrue(messenger.enqueue_from_live(signals[1], strategies[1], "MNQU26", 3))
            self.assertFalse(messenger.enqueue_from_live(signals[2], strategies[2], "MNQU26", 3))
            self.assertLessEqual(messenger._queue.qsize(), messenger.queue_maxsize)
            transport.release.set()
            await messenger.stop()

        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(len(_rows(messenger.history_path)), 2)

    async def test_chart_failure_falls_back_to_text_and_records_warning(self):
        transport = _CaptureTransport()
        messenger = self._messenger(transport)
        await messenger.start()
        strategy = _Strategy(_bars())
        signal = _signal(strategy, meta={"signal_family": "emapmo"})

        with patch("backend.live.emapmo_messenger._render_signal_chart_png", side_effect=ImportError):
            self.assertTrue(messenger.enqueue_from_live(signal, strategy, "MNQU26", 3))
            await asyncio.wait_for(messenger._queue.join(), timeout=5)
        await messenger.stop()

        self.assertEqual(len(transport.calls), 1)
        self.assertIsNone(transport.calls[0][1])
        self.assertIn("Chart unavailable", transport.calls[0][0])
        row = _rows(messenger.history_path)[0]
        self.assertEqual(row[1], "sent")
        self.assertEqual(row[4], "chart_ImportError")

    async def test_delivery_failure_is_sanitized_and_marked_failed(self):
        messenger = self._messenger(_FailTransport())
        await messenger.start()
        strategy = _Strategy(_bars())
        signal = _signal(strategy, meta={"signal_family": "emapmo"})
        with patch("backend.live.emapmo_messenger._render_signal_chart_png", return_value=b"png"):
            self.assertTrue(messenger.enqueue_from_live(signal, strategy, "MNQU26", 3))
            await asyncio.wait_for(messenger._queue.join(), timeout=5)
        await messenger.stop()
        row = _rows(messenger.history_path)[0]
        self.assertEqual(row[1], "failed")
        self.assertEqual(row[4], "discord_http_401")

    async def test_history_init_failure_disables_without_raising(self):
        messenger = self._messenger(_CaptureTransport())
        with patch.object(messenger, "_init_db_sync", side_effect=PermissionError):
            await messenger.start()
        self.assertFalse(messenger.enabled)
        strategy = _Strategy(_bars())
        self.assertFalse(messenger.enqueue_from_live(
            _signal(strategy, meta={"signal_family": "emapmo"}),
            strategy,
            "MNQU26",
            3,
        ))
        await messenger.stop()

    async def test_thirty_day_prune_and_metadata_only_schema(self):
        messenger = self._messenger(_CaptureTransport(), history_days=30)
        await messenger.start()
        old_strategy = _Strategy(_bars())
        new_strategy = _Strategy(_bars(offset_minutes=500))
        old = messenger._snapshot_from_live(
            _signal(old_strategy, meta={"signal_family": "emapmo"}),
            old_strategy, "MNQU26", 3,
        )
        new = messenger._snapshot_from_live(
            _signal(new_strategy, meta={"signal_family": "emapmo"}),
            new_strategy, "MNQU26", 3,
        )
        old = replace(old, created_at_epoch=int((self.fixed_now - timedelta(days=31)).timestamp()))
        new = replace(new, created_at_epoch=int((self.fixed_now - timedelta(days=29)).timestamp()))
        self.assertTrue(messenger._claim_sync(old))
        self.assertTrue(messenger._claim_sync(new))
        messenger._prune_sync(int((self.fixed_now - timedelta(days=30)).timestamp()))

        with sqlite3.connect(messenger.history_path) as connection:
            keys = [row[0] for row in connection.execute("SELECT event_key FROM emapmo_signals")]
            schema = connection.execute("PRAGMA table_info(emapmo_signals)").fetchall()
        await messenger.stop()

        self.assertEqual(keys, [new.event_key])
        column_names = {str(row[1]).lower() for row in schema}
        column_types = {str(row[2]).upper() for row in schema}
        self.assertFalse({"image", "png", "bars", "candles", "payload"} & column_names)
        self.assertNotIn("BLOB", column_types)


class EMAPMOMessageTests(unittest.TestCase):
    def _snapshot(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        messenger = EMAPMOSignalMessenger(
            root=Path(temp.name), enabled=True, transport=_CaptureTransport(), chart_bars=20
        )
        strategy = _Strategy(_bars())
        signal = _signal(strategy, meta={"signal_family": "emapmo"})
        return messenger._snapshot_from_live(signal, strategy, "MNQU26", 3)

    def test_ascii_condition_text_for_all_four_signal_variants(self):
        base = self._snapshot()
        cases = (
            (
                "early", "long",
                (-0.13000, -0.12700, -0.12500),
                (-0.11000, -0.11000, -0.11000),
                ("SIG < -0.10000: -0.11000", "PMO < SIG: -0.12500 < -0.11000",
                 "SIG-PMO: NOW 0.01500 < PREV 0.01700 < PREV2 0.02000"),
            ),
            (
                "early", "short",
                (0.08500, 0.08200, 0.08000),
                (0.07000, 0.07000, 0.07000),
                ("SIG > 0.06000: 0.07000", "PMO > SIG: 0.08000 > 0.07000",
                 "PMO-SIG: NOW 0.01000 < PREV 0.01200 < PREV2 0.01500"),
            ),
            (
                "normal", "long",
                (-0.13000, -0.12500),
                (-0.12800, -0.12600),
                ("PMO < -0.10000: -0.12500",
                 "CROSS UP: PMO -0.13000 <= SIG -0.12800 -> PMO -0.12500 > SIG -0.12600"),
            ),
            (
                "normal", "short",
                (0.06500, 0.07000),
                (0.06400, 0.07100),
                ("PMO > 0.06000: 0.07000",
                 "CROSS DOWN: PMO 0.06500 >= SIG 0.06400 -> PMO 0.07000 < SIG 0.07100"),
            ),
        )
        for mode, direction, pmo, sig, expected_lines in cases:
            with self.subTest(mode=mode, direction=direction):
                snapshot = replace(
                    base,
                    signal_mode=mode,
                    direction=direction,
                    pmo_value=pmo[-1],
                    signal_value=sig[-1],
                    pmo_series=pmo,
                    signal_series=sig,
                )
                content = snapshot.message_text()
                self.assertTrue(content.isascii())
                self.assertEqual(content.splitlines()[0], "ICE PI signal")
                self.assertIn(f"{mode.upper()} {direction.upper()}", content)
                for expected in expected_lines:
                    self.assertIn(expected, content)
                self.assertNotIn("Entry", content)
                self.assertNotIn("Qty", content)

    def test_both_mode_reports_the_branch_that_actually_fired(self):
        base = self._snapshot()
        cases = (
            ("long", (-0.130, -0.125), (-0.128, -0.126), "NORMAL"),
            ("long", (-0.130, -0.127, -0.125), (-0.110, -0.110, -0.110), "EARLY"),
            ("short", (0.065, 0.070), (0.064, 0.071), "NORMAL"),
            ("short", (0.085, 0.082, 0.080), (0.070, 0.070, 0.070), "EARLY"),
        )
        for direction, pmo, sig, expected in cases:
            with self.subTest(direction=direction, expected=expected):
                snapshot = replace(
                    base,
                    signal_mode="both",
                    direction=direction,
                    pmo_value=pmo[-1],
                    signal_value=sig[-1],
                    pmo_series=pmo,
                    signal_series=sig,
                )
                self.assertEqual(snapshot.matched_signal_mode(), expected.lower())
                self.assertIn(f"| {expected} {direction.upper()}", snapshot.message_text())

    def test_both_mode_with_missing_history_does_not_claim_a_condition(self):
        snapshot = replace(
            self._snapshot(),
            signal_mode="both",
            pmo_value=-0.12000,
            signal_value=-0.11000,
            pmo_series=(-0.12000,),
            signal_series=(-0.11000,),
        )
        content = snapshot.message_text()
        self.assertIn("| BOTH LONG", content)
        self.assertIn("PMO -0.12000 | SIG -0.11000", content)


class EMAPMOChartTests(unittest.TestCase):
    def test_matplotlib_chart_is_exact_1280_by_720_png(self):
        with tempfile.TemporaryDirectory() as temp:
            messenger = EMAPMOSignalMessenger(
                root=Path(temp), enabled=True, transport=_CaptureTransport(), chart_bars=20
            )
            strategy = _Strategy(_bars())
            signal = _signal(strategy, meta={
                "signal_family": "emapmo",
                "signal_detail": {"pmo": -0.125, "signal": -0.11},
            })
            snapshot = messenger._snapshot_from_live(signal, strategy, "MNQU26", 3)
            png = _render_signal_chart_png(snapshot)

        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        width, height = struct.unpack(">II", png[16:24])
        self.assertEqual((width, height), (1280, 720))


class _Response:
    status_code = 200
    headers = {}

    def json(self):
        return {"id": "123"}


class _HTTPClient:
    def __init__(self):
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response()


class _ReadTimeoutHTTPClient(_HTTPClient):
    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        raise httpx.ReadTimeout("secret URL must not be surfaced", request=httpx.Request("POST", url))


class DiscordTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_and_user_token_authorization_modes(self):
        for mode, expected in (("bot", "Bot top-secret"), ("user", "top-secret")):
            client = _HTTPClient()
            transport = _DiscordTransport(
                token="top-secret", channel_id="456", auth_mode=mode, client=client
            )
            result = await transport.send("hello", None)
            self.assertEqual(result.message_id, "123")
            self.assertEqual(client.calls[0][1]["headers"]["Authorization"], expected)

    async def test_ambiguous_read_timeout_is_not_retried(self):
        client = _ReadTimeoutHTTPClient()
        transport = _DiscordTransport(
            webhook_url="https://discord.com/api/webhooks/id/top-secret", client=client
        )
        with self.assertRaises(DiscordSendError) as raised:
            await transport.send("hello", None)
        self.assertEqual(raised.exception.code, "discord_delivery_uncertain")
        self.assertEqual(len(client.calls), 1)

    async def test_from_env_prefers_webhook_without_exposing_secret(self):
        keys = {
            "EMAPMO_MESSENGER_ENABLED": "true",
            "EMAPMO_DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/id/secret",
            "DISCORD_TOKEN": "other-secret",
            "EMAPMO_DISCORD_CHANNEL_ID": "456",
            "EMAPMO_DISCORD_AUTH_MODE": "user",
            "EMAPMO_SIGNAL_TIMEZONE": "America/Los_Angeles",
        }
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, keys, clear=False):
            messenger = EMAPMOSignalMessenger.from_env(Path(temp))
        self.assertTrue(messenger.enabled)
        self.assertEqual(messenger.delivery_mode, "webhook")
        self.assertEqual(messenger.timezone_name, "America/Los_Angeles")

    async def test_credentials_alone_do_not_enable_external_posting(self):
        keys = {
            "EMAPMO_DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/id/secret",
        }
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, keys, clear=True):
            messenger = EMAPMOSignalMessenger.from_env(Path(temp))
        self.assertFalse(messenger.enabled)


if __name__ == "__main__":
    unittest.main()
