"""1.0.8 研究(僅腳本):組合帳本 round 2 — ladder 書配軟斷路器壓 DD。

Round 1 結論:ladder(+8264/DD915)+ fadeA4只多(+1255/DD247)
  → 合併 +9518 / DD 915(相關性 +0.04,但 fade 沒墊到 6/9-11 連敗窗)。
目標:合併 DD ≤ ~700,PnL 儘量留住。

本輪:ladder + 日虧 N 單斷路器(N=3 / 4,比 C4 的 2 溫和),各自與 fadeA4 合併。

Run:  PYTHONIOENCODING=utf-8 python -m scripts.portfolio_round2
"""
from __future__ import annotations

import copy
import logging
import statistics
from collections import defaultdict

from backend.backtest.engine import _topstep_trade_date
from backend.data import candle_store
from backend.db.models import BacktestConfig, _extract_symbol, get_commission_rt, get_fees_rt
from backend.terminal_live import (
    BUILTIN_PRESETS, CLAUDE_701_PRESET_1, _build_strategy_params,
)
from scripts.combo_study import ComboBacktest          # ladder + daily_stop
from scripts.portfolio_study import RefineBacktest, _cfg, _metrics, _row

INITIAL_CAPITAL = 50_000.0


def merged(trades_a, trades_b, label, fade_mult=1.0):
    evs = ([(t.exit_time or t.entry_time, t.pnl or 0.0, "L") for t in trades_a]
           + [(t.exit_time or t.entry_time, (t.pnl or 0.0) * fade_mult, "F") for t in trades_b])
    evs.sort(key=lambda x: x[0])
    eq = peak = dd = 0.0
    dd_lo = dd_hi = peak_d = None
    daily = defaultdict(float)
    for ts, p, tag in evs:
        d = str(_topstep_trade_date(ts))
        daily[d] += p
        eq += p
        if eq > peak:
            peak, peak_d = eq, d
        if peak - eq > dd:
            dd, dd_lo, dd_hi = peak - eq, peak_d, d
    tot = sum(p for _, p, _ in evs)
    dvals = list(daily.values())
    print(f"\n{label}: 合併 pnl={tot:+.1f}  maxDD={dd:.1f}  ({dd_lo} -> {dd_hi})  "
          f"日勝率 {100*sum(1 for v in dvals if v>0)/len(dvals):.0f}%  最差日 {min(dvals):+.0f}", flush=True)


def main():
    logging.getLogger("backend").setLevel(logging.WARNING)
    candles = candle_store.load("MNQ", 1)
    candles.sort(key=lambda c: c.timestamp)
    print(f"candles {len(candles)}  {candles[0].timestamp} -> {candles[-1].timestamp}", flush=True)

    preset = BUILTIN_PRESETS[CLAUDE_701_PRESET_1]
    cid = preset.get("contract_id", "CON.F.US.MNQ.U26")

    lad = _build_strategy_params(preset, cid)
    lad.value_area_pct = float(preset.get("value_area_pct", 0.70))

    fad = _build_strategy_params(preset, cid)
    fad.tr_allowed_sessions = None
    fad.one_trade_per_session_direction = False
    fad.tr_one_trade_per_session = False
    fad.full_tp_lock = 0
    fad.tr_full_tp_lock = 0

    # fade A4(只多 SL80)
    p = copy.deepcopy(fad)
    fade_res = RefineBacktest(config=_cfg(p), strategy_params=p,
                              zone_timeline=None, record_equity=False,
                              fade_kw={"fade_sl_ticks": 80, "long_only": True}).run(candles)
    _row("fadeA4 只多", _metrics(fade_res))

    # round4: fade 書加倍(2 MNQ,PF2.38/DD247 的書值得加倉)
    for n, mult in ((4, 2.0), (6, 2.0), (None, 2.0)):
        p = copy.deepcopy(lad)
        kw = {"ladder": True}
        if n is not None:
            kw["daily_stop"] = n
        res = ComboBacktest(config=_cfg(p), strategy_params=p,
                            zone_timeline=None, record_equity=False, **kw).run(candles)
        tag = f"ladder+stop{n}" if n else "ladder(無斷路)"
        _row(tag, _metrics(res))
        merged(res.trades, fade_res.trades, f"[{tag} ⊕ fadeA4 x{mult:g}]", fade_mult=mult)


if __name__ == "__main__":
    main()
