"""Intrahour OI-wall monitoring for strict primary option-wall trades.

For each accepted primary entry, recompute the zero-day OI Gamma profile from
the latest causal CBBO snapshot after 5, 10, and 15 minutes.  The experiment
then asks whether an MNQ trade should exit when its target wall moves adversely,
falls behind spot, loses peak alignment, or changes Gamma state.

Volume walls cannot be recomputed at this cadence from the purchased hourly
volume schema, so every output is explicitly labelled OI-only.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.db.models import get_commission_rt, get_fees_rt, get_point_value
from scripts.option_wall_all_models_gate_exit_study import STRICT_GATE, _summary
from scripts.option_wall_book_rules_study import augment_book_features, book_rule_masks
from scripts.option_wall_demo import _contract_profile, _gamma_price_profile
from scripts.option_wall_ml_study import (
    DEFAULT_DATA_ROOT,
    _atomic_csv,
    _atomic_json,
    _column_time,
    _fresh_price_at,
    _iso,
    _qqq_bars,
    _session_bounds,
    _signed_log1p,
    extract_wall_features,
)
from scripts.option_wall_sltp_study import (
    _path_for_row,
    _read_mnq,
    _simulate_ohlc_exit,
)


CHECK_MINUTES = (5, 10, 15)
DECISION_LATENCY_MINUTES = (0, 1)
EXIT_RULES = (
    "baseline_hold",
    "exit_if_target_behind_spot",
    "exit_if_target_moved_adverse_5bps",
    "exit_unless_target_moved_favorable",
    "exit_if_oi_gamma_state_changed",
    "exit_if_peak_not_directional",
    "exit_on_any_structure_failure",
)


def _intrahour_gamma_state(net_signed_log: float, flip_bps: float) -> int:
    if not math.isfinite(net_signed_log) or not math.isfinite(flip_bps):
        return 0
    if net_signed_log > 0 and flip_bps <= 0:
        return 1
    if net_signed_log < 0 and flip_bps > 0:
        return -1
    return 0


def _requested_snapshots(entries: pd.DataFrame) -> dict[str, list[pd.Timestamp]]:
    requests: dict[str, set[pd.Timestamp]] = defaultdict(set)
    for row in entries.itertuples():
        as_of = pd.Timestamp(row.as_of)
        close_at = pd.Timestamp(row.close_at)
        for minutes in CHECK_MINUTES:
            at = as_of + pd.Timedelta(minutes=minutes)
            if at < close_at:
                requests[str(row.date)].add(at)
    return {day: sorted(values) for day, values in requests.items()}


def build_intrahour_oi_snapshots(
    data_root: Path,
    requests: dict[str, list[pd.Timestamp]],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    raw_root = data_root / "raw"
    for day_index, (day, times) in enumerate(sorted(requests.items()), 1):
        folder = raw_root / day
        required = [
            folder / "qqq_definition.csv.gz", folder / "qqq_0dte_statistics.csv.gz",
            folder / "qqq_0dte_cbbo_1m.csv.gz", folder / "qqq_ohlcv_1m.csv.gz",
        ]
        if not all(path.is_file() for path in required):
            continue
        definitions_frame = pd.read_csv(required[0], compression="gzip")
        expiration = pd.to_datetime(definitions_frame["expiration"], utc=True, errors="coerce")
        definitions_frame = definitions_frame[
            (expiration.dt.date.astype(str) == day)
            & definitions_frame["instrument_class"].isin(["C", "P"])
        ]
        definitions = {
            str(row.raw_symbol): {
                "strike": float(row.strike_price), "class": str(row.instrument_class),
            }
            for row in definitions_frame.itertuples()
        }
        stats = pd.read_csv(required[1], compression="gzip")
        open_interest = (
            stats[stats["stat_type"] == 9]
            .groupby("symbol")["quantity"].first().astype(int).to_dict()
        )
        quotes = pd.read_csv(required[2], compression="gzip")
        quotes["ts"] = _column_time(quotes, ["ts_recv", "ts_event"])
        quotes = quotes.sort_values("ts")
        quote_records = quotes.to_dict("records")
        qqq = _qqq_bars(folder)
        _, _, expiry = _session_bounds(pd.Timestamp(day).date())
        expiry_at = pd.Timestamp(expiry)
        latest_quotes: dict[str, dict[str, Any]] = {}
        quote_index = 0
        for at in times:
            while quote_index < len(quote_records) and quote_records[quote_index]["ts"] <= at:
                quote = quote_records[quote_index]
                latest_quotes[str(quote["symbol"])] = {
                    "ts": quote["ts"], "bid": quote.get("bid_px_00"),
                    "ask": quote.get("ask_px_00"),
                }
                quote_index += 1
            spot = _fresh_price_at(qqq, at)
            if spot is None:
                continue
            profile = _contract_profile(
                latest_quotes, {}, definitions, open_interest, float(spot), at, expiry_at,
            )
            if profile.empty:
                continue
            years = max((expiry_at - at).total_seconds(), 1.0) / (365.0 * 24.0 * 3600.0)
            gamma_profile = _gamma_price_profile(profile, float(spot), years, "oi")
            walls = extract_wall_features(
                profile, float(spot), at.to_pydatetime(), expiry_at.to_pydatetime(),
                gamma_profile,
            )
            scoped = profile[
                np.abs(pd.to_numeric(profile["strike"], errors="coerce") / float(spot) - 1.0)
                <= 0.04
            ]
            net_gex = float(pd.to_numeric(scoped["oi_gex"], errors="coerce").sum())
            call_bps = float(walls.get("oi_call_wall_bps", math.nan))
            put_bps = float(walls.get("oi_put_wall_bps", math.nan))
            flip_bps = float(walls.get("oi_gamma_flip_bps", math.nan))
            rows.append({
                "date": day, "snapshot_at": at, "qqq_spot": float(spot),
                "oi_call_wall_bps": call_bps, "oi_put_wall_bps": put_bps,
                "oi_call_wall_level": float(spot) * (1.0 + call_bps / 10_000.0),
                "oi_put_wall_level": float(spot) * (1.0 + put_bps / 10_000.0),
                "oi_gamma_flip_bps": flip_bps,
                "oi_net_gex_signed_log": _signed_log1p(net_gex),
                "oi_gamma_state": _intrahour_gamma_state(_signed_log1p(net_gex), flip_bps),
                "oi_peak1_bps": float(walls.get("oi_peak1_bps", math.nan)),
                "oi_peak1_share": float(walls.get("oi_peak1_share", math.nan)),
                "valid_contracts": int(len(profile)),
            })
        print(f"intrahour OI {day_index}/{len(requests)} {day}", flush=True)
    return pd.DataFrame(rows)


def _rule_exit_required(row: pd.Series, rule: str) -> bool:
    if rule == "baseline_hold":
        return False
    target_behind = not bool(row["target_still_beyond_spot"])
    adverse = float(row["oriented_target_move_bps"]) < -5.0
    not_favorable = float(row["oriented_target_move_bps"]) <= 0.0
    gamma_changed = int(row["snapshot_oi_gamma_state"]) != int(row["entry_oi_gamma_state"])
    peak_wrong = int(row["snapshot_peak_direction"]) != int(row["direction"])
    checks = {
        "exit_if_target_behind_spot": target_behind,
        "exit_if_target_moved_adverse_5bps": adverse,
        "exit_unless_target_moved_favorable": not_favorable,
        "exit_if_oi_gamma_state_changed": gamma_changed,
        "exit_if_peak_not_directional": peak_wrong,
        "exit_on_any_structure_failure": target_behind or adverse or gamma_changed,
    }
    if rule not in checks:
        raise ValueError(f"unknown OI monitoring exit rule: {rule}")
    return bool(checks[rule])


def _conditional_exit(
    path: pd.DataFrame,
    direction: int,
    entry: float,
    stop: float,
    decision_at: pd.Timestamp,
    should_exit: bool,
) -> dict[str, Any]:
    before = path[path["ts"] < decision_at]
    if not before.empty:
        prefix = _simulate_ohlc_exit(before, direction, entry, stop, None)
        if prefix["exit_reason"] == "sl":
            return {**prefix, "decision_exit": False}
    if should_exit:
        decision_rows = path[path["ts"] >= decision_at]
        if not decision_rows.empty:
            return {
                "exit_price": float(decision_rows.iloc[0]["open"]),
                "exit_reason": "oi_structure_exit",
                "bars_held": int(len(before) + 1),
                "decision_exit": True,
            }
    outcome = _simulate_ohlc_exit(path, direction, entry, stop, None)
    return {**outcome, "decision_exit": False}


def run_intrahour_oi_study(data_root: Path = DEFAULT_DATA_ROOT) -> dict[str, Any]:
    dataset = pd.read_csv(data_root / "option_wall_ml_dataset.csv.gz", compression="gzip")
    trades = pd.read_csv(data_root / "option_wall_sltp_trades.csv.gz", compression="gzip")
    dataset["as_of"] = pd.to_datetime(dataset["as_of"], utc=True)
    dataset["close_at"] = pd.to_datetime(dataset["close_at"], utc=True)
    trades["as_of"] = pd.to_datetime(trades["as_of"], utc=True)
    augmented = augment_book_features(dataset)
    context_columns = [
        "as_of", "close_at", "qqq_spot", "oi_call_wall_bps", "oi_put_wall_bps",
        "oi_gamma_state", "book_gamma_consensus", "volume_gamma_state",
        "article_price_vwap_distance_bps", "article_price_return_15m_bps",
        "dashboard_vol_call_wall_bps", "dashboard_vol_put_wall_bps",
        "article_event_opex_week", "article_event_month_end_friday",
        "dashboard_oi_net_gex_signed_log", "dashboard_vol_net_gex_signed_log",
        "dashboard_vol_gamma_flip_proxy_bps",
        "dashboard_vol_call_wall_share", "dashboard_vol_put_wall_share",
        "article_dashboard_vol_call_wall_share_delta",
        "article_dashboard_vol_put_wall_share_delta",
        "article_dashboard_vol_call_wall_migration_bps_per_hour",
        "article_dashboard_vol_put_wall_migration_bps_per_hour",
        "book_wall_tension", "book_wall_break_signal", "oi_peak_count_20pct",
        "oi_peak1_share", "oi_peak1_bps", "article_gvp_full_alignment",
    ]
    entries = trades[
        (trades["strategy"] == "primary_model_confidence")
        & (trades["policy"] == "wall_tp_pi_stop")
    ].merge(
        augmented[context_columns], on="as_of", how="left", validate="one_to_one",
        suffixes=("", "_feature"),
    )
    entries = entries[np.asarray(book_rule_masks(entries)[STRICT_GATE], dtype=bool)].copy()
    entries["entry_oi_gamma_state"] = entries["oi_gamma_state"]
    requests = _requested_snapshots(entries)
    snapshot_path = data_root / "option_wall_intrahour_oi_snapshots.csv.gz"
    expected_count = sum(len(values) for values in requests.values())
    if snapshot_path.is_file():
        snapshots = pd.read_csv(snapshot_path, compression="gzip")
        snapshots["snapshot_at"] = pd.to_datetime(snapshots["snapshot_at"], utc=True)
        keys = set(snapshots["snapshot_at"])
        reusable = all(at in keys for values in requests.values() for at in values)
    else:
        reusable = False
        snapshots = pd.DataFrame()
    if not reusable:
        snapshots = build_intrahour_oi_snapshots(data_root, requests)
        _atomic_csv(snapshot_path, snapshots)

    mnq = _read_mnq(data_root)
    cost = get_commission_rt("MNQ") + get_fees_rt("MNQ")
    point_value = get_point_value("MNQ")
    snapshot_map = snapshots.set_index("snapshot_at")
    rows: list[dict[str, Any]] = []
    for _, entry_row in entries.iterrows():
        source = pd.Series({
            "as_of": entry_row["as_of"], "close_at": entry_row["close_at"],
            "mnq_entry": entry_row["entry_price"],
        })
        path = _path_for_row(mnq, source, int(entry_row["horizon_minutes"]))
        if path.empty:
            continue
        direction = int(entry_row["direction"])
        initial_bps = float(
            entry_row["oi_call_wall_bps"] if direction == 1 else entry_row["oi_put_wall_bps"]
        )
        initial_level = float(entry_row["qqq_spot"]) * (1.0 + initial_bps / 10_000.0)
        for check_minutes in CHECK_MINUTES:
            at = pd.Timestamp(entry_row["as_of"]) + pd.Timedelta(minutes=check_minutes)
            if at not in snapshot_map.index:
                continue
            snapshot = snapshot_map.loc[at]
            if isinstance(snapshot, pd.DataFrame):
                snapshot = snapshot.iloc[-1]
            target_level = float(
                snapshot["oi_call_wall_level"] if direction == 1
                else snapshot["oi_put_wall_level"]
            )
            target_move = direction * (target_level / initial_level - 1.0) * 10_000.0
            still_beyond = direction * (target_level - float(snapshot["qqq_spot"])) > 0
            peak_direction = int(np.sign(float(snapshot["oi_peak1_bps"])))
            state = pd.Series({
                "direction": direction,
                "entry_oi_gamma_state": int(entry_row["entry_oi_gamma_state"]),
                "snapshot_oi_gamma_state": int(snapshot["oi_gamma_state"]),
                "snapshot_peak_direction": peak_direction,
                "oriented_target_move_bps": target_move,
                "target_still_beyond_spot": still_beyond,
            })
            for latency_minutes in DECISION_LATENCY_MINUTES:
                execution_at = at + pd.Timedelta(minutes=latency_minutes)
                for rule in EXIT_RULES:
                    should_exit = _rule_exit_required(state, rule)
                    outcome = _conditional_exit(
                        path, direction, float(entry_row["entry_price"]),
                        float(entry_row["sl_price"]), execution_at, should_exit,
                    )
                    points = float(outcome["exit_price"]) - float(entry_row["entry_price"])
                    rows.append({
                        "date": str(entry_row["date"]), "as_of": entry_row["as_of"],
                        "as_of_et": str(entry_row["as_of_et"]), "direction": direction,
                        "check_minutes": check_minutes,
                        "decision_latency_minutes": latency_minutes,
                        "exit_rule": rule,
                        "entry_oi_gamma_state": int(entry_row["entry_oi_gamma_state"]),
                        "snapshot_oi_gamma_state": int(snapshot["oi_gamma_state"]),
                        "oriented_target_move_bps": target_move,
                        "target_still_beyond_spot": still_beyond,
                        "snapshot_peak_direction": peak_direction,
                        "decision_exit": bool(outcome["decision_exit"]),
                        "exit_reason": str(outcome["exit_reason"]),
                        "bars_held": int(outcome["bars_held"]),
                        "pnl": direction * points * point_value - cost,
                    })
    replay = pd.DataFrame(rows)
    if replay.empty:
        raise RuntimeError("no intrahour OI exit replays were produced")
    sessions = sorted(augmented["date"].astype(str).unique())
    holdout_sessions = set(sessions[int(math.floor(len(sessions) * 0.70)):])
    months = sorted(augmented["date"].astype(str).str[:7].unique())
    results: dict[str, Any] = {}
    for check_minutes in CHECK_MINUTES:
        results[str(check_minutes)] = {}
        for latency_minutes in DECISION_LATENCY_MINUTES:
            latency_key = f"latency_{latency_minutes}m"
            results[str(check_minutes)][latency_key] = {}
            for rule in EXIT_RULES:
                current = replay[
                    (replay["check_minutes"] == check_minutes)
                    & (replay["decision_latency_minutes"] == latency_minutes)
                    & (replay["exit_rule"] == rule)
                ].copy()
                holdout = current[current["date"].astype(str).isin(holdout_sessions)]
                summary = _summary(current, current["pnl"].to_numpy(), {months[0], months[-1]})
                summary["decision_exit_rate"] = float(current["decision_exit"].mean())
                results[str(check_minutes)][latency_key][rule] = {
                    "all": summary,
                    "holdout_last_30pct_sessions": _summary(
                        holdout, holdout["pnl"].to_numpy(), {months[-1]},
                    ),
                }

    output_path = data_root / "option_wall_intrahour_oi_trades.csv.gz"
    _atomic_csv(output_path, replay)
    report = {
        "status": "exploratory_research_only",
        "created_at": _iso(pd.Timestamp.now(tz="UTC")),
        "data": {
            "strict_primary_entries": len(entries),
            "requested_snapshots": expected_count,
            "computed_snapshots": len(snapshots),
            "checks_minutes": list(CHECK_MINUTES),
            "decision_latency_minutes": list(DECISION_LATENCY_MINUTES),
        },
        "results": results,
        "snapshot_file": str(snapshot_path),
        "trades_file": str(output_path),
        "warnings": [
            "Intrahour wall updates use official overnight OI plus causal CBBO IV/Gamma only.",
            "Purchased option volume is hourly and is not interpolated into fake five-minute Volume Walls.",
            "Structure exits are reported at both zero-minute and one-minute decision latency.",
        ],
    }
    report_path = data_root / "option_wall_intrahour_oi_report.json"
    _atomic_json(report_path, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    args = parser.parse_args()
    report = run_intrahour_oi_study(args.data_root)
    print(json.dumps({
        "status": report["status"], "data": report["data"],
        "report": str(args.data_root / "option_wall_intrahour_oi_report.json"),
    }, indent=2))


if __name__ == "__main__":
    main()
