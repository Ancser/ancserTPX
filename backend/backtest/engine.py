# ============================================================
# 文件: backend/backtest/engine.py
# 狀態: v3.0 — Session-based zone + SessionTrendFollow only
# 變更:
#   - Removed ConsolidationDetector, ReversionStrategy, TrendFollowStrategy
#   - Removed run_with_replay()
#   - Uses SessionZoneDetector + SessionTrendFollow only
#   - Flatten time: UTC 19:45 (PT 12:45)
# ============================================================
"""
回測引擎 v3 — SessionTrendFollow Only

將歷史 1m K 線逐根餵入 → Session Zone 偵測 → SessionTrendFollow 評估 → 模擬成交 → 績效計算
"""

from __future__ import annotations
import uuid
import logging
from datetime import datetime, time, timedelta
from typing import Dict, List, Optional, Tuple

from backend.db.models import (
    Candle, Trade, TradeSignal, BacktestConfig, BacktestResult,
    Metrics, ConsolidationZone, BreakoutAnalysis, StrategyParams,
    Direction, ExitReason, StrategyType, ZoneStatus,
)
from backend.strategy.consolidation import SessionZoneDetector
from backend.strategy.trend_follow import SessionTrendFollow

logger = logging.getLogger(__name__)


class BacktestEngine:
    """回測引擎 v3 — Session Zone + SessionTrendFollow"""

    POINT_VALUE = 20.0
    TICK_SIZE = 0.25
    # PT 12:45 = UTC 19:45
    FLATTEN_TIME_UTC = time(19, 45)
    PRE_FLATTEN_UTC = time(19, 30)

    def __init__(self, config: Optional[BacktestConfig] = None,
                 strategy_params: Optional[StrategyParams] = None):
        self.config = config or BacktestConfig()
        self.strategy_params = strategy_params or StrategyParams()

        # Session-based zone detector
        self.detector = SessionZoneDetector(
            value_area_pct=self.config.value_area_pct,
        )
        # SessionTrendFollow with configurable params
        self.trend_follow = SessionTrendFollow(params=self.strategy_params)

        # State
        self._capital = self.config.initial_capital
        self._open_position: Optional[Trade] = None
        self._pending_order: Optional[TradeSignal] = None
        self._pending_age: int = 0
        self._pending_max_age: int = self.trend_follow.PENDING_TIMEOUT_CANDLES
        self._trades: List[Trade] = []
        self._equity_curve: List[Tuple[datetime, float]] = []
        self._daily_pnl: Dict[str, float] = {}
        self._last_closed_trade: Optional[Trade] = None
        # TP timeout state
        self._position_age: int = 0
        self._tp_timeout_triggered: bool = False

    def run(self, candles: List[Candle]) -> BacktestResult:
        """執行回測 (1m candles)"""
        logger.info(
            f"開始回測: {len(candles)} 根 K 線, "
            f"初始資金=${self._capital:,.0f}"
        )
        self._reset()

        # Ensure chronological order (API may return newest-first)
        candles = sorted(candles, key=lambda c: c.timestamp)

        for candle in candles:
            self._process_candle(candle)

        if self._open_position:
            self._force_exit(candles[-1], ExitReason.FLATTEN)

        # Close any remaining active zone at end of backtest
        if candles:
            self.detector.close_final_zone(candles[-1])

        from backend.backtest.metrics import MetricsCalculator
        calc = MetricsCalculator()
        metrics = calc.calculate_all(self._trades, self.config.initial_capital)

        all_zones = self.detector.get_all_zones()

        result = BacktestResult(
            config=self.config,
            trades=self._trades,
            zones=all_zones,
            metrics=metrics,
            equity_curve=self._equity_curve,
        )

        logger.info(
            f"回測完成: {metrics.total_trades} 筆交易, "
            f"勝率={metrics.win_rate:.1%}, PnL=${metrics.total_pnl:,.0f}"
        )
        return result

    def _reset(self):
        self._capital = self.config.initial_capital
        self._open_position = None
        self._pending_order = None
        self._pending_age = 0
        self._trades = []
        self._equity_curve = []
        self._daily_pnl = {}
        self._last_closed_trade = None
        self._position_age = 0
        self._tp_timeout_triggered = False
        self.detector.reset()
        self.trend_follow.reset()

    def _process_candle(self, candle: Candle):
        self._equity_curve.append((candle.timestamp, self._capital))

        # ── ALWAYS update zone detector first (even during flatten/limits) ──
        self.detector.update(candle)

        # Daily loss limit
        date_str = candle.timestamp.strftime("%Y-%m-%d")
        daily = self._daily_pnl.get(date_str, 0)
        if daily <= -self.config.max_daily_loss:
            if self._open_position:
                self._force_exit(candle, ExitReason.FLATTEN)
            if self._pending_order:
                self._cancel_pending_order()
            return

        # Flatten time (UTC 19:45 = PT 12:45)
        # Only flatten between 19:45-21:59 UTC (not after 22:00 = new session)
        candle_time = candle.timestamp.time()
        SESSION_START = time(22, 0)  # 22:00 UTC = new session

        in_flatten_window = (
            candle_time >= self.FLATTEN_TIME_UTC and candle_time < SESSION_START
        )
        if in_flatten_window:
            if self._open_position:
                self._force_exit(candle, ExitReason.FLATTEN)
            if self._pending_order:
                self._cancel_pending_order()
            return  # no new trades during flatten, but detector already updated

        # Pre-flatten: cancel pending (UTC 19:30 = PT 12:30)
        in_pre_flatten = (
            candle_time >= self.PRE_FLATTEN_UTC and candle_time < SESSION_START
        )
        if in_pre_flatten and self._pending_order:
            logger.debug("收盤前取消掛單")
            self._cancel_pending_order()

        # Check SL/TP on open position
        if self._open_position:
            self._check_exit(candle)
            if self._open_position:
                # ── TP Timeout check ──
                self._position_age += 1
                tp_timeout = self.trend_follow.TP_TIMEOUT_CANDLES
                if tp_timeout > 0 and not self._tp_timeout_triggered and self._position_age >= tp_timeout:
                    self._tp_timeout_triggered = True
                    action = self.trend_follow.TP_TIMEOUT_ACTION
                    if action == "flat":
                        logger.debug(f"TP timeout ({tp_timeout} min) → flat close")
                        self._force_exit(candle, ExitReason.FLATTEN)
                    else:
                        new_factor = int(action)
                        self._recalc_tp(new_factor)
                        logger.debug(f"TP timeout ({tp_timeout} min) → TP factor → {new_factor}x")
                if self._open_position:
                    return  # still open, don't open new

        # ── Check if pending limit order fills on this candle ──
        if self._pending_order and not self._open_position:
            filled = self._check_pending_fill(candle)
            if filled:
                return
            # Check expiry
            self._pending_age += 1
            if self._pending_age > self._pending_max_age:
                logger.debug(f"掛單超時取消: {self._pending_order.reason}")
                self._cancel_pending_order()

        # ── Strategy evaluation ──
        if not self._open_position and not self._pending_order:
            active_zone = self.detector.get_active_zone()
            is_mature = self.detector.is_zone_mature

            # If current zone not mature, use last completed zone
            eval_zone = active_zone
            eval_mature = is_mature
            if not is_mature:
                prev = self.detector.get_last_left_zone()
                if prev:
                    eval_zone = prev
                    eval_mature = True

            signal = self.trend_follow.evaluate(candle, eval_zone, eval_mature)
            if signal:
                self._place_pending_order(signal, candle)
                return

    def _check_exit(self, candle: Candle):
        pos = self._open_position
        if not pos:
            return

        if pos.direction == Direction.BUY:
            if candle.low <= pos.sl_price:
                self._execute_exit(candle, pos.sl_price, ExitReason.SL)
            elif candle.high >= pos.tp_price:
                self._execute_exit(candle, pos.tp_price, ExitReason.TP)
        else:
            if candle.high >= pos.sl_price:
                self._execute_exit(candle, pos.sl_price, ExitReason.SL)
            elif candle.low <= pos.tp_price:
                self._execute_exit(candle, pos.tp_price, ExitReason.TP)

    def _cancel_pending_order(self):
        """Cancel a pending limit order and notify the strategy."""
        if self._pending_order:
            self.trend_follow.notify_order_cancelled()
        self._pending_order = None
        self._pending_age = 0

    def _place_pending_order(self, signal: TradeSignal, candle: Candle):
        """Place a limit order — will fill on a future candle when price touches."""
        self._pending_order = signal
        self._pending_age = 0
        logger.debug(
            f"掛單: {signal.strategy.value} {signal.direction.value} "
            f"limit @ {signal.entry_price:.2f} | SL={signal.sl_price:.2f} TP={signal.tp_price:.2f}"
        )

    def _check_pending_fill(self, candle: Candle) -> bool:
        """Check if the pending limit order fills on this candle."""
        order = self._pending_order
        if not order:
            return False

        filled = False
        if order.direction == Direction.BUY:
            if candle.low <= order.entry_price:
                filled = True
        else:
            if candle.high >= order.entry_price:
                filled = True

        if filled:
            self._execute_entry(order, candle)
            self._pending_order = None
            self._pending_age = 0
            return True

        return False

    def _execute_entry(self, signal: TradeSignal, candle: Candle):
        slippage = self.config.slippage_ticks * self.TICK_SIZE
        if signal.direction == Direction.BUY:
            fill_price = signal.entry_price + slippage
        else:
            fill_price = signal.entry_price - slippage

        trade = Trade(
            trade_id=f"T{uuid.uuid4().hex[:8]}",
            strategy=signal.strategy,
            direction=signal.direction,
            entry_price=fill_price,
            entry_time=candle.timestamp,
            sl_price=signal.sl_price,
            tp_price=signal.tp_price,
            zone_id=signal.zone_id,
            contracts=1,
            vol_ratio=signal.vol_ratio,
            is_big_trend=signal.is_big_trend,
            breakout_range=signal.breakout_range,
        )
        self._open_position = trade
        self._position_age = 0
        self._tp_timeout_triggered = False
        logger.debug(
            f"入場: {trade.strategy.value} {trade.direction.value} "
            f"@ {fill_price:.2f} | SL={trade.sl_price:.2f} TP={trade.tp_price:.2f}"
        )

    def _recalc_tp(self, new_factor: int):
        """Recalculate TP price with a new tp_factor (for TP timeout).
        Uses SL distance as base: TP = entry ± |entry - SL| × new_factor
        """
        pos = self._open_position
        if not pos:
            return
        sl_distance = abs(pos.entry_price - pos.sl_price)
        if pos.direction == Direction.BUY:
            pos.tp_price = pos.entry_price + sl_distance * new_factor
        else:
            pos.tp_price = pos.entry_price - sl_distance * new_factor
        logger.debug(f"TP recalculated: new TP={pos.tp_price:.2f} ({new_factor}x SL)")

    def _execute_exit(self, candle: Candle, exit_price: float, reason: ExitReason):
        pos = self._open_position
        if not pos:
            return

        slippage = self.config.slippage_ticks * self.TICK_SIZE
        if reason == ExitReason.SL:
            if pos.direction == Direction.BUY:
                exit_price -= slippage
            else:
                exit_price += slippage
        else:
            if pos.direction == Direction.BUY:
                exit_price -= slippage
            else:
                exit_price += slippage

        if pos.direction == Direction.BUY:
            pnl = (exit_price - pos.entry_price) * self.POINT_VALUE * pos.contracts
        else:
            pnl = (pos.entry_price - exit_price) * self.POINT_VALUE * pos.contracts

        # Deduct round-turn commission (Mini NQ: $1.00 per contract)
        pnl -= self.config.commission_rt * pos.contracts

        pos.exit_price = exit_price
        pos.exit_time = candle.timestamp
        pos.pnl = pnl
        pos.exit_reason = reason

        self._capital += pnl
        date_str = candle.timestamp.strftime("%Y-%m-%d")
        self._daily_pnl[date_str] = self._daily_pnl.get(date_str, 0) + pnl

        self._trades.append(pos)
        self._last_closed_trade = pos
        self._open_position = None

        # Notify strategy of trade close
        self.trend_follow.notify_trade_closed(reason.value)

        logger.debug(
            f"出場: {reason.value} @ {exit_price:.2f} | "
            f"PnL=${pnl:+.0f} | 資金=${self._capital:,.0f}"
        )

    def _force_exit(self, candle: Candle, reason: ExitReason):
        self._execute_exit(candle, candle.close, reason)

    @staticmethod
    def aggregate_1m_to_5m(candles_1m: List[Candle]) -> List[Candle]:
        if not candles_1m:
            return []
        buckets: Dict[datetime, List[Candle]] = {}
        for c in candles_1m:
            minute = c.timestamp.minute
            aligned_minute = (minute // 5) * 5
            bucket_time = c.timestamp.replace(
                minute=aligned_minute, second=0, microsecond=0
            )
            if bucket_time not in buckets:
                buckets[bucket_time] = []
            buckets[bucket_time].append(c)

        candles_5m = []
        for ts in sorted(buckets.keys()):
            group = buckets[ts]
            candles_5m.append(Candle(
                timestamp=ts,
                open=group[0].open,
                high=max(c.high for c in group),
                low=min(c.low for c in group),
                close=group[-1].close,
                volume=sum(c.volume for c in group),
                symbol=group[0].symbol,
                interval="5m",
            ))
        return candles_5m
