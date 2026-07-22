# ============================================================
# 文件: backend/strategy/trend_follow.py
# 狀態: v1.0.6 (session-based breakout — the live/backtest trend strategy)
# 規則 (SessionTrendFollow):
#   1. 等待 SessionZoneDetector 報告區間成熟
#   2. 突破: 單根 K 線 open AND close 都在 VAH 上方 (up) 或 VAL 下方 (down)
#   3. 掛 BUY LIMIT @ VAH (up) 或 SELL LIMIT @ VAL (down)
#   4. 每根 K 線刷新掛單; open+close 都回到 VA 內 → 突破失敗，回 idle
# 關聯文件:
#   → backend/live/engine.py     (SessionTrendFollow live path)
#   → backend/backtest/engine.py (SessionTrendFollow backtest path)
# ============================================================
"""策略二：趨勢跟隨 (Session-based Trend Follow)。

The single trend strategy used by both the live engine and the backtester. See
the SessionTrendFollow docstring for the state machine.
"""

from __future__ import annotations
import logging
from typing import Any, Dict, List, Optional, Set, Tuple
from backend.db.models import (
    Candle, ConsolidationZone, TradeSignal, StrategyParams,
    Direction, StrategyType,
)

logger = logging.getLogger(__name__)

POINT_VALUE = 20.0


# ══════════════════════════════════════════════════════════
# Session-based Trend Follow (for live overnight trading)
# ══════════════════════════════════════════════════════════

class SessionTrendFollow:
    """
    Session-based trend follow strategy.

    規則:
      1. 等待 SessionZoneDetector 報告區間成熟
      2. 突破: 單根 K 線 open AND close 都在 VAH 上方 (up) 或 VAL 下方 (down)
      3. 掛 BUY LIMIT @ VAH (up) 或 SELL LIMIT @ VAL (down)
      4. 每根 K 線刷新掛單 (引擎 silent cancel → 策略重新出信號)
      5. open+close 都回到 VA 內 → 突破失敗，回 idle
    """

    TICK_SIZE = 0.25
    MIN_STOP_TICKS = 4

    def __init__(self, params: Optional[StrategyParams] = None):
        p = params or StrategyParams()
        self.RR_RATIO = max(1, int(getattr(p, "rr_ratio", 2) or 2))
        # Fallback SL distance (ticks) when a zone has no VP histogram to locate a node.
        self.SL_TICKS = getattr(p, "tr_sl_ticks", p.sl_ticks)
        self.PENDING_TIMEOUT_CANDLES = 1
        self.CONFIRM_BARS = max(1, int(getattr(p, 'breakout_confirm_bars', 1) or 1))
        self.area_timeframe = str(getattr(p, "area_timeframe", "5m") or "5m")
        self.method = str(getattr(p, "method", "single") or "single").lower()
        self.tf_combo = [str(t) for t in (getattr(p, "tf_combo", None) or []) if t]

        self._state = "idle"  # idle | watching | confirmed | in_trade
        self._breakout_direction: Optional[str] = None
        self._confirm_count: int = 0
        self._armed: bool = False  # True after N consecutive outside candles
        self._recent_candles: List[Candle] = []
        self._traded_breakouts: Set[Tuple[str, str]] = set()

    def reset(self):
        self._state = "idle"
        self._breakout_direction = None
        self._confirm_count = 0
        self._armed = False
        self._recent_candles = []

    def set_traded_breakouts(self, keys):
        normalized: Set[Tuple[str, str]] = set()
        for item in keys or []:
            try:
                zone_id, direction = item[:2]
            except (TypeError, ValueError, IndexError):
                continue
            if zone_id and direction:
                normalized.add((str(zone_id), str(direction)))
        self._traded_breakouts = normalized

    def mark_breakout_used(self, zone_id: str, direction: str):
        if zone_id and direction:
            self._traded_breakouts.add((str(zone_id), str(direction)))

    def unlock_breakout(self, zone_id: str, direction: str):
        self._traded_breakouts.discard((str(zone_id), str(direction)))

    def reset_state_only(self):
        self.reset()

    def _remember_candle(self, candle: Candle):
        if self._recent_candles:
            gap = (candle.timestamp - self._recent_candles[-1].timestamp).total_seconds() / 60
            if gap > 60:
                self._recent_candles = []
        self._recent_candles.append(candle)
        if len(self._recent_candles) > 20:
            self._recent_candles = self._recent_candles[-20:]

    def warmup(self, candle: Candle):
        self._remember_candle(candle)

    @staticmethod
    def _normalize_zones(zones) -> List[ConsolidationZone]:
        if zones is None:
            return []
        if isinstance(zones, ConsolidationZone):
            return [zones]
        return [z for z in zones if z is not None]

    @staticmethod
    def _breakout_context(candle: Candle, zone_list: List[ConsolidationZone]):
        up_zones = [z for z in zone_list
                    if candle.open > z.vah_80 and candle.close > z.vah_80]
        down_zones = [z for z in zone_list
                      if candle.open < z.val_80 and candle.close < z.val_80]
        inside_any = any(
            z.val_80 <= candle.open <= z.vah_80 and z.val_80 <= candle.close <= z.vah_80
            for z in zone_list
        )

        up = bool(up_zones) and not down_zones
        down = bool(down_zones) and not up_zones
        direction = "up" if up else ("down" if down else None)
        return direction, up_zones, down_zones, inside_any

    def _clear_breakout_state(self):
        self._state = "idle"
        self._breakout_direction = None
        self._confirm_count = 0
        self._armed = False

    def reset_breakout_confirmation(self):
        self._clear_breakout_state()

    def _advance_breakout_state(self, candle: Candle, zone_list: List[ConsolidationZone], is_mature: bool):
        if not zone_list or not is_mature:
            return None, [], []
        if self._state == "in_trade":
            return None, [], []

        direction, up_zones, down_zones, inside_any = self._breakout_context(candle, zone_list)

        if inside_any and direction is None:
            self._clear_breakout_state()
            return None, up_zones, down_zones

        if direction == "up":
            if self._breakout_direction == "up":
                self._confirm_count += 1
            else:
                self._breakout_direction = "up"
                self._confirm_count = 1
                self._state = "watching"
        elif direction == "down":
            if self._breakout_direction == "down":
                self._confirm_count += 1
            else:
                self._breakout_direction = "down"
                self._confirm_count = 1
                self._state = "watching"
        else:
            if not self._armed:
                self._clear_breakout_state()

        if self._confirm_count >= self.CONFIRM_BARS:
            self._armed = True

        return direction, up_zones, down_zones

    def observe(
        self,
        candle: Candle,
        zones,
        is_mature: bool,
    ) -> None:
        """Advance breakout confirmation from historical/catch-up candles only."""
        self._remember_candle(candle)
        zone_list = self._normalize_zones(zones)
        self._advance_breakout_state(candle, zone_list, is_mature)
        if self._state == "confirmed":
            self._state = "watching"

    def evaluate(
        self,
        candle: Candle,
        zones,
        is_mature: bool,
    ) -> Optional[TradeSignal]:
        """Breakout vs. up to N recent reference zones.

        `zones` may be a single ConsolidationZone (legacy) or a list of the
        recent reference zones (v1.0.6). A breakout up requires the candle to
        close AND open above the VAH of at least one zone; a breakout down
        below the VAL of at least one zone — sustained for CONFIRM_BARS
        consecutive candles. The trade zone is the strongest level broken
        (highest VAH for longs / lowest VAL for shorts), and its VP profile
        drives the lowest-volume-node SL.
        """
        self._remember_candle(candle)

        # Normalize to a list of reference zones.
        zone_list = self._normalize_zones(zones)

        if not zone_list or not is_mature:
            return None
        if self._state == "in_trade":
            return None

        _, up_zones, down_zones = self._advance_breakout_state(candle, zone_list, is_mature)

        # Armed → choose the strongest broken zone and place/refresh order.
        if self._armed and self._breakout_direction:
            if self._breakout_direction == "up":
                trade_zone = max(up_zones, key=lambda z: z.vah_80) if up_zones else None
            else:
                trade_zone = min(down_zones, key=lambda z: z.val_80) if down_zones else None
            if trade_zone is None:
                return None
            bk = (str(trade_zone.zone_id), self._breakout_direction)
            if bk in self._traded_breakouts:
                return None
            self._state = "confirmed"
            return self._generate_signal(candle, trade_zone, self._breakout_direction)

        return None

    def _generate_signal(self, candle: Candle, zone: ConsolidationZone, direction: str) -> TradeSignal:
        # v1.0.6 SL model: SL = lowest-volume price node between POC and VAH (long)
        # or between POC and VAL (short). TP = entry ± rr_ratio × |entry − SL|.
        fallback_pts = self.SL_TICKS * self.TICK_SIZE

        if direction == "up":
            entry = zone.vah_80
            node = zone.lowest_volume_price_between(zone.poc, zone.vah_80)
            # SL must sit below entry for a long; clamp to fallback if node is invalid.
            if node is None or node >= entry:
                sl = entry - fallback_pts
            else:
                sl = node
            min_sl = entry - self.MIN_STOP_TICKS * self.TICK_SIZE
            if sl > min_sl:
                sl = min_sl
            sl_dist = abs(entry - sl)
            tp = entry + sl_dist * self.RR_RATIO
            trade_dir = Direction.BUY
        else:
            entry = zone.val_80
            node = zone.lowest_volume_price_between(zone.poc, zone.val_80)
            # SL must sit above entry for a short; clamp to fallback if node is invalid.
            if node is None or node <= entry:
                sl = entry + fallback_pts
            else:
                sl = node
            min_sl = entry + self.MIN_STOP_TICKS * self.TICK_SIZE
            if sl < min_sl:
                sl = min_sl
            sl_dist = abs(sl - entry)
            tp = entry - sl_dist * self.RR_RATIO
            trade_dir = Direction.SELL

        sl_dollars = abs(entry - sl) * POINT_VALUE
        tp_dollars = abs(tp - entry) * POINT_VALUE

        logger.info(
            f"[SessionTrend] BREAKOUT {direction.upper()} | "
            f"entry={entry:.2f} SL={sl:.2f} TP={tp:.2f} RR=1:{self.RR_RATIO} | "
            f"zone={zone.zone_id}"
        )

        trade_tf = str(getattr(zone, "timeframe", "") or self.area_timeframe)
        decision_tfs = (
            list(self.tf_combo)
            if self.method == "overlap" and len(self.tf_combo) >= 2
            else [self.area_timeframe]
        )
        overlap_tfs = list(self.tf_combo) if self.method == "overlap" and len(self.tf_combo) >= 2 else []
        primary_zone: Dict[str, Any] = {
            "tf": trade_tf,
            "zone_id": getattr(zone, "zone_id", "") or "",
            "poc": getattr(zone, "poc", None),
            "vah_80": getattr(zone, "vah_80", None),
            "val_80": getattr(zone, "val_80", None),
            "high_100": getattr(zone, "high_100", None),
            "low_100": getattr(zone, "low_100", None),
            "formed_at": (
                zone.formed_at.isoformat()
                if getattr(zone, "formed_at", None) else None
            ),
            "left_at": (
                zone.left_at.isoformat()
                if getattr(zone, "left_at", None) else None
            ),
        }
        meta = {
            "strategy_family": "trend",
            "mode": self.method,
            "side": "VAH" if direction == "up" else "VAL",
            "decision_tfs": decision_tfs,
            "overlap_tfs": overlap_tfs,
            "trade_tf": trade_tf,
            "largest_tf": trade_tf,
            "wall_id": f"{trade_tf}:{zone.zone_id}:{direction}",
            "labels": [
                "judge:" + "/".join(decision_tfs),
                "overlap:" + ("/".join(overlap_tfs) if overlap_tfs else "off"),
                "trade:" + trade_tf,
            ],
            "primary_zone": primary_zone,
        }

        return TradeSignal(
            strategy=StrategyType.TREND_FOLLOW,
            direction=trade_dir,
            entry_price=entry,
            sl_price=sl,
            tp_price=tp,
            zone_id=zone.zone_id,
            reason=(
                f"TREND {direction.upper()} | "
                f"open+close {'> VAH' if direction == 'up' else '< VAL'} | "
                f"SL@lowVol {sl:.2f}(${sl_dollars:.0f}) TP 1:{self.RR_RATIO}(${tp_dollars:.0f})"
            ),
            timestamp=candle.timestamp,
            breakout_range=abs(zone.high_100 - zone.vah_80) if direction == "up" else abs(zone.val_80 - zone.low_100),
            meta=meta,
        )

    def notify_trade_closed(self, exit_reason: str):
        self._state = "idle"
        self._breakout_direction = None

    def notify_order_cancelled(self):
        if self._state == "confirmed":
            self._state = "idle"

    def get_phase_label(self) -> str:
        if self._state == "confirmed":
            d = "VAH UP" if self._breakout_direction == "up" else "VAL DOWN"
            return f"BREAKOUT ORDER PENDING: {d}"
        if self._state == "in_trade":
            return "IN POSITION"
        return "Waiting for breakout"

    @property
    def raw_state(self) -> str:
        return self._state
