# ============================================================
# 文件: scripts/train_model_grid.py
# 用途: Pre-train the FULL model-registry grid so the web MODEL dropdown can
#       pick any combination instantly (trained → selectable, missing → (untrain)).
#
#       Model identity = RR × BAND × MIN_DISTINCT_TF × BREAKOUT
#         RR    ∈ {1, 2, 3}          (step 1)
#         BAND  ∈ {4, 6, 8, 10, 12}  (step 2, ticks)
#         TF    ∈ {2, 3, 4, 5}       (min distinct timeframes)
#         BRK   ∈ {on, off}          (breakout mode)
#       = 3 × 5 × 4 × 2 = 120 models.
#
#       EFFICIENCY: the zone timeline depends only on the (fixed) timeframes, so
#       it is built ONCE per worker. RR only changes the *label* (not the
#       enumerated candidate), so ONE forward-scan per (band,tf,breakout)=40
#       passes labels all 3 RR at once → 120 scorers from 40 collection passes.
#
#       Each scorer is saved to data/models/grid/<name>.json with the same
#       schema as scripts/train_confluence.py (raw-space weights, live==backtest),
#       plus data/models/grid/manifest.json listing every combo (trained/skipped).
#
# 執行:
#   python -m scripts.train_model_grid          # full 120-model grid (background)
# ============================================================
from __future__ import annotations

import json
import os
import pickle
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from itertools import product
from pathlib import Path

import numpy as np

# ── grid axes (per user spec) ──
RR_GRID = (1.0, 2.0, 3.0)
BAND_GRID = (4.0, 6.0, 8.0, 10.0, 12.0)
TF_GRID = (2, 3, 4, 5)
BRK_GRID = (True, False)

# ── HELD training constants (= scripts/train_confluence.py defaults) ──
PKL = Path("data/historical/CON_F_US_MNQ_M26_1m_60d_20260615.pkl")
CONTRACT_ID = "CON.F.US.MNQ.M26"
BASE_MINUTES = 1
STRIDE = 5
WAIT_MIN = 60
HORIZON_MIN = 1440
MIN_SAMPLES = 50          # below this a combo is left (untrain)
# Cost-sensitive loss aversion for the grid. 1.0 = baseline (each loss weighted
# normally). >1 up-weights LOSS samples at fit time → the scorer learns to avoid
# losers → higher PF / lower maxDD / fewer trades. Kept as a single grid-wide
# constant (NOT a naming axis) so the 120-model registry stays comparable; the web
# retrain button can re-fit any one model at a different loss_weight to compare.
LOSS_WEIGHT = 1.0

GRID_DIR = Path("data/models/grid")
MANIFEST = GRID_DIR / "manifest.json"

# ── per-worker globals (populated by the initializer in each spawned process) ──
_W: dict = {}


def model_name(rr: float, band: float, tf: int, brk: bool) -> str:
    """Canonical file stem encoding the model params (the registry naming rule)."""
    return f"rr{int(round(rr))}_b{int(round(band))}_tf{tf}_{'brk' if brk else 'nobrk'}"


def _init_worker():
    """Runs ONCE per worker: load candles + build the (shared) zone timeline."""
    from backend.db.models import get_tick_size
    from backend.strategy.confluence import MAX_RECENCY_DEPTH
    from backend.backtest.confluence_backtest import build_zone_timeline
    from scripts.confluence_common import timeframes_for_base

    with PKL.open("rb") as f:
        candles = sorted(pickle.load(f), key=lambda c: c.timestamp)
    tick = get_tick_size(CONTRACT_ID)
    tfs = timeframes_for_base(BASE_MINUTES)
    _W["candles"] = candles
    _W["tick"] = tick
    _W["tfs"] = tfs
    _W["wait_bars"] = max(1, round(WAIT_MIN / BASE_MINUTES))
    _W["horizon_bars"] = max(1, round(HORIZON_MIN / BASE_MINUTES))
    _W["timeline"] = build_zone_timeline(candles, tfs, tick, MAX_RECENCY_DEPTH)


def _collect_all_rr(candles, timeline, cfg, stride, wait, horizon, rr_grid):
    """ONE forward-scan that labels EVERY rr in rr_grid. The enumerated candidate
    set is identical across rr (rr only changes the TP target), so we score the
    setup once and ask simulate_outcomes for all rr at once. Returns
    {rr: (X, y, starts, ends)} (lists)."""
    from backend.strategy.confluence import evaluate_confluence_scored
    from backend.strategy.confluence_features import features_to_vector, CONTEXT_WINDOW
    from backend.strategy.confluence_scorer import ConfluenceScorer
    from scripts.confluence_label import simulate_outcomes

    heuristic = ConfluenceScorer.heuristic()
    out = {rr: ([], [], [], []) for rr in rr_grid}
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
            vec = None
            for rr in rr_grid:
                got = res.get(rr)
                if got is None:
                    continue
                label, end_idx = got
                if vec is None:
                    vec = features_to_vector(sig.features)
                X, y, starts, ends = out[rr]
                X.append(vec)
                y.append(label)
                starts.append(i)
                ends.append(end_idx)
    return out


def _combo_task(args):
    """One (band, tf, breakout): collect once, fit + save a scorer per rr."""
    from datetime import datetime
    from backend.strategy.confluence import ConfluenceConfig
    from backend.strategy.confluence_features import FEATURE_NAMES
    from backend.strategy.confluence_scorer import ConfluenceScorer
    from scripts.train_confluence import evaluate_and_meta

    band, tf, brk = args
    cfg = ConfluenceConfig(band_ticks=band, min_distinct_tf=tf, enable_breakout=brk)
    cfg.direction_mode = "auto"
    cfg.tick_size = _W["tick"]

    collected = _collect_all_rr(
        _W["candles"], _W["timeline"], cfg, STRIDE,
        _W["wait_bars"], _W["horizon_bars"], RR_GRID,
    )

    embargo = _W["wait_bars"] + _W["horizon_bars"]
    n_bars = len(_W["candles"])
    rows = []
    for rr in RR_GRID:
        X, y, starts, ends = collected[rr]
        name = model_name(rr, band, tf, brk)
        rec = {"name": name, "rr": rr, "band": band, "min_distinct_tf": tf,
               "breakout": brk, "n_samples": len(y)}
        if len(y) < MIN_SAMPLES or len(set(y)) < 2:
            rec.update(trained=False, reason=f"too few samples ({len(y)})")
            rows.append(rec)
            continue
        Xa = np.array(X, dtype=float)
        ya = np.array(y, dtype=int)
        sa = np.array(starts, dtype=int)
        ea = np.array(ends, dtype=int)
        weights, bias, info = evaluate_and_meta(
            Xa, ya, sa, ea, n_bars=n_bars, embargo=embargo, C=None,
            loss_weight=LOSS_WEIGHT)
        scorer = ConfluenceScorer(
            weights=weights, bias=bias,
            meta={
                "kind": "logistic", "trained": True, "grid_model": name,
                "trained_at": datetime.now().isoformat(timespec="seconds"),
                "contract": CONTRACT_ID, "base_min": BASE_MINUTES,
                "timeframes": list(_W["tfs"]), "n_samples": int(len(ya)),
                "train_win_rate": float(ya.mean()), "train_auc": info["auc"],
                "train_brier": info["brier"], "C": info["C"],
                "oos_auc": info["oos_auc"], "oos_brier": info["oos_brier"],
                "oos_folds": info["oos_folds"],
                "dropped_features": info["dropped_features"],
                "std_weights": info["std_weights"],
                "loss_weight": LOSS_WEIGHT,
                "cfg": {"band_ticks": band, "min_distinct_tf": tf, "rr": rr,
                        "enable_breakout": brk, "wait_min": WAIT_MIN,
                        "horizon_min": HORIZON_MIN, "loss_weight": LOSS_WEIGHT},
            },
        )
        GRID_DIR.mkdir(parents=True, exist_ok=True)
        scorer.save(GRID_DIR / f"{name}.json")
        rec.update(trained=True, win_rate=float(ya.mean()),
                   train_auc=info["auc"], oos_auc=info["oos_auc"],
                   oos_brier=info["oos_brier"])
        rows.append(rec)
    return {"band": band, "tf": tf, "brk": brk, "rows": rows}


def _fmt(rec: dict) -> str:
    if not rec.get("trained"):
        return f"  {rec['name']:<22} (untrain) — {rec.get('reason','')}"
    return (f"  {rec['name']:<22} n={rec['n_samples']:>5} win={rec['win_rate']*100:>4.1f}% "
            f"auc={rec['train_auc']:.3f} oos={rec['oos_auc']:.3f}")


def _combo_done(band, tf, brk) -> bool:
    """A (band,tf,breakout) pass is complete iff all 3 RR model files exist."""
    return all((GRID_DIR / f"{model_name(rr, band, tf, brk)}.json").exists()
               for rr in RR_GRID)


def main() -> None:
    GRID_DIR.mkdir(parents=True, exist_ok=True)
    all_combos = list(product(BAND_GRID, TF_GRID, BRK_GRID))   # 40 collection passes
    n_models = len(all_combos) * len(RR_GRID)                  # 120 models
    # RESUME: skip passes whose 3 RR files already exist on disk (so a crash /
    # OOM restart continues instead of re-training the whole grid).
    combos = [c for c in all_combos if not _combo_done(*c)]
    skipped = len(all_combos) - len(combos)
    # MEMORY: each worker holds its OWN full zone timeline (large); 14 workers
    # OOM'd this box. Default conservatively to cpu//4; override with GRID_WORKERS.
    env_w = os.environ.get("GRID_WORKERS")
    workers = (max(1, int(env_w)) if env_w
               else max(1, (os.cpu_count() or 4) // 4))
    workers = min(workers, max(1, len(combos)))

    t0 = time.perf_counter()
    print(f"[Grid] {len(all_combos)} passes ({skipped} already done, "
          f"{len(combos)} to run) -> {n_models} models, {workers} workers "
          f"(timeline built once per worker; ~40s startup)", flush=True)
    print(f"[Grid] models -> {GRID_DIR}/  manifest -> {MANIFEST}", flush=True)
    if not combos:
        print("[Grid] nothing to do — all 120 models already trained.", flush=True)
        return

    manifest = []
    done = 0
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker) as ex:
        fut_map = {ex.submit(_combo_task, c): c for c in combos}
        for fut in as_completed(fut_map):
            res = fut.result()
            done += 1
            band, tf, brk = res["band"], res["tf"], res["brk"]
            print(f"[{done:>2}/{len(combos)}] band={band:g} tf={tf} "
                  f"brk={'on' if brk else 'off'}  ({time.perf_counter()-t0:.0f}s)",
                  flush=True)
            for rec in res["rows"]:
                print(_fmt(rec), flush=True)
                manifest.append(rec)
            # Stream the manifest after every pass so it is always current on disk.
            MANIFEST.write_text(json.dumps({
                "grid": {"rr": list(RR_GRID), "band": list(BAND_GRID),
                         "min_distinct_tf": list(TF_GRID), "breakout": list(BRK_GRID)},
                "n_models": n_models, "trained": sum(1 for m in manifest if m.get("trained")),
                "models": manifest,
            }, indent=2, default=str), encoding="utf-8")

    ok = sum(1 for m in manifest if m.get("trained"))
    print(f"\n[Grid] DONE — {ok}/{n_models} trained, "
          f"{n_models-ok} left (untrain). wall={time.perf_counter()-t0:.0f}s "
          f"({(time.perf_counter()-t0)/60:.1f} min)", flush=True)


if __name__ == "__main__":
    main()
