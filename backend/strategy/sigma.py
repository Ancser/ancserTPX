"""Rolling sigma fade strategy.

The strategy recalculates a rolling price distribution from completed 1m bars
and fades rejection at outer sigma bands. It is intentionally interface
compatible with SessionTrendFollow so the same backtest/live engines can run it.
"""

from __future__ import annotations

import math
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

from backend.db.models import Candle, Direction, StrategyType, TradeSignal
from backend.strategy.session_filter import market_session


TICK_SIZE = 0.25
MIN_SIGMA_POINTS = 1.0


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _round_tick(price: float) -> float:
    return round(float(price) / TICK_SIZE) * TICK_SIZE


def _weighted_median(values: list[float], weights: list[float]) -> float:
    pairs = sorted(zip(values, weights), key=lambda item: item[0])
    total = sum(weight for _, weight in pairs)
    if total <= 0:
        return pairs[len(pairs) // 2][0]
    acc = 0.0
    half = total / 2.0
    for value, weight in pairs:
        acc += weight
        if acc >= half:
            return value
    return pairs[-1][0]


def _session_for(ts: datetime) -> tuple[str, datetime]:
    return market_session(ts)


class RollingSigmaFade:
    """Fade rolling sigma bands.

    Production presets use:
      #1: RTH, rolling 30m, std, blind/resting, no acceptance filter, TP half, SL 1 sigma
      #2: RTH, rolling 15m, std, blind/resting, acceptance filter, TP half, SL 1.5 sigma
    """

    PENDING_TIMEOUT_CANDLES = 1

    def __init__(self, params=None):
        p = params
        self.window_minutes = max(5, int(getattr(p, "sigma_window_minutes", 15) or 15))
        candle_seconds = max(1, int(getattr(p, "candle_seconds", 60) or 60))
        self.window_bars = max(3, int(round(self.window_minutes * 60 / candle_seconds)))
        self.method = str(getattr(p, "sigma_method", "std") or "std").lower()
        if self.method not in {"std", "mad"}:
            self.method = "std"
        self.entry_mode = str(getattr(p, "sigma_entry_mode", "reject") or "reject").lower()
        if self.entry_mode not in {"reject", "blind"}:
            self.entry_mode = "reject"
        self.accept_mode = str(getattr(p, "sigma_accept_mode", "none") or "none").lower()
        if self.accept_mode not in {"none", "filter", "switch"}:
            self.accept_mode = "none"
        self.start_sigma = max(0.5, float(getattr(p, "sigma_start", 1.0) or 1.0))
        self.max_sigma = max(self.start_sigma, float(getattr(p, "sigma_max", 3.0) or 3.0))
        self.target_mode = str(getattr(p, "sigma_target_mode", "half") or "half").lower()
        if self.target_mode not in {"inner1", "half", "center"}:
            self.target_mode = "half"
        self.stop_span = max(0.25, float(getattr(p, "sigma_stop_span", 1.0) or 1.0))
        self.accept_sigma = max(1.0, float(getattr(p, "sigma_accept_sigma", 2.0) or 2.0))
        self.accept_bars = max(1, int(getattr(p, "sigma_accept_bars", 2) or 2))

        self._candles: deque[Candle] = deque(maxlen=self.window_bars + 2)
        self._session_key: Optional[str] = None
        self._session_code: str = ""
        self._state = "idle"
        self._pending_side: Optional[str] = None
        self._armed_long = True
        self._armed_short = True
        self._accepted_up = False
        self._accepted_down = False
        self._up_count = 0
        self._down_count = 0
        self._last_center: Optional[float] = None
        self._last_sigma: Optional[float] = None

    def reset(self):
        self._candles.clear()
        self._session_key = None
        self._session_code = ""
        self._state = "idle"
        self._pending_side = None
        self._armed_long = True
        self._armed_short = True
        self._accepted_up = False
        self._accepted_down = False
        self._up_count = 0
        self._down_count = 0
        self._last_center = None
        self._last_sigma = None

    def reset_state_only(self):
        self.reset()

    def reset_breakout_confirmation(self):
        self.reset()

    def warmup(self, candle: Candle):
        self.observe(candle, None, True)

    def set_levels(self, levels: Optional[Dict[str, Any]]) -> None:
        pass

    def get_levels(self) -> Optional[Dict[str, Any]]:
        return None

    def set_traded_breakouts(self, keys):
        pass

    def mark_breakout_used(self, zone_id, direction):
        pass

    def unlock_breakout(self, zone_id, direction):
        pass

    def notify_trade_closed(self, exit_reason: str):
        self._state = "idle"
        self._pending_side = None

    def notify_order_cancelled(self):
        if self._pending_side == "long":
            self._armed_long = True
        elif self._pending_side == "short":
            self._armed_short = True
        self._state = "idle"
        self._pending_side = None

    @property
    def raw_state(self) -> str:
        return self._state

    def get_phase_label(self) -> str:
        if self._state == "confirmed":
            return "DISTRIBUTION pending"
        if self._state == "in_trade":
            return "DISTRIBUTION in trade"
        if self._last_center is None or self._last_sigma is None:
            return f"DISTRIBUTION warming {len(self._candles)}/{self.window_bars}"
        return f"DISTRIBUTION {self.window_minutes}m {self.method} C={self._last_center:.2f} σ={self._last_sigma:.2f}"

    def _ensure_session(self, candle: Candle) -> None:
        code, start = _session_for(candle.timestamp)
        key = f"{code}:{start.isoformat()}"
        if key == self._session_key:
            return
        self._candles.clear()
        self._session_key = key
        self._session_code = code
        self._state = "idle"
        self._pending_side = None
        self._armed_long = True
        self._armed_short = True
        self._accepted_up = False
        self._accepted_down = False
        self._up_count = 0
        self._down_count = 0
        self._last_center = None
        self._last_sigma = None

    def _history(self, candle: Candle) -> list[Candle]:
        bars = list(self._candles)
        if bars and _utc(bars[-1].timestamp) == _utc(candle.timestamp):
            bars = bars[:-1]
        return bars[-self.window_bars :]

    def _dist(self, bars: Iterable[Candle]) -> Optional[tuple[float, float]]:
        items = list(bars)
        if len(items) < self.window_bars:
            return None
        if (_utc(items[-1].timestamp) - _utc(items[0].timestamp)).total_seconds() > (self.window_minutes + 5) * 60:
            return None
        prices = [(c.high + c.low + c.close) / 3.0 for c in items]
        weights = [max(float(c.volume or 0), 1.0) for c in items]
        if self.method == "mad":
            center = _weighted_median(prices, weights)
            sigma = 1.4826 * _weighted_median([abs(p - center) for p in prices], weights)
        else:
            wsum = sum(weights)
            if wsum <= 0:
                return None
            center = sum(p * w for p, w in zip(prices, weights)) / wsum
            second = sum(p * p * w for p, w in zip(prices, weights)) / wsum
            sigma = math.sqrt(max(0.0, second - center * center))
        if sigma < MIN_SIGMA_POINTS:
            lo = min(c.low for c in items)
            hi = max(c.high for c in items)
            sigma = max(sigma, (hi - lo) / 4.0)
        if sigma < MIN_SIGMA_POINTS:
            return None
        return _round_tick(center), max(_round_tick(sigma), TICK_SIZE)

    def _levels(self) -> list[float]:
        levels: list[float] = []
        cur = float(self.start_sigma)
        while cur <= self.max_sigma + 1e-9:
            levels.append(round(cur, 2))
            cur += 1.0
        return levels

    def _target_level(self, level: float) -> float:
        if self.target_mode == "center":
            return 0.0
        if self.target_mode == "half":
            return level / 2.0
        return max(0.0, level - 1.0)

    def observe(self, candle: Candle, zones=None, is_mature=True) -> None:
        self._ensure_session(candle)
        if self._candles and _utc(self._candles[-1].timestamp) == _utc(candle.timestamp):
            return
        self._candles.append(candle)
        dist = self._dist(self._history(candle))
        if dist is None:
            return
        center, sigma = dist
        self._last_center = center
        self._last_sigma = sigma

        upper_accept = _round_tick(center + self.accept_sigma * sigma)
        lower_accept = _round_tick(center - self.accept_sigma * sigma)
        self._up_count = self._up_count + 1 if candle.close > upper_accept else 0
        self._down_count = self._down_count + 1 if candle.close < lower_accept else 0
        if self._up_count >= self.accept_bars:
            self._accepted_up = True
        if self._down_count >= self.accept_bars:
            self._accepted_down = True
        if candle.close <= center + self.start_sigma * sigma:
            self._accepted_up = False
        if candle.close >= center - self.start_sigma * sigma:
            self._accepted_down = False
        if candle.close > center:
            self._armed_long = True
        if candle.close < center:
            self._armed_short = True

    def evaluate(self, candle: Candle, zones=None, is_mature: bool = True) -> Optional[TradeSignal]:
        self._ensure_session(candle)
        if not self._candles or _utc(self._candles[-1].timestamp) != _utc(candle.timestamp):
            self.observe(candle, zones, is_mature)
        if self._state == "in_trade":
            return None
        dist = self._dist(self._history(candle))
        if dist is None:
            return None
        center, sigma = dist
        self._last_center = center
        self._last_sigma = sigma

        disable_short = self.accept_mode in {"filter", "switch"} and self._accepted_up
        disable_long = self.accept_mode in {"filter", "switch"} and self._accepted_down

        picks = []
        short_pick = None
        if self._armed_short and not disable_short:
            for level in self._levels():
                entry = _round_tick(center + level * sigma)
                touched = candle.high >= entry and candle.close <= entry
                resting = self.entry_mode == "blind" and entry > candle.close
                if resting or (self.entry_mode == "reject" and touched):
                    target_level = self._target_level(level)
                    tp = _round_tick(center + target_level * sigma)
                    sl = _round_tick(entry + self.stop_span * sigma)
                    if sl > entry > tp:
                        short_pick = (Direction.SELL, entry, sl, tp, level)
                        picks.append(short_pick)
                    break

        long_pick = None
        if self._armed_long and not disable_long:
            for level in self._levels():
                entry = _round_tick(center - level * sigma)
                touched = candle.low <= entry and candle.close >= entry
                resting = self.entry_mode == "blind" and entry < candle.close
                if resting or (self.entry_mode == "reject" and touched):
                    target_level = self._target_level(level)
                    tp = _round_tick(center - target_level * sigma)
                    sl = _round_tick(entry - self.stop_span * sigma)
                    if sl < entry < tp:
                        long_pick = (Direction.BUY, entry, sl, tp, level)
                        picks.append(long_pick)
                    break

        if self.entry_mode == "blind" and len(picks) >= 2:
            # The production engines support one working entry order.  Pick the
            # nearest resting band; a true multi-level grid needs a separate
            # pending-order subsystem.
            pick = min(picks, key=lambda item: abs(item[1] - candle.close))
        elif short_pick and long_pick:
            return None
        else:
            pick = short_pick or long_pick
        if pick is None:
            return None

        direction, entry, sl, tp, level = pick
        if direction == Direction.BUY:
            self._armed_long = False
            self._pending_side = "long"
        else:
            self._armed_short = False
            self._pending_side = "short"
        self._state = "confirmed"
        session_key = self._session_key or ""
        side = "long" if direction == Direction.BUY else "short"
        zone_id = f"SIG:{session_key}:{side}:{level:g}:{candle.timestamp.isoformat()}"
        primary_zone = {
            "tf": f"{self.window_minutes}m-sigma",
            "zone_id": zone_id,
            "poc": center,
            "vah_80": _round_tick(center + level * sigma),
            "val_80": _round_tick(center - level * sigma),
            "high_100": _round_tick(center + self.max_sigma * sigma),
            "low_100": _round_tick(center - self.max_sigma * sigma),
        }
        return TradeSignal(
            strategy=StrategyType.TREND_FOLLOW,
            direction=direction,
            entry_price=entry,
            sl_price=sl,
            tp_price=tp,
            zone_id=zone_id,
            zone_source="rolling_sigma",
            reason=(
                f"DISTRIBUTION {side.upper()} {self.entry_mode} L{level:g} | C={center:.2f} "
                f"sigma={sigma:.2f} SLspan={self.stop_span:g} TP={self.target_mode}"
            ),
            timestamp=candle.timestamp,
            breakout_range=sigma,
            meta={
                "strategy_family": "sigma",
                "mode": "rolling_sigma",
                "side": side,
                "session_code": self._session_code,
                "session_start": session_key,
                "center": center,
                "sigma": sigma,
                "level": level,
                "kind": "fade",
                "trade_tf": f"{self.window_minutes}m",
                "labels": [
                    f"sigma:{self.window_minutes}m",
                    f"{self.method}:{self.entry_mode}",
                    f"accept:{self.accept_mode}",
                ],
                "primary_zone": primary_zone,
                "accepted_up": self._accepted_up,
                "accepted_down": self._accepted_down,
            },
        )
