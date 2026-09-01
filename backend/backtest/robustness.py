"""Robustness evaluation: Monte Carlo, walk-forward, series stats.

1.0.10p: these lived only in `frontend/static/ancserTPX.js` (`_robMonteCarlo`,
`_robWalkForward`, `_robSeriesStats`).  Three consequences of that:

  * the sweep pipeline could not use them — a sweep variant had no Monte Carlo
    number to gate on, so `_annotate_plateau_and_acceptance` had to judge
    robustness from PF and walk-forward alone;
  * research agents could not use them either, so each one reimplemented a
    bootstrap in its own script — the same divergence that let PI accumulate
    nine private simulation loops;
  * pytest could not reach them.  Only the Chromium suite could, and only
    through the rendered panel.

Walk-forward additionally existed TWICE, here and at `sweep.py`'s three-way
date split, with nothing keeping the two definitions in step.  This module is
now the single implementation; `sweep.py` keeps its own inline split because it
works on day-keyed aggregates rather than trades, and `test_robustness.py`
pins the two against each other.

One deliberate behaviour change: the browser used `Math.random()`, so the same
trades produced different Monte Carlo percentiles on every render and no result
was reproducible or reviewable.  `monte_carlo()` takes an explicit seed and
defaults to a fixed one.
"""
from __future__ import annotations

import random
from typing import Iterable, List, Optional, Sequence

# Matches the frontend constant of the same name; a "month" for normalising
# P&L across runs of different lengths.  Total P&L is not comparable — seven
# months of $5,000 and two months of $5,000 are different things.
DAYS_PER_MONTH = 30.44

# Percentile ladder reported for every bootstrap.  The middle band is kept
# explicit because the Research panel renders P25–P75 as the light band.
_PCTS = (0.05, 0.25, 0.50, 0.75, 0.95)

# A profit factor with zero losing trades is unbounded; the frontend reports
# this sentinel rather than infinity so the value stays JSON-serialisable.
PF_NO_LOSSES = 999.0

DEFAULT_ITERS = 1000
DEFAULT_SEED = 20261010


def _pf(gain: float, loss: float) -> float:
    if loss > 0:
        return gain / loss
    return PF_NO_LOSSES if gain > 0 else 0.0


def series_stats(pnls: Sequence[float]) -> dict:
    """Port of `_robSeriesStats`: equity walk over an ordered P&L series."""
    gain = loss = eq = peak = dd = 0.0
    wins = 0
    for raw in pnls:
        p = float(raw or 0.0)
        if p > 0:
            gain += p
            wins += 1
        else:
            loss += -p
        eq += p
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    n = len(pnls)
    return {
        "n": n,
        "pnl": eq,
        "pf": _pf(gain, loss),
        "max_dd": dd,
        "win": (wins / n) if n else 0.0,
    }


def _quantile(ordered: List[float], p: float) -> float:
    if not ordered:
        return 0.0
    idx = round(p * (len(ordered) - 1))
    return ordered[min(len(ordered) - 1, max(0, int(idx)))]


def _equity_curve(pnls: Sequence[float]) -> List[dict]:
    """Return the cumulative P&L and running max drawdown for a sequence."""
    equity = peak = drawdown = 0.0
    curve = [{"step": 0, "pnl": 0.0, "max_dd": 0.0}]
    for step, raw in enumerate(pnls, 1):
        equity += float(raw or 0.0)
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        curve.append({"step": step, "pnl": equity, "max_dd": drawdown})
    return curve


def _percentile_curve(paths: Sequence[Sequence[float]]) -> List[dict]:
    """Collapse bootstrap paths into a P5/P25/P50/P75/P95 point per step."""
    if not paths:
        return []
    length = max(len(path) for path in paths)
    curve = []
    for step in range(length):
        values = sorted(path[step] for path in paths if step < len(path))
        point = {"step": step}
        for pct in _PCTS:
            point[f"p{int(pct * 100)}"] = _quantile(values, pct)
        curve.append(point)
    return curve


def monte_carlo(
    pnls: Sequence[float],
    iters: int = DEFAULT_ITERS,
    seed: Optional[int] = DEFAULT_SEED,
    dd_threshold: float = 2000.0,
) -> Optional[dict]:
    """Bootstrap the trade sequence; resample WITH replacement, n draws.

    Returns None below 10 trades — the frontend's guard, kept because a
    bootstrap of a handful of trades reports a confident-looking distribution
    built from almost no information.

    `dd_threshold` is the drawdown the caller cares about breaching (Topstep's
    MLL for the account being evaluated), reported as `p_dd_breach`.
    """
    n = len(pnls)
    if n < 10:
        return None
    values = [float(p or 0.0) for p in pnls]
    rng = random.Random(seed)
    totals: List[float] = []
    dds: List[float] = []
    pfs: List[float] = []
    pnl_paths: List[List[float]] = []
    dd_paths: List[List[float]] = []
    for _ in range(int(iters)):
        eq = peak = dd = gain = loss = 0.0
        pnl_path = [0.0]
        dd_path = [0.0]
        for _ in range(n):
            p = values[rng.randrange(n)]
            if p > 0:
                gain += p
            else:
                loss += -p
            eq += p
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
            pnl_path.append(eq)
            dd_path.append(dd)
        totals.append(eq)
        dds.append(dd)
        pfs.append(_pf(gain, loss))
        pnl_paths.append(pnl_path)
        dd_paths.append(dd_path)
    totals.sort()
    dds.sort()
    pfs.sort()
    return {
        "iters": int(iters),
        "seed": seed,
        "n": n,
        "pnl_p5": _quantile(totals, 0.05),
        "pnl_p25": _quantile(totals, 0.25),
        "pnl_p50": _quantile(totals, 0.50),
        "pnl_p75": _quantile(totals, 0.75),
        "pnl_p95": _quantile(totals, 0.95),
        "p_loss": sum(1 for v in totals if v <= 0) / len(totals),
        "dd_p5": _quantile(dds, 0.05),
        "dd_p25": _quantile(dds, 0.25),
        "dd_p50": _quantile(dds, 0.50),
        "dd_p75": _quantile(dds, 0.75),
        "dd_p95": _quantile(dds, 0.95),
        "p_dd_breach": sum(1 for v in dds if v > dd_threshold) / len(dds),
        "dd_threshold": dd_threshold,
        "pf_p5": _quantile(pfs, 0.05),
        # Keep the scalar fields above for gates and reports; these paths are
        # the same seeded replays, collapsed point-by-point for the UI chart.
        "pnl_curve": _percentile_curve(pnl_paths),
        "dd_curve": _percentile_curve(dd_paths),
    }


def monte_carlo_passes(mc: Optional[dict]) -> bool:
    """Port of `_robMcPass`. All three gates, not any of them."""
    if not mc:
        return False
    return (mc["p_loss"] <= 0.05
            and mc["dd_p95"] < mc["dd_threshold"]
            and mc["pf_p5"] > 1.0)


def segment_index(offset: float, span: float, segments: int = 3) -> int:
    """Which walk-forward bucket a point `offset` into a `span` belongs to.

    1.1.1: THE single definition of the split. It previously existed twice —
    inline in sweep.py (day-keyed aggregates, since 1.0.8g) and again here
    (trade dicts) — with nothing but a string-matching test claiming the two
    agreed. They were never checked against each other on actual numbers, so
    "walk-forward" could have meant two different things depending on whether
    you ran a sweep or opened the RESEARCH panel.

    The clamp is load-bearing: the last point sits exactly at `span`, which
    divides to `segments` and would index one past the end.
    """
    if span <= 0:
        return 0
    return min(segments - 1, int(offset * segments / span))


def segment_day_span(day_keys: Sequence[str], segments: int = 3):
    """Bucket ISO date strings into equal date spans; yields (index, key).

    Span is counted in whole days INCLUSIVE of both ends (`+ 1`), matching the
    sweep's original arithmetic — a one-day run is a span of 1, not 0.
    """
    from datetime import date as _date
    keys = sorted(day_keys)
    if not keys:
        return
    d0 = _date.fromisoformat(keys[0])
    span_days = max(1, (_date.fromisoformat(keys[-1]) - d0).days + 1)
    for key in day_keys:
        off = (_date.fromisoformat(key) - d0).days
        yield segment_index(off, span_days, segments), key


def walk_forward(trades: Iterable[dict], segments: int = 3) -> Optional[dict]:
    """Split trades into equal DATE spans (not equal counts) and score each.

    Equal spans, not equal trade counts: a strategy that stopped producing
    trades halfway through must fail, and equal-count segments would hide that
    by stretching the last segment over the dead half.
    """
    rows = [t for t in trades if t.get("entry_time")]
    if len(rows) < 2 * segments:
        return None
    stamps = [_epoch(t["entry_time"]) for t in rows]
    if any(s is None for s in stamps):
        return None
    t0, t1 = min(stamps), max(stamps)
    span = max(1.0, t1 - t0)
    buckets: List[List[float]] = [[] for _ in range(segments)]
    for trade, stamp in zip(rows, stamps):
        buckets[segment_index(stamp - t0, span, segments)].append(
            float(trade.get("pnl") or 0.0))
    stats = [series_stats(b) for b in buckets]
    equity = peak = drawdown = 0.0
    curve = [{"step": 0, "segment": 0, "pnl": 0.0, "max_dd": 0.0}]
    for step, (trade, stamp) in enumerate(
            sorted(zip(rows, stamps), key=lambda item: item[1]), 1):
        equity += float(trade.get("pnl") or 0.0)
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        curve.append({
            "step": step,
            "segment": segment_index(stamp - t0, span, segments) + 1,
            "pnl": equity,
            "max_dd": drawdown,
        })
    return {
        "segments": stats,
        "pass": all(s["n"] > 0 and s["pnl"] > 0 and s["pf"] > 1.0 for s in stats),
        "curve": curve,
    }


def _epoch(value) -> Optional[float]:
    """Seconds since epoch from a datetime, an ISO string, or a number.

    The numeric case matters: callers that already resolved timestamps pass
    them straight back in, and silently returning None for those turned into a
    missing `monthly_pnl` rather than an error.
    """
    from datetime import datetime
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return value.timestamp()
    try:
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return None


def monthly_pnl(total_pnl: float, start, end) -> Optional[float]:
    """Normalise total P&L to a 30.44-day month so runs stay comparable."""
    a, b = _epoch(start), _epoch(end)
    if a is None or b is None:
        return None
    days = (b - a) / 86400.0
    if days < 1:
        return None
    return float(total_pnl) / (days / DAYS_PER_MONTH)


# Dollar value of one point, per contract. Mirrors _ROB_POINT_VALUE in the
# frontend; MNQ is the default because it is what the account trades.
POINT_VALUE = {"MNQ": 2, "NQ": 20, "ENQ": 20, "MES": 5, "ES": 50,
               "MGC": 10, "GC": 100, "ZL": 600}
TICK = 0.25
DEFAULT_SLIP_LEVELS = (1, 2, 4, 8)


def tick_value(symbol: Optional[str]) -> float:
    """Dollars per tick per contract for the traded symbol."""
    key = str(symbol or "/MNQ").replace("/", "").upper()
    return POINT_VALUE.get(key, POINT_VALUE["MNQ"]) * TICK


def slip_injection(trades: Sequence[dict],
                   levels: Sequence[int] = DEFAULT_SLIP_LEVELS,
                   symbol: Optional[str] = None) -> dict:
    """Re-score the run with N ticks of round-trip slip charged per contract.

    Answers "does the edge survive a worse fill than the backtest assumed?".
    Size matters: a 2-contract trade eats the slip twice.
    """
    pnls = [float(t.get("pnl") or 0.0) for t in trades]
    sizes = [max(1.0, float(t.get("size") or 1)) for t in trades]
    sym = symbol or (trades[0].get("symbol") if trades else None)
    tick = tick_value(sym)
    out = []
    for level in sorted({int(v) for v in levels}):
        charged = [p - level * tick * s for p, s in zip(pnls, sizes)]
        out.append({"level": level, "stats": series_stats(charged),
                    "curve": _equity_curve(charged)})
    return {"tick_value": tick, "symbol": sym, "base_curve": _equity_curve(pnls),
            "levels": out}


def evaluate(trades: Sequence[dict], *, iters: int = DEFAULT_ITERS,
             seed: Optional[int] = DEFAULT_SEED,
             dd_threshold: float = 2000.0,
             slip_levels: Sequence[int] = DEFAULT_SLIP_LEVELS) -> dict:
    """One call the API, the sweep, and research scripts all share."""
    pnls = [float(t.get("pnl") or 0.0) for t in trades]
    mc = monte_carlo(pnls, iters=iters, seed=seed, dd_threshold=dd_threshold)
    wf = walk_forward(trades)
    stamps = [s for s in (_epoch(t.get("entry_time")) for t in trades) if s]
    span_months = None
    if len(stamps) >= 2:
        span_months = (max(stamps) - min(stamps)) / 86400.0 / DAYS_PER_MONTH
    return {
        "trades": len(trades),
        "stats": series_stats(pnls),
        "monte_carlo": mc,
        "monte_carlo_pass": monte_carlo_passes(mc),
        "walk_forward": wf,
        "monthly_pnl": (monthly_pnl(sum(pnls), min(stamps), max(stamps))
                        if len(stamps) >= 2 else None),
        "span_months": span_months,
        "start": min(stamps) if stamps else None,
        "end": max(stamps) if stamps else None,
        "slip": slip_injection(trades, slip_levels),
    }
