"""Frozen order-flow filter and PI-exit study for MNQ.

This is research-only.  It compares two entry families:

* ``pi_gated``: the existing PI direction is accepted only when the previous
  five completed minutes contain an opposing aggressive-trade size band.
* ``mbo_responsive``: a live-safe absorption or pressure-turnover event is
  generated from completed MBO bars without consulting PI.

Every family is run independently for no size filter and the exact
``50-99`` / ``100-149`` / ``150+`` bands.  Entries use the next one-minute
bar open, then use the frozen PI asymmetric ATR-blend geometry, the requested
long-ATR/short-time combination, or a 60-minute time exit.  No parameter is
fitted in this file.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.data import market_data
from backend.db.models import get_commission_rt, get_fees_rt, get_point_value
from scripts.orderflow_pi_relationship_study import (
    DISCOVERY_END,
    PI_LEAD_MINUTES,
    _candidate_events,
    _enrich_day,
    _load_days,
    _load_pi,
    _profile,
)


UTC = timezone.utc
TICK_SIZE = 0.25
HORIZON_MINUTES = 60
CONTRACTS = 1
LONG_SL_ATR = 4.0
LONG_TP_ATR = 12.0
SHORT_SL_ATR = 1.5
SHORT_TP_ATR = 4.5
COOLDOWN_MINUTES = 10
FILTERS = ("none", "50_99", "100_149", "150_plus")
POLICIES = ("atr_blend", "long_atr_short_time", "time_60m")
BAND_FIELDS = {
    "50_99": {"buy": "buy_50_99_qty", "sell": "sell_50_99_qty"},
    "100_149": {"buy": "buy_100_149_qty", "sell": "sell_100_149_qty"},
    "150_plus": {"buy": "buy_150_plus_qty", "sell": "sell_150_plus_qty"},
}


def _round_tick(value: float) -> float:
    return round(float(value) / TICK_SIZE) * TICK_SIZE


def _band_qty(bar: dict[str, Any], direction: int, band: str) -> int:
    if band == "none" or direction not in {-1, 1}:
        return 0
    side = "sell" if direction > 0 else "buy"
    field = BAND_FIELDS[band][side]
    value = bar.get(field)
    if value is None:
        # This fallback lets the study read a freshly rebuilt cache even if a
        # caller has not rerun _enrich_day manually.
        index = {
            "50_99": 9 if side == "buy" else 12,
            "100_149": 10 if side == "buy" else 13,
            "150_plus": 11 if side == "buy" else 14,
        }[band]
        return sum(int(cell[index] or 0) for cell in bar.get("cells") or [] if len(cell) > index)
    return int(value or 0)


def _window_has_band(
    bars: list[dict[str, Any]], start: int, end: int, direction: int, band: str,
) -> bool:
    return band == "none" or any(
        _band_qty(bar, direction, band) > 0
        for bar in bars[max(0, start):max(0, end)]
    )


def _atr_history(days: list[dict[str, Any]]) -> list[tuple[int, float]]:
    """Return completed 5-minute true ranges keyed by bucket end epoch."""
    grouped: dict[int, dict[str, float]] = {}
    counts: Counter[int] = Counter()
    for day in days:
        for bar in day["bars"]:
            if bar.get("high") is None or bar.get("low") is None or bar.get("close") is None:
                continue
            epoch = int(bar["epoch"])
            bucket = (epoch // 300) * 300
            row = grouped.get(bucket)
            if row is None:
                row = {
                    "high": float(bar["high"]),
                    "low": float(bar["low"]),
                    "close": float(bar["close"]),
                }
                grouped[bucket] = row
            else:
                row["high"] = max(row["high"], float(bar["high"]))
                row["low"] = min(row["low"], float(bar["low"]))
                row["close"] = float(bar["close"])
            counts[bucket] += 1

    result: list[tuple[int, float]] = []
    previous_bucket: int | None = None
    previous_close: float | None = None
    for bucket in sorted(grouped):
        row = grouped[bucket]
        if previous_bucket is None or bucket - previous_bucket > 300:
            previous_close = None
        tr = max(
            row["high"] - row["low"],
            abs(row["high"] - previous_close) if previous_close is not None else 0.0,
            abs(row["low"] - previous_close) if previous_close is not None else 0.0,
        )
        result.append((bucket + 300, float(tr)))
        previous_bucket = bucket
        previous_close = row["close"]
    return result


def _atr_blend_at(history: list[tuple[int, float]], epoch: int) -> float | None:
    completed = [tr for end, tr in history if end <= epoch]
    if len(completed) < 25:
        return None
    atr14 = statistics.mean(completed[-14:])
    atr50 = statistics.mean(completed[-50:])
    return float((atr14 + atr50) / 2.0) if atr14 > 0 and atr50 > 0 else None


def _simulate(
    bars: list[dict[str, Any]], entry_index: int, direction: int, entry: float,
    sl: float | None, tp: float | None,
) -> dict[str, Any] | None:
    path: list[dict[str, Any]] = []
    for bar in bars[entry_index:min(len(bars), entry_index + HORIZON_MINUTES)]:
        if any(bar.get(field) is None for field in ("open", "high", "low", "close")):
            break
        path.append(bar)
    if not path:
        return None
    has_sl = sl is not None and math.isfinite(float(sl))
    has_tp = tp is not None and math.isfinite(float(tp))
    for elapsed, bar in enumerate(path, 1):
        if direction > 0:
            sl_hit = has_sl and float(bar["low"]) <= float(sl)
            tp_hit = has_tp and float(bar["high"]) >= float(tp)
        else:
            sl_hit = has_sl and float(bar["high"]) >= float(sl)
            tp_hit = has_tp and float(bar["low"]) <= float(tp)
        if not (sl_hit or tp_hit):
            continue
        if sl_hit and tp_hit:
            sl_first = abs(float(bar["open"]) - float(sl)) <= abs(float(bar["open"]) - float(tp))
        else:
            sl_first = bool(sl_hit)
        if sl_first:
            exit_price = min(float(sl), float(bar["open"])) if direction > 0 else max(float(sl), float(bar["open"]))
            reason = "sl"
        else:
            exit_price = float(tp)
            reason = "tp"
        return {"exit_price": exit_price, "exit_reason": reason, "bars_held": elapsed}
    return {
        "exit_price": float(path[-1]["close"]),
        "exit_reason": "time",
        "bars_held": len(path),
    }


def _metrics(trades: list[dict[str, Any]], day_count: int) -> dict[str, Any]:
    pnls = [float(row["net_pnl"]) for row in trades]
    points = [float(row["directional_points"]) for row in trades]
    if not pnls:
        return {
            "trades": 0, "trades_per_day": 0.0, "directional_points": 0.0,
            "net_pnl": 0.0, "max_drawdown": 0.0, "max_drawdown_abs": 0.0,
            "pf": 0.0, "win_rate": None, "qualified": False,
        }
    gains = sum(value for value in pnls if value > 0)
    losses = -sum(value for value in pnls if value < 0)
    equity: list[float] = []
    running = 0.0
    for value in pnls:
        running += value
        equity.append(running)
    peak = 0.0
    drawdowns = []
    for value in equity:
        peak = max(peak, value)
        drawdowns.append(value - peak)
    max_drawdown = min(drawdowns)
    net = sum(pnls)
    pf = gains / losses if losses else (math.inf if gains else 0.0)
    return {
        "trades": len(trades),
        "trades_per_day": round(len(trades) / max(1, day_count), 2),
        "directional_points": round(sum(points), 2),
        "net_pnl": round(net, 2),
        "max_drawdown": round(max_drawdown, 2),
        "max_drawdown_abs": round(abs(max_drawdown), 2),
        "pf": round(pf, 4) if math.isfinite(pf) else "inf",
        "win_rate": round(sum(value > 0 for value in pnls) / len(pnls), 4),
        "gross_profit": round(gains, 2),
        "gross_loss": round(losses, 2),
        "qualified": bool(net > 0 and pf > 1.0),
        "exit_reasons": dict(Counter(row["exit_reason"] for row in trades)),
        "average_bars_held": round(statistics.mean(row["bars_held"] for row in trades), 2),
    }


def _apply_cooldown(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    accepted: list[dict[str, Any]] = []
    last: dict[tuple[str, int], int] = {}
    for row in sorted(rows, key=lambda value: (value["signal_epoch"], value["direction"])):
        key = (str(row["date"]), int(row["direction"]))
        epoch = int(row["signal_epoch"])
        if epoch - last.get(key, -10**12) < COOLDOWN_MINUTES * 60:
            continue
        accepted.append(row)
        last[key] = epoch
    return accepted


def _levels(entry: float, direction: int, atr: float | None, policy: str) -> tuple[float | None, float | None]:
    if policy == "time_60m" or (policy == "long_atr_short_time" and direction < 0):
        return None, None
    if atr is None or not math.isfinite(atr) or atr <= 0:
        return None, None
    if direction > 0:
        return _round_tick(entry - LONG_SL_ATR * atr), _round_tick(entry + LONG_TP_ATR * atr)
    return _round_tick(entry + SHORT_SL_ATR * atr), _round_tick(entry - SHORT_TP_ATR * atr)


def _entry_rows(
    days: list[dict[str, Any]], pi_rows: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    by_date = {day["date"]: day for day in days}
    by_minute: dict[tuple[str, int], tuple[list[dict[str, Any]], int]] = {}
    for day in days:
        for index, bar in enumerate(day["bars"]):
            by_minute[(day["date"], int(bar["epoch"]) // 60)] = (day["bars"], index)

    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for pi in pi_rows:
        dt = datetime.fromisoformat(str(pi["ts"]))
        key = (dt.date().isoformat(), int(dt.timestamp()) // 60)
        located = by_minute.get(key)
        if not located:
            continue
        bars, index = located
        if index + 1 >= len(bars) or bars[index + 1].get("open") is None:
            continue
        direction = int(pi["direction"])
        for band in FILTERS:
            if band != "none" and not _window_has_band(bars, index - PI_LEAD_MINUTES, index, direction, band):
                continue
            result[f"pi_gated::{band}"].append({
                "family": "pi_gated", "rule": "pi", "filter": band,
                "date": key[0], "signal_epoch": int(bars[index]["epoch"]),
                "entry_index": index + 1, "direction": direction,
                "pi_message_id": str(pi["message_id"]), "bars": bars,
            })

    direct_rules = {
        "mbo_absorption": {"bull_absorption", "bear_absorption"},
        "mbo_turnover": {"bull_pressure_flip", "bear_pressure_flip"},
    }
    for day in days:
        bars = day["bars"]
        for index in range(PI_LEAD_MINUTES, len(bars) - 1):
            if bars[index].get("open") is None or bars[index + 1].get("open") is None:
                continue
            events = _candidate_events(bars[index], bars[max(0, index - PI_LEAD_MINUTES):index])
            for rule, names in direct_rules.items():
                for name, direction in events:
                    if name not in names:
                        continue
                    for band in FILTERS:
                        if band != "none":
                            if rule == "mbo_absorption":
                                eligible = _band_qty(bars[index], direction, band) > 0
                            else:
                                eligible = _window_has_band(
                                    bars, index - PI_LEAD_MINUTES, index, direction, band,
                                )
                            if not eligible:
                                continue
                        result[f"{rule}::{band}"].append({
                            "family": rule, "rule": name, "filter": band,
                            "date": day["date"], "signal_epoch": int(bars[index]["epoch"]),
                            "entry_index": index + 1, "direction": direction,
                            "pi_message_id": None, "bars": bars,
                        })
    return dict(result)


def _run_variant(
    rows: list[dict[str, Any]], policy: str, atr_history: list[tuple[int, float]],
    day_count: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    trades: list[dict[str, Any]] = []
    occupied_until: dict[str, int] = {}
    for row in _apply_cooldown(rows):
        entry_index = int(row["entry_index"])
        date_key = str(row["date"])
        if entry_index <= occupied_until.get(date_key, -1):
            continue
        bars = row["bars"]
        entry_bar = bars[entry_index]
        entry = float(entry_bar["open"])
        atr = _atr_blend_at(atr_history, int(entry_bar["epoch"]))
        sl, tp = _levels(entry, int(row["direction"]), atr, policy)
        if (policy == "atr_blend" or (policy == "long_atr_short_time" and int(row["direction"]) > 0)) and sl is None:
            continue
        outcome = _simulate(bars, entry_index, int(row["direction"]), entry, sl, tp)
        if outcome is None:
            continue
        direction = int(row["direction"])
        directional_points = direction * (float(outcome["exit_price"]) - entry)
        gross_pnl = directional_points * get_point_value("MNQ") * CONTRACTS
        costs = (get_commission_rt("MNQ") + get_fees_rt("MNQ")) * CONTRACTS
        trade = {
            "family": row["family"], "rule": row["rule"], "filter": row["filter"],
            "policy": policy, "date": row["date"], "signal_epoch": row["signal_epoch"],
            "direction": direction, "entry_index": entry_index,
            "entry_price": entry, "exit_price": float(outcome["exit_price"]),
            "sl_price": sl, "tp_price": tp, "atr_blend": atr,
            "exit_reason": outcome["exit_reason"], "bars_held": int(outcome["bars_held"]),
            "directional_points": directional_points, "gross_pnl": gross_pnl,
            "costs": costs, "net_pnl": gross_pnl - costs,
            "pi_message_id": row.get("pi_message_id"),
        }
        trades.append(trade)
        occupied_until[date_key] = entry_index + int(outcome["bars_held"]) - 1
    return _metrics(trades, day_count), trades


def run() -> dict[str, Any]:
    cache_root = market_data.derived_path("orderflow", "mnq")
    audit_path = market_data.runtime_path("logs", "pi_live_signals.jsonl")
    days = _load_days(cache_root)
    pi_rows = _load_pi(audit_path)
    if not days:
        return {"status": "no_cache", "results": {}}
    previous_profile: dict[str, float] = {}
    previous_date: datetime | None = None
    for day in days:
        current_date = datetime.fromisoformat(day["date"])
        profile_for_day = previous_profile
        if previous_date is None or (current_date - previous_date).days > 3:
            profile_for_day = {}
        _enrich_day(day, profile_for_day)
        previous_profile = _profile(day["bars"])
        previous_date = current_date
    schema_versions = Counter(day["schema_version"] for day in days)
    if not all(day["schema_version"] >= 5 for day in days):
        return {
            "status": "waiting_for_schema_v5_rebuild",
            "cache_dates": [day["date"] for day in days],
            "cache_schema_versions": dict(schema_versions),
            "results": {},
        }

    entries = _entry_rows(days, pi_rows)
    atr_history = _atr_history(days)
    results: dict[str, Any] = {}
    all_trades: list[dict[str, Any]] = []
    day_count = len(days)
    for name, rows in sorted(entries.items()):
        family, band = name.split("::", 1)
        result = {"family": family, "filter": band, "input_candidates": len(rows), "policies": {}}
        for policy in POLICIES:
            summary, trades = _run_variant(rows, policy, atr_history, day_count)
            discovery = _metrics(
                [trade for trade in trades if trade["date"] <= DISCOVERY_END],
                len({day["date"] for day in days if day["date"] <= DISCOVERY_END}),
            )
            evaluation = _metrics(
                [trade for trade in trades if trade["date"] > DISCOVERY_END],
                len({day["date"] for day in days if day["date"] > DISCOVERY_END}),
            )
            result["policies"][policy] = {
                "all": summary, "discovery": discovery, "evaluation": evaluation,
            }
            all_trades.extend(trades)
        results[name] = result
    return {
        "status": "provisional_research_only",
        "generated_at": datetime.now(UTC).isoformat(),
        "coverage": {
            "rth_days": len(days), "cache_dates": [day["date"] for day in days],
            "cache_schema_versions": dict(schema_versions),
            "pi_unique": len(pi_rows),
            "pi_candidates": sum(len(rows) for name, rows in entries.items() if name.startswith("pi_gated::")),
            "direct_candidates": sum(len(rows) for name, rows in entries.items() if name.startswith("mbo_")),
        },
        "entry_definition": {
            "pi_gated": "PI direction plus an opposing aggressive MBO size-band trade in the previous five completed minutes",
            "mbo_absorption": "completed-bar bull/bear absorption event from fixed research rule plus current-bar size band",
            "mbo_turnover": "completed-bar pressure flip plus previous-five-minute size band",
            "entry": "next one-minute bar open; one position at a time; 10-minute per-direction cooldown",
        },
        "exit_definition": {
            "atr_blend": "long SL 4x ATR-blend / TP 12x; short SL 1.5x / TP 4.5x; completed 5-minute ATR14/ATR50 blend",
            "long_atr_short_time": "long uses the fixed ATR-blend SL/TP; short has no SL/TP and exits after 60 one-minute bars or RTH session cache end",
            "time_60m": "no SL/TP; close at 60 one-minute bars or RTH session cache end",
            "cost": "one MNQ round-turn; canonical commission + fees",
        },
        "results": results,
        "trades": all_trades,
        "warnings": [
            "This is not a live recommendation and does not identify institutions from anonymous MBO.",
            "OHLC replay resolves a same-minute SL/TP collision conservatively by distance from the bar open.",
            "Final daily high/low is not used for entry; only the separate relationship study labels hindsight proximity.",
        ],
    }


def _markdown(result: dict[str, Any]) -> str:
    lines = [
        "# MNQ order-flow filters × PI exits",
        "",
        f"Status: **{result.get('status')}**",
        f"Generated: `{result.get('generated_at', '—')}`",
        "",
    ]
    if result.get("status") != "provisional_research_only":
        lines.extend([
            f"Cache dates: `{result.get('cache_dates', [])}`",
            f"Cache schema versions: `{result.get('cache_schema_versions', {})}`",
            "",
            "The study is intentionally not run against old caches because exact per-price size bands and corrected MBO book semantics are required.",
        ])
        return "\n".join(lines) + "\n"
    coverage = result["coverage"]
    lines.extend([
        f"RTH days: **{coverage['rth_days']}**; PI rows: **{coverage['pi_unique']}**; "
        f"PI candidates: **{coverage['pi_candidates']}**; direct candidates: **{coverage['direct_candidates']}**",
        "",
        "All results use the next one-minute bar open, one position at a time, a 10-minute per-direction cooldown, one MNQ, and canonical round-turn cost.",
        "Exit policies: `atr_blend` = fixed ATR-blend geometry for both sides; `long_atr_short_time` = fixed ATR-blend for longs and 60-minute time exit for shorts; `time_60m` = 60-minute time exit for both sides.",
        "",
        "| Family | Filter | Exit | N | Points | Net P&L | Max DD (abs) | PF | Win | Pass (net>0 & PF>1) |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ])
    for name, row in result["results"].items():
        for policy, policy_result in row["policies"].items():
            metric = policy_result["all"]
            lines.append(
                f"| {row['family']} | {row['filter']} | {policy} | {metric['trades']} | "
                f"{metric['directional_points']} | ${metric['net_pnl']} | -${metric['max_drawdown_abs']} | "
                f"{metric['pf']} | {metric['win_rate'] if metric['win_rate'] is not None else '—'} | "
                f"{'YES' if metric['qualified'] else 'NO'} |"
            )
    lines.extend([
        "",
        "## Evaluation only",
        "",
        f"Discovery ends at **{DISCOVERY_END}**. The evaluation column is the only one to use for a new candidate decision.",
        "",
        "| Family | Filter | Exit | N | Net P&L | Max DD (abs) | PF | Pass |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ])
    for name, row in result["results"].items():
        for policy, policy_result in row["policies"].items():
            metric = policy_result["evaluation"]
            lines.append(
                f"| {row['family']} | {row['filter']} | {policy} | {metric['trades']} | "
                f"${metric['net_pnl']} | -${metric['max_drawdown_abs']} | {metric['pf']} | "
                f"{'YES' if metric['qualified'] else 'NO'} |"
            )
    qualified = [
        (row["family"], row["filter"], policy, policy_result["evaluation"])
        for row in result["results"].values()
        for policy, policy_result in row["policies"].items()
        if policy_result["evaluation"]["qualified"]
    ]
    lines.extend(["", "### Evaluation candidates passing net > 0 and PF > 1", ""])
    if qualified:
        lines.extend([
            "| Family | Filter | Exit | N | Net P&L | Max DD (abs) | PF |",
            "|---|---|---:|---:|---:|---:|---:|",
        ])
        for family, band, policy, metric in qualified:
            lines.append(
                f"| {family} | {band} | {policy} | {metric['trades']} | "
                f"${metric['net_pnl']} | -${metric['max_drawdown_abs']} | {metric['pf']} |"
            )
    else:
        lines.append("No evaluation row passes both conditions; no candidate is promoted.")
    lines.extend([
        "",
        "## Interpretation",
        "",
        "A size band is a filter on an executed MBO trade at a price, not proof of an institution. "
        "The responsive interpretation requires price failure/turnover after the attack; a large trade without that sequence is continuation risk.",
        "",
        "The ATR-blend and 60-minute rows answer different questions: the first tests the current PI risk geometry, while the second tests whether direction has value without stop/target geometry. "
        "A high PF with very few trades is not promoted without evaluation support.",
        "",
        "Source methodology: [Databento MBO](https://databento.com/docs/schemas-and-data-formats/mbo), "
        "[CME MBO FAQ](https://www.cmegroup.com/articles/faqs/market-by-order-mbo.html), "
        "[Condition–Zone–Trigger](https://jesserogerstrading.com/czt/what-is-condition-zone-trigger), "
        "[Order flow / effort versus result](https://jesserogerstrading.com/order-flow).",
    ])
    return "\n".join(lines) + "\n"


def _write_trades(path: Path, trades: list[dict[str, Any]]) -> None:
    if not trades:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(trades[0])
    with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(trades)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-stem", default="orderflow_filter_exit_study")
    args = parser.parse_args()
    result = run()
    output_dir = market_data.derived_path("research")
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / f"{args.output_stem}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (output_dir / f"{args.output_stem}.md").write_text(
        _markdown(result), encoding="utf-8",
    )
    if result.get("trades"):
        _write_trades(output_dir / f"{args.output_stem}_trades.csv.gz", result["trades"])
    print(output_dir / f"{args.output_stem}.md")
    print(json.dumps(result.get("coverage", result), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
