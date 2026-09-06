"""Causal QQQ Option Wall signals mapped onto MNQ execution candles.

Version one intentionally exposes one sub-model only: ``primary_strict``.
Its entry tape is the out-of-sample hourly primary-model direction after the
fixed OI/volume Gamma, article-alignment, and target-wall-room gates.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from typing import Iterable, Optional

from backend.data.option_wall_signals import (
    OptionWallSignal,
    load_primary_strict_signals,
)
from backend.db.models import Candle, Direction, StrategyType, TradeSignal
from backend.strategy.research_lab import _ResearchBase, _utc


PRIMARY_STRICT = "primary_strict"
OPTION_WALL_SUBMODELS = (PRIMARY_STRICT,)
SIGNAL_TOLERANCE_MINUTES = 2
NO_TP_DISTANCE_POINTS = 1_000_000.0


def normalize_option_wall_submodel(value: object) -> str:
    name = str(value or "").strip().lower().replace(" ", "_")
    return name if name in OPTION_WALL_SUBMODELS else PRIMARY_STRICT


def normalize_option_wall_side(value: object) -> str:
    name = str(value or "").strip().lower()
    return name if name in {"all", "long_only", "short_only"} else "all"


class OptionWallStrategy(_ResearchBase):
    """Historical-replay Option Wall strategy for the shared MNQ engine."""

    NAME = "OPTION WALL"

    def __init__(
        self,
        params,
        signals: Optional[Iterable[OptionWallSignal]] = None,
    ):
        super().__init__(params)
        # The validated ATR blend uses completed five-minute candles regardless
        # of the generic research timeframe used by other strategy families.
        self.tf_minutes = 5
        self.submodel = normalize_option_wall_submodel(
            getattr(params, "option_wall_submodel", PRIMARY_STRICT),
        )
        self.side_mode = normalize_option_wall_side(
            getattr(params, "option_wall_side_mode", "all"),
        )
        self.long_sl_atr = max(
            0.1, float(getattr(params, "option_wall_long_sl_atr", 4.0) or 4.0),
        )
        self.short_sl_atr = max(
            0.1, float(getattr(params, "option_wall_short_sl_atr", 1.5) or 1.5),
        )
        self.max_hold_minutes = max(
            1, int(getattr(params, "option_wall_max_hold_min", 60) or 60),
        )
        self.max_trades_per_day = max(
            0, int(getattr(params, "option_wall_max_trades_per_day", 3) or 0),
        )
        self._history = sorted(
            list(signals) if signals is not None else load_primary_strict_signals(),
            key=lambda item: item.timestamp,
        )
        self._history_index = 0
        self._queue: deque[OptionWallSignal] = deque()
        contract_id = str(getattr(params, "contract_id", "") or "").upper()
        self.supported_contract = ".MNQ." in contract_id or contract_id == "MNQ"

    @property
    def signal_count(self) -> int:
        return len(self._history)

    def get_phase_label(self) -> str:
        if not self.supported_contract:
            return "OPTION WALL BLOCKED · MNQ ONLY"
        if not self._history:
            return "OPTION WALL DATA UNAVAILABLE"
        return f"OPTION WALL PRIMARY STRICT · {self.signal_count} REPLAY SIGNALS"

    def _drain_history(self, now: datetime) -> None:
        tolerance = timedelta(minutes=SIGNAL_TOLERANCE_MINUTES)
        while self._history_index < len(self._history):
            item = self._history[self._history_index]
            # Never look ahead.  The tolerance only permits a late/missing 1m
            # candle to consume an already-published hourly snapshot.
            if item.timestamp > now:
                break
            self._history_index += 1
            if now - item.timestamp <= tolerance:
                self._queue.append(item)

    def _signal_allowed(self, item: OptionWallSignal) -> bool:
        if self.side_mode == "long_only":
            return item.direction == 1
        if self.side_mode == "short_only":
            return item.direction == -1
        return True

    def _make_signal(
        self,
        candle: Candle,
        item: OptionWallSignal,
        atr_blend: float,
    ) -> Optional[TradeSignal]:
        direction = Direction.BUY if item.direction > 0 else Direction.SELL
        if not self._side_ok(direction):
            return None
        trade_date = self._trade_date(candle.timestamp)
        if (
            self.max_trades_per_day
            and self._daily.get(trade_date, 0) >= self.max_trades_per_day
        ):
            return None

        multiple = self.long_sl_atr if item.direction > 0 else self.short_sl_atr
        entry = self._round(candle.close)
        risk = atr_blend * multiple
        if item.direction > 0:
            sl_price = self._round(entry - risk)
            tp_price = self._round(entry + NO_TP_DISTANCE_POINTS)
        else:
            sl_price = self._round(entry + risk)
            tp_price = self._round(entry - NO_TP_DISTANCE_POINTS)
        if entry == sl_price:
            return None

        self._daily[trade_date] = self._daily.get(trade_date, 0) + 1
        self._state = "confirmed"
        regime = "positive" if item.volume_gamma_state > 0 else "negative"
        signal_time = item.timestamp.isoformat()
        return TradeSignal(
            strategy=StrategyType.TREND_FOLLOW,
            direction=direction,
            entry_price=entry,
            sl_price=sl_price,
            tp_price=tp_price,
            zone_id=f"OPTION_WALL:{self.submodel}:{signal_time}",
            zone_source="option_wall",
            reason=(
                f"OPTION WALL | PRIMARY STRICT | {regime} gamma | "
                f"{multiple:g}x ATR SL | {self.max_hold_minutes}m max"
            ),
            timestamp=item.timestamp,
            order_type="market",
            meta={
                "option_wall": {
                    "submodel": self.submodel,
                    "signal_ts": signal_time,
                    "oi_gamma_state": item.oi_gamma_state,
                    "volume_gamma_state": item.volume_gamma_state,
                    "vwap_distance_bps": item.vwap_distance_bps,
                    "return_15m_bps": item.return_15m_bps,
                    "call_wall_bps": item.call_wall_bps,
                    "put_wall_bps": item.put_wall_bps,
                    "atr_blend": atr_blend,
                    "sl_atr_multiple": multiple,
                    "hard_tp_enabled": False,
                    "trailing_enabled": False,
                    "max_hold_minutes": self.max_hold_minutes,
                    "historical_replay_only": True,
                },
            },
        )

    def evaluate(self, candle: Candle, zones=None, is_mature: bool = True):
        self._roll(candle)
        if not self.supported_contract or not self._history:
            return None
        now = _utc(candle.timestamp)
        self._drain_history(now)
        while self._queue:
            item = self._queue.popleft()
            if not self._signal_allowed(item):
                continue
            atr_blend = self._atr_blend()
            if atr_blend is None or atr_blend <= 0:
                continue
            signal = self._make_signal(candle, item, atr_blend)
            if signal is not None:
                return signal
        return None
