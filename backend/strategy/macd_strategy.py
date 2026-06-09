# ============================================================
# 文件 / File: backend/strategy/macd_strategy.py
# 狀態 / Status: legacy indicator strategy
# 功能 / Features:
#   - Historical MACD/VWAP crossover strategy kept for reference.
#   - Not selectable in v0.17.0 presets or live/backtest routes.
#   - Candidate for deletion after confirming no external scripts import it.
# ============================================================

"""
Legacy MACD-only strategy / 舊版 MACD-only 策略。

The current app uses session-zone strategies only. This file remains outside the
active preset list and is documented in the unused-code report.
"""
from __future__ import annotations
import logging
from typing import Optional

from backend.db.models import (
    Candle, ConsolidationZone, TradeSignal,
    StrategyParams, Direction, StrategyType,
)
from backend.strategy.indicators import MACDCalculator, VWAPCalculator

logger = logging.getLogger(__name__)

POINT_VALUE = 20.0
TICK_SIZE = 0.25


class MACDOnlyStrategy:
    """
    Pure MACD crossover strategy.

    Entry conditions:
      金叉: histogram ≤0 → >0  ⟹  BUY limit order at VWAP
      死叉: histogram ≥0 → <0  ⟹  SELL limit order at VWAP

    No zone dependency. SL/TP are pure tick-based from fill price.
    One position at a time (state = "in_trade" blocks new signals).
    """

    # Interface compatibility constant
    BREAKOUT_CONFIRM_CANDLES = 0    # N/A

    def __init__(self, params: Optional[StrategyParams] = None):
        p = params or StrategyParams()
        self.SL_TICKS = p.sl_ticks
        self.TP_TICKS = p.tp_ticks
        # Entry timeout: hardcoded 5 min (not user-configurable)
        _candle_secs = getattr(p, 'candle_seconds', 30)
        _cpm = max(1, 60 // _candle_secs)
        self.PENDING_TIMEOUT_CANDLES = 5 * _cpm   # 5 min hardcoded
        # TP timeout removed — no TP_TIMEOUT_CANDLES / TP_TIMEOUT_ACTION
        self._macd = MACDCalculator(12, 26, 9)   # EMA 12/26/9 hardcoded
        self._vwap = VWAPCalculator()
        # VWAP filter: always True (forced — BUY only above VWAP, SELL only below)
        self._prev_hist: Optional[float] = None
        self._state: str = "idle"       # "idle" | "in_trade"

    # ── Public API ──────────────────────────────────────────

    def reset(self):
        """Full reset — also clears MACD and VWAP history. Use for backtest reruns."""
        self._macd.reset()
        self._vwap.reset()
        self._prev_hist = None
        self._state = "idle"

    def reset_state_only(self):
        """
        Soft reset — keeps MACD and VWAP history.
        Called by live engine after warm-up so calculators are already primed.
        Sets _prev_hist to current MACD value so the first live candle can
        detect a crossover correctly.
        """
        self._prev_hist = self._macd.histogram
        self._state = "idle"
        # VWAP keeps its session accumulators — no reset needed

    def warmup(self, candle: Candle):
        """Feed a candle to MACD and VWAP without generating signals (live warm-up use)."""
        self._macd.update(candle.close)
        self._vwap.update(candle)

    def evaluate(
        self,
        candle: Candle,
        zone: Optional[ConsolidationZone] = None,
        is_mature: bool = True,
    ) -> Optional[TradeSignal]:
        """
        Called on every 1m candle.
        Ignores zone / is_mature — MACD is fully independent of zones.
        """
        self._macd.update(candle.close)
        vwap = self._vwap.update(candle)
        hist = self._macd.histogram

        signal = None
        if hist is not None and self._prev_hist is not None and self._state == "idle":
            # ── 金叉 (Golden Cross): histogram crosses zero upward → BUY ──
            if self._prev_hist <= 0 < hist:
                # VWAP filter (forced ON): only BUY if price > VWAP
                if vwap is None or candle.close > vwap:
                    signal = self._make_signal(candle, "up", vwap)
                    _vw = f"{vwap:.2f}" if vwap is not None else "N/A"
                    logger.info(
                        f"[MACD] 金叉 BUY @ {candle.close:.2f} | "
                        f"hist={hist:.4f}  prev={self._prev_hist:.4f} | VWAP={_vw}"
                    )
                else:
                    logger.info(
                        f"[MACD] 金叉 blocked by VWAP: close={candle.close:.2f} < VWAP={vwap:.2f}"
                    )

            # ── 死叉 (Death Cross): histogram crosses zero downward → SELL ──
            elif self._prev_hist >= 0 > hist:
                # VWAP filter (forced ON): only SELL if price < VWAP
                if vwap is None or candle.close < vwap:
                    signal = self._make_signal(candle, "down", vwap)
                    _vw = f"{vwap:.2f}" if vwap is not None else "N/A"
                    logger.info(
                        f"[MACD] 死叉 SELL @ {candle.close:.2f} | "
                        f"hist={hist:.4f}  prev={self._prev_hist:.4f} | VWAP={_vw}"
                    )
                else:
                    logger.info(
                        f"[MACD] 死叉 blocked by VWAP: close={candle.close:.2f} > VWAP={vwap:.2f}"
                    )

        self._prev_hist = hist
        return signal

    def notify_trade_closed(self, exit_reason: str):
        """Called by engine when position closes."""
        self._state = "idle"

    def notify_order_cancelled(self):
        """Called by engine when order is cancelled (edge case for market orders)."""
        self._state = "idle"

    def get_phase_label(self) -> str:
        if self._state == "idle":
            hist = self._macd.histogram
            vwap = self._vwap.value
            if hist is not None:
                vwap_str = f" | VWAP={vwap:.2f}" if vwap else ""
                return f"等待MACD信號 (hist={'%.3f' % hist}{vwap_str})"
            return "MACD 預熱中"
        elif self._state == "in_trade":
            return "持倉中"
        return self._state

    @property
    def raw_state(self) -> str:
        return self._state

    # ── Private ─────────────────────────────────────────────

    def _make_signal(self, candle: Candle, direction: str, vwap: Optional[float] = None) -> TradeSignal:
        sl_pts = self.SL_TICKS * TICK_SIZE
        tp_pts = self.TP_TICKS * TICK_SIZE

        # Limit order at VWAP (pullback entry); fall back to candle.close if VWAP unavailable
        entry = vwap if vwap is not None else candle.close
        order_type = "limit" if vwap is not None else "market"

        if direction == "up":
            dir_enum  = Direction.BUY
            sl_price  = entry - sl_pts
            tp_price  = entry + tp_pts
        else:
            dir_enum  = Direction.SELL
            sl_price  = entry + sl_pts
            tp_price  = entry - tp_pts

        self._state = "in_trade"

        cross_label = '金叉' if direction == 'up' else '死叉'
        _entry_str = f"{entry:.2f}"
        vwap_str = f" | VWAP limit={_entry_str}" if vwap is not None else " | market fallback"
        return TradeSignal(
            strategy=StrategyType.MACD,
            direction=dir_enum,
            entry_price=entry,
            sl_price=sl_price,
            tp_price=tp_price,
            zone_id="",
            reason=f"MACD {cross_label} | hist={self._macd.histogram:.4f}{vwap_str}",
            timestamp=candle.timestamp,
            order_type=order_type,
            macd_hist=self._macd.histogram,
        )
