"""Confluence sweep using subprocess for each combo to avoid OOM.

Each combo runs in a fresh Python process that loads data, runs one backtest,
prints the result, and exits — freeing all memory.
"""

import csv
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "machinelearning"
CSV_OUT = OUT_DIR / "confluence_claude_sweep.csv"
PRESETS_OUT = ROOT / "data" / "confluence_claude_presets.json"

FIELDNAMES = [
    "rr", "max_risk", "min_prob", "band", "min_tf",
    "sessions", "trail_trigger", "trail_lock", "full_tp_lock",
    "trades", "win_rate", "pnl", "pf", "max_dd", "calmar",
]

WORKER = r'''
import sys, pickle, json
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from backend.backtest.confluence_backtest import ConfluenceBacktester, ConfluenceBacktestConfig
from backend.db.models import BacktestConfig, get_tick_size
from backend.strategy.confluence import ConfluenceConfig
from backend.strategy.confluence_scorer import resolve_scorer
from backend.strategy.consolidation import timeframes_for_base

p = json.loads(sys.argv[2])
candles = sorted(pickle.loads(Path(sys.argv[1], 'data/store/MNQ_accumulated_1m.pkl').read_bytes()), key=lambda c: c.timestamp)
tick = get_tick_size('CON.F.US.MNQ.M26')
tf = timeframes_for_base(1)
scorer = resolve_scorer(True, None)
import glob as _g
cache_files = sorted(_g.glob(str(Path(sys.argv[1], 'data/machinelearning/confluence*timeline*.pkl'))))
if not cache_files:
    from backend.backtest.confluence_backtest import build_zone_timeline
    from backend.strategy.confluence import MAX_RECENCY_DEPTH
    timeline = build_zone_timeline(candles, tf, tick, MAX_RECENCY_DEPTH)
else:
    obj = pickle.loads(Path(cache_files[-1]).read_bytes())
    timeline = obj['timeline']
    # pad to current candle count if shorter
    if len(timeline) < len(candles):
        timeline.extend([timeline[-1]] * (len(candles) - len(timeline)))

s = ConfluenceConfig(band_ticks=p['band'], min_distinct_tf=p['min_tf'], rr=p['rr'])
s.direction_mode = 'auto'
s.tick_size = tick
s.ev_floor = None
s.rr_grid = None
s.enable_breakout = False
s.max_risk_ticks = p['max_risk']

import math
min_score = -999.0
if p['min_prob'] and 0 < p['min_prob'] < 1:
    min_score = math.log(p['min_prob'] / (1 - p['min_prob']))

r = ConfluenceBacktestConfig(
    wait_minutes=1, min_score=min_score, base_minutes=1,
    timeframes=tf, one_trade_per_session_direction=True,
    trail_trigger_pct=p['trail_trigger'], trail_lock_pct=p['trail_lock'],
    full_tp_lock=p['full_tp_lock'],
    allowed_sessions=tuple(p['sessions']),
)
b = BacktestConfig(initial_capital=50000.0, symbol='MNQ', commission_rt=1.0, fees_rt=2.8)
bt = ConfluenceBacktester(
    signal_cfg=s, run_cfg=r, contract_id='CON.F.US.MNQ.M26',
    contract_size=1, bt_config=b, scorer=scorer,
)
result = bt.run(candles, zones_timeline=timeline)
m = result.metrics
print(json.dumps({
    'trades': m.total_trades, 'win_rate': round(m.win_rate, 6),
    'pnl': round(m.total_pnl, 2), 'pf': round(m.profit_factor, 4),
    'max_dd': round(m.max_drawdown, 2), 'calmar': round(m.calmar_ratio, 4),
}))
'''


def run_one(params: dict) -> dict:
    """Run one combo in a subprocess."""
    cmd = [sys.executable, "-c", WORKER, str(ROOT), json.dumps(params)]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=180,
            cwd=str(ROOT),
        )
        if result.returncode != 0:
            return None
        line = result.stdout.strip().split("\n")[-1]
        metrics = json.loads(line)
        return {**params, "sessions": "+".join(params["sessions"]), **metrics}
    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
        print(f"  ERROR: {e}", flush=True)
        return None


def ensure_timeline_cache():
    """Build timeline cache if not present."""
    cache = OUT_DIR / "confluence_claude_timeline.pkl"
    if cache.exists():
        print(f"Timeline cache exists: {cache.stat().st_size / 1024**2:.0f} MB", flush=True)
        return
    print("Building timeline cache (one-time)...", flush=True)
    import pickle as _pkl
    sys.path.insert(0, str(ROOT))
    from backend.backtest.confluence_backtest import build_zone_timeline
    from backend.db.models import get_tick_size as _gts
    from backend.strategy.confluence import MAX_RECENCY_DEPTH as _mrd
    from backend.strategy.consolidation import timeframes_for_base as _tfb
    candles = sorted(_pkl.loads((ROOT / "data" / "store" / "MNQ_accumulated_1m.pkl").read_bytes()),
                     key=lambda c: c.timestamp)
    tick = _gts("CON.F.US.MNQ.M26")
    tf = _tfb(1)
    timeline = build_zone_timeline(candles, tf, tick, _mrd)
    meta = {
        "n": len(candles),
        "first": candles[0].timestamp.isoformat(),
        "last": candles[-1].timestamp.isoformat(),
    }
    import pickle
    cache.write_bytes(pickle.dumps({"meta": meta, "timeline": timeline},
                                   protocol=pickle.HIGHEST_PROTOCOL))
    print(f"Timeline cached: {cache.stat().st_size / 1024**2:.0f} MB", flush=True)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # Skip timeline rebuild — use existing 06/24 cache (worker loads it directly)

    combos = []

    # Phase 1: RR x MaxRisk (broad)
    for rr in [1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0, 3.5, 4.0]:
        for risk in [30, 40, 50, 60, 70, 80, 90, 100]:
            combos.append(dict(
                rr=rr, max_risk=risk, min_prob=0.0,
                band=4, min_tf=2, sessions=["ASIA"],
                trail_trigger=0.50, trail_lock=0.05, full_tp_lock=0,
            ))

    # Phase 2: Sessions + Trail + Prob around sweet spot
    for rr in [1.25, 1.5, 1.75, 2.0, 2.5]:
        for risk in [40, 60, 80]:
            for sessions in [["ASIA"], ["ASIA", "EURO"], ["ASIA", "PRE"]]:
                for trail_trigger, trail_lock in [(0.0, 0.0), (0.50, 0.05)]:
                    for min_prob in [0.0, 0.60]:
                        combos.append(dict(
                            rr=rr, max_risk=risk, min_prob=min_prob,
                            band=4, min_tf=2, sessions=sessions,
                            trail_trigger=trail_trigger, trail_lock=trail_lock,
                            full_tp_lock=0,
                        ))

    # Phase 3: Band + TF
    for rr in [1.5, 1.75, 2.0]:
        for risk in [50, 70, 90]:
            for band in [2, 4, 6, 8]:
                for min_tf in [2, 3]:
                    combos.append(dict(
                        rr=rr, max_risk=risk, min_prob=0.0,
                        band=band, min_tf=min_tf, sessions=["ASIA"],
                        trail_trigger=0.50, trail_lock=0.05, full_tp_lock=0,
                    ))

    total = len(combos)
    print(f"Total combos: {total}", flush=True)

    rows = []
    with CSV_OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader()

    t0 = time.perf_counter()
    for idx, params in enumerate(combos, 1):
        result = run_one(params)
        if result and result.get("trades", 0) >= 5:
            rows.append(result)
            with CSV_OUT.open("a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=FIELDNAMES)
                w.writerow({k: result.get(k, "") for k in FIELDNAMES})

        elapsed = time.perf_counter() - t0
        rate = idx / elapsed if elapsed > 0 else 0
        eta = (total - idx) / rate if rate > 0 else 0

        if result:
            r = result
            pnl_s = f"${r['pnl']:>8,.0f}" if r['pnl'] else "$     0"
            print(
                f"{idx:>4}/{total} ({eta/60:>4.0f}m left) "
                f"RR={params['rr']:>4} R={params['max_risk']:>3} "
                f"N={r.get('trades',0):>4} WR={r.get('win_rate',0)*100:>5.1f}% "
                f"PnL={pnl_s} DD=${r.get('max_dd',0):>7,.0f} "
                f"Cal={r.get('calmar',0):>6.2f}",
                flush=True,
            )
        else:
            print(f"{idx:>4}/{total} ({eta/60:>4.0f}m left) SKIPPED", flush=True)

    # ── CLAUDE #1-5 ──
    profitable = [r for r in rows if r["pnl"] > 0 and r["trades"] >= 10]
    picks = {}

    c1 = [r for r in profitable if r["max_dd"] < 2000]
    c1.sort(key=lambda r: r["calmar"], reverse=True)
    if c1: picks["#1 Best Calmar"] = c1[0]

    c2 = sorted(profitable, key=lambda r: r["pnl"], reverse=True)
    if c2: picks["#2 Highest PnL"] = c2[0]

    c3 = [r for r in profitable if r["trades"] >= 20]
    c3.sort(key=lambda r: r["max_dd"])
    if c3: picks["#3 Lowest DD"] = c3[0]

    c4 = [r for r in profitable if r["trades"] >= 30]
    c4.sort(key=lambda r: r["win_rate"], reverse=True)
    if c4: picks["#4 Best WinRate"] = c4[0]

    c5 = [r for r in profitable if r["trades"] >= 30]
    c5.sort(key=lambda r: r["pf"], reverse=True)
    if c5: picks["#5 Best PF"] = c5[0]

    print("\n" + "=" * 60, flush=True)
    print(" CLAUDE PRESETS #1-5", flush=True)
    print("=" * 60, flush=True)
    for label, b in picks.items():
        tr = f"Trail{int(b['trail_trigger']*100)}/{int(b['trail_lock']*100)}" if b['trail_trigger'] else "NoTrail"
        print(f"\n  {label}:", flush=True)
        print(f"    RR={b['rr']} Risk={b['max_risk']} Band={b['band']} TF={b['min_tf']} "
              f"Sess={b['sessions']} {tr}", flush=True)
        print(f"    {b['trades']} trades | {b['win_rate']*100:.1f}% WR | "
              f"${b['pnl']:,.0f} PnL | PF={b['pf']:.2f} | "
              f"DD=${b['max_dd']:,.0f} | Calmar={b['calmar']:.2f}", flush=True)

    PRESETS_OUT.write_text(json.dumps(picks, indent=2, default=str), encoding="utf-8")
    print(f"\nSaved {len(rows)} results + CLAUDE presets", flush=True)


if __name__ == "__main__":
    main()
