"""1.0.8 研究(僅腳本):階梯滾動出場(無TP 2R觸發/1R步進)逐筆解剖。

對 rolling_exit_study 的 B 變體(ladder)展開:
  1. 出場 R 倍數分佈(死在 -1R / 保本帶 / 1-2R / 2-3R / 3-5R / 5R+)
  2. 每筆最高浮盈 max_r vs 實際出場 R(階梯回吐成本)
  3. 觸發率:多少筆到過 +2R(ladder 啟動)
  4. 持倉時間(贏家/輸家分開)
  5. 月度 PnL 對照 base、日 PnL 統計、maxDD 時間窗
  6. Top 10 贏家 / Top 5 輸家
"""
from __future__ import annotations

import copy
import logging
import statistics
from collections import defaultdict

from backend.backtest.engine import BacktestEngine, _topstep_trade_date
from backend.data import candle_store
from backend.db.models import (
    BacktestConfig, Direction, _extract_symbol, get_commission_rt, get_fees_rt,
)
from backend.terminal_live import (
    BUILTIN_PRESETS, CLAUDE_701_PRESET_1, _build_strategy_params,
)
from scripts.rolling_exit_study import RollingExitBacktest

INITIAL_CAPITAL = 50_000.0


class RollingDetail(RollingExitBacktest):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.records = []
        self._cur_risk = 0.0

    def _execute_entry(self, signal, candle):
        super()._execute_entry(signal, candle)
        self._cur_risk = self._initial_risk

    def _execute_exit(self, candle, exit_price, reason):
        max_r = self._max_r
        risk = self._cur_risk
        super()._execute_exit(candle, exit_price, reason)
        t = self._last_closed_trade
        if t is not None and risk > 0:
            if t.direction == Direction.BUY:
                exit_r = (t.exit_price - t.entry_price) / risk
            else:
                exit_r = (t.entry_price - t.exit_price) / risk
            self.records.append({
                "t": t, "risk": risk, "max_r": max_r, "exit_r": exit_r,
            })


def _analyze(tag, records, trades):
    print(f"\n{'='*70}\n{tag}\n{'='*70}", flush=True)
    n = len(records)
    pnls = [r["t"].pnl or 0.0 for r in records]
    print(f"trades={n}  pnl={sum(pnls):+.1f}  win%={100*sum(1 for p in pnls if p>0)/n:.1f}", flush=True)

    # 1. exit R distribution
    bins = [("<=-1R 滿損", lambda r: r["exit_r"] <= -0.95),
            ("-1R~-0.2R 部分損", lambda r: -0.95 < r["exit_r"] <= -0.2),
            ("保本帶 ±0.2R", lambda r: -0.2 < r["exit_r"] < 0.2),
            ("0.2~1R", lambda r: 0.2 <= r["exit_r"] < 1),
            ("1~2R", lambda r: 1 <= r["exit_r"] < 2),
            ("2~3R", lambda r: 2 <= r["exit_r"] < 3),
            ("3~5R", lambda r: 3 <= r["exit_r"] < 5),
            ("5R+", lambda r: r["exit_r"] >= 5)]
    print("\n-- 出場 R 分佈 --", flush=True)
    for lab, fn in bins:
        xs = [r for r in records if fn(r)]
        if xs:
            s = sum(x["t"].pnl or 0 for x in xs)
            print(f"  {lab:<16} n={len(xs):>4} ({100*len(xs)/n:>4.1f}%)  pnl={s:>+9.1f}", flush=True)

    # 2/3. ladder armed & giveback
    armed = [r for r in records if r["max_r"] >= 2.0]
    print(f"\n-- ladder 啟動(max_r>=2R)--", flush=True)
    print(f"  啟動 {len(armed)}/{n} ({100*len(armed)/n:.1f}%)  啟動後 pnl={sum(r['t'].pnl or 0 for r in armed):+.1f}", flush=True)
    if armed:
        gb = [r["max_r"] - r["exit_r"] for r in armed]
        mr = [r["max_r"] for r in armed]
        print(f"  峰值 max_r: med={statistics.median(mr):.2f}R p90={sorted(mr)[int(len(mr)*0.9)]:.2f}R max={max(mr):.2f}R", flush=True)
        print(f"  回吐(峰值-出場): med={statistics.median(gb):.2f}R p90={sorted(gb)[int(len(gb)*0.9)]:.2f}R", flush=True)
    near = [r for r in records if 1.0 <= r["max_r"] < 2.0]
    print(f"  差一步(1R<=max_r<2R 沒觸發): n={len(near)}  pnl={sum(r['t'].pnl or 0 for r in near):+.1f}", flush=True)

    # 4. hold time
    win = [r for r in records if (r["t"].pnl or 0) > 0]
    los = [r for r in records if (r["t"].pnl or 0) <= 0]
    def _hold(xs):
        hs = [x["t"].duration_minutes or 0 for x in xs]
        return f"med={statistics.median(hs):.0f}m p90={sorted(hs)[int(len(hs)*0.9)]:.0f}m" if hs else "-"
    print(f"\n-- 持倉時間 -- 贏家 {_hold(win)} | 輸家 {_hold(los)}", flush=True)

    # 5. monthly + daily
    mon = defaultdict(float); day = defaultdict(float)
    for r in records:
        t = r["t"]
        d = _topstep_trade_date(t.entry_time)
        mon[str(d)[:7]] += t.pnl or 0
        day[str(d)] += t.pnl or 0
    print("\n-- 月度 pnl --", flush=True)
    for m in sorted(mon):
        print(f"  {m}: {mon[m]:>+9.1f}", flush=True)
    dvals = list(day.values())
    print(f"-- 日 pnl -- 正日 {sum(1 for v in dvals if v>0)}/{len(dvals)}  "
          f"最好 {max(dvals):+.0f}  最差 {min(dvals):+.0f}  日均 {statistics.mean(dvals):+.1f}", flush=True)

    # DD anatomy (trade-close equity)
    eq, peak, dd, dd_lo, dd_hi = 0.0, 0.0, 0.0, None, None
    cur_peak_d = None
    for r in sorted(records, key=lambda x: x["t"].exit_time or x["t"].entry_time):
        d = str(_topstep_trade_date(r["t"].exit_time or r["t"].entry_time))
        eq += r["t"].pnl or 0
        if eq > peak:
            peak, cur_peak_d = eq, d
        if peak - eq > dd:
            dd, dd_lo, dd_hi = peak - eq, cur_peak_d, d
    print(f"-- maxDD(收盤基準)= {dd:.1f}  期間 {dd_lo} -> {dd_hi}", flush=True)

    # 6. top winners / losers
    srt = sorted(records, key=lambda r: r["t"].pnl or 0)
    print("\n-- Top 10 贏家 --", flush=True)
    for r in srt[-10:][::-1]:
        t = r["t"]
        print(f"  {str(t.entry_time)[:16]} {t.direction.value:<4} pnl={t.pnl:>+8.1f} "
              f"risk={r['risk']:.1f}pt exit_r={r['exit_r']:+.2f}R peak={r['max_r']:.2f}R "
              f"hold={t.duration_minutes:.0f}m {t.exit_reason.value}", flush=True)
    print("-- Top 5 輸家 --", flush=True)
    for r in srt[:5]:
        t = r["t"]
        print(f"  {str(t.entry_time)[:16]} {t.direction.value:<4} pnl={t.pnl:>+8.1f} "
              f"risk={r['risk']:.1f}pt exit_r={r['exit_r']:+.2f}R peak={r['max_r']:.2f}R "
              f"hold={t.duration_minutes:.0f}m {t.exit_reason.value}", flush=True)


def main():
    logging.getLogger("backend").setLevel(logging.WARNING)
    candles = candle_store.load("MNQ", 1)
    candles.sort(key=lambda c: c.timestamp)
    print(f"candles {len(candles)}  {candles[0].timestamp} -> {candles[-1].timestamp}", flush=True)

    preset = BUILTIN_PRESETS[CLAUDE_701_PRESET_1]
    cid = preset.get("contract_id", "CON.F.US.MNQ.U26")
    base = _build_strategy_params(preset, cid)
    base.value_area_pct = float(preset.get("value_area_pct", 0.70))

    cfg = BacktestConfig(
        strategies=["trend"], initial_capital=INITIAL_CAPITAL,
        symbol=_extract_symbol(cid), commission_rt=get_commission_rt(cid),
        fees_rt=get_fees_rt(cid), value_area_pct=0.70,
    )
    eng = RollingDetail(config=cfg, strategy_params=copy.deepcopy(base),
                        zone_timeline=None, record_equity=False, roll_mode="ladder")
    res = eng.run(candles)
    _analyze("B rollLadder(無TP,2R觸發,1R步進,gap2R)", eng.records, res.trades)

    # base 月度對照(原引擎,固定 TP)
    res2 = BacktestEngine(config=cfg, strategy_params=copy.deepcopy(base),
                          zone_timeline=None, record_equity=False).run(candles)
    mon = defaultdict(float)
    for t in res2.trades:
        mon[str(_topstep_trade_date(t.entry_time))[:7]] += t.pnl or 0
    print("\n-- base(固定TP RR4)月度對照 --", flush=True)
    for m in sorted(mon):
        print(f"  {m}: {mon[m]:>+9.1f}", flush=True)


if __name__ == "__main__":
    main()
