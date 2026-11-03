"""P1: exercise the real PI startup wiring without a broker or Discord call."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from backend.db.models import StrategyParams
from backend.live.engine import LiveTradingEngine
from backend.live.pi_listener import BOT_ID, PiListener


def test_live_start_uses_bounded_five_second_listener_and_queues_signal(monkeypatch):
    contract = "CON.F.US.MNQ.U26"
    broker = MagicMock()
    messenger = MagicMock()
    messenger.start = AsyncMock()
    messenger.stop = AsyncMock()
    messenger.enabled = False
    with patch("backend.live.engine.EMAPMOSignalMessenger.from_env", return_value=messenger):
        engine = LiveTradingEngine(
            broker, 123, contract, contract_size=1,
            strategy_params=StrategyParams(strategy="pi", contract_id=contract, contract_size=1),
        )
    for name in ("_restore_daily_bot_risk_state", "_restore_pi_reopen_ticket",
                 "_register_param_snapshot", "_refresh_fade_levels",
                 "_resume_persisted_manual_guardian"):
        monkeypatch.setattr(engine, name, MagicMock())
    monkeypatch.setattr(engine, "_load_breakout_locks", lambda: [])
    monkeypatch.setattr(engine, "_refresh_account_snapshot", AsyncMock())
    monkeypatch.setattr(engine, "_prune_stale_pending_breakout_locks", AsyncMock())
    monkeypatch.setattr(engine.trend_follow, "push", MagicMock(return_value=True))
    monkeypatch.setattr("backend.backtest.shadow_replay.set_main_account", lambda *_: None)
    monkeypatch.setattr("backend.live.pi_listener.append_signal_event", lambda *_a, **_k: True)
    monkeypatch.setattr("backend.live.pi_listener.append_message_event", lambda *_a, **_k: True)
    created = {}

    def from_env(callback, **kwargs):
        listener = PiListener("unused", callback, **kwargs)
        listener._audit_status = lambda *_a, **_k: None
        listener.in_window = lambda: True
        listener._fetch = AsyncMock(side_effect=[
            [{"id": "900", "timestamp": "2026-10-26T14:00:00+00:00"}],
            [{"id": "901", "author": {"id": BOT_ID},
              "timestamp": "2026-10-26T14:01:00+00:00",
              "content": "@everyone (QQQ)\n• 青π×1 (中)"}],
        ])
        # The test drives one real poll directly, without a background network task.
        listener.run = AsyncMock(side_effect=lambda: asyncio.Event().wait())
        created["listener"] = listener
        return listener

    monkeypatch.setattr(PiListener, "from_env", from_env)
    monkeypatch.setattr(engine, "_main_loop", AsyncMock(side_effect=lambda: asyncio.Event().wait()))

    async def exercise():
        await engine.start([])
        listener = created["listener"]
        assert listener._poll == 5.0
        assert listener._bucket.limit == 30
        assert await listener._poll_once(MagicMock()) == 5.0
        assert engine.trend_follow.push.call_count == 1
        assert listener._last_id == "901"
        broker.place_order.assert_not_called()
        await engine._rollback_failed_start()

    asyncio.run(exercise())


def test_listener_respects_429_retry_and_recovers(monkeypatch):
    listener = PiListener("unused", lambda _sig: None, poll_seconds=5, rate_limit_per_min=30)
    listener._audit_status = lambda *_a, **_k: None
    limited = MagicMock(status_code=429)
    limited.json.return_value = {"retry_after": 2.0}
    ok = MagicMock(status_code=200)
    ok.json.return_value = []
    client = MagicMock()
    client.get = AsyncMock(side_effect=[limited, ok])
    sleep = AsyncMock()
    monkeypatch.setattr("backend.live.pi_listener.asyncio.sleep", sleep)

    async def exercise():
        assert await listener._fetch(client, {"limit": 1}) is None
        sleep.assert_awaited_once_with(2.5)
        assert await listener._fetch(client, {"limit": 1}) == []
        assert listener.get_health()["consecutive_errors"] == 0

    asyncio.run(exercise())
