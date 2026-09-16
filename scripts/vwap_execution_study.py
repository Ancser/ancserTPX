"""Research-only VWAP execution study.

This script compares the existing causal VWAP setup families under three
entry execution assumptions without changing live or production behaviour:

* ``next_1m_open``: the shared research baseline;
* ``passive_touch_1bar``: place a VWAP/band limit after the completed signal
  bar and allow a fill only if the next 1m bar touches it;
* ``passive_through_1bar``: the same one-shot limit, but require price to
  trade at least one full tick through the level.  This is a conservative
  OHLC proxy for queue uncertainty, not a true MBO fill simulation.

All exits use the shared research exit rules: target limits, stop-market-like
stop fills (with an adverse opening gap filled at the bar open), costs,
one-position execution, walk-forward, Monte Carlo, and 0/2/4/8/14 tick
round-trip stress.  Limit orders are deliberately one-shot for parity with
the live engine's pending-order behaviour; an unfilled order is not counted as
a trade.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.backtest.prop_intraday_research import (  # noqa: E402
    FORCE_FLAT,
    EntryCandidate,
    RthSession,
    ResearchTrade,
    _as_et,
    _close_part,
    _entry_index,
    _level_hit,
    _resolve_target,
    _round_tick,
    _stop_fill,
    generate_mean_reversion_candidates,
    generate_vwap_pullback_candidates,
    load_symbol_sessions,
    recommended_configs,
    simulate_candidate,
)
from backend.backtest.robustness import evaluate  # noqa: E402
from backend.data import market_data  # noqa: E402
from backend.db.models import (  # noqa: E402
    current_quarterly_contract_id,
    get_commission_rt,
    get_fees_rt,
    get_point_value,
    get_tick_size,
)
from scripts.pro_trader_setup_study import _generate_vwap_reclaim  # noqa: E402


UTC = timezone.utc
RISK_DOLLARS = 200.0
MAX_TRADES_PER_DAY = 2
MC_SEED = 20260915
SLIP_LEVELS = (0, 2, 4, 8, 14)
SETUP_NAMES = (
    "vwap_pb_5m_wide_band",
    "vwap_pb_15m_wide_band",
    "meanrev_5m_15sd_vwap_tight",
    "meanrev_5m_20sd_vwap_wide",
    "vwap_reclaim_15m",
)
EXECUTION_MODELS = (
    "next_1m_open",
    "passive_touch_1bar",
    "passive_through_1bar",
)


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _find_config(symbol: str, name: str):
    configs = {config.name: config for config in recommended_configs(
        symbol, risk_dollars=RISK_DOLLARS, max_trades_per_day=MAX_TRADES_PER_DAY,
    )}
    try:
        return configs[name]
    except KeyError as exc:
        raise ValueError(f"unknown VWAP config {name!r}") from exc


def _generate_candidates(
    session: RthSession,
    symbol: str,
    setup: str,
    tick: float,
) -> list[EntryCandidate]:
    if setup == "vwap_reclaim_15m":
        return _generate_vwap_reclaim(session, tick, minutes=15)
    config = _find_config(symbol, setup)
    if setup.startswith("vwap_pb_"):
        candidates, _blocked = generate_vwap_pullback_candidates(session, config, symbol)
        return candidates
    if setup.startswith("meanrev_"):
        candidates, _blocked = generate_mean_reversion_candidates(session, config, symbol)
        return candidates
    raise ValueError(f"unsupported setup {setup!r}")


def _limit_price(candidate: EntryCandidate, tick: float) -> float:
    """Return a frozen passive price derived only from the signal bar."""

    vwap = candidate.meta.get("signal_vwap")
    std = candidate.meta.get("signal_std")
    if vwap is None:
        raise ValueError(f"{candidate.variant} is missing signal_vwap metadata")
    price = float(vwap)
    if candidate.strategy == "MEAN_REVERSION":
        if std is None or float(std) <= 0:
            raise ValueError(f"{candidate.variant} is missing signal_std metadata")
        sigma = 1.5 if "15sd" in candidate.variant else 2.0
        # Mean-reversion longs enter at the lower band and shorts at the
        # upper band; direction is +1 for long and -1 for short.
        price -= candidate.direction * sigma * float(std)
    return _round_tick(price, tick)


def _find_passive_fill(
    session: RthSession,
    candidate: EntryCandidate,
    symbol: str,
    model: str,
) -> tuple[int, float] | None:
    """Find a one-shot next-minute passive fill.

    OHLC data cannot establish queue priority.  ``touch`` is therefore an
    optimistic upper bound, while ``through`` requires one extra tick of
    trade-through and is a deliberately conservative sensitivity.
    """

    if model not in ("passive_touch_1bar", "passive_through_1bar"):
        return None
    entry_index = _entry_index(session, candidate.signal_index)
    if entry_index is None:
        return None
    bar = session.bars[entry_index]
    tick = get_tick_size(current_quarterly_contract_id(symbol))
    limit = _limit_price(candidate, tick)
    through = model == "passive_through_1bar"

    if candidate.direction > 0:
        if float(bar.open) <= limit:
            return entry_index, _round_tick(min(float(bar.open), limit), tick)
        threshold = limit - tick if through else limit
        if float(bar.low) <= threshold:
            return entry_index, limit
    else:
        if float(bar.open) >= limit:
            return entry_index, _round_tick(max(float(bar.open), limit), tick)
        threshold = limit + tick if through else limit
        if float(bar.high) >= threshold:
            return entry_index, limit
    return None


def _simulate_filled_candidate(
    session: RthSession,
    candidate: EntryCandidate,
    symbol: str,
    *,
    entry_index: int,
    fill_price: float,
) -> tuple[Optional[ResearchTrade], int, str]:
    """Run shared exit semantics after a passive limit has filled.

    The normal shared simulator always enters at the next 1m open.  This
    narrow adapter changes only the fill index/price and preserves its target,
    stop, partial-management, VWAP invalidation, and force-flat logic.  On the
    limit-entry candle it checks only the stop, matching the live engine's
    conservative rule that the candle's pre-fill high cannot prove a target
    was reached after the fill.
    """

    symbol = symbol.upper()
    contract_id = current_quarterly_contract_id(symbol)
    tick = get_tick_size(contract_id)
    point_value = get_point_value(contract_id)
    entry_bar = session.bars[entry_index]
    entry = _round_tick(float(fill_price), tick)
    stop = _round_tick(candidate.stop_price, tick)
    direction = candidate.direction
    if (direction > 0 and stop >= entry) or (direction < 0 and stop <= entry):
        return None, candidate.signal_index, "gapped_through_stop"

    risk = abs(entry - stop)
    contracts = int(candidate.risk_dollars // (risk * point_value))
    if contracts < 1:
        return None, candidate.signal_index, "risk_too_large"

    target1 = _resolve_target(
        candidate.target1_kind,
        candidate.target1_value,
        entry=entry,
        risk=risk,
        direction=direction,
    )
    target2 = _resolve_target(
        candidate.target2_kind,
        candidate.target2_value,
        entry=entry,
        risk=risk,
        direction=direction,
    )
    if target1 is None:
        return None, candidate.signal_index, "missing_target"
    target1 = _round_tick(target1, tick)
    target2 = _round_tick(target2, tick) if target2 is not None else None
    if (direction > 0 and target1 <= entry) or (direction < 0 and target1 >= entry):
        return None, candidate.signal_index, "gapped_through_target"
    if target2 is not None and (
        (direction > 0 and target2 <= target1)
        or (direction < 0 and target2 >= target1)
    ):
        target2 = _round_tick(
            entry + direction * max(risk * 1.5, abs(target1 - entry) * 1.25),
            tick,
        )

    if target2 is None or contracts == 1 or candidate.target1_fraction >= 1.0:
        first_qty = contracts
    else:
        first_qty = max(
            1,
            min(contracts - 1, int(math.floor(contracts * candidate.target1_fraction + 0.5))),
        )

    remaining = contracts
    active_stop = stop
    active_target = target1
    first_stage = True
    fills: list[tuple[int, float, str]] = []
    tp1_hit = False
    pending_market_exit: Optional[str] = None
    exit_idx = len(session.bars) - 1
    signal_by_end = (
        {
            bar.end_index: bar
            for bar in session.signal_bars(candidate.structural_trail_minutes)
        }
        if candidate.structural_trail_minutes
        else {}
    )

    for idx in range(entry_index, len(session.bars)):
        bar = session.bars[idx]
        et_time = session.et_times[idx].time().replace(tzinfo=None)
        elapsed = (bar.timestamp - entry_bar.timestamp).total_seconds() / 60.0
        if pending_market_exit:
            _close_part(fills, remaining, float(bar.open), pending_market_exit)
            remaining = 0
            exit_idx = idx
            break
        if idx > entry_index and et_time >= FORCE_FLAT:
            _close_part(fills, remaining, float(bar.open), "force_flat")
            remaining = 0
            exit_idx = idx
            break
        if idx > entry_index and candidate.max_hold_minutes and elapsed >= candidate.max_hold_minutes:
            _close_part(fills, remaining, float(bar.open), "time_stop")
            remaining = 0
            exit_idx = idx
            break

        if idx == entry_index:
            stop_hit = (
                float(bar.low) <= active_stop
                if direction > 0
                else float(bar.high) >= active_stop
            )
            if stop_hit:
                _close_part(fills, remaining, _stop_fill(bar, direction, active_stop), "sl")
                remaining = 0
                exit_idx = idx
                break
            continue

        outcome = _level_hit(bar, direction, active_stop, active_target)
        if outcome == "sl":
            reason = "breakeven" if tp1_hit and active_stop == entry else "sl"
            _close_part(fills, remaining, _stop_fill(bar, direction, active_stop), reason)
            remaining = 0
            exit_idx = idx
            break
        if outcome == "tp":
            quantity = first_qty if first_stage else remaining
            _close_part(fills, quantity, active_target, "tp1" if first_stage else "tp2")
            remaining -= quantity
            if remaining <= 0:
                exit_idx = idx
                break
            tp1_hit = True
            first_stage = False
            if candidate.move_stop_to_breakeven:
                active_stop = entry
            active_target = float(target2)
            stop_touched = (
                float(bar.low) <= active_stop
                if direction > 0
                else float(bar.high) >= active_stop
            )
            target_touched = (
                float(bar.high) >= active_target
                if direction > 0
                else float(bar.low) <= active_target
            )
            if stop_touched:
                _close_part(
                    fills,
                    remaining,
                    _stop_fill(bar, direction, active_stop),
                    "breakeven",
                )
                remaining = 0
                exit_idx = idx
                break
            if target_touched:
                _close_part(fills, remaining, active_target, "tp2")
                remaining = 0
                exit_idx = idx
                break

        if remaining <= 0:
            break

        completed = signal_by_end.get(idx)
        if tp1_hit and completed is not None:
            if direction > 0:
                proposed = _round_tick(completed.low - candidate.trail_buffer_ticks * tick, tick)
                if active_stop < proposed < float(completed.close):
                    active_stop = proposed
            else:
                proposed = _round_tick(completed.high + candidate.trail_buffer_ticks * tick, tick)
                if float(completed.close) < proposed < active_stop:
                    active_stop = proposed

        if candidate.vwap_invalidation_buffer is not None:
            buffer = candidate.vwap_invalidation_buffer
            invalid = (
                float(bar.close) < session.vwap[idx] - buffer
                if direction > 0
                else float(bar.close) > session.vwap[idx] + buffer
            )
            if invalid:
                pending_market_exit = "vwap_invalidated"

    if remaining > 0:
        last = session.bars[-1]
        _close_part(fills, remaining, float(last.close), "session_end")
        exit_idx = len(session.bars) - 1
        remaining = 0

    if not fills:
        return None, exit_idx, "no_exit"
    filled_qty = sum(quantity for quantity, _, _ in fills)
    if filled_qty != contracts:
        raise AssertionError(f"partial fill accounting mismatch: {filled_qty} != {contracts}")
    weighted_exit = sum(quantity * price for quantity, price, _ in fills) / contracts
    gross = sum(quantity * direction * (price - entry) * point_value for quantity, price, _ in fills)
    costs = contracts * (get_commission_rt(contract_id) + get_fees_rt(contract_id))
    net = gross - costs
    final_reason = fills[-1][2]
    trade = ResearchTrade(
        symbol=symbol,
        strategy=candidate.strategy,
        variant=candidate.variant,
        direction="long" if direction > 0 else "short",
        signal_time=candidate.signal_time,
        entry_time=entry_bar.timestamp,
        exit_time=session.bars[exit_idx].timestamp,
        entry_price=entry,
        exit_price=weighted_exit,
        initial_stop=stop,
        final_stop=active_stop,
        contracts=contracts,
        risk_points=risk,
        planned_risk_dollars=contracts * risk * point_value,
        gross_pnl=gross,
        costs=costs,
        pnl=net,
        exit_reason=final_reason,
        tp1_hit=tp1_hit or any(reason == "tp1" for _, _, reason in fills),
        reason=candidate.reason,
    )
    return trade, exit_idx, ""


def _simulate_session(
    session: RthSession,
    candidates: Sequence[EntryCandidate],
    symbol: str,
    model: str,
) -> tuple[list[ResearchTrade], Counter[str], dict[str, int]]:
    trades: list[ResearchTrade] = []
    skipped: Counter[str] = Counter()
    fill_counts: Counter[str] = Counter()
    last_exit_index = -1
    for candidate in sorted(candidates, key=lambda row: row.signal_index):
        if len(trades) >= MAX_TRADES_PER_DAY:
            skipped["max_trades"] += 1
            continue
        if candidate.signal_index < last_exit_index:
            skipped["overlap"] += 1
            continue

        if model == "next_1m_open":
            fill_counts["filled_signals"] += 1
            trade, exit_idx, reason = simulate_candidate(session, candidate, symbol)
        else:
            fill = _find_passive_fill(session, candidate, symbol, model)
            if fill is None:
                fill_counts["no_fill"] += 1
                continue
            fill_counts["filled_signals"] += 1
            trade, exit_idx, reason = _simulate_filled_candidate(
                session,
                candidate,
                symbol,
                entry_index=fill[0],
                fill_price=fill[1],
            )
        if trade is None:
            skipped[reason or "invalid"] += 1
            continue
        trades.append(trade)
        last_exit_index = exit_idx
    return trades, skipped, dict(fill_counts)


def _metrics(trades: Sequence[ResearchTrade], *, mc_iters: int) -> dict[str, Any]:
    rows = [trade.robustness_row() for trade in trades]
    robust = evaluate(
        rows,
        iters=mc_iters,
        seed=MC_SEED,
        dd_threshold=2000.0,
        slip_levels=SLIP_LEVELS,
    )
    stats = robust["stats"]
    stress = next(item["stats"] for item in robust["slip"]["levels"] if item["level"] == 14)
    walk = robust.get("walk_forward") or {}
    mc = robust.get("monte_carlo") or {}
    mc_pass = bool(
        mc
        and mc.get("p_loss", 1.0) <= 0.05
        and mc.get("dd_p95", float("inf")) < 2000.0
        and mc.get("pf_p5", 0.0) > 1.0
    )
    if len(trades) < 100:
        verdict = "INSUFFICIENT_SAMPLE"
    elif stats["pf"] <= 1.0:
        verdict = "FAIL_BASE_PF"
    elif not bool(walk.get("pass")):
        verdict = "FAIL_WALK_FORWARD"
    elif stress["pf"] <= 1.0:
        verdict = "FAIL_14T_SLIPPAGE"
    elif not mc_pass:
        verdict = "FAIL_MONTE_CARLO"
    else:
        verdict = "RESEARCH_CANDIDATE"

    by_side: dict[str, list[float]] = defaultdict(list)
    by_year: dict[str, list[float]] = defaultdict(list)
    for trade in trades:
        by_side[trade.direction].append(float(trade.pnl))
        by_year[str(_as_et(trade.entry_time).year)].append(float(trade.pnl))
    return {
        "trades": int(stats["n"]),
        "pnl": round(float(stats["pnl"]), 2),
        "pf": round(float(stats["pf"]), 4),
        "win": round(float(stats["win"]), 4),
        "max_dd": round(float(stats["max_dd"]), 2),
        "expectancy": round(float(stats["pnl"]) / len(trades), 2) if trades else 0.0,
        "slip14_pnl": round(float(stress["pnl"]), 2),
        "slip14_pf": round(float(stress["pf"]), 4),
        "walk_forward_pass": bool(walk.get("pass")),
        "walk_forward_segments": [
            {key: segment.get(key) for key in ("n", "pnl", "pf", "win")}
            for segment in walk.get("segments", [])
        ],
        "monte_carlo_pass": mc_pass,
        "monte_carlo": {
            key: mc.get(key)
            for key in ("iters", "seed", "n", "p_loss", "dd_p95", "dd_threshold", "pf_p5")
            if key in mc
        },
        "by_side": {
            side: {
                "n": len(values),
                "pnl": round(sum(values), 2),
            }
            for side, values in sorted(by_side.items())
        },
        "yearly": {
            year: {
                "n": len(values),
                "pnl": round(sum(values), 2),
            }
            for year, values in sorted(by_year.items())
        },
        "verdict": verdict,
    }


def _write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields or ["symbol"])
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in fields} for row in rows)


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# VWAP execution model study",
        "",
        f"Status: **{payload['status']}**",
        f"Generated: `{payload['generated_at']}`",
        "",
        "This is research-only.  No live or production setting was changed.",
        "The passive models use a one-shot next-1m limit and count unfilled orders as no-fill; OHLC cannot model queue priority, so touch is an optimistic upper bound and through is a conservative proxy.",
        "",
        "## Coverage",
        "",
    ]
    for symbol, item in sorted(payload["coverage"].items()):
        dataset = item["dataset"]
        lines.append(
            f"- **{symbol}**: {item['rth_days']} complete RTH days ({item['rth_start']} → {item['rth_end']}); "
            f"{dataset['total_bars']:,} 1m store bars; source counts {dataset['source_counts']}."
        )
    lines.extend([
        "",
        "## Results",
        "",
        "14t PF is after an additional 14-tick round-trip adverse charge.  Positive baseline PF alone is not a promotion signal.",
        "",
        "| Symbol | Setup | Execution | Signals | Filled | No fill | Trades | P&L | PF | 14t PF | WF | Verdict |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|:---:|---|",
    ])
    for row in sorted(payload["results"], key=lambda item: (item["symbol"], item["setup"], item["execution"])):
        lines.append(
            f"| {row['symbol']} | {row['setup']} | {row['execution']} | {row['signals']} | "
            f"{row['filled_signals']} | {row['no_fill']} | {row['trades']} | ${_fmt(row['pnl'])} | "
            f"{_fmt(row['pf'])} | {_fmt(row['slip14_pf'])} | "
            f"{'PASS' if row['walk_forward_pass'] else 'FAIL'} | {row['verdict']} |"
        )
    lines.extend([
        "",
        "## Frozen execution definitions",
        "",
        "- next_1m_open: the shared baseline after a completed causal 5m/15m signal bar.",
        "- passive_touch_1bar: buy limit at VWAP (or the signal deviation band for mean reversion), or sell limit symmetrically; fill on the next 1m touch only.",
        "- passive_through_1bar: same order, but require one full tick beyond the limit; this is not a queue/MBO fill guarantee.",
        "- Stop exits use the shared stop-market-like rule; target exits remain limit-style OHLC fills.",
        "",
    ])
    return "\n".join(lines)


def _run_symbol(
    symbol: str,
    *,
    mc_iters: int,
    start: date | None,
    end: date | None,
    setups: Sequence[str],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    sessions, info = load_symbol_sessions(symbol, start_date=start, end_date=end)
    tick = get_tick_size(current_quarterly_contract_id(symbol))
    candidates_by_setup: dict[str, dict[str, list[EntryCandidate]]] = {
        setup: defaultdict(list) for setup in setups
    }
    sessions_by_day = {session.session_date.isoformat(): session for session in sessions}
    for session in sessions:
        day = session.session_date.isoformat()
        for setup in setups:
            candidates_by_setup[setup][day].extend(
                _generate_candidates(session, symbol, setup, tick)
            )

    result_rows: list[dict[str, Any]] = []
    trade_rows: list[dict[str, Any]] = []
    for setup in setups:
        rows_by_day = candidates_by_setup[setup]
        signals = sum(len(rows) for rows in rows_by_day.values())
        for execution in EXECUTION_MODELS:
            all_trades: list[ResearchTrade] = []
            skipped: Counter[str] = Counter()
            fill_counts: Counter[str] = Counter()
            for day in sorted(rows_by_day):
                session = sessions_by_day[day]
                trades, session_skipped, session_fill_counts = _simulate_session(
                    session,
                    rows_by_day[day],
                    symbol,
                    execution,
                )
                all_trades.extend(trades)
                skipped.update(session_skipped)
                fill_counts.update(session_fill_counts)
            metrics = _metrics(all_trades, mc_iters=mc_iters)
            result_rows.append({
                "symbol": symbol,
                "setup": setup,
                "execution": execution,
                "signals": signals,
                "filled_signals": int(fill_counts.get("filled_signals", 0)),
                "no_fill": int(fill_counts.get("no_fill", 0)),
                "skipped": dict(skipped),
                **metrics,
            })
            for trade in all_trades:
                row = asdict(trade)
                row.update({"symbol": symbol, "setup": setup, "execution": execution})
                trade_rows.append(row)
    coverage = {
        "symbol": symbol,
        "dataset": asdict(info),
        "rth_days": len(sessions),
        "rth_start": sessions[0].session_date.isoformat() if sessions else None,
        "rth_end": sessions[-1].session_date.isoformat() if sessions else None,
    }
    return coverage, result_rows, trade_rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default=["MNQ", "MES"])
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--mc-iters", type=int, default=1000)
    parser.add_argument(
        "--setup",
        action="append",
        dest="setups",
        help="run only this frozen setup; repeatable",
    )
    parser.add_argument(
        "--output",
        default=str(market_data.derived_path("research", "vwap_execution_study_current.json")),
    )
    parser.add_argument("--include-trades", action="store_true")
    args = parser.parse_args()

    symbols = [str(symbol).upper() for symbol in args.symbols]
    bad_symbols = [symbol for symbol in symbols if symbol not in {"MNQ", "MES"}]
    if bad_symbols:
        parser.error(f"unsupported symbols: {', '.join(bad_symbols)}")
    setups = tuple(args.setups or SETUP_NAMES)
    bad_setups = [setup for setup in setups if setup not in SETUP_NAMES]
    if bad_setups:
        parser.error(f"unsupported VWAP setups: {', '.join(bad_setups)}")
    if args.mc_iters <= 0:
        parser.error("--mc-iters must be positive")
    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None
    if start and end and start > end:
        parser.error("--start must be <= --end")

    started = time.time()
    payload: dict[str, Any] = {
        "status": "research_only",
        "generated_at": datetime.now(UTC).isoformat(),
        "study": {
            "symbols": symbols,
            "setups": list(setups),
            "execution_models": list(EXECUTION_MODELS),
            "risk_dollars": RISK_DOLLARS,
            "max_trades_per_day": MAX_TRADES_PER_DAY,
            "mc_iters": args.mc_iters,
            "mc_seed": MC_SEED,
            "slippage_levels_round_trip_ticks": list(SLIP_LEVELS),
            "entry": "signal is completed causally, then next-1m execution model is applied",
            "passive_limit": "one-shot next-1m order; VWAP for trend/reclaim, signal deviation band for mean reversion",
            "queue_model": "not available from OHLC; touch is optimistic, through-one-tick is conservative proxy",
            "stop_exit": "shared stop-market-like fill; adverse opening gap fills at bar open",
            "data_scope": "canonical reconciled 1m store, complete RTH sessions only",
        },
        "coverage": {},
        "results": [],
        "notes": [
            "Unfilled passive orders are excluded from trades and reported separately.",
            "A 14-tick stress is a deterministic adverse round-trip charge, not an observed quote replay.",
            "No model or preset was changed; the result is not live approval.",
        ],
    }
    all_trades: list[dict[str, Any]] = []
    for symbol in symbols:
        print(f"[{symbol}] loading sessions and testing {len(setups)} VWAP setups x {len(EXECUTION_MODELS)} execution models")
        coverage, rows, trades = _run_symbol(
            symbol,
            mc_iters=args.mc_iters,
            start=start,
            end=end,
            setups=setups,
        )
        payload["coverage"][symbol] = coverage
        payload["results"].extend(rows)
        all_trades.extend(trades)
        for row in rows:
            print(
                f"  {row['setup']:<37} {row['execution']:<24} "
                f"signals={row['signals']:4d} filled={row['filled_signals']:4d} "
                f"n={row['trades']:4d} PF={row['pf']:5.2f} 14tPF={row['slip14_pf']:5.2f} {row['verdict']}"
            )
    payload["elapsed_seconds"] = round(time.time() - started, 2)
    if args.include_trades:
        payload["trades"] = all_trades

    output = Path(args.output)
    if not output.is_absolute():
        output = (ROOT / output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    summary_rows = [
        {
            key: row.get(key)
            for key in (
                "symbol", "setup", "execution", "signals", "filled_signals", "no_fill",
                "trades", "pnl", "pf", "max_dd", "slip14_pnl", "slip14_pf",
                "walk_forward_pass", "monte_carlo_pass", "verdict",
            )
        }
        for row in payload["results"]
    ]
    _write_csv(output.with_suffix(".csv"), summary_rows)
    output.with_suffix(".md").write_text(_markdown(payload), encoding="utf-8")
    if args.include_trades:
        _write_csv(output.with_name(output.stem + "_trades.csv"), all_trades)
    print(f"JSON: {output}")
    print(f"CSV : {output.with_suffix('.csv')}")
    print(f"MD  : {output.with_suffix('.md')}")
    if args.include_trades:
        print(f"Trades: {output.with_name(output.stem + '_trades.csv')}")
    print(f"Elapsed: {payload['elapsed_seconds']:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
