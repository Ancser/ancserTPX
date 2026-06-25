"""Quick exit-reason breakdown for RR=2.0 ASIA to verify trail TP is working."""
import pickle, sys, math
from pathlib import Path
from collections import Counter

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
timeline = build_zone_timeline(candles, timeframes, tick, MAX_RECENCY_DEPTH)

min_prob = 0.65
ms = math.log(min_prob / (1.0 - min_prob))

sig_cfg = ConfluenceConfig(band_ticks=4.0, min_distinct_tf=2, rr=2.0)
sig_cfg.direction_mode = "auto"
sig_cfg.tick_size = tick

run_cfg = ConfluenceBacktestConfig(
    wait_minutes=1, min_score=ms,
    base_minutes=base, timeframes=timeframes,
    one_trade_per_session_direction=True,
    trail_trigger_pct=0.50, trail_lock_pct=0.05,
    full_tp_lock=0, allowed_sessions=("ASIA",),
)
bt_cfg = BacktestConfig(initial_capital=50000.0, symbol="MNQ", commission_rt=1.0, fees_rt=2.8)

bt = ConfluenceBacktester(
    signal_cfg=sig_cfg, run_cfg=run_cfg,
    contract_id="CON.F.US.MNQ.M26", contract_size=3,
    bt_config=bt_cfg, scorer=scorer,
)
result = bt.run(candles, zones_timeline=timeline)
m = result.metrics

exits = Counter(t.exit_reason.value if t.exit_reason else "?" for t in result.trades)
print(f"RR=2.0 ASIA 3xMNQ Trail=50%/5%")
print(f"Trades: {m.total_trades}  Win: {m.win_rate*100:.1f}%  PnL: ${m.total_pnl:,.0f}")
print(f"Exit breakdown: {dict(exits)}")

# PnL by exit type
for reason in exits:
    trades = [t for t in result.trades if (t.exit_reason.value if t.exit_reason else "?") == reason]
    pnl = sum(t.pnl or 0 for t in trades)
    avg = pnl / len(trades) if trades else 0
    print(f"  {reason:10s}: {len(trades):3d} trades, total ${pnl:>+8,.0f}, avg ${avg:>+6,.0f}")
