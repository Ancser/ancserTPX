"""Research-only KDJMA entry + PI exit-structure experiment.

This script deliberately does not change production strategy code or presets.
It collects one fixed stream of completed-5m KDJMA signals, then applies the
same candidate entries to several exit contracts.  A second, one-position
simulation is included because a longer exit can suppress later entries in a
real engine; the isolated view keeps the exit-only comparison honest.

The current PI contract tested here is the live preset's actual geometry:

* long:  ATR-blend SL 4x, TP 3R (12x ATR-blend), no time exit
* short: ATR-blend SL 1.5x, TP 3R (4.5x ATR-blend), 60-minute time exit

All timestamps are UTC internally.  Session close uses the production New York
market clock, and same-candle SL/TP ambiguity uses the production resolver.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.backtest.intrabar import resolve_same_bar_exit  # noqa: E402
from backend.data import candle_store  # noqa: E402
from backend.db.models import (  # noqa: E402
    Direction,
    StrategyParams,
    current_quarterly_contract_id,
    get_commission_rt,
    get_fees_rt,
    get_point_value,
    get_tick_size,
)
from backend.strategy.factor import FactorSignalStrategy  # noqa: E402
from backend.strategy.session_filter import (  # noqa: E402
    MARKET_PHASE_FLATTEN,
    is_allowed_session,
    market_close_phase,
)

UTC = timezone.utc
ALL_SESSIONS = ["ASIA", "EURO", "PRE", "RTH", "AH"]


@dataclass(frozen=True)
class ExitSpec:
    name: str
    long_sl: float
    long_rr: float
    long_hold_min: int
    short_sl: float
    short_rr: float
    short_hold_min: int
    short_hard_tp: bool = True


@dataclass(frozen=True)
class SignalEvent:
    index: int
    timestamp: datetime
    direction: int
    entry: float
    atr_blend: float
    reason: str


@dataclass(frozen=True)
class TradeOutcome:
    event_index: int
    exit_index: int
    entry_time: datetime
    exit_time: datetime
    direction: int
    entry: float
    exit: float
    net: float
    hold_min: float
    reason: str


class _KdjmaEventCollector(FactorSignalStrategy):
    """Production KDJMA entry logic with a neutral 1x geometry for recording."""

    def _build_signal(self, candle, pending, entry_price=None):
        # The signal conditions do not use SL/TP, but FactorSignalStrategy
        # needs a valid risk width to emit a TradeSignal.  With both values at
        # 1.0, the resulting stop distance is exactly the completed-5m
        # ATR-blend (after the production tick rounding), which we record for
        # the later PI geometry variants.
        signal = super()._build_signal(candle, pending, entry_price)
        if signal is not None:
            width = self._risk_width("atr_blend", 1.0)
            if width is not None and width > 0:
                meta = dict(getattr(signal, "meta", None) or {})
                meta["research_atr_blend"] = float(width)
                signal.meta = meta
        return signal


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_time(value: str) -> datetime:
    return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _round_tick(price: float, tick: float) -> float:
    return round(round(float(price) / tick) * tick, 10)


def _params(contract_id: str) -> StrategyParams:
    return StrategyParams(
        strategy="factor",
        contract_id=contract_id,
        contract_size=1,
        candle_seconds=60,
        tr_allowed_sessions=list(ALL_SESSIONS),
        tr_one_trade_per_session=False,
        one_trade_per_session_direction=False,
        tr_daily_loss_stop=0,
        tr_daily_win_stop=0,
        factor_timeframe_minutes=5,
        factor_signal_family="icefishball",
        factor_side_mode="all",
        factor_pmo_signal_mode="normal",
        factor_session_va_filter="off",
        factor_sl_rule="atr_blend",
        factor_tp_rule="atr_blend",
        factor_sl_value=1.0,
        factor_tp_value=1.0,
        factor_max_hold_bars=0,
        factor_max_trades_per_day=3,
        factor_warmup_bars=150,
        trail_enabled=False,
        tr_trail_enabled=False,
        trail_trigger_pct=0.0,
        tr_trail_trigger_pct=0.0,
        trail_sl_ticks=0,
        tr_trail_sl_ticks=0,
        tr_exit_mode="tp",
    )


def collect_events(
    candles: list,
    output_start: datetime,
) -> list[SignalEvent]:
    strategy = _KdjmaEventCollector(_params(current_quarterly_contract_id("MNQ")))
    events: list[SignalEvent] = []

    for index, candle in enumerate(candles):
        if not is_allowed_session(candle.timestamp, strategy.tr_allowed_sessions if hasattr(strategy, "tr_allowed_sessions") else ALL_SESSIONS):
            strategy.observe(candle, [], True)
            continue
        if market_close_phase(candle.timestamp) == MARKET_PHASE_FLATTEN:
            strategy.observe(candle, [], True)
            continue

        signal = strategy.evaluate(candle, [], True)
        if signal is None:
            continue

        meta = dict(getattr(signal, "meta", None) or {})
        width = float(meta.get("research_atr_blend") or 0.0)
        if width <= 0:
            # This should not occur after warm-up.  Skip rather than invent a
            # risk width, matching production's no-width/no-entry behavior.
            strategy.notify_trade_closed("research_missing_width")
            continue
        ts = _utc(candle.timestamp)
        if ts >= output_start:
            events.append(
                SignalEvent(
                    index=index,
                    timestamp=ts,
                    direction=1 if signal.direction == Direction.BUY else -1,
                    entry=float(signal.entry_price),
                    atr_blend=width,
                    reason=str(getattr(signal, "reason", "KDJMA")),
                )
            )
        # Collector has no active position.  Release the strategy's local
        # state while retaining its per-day candidate limit.
        strategy.notify_trade_closed("research_signal_recorded")

    return events


def _levels(event: SignalEvent, spec: ExitSpec, tick: float) -> tuple[float, Optional[float], int]:
    if event.direction > 0:
        sl_k, rr, hold = spec.long_sl, spec.long_rr, spec.long_hold_min
    else:
        sl_k, rr, hold = spec.short_sl, spec.short_rr, spec.short_hold_min
    sl = _round_tick(event.entry - event.direction * sl_k * event.atr_blend, tick)
    tp = (
        _round_tick(event.entry + event.direction * sl_k * rr * event.atr_blend, tick)
        if rr > 0 and (event.direction > 0 or spec.short_hard_tp)
        else None
    )
    return sl, tp, hold


def simulate_one(
    event: SignalEvent,
    spec: ExitSpec,
    candles: list,
    tick: float,
    point_value: float,
    round_trip_cost: float,
) -> TradeOutcome:
    sl, tp, hold_limit = _levels(event, spec, tick)
    entry = event.entry
    exit_index = len(candles) - 1
    exit_price = float(candles[-1].close)
    exit_reason = "EOD"

    for index in range(event.index + 1, len(candles)):
        candle = candles[index]
        ts = _utc(candle.timestamp)

        # Production engine checks the close-window flatten before SL/TP.
        if market_close_phase(ts) == MARKET_PHASE_FLATTEN:
            exit_index, exit_price, exit_reason = index, float(candle.close), "FLAT"
            break

        hit_sl = (candle.low <= sl) if event.direction > 0 else (candle.high >= sl)
        hit_tp = (
            tp is not None
            and ((candle.high >= tp) if event.direction > 0 else (candle.low <= tp))
        )
        if hit_sl and hit_tp:
            first = resolve_same_bar_exit(candle.open, sl, tp)
            if first == "sl":
                exit_index, exit_price, exit_reason = index, sl, "SL"
            else:
                exit_index, exit_price, exit_reason = index, tp, "TP"
            break
        if hit_sl:
            exit_index, exit_price, exit_reason = index, sl, "SL"
            break
        if hit_tp:
            exit_index, exit_price, exit_reason = index, tp, "TP"
            break

        held = (ts - event.timestamp).total_seconds() / 60.0
        # In production, a time close is evaluated after the candle's SL/TP.
        if hold_limit > 0 and held >= hold_limit:
            exit_index, exit_price, exit_reason = index, float(candle.close), "TIME"
            break

    gross = (exit_price - entry) * event.direction * point_value
    net = gross - round_trip_cost
    hold = max(
        0.0,
        (_utc(candles[exit_index].timestamp) - event.timestamp).total_seconds() / 60.0,
    )
    return TradeOutcome(
        event_index=event.index,
        exit_index=exit_index,
        entry_time=event.timestamp,
        exit_time=_utc(candles[exit_index].timestamp),
        direction=event.direction,
        entry=entry,
        exit=exit_price,
        net=net,
        hold_min=hold,
        reason=exit_reason,
    )


def _stats(trades: Iterable[TradeOutcome], candidate_count: int) -> dict[str, Any]:
    rows = list(trades)
    values = [float(row.net) for row in rows]
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    equity = peak = max_dd = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    def side_stats(direction: int) -> dict[str, Any]:
        side = [row.net for row in rows if row.direction == direction]
        gain = sum(value for value in side if value > 0)
        loss = -sum(value for value in side if value < 0)
        return {
            "trades": len(side),
            "pnl": round(sum(side), 2),
            "pf": round(gain / loss, 4) if loss > 0 else (999.0 if gain > 0 else 0.0),
            "win_rate": round(sum(value > 0 for value in side) / len(side), 4) if side else 0.0,
        }

    monthly: dict[str, dict[str, Any]] = defaultdict(lambda: {"trades": 0, "pnl": 0.0})
    for row in rows:
        key = row.entry_time.strftime("%Y-%m")
        monthly[key]["trades"] += 1
        monthly[key]["pnl"] += row.net
    for item in monthly.values():
        item["pnl"] = round(item["pnl"], 2)

    return {
        "candidate_events": int(candidate_count),
        "trades": len(rows),
        "skipped_candidates": max(0, int(candidate_count) - len(rows)),
        "pnl": round(sum(values), 2),
        "pf": round(gains / losses, 4) if losses > 0 else (999.0 if gains > 0 else 0.0),
        "win_rate": round(sum(value > 0 for value in values) / len(values), 4) if values else 0.0,
        "max_dd": round(max_dd, 2),
        "expectancy": round(statistics.mean(values), 2) if values else 0.0,
        "avg_hold_min": round(statistics.mean(row.hold_min for row in rows), 2) if rows else 0.0,
        "median_hold_min": round(statistics.median(row.hold_min for row in rows), 2) if rows else 0.0,
        "exit_counts": dict(sorted(Counter(row.reason for row in rows).items())),
        "long": side_stats(1),
        "short": side_stats(-1),
        "monthly": dict(sorted(monthly.items())),
    }


def run_variant(
    events: list[SignalEvent],
    spec: ExitSpec,
    candles: list,
    tick: float,
    point_value: float,
    round_trip_cost: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    isolated = [
        simulate_one(event, spec, candles, tick, point_value, round_trip_cost)
        for event in events
    ]

    # Realistic one-position stream: an event on the exit candle cannot be
    # re-entered because the production engine returns after an exit.
    exclusive: list[TradeOutcome] = []
    last_exit_index = -1
    for event in events:
        if event.index <= last_exit_index:
            continue
        outcome = simulate_one(event, spec, candles, tick, point_value, round_trip_cost)
        exclusive.append(outcome)
        last_exit_index = outcome.exit_index

    return (
        _stats(isolated, len(events)),
        _stats(exclusive, len(events)),
    )


def _fmt(value: float) -> str:
    return f"{value:,.2f}"


def _print_table(rows: list[dict[str, Any]]) -> None:
    print("\n| Variant | N | PnL | PF | Win | Max DD | Avg hold | Long PF | Short PF | Exits |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for row in rows:
        s = row["exclusive"]
        win = f"{s['win_rate'] * 100:.1f}%"
        exits = ", ".join(f"{k}:{v}" for k, v in s["exit_counts"].items())
        print(
            f"| {row['name']} | {s['trades']} | ${_fmt(s['pnl'])} | {s['pf']:.2f} | "
            f"{win} | ${_fmt(s['max_dd'])} | {s['avg_hold_min']:.1f}m | "
            f"{s['long']['pf']:.2f} | {s['short']['pf']:.2f} | {exits} |"
        )


def _markdown(
    *,
    start: datetime,
    end: datetime,
    scan_start: datetime,
    candles: list,
    events: list[SignalEvent],
    rows: list[dict[str, Any]],
    contract_id: str,
    point_value: float,
    round_trip_cost: float,
) -> str:
    lines = [
        "# KDJMA entries with PI exit structure",
        "",
        "Research-only result; no production preset was changed.",
        "",
        f"- Output window: `{start.isoformat()}` → `{end.isoformat()}`",
        f"- Warm-up scan starts: `{scan_start.isoformat()}`",
        f"- Candles: `{len(candles):,}` 1m MNQ; contract `{contract_id}`",
        f"- Candidate KDJMA events: `{len(events)}` (long `{sum(e.direction > 0 for e in events)}`, short `{sum(e.direction < 0 for e in events)}`)",
        f"- Cost: `${round_trip_cost:.2f}` round-turn per contract; point value `${point_value:.2f}`",
        "- Entry stream is fixed across every row; the realistic view permits only one open position.",
        "",
        "## One-position results",
        "",
        "| Variant | N | PnL | PF | Win | Max DD | Avg hold | Long PF | Short PF | Exit counts |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        s = row["exclusive"]
        exits = ", ".join(f"{k}:{v}" for k, v in s["exit_counts"].items())
        lines.append(
            f"| {row['name']} | {s['trades']} | ${s['pnl']:,.2f} | {s['pf']:.2f} | "
            f"{s['win_rate'] * 100:.1f}% | ${s['max_dd']:,.2f} | {s['avg_hold_min']:.1f}m | "
            f"{s['long']['pf']:.2f} | {s['short']['pf']:.2f} | {exits} |"
        )

    lines += ["", "## Isolated exit-only results", "", "| Variant | Candidate N | PnL | PF | Win | Max DD | Long PF | Short PF |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for row in rows:
        s = row["isolated"]
        lines.append(
            f"| {row['name']} | {s['trades']} | ${s['pnl']:,.2f} | {s['pf']:.2f} | "
            f"{s['win_rate'] * 100:.1f}% | ${s['max_dd']:,.2f} | {s['long']['pf']:.2f} | {s['short']['pf']:.2f} |"
        )

    lines += ["", "## Monthly one-position PnL", ""]
    months = sorted({month for row in rows for month in row["exclusive"]["monthly"]})
    lines.append("| Variant | " + " | ".join(months) + " |")
    lines.append("|---|" + "---:|" * len(months))
    for row in rows:
        monthly = row["exclusive"]["monthly"]
        lines.append("| " + row["name"] + " | " + " | ".join(f"${monthly.get(m, {}).get('pnl', 0.0):,.0f}" for m in months) + " |")

    lines += [
        "",
        "## Interpretation guardrails",
        "",
        "- `KDJMA current baseline` uses the published KDJMA geometry `SL2x / TP2x` (1R).",
        "- `PI current asymmetric` is the current PI preset geometry: long `4x/3R`, short `1.5x/3R + 60m`; the short TP remains enabled because that is what the production PI engine currently creates.",
        "- Isolated rows answer the pure exit question. One-position rows answer what a live/backtest engine can actually trade; differences in N are caused by the exit holding time, not by a changed entry formula.",
        "- A positive PF/PnL in this in-sample window is not enough to promote a preset; require a contiguous out-of-sample block and slippage stress afterward.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-01-01T00:00:00+00:00")
    parser.add_argument("--end", default=None)
    parser.add_argument(
        "--out",
        default=r"F:\ancserQuant\ancserMarketData\derived\research\kdjma_pi_exit_study_20260909",
    )
    args = parser.parse_args()

    start = _parse_time(args.start)
    all_candles = sorted(candle_store.load("MNQ", 1), key=lambda candle: _utc(candle.timestamp))
    if not all_candles:
        raise SystemExit("MNQ candle store is empty")
    end = _parse_time(args.end) if args.end else _utc(all_candles[-1].timestamp)
    scan_start = start - timedelta(days=3)
    candles = [
        candle
        for candle in all_candles
        if scan_start <= _utc(candle.timestamp) <= end
    ]
    if not candles:
        raise SystemExit("No MNQ candles in requested range")

    contract_id = current_quarterly_contract_id("MNQ")
    tick = get_tick_size(contract_id)
    point_value = get_point_value(contract_id)
    round_trip_cost = get_commission_rt(contract_id) + get_fees_rt(contract_id)
    events = collect_events(candles, start)

    specs = [
        ExitSpec("KDJMA current baseline", 2.0, 1.0, 0, 2.0, 1.0, 0),
        ExitSpec("KDJMA wider RR3", 2.0, 3.0, 0, 2.0, 3.0, 0),
        ExitSpec("PI current asymmetric", 4.0, 3.0, 0, 1.5, 3.0, 60),
        ExitSpec("PI geometry only", 4.0, 3.0, 0, 1.5, 3.0, 0),
        ExitSpec("PI time only", 2.0, 1.0, 0, 2.0, 1.0, 60),
        ExitSpec("PI earlier research asym", 3.5, 3.0, 0, 2.5, 2.0, 60),
        ExitSpec("PI current + long 240m", 4.0, 3.0, 240, 1.5, 3.0, 60),
        ExitSpec("PI current short no TP", 4.0, 3.0, 0, 1.5, 0.0, 60, False),
    ]

    rows: list[dict[str, Any]] = []
    for spec in specs:
        isolated, exclusive = run_variant(
            events, spec, candles, tick, point_value, round_trip_cost
        )
        rows.append({
            "name": spec.name,
            "spec": asdict(spec),
            "isolated": isolated,
            "exclusive": exclusive,
        })

    print("KDJMA + PI exit structure study (research-only)")
    print(f"Window: {start.isoformat()} -> {end.isoformat()}")
    print(f"Candles: {len(candles):,} | candidates: {len(events)} | tick: {tick} | cost: ${round_trip_cost:.2f}")
    print(f"Candidates long/short: {sum(e.direction > 0 for e in events)}/{sum(e.direction < 0 for e in events)}")
    print("\nRealistic one-position results:")
    _print_table(rows)
    print("\nIsolated exit-only check (overlapping candidates intentionally allowed):")
    _print_table([{**row, "exclusive": row["isolated"]} for row in rows])

    payload = {
        "created_at": datetime.now(UTC).isoformat(),
        "window": {"start": start.isoformat(), "end": end.isoformat(), "scan_start": scan_start.isoformat()},
        "data": {"symbol": "MNQ", "candles": len(candles), "contract_id": contract_id, "tick_size": tick, "point_value": point_value, "round_trip_cost": round_trip_cost},
        "candidate_events": {"count": len(events), "long": sum(e.direction > 0 for e in events), "short": sum(e.direction < 0 for e in events)},
        "results": rows,
    }
    base = Path(args.out)
    base.parent.mkdir(parents=True, exist_ok=True)
    base.with_suffix(".json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    base.with_suffix(".md").write_text(
        _markdown(
            start=start,
            end=end,
            scan_start=scan_start,
            candles=candles,
            events=events,
            rows=rows,
            contract_id=contract_id,
            point_value=point_value,
            round_trip_cost=round_trip_cost,
        ),
        encoding="utf-8",
    )
    print(f"\nSaved: {base.with_suffix('.json')}")
    print(f"Saved: {base.with_suffix('.md')}")


if __name__ == "__main__":
    main()
