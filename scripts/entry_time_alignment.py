"""
Entry-time alignment: how far apart (in TIME) do live and backtest fire the SAME
trade, WHY, and can we squeeze the gap under 1 minute?

Background
----------
The PnL of live ≈ backtest now (the judging rules were fixed). But entries still
drift 1-3 min apart. This script quantifies that drift and tests fixes.

Root cause (from backend/backtest/engine.py):
  * Backtest fills a pending LIMIT at the breakout level on the FIRST 1m bar whose
    low<=entry (buy) / high>=entry (sell), and stamps
        entry_time = candle.timestamp          # the 1m bar's OPEN minute (:00 sec)
        entry_price = signal.entry_price        # exact limit, zero slippage
  * Live stamps entry_time = the broker's actual FILL time (mid-minute + order /
    confirmation / network latency) and entry_price = the real fill.
So even for a perfectly-matched signal, live is stamped LATER than backtest by
(seconds-into-the-bar + latency). If the limit needs a pullback, live can rest a
few bars → 1-3 min. This script measures that delta and shows what constant
time-shift on the backtest stamp drives the most pairs under 60 s.

What it prints
--------------
  1. delta = live.entry_time - backtest.entry_time  (signed seconds), distribution
  2. buckets: <=1min / 1-2 / 2-3 / >3 min, and the signed median (the latency)
  3. breakdown by direction / exit_reason / UTC hour
  4. CORRECTION EXPERIMENT: apply candidate offsets to the backtest stamp and
     report the % of pairs that land within 1 min — i.e. the achievable alignment
  5. UNMATCHED live trades (no bt signal within window) — the real divergence risk
  6. a focused re-run since SINCE_DATE (the post-rule-fix regime)

Run:  python -m scripts.entry_time_alignment
"""
from __future__ import annotations

import statistics
import sys
from collections import defaultdict
from datetime import timedelta

# Windows console defaults to cp1252; preset names + box-drawing chars are UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from scripts.live_vs_backtest_slippage import (
    load_live_signals, run_backtest, MNQ_PV, TICK, MATCH_PX_TICKS,
)
from scripts.defense_backtest_compare import _utc

# Match tolerances. Time window is WIDE on purpose — we want to SEE the 1-3 min
# drift, not pre-filter it away. Price tol keeps us on the same breakout level so
# we pair the same signal (not a different trade that merely fired nearby).
MATCH_WIN_MIN = 8
SINCE_DATE = "2026-06-25"          # last Thursday = first day of the fixed-rule regime
CORRECTION_OFFSETS_S = [0, 15, 30, 45, 60, 75, 90, 120, 180]  # added to bt stamp

# Multi-account copy-trading fires the SAME signal once per account, each filling
# a few ticks / a few seconds apart. Collapse those into ONE representative signal
# so we compare the STRATEGY, not the account count. Two live fills merge if same
# direction AND within MERGE_DT_S seconds AND within MERGE_PX_TICKS ticks.
MERGE_DT_S = 90
MERGE_PX_TICKS = 8


def merge_close_signals(sigs):
    """Cluster near-identical live fills (multi-account copies / micro re-entries)
    into one signal. Representative = the cluster's median-time member, with PnL
    AVERAGED across the cluster (so a 5-account copy counts once, at its mean
    outcome). Returns merged list + (raw_count, merged_count)."""
    if not sigs:
        return [], (0, 0)
    sigs = sorted(sigs, key=lambda s: s["entry_time"])
    px_tol = MERGE_PX_TICKS * TICK
    clusters = []
    cur = [sigs[0]]
    for s in sigs[1:]:
        ref = cur[-1]
        same = (
            s["direction"] == ref["direction"]
            and (s["entry_time"] - ref["entry_time"]).total_seconds() <= MERGE_DT_S
            and s["entry_price"] is not None and ref["entry_price"] is not None
            and abs(s["entry_price"] - ref["entry_price"]) <= px_tol
        )
        if same:
            cur.append(s)
        else:
            clusters.append(cur)
            cur = [s]
    clusters.append(cur)

    merged = []
    for c in clusters:
        c = sorted(c, key=lambda s: s["entry_time"])
        rep = dict(c[len(c) // 2])               # median-time member
        rep["pnl"] = sum(x["pnl"] for x in c) / len(c)
        rep["n_accounts"] = len(c)
        merged.append(rep)
    return merged, (len(sigs), len(merged))


def _to_bt_dicts(bt_trades, lo, hi):
    out = []
    for t in bt_trades:
        et = _utc(t.entry_time)
        if not (lo <= et <= hi):
            continue
        out.append({
            "entry_time": et,
            "direction": t.direction.value if hasattr(t.direction, "value") else str(t.direction),
            "entry_price": t.entry_price,
            "pnl": t.pnl or 0.0,
            "exit_reason": t.exit_reason,
        })
    return out


def match_pairs(live, bt):
    """Greedy nearest-time, constrained to same direction + same breakout price.
    Returns (pairs, live_only). pair = (live, bt, delta_seconds)."""
    bt_used = [False] * len(bt)
    win = timedelta(minutes=MATCH_WIN_MIN)
    px_tol = MATCH_PX_TICKS * TICK
    pairs, live_only = [], []
    for l in live:
        best_j, best_dt = None, win + timedelta(seconds=1)
        for j, b in enumerate(bt):
            if bt_used[j] or b["direction"] != l["direction"]:
                continue
            if l["entry_price"] is None or b["entry_price"] is None:
                continue
            if abs(l["entry_price"] - b["entry_price"]) > px_tol:
                continue
            dt = abs(b["entry_time"] - l["entry_time"])
            if dt <= win and dt < best_dt:
                best_j, best_dt = j, dt
        if best_j is None:
            live_only.append(l)
        else:
            bt_used[best_j] = True
            b = bt[best_j]
            delta = (l["entry_time"] - b["entry_time"]).total_seconds()  # +live later
            pairs.append((l, b, delta))
    return pairs, live_only


def _pct(n, d):
    return f"{(100.0 * n / d):5.1f}%" if d else "  n/a"


def _bucket_report(deltas):
    n = len(deltas)
    if not n:
        print("  (no matched pairs)")
        return
    absd = [abs(d) for d in deltas]
    le1 = sum(1 for d in absd if d <= 60)
    b12 = sum(1 for d in absd if 60 < d <= 120)
    b23 = sum(1 for d in absd if 120 < d <= 180)
    gt3 = sum(1 for d in absd if d > 180)
    print(f"  pairs                 : {n}")
    print(f"  signed delta (s)      : median {statistics.median(deltas):+.0f}  "
          f"mean {statistics.fmean(deltas):+.0f}  "
          f"stdev {statistics.pstdev(deltas):.0f}   (+ = live LATER than backtest)")
    print(f"  |delta| <= 1 min      : {le1:4d}  {_pct(le1, n)}")
    print(f"  |delta|  1-2 min      : {b12:4d}  {_pct(b12, n)}")
    print(f"  |delta|  2-3 min      : {b23:4d}  {_pct(b23, n)}")
    print(f"  |delta|  > 3 min      : {gt3:4d}  {_pct(gt3, n)}")


def _correction_experiment(deltas):
    """If we shift the backtest stamp by +offset, |delta-offset| is the residual.
    Report which constant offset lands the most pairs within 1 min."""
    n = len(deltas)
    if not n:
        return
    print("  offset(+s)  within-1min   median|resid|")
    best = None
    for off in CORRECTION_OFFSETS_S:
        resid = [abs(d - off) for d in deltas]
        within = sum(1 for r in resid if r <= 60)
        med = statistics.median(resid)
        star = ""
        if best is None or within > best[1]:
            best = (off, within, med)
        print(f"    {off:4d}      {within:4d} {_pct(within, n)}     {med:6.0f}s")
    # also the data-driven best-fit (median delta) — the single number that
    # describes the live-vs-backtest latency
    med_delta = statistics.median(deltas)
    resid = [abs(d - med_delta) for d in deltas]
    within = sum(1 for r in resid if r <= 60)
    print(f"    best-fit shift = median delta = {med_delta:+.0f}s "
          f"-> within-1min {within} {_pct(within, n)}")


def analyse(live, bt, label):
    print("=" * 68)
    print(label)
    print("=" * 68)
    pairs, live_only = match_pairs(live, bt)
    deltas = [d for _, _, d in pairs]

    print(f"live signals: {len(live)}   backtest signals: {len(bt)}   "
          f"matched: {len(pairs)}   live-only: {len(live_only)}")
    print()
    print("── TIME DRIFT (live entry - backtest entry) ──────────────────")
    _bucket_report(deltas)
    print()

    if deltas:
        print("── DRIFT by DIRECTION ────────────────────────────────────────")
        by_dir = defaultdict(list)
        for l, b, d in pairs:
            by_dir[l["direction"]].append(d)
        for dr in sorted(by_dir):
            ds = by_dir[dr]
            print(f"  {dr:4}  n={len(ds):4d}  median {statistics.median(ds):+5.0f}s  "
                  f"within1min {_pct(sum(1 for x in ds if abs(x)<=60), len(ds))}")
        print()

        print("── DRIFT by EXIT REASON (live) ───────────────────────────────")
        by_ex = defaultdict(list)
        for l, b, d in pairs:
            by_ex[l.get("exit_reason") or "?"].append(d)
        for ex in sorted(by_ex):
            ds = by_ex[ex]
            print(f"  {ex:6}  n={len(ds):4d}  median {statistics.median(ds):+5.0f}s")
        print()

        print("── DRIFT by UTC HOUR ─────────────────────────────────────────")
        by_h = defaultdict(list)
        for l, b, d in pairs:
            by_h[l["entry_time"].hour].append(d)
        for h in sorted(by_h):
            ds = by_h[h]
            print(f"  {h:02d}:00  n={len(ds):4d}  median {statistics.median(ds):+5.0f}s  "
                  f"within1min {_pct(sum(1 for x in ds if abs(x)<=60), len(ds))}")
        print()

        print("── CORRECTION EXPERIMENT (shift backtest stamp) ──────────────")
        print("  goal: pick a constant time-shift that drives entries < 1 min apart")
        _correction_experiment(deltas)
        print()

    print("── UNMATCHED LIVE (no backtest signal within "
          f"{MATCH_WIN_MIN} min @ same level) ──")
    print(f"  count {len(live_only)}   pnl {sum(x['pnl'] for x in live_only):+.1f}  "
          f"(these are real live↔backtest divergences, not just timing)")
    if live_only:
        for x in sorted(live_only, key=lambda r: r["entry_time"])[:12]:
            print(f"    {x['entry_time']:%m-%d %H:%M:%S} {x['direction']:4} "
                  f"@ {x['entry_price']}  pnl {x['pnl']:+7.1f}  {x.get('exit_reason')}")
    print()

    # worst time-drift samples
    if pairs:
        print("── 12 WORST time-drift pairs ─────────────────────────────────")
        for l, b, d in sorted(pairs, key=lambda p: -abs(p[2]))[:12]:
            print(f"    {l['entry_time']:%m-%d %H:%M:%S} {l['direction']:4} "
                  f"live@{l['entry_price']} vs bt {b['entry_time']:%H:%M:%S}@{b['entry_price']}  "
                  f"drift {d:+6.0f}s  livePnL {l['pnl']:+6.1f}")
        print()


def main():
    bt_trades, preset_name, (bt_start, bt_end), params = run_backtest()
    live_all = load_live_signals()

    print(f"preset      : {preset_name}  (sessions={params.tr_allowed_sessions})")
    print(f"bt data span: {bt_start:%Y-%m-%d %H:%M} → {bt_end:%Y-%m-%d %H:%M}  (UTC)")
    print(f"match       : same direction, same level (±{MATCH_PX_TICKS} ticks), "
          f"entry within {MATCH_WIN_MIN} min")
    print()

    # FULL history (clipped to bt data coverage), live copies merged
    live = [l for l in live_all if bt_start <= l["entry_time"] <= bt_end]
    live, (raw, mrg) = merge_close_signals(live)
    print(f"live merge   : {raw} raw fills → {mrg} signals "
          f"(merged copies within {MERGE_DT_S}s / {MERGE_PX_TICKS} ticks)")
    print()
    bt = _to_bt_dicts(bt_trades, bt_start, bt_end)
    analyse(live, bt, f"FULL HISTORY  {bt_start:%Y-%m-%d} → {bt_end:%Y-%m-%d}")

    # POST-FIX regime only
    from datetime import datetime, timezone
    since = datetime.fromisoformat(SINCE_DATE).replace(tzinfo=timezone.utc)
    lo = max(since, bt_start)
    live_s = [l for l in live if l["entry_time"] >= lo]
    bt_s = [b for b in bt if b["entry_time"] >= lo]
    analyse(live_s, bt_s, f"POST-FIX REGIME  since {SINCE_DATE}")


if __name__ == "__main__":
    main()
