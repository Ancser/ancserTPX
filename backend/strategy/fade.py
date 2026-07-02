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
        # 2026-07-02 isolation study:
        # long->POC keeps the best PF/DD; breakout legs are negative.
        # both->mid has higher gross PnL but lower win/PF quality.
        self.SL_TICKS = int(getattr(p, "tr_sl_ticks", None) or getattr(p, "sl_ticks", 80) or 80)
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
        # 只在前日 VA 區間內掛(價格已離開區間 = 趨勢日,不接)
        if not (val < candle.close < vah):
            return None
        if (poc - val) <= self.MIN_TP_TICKS * self.TICK_SIZE:
            return None

        entry = val
        sl = entry - self.SL_TICKS * self.TICK_SIZE
        min_sl = entry - self.MIN_STOP_TICKS * self.TICK_SIZE
        if sl > min_sl:
            sl = min_sl
        tp = poc

        self._used.add(key)
        self._last_key = key
        self._state = "confirmed"

        logger.info(
            f"[FADE] BUY LIMIT @ prevVAL {entry:.2f} | SL={sl:.2f} TP=prevPOC {tp:.2f} "
            f"| levels({lv['date']})"
        )
        return TradeSignal(
            strategy=StrategyType.TREND_FOLLOW,
            direction=Direction.BUY,
            entry_price=entry, sl_price=sl, tp_price=tp,
            zone_id=f"FD:{lv['date']}",
            reason=(
                f"FADE LONG | prevVAL {entry:.2f} -> prevPOC {tp:.2f} | "
                f"SL {sl:.2f} ({self.SL_TICKS}t)"
            ),
            timestamp=candle.timestamp,
            breakout_range=abs(vah - val),
            meta={
                "strategy_family": "fade",
                "mode": "prev_day_va",
                "side": "VAL",
                "trade_tf": "1d",
                "labels": ["fade:prevVAL->prevPOC"],
                "primary_zone": {
                    "tf": "1d", "zone_id": f"FD:{lv['date']}",
                    "poc": poc, "vah_80": vah, "val_80": val,
                },
            },
        )
