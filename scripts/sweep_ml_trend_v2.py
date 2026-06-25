"""ML Trend V2 sweep: relaxed session limit + fixed RR + POC modes.

V1 finding: one_trade_per_session caps at ~49 trades, too thin.
V2 changes:
  - one_trade_per_session=False (allow multiple trades per day)
  - Add fixed RR mode (tp_mode="rr") with RR sweep
  - Shorter lookbacks (30, 60, 120) for tighter consolidations
"""

import json
import math
import pickle
import sys
import time as _time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.db.models import get_tick_size, BacktestConfig
from backend.strategy.ml_trend import MLTrendConfig
from backend.backtest.ml_trend_backtest import (
    MLTrendBacktester, MLTrendBacktestConfig, precompute_vp_timeline,
)

TICK = get_tick_size("CON.F.US.MNQ.M26")
CONTRACT_ID = "CON.F.US.MNQ.M26"


def load_candles():
    store = ROOT / "data" / "store" / "MNQ_accumulated_1m.pkl"
    candles = sorted(pickle.loads(store.read_bytes()), key=lambda c: c.timestamp)
    print(f"Loaded {len(candles)} candles.")
    return candles


def run_one(candles, vp_tl, lb, band, sl_buf, sessions, trail, sz, tp_mode, rr, per_session):
    sig_cfg = MLTrendConfig(
        lookback=lb, band_ticks=band, sl_buffer_ticks=sl_buf,
        tick_size=TICK, tp_mode=tp_mode, rr=rr,
    )
    run_cfg = MLTrendBacktestConfig(
        trail_trigger_pct=0.50 if trail else 0.0,
        trail_lock_pct=0.05 if trail else 0.0,
        one_trade_per_session=per_session,
        allowed_sessions=sessions,
    )
    bt_cfg = BacktestConfig(initial_capital=50000.0, symbol="MNQ",
                            commission_rt=1.0, fees_rt=2.8)
    bt = MLTrendBacktester(
        signal_cfg=sig_cfg, run_cfg=run_cfg,
        contract_id=CONTRACT_ID, contract_size=sz, bt_config=bt_cfg,
    )
    result = bt.run(candles, vp_timeline=vp_tl)
    m = result.metrics
    if m.total_trades < 5:
        return None
    return {
        "lb": lb, "band": band, "sl_buf": sl_buf,
        "sess": "+".join(sessions), "trail": trail, "sz": sz,
        "tp": tp_mode, "rr": rr, "per_sess": per_session,
        "trades": m.total_trades, "wr": m.win_rate,
        "pnl": m.total_pnl, "pf": m.profit_factor,
        "dd": m.max_drawdown, "calmar": m.calmar_ratio,
        "avg_win": m.avg_win, "avg_loss": m.avg_loss,
    }


def print_table(title, results, limit=30):
    print(f"\n{title}")
    hdr = (f"{'LB':>3} {'Bd':>2} {'SB':>2} {'Sessions':<12} {'Tr':>2} {'Sz':>2} "
           f"{'TP':>3} {'RR':>3} {'1/S':>3} {'Trd':>4} {'Win%':>6} "
           f"{'PnL':>10} {'PF':>6} {'MaxDD':>8} {'Calmar':>7}")
    print(hdr)
    print("-" * 105)
    for r in results[:limit]:
        tr = "Y" if r["trail"] else "N"
        ps = "Y" if r["per_sess"] else "N"
        print(f"{r['lb']:>3} {r['band']:>2} {r['sl_buf']:>2} "
              f"{r['sess']:<12} {tr:>2} {r['sz']:>2} "
              f"{r['tp']:>3} {r['rr']:>3.1f} {ps:>3} {r['trades']:>4} {r['wr']*100:>5.1f}% "
              f"${r['pnl']:>9,.0f} {r['pf']:>5.2f} ${r['dd']:>7,.0f} {r['calmar']:>6.2f}")


def main():
    candles = load_candles()

    lookbacks = [30, 60, 120, 240]
    print(f"Pre-computing VP timelines for {lookbacks}...")
    timelines = {}
    for lb in lookbacks:
        t0 = _time.perf_counter()
        tl = precompute_vp_timeline(candles, lb, TICK, recalc_interval=5)
        el = _time.perf_counter() - t0
        print(f"  LB={lb:>3}: {el:.1f}s")
        timelines[lb] = tl

    # Build config grid
    configs = []
    for lb in lookbacks:
        for band in [2, 4, 6]:
            for sl_buf in [4, 8]:
                for sessions in [("ASIA",), ("ASIA", "EURO")]:
                    for trail in [True, False]:
                        for sz in [1, 3]:
                            # POC mode (original)
                            configs.append((lb, band, sl_buf, sessions, trail, sz, "poc", 0, False))
                            # Fixed RR modes
                            for rr in [1.5, 2.0, 3.0, 4.0]:
                                configs.append((lb, band, sl_buf, sessions, trail, sz, "rr", rr, False))

    print(f"\nSweeping {len(configs)} configs (no session limit)...")
    results = []
    t0 = _time.perf_counter()
    for idx, (lb, band, sl_buf, sess, trail, sz, tp, rr, ps) in enumerate(configs):
        r = run_one(candles, timelines[lb], lb, band, sl_buf, sess, trail, sz, tp, rr, ps)
        if r:
            results.append(r)
        if (idx + 1) % 100 == 0:
            sys.stdout.write(f"\r  {idx+1}/{len(configs)} ({len(results)} valid)...")
            sys.stdout.flush()
    el = _time.perf_counter() - t0
    print(f"\r  Done: {len(configs)} -> {len(results)} valid ({el:.0f}s)")

    # ── Reports ──

    # 1) Best by Calmar (DD < $3k)
    good = [r for r in results if r["pnl"] > 0 and r["dd"] < 3000]
    good.sort(key=lambda r: r["calmar"], reverse=True)
    print_table(f"=== Top Calmar (DD < $3k, PnL > 0) -- {len(good)} configs ===", good, 30)

    # 2) Low DD < $2k
    low_dd = [r for r in results if r["pnl"] > 0 and r["dd"] < 2000]
    low_dd.sort(key=lambda r: r["calmar"], reverse=True)
    print_table(f"\n=== Low DD < $2k -- {len(low_dd)} configs ===", low_dd, 25)

    # 3) Highest PnL
    by_pnl = [r for r in results if r["pnl"] > 0]
    by_pnl.sort(key=lambda r: r["pnl"], reverse=True)
    print_table(f"\n=== Top PnL -- {len(by_pnl)} configs ===", by_pnl, 20)

    # 4) Highest Win Rate (min 20 trades)
    by_wr = [r for r in results if r["pnl"] > 0 and r["trades"] >= 20]
    by_wr.sort(key=lambda r: r["wr"], reverse=True)
    print_table(f"\n=== Top Win Rate (>=20 trades) -- {len(by_wr)} configs ===", by_wr, 20)

    # 5) POC mode only
    poc_good = [r for r in good if r["tp"] == "poc"]
    poc_good.sort(key=lambda r: r["calmar"], reverse=True)
    print_table(f"\n=== POC Mode Best -- {len(poc_good)} configs ===", poc_good, 15)

    # 6) Fixed RR mode only
    rr_good = [r for r in good if r["tp"] == "rr"]
    rr_good.sort(key=lambda r: r["calmar"], reverse=True)
    print_table(f"\n=== Fixed RR Mode Best -- {len(rr_good)} configs ===", rr_good, 15)

    # Save
    out = ROOT / "data" / "ml_trend_sweep_v2_results.json"
    out.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\nSaved {len(results)} results to {out}")

    if good:
        b = good[0]
        print(f"\nBEST: LB={b['lb']} Band={b['band']} SLB={b['sl_buf']} "
              f"TP={b['tp']} RR={b['rr']:.1f} Sess={b['sess']} Trail={'Y' if b['trail'] else 'N'} "
              f"Sz={b['sz']}x | {b['trades']} trades {b['wr']*100:.1f}% WR "
              f"${b['pnl']:,.0f} PnL PF={b['pf']:.2f} DD=${b['dd']:,.0f} Calmar={b['calmar']:.2f}")


if __name__ == "__main__":
    main()
