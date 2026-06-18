# ============================================================
# 文件: scripts/run_combination_sweep.py
# 用途: Standalone runner for the confluence COMBINATION sweep. Confluence is
#       pure-Python + CPU-bound, so a single combo over 60d/1m (~57k candles)
#       costs ~220s and ThreadPoolExecutor is GIL-bound (no speedup). This runner
#       therefore uses a PROCESS pool (true parallelism) with a per-worker
#       initializer that rebuilds the (read-only) zone timeline once, and STREAMS
#       each result to data/machinelearning/combination_partial.jsonl as it
#       finishes — so partial results are always available on disk.
# 執行:
#   python -m scripts.run_combination_sweep            # DIAGNOSTIC 16-combo grid (~minutes)
#   python -m scripts.run_combination_sweep full       # FULL 240-combo grid (long)
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

# ── HELD constants (= ML CONFLUENCE panel defaults / all-off baseline) ──
PKL = Path("data/historical/CON_F_US_MNQ_M26_1m_60d_20260615.pkl")
CONTRACT_ID = "CON.F.US.MNQ.M26"
CONTRACT_SIZE = 3
BAND_TICKS = 8.0
MIN_DISTINCT_TF = 3
MIN_PROB = 0.0
EV_FLOOR = None
WAIT_MINUTES = 60
TRAIL_LOCK_PCT = 0.0
BASE_MINUTES = 1
INITIAL_CAPITAL = 50000.0

OUT_DIR = Path("data/machinelearning")
PARTIAL = OUT_DIR / "combination_partial.jsonl"

# DIAGNOSTIC grid — the suspects behind low win-rate: breakout on/off × RR × a
# break-even trail on/off. 2 × 4 × 2 = 16 combos (one parallel wave on 16 cores).
DIAG_RR = (1.5, 2.0, 2.5, 3.0)
DIAG_BREAKOUT = (True, False)
DIAG_TRAIL_TRIGGER = (0.0, 0.50)
DIAG_FULL_TP_LOCK = (0,)
DIAG_SESSION = (True,)

# ── per-worker globals (populated by the initializer in each spawned process) ──
_W: dict = {}


def _init_worker():
    """Runs ONCE per worker process: load candles + rebuild the zone timeline."""
    from backend.api.routes import (
        get_tick_size, get_commission_rt, get_fees_rt,
        _extract_symbol, _normalize_contract_size,
    )
    from backend.strategy.confluence_scorer import resolve_scorer
    from backend.strategy.consolidation import timeframes_for_base
    from backend.strategy.confluence import MAX_RECENCY_DEPTH
    from backend.backtest.confluence_backtest import build_zone_timeline

    with PKL.open("rb") as f:
        candles = sorted(pickle.load(f), key=lambda c: c.timestamp)
    tick = get_tick_size(CONTRACT_ID)
    tfs = timeframes_for_base(BASE_MINUTES)
    _W["candles"] = candles
    _W["timeline"] = build_zone_timeline(candles, tfs, tick, MAX_RECENCY_DEPTH)
    _W["tick"] = tick
    _W["tfs"] = tfs
    _W["scorer"] = resolve_scorer(True, None)
    _W["cs"] = _normalize_contract_size(CONTRACT_ID, CONTRACT_SIZE)
    _W["btk"] = dict(
        initial_capital=INITIAL_CAPITAL, symbol=_extract_symbol(CONTRACT_ID),
        commission_rt=get_commission_rt(CONTRACT_ID), fees_rt=get_fees_rt(CONTRACT_ID),
    )


def _combo_task(args):
    from backend.api.routes import _run_conf_combo
    rr, brk, trig, ftl, ses = args
    return _run_conf_combo(
        _W["candles"], _W["timeline"], _W["scorer"], _W["tick"], BASE_MINUTES, _W["tfs"],
        BAND_TICKS, MIN_DISTINCT_TF, MIN_PROB, EV_FLOOR, WAIT_MINUTES,
        TRAIL_LOCK_PCT, CONTRACT_ID, _W["cs"], _W["btk"],
        rr, brk, trig, ftl, ses,
    )


def _fmt_row(r: dict) -> str:
    if r.get("error"):
        return f"  ERROR rr={r.get('rr_ratio')} : {r['error'][:70]}"
    return (f"  rr=1:{r['rr_ratio']:<3g} brk={'on ' if r['conf_enable_breakout'] else 'off'} "
            f"trig={int(round(r['conf_trail_trigger_pct']*100)):>2}% ftl={r['conf_full_tp_lock']} "
            f"ses={'on' if r['conf_session_limit'] else 'off'} | "
            f"trades={r['total_trades']:>3} win={r['win_rate']*100:>5.1f}% "
            f"pnl=${r['total_pnl']:>8.0f} dd=${r['max_drawdown']:>7.0f} "
            f"pf={r['profit_factor']:>5.2f} calmar={r['calmar_ratio']:>6.2f}")


def main() -> None:
    full = len(sys.argv) > 1 and sys.argv[1].lower() == "full"
    if full:
        from backend.api.routes import (
            CONF_COMBO_RR, CONF_COMBO_BREAKOUT, CONF_COMBO_TRAIL_TRIGGER,
            CONF_COMBO_FULL_TP_LOCK, CONF_COMBO_SESSION,
        )
        # RR 1:2 is the proven "dead zone" (every 1:2 combo lost $6.5-9.2k in the
        # diagnostic) — exclude it so the full grid spends its time on viable RRs.
        rr_grid = tuple(r for r in CONF_COMBO_RR if r != 2.0)
        combos = list(product(rr_grid, CONF_COMBO_BREAKOUT,
                              CONF_COMBO_TRAIL_TRIGGER, CONF_COMBO_FULL_TP_LOCK,
                              CONF_COMBO_SESSION))
        label = "FULL (no RR1:2)"
    else:
        combos = list(product(DIAG_RR, DIAG_BREAKOUT, DIAG_TRAIL_TRIGGER,
                              DIAG_FULL_TP_LOCK, DIAG_SESSION))
        label = "DIAGNOSTIC"

    workers = min(len(combos), max(1, (os.cpu_count() or 4) - 2))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PARTIAL.write_text("", encoding="utf-8")  # truncate

    t_start = time.perf_counter()
    print(f"[Combination] {label} grid: {len(combos)} combos, {workers} worker "
          f"processes (each builds the timeline once; ~40s startup)", flush=True)
    print(f"[Combination] streaming partials -> {PARTIAL}", flush=True)

    results = []
    done = 0
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker) as ex:
        fut_map = {ex.submit(_combo_task, c): c for c in combos}
        with PARTIAL.open("a", encoding="utf-8") as pf:
            for fut in as_completed(fut_map):
                r = fut.result()
                results.append(r)
                done += 1
                pf.write(json.dumps(r, default=str) + "\n")
                pf.flush()
                print(f"[{done:>3}/{len(combos)}] {_fmt_row(r)}  "
                      f"({time.perf_counter() - t_start:.0f}s)", flush=True)

    ok = [r for r in results if not r.get("error")]
    ranked = sorted(ok, key=lambda r: (r.get("calmar_ratio", 0) or 0,
                                       r.get("total_pnl", 0) or 0), reverse=True)
    print("\n" + "=" * 96)
    print(f"{label} SWEEP — {len(ranked)}/{len(combos)} OK, ranked by Calmar "
          f"(held: band={BAND_TICKS} min_tf={MIN_DISTINCT_TF} min_prob={MIN_PROB})")
    print("=" * 96)
    for i, r in enumerate(ranked, 1):
        print(f"{i:>2}. {_fmt_row(r).strip()}")
    print(f"\nTOTAL wall time: {time.perf_counter() - t_start:.0f}s "
          f"({(time.perf_counter() - t_start) / 60:.1f} min)")


if __name__ == "__main__":
    main()
