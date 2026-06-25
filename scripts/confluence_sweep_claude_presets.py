"""Comprehensive Confluence sweep -> CLAUDE #1-5 presets.

Based on 06/24 sweep findings: RR 1.25-3.0 region is profitable.
This sweep covers full parameter space with latest data (67k+ candles).

Phase 1: RR x MaxRisk (broad scan)
Phase 2: Best region + Sessions/Trail/Prob/FTL
Phase 3: Best region + Band/MinTF

Outputs CLAUDE #1-5 presets auto-selected by:
  #1: Best Calmar (DD < $2k)     -- risk-adjusted king
  #2: Highest PnL (>= 30 trades) -- profit maximizer
  #3: Lowest DD (PnL > 0)        -- safest
  #4: Highest Win Rate (>= 30)   -- consistency
  #5: Best PF (>= 30 trades)     -- edge quality
"""

import csv
import json
import math
import pickle
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.backtest.confluence_backtest import (
    ConfluenceBacktester, ConfluenceBacktestConfig, build_zone_timeline,
)
from backend.db.models import BacktestConfig, get_tick_size
from backend.strategy.confluence import ConfluenceConfig, MAX_RECENCY_DEPTH
from backend.strategy.confluence_scorer import resolve_scorer
from backend.strategy.consolidation import timeframes_for_base

STORE = ROOT / "data" / "store" / "MNQ_accumulated_1m.pkl"
OUT_DIR = ROOT / "data" / "machinelearning"
TIMELINE_CACHE = OUT_DIR / "confluence_claude_timeline.pkl"
CSV_OUT = OUT_DIR / "confluence_claude_sweep.csv"
JSON_OUT = OUT_DIR / "confluence_claude_sweep.json"
PRESETS_OUT = ROOT / "data" / "confluence_claude_presets.json"

CONTRACT_ID = "CON.F.US.MNQ.M26"
SYMBOL = "MNQ"
INITIAL_CAPITAL = 50_000.0

FIELDNAMES = [
    "phase", "rr", "max_risk", "min_prob", "band", "min_tf",
    "sessions", "trail_trigger", "trail_lock", "full_tp_lock", "size",
    "trades", "win_rate", "pnl", "pf", "max_dd", "calmar",
    "avg_win", "avg_loss", "max_consecutive_losses",
]


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def load_candles():
    candles = sorted(pickle.loads(STORE.read_bytes()), key=lambda c: c.timestamp)
    log(f"Loaded {len(candles):,} candles: {candles[0].timestamp} -> {candles[-1].timestamp}")
    return candles


def load_or_build_timeline(candles, timeframes, tick):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    meta = {
        "n": len(candles),
        "first": candles[0].timestamp.isoformat(),
        "last": candles[-1].timestamp.isoformat(),
        "timeframes": list(timeframes),
        "tick": tick,
        "depth": MAX_RECENCY_DEPTH,
    }
    if TIMELINE_CACHE.exists():
        try:
            obj = pickle.loads(TIMELINE_CACHE.read_bytes())
            if obj.get("meta") == meta and len(obj.get("timeline", [])) == len(candles):
                log(f"Using cached timeline: {TIMELINE_CACHE.name}")
                return obj["timeline"]
        except Exception as exc:
            log(f"Timeline cache unreadable, rebuilding: {exc}")
    log("Building zone timeline (one-time, ~10 min)...")
    t0 = time.perf_counter()
    timeline = build_zone_timeline(candles, timeframes, tick, MAX_RECENCY_DEPTH)
    TIMELINE_CACHE.write_bytes(pickle.dumps(
        {"meta": meta, "timeline": timeline}, protocol=pickle.HIGHEST_PROTOCOL,
    ))
    log(f"Timeline built in {time.perf_counter() - t0:.1f}s and cached.")
    return timeline


def min_score_from_prob(min_prob: float) -> float:
    if min_prob and 0.0 < min_prob < 1.0:
        return math.log(min_prob / (1.0 - min_prob))
    return -999.0


def run_combo(
    *, phase, candles, timeline, tick, scorer, timeframes,
    rr, max_risk, min_prob, band, min_tf, sessions,
    trail_trigger, trail_lock, full_tp_lock, size,
) -> Dict[str, Any]:
    sig_cfg = ConfluenceConfig(band_ticks=band, min_distinct_tf=min_tf, rr=rr)
    sig_cfg.direction_mode = "auto"
    sig_cfg.tick_size = tick
    sig_cfg.ev_floor = None
    sig_cfg.rr_grid = None
    sig_cfg.enable_breakout = False
    sig_cfg.max_risk_ticks = max_risk

    run_cfg = ConfluenceBacktestConfig(
        wait_minutes=1,
        min_score=min_score_from_prob(min_prob),
        base_minutes=1,
        timeframes=timeframes,
        one_trade_per_session_direction=True,
        trail_trigger_pct=trail_trigger,
        trail_lock_pct=trail_lock,
        full_tp_lock=full_tp_lock,
        allowed_sessions=sessions,
    )
    bt_cfg = BacktestConfig(
        initial_capital=INITIAL_CAPITAL, symbol=SYMBOL,
        commission_rt=1.0, fees_rt=2.8,
    )
    bt = ConfluenceBacktester(
        signal_cfg=sig_cfg, run_cfg=run_cfg,
        contract_id=CONTRACT_ID, contract_size=size,
        bt_config=bt_cfg, scorer=scorer,
    )
    result = bt.run(candles, zones_timeline=timeline)
    m = result.metrics
    return {
        "phase": phase,
        "rr": rr, "max_risk": max_risk if max_risk else "OFF",
        "min_prob": min_prob if min_prob else "OFF",
        "band": band, "min_tf": min_tf,
        "sessions": "+".join(sessions),
        "trail_trigger": trail_trigger, "trail_lock": trail_lock,
        "full_tp_lock": full_tp_lock, "size": size,
        "trades": m.total_trades,
        "win_rate": round(m.win_rate, 6),
        "pnl": round(m.total_pnl, 2),
        "pf": round(m.profit_factor, 4),
        "max_dd": round(m.max_drawdown, 2),
        "calmar": round(m.calmar_ratio, 4),
        "avg_win": round(m.avg_win, 2),
        "avg_loss": round(m.avg_loss, 2),
        "max_consecutive_losses": m.max_consecutive_losses,
    }


def append_csv(row: Dict[str, Any], *, reset: bool = False) -> None:
    write_header = reset or not CSV_OUT.exists()
    mode = "w" if reset else "a"
    with CSV_OUT.open(mode, newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if write_header:
            w.writeheader()
        if not reset:
            w.writerow(row)


def print_table(title: str, rows: List[Dict], limit: int = 20):
    print(f"\n{title}")
    hdr = (f"{'RR':>4} {'Risk':>4} {'Prob':>5} {'Bd':>2} {'TF':>2} "
           f"{'Sessions':<10} {'Tr':>4} {'FTL':>3} "
           f"{'Trd':>4} {'Win%':>6} {'PnL':>10} {'PF':>6} {'MaxDD':>8} {'Calmar':>7}")
    print(hdr)
    print("-" * 105)
    for r in rows[:limit]:
        tr = f"{r['trail_trigger']}" if r['trail_trigger'] else "OFF"
        print(f"{r['rr']:>4} {str(r['max_risk']):>4} {str(r['min_prob']):>5} "
              f"{r['band']:>2} {r['min_tf']:>2} "
              f"{r['sessions']:<10} {tr:>4} {r['full_tp_lock']:>3} "
              f"{r['trades']:>4} {r['win_rate']*100:>5.1f}% "
              f"${r['pnl']:>9,.0f} {r['pf']:>5.2f} ${r['max_dd']:>7,.0f} {r['calmar']:>6.2f}")


def main():
    if CSV_OUT.exists():
        CSV_OUT.unlink()
    append_csv({}, reset=True)

    candles = load_candles()
    tick = get_tick_size(CONTRACT_ID)
    timeframes = timeframes_for_base(1)
    scorer = resolve_scorer(True, None)
    timeline = load_or_build_timeline(candles, timeframes, tick)

    combos: List[dict] = []

    # ============================================================
    # Phase 1: Broad RR x MaxRisk scan
    # Fixed: Band=4, TF=2, ASIA, Trail50/5, FTL=0, Prob=OFF, Size=1
    # ============================================================
    for rr in [1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0, 3.5, 4.0]:
        for risk in [30, 40, 50, 60, 70, 80, 90, 100, 120]:
            combos.append(dict(
                phase="P1_rr_risk", rr=rr, max_risk=risk, min_prob=0.0,
                band=4, min_tf=2, sessions=("ASIA",),
                trail_trigger=0.50, trail_lock=0.05, full_tp_lock=0, size=1,
            ))

    # ============================================================
    # Phase 2: Session + Trail + Prob + FTL around sweet spot
    # RR 1.25-2.5, Risk 40-90
    # ============================================================
    for rr in [1.25, 1.5, 1.75, 2.0, 2.5]:
        for risk in [40, 50, 60, 70, 80, 90]:
            for sessions in [("ASIA",), ("ASIA", "EURO"), ("ASIA", "PRE")]:
                for trail_trigger, trail_lock in [(0.0, 0.0), (0.30, 0.05), (0.50, 0.05)]:
                    for min_prob in [0.0, 0.55, 0.60, 0.65]:
                        for ftp in [0, 1]:
                            combos.append(dict(
                                phase="P2_filters", rr=rr, max_risk=risk, min_prob=min_prob,
                                band=4, min_tf=2, sessions=sessions,
                                trail_trigger=trail_trigger, trail_lock=trail_lock,
                                full_tp_lock=ftp, size=1,
                            ))

    # ============================================================
    # Phase 3: Band + MinTF around sweet spot
    # RR 1.5-2.5, Risk 50-90
    # ============================================================
    for rr in [1.5, 1.75, 2.0, 2.5]:
        for risk in [50, 60, 70, 80, 90]:
            for band in [2, 4, 6, 8]:
                for min_tf in [2, 3]:
                    for sessions in [("ASIA",), ("ASIA", "EURO")]:
                        combos.append(dict(
                            phase="P3_band_tf", rr=rr, max_risk=risk, min_prob=0.0,
                            band=band, min_tf=min_tf, sessions=sessions,
                            trail_trigger=0.50, trail_lock=0.05, full_tp_lock=0, size=1,
                        ))

    log(f"Total combos: {len(combos):,}")
    sys.stdout.flush()

    rows: List[Dict] = []
    t0 = time.perf_counter()
    for idx, params in enumerate(combos, 1):
        try:
            row = run_combo(
                candles=candles, timeline=timeline, tick=tick,
                scorer=scorer, timeframes=timeframes, **params,
            )
        except Exception as exc:
            log(f"ERROR combo {idx}: {exc}")
            import traceback; traceback.print_exc()
            continue
        rows.append(row)
        append_csv(row)
        if idx % 50 == 0 or idx == len(combos):
            elapsed = time.perf_counter() - t0
            rate = idx / elapsed if elapsed > 0 else 0
            eta = (len(combos) - idx) / rate if rate > 0 else 0
            best = max(
                [r for r in rows if r["pnl"] > 0],
                key=lambda r: r["calmar"], default=rows[-1],
            )
            log(f"{idx:,}/{len(combos):,} ({rate:.1f}/s ETA {eta/60:.0f}m) "
                f"best: RR={best['rr']} R={best['max_risk']} "
                f"N={best['trades']} PnL=${best['pnl']:.0f} DD=${best['max_dd']:.0f} "
                f"Calmar={best['calmar']:.2f}")

    # ============================================================
    # Reports
    # ============================================================
    profitable = [r for r in rows if r["pnl"] > 0 and r["trades"] >= 10]

    # Top Calmar (DD < $2k)
    top_calmar = [r for r in profitable if r["max_dd"] < 2000]
    top_calmar.sort(key=lambda r: r["calmar"], reverse=True)
    print_table(f"=== Top Calmar (DD < $2k) -- {len(top_calmar)} ===", top_calmar, 25)

    # Top PnL
    top_pnl = sorted(profitable, key=lambda r: r["pnl"], reverse=True)
    print_table(f"\n=== Top PnL -- {len(top_pnl)} ===", top_pnl, 20)

    # Lowest DD
    low_dd = sorted(profitable, key=lambda r: r["max_dd"])
    print_table(f"\n=== Lowest DD ===", low_dd, 15)

    # Best WR (>= 30 trades)
    top_wr = [r for r in profitable if r["trades"] >= 30]
    top_wr.sort(key=lambda r: r["win_rate"], reverse=True)
    print_table(f"\n=== Top Win Rate (>=30 trades) ===", top_wr, 15)

    # Best PF (>= 30 trades)
    top_pf = [r for r in profitable if r["trades"] >= 30]
    top_pf.sort(key=lambda r: r["pf"], reverse=True)
    print_table(f"\n=== Top PF (>=30 trades) ===", top_pf, 15)

    # ============================================================
    # CLAUDE #1-5 auto-selection
    # ============================================================
    print("\n" + "=" * 60)
    print(" CLAUDE PRESETS #1-5")
    print("=" * 60)

    picks = {}

    # #1: Best Calmar, DD < $2k
    c1 = [r for r in profitable if r["max_dd"] < 2000]
    c1.sort(key=lambda r: r["calmar"], reverse=True)
    if c1:
        picks["#1 Best Calmar"] = c1[0]

    # #2: Highest PnL, trades >= 30
    c2 = [r for r in profitable if r["trades"] >= 30]
    c2.sort(key=lambda r: r["pnl"], reverse=True)
    if c2:
        picks["#2 Highest PnL"] = c2[0]

    # #3: Lowest DD, PnL > 0, trades >= 20
    c3 = [r for r in profitable if r["trades"] >= 20]
    c3.sort(key=lambda r: r["max_dd"])
    if c3:
        picks["#3 Lowest DD"] = c3[0]

    # #4: Highest Win Rate, trades >= 30
    c4 = [r for r in profitable if r["trades"] >= 30]
    c4.sort(key=lambda r: r["win_rate"], reverse=True)
    if c4:
        picks["#4 Best WinRate"] = c4[0]

    # #5: Best PF, trades >= 30
    c5 = [r for r in profitable if r["trades"] >= 30]
    c5.sort(key=lambda r: r["pf"], reverse=True)
    if c5:
        picks["#5 Best PF"] = c5[0]

    for label, b in picks.items():
        tr = f"Trail{int(b['trail_trigger']*100)}/{int(b['trail_lock']*100)}" if b['trail_trigger'] else "NoTrail"
        prob = f"P{b['min_prob']}" if b['min_prob'] != "OFF" and b['min_prob'] else "ProbOFF"
        print(f"\n  {label}:")
        print(f"    RR={b['rr']} Risk={b['max_risk']} {prob} "
              f"Band={b['band']} TF={b['min_tf']} "
              f"Sess={b['sessions']} {tr} FTL={b['full_tp_lock']}")
        print(f"    {b['trades']} trades | {b['win_rate']*100:.1f}% WR | "
              f"${b['pnl']:,.0f} PnL | PF={b['pf']:.2f} | "
              f"DD=${b['max_dd']:,.0f} | Calmar={b['calmar']:.2f}")

    # Save
    JSON_OUT.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    log(f"Saved {len(rows)} results to {JSON_OUT}")

    PRESETS_OUT.write_text(json.dumps(picks, indent=2, default=str), encoding="utf-8")
    log(f"Saved CLAUDE presets to {PRESETS_OUT}")


if __name__ == "__main__":
    main()
