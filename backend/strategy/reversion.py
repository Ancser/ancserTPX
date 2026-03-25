# ============================================================
# 文件: backend/strategy/reversion.py
# 狀態: 已更新 v2
# 規則:
#   1. Zone 必須有至少 10 根 K 線 (50 分鐘形成)
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

from typing import Optional
from backend.db.models import (
    Candle, ConsolidationZone, TradeSignal,
    Direction, StrategyType, ZoneStatus
)


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
