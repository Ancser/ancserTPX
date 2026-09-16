"""Reconcile the 1-minute MES/MNQ stores from Databento and TopstepX.

The canonical policy is deliberately narrow:

* Databento supplies 2026 history older than the recent 31-day window.
* TopstepX wins in that recent window when it returns a bar; this includes the
  current calendar month and preserves the broker's latest OHLC revisions.
* A missing minute is never synthesized.
* Databento continuous data is adjusted with the observed instrument-id roll
  seams before it is merged.  A price jump alone is never considered a roll.

The script is report-only unless ``--apply`` is provided.  An apply run backs
up both accumulated stores and their sidecars before the first atomic write.
The paid Databento request is quoted first and remains subscription-only by
default (``--max-cost 0``).

Examples::

    python scripts/reconcile_futures_data.py
    python scripts/reconcile_futures_data.py --apply
    python scripts/reconcile_futures_data.py --apply --max-cost 1.00
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import math
import os
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend.data import candle_store, market_data  # noqa: E402
from backend.data.futures_data import (  # noqa: E402
    FUTURES_DATA_SCHEMA,
    SOURCE_DATABENTO,
    SOURCE_TOPSTEPX,
    flatten_rolls,
    merge_futures_by_policy,
    normalize_futures_candle,
)
from backend.db.models import BarUnit, Candle  # noqa: E402
from backend.timebase import UTC, as_utc, utc_now  # noqa: E402
from backend.broker.topstepx import TopstepXClient  # noqa: E402


DATASET = "GLBX.MDP3"
SCHEMA = "ohlcv-1m"
SYMBOLS = ("MES", "MNQ")
ROLL_SYMBOL_SUFFIX = ".v.0"
RECENT_WINDOW = timedelta(days=31)
MIN_ALIGNMENT_SAMPLES = 100
MIN_ALIGNMENT_AGREEMENT = 0.95
PRICE_FIELDS = ("open", "high", "low", "close")


def _parse_instant(value: object) -> datetime:
    """Parse an ISO instant, including Databento's nine-digit fractions."""
    if isinstance(value, datetime):
        return as_utc(value)
    text = str(value or "").strip().replace("Z", "+00:00")
    if "." in text:
        head, tail = text.split(".", 1)
        suffix = ""
        for marker in ("+", "-"):
            position = tail.find(marker)
            if position >= 0:
                suffix = tail[position:]
                tail = tail[:position]
                break
        text = head + "." + tail[:6] + suffix
    return as_utc(datetime.fromisoformat(text))


def _minute_floor(value: datetime) -> datetime:
    return as_utc(value).replace(second=0, microsecond=0)


def _iso(value: datetime) -> str:
    return as_utc(value).isoformat()


def _query(symbol: str, start: datetime, end: datetime) -> dict[str, Any]:
    return {
        "dataset": DATASET,
        "symbols": [f"{symbol}{ROLL_SYMBOL_SUFFIX}"],
        "schema": SCHEMA,
        "stype_in": "continuous",
        "start": _iso(start),
        "end": _iso(end),
    }


def _available_end(client: Any, now: datetime) -> datetime:
    payload = client.metadata.get_dataset_range(DATASET)
    schema_range = (payload.get("schema") or {}).get(SCHEMA) or {}
    raw_end = schema_range.get("end") or payload.get("end")
    if not raw_end:
        raise RuntimeError("Databento did not return an available dataset end")
    return min(_minute_floor(_parse_instant(raw_end)), _minute_floor(now))


def _load_existing(symbol: str) -> list[Candle]:
    """Load the pickle plus a snapshot of the live pending journal.

    The journal is intentionally not cleared here.  A running desktop app may
    append to it while reconciliation is in progress; leaving it in place
    makes the next normal merge lossless.
    """
    persisted = candle_store.load(symbol, 1, use_cache=False)
    pending = candle_store._read_pending_locked(symbol, 1)
    by_timestamp: dict[datetime, Candle] = {}
    for candle in persisted + pending:
        by_timestamp[as_utc(candle.timestamp)] = candle
    return sorted(by_timestamp.values(), key=lambda candle: as_utc(candle.timestamp))


def _frame_to_databento_candles(
    frame: Any,
    symbol: str,
) -> tuple[list[Candle], list[tuple[datetime, float, int, int]], list[str]]:
    """Convert a Databento frame and flatten its instrument-id roll seams."""
    from backend.data.futures_data import find_rolls

    rolls = find_rolls(frame)
    raw: list[Candle] = []
    invalid: list[str] = []
    for timestamp, row in frame.iterrows():
        try:
            if hasattr(timestamp, "to_pydatetime"):
                timestamp = timestamp.to_pydatetime()
            candle = Candle(
                timestamp=as_utc(timestamp),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=int(row["volume"]),
                symbol=symbol,
                interval="1m",
                source=SOURCE_DATABENTO,
            )
            raw.append(normalize_futures_candle(
                candle, symbol=symbol, source=SOURCE_DATABENTO,
            ))
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            invalid.append(f"{timestamp}: {exc}")

    flattened = flatten_rolls(raw, rolls)
    by_timestamp: dict[datetime, Candle] = {}
    for candle in flattened:
        by_timestamp[as_utc(candle.timestamp)] = candle
    return (
        sorted(by_timestamp.values(), key=lambda candle: as_utc(candle.timestamp)),
        rolls,
        invalid,
    )


def _roll_report(
    bars: list[Candle],
    rolls: Iterable[tuple[datetime, float, int, int]],
) -> list[dict[str, Any]]:
    ordered = sorted(bars, key=lambda candle: as_utc(candle.timestamp))
    output: list[dict[str, Any]] = []
    for seam, jump, previous, current in rolls:
        before = [c for c in ordered if as_utc(c.timestamp) < as_utc(seam)]
        after = [c for c in ordered if as_utc(c.timestamp) >= as_utc(seam)]
        previous_bar = before[-1] if before else None
        next_bar = after[0] if after else None
        residual = None
        if previous_bar is not None and next_bar is not None:
            residual = round(
                float(next_bar.open) - float(previous_bar.close), 4,
            )
        output.append({
            "timestamp": _iso(seam),
            "previous_instrument_id": int(previous),
            "current_instrument_id": int(current),
            "raw_open_minus_previous_close": round(float(jump), 4),
            "adjusted_adjacent_open_minus_previous_close": residual,
        })
    return output


def _shift_bars(bars: Iterable[Candle], offset: float) -> list[Candle]:
    if abs(offset) < 1e-12:
        return list(bars)
    from dataclasses import replace

    return [
        replace(
            candle,
            open=candle.open + offset,
            high=candle.high + offset,
            low=candle.low + offset,
            close=candle.close + offset,
        )
        for candle in bars
    ]


def _estimate_recent_alignment(
    databento_bars: list[Candle],
    topstep_bars: list[Candle],
    recent_cutoff: datetime,
) -> dict[str, Any]:
    """Measure a fixed current-anchor offset without guessing from old bars."""
    db = {
        as_utc(candle.timestamp): candle
        for candle in databento_bars
        if as_utc(candle.timestamp) >= as_utc(recent_cutoff)
    }
    top = {
        as_utc(candle.timestamp): candle
        for candle in topstep_bars
        if as_utc(candle.timestamp) >= as_utc(recent_cutoff)
    }
    common = sorted(set(db).intersection(top))
    differences = [round(top[t].close - db[t].close, 4) for t in common]
    counts = collections.Counter(differences)
    mode, hits = counts.most_common(1)[0] if counts else (None, 0)
    agreement = hits / len(differences) if differences else 0.0
    applied = bool(
        mode is not None
        and len(differences) >= MIN_ALIGNMENT_SAMPLES
        and agreement >= MIN_ALIGNMENT_AGREEMENT
        and math.isfinite(float(mode))
    )
    return {
        "common_bars": len(common),
        "mode_topstep_minus_databento": mode,
        "agreement": round(agreement, 6),
        "minimum_samples": MIN_ALIGNMENT_SAMPLES,
        "minimum_agreement": MIN_ALIGNMENT_AGREEMENT,
        "applied": applied,
    }


def _compare_sources(
    databento_bars: list[Candle],
    topstep_bars: list[Candle],
    recent_cutoff: datetime,
) -> dict[str, Any]:
    db = {
        as_utc(candle.timestamp): candle
        for candle in databento_bars
        if as_utc(candle.timestamp) >= as_utc(recent_cutoff)
    }
    top = {
        as_utc(candle.timestamp): candle
        for candle in topstep_bars
        if as_utc(candle.timestamp) >= as_utc(recent_cutoff)
    }
    common = sorted(set(db).intersection(top))
    price_diffs = [
        round(top[t].close - db[t].close, 4)
        for t in common
    ]
    ohlcv_conflicts = 0
    for timestamp in common:
        left = db[timestamp]
        right = top[timestamp]
        if (
            any(getattr(left, field) != getattr(right, field) for field in PRICE_FIELDS)
            or left.volume != right.volume
        ):
            ohlcv_conflicts += 1
    counts = collections.Counter(price_diffs)
    return {
        "common_bars": len(common),
        "ohlcv_conflicts": ohlcv_conflicts,
        "exact_ohlcv_matches": len(common) - ohlcv_conflicts,
        "close_difference_mode": counts.most_common(5),
        "close_difference_abs_max": (
            round(max((abs(value) for value in price_diffs), default=0.0), 4)
        ),
    }


def _source_counts(bars: Iterable[Candle], start: datetime) -> dict[str, int]:
    counts = collections.Counter(
        str(getattr(candle, "source", "unknown") or "unknown")
        for candle in bars
        if as_utc(candle.timestamp) >= as_utc(start)
    )
    return dict(sorted(counts.items()))


def _source_transitions(bars: Iterable[Candle], start: datetime) -> list[dict[str, str]]:
    ordered = sorted(
        (candle for candle in bars if as_utc(candle.timestamp) >= as_utc(start)),
        key=lambda candle: as_utc(candle.timestamp),
    )
    transitions: list[dict[str, str]] = []
    previous = None
    for candle in ordered:
        source = str(getattr(candle, "source", "unknown") or "unknown")
        if source != previous:
            transitions.append({
                "timestamp": _iso(candle.timestamp),
                "source": source,
            })
            previous = source
    return transitions


def _backup_store(symbols: Iterable[str], stamp: str) -> Path:
    destination = market_data.archive_path(f"futures_reconciliation_{stamp}")
    destination.mkdir(parents=True, exist_ok=True)
    store_root = market_data.candle_store_dir()
    for symbol in symbols:
        prefix = f"{symbol}_accumulated_1m"
        for path in store_root.glob(prefix + "*"):
            if not path.is_file():
                continue
            shutil.copy2(path, destination / path.name)
    return destination


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


def _remove_resolved_roll_seams(
    symbol: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """Remove old active bad-seam flags now covered by Databento adjustment."""
    meta = candle_store.load_meta(symbol, 1)
    kept: list[dict[str, Any]] = []
    resolved: list[dict[str, Any]] = []
    for seam in meta.get("known_seams", []) or []:
        kind = str(seam.get("kind") or "")
        raw_timestamp = seam.get("timestamp")
        try:
            timestamp = _parse_instant(raw_timestamp)
        except (TypeError, ValueError, OverflowError):
            timestamp = None
        if (
            kind == "roll_anchor_mismatch"
            and timestamp is not None
            and as_utc(start) <= timestamp <= as_utc(end)
        ):
            resolved.append(seam)
        else:
            kept.append(seam)
    if resolved:
        meta["known_seams"] = kept
        candle_store.save_meta(meta, symbol, 1)
    return resolved


async def _fetch_topstep_recent(
    start: datetime,
    end: datetime,
) -> tuple[dict[str, list[Candle]], dict[str, str], list[str]]:
    """Fetch the recent broker window with one authenticated client."""
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    username = os.environ.get("TOPSTEPX_USERNAME", "").strip()
    api_key = os.environ.get("TOPSTEPX_API_KEY", "").strip()
    if not username or not api_key:
        return {}, {}, ["TopstepX credentials are not configured"]

    use_demo = os.environ.get("TOPSTEPX_USE_DEMO", "false").strip().lower() == "true"
    client = TopstepXClient(username, api_key, use_demo=use_demo)
    output: dict[str, list[Candle]] = {}
    contracts: dict[str, str] = {}
    errors: list[str] = []
    try:
        await client.authenticate()
        for symbol in SYMBOLS:
            try:
                contract = await client.get_front_month_contract_id(symbol)
                contracts[symbol] = contract
                bars = await client.get_historical_bars_paginated(
                    contract_id=contract,
                    unit=BarUnit.MINUTE,
                    unit_number=1,
                    start_time=_iso(start),
                    end_time=_iso(end + timedelta(minutes=1)),
                )
                normalized: list[Candle] = []
                for candle in bars:
                    try:
                        normalized.append(normalize_futures_candle(
                            candle, symbol=symbol, source=SOURCE_TOPSTEPX,
                        ))
                    except (TypeError, ValueError, OverflowError) as exc:
                        errors.append(f"TopstepX {symbol} {candle.timestamp}: {exc}")
                output[symbol] = sorted(
                    {
                        as_utc(candle.timestamp): candle
                        for candle in normalized
                        if as_utc(candle.timestamp) >= as_utc(start)
                        and as_utc(candle.timestamp) <= as_utc(end)
                    }.values(),
                    key=lambda candle: as_utc(candle.timestamp),
                )
            except Exception as exc:  # one product must not hide the other
                errors.append(f"TopstepX {symbol}: {type(exc).__name__}: {exc}")
                output[symbol] = []
    finally:
        try:
            await client.disconnect()
        except Exception as exc:
            errors.append(f"TopstepX disconnect: {type(exc).__name__}: {exc}")
    return output, contracts, errors


def _fetch_databento(
    client: Any,
    start: datetime,
    end: datetime,
) -> tuple[dict[str, list[Candle]], dict[str, list[dict[str, Any]]], list[str]]:
    output: dict[str, list[Candle]] = {}
    rolls: dict[str, list[dict[str, Any]]] = {}
    errors: list[str] = []
    for symbol in SYMBOLS:
        request = _query(symbol, start, end)
        try:
            frame = client.timeseries.get_range(**request).to_df().sort_index()
            bars, detected, invalid = _frame_to_databento_candles(frame, symbol)
            output[symbol] = bars
            rolls[symbol] = _roll_report(bars, detected)
            errors.extend(f"Databento {symbol}: {item}" for item in invalid)
        except Exception as exc:
            errors.append(f"Databento {symbol}: {type(exc).__name__}: {exc}")
            output[symbol] = []
            rolls[symbol] = []
    return output, rolls, errors


def _quote_databento(client: Any, start: datetime, end: datetime) -> dict[str, Any]:
    quotes: dict[str, Any] = {}
    total_cost = 0.0
    total_records = 0
    for symbol in SYMBOLS:
        request = _query(symbol, start, end)
        count = int(client.metadata.get_record_count(**request))
        cost = float(client.metadata.get_cost(**request))
        quotes[symbol] = {
            "request": request,
            "record_count": count,
            "quoted_cost_usd": cost,
        }
        total_records += count
        total_cost += cost
    return {
        "symbols": quotes,
        "total_records": total_records,
        "total_cost_usd": round(total_cost, 8),
    }


def _merge_one_symbol(
    symbol: str,
    existing: list[Candle],
    databento_bars: list[Candle],
    topstep_bars: list[Candle],
    *,
    start: datetime,
    recent_cutoff: datetime,
    report_end: datetime,
) -> tuple[list[Candle], dict[str, Any]]:
    valid_existing: list[Candle] = []
    invalid_existing: list[str] = []
    for candle in existing:
        timestamp = as_utc(candle.timestamp)
        if timestamp < as_utc(start) or timestamp > as_utc(report_end):
            valid_existing.append(candle)
            continue
        try:
            valid_existing.append(normalize_futures_candle(
                candle,
                symbol=symbol,
                source=str(getattr(candle, "source", SOURCE_TOPSTEPX) or SOURCE_TOPSTEPX),
            ))
        except (TypeError, ValueError, OverflowError) as exc:
            invalid_existing.append(f"{timestamp.isoformat()}: {exc}")

    incoming = list(databento_bars) + list(topstep_bars)
    merged = merge_futures_by_policy(
        valid_existing,
        incoming,
        recent_cutoff=recent_cutoff,
        start=start,
    )
    existing_by_ts = {as_utc(c.timestamp): c for c in valid_existing}
    added = 0
    replaced = 0
    source_changed = 0
    for candle in merged:
        old = existing_by_ts.get(as_utc(candle.timestamp))
        if old is None:
            added += 1
        elif old is not candle:
            if any(getattr(old, field) != getattr(candle, field) for field in PRICE_FIELDS) \
                    or old.volume != candle.volume:
                replaced += 1
            if getattr(old, "source", None) != getattr(candle, "source", None):
                source_changed += 1
    return merged, {
        "existing_bars_including_pending": len(existing),
        "invalid_existing_removed": invalid_existing,
        "databento_candidates": len(databento_bars),
        "topstepx_candidates": len(topstep_bars),
        "added_bars": added,
        "revised_bars": replaced,
        "source_changed_bars": source_changed,
        "final_bars": len(merged),
        "final_source_counts_2026": _source_counts(merged, start),
        "source_transitions_2026": _source_transitions(merged, start),
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    key = os.environ.get("DATABENTO_API_KEY", "").strip()
    if not key:
        raise RuntimeError("DATABENTO_API_KEY is missing from .env")
    try:
        import databento as db
    except ImportError as exc:
        raise RuntimeError("databento package is not installed") from exc

    now = _minute_floor(utc_now())
    start = _minute_floor(_parse_instant(args.start))
    if start >= now:
        raise ValueError("--start must be earlier than the current time")
    client = db.Historical(key)
    available_end = _available_end(client, now)
    requested_end = _minute_floor(_parse_instant(args.end)) if args.end else available_end
    data_end = min(available_end, requested_end)
    if data_end <= start:
        raise ValueError("Databento available range does not overlap --start")
    recent_cutoff = max(start, _minute_floor(now - RECENT_WINDOW))

    quotes = _quote_databento(client, start, data_end)
    print(
        f"Databento {DATASET} | {start.isoformat()} -> {data_end.isoformat()} "
        f"(end exclusive)"
    )
    print(
        f"Recent TopstepX window: {recent_cutoff.isoformat()} -> {now.isoformat()} "
        f"({RECENT_WINDOW.days} days)"
    )
    print(
        f"Quoted {quotes['total_records']:,} records | "
        f"${quotes['total_cost_usd']:.8f}"
    )
    if quotes["total_cost_usd"] > float(args.max_cost) + 1e-9:
        raise RuntimeError(
            f"Databento quote ${quotes['total_cost_usd']:.8f} exceeds "
            f"--max-cost ${float(args.max_cost):.8f}"
        )

    report: dict[str, Any] = {
        "schema": FUTURES_DATA_SCHEMA,
        "generated_at": _iso(utc_now()),
        "apply": bool(args.apply),
        "symbols": list(SYMBOLS),
        "policy": {
            "recent_window_days": RECENT_WINDOW.days,
            "recent_cutoff": _iso(recent_cutoff),
            "recent_source_priority": [SOURCE_TOPSTEPX, SOURCE_DATABENTO],
            "historical_source_priority": [SOURCE_DATABENTO, SOURCE_TOPSTEPX],
            "timestamp": "aware UTC exact minute",
            "missing_minutes": "not fabricated",
            "databento_roll_rule": "instrument_id change only; additive back-adjustment",
        },
        "databento": quotes,
        "available_end": _iso(available_end),
        "errors": [],
        "symbols_report": {},
    }

    if not args.apply:
        report["status"] = "plan_only"
        report_path = market_data.derived_path("research", "futures_reconciliation_latest.json")
        _write_json(report_path, report)
        print(f"Plan only; report written to {report_path}")
        return report

    databento_bars, roll_reports, db_errors = _fetch_databento(client, start, data_end)
    topstep_bars, contracts, top_errors = await _fetch_topstep_recent(
        recent_cutoff, now,
    )
    report["databento_rolls"] = roll_reports
    report["topstepx_contracts"] = contracts
    report["errors"].extend(db_errors)
    report["errors"].extend(top_errors)

    archive_stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    archive_path: Optional[Path] = None
    existing_by_symbol: dict[str, list[Candle]] = {}
    merged_by_symbol: dict[str, list[Candle]] = {}
    merge_stats: dict[str, dict[str, Any]] = {}
    for symbol in SYMBOLS:
        existing = _load_existing(symbol)
        existing_by_symbol[symbol] = existing

        alignment = _estimate_recent_alignment(
            databento_bars[symbol], topstep_bars.get(symbol, []), recent_cutoff,
        )
        if alignment["applied"]:
            offset = float(alignment["mode_topstep_minus_databento"])
            databento_bars[symbol] = _shift_bars(databento_bars[symbol], offset)
            alignment["applied_offset"] = offset
        else:
            alignment["applied_offset"] = 0.0
        report["symbols_report"].setdefault(symbol, {})["recent_alignment"] = alignment
        report["symbols_report"][symbol]["recent_overlap"] = _compare_sources(
            databento_bars[symbol], topstep_bars.get(symbol, []), recent_cutoff,
        )

        report_end = max(
            [as_utc(c.timestamp) for c in databento_bars[symbol]]
            + [as_utc(c.timestamp) for c in topstep_bars.get(symbol, [])]
            + [as_utc(c.timestamp) for c in existing]
        )
        merged, stats = _merge_one_symbol(
            symbol,
            existing,
            databento_bars[symbol],
            topstep_bars.get(symbol, []),
            start=start,
            recent_cutoff=recent_cutoff,
            report_end=report_end,
        )
        merged_by_symbol[symbol] = merged
        merge_stats[symbol] = stats
        report["symbols_report"][symbol]["merge"] = stats

        # Only the previous active roll-anchor mismatch is resolved here. Other
        # provenance entries remain untouched and are still useful evidence.
        report["symbols_report"][symbol]["resolved_known_seams"] = []

    # A complete backup is made before either pickle is replaced.
    archive_path = _backup_store(SYMBOLS, archive_stamp)
    report["backup"] = str(archive_path)
    backup_files = {
        symbol: archive_path / f"{symbol}_accumulated_1m.pkl"
        for symbol in SYMBOLS
    }
    try:
        for symbol in SYMBOLS:
            candle_store.save(merged_by_symbol[symbol], symbol, 1)
            resolved = _remove_resolved_roll_seams(
                symbol,
                start,
                max(as_utc(c.timestamp) for c in merged_by_symbol[symbol]),
            )
            report["symbols_report"][symbol]["resolved_known_seams"] = resolved
            meta = candle_store.load_meta(symbol, 1)
            meta["data_standard"] = FUTURES_DATA_SCHEMA
            meta["last_reconciliation_report"] = "derived/research/futures_reconciliation_latest.json"
            candle_store.save_meta(meta, symbol, 1)
    except Exception:
        # Atomic save normally leaves the original untouched, but restore the
        # complete file if a later metadata/mirror operation failed.
        for symbol, backup in backup_files.items():
            target = market_data.candle_store_dir() / backup.name
            if backup.is_file():
                shutil.copy2(backup, target)
        raise

    for symbol in SYMBOLS:
        final = merged_by_symbol[symbol]
        target = [
            candle for candle in final
            if as_utc(candle.timestamp) >= as_utc(start)
        ]
        gaps = candle_store.detect_gaps(target, tolerance_min=3)
        report["symbols_report"][symbol]["post_apply"] = {
            "range": [
                _iso(final[0].timestamp) if final else None,
                _iso(final[-1].timestamp) if final else None,
            ],
            "total_bars": len(final),
            "source_counts_2026": _source_counts(final, start),
            "unexpected_gaps_2026": [
                {"start": _iso(gap[0]), "end": _iso(gap[1]), "minutes": gap[2]}
                for gap in gaps
            ],
            "rolls": roll_reports.get(symbol, []),
        }
    report["status"] = "applied"
    report_path = market_data.derived_path("research", "futures_reconciliation_latest.json")
    _write_json(report_path, report)
    print(f"Applied; report written to {report_path}")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-01-01T00:00:00Z")
    parser.add_argument(
        "--end",
        default="",
        help="exclusive Databento end; default is the provider's available end",
    )
    parser.add_argument("--apply", action="store_true", help="download and write canonical stores")
    parser.add_argument("--max-cost", type=float, default=0.0)
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

