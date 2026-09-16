"""Shared receive, roll-normalisation, and source-priority rules for futures.

The application stores one canonical 1-minute series per product.  Every
ingestion path must therefore agree on the same small contract:

* timestamps are aware UTC instants on an exact minute boundary;
* the symbol is the product root (``MES`` or ``MNQ``), never a contract alias;
* OHLCV is real source data; missing minutes are not fabricated;
* Databento continuous data is back-adjusted from the observed
  ``instrument_id`` changes, never from a price-jump threshold;
* source preference is time-dependent: TopstepX is preferred for the recent
  window, while Databento is preferred for older history.

The last rule is intentionally explicit rather than hidden in a caller.  It
prevents a fresh but differently anchored broker response from silently
rewriting the frozen historical series, while still allowing the current
month/recent tail to use the live broker's final OHLC values.
"""
from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime
from typing import Any, Iterable, List, Optional, Tuple

from backend.db.models import Candle
from backend.timebase import UTC, as_utc


FUTURES_DATA_SCHEMA = "futures-1m-v1"
SOURCE_DATABENTO = "databento"
SOURCE_TOPSTEPX = "topstepx"
SUPPORTED_FUTURES = frozenset(("MES", "MNQ"))


def normalize_symbol(value: object) -> str:
    """Return a supported product root or raise instead of guessing."""
    symbol = str(value or "").strip().upper().replace("/", "")
    if symbol not in SUPPORTED_FUTURES:
        raise ValueError(f"unsupported futures symbol: {symbol or '-'}")
    return symbol


def normalize_source(value: object) -> str:
    """Normalize source labels used by broker/backfill callers."""
    source = str(value or "").strip().lower().replace("-", "_")
    if source in {"topstep", "topstepx", "broker", "projectx"}:
        return SOURCE_TOPSTEPX
    if source in {"databento", "db", "glbx"}:
        return SOURCE_DATABENTO
    raise ValueError(f"unsupported futures data source: {source or '-'}")


def normalize_futures_candle(
    candle: Candle,
    *,
    symbol: str,
    source: str,
) -> Candle:
    """Validate and return a canonical 1-minute :class:`Candle`.

    A fractional-second timestamp is rejected instead of rounded.  Rounding
    could merge a malformed bar with a valid minute and hide a provider bug.
    """
    root = normalize_symbol(symbol)
    source_name = normalize_source(source)
    timestamp = as_utc(candle.timestamp)
    if timestamp.second or timestamp.microsecond:
        raise ValueError(
            f"timestamp is not minute aligned: {timestamp.isoformat()}"
        )

    values = tuple(float(getattr(candle, field)) for field in (
        "open", "high", "low", "close",
    ))
    if not all(math.isfinite(value) for value in values):
        raise ValueError("OHLC contains a non-finite value")
    open_price, high, low, close = values
    if high < max(open_price, close) or low > min(open_price, close):
        raise ValueError(
            f"invalid OHLC bounds at {timestamp.isoformat()}: "
            f"O={open_price} H={high} L={low} C={close}"
        )
    volume = int(candle.volume)
    if volume < 0:
        raise ValueError(f"negative volume at {timestamp.isoformat()}")

    return replace(
        candle,
        timestamp=timestamp,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
        symbol=root,
        interval="1m",
        source=source_name,
    )


def find_rolls(df: Any) -> List[Tuple[datetime, float, int, int]]:
    """Detect continuous-series rolls from ``instrument_id`` changes.

    The size of the price jump is only recorded.  It is never used as the
    classifier: a large same-contract market move is real data and must stay
    intact.  In particular, the 2026-04-10 +69.00-point move was a real
    same-instrument MNQ move; classifying it as a roll would destroy history.
    """
    rolls: List[Tuple[datetime, float, int, int]] = []
    previous_instrument: Optional[int] = None
    previous_close: Optional[float] = None
    for timestamp, row in df.iterrows():
        try:
            raw_instrument = row["instrument_id"]
            instrument = int(raw_instrument)
            close = float(row["close"])
            if not math.isfinite(close):
                raise ValueError("non-finite close")
            if hasattr(timestamp, "to_pydatetime"):
                timestamp = timestamp.to_pydatetime()
            instant = as_utc(timestamp)
        except (KeyError, TypeError, ValueError, OverflowError):
            previous_instrument = None
            previous_close = None
            continue

        if (
            previous_instrument is not None
            and instrument != previous_instrument
            and previous_close is not None
        ):
            try:
                jump = float(row["open"]) - previous_close
            except (KeyError, TypeError, ValueError):
                jump = float("nan")
            if math.isfinite(jump):
                rolls.append((instant, jump, previous_instrument, instrument))
        previous_instrument = instrument
        previous_close = close
    return rolls


def flatten_rolls(
    bars: Iterable[Candle],
    rolls: Iterable[Tuple[datetime, float, int, int]],
) -> List[Candle]:
    """Back-adjust all bars before each observed roll to the latest segment.

    Each adjustment is additive because MES/MNQ are quoted in points.  Applying
    every seam cumulatively means a full-year request remains continuous even
    when it contains more than one quarterly roll.
    """
    output = list(bars)
    for seam_timestamp, jump, _previous, _current in rolls:
        seam = as_utc(seam_timestamp)
        output = [
            replace(
                candle,
                open=candle.open + jump,
                high=candle.high + jump,
                low=candle.low + jump,
                close=candle.close + jump,
            )
            if as_utc(candle.timestamp) < seam else candle
            for candle in output
        ]
    return output


def source_priority(
    timestamp: datetime,
    source: str,
    *,
    recent_cutoff: datetime,
) -> int:
    """Return the deterministic winner rank for one timestamp/source pair."""
    source_name = normalize_source(source)
    recent = as_utc(timestamp) >= as_utc(recent_cutoff)
    if recent:
        return 2 if source_name == SOURCE_TOPSTEPX else 1
    return 2 if source_name == SOURCE_DATABENTO else 1


def merge_futures_by_policy(
    existing: Iterable[Candle],
    incoming: Iterable[Candle],
    *,
    recent_cutoff: datetime,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> List[Candle]:
    """Merge by UTC timestamp using the documented source priority.

    ``incoming`` wins ties inside the same source because it is the newer
    receive.  Outside the requested range, existing bars are preserved exactly.
    No bar is synthesized for a missing minute.
    """
    start_utc = as_utc(start) if start is not None else None
    end_utc = as_utc(end) if end is not None else None
    by_timestamp: dict[datetime, Candle] = {}
    for candle in existing:
        by_timestamp[as_utc(candle.timestamp)] = candle

    for candle in incoming:
        timestamp = as_utc(candle.timestamp)
        if start_utc is not None and timestamp < start_utc:
            continue
        if end_utc is not None and timestamp > end_utc:
            continue
        old = by_timestamp.get(timestamp)
        if old is None:
            by_timestamp[timestamp] = candle
            continue
        new_rank = source_priority(
            timestamp, candle.source, recent_cutoff=recent_cutoff,
        )
        old_rank = source_priority(
            timestamp, old.source, recent_cutoff=recent_cutoff,
        )
        if new_rank >= old_rank:
            by_timestamp[timestamp] = candle

    return sorted(by_timestamp.values(), key=lambda candle: as_utc(candle.timestamp))
