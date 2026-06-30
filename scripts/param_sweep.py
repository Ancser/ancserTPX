"""Parameter sweep: pre-compute signals once → replay with different gates.

Writes results to data/machinelearning/param_sweep_results.txt for monitoring.
Only keeps top-5 signals per bar to save memory (~150MB vs ~1.5GB).
"""

from __future__ import annotations
import math, sys, time, os
from dataclasses import dataclass
from datetime import timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.db.models import get_tick_size, get_point_value
from backend.strategy.confluence import (
    ConfluenceConfig, MAX_RECENCY_DEPTH, evaluate_confluence_scored,
)
from backend.strategy.confluence_scorer import ConfluenceScorer, default_scorer_path
from backend.strategy.confluence_features import CONTEXT_WINDOW
from backend.backtest.confluence_backtest import build_zone_timeline
from backend.backtest.intrabar import resolve_same_bar_exit
from backend.strategy.consolidation import timeframes_for_base
from backend.ml.confluence_common import load_store

CONTRACT = "CON.F.US.MNQ.M26"
TICK = get_tick_size(CONTRACT)
PV = get_point_value(CONTRACT)
SIZE = 3
TARGET_DD = 2000
WAIT_BARS = 1
TOP_N = 10   # keep top-N signals per bar (by score)
_CT = ZoneInfo("America/Chicago")

OUT_DIR = ROOT / "data" / "machinelearning"
OUT_FILE = OUT_DIR / "param_sweep_results.txt"

_log_fh = None

def log(msg):
    global _log_fh
    if _log_fh is None:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        _log_fh = open(OUT_FILE, "w", encoding="utf-8")
    _log_fh.write(msg + "\n")
    _log_fh.flush()
    os.fsync(_log_fh.fileno())
    print(msg, flush=True)


@dataclass(slots=True)
class Sig:
    direction: str
    zone_id: str
    mode: str
    entry: float
    sl: float
    tp: float
    score: float
    prob: float
    ev: float
    risk_ticks: float


def precompute(candles, timeline, scorer, cfg) -> List[List[Sig]]:
    n = len(candles)
    modes = cfg.auto_modes()
    edge = WAIT_BARS + 2
    out: List[List[Sig]] = [[] for _ in range(n)]
    t0 = time.time()
    step = max(1, n // 20)
    total_kept = 0

    for i in range(n - edge):
        snap = timeline[i]
        if len(snap) < cfg.min_distinct_tf:
            continue
        recent = candles[max(0, i - CONTEXT_WINDOW + 1):i + 1]
        sigs = evaluate_confluence_scored(
            snap, candles[i].close, cfg, scorer, modes=modes,
            recent_candles=recent,
        )
        if not sigs:
            continue
        sigs.sort(key=lambda s: s.score, reverse=True)
        bar_sigs = []
        for s in sigs[:TOP_N]:
            rt = abs(s.entry_price - s.sl_price) / TICK
            bar_sigs.append(Sig(
                direction=s.direction.value.upper(), mode=s.direction_mode,
                zone_id=s.cluster.largest_tf,
                entry=s.entry_price, sl=s.sl_price, tp=s.tp_price,
                score=s.score, prob=s.prob, ev=s.ev, risk_ticks=rt,
            ))
        out[i] = bar_sigs
        total_kept += len(bar_sigs)

        if (i + 1) % step == 0:
            el = time.time() - t0
            rate = (i + 1) / el if el > 0 else 0
            eta = (n - edge - i) / rate if rate > 0 else 0
            log(f"  {i+1}/{n} ({100*(i+1)//n}%) {rate:.0f} b/s ETA {eta:.0f}s kept={total_kept}")

    log(f"  Done: {total_kept} signals in {time.time()-t0:.0f}s")
    return out


def replay(candles, sigs_by_bar, *, min_score=0.0, max_risk=0,
           trail_pct=0.5, trail_lock=0.05, session_limit=True,
           full_tp_lock=0) -> dict:
    n = len(candles)
    edge = WAIT_BARS + 2
    trades = []
    capital = peak = max_dd = 0.0
    consec = max_consec = 0
    total_gain = total_loss = 0.0
    open_dir = None
    open_entry = open_sl = open_tp = 0.0
    trail_on = False
    pending = None
    pending_age = 0
    sess_used = set()
    sess_tp: Dict[str, int] = {}

    def skey(ts):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ct = ts.astimezone(_CT)
        if ct.hour >= 17:
            ct = ct + timedelta(days=1)
        return ct.strftime("%Y-%m-%d")

    def lkey(ts, sig):
        direction = "up" if sig.direction == "BUY" else "down"
        return (skey(ts), str(sig.zone_id), direction)

    def do_exit(ep):
        nonlocal capital, peak, max_dd, consec, max_consec
        nonlocal total_gain, total_loss, open_dir, trail_on
        pnl = ((ep - open_entry) if open_dir == "BUY" else (open_entry - ep)) * PV * SIZE
        pnl -= 4.56 * SIZE
        capital += pnl
        trades.append(pnl)
        if pnl >= 0:
            total_gain += pnl
            consec = 0
        else:
            total_loss += abs(pnl)
            consec += 1
            max_consec = max(max_consec, consec)
        peak = max(peak, capital)
        max_dd = max(max_dd, peak - capital)
        is_tp = (open_dir == "BUY" and ep >= open_tp) or \
                (open_dir == "SELL" and ep <= open_tp)
        open_dir = None
        trail_on = False
        return is_tp

    for i in range(n):
        c = candles[i]
        if open_dir is not None:
            if open_dir == "BUY":
                hsl = c.low <= open_sl
                htp = c.high >= open_tp
            else:
                hsl = c.high >= open_sl
                htp = c.low <= open_tp
            if hsl and htp:
                if resolve_same_bar_exit(c.open, open_sl, open_tp) == "sl":
                    do_exit(open_sl)
                else:
                    is_tp = do_exit(open_tp)
                    if is_tp and full_tp_lock > 0:
                        k = skey(c.timestamp)
                        sess_tp[k] = sess_tp.get(k, 0) + 1
            elif hsl:
                do_exit(open_sl)
            elif htp:
                is_tp = do_exit(open_tp)
                if is_tp and full_tp_lock > 0:
                    k = skey(c.timestamp)
                    sess_tp[k] = sess_tp.get(k, 0) + 1
            elif trail_pct > 0:
                if open_dir == "BUY":
                    td = open_tp - open_entry
                    if c.close >= open_entry + td * trail_pct and not trail_on:
                        trail_on = True
                        open_sl = max(open_sl, open_entry + td * trail_lock)
                else:
                    td = open_entry - open_tp
                    if c.close <= open_entry - td * trail_pct and not trail_on:
                        trail_on = True
                        open_sl = min(open_sl, open_entry - td * trail_lock)
            if open_dir is not None:
                continue

        if pending is not None:
            filled = (c.low <= pending.entry) if pending.direction == "BUY" \
                     else (c.high >= pending.entry)
            if filled:
                open_dir = pending.direction
                open_entry = pending.entry
                open_sl = pending.sl
                open_tp = pending.tp
                trail_on = False
                pending = None
                pending_age = 0
                if open_dir == "BUY" and c.low <= open_sl:
                    do_exit(open_sl)
                elif open_dir == "SELL" and c.high >= open_sl:
                    do_exit(open_sl)
                continue
            pending_age += 1
            if pending_age >= WAIT_BARS:
                if session_limit:
                    sess_used.discard(lkey(c.timestamp, pending))
                pending = None
                pending_age = 0
            continue

        if i >= n - edge:
            continue
        if full_tp_lock > 0 and sess_tp.get(skey(c.timestamp), 0) >= full_tp_lock:
            continue

        bar = sigs_by_bar[i]
        if not bar:
            continue
        best = None
        for s in bar:
            if s.score < min_score:
                continue
            if max_risk and s.risk_ticks > max_risk:
                continue
            if session_limit and lkey(c.timestamp, s) in sess_used:
                continue
            if best is None or s.ev > best.ev:
                best = s
        if best is not None:
            if session_limit:
                sess_used.add(lkey(c.timestamp, best))
            pending = best
            pending_age = 0

    nt = len(trades)
    if nt == 0:
        return {"trades": 0, "wins": 0, "wr": 0, "pnl": 0, "dd": 0,
                "pf": 0, "consec": 0, "avg_w": 0, "avg_l": 0}
    wins = [t for t in trades if t >= 0]
    losses = [t for t in trades if t < 0]
    return {
        "trades": nt, "wins": len(wins), "wr": len(wins)/nt,
        "pnl": sum(trades), "dd": max_dd,
        "pf": total_gain / total_loss if total_loss > 0 else 999,
        "consec": max_consec,
        "avg_w": sum(wins)/len(wins) if wins else 0,
        "avg_l": sum(losses)/len(losses) if losses else 0,
    }


def fmt(r, label=""):
    ok = r["dd"] < TARGET_DD and r["pnl"] > 0
    mark = " ***PASS***" if ok else ""
    return (f"{label:45s} {r['trades']:4d}t WR={r['wr']:.1%} "
            f"PnL=${r['pnl']:>8,.0f} DD=${r['dd']:>7,.0f} "
            f"PF={r['pf']:5.2f} consec={r['consec']:2d} "
            f"avg_l=${r['avg_l']:>7,.0f}{mark}")


def main():
    t0 = time.time()
    log("=" * 90)
    log("PARAMETER SWEEP — 3 MNQ, target maxDD < $2,000")
    log("=" * 90)

    candles = load_store(1, "MNQ")
    if not candles:
        raise SystemExit("No store data.")
    candles.sort(key=lambda c: c.timestamp)
    log(f"Data: {len(candles)} bars ({candles[0].timestamp.date()} .. {candles[-1].timestamp.date()})")

    scorer = ConfluenceScorer.load(default_scorer_path())
    log(f"Scorer: {scorer.source_name()}")

    tfs = timeframes_for_base(1)
    log(f"\nBuilding zone timeline...")
    t1 = time.time()
    timeline = build_zone_timeline(candles, tfs, TICK, MAX_RECENCY_DEPTH)
    log(f"Timeline: {time.time()-t1:.0f}s")

    # ── Pre-compute signals at RR=3 (trained value) ──
    log(f"\n--- PRE-COMPUTE (RR=3, top-{TOP_N}/bar) ---")
    cfg = ConfluenceConfig(band_ticks=4.0, min_distinct_tf=2, rr=3.0)
    cfg.direction_mode = "auto"
    cfg.tick_size = TICK
    cfg.max_risk_ticks = None
    sigs = precompute(candles, timeline, scorer, cfg)

    all_s = [s for bar in sigs for s in bar]
    if all_s:
        rtv = sorted([s.risk_ticks for s in all_s])
        log(f"Signals: {len(all_s)} total")
        log(f"  risk_ticks: p10={rtv[len(rtv)//10]:.0f} med={rtv[len(rtv)//2]:.0f} "
            f"p90={rtv[9*len(rtv)//10]:.0f} max={rtv[-1]:.0f}")

    # ================================================================
    # PHASE 1: Baseline
    # ================================================================
    log(f"\n{'='*90}")
    log("PHASE 1: BASELINE (no gate, no max_risk)")
    log(f"{'='*90}")
    for trail, tl in [(0.5, "trail=ON"), (0.0, "trail=OFF")]:
        for sl in [True, False]:
            lbl = f"{tl} sess={'ON' if sl else 'OFF'}"
            r = replay(candles, sigs, trail_pct=trail, trail_lock=0.05 if trail else 0,
                       session_limit=sl)
            log(fmt(r, lbl))

    # ================================================================
    # PHASE 2: Max risk ticks (biggest DD lever)
    # ================================================================
    log(f"\n{'='*90}")
    log("PHASE 2: MAX RISK TICKS (with trail=ON, session=ON)")
    log(f"{'='*90}")
    for mr in [0, 40, 60, 80, 100, 120, 140, 160, 200, 300]:
        r = replay(candles, sigs, max_risk=mr)
        log(fmt(r, f"max_risk={mr}"))

    # ================================================================
    # PHASE 3: Probability gate (second biggest lever)
    # ================================================================
    log(f"\n{'='*90}")
    log("PHASE 3: PROBABILITY GATE (trail=ON, session=ON)")
    log(f"{'='*90}")
    for mp in [0, 0.30, 0.35, 0.40, 0.42, 0.44, 0.46, 0.48, 0.50,
               0.52, 0.54, 0.56, 0.58, 0.60, 0.65, 0.70, 0.75]:
        ms = math.log(mp / (1 - mp)) if 0 < mp < 1 else 0.0
        r = replay(candles, sigs, min_score=ms)
        log(fmt(r, f"min_prob={mp:.2f}"))

    # ================================================================
    # PHASE 4: Full TP lock
    # ================================================================
    log(f"\n{'='*90}")
    log("PHASE 4: FULL TP LOCK (trail=ON, session=ON)")
    log(f"{'='*90}")
    for ftl in [0, 1, 2, 3]:
        r = replay(candles, sigs, full_tp_lock=ftl)
        log(fmt(r, f"full_tp_lock={ftl}"))

    # ================================================================
    # PHASE 5: CROSS-COMBOS (systematic grid)
    # ================================================================
    log(f"\n{'='*90}")
    log("PHASE 5: CROSS-COMBINATIONS")
    log(f"{'='*90}")

    results = []
    for mr in [0, 60, 80, 100, 120, 160]:
        for mp in [0, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70]:
            for trail in [0.0, 0.5]:
                for sl in [True, False]:
                    for ftl in [0, 1, 2]:
                        ms = math.log(mp / (1 - mp)) if 0 < mp < 1 else 0.0
                        r = replay(candles, sigs, min_score=ms, max_risk=mr,
                                   trail_pct=trail, trail_lock=0.05 if trail else 0,
                                   session_limit=sl, full_tp_lock=ftl)
                        label = (f"mr={mr:3d} p={mp:.2f} "
                                 f"tr={'Y' if trail else 'N'} "
                                 f"se={'Y' if sl else 'N'} ftl={ftl}")
                        results.append((label, r))

    log(f"\nTotal combos: {len(results)}")

    qualified = [(l, r) for l, r in results if r["dd"] < TARGET_DD and r["pnl"] > 0]
    qualified.sort(key=lambda x: x[1]["pnl"], reverse=True)
    log(f"Qualified (DD<${TARGET_DD} + PnL>0): {len(qualified)}")

    if qualified:
        log(f"\n--- TOP 20 QUALIFIED (by PnL) ---")
        for l, r in qualified[:20]:
            log(fmt(r, l))

    log(f"\n--- TOP 10 BY LOWEST DD ---")
    by_dd = sorted(results, key=lambda x: x[1]["dd"])
    for l, r in by_dd[:10]:
        log(fmt(r, l))

    log(f"\n--- TOP 10 BY HIGHEST PnL ---")
    by_pnl = sorted(results, key=lambda x: x[1]["pnl"], reverse=True)
    for l, r in by_pnl[:10]:
        log(fmt(r, l))

    # ================================================================
    # PHASE 6: FINE-TUNE around best qualified
    # ================================================================
    if qualified:
        log(f"\n{'='*90}")
        log("PHASE 6: FINE-TUNE around best")
        log(f"{'='*90}")
        # Parse best combo
        best_l, best_r = qualified[0]
        log(f"Best base: {best_l}")
        # Extract params from label
        parts = best_l.split()
        b_mr = int(parts[0].split("=")[1])
        b_mp = float(parts[1].split("=")[1])
        b_tr = parts[2].split("=")[1] == "Y"
        b_se = parts[3].split("=")[1] == "Y"
        b_ftl = int(parts[4].split("=")[1])

        # Fine-tune prob around best
        for mp in [b_mp - 0.05, b_mp - 0.03, b_mp - 0.02, b_mp - 0.01,
                   b_mp, b_mp + 0.01, b_mp + 0.02, b_mp + 0.03, b_mp + 0.05]:
            if mp <= 0 or mp >= 1:
                continue
            ms = math.log(mp / (1 - mp))
            for mr in [max(0, b_mr - 20), b_mr, b_mr + 20]:
                r = replay(candles, sigs, min_score=ms, max_risk=mr,
                           trail_pct=0.5 if b_tr else 0, trail_lock=0.05 if b_tr else 0,
                           session_limit=b_se, full_tp_lock=b_ftl)
                log(fmt(r, f"  mr={mr} p={mp:.2f}"))

    # ================================================================
    # SUMMARY
    # ================================================================
    log(f"\n{'='*90}")
    log("FINAL SUMMARY")
    log(f"{'='*90}")

    all_res = results
    winners = sorted(
        [(l, r) for l, r in all_res if r["dd"] < TARGET_DD and r["pnl"] > 0],
        key=lambda x: x[1]["pnl"], reverse=True
    )
    if winners:
        log(f"\nPASS: {len(winners)} combos meet DD<${TARGET_DD} + PnL>0")
        b = winners[0]
        log(f"\nBEST: {b[0]}")
        log(f"  Trades={b[1]['trades']} WR={b[1]['wr']:.1%} PnL=${b[1]['pnl']:,.0f} "
            f"DD=${b[1]['dd']:,.0f} PF={b[1]['pf']:.2f} consec={b[1]['consec']} "
            f"avg_w=${b[1]['avg_w']:,.0f} avg_l=${b[1]['avg_l']:,.0f}")
    else:
        log(f"\nFAIL: no combo meets DD<${TARGET_DD} + PnL>0")
        bd = sorted(all_res, key=lambda x: x[1]["dd"])[0]
        log(f"Lowest DD: {bd[0]} → DD=${bd[1]['dd']:,.0f} PnL=${bd[1]['pnl']:,.0f}")

    log(f"\nTotal time: {time.time()-t0:.0f}s")
    log(f"Results saved to: {OUT_FILE}")


if __name__ == "__main__":
    main()
