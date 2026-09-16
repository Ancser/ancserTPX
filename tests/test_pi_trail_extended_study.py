"""Contract tests for the research-only PI extended trail study."""
from __future__ import annotations

from datetime import datetime, timezone

from backend.db.models import Candle, Direction, StrategyParams, StrategyType, Trade, TradeSignal
from backend.strategy.exit_policy import ExitAction, ExitPolicy, ExitTrailMode
from scripts.pi_trail_extended_study import (
    CURRENT_VARIANT,
    EXTENDED_VARIANTS,
    ContinuousTrailEngine,
    ExtendedPiSignalStrategy,
)


CONTRACT = "CON.F.US.MNQ.U26"
UTC = timezone.utc


def _params() -> StrategyParams:
    return StrategyParams(strategy="pi", contract_id=CONTRACT, contract_size=2)


def _signal() -> TradeSignal:
    return TradeSignal(
        strategy=StrategyType.TREND_FOLLOW,
        direction=Direction.BUY,
        entry_price=100.0,
        sl_price=90.0,
        tp_price=130.0,
        zone_id="trail-study",
        reason="research test",
        order_type="market",
    )


def test_extended_grid_is_unique_and_contains_both_research_modes():
    keys = [variant.key for variant in EXTENDED_VARIANTS]
    assert len(keys) == len(set(keys))
    assert CURRENT_VARIANT.mode == "none"
    assert {variant.mode for variant in EXTENDED_VARIANTS} == {"none", "continuous", "ladder"}


def test_continuous_policy_keeps_hard_tp_and_uses_activation_r():
    variant = next(item for item in EXTENDED_VARIANTS if item.key == "cont_tp_1r_gap05")
    policy = ExtendedPiSignalStrategy(_params(), variant)._policy_for_signal(_signal())
    assert policy.hard_tp_enabled is True
    assert policy.trail_mode == ExitTrailMode.SINGLE
    assert policy.trail_trigger_pct == 1.0


def test_continuous_trail_uses_high_watermark_and_moves_for_next_candle():
    variant = next(item for item in EXTENDED_VARIANTS if item.key == "cont_tp_1r_gap05")
    engine = ContinuousTrailEngine(
        strategy_params=_params(),
        record_equity=False,
        trail_variant=variant,
    )
    entry_time = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
    engine._open_position = Trade(
        trade_id="extended-trail",
        strategy=StrategyType.TREND_FOLLOW,
        direction=Direction.BUY,
        entry_price=100.0,
        entry_time=entry_time,
        sl_price=90.0,
        tp_price=130.0,
        original_sl_price=90.0,
        original_tp_price=130.0,
        contracts=2,
        point_value=2.0,
        contract_id=CONTRACT,
    )
    engine._active_exit_policy = ExitPolicy(
        model="pi",
        hard_tp_enabled=True,
        trail_mode=ExitTrailMode.SINGLE,
        trail_trigger_pct=1.0,
    )
    candle = Candle(
        timestamp=entry_time.replace(minute=1),
        open=101.0,
        high=115.0,
        low=104.0,
        close=110.0,
        volume=10,
    )

    decision = engine._evaluate_active_exit(candle)

    assert decision.action == ExitAction.MOVE_SL
    assert decision.stop_price == 110.0  # 1R arm, 0.5R gap from the 1.5R high
    assert decision.state.ladder_max_r == 1.5
