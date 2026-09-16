"""Research-only PI exit and signal durability study.

This study deliberately runs the production :class:`BacktestEngine` instead
of the older isolated PI simulator.  It answers three separate questions:

* does the current ``PI 2MNQ BOTH BEST`` preset reproduce before variants are
  compared;
* which pre-declared SL/TP/time-exit combinations survive costs and a
  round-trip 14-tick stress; and
* which PI mark kinds still have an edge when the current exit contract is
  held fixed.

No preset, live setting, market-data store, or production strategy is changed.
Reports are written to the external ``ancserMarketData`` research tree.

The data currently covers only several months of PI marks.  The report
therefore uses ``COVERAGE_LIMITED`` language for every result and treats
walk-forward/monthly consistency as evidence of regime dependence, not proof
of a permanent edge.
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import logging
import math
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.backtest.engine import BacktestConfig, BacktestEngine  # noqa: E402
from backend.backtest.robustness import evaluate, series_stats  # noqa: E402
from backend.data import candle_store, market_data  # noqa: E402
from backend.data.pi_history import load_rows, parse_ts  # noqa: E402
from backend.db.models import (  # noqa: E402
    Candle,
    Direction,
    ExitReason,
    _extract_symbol,
    current_quarterly_contract_id,
    get_commission_rt,
    get_fees_rt,
)
from backend.strategy.exit_policy import ExitPolicy, ExitTrailMode  # noqa: E402
from backend.strategy.pi_signal import PiSignalStrategy  # noqa: E402
from backend.timebase import UTC, as_utc  # noqa: E402
from scripts.strategy_long_term_audit import _make_params  # noqa: E402


STUDY_VERSION = "2026-09-14-pi-exit-engine-grid-v1"
INITIAL_CAPITAL = 50_000.0
STRESS_TICKS = 14
MC_ITERS = 500
MC_SEED = 20260914
DD_THRESHOLD = 2_000.0
PI_SYMBOLS = ("MNQ", "MES")
PI_SOURCE_SYMBOL = {"MNQ": "QQQ", "MES": "SPY"}


@dataclass(frozen=True)
class ExitVariant:
    """A pre-declared exit counterfactual.

    ``long_sl`` and ``short_sl`` are ATR-blend multipliers.  PI uses one
    global ``rr`` for both directions, matching the production parameter
    contract.  ``hard_tp=False`` means the attached stop remains active and
    the position is closed by the time policy/session flatten; it is included
    as a research counterfactual only.
    """

    key: str
    label: str
    long_sl: float
    rr: float
    short_sl: float
    long_hold: int
    short_hold: int
    hard_tp: bool = True


@dataclass(frozen=True)
class SignalSet:
    key: str
    label: str
    long_kinds: tuple[str, ...]
    short_kinds: tuple[str, ...]
    long_only: bool = False


CURRENT_EXIT = ExitVariant(
    key="current",
    label="CURRENT · L4 / TP3R / S1.5 / short60",
    long_sl=4.0,
    rr=3.0,
    short_sl=1.5,
    long_hold=0,
    short_hold=60,
)


EXIT_VARIANTS: tuple[ExitVariant, ...] = (
    CURRENT_EXIT,
    ExitVariant("rr1", "RR1 · L4 / S1.5", 4.0, 1.0, 1.5, 0, 60),
    ExitVariant("rr1_5", "RR1.5 · L4 / S1.5", 4.0, 1.5, 1.5, 0, 60),
    ExitVariant("rr2", "RR2 · L4 / S1.5", 4.0, 2.0, 1.5, 0, 60),
    ExitVariant("long_sl3_rr3", "L3 / RR3 / S1.5", 3.0, 3.0, 1.5, 0, 60),
    ExitVariant("long_sl3_5_rr3", "L3.5 / RR3 / S1.5", 3.5, 3.0, 1.5, 0, 60),
    ExitVariant("long_sl5_rr3", "L5 / RR3 / S1.5", 5.0, 3.0, 1.5, 0, 60),
    ExitVariant("short_sl2_rr3", "L4 / RR3 / S2", 4.0, 3.0, 2.0, 0, 60),
    ExitVariant("short_sl2_5_rr3", "L4 / RR3 / S2.5", 4.0, 3.0, 2.5, 0, 60),
    ExitVariant("short_no_time", "L4 / RR3 / short OFF", 4.0, 3.0, 1.5, 0, 0),
    ExitVariant("short_time30", "L4 / RR3 / short30", 4.0, 3.0, 1.5, 0, 30),
    ExitVariant("short_time120", "L4 / RR3 / short120", 4.0, 3.0, 1.5, 0, 120),
    ExitVariant("long_time60", "L4 / RR3 / long60", 4.0, 3.0, 1.5, 60, 60),
    ExitVariant("long_time120", "L4 / RR3 / long120", 4.0, 3.0, 1.5, 120, 60),
    ExitVariant("long_time240", "L4 / RR3 / long240", 4.0, 3.0, 1.5, 240, 60),
    ExitVariant(
        "no_hard_tp_time",
        "SL only + long120 / short60",
        4.0,
        3.0,
        1.5,
        120,
        60,
        hard_tp=False,
    ),
    ExitVariant(
        "no_hard_tp_session",
        "SL only + session flatten",
        4.0,
        3.0,
        1.5,
        0,
        0,
        hard_tp=False,
    ),
)


SIGNAL_SETS: tuple[SignalSet, ...] = (
    SignalSet("current_both", "CURRENT kinds", ("青π", "深蓝圈"), ("粉π",)),
    SignalSet("long_pi_only", "Blue PI + deep blue, long only", ("青π", "深蓝圈"), (), True),
    SignalSet("long_all", "All blue kinds, long only", ("青π", "深蓝圈", "淡蓝圈"), (), True),
    SignalSet("pi_strict_both", "Strict PI kinds", ("青π",), ("粉π",)),
    SignalSet("all_both", "All blue + all purple", ("淡蓝圈", "青π", "深蓝圈"), ("粉π", "紫圈")),
    SignalSet("cyan_only", "青π only", ("青π",), (), True),
    SignalSet("deep_blue_only", "深蓝圈 only", ("深蓝圈",), (), True),
    SignalSet("light_blue_only", "淡蓝圈 only", ("淡蓝圈",), (), True),
    SignalSet("pink_only", "粉π only", (), ("粉π",)),
    SignalSet("purple_only", "紫圈 only", (), ("紫圈",)),
)


class GridPiSignalStrategy(PiSignalStrategy):
    """Production PI entry logic with an explicit research exit policy."""

    def __init__(self, params: Any, variant: ExitVariant):
        self._grid_variant = variant
        super().__init__(params)

    def evaluate(self, candle: Candle, zones=None, is_mature: bool = True):
        signal = super().evaluate(candle, zones, is_mature)
        if signal is None:
            return None
        hold = (
            self._grid_variant.long_hold
            if signal.direction == Direction.BUY
            else self._grid_variant.short_hold
        )
        signal.exit_policy = ExitPolicy(
            model="pi",
            max_hold_minutes=max(0, int(hold)),
            hard_tp_enabled=bool(self._grid_variant.hard_tp),
            trail_mode=ExitTrailMode.NONE,
        )
        return signal


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return as_utc(value).isoformat()
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return datetime.fromtimestamp(float(value), tz=UTC).isoformat()
    return str(value)


def _direction(value: Any) -> str:
    raw = getattr(value, "value", value)
    text = str(raw or "").lower()
    if text in {"buy", "long", "1", "direction.buy"}:
        return "long"
    if text in {"sell", "short", "-1", "direction.sell"}:
        return "short"
    return text


def _kind(trade: Any) -> str:
    meta = getattr(trade, "meta", None) or {}
    pi = meta.get("pi") if isinstance(meta, dict) else None
    return str((pi or {}).get("kind") or "unknown")


def trade_rows(trades: Iterable[Any], symbol: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for trade in sorted(trades, key=lambda item: as_utc(item.entry_time)):
        entry = as_utc(trade.entry_time)
        exit_time = getattr(trade, "exit_time", None)
        rows.append(
            {
                "entry_time": entry,
                "exit_time": as_utc(exit_time) if exit_time else None,
                "pnl": _finite(getattr(trade, "pnl", 0.0)),
                "direction": _direction(getattr(trade, "direction", "")),
                "size": max(1.0, _finite(getattr(trade, "contracts", 1), 1.0)),
                "symbol": f"/{symbol.upper()}",
                "kind": _kind(trade),
                "exit_reason": getattr(getattr(trade, "exit_reason", None), "value", None),
            }
        )
    return rows


def _compact(values: Iterable[float]) -> dict[str, Any]:
    values = list(values)
    stats = series_stats(values)
    return {
        "n": int(stats["n"]),
        "pnl": round(float(stats["pnl"]), 2),
        "pf": round(float(stats["pf"]), 4),
        "win_rate": round(float(stats["win"]), 4),
        "max_dd": round(float(stats["max_dd"]), 2),
        "avg_trade": round(float(stats["pnl"]) / len(values), 2) if values else 0.0,
    }


def _group_stats(rows: list[dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get(field) or "unknown")].append(float(row.get("pnl") or 0.0))
    return {key: _compact(values) for key, values in sorted(grouped.items())}


def _monthly_stats(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        stamp = row.get("entry_time")
        if isinstance(stamp, datetime):
            key = as_utc(stamp).strftime("%Y-%m")
        else:
            key = "unknown"
        grouped[key].append(float(row.get("pnl") or 0.0))
    return {key: _compact(values) for key, values in sorted(grouped.items())}


def _exit_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return dict(sorted(Counter(str(row.get("exit_reason") or "unknown") for row in rows).items()))


def _robustness(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result = evaluate(
        rows,
        iters=MC_ITERS,
        seed=MC_SEED,
        dd_threshold=DD_THRESHOLD,
        slip_levels=(STRESS_TICKS,),
    )
    base = result.get("stats") or {}
    stress_rows = ((result.get("slip") or {}).get("levels") or [])
    stress = stress_rows[0].get("stats") if stress_rows else {}
    mc = result.get("monte_carlo") or {}
    wf = result.get("walk_forward") or {}
    return {
        "baseline": {
            "n": int(base.get("n", 0)),
            "pnl": round(_finite(base.get("pnl")), 2),
            "pf": round(_finite(base.get("pf")), 4),
            "win_rate": round(_finite(base.get("win")), 4),
            "max_dd": round(_finite(base.get("max_dd")), 2),
            "avg_trade": round(_finite(base.get("pnl")) / int(base.get("n", 0)), 2)
            if int(base.get("n", 0)) else 0.0,
        },
        "span_months": round(_finite(result.get("span_months")), 3),
        "start": _iso(result.get("start")),
        "end": _iso(result.get("end")),
        "monte_carlo": {
            "iters": mc.get("iters"),
            "seed": mc.get("seed"),
            "p_loss": round(_finite(mc.get("p_loss")), 4) if mc else None,
            "pf_p5": round(_finite(mc.get("pf_p5")), 4) if mc else None,
            "dd_p95": round(_finite(mc.get("dd_p95")), 2) if mc else None,
            "p_dd_breach": round(_finite(mc.get("p_dd_breach")), 4) if mc else None,
        },
        "monte_carlo_pass": bool(result.get("monte_carlo_pass")),
        "walk_forward": {
            "pass": bool(wf.get("pass")),
            "segments": [
                {
                    "n": int(segment.get("n", 0)),
                    "pnl": round(_finite(segment.get("pnl")), 2),
                    "pf": round(_finite(segment.get("pf")), 4),
                    "win_rate": round(_finite(segment.get("win")), 4),
                }
                for segment in (wf.get("segments") or [])
            ],
        },
        "stress_14t": {
            "n": int(stress.get("n", 0)),
            "pnl": round(_finite(stress.get("pnl")), 2),
            "pf": round(_finite(stress.get("pf")), 4),
            "win_rate": round(_finite(stress.get("win")), 4),
            "max_dd": round(_finite(stress.get("max_dd")), 2),
        },
    }


def _status(robustness: dict[str, Any]) -> tuple[str, list[str]]:
    """Conservative label; six months cannot earn a durable status."""
    base = robustness["baseline"]
    stress = robustness["stress_14t"]
    wf = robustness["walk_forward"]
    reasons: list[str] = []
    if base["pf"] <= 1.0:
        reasons.append(f"全期 PF {base['pf']:.2f} ≤ 1")
    if stress["pf"] <= 1.0:
        reasons.append(f"14-tick 壓力 PF {stress['pf']:.2f} ≤ 1")
    if base["n"] < 40:
        reasons.append(f"只有 {base['n']} 筆成交，統計功效不足")
    if not wf["pass"]:
        reasons.append("三段等時間 walk-forward 未全部同時獲利且 PF>1")
    segments = wf.get("segments") or []
    if segments and segments[-1].get("pf", 0.0) <= 1.0:
        reasons.append("最後一段 PF ≤ 1，近期沒有持續性證據")
    if robustness.get("span_months", 0.0) < 12.0:
        reasons.append("PI 歷史覆蓋少於 12 個月，不能宣稱長期持久")
    if base["pf"] <= 1.0 or stress["pf"] <= 1.0:
        return "NO_EDGE", reasons
    if base["n"] < 40:
        return "INSUFFICIENT_SAMPLE", reasons
    if not wf["pass"] or (segments and segments[-1].get("pf", 0.0) <= 1.0):
        return "REGIME_DEPENDENT", reasons
    return "COVERAGE_LIMITED_CANDIDATE", reasons


def _source_bounds(rows: list[dict[str, Any]], symbol: str) -> tuple[datetime, datetime]:
    stamps = []
    target = PI_SOURCE_SYMBOL[symbol]
    for row in rows:
        if str(row.get("symbol") or "").upper() != target:
            continue
        try:
            stamps.append(parse_ts(row["ts"]))
        except (KeyError, TypeError, ValueError):
            continue
    if not stamps:
        raise RuntimeError(f"no PI rows for {symbol} ({target})")
    return min(stamps), max(stamps)


def _load_run_candles(symbol: str, rows: list[dict[str, Any]]) -> tuple[list[Candle], dict[str, Any]]:
    """Load only the causal PI window, while retaining seven days of warm-up."""
    first, last = _source_bounds(rows, symbol)
    start = first - timedelta(days=7)
    end = last + timedelta(days=2)
    snapshot = candle_store.load_snapshot(symbol, 1)
    candles = candle_store.select_range(snapshot, start=start, end=end)
    if not candles:
        raise RuntimeError(f"no {symbol} candles in {start} → {end}")
    return candles, {
        "source_symbol": PI_SOURCE_SYMBOL[symbol],
        "first_signal": first.isoformat(),
        "last_signal": last.isoformat(),
        "warmup_start": start.isoformat(),
        "replay_end": end.isoformat(),
        "bars": len(candles),
        "first_bar": as_utc(candles[0].timestamp).isoformat(),
        "last_bar": as_utc(candles[-1].timestamp).isoformat(),
    }


def _configure_params(base: Any, variant: ExitVariant, signals: SignalSet, daily_loss_stop: Optional[int] = None) -> Any:
    params = copy.deepcopy(base)
    params.strategy = "pi"
    params.factor_sl_rule = "atr_blend"
    params.factor_tp_rule = "atr_blend"
    params.factor_sl_value = float(variant.long_sl)
    params.factor_tp_value = float(variant.long_sl * variant.rr)
    params.rr_ratio = float(variant.rr)
    params.pi_short_sl_value = float(variant.short_sl)
    params.pi_long_hold_min = int(variant.long_hold)
    params.pi_short_hold_min = int(variant.short_hold)
    params.trail_enabled = False
    params.tr_trail_enabled = False
    params.tr_exit_mode = "tp"
    params.tr_one_trade_per_session = False
    params.tr_allowed_sessions = None
    params.pi_long_only = bool(signals.long_only)
    params.pi_signal_set = "custom"
    params.pi_long_kinds = list(signals.long_kinds)
    params.pi_short_kinds = list(signals.short_kinds)
    if daily_loss_stop is not None:
        params.tr_daily_loss_stop = int(daily_loss_stop)
    return params


def _build_engine(params: Any, variant: ExitVariant) -> BacktestEngine:
    symbol = _extract_symbol(params.contract_id)
    config = BacktestConfig(
        strategies=["trend"],
        initial_capital=INITIAL_CAPITAL,
        symbol=symbol,
        commission_rt=get_commission_rt(params.contract_id),
        fees_rt=get_fees_rt(params.contract_id),
        value_area_pct=float(getattr(params, "value_area_pct", 0.80)),
    )
    engine = BacktestEngine(config=config, strategy_params=params, record_equity=False)
    # The engine constructor creates the production strategy.  Replace only
    # the strategy slot with the research subclass; all engine gates and
    # execution/exit code remain the production implementation.
    engine.trend_follow = GridPiSignalStrategy(params, variant)
    engine._pending_max_age = engine.trend_follow.PENDING_TIMEOUT_CANDLES
    return engine


def _run(params: Any, variant: ExitVariant, candles: list[Candle]) -> list[Any]:
    return _build_engine(params, variant).run(candles).trades


def _fingerprint(trades: Iterable[Any]) -> list[tuple[Any, ...]]:
    out = []
    for trade in sorted(trades, key=lambda item: as_utc(item.entry_time)):
        out.append(
            (
                as_utc(trade.entry_time).isoformat(),
                as_utc(trade.exit_time).isoformat() if trade.exit_time else None,
                round(float(trade.entry_price), 8),
                round(float(trade.exit_price), 8) if trade.exit_price is not None else None,
                round(_finite(trade.pnl), 8),
                getattr(getattr(trade, "exit_reason", None), "value", None),
                _kind(trade),
            )
        )
    return out


def _run_summary(trades: list[Any], symbol: str, *, key: str, label: str, params: Any, variant: ExitVariant) -> dict[str, Any]:
    rows = trade_rows(trades, symbol)
    robustness = _robustness(rows)
    status, reasons = _status(robustness)
    return {
        "key": key,
        "label": label,
        "status": status,
        "status_reasons": reasons,
        "symbol": symbol,
        "exit": {
            "key": variant.key,
            "label": variant.label,
            "long_sl_atr": variant.long_sl,
            "rr": variant.rr,
            "short_sl_atr": variant.short_sl,
            "long_hold_min": variant.long_hold,
            "short_hold_min": variant.short_hold,
            "hard_tp": variant.hard_tp,
        },
        "signal_params": {
            "pi_long_only": bool(getattr(params, "pi_long_only", False)),
            "pi_long_kinds": list(getattr(params, "pi_long_kinds", []) or []),
            "pi_short_kinds": list(getattr(params, "pi_short_kinds", []) or []),
            "tr_daily_loss_stop": int(getattr(params, "tr_daily_loss_stop", 0) or 0),
        },
        "robustness": robustness,
        "monthly": _monthly_stats(rows),
        "by_direction": _group_stats(rows, "direction"),
        "by_kind": _group_stats(rows, "kind"),
        "exit_counts": _exit_counts(rows),
        "trades": rows,
    }


def _strip_trade_rows(result: dict[str, Any]) -> dict[str, Any]:
    out = dict(result)
    out.pop("trades", None)
    return out


def _load_base_params(symbol: str) -> Any:
    preset_doc = json.loads(market_data.repository_data_path("presets.json").read_text(encoding="utf-8"))
    presets = preset_doc.get("presets", {})
    spec = {
        "strategy": "pi",
        "preset": "PI 2MNQ BOTH BEST",
        "overrides": {},
    }
    return _make_params(spec, symbol, presets)


def _reproduction(symbol: str, candles: list[Candle], base: Any) -> dict[str, Any]:
    current_signals = SIGNAL_SETS[0]
    params = _configure_params(base, CURRENT_EXIT, current_signals)
    production_trades = BacktestEngine(
        config=BacktestConfig(
            strategies=["trend"],
            initial_capital=INITIAL_CAPITAL,
            symbol=symbol,
            commission_rt=get_commission_rt(params.contract_id),
            fees_rt=get_fees_rt(params.contract_id),
            value_area_pct=float(getattr(params, "value_area_pct", 0.80)),
        ),
        strategy_params=params,
        record_equity=False,
    ).run(candles).trades
    grid_trades = _run(params, CURRENT_EXIT, candles)
    same = _fingerprint(production_trades) == _fingerprint(grid_trades)
    if not same:
        raise RuntimeError(
            f"current PI reproduction mismatch for {symbol}: "
            f"production={len(production_trades)} grid={len(grid_trades)}"
        )
    rows = trade_rows(production_trades, symbol)
    rob = _robustness(rows)
    return {
        "match": True,
        "trades": len(production_trades),
        "baseline": rob["baseline"],
        "stress_14t": rob["stress_14t"],
        "contract_id": params.contract_id,
        "contract_size": params.contract_size,
        "preset": "PI 2MNQ BOTH BEST",
    }


def _display_line(result: dict[str, Any]) -> str:
    rob = result["robustness"]
    base = rob["baseline"]
    stress = rob["stress_14t"]
    return (
        f"{result['symbol']} {result['label']}: {result['status']} "
        f"n={base['n']} PnL=${base['pnl']:,.0f} PF={base['pf']:.2f} "
        f"14tPF={stress['pf']:.2f}"
    )


def _write_csv(report: dict[str, Any], path: Path) -> None:
    rows = []
    for result in report["exit_results"] + report["signal_results"] + report["gate_results"]:
        rob = result["robustness"]
        rows.append(
            {
                "study": result.get("study"),
                "symbol": result["symbol"],
                "key": result["key"],
                "label": result["label"],
                "status": result["status"],
                "n": rob["baseline"]["n"],
                "pnl": rob["baseline"]["pnl"],
                "pf": rob["baseline"]["pf"],
                "stress_14t_pf": rob["stress_14t"]["pf"],
                "max_dd": rob["baseline"]["max_dd"],
                "wf_pass": rob["walk_forward"]["pass"],
                "last_segment_pf": (rob["walk_forward"].get("segments") or [{}])[-1].get("pf"),
                "mc_pass": rob["monte_carlo_pass"],
            }
        )
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else ["study"])
        writer.writeheader()
        writer.writerows(rows)


def _write_markdown(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# PI exit and signal durability study",
        "",
        f"- Version: `{report['study_version']}`",
        f"- Generated: `{report['generated_at']}`",
        "- Engine: production `BacktestEngine`; no production configuration changed.",
        "- Costs: canonical commission + fees; one contract; 14-tick round-trip stress.",
        "- Important limit: PI history is under 12 months, so no result is called durable.",
        "",
        "## Current preset reproduction",
        "",
    ]
    for symbol, item in report["reproduction"].items():
        base = item["baseline"]
        stress = item["stress_14t"]
        lines.append(
            f"- `{symbol}`: match={item['match']} / n={base['n']} / "
            f"PnL=${base['pnl']:,.0f} / PF={base['pf']:.2f} / 14t PF={stress['pf']:.2f}."
        )
    lines.extend([
        "",
        "## Exit combinations",
        "",
        "| Symbol | Variant | Status | n | PnL | PF | 14t PF | Last WF PF |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ])
    for result in report["exit_results"]:
        rob = result["robustness"]
        segments = rob["walk_forward"].get("segments") or []
        last_pf = segments[-1].get("pf") if segments else None
        lines.append(
            f"| {result['symbol']} | {result['label']} | {result['status']} | "
            f"{rob['baseline']['n']} | ${rob['baseline']['pnl']:,.0f} | "
            f"{rob['baseline']['pf']:.2f} | {rob['stress_14t']['pf']:.2f} | "
            f"{last_pf:.2f} |" if last_pf is not None else
            f"| {result['symbol']} | {result['label']} | {result['status']} | "
            f"{rob['baseline']['n']} | ${rob['baseline']['pnl']:,.0f} | "
            f"{rob['baseline']['pf']:.2f} | {rob['stress_14t']['pf']:.2f} | n/a |"
        )
    lines.extend([
        "",
        "## Signal kinds / sets with current exits",
        "",
        "| Symbol | Set | Status | n | PnL | PF | 14t PF | Last WF PF |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ])
    for result in report["signal_results"]:
        rob = result["robustness"]
        segments = rob["walk_forward"].get("segments") or []
        last_pf = segments[-1].get("pf") if segments else None
        lines.append(
            f"| {result['symbol']} | {result['label']} | {result['status']} | "
            f"{rob['baseline']['n']} | ${rob['baseline']['pnl']:,.0f} | "
            f"{rob['baseline']['pf']:.2f} | {rob['stress_14t']['pf']:.2f} | "
            f"{last_pf:.2f} |" if last_pf is not None else
            f"| {result['symbol']} | {result['label']} | {result['status']} | "
            f"{rob['baseline']['n']} | ${rob['baseline']['pnl']:,.0f} | "
            f"{rob['baseline']['pf']:.2f} | {rob['stress_14t']['pf']:.2f} | n/a |"
        )
    lines.extend([
        "",
        "## Interpretation rules",
        "",
        "- `NO_EDGE`: baseline PF≤1 or 14-tick PF≤1 after costs/stress.",
        "- `INSUFFICIENT_SAMPLE`: positive PFs but fewer than 40 trades; evidence is too thin to rank as durable.",
        "- `REGIME_DEPENDENT`: positive full sample but the equal-time walk-forward or latest segment fails.",
        "- `COVERAGE_LIMITED_CANDIDATE`: passes the mechanical checks but still has less than 12 months of PI history.",
        "- A result that wins only after selecting a variant from this same sample is not an out-of-sample promotion.",
        "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="MNQ,MES", help="comma-separated symbols")
    parser.add_argument("--out", default="", help="external JSON output path")
    parser.add_argument("--skip-signal-sets", action="store_true")
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

    rows = load_rows()
    if not rows:
        raise SystemExit("canonical PI history is empty")

    report: dict[str, Any] = {
        "study_version": STUDY_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(),
        "method": {
            "engine": "production BacktestEngine",
            "costs": "canonical commission_rt + fees_rt",
            "normalization": "one contract",
            "stress_ticks_round_trip": STRESS_TICKS,
            "monte_carlo_iters": MC_ITERS,
            "monte_carlo_seed": MC_SEED,
            "dd_threshold": DD_THRESHOLD,
            "production_changed": False,
            "no_hard_tp_note": "research counterfactual; attached SL remains active and session/time policy closes",
            "current_preset": "PI 2MNQ BOTH BEST",
        },
        "pi_history": {
            "rows_after_shared_loader": len(rows),
            "first": min(parse_ts(row["ts"]) for row in rows).isoformat(),
            "last": max(parse_ts(row["ts"]) for row in rows).isoformat(),
        },
        "data": {},
        "reproduction": {},
        "exit_results": [],
        "signal_results": [],
        "gate_results": [],
    }

    candles_by_symbol: dict[str, list[Candle]] = {}
    base_by_symbol: dict[str, Any] = {}
    for symbol in symbols:
        print(f"Loading {symbol} PI window...", flush=True)
        candles, coverage = _load_run_candles(symbol, rows)
        candles_by_symbol[symbol] = candles
        base = _load_base_params(symbol)
        base_by_symbol[symbol] = base
        report["data"][symbol] = coverage
        print(
            f"  {len(candles):,} candles / {coverage['first_signal']} → {coverage['last_signal']}",
            flush=True,
        )
        started = time.perf_counter()
        report["reproduction"][symbol] = _reproduction(symbol, candles, base)
        print(
            f"  reproduction OK: n={report['reproduction'][symbol]['trades']} "
            f"PF={report['reproduction'][symbol]['baseline']['pf']:.2f} "
            f"14tPF={report['reproduction'][symbol]['stress_14t']['pf']:.2f} "
            f"({time.perf_counter() - started:.1f}s)",
            flush=True,
        )

    exit_total = len(symbols) * len(EXIT_VARIANTS)
    exit_index = 0
    for symbol in symbols:
        base = base_by_symbol[symbol]
        candles = candles_by_symbol[symbol]
        for variant in EXIT_VARIANTS:
            exit_index += 1
            params = _configure_params(base, variant, SIGNAL_SETS[0])
            started = time.perf_counter()
            trades = _run(params, variant, candles)
            result = _run_summary(
                trades,
                symbol,
                key=variant.key,
                label=variant.label,
                params=params,
                variant=variant,
            )
            result["study"] = "exit"
            report["exit_results"].append(result)
            print(f"[{exit_index}/{exit_total}] {_display_line(result)} ({time.perf_counter() - started:.1f}s)", flush=True)

    if not args.skip_signal_sets:
        signal_total = len(symbols) * len(SIGNAL_SETS)
        signal_index = 0
        for symbol in symbols:
            base = base_by_symbol[symbol]
            candles = candles_by_symbol[symbol]
            for signals in SIGNAL_SETS:
                signal_index += 1
                params = _configure_params(base, CURRENT_EXIT, signals)
                started = time.perf_counter()
                trades = _run(params, CURRENT_EXIT, candles)
                result = _run_summary(
                    trades,
                    symbol,
                    key=signals.key,
                    label=signals.label,
                    params=params,
                    variant=CURRENT_EXIT,
                )
                result["study"] = "signal_set"
                report["signal_results"].append(result)
                print(f"[signal {signal_index}/{signal_total}] {_display_line(result)} ({time.perf_counter() - started:.1f}s)", flush=True)

    # Isolate the current daily loss lock.  This is intentionally only one
    # pair: it answers whether the present PF is mainly a gate artifact,
    # without turning every exit/signal cell into a multiple-comparison trap.
    for symbol in symbols:
        base = base_by_symbol[symbol]
        candles = candles_by_symbol[symbol]
        for lock in (1, 0):
            params = _configure_params(base, CURRENT_EXIT, SIGNAL_SETS[0], daily_loss_stop=lock)
            started = time.perf_counter()
            trades = _run(params, CURRENT_EXIT, candles)
            result = _run_summary(
                trades,
                symbol,
                key=f"daily_loss_stop_{lock}",
                label=f"current + daily loss lock {lock}",
                params=params,
                variant=CURRENT_EXIT,
            )
            result["study"] = "gate"
            report["gate_results"].append(result)
            print(f"[gate {symbol}/{lock}] {_display_line(result)} ({time.perf_counter() - started:.1f}s)", flush=True)

    output = Path(args.out).expanduser() if args.out else market_data.derived_path(
        "research", "pi_exit_engine_grid_latest.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    # Trade rows are useful for audit/debugging but live outside the compact
    # report only when explicitly requested in a future extension.  Keeping
    # the default report compact makes it safe to review and diff.
    report["exit_results"] = [_strip_trade_rows(result) for result in report["exit_results"]]
    report["signal_results"] = [_strip_trade_rows(result) for result in report["signal_results"]]
    report["gate_results"] = [_strip_trade_rows(result) for result in report["gate_results"]]
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csv(report, output.with_suffix(".csv"))
    _write_markdown(report, output.with_suffix(".md"))
    print(f"JSON: {output}", flush=True)
    print(f"CSV: {output.with_suffix('.csv')}", flush=True)
    print(f"Report: {output.with_suffix('.md')}", flush=True)


if __name__ == "__main__":
    main()
