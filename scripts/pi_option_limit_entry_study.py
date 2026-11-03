"""PI long signal -> next-session-expiry call: which strike, and does a resting
limit below the signal-time ask beat buying at the ask?

Pre-declared (2026-09-27, before any result was read):

  Signals   canonical PI long marks of kind 深蓝圈 (LV2) and 青π (LV3) on QQQ/SPY,
            the first such mark per symbol and day (one decision per chart),
            inside the saved 09:30-16:00 option-quote window.
  Contract  the saved cohort expiry (next listed session); strikes ATM and the
            lowest listed call >= spot + $3 / $7 / $10 at the entry minute
            (pi_theta_option_pilot._entry_selection).
  Entry     MKT: the entry-minute ask (signal + 1 minute).
            LMT -20/-40/-60%: a buy limit at (1 - x) x that ask, rounded down
            to $0.01, working from the next minute until one minute before the
            exit; filled at the limit on the first minute whose valid ask <= limit.
  Exit      next-session 10:00 ET bid and 15:30 ET bid (a fill after the exit
            time is no trade for that exit); an economic zero bid is a full
            premium loss; $1.30 round trip per contract.
  Report    per cell: fill rate, filled-trade PnL / PF / win rate / return on
            premium, a fixed $1,000 premium budget, and what MKT made on the
            SAME signals (so a limit's result is never compared with a
            different set of days); LV2 and LV3 separately; the best market date
            removed.

Minute snapshots cannot see intra-minute prints or queue position, so limit
fills are approximate (a dip shorter than a minute is invisible).  Retrospective
source-time study on ~7 months of signals; not a live-execution result.

Run:  python scripts/pi_option_limit_entry_study.py
"""
from __future__ import annotations

import json
import math
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
from scripts.pi_theta_cohort_replay import ET, load_month_cohorts, mark_timing  # noqa: E402
from scripts.pi_theta_exit_path_study import _entry_ask_quote  # noqa: E402
from scripts.pi_theta_option_pilot import (  # noqa: E402
    CONTRACT_MULTIPLIER_ASSUMPTION,
    DEFAULT_DATA_ROOT,
    ROUND_TRIP_COMMISSION_SENSITIVITY,
    _bid_mark_quality,
    _entry_selection,
    _number,
    _quote_file,
)
from scripts.theta_pi_cohort_ingest import cohort_requests  # noqa: E402
from backend.options.pi_options import limit_fill_index  # noqa: E402
from backend.options.pi_options import limit_price as _limit_price  # noqa: E402

MONTHS = ("2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09")
KINDS = {"深蓝圈": "LV2", "青π": "LV3"}
OFFSETS = (3.0, 7.0, 10.0)
STRIKES = ("atm",) + tuple(f"otm_{o:g}" for o in OFFSETS)
LIMITS = (0.20, 0.40, 0.60)
ENTRIES = ("MKT",) + tuple(f"LMT-{int(x * 100)}%" for x in LIMITS)
EXITS = {"next_10:00": time(10, 0), "next_15:30": time(15, 30)}
BUDGET = 1000.0


def load_signals(root: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    signals: list[dict[str, Any]] = []
    skipped: dict[str, int] = defaultdict(int)
    for month in MONTHS:
        try:
            _all, cohorts, _meta = load_month_cohorts(root, month)
        except (ValueError, FileNotFoundError):
            skipped[f"no_cohorts_{month}"] += 1
            continue
        for cohort in cohorts:
            timed = []
            for mark in cohort["marks"]:
                if int(mark["direction"]) != 1 or mark.get("kind") not in KINDS:
                    continue
                entry = mark_timing(mark)["entry_timestamp_et"]
                if entry is None:
                    skipped["outside_quote_window"] += 1
                    continue
                timed.append((datetime.fromisoformat(entry), mark))
            if timed:
                entry, mark = min(timed, key=lambda item: item[0])
                signals.append({"cohort": cohort, "mark": mark, "entry": entry,
                                "tier": KINDS[mark["kind"]]})
    return signals, dict(skipped)


def _valid_ask(row: dict[str, str] | None, stamp: datetime) -> float | None:
    if row is None:
        return None
    _, reasons = _entry_ask_quote({stamp: row}, stamp)
    ask = _number(row.get("ask"))
    size = _number(row.get("ask_size"))
    if reasons or ask is None or ask <= 0 or not size or size <= 0:
        return None
    return ask


def _exit_bid(row: dict[str, str] | None) -> float | None:
    if row is None:
        return None
    mark, _executable, _zero, _no_exec, values = _bid_mark_quality(row)
    return float(values["bid"]) if mark else None


def quote_point(row: dict[str, str], stamp: datetime):
    """One minute as ``backend.options.pi_options.Point`` using the Theta
    quality rules (buyable ask / valid economic bid)."""
    ask = _valid_ask(row, stamp)
    bid = _exit_bid(row)
    return (int(stamp.timestamp()), bid, ask, ask is not None, bid is not None)


def limit_price(ask0: float, discount: float) -> float:
    """Buy limit ``discount`` below the entry ask (backend rule)."""
    return _limit_price(ask0, discount)


def limit_fill_time(path: dict[datetime, dict[str, str]], minutes: list[datetime],
                    limit: float, exit_at: datetime) -> datetime | None:
    """First minute before ``exit_at`` whose buyable ask is <= ``limit``
    (backend.options.pi_options.limit_fill_index over these minutes)."""
    points = [quote_point(path[t], t) for t in minutes]
    index = limit_fill_index(points, 0, limit, int(exit_at.timestamp()))
    return minutes[index] if index is not None else None


def load_signal_paths(root: Path, signal: dict[str, Any], offsets: tuple = OFFSETS):
    """Strikes chosen at the entry minute and every saved CALL quote for them
    from the signal day through the exit day: ``(spot, strikes, paths)``.
    Raises ValueError when the entry chain lacks a synchronised spot/strike."""
    cohort, entry = signal["cohort"], signal["entry"]
    requests = {(r["key"], r["request_date"]): r for r in cohort_requests(cohort)}
    greeks = root / requests[("greeks_first_order_1m", cohort["event_date"])]["relative_path"]
    spot, strikes, _details, _greeks = _entry_selection(
        greeks, entry_time=entry, expiration=cohort["expiration"].isoformat(),
        otm_offsets=offsets)
    wanted = {float(v) for v in strikes.values()}
    paths: dict[float, dict[datetime, dict[str, str]]] = defaultdict(dict)
    for day in (cohort["event_date"], cohort["exit_date"]):
        _diag, rows = _quote_file(root / requests[("quote_1m", day)]["relative_path"], wanted)
        for (strike, _right), by_time in rows.items():
            paths[strike].update(by_time)
    return spot, strikes, paths


def replay_signal(root: Path, signal: dict[str, Any]) -> dict[str, Any]:
    cohort, entry = signal["cohort"], signal["entry"]
    base = {"symbol": cohort["symbol"], "date": cohort["event_date"].isoformat(),
            "tier": signal["tier"], "entry_et": entry.isoformat(), "cells": {}}
    try:
        spot, strikes, paths = load_signal_paths(root, signal)
    except ValueError as exc:
        return {**base, "status": f"strike_selection:{exc}"}
    base.update(spot=spot, strikes={k: float(v) for k, v in strikes.items()})
    for label in STRIKES:
        strike = float(strikes[label])
        path = paths.get(strike, {})
        ask0 = _valid_ask(path.get(entry), entry)
        if ask0 is None:
            base["cells"][label] = {"status": "no_valid_entry_ask"}
            continue
        minutes = sorted(t for t in path if t > entry)
        per_exit: dict[str, Any] = {}
        for exit_name, exit_time in EXITS.items():
            exit_at = datetime.combine(cohort["exit_date"], exit_time, tzinfo=ET)
            bid = _exit_bid(path.get(exit_at))
            if bid is None:
                per_exit[exit_name] = {"status": "no_exit_mark"}
                continue
            fills: dict[str, Any] = {"MKT": {"fill": ask0, "at": entry.isoformat()}}
            for x in LIMITS:
                limit = limit_price(ask0, x)
                hit = limit_fill_time(path, minutes, limit, exit_at)
                fills[f"LMT-{int(x * 100)}%"] = (
                    {"fill": limit, "at": hit.isoformat()} if hit else None)
            outcome = {}
            for name, fill in fills.items():
                if fill is None:
                    outcome[name] = None
                    continue
                price = fill["fill"]
                net = (bid - price) * CONTRACT_MULTIPLIER_ASSUMPTION - ROUND_TRIP_COMMISSION_SENSITIVITY
                contracts = math.floor(BUDGET / (price * CONTRACT_MULTIPLIER_ASSUMPTION) + 1e-12)
                outcome[name] = {
                    "fill": price, "at": fill["at"], "exit_bid": bid,
                    "net": round(net, 2), "ret": round(bid / price - 1, 4),
                    "budget_net": round(contracts * net, 2), "contracts": contracts,
                    "next_day_fill": datetime.fromisoformat(fill["at"]).date() != entry.date(),
                }
            per_exit[exit_name] = {"status": "ok", "ask0": ask0, "outcomes": outcome}
        base["cells"][label] = {"status": "ok", "strike": strike, "ask0": ask0, "exits": per_exit}
    base["status"] = "ok"
    return base


def summarise(cases: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for tier in ("ALL", "LV2", "LV3"):
        chosen = [c for c in cases if c.get("status") == "ok" and (tier == "ALL" or c["tier"] == tier)]
        for label in STRIKES:
            for exit_name in EXITS:
                rows = []
                for case in chosen:
                    cell = case["cells"].get(label, {})
                    ex = (cell.get("exits") or {}).get(exit_name, {})
                    if ex.get("status") == "ok":
                        rows.append((case, ex["outcomes"]))
                for entry in ENTRIES:
                    filled = [(c, o[entry]) for c, o in rows if o.get(entry)]
                    missed = [(c, o) for c, o in rows if not o.get(entry)]
                    pnls = [f["net"] for _c, f in filled]
                    stats = series_stats(pnls)
                    by_date: dict[str, float] = defaultdict(float)
                    for c, f in filled:
                        by_date[c["date"]] += f["net"]
                    best = max(by_date, key=by_date.get) if by_date else None
                    same_mkt = sum(o["MKT"]["net"] for c, o in rows if o.get(entry))
                    out[f"{tier}|{label}|{exit_name}|{entry}"] = {
                        "signals": len(rows),
                        "fills": len(filled),
                        "fill_rate": round(len(filled) / len(rows), 4) if rows else None,
                        "next_day_fills": sum(1 for _c, f in filled if f["next_day_fill"]),
                        "pnl": round(stats["pnl"], 2),
                        "pf": round(stats["pf"], 4),
                        "win_rate": round(stats["win"], 4),
                        "avg_return": round(sum(f["ret"] for _c, f in filled) / len(filled), 4) if filled else None,
                        "budget_pnl": round(sum(f["budget_net"] for _c, f in filled), 2),
                        "pnl_without_best_date": round(stats["pnl"] - (by_date.get(best, 0.0) if best else 0.0), 2),
                        "best_date": best,
                        "mkt_pnl_same_filled_signals": round(same_mkt, 2),
                        "mkt_pnl_on_missed_signals": round(sum(o["MKT"]["net"] for _c, o in missed), 2),
                        "missed_mkt_winners": sum(1 for _c, o in missed if o["MKT"]["net"] > 0),
                    }
    return out


def main() -> None:
    root = DEFAULT_DATA_ROOT
    signals, skipped = load_signals(root)
    cases = []
    for index, signal in enumerate(signals, 1):
        cases.append(replay_signal(root, signal))
        print(f"{index}/{len(signals)} {signal['cohort']['symbol']} {signal['entry']:%Y-%m-%d %H:%M} "
              f"{signal['tier']} {cases[-1]['status']}", flush=True)
    summary = summarise(cases)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%MZ")
    out = market_data.derived_path("research") / f"pi_option_limit_entry_study_{stamp}.json"
    out.write_text(json.dumps({"skipped": skipped, "cases": cases, "summary": summary},
                              indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"written: {out}")


if __name__ == "__main__":
    main()
