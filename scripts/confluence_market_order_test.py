"""Quick comparison: confluence backtest with market orders (new) vs original limit.

Runs the confluence backtester with the user's standard preset params and
reports key metrics. Uses the stored 1m candle data.
"""
import pickle, sys, math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.db.models import Candle, get_tick_size
from backend.strategy.consolidation import timeframes_for_base
from backend.strategy.confluence import ConfluenceConfig, MAX_RECENCY_DEPTH
from backend.strategy.confluence_scorer import resolve_scorer
from backend.backtest.confluence_backtest import (
    ConfluenceBacktester, ConfluenceBacktestConfig, build_zone_timeline,
)
from backend.db.models import BacktestConfig

# Load candles
store = ROOT / "data" / "store" / "MNQ_accumulated_1m.pkl"
candles = sorted(pickle.loads(store.read_bytes()), key=lambda c: c.timestamp)
print(f"Candles: {len(candles)} | {candles[0].timestamp} to {candles[-1].timestamp}")

tick = get_tick_size("CON.F.US.MNQ.M26")
base = 1
timeframes = timeframes_for_base(base)
scorer = resolve_scorer(True, None)

# Standard preset params
min_prob = 0.65
min_score = math.log(min_prob / (1.0 - min_prob))

sig_cfg = ConfluenceConfig(
    band_ticks=4.0,
    min_distinct_tf=2,
    rr=1.0,
)
sig_cfg.direction_mode = "auto"
sig_cfg.tick_size = tick
sig_cfg.ev_floor = None
sig_cfg.rr_grid = None
sig_cfg.enable_breakout = False
sig_cfg.max_risk_ticks = None

run_cfg = ConfluenceBacktestConfig(
    wait_minutes=1,
    min_score=min_score,
    base_minutes=base,
    timeframes=timeframes,
    one_trade_per_session_direction=True,
    trail_trigger_pct=0.50,
    trail_lock_pct=0.05,
    full_tp_lock=0,
    allowed_sessions=("ASIA",),
)

bt_cfg = BacktestConfig(
    initial_capital=50000.0,
    symbol="MNQ",
    commission_rt=1.0,
    fees_rt=2.8,
)

print("Building zone timeline...")
timeline = build_zone_timeline(candles, timeframes, tick, MAX_RECENCY_DEPTH)
print(f"Timeline built: {len(timeline)} entries")

print("\n--- Running MARKET ORDER backtest ---")
bt = ConfluenceBacktester(
    signal_cfg=sig_cfg, run_cfg=run_cfg, contract_id="CON.F.US.MNQ.M26",
    contract_size=3, bt_config=bt_cfg, scorer=scorer,
)
result = bt.run(candles, zones_timeline=timeline)
m = result.metrics

wins = [t for t in result.trades if (t.pnl or 0) > 0]
losses = [t for t in result.trades if (t.pnl or 0) < 0]
scratches = [t for t in result.trades if (t.pnl or 0) == 0]

print(f"  Trades:  {m.total_trades}")
print(f"  Wins:    {len(wins)}")
print(f"  Losses:  {len(losses)}")
print(f"  Scratches: {len(scratches)}")
print(f"  Win Rate: {m.win_rate*100:.1f}%")
print(f"  Total PnL: ${m.total_pnl:,.0f}")
print(f"  PF:      {m.profit_factor:.2f}")
print(f"  Max DD:  ${m.max_drawdown:,.0f}")
print(f"  Calmar:  {m.calmar_ratio:.2f}")
print(f"  Avg Win:  ${m.avg_win:,.0f}")
print(f"  Avg Loss: ${m.avg_loss:,.0f}")
print(f"  Avg RR:   {m.avg_rr_ratio:.2f}")

# Show exit breakdown
from collections import Counter
exits = Counter(t.exit_reason.value if t.exit_reason else "?" for t in result.trades)
print(f"  Exits:   {dict(exits)}")

# Show top 10 trades by PnL
print("\n--- Top 10 trades ---")
by_pnl = sorted(result.trades, key=lambda t: t.pnl or 0, reverse=True)
for t in by_pnl[:10]:
    print(f"  {t.entry_time:%m-%d %H:%M} {t.direction.value:4s} "
          f"entry={t.entry_price:.2f} exit={t.exit_price:.2f} "
          f"SL={t.sl_price:.2f} TP={t.tp_price:.2f} "
          f"PnL=${t.pnl:+,.0f} [{t.exit_reason.value}]")

# Show worst 5
print("\n--- Worst 5 trades ---")
for t in by_pnl[-5:]:
    print(f"  {t.entry_time:%m-%d %H:%M} {t.direction.value:4s} "
          f"entry={t.entry_price:.2f} exit={t.exit_price:.2f} "
          f"SL={t.sl_price:.2f} TP={t.tp_price:.2f} "
          f"PnL=${t.pnl:+,.0f} [{t.exit_reason.value}]")
