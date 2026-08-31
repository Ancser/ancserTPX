"""Constrained micro-tuning for the two near-miss prop intraday ideas.

This script deliberately does not search for the best full-sample result.
Primary parameters are selected on 2020-2023 development data, then reported
on 2024-present validation data.  A separately labelled risk-repair diagnostic
uses the full sample to test the exact canonical failure.  The validation
period is non-blind: aggregate baseline diagnostics were inspected before this
grid was declared.

The ORB study changes only the proposed opening-range minimum.  The mean-
reversion study changes only values already present in the original proposal.
Every grid reproduces its current baseline first, verifies that every scanned
dimension changes simulated trades, and looks for neighbouring plateaus.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from itertools import product
from pathlib import Path
from typing import Any, Iterable, Sequence


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from backend.backtest.prop_intraday_research import (  # noqa: E402
    ET,
    MeanReversionConfig,
    OrbConfig,
    ResearchTrade,
    SYMBOL_RULES,
    load_symbol_sessions,
    recommended_configs,
    run_config,
)
from backend.backtest.robustness import series_stats, slip_injection  # noqa: E402


DEVELOPMENT_END = date(2023, 12, 31)
VALIDATION_START = date(2024, 1, 1)
ORB_WIDTHS = {
    "MES": (8.0, 10.0, 12.0, 14.0, 16.0, 18.0, 20.0, 22.0),
    "MNQ": (15.0, 20.0, 24.0, 28.0, 32.0, 36.0, 40.0, 44.0),
}
MR_SIGMAS = (1.5, 1.75, 2.0)
MR_FLAT_LIMITS = (0.0003, 0.0005)
MR_STOP_PROFILES = ("tight", "wide")
MR_HOLD_MINUTES = (30, 45, 60)
RISK_BUDGETS = (100.0, 125.0, 150.0, 175.0, 200.0)


def _json_default(value: Any):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"not JSON serialisable: {type(value)!r}")


def _trade_signature(trades: Sequence[ResearchTrade]) -> tuple:
    """Variant-independent exact signature used for baseline reproduction."""

    return tuple(
        (
            trade.entry_time.isoformat(),
            trade.exit_time.isoformat(),
            trade.direction,
            trade.contracts,
            round(trade.entry_price, 8),
            round(trade.exit_price, 8),
            round(trade.pnl, 8),
            trade.exit_reason,
        )
        for trade in trades
    )


def _fingerprint(trades: Sequence[ResearchTrade]) -> tuple:
    return (
        len(trades),
        round(sum(trade.pnl for trade in trades), 6),
        _trade_signature(trades),
    )


def _period_trades(
    trades: Sequence[ResearchTrade],
    *,
    start: date | None = None,
    end: date | None = None,
) -> tuple[ResearchTrade, ...]:
    out = []
    for trade in trades:
        day = trade.entry_time.astimezone(ET).date()
        if start is not None and day < start:
            continue
        if end is not None and day > end:
            continue
        out.append(trade)
    return tuple(out)


def _period_metrics(trades: Sequence[ResearchTrade]) -> dict:
    pnls = [trade.pnl for trade in trades]
    rows = [trade.robustness_row() for trade in trades]
    slip = slip_injection(rows, levels=(4, 8, 14))
    levels = {str(row["level"]): row["stats"] for row in slip["levels"]}
    yearly: dict[str, list[float]] = {}
    for trade in trades:
        year = str(trade.entry_time.astimezone(ET).year)
        yearly.setdefault(year, []).append(trade.pnl)
    return {
        "stats": series_stats(pnls),
        "slip": levels,
        "yearly": {year: series_stats(values) for year, values in sorted(yearly.items())},
    }


def _development_pass(metrics: dict) -> bool:
    stats = metrics["stats"]
    slip4 = metrics["slip"]["4"]
    return bool(
        stats["n"] >= 25
        and stats["pnl"] > 0
        and stats["pf"] >= 1.15
        and stats["max_dd"] <= 2000
        and slip4["pnl"] > 0
        and slip4["pf"] > 1.0
    )


def _validation_pass(metrics: dict) -> bool:
    stats = metrics["stats"]
    slip4 = metrics["slip"]["4"]
    return bool(
        stats["n"] >= 15
        and stats["pnl"] > 0
        and stats["pf"] > 1.05
        and stats["max_dd"] <= 2000
        and slip4["pnl"] > 0
        and slip4["pf"] > 1.0
    )


def _result_row(result, family: str, logical_params: dict) -> dict:
    development = _period_metrics(
        _period_trades(result.trades, end=DEVELOPMENT_END)
    )
    validation = _period_metrics(
        _period_trades(result.trades, start=VALIDATION_START)
    )
    full = _period_metrics(result.trades)
    return {
        "symbol": result.symbol,
        "family": family,
        "variant": result.variant,
        # A grid row owns its parameter payload.  Reusing the caller's dict
        # makes a later sensitivity label rewrite every earlier row.
        "logical_params": dict(logical_params),
        "config": result.config,
        "diagnostics": result.diagnostics,
        "development": development,
        "validation": validation,
        "full": full,
        "development_pass": _development_pass(development),
        "validation_pass": _validation_pass(validation),
        "full_8t_positive": bool(
            full["slip"]["8"]["pnl"] > 0 and full["slip"]["8"]["pf"] > 1.0
        ),
        "canonical_verdict": result.summary["verdict"],
        "monte_carlo": result.summary.get("monte_carlo"),
        "walk_forward_pass": bool(
            (result.summary.get("walk_forward") or {}).get("pass")
        ),
        "walk_forward": result.summary.get("walk_forward"),
        "monte_carlo_pass": bool(result.summary.get("monte_carlo_pass")),
        "position_size": result.summary.get("position_size"),
        "_fingerprint": _fingerprint(result.trades),
    }


def _current_config(symbol: str, name: str):
    matches = [config for config in recommended_configs(symbol) if config.name == name]
    if len(matches) != 1:
        raise RuntimeError(f"expected one current config named {name!r}; got {len(matches)}")
    return matches[0]


def _orb_configs(symbol: str) -> list[tuple[OrbConfig, dict]]:
    maximum = SYMBOL_RULES[symbol].opening_width_max
    return [
        (
            OrbConfig(
                f"orb15_5m_retest_min{width:g}",
                entry_mode="retest",
                opening_width_min=width,
                opening_width_max=maximum,
            ),
            {"width_index": index, "opening_width_min": width},
        )
        for index, width in enumerate(ORB_WIDTHS[symbol])
    ]


def _mean_reversion_configs(symbol: str) -> list[tuple[MeanReversionConfig, dict]]:
    rules = SYMBOL_RULES[symbol]
    stops = {
        "tight": rules.mean_stop_tight,
        "wide": rules.mean_stop_wide,
    }
    rows = []
    for sigma, flat, stop_profile, hold in product(
        MR_SIGMAS,
        MR_FLAT_LIMITS,
        MR_STOP_PROFILES,
        MR_HOLD_MINUTES,
    ):
        config = MeanReversionConfig(
            (
                f"meanrev_5m_{sigma:g}sd_vwap_{stop_profile}_"
                f"flat{flat * 10000:g}_hold{hold}"
            ),
            entry_sigma=sigma,
            stop_buffer_points=stops[stop_profile],
            vwap_flat_max_pct=flat,
            max_hold_minutes=hold,
        )
        params = {
            "entry_sigma": sigma,
            "vwap_flat_max_pct": flat,
            "stop_profile": stop_profile,
            "max_hold_minutes": hold,
        }
        rows.append((config, params))
    return rows


def _verify_baseline(
    sessions,
    *,
    symbol: str,
    current_name: str,
    explicit_config,
    monte_carlo_iters: int,
) -> dict:
    current = run_config(
        sessions,
        _current_config(symbol, current_name),
        symbol,
        monte_carlo_iters=monte_carlo_iters,
    )
    explicit = run_config(
        sessions,
        explicit_config,
        symbol,
        monte_carlo_iters=monte_carlo_iters,
    )
    current_sig = _trade_signature(current.trades)
    explicit_sig = _trade_signature(explicit.trades)
    if current_sig != explicit_sig:
        raise RuntimeError(
            f"{symbol} {current_name} was not reproduced by the tuning grid"
        )
    return {
        "pass": True,
        "current_name": current_name,
        "trades": len(current_sig),
        "pnl": current.summary["stats"]["pnl"],
        "pf": current.summary["stats"]["pf"],
    }


def _dimension_effects(rows: Sequence[dict], dimensions: Sequence[str]) -> dict:
    effects = {}
    for dimension in dimensions:
        controls: dict[tuple, set] = {}
        for row in rows:
            params = row["logical_params"]
            key = tuple(
                (name, params[name]) for name in dimensions if name != dimension
            )
            controls.setdefault(key, set()).add(row["_fingerprint"])
        effects[dimension] = any(len(fingerprints) > 1 for fingerprints in controls.values())
    dead = [name for name, changed in effects.items() if not changed]
    if dead:
        raise RuntimeError(f"grid dimensions did not change trades: {', '.join(dead)}")
    return effects


def _annotate_orb_plateau(rows: list[dict]) -> dict:
    ordered = sorted(rows, key=lambda row: row["logical_params"]["width_index"])
    passing = [bool(row["development_pass"]) for row in ordered]
    for index, row in enumerate(ordered):
        row["development_plateau"] = bool(
            0 < index < len(ordered) - 1
            and passing[index - 1]
            and passing[index]
            and passing[index + 1]
        )

    runs: list[list[int]] = []
    current: list[int] = []
    for index, passed in enumerate(passing):
        if passed:
            current.append(index)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    eligible = [run for run in runs if len(run) >= 3]
    if not eligible:
        return {"development_plateau_found": False, "selected_width_index": None}
    longest = max(eligible, key=lambda run: (len(run), -run[0]))
    selected_index = longest[(len(longest) - 1) // 2]
    selected = ordered[selected_index]
    return {
        "development_plateau_found": True,
        "passing_width_indices": longest,
        "passing_widths": [
            ordered[index]["logical_params"]["opening_width_min"] for index in longest
        ],
        "selected_width_index": selected["logical_params"]["width_index"],
        "selected_width": selected["logical_params"]["opening_width_min"],
        "selection_rule": "lower middle of longest adjacent development-pass run",
    }


def _is_adjacent(a: dict, b: dict, dimension: str, value_order: dict) -> bool:
    for name in a:
        if name == dimension:
            continue
        if a[name] != b[name]:
            return False
    order = value_order[dimension]
    return abs(order.index(a[dimension]) - order.index(b[dimension])) == 1


def _annotate_mean_reversion_plateau(rows: list[dict]) -> dict:
    dimensions = (
        "entry_sigma",
        "vwap_flat_max_pct",
        "stop_profile",
        "max_hold_minutes",
    )
    value_order = {
        "entry_sigma": list(MR_SIGMAS),
        "vwap_flat_max_pct": list(MR_FLAT_LIMITS),
        "stop_profile": list(MR_STOP_PROFILES),
        "max_hold_minutes": list(MR_HOLD_MINUTES),
    }
    plateau_rows = []
    for row in rows:
        support = {}
        for dimension in dimensions:
            support[dimension] = any(
                other is not row
                and other["development_pass"]
                and _is_adjacent(
                    row["logical_params"],
                    other["logical_params"],
                    dimension,
                    value_order,
                )
                for other in rows
            )
        row["development_neighbor_support"] = support
        row["development_plateau"] = bool(
            row["development_pass"] and all(support.values())
        )
        if row["development_plateau"]:
            plateau_rows.append(row)

    if not plateau_rows:
        return {"development_plateau_found": False, "selected_params": None}

    baseline = {
        "entry_sigma": 2.0,
        "vwap_flat_max_pct": 0.0005,
        "stop_profile": "wide",
        "max_hold_minutes": 45,
    }

    def distance(row: dict) -> tuple:
        params = row["logical_params"]
        steps = sum(
            abs(value_order[name].index(params[name]) - value_order[name].index(value))
            for name, value in baseline.items()
        )
        return steps, row["variant"]

    selected = min(plateau_rows, key=distance)
    return {
        "development_plateau_found": True,
        "plateau_cells": len(plateau_rows),
        "selected_params": selected["logical_params"],
        "selection_rule": "development plateau cell closest to current baseline",
    }


def _public_row(row: dict) -> dict:
    return {key: value for key, value in row.items() if not key.startswith("_")}


def _find_row(rows: Sequence[dict], **params) -> dict | None:
    for row in rows:
        if all(row["logical_params"].get(key) == value for key, value in params.items()):
            return row
    return None


def _matching_mr_config(symbol: str, params: dict) -> MeanReversionConfig:
    for config, logical in _mean_reversion_configs(symbol):
        if logical == params:
            return config
    raise RuntimeError(f"missing {symbol} mean-reversion config for {params!r}")


def _risk_repair_study(
    mnq_sessions,
    mes_sessions,
    *,
    monte_carlo_iters: int,
) -> tuple[dict, list[dict]]:
    """Test whether lower sizing fixes the actual Monte Carlo failure.

    The fixed $150 grid is evaluated in full, not just at the two promising
    cells.  Risk sensitivity is then run for every fixed-grid canonical
    candidate.  A selected budget must pass for every such signal cell and
    have an adjacent passing budget for each one.
    """

    robust_iters = max(1000, monte_carlo_iters)
    fixed_rows = []
    grid = _mean_reversion_configs("MNQ")
    for index, (config, params) in enumerate(grid, start=1):
        tuned = replace(
            config,
            name=f"{config.name}_risk150",
            risk_dollars=150.0,
        )
        print(f"[MNQ MR risk150] {index}/{len(grid)} {tuned.name}", flush=True)
        result = run_config(
            mnq_sessions,
            tuned,
            "MNQ",
            monte_carlo_iters=robust_iters,
        )
        fixed_rows.append(_result_row(result, "mean_reversion_risk150", params))

    dimensions = (
        "entry_sigma",
        "vwap_flat_max_pct",
        "stop_profile",
        "max_hold_minutes",
    )
    fixed_effects = _dimension_effects(fixed_rows, dimensions)
    candidate_rows = [
        row
        for row in fixed_rows
        if row["canonical_verdict"] == "RESEARCH_CANDIDATE"
        and row["development_pass"]
        and row["validation_pass"]
    ]

    sensitivity_rows = []
    for candidate in candidate_rows:
        params = candidate["logical_params"]
        base = _matching_mr_config("MNQ", params)
        for risk in RISK_BUDGETS:
            if risk == 150.0:
                row = dict(candidate)
                row["logical_params"] = dict(candidate["logical_params"])
                row["family"] = "mean_reversion_risk_sensitivity"
            else:
                config = replace(
                    base,
                    name=f"{base.name}_risk{risk:g}",
                    risk_dollars=risk,
                )
                print(
                    f"[MNQ MR risk] {candidate['variant']} @ ${risk:g}",
                    flush=True,
                )
                result = run_config(
                    mnq_sessions,
                    config,
                    "MNQ",
                    monte_carlo_iters=robust_iters,
                )
                row = _result_row(result, "mean_reversion_risk_sensitivity", params)
            row["logical_params"]["risk_dollars"] = risk
            sensitivity_rows.append(row)

    def row_passes(row: dict) -> bool:
        return bool(
            row["canonical_verdict"] == "RESEARCH_CANDIDATE"
            and row["development_pass"]
            and row["validation_pass"]
        )

    signal_keys = [
        tuple(sorted(row["logical_params"].items())) for row in candidate_rows
    ]
    supported_risks = []
    for risk_index, risk in enumerate(RISK_BUDGETS):
        all_signals_pass = True
        every_signal_has_neighbor = True
        for signal_key in signal_keys:
            params = dict(signal_key)
            matching = [
                row
                for row in sensitivity_rows
                if row["logical_params"].get("risk_dollars") == risk
                and all(row["logical_params"].get(key) == value for key, value in params.items())
            ]
            if len(matching) != 1 or not row_passes(matching[0]):
                all_signals_pass = False
                every_signal_has_neighbor = False
                continue
            neighbor_indices = [risk_index - 1, risk_index + 1]
            neighbor_pass = False
            for neighbor_index in neighbor_indices:
                if not (0 <= neighbor_index < len(RISK_BUDGETS)):
                    continue
                neighbor_risk = RISK_BUDGETS[neighbor_index]
                neighbor = [
                    row
                    for row in sensitivity_rows
                    if row["logical_params"].get("risk_dollars") == neighbor_risk
                    and all(
                        row["logical_params"].get(key) == value
                        for key, value in params.items()
                    )
                ]
                if len(neighbor) == 1 and row_passes(neighbor[0]):
                    neighbor_pass = True
            every_signal_has_neighbor &= neighbor_pass
        if signal_keys and all_signals_pass and every_signal_has_neighbor:
            supported_risks.append(risk)

    selected_risk = max(supported_risks) if supported_risks else None
    risk_effect = True
    for signal_key in signal_keys:
        params = dict(signal_key)
        fingerprints = {
            row["_fingerprint"]
            for row in sensitivity_rows
            if all(row["logical_params"].get(key) == value for key, value in params.items())
        }
        risk_effect &= len(fingerprints) > 1
    if signal_keys and not risk_effect:
        raise RuntimeError("risk_dollars did not change trades for every candidate signal")

    cross_rows = []
    if selected_risk is not None:
        for candidate in candidate_rows:
            params = candidate["logical_params"]
            base = _matching_mr_config("MES", params)
            config = replace(
                base,
                name=f"{base.name}_risk{selected_risk:g}_cross_mes",
                risk_dollars=selected_risk,
            )
            print(f"[MES MR cross] {config.name}", flush=True)
            result = run_config(
                mes_sessions,
                config,
                "MES",
                monte_carlo_iters=robust_iters,
            )
            logical = dict(params)
            logical["risk_dollars"] = selected_risk
            cross_rows.append(
                _result_row(result, "mean_reversion_risk150_cross", logical)
            )

    report = {
        "monte_carlo_iters": robust_iters,
        "fixed_risk": 150.0,
        "fixed_grid_dimension_effects": fixed_effects,
        "fixed_grid_canonical_candidates": len(candidate_rows),
        "candidate_signal_params": [row["logical_params"] for row in candidate_rows],
        "risk_budgets": RISK_BUDGETS,
        "risk_dimension_changes_trades": risk_effect,
        "supported_risks": supported_risks,
        "selected_risk": selected_risk,
        "selection_rule": (
            "canonical + development + non-blind-validation pass for every fixed-grid "
            "candidate, with an adjacent passing risk for each signal"
        ),
        "fixed_grid": [_public_row(row) for row in fixed_rows],
        "risk_sensitivity": [_public_row(row) for row in sensitivity_rows],
        "cross_symbol": [_public_row(row) for row in cross_rows],
    }
    return report, [*fixed_rows, *sensitivity_rows, *cross_rows]


def _short_metrics(metrics: dict) -> str:
    stats = metrics["stats"]
    slip8 = metrics["slip"]["8"]
    return (
        f"n={stats['n']} PnL=${stats['pnl']:.0f} PF={stats['pf']:.2f} "
        f"8tPF={slip8['pf']:.2f} DD=${stats['max_dd']:.0f}"
    )


def _write_csv(path: Path, family_rows: Iterable[dict]) -> None:
    rows = []
    for row in family_rows:
        flat = {
            "symbol": row["symbol"],
            "family": row["family"],
            "variant": row["variant"],
            **row["logical_params"],
            "dev_n": row["development"]["stats"]["n"],
            "dev_pnl": row["development"]["stats"]["pnl"],
            "dev_pf": row["development"]["stats"]["pf"],
            "dev_4t_pf": row["development"]["slip"]["4"]["pf"],
            "validation_n": row["validation"]["stats"]["n"],
            "validation_pnl": row["validation"]["stats"]["pnl"],
            "validation_pf": row["validation"]["stats"]["pf"],
            "validation_4t_pf": row["validation"]["slip"]["4"]["pf"],
            "full_n": row["full"]["stats"]["n"],
            "full_pnl": row["full"]["stats"]["pnl"],
            "full_pf": row["full"]["stats"]["pf"],
            "full_8t_pf": row["full"]["slip"]["8"]["pf"],
            "full_14t_pf": row["full"]["slip"]["14"]["pf"],
            "development_pass": row["development_pass"],
            "development_plateau": row.get("development_plateau", False),
            "validation_pass": row["validation_pass"],
            "canonical_verdict": row["canonical_verdict"],
        }
        rows.append(flat)
    fieldnames = sorted({key for row in rows for key in row})
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_markdown(path: Path, payload: dict) -> None:
    lines = [
        "# Prop Intraday Constrained Micro-Tuning",
        "",
        f"Generated: {payload['created_at']}",
        "",
        "This is exploratory research, not live-strategy approval. Validation is non-blind because aggregate 2024-present baseline diagnostics were inspected before the grid was declared.",
        "",
        "## Protocol",
        "",
        "- Parameter selection: 2020-2023 development only",
        "- Validation: 2024-present; not used for primary-grid selection",
        "- The second-stage risk repair is explicitly full-sample exploratory",
        "- ORB: only the minimum opening-range width changes",
        "- Mean reversion: only sigma, VWAP-flat limit, proposed stop profile, and 30/45/60-minute time stop change",
        "- Existing baseline must reproduce exactly; every grid dimension must alter simulated trades",
        "- A canonical pass remains the harness's existing 100-trade + walk-forward + 14-tick + Monte Carlo gate",
        "",
    ]
    for family, primary_symbol in (("orb", "MES"), ("mean_reversion", "MNQ")):
        selection = payload["selection"][family]
        lines.extend([f"## {family.replace('_', ' ').title()} ({primary_symbol} primary)", ""])
        if not selection.get("selected_primary"):
            lines.extend(["No development plateau met the predeclared gate.", ""])
            continue
        primary = selection["selected_primary"]
        cross = selection.get("cross_symbol")
        lines.extend(
            [
                f"Selected parameters: `{json.dumps(primary['logical_params'], sort_keys=True)}`",
                "",
                f"- Development: {_short_metrics(primary['development'])}",
                f"- Non-blind validation: {_short_metrics(primary['validation'])}",
                f"- Full: {_short_metrics(primary['full'])}",
                f"- Canonical verdict: `{primary['canonical_verdict']}`",
            ]
        )
        if cross:
            lines.append(
                f"- Cross-symbol {cross['symbol']}: {_short_metrics(cross['full'])}; "
                f"verdict `{cross['canonical_verdict']}`"
            )
        lines.append("")
    repair = payload["risk_repair"]
    lines.extend(
        [
            "## Mean Reversion Risk Repair",
            "",
            f"- Fixed $150 grid canonical candidates: {repair['fixed_grid_canonical_candidates']}",
            f"- Supported common risk: {repair['selected_risk']}",
            f"- Monte Carlo iterations: {repair['monte_carlo_iters']}",
            "",
        ]
    )
    selected = repair.get("selected_risk")
    if selected is not None:
        for row in repair["risk_sensitivity"]:
            if (
                row["logical_params"].get("risk_dollars") == selected
                and row["canonical_verdict"] == "RESEARCH_CANDIDATE"
            ):
                lines.append(
                    f"- `{row['variant']}`: {_short_metrics(row['full'])}; "
                    f"14tPF={row['full']['slip']['14']['pf']:.2f}"
                )
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mc-iters", type=int, default=250)
    parser.add_argument(
        "--output",
        default="data/research/prop_intraday_micro_tune_current.json",
    )
    args = parser.parse_args()
    if args.mc_iters <= 0:
        parser.error("--mc-iters must be positive")

    started = time.time()
    sessions_by_symbol = {}
    datasets = {}
    for symbol in ("MES", "MNQ"):
        load_started = time.time()
        sessions, info = load_symbol_sessions(symbol)
        sessions_by_symbol[symbol] = sessions
        datasets[symbol] = asdict(info)
        print(
            f"[{symbol}] {len(sessions):,} full RTH sessions loaded in "
            f"{time.time() - load_started:.1f}s",
            flush=True,
        )

    reproduction = {}
    family_rows: dict[str, dict[str, list[dict]]] = {
        "orb": {},
        "mean_reversion": {},
    }
    dimension_effects = {}

    for symbol in ("MES", "MNQ"):
        sessions = sessions_by_symbol[symbol]
        orb_grid = _orb_configs(symbol)
        reproduction[f"{symbol}_orb"] = _verify_baseline(
            sessions,
            symbol=symbol,
            current_name="orb15_5m_retest_std_v12_tp20",
            explicit_config=orb_grid[0][0],
            monte_carlo_iters=args.mc_iters,
        )
        rows = []
        for index, (config, params) in enumerate(orb_grid, start=1):
            print(f"[{symbol} ORB] {index}/{len(orb_grid)} {config.name}", flush=True)
            result = run_config(
                sessions, config, symbol, monte_carlo_iters=args.mc_iters
            )
            rows.append(_result_row(result, "orb", params))
        dimension_effects[f"{symbol}_orb"] = _dimension_effects(
            rows, ("opening_width_min",)
        )
        _annotate_orb_plateau(rows)
        family_rows["orb"][symbol] = rows

        mr_grid = _mean_reversion_configs(symbol)
        baseline = next(
            config
            for config, params in mr_grid
            if params
            == {
                "entry_sigma": 2.0,
                "vwap_flat_max_pct": 0.0005,
                "stop_profile": "wide",
                "max_hold_minutes": 45,
            }
        )
        reproduction[f"{symbol}_mean_reversion"] = _verify_baseline(
            sessions,
            symbol=symbol,
            current_name="meanrev_5m_20sd_vwap_flat05",
            explicit_config=baseline,
            monte_carlo_iters=args.mc_iters,
        )
        rows = []
        for index, (config, params) in enumerate(mr_grid, start=1):
            print(
                f"[{symbol} MR] {index}/{len(mr_grid)} {config.name}",
                flush=True,
            )
            result = run_config(
                sessions, config, symbol, monte_carlo_iters=args.mc_iters
            )
            rows.append(_result_row(result, "mean_reversion", params))
        dimensions = (
            "entry_sigma",
            "vwap_flat_max_pct",
            "stop_profile",
            "max_hold_minutes",
        )
        dimension_effects[f"{symbol}_mean_reversion"] = _dimension_effects(
            rows, dimensions
        )
        _annotate_mean_reversion_plateau(rows)
        family_rows["mean_reversion"][symbol] = rows

    risk_repair, risk_rows = _risk_repair_study(
        sessions_by_symbol["MNQ"],
        sessions_by_symbol["MES"],
        monte_carlo_iters=args.mc_iters,
    )

    mes_orb_plateau = _annotate_orb_plateau(family_rows["orb"]["MES"])
    orb_primary = None
    orb_cross = None
    selected_width_index = mes_orb_plateau.get("selected_width_index")
    if selected_width_index is not None:
        orb_primary = _find_row(
            family_rows["orb"]["MES"], width_index=selected_width_index
        )
        orb_cross = _find_row(
            family_rows["orb"]["MNQ"], width_index=selected_width_index
        )

    mnq_mr_plateau = _annotate_mean_reversion_plateau(
        family_rows["mean_reversion"]["MNQ"]
    )
    mr_primary = None
    mr_cross = None
    selected_mr = mnq_mr_plateau.get("selected_params")
    if selected_mr:
        mr_primary = _find_row(family_rows["mean_reversion"]["MNQ"], **selected_mr)
        mr_cross = _find_row(family_rows["mean_reversion"]["MES"], **selected_mr)

    public_families = {
        family: {
            symbol: [_public_row(row) for row in rows]
            for symbol, rows in symbols.items()
        }
        for family, symbols in family_rows.items()
    }
    selection = {
        "orb": {
            **mes_orb_plateau,
            "primary_symbol": "MES",
            "selected_primary": _public_row(orb_primary) if orb_primary else None,
            "cross_symbol": _public_row(orb_cross) if orb_cross else None,
        },
        "mean_reversion": {
            **mnq_mr_plateau,
            "primary_symbol": "MNQ",
            "selected_primary": _public_row(mr_primary) if mr_primary else None,
            "cross_symbol": _public_row(mr_cross) if mr_cross else None,
        },
    }
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": time.time() - started,
        "protocol": {
            "development": "2020-01-01 through 2023-12-31",
            "validation": "2024-01-01 through latest local store",
            "validation_blind": False,
            "validation_note": (
                "Aggregate 2024-present baseline/diagnostic results were inspected "
                "before this grid; validation is corroborative, not confirmatory."
            ),
            "primary_grid_selection_uses_validation": False,
            "risk_repair_uses_full_sample": True,
            "development_gate": {
                "n_min": 25,
                "pnl": ">0",
                "pf_min": 1.15,
                "max_dd_max": 2000,
                "4_tick_pnl": ">0",
                "4_tick_pf": ">1",
            },
            "validation_gate": {
                "n_min": 15,
                "pnl": ">0",
                "pf_min": 1.05,
                "max_dd_max": 2000,
                "4_tick_pnl": ">0",
                "4_tick_pf": ">1",
            },
            "canonical_gate_unchanged": (
                "100 trades + baseline PF + walk-forward + 14-tick slippage + "
                "Monte Carlo, as implemented by prop_intraday_research"
            ),
        },
        "grids": {
            "orb_opening_widths": ORB_WIDTHS,
            "mean_reversion": {
                "entry_sigma": MR_SIGMAS,
                "vwap_flat_max_pct": MR_FLAT_LIMITS,
                "stop_profile": MR_STOP_PROFILES,
                "max_hold_minutes": MR_HOLD_MINUTES,
            },
            "risk_budgets": RISK_BUDGETS,
        },
        "datasets": datasets,
        "baseline_reproduction": reproduction,
        "dimension_effects": dimension_effects,
        "selection": selection,
        "risk_repair": risk_repair,
        "results": public_families,
    }

    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    flat_rows = [
        row
        for symbols in family_rows.values()
        for rows in symbols.values()
        for row in rows
    ] + risk_rows
    _write_csv(output.with_suffix(".csv"), flat_rows)
    _write_markdown(output.with_suffix(".md"), payload)
    print(f"Wrote {output}", flush=True)
    print(f"Elapsed {time.time() - started:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
