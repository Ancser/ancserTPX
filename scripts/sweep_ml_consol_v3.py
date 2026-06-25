"""ML Consolidation V2 sweep V3: fixed SL modes + tighter risk caps.

V2 finding: SL at 100% range is too loose — risk can be 40-80 ticks.
V3 changes:
  - sl_mode="va" (SL at VA edge, not 100% range) as primary
  - sl_mode="range" kept for comparison
  - max_risk_ticks sweep [20, 30, 40] (tighter than V2's 80)
  - Sessions: ASIA, ASIA+EURO
  - Lookbacks: 30, 60, 120
  - RR: 1.5, 2.0, 3.0, 4.0
  - Band: 2, 4, 6
  - SL buffer: 2, 4, 8
  - Trail: on/off
  - Size: 1 only (risk-adjusted comparison)
  - one_trade_per_session=False
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


def run_one(candles, vp_tl, lb, band, sl_buf, sl_mode, max_risk, sessions, trail, tp_mode, rr):
    sig_cfg = MLTrendConfig(
        lookback=lb, band_ticks=band, sl_buffer_ticks=sl_buf,
        sl_mode=sl_mode, tick_size=TICK, tp_mode=tp_mode, rr=rr,
        max_risk_ticks=max_risk, min_risk_ticks=4.0,
    )
    run_cfg = MLTrendBacktestConfig(
        trail_trigger_pct=0.50 if trail else 0.0,
        trail_lock_pct=0.05 if trail else 0.0,
        one_trade_per_session=False,
        allowed_sessions=sessions,
    )
    bt_cfg = BacktestConfig(initial_capital=50000.0, symbol="MNQ",
                            commission_rt=1.0, fees_rt=2.8)
    bt = MLTrendBacktester(
        signal_cfg=sig_cfg, run_cfg=run_cfg,
        contract_id=CONTRACT_ID, contract_size=1, bt_config=bt_cfg,
    )
    result = bt.run(candles, vp_timeline=vp_tl)
    m = result.metrics
    if m.total_trades < 10:
        return None
    return {
        "lb": lb, "band": band, "sl_buf": sl_buf, "sl_mode": sl_mode,
        "max_risk": max_risk,
        "sess": "+".join(sessions), "trail": trail,
        "tp": tp_mode, "rr": rr,
        "trades": m.total_trades, "wr": m.win_rate,
        "pnl": m.total_pnl, "pf": m.profit_factor,
        "dd": m.max_drawdown, "calmar": m.calmar_ratio,
        "avg_win": m.avg_win, "avg_loss": m.avg_loss,
    }


def print_table(title, results, limit=30):
    print(f"\n{title}")
    hdr = (f"{'LB':>3} {'Bd':>2} {'SB':>2} {'SLM':>3} {'MxR':>3} {'Sessions':<12} {'Tr':>2} "
           f"{'TP':>3} {'RR':>3} {'Trd':>5} {'Win%':>6} "
           f"{'PnL':>10} {'PF':>6} {'MaxDD':>8} {'Calmar':>7}")
    print(hdr)
    print("-" * 115)
    for r in results[:limit]:
        tr = "Y" if r["trail"] else "N"
        print(f"{r['lb']:>3} {r['band']:>2} {r['sl_buf']:>2} {r['sl_mode']:>3} {r['max_risk']:>3} "
              f"{r['sess']:<12} {tr:>2} "
              f"{r['tp']:>3} {r['rr']:>3.1f} {r['trades']:>5} {r['wr']*100:>5.1f}% "
              f"${r['pnl']:>9,.0f} {r['pf']:>5.2f} ${r['dd']:>7,.0f} {r['calmar']:>6.2f}")


def main():
    candles = load_candles()

    lookbacks = [30, 60, 120]
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
            for sl_buf in [2, 4, 8]:
                for sl_mode in ["va", "range"]:
                    for max_risk in [20, 30, 40]:
                        for sessions in [("ASIA",), ("ASIA", "EURO")]:
                            for trail in [True, False]:
                                # Fixed RR modes
                                for rr in [1.5, 2.0, 3.0, 4.0]:
                                    configs.append((lb, band, sl_buf, sl_mode, max_risk,
                                                    sessions, trail, "rr", rr))
                                # POC mode
                                configs.append((lb, band, sl_buf, sl_mode, max_risk,
                                                sessions, trail, "poc", 0))

    print(f"\nSweeping {len(configs)} configs...")
    results = []
    t0 = _time.perf_counter()
    for idx, (lb, band, sl_buf, sl_mode, max_risk, sess, trail, tp, rr) in enumerate(configs):
        r = run_one(candles, timelines[lb], lb, band, sl_buf, sl_mode, max_risk,
                    sess, trail, tp, rr)
        if r:
            results.append(r)
        if (idx + 1) % 200 == 0:
            sys.stdout.write(f"\r  {idx+1}/{len(configs)} ({len(results)} valid)...")
            sys.stdout.flush()
    el = _time.perf_counter() - t0
    print(f"\r  Done: {len(configs)} -> {len(results)} valid ({el:.0f}s)")

    # ── Reports ──

    # Filter profitable configs
    profitable = [r for r in results if r["pnl"] > 0]

    # 1) Best by Calmar (DD < $3k)
    good = [r for r in profitable if r["dd"] < 3000]
    good.sort(key=lambda r: r["calmar"], reverse=True)
    print_table(f"=== Top Calmar (DD < $3k) -- {len(good)} configs ===", good, 30)

    # 2) Low DD < $1.5k
    low_dd = [r for r in profitable if r["dd"] < 1500]
    low_dd.sort(key=lambda r: r["calmar"], reverse=True)
    print_table(f"\n=== Low DD < $1.5k -- {len(low_dd)} configs ===", low_dd, 25)

    # 3) Highest PnL (min 50 trades)
    by_pnl = [r for r in profitable if r["trades"] >= 50]
    by_pnl.sort(key=lambda r: r["pnl"], reverse=True)
    print_table(f"\n=== Top PnL (>=50 trades) -- {len(by_pnl)} configs ===", by_pnl, 20)

    # 4) Highest Win Rate (min 50 trades)
    by_wr = [r for r in profitable if r["trades"] >= 50]
    by_wr.sort(key=lambda r: r["wr"], reverse=True)
    print_table(f"\n=== Top Win Rate (>=50 trades) -- {len(by_wr)} configs ===", by_wr, 20)

    # 5) VA SL mode only (the fix)
    va_good = [r for r in good if r["sl_mode"] == "va"]
    va_good.sort(key=lambda r: r["calmar"], reverse=True)
    print_table(f"\n=== VA SL Mode Best (DD < $3k) -- {len(va_good)} configs ===", va_good, 20)

    # 6) Range SL mode (old, for comparison)
    range_good = [r for r in good if r["sl_mode"] == "range"]
    range_good.sort(key=lambda r: r["calmar"], reverse=True)
    print_table(f"\n=== Range SL Mode Best (DD < $3k) -- {len(range_good)} configs ===", range_good, 10)

    # ── Pick CLAUDE #1-5 presets ──
    # Criteria:
    #   #1: Highest Calmar (best risk-adjusted), DD < $2k
    #   #2: Highest PnL (most profit), trades >= 50
    #   #3: Lowest DD (safest), PnL > 0
    #   #4: Highest Win Rate, trades >= 50
    #   #5: Best PF (profit factor), trades >= 30
    print("\n" + "=" * 60)
    print(" CLAUDE PRESETS #1-5")
    print("=" * 60)

    picks = {}

    # #1: Best Calmar, DD < $2k
    c1 = [r for r in profitable if r["dd"] < 2000]
    c1.sort(key=lambda r: r["calmar"], reverse=True)
    if c1:
        picks["#1 Best Calmar"] = c1[0]

    # #2: Highest PnL, trades >= 50
    c2 = [r for r in profitable if r["trades"] >= 50]
    c2.sort(key=lambda r: r["pnl"], reverse=True)
    if c2:
        picks["#2 Highest PnL"] = c2[0]

    # #3: Lowest DD, PnL > 0, trades >= 20
    c3 = [r for r in profitable if r["trades"] >= 20]
    c3.sort(key=lambda r: r["dd"])
    if c3:
        picks["#3 Lowest DD"] = c3[0]

    # #4: Highest Win Rate, trades >= 50
    c4 = [r for r in profitable if r["trades"] >= 50]
    c4.sort(key=lambda r: r["wr"], reverse=True)
    if c4:
        picks["#4 Best WinRate"] = c4[0]

    # #5: Best PF, trades >= 30
    c5 = [r for r in profitable if r["trades"] >= 30]
    c5.sort(key=lambda r: r["pf"], reverse=True)
    if c5:
        picks["#5 Best PF"] = c5[0]

    for label, b in picks.items():
        tr = "Trail50/5" if b["trail"] else "NoTrail"
        print(f"\n  {label}:")
        print(f"    LB={b['lb']} Band={b['band']} SLB={b['sl_buf']} SLM={b['sl_mode']} "
              f"MxR={b['max_risk']} TP={b['tp']} RR={b['rr']:.1f} "
              f"Sess={b['sess']} {tr}")
        print(f"    {b['trades']} trades | {b['wr']*100:.1f}% WR | "
              f"${b['pnl']:,.0f} PnL | PF={b['pf']:.2f} | "
              f"DD=${b['dd']:,.0f} | Calmar={b['calmar']:.2f}")

    # Save
    out = ROOT / "data" / "ml_consol_v3_sweep_results.json"
    out.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\nSaved {len(results)} results to {out}")

    # Save picks
    picks_out = ROOT / "data" / "ml_consol_v3_claude_presets.json"
    picks_out.write_text(json.dumps(picks, indent=2, default=str), encoding="utf-8")
    print(f"Saved CLAUDE presets to {picks_out}")


if __name__ == "__main__":
    main()
