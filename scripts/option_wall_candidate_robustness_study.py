"""Consolidated robustness report for option-wall research candidates.

Candidates come from fixed strict filters, causal monthly rule selection,
nested meta-filters, intrahour OI monitoring, and standalone book rules.  This
report does not select a winner; it applies the same costs, calendar, temporal
segments, concentration, and session-block bootstrap diagnostics to all of
them so small-sample ideas cannot hide behind a high PF.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import zlib
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.option_wall_all_models_gate_exit_study import STRICT_GATE, _concentration, _summary, _trade_pnl
from scripts.option_wall_book_rules_study import (
    _standalone_pnl,
    augment_book_features,
    book_rule_masks,
    standalone_book_signals,
    walk_forward_rule_selection,
)
from scripts.option_wall_exit_grid_study import _basic_metrics
from scripts.option_wall_ml_study import DEFAULT_DATA_ROOT, _atomic_csv, _atomic_json, _iso


EXTRA_COSTS = (1.0, 2.0, 4.0, 8.0)
BOOTSTRAP_DRAWS = 10_000


def _candidate_frame(frame: pd.DataFrame) -> pd.DataFrame:
    columns = ["date", "as_of", "as_of_et", "direction", "pnl"]
    result = frame[columns].copy()
    result["date"] = result["date"].astype(str)
    result["as_of"] = pd.to_datetime(result["as_of"], utc=True)
    return result.sort_values("as_of").reset_index(drop=True)


def _session_bootstrap(
    frame: pd.DataFrame,
    available_sessions: list[str],
    seed: int,
) -> dict[str, Any]:
    if frame.empty:
        return {"draws": BOOTSTRAP_DRAWS, "sessions": 0, "probability_net_positive": None}
    start, end = frame["date"].min(), frame["date"].max()
    sessions = [day for day in available_sessions if start <= day <= end]
    daily = frame.groupby("date")["pnl"].sum().reindex(sessions, fill_value=0.0)
    values = daily.to_numpy(dtype=float)
    rng = np.random.default_rng(seed)
    samples = values[rng.integers(0, len(values), size=(BOOTSTRAP_DRAWS, len(values)))]
    totals = samples.sum(axis=1)
    equity = np.cumsum(samples, axis=1)
    peaks = np.maximum.accumulate(np.c_[np.zeros(BOOTSTRAP_DRAWS), equity], axis=1)[:, 1:]
    drawdowns = np.min(equity - peaks, axis=1)
    return {
        "draws": BOOTSTRAP_DRAWS,
        "sessions": int(len(values)),
        "active_sessions": int(np.count_nonzero(values)),
        "probability_net_positive": float((totals > 0).mean()),
        "net_pnl_p05": float(np.quantile(totals, 0.05)),
        "net_pnl_p50": float(np.quantile(totals, 0.50)),
        "net_pnl_p95": float(np.quantile(totals, 0.95)),
        "max_drawdown_p05": float(np.quantile(drawdowns, 0.05)),
        "max_drawdown_p50": float(np.quantile(drawdowns, 0.50)),
        "max_drawdown_p95": float(np.quantile(drawdowns, 0.95)),
    }


def _temporal_thirds(frame: pd.DataFrame, available_sessions: list[str]) -> dict[str, Any]:
    if frame.empty:
        return {}
    sessions = [
        day for day in available_sessions
        if frame["date"].min() <= day <= frame["date"].max()
    ]
    chunks = np.array_split(np.asarray(sessions, dtype=str), 3)
    return {
        f"third_{index + 1}": _basic_metrics(
            frame[frame["date"].isin(set(chunk))]["pnl"].to_numpy(dtype=float)
        )
        for index, chunk in enumerate(chunks)
    }


def _robust_summary(
    name: str,
    frame: pd.DataFrame,
    available_sessions: list[str],
    event_by_date: pd.DataFrame,
) -> dict[str, Any]:
    current = _candidate_frame(frame).merge(
        event_by_date, on="date", how="left", validate="many_to_one",
    )
    result = _summary(current, current["pnl"].to_numpy(), {"2026-09"})
    result["average_trades_per_active_month"] = (
        len(current) / current["date"].str[:7].nunique() if len(current) else None
    )
    result["additional_cost_per_trade"] = {
        f"{extra:g}": _basic_metrics(current["pnl"].to_numpy(dtype=float) - extra)
        for extra in EXTRA_COSTS
    }
    result["temporal_thirds"] = _temporal_thirds(current, available_sessions)
    candidate_sessions = [
        day for day in available_sessions
        if len(current) and current["date"].min() <= day <= current["date"].max()
    ]
    holdout_start = int(math.floor(len(candidate_sessions) * 0.70))
    holdout_sessions = set(candidate_sessions[holdout_start:])
    holdout = current[current["date"].isin(holdout_sessions)]
    result["last_30pct_sessions"] = _basic_metrics(
        holdout["pnl"].to_numpy(dtype=float)
    )
    event = (
        current["article_event_opex_week"].fillna(0).astype(bool)
        | current["article_event_month_end_friday"].fillna(0).astype(bool)
    )
    result["calendar"] = {
        "opex_week_or_month_end_friday": _basic_metrics(
            current.loc[event, "pnl"].to_numpy(dtype=float)
        ),
        "excluding_opex_week_and_month_end_friday": _basic_metrics(
            current.loc[~event, "pnl"].to_numpy(dtype=float)
        ),
    }
    monthly = current.assign(month=current["date"].str[:7]).groupby("month")["pnl"].sum()
    if len(monthly):
        best_month = str(monthly.idxmax())
        worst_month = str(monthly.idxmin())
        result["month_concentration"] = {
            "best_month": best_month, "best_month_pnl": float(monthly.max()),
            "worst_month": worst_month, "worst_month_pnl": float(monthly.min()),
            "net_without_best_month": float(monthly.sum() - monthly.max()),
            "net_without_worst_month": float(monthly.sum() - monthly.min()),
        }
    result["session_block_bootstrap"] = _session_bootstrap(
        current, available_sessions, 20260905 + zlib.crc32(name.encode("utf-8")),
    )
    return result


def _base_candidates(
    data_root: Path,
    augmented: pd.DataFrame,
) -> dict[str, pd.DataFrame]:
    trades = pd.read_csv(data_root / "option_wall_sltp_trades.csv.gz", compression="gzip")
    trades["as_of"] = pd.to_datetime(trades["as_of"], utc=True)
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
    candidates: dict[str, pd.DataFrame] = {}
    for strategy, prefix in (
        ("primary_model_confidence", "primary"),
        ("side_article_state", "article"),
        ("side_regime_direction", "regime"),
        ("side_target_confirmed", "target"),
    ):
        current = base[base["strategy"] == strategy].copy().reset_index(drop=True)
        masks = book_rule_masks(current)
        candidates[f"{prefix}_strict_pi_stop"] = current[masks[STRICT_GATE]].copy()
        candidates[f"{prefix}_strict_not_collapsing_pi_stop"] = current[
            masks["strict_target_wall_not_collapsing"]
        ].copy()
        if prefix == "target":
            candidates["target_strict_directional_single_oi_peak"] = current[
                masks["strict_directional_single_oi_peak"]
            ].copy()
        selected, _, _ = walk_forward_rule_selection(current, masks)
        candidates[f"{prefix}_monthly_rule_selection"] = selected
    return candidates


def run_candidate_robustness_study(data_root: Path = DEFAULT_DATA_ROOT) -> dict[str, Any]:
    dataset = pd.read_csv(data_root / "option_wall_ml_dataset.csv.gz", compression="gzip")
    dataset["as_of"] = pd.to_datetime(dataset["as_of"], utc=True)
    augmented = augment_book_features(dataset)
    candidates = _base_candidates(data_root, augmented)
    for source_name in (
        "primary_strict_pi_stop",
        "article_strict_pi_stop",
        "article_strict_not_collapsing_pi_stop",
    ):
        source = candidates[source_name]
        candidates[f"{source_name}_long_only"] = source[source["direction"] == 1].copy()
        candidates[f"{source_name}_short_only"] = source[source["direction"] == -1].copy()

    primary = candidates["primary_strict_pi_stop"].copy()
    article = candidates["article_strict_pi_stop"].copy()
    article_key = article[["as_of", "direction"]].drop_duplicates()
    confirmed = primary.merge(
        article_key.assign(article_confirmation=True),
        on=["as_of", "direction"], how="left", validate="many_to_one",
    )
    has_confirmation = confirmed["article_confirmation"].eq(True)
    candidates["ensemble_primary_confirmed_by_article"] = confirmed[
        has_confirmation
    ].copy()
    candidates["ensemble_primary_short_plus_confirmed_long"] = confirmed[
        (confirmed["direction"] == -1)
        | has_confirmation
    ].copy()
    occupied = set(primary["as_of"])
    article_long_extra = article[
        (article["direction"] == 1) & ~article["as_of"].isin(occupied)
    ].copy()
    candidates["ensemble_primary_plus_nonoverlap_article_long"] = pd.concat(
        [primary, article_long_extra], ignore_index=True,
    ).sort_values("as_of")
    article_not_collapsing = candidates[
        "article_strict_not_collapsing_pi_stop"
    ]
    article_not_collapsing_long_extra = article_not_collapsing[
        (article_not_collapsing["direction"] == 1)
        & ~article_not_collapsing["as_of"].isin(occupied)
    ].copy()
    candidates[
        "ensemble_primary_plus_nonoverlap_article_notcollapse_long"
    ] = pd.concat(
        [primary, article_not_collapsing_long_extra], ignore_index=True,
    ).sort_values("as_of")

    hybrid = pd.read_csv(data_root / "option_wall_hybrid_exit_trades.csv.gz", compression="gzip")
    hybrid["as_of"] = pd.to_datetime(hybrid["as_of"], utc=True)
    candidates["article_strict_wall_trail_1p5atr"] = hybrid[
        (hybrid["strategy"] == "side_article_state")
        & (hybrid["variant"] == "trail_1p5atr_after_wall")
        & hybrid["strict_gate"].astype(bool)
    ]

    intrahour = pd.read_csv(data_root / "option_wall_intrahour_oi_trades.csv.gz", compression="gzip")
    intrahour["as_of"] = pd.to_datetime(intrahour["as_of"], utc=True)
    candidates["primary_intrahour_5m_adverse_wall_lag1"] = intrahour[
        (intrahour["check_minutes"] == 5)
        & (intrahour["decision_latency_minutes"] == 1)
        & (intrahour["exit_rule"] == "exit_if_target_moved_adverse_5bps")
    ]

    meta = pd.read_csv(data_root / "option_wall_meta_filter_predictions.csv.gz", compression="gzip")
    meta["as_of"] = pd.to_datetime(meta["as_of"], utc=True)
    candidates["primary_strict_meta_p65"] = meta[
        (meta["strategy"] == "primary_model_confidence")
        & (meta["cohort"] == "after_fixed_strict_gate")
        & (meta["meta_probability"] >= 0.65)
    ]
    candidates["article_strict_meta_p50"] = meta[
        (meta["strategy"] == "side_article_state")
        & (meta["cohort"] == "after_fixed_strict_gate")
        & (meta["meta_probability"] >= 0.50)
    ]

    hourly = augmented[
        augmented["as_of_et"].astype(str).str.endswith(":00")
        & (augmented["date"].astype(str) >= "2025-12-03")
    ].copy()
    for rule_name in (
        "deep_v_gex_iv_price", "wall_break_replacement_negative_gamma",
        "pin_20bps_positive_gamma",
    ):
        signal = standalone_book_signals(hourly)[rule_name]
        active = (signal != 0) & np.isfinite(pd.to_numeric(
            hourly["mnq_points_30m"], errors="coerce",
        ).to_numpy(dtype=float))
        current = hourly.loc[active].copy()
        current["direction"] = signal[active]
        current["pnl"] = _standalone_pnl(current, signal[active])
        candidates[rule_name] = current

    event_by_date = augmented.groupby("date", as_index=False)[[
        "article_event_opex_week", "article_event_month_end_friday",
    ]].first()
    event_by_date["date"] = event_by_date["date"].astype(str)
    sessions = sorted(augmented["date"].astype(str).unique())
    summaries = {
        name: _robust_summary(name, frame, sessions, event_by_date)
        for name, frame in candidates.items()
    }
    rows: list[pd.DataFrame] = []
    for name, frame in candidates.items():
        current = _candidate_frame(frame)
        current["candidate"] = name
        rows.append(current)
    output_path = data_root / "option_wall_candidate_robustness_trades.csv.gz"
    _atomic_csv(output_path, pd.concat(rows, ignore_index=True))
    report = {
        "status": "exploratory_research_only",
        "created_at": _iso(pd.Timestamp.now(tz="UTC")),
        "candidate_count": len(candidates),
        "extra_costs_per_trade": list(EXTRA_COSTS),
        "bootstrap_draws": BOOTSTRAP_DRAWS,
        "results": summaries,
        "trades_file": str(output_path),
        "warnings": [
            "Historical session bootstrap assumes iid reuse of observed regimes.",
            "Candidates were proposed after inspecting the same broad data period; bootstrap does not correct selection bias.",
            "Candidates with fewer than 30 trades remain idea-level regardless of PF.",
        ],
    }
    report_path = data_root / "option_wall_candidate_robustness_report.json"
    _atomic_json(report_path, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    args = parser.parse_args()
    report = run_candidate_robustness_study(args.data_root)
    print(json.dumps({
        "status": report["status"], "candidate_count": report["candidate_count"],
        "report": str(args.data_root / "option_wall_candidate_robustness_report.json"),
    }, indent=2))


if __name__ == "__main__":
    main()
