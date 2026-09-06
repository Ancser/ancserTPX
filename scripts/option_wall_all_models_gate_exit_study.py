"""Cross-model Gamma-gate and wall-exit portability study.

This module does not retrain entries or tune stop distances.  It applies the
same causal, theory-led Gamma gate to every existing option-wall signal and
replays the already-defined exits.  A small supplementary replay exits 5/10
bps before the frozen target wall, matching the book's "near the wall" idea
without using a future wall snapshot.

Research only: there are no order-routing imports.
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
from scripts.option_wall_exit_grid_study import _basic_metrics, _metrics_with_months
from scripts.option_wall_gamma_gate_study import FEATURE_COLUMNS, _gamma_state, _gate_masks
from scripts.option_wall_ml_study import DEFAULT_DATA_ROOT, _atomic_csv, _atomic_json, _iso
from scripts.option_wall_sltp_study import (
    _path_for_row,
    _read_mnq,
    _round_tick,
    _simulate_ohlc_exit,
)


STRICT_GATE = "consensus_article_alignment_wall_room"
WIDE_GATE = "volume_article_alignment_wall_room"
PROXIMITY_BPS = (5.0, 10.0)


def _trade_pnl(frame: pd.DataFrame) -> np.ndarray:
    """Convert raw MNQ exit displacement to one-contract net P&L."""
    direction = pd.to_numeric(frame["direction"], errors="raise").to_numpy(dtype=int)
    points = pd.to_numeric(frame["market_points"], errors="coerce").to_numpy(dtype=float)
    cost = get_commission_rt("MNQ") + get_fees_rt("MNQ")
    return direction * points * get_point_value("MNQ") - cost


def _proximity_target(
    entry: float,
    exact_target: float,
    target_wall_bps: float,
    proximity_bps: float,
) -> float | None:
    """Move a frozen wall target toward entry by a QQQ basis-point buffer."""
    distance_bps = abs(float(target_wall_bps))
    if not all(math.isfinite(value) for value in (entry, exact_target, distance_bps)):
        return None
    remaining_bps = distance_bps - float(proximity_bps)
    if remaining_bps <= 0 or exact_target == entry:
        return None
    target = _round_tick(entry + (exact_target - entry) * remaining_bps / distance_bps)
    if (exact_target > entry and not entry < target <= exact_target) or (
        exact_target < entry and not exact_target <= target < entry
    ):
        return None
    return float(target)


def _concentration(frame: pd.DataFrame, pnl: np.ndarray) -> dict[str, Any]:
    """Measure whether aggregate profit is dominated by one trade/session."""
    values = np.asarray(pnl, dtype=float)
    if not len(values):
        return {
            "largest_trade_pnl": None,
            "largest_session_pnl": None,
            "net_without_largest_trade": 0.0,
            "net_without_largest_session": 0.0,
        }
    dates = frame["date"].astype(str).to_numpy()
    daily = pd.Series(values, index=dates).groupby(level=0).sum()
    largest_trade = float(values.max())
    largest_session = float(daily.max())
    return {
        "largest_trade_pnl": largest_trade,
        "largest_session_pnl": largest_session,
        "net_without_largest_trade": float(values.sum() - largest_trade),
        "net_without_largest_session": float(values.sum() - largest_session),
        "active_sessions": int(len(daily)),
        "multi_trade_sessions": int((pd.Series(dates).value_counts() > 1).sum()),
        "maximum_trades_in_one_session": int(pd.Series(dates).value_counts().max()),
    }


def _summary(
    frame: pd.DataFrame,
    pnl: np.ndarray,
    boundary_months: set[str],
) -> dict[str, Any]:
    result = _metrics_with_months(
        np.asarray(pnl, dtype=float), frame["date"].astype(str).to_numpy(), boundary_months,
    )
    direction = frame["direction"].to_numpy(dtype=int)
    result["long"] = _basic_metrics(np.asarray(pnl)[direction == 1])
    result["short"] = _basic_metrics(np.asarray(pnl)[direction == -1])
    result["concentration"] = _concentration(frame, pnl)
    if "exit_reason" in frame:
        result["exit_reasons"] = {
            str(key): int(value)
            for key, value in frame["exit_reason"].astype(str).value_counts().items()
        }
        result["average_bars_held"] = float(frame["bars_held"].mean())
    return result


def _attach_context(trades: pd.DataFrame, dataset: pd.DataFrame) -> pd.DataFrame:
    context_columns = ["as_of", "close_at", *FEATURE_COLUMNS]
    context = dataset[context_columns].copy()
    if context["as_of"].duplicated().any():
        raise RuntimeError("option-wall dataset has duplicate as_of rows")
    frame = trades.merge(context, on="as_of", how="left", validate="many_to_one")
    required = [column for column in FEATURE_COLUMNS if column != "oi_gamma_flip_bps"]
    if frame[required].isna().any().any():
        raise RuntimeError("required Gamma context is missing from SL/TP trades")
    frame["oi_gamma_state"] = _gamma_state(
        frame["dashboard_oi_net_gex_signed_log"], frame["oi_gamma_flip_bps"],
    )
    frame["volume_gamma_state"] = _gamma_state(
        frame["dashboard_vol_net_gex_signed_log"],
        frame["dashboard_vol_gamma_flip_proxy_bps"],
    )
    return frame


def _replay_wall_proximity(
    frame: pd.DataFrame,
    mnq: pd.DataFrame,
    proximity_bps: float,
) -> pd.DataFrame:
    """Replay wall targets with a fixed buffer and the original PI stop."""
    rows: list[dict[str, Any]] = []
    cost = get_commission_rt("MNQ") + get_fees_rt("MNQ")
    point_value = get_point_value("MNQ")
    for _, trade in frame.iterrows():
        target = _proximity_target(
            float(trade["entry_price"]), float(trade["tp_price"]),
            float(trade["target_wall_bps"]), proximity_bps,
        )
        if target is None:
            continue
        source = pd.Series({
            "as_of": trade["as_of"],
            "close_at": trade["close_at"],
            "mnq_entry": trade["entry_price"],
        })
        path = _path_for_row(mnq, source, int(trade["horizon_minutes"]))
        if path.empty:
            continue
        outcome = _simulate_ohlc_exit(
            path, int(trade["direction"]), float(trade["entry_price"]),
            float(trade["sl_price"]), target,
        )
        points = float(outcome["exit_price"]) - float(trade["entry_price"])
        row = trade.to_dict()
        row.update({
            "proximity_bps": float(proximity_bps),
            "buffered_tp_price": target,
            "buffered_exit_price": float(outcome["exit_price"]),
            "original_wall_pnl": float(trade["pnl"]),
            "original_wall_exit_reason": str(trade["exit_reason"]),
            "original_wall_bars_held": int(trade["bars_held"]),
            "exit_reason": str(outcome["exit_reason"]),
            "bars_held": int(outcome["bars_held"]),
            "pnl": int(trade["direction"]) * points * point_value - cost,
        })
        rows.append(row)
    return pd.DataFrame(rows)


def _original_wall_view(frame: pd.DataFrame) -> pd.DataFrame:
    """Restore original exit metadata for a matched-eligibility comparison."""
    result = frame.copy()
    result["exit_reason"] = result["original_wall_exit_reason"]
    result["bars_held"] = result["original_wall_bars_held"]
    return result


def run_all_models_gate_exit_study(
    data_root: Path = DEFAULT_DATA_ROOT,
) -> dict[str, Any]:
    trades_path = data_root / "option_wall_sltp_trades.csv.gz"
    dataset_path = data_root / "option_wall_ml_dataset.csv.gz"
    report_path = data_root / "option_wall_sltp_report.json"
    for path in (trades_path, dataset_path, report_path):
        if not path.is_file():
            raise RuntimeError(f"required prior research artifact missing: {path}")

    trades = pd.read_csv(trades_path, compression="gzip")
    dataset = pd.read_csv(dataset_path, compression="gzip")
    trades["as_of"] = pd.to_datetime(trades["as_of"], utc=True)
    dataset["as_of"] = pd.to_datetime(dataset["as_of"], utc=True)
    dataset["close_at"] = pd.to_datetime(dataset["close_at"], utc=True)
    frame = _attach_context(trades, dataset)
    frame["pnl"] = _trade_pnl(frame)
    gates = _gate_masks(frame)
    for name, mask in gates.items():
        frame[f"gate_{name}"] = mask

    months = sorted(dataset["date"].astype(str).str[:7].unique())
    boundary_months = {months[0], months[-1]}
    sessions = sorted(dataset["date"].astype(str).unique())
    holdout_sessions = set(sessions[int(math.floor(len(sessions) * 0.70)):])

    prior = json.loads(report_path.read_text(encoding="utf-8"))
    results: dict[str, Any] = {}
    for strategy in sorted(frame["strategy"].unique()):
        strategy_frame = frame[frame["strategy"] == strategy].copy()
        results[strategy] = {"policies": {}}
        for policy in sorted(strategy_frame["policy"].unique()):
            policy_frame = strategy_frame[strategy_frame["policy"] == policy].copy()
            expected = prior["strategies"][strategy]["policies"][policy]["net_pnl"]
            observed = float(policy_frame["pnl"].sum())
            if not math.isclose(observed, float(expected), abs_tol=1e-7):
                raise RuntimeError(
                    f"prior SL/TP replay mismatch for {strategy}/{policy}: "
                    f"{observed} != {expected}"
                )
            gate_rows: dict[str, Any] = {}
            for gate_name in ("baseline", WIDE_GATE, STRICT_GATE):
                selected = policy_frame[policy_frame[f"gate_{gate_name}"]].copy()
                all_summary = _summary(selected, selected["pnl"].to_numpy(), boundary_months)
                holdout = selected[selected["date"].astype(str).isin(holdout_sessions)]
                holdout_summary = _summary(
                    holdout, holdout["pnl"].to_numpy(), {months[-1]},
                )
                all_summary["coverage_of_policy_eligible"] = (
                    len(selected) / len(policy_frame) if len(policy_frame) else 0.0
                )
                gate_rows[gate_name] = {
                    "all": all_summary,
                    "holdout_last_30pct_sessions": holdout_summary,
                }
            results[strategy]["policies"][policy] = gate_rows

    mnq = _read_mnq(data_root)
    wall_base = frame[frame["policy"] == "wall_tp_pi_stop"].copy()
    proximity_results: dict[str, Any] = {}
    proximity_output: list[pd.DataFrame] = []
    for proximity in PROXIMITY_BPS:
        replay = _replay_wall_proximity(wall_base, mnq, proximity)
        if replay.empty:
            continue
        proximity_output.append(replay)
        label = f"{proximity:g}bps_before_wall"
        proximity_results[label] = {}
        for strategy in sorted(replay["strategy"].unique()):
            current = replay[replay["strategy"] == strategy]
            positive_long = current[
                (current["direction"] == 1) & (current["volume_gamma_state"] == 1)
            ]
            proximity_results[label][strategy] = {}
            for gate_name in ("baseline", WIDE_GATE, STRICT_GATE):
                selected = current[current[f"gate_{gate_name}"]]
                selected_positive_long = positive_long[
                    positive_long[f"gate_{gate_name}"]
                ]
                exact_selected = _original_wall_view(selected)
                exact_positive_long = _original_wall_view(selected_positive_long)
                proximity_results[label][strategy][gate_name] = {
                    "all": _summary(selected, selected["pnl"].to_numpy(), boundary_months),
                    "exact_wall_same_eligible": _summary(
                        exact_selected,
                        exact_selected["original_wall_pnl"].to_numpy(),
                        boundary_months,
                    ),
                    "positive_gamma_long": _summary(
                        selected_positive_long,
                        selected_positive_long["pnl"].to_numpy(),
                        boundary_months,
                    ),
                    "positive_gamma_long_exact_wall_same_eligible": _summary(
                        exact_positive_long,
                        exact_positive_long["original_wall_pnl"].to_numpy(),
                        boundary_months,
                    ),
                }

    proximity_path = data_root / "option_wall_wall_proximity_trades.csv.gz"
    if proximity_output:
        _atomic_csv(proximity_path, pd.concat(proximity_output, ignore_index=True))

    output_trades = frame[[
        "date", "as_of", "as_of_et", "strategy", "policy", "horizon_minutes",
        "direction", "exit_reason", "bars_held", "market_points", "pnl",
        "oi_gamma_state", "volume_gamma_state",
        *[f"gate_{name}" for name in gates],
    ]].copy()
    output_path = data_root / "option_wall_all_models_gate_exit_trades.csv.gz"
    _atomic_csv(output_path, output_trades)

    report: dict[str, Any] = {
        "status": "exploratory_research_only",
        "created_at": _iso(pd.Timestamp.now(tz="UTC")),
        "scope": (
            "unchanged existing walk-forward entries and unchanged existing exits; "
            "the same causal Gamma gate is transported across models"
        ),
        "strict_gate": STRICT_GATE,
        "strict_gate_definition": (
            "OI and Volume Gamma regime agree; positive Gamma follows model direction "
            "toward VWAP, negative Gamma follows the causal 15-minute move; a correctly "
            "positioned Volume target wall must remain beyond spot"
        ),
        "data": {
            "first_session": min(sessions),
            "last_session": max(sessions),
            "sessions": len(sessions),
            "holdout_sessions": len(holdout_sessions),
            "holdout_first_session": min(holdout_sessions),
            "sltp_trade_rows": int(len(frame)),
        },
        "results": results,
        "wall_proximity_exit": {
            "definition": (
                "frozen entry-snapshot target wall, moved 5/10 QQQ bps toward entry; "
                "same PI asymmetric stop and original model horizon"
            ),
            "results": proximity_results,
            "trades_file": str(proximity_path) if proximity_output else None,
        },
        "trades_file": str(output_path),
        "warnings": [
            "The gate was discovered on the primary model and is not an independent holdout there.",
            "Side models share dates and features, so transport is useful evidence but not independent market data.",
            "Wall targets are frozen at entry; dynamic wall migration is not yet an exit trigger.",
            "A five/ten-bps proximity grid is a sensitivity check, not a newly optimized live parameter.",
        ],
    }
    output_report = data_root / "option_wall_all_models_gate_exit_report.json"
    _atomic_json(output_report, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    args = parser.parse_args()
    report = run_all_models_gate_exit_study(args.data_root)
    print(json.dumps({
        "status": report["status"],
        "models": len(report["results"]),
        "report": str(args.data_root / "option_wall_all_models_gate_exit_report.json"),
    }, indent=2))


if __name__ == "__main__":
    main()
