"""PI long -> next-session call: premium stop / target grid, with drawdown.

Pre-declared (2026-09-27, before any result was read), same signals, strikes
and market entry as scripts/pi_option_limit_entry_study.py:

  Exit    stop-loss at bid <= (1 - SL) x fill, sold at that observed bid (a
          gap through the stop fills at the gap);  take-profit when the bid
          reaches (1 + TP) x fill, filled at the target;  otherwise the
          next-session 15:30 ET bid.  Watching starts the minute after entry
          (the orders are placed after the fill); invalid/boundary quotes are
          skipped; an economic zero bid is a zero.  Reference: the plain
          next-session 10:00 exit.
  Grid    SL in {none, 20, 30, 40, 50, 60 %}  x  TP in {none, 50, 100, 200, 300 %}
          x strike {ATM, $3, $7, $10 OTM}.  120 cells on 56 signals: the best
          single cell is selection-biased by construction, so the report ranks
          neighbourhoods (plateaus) and robustness, not the maximum.
  Sizing  $1,000 of premium per signal (whole contracts), $1.30 per contract
          round trip; trades in entry order for the equity curve (overlapping
          QQQ/SPY trades are sequenced, a mild understatement of peak risk).
  Report  total, max drawdown, win rate, PF, return distribution (P10/median/
          P90/min/max), total without the 2 best dates, Mar-May vs Jun-Sep,
          and how many eventual big winners a stop cut early.

Run:  python scripts/pi_option_exit_grid_study.py
"""
from __future__ import annotations

import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.backtest.robustness import series_stats  # noqa: E402
from backend.data import market_data  # noqa: E402
from backend.options.pi_options import exit_after  # noqa: E402
from scripts.pi_option_limit_entry_study import (  # noqa: E402
    ET, STRIKES, _exit_bid, _valid_ask, load_signal_paths, load_signals, quote_point)
from scripts.pi_theta_option_pilot import (  # noqa: E402
    CONTRACT_MULTIPLIER_ASSUMPTION, DEFAULT_DATA_ROOT, ROUND_TRIP_COMMISSION_SENSITIVITY)

STOPS = (None, 0.20, 0.30, 0.40, 0.50, 0.60)
TARGETS = (None, 0.50, 1.00, 2.00, 3.00)
CAP = time(15, 30)
REFERENCE_CAP = time(10, 0)
BUDGET = 1000.0
SPLIT = "2026-06-01"


def simulate_exit(path: dict[datetime, dict[str, str]], minutes: list[datetime], fill: float,
                  stop: float | None, target: float | None, cap_at: datetime):
    """``(exit_price, exit_time, reason)`` or None when the cap has no valid bid
    (backend.options.pi_options.exit_after over these minutes)."""
    points = [quote_point(path[t], t) for t in minutes]
    by_epoch = {int(t.timestamp()): t for t in minutes}
    result = exit_after(points, 0, fill, stop, target, int(cap_at.timestamp()))
    if result is None:
        return None
    price, epoch, reason = result
    return price, by_epoch.get(epoch, cap_at), reason


def _label(stop, target):
    return f"SL {'none' if stop is None else f'-{int(stop * 100)}%'} / TP {'none' if target is None else f'+{int(target * 100)}%'}"


def run() -> dict[str, Any]:
    root = DEFAULT_DATA_ROOT
    signals, _skipped = load_signals(root)
    trades: dict[tuple, list[dict]] = defaultdict(list)
    for index, signal in enumerate(signals, 1):
        try:
            _spot, strikes, paths = load_signal_paths(root, signal)
        except ValueError:
            continue
        entry = signal["entry"]
        cohort = signal["cohort"]
        cap_at = datetime.combine(cohort["exit_date"], CAP, tzinfo=ET)
        ref_at = datetime.combine(cohort["exit_date"], REFERENCE_CAP, tzinfo=ET)
        for label in STRIKES:
            path = paths.get(float(strikes[label]), {})
            fill = _valid_ask(path.get(entry), entry)
            if fill is None:
                continue
            minutes = sorted(t for t in path if t > entry)
            contracts = math.floor(BUDGET / (fill * CONTRACT_MULTIPLIER_ASSUMPTION) + 1e-12)
            if contracts < 1:
                continue
            final = _exit_bid(path.get(cap_at))
            cells = [(s, t, cap_at) for s in STOPS for t in TARGETS] + [(None, None, ref_at)]
            for stop, target, cap in cells:
                result = simulate_exit(path, minutes, fill, stop, target, cap)
                if result is None:
                    continue
                price, when, reason = result
                net = ((price - fill) * CONTRACT_MULTIPLIER_ASSUMPTION
                       - ROUND_TRIP_COMMISSION_SENSITIVITY) * contracts
                key = (label, "next 10:00 (plain)" if cap == ref_at else _label(stop, target))
                trades[key].append({
                    "date": cohort["event_date"].isoformat(), "entry": entry.isoformat(),
                    "symbol": cohort["symbol"], "tier": signal["tier"], "fill": fill,
                    "exit": price, "reason": reason, "ret": price / fill - 1, "net": net,
                    "hold_to_close_ret": (final / fill - 1) if final is not None else None,
                })
        print(f"{index}/{len(signals)}", flush=True)
    return summarise(trades)


def summarise(trades: dict[tuple, list[dict]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for (strike, cell), rows in trades.items():
        rows = sorted(rows, key=lambda r: r["entry"])
        pnls = [r["net"] for r in rows]
        stats = series_stats(pnls)
        by_date: dict[str, float] = defaultdict(float)
        for r in rows:
            by_date[r["date"]] += r["net"]
        top = sorted(by_date.values(), reverse=True)
        rets = sorted(r["ret"] for r in rows)
        q = statistics.quantiles(rets, n=10) if len(rets) >= 10 else [math.nan] * 9
        cut_winners = sum(1 for r in rows if r["reason"] == "sl"
                          and r["hold_to_close_ret"] is not None and r["hold_to_close_ret"] >= 1.0)
        out[f"{strike}|{cell}"] = {
            "strike": strike, "cell": cell, "n": stats["n"],
            "total": round(stats["pnl"], 2), "max_dd": round(stats["max_dd"], 2),
            "pf": round(stats["pf"], 3), "win_rate": round(stats["win"], 3),
            "ret_p10": round(q[0], 3), "ret_median": round(statistics.median(rets), 3),
            "ret_p90": round(q[-1], 3), "ret_min": round(rets[0], 3), "ret_max": round(rets[-1], 3),
            "without_best_2_dates": round(stats["pnl"] - sum(top[:2]), 2),
            "mar_may": round(sum(r["net"] for r in rows if r["date"] < SPLIT), 2),
            "jun_sep": round(sum(r["net"] for r in rows if r["date"] >= SPLIT), 2),
            "stopped_then_doubled": cut_winners,
            "exits": {reason: sum(1 for r in rows if r["reason"] == reason) for reason in ("sl", "tp", "time")},
        }
    return out


def main() -> None:
    summary = run()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%MZ")
    out = market_data.derived_path("research") / f"pi_option_exit_grid_study_{stamp}.json"
    out.write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"written: {out}")


if __name__ == "__main__":
    main()
