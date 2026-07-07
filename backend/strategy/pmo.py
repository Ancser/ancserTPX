"""EMAPMO strategy.

This is the production/backtest wrapper for the EMAPMO candidate researched in
``scripts/icefishball_pine_strategy_test.py``.  It keeps the same strategy
interface as ``SessionTrendFollow`` so the existing backtest and live engines can
run it without a separate execution path.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from backend.db.models import (
    Candle,
    Direction,
    StrategyType,
    TradeSignal,
    get_tick_size,
)


_CT = ZoneInfo("America/Chicago")
_UTC_TZ = timezone.utc


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=_UTC_TZ)
    return ts.astimezone(_UTC_TZ)


def _topstep_trade_date(utc_dt: datetime) -> str:
    aware = _utc(utc_dt)
    ct_dt = aware.astimezone(_CT)
    if ct_dt.hour >= 17:
        return (ct_dt + timedelta(days=1)).strftime("%Y-%m-%d")
    return ct_dt.strftime("%Y-%m-%d")


def _session_for(ts: datetime) -> tuple[str, datetime]:
    ts = _utc(ts)
    d = ts.date()
    tod = ts.time()
    if tod >= time(22, 0) or tod < time(7, 0):
        start_day = d if tod >= time(22, 0) else d - timedelta(days=1)
        return "ASIA", datetime.combine(start_day, time(22, 0), tzinfo=_UTC_TZ)
    if time(7, 0) <= tod < time(11, 0):
        return "EURO", datetime.combine(d, time(7, 0), tzinfo=_UTC_TZ)
    if time(11, 0) <= tod < time(13, 30):
        return "PRE", datetime.combine(d, time(11, 0), tzinfo=_UTC_TZ)
    if time(13, 30) <= tod < time(20, 0):
        return "RTH", datetime.combine(d, time(13, 30), tzinfo=_UTC_TZ)
    return "AH", datetime.combine(d, time(20, 0), tzinfo=_UTC_TZ)


def _ema(values: list[Optional[float]], span: int) -> list[Optional[float]]:
    alpha = 2.0 / (float(span) + 1.0)
    out: list[Optional[float]] = []
    prev: Optional[float] = None
    for value in values:
        if value is None:
            out.append(prev)
            continue
        if prev is None:
            prev = float(value)
        else:
            prev = alpha * float(value) + (1.0 - alpha) * prev
        out.append(prev)
    return out


class EMAPMOStrategy:
    """5m EMAPMO crossover/reversion strategy.

    Signal logic:
      - short: PMO > +0.06 and PMO crosses below its signal line
      - long:  PMO < -0.10 and PMO crosses above its signal line

    Risk uses completed-5m ATR(14): SL = ``pmo_sl_atr`` x ATR,
    TP = ``pmo_tp_atr`` x ATR.  Orders are market entries.
    """

    PENDING_TIMEOUT_CANDLES = 1

    def __init__(self, params=None):
        p = params
        self.timeframe_minutes = max(1, int(getattr(p, "pmo_timeframe_minutes", 5) or 5))
        self.candle_seconds = max(1, int(getattr(p, "candle_seconds", 60) or 60))
        self.signal_mode = str(getattr(p, "pmo_signal_mode", "normal") or "normal").lower()
        if self.signal_mode not in {"normal", "early"}:
            self.signal_mode = "normal"
        self.sl_atr = max(0.1, float(getattr(p, "pmo_sl_atr", 1.0) or 1.0))
        self.tp_atr = max(0.1, float(getattr(p, "pmo_tp_atr", 1.0) or 1.0))
        self.max_trades_per_day = max(0, int(getattr(p, "pmo_max_trades_per_day", 3) or 0))
        self.warmup_bars = max(20, int(getattr(p, "pmo_warmup_bars", 150) or 150))
        self.tick_size = max(0.0001, float(get_tick_size(getattr(p, "contract_id", ""))))

        self._bars: deque[Candle] = deque(maxlen=max(self.warmup_bars + 80, 260))
        self._working: Optional[dict[str, Any]] = None
        self._last_bucket_key: Optional[datetime] = None
        self._daily_counts: dict[str, int] = {}
        self._state = "idle"
        self._last_pmo: Optional[float] = None
        self._last_signal: Optional[float] = None
        self._last_atr: Optional[float] = None
        self._last_session_code: str = ""
        self._deferred_signal: Optional[dict[str, Any]] = None

    @property
    def raw_state(self) -> str:
        return self._state

    def reset(self):
        self._bars.clear()
        self._working = None
        self._last_bucket_key = None
        self._daily_counts = {}
        self._state = "idle"
        self._last_pmo = None
        self._last_signal = None
        self._last_atr = None
        self._last_session_code = ""
        self._deferred_signal = None

    def reset_state_only(self):
        self._state = "idle"

    def reset_breakout_confirmation(self):
        self.reset_state_only()

    def warmup(self, candle: Candle):
        self.observe(candle, [], True)

    def observe(self, candle: Candle, zones=None, is_mature=True) -> None:
        final_bar = self._ingest(candle)
        if final_bar is None:
            return
        if self._bars and _utc(self._bars[-1].timestamp) == _utc(final_bar.timestamp):
            return
        self._bars.append(final_bar)
        atr = self._atr14()
        self._last_atr = atr
        if len(self._bars) >= 3:
            pmo, signal = self._pmo_series()
            if pmo[-1] is not None and signal[-1] is not None:
                self._last_pmo = float(pmo[-1])
                self._last_signal = float(signal[-1])

    def set_levels(self, levels: Optional[Dict[str, Any]]) -> None:
        return None

    def get_levels(self) -> Optional[Dict[str, Any]]:
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
        if len(self._bars) < self.warmup_bars:
            return f"PMO warming {len(self._bars)}/{self.warmup_bars}"
        pmo = "?" if self._last_pmo is None else f"{self._last_pmo:.3f}"
        sig = "?" if self._last_signal is None else f"{self._last_signal:.3f}"
        atr = "?" if self._last_atr is None else f"{self._last_atr:.2f}"
        return f"PMO {self.signal_mode} {self.timeframe_minutes}m PMO={pmo} SIG={sig} ATR={atr}"

    def _round_tick(self, price: float) -> float:
        return round(float(price) / self.tick_size) * self.tick_size

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
            symbol=w.get("symbol", "ZL"),
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

    def _atr14(self) -> Optional[float]:
        bars = list(self._bars)
        if len(bars) < 7:
            return None
        trs: list[float] = []
        start = max(0, len(bars) - 14)
        for i in range(start, len(bars)):
            cur = bars[i]
            prev_close = bars[i - 1].close if i > 0 else cur.close
            tr = max(
                float(cur.high) - float(cur.low),
                abs(float(cur.high) - float(prev_close)),
                abs(float(cur.low) - float(prev_close)),
            )
            trs.append(tr)
        if not trs:
            return None
        return sum(trs) / len(trs)

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

    def _direction_from_signal(self) -> Optional[Direction]:
        if len(self._bars) < self.warmup_bars:
            return None
        pmo, signal = self._pmo_series()
        if len(pmo) < 3 or len(signal) < 3:
            return None
        p0, p1 = pmo[-2], pmo[-1]
        s0, s1 = signal[-2], signal[-1]
        if None in (p0, p1, s0, s1):
            return None
        self._last_pmo = float(p1)
        self._last_signal = float(s1)

        if self.signal_mode == "early":
            p = [None if a is None or b is None else a - b for a, b in zip(pmo, signal)]
            q = [None if a is None or b is None else b - a for a, b in zip(pmo, signal)]
            if None in (p[-1], p[-2], p[-3], q[-1], q[-2], q[-3]):
                return None
            if s1 > 0.06 and p[-1] < p[-2] and p1 > s1 and p[-2] < p[-3]:
                return Direction.SELL
            if s1 < -0.10 and q[-1] < q[-2] and p1 < s1 and q[-2] < q[-3]:
                return Direction.BUY
            return None

        crossunder = p1 < s1 and p0 >= s0
        crossover = p1 > s1 and p0 <= s0
        if p1 > 0.06 and crossunder:
            return Direction.SELL
        if p1 < -0.10 and crossover:
            return Direction.BUY
        return None

    def _build_signal(
        self,
        *,
        candle: Candle,
        direction: Direction,
        atr: float,
        pmo_value: float,
        signal_value: float,
        final_bar_ts: datetime,
        entry_price: float,
        session_code: str,
        session_start: datetime,
        trade_date: str,
    ) -> Optional[TradeSignal]:
        if self.max_trades_per_day and self._daily_counts.get(trade_date, 0) >= self.max_trades_per_day:
            return None

        entry = self._round_tick(entry_price)
        risk = max(self.tick_size, atr * self.sl_atr)
        reward = max(self.tick_size, atr * self.tp_atr)
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
        zone_id = f"PMO:{trade_date}:{side}:{final_bar_ts.isoformat()}"
        primary_zone = {
            "tf": f"{self.timeframe_minutes}m-pmo",
            "zone_id": zone_id,
            "poc": entry,
            "vah_80": max(entry, tp, sl),
            "val_80": min(entry, tp, sl),
            "high_100": max(entry, tp, sl),
            "low_100": min(entry, tp, sl),
        }
        return TradeSignal(
            strategy=StrategyType.TREND_FOLLOW,
            direction=direction,
            entry_price=entry,
            sl_price=sl,
            tp_price=tp,
            zone_id=zone_id,
            zone_source="pmo",
            reason=(
                f"PMO {self.signal_mode.upper()} {side.upper()} | "
                f"PMO={pmo_value:.3f} SIG={signal_value:.3f} ATR={atr:.2f} "
                f"SL={self.sl_atr:g}ATR TP={self.tp_atr:g}ATR"
            ),
            timestamp=candle.timestamp,
            breakout_range=atr,
            order_type="market",
            meta={
                "strategy_family": "pmo",
                "mode": "emapmo",
                "signal_mode": self.signal_mode,
                "side": side,
                "session_code": session_code,
                "session_start": session_start.isoformat(),
                "trade_tf": f"{self.timeframe_minutes}m",
                "atr14": atr,
                "pmo": pmo_value,
                "pmo_signal": signal_value,
                "primary_zone": primary_zone,
                "labels": [
                    f"pmo:{self.timeframe_minutes}m",
                    f"sl:{self.sl_atr:g}atr",
                    f"tp:{self.tp_atr:g}atr",
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
            if final_bar is not None and (
                not self._bars or _utc(self._bars[-1].timestamp) != _utc(final_bar.timestamp)
            ):
                self._bars.append(final_bar)
            return self._build_signal(
                candle=candle,
                direction=pending["direction"],
                atr=pending["atr"],
                pmo_value=pending["pmo"],
                signal_value=pending["signal"],
                final_bar_ts=pending["final_bar_ts"],
                entry_price=float(candle.open),
                session_code=pending["session_code"],
                session_start=pending["session_start"],
                trade_date=pending["trade_date"],
            )

        final_bar = self._ingest(candle)
        if final_bar is None:
            return None
        if self._bars and _utc(self._bars[-1].timestamp) == _utc(final_bar.timestamp):
            return None
        self._bars.append(final_bar)

        code, sess_start = _session_for(final_bar.timestamp)
        self._last_session_code = code
        trade_date = _topstep_trade_date(final_bar.timestamp)

        atr = self._atr14()
        self._last_atr = atr
        if atr is None or atr <= 0:
            return None

        direction = self._direction_from_signal()
        if direction is None:
            return None

        pmo_value = float(self._last_pmo if self._last_pmo is not None else 0.0)
        signal_value = float(self._last_signal if self._last_signal is not None else 0.0)
        if self.candle_seconds >= self.timeframe_minutes * 60:
            self._deferred_signal = {
                "direction": direction,
                "atr": atr,
                "pmo": pmo_value,
                "signal": signal_value,
                "final_bar_ts": final_bar.timestamp,
                "session_code": code,
                "session_start": sess_start,
                "trade_date": trade_date,
            }
            return None

        return self._build_signal(
            candle=candle,
            direction=direction,
            atr=atr,
            pmo_value=pmo_value,
            signal_value=signal_value,
            final_bar_ts=final_bar.timestamp,
            entry_price=float(candle.close),
            session_code=code,
            session_start=sess_start,
            trade_date=trade_date,
        )
