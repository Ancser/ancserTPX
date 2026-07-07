"""Completed-candle factor strategies.

Research and live assumptions:
- Build completed 5m bars from 1m candles.
- Evaluate the factor only after a 5m bar is complete.
- Defer the signal to the next 5m open and submit a market order.
- Risk can be fixed points, ATR, ATR blend, or a fraction of the last 15m range.
"""

from __future__ import annotations

import math
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from backend.db.models import Candle, Direction, StrategyType, TradeSignal, get_tick_size


_UTC = timezone.utc
_CT = ZoneInfo("America/Chicago")


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=_UTC)
    return ts.astimezone(_UTC)


def _topstep_trade_date(ts: datetime) -> str:
    ct = _utc(ts).astimezone(_CT)
    if ct.hour >= 17:
        ct = ct + timedelta(days=1)
    return ct.strftime("%Y-%m-%d")


def _ema(values: list[Optional[float]], span: int) -> list[Optional[float]]:
    alpha = 2.0 / (float(span) + 1.0)
    out: list[Optional[float]] = []
    prev: Optional[float] = None
    for value in values:
        if value is None:
            out.append(prev)
            continue
        v = float(value)
        if not math.isfinite(v):
            out.append(prev)
            continue
        prev = v if prev is None else alpha * v + (1.0 - alpha) * prev
        out.append(prev)
    return out


def _rma(values: list[Optional[float]], length: int) -> list[float]:
    alpha = 1.0 / float(length)
    out: list[float] = []
    prev = 0.0
    seeded = False
    for value in values:
        v = 0.0 if value is None else float(value)
        if not math.isfinite(v):
            v = 0.0
        prev = v if not seeded else alpha * v + (1.0 - alpha) * prev
        seeded = True
        out.append(prev)
    return out


def _bcwsma(values: list[Optional[float]], length: int, multiplier: int) -> list[float]:
    out: list[float] = []
    prev = 0.0
    for value in values:
        raw = 0.0 if value is None else float(value)
        if not math.isfinite(raw):
            raw = 0.0
        prev = (multiplier * raw + (length - multiplier) * prev) / float(length)
        out.append(prev)
    return out


class FactorSignalStrategy:
    """EMAPMO / momentum-reversion / icefishball as a live/backtest strategy."""

    PENDING_TIMEOUT_CANDLES = 1

    def __init__(self, params=None):
        p = params
        self.timeframe_minutes = max(1, int(getattr(p, "factor_timeframe_minutes", 5) or 5))
        self.candle_seconds = max(1, int(getattr(p, "candle_seconds", 60) or 60))
        self.signal_family = str(getattr(p, "factor_signal_family", "emapmo") or "emapmo").lower()
        if self.signal_family in {"pmo", "ema_pmo", "emAPMO".lower()}:
            self.signal_family = "emapmo"
        if self.signal_family not in {"emapmo", "momentum_reversion", "icefishball"}:
            self.signal_family = "emapmo"
        self.pmo_signal_mode = str(getattr(p, "factor_pmo_signal_mode", "normal") or "normal").lower()
        if self.pmo_signal_mode not in {"normal", "early", "both"}:
            self.pmo_signal_mode = "normal"
        self.side_mode = str(getattr(p, "factor_side_mode", "all") or "all").lower()
        if self.side_mode not in {"all", "long_only", "short_only"}:
            self.side_mode = "all"
        self.sl_rule = str(getattr(p, "factor_sl_rule", "atr") or "atr").lower()
        self.tp_rule = str(getattr(p, "factor_tp_rule", "atr") or "atr").lower()
        self.sl_value = max(0.01, float(getattr(p, "factor_sl_value", 1.5) or 1.5))
        self.tp_value = max(0.01, float(getattr(p, "factor_tp_value", 2.0) or 2.0))
        self.max_hold_bars = max(0, int(getattr(p, "factor_max_hold_bars", 24) or 0))
        self.max_trades_per_day = max(0, int(getattr(p, "factor_max_trades_per_day", 3) or 0))
        self.warmup_bars = max(20, int(getattr(p, "factor_warmup_bars", 150) or 150))
        self.tick_size = max(0.0001, float(get_tick_size(getattr(p, "contract_id", ""))))

        self._bars: deque[Candle] = deque(maxlen=max(self.warmup_bars + 120, 320))
        self._working: Optional[dict[str, Any]] = None
        self._last_bucket_key: Optional[datetime] = None
        self._deferred_signal: Optional[dict[str, Any]] = None
        self._daily_counts: dict[str, int] = {}
        self._state = "idle"

    @property
    def raw_state(self) -> str:
        return self._state

    def reset(self):
        self._bars.clear()
        self._working = None
        self._last_bucket_key = None
        self._deferred_signal = None
        self._daily_counts = {}
        self._state = "idle"

    def reset_state_only(self):
        self._state = "idle"

    def reset_breakout_confirmation(self):
        self.reset_state_only()

    def warmup(self, candle: Candle):
        self.observe(candle, [], True)

    def observe(self, candle: Candle, zones=None, is_mature=True) -> None:
        final_bar = self._ingest(candle)
        if final_bar is not None:
            self._append_bar(final_bar)

    def set_levels(self, levels):
        return None

    def get_levels(self):
        return None

    def set_traded_breakouts(self, keys):
        return None

    def mark_breakout_used(self, zone_id, direction):
        return None

    def unlock_breakout(self, zone_id, direction):
        return None

    def notify_trade_closed(self, exit_reason: str):
        self._state = "idle"

    def notify_order_cancelled(self):
        self._state = "idle"

    def get_phase_label(self) -> str:
        return f"FACTOR {self.signal_family} {len(self._bars)}/{self.warmup_bars}"

    def _bucket_start(self, ts: datetime) -> datetime:
        ts = _utc(ts).replace(second=0, microsecond=0)
        minute = (ts.minute // self.timeframe_minutes) * self.timeframe_minutes
        return ts.replace(minute=minute)

    def _complete_this_candle(self, candle: Candle) -> bool:
        if self.candle_seconds >= self.timeframe_minutes * 60:
            return True
        ts = _utc(candle.timestamp)
        return (ts.minute % self.timeframe_minutes) == (self.timeframe_minutes - 1)

    def _make_bar_from_working(self) -> Optional[Candle]:
        w = self._working
        if not w:
            return None
        return Candle(
            timestamp=w["timestamp"],
            open=w["open"],
            high=w["high"],
            low=w["low"],
            close=w["close"],
            volume=int(w["volume"]),
            symbol=w.get("symbol", "MNQ"),
            interval=f"{self.timeframe_minutes}m",
        )

    def _ingest(self, candle: Candle) -> Optional[Candle]:
        ts = _utc(candle.timestamp)
        if self.candle_seconds >= self.timeframe_minutes * 60:
            return Candle(
                timestamp=candle.timestamp,
                open=float(candle.open),
                high=float(candle.high),
                low=float(candle.low),
                close=float(candle.close),
                volume=int(candle.volume or 0),
                symbol=candle.symbol,
                interval=f"{self.timeframe_minutes}m",
            )

        bucket = self._bucket_start(ts)
        finalized: Optional[Candle] = None
        if self._working is not None and bucket != self._last_bucket_key:
            finalized = self._make_bar_from_working()
            self._working = None

        if self._working is None:
            self._working = {
                "timestamp": bucket,
                "open": float(candle.open),
                "high": float(candle.high),
                "low": float(candle.low),
                "close": float(candle.close),
                "volume": float(candle.volume or 0),
                "symbol": candle.symbol,
            }
            self._last_bucket_key = bucket
        else:
            self._working["high"] = max(float(self._working["high"]), float(candle.high))
            self._working["low"] = min(float(self._working["low"]), float(candle.low))
            self._working["close"] = float(candle.close)
            self._working["volume"] = float(self._working["volume"]) + float(candle.volume or 0)

        if self._complete_this_candle(candle):
            finalized = self._make_bar_from_working()
            self._working = None
            self._last_bucket_key = None
        return finalized

    def _append_bar(self, bar: Candle) -> None:
        if self._bars and _utc(self._bars[-1].timestamp) == _utc(bar.timestamp):
            return
        self._bars.append(bar)

    def _round_tick(self, price: float) -> float:
        return round(float(price) / self.tick_size) * self.tick_size

    def _atr(self, length: int = 14, min_periods: int = 7) -> Optional[float]:
        bars = list(self._bars)
        if len(bars) < min_periods:
            return None
        trs: list[float] = []
        start = max(0, len(bars) - length)
        for i in range(start, len(bars)):
            cur = bars[i]
            prev_close = bars[i - 1].close if i > 0 else cur.close
            trs.append(max(
                float(cur.high) - float(cur.low),
                abs(float(cur.high) - float(prev_close)),
                abs(float(cur.low) - float(prev_close)),
            ))
        return sum(trs) / len(trs) if len(trs) >= min_periods else None

    def _range15(self) -> Optional[float]:
        bars = list(self._bars)
        if len(bars) < 3:
            return None
        tail = bars[-3:]
        return max(float(b.high) for b in tail) - min(float(b.low) for b in tail)

    def _risk_width(self, rule: str, value: float) -> Optional[float]:
        atr14 = self._atr(14, 7)
        if atr14 is None or atr14 <= 0:
            return None
        if rule == "fixed":
            width = value
        elif rule == "atr_blend":
            atr50 = self._atr(50, 25) or atr14
            width = ((atr14 + atr50) / 2.0) * value
        elif rule == "range15_pct":
            rng = self._range15() or atr14
            width = max(rng, atr14) * value
        else:
            width = atr14 * value
        return max(self.tick_size, float(width))

    def _pmo_series(self) -> tuple[list[Optional[float]], list[Optional[float]]]:
        closes = [float(c.close) for c in self._bars]
        roc: list[Optional[float]] = [None]
        for i in range(1, len(closes)):
            prev = closes[i - 1]
            roc.append(None if prev == 0 else 100.0 * (closes[i] - prev) / prev)
        first = _ema(roc, 100)
        pmo = _ema([None if v is None else 10.0 * v for v in first], 50)
        signal = _ema(pmo, 10)
        return pmo, signal

    def _factor_direction(self) -> tuple[Optional[Direction], dict[str, Any]]:
        if len(self._bars) < self.warmup_bars:
            return None, {}
        bars = list(self._bars)
        closes = [float(c.close) for c in bars]

        if self.signal_family == "emapmo":
            pmo, sig = self._pmo_series()
            if len(pmo) < 3 or len(sig) < 3:
                return None, {}
            p0, p1 = pmo[-2], pmo[-1]
            s0, s1 = sig[-2], sig[-1]
            if None in (p0, p1, s0, s1):
                return None, {}
            assert p0 is not None and p1 is not None and s0 is not None and s1 is not None
            normal_short = p1 > 0.06 and p1 < s1 and p0 >= s0
            normal_long = p1 < -0.10 and p1 > s1 and p0 <= s0
            p = [None if a is None or b is None else a - b for a, b in zip(pmo, sig)]
            q = [None if a is None or b is None else b - a for a, b in zip(pmo, sig)]
            early_short = early_long = False
            if None not in (p[-1], p[-2], p[-3], q[-1], q[-2], q[-3]):
                early_short = bool(s1 > 0.06 and p[-1] < p[-2] and p1 > s1 and p[-2] < p[-3])
                early_long = bool(s1 < -0.10 and q[-1] < q[-2] and p1 < s1 and q[-2] < q[-3])
            use_normal = self.pmo_signal_mode in {"normal", "both"}
            use_early = self.pmo_signal_mode in {"early", "both"}
            if (use_normal and normal_short) or (use_early and early_short):
                return Direction.SELL, {"pmo": p1, "signal": s1}
            if (use_normal and normal_long) or (use_early and early_long):
                return Direction.BUY, {"pmo": p1, "signal": s1}
            return None, {"pmo": p1, "signal": s1}

        if self.signal_family == "icefishball":
            if len(bars) < 10:
                return None, {}
            rsv: list[Optional[float]] = []
            for i, close in enumerate(closes):
                if i < 8:
                    rsv.append(None)
                    continue
                hi = max(float(b.high) for b in bars[i - 8:i + 1])
                lo = min(float(b.low) for b in bars[i - 8:i + 1])
                rsv.append(None if hi <= lo else 100.0 * ((close - lo) / (hi - lo)))
            k = _bcwsma(rsv, 3, 1)
            d = _bcwsma(k, 3, 1)
            j = [(3.0 * kk) - (2.0 * dd) for kk, dd in zip(k, d)]
            delta: list[Optional[float]] = [None]
            for i in range(1, len(closes)):
                delta.append(closes[i] - closes[i - 1])
            up = _rma([None if v is None else max(v, 0.0) for v in delta], 14)
            down = _rma([None if v is None else max(-v, 0.0) for v in delta], 14)
            rsi = 100.0 if down[-1] == 0 else (0.0 if up[-1] == 0 else 100.0 - (100.0 / (1.0 + up[-1] / down[-1])))
            if j[-1] > 80 and j[-1] < j[-2] and closes[-1] > closes[-2] and rsi > 60:
                return Direction.SELL, {"j": j[-1], "rsi": rsi}
            if j[-1] < 20 and j[-1] > j[-2] and closes[-1] < closes[-2] and rsi < 40:
                return Direction.BUY, {"j": j[-1], "rsi": rsi}
            return None, {"j": j[-1], "rsi": rsi}

        if len(bars) < 43:
            return None, {}
        atr = self._atr(14, 7)
        if atr is None or atr <= 0:
            return None, {}
        mean_vals = _ema([float(c) for c in closes], 12)
        mean = mean_vals[-1]
        if mean is None:
            return None, {}
        mom = (closes[-1] - closes[-41]) / (atr * math.sqrt(40))
        rev = (closes[-1] - mean) / atr
        if mom >= 0.4 and rev <= -1.1:
            return Direction.BUY, {"mom_norm": mom, "rev_z": rev}
        if mom <= -0.4 and rev >= 1.1:
            return Direction.SELL, {"mom_norm": mom, "rev_z": rev}
        return None, {"mom_norm": mom, "rev_z": rev}

    def _side_allowed(self, direction: Direction) -> bool:
        if self.side_mode == "long_only" and direction != Direction.BUY:
            return False
        if self.side_mode == "short_only" and direction != Direction.SELL:
            return False
        return True

    def _build_signal(
        self,
        candle: Candle,
        pending: dict[str, Any],
        entry_price: Optional[float] = None,
    ) -> Optional[TradeSignal]:
        direction: Direction = pending["direction"]
        if not self._side_allowed(direction):
            return None
        trade_date = str(_topstep_trade_date(candle.timestamp))
        if self.max_trades_per_day and self._daily_counts.get(trade_date, 0) >= self.max_trades_per_day:
            return None
        risk = self._risk_width(self.sl_rule, self.sl_value)
        reward = self._risk_width(self.tp_rule, self.tp_value)
        if risk is None or reward is None:
            return None
        entry = self._round_tick(float(candle.open if entry_price is None else entry_price))
        if direction == Direction.BUY:
            sl = self._round_tick(entry - risk)
            tp = self._round_tick(entry + reward)
            side = "long"
        else:
            sl = self._round_tick(entry + risk)
            tp = self._round_tick(entry - reward)
            side = "short"
        if entry == sl or entry == tp:
            return None
        self._daily_counts[trade_date] = self._daily_counts.get(trade_date, 0) + 1
        self._state = "confirmed"
        final_bar_ts = pending["final_bar_ts"]
        zone_id = f"FACTOR:{self.signal_family}:{trade_date}:{side}:{final_bar_ts.isoformat()}"
        detail = pending.get("detail") or {}
        return TradeSignal(
            strategy=StrategyType.TREND_FOLLOW,
            direction=direction,
            entry_price=entry,
            sl_price=sl,
            tp_price=tp,
            zone_id=zone_id,
            zone_source="factor",
            reason=(
                f"FACTOR {self.signal_family.upper()} {side.upper()} | "
                f"SL={self.sl_rule}:{self.sl_value:g} TP={self.tp_rule}:{self.tp_value:g}"
            ),
            timestamp=candle.timestamp,
            breakout_range=risk,
            order_type="market",
            meta={
                "strategy_family": "factor",
                "signal_family": self.signal_family,
                "side": side,
                "signal_detail": detail,
                "trade_tf": f"{self.timeframe_minutes}m",
                "labels": [
                    f"factor:{self.signal_family}",
                    f"side:{self.side_mode}",
                    f"sl:{self.sl_rule}:{self.sl_value:g}",
                    f"tp:{self.tp_rule}:{self.tp_value:g}",
                ],
            },
        )

    def evaluate(self, candle: Candle, zones=None, is_mature: bool = True) -> Optional[TradeSignal]:
        if self._state == "in_trade":
            return None

        if self._deferred_signal is not None and self.candle_seconds >= self.timeframe_minutes * 60:
            pending = self._deferred_signal
            self._deferred_signal = None
            final_bar = self._ingest(candle)
            if final_bar is not None:
                self._append_bar(final_bar)
            return self._build_signal(candle, pending)

        final_bar = self._ingest(candle)
        if final_bar is None:
            return None
        self._append_bar(final_bar)
        direction, detail = self._factor_direction()
        if direction is None:
            return None
        self._deferred_signal = {
            "direction": direction,
            "final_bar_ts": _utc(final_bar.timestamp),
            "detail": detail,
        }
        if self.candle_seconds < self.timeframe_minutes * 60:
            pending = self._deferred_signal
            self._deferred_signal = None
            return self._build_signal(candle, pending, entry_price=float(candle.close))
        return None
