"""Sweep RR values for confluence with market-order entries.

Tests RR = 1.0, 1.5, 2.0, 3.0, 4.0 and reports metrics for each.
"""
import pickle, sys, math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.db.models import Candle, get_tick_size, BacktestConfig
from backend.strategy.consolidation import timeframes_for_base
from backend.strategy.confluence import ConfluenceConfig, MAX_RECENCY_DEPTH
from backend.strategy.confluence_scorer import resolve_scorer
from backend.backtest.confluence_backtest import (
    ConfluenceBacktester, ConfluenceBacktestConfig, build_zone_timeline,
)
from collections import Counter

store = ROOT / "data" / "store" / "MNQ_accumulated_1m.pkl"
candles = sorted(pickle.loads(store.read_bytes()), key=lambda c: c.timestamp)
print(f"Candles: {len(candles)}")

tick = get_tick_size("CON.F.US.MNQ.M26")
base = 1
timeframes = timeframes_for_base(base)
scorer = resolve_scorer(True, None)

min_prob = 0.65
min_score = math.log(min_prob / (1.0 - min_prob))

print("Building zone timeline (once)...")
timeline = build_zone_timeline(candles, timeframes, tick, MAX_RECENCY_DEPTH)
print(f"Timeline: {len(timeline)} entries\n")

bt_cfg = BacktestConfig(
    initial_capital=50000.0, symbol="MNQ",
    commission_rt=1.0, fees_rt=2.8,
)

# Also try different sessions
session_sets = [
    ("ASIA",),
    ("ASIA", "EURO"),
    ("ASIA", "EURO", "PRE"),
    ("ASIA", "EURO", "PRE", "RTH"),
]

rr_values = [1.0, 1.5, 2.0, 3.0, 4.0]

print(f"{'RR':>4} {'Sessions':<24} {'Trades':>6} {'Win%':>6} {'PnL':>9} {'PF':>6} "
      f"{'MaxDD':>9} {'Calmar':>7} {'AvgW':>7} {'AvgL':>7}")
print("-" * 100)

best_pnl = -999999
best_cfg = ""

for sessions in session_sets:
    for rr in rr_values:
        sig_cfg = ConfluenceConfig(
            band_ticks=4.0, min_distinct_tf=2, rr=rr,
        )
        sig_cfg.direction_mode = "auto"
        sig_cfg.tick_size = tick
        sig_cfg.ev_floor = None
        sig_cfg.rr_grid = None
        sig_cfg.enable_breakout = False
        sig_cfg.max_risk_ticks = None

        run_cfg = ConfluenceBacktestConfig(
            wait_minutes=1, min_score=min_score,
            base_minutes=base, timeframes=timeframes,
            one_trade_per_session_direction=True,
            trail_trigger_pct=0.50, trail_lock_pct=0.05,
            full_tp_lock=0,
            allowed_sessions=sessions,
        )

        bt = ConfluenceBacktester(
            signal_cfg=sig_cfg, run_cfg=run_cfg,
            contract_id="CON.F.US.MNQ.M26",
            contract_size=3, bt_config=bt_cfg, scorer=scorer,
        )
        result = bt.run(candles, zones_timeline=timeline)
        m = result.metrics
        sess_label = "+".join(sessions)

        print(f"{rr:>4.1f} {sess_label:<24} {m.total_trades:>6} "
              f"{m.win_rate*100:>5.1f}% ${m.total_pnl:>8,.0f} "
              f"{m.profit_factor:>5.2f} ${m.max_drawdown:>8,.0f} "
              f"{m.calmar_ratio:>6.2f} ${m.avg_win:>6,.0f} ${m.avg_loss:>6,.0f}")

        if m.total_pnl > best_pnl:
            best_pnl = m.total_pnl
            best_cfg = f"RR={rr} Sessions={sess_label} PnL=${m.total_pnl:,.0f} PF={m.profit_factor:.2f}"

print(f"\nBest: {best_cfg}")
