"""1.0.9: 公開文獻策略研究庫 —— 用現有 BacktestEngine 回測外部策略構想。

每個類別都實作與 fade.py / factor.py 相同的介面(observe / evaluate /
notify_* / reset),所以能直接塞進 BacktestEngine 的策略插槽,沿用同一套
成交假設、佣金費用、時段過濾、每日上限與 trail —— 換句話說,這些外部
策略跟 BEST / OR15 是在完全相同的條件下被比較。

收錄的都是「規則明確、可用 1m OHLCV 重現」的公開策略族。實作的是**規則
本身**(公開知識),不是任何特定來源的程式碼。

  ORB          Opening Range Breakout —— 開盤 N 分鐘區間突破後順勢
  VWAPREV      日內 VWAP 偏離回歸(偏離 k 倍 σ 後反向)
  IBS          Internal Bar Strength = (C-L)/(H-L),極低時做多
  RSI2         Connors RSI(2) 超賣反轉
  GAPFADE      隔夜跳空在 RTH 開盤後反向回補
  INTRAMOM     Market Intraday Momentum —— 開盤半小時報酬預測尾盤方向
  DONCHIAN     Donchian 通道突破(海龜式,日內版)
  BBREV        Bollinger 帶外回歸

風險口徑統一用 atr_blend(與 FACTOR 相同),讓 SL/TP 寬度可比較。

用法見 scripts/public_strategy_research.py。
"""
from __future__ import annotations

import math
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from backend.db.models import Candle, Direction, StrategyType, TradeSignal, get_tick_size

_UTC = timezone.utc


def _utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=_UTC) if ts.tzinfo is None else ts.astimezone(_UTC)


class _ResearchBase:
    """共用骨架:高階 K 棒聚合、ATR、日界線、引擎介面樣板。"""

    PENDING_TIMEOUT_CANDLES = 1
    NAME = "BASE"

    def __init__(self, params: Any):
        p = params
        self.params = p
        self.tick_size = get_tick_size(getattr(p, "contract_id", "") or "")
        self.tf_minutes = int(getattr(p, "research_tf_minutes", 5) or 5)
        self.sl_atr = float(getattr(p, "factor_sl_value", 2.5) or 2.5)
        self.rr = float(getattr(p, "rr_ratio", 3) or 3)
        self.max_trades_per_day = int(getattr(p, "factor_max_trades_per_day", 3) or 0)
        self.side_mode = str(getattr(p, "factor_side_mode", "all") or "all").lower()
        self._bars: deque = deque(maxlen=400)      # 已完成的高階 K 棒
        self._cur: Optional[list] = None           # [ts, o, h, l, c, v]
        self._daily: dict = {}
        self._state = "idle"
        self.raw_state = "idle"

    # ── 引擎介面樣板 ──────────────────────────────────────
    def reset(self) -> None:
        self._state = "idle"

    def reset_state_only(self) -> None:
        self.reset()

    def reset_breakout_confirmation(self) -> None:
        self.reset()

    def notify_trade_closed(self, exit_reason: str) -> None:
        self._state = "idle"

    def notify_order_cancelled(self) -> None:
        self._state = "idle"

    def observe(self, candle: Candle, zones=None, is_mature: bool = True) -> None:
        self._roll(candle)

    def get_phase_label(self) -> str:
        return f"{self.NAME} {self._state}"

    # ── 共用工具 ──────────────────────────────────────────
    def _roll(self, candle: Candle) -> Optional[list]:
        """把 1m 併進高階 K 棒;回傳剛完成的那根(若有)。"""
        ts = _utc(candle.timestamp).replace(second=0, microsecond=0)
        key = ts - timedelta(minutes=ts.minute % self.tf_minutes)
        done = None
        if self._cur is None:
            self._cur = [key, candle.open, candle.high, candle.low, candle.close, candle.volume or 0]
        elif key != self._cur[0]:
            done = list(self._cur)
            self._bars.append(done)
            self._cur = [key, candle.open, candle.high, candle.low, candle.close, candle.volume or 0]
        else:
            c = self._cur
            c[2] = max(c[2], candle.high); c[3] = min(c[3], candle.low)
            c[4] = candle.close; c[5] += (candle.volume or 0)
        return done

    def _atr(self, length: int = 14) -> Optional[float]:
        bars = list(self._bars)
        if len(bars) < max(7, length // 2):
            return None
        seg = bars[-length:]
        trs = []
        for i, b in enumerate(seg):
            pc = seg[i - 1][4] if i > 0 else b[4]
            trs.append(max(b[2] - b[3], abs(b[2] - pc), abs(b[3] - pc)))
        return sum(trs) / len(trs) if trs else None

    def _atr_blend(self) -> Optional[float]:
        a14, a50 = self._atr(14), self._atr(50)
        if a14 is None or a14 <= 0:
            return None
        return (a14 + (a50 or a14)) / 2.0

    def _trade_date(self, ts: datetime) -> str:
        from backend.strategy.factor import _topstep_trade_date
        return _topstep_trade_date(ts)

    def _side_ok(self, d: Direction) -> bool:
        if self.side_mode == "long_only":
            return d == Direction.BUY
        if self.side_mode == "short_only":
            return d == Direction.SELL
        return True

    def _round(self, price: float) -> float:
        return round(float(price) / self.tick_size) * self.tick_size

    def _make(self, candle: Candle, direction: Direction, reason: str,
              width: Optional[float] = None) -> Optional[TradeSignal]:
        if not self._side_ok(direction):
            return None
        d = self._trade_date(candle.timestamp)
        if self.max_trades_per_day and self._daily.get(d, 0) >= self.max_trades_per_day:
            return None
        risk = width if width is not None else self._atr_blend()
        if risk is None or risk <= 0:
            return None
        risk *= self.sl_atr
        entry = self._round(candle.close)
        if direction == Direction.BUY:
            sl, tp = self._round(entry - risk), self._round(entry + risk * self.rr)
        else:
            sl, tp = self._round(entry + risk), self._round(entry - risk * self.rr)
        if entry == sl or entry == tp:
            return None
        self._daily[d] = self._daily.get(d, 0) + 1
        self._state = "confirmed"
        return TradeSignal(
            strategy=StrategyType.TREND_FOLLOW,
            direction=direction,
            entry_price=entry, sl_price=sl, tp_price=tp,
            zone_id=f"{self.NAME}:{d}:{_utc(candle.timestamp).isoformat()}",
            zone_source="research",
            reason=f"{self.NAME} | {reason}",
            order_type="market",
        )

    def evaluate(self, candle: Candle, zones=None, is_mature: bool = True):
        raise NotImplementedError


# ── 1. Opening Range Breakout ────────────────────────────────

class ORBreakout(_ResearchBase):
    """開盤 N 分鐘區間形成後,收盤突破區間即順勢進場(每個方向每日一次)。

    與現有 OR15 的差異:OR15 做的是「假突破反向」,這裡做的是**順勢突破**。
    """
    NAME = "ORB"

    def __init__(self, params):
        super().__init__(params)
        self.or_minutes = int(getattr(params, "research_or_minutes", 15) or 15)
        self._day = None
        self._hi = self._lo = None
        self._open_ts = None
        self._done_dirs: set = set()

    def evaluate(self, candle, zones=None, is_mature=True):
        self._roll(candle)
        ts = _utc(candle.timestamp)
        d = self._trade_date(ts)
        if d != self._day:
            self._day, self._hi, self._lo, self._open_ts = d, None, None, ts
            self._done_dirs = set()
        elapsed = (ts - self._open_ts).total_seconds() / 60.0
        if elapsed <= self.or_minutes:
            self._hi = candle.high if self._hi is None else max(self._hi, candle.high)
            self._lo = candle.low if self._lo is None else min(self._lo, candle.low)
            return None
        if self._hi is None or self._lo is None:
            return None
        if candle.close > self._hi and "up" not in self._done_dirs:
            self._done_dirs.add("up")
            return self._make(candle, Direction.BUY, f"break above OR{self.or_minutes} {self._hi:.2f}")
        if candle.close < self._lo and "dn" not in self._done_dirs:
            self._done_dirs.add("dn")
            return self._make(candle, Direction.SELL, f"break below OR{self.or_minutes} {self._lo:.2f}")
        return None


# ── 2. VWAP 偏離回歸 ─────────────────────────────────────────

class VwapReversion(_ResearchBase):
    """日內 VWAP 偏離 k 倍標準差後反向;經典的日內均值回歸基準。"""
    NAME = "VWAPREV"

    def __init__(self, params):
        super().__init__(params)
        self.k = float(getattr(params, "research_k", 2.0) or 2.0)
        self._day = None
        self._pv = self._vol = 0.0
        self._sq = 0.0
        self._n = 0

    def evaluate(self, candle, zones=None, is_mature=True):
        self._roll(candle)
        d = self._trade_date(candle.timestamp)
        if d != self._day:
            self._day, self._pv, self._vol, self._sq, self._n = d, 0.0, 0.0, 0.0, 0
        tp = (candle.high + candle.low + candle.close) / 3.0
        v = float(candle.volume or 0) or 1.0
        self._pv += tp * v; self._vol += v
        vwap = self._pv / self._vol
        self._sq += (tp - vwap) ** 2; self._n += 1
        if self._n < 30:
            return None
        sd = math.sqrt(self._sq / self._n)
        if sd <= 0:
            return None
        dev = (candle.close - vwap) / sd
        if dev <= -self.k:
            return self._make(candle, Direction.BUY, f"{dev:.2f}σ below VWAP {vwap:.2f}")
        if dev >= self.k:
            return self._make(candle, Direction.SELL, f"{dev:+.2f}σ above VWAP {vwap:.2f}")
        return None


# ── 3. Internal Bar Strength ─────────────────────────────────

class IbsReversion(_ResearchBase):
    """IBS = (C-L)/(H-L)。極低 → 收在當根低點附近 → 短線反彈傾向。"""
    NAME = "IBS"

    def __init__(self, params):
        super().__init__(params)
        self.lo = float(getattr(params, "research_ibs_low", 0.10) or 0.10)
        self.hi = float(getattr(params, "research_ibs_high", 0.90) or 0.90)

    def evaluate(self, candle, zones=None, is_mature=True):
        done = self._roll(candle)
        if not done or len(self._bars) < 50:
            return None
        _, o, h, l, c, _ = done
        if h <= l:
            return None
        ibs = (c - l) / (h - l)
        if ibs <= self.lo:
            return self._make(candle, Direction.BUY, f"IBS {ibs:.2f} <= {self.lo}")
        if ibs >= self.hi:
            return self._make(candle, Direction.SELL, f"IBS {ibs:.2f} >= {self.hi}")
        return None


# ── 4. RSI(2) 超賣反轉(Connors)──────────────────────────────

class Rsi2Reversion(_ResearchBase):
    """RSI(2) 極端值反轉 —— Connors 短線均值回歸的標準設定。"""
    NAME = "RSI2"

    def __init__(self, params):
        super().__init__(params)
        self.length = int(getattr(params, "research_rsi_len", 2) or 2)
        self.lo = float(getattr(params, "research_rsi_low", 5.0) or 5.0)
        self.hi = float(getattr(params, "research_rsi_high", 95.0) or 95.0)

    def _rsi(self) -> Optional[float]:
        bars = list(self._bars)
        n = self.length
        if len(bars) < n + 1:
            return None
        gains = losses = 0.0
        for i in range(len(bars) - n, len(bars)):
            ch = bars[i][4] - bars[i - 1][4]
            if ch >= 0:
                gains += ch
            else:
                losses -= ch
        if losses == 0:
            return 100.0
        rs = (gains / n) / (losses / n)
        return 100.0 - 100.0 / (1.0 + rs)

    def evaluate(self, candle, zones=None, is_mature=True):
        done = self._roll(candle)
        if not done or len(self._bars) < 50:
            return None
        r = self._rsi()
        if r is None:
            return None
        if r <= self.lo:
            return self._make(candle, Direction.BUY, f"RSI{self.length} {r:.1f} <= {self.lo}")
        if r >= self.hi:
            return self._make(candle, Direction.SELL, f"RSI{self.length} {r:.1f} >= {self.hi}")
        return None


# ── 5. 隔夜跳空回補 ──────────────────────────────────────────

class GapFade(_ResearchBase):
    """RTH 開盤相對前一日 RTH 收盤的跳空,達門檻後反向做回補。"""
    NAME = "GAPFADE"

    RTH_OPEN = (13, 30)     # UTC
    RTH_CLOSE = (20, 0)

    def __init__(self, params):
        super().__init__(params)
        self.min_atr = float(getattr(params, "research_gap_atr", 0.5) or 0.5)
        self._prev_close = None
        self._day = None
        self._fired = False
        self._last_rth_close = None

    def evaluate(self, candle, zones=None, is_mature=True):
        self._roll(candle)
        ts = _utc(candle.timestamp)
        d = self._trade_date(ts)
        if d != self._day:
            self._day, self._fired = d, False
            self._prev_close = self._last_rth_close
        hm = (ts.hour, ts.minute)
        if hm >= self.RTH_CLOSE:
            self._last_rth_close = candle.close
        if self._fired or self._prev_close is None:
            return None
        if hm < self.RTH_OPEN or hm > (self.RTH_OPEN[0], self.RTH_OPEN[1] + 15):
            return None
        atr = self._atr_blend()
        if not atr or atr <= 0:
            return None
        gap = candle.open - self._prev_close
        if abs(gap) < self.min_atr * atr:
            return None
        self._fired = True
        d_ = Direction.SELL if gap > 0 else Direction.BUY
        return self._make(candle, d_, f"gap {gap:+.2f} ({gap/atr:+.2f} ATR) fade")


# ── 6. Market Intraday Momentum ──────────────────────────────

class IntradayMomentum(_ResearchBase):
    """開盤第一段的報酬方向,用來預測尾盤同向 —— 日內動能效應。"""
    NAME = "INTRAMOM"

    def __init__(self, params):
        super().__init__(params)
        self.first_minutes = int(getattr(params, "research_first_minutes", 30) or 30)
        self.entry_hour = int(getattr(params, "research_entry_hour", 19) or 19)  # UTC
        self._day = None
        self._open_px = None
        self._first_ret = None
        self._open_ts = None
        self._fired = False

    def evaluate(self, candle, zones=None, is_mature=True):
        self._roll(candle)
        ts = _utc(candle.timestamp)
        d = self._trade_date(ts)
        if d != self._day:
            self._day, self._open_px, self._first_ret = d, None, None
            self._open_ts, self._fired = ts, False
        if self._open_px is None:
            self._open_px = candle.open
        elapsed = (ts - self._open_ts).total_seconds() / 60.0
        if self._first_ret is None and elapsed >= self.first_minutes:
            self._first_ret = (candle.close - self._open_px) / self._open_px
        if self._fired or self._first_ret is None:
            return None
        if ts.hour < self.entry_hour:
            return None
        self._fired = True
        if abs(self._first_ret) < 1e-5:
            return None
        d_ = Direction.BUY if self._first_ret > 0 else Direction.SELL
        return self._make(candle, d_, f"first {self.first_minutes}m ret {self._first_ret*100:+.2f}%")


# ── 7. Donchian 通道突破 ─────────────────────────────────────

class DonchianBreakout(_ResearchBase):
    """N 根高階 K 棒的最高/最低突破 —— 海龜式突破的日內版。"""
    NAME = "DONCHIAN"

    def __init__(self, params):
        super().__init__(params)
        self.lookback = int(getattr(params, "research_lookback", 20) or 20)

    def evaluate(self, candle, zones=None, is_mature=True):
        done = self._roll(candle)
        if not done or len(self._bars) < self.lookback + 5:
            return None
        seg = list(self._bars)[-self.lookback - 1:-1]
        hi = max(b[2] for b in seg); lo = min(b[3] for b in seg)
        c = done[4]
        if c > hi:
            return self._make(candle, Direction.BUY, f"break {self.lookback}-bar high {hi:.2f}")
        if c < lo:
            return self._make(candle, Direction.SELL, f"break {self.lookback}-bar low {lo:.2f}")
        return None


# ── 8. Bollinger 帶外回歸 ────────────────────────────────────

class BollingerReversion(_ResearchBase):
    """收盤跌破/突破 N 期 k 倍標準差通道後反向。"""
    NAME = "BBREV"

    def __init__(self, params):
        super().__init__(params)
        self.length = int(getattr(params, "research_bb_len", 20) or 20)
        self.k = float(getattr(params, "research_k", 2.0) or 2.0)

    def evaluate(self, candle, zones=None, is_mature=True):
        done = self._roll(candle)
        if not done or len(self._bars) < self.length + 5:
            return None
        seg = [b[4] for b in list(self._bars)[-self.length:]]
        mean = sum(seg) / len(seg)
        var = sum((x - mean) ** 2 for x in seg) / len(seg)
        sd = math.sqrt(var)
        if sd <= 0:
            return None
        c = done[4]
        if c < mean - self.k * sd:
            return self._make(candle, Direction.BUY, f"below BB({self.length},{self.k:g})")
        if c > mean + self.k * sd:
            return self._make(candle, Direction.SELL, f"above BB({self.length},{self.k:g})")
        return None


RESEARCH_STRATEGIES = {
    "ORB": ORBreakout,
    "VWAPREV": VwapReversion,
    "IBS": IbsReversion,
    "RSI2": Rsi2Reversion,
    "GAPFADE": GapFade,
    "INTRAMOM": IntradayMomentum,
    "DONCHIAN": DonchianBreakout,
    "BBREV": BollingerReversion,
}
