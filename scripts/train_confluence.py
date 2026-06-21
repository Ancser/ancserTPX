# ============================================================
# 文件: scripts/train_confluence.py
# 狀態: v1.0.6 (explainable confluence — scorer trainer)
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
import math
import pickle
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.db.models import get_tick_size
from backend.strategy.confluence import (
    ConfluenceConfig, MAX_RECENCY_DEPTH, evaluate_confluence_scored,
)
from backend.strategy.confluence_features import (
    FEATURE_NAMES, DEAD_FEATURES, features_to_vector, CONTEXT_WINDOW,
)
from backend.strategy.confluence_scorer import ConfluenceScorer, save_model_version
from backend.backtest.confluence_backtest import build_zone_timeline
from scripts.confluence_common import (
    CONTRACT_ID, MODEL_DIR, resolve_candles, timeframes_for_base,
)
from scripts.confluence_label import (
    simulate_outcomes, uniqueness_weights, walk_forward_oos,
)

STD_TOL = 1e-8       # below this a feature is treated as constant (no signal)
RANDOM_STATE = 42    # pinned for reproducibility


def collect(candles, timeline, cfg, stride, wait, horizon):
    """Forward-scan labels for fixed-RR training. Returns
    (X, y, modes, starts, ends): starts/ends are the bar indices bounding each
    sample's outcome window, used for uniqueness weighting + embargoed OOS."""
    heuristic = ConfluenceScorer.heuristic()
    rr_grid = (cfg.rr,)
    X, y, modes, starts, ends = [], [], [], [], []
    n = len(candles)
    edge = wait + horizon + 2
    for i in range(0, n - edge, stride):
        snap = timeline[i]
        if len(snap) < cfg.min_distinct_tf:
            continue
        recent = candles[max(0, i - CONTEXT_WINDOW + 1):i + 1]
        sigs = evaluate_confluence_scored(snap, candles[i].close, cfg, heuristic,
                                          recent_candles=recent)
        for sig in sigs:
            risk = abs(sig.entry_price - sig.sl_price)
            if risk <= 0:
                continue
            res = simulate_outcomes(candles, i, sig.direction, sig.entry_price,
                                    sig.sl_price, risk, rr_grid, wait, horizon)
            got = res.get(cfg.rr)
            if got is None:
                continue
            label, end_idx = got
            X.append(features_to_vector(sig.features))
            y.append(label)
            modes.append(sig.direction_mode)
            starts.append(i)
            ends.append(end_idx)
    return (np.array(X, dtype=float), np.array(y, dtype=int), modes,
            np.array(starts, dtype=int), np.array(ends, dtype=int))


def _fit_logistic(Xz, y, C, sample_weight):
    """L2 logistic fit on standardised features. C=None → pick C by
    time-series CV on Brier (proper calibration), pinned random_state."""
    from sklearn.linear_model import LogisticRegression, LogisticRegressionCV
    from sklearn.model_selection import TimeSeriesSplit

    distinct = len(set(np.asarray(y).tolist())) > 1
    if C is None and distinct and len(y) >= 5 * 40:
        clf = LogisticRegressionCV(
            Cs=np.logspace(-3, 2, 10), cv=TimeSeriesSplit(n_splits=5),
            scoring="neg_brier_score", class_weight="balanced",
            max_iter=5000, random_state=RANDOM_STATE,
        )
    else:
        clf = LogisticRegression(C=(C or 1.0), class_weight="balanced",
                                 max_iter=5000, random_state=RANDOM_STATE)
    clf.fit(Xz, y, sample_weight=sample_weight)
    return clf


def fit_scorer(X, y, C=None, sample_weight=None):
    """Standardize → drop constant features → L2 logistic fit → fold
    standardization back to RAW-space weights. Returns (weights, bias, info).

    Constant (≈zero-variance) features are excluded BEFORE fitting and forced to
    weight 0: folding them would divide by ~0 and explode the weight (this is the
    bug that gave the fixed-RR `rr` feature a weight of 141). `info` carries
    train auc/acc/brier, the chosen C and which features were dropped.

    Single source of truth shared by the CLI trainers and the web
    /confluence/train endpoint, so all paths produce an identical scorer."""
    from sklearn.metrics import roc_auc_score, accuracy_score, brier_score_loss

    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=int)
    mean, std = X.mean(axis=0), X.std(axis=0)
    dead_mask = np.array([name in DEAD_FEATURES for name in FEATURE_NAMES])
    usable = (std > STD_TOL) & ~dead_mask
    Xz = (X[:, usable] - mean[usable]) / std[usable]

    clf = _fit_logistic(Xz, y, C, sample_weight)
    p = clf.predict_proba(Xz)[:, 1]
    distinct = len(set(y.tolist())) > 1
    auc = float(roc_auc_score(y, p)) if distinct else float("nan")
    acc = float(accuracy_score(y, (p >= 0.5).astype(int)))
    brier = float(brier_score_loss(y, p)) if distinct else float("nan")

    coef_full = np.zeros(X.shape[1])
    coef_full[usable] = clf.coef_[0]
    w_raw = np.where(usable, coef_full / np.where(usable, std, 1.0), 0.0)
    b_raw = float(clf.intercept_[0] - np.sum(clf.coef_[0] * mean[usable] / std[usable]))
    weights = {name: float(w_raw[i]) for i, name in enumerate(FEATURE_NAMES)}
    # STANDARDIZED weights = raw_weight * feature_std = the model's coef on the
    # z-scored feature. Each is the log-odds shift per 1 SD of that feature, so
    # magnitudes ARE comparable across features (raw weights are NOT, because each
    # feature has a different scale). For human reading only — inference still
    # uses raw `weights`, so live==backtest parity is untouched.
    std_weights = {name: float(coef_full[i]) for i, name in enumerate(FEATURE_NAMES)}
    info = {
        "auc": auc, "acc": acc, "brier": brier,
        "C": float(getattr(clf, "C_", [C or 1.0])[0]),
        "n_features_used": int(usable.sum()),
        "dropped_features": [FEATURE_NAMES[i] for i in range(len(FEATURE_NAMES))
                             if not usable[i] or dead_mask[i]],
        "std_weights": std_weights,
    }
    return weights, b_raw, info


def make_oos_fit(C):
    """Predictor factory for walk_forward_oos: standardise + drop constants on
    the TRAIN fold only (no leakage), fit, return a proba(Xte) closure."""
    def fit_fn(Xtr, ytr):
        Xtr = np.asarray(Xtr, dtype=float)
        mean, std = Xtr.mean(axis=0), Xtr.std(axis=0)
        dead_mask = np.array([name in DEAD_FEATURES for name in FEATURE_NAMES])
        usable = (std > STD_TOL) & ~dead_mask
        clf = _fit_logistic((Xtr[:, usable] - mean[usable]) / std[usable],
                            np.asarray(ytr, dtype=int), C, None)

        def proba(Xte):
            Xte = np.asarray(Xte, dtype=float)
            return clf.predict_proba((Xte[:, usable] - mean[usable]) / std[usable])[:, 1]
        return proba
    return fit_fn


def evaluate_and_meta(X, y, starts, ends, n_bars, embargo, C=None, loss_weight=1.0):
    """Fit the production scorer with uniqueness weighting AND compute an honest
    embargoed walk-forward OOS estimate. Returns (weights, bias, info) where
    info also holds oos_auc / oos_brier / oos_folds. Shared by CLI + web so the
    shipped model ALWAYS carries an out-of-sample number, even at train_frac=1.

    COST-SENSITIVE (loss_weight): in fixed-RR training every win pays +rr·risk and
    every loss costs −1·risk, so the economic asymmetry is fully captured by the
    LABEL. ``loss_weight`` ≥ 1 multiplies the fit weight of every LOSS sample (on
    top of the base ``class_weight='balanced'`` and uniqueness weights), making the
    model work harder to push losers' probability down. The downstream effect is
    fewer losers admitted at any probability/EV threshold → higher PF, lower maxDD
    and smaller total loss, at the cost of fewer trades (lower gross $). loss_weight
    == 1.0 reproduces the previous behaviour EXACTLY. This only changes the TRAINED
    weights — inference is still a plain raw-space dot product, so live==backtest
    parity is untouched."""
    w = np.asarray(uniqueness_weights(starts, ends, n_bars), dtype=float)
    if loss_weight and loss_weight != 1.0:
        ya = np.asarray(y, dtype=int)
        w = w * np.where(ya == 0, float(loss_weight), 1.0)
    weights, bias, info = fit_scorer(X, y, C=C, sample_weight=w)
    oos = walk_forward_oos(X, y, starts, ends, make_oos_fit(info["C"]),
                           n_splits=5, embargo=embargo)
    info.update(oos)
    info["mean_uniqueness"] = float(np.mean(w)) if len(w) else float("nan")
    info["loss_weight"] = float(loss_weight)
    return weights, bias, info


def sweep_probability_threshold(
    candles, timeline, scorer, signal_cfg, contract_id="CON.F.US.MNQ.M26",
    contract_size=3, wait_minutes=1,
    probs=None, trail_trigger_pct=0.0, trail_lock_pct=0.0,
    max_dd_target=2000.0,
):
    """Sweep min_prob thresholds and return per-threshold backtest metrics.

    Reuses the precomputed ``timeline`` so only the signal gate changes per
    run — each replay is cheap (no detector re-feeding). Returns a list of
    dicts sorted by min_prob, plus the index of the recommended threshold
    (highest PnL where maxDD < max_dd_target, or lowest maxDD if none qualifies).
    """
    from backend.backtest.confluence_backtest import (
        ConfluenceBacktester, ConfluenceBacktestConfig,
    )
    from backend.db.models import get_point_value

    if probs is None:
        probs = [i / 20.0 for i in range(13)]  # 0.00, 0.05, … 0.60
    point_val = get_point_value(contract_id)
    rows = []
    for prob in probs:
        min_score = math.log(prob / (1.0 - prob)) if 0 < prob < 1.0 else 0.0
        run_cfg = ConfluenceBacktestConfig(
            wait_minutes=wait_minutes, min_score=min_score,
            trail_trigger_pct=trail_trigger_pct, trail_lock_pct=trail_lock_pct,
        )
        bt = ConfluenceBacktester(
            signal_cfg, run_cfg, contract_id, contract_size, scorer=scorer,
        )
        result = bt.run(candles, zones_timeline=timeline)
        m = result.metrics
        rows.append({
            "min_prob": prob, "min_score": round(min_score, 4),
            "trades": m.total_trades, "wins": m.wins,
            "win_rate": round(m.win_rate, 4),
            "pnl": round(m.total_pnl, 2),
            "max_dd": round(m.max_drawdown, 2),
            "pf": round(m.profit_factor, 2),
            "max_consec_loss": m.max_consecutive_losses,
            "avg_loss": round(m.avg_loss, 2),
        })
    # pick best: highest PnL among rows with maxDD < target; fallback to lowest maxDD
    qualified = [r for r in rows if r["max_dd"] < max_dd_target and r["pnl"] > 0]
    if qualified:
        best_idx = rows.index(max(qualified, key=lambda r: r["pnl"]))
    else:
        best_idx = rows.index(min(rows, key=lambda r: r["max_dd"]))
    return rows, best_idx


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
    ap.add_argument("--wait", type=int, default=1, help="limit-fill timeout in MINUTES")
    ap.add_argument("--horizon", type=int, default=1440, help="SL/TP resolve window in MINUTES")
    ap.add_argument("--band", type=float, default=4.0)
    ap.add_argument("--mdt", type=int, default=2)
    ap.add_argument("--rr", type=float, default=3.0)
    ap.add_argument("--C", type=float, default=0.0,
                    help="inverse L2 strength; 0 = pick by time-series CV (recommended)")
    ap.add_argument("--loss-weight", type=float, default=1.0,
                    help="cost-sensitive loss aversion: >1 up-weights LOSS samples "
                         "→ higher PF / lower maxDD / fewer trades. 1.0 = baseline.")
    ap.add_argument("--trainer", choices=("codex", "claude"), default="codex",
                    help="assistant responsible for this training run")
    ap.add_argument("--description", default="",
                    help="one-line model description used in the version name")
    ap.add_argument("--enable-breakout", action="store_true",
                    help="include breakout-retrace candidates (default: off)")
    ap.add_argument("--sweep", action="store_true",
                    help="after training, sweep min_prob thresholds and print "
                         "maxDD/PnL table (3 MNQ, target maxDD < $2k)")
    ap.add_argument("--sweep-contracts", type=int, default=3,
                    help="contract size for the threshold sweep (default: 3)")
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
    cfg.enable_breakout = bool(args.enable_breakout)

    print("[zones] building train-split timeline...", flush=True)
    tl = build_zone_timeline(train, timeframes, tick, MAX_RECENCY_DEPTH)
    print("[collect] forward-scan labeling...", flush=True)
    X, y, modes, starts, ends = collect(train, tl, cfg, args.stride, wait_bars, horizon_bars)
    if len(y) < 50:
        raise SystemExit(f"Too few labeled samples ({len(y)}). Lower --stride or --mdt.")
    print(f"[data] {len(y)} samples | win rate {y.mean():.1%} | "
          f"reversion {sum(1 for m in modes if m=='reversion')}/{len(modes)}", flush=True)

    weights, b_raw, info = evaluate_and_meta(
        X, y, starts, ends, n_bars=len(train),
        embargo=wait_bars + horizon_bars, C=(args.C or None),
        loss_weight=args.loss_weight)
    print(f"[fit] train AUC={info['auc']:.3f} acc={info['acc']:.3f} "
          f"brier={info['brier']:.3f} C={info['C']:.4g} "
          f"loss_weight={args.loss_weight:g}", flush=True)
    print(f"[oos]  walk-forward AUC={info['oos_auc']:.3f} "
          f"brier={info['oos_brier']:.3f} folds={info['oos_folds']}", flush=True)
    if info["dropped_features"]:
        print(f"[drop] constant features (weight=0): {info['dropped_features']}", flush=True)

    sw = info["std_weights"]
    print("\n[weights] (normalized = log-odds per 1 SD, sorted by importance):", flush=True)
    for name in sorted(sw, key=lambda k: abs(sw[k]), reverse=True):
        print(f"   {name:22s} norm={sw[name]:+.4f}   raw={weights[name]:+.4g}", flush=True)
    print(f"   {'(bias)':22s}              raw={b_raw:+.4f}", flush=True)

    scorer = ConfluenceScorer(
        weights=weights, bias=b_raw,
        meta={
            "kind": "logistic", "trained": True,
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
            "loss_weight": args.loss_weight,
            "cfg": {"band_ticks": args.band, "min_distinct_tf": args.mdt,
                    "rr": args.rr, "wait_min": args.wait, "horizon_min": args.horizon,
                    "loss_weight": args.loss_weight,
                    "enable_breakout": bool(args.enable_breakout)},
        },
    )
    description = args.description.strip() or (
        f"RR{args.rr:g} Band{args.band:g} MinTF{args.mdt} command-line training"
    )
    model_id, out = save_model_version(
        scorer, args.trainer, description, activate=True,
        active_path=MODEL_DIR / "confluence_scorer.json",
    )
    print(f"\n[model] {model_id}", flush=True)
    print(f"[version] {out}", flush=True)
    print(f"[active] {MODEL_DIR / 'confluence_scorer.json'}", flush=True)

    if args.sweep:
        print(f"\n[sweep] running probability threshold sweep "
              f"({args.sweep_contracts} MNQ) ...", flush=True)
        sweep_tl = build_zone_timeline(candles, timeframes, tick, MAX_RECENCY_DEPTH)
        rows, best_idx = sweep_probability_threshold(
            candles, sweep_tl, scorer, cfg, args.contract,
            contract_size=args.sweep_contracts,
            wait_minutes=args.wait,
        )
        print(f"\n{'prob':>6s} {'score':>7s} {'trades':>6s} {'WR':>6s} "
              f"{'PnL':>10s} {'maxDD':>8s} {'PF':>6s} {'consec':>6s}", flush=True)
        print("-" * 62, flush=True)
        for i, r in enumerate(rows):
            mark = " <<" if i == best_idx else ""
            print(f"{r['min_prob']:6.2f} {r['min_score']:7.3f} {r['trades']:6d} "
                  f"{r['win_rate']:6.1%} {r['pnl']:10.2f} {r['max_dd']:8.2f} "
                  f"{r['pf']:6.2f} {r['max_consec_loss']:6d}{mark}", flush=True)
        best = rows[best_idx]
        if best["max_dd"] < 2000 and best["pnl"] > 0:
            print(f"\n[sweep] RECOMMENDED: min_prob={best['min_prob']:.2f} → "
                  f"maxDD=${best['max_dd']:.0f}, PnL=${best['pnl']:.0f}, "
                  f"{best['trades']} trades", flush=True)
        else:
            print(f"\n[sweep] no threshold meets maxDD<$2k + positive PnL. "
                  f"Lowest maxDD: ${best['max_dd']:.0f} at prob={best['min_prob']:.2f}",
                  flush=True)


if __name__ == "__main__":
    main()
