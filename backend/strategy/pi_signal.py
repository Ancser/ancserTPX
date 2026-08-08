"""1.0.10: π 外部訊號策略 —— Discord 推播驅動的進場。

與其他策略不同,進場時機不是從 K 棒算出來的,而是外部推播。但**出場結構、
風控、下單路徑完全共用** —— 繼承 `_ResearchBase` 取得同一套 `_atr_blend()`
與 `_make()`,所以 SL/TP 與回測(`scripts/pi_exit_study.py`)位元一致。

架構:listener 收到推播 → `push()` 進佇列 → 下一根 K 棒的 `evaluate()` 取出並成單。
走 K 棒節奏而非直接下單,是為了讓訊號經過引擎既有的全部閘門
(幾何驗證、日虧斷路器、時段限制、每日筆數上限)。延遲最多一根 K 棒(60 秒),
相對於 240 分鐘的持倉尺度可以忽略。

實測結論(2026-06-11 → 08-07,366 個標記,見 memory project_pi_signal_source):
  · 藍系做多 PF 1.99、每筆 $82
  · 紫系做空只貢獻 6.6% 獲利,MNQ 空單字面上是零 → 預設**只做多**
  · 紫系當出場訊號會讓做多獲利腰斬($11,780→$5,807)且勝率不變 → **不使用**
"""
from __future__ import annotations

import json
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from backend.db.models import Candle, Direction
from backend.strategy.research_lab import _ResearchBase, _utc

logger = logging.getLogger(__name__)

# 訊號超過這個時間就丟棄。引擎重啟或斷線後,舊訊號的進場價早已不成立 ——
# 照著下單等於用一小時前的理由吃現在的價格。必須 BLOCK 而不是 WARN。
DEFAULT_MAX_SIGNAL_AGE_MIN = 5

# 1.0.10: UI 用的訊號級別組合。每筆損益來自 366 個標記的回測
# (多 SL3.5×blend/TP3R、空 SL2.5×blend/60m,成本已扣):
#     青π   n=49  PF 3.05  $162/筆      深蓝圈 n=13 PF 2.90  $96/筆
#     粉π   n=56  PF 2.28  $62/筆
#     淡蓝圈 n=97  PF 1.35  $32/筆      紫圈   n=151 PF 1.18 $12/筆
# 「圈圈」只有大尺寸、π 只有中小 —— 尺寸與種類完全共線,所以只依種類分組。
PI_SIGNAL_SETS: dict[str, dict[str, tuple]] = {
    # 推薦:只做 π 級別 + 深藍圈,排除每筆 $12~$32 的兩種弱訊號
    "pi_only": {"long": ("青π", "深蓝圈"), "short": ("粉π",)},
    # 只做純 π,不含深藍圈(n=13 樣本太小)
    "pi_strict": {"long": ("青π",), "short": ("粉π",)},
    # 全部藍系做多 / 全部紫系做空(原始定義,含弱訊號)
    "all": {"long": ("青π", "深蓝圈", "淡蓝圈"), "short": ("粉π", "紫圈")},
    # 只做多側的 π 級別(青π PF 5.40 + 深蓝圈 PF 6.31,但 n 只有 34)
    "long_pi_only": {"long": ("青π", "深蓝圈"), "short": ()},
    # 1.0.10: 全部藍系做多、完全不做空。含淡蓝圈(PF 1.86,弱但為正),
    # 換到 n=84 的樣本量。與 long_pi_only 的取捨:
    #   long_pi_only  n=34  $7,279   每筆 $214   ← 品質高、頻率低
    #   long_all      n=84  $10,328  每筆 $123   ← 總額高、樣本紮實
    "long_all": {"long": ("青π", "深蓝圈", "淡蓝圈"), "short": ()},
}


# 1.0.10: 回測用歷史訊號。K 棒時間與訊號時間相差在此範圍內才視為「同一刻」。
# 太小會因為 1m K 棒對不上秒級時間戳而漏單,太大會在 live 誤觸舊訊號。
_HIST_TOL_MIN = 2
_HIST_PATH = Path(__file__).resolve().parents[2] / "data" / "research" / "pi_signals.json"
_HIST_CACHE: Optional[list] = None


def _load_history() -> list:
    """讀 scripts/pi_collect_history.py 收集的歷史訊號,轉成 (ts, PiSignal) 並排序。

    只讀一次(模組層快取)。檔案不存在就回空 —— 那表示還沒跑過收集腳本,
    回測會是 0 筆,但不該因此讓策略無法建立。
    """
    global _HIST_CACHE
    if _HIST_CACHE is not None:
        return _HIST_CACHE
    out: list = []
    try:
        rows = json.loads(_HIST_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("[PI] 找不到 %s —— 回測沒有歷史訊號可用"
                       "(先跑 scripts/pi_collect_history.py)", _HIST_PATH.name)
        _HIST_CACHE = []
        return _HIST_CACHE
    except Exception as e:
        logger.warning("[PI] 讀取歷史訊號失敗 %s: %s", type(e).__name__, e)
        _HIST_CACHE = []
        return _HIST_CACHE

    from backend.live.pi_listener import SYMBOL_MAP, DIRECTION, PiSignal, is_pre_session
    _skipped = 0
    for r in rows:
        sym = r.get("symbol")
        if not sym:
            continue
        try:
            ts = datetime.fromisoformat(str(r["ts"]).replace("Z", "+00:00"))
        except Exception:
            continue
        ts = ts.astimezone(timezone.utc)
        # 1.0.10: 濾掉每日 06:33 開盤前重播 —— 那是前一交易日標記的重播,
        # 不是即時訊號。舊檔沒有 pre_session 欄位,所以用時間再判一次。
        if r.get("pre_session") or is_pre_session(ts):
            _skipped += 1
            continue
        for mk in r.get("marks", []):
            d = DIRECTION.get(mk.get("kind"), 0)
            if not d:
                continue
            out.append((ts, PiSignal(
                message_id=str(r.get("id", "")), ts=ts, equity=sym,
                future=SYMBOL_MAP[sym], direction=d, kind=mk["kind"],
                size=mk.get("size", ""), pos=mk.get("pos"),
                raw=r.get("content", ""))))
    out.sort(key=lambda x: x[0])
    logger.info("[PI] 載入 %d 個歷史訊號(%s → %s)供回測使用", len(out),
                out[0][0].date() if out else "-", out[-1][0].date() if out else "-")
    if _skipped:
        logger.info("[PI] 歷史訊號 %d 筆,另濾除 %d 筆開盤前重播", len(out), _skipped)
    _HIST_CACHE = out
    return out


class PiSignalStrategy(_ResearchBase):
    """外部 π 訊號驅動。`push()` 由 listener 呼叫,`evaluate()` 由引擎呼叫。"""

    NAME = "PI"

    # 1.0.10: 級別過濾。實測(SL3.5/TP3R 做多、SL2.5/60m 做空,366 筆):
    #   多  青π   n=49 PF 3.05 每筆 $162   ← π 級別
    #       深蓝圈 n=13 PF 2.90 每筆 $96
    #       淡蓝圈 n=50 PF 1.86 每筆 $61    ← 圈級別,弱 4 倍
    #   空  粉π   n=32 PF 1.92 每筆 $39
    #       紫圈  n=90 PF 1.22 每筆 $12     ← 接近噪音
    # 依「尺寸」分組看起來小>中>大,但那是共線假象:大尺寸幾乎全是淡蓝圈,
    # 中小幾乎全是青π。真正的驅動是**標記種類**,所以只依種類過濾。
    #
    # 1.0.10:上面的數字是**濾除 06:33 開盤前重播後**重算的(舊數字被 44% 的
    # 重播標記污染,見 is_pre_session)。乾淨資料下**整個空方是淨虧的**:
    # 不對稱結構 空(紫系) n=122 PnL −$948 PF 0.91,空/MNQ 更差 PF 0.82。
    # 所以預設不做空。
    DEFAULT_LONG_KINDS = ("青π", "深蓝圈")
    DEFAULT_SHORT_KINDS = ()

    def __init__(self, params):
        super().__init__(params)
        self._queue: deque = deque(maxlen=32)
        # 1.0.10:濾除開盤前重播後空方轉為淨虧(PF 0.91),預設關閉做空。
        # pi_long_only=True 會在下面把 short kinds 清空,不論 signal_set 選什麼。
        self.pi_long_only = bool(getattr(params, "pi_long_only", True))
        # 允許的標記種類。優先序:明確給的 kinds > pi_signal_set > 預設
        _set = str(getattr(params, "pi_signal_set", "") or "").strip().lower()
        _preset = PI_SIGNAL_SETS.get(_set)
        _lk = getattr(params, "pi_long_kinds", None)
        _sk = getattr(params, "pi_short_kinds", None)
        self.pi_long_kinds = (tuple(_lk) if _lk is not None
                              else _preset["long"] if _preset else self.DEFAULT_LONG_KINDS)
        self.pi_short_kinds = (tuple(_sk) if _sk is not None
                               else _preset["short"] if _preset else self.DEFAULT_SHORT_KINDS)
        # long_only 是硬開關:壓過 signal_set 與明確指定的 short kinds。
        # 沒有這一條的話,選了 pi_only 之類含空方的 set 就會繞過它。
        if self.pi_long_only:
            self.pi_short_kinds = ()
        # 空單專屬出場(多單沿用 factor_sl_value / rr_ratio)——
        # 多單抱越久越好、空單抱越久越差,兩邊不能共用同一組。
        self.pi_short_sl = float(getattr(params, "pi_short_sl_value", 2.5) or 2.5)
        self.pi_short_hold = int(getattr(params, "pi_short_hold_min", 60) or 0)
        # 1.0.10: 回測用的歷史訊號。live 走 push() 佇列,回測走這條 ——
        # 否則回測時佇列永遠是空的,結果一定 0 筆。
        # 只在 K 棒時間**對得上訊號時間**(±_HIST_TOL_MIN)時才觸發,所以
        # live 模式下(K 棒是「現在」、歷史訊號都在過去)不會被誤觸。
        self._hist: list = _load_history()
        self._hist_i = 0
        self.pi_max_age_min = int(
            getattr(params, "pi_max_signal_age_min", DEFAULT_MAX_SIGNAL_AGE_MIN) or
            DEFAULT_MAX_SIGNAL_AGE_MIN)
        # 1.0.10: 只接受**本引擎正在交易的商品**的訊號。
        # 這個過濾器原本靠外部設定,但沒人設 → 回測 MNQ 時把 SPY(→MES)的訊號
        # 也收了進來,等於用 MNQ 的價格執行 SPY 的訊號。不會報錯,只會靜默地
        # 混入一半錯商品的結果。改成從 contract_id 自動推導。
        _pf = getattr(params, "pi_future", None)
        if not _pf:
            _cid = str(getattr(params, "contract_id", "") or "").upper()
            for _sym in ("MNQ", "MES"):
                if f".{_sym}." in _cid:
                    _pf = _sym
                    break
        self.pi_future: Optional[str] = _pf
        if not self.pi_future:
            logger.warning("[PI] 無法從 contract_id 判斷商品 —— 將接受所有訊號"
                           "(QQQ 與 SPY 混用同一份價格,結果不可信)")
        self._seen: set[str] = set()

    # ── listener 介面 ────────────────────────────────────
    def push(self, sig: Any) -> bool:
        """收到推播。回傳是否入列(重複/不符方向/商品不符 → False)。"""
        key = f"{getattr(sig, 'message_id', '')}:{getattr(sig, 'kind', '')}"
        if key in self._seen:
            return False
        if self.pi_future and getattr(sig, "future", None) != self.pi_future:
            return False
        d = getattr(sig, "direction", 0)
        kind = getattr(sig, "kind", "")
        if self.pi_long_only and d <= 0:
            logger.info("[PI] 略過空單訊號 %s(只做多)", kind)
            return False
        allow = self.pi_long_kinds if d > 0 else self.pi_short_kinds
        if allow and kind not in allow:
            logger.info("[PI] 略過 %s(不在允許級別 %s)", kind, "/".join(allow))
            return False
        self._seen.add(key)
        if len(self._seen) > 4000:
            self._seen = set(list(self._seen)[-1500:])
        self._queue.append(sig)
        logger.info("[PI] 入列 %s %s %s,佇列 %d",
                    getattr(sig, "future", "?"), getattr(sig, "side", "?"),
                    getattr(sig, "kind", "?"), len(self._queue))
        return True

    def pending(self) -> int:
        return len(self._queue)

    # ── 引擎介面 ────────────────────────────────────────
    def _drain_history(self, now: datetime) -> None:
        """把時間對得上這根 K 棒的歷史訊號放進佇列(回測用)。

        指標單向前進,所以整個回測是 O(訊號數) 而不是每根 K 棒掃一遍。
        只有 |K棒時間 − 訊號時間| <= _HIST_TOL_MIN 才算命中 —— live 模式下
        K 棒是「現在」、歷史訊號都在過去,差距遠大於容忍值,不會被誤觸。
        """
        tol = timedelta(minutes=_HIST_TOL_MIN)
        n = len(self._hist)
        while self._hist_i < n:
            ts, sig = self._hist[self._hist_i]
            if ts > now + tol:
                break                      # 還沒到,等後面的 K 棒
            self._hist_i += 1
            if now - ts <= tol:            # 命中這根 K 棒
                self.push(sig)
            # 否則:訊號比這根 K 棒早太多(回測起點在訊號之後)→ 略過

    def evaluate(self, candle: Candle, zones=None, is_mature: bool = True):
        self._roll(candle)
        now = _utc(candle.timestamp)
        if self._hist:
            self._drain_history(now)
        if not self._queue:
            return None

        while self._queue:
            sig = self._queue.popleft()
            ts = getattr(sig, "ts", None)
            if ts is not None:
                age = (now - _utc(ts)).total_seconds() / 60.0
                if age > self.pi_max_age_min:
                    # BLOCK,不是 WARN —— 過期訊號的進場理由已經不成立
                    logger.warning("[PI] 丟棄過期訊號 %s(%.1f 分鐘 > 上限 %d)",
                                   getattr(sig, "kind", "?"), age, self.pi_max_age_min)
                    continue
                if age < -2:
                    logger.warning("[PI] 訊號時間在未來 %.1f 分鐘,丟棄(時鐘不同步?)", -age)
                    continue

            d = Direction.BUY if getattr(sig, "direction", 0) > 0 else Direction.SELL
            if self.pi_long_only and d != Direction.BUY:
                continue

            width = self._atr_blend()
            if width is None or width <= 0:
                # 沒有波動基準就沒有 SL 寬度 → 拒絕下單,不猜
                logger.warning("[PI] atr_blend 尚未暖機完成,丟棄訊號 %s",
                               getattr(sig, "kind", "?"))
                continue
            # 空單用自己的 SL 倍數。_make() 內部是 risk = width × self.sl_atr,
            # 所以預先把 width 縮放成 width × (pi_short_sl / sl_atr),
            # 乘進去之後剛好等於 width × pi_short_sl。
            if d == Direction.SELL and self.sl_atr > 0:
                width = width * (self.pi_short_sl / self.sl_atr)

            reason = (f"PI {getattr(sig, 'equity', '?')} {getattr(sig, 'kind', '?')}"
                      f"/{getattr(sig, 'size', '?')}"
                      f"{'/' + sig.pos if getattr(sig, 'pos', None) else ''}")
            out = self._make(candle, d, reason, width=width)
            if out is not None:
                out.meta.setdefault("pi", {}).update({
                    "message_id": getattr(sig, "message_id", None),
                    "equity": getattr(sig, "equity", None),
                    "kind": getattr(sig, "kind", None),
                    "size": getattr(sig, "size", None),
                    "pos": getattr(sig, "pos", None),
                    "signal_ts": str(ts) if ts else None,
                })
                return out
        return None
