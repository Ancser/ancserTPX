"""1.0.9 研究(僅腳本):Fade 更大 SL 緩衝 + 更靈活 TP 網格。

使用者需求:fade 想要「SL 更高緩衝、TP 更靈活」。
網格(只做多,前日 VAL 接多):
  SL 緩衝(tick):80(現行) / 100 / 120 / 150
  TP(以 VAL→POC 距離為單位):0.75(較近,命中高) / 1.0(=POC,現行) /
                              1.25 / 1.5(越過 POC 朝 VAH,較貪)
每格報 pnl/maxDD/PF/勝率/勝日%/最差日 + walk-forward 三段 + P1 ACC 判定。

Run:  PYTHONIOENCODING=utf-8 python -m scripts.fade_flex_study
"""
from __future__ import annotations

import copy
import logging
from collections import defaultdict
from datetime import date as _date

from backend.backtest.engine import _topstep_trade_date
from backend.data import candle_store
from backend.db.models import BacktestConfig, _extract_symbol, get_commission_rt, get_fees_rt
from backend.terminal_live import BUILTIN_PRESETS, CLAUDE_701_PRESET_1, _build_strategy_params
from scripts.portfolio_study import RefineBacktest


def _segs(trades):
    day = defaultdict(float)
    for t in trades:
        day[_topstep_trade_date(t.entry_time)] += t.pnl or 0.0
    if not day:
        return [0, 0, 0], 0, 0
    keys = sorted(day)
    d0 = _date.fromisoformat(keys[0])
    span = max(1, (_date.fromisoformat(keys[-1]) - d0).days + 1)
    s = [0.0, 0.0, 0.0]
    for k, v in day.items():
        s[min(2, int((_date.fromisoformat(k) - d0).days * 3 / span))] += v
    dv = list(day.values())
    return s, (100 * sum(1 for v in dv if v > 0) / len(dv)), (min(dv))


def main():
    logging.getLogger("backend").setLevel(logging.WARNING)
    candles = candle_store.load("MNQ", 1)
    candles.sort(key=lambda c: c.timestamp)
    print(f"candles {len(candles)}", flush=True)

    preset = BUILTIN_PRESETS[CLAUDE_701_PRESET_1]
    cid = preset.get("contract_id")
    base = _build_strategy_params(preset, cid)
    base.tr_allowed_sessions = None
    base.one_trade_per_session_direction = False
    base.tr_one_trade_per_session = False
    base.full_tp_lock = 0
    base.tr_full_tp_lock = 0
    cfg = BacktestConfig(strategies=["trend"], initial_capital=50000.0,
        symbol=_extract_symbol(cid), commission_rt=get_commission_rt(cid),
        fees_rt=get_fees_rt(cid), value_area_pct=0.80)

    hdr = "{:<16} {:>6} {:>6} {:>8} {:>7} {:>5} {:>7} {:>8} {:>22} {:>4}".format(
        "SL / TPfrac", "n", "win%", "pnl", "maxDD", "PF", "winD%", "worstD", "seg1/2/3", "ACC")
    print("\n" + hdr, flush=True)
    print("-" * len(hdr), flush=True)
    for sl in (80, 100, 120, 150):
        for frac in (0.75, 1.0, 1.25, 1.5):
            res = RefineBacktest(config=cfg, strategy_params=copy.deepcopy(base),
                                 zone_timeline=None, record_equity=False,
                                 fade_kw={"fade_sl_ticks": sl, "tp_frac": frac,
                                          "long_only": True}).run(candles)
            m = res.metrics
            segs, wdp, worst = _segs(res.trades)
            wf = all(x > 0 for x in segs)
            acc = wf and m.total_trades >= 30 and m.expectancy > 0  # fade 樣本小,門檻降到 30
            print("{:<16} {:>6} {:>5.1f}% {:>+8.0f} {:>7.0f} {:>5.2f} {:>6.1f}% {:>+8.0f} {:>22} {:>4}".format(
                f"SL{sl}/{frac:g}", m.total_trades, 100*m.win_rate, m.total_pnl, m.max_drawdown,
                m.profit_factor, wdp, worst, "/".join(f"{x:+.0f}" for x in segs),
                "★" if acc else "—"), flush=True)


if __name__ == "__main__":
    main()
