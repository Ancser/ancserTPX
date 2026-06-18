# ============================================================
# 文件: scripts/train_confluence_ev.py
# 狀態: v0.20.0 (explainable confluence — VARIABLE-RR / EV scorer trainer)
# 用途: option C 第二階段。同一個可解釋線性模型，但每個候選用「一次前向掃描」
#       取得多個 RR 的勝負標籤 (TP@r 是否先於 SL 觸發)，把 rr 當成 geometry
#       特徵一起餵入 → 學到 rr 的權重 → 推論時對 RR 網格逐一算 EV，挑最佳 RR。
# 關聯文件:
#   ← backend/backtest/confluence_backtest.py (build_zone_timeline)
#   ← backend/strategy/confluence.py          (clusters + signal geometry)
#   ← backend/strategy/confluence_features.py (FEATURE_NAMES incl. 'rr')
#   ← scripts/train_confluence.py             (evaluate_and_meta — shared fit)
#   ← scripts/confluence_label.py             (simulate_outcomes — shared labeler)
#   → backend/strategy/confluence_scorer.py   (saves raw-space weights JSON)
# 執行:
#   python -m scripts.train_confluence_ev --days 60 --rr-grid 1.0,1.5,2.0,2.5,3.0
# ============================================================
"""Train the VARIABLE-RR confluence scorer (interpretable logistic regression).

Difference vs scripts/train_confluence.py (fixed RR):
  * one forward scan per candidate yields a win/loss label for EVERY RR in the
    grid (TP@r reached before SL?), instead of a single fixed-RR label;
  * each (candidate, rr) becomes its own training row — `rr` (a geometry
    feature) therefore VARIES across rows, so the logistic model learns how win
    probability falls as RR rises;
  * at inference the decision layer sweeps the same RR grid per candidate and
    picks RR* = argmax EV (see ConfluenceConfig.rr_grid).

The model + JSON format are identical to the fixed-RR scorer (same FEATURE_NAMES,
raw-space weights, no hidden transform), so it stays fully explainable and live
== backtest. Only the TRAIN split is fit; the tail is left for validation.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.db.models import get_tick_size
from backend.strategy.confluence import (
    ConfluenceConfig, MAX_RECENCY_DEPTH,
    extract_levels, cluster_levels, _signal_geometry, _make_signal,
)
from backend.strategy.confluence_features import (
    FEATURE_NAMES, features_to_vector, extract_features, CONTEXT_WINDOW,
)
from backend.strategy.confluence_scorer import ConfluenceScorer
from backend.backtest.confluence_backtest import build_zone_timeline
from scripts.train_confluence import evaluate_and_meta
from scripts.confluence_label import simulate_outcomes
from scripts.confluence_common import (
    CONTRACT_ID, MODEL_DIR, resolve_candles, timeframes_for_base,
)


def collect(candles, timeline, cfg, rr_grid, stride, wait, horizon):
    """Multi-RR forward-scan labels. One scan per candidate emits one row per RR
    that resolves; `rr` therefore varies across rows so the model learns it.
    Returns (X, y, meta, starts, ends) — starts/ends bound each row's outcome
    window for uniqueness weighting + embargoed OOS."""
    tick = cfg.tick_size
    modes = ("momentum", "reversion", "breakout")
    X, y, meta, starts, ends = [], [], [], [], []
    n = len(candles)
    edge = wait + horizon + 2
    for i in range(0, n - edge, stride):
        snap = timeline[i]
        if len(snap) < cfg.min_distinct_tf:
            continue
        levels = extract_levels(snap, cfg)
        if not levels:
            continue
        recent = candles[max(0, i - CONTEXT_WINDOW + 1):i + 1]
        for cl in cluster_levels(levels, cfg):
            for mode in modes:
                geom = _signal_geometry(cl, candles[i].close, snap, mode, cfg,
                                        recent_candles=recent)
                if geom is None:
                    continue
                direction, entry, sl, risk = geom
                labels = simulate_outcomes(candles, i, direction, entry, sl, risk,
                                           rr_grid, wait, horizon)
                for rr, (label, end_idx) in labels.items():
                    sig = _make_signal(cl, mode, geom, rr)
                    feats = extract_features(sig, candles[i].close, tick,
                                             levels=levels, recent_candles=recent)
                    X.append(features_to_vector(feats))
                    y.append(label)
                    meta.append((mode, rr))
                    starts.append(i)
                    ends.append(end_idx)
    return (np.array(X, dtype=float), np.array(y, dtype=int), meta,
            np.array(starts, dtype=int), np.array(ends, dtype=int))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--contract", default=CONTRACT_ID)
    ap.add_argument("--base-min", type=int, default=1)
    ap.add_argument("--stitch", type=int, default=1)
    ap.add_argument("--use-store", action="store_true")
    ap.add_argument("--train-frac", type=float, default=0.80)
    ap.add_argument("--stride", type=int, default=5)
    ap.add_argument("--wait", type=int, default=60, help="limit-fill timeout in MINUTES")
    ap.add_argument("--horizon", type=int, default=1440, help="SL/TP resolve window in MINUTES")
    ap.add_argument("--band", type=float, default=8.0)
    ap.add_argument("--mdt", type=int, default=3)
    ap.add_argument("--rr-grid", default="1.0,1.5,2.0,2.5,3.0",
                    help="comma-separated RR candidates to label & learn")
    ap.add_argument("--C", type=float, default=0.0,
                    help="inverse L2 strength; 0 = pick by time-series CV (recommended)")
    args = ap.parse_args()

    rr_grid = tuple(sorted(float(x) for x in args.rr_grid.split(",") if x.strip()))
    if not rr_grid:
        raise SystemExit("Empty --rr-grid")

    base = max(1, args.base_min)
    timeframes = timeframes_for_base(base)
    wait_bars = max(1, round(args.wait / base))
    horizon_bars = max(1, round(args.horizon / base))
    candles = resolve_candles(args.contract, args.days, base, stitch=args.stitch,
                              use_store=args.use_store)
    tick = get_tick_size(args.contract)
    split = int(len(candles) * args.train_frac)
    train = candles[:split]
    print(f"[base] {base}m candles | TFs={timeframes} | rr_grid={rr_grid} | "
          f"wait={wait_bars}bars horizon={horizon_bars}bars", flush=True)
    print(f"[split] train={len(train)} bars (front {args.train_frac:.0%}); "
          f"tail held out for validation", flush=True)

    cfg = ConfluenceConfig(band_ticks=args.band, min_distinct_tf=args.mdt, rr=rr_grid[0])
    cfg.direction_mode = "auto"
    cfg.tick_size = tick

    print("[zones] building train-split timeline...", flush=True)
    tl = build_zone_timeline(train, timeframes, tick, MAX_RECENCY_DEPTH)
    print("[collect] multi-RR forward-scan labeling...", flush=True)
    X, y, meta, starts, ends = collect(train, tl, cfg, rr_grid, args.stride,
                                       wait_bars, horizon_bars)
    if len(y) < 50:
        raise SystemExit(f"Too few labeled samples ({len(y)}). Lower --stride or --mdt.")
    print(f"[data] {len(y)} (candidate×RR) samples | win rate {y.mean():.1%}", flush=True)
    for rr in rr_grid:
        idx = [j for j, (_m, r) in enumerate(meta) if r == rr]
        if idx:
            wr = float(np.mean([y[j] for j in idx]))
            print(f"   RR={rr:<4} n={len(idx):>7} win={wr:.1%}", flush=True)

    weights, b_raw, info = evaluate_and_meta(
        X, y, starts, ends, n_bars=len(train),
        embargo=wait_bars + horizon_bars, C=(args.C or None))
    print(f"[fit] train AUC={info['auc']:.3f} acc={info['acc']:.3f} "
          f"brier={info['brier']:.3f} C={info['C']:.4g}", flush=True)
    print(f"[oos]  walk-forward AUC={info['oos_auc']:.3f} "
          f"brier={info['oos_brier']:.3f} folds={info['oos_folds']}", flush=True)
    sw = info["std_weights"]
    print("\n[weights] (normalized = log-odds per 1 SD, sorted by importance):", flush=True)
    for name in sorted(sw, key=lambda k: abs(sw[k]), reverse=True):
        print(f"   {name:22s} norm={sw[name]:+.4f}   raw={weights[name]:+.4g}", flush=True)
    print(f"   {'(bias)':22s}              raw={b_raw:+.4f}", flush=True)

    scorer = ConfluenceScorer(
        weights=weights, bias=b_raw,
        meta={
            "kind": "logistic_ev", "trained": True, "multi_rr": True,
            "rr_grid": list(rr_grid),
            "trained_at": datetime.now().isoformat(timespec="seconds"),
            "contract": args.contract, "days": args.days, "base_min": base,
            "timeframes": list(timeframes),
            "train_frac": args.train_frac, "n_samples": int(len(y)),
            "train_win_rate": float(y.mean()), "train_auc": info["auc"],
            "train_brier": info["brier"], "C": info["C"],
            "oos_auc": info["oos_auc"], "oos_brier": info["oos_brier"],
            "oos_folds": info["oos_folds"], "mean_uniqueness": info["mean_uniqueness"],
            "dropped_features": info["dropped_features"],
            "std_weights": info["std_weights"],
            "sklearn_hygiene": "drop-constant+ts-cv+uniqueness+walkforward",
            "cfg": {"band_ticks": args.band, "min_distinct_tf": args.mdt,
                    "rr_grid": list(rr_grid), "wait_min": args.wait,
                    "horizon_min": args.horizon},
        },
    )
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    out = MODEL_DIR / "confluence_scorer_ev.json"
    scorer.save(out)
    print(f"\n[out] {out}", flush=True)
    print("[note] point the live/backtest scorer at this file (or rename to "
          "confluence_scorer.json) AND enable variable-RR (conf_rr_grid / "
          "EV-opt mode) to use it.", flush=True)


if __name__ == "__main__":
    main()
