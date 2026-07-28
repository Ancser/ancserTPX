"""1.0.9: RSI2 深度驗證 —— 它是高原還是尖峰?

背景:scripts/public_strategy_research.py 掃了 8 個公開策略族 × 212 變體,
RSI2 是唯一在 MNQ 與 MES 上「各自獨立」通過全部五道關卡的。但那是從 424
個變體裡挑出來的,必然帶挑選偏誤。

本 session 已經栽過一次:在 21 筆樣本上看到「平滑的一列」就叫它高原,
結果在有檢定力的樣本上完全反過來。所以這次先驗證結構,再談使用。

檢驗三件事:
  1. 密網格掃描 —— 通過區是連續的一片,還是孤立的點?
  2. 鄰域穩定度 —— 把任一維推一格,PF / 邊際掉多少?
  3. 兩商品的最佳區是否重疊 —— 真效應應該落在相近的參數區間

判定門檻與其他研究一致:每筆邊際 > 14t 實測往返滑價才有實盤意義。

用法: python scripts/rsi2_robustness.py --symbol MNQ
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from public_strategy_research import (  # noqa: E402
    MEASURED_SLIP_TICKS, MIN_TRADES, _init, _run_job, evaluate, tick_value,
)

GRID = {
    "research_rsi_len":   [2, 3, 4, 5],
    "research_rsi_low":   [2.0, 5.0, 10.0, 15.0, 20.0],
    "research_tf_minutes": [5, 15, 30],
    "factor_side_mode":   ["all", "long_only", "short_only"],
    "factor_sl_value":    [1.5, 2.5],
    "rr_ratio":           [2, 3],
}
LOG = lambda *a: (print(*a), sys.stdout.flush())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="MNQ")
    ap.add_argument("--workers", type=int, default=0)
    args = ap.parse_args()

    keys = list(GRID)
    jobs = [("RSI2", dict(zip(keys, c))) for c in itertools.product(*GRID.values())]
    workers = args.workers or max(2, min(12, (os.cpu_count() or 8) - 4))
    LOG(f"[{args.symbol}] RSI2 密網格 {len(jobs)} 變體, {workers} workers")
    LOG(f"  滑價門檻 {MEASURED_SLIP_TICKS:g}t = ${MEASURED_SLIP_TICKS*tick_value(args.symbol):.2f}/口\n")

    t0 = time.time(); rows = []
    with ProcessPoolExecutor(max_workers=workers, initializer=_init,
                             initargs=(args.symbol,)) as pool:
        futs = [pool.submit(_run_job, j) for j in jobs]
        for i, f in enumerate(as_completed(futs), 1):
            try:
                rows.append(evaluate(f.result(), args.symbol))
            except Exception as exc:
                LOG(f"  FAILED: {type(exc).__name__}: {exc}"); continue
            if i % 100 == 0 or i == len(jobs):
                LOG(f"  {i}/{len(jobs)}  ({time.time()-t0:.0f}s)")

    out = Path(f"data/research/rsi2_robustness_{args.symbol}.json")
    out.write_text(json.dumps({"created_at": datetime.now(timezone.utc).isoformat(),
                               "symbol": args.symbol, "grid": GRID, "results": rows},
                              indent=1, default=str), encoding="utf-8")

    idx = {tuple(r["params"][k] for k in keys): r for r in rows}
    passed = [r for r in rows if r["gate"] == "PASS"]
    over = [r for r in rows if r.get("edge_ticks", -1e9) > MEASURED_SLIP_TICKS
            and r.get("n", 0) >= MIN_TRADES]
    LOG(f"\n{'='*78}\n[{args.symbol}] 通過全部關卡 {len(passed)}/{len(rows)}   "
        f"邊際 > {MEASURED_SLIP_TICKS:g}t 的 {len(over)}/{len(rows)}\n{'='*78}")

    # ── 1. 每個切面的通過率(找連續區)──
    LOG("\n--- 各維度的通過率(看通過區是否集中)---")
    for k in keys:
        LOG(f"\n  {k}")
        for v in GRID[k]:
            sel = [r for r in rows if r["params"][k] == v]
            p = [r for r in sel if r["gate"] == "PASS"]
            o = [r for r in sel if r.get("edge_ticks", -1e9) > MEASURED_SLIP_TICKS
                 and r.get("n", 0) >= MIN_TRADES]
            eg = [r["edge_ticks"] for r in sel if r.get("n", 0) >= MIN_TRADES
                  and r.get("edge_ticks") is not None]
            LOG(f"    {str(v):<12} PASS {len(p):>3}/{len(sel):<4} "
                f"邊際>14t {len(o):>3}  中位邊際 "
                f"{(np.median(eg) if eg else float('nan')):>7.1f}t")

    # ── 2. 通過者列表 ──
    if passed:
        LOG(f"\n--- 通過全部關卡的變體(依邊際排序)---")
        for r in sorted(passed, key=lambda r: -r["edge_ticks"])[:15]:
            p = r["params"]
            LOG(f"  len{p['research_rsi_len']} lo{p['research_rsi_low']:<5g} "
                f"{p['research_tf_minutes']:>2}m {p['factor_side_mode']:<11} "
                f"SL{p['factor_sl_value']} RR{p['rr_ratio']}  "
                f"PF={r['pf']:<6} n={r['n']:<4} 邊際={r['edge_ticks']:>6.1f}t "
                f"DD=${r['max_dd']:<7} 走查={r['seg_pf']}")

    # ── 3. 鄰域穩定度 ──
    LOG(f"\n--- 鄰域穩定度:最佳變體推一格會怎樣 ---")
    elig = [r for r in rows if r.get("n", 0) >= MIN_TRADES]
    if elig:
        best = max(elig, key=lambda r: r.get("edge_ticks", -1e9))
        bp = best["params"]
        LOG(f"  基準: " + "  ".join(f"{k}={bp[k]}" for k in keys))
        LOG(f"         PF={best['pf']} n={best['n']} 邊際={best['edge_ticks']}t "
            f"gate={best['gate']}")
        nb = []
        for k in keys:
            vs = GRID[k]; i = vs.index(bp[k])
            for j in (i - 1, i + 1):
                if 0 <= j < len(vs):
                    key = tuple(vs[j] if kk == k else bp[kk] for kk in keys)
                    r = idx.get(key)
                    if r and r.get("n", 0) >= MIN_TRADES:
                        nb.append((f"{k}={vs[j]}", r))
        if nb:
            eg = [r["edge_ticks"] for _, r in nb]
            pf = [r["pf"] for _, r in nb]
            LOG(f"  {len(nb)} 個直接鄰居:")
            for lbl, r in nb:
                mark = "✅" if r["gate"] == "PASS" else ("○" if r["edge_ticks"] > MEASURED_SLIP_TICKS else "✗")
                LOG(f"    {mark} {lbl:<28} PF={r['pf']:<6} n={r['n']:<4} "
                    f"邊際={r['edge_ticks']:>6.1f}t")
            LOG(f"  鄰居邊際 中位 {np.median(eg):.1f}t  最低 {min(eg):.1f}t  "
                f"(基準 {best['edge_ticks']:.1f}t;跌幅 {100*(1-min(eg)/best['edge_ticks']):.0f}%)")
            LOG(f"  鄰居 PF   中位 {np.median(pf):.2f}  最低 {min(pf):.2f}")
            good = sum(1 for e in eg if e > MEASURED_SLIP_TICKS)
            LOG(f"  → {good}/{len(nb)} 個鄰居仍在滑價門檻之上"
                + ("  ← 高原" if good >= len(nb) * 0.7 else "  ← 尖峰,警戒"))
    LOG(f"\nreport: {out}  ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
