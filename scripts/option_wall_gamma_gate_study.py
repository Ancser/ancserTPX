"""Apply causal Gamma-regime gates to the existing option-wall ML entries.

The entry predictions are not retrained and the exit parameters are not
re-optimized here.  Each gate sees only features available at the entry
snapshot.  This keeps the experiment focused on whether Gamma context removes
bad model trades instead of silently creating a new strategy.
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
from scripts.option_wall_exit_grid_study import (
    CURRENT_LONG,
    CURRENT_SHORT,
    _basic_metrics,
    _metrics_with_months,
    _prepare_entries,
    _side_pnl_matrix,
)
from scripts.option_wall_ml_study import DEFAULT_DATA_ROOT, _atomic_csv, _atomic_json, _iso
from scripts.option_wall_sltp_study import _pi_atr_levels, _simulate_ohlc_exit


BEST_FULL_PERIOD_LONG = (3.0, 10.0)
BEST_FULL_PERIOD_SHORT = (1.5, None)

FEATURE_COLUMNS = (
    "dashboard_oi_net_gex_signed_log",
    "oi_gamma_flip_bps",
    "dashboard_vol_net_gex_signed_log",
    "dashboard_vol_gamma_flip_proxy_bps",
    "oi_call_wall_bps",
    "oi_put_wall_bps",
    "dashboard_vol_call_wall_bps",
    "dashboard_vol_put_wall_bps",
    "article_price_vwap_distance_bps",
    "article_price_return_15m_bps",
)


def _gamma_state(net_gex: pd.Series, flip_bps: pd.Series) -> np.ndarray:
    """Return +1 positive, -1 negative, 0 unconfirmed/disagreeing."""
    net = pd.to_numeric(net_gex, errors="coerce").to_numpy(dtype=float)
    flip = pd.to_numeric(flip_bps, errors="coerce").to_numpy(dtype=float)
    finite = np.isfinite(net) & np.isfinite(flip)
    return np.where(
        finite & (net > 0) & (flip <= 0), 1,
        np.where(finite & (net < 0) & (flip > 0), -1, 0),
    ).astype(int)


def _gate_masks(frame: pd.DataFrame) -> dict[str, np.ndarray]:
    """Fixed, theory-led gates; no outcome columns are used."""
    direction = frame["direction"].to_numpy(dtype=int)
    oi_state = frame["oi_gamma_state"].to_numpy(dtype=int)
    vol_state = frame["volume_gamma_state"].to_numpy(dtype=int)
    vwap_distance = pd.to_numeric(
        frame["article_price_vwap_distance_bps"], errors="coerce",
    ).to_numpy(dtype=float)
    return_15m = pd.to_numeric(
        frame["article_price_return_15m_bps"], errors="coerce",
    ).to_numpy(dtype=float)
    call_wall = pd.to_numeric(
        frame["dashboard_vol_call_wall_bps"], errors="coerce",
    ).to_numpy(dtype=float)
    put_wall = pd.to_numeric(
        frame["dashboard_vol_put_wall_bps"], errors="coerce",
    ).to_numpy(dtype=float)

    gamma_consensus = (oi_state != 0) & (oi_state == vol_state)
    wall_room = ((direction == 1) & (call_wall > 0)) | (
        (direction == -1) & (put_wall < 0)
    )
    volume_article_alignment = (
        ((vol_state == 1) & (direction * vwap_distance < 0))
        | ((vol_state == -1) & (direction * return_15m > 0))
    )
    consensus_article_alignment = gamma_consensus & (
        ((vol_state == 1) & (direction * vwap_distance < 0))
        | ((vol_state == -1) & (direction * return_15m > 0))
    )
    return {
        "baseline": np.ones(len(frame), dtype=bool),
        "volume_directional": direction == vol_state,
        "oi_directional": direction == oi_state,
        "oi_volume_directional_consensus": (
            (direction == oi_state) & (direction == vol_state)
        ),
        "oi_volume_regime_consensus": gamma_consensus,
        "volume_article_alignment": volume_article_alignment,
        "consensus_article_alignment": consensus_article_alignment,
        "volume_wall_room": wall_room,
        "volume_article_alignment_wall_room": volume_article_alignment & wall_room,
        "consensus_article_alignment_wall_room": (
            consensus_article_alignment & wall_room
        ),
    }


def _fixed_exit_pnl(
    entries: pd.DataFrame,
    paths: list[pd.DataFrame],
    long_config: tuple[float, float | None],
    short_config: tuple[float, float | None],
) -> np.ndarray:
    long_matrix, _ = _side_pnl_matrix(entries, paths, 1, [long_config])
    short_matrix, _ = _side_pnl_matrix(entries, paths, -1, [short_config])
    return long_matrix[0] + short_matrix[0]


def _monthly_walk_forward_pnl(
    entries: pd.DataFrame,
    paths: list[pd.DataFrame],
    selections: list[dict[str, Any]],
) -> np.ndarray:
    """Replay already-selected monthly exits without looking at test outcomes."""
    by_month = {str(row["test_month"]): row for row in selections}
    point_value = get_point_value("MNQ")
    cost = get_commission_rt("MNQ") + get_fees_rt("MNQ")
    pnl = np.full(len(entries), np.nan, dtype=float)
    for index, row in entries.iterrows():
        config = by_month.get(str(row["date"])[:7])
        if config is None:
            continue
        direction = int(row["direction"])
        prefix = "long" if direction == 1 else "short"
        sl_multiple = float(config[f"{prefix}_sl_atr"])
        raw_tp = config[f"{prefix}_tp_atr"]
        tp_multiple = None if raw_tp is None else float(raw_tp)
        sl, tp = _pi_atr_levels(
            float(row["entry_price"]), direction, float(row["atr_blend"]),
            sl_multiple, tp_multiple,
        )
        outcome = _simulate_ohlc_exit(
            paths[int(index)], direction, float(row["entry_price"]), sl, tp,
        )
        points = float(outcome["exit_price"]) - float(row["entry_price"])
        pnl[int(index)] = direction * points * point_value - cost
    return pnl


def _gate_summary(
    frame: pd.DataFrame,
    pnl: np.ndarray,
    gate: np.ndarray,
    eligible: np.ndarray,
    boundary_months: set[str],
) -> dict[str, Any]:
    finite = np.isfinite(pnl)
    selected = np.asarray(gate, dtype=bool) & np.asarray(eligible, dtype=bool) & finite
    available = np.asarray(eligible, dtype=bool) & finite
    removed = available & ~np.asarray(gate, dtype=bool)
    dates = frame["date"].astype(str).to_numpy()
    direction = frame["direction"].to_numpy(dtype=int)
    result = _metrics_with_months(pnl[selected], dates[selected], boundary_months)
    result.update({
        "eligible_trades": int(available.sum()),
        "coverage": float(selected.sum() / available.sum()) if available.any() else 0.0,
        "removed_trades": int(removed.sum()),
        "removed_baseline_net_pnl": float(pnl[removed].sum()),
        "long": _basic_metrics(pnl[selected & (direction == 1)]),
        "short": _basic_metrics(pnl[selected & (direction == -1)]),
        "additional_cost_per_trade": {
            str(extra): _basic_metrics(pnl[selected] - float(extra))
            for extra in (1, 2, 4)
        },
        "entry_session_block_bootstrap": _entry_session_block_bootstrap(
            frame, pnl, selected, available,
        ),
    })
    return result


def _entry_session_block_bootstrap(
    frame: pd.DataFrame,
    pnl: np.ndarray,
    selected: np.ndarray,
    available: np.ndarray,
    draws: int = 5_000,
    seed: int = 20_260_904,
) -> dict[str, Any]:
    """Resample base-entry sessions, preserving same-day trade clustering.

    Sessions that contained a base entry but no gate-approved trade remain as
    zero-PnL observations.  This is an iid historical-session sensitivity test,
    not protection against a future regime change.
    """
    dates = frame["date"].astype(str).to_numpy()
    sessions = np.asarray(sorted(set(dates[np.asarray(available, dtype=bool)])))
    if not len(sessions):
        return {
            "draws": int(draws), "base_entry_sessions": 0,
            "active_sessions": 0, "probability_net_positive": None,
            "net_pnl_median": None, "net_pnl_5pct": None, "net_pnl_95pct": None,
        }
    daily = pd.Series(
        np.asarray(pnl)[np.asarray(selected, dtype=bool)],
        index=dates[np.asarray(selected, dtype=bool)],
    ).groupby(level=0).sum()
    values = daily.reindex(sessions, fill_value=0.0).to_numpy(dtype=float)
    rng = np.random.default_rng(seed)
    totals = values[
        rng.integers(0, len(values), size=(int(draws), len(values)))
    ].sum(axis=1)
    return {
        "draws": int(draws),
        "base_entry_sessions": int(len(values)),
        "active_sessions": int(np.count_nonzero(values)),
        "probability_net_positive": float((totals > 0).mean()),
        "net_pnl_median": float(np.median(totals)),
        "net_pnl_5pct": float(np.quantile(totals, 0.05)),
        "net_pnl_95pct": float(np.quantile(totals, 0.95)),
        "largest_session_pnl": float(values.max(initial=0.0)),
        "net_pnl_without_largest_session": float(values.sum() - values.max(initial=0.0)),
    }


def _assert_reference(
    observed: np.ndarray,
    expected: float,
    label: str,
) -> None:
    value = float(np.nansum(observed))
    if not math.isclose(value, float(expected), abs_tol=1e-7):
        raise RuntimeError(
            f"{label} replay mismatch: observed={value}, expected={expected}"
        )


def run_gamma_gate_study(data_root: Path = DEFAULT_DATA_ROOT) -> dict[str, Any]:
    entries, paths, skipped = _prepare_entries(data_root)
    dataset = pd.read_csv(data_root / "option_wall_ml_dataset.csv.gz", compression="gzip")
    dataset["as_of"] = pd.to_datetime(dataset["as_of"], utc=True)
    features = dataset[["as_of", *FEATURE_COLUMNS]].copy()
    frame = entries.merge(features, on="as_of", how="left", validate="one_to_one")
    if frame[list(FEATURE_COLUMNS)].drop(columns=["oi_gamma_flip_bps"]).isna().any().any():
        raise RuntimeError("required Gamma gate features are missing")
    frame["oi_gamma_state"] = _gamma_state(
        frame["dashboard_oi_net_gex_signed_log"], frame["oi_gamma_flip_bps"],
    )
    frame["volume_gamma_state"] = _gamma_state(
        frame["dashboard_vol_net_gex_signed_log"],
        frame["dashboard_vol_gamma_flip_proxy_bps"],
    )
    gates = _gate_masks(frame)

    exit_report_path = data_root / "option_wall_exit_grid_report.json"
    exit_report = json.loads(exit_report_path.read_text(encoding="utf-8"))
    best_reference = exit_report["full_period_leaderboards"]["highest_net_pnl"][0]
    monthly_reference = exit_report["monthly_plateau_walk_forward"]

    best_pnl = _fixed_exit_pnl(
        entries, paths, BEST_FULL_PERIOD_LONG, BEST_FULL_PERIOD_SHORT,
    )
    current_pnl = _fixed_exit_pnl(entries, paths, CURRENT_LONG, CURRENT_SHORT)
    monthly_pnl = _monthly_walk_forward_pnl(
        entries, paths, monthly_reference["selected_by_month"],
    )
    _assert_reference(best_pnl, best_reference["net_pnl"], "best full-period exit")
    _assert_reference(
        monthly_pnl, monthly_reference["aggregate"]["net_pnl"],
        "monthly walk-forward exit",
    )

    all_sessions = sorted(dataset["date"].astype(str).unique())
    split_index = int(math.floor(len(all_sessions) * 0.70))
    holdout_sessions = set(all_sessions[split_index:])
    holdout = frame["date"].astype(str).isin(holdout_sessions).to_numpy(dtype=bool)
    all_eligible = np.ones(len(frame), dtype=bool)
    ordered_months = sorted(frame["date"].astype(str).str[:7].unique())
    all_boundaries = {ordered_months[0], ordered_months[-1]}
    holdout_months = sorted(frame.loc[holdout, "date"].astype(str).str[:7].unique())
    holdout_boundaries = {holdout_months[0], holdout_months[-1]}
    monthly_months = sorted({str(row["test_month"]) for row in monthly_reference["selected_by_month"]})
    monthly_boundaries = {monthly_months[-1]}

    policies = {
        "best_full_period_exit_replay": best_pnl,
        "current_asymmetric_3r": current_pnl,
        "monthly_walk_forward_exit": monthly_pnl,
    }
    results: dict[str, Any] = {}
    for policy, pnl in policies.items():
        eligible = np.isfinite(pnl) if policy == "monthly_walk_forward_exit" else all_eligible
        boundaries = monthly_boundaries if policy == "monthly_walk_forward_exit" else all_boundaries
        results[policy] = {
            name: {
                "all": _gate_summary(frame, pnl, mask, eligible, boundaries),
                "holdout_last_30pct_sessions": _gate_summary(
                    frame, pnl, mask, eligible & holdout, holdout_boundaries,
                ),
            }
            for name, mask in gates.items()
        }

    trade_output = frame[[
        "date", "as_of", "as_of_et", "direction", "entry_price", "atr_blend",
        *FEATURE_COLUMNS, "oi_gamma_state", "volume_gamma_state",
    ]].copy()
    for name, mask in gates.items():
        trade_output[f"gate_{name}"] = mask
    trade_output["pnl_best_full_period_exit_replay"] = best_pnl
    trade_output["pnl_current_asymmetric_3r"] = current_pnl
    trade_output["pnl_monthly_walk_forward_exit"] = monthly_pnl
    trade_output["holdout_last_30pct_sessions"] = holdout
    trades_path = data_root / "option_wall_gamma_gate_trades.csv.gz"
    _atomic_csv(trades_path, trade_output)

    report = {
        "status": "exploratory_research_only",
        "created_at": _iso(pd.Timestamp.now(tz="UTC")),
        "entry_strategy": "unchanged primary_model_confidence walk-forward signals",
        "data": {
            "entries": int(len(frame)),
            "long_entries": int((frame["direction"] == 1).sum()),
            "short_entries": int((frame["direction"] == -1).sum()),
            "entry_path_skipped": int(skipped),
            "first_session": str(frame["date"].min()),
            "last_session": str(frame["date"].max()),
            "holdout_sessions": int(len(holdout_sessions)),
            "holdout_first_session": min(holdout_sessions),
            "oi_volume_regime_agreement_entries": int((
                (frame["oi_gamma_state"] != 0)
                & (frame["oi_gamma_state"] == frame["volume_gamma_state"])
            ).sum()),
        },
        "definitions": {
            "positive_gamma": "net GEX > 0 and spot at/above the Gamma-flip proxy",
            "negative_gamma": "net GEX < 0 and spot below the Gamma-flip proxy",
            "directional": "long requires positive Gamma; short requires negative Gamma",
            "article_alignment": (
                "positive Gamma requires model direction back toward VWAP; negative Gamma "
                "requires model direction to agree with the causal 15-minute price return"
            ),
            "wall_room": "long requires Volume Call Wall above spot; short requires Volume Put Wall below spot",
        },
        "exit_replays": {
            "best_full_period_exit_replay": {
                "long": {"sl_atr": 3.0, "tp_atr": 10.0},
                "short": {"sl_atr": 1.5, "tp_atr": None},
                "warning": "exit parameters were selected on the full evaluation period",
            },
            "current_asymmetric_3r": {
                "long": {"sl_atr": CURRENT_LONG[0], "tp_atr": CURRENT_LONG[1]},
                "short": {"sl_atr": CURRENT_SHORT[0], "tp_atr": CURRENT_SHORT[1]},
            },
            "monthly_walk_forward_exit": {
                "source": str(exit_report_path),
                "test_months": monthly_reference["test_months"],
            },
        },
        "results": results,
        "trades_file": str(trades_path),
        "warnings": [
            "Gamma gates are evaluated after their design and are exploratory, not untouched forward evidence.",
            "Volume GEX uses cumulative unsigned option volume and is not observed dealer inventory.",
            "The option state is hourly; intrahour Gamma flips are not observed.",
            "The $6k exit replay is in-sample for exit selection; monthly walk-forward exit results are the honest comparison.",
        ],
    }
    report_path = data_root / "option_wall_gamma_gate_report.json"
    _atomic_json(report_path, report)
    return {**report, "report_file": str(report_path)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    args = parser.parse_args()
    print(json.dumps(run_gamma_gate_study(args.data_root), indent=2, default=str))


if __name__ == "__main__":
    main()
