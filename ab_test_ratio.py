"""
A/B Test: ENTRY_RATIO 0.2 vs 0.5
Runs backtest with both ratios using the same historical candle data
and compares results to definitively prove whether they differ.
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import asyncio
import logging
from backend.db.models import BacktestConfig, Candle
from backend.backtest.engine import BacktestEngine
from backend.strategy.trend_follow import SessionTrendFollow

# Suppress noisy logs
logging.basicConfig(level=logging.WARNING)

# Load candles from the server's fetch-historical endpoint
from backend.broker.topstepx import TopstepXClient, BarUnit
from datetime import datetime, timedelta


async def fetch_candles():
    """Fetch 5 days of 1m NQ data from TopstepX."""
    client = TopstepXClient()
    await client.login()

    end = datetime.utcnow()
    start = end - timedelta(days=7)  # fetch 7 calendar days to get ~5 trading days

    print(f"Fetching 1m candles: {start.strftime('%Y-%m-%d')} ~ {end.strftime('%Y-%m-%d')}")

    candles = await client.get_historical_bars_paginated(
        contract_id="CON.F.US.ENQ*.M25",
        unit=BarUnit.MINUTE,
        unit_number=1,
        start_time=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        end_time=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    print(f"Got {len(candles)} candles")
    return candles


def run_backtest_with_ratio(candles, ratio):
    """Run backtest with a specific ENTRY_RATIO."""
    # Monkey-patch the ratio
    original = SessionTrendFollow.ENTRY_RATIO
    SessionTrendFollow.ENTRY_RATIO = ratio

    config = BacktestConfig(
        strategies=["trend_follow"],
        initial_capital=50000,
    )
    engine = BacktestEngine(config, max_daily_trades=5)
    result = engine.run(list(candles))

    # Restore
    SessionTrendFollow.ENTRY_RATIO = original
    return result


async def main():
    candles = await fetch_candles()
    if not candles:
        print("ERROR: No candles fetched!")
        return

    candles = sorted(candles, key=lambda c: c.timestamp)
    print(f"\nCandle range: {candles[0].timestamp} ~ {candles[-1].timestamp}")
    print(f"Total candles: {len(candles)}\n")

    # Run A/B
    print("=" * 60)
    print("Running backtest with ENTRY_RATIO = 0.5 ...")
    result_05 = run_backtest_with_ratio(candles, 0.5)

    print("Running backtest with ENTRY_RATIO = 0.2 ...")
    result_02 = run_backtest_with_ratio(candles, 0.2)
    print("=" * 60)

    # Compare
    m05 = result_05.metrics
    m02 = result_02.metrics

    print(f"\n{'Metric':<25} {'Ratio=0.5':>15} {'Ratio=0.2':>15} {'Diff':>12}")
    print("-" * 70)
    print(f"{'Total Trades':<25} {m05.total_trades:>15} {m02.total_trades:>15} {m02.total_trades - m05.total_trades:>12}")
    print(f"{'Win Rate':<25} {m05.win_rate:>14.1%} {m02.win_rate:>14.1%} {(m02.win_rate - m05.win_rate):>11.1%}")
    print(f"{'Total PnL':<25} ${m05.total_pnl:>13,.0f} ${m02.total_pnl:>13,.0f} ${m02.total_pnl - m05.total_pnl:>10,.0f}")
    print(f"{'Max Drawdown':<25} ${m05.max_drawdown:>13,.0f} ${m02.max_drawdown:>13,.0f} ${m02.max_drawdown - m05.max_drawdown:>10,.0f}")
    print(f"{'Avg Win':<25} ${m05.avg_win:>13,.0f} ${m02.avg_win:>13,.0f} ${m02.avg_win - m05.avg_win:>10,.0f}")
    print(f"{'Avg Loss':<25} ${m05.avg_loss:>13,.0f} ${m02.avg_loss:>13,.0f} ${m02.avg_loss - m05.avg_loss:>10,.0f}")

    # Print individual trades for comparison
    print(f"\n{'='*70}")
    print(f"INDIVIDUAL TRADES COMPARISON (Ratio=0.5 vs Ratio=0.2)")
    print(f"{'='*70}")

    max_len = max(len(result_05.trades), len(result_02.trades))
    for i in range(max_len):
        t05 = result_05.trades[i] if i < len(result_05.trades) else None
        t02 = result_02.trades[i] if i < len(result_02.trades) else None

        if t05 and t02:
            print(f"\nTrade #{i+1}:")
            print(f"  0.5: {t05.direction.value} entry={t05.entry_price:.2f} SL={t05.sl_price:.2f} TP={t05.tp_price:.2f} → {t05.exit_reason.value if t05.exit_reason else '?'} PnL=${t05.pnl:+,.0f}")
            print(f"  0.2: {t02.direction.value} entry={t02.entry_price:.2f} SL={t02.sl_price:.2f} TP={t02.tp_price:.2f} → {t02.exit_reason.value if t02.exit_reason else '?'} PnL=${t02.pnl:+,.0f}")
            if t05.entry_price != t02.entry_price:
                print(f"  ΔEntry: {t02.entry_price - t05.entry_price:+.2f} pts")
        elif t05:
            print(f"\nTrade #{i+1}: ONLY in 0.5 — {t05.direction.value} entry={t05.entry_price:.2f} → {t05.exit_reason.value if t05.exit_reason else '?'} PnL=${t05.pnl:+,.0f}")
        elif t02:
            print(f"\nTrade #{i+1}: ONLY in 0.2 — {t02.direction.value} entry={t02.entry_price:.2f} → {t02.exit_reason.value if t02.exit_reason else '?'} PnL=${t02.pnl:+,.0f}")

    # Zone comparison
    print(f"\n{'='*70}")
    print(f"Zones: 0.5={len(result_05.zones)}  |  0.2={len(result_02.zones)}")
    print(f"(Zones should be identical since detector is the same)")


if __name__ == "__main__":
    asyncio.run(main())
