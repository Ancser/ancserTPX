"""Long-horizon, research-only audit of the selectable strategy families.

This script is deliberately outside the production execution path.  It replays
the current backtest engine on the canonical 1-minute futures store, then
reports:

* full-sample net results after the repository's commission and fee model;
* calendar-year and descriptive RTH day-regime attribution;
* equal-time walk-forward segments, bootstrap risk, and 14-tick stress;
* a clearly-labelled PI directional time-box overlay for generic entries.

The PI overlay is not a claim that a generic signal has become a PI strategy:
it keeps the generic signal's entry, stop, and target and only replaces its
active-position time policy with PI's asymmetric ``long=OFF / short=60m``
policy.  This lets the audit answer whether PI-style timing exits rescue a
weak entry model without duplicating the production exit kernel.

Large inputs and generated reports stay in the external canonical
``ancserMarketData`` tree.  No data download, preset write, or production
configuration change is performed here.
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
from collections import defaultdict
from datetime import date, datetime, time as clock_time, timedelta
from pathlib import Path
from statistics import median
from typing import Any, Callable, Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.api.routes import BacktestRequest, _build_strategy_params_from_request  # noqa: E402
from backend.backtest.engine import BacktestConfig, BacktestEngine  # noqa: E402
from backend.backtest.robustness import evaluate, series_stats, slip_injection  # noqa: E402
from backend.data import candle_store, market_data  # noqa: E402
from backend.data.option_wall_signals import load_primary_strict_signals  # noqa: E402
from backend.db.models import (  # noqa: E402
    Candle,
    Direction,
    _extract_symbol,
    current_quarterly_contract_id,
    get_commission_rt,
    get_fees_rt,
)
from backend.strategy.exit_policy import resolve_exit_policy  # noqa: E402
from backend.strategy.session_filter import as_new_york, rth_session_date  # noqa: E402
from backend.timebase import UTC, as_utc  # noqa: E402


AUDIT_VERSION = "2026-09-13-long-term-v1"
INITIAL_CAPITAL = 50_000.0
STRESS_TICKS = 14
MC_ITERS = 500
MC_SEED = 20260913
DD_THRESHOLD = 2_000.0


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return as_utc(value).isoformat()
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return datetime.fromtimestamp(float(value), tz=UTC).isoformat()
    return str(value)


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _direction_value(value: Any) -> str:
    raw = getattr(value, "value", value)
    text = str(raw or "").lower()
    if text in {"buy", "long", "1", "direction.buy"}:
        return "long"
    if text in {"sell", "short", "-1", "direction.sell"}:
        return "short"
    return text


def _session_key(entry_time: datetime) -> str:
    return rth_session_date(as_utc(entry_time)).isoformat()


def build_rth_regimes(candles: Iterable[Candle]) -> dict[str, dict[str, Any]]:
    """Build causal volatility labels plus descriptive same-day trend labels.

    Volatility uses only the prior 60 completed RTH ranges.  The trend label
    uses the completed day's open-to-close path and is explicitly attribution,
    not an entry filter.  This avoids presenting a hindsight classification as
    a deployable regime gate.
    """
    daily: dict[str, dict[str, Any]] = {}
    for candle in candles:
        local = as_new_york(candle.timestamp)
        local_time = local.timetz().replace(tzinfo=None)
        if not (clock_time(9, 30) <= local_time < clock_time(16, 0)):
            continue
        key = local.date().isoformat()
        row = daily.setdefault(
            key,
            {
                "date": key,
                "open": float(candle.open),
                "high": float(candle.high),
                "low": float(candle.low),
                "close": float(candle.close),
                "bars": 0,
            },
        )
        row["high"] = max(row["high"], float(candle.high))
        row["low"] = min(row["low"], float(candle.low))
        row["close"] = float(candle.close)
        row["bars"] += 1

    history_ranges: list[float] = []
    out: dict[str, dict[str, Any]] = {}
    for key in sorted(daily):
        row = daily[key]
        day_range = max(0.0, row["high"] - row["low"])
        move = row["close"] - row["open"]
        efficiency = abs(move) / day_range if day_range > 0 else 0.0
        if efficiency >= 0.35 and move > 0:
            trend = "up"
        elif efficiency >= 0.35 and move < 0:
            trend = "down"
        else:
            trend = "range"

        prior = history_ranges[-60:]
        prior_median = median(prior) if len(prior) >= 20 else None
        if prior_median is None or prior_median <= 0:
            volatility = "unknown"
            ratio = None
        else:
            ratio = day_range / prior_median
            if ratio < 0.75:
                volatility = "low"
            elif ratio > 1.25:
                volatility = "high"
            else:
                volatility = "normal"
        out[key] = {
            "date": key,
            "rth_open": round(row["open"], 6),
            "rth_close": round(row["close"], 6),
            "rth_range": round(day_range, 6),
            "rth_move": round(move, 6),
            "trend": trend,
            "volatility": volatility,
            "range_vs_prior_median": round(ratio, 6) if ratio is not None else None,
            "regime": f"{trend}/{volatility}",
            "bars": row["bars"],
        }
        history_ranges.append(day_range)
    return out


def trade_rows(trades: Iterable[Any], regimes: dict[str, dict[str, Any]], symbol: str) -> list[dict[str, Any]]:
    """Project engine trades into JSON-safe rows used by the robustness module."""
    rows: list[dict[str, Any]] = []
    for trade in sorted(
        list(trades), key=lambda item: as_utc(item.entry_time),
    ):
        entry_time = as_utc(trade.entry_time)
        trade_date = _session_key(entry_time)
        regime = regimes.get(trade_date, {})
        rows.append(
            {
                "entry_time": entry_time,
                "exit_time": as_utc(trade.exit_time) if getattr(trade, "exit_time", None) else None,
                "pnl": _finite(getattr(trade, "pnl", 0.0)),
                "direction": _direction_value(getattr(trade, "direction", "")),
                "size": max(1.0, _finite(getattr(trade, "contracts", 1), 1.0)),
                "symbol": symbol,
                "trade_date": trade_date,
                "year": trade_date[:4],
                "quarter": f"{trade_date[:4]}-Q{((int(trade_date[5:7]) - 1) // 3) + 1}",
                "trend_regime": regime.get("trend", "unknown"),
                "volatility_regime": regime.get("volatility", "unknown"),
                "regime": regime.get("regime", "unknown/unknown"),
                "exit_reason": getattr(getattr(trade, "exit_reason", None), "value", None),
            }
        )
    return rows


def compact_stats(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    values = [float(row.get("pnl") or 0.0) for row in rows]
    raw = series_stats(values)
    return {
        "n": int(raw["n"]),
        "pnl": round(float(raw["pnl"]), 2),
        "pf": round(float(raw["pf"]), 4),
        "win_rate": round(float(raw["win"]), 4),
        "max_dd": round(float(raw["max_dd"]), 2),
        "avg_trade": round(float(raw["pnl"]) / len(values), 2) if values else 0.0,
    }


def compact_robustness(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result = evaluate(
        rows,
        iters=MC_ITERS,
        seed=MC_SEED,
        dd_threshold=DD_THRESHOLD,
        slip_levels=(STRESS_TICKS,),
    )
    mc = result.get("monte_carlo") or {}
    wf = result.get("walk_forward") or {}
    stress_levels = (result.get("slip") or {}).get("levels") or []
    stress = stress_levels[0].get("stats", {}) if stress_levels else {}
    return {
        "baseline": compact_stats(rows),
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
        "stress_tick_value": round(
            _finite((result.get("slip") or {}).get("tick_value")), 4
        ),
    }


def grouped_stats(rows: list[dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(field) or "unknown")].append(row)
    return {key: compact_stats(groups[key]) for key in sorted(groups)}


def parameter_snapshot(params: Any) -> dict[str, Any]:
    fields = (
        "strategy", "factor_signal_family", "factor_side_mode",
        "factor_pmo_signal_mode", "factor_timeframe_minutes",
        "factor_sl_rule", "factor_sl_value", "factor_tp_rule", "factor_tp_value",
        "momentum_first_minutes", "momentum_entry_hour",
        "betafib_entry_fib", "betafib_anchor", "betafib_risk_basis",
        "fade_entry_mode", "fade_tp_frac", "tr_sl_ticks",
        "sigma_window_minutes", "sigma_method", "sigma_entry_mode",
        "sigma_accept_mode", "sigma_target_mode", "sigma_stop_span",
        "pi_signal_set", "pi_long_only", "pi_long_hold_min", "pi_short_hold_min",
        "option_wall_submodel", "option_wall_side_mode", "option_wall_max_hold_min",
        "delta_window", "delta_gate", "delta_pattern", "delta_side_mode",
        "tr_allowed_sessions", "tr_daily_loss_stop", "tr_exit_mode",
    )
    out: dict[str, Any] = {}
    for field in fields:
        value = getattr(params, field, None)
        if isinstance(value, tuple):
            value = list(value)
        out[field] = value
    return out


class PiDirectionalExitOverlay:
    """Delegate an entry model while attaching the canonical PI time policy."""

    def __init__(self, inner: Any, params: Any, *, long_hold: int = 0, short_hold: int = 60):
        self.inner = inner
        self.PENDING_TIMEOUT_CANDLES = getattr(inner, "PENDING_TIMEOUT_CANDLES", 1)
        self.params = copy.deepcopy(params)
        self.params.strategy = "pi"
        self.params.pi_long_hold_min = max(0, int(long_hold))
        self.params.pi_short_hold_min = max(0, int(short_hold))
        self.params.trail_enabled = False
        self.params.tr_trail_enabled = False
        self.params.tr_exit_mode = "tp"

    def evaluate(self, candle, zones=None, is_mature=True):
        signal = self.inner.evaluate(candle, zones, is_mature)
        if signal is not None:
            signal.exit_policy = resolve_exit_policy(
                self.params,
                "pi",
                signal.direction,
            )
        return signal

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


def _make_params(spec: dict[str, Any], symbol: str, presets: dict[str, dict[str, Any]]) -> Any:
    payload: dict[str, Any] = {}
    preset_name = spec.get("preset")
    if preset_name:
        payload.update(presets[preset_name])
    payload.update(spec.get("overrides") or {})
    payload["strategy"] = spec["strategy"]
    payload["contract_id"] = current_quarterly_contract_id(symbol)
    payload["contract_size"] = 1
    request = BacktestRequest(
        **{key: value for key, value in payload.items() if key in BacktestRequest.model_fields}
    )
    params = _build_strategy_params_from_request(request, 1)
    params.contract_id = payload["contract_id"]
    params.contract_size = 1
    return params


def model_specs() -> list[dict[str, Any]]:
    """Current named presets plus documented current candidates for no-preset models."""
    return [
        {
            "key": "fade",
            "label": "FADE",
            "strategy": "fade",
            "preset": None,
            "overrides": {
                "fade_entry_mode": "limit",
                "fade_tp_frac": 0.75,
                "tr_sl_ticks": 120,
                "tr_allowed_sessions": ["ASIA", "EURO", "PRE", "RTH", "AH"],
                "tr_one_trade_per_session": False,
                "trail_enabled": False,
                "tr_trail_enabled": False,
            },
            "config_note": "documented FADE candidate: SL120 / TP0.75 / limit / all sessions",
            "pi_overlay": True,
        },
        {
            "key": "sigma",
            "label": "SIGMA",
            "strategy": "sigma",
            "preset": None,
            "overrides": {
                "sigma_window_minutes": 30,
                "sigma_method": "std",
                "sigma_entry_mode": "blind",
                "sigma_accept_mode": "none",
                "sigma_target_mode": "half",
                "sigma_stop_span": 1.0,
                "tr_allowed_sessions": ["RTH"],
                "tr_one_trade_per_session": False,
                "trail_enabled": False,
                "tr_trail_enabled": False,
            },
            "config_note": "documented SIGMA #1 candidate: RTH / rolling 30m / std / blind / half target",
            "pi_overlay": True,
        },
        {
            "key": "factor",
            "label": "FACTOR · BEST",
            "strategy": "factor",
            "preset": "BEST",
            "overrides": {},
            "config_note": "current data/presets.json BEST preset",
            "pi_overlay": True,
        },
        {
            "key": "momentum",
            "label": "MOMENTUM · BEST",
            "strategy": "momentum",
            "preset": "MOMENTUM BEST",
            "overrides": {},
            "config_note": "current data/presets.json MOMENTUM BEST preset",
            "pi_overlay": True,
        },
        {
            "key": "betafib",
            "label": "BETAFIB · BEST",
            "strategy": "betafib",
            "preset": "BETAFIB BEST",
            "overrides": {},
            "config_note": "current data/presets.json BETAFIB BEST preset",
            "pi_overlay": True,
        },
        {
            "key": "pi",
            "label": "PI · BEST",
            "strategy": "pi",
            "preset": "PI 2MNQ BOTH BEST",
            "overrides": {},
            "config_note": "current PI 2MNQ BOTH BEST preset and canonical PI history",
            "pi_overlay": False,
        },
        {
            "key": "optionwall",
            "label": "OPTION WALL",
            "strategy": "optionwall",
            "preset": None,
            "overrides": {
                "option_wall_submodel": "primary_strict",
                "option_wall_side_mode": "all",
                "option_wall_long_sl_atr": 4.0,
                "option_wall_short_sl_atr": 1.5,
                "option_wall_max_hold_min": 60,
                "option_wall_max_trades_per_day": 3,
                "tr_allowed_sessions": ["RTH"],
                "tr_one_trade_per_session": False,
            },
            "config_note": "primary-strict historical option-wall tape; MNQ only",
            "pi_overlay": False,
        },
        {
            "key": "delta_absorption",
            "label": "DELTA ABSORPTION",
            "strategy": "delta_absorption",
            "preset": None,
            "overrides": {
                "delta_window": 5,
                "delta_baseline_window": 30,
                "delta_strength_multiplier": 1.0,
                "delta_weakening_ratio": 0.70,
                "delta_stall_ticks": 1,
                "delta_source": "whole",
                "delta_gate": "location",
                "delta_pattern": "absorption",
                "delta_side_mode": "all",
                "delta_require_profile": True,
                "factor_sl_rule": "atr_blend",
                "factor_tp_rule": "atr_blend",
                "factor_sl_value": 4.0,
                "factor_tp_value": 12.0,
                "rr_ratio": 3,
                "pi_long_only": False,
                "pi_long_hold_min": 0,
                "pi_short_hold_min": 60,
                "tr_allowed_sessions": ["RTH"],
                "tr_one_trade_per_session": False,
            },
            "config_note": "current selected MBO absorption/w5/whole/location candidate",
            "pi_overlay": False,
        },
    ]


def external_coverage(key: str, symbol: str) -> Optional[dict[str, Any]]:
    if key == "pi":
        try:
            from backend.data.pi_history import load_rows
            rows = load_rows()
        except Exception:
            rows = []
        stamps = []
        for row in rows:
            try:
                stamps.append(as_utc(datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00"))))
            except (KeyError, TypeError, ValueError):
                continue
        return _coverage_payload("canonical PI rows", stamps, len(rows))
    if key == "optionwall":
        rows = load_primary_strict_signals()
        return _coverage_payload(
            "primary-strict option-wall signals",
            [row.timestamp for row in rows],
            len(rows),
        )
    if key == "delta_absorption":
        root = market_data.derived_path("orderflow", symbol.lower())
        paths = sorted(root.glob(f"footprint_{symbol.lower()}_*.json.gz"))
        stamps: list[datetime] = []
        for path in paths:
            try:
                stamp = datetime.fromisoformat(path.name[len(f"footprint_{symbol.lower()}_"):len(f"footprint_{symbol.lower()}_") + 10]).replace(tzinfo=as_utc(datetime.now()).tzinfo)
                stamps.append(stamp)
            except ValueError:
                continue
        return _coverage_payload("compact MBO footprint cache", stamps, len(paths))
    return None


def _coverage_payload(label: str, stamps: list[datetime], count: int) -> dict[str, Any]:
    if not stamps:
        return {"label": label, "records": int(count), "first": None, "last": None, "span_months": 0.0}
    first, last = min(stamps), max(stamps)
    return {
        "label": label,
        "records": int(count),
        "first": _iso(first),
        "last": _iso(last),
        "span_months": round((last - first).total_seconds() / 86400 / 30.44, 3),
    }


def model_input_candles(spec: dict[str, Any], candles: list[Candle], symbol: str) -> list[Candle]:
    """Bound tape-driven models to their actual causal input window.

    PI, option-wall, and MBO entries cannot exist outside their source tape.
    Replaying millions of unrelated candles is especially wasteful for the
    MBO provider because it must inspect every eligible footprint cell.  Keep
    seven calendar days before the first source record for ATR/prior-profile
    warm-up and two days after the last record for the final exits.  Generic
    candle-derived strategies continue to use the full history.
    """
    coverage = external_coverage(spec["key"], symbol)
    if not coverage or not coverage.get("first") or not coverage.get("last"):
        return candles
    try:
        first = as_utc(datetime.fromisoformat(str(coverage["first"])))
        last = as_utc(datetime.fromisoformat(str(coverage["last"])))
    except (TypeError, ValueError):
        return candles
    start = first - timedelta(days=7)
    end = last + timedelta(days=2)
    return [candle for candle in candles if start <= as_utc(candle.timestamp) <= end]


def classify_result(
    *,
    key: str,
    robustness: dict[str, Any],
    yearly: dict[str, dict[str, Any]],
    regimes: dict[str, dict[str, Any]],
    coverage: Optional[dict[str, Any]],
) -> tuple[str, list[str]]:
    """Conservative status, intentionally not a live promotion gate."""
    reasons: list[str] = []
    base = robustness["baseline"]
    stress = robustness["stress_14t"]
    if coverage is not None and _finite(coverage.get("span_months")) < 12:
        reasons.append("外部訊號/Order-flow 覆蓋少於 12 個月，無法證明長期生存")
        return "COVERAGE_LIMITED", reasons
    if base["n"] < 40:
        reasons.append(f"只有 {base['n']} 筆交易，長期統計功效不足")
        return "COVERAGE_LIMITED", reasons

    if base["pf"] <= 1.0:
        reasons.append(f"全期 PF {base['pf']:.2f} ≤ 1")
    if stress["pf"] <= 1.0:
        reasons.append(f"14 tick 壓力 PF {stress['pf']:.2f} ≤ 1")
    if not robustness["walk_forward"]["pass"]:
        reasons.append("三段等時間 walk-forward 未全部獲利且 PF>1")
    if not robustness["monte_carlo_pass"]:
        reasons.append("bootstrap 風險門檻未通過")
    if reasons:
        return "FAIL_NO_EDGE", reasons

    populated_years = [row for row in yearly.values() if row["n"] > 0]
    positive_years = [row for row in populated_years if row["pnl"] > 0]
    positive_fraction = len(positive_years) / len(populated_years) if populated_years else 0.0
    total_pnl = base["pnl"]
    dominant_share = max(
        [max(0.0, row["pnl"]) for row in populated_years], default=0.0
    ) / total_pnl if total_pnl > 0 else 1.0
    if positive_fraction < 0.60:
        reasons.append(f"獲利年份 {len(positive_years)}/{len(populated_years)}")
    if dominant_share > 0.65:
        reasons.append(f"單一年份貢獻 {dominant_share:.0%} 全期 PnL")
    populated_regimes = [row for row in regimes.values() if row["n"] >= 10]
    bad_regimes = [row for row in populated_regimes if row["pf"] < 1.0]
    if populated_regimes and len(bad_regimes) / len(populated_regimes) >= 0.5:
        reasons.append(f"{len(bad_regimes)}/{len(populated_regimes)} 個有樣本 regime PF<1")
    return ("REGIME_DEPENDENT", reasons) if reasons else ("LONG_TERM_CANDIDATE", ["目前審計門檻通過；仍需未來資料持續驗證"])


def audit_model(
    spec: dict[str, Any],
    params: Any,
    candles: list[Candle],
    regimes: dict[str, dict[str, Any]],
    symbol: str,
    progress_cb: Optional[Callable[..., None]] = None,
    include_trade_rows: bool = False,
    run_exit_overlay: bool = True,
) -> dict[str, Any]:
    run_candles = model_input_candles(spec, candles, symbol)
    # Prev-day FADE never consumes detector zones; it only consumes the VP
    # levels that the engine calculates at the trade-day rollover.  Supplying
    # an empty, index-aligned timeline skips the detector's per-minute volume
    # profile work while preserving the same candle order and FADE inputs.
    # Other models retain the normal engine path.
    zone_timeline = [{}] * len(run_candles) if spec["strategy"] == "fade" else None
    config = BacktestConfig(
        strategies=["trend"],
        initial_capital=INITIAL_CAPITAL,
        symbol=_extract_symbol(params.contract_id),
        commission_rt=get_commission_rt(params.contract_id),
        fees_rt=get_fees_rt(params.contract_id),
        value_area_pct=float(getattr(params, "value_area_pct", 0.80)),
    )

    started = time.perf_counter()
    engine = BacktestEngine(
        config=config,
        strategy_params=params,
        zone_timeline=zone_timeline,
        record_equity=False,
    )
    result = engine.run(run_candles, progress_cb=progress_cb)
    baseline_rows = trade_rows(result.trades, regimes, symbol)
    baseline_robustness = compact_robustness(baseline_rows)

    overlay_rows: Optional[list[dict[str, Any]]] = None
    overlay_robustness: Optional[dict[str, Any]] = None
    overlay_comparison: Optional[dict[str, Any]] = None
    if spec.get("pi_overlay") and run_exit_overlay:
        overlay_engine = BacktestEngine(
            config=config,
            strategy_params=params,
            zone_timeline=zone_timeline,
            record_equity=False,
        )
        overlay = PiDirectionalExitOverlay(overlay_engine.trend_follow, params)
        overlay_engine.trend_follow = overlay
        overlay_engine._pending_max_age = overlay.PENDING_TIMEOUT_CANDLES
        overlay_result = overlay_engine.run(run_candles, progress_cb=progress_cb)
        overlay_rows = trade_rows(overlay_result.trades, regimes, symbol)
        overlay_robustness = compact_robustness(overlay_rows)
        overlay_comparison = {
            "description": "same entries/SL/TP; PI directional max-hold overlay (long OFF, short 60m)",
            "baseline_pnl": baseline_robustness["baseline"]["pnl"],
            "overlay_pnl": overlay_robustness["baseline"]["pnl"],
            "delta_pnl": round(
                overlay_robustness["baseline"]["pnl"] - baseline_robustness["baseline"]["pnl"],
                2,
            ),
            "baseline_stress_pf": baseline_robustness["stress_14t"]["pf"],
            "overlay_stress_pf": overlay_robustness["stress_14t"]["pf"],
            "delta_stress_pf": round(
                overlay_robustness["stress_14t"]["pf"] - baseline_robustness["stress_14t"]["pf"],
                4,
            ),
            "same_trade_count": len(baseline_rows) == len(overlay_rows),
            "rescued": bool(
                overlay_robustness["baseline"]["pf"] > 1.0
                and overlay_robustness["stress_14t"]["pf"] > 1.0
                and overlay_robustness["walk_forward"]["pass"]
            ),
        }

    yearly = grouped_stats(baseline_rows, "year")
    by_regime = grouped_stats(baseline_rows, "regime")
    status, status_reasons = classify_result(
        key=spec["key"],
        robustness=baseline_robustness,
        yearly=yearly,
        regimes=by_regime,
        coverage=external_coverage(spec["key"], symbol),
    )
    elapsed = time.perf_counter() - started
    output = {
        "key": spec["key"],
        "label": spec["label"],
        "strategy": spec["strategy"],
        "config_note": spec["config_note"],
        "parameters": parameter_snapshot(params),
        "data_span": {
            "bars": len(candles),
            "first": _iso(candles[0].timestamp) if candles else None,
            "last": _iso(candles[-1].timestamp) if candles else None,
            "rth_days": len(regimes),
        },
        "engine_input_span": {
            "bars": len(run_candles),
            "first": _iso(run_candles[0].timestamp) if run_candles else None,
            "last": _iso(run_candles[-1].timestamp) if run_candles else None,
        },
        "external_coverage": external_coverage(spec["key"], symbol),
        "baseline": baseline_robustness,
        "yearly": yearly,
        "quarterly": grouped_stats(baseline_rows, "quarter"),
        "by_direction": grouped_stats(baseline_rows, "direction"),
        "by_regime": by_regime,
        "exit_overlay": {
            "robustness": overlay_robustness,
            "comparison": overlay_comparison,
        },
        "status": status,
        "status_reasons": status_reasons,
        "elapsed_seconds": round(elapsed, 2),
    }
    # Resumable downstream research may need the exact event rows.  Keep the
    # default audit artifact compact and unchanged; callers must opt in.
    if include_trade_rows:
        output["trade_rows"] = baseline_rows
    return output


def _status_line(result: dict[str, Any]) -> str:
    base = result["baseline"]["baseline"]
    stress = result["baseline"]["stress_14t"]
    return (
        f"{result['label']}: {result['status']} | n={base['n']} "
        f"PnL=${base['pnl']:,.0f} PF={base['pf']:.2f} "
        f"14tPF={stress['pf']:.2f} | {result['elapsed_seconds']:.1f}s"
    )


def write_outputs(report: dict[str, Any], out_json: Path) -> tuple[Path, Path]:
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md = out_json.with_suffix(".md")
    out_csv = out_json.with_suffix(".csv")
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=_iso), encoding="utf-8")

    rows = []
    for result in report["results"]:
        base = result["baseline"]["baseline"]
        stress = result["baseline"]["stress_14t"]
        overlay = result["exit_overlay"].get("comparison") or {}
        rows.append(
            {
                "label": result["label"],
                "status": result["status"],
                "n": base["n"],
                "pnl": base["pnl"],
                "pf": base["pf"],
                "stress_14t_pf": stress["pf"],
                "max_dd": base["max_dd"],
                "mc_pass": result["baseline"]["monte_carlo_pass"],
                "wf_pass": result["baseline"]["walk_forward"]["pass"],
                "pi_overlay_delta_pnl": overlay.get("delta_pnl"),
                "pi_overlay_stress_pf": overlay.get("overlay_stress_pf"),
            }
        )
    with out_csv.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else ["label"])
        writer.writeheader()
        writer.writerows(rows)

    lines = [
        "# Strategy long-term audit",
        "",
        f"- Version: `{report['audit_version']}`",
        f"- Symbol: `{report['symbol']}`; candles: `{report['data']['bars']:,}`",
        f"- Candle span: `{report['data']['first']}` → `{report['data']['last']}`",
        "- Costs: canonical commission + fees; all results normalized to one contract.",
        "- 14t stress: round-trip slippage charged by the shared robustness module.",
        "- Regime labels: RTH trend is descriptive; volatility compares today's range with the prior 60 completed RTH ranges.",
        "- PI overlay: generic entry/SL/TP unchanged; only long OFF / short 60m max-hold is replaced through the shared exit policy.",
        "",
        "## Summary",
        "",
        "| Model | Status | n | PnL | PF | 14t PF | MC | WF |",
        "|---|---|---:|---:|---:|---:|---|---|",
    ]
    for result in report["results"]:
        base = result["baseline"]["baseline"]
        stress = result["baseline"]["stress_14t"]
        lines.append(
            f"| {result['label']} | **{result['status']}** | {base['n']} | ${base['pnl']:,.0f} | "
            f"{base['pf']:.2f} | {stress['pf']:.2f} | "
            f"{'PASS' if result['baseline']['monte_carlo_pass'] else 'FAIL'} | "
            f"{'PASS' if result['baseline']['walk_forward']['pass'] else 'FAIL'} |"
        )
    lines.extend(["", "## Findings", ""])
    for result in report["results"]:
        lines.append(f"### {result['label']}")
        lines.append("")
        lines.append(f"- Status: **{result['status']}**")
        lines.append(f"- Config: {result['config_note']}")
        for reason in result["status_reasons"]:
            lines.append(f"- {reason}")
        coverage = result.get("external_coverage")
        if coverage:
            lines.append(
                f"- External coverage: {coverage['label']} / {coverage['records']} records / "
                f"{coverage['first']} → {coverage['last']} ({coverage['span_months']:.1f} months)."
            )
        comparison = result["exit_overlay"].get("comparison")
        if comparison:
            lines.append(
                f"- PI time-box overlay: ΔPnL ${comparison['delta_pnl']:,.0f}; "
                f"14t PF {comparison['baseline_stress_pf']:.2f} → {comparison['overlay_stress_pf']:.2f}; "
                f"rescued={comparison['rescued']}."
            )
        lines.append("")
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_json, out_md


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="MNQ", choices=("MNQ", "MES"))
    parser.add_argument(
        "--models",
        default="all",
        help="comma-separated model keys; default all",
    )
    parser.add_argument("--out", default="", help="external JSON output path")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.disable(logging.CRITICAL)
    symbol = args.symbol.upper()
    selected = {piece.strip().lower() for piece in str(args.models).split(",") if piece.strip()}
    specs = [spec for spec in model_specs() if selected == {"all"} or spec["key"] in selected]
    if not specs:
        raise SystemExit("no matching model keys")

    print(f"Loading canonical {symbol} 1m candles...", flush=True)
    candles = sorted(candle_store.load(symbol, 1), key=lambda item: as_utc(item.timestamp))
    if not candles:
        raise SystemExit(f"no {symbol} candles in canonical store")
    regimes = build_rth_regimes(candles)
    preset_doc = json.loads(market_data.repository_data_path("presets.json").read_text(encoding="utf-8"))
    presets = preset_doc.get("presets", {})
    print(
        f"Loaded {len(candles):,} bars / {len(regimes):,} RTH days / "
        f"{_iso(candles[0].timestamp)} → {_iso(candles[-1].timestamp)}",
        flush=True,
    )

    results: list[dict[str, Any]] = []
    for index, spec in enumerate(specs, 1):
        print(f"[{index}/{len(specs)}] Running {spec['label']}...", flush=True)
        params = _make_params(spec, symbol, presets)
        result = audit_model(spec, params, candles, regimes, symbol)
        results.append(result)
        print(_status_line(result), flush=True)

    report = {
        "audit_version": AUDIT_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(),
        "symbol": symbol,
        "data": {
            "bars": len(candles),
            "first": _iso(candles[0].timestamp),
            "last": _iso(candles[-1].timestamp),
            "rth_days": len(regimes),
            "regime_definition": "descriptive RTH trend; causal prior-60-day range volatility",
        },
        "method": {
            "costs": "canonical commission_rt + fees_rt",
            "normalization": "one contract",
            "stress_ticks_round_trip": STRESS_TICKS,
            "monte_carlo_iters": MC_ITERS,
            "monte_carlo_seed": MC_SEED,
            "dd_threshold": DD_THRESHOLD,
            "production_changed": False,
            "pi_overlay_scope": "generic entries only; long hold OFF / short hold 60m",
        },
        "results": results,
    }
    output = Path(args.out).expanduser() if args.out else market_data.derived_path(
        "research", f"strategy_long_term_audit_{symbol.lower()}_current.json"
    )
    json_path, md_path = write_outputs(report, output)
    print(f"JSON: {json_path}", flush=True)
    print(f"Report: {md_path}", flush=True)


if __name__ == "__main__":
    main()
