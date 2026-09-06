"""Leakage-safe walk-forward and day-block Monte Carlo for option-wall research.

This script consumes the point-in-time dataset produced by
``option_wall_ml_study.py build``.  It retrains before every test session using
only older sessions, compares two model algorithms on an inner chronological
validation slice, and reports actual 1-MNQ results after repository costs.

It is research-only and has no order-routing imports.
"""
from __future__ import annotations

import argparse
import calendar
import json
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, balanced_accuracy_score, f1_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.option_wall_ml_study import (
    DEFAULT_DATA_ROOT,
    _atomic_csv,
    _atomic_json,
    _feature_columns,
    _mnq_pnl_summary,
)


def _candidates() -> dict[str, Pipeline]:
    return {
        "logistic": Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("scale", StandardScaler()),
            ("model", LogisticRegression(
                max_iter=2000, class_weight="balanced", random_state=42,
            )),
        ]),
        "hist_gradient_boosting": Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("model", HistGradientBoostingClassifier(
                max_iter=180, learning_rate=0.05, max_leaf_nodes=15,
                min_samples_leaf=20, l2_regularization=1.0, random_state=42,
            )),
        ]),
    }


def walk_forward_boundaries(dates: Sequence[str], min_train_sessions: int) -> list[tuple[list[str], str]]:
    ordered = sorted(dict.fromkeys(str(value) for value in dates))
    if min_train_sessions < 10:
        raise ValueError("min_train_sessions must be at least 10")
    if len(ordered) <= min_train_sessions:
        raise ValueError("not enough sessions for walk-forward testing")
    return [(ordered[:idx], ordered[idx]) for idx in range(min_train_sessions, len(ordered))]


def _day_block_monte_carlo(summary: dict[str, Any], all_sessions: Sequence[str],
                           simulations: int, horizon_sessions: int,
                           seed: int = 42) -> dict[str, Any]:
    by_session = summary.get("by_session_pnl", {})
    daily = np.asarray([float(by_session.get(str(day), 0.0)) for day in all_sessions], dtype=float)
    if not len(daily):
        return {"status": "unavailable"}
    rng = np.random.default_rng(seed)
    draws = daily[rng.integers(0, len(daily), size=(simulations, horizon_sessions))]
    paths = draws.cumsum(axis=1)
    terminal = paths[:, -1]
    running_peak = np.maximum.accumulate(np.c_[np.zeros(simulations), paths], axis=1)[:, 1:]
    max_drawdown = (paths - running_peak).min(axis=1)

    def percentiles(values: np.ndarray) -> dict[str, float]:
        q05, q50, q95 = np.quantile(values, [0.05, 0.50, 0.95])
        return {"p05": float(q05), "p50": float(q50), "p95": float(q95)}

    return {
        "status": "conditional_on_observed_oos_days",
        "method": "resample complete OOS sessions with replacement",
        "observed_sessions": int(len(daily)),
        "simulations": int(simulations),
        "horizon_sessions": int(horizon_sessions),
        "terminal_pnl": percentiles(terminal),
        "probability_terminal_loss": float((terminal < 0).mean()),
        "max_drawdown": percentiles(max_drawdown),
    }


def _calendar_event_dates(all_sessions: Sequence[str]) -> dict[str, set[str]]:
    sessions = sorted(date.fromisoformat(str(value)) for value in set(all_sessions))
    opex_days: set[str] = set()
    opex_weeks: set[str] = set()
    month_end_fridays: set[str] = set()
    for year, month in sorted({(value.year, value.month) for value in sessions}):
        month_sessions = [value for value in sessions if (value.year, value.month) == (year, month)]
        friday_dates = [
            date(year, month, day)
            for week in calendar.monthcalendar(year, month)
            if (day := week[calendar.FRIDAY])
        ]
        third_friday = friday_dates[2]
        opex_week_start = third_friday - timedelta(days=4)
        opex_week_sessions = [
            value for value in month_sessions if opex_week_start <= value <= third_friday
        ]
        if opex_week_sessions:
            # If Friday is a market holiday, the last observed session in the
            # standard expiration week is used as the monthly OPEX session.
            opex_days.add(max(opex_week_sessions).isoformat())
            opex_weeks.update(value.isoformat() for value in opex_week_sessions)
        last_friday = friday_dates[-1]
        if last_friday in month_sessions:
            month_end_fridays.add(last_friday.isoformat())
    return {
        "opex_day": opex_days,
        "opex_week": opex_weeks,
        "month_end_friday": month_end_fridays,
    }


def _masked_pnl_summary(frame: pd.DataFrame, signals: np.ndarray,
                        mask: np.ndarray,
                        points_column: str = "mnq_points_60m") -> dict[str, Any]:
    selected = frame.loc[mask]
    return _mnq_pnl_summary(
        pd.to_numeric(selected[points_column], errors="coerce").to_numpy(dtype=float),
        np.asarray(signals, dtype=int)[mask],
        selected["date"].astype(str).to_numpy(),
    )


def _monthly_report(frame: pd.DataFrame, signals: np.ndarray, mask: np.ndarray,
                    all_sessions: Sequence[str],
                    points_column: str = "mnq_points_60m") -> dict[str, Any]:
    months = sorted({str(value)[:7] for value in all_sessions})
    results: dict[str, dict[str, Any]] = {}
    dates = frame["date"].astype(str)
    for month in months:
        month_mask = mask & dates.str.startswith(month).to_numpy()
        summary = _masked_pnl_summary(frame, signals, month_mask, points_column)
        summary["boundary_partial_month"] = month in {months[0], months[-1]}
        results[month] = summary
    complete = [value for value in results.values() if not value["boundary_partial_month"]]
    positive = sum(value["net_pnl"] > 0 for value in complete)
    if len(complete) < 3:
        assessment = "insufficient_complete_months"
    elif positive == len(complete) and all(value["pf"] > 1.0 for value in complete):
        assessment = "positive_in_every_complete_month"
    elif positive / len(complete) >= 0.75:
        assessment = "mostly_positive_but_not_every_month"
    else:
        assessment = "not_monthly_stable"
    return {
        "months": results,
        "complete_months": len(complete),
        "positive_complete_months": positive,
        "positive_complete_month_ratio": positive / len(complete) if complete else None,
        "all_complete_months_positive": bool(complete) and positive == len(complete),
        "pnl_per_complete_month": (
            float(np.mean([value["net_pnl"] for value in complete])) if complete else None
        ),
        "median_pnl_per_complete_month": (
            float(np.median([value["net_pnl"] for value in complete])) if complete else None
        ),
        "assessment": assessment,
    }


def _event_exclusion_assessment(all_summary: dict[str, Any], event_summary: dict[str, Any],
                                excluded_summary: dict[str, Any]) -> str:
    if event_summary["trades"] < 20:
        return "insufficient_event_trades"
    if (event_summary["net_pnl"] < 0 and event_summary["pf"] < 0.8
            and excluded_summary["pf"] > all_summary["pf"]):
        return "candidate_for_exclusion"
    if event_summary["pf"] >= 1.0:
        return "no_current_evidence_to_exclude"
    return "inconclusive"


def _calendar_report(frame: pd.DataFrame, signals: np.ndarray, mask: np.ndarray,
                     all_sessions: Sequence[str],
                     points_column: str = "mnq_points_60m") -> dict[str, Any]:
    event_dates = _calendar_event_dates(all_sessions)
    dates = frame["date"].astype(str)
    all_summary = _masked_pnl_summary(frame, signals, mask, points_column)
    result: dict[str, Any] = {}
    for event, values in event_dates.items():
        event_mask = mask & dates.isin(values).to_numpy()
        excluded_mask = mask & ~dates.isin(values).to_numpy()
        event_summary = _masked_pnl_summary(frame, signals, event_mask, points_column)
        excluded_summary = _masked_pnl_summary(frame, signals, excluded_mask, points_column)
        result[event] = {
            "event_only": event_summary,
            "excluding_event": excluded_summary,
            "delta_net_pnl_if_excluded": excluded_summary["net_pnl"] - all_summary["net_pnl"],
            "assessment": _event_exclusion_assessment(
                all_summary, event_summary, excluded_summary,
            ),
        }
    combined_dates = event_dates["opex_week"] | event_dates["month_end_friday"]
    combined_mask = mask & dates.isin(combined_dates).to_numpy()
    combined_excluded = mask & ~dates.isin(combined_dates).to_numpy()
    result["opex_week_or_month_end_friday"] = {
        "event_only": _masked_pnl_summary(frame, signals, combined_mask, points_column),
        "excluding_event": _masked_pnl_summary(
            frame, signals, combined_excluded, points_column,
        ),
    }
    return result


def _strategy_summary(frame: pd.DataFrame, signal: np.ndarray, mask: np.ndarray,
                      all_sessions: Sequence[str], simulations: int,
                      monte_carlo_horizon: int,
                      points_column: str = "mnq_points_60m") -> dict[str, Any]:
    result = _masked_pnl_summary(frame, signal, mask, points_column)
    result["monte_carlo"] = _day_block_monte_carlo(
        result, all_sessions, simulations, monte_carlo_horizon,
    )
    result["monthly"] = _monthly_report(
        frame, signal, mask, all_sessions, points_column,
    )
    result["calendar_events"] = _calendar_report(
        frame, signal, mask, all_sessions, points_column,
    )
    return result


def _annotate_strategy_signals(oos: pd.DataFrame,
                               probability_threshold: float) -> pd.DataFrame:
    ordered = oos.sort_values(["date", "as_of"]).copy()
    ordered["previous_prediction"] = ordered.groupby("date")["wf_prediction"].shift(1)
    ordered["previous_confidence"] = ordered.groupby("date")["wf_confidence"].shift(1)
    predicted = ordered["wf_prediction"].to_numpy(dtype=int)
    confidence = ordered["wf_confidence"].to_numpy(dtype=float)
    wall_direction = np.sign(np.nan_to_num(
        ordered["oi_peak1_bps"].to_numpy(dtype=float), nan=0.0,
    )).astype(int)
    previous_wall_direction = np.sign(np.nan_to_num(
        ordered.groupby("date")["oi_peak1_bps"].shift(1).to_numpy(dtype=float), nan=0.0,
    )).astype(int)
    confident = (predicted != 0) & (confidence >= probability_threshold)
    stable_model = (
        confident
        & (ordered["previous_prediction"].to_numpy(dtype=float) == predicted)
        & (ordered["previous_confidence"].to_numpy(dtype=float) >= probability_threshold)
    )
    stable_wall = (
        (wall_direction != 0)
        & (wall_direction == previous_wall_direction)
        & (ordered["oi_peak1_share"].to_numpy(dtype=float) >= 0.15)
        & (np.abs(ordered["oi_side_imbalance"].to_numpy(dtype=float)) >= 0.10)
        & (np.sign(ordered["oi_side_imbalance"].to_numpy(dtype=float)) == wall_direction)
        & (ordered["oi_peak_count_20pct"].to_numpy(dtype=float) <= 2)
    )
    ordered["primary_model_confidence_signal"] = np.where(confident, predicted, 0)
    ordered["side_model_stable_signal"] = np.where(stable_model, predicted, 0)
    ordered["single_wall_direction"] = wall_direction
    ordered["primary_single_wall_stable_signal"] = np.where(stable_wall, wall_direction, 0)
    return ordered


def _hourly_reports(ordered: pd.DataFrame, probability_threshold: float,
                    simulations: int, monte_carlo_horizon: int) -> dict[str, Any]:
    hourly = ordered[ordered["as_of_et"].astype(str).str.endswith(":00")].copy()
    all_sessions = sorted(ordered["date"].astype(str).unique())
    predicted = hourly["wf_prediction"].to_numpy(dtype=int)
    confidence = hourly["wf_confidence"].to_numpy(dtype=float)
    forced = np.where(hourly["wf_p_long"] >= hourly["wf_p_short"], 1, -1)
    all_rows = np.ones(len(hourly), dtype=bool)
    confident_signal = hourly["primary_model_confidence_signal"].to_numpy(dtype=int)
    confident = confident_signal != 0
    stable_model_signal = hourly["side_model_stable_signal"].to_numpy(dtype=int)
    stable_model = stable_model_signal != 0
    stable_wall_signal = hourly["primary_single_wall_stable_signal"].to_numpy(dtype=int)
    stable_wall = stable_wall_signal != 0

    primary = {
        "model_confidence": _strategy_summary(
            hourly, confident_signal, confident, all_sessions, simulations, monte_carlo_horizon,
        ),
        "single_wall_stable": _strategy_summary(
            hourly, stable_wall_signal, stable_wall, all_sessions, simulations, monte_carlo_horizon,
        ),
    }
    side_models = {
        "forced_every_hour": _strategy_summary(
            hourly, forced, all_rows, all_sessions, simulations, monte_carlo_horizon,
        ),
        "model_stable_two_observations": _strategy_summary(
            hourly, stable_model_signal, stable_model, all_sessions, simulations, monte_carlo_horizon,
        ),
        "always_long_baseline": _strategy_summary(
            hourly, np.ones(len(hourly), dtype=int), all_rows,
            all_sessions, simulations, monte_carlo_horizon,
        ),
        "always_short_baseline": _strategy_summary(
            hourly, -np.ones(len(hourly), dtype=int), all_rows,
            all_sessions, simulations, monte_carlo_horizon,
        ),
    }
    primary["model_confidence"]["fixed_rule"] = {
        "prediction_must_be_non_neutral": True,
        "minimum_class_probability": probability_threshold,
    }
    primary["single_wall_stable"]["fixed_rule"] = {
        "confirmation": "largest OI-depth peak remains on same side for two observations",
        "minimum_peak_share": 0.15,
        "minimum_absolute_side_imbalance": 0.10,
        "maximum_large_peak_count": 2,
        "mass_direction_must_agree": True,
    }
    return {"primary_strategies": primary, "side_models": side_models}


def run_walk_forward(data_root: Path, min_train_sessions: int = 20,
                     probability_threshold: float = 0.55,
                     simulations: int = 10_000,
                     monte_carlo_horizon: int = 20) -> dict[str, Any]:
    dataset_path = data_root / "option_wall_ml_dataset.csv.gz"
    if not dataset_path.is_file():
        raise RuntimeError(f"dataset missing: {dataset_path}; run build first")
    frame = pd.read_csv(dataset_path, compression="gzip")
    features = _feature_columns(frame, include_price=False)
    boundaries = walk_forward_boundaries(frame["date"].astype(str).unique(), min_train_sessions)
    predictions: list[pd.DataFrame] = []
    selected_algorithms: list[str] = []

    for historical_days, test_day in boundaries:
        validation_count = max(3, int(round(len(historical_days) * 0.20)))
        fit_days = historical_days[:-validation_count]
        validation_days = historical_days[-validation_count:]
        inner_train = frame[frame["date"].astype(str).isin(fit_days)]
        inner_validation = frame[frame["date"].astype(str).isin(validation_days)]
        scores: dict[str, float] = {}
        for name, candidate in _candidates().items():
            candidate.fit(inner_train[features], inner_train["label_60m"].astype(int))
            scores[name] = float(balanced_accuracy_score(
                inner_validation["label_60m"].astype(int),
                candidate.predict(inner_validation[features]),
            ))
        winner_name = max(scores, key=scores.get)
        selected_algorithms.append(winner_name)
        history = frame[frame["date"].astype(str).isin(historical_days)]
        winner = _candidates()[winner_name]
        winner.fit(history[features], history["label_60m"].astype(int))
        current = frame[frame["date"].astype(str) == test_day].copy()
        probabilities = np.asarray(winner.predict_proba(current[features]), dtype=float)
        classes = np.asarray(winner.classes_, dtype=int)
        class_index = {int(value): idx for idx, value in enumerate(classes)}
        current["wf_prediction"] = winner.predict(current[features]).astype(int)
        current["wf_confidence"] = probabilities.max(axis=1)
        current["wf_p_long"] = probabilities[:, class_index[1]]
        current["wf_p_short"] = probabilities[:, class_index[-1]]
        current["wf_selected_algorithm"] = winner_name
        predictions.append(current)

    oos = pd.concat(predictions, ignore_index=True).sort_values(["date", "as_of"])
    oos = _annotate_strategy_signals(oos, probability_threshold)
    signal_path = data_root / "option_wall_walk_forward_signals.csv.gz"
    _atomic_csv(signal_path, oos)
    y = oos["label_60m"].astype(int).to_numpy()
    predicted = oos["wf_prediction"].astype(int).to_numpy()
    report: dict[str, Any] = {
        "status": "provisional_research_only",
        "method": "expanding walk-forward; model selected only from prior-day inner validation",
        "dataset": str(dataset_path),
        "signals": str(signal_path),
        "feature_set": "wall_only",
        "features": len(features),
        "minimum_training_sessions": int(min_train_sessions),
        "oos_first_session": str(oos["date"].min()),
        "oos_last_session": str(oos["date"].max()),
        "oos_sessions": int(oos["date"].nunique()),
        "oos_rows": int(len(oos)),
        "selected_algorithm_counts": dict(Counter(selected_algorithms)),
        "classification": {
            "accuracy": float(accuracy_score(y, predicted)),
            "balanced_accuracy": float(balanced_accuracy_score(y, predicted)),
            "f1_macro": float(f1_score(y, predicted, average="macro", zero_division=0)),
        },
        "hourly_test_mnq": _hourly_reports(
            oos, probability_threshold, simulations, monte_carlo_horizon,
        ),
        "warnings": [
            "Monte Carlo resamples observed OOS days; it estimates path dispersion, not predictive validity.",
            "A small OOS day count cannot establish a live-trading edge.",
            "The stable-wall rule thresholds are fixed research assumptions, not optimized parameters.",
        ],
    }
    _atomic_json(data_root / "option_wall_walk_forward_report.json", report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--min-train-sessions", type=int, default=20)
    parser.add_argument("--probability-threshold", type=float, default=0.55)
    parser.add_argument("--simulations", type=int, default=10_000)
    parser.add_argument("--monte-carlo-horizon", type=int, default=20)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = run_walk_forward(
        args.data_root, args.min_train_sessions, args.probability_threshold,
        args.simulations, args.monte_carlo_horizon,
    )
    print(json.dumps(report, indent=2, allow_nan=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
