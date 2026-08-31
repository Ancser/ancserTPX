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
from backend.strategy.session_filter import as_new_york, rth_session_date

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
        # 引擎對「不可交易」的 K 棒只呼叫 observe(),對可交易的才呼叫 evaluate()。
        # 需要跨時段狀態的策略(ONCONT 要 RTH 開收、GAPFADE 要前日 RTH 收盤)
        # 必須在這裡也更新,否則它們永遠看不到 RTH 的 K 棒。
        self._roll(candle)
        self._track(candle)

    def _track(self, candle: Candle) -> None:
        """跨時段狀態記錄。預設不做事;需要的子類覆寫,並在 evaluate 開頭呼叫。"""
        return None

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

    def _make_at(self, candle: Candle, direction: Direction,
                 sl_px: float, tp_px: float, reason: str) -> Optional[TradeSignal]:
        """1.0.9: SL/TP 用**絕對價位**,不是 entry ± ATR 倍數。

        給 fib 型策略用 —— 停損停利掛在 fib 層級本身(0.75 / 0.90),
        風險大小自然隨當天走勢幅度縮放,而不是隨波動率指標縮放。
        進場仍是觸價後的市價(candle.close),名目風險與實際會略有出入,
        這是誠實的:限價單成交假設無法在回測裡驗證。
        """
        if not self._side_ok(direction):
            return None
        d = self._trade_date(candle.timestamp)
        if self.max_trades_per_day and self._daily.get(d, 0) >= self.max_trades_per_day:
            return None
        entry = self._round(candle.close)
        sl, tp = self._round(sl_px), self._round(tp_px)
        # 觸價瞬間市價進場,可能已穿過 SL 或還沒到 TP 的正確側 —— 兩者都廢單
        if direction == Direction.BUY:
            if not (sl < entry < tp):
                return None
        else:
            if not (tp < entry < sl):
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

    RTH_OPEN = (9, 30)      # America/New_York
    RTH_CLOSE = (16, 0)

    def __init__(self, params):
        super().__init__(params)
        self.min_atr = float(getattr(params, "research_gap_atr", 0.5) or 0.5)
        self._prev_close = None
        self._day = None
        self._fired = False
        self._last_rth_close = None

    def _track(self, candle) -> None:
        ts = _utc(candle.timestamp)
        local = as_new_york(ts)
        d = self._trade_date(ts)
        if d != self._day:
            self._day, self._fired = d, False
            self._prev_close = self._last_rth_close
        if (local.hour, local.minute) >= self.RTH_CLOSE:
            self._last_rth_close = candle.close

    def evaluate(self, candle, zones=None, is_mature=True):
        self._roll(candle)
        self._track(candle)
        local = as_new_york(candle.timestamp)
        hm = (local.hour, local.minute)
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

class MomentumContinuation(_ResearchBase):
    """交易日開頭第一段的報酬方向 → 同向進場(日內動能延續)。

    ⚠️ 命名沿用文獻的 Market Intraday Momentum,但**實際量測的不是 RTH 開盤
    後 30 分鐘**。這裡的「開盤」是 Topstep 17:00 CT 交易日邊界
    (夏令 22:00 UTC、冬令 23:00 UTC),不是 RTH 開盤。

    entry_hour 只在「該交易日 22:00 沒有 K 棒」(週末/假日順延)時才會真正
    生效 —— 那時 30 分鐘標記會落在更晚,才需要等到指定小時。所以密網格裡
    17/18/19/20 的通過率幾乎相同(28/26/27/28),不是因為它穩健,而是因為它
    大部分時候不起作用。

    驗證結果見 docs/1.0.9_RESEARCH_FINDINGS.md:960 格密網格 × 雙商品交集
    22 組、鄰域 6/7 在門檻之上,是唯一在 MNQ 與 MES 上 MC+走查全通過的策略。
    """
    NAME = "MOMENTUM"

    def __init__(self, params):
        super().__init__(params)
        # 正式參數名優先;research_* 是研究腳本用的別名
        self.first_minutes = int(
            getattr(params, "momentum_first_minutes", None)
            or getattr(params, "research_first_minutes", 30) or 30)
        self.entry_hour = int(
            getattr(params, "momentum_entry_hour", None)
            or getattr(params, "research_entry_hour", 18) or 18)   # UTC
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


# ── 9. 白天方向 → 夜盤延續 / 反向 ────────────────────────────

class OvernightContinuation(_ResearchBase):
    """白天(RTH)漲跌定方向,夜盤開盤進場。

    時區(夏令 PDT = UTC-7):
        09:30 ET   RTH 開盤,記錄開盤價
        16:00 ET   RTH 收盤 → 白天方向定案
        18:00 ET   夜盤(ASIA)開盤 → 進場

    ⚠️ 結構限制:週五 21:00 UTC 收盤後到週日才開市,**週五沒有當晚夜盤**,
    所以本策略每週只有週一~週四 4 個機會,樣本天生比別的策略少 20%。

    research_reverse=True 時改成反向做(白天漲→夜盤做空)。實測 MNQ 2026-05~08
    這段(單邊跌市)顯示夜盤偏均值回歸而非動能延續,但那可能只是市況產物 ——
    所以兩個方向都要能測,交給 G5 跨商品去分辨。
    """
    NAME = "ONCONT"

    RTH_OPEN = (9, 30)
    RTH_CLOSE = (16, 0)
    NIGHT_OPEN = (18, 0)

    def __init__(self, params):
        super().__init__(params)
        self.min_move_atr = float(getattr(params, "research_move_atr", 0.5) or 0.5)
        self.reverse = bool(getattr(params, "research_reverse", False))
        # 1.0.9: 風險寬度基準。門檻(min_move_atr)一律用日 ATR —— 那是在
        # 描述「白天走了多大」;但 SL/TP 寬度可以另外選。日 ATR 約 517 點、
        # 5m atr_blend 約 30 點,差 17 倍,同一個 sl_value 是完全不同的策略。
        # SESSFIB 換基準後 PF 從 0.66 翻到 2.83,所以這個必須能分開測。
        self.risk_basis = str(
            getattr(params, "research_risk_basis", "daily") or "daily").lower()
        self._day = None
        self._rth_open_px = None
        self._rth_close_px = None
        self._rth_hi = self._rth_lo = None
        self._day_ranges: deque = deque(maxlen=14)
        self._fired = False

    def _daily_atr(self) -> Optional[float]:
        if len(self._day_ranges) < 5:
            return None
        return sum(self._day_ranges) / len(self._day_ranges)

    def _track(self, candle: Candle) -> None:
        local = as_new_york(candle.timestamp)
        hm = (local.hour, local.minute)
        d = local.date()
        if d != self._day:
            # 換日:把昨天的 RTH 幅度存進 ATR 樣本
            if self._rth_hi is not None and self._rth_lo is not None:
                self._day_ranges.append(self._rth_hi - self._rth_lo)
            self._day = d
            self._rth_open_px = self._rth_close_px = None
            self._rth_hi = self._rth_lo = None
            self._fired = False
        if self.RTH_OPEN <= hm < self.RTH_CLOSE:
            if self._rth_open_px is None:
                self._rth_open_px = candle.open
            self._rth_hi = candle.high if self._rth_hi is None else max(self._rth_hi, candle.high)
            self._rth_lo = candle.low if self._rth_lo is None else min(self._rth_lo, candle.low)
            self._rth_close_px = candle.close

    def evaluate(self, candle: Candle, zones=None, is_mature: bool = True):
        self._roll(candle)
        self._track(candle)
        local = as_new_york(candle.timestamp)
        hm = (local.hour, local.minute)
        if self.RTH_OPEN <= hm < self.RTH_CLOSE:
            return None

        if hm < self.NIGHT_OPEN or self._fired:
            return None
        if self._rth_open_px is None or self._rth_close_px is None:
            return None
        atr = self._daily_atr()
        if not atr or atr <= 0:
            return None

        move = self._rth_close_px - self._rth_open_px
        if abs(move) / atr < self.min_move_atr:
            return None
        self._fired = True

        up = move > 0
        if self.reverse:
            up = not up
        direction = Direction.BUY if up else Direction.SELL
        width = atr if self.risk_basis == "daily" else self._atr_blend()
        if width is None or width <= 0:
            return None
        return self._make(candle, direction,
                          f"RTH {move:+.1f} ({move/atr:+.2f} ATR)"
                          + (" REVERSED" if self.reverse else "")
                          + f" [{self.risk_basis}]",
                          width=width)


# ── 10. NY session Fib 回撤掛單 ──────────────────────────────

class BetaFibRetrace(_ResearchBase):
    """完整 NY session(RTH)漲跌 → 在 Fib 回撤位掛限價單,順著 session 方向。

    與 INTRAMOM 的差別(這是使用者原本想要的版本):
      INTRAMOM  量夜盤前 30 分鐘 → 到時間就市價進場
      本策略    量**整個 RTH**(09:30–16:00 ET)→ 在 0.8 回撤位**掛單等**

    規則:
      1. RTH 收盤定案 move = rth_close - rth_open
      2. |move| 需 > min_move_atr × 日ATR(否則當天不掛)
      3. 掛單價 = rth_close - retrace_frac × move
         retrace_frac=0.2 → 守住 fib 0.8(價格回吐 20% 漲幅);
         這正是「回踩到 0.8 支撐」的口語說法。
      4. 夜盤(16:00 ET → 隔日 09:30 ET)內價格觸及該價位才成交,順 session 方向:
         session 漲 → 回踩到位做多;session 跌 → 反彈到位做空
      5. 沒觸及就當天作廢(限價單的本質:不追價,寧可漏單)

    ⚠️ 觸價後走**市價**進場(order_type="market"),不是在掛單價成交。理由:
    靜止限價單的漏單風險在回測裡無法誠實建模(你不知道排隊位置),而市價
    版本會吃滿實測 14t 往返滑價 —— 寧可低估績效也不要高估。
    """
    NAME = "BETAFIB"

    RTH_OPEN = (9, 30)
    RTH_CLOSE = (16, 0)

    def __init__(self, params):
        super().__init__(params)
        # 1.0.9: 上線後參數改叫 sessfib_*(UI 直接可調),research_* 保留給
        # scripts/ 的掃描腳本當 fallback,兩邊都能跑同一份程式碼。
        # UI 給的是「進場 fib」(0.618),內部沿用 retrace_frac = 1 - entry_fib。
        _entry = getattr(params, "betafib_entry_fib", None)
        if _entry is not None:
            self.retrace_frac = 1.0 - float(_entry)
        else:
            self.retrace_frac = float(getattr(params, "research_retrace_frac", 0.2) or 0.2)
        self.min_move_atr = float(getattr(params, "research_min_move_atr", 0.3) or 0.3)
        # 1.0.9: 風險寬度基準 —— 「日 ATR」(RTH 幅度 14 日均,MNQ 約 517 點)
        # 與 BEST 用的 5m atr_blend(中位 30.4 點)差 17 倍。同一個 sl_value
        # 在兩種基準下是完全不同的策略,必須能分開測。
        #   "daily"     日 ATR(隔夜部位的自然尺度)
        #   "atr_blend" 5m atr_blend —— 與 BEST / FACTOR 完全同口徑
        #   "fib"       SL/TP 直接掛在 fib 層級 —— 風險隨當天走勢幅度縮放,
        #               不隨波動率指標縮放
        self.risk_basis = str(
            getattr(params, "betafib_risk_basis", None)
            or getattr(params, "research_risk_basis", "daily") or "daily").lower()
        # 1.0.9: fib 基準專用。層級以 rth_open 為 0、rth_close 為 1 度量:
        #   price(f) = rth_open + f × move
        # 進場 = 1 - retrace_frac(預設 0.8);SL 更深(0.75/0.70);TP 更淺(0.90)
        # 1.0.10: 這兩個先前寫死且沒有 UI —— risk_basis="fib" 等於沒有 TP 選項。
        self.sl_fib = float(
            getattr(params, "betafib_sl_fib", None)
            or getattr(params, "research_sl_fib", 0.75) or 0.75)
        self.tp_fib = float(
            getattr(params, "betafib_tp_fib", None)
            or getattr(params, "research_tp_fib", 0.90) or 0.90)
        # 1.0.9: 觸發門檻的第二種寫法 —— 當日 RTH 漲跌的**百分比**。
        # ATR 倍數會隨波動率漂移,「今天漲了 0.4%」則是固定、可直接對話的口徑。
        self.min_move_pct = float(
            getattr(params, "betafib_min_move_pct", None)
            or getattr(params, "research_min_move_pct", 0.0) or 0.0)
        # 1.0.10: 腿幅**上限**。暴漲日(>4%)的回撤行為與溫和上漲日不同,
        # 只有下限的話兩者會被混在同一個統計裡。0 = 無上限。
        self.max_move_pct = float(
            getattr(params, "betafib_max_move_pct", 0.0) or 0.0)
        # Entry-window hours are New York local wall time; ZoneInfo resolves
        # EST/EDT per candle. None keeps the full overnight window.
        self.entry_start_h = getattr(params, "betafib_entry_start_hour", None)
        self.entry_end_h = getattr(params, "betafib_entry_end_hour", None)
        # 1.0.9: fib 錨點。使用者圖表確認實際是後者。
        #   "oc" RTH open → close(舊行為)
        #   "hl" RTH 擺動低 → 擺動高(推動腿)—— 交易者實際畫線的方式,
        #        上漲日取「最高點之前」的最低點,下跌日鏡像
        self.fib_anchor = str(
            getattr(params, "betafib_anchor", None)
            or getattr(params, "research_fib_anchor", "oc") or "oc").lower()
        self._day = None
        # hl 錨點的串流狀態
        self._run_lo = self._run_hi = None
        self._lo_before_hi = self._hi_before_lo = None
        self._rth_open_px = self._rth_close_px = None
        self._rth_hi = self._rth_lo = None
        self._day_ranges: deque = deque(maxlen=14)
        self._level = None          # 掛單價
        self._level_dir = None      # Direction
        self._fired = False

    def _in_entry_window(self, ts: datetime) -> bool:
        """是否落在允許進場的 New York 小時區間（可跨午夜）。

        兩端任一為 None = 不限制,維持原本整個夜盤都能進場的行為。
        22→1 這種跨午夜的區間是 [22,24) ∪ [0,1),所以用 or 而不是 and。
        """
        a, b = self.entry_start_h, self.entry_end_h
        if a is None or b is None:
            return True
        h = as_new_york(ts).hour
        return (a <= h < b) if a < b else (h >= a or h < b)

    def _daily_atr(self) -> Optional[float]:
        if len(self._day_ranges) < 5:
            return None
        return sum(self._day_ranges) / len(self._day_ranges)

    def _session_day(self, ts: datetime):
        """以 09:30 America/New_York RTH 開盤為界的 session day。

        原本用 UTC `ts.date()` 分日,夜盤跨 UTC 午夜時會過早清掉掛單。
        中途會跨過 UTC 00:00。日曆日一換 `_level` 就被清成 None,等於掛單只在
        20:00–23:59(4 小時)有效,而不是文件寫的 17.5 小時。訊號量被砍掉七成,
        這正是先前 SESSFIB 在 MES 上湊不到 15 筆的原因。
        """
        return rth_session_date(ts)

    def _track(self, candle: Candle) -> None:
        local = as_new_york(candle.timestamp)
        hm = (local.hour, local.minute)
        ts = _utc(candle.timestamp)
        d = self._session_day(ts)
        if d != self._day:
            if self._rth_hi is not None and self._rth_lo is not None:
                self._day_ranges.append(self._rth_hi - self._rth_lo)
            self._day = d
            self._rth_open_px = self._rth_close_px = None
            self._rth_hi = self._rth_lo = None
            self._level = self._level_dir = None
            self._fired = False
            self._run_lo = self._run_hi = None
            self._lo_before_hi = self._hi_before_lo = None
        if self.RTH_OPEN <= hm < self.RTH_CLOSE:
            if self._rth_open_px is None:
                self._rth_open_px = candle.open
            # 串流版的擺動腿追蹤:創新高時,記下「到此為止的最低點」
            self._run_lo = candle.low if self._run_lo is None else min(self._run_lo, candle.low)
            self._run_hi = candle.high if self._run_hi is None else max(self._run_hi, candle.high)
            if self._rth_hi is None or candle.high >= self._rth_hi:
                self._lo_before_hi = self._run_lo
            if self._rth_lo is None or candle.low <= self._rth_lo:
                self._hi_before_lo = self._run_hi
            self._rth_hi = candle.high if self._rth_hi is None else max(self._rth_hi, candle.high)
            self._rth_lo = candle.low if self._rth_lo is None else min(self._rth_lo, candle.low)
            self._rth_close_px = candle.close
            return
        # RTH 之後第一次進來 → 定案掛單價
        if hm >= self.RTH_CLOSE and self._level is None and self._rth_close_px is not None:
            atr = self._daily_atr()
            if not atr or atr <= 0:
                return
            up = self._rth_close_px > self._rth_open_px
            if self.fib_anchor == "hl":
                # 推動腿:上漲日 = 最高點之前的最低點 → 最高點
                a0 = self._lo_before_hi if up else self._hi_before_lo
                a1 = self._rth_hi if up else self._rth_lo
                if a0 is None or a1 is None:
                    return
                anchor, move = a0, a1 - a0
            else:
                anchor = self._rth_open_px
                move = self._rth_close_px - self._rth_open_px
            if move == 0 or anchor <= 0:
                return
            if abs(move) / atr < self.min_move_atr:
                return
            # 1.0.9: 百分比門檻 —— 「今天漲了 0.4% 才算大漲」
            # 1.0.10: 加上限 → 變成區間「漲 1~4% 才算數」。暴漲日的回撤
            # 行為與溫和上漲日不同,不排除的話兩者會被混進同一個統計。
            move_pct = abs(move) / anchor * 100.0
            if self.min_move_pct > 0 and move_pct < self.min_move_pct:
                return
            if self.max_move_pct > 0 and move_pct > self.max_move_pct:
                return
            # retrace_frac 是「回吐比例」,進場 fib = 1 - retrace_frac
            self._level = anchor + (1.0 - self.retrace_frac) * move
            self._level_dir = Direction.BUY if move > 0 else Direction.SELL
            self._level_move = move
            self._level_atr = atr
            self._level_open = anchor                 # fib 基準的原點

    def evaluate(self, candle: Candle, zones=None, is_mature: bool = True):
        self._roll(candle)
        self._track(candle)
        if self._fired or self._level is None or self._level_dir is None:
            return None
        # 1.0.10: 進場時窗。_track() 照常在整個夜盤維護掛單價,只是在窗外
        # 不成交 —— 這樣切換時窗不會改變 fib 層級本身,兩個維度可以獨立測。
        if not self._in_entry_window(_utc(candle.timestamp)):
            return None
        # 限價單:價格必須真的走到掛單價才成交
        if self._level_dir == Direction.BUY:
            if candle.low > self._level:
                return None
        else:
            if candle.high < self._level:
                return None
        self._fired = True
        # 1.0.9: 觸價後走**市價**,不是在掛單價成交。
        # 靜止限價單會假設你剛好在該價位成交(理想化);市價是偵測到觸及後
        # 以當根收盤進場並吃滿滑價 —— 保守得多,也跟「全部改市價」的方針一致。
        # (先前研究已證實限價的漏單風險無法在回測裡誠實建模。)
        pct = abs(self._level_move) / self._level_open * 100.0
        if self.risk_basis == "fib":
            # SL/TP 掛在 fib 層級本身:price(f) = rth_open + f × move
            # 漲勢:進場 0.8 → SL 0.75/0.70(更深回撤)、TP 0.90(反彈回去)
            # 跌勢:move 為負,同一組 f 自動鏡像,幾何完全對稱
            sl_px = self._level_open + self.sl_fib * self._level_move
            tp_px = self._level_open + self.tp_fib * self._level_move
            return self._make_at(
                candle, self._level_dir, sl_px, tp_px,
                reason=f"RTH {self._level_move:+.1f} ({pct:.2f}%) "
                       f"→ 進 fib {1 - self.retrace_frac:.3f} @ {self._level:.2f} "
                       f"| SL {self.sl_fib:.2f} TP {self.tp_fib:.2f}")
        width = self._level_atr if self.risk_basis == "daily" else self._atr_blend()
        if width is None or width <= 0:
            return None
        return self._make(candle, self._level_dir,
                          f"RTH {self._level_move:+.1f} "
                          f"({self._level_move / self._level_atr:+.2f} ATR) "
                          f"→ fib {1 - self.retrace_frac:.3f} @ {self._level:.2f} "
                          f"(market, {self.risk_basis})",
                          width=width)


RESEARCH_STRATEGIES = {
    "ORB": ORBreakout,
    "VWAPREV": VwapReversion,
    "IBS": IbsReversion,
    "RSI2": Rsi2Reversion,
    "GAPFADE": GapFade,
    "MOMENTUM": MomentumContinuation,
    "DONCHIAN": DonchianBreakout,
    "BBREV": BollingerReversion,
    "ONCONT": OvernightContinuation,
    "BETAFIB": BetaFibRetrace,
}
