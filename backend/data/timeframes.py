"""The one bar-aggregation definition (chart timeframes and 1m->5m callers).

Buckets follow the CME equity-index / Topstep clock rather than raw UTC days:

* ``5m`` .. ``1h`` are clock-aligned.  New York is always a whole number of
  hours from UTC, so a UTC floor is also a New York floor.
* ``4h`` is anchored to the session open (18:00 New York = 17:00 Chicago):
  18:00, 22:00, 02:00, 06:00, 10:00, 14:00 New York.
* ``1d`` is the Topstep trade date (17:00 Chicago boundary, DST-aware), so a
  Sunday-evening session belongs to Monday's bar.
* ``1w`` groups trade dates by their ISO week (Monday trade date anchors it).

A bucket's timestamp is its start instant in UTC.  Input minutes are never
invented: a bucket exists only where at least one source bar exists.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List

from backend.db.models import Candle
from backend.timebase import UTC, as_utc, topstep_trade_date, topstep_trade_date_bounds

CHART_TIMEFRAMES = ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w")
_CLOCK_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}
_ALIASES = {"1": "1m", "5": "5m", "15": "15m", "30": "30m", "60": "1h", "1H": "1h",
            "240": "4h", "4H": "4h", "D": "1d", "1D": "1d", "W": "1w", "1W": "1w"}


def normalize_timeframe(value: object) -> str:
    raw = str(value or "1m").strip()
    tf = _ALIASES.get(raw, raw.lower())
    if tf not in CHART_TIMEFRAMES:
        raise ValueError(f"unsupported timeframe {value!r}; choose one of {CHART_TIMEFRAMES}")
    return tf


def timeframe_seconds(tf: str) -> int:
    """Nominal bucket length (4h/1d/1w are session-anchored, not epoch floors)."""
    tf = normalize_timeframe(tf)
    if tf in _CLOCK_MINUTES:
        return _CLOCK_MINUTES[tf] * 60
    return {"4h": 4 * 3600, "1d": 86400, "1w": 7 * 86400}[tf]


def bucket_start(ts: datetime, tf: str) -> datetime:
    """Start of the ``tf`` bucket containing ``ts``.

    Clock-aligned frames keep ``ts``'s own tz convention (a legacy naive-UTC
    input stays naive), exactly like the former 1m->5m helper.
    """
    tf = normalize_timeframe(tf)
    if tf in _CLOCK_MINUTES:
        step = _CLOCK_MINUTES[tf]
        if step == 60:
            return ts.replace(minute=0, second=0, microsecond=0)
        return ts.replace(minute=(ts.minute // step) * step, second=0, microsecond=0)
    instant = as_utc(ts)
    trade_date = topstep_trade_date(instant)
    if tf == "1d":
        return topstep_trade_date_bounds(trade_date)[0]
    if tf == "1w":
        day = date.fromisoformat(trade_date)
        monday = day - timedelta(days=day.weekday())
        return topstep_trade_date_bounds(monday.isoformat())[0]
    session_open = topstep_trade_date_bounds(trade_date)[0]
    offset = int((instant - session_open).total_seconds()) // (4 * 3600)
    return session_open + timedelta(hours=4 * offset)


_VECTOR_MIN_BARS = 2000


def _vectorizable(source: List[Candle]) -> bool:
    """Large, sorted, tz-aware input can take the numpy path with equal output."""
    if len(source) < _VECTOR_MIN_BARS:
        return False
    if source[0].timestamp.tzinfo is None or source[-1].timestamp.tzinfo is None:
        return False
    previous = None
    for bar in source:
        ts = bar.timestamp
        if previous is not None and ts < previous:
            return False
        previous = ts
    return True


def _bucket_keys(epoch_s, tf: str):
    """Bucket-start epoch seconds for sorted epoch-second input.

    Clock frames floor the epoch; session frames apply the scalar
    :func:`bucket_start` once per distinct hour (their boundaries are whole
    hours), so this is the same definition, not a second copy of the clock.
    """
    import numpy as np

    if tf in _CLOCK_MINUTES:
        step = _CLOCK_MINUTES[tf] * 60
        return (epoch_s // step) * step
    hours = epoch_s // 3600
    unique_hours, inverse = np.unique(hours, return_inverse=True)
    starts = np.fromiter(
        (int(bucket_start(datetime.fromtimestamp(int(h) * 3600, UTC), tf).timestamp())
         for h in unique_hours),
        dtype=np.int64, count=len(unique_hours),
    )
    return starts[inverse]


def _aggregate_arrays(epoch, opens, highs, lows, closes, volumes, symbol_at,
                      frames: List[str]) -> Dict[str, List[Candle]]:
    """Vectorised core shared by :func:`aggregate_many` and
    :func:`aggregate_columns`: sorted epoch-second rows -> bars per frame."""
    import numpy as np

    n = len(epoch)
    out: Dict[str, List[Candle]] = {}
    for tf in frames:
        keys = _bucket_keys(epoch, tf)
        starts = np.flatnonzero(np.concatenate(([True], keys[1:] != keys[:-1])))
        ends = np.concatenate((starts[1:] - 1, [n - 1]))
        bucket_high = np.maximum.reduceat(highs, starts).tolist()
        bucket_low = np.minimum.reduceat(lows, starts).tolist()
        bucket_volume = np.add.reduceat(volumes, starts).tolist()
        first_open = opens[starts].tolist()
        last_close = closes[ends].tolist()
        bucket_keys = keys[starts].tolist()
        start_rows = starts.tolist()
        out[tf] = [
            Candle(
                timestamp=datetime.fromtimestamp(int(bucket_keys[i]), UTC),
                open=first_open[i], high=bucket_high[i], low=bucket_low[i],
                close=last_close[i], volume=int(bucket_volume[i]),
                symbol=symbol_at(start_rows[i]), interval=tf,
            )
            for i in range(len(start_rows))
        ]
    return out


def aggregate_many(candles: Iterable[Candle], timeframes: Iterable[str]) -> Dict[str, List[Candle]]:
    """Aggregate one bar list into several timeframes with a single pass over
    the Candle objects (the expensive part on a multi-million-bar store)."""
    import numpy as np

    source = list(candles)
    frames = [normalize_timeframe(tf) for tf in timeframes]
    if not source or not _vectorizable(source):
        return {tf: aggregate_candles(source, tf) for tf in frames}
    rows = np.array(
        [(b.timestamp.timestamp(), b.open, b.high, b.low, b.close, b.volume) for b in source],
        dtype=np.float64,
    )
    higher = [tf for tf in frames if tf != "1m"]
    out = _aggregate_arrays(
        rows[:, 0].astype(np.int64), rows[:, 1], rows[:, 2], rows[:, 3], rows[:, 4],
        rows[:, 5], lambda row: source[row].symbol, higher,
    )
    if "1m" in frames:
        out["1m"] = source
    return {tf: out[tf] for tf in frames}


def aggregate_columns(columns, timeframes: Iterable[str]) -> Dict[str, List[Candle]]:
    """Same result as :func:`aggregate_many` over a columnar store
    (``backend.data.columnar_store.CandleColumns``) without creating a
    ``Candle`` per 1-minute row."""
    import numpy as np

    frames = [normalize_timeframe(tf) for tf in timeframes]
    if not len(columns):
        return {tf: [] for tf in frames}
    higher = [tf for tf in frames if tf != "1m"]
    symbols, codes = columns.symbols, columns.symbol_code
    out = _aggregate_arrays(
        columns.epoch_seconds(), columns.open, columns.high, columns.low, columns.close,
        columns.volume.astype(np.float64), lambda row: symbols[int(codes[row])], higher,
    )
    if "1m" in frames:
        out["1m"] = columns.candles()
    return {tf: out[tf] for tf in frames}


def aggregate_candles(candles: Iterable[Candle], tf: str) -> List[Candle]:
    """OHLCV-aggregate ``candles`` into ``tf`` buckets, sorted by bucket start.

    Open/close follow input order inside a bucket (callers pass sorted bars).
    ``1m`` returns the input unchanged as a list.
    """
    tf = normalize_timeframe(tf)
    source = list(candles)
    if tf == "1m" or not source:
        return source
    if _vectorizable(source):
        return aggregate_many(source, [tf])[tf]
    start_of = _bucket_function(tf)
    out: List[Candle] = []
    # Fast path: sorted input streams through one running bucket.
    key = None
    o = h = l = c = v = sym = None
    previous = None
    for bar in source:
        ts = bar.timestamp
        if previous is not None and ts < previous:
            return _aggregate_unsorted(source, tf, start_of)
        previous = ts
        k = start_of(ts)
        if k != key:
            if key is not None:
                out.append(Candle(timestamp=key, open=o, high=h, low=l, close=c,
                                  volume=v, symbol=sym, interval=tf))
            key, o, h, l, c, v, sym = k, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.symbol
        else:
            if bar.high > h:
                h = bar.high
            if bar.low < l:
                l = bar.low
            c = bar.close
            v += bar.volume
    out.append(Candle(timestamp=key, open=o, high=h, low=l, close=c,
                      volume=v, symbol=sym, interval=tf))
    return out


def _bucket_function(tf: str):
    """Per-bar bucket key; session-anchored frames are memoized per hour.

    4h/1d/1w boundaries always fall on a whole hour (17:00 Chicago plus
    multiples of 4h), so every minute of an hour shares one bucket.
    """
    if tf in _CLOCK_MINUTES:
        step = _CLOCK_MINUTES[tf]
        if step == 60:
            return lambda ts: ts.replace(minute=0, second=0, microsecond=0)
        return lambda ts: ts.replace(minute=(ts.minute // step) * step, second=0, microsecond=0)
    memo: Dict[datetime, datetime] = {}

    def start_of(ts: datetime) -> datetime:
        hour = ts.replace(minute=0, second=0, microsecond=0)
        found = memo.get(hour)
        if found is None:
            found = memo[hour] = bucket_start(hour, tf)
        return found
    return start_of


def _aggregate_unsorted(source: List[Candle], tf: str, start_of) -> List[Candle]:
    # Tolerates disorder like the former 1m->5m helper: open/close follow
    # input order inside each bucket.
    buckets: Dict[datetime, List[Candle]] = {}
    for candle in source:
        buckets.setdefault(start_of(candle.timestamp), []).append(candle)
    out: List[Candle] = []
    for start in sorted(buckets):
        group = buckets[start]
        out.append(Candle(
            timestamp=start,
            open=group[0].open,
            high=max(c.high for c in group),
            low=min(c.low for c in group),
            close=group[-1].close,
            volume=sum(c.volume for c in group),
            symbol=group[0].symbol,
            interval=tf,
        ))
    return out
