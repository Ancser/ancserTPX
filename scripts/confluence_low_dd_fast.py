"""Focused sweep: find confluence configs with max DD < $2k.

Key insight: with 3xMNQ, avg loss is $500-800, so 3 consecutive losses = $1.5-2.4k.
Try 1xMNQ (divides all by 3) and higher prob gates to reduce loss frequency.
"""
import pickle, sys, math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.db.models import get_tick_size, BacktestConfig
from backend.strategy.consolidation import timeframes_for_base
from backend.strategy.confluence import ConfluenceConfig, MAX_RECENCY_DEPTH
from backend.strategy.confluence_scorer import resolve_scorer
from backend.backtest.confluence_backtest import (
    ConfluenceBacktester, ConfluenceBacktestConfig, build_zone_timeline,
)

store = ROOT / "data" / "store" / "MNQ_accumulated_1m.pkl"
candles = sorted(pickle.loads(store.read_bytes()), key=lambda c: c.timestamp)
tick = get_tick_size("CON.F.US.MNQ.M26")
base = 1
timeframes = timeframes_for_base(base)
scorer = resolve_scorer(True, None)
print("Building timeline...")
timeline = build_zone_timeline(candles, timeframes, tick, MAX_RECENCY_DEPTH)
print(f"Done. {len(candles)} candles.\n")

results = []

configs = []
for rr in [1.5, 2.0, 3.0, 4.0]:
    for mp in [0.65, 0.70, 0.75, 0.80, 0.85, 0.90]:
        for band in [4, 6, 8]:
            for mtf in [2, 3]:
                for sessions in [("ASIA",), ("ASIA","EURO"), ("ASIA","EURO","PRE","RTH")]:
                    for sz in [1, 3]:
                        configs.append((rr, mp, band, mtf, sessions, sz))

print(f"Configs: {len(configs)}")
for idx, (rr, mp, band, mtf, sessions, sz) in enumerate(configs):
    ms = math.log(mp / (1.0 - mp))
    sig_cfg = ConfluenceConfig(band_ticks=band, min_distinct_tf=mtf, rr=rr)
    sig_cfg.direction_mode = "auto"
    sig_cfg.tick_size = tick
    sig_cfg.enable_breakout = False

    run_cfg = ConfluenceBacktestConfig(
        wait_minutes=1, min_score=ms,
        base_minutes=base, timeframes=timeframes,
        one_trade_per_session_direction=True,
        trail_trigger_pct=0.50, trail_lock_pct=0.05,
        full_tp_lock=0, allowed_sessions=sessions,
    )
    bt_cfg = BacktestConfig(initial_capital=50000.0, symbol="MNQ", commission_rt=1.0, fees_rt=2.8)

    bt = ConfluenceBacktester(
        signal_cfg=sig_cfg, run_cfg=run_cfg,
        contract_id="CON.F.US.MNQ.M26", contract_size=sz,
        bt_config=bt_cfg, scorer=scorer,
    )
    result = bt.run(candles, zones_timeline=timeline)
    m = result.metrics
    if m.total_trades < 3:
        continue

    results.append({
        "rr": rr, "prob": mp, "band": band, "mtf": mtf,
        "sessions": "+".join(sessions), "size": sz,
        "trades": m.total_trades,
        "wr": m.win_rate,
        "pnl": m.total_pnl,
        "pf": m.profit_factor,
        "dd": m.max_drawdown,
        "calmar": m.calmar_ratio,
        "aw": m.avg_win,
        "al": m.avg_loss,
    })
    if (idx+1) % 100 == 0:
        sys.stdout.write(f"\r  {idx+1}/{len(configs)}...")
        sys.stdout.flush()

sys.stdout.write(f"\r  {len(configs)}/{len(configs)} done.  {len(results)} valid results.\n")
sys.stdout.flush()

# Write all results to a file for reliable reading
import json
out_path = ROOT / "data" / "confluence_market_sweep_results.json"
out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

# Filter DD < 2k, positive PnL
low_dd = [r for r in results if r["dd"] < 2000 and r["pnl"] > 0]
low_dd.sort(key=lambda r: r["pnl"], reverse=True)

hdr = (f"{'RR':>4} {'Prob':>5} {'Band':>4} {'TF':>2} {'Sessions':<20} {'Sz':>2} "
       f"{'Trd':>4} {'Win%':>6} {'PnL':>9} {'PF':>6} {'MaxDD':>8} {'Calmar':>7}")

lines = []
lines.append(f"=== MaxDD < $2,000 + positive PnL ({len(low_dd)} configs) ===")
lines.append(hdr)
lines.append("-" * 100)
for r in low_dd[:30]:
    lines.append(f"{r['rr']:>4.1f} {r['prob']:>5.2f} {r['band']:>4} {r['mtf']:>2} "
          f"{r['sessions']:<20} {r['size']:>2} "
          f"{r['trades']:>4} {r['wr']*100:>5.1f}% ${r['pnl']:>8,.0f} "
          f"{r['pf']:>5.2f} ${r['dd']:>7,.0f} {r['calmar']:>6.2f}")

# Also show top by Calmar with DD < 4k
lines.append(f"\n=== Top 20 by Calmar, DD < $4,000, PnL > 0 ===")
lines.append(hdr)
lines.append("-" * 100)
med_dd = [r for r in results if r["dd"] < 4000 and r["pnl"] > 0]
med_dd.sort(key=lambda r: r["calmar"], reverse=True)
for r in med_dd[:20]:
    lines.append(f"{r['rr']:>4.1f} {r['prob']:>5.2f} {r['band']:>4} {r['mtf']:>2} "
          f"{r['sessions']:<20} {r['size']:>2} "
          f"{r['trades']:>4} {r['wr']*100:>5.1f}% ${r['pnl']:>8,.0f} "
          f"{r['pf']:>5.2f} ${r['dd']:>7,.0f} {r['calmar']:>6.2f}")

report = "\n".join(lines)
report_path = ROOT / "data" / "confluence_market_sweep_report.txt"
report_path.write_text(report, encoding="utf-8")
print(report)
print(f"\nResults saved to {out_path}")
print(f"Report saved to {report_path}")
