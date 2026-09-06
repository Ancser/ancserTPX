"""Asymmetric ATR SL/TP grid for the option-wall confidence entries.

This is a second-stage exit diagnostic.  It does not refit or alter the
walk-forward entry model.  Long and short stop distances and target distances
are independently varied, then combined.  A monthly expanding selection is
also reported so the full-period leaderboard is not mistaken for honest OOS
strategy evidence.
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.db.models import get_commission_rt, get_fees_rt, get_point_value
from scripts.option_wall_ml_study import DEFAULT_DATA_ROOT, _atomic_csv, _atomic_json, _iso
from scripts.option_wall_sltp_study import (
    _atr_blend_at,
    _five_minute_bars,
    _path_for_row,
    _pi_atr_levels,
    _read_mnq,
    _signal_columns,
    _simulate_ohlc_exit,
)


LONG_SL_GRID = (1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0)
SHORT_SL_GRID = (0.5, 0.75, 1.0, 1.25, 1.5)
LONG_TP_GRID = (None, 2.0, 3.0, 4.0, 6.0, 8.0, 10.0, 12.0)
SHORT_TP_GRID = (None, 1.0, 1.5, 2.0, 3.0, 4.0, 4.5, 6.0)
CURRENT_LONG = (4.0, 12.0)
CURRENT_SHORT = (1.5, 4.5)
CURRENT_STOP_ONLY_LONG = (4.0, None)
CURRENT_STOP_ONLY_SHORT = (1.5, None)


def _profit_factor(pnl: np.ndarray) -> float:
    gains = float(pnl[pnl > 0].sum())
    losses = float(-pnl[pnl < 0].sum())
    return gains / losses if losses > 0 else math.inf if gains > 0 else 0.0


def _basic_metrics(pnl: np.ndarray) -> dict[str, Any]:
    values = np.asarray(pnl, dtype=float)
    if not len(values):
        return {
            "trades": 0, "net_pnl": 0.0, "pf": 0.0,
            "win_rate": None, "max_drawdown": None, "pnl_to_drawdown": None,
        }
    equity = np.cumsum(values)
    peak = np.maximum.accumulate(np.r_[0.0, equity])[1:]
    drawdown = float((equity - peak).min())
    return {
        "trades": int(len(values)),
        "net_pnl": float(values.sum()),
        "pf": _profit_factor(values),
        "win_rate": float((values > 0).mean()),
        "max_drawdown": drawdown,
        "pnl_to_drawdown": float(values.sum() / max(abs(drawdown), 1.0)),
    }


def _metrics_with_months(
    pnl: np.ndarray,
    dates: np.ndarray,
    boundary_months: set[str],
) -> dict[str, Any]:
    result = _basic_metrics(pnl)
    months = np.asarray([str(value)[:7] for value in dates], dtype=str)
    month_rows: dict[str, Any] = {}
    for month in sorted(set(months)):
        summary = _basic_metrics(np.asarray(pnl)[months == month])
        summary["boundary_partial_month"] = month in boundary_months
        month_rows[month] = summary
    complete = [value for value in month_rows.values() if not value["boundary_partial_month"]]
    positive = sum(value["net_pnl"] > 0 for value in complete)
    result.update({
        "months": month_rows,
        "complete_months": int(len(complete)),
        "positive_complete_months": int(positive),
        "all_complete_months_positive": bool(complete) and positive == len(complete),
        "pnl_per_complete_month": (
            float(np.mean([value["net_pnl"] for value in complete])) if complete else None
        ),
        "median_pnl_per_complete_month": (
            float(np.median([value["net_pnl"] for value in complete])) if complete else None
        ),
    })
    return result


def _side_configs(
    sl_grid: Sequence[float], tp_grid: Sequence[float | None],
) -> list[tuple[float, float | None]]:
    return [(float(sl), None if tp is None else float(tp)) for sl in sl_grid for tp in tp_grid]


def _prepare_entries(data_root: Path) -> tuple[pd.DataFrame, list[pd.DataFrame], int]:
    article_path = data_root / "option_wall_article_walk_forward_signals.csv.gz"
    legacy_path = data_root / "option_wall_walk_forward_signals.csv.gz"
    if not article_path.is_file() or not legacy_path.is_file():
        raise RuntimeError("walk-forward signal files are missing")
    article = pd.read_csv(article_path, compression="gzip")
    article["as_of"] = pd.to_datetime(article["as_of"], utc=True)
    article["close_at"] = pd.to_datetime(article["close_at"], utc=True)
    article = article.sort_values("as_of").reset_index(drop=True)
    legacy = pd.read_csv(legacy_path, compression="gzip")
    legacy = legacy[legacy["as_of_et"].astype(str).str.endswith(":00")].copy()
    signal = _signal_columns(article, legacy)["primary_model_confidence"]

    mnq = _read_mnq(data_root)
    five_minute = _five_minute_bars(mnq)
    records: list[dict[str, Any]] = []
    paths: list[pd.DataFrame] = []
    skipped = 0
    for index in np.flatnonzero(signal != 0):
        source = article.iloc[int(index)]
        path = _path_for_row(mnq, source, 60)
        atr = _atr_blend_at(five_minute, pd.Timestamp(source["as_of"]))
        if path.empty or atr is None or not math.isfinite(atr) or atr <= 0:
            skipped += 1
            continue
        records.append({
            "date": str(source["date"]),
            "as_of": pd.Timestamp(source["as_of"]),
            "as_of_et": str(source["as_of_et"]),
            "direction": int(signal[index]),
            "entry_price": float(source["mnq_entry"]),
            "atr_blend": float(atr),
        })
        paths.append(path)
    return pd.DataFrame(records), paths, skipped


def _side_pnl_matrix(
    entries: pd.DataFrame,
    paths: list[pd.DataFrame],
    direction: int,
    configs: Sequence[tuple[float, float | None]],
) -> tuple[np.ndarray, list[dict[str, int]]]:
    point_value = get_point_value("MNQ")
    cost = get_commission_rt("MNQ") + get_fees_rt("MNQ")
    matrix = np.zeros((len(configs), len(entries)), dtype=float)
    reason_counts: list[dict[str, int]] = []
    positions = np.flatnonzero(entries["direction"].to_numpy(dtype=int) == direction)
    for config_index, (sl_multiple, tp_multiple) in enumerate(configs):
        reasons: Counter[str] = Counter()
        for position in positions:
            entry = float(entries.iloc[position]["entry_price"])
            atr = float(entries.iloc[position]["atr_blend"])
            sl, tp = _pi_atr_levels(
                entry, direction, atr, sl_multiple, tp_multiple,
            )
            outcome = _simulate_ohlc_exit(paths[position], direction, entry, sl, tp)
            market_points = float(outcome["exit_price"]) - entry
            matrix[config_index, position] = direction * market_points * point_value - cost
            reasons[str(outcome["exit_reason"])] += 1
        reason_counts.append(dict(reasons))
    return matrix, reason_counts


def _neighbor_lists(
    coordinates: Sequence[tuple[int, int, int, int]],
    shape: tuple[int, int, int, int],
) -> list[np.ndarray]:
    lookup = {coordinate: index for index, coordinate in enumerate(coordinates)}
    result: list[np.ndarray] = []
    for coordinate in coordinates:
        dimensions = [
            range(max(0, value - 1), min(limit, value + 2))
            for value, limit in zip(coordinate, shape)
        ]
        result.append(np.asarray([
            lookup[item] for item in itertools.product(*dimensions)
        ], dtype=int))
    return result


def _selection_score(pnl: np.ndarray) -> float:
    metrics = _basic_metrics(pnl)
    if metrics["net_pnl"] <= 0 or metrics["pf"] <= 1.0:
        return -1.0
    return float(metrics["pnl_to_drawdown"])


def _monthly_plateau_walk_forward(
    combo_matrix: np.ndarray,
    dates: np.ndarray,
    grid_rows: list[dict[str, Any]],
    neighbors: list[np.ndarray],
    current_index: int,
    prior_months: int = 3,
) -> dict[str, Any]:
    months = np.asarray([str(value)[:7] for value in dates], dtype=str)
    ordered_months = sorted(set(months))
    selected_rows: list[dict[str, Any]] = []
    realized: list[float] = []
    realized_dates: list[str] = []
    test_months = ordered_months[prior_months:]
    for month in test_months:
        train_mask = months < month
        test_mask = months == month
        raw_scores = np.asarray([
            _selection_score(combo_matrix[index, train_mask])
            for index in range(len(grid_rows))
        ])
        plateau_scores = np.asarray([
            float(np.median(raw_scores[group])) for group in neighbors
        ])
        eligible = np.flatnonzero(plateau_scores >= 0)
        selected = (
            int(eligible[np.argmax(plateau_scores[eligible])])
            if len(eligible) else int(current_index)
        )
        train_summary = _basic_metrics(combo_matrix[selected, train_mask])
        test_summary = _basic_metrics(combo_matrix[selected, test_mask])
        selected_rows.append({
            "test_month": month,
            **{key: grid_rows[selected][key] for key in (
                "long_sl_atr", "long_tp_atr", "short_sl_atr", "short_tp_atr",
            )},
            "selection_plateau_score": float(plateau_scores[selected]),
            "prior_trades": train_summary["trades"],
            "prior_net_pnl": train_summary["net_pnl"],
            "prior_pf": train_summary["pf"],
            "test_trades": test_summary["trades"],
            "test_net_pnl": test_summary["net_pnl"],
            "test_pf": test_summary["pf"],
        })
        realized.extend(combo_matrix[selected, test_mask].tolist())
        realized_dates.extend(dates[test_mask].tolist())
    boundary_months = {ordered_months[-1]} if ordered_months else set()
    return {
        "method": (
            "at each month boundary, use only prior trades; maximize the median "
            "positive PNL/maxDD score across the one-grid-step neighborhood"
        ),
        "minimum_prior_months": int(prior_months),
        "selected_by_month": selected_rows,
        "aggregate": _metrics_with_months(
            np.asarray(realized, dtype=float), np.asarray(realized_dates, dtype=str),
            boundary_months,
        ),
        "test_months": test_months,
    }


def _config_identity(row: dict[str, Any]) -> tuple[float, float | None, float, float | None]:
    return (
        float(row["long_sl_atr"]), row["long_tp_atr"],
        float(row["short_sl_atr"]), row["short_tp_atr"],
    )


def run_exit_grid(data_root: Path = DEFAULT_DATA_ROOT) -> dict[str, Any]:
    entries, paths, skipped = _prepare_entries(data_root)
    long_configs = _side_configs(LONG_SL_GRID, LONG_TP_GRID)
    short_configs = _side_configs(SHORT_SL_GRID, SHORT_TP_GRID)
    long_matrix, long_reasons = _side_pnl_matrix(entries, paths, 1, long_configs)
    short_matrix, short_reasons = _side_pnl_matrix(entries, paths, -1, short_configs)
    dates = entries["date"].astype(str).to_numpy()
    directions = entries["direction"].to_numpy(dtype=int)
    ordered_months = sorted({value[:7] for value in dates})
    boundary_months = {ordered_months[0], ordered_months[-1]}

    def side_table(
        configs: Sequence[tuple[float, float | None]],
        matrix: np.ndarray,
        side_direction: int,
        reasons: Sequence[dict[str, int]],
    ) -> pd.DataFrame:
        mask = directions == side_direction
        rows: list[dict[str, Any]] = []
        for index, (sl_multiple, tp_multiple) in enumerate(configs):
            summary = _metrics_with_months(matrix[index, mask], dates[mask], boundary_months)
            rows.append({
                "sl_atr": sl_multiple,
                "tp_atr": tp_multiple,
                "reward_risk": tp_multiple / sl_multiple if tp_multiple is not None else None,
                **{key: summary[key] for key in (
                    "trades", "net_pnl", "pf", "win_rate", "max_drawdown",
                    "pnl_to_drawdown", "complete_months", "positive_complete_months",
                    "pnl_per_complete_month", "median_pnl_per_complete_month",
                )},
                "exit_reasons": json.dumps(reasons[index], sort_keys=True),
            })
        return pd.DataFrame(rows)

    long_table = side_table(long_configs, long_matrix, 1, long_reasons)
    short_table = side_table(short_configs, short_matrix, -1, short_reasons)
    long_path = data_root / "option_wall_exit_grid_long.csv.gz"
    short_path = data_root / "option_wall_exit_grid_short.csv.gz"
    _atomic_csv(long_path, long_table)
    _atomic_csv(short_path, short_table)

    coordinates: list[tuple[int, int, int, int]] = []
    grid_rows: list[dict[str, Any]] = []
    pnl_rows: list[np.ndarray] = []
    for long_index, (long_sl, long_tp) in enumerate(long_configs):
        long_sl_index = LONG_SL_GRID.index(long_sl)
        long_tp_index = LONG_TP_GRID.index(long_tp)
        for short_index, (short_sl, short_tp) in enumerate(short_configs):
            short_sl_index = SHORT_SL_GRID.index(short_sl)
            short_tp_index = SHORT_TP_GRID.index(short_tp)
            pnl = long_matrix[long_index] + short_matrix[short_index]
            summary = _metrics_with_months(pnl, dates, boundary_months)
            long_summary = _basic_metrics(pnl[directions == 1])
            short_summary = _basic_metrics(pnl[directions == -1])
            row = {
                "long_sl_atr": long_sl,
                "long_tp_atr": long_tp,
                "long_reward_risk": long_tp / long_sl if long_tp is not None else None,
                "short_sl_atr": short_sl,
                "short_tp_atr": short_tp,
                "short_reward_risk": short_tp / short_sl if short_tp is not None else None,
                **{key: summary[key] for key in (
                    "trades", "net_pnl", "pf", "win_rate", "max_drawdown",
                    "pnl_to_drawdown", "complete_months", "positive_complete_months",
                    "all_complete_months_positive", "pnl_per_complete_month",
                    "median_pnl_per_complete_month",
                )},
                "long_net_pnl": long_summary["net_pnl"],
                "long_pf": long_summary["pf"],
                "long_max_drawdown": long_summary["max_drawdown"],
                "short_net_pnl": short_summary["net_pnl"],
                "short_pf": short_summary["pf"],
                "short_max_drawdown": short_summary["max_drawdown"],
                "long_exit_reasons": json.dumps(long_reasons[long_index], sort_keys=True),
                "short_exit_reasons": json.dumps(short_reasons[short_index], sort_keys=True),
            }
            coordinates.append((long_sl_index, long_tp_index, short_sl_index, short_tp_index))
            grid_rows.append(row)
            pnl_rows.append(pnl)

    combo_matrix = np.vstack(pnl_rows)
    neighbors = _neighbor_lists(
        coordinates,
        (len(LONG_SL_GRID), len(LONG_TP_GRID), len(SHORT_SL_GRID), len(SHORT_TP_GRID)),
    )
    pf_values = np.asarray([float(row["pf"]) for row in grid_rows])
    for index, row in enumerate(grid_rows):
        local_pf = pf_values[neighbors[index]]
        row["neighbor_count"] = int(len(local_pf))
        row["neighbor_pf_median"] = float(np.median(local_pf))
        row["neighbor_pf_min"] = float(np.min(local_pf))
        row["neighbor_profitable_fraction"] = float((local_pf > 1.0).mean())

    grid = pd.DataFrame(grid_rows)
    grid_path = data_root / "option_wall_exit_grid.csv.gz"
    _atomic_csv(grid_path, grid)
    identity_to_index = {_config_identity(row): index for index, row in enumerate(grid_rows)}
    current_index = identity_to_index[(4.0, 12.0, 1.5, 4.5)]
    stop_only_index = identity_to_index[(4.0, None, 1.5, None)]

    def ranked(column: str, count: int = 10) -> list[dict[str, Any]]:
        indexes = grid[column].sort_values(ascending=False).head(count).index
        return [grid_rows[int(index)] for index in indexes]

    stable = grid.sort_values(
        ["positive_complete_months", "neighbor_pf_median", "net_pnl"],
        ascending=False,
    ).head(10)
    full_walk = _monthly_plateau_walk_forward(
        combo_matrix, dates, grid_rows, neighbors, current_index,
    )
    test_month_set = set(full_walk["test_months"])
    walk_mask = np.asarray([value[:7] in test_month_set for value in dates], dtype=bool)
    full_walk["fixed_current_3r_same_window"] = _metrics_with_months(
        combo_matrix[current_index, walk_mask], dates[walk_mask], {ordered_months[-1]},
    )
    full_walk["fixed_current_stop_only_same_window"] = _metrics_with_months(
        combo_matrix[stop_only_index, walk_mask], dates[walk_mask], {ordered_months[-1]},
    )

    best_plateau_index = int(grid["neighbor_pf_median"].idxmax())
    best_net_index = int(grid["net_pnl"].idxmax())
    finite_tp = grid[grid["long_tp_atr"].notna() & grid["short_tp_atr"].notna()]
    best_finite_tp_index = int(finite_tp["net_pnl"].idxmax())
    best_stable_index = int(stable.index[0])

    def detailed(index: int) -> dict[str, Any]:
        pnl = combo_matrix[index]
        return {
            "configuration": grid_rows[index],
            "all_trades": _metrics_with_months(pnl, dates, boundary_months),
            "long": _metrics_with_months(
                pnl[directions == 1], dates[directions == 1], boundary_months,
            ),
            "short": _metrics_with_months(
                pnl[directions == -1], dates[directions == -1], boundary_months,
            ),
        }

    sensitivity: dict[str, Any] = {}
    for name, index in {
        "current_3r": current_index,
        "current_stop_only": stop_only_index,
        "best_full_period_net": best_net_index,
        "best_full_period_finite_tp": best_finite_tp_index,
        "best_full_period_plateau": best_plateau_index,
    }.items():
        sensitivity[name] = {
            str(extra): _basic_metrics(combo_matrix[index] - float(extra))
            for extra in (0, 1, 2, 4)
        }

    report: dict[str, Any] = {
        "status": "diagnostic_grid_not_live_validated",
        "created_at": _iso(pd.Timestamp.now(tz="UTC").to_pydatetime()),
        "entry_strategy": "primary_model_confidence; unchanged 60-minute walk-forward entries",
        "entries": int(len(entries)),
        "skipped_entries": int(skipped),
        "oos_first_session": str(entries["date"].min()),
        "oos_last_session": str(entries["date"].max()),
        "grid": {
            "long_sl_atr": list(LONG_SL_GRID),
            "short_sl_atr": list(SHORT_SL_GRID),
            "long_tp_atr": list(LONG_TP_GRID),
            "short_tp_atr": list(SHORT_TP_GRID),
            "combinations": int(len(grid_rows)),
            "results_file": str(grid_path),
            "long_results_file": str(long_path),
            "short_results_file": str(short_path),
        },
        "references": {
            "current_asymmetric_3r": grid_rows[current_index],
            "current_asymmetric_stop_only": grid_rows[stop_only_index],
        },
        "full_period_leaderboards": {
            "highest_net_pnl": ranked("net_pnl"),
            "highest_pf": ranked("pf"),
            "widest_profitable_neighborhood": ranked("neighbor_pf_median"),
            "monthly_stability_then_plateau": stable.to_dict("records"),
        },
        "selected_details": {
            "current_asymmetric_3r": detailed(current_index),
            "current_asymmetric_stop_only": detailed(stop_only_index),
            "best_full_period_net": detailed(best_net_index),
            "best_full_period_finite_tp": detailed(best_finite_tp_index),
            "best_full_period_plateau": detailed(best_plateau_index),
            "best_full_period_monthly_stability": detailed(best_stable_index),
        },
        "monthly_plateau_walk_forward": full_walk,
        "additional_cost_per_trade_sensitivity": sensitivity,
        "warnings": [
            "The full-period leaderboard optimizes exits on the same OOS entry outcomes and is in-sample for exit selection.",
            "Only monthly expanding selection is sequentially out of sample for the exit choice.",
            "The grid changes exits only; it does not retrain the option-wall entry model.",
            "One-minute OHLC and the repository's conservative same-bar resolver are used; tick order remains unknown.",
        ],
    }
    _atomic_json(data_root / "option_wall_exit_grid_report.json", report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = run_exit_grid(args.data_root)
    print(json.dumps(report, indent=2, allow_nan=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
