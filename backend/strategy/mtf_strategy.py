# ============================================================
# 文件: backend/strategy/mtf_strategy.py
# 狀態: ⚠️ 未完成 — 需要測試和修復
# 問題:
#   1. [BUG] 回測 MTF 模式產生 0 筆交易 — 信號傳遞邏輯修復後尚未驗證
#      - evaluate_1m() signal_5m 傳遞可能在狀態切換時丟失
#      - _track_5m_breakout 外界蠟燭計數包含 detector exit_confirm candles
#      - 修復已套用但未重新測試 (synthetic data test)
#   2. [BUG] 1min zone detection inside big zone = 0 zones found
#      - _evaluate_1m_inside_big 正確 feed detector_1m 嗎?
#      - 可能 1m detector 的 min_candles/poc_drift 參數太嚴
#   3. [BUG] 5m zone formed_at 標記可能不正確 — 前端顯示 zone 起點
#      不在最高/最低觸及範圍內 (zone start 早於實際橫盤期)
#   4. [BUG] 新 5m zone 未標記 VAH/VAL — formed 後只有 POC line
#      (可能是 frontend 只顯示 active zone 的 VAH/VAL, 而 formed
#      事件延遲)
#   5. [TODO] 即時交易 (live engine) MTF 模式需要端對端驗證
#      - live engine _tick_mtf() 邏輯已寫但未實測下單
#      - 需確認 TopstepX API limit order 和 SL/TP 可正常運作
# 關聯文件:
#   ← backend/live/engine.py        (即時交易調用 evaluate_1m)
#   ← backend/backtest/engine.py    (回測調用 evaluate_1m)
#   → backend/strategy/consolidation.py (zone 偵測)
#   → backend/db/models.py          (TradeSignal, ConsolidationZone)
# ============================================================
# Multi-Timeframe Strategy
#
# 5min zone = daily consolidation (big zone)
# 1min zone = micro consolidation inside the big zone
#
# Logic:
#   INSIDE 5min zone  -> trade 1min breakouts, TP = nearest 5min level
#   OUTSIDE 5min zone -> trade 5min breakout (4-candle confirm), TP = SL * multiplier
#   One position at a time. Complete one before starting another.
# ============================================================
"""
MTF 策略

狀態機:
  [WAITING_BIG]     -- 等待 5min 盤整區間
  [INSIDE_BIG]      -- 在 5min zone 裡面，用 1min 做交易
  [IN_1M_TRADE]     -- 1min 突破交易中
  [BIG_BREAKOUT]    -- 5min zone 出界確認
  [IN_5M_TRADE]     -- 5min 突破交易中
"""

from __future__ import annotations
import logging
from datetime import datetime
from typing import List, Optional

from backend.db.models import (
    Candle, ConsolidationZone, TradeSignal,
    Direction, StrategyType, ZoneStatus,
)
from backend.strategy.consolidation import ConsolidationDetector

logger = logging.getLogger(__name__)

POINT_VALUE = 20.0


class MTFStrategy:
    """
    Multi-Timeframe Strategy

    Receives 1-min candles. Internally aggregates to 5-min.
    Runs two ConsolidationDetectors and coordinates signals.
    """

    def __init__(
        self,
        # 5min params
        sl_points_5m: float = 15.0,        # $300
        tp_multiplier_5m: float = 4.0,     # TP = SL * 4
        confirm_candles_5m: int = 4,
        min_candles_5m: int = 6,
        poc_drift_5m: float = 3.0,
        value_area_pct: float = 0.80,
        # 1min params
        sl_points_1m: float = 15.0,        # $300
        confirm_candles_1m: int = 4,
        min_candles_1m: int = 6,
        poc_drift_1m: float = 1.5,
        # Risk
        min_rr: float = 1.0,              # minimum risk/reward ratio
    ):
        # ── 5-min detector ──
        self.detector_5m = ConsolidationDetector(
            min_candles=min_candles_5m,
            poc_drift_threshold=poc_drift_5m,
            value_area_pct=value_area_pct,
        )

        # ── 1-min detector (tighter params for micro zones) ──
        self.detector_1m = ConsolidationDetector(
            min_candles=min_candles_1m,
            max_candles=30,
            poc_drift_threshold=poc_drift_1m,
            value_area_pct=value_area_pct,
            exit_confirm_candles=1,   # faster exit detection on 1m
        )

        # Strategy params
        self.sl_points_5m = sl_points_5m
        self.tp_multiplier_5m = tp_multiplier_5m
        self.confirm_candles_5m = confirm_candles_5m
        self.sl_points_1m = sl_points_1m
        self.confirm_candles_1m = confirm_candles_1m
        self.min_rr = min_rr

        # ── State ──
        self._state = "WAITING_BIG"
        self._big_zone: Optional[ConsolidationZone] = None  # active 5min zone

        # 5min breakout tracking
        self._5m_exit_dir: Optional[str] = None
        self._5m_outside_candles: List[Candle] = []
        self._5m_candle_before_exit: Optional[Candle] = None
        self._5m_all_recent: List[Candle] = []  # sliding window of 5m candles

        # 1min breakout tracking
        self._1m_exit_dir: Optional[str] = None
        self._1m_outside_candles: List[Candle] = []
        self._1m_ref_zone: Optional[ConsolidationZone] = None
        self._1m_watching: bool = False

        # 5min aggregation buffer
        self._1m_buffer: List[Candle] = []
        self._last_5m_candle: Optional[Candle] = None

        # Track all zones for frontend
        self._all_1m_zones: List[ConsolidationZone] = []

    def reset(self):
        self._state = "WAITING_BIG"
        self._big_zone = None
        self._5m_exit_dir = None
        self._5m_outside_candles = []
        self._5m_candle_before_exit = None
        self._5m_all_recent = []
        self._1m_exit_dir = None
        self._1m_outside_candles = []
        self._1m_ref_zone = None
        self._1m_watching = False
        self._1m_buffer = []
        self._last_5m_candle = None
        self._all_1m_zones = []
        self.detector_5m.reset()

    def reset_to_safe_state(self):
        """Reset strategy to a safe state after warm-up.

        Keeps zone data (for display) but clears any active breakout tracking.
        This prevents warm-up breakout states from triggering immediate orders
        on the first live tick.
        """
        old_state = self._state
        logger.info(
            f"[MTF] reset_to_safe_state: {old_state} → "
            f"{'INSIDE_BIG' if self._big_zone else 'WAITING_BIG'}"
        )

        # Clear all breakout tracking
        self._5m_exit_dir = None
        self._5m_outside_candles = []
        self._5m_candle_before_exit = None
        self._1m_exit_dir = None
        self._1m_outside_candles = []
        self._1m_ref_zone = None
        self._1m_watching = False

        # Go back to appropriate waiting state
        if self._big_zone and self._big_zone.status == ZoneStatus.ACTIVE:
            self._state = "INSIDE_BIG"
        else:
            self._state = "WAITING_BIG"
        self.detector_1m.reset()

    # ── Public API ────────────────────────────────────

    def evaluate_1m(self, candle_1m: Candle) -> Optional[TradeSignal]:
        """
        Called on every 1-min candle.
        Returns TradeSignal if entry should be placed, else None.
        """
        # Aggregate to 5min — always process regardless of state
        self._1m_buffer.append(candle_1m)
        signal_5m = None
        if len(self._1m_buffer) >= 5:
            candle_5m = self._aggregate_to_5m(self._1m_buffer[:5])
            self._1m_buffer = self._1m_buffer[5:]
            signal_5m = self._update_5m(candle_5m)

        # If 5m produced a signal, return it immediately (highest priority)
        if signal_5m:
            return signal_5m

        # ── State: WAITING_BIG — nothing to do ──
        if self._state == "WAITING_BIG":
            return None

        # ── State: INSIDE_BIG — trade 1m breakouts ──
        if self._state == "INSIDE_BIG":
            # If price left 5m zone on 1m level, stop 1m trading
            if self._big_zone and not self._big_zone.is_price_inside_range(candle_1m.close):
                return None
            return self._evaluate_1m_inside_big(candle_1m)

        # ── Other states: engine manages position ──
        # IN_1M_TRADE, BIG_BREAKOUT, IN_5M_TRADE
        return None

    def _update_5m(self, candle_5m: Candle) -> Optional[TradeSignal]:
        """Process a completed 5-min candle."""
        self._5m_all_recent.append(candle_5m)
        if len(self._5m_all_recent) > 20:
            self._5m_all_recent = self._5m_all_recent[-20:]

        # If already tracking breakout, continue counting
        if self._5m_exit_dir and self._big_zone:
            return self._track_5m_breakout(candle_5m)

        event = self.detector_5m.update(candle_5m)

        # Check if a new 5m zone became active
        active_5m = self.detector_5m.get_active_zone()

        if active_5m and active_5m.status == ZoneStatus.ACTIVE:
            if self._state in ("WAITING_BIG", "BIG_BREAKOUT") or self._big_zone != active_5m:
                self._big_zone = active_5m
                self._state = "INSIDE_BIG"
                # Reset 1m detector for fresh zone detection
                self.detector_1m.reset()
                self._1m_watching = False
                self._1m_outside_candles = []
                self._5m_exit_dir = None
                self._5m_outside_candles = []
                logger.info(
                    f"[MTF] 5min zone active: {active_5m.zone_id} | "
                    f"POC={active_5m.poc:.2f} VAH={active_5m.vah_80:.2f} VAL={active_5m.val_80:.2f}"
                )
                return None

        # Check if 5m zone was LEFT (breakout detected by detector)
        if event and event.event_type == "left":
            zone_left = event.zone
            self._5m_exit_dir = zone_left.exit_direction

            # Detector already confirmed with exit_confirm_candles (2),
            # so we already have 2 outside candles. Include recent ones.
            # Grab the last few 5m candles that were outside
            self._5m_outside_candles = list(self._5m_all_recent[-3:])  # include current + previous

            if len(self._5m_all_recent) >= 4:
                self._5m_candle_before_exit = self._5m_all_recent[-4]
            elif len(self._5m_all_recent) >= 2:
                self._5m_candle_before_exit = self._5m_all_recent[-2]

            logger.info(
                f"[MTF] 5min zone LEFT: dir={self._5m_exit_dir}, "
                f"outside count={len(self._5m_outside_candles)}"
            )

            # Check if we already have enough candles
            if len(self._5m_outside_candles) >= self.confirm_candles_5m:
                return self._generate_5m_signal(candle_5m)

            self._state = "BIG_BREAKOUT"
            return None

        return None

    def _track_5m_breakout(self, candle_5m: Candle) -> Optional[TradeSignal]:
        """Track 5m breakout confirmation (counting outside candles)."""
        if not self._big_zone or not self._5m_exit_dir:
            return None

        zone = self._big_zone

        # Check if still outside
        still_outside = False
        if self._5m_exit_dir == "up" and candle_5m.close > zone.high_100:
            still_outside = True
        elif self._5m_exit_dir == "down" and candle_5m.close < zone.low_100:
            still_outside = True

        if not still_outside:
            logger.info("[MTF] 5min breakout cancelled, price returned")
            self._5m_exit_dir = None
            self._5m_outside_candles = []
            active = self.detector_5m.get_active_zone()
            if active:
                self._big_zone = active
                self._state = "INSIDE_BIG"
            else:
                self._state = "WAITING_BIG"
            return None

        self._5m_outside_candles.append(candle_5m)

        if len(self._5m_outside_candles) >= self.confirm_candles_5m:
            return self._generate_5m_signal(candle_5m)

        return None

    def _generate_5m_signal(self, candle_5m: Candle) -> Optional[TradeSignal]:
        """Generate 5m breakout trade signal."""
        zone = self._big_zone
        if not zone or not self._5m_exit_dir:
            return None

        confirm = self._5m_outside_candles[:self.confirm_candles_5m]

        # Volume check (soft — skip if no data)
        total_vol = sum(c.volume for c in confirm)
        vol_before = self._5m_candle_before_exit.volume if self._5m_candle_before_exit else 0
        if vol_before > 0 and total_vol <= vol_before:
            logger.info("[MTF] 5min breakout vol check failed, but proceeding anyway")

        # ── Log zone details for debugging stale zone issue ──
        zone_age_str = "?"
        if zone.formed_at:
            zone_age = candle_5m.timestamp - zone.formed_at
            zone_age_str = f"{zone_age.total_seconds()/3600:.1f}h"
            if zone_age.total_seconds() > 86400:  # > 24h
                logger.warning(
                    f"[MTF] ⚠ STALE ZONE: zone_id={zone.zone_id} formed_at={zone.formed_at} "
                    f"age={zone_age_str} — entry price 可能不反映當前市場!"
                )

        logger.info(
            f"[MTF] ZONE USED: id={zone.zone_id} formed={zone.formed_at} age={zone_age_str} | "
            f"POC={zone.poc:.2f} VAH={zone.vah_80:.2f} VAL={zone.val_80:.2f} | "
            f"candle_close={candle_5m.close:.2f}"
        )

        sl = self.sl_points_5m
        tp = sl * self.tp_multiplier_5m

        if self._5m_exit_dir == "up":
            max_high = max(c.high for c in confirm)
            entry = (max_high + zone.vah_80) / 2.0
            sl_price = entry - sl
            tp_price = entry + tp
            direction = Direction.BUY
            logger.info(
                f"[MTF] 5m ENTRY CALC: (max_high={max_high:.2f} + VAH={zone.vah_80:.2f}) / 2 = {entry:.2f}"
            )
        else:
            min_low = min(c.low for c in confirm)
            entry = (min_low + zone.val_80) / 2.0
            sl_price = entry + sl
            tp_price = entry - tp
            direction = Direction.SELL
            logger.info(
                f"[MTF] 5m ENTRY CALC: (min_low={min_low:.2f} + VAL={zone.val_80:.2f}) / 2 = {entry:.2f}"
            )

        self._state = "BIG_BREAKOUT"
        sl_dollars = sl * POINT_VALUE
        tp_dollars = tp * POINT_VALUE

        logger.info(
            f"[MTF] 5min BREAKOUT {self._5m_exit_dir} | "
            f"entry={entry:.2f} SL=${sl_dollars:.0f} TP=${tp_dollars:.0f}"
        )

        return TradeSignal(
            strategy=StrategyType.MTF_5M_TREND,
            direction=direction,
            entry_price=entry,
            sl_price=sl_price,
            tp_price=tp_price,
            zone_id=zone.zone_id,
            reason=(
                f"MTF 5M BREAKOUT {direction.value.upper()} | "
                f"entry=50%({('high' if self._5m_exit_dir == 'up' else 'low')}+"
                f"{'VAH' if self._5m_exit_dir == 'up' else 'VAL'}) | "
                f"SL ${sl_dollars:.0f} TP ${tp_dollars:.0f}"
            ),
            timestamp=candle_5m.timestamp,
        )

    def _evaluate_1m_inside_big(self, candle_1m: Candle) -> Optional[TradeSignal]:
        """
        While price is inside the 5min zone, look for 1min breakouts.
        TP = nearest 5min level (POC/VAH/VAL) in breakout direction.
        """
        if not self._big_zone:
            return None

        # Feed 1m detector
        event = self.detector_1m.update(candle_1m)
        active_1m = self.detector_1m.get_active_zone()

        # Track 1m zones for frontend
        if event and event.event_type == "active":
            z = event.zone
            z.timeframe = "1m"
            z.parent_zone_id = self._big_zone.zone_id
            self._all_1m_zones.append(z)

        # ── If watching a 1m breakout, track confirmation ──
        if self._1m_watching and self._1m_ref_zone:
            return self._track_1m_breakout(candle_1m)

        # ── If a 1m zone just became LEFT, start watching ──
        if event and event.event_type == "left":
            zone_left = event.zone
            self._1m_ref_zone = zone_left
            self._1m_exit_dir = zone_left.exit_direction
            self._1m_outside_candles = [candle_1m]
            self._1m_watching = True

            logger.info(
                f"[MTF] 1min zone {zone_left.zone_id} LEFT "
                f"dir={self._1m_exit_dir}, tracking..."
            )
            return None

        return None

    def _track_1m_breakout(self, candle_1m: Candle) -> Optional[TradeSignal]:
        """Track 1m breakout confirmation."""
        zone = self._1m_ref_zone
        if not zone or not self._1m_exit_dir:
            self._1m_watching = False
            return None

        # Check if still outside
        range_100 = zone.high_100 - zone.low_100
        if range_100 <= 0:
            self._1m_watching = False
            return None

        still_outside = False
        if self._1m_exit_dir == "up" and candle_1m.close > zone.high_100:
            still_outside = True
        elif self._1m_exit_dir == "down" and candle_1m.close < zone.low_100:
            still_outside = True

        if not still_outside:
            # Came back → cancel
            self._1m_watching = False
            self._1m_outside_candles = []
            return None

        self._1m_outside_candles.append(candle_1m)

        if len(self._1m_outside_candles) < self.confirm_candles_1m:
            return None

        # ── Confirmed 1m breakout ──
        # Find nearest 5m target
        target = self._find_nearest_5m_target(candle_1m.close, self._1m_exit_dir)
        if target is None:
            logger.info("[MTF] 1min breakout: no valid 5m target")
            self._1m_watching = False
            self._1m_outside_candles = []
            return None

        logger.info(
            f"[MTF] 1m SIGNAL PREP: entry={candle_1m.close:.2f} target_5m={target:.2f} "
            f"dir={self._1m_exit_dir} | big_zone POC={self._big_zone.poc:.2f} "
            f"VAH={self._big_zone.vah_80:.2f} VAL={self._big_zone.val_80:.2f}"
        )

        sl = self.sl_points_1m
        sl_price_offset = sl

        if self._1m_exit_dir == "up":
            # Buy breakout upward
            entry = candle_1m.close
            sl_price = entry - sl_price_offset
            tp_price = target
            direction = Direction.BUY

            # Check R:R
            reward = tp_price - entry
            risk = entry - sl_price
            if risk <= 0 or reward / risk < self.min_rr:
                logger.info(f"[MTF] 1min breakout BUY: R:R too low ({reward:.1f}/{risk:.1f})")
                self._1m_watching = False
                self._1m_outside_candles = []
                return None

        else:
            # Sell breakout downward
            entry = candle_1m.close
            sl_price = entry + sl_price_offset
            tp_price = target
            direction = Direction.SELL

            reward = entry - tp_price
            risk = sl_price - entry
            if risk <= 0 or reward / risk < self.min_rr:
                logger.info(f"[MTF] 1min breakout SELL: R:R too low ({reward:.1f}/{risk:.1f})")
                self._1m_watching = False
                self._1m_outside_candles = []
                return None

        self._state = "IN_1M_TRADE"
        self._1m_watching = False
        self._1m_outside_candles = []

        sl_dollars = sl * POINT_VALUE
        tp_dollars = abs(target - entry) * POINT_VALUE

        logger.info(
            f"[MTF] 1min BREAKOUT {self._1m_exit_dir} | "
            f"entry={entry:.2f} target(5m)={target:.2f} | "
            f"SL ${sl_dollars:.0f} TP ${tp_dollars:.0f}"
        )

        return TradeSignal(
            strategy=StrategyType.MTF_1M_TREND,
            direction=direction,
            entry_price=entry,
            sl_price=sl_price,
            tp_price=tp_price,
            zone_id=zone.zone_id,
            reason=(
                f"MTF 1M BREAKOUT {direction.value.upper()} | "
                f"target=5m {'POC' if abs(target - self._big_zone.poc) < 1 else 'VAH/VAL'} "
                f"{target:.2f} | SL ${sl_dollars:.0f} TP ${tp_dollars:.0f}"
            ),
            timestamp=candle_1m.timestamp,
        )

    def _find_nearest_5m_target(
        self, price: float, direction: str
    ) -> Optional[float]:
        """
        Find the nearest 5min level (POC/VAH/VAL) in the breakout direction.

        For UP breakout: first target above price (POC → VAH → high_100)
        For DOWN breakout: first target below price (POC → VAL → low_100)
        """
        if not self._big_zone:
            return None

        z = self._big_zone
        levels = [z.poc, z.vah_80, z.val_80]

        if direction == "up":
            # Targets above current price, sorted by proximity (nearest first)
            above = sorted([l for l in levels if l > price + 1.0])
            return above[0] if above else None
        else:
            # Targets below current price
            below = sorted([l for l in levels if l < price - 1.0], reverse=True)
            return below[0] if below else None

    # ── Notifications from engine ────────────────────

    def notify_trade_closed(self, exit_reason: str):
        """Called when a trade closes (SL/TP/flatten)."""
        if self._state == "IN_1M_TRADE":
            # 1m trade closed → back to trading inside big zone
            self._state = "INSIDE_BIG"
            self._1m_watching = False
            self._1m_outside_candles = []
            logger.info(f"[MTF] 1m trade closed ({exit_reason}) → INSIDE_BIG")

        elif self._state in ("BIG_BREAKOUT", "IN_5M_TRADE"):
            # 5m trade closed → wait for new zone
            self._state = "WAITING_BIG"
            self._big_zone = None
            self._5m_exit_dir = None
            self._5m_outside_candles = []
            logger.info(f"[MTF] 5m trade closed ({exit_reason}) → WAITING_BIG")

    def notify_order_cancelled(self):
        """Called when a pending order is cancelled."""
        if self._state == "BIG_BREAKOUT":
            self._state = "WAITING_BIG"
            self._5m_exit_dir = None
            self._5m_outside_candles = []
        elif self._state == "IN_1M_TRADE":
            self._state = "INSIDE_BIG"
        self._1m_watching = False
        self._1m_outside_candles = []

    def notify_order_filled(self):
        """Called when a pending order fills."""
        if self._state == "BIG_BREAKOUT":
            self._state = "IN_5M_TRADE"
        # IN_1M_TRADE stays as is

    # ── Query methods ────────────────────────────────

    @property
    def raw_state(self) -> str:
        return self._state

    def get_phase_label(self) -> str:
        labels = {
            "WAITING_BIG": "等待5min盤整",
            "INSIDE_BIG": "5min區間內",
            "IN_1M_TRADE": "1min交易中",
            "BIG_BREAKOUT": "5min出界",
            "IN_5M_TRADE": "5min交易中",
        }
        extra = ""
        if self._state == "INSIDE_BIG" and self._1m_watching:
            n = len(self._1m_outside_candles)
            extra = f" | 1m出界({n}bar)"
        if self._state == "INSIDE_BIG" and self._5m_exit_dir:
            n = len(self._5m_outside_candles)
            extra = f" | 5m出界({n}bar)"
        return labels.get(self._state, self._state) + extra

    def get_big_zone(self) -> Optional[ConsolidationZone]:
        return self._big_zone

    def get_all_5m_zones(self) -> List[ConsolidationZone]:
        return self.detector_5m.get_all_zones()

    def get_all_1m_zones(self) -> List[ConsolidationZone]:
        zones = self.detector_1m.get_all_zones()
        # Tag as 1m
        for z in zones:
            z.timeframe = "1m"
            if self._big_zone:
                z.parent_zone_id = self._big_zone.zone_id
        return zones

    def get_all_zones(self) -> List[ConsolidationZone]:
        """All zones (5m + 1m) for frontend display."""
        zones_5m = self.get_all_5m_zones()
        for z in zones_5m:
            z.timeframe = "5m"
        zones_1m = self.get_all_1m_zones()
        return zones_5m + zones_1m

    # ── Internal helpers ─────────────────────────────

    @staticmethod
    def _aggregate_to_5m(candles_1m: List[Candle]) -> Candle:
        """Aggregate 5 one-minute candles into a single 5-min candle."""
        return Candle(
            timestamp=candles_1m[0].timestamp,
            open=candles_1m[0].open,
            high=max(c.high for c in candles_1m),
            low=min(c.low for c in candles_1m),
            close=candles_1m[-1].close,
            volume=sum(c.volume for c in candles_1m),
            symbol=candles_1m[0].symbol,
            interval="5m",
        )
