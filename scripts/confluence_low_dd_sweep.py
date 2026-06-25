"""Sweep for confluence market-order configs with max DD < $2k.

Dimensions: RR, min_prob, band_ticks, min_distinct_tf, sessions, contract_size.
Trail TP 50% is always ON (trail_trigger_pct=0.50, trail_lock_pct=0.05).
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
print(f"Candles: {len(candles)}")

tick = get_tick_size("CON.F.US.MNQ.M26")
base = 1
timeframes = timeframes_for_base(base)
scorer = resolve_scorer(True, None)

print("Building zone timeline...")
timeline = build_zone_timeline(candles, timeframes, tick, MAX_RECENCY_DEPTH)
print(f"Timeline: {len(timeline)} entries\n")

# Sweep dimensions
rr_values = [1.5, 2.0, 3.0, 4.0]
min_prob_values = [0.65, 0.70, 0.75, 0.80, 0.85]
band_values = [4, 6, 8]
min_tf_values = [2, 3]
session_sets = [
    ("ASIA",),
    ("ASIA", "EURO"),
    ("ASIA", "EURO", "PRE", "RTH"),
]
size_values = [1, 3]
trail_configs = [
    (0.50, 0.05),   # 50% trigger, 5% lock
    (0.30, 0.05),   # 30% trigger
]

results = []
total = len(rr_values) * len(min_prob_values) * len(band_values) * len(min_tf_values) * len(session_sets) * len(size_values) * len(trail_configs)
print(f"Total combos: {total}")

count = 0
for rr in rr_values:
    for mp in min_prob_values:
        ms = math.log(mp / (1.0 - mp))
        for band in band_values:
            for mtf in min_tf_values:
                for sessions in session_sets:
                    for sz in size_values:
                        for tt_pct, tl_pct in trail_configs:
                            count += 1
                            sig_cfg = ConfluenceConfig(
                                band_ticks=band, min_distinct_tf=mtf, rr=rr,
                            )
                            sig_cfg.direction_mode = "auto"
                            sig_cfg.tick_size = tick
                            sig_cfg.ev_floor = None
                            sig_cfg.rr_grid = None
                            sig_cfg.enable_breakout = False
                            sig_cfg.max_risk_ticks = None

                            run_cfg = ConfluenceBacktestConfig(
                                wait_minutes=1, min_score=ms,
                                base_minutes=base, timeframes=timeframes,
                                one_trade_per_session_direction=True,
                                trail_trigger_pct=tt_pct, trail_lock_pct=tl_pct,
                                full_tp_lock=0,
                                allowed_sessions=sessions,
                            )
                            bt_cfg = BacktestConfig(
                                initial_capital=50000.0, symbol="MNQ",
                                commission_rt=1.0, fees_rt=2.8,
                            )

                            bt = ConfluenceBacktester(
                                signal_cfg=sig_cfg, run_cfg=run_cfg,
                                contract_id="CON.F.US.MNQ.M26",
                                contract_size=sz, bt_config=bt_cfg, scorer=scorer,
                            )
                            result = bt.run(candles, zones_timeline=timeline)
                            m = result.metrics

                            if m.total_trades < 5:
                                continue

                            results.append({
                                "rr": rr, "prob": mp, "band": band, "mtf": mtf,
                                "sessions": "+".join(sessions), "size": sz,
                                "trail": f"{tt_pct:.0%}",
                                "trades": m.total_trades,
                                "win_rate": m.win_rate,
                                "pnl": m.total_pnl,
                                "pf": m.profit_factor,
                                "max_dd": m.max_drawdown,
                                "calmar": m.calmar_ratio,
                                "avg_win": m.avg_win,
                                "avg_loss": m.avg_loss,
                            })

                            if count % 50 == 0:
                                print(f"  {count}/{total}...")

# Sort by PnL, filter DD < 2k
low_dd = [r for r in results if r["max_dd"] < 2000 and r["pnl"] > 0]
low_dd.sort(key=lambda r: r["pnl"], reverse=True)

print(f"\n=== ALL configs with MaxDD < $2,000 and positive PnL ({len(low_dd)} found) ===")
print(f"{'RR':>4} {'Prob':>5} {'Band':>4} {'TF':>2} {'Sessions':<20} {'Sz':>2} {'Trail':>5} "
      f"{'Trd':>4} {'Win%':>6} {'PnL':>9} {'PF':>6} {'MaxDD':>8} {'Calmar':>7}")
print("-" * 110)
for r in low_dd[:40]:
    print(f"{r['rr']:>4.1f} {r['prob']:>5.2f} {r['band']:>4} {r['mtf']:>2} "
          f"{r['sessions']:<20} {r['size']:>2} {r['trail']:>5} "
          f"{r['trades']:>4} {r['win_rate']*100:>5.1f}% ${r['pnl']:>8,.0f} "
          f"{r['pf']:>5.2f} ${r['max_dd']:>7,.0f} {r['calmar']:>6.2f}")

# Also show best by Calmar regardless of DD threshold
print(f"\n=== Top 15 by Calmar (any DD, PnL>0) ===")
positive = [r for r in results if r["pnl"] > 0]
positive.sort(key=lambda r: r["calmar"], reverse=True)
for r in positive[:15]:
    print(f"{r['rr']:>4.1f} {r['prob']:>5.2f} {r['band']:>4} {r['mtf']:>2} "
          f"{r['sessions']:<20} {r['size']:>2} {r['trail']:>5} "
          f"{r['trades']:>4} {r['win_rate']*100:>5.1f}% ${r['pnl']:>8,.0f} "
          f"{r['pf']:>5.2f} ${r['max_dd']:>7,.0f} {r['calmar']:>6.2f}")
