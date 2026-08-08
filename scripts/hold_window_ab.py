"""1.0.10: 固定時間窗口出場 vs 抱到 SL/TP —— 六年 A/B。

使用者假設:「大 SL/TP 的高獲利是因為直接抄底就 hold 很久,剛好某天反彈。
若改成固定 2 小時收網,會不會更平衡?而且一天能多次進場而非只有一次。」

引擎裡的時間出場機制**還在**(`engine.py:545` 的 `_pmo_max_hold_minutes`),
只是 `routes.py` 的 request builder 把 `factor_max_hold_bars` 釘成 0。
本腳本直接設 `params.factor_max_hold_bars`,不動生產路徑。

⚠️ 這是**研究測試**,不是把 HOLD 控制項加回 UI。
記憶裡「time-exit permanently OFF」指的是 preset 的預設值,不是禁止研究。

同時輸出持倉時間分布,驗證「賺錢的單是不是抱比較久」。

用法:
    python scripts/hold_window_ab.py --preset BEST --holds 0,12,24,48
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import sys
from collections import defaultdict
from datetime import timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import logging
logging.disable(logging.INFO)

from backend.api.routes import BacktestRequest, _build_strategy_params_from_request  # noqa
from backend.backtest.engine import BacktestEngine, BacktestConfig  # noqa
from backend.backtest.sweep import _extract_symbol  # noqa
from backend.data import candle_store  # noqa
try:
    from backend.backtest.costs import get_commission_rt, get_fees_rt
except ImportError:
    from backend.backtest.sweep import get_commission_rt, get_fees_rt


def _utc(t):
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t


def trade_year(dt):
    d = _utc(dt)
    return (d + timedelta(days=1)).year if d.hour >= 22 else d.year


def stats(p):
    if not p:
        return 0, 0.0, 0.0
    g = sum(x for x in p if x > 0)
    l = -sum(x for x in p if x <= 0)
    return len(p), sum(p), (g / l if l > 0 else float("inf") if g > 0 else 0.0)


def run(preset, bars, symbol, hold_bars, adaptive=0):
    cid = f"CON.F.US.{symbol}.U26"
    req = BacktestRequest()
    req.contract_id = cid
    for k, v in preset.items():
        if hasattr(req, k) and k not in ("contract_id", "contract_size"):
            setattr(req, k, v)
    req.factor_pmo_adaptive_window = adaptive
    p = _build_strategy_params_from_request(req, 1)
    p.contract_id = cid
    # routes 把它釘成 0;研究時直接覆寫(engine.py:167 讀的就是這個欄位)
    p.factor_max_hold_bars = int(hold_bars)
    cfg = BacktestConfig(strategies=["trend"], initial_capital=50_000.0,
                         symbol=_extract_symbol(cid),
                         commission_rt=get_commission_rt(cid),
                         fees_rt=get_fees_rt(cid), value_area_pct=0.80)
    res = BacktestEngine(config=cfg, strategy_params=p, record_equity=False).run(bars)

    by_year, allp, holds, win_h, los_h = defaultdict(list), [], [], [], []
    days = set()
    for t in res.trades:
        v = float(t.pnl or 0.0)
        by_year[trade_year(t.entry_time)].append(v)
        allp.append(v)
        if t.entry_time and t.exit_time:
            mins = (_utc(t.exit_time) - _utc(t.entry_time)).total_seconds() / 60
            holds.append(mins)
            (win_h if v > 0 else los_h).append(mins)
        days.add(_utc(t.entry_time).date())

    ys = {y: stats(by_year[y]) for y in sorted(by_year)}
    elig = [y for y in ys if ys[y][0] >= 8]
    fin = [ys[y][2] for y in elig if ys[y][2] != float("inf")]
    n, pnl, pf = stats(allp)
    return {
        "n": n, "pnl": pnl, "pf": pf, "yearly": ys,
        "worst_year_pf": min(fin) if fin else 0.0,
        "years_profitable": sum(1 for y in elig if ys[y][1] > 0),
        "years": len(elig),
        "hold_med": st.median(holds) if holds else 0,
        "hold_p90": sorted(holds)[int(len(holds) * .9)] if holds else 0,
        "win_hold": st.median(win_h) if win_h else 0,
        "los_hold": st.median(los_h) if los_h else 0,
        "per_day": n / max(len(days), 1),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="MNQ")
    ap.add_argument("--preset", default="BEST")
    ap.add_argument("--holds", default="0,12,24,48")
    ap.add_argument("--adaptive", type=int, default=0)
    a = ap.parse_args()

    preset = json.load(open(ROOT / "data" / "presets.json",
                            encoding="utf-8"))["presets"][a.preset]
    bars = sorted(candle_store.load(a.symbol, 1), key=lambda c: c.timestamp)
    tfm = int(preset.get("factor_timeframe_minutes", 5) or 5)
    print(f"[{a.symbol}] {len(bars):,} 根  preset={a.preset}  "
          f"adaptive={a.adaptive}  (1 bar = {tfm}m)\n")

    holds = [int(x) for x in a.holds.split(",")]
    out, years = {}, set()
    for h in holds:
        r = run(preset, bars, a.symbol, h, a.adaptive)
        out[h] = r
        years |= set(r["yearly"])
        lbl = "抱到 SL/TP" if h == 0 else f"{h}根={h*tfm//60}h{h*tfm%60:02d}m"
        print(f"{lbl:<16} n={r['n']:>4} {r['per_day']:>4.1f}筆/日  "
              f"PnL={r['pnl']:>9,.0f}  PF={r['pf']:>5.2f}  "
              f"最差年PF={r['worst_year_pf']:>5.2f}  獲利年={r['years_profitable']}/{r['years']}  "
              f"持倉中位={r['hold_med']:>5.0f}m")

    print(f"\n持倉時間(分鐘) —— 驗證「賺錢的單是不是抱比較久」")
    print(f"{'設定':<16}{'中位':>7}{'p90':>8}{'賺單中位':>10}{'賠單中位':>10}")
    print("-" * 52)
    for h in holds:
        r = out[h]
        lbl = "抱到 SL/TP" if h == 0 else f"{h}根"
        print(f"{lbl:<16}{r['hold_med']:>7.0f}{r['hold_p90']:>8.0f}"
              f"{r['win_hold']:>10.0f}{r['los_hold']:>10.0f}")

    print(f"\n逐年 PnL")
    print(f"{'年':<7}" + "".join(f"{('抱死' if h==0 else str(h)+'根'):>13}" for h in holds))
    print("-" * (7 + 13 * len(holds)))
    for y in sorted(years):
        row = f"{y:<7}"
        for h in holds:
            v = out[h]["yearly"].get(y)
            row += f"{(f'{v[1]:>8,.0f}(n{v[0]})' if v else '—'):>13}"
        print(row)


if __name__ == "__main__":
    main()
