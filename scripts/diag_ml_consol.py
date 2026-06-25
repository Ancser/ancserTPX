"""Quick diagnostic: test specific ML Consol V2 configs to understand performance.

Tests:
  1. Original V2 preset (range SL, max_risk=80, no cap)
  2. VA SL with various buffers
  3. Range SL with various max_risk caps
"""

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
    print(f"Loaded {len(candles)} candles, {candles[0].timestamp} -> {candles[-1].timestamp}")
    return candles


def run_test(candles, vp_tl, label, **kwargs):
    """Run a single backtest config and print results."""
    defaults = dict(
        lookback=30, band_ticks=2, sl_buffer_ticks=4,
        sl_mode="range", tick_size=TICK, tp_mode="rr", rr=4.0,
        max_risk_ticks=80.0, min_risk_ticks=4.0,
    )
    defaults.update(kwargs)

    lb = defaults.pop("lookback")
    sessions = defaults.pop("sessions", ("ASIA", "EURO"))
    trail = defaults.pop("trail", False)
    one_per_sess = defaults.pop("one_trade_per_session", False)

    sig_cfg = MLTrendConfig(lookback=lb, **defaults)
    run_cfg = MLTrendBacktestConfig(
        trail_trigger_pct=0.50 if trail else 0.0,
        trail_lock_pct=0.05 if trail else 0.0,
        one_trade_per_session=one_per_sess,
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

    # Compute avg risk from trades
    risks = []
    for t in result.trades:
        r = abs(t.entry_price - t.original_sl_price) / TICK
        risks.append(r)
    avg_risk = sum(risks) / len(risks) if risks else 0
    max_risk_actual = max(risks) if risks else 0
    min_risk_actual = min(risks) if risks else 0

    print(f"\n  {label}")
    print(f"    Trades={m.total_trades:>5} | WR={m.win_rate*100:>5.1f}% | "
          f"PnL=${m.total_pnl:>9,.0f} | PF={m.profit_factor:>5.2f} | "
          f"DD=${m.max_drawdown:>7,.0f} | Calmar={m.calmar_ratio:>6.2f}")
    print(f"    Risk ticks: avg={avg_risk:.1f} min={min_risk_actual:.1f} max={max_risk_actual:.1f}")
    if m.total_trades > 0:
        print(f"    AvgWin=${m.avg_win:>7,.0f} AvgLoss=${m.avg_loss:>7,.0f}")

    # Show first 5 trades for debugging
    if result.trades:
        print(f"    Sample trades:")
        for t in result.trades[:5]:
            risk_t = abs(t.entry_price - t.original_sl_price) / TICK
            dir_s = "L" if t.direction.value == "BUY" else "S"
            print(f"      {dir_s} entry={t.entry_price:.2f} SL={t.original_sl_price:.2f} "
                  f"TP={t.original_tp_price:.2f} exit={t.exit_price:.2f} "
                  f"risk={risk_t:.0f}t PnL=${t.pnl:>7,.0f} {t.exit_reason.value}")

    return m


def main():
    candles = load_candles()

    # Pre-compute VP timelines
    lookbacks = [30, 60, 120]
    timelines = {}
    for lb in lookbacks:
        t0 = _time.perf_counter()
        tl = precompute_vp_timeline(candles, lb, TICK, recalc_interval=5)
        el = _time.perf_counter() - t0
        print(f"VP LB={lb}: {el:.1f}s")
        timelines[lb] = tl

    print("\n" + "=" * 80)
    print(" DIAGNOSTIC: ML Consolidation V2")
    print("=" * 80)

    # -- Test 1: Original V2 preset (what user had before) --
    print("\n-- TEST 1: Original V2 preset (range SL, max_risk=80) --")
    run_test(candles, timelines[30],
             "LB30 Band2 SLB4 range MxR80 RR4 ASIA+EURO NoTrail",
             lookback=30, band_ticks=2, sl_buffer_ticks=4,
             sl_mode="range", max_risk_ticks=80, rr=4.0,
             sessions=("ASIA", "EURO"))

    # -- Test 2: Same but with one_trade_per_session=True (original V1 setting) --
    print("\n-- TEST 2: V1 style (one trade per session) --")
    run_test(candles, timelines[30],
             "LB30 Band2 SLB4 range MxR80 RR4 ASIA+EURO NoTrail 1/sess",
             lookback=30, band_ticks=2, sl_buffer_ticks=4,
             sl_mode="range", max_risk_ticks=80, rr=4.0,
             sessions=("ASIA", "EURO"), one_trade_per_session=True)

    # -- Test 3: VA SL with different buffers --
    print("\n-- TEST 3: VA SL with varying buffers --")
    for buf in [4, 8, 12, 16, 24]:
        run_test(candles, timelines[30],
                 f"LB30 Band2 SLB{buf} VA MxR80 RR4 ASIA+EURO",
                 lookback=30, band_ticks=2, sl_buffer_ticks=buf,
                 sl_mode="va", max_risk_ticks=80, rr=4.0,
                 sessions=("ASIA", "EURO"))

    # -- Test 4: Range SL with different max_risk caps --
    print("\n-- TEST 4: Range SL with varying max_risk --")
    for mxr in [20, 40, 60, 80, 120, 200]:
        run_test(candles, timelines[30],
                 f"LB30 Band2 SLB4 range MxR{mxr} RR4 ASIA+EURO",
                 lookback=30, band_ticks=2, sl_buffer_ticks=4,
                 sl_mode="range", max_risk_ticks=mxr, rr=4.0,
                 sessions=("ASIA", "EURO"))

    # -- Test 5: Different lookbacks with range SL --
    print("\n-- TEST 5: Range SL with different lookbacks --")
    for lb in [30, 60, 120]:
        run_test(candles, timelines[lb],
                 f"LB{lb} Band2 SLB4 range MxR80 RR4 ASIA+EURO",
                 lookback=lb, band_ticks=2, sl_buffer_ticks=4,
                 sl_mode="range", max_risk_ticks=80, rr=4.0,
                 sessions=("ASIA", "EURO"))

    # -- Test 6: Different RRs with range SL --
    print("\n-- TEST 6: Range SL different RRs --")
    for rr in [1.0, 1.5, 2.0, 3.0, 4.0]:
        run_test(candles, timelines[30],
                 f"LB30 Band2 SLB4 range MxR80 RR{rr} ASIA+EURO",
                 lookback=30, band_ticks=2, sl_buffer_ticks=4,
                 sl_mode="range", max_risk_ticks=80, rr=rr,
                 sessions=("ASIA", "EURO"))

    # -- Test 7: POC mode (original concept) --
    print("\n-- TEST 7: POC mode --")
    for lb in [30, 60, 120]:
        run_test(candles, timelines[lb],
                 f"LB{lb} Band2 SLB4 range MxR80 POC ASIA+EURO",
                 lookback=lb, band_ticks=2, sl_buffer_ticks=4,
                 sl_mode="range", max_risk_ticks=80, tp_mode="poc", rr=0,
                 sessions=("ASIA", "EURO"))

    # -- Test 8: ASIA only --
    print("\n-- TEST 8: ASIA only --")
    run_test(candles, timelines[30],
             "LB30 Band2 SLB4 range MxR80 RR4 ASIA NoTrail",
             lookback=30, band_ticks=2, sl_buffer_ticks=4,
             sl_mode="range", max_risk_ticks=80, rr=4.0,
             sessions=("ASIA",))

    # -- Test 9: All sessions --
    print("\n-- TEST 9: All sessions --")
    run_test(candles, timelines[30],
             "LB30 Band2 SLB4 range MxR80 RR4 ALL NoTrail",
             lookback=30, band_ticks=2, sl_buffer_ticks=4,
             sl_mode="range", max_risk_ticks=80, rr=4.0,
             sessions=("ASIA", "EURO", "US"))


if __name__ == "__main__":
    main()
