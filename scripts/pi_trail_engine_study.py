"""Research-only PI trailing-exit and time-exit replay.

This script answers a narrow question raised after the PI drawdown: does a
one-time profit lock help when it is armed after a fixed dollar move or after
10%/70% of the original TP distance, and what happens when the original TP is
replaced by a time exit?

The production ``BacktestEngine`` and the production PI entry logic are used.
Only the exit policy attached to each research signal is changed.  No preset,
live setting, market-data file, or production strategy is changed.  Reports
are written to the external ``ancserMarketData`` research tree.

Interpretation of the requested dollar threshold:

* ``$100`` is gross favourable PnL for the whole research position, not per
  contract.  Thus it is 25 MNQ points for the two-contract PI position and 20
  MES points for one contract.
* A 5%/10%/50% lock moves the stop to entry plus/minus that fraction of the
  original entry-to-TP distance after the trigger.  It is not a trailing
  offset that follows every new high/low; it is the shared one-time ratchet.
* MFE is measured only on candles after the fill, including the exit candle.
  The entry candle is excluded because the engine deliberately treats a limit
  fill as occurring after the candle's pre-fill excursion.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
import time
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.backtest.engine import BacktestConfig, BacktestEngine  # noqa: E402
from backend.data import candle_store, market_data  # noqa: E402
from backend.db.models import (  # noqa: E402
    Candle,
    Direction,
    get_commission_rt,
    get_fees_rt,
    get_point_value,
)
from backend.strategy.exit_policy import ExitPolicy, ExitTrailMode  # noqa: E402
from backend.strategy.pi_signal import PiSignalStrategy  # noqa: E402
from backend.timebase import UTC, as_utc  # noqa: E402
from scripts.pi_exit_engine_grid_study import (  # noqa: E402
    CURRENT_EXIT,
    PI_SOURCE_SYMBOL,
    SIGNAL_SETS,
    SignalSet,
    _configure_params,
    _fingerprint,
    _load_base_params,
    _load_run_candles,
    _robustness,
    _status,
    trade_rows,
)


STUDY_VERSION = "2026-09-14-pi-trail-engine-v1"
INITIAL_CAPITAL = 50_000.0
PI_SYMBOLS = ("MNQ", "MES")
CURRENT_SIGNALS = SIGNAL_SETS[0]


@dataclass(frozen=True)
class TrailVariant:
    key: str
    label: str
    trigger_mode: str  # off, dollars, pct_tp, time
    trigger_value: float
    lock_pct: float
    long_hold: int
    short_hold: int
    hard_tp: bool = True


CURRENT_VARIANT = TrailVariant(
    "current",
    "CURRENT · hard TP3R / long OFF / short60",
    "off",
    0.0,
    0.0,
    0,
    60,
)


TRAIL_VARIANTS: tuple[TrailVariant, ...] = (
    CURRENT_VARIANT,
    TrailVariant("dollar100_lock5", "trigger ≥$100 · lock 5% TP", "dollars", 100.0, 0.05, 0, 60),
    TrailVariant("dollar100_lock10", "trigger ≥$100 · lock 10% TP", "dollars", 100.0, 0.10, 0, 60),
    TrailVariant("dollar100_lock50", "trigger ≥$100 · lock 50% TP", "dollars", 100.0, 0.50, 0, 60),
    TrailVariant("pct10_lock5", "trigger 10% TP · lock 5% TP", "pct_tp", 0.10, 0.05, 0, 60),
    TrailVariant("pct10_lock10", "trigger 10% TP · lock 10% TP", "pct_tp", 0.10, 0.10, 0, 60),
    TrailVariant("pct10_lock50", "trigger 10% TP · lock 50% TP", "pct_tp", 0.10, 0.50, 0, 60),
    TrailVariant("pct70_lock5", "trigger 70% TP · lock 5% TP", "pct_tp", 0.70, 0.05, 0, 60),
    TrailVariant("pct70_lock10", "trigger 70% TP · lock 10% TP", "pct_tp", 0.70, 0.10, 0, 60),
    TrailVariant("pct70_lock50", "trigger 70% TP · lock 50% TP", "pct_tp", 0.70, 0.50, 0, 60),
    TrailVariant("time_asym", "TIME only · long OFF / short60", "time", 0.0, 0.0, 0, 60, False),
    TrailVariant("time15", "TIME only · 15m both sides", "time", 0.0, 0.0, 15, 15, False),
    TrailVariant("time30", "TIME only · 30m both sides", "time", 0.0, 0.0, 30, 30, False),
    TrailVariant("time60", "TIME only · 60m both sides", "time", 0.0, 0.0, 60, 60, False),
    TrailVariant("time120", "TIME only · 120m both sides", "time", 0.0, 0.0, 120, 120, False),
    TrailVariant("time240", "TIME only · 240m both sides", "time", 0.0, 0.0, 240, 240, False),
)


class TrailPiSignalStrategy(PiSignalStrategy):
    """Production PI entries with one explicit research exit policy."""

    def __init__(self, params: Any, variant: TrailVariant):
        self._trail_variant = variant
        super().__init__(params)

    def _policy_for_signal(self, signal: Any) -> ExitPolicy:
        variant = self._trail_variant
        hold = variant.long_hold if signal.direction == Direction.BUY else variant.short_hold
        if variant.trigger_mode == "time":
            return ExitPolicy(
                model="pi",
                max_hold_minutes=max(0, int(hold)),
                hard_tp_enabled=False,
                trail_mode=ExitTrailMode.NONE,
            )

        tp_distance = abs(float(signal.tp_price) - float(signal.entry_price))
        if variant.trigger_mode == "dollars":
            point_value = float(get_point_value(self.params.contract_id))
            contracts = max(1, int(getattr(self.params, "contract_size", 1) or 1))
            trigger_points = variant.trigger_value / max(1e-12, point_value * contracts)
            trigger_pct = trigger_points / tp_distance if tp_distance > 0 else 0.0
        elif variant.trigger_mode == "pct_tp":
            trigger_pct = max(0.0, float(variant.trigger_value))
        else:
            trigger_pct = 0.0

        return ExitPolicy(
            model="pi",
            max_hold_minutes=max(0, int(hold)),
            hard_tp_enabled=bool(variant.hard_tp),
            trail_mode=ExitTrailMode.SINGLE if trigger_pct > 0 else ExitTrailMode.NONE,
            trail_trigger_pct=trigger_pct,
            trail_lock_pct=max(0.0, float(variant.lock_pct)),
        )

    def evaluate(self, candle: Candle, zones=None, is_mature: bool = True):
        signal = super().evaluate(candle, zones, is_mature)
        if signal is not None:
            signal.exit_policy = self._policy_for_signal(signal)
        return signal


def _engine_config(params: Any, symbol: str) -> BacktestConfig:
    return BacktestConfig(
        strategies=["trend"],
        initial_capital=INITIAL_CAPITAL,
        symbol=symbol,
        commission_rt=get_commission_rt(params.contract_id),
        fees_rt=get_fees_rt(params.contract_id),
        value_area_pct=float(getattr(params, "value_area_pct", 0.80)),
    )


def _build_engine(params: Any, variant: TrailVariant) -> BacktestEngine:
    symbol = str(getattr(params, "contract_id", "")).split(".")[-2].upper()
    engine = BacktestEngine(
        config=_engine_config(params, symbol),
        strategy_params=params,
        record_equity=False,
    )
    engine.trend_follow = TrailPiSignalStrategy(params, variant)
    engine._pending_max_age = engine.trend_follow.PENDING_TIMEOUT_CANDLES
    return engine


def _run(params: Any, variant: TrailVariant, candles: list[Candle]) -> list[Any]:
    return _build_engine(params, variant).run(candles).trades


def _actual_params(symbol: str, base: Any) -> Any:
    params = _configure_params(base, CURRENT_EXIT, CURRENT_SIGNALS)
    # The dollar trigger is intentionally account-position based.  The current
    # PI preset is 2 MNQ; MES is replayed as one micro contract.
    params.contract_size = 2 if symbol == "MNQ" else 1
    return params


def _reproduction(symbol: str, candles: list[Candle], base: Any) -> dict[str, Any]:
    params = _actual_params(symbol, base)
    production_trades = BacktestEngine(
        config=_engine_config(params, symbol),
        strategy_params=params,
        record_equity=False,
    ).run(candles).trades
    research_trades = _run(params, CURRENT_VARIANT, candles)
    if _fingerprint(production_trades) != _fingerprint(research_trades):
        raise RuntimeError(
            f"current PI reproduction mismatch for {symbol}: "
            f"production={len(production_trades)} research={len(research_trades)}"
        )
    rob = _robustness(trade_rows(production_trades, symbol))
    return {
        "match": True,
        "trades": len(production_trades),
        "baseline": rob["baseline"],
        "stress_14t": rob["stress_14t"],
        "contract_id": params.contract_id,
        "contract_size": params.contract_size,
        "dollar_trigger_definition": "gross favourable PnL for the whole position",
    }


def _trigger_points(variant: TrailVariant, trade: Any, params: Any) -> Optional[float]:
    if variant.trigger_mode == "dollars":
        point_value = float(get_point_value(params.contract_id))
        contracts = max(1, int(getattr(params, "contract_size", 1) or 1))
        return float(variant.trigger_value) / max(1e-12, point_value * contracts)
    if variant.trigger_mode == "pct_tp":
        tp_distance = abs(float(trade.original_tp_price) - float(trade.entry_price))
        return tp_distance * max(0.0, float(variant.trigger_value))
    return None


def _excursion_rows(
    trades: Iterable[Any],
    candles: list[Candle],
    params: Any,
    variant: TrailVariant,
) -> list[dict[str, Any]]:
    ordered = sorted(candles, key=lambda c: as_utc(c.timestamp))
    times = [as_utc(c.timestamp) for c in ordered]
    rows: list[dict[str, Any]] = []
    for trade in sorted(trades, key=lambda item: as_utc(item.entry_time)):
        entry_time = as_utc(trade.entry_time)
        exit_time = as_utc(trade.exit_time) if trade.exit_time else entry_time
        # Exclude the fill candle; include the exit candle.
        start = bisect_right(times, entry_time)
        end = bisect_right(times, exit_time)
        segment = ordered[start:end]
        entry = float(trade.entry_price)
        if trade.direction == Direction.BUY:
            mfe = max((float(c.high) - entry for c in segment), default=0.0)
            mae = max((entry - float(c.low) for c in segment), default=0.0)
        else:
            mfe = max((entry - float(c.low) for c in segment), default=0.0)
            mae = max((float(c.high) - entry for c in segment), default=0.0)
        mfe = max(0.0, mfe)
        mae = max(0.0, mae)
        tp_distance = abs(float(trade.original_tp_price) - entry)
        initial_r = abs(entry - float(trade.original_sl_price))
        trigger_points = _trigger_points(variant, trade, params)
        reason = getattr(getattr(trade, "exit_reason", None), "value", None) or "unknown"
        pnl = float(trade.pnl or 0.0)
        reached_trigger = (
            None if trigger_points is None else mfe + 1e-9 >= trigger_points
        )
        reached_tp = mfe + 1e-9 >= tp_distance if tp_distance > 0 else False
        rows.append(
            {
                "entry_time": entry_time,
                "exit_time": exit_time,
                "direction": "long" if trade.direction == Direction.BUY else "short",
                "kind": str((getattr(trade, "meta", {}) or {}).get("pi", {}).get("kind", "unknown")),
                "pnl": round(pnl, 8),
                "exit_reason": reason,
                "mfe_points": round(mfe, 8),
                "mae_points": round(mae, 8),
                "initial_r_points": round(initial_r, 8),
                "tp_distance_points": round(tp_distance, 8),
                "mfe_to_tp": round(mfe / tp_distance, 8) if tp_distance > 0 else None,
                "mfe_r": round(mfe / initial_r, 8) if initial_r > 0 else None,
                "trigger_points": round(trigger_points, 8) if trigger_points is not None else None,
                "trigger_to_tp": (
                    round(trigger_points / tp_distance, 8)
                    if trigger_points is not None and tp_distance > 0 else None
                ),
                "reached_trigger": reached_trigger,
                "reached_original_tp": reached_tp,
                "loss_before_original_tp": pnl < 0 and not reached_tp,
                "loss_at_initial_sl": pnl < 0 and reason == "sl",
                "loss_at_initial_sl_before_trigger": (
                    pnl < 0 and reason == "sl" and reached_trigger is not True
                ),
                "loss_at_trail_sl": pnl < 0 and reason == "trail_sl",
                "loss_flatten_before_tp": pnl < 0 and reason == "flatten" and not reached_tp,
                "profit_flatten_before_tp": pnl > 0 and reason in {"flatten", "trail_sl"} and not reached_tp,
            }
        )
    return rows


def _diagnostics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    reasons = Counter(str(row.get("exit_reason") or "unknown") for row in rows)
    losses = [row for row in rows if float(row.get("pnl") or 0.0) < 0]
    mfe_to_tp = [float(row["mfe_to_tp"]) for row in rows if row.get("mfe_to_tp") is not None]
    loss_mfe_to_tp = [float(row["mfe_to_tp"]) for row in losses if row.get("mfe_to_tp") is not None]
    trigger_rows = [row for row in rows if row.get("reached_trigger") is True]
    tp_rows = [row for row in rows if row.get("reached_original_tp") is True]
    return {
        "trades": len(rows),
        "exit_reasons": dict(sorted(reasons.items())),
        "reached_trigger": len(trigger_rows),
        "reached_original_tp": len(tp_rows),
        "losses": len(losses),
        "losses_before_original_tp": sum(bool(row["loss_before_original_tp"]) for row in losses),
        "losses_at_initial_sl": sum(bool(row["loss_at_initial_sl"]) for row in losses),
        "losses_at_initial_sl_before_trigger": sum(
            bool(row["loss_at_initial_sl_before_trigger"]) for row in losses
        ),
        "losses_at_trail_sl": sum(bool(row["loss_at_trail_sl"]) for row in losses),
        "losses_flatten_before_tp": sum(bool(row["loss_flatten_before_tp"]) for row in losses),
        "losses_reached_trigger_but_not_tp": sum(
            row.get("reached_trigger") is True
            and not bool(row.get("reached_original_tp"))
            for row in losses
        ),
        "profit_flatten_before_tp": sum(bool(row["profit_flatten_before_tp"]) for row in rows),
        "avg_mfe_to_tp": round(sum(mfe_to_tp) / len(mfe_to_tp), 4) if mfe_to_tp else None,
        "median_mfe_to_tp": round(median(mfe_to_tp), 4) if mfe_to_tp else None,
        "avg_loss_mfe_to_tp": round(sum(loss_mfe_to_tp) / len(loss_mfe_to_tp), 4)
        if loss_mfe_to_tp else None,
        "median_loss_mfe_to_tp": round(median(loss_mfe_to_tp), 4)
        if loss_mfe_to_tp else None,
        "loss_examples": [
            {
                "entry_time": row["entry_time"].isoformat(),
                "direction": row["direction"],
                "pnl": row["pnl"],
                "exit_reason": row["exit_reason"],
                "mfe_to_tp": row["mfe_to_tp"],
            }
            for row in sorted(losses, key=lambda item: float(item.get("pnl") or 0.0))[:5]
        ],
    }


def _compact_row(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for key in ("entry_time", "exit_time"):
        value = out.get(key)
        if isinstance(value, datetime):
            out[key] = as_utc(value).isoformat()
    return out


def _write_trade_csv(rows_by_key: dict[tuple[str, str], list[dict[str, Any]]], path: Path) -> None:
    rows: list[dict[str, Any]] = []
    for (symbol, key), items in rows_by_key.items():
        for item in items:
            row = _compact_row(item)
            row["symbol"] = symbol
            row["variant"] = key
            rows.append(row)
    fieldnames = [
        "symbol", "variant", "entry_time", "exit_time", "direction", "kind", "pnl",
        "exit_reason", "mfe_points", "mae_points", "initial_r_points", "tp_distance_points",
        "mfe_to_tp", "mfe_r", "trigger_points", "trigger_to_tp", "reached_trigger",
        "reached_original_tp", "loss_before_original_tp", "loss_at_initial_sl",
        "loss_at_initial_sl_before_trigger", "loss_at_trail_sl", "loss_flatten_before_tp",
        "profit_flatten_before_tp",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_markdown(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# PI trailing and time-exit study",
        "",
        f"- Version: `{report['study_version']}`",
        f"- Generated: `{report['generated_at']}`",
        "- Engine: production `BacktestEngine`; production configuration unchanged.",
        "- Baseline: current PI entries, hard TP 3R, long hold OFF, short hold 60m.",
        "- A dollar trigger is gross favourable PnL for the whole position.",
        "- MFE excludes the fill candle and includes the exit candle.",
        "",
        "## Reproduction",
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
        "## Variants",
        "",
        "| Symbol | Variant | Status | n | PnL | PF | 14t PF | Triggered | Original TP reached | Losses before TP | Initial SL before trigger | Trail SL losses | Time flatten losses |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for item in report["results"]:
        rob = item["robustness"]
        diag = item["diagnostics"]
        lines.append(
            f"| {item['symbol']} | {item['label']} | {item['status']} | "
            f"{rob['baseline']['n']} | ${rob['baseline']['pnl']:,.0f} | {rob['baseline']['pf']:.2f} | "
            f"{rob['stress_14t']['pf']:.2f} | {diag['reached_trigger']} | "
            f"{diag['reached_original_tp']} | {diag['losses_before_original_tp']} | "
            f"{diag['losses_at_initial_sl_before_trigger']} | {diag['losses_at_trail_sl']} | "
            f"{diag['losses_flatten_before_tp']} |"
        )
    lines.extend([
        "",
        "## Reading the loss-path columns",
        "",
        "- `Initial SL before trigger` means the trade failed before the requested lock could arm; a lock cannot repair that path.",
        "- `Losses before TP` includes both initial-stop losses and time/session exits whose excursion never reached the original TP.",
        "- `Trail SL losses` is a loss after the one-time stop move; a positive trail-protected exit is counted separately in the JSON diagnostics.",
        "- For `TIME only`, original TP is counterfactual: it was not active, but MFE still says whether price would have reached it.",
        "- Positive full-sample PF is not promotion evidence: the PI history is short and variants are selected on the same sample.",
        "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="MNQ,MES", help="comma-separated symbols")
    parser.add_argument("--out", default="", help="external JSON output path")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.disable(logging.CRITICAL)
    symbols = tuple(
        symbol.strip().upper()
        for symbol in str(args.symbols).split(",")
        if symbol.strip()
    )
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
            "engine": "production BacktestEngine",
            "baseline": "current PI 2MNQ BOTH BEST entry logic and current hard TP/time policy",
            "position_contracts": {"MNQ": 2, "MES": 1},
            "dollar_trigger": "gross favourable PnL for the whole position",
            "dollar_trigger_points": {"MNQ": 25.0, "MES": 20.0},
            "lock_definition": "entry +/- lock_pct * original TP distance after one-time trigger",
            "mfe_definition": "post-fill candles through exit candle; fill candle excluded",
            "time_only_tp": "original TP retained only as a counterfactual for MFE diagnostics; hard TP disabled",
            "production_changed": False,
            "same_sample_selection_warning": True,
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
        candles_by_symbol[symbol] = candles
        base = _load_base_params(symbol)
        params_by_symbol[symbol] = _actual_params(symbol, base)
        report["data"][symbol] = coverage
        print(
            f"  {len(candles):,} candles / {coverage['first_signal']} -> {coverage['last_signal']}",
            flush=True,
        )
        started = time.perf_counter()
        report["reproduction"][symbol] = _reproduction(symbol, candles, base)
        item = report["reproduction"][symbol]
        print(
            f"  reproduction OK: n={item['trades']} PF={item['baseline']['pf']:.2f} "
            f"14tPF={item['stress_14t']['pf']:.2f} ({time.perf_counter() - started:.1f}s)",
            flush=True,
        )

    trade_rows_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    total = len(symbols) * len(TRAIL_VARIANTS)
    index = 0
    for symbol in symbols:
        params = params_by_symbol[symbol]
        candles = candles_by_symbol[symbol]
        times = [as_utc(c.timestamp) for c in candles]
        for variant in TRAIL_VARIANTS:
            index += 1
            started = time.perf_counter()
            trades = _run(params, variant, candles)
            base_rows = trade_rows(trades, symbol)
            robustness = _robustness(base_rows)
            status, reasons = _status(robustness)
            excursions = _excursion_rows(trades, candles, params, variant)
            # Keep this assertion close to the calculation: a missing or
            # reordered trade row would make the loss-path counts misleading.
            if len(excursions) != len(trades) or any(
                row["entry_time"] not in times and False for row in excursions
            ):
                raise RuntimeError(f"excursion row mismatch for {symbol}/{variant.key}")
            trade_rows_by_key[(symbol, variant.key)] = excursions
            result = {
                "symbol": symbol,
                "key": variant.key,
                "label": variant.label,
                "status": status,
                "status_reasons": reasons,
                "variant": {
                    "trigger_mode": variant.trigger_mode,
                    "trigger_value": variant.trigger_value,
                    "lock_pct": variant.lock_pct,
                    "long_hold_min": variant.long_hold,
                    "short_hold_min": variant.short_hold,
                    "hard_tp": variant.hard_tp,
                },
                "position_contracts": params.contract_size,
                "robustness": robustness,
                "diagnostics": _diagnostics(excursions),
                "by_direction": {
                    direction: {
                        "trades": sum(row["direction"] == direction for row in excursions),
                        "pnl": round(sum(row["pnl"] for row in excursions if row["direction"] == direction), 2),
                        "losses_before_tp": sum(
                            row["direction"] == direction and row["loss_before_original_tp"]
                            for row in excursions
                        ),
                    }
                    for direction in ("long", "short")
                },
            }
            report["results"].append(result)
            rob = robustness["baseline"]
            stress = robustness["stress_14t"]
            diag = result["diagnostics"]
            print(
                f"[{index}/{total}] {symbol} {variant.key}: {status} "
                f"n={rob['n']} PnL=${rob['pnl']:,.0f} PF={rob['pf']:.2f} "
                f"14tPF={stress['pf']:.2f} trigger={diag['reached_trigger']} "
                f"TP={diag['reached_original_tp']} ({time.perf_counter() - started:.1f}s)",
                flush=True,
            )

    output = Path(args.out).expanduser() if args.out else market_data.derived_path(
        "research", "pi_trail_engine_study_latest.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_trade_csv(trade_rows_by_key, output.with_name(output.stem + "_trades.csv"))
    _write_markdown(report, output.with_suffix(".md"))
    print(f"JSON: {output}", flush=True)
    print(f"Trades CSV: {output.with_name(output.stem + '_trades.csv')}", flush=True)
    print(f"Report: {output.with_suffix('.md')}", flush=True)


if __name__ == "__main__":
    main()
