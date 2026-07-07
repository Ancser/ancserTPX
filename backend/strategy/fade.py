# ============================================================
# 文件: backend/strategy/fade.py
# 狀態: 1.0.8 新增 (DAY ZONE — 前日價值區回歸策略)
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
"""策略三:DAY ZONE — 前日價值區回歸(買前日 VAL 回歸前日 POC)。"""

from __future__ import annotations

import logging
from datetime import timezone
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
            return "DAY ZONE 掛單中 VAL↑POC"
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
            reason = (f"DAY ZONE REJECTION(market) | 掃VAL {val:.2f} 收回 @ {entry:.2f} "
                      f"-> TP {tp:.2f} | SL {sl:.2f}")
        else:
            # 直接掛單:價格在前日 VA 內時掛 BUY LIMIT @ VAL(趨勢日離開區間不接)。
            if not (val < candle.close < vah):
                return None
            entry = val
            order_type = "limit"
            reason = (f"DAY ZONE LIMIT | prevVAL {entry:.2f} -> TP {tp:.2f}({self.TP_FRAC:g}) "
                      f"| SL {sl:.2f} ({self.SL_TICKS}t)")

        self._used.add(key)
        self._last_key = key
        self._state = "confirmed"

        logger.info(f"[DAYZONE-{self.ENTRY_MODE}] BUY @ {entry:.2f} | SL={sl:.2f} TP={tp:.2f} "
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


class OpeningRangeFade:
    """策略:15m 開盤區間假突破 fade(雙向)。

    1.0.9 從 `scripts/fade_professional_idea_sweep.py` 的 `or15_false_break` 最佳
    生產可用變體移植(SL=0.2×前日VA幅、TP=1×前日VA幅、每方向每日一次、market@close)。
    以 `fade_entry_mode == "or15"` 被引擎選用,共用 fade 的前日 VP 水位餵入管線。
    介面與 PrevDayFade / SessionTrendFollow 相容。

    規則:
      - OR 視窗 = RTH 開盤 13:30–13:45 UTC(09:30–09:45 ET),逐根更新 OR 高/低。
      - t ≥ 13:45(OR 完成)後,當根 K:
          高假突破(high > OR_high 且 close < OR_high)→ 做空(fade 假突破)
          低假突破(low  < OR_low  且 close > OR_low )→ 做多(fade 假突破)
      - rng = 前日 VAH − VAL(由 set_levels 餵入);SL 距 = max(4t, 0.2×rng);
        TP 距 = max(4t, 1.0×rng);entry = 當根 close(市價)。
      - 每交易日每方向最多一次(_used 上鎖);OR 於 set_levels(換日)重置。

    注意:需要 MARKET SESSION 允許 RTH(建議 ALL),否則盤段過濾會在 13:30–13:45
    擋掉 evaluate → OR 無法累積、訊號不會觸發。回測驗證見
    docs/1.0.9_FADE_PROFESSIONAL_IDEA_SWEEP_REPORT.md。此模式可在 practice account
    做 live order 測試;是否用於 express 仍應由使用者手動選帳號與 preset。
    """

    TICK_SIZE = 0.25
    MIN_STOP_TICKS = 4
    MIN_TARGET_TICKS = 4
    PENDING_TIMEOUT_CANDLES = 1

    OR_START_MIN = 13 * 60 + 30    # 13:30 UTC(含)
    OR_END_MIN = 13 * 60 + 45      # 13:45 UTC(不含)— OR 完成
    SL_FRAC = 0.20                 # SL = 0.2 × 前日VA幅(研究最佳)
    TP_FRAC_RNG = 1.0              # TP = 1.0 × 前日VA幅(full_1r)

    def __init__(self, params=None):
        self._levels: Optional[Dict[str, Any]] = None
        self._or_high: Optional[float] = None
        self._or_low: Optional[float] = None
        self._or_day: Optional[str] = None
        self._used: set[str] = set()
        self._last_key: Optional[str] = None
        self._state = "idle"       # idle | confirmed | in_trade

    # ── 引擎餵入前日水位(換日 rollover 觸發 → 一併重置 OR) ──
    def set_levels(self, levels: Optional[Dict[str, Any]]) -> None:
        self._levels = levels
        d = (levels or {}).get("date")
        if d != self._or_day:
            self._or_day = d
            self._or_high = None
            self._or_low = None

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
        # 無倉時也追蹤 OR(冗餘保險;evaluate 亦會追蹤)
        self._track_or(candle)

    def notify_trade_closed(self, exit_reason: str):
        self._state = "idle"
        self._last_key = None       # 已成交平倉 → 今天這個方向用掉

    def notify_order_cancelled(self):
        self._state = "idle"
        if self._last_key:          # 市價單未成 → 解鎖該方向,下一根可重試
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
            return "OR15 假突破進場中"
        if self._state == "in_trade":
            return "持倉中"
        if self._or_high is not None and self._or_low is not None:
            return f"OR15 完成 {self._or_low:.2f}~{self._or_high:.2f} 等假突破"
        return "等待 RTH 開盤 15m 區間"

    @property
    def raw_state(self) -> str:
        return self._state

    # ── OR 追蹤 ──
    @staticmethod
    def _utc_minutes(ts) -> int:
        from datetime import timezone
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts = ts.astimezone(timezone.utc)
        return ts.hour * 60 + ts.minute

    def _track_or(self, candle: Candle) -> None:
        mins = self._utc_minutes(candle.timestamp)
        if self.OR_START_MIN <= mins < self.OR_END_MIN:
            self._or_high = candle.high if self._or_high is None else max(self._or_high, candle.high)
            self._or_low = candle.low if self._or_low is None else min(self._or_low, candle.low)

    # ── 核心 ──
    def evaluate(self, candle: Candle, zones=None, is_mature: bool = True) -> Optional[TradeSignal]:
        # 每根先累積 OR(無論狀態);OR 視窗內不進場。
        self._track_or(candle)
        lv = self._levels
        if not lv or self._state == "in_trade":
            return None
        if self._utc_minutes(candle.timestamp) < self.OR_END_MIN:
            return None
        if self._or_high is None or self._or_low is None:
            return None

        vah, val = float(lv["vah"]), float(lv["val"])
        rng = max(self.TICK_SIZE, vah - val)
        orh, orl = float(self._or_high), float(self._or_low)

        if candle.high > orh and candle.close < orh:
            direction, dsign, side = Direction.SELL, -1, "ORH"      # 高假突破 → 空
        elif candle.low < orl and candle.close > orl:
            direction, dsign, side = Direction.BUY, +1, "ORL"       # 低假突破 → 多
        else:
            return None

        key = f"{lv['date']}:or{'Short' if dsign < 0 else 'Long'}"
        if key in self._used:
            return None

        entry = float(candle.close)
        sl_dist = max(self.MIN_STOP_TICKS * self.TICK_SIZE, self.SL_FRAC * rng)
        tp_dist = max(self.MIN_TARGET_TICKS * self.TICK_SIZE, self.TP_FRAC_RNG * rng)
        sl = entry - dsign * sl_dist
        tp = entry + dsign * tp_dist

        self._used.add(key)
        self._last_key = key
        self._state = "confirmed"

        reason = (f"OR15 假突破 {'高→空' if dsign < 0 else '低→多'} | OR {orl:.2f}~{orh:.2f} "
                  f"entry {entry:.2f} -> TP {tp:.2f} SL {sl:.2f} | rng(前日VA)={rng:.2f}")
        logger.info(f"[OR15DAYZONE] {'SELL' if dsign < 0 else 'BUY'} @ {entry:.2f} "
                    f"| SL={sl:.2f} TP={tp:.2f} | levels({lv['date']})")
        ts_utc = candle.timestamp.replace(tzinfo=timezone.utc) if candle.timestamp.tzinfo is None else candle.timestamp.astimezone(timezone.utc)
        or_start = ts_utc.replace(hour=13, minute=30, second=0, microsecond=0)
        or_end = ts_utc.replace(hour=13, minute=45, second=0, microsecond=0)
        return TradeSignal(
            strategy=StrategyType.TREND_FOLLOW,
            direction=direction,
            entry_price=entry, sl_price=sl, tp_price=tp,
            zone_id=f"OR15:{lv['date']}",
            reason=reason,
            timestamp=candle.timestamp,
            breakout_range=rng,
            order_type="market",
            meta={
                "strategy_family": "fade",
                "mode": "or15_false_break",
                "side": side,
                "signal_reason": reason,
                "trade_tf": "15m",
                "labels": [f"or15:{'short' if dsign < 0 else 'long'}:SL{self.SL_FRAC:g}rng:TP{self.TP_FRAC_RNG:g}rng"],
                "or_range": {
                    "tf": "or15",
                    "zone_id": f"OR15:{ts_utc.date().isoformat()}",
                    "formed_at": or_start.isoformat(),
                    "left_at": or_end.isoformat(),
                    "or_high": orh,
                    "or_low": orl,
                    "vah_80": orh,
                    "val_80": orl,
                    "high_100": orh,
                    "low_100": orl,
                    "break_side": side,
                },
                "primary_zone": {
                    "tf": "1d", "zone_id": f"OR15:{lv['date']}",
                    "poc": float(lv["poc"]), "vah_80": vah, "val_80": val,
                },
            },
        )
