"""Article-faithful, three-layer QQQ option-wall walk-forward research.

The legacy experiment asks one model to guess a future direction from a wall
snapshot.  This study keeps that baseline, adds four explicit feature
ablations, then separates the trading question into:

1. regime: is the next 30 minutes likely to leave a 10 bp deadband?
2. direction: conditional on expansion, which side is favoured?
3. target: does the upper or lower OI wall get touched first?

Signals are evaluated only at whole-hour observations and held for 30 minutes,
so one-MNQ research trades cannot overlap.  The module has no order-routing
imports and cannot place a live order.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd
from sklearn.dummy import DummyClassifier
from sklearn.metrics import accuracy_score, balanced_accuracy_score, f1_score

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.option_wall_ml_study import (
    ABLATION_FEATURE_SETS,
    DEFAULT_DATA_ROOT,
    _atomic_csv,
    _atomic_json,
    _iso,
    ablation_feature_columns,
)
from scripts.option_wall_walk_forward import (
    _calendar_event_dates,
    _candidates,
    _strategy_summary,
    walk_forward_boundaries,
)


def _add_calendar_feature_columns(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    dates = result["date"].astype(str)
    events = _calendar_event_dates(dates.unique())
    result["article_event_opex_day"] = dates.isin(events["opex_day"]).astype(float)
    result["article_event_opex_week"] = dates.isin(events["opex_week"]).astype(float)
    result["article_event_month_end_friday"] = dates.isin(
        events["month_end_friday"]
    ).astype(float)
    parsed = pd.to_datetime(dates, errors="coerce")
    result["article_event_friday"] = (parsed.dt.weekday == 4).astype(float)
    result["article_event_late_month"] = (parsed.dt.day >= 25).astype(float)
    return result


def _new_model(name: str):
    if name == "dummy_prior":
        return DummyClassifier(strategy="prior")
    candidates = _candidates()
    if name not in candidates:
        raise ValueError(f"unknown model: {name}")
    return candidates[name]


def _clean_target_rows(frame: pd.DataFrame, target: str) -> pd.DataFrame:
    values = pd.to_numeric(frame[target], errors="coerce")
    return frame.loc[values.notna()].copy()


def _select_algorithm(
    inner_train: pd.DataFrame,
    inner_validation: pd.DataFrame,
    features: list[str],
    target: str,
) -> tuple[str, dict[str, float]]:
    train = _clean_target_rows(inner_train, target)
    validation = _clean_target_rows(inner_validation, target)
    if train.empty:
        raise ValueError(f"no training rows for {target}")
    y_train = train[target].astype(int)
    if y_train.nunique() < 2 or validation.empty:
        return "dummy_prior", {"dummy_prior": 0.0}
    scores: dict[str, float] = {}
    for name, model in _candidates().items():
        model.fit(train[features], y_train)
        predicted = model.predict(validation[features]).astype(int)
        scores[name] = float(balanced_accuracy_score(
            validation[target].astype(int), predicted,
        ))
    return max(scores, key=scores.get), scores


def _fit_selected(
    history: pd.DataFrame,
    features: list[str],
    target: str,
    algorithm: str,
):
    rows = _clean_target_rows(history, target)
    if rows.empty:
        raise ValueError(f"no history rows for {target}")
    model = _new_model(algorithm)
    model.fit(rows[features], rows[target].astype(int))
    return model


def _probability(model, frame: pd.DataFrame, features: list[str], label: int) -> np.ndarray:
    probabilities = np.asarray(model.predict_proba(frame[features]), dtype=float)
    classes = [int(value) for value in model.classes_]
    if label not in classes:
        return np.zeros(len(frame), dtype=float)
    return probabilities[:, classes.index(label)]


def _prediction_bundle(model, frame: pd.DataFrame, features: list[str]) -> dict[str, np.ndarray]:
    probabilities = np.asarray(model.predict_proba(frame[features]), dtype=float)
    predicted = np.asarray(model.predict(frame[features]), dtype=int)
    return {
        "prediction": predicted,
        "confidence": probabilities.max(axis=1),
        "p_long": _probability(model, frame, features, 1),
        "p_short": _probability(model, frame, features, -1),
    }


def _layered_signals(
    regime_probability: np.ndarray,
    direction_prediction: np.ndarray,
    direction_confidence: np.ndarray,
    target_prediction: np.ndarray,
    target_confidence: np.ndarray,
    regime_threshold: float,
    direction_threshold: float,
    target_threshold: float,
) -> tuple[np.ndarray, np.ndarray]:
    direction_ok = (
        (regime_probability >= regime_threshold)
        & (direction_confidence >= direction_threshold)
        & np.isin(direction_prediction, [-1, 1])
    )
    regime_direction = np.where(direction_ok, direction_prediction, 0).astype(int)
    target_ok = (
        direction_ok
        & (target_prediction == direction_prediction)
        & (target_confidence >= target_threshold)
    )
    target_confirmed = np.where(target_ok, direction_prediction, 0).astype(int)
    return regime_direction, target_confirmed


def _classification_summary(y: np.ndarray, predicted: np.ndarray) -> dict[str, Any]:
    return {
        "accuracy": float(accuracy_score(y, predicted)),
        "balanced_accuracy": float(balanced_accuracy_score(y, predicted)),
        "f1_macro": float(f1_score(y, predicted, average="macro", zero_division=0)),
    }


def _direct_ablation_report(
    hourly: pd.DataFrame,
    feature_set: str,
    probability_threshold: float,
    all_sessions: Sequence[str],
    simulations: int,
    monte_carlo_horizon: int,
) -> dict[str, Any]:
    prefix = f"ablation_{feature_set}"
    predicted = hourly[f"{prefix}_prediction"].to_numpy(dtype=int)
    confidence = hourly[f"{prefix}_confidence"].to_numpy(dtype=float)
    signal = np.where(
        (predicted != 0) & (confidence >= probability_threshold), predicted, 0,
    ).astype(int)
    mask = signal != 0
    result = {
        "feature_count": int(hourly.attrs["feature_counts"][feature_set]),
        "selected_algorithm_counts": dict(
            Counter(hourly[f"{prefix}_algorithm"].astype(str))
        ),
        "classification": _classification_summary(
            hourly["label_30m"].astype(int).to_numpy(), predicted,
        ),
        "strategy": _strategy_summary(
            hourly, signal, mask, all_sessions, simulations,
            monte_carlo_horizon, points_column="mnq_points_30m",
        ),
    }
    result["fixed_rule"] = {
        "prediction_must_be_non_neutral": True,
        "minimum_class_probability": probability_threshold,
        "exit": "30 minute close; non-overlapping whole-hour observations",
    }
    return result


def run_article_walk_forward(
    data_root: Path,
    min_train_sessions: int = 20,
    probability_threshold: float = 0.55,
    regime_threshold: float = 0.55,
    target_threshold: float = 0.45,
    simulations: int = 10_000,
    monte_carlo_horizon: int = 20,
    algorithm_mode: str = "logistic",
) -> dict[str, Any]:
    dataset_path = data_root / "option_wall_ml_dataset.csv.gz"
    if not dataset_path.is_file():
        raise RuntimeError(f"dataset missing: {dataset_path}; run build first")
    frame = pd.read_csv(dataset_path, compression="gzip")
    required = {
        "label_30m", "target_expansion_30m", "target_wall_first_30m",
        "mnq_points_30m",
    }
    missing = required - set(frame.columns)
    if missing:
        raise RuntimeError(
            f"article dataset columns missing: {sorted(missing)}; rebuild feature version 2"
        )
    if algorithm_mode not in {"logistic", "hist_gradient_boosting", "select"}:
        raise ValueError(f"unknown algorithm mode: {algorithm_mode}")
    frame = _add_calendar_feature_columns(frame)
    feature_sets = {
        name: ablation_feature_columns(frame, name) for name in ABLATION_FEATURE_SETS
    }
    if any(not values for values in feature_sets.values()):
        raise RuntimeError("one or more ablation feature sets are empty")
    combined_features = feature_sets["combined_0dte"]
    boundaries = walk_forward_boundaries(frame["date"].astype(str).unique(), min_train_sessions)
    output_rows: list[pd.DataFrame] = []
    algorithm_counts: dict[str, Counter] = defaultdict(Counter)

    for boundary_index, (historical_days, test_day) in enumerate(boundaries, 1):
        validation_count = max(3, int(round(len(historical_days) * 0.20)))
        fit_days = historical_days[:-validation_count]
        validation_days = historical_days[-validation_count:]
        historical = frame[frame["date"].astype(str).isin(historical_days)]
        inner_train = frame[frame["date"].astype(str).isin(fit_days)]
        inner_validation = frame[frame["date"].astype(str).isin(validation_days)]
        current = frame[frame["date"].astype(str) == test_day].copy()

        def choose(features: list[str], target: str,
                   train_rows: pd.DataFrame = inner_train,
                   validation_rows: pd.DataFrame = inner_validation) -> str:
            if algorithm_mode != "select":
                return algorithm_mode
            selected, _scores = _select_algorithm(
                train_rows, validation_rows, features, target,
            )
            return selected

        for feature_set, features in feature_sets.items():
            algorithm = choose(features, "label_30m")
            model = _fit_selected(historical, features, "label_30m", algorithm)
            bundle = _prediction_bundle(model, current, features)
            prefix = f"ablation_{feature_set}"
            for name, values in bundle.items():
                current[f"{prefix}_{name}"] = values
            current[f"{prefix}_algorithm"] = algorithm
            algorithm_counts[prefix][algorithm] += 1

        regime_algorithm = choose(combined_features, "target_expansion_30m")
        regime_model = _fit_selected(
            historical, combined_features, "target_expansion_30m", regime_algorithm,
        )
        current["layer_regime_probability"] = _probability(
            regime_model, current, combined_features, 1,
        )
        current["layer_regime_prediction"] = regime_model.predict(
            current[combined_features]
        ).astype(int)
        current["layer_regime_algorithm"] = regime_algorithm
        algorithm_counts["layer_regime"][regime_algorithm] += 1

        direction_train = inner_train[inner_train["target_expansion_30m"] == 1]
        direction_validation = inner_validation[
            inner_validation["target_expansion_30m"] == 1
        ]
        direction_history = historical[historical["target_expansion_30m"] == 1]
        direction_algorithm = choose(
            combined_features, "label_30m", direction_train, direction_validation,
        )
        direction_model = _fit_selected(
            direction_history, combined_features, "label_30m", direction_algorithm,
        )
        direction = _prediction_bundle(direction_model, current, combined_features)
        current["layer_direction_prediction"] = direction["prediction"]
        current["layer_direction_confidence"] = direction["confidence"]
        current["layer_direction_p_long"] = direction["p_long"]
        current["layer_direction_p_short"] = direction["p_short"]
        current["layer_direction_algorithm"] = direction_algorithm
        algorithm_counts["layer_direction"][direction_algorithm] += 1

        target_algorithm = choose(combined_features, "target_wall_first_30m")
        target_model = _fit_selected(
            historical, combined_features, "target_wall_first_30m", target_algorithm,
        )
        target = _prediction_bundle(target_model, current, combined_features)
        current["layer_target_prediction"] = target["prediction"]
        current["layer_target_confidence"] = target["confidence"]
        current["layer_target_algorithm"] = target_algorithm
        algorithm_counts["layer_target"][target_algorithm] += 1
        output_rows.append(current)

        if boundary_index == 1 or boundary_index % 10 == 0 or boundary_index == len(boundaries):
            print(
                f"article walk-forward {boundary_index}/{len(boundaries)}: {test_day}",
                flush=True,
            )

    oos = pd.concat(output_rows, ignore_index=True).sort_values(["date", "as_of"])
    hourly = oos[oos["as_of_et"].astype(str).str.endswith(":00")].copy()
    hourly.attrs["feature_counts"] = {name: len(values) for name, values in feature_sets.items()}
    all_sessions = sorted(oos["date"].astype(str).unique())

    regime_direction, target_confirmed = _layered_signals(
        hourly["layer_regime_probability"].to_numpy(dtype=float),
        hourly["layer_direction_prediction"].to_numpy(dtype=int),
        hourly["layer_direction_confidence"].to_numpy(dtype=float),
        hourly["layer_target_prediction"].to_numpy(dtype=int),
        hourly["layer_target_confidence"].to_numpy(dtype=float),
        regime_threshold, probability_threshold, target_threshold,
    )
    hourly["layer_regime_direction_signal"] = regime_direction
    hourly["layer_target_confirmed_signal"] = target_confirmed

    ablations = {
        feature_set: _direct_ablation_report(
            hourly, feature_set, probability_threshold, all_sessions,
            simulations, monte_carlo_horizon,
        )
        for feature_set in ABLATION_FEATURE_SETS
    }
    layer_reports = {
        "regime_direction": _strategy_summary(
            hourly, regime_direction, regime_direction != 0, all_sessions,
            simulations, monte_carlo_horizon, points_column="mnq_points_30m",
        ),
        "target_confirmed": _strategy_summary(
            hourly, target_confirmed, target_confirmed != 0, all_sessions,
            simulations, monte_carlo_horizon, points_column="mnq_points_30m",
        ),
    }
    layer_reports["regime_direction"]["fixed_rule"] = {
        "minimum_expansion_probability": regime_threshold,
        "minimum_direction_probability": probability_threshold,
        "exit": "30 minute close",
    }
    layer_reports["target_confirmed"]["fixed_rule"] = {
        "minimum_expansion_probability": regime_threshold,
        "minimum_direction_probability": probability_threshold,
        "target_prediction_must_agree_with_direction": True,
        "minimum_target_probability": target_threshold,
        "exit": "30 minute close; wall prediction is a gate, not a synthetic fill",
    }

    expansion_true = hourly["target_expansion_30m"].astype(int).to_numpy()
    expansion_prediction = hourly["layer_regime_prediction"].astype(int).to_numpy()
    directional_mask = expansion_true == 1
    target_true = hourly["target_wall_first_30m"].astype(int).to_numpy()
    report: dict[str, Any] = {
        "status": "provisional_research_only",
        "created_at": _iso(pd.Timestamp.now(tz="UTC").to_pydatetime()),
        "method": (
            "expanding daily walk-forward; models use prior sessions only; "
            f"algorithm_mode={algorithm_mode}"
        ),
        "dataset": str(dataset_path),
        "signals": str(data_root / "option_wall_article_walk_forward_signals.csv.gz"),
        "horizon": "30 minutes",
        "algorithm_mode": algorithm_mode,
        "oos_first_session": str(oos["date"].min()),
        "oos_last_session": str(oos["date"].max()),
        "oos_sessions": int(oos["date"].nunique()),
        "oos_hourly_rows": int(len(hourly)),
        "feature_sets": {
            name: {"count": len(values), "features": values}
            for name, values in feature_sets.items()
        },
        "ablations": ablations,
        "three_layer": {
            "selected_algorithm_counts": {
                name: dict(counts) for name, counts in algorithm_counts.items()
                if name.startswith("layer_")
            },
            "classification": {
                "regime_expansion": _classification_summary(
                    expansion_true, expansion_prediction,
                ),
                "direction_given_observed_expansion": _classification_summary(
                    hourly.loc[directional_mask, "label_30m"].astype(int).to_numpy(),
                    hourly.loc[directional_mask, "layer_direction_prediction"].astype(int).to_numpy(),
                ),
                "wall_first": _classification_summary(
                    target_true, hourly["layer_target_prediction"].astype(int).to_numpy(),
                ),
            },
            "strategies": layer_reports,
        },
        "multi_expiry": {
            "status": "not_run",
            "reason": "licensed archive currently contains QQQ 0DTE contracts only",
        },
        "warnings": [
            "GEX uses the calls-positive/puts-negative convention, not observed dealer inventory.",
            "Hourly option volume cannot validate five-to-ten-minute wall migration.",
            "The wall target model gates trades; P&L uses observed MNQ 30-minute exits, never assumed wall fills.",
            "No transaction slippage is added beyond repository round-turn commission and fees.",
        ],
    }
    signal_path = data_root / "option_wall_article_walk_forward_signals.csv.gz"
    _atomic_csv(signal_path, hourly)
    _atomic_json(data_root / "option_wall_article_walk_forward_report.json", report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--min-train-sessions", type=int, default=20)
    parser.add_argument("--probability-threshold", type=float, default=0.55)
    parser.add_argument("--regime-threshold", type=float, default=0.55)
    parser.add_argument("--target-threshold", type=float, default=0.45)
    parser.add_argument("--simulations", type=int, default=10_000)
    parser.add_argument("--monte-carlo-horizon", type=int, default=20)
    parser.add_argument(
        "--algorithm-mode",
        choices=["logistic", "hist_gradient_boosting", "select"],
        default="logistic",
        help="fixed logistic isolates feature ablations; select is slower and adds model-selection noise",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = run_article_walk_forward(
        args.data_root, args.min_train_sessions, args.probability_threshold,
        args.regime_threshold, args.target_threshold, args.simulations,
        args.monte_carlo_horizon, args.algorithm_mode,
    )
    print(json.dumps(report, indent=2, allow_nan=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
