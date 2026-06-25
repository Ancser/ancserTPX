"""Staged sweep for MARKET-entry confluence.

This is intentionally independent from the web/live process.  It uses the
current market-entry ConfluenceBacktester:

    signal at bar i -> market fill at bar i+1 open
    TP recalculated from actual market entry to structural SL

Phase 1 keeps the user's current structure fixed and sweeps RR + maxRisk:
band4, minTF2, ASIA, minProb OFF, EV OFF, Trail50/Lock5, session lock ON.

Outputs are checkpointed after every combo so a long run can be inspected or
resumed mentally without waiting for the whole search.
"""

from __future__ import annotations

import csv
import json
import math
import pickle
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.backtest.confluence_backtest import (  # noqa: E402
    ConfluenceBacktester,
    ConfluenceBacktestConfig,
    build_zone_timeline,
)
from backend.db.models import BacktestConfig, get_tick_size  # noqa: E402
from backend.strategy.confluence import ConfluenceConfig, MAX_RECENCY_DEPTH  # noqa: E402
from backend.strategy.confluence_scorer import resolve_scorer  # noqa: E402
from backend.strategy.consolidation import timeframes_for_base  # noqa: E402


STORE = ROOT / "data" / "store" / "MNQ_accumulated_1m.pkl"
OUT_DIR = ROOT / "data" / "machinelearning"
TIMELINE_CACHE = OUT_DIR / "confluence_market_timeline_20260624.pkl"
CSV_OUT = OUT_DIR / "confluence_market_staged_sweep_20260624.csv"
JSON_OUT = OUT_DIR / "confluence_market_staged_sweep_20260624.json"
MD_OUT = OUT_DIR / "confluence_market_staged_sweep_20260624.md"
LOG_OUT = OUT_DIR / "confluence_market_staged_sweep_20260624.log"

CONTRACT_ID = "CON.F.US.MNQ.M26"
SYMBOL = "MNQ"
INITIAL_CAPITAL = 50_000.0


FIELDNAMES = [
    "phase", "rr", "max_risk", "min_prob", "ev_floor", "band", "min_tf",
    "sessions", "trail_trigger", "trail_lock", "full_tp_lock", "size",
    "trades", "win_rate", "pnl", "pf", "max_dd", "calmar",
    "avg_win", "avg_loss", "max_consecutive_losses",
]


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with LOG_OUT.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


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
    log("Building zone timeline once...")
    t0 = time.perf_counter()
    timeline = build_zone_timeline(candles, timeframes, tick, MAX_RECENCY_DEPTH)
    TIMELINE_CACHE.write_bytes(pickle.dumps({"meta": meta, "timeline": timeline}, protocol=pickle.HIGHEST_PROTOCOL))
    log(f"Timeline built in {time.perf_counter() - t0:.1f}s and cached.")
    return timeline


def score_for_sort(r: Dict[str, Any]) -> Tuple[float, float, float]:
    # Prefer funded-usable configs: positive PnL, low DD, then Calmar.
    dd = float(r["max_dd"] or 0.0)
    pnl = float(r["pnl"] or 0.0)
    calmar = float(r["calmar"] or 0.0)
    funded_bonus = 1.0 if (pnl > 0 and dd <= 2000) else 0.0
    return (funded_bonus, calmar, pnl / max(dd, 1.0))


def min_score_from_prob(min_prob: float) -> float:
    if min_prob and 0.0 < min_prob < 1.0:
        return math.log(min_prob / (1.0 - min_prob))
    return -999.0


def run_combo(
    *,
    phase: str,
    candles,
    timeline,
    tick: float,
    scorer,
    timeframes,
    rr: float,
    max_risk: Optional[int],
    min_prob: float,
    ev_floor: Optional[float],
    band: int,
    min_tf: int,
    sessions: Tuple[str, ...],
    trail_trigger: float,
    trail_lock: float,
    full_tp_lock: int,
    size: int,
) -> Dict[str, Any]:
    sig_cfg = ConfluenceConfig(band_ticks=band, min_distinct_tf=min_tf, rr=rr)
    sig_cfg.direction_mode = "auto"
    sig_cfg.tick_size = tick
    sig_cfg.ev_floor = ev_floor
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
        initial_capital=INITIAL_CAPITAL,
        symbol=SYMBOL,
        commission_rt=1.0,
        fees_rt=2.8,
    )
    bt = ConfluenceBacktester(
        signal_cfg=sig_cfg,
        run_cfg=run_cfg,
        contract_id=CONTRACT_ID,
        contract_size=size,
        bt_config=bt_cfg,
        scorer=scorer,
    )
    result = bt.run(candles, zones_timeline=timeline)
    m = result.metrics
    return {
        "phase": phase,
        "rr": rr,
        "max_risk": max_risk if max_risk is not None else "OFF",
        "min_prob": min_prob if min_prob else "OFF",
        "ev_floor": ev_floor if ev_floor is not None else "OFF",
        "band": band,
        "min_tf": min_tf,
        "sessions": "+".join(sessions),
        "trail_trigger": trail_trigger,
        "trail_lock": trail_lock,
        "full_tp_lock": full_tp_lock,
        "size": size,
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


def write_reports(rows: Iterable[Dict[str, Any]]) -> None:
    rows = list(rows)
    JSON_OUT.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    positive = [r for r in rows if r["pnl"] > 0 and r["trades"] >= 10]
    funded = [r for r in positive if r["max_dd"] <= 2000]
    by_calmar = sorted(positive, key=lambda r: (r["calmar"], r["pnl"]), reverse=True)
    by_funded = sorted(funded, key=lambda r: (r["pnl"], r["calmar"]), reverse=True)
    by_dd = sorted(positive, key=lambda r: (r["max_dd"], -r["pnl"]))

    def table(items, n=20):
        lines = [
            "| RR | Risk | Prob | EV | Sess | Trail | FTL | N | Win | PnL | MaxDD | PF | Calmar |",
            "|---:|---:|---:|---:|:---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for r in items[:n]:
            lines.append(
                f"| {r['rr']} | {r['max_risk']} | {r['min_prob']} | {r['ev_floor']} | "
                f"{r['sessions']} | {r['trail_trigger']} | {r['full_tp_lock']} | "
                f"{r['trades']} | {100*r['win_rate']:.1f}% | ${r['pnl']:.0f} | "
                f"${r['max_dd']:.0f} | {r['pf']:.2f} | {r['calmar']:.2f} |"
            )
        return "\n".join(lines)

    md = [
        "# Confluence market staged sweep - 2026-06-24",
        "",
        "Market-entry replay: signal bar -> next bar open, TP recalculated from market entry to structural SL.",
        "",
        f"Rows: {len(rows)}",
        f"Positive rows: {len(positive)}",
        f"Positive + MaxDD <= $2k: {len(funded)}",
        "",
        "## Best funded candidates (DD <= $2k)",
        "",
        table(by_funded, 25),
        "",
        "## Top by Calmar",
        "",
        table(by_calmar, 25),
        "",
        "## Lowest DD positive rows",
        "",
        table(by_dd, 25),
        "",
    ]
    MD_OUT.write_text("\n".join(md), encoding="utf-8")


def main() -> None:
    if CSV_OUT.exists():
        CSV_OUT.unlink()
    if LOG_OUT.exists():
        LOG_OUT.unlink()
    append_csv({}, reset=True)

    candles = load_candles()
    tick = get_tick_size(CONTRACT_ID)
    timeframes = timeframes_for_base(1)
    scorer = resolve_scorer(True, None)
    timeline = load_or_build_timeline(candles, timeframes, tick)

    # Phase 1: current structure, sweep RR and max risk.
    rr_values = [1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0, 3.25, 3.5]
    max_risk_values = [40, 50, 60, 70, 80, 90, 100, 120, None]
    rows = []
    combos = []
    for rr in rr_values:
        for risk in max_risk_values:
            combos.append(dict(
                phase="phase1_rr_risk",
                rr=rr, max_risk=risk, min_prob=0.0, ev_floor=None,
                band=4, min_tf=2, sessions=("ASIA",),
                trail_trigger=0.50, trail_lock=0.05, full_tp_lock=0, size=3,
            ))

    # Phase 2: focused around the RR/risk region the user is testing.
    for rr in [1.75, 2.0, 2.25, 2.5, 2.75, 3.0]:
        for risk in [50, 60, 70, 80, 90, 100]:
            for min_prob in [0.0, 0.55, 0.60, 0.65, 0.70]:
                for ev_floor in [None, 0.0, 0.05]:
                    for sessions in [("ASIA",), ("PRE",), ("ASIA", "PRE")]:
                        for trail_trigger, trail_lock in [(0.0, 0.0), (0.30, 0.05), (0.50, 0.05)]:
                            for full_tp_lock in [0, 1]:
                                combos.append(dict(
                                    phase="phase2_filters",
                                    rr=rr, max_risk=risk, min_prob=min_prob, ev_floor=ev_floor,
                                    band=4, min_tf=2, sessions=sessions,
                                    trail_trigger=trail_trigger, trail_lock=trail_lock,
                                    full_tp_lock=full_tp_lock, size=3,
                                ))

    # Phase 3: stricter confluence geometry for the best RR/risk range.
    for rr in [2.0, 2.25, 2.5, 2.75]:
        for risk in [60, 70, 80, 90]:
            for band in [2, 4, 6, 8]:
                for min_tf in [2, 3]:
                    for sessions in [("ASIA",), ("ASIA", "PRE")]:
                        combos.append(dict(
                            phase="phase3_band_mtf",
                            rr=rr, max_risk=risk, min_prob=0.0, ev_floor=None,
                            band=band, min_tf=min_tf, sessions=sessions,
                            trail_trigger=0.50, trail_lock=0.05, full_tp_lock=0, size=3,
                        ))

    log(f"Running {len(combos):,} market confluence combos...")
    t0 = time.perf_counter()
    for idx, params in enumerate(combos, 1):
        row = run_combo(
            candles=candles, timeline=timeline, tick=tick, scorer=scorer,
            timeframes=timeframes, **params,
        )
        rows.append(row)
        append_csv(row)
        if idx % 25 == 0 or idx == len(combos):
            elapsed = time.perf_counter() - t0
            rate = idx / elapsed if elapsed > 0 else 0
            eta = (len(combos) - idx) / rate if rate > 0 else 0
            best = max(rows, key=score_for_sort)
            log(
                f"{idx:,}/{len(combos):,} ({rate:.2f}/s ETA {eta/60:.1f}m) "
                f"best_so_far RR={best['rr']} R={best['max_risk']} "
                f"N={best['trades']} PnL=${best['pnl']:.0f} DD=${best['max_dd']:.0f} "
                f"Calmar={best['calmar']:.2f}"
            )
            write_reports(rows)

    write_reports(rows)
    log(f"Done. CSV={CSV_OUT} MD={MD_OUT}")


if __name__ == "__main__":
    main()
