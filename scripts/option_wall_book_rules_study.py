"""Causal tests for the remaining Options Wall book rule families.

The study keeps the five existing entry models and their exits unchanged while
testing theory-led abstention rules.  It also evaluates three standalone book
ideas (Deep-V exhaustion, late-day pinning, and wall-break continuation) on
non-overlapping hourly observations.  No rule can route an order.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.db.models import get_commission_rt, get_fees_rt, get_point_value
from scripts.option_wall_all_models_gate_exit_study import (
    STRICT_GATE,
    _summary,
    _trade_pnl,
)
from scripts.option_wall_article_walk_forward import _add_calendar_feature_columns
from scripts.option_wall_gamma_gate_study import _gamma_state, _gate_masks
from scripts.option_wall_ml_study import DEFAULT_DATA_ROOT, _atomic_csv, _atomic_json, _iso


POLICIES = ("time_only", "pi_asymmetric_sl_only")
RULE_SELECTION_CANDIDATES = (
    STRICT_GATE,
    "strict_wall_dominance_1p00",
    "strict_low_tension_0p20",
    "strict_directional_single_oi_peak",
    "strict_target_wall_not_collapsing",
    "strict_excluding_opex_week_and_month_end_friday",
    "strict_excluding_15_et",
)


def _finite(series: pd.Series) -> np.ndarray:
    return pd.to_numeric(series, errors="coerce").to_numpy(dtype=float)


def augment_book_features(dataset: pd.DataFrame) -> pd.DataFrame:
    """Add current/past-only state changes and wall lifecycle proxies."""
    frame = _add_calendar_feature_columns(dataset).sort_values(["date", "as_of"]).copy()
    grouped = frame.groupby("date", sort=False)

    for column in (
        "dashboard_oi_net_gex_signed_log",
        "dashboard_vol_net_gex_signed_log",
        "article_iv_atm_pct",
    ):
        frame[f"book_{column}_delta"] = frame[column] - grouped[column].shift(1)
    frame["book_oi_abs_gex_delta"] = (
        frame["dashboard_oi_net_gex_signed_log"].abs()
        - grouped["dashboard_oi_net_gex_signed_log"].shift(1).abs()
    )
    frame["book_vol_abs_gex_delta"] = (
        frame["dashboard_vol_net_gex_signed_log"].abs()
        - grouped["dashboard_vol_net_gex_signed_log"].shift(1).abs()
    )

    spot = pd.to_numeric(frame["qqq_spot"], errors="coerce")
    for side in ("call", "put"):
        bps_column = f"dashboard_vol_{side}_wall_bps"
        level_column = f"book_vol_{side}_wall_level"
        frame[level_column] = spot * (
            1.0 + pd.to_numeric(frame[bps_column], errors="coerce") / 10_000.0
        )
        frame[f"book_previous_vol_{side}_wall_level"] = grouped[level_column].shift(1)

    call_share = pd.to_numeric(
        frame["dashboard_vol_call_wall_share"], errors="coerce",
    )
    put_share = pd.to_numeric(
        frame["dashboard_vol_put_wall_share"], errors="coerce",
    )
    total_share = call_share + put_share
    frame["book_wall_tension"] = np.where(
        total_share > 0, (call_share - put_share).abs() / total_share, np.nan,
    )

    oi_state = _gamma_state(
        frame["dashboard_oi_net_gex_signed_log"], frame["oi_gamma_flip_bps"],
    )
    vol_state = _gamma_state(
        frame["dashboard_vol_net_gex_signed_log"],
        frame["dashboard_vol_gamma_flip_proxy_bps"],
    )
    frame["oi_gamma_state"] = oi_state
    frame["volume_gamma_state"] = vol_state
    frame["book_gamma_consensus"] = (oi_state != 0) & (oi_state == vol_state)

    previous_call = _finite(frame["book_previous_vol_call_wall_level"])
    previous_put = _finite(frame["book_previous_vol_put_wall_level"])
    call_bps = _finite(frame["dashboard_vol_call_wall_bps"])
    put_bps = _finite(frame["dashboard_vol_put_wall_bps"])
    return_15m = _finite(frame["article_price_return_15m_bps"])
    current_spot = spot.to_numpy(dtype=float)
    long_break = (
        np.isfinite(previous_call) & (current_spot > previous_call)
        & (call_bps > 0) & (return_15m > 0)
    )
    short_break = (
        np.isfinite(previous_put) & (current_spot < previous_put)
        & (put_bps < 0) & (return_15m < 0)
    )
    frame["book_wall_break_signal"] = np.where(
        long_break & ~short_break, 1,
        np.where(short_break & ~long_break, -1, 0),
    ).astype(int)
    return frame


def _directed_columns(frame: pd.DataFrame) -> dict[str, np.ndarray]:
    direction = frame["direction"].to_numpy(dtype=int)
    long = direction == 1
    call_share = _finite(frame["dashboard_vol_call_wall_share"])
    put_share = _finite(frame["dashboard_vol_put_wall_share"])
    call_delta = _finite(frame["article_dashboard_vol_call_wall_share_delta"])
    put_delta = _finite(frame["article_dashboard_vol_put_wall_share_delta"])
    call_migration = _finite(
        frame["article_dashboard_vol_call_wall_migration_bps_per_hour"],
    )
    put_migration = _finite(
        frame["article_dashboard_vol_put_wall_migration_bps_per_hour"],
    )
    return {
        "target_share": np.where(long, call_share, put_share),
        "opposing_share": np.where(long, put_share, call_share),
        "target_share_delta": np.where(long, call_delta, put_delta),
        "target_migration": np.where(long, call_migration, put_migration),
        "oriented_call_migration": direction * call_migration,
        "oriented_put_migration": direction * put_migration,
    }


def book_rule_masks(frame: pd.DataFrame) -> dict[str, np.ndarray]:
    """Predeclared rule sensitivities; none uses an outcome column."""
    gates = _gate_masks(frame)
    strict = gates[STRICT_GATE]
    direction = frame["direction"].to_numpy(dtype=int)
    directed = _directed_columns(frame)
    target_share = directed["target_share"]
    opposing_share = directed["opposing_share"]
    tension = _finite(frame["book_wall_tension"])
    oi_peak_count = _finite(frame["oi_peak_count_20pct"])
    oi_peak_share = _finite(frame["oi_peak1_share"])
    oi_peak_direction = np.sign(_finite(frame["oi_peak1_bps"])).astype(int)
    full_gvp = _finite(frame["article_gvp_full_alignment"])
    break_signal = frame["book_wall_break_signal"].to_numpy(dtype=int)
    target_not_collapsing = directed["target_share_delta"] >= -0.02
    target_migrating = direction * directed["target_migration"] > 0
    both_walls_migrating = (
        (directed["oriented_call_migration"] > 0)
        & (directed["oriented_put_migration"] > 0)
    )
    clean_event = ~(
        (frame["article_event_opex_week"].to_numpy(dtype=float) > 0)
        | (frame["article_event_month_end_friday"].to_numpy(dtype=float) > 0)
    )
    return {
        "baseline": np.ones(len(frame), dtype=bool),
        STRICT_GATE: strict,
        "strict_wall_dominance_1p00": strict & (target_share > opposing_share),
        "strict_wall_dominance_1p25": strict & (target_share > opposing_share * 1.25),
        "strict_wall_dominance_1p50": strict & (target_share > opposing_share * 1.50),
        "strict_low_tension_0p20": strict & (tension >= 0.20),
        "strict_directional_single_oi_peak": (
            strict & (oi_peak_count <= 2) & (oi_peak_share >= 0.10)
            & (oi_peak_direction == direction)
        ),
        "strict_full_gvp_alignment": strict & (full_gvp == direction),
        "strict_target_wall_migrating": strict & target_migrating,
        "strict_both_volume_walls_migrating": strict & both_walls_migrating,
        "strict_target_wall_not_collapsing": strict & target_not_collapsing,
        "strict_wall_break_with_replacement": strict & (break_signal == direction),
        "strict_excluding_opex_week_and_month_end_friday": strict & clean_event,
        "strict_excluding_15_et": strict & (frame["as_of_et"].astype(str) != "15:00"),
        "strict_10_to_13_et": strict & frame["as_of_et"].astype(str).isin(
            ["10:00", "11:00", "12:00", "13:00"]
        ).to_numpy(),
        "strict_dominance_not_collapsing": (
            strict & (target_share > opposing_share * 1.25) & target_not_collapsing
        ),
    }


def _standalone_pnl(frame: pd.DataFrame, signal: np.ndarray) -> np.ndarray:
    points = _finite(frame["mnq_points_30m"])
    cost = get_commission_rt("MNQ") + get_fees_rt("MNQ")
    return signal * points * get_point_value("MNQ") - cost


def standalone_book_signals(frame: pd.DataFrame) -> dict[str, np.ndarray]:
    """Standalone causal signals for rule families not represented by a base model."""
    volume_state = frame["volume_gamma_state"].to_numpy(dtype=int)
    oi_state = frame["oi_gamma_state"].to_numpy(dtype=int)
    negative_consensus = (volume_state == -1) & (oi_state == -1)
    positive_consensus = (volume_state == 1) & (oi_state == 1)
    gex_contracting = _finite(frame["book_vol_abs_gex_delta"]) < 0
    iv_falling = _finite(frame["book_article_iv_atm_pct_delta"]) < 0
    recent_put_breach = _finite(frame["article_price_below_put_wall_fraction_5m"]) > 0
    price_recovering = _finite(frame["article_price_return_5m_bps"]) > 0
    put_wall_below = _finite(frame["dashboard_vol_put_wall_bps"]) < 0
    deep_v_core = negative_consensus & gex_contracting & recent_put_breach & price_recovering

    peak_bps = _finite(frame["oi_peak1_bps"])
    pin_direction = np.sign(peak_bps).astype(int)
    pin_base = (
        positive_consensus & (frame["as_of_et"].astype(str).to_numpy() == "15:00")
        & (_finite(frame["oi_peak1_share"]) >= 0.10) & (pin_direction != 0)
    )
    break_signal = frame["book_wall_break_signal"].to_numpy(dtype=int)
    return {
        "deep_v_gex_price": np.where(
            deep_v_core & put_wall_below, 1, 0,
        ).astype(int),
        "deep_v_gex_iv_price": np.where(
            deep_v_core & iv_falling & put_wall_below, 1, 0,
        ).astype(int),
        "wall_break_replacement_any_gamma": break_signal,
        "wall_break_replacement_negative_gamma": np.where(
            negative_consensus, break_signal, 0,
        ).astype(int),
        "pin_20bps_positive_gamma": np.where(
            pin_base & (np.abs(peak_bps) <= 20), pin_direction, 0,
        ).astype(int),
        "pin_30bps_positive_gamma": np.where(
            pin_base & (np.abs(peak_bps) <= 30), pin_direction, 0,
        ).astype(int),
        "pin_50bps_positive_gamma": np.where(
            pin_base & (np.abs(peak_bps) <= 50), pin_direction, 0,
        ).astype(int),
    }


def walk_forward_rule_selection(
    frame: pd.DataFrame,
    masks: dict[str, np.ndarray],
) -> tuple[pd.DataFrame, pd.DataFrame, list[dict[str, Any]]]:
    """Select a fixed rule from prior months, then apply it to the next month."""
    ordered = frame.sort_values(["date", "as_of"]).copy()
    ordered["month"] = ordered["date"].astype(str).str[:7]
    mask_columns: dict[str, str] = {}
    for name in RULE_SELECTION_CANDIDATES:
        column = f"_rule_{name}"
        ordered[column] = np.asarray(masks[name], dtype=bool)[ordered.index]
        mask_columns[name] = column
    months = sorted(ordered["month"].unique())
    selected_rows: list[pd.DataFrame] = []
    baseline_rows: list[pd.DataFrame] = []
    selections: list[dict[str, Any]] = []
    for test_month in months:
        prior = [month for month in months if month < test_month]
        if len(prior) < 2:
            continue
        history = ordered[ordered["month"].isin(prior)]
        test = ordered[ordered["month"] == test_month]
        scores: dict[str, dict[str, Any]] = {}
        for name, column in mask_columns.items():
            accepted = history[history[column]]
            active_months = accepted["month"].nunique()
            if len(accepted) < 10 or active_months < 2:
                continue
            pnl = accepted["pnl"].to_numpy(dtype=float)
            equity = np.cumsum(pnl)
            peak = np.maximum.accumulate(np.r_[0.0, equity])[1:]
            drawdown = float((equity - peak).min())
            net = float(pnl.sum())
            scores[name] = {
                "trades": int(len(accepted)), "net_pnl": net,
                "max_drawdown": drawdown,
                "score": net / max(abs(drawdown), 100.0),
            }
        if not scores:
            continue
        winner = max(scores, key=lambda name: (scores[name]["score"], scores[name]["trades"]))
        accepted_test = test[test[mask_columns[winner]]].copy()
        accepted_test["selected_rule"] = winner
        selected_rows.append(accepted_test)
        strict_test = test[test[mask_columns[STRICT_GATE]]].copy()
        baseline_rows.append(strict_test)
        selections.append({
            "test_month": test_month,
            "history_last_month": prior[-1],
            "selected_rule": winner,
            "test_base_signals": int(len(test)),
            "test_selected_signals": int(len(accepted_test)),
            "history_scores": scores,
        })
    empty = ordered.iloc[0:0].copy()
    return (
        pd.concat(selected_rows, ignore_index=True) if selected_rows else empty,
        pd.concat(baseline_rows, ignore_index=True) if baseline_rows else empty,
        selections,
    )


def run_book_rules_study(data_root: Path = DEFAULT_DATA_ROOT) -> dict[str, Any]:
    dataset_path = data_root / "option_wall_ml_dataset.csv.gz"
    trades_path = data_root / "option_wall_sltp_trades.csv.gz"
    if not dataset_path.is_file() or not trades_path.is_file():
        raise RuntimeError("option-wall dataset and SL/TP trade artifacts are required")
    dataset = pd.read_csv(dataset_path, compression="gzip")
    trades = pd.read_csv(trades_path, compression="gzip")
    dataset["as_of"] = pd.to_datetime(dataset["as_of"], utc=True)
    dataset["close_at"] = pd.to_datetime(dataset["close_at"], utc=True)
    trades["as_of"] = pd.to_datetime(trades["as_of"], utc=True)
    augmented = augment_book_features(dataset)

    all_sessions = sorted(augmented["date"].astype(str).unique())
    split = int(math.floor(len(all_sessions) * 0.70))
    holdout_sessions = set(all_sessions[split:])
    months = sorted(augmented["date"].astype(str).str[:7].unique())
    boundary_months = {months[0], months[-1]}

    causal_columns = [
        column for column in augmented.columns
        if column not in {
            "future_30m_at", "future_60m_at", "qqq_future_return_bps_30m",
            "qqq_future_max_up_bps_30m", "qqq_future_max_down_bps_30m",
            "qqq_future_range_bps_30m", "qqq_future_directional_efficiency_30m",
            "qqq_future_return_bps_60m", "qqq_future_return_bps_close",
            "label_30m", "label_60m", "label_close", "target_expansion_30m",
            "target_wall_first_30m", "target_wall_hit_minutes_30m",
            "mnq_exit_30m", "mnq_exit_60m", "mnq_exit_close",
            "mnq_points_30m", "mnq_points_60m", "mnq_points_close",
        }
    ]
    context = augmented[causal_columns].copy()
    frame = trades[trades["policy"].isin(POLICIES)].merge(
        context, on="as_of", how="left", validate="many_to_one",
        suffixes=("", "_feature"),
    )
    frame["pnl"] = _trade_pnl(frame)
    results: dict[str, Any] = {}
    walk_forward_results: dict[str, Any] = {}
    output_rows: list[pd.DataFrame] = []
    for strategy in sorted(frame["strategy"].unique()):
        results[strategy] = {}
        walk_forward_results[strategy] = {}
        for policy in POLICIES:
            current = frame[(frame["strategy"] == strategy) & (frame["policy"] == policy)].copy()
            current = current.reset_index(drop=True)
            masks = book_rule_masks(current)
            results[strategy][policy] = {}
            for name, mask in masks.items():
                selected = current[np.asarray(mask, dtype=bool)].copy()
                holdout = selected[selected["date"].astype(str).isin(holdout_sessions)]
                summary = _summary(selected, selected["pnl"].to_numpy(), boundary_months)
                summary["coverage"] = len(selected) / len(current) if len(current) else 0.0
                results[strategy][policy][name] = {
                    "all": summary,
                    "holdout_last_30pct_sessions": _summary(
                        holdout, holdout["pnl"].to_numpy(), {months[-1]},
                    ),
                }
                if len(selected):
                    rows = selected[[
                        "date", "as_of", "as_of_et", "strategy", "policy",
                        "direction", "pnl", "volume_gamma_state", "oi_gamma_state",
                    ]].copy()
                    rows["book_rule"] = name
                    output_rows.append(rows)
            selected_wf, baseline_wf, selections = walk_forward_rule_selection(current, masks)
            wf_months = sorted(set(
                selected_wf["month"].astype(str) if len(selected_wf) else []
            ) | set(baseline_wf["month"].astype(str) if len(baseline_wf) else []))
            wf_boundary = {wf_months[-1]} if wf_months else set()
            walk_forward_results[strategy][policy] = {
                "selected_rules": _summary(
                    selected_wf, selected_wf["pnl"].to_numpy(), wf_boundary,
                ),
                "fixed_strict_same_outer_months": _summary(
                    baseline_wf, baseline_wf["pnl"].to_numpy(), wf_boundary,
                ),
                "selections": selections,
            }

    hourly = augmented[
        augmented["as_of_et"].astype(str).str.endswith(":00")
        & (augmented["date"].astype(str) >= str(trades["date"].min()))
    ].copy()
    standalone: dict[str, Any] = {}
    standalone_rows: list[pd.DataFrame] = []
    for name, signal in standalone_book_signals(hourly).items():
        active = (signal != 0) & np.isfinite(_finite(hourly["mnq_points_30m"]))
        current = hourly.loc[active].copy()
        if len(current):
            current["direction"] = signal[active]
            current["pnl"] = _standalone_pnl(current, signal[active])
        else:
            current["direction"] = pd.Series(dtype=int)
            current["pnl"] = pd.Series(dtype=float)
        holdout = current[current["date"].astype(str).isin(holdout_sessions)]
        standalone[name] = {
            "all": _summary(current, current["pnl"].to_numpy(), boundary_months),
            "holdout_last_30pct_sessions": _summary(
                holdout, holdout["pnl"].to_numpy(), {months[-1]},
            ),
        }
        if len(current):
            rows = current[["date", "as_of", "as_of_et", "direction", "pnl"]].copy()
            rows["book_rule"] = name
            standalone_rows.append(rows)

    output_path = data_root / "option_wall_book_rules_trades.csv.gz"
    if output_rows or standalone_rows:
        _atomic_csv(output_path, pd.concat(output_rows + standalone_rows, ignore_index=True))
    report = {
        "status": "exploratory_research_only",
        "created_at": _iso(pd.Timestamp.now(tz="UTC")),
        "data": {
            "sessions": len(all_sessions), "first_session": min(all_sessions),
            "last_session": max(all_sessions), "holdout_sessions": len(holdout_sessions),
        },
        "fixed_entry_rule_filters": results,
        "monthly_walk_forward_rule_selection": walk_forward_results,
        "standalone_book_rules": standalone,
        "rules": {
            "deep_v": (
                "negative OI/Volume Gamma consensus, Volume-GEX magnitude contracts, "
                "recent Put-Wall breach, positive trailing 5m return; strict variant also "
                "requires ATM IV to fall"
            ),
            "wall_break": (
                "spot crosses the prior snapshot wall, current snapshot has a new wall "
                "on the same side, and trailing 15m price agrees"
            ),
            "pin": (
                "15:00 ET, positive OI/Volume Gamma, dominant OI peak within 20/30/50 bps"
            ),
        },
        "warnings": [
            "Thresholds are theory-led sensitivities evaluated on the same historical period.",
            "The last-30-percent result is temporal but not untouched because the rule family was designed afterward.",
            "Hourly option volume cannot prove a five-minute dynamic Volume-Wall lifecycle.",
        ],
        "trades_file": str(output_path),
    }
    report_path = data_root / "option_wall_book_rules_report.json"
    _atomic_json(report_path, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    args = parser.parse_args()
    report = run_book_rules_study(args.data_root)
    print(json.dumps({
        "status": report["status"],
        "report": str(args.data_root / "option_wall_book_rules_report.json"),
    }, indent=2))


if __name__ == "__main__":
    main()
