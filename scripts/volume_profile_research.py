"""Five-year research audit for the prior-RTH Volume Profile model.

This is a research-only runner.  It uses the production ``BacktestEngine``
and the canonical continuous 1-minute store, but it never writes a preset or
changes live configuration.  The selection window is five completed calendar
years (2021-01-01 through 2025-12-31); 2026 YTD is reported only as a holdout
for the selected training candidate.

The candidate matrix is intentionally small and interpretable.  It compares
the four state-machine modes, ATR versus structural targets, confirmation
bars, and a few ATR risk pairs.  The ranking is based on training-only
stability (minimum walk-forward PnL, 14-tick stress, and a modest trade-count
floor), not on the highest single PF.  This keeps a one-off spike from being
called ``BEST``.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
import time
from collections import defaultdict
from datetime import date, datetime, time as clock_time, timedelta
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.api.routes import BacktestRequest, _build_strategy_params_from_request  # noqa: E402
from backend.backtest.engine import BacktestConfig, BacktestEngine  # noqa: E402
from backend.backtest.robustness import evaluate, segment_index, series_stats, slip_injection, walk_forward  # noqa: E402
from backend.data import candle_store, market_data  # noqa: E402
from backend.db.models import Candle, Direction, current_quarterly_contract_id, get_commission_rt, get_fees_rt  # noqa: E402
from backend.strategy.session_filter import as_new_york, rth_session_date  # noqa: E402
from backend.timebase import UTC, as_utc  # noqa: E402


VERSION = "2026-09-15-volume-profile-v1"
TRAIN_START = date(2021, 1, 1)
TRAIN_END = date(2025, 12, 31)
HOLDOUT_START = date(2026, 1, 1)
STRESS_TICKS = 14
MC_ITERS = 500
MC_SEED = 20260915
INITIAL_CAPITAL = 50_000.0


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return as_utc(value).isoformat()
    return str(value)


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _date_key(value: datetime) -> str:
    return as_utc(value).date().isoformat()


def _direction(value: Any) -> str:
    raw = getattr(value, "value", value)
    text = str(raw or "").lower()
    if text in {"buy", "long", "1", "direction.buy"}:
        return "long"
    if text in {"sell", "short", "-1", "direction.sell"}:
        return "short"
    return text


def _trade_rows(trades: Iterable[Any], start: date, end: date) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for trade in sorted(trades, key=lambda item: as_utc(item.entry_time)):
        entry_time = as_utc(trade.entry_time)
        entry_date = entry_time.date()
        if entry_date < start or entry_date > end:
            continue
        rows.append(
            {
                "entry_time": entry_time,
                "exit_time": as_utc(trade.exit_time) if trade.exit_time else None,
                "pnl": _finite(trade.pnl),
                "direction": _direction(trade.direction),
                "size": max(1.0, _finite(getattr(trade, "contracts", 1), 1.0)),
                "trade_date": _date_key(entry_time),
                "year": str(entry_time.year),
                "regime": "unknown",
                "setup": str((getattr(trade, "meta", {}) or {}).get("setup") or "unknown"),
                "edge": str((getattr(trade, "meta", {}) or {}).get("edge") or "unknown"),
            }
        )
    return rows


def _stats(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
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


def _robustness(rows: list[dict[str, Any]], symbol: str) -> dict[str, Any]:
    result = evaluate(
        rows,
        iters=MC_ITERS,
        seed=MC_SEED,
        dd_threshold=2_000.0,
        slip_levels=(STRESS_TICKS,),
    )
    wf = walk_forward(rows, segments=3)
    stress_levels = (result.get("slip") or {}).get("levels") or []
    stress = stress_levels[0].get("stats", {}) if stress_levels else {}
    mc = result.get("monte_carlo") or {}
    wf_segments = (wf or {}).get("segments") or []
    return {
        "baseline": _stats(rows),
        "stress_14t": {
            "n": int(stress.get("n", 0)),
            "pnl": round(_finite(stress.get("pnl")), 2),
            "pf": round(_finite(stress.get("pf")), 4),
            "win_rate": round(_finite(stress.get("win")), 4),
            "max_dd": round(_finite(stress.get("max_dd")), 2),
        },
        "walk_forward": {
            "pass": bool((wf or {}).get("pass")),
            "segments": [
                {
                    "n": int(item.get("n", 0)),
                    "pnl": round(_finite(item.get("pnl")), 2),
                    "pf": round(_finite(item.get("pf")), 4),
                }
                for item in wf_segments
            ],
        },
        "monte_carlo": {
            "iters": mc.get("iters"),
            "seed": mc.get("seed"),
            "p_loss": round(_finite(mc.get("p_loss")), 4) if mc else None,
            "pf_p5": round(_finite(mc.get("pf_p5")), 4) if mc else None,
            "dd_p95": round(_finite(mc.get("dd_p95")), 2) if mc else None,
        },
        "monte_carlo_pass": bool(result.get("monte_carlo_pass")),
        "slip_tick_value": round(_finite((result.get("slip") or {}).get("tick_value")), 4),
    }


def _rth_regimes(candles: Iterable[Candle]) -> dict[str, dict[str, Any]]:
    """Build descriptive RTH trend/volatility labels for attribution only."""
    daily: dict[str, dict[str, Any]] = {}
    for candle in candles:
        local = as_new_york(candle.timestamp)
        local_time = local.timetz().replace(tzinfo=None)
        if not (clock_time(9, 30) <= local_time < clock_time(16, 0)):
            continue
        key = local.date().isoformat()
        row = daily.setdefault(
            key,
            {"open": float(candle.open), "high": float(candle.high), "low": float(candle.low), "close": float(candle.close)},
        )
        row["high"] = max(row["high"], float(candle.high))
        row["low"] = min(row["low"], float(candle.low))
        row["close"] = float(candle.close)

    history: list[float] = []
    out: dict[str, dict[str, Any]] = {}
    for key in sorted(daily):
        row = daily[key]
        day_range = max(0.0, row["high"] - row["low"])
        move = row["close"] - row["open"]
        efficiency = abs(move) / day_range if day_range > 0 else 0.0
        trend = "up" if efficiency >= 0.35 and move > 0 else "down" if efficiency >= 0.35 and move < 0 else "range"
        prior = history[-60:]
        prior_median = sorted(prior)[len(prior) // 2] if len(prior) >= 20 else None
        ratio = day_range / prior_median if prior_median and prior_median > 0 else None
        volatility = "unknown" if ratio is None else "low" if ratio < 0.75 else "high" if ratio > 1.25 else "normal"
        out[key] = {"trend": trend, "volatility": volatility, "regime": f"{trend}/{volatility}"}
        history.append(day_range)
    return out


def _attach_regimes(rows: list[dict[str, Any]], regimes: dict[str, dict[str, Any]]) -> None:
    for row in rows:
        row["regime"] = regimes.get(row["trade_date"], {}).get("regime", "unknown")


def _grouped(rows: list[dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(field) or "unknown")].append(row)
    return {key: _stats(groups[key]) for key in sorted(groups)}


def candidates() -> list[dict[str, Any]]:
    """Representative matrix; no per-year or holdout tuning is performed.

    Twelve runs are enough to compare the structural choices and to see
    whether the ATR pair has a local plateau, while avoiding a misleading
    32-way micro-grid on a five-year 1-minute tape.
    """
    specs = (
        ("auto", "atr", 1.25, 1.5, 1),
        ("auto", "atr", 1.5, 2.0, 2),
        ("auto", "poc", 1.5, 2.0, 2),
        ("range", "atr", 1.25, 1.5, 2),
        ("range", "poc", 1.5, 2.0, 2),
        ("breakout", "atr", 1.25, 1.5, 1),
        ("breakout", "atr", 1.5, 2.0, 2),
        ("breakout", "atr", 1.5, 2.0, 3),
        ("breakout", "opposite_edge", 1.5, 2.0, 2),
        ("failed_break", "poc", 1.25, 1.5, 2),
        ("failed_break", "poc", 1.5, 2.0, 2),
        ("failed_break", "atr", 1.5, 2.0, 2),
    )
    return [
        {
            "entry_mode": mode,
            "target_mode": target,
            "side_mode": "all",
            "sl_atr": sl_atr,
            "tp_atr": tp_atr,
            "confirm_bars": confirm,
            "breakout_buffer_ticks": 2,
            "touch_tolerance_ticks": 2,
            "reclaim_buffer_ticks": 1,
            "max_trades_per_day": 2,
            "min_source_candles": 60,
            "value_area_pct": 0.70,
        }
        for mode, target, sl_atr, tp_atr, confirm in specs
    ]


def _params(symbol: str, values: dict[str, Any]):
    contract_id = current_quarterly_contract_id(symbol)
    request = BacktestRequest(
        strategy="volume_profile",
        contract_id=contract_id,
        contract_size=1,
        tr_allowed_sessions=["RTH"],
        tr_one_trade_per_session=False,
        one_trade_per_session_direction=False,
        trail_enabled=False,
        tr_trail_enabled=False,
        tr_exit_mode="tp",
        vp_value_area_pct=values["value_area_pct"],
        vp_entry_mode=values["entry_mode"],
        vp_target_mode=values["target_mode"],
        vp_side_mode=values["side_mode"],
        vp_sl_atr=values["sl_atr"],
        vp_tp_atr=values["tp_atr"],
        vp_confirm_bars=values["confirm_bars"],
        vp_breakout_buffer_ticks=values["breakout_buffer_ticks"],
        vp_touch_tolerance_ticks=values["touch_tolerance_ticks"],
        vp_reclaim_buffer_ticks=values["reclaim_buffer_ticks"],
        vp_max_trades_per_day=values["max_trades_per_day"],
        vp_min_source_candles=values["min_source_candles"],
    )
    params = _build_strategy_params_from_request(request, 1)
    params.contract_id = contract_id
    params.contract_size = 1
    return params


def _run_candidate(
    values: dict[str, Any],
    candles: list[Candle],
    regimes: dict[str, dict[str, Any]],
    symbol: str,
    start: date,
    end: date,
) -> dict[str, Any]:
    params = _params(symbol, values)
    config = BacktestConfig(
        strategies=["trend"],
        symbol=symbol,
        interval="1m",
        initial_capital=INITIAL_CAPITAL,
        commission_rt=get_commission_rt(params.contract_id),
        fees_rt=get_fees_rt(params.contract_id),
        value_area_pct=0.80,
    )
    started = time.perf_counter()
    result = BacktestEngine(config=config, strategy_params=params, record_equity=False).run(candles)
    rows = _trade_rows(result.trades, start, end)
    _attach_regimes(rows, regimes)
    robust = _robustness(rows, symbol)
    wf_segments = robust["walk_forward"]["segments"]
    min_wf_pnl = min((float(item["pnl"]) for item in wf_segments), default=0.0)
    stress_pf = float(robust["stress_14t"]["pf"])
    base = robust["baseline"]
    # Ranking metric: stable floor first, then PF/PnL.  This is not a
    # promotion gate; it only gives a deterministic training candidate.
    eligible = base["n"] >= 40 and stress_pf > 1.0 and min_wf_pnl > 0
    score = (1 if eligible else 0, min_wf_pnl, stress_pf, base["pf"], base["pnl"])
    return {
        "parameters": dict(values),
        "baseline": robust,
        "by_year": _grouped(rows, "year"),
        "by_regime": _grouped(rows, "regime"),
        "by_setup": _grouped(rows, "setup"),
        "by_edge": _grouped(rows, "edge"),
        "training_eligible": eligible,
        "ranking_score": list(score),
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "trade_rows": rows,
    }


def _holdout(
    values: dict[str, Any],
    candles: list[Candle],
    regimes: dict[str, dict[str, Any]],
    symbol: str,
    start: date,
    end: date,
) -> dict[str, Any]:
    params = _params(symbol, values)
    config = BacktestConfig(
        strategies=["trend"],
        symbol=symbol,
        interval="1m",
        initial_capital=INITIAL_CAPITAL,
        commission_rt=get_commission_rt(params.contract_id),
        fees_rt=get_fees_rt(params.contract_id),
        value_area_pct=0.80,
    )
    result = BacktestEngine(config=config, strategy_params=params, record_equity=False).run(candles)
    rows = _trade_rows(result.trades, start, end)
    _attach_regimes(rows, regimes)
    return {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "baseline": _robustness(rows, symbol),
        "by_year": _grouped(rows, "year"),
        "by_regime": _grouped(rows, "regime"),
        "trade_rows": rows,
    }


def _format_line(index: int, total: int, item: dict[str, Any]) -> str:
    p = item["parameters"]
    base = item["baseline"]["baseline"]
    stress = item["baseline"]["stress_14t"]
    return (
        f"[{index}/{total}] {p['entry_mode']}/{p['target_mode']} "
        f"SL{p['sl_atr']:g}/TP{p['tp_atr']:g}/C{p['confirm_bars']} "
        f"n={base['n']} PF={base['pf']:.2f} 14t={stress['pf']:.2f} "
        f"PnL=${base['pnl']:,.0f}"
    )


def _select_training_candidate(results: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Select a non-empty stable candidate; never select a zero-trade row."""
    eligible = [item for item in results if item.get("training_eligible")]
    if eligible:
        return max(eligible, key=lambda item: tuple(item["ranking_score"]))
    nonempty = [
        item for item in results
        if int((item.get("baseline") or {}).get("baseline", {}).get("n", 0)) > 0
    ]
    if not nonempty:
        return None
    return max(nonempty, key=lambda item: tuple(item["ranking_score"]))


def _write(report: dict[str, Any], path: Path) -> tuple[Path, Path, Path]:
    path.parent.mkdir(parents=True, exist_ok=True)
    compact = json.loads(json.dumps(report, default=_iso))
    path.write_text(json.dumps(compact, ensure_ascii=False, indent=2), encoding="utf-8")
    csv_path = path.with_suffix(".csv")
    rows = []
    for index, item in enumerate(report["candidates"], 1):
        p = item["parameters"]
        b = item["baseline"]["baseline"]
        s = item["baseline"]["stress_14t"]
        wf = item["baseline"]["walk_forward"]
        rows.append(
            {
                "rank": index,
                "entry_mode": p["entry_mode"],
                "target_mode": p["target_mode"],
                "sl_atr": p["sl_atr"],
                "tp_atr": p["tp_atr"],
                "confirm_bars": p["confirm_bars"],
                "trades": b["n"],
                "pnl": b["pnl"],
                "pf": b["pf"],
                "stress_14t_pf": s["pf"],
                "max_dd": b["max_dd"],
                "wf_pass": wf["pass"],
                "training_eligible": item["training_eligible"],
            }
        )
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else ["rank"])
        writer.writeheader()
        writer.writerows(rows)

    md_path = path.with_suffix(".md")
    selected = report.get("selected_training_candidate") or {}
    lines = [
        "# Volume Profile five-year research",
        "",
        f"- Version: `{report['version']}`",
        f"- Symbol: `{report['symbol']}`",
        f"- Training: `{report['training_window']['start']}` → `{report['training_window']['end']}`",
        f"- Holdout: `{report['holdout_window']['start']}` → `{report['holdout_window']['end']}`",
        "- Engine: production `BacktestEngine`, one contract, canonical commission + fees.",
        "- Stress: 14 ticks round-trip via the shared robustness module.",
        "- Regime labels are descriptive RTH attribution; they are not hindsight filters in the production model.",
        "",
        "## Training ranking",
        "",
        "| Rank | Mode | Target | SL | TP | Confirm | n | PnL | PF | 14t PF | WF | Eligible |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for index, item in enumerate(report["candidates"], 1):
        p = item["parameters"]
        b = item["baseline"]["baseline"]
        s = item["baseline"]["stress_14t"]
        lines.append(
            f"| {index} | {p['entry_mode']} | {p['target_mode']} | {p['sl_atr']:g} | {p['tp_atr']:g} | "
            f"{p['confirm_bars']} | {b['n']} | ${b['pnl']:,.0f} | {b['pf']:.2f} | {s['pf']:.2f} | "
            f"{'PASS' if item['baseline']['walk_forward']['pass'] else 'FAIL'} | "
            f"{'YES' if item['training_eligible'] else 'NO'} |"
        )
    lines.extend(["", "## Selected training candidate", ""])
    if selected:
        p = selected["parameters"]
        b = selected["baseline"]["baseline"]
        h = report.get("holdout") or {}
        hb = (h.get("baseline") or {}).get("baseline") or {}
        lines.extend(
            [
                f"- Parameters: `{json.dumps(p, sort_keys=True)}`",
                f"- Training: n={b['n']} / PnL=${b['pnl']:,.2f} / PF={b['pf']:.4f} / 14t PF={selected['baseline']['stress_14t']['pf']:.4f}.",
                f"- 2026 holdout: n={hb.get('n', 0)} / PnL=${hb.get('pnl', 0):,.2f} / PF={hb.get('pf', 0):.4f} / 14t PF={(h.get('baseline') or {}).get('stress_14t', {}).get('pf', 0):.4f}.",
                "- A selectable preset should remain research-labelled unless the holdout and future rolling evidence support promotion.",
            ]
        )
    else:
        lines.append("No training candidate met the stability floor; keep the model research-only.")
    lines.extend(["", "## Interpretation", "", "- The ranking is not a claim of live profitability.", "- Candidate selection never used 2026 holdout results.", "- Per-regime tables are attribution, not a deployable regime oracle."])
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path, md_path, csv_path


def _load_window(symbol: str, start: date, end: date) -> tuple[list[Candle], dict[str, dict[str, Any]]]:
    snapshot = candle_store.load_snapshot(symbol, 1)
    if not snapshot.bars:
        raise SystemExit(f"no {symbol} candles in canonical store")
    run_start = datetime.combine(start - timedelta(days=45), clock_time.min, tzinfo=UTC)
    run_end = datetime.combine(end + timedelta(days=2), clock_time.max, tzinfo=UTC)
    candles = candle_store.select_range(snapshot, run_start, run_end)
    if not candles:
        raise SystemExit(f"no {symbol} candles in requested range")
    return candles, _rth_regimes(candles)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="MNQ", choices=("MNQ", "MES"))
    parser.add_argument("--out", default="", help="external JSON output path")
    parser.add_argument(
        "--reuse-training",
        default="",
        help="reuse a prior JSON training matrix and only recompute the holdout",
    )
    parser.add_argument(
        "--validate-from",
        default="",
        help="validate the selected parameters from another report on this symbol",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.disable(logging.CRITICAL)
    symbol = args.symbol.upper()
    if args.validate_from:
        source_path = Path(args.validate_from).expanduser()
        source = json.loads(source_path.read_text(encoding="utf-8"))
        selected = source.get("selected_training_candidate") or {}
        values = selected.get("parameters") or {}
        if not values:
            raise SystemExit("source report has no selected candidate")
        training_candles, training_regimes = _load_window(symbol, TRAIN_START, TRAIN_END)
        holdout_candles, holdout_regimes = _load_window(symbol, HOLDOUT_START, date.today())
        train_result = _run_candidate(values, training_candles, training_regimes, symbol, TRAIN_START, TRAIN_END)
        holdout = _holdout(values, holdout_candles, holdout_regimes, symbol, HOLDOUT_START, date.today())
        train_result.pop("trade_rows", None)
        report = {
            "version": VERSION + "-cross-symbol",
            "generated_at": datetime.now().astimezone().isoformat(),
            "symbol": symbol,
            "training_window": {"start": TRAIN_START.isoformat(), "end": TRAIN_END.isoformat()},
            "holdout_window": {"start": HOLDOUT_START.isoformat(), "end": date.today().isoformat()},
            "source": {
                "store": str(candle_store.load_snapshot(symbol, 1).source_path),
                "meta": candle_store.load_meta(symbol, 1),
                "training_bars": len(training_candles),
                "holdout_bars": len(holdout_candles),
                "selection_source": str(source_path),
            },
            "method": {
                "engine": "backend.backtest.engine.BacktestEngine",
                "costs": "canonical commission_rt + fees_rt",
                "contract_size": 1,
                "stress_ticks_round_trip": STRESS_TICKS,
                "monte_carlo_iters": MC_ITERS,
                "monte_carlo_seed": MC_SEED,
                "selection": "parameters selected on the source report; no MES tuning",
                "segment_index_source": "backend.backtest.robustness.segment_index",
                "production_changed": False,
            },
            "selected_training_candidate": train_result,
            "holdout": holdout,
            "candidates": [train_result],
        }
        output = Path(args.out).expanduser() if args.out else market_data.derived_path(
            "research", f"volume_profile_validation_{symbol.lower()}_from_mnq.json"
        )
        json_path, md_path, csv_path = _write(report, output)
        print(f"Validated source candidate on {symbol}: {values}", flush=True)
        print(f"JSON: {json_path}", flush=True)
        print(f"Report: {md_path}", flush=True)
        print(f"CSV: {csv_path}", flush=True)
        return 0
    if args.reuse_training:
        source_path = Path(args.reuse_training).expanduser()
        report = json.loads(source_path.read_text(encoding="utf-8"))
        results = list(report.get("candidates") or [])
        selected = _select_training_candidate(results)
        if selected is None:
            raise SystemExit("prior training report has no non-empty candidate")
        holdout_candles, holdout_regimes = _load_window(symbol, HOLDOUT_START, date.today())
        report["generated_at"] = datetime.now().astimezone().isoformat()
        report["selected_training_candidate"] = selected
        report["holdout"] = _holdout(
            selected["parameters"], holdout_candles, holdout_regimes,
            symbol, HOLDOUT_START, date.today(),
        )
        output = Path(args.out).expanduser() if args.out else source_path
        json_path, md_path, csv_path = _write(report, output)
        print(f"Reused training matrix; selected: {selected['parameters']}", flush=True)
        print(f"JSON: {json_path}", flush=True)
        print(f"Report: {md_path}", flush=True)
        print(f"CSV: {csv_path}", flush=True)
        return 0
    training_candles, training_regimes = _load_window(symbol, TRAIN_START, TRAIN_END)
    holdout_candles, holdout_regimes = _load_window(symbol, HOLDOUT_START, date.today())
    print(
        f"Loaded {symbol}: train {len(training_candles):,} bars "
        f"({_iso(training_candles[0].timestamp)} -> {_iso(training_candles[-1].timestamp)}); "
        f"holdout {len(holdout_candles):,} bars "
        f"({_iso(holdout_candles[0].timestamp)} -> {_iso(holdout_candles[-1].timestamp)})",
        flush=True,
    )
    grid = candidates()
    results: list[dict[str, Any]] = []
    for index, values in enumerate(grid, 1):
        item = _run_candidate(values, training_candles, training_regimes, symbol, TRAIN_START, TRAIN_END)
        item.pop("trade_rows", None)
        results.append(item)
        print(_format_line(index, len(grid), item), flush=True)

    results.sort(key=lambda item: tuple(item["ranking_score"]), reverse=True)
    selected = _select_training_candidate(results)
    holdout = _holdout(
        selected["parameters"], holdout_candles, holdout_regimes, symbol, HOLDOUT_START, date.today()
    ) if selected else None
    report = {
        "version": VERSION,
        "generated_at": datetime.now().astimezone().isoformat(),
        "symbol": symbol,
        "training_window": {"start": TRAIN_START.isoformat(), "end": TRAIN_END.isoformat()},
        "holdout_window": {"start": HOLDOUT_START.isoformat(), "end": date.today().isoformat()},
        "source": {
            "store": str(candle_store.load_snapshot(symbol, 1).source_path),
            "meta": candle_store.load_meta(symbol, 1),
            "training_bars": len(training_candles),
            "holdout_bars": len(holdout_candles),
        },
        "method": {
            "engine": "backend.backtest.engine.BacktestEngine",
            "costs": "canonical commission_rt + fees_rt",
            "contract_size": 1,
            "stress_ticks_round_trip": STRESS_TICKS,
            "monte_carlo_iters": MC_ITERS,
            "monte_carlo_seed": MC_SEED,
            "selection": "training-only; eligible = n>=40, 14t PF>1, positive PnL in all three date walk-forward segments",
            "regime": "descriptive RTH trend + prior-60-day range volatility; attribution only",
            "segment_index_source": "backend.backtest.robustness.segment_index",
            "production_changed": False,
        },
        "selected_training_candidate": selected,
        "holdout": holdout,
        "candidates": results,
    }
    output = Path(args.out).expanduser() if args.out else market_data.derived_path(
        "research", f"volume_profile_research_{symbol.lower()}_2021_2025.json"
    )
    json_path, md_path, csv_path = _write(report, output)
    print(f"Selected: {selected['parameters'] if selected else 'none'}", flush=True)
    print(f"JSON: {json_path}", flush=True)
    print(f"Report: {md_path}", flush=True)
    print(f"CSV: {csv_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
