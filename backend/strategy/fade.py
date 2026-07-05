# ============================================================
# 文件: backend/strategy/fade.py
# 狀態: 1.0.8 新增 (FADE — 前日價值區回歸策略)
# 規則 (PrevDayFade):
#   1. 每個 Topstep 交易日開盤,引擎以「前一整個交易日」1m K 線算 VP
#      → 前日 POC / VAH80 / VAL80,經 set_levels() 餵入本策略
#   2. 價格在前日 VA 區間內時,掛 BUY LIMIT @ 前日 VAL,TP = 前日 POC
#      (只做多 — 回測顯示 fadeShort 腿顯著弱於 fadeLong,已砍)
#   3. SL = VAL − sl_ticks(預設 80 tick;回測證實 fade 的 SL 不可收窄)
#   4. 每張單每天最多成交一次;limit 未成交每根重掛;距 POC < 8 tick 不做
#   5. trail 沿用引擎共用 Trail50 機制
# 回測驗證 (2.5 個月 MNQ): 31 筆, 勝率29%, +1255, maxDD 247, PF 2.38
# 關聯文件:
#   → backend/live/engine.py     (PrevDayFade live path + 前日 VP 計算)
#   → backend/backtest/engine.py (PrevDayFade backtest path + 前日 VP 計算)
# ============================================================
"""策略三:FADE — 前日價值區回歸(買前日 VAL 回歸前日 POC)。"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from backend.db.models import (
    Candle, Direction, StrategyType, TradeSignal,
)

logger = logging.getLogger(__name__)


class PrevDayFade:
    """前日 VA fadeLong。與 SessionTrendFollow 介面相容(evaluate/observe/notify)。

    levels 由引擎在交易日 rollover 時經 set_levels() 餵入:
      {"date": "YYYY-MM-DD", "poc": float, "vah": float, "val": float}
    """

    TICK_SIZE = 0.25
    MIN_STOP_TICKS = 4
    MIN_TP_TICKS = 8            # 距 POC 太近沒肉,不做
    PENDING_TIMEOUT_CANDLES = 1

    def __init__(self, params=None):
        p = params
        # 2026-07-02 isolation study: long->POC 保 PF/DD;breakout 腿為負。
        # 2026-07-03 flex 研究:SL120 + TP=VAL+0.75*(POC-VAL) 是唯一過 walk-forward
        # 三段的組合(SL 越寬越穩、TP 越近越穩);tp_frac>1 過 POC 皆第三段翻負。
        self.SL_TICKS = int(getattr(p, "tr_sl_ticks", None) or getattr(p, "sl_ticks", 120) or 120)
        # TP 佔 VAL→POC 距離的比例(0.75 = 提前落袋,經驗證最穩)
        self.TP_FRAC = float(getattr(p, "fade_tp_frac", 0.75) or 0.75)
        # 進場模式:"limit"(直接掛 VAL)| "rejection"(跌破 VAL 又收回 → 市價追)
        self.ENTRY_MODE = str(getattr(p, "fade_entry_mode", "limit") or "limit").lower()
        if self.ENTRY_MODE not in ("limit", "rejection"):
            self.ENTRY_MODE = "limit"
        self._levels: Optional[Dict[str, Any]] = None
        # 每天一次:發信號上鎖;掛單過期未成交解鎖;成交後鎖到日終。
        self._used: set[str] = set()
        self._last_key: Optional[str] = None
        self._state = "idle"    # idle | confirmed | in_trade

    # ── 引擎餵入前日水位 ──
    def set_levels(self, levels: Optional[Dict[str, Any]]) -> None:
        self._levels = levels

    def get_levels(self) -> Optional[Dict[str, Any]]:
        return self._levels

    # ── SessionTrendFollow 相容介面 ──
    def reset(self):
        self._state = "idle"
        self._last_key = None

    def reset_state_only(self):
        self.reset()

    def reset_breakout_confirmation(self):
        self.reset()

    def warmup(self, candle: Candle):
        pass

    def observe(self, candle: Candle, zones, is_mature) -> None:
        pass

    def notify_trade_closed(self, exit_reason: str):
        self._state = "idle"
        self._last_key = None       # 已成交並平倉 → 今天這張單用掉

    def notify_order_cancelled(self):
        self._state = "idle"
        if self._last_key:          # 掛單過期沒成交 → 解鎖,下一根重掛
            self._used.discard(self._last_key)
            self._last_key = None

    def set_traded_breakouts(self, keys):
        pass

    def mark_breakout_used(self, zone_id, direction):
        pass

    def unlock_breakout(self, zone_id, direction):
        pass

    def get_phase_label(self) -> str:
        if self._state == "confirmed":
            return "FADE 掛單中 VAL↑POC"
        if self._state == "in_trade":
            return "持倉中"
        return "等待回踩前日 VAL"

    @property
    def raw_state(self) -> str:
        return self._state

    # ── 核心 ──
    def evaluate(self, candle: Candle, zones=None, is_mature: bool = True) -> Optional[TradeSignal]:
        lv = self._levels
        if not lv or self._state == "in_trade":
            return None
        poc, vah, val = float(lv["poc"]), float(lv["vah"]), float(lv["val"])
        key = f"{lv['date']}:fadeLong"
        if key in self._used:
            return None
        if (poc - val) <= self.MIN_TP_TICKS * self.TICK_SIZE:
            return None

        sl = val - self.SL_TICKS * self.TICK_SIZE     # 結構性 SL,置於 VAL 下方固定緩衝
        tp = val + self.TP_FRAC * (poc - val)          # TP = VAL→POC 的 TP_FRAC 比例處

        if self.ENTRY_MODE == "rejection":
            # 拒絕進場:本根 K 跌破 VAL(掃)又收回 VAL 上方(拒絕)→ 市價追多。
            # 只在仍離 TP 有空間時做(避免收得太高)。
            if not (candle.low <= val < candle.close < poc):
                return None
            entry = candle.close
            order_type = "market"
            if tp - entry <= self.MIN_TP_TICKS * self.TICK_SIZE:
                return None
            reason = (f"FADE REJECTION(market) | 掃VAL {val:.2f} 收回 @ {entry:.2f} "
                      f"-> TP {tp:.2f} | SL {sl:.2f}")
        else:
            # 直接掛單:價格在前日 VA 內時掛 BUY LIMIT @ VAL(趨勢日離開區間不接)。
            if not (val < candle.close < vah):
                return None
            entry = val
            order_type = "limit"
            reason = (f"FADE LIMIT | prevVAL {entry:.2f} -> TP {tp:.2f}({self.TP_FRAC:g}) "
                      f"| SL {sl:.2f} ({self.SL_TICKS}t)")

        self._used.add(key)
        self._last_key = key
        self._state = "confirmed"

        logger.info(f"[FADE-{self.ENTRY_MODE}] BUY @ {entry:.2f} | SL={sl:.2f} TP={tp:.2f} "
                    f"| levels({lv['date']})")
        return TradeSignal(
            strategy=StrategyType.TREND_FOLLOW,
            direction=Direction.BUY,
            entry_price=entry, sl_price=sl, tp_price=tp,
            zone_id=f"FD:{lv['date']}",
            reason=reason,
            timestamp=candle.timestamp,
            breakout_range=abs(vah - val),
            order_type=order_type,
            meta={
                "strategy_family": "fade",
                "mode": "prev_day_va_" + self.ENTRY_MODE,
                "side": "VAL",
                "trade_tf": "1d",
                "labels": [f"fade:{self.ENTRY_MODE}:VAL->{self.TP_FRAC:g}POC"],
                "primary_zone": {
                    "tf": "1d", "zone_id": f"FD:{lv['date']}",
                    "poc": poc, "vah_80": vah, "val_80": val,
                },
            },
        )
