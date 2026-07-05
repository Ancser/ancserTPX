"""1.0.9 研究(僅腳本):金字塔 TP(3 MNQ 分批出場)vs 1 MNQ ladder vs 3 MNQ ladder。

使用者提案:買 3 口,不同層級 TP。忠實模擬(逐筆前向走 K):
  以現行 FABLE#1(VA70 ladder C4 已改 C3 S4 對照;此處用 ladder C3 S0 取足量樣本)
  的每一筆進場為基準,同一進場開 3 口:
    A 口:固定 TP +1R(先到 SL 則 -1R)
    B 口:固定 TP +2R
    C 口:ladder 滾動(= 引擎實際記錄的那口)
  金字塔總 pnl = A + B + C(每口各自 1 MNQ 成本)。

對照:1 MNQ ladder(現行) / 3 MNQ ladder(全進全出 ×3) / 3 MNQ 金字塔。

Run:  PYTHONIOENCODING=utf-8 python -m scripts.pyramid_tp_study
"""
from __future__ import annotations

import bisect
import copy
import logging
import statistics
from collections import defaultdict
from datetime import date as _date

from backend.backtest.engine import BacktestEngine, _topstep_trade_date
from backend.backtest.intrabar import resolve_same_bar_exit
from backend.backtest.sweep import build_trend_zone_timeline
from backend.data import candle_store
from backend.db.models import (
    BacktestConfig, Direction, _extract_symbol, get_commission_rt, get_fees_rt,
)
from backend.terminal_live import BUILTIN_PRESETS, FABLE_702_PRESET_1, _build_strategy_params

MNQ_PV = 2.0
COST_RT = 1.24   # MNQ 手續費+規費 約 $1.24/口 RT


def _sim_leg(candles, ts_list, entry_i, entry, sl, tp, direction, exit_deadline):
    """從 entry_i 之後逐 K 走,回傳該口出場價(先到 SL/TP,同根保守 SL 先)。
    走到 exit_deadline(ladder 口的實際出場時間)仍未觸發 → 以該時收盤平(flatten 對齊)。"""
    n = len(candles)
    for j in range(entry_i + 1, n):
        c = candles[j]
        if c.timestamp > exit_deadline:
            # 用 deadline 當日收盤價近似(A/B 未觸發 → 跟 C 一起被 flatten)
            return float(candles[j - 1].close)
        if direction == Direction.BUY:
            hit_sl = c.low <= sl
            hit_tp = c.high >= tp
            if hit_sl and hit_tp:
                return sl if resolve_same_bar_exit(c.open, sl, tp) == "sl" else tp
            if hit_sl:
                return sl
            if hit_tp:
                return tp
        else:
            hit_sl = c.high >= sl
            hit_tp = c.low <= tp
            if hit_sl and hit_tp:
                return sl if resolve_same_bar_exit(c.open, sl, tp) == "sl" else tp
            if hit_sl:
                return sl
            if hit_tp:
                return tp
    return float(candles[-1].close)


def _pnl(direction, entry, exit_px, contracts=1):
    raw = (exit_px - entry) if direction == Direction.BUY else (entry - exit_px)
    return raw * MNQ_PV * contracts - COST_RT * contracts


def _metrics_from_daily(day_pnls, total_trades):
    dv = list(day_pnls.values())
    # equity DD by trade-close order approximated by daily order
    eq = peak = dd = 0.0
    for d in sorted(day_pnls):
        eq += day_pnls[d]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    tot = sum(dv)
    wd = sum(1 for v in dv if v > 0)
    return tot, dd, (100 * wd / len(dv) if dv else 0), (min(dv) if dv else 0), total_trades


def main():
    logging.getLogger("backend").setLevel(logging.WARNING)
    candles = candle_store.load("MNQ", 1)
    candles.sort(key=lambda c: c.timestamp)
    ts_list = [c.timestamp for c in candles]
    print(f"candles {len(candles)}", flush=True)

    preset = BUILTIN_PRESETS[FABLE_702_PRESET_1]
    base = _build_strategy_params(preset, preset.get("contract_id"))
    base.value_area_pct = 0.70
    base.tr_exit_mode = "ladder"
    base.breakout_confirm_bars = 3
    base.tr_daily_loss_stop = 0     # 取足量樣本看金字塔效果
    tl = build_trend_zone_timeline(candles, "5m", 0.70)
    cfg = BacktestConfig(strategies=["trend"], initial_capital=50000.0,
        symbol=_extract_symbol(base.contract_id), commission_rt=get_commission_rt(base.contract_id),
        fees_rt=get_fees_rt(base.contract_id), value_area_pct=0.70)
    res = BacktestEngine(config=cfg, strategy_params=copy.deepcopy(base),
                         zone_timeline=tl, record_equity=False).run(candles)
    trades = res.trades
    print(f"ladder trades={len(trades)}", flush=True)

    lad_day = defaultdict(float)     # 1 MNQ ladder(C 口)
    lad3_day = defaultdict(float)    # 3 MNQ ladder 全進全出
    pyr_day = defaultdict(float)     # 3 MNQ 金字塔(A@1R B@2R C ladder)
    r_reach_1r = r_reach_2r = 0

    for t in trades:
        d = _topstep_trade_date(t.entry_time)
        entry = float(t.entry_price)
        sl0 = float(getattr(t, "original_sl_price", t.sl_price))
        R = abs(entry - sl0)
        c_pnl = t.pnl or 0.0
        lad_day[d] += c_pnl
        lad3_day[d] += c_pnl * 3
        if R <= 0:
            pyr_day[d] += c_pnl * 3
            continue
        ei = bisect.bisect_left(ts_list, t.entry_time)
        if ei >= len(candles):
            pyr_day[d] += c_pnl * 3
            continue
        dl = t.exit_time or candles[-1].timestamp
        if t.direction == Direction.BUY:
            tpA, tpB = entry + R, entry + 2 * R
        else:
            tpA, tpB = entry - R, entry - 2 * R
        exA = _sim_leg(candles, ts_list, ei, entry, sl0, tpA, t.direction, dl)
        exB = _sim_leg(candles, ts_list, ei, entry, sl0, tpB, t.direction, dl)
        pA = _pnl(t.direction, entry, exA)
        pB = _pnl(t.direction, entry, exB)
        if abs(exA - tpA) < 1e-6: r_reach_1r += 1
        if abs(exB - tpB) < 1e-6: r_reach_2r += 1
        pyr_day[d] += pA + pB + c_pnl

    print("\n{:<24} {:>7} {:>8} {:>8} {:>9}".format("variant","pnl","maxDD","winDay%","worstDay"), flush=True)
    for lbl, dd_map in [("1 MNQ ladder(現行)", lad_day),
                        ("3 MNQ ladder(全進全出)", lad3_day),
                        ("3 MNQ 金字塔(1R/2R/run)", pyr_day)]:
        tot, dd, wdp, worst, _ = _metrics_from_daily(dd_map, len(trades))
        print("{:<24} {:>+7.0f} {:>8.0f} {:>7.1f}% {:>+9.0f}".format(lbl, tot, dd, wdp, worst), flush=True)
    print(f"\nA 口(+1R)觸及率 {100*r_reach_1r/max(len(trades),1):.1f}%  "
          f"B 口(+2R)觸及率 {100*r_reach_2r/max(len(trades),1):.1f}%", flush=True)


if __name__ == "__main__":
    main()
