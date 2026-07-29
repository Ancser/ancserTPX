"""Completed-candle factor strategies.

Research and live assumptions:
- Build completed 5m bars from 1m candles.
- Evaluate the factor only after a 5m bar is complete.
- Defer the signal to the next 5m open and submit a market order.
- Risk can be fixed points, ATR, ATR blend, a fraction of the last 15m range,
  or the same tick/RR rules used by TREND presets.
"""

from __future__ import annotations

import math
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from backend.db.models import Candle, Direction, StrategyType, TradeSignal, get_tick_size
from backend.strategy.volume_profile import VolumeProfileCalculator


_UTC = timezone.utc
_CT = ZoneInfo("America/Chicago")

# FACTOR backtests and live trading intentionally recalculate EMAPMO from this
# bounded completed-5m history.  Read-only chart overlays must use the same
# window; seeding the EMA from the full chart history can move SIG across an
# entry threshold even though the trading strategy has no signal.
FACTOR_EMAPMO_HISTORY_BARS = 320


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=_UTC)
    return ts.astimezone(_UTC)


def _topstep_trade_date(ts: datetime) -> str:
    ct = _utc(ts).astimezone(_CT)
    if ct.hour >= 17:
        ct = ct + timedelta(days=1)
    return ct.strftime("%Y-%m-%d")


def _session_id(ts: datetime) -> str:
    ts = _utc(ts)
    h, m = ts.hour, ts.minute
    if h >= 22:
        return ts.strftime("%Y-%m-%d") + "-ASIA"
    if h >= 20:
        return ts.strftime("%Y-%m-%d") + "-AH"
    if h > 13 or (h == 13 and m >= 30):
        return ts.strftime("%Y-%m-%d") + "-RTH"
    if h >= 11:
        return ts.strftime("%Y-%m-%d") + "-PRE"
    if h >= 7:
        return ts.strftime("%Y-%m-%d") + "-EURO"
    prev = ts - timedelta(days=1)
    return prev.strftime("%Y-%m-%d") + "-ASIA"


class _DevelopingSessionVa:
    """Incremental developing session value area for live/backtest filtering."""

    def __init__(self, tick_size: float, value_area_pct: float):
        self.calc = VolumeProfileCalculator(tick_size=tick_size, value_area_pct=value_area_pct)
        self.tick_size = tick_size
        self.value_area_pct = value_area_pct
        self.session_id: Optional[str] = None
        self.profile: dict[float, int] = {}
        self.poc: Optional[float] = None
        self.vah: Optional[float] = None
        self.val: Optional[float] = None

    def reset(self) -> None:
        self.session_id = None
        self.profile = {}
        self.poc = None
        self.vah = None
        self.val = None

    def update(self, candle: Candle) -> None:
        sid = _session_id(candle.timestamp)
        if sid != self.session_id:
            self.session_id = sid
            self.profile = {}
            self.poc = None
            self.vah = None
            self.val = None
        self._add_candle(candle)
        self._recompute()

    def _round_tick(self, price: float) -> float:
        return round(float(price) / self.tick_size) * self.tick_size

    def _add_candle(self, candle: Candle) -> None:
        volume = int(candle.volume or 0)
        if volume <= 0:
            return
        high = self._round_tick(float(candle.high))
        low = self._round_tick(float(candle.low))
        if high <= low:
            price = self._round_tick(float(candle.close))
            self.profile[price] = self.profile.get(price, 0) + volume
            return
        ticks = round((high - low) / self.tick_size) + 1
        vol_per_tick = int(volume / max(1, ticks))
        if vol_per_tick <= 0:
            vol_per_tick = 1
        price = low
        while price <= high + self.tick_size * 0.5:
            rounded = self._round_tick(price)
            self.profile[rounded] = self.profile.get(rounded, 0) + vol_per_tick
            price += self.tick_size

    def _recompute(self) -> None:
        if not self.profile:
            return
        poc = self.calc._find_poc(self.profile)
        vah, val = self.calc._calculate_value_area(self.profile, poc, self.value_area_pct)
        self.poc = poc
        self.vah = vah
        self.val = val


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


def calculate_emapmo_series(
    closes: list[float],
) -> tuple[list[Optional[float]], list[Optional[float]]]:
    """Return the production EMAPMO/Signal series for an already-bounded input."""
    roc: list[Optional[float]] = [None]
    for i in range(1, len(closes)):
        prev = float(closes[i - 1])
        roc.append(None if prev == 0 else 100.0 * (float(closes[i]) - prev) / prev)
    first = _ema(roc, 100)
    pmo = _ema([None if value is None else 10.0 * value for value in first], 50)
    signal = _ema(pmo, 10)
    return pmo, signal


EMAPMO_LONG_THRESHOLD = -0.10
EMAPMO_SHORT_THRESHOLD = 0.06


def calculate_emapmo_snapshot(
    closes: list[float],
    threshold_scale: float = 1.0,
    normal_scale: Optional[float] = None,
    early_scale: Optional[float] = None,
) -> dict[str, Any]:
    """Calculate the exact EMAPMO conditions shared by trading and charting.

    ``closes`` is deliberately not truncated here.  The caller owns the input
    window: :class:`FactorSignalStrategy` passes its bounded deque and the chart
    collector passes the matching last ``FACTOR_EMAPMO_HISTORY_BARS`` closes.

    1.0.9 ``threshold_scale``: PMO is built from *percent* ROC, so its scale
    tracks the instrument's percentage volatility — not its point volatility.
    The ATR SL/TP rules only size the exit, they do not touch this entry gate,
    so a lower-%-vol contract simply stops reaching ±0.10 and the strategy goes
    quiet (measured: the gate fires on 6.6% of MNQ 5m bars but only 1.9% of MES
    bars, and BEST produced 20 MNQ trades vs 7 MES trades over comparable
    windows).  Scaling the thresholds restores equal signal rarity across
    contracts.  Default 1.0 keeps MNQ behaviour bit-identical; the calibrated
    MES value is ~0.55 (see scripts/emapmo_vol_calibration.py).
    """
    # 1.0.9: normal 與 early 用的是不同的序列 —— normal 比較 PMO,early 比較
    # SIG(PMO 的 EMA10)。兩者的門檻要能分開鬆綁,所以各自有 scale;未指定時
    # 沿用共用的 threshold_scale(MES 波動校準走這條)。
    scale = abs(float(threshold_scale)) if threshold_scale else 1.0
    ns = abs(float(normal_scale)) if normal_scale else scale
    es = abs(float(early_scale)) if early_scale else scale
    n_long_th = EMAPMO_LONG_THRESHOLD * ns
    n_short_th = EMAPMO_SHORT_THRESHOLD * ns
    e_long_th = EMAPMO_LONG_THRESHOLD * es
    e_short_th = EMAPMO_SHORT_THRESHOLD * es
    long_th, short_th = n_long_th, n_short_th   # 說明頁沿用 normal 的門檻
    result: dict[str, Any] = {
        "pmo": None,
        "signal": None,
        "prev_pmo": None,
        "prev_signal": None,
        "p_gap_now": None,
        "p_gap_prev": None,
        "p_gap_prev2": None,
        "q_gap_now": None,
        "q_gap_prev": None,
        "q_gap_prev2": None,
        "normal_short": False,
        "normal_long": False,
        "early_short": False,
        "early_long": False,
    }
    if not closes:
        return result

    pmo, sig = calculate_emapmo_series(closes)
    if pmo and pmo[-1] is not None:
        result["pmo"] = float(pmo[-1])
    if sig and sig[-1] is not None:
        result["signal"] = float(sig[-1])
    if len(pmo) < 2 or len(sig) < 2:
        return result

    p0, p1 = pmo[-2], pmo[-1]
    s0, s1 = sig[-2], sig[-1]
    if None in (p0, p1, s0, s1):
        return result
    assert p0 is not None and p1 is not None and s0 is not None and s1 is not None
    result.update({
        "pmo": float(p1),
        "signal": float(s1),
        "prev_pmo": float(p0),
        "prev_signal": float(s0),
        "normal_short": bool(p1 > n_short_th and p1 < s1 and p0 >= s0),
        "normal_long": bool(p1 < n_long_th and p1 > s1 and p0 <= s0),
    })

    p_gap = [None if a is None or b is None else float(a - b) for a, b in zip(pmo, sig)]
    q_gap = [None if a is None or b is None else float(b - a) for a, b in zip(pmo, sig)]
    if len(p_gap) >= 3 and None not in (
        p_gap[-1], p_gap[-2], p_gap[-3], q_gap[-1], q_gap[-2], q_gap[-3],
    ):
        pn, pp, pp2 = p_gap[-1], p_gap[-2], p_gap[-3]
        qn, qp, qp2 = q_gap[-1], q_gap[-2], q_gap[-3]
        assert None not in (pn, pp, pp2, qn, qp, qp2)
        result.update({
            "p_gap_now": float(pn),
            "p_gap_prev": float(pp),
            "p_gap_prev2": float(pp2),
            "q_gap_now": float(qn),
            "q_gap_prev": float(qp),
            "q_gap_prev2": float(qp2),
            "early_short": bool(s1 > e_short_th and pn < pp and p1 > s1 and pp < pp2),
            "early_long": bool(s1 < e_long_th and qn < qp and p1 < s1 and qp < qp2),
        })
    return result


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
        self.session_va_filter = str(getattr(p, "factor_session_va_filter", "off") or "off").lower()
        if self.session_va_filter not in {"off", "outside"}:
            self.session_va_filter = "off"
        self.side_mode = str(getattr(p, "factor_side_mode", "all") or "all").lower()
        if self.side_mode not in {"all", "long_only", "short_only"}:
            self.side_mode = "all"
        # 1.0.9: PMO 進場門檻的波動縮放。1.0 = MNQ 原始行為;MES ≈ 0.55。
        # 見 calculate_emapmo_snapshot 與 scripts/emapmo_vol_calibration.py。
        self.pmo_threshold_scale = abs(float(
            getattr(p, "factor_pmo_threshold_scale", 1.0) or 1.0))
        # 1.0.9: normal(比 PMO)與 early(比 SIG)的門檻可分開鬆綁;
        # 0/None = 沿用共用的 pmo_threshold_scale。
        self.pmo_normal_scale = float(
            getattr(p, "factor_pmo_normal_scale", 0) or 0) or None
        self.pmo_early_scale = float(
            getattr(p, "factor_pmo_early_scale", 0) or 0) or None
        # 1.0.9: SL/TP 寬度上下限(ticks,單口)。見 _clamp_risk_reward。
        self.max_risk_ticks = float(getattr(p, "max_risk_ticks", 0) or 0)
        self.max_profit_ticks = float(getattr(p, "max_profit_ticks", 0) or 0)
        self.risk_cap_mode = str(
            getattr(p, "risk_cap_mode", "block") or "block").lower()
        if self.risk_cap_mode not in {"clamp", "block"}:
            self.risk_cap_mode = "clamp"
        self.sl_rule = str(getattr(p, "factor_sl_rule", "atr") or "atr").lower()
        self.tp_rule = str(getattr(p, "factor_tp_rule", "atr") or "atr").lower()
        self.sl_value = max(0.01, float(getattr(p, "factor_sl_value", 1.5) or 1.5))
        self.tp_value = max(0.01, float(getattr(p, "factor_tp_value", 2.0) or 2.0))
        self.max_hold_bars = max(0, int(getattr(p, "factor_max_hold_bars", 24) or 0))
        self.max_trades_per_day = max(0, int(getattr(p, "factor_max_trades_per_day", 3) or 0))
        self.warmup_bars = max(20, int(getattr(p, "factor_warmup_bars", 150) or 150))
        self.tick_size = max(0.0001, float(get_tick_size(getattr(p, "contract_id", ""))))
        self.trend_sl_ticks = max(1, int(getattr(p, "tr_sl_ticks", getattr(p, "sl_ticks", 50)) or 50))
        self.rr_ratio = max(1, min(6, int(getattr(p, "rr_ratio", 2) or 2)))
        self.value_area_pct = max(0.50, min(0.95, float(getattr(p, "value_area_pct", 0.80) or 0.80)))
        self._session_va = (
            _DevelopingSessionVa(tick_size=self.tick_size, value_area_pct=self.value_area_pct)
            if self.session_va_filter != "off"
            else None
        )

        self._bars: deque[Candle] = deque(
            maxlen=max(self.warmup_bars + 120, FACTOR_EMAPMO_HISTORY_BARS)
        )
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
        if self._session_va is not None:
            self._session_va.reset()

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
        # 1.0.9: 信號型策略的即時狀態(取代 trend 突破階段)—— 家族/TF、當前指標值、
        # ATR 狀態、是否正在觸發信號。自足計算,不依賴額外欄位(避免與其他改動衝突)。
        fam = self.signal_family.upper()
        n = len(self._bars)
        if self.signal_family == "emapmo":
            snapshot = self._emapmo_snapshot()
            pmo_s = self._format_indicator(snapshot.get("pmo"), 5)
            sig_s = self._format_indicator(snapshot.get("signal"), 5)
            if n < self.warmup_bars:
                missing = self.warmup_bars - n
                return "\n".join([
                    f"{fam} WARM-UP: {n}/{self.warmup_bars} completed "
                    f"{self.timeframe_minutes}m bars ({missing} remaining; trading disabled)",
                    f"SIG: {sig_s}",
                    f"PMO: {pmo_s}",
                ])

            atr = self._atr(14, 7)
            atr_s = f"ATR{self.timeframe_minutes}m: {atr:.2f}" if atr else "ATR: ?"
            direction = self._emapmo_direction(snapshot)
            lines = [
                f"{fam} {self.timeframe_minutes}m",
                f"SIG: {sig_s}",
                f"PMO: {pmo_s}",
                atr_s,
            ]
            if self._state in ("confirmed", "in_trade"):
                lines.append("State: POSITION / PENDING ORDER")
            elif direction is not None and self._side_allowed(direction):
                side = "LONG" if direction == Direction.BUY else "SHORT"
                lines.append(f"Signal: {side}")
            else:
                lines.append(self._emapmo_wait_label(snapshot))
                if direction is not None and not self._side_allowed(direction):
                    blocked = "LONG" if direction == Direction.BUY else "SHORT"
                    lines.append(f"Blocked: {blocked} signal ignored ({self.side_mode})")
            return "\n".join(lines)

        if n < self.warmup_bars:
            return f"{fam} WARM-UP: {n}/{self.warmup_bars} completed bars ({self.timeframe_minutes}m)"
        atr = self._atr(14, 7)
        atr_s = f"ATR{self.timeframe_minutes}m: {atr:.2f}" if atr else "ATR: ?"
        try:
            direction, detail = self._factor_direction()
        except Exception:
            direction, detail = None, {}
        detail = detail or {}
        if self.signal_family == "emapmo":
            ind = [
                f"SIG: {detail.get('signal', 0.0):.3f}",
                f"PMO: {detail.get('pmo', 0.0):.3f}",
            ]
        elif self.signal_family == "icefishball":
            ind = [
                f"KDJ-J: {detail.get('j', 0.0):.1f}",
                f"RSI: {detail.get('rsi', 0.0):.1f}",
            ]
        else:
            ind = [
                f"Momentum: {detail.get('mom_norm', 0.0):.2f}",
                f"Reversion Z: {detail.get('rev_z', 0.0):.2f}",
            ]
        if direction is not None:
            sig = "Signal: LONG" if direction == Direction.BUY else "Signal: SHORT"
        elif self._state in ("confirmed", "in_trade"):
            sig = "State: POSITION / PENDING ORDER"
        else:
            sig = "State: WAITING FOR SIGNAL"
        return "\n".join([f"{fam} {self.timeframe_minutes}m", *ind, atr_s, sig])

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
        if self._session_va is not None:
            self._session_va.update(candle)

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
        if rule == "fixed":
            width = value
        elif rule == "trend_ticks":
            width = self.trend_sl_ticks * self.tick_size
        elif rule == "trend_rr":
            width = self.trend_sl_ticks * self.tick_size * self.rr_ratio
        elif rule == "atr_blend":
            atr14 = self._atr(14, 7)
            if atr14 is None or atr14 <= 0:
                return None
            atr50 = self._atr(50, 25) or atr14
            width = ((atr14 + atr50) / 2.0) * value
        elif rule == "range15_pct":
            atr14 = self._atr(14, 7)
            if atr14 is None or atr14 <= 0:
                return None
            rng = self._range15() or atr14
            width = max(rng, atr14) * value
        else:
            atr14 = self._atr(14, 7)
            if atr14 is None or atr14 <= 0:
                return None
            width = atr14 * value
        return max(self.tick_size, float(width))

    def _session_va_allows(self, direction: Direction, signal_close: float) -> tuple[bool, dict[str, Any]]:
        if self.session_va_filter == "off":
            return True, {}
        tracker = self._session_va
        if tracker is None:
            return False, {"session_va_filter": "no_zone"}
        vah = float(tracker.vah if tracker.vah is not None else float("nan"))
        val = float(tracker.val if tracker.val is not None else float("nan"))
        if not math.isfinite(vah) or not math.isfinite(val):
            return False, {"session_va_filter": "no_va"}
        close = float(signal_close)
        if direction == Direction.SELL:
            ok = close > vah
        else:
            ok = close < val
        return ok, {
            "session_va_filter": self.session_va_filter,
            "session_vah": vah,
            "session_val": val,
            "session_poc": float(tracker.poc if tracker.poc is not None else close),
            "session_zone_id": tracker.session_id,
            "signal_close": close,
        }

    def _pmo_series(self) -> tuple[list[Optional[float]], list[Optional[float]]]:
        closes = [float(c.close) for c in self._bars]
        return calculate_emapmo_series(closes)

    @staticmethod
    def _format_indicator(value: Any, decimals: int = 3) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "?"
        if not math.isfinite(number):
            return "?"
        return f"{number:.{decimals}f}"

    @staticmethod
    def _condition_mark(ok: bool) -> str:
        return "[PASS]" if ok else "[WAIT]"

    def _emapmo_snapshot(self) -> dict[str, Any]:
        """One EMAPMO calculation shared by trading and the explainable status."""
        return calculate_emapmo_snapshot(
            [float(c.close) for c in self._bars], self.pmo_threshold_scale,
            self.pmo_normal_scale, self.pmo_early_scale)

    def _emapmo_direction(self, snapshot: dict[str, Any]) -> Optional[Direction]:
        use_normal = self.pmo_signal_mode in {"normal", "both"}
        use_early = self.pmo_signal_mode in {"early", "both"}
        if (use_normal and snapshot["normal_short"]) or (use_early and snapshot["early_short"]):
            return Direction.SELL
        if (use_normal and snapshot["normal_long"]) or (use_early and snapshot["early_long"]):
            return Direction.BUY
        return None

    def _emapmo_side_conditions(self, snapshot: dict[str, Any], direction: Direction) -> str:
        pmo = float(snapshot.get("pmo") or 0.0)
        signal = float(snapshot.get("signal") or 0.0)
        prev_pmo = float(snapshot.get("prev_pmo") or 0.0)
        prev_signal = float(snapshot.get("prev_signal") or 0.0)
        side = "LONG" if direction == Direction.BUY else "SHORT"
        # 1.0.9: 顯示實際生效的門檻(隨 pmo_threshold_scale 縮放),否則
        # 非 MNQ 商品的說明頁會顯示與實際判定不符的數字。
        long_th = EMAPMO_LONG_THRESHOLD * self.pmo_threshold_scale
        short_th = EMAPMO_SHORT_THRESHOLD * self.pmo_threshold_scale

        if direction == Direction.BUY:
            normal_threshold = pmo < long_th
            normal_cross = pmo > signal and prev_pmo <= prev_signal
            normal = "\n".join([
                f"{side} NORMAL",
                f"PMO < {long_th:.5f}: current={pmo:.5f} {self._condition_mark(normal_threshold)}",
                f"PMO crosses above SIG: previous {prev_pmo:.5f} <= {prev_signal:.5f}; "
                f"current {pmo:.5f} > {signal:.5f} {self._condition_mark(normal_cross)}",
            ])
            gaps = (
                snapshot.get("q_gap_prev2"),
                snapshot.get("q_gap_prev"),
                snapshot.get("q_gap_now"),
            )
            threshold = signal < long_th
            relation = pmo < signal
            gap_ok = all(value is not None for value in gaps) and float(gaps[2]) < float(gaps[1]) < float(gaps[0])
            gap_s = " -> ".join(self._format_indicator(value, 5) for value in gaps)
            early = "\n".join([
                f"{side} EARLY",
                f"SIG < {long_th:.5f}: current={signal:.5f} {self._condition_mark(threshold)}",
                f"PMO < SIG: {pmo:.5f} < {signal:.5f} {self._condition_mark(relation)}",
                f"SIG - PMO gap shrinking: {gap_s} {self._condition_mark(gap_ok)}",
            ])
        else:
            normal_threshold = pmo > short_th
            normal_cross = pmo < signal and prev_pmo >= prev_signal
            normal = "\n".join([
                f"{side} NORMAL",
                f"PMO > {short_th:.5f}: current={pmo:.5f} {self._condition_mark(normal_threshold)}",
                f"PMO crosses below SIG: previous {prev_pmo:.5f} >= {prev_signal:.5f}; "
                f"current {pmo:.5f} < {signal:.5f} {self._condition_mark(normal_cross)}",
            ])
            gaps = (
                snapshot.get("p_gap_prev2"),
                snapshot.get("p_gap_prev"),
                snapshot.get("p_gap_now"),
            )
            threshold = signal > short_th
            relation = pmo > signal
            gap_ok = all(value is not None for value in gaps) and float(gaps[2]) < float(gaps[1]) < float(gaps[0])
            gap_s = " -> ".join(self._format_indicator(value, 5) for value in gaps)
            early = "\n".join([
                f"{side} EARLY",
                f"SIG > {short_th:.5f}: current={signal:.5f} {self._condition_mark(threshold)}",
                f"SIG < PMO: {signal:.5f} < {pmo:.5f} {self._condition_mark(relation)}",
                f"PMO - SIG gap shrinking: {gap_s} {self._condition_mark(gap_ok)}",
            ])

        if self.pmo_signal_mode == "normal":
            return normal
        if self.pmo_signal_mode == "early":
            return early
        return f"{normal}\nOR\n{early}"

    def _emapmo_wait_label(self, snapshot: dict[str, Any]) -> str:
        directions = []
        if self.side_mode in {"all", "long_only"}:
            directions.append(Direction.BUY)
        if self.side_mode in {"all", "short_only"}:
            directions.append(Direction.SELL)
        conditions = "\nOR\n".join(
            self._emapmo_side_conditions(snapshot, direction) for direction in directions
        )
        return f"Waiting for:\n{conditions}"

    def _factor_direction(self) -> tuple[Optional[Direction], dict[str, Any]]:
        if len(self._bars) < self.warmup_bars:
            return None, {}
        bars = list(self._bars)
        closes = [float(c.close) for c in bars]

        if self.signal_family == "emapmo":
            snapshot = self._emapmo_snapshot()
            detail = {
                "pmo": snapshot.get("pmo"),
                "signal": snapshot.get("signal"),
            }
            return self._emapmo_direction(snapshot), detail

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

    def _clamp_risk_reward(
        self, risk: float, reward: float,
    ) -> tuple[Optional[float], float, str]:
        """1.0.9: 把 ATR 推導出的 SL/TP 寬度夾進上下限,並維持 SL:TP 比例。

        用途是 prop firm 的兩條線:單筆風險(maxDD)與單日獲利佔比
        (consistency rule —— 單日賺太多會推高通關/出金門檻)。

        兩個上限都以 **ticks(單口)** 計:
            max_risk_ticks    SL 寬度上限
            max_profit_ticks  TP 寬度上限
        取較嚴的那個縮放因子,同時乘在 risk 與 reward 上,所以 RR 不變 ——
        只是整組 SL/TP 等比縮小。

        ⚠️ 夾窄 SL 不是免費的:停損離進場更近,高波動時被掃出場的機率上升。
        改動前務必用 scripts/clamp_cap_study.py 回測比較。

        risk_cap_mode:
            "clamp"(預設,有設上限時)  縮放到上限,照樣進場
            "block"                    超過上限就跳過這個訊號
        兩個上限都沒設 → 原樣返回,行為與 1.0.8 完全相同。
        """
        max_risk = self.max_risk_ticks
        max_profit = self.max_profit_ticks
        if not max_risk and not max_profit:
            return risk, reward, ""

        tick = self.tick_size
        risk_t = risk / tick
        reward_t = reward / tick
        scale = 1.0
        why = []
        if max_risk and risk_t > max_risk:
            scale = min(scale, max_risk / risk_t)
            why.append(f"risk {risk_t:.0f}t>{max_risk:g}t")
        if max_profit and reward_t > max_profit:
            scale = min(scale, max_profit / reward_t)
            why.append(f"reward {reward_t:.0f}t>{max_profit:g}t")
        if scale >= 1.0:
            return risk, reward, ""
        if self.risk_cap_mode == "block":
            return None, reward, "blocked"
        return risk * scale, reward * scale, " CLAMP(" + ", ".join(why) + ")"

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
        risk, reward, clamp_note = self._clamp_risk_reward(risk, reward)
        if risk is None:
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
        filter_label = "" if self.session_va_filter == "off" else f" VA={self.session_va_filter}"
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
                f"SL={self.sl_rule}:{self.sl_value:g} TP={self.tp_rule}:{self.tp_value:g}{filter_label}"
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
                    f"va_filter:{self.session_va_filter}",
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
        ok, va_detail = self._session_va_allows(direction, float(final_bar.close))
        if not ok:
            return None
        detail.update(va_detail)
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
