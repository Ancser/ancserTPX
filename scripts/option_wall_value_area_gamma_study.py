"""Research-only study of opening value placement and option-wall gamma.

The study answers a narrow, causal question without changing the product
strategy or live engine:

* take the downloaded option-wall/GEX snapshot available at 10:00 ET;
* calculate the completed prior Topstep session's 70% VAH/VAL/POC from the
  canonical MNQ 1-minute store;
* calculate the 09:30-10:30 ET opening-hour value area;
* classify opening value as above, overlapping, or below yesterday's value;
* report every 2 x 3 state in both long and short directions; and
* evaluate a fixed theory map as a research counterfactual.

The option-wall artifact is a QQQ options context mapped to MNQ in the
existing research pipeline.  It is not a live MNQ options feed.  This module
does not place orders, register a preset, or alter the production
``OPTION WALL`` strategy.

The fixed map is deliberately explicit:

    negative gamma + value above -> long (breakout continuation)
    negative gamma + value below -> short (breakout continuation)
    positive gamma + value above -> short (mean reversion)
    positive gamma + value below -> long (mean reversion)

When opening value overlaps yesterday's value, the first post-opening-hour
boundary event is used:

    positive gamma: VAL touch -> long, VAH touch -> short
    negative gamma: VAL break -> short, VAH break -> long

The report also contains a directional grid for all states.  That grid is a
diagnostic, not a selection procedure; no in-sample winner is promoted to the
application.

Example::

    python scripts/option_wall_value_area_gamma_study.py
    python scripts/option_wall_value_area_gamma_study.py \
        --data-root F:/ancserQuant/ancserMarketData/source/options/qqq_option_ml

Outputs stay under the canonical MarketData tree by default, outside this repo:

* ``option_wall_value_area_gamma_study.json``
* ``option_wall_value_area_gamma_rows.csv.gz``
* ``option_wall_value_area_gamma_trades.csv.gz``
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.db.models import get_commission_rt, get_fees_rt, get_point_value
from backend.data import market_data
from backend.strategy.volume_profile import VolumeProfileCalculator
from backend.timebase import topstep_trade_date
from scripts.option_wall_gamma_gate_study import _gamma_state
from scripts.option_wall_ml_study import DEFAULT_DATA_ROOT, _atomic_csv, _atomic_json, _iso


UTC = timezone.utc
NY = ZoneInfo("America/New_York")
DEFAULT_MNQ_PATH = market_data.candle_store_dir() / "MNQ_accumulated_1m.pkl"
VALUE_AREA_PCT = 0.70
OPENING_HOUR_START = time(9, 30)
OPENING_HOUR_END = time(10, 30)
ENTRY_TIME = time(10, 30)
RTH_CLOSE = time(16, 0)
GEX_FAMILIES = ("oi", "volume")
VALUE_SHIFTS = ("above", "overlap", "below")
GAMMA_STATES = (-1, 1, 0)
HORIZONS = ("30m", "60m", "close")

REQUIRED_DATA_COLUMNS = (
    "date",
    "as_of",
    "as_of_et",
    "dashboard_oi_net_gex_signed_log",
    "oi_gamma_flip_bps",
    "dashboard_vol_net_gex_signed_log",
    "dashboard_vol_gamma_flip_proxy_bps",
)


def classify_value_position(price: float, vah: float, val: float) -> str:
    """Classify a price against a completed value area."""
    if price > vah:
        return "above"
    if price < val:
        return "below"
    return "inside"


def classify_value_shift(
    first_vah: float,
    first_val: float,
    prior_vah: float,
    prior_val: float,
) -> str:
    """Classify opening value placement without inventing a gap direction."""
    if first_val > prior_vah:
        return "above"
    if first_vah < prior_val:
        return "below"
    return "overlap"


def theory_direction(gamma_state: int, value_shift: str) -> int:
    """Return the fixed outside-value direction, or 0 for overlap/unknown."""
    if gamma_state not in (-1, 1) or value_shift not in ("above", "below"):
        return 0
    if gamma_state == -1:
        return 1 if value_shift == "above" else -1
    return -1 if value_shift == "above" else 1


def boundary_direction(gamma_state: int, boundary: str) -> int:
    """Map an overlap boundary event to the fixed gamma theory direction."""
    if gamma_state not in (-1, 1) or boundary not in ("vah", "val"):
        return 0
    if gamma_state == 1:
        return 1 if boundary == "val" else -1
    return 1 if boundary == "vah" else -1


def _utc_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _local_time(candle: Any) -> time:
    return _utc_timestamp(candle.timestamp).astimezone(NY).time().replace(
        second=0,
        microsecond=0,
    )


def _local_date(candle: Any) -> str:
    return _utc_timestamp(candle.timestamp).astimezone(NY).date().isoformat()


def _candle_timestamp(candle: Any) -> datetime:
    return _utc_timestamp(candle.timestamp)


def _load_option_rows(dataset_path: Path) -> pd.DataFrame:
    if not dataset_path.is_file():
        raise RuntimeError(f"option-wall dataset missing: {dataset_path}")
    frame = pd.read_csv(dataset_path, compression="gzip")
    missing = [column for column in REQUIRED_DATA_COLUMNS if column not in frame.columns]
    if missing:
        raise RuntimeError(f"option-wall dataset is missing columns: {missing}")
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    frame["as_of"] = pd.to_datetime(frame["as_of"], utc=True, errors="coerce")
    frame = frame[
        frame["date"].notna()
        & frame["as_of"].notna()
        & frame["as_of_et"].astype(str).eq("10:00")
    ].copy()
    frame = frame.sort_values(["date", "as_of"]).drop_duplicates("date", keep="last")
    if frame.empty:
        raise RuntimeError("option-wall dataset has no usable 10:00 ET rows")
    return frame.reset_index(drop=True)


def _load_candles(
    store_path: Path,
    target_dates: set[str],
) -> tuple[dict[str, list[Any]], dict[str, list[Any]]]:
    """Load only the profile/session buckets needed by this study.

    The pickle itself is loaded once because it is the canonical app store;
    only candles in the target window are retained in the working buckets.
    """
    if not store_path.is_file():
        raise RuntimeError(f"canonical MNQ store missing: {store_path}")
    if not target_dates:
        return {}, {}
    first = date.fromisoformat(min(target_dates)) - timedelta(days=14)
    last = date.fromisoformat(max(target_dates)) + timedelta(days=1)
    by_trade_date: dict[str, list[Any]] = defaultdict(list)
    by_session_date: dict[str, list[Any]] = defaultdict(list)
    with store_path.open("rb") as handle:
        candles = pickle.load(handle)
    for candle in candles:
        local = _utc_timestamp(candle.timestamp).astimezone(NY)
        local_date = local.date()
        if local_date < first or local_date > last:
            continue
        by_trade_date[topstep_trade_date(_utc_timestamp(candle.timestamp))].append(candle)
        day = local_date.isoformat()
        if day not in target_dates:
            continue
        local_clock = local.time().replace(second=0, microsecond=0)
        if OPENING_HOUR_START <= local_clock < RTH_CLOSE:
            by_session_date[day].append(candle)
    return dict(by_trade_date), dict(by_session_date)


def _completed_profiles(
    by_trade_date: dict[str, list[Any]],
) -> dict[str, Any]:
    calculator = VolumeProfileCalculator(
        tick_size=0.25,
        value_area_pct=VALUE_AREA_PCT,
    )
    profiles: dict[str, Any] = {}
    for trade_date, candles in by_trade_date.items():
        ordered = sorted(candles, key=_candle_timestamp)
        try:
            profiles[trade_date] = calculator.calculate(ordered)
        except ValueError:
            # A session with no positive volume cannot define a reference area.
            continue
    return profiles


def _prior_profile(
    target_date: str,
    profiles: dict[str, Any],
) -> tuple[str, Any] | None:
    session_open = datetime.combine(
        date.fromisoformat(target_date),
        OPENING_HOUR_START,
        tzinfo=NY,
    ).astimezone(UTC)
    target_trade_date = topstep_trade_date(session_open)
    candidates = [value for value in profiles if value < target_trade_date]
    if not candidates:
        return None
    source_date = max(candidates)
    return source_date, profiles[source_date]


def _price_at_or_after(
    candles: list[Any],
    target: datetime,
) -> float | None:
    target = _utc_timestamp(target)
    for candle in candles:
        if _candle_timestamp(candle) >= target:
            return float(candle.open)
    return None


def _opening_snapshot(
    target_date: str,
    candles: list[Any],
    prior: tuple[str, Any],
) -> dict[str, Any] | None:
    calculator = VolumeProfileCalculator(
        tick_size=0.25,
        value_area_pct=VALUE_AREA_PCT,
    )
    ordered = sorted(candles, key=_candle_timestamp)
    opening = [
        candle for candle in ordered
        if OPENING_HOUR_START <= _local_time(candle) < OPENING_HOUR_END
    ]
    if not opening:
        return None
    entry_bar = next(
        (candle for candle in ordered if _local_time(candle) == ENTRY_TIME),
        None,
    )
    if entry_bar is None:
        return None
    first_profile = calculator.calculate(opening)
    source_date, prior_profile = prior
    entry_ts = _candle_timestamp(entry_bar)
    p30 = _price_at_or_after(ordered, entry_ts + timedelta(minutes=30))
    p60 = _price_at_or_after(ordered, entry_ts + timedelta(minutes=60))
    close = float(ordered[-1].close) if ordered else None
    if p30 is None or p60 is None or close is None:
        return None
    first_vah = float(first_profile.vah)
    first_val = float(first_profile.val)
    prior_vah = float(prior_profile.vah)
    prior_val = float(prior_profile.val)
    entry = float(entry_bar.open)
    return {
        "date": target_date,
        "entry_ts": _iso(entry_ts),
        "entry_price": entry,
        "price_30m": p30,
        "price_60m": p60,
        "price_close": close,
        "prior_trade_date": source_date,
        "prior_poc": float(prior_profile.poc),
        "prior_vah_70": prior_vah,
        "prior_val_70": prior_val,
        "first_hour_poc": float(first_profile.poc),
        "first_hour_vah_70": first_vah,
        "first_hour_val_70": first_val,
        "first_hour_bars": len(opening),
        "value_shift": classify_value_shift(
            first_vah,
            first_val,
            prior_vah,
            prior_val,
        ),
        "entry_location": classify_value_position(entry, prior_vah, prior_val),
        "_candles": ordered,
    }


def _first_boundary_event(
    snapshot: dict[str, Any],
    gamma_state: int,
) -> dict[str, Any] | None:
    """Find the first causal boundary event for an overlapping opening value."""
    if snapshot["value_shift"] != "overlap" or gamma_state not in (-1, 1):
        return None
    vah = float(snapshot["prior_vah_70"])
    val = float(snapshot["prior_val_70"])
    entry_ts = datetime.fromisoformat(snapshot["entry_ts"].replace("Z", "+00:00"))
    candles = snapshot["_candles"]
    for candle in candles:
        timestamp = _candle_timestamp(candle)
        if timestamp < entry_ts:
            continue
        hit_vah = float(candle.high) >= vah
        hit_val = float(candle.low) <= val
        if not hit_vah and not hit_val:
            continue
        if hit_vah and hit_val:
            # A 1-minute OHLC bar has no tick ordering.  Do not manufacture a
            # first boundary or a direction when both levels were crossed.
            return {
                "event_status": "ambiguous_both_boundaries",
                "event_ts": _iso(timestamp),
            }
        boundary = "vah" if hit_vah else "val"
        direction = boundary_direction(gamma_state, boundary)
        return {
            "event_status": "triggered",
            "event_ts": _iso(timestamp),
            "boundary": boundary,
            "trigger_price": vah if boundary == "vah" else val,
            "direction": direction,
        }
    return {"event_status": "not_triggered"}


def _trigger_prices(
    snapshot: dict[str, Any],
    event: dict[str, Any],
) -> dict[str, float | None]:
    if event.get("event_status") != "triggered":
        return {horizon: None for horizon in HORIZONS}
    event_ts = datetime.fromisoformat(event["event_ts"].replace("Z", "+00:00"))
    candles = snapshot["_candles"]
    return {
        "30m": _price_at_or_after(candles, event_ts + timedelta(minutes=30)),
        "60m": _price_at_or_after(candles, event_ts + timedelta(minutes=60)),
        "close": float(candles[-1].close) if candles else None,
    }


def _pnl(direction: int, entry: float, exit_price: float | None, cost: float, point_value: float) -> float | None:
    if direction not in (-1, 1) or exit_price is None:
        return None
    return direction * (exit_price - entry) * point_value - cost


def _summary(values: Iterable[float | None]) -> dict[str, Any]:
    numbers = np.asarray(
        [float(value) for value in values if value is not None and np.isfinite(value)],
        dtype=float,
    )
    if not len(numbers):
        return {"n": 0, "net_pnl": None, "avg_pnl": None, "win_rate": None, "profit_factor": None}
    gains = float(numbers[numbers > 0].sum())
    losses = float(-numbers[numbers < 0].sum())
    return {
        "n": int(len(numbers)),
        "net_pnl": round(float(numbers.sum()), 2),
        "avg_pnl": round(float(numbers.mean()), 2),
        "win_rate": round(float((numbers > 0).mean()), 4),
        "profit_factor": round(gains / losses, 4) if losses else None,
    }


def _snapshot_grid(
    snapshots: pd.DataFrame,
    family: str,
    cost: float,
    point_value: float,
) -> list[dict[str, Any]]:
    """Return all two-direction counterfactuals for every 2 x 3 bucket."""
    result: list[dict[str, Any]] = []
    for gamma_state in GAMMA_STATES:
        for value_shift in VALUE_SHIFTS:
            bucket = snapshots[
                snapshots["gamma_family"].eq(family)
                & (snapshots["gamma_state"] == gamma_state)
                & snapshots["value_shift"].eq(value_shift)
            ]
            for direction, direction_name in ((1, "long"), (-1, "short")):
                row: dict[str, Any] = {
                    "family": family,
                    "gamma_state": gamma_state,
                    "gamma_label": _gamma_label(gamma_state),
                    "value_shift": value_shift,
                    "direction": direction,
                    "direction_label": direction_name,
                }
                for horizon in HORIZONS:
                    row[horizon] = _summary(
                        _pnl(
                            direction,
                            float(entry),
                            float(exit_price),
                            cost,
                            point_value,
                        )
                        for entry, exit_price in zip(
                            bucket["entry_price"],
                            bucket[f"price_{horizon}"],
                        )
                    )
                result.append(row)
    return result


def _gamma_label(state: int) -> str:
    return {-1: "negative", 1: "positive", 0: "unknown"}.get(state, "unknown")


def _safe_number(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if pd.isna(value):
        return None
    return value


def _build_study(
    option_rows: pd.DataFrame,
    session_bars: dict[str, list[Any]],
    trade_bars: dict[str, list[Any]],
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    profiles = _completed_profiles(trade_bars)
    snapshots: list[dict[str, Any]] = []
    for row in option_rows.to_dict("records"):
        target_date = str(row["date"])
        prior = _prior_profile(target_date, profiles)
        if prior is None:
            continue
        snapshot = _opening_snapshot(target_date, session_bars.get(target_date, []), prior)
        if snapshot is None:
            continue
        for family, net_column, flip_column in (
            ("oi", "dashboard_oi_net_gex_signed_log", "oi_gamma_flip_bps"),
            (
                "volume",
                "dashboard_vol_net_gex_signed_log",
                "dashboard_vol_gamma_flip_proxy_bps",
            ),
        ):
            state = int(_gamma_state(
                pd.Series([row[net_column]]),
                pd.Series([row[flip_column]]),
            )[0])
            item = dict(snapshot)
            item.update({
                "gamma_family": family,
                "gamma_state": state,
                "gamma_label": _gamma_label(state),
                "gex_value": _safe_number(row[net_column]),
                "gamma_flip_bps": _safe_number(row[flip_column]),
                "option_as_of": _iso(row["as_of"].to_pydatetime()),
            })
            # Keep the session candles private until boundary events are
            # evaluated; snapshots written to the CSV stay scalar and small.
            item["_candles"] = session_bars[target_date]
            event = _first_boundary_event(item, state)
            item["_boundary_event"] = event
            item["boundary_event_status"] = (
                event.get("event_status")
                if event is not None
                else "outside_value"
                if snapshot["value_shift"] != "overlap"
                else "not_applicable"
            )
            snapshots.append(item)

    if not snapshots:
        raise RuntimeError("no option-wall rows could be aligned to the canonical MNQ store")

    cost = get_commission_rt("MNQ") + get_fees_rt("MNQ")
    point_value = get_point_value("MNQ")
    trade_rows: list[dict[str, Any]] = []
    for snapshot in snapshots:
        family = snapshot["gamma_family"]
        state = int(snapshot["gamma_state"])
        direction = theory_direction(state, snapshot["value_shift"])
        event: dict[str, Any] | None = snapshot.get("_boundary_event")
        entry_price: float | None = None
        entry_ts: str | None = None
        entry_reason = "opening_hour_value_shift"
        if direction:
            entry_price = float(snapshot["entry_price"])
            entry_ts = snapshot["entry_ts"]
        elif snapshot["value_shift"] == "overlap" and state in (-1, 1):
            event = _first_boundary_event(snapshot, state)
            if event and event.get("event_status") == "triggered":
                direction = int(event["direction"])
                entry_price = float(event["trigger_price"])
                entry_ts = str(event["event_ts"])
                entry_reason = f"{event['boundary']}_{'break' if state == -1 else 'touch'}"
        if not direction or entry_price is None or entry_ts is None:
            continue
        if event and event.get("event_status") == "triggered":
            exits = _trigger_prices(snapshot, event)
        else:
            exits = {
                "30m": snapshot["price_30m"],
                "60m": snapshot["price_60m"],
                "close": snapshot["price_close"],
            }
        trade = {
            "date": snapshot["date"],
            "gamma_family": family,
            "gamma_state": state,
            "gamma_label": snapshot["gamma_label"],
            "value_shift": snapshot["value_shift"],
            "entry_location": snapshot["entry_location"],
            "entry_reason": entry_reason,
            "entry_ts": entry_ts,
            "entry_price": entry_price,
            "direction": direction,
            "direction_label": "long" if direction == 1 else "short",
            "boundary": event.get("boundary") if event else None,
            "event_status": event.get("event_status") if event else "outside_value",
        }
        for horizon in HORIZONS:
            trade[f"exit_{horizon}"] = exits[horizon]
            trade[f"pnl_{horizon}"] = _pnl(
                direction,
                entry_price,
                exits[horizon],
                cost,
                point_value,
            )
        trade_rows.append(trade)

    snapshot_frame = pd.DataFrame(
        [{key: value for key, value in item.items() if not key.startswith("_")} for item in snapshots]
    )
    trade_frame = pd.DataFrame(trade_rows)
    grid = {
        family: _snapshot_grid(snapshot_frame, family, cost, point_value)
        for family in GEX_FAMILIES
    }
    summary: dict[str, Any] = {
        "sample": {
            "option_rows_at_10_et": int(len(option_rows)),
            "aligned_snapshot_rows": int(len(snapshot_frame)),
            "aligned_sessions": int(snapshot_frame["date"].nunique()),
            "date_start": str(snapshot_frame["date"].min()),
            "date_end": str(snapshot_frame["date"].max()),
            "opening_hour_et": "09:30-10:30",
            "gex_snapshot_et": "10:00",
            "entry_et": "10:30",
            "value_area_pct": VALUE_AREA_PCT,
            "prior_profile_boundary": "Topstep trade date, 17:00 America/Chicago",
            "mnq_store": str(DEFAULT_MNQ_PATH),
            "option_wall_context": "QQQ options artifact mapped to MNQ research labels",
        },
        "cost_model": {
            "contract": "MNQ",
            "round_turn_cost": cost,
            "point_value": point_value,
        },
        "value_shift_counts": {
            key: int(value)
            for key, value in snapshot_frame.drop_duplicates("date")["value_shift"].value_counts().to_dict().items()
        },
        "gamma_state_counts": {
            family: {
                _gamma_label(int(state)): int(value)
                for state, value in snapshot_frame[
                    snapshot_frame["gamma_family"].eq(family)
                ]["gamma_state"].value_counts().to_dict().items()
            }
            for family in GEX_FAMILIES
        },
        "boundary_event_counts": {
            family: {
                str(status): int(value)
                for status, value in snapshot_frame[
                    snapshot_frame["gamma_family"].eq(family)
                ]["boundary_event_status"].value_counts().to_dict().items()
            }
            for family in GEX_FAMILIES
        },
        "directional_grid": grid,
        "theory_mapping": {
            "negative_above": "long",
            "negative_overlap_vah_break": "long",
            "negative_overlap_val_break": "short",
            "negative_below": "short",
            "positive_above": "short",
            "positive_overlap_vah_touch": "short",
            "positive_overlap_val_touch": "long",
            "positive_below": "long",
            "unknown": "no trade",
        },
        "theory_trades": {
            family: {
                horizon: _summary(
                    trade_frame.loc[
                        trade_frame["gamma_family"].eq(family),
                        f"pnl_{horizon}",
                    ].tolist()
                    if not trade_frame.empty else []
                )
                for horizon in HORIZONS
            }
            for family in GEX_FAMILIES
        },
        "theory_trade_counts": {
            family: int(
                trade_frame["gamma_family"].eq(family).sum()
                if not trade_frame.empty else 0
            )
            for family in GEX_FAMILIES
        },
        "theory_trade_buckets": {
            family: {
                f"{gamma_label}_{value_shift}": {
                    "trades": int(
                        (
                            trade_frame["gamma_family"].eq(family)
                            & trade_frame["gamma_label"].eq(gamma_label)
                            & trade_frame["value_shift"].eq(value_shift)
                        ).sum()
                        if not trade_frame.empty else 0
                    ),
                    "30m": _summary(
                        trade_frame.loc[
                            trade_frame["gamma_family"].eq(family)
                            & trade_frame["gamma_label"].eq(gamma_label)
                            & trade_frame["value_shift"].eq(value_shift),
                            "pnl_30m",
                        ].tolist()
                        if not trade_frame.empty else []
                    ),
                    "60m": _summary(
                        trade_frame.loc[
                            trade_frame["gamma_family"].eq(family)
                            & trade_frame["gamma_label"].eq(gamma_label)
                            & trade_frame["value_shift"].eq(value_shift),
                            "pnl_60m",
                        ].tolist()
                        if not trade_frame.empty else []
                    ),
                    "close": _summary(
                        trade_frame.loc[
                            trade_frame["gamma_family"].eq(family)
                            & trade_frame["gamma_label"].eq(gamma_label)
                            & trade_frame["value_shift"].eq(value_shift),
                            "pnl_close",
                        ].tolist()
                        if not trade_frame.empty else []
                    ),
                }
                for gamma_label in ("negative", "positive", "unknown")
                for value_shift in VALUE_SHIFTS
            }
            for family in GEX_FAMILIES
        },
        "notes": [
            "GEX is sampled at 10:00 ET because the downloaded artifact has no 10:30 ET snapshot.",
            "The opening value area is complete before the 10:30 ET entry; no future bar is used to classify it.",
            "Overlapping-value boundary events use 1-minute OHLC. If one bar crosses both VAH and VAL, it is marked ambiguous and skipped.",
            "The directional grid is descriptive. It must not be used to pick an in-sample winner.",
            "This output is research only; production OPTION WALL remains historical replay only until a causal live option feed exists.",
        ],
    }
    return snapshot_frame, trade_frame, summary


def run_study(
    data_root: Path = DEFAULT_DATA_ROOT,
    mnq_store: Path = DEFAULT_MNQ_PATH,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    option_rows = _load_option_rows(data_root / "option_wall_ml_dataset.csv.gz")
    target_dates = set(option_rows["date"].astype(str))
    trade_bars, session_bars = _load_candles(mnq_store, target_dates)
    snapshots, trades, report = _build_study(option_rows, session_bars, trade_bars)
    report["sample"]["mnq_store"] = str(mnq_store)
    return snapshots, trades, report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--mnq-store", type=Path, default=DEFAULT_MNQ_PATH)
    parser.add_argument("--json-name", default="option_wall_value_area_gamma_study.json")
    parser.add_argument("--rows-name", default="option_wall_value_area_gamma_rows.csv.gz")
    parser.add_argument("--trades-name", default="option_wall_value_area_gamma_trades.csv.gz")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    snapshots, trades, report = run_study(args.data_root, args.mnq_store)
    report["created_at"] = _iso(datetime.now(UTC))
    report["paths"] = {
        "data_root": str(args.data_root),
        "mnq_store": str(args.mnq_store),
        "rows": str(args.data_root / args.rows_name),
        "trades": str(args.data_root / args.trades_name),
    }
    _atomic_json(args.data_root / args.json_name, report)
    _atomic_csv(args.data_root / args.rows_name, snapshots)
    _atomic_csv(args.data_root / args.trades_name, trades)
    print(json.dumps({
        "report": str(args.data_root / args.json_name),
        "rows": len(snapshots),
        "trades": len(trades),
        "theory_trades": report["theory_trade_counts"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
