"""Nested monthly walk-forward acceptance model for option-wall entries.

The base models continue to choose direction.  This module asks a narrower
question: given only information available at entry, should that existing
signal be accepted?  Algorithm selection uses prior-month Brier score, never
test P&L.  Fixed probability thresholds are reported as sensitivities.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.option_wall_all_models_gate_exit_study import STRICT_GATE, _summary, _trade_pnl
from scripts.option_wall_book_rules_study import (
    _directed_columns,
    augment_book_features,
    book_rule_masks,
)
from scripts.option_wall_ml_study import DEFAULT_DATA_ROOT, _atomic_csv, _atomic_json, _iso


THRESHOLDS = (0.50, 0.55, 0.60, 0.65)
MIN_INNER_TRAIN = 30
MIN_VALIDATION = 5


def meta_feature_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Small, theory-led and direction-oriented feature set."""
    direction = frame["direction"].to_numpy(dtype=int)
    directed = _directed_columns(frame)
    call_migration = pd.to_numeric(
        frame["article_dashboard_vol_call_wall_migration_bps_per_hour"],
        errors="coerce",
    ).to_numpy(dtype=float)
    put_migration = pd.to_numeric(
        frame["article_dashboard_vol_put_wall_migration_bps_per_hour"],
        errors="coerce",
    ).to_numpy(dtype=float)
    call_bps = pd.to_numeric(
        frame["dashboard_vol_call_wall_bps"], errors="coerce",
    ).to_numpy(dtype=float)
    put_bps = pd.to_numeric(
        frame["dashboard_vol_put_wall_bps"], errors="coerce",
    ).to_numpy(dtype=float)
    peak_bps = pd.to_numeric(frame["oi_peak1_bps"], errors="coerce").to_numpy(dtype=float)

    result = pd.DataFrame(index=frame.index)
    result["direction"] = direction
    result["minutes_since_open"] = pd.to_numeric(frame["minutes_since_open"], errors="coerce")
    result["volume_gamma_state"] = frame["volume_gamma_state"].to_numpy(dtype=int)
    result["oi_gamma_state"] = frame["oi_gamma_state"].to_numpy(dtype=int)
    result["gamma_consensus"] = frame["book_gamma_consensus"].astype(float).to_numpy()
    result["oriented_vwap_distance"] = direction * pd.to_numeric(
        frame["article_price_vwap_distance_bps"], errors="coerce",
    ).to_numpy(dtype=float)
    result["oriented_return_5m"] = direction * pd.to_numeric(
        frame["article_price_return_5m_bps"], errors="coerce",
    ).to_numpy(dtype=float)
    result["oriented_return_15m"] = direction * pd.to_numeric(
        frame["article_price_return_15m_bps"], errors="coerce",
    ).to_numpy(dtype=float)
    result["oriented_vwap_slope"] = direction * pd.to_numeric(
        frame["article_price_vwap_slope_15m_bps"], errors="coerce",
    ).to_numpy(dtype=float)
    result["target_wall_room_bps"] = np.where(direction == 1, call_bps, -put_bps)
    result["opposing_wall_room_bps"] = np.where(direction == 1, -put_bps, call_bps)
    result["target_wall_share"] = directed["target_share"]
    result["opposing_wall_share"] = directed["opposing_share"]
    result["wall_tension"] = pd.to_numeric(frame["book_wall_tension"], errors="coerce")
    result["target_wall_share_delta"] = directed["target_share_delta"]
    result["oriented_target_wall_migration"] = direction * directed["target_migration"]
    result["oriented_call_wall_migration"] = direction * call_migration
    result["oriented_put_wall_migration"] = direction * put_migration
    result["oi_peak1_share"] = pd.to_numeric(frame["oi_peak1_share"], errors="coerce")
    result["oi_peak_count"] = pd.to_numeric(frame["oi_peak_count_20pct"], errors="coerce")
    result["oi_peak_direction_alignment"] = direction * np.sign(peak_bps)
    result["oriented_oi_side_imbalance"] = direction * pd.to_numeric(
        frame["oi_side_imbalance"], errors="coerce",
    ).to_numpy(dtype=float)
    result["oriented_vol_side_imbalance"] = direction * pd.to_numeric(
        frame["vol_side_imbalance"], errors="coerce",
    ).to_numpy(dtype=float)
    result["atm_iv"] = pd.to_numeric(frame["article_iv_atm_pct"], errors="coerce")
    result["atm_iv_delta"] = pd.to_numeric(
        frame["book_article_iv_atm_pct_delta"], errors="coerce",
    )
    result["vol_abs_gex_delta"] = pd.to_numeric(
        frame["book_vol_abs_gex_delta"], errors="coerce",
    )
    result["iv_skew"] = pd.to_numeric(
        frame["article_iv_downside_minus_upside_pct"], errors="coerce",
    )
    result["opex_week"] = pd.to_numeric(frame["article_event_opex_week"], errors="coerce")
    result["month_end_friday"] = pd.to_numeric(
        frame["article_event_month_end_friday"], errors="coerce",
    )
    return result


def _candidate_models() -> dict[str, Pipeline]:
    return {
        "logistic_c0p1": Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("scale", StandardScaler()),
            ("model", LogisticRegression(C=0.1, max_iter=2_000, random_state=20260905)),
        ]),
        "logistic_c1": Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("scale", StandardScaler()),
            ("model", LogisticRegression(C=1.0, max_iter=2_000, random_state=20260905)),
        ]),
        "hist_leaf7": Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("model", HistGradientBoostingClassifier(
                max_leaf_nodes=7, max_iter=80, learning_rate=0.05,
                l2_regularization=1.0, random_state=20260905,
            )),
        ]),
        "hist_leaf15": Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("model", HistGradientBoostingClassifier(
                max_leaf_nodes=15, max_iter=80, learning_rate=0.05,
                l2_regularization=2.0, random_state=20260905,
            )),
        ]),
    }


def _positive_probability(model: Pipeline, features: pd.DataFrame) -> np.ndarray:
    probabilities = model.predict_proba(features)
    classes = list(model.classes_)
    return probabilities[:, classes.index(1)] if 1 in classes else np.zeros(len(features))


def nested_meta_predictions(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """Outer test month; prior final month selects algorithm by Brier score."""
    ordered = frame.sort_values(["date", "as_of"]).copy()
    ordered["month"] = ordered["date"].astype(str).str[:7]
    features = meta_feature_frame(ordered)
    target = (ordered["pnl"].to_numpy(dtype=float) > 0).astype(int)
    months = sorted(ordered["month"].unique())
    outputs: list[pd.DataFrame] = []
    selections: list[dict[str, Any]] = []
    for test_month in months:
        previous = [month for month in months if month < test_month]
        if len(previous) < 2:
            continue
        validation_month = previous[-1]
        train_mask = ordered["month"].isin(previous[:-1]).to_numpy()
        validation_mask = (ordered["month"] == validation_month).to_numpy()
        history_mask = ordered["month"].isin(previous).to_numpy()
        test_mask = (ordered["month"] == test_month).to_numpy()
        if (
            train_mask.sum() < MIN_INNER_TRAIN
            or validation_mask.sum() < MIN_VALIDATION
            or not test_mask.any()
            or len(np.unique(target[train_mask])) < 2
            or len(np.unique(target[history_mask])) < 2
        ):
            continue
        scores: dict[str, float] = {}
        for name, model in _candidate_models().items():
            model.fit(features.loc[train_mask], target[train_mask])
            probability = _positive_probability(model, features.loc[validation_mask])
            scores[name] = float(np.mean((probability - target[validation_mask]) ** 2))
        selected_name = min(scores, key=scores.get)
        selected = _candidate_models()[selected_name]
        selected.fit(features.loc[history_mask], target[history_mask])
        probability = _positive_probability(selected, features.loc[test_mask])
        result = ordered.loc[test_mask].copy()
        result["meta_probability"] = probability
        result["meta_algorithm"] = selected_name
        result["test_month"] = test_month
        result["validation_month"] = validation_month
        result["history_last_date"] = ordered.loc[history_mask, "date"].astype(str).max()
        outputs.append(result)
        selections.append({
            "test_month": test_month,
            "validation_month": validation_month,
            "history_trades": int(history_mask.sum()),
            "test_trades": int(test_mask.sum()),
            "selected_algorithm": selected_name,
            "validation_brier_scores": scores,
        })
    return (
        pd.concat(outputs, ignore_index=True) if outputs else ordered.iloc[0:0].copy(),
        selections,
    )


def run_meta_filter_study(data_root: Path = DEFAULT_DATA_ROOT) -> dict[str, Any]:
    dataset = pd.read_csv(data_root / "option_wall_ml_dataset.csv.gz", compression="gzip")
    trades = pd.read_csv(data_root / "option_wall_sltp_trades.csv.gz", compression="gzip")
    dataset["as_of"] = pd.to_datetime(dataset["as_of"], utc=True)
    trades["as_of"] = pd.to_datetime(trades["as_of"], utc=True)
    augmented = augment_book_features(dataset)
    excluded = {
        "future_30m_at", "future_60m_at", "label_30m", "label_60m", "label_close",
        "target_expansion_30m", "target_wall_first_30m", "target_wall_hit_minutes_30m",
        "qqq_future_return_bps_30m", "qqq_future_max_up_bps_30m",
        "qqq_future_max_down_bps_30m", "qqq_future_range_bps_30m",
        "qqq_future_directional_efficiency_30m", "qqq_future_return_bps_60m",
        "qqq_future_return_bps_close", "mnq_exit_30m", "mnq_exit_60m",
        "mnq_exit_close", "mnq_points_30m", "mnq_points_60m", "mnq_points_close",
    }
    context = augmented[[column for column in augmented.columns if column not in excluded]]
    base = trades[trades["policy"] == "pi_asymmetric_sl_only"].merge(
        context, on="as_of", how="left", validate="many_to_one", suffixes=("", "_feature"),
    )
    base["pnl"] = _trade_pnl(base)
    sessions = sorted(augmented["date"].astype(str).unique())
    holdout_sessions = set(sessions[int(math.floor(len(sessions) * 0.70)):])

    report_results: dict[str, Any] = {}
    prediction_rows: list[pd.DataFrame] = []
    for strategy in sorted(base["strategy"].unique()):
        source = base[base["strategy"] == strategy].copy()
        strict = book_rule_masks(source)[STRICT_GATE]
        cohorts = {
            "all_base_signals": source,
            "after_fixed_strict_gate": source[np.asarray(strict, dtype=bool)].copy(),
        }
        report_results[strategy] = {}
        for cohort_name, cohort in cohorts.items():
            predictions, selections = nested_meta_predictions(cohort)
            if predictions.empty:
                report_results[strategy][cohort_name] = {
                    "status": "insufficient_nested_history", "selections": selections,
                }
                continue
            months = sorted(predictions["test_month"].astype(str).unique())
            boundary = {months[-1]} if months else set()
            baseline = _summary(predictions, predictions["pnl"].to_numpy(), boundary)
            threshold_results: dict[str, Any] = {}
            for threshold in THRESHOLDS:
                selected = predictions[predictions["meta_probability"] >= threshold].copy()
                holdout = selected[selected["date"].astype(str).isin(holdout_sessions)]
                current = _summary(selected, selected["pnl"].to_numpy(), boundary)
                current["coverage_of_outer_test"] = len(selected) / len(predictions)
                threshold_results[f"p_at_least_{threshold:.2f}"] = {
                    "all": current,
                    "holdout_last_30pct_sessions": _summary(
                        holdout, holdout["pnl"].to_numpy(), boundary,
                    ),
                }
            report_results[strategy][cohort_name] = {
                "status": "complete",
                "outer_test_baseline": baseline,
                "thresholds": threshold_results,
                "algorithm_counts": dict(Counter(
                    predictions["meta_algorithm"].astype(str)
                )),
                "selections": selections,
            }
            output = predictions[[
                "date", "as_of", "as_of_et", "strategy", "direction", "pnl",
                "meta_probability", "meta_algorithm", "test_month",
                "validation_month", "history_last_date",
            ]].copy()
            output["cohort"] = cohort_name
            prediction_rows.append(output)

    output_path = data_root / "option_wall_meta_filter_predictions.csv.gz"
    if prediction_rows:
        _atomic_csv(output_path, pd.concat(prediction_rows, ignore_index=True))
    report = {
        "status": "nested_walk_forward_research_only",
        "created_at": _iso(pd.Timestamp.now(tz="UTC")),
        "target": "whether the unchanged base signal's PI stop-only trade has positive net P&L",
        "selection": (
            "outer test month; immediately prior month validates algorithm using Brier score; "
            "all earlier months train; selected algorithm refits on all prior months"
        ),
        "fixed_thresholds": list(THRESHOLDS),
        "feature_count": len(meta_feature_frame(base.head(1)).columns),
        "results": report_results,
        "predictions_file": str(output_path),
        "warnings": [
            "The base entry models and original option dataset share the same historical period.",
            "A profitable threshold sensitivity is not a final preset until later untouched sessions confirm it.",
            "Small strict-gate cohorts may not have enough inner validation observations.",
        ],
    }
    report_path = data_root / "option_wall_meta_filter_report.json"
    _atomic_json(report_path, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    args = parser.parse_args()
    report = run_meta_filter_study(args.data_root)
    print(json.dumps({
        "status": report["status"],
        "report": str(args.data_root / "option_wall_meta_filter_report.json"),
    }, indent=2))


if __name__ == "__main__":
    main()
