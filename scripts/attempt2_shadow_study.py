"""1.0.8 研究(僅腳本):attempt2 正確版 — 影子第一單。

前兩輪 attempt2 = 0 筆的原因是死鎖:gate 要求先有虧單,但第一單也被 gate 擋。
正確實現:同日同方向的**第一次訊號不真進場**,記成影子單(entry/SL/TP 照策略
原樣),用後續 K 線模擬:
  影子被 SL 打死 → 該日該方向解鎖,下一次訊號真進場(「第二次才是真突破」)。
  影子跑到 TP   → 移除影子(這次突破是真的但我們沒上,繼續等新的第一次)。
同一根 K 線 SL/TP 都碰到 → 保守算 SL(較快解鎖)。

  A2  shadowAttempt2          — 原出場(固定 TP RR4 + Trail50L10)。
  A2L shadowAttempt2 + ladder — 配無TP階梯滾動出場。

Run:  PYTHONIOENCODING=utf-8 python -m scripts.attempt2_shadow_study
"""
from __future__ import annotations

import copy
import logging

from backend.backtest.engine import _topstep_trade_date
from backend.data import candle_store
from backend.db.models import (
    BacktestConfig, Direction, _extract_symbol, get_commission_rt, get_fees_rt,
)
from backend.terminal_live import (
    BUILTIN_PRESETS, CLAUDE_701_PRESET_1, _build_strategy_params,
)
from scripts.combo_study import ComboBacktest

INITIAL_CAPITAL = 50_000.0


class ShadowAttempt2Backtest(ComboBacktest):
    """同日同方向第一次訊號 → 影子單;影子死了才放行真單。"""

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self._shadows = []      # {key, dir, entry, sl, tp}
        self._dir_ready = set() # (trade_date, Direction) 影子已死 → 解鎖

    def _process_candle(self, candle):
        d = _topstep_trade_date(candle.timestamp)
        for s in list(self._shadows):
            if s["key"][0] != d:
                self._shadows.remove(s)
                continue
            if s["dir"] == Direction.BUY:
                hit_sl = candle.low <= s["sl"]
                hit_tp = candle.high >= s["tp"]
            else:
                hit_sl = candle.high >= s["sl"]
                hit_tp = candle.low <= s["tp"]
            if hit_sl:                      # 同根同碰保守算 SL → 解鎖
                self._dir_ready.add(s["key"])
                self._shadows.remove(s)
            elif hit_tp:
                self._shadows.remove(s)     # 真突破但沒上;繼續等新的第一次
        super()._process_candle(candle)

    def _gates_pass(self, candle, sig) -> bool:
        if not super()._gates_pass(candle, sig):
            return False
        key = (_topstep_trade_date(candle.timestamp), sig.direction)
        if key in self._dir_ready:
            return True
        if not any(s["key"] == key for s in self._shadows):
            self._shadows.append({
                "key": key, "dir": sig.direction,
                "entry": float(sig.entry_price),
                "sl": float(sig.sl_price), "tp": float(sig.tp_price),
            })
        return False


def _run(params, candles, **kw):
    cid = params.contract_id
    config = BacktestConfig(
        strategies=["trend"], initial_capital=INITIAL_CAPITAL,
        symbol=_extract_symbol(cid), commission_rt=get_commission_rt(cid),
        fees_rt=get_fees_rt(cid),
        value_area_pct=float(getattr(params, "value_area_pct", 0.80)),
    )
    result = ShadowAttempt2Backtest(
        config=config, strategy_params=params,
        zone_timeline=None, record_equity=False, **kw,
    ).run(candles)
    m = result.metrics
    return {
        "trades": int(m.total_trades), "win_rate": float(m.win_rate),
        "pnl": float(m.total_pnl), "max_dd": float(m.max_drawdown),
        "pf": float(m.profit_factor), "calmar": float(m.calmar_ratio),
        "expectancy": float(m.expectancy),
    }


def _row(tag, r):
    print(f"{tag:<24} {r['trades']:>6} {100*r['win_rate']:>6.1f}% "
          f"{r['pnl']:>+11.1f} {r['max_dd']:>9.1f} {r['pf']:>6.2f} "
          f"{r['calmar']:>7.2f} {r['expectancy']:>+9.2f}", flush=True)


def main():
    logging.getLogger("backend").setLevel(logging.WARNING)
    candles = candle_store.load("MNQ", 1)
    if not candles:
        raise SystemExit("No MNQ 1m candles.")
    candles.sort(key=lambda c: c.timestamp)
    print(f"candles {len(candles)}  {candles[0].timestamp} -> {candles[-1].timestamp}", flush=True)
    print("(base 對照:+7181.3 / DD 791.4 / PF 1.47;ladder 對照:+8043.5 / DD 915.4 / PF 1.66)", flush=True)

    preset = BUILTIN_PRESETS[CLAUDE_701_PRESET_1]
    cid = preset.get("contract_id", "CON.F.US.MNQ.U26")
    base = _build_strategy_params(preset, cid)
    base.value_area_pct = float(preset.get("value_area_pct", 0.70))

    header = (f"{'variant':<24} {'trades':>6} {'win%':>7} {'pnl':>11} "
              f"{'maxDD':>9} {'PF':>6} {'Calmar':>7} {'expect':>9}")
    print("\n" + header, flush=True)
    print("-" * len(header), flush=True)

    _row("A2 shadowAttempt2", _run(copy.deepcopy(base), candles))
    _row("A2L shadow+ladder", _run(copy.deepcopy(base), candles, ladder=True))


if __name__ == "__main__":
    main()
