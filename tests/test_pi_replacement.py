from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from backend.backtest.engine import BacktestConfig, BacktestEngine
from backend.db.models import (
    Candle, Direction, OrderResponse, StrategyParams, StrategyType, Trade,
    TradeSignal,
)
from backend.live.engine import LiveTradingEngine
from backend.live.pi_listener import PiSignal
from backend.strategy.pi_signal import PiSignalStrategy


UTC = timezone.utc
CONTRACT = "CON.F.US.MNQ.Z26"


def _params(**overrides):
    values = {
        "strategy": "pi",
        "contract_id": CONTRACT,
        "contract_size": 2,
        "pi_long_only": False,
        "pi_long_kinds": ["青π", "深蓝圈"],
        "pi_short_kinds": ["粉π"],
        "pi_lv2_replace_pi": True,
        "pi_continue_long_kinds": ["深蓝圈"],
        "pi_continue_short_kinds": [],
        "pi_continue_short_levels": [],
        "factor_sl_value": 1.0,
        "rr_ratio": 2,
        "tr_daily_loss_stop": 0,
        "tr_daily_win_stop": 0,
        "tr_daily_profit_stop": 0,
        "tr_allowed_sessions": None,
    }
    values.update(overrides)
    return StrategyParams(**values)


def _candle(ts=None, price=100.0):
    return Candle(
        timestamp=ts or datetime(2026, 9, 21, 15, 0, tzinfo=UTC),
        open=price, high=price + 0.25, low=price - 0.25,
        close=price, volume=10,
    )


def _active(kind="青π", *, level=3, continuation=False):
    meta = {"pi": {"kind": kind, "level": level, "message_id": "original"}}
    if continuation:
        meta["pi_continuation"] = {"reentries_for_source": 1}
    return TradeSignal(
        strategy=StrategyType.TREND_FOLLOW,
        direction=Direction.BUY,
        entry_price=100.0,
        sl_price=90.0,
        tp_price=120.0,
        zone_id="original",
        reason="PI source",
        order_type="market",
        meta=meta,
    )


def _source(level=2, *, message_id="lv2"):
    return PiSignal(
        message_id=message_id,
        ts=datetime(2026, 9, 21, 15, 0, tzinfo=UTC),
        equity="QQQ", future="MNQ", direction=1,
        kind="深蓝圈", size="presentation-only", pos=None, level=level,
    )


def test_replacement_defaults_off_and_requires_exact_structured_level_two():
    disabled = PiSignalStrategy(_params(pi_lv2_replace_pi=False))
    assert not disabled.replacement_source_active(_active())

    strategy = PiSignalStrategy(_params())
    strategy._atr_blend = Mock(return_value=2.0)
    assert strategy.replacement_source_active(_active())
    assert not strategy.replacement_source_active(_active("深蓝圈"))
    assert not strategy.replacement_source_active(_active(level=2))
    assert not strategy.replacement_source_active(_active(level=None))
    assert not strategy.replacement_source_active(_active(continuation=True))

    assert strategy.push(_source(level=1, message_id="level-1"))
    assert strategy.take_lv2_replacement(_candle(), _active()) is None
    visual_only = _source(level=None, message_id="visual-only")
    visual_only.size = "Level 2"
    assert strategy.push(visual_only)
    assert strategy.take_lv2_replacement(_candle(), _active()) is None
    assert strategy.push(_source(level=2, message_id="level-2"))
    replacement = strategy.take_lv2_replacement(_candle(), _active())
    assert replacement is not None
    assert replacement.meta["pi"]["level"] == 2
    assert replacement.meta["pi"]["size"] == "presentation-only"
    assert replacement.meta["pi_replacement"]["replaces_kind"] == "青π"


def test_backtest_closes_original_before_opening_fresh_lv2_without_overlap():
    params = _params()
    engine = BacktestEngine(
        BacktestConfig(strategies=["trend"], symbol="MNQ"),
        params,
        record_equity=False,
    )
    engine.trend_follow._atr_blend = Mock(return_value=2.0)
    candle = _candle()
    old = Trade(
        trade_id="old", strategy=StrategyType.TREND_FOLLOW,
        direction=Direction.BUY, entry_price=99.0,
        entry_time=candle.timestamp, sl_price=90.0, tp_price=120.0,
        original_sl_price=90.0, original_tp_price=120.0,
        contracts=2, point_value=2.0,
        meta={"pi": {"kind": "青π", "level": 3, "message_id": "original"}},
    )
    engine._open_position = old
    assert engine.trend_follow.push(_source())

    engine._process_candle(candle)

    assert len(engine._trades) == 1
    assert engine._trades[0].trade_id == "old"
    assert engine._trades[0].meta["pi_replaced_by"]["kind"] == "深蓝圈"
    assert engine._open_position is not None
    assert engine._open_position.trade_id != "old"
    assert engine._open_position.meta["pi"]["kind"] == "深蓝圈"
    assert engine._open_position.meta["pi_replacement"]["source_level"] == 2
    assert engine._open_position.contracts == 2


def _live_engine(params=None):
    with patch(
        "backend.live.engine.EMAPMOSignalMessenger.from_env",
        return_value=MagicMock(),
    ):
        return LiveTradingEngine(
            MagicMock(), account_id=123, contract_id=CONTRACT,
            strategy_params=params or _params(),
        )


def test_live_replacement_uses_contract_close_and_requires_flat_confirmation():
    engine = _live_engine()
    engine._open_position = {"contractId": CONTRACT, "size": 2}
    engine._active_signal = _active()
    engine._sl_order_id = 11
    engine._tp_order_id = 12
    engine.client.cancel_order = AsyncMock(return_value=True)
    engine.client.close_position = AsyncMock(
        return_value=OrderResponse(order_id=50, success=True)
    )
    engine.client.flatten_all = AsyncMock()

    async def confirm_flat():
        engine._open_position = None

    engine._sync_position = AsyncMock(side_effect=confirm_flat)
    accepted, flat = asyncio.run(
        engine._close_bot_position_for_pi_replacement()
    )

    assert accepted and flat
    engine.client.close_position.assert_awaited_once_with(123, CONTRACT)
    engine.client.flatten_all.assert_not_awaited()


def test_live_rejected_replacement_close_reprotects_and_never_claims_flat():
    engine = _live_engine()
    engine._open_position = {"contractId": CONTRACT, "size": 2}
    engine._active_signal = _active()
    engine.client.close_position = AsyncMock(
        return_value=OrderResponse(order_id=0, success=False)
    )
    engine._sync_auto_oco_protection = AsyncMock(return_value=True)
    accepted, flat = asyncio.run(
        engine._close_bot_position_for_pi_replacement()
    )
    assert not accepted and not flat
    engine._sync_auto_oco_protection.assert_awaited_once()


def test_live_flat_replacement_ticket_uses_existing_market_entry_path(monkeypatch):
    import time
    from backend.live import engine as live_engine_module

    engine = _live_engine()
    replacement = _active("深蓝圈")
    replacement.meta["pi"]["level"] = 2
    replacement.meta["pi_replacement"] = {
        "replaces_kind": "青π", "source_level": 2,
    }
    replacement.entry_price = 100.0
    replacement.sl_price = 98.0
    replacement.tp_price = 104.0
    engine._pi_replacement_ticket = replacement
    engine._sync_position = AsyncMock()
    engine._last_account_refresh = time.time()
    engine._fetch_latest_candles = AsyncMock(return_value=[_candle()])
    engine._last_candle_time = None
    engine._last_status_log_minute = -1
    engine._get_phase = Mock(return_value="RTH")
    engine._get_order_short = Mock(return_value="FLAT")
    engine.detector.update = Mock()
    engine.confluence = None
    engine._append_history = Mock()
    engine._update_tf_breakout = Mock()
    engine._emapmo_messenger.enqueue_from_live = Mock(return_value=False)
    engine._place_market_entry = AsyncMock(return_value=True)
    monkeypatch.setattr(
        live_engine_module,
        "utc_now_naive",
        lambda: datetime(2026, 9, 21, 15, 1),
    )

    asyncio.run(engine._tick())

    engine._place_market_entry.assert_awaited_once_with(replacement)
    assert engine._pi_replacement_ticket is None


def test_pi_2mnq_preset_enables_only_requested_lv2_features():
    import json
    from pathlib import Path

    preset = json.loads(
        (Path(__file__).resolve().parents[1] / "data" / "presets.json")
        .read_text(encoding="utf-8")
    )["presets"]["PI 2MNQ BOTH BEST"]
    assert preset["contract_size"] == 2
    assert preset["pi_continue_long_kinds"] == ["深蓝圈"]
    assert preset["pi_continue_short_kinds"] == []
    assert preset["pi_continue_short_levels"] == []
    assert preset["pi_reopen_max_gap_r"] == 1.0
    assert preset["pi_lv2_replace_pi"] is True
