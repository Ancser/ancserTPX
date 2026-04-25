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
      2. **觸發 = 假突破 pattern**（兩根相鄰 K 線，立即進場，不再等 5 根）：
            上沿: prev.high > VAH 且 current.close < VAH → SELL LIMIT @ VAH
            下沿: prev.low  < VAL 且 current.close > VAL → BUY  LIMIT @ VAL
      3. SL: VAH/VAL 外側 ± sl_ticks × tick_size  (區間破位即停損)
      4. TP: entry ± tp_ticks × tick_size
      5. **掛單超時延長機制**：訂單被引擎超時取消時，
            檢查最近 5 根 close 是否仍在正確側
              SELL @ VAH : 全 5 根 close < VAH → re-arm 下一根重新掛
              BUY  @ VAL : 全 5 根 close > VAL → re-arm 下一根重新掛
            否則 → 真的取消、回 idle
      6. 新 zone 進來時清除待掛狀態

    對比 Trend：
      Trend     —  5 根 close 出界 → 順勢突破做多/做空
      Reversion —  假突破一次 + 5 根 close 範圍內 → 反向 fade
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

        self._state = "idle"  # idle | confirmed | in_trade | rearm
        self._fade_direction: Optional[str] = None  # "up" = fade VAH, "down" = fade VAL
        self._ref_zone: Optional[ConsolidationZone] = None
        self._recent_candles: List[Candle] = []

        # ── Trigger pattern state (prev/current pair) ──
        self._prev_candle: Optional[Candle] = None
        self._tracked_zone_id: Optional[str] = None

    def reset(self):
        self._state = "idle"
        self._fade_direction = None
        self._ref_zone = None
        self._recent_candles = []
        self._prev_candle = None
        self._tracked_zone_id = None

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
        每根 1m K 線調用一次。
        即時觸發：偵測到假突破 pattern 立即返回 signal，不再等 5 根。
        Re-arm: 若上一筆掛單超時被取消但條件仍合格 → 下一根再次返回 signal。
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

        # ── Reset state when zone changes ──
        if zone.zone_id != self._tracked_zone_id:
            self._tracked_zone_id = zone.zone_id
            self._prev_candle = None
            self._fade_direction = None
            self._ref_zone = None
            if self._state == "rearm":
                self._state = "idle"

        # ── Re-arm path: timeout extended; re-fire same direction ──
        if self._state == "rearm" and self._fade_direction is not None:
            return self._generate_signal(candle, zone, self._fade_direction)

        # ── Detect 2-bar fakeout pattern ──
        prev = self._prev_candle
        self._prev_candle = candle
        if prev is None:
            return None

        vah = zone.vah_80
        val = zone.val_80

        # Up-side fakeout → SELL @ VAH
        if prev.high > vah and candle.close < vah:
            self._fade_direction = "up"
            self._ref_zone = zone
            return self._generate_signal(candle, zone, "up")

        # Down-side fakeout → BUY @ VAL
        if prev.low < val and candle.close > val:
            self._fade_direction = "down"
            self._ref_zone = zone
            return self._generate_signal(candle, zone, "down")

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
        signal = TradeSignal(
            strategy=StrategyType.REVERSION,
            direction=trade_dir,
            entry_price=entry,
            sl_price=sl,
            tp_price=tp,
            zone_id=zone.zone_id,
            reason=(
                f"SESSION REV {edge_label} | "
                f"fakeout pattern | "
                f"SL {self.SL_TICKS}t(${sl_dollars:.0f}) TP {self.TP_TICKS}t(${tp_dollars:.0f})"
            ),
            timestamp=candle.timestamp,
        )
        # State → confirmed (engine will treat as pending-order armed)
        self._state = "confirmed"
        return signal

    def notify_trade_closed(self, exit_reason: str):
        self._state = "idle"
        self._fade_direction = None

    def notify_order_cancelled(self):
        """
        Engine cancelled the pending limit (timeout / flatten).
        Extension rule: if last N closes still favour the fade direction,
        flip to "rearm" so next evaluate re-fires the same order.
        Otherwise reset to idle.
        """
        direction = self._fade_direction
        if direction is None or self._ref_zone is None:
            self._state = "idle"
            self._fade_direction = None
            return

        n = self.BREAKOUT_CONFIRM_CANDLES   # check last 5 candles
        recent = self._recent_candles[-n:] if len(self._recent_candles) >= n else []

        keep = False
        if len(recent) >= n:
            if direction == "up":
                # SELL @ VAH; need all last 5 closes < VAH
                keep = all(c.close < self._ref_zone.vah_80 for c in recent)
            else:
                # BUY @ VAL; need all last 5 closes > VAL
                keep = all(c.close > self._ref_zone.val_80 for c in recent)

        if keep:
            self._state = "rearm"
            side = "<VAH" if direction == "up" else ">VAL"
            logger.info(
                f"[SessionReversion] Timeout — last {n} closes still {side}, re-arm next bar"
            )
        else:
            self._state = "idle"
            self._fade_direction = None
            logger.info(f"[SessionReversion] Timeout — condition broken, abort")

    def get_phase_label(self) -> str:
        if self._state == "in_trade":
            return "持倉中"
        if self._state == "confirmed":
            return "掛單中"
        if self._state == "rearm":
            side = "VAH" if self._fade_direction == "up" else "VAL"
            return f"超時延長·待重掛@{side}"
        return "等待假突破"

    @property
    def raw_state(self) -> str:
        return self._state


# ══════════════════════════════════════════════════════════
# Combined Trend + Reversion (parallel sub-strategies)
# ══════════════════════════════════════════════════════════

class SessionTrendReversion:
    """
    Combined wrapper — runs SessionTrendFollow and SessionReversion in parallel.

    規則:
      - 引擎本身一倉一單；evaluate 只在閒置時被呼叫
      - 每根 K 線同時餵兩個子策略 (避免 lookback 不同步)
      - Trend 優先：先評估 trend；trend 沒 signal 才評估 reversion
      - Active sub 記錄哪個子策略當前持單；notify_* 只轉發給該子
      - reset / warmup 同時作用於兩子
    """

    BREAKOUT_CONFIRM_CANDLES = 5
    TICK_SIZE = 0.25

    def __init__(self, params: Optional[StrategyParams] = None):
        # Lazy import to avoid circular: trend_follow imports reversion not, but be safe
        from backend.strategy.trend_follow import SessionTrendFollow
        self.trend = SessionTrendFollow(params)
        self.rev = SessionReversion(params)
        # Engine reads PENDING_TIMEOUT_CANDLES from the strategy — both subs use same value
        self.PENDING_TIMEOUT_CANDLES = self.trend.PENDING_TIMEOUT_CANDLES
        self._active_sub: Optional[str] = None  # "trend" | "rev" | None

    def reset(self):
        self.trend.reset()
        self.rev.reset()
        self._active_sub = None

    def reset_state_only(self):
        if hasattr(self.trend, 'reset_state_only'):
            self.trend.reset_state_only()
        else:
            self.trend.reset()
        self.rev.reset_state_only()
        self._active_sub = None

    def warmup(self, candle: Candle):
        self.trend.warmup(candle)
        self.rev.warmup(candle)

    def evaluate(
        self,
        candle: Candle,
        zone: Optional[ConsolidationZone],
        is_mature: bool,
    ) -> Optional[TradeSignal]:
        # Trend 優先評估
        signal = self.trend.evaluate(candle, zone, is_mature)
        if signal:
            self._active_sub = "trend"
            return signal

        # Reversion 補位
        signal = self.rev.evaluate(candle, zone, is_mature)
        if signal:
            self._active_sub = "rev"
            return signal

        return None

    def notify_trade_closed(self, exit_reason: str):
        if self._active_sub == "trend":
            self.trend.notify_trade_closed(exit_reason)
        elif self._active_sub == "rev":
            self.rev.notify_trade_closed(exit_reason)
        self._active_sub = None

    def notify_order_cancelled(self):
        if self._active_sub == "trend":
            self.trend.notify_order_cancelled()
        elif self._active_sub == "rev":
            self.rev.notify_order_cancelled()
        self._active_sub = None

    def get_phase_label(self) -> str:
        if self._active_sub == "trend":
            return f"[T] {self.trend.get_phase_label()}"
        if self._active_sub == "rev":
            return f"[R] {self.rev.get_phase_label()}"
        # Both idle — surface compact dual state
        t = self.trend.get_phase_label()
        r = self.rev.get_phase_label()
        return f"T:{t} | R:{r}"

    @property
    def raw_state(self) -> str:
        if self._active_sub == "trend":
            return self.trend.raw_state
        if self._active_sub == "rev":
            return self.rev.raw_state
        # Both idle
        return "idle"
