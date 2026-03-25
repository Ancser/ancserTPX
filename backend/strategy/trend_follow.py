# ============================================================
# 文件: backend/strategy/trend_follow.py
# 狀態: 已更新 v2
# 規則:
#   1. Candle opens OUTSIDE 90% of formed range
#   2. Wait 4 candles staying outside
#   3. Compare total vol of 4 candles with vol of 1 candle before them
#   4. If 4-candle vol > 1-candle vol → trend mode (高量確認趨勢)
#   5. Sell limit if under range, Buy limit if over range
#   6. SL: $300 (15pt), TP: $1200 (60pt)
#   7. If first trade SL'd → retry at POC of big range, same SL:TP
# ============================================================
"""
策略二：趨勢跟隨 (Trend Follow)

全新狀態機設計：
  [idle] → 價格在 zone 內
  [watching] → 價格離開 90% range，開始計數
  [confirmed] → 4 根 K 線在外 + 成交量確認 → 發出信號
  [retry] → 第一筆 SL 後，等待 POC 回測入場
"""

from __future__ import annotations
import logging
from typing import List, Optional
from backend.db.models import (
    Candle, ConsolidationZone, TradeSignal,
    Direction, StrategyType, ZoneStatus
)

logger = logging.getLogger(__name__)

POINT_VALUE = 20.0


class TrendFollowStrategy:
    """
    Stateful trend follow strategy.

    Tracks candles outside the zone to confirm trend, then enters.
    If first entry fails (SL), retries at POC.
    """

    def __init__(
        self,
        sl_points: float = 15.0,       # $300
        tp_points: float = 60.0,       # $1200
        confirm_candles: int = 4,       # 需要 4 根在外
        range_pct: float = 0.90,        # 90% of range
        point_value: float = 20.0,
        contracts: int = 1,
    ):
        self.sl_points = sl_points
        self.tp_points = tp_points
        self.confirm_candles = confirm_candles
        self.range_pct = range_pct
        self.point_value = point_value
        self.contracts = contracts

        # State machine
        self._state = "idle"  # idle | watching | confirmed | retry
        self._outside_candles: List[Candle] = []  # candles outside range
        self._candle_before_exit: Optional[Candle] = None  # 1 candle before the 4
        self._exit_direction: Optional[str] = None  # "up" | "down"
        self._ref_zone: Optional[ConsolidationZone] = None  # the zone we left
        self._first_trade_failed: bool = False
        self._all_recent_candles: List[Candle] = []  # sliding window

    def reset(self):
        """Reset state machine"""
        self._state = "idle"
        self._outside_candles = []
        self._candle_before_exit = None
        self._exit_direction = None
        self._ref_zone = None
        self._first_trade_failed = False
        self._all_recent_candles = []

    def evaluate(
        self,
        candle: Candle,
        active_zone: Optional[ConsolidationZone],
        all_zones: Optional[List[ConsolidationZone]] = None,
        last_trade_was_sl: bool = False,
    ) -> Optional[TradeSignal]:
        """
        Evaluate each candle. Stateful — call on every candle.

        Args:
            candle: current 5m candle
            active_zone: currently active zone (if any)
            all_zones: all zones for POC reference
            last_trade_was_sl: whether the last trend trade hit SL
        """
        # Keep sliding window of recent candles
        self._all_recent_candles.append(candle)
        if len(self._all_recent_candles) > 20:
            self._all_recent_candles = self._all_recent_candles[-20:]

        # Track if we need to retry at POC
        if last_trade_was_sl and self._state == "confirmed":
            self._first_trade_failed = True
            self._state = "retry"
            logger.info("[TrendFollow] First trade SL'd → retry mode at POC")

        # ── State: idle ──
        if self._state == "idle":
            return self._handle_idle(candle, active_zone, all_zones)

        # ── State: watching (counting candles outside) ──
        elif self._state == "watching":
            return self._handle_watching(candle)

        # ── State: retry (wait for price to return to POC) ──
        elif self._state == "retry":
            return self._handle_retry(candle)

        return None

    def _handle_idle(
        self,
        candle: Candle,
        active_zone: Optional[ConsolidationZone],
        all_zones: Optional[List[ConsolidationZone]],
    ) -> Optional[TradeSignal]:
        """
        In idle state, look for price leaving the 90% range.
        """
        # Use the most recent LEFT zone as reference
        ref_zone = None
        if all_zones:
            for z in reversed(all_zones):
                if z.status == ZoneStatus.LEFT:
                    ref_zone = z
                    break

        # If there's an active zone being exited right now
        if active_zone and active_zone.status == ZoneStatus.ACTIVE:
            ref_zone = active_zone

        if not ref_zone:
            return None

        # Calculate 90% boundaries
        range_100 = ref_zone.high_100 - ref_zone.low_100
        if range_100 <= 0:
            return None

        margin = range_100 * (1.0 - self.range_pct) / 2.0  # 5% on each side
        upper_90 = ref_zone.high_100 - margin
        lower_90 = ref_zone.low_100 + margin

        # Check if candle opens outside 90% range
        if candle.open > upper_90:
            self._exit_direction = "up"
        elif candle.open < lower_90:
            self._exit_direction = "down"
        else:
            return None

        # Start watching
        self._state = "watching"
        self._ref_zone = ref_zone
        self._outside_candles = [candle]
        self._first_trade_failed = False

        # The candle before exit = the one right before this one
        if len(self._all_recent_candles) >= 2:
            self._candle_before_exit = self._all_recent_candles[-2]
        else:
            self._candle_before_exit = None

        logger.info(f"[TrendFollow] Watching: price outside 90% range, dir={self._exit_direction}")
        return None

    def _handle_watching(self, candle: Candle) -> Optional[TradeSignal]:
        """
        Count candles staying outside. After 4, check volume.
        """
        if not self._ref_zone:
            self._state = "idle"
            return None

        range_100 = self._ref_zone.high_100 - self._ref_zone.low_100
        if range_100 <= 0:
            self._state = "idle"
            return None

        margin = range_100 * (1.0 - self.range_pct) / 2.0
        upper_90 = self._ref_zone.high_100 - margin
        lower_90 = self._ref_zone.low_100 + margin

        # Check if candle is still outside
        still_outside = False
        if self._exit_direction == "up" and candle.close > upper_90:
            still_outside = True
        elif self._exit_direction == "down" and candle.close < lower_90:
            still_outside = True

        if not still_outside:
            # Price came back in → reset
            logger.info("[TrendFollow] Price returned inside range → reset")
            self._state = "idle"
            self._outside_candles = []
            return None

        self._outside_candles.append(candle)

        # Need 4 candles outside
        if len(self._outside_candles) < self.confirm_candles:
            return None

        # ── 4 candles confirmed → check volume ──
        total_vol_4 = sum(c.volume for c in self._outside_candles[:4])

        # Volume of 1 candle before exit
        if self._candle_before_exit:
            vol_before = self._candle_before_exit.volume
        else:
            vol_before = total_vol_4  # fallback: no comparison

        # Trend confirmed: 4-candle vol > 1-candle vol (高量 = 趨勢有動力)
        if vol_before > 0 and total_vol_4 > vol_before:
            logger.info(
                f"[TrendFollow] CONFIRMED: 4-candle vol={total_vol_4} > "
                f"pre-exit vol={vol_before} → trend {self._exit_direction}"
            )
            self._state = "confirmed"
            return self._generate_signal(candle)
        else:
            logger.info(
                f"[TrendFollow] Volume check failed: 4-candle={total_vol_4} "
                f"vs pre-exit={vol_before} → no trend"
            )
            self._state = "idle"
            self._outside_candles = []
            return None

    def _handle_retry(self, candle: Candle) -> Optional[TradeSignal]:
        """
        After first trade SL, retry at POC of the big range.
        """
        if not self._ref_zone:
            self._state = "idle"
            return None

        poc = self._ref_zone.poc

        # Check if price touches POC
        if self._exit_direction == "up":
            # We were going long, SL'd. Now retry buy limit at POC
            if candle.low <= poc + 2.0:  # tolerance
                entry = poc
                sl = entry - self.sl_points
                tp = entry + self.tp_points
                self._state = "idle"
                logger.info(f"[TrendFollow] RETRY at POC {poc:.2f} BUY")
                return TradeSignal(
                    strategy=StrategyType.TREND_FOLLOW,
                    direction=Direction.BUY,
                    entry_price=entry,
                    sl_price=sl,
                    tp_price=tp,
                    zone_id=self._ref_zone.zone_id,
                    reason=f"TREND RETRY BUY @ POC {entry:.2f} | SL $300 TP $1200",
                    timestamp=candle.timestamp,
                )

        elif self._exit_direction == "down":
            # We were going short, SL'd. Now retry sell limit at POC
            if candle.high >= poc - 2.0:
                entry = poc
                sl = entry + self.sl_points
                tp = entry - self.tp_points
                self._state = "idle"
                logger.info(f"[TrendFollow] RETRY at POC {poc:.2f} SELL")
                return TradeSignal(
                    strategy=StrategyType.TREND_FOLLOW,
                    direction=Direction.SELL,
                    entry_price=entry,
                    sl_price=sl,
                    tp_price=tp,
                    zone_id=self._ref_zone.zone_id,
                    reason=f"TREND RETRY SELL @ POC {entry:.2f} | SL $300 TP $1200",
                    timestamp=candle.timestamp,
                )

        # Timeout: if we wait too long for POC, give up
        if len(self._all_recent_candles) > 15:
            self._state = "idle"

        return None

    def _generate_signal(self, candle: Candle) -> Optional[TradeSignal]:
        """Generate the initial trend signal."""
        if not self._ref_zone:
            return None

        if self._exit_direction == "up":
            # Price broke above → buy limit at current level
            entry = candle.close
            sl = entry - self.sl_points
            tp = entry + self.tp_points
            direction = Direction.BUY
        elif self._exit_direction == "down":
            # Price broke below → sell limit
            entry = candle.close
            sl = entry + self.sl_points
            tp = entry - self.tp_points
            direction = Direction.SELL
        else:
            return None

        return TradeSignal(
            strategy=StrategyType.TREND_FOLLOW,
            direction=direction,
            entry_price=entry,
            sl_price=sl,
            tp_price=tp,
            zone_id=self._ref_zone.zone_id,
            reason=(
                f"TREND {direction.value.upper()} | "
                f"4-candle confirm {self._exit_direction} | "
                f"SL $300 TP $1200"
            ),
            timestamp=candle.timestamp,
        )

    def notify_trade_closed(self, exit_reason: str):
        """Called by engine when a trend trade closes."""
        if exit_reason == "sl" and self._state == "confirmed":
            self._first_trade_failed = True
            self._state = "retry"
            logger.info("[TrendFollow] Trade SL'd → entering retry mode")
        elif exit_reason in ("tp", "flatten"):
            self._state = "idle"
            self._first_trade_failed = False

    def get_phase_label(self) -> str:
        """Return current state label for UI display."""
        if self._state == "idle":
            return "idle"
        elif self._state == "watching":
            n = len(self._outside_candles)
            if n >= self.confirm_candles - 1:
                return f"確認出界({n} bar)"
            return f"出界({n} bar)"
        elif self._state == "confirmed":
            return "入場準備"
        elif self._state == "retry":
            return "等待POC"
        return self._state

    @property
    def raw_state(self) -> str:
        return self._state
