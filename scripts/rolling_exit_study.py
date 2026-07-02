"""1.0.8 研究(僅腳本):無固定 TP + R 階梯滾動 SL(贏家一直滾)。

使用者提案:不設 TP。當浮盈到 +2R 時 SL 移到 entry(保本),之後每再走 +1R,
SL 跟進 +1R(始終落後最高浮盈整數 R 約 2R)→ 趨勢日贏家滾到被階梯掃出或
19:45 flatten;假突破照舊 -1R 止損。

變體:
  A base       — CLAUDE #1 原樣(固定 TP RR4 + Trail50L10)對照。
  B rollLadder — 無 TP;+2R 觸發 SL→entry;每 +1R 階梯 +1R(gap 2R)。
  C rollRatio  — 無 TP;+2R 觸發後 SL = 最高浮盈的 50%(連續棘輪)。
  D sessLadder — B 的規則,zone 換成 0.15.5 式整個-session 生長 zone。

R = 進場時的初始 SL 距離(最低量節點 SL,與現行相同)。
浮盈以收盤價計(與現行 trail 同一慣例,保守)。

Run:  PYTHONIOENCODING=utf-8 python -m scripts.rolling_exit_study
"""
from __future__ import annotations

import copy
import logging
import math

from backend.backtest.engine import BacktestEngine
from backend.data import candle_store
from backend.db.models import (
    BacktestConfig, Direction, _extract_symbol, get_commission_rt, get_fees_rt,
)
from backend.terminal_live import (
    BUILTIN_PRESETS, CLAUDE_701_PRESET_1, _build_strategy_params,
)
from scripts.big_structure_study import SessionZoneAdapter

INITIAL_CAPITAL = 50_000.0
FAR_TP_PTS = 1_000_000.0   # 名義上仍有 TP 欄位,推到永遠打不到


class RollingExitBacktest(BacktestEngine):
    """No-TP + R-ladder ratchet SL. mode='ladder' or 'ratio'."""

    TRIGGER_R = 2.0    # 浮盈達 2R 才開始滾
    GAP_R = 2.0        # ladder:SL 落後最高整數 R 的距離
    RATIO = 0.50       # ratio:SL 鎖最高浮盈的比例

    def __init__(self, *args, roll_mode: str = "ladder", **kw):
        super().__init__(*args, **kw)
        self.roll_mode = roll_mode
        self._initial_risk = 0.0
        self._max_r = 0.0

    def _execute_entry(self, signal, candle):
        super()._execute_entry(signal, candle)
        pos = self._open_position
        if not pos:
            return
        self._initial_risk = abs(pos.entry_price - pos.sl_price)
        self._max_r = 0.0
        # 拔掉固定 TP:推到不可能成交的距離,出場只剩滾動 SL / flatten
        if pos.direction == Direction.BUY:
            pos.tp_price = pos.entry_price + FAR_TP_PTS
        else:
            pos.tp_price = pos.entry_price - FAR_TP_PTS

    def _check_trailing_sl(self, candle):
        pos = self._open_position
        if not pos or self._initial_risk <= 0:
            return
        mkt = candle.close
        if pos.direction == Direction.BUY:
            fav = mkt - pos.entry_price
        else:
            fav = pos.entry_price - mkt
        r = fav / self._initial_risk
        if r > self._max_r:
            self._max_r = r
        if self._max_r < self.TRIGGER_R:
            return

        if self.roll_mode == "ladder":
            lock_r = math.floor(self._max_r) - self.GAP_R   # 2R→0(entry), 3R→+1R…
        else:  # ratio
            lock_r = self._max_r * self.RATIO

        tick = self.TICK_SIZE
        if pos.direction == Direction.BUY:
            new_sl = round((pos.entry_price + lock_r * self._initial_risk) / tick) * tick
            if new_sl > pos.sl_price:
                pos.sl_price = new_sl
                self._trail_sl_triggered = True
        else:
            new_sl = round((pos.entry_price - lock_r * self._initial_risk) / tick) * tick
            if new_sl < pos.sl_price:
                pos.sl_price = new_sl
                self._trail_sl_triggered = True


class RollingSessionBacktest(RollingExitBacktest):
    """Rolling ladder + 0.15.5 式整個-session zone。"""

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.detector = SessionZoneAdapter(
            value_area_pct=float(self.config.value_area_pct),
            tick_size=self.TICK_SIZE,
        )


def _run(engine_cls, params, candles, **kw):
    cid = params.contract_id
    config = BacktestConfig(
        strategies=["trend"], initial_capital=INITIAL_CAPITAL,
        symbol=_extract_symbol(cid), commission_rt=get_commission_rt(cid),
        fees_rt=get_fees_rt(cid),
        value_area_pct=float(getattr(params, "value_area_pct", 0.80)),
    )
    result = engine_cls(config=config, strategy_params=params,
                        zone_timeline=None, record_equity=False, **kw).run(candles)
    m = result.metrics
    reasons = {}
    for t in result.trades:
        k = str(getattr(t, "exit_reason", None) or "?")
        k = k.split(".")[-1].lower()
        reasons[k] = reasons.get(k, 0) + 1
    return {
        "trades": int(m.total_trades), "win_rate": float(m.win_rate),
        "pnl": float(m.total_pnl), "max_dd": float(m.max_drawdown),
        "pf": float(m.profit_factor), "calmar": float(m.calmar_ratio),
        "expectancy": float(m.expectancy), "reasons": reasons,
    }


def _row(tag, r):
    print(f"{tag:<26} {r['trades']:>6} {100*r['win_rate']:>6.1f}% "
          f"{r['pnl']:>+11.1f} {r['max_dd']:>9.1f} {r['pf']:>6.2f} "
          f"{r['calmar']:>7.2f} {r['expectancy']:>+9.2f}  {r['reasons']}", flush=True)


def main():
    logging.getLogger("backend").setLevel(logging.WARNING)
    candles = candle_store.load("MNQ", 1)
    if not candles:
        raise SystemExit("No MNQ 1m candles.")
    candles.sort(key=lambda c: c.timestamp)
    print(f"candles {len(candles)}  {candles[0].timestamp} -> {candles[-1].timestamp}", flush=True)

    preset = BUILTIN_PRESETS[CLAUDE_701_PRESET_1]   # 單5m VA70 RR4 C3
    cid = preset.get("contract_id", "CON.F.US.MNQ.U26")
    base = _build_strategy_params(preset, cid)
    base.value_area_pct = float(preset.get("value_area_pct", 0.70))

    header = (f"{'variant':<26} {'trades':>6} {'win%':>7} {'pnl':>11} "
              f"{'maxDD':>9} {'PF':>6} {'Calmar':>7} {'expect':>9}  exits")
    print("\n" + header, flush=True)
    print("-" * len(header), flush=True)

    _row("A base 固定TP RR4", _run(BacktestEngine, copy.deepcopy(base), candles))
    _row("B rollLadder 2R/1R", _run(RollingExitBacktest, copy.deepcopy(base), candles,
                                    roll_mode="ladder"))
    _row("C rollRatio 50%", _run(RollingExitBacktest, copy.deepcopy(base), candles,
                                 roll_mode="ratio"))
    _row("D sessLadder 0.15.5", _run(RollingSessionBacktest, copy.deepcopy(base), candles,
                                     roll_mode="ladder"))


if __name__ == "__main__":
    main()
