"""PI directional time-exit parity and protected live flattening."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from backend.backtest.engine import BacktestEngine
from backend.db.models import (
    Candle,
    Direction,
    ExitReason,
    StrategyParams,
    StrategyType,
    Trade,
    TradeSignal,
)
from backend.live import engine as live_engine_module
from backend.live.engine import LiveTradingEngine


CONTRACT = "CON.F.US.MNQ.U26"
UTC = timezone.utc


def _signal(direction: Direction) -> TradeSignal:
    return TradeSignal(
        strategy=StrategyType.TREND_FOLLOW,
        direction=direction,
        entry_price=100.0,
        sl_price=90.0 if direction == Direction.BUY else 110.0,
        tp_price=120.0 if direction == Direction.BUY else 80.0,
        zone_id="pi-time-exit",
        zone_source="pi",
        reason="directional time-exit test",
        order_type="market",
    )


def _params(*, long_hold: int = 0, short_hold: int = 60) -> StrategyParams:
    return StrategyParams(
        strategy="pi",
        contract_id=CONTRACT,
        contract_size=1,
        pi_long_only=False,
        pi_long_hold_min=long_hold,
        pi_short_hold_min=short_hold,
    )


@pytest.mark.parametrize(
    ("direction", "long_hold", "short_hold", "should_exit"),
    [
        (Direction.BUY, 0, 60, False),
        (Direction.BUY, 60, 60, True),
        (Direction.SELL, 0, 60, True),
        (Direction.SELL, 0, 0, False),
    ],
)
def test_backtest_pi_time_exit_uses_the_matching_direction(
    direction: Direction,
    long_hold: int,
    short_hold: int,
    should_exit: bool,
):
    engine = BacktestEngine(
        strategy_params=_params(long_hold=long_hold, short_hold=short_hold),
        record_equity=False,
    )
    entry_time = datetime(2026, 1, 15, 16, 0, tzinfo=UTC)
    trade = Trade(
        trade_id="pi-directional-hold",
        strategy=StrategyType.TREND_FOLLOW,
        direction=direction,
        entry_price=100.0,
        entry_time=entry_time,
        sl_price=50.0 if direction == Direction.BUY else 150.0,
        tp_price=150.0 if direction == Direction.BUY else 50.0,
        contracts=1,
        point_value=2.0,
        contract_id=CONTRACT,
    )
    engine._open_position = trade
    engine._check_exit = Mock()
    engine._check_trailing_sl = Mock()
    engine.trend_follow.observe = Mock()
    engine.trend_follow.notify_trade_closed = Mock()
    candle = Candle(
        timestamp=entry_time.replace(hour=17),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
        volume=10,
    )

    engine._process_candle(candle)

    if should_exit:
        assert engine._open_position is None
        assert engine._trades[-1].exit_reason == ExitReason.FLATTEN
    else:
        assert engine._open_position is trade


def _live_engine(direction: Direction, *, long_hold: int, short_hold: int):
    client = MagicMock()
    with patch(
        "backend.live.engine.EMAPMOSignalMessenger.from_env",
        return_value=MagicMock(),
    ):
        engine = LiveTradingEngine(
            client,
            account_id=123,
            contract_id=CONTRACT,
            contract_size=1,
            strategy_params=_params(long_hold=long_hold, short_hold=short_hold),
        )
    engine._open_position = {
        "id": 7001,
        "contractId": CONTRACT,
        "size": 1,
        "averagePrice": 100.0,
    }
    engine._active_signal = _signal(direction)
    engine._entry_time = datetime(2026, 1, 15, 16, 0)
    engine._last_market_price = 100.0
    return engine


def _prepare_live_tick(engine: LiveTradingEngine, monkeypatch) -> None:
    class FixedDateTime(datetime):
        @classmethod
        def utcnow(cls):
            return cls(2026, 1, 15, 17, 0)

    monkeypatch.setattr(live_engine_module, "datetime", FixedDateTime)
    engine._today = "2026-01-15"
    engine._get_topstep_trade_date = Mock(return_value="2026-01-15")
    engine._sync_position = AsyncMock()
    engine._last_account_refresh = time.time()
    engine._monitor_auto_oco_protection = AsyncMock(return_value=False)
    engine._fetch_latest_candles = AsyncMock(return_value=[
        Candle(
            timestamp=datetime(2026, 1, 15, 16, 59, tzinfo=UTC),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=10,
        )
    ])
    engine._last_candle_time = None
    engine._last_status_log_minute = 0
    engine._candles_processed = 0
    engine.detector = Mock()
    engine.confluence = None
    engine._append_history = Mock()
    engine._update_tf_breakout = Mock()
    engine.trend_follow.observe = Mock()
    engine._save_zones = Mock()
    engine._pending_order_id = None
    engine._check_trailing_sl_live = AsyncMock()
    engine.flatten_now = AsyncMock()


@pytest.mark.parametrize(
    ("direction", "long_hold", "short_hold", "should_flatten"),
    [
        (Direction.BUY, 0, 60, False),
        (Direction.BUY, 60, 60, True),
        (Direction.SELL, 0, 60, True),
        (Direction.SELL, 0, 0, False),
    ],
)
def test_live_pi_time_exit_uses_the_matching_direction_and_existing_flatten_path(
    monkeypatch,
    direction: Direction,
    long_hold: int,
    short_hold: int,
    should_flatten: bool,
):
    engine = _live_engine(
        direction,
        long_hold=long_hold,
        short_hold=short_hold,
    )
    _prepare_live_tick(engine, monkeypatch)

    asyncio.run(engine._tick())

    if should_flatten:
        engine.flatten_now.assert_awaited_once()
    else:
        engine.flatten_now.assert_not_awaited()


def test_live_pi_time_exit_never_flattens_an_unowned_manual_position(monkeypatch):
    engine = _live_engine(Direction.SELL, long_hold=60, short_hold=60)
    engine._active_signal = None
    _prepare_live_tick(engine, monkeypatch)

    asyncio.run(engine._tick())

    engine.flatten_now.assert_not_awaited()


def test_existing_flatten_path_cancels_known_brackets_then_sweeps_residual_orders():
    engine = _live_engine(Direction.BUY, long_hold=60, short_hold=60)
    engine._sl_order_id = 10
    engine._tp_order_id = 11
    engine._pending_order_id = 12
    engine._cancel_with_retry = AsyncMock()
    engine.client.flatten_all = AsyncMock(return_value=[{"orderId": 13}])
    engine.client.get_open_orders = AsyncMock(return_value=[
        {"id": 99, "contractId": CONTRACT},
        {"id": 100, "contractId": "CON.F.US.MES.U26"},
    ])
    engine._refresh_account_snapshot = AsyncMock(return_value=True)

    asyncio.run(engine.flatten_now())

    cancelled = {(call.args[0], call.args[1]) for call in engine._cancel_with_retry.await_args_list}
    assert cancelled == {
        (10, "SL (flatten)"),
        (11, "TP (flatten)"),
        (12, "ENTRY (flatten)"),
        (99, "SWEEP (flatten)"),
    }
    engine.client.flatten_all.assert_awaited_once_with(123)
    engine.client.get_open_orders.assert_awaited_once_with(123)

