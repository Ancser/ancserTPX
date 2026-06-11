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
        self.SL_TICKS = getattr(p, "cd_sl_ticks", p.sl_ticks)
        self.TP_TICKS = getattr(p, "cd_tp_ticks", p.tp_ticks)
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
        """Alias for reset() — keeps the live warm-up interface consistent across strategies."""
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

class SessionConsolidation:
    """
    盤整回歸策略: 同時在 VAH 掛做空 + VAL 掛做多，哪個先觸及就成交。

    規則:
      1. Zone 成熟後，K 線 high 觸及 VAH → SELL @ VAH (market fill)
      2. K 線 low 觸及 VAL → BUY @ VAL (market fill)
      3. 兩邊同時觸及 → open 距哪邊近先成交
    """

    TICK_SIZE = 0.25

    def __init__(self, params: Optional[StrategyParams] = None):
        p = params or StrategyParams()
        self.SL_TICKS = getattr(p, "cd_sl_ticks", p.sl_ticks)
        self.TP_TICKS = getattr(p, "cd_tp_ticks", p.tp_ticks)
        self.PENDING_TIMEOUT_CANDLES = 1
        self.CONFIRM_BARS = max(1, int(getattr(p, 'breakout_confirm_bars', 1) or 1))

        self._state = "idle"
        self._tracked_zone_id: Optional[str] = None
        self._inside_count: int = 0

    def reset(self):
        self._state = "idle"
        self._tracked_zone_id = None
        self._inside_count = 0

    def reset_state_only(self):
        self.reset()

    def warmup(self, candle: Candle):
        pass

    def evaluate(
        self,
        candle: Candle,
        zone: Optional[ConsolidationZone],
        is_mature: bool,
    ) -> Optional[TradeSignal]:
        if not zone or not is_mature:
            return None

        if zone.zone_id != self._tracked_zone_id:
            self._tracked_zone_id = zone.zone_id
            self._state = "idle"
            self._inside_count = 0

        if self._state == "in_trade":
            return None

        vah = zone.vah_80
        val = zone.val_80

        inside = val <= candle.open <= vah and val <= candle.close <= vah
        if inside:
            self._inside_count += 1
        else:
            self._inside_count = 0

        if self._inside_count < self.CONFIRM_BARS:
            return None

        hit_vah = candle.high >= vah
        hit_val = candle.low <= val

        if hit_vah and hit_val:
            if abs(candle.open - vah) <= abs(candle.open - val):
                return self._make_signal(candle, zone, "sell")
            else:
                return self._make_signal(candle, zone, "buy")
        elif hit_vah:
            return self._make_signal(candle, zone, "sell")
        elif hit_val:
            return self._make_signal(candle, zone, "buy")
        return None

    def _make_signal(self, candle: Candle, zone: ConsolidationZone, side: str) -> TradeSignal:
        sl_points = self.SL_TICKS * self.TICK_SIZE
        tp_points = self.TP_TICKS * self.TICK_SIZE

        if side == "sell":
            entry = zone.vah_80
            sl = entry + sl_points
            tp = entry - tp_points
            trade_dir = Direction.SELL
            label = "SELL@VAH"
        else:
            entry = zone.val_80
            sl = entry - sl_points
            tp = entry + tp_points
            trade_dir = Direction.BUY
            label = "BUY@VAL"

        sl_dollars = abs(entry - sl) * POINT_VALUE
        tp_dollars = abs(tp - entry) * POINT_VALUE
        self._state = "in_trade"

        logger.info(
            f"[Consolidation] {label} | entry={entry:.2f} SL={sl:.2f} TP={tp:.2f} | zone={zone.zone_id}"
        )
        return TradeSignal(
            strategy=StrategyType.CONSOLIDATION,
            direction=trade_dir,
            entry_price=entry,
            sl_price=sl,
            tp_price=tp,
            zone_id=zone.zone_id,
            reason=(
                f"CONSOLIDATION {label} | "
                f"SL {self.SL_TICKS}t(${sl_dollars:.0f}) TP {self.TP_TICKS}t(${tp_dollars:.0f})"
            ),
            timestamp=candle.timestamp,
            order_type="market",
        )

    def notify_trade_closed(self, exit_reason: str):
        self._state = "idle"

    def notify_order_cancelled(self):
        self._state = "idle"

    def get_phase_label(self) -> str:
        if self._state == "in_trade":
            return "持倉中"
        return "VAH做空|VAL做多"

    @property
    def raw_state(self) -> str:
        return self._state


class SessionHybridStrategy:
    """
    Hybrid 模式: 根據 K 線位置自動切換 breakthrough / consolidation。

    模式切換:
      - open+close 都在 VAH 上或 VAL 下 → breakthrough
      - open+close 都在 VA 內 → consolidation
      - 跨界 K 線 → 維持當前模式
    """

    TICK_SIZE = 0.25

    def __init__(self, params: Optional[StrategyParams] = None):
        from backend.strategy.trend_follow import SessionTrendFollow

        self.breakthrough = SessionTrendFollow(params)
        self.consolidation = SessionConsolidation(params)
        self.PENDING_TIMEOUT_CANDLES = 1
        self._mode = "consolidation"
        self._active_sub: Optional[str] = None

    def reset(self):
        self.breakthrough.reset()
        self.consolidation.reset()
        self._mode = "consolidation"
        self._active_sub = None

    def reset_state_only(self):
        self.breakthrough.reset_state_only()
        self.consolidation.reset_state_only()
        self._mode = "consolidation"
        self._active_sub = None

    def warmup(self, candle: Candle):
        self.breakthrough.warmup(candle)

    def set_traded_breakouts(self, keys):
        if hasattr(self.breakthrough, "set_traded_breakouts"):
            self.breakthrough.set_traded_breakouts(keys)

    def mark_breakout_used(self, zone_id: str, direction: str):
        if hasattr(self.breakthrough, "mark_breakout_used"):
            self.breakthrough.mark_breakout_used(zone_id, direction)

    def unlock_breakout(self, zone_id: str, direction: str):
        if hasattr(self.breakthrough, "unlock_breakout"):
            self.breakthrough.unlock_breakout(zone_id, direction)

    @property
    def active_mode(self) -> str:
        return self._mode

    def evaluate(
        self,
        candle: Candle,
        zone: Optional[ConsolidationZone],
        is_mature: bool,
    ) -> Optional[TradeSignal]:
        if not zone or not is_mature:
            return None

        vah = zone.vah_80
        val = zone.val_80
        inside = val <= candle.open <= vah and val <= candle.close <= vah
        up = candle.open > vah and candle.close > vah
        down = candle.open < val and candle.close < val

        if up or down:
            self._mode = "breakthrough"
        elif inside:
            self._mode = "consolidation"

        if self._mode == "breakthrough":
            signal = self.breakthrough.evaluate(candle, zone, is_mature)
            if signal:
                self._active_sub = "breakthrough"
                return signal
        else:
            signal = self.consolidation.evaluate(candle, zone, is_mature)
            if signal:
                self._active_sub = "consolidation"
                return signal

        return None

    def notify_trade_closed(self, exit_reason: str):
        if self._active_sub == "breakthrough":
            self.breakthrough.notify_trade_closed(exit_reason)
        elif self._active_sub == "consolidation":
            self.consolidation.notify_trade_closed(exit_reason)
        self._active_sub = None

    def notify_order_cancelled(self):
        if self._active_sub == "breakthrough":
            self.breakthrough.notify_order_cancelled()
        elif self._active_sub == "consolidation":
            self.consolidation.notify_order_cancelled()
        self._active_sub = None

    def get_phase_label(self) -> str:
        mode_label = "突破" if self._mode == "breakthrough" else "盤整"
        if self._active_sub == "breakthrough":
            return f"[{mode_label}] {self.breakthrough.get_phase_label()}"
        if self._active_sub == "consolidation":
            return f"[{mode_label}] {self.consolidation.get_phase_label()}"
        if self._mode == "breakthrough":
            return f"[突破] {self.breakthrough.get_phase_label()}"
        return f"[盤整] {self.consolidation.get_phase_label()}"

    @property
    def raw_state(self) -> str:
        if self._active_sub == "breakthrough":
            return self.breakthrough.raw_state
        if self._active_sub == "consolidation":
            return self.consolidation.raw_state
        return "idle"
