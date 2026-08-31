from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from backend.db.models import Direction, StrategyParams, StrategyType, TradeSignal
from backend.live import engine as engine_module
from backend.live.engine import LiveTradingEngine
from backend.live.manual_guardian_launcher import (
    GuardianLaunchResult,
    GuardianLaunchStatus,
    GuardianStateSnapshot,
)


CONTRACT = "CON.F.US.MNQ.U26"
POSITION = {
    "id": 7001,
    "accountId": 123,
    "contractId": CONTRACT,
    "type": 1,
    "size": 3,
    "averagePrice": 100.0,
    "creationTimestamp": "2026-07-17T00:53:22Z",
}


class FakeClient:
    def __init__(self, *, positions=None, orders=None):
        self.positions = list(positions or [])
        self.orders = list(orders or [])
        self.order_history = []
        self.open_order_reads = 0
        self.close_calls = []

    async def get_positions(self, account_id):
        return [dict(row) for row in self.positions]

    async def get_open_orders(self, account_id):
        self.open_order_reads += 1
        return [dict(row) for row in self.orders]

    async def get_orders(self, account_id):
        return [dict(row) for row in self.order_history + self.orders]

    async def cancel_order(self, account_id, order_id):
        for row in self.orders:
            if row.get("id") == order_id:
                self.order_history.append({**row, "status": 3})
                self.orders = [item for item in self.orders if item.get("id") != order_id]
                return True
        return False

    async def close_position(self, account_id, contract_id):
        self.close_calls.append((account_id, contract_id))
        self.positions = [row for row in self.positions if row.get("contractId") != contract_id]
        return SimpleNamespace(success=True, error_code=None, error_message=None)


def make_engine(
    client: FakeClient, *, guardian_enabled: bool = True,
) -> LiveTradingEngine:
    params = StrategyParams(
        strategy="factor",
        contract_id=CONTRACT,
        contract_size=3,
        factor_sl_rule="atr_blend",
        factor_tp_rule="atr_blend",
        factor_sl_value=2.5,
        factor_tp_value=7.5,
    )
    item = LiveTradingEngine(
        client,
        account_id=123,
        contract_id=CONTRACT,
        strategy_params=params,
    )
    item._open_position = dict(POSITION)
    item._last_market_price = 101.0
    if guardian_enabled:
        # The legacy guardian remains testable as a dormant recovery path, but
        # production defaults to observe-only ownership for manual positions.
        item.MANUAL_POSITION_POLICY = "guardian"
    item.trend_follow._risk_width = (
        lambda rule, value: 10.0 if float(value) == 2.5 else 30.0
    )
    return item


def _prepare_close_window_tick(item: LiveTradingEngine, monkeypatch) -> None:
    class FixedDateTime(datetime):
        @classmethod
        def utcnow(cls):
            return cls(2026, 1, 15, 20, 45)

    monkeypatch.setattr(engine_module, "datetime", FixedDateTime)
    item._today = "2026-01-15"
    item._get_topstep_trade_date = Mock(return_value="2026-01-15")
    item._sync_position = AsyncMock()
    item._last_account_refresh = time.time()
    item._monitor_auto_oco_protection = AsyncMock(return_value=False)
    item._fetch_latest_candles = AsyncMock(return_value=[
        engine_module.Candle(
            timestamp=datetime(2026, 1, 15, 20, 44, tzinfo=timezone.utc),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=10,
        )
    ])
    item._last_candle_time = None
    item._last_status_log_minute = 45
    item._candles_processed = 0
    item.detector = Mock()
    item.confluence = None
    item._append_history = Mock()
    item._update_tf_breakout = Mock()
    item.trend_follow.observe = Mock()
    item._save_zones = Mock()
    item._pending_order_id = None
    item.flatten_now = AsyncMock()


def snapshot(
    *,
    state_exists=False,
    lock_exists=False,
    running=False,
    pid=None,
    lock_status="missing",
    status="missing",
    sl_price=None,
    tp_price=None,
    sl_order_id=None,
    tp_order_id=None,
    error=None,
    position_id=7001,
    contract_id=CONTRACT,
    side="long",
    creation_timestamp=POSITION["creationTimestamp"],
    entry_price=100.0,
    size=3,
    account_busy=False,
    lock_position_id=None,
    lock_state_path=None,
):
    return GuardianStateSnapshot(
        account_id=123,
        position_id=position_id,
        state_path="guardian.json",
        lock_path="guardian.json.lock",
        log_path="guardian.log",
        state_exists=state_exists,
        lock_exists=lock_exists,
        running=running,
        pid=pid,
        lock_status=lock_status,
        status=status,
        contract_id=contract_id if state_exists else None,
        side=side if state_exists else None,
        creation_timestamp=(
            creation_timestamp if state_exists else None
        ),
        size=size if state_exists else None,
        entry_price=entry_price if state_exists else None,
        sl_price=sl_price,
        tp_price=tp_price,
        sl_order_id=sl_order_id,
        tp_order_id=tp_order_id,
        updated_at=None,
        error=error,
        account_busy=account_busy,
        lock_position_id=lock_position_id,
        lock_state_path=lock_state_path,
    )


def launch_result(status=GuardianLaunchStatus.LAUNCHED):
    return GuardianLaunchResult(
        status=status,
        message="ok",
        account_id=123,
        position_id=7001,
        state_path="guardian.json",
        lock_path="guardian.json.lock",
        log_path="guardian.log",
        pid=4567,
    )


def bot_signal(direction=Direction.BUY):
    return TradeSignal(
        strategy=StrategyType.TREND_FOLLOW,
        direction=direction,
        entry_price=100.0,
        sl_price=90.0,
        tp_price=130.0,
        zone_id="bot-test",
        reason="test",
        order_type="market",
        meta={},
    )


def isolate_close_bookkeeping(item):
    item._latest_topstep_closing_fill = AsyncMock(return_value=None)
    item._exit_reason_from_topstep_fill = Mock(
        return_value=("manual", None, None, None)
    )
    item._record_daily_bot_outcome = Mock()
    item._persist_exit_record = Mock()
    item._persist_trade_record = Mock()
    item._refresh_account_snapshot = AsyncMock()
    item._sweep_contract_open_orders = AsyncMock()
    item._cancel_with_retry = AsyncMock()


def test_best_factor_plan_uses_entry_centered_atr_blend_geometry():
    item = make_engine(FakeClient())

    plan = item._manual_guardian_plan(item._open_position)

    assert plan is not None
    assert plan["side"] == "long"
    assert plan["size"] == 3
    assert plan["sl_price"] == 90.0
    assert plan["tp_price"] == 130.0
    assert plan["market_safe"] is True
    assert plan["source"] == "factor atr_blend:2.5/atr_blend:7.5"


def test_default_manual_position_policy_never_inspects_launches_or_closes(monkeypatch):
    client = FakeClient(positions=[POSITION])
    item = make_engine(client, guardian_enabled=False)
    inspected = []
    launched = []
    monkeypatch.setattr(
        engine_module,
        "inspect_manual_position_guardian",
        lambda *args, **kwargs: inspected.append(args),
    )
    monkeypatch.setattr(
        engine_module,
        "launch_manual_position_guardian",
        lambda *args, **kwargs: launched.append(args),
    )

    asyncio.run(item._ensure_manual_position_guardian())

    assert inspected == []
    assert launched == []
    assert client.open_order_reads == 0
    assert client.close_calls == []
    assert item._manual_guardian_status["status"] == "manual_position_observed"


def test_observe_only_startup_never_reads_legacy_guardian_state(monkeypatch):
    item = make_engine(FakeClient(positions=[POSITION]), guardian_enabled=False)
    reads = []
    monkeypatch.setattr(
        engine_module,
        "list_manual_position_guardians",
        lambda account_id: reads.append(account_id) or [],
    )

    item._resume_persisted_manual_guardian()

    assert reads == []
    assert item._manual_guardian_status["policy"] == "observe_only"


def test_missing_auto_oco_timeout_applies_only_to_bot_owned_position():
    item = make_engine(FakeClient(positions=[POSITION]), guardian_enabled=False)
    item._position_open_ts = time.time() - item.AUTO_OCO_FAILSAFE_SECONDS - 1
    item._sl_order_id = None
    item._tp_order_id = None

    item._active_signal = None
    assert item._auto_oco_missing_timed_out() is False

    item._active_signal = bot_signal()
    assert item._auto_oco_missing_timed_out() is True


def test_session_close_does_not_flatten_manual_position(monkeypatch):
    item = make_engine(FakeClient(positions=[POSITION]), guardian_enabled=False)
    item._active_signal = None
    _prepare_close_window_tick(item, monkeypatch)

    asyncio.run(item._tick())

    item.flatten_now.assert_not_awaited()
    assert item._open_position == POSITION


def test_session_close_still_flattens_bot_owned_position(monkeypatch):
    item = make_engine(FakeClient(positions=[POSITION]), guardian_enabled=False)
    item._active_signal = bot_signal()
    _prepare_close_window_tick(item, monkeypatch)

    asyncio.run(item._tick())

    item.flatten_now.assert_awaited_once()


def test_fresh_unprotected_position_launches_detached_guardian(monkeypatch):
    client = FakeClient()
    item = make_engine(client)
    captured = []
    monkeypatch.setattr(
        engine_module,
        "inspect_manual_position_guardian",
        lambda account_id, position_id: snapshot(),
    )
    monkeypatch.setattr(
        engine_module,
        "launch_manual_position_guardian",
        lambda spec, **kwargs: captured.append(spec) or launch_result(),
    )

    asyncio.run(item._ensure_manual_position_guardian())

    assert len(captured) == 1
    spec = captured[0]
    assert (spec.sl_price, spec.tp_price, spec.poll_seconds) == (90.0, 130.0, 2.5)
    assert spec.position_id == 7001
    assert item._active_signal is None
    assert client.open_order_reads == 1


def test_unknown_close_side_order_blocks_fresh_launch(monkeypatch):
    client = FakeClient(
        orders=[
            {
                "id": 99,
                "contractId": CONTRACT,
                "side": 1,
                "type": 4,
                "size": 3,
                "stopPrice": 89.0,
            }
        ]
    )
    item = make_engine(client)
    monkeypatch.setattr(
        engine_module,
        "inspect_manual_position_guardian",
        lambda account_id, position_id: snapshot(),
    )
    launched = []
    monkeypatch.setattr(
        engine_module,
        "launch_manual_position_guardian",
        lambda spec, **kwargs: launched.append(spec),
    )

    asyncio.run(item._ensure_manual_position_guardian())

    assert launched == []
    assert any("refusing to adopt or duplicate" in row for row in item._log)


def test_live_lock_skips_broker_scan_and_duplicate_launch(monkeypatch):
    client = FakeClient()
    item = make_engine(client)
    monkeypatch.setattr(
        engine_module,
        "inspect_manual_position_guardian",
        lambda account_id, position_id: snapshot(
            state_exists=True,
            lock_exists=True,
            running=True,
            pid=4567,
            lock_status="live",
            status="guarding",
            sl_price=90.0,
            tp_price=130.0,
            sl_order_id=10,
            tp_order_id=11,
        ),
    )
    launched = []
    monkeypatch.setattr(
        engine_module,
        "launch_manual_position_guardian",
        lambda spec, **kwargs: launched.append(spec),
    )

    asyncio.run(item._ensure_manual_position_guardian())

    assert launched == []
    assert client.open_order_reads == 0


def test_persisted_pair_resumes_frozen_prices_without_adopting_external_orders(monkeypatch):
    client = FakeClient()
    item = make_engine(client)
    captured = []
    monkeypatch.setattr(
        engine_module,
        "inspect_manual_position_guardian",
        lambda account_id, position_id: snapshot(
            state_exists=True,
            status="guarding",
            sl_price=91.0,
            tp_price=129.0,
            sl_order_id=10,
            tp_order_id=11,
        ),
    )
    monkeypatch.setattr(
        engine_module,
        "launch_manual_position_guardian",
        lambda spec, **kwargs: captured.append(spec) or launch_result(),
    )

    asyncio.run(item._ensure_manual_position_guardian())

    assert len(captured) == 1
    assert (captured[0].sl_price, captured[0].tp_price) == (91.0, 129.0)
    assert captured[0].adopt_sl_order_id is None
    assert captured[0].adopt_tp_order_id is None
    assert client.open_order_reads == 0


def test_terminal_state_never_rearms_same_position(monkeypatch):
    item = make_engine(FakeClient())
    monkeypatch.setattr(
        engine_module,
        "inspect_manual_position_guardian",
        lambda account_id, position_id: snapshot(
            state_exists=True,
            status="finished_position_gone",
            sl_price=90.0,
            tp_price=130.0,
        ),
    )
    launched = []
    monkeypatch.setattr(
        engine_module,
        "launch_manual_position_guardian",
        lambda spec, **kwargs: launched.append(spec),
    )

    asyncio.run(item._ensure_manual_position_guardian())

    assert launched == []
    assert any("terminal state=finished_position_gone" in row for row in item._log)


def test_manual_monitor_delegates_without_creating_bot_signal():
    item = make_engine(FakeClient())
    item._ensure_manual_position_guardian = AsyncMock()

    stopped = asyncio.run(item._monitor_auto_oco_protection())

    assert stopped is False
    item._ensure_manual_position_guardian.assert_awaited_once()
    assert item._active_signal is None


def test_engine_owned_short_double_fill_uses_buy_close_from_position_type():
    rogue = {
        **POSITION,
        "id": 7002,
        "type": 2,
        "creationTimestamp": "2026-07-17T01:00:00Z",
    }
    item = make_engine(FakeClient(positions=[rogue]))
    item._open_position = None
    item._position_just_closed = True
    item._emergency_market_close = AsyncMock()

    asyncio.run(item._sync_position())

    item._emergency_market_close.assert_awaited_once_with(1, "DOUBLE_FILL")
    assert any("rogue side=SHORT" in row for row in item._log)


def test_position_selection_uses_configured_contract_not_first_broker_row():
    other = {**POSITION, "id": 6001, "contractId": "CON.F.US.MES.U26"}
    target = {**POSITION, "id": 7001}
    item = make_engine(FakeClient())

    selected = item._position_for_configured_contract([other, target])

    assert selected == target


def test_live_lock_with_mismatched_state_identity_is_not_trusted(monkeypatch):
    client = FakeClient()
    item = make_engine(client)
    monkeypatch.setattr(
        engine_module,
        "inspect_manual_position_guardian",
        lambda account_id, position_id: snapshot(
            state_exists=True,
            lock_exists=True,
            running=True,
            pid=4567,
            lock_status="live",
            status="guarding",
            contract_id="CON.F.US.MES.U26",
            sl_price=90.0,
            tp_price=130.0,
            sl_order_id=10,
            tp_order_id=11,
        ),
    )
    launched = []
    monkeypatch.setattr(
        engine_module,
        "launch_manual_position_guardian",
        lambda spec, **kwargs: launched.append(spec),
    )

    asyncio.run(item._ensure_manual_position_guardian())

    assert launched == []
    assert client.open_order_reads == 0
    assert any("does not match the live position" in row for row in item._log)


def test_startup_resumes_flat_orphan_state_by_exact_persisted_prices(monkeypatch):
    item = make_engine(FakeClient())
    item._open_position = None
    orphan = snapshot(
        state_exists=True,
        status="closing_retry_position_gone",
        sl_price=91.0,
        tp_price=129.0,
        sl_order_id=10,
        tp_order_id=11,
    )
    captured = []
    monkeypatch.setattr(
        engine_module,
        "list_manual_position_guardians",
        lambda account_id: [orphan],
    )
    monkeypatch.setattr(
        engine_module,
        "launch_manual_position_guardian",
        lambda spec, **kwargs: captured.append(spec) or launch_result(),
    )

    item._resume_persisted_manual_guardian()

    assert len(captured) == 1
    assert captured[0].position_id == orphan.position_id
    assert (captured[0].sl_price, captured[0].tp_price) == (91.0, 129.0)
    assert str(captured[0].state_path) == orphan.state_path
    assert item._active_signal is None


def test_market_outside_fresh_strategy_envelope_closes_manual_position(monkeypatch):
    client = FakeClient(positions=[POSITION])
    item = make_engine(client)
    item._last_market_price = 150.0
    monkeypatch.setattr(
        engine_module,
        "inspect_manual_position_guardian",
        lambda account_id, position_id: snapshot(),
    )
    launched = []
    monkeypatch.setattr(
        engine_module,
        "launch_manual_position_guardian",
        lambda spec, **kwargs: launched.append(spec),
    )

    asyncio.run(item._ensure_manual_position_guardian())

    assert launched == []
    assert client.close_calls == [(123, CONTRACT)]
    assert client.positions == []
    assert item._active_signal is None


def test_other_position_account_guardian_times_out_to_contract_close(monkeypatch):
    client = FakeClient(positions=[POSITION])
    item = make_engine(client)
    item._manual_guardian_unprotected_since = (
        time.monotonic() - item.MANUAL_GUARDIAN_BUSY_TIMEOUT_SECONDS - 1
    )
    item._manual_guardian_last_position_id = POSITION["id"]
    monkeypatch.setattr(
        engine_module,
        "inspect_manual_position_guardian",
        lambda account_id, position_id: snapshot(
            lock_exists=True,
            lock_status="live",
            account_busy=True,
            lock_position_id=6999,
        ),
    )

    asyncio.run(item._ensure_manual_position_guardian())

    assert client.close_calls == [(123, CONTRACT)]
    assert client.positions == []


def test_flat_engine_periodically_restarts_orphan_cleanup(monkeypatch):
    item = make_engine(FakeClient())
    item._open_position = None
    item._manual_guardian_last_recovery_scan_ts = 0.0
    item._resume_persisted_manual_guardian = Mock()

    stopped = asyncio.run(item._monitor_auto_oco_protection())

    assert stopped is False
    item._resume_persisted_manual_guardian.assert_called_once()


def test_manual_position_does_not_claim_working_bot_pending_entry():
    pending = {
        "id": 88,
        "contractId": CONTRACT,
        "side": 0,
        "type": 1,
        "size": 3,
        "limitPrice": 99.0,
        "status": 1,
        "fillVolume": 0,
    }
    client = FakeClient(positions=[POSITION], orders=[pending])
    item = make_engine(client)
    item._open_position = None
    item._pending_order_id = 88
    item._pending_signal = None

    asyncio.run(item._sync_position())

    assert item._pending_order_id is None
    assert item._active_signal is None
    assert item._open_position == POSITION
    assert all(row.get("id") != 88 for row in client.orders)
    assert any("PENDING COLLISION" in row for row in item._log)


def test_engine_ignores_two_transient_position_omissions():
    client = FakeClient(positions=[POSITION])
    item = make_engine(client)
    calls = 0

    async def omitted_twice(account_id):
        nonlocal calls
        calls += 1
        if calls <= 2:
            return []
        return [dict(POSITION)]

    client.get_positions = omitted_twice

    asyncio.run(item._sync_position())

    assert calls == 3
    assert item._open_position == POSITION
    assert item._position_just_closed is False


def test_close_and_manual_reopen_between_polls_does_not_inherit_bot_signal():
    replacement = {
        **POSITION,
        "id": 7002,
        "creationTimestamp": "2026-07-17T01:10:00Z",
    }
    client = FakeClient(positions=[replacement])
    item = make_engine(client)
    item._active_signal = bot_signal()
    item._entry_time = None
    isolate_close_bookkeeping(item)
    item._ensure_manual_position_guardian = AsyncMock()

    asyncio.run(item._sync_position())

    assert item._active_signal is None
    assert item._open_position == replacement
    item._ensure_manual_position_guardian.assert_awaited_once()
    item._record_daily_bot_outcome.assert_called_once()
    assert item._record_daily_bot_outcome.call_args.kwargs["program_owned"] is True


def test_direct_bot_long_to_short_exact_double_fill_is_closed():
    reverse = {
        **POSITION,
        "id": 7002,
        "type": 2,
        "creationTimestamp": "2026-07-17T01:10:00Z",
    }
    exits = [
        {"id": 10, "status": 2, "fillVolume": 3},
        {"id": 11, "status": 2, "fillVolume": 3},
    ]
    client = FakeClient(positions=[reverse])
    client.order_history = exits
    item = make_engine(client)
    item._active_signal = bot_signal()
    item._sl_order_id = 10
    item._tp_order_id = 11
    isolate_close_bookkeeping(item)
    item._emergency_market_close = AsyncMock()

    asyncio.run(item._sync_position())

    item._emergency_market_close.assert_awaited_once_with(1, "DOUBLE_FILL")
