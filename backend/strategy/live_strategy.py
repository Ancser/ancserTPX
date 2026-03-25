# ============================================================
# 文件: backend/strategy/live_strategy.py
# 狀態: 已更新 v2
# 變更:
#   - Reversion: 10 candle zone, top 90%/bottom 10%, SL $300 TP $900
#   - TrendFollow: stateful, 4-candle confirm, vol comparison, POC retry
#   - Zone data includes profile for VP drawing at zone start
# ============================================================
"""
Real-time Strategy Engine v2

Runs consolidation detection + reversion/trend strategy
on 5m candles as they form.
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from backend.db.models import Candle, ConsolidationZone, ZoneStatus
from backend.strategy.consolidation import ConsolidationDetector, ZoneEvent
from backend.strategy.reversion import ReversionStrategy
from backend.strategy.trend_follow import TrendFollowStrategy
from backend.strategy.volume_profile import VolumeProfileCalculator

logger = logging.getLogger(__name__)

POINT_VALUE = 20.0


@dataclass
class LiveTrade:
    """A trade tracked in real-time"""
    trade_id: str
    strategy: str           # "reversion" | "trend_follow"
    direction: str          # "buy" | "sell"
    entry_price: float
    entry_time: str
    sl_price: float
    tp_price: float
    zone_id: str
    vol_ratio: Optional[float] = None
    is_big_trend: bool = False
    exit_price: Optional[float] = None
    exit_time: Optional[str] = None
    pnl: Optional[float] = None
    exit_reason: Optional[str] = None


class LiveStrategyEngine:
    """Real-time strategy execution v2"""

    def __init__(self):
        self.detector = ConsolidationDetector(
            min_candles=6,
            max_candles=50,
            poc_drift_threshold=3.0,
            min_touches=2,
            exit_confirm_candles=2,
            value_area_pct=0.80,
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
        self.vp_calc = VolumeProfileCalculator(tick_size=0.25, value_area_pct=0.80)

        # State
        self._candles_processed: int = 0
        self._trade_counter: int = 0
        self._open_trades: List[LiveTrade] = []
        self._closed_trades: List[LiveTrade] = []
        self._last_zone_event: Optional[ZoneEvent] = None
        self._last_signal_candle_idx: int = -10

    def update(self, candle: Candle) -> None:
        """Process a new 5m candle."""
        self._candles_processed += 1

        # Step 1: Manage open trades
        self._manage_open_trades(candle)

        # Step 2: Feed to detector
        event = self.detector.update(candle)
        if event:
            self._last_zone_event = event
            if event.event_type == "left" and event.breakout:
                logger.info(f"[LiveStrategy] Zone {event.zone.zone_id} LEFT "
                           f"dir={event.zone.exit_direction}")
            elif event.event_type == "active":
                logger.info(f"[LiveStrategy] Zone {event.zone.zone_id} ACTIVE "
                           f"POC={event.zone.poc:.2f}")

        # Step 3: Only evaluate if no open trades
        if self._open_trades:
            return

        if self._candles_processed - self._last_signal_candle_idx < 2:
            return

        # Check if last closed was trend SL
        last_trend_sl = False
        if self._closed_trades:
            last = self._closed_trades[-1]
            if last.strategy == "trend_follow" and last.exit_reason == "sl":
                last_trend_sl = True

        all_zones = self.detector.get_all_zones()
        active_zone = self.detector.get_active_zone()

        # Trend follow (stateful — needs every candle)
        signal = self.trend_follow.evaluate(
            candle, active_zone, all_zones, last_trend_sl
        )
        if signal:
            self._open_trade(signal, candle)
            return

        # Reversion
        if active_zone:
            signal = self.reversion.evaluate(candle, active_zone)
            if signal:
                self._open_trade(signal, candle)
                return

    def _open_trade(self, signal, candle: Candle) -> None:
        self._trade_counter += 1
        trade = LiveTrade(
            trade_id=f"LT{self._trade_counter:04d}",
            strategy=signal.strategy.value,
            direction=signal.direction.value,
            entry_price=signal.entry_price,
            entry_time=candle.timestamp.isoformat(),
            sl_price=signal.sl_price,
            tp_price=signal.tp_price,
            zone_id=signal.zone_id,
            vol_ratio=getattr(signal, 'vol_ratio', None),
            is_big_trend=getattr(signal, 'is_big_trend', False),
        )
        self._open_trades.append(trade)
        self._last_signal_candle_idx = self._candles_processed
        logger.info(f"[LiveStrategy] OPEN {trade.trade_id} {trade.strategy} "
                   f"{trade.direction} @ {trade.entry_price:.2f} "
                   f"SL={trade.sl_price:.2f} TP={trade.tp_price:.2f}")

    def _manage_open_trades(self, candle: Candle) -> None:
        still_open = []
        for trade in self._open_trades:
            closed = False

            if trade.direction == "buy":
                if candle.high >= trade.tp_price:
                    trade.exit_price = trade.tp_price
                    trade.exit_reason = "tp"
                    closed = True
                elif candle.low <= trade.sl_price:
                    trade.exit_price = trade.sl_price
                    trade.exit_reason = "sl"
                    closed = True
            else:
                if candle.low <= trade.tp_price:
                    trade.exit_price = trade.tp_price
                    trade.exit_reason = "tp"
                    closed = True
                elif candle.high >= trade.sl_price:
                    trade.exit_price = trade.sl_price
                    trade.exit_reason = "sl"
                    closed = True

            if closed:
                trade.exit_time = candle.timestamp.isoformat()
                if trade.direction == "buy":
                    trade.pnl = (trade.exit_price - trade.entry_price) * POINT_VALUE
                else:
                    trade.pnl = (trade.entry_price - trade.exit_price) * POINT_VALUE
                self._closed_trades.append(trade)

                # Notify trend follow
                if trade.strategy == "trend_follow":
                    self.trend_follow.notify_trade_closed(trade.exit_reason)

                logger.info(f"[LiveStrategy] CLOSE {trade.trade_id} "
                           f"reason={trade.exit_reason} PnL=${trade.pnl:.0f}")
            else:
                still_open.append(trade)

        self._open_trades = still_open

    def get_state(self) -> dict:
        """Return complete state for frontend."""
        active_zone = self.detector.get_active_zone()
        zone_data = None
        if active_zone:
            zone_data = {
                "zone_id": active_zone.zone_id,
                "poc": active_zone.poc,
                "vah_80": active_zone.vah_80,
                "val_80": active_zone.val_80,
                "high_100": active_zone.high_100,
                "low_100": active_zone.low_100,
                "status": active_zone.status.value,
                "num_candles": active_zone.num_candles,
            }

        # All zones with formed_at for VP positioning
        all_zones = []
        for z in self.detector.get_all_zones():
            zone_dict = {
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

            # Include VP profile data for histogram drawing
            if z.candles:
                try:
                    vp = self.vp_calc.calculate(z.candles)
                    # Downsample profile to top 30 levels for frontend
                    sorted_levels = sorted(vp.profile.items(), key=lambda x: x[1], reverse=True)
                    max_vol = sorted_levels[0][1] if sorted_levels else 1
                    zone_dict["profile"] = [
                        {"price": p, "volume": v, "pct": round(v / max_vol, 3)}
                        for p, v in sorted(vp.profile.items())
                    ]
                except Exception:
                    zone_dict["profile"] = []
            else:
                zone_dict["profile"] = []

            all_zones.append(zone_dict)

        # Trades
        open_trades = [{
            "trade_id": t.trade_id,
            "strategy": t.strategy,
            "direction": t.direction,
            "entry_price": t.entry_price,
            "entry_time": t.entry_time,
            "sl_price": t.sl_price,
            "tp_price": t.tp_price,
            "zone_id": t.zone_id,
        } for t in self._open_trades]

        closed_trades = []
        for t in self._closed_trades[-50:]:
            risk = abs(t.sl_price - t.entry_price) * POINT_VALUE
            reward = abs(t.tp_price - t.entry_price) * POINT_VALUE
            closed_trades.append({
                "trade_id": t.trade_id,
                "strategy": t.strategy,
                "direction": t.direction,
                "entry_price": t.entry_price,
                "entry_time": t.entry_time,
                "exit_price": t.exit_price,
                "exit_time": t.exit_time,
                "sl_price": t.sl_price,
                "tp_price": t.tp_price,
                "pnl": t.pnl,
                "exit_reason": t.exit_reason,
                "zone_id": t.zone_id,
                "risk": risk,
                "reward": reward,
                "rr_ratio": round(reward / risk, 2) if risk > 0 else 0,
            })

        metrics = self._calculate_metrics()

        return {
            "candles_processed": self._candles_processed,
            "active_zone": zone_data,
            "all_zones": all_zones,
            "open_trades": open_trades,
            "closed_trades": closed_trades,
            "metrics": metrics,
        }

    def _calculate_metrics(self) -> dict:
        trades = self._closed_trades
        total = len(trades)
        if total == 0:
            return {
                "total_trades": 0, "wins": 0, "losses": 0,
                "win_rate": 0, "total_pnl": 0, "avg_win": 0,
                "avg_loss": 0, "avg_rr": 0, "expectancy": 0,
                "max_drawdown": 0, "profit_factor": 0,
                "max_consec_losses": 0, "open_count": len(self._open_trades),
            }

        wins = [t for t in trades if (t.pnl or 0) > 0]
        losses = [t for t in trades if (t.pnl or 0) <= 0]
        total_pnl = sum(t.pnl or 0 for t in trades)
        avg_win = sum(t.pnl for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t.pnl for t in losses) / len(losses) if losses else 0
        win_rate = len(wins) / total

        rr_list = []
        for t in trades:
            risk = abs(t.sl_price - t.entry_price)
            if risk > 0 and t.exit_price is not None:
                actual_rr = abs(t.exit_price - t.entry_price) / risk
                rr_list.append(actual_rr)
        avg_rr = sum(rr_list) / len(rr_list) if rr_list else 0

        expectancy = (win_rate * avg_win + (1 - win_rate) * avg_loss)

        equity = peak = max_dd = 0
        for t in trades:
            equity += (t.pnl or 0)
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)

        gross_win = sum(t.pnl for t in wins) if wins else 0
        gross_loss = abs(sum(t.pnl for t in losses)) if losses else 0
        profit_factor = gross_win / gross_loss if gross_loss > 0 else (999 if gross_win > 0 else 0)

        max_consec = current_consec = 0
        for t in trades:
            if (t.pnl or 0) <= 0:
                current_consec += 1
                max_consec = max(max_consec, current_consec)
            else:
                current_consec = 0

        return {
            "total_trades": total,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 3),
            "total_pnl": round(total_pnl, 0),
            "avg_win": round(avg_win, 0),
            "avg_loss": round(avg_loss, 0),
            "avg_rr": round(avg_rr, 2),
            "expectancy": round(expectancy, 0),
            "max_drawdown": round(max_dd, 0),
            "profit_factor": round(profit_factor, 2),
            "max_consec_losses": max_consec,
            "open_count": len(self._open_trades),
        }


# -- Singleton --
_live_engine: Optional[LiveStrategyEngine] = None

def get_live_engine() -> Optional[LiveStrategyEngine]:
    return _live_engine

def create_live_engine() -> LiveStrategyEngine:
    global _live_engine
    _live_engine = LiveStrategyEngine()
    return _live_engine

def reset_live_engine() -> None:
    global _live_engine
    _live_engine = None
