"""Positive-GEX Put-Wall reclaim study using observed QQQ/MNQ minute paths.

The option state is frozen at each hourly point-in-time snapshot.  QQQ minute
bars then identify a touch, a real close below the Volume Put Wall, and a
subsequent reclaim.  MNQ entry is the first open after the confirming QQQ bar.
This module is research-only and cannot route orders.
"""
from __future__ import annotations

import argparse
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
from scripts.option_wall_exit_grid_study import _basic_metrics
from scripts.option_wall_ml_study import (
    DEFAULT_DATA_ROOT,
    _atomic_csv,
    _atomic_json,
    _iso,
    _qqq_bars,
)
from scripts.option_wall_sltp_study import (
    _atr_blend_at,
    _five_minute_bars,
    _pi_atr_levels,
    _read_mnq,
    _simulate_ohlc_exit,
)


PENETRATION_BPS = 2.0


def _positive_gamma(row: pd.Series) -> bool:
    net = float(row["dashboard_vol_net_gex_signed_log"])
    flip_bps = float(row["dashboard_vol_gamma_flip_proxy_bps"])
    return math.isfinite(net) and math.isfinite(flip_bps) and net > 0 and flip_bps <= 0


def _negative_gamma(row: pd.Series) -> bool:
    net = float(row["dashboard_vol_net_gex_signed_log"])
    flip_bps = float(row["dashboard_vol_gamma_flip_proxy_bps"])
    return math.isfinite(net) and math.isfinite(flip_bps) and net < 0 and flip_bps > 0


def _find_put_wall_event(
    qqq: pd.DataFrame,
    as_of: pd.Timestamp,
    window_end: pd.Timestamp,
    wall_price: float,
    mode: str,
    reclaim_minutes: int = 5,
    minimum_reclaim_price: float | None = None,
    penetration_bps: float = PENETRATION_BPS,
) -> dict[str, Any] | None:
    """Find the first causal touch/breach/reclaim after one snapshot."""
    future = qqq[
        (qqq["available_at"] > as_of) & (qqq["available_at"] <= window_end)
    ].reset_index(drop=True)
    if future.empty:
        return None
    low = pd.to_numeric(future["low"], errors="coerce").to_numpy(dtype=float)
    close = pd.to_numeric(future["close"], errors="coerce").to_numpy(dtype=float)
    threshold = wall_price * (1.0 - penetration_bps / 10_000.0)

    if mode == "touch":
        positions = np.flatnonzero(low <= wall_price)
        if not len(positions):
            return None
        index = int(positions[0])
        return {
            "event_at": pd.Timestamp(future.iloc[index]["available_at"]),
            "breach_at": pd.Timestamp(future.iloc[index]["available_at"]),
            "reclaim_minutes": None,
            "qqq_entry_signal_price": float(close[index]),
        }
    if mode not in {"wick_reclaim", "close_reclaim"}:
        raise ValueError(f"unknown event mode: {mode}")

    breach_mask = low <= threshold if mode == "wick_reclaim" else close <= threshold
    breach_positions = np.flatnonzero(breach_mask)
    if mode == "close_reclaim" and len(breach_positions):
        # A run of closes below the wall is one breach episode.  Do not reset the
        # reclaim clock on every additional below-wall bar; that would silently
        # turn a 4-minute reclaim into a "3-minute" reclaim.
        breach_positions = np.flatnonzero(
            breach_mask & np.r_[True, ~breach_mask[:-1]]
        )
    reclaim_level = max(wall_price, minimum_reclaim_price or wall_price)
    for breach_index in breach_positions:
        breach_at = pd.Timestamp(future.iloc[int(breach_index)]["available_at"])
        first_reclaim = int(breach_index) if mode == "wick_reclaim" else int(breach_index) + 1
        for reclaim_index in range(first_reclaim, len(future)):
            reclaim_at = pd.Timestamp(future.iloc[reclaim_index]["available_at"])
            if reclaim_at > breach_at + pd.Timedelta(minutes=reclaim_minutes):
                break
            if close[reclaim_index] >= reclaim_level:
                return {
                    "event_at": reclaim_at,
                    "breach_at": breach_at,
                    "reclaim_minutes": float(
                        (reclaim_at - breach_at).total_seconds() / 60.0
                    ),
                    "qqq_entry_signal_price": float(close[reclaim_index]),
                }
    return None


def _rule_event(
    row: pd.Series,
    qqq: pd.DataFrame,
    rule: str,
) -> dict[str, Any] | None:
    as_of = pd.Timestamp(row["as_of"])
    window_end = min(as_of + pd.Timedelta(minutes=60), pd.Timestamp(row["close_at"]))
    spot = float(row["qqq_spot"])
    put_bps = float(row["dashboard_vol_put_wall_bps"])
    if rule == "positive_snapshot":
        return {
            "event_at": as_of, "breach_at": None, "reclaim_minutes": None,
            "qqq_entry_signal_price": spot,
        } if _positive_gamma(row) else None
    if not math.isfinite(put_bps) or put_bps >= 0:
        return None
    wall = spot * (1.0 + put_bps / 10_000.0)
    positive = _positive_gamma(row)
    negative = _negative_gamma(row)
    specifications = {
        "touch_any": ("touch", 5, True, None),
        "touch_positive": ("touch", 5, positive, None),
        "wick_reclaim_5m_positive": ("wick_reclaim", 5, positive, None),
        "close_reclaim_3m_positive": ("close_reclaim", 3, positive, None),
        "close_reclaim_5m_positive": ("close_reclaim", 5, positive, None),
        "close_reclaim_10m_positive": ("close_reclaim", 10, positive, None),
        "close_reclaim_5m_any": ("close_reclaim", 5, True, None),
        "close_reclaim_5m_negative": ("close_reclaim", 5, negative, None),
        "close_reclaim_5m_positive_flip_recovered": (
            "close_reclaim", 5, positive,
            spot * (1.0 + float(row["dashboard_vol_gamma_flip_proxy_bps"]) / 10_000.0),
        ),
    }
    if rule not in specifications:
        raise ValueError(f"unknown rule: {rule}")
    mode, minutes, enabled, minimum = specifications[rule]
    if not enabled:
        return None
    event = _find_put_wall_event(
        qqq, as_of, window_end, wall, mode, minutes, minimum,
    )
    if event is not None:
        event.update({
            "put_wall_price": wall,
            "put_wall_bps": put_bps,
            "gamma_positive": positive,
            "gamma_negative": negative,
        })
    return event


def _mnq_trade(
    mnq: pd.DataFrame,
    five_minute: pd.DataFrame,
    entry_at: pd.Timestamp,
    close_at: pd.Timestamp,
    exit_policy: str,
) -> dict[str, Any] | None:
    entries = mnq.loc[entry_at:entry_at + pd.Timedelta(minutes=2)]
    if entries.empty:
        return None
    entry_row = entries.iloc[0]
    entry_ts = pd.Timestamp(entry_row["ts"])
    entry = float(entry_row["open"])
    horizon = 30 if exit_policy == "time_30m" else 60
    deadline = min(entry_ts + pd.Timedelta(minutes=horizon), close_at)
    path = mnq.loc[entry_ts:deadline - pd.Timedelta(nanoseconds=1)].copy()
    if path.empty:
        return None
    if exit_policy == "pi_sl3_tp10_60m":
        atr = _atr_blend_at(five_minute, entry_ts)
        if atr is None or not math.isfinite(atr) or atr <= 0:
            return None
        sl, tp = _pi_atr_levels(entry, 1, atr, 3.0, 10.0)
    elif exit_policy in {"time_30m", "time_60m"}:
        atr, sl, tp = None, None, None
    else:
        raise ValueError(f"unknown exit policy: {exit_policy}")
    outcome = _simulate_ohlc_exit(path, 1, entry, sl, tp)
    exit_bar = path.iloc[int(outcome["bars_held"]) - 1]
    exit_available_at = min(
        pd.Timestamp(exit_bar["ts"]) + pd.Timedelta(minutes=1), close_at,
    )
    pnl = (
        (float(outcome["exit_price"]) - entry) * get_point_value("MNQ")
        - get_commission_rt("MNQ") - get_fees_rt("MNQ")
    )
    return {
        "mnq_entry_at": entry_ts,
        "mnq_entry": entry,
        "mnq_exit_at": exit_available_at,
        "mnq_exit": float(outcome["exit_price"]),
        "exit_reason": str(outcome["exit_reason"]),
        "bars_held": int(outcome["bars_held"]),
        "atr_blend": atr,
        "pnl": float(pnl),
    }


def _target_diagnostics(
    qqq: pd.DataFrame,
    entry_at: pd.Timestamp,
    close_at: pd.Timestamp,
    snapshot_spot: float,
    call_wall_price: float | None,
) -> dict[str, Any]:
    end = min(entry_at + pd.Timedelta(minutes=60), close_at)
    future = qqq[(qqq["available_at"] > entry_at) & (qqq["available_at"] <= end)]
    high = pd.to_numeric(future["high"], errors="coerce")
    return {
        "returned_to_snapshot_spot": bool(len(high) and (high >= snapshot_spot).any()),
        "hit_call_wall": bool(
            call_wall_price is not None and len(high) and (high >= call_wall_price).any()
        ),
    }


def _summary(trades: pd.DataFrame, session_months: Sequence[str]) -> dict[str, Any]:
    if trades.empty:
        return {"trades": 0, "net_pnl": 0.0, "pf": 0.0}
    ordered = trades.sort_values("mnq_entry_at")
    result = _basic_metrics(ordered["pnl"].to_numpy(dtype=float))
    months = ordered["date"].astype(str).str[:7]
    result["monthly"] = {
        month: float(ordered.loc[months == month, "pnl"].sum())
        for month in session_months
    }
    result.update({
        "exit_reasons": dict(Counter(ordered["exit_reason"].astype(str))),
        "returned_to_snapshot_spot_rate": float(
            ordered["returned_to_snapshot_spot"].mean()
        ),
        "hit_call_wall_rate": float(ordered["hit_call_wall"].mean()),
        "mean_reclaim_minutes": (
            float(pd.to_numeric(ordered["reclaim_minutes"], errors="coerce").mean())
            if ordered["reclaim_minutes"].notna().any() else None
        ),
    })
    return result


def run_positive_gamma_reclaim(data_root: Path = DEFAULT_DATA_ROOT) -> dict[str, Any]:
    dataset_path = data_root / "option_wall_ml_dataset.csv.gz"
    frame = pd.read_csv(dataset_path, compression="gzip")
    frame = frame[frame["as_of_et"].astype(str).str.endswith(":00")].copy()
    frame["as_of"] = pd.to_datetime(frame["as_of"], utc=True)
    frame["close_at"] = pd.to_datetime(frame["close_at"], utc=True)
    frame = frame.sort_values("as_of").reset_index(drop=True)
    mnq = _read_mnq(data_root)
    five_minute = _five_minute_bars(mnq)
    qqq_by_day = {
        day: _qqq_bars(data_root / "raw" / day)
        for day in frame["date"].astype(str).unique()
    }
    rules = (
        "positive_snapshot",
        "touch_any",
        "touch_positive",
        "wick_reclaim_5m_positive",
        "close_reclaim_3m_positive",
        "close_reclaim_5m_positive",
        "close_reclaim_10m_positive",
        "close_reclaim_5m_any",
        "close_reclaim_5m_negative",
        "close_reclaim_5m_positive_flip_recovered",
    )
    policies = ("time_30m", "time_60m", "pi_sl3_tp10_60m")
    candidates: dict[str, list[dict[str, Any]]] = {rule: [] for rule in rules}
    for _, row in frame.iterrows():
        day = str(row["date"])
        qqq = qqq_by_day[day]
        call_bps = float(row["dashboard_vol_call_wall_bps"])
        call_wall = (
            float(row["qqq_spot"]) * (1.0 + call_bps / 10_000.0)
            if math.isfinite(call_bps) and call_bps > 0 else None
        )
        for rule in rules:
            event = _rule_event(row, qqq, rule)
            if event is None:
                continue
            diagnostics = _target_diagnostics(
                qqq, pd.Timestamp(event["event_at"]), pd.Timestamp(row["close_at"]),
                float(row["qqq_spot"]), call_wall,
            )
            candidates[rule].append({
                "date": day,
                "snapshot_at": pd.Timestamp(row["as_of"]),
                "snapshot_et": str(row["as_of_et"]),
                "close_at": pd.Timestamp(row["close_at"]),
                "qqq_spot": float(row["qqq_spot"]),
                "call_wall_price": call_wall,
                "net_volume_gex_signed_log": float(row["dashboard_vol_net_gex_signed_log"]),
                "volume_gamma_flip_bps": float(row["dashboard_vol_gamma_flip_proxy_bps"]),
                "rule": rule,
                **event,
                **diagnostics,
            })

    all_sessions = sorted(frame["date"].astype(str).unique())
    split_index = int(math.floor(len(all_sessions) * 0.70))
    holdout_sessions = set(all_sessions[split_index:])
    all_months = sorted({day[:7] for day in all_sessions})
    holdout_months = sorted({day[:7] for day in holdout_sessions})
    reports: dict[str, Any] = {}
    output_rows: list[dict[str, Any]] = []
    for rule in rules:
        reports[rule] = {"candidate_events": len(candidates[rule]), "exits": {}}
        for policy in policies:
            accepted: list[dict[str, Any]] = []
            next_available = pd.Timestamp.min.tz_localize("UTC")
            overlap_skipped = 0
            for candidate in sorted(candidates[rule], key=lambda value: value["event_at"]):
                event_at = pd.Timestamp(candidate["event_at"])
                if event_at < next_available:
                    overlap_skipped += 1
                    continue
                trade = _mnq_trade(
                    mnq, five_minute, event_at, pd.Timestamp(candidate["close_at"]), policy,
                )
                if trade is None:
                    continue
                row = {
                    **candidate,
                    **trade,
                    "exit_policy": policy,
                    "holdout": str(candidate["date"]) in holdout_sessions,
                }
                accepted.append(row)
                output_rows.append(row)
                next_available = pd.Timestamp(trade["mnq_exit_at"])
            trades = pd.DataFrame(accepted)
            holdout = (
                trades[trades["holdout"]].copy() if not trades.empty else pd.DataFrame()
            )
            reports[rule]["exits"][policy] = {
                "all": _summary(trades, all_months),
                "holdout_last_30pct_sessions": _summary(holdout, holdout_months),
                "overlap_skipped": int(overlap_skipped),
            }

    trades_path = data_root / "option_wall_positive_gamma_reclaim_trades.csv.gz"
    _atomic_csv(trades_path, pd.DataFrame(output_rows))
    positive = frame.apply(_positive_gamma, axis=1)
    negative = frame.apply(_negative_gamma, axis=1)
    valid_put = pd.to_numeric(frame["dashboard_vol_put_wall_bps"], errors="coerce") < 0
    report: dict[str, Any] = {
        "status": "provisional_proxy_research_only",
        "created_at": _iso(pd.Timestamp.now(tz="UTC").to_pydatetime()),
        "source_chapter": "https://options-wall-book.mmoptions.workers.dev/read/market-makers-gex/",
        "rule": (
            "hourly Volume GEX snapshot; Volume Put Wall below spot; at least 2 bp breach; "
            "close back above the frozen wall within 3/5/10 minutes; next MNQ minute open"
        ),
        "data": {
            "sessions": len(all_sessions),
            "first_session": all_sessions[0],
            "last_session": all_sessions[-1],
            "hourly_snapshots": int(len(frame)),
            "positive_gamma_snapshots": int(positive.sum()),
            "negative_gamma_snapshots": int(negative.sum()),
            "positive_gamma_with_put_wall_below_spot": int((positive & valid_put).sum()),
            "holdout_sessions": int(len(holdout_sessions)),
            "holdout_first_session": min(holdout_sessions),
        },
        "gex_definition": (
            "calls-positive/puts-negative Dollar-GEX proxy within +/-4% of spot; "
            "Volume uses cumulative unsigned option volume and is not observed dealer inventory"
        ),
        "results": reports,
        "trades_file": str(trades_path),
        "warnings": [
            "Option volume/GEX updates hourly; QQQ and MNQ confirmation paths are one minute.",
            "The wall and Gamma sign are frozen until the next snapshot, so an intrahour regime flip is unobserved.",
            "Positive GEX describes damping/mean reversion, not an upward forecast.",
            "The last-30%-session holdout is more informative than the all-period comparison, but event counts may be small.",
        ],
    }
    _atomic_json(data_root / "option_wall_positive_gamma_reclaim_report.json", report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = run_positive_gamma_reclaim(args.data_root)
    print(json.dumps(report, indent=2, allow_nan=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
