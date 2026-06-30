# ============================================================
# 文件: scripts/validate_confluence.py
# 狀態: v1.0.6 (explainable confluence — out-of-sample validation)
# 關聯文件:
#   ← backend/strategy/confluence_scorer.py   (loads trained weights)
#   ← backend/backtest/confluence_backtest.py (build_zone_timeline + run)
#   ← scripts/confluence_common.py            (load_or_fetch + base/TF helpers)
#   → scripts/confluence_report.py            (per-trade CSV + price chart)
# 執行:
#   python -m scripts.validate_confluence --days 365 --base-min 5 --train-frac 0.80
# ============================================================
"""Out-of-sample validation for the trained confluence scorer.

The scorer was fit ONLY on the front `train-frac` of the data. Here we replay
the sequential backtester with that scorer on:
   • TRAIN split  (in-sample, expect it to look good — that's the fit)
   • TAIL split   (OUT-OF-SAMPLE — the honest test)
   • TAIL with the untrained HEURISTIC (does learning actually beat the prior?)
and sweep a `min_score` gate on the tail (selectivity vs. profit trade-off).

If the tail (out-of-sample) row is profitable and beats the heuristic, the
edge is plausibly real rather than overfit. The full-history zone timeline is
built ONCE and sliced, so the tail's zones still reflect real prior context.

For the OUT-OF-SAMPLE tail it also exports an EXPLAINABLE per-trade CSV (params
+ scoring reasons + realised outcome) and a price chart marking every trade —
so each operation is justified and reproducible, not a black box.
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.db.models import get_tick_size, BacktestConfig
from backend.strategy.confluence import ConfluenceConfig, MAX_RECENCY_DEPTH
from backend.strategy.confluence_scorer import ConfluenceScorer
from backend.backtest.confluence_backtest import (
    ConfluenceBacktester, ConfluenceBacktestConfig, build_zone_timeline,
)
from backend.ml.confluence_common import (
    CONTRACT_ID, MODEL_DIR, OUT_DIR, resolve_candles, timeframes_for_base,
)
from scripts.confluence_report import export_trades_csv, plot_trades


def _run(candles, timeline, cfg, contract_id, scorer, wait, min_score, base):
    run_cfg = ConfluenceBacktestConfig(
        wait_minutes=wait, min_score=min_score, base_minutes=base,
    )
    bt = ConfluenceBacktester(
        signal_cfg=cfg, run_cfg=run_cfg, contract_id=contract_id,
        bt_config=BacktestConfig(), scorer=scorer,
    )
    res = bt.run(candles, zones_timeline=timeline)
    m = res.metrics
    summary = {
        "trades": len(res.trades),
        "wr": round(m.win_rate * 100, 1),
        "pnl": round(m.total_pnl, 1),
        "pf": round(m.profit_factor, 2),
        "calmar": round(m.calmar_ratio, 2),
        "dd": round(m.max_drawdown, 1),
    }
    return summary, res


def _line(tag, r):
    print(f"{tag:30s} trades={r['trades']:>4} wr={r['wr']:>5.1f}% "
          f"pnl=${r['pnl']:>9.1f} pf={r['pf']:>5.2f} calmar={r['calmar']:>5.2f} "
          f"maxDD={r['dd']:>8.1f}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--contract", default=CONTRACT_ID)
    ap.add_argument("--base-min", type=int, default=1, help="minutes per input candle (1 or 5)")
    ap.add_argument("--stitch", type=int, default=1,
                    help="splice N quarterly contracts (non-overlap) for >1yr history")
    ap.add_argument("--use-store", action="store_true",
                    help="validate on the persistent accumulated store (option C)")
    ap.add_argument("--train-frac", type=float, default=0.80)
    ap.add_argument("--wait", type=int, default=60, help="limit-fill timeout in MINUTES")
    ap.add_argument("--band", type=float, default=8.0)
    ap.add_argument("--mdt", type=int, default=3)
    ap.add_argument("--rr", type=float, default=1.5)
    ap.add_argument("--model", default=str(MODEL_DIR / "confluence_scorer.json"))
    args = ap.parse_args()

    base = max(1, args.base_min)
    timeframes = timeframes_for_base(base)
    candles = resolve_candles(args.contract, args.days, base, stitch=args.stitch,
                              use_store=args.use_store)
    tick = get_tick_size(args.contract)
    split = int(len(candles) * args.train_frac)

    trained = ConfluenceScorer.load(args.model)
    heuristic = ConfluenceScorer.heuristic()
    print(f"[model] {Path(args.model).name}  meta={trained.meta}", flush=True)
    print(f"[base] {base}m candles | TFs={timeframes}", flush=True)

    cfg = ConfluenceConfig(band_ticks=args.band, min_distinct_tf=args.mdt, rr=args.rr)
    cfg.direction_mode = "auto"
    cfg.tick_size = tick

    print("[zones] building full-history timeline once...", flush=True)
    tl = build_zone_timeline(candles, timeframes, tick, MAX_RECENCY_DEPTH)
    train_c, train_tl = candles[:split], tl[:split]
    tail_c, tail_tl = candles[split:], tl[split:]
    print(f"[split] train={len(train_c)} bars  tail(OOS)={len(tail_c)} bars\n", flush=True)

    print("=== core comparison (min_score=0) ===", flush=True)
    tr_sum, _ = _run(train_c, train_tl, cfg, args.contract, trained, args.wait, 0.0, base)
    _line("TRAIN  trained (in-sample)", tr_sum)
    oos_sum, oos_res = _run(tail_c, tail_tl, cfg, args.contract, trained, args.wait, 0.0, base)
    _line("TAIL   trained (OUT-OF-SAMPLE)", oos_sum)
    heur_sum, _ = _run(tail_c, tail_tl, cfg, args.contract, heuristic, args.wait, 0.0, base)
    _line("TAIL   heuristic (baseline)", heur_sum)

    print("\n=== TAIL trained: min_score gate sweep (probability) ===", flush=True)
    for p in (0.50, 0.55, 0.60, 0.65):
        logit = math.log(p / (1 - p))
        r, _ = _run(tail_c, tail_tl, cfg, args.contract, trained, args.wait, logit, base)
        _line(f"TAIL p>={p:.2f} (logit {logit:+.2f})", r)

    # ---- explainable per-trade export + chart for the OOS tail ----
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = args.contract.replace(".", "_")
    base_name = f"oos_{safe}_{args.days}d_{base}m_{stamp}"
    csv_out = OUT_DIR / f"{base_name}_trades.csv"
    png_out = OUT_DIR / f"{base_name}_chart.png"
    export_trades_csv(oos_res.trades, csv_out, trained)
    plot_trades(tail_c, oos_res.trades, png_out,
                title=f"OOS {base}m {args.contract} {args.days}d")
    print(f"\n[report] per-trade CSV -> {csv_out}", flush=True)
    print(f"[report] price chart  -> {png_out}", flush=True)


if __name__ == "__main__":
    main()
