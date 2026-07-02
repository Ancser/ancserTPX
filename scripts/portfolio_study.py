"""1.0.8 研究(僅腳本):F4 fade 細化掃描 + ladder/fade 雙引擎組合帳本。

Part A — futureman fade 腿細化(全部 fades-only):
  A0 SL80 TP=POC(F4 原樣)     A1 SL60      A2 SL40
  A3 SL=半程結構(SL距離 = 到POC距離的一半 → 固定 RR2)
  A4 只做多(fadeLong)SL80      A5 只做多 SL40
  A6 TP=75% 路程(更易成交)     A7 SL40+TP75%
  自動選最佳:score = pnl / max(maxDD,100),要求 trades>=25 且 pnl>0。

Part B — 組合帳本:ladder(5m 動量書)+ 最佳 fade(日級回歸書)
  兩本獨立回測 → 按出場時間合併權益 → 合併 maxDD、日收益相關性、月度。
  (等同兩個帳號並行,各 1 MNQ)

Run:  PYTHONIOENCODING=utf-8 python -m scripts.portfolio_study
"""
from __future__ import annotations

import copy
import logging
import math
import statistics
from collections import defaultdict
from typing import Optional

from backend.backtest.engine import _topstep_trade_date
from backend.data import candle_store
from backend.db.models import (
    BacktestConfig, Candle, Direction, TradeSignal,
    _extract_symbol, get_commission_rt, get_fees_rt,
)
from backend.terminal_live import (
    BUILTIN_PRESETS, CLAUDE_701_PRESET_1, _build_strategy_params,
)
from scripts.futureman_study import FuturemanBacktest, FuturemanStrategy, TICK
from scripts.rolling_exit_study import RollingExitBacktest

INITIAL_CAPITAL = 50_000.0


class RefinedFade(FuturemanStrategy):
    """fades-only + 可調 SL/TP/只做多。"""

    def __init__(self, fade_sl_ticks=80, sl_half=False, tp_frac=1.0, long_only=False):
        super().__init__(rr=2.0, fades=True, breakouts=False)
        self.fade_sl_ticks = int(fade_sl_ticks)
        self.sl_half = bool(sl_half)
        self.tp_frac = float(tp_frac)
        self.long_only = bool(long_only)

    def evaluate(self, candle: Candle, zones, is_mature) -> Optional[TradeSignal]:
        lv = self.levels
        self._prev_close = candle.close
        if not lv:
            return None
        poc, vah, val = lv["poc"], lv["vah"], lv["val"]
        d = lv["date"]

        def used(play):
            return f"{d}:{play}" in self._used

        if not (val < candle.close < vah):
            return None

        # fadeShort @ VAH → POC
        if (not self.long_only and not used("fadeShort")
                and (vah - poc) > 8 * TICK):
            dist = vah - poc
            slp = dist / 2 if self.sl_half else self.fade_sl_ticks * TICK
            slp = max(slp, 4 * TICK)
            tp = vah - dist * self.tp_frac
            return self._mk(candle, "fadeShort", Direction.SELL,
                            vah, vah + slp, tp, "limit")
        # fadeLong @ VAL → POC
        if not used("fadeLong") and (poc - val) > 8 * TICK:
            dist = poc - val
            slp = dist / 2 if self.sl_half else self.fade_sl_ticks * TICK
            slp = max(slp, 4 * TICK)
            tp = val + dist * self.tp_frac
            return self._mk(candle, "fadeLong", Direction.BUY,
                            val, val - slp, tp, "limit")
        return None


class RefineBacktest(FuturemanBacktest):
    def __init__(self, *args, fade_kw=None, **kw):
        super().__init__(*args, fades=True, breakouts=False, **kw)
        self.trend_follow = RefinedFade(**(fade_kw or {}))


def _cfg(params):
    cid = params.contract_id
    return BacktestConfig(
        strategies=["trend"], initial_capital=INITIAL_CAPITAL,
        symbol=_extract_symbol(cid), commission_rt=get_commission_rt(cid),
        fees_rt=get_fees_rt(cid), value_area_pct=0.80,
    )


def _metrics(result):
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
    candles.sort(key=lambda c: c.timestamp)
    print(f"candles {len(candles)}  {candles[0].timestamp} -> {candles[-1].timestamp}", flush=True)

    preset = BUILTIN_PRESETS[CLAUDE_701_PRESET_1]
    cid = preset.get("contract_id", "CON.F.US.MNQ.U26")

    fade_base = _build_strategy_params(preset, cid)
    fade_base.tr_allowed_sessions = None
    fade_base.one_trade_per_session_direction = False
    fade_base.tr_one_trade_per_session = False
    fade_base.full_tp_lock = 0
    fade_base.tr_full_tp_lock = 0

    lad_base = _build_strategy_params(preset, cid)
    lad_base.value_area_pct = float(preset.get("value_area_pct", 0.70))

    header = (f"{'variant':<22} {'trades':>6} {'win%':>7} {'pnl':>11} "
              f"{'maxDD':>9} {'PF':>6} {'Calmar':>7} {'expect':>9}")
    print("\n== Part A. fade 細化(fades-only)==", flush=True)
    print(header, flush=True)
    print("-" * len(header), flush=True)

    variants = [
        ("A0 SL80 TP=POC", {"fade_sl_ticks": 80}),
        ("A1 SL60", {"fade_sl_ticks": 60}),
        ("A2 SL40", {"fade_sl_ticks": 40}),
        ("A3 SL=半程(RR2)", {"sl_half": True}),
        ("A4 只多 SL80", {"fade_sl_ticks": 80, "long_only": True}),
        ("A5 只多 SL40", {"fade_sl_ticks": 40, "long_only": True}),
        ("A6 TP75%", {"fade_sl_ticks": 80, "tp_frac": 0.75}),
        ("A7 SL40+TP75%", {"fade_sl_ticks": 40, "tp_frac": 0.75}),
    ]
    results = {}
    for tag, fkw in variants:
        p = copy.deepcopy(fade_base)
        res = RefineBacktest(config=_cfg(p), strategy_params=p,
                             zone_timeline=None, record_equity=False,
                             fade_kw=fkw).run(candles)
        r = _metrics(res)
        results[tag] = (r, res.trades)
        _row(tag, r)

    # 自動選最佳 fade
    def score(item):
        r, _ = item[1]
        if r["trades"] < 25 or r["pnl"] <= 0:
            return -1e9
        return r["pnl"] / max(r["max_dd"], 100.0)

    best_tag, (best_r, best_trades) = max(results.items(), key=score)
    print(f"\n>> 最佳 fade = {best_tag}  (pnl/DD score={score((best_tag,(best_r,best_trades))):.2f})", flush=True)

    # Part B: ladder book
    print("\n== Part B. 組合帳本:ladder + 最佳 fade ==", flush=True)
    lp = copy.deepcopy(lad_base)
    lad_res = RollingExitBacktest(config=_cfg(lp), strategy_params=lp,
                                  zone_timeline=None, record_equity=False,
                                  roll_mode="ladder").run(candles)
    lad_r = _metrics(lad_res)
    _row("ladder 書", lad_r)
    _row(f"fade 書 {best_tag}", best_r)

    # 合併
    evs = ([(t.exit_time or t.entry_time, t.pnl or 0.0, "L") for t in lad_res.trades]
           + [(t.exit_time or t.entry_time, t.pnl or 0.0, "F") for t in best_trades])
    evs.sort(key=lambda x: x[0])
    eq = peak = dd = 0.0
    dd_lo = dd_hi = None
    peak_d = None
    daily = defaultdict(lambda: [0.0, 0.0])
    for ts, p, tag in evs:
        d = str(_topstep_trade_date(ts))
        daily[d][0 if tag == "L" else 1] += p
        eq += p
        if eq > peak:
            peak, peak_d = eq, d
        if peak - eq > dd:
            dd, dd_lo, dd_hi = peak - eq, peak_d, d

    tot = lad_r["pnl"] + best_r["pnl"]
    print(f"\n合併: pnl={tot:+.1f}  合併maxDD={dd:.1f}  (期間 {dd_lo} -> {dd_hi})", flush=True)
    print(f"單書DD: ladder {lad_r['max_dd']:.1f} + fade {best_r['max_dd']:.1f} = 簡單加總 {lad_r['max_dd']+best_r['max_dd']:.1f}"
          f" → 分散化節省 {lad_r['max_dd']+best_r['max_dd']-dd:+.1f}", flush=True)

    ls = [v[0] for v in daily.values()]
    fs = [v[1] for v in daily.values()]
    if len(ls) > 2 and statistics.pstdev(ls) > 0 and statistics.pstdev(fs) > 0:
        mx, my = statistics.mean(ls), statistics.mean(fs)
        num = sum((a - mx) * (b - my) for a, b in zip(ls, fs))
        den = math.sqrt(sum((a - mx) ** 2 for a in ls)) * math.sqrt(sum((b - my) ** 2 for b in fs))
        print(f"日收益相關性 corr(L,F) = {num/den:+.3f}", flush=True)

    tot_daily = [a + b for a, b in daily.values()]
    print(f"合併日勝率 {100*sum(1 for v in tot_daily if v>0)/len(tot_daily):.0f}%  "
          f"最差日 {min(tot_daily):+.0f}  最好日 {max(tot_daily):+.0f}", flush=True)
    mon = defaultdict(float)
    for d, (a, b) in daily.items():
        mon[d[:7]] += a + b
    for m in sorted(mon):
        print(f"  {m}: {mon[m]:>+9.1f}", flush=True)


if __name__ == "__main__":
    main()
