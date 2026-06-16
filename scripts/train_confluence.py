# ============================================================
# 文件: scripts/train_confluence.py
# 狀態: v0.19.0 (explainable confluence — scorer trainer)
# 關聯文件:
#   ← backend/backtest/confluence_backtest.py (build_zone_timeline)
#   ← backend/strategy/confluence.py          (evaluate_confluence_scored)
#   ← backend/strategy/confluence_features.py (FEATURE_NAMES)
#   → backend/strategy/confluence_scorer.py   (saves raw-space weights JSON)
# 執行:
#   python -m scripts.train_confluence --days 60 --train-frac 0.67
# ============================================================
"""Train the confluence scorer (interpretable logistic regression).

Labeling — FORWARD SCAN (counterfactual, removes 'one trade at a time' bias):
  at every Nth bar we enumerate ALL candidate signals (both modes) and
  INDEPENDENTLY simulate each one's one-shot outcome:
    1. give `wait` bars for a limit fill (price must touch entry),
    2. then scan forward up to `horizon` bars for SL/TP,
    3. label win=1 (TP) / 0 (SL); drop unfilled / unresolved candidates.
  This yields thousands of labeled (features -> win) rows.

Model — sklearn LogisticRegression (L2). Features are standardised for stable
fitting, then the standardisation is FOLDED BACK into raw-space weights so the
saved scorer is a plain dot product on the original interpretable features
(no scaler needed at inference; live == backtest).

Only the TRAIN split is used to fit; the held-out tail is left for
scripts/validate_confluence.py (the out-of-sample step).
"""

from __future__ import annotations

import argparse
import glob
import pickle
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.db.models import get_tick_size, Direction
from backend.strategy.confluence import (
    ConfluenceConfig, MAX_RECENCY_DEPTH, evaluate_confluence_scored,
)
from backend.strategy.confluence_features import FEATURE_NAMES, features_to_vector
from backend.strategy.confluence_scorer import ConfluenceScorer
from backend.backtest.confluence_backtest import build_zone_timeline
from scripts.confluence_common import (
    CONTRACT_ID, MODEL_DIR, resolve_candles, timeframes_for_base,
)


def _simulate(candles, i, sig, wait, horizon):
    """Independent one-shot outcome for a candidate built at close of bar i.
    Returns 1 (TP), 0 (SL), or None (unfilled / unresolved)."""
    n = len(candles)
    entry, sl, tp = sig.entry_price, sig.sl_price, sig.tp_price
    buy = sig.direction == Direction.BUY
    # 1) fill within wait bars (start next bar)
    k = None
    for j in range(i + 1, min(i + 1 + wait, n)):
        c = candles[j]
        if (c.low <= entry) if buy else (c.high >= entry):
            k = j
            break
    if k is None:
        return None
    # entry bar: SL-only can trigger
    ck = candles[k]
    if (ck.low <= sl) if buy else (ck.high >= sl):
        return 0
    # 2) scan for SL/TP
    for m in range(k + 1, min(k + 1 + horizon, n)):
        c = candles[m]
        if buy:
            hit_sl, hit_tp = c.low <= sl, c.high >= tp
        else:
            hit_sl, hit_tp = c.high >= sl, c.low <= tp
        if hit_sl and hit_tp:
            # ambiguous bar: nearer-to-open resolves first
            return 1 if abs(c.open - tp) <= abs(c.open - sl) else 0
        if hit_tp:
            return 1
        if hit_sl:
            return 0
    return None


def collect(candles, timeline, cfg, stride, wait, horizon):
    heuristic = ConfluenceScorer.heuristic()
    X, y, meta = [], [], []
    n = len(candles)
    edge = wait + horizon + 2
    for i in range(0, n - edge, stride):
        snap = timeline[i]
        if len(snap) < cfg.min_distinct_tf:
            continue
        sigs = evaluate_confluence_scored(snap, candles[i].close, cfg, heuristic)
        for sig in sigs:
            label = _simulate(candles, i, sig, wait, horizon)
            if label is None:
                continue
            X.append(features_to_vector(sig.features))
            y.append(label)
            meta.append(sig.direction_mode)
    return np.array(X, dtype=float), np.array(y, dtype=int), meta


def fold_standardization(coef, intercept, mean, std):
    """z=(x-mean)/std ; logit=intercept+coef·z  ->  raw weights on x."""
    std = np.where(std == 0, 1.0, std)
    w_raw = coef / std
    b_raw = float(intercept - np.sum(coef * mean / std))
    return w_raw, b_raw


def fit_scorer(X, y, C: float = 1.0):
    """Standardize → L2 logistic fit → fold standardization back to raw-space
    weights. Returns (weights_dict, bias, auc, acc).

    Single source of truth shared by the CLI trainer (main) and the web
    /confluence/train endpoint, so both produce an identical, reproducible
    scorer from the same (X, y)."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score, accuracy_score

    mean, std = X.mean(axis=0), X.std(axis=0)
    Xz = (X - mean) / np.where(std == 0, 1.0, std)
    clf = LogisticRegression(C=C, class_weight="balanced", max_iter=2000)
    clf.fit(Xz, y)
    p = clf.predict_proba(Xz)[:, 1]
    auc = float(roc_auc_score(y, p)) if len(set(y)) > 1 else float("nan")
    acc = float(accuracy_score(y, (p >= 0.5).astype(int)))
    w_raw, b_raw = fold_standardization(clf.coef_[0], clf.intercept_[0], mean, std)
    weights = {name: float(w_raw[i]) for i, name in enumerate(FEATURE_NAMES)}
    return weights, float(b_raw), auc, acc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--contract", default=CONTRACT_ID)
    ap.add_argument("--base-min", type=int, default=1, help="minutes per input candle (1 or 5)")
    ap.add_argument("--stitch", type=int, default=1,
                    help="splice N quarterly contracts (non-overlap) for >1yr history")
    ap.add_argument("--use-store", action="store_true",
                    help="train on the persistent accumulated store (option C)")
    ap.add_argument("--train-frac", type=float, default=0.80, help="front fraction used to TRAIN")
    ap.add_argument("--stride", type=int, default=5, help="sample a candidate every N bars")
    ap.add_argument("--wait", type=int, default=60, help="limit-fill timeout in MINUTES")
    ap.add_argument("--horizon", type=int, default=1440, help="SL/TP resolve window in MINUTES")
    ap.add_argument("--band", type=float, default=8.0)
    ap.add_argument("--mdt", type=int, default=3)
    ap.add_argument("--rr", type=float, default=1.5)
    ap.add_argument("--C", type=float, default=1.0, help="inverse L2 strength")
    args = ap.parse_args()

    base = max(1, args.base_min)
    timeframes = timeframes_for_base(base)
    wait_bars = max(1, round(args.wait / base))
    horizon_bars = max(1, round(args.horizon / base))
    candles = resolve_candles(args.contract, args.days, base, stitch=args.stitch,
                              use_store=args.use_store)
    tick = get_tick_size(args.contract)
    split = int(len(candles) * args.train_frac)
    train = candles[:split]
    print(f"[base] {base}m candles | TFs={timeframes} | "
          f"wait={wait_bars}bars horizon={horizon_bars}bars", flush=True)
    print(f"[split] train={len(train)} bars (front {args.train_frac:.0%}); "
          f"tail held out for validation", flush=True)

    cfg = ConfluenceConfig(band_ticks=args.band, min_distinct_tf=args.mdt, rr=args.rr)
    cfg.direction_mode = "auto"
    cfg.tick_size = tick

    print("[zones] building train-split timeline...", flush=True)
    tl = build_zone_timeline(train, timeframes, tick, MAX_RECENCY_DEPTH)
    print("[collect] forward-scan labeling...", flush=True)
    X, y, modes = collect(train, tl, cfg, args.stride, wait_bars, horizon_bars)
    if len(y) < 50:
        raise SystemExit(f"Too few labeled samples ({len(y)}). Lower --stride or --mdt.")
    print(f"[data] {len(y)} samples | win rate {y.mean():.1%} | "
          f"reversion {sum(1 for m in modes if m=='reversion')}/{len(modes)}", flush=True)

    weights, b_raw, auc, acc = fit_scorer(X, y, C=args.C)
    print(f"[fit] train AUC={auc:.3f} acc={acc:.3f}", flush=True)

    print("\n[weights] (raw-space, sorted by |coef|):", flush=True)
    for name in sorted(weights, key=lambda k: abs(weights[k]), reverse=True):
        print(f"   {name:22s} {weights[name]:+.4f}", flush=True)
    print(f"   {'(bias)':22s} {b_raw:+.4f}", flush=True)

    scorer = ConfluenceScorer(
        weights=weights, bias=b_raw,
        meta={
            "kind": "logistic", "trained": True,
            "trained_at": datetime.now().isoformat(timespec="seconds"),
            "contract": args.contract, "days": args.days, "base_min": base,
            "timeframes": list(timeframes),
            "train_frac": args.train_frac, "n_samples": int(len(y)),
            "train_win_rate": float(y.mean()), "train_auc": float(auc),
            "cfg": {"band_ticks": args.band, "min_distinct_tf": args.mdt,
                    "rr": args.rr, "wait_min": args.wait, "horizon_min": args.horizon},
        },
    )
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    out = MODEL_DIR / "confluence_scorer.json"
    scorer.save(out)
    print(f"\n[out] {out}", flush=True)


if __name__ == "__main__":
    main()
