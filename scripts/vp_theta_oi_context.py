"""Bounded point-in-time QQQ OI context for the saved 22-trade VP sample.

This is an offline research reader.  It loads only the saved August and
September 2026 QQQ monthly OI partitions, keeps vendor publication timestamps,
and joins the saved VP trade rows without changing the VP trade stream.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.volume_profile_context_study import (  # noqa: E402
    GEX_ROWS_PATH,
    _join_gex,
    _load_gex_rows,
    _stats,
)


ET = ZoneInfo("America/New_York")
UTC = timezone.utc
VP_TRADES_PATH = Path(
    r"F:\ancserQuant\ancserMarketData\derived\research\volume_profile_context_mnq_mbo_gex.csv"
)
OI_ROOT = Path(
    r"F:\ancserQuant\ancserMarketData\source\options\thetadata\raw\QQQ\monthly_open_interest_dte_0_60"
)
OI_PATHS = (
    OI_ROOT / "month=2026-08.csv.gz",
    OI_ROOT / "month=2026-09.csv.gz",
)
MIN_SPLIT_MATCHES = 10
FEATURE_FIELDS = (
    "oi_unsigned_total",
    "oi_put_call_ratio",
    "oi_near_expiry_share_calendar_dte_le_1",
    "oi_strike_hhi",
)


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an aware vendor timestamp and normalize only the comparison copy to UTC."""
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        normalized = text.replace("Z", "+00:00")
        if len(normalized) >= 5 and normalized[-5] in "+-" and normalized[-3] != ":":
            normalized = normalized[:-2] + ":" + normalized[-2:]
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or ""))
    except ValueError:
        return None


def _safe_stats(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    materialized = list(rows)
    if not materialized:
        return {"n": 0, "pnl": 0.0, "pf": 0.0, "win_rate": 0.0, "max_dd": 0.0}
    return _stats(materialized)


def load_oi_rows(paths: Iterable[Path] = OI_PATHS) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read only the requested QQQ monthly partitions and preserve raw timestamps."""
    rows: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    invalid_rows = 0
    for path in paths:
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"OI partition missing: {path}")
        file_rows = 0
        raw_min: datetime | None = None
        raw_max: datetime | None = None
        with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as handle:
            for raw in csv.DictReader(handle):
                file_rows += 1
                symbol = str(raw.get("symbol") or "").upper()
                timestamp_raw = str(raw.get("timestamp") or "")
                timestamp_utc = _parse_timestamp(timestamp_raw)
                expiration = _parse_date(raw.get("expiration"))
                strike = _finite(raw.get("strike"))
                open_interest = _finite(raw.get("open_interest"))
                right = str(raw.get("right") or "").upper()
                if (
                    symbol != "QQQ"
                    or timestamp_utc is None
                    or expiration is None
                    or strike is None
                    or open_interest is None
                    or open_interest < 0
                    or right not in {"CALL", "PUT"}
                ):
                    invalid_rows += 1
                    continue
                raw_min = timestamp_utc if raw_min is None else min(raw_min, timestamp_utc)
                raw_max = timestamp_utc if raw_max is None else max(raw_max, timestamp_utc)
                rows.append(
                    {
                        "symbol": symbol,
                        "expiration": expiration,
                        "strike": strike,
                        "right": right,
                        "open_interest": open_interest,
                        "timestamp": timestamp_utc,
                        "timestamp_utc": timestamp_utc.isoformat(),
                        "timestamp_et_date": timestamp_utc.astimezone(ET).date(),
                        "timestamp_raw": timestamp_raw,
                        "source_path": str(path),
                    }
                )
        files.append(
            {
                "path": str(path),
                "rows": file_rows,
                "compressed_bytes": path.stat().st_size,
                "timestamp_min_utc": raw_min.isoformat() if raw_min else None,
                "timestamp_max_utc": raw_max.isoformat() if raw_max else None,
            }
        )
    return rows, {
        "files": files,
        "rows_read": sum(item["rows"] for item in files),
        "valid_qqq_rows": len(rows),
        "invalid_rows": invalid_rows,
        "zero_oi_rows": sum(row["open_interest"] == 0 for row in rows),
        "effective_session_status": "unknown_until_calendar_join",
        "expected_publication_time_et": "approximately 06:30 America/New_York",
    }


def _trade_rows(path: Path = VP_TRADES_PATH) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"VP trade rows missing: {path}")
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            entry_time = _parse_timestamp(raw.get("entry_time"))
            pnl = _finite(raw.get("pnl"))
            if entry_time is None or pnl is None:
                raise ValueError(f"invalid saved VP row: {raw}")
            trade_date = entry_time.astimezone(ET).date()
            rows.append(
                {
                    "trade_date": trade_date.isoformat(),
                    "entry_time_utc": entry_time.isoformat(),
                    "entry_time_et": entry_time.astimezone(ET).isoformat(),
                    "entry_epoch": int(entry_time.timestamp()),
                    "direction": str(raw.get("direction") or "unknown").lower(),
                    "direction_int": int(_finite(raw.get("direction_int")) or 0),
                    "edge": str(raw.get("edge") or "unknown"),
                    "setup": str(raw.get("setup") or "unknown"),
                    "period": str(raw.get("period") or "unknown"),
                    "pnl": round(pnl, 2),
                    "saved_gex_oi_label": str(raw.get("gex_oi_label") or "") or None,
                }
            )
    rows.sort(key=lambda row: row["entry_time_utc"])
    clusters = Counter(row["trade_date"] for row in rows)
    for row in rows:
        row["trades_on_trade_date"] = clusters[row["trade_date"]]
    return rows


def _dedupe_latest(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[tuple[str, float, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["expiration"].isoformat(), row["strike"], row["right"])
        previous = latest.get(key)
        if previous is None or row["timestamp"] > previous["timestamp"]:
            latest[key] = row
    return list(latest.values())


def _structure_metrics(rows: list[dict[str, Any]], trade_date: date) -> dict[str, Any]:
    active = [row for row in rows if row["expiration"] >= trade_date]
    expired = len(rows) - len(active)
    total_oi = sum(row["open_interest"] for row in active)
    call_oi = sum(row["open_interest"] for row in active if row["right"] == "CALL")
    put_oi = sum(row["open_interest"] for row in active if row["right"] == "PUT")
    near_oi = sum(
        row["open_interest"]
        for row in active
        if 0 <= (row["expiration"] - trade_date).days <= 1
    )
    strike_oi: defaultdict[float, float] = defaultdict(float)
    for row in active:
        strike_oi[row["strike"]] += row["open_interest"]
    weights = [value / total_oi for value in strike_oi.values() if total_oi > 0]
    return {
        "oi_active_contract_rows": len(active),
        "oi_expired_rows_excluded": expired,
        "oi_zero_rows": sum(row["open_interest"] == 0 for row in active),
        "oi_unsigned_total": round(total_oi, 4),
        "oi_call_total": round(call_oi, 4),
        "oi_put_total": round(put_oi, 4),
        "oi_put_call_ratio": round(put_oi / call_oi, 8) if call_oi > 0 else None,
        "oi_near_expiry_share_calendar_dte_le_1": round(near_oi / total_oi, 8) if total_oi > 0 else None,
        "oi_strike_hhi": round(sum(weight * weight for weight in weights), 8) if weights else None,
        "oi_metrics_status": "matched_zero_total" if total_oi == 0 else "matched",
    }


def join_oi_context(
    trade: dict[str, Any],
    oi_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Join one trade using only rows published by its entry instant.

    A same-ET-date publication is preferred.  When that date has no eligible
    publication, the latest earlier ET publication is labelled stale and kept
    separate from same-date rows.  No later same-date row is used.
    """
    entry = _parse_timestamp(trade["entry_time_utc"])
    assert entry is not None
    trade_date = date.fromisoformat(trade["trade_date"])
    by_date: defaultdict[date, list[dict[str, Any]]] = defaultdict(list)
    for row in oi_rows:
        by_date[row["timestamp_et_date"]].append(row)
    same_date_rows = by_date.get(trade_date, [])
    eligible_same = [row for row in same_date_rows if row["timestamp"] <= entry]
    future_same = sum(row["timestamp"] > entry for row in same_date_rows)
    if eligible_same:
        snapshot_date = trade_date
        status = "same_date"
        eligible = eligible_same
    else:
        prior_dates = sorted(
            candidate
            for candidate, rows in by_date.items()
            if candidate < trade_date and any(row["timestamp"] <= entry for row in rows)
        )
        if not prior_dates:
            return {
                **trade,
                "oi_join_status": "missing",
                "oi_snapshot_date_et": None,
                "oi_publication_min_utc": None,
                "oi_publication_max_utc": None,
                "oi_publication_raw_min": None,
                "oi_publication_raw_max": None,
                "oi_future_same_date_rows_excluded": future_same,
                "oi_selected_rows": 0,
                "oi_source_months": [],
                "oi_stale_days": None,
                "oi_missing_reason": "no publication timestamp <= entry in bounded Aug/Sep files",
            }
        snapshot_date = prior_dates[-1]
        status = "prior_date_stale"
        eligible = [row for row in by_date[snapshot_date] if row["timestamp"] <= entry]

    selected = _dedupe_latest(eligible)
    timestamps = [row["timestamp"] for row in selected]
    raw_by_time = sorted(selected, key=lambda row: row["timestamp"])
    result = {
        **trade,
        "oi_join_status": status,
        "oi_snapshot_date_et": snapshot_date.isoformat(),
        "oi_publication_min_utc": min(timestamps).isoformat() if timestamps else None,
        "oi_publication_max_utc": max(timestamps).isoformat() if timestamps else None,
        "oi_publication_raw_min": raw_by_time[0]["timestamp_raw"] if raw_by_time else None,
        "oi_publication_raw_max": raw_by_time[-1]["timestamp_raw"] if raw_by_time else None,
        "oi_future_same_date_rows_excluded": future_same,
        "oi_selected_rows": len(selected),
        "oi_source_months": sorted({Path(row["source_path"]).stem.replace("month=", "").replace(".csv", "") for row in selected}),
        "oi_stale_days": (trade_date - snapshot_date).days,
        "oi_missing_reason": None,
    }
    result.update(_structure_metrics(selected, trade_date))
    return result


def _attach_old_gex(rows: list[dict[str, Any]], gex_path: Path) -> list[dict[str, Any]]:
    gex_rows = _load_gex_rows(gex_path)
    attached: list[dict[str, Any]] = []
    for row in rows:
        joined = _join_gex(row, gex_rows)
        snapshot = (gex_rows.get(row["trade_date"]) or {}).get("oi")
        if joined.get("gex_oi_label"):
            status = "matched"
        elif snapshot and row["entry_epoch"] < int(snapshot["gex_snapshot_epoch"]):
            status = "future_snapshot_excluded"
        else:
            status = "missing"
        attached.append(
            {
                **row,
                "old_gex_oi_status": status,
                "old_gex_oi_label": joined.get("gex_oi_label"),
                "old_gex_oi_snapshot_utc": (
                    datetime.fromtimestamp(int(snapshot["gex_snapshot_epoch"]), tz=UTC).isoformat()
                    if snapshot
                    else None
                ),
                "old_gex_saved_available": bool(row.get("saved_gex_oi_label")),
            }
        )
    return attached


def _group_summary(rows: list[dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    groups: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(field) or "unknown")].append(row)
    return {
        key: {
            "trades": len(group),
            "independent_dates": len({row["trade_date"] for row in group}),
            "directions": dict(Counter(row["direction"] for row in group)),
            "stats": _safe_stats(group),
        }
        for key, group in sorted(groups.items())
    }


def _date_cluster_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(row["trade_date"] for row in rows)
    return {
        "trade_count": len(rows),
        "independent_dates": len(counts),
        "single_trade_dates": sum(count == 1 for count in counts.values()),
        "multi_trade_dates": sum(count > 1 for count in counts.values()),
        "trade_counts_by_date": dict(sorted(counts.items())),
    }


def _median_split(rows: list[dict[str, Any]]) -> dict[str, Any]:
    matched = [
        row for row in rows
        if row.get("oi_join_status") in {"same_date", "prior_date_stale"}
    ]
    matched.sort(key=lambda row: row["entry_time_utc"])
    result: dict[str, Any] = {
        "status": "ready" if len(matched) >= MIN_SPLIT_MATCHES else "insufficient_matched_sample",
        "predeclared_minimum_matched": MIN_SPLIT_MATCHES,
        "matched_trades": len(matched),
        "first_half_trades": 0,
        "later_half_trades": 0,
        "features": {},
    }
    if len(matched) < MIN_SPLIT_MATCHES:
        return result
    cut = len(matched) // 2
    first_half = matched[:cut]
    later_half = matched[cut:]
    result["first_half_trades"] = len(first_half)
    result["later_half_trades"] = len(later_half)
    for field in FEATURE_FIELDS:
        training = [row[field] for row in first_half if row.get(field) is not None]
        evaluation = [row for row in later_half if row.get(field) is not None]
        if not training:
            result["features"][field] = {"status": "missing_training_values"}
            continue
        threshold = float(median(training))
        low = [row for row in evaluation if float(row[field]) <= threshold]
        high = [row for row in evaluation if float(row[field]) > threshold]
        result["features"][field] = {
            "status": "evaluated",
            "training_median": round(threshold, 8),
            "training_rows": len(training),
            "later_half_rows_with_value": len(evaluation),
            "later_half_low_or_equal": {"n": len(low), "dates": len({row["trade_date"] for row in low}), "stats": _safe_stats(low)},
            "later_half_high": {"n": len(high), "dates": len({row["trade_date"] for row in high}), "stats": _safe_stats(high)},
        }
    return result


def build_context(
    vp_path: Path = VP_TRADES_PATH,
    oi_paths: Iterable[Path] = OI_PATHS,
    gex_path: Path = GEX_ROWS_PATH,
) -> dict[str, Any]:
    oi_rows, oi_meta = load_oi_rows(oi_paths)
    trades = _trade_rows(vp_path)
    rows = _attach_old_gex(
        [join_oi_context(trade, oi_rows) for trade in trades],
        Path(gex_path),
    )
    rows.sort(key=lambda row: row["entry_time_utc"])
    return {
        "study": "bounded QQQ monthly OI context for saved MNQ VP trades",
        "version": "2026-09-22-vp-theta-oi-context-v1",
        "input_paths": {
            "vp_trade_rows": str(vp_path),
            "oi_partitions": [str(Path(path)) for path in oi_paths],
            "older_gex_rows": str(gex_path),
        },
        "join_policy": {
            "timestamp_comparison": "actual aware vendor publication timestamp normalized to UTC <= entry UTC",
            "date_preference": "same America/New_York publication date as the trade; latest earlier ET date is retained as prior_date_stale fallback",
            "future_rows": "same-date rows after entry are excluded",
            "effective_session": "unknown_until_calendar_join; no weekend/holiday shift inferred",
            "oi_universe": "QQQ monthly partial-chain DTE 0-60 rows with expiration >= trade date",
            "dealer_sign": "none; OI is unsigned and put/call labels are contract-right totals",
            "zero_oi": "retained as zero values and counted separately",
            "near_expiry": "calendar-date expiration minus trade date <= 1 and >= 0; no exchange-calendar inference",
            "strike_hhi": "sum of squared strike OI shares after combining calls and puts by strike",
        },
        "oi_input": oi_meta,
        "trade_inventory": {
            "trades": len(rows),
            "independent_dates": len({row["trade_date"] for row in rows}),
            "directions": dict(Counter(row["direction"] for row in rows)),
            "date_clusters": _date_cluster_summary(rows),
            "saved_baseline_stats": _safe_stats(rows),
        },
        "availability": {
            "oi_join_status": _group_summary(rows, "oi_join_status"),
            "old_gex_oi_status": _group_summary(rows, "old_gex_oi_status"),
            "oi_status_x_old_gex_status": _group_summary(
                [{**row, "join": f"{row['oi_join_status']}__{row['old_gex_oi_status']}"} for row in rows],
                "join",
            ),
        },
        "median_split": _median_split(rows),
        "trades": rows,
        "interpretation": "Descriptive saved-row context only; no threshold optimization, dealer sign, live gate, or improvement claim.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vp-trades", type=Path, default=VP_TRADES_PATH)
    parser.add_argument("--oi-august", type=Path, default=OI_PATHS[0])
    parser.add_argument("--oi-september", type=Path, default=OI_PATHS[1])
    parser.add_argument("--old-gex", type=Path, default=GEX_ROWS_PATH)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    result = build_context(args.vp_trades, (args.oi_august, args.oi_september), args.old_gex)
    print(json.dumps(result, ensure_ascii=False, indent=None if args.compact else 2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
