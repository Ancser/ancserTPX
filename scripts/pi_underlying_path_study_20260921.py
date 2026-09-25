"""Retrospective PI source-time path study on actual QQQ 1m underlying bars.

No futures proxy is used. Rows come from the canonical PI history loader; mark
level is retained separately from mark kind. Run from repository root:
    python scripts/pi_underlying_path_study_20260921.py --focus-only
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.data.market_data import option_wall_root
from backend.data.pi_history import load_rows, parse_ts
from backend.live.pi_listener import DIRECTION

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
BUY_KINDS = frozenset(kind for kind, direction in DIRECTION.items() if direction > 0)
RTH_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)
CLOSED_2026 = frozenset(date.fromisoformat(value) for value in (
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
    "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
    "2026-11-26", "2026-12-25",
))
EARLY_CLOSES_2026 = frozenset(date.fromisoformat(value) for value in (
    "2026-11-27", "2026-12-24",
))
FOCUS_DATES = frozenset(date.fromisoformat(value) for value in (
    "2026-03-30", "2026-06-05", "2026-06-09",
    "2026-07-02", "2026-07-20", "2026-07-24",
))


@dataclass(frozen=True)
class Bar:
    ts: datetime  # UTC bar-start timestamp
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Signal:
    ts: datetime
    symbol: str
    kind: str
    level: int | str
    signal_id: str


def is_session(day: date) -> bool:
    return day.weekday() < 5 and day not in CLOSED_2026


def session_close(day: date) -> time:
    return EARLY_CLOSE if day in EARLY_CLOSES_2026 else REGULAR_CLOSE


def expected_session_times(day: date) -> list[datetime]:
    if not is_session(day):
        return []
    start = datetime.combine(day, RTH_OPEN, tzinfo=NY)
    close = datetime.combine(day, session_close(day), tzinfo=NY)
    count = int((close - start).total_seconds() // 60)
    return [(start + timedelta(minutes=i)).astimezone(UTC) for i in range(count)]


def following_sessions(day: date, count: int) -> list[date]:
    result: list[date] = []
    cursor = day + timedelta(days=1)
    while len(result) < count:
        if is_session(cursor):
            result.append(cursor)
        cursor += timedelta(days=1)
    return result


def source_level(mark: dict) -> int | str:
    value = mark.get("level")
    if value is None:
        return "unknown"
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)


def canonical_buy_signals(rows: list[dict]) -> tuple[list[Signal], int]:
    signals: list[Signal] = []
    seen: set[tuple] = set()
    duplicate_count = 0
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        if symbol not in {"QQQ", "SPY"}:
            continue
        try:
            ts = parse_ts(row["ts"])
        except Exception:
            continue
        for mark in row.get("marks") or []:
            kind = mark.get("kind")
            if kind not in BUY_KINDS:
                continue
            level = source_level(mark)
            key = (ts.replace(second=0, microsecond=0), symbol, kind, str(level))
            if key in seen:
                duplicate_count += 1
                continue
            seen.add(key)
            signals.append(Signal(ts, symbol, kind, level, str(row.get("id") or "")))
    signals.sort(key=lambda item: (item.ts, item.symbol, item.kind, str(item.level)))
    return signals, duplicate_count


def load_qqq_rth_bars(root: Path) -> tuple[dict[date, dict[datetime, Bar]], dict]:
    bars_by_day: dict[date, dict[datetime, Bar]] = defaultdict(dict)
    files = sorted((root / "raw").glob("????-??-??/qqq_ohlcv_1m.csv.gz"))
    for path in files:
        try:
            frame = pd.read_csv(
                path, compression="gzip",
                usecols=["ts_event", "open", "high", "low", "close"],
            )
        except Exception:
            continue
        timestamps = pd.to_datetime(frame["ts_event"], utc=True, errors="coerce")
        local = timestamps.dt.tz_convert(NY)
        day = path.parent.name
        close_text = session_close(date.fromisoformat(day))
        tod = local.dt.time
        mask = (
            timestamps.notna()
            & local.dt.date.eq(date.fromisoformat(day))
            & tod.ge(RTH_OPEN)
            & tod.lt(close_text)
        )
        picked = frame.loc[mask, ["open", "high", "low", "close"]].copy()
        picked_ts = timestamps.loc[mask]
        for stamp, values in zip(picked_ts, picked.itertuples(index=False, name=None)):
            ts = stamp.to_pydatetime().astimezone(UTC)
            bars_by_day[date.fromisoformat(day)][ts] = Bar(
                ts, *(float(value) for value in values)
            )
    coverage = {
        "daily_files": len(files),
        "rth_days_with_bars": len(bars_by_day),
        "rth_bar_count": sum(len(day_bars) for day_bars in bars_by_day.values()),
        "first_bar_day": min(bars_by_day).isoformat() if bars_by_day else None,
        "last_bar_day": max(bars_by_day).isoformat() if bars_by_day else None,
    }
    return bars_by_day, coverage


def expected_entry(signal_ts: datetime) -> tuple[date | None, datetime | None]:
    cutoff = signal_ts.replace(second=0, microsecond=0) + timedelta(minutes=1)
    source_day = signal_ts.astimezone(NY).date()
    if not is_session(source_day):
        return None, None
    eligible = [
        stamp for stamp in expected_session_times(source_day)
        if stamp >= cutoff
    ]
    return source_day, (eligible[0] if eligible else None)


def first_pass_category(bars: Iterable[Bar], p0: float, threshold: float) -> str:
    up_level, down_level = p0 * (1 + threshold), p0 * (1 - threshold)
    first: str | None = None
    for bar in bars:
        up = bar.high >= up_level
        down = bar.low <= down_level
        if first is None:
            if up and down:
                return "same_bar_ambiguous"
            if up:
                first = "up"
            elif down:
                first = "down"
        elif first == "up" and down:
            return "up_first_then_down"
        elif first == "down" and up:
            return "down_first_then_up"
    if first == "up":
        return "up_only"
    if first == "down":
        return "down_only"
    return "neither"


def recovery_flags(bars: list[Bar], p0: float, threshold: float) -> dict:
    up_idx = next((i for i, bar in enumerate(bars) if bar.high >= p0 * (1 + threshold)), None)
    down_idx = next((i for i, bar in enumerate(bars) if bar.low <= p0 * (1 - threshold)), None)
    return {
        "up_hit": up_idx is not None,
        "up_then_fallback_p0": (
            up_idx is not None and any(bar.low <= p0 for bar in bars[up_idx + 1:])
        ),
        "down_hit": down_idx is not None,
        "down_then_reclaim_p0": (
            down_idx is not None and any(bar.high >= p0 for bar in bars[down_idx + 1:])
        ),
    }


def target_hit(bars: list[Bar], p0: float, dollars: float) -> dict:
    target = p0 + dollars
    index = next((i for i, bar in enumerate(bars) if bar.high >= target), None)
    if index is None:
        return {"hit": False, "target": target}
    prior_lows = [bar.low for bar in bars[:index]]
    hit_bar = bars[index]
    prior_min = min(prior_lows) if prior_lows else p0
    including_hit_min = min(prior_lows + [hit_bar.low])
    return {
        "hit": True,
        "target": target,
        "elapsed_minutes_lower": index,
        "elapsed_minutes_upper": index + 1,
        "hit_bar_utc": hit_bar.ts.isoformat(),
        "strict_prior_min_low": min(prior_lows) if prior_lows else None,
        "strict_prior_min_low_pct": (prior_min / p0 - 1) * 100,
        "hit_bar_low": hit_bar.low,
        "hit_bar_low_pct": (hit_bar.low / p0 - 1) * 100,
        "pre_target_min_low_price_bound": [including_hit_min, prior_min],
        "pre_target_min_low_pct_bound": [
            (including_hit_min / p0 - 1) * 100,
            (prior_min / p0 - 1) * 100,
        ],
        "hit_bar_low_target_order_unknown": True,
    }


def validated_horizon_path(
    entry_day: date,
    entry_ts: datetime,
    horizon: int,
    bars_by_day: dict[date, dict[datetime, Bar]],
) -> tuple[list[Bar] | None, list[datetime]]:
    day0_expected = [
        stamp for stamp in expected_session_times(entry_day) if stamp >= entry_ts
    ]
    following = following_sessions(entry_day, horizon)
    required = day0_expected + [
        stamp for session_day in following
        for stamp in expected_session_times(session_day)
    ]
    missing = [
        stamp for stamp in required
        if stamp not in bars_by_day.get(stamp.astimezone(NY).date(), {})
    ]
    if missing:
        return None, missing
    path = [
        bars_by_day[entry_day][stamp] for stamp in day0_expected
    ] + [
        bars_by_day[session_day][stamp]
        for session_day in following
        for stamp in expected_session_times(session_day)
    ]
    return path, []


def summarize_path(bars: list[Bar], p0: float, horizon: int, end_day: date) -> dict:
    terminal = bars[-1].close
    highest = max(bar.high for bar in bars)
    lowest = min(bar.low for bar in bars)
    ranges = [(bar.high - bar.low) / p0 * 100 for bar in bars]
    return {
        "horizon_sessions": horizon,
        "end_session": end_day.isoformat(),
        "terminal_close": terminal,
        "terminal_return_dollars": terminal - p0,
        "terminal_return_pct": (terminal / p0 - 1) * 100,
        "positive_close": terminal > p0,
        "path_range_dollars": highest - lowest,
        "path_range_pct": (highest - lowest) / p0 * 100,
        "mfe_dollars": highest - p0,
        "mfe_pct": (highest / p0 - 1) * 100,
        "mae_dollars": lowest - p0,
        "mae_pct": (lowest / p0 - 1) * 100,
        "mean_bar_range_pct": statistics.fmean(ranges),
        "median_bar_range_pct": statistics.median(ranges),
    }


def pooled_horizon_summary(events: list[dict], horizon: str) -> dict:
    values = [
        event["horizons"][horizon]
        for event in events
        if event.get("horizons", {}).get(horizon, {}).get("valid")
    ]
    returns = [item["terminal_return_pct"] for item in values]
    dollar_returns = [item["terminal_return_dollars"] for item in values]
    ranges = [item["path_range_dollars"] for item in values]
    range_pcts = [item["path_range_pct"] for item in values]
    return {
        "n": len(values),
        "positive_close_count": sum(item["positive_close"] for item in values),
        "mean_return_pct": statistics.fmean(returns) if returns else None,
        "median_return_pct": statistics.median(returns) if returns else None,
        "mean_return_dollars": statistics.fmean(dollar_returns) if values else None,
        "median_return_dollars": statistics.median(dollar_returns) if values else None,
        "mean_path_range_dollars": statistics.fmean(ranges) if values else None,
        "median_path_range_dollars": statistics.median(ranges) if values else None,
        "mean_path_range_pct": statistics.fmean(range_pcts) if values else None,
        "median_path_range_pct": statistics.median(range_pcts) if values else None,
        "mean_mfe_pct": statistics.fmean(item["mfe_pct"] for item in values) if values else None,
        "median_mfe_pct": statistics.median(item["mfe_pct"] for item in values) if values else None,
        "mean_mfe_dollars": statistics.fmean(item["mfe_dollars"] for item in values) if values else None,
        "median_mfe_dollars": statistics.median(item["mfe_dollars"] for item in values) if values else None,
        "mean_mae_pct": statistics.fmean(item["mae_pct"] for item in values) if values else None,
        "median_mae_pct": statistics.median(item["mae_pct"] for item in values) if values else None,
        "mean_mae_dollars": statistics.fmean(item["mae_dollars"] for item in values) if values else None,
        "median_mae_dollars": statistics.median(item["mae_dollars"] for item in values) if values else None,
        "max_up_excursion_pct": max((item["mfe_pct"] for item in values), default=None),
        "max_down_excursion_pct": min((item["mae_pct"] for item in values), default=None),
        "max_up_excursion_dollars": max((item["mfe_dollars"] for item in values), default=None),
        "max_down_excursion_dollars": min((item["mae_dollars"] for item in values), default=None),
        "mean_bar_range_pct_1m": statistics.fmean(item["mean_bar_range_pct"] for item in values) if values else None,
        "median_bar_range_pct_1m": statistics.median(item["median_bar_range_pct"] for item in values) if values else None,
    }


def analyze_signal(
    signal: Signal, bars_by_day: dict[date, dict[datetime, Bar]],
) -> dict:
    entry_day, entry_ts = expected_entry(signal.ts)
    base = {
        "source_ts_utc": signal.ts.isoformat(),
        "source_ts_ny": signal.ts.astimezone(NY).isoformat(),
        "symbol": signal.symbol,
        "kind": signal.kind,
        "source_level": signal.level,
        "signal_id": signal.signal_id,
    }
    if signal.symbol != "QQQ":
        return {**base, "status": "no_underlying_dataset"}
    if entry_day is None or entry_ts is None:
        return {
            **base,
            "status": "no_source_session_entry" if entry_day else "no_calendar_entry",
            "source_session": entry_day.isoformat() if entry_day else None,
        }
    day_bars = bars_by_day.get(entry_day, {})
    if entry_ts not in day_bars:
        return {
            **base, "status": "missing_entry_bar",
            "expected_entry_day": entry_day.isoformat(),
            "expected_entry_utc": entry_ts.isoformat(),
        }
    p0 = day_bars[entry_ts].open
    result = {
        **base,
        "status": "entry_ok",
        "reference_price": p0,
        "reference_utc": entry_ts.isoformat(),
        "reference_ny": entry_ts.astimezone(NY).isoformat(),
        "entry_session": entry_day.isoformat(),
        "horizons": {},
    }
    horizon_paths: dict[int, list[Bar]] = {}
    for horizon in (1, 2, 3, 5):
        next_days = following_sessions(entry_day, horizon)
        path, missing = validated_horizon_path(
            entry_day, entry_ts, horizon, bars_by_day,
        )
        if missing:
            day0_expected = [
                stamp for stamp in expected_session_times(entry_day)
                if stamp >= entry_ts
            ]
            missing_day0 = [
                stamp for stamp in day0_expected if stamp not in day_bars
            ]
            result["horizons"][str(horizon)] = {
                "valid": False,
                "missing_day0_bars": len(missing_day0),
                "missing_required_bars": len(missing),
                "first_missing_utc": missing[0].isoformat(),
                "end_session": next_days[-1].isoformat(),
            }
            continue
        assert path is not None
        horizon_paths[horizon] = path
        summary = summarize_path(path, p0, horizon, next_days[-1])
        summary["valid"] = True
        summary["missing_day0_bars"] = 0
        summary["first_pass_0_5"] = first_pass_category(path, p0, 0.005)
        summary["recoveries_0_5"] = recovery_flags(path, p0, 0.005)
        result["horizons"][str(horizon)] = summary
    if 5 in horizon_paths:
        path5 = horizon_paths[5]
        result["first_pass_sensitivity"] = {
            str(pct): first_pass_category(path5, p0, pct / 100)
            for pct in (0.25, 0.5, 1.0)
        }
        result["recovery_0_5"] = recovery_flags(path5, p0, 0.005)
        result["targets_5d"] = {
            "plus_3": target_hit(path5, p0, 3),
            "plus_10": target_hit(path5, p0, 10),
        }
    return result


def aggregate(events: list[dict]) -> dict:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for event in events:
        groups[(event["symbol"], event["kind"], str(event["source_level"]))].append(event)
    output = []
    for key, rows in sorted(groups.items(), key=lambda item: tuple(map(str, item[0]))):
        group = {"symbol": key[0], "kind": key[1], "source_level": key[2], "signals": len(rows)}
        group["status_counts"] = dict(Counter(row["status"] for row in rows))
        group["horizons"] = {}
        for horizon in ("1", "2", "3", "5"):
            values = [
                row["horizons"][horizon]
                for row in rows
                if row.get("horizons", {}).get(horizon, {}).get("valid")
            ]
            returns = [item["terminal_return_pct"] for item in values]
            group["horizons"][horizon] = {
                "n": len(values),
                "positive_close_count": sum(item["positive_close"] for item in values),
                "mean_return_pct": statistics.fmean(returns) if returns else None,
                "median_return_pct": statistics.median(returns) if returns else None,
                "mean_return_dollars": statistics.fmean(item["terminal_return_dollars"] for item in values) if values else None,
                "median_return_dollars": statistics.median(item["terminal_return_dollars"] for item in values) if values else None,
                "mean_path_range_dollars": statistics.fmean(item["path_range_dollars"] for item in values) if values else None,
                "median_path_range_dollars": statistics.median(item["path_range_dollars"] for item in values) if values else None,
                "mean_path_range_pct": statistics.fmean(item["path_range_pct"] for item in values) if values else None,
                "median_path_range_pct": statistics.median(item["path_range_pct"] for item in values) if values else None,
                "mean_mfe_pct": statistics.fmean(item["mfe_pct"] for item in values) if values else None,
                "median_mfe_pct": statistics.median(item["mfe_pct"] for item in values) if values else None,
                "mean_mfe_dollars": statistics.fmean(item["mfe_dollars"] for item in values) if values else None,
                "median_mfe_dollars": statistics.median(item["mfe_dollars"] for item in values) if values else None,
                "mean_mae_pct": statistics.fmean(item["mae_pct"] for item in values) if values else None,
                "median_mae_pct": statistics.median(item["mae_pct"] for item in values) if values else None,
                "mean_mae_dollars": statistics.fmean(item["mae_dollars"] for item in values) if values else None,
                "median_mae_dollars": statistics.median(item["mae_dollars"] for item in values) if values else None,
                "max_up_excursion_pct": max((item["mfe_pct"] for item in values), default=None),
                "max_down_excursion_pct": min((item["mae_pct"] for item in values), default=None),
                "max_up_excursion_dollars": max((item["mfe_dollars"] for item in values), default=None),
                "max_down_excursion_dollars": min((item["mae_dollars"] for item in values), default=None),
                "mean_bar_range_pct": statistics.fmean(item["mean_bar_range_pct"] for item in values) if values else None,
                "median_bar_range_pct": statistics.median(item["median_bar_range_pct"] for item in values) if values else None,
            }
        valid_five = [
            row for row in rows
            if row.get("horizons", {}).get("5", {}).get("valid")
        ]
        group["first_pass_0_5_5d"] = dict(Counter(
            row["horizons"]["5"]["first_pass_0_5"] for row in valid_five
        ))
        group["recovery_0_5_5d"] = {
            "up_threshold_hits": sum(row["recovery_0_5"]["up_hit"] for row in valid_five),
            "up_then_fallback": sum(row["recovery_0_5"]["up_then_fallback_p0"] for row in valid_five),
            "down_threshold_hits": sum(row["recovery_0_5"]["down_hit"] for row in valid_five),
            "down_then_reclaim": sum(row["recovery_0_5"]["down_then_reclaim_p0"] for row in valid_five),
            "n_valid_5d": len(valid_five),
        }
        group["targets_5d"] = {
            name: {
                "hits": sum(row["targets_5d"][name]["hit"] for row in valid_five),
                "n_valid_5d": len(valid_five),
            }
            for name in ("plus_3", "plus_10")
        }
        output.append(group)
    return {"groups": output, "group_count": len(output)}


def run(focus_only: bool = False, data_root: Path | None = None) -> dict:
    rows = load_rows()
    raw_rows = load_rows(include_pre_session=True)
    signals, duplicate_count = canonical_buy_signals(rows)
    unsupported_years = sorted({
        signal.ts.astimezone(NY).year for signal in signals
        if signal.ts.astimezone(NY).year != 2026
    })
    if unsupported_years:
        raise RuntimeError(f"No pinned NYSE calendar in this study for years: {unsupported_years}")
    bars_by_day, coverage = load_qqq_rth_bars(data_root or option_wall_root())
    focus_all = [
        signal for signal in signals
        if signal.kind == "深蓝圈" and signal.level == 2 and signal.ts.astimezone(NY).date() in FOCUS_DATES
    ]
    focused_qqq = [signal for signal in focus_all if signal.symbol == "QQQ"]
    selected = focused_qqq if focus_only else signals
    analyzed = [analyze_signal(signal, bars_by_day) for signal in selected]
    if focus_only:
        return {
            "raw_rows_unfiltered": len(raw_rows),
            "canonical_rows": len(rows),
            "canonical_buy_signals_after_dedup": len(signals),
            "dedup_removed": duplicate_count,
            "qqq_rth_coverage": coverage,
            "focus_source_level2_deepblue_total": len(focus_all),
            "focus_qqq_count": len(focused_qqq),
            "focus_events": analyzed,
            "non_qqq_focus": [
                {
                    "source_ts_utc": signal.ts.isoformat(),
                    "source_ts_ny": signal.ts.astimezone(NY).isoformat(),
                    "symbol": signal.symbol,
                    "signal_id": signal.signal_id,
                }
                for signal in focus_all if signal.symbol != "QQQ"
            ],
        }
    valid_five = [event for event in analyzed if event.get("horizons", {}).get("5", {}).get("valid")]
    sensitivity = {}
    for threshold in ("0.25", "0.5", "1.0"):
        sensitivity[threshold] = dict(Counter(
            event.get("first_pass_sensitivity", {}).get(threshold, "incomplete")
            for event in valid_five
        ))
    recovery = {
        "up_threshold_hits": sum(bool(event.get("recovery_0_5", {}).get("up_hit")) for event in valid_five),
        "up_then_fallback": sum(bool(event.get("recovery_0_5", {}).get("up_then_fallback_p0")) for event in valid_five),
        "down_threshold_hits": sum(bool(event.get("recovery_0_5", {}).get("down_hit")) for event in valid_five),
        "down_then_reclaim": sum(bool(event.get("recovery_0_5", {}).get("down_then_reclaim_p0")) for event in valid_five),
    }
    return {
        "raw_rows_unfiltered": len(raw_rows),
        "canonical_rows": len(rows),
        "canonical_buy_signals_after_dedup": len(signals),
        "dedup_removed": duplicate_count,
        "qqq_rth_coverage": coverage,
        "focus_source_level2_deepblue_total": len(focus_all),
        "focus_qqq_count": len(focused_qqq),
        "focus_events": [analyze_signal(signal, bars_by_day) for signal in focused_qqq],
        "focus_non_qqq": [
            {"source_ts_utc": signal.ts.isoformat(), "symbol": signal.symbol, "signal_id": signal.signal_id}
            for signal in focus_all if signal.symbol != "QQQ"
        ],
        "all_buy_groups": aggregate(analyzed),
        "qqq_pooled_horizons": {
            horizon: pooled_horizon_summary(
                [event for event in analyzed if event["symbol"] == "QQQ"],
                horizon,
            )
            for horizon in ("1", "2", "3", "5")
        },
        "first_pass_5d_sensitivity": sensitivity,
        "recovery_0_5_5d": recovery,
        "all_buy_status_counts": dict(Counter(event["status"] for event in analyzed)),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--focus-only", action="store_true")
    parser.add_argument("--data-root", type=Path, default=None)
    args = parser.parse_args()
    print(json.dumps(run(args.focus_only, args.data_root), ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
