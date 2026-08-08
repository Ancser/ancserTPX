"""1.0.10: 固定門檻 vs 自適應門檻 —— 在 6 年資料上對打。

背景:`EMAPMO_LONG_THRESHOLD=-0.10` / `SHORT=0.06` 是絕對常數,但 PMO 由百分比
ROC 建構,量級隨波動縮放。實測 MNQ 2026 逐月觸發率在 14.9%~29.4% 擺盪(2.0 倍)。
`emapmo_adaptive_scale()` 讓門檻隨 PMO 自身離散度縮放,觸發率依構造趨於恆定。

本腳本用 BEST 的完整設定,只改 `factor_pmo_adaptive_window`,比較逐年表現。
判準不是總 PnL —— 而是**最差年 PF** 與**獲利年數**,也就是體制穩定度。

用法:  python scripts/emapmo_adaptive_ab.py [--symbol MNQ]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
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


def stats(pnls):
    if not pnls:
        return 0, 0.0, 0.0
    g = sum(p for p in pnls if p > 0)
    l = -sum(p for p in pnls if p <= 0)
    return len(pnls), sum(pnls), (g / l if l > 0 else float("inf") if g > 0 else 0.0)


def run(preset, bars, symbol, window):
    cid = f"CON.F.US.{symbol}.U26"
    req = BacktestRequest()
    req.contract_id = cid
    for k, v in preset.items():
        if hasattr(req, k) and k not in ("contract_id", "contract_size"):
            setattr(req, k, v)
    req.factor_pmo_adaptive_window = window
    p = _build_strategy_params_from_request(req, 1)
    p.contract_id = cid
    cfg = BacktestConfig(strategies=["trend"], initial_capital=50_000.0,
                         symbol=_extract_symbol(cid),
                         commission_rt=get_commission_rt(cid),
                         fees_rt=get_fees_rt(cid), value_area_pct=0.80)
    res = BacktestEngine(config=cfg, strategy_params=p, record_equity=False).run(bars)
    by_year = defaultdict(list)
    allp = []
    for t in res.trades:
        v = float(t.pnl or 0.0)
        by_year[trade_year(t.entry_time)].append(v)
        allp.append(v)
    ys = {y: stats(by_year[y]) for y in sorted(by_year)}
    elig = [y for y in ys if ys[y][0] >= 8]
    fin = [ys[y][2] for y in elig if ys[y][2] != float("inf")]
    n, pnl, pf = stats(allp)
    return {"n": n, "pnl": pnl, "pf": pf, "yearly": ys,
            "worst_year_pf": min(fin) if fin else 0.0,
            "years_profitable": sum(1 for y in elig if ys[y][1] > 0),
            "years": len(elig)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="MNQ")
    ap.add_argument("--preset", default="BEST")
    ap.add_argument("--windows", default="0,300,600,1200")
    a = ap.parse_args()

    preset = json.load(open(ROOT / "data" / "presets.json",
                            encoding="utf-8"))["presets"][a.preset]
    bars = sorted(candle_store.load(a.symbol, 1), key=lambda c: c.timestamp)
    print(f"[{a.symbol}] {len(bars):,} 根  preset={a.preset}\n")

    wins = [int(x) for x in a.windows.split(",")]
    out = {}
    years = set()
    for w in wins:
        r = run(preset, bars, a.symbol, w)
        out[w] = r
        years |= set(r["yearly"])
        tag = "固定門檻" if w == 0 else f"自適應 {w}"
        print(f"{tag:<14} n={r['n']:>4}  PnL={r['pnl']:>9,.0f}  PF={r['pf']:>5.2f}  "
              f"最差年PF={r['worst_year_pf']:>5.2f}  獲利年={r['years_profitable']}/{r['years']}")

    print(f"\n{'年':<7}" + "".join(f"{('固定' if w==0 else str(w)):>13}" for w in wins))
    print("-" * (7 + 13 * len(wins)))
    for y in sorted(years):
        row = f"{y:<7}"
        for w in wins:
            v = out[w]["yearly"].get(y)
            row += f"{(f'{v[1]:>8,.0f}(n{v[0]})' if v else '—'):>13}"
        print(row)


if __name__ == "__main__":
    main()
