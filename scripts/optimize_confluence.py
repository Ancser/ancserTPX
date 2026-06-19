# ============================================================
# 文件: scripts/optimize_confluence.py
# 狀態: v1.0.6 (confluence parameter optimizer — precompute zones once)
# 用途: 用快取的真實 1m 資料, 把 7-TF 區間時間軸算一次, 再快速掃描訊號參數
# 關聯文件:
#   ← backend/backtest/confluence_backtest.py (build_zone_timeline + replay)
#   ← backend/strategy/confluence.py          (ConfluenceConfig)
# 執行:
#   python -m scripts.optimize_confluence --days 60
# ============================================================
"""Confluence optimizer.

The slow part of a confluence backtest is the per-TF zone detection (volume
profiles over 57k bars). That work is INDEPENDENT of the signal params, so we
build the zone timeline ONCE and replay the cluster -> signal -> one-shot-fill
state machine cheaply for every grid combo.

Grid (the "optimized" search) vs the fixed baseline (band=12, mdt=3, rr=2.0):

    band_ticks      ∈ {8, 12, 16, 24}      cluster proximity
    min_distinct_tf ∈ {2, 3, 4}            confluence strictness
    rr              ∈ {1.5, 2.0, 3.0}      reward:risk
    direction_mode  ∈ {momentum, reversion}
    wait_minutes    ∈ {15, 60}             one-shot timeout

All net-negative right now, so rows rank by total PnL (min 20 trades), with
profit factor reported alongside.
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import pickle
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.db.models import BacktestConfig, get_tick_size
from backend.strategy.confluence import ConfluenceConfig, MAX_RECENCY_DEPTH
from backend.strategy.consolidation import AREA_TIMEFRAME_MINUTES
from backend.backtest.confluence_backtest import (
    ConfluenceBacktester, ConfluenceBacktestConfig, build_zone_timeline,
)

CONTRACT_ID = "CON.F.US.MNQ.M26"
DATA_DIR = ROOT / "data" / "historical"
OUT_DIR = ROOT / "data" / "machinelearning"
TIMEFRAMES = tuple(AREA_TIMEFRAME_MINUTES.keys())

# ── search grid ──
GRID_BAND = (8.0, 12.0, 16.0, 24.0)
GRID_MDT = (2, 3, 4)
GRID_RR = (1.5, 2.0, 3.0)
GRID_MODE = ("momentum", "reversion")
GRID_WAIT = (15, 60)
MIN_TRADES = 20  # ignore tiny-sample rows when ranking

FIXED = {"band_ticks": 12.0, "min_distinct_tf": 3, "rr": 2.0,
         "direction_mode": "reversion", "wait_minutes": 60}


def _load_candles(contract_id: str, days: int):
    safe = contract_id.replace(".", "_")
    prior = sorted(DATA_DIR.glob(f"{safe}_1m_{days}d_*.pkl"))
    if not prior:
        raise SystemExit(
            f"No cached bars for {contract_id} {days}d. "
            f"Run: python -m scripts.run_confluence --days {days}"
        )
    with prior[-1].open("rb") as fh:
        candles = pickle.load(fh)
    candles = sorted(candles, key=lambda c: c.timestamp)
    print(f"[cache] {len(candles)} bars from {prior[-1].name}", flush=True)
    return candles


def _replay(candles, timeline, contract_id, bt_cfg, tick, combo):
    sig = ConfluenceConfig(
        band_ticks=combo["band_ticks"],
        min_distinct_tf=combo["min_distinct_tf"],
        rr=combo["rr"],
        weighted_entry=True,
    )
    sig.direction_mode = combo["direction_mode"]
    sig.tick_size = tick
    run_cfg = ConfluenceBacktestConfig(wait_minutes=combo["wait_minutes"])
    bt = ConfluenceBacktester(
        signal_cfg=sig, run_cfg=run_cfg,
        contract_id=contract_id, bt_config=bt_cfg,
    )
    res = bt.run(candles, zones_timeline=timeline)
    m = res.metrics
    return {
        **combo,
        "trades": len(res.trades),
        "win_rate": round(m.win_rate * 100.0, 1),
        "pnl": round(m.total_pnl, 1),
        "profit_factor": round(m.profit_factor, 2),
        "calmar": round(m.calmar_ratio, 2),
        "max_drawdown": round(m.max_drawdown, 1),
        "avg_rr": round(m.avg_rr_ratio, 2),
        "final_capital": round(res.final_capital, 1),
    }


def _rank_key(r):
    # all net-negative for now → rank by PnL, but bury tiny samples
    enough = r["trades"] >= MIN_TRADES
    return (1 if enough else 0, r["pnl"], r["profit_factor"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--contract", default=CONTRACT_ID)
    args = ap.parse_args()

    candles = _load_candles(args.contract, args.days)
    tick = get_tick_size(args.contract)
    bt_cfg = BacktestConfig()

    print(f"[zones] building {len(TIMEFRAMES)}-TF timeline once over {len(candles)} bars...", flush=True)
    t0 = time.perf_counter()
    timeline = build_zone_timeline(candles, TIMEFRAMES, tick, MAX_RECENCY_DEPTH)
    print(f"[zones] done in {time.perf_counter()-t0:.1f}s", flush=True)

    combos = [
        {"band_ticks": b, "min_distinct_tf": mdt, "rr": rr,
         "direction_mode": mode, "wait_minutes": w}
        for b, mdt, rr, mode, w in itertools.product(
            GRID_BAND, GRID_MDT, GRID_RR, GRID_MODE, GRID_WAIT)
    ]
    print(f"[sweep] {len(combos)} combos (replay, no detector pass)\n", flush=True)

    rows = []
    t0 = time.perf_counter()
    for k, combo in enumerate(combos, 1):
        r = _replay(candles, timeline, args.contract, bt_cfg, tick, combo)
        rows.append(r)
        if k % 12 == 0 or k == len(combos):
            print(f"  ...{k}/{len(combos)}  ({time.perf_counter()-t0:.0f}s)", flush=True)

    rows.sort(key=_rank_key, reverse=True)

    # fixed baseline row for comparison
    fixed_row = next(
        (r for r in rows if all(r[key] == FIXED[key] for key in FIXED)), None
    )

    print("\n=== TOP 10 (ranked by PnL, >=20 trades) ===", flush=True)
    print(f"{'mode':10s}{'band':>5}{'mdt':>4}{'rr':>5}{'wait':>5}"
          f"{'trd':>5}{'wr%':>6}{'pnl':>11}{'pf':>6}", flush=True)
    for r in rows[:10]:
        print(f"{r['direction_mode']:10s}{r['band_ticks']:>5.0f}{r['min_distinct_tf']:>4}"
              f"{r['rr']:>5.1f}{r['wait_minutes']:>5}{r['trades']:>5}{r['win_rate']:>6.1f}"
              f"{r['pnl']:>11.1f}{r['profit_factor']:>6.2f}", flush=True)

    if fixed_row:
        print(f"\n=== FIXED baseline (band=12 mdt=3 rr=2.0 reversion wait=60) ===", flush=True)
        print(f"  trades={fixed_row['trades']} wr={fixed_row['win_rate']}% "
              f"pnl=${fixed_row['pnl']} pf={fixed_row['profit_factor']}", flush=True)

    best = rows[0]
    print(f"\n[best] {best['direction_mode']} band={best['band_ticks']:.0f} "
          f"mdt={best['min_distinct_tf']} rr={best['rr']} wait={best['wait_minutes']}m "
          f"-> pnl=${best['pnl']} pf={best['profit_factor']} trades={best['trades']}", flush=True)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = args.contract.replace(".", "_")
    out = OUT_DIR / f"confluence_opt_{safe}_{args.days}d_{stamp}.csv"
    cols = ["direction_mode", "band_ticks", "min_distinct_tf", "rr", "wait_minutes",
            "trades", "win_rate", "pnl", "profit_factor", "calmar",
            "max_drawdown", "avg_rr", "final_capital"]
    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f"\n[out] {out}", flush=True)


if __name__ == "__main__":
    main()
