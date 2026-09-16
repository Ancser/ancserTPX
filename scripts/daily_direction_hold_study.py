"""Causal one-entry-per-day direction/hold diagnostic for MNQ.

This is an offline research study, not a production strategy.  It answers a
narrow question: if a trader is allowed one decision at a known intraday time
and then holds until the RTH close (or the engine's pre-flatten boundary), is
the direction predictable enough to overcome costs?

Every feature is calculated from bars at or before the entry bar:

* current RTH VWAP side;
* first 15-minute opening-range break;
* direction from the RTH open to entry;
* previous completed RTH direction and previous high/low break;
* agreement filters that go flat when the signals disagree.

The matrix and date range are declared in this file before inspecting the
results.  The random baseline is a distribution of equally likely long/short
assignments over the same realized daily moves; it is a diagnostic, not a
claim that one particular random seed is a tradable system.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.backtest.prop_intraday_research import (  # noqa: E402
    RthSession,
    load_symbol_sessions,
)
from backend.backtest.robustness import series_stats  # noqa: E402
from backend.data import market_data  # noqa: E402
from backend.db.models import (  # noqa: E402
    current_quarterly_contract_id,
    get_commission_rt,
    get_fees_rt,
    get_point_value,
    get_tick_size,
    get_tick_value,
)
from backend.timebase import as_utc  # noqa: E402


ENTRY_OFFSETS = (30, 60, 90, 120, 180, 240)
EXIT_OFFSETS = {
    # 09:30 + 374 minutes = 15:44 ET, the last minute before the shared
    # 15:45 close phase begins.
    "pre_flatten": 374,
    # 09:30 + 389 minutes = 15:59 ET, the final regular-session minute.
    "rth_close": 389,
}
OPENING_RANGE_MINUTES = 15
STRESS_TICKS = 14
RANDOM_ITERS = 2000
RANDOM_SEED = 20260914


@dataclass(frozen=True)
class DailyObservation:
    session_date: date
    entry_offset: int
    entry_time: datetime
    entry_price: float
    exit_prices: dict[str, float]
    rth_open: float
    vwap: float
    opening_range_high: float
    opening_range_low: float
    previous_day_direction: int
    previous_high: Optional[float]
    previous_low: Optional[float]


def _sign(value: float, tolerance: float = 0.0) -> int:
    value = float(value)
    if value > tolerance:
        return 1
    if value < -tolerance:
        return -1
    return 0


def _minute_offset(ts: datetime) -> int:
    """Return minutes after the 09:30 ET RTH open."""

    return (ts.hour * 60 + ts.minute) - (9 * 60 + 30)


def _previous_levels(session: Optional[RthSession]) -> tuple[Optional[float], Optional[float], int]:
    if session is None or not session.bars:
        return None, None, 0
    opening = float(session.bars[0].open)
    closing = float(session.bars[-1].close)
    high = max(float(bar.high) for bar in session.bars)
    low = min(float(bar.low) for bar in session.bars)
    return high, low, _sign(closing - opening)


def build_observations(sessions: Iterable[RthSession]) -> list[DailyObservation]:
    """Build only complete, causal entry/exit observations."""

    ordered = list(sessions)
    observations: list[DailyObservation] = []
    for session_index, session in enumerate(ordered):
        if not session.bars:
            continue
        indexes = {
            _minute_offset(session.et_times[index]): index
            for index in range(len(session.bars))
        }
        previous = ordered[session_index - 1] if session_index else None
        previous_high, previous_low, previous_direction = _previous_levels(previous)
        opening_indexes = [indexes.get(offset) for offset in range(OPENING_RANGE_MINUTES)]
        if any(index is None for index in opening_indexes):
            continue
        opening_bars = [session.bars[index] for index in opening_indexes if index is not None]
        opening_range_high = max(float(bar.high) for bar in opening_bars)
        opening_range_low = min(float(bar.low) for bar in opening_bars)
        rth_open = float(session.bars[0].open)
        exit_indexes = {
            name: indexes.get(offset)
            for name, offset in EXIT_OFFSETS.items()
        }
        # This avoids silently turning an incomplete day into a different exit
        # definition.  load_symbol_sessions already requires the 15:50 bar.
        if any(index is None for index in exit_indexes.values()):
            continue

        for entry_offset in ENTRY_OFFSETS:
            entry_index = indexes.get(entry_offset)
            if entry_index is None:
                continue
            entry_bar = session.bars[entry_index]
            observations.append(
                DailyObservation(
                    session_date=session.session_date,
                    entry_offset=entry_offset,
                    entry_time=as_utc(entry_bar.timestamp),
                    entry_price=float(entry_bar.close),
                    exit_prices={
                        name: float(session.bars[index].close)
                        for name, index in exit_indexes.items()
                        if index is not None
                    },
                    rth_open=rth_open,
                    vwap=float(session.vwap[entry_index]),
                    opening_range_high=opening_range_high,
                    opening_range_low=opening_range_low,
                    previous_day_direction=previous_direction,
                    previous_high=previous_high,
                    previous_low=previous_low,
                )
            )
    return observations


def policy_catalog() -> tuple[str, ...]:
    """Return the predeclared direction rules, including flat filters."""

    return (
        "always_long",
        "always_short",
        "open_to_entry",
        "vwap_side",
        "opening_range_break",
        "previous_day_direction",
        "previous_level_break",
        "vwap_and_previous_day",
        "vwap_and_opening_range",
    )


def policy_direction(observation: DailyObservation, policy: str) -> int:
    """Return +1 long, -1 short, 0 flat using entry-time information only."""

    vwap_direction = _sign(observation.entry_price - observation.vwap)
    open_direction = _sign(observation.entry_price - observation.rth_open)
    if policy == "always_long":
        return 1
    if policy == "always_short":
        return -1
    if policy == "open_to_entry":
        return open_direction
    if policy == "vwap_side":
        return vwap_direction
    if policy == "opening_range_break":
        if observation.entry_price > observation.opening_range_high:
            return 1
        if observation.entry_price < observation.opening_range_low:
            return -1
        return 0
    if policy == "previous_day_direction":
        return observation.previous_day_direction
    if policy == "previous_level_break":
        if observation.previous_high is not None and observation.entry_price > observation.previous_high:
            return 1
        if observation.previous_low is not None and observation.entry_price < observation.previous_low:
            return -1
        return 0
    if policy == "vwap_and_previous_day":
        return vwap_direction if vwap_direction == observation.previous_day_direction else 0
    if policy == "vwap_and_opening_range":
        opening_direction = policy_direction(observation, "opening_range_break")
        return vwap_direction if vwap_direction == opening_direction else 0
    raise ValueError(f"unknown policy: {policy}")


def _percentile(values: list[float], probability: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(round(float(probability) * (len(ordered) - 1)))
    return float(ordered[max(0, min(len(ordered) - 1, index))])


def _net_pnl(
    observation: DailyObservation,
    direction: int,
    exit_name: str,
    *,
    point_value: float,
    round_turn_cost: float,
    tick_value: float,
    stress_ticks: int = 0,
) -> float:
    if not direction:
        return 0.0
    move = float(observation.exit_prices[exit_name]) - float(observation.entry_price)
    return float(direction) * move * point_value - round_turn_cost - int(stress_ticks) * tick_value


def _stats(pnls: list[float], *, days: int, signals: int) -> dict:
    raw = series_stats(pnls)
    return {
        "days": int(days),
        "signals": int(signals),
        "trade_rate": round(signals / days, 4) if days else 0.0,
        "n": int(raw["n"]),
        "pnl": round(float(raw["pnl"]), 2),
        "pf": round(float(raw["pf"]), 4),
        "win_rate": round(float(raw["win"]), 4),
        "max_dd": round(float(raw["max_dd"]), 2),
        "avg_trade": round(float(raw["pnl"]) / len(pnls), 2) if pnls else 0.0,
    }


def _yearly(
    observations: list[DailyObservation],
    policy: str,
    exit_name: str,
    *,
    point_value: float,
    round_turn_cost: float,
    tick_value: float,
    stress_ticks: int = 0,
) -> dict[str, dict]:
    grouped: dict[str, list[float]] = {}
    signals: dict[str, int] = {}
    days: dict[str, int] = {}
    for observation in observations:
        key = str(observation.session_date.year)
        grouped.setdefault(key, [])
        days[key] = days.get(key, 0) + 1
        direction = policy_direction(observation, policy)
        if direction:
            signals[key] = signals.get(key, 0) + 1
            grouped[key].append(
                _net_pnl(
                    observation,
                    direction,
                    exit_name,
                    point_value=point_value,
                    round_turn_cost=round_turn_cost,
                    tick_value=tick_value,
                    stress_ticks=stress_ticks,
                )
            )
    return {
        year: _stats(values, days=days[year], signals=signals.get(year, 0))
        for year, values in sorted(grouped.items())
    }


def evaluate_policy(
    observations: list[DailyObservation],
    policy: str,
    exit_name: str,
    *,
    point_value: float,
    round_turn_cost: float,
    tick_value: float,
    stress_ticks: int = 0,
) -> dict:
    pnls: list[float] = []
    directions = {"long": 0, "short": 0, "flat": 0}
    for observation in observations:
        direction = policy_direction(observation, policy)
        directions["long" if direction > 0 else "short" if direction < 0 else "flat"] += 1
        if direction:
            pnls.append(
                _net_pnl(
                    observation,
                    direction,
                    exit_name,
                    point_value=point_value,
                    round_turn_cost=round_turn_cost,
                    tick_value=tick_value,
                    stress_ticks=stress_ticks,
                )
            )
    result = _stats(pnls, days=len(observations), signals=len(pnls))
    result["direction_counts"] = directions
    result["yearly"] = _yearly(
        observations,
        policy,
        exit_name,
        point_value=point_value,
        round_turn_cost=round_turn_cost,
        tick_value=tick_value,
        stress_ticks=stress_ticks,
    )
    return result


def random_baseline(
    observations: list[DailyObservation],
    exit_name: str,
    *,
    point_value: float,
    round_turn_cost: float,
    tick_value: float,
    stress_ticks: int,
    iters: int = RANDOM_ITERS,
    seed: int = RANDOM_SEED,
) -> dict:
    """Monte Carlo distribution for a fair random long/short choice each day."""

    totals: list[float] = []
    positive = 0
    for iteration in range(int(iters)):
        rng = random.Random(int(seed) + iteration)
        total = 0.0
        for observation in observations:
            direction = 1 if rng.random() >= 0.5 else -1
            total += _net_pnl(
                observation,
                direction,
                exit_name,
                point_value=point_value,
                round_turn_cost=round_turn_cost,
                tick_value=tick_value,
                stress_ticks=stress_ticks,
            )
        totals.append(total)
        if total > 0:
            positive += 1
    per_day_cost = round_turn_cost + stress_ticks * tick_value
    return {
        "days": len(observations),
        "iterations": int(iters),
        "seed": int(seed),
        "expected_pnl_if_fair": round(-len(observations) * per_day_cost, 2),
        "mean_pnl": round(sum(totals) / len(totals), 2) if totals else 0.0,
        "p05_pnl": round(_percentile(totals, 0.05), 2),
        "median_pnl": round(_percentile(totals, 0.50), 2),
        "p95_pnl": round(_percentile(totals, 0.95), 2),
        "probability_total_positive": round(positive / len(totals), 4) if totals else 0.0,
    }


def run_study(symbol: str = "MNQ") -> dict:
    contract_id = current_quarterly_contract_id(symbol)
    sessions, info = load_symbol_sessions(symbol)
    observations = build_observations(sessions)
    point_value = get_point_value(contract_id)
    tick_size = get_tick_size(contract_id)
    tick_value = get_tick_value(contract_id)
    round_turn_cost = get_commission_rt(contract_id) + get_fees_rt(contract_id)
    result_rows: list[dict] = []
    for entry_offset in ENTRY_OFFSETS:
        entry_observations = [row for row in observations if row.entry_offset == entry_offset]
        for exit_name in EXIT_OFFSETS:
            for policy in policy_catalog():
                base = evaluate_policy(
                    entry_observations,
                    policy,
                    exit_name,
                    point_value=point_value,
                    round_turn_cost=round_turn_cost,
                    tick_value=tick_value,
                )
                stress = evaluate_policy(
                    entry_observations,
                    policy,
                    exit_name,
                    point_value=point_value,
                    round_turn_cost=round_turn_cost,
                    tick_value=tick_value,
                    stress_ticks=STRESS_TICKS,
                )
                result_rows.append(
                    {
                        "entry_offset": entry_offset,
                        "entry_label": f"{9 + (30 + entry_offset) // 60:02d}:{(30 + entry_offset) % 60:02d} ET",
                        "exit": exit_name,
                        "policy": policy,
                        "base": base,
                        "stress_14t": stress,
                    }
                )

    first_date = min((row.session_date for row in observations), default=None)
    last_date = max((row.session_date for row in observations), default=None)
    random_rows = {}
    for entry_offset in ENTRY_OFFSETS:
        entry_observations = [row for row in observations if row.entry_offset == entry_offset]
        random_rows[str(entry_offset)] = {
            exit_name: random_baseline(
                entry_observations,
                exit_name,
                point_value=point_value,
                round_turn_cost=round_turn_cost,
                tick_value=tick_value,
                stress_ticks=STRESS_TICKS,
            )
            for exit_name in EXIT_OFFSETS
        }
    return {
        "study_version": "daily-direction-hold-v1",
        "generated_at": datetime.now().astimezone().isoformat(),
        "symbol": symbol,
        "contract_id": contract_id,
        "data": {
            "bars": int(info.total_bars),
            "rth_sessions": int(info.rth_sessions),
            "observations_per_entry": len(observations) // len(ENTRY_OFFSETS) if ENTRY_OFFSETS else 0,
            "first_session": first_date.isoformat() if first_date else None,
            "last_session": last_date.isoformat() if last_date else None,
            "skipped_short_sessions": int(info.skipped_short_sessions),
        },
        "method": {
            "entry_times": "completed 1m close at the listed ET offsets after 09:30",
            "exit_definitions": {
                "pre_flatten": "15:44 ET close, immediately before the shared 15:45 close phase",
                "rth_close": "15:59 ET close, final regular-session minute",
            },
            "opening_range": "09:30–09:44 ET inclusive, 15 completed 1m bars",
            "costs": "canonical per-contract round-turn commission + fees",
            "round_turn_cost": round(round_turn_cost, 4),
            "point_value": round(point_value, 4),
            "tick_size": round(tick_size, 4),
            "tick_value": round(tick_value, 4),
            "stress_ticks_round_trip": STRESS_TICKS,
            "random_baseline": "fair 50/50 long/short assignments over the same realized moves",
            "production_changed": False,
        },
        "policy_catalog": list(policy_catalog()),
        "results": result_rows,
        "random_baseline": random_rows,
    }


def _fmt(value: float) -> str:
    return f"${float(value):,.0f}"


def _write_csv(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "entry",
                "exit",
                "policy",
                "days",
                "signals",
                "trade_rate",
                "pnl",
                "pf",
                "win_rate",
                "max_dd",
                "stress_pnl",
                "stress_pf",
                "stress_max_dd",
            ]
        )
        for row in report["results"]:
            base = row["base"]
            stress = row["stress_14t"]
            writer.writerow(
                [
                    row["entry_label"],
                    row["exit"],
                    row["policy"],
                    base["days"],
                    base["signals"],
                    base["trade_rate"],
                    base["pnl"],
                    base["pf"],
                    base["win_rate"],
                    base["max_dd"],
                    stress["pnl"],
                    stress["pf"],
                    stress["max_dd"],
                ]
            )


def _write_markdown(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = report["results"]
    # The table is intentionally fixed to the final-minute hold.  The CSV/JSON
    # retain every entry and both exits for auditability.
    close_rows = [row for row in rows if row["exit"] == "rth_close"]
    ranked = sorted(close_rows, key=lambda row: row["base"]["pnl"], reverse=True)
    lines = [
        "# Causal daily direction / hold study",
        "",
        "這是診斷性研究，不是已核准的交易策略。每個交易日最多一筆，方向只用進場當下已完成的 1m 資料；沒有訊號的日子保持 flat。",
        "",
        f"- Data: {report['data']['rth_sessions']:,} complete RTH sessions, {report['data']['first_session']} → {report['data']['last_session']}.",
        f"- Cost: ${report['method']['round_turn_cost']:.2f}/contract round turn; stress adds {report['method']['stress_ticks_round_trip']} ticks (${report['method']['stress_ticks_round_trip'] * report['method']['tick_value']:.2f}).",
        "- Entry: completed close at 10:00, 10:30, 11:00, 11:30, 12:30, or 13:30 ET.",
        "- Exit shown below: 15:59 ET. A separate 15:44 ET pre-flatten result is in JSON/CSV.",
        "",
        "## Final-minute hold, ranked by in-sample net PnL",
        "",
        "| Entry | Policy | Signals | Rate | Net PnL | PF | Win | Max DD | 14t PnL | 14t PF |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in ranked:
        base = row["base"]
        stress = row["stress_14t"]
        lines.append(
            f"| {row['entry_label']} | {row['policy']} | {base['signals']:,} | {base['trade_rate']:.0%} | "
            f"{_fmt(base['pnl'])} | {base['pf']:.2f} | {base['win_rate']:.0%} | {_fmt(base['max_dd'])} | "
            f"{_fmt(stress['pnl'])} | {stress['pf']:.2f} |"
        )
    lines.extend(
        [
            "",
            "## How to read it",
            "",
            "- `always_long`/`always_short` show the sample's directional drift; they are not forecasts.",
            "- `vwap_side` is a continuation filter, not a random entry. `opening_range_break` and `previous_level_break` deliberately go flat until a level is broken.",
            "- A positive base result that collapses below PF 1 after 14 ticks is not enough margin for a live claim.",
            "- The random baseline's fair expectation is negative by costs; a positive total in some random paths is normal tail variation, not evidence of edge.",
            "",
            "## Random baseline",
            "",
            "| Entry | Exit | Fair expected PnL | Mean | P05 | Median | P95 | P(total > 0) |",
            "|---:|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for entry_offset, exits in report["random_baseline"].items():
        for exit_name, values in exits.items():
            lines.append(
                f"| {entry_offset}m | {exit_name} | {_fmt(values['expected_pnl_if_fair'])} | {_fmt(values['mean_pnl'])} | "
                f"{_fmt(values['p05_pnl'])} | {_fmt(values['median_pnl'])} | {_fmt(values['p95_pnl'])} | "
                f"{values['probability_total_positive']:.1%} |"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_outputs(report: dict, output: Path) -> tuple[Path, Path, Path]:
    output = output.with_suffix(".json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    csv_path = output.with_suffix(".csv")
    md_path = output.with_suffix(".md")
    _write_csv(report, csv_path)
    _write_markdown(report, md_path)
    return output, csv_path, md_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="MNQ")
    parser.add_argument("--out", default="")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    symbol = str(args.symbol).upper()
    print(f"Loading canonical {symbol} 1m RTH sessions...", flush=True)
    report = run_study(symbol)
    output = Path(args.out).expanduser() if args.out else market_data.derived_path(
        "research", f"daily_direction_hold_{symbol.lower()}_current.json"
    )
    json_path, csv_path, md_path = write_outputs(report, output)
    print(
        f"Loaded {report['data']['bars']:,} bars / {report['data']['rth_sessions']:,} RTH sessions "
        f"({report['data']['first_session']} → {report['data']['last_session']})",
        flush=True,
    )
    print(f"JSON: {json_path}", flush=True)
    print(f"CSV: {csv_path}", flush=True)
    print(f"Report: {md_path}", flush=True)


if __name__ == "__main__":
    main()
