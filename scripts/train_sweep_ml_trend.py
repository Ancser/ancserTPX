"""ML Trend: train scorer + sweep Value Area mean reversion parameters.

Phase 1 — Pre-compute rolling VP timelines (one per lookback value).
Phase 2 — Mechanical sweep (no ML, all rule-based signals taken).
Phase 3 — Train logistic regression on the best mechanical config.
Phase 4 — ML-gated sweep (min_prob threshold).
Phase 5 — Report.
"""

import json
import math
import pickle
import sys
import time as _time
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.db.models import get_tick_size, BacktestConfig, Direction
from backend.strategy.volume_profile import VolumeProfileCalculator
from backend.strategy.ml_trend import (
    MLTrendConfig, extract_features, features_to_vector,
    ML_TREND_FEATURE_NAMES, ML_TREND_DEAD_FEATURES,
)
from backend.backtest.ml_trend_backtest import (
    MLTrendBacktester, MLTrendBacktestConfig, precompute_vp_timeline,
)

TICK = get_tick_size("CON.F.US.MNQ.M26")
CONTRACT_ID = "CON.F.US.MNQ.M26"


# ═══════════════════════════════════════════════════════════
# Data loading
# ═══════════════════════════════════════════════════════════

def load_candles():
    store = ROOT / "data" / "store" / "MNQ_accumulated_1m.pkl"
    if not store.exists():
        sys.exit(f"Store not found: {store}")
    candles = sorted(pickle.loads(store.read_bytes()), key=lambda c: c.timestamp)
    print(f"Loaded {len(candles)} candles from store.")
    return candles


# ═══════════════════════════════════════════════════════════
# Phase 1: Pre-compute VP timelines
# ═══════════════════════════════════════════════════════════

def build_vp_timelines(candles, lookbacks):
    """Pre-compute rolling VP for each lookback value. Returns {lookback: timeline}."""
    timelines = {}
    for lb in lookbacks:
        t0 = _time.perf_counter()
        tl = precompute_vp_timeline(candles, lb, TICK, recalc_interval=5)
        el = _time.perf_counter() - t0
        valid = sum(1 for v in tl if v is not None)
        print(f"  lookback={lb:>4}: {valid}/{len(tl)} valid VP snapshots ({el:.1f}s)")
        timelines[lb] = tl
    return timelines


# ═══════════════════════════════════════════════════════════
# Phase 2: Mechanical sweep (no ML)
# ═══════════════════════════════════════════════════════════

def run_one_config(candles, vp_timeline, lookback, band, sl_buf, sessions,
                   trail_on, sz, tp_mode="poc", rr=2.0, scorer=None, min_prob=0.0):
    """Run a single backtest config and return metrics dict (or None)."""
    sig_cfg = MLTrendConfig(
        lookback=lookback, band_ticks=band, sl_buffer_ticks=sl_buf,
        tick_size=TICK, tp_mode=tp_mode, rr=rr,
    )
    min_score = math.log(min_prob / (1.0 - min_prob)) if 0 < min_prob < 1 else 0.0
    run_cfg = MLTrendBacktestConfig(
        trail_trigger_pct=0.50 if trail_on else 0.0,
        trail_lock_pct=0.05 if trail_on else 0.0,
        one_trade_per_session=True,
        allowed_sessions=sessions,
        min_score=min_score,
    )
    bt_cfg = BacktestConfig(initial_capital=50000.0, symbol="MNQ",
                            commission_rt=1.0, fees_rt=2.8)
    bt = MLTrendBacktester(
        signal_cfg=sig_cfg, run_cfg=run_cfg,
        contract_id=CONTRACT_ID, contract_size=sz,
        bt_config=bt_cfg, scorer=scorer,
    )
    result = bt.run(candles, vp_timeline=vp_timeline)
    m = result.metrics
    if m.total_trades < 3:
        return None
    return {
        "lookback": lookback, "band": band, "sl_buf": sl_buf,
        "sessions": "+".join(sessions), "trail": trail_on, "size": sz,
        "tp_mode": tp_mode, "rr": rr, "min_prob": min_prob,
        "trades": m.total_trades, "wins": m.wins,
        "wr": m.win_rate, "pnl": m.total_pnl,
        "pf": m.profit_factor, "dd": m.max_drawdown,
        "calmar": m.calmar_ratio,
        "avg_win": m.avg_win, "avg_loss": m.avg_loss,
    }


def mechanical_sweep(candles, timelines):
    """Sweep without ML model — pure rule-based signals."""
    configs = []
    for lb in timelines:
        for band in [2, 4, 6, 8]:
            for sl_buf in [2, 4, 8]:
                for sessions in [("ASIA",), ("ASIA", "EURO"), ("ASIA", "EURO", "PRE", "RTH")]:
                    for trail in [True, False]:
                        for sz in [1, 3]:
                            configs.append((lb, band, sl_buf, sessions, trail, sz))
    print(f"\nPhase 2: Mechanical sweep — {len(configs)} configs")
    results = []
    t0 = _time.perf_counter()
    for idx, (lb, band, sl_buf, sess, trail, sz) in enumerate(configs):
        r = run_one_config(candles, timelines[lb], lb, band, sl_buf, sess, trail, sz)
        if r:
            results.append(r)
        if (idx + 1) % 50 == 0:
            sys.stdout.write(f"\r  {idx+1}/{len(configs)} ({len(results)} valid)...")
            sys.stdout.flush()
    el = _time.perf_counter() - t0
    print(f"\r  Done: {len(configs)} configs, {len(results)} valid results ({el:.0f}s)")
    return results


# ═══════════════════════════════════════════════════════════
# Phase 3: Train logistic regression
# ═══════════════════════════════════════════════════════════

def generate_labeled_data(candles, vp_timeline, cfg: MLTrendConfig, stride=5, horizon=1440):
    """Forward-scan labeling: at each stride-th bar, check if a VA boundary
    signal would result in a TP (POC) or SL hit."""
    tick = cfg.tick_size
    band = cfg.band_ticks * tick
    buf = cfg.sl_buffer_ticks * tick
    X, y, starts, ends = [], [], [], []
    n = len(candles)

    for i in range(cfg.lookback, n - horizon - 2, stride):
        vp = vp_timeline[i]
        if vp is None:
            continue
        val, vah, poc = vp["val"], vp["vah"], vp["poc"]
        low_100, high_100 = vp["low_100"], vp["high_100"]
        va_width = vah - val
        if va_width < tick * 4:
            continue

        price = candles[i].close

        # check LONG candidate
        if price <= val + band:
            entry = float(candles[i + 1].open)
            sl = low_100 - buf
            tp = poc
            direction = Direction.BUY
        # check SHORT candidate
        elif price >= vah - band:
            entry = float(candles[i + 1].open)
            sl = high_100 + buf
            tp = poc
            direction = Direction.SELL
        else:
            continue

        risk = abs(entry - sl)
        reward = abs(tp - entry)
        if risk < tick * cfg.min_risk_ticks or risk > tick * cfg.max_risk_ticks:
            continue
        if reward < tick * 2:
            continue

        # forward-scan for SL/TP hit
        label, end_idx = None, None
        for j in range(i + 1, min(n, i + 1 + horizon)):
            c = candles[j]
            if direction == Direction.BUY:
                if c.low <= sl:
                    label, end_idx = 0, j
                    break
                if c.high >= tp:
                    label, end_idx = 1, j
                    break
            else:
                if c.high >= sl:
                    label, end_idx = 0, j
                    break
                if c.low <= tp:
                    label, end_idx = 1, j
                    break

        if label is None:
            continue

        # extract features
        recent = candles[max(0, i - 45):i + 1]
        feats = extract_features(
            candles[i], val, vah, poc, recent,
            direction, entry, sl, tp, tick,
        )
        X.append(features_to_vector(feats))
        y.append(label)
        starts.append(i)
        ends.append(end_idx)

    return np.array(X, dtype=float), np.array(y, dtype=int), np.array(starts), np.array(ends)


def fit_scorer(X, y, C=None):
    """Standardise, drop constants, fit L2 logistic, fold back to raw-space."""
    from sklearn.linear_model import LogisticRegression, LogisticRegressionCV
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.metrics import roc_auc_score, accuracy_score, brier_score_loss

    mean, std = X.mean(axis=0), X.std(axis=0)
    dead_mask = np.array([name in ML_TREND_DEAD_FEATURES for name in ML_TREND_FEATURE_NAMES])
    usable = (std > 1e-8) & ~dead_mask
    Xz = (X[:, usable] - mean[usable]) / std[usable]

    distinct = len(set(y.tolist())) > 1
    if C is None and distinct and len(y) >= 200:
        clf = LogisticRegressionCV(
            Cs=np.logspace(-3, 2, 10), cv=TimeSeriesSplit(n_splits=5),
            scoring="neg_brier_score", class_weight="balanced",
            max_iter=5000, random_state=42,
        )
    else:
        clf = LogisticRegression(C=(C or 1.0), class_weight="balanced",
                                 max_iter=5000, random_state=42)
    clf.fit(Xz, y)
    p = clf.predict_proba(Xz)[:, 1]
    auc = float(roc_auc_score(y, p)) if distinct else float("nan")
    acc = float(accuracy_score(y, (p >= 0.5).astype(int)))
    brier = float(brier_score_loss(y, p)) if distinct else float("nan")

    coef_full = np.zeros(X.shape[1])
    coef_full[usable] = clf.coef_[0]
    w_raw = np.where(usable, coef_full / np.where(usable, std, 1.0), 0.0)
    b_raw = float(clf.intercept_[0] - np.sum(clf.coef_[0] * mean[usable] / std[usable]))
    weights = {name: float(w_raw[i]) for i, name in enumerate(ML_TREND_FEATURE_NAMES)}
    std_weights = {name: float(coef_full[i]) for i, name in enumerate(ML_TREND_FEATURE_NAMES)}
    chosen_C = float(getattr(clf, "C_", [C or 1.0])[0])
    dropped = [ML_TREND_FEATURE_NAMES[i] for i in range(len(ML_TREND_FEATURE_NAMES))
               if not usable[i]]
    return weights, b_raw, {
        "auc": auc, "acc": acc, "brier": brier, "C": chosen_C,
        "dropped": dropped, "std_weights": std_weights,
    }


class MLTrendScorer:
    """Simple linear scorer (same pattern as ConfluenceScorer)."""

    def __init__(self, weights, bias):
        self.weights = weights
        self.bias = bias

    def logit(self, feats):
        s = self.bias
        for name in ML_TREND_FEATURE_NAMES:
            s += self.weights.get(name, 0.0) * feats.get(name, 0.0)
        return s

    def score(self, feats):
        return self.logit(feats)

    def probability(self, feats):
        z = max(-60.0, min(60.0, self.logit(feats)))
        return 1.0 / (1.0 + math.exp(-z))


# ═══════════════════════════════════════════════════════════
# Phase 4: ML-gated sweep
# ═══════════════════════════════════════════════════════════

def ml_sweep(candles, timelines, scorer, best_mech):
    """Sweep min_prob thresholds using the trained scorer on the best
    mechanical config's lookback/band/sl_buf/sessions."""
    lb = best_mech["lookback"]
    band = best_mech["band"]
    sl_buf = best_mech["sl_buf"]
    trail = best_mech["trail"]
    sz = best_mech["size"]

    results = []
    for sessions in [("ASIA",), ("ASIA", "EURO"), ("ASIA", "EURO", "PRE", "RTH")]:
        for prob in [0.0, 0.10, 0.20, 0.30, 0.40, 0.50, 0.55, 0.60]:
            r = run_one_config(
                candles, timelines[lb], lb, band, sl_buf, sessions,
                trail, sz, scorer=scorer, min_prob=prob,
            )
            if r:
                results.append(r)
    return results


# ═══════════════════════════════════════════════════════════
# Reporting
# ═══════════════════════════════════════════════════════════

def print_table(title, results, limit=30):
    print(f"\n{title}")
    hdr = (f"{'LB':>4} {'Band':>4} {'SLB':>3} {'Sessions':<16} {'Tr':>2} {'Sz':>2} "
           f"{'P':>4} {'Trd':>4} {'Win%':>6} {'PnL':>10} {'PF':>6} {'MaxDD':>8} {'Calmar':>7}")
    print(hdr)
    print("-" * 95)
    for r in results[:limit]:
        tr = "Y" if r.get("trail") else "N"
        prob_str = f"{r.get('min_prob', 0):.2f}" if r.get("min_prob") else " -- "
        print(f"{r['lookback']:>4} {r['band']:>4} {r['sl_buf']:>3} "
              f"{r['sessions']:<16} {tr:>2} {r['size']:>2} "
              f"{prob_str:>4} {r['trades']:>4} {r['wr']*100:>5.1f}% "
              f"${r['pnl']:>9,.0f} {r['pf']:>5.2f} ${r['dd']:>7,.0f} {r['calmar']:>6.2f}")


# ═══════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════

def main():
    candles = load_candles()

    # ── Phase 1: Pre-compute VP timelines ──
    lookbacks = [60, 120, 180, 240]
    print(f"\nPhase 1: Pre-computing VP timelines for lookbacks {lookbacks}...")
    timelines = build_vp_timelines(candles, lookbacks)

    # ── Phase 2: Mechanical sweep ──
    mech_results = mechanical_sweep(candles, timelines)

    # Sort by Calmar (best risk-adjusted)
    mech_results.sort(key=lambda r: r["calmar"], reverse=True)

    # Filter: positive PnL + DD < $3k
    good = [r for r in mech_results if r["pnl"] > 0 and r["dd"] < 3000]
    good.sort(key=lambda r: r["calmar"], reverse=True)
    print_table(f"=== Top Mechanical (DD < $3k, PnL > 0) — {len(good)} configs ===", good, 25)

    # Low DD view
    low_dd = [r for r in mech_results if r["pnl"] > 0 and r["dd"] < 2000]
    low_dd.sort(key=lambda r: r["pnl"], reverse=True)
    print_table(f"\n=== Low DD < $2k, PnL > 0 — {len(low_dd)} configs ===", low_dd, 20)

    # Best by PnL
    by_pnl = [r for r in mech_results if r["pnl"] > 0]
    by_pnl.sort(key=lambda r: r["pnl"], reverse=True)
    print_table(f"\n=== Top by PnL — {len(by_pnl)} configs ===", by_pnl, 15)

    if not good:
        print("\nNo configs with positive PnL and DD < $3k. Skipping ML training.")
        # save raw results
        out_path = ROOT / "data" / "ml_trend_sweep_results.json"
        out_path.write_text(json.dumps(mech_results, indent=2), encoding="utf-8")
        print(f"Raw results saved to {out_path}")
        return

    best_mech = good[0]
    print(f"\n[Best mechanical] lookback={best_mech['lookback']} band={best_mech['band']} "
          f"sl_buf={best_mech['sl_buf']} sessions={best_mech['sessions']} "
          f"trail={best_mech['trail']} sz={best_mech['size']}")
    print(f"  Trades={best_mech['trades']} WR={best_mech['wr']*100:.1f}% "
          f"PnL=${best_mech['pnl']:,.0f} PF={best_mech['pf']:.2f} "
          f"MaxDD=${best_mech['dd']:,.0f} Calmar={best_mech['calmar']:.2f}")

    # ── Phase 3: Train ML model ──
    print(f"\nPhase 3: Training ML model on best config (lookback={best_mech['lookback']})...")
    train_cfg = MLTrendConfig(
        lookback=best_mech["lookback"],
        band_ticks=best_mech["band"],
        sl_buffer_ticks=best_mech["sl_buf"],
        tick_size=TICK,
    )
    train_split = int(len(candles) * 0.80)
    train_candles = candles[:train_split]
    train_tl = precompute_vp_timeline(train_candles, train_cfg.lookback, TICK, recalc_interval=5)

    X, y, starts, ends = generate_labeled_data(train_candles, train_tl, train_cfg, stride=3)
    print(f"  Labeled samples: {len(y)} | win rate: {y.mean():.1%}")

    if len(y) < 50:
        print("  Too few samples for ML training. Reporting mechanical results only.")
        out_path = ROOT / "data" / "ml_trend_sweep_results.json"
        out_path.write_text(json.dumps(mech_results, indent=2), encoding="utf-8")
        return

    weights, bias, info = fit_scorer(X, y)
    print(f"  Train AUC={info['auc']:.3f} ACC={info['acc']:.3f} "
          f"Brier={info['brier']:.3f} C={info['C']:.4g}")
    if info["dropped"]:
        print(f"  Dropped (constant): {info['dropped']}")

    # Print feature weights
    sw = info["std_weights"]
    print("\n  Feature weights (normalised, sorted by importance):")
    for name in sorted(sw, key=lambda k: abs(sw[k]), reverse=True):
        if abs(sw[name]) > 0.01:
            print(f"    {name:24s} norm={sw[name]:+.4f}  raw={weights[name]:+.4g}")
    print(f"    {'(bias)':24s}             raw={bias:+.4f}")

    scorer = MLTrendScorer(weights, bias)

    # Save model
    model_path = ROOT / "data" / "models" / "ml_trend_scorer.json"
    model_path.parent.mkdir(parents=True, exist_ok=True)
    model_data = {
        "weights": weights, "bias": bias,
        "feature_names": list(ML_TREND_FEATURE_NAMES),
        "meta": {
            "kind": "logistic", "trained": True,
            "auc": info["auc"], "brier": info["brier"],
            "n_samples": int(len(y)), "win_rate": float(y.mean()),
            "best_config": {
                "lookback": best_mech["lookback"],
                "band_ticks": best_mech["band"],
                "sl_buf_ticks": best_mech["sl_buf"],
            },
        },
    }
    model_path.write_text(json.dumps(model_data, indent=2), encoding="utf-8")
    print(f"\n  Model saved to {model_path}")

    # ── Phase 4: ML-gated sweep ──
    print(f"\nPhase 4: ML-gated sweep (min_prob thresholds)...")
    ml_results = ml_sweep(candles, timelines, scorer, best_mech)
    ml_results.sort(key=lambda r: r["calmar"], reverse=True)
    print_table(f"=== ML-Gated Results ===", ml_results, 30)

    # ── Phase 5: Save all results ──
    all_results = {"mechanical": mech_results, "ml_gated": ml_results}
    out_path = ROOT / "data" / "ml_trend_sweep_results.json"
    out_path.write_text(json.dumps(all_results, indent=2, default=str), encoding="utf-8")
    print(f"\nAll results saved to {out_path}")

    # Final recommendation
    best_overall = ml_results[0] if ml_results else best_mech
    print(f"\n{'='*60}")
    print(f" BEST OVERALL CONFIG")
    print(f"{'='*60}")
    print(f"  Lookback:  {best_overall['lookback']} bars")
    print(f"  Band:      {best_overall['band']} ticks")
    print(f"  SL Buffer: {best_overall['sl_buf']} ticks")
    print(f"  Sessions:  {best_overall['sessions']}")
    print(f"  Trail:     {'50%/5%' if best_overall['trail'] else 'OFF'}")
    print(f"  Size:      {best_overall['size']}x MNQ")
    print(f"  Min Prob:  {best_overall.get('min_prob', 0):.2f}")
    print(f"  ---")
    print(f"  Trades:    {best_overall['trades']}")
    print(f"  Win Rate:  {best_overall['wr']*100:.1f}%")
    print(f"  PnL:       ${best_overall['pnl']:,.0f}")
    print(f"  PF:        {best_overall['pf']:.2f}")
    print(f"  Max DD:    ${best_overall['dd']:,.0f}")
    print(f"  Calmar:    {best_overall['calmar']:.2f}")


if __name__ == "__main__":
    main()
