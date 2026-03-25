# ============================================================
# 文件: backend/backtest/engine.py
# 狀態: 已更新 v2
# 變更:
#   - Reversion: 10 candle zone, top 90%/bottom 10%, SL $300 TP $900
#   - TrendFollow: stateful, 4-candle confirm, vol comparison, POC retry
#   - Zone data includes formed_at for VP drawing
# ============================================================
"""
回測引擎 v2

將歷史 K 線逐根餵入 → 盤整偵測 → 策略評估 → 模擬成交 → 績效計算
"""

from __future__ import annotations
import uuid
import logging
from datetime import datetime, time, timedelta
from typing import Dict, List, Optional, Tuple

from backend.db.models import (
    Candle, Trade, TradeSignal, BacktestConfig, BacktestResult,
    Metrics, ConsolidationZone, BreakoutAnalysis,
    Direction, ExitReason, StrategyType, ZoneStatus,
)
from backend.strategy.consolidation import ConsolidationDetector, ZoneEvent
from backend.strategy.reversion import ReversionStrategy
from backend.strategy.trend_follow import TrendFollowStrategy

logger = logging.getLogger(__name__)


class BacktestEngine:
    """回測引擎 v2"""

    POINT_VALUE = 20.0
    TICK_SIZE = 0.25
    FLATTEN_TIME = time(15, 5)

    def __init__(self, config: Optional[BacktestConfig] = None):
        self.config = config or BacktestConfig()

        self.detector = ConsolidationDetector(
            min_candles=self.config.min_candles_for_zone,
            poc_drift_threshold=self.config.poc_drift_threshold,
            value_area_pct=self.config.value_area_pct,
        )
        self.reversion = ReversionStrategy(
            sl_points=15.0,       # $300
            tp_points=45.0,       # $900
            min_zone_candles=10,
            entry_pct_high=0.90,
            entry_pct_low=0.10,
        )
        self.trend_follow = TrendFollowStrategy(
            sl_points=15.0,       # $300
            tp_points=60.0,       # $1200
            confirm_candles=4,
            range_pct=0.90,
        )

        # State
        self._capital = self.config.initial_capital
        self._open_position: Optional[Trade] = None
        self._trades: List[Trade] = []
        self._equity_curve: List[Tuple[datetime, float]] = []
        self._daily_pnl: Dict[str, float] = {}
        self._last_zone_event: Optional[ZoneEvent] = None
        self._last_closed_trade: Optional[Trade] = None

    def run(self, candles: List[Candle]) -> BacktestResult:
        """執行回測"""
        logger.info(
            f"開始回測: {len(candles)} 根 K 線, "
            f"初始資金=${self._capital:,.0f}"
        )
        self._reset()

        for candle in candles:
            self._process_candle(candle)

        if self._open_position:
            self._force_exit(candles[-1], ExitReason.FLATTEN)

        from backend.backtest.metrics import MetricsCalculator
        calc = MetricsCalculator()
        metrics = calc.calculate_all(self._trades, self.config.initial_capital)

        result = BacktestResult(
            config=self.config,
            trades=self._trades,
            zones=self.detector.get_all_zones(),
            metrics=metrics,
            equity_curve=self._equity_curve,
        )

        logger.info(
            f"回測完成: {metrics.total_trades} 筆交易, "
            f"勝率={metrics.win_rate:.1%}, PnL=${metrics.total_pnl:,.0f}"
        )
        return result

    def run_with_replay(self, candles: List[Candle]) -> dict:
        """
        Run backtest and capture per-candle snapshots for replay mode.

        Returns dict with:
          - result: BacktestResult (normal)
          - snapshots: List[dict] per-candle state
          - candles: List[dict] raw candle data for chart
        """
        logger.info(f"開始回測 (replay mode): {len(candles)} 根 K 線")
        self._reset()

        from backend.strategy.volume_profile import VolumeProfileCalculator
        vp_calc = VolumeProfileCalculator(tick_size=0.25, value_area_pct=0.80)

        snapshots = []
        candle_dicts = []
        prev_open_id = None

        for i, candle in enumerate(candles):
            # Track trade events on this candle
            open_before = self._open_position.trade_id if self._open_position else None
            trades_before = len(self._trades)

            self._process_candle(candle)

            open_after = self._open_position.trade_id if self._open_position else None
            trades_after = len(self._trades)

            # Detect events
            events = []
            if open_after and open_after != open_before:
                events.append("entry")
            if trades_after > trades_before:
                t = self._trades[-1]
                events.append(f"exit_{t.exit_reason.value}" if t.exit_reason else "exit")

            # Capture zone state
            active_zone = self.detector.get_active_zone()
            all_zones = self.detector.get_all_zones()

            # Strategy phase
            trend_phase = self.trend_follow.get_phase_label()
            trend_raw = self.trend_follow.raw_state

            if self._open_position:
                if self._open_position.strategy == StrategyType.REVERSION:
                    phase = "入場中"
                    mode = "邊界回歸POC"
                else:
                    phase = "入場中"
                    mode = "趨勢突破"
            elif active_zone and trend_raw == "idle":
                # In consolidation
                phase = "盤整"
                mode = "邊界回歸POC"
            elif trend_raw != "idle":
                phase = trend_phase
                mode = "趨勢突破"
            else:
                phase = "等待盤整"
                mode = "idle"

            # Zone data with VP profiles
            zone_list = []
            for z in all_zones:
                zd = {
                    "zone_id": z.zone_id,
                    "poc": z.poc,
                    "vah_80": z.vah_80,
                    "val_80": z.val_80,
                    "high_100": z.high_100,
                    "low_100": z.low_100,
                    "status": z.status.value,
                    "formed_at": z.formed_at.isoformat() if z.formed_at else None,
                    "left_at": z.left_at.isoformat() if z.left_at else None,
                    "exit_direction": z.exit_direction,
                    "num_candles": z.num_candles,
                }
                # VP profile for current active zone (re-computed)
                if z.candles and z.status.value == "active":
                    try:
                        vp = vp_calc.calculate(z.candles)
                        max_vol = max(vp.profile.values()) if vp.profile else 1
                        zd["profile"] = [
                            {"price": p, "volume": v, "pct": round(v / max_vol, 3)}
                            for p, v in sorted(vp.profile.items())
                        ]
                    except Exception:
                        zd["profile"] = []
                else:
                    zd["profile"] = []
                zone_list.append(zd)

            # Open trade info
            open_trade = None
            if self._open_position:
                p = self._open_position
                open_trade = {
                    "trade_id": p.trade_id,
                    "strategy": p.strategy.value,
                    "direction": p.direction.value,
                    "entry_price": p.entry_price,
                    "entry_time": p.entry_time.isoformat(),
                    "sl_price": p.sl_price,
                    "tp_price": p.tp_price,
                    "zone_id": p.zone_id,
                }

            snapshots.append({
                "i": i,
                "phase": phase,
                "mode": mode,
                "events": events,
                "zones": zone_list,
                "open_trade": open_trade,
                "capital": round(self._capital, 0),
            })

            candle_dicts.append({
                "time": candle.timestamp.isoformat(),
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
            })

        # Force exit if still open
        if self._open_position:
            self._force_exit(candles[-1], ExitReason.FLATTEN)

        from backend.backtest.metrics import MetricsCalculator
        calc = MetricsCalculator()
        metrics = calc.calculate_all(self._trades, self.config.initial_capital)

        result = BacktestResult(
            config=self.config,
            trades=self._trades,
            zones=self.detector.get_all_zones(),
            metrics=metrics,
            equity_curve=self._equity_curve,
        )

        # Build trades response
        trades_resp = []
        for t in self._trades:
            risk = abs(t.sl_price - t.entry_price) * self.POINT_VALUE
            reward = abs(t.tp_price - t.entry_price) * self.POINT_VALUE
            trades_resp.append({
                "trade_id": t.trade_id,
                "strategy": t.strategy.value,
                "direction": t.direction.value,
                "entry_price": t.entry_price,
                "entry_time": t.entry_time.isoformat(),
                "exit_price": t.exit_price,
                "exit_time": t.exit_time.isoformat() if t.exit_time else None,
                "sl_price": t.sl_price,
                "tp_price": t.tp_price,
                "pnl": t.pnl,
                "exit_reason": t.exit_reason.value if t.exit_reason else None,
                "zone_id": t.zone_id,
                "risk": risk,
                "reward": reward,
            })

        logger.info(
            f"回測完成 (replay): {metrics.total_trades} 筆交易, "
            f"勝率={metrics.win_rate:.1%}, PnL=${metrics.total_pnl:,.0f}, "
            f"snapshots={len(snapshots)}"
        )

        return {
            "result": result,
            "metrics": metrics,
            "trades": trades_resp,
            "snapshots": snapshots,
            "candles": candle_dicts,
        }

    def _reset(self):
        self._capital = self.config.initial_capital
        self._open_position = None
        self._trades = []
        self._equity_curve = []
        self._daily_pnl = {}
        self._last_zone_event = None
        self._last_closed_trade = None
        self.detector.reset()
        self.trend_follow.reset()

    def _process_candle(self, candle: Candle):
        self._equity_curve.append((candle.timestamp, self._capital))

        # Daily loss limit
        date_str = candle.timestamp.strftime("%Y-%m-%d")
        daily = self._daily_pnl.get(date_str, 0)
        if daily <= -self.config.max_daily_loss:
            if self._open_position:
                self._force_exit(candle, ExitReason.FLATTEN)
            return

        # Flatten time
        candle_time = candle.timestamp.time()
        if candle_time >= self.FLATTEN_TIME:
            if self._open_position:
                self._force_exit(candle, ExitReason.FLATTEN)
            return

        # Check SL/TP
        if self._open_position:
            self._check_exit(candle)
            if self._open_position:
                return  # still open, don't open new

        # Update consolidation detector
        event = self.detector.update(candle)
        if event:
            self._last_zone_event = event

        # ── Always evaluate trend follow (stateful) ──
        # Check if last closed trade was a trend SL
        last_trend_sl = False
        if (self._last_closed_trade
                and self._last_closed_trade.strategy == StrategyType.TREND_FOLLOW
                and self._last_closed_trade.exit_reason == ExitReason.SL):
            last_trend_sl = True
            self._last_closed_trade = None  # consume

        all_zones = self.detector.get_all_zones()
        active_zone = self.detector.get_active_zone()

        # Try trend follow first (it needs every candle for state tracking)
        if not self._open_position and "trend_follow" in self.config.strategies:
            signal = self.trend_follow.evaluate(
                candle, active_zone, all_zones, last_trend_sl
            )
            if signal:
                self._execute_entry(signal, candle)
                return

        # Try reversion
        if not self._open_position and "reversion" in self.config.strategies:
            if active_zone:
                signal = self.reversion.evaluate(candle, active_zone)
                if signal:
                    self._execute_entry(signal, candle)
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
        )
        self._open_position = trade
        logger.debug(
            f"入場: {trade.strategy.value} {trade.direction.value} "
            f"@ {fill_price:.2f} | SL={trade.sl_price:.2f} TP={trade.tp_price:.2f}"
        )

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

        # Notify trend follow strategy of trade close
        if pos.strategy == StrategyType.TREND_FOLLOW:
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
