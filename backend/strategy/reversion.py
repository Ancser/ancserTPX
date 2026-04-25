# ============================================================
# 文件: backend/strategy/reversion.py
# 狀態: 已更新 v2
# 規則:
#   1. Zone 必須有至少 10 根 K 線 (10 分鐘形成)
#   2. Sell limit at top 90% of 100% range
#   3. Buy limit at bottom 10% of 100% range
#   4. SL: $300 (15 NQ points), TP: $900 (45 NQ points)
#   5. R:R = 1:3
# ============================================================
"""
策略一：均值回歸 (Reversion)

新規則：
  1. 新盤整區間已形成 + 至少 10 根 K 線
  2. Sell limit at 90th percentile of range (top 90%)
  3. Buy limit at 10th percentile of range (bottom 10%)
  4. SL = $300 (15 pt), TP = $900 (45 pt)
  5. 固定風報比 1:3
"""

import logging
from typing import List, Optional
from backend.db.models import (
    Candle, ConsolidationZone, TradeSignal, StrategyParams,
    Direction, StrategyType, ZoneStatus
)

logger = logging.getLogger(__name__)
POINT_VALUE = 20.0


class ReversionStrategy:

    def __init__(
        self,
        sl_points: float = 15.0,       # $300
        tp_points: float = 45.0,       # $900
        min_zone_candles: int = 10,    # zone 至少 10 根 K 線
        entry_pct_high: float = 0.90,  # 90th percentile = sell
        entry_pct_low: float = 0.10,   # 10th percentile = buy
        touch_tolerance: float = 3.0,  # 觸及容差 (NQ 3 pts)
        point_value: float = 20.0,
        contracts: int = 1,
    ):
        self.sl_points = sl_points
        self.tp_points = tp_points
        self.min_zone_candles = min_zone_candles
        self.entry_pct_high = entry_pct_high
        self.entry_pct_low = entry_pct_low
        self.touch_tolerance = touch_tolerance
        self.point_value = point_value
        self.contracts = contracts

    def evaluate(
        self,
        candle: Candle,
        active_zone: Optional[ConsolidationZone],
    ) -> Optional[TradeSignal]:
        """
        評估當前 K 線是否觸發均值回歸入場

        Returns:
            TradeSignal or None
        """
        if not active_zone or active_zone.status != ZoneStatus.ACTIVE:
            return None

        # 條件: Zone 至少 10 根 K 線形成
        if active_zone.num_candles < self.min_zone_candles:
            return None

        # 計算入場水平
        range_100 = active_zone.high_100 - active_zone.low_100
        if range_100 <= 0:
            return None

        sell_level = active_zone.low_100 + self.entry_pct_high * range_100  # top 90%
        buy_level = active_zone.low_100 + self.entry_pct_low * range_100    # bottom 10%

        # === 檢查 top 90% 觸及 → 做空 (sell limit) ===
        if self._check_touch_high(candle.high, sell_level):
            entry = sell_level
            sl = entry + self.sl_points
            tp = entry - self.tp_points

            return TradeSignal(
                strategy=StrategyType.REVERSION,
                direction=Direction.SELL,
                entry_price=entry,
                sl_price=sl,
                tp_price=tp,
                zone_id=active_zone.zone_id,
                reason=f"REV SELL @ top 90% {entry:.2f} | SL ${self.sl_points * self.point_value:.0f} TP ${self.tp_points * self.point_value:.0f}",
                timestamp=candle.timestamp,
            )

        # === 檢查 bottom 10% 觸及 → 做多 (buy limit) ===
        if self._check_touch_low(candle.low, buy_level):
            entry = buy_level
            sl = entry - self.sl_points
            tp = entry + self.tp_points

            return TradeSignal(
                strategy=StrategyType.REVERSION,
                direction=Direction.BUY,
                entry_price=entry,
                sl_price=sl,
                tp_price=tp,
                zone_id=active_zone.zone_id,
                reason=f"REV BUY @ bottom 10% {entry:.2f} | SL ${self.sl_points * self.point_value:.0f} TP ${self.tp_points * self.point_value:.0f}",
                timestamp=candle.timestamp,
            )

        return None

    def _check_touch_high(self, price_high: float, level: float) -> bool:
        """K 線高點是否觸及或超過 sell level"""
        return price_high >= level - self.touch_tolerance

    def _check_touch_low(self, price_low: float, level: float) -> bool:
        """K 線低點是否觸及或低於 buy level"""
        return price_low <= level + self.touch_tolerance


# ══════════════════════════════════════════════════════════
# Session-based Reversion (mirror of SessionTrendFollow)
# ══════════════════════════════════════════════════════════

class SessionReversion:
    """
    Session-based mean-reversion strategy (與 SessionTrendFollow 同基礎，方向相反).

    規則:
      1. 等待 SessionZoneDetector 報告區間成熟
      2. 連續 5 根 1m close 都在 [VAL, VAH] 範圍內 → 確認區間有效
      3. 第 5 根 close 偏向 VAH (≥ POC) → SELL LIMIT @ VAH (fade 上沿)
         第 5 根 close 偏向 VAL (< POC) → BUY  LIMIT @ VAL (fade 下沿)
      4. SL: VAH/VAL 外側 ± sl_ticks × tick_size  (區間破位即停損)
      5. TP: entry ± tp_ticks × tick_size         (與 Trend 同)
      6. 掛單超時取消 (entry_timeout)

    對比 Trend：
      Trend     —  5 根 close 出界 → 順勢突破做多/做空
      Reversion —  5 根 close 範圍內 → 反向掛單做空/做多 (賭區間繼續守住)
    """

    BREAKOUT_CONFIRM_CANDLES = 5   # 連續 5 根 close 在 [VAL, VAH] 內
    TICK_SIZE = 0.25

    def __init__(self, params: Optional[StrategyParams] = None):
        p = params or StrategyParams()
        self.SL_TICKS = p.sl_ticks
        self.TP_TICKS = p.tp_ticks
        _candle_secs = getattr(p, 'candle_seconds', 30)
        _cpm = max(1, 60 // _candle_secs)
        self.PENDING_TIMEOUT_CANDLES = 5 * _cpm   # 5 min hardcoded

        self._state = "idle"  # idle | watching | confirmed | in_trade
        self._consecutive_inside: int = 0
        self._fade_direction: Optional[str] = None  # "up" = fade VAH, "down" = fade VAL
        self._ref_zone: Optional[ConsolidationZone] = None
        self._recent_candles: List[Candle] = []

    def reset(self):
        self._state = "idle"
        self._consecutive_inside = 0
        self._fade_direction = None
        self._ref_zone = None
        self._recent_candles = []

    def reset_state_only(self):
        """Alias for reset() — keeps interface compatible with MACDOnlyStrategy."""
        self.reset()

    def warmup(self, candle: Candle):
        """Feed candle during warm-up without generating signals (no-op)."""
        pass

    def evaluate(
        self,
        candle: Candle,
        zone: Optional[ConsolidationZone],
        is_mature: bool,
    ) -> Optional[TradeSignal]:
        """
        每根 1m K 線調用一次.
        Lookback: 看最近 5 根 close 是否都在 [VAL, VAH] 內.
        """
        self._recent_candles.append(candle)
        if len(self._recent_candles) > 20:
            self._recent_candles = self._recent_candles[-20:]

        if not zone or not is_mature:
            return None

        # No operation on AH (After Hours 20:00 - 22:00 UTC)
        h = candle.timestamp.hour
        if 20 <= h < 22:
            return None

        if self._state in ("confirmed", "in_trade"):
            return None

        vah = zone.vah_80
        val = zone.val_80
        poc = zone.poc

        n = self.BREAKOUT_CONFIRM_CANDLES
        recent = self._recent_candles[-n:] if len(self._recent_candles) >= n else self._recent_candles

        # Count consecutive inside-range from the END (most recent)
        inside_count = 0
        for c in reversed(recent):
            if val <= c.close <= vah:
                inside_count += 1
            else:
                break

        self._consecutive_inside = inside_count

        if inside_count == 0:
            self._state = "idle"
            self._fade_direction = None
            return None

        self._state = "watching"

        # ── 5 consecutive inside → confirmed range, place fade order ──
        if inside_count >= n:
            # Fade direction: latest close above POC → fade VAH (sell), else fade VAL (buy)
            self._fade_direction = "up" if candle.close >= poc else "down"
            self._state = "confirmed"
            self._ref_zone = zone
            return self._generate_signal(candle, zone, self._fade_direction)

        return None

    def _generate_signal(
        self,
        candle: Candle,
        zone: ConsolidationZone,
        direction: str,
    ) -> TradeSignal:
        """
        Entry = VAH (fade up) or VAL (fade down)
        SL    = beyond the VA edge by sl_ticks (range break = lose)
        TP    = entry ± tp_ticks (toward POC)
        """
        sl_points = self.SL_TICKS * self.TICK_SIZE
        tp_points = self.TP_TICKS * self.TICK_SIZE

        if direction == "up":
            # Fade VAH: SELL limit at VAH, SL above range, TP toward POC
            entry = zone.vah_80
            sl = entry + sl_points
            tp = entry - tp_points
            trade_dir = Direction.SELL
        else:
            # Fade VAL: BUY limit at VAL, SL below range, TP toward POC
            entry = zone.val_80
            sl = entry - sl_points
            tp = entry + tp_points
            trade_dir = Direction.BUY

        sl_dollars = abs(entry - sl) * POINT_VALUE
        tp_dollars = abs(tp - entry) * POINT_VALUE

        logger.info(
            f"[SessionReversion] FADE {direction.upper()} confirmed | "
            f"entry={entry:.2f} SL={sl:.2f} TP={tp:.2f} | "
            f"SL ${sl_dollars:.0f} TP ${tp_dollars:.0f} | "
            f"zone={zone.zone_id}"
        )

        edge_label = "SELL@VAH" if direction == "up" else "BUY@VAL"
        return TradeSignal(
            strategy=StrategyType.REVERSION,
            direction=trade_dir,
            entry_price=entry,
            sl_price=sl,
            tp_price=tp,
            zone_id=zone.zone_id,
            reason=(
                f"SESSION REV {edge_label} | "
                f"5-bar inside-range fade | "
                f"SL {self.SL_TICKS}t(${sl_dollars:.0f}) TP {self.TP_TICKS}t(${tp_dollars:.0f})"
            ),
            timestamp=candle.timestamp,
        )

    def notify_trade_closed(self, exit_reason: str):
        self._state = "idle"
        self._consecutive_inside = 0
        self._fade_direction = None

    def notify_order_cancelled(self):
        """Order cancelled (timeout/flatten). Keep counter — re-check next bar."""
        if self._state == "confirmed":
            logger.info(
                f"[SessionReversion] Order cancelled → watching "
                f"(keep count={self._consecutive_inside}, dir={self._fade_direction})"
            )
        self._state = "watching"

    def get_phase_label(self) -> str:
        if self._state == "idle":
            return "等待範圍"
        elif self._state == "watching":
            return f"範圍內({self._consecutive_inside}/{self.BREAKOUT_CONFIRM_CANDLES} bar)"
        elif self._state == "confirmed":
            return "入場準備"
        elif self._state == "in_trade":
            return "持倉中"
        return self._state

    @property
    def raw_state(self) -> str:
        return self._state
