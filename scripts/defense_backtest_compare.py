"""
Defense backtest comparison — preset #2 (trend, ASIA, MNQx1).

Runs the REAL BacktestEngine once on the persistent candle store, then evaluates
two risk defenses as filters on the realized trade list, scored with the SAME
PerformanceCalculator the app uses:

  Defense #1  TIME       — session-open cooldown: skip trades opened within the
                           first COOLDOWN_MIN minutes after a session open
                           (avoids the opening-volatility / stop-hunt window).
  Defense #2  STREAK      — consecutive-loss circuit breaker: within one TopStep
                           trade-date, once STREAK_STOP losses fire in a row,
                           skip the rest of that trade-date's trades.

Scenarios: BASELINE, #1, #2, #1+#2 (separate and combined).

Why filtering the realized trade list is faithful: both defenses only ever
*remove* trades (cooldown drops early ones; the breaker stops opening new ones
after a streak). The engine takes one trade per session-direction and each trade
is an independent signal, so a kept subset reproduces exactly what the engine
would have done with those defenses gating order entry. PnL is recomputed by the
same calculator, so numbers are comparable to the app's report.

Run:  python -m scripts.defense_backtest_compare
"""
from __future__ import annotations

import dataclasses
import json
import os
from collections import defaultdict
from datetime import datetime, timezone

from backend.data import candle_store
from backend.db.models import (
    StrategyParams, BacktestConfig, get_commission_rt, get_fees_rt,
    _extract_symbol,
)
from backend.backtest.engine import BacktestEngine, _topstep_trade_date
from backend.backtest.metrics import MetricsCalculator
from backend.api.routes import _normalize_strategy_name

# ── tunables ──────────────────────────────────────────────────────────────
PRESET_FILE   = os.path.join("data", "presets.json")
INITIAL_CAP   = 50_000.0
COOLDOWN_MIN  = 60          # Defense #1: minutes blocked after each session open
STREAK_STOP   = 3           # Defense #2: stop after this many consecutive losses/day

# Session opens in UTC (memory: ASIA 22:00, EURO 07:00, PRE 11:00, RTH 13:30, AH 20:00)
SESSION_OPENS_UTC = [(22, 0), (7, 0), (11, 0), (13, 30), (20, 0)]


def load_preset_params() -> tuple[StrategyParams, str]:
    d = json.load(open(PRESET_FILE, encoding="utf-8"))
    name, raw = next(iter(d["presets"].items()))
    field_names = {f.name for f in dataclasses.fields(StrategyParams)}
    kwargs = {k: v for k, v in raw.items() if k in field_names}
    return StrategyParams(**kwargs), name


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def minutes_since_session_open(dt: datetime) -> float:
    """Smallest non-negative minutes elapsed since any session open (handles
    opens before midnight like ASIA 22:00 by also checking the prior day)."""
    from datetime import timedelta
    dt = _utc(dt)
    best = 1e9
    for h, m in SESSION_OPENS_UTC:
        for day_off in (0, -1):
            open_dt = dt.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=day_off)
            elapsed = (dt - open_dt).total_seconds() / 60.0
            if 0 <= elapsed < best:
                best = elapsed
    return best


def filter_time(trades, cooldown_min=COOLDOWN_MIN):
    """Defense #1: drop trades opened within cooldown_min of a session open."""
    return [t for t in trades if minutes_since_session_open(t.entry_time) >= cooldown_min]


def filter_streak(trades, streak_stop=STREAK_STOP):
    """Defense #2: per TopStep trade-date, stop after streak_stop consecutive losses."""
    by_day = defaultdict(list)
    for t in sorted(trades, key=lambda x: _utc(x.entry_time)):
        by_day[_topstep_trade_date(_utc(t.entry_time))].append(t)
    kept = []
    for day in sorted(by_day):
        streak = 0
        stopped = False
        for t in by_day[day]:
            if stopped:
                continue
            kept.append(t)
            if (t.pnl or 0) < 0:
                streak += 1
                if streak >= streak_stop:
                    stopped = True
            else:
                streak = 0
    return sorted(kept, key=lambda x: _utc(x.entry_time))


def score(trades):
    m = MetricsCalculator().calculate_all(trades, INITIAL_CAP)
    completed = [t for t in trades if t.pnl is not None]
    return {
        "trades": len(completed),
        "pnl": m.total_pnl,
        "win_rate": m.win_rate,
        "maxDD": m.max_drawdown,
        "pf": m.profit_factor,
        "calmar": m.calmar_ratio,
    }


def hourly_profile(trades):
    by_h = defaultdict(lambda: [0, 0, 0.0])
    for t in trades:
        if t.pnl is None:
            continue
        h = _utc(t.entry_time).hour
        by_h[h][0] += 1
        if t.pnl < 0:
            by_h[h][1] += 1
        by_h[h][2] += t.pnl
    return by_h


def main():
    params, preset_name = load_preset_params()
    candles = candle_store.load("MNQ", 1)
    if not candles:
        raise SystemExit("no candles in store")

    cid = params.contract_id
    config = BacktestConfig(
        strategies=[_normalize_strategy_name(params.strategy)],
        initial_capital=INITIAL_CAP,
        symbol=_extract_symbol(cid),
        commission_rt=get_commission_rt(cid),
        fees_rt=get_fees_rt(cid),
        value_area_pct=params.value_area_pct,
    )

    span = f"{_utc(candles[0].timestamp):%Y-%m-%d} → {_utc(candles[-1].timestamp):%Y-%m-%d}"
    print(f"preset      : {preset_name}")
    print(f"strategy    : {params.strategy}  sessions={params.tr_allowed_sessions}  x{params.contract_size}")
    print(f"candles     : {len(candles)}  ({span})")
    print(f"defense #1  : skip first {COOLDOWN_MIN} min after session open")
    print(f"defense #2  : stop after {STREAK_STOP} consecutive losses / trade-date")
    print()

    engine = BacktestEngine(config, strategy_params=params)
    result = engine.run(list(candles))
    base = list(result.trades)

    scenarios = {
        "BASELINE":        base,
        "#1 TIME":         filter_time(base),
        "#2 STREAK":       filter_streak(base),
        "#1+#2 COMBINED":  filter_streak(filter_time(base)),
    }

    print(f"{'scenario':<16} {'trades':>7} {'pnl':>11} {'win%':>7} {'maxDD':>9} {'PF':>6} {'calmar':>7}")
    print("-" * 70)
    base_pnl = None
    for name, ts in scenarios.items():
        s = score(ts)
        if base_pnl is None:
            base_pnl = s["pnl"]
        delta = "" if name == "BASELINE" else f"  ({s['pnl'] - base_pnl:+.0f})"
        print(f"{name:<16} {s['trades']:>7} {s['pnl']:>+11.1f} {100*s['win_rate']:>6.1f}% "
              f"{s['maxDD']:>9.1f} {s['pf']:>6.2f} {s['calmar']:>7.2f}{delta}")

    print()
    print("BASELINE hourly profile (UTC hour | trades | loss% | net pnl):")
    prof = hourly_profile(base)
    for h in sorted(prof):
        n, nl, p = prof[h]
        print(f"  {h:02d}:00 | {n:4d} | {100*nl/n:4.0f}% | {p:+9.1f}")

    # ── parameter sweep ───────────────────────────────────────────────────
    print()
    print("PARAMETER SWEEP (find best defense setting, if any):")
    print(f"{'config':<22} {'trades':>7} {'pnl':>11} {'maxDD':>9} {'calmar':>7}")
    print("-" * 60)
    bs = score(base)
    print(f"{'BASELINE':<22} {bs['trades']:>7} {bs['pnl']:>+11.1f} {bs['maxDD']:>9.1f} {bs['calmar']:>7.2f}")
    for cd in (15, 30, 60, 90):
        s = score(filter_time(base, cd))
        print(f"{'#1 cooldown ' + str(cd) + 'm':<22} {s['trades']:>7} {s['pnl']:>+11.1f} {s['maxDD']:>9.1f} {s['calmar']:>7.2f}")
    for ss in (2, 3, 4, 5):
        s = score(filter_streak(base, ss))
        print(f"{'#2 streak ' + str(ss):<22} {s['trades']:>7} {s['pnl']:>+11.1f} {s['maxDD']:>9.1f} {s['calmar']:>7.2f}")
    for cd in (30, 60):
        for ss in (3, 4):
            s = score(filter_streak(filter_time(base, cd), ss))
            print(f"{'#1+#2 ' + str(cd) + 'm/' + str(ss):<22} {s['trades']:>7} {s['pnl']:>+11.1f} {s['maxDD']:>9.1f} {s['calmar']:>7.2f}")


if __name__ == "__main__":
    main()
