# ============================================================
# 文件: backend/strategy/exit_policy.py
# 狀態: unified model exit contract
# 用途: SINGLE source of truth for model exit-policy resolution and active-
#       position time/trail/ladder decisions. Backtest and live execute the
#       returned operation through different adapters, but never recalculate it.
# 關聯文件:
#   ← backend/backtest/confluence_backtest.py (per-candle exit management)
#   ← backend/live/engine.py                  (live SL-order modification)
#   ← backend/api/routes.py                    (conf_* request params)
# ============================================================
"""Unified exit-policy and pure active-position decision kernel.

Strategies still decide when and where to enter and emit a :class:`TradeSignal`.
This module gives that signal one normalized exit contract. The backtest adapter
simulates the returned operation against candles; the live adapter translates it
to broker flatten/modify-order calls. Broker ownership and fill reconciliation
remain live-only concerns.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Optional

from backend.db.models import (
    Direction,
    ExitReason,
    FACTOR_PIPELINE_STRATEGIES,
    StrategyParams,
    TradeSignal,
    strategy_param,
)


class ExitAction(str, Enum):
    """Operation requested by the shared exit kernel."""

    NONE = "none"
    CLOSE = "close"
    MOVE_SL = "move_sl"


class ExitTrailMode(str, Enum):
    NONE = "none"
    SINGLE = "single"
    LADDER = "ladder"


@dataclass(frozen=True)
class ExitPolicy:
    """Normalized exit behaviour attached to one ``TradeSignal``."""

    model: str
    max_hold_minutes: int = 0
    hard_tp_enabled: bool = True
    trail_mode: ExitTrailMode = ExitTrailMode.NONE
    trail_trigger_pct: float = 0.0
    trail_offset_ticks: int = 0
    trail_lock_pct: float = 0.0
    ladder_trigger_r: float = 2.0
    ladder_gap_r: float = 2.0


@dataclass(frozen=True)
class ExitState:
    """Small ratchet state carried by an engine while a position is open."""

    trail_triggered: bool = False
    ladder_max_r: float = 0.0
    ladder_lock_r: Optional[float] = None


@dataclass(frozen=True)
class ExitDecision:
    """Pure decision; execution adapters perform the requested side effect."""

    action: ExitAction
    state: ExitState
    reason: Optional[ExitReason] = None
    stop_price: Optional[float] = None
    favourable_ticks: float = 0.0
    lock_r: Optional[float] = None


def _trigger_pct(params: StrategyParams) -> float:
    value = strategy_param(params, "trail_trigger_pct", 0.30)
    try:
        pct = float(0.30 if value is None else value)
    except (TypeError, ValueError):
        pct = 0.30
    if pct > 1:
        pct /= 100.0
    return max(0.0, pct)


def _resolved_trail_ticks(params: StrategyParams, trigger_pct: float) -> int:
    try:
        tp_ticks = abs(int(strategy_param(params, "tp_ticks", 0) or 0))
        trail_ticks = int(strategy_param(params, "trail_sl_ticks", 5) or 0)
    except (TypeError, ValueError):
        return 0
    tick_step = 5
    floored_trigger = int(abs(tp_ticks * trigger_pct) // tick_step) * tick_step
    max_positive = max(0, floored_trigger - tick_step)
    return max(0, min(min(tp_ticks, max_positive), trail_ticks))


def resolve_exit_policy(
    params: StrategyParams,
    strategy_mode: str,
    direction: Direction,
) -> ExitPolicy:
    """Resolve every model's exit settings into one immutable contract."""

    model = str(strategy_mode or getattr(params, "strategy", "factor") or "factor").lower()

    if model in ("pi", "delta_absorption"):
        hold_field = "pi_long_hold_min" if direction == Direction.BUY else "pi_short_hold_min"
        max_hold = max(0, int(getattr(params, hold_field, 0) or 0))
    elif model == "optionwall":
        max_hold = max(1, int(getattr(params, "option_wall_max_hold_min", 60) or 60))
    elif model in FACTOR_PIPELINE_STRATEGIES:
        timeframe = (
            int(getattr(params, "factor_timeframe_minutes", 5) or 5)
            if model == "factor"
            else int(getattr(params, "research_tf_minutes", 5) or 5)
        )
        max_hold = max(0, int(getattr(params, "factor_max_hold_bars", 0) or 0)) * max(1, timeframe)
    else:
        max_hold = 0

    trigger_pct = _trigger_pct(params)
    trail_mode = ExitTrailMode.NONE
    trail_offset_ticks = 0
    trail_lock_pct = 0.0

    if model == "confluence":
        trigger_pct = max(0.0, float(getattr(params, "conf_trail_trigger_pct", 0.0) or 0.0))
        trail_lock_pct = float(getattr(params, "conf_trail_lock_pct", 0.0) or 0.0)
        if trigger_pct > 0:
            trail_mode = ExitTrailMode.SINGLE
    elif model != "optionwall":
        requested_mode = str(getattr(params, "tr_exit_mode", "tp") or "tp").lower()
        if requested_mode == "ladder" and model in ("trend", "factor"):
            trail_mode = ExitTrailMode.LADDER
        elif bool(strategy_param(params, "trail_enabled", True)) and trigger_pct > 0:
            trail_mode = ExitTrailMode.SINGLE
            trail_offset_ticks = _resolved_trail_ticks(params, trigger_pct)

    return ExitPolicy(
        model=model,
        max_hold_minutes=max_hold,
        hard_tp_enabled=(model != "optionwall" and trail_mode != ExitTrailMode.LADDER),
        trail_mode=trail_mode,
        trail_trigger_pct=trigger_pct,
        trail_offset_ticks=trail_offset_ticks,
        trail_lock_pct=trail_lock_pct,
    )


def ensure_exit_policy(
    signal: TradeSignal,
    params: StrategyParams,
    strategy_mode: str,
) -> ExitPolicy:
    """Attach the canonical policy unless the model supplied one explicitly."""

    if signal.exit_policy is None:
        signal.exit_policy = resolve_exit_policy(params, strategy_mode, signal.direction)
    return signal.exit_policy


def _round_to_tick(price: float, tick_size: float) -> float:
    if tick_size <= 0:
        return price
    return round(round(price / tick_size) * tick_size, 10)


def evaluate_exit_operation(
    *,
    policy: ExitPolicy,
    state: ExitState,
    direction: Direction,
    entry_price: float,
    current_sl: float,
    original_sl: float,
    tp_price: float,
    market_price: float,
    held_minutes: float,
    tick_size: float,
) -> ExitDecision:
    """Return CLOSE/MOVE_SL/NONE without touching an engine or broker."""

    if policy.max_hold_minutes > 0 and held_minutes >= policy.max_hold_minutes:
        return ExitDecision(
            action=ExitAction.CLOSE,
            reason=ExitReason.FLATTEN,
            state=state,
        )

    if policy.trail_mode == ExitTrailMode.NONE:
        return ExitDecision(action=ExitAction.NONE, state=state)

    favourable = (
        market_price - entry_price
        if direction == Direction.BUY
        else entry_price - market_price
    )
    favourable_ticks = favourable / tick_size if tick_size > 0 else 0.0

    if policy.trail_mode == ExitTrailMode.LADDER:
        risk = abs(entry_price - original_sl)
        if risk <= 0:
            return ExitDecision(action=ExitAction.NONE, state=state)
        peak_r = max(state.ladder_max_r, favourable / risk)
        observed = ExitState(
            trail_triggered=state.trail_triggered,
            ladder_max_r=peak_r,
            ladder_lock_r=state.ladder_lock_r,
        )
        if peak_r < policy.ladder_trigger_r:
            return ExitDecision(action=ExitAction.NONE, state=observed)

        lock_r = math.floor(peak_r) - policy.ladder_gap_r
        if state.ladder_lock_r is not None and lock_r <= state.ladder_lock_r:
            return ExitDecision(action=ExitAction.NONE, state=observed)
        raw_sl = (
            entry_price + lock_r * risk
            if direction == Direction.BUY
            else entry_price - lock_r * risk
        )
        new_sl = _round_to_tick(raw_sl, tick_size)
        improves = new_sl > current_sl if direction == Direction.BUY else new_sl < current_sl
        next_state = ExitState(
            trail_triggered=state.trail_triggered or improves,
            ladder_max_r=peak_r,
            ladder_lock_r=lock_r,
        )
        if not improves:
            return ExitDecision(action=ExitAction.NONE, state=next_state, lock_r=lock_r)
        return ExitDecision(
            action=ExitAction.MOVE_SL,
            reason=ExitReason.TRAIL_SL,
            stop_price=new_sl,
            state=next_state,
            favourable_ticks=favourable_ticks,
            lock_r=lock_r,
        )

    if state.trail_triggered or policy.trail_trigger_pct <= 0:
        return ExitDecision(action=ExitAction.NONE, state=state)
    tp_distance = abs(tp_price - entry_price)
    if tp_distance <= 0 or favourable < policy.trail_trigger_pct * tp_distance:
        return ExitDecision(action=ExitAction.NONE, state=state)

    lock_distance = (
        policy.trail_lock_pct * tp_distance
        if policy.trail_lock_pct
        else policy.trail_offset_ticks * tick_size
    )
    new_sl = _round_to_tick(
        entry_price + lock_distance
        if direction == Direction.BUY
        else entry_price - lock_distance,
        tick_size,
    )
    return ExitDecision(
        action=ExitAction.MOVE_SL,
        reason=ExitReason.TRAIL_SL,
        stop_price=new_sl,
        state=ExitState(
            trail_triggered=True,
            ladder_max_r=state.ladder_max_r,
            ladder_lock_r=state.ladder_lock_r,
        ),
        favourable_ticks=favourable_ticks,
    )


def rejected_exit_state(previous: ExitState, proposed: ExitState) -> ExitState:
    """Keep observed ladder progress while retrying a failed live SL update."""

    return ExitState(
        trail_triggered=previous.trail_triggered,
        ladder_max_r=max(previous.ladder_max_r, proposed.ladder_max_r),
        ladder_lock_r=previous.ladder_lock_r,
    )


@dataclass
class ConfluenceExitStyle:
    """Optional exit-policy knobs. Defaults = original behaviour (all OFF except
    session_limit, which is confluence's live-style one-shot-per-zone+direction
    rule within a Topstep session)."""
    trail_trigger_pct: float = 0.0   # 0 = trailing OFF
    trail_lock_pct: float = 0.0      # locked SL as fraction of TP distance on trigger
    full_tp_lock: int = 0            # 0 = OFF; stop new entries after N full-TP exits/session
    session_limit: bool = True       # one trade per zone+direction per Topstep session

    @property
    def trail_enabled(self) -> bool:
        return self.trail_trigger_pct > 0.0


def maybe_trail_sl(direction: Direction, entry: float, tp: float, current_sl: float,
                   triggered: bool, market_price: float,
                   style: ConfluenceExitStyle) -> "tuple[float, bool]":
    """One-time break-even/trail. Returns (new_sl, triggered).

    Idempotent once triggered. When price has advanced ``trail_trigger_pct`` of
    the entry→TP distance, move SL to entry ± ``trail_lock_pct`` × TP distance and
    latch ``triggered=True``. Identical in backtest and live → parity holds."""
    policy = ExitPolicy(
        model="confluence",
        trail_mode=(ExitTrailMode.SINGLE if style.trail_enabled else ExitTrailMode.NONE),
        trail_trigger_pct=style.trail_trigger_pct,
        trail_lock_pct=style.trail_lock_pct,
    )
    decision = evaluate_exit_operation(
        policy=policy,
        state=ExitState(trail_triggered=triggered),
        direction=direction,
        entry_price=entry,
        current_sl=current_sl,
        original_sl=current_sl,
        tp_price=tp,
        market_price=market_price,
        held_minutes=0.0,
        tick_size=0.0,
    )
    return (
        decision.stop_price if decision.action == ExitAction.MOVE_SL else current_sl,
        decision.state.trail_triggered,
    )
