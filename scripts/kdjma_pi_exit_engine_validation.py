"""Research-only validation of KDJMA + PI exits through BacktestEngine.

The companion ``kdjma_pi_exit_study.py`` keeps the entry stream fixed so the
exit-only question is visible.  This file runs the key variants through the
real production backtest adapter as a parity check.  It does not modify any
production module, preset, or live state.
"""

from __future__ import annotations

import copy
import argparse
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.backtest.engine import BacktestEngine  # noqa: E402
from backend.data import candle_store  # noqa: E402
from backend.db.models import (  # noqa: E402
    BacktestConfig,
    Direction,
    current_quarterly_contract_id,
    get_commission_rt,
    get_fees_rt,
)
from backend.strategy.exit_policy import ExitPolicy, ExitTrailMode  # noqa: E402
from backend.strategy.factor import FactorSignalStrategy  # noqa: E402
from backend.strategy.session_filter import (  # noqa: E402
    MARKET_PHASE_FLATTEN,
    is_allowed_session,
    market_close_phase,
)
from scripts.kdjma_pi_exit_study import ExitSpec, _params, _parse_time  # noqa: E402

UTC = timezone.utc
START = datetime(2026, 1, 1, tzinfo=UTC)
ALL_SESSIONS = ["ASIA", "EURO", "PRE", "RTH", "AH"]


class _PiExitKdjma(FactorSignalStrategy):
    """KDJMA entry conditions with one direction-aware PI exit contract."""

    def __init__(self, params, spec: ExitSpec):
        self._exit_spec = spec
        super().__init__(params)

    def _build_signal(self, candle, pending, entry_price=None):
        direction = pending["direction"]
        if direction == Direction.BUY:
            sl = self._exit_spec.long_sl
            tp = sl * self._exit_spec.long_rr
            hold = self._exit_spec.long_hold_min
            hard_tp = True
        else:
            sl = self._exit_spec.short_sl
            tp = sl * self._exit_spec.short_rr
            hold = self._exit_spec.short_hold_min
            hard_tp = self._exit_spec.short_hard_tp

        old_sl, old_tp = self.sl_value, self.tp_value
        self.sl_value, self.tp_value = sl, (tp if tp > 0 else 1.0)
        try:
            signal = super()._build_signal(candle, pending, entry_price)
        finally:
            self.sl_value, self.tp_value = old_sl, old_tp
        if signal is not None:
            signal.exit_policy = ExitPolicy(
                model="pi",
                max_hold_minutes=max(0, int(hold)),
                hard_tp_enabled=hard_tp,
                trail_mode=ExitTrailMode.NONE,
            )
        return signal


def _stats(trades, spec=None):
    values = [float(t.pnl or 0.0) for t in trades]
    gain = sum(v for v in values if v > 0)
    loss = -sum(v for v in values if v < 0)
    equity = peak = dd = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        dd = max(dd, peak - equity)

    def side(direction):
        wanted = Direction.BUY if direction == "BUY" else Direction.SELL
        rows = [t for t in trades if t.direction == wanted]
        vals = [float(t.pnl or 0.0) for t in rows]
        g = sum(v for v in vals if v > 0)
        l = -sum(v for v in vals if v < 0)
        return len(rows), round(sum(vals), 2), round(g / l, 4) if l else (999.0 if g else 0.0)

    monthly = defaultdict(list)
    exit_counts = Counter()
    for trade in trades:
        monthly[trade.entry_time.strftime("%Y-%m")].append(float(trade.pnl or 0.0))
        reason = getattr(trade.exit_reason, "value", str(trade.exit_reason))
        if reason == "flatten" and spec is not None:
            held = (
                trade.exit_time - trade.entry_time
            ).total_seconds() / 60.0 if trade.exit_time else 0.0
            hold_limit = spec.long_hold_min if trade.direction == Direction.BUY else spec.short_hold_min
            reason = "time" if hold_limit > 0 and abs(held - hold_limit) <= 1.1 else "session_flat"
        exit_counts[reason] += 1

    monthly_stats = {}
    for month, vals in sorted(monthly.items()):
        g = sum(v for v in vals if v > 0)
        l = -sum(v for v in vals if v < 0)
        monthly_stats[month] = {
            "trades": len(vals),
            "pnl": round(sum(vals), 2),
            "pf": round(g / l, 4) if l else (999.0 if g else 0.0),
        }

    return {
        "trades": len(trades),
        "pnl": round(sum(values), 2),
        "pf": round(gain / loss, 4) if loss else (999.0 if gain else 0.0),
        "win_rate": round(sum(v > 0 for v in values) / len(values), 4) if values else 0.0,
        "max_dd": round(dd, 2),
        "long": side("BUY"),
        "short": side("SELL"),
        "exit_counts": dict(sorted(exit_counts.items())),
        "monthly": monthly_stats,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--only",
        choices=("baseline", "pi", "pi_long240", "research"),
        default=None,
    )
    parser.add_argument("--start", default=START.isoformat())
    parser.add_argument("--long-hold", type=int, default=240)
    parser.add_argument("--short-hold", type=int, default=60)
    args = parser.parse_args()
    start = _parse_time(args.start)
    cid = current_quarterly_contract_id("MNQ")
    params = _params(cid)
    params.tr_allowed_sessions = list(ALL_SESSIONS)
    params.tr_one_trade_per_session = False
    params.one_trade_per_session_direction = False
    params.tr_daily_loss_stop = 0
    params.tr_daily_win_stop = 0
    params.factor_max_trades_per_day = 3
    scan_start = start - timedelta(days=3)
    candles = sorted(
        [c for c in candle_store.load("MNQ", 1) if scan_start <= c.timestamp],
        key=lambda c: c.timestamp,
    )
    cfg = BacktestConfig(
        initial_capital=50_000.0,
        symbol="MNQ",
        commission_rt=get_commission_rt(cid),
        fees_rt=get_fees_rt(cid),
    )
    specs = [
        ("baseline", "KDJMA current baseline", None),
        ("pi", "PI current asymmetric", ExitSpec("PI current asymmetric", 4.0, 3.0, 0, 1.5, 3.0, args.short_hold)),
        ("pi_long240", f"PI current + long {args.long_hold}m", ExitSpec("PI current + long 240m", 4.0, 3.0, args.long_hold, 1.5, 3.0, args.short_hold)),
        ("research", "PI earlier research asym", ExitSpec("PI earlier research asym", 3.5, 3.0, 0, 2.5, 2.0, args.short_hold)),
    ]
    print(f"BacktestEngine validation: {len(candles):,} candles from {scan_start.isoformat()}", flush=True)
    if args.only:
        specs = [item for item in specs if item[0] == args.only]
    for _, name, spec in specs:
        p = copy.deepcopy(params)
        if spec is None:
            p.factor_sl_value = 2.0
            p.factor_tp_value = 2.0
            engine = BacktestEngine(config=cfg, strategy_params=p, record_equity=False)
        else:
            engine = BacktestEngine(config=cfg, strategy_params=p, record_equity=False)
            engine.trend_follow = _PiExitKdjma(p, spec)
        result = engine.run(candles)
        trades = [t for t in result.trades if t.entry_time >= start]
        stats = _stats(trades, spec)
        print(name, stats, flush=True)


if __name__ == "__main__":
    main()
