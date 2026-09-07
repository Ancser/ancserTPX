"""One exit-policy contract drives both backtest and live execution adapters."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from backend.backtest.engine import BacktestEngine
from backend.db.models import (
    Candle,
    Direction,
    ExitReason,
    OrderResponse,
    StrategyParams,
    StrategyType,
    Trade,
    TradeSignal,
    current_quarterly_contract_id,
)
from backend.live.engine import LiveTradingEngine
from backend.strategy.exit_policy import (
    ExitAction,
    ExitPolicy,
    ExitState,
    ExitTrailMode,
    ensure_exit_policy,
    evaluate_exit_operation,
    resolve_exit_policy,
)


TEST_CONTRACT = current_quarterly_contract_id("MNQ")


def _signal(direction: Direction = Direction.BUY) -> TradeSignal:
    return TradeSignal(
        strategy=StrategyType.TREND_FOLLOW,
        direction=direction,
        entry_price=100.0,
        sl_price=90.0 if direction == Direction.BUY else 110.0,
        tp_price=120.0 if direction == Direction.BUY else 80.0,
        zone_id="exit-policy-test",
        reason="shared exit policy",
        order_type="market",
    )


def _live_engine(params: StrategyParams) -> LiveTradingEngine:
    client = MagicMock()
    with patch(
        "backend.live.engine.EMAPMOSignalMessenger.from_env",
        return_value=MagicMock(),
    ):
        return LiveTradingEngine(
            client,
            account_id=123,
            contract_id=TEST_CONTRACT,
            contract_size=1,
            strategy_params=params,
        )


def test_signal_carries_the_resolved_exit_policy():
    signal = _signal(Direction.SELL)
    params = StrategyParams(
        strategy="pi",
        pi_long_hold_min=0,
        pi_short_hold_min=60,
    )

    policy = ensure_exit_policy(signal, params, "pi")

    assert signal.exit_policy is policy
    assert policy.model == "pi"
    assert policy.max_hold_minutes == 60


def test_model_exit_modes_are_normalized_by_one_resolver():
    factor = resolve_exit_policy(
        StrategyParams(strategy="factor", factor_max_hold_bars=0),
        "factor",
        Direction.BUY,
    )
    assert factor.max_hold_minutes == 0
    assert factor.trail_mode == ExitTrailMode.SINGLE
    assert factor.hard_tp_enabled is True

    ladder = resolve_exit_policy(
        StrategyParams(strategy="factor", tr_exit_mode="ladder"),
        "factor",
        Direction.BUY,
    )
    assert ladder.trail_mode == ExitTrailMode.LADDER
    assert ladder.hard_tp_enabled is False

    option_wall = resolve_exit_policy(
        StrategyParams(strategy="optionwall", option_wall_max_hold_min=60),
        "optionwall",
        Direction.BUY,
    )
    assert option_wall.max_hold_minutes == 60
    assert option_wall.trail_mode == ExitTrailMode.NONE
    assert option_wall.hard_tp_enabled is False


def test_pi_direction_selects_its_own_time_exit_without_engine_branching():
    params = StrategyParams(
        strategy="pi",
        pi_long_hold_min=0,
        pi_short_hold_min=60,
    )

    assert resolve_exit_policy(params, "pi", Direction.BUY).max_hold_minutes == 0
    assert resolve_exit_policy(params, "pi", Direction.SELL).max_hold_minutes == 60


def test_time_exit_returns_a_common_close_decision():
    policy = resolve_exit_policy(
        StrategyParams(strategy="optionwall", option_wall_max_hold_min=60),
        "optionwall",
        Direction.BUY,
    )

    decision = evaluate_exit_operation(
        policy=policy,
        state=ExitState(),
        direction=Direction.BUY,
        entry_price=100.0,
        current_sl=90.0,
        original_sl=90.0,
        tp_price=1000.0,
        market_price=101.0,
        held_minutes=60.0,
        tick_size=0.25,
    )

    assert decision.action == ExitAction.CLOSE
    assert decision.reason == ExitReason.FLATTEN
    assert decision.stop_price is None


def test_single_trail_returns_a_common_move_sl_decision():
    policy = resolve_exit_policy(
        StrategyParams(
            strategy="factor",
            tr_trail_enabled=True,
            tr_trail_trigger_pct=0.5,
            tr_trail_sl_ticks=10,
            tr_tp_ticks=200,
        ),
        "factor",
        Direction.BUY,
    )

    decision = evaluate_exit_operation(
        policy=policy,
        state=ExitState(),
        direction=Direction.BUY,
        entry_price=100.0,
        current_sl=90.0,
        original_sl=90.0,
        tp_price=120.0,
        market_price=110.0,
        held_minutes=1.0,
        tick_size=0.25,
    )

    assert decision.action == ExitAction.MOVE_SL
    assert decision.reason == ExitReason.TRAIL_SL
    assert decision.stop_price == 102.5
    assert decision.state.trail_triggered is True


def test_ladder_ratchet_is_calculated_by_the_same_exit_operation():
    policy = resolve_exit_policy(
        StrategyParams(strategy="factor", tr_exit_mode="ladder"),
        "factor",
        Direction.BUY,
    )

    first = evaluate_exit_operation(
        policy=policy,
        state=ExitState(),
        direction=Direction.BUY,
        entry_price=100.0,
        current_sl=90.0,
        original_sl=90.0,
        tp_price=1_000_100.0,
        market_price=120.0,
        held_minutes=1.0,
        tick_size=0.25,
    )
    assert first.action == ExitAction.MOVE_SL
    assert first.stop_price == 100.0
    assert first.state.ladder_max_r == 2.0
    assert first.state.ladder_lock_r == 0.0

    second = evaluate_exit_operation(
        policy=policy,
        state=first.state,
        direction=Direction.BUY,
        entry_price=100.0,
        current_sl=100.0,
        original_sl=90.0,
        tp_price=1_000_100.0,
        market_price=130.0,
        held_minutes=2.0,
        tick_size=0.25,
    )
    assert second.action == ExitAction.MOVE_SL
    assert second.stop_price == 110.0
    assert second.state.ladder_lock_r == 1.0


def test_backtest_and_live_adapters_apply_the_same_single_trail_stop():
    params = StrategyParams(
        strategy="factor",
        contract_id=TEST_CONTRACT,
        contract_size=1,
        tr_trail_enabled=True,
        tr_trail_trigger_pct=0.5,
        tr_trail_sl_ticks=10,
        tr_tp_ticks=200,
    )
    entry_time = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)
    backtest = BacktestEngine(strategy_params=params, record_equity=False)
    backtest._open_position = Trade(
        trade_id="shared-exit",
        strategy=StrategyType.TREND_FOLLOW,
        direction=Direction.BUY,
        entry_price=100.0,
        entry_time=entry_time,
        sl_price=90.0,
        tp_price=120.0,
        original_sl_price=90.0,
    )
    backtest._check_trailing_sl(Candle(
        timestamp=entry_time,
        open=109.0,
        high=111.0,
        low=108.0,
        close=110.0,
        volume=1,
    ))

    live = _live_engine(params)
    live_signal = _signal(Direction.BUY)
    live._active_signal = live_signal
    live._fill_price = 100.0
    live._last_market_price = 110.0
    live._sl_order_id = 77
    live._protection_synced = True
    live.client.modify_order = AsyncMock(return_value=OrderResponse(77, True))
    asyncio.run(live._check_trailing_sl_live())

    assert backtest._open_position.sl_price == 102.5
    assert live_signal.sl_price == 102.5
    assert backtest._active_exit_policy == live_signal.exit_policy
    live.client.modify_order.assert_awaited_once_with(
        123,
        77,
        size=1,
        stop_price=102.5,
    )


def test_live_market_no_hard_tp_policy_uses_a_far_tp_bracket():
    params = StrategyParams(
        strategy="factor",
        contract_id=TEST_CONTRACT,
        contract_size=1,
        tr_exit_mode="ladder",
    )
    live = _live_engine(params)
    live._last_market_price = 100.0
    live.client.place_order = AsyncMock(return_value=OrderResponse(88, True))
    signal = _signal(Direction.BUY)

    assert asyncio.run(live._place_market_entry(signal)) is True

    order = live.client.place_order.await_args.args[0]
    assert signal.exit_policy.trail_mode == ExitTrailMode.LADDER
    assert signal.exit_policy.hard_tp_enabled is False
    assert order.take_profit_bracket == {
        "ticks": live.NO_HARD_TP_BRACKET_TICKS,
        "type": 1,
    }

    # The adapter follows the normalized policy field, not a ladder/model name.
    generic = _live_engine(StrategyParams(
        strategy="factor",
        contract_id=TEST_CONTRACT,
        contract_size=1,
    ))
    generic._last_market_price = 100.0
    generic.client.place_order = AsyncMock(return_value=OrderResponse(89, True))
    generic_signal = _signal(Direction.BUY)
    generic_signal.exit_policy = ExitPolicy(model="custom", hard_tp_enabled=False)

    assert asyncio.run(generic._place_market_entry(generic_signal)) is True
    generic_order = generic.client.place_order.await_args.args[0]
    assert generic_order.take_profit_bracket == {
        "ticks": generic.NO_HARD_TP_BRACKET_TICKS,
        "type": 1,
    }
