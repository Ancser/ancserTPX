# ============================================================
# 文件: scripts/confluence_label.py
# 狀態: v1.0.6 (explainable confluence — shared labeler + ML hygiene)
# 用途: 單一真相來源的「前向掃描標註器」與統計衛生工具，供
#       fixed-RR trainer 與 web /confluence/train 端點共用。
#   ← scripts/train_confluence.py      (fixed-RR collect)
#   ← backend/api/routes.py            (web learn)
# ============================================================
"""Forward-scan labeling and sample-hygiene helpers shared by every confluence
trainer, so labels, sample weighting and out-of-sample evaluation are computed
ONE way (train == backtest == live).

Why this module exists:
  * `simulate_outcomes` replaces the two copy-pasted labelers (`_simulate`,
    `_simulate_multi`). It returns the resolution BAR for each outcome so we can
    measure label-window overlap.
  * Overlapping outcome windows make consecutive samples statistically
    dependent (pseudo-replication). `uniqueness_weights` down-weights samples
    that share their outcome window (López de Prado average-uniqueness), so the
    fit and its metrics are not inflated by correlated rows.
  * `walk_forward_oos` gives an honest, embargoed out-of-sample AUC/Brier even
    when the final model is fit on 100% of the data for going live.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Sequence, Tuple

import numpy as np

from backend.db.models import Direction
from backend.backtest.intrabar import resolve_same_bar_exit


# ── 1. unified forward-scan labeler ─────────────────────────────────────────

def simulate_outcomes(candles, i, direction, entry, sl, risk, rr_grid,
                      wait, horizon) -> Dict[float, Tuple[int, int]]:
    """ONE forward scan for a candidate built at the close of bar `i`.

    Returns {rr: (label, end_idx)} for every rr that resolves:
        label   = 1 (TP@r reached before SL) / 0 (SL first),
        end_idx = bar index at which that rr resolved (for overlap/embargo).
    Unfilled (no limit fill within `wait` bars) -> {} (caller drops it).

    Both SL and TP on the same bar resolve by the shared nearer-to-open rule;
    exact ties are conservatively labelled SL.
    """
    n = len(candles)
    buy = direction == Direction.BUY
    # 1) limit fill within `wait` bars (start next bar)
    k = None
    for j in range(i + 1, min(i + 1 + wait, n)):
        c = candles[j]
        if (c.low <= entry) if buy else (c.high >= entry):
            k = j
            break
    if k is None:
        return {}
    tp = {rr: (entry + rr * risk if buy else entry - rr * risk) for rr in rr_grid}
    out: Dict[float, Tuple[int, int]] = {}
    # entry bar: SL-only can trigger first
    ck = candles[k]
    if (ck.low <= sl) if buy else (ck.high >= sl):
        return {rr: (0, k) for rr in rr_grid}
    # 2) scan for SL / TP@r
    for m in range(k + 1, min(k + 1 + horizon, n)):
        c = candles[m]
        sl_hit = (c.low <= sl) if buy else (c.high >= sl)
        for rr in rr_grid:
            if rr in out:
                continue
            tp_hit = (c.high >= tp[rr]) if buy else (c.low <= tp[rr])
            if tp_hit and sl_hit:
                first = resolve_same_bar_exit(c.open, sl, tp[rr])
                out[rr] = (1 if first == "tp" else 0, m)
            elif tp_hit:
                out[rr] = (1, m)
            elif sl_hit:
                out[rr] = (0, m)
        if sl_hit:
            for rr in rr_grid:
                out.setdefault(rr, (0, m))
            break
        if len(out) == len(rr_grid):
            break
    return out


# ── 2. sample uniqueness (decorrelate overlapping outcome windows) ──────────

def uniqueness_weights(starts: Sequence[int], ends: Sequence[int],
                       n_bars: int) -> np.ndarray:
    """Average-uniqueness weight per sample (López de Prado).

    A sample's "outcome window" is [start_bar, end_bar]. Bars shared by many
    samples carry little independent information, so each sample's weight is the
    mean of 1/concurrency over its own window. Returned normalised to mean 1 so
    it scales the loss without changing its magnitude. Empty -> empty array.
    """
    starts = np.asarray(starts, dtype=int)
    ends = np.asarray(ends, dtype=int)
    if starts.size == 0:
        return np.empty(0, dtype=float)
    span = int(n_bars) + 2
    # concurrency[t] = how many samples' windows cover bar t (difference array)
    diff = np.zeros(span + 1, dtype=np.int64)
    np.add.at(diff, starts, 1)
    np.add.at(diff, ends + 1, -1)
    concurrency = np.cumsum(diff)[:span]
    concurrency = np.maximum(concurrency, 1)
    inv = 1.0 / concurrency
    cum = np.concatenate([[0.0], np.cumsum(inv)])      # prefix sum of 1/conc
    lengths = (ends - starts + 1).astype(float)
    avg_uniq = (cum[ends + 1] - cum[starts]) / lengths
    mean = float(avg_uniq.mean())
    return avg_uniq / mean if mean > 0 else np.ones_like(avg_uniq)


# ── 3. embargoed walk-forward out-of-sample estimate ────────────────────────

def walk_forward_oos(X: np.ndarray, y: np.ndarray, starts: Sequence[int],
                     ends: Sequence[int], fit_fn: Callable, *,
                     n_splits: int = 5, embargo: int = 0) -> Dict[str, float]:
    """Rolling-origin walk-forward AUC/Brier with a purge+embargo gap.

    Samples must already be in chronological order (collect() emits them so).
    For each fold the test block is a later contiguous slice; any train sample
    whose outcome window ends within `embargo` bars of the test block's first
    start bar is PURGED, killing the label-window leakage across the split.

    `fit_fn(Xtr, ytr) -> proba(Xte)` is injected so the OOS fit is identical to
    the production fit. Returns mean OOS metrics over folds (NaN if unscorable).
    """
    from sklearn.metrics import roc_auc_score, brier_score_loss

    n = len(y)
    starts = np.asarray(starts, dtype=int)
    ends = np.asarray(ends, dtype=int)
    if n < (n_splits + 1) * 20:
        return {"oos_auc": float("nan"), "oos_brier": float("nan"), "oos_folds": 0}
    bounds = np.linspace(0, n, n_splits + 2, dtype=int)
    aucs: List[float] = []
    briers: List[float] = []
    for f in range(n_splits):
        tr_hi = bounds[f + 1]
        te_lo, te_hi = bounds[f + 1], bounds[f + 2]
        if te_hi - te_lo < 20 or tr_hi < 20:
            continue
        test_start_bar = int(starts[te_lo])
        keep = ends[:tr_hi] < (test_start_bar - embargo)
        if keep.sum() < 20:
            continue
        Xtr, ytr = X[:tr_hi][keep], y[:tr_hi][keep]
        if len(set(ytr.tolist())) < 2:
            continue
        Xte, yte = X[te_lo:te_hi], y[te_lo:te_hi]
        try:
            p = fit_fn(Xtr, ytr)(Xte)
        except Exception:
            continue
        if len(set(yte.tolist())) > 1:
            aucs.append(float(roc_auc_score(yte, p)))
        briers.append(float(brier_score_loss(yte, p)))
    return {
        "oos_auc": float(np.mean(aucs)) if aucs else float("nan"),
        "oos_brier": float(np.mean(briers)) if briers else float("nan"),
        "oos_folds": len(briers),
    }
