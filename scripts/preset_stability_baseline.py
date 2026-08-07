"""1.0.10: 用與 stability_sweep_2026 完全相同的指標,量測現有 preset 的全期表現。

sweep 的網格只掃「策略核心參數」,不含 BEST 額外帶的
`one_trade_per_session_direction` / `rr_ratio` / `tr_daily_loss_stop` 等,
所以 sweep 的結果不能直接跟 BEST 比。本腳本走 preset 的完整設定,
產出同構的指標作為基準線。

用法:  python scripts/preset_stability_baseline.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from stability_sweep_2026 import _stats, trade_month, passes  # noqa: E402
from backend.api.routes import BacktestRequest, _build_strategy_params_from_request
from backend.backtest.engine import BacktestEngine, BacktestConfig
from backend.backtest.sweep import _extract_symbol
from backend.data import candle_store

try:
    from backend.backtest.costs import get_commission_rt, get_fees_rt
except ImportError:
    from backend.backtest.sweep import get_commission_rt, get_fees_rt


def evaluate(preset, bars, scale_by_size=True):
    cid = preset.get("contract_id") or "CON.F.US.MNQ.U26"
    size = int(preset.get("contract_size", 1) or 1) if scale_by_size else 1
    req = BacktestRequest()
    req.contract_id = cid
    for k, v in preset.items():
        if hasattr(req, k):
            setattr(req, k, v)
    params = _build_strategy_params_from_request(req, 1)
    params.contract_id = cid
    cfg = BacktestConfig(
        strategies=["trend"], initial_capital=50_000.0,
        symbol=_extract_symbol(cid), commission_rt=get_commission_rt(cid),
        fees_rt=get_fees_rt(cid),
        value_area_pct=float(preset.get("value_area_pct", 0.80)))
    res = BacktestEngine(config=cfg, strategy_params=params,
                         record_equity=False).run(bars)

    by_month = defaultdict(list)
    allp = []
    for t in res.trades:
        p = float(t.pnl or 0.0) * size
        by_month[trade_month(t.entry_time)].append(p)
        allp.append(p)
    n, pnl, pf = _stats(allp)
    months = sorted(by_month)
    ms = {m: _stats(by_month[m]) for m in months}
    segs = {"S1": ("2026-01", "2026-03"), "S2": ("2026-04", "2026-06"),
            "S3": ("2026-06", "2026-08")}
    seg_pf = {}
    for k, (a, b) in segs.items():
        sp = [p for m in months if a <= m <= b for p in by_month[m]]
        seg_pf[k] = _stats(sp)[2]
    finite = [v for v in seg_pf.values() if v != float("inf")]
    return {
        "n": n, "pnl": pnl, "pf": None if pf == float("inf") else pf,
        "size": size,
        "months_traded": len(months),
        "months_profitable": sum(1 for m in months if ms[m][1] > 0),
        "worst_month_pnl": min((ms[m][1] for m in months), default=0.0),
        "worst_seg_pf": min(finite) if finite else 0.0,
        "seg_pf": {k: (None if v == float("inf") else v) for k, v in seg_pf.items()},
        "monthly": {m: {"n": ms[m][0], "pnl": ms[m][1]} for m in months},
    }


def main():
    cfg = json.load(open(ROOT / "data" / "presets.json", encoding="utf-8"))
    bars = sorted(candle_store.load("MNQ", 1), key=lambda c: c.timestamp)
    print(f"資料 {len(bars):,} 根\n")

    rows = []
    for name, preset in cfg["presets"].items():
        try:
            r = evaluate(preset, bars)
        except Exception as e:
            print(f"  {name[:40]:<42} ERROR {type(e).__name__}: {e}")
            continue
        r["name"] = name
        rows.append(r)

    rows.sort(key=lambda r: -(r["worst_seg_pf"] or 0))
    print(f"{'preset':<44}{'口':>3}{'n':>5}{'PnL':>10}{'PF':>6}"
          f"{'最差段':>7}{'獲利月':>7}{'最差月':>9}  過閘")
    print("-" * 100)
    for r in rows:
        gate = "✔" if passes({**r, "seg_pf": r["seg_pf"], "error": None}) else ""
        print(f"{r['name'][:42]:<44}{r['size']:>3}{r['n']:>5}{r['pnl']:>10,.0f}"
              f"{(r['pf'] or 0):>6.2f}{r['worst_seg_pf']:>7.2f}"
              f"{r['months_profitable']:>4}/{r['months_traded']}"
              f"{r['worst_month_pnl']:>9,.0f}   {gate}")

    out = ROOT / "data" / "research" / "preset_stability_baseline.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    json.dump(rows, open(out, "w", encoding="utf-8"), ensure_ascii=False,
              indent=1, default=float)
    print(f"\n寫入 {out}")


if __name__ == "__main__":
    main()
