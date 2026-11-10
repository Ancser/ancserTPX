"""PI → Grok Bot webhook: ancserTPX is only a forwarder.

What this pins (1.5.1i):
* the test session can never reach the real Bot (conftest neutralises the env);
* no URL configured = no request at all; a configured URL really POSTs;
* forwarding never runs on the listener's event loop thread;
* every payload carries received_at / catch_up / listener_role so the
  receiver can drop stale catch-up rows or dedupe the App recorder and the
  terminal listener by message_id. Nothing is filtered on this side.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import AsyncMock

from dotenv import load_dotenv

from backend.live import pi_grok_webhook
from backend.live.pi_listener import BOT_ID, PiListener, PiSignal

ROOT = Path(__file__).resolve().parents[1]
ENV_NAMES = ("GROK_PI_WEBHOOK_URL", "GROK_PI_WEBHOOK_AUTHORIZATION", "GROK_PI_WEBHOOK_KEY")


def test_test_session_cannot_reach_the_real_bot():
    for name in ENV_NAMES:
        assert name in os.environ                      # present, so load_dotenv skips it
        assert os.environ[name] == ""
    load_dotenv(ROOT / ".env", override=False)          # what backend modules do on import
    assert all(os.environ[name] == "" for name in ENV_NAMES)


def test_no_url_means_no_request(monkeypatch):
    calls = []
    monkeypatch.setattr(pi_grok_webhook.urllib.request, "urlopen", lambda *a, **k: calls.append(a))
    pi_grok_webhook.notify_pi_webhook({"event": "received", "message_id": "1"})
    assert calls == []


class _Sink(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        type(self).received.append((self.headers.get("Authorization"), json.loads(body)))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def test_configured_url_posts_payload_with_authorization(monkeypatch):
    _Sink.received = []
    server = HTTPServer(("127.0.0.1", 0), _Sink)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("GROK_PI_WEBHOOK_URL", f"http://127.0.0.1:{server.server_port}/hook")
        monkeypatch.setenv("GROK_PI_WEBHOOK_AUTHORIZATION", "Bearer test-key")
        pi_grok_webhook.notify_pi_webhook({"event": "received", "message_id": "42"})
    finally:
        server.shutdown()
    # Positive: the forwarder really delivers when configured.
    assert _Sink.received == [("Bearer test-key", {"event": "received", "message_id": "42"})]


def _signal(msg):
    return [PiSignal(
        message_id=str(msg["id"]),
        ts=datetime.fromisoformat(msg["timestamp"]),
        equity="QQQ", future="MNQ", direction=1, kind="青π", size="中", pos=None,
        raw=msg.get("content", ""),
    )]


def _capture_forwarding(monkeypatch):
    sent = []
    monkeypatch.setattr(
        "backend.live.pi_listener.notify_pi_webhook",
        lambda payload: sent.append((threading.get_ident(), payload)),
    )
    monkeypatch.setattr("backend.live.pi_listener.append_signal_event", lambda sig, **kw: True)
    monkeypatch.setattr("backend.live.pi_listener.append_message_event", lambda msg, **kw: True)
    return sent


def test_live_forward_runs_off_the_event_loop_and_is_tagged_live(monkeypatch):
    sent = _capture_forwarding(monkeypatch)
    listener = PiListener("token", lambda _sig: None)
    message = {"id": "123456789", "author": {"id": BOT_ID},
               "timestamp": "2026-08-10T17:00:00+00:00", "content": "@everyone (QQQ)\n• 青π×1 (中)"}

    async def run():
        await listener._dispatch_messages([message])
        await asyncio.gather(*list(listener._webhook_tasks))
        return threading.get_ident()

    loop_thread = asyncio.run(run())

    assert len(sent) == 1                                  # positive: the signal was forwarded
    worker_thread, payload = sent[0]
    assert worker_thread != loop_thread                    # never blocks polling / trading
    assert payload["message_id"] == "123456789" and payload["kind"] == "青π"
    assert payload["catch_up"] is False
    assert payload["listener_role"] == "trader"
    assert datetime.fromisoformat(payload["received_at"]).tzinfo is not None
    assert listener._webhook_tasks == set()                # references released when done


def test_startup_backfill_is_forwarded_but_tagged_catch_up(monkeypatch):
    sent = _capture_forwarding(monkeypatch)
    monkeypatch.setattr("backend.live.pi_listener.load_message_ids", lambda: set())
    monkeypatch.setattr("backend.live.pi_listener.load_message_timestamps", lambda: set())
    monkeypatch.setattr("backend.live.pi_listener.parse_message", _signal)
    listener = PiListener("token", lambda _sig: None, record_only=True)
    page = [{"id": "300", "timestamp": "2026-08-11T16:00:00+00:00", "author": {"id": BOT_ID}}]
    listener._fetch = AsyncMock(side_effect=[page, []])

    async def run():
        await listener.backfill_recent(now=datetime(2026, 8, 11, 17, 0, tzinfo=timezone.utc))
        await asyncio.gather(*list(listener._webhook_tasks))

    asyncio.run(run())

    assert [p["message_id"] for _, p in sent] == ["300"]  # positive: forwarded, not filtered
    payload = sent[0][1]
    assert payload["catch_up"] is True
    assert payload["listener_role"] == "recorder"
    assert listener._catching_up is False                  # flag scoped to the backfill dispatch
