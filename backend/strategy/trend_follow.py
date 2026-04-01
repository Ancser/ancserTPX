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
            candle: current 1m candle
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

        # ── State: confirmed (order placed, waiting for fill) ──
        elif self._state == "confirmed":
            # Nothing to do — engine manages the pending order.
            # If the order was cancelled externally, notify_order_cancelled
            # will move us back to idle.
            return None

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
        """
        Generate the initial trend signal.

        Entry price = 50% midpoint of (4-candle breakthrough extreme + VAH/VAL).
        For up breakout:  entry = (max_high_of_4_candles + VAH_80) / 2
        For down breakout: entry = (min_low_of_4_candles + VAL_80) / 2
        """
        if not self._ref_zone:
            return None

        if not self._outside_candles or len(self._outside_candles) < self.confirm_candles:
            return None

        confirm_4 = self._outside_candles[:self.confirm_candles]

        if self._exit_direction == "up":
            # 4-candle max high + VAH → midpoint
            max_high = max(c.high for c in confirm_4)
            vah = self._ref_zone.vah_80
            entry = (max_high + vah) / 2.0
            sl = entry - self.sl_points
            tp = entry + self.tp_points
            direction = Direction.BUY
        elif self._exit_direction == "down":
            # 4-candle min low + VAL → midpoint
            min_low = min(c.low for c in confirm_4)
            val = self._ref_zone.val_80
            entry = (min_low + val) / 2.0
            sl = entry + self.sl_points
            tp = entry - self.tp_points
            direction = Direction.SELL
        else:
            return None

        sl_dollars = self.sl_points * POINT_VALUE
        tp_dollars = self.tp_points * POINT_VALUE

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
                f"entry=50%({('high' if self._exit_direction == 'up' else 'low')}+{'VAH' if self._exit_direction == 'up' else 'VAL'}) | "
                f"SL ${sl_dollars:.0f} TP ${tp_dollars:.0f}"
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

    def notify_order_cancelled(self):
        """Called by engine when a pending trend order is cancelled (timeout/flatten/new zones)."""
        if self._state in ("confirmed", "retry"):
            logger.info(f"[TrendFollow] Order cancelled → reset from {self._state} to idle")
            self._state = "idle"
            self._outside_candles = []
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


# ══════════════════════════════════════════════════════════
# Session-based Trend Follow (for live overnight trading)
# ══════════════════════════════════════════════════════════

class SessionTrendFollow:
    """
    Session-based trend follow strategy.

    規則:
      1. 等待 SessionZoneDetector 報告區間成熟
      2. 突破上方: 連續 5 根 1m close > VAH → BUY LIMIT @ VAH + 20%×(H100-VAH)
      3. 突破下方: 連續 5 根 1m close < VAL → SELL LIMIT @ VAL - 20%×(VAL-L100)
      4. SL: BUY → VAH - 50 tick,  SELL → VAL + 50 tick
      5. TP: entry ± (|entry - SL| × 3)
      6. 30 分鐘未成交取消
    """

    BREAKOUT_CONFIRM_CANDLES = 5   # 連續 5 根 close 在外
    ENTRY_RATIO = 0.5              # entry = VAH/VAL + 50% × (extreme - edge)
    SL_TICKS = 50                  # 50 tick = 12.5 pts
    TP_MULTIPLIER = 3              # TP = SL × 3
    TICK_SIZE = 0.25
    PENDING_TIMEOUT_CANDLES = 30   # 30 根 1m = 30 min

    def __init__(self):
        self._state = "idle"  # idle | watching | confirmed | in_trade
        self._consecutive_outside: int = 0
        self._breakout_direction: Optional[str] = None  # "up" | "down"
        self._ref_zone = None  # snapshot of zone at breakout

    def reset(self):
        self._state = "idle"
        self._consecutive_outside = 0
        self._breakout_direction = None
        self._ref_zone = None

    def evaluate(
        self,
        candle: Candle,
        zone: Optional[ConsolidationZone],
        is_mature: bool,
    ) -> Optional[TradeSignal]:
        """
        每根 1m K 線調用一次.

        Args:
            candle:    current 1m candle
            zone:      active session zone (from SessionZoneDetector)
            is_mature: whether zone is mature
        """
        # Need a mature zone to trade
        if not zone or not is_mature:
            if self._state == "watching":
                self._state = "idle"
                self._consecutive_outside = 0
            return None

        # No operation on AH (After Hours 20:00 - 22:00 UTC)
        h = candle.timestamp.hour
        if 20 <= h < 22:
            return None

        # Already confirmed or in trade → engine manages
        if self._state in ("confirmed", "in_trade"):
            return None

        vah = zone.vah_80
        val = zone.val_80

        # ── Check breakout direction ──
        if candle.close > vah:
            current_dir = "up"
        elif candle.close < val:
            current_dir = "down"
        else:
            # Inside VA → reset
            self._consecutive_outside = 0
            self._breakout_direction = None
            self._state = "idle"
            return None

        # ── Count consecutive outside ──
        if current_dir == self._breakout_direction:
            self._consecutive_outside += 1
        else:
            # Direction changed
            self._breakout_direction = current_dir
            self._consecutive_outside = 1

        self._state = "watching"

        # ── 5 consecutive → confirmed breakout ──
        if self._consecutive_outside >= self.BREAKOUT_CONFIRM_CANDLES:
            self._state = "confirmed"
            self._ref_zone = zone
            return self._generate_signal(candle, zone, current_dir)

        return None

    def _generate_signal(
        self,
        candle: Candle,
        zone: ConsolidationZone,
        direction: str,
    ) -> TradeSignal:
        """Generate entry signal with SL/TP."""
        sl_points = self.SL_TICKS * self.TICK_SIZE  # 50 * 0.25 = 12.5 pts

        if direction == "up":
            # BUY: entry = VAH + 20% × (H100 - VAH), SL = VAH - 12.5
            entry = zone.vah_80 + self.ENTRY_RATIO * (zone.high_100 - zone.vah_80)
            sl = zone.vah_80 - sl_points
            sl_distance = abs(entry - sl)
            tp = entry + sl_distance * self.TP_MULTIPLIER
            trade_dir = Direction.BUY
        else:
            # SELL: entry = VAL - 20% × (VAL - L100), SL = VAL + 12.5
            entry = zone.val_80 - self.ENTRY_RATIO * (zone.val_80 - zone.low_100)
            sl = zone.val_80 + sl_points
            sl_distance = abs(entry - sl)
            tp = entry - sl_distance * self.TP_MULTIPLIER
            trade_dir = Direction.SELL

        sl_dollars = sl_distance * POINT_VALUE
        tp_dollars = sl_distance * self.TP_MULTIPLIER * POINT_VALUE

        logger.info(
            f"[SessionTrend] BREAKOUT {direction.upper()} confirmed | "
            f"entry={(entry):.2f} SL={sl:.2f} TP={tp:.2f} | "
            f"SL ${sl_dollars:.0f} TP ${tp_dollars:.0f} | "
            f"zone={zone.zone_id}"
        )

        return TradeSignal(
            strategy=StrategyType.TREND_FOLLOW,
            direction=trade_dir,
            entry_price=entry,
            sl_price=sl,
            tp_price=tp,
            zone_id=zone.zone_id,
            reason=(
                f"SESSION TREND {direction.upper()} | "
                f"5-bar breakout {'> VAH' if direction == 'up' else '< VAL'} | "
                f"entry=20%({'H100-VAH' if direction == 'up' else 'VAL-L100'}) | "
                f"SL ${sl_dollars:.0f} TP ${tp_dollars:.0f} (1:{self.TP_MULTIPLIER})"
            ),
            timestamp=candle.timestamp,
        )

    def notify_trade_closed(self, exit_reason: str):
        """Called by engine when trade closes."""
        self._state = "idle"
        self._consecutive_outside = 0
        self._breakout_direction = None

    def notify_order_cancelled(self):
        """Called by engine when pending order is cancelled/timeout."""
        if self._state == "confirmed":
            logger.info("[SessionTrend] Order cancelled → reset")
        self._state = "idle"
        self._consecutive_outside = 0
        self._breakout_direction = None

    def get_phase_label(self) -> str:
        if self._state == "idle":
            return "等待突破"
        elif self._state == "watching":
            return f"出界({self._consecutive_outside}/{self.BREAKOUT_CONFIRM_CANDLES} bar)"
        elif self._state == "confirmed":
            return "入場準備"
        elif self._state == "in_trade":
            return "持倉中"
        return self._state

    @property
    def raw_state(self) -> str:
        return self._state
