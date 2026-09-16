"""Research-only extension of the PI trailing-exit study.

The first PI trail study tested one-time profit locks.  This companion study
tests two additional behaviours without changing production code:

* ``continuous``: a true high-watermark trail.  After activation at N R, the
  stop follows the best post-fill candle extreme with a fixed R gap.
* ``ladder``: the production shared R-ladder exit kernel, with several trigger
  and gap combinations.

Each variant is replayed through the production ``BacktestEngine`` and the
production PI entry logic.  The study is deliberately a counterfactual:
reports are written under the external ``ancserMarketData`` research tree and
no live preset, order rule, or production strategy is changed.

Intrabar convention for the continuous trail: the engine checks the existing
SL/TP before it observes the current candle's extreme and moves the stop for a
future candle.  Thus a new high/low cannot retroactively stop out the same
candle.  This is conservative and avoids using future intrabar ordering that
1-minute OHLC data cannot identify.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.backtest.engine import BacktestConfig, BacktestEngine  # noqa: E402
from backend.data import market_data  # noqa: E402
from backend.db.models import (  # noqa: E402
    Candle,
    Direction,
    ExitReason,
    get_commission_rt,
    get_fees_rt,
)
from backend.strategy.exit_policy import (  # noqa: E402
    ExitAction,
    ExitDecision,
    ExitPolicy,
    ExitState,
    ExitTrailMode,
)
from backend.strategy.pi_signal import PiSignalStrategy  # noqa: E402
from backend.timebase import as_utc  # noqa: E402
from scripts.pi_exit_engine_grid_study import (  # noqa: E402
    _fingerprint,
    _load_base_params,
    _load_run_candles,
    _monthly_stats,
    _robustness,
    _status,
    trade_rows,
)
from scripts.pi_trail_engine_study import _actual_params, _diagnostics  # noqa: E402


STUDY_VERSION = "2026-09-14-pi-trail-extended-v1"
INITIAL_CAPITAL = 50_000.0
PI_SYMBOLS = ("MNQ", "MES")


@dataclass(frozen=True)
class ExtendedVariant:
    key: str
    label: str
    mode: str  # none, continuous, ladder
    activation_r: float
    gap_r: float
    hard_tp: bool
    long_hold: int
    short_hold: int


CURRENT_VARIANT = ExtendedVariant(
    "current",
    "CURRENT · hard TP3R / long OFF / short60",
    "none",
    0.0,
    0.0,
    True,
    0,
    60,
)


# The grid is fixed before looking at outcomes.  It is intentionally small so
# that a future holdout can be run without a large multiple-comparison search.
EXTENDED_VARIANTS: tuple[ExtendedVariant, ...] = (
    CURRENT_VARIANT,
    ExtendedVariant("cont_tp_1r_gap05", "CONT · TP on · arm 1R / gap 0.5R", "continuous", 1.0, 0.5, True, 0, 60),
    ExtendedVariant("cont_tp_1r_gap1", "CONT · TP on · arm 1R / gap 1R", "continuous", 1.0, 1.0, True, 0, 60),
    ExtendedVariant("cont_tp_1_5r_gap05", "CONT · TP on · arm 1.5R / gap 0.5R", "continuous", 1.5, 0.5, True, 0, 60),
    ExtendedVariant("cont_tp_1_5r_gap1", "CONT · TP on · arm 1.5R / gap 1R", "continuous", 1.5, 1.0, True, 0, 60),
    ExtendedVariant("cont_tp_2r_gap05", "CONT · TP on · arm 2R / gap 0.5R", "continuous", 2.0, 0.5, True, 0, 60),
    ExtendedVariant("cont_tp_2r_gap1", "CONT · TP on · arm 2R / gap 1R", "continuous", 2.0, 1.0, True, 0, 60),
    ExtendedVariant("cont_runner_1r_gap05", "CONT runner · arm 1R / gap 0.5R", "continuous", 1.0, 0.5, False, 0, 60),
    ExtendedVariant("cont_runner_1_5r_gap1", "CONT runner · arm 1.5R / gap 1R", "continuous", 1.5, 1.0, False, 0, 60),
    ExtendedVariant("cont_runner_2r_gap1", "CONT runner · arm 2R / gap 1R", "continuous", 2.0, 1.0, False, 0, 60),
    ExtendedVariant("cont_time120_1r_gap05", "CONT + time120 · arm 1R / gap 0.5R", "continuous", 1.0, 0.5, False, 120, 120),
    ExtendedVariant("cont_time120_1_5r_gap1", "CONT + time120 · arm 1.5R / gap 1R", "continuous", 1.5, 1.0, False, 120, 120),
    ExtendedVariant("ladder_1r_gap05", "LADDER · arm 1R / gap 0.5R", "ladder", 1.0, 0.5, False, 0, 60),
    ExtendedVariant("ladder_1r_gap1", "LADDER · arm 1R / gap 1R", "ladder", 1.0, 1.0, False, 0, 60),
    ExtendedVariant("ladder_1_5r_gap05", "LADDER · arm 1.5R / gap 0.5R", "ladder", 1.5, 0.5, False, 0, 60),
    ExtendedVariant("ladder_1_5r_gap1", "LADDER · arm 1.5R / gap 1R", "ladder", 1.5, 1.0, False, 0, 60),
    ExtendedVariant("ladder_2r_gap05", "LADDER · arm 2R / gap 0.5R", "ladder", 2.0, 0.5, False, 0, 60),
    ExtendedVariant("ladder_2r_gap1", "LADDER · arm 2R / gap 1R", "ladder", 2.0, 1.0, False, 0, 60),
)


class ExtendedPiSignalStrategy(PiSignalStrategy):
    """Production PI entries with one explicit research exit policy."""

    def __init__(self, params: Any, variant: ExtendedVariant):
        self._extended_variant = variant
        super().__init__(params)

    def _policy_for_signal(self, signal: Any) -> ExitPolicy:
        variant = self._extended_variant
        hold = variant.long_hold if signal.direction == Direction.BUY else variant.short_hold
        if variant.mode == "continuous":
            return ExitPolicy(
                model="pi",
                max_hold_minutes=max(0, int(hold)),
                hard_tp_enabled=bool(variant.hard_tp),
                trail_mode=ExitTrailMode.SINGLE,
                trail_trigger_pct=max(0.0, float(variant.activation_r)),
            )
        if variant.mode == "ladder":
            return ExitPolicy(
                model="pi",
                max_hold_minutes=max(0, int(hold)),
                hard_tp_enabled=False,
                trail_mode=ExitTrailMode.LADDER,
                ladder_trigger_r=max(0.0, float(variant.activation_r)),
                ladder_gap_r=max(0.0, float(variant.gap_r)),
            )
        return ExitPolicy(
            model="pi",
            max_hold_minutes=max(0, int(hold)),
            hard_tp_enabled=bool(variant.hard_tp),
            trail_mode=ExitTrailMode.NONE,
        )

    def evaluate(self, candle: Candle, zones=None, is_mature: bool = True):
        signal = super().evaluate(candle, zones, is_mature)
        if signal is not None:
            signal.exit_policy = self._policy_for_signal(signal)
        return signal


class ContinuousTrailEngine(BacktestEngine):
    """Production engine with a research-only high/low trailing adapter."""

    def __init__(self, *args, trail_variant: ExtendedVariant, **kwargs):
        self._extended_variant = trail_variant
        super().__init__(*args, **kwargs)

    @staticmethod
    def _round_to_tick(price: float, tick_size: float) -> float:
        if tick_size <= 0:
            return price
        return round(round(price / tick_size) * tick_size, 10)

    def _evaluate_active_exit(
        self,
        candle: Candle,
        *,
        held_minutes: Optional[float] = None,
    ) -> ExitDecision:
        variant = self._extended_variant
        if variant.mode != "continuous":
            return super()._evaluate_active_exit(candle, held_minutes=held_minutes)

        pos = self._open_position
        if not pos:
            return ExitDecision(action=ExitAction.NONE, state=self._exit_state)
        policy = self._active_exit_policy
        if policy is None:
            return super()._evaluate_active_exit(candle, held_minutes=held_minutes)

        state = ExitState(
            trail_triggered=self._trail_sl_triggered,
            ladder_max_r=self._exit_state.ladder_max_r,
            ladder_lock_r=self._exit_state.ladder_lock_r,
        )
        held = (
            (candle.timestamp - pos.entry_time).total_seconds() / 60.0
            if held_minutes is None
            else held_minutes
        )
        if policy.max_hold_minutes > 0 and held >= policy.max_hold_minutes:
            return ExitDecision(
                action=ExitAction.CLOSE,
                reason=ExitReason.FLATTEN,
                state=state,
            )

        risk = abs(float(pos.entry_price) - float(pos.original_sl_price or pos.sl_price))
        if risk <= 0:
            return ExitDecision(action=ExitAction.NONE, state=state)

        if pos.direction == Direction.BUY:
            favourable = max(0.0, float(candle.high) - float(pos.entry_price))
            raw_stop = float(candle.high) - variant.gap_r * risk
        else:
            favourable = max(0.0, float(pos.entry_price) - float(candle.low))
            raw_stop = float(candle.low) + variant.gap_r * risk

        peak_r = max(float(state.ladder_max_r), favourable / risk)
        observed = ExitState(
            trail_triggered=state.trail_triggered,
            ladder_max_r=peak_r,
            ladder_lock_r=state.ladder_lock_r,
        )
        if peak_r < variant.activation_r:
            return ExitDecision(action=ExitAction.NONE, state=observed)

        new_stop = self._round_to_tick(raw_stop, self.TICK_SIZE)
        improves = (
            new_stop > float(pos.sl_price)
            if pos.direction == Direction.BUY
            else new_stop < float(pos.sl_price)
        )
        next_state = ExitState(
            trail_triggered=state.trail_triggered or improves,
            ladder_max_r=peak_r,
            ladder_lock_r=state.ladder_lock_r,
        )
        if not improves:
            return ExitDecision(action=ExitAction.NONE, state=next_state)
        return ExitDecision(
            action=ExitAction.MOVE_SL,
            reason=ExitReason.TRAIL_SL,
            stop_price=new_stop,
            state=next_state,
            favourable_ticks=favourable / self.TICK_SIZE if self.TICK_SIZE > 0 else 0.0,
        )


def _engine_config(params: Any, symbol: str) -> BacktestConfig:
    return BacktestConfig(
        strategies=["trend"],
        initial_capital=INITIAL_CAPITAL,
        symbol=symbol,
        commission_rt=get_commission_rt(params.contract_id),
        fees_rt=get_fees_rt(params.contract_id),
        value_area_pct=float(getattr(params, "value_area_pct", 0.80)),
    )


def _build_engine(params: Any, symbol: str, variant: ExtendedVariant) -> BacktestEngine:
    engine = ContinuousTrailEngine(
        config=_engine_config(params, symbol),
        strategy_params=params,
        record_equity=False,
        trail_variant=variant,
    )
    engine.trend_follow = ExtendedPiSignalStrategy(params, variant)
    engine._pending_max_age = engine.trend_follow.PENDING_TIMEOUT_CANDLES
    return engine


def _run(params: Any, symbol: str, variant: ExtendedVariant, candles: list[Candle]) -> list[Any]:
    return _build_engine(params, symbol, variant).run(candles).trades


def _excursion_rows(
    trades: Iterable[Any],
    candles: list[Candle],
    variant: ExtendedVariant,
) -> list[dict[str, Any]]:
    ordered = sorted(candles, key=lambda candle: as_utc(candle.timestamp))
    times = [as_utc(candle.timestamp) for candle in ordered]
    rows: list[dict[str, Any]] = []
    for trade in sorted(trades, key=lambda item: as_utc(item.entry_time)):
        entry_time = as_utc(trade.entry_time)
        exit_time = as_utc(trade.exit_time) if trade.exit_time else entry_time
        start = bisect_right(times, entry_time)
        end = bisect_right(times, exit_time)
        segment = ordered[start:end]
        entry = float(trade.entry_price)
        if trade.direction == Direction.BUY:
            mfe = max((float(candle.high) - entry for candle in segment), default=0.0)
            mae = max((entry - float(candle.low) for candle in segment), default=0.0)
        else:
            mfe = max((entry - float(candle.low) for candle in segment), default=0.0)
            mae = max((float(candle.high) - entry for candle in segment), default=0.0)
        mfe = max(0.0, mfe)
        mae = max(0.0, mae)
        tp_distance = abs(float(trade.original_tp_price) - entry)
        initial_r = abs(entry - float(trade.original_sl_price))
        trigger_points = initial_r * variant.activation_r if variant.mode != "none" else None
        reached_trigger = None if trigger_points is None else mfe + 1e-9 >= trigger_points
        reached_tp = mfe + 1e-9 >= tp_distance if tp_distance > 0 else False
        pnl = float(trade.pnl or 0.0)
        reason = getattr(getattr(trade, "exit_reason", None), "value", None) or "unknown"
        meta = getattr(trade, "meta", {}) or {}
        kind = str((meta.get("pi") or {}).get("kind", "unknown")) if isinstance(meta, dict) else "unknown"
        rows.append(
            {
                "entry_time": entry_time,
                "exit_time": exit_time,
                "direction": "long" if trade.direction == Direction.BUY else "short",
                "kind": kind,
                "pnl": round(pnl, 8),
                "exit_reason": reason,
                "mfe_points": round(mfe, 8),
                "mae_points": round(mae, 8),
                "initial_r_points": round(initial_r, 8),
                "tp_distance_points": round(tp_distance, 8),
                "mfe_to_tp": round(mfe / tp_distance, 8) if tp_distance > 0 else None,
                "mfe_r": round(mfe / initial_r, 8) if initial_r > 0 else None,
                "trigger_points": round(trigger_points, 8) if trigger_points is not None else None,
                "trigger_to_tp": round(trigger_points / tp_distance, 8)
                if trigger_points is not None and tp_distance > 0 else None,
                "reached_trigger": reached_trigger,
                "reached_original_tp": reached_tp,
                "loss_before_original_tp": pnl < 0 and not reached_tp,
                "loss_at_initial_sl": pnl < 0 and reason == "sl",
                "loss_at_initial_sl_before_trigger": pnl < 0 and reason == "sl" and reached_trigger is not True,
                "loss_at_trail_sl": pnl < 0 and reason == "trail_sl",
                "loss_flatten_before_tp": pnl < 0 and reason == "flatten" and not reached_tp,
                "profit_flatten_before_tp": pnl > 0 and reason in {"flatten", "trail_sl"} and not reached_tp,
            }
        )
    return rows


def _exit_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return dict(sorted(Counter(str(row.get("exit_reason") or "unknown") for row in rows).items()))


def _compact_row(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key in ("entry_time", "exit_time"):
        value = result.get(key)
        if isinstance(value, datetime):
            result[key] = as_utc(value).isoformat()
    return result


def _write_trade_csv(rows_by_key: dict[tuple[str, str], list[dict[str, Any]]], path: Path) -> None:
    fields = [
        "symbol", "variant", "entry_time", "exit_time", "direction", "kind", "pnl",
        "exit_reason", "mfe_points", "mae_points", "initial_r_points", "tp_distance_points",
        "mfe_to_tp", "mfe_r", "trigger_points", "trigger_to_tp", "reached_trigger",
        "reached_original_tp", "loss_before_original_tp", "loss_at_initial_sl",
        "loss_at_initial_sl_before_trigger", "loss_at_trail_sl", "loss_flatten_before_tp",
        "profit_flatten_before_tp",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for (symbol, variant), rows in rows_by_key.items():
            for row in rows:
                output = _compact_row(row)
                output["symbol"] = symbol
                output["variant"] = variant
                writer.writerow(output)


def _write_markdown(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# PI extended trailing-exit study",
        "",
        f"- Version: `{report['study_version']}`",
        f"- Generated: `{report['generated_at']}`",
        "- Entries: production `PiSignalStrategy`; simulation: production `BacktestEngine`.",
        "- Costs: canonical commission + fees; 14-tick round-trip stress.",
        "- This is research-only; no live preset or production exit rule changed.",
        "- Continuous trail uses each post-fill candle high/low and updates the stop for the next candle.",
        "- Ladder uses the shared production R-ladder kernel and is close-observed, not a new live mode.",
        "- PI history is under 12 months; no result can be called durable.",
        "",
        "## Baseline reproduction",
        "",
    ]
    for symbol, item in report["reproduction"].items():
        base = item["baseline"]
        stress = item["stress_14t"]
        lines.append(
            f"- `{symbol}` match={item['match']} / n={base['n']} / "
            f"PnL=${base['pnl']:,.0f} / PF={base['pf']:.2f} / 14t PF={stress['pf']:.2f}"
        )
    lines.extend([
        "",
        "## Pre-declared variants",
        "",
        "| Symbol | Variant | Status | n | PnL | PF | 14t PF | Last WF PF | Armed | Trail exits |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for result in report["results"]:
        base = result["robustness"]["baseline"]
        stress = result["robustness"]["stress_14t"]
        segments = result["robustness"]["walk_forward"].get("segments") or []
        last_pf = segments[-1].get("pf") if segments else None
        diag = result["diagnostics"]
        lines.append(
            f"| {result['symbol']} | {result['label']} | {result['status']} | "
            f"{base['n']} | ${base['pnl']:,.0f} | {base['pf']:.2f} | {stress['pf']:.2f} | "
            f"{last_pf:.2f} | {diag['reached_trigger']} | "
            f"{result['exit_counts'].get('trail_sl', 0)} |"
            if last_pf is not None else
            f"| {result['symbol']} | {result['label']} | {result['status']} | "
            f"{base['n']} | ${base['pnl']:,.0f} | {base['pf']:.2f} | {stress['pf']:.2f} | "
            f"n/a | {diag['reached_trigger']} | {result['exit_counts'].get('trail_sl', 0)} |"
        )
    lines.extend([
        "",
        "## Interpretation",
        "",
        "- `NO_EDGE` means baseline PF≤1 or 14-tick PF≤1 after costs.",
        "- `REGIME_DEPENDENT` means the full-sample result is positive but walk-forward or the latest segment fails.",
        "- `COVERAGE_LIMITED_CANDIDATE` is only a mechanical pass; the sample is still too short for promotion.",
        "- A high full-sample PF after selecting a trail on the same six-month sample is not out-of-sample evidence.",
        "- A trail cannot repair losses that hit the initial SL before activation; those paths are shown in the JSON diagnostics.",
        "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


def _result(symbol: str, variant: ExtendedVariant, params: Any, candles: list[Candle], trades: list[Any]) -> dict[str, Any]:
    rows = trade_rows(trades, symbol)
    robustness = _robustness(rows)
    status, reasons = _status(robustness)
    excursions = _excursion_rows(trades, candles, variant)
    return {
        "symbol": symbol,
        "key": variant.key,
        "label": variant.label,
        "status": status,
        "status_reasons": reasons,
        "variant": {
            "mode": variant.mode,
            "activation_r": variant.activation_r,
            "gap_r": variant.gap_r,
            "hard_tp": variant.hard_tp,
            "long_hold_min": variant.long_hold,
            "short_hold_min": variant.short_hold,
        },
        "contract_id": params.contract_id,
        "contract_size": params.contract_size,
        "robustness": robustness,
        "diagnostics": _diagnostics(excursions),
        "monthly": _monthly_stats(rows),
        "exit_counts": _exit_counts(rows),
        "by_direction": {
            direction: {
                "trades": sum(row["direction"] == direction for row in excursions),
                "pnl": round(sum(row["pnl"] for row in excursions if row["direction"] == direction), 2),
            }
            for direction in ("long", "short")
        },
        "trades": excursions,
    }


def _strip_trade_rows(result: dict[str, Any]) -> dict[str, Any]:
    output = dict(result)
    output.pop("trades", None)
    return output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="MNQ,MES", help="comma-separated symbols")
    parser.add_argument("--out", default="", help="external JSON output path")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.disable(logging.CRITICAL)
    symbols = tuple(symbol.strip().upper() for symbol in str(args.symbols).split(",") if symbol.strip())
    invalid = set(symbols) - set(PI_SYMBOLS)
    if not symbols or invalid:
        raise SystemExit(f"symbols must be a subset of {PI_SYMBOLS}")

    from backend.data.pi_history import load_rows, parse_ts

    pi_rows = load_rows()
    if not pi_rows:
        raise SystemExit("canonical PI history is empty")
    report: dict[str, Any] = {
        "study_version": STUDY_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(),
        "method": {
            "engine": "production BacktestEngine with research-only continuous adapter",
            "entries": "production PI entry logic",
            "continuous_intrabar": "post-fill candle high/low observed after existing SL/TP check; new stop applies next candle",
            "ladder_intrabar": "shared evaluate_exit_operation close-observed R ladder",
            "stress_ticks": 14,
            "same_sample_selection_warning": True,
            "production_changed": False,
        },
        "pi_history": {
            "rows": len(pi_rows),
            "first": min(parse_ts(row["ts"]) for row in pi_rows).isoformat(),
            "last": max(parse_ts(row["ts"]) for row in pi_rows).isoformat(),
        },
        "data": {},
        "reproduction": {},
        "results": [],
    }

    candles_by_symbol: dict[str, list[Candle]] = {}
    params_by_symbol: dict[str, Any] = {}
    for symbol in symbols:
        print(f"Loading {symbol} PI window...", flush=True)
        candles, coverage = _load_run_candles(symbol, pi_rows)
        base = _load_base_params(symbol)
        params = _actual_params(symbol, base)
        candles_by_symbol[symbol] = candles
        params_by_symbol[symbol] = params
        report["data"][symbol] = coverage
        print(f"  {len(candles):,} candles / {coverage['first_signal']} -> {coverage['last_signal']}", flush=True)

        production = BacktestEngine(
            config=_engine_config(params, symbol),
            strategy_params=params,
            record_equity=False,
        ).run(candles).trades
        baseline = _run(params, symbol, CURRENT_VARIANT, candles)
        if _fingerprint(production) != _fingerprint(baseline):
            raise RuntimeError(
                f"current PI reproduction mismatch for {symbol}: "
                f"production={len(production)} extended={len(baseline)}"
            )
        baseline_rows = trade_rows(production, symbol)
        baseline_rob = _robustness(baseline_rows)
        report["reproduction"][symbol] = {
            "match": True,
            "trades": len(production),
            "baseline": baseline_rob["baseline"],
            "stress_14t": baseline_rob["stress_14t"],
            "contract_id": params.contract_id,
            "contract_size": params.contract_size,
        }
        print(
            f"  reproduction OK: n={len(production)} PF={baseline_rob['baseline']['pf']:.2f} "
            f"14tPF={baseline_rob['stress_14t']['pf']:.2f}",
            flush=True,
        )

    raw_rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
    total = len(symbols) * len(EXTENDED_VARIANTS)
    index = 0
    for symbol in symbols:
        params = params_by_symbol[symbol]
        candles = candles_by_symbol[symbol]
        for variant in EXTENDED_VARIANTS:
            index += 1
            started = time.perf_counter()
            trades = _run(params, symbol, variant, candles)
            item = _result(symbol, variant, params, candles, trades)
            raw_rows[(symbol, variant.key)] = item["trades"]
            report["results"].append(_strip_trade_rows(item))
            base = item["robustness"]["baseline"]
            stress = item["robustness"]["stress_14t"]
            diag = item["diagnostics"]
            print(
                f"[{index}/{total}] {symbol} {variant.key}: {item['status']} "
                f"n={base['n']} PnL=${base['pnl']:,.0f} PF={base['pf']:.2f} "
                f"14tPF={stress['pf']:.2f} armed={diag['reached_trigger']} "
                f"({time.perf_counter() - started:.1f}s)",
                flush=True,
            )

    output = Path(args.out).expanduser() if args.out else market_data.derived_path(
        "research", "pi_trail_extended_study_latest.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    _write_trade_csv(raw_rows, output.with_name(output.stem + "_trades.csv"))
    _write_markdown(report, output.with_suffix(".md"))
    print(f"JSON: {output}", flush=True)
    print(f"Trades CSV: {output.with_name(output.stem + '_trades.csv')}", flush=True)
    print(f"Report: {output.with_suffix('.md')}", flush=True)


if __name__ == "__main__":
    main()
