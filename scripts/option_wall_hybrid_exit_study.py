"""Futures-specific wall transition exits for existing option-wall entries.

Instead of assuming an option wall must be a full take-profit, the wall can
change position risk: ignore it, move the stop, or activate an ATR trail.  The
wall and Gamma state remain frozen at entry, all transitions occur after an
observed minute bar, and ambiguous same-bar stop/wall touches resolve to stop.
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
from backend.backtest.intrabar import resolve_same_bar_exit
from scripts.option_wall_all_models_gate_exit_study import STRICT_GATE, _summary
from scripts.option_wall_book_rules_study import augment_book_features, book_rule_masks
from scripts.option_wall_ml_study import DEFAULT_DATA_ROOT, _atomic_csv, _atomic_json, _iso
from scripts.option_wall_sltp_study import _path_for_row, _read_mnq


TICK_SIZE = 0.25
VARIANTS = (
    "full_exit_at_wall",
    "ignore_wall",
    "breakeven_after_wall",
    "lock_0p5atr_after_wall",
    "trail_1atr_after_wall",
    "trail_1p5atr_after_wall",
    "positive_full_negative_ignore",
    "positive_full_negative_breakeven",
    "positive_full_negative_trail_1atr",
    "positive_breakeven_negative_trail_1atr",
)


def _stop_fill(bar: Any, direction: int, stop: float) -> float:
    return min(float(stop), float(bar.open)) if direction == 1 else max(
        float(stop), float(bar.open),
    )


def _variant_action(variant: str, gamma_state: int) -> tuple[str, float | None]:
    """Return wall action and optional ATR trail distance."""
    simple = {
        "full_exit_at_wall": ("full", None),
        "ignore_wall": ("ignore", None),
        "breakeven_after_wall": ("breakeven", None),
        "lock_0p5atr_after_wall": ("lock", 0.5),
        "trail_1atr_after_wall": ("trail", 1.0),
        "trail_1p5atr_after_wall": ("trail", 1.5),
    }
    if variant in simple:
        return simple[variant]
    if variant == "positive_full_negative_ignore":
        return ("full", None) if gamma_state == 1 else ("ignore", None)
    if variant == "positive_full_negative_breakeven":
        return ("full", None) if gamma_state == 1 else ("breakeven", None)
    if variant == "positive_full_negative_trail_1atr":
        return ("full", None) if gamma_state == 1 else ("trail", 1.0)
    if variant == "positive_breakeven_negative_trail_1atr":
        return ("breakeven", None) if gamma_state == 1 else ("trail", 1.0)
    raise ValueError(f"unknown hybrid exit variant: {variant}")


def simulate_wall_transition(
    path: pd.DataFrame,
    direction: int,
    entry: float,
    initial_stop: float,
    wall_target: float,
    atr: float,
    action: str,
    trail_atr: float | None = None,
) -> dict[str, Any]:
    """Simulate a causal post-wall risk transition on MNQ minute OHLC."""
    if path.empty or direction not in {-1, 1}:
        raise ValueError("non-empty path and direction +/-1 are required")
    if action not in {"full", "ignore", "breakeven", "lock", "trail"}:
        raise ValueError(f"unknown wall action: {action}")
    if action == "trail" and (trail_atr is None or trail_atr <= 0):
        raise ValueError("positive trail_atr is required for a trail action")

    stop = float(initial_stop)
    wall_active = False
    peak = float(entry)
    wall_touched = False
    for elapsed, bar in enumerate(path.itertuples(index=False), 1):
        stop_hit = float(bar.low) <= stop if direction == 1 else float(bar.high) >= stop
        wall_hit = (
            float(bar.high) >= wall_target if direction == 1
            else float(bar.low) <= wall_target
        )
        if stop_hit and wall_hit and not wall_active and action == "full":
            reason = resolve_same_bar_exit(float(bar.open), stop, wall_target)
            if reason == "wall_tp" or reason == "tp":
                return {
                    "exit_price": float(wall_target), "exit_reason": "wall_tp",
                    "bars_held": elapsed, "wall_touched": True,
                }
        if stop_hit:
            return {
                "exit_price": _stop_fill(bar, direction, stop),
                "exit_reason": "sl_after_wall" if wall_active else "sl_before_wall",
                "bars_held": elapsed,
                "wall_touched": wall_touched,
            }
        if wall_hit and not wall_active:
            wall_touched = True
            if action == "full":
                return {
                    "exit_price": float(wall_target), "exit_reason": "wall_tp",
                    "bars_held": elapsed, "wall_touched": True,
                }
            if action != "ignore":
                wall_active = True
                if action == "breakeven":
                    stop = float(entry)
                elif action == "lock":
                    desired = entry + direction * float(atr) * float(trail_atr or 0.5)
                    stop = (
                        min(desired, wall_target - TICK_SIZE) if direction == 1
                        else max(desired, wall_target + TICK_SIZE)
                    )
                elif action == "trail":
                    peak = max(peak, float(bar.high)) if direction == 1 else min(
                        peak, float(bar.low),
                    )
                    desired = peak - direction * float(atr) * float(trail_atr)
                    stop = max(stop, desired) if direction == 1 else min(stop, desired)
                continue
        if wall_active and action == "trail":
            peak = max(peak, float(bar.high)) if direction == 1 else min(
                peak, float(bar.low),
            )
            desired = peak - direction * float(atr) * float(trail_atr)
            stop = max(stop, desired) if direction == 1 else min(stop, desired)

    return {
        "exit_price": float(path.iloc[-1]["close"]),
        "exit_reason": "time_after_wall" if wall_active else "time_before_wall",
        "bars_held": int(len(path)),
        "wall_touched": wall_touched,
    }


def run_hybrid_exit_study(data_root: Path = DEFAULT_DATA_ROOT) -> dict[str, Any]:
    trades = pd.read_csv(data_root / "option_wall_sltp_trades.csv.gz", compression="gzip")
    dataset = pd.read_csv(data_root / "option_wall_ml_dataset.csv.gz", compression="gzip")
    trades["as_of"] = pd.to_datetime(trades["as_of"], utc=True)
    dataset["as_of"] = pd.to_datetime(dataset["as_of"], utc=True)
    dataset["close_at"] = pd.to_datetime(dataset["close_at"], utc=True)
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
    base = trades[trades["policy"] == "wall_tp_pi_stop"].merge(
        context, on="as_of", how="left", validate="many_to_one", suffixes=("", "_feature"),
    )
    mnq = _read_mnq(data_root)
    cost = get_commission_rt("MNQ") + get_fees_rt("MNQ")
    point_value = get_point_value("MNQ")
    rows: list[dict[str, Any]] = []
    for _, trade in base.iterrows():
        source = pd.Series({
            "as_of": trade["as_of"], "close_at": trade["close_at"],
            "mnq_entry": trade["entry_price"],
        })
        path = _path_for_row(mnq, source, int(trade["horizon_minutes"]))
        if path.empty:
            continue
        gamma_state = int(trade["volume_gamma_state"])
        for variant in VARIANTS:
            action, trail_atr = _variant_action(variant, gamma_state)
            outcome = simulate_wall_transition(
                path, int(trade["direction"]), float(trade["entry_price"]),
                float(trade["sl_price"]), float(trade["tp_price"]),
                float(trade["atr_blend"]), action, trail_atr,
            )
            points = float(outcome["exit_price"]) - float(trade["entry_price"])
            rows.append({
                "date": str(trade["date"]), "as_of": trade["as_of"],
                "as_of_et": str(trade["as_of_et"]), "strategy": str(trade["strategy"]),
                "direction": int(trade["direction"]), "variant": variant,
                "volume_gamma_state": gamma_state, "oi_gamma_state": int(trade["oi_gamma_state"]),
                "exit_price": float(outcome["exit_price"]),
                "exit_reason": str(outcome["exit_reason"]),
                "bars_held": int(outcome["bars_held"]),
                "wall_touched": bool(outcome["wall_touched"]),
                "pnl": int(trade["direction"]) * points * point_value - cost,
                "original_wall_pnl": (
                    int(trade["direction"]) * float(trade["market_points"]) * point_value - cost
                ),
                "source_index": int(trade.name),
            })
    replay = pd.DataFrame(rows)
    if replay.empty:
        raise RuntimeError("no hybrid exit rows were produced")

    # Attach fixed gate decisions once per source entry.
    source_rules = base.copy()
    strict = book_rule_masks(source_rules)[STRICT_GATE]
    source_rules["strict_gate"] = strict
    strict_map = source_rules.set_index(source_rules.index)["strict_gate"].to_dict()
    replay["strict_gate"] = replay["source_index"].map(strict_map).fillna(False).astype(bool)

    # The exact-wall variant must reproduce the prior wall-target replay.
    exact = replay[replay["variant"] == "full_exit_at_wall"]
    if not np.allclose(exact["pnl"], exact["original_wall_pnl"]):
        raise RuntimeError("full wall replay does not reconcile with the prior SL/TP study")

    sessions = sorted(augmented["date"].astype(str).unique())
    holdout_sessions = set(sessions[int(math.floor(len(sessions) * 0.70)):])
    months = sorted(augmented["date"].astype(str).str[:7].unique())
    boundary = {months[0], months[-1]}
    results: dict[str, Any] = {}
    for strategy in sorted(replay["strategy"].unique()):
        results[strategy] = {}
        for variant in VARIANTS:
            current = replay[
                (replay["strategy"] == strategy) & (replay["variant"] == variant)
            ].copy()
            results[strategy][variant] = {}
            for gate_name, gate in (
                ("baseline", np.ones(len(current), dtype=bool)),
                (STRICT_GATE, current["strict_gate"].to_numpy(dtype=bool)),
            ):
                selected = current[gate].copy()
                holdout = selected[selected["date"].astype(str).isin(holdout_sessions)]
                positive = selected[selected["volume_gamma_state"] == 1]
                negative = selected[selected["volume_gamma_state"] == -1]
                summary = _summary(selected, selected["pnl"].to_numpy(), boundary)
                summary["wall_touch_rate"] = float(selected["wall_touched"].mean()) if len(selected) else None
                results[strategy][variant][gate_name] = {
                    "all": summary,
                    "holdout_last_30pct_sessions": _summary(
                        holdout, holdout["pnl"].to_numpy(), {months[-1]},
                    ),
                    "positive_gamma": _summary(
                        positive, positive["pnl"].to_numpy(), boundary,
                    ),
                    "negative_gamma": _summary(
                        negative, negative["pnl"].to_numpy(), boundary,
                    ),
                }

    output_path = data_root / "option_wall_hybrid_exit_trades.csv.gz"
    _atomic_csv(output_path, replay)
    report = {
        "status": "exploratory_research_only",
        "created_at": _iso(pd.Timestamp.now(tz="UTC")),
        "scope": "same entries, frozen entry wall/Gamma, original PI asymmetric stop",
        "variants": list(VARIANTS),
        "results": results,
        "trades_file": str(output_path),
        "warnings": [
            "Post-wall stops activate only after the wall-touch minute completes.",
            "Ambiguous same-minute wall and stop touches resolve to the stop.",
            "The test changes risk after a frozen wall; it does not yet consume a new option snapshot.",
        ],
    }
    report_path = data_root / "option_wall_hybrid_exit_report.json"
    _atomic_json(report_path, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    args = parser.parse_args()
    report = run_hybrid_exit_study(args.data_root)
    print(json.dumps({
        "status": report["status"],
        "report": str(args.data_root / "option_wall_hybrid_exit_report.json"),
    }, indent=2))


if __name__ == "__main__":
    main()
