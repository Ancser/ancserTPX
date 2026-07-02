"""1.0.8 研究(僅腳本):組合驗證輪 — 修正 F7 + 把單項贏家疊起來。

單項結果(全區間,base = CLAUDE #1 = +7181 / DD 791 / PF 1.47):
  rollLadder(無TP 2R觸發1R階梯) = +8044 / DD 915 / PF 1.66   ← 唯一贏 PnL
  W5 prevRVcap(前日RV最高三分位不交易) = +4331 / DD 457 / PF 1.67 ← 風險冠軍
  W2 openDisp40t = +5486 / DD 591    W3 adrBudget80 = +7043 / PF 1.50
  F5 stop2loss   = +1746 / DD 442(單獨用太保守)
  F7 attempt2 上一輪因 Direction enum 大小寫 bug 得 0 筆 → 本輪修正重測。

組合:
  C1 F7fix attempt2      — 同日同方向先虧 1 單才准進第 2 次(修正版)。
  C2 W5+W2               — 兩個 DD 削減器疊加。
  C3 W5+ladder           — 風險冠軍 gate + PnL 冠軍 exit。★主候選
  C4 ladder+stop2        — 滾動出場 + 日虧2單斷路器。
  C5 W5+W3+ladder        — 全疊。

Run:  PYTHONIOENCODING=utf-8 python -m scripts.combo_study
"""
from __future__ import annotations

import copy
import logging
import math

from backend.data import candle_store
from backend.db.models import (
    BacktestConfig, Direction, _extract_symbol, get_commission_rt, get_fees_rt,
)
from backend.backtest.engine import _topstep_trade_date
from backend.terminal_live import (
    BUILTIN_PRESETS, CLAUDE_701_PRESET_1, _build_strategy_params,
)
from scripts.wild_ideas_study import WildBacktest

INITIAL_CAPITAL = 50_000.0
FAR_TP_PTS = 1_000_000.0


class ComboBacktest(WildBacktest):
    """WildBacktest 的 gates + 可選 no-TP 階梯滾動出場 + 修正版 attempt2。"""

    TRIGGER_R = 2.0
    GAP_R = 2.0

    def __init__(self, *args, ladder=False, attempt2=False, **kw):
        super().__init__(*args, **kw)
        self.ladder = ladder
        self.attempt2 = attempt2
        self._initial_risk = 0.0
        self._max_r = 0.0
        self._dir_losses = {}   # (trade_date, Direction) -> count

    def _execute_entry(self, signal, candle):
        super()._execute_entry(signal, candle)
        pos = self._open_position
        if not pos or not self.ladder:
            return
        self._initial_risk = abs(pos.entry_price - pos.sl_price)
        self._max_r = 0.0
        if pos.direction == Direction.BUY:
            pos.tp_price = pos.entry_price + FAR_TP_PTS
        else:
            pos.tp_price = pos.entry_price - FAR_TP_PTS

    def _execute_exit(self, candle, exit_price, reason):
        super()._execute_exit(candle, exit_price, reason)
        t = self._last_closed_trade
        if t is not None and (t.pnl or 0.0) < 0:
            k = (_topstep_trade_date(candle.timestamp), t.direction)
            self._dir_losses[k] = self._dir_losses.get(k, 0) + 1

    def _gates_pass(self, candle, sig) -> bool:
        if not super()._gates_pass(candle, sig):
            return False
        if self.attempt2:
            k = (_topstep_trade_date(candle.timestamp), sig.direction)
            if self._dir_losses.get(k, 0) < 1:
                return False
        return True

    def _check_trailing_sl(self, candle):
        if not self.ladder:
            return super()._check_trailing_sl(candle)
        pos = self._open_position
        if not pos or self._initial_risk <= 0:
            return
        mkt = candle.close
        fav = (mkt - pos.entry_price) if pos.direction == Direction.BUY else (pos.entry_price - mkt)
        r = fav / self._initial_risk
        if r > self._max_r:
            self._max_r = r
        if self._max_r < self.TRIGGER_R:
            return
        lock_r = math.floor(self._max_r) - self.GAP_R
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


def _run(params, candles, **kw):
    cid = params.contract_id
    config = BacktestConfig(
        strategies=["trend"], initial_capital=INITIAL_CAPITAL,
        symbol=_extract_symbol(cid), commission_rt=get_commission_rt(cid),
        fees_rt=get_fees_rt(cid),
        value_area_pct=float(getattr(params, "value_area_pct", 0.80)),
    )
    result = ComboBacktest(config=config, strategy_params=params,
                           zone_timeline=None, record_equity=False, **kw).run(candles)
    m = result.metrics
    return {
        "trades": int(m.total_trades), "win_rate": float(m.win_rate),
        "pnl": float(m.total_pnl), "max_dd": float(m.max_drawdown),
        "pf": float(m.profit_factor), "calmar": float(m.calmar_ratio),
        "expectancy": float(m.expectancy),
    }


def _row(tag, r):
    print(f"{tag:<22} {r['trades']:>6} {100*r['win_rate']:>6.1f}% "
          f"{r['pnl']:>+11.1f} {r['max_dd']:>9.1f} {r['pf']:>6.2f} "
          f"{r['calmar']:>7.2f} {r['expectancy']:>+9.2f}", flush=True)


def main():
    logging.getLogger("backend").setLevel(logging.WARNING)
    candles = candle_store.load("MNQ", 1)
    if not candles:
        raise SystemExit("No MNQ 1m candles.")
    candles.sort(key=lambda c: c.timestamp)
    print(f"candles {len(candles)}  {candles[0].timestamp} -> {candles[-1].timestamp}", flush=True)
    print("(base 對照:+7181.3 / DD 791.4 / PF 1.47 / Calmar 9.07,上輪已測)", flush=True)

    preset = BUILTIN_PRESETS[CLAUDE_701_PRESET_1]
    cid = preset.get("contract_id", "CON.F.US.MNQ.U26")
    base = _build_strategy_params(preset, cid)
    base.value_area_pct = float(preset.get("value_area_pct", 0.70))

    header = (f"{'variant':<22} {'trades':>6} {'win%':>7} {'pnl':>11} "
              f"{'maxDD':>9} {'PF':>6} {'Calmar':>7} {'expect':>9}")
    print("\n" + header, flush=True)
    print("-" * len(header), flush=True)

    runs = [
        ("C1 attempt2(fix)", {"attempt2": True}),
        ("C2 prevRV+openDisp", {"prev_rv_cap": True, "open_disp_ticks": 40}),
        ("C3 prevRV+ladder", {"prev_rv_cap": True, "ladder": True}),
        ("C4 ladder+stop2", {"ladder": True, "daily_stop": 2}),
        ("C5 prevRV+adr+ladder", {"prev_rv_cap": True, "adr_budget_pct": 0.8,
                                  "ladder": True}),
    ]
    for tag, kw in runs:
        r = _run(copy.deepcopy(base), candles, **kw)
        _row(tag, r)


if __name__ == "__main__":
    main()
