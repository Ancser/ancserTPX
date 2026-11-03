"""Columnar view of a persistent candle store (numpy arrays, ~64 B per bar).

The pickle written by ``candle_store.save`` stays the canonical store.  This
module keeps a derived sidecar (``<store>.columns.npz``) holding the same bars
as plain arrays, stamped with the pickle generation it was built from.  A
fresh sidecar loads in well under a second into ~150 MB, instead of
unpickling ~2.4M ``Candle`` objects (~8 s, ~1.3 GB).  Readers get a
read-only :class:`ColumnarBars` sequence that creates ``Candle`` objects only
for the rows they touch; full-history aggregation reads the arrays directly.

A sidecar that does not match the pickle's current generation is ignored and
rebuilt from the pickle, so a stale or missing sidecar can cost time but never
serve different bars.  Loaded arrays live in memory (no mmap): the store is
replaced in place, which Windows refuses while a file is mapped.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Tuple, Union, overload

import numpy as np

from backend.db.models import Candle
from backend.timebase import UTC, as_utc

logger = logging.getLogger(__name__)

SCHEMA = "ancsertpx.candle-columns.v1"
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)
_CHUNK = 65_536
Version = Optional[Tuple[int, int]]


def _micros(ts: datetime) -> int:
    return (as_utc(ts) - EPOCH) // _MICROSECOND


def _table(values: Iterable[Optional[str]]) -> Tuple[Tuple[Optional[str], ...], np.ndarray]:
    table: dict = {}
    codes = np.fromiter((table.setdefault(v, len(table)) for v in values), dtype=np.int32)
    names = tuple(sorted(table, key=table.get))
    return names, codes.astype(np.int16 if len(names) < 32_000 else np.int32)


class CandleColumns:
    """Immutable arrays for one sorted store generation."""

    __slots__ = ("ts_us", "open", "high", "low", "close", "volume",
                 "symbol_code", "interval_code", "source_code", "contract_code",
                 "price_adjustment", "symbols", "intervals", "sources", "contracts")

    def __init__(self, **arrays):
        for name in self.__slots__:
            value = arrays[name]
            if isinstance(value, np.ndarray):
                value.setflags(write=False)
            object.__setattr__(self, name, value)

    def __setattr__(self, name, value):
        raise AttributeError("CandleColumns is immutable")

    def __len__(self) -> int:
        return int(self.ts_us.shape[0])

    @classmethod
    def from_bars(cls, bars: Sequence[Candle]) -> "CandleColumns":
        """Columns for ``bars``, which must already be sorted by time."""
        n = len(bars)
        ts = np.fromiter((_micros(b.timestamp) for b in bars), dtype=np.int64, count=n)
        if n > 1 and bool(np.any(ts[1:] < ts[:-1])):
            raise ValueError("bars must be sorted by timestamp")
        volume_list = [b.volume for b in bars]
        integral = all(isinstance(v, int) for v in volume_list)
        volume = np.array(volume_list, dtype=np.int64 if integral else np.float64)
        adjustment = np.fromiter(
            (np.nan if b.price_adjustment is None else float(b.price_adjustment) for b in bars),
            dtype=np.float64, count=n)
        symbols, symbol_code = _table(b.symbol for b in bars)
        intervals, interval_code = _table(b.interval for b in bars)
        sources, source_code = _table(b.source for b in bars)
        contracts, contract_code = _table(b.contract_id for b in bars)
        return cls(
            ts_us=ts,
            open=np.fromiter((b.open for b in bars), dtype=np.float64, count=n),
            high=np.fromiter((b.high for b in bars), dtype=np.float64, count=n),
            low=np.fromiter((b.low for b in bars), dtype=np.float64, count=n),
            close=np.fromiter((b.close for b in bars), dtype=np.float64, count=n),
            volume=volume, price_adjustment=adjustment,
            symbol_code=symbol_code, interval_code=interval_code,
            source_code=source_code, contract_code=contract_code,
            symbols=symbols, intervals=intervals, sources=sources, contracts=contracts,
        )

    def candles(self, lo: int = 0, hi: Optional[int] = None) -> List[Candle]:
        """Materialise rows ``[lo, hi)`` as new ``Candle`` objects."""
        hi = len(self) if hi is None else hi
        if hi <= lo:
            return []
        ts = self.ts_us[lo:hi].tolist()
        opens = self.open[lo:hi].tolist()
        highs = self.high[lo:hi].tolist()
        lows = self.low[lo:hi].tolist()
        closes = self.close[lo:hi].tolist()
        volumes = self.volume[lo:hi].tolist()
        symbols = [self.symbols[i] for i in self.symbol_code[lo:hi].tolist()]
        intervals = [self.intervals[i] for i in self.interval_code[lo:hi].tolist()]
        sources = [self.sources[i] for i in self.source_code[lo:hi].tolist()]
        contracts = [self.contracts[i] for i in self.contract_code[lo:hi].tolist()]
        adjustments = [None if a != a else a for a in self.price_adjustment[lo:hi].tolist()]
        epoch, micro = EPOCH, _MICROSECOND
        return [
            Candle(epoch + micro * t, o, h, l, c, v, s, iv, src, cid, adj)
            for t, o, h, l, c, v, s, iv, src, cid, adj in zip(
                ts, opens, highs, lows, closes, volumes, symbols, intervals,
                sources, contracts, adjustments)
        ]

    def search(self, start: Optional[datetime], end: Optional[datetime]) -> Tuple[int, int]:
        """Row bounds for the inclusive ``[start, end]`` window."""
        lo = 0 if start is None else int(np.searchsorted(self.ts_us, _micros(start), "left"))
        hi = len(self) if end is None else int(np.searchsorted(self.ts_us, _micros(end), "right"))
        return lo, max(lo, hi)

    def epoch_seconds(self) -> np.ndarray:
        return self.ts_us // 1_000_000

    def appended(self, bars: Sequence[Candle]) -> "CandleColumns":
        """A new generation with ``bars`` (all later than the last row) added."""
        if not bars:
            return self
        extra = CandleColumns.from_bars(bars)
        if len(self) and int(extra.ts_us[0]) <= int(self.ts_us[-1]):
            raise ValueError("appended bars must be newer than the last row")

        def recode(codes, names, table):
            index = {name: i for i, name in enumerate(table)}
            merged = list(table)
            for name in names:
                if name not in index:
                    index[name] = len(merged)
                    merged.append(name)
            mapping = np.array([index[name] for name in names], dtype=np.int32)
            dtype = np.int16 if len(merged) < 32_000 else np.int32
            return tuple(merged), mapping[codes].astype(dtype)

        out = {}
        for field, table_field in (("symbol_code", "symbols"), ("interval_code", "intervals"),
                                   ("source_code", "sources"), ("contract_code", "contracts")):
            table, codes = recode(getattr(extra, field), getattr(extra, table_field),
                                  getattr(self, table_field))
            out[table_field] = table
            out[field] = np.concatenate((getattr(self, field).astype(codes.dtype), codes))
        volume_dtype = np.result_type(self.volume.dtype, extra.volume.dtype)
        return CandleColumns(
            ts_us=np.concatenate((self.ts_us, extra.ts_us)),
            open=np.concatenate((self.open, extra.open)),
            high=np.concatenate((self.high, extra.high)),
            low=np.concatenate((self.low, extra.low)),
            close=np.concatenate((self.close, extra.close)),
            volume=np.concatenate((self.volume.astype(volume_dtype), extra.volume.astype(volume_dtype))),
            price_adjustment=np.concatenate((self.price_adjustment, extra.price_adjustment)),
            **out,
        )

    # ── persistence ─────────────────────────────────────────────────────────
    def save(self, path: Path, source_version: Version) -> None:
        """Write atomically, stamped with the pickle generation it mirrors."""
        header = {
            "schema": SCHEMA,
            "source_version": list(source_version) if source_version else None,
            "count": len(self),
            "symbols": list(self.symbols),
            "intervals": list(self.intervals),
            "sources": list(self.sources),
            "contracts": list(self.contracts),
        }
        temp = path.with_name(path.name + ".tmp")
        with temp.open("wb") as handle:
            np.savez(
                handle,
                header=np.frombuffer(json.dumps(header).encode("utf-8"), dtype=np.uint8),
                ts_us=self.ts_us, open=self.open, high=self.high, low=self.low,
                close=self.close, volume=self.volume, price_adjustment=self.price_adjustment,
                symbol_code=self.symbol_code, interval_code=self.interval_code,
                source_code=self.source_code, contract_code=self.contract_code,
            )
        os.replace(temp, path)

    @classmethod
    def load(cls, path: Path, source_version: Version) -> Optional["CandleColumns"]:
        """The sidecar's columns if it mirrors ``source_version``; else None."""
        try:
            with np.load(path, allow_pickle=False) as data:
                header = json.loads(bytes(data["header"]).decode("utf-8"))
                if header.get("schema") != SCHEMA:
                    return None
                stamped = header.get("source_version")
                if source_version is None or stamped != list(source_version):
                    return None
                arrays = {name: data[name] for name in (
                    "ts_us", "open", "high", "low", "close", "volume", "price_adjustment",
                    "symbol_code", "interval_code", "source_code", "contract_code")}
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return None
        if int(arrays["ts_us"].shape[0]) != int(header.get("count", -1)):
            return None
        return cls(
            **arrays,
            symbols=tuple(header["symbols"]), intervals=tuple(header["intervals"]),
            sources=tuple(header["sources"]), contracts=tuple(header["contracts"]),
        )


class ColumnarBars(Sequence):
    """Read-only ``Sequence[Candle]`` over :class:`CandleColumns`.

    Every access returns new ``Candle`` objects, so callers cannot mutate the
    cached generation (DATA-004) and memory holds only what they keep.
    """

    __slots__ = ("columns",)

    def __init__(self, columns: CandleColumns):
        object.__setattr__(self, "columns", columns)

    def __len__(self) -> int:
        return len(self.columns)

    @overload
    def __getitem__(self, index: int) -> Candle: ...

    @overload
    def __getitem__(self, index: slice) -> List[Candle]: ...

    def __getitem__(self, index: Union[int, slice]):
        n = len(self.columns)
        if isinstance(index, slice):
            lo, hi, step = index.indices(n)
            rows = self.columns.candles(lo, hi) if lo < hi else []
            return rows if step == 1 else rows[::step]
        i = int(index)
        if i < 0:
            i += n
        if not 0 <= i < n:
            raise IndexError("candle index out of range")
        return self.columns.candles(i, i + 1)[0]

    def __iter__(self) -> Iterator[Candle]:
        n = len(self.columns)
        for lo in range(0, n, _CHUNK):
            yield from self.columns.candles(lo, min(n, lo + _CHUNK))

    def __reversed__(self) -> Iterator[Candle]:
        n = len(self.columns)
        for hi in range(n, 0, -_CHUNK):
            yield from reversed(self.columns.candles(max(0, hi - _CHUNK), hi))

    def __bool__(self) -> bool:
        return len(self.columns) > 0

    def __repr__(self) -> str:
        return f"ColumnarBars({len(self)} bars)"


def sidecar_path(store_path: Path) -> Path:
    return store_path.with_name(store_path.stem + ".columns.npz")

