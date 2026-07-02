"""1.0.8 研究(僅腳本):5m trend 甜蜜點大掃描(timeline 高效法,144 變體)。

Grid(ASIA 固定,SL80、Trail50L10 固定):
  VA {0.70, 0.80} × 出場 {tp RR2/3/4/5/6, ladder} × confirm {2,3,4,5} × 斷路器 {0,3,4}
  = 2 × 6 × 4 × 3 = 144 變體;timeline 每 VA 建一次,引擎每變體 ~2s。

評分:score = pnl / max(maxDD, 100)(每承受 $1 回撤賺多少)+ 最差單日。
輸出:score 前 40 名 + PnL 前 10 + 現行 FABLE #1 對照。

Run:  PYTHONIOENCODING=utf-8 python -m scripts.sweet_preset_sweep
"""
from __future__ import annotations

import copy
import logging
import time as time_mod
from collections import defaultdict

from backend.backtest.engine import BacktestEngine, _topstep_trade_date
from backend.data import candle_store
from backend.db.models import (
    BacktestConfig, _extract_symbol, get_commission_rt, get_fees_rt,
)
from backend.terminal_live import (
    BUILTIN_PRESETS, FABLE_702_PRESET_1, _build_strategy_params,
)
from scripts.session_ladder_sweep import build_trend_zone_timeline

INITIAL_CAPITAL = 50_000.0


def _run(params, candles, timeline):
    cid = params.contract_id
    config = BacktestConfig(
        strategies=["trend"], initial_capital=INITIAL_CAPITAL,
        symbol=_extract_symbol(cid), commission_rt=get_commission_rt(cid),
        fees_rt=get_fees_rt(cid),
        value_area_pct=float(getattr(params, "value_area_pct", 0.80)),
    )
    result = BacktestEngine(config=config, strategy_params=params,
                            zone_timeline=timeline, record_equity=False).run(candles)
    m = result.metrics
    day = defaultdict(float)
    for t in result.trades:
        day[_topstep_trade_date(t.entry_time)] += t.pnl or 0.0
    worst = min(day.values()) if day else 0.0
    return {
        "trades": int(m.total_trades), "win_rate": float(m.win_rate),
        "pnl": float(m.total_pnl), "max_dd": float(m.max_drawdown),
        "pf": float(m.profit_factor), "expect": float(m.expectancy),
        "worst_day": worst,
        "score": float(m.total_pnl) / max(float(m.max_drawdown), 100.0),
    }


def main():
    logging.getLogger("backend").setLevel(logging.WARNING)
    candles = candle_store.load("MNQ", 1)
    candles.sort(key=lambda c: c.timestamp)
    print(f"candles {len(candles)}  {candles[0].timestamp} -> {candles[-1].timestamp}", flush=True)

    preset = BUILTIN_PRESETS[FABLE_702_PRESET_1]
    cid = preset.get("contract_id")
    base0 = _build_strategy_params(preset, cid)

    results = []
    t_all = time_mod.time()
    for va in (0.70, 0.80):
        t0 = time_mod.time()
        timeline = build_trend_zone_timeline(candles, "5m", va)
        print(f"timeline VA{int(va*100)} built in {time_mod.time()-t0:.0f}s", flush=True)
        for exit_mode, rr in [("tp", 2), ("tp", 3), ("tp", 4), ("tp", 5), ("tp", 6), ("ladder", 4)]:
            for c_bars in (2, 3, 4, 5):
                for stop in (0, 3, 4):
                    p = copy.deepcopy(base0)
                    p.value_area_pct = va
                    p.tr_exit_mode = exit_mode
                    p.rr_ratio = rr
                    p.breakout_confirm_bars = c_bars
                    p.tr_daily_loss_stop = stop
                    r = _run(p, candles, timeline)
                    tag = f"VA{int(va*100)} {'ladder' if exit_mode=='ladder' else 'RR'+str(rr)} C{c_bars} S{stop}"
                    results.append((tag, r))
    print(f"\n{len(results)} variants in {time_mod.time()-t_all:.0f}s", flush=True)

    header = (f"{'#':>3} {'variant':<22} {'trades':>6} {'win%':>6} {'pnl':>9} "
              f"{'maxDD':>7} {'PF':>5} {'expect':>7} {'worstD':>7} {'score':>6}")

    def _rows(items, n):
        for i, (tag, r) in enumerate(items[:n], 1):
            star = " ★現行#1" if tag == "VA70 ladder C3 S4" else ""
            print(f"{i:>3} {tag:<22} {r['trades']:>6} {100*r['win_rate']:>5.1f}% "
                  f"{r['pnl']:>+9.0f} {r['max_dd']:>7.0f} {r['pf']:>5.2f} "
                  f"{r['expect']:>+7.1f} {r['worst_day']:>+7.0f} {r['score']:>6.2f}{star}", flush=True)

    print("\n== score(pnl/DD)前 40 ==", flush=True)
    print(header, flush=True)
    _rows(sorted(results, key=lambda x: -x[1]["score"]), 40)

    print("\n== PnL 前 10 ==", flush=True)
    print(header, flush=True)
    _rows(sorted(results, key=lambda x: -x[1]["pnl"]), 10)

    cur = next((r for t, r in results if t == "VA70 ladder C3 S4"), None)
    if cur:
        print(f"\n現行 FABLE#1(VA70 ladder C3 S4): pnl={cur['pnl']:+.0f} DD={cur['max_dd']:.0f} "
              f"PF={cur['pf']:.2f} worstD={cur['worst_day']:+.0f} score={cur['score']:.2f}", flush=True)


if __name__ == "__main__":
    main()
