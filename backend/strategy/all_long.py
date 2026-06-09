"""
Legacy All Long support-level strategy / 舊版只做多支撐策略。

This module was an earlier experiment that placed buy-limit orders near recent
session-zone support levels. v0.17.0 does not expose it in UI/API presets; the
active modes are breakthrough, consolidation, and hybrid. Keep only as reference
until the unused-code report is acted on.
"""
from __future__ import annotations
import logging
from datetime import timedelta
from typing import List, Optional, Tuple

from backend.db.models import (
    Candle, ConsolidationZone, TradeSignal, StrategyParams,
    Direction, StrategyType,
)

logger = logging.getLogger(__name__)

POINT_VALUE = 20.0


class AllLongStrategy:
    """
    All Long Only — 在歷史支撐位掛 Limit Buy.

    支撐位來源: 過去 2 天所有 session zone 的 POC, VAH, VAL
    """

    TICK_SIZE = 0.25
    LOOKBACK_DAYS = 2

    def __init__(self, params: Optional[StrategyParams] = None):
        p = params or StrategyParams()
        self.SL_TICKS = p.sl_ticks
        self.TP_TICKS = p.tp_ticks
        self.PENDING_TIMEOUT_CANDLES = p.entry_timeout_minutes
        self.TP_TIMEOUT_CANDLES = p.tp_timeout_minutes
        self.TP_TIMEOUT_ACTION = p.tp_timeout_action

        self._state = "idle"  # idle | confirmed | in_trade
        self._current_target: Optional[float] = None
        self._consecutive_outside = 0  # not used, but kept for interface compat
        self.BREAKOUT_CONFIRM_CANDLES = 1  # instant confirm (for phase label compat)

    def reset(self):
        self._state = "idle"
        self._current_target = None

    def evaluate(
        self,
        candle: Candle,
        all_zones: List[ConsolidationZone],
        current_price: Optional[float] = None,
    ) -> Optional[TradeSignal]:
        """
        每根 1m K 線調用一次.

        Args:
            candle:        current 1m candle
            all_zones:     ALL zones from SessionZoneDetector (not just active)
            current_price: current market price (defaults to candle.close)
        """
        if self._state in ("confirmed", "in_trade"):
            return None

        price = current_price or candle.close

        # Collect support levels from past 2 days
        levels = self._collect_levels(all_zones, candle)

        # Find closest level below current price
        target = self._pick_closest_below(levels, price)
        if not target:
            return None

        level_price, zone_id, level_type = target
        self._state = "confirmed"
        self._current_target = level_price
        return self._generate_signal(level_price, zone_id, level_type, candle)

    def _collect_levels(
        self,
        all_zones: List[ConsolidationZone],
        candle: Candle,
    ) -> List[Tuple[float, str, str]]:
        """Collect all POC/VAH/VAL from zones formed in the past 2 days."""
        cutoff = candle.timestamp - timedelta(days=self.LOOKBACK_DAYS)
        levels = []
        seen = set()  # deduplicate identical price levels

        for zone in all_zones:
            if not zone.formed_at or zone.formed_at < cutoff:
                continue

            for price, label in [
                (zone.poc, "POC"),
                (zone.vah_80, "VAH"),
                (zone.val_80, "VAL"),
            ]:
                # Round to tick and deduplicate
                rounded = round(round(price / self.TICK_SIZE) * self.TICK_SIZE, 2)
                if rounded not in seen:
                    seen.add(rounded)
                    levels.append((rounded, zone.zone_id, label))

        return levels

    def _pick_closest_below(
        self,
        levels: List[Tuple[float, str, str]],
        price: float,
    ) -> Optional[Tuple[float, str, str]]:
        """Pick the highest support level that is below current price."""
        # Must be at least 1 point below price to avoid instant fill
        below = [
            (lvl, zid, ltype) for lvl, zid, ltype in levels
            if lvl < price - 1.0
        ]
        if not below:
            return None

        # Sort descending — closest to price first
        below.sort(key=lambda x: x[0], reverse=True)
        return below[0]

    def _generate_signal(
        self,
        level: float,
        zone_id: str,
        level_type: str,
        candle: Candle,
    ) -> TradeSignal:
        """Generate a BUY signal at the support level."""
        entry = level
        sl_points = self.SL_TICKS * self.TICK_SIZE
        tp_points = self.TP_TICKS * self.TICK_SIZE
        sl = entry - sl_points
        tp = entry + tp_points

        sl_dollars = sl_points * POINT_VALUE
        tp_dollars = tp_points * POINT_VALUE

        logger.info(
            f"[AllLong] BUY @ {level_type} {entry:.2f} | "
            f"SL={sl:.2f} TP={tp:.2f} | "
            f"SL ${sl_dollars:.0f} TP ${tp_dollars:.0f} | "
            f"zone={zone_id}"
        )

        return TradeSignal(
            strategy=StrategyType.ALL_LONG,
            direction=Direction.BUY,
            entry_price=entry,
            sl_price=sl,
            tp_price=tp,
            zone_id=zone_id,
            reason=(
                f"ALL LONG | BUY @ {level_type} {entry:.2f} | "
                f"SL {self.SL_TICKS}t(${sl_dollars:.0f}) TP {self.TP_TICKS}t(${tp_dollars:.0f})"
            ),
            timestamp=candle.timestamp,
        )

    def notify_trade_closed(self, exit_reason: str):
        """Called by engine when trade closes."""
        self._state = "idle"
        self._current_target = None

    def notify_order_cancelled(self):
        """Called by engine when pending order is cancelled/timeout.
        Go back to idle to rescan levels on next candle.
        """
        logger.info(
            f"[AllLong] Order cancelled → rescan "
            f"(was targeting {self._current_target})"
        )
        self._state = "idle"
        self._current_target = None

    def get_phase_label(self) -> str:
        if self._state == "idle":
            if self._current_target:
                return f"掃描支撐 @ {self._current_target:.2f}"
            return "掃描支撐"
        elif self._state == "confirmed":
            return f"入場準備 @ {self._current_target:.2f}" if self._current_target else "入場準備"
        elif self._state == "in_trade":
            return "持倉中"
        return self._state

    @property
    def raw_state(self) -> str:
        return self._state
