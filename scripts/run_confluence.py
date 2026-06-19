# ============================================================
# 文件: scripts/run_confluence.py
# 狀態: v1.0.6 (standalone confluence research runner — base-candle aware)
# 用途: 抓取真實 MNQ 資料(1m 或 5m) → 跑多時間框加權匯流回測 → 輸出比較表
# 關聯文件:
#   ← backend/backtest/confluence_backtest.py  (ConfluenceBacktester)
#   ← backend/strategy/confluence.py           (ConfluenceConfig)
#   ← scripts/confluence_common.py             (load_or_fetch + base/TF helpers)
# 執行:
#   python -m scripts.run_confluence                       # 60 天 1m, 全 matrix
#   python -m scripts.run_confluence --days 365 --base-min 5   # 一年 5m
#   python -m scripts.run_confluence --no-fetch            # 只用快取(離線)
# ============================================================
"""Best-config confluence backtest runner.

Fetches REAL MNQ bars from TopstepX (cached to data/historical so re-runs are
offline & instant), then sweeps a small "best" matrix:

    direction_mode ∈ {momentum, reversion}
    wait_minutes   ∈ {1, 5, 15, 30, 60}

against a single curated signal config. base-min selects the input candle:
  1  -> 1m bars, TFs 5m..4h (full precision, short history)
  5  -> 5m bars, TFs 10m..4h (5x more calendar history per candle count)

Each row is scored by the SHARED MetricsCalculator so numbers match the app.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.db.models import BacktestConfig
from backend.strategy.confluence import ConfluenceConfig
from backend.backtest.confluence_backtest import (
    ConfluenceBacktester, ConfluenceBacktestConfig, WAIT_MINUTES_CHOICES,
)
from scripts.confluence_common import CONTRACT_ID, OUT_DIR, resolve_candles


def _best_signal_cfg() -> ConfluenceConfig:
    """The curated 'best' baseline — all TFs, 3-TF confluence, mid band, RR 2."""
    return ConfluenceConfig(
        band_ticks=12.0,
        min_distinct_tf=3,
        rr=2.0,
        weighted_entry=True,
    )


def run_matrix(candles, contract_id: str, base: int, waits=WAIT_MINUTES_CHOICES):
    bt_cfg = BacktestConfig()
    rows = []
    print(f"\ncandles: {len(candles)}  base={base}m", flush=True)
    for mode in ("momentum", "reversion"):
        for wait in waits:
            sig = _best_signal_cfg()
            sig.direction_mode = mode
            run_cfg = ConfluenceBacktestConfig(wait_minutes=wait, base_minutes=base)
            t0 = time.perf_counter()
            bt = ConfluenceBacktester(
                signal_cfg=sig,
                run_cfg=run_cfg,
                contract_id=contract_id,
                bt_config=bt_cfg,
            )
            res = bt.run(candles)
            dt = time.perf_counter() - t0
            m = res.metrics
            n = len(res.trades)
            sample = res.trades[0].meta if res.trades else {}
            rows.append({
                "mode": mode,
                "wait_min": wait,
                "trades": n,
                "win_rate": round(m.win_rate * 100.0, 1),
                "pnl": round(m.total_pnl, 1),
                "calmar": round(m.calmar_ratio, 2),
                "profit_factor": round(m.profit_factor, 2),
                "max_drawdown": round(m.max_drawdown, 1),
                "final_capital": round(res.final_capital, 1),
                "sample_meta": json.dumps(sample, ensure_ascii=False),
            })
            print(
                f"{mode:9s} wait={wait:>3}m  trades={n:>4} "
                f"wr={m.win_rate*100:5.1f}% pnl=${m.total_pnl:>10.1f} "
                f"calmar={m.calmar_ratio:5.2f} pf={m.profit_factor:5.2f}  ({dt:.1f}s)",
                flush=True,
            )
    return rows


def _write_csv(rows, contract_id: str, days: int, base: int):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = contract_id.replace(".", "_")
    out = OUT_DIR / f"confluence_{safe}_{days}d_{base}m_{stamp}.csv"
    cols = ["mode", "wait_min", "trades", "win_rate", "pnl", "calmar",
            "profit_factor", "max_drawdown", "final_capital", "sample_meta"]
    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f"\n[out] {out}", flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60, help="lookback window (default 60)")
    ap.add_argument("--contract", default=CONTRACT_ID)
    ap.add_argument("--base-min", type=int, default=1, help="minutes per input candle (1 or 5)")
    ap.add_argument("--stitch", type=int, default=1,
                    help="splice N quarterly contracts (non-overlap) for >1yr history")
    ap.add_argument("--use-store", action="store_true",
                    help="run on the persistent accumulated store (option C)")
    ap.add_argument("--no-fetch", action="store_true", help="offline: cache only")
    args = ap.parse_args()

    base = max(1, args.base_min)
    candles = resolve_candles(args.contract, args.days, base, stitch=args.stitch,
                              allow_fetch=not args.no_fetch, use_store=args.use_store)
    rows = run_matrix(candles, args.contract, base)
    best = max(rows, key=lambda r: (r["calmar"], r["pnl"]))
    print(
        f"\n[best] {best['mode']} wait={best['wait_min']}m  "
        f"calmar={best['calmar']} pnl=${best['pnl']} trades={best['trades']}",
        flush=True,
    )
    _write_csv(rows, args.contract, args.days, base)


if __name__ == "__main__":
    main()
