"""Read and derive compact chart data from Databento CME MBO files.

Raw DBN files are the source of truth and stay in ``ancserMarketData/source``.
The application never loads those multi-hundred-megabyte files on a chart
request.  A one-time builder replays each file into a small, gzip-compressed
one-minute cache under ``derived/orderflow``; the API then reads only the
requested time window from that cache.
"""
from __future__ import annotations

import gzip
import heapq
import json
import math
import threading
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from backend.data import market_data
from backend.timebase import LOS_ANGELES, UTC


NANO = 1_000_000_000
PRICE_SCALE = 1_000_000_000
DEFAULT_TICK_SIZE = 0.25
DEFAULT_SESSION_TIME_ZONE = LOS_ANGELES.key
RTH_OPEN = time(6, 30)
RTH_CLOSE = time(13, 0)
DEFAULT_DEPTH_RADIUS_POINTS = 40.0
# v5 adds per-price aggressive-trade size bands to the v4 corrected book
# semantics.  Old v2/v4 caches remain readable but are never treated as the
# complete footprint contract by a rebuild.
CACHE_SCHEMA_VERSION = 5
LAST_EVENT_FLAG = 128
REFILL_WINDOW_NS = 250_000_000
_CACHE_READ_LOCK = threading.Lock()


def base_symbol(value: str) -> str:
    """Normalize a contract/raw symbol to the product used by chart caches."""
    upper = str(value or "MNQ").strip().upper()
    for symbol in ("MNQ", "MES", "NQ", "ES"):
        if symbol in upper:
            return symbol
    return upper or "MNQ"


def _record_text(record: object, name: str) -> str:
    return str(getattr(record, name, "") or "").upper()


def _record_int(record: object, name: str) -> int:
    try:
        return int(getattr(record, name, 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _valid_price_nano(value: int) -> bool:
    return 0 < value < 10**18


def _session_bounds_ns(
    trade_date: str | date,
    time_zone: str = DEFAULT_SESSION_TIME_ZONE,
) -> tuple[int, int]:
    day = date.fromisoformat(trade_date) if isinstance(trade_date, str) else trade_date
    zone = ZoneInfo(time_zone)
    start = datetime.combine(day, RTH_OPEN, tzinfo=zone).astimezone(UTC)
    end = datetime.combine(day, RTH_CLOSE, tzinfo=zone).astimezone(UTC)
    return int(start.timestamp() * NANO), int(end.timestamp() * NANO)


def _iso_from_epoch(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=UTC).isoformat()


class _MboAggregator:
    """Streaming MBO replay state; bounded by one requested trade day."""

    def __init__(
        self,
        trade_date: str,
        *,
        tick_size: float = DEFAULT_TICK_SIZE,
        time_zone: str = DEFAULT_SESSION_TIME_ZONE,
        depth_radius_points: float = DEFAULT_DEPTH_RADIUS_POINTS,
        snapshot_flag: int = 0,
        last_event_flag: int = LAST_EVENT_FLAG,
    ) -> None:
        if tick_size <= 0:
            raise ValueError("tick_size must be positive")
        self.trade_date = date.fromisoformat(trade_date).isoformat()
        self.tick_size = float(tick_size)
        self.tick_nano = int(round(self.tick_size * PRICE_SCALE))
        self.time_zone = time_zone
        self.depth_radius_nano = int(depth_radius_points * PRICE_SCALE)
        self.snapshot_flag = int(snapshot_flag)
        self.last_event_flag = int(last_event_flag)
        self.start_ns, self.end_ns = _session_bounds_ns(self.trade_date, time_zone)
        self.orders: dict[int, list[Any]] = {}
        self.levels: defaultdict[tuple[str, int], int] = defaultdict(int)
        self.bid_heap: list[int] = []
        self.ask_heap: list[int] = []
        self.bid_heap_prices: set[int] = set()
        self.ask_heap_prices: set[int] = set()
        self.bars: dict[int, dict[str, Any]] = {}
        self.last_trade_nano: int | None = None
        self.record_count = 0
        self.pending_depth_touches: set[tuple[str, int]] = set()
        self.event_bbo_before: tuple[int, int, int, int] | None = None
        self.recent_fill_levels: dict[tuple[str, int], int] = {}

    def _tick(self, price_nano: int) -> int:
        return int(round(price_nano / self.tick_nano))

    def _bar(self, ts_ns: int) -> dict[str, Any]:
        epoch = (ts_ns // (60 * NANO)) * 60
        bar = self.bars.get(epoch)
        if bar is None:
            bar = {
                "epoch": int(epoch),
                "open": None,
                "high": None,
                "low": None,
                "close": None,
                "buy": 0,
                "sell": 0,
                "buy_trades": 0,
                "sell_trades": 0,
                "max_buy_trade": 0,
                "max_sell_trade": 0,
                "buy_ge_50": 0,
                "buy_ge_100": 0,
                "buy_ge_150": 0,
                "sell_ge_50": 0,
                "sell_ge_100": 0,
                "sell_ge_150": 0,
                "delta": 0,
                "trades": 0,
                "adds": 0,
                "cancels": 0,
                "modifies": 0,
                "fills": 0,
                "add_qty": 0,
                "cancel_qty": 0,
                "bid_add_qty": 0,
                "ask_add_qty": 0,
                "bid_cancel_qty": 0,
                "ask_cancel_qty": 0,
                "bid_refill_qty": 0,
                "ask_refill_qty": 0,
                "ofi": 0,
                "close_bid_price": None,
                "close_bid_depth": 0,
                "close_ask_price": None,
                "close_ask_depth": 0,
                # tick -> buy, sell, passive bid fill, passive ask fill,
                #         maximum displayed bid depth, maximum ask depth,
                #         near-fill bid refill, near-fill ask refill,
                #         buy 50-99, buy 100-149, buy 150+,
                #         sell 50-99, sell 100-149, sell 150+
                "cells": defaultdict(lambda: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]),
            }
            self.bars[epoch] = bar
            self._seed_depth(bar)
        return bar

    def _seed_depth(self, bar: dict[str, Any]) -> None:
        """Capture resting levels once per minute, including unchanged walls."""
        anchor = self.last_trade_nano
        if anchor is None:
            return
        for (side, price_nano), depth in self.levels.items():
            if depth <= 0 or abs(price_nano - anchor) > self.depth_radius_nano:
                continue
            cell = self._cell(bar, price_nano)
            index = 4 if side == "B" else 5
            cell[index] = max(cell[index], int(depth))

    def _cell(self, bar: dict[str, Any], price_nano: int) -> list[int]:
        return bar["cells"][self._tick(price_nano)]

    def _remove_order(self, order_id: int) -> list[Any] | None:
        old = self.orders.pop(order_id, None)
        if old is None:
            return None
        key = (str(old[0]), int(old[1]))
        remaining = max(0, self.levels[key] - int(old[2]))
        if remaining:
            self.levels[key] = remaining
        else:
            self.levels.pop(key, None)
        return old

    def _cancel_order(self, order_id: int, size: int) -> tuple[list[Any] | None, int]:
        """Apply Databento's cancel quantity without deleting a partial order."""
        old = self.orders.get(order_id)
        if old is None:
            return None, 0
        old_size = max(0, int(old[2]))
        removed = old_size if size <= 0 else min(old_size, int(size))
        key = (str(old[0]), int(old[1]))
        remaining_level = max(0, self.levels[key] - removed)
        if remaining_level:
            self.levels[key] = remaining_level
        else:
            self.levels.pop(key, None)
        old[2] = old_size - removed
        if old[2] <= 0:
            self.orders.pop(order_id, None)
        return old, removed

    def _add_order(self, order_id: int, side: str, price_nano: int, size: int) -> None:
        if not order_id or side not in ("A", "B") or not _valid_price_nano(price_nano):
            return
        if order_id in self.orders:
            self._remove_order(order_id)
        clean_size = max(0, int(size))
        was_empty = self.levels[(side, price_nano)] <= 0
        self.orders[order_id] = [side, price_nano, clean_size]
        self.levels[(side, price_nano)] += clean_size
        heap_prices = self.bid_heap_prices if side == "B" else self.ask_heap_prices
        if clean_size and was_empty and price_nano not in heap_prices:
            heapq.heappush(self.bid_heap if side == "B" else self.ask_heap,
                           -price_nano if side == "B" else price_nano)
            heap_prices.add(price_nano)

    def _best_level(self, side: str) -> tuple[int, int]:
        heap = self.bid_heap if side == "B" else self.ask_heap
        while heap:
            price_nano = -heap[0] if side == "B" else heap[0]
            depth = int(self.levels.get((side, price_nano), 0))
            if depth > 0:
                return price_nano, depth
            heapq.heappop(heap)
            (self.bid_heap_prices if side == "B" else self.ask_heap_prices).discard(price_nano)
        return 0, 0

    def _current_bbo(self) -> tuple[int, int, int, int]:
        bid_price, bid_depth = self._best_level("B")
        ask_price, ask_depth = self._best_level("A")
        return bid_price, bid_depth, ask_price, ask_depth

    def _begin_book_event(self, bar: dict[str, Any] | None) -> None:
        if bar is not None and self.event_bbo_before is None:
            self.event_bbo_before = self._current_bbo()

    def _touch_depth(self, bar: dict[str, Any], side: str, price_nano: int) -> None:
        if side not in ("A", "B") or not _valid_price_nano(price_nano):
            return
        anchor = self.last_trade_nano
        if anchor is not None and abs(price_nano - anchor) > self.depth_radius_nano:
            return
        depth = max(0, int(self.levels.get((side, price_nano), 0)))
        cell = self._cell(bar, price_nano)
        index = 4 if side == "B" else 5
        cell[index] = max(cell[index], depth)

    def _queue_depth_touch(self, side: str, price_nano: int) -> None:
        if side in ("A", "B") and _valid_price_nano(price_nano):
            self.pending_depth_touches.add((side, price_nano))

    def _flush_depth_touches(self, bar: dict[str, Any] | None, flags: int) -> None:
        if not (flags & self.last_event_flag):
            return
        if bar is not None:
            for side, price_nano in self.pending_depth_touches:
                self._touch_depth(bar, side, price_nano)
            current = self._current_bbo()
            bid_price, bid_depth, ask_price, ask_depth = current
            bar["close_bid_price"] = bid_price / PRICE_SCALE if bid_price else None
            bar["close_bid_depth"] = bid_depth
            bar["close_ask_price"] = ask_price / PRICE_SCALE if ask_price else None
            bar["close_ask_depth"] = ask_depth
            if self.event_bbo_before is not None:
                old_bid_price, old_bid_depth, old_ask_price, old_ask_depth = self.event_bbo_before
                # Cont-Kukanov-Stoikov best-level order-flow imbalance.
                bar["ofi"] += (
                    (bid_depth if bid_price >= old_bid_price else 0)
                    - (old_bid_depth if bid_price <= old_bid_price else 0)
                    - (ask_depth if ask_price <= old_ask_price else 0)
                    + (old_ask_depth if ask_price >= old_ask_price else 0)
                )
        self.pending_depth_touches.clear()
        self.event_bbo_before = None

    def _record_trade(self, bar: dict[str, Any], side: str, price_nano: int, size: int) -> None:
        price = round(price_nano / PRICE_SCALE, 8)
        if bar["open"] is None:
            bar["open"] = price
        bar["high"] = price if bar["high"] is None else max(bar["high"], price)
        bar["low"] = price if bar["low"] is None else min(bar["low"], price)
        bar["close"] = price
        bar["trades"] += 1
        cell = self._cell(bar, price_nano)
        if side == "B":
            bar["buy"] += size
            bar["buy_trades"] += 1
            bar["max_buy_trade"] = max(bar["max_buy_trade"], size)
            for threshold in (50, 100, 150):
                if size >= threshold:
                    bar[f"buy_ge_{threshold}"] += 1
            if 50 <= size < 100:
                cell[8] += size
            elif 100 <= size < 150:
                cell[9] += size
            elif size >= 150:
                cell[10] += size
            cell[0] += size
        else:
            bar["sell"] += size
            bar["sell_trades"] += 1
            bar["max_sell_trade"] = max(bar["max_sell_trade"], size)
            for threshold in (50, 100, 150):
                if size >= threshold:
                    bar[f"sell_ge_{threshold}"] += 1
            if 50 <= size < 100:
                cell[11] += size
            elif 100 <= size < 150:
                cell[12] += size
            elif size >= 150:
                cell[13] += size
            cell[1] += size
        bar["delta"] = bar["buy"] - bar["sell"]

    def consume(self, record: object) -> None:
        self.record_count += 1
        ts_ns = _record_int(record, "ts_event")
        action = _record_text(record, "action")
        side = _record_text(record, "side")
        order_id = _record_int(record, "order_id")
        size = max(0, _record_int(record, "size"))
        price_nano = _record_int(record, "price")
        flags = _record_int(record, "flags")
        in_session = self.start_ns <= ts_ns < self.end_ns
        is_snapshot = bool(self.snapshot_flag and flags & self.snapshot_flag)

        if action == "R":
            self.orders.clear()
            self.levels.clear()
            self.bid_heap.clear()
            self.ask_heap.clear()
            self.bid_heap_prices.clear()
            self.ask_heap_prices.clear()
            self.pending_depth_touches.clear()
            return

        # Synthetic midnight snapshots seed the book, but their original
        # ts_event can precede the requested date and must never become a bar.
        if is_snapshot:
            if action == "A":
                self._add_order(order_id, side, price_nano, size)
            return

        if action == "T" and side in ("A", "B") and _valid_price_nano(price_nano):
            self.last_trade_nano = price_nano
            if in_session:
                self._record_trade(self._bar(ts_ns), side, price_nano, size)
            return

        bar = self._bar(ts_ns) if in_session else None

        if action == "A":
            self._begin_book_event(bar)
            self._add_order(order_id, side, price_nano, size)
            if bar is not None:
                bar["adds"] += 1
                bar["add_qty"] += size
                bar["bid_add_qty" if side == "B" else "ask_add_qty"] += size
                last_fill = self.recent_fill_levels.get((side, price_nano))
                if last_fill is not None and 0 <= ts_ns - last_fill <= REFILL_WINDOW_NS:
                    key = "bid_refill_qty" if side == "B" else "ask_refill_qty"
                    bar[key] += size
                    self._cell(bar, price_nano)[6 if side == "B" else 7] += size
            self._queue_depth_touch(side, price_nano)
            self._flush_depth_touches(bar, flags)
            return

        if action == "M":
            self._begin_book_event(bar)
            old = self._remove_order(order_id)
            new_side = side if side in ("A", "B") else (str(old[0]) if old else "")
            new_price = price_nano if _valid_price_nano(price_nano) else (int(old[1]) if old else 0)
            self._add_order(order_id, new_side, new_price, size)
            if bar is not None:
                bar["modifies"] += 1
            if old is not None:
                self._queue_depth_touch(str(old[0]), int(old[1]))
            self._queue_depth_touch(new_side, new_price)
            self._flush_depth_touches(bar, flags)
            return

        if action == "C":
            self._begin_book_event(bar)
            old, removed = self._cancel_order(order_id, size)
            if bar is not None:
                bar["cancels"] += 1
                if old is not None:
                    bar["cancel_qty"] += removed
                    key = "bid_cancel_qty" if str(old[0]) == "B" else "ask_cancel_qty"
                    bar[key] += removed
                    self._queue_depth_touch(str(old[0]), int(old[1]))
            self._flush_depth_touches(bar, flags)
            return

        if action != "F":
            return

        old = self.orders.get(order_id)
        if old is not None:
            old_side = str(old[0])
            old_price = int(old[1])
            take = min(max(0, int(old[2])), size)
            if bar is not None:
                cell = self._cell(bar, old_price)
                cell[2 if old_side == "B" else 3] += take
                self.recent_fill_levels[(old_side, old_price)] = ts_ns
        if bar is not None:
            bar["fills"] += 1

    def finish(self) -> dict[str, Any]:
        serial: list[dict[str, Any]] = []
        for epoch in sorted(self.bars):
            bar = self.bars[epoch]
            cells = [
                [int(tick), *(int(value) for value in values)]
                for tick, values in sorted(bar["cells"].items())
                if any(values)
            ]
            serial.append({
                "time": _iso_from_epoch(epoch),
                "epoch": epoch,
                "open": bar["open"],
                "high": bar["high"],
                "low": bar["low"],
                "close": bar["close"],
                "buy": int(bar["buy"]),
                "sell": int(bar["sell"]),
                "buy_trades": int(bar["buy_trades"]),
                "sell_trades": int(bar["sell_trades"]),
                "max_buy_trade": int(bar["max_buy_trade"]),
                "max_sell_trade": int(bar["max_sell_trade"]),
                "buy_ge_50": int(bar["buy_ge_50"]),
                "buy_ge_100": int(bar["buy_ge_100"]),
                "buy_ge_150": int(bar["buy_ge_150"]),
                "sell_ge_50": int(bar["sell_ge_50"]),
                "sell_ge_100": int(bar["sell_ge_100"]),
                "sell_ge_150": int(bar["sell_ge_150"]),
                "delta": int(bar["delta"]),
                "trades": int(bar["trades"]),
                "adds": int(bar["adds"]),
                "cancels": int(bar["cancels"]),
                "modifies": int(bar["modifies"]),
                "fills": int(bar["fills"]),
                "add_qty": int(bar["add_qty"]),
                "cancel_qty": int(bar["cancel_qty"]),
                "bid_add_qty": int(bar["bid_add_qty"]),
                "ask_add_qty": int(bar["ask_add_qty"]),
                "bid_cancel_qty": int(bar["bid_cancel_qty"]),
                "ask_cancel_qty": int(bar["ask_cancel_qty"]),
                "bid_refill_qty": int(bar["bid_refill_qty"]),
                "ask_refill_qty": int(bar["ask_refill_qty"]),
                "ofi": int(bar["ofi"]),
                "close_bid_price": bar["close_bid_price"],
                "close_bid_depth": int(bar["close_bid_depth"]),
                "close_ask_price": bar["close_ask_price"],
                "close_ask_depth": int(bar["close_ask_depth"]),
                "cells": cells,
            })
        return {
            "meta": {
                "schema_version": CACHE_SCHEMA_VERSION,
                "trade_date": self.trade_date,
                "source_schema": "mbo",
                "interval": "1m",
                "interval_seconds": 60,
                "tick_size": self.tick_size,
                "session": "RTH",
                "session_time_zone": self.time_zone,
                "session_open": RTH_OPEN.isoformat(timespec="minutes"),
                "session_close": RTH_CLOSE.isoformat(timespec="minutes"),
                "depth_radius_points": self.depth_radius_nano / PRICE_SCALE,
                "source_records": self.record_count,
                "cell_encoding": [
                    "price_tick", "aggressive_buy", "aggressive_sell",
                    "passive_bid_fill", "passive_ask_fill",
                    "max_bid_depth", "max_ask_depth",
                    "near_fill_bid_refill", "near_fill_ask_refill",
                    "buy_50_99_qty", "buy_100_149_qty", "buy_150_plus_qty",
                    "sell_50_99_qty", "sell_100_149_qty", "sell_150_plus_qty",
                ],
            },
            "bars": serial,
        }


def aggregate_mbo_records(
    records: Iterable[object],
    trade_date: str,
    *,
    tick_size: float = DEFAULT_TICK_SIZE,
    time_zone: str = DEFAULT_SESSION_TIME_ZONE,
    depth_radius_points: float = DEFAULT_DEPTH_RADIUS_POINTS,
    snapshot_flag: int = 0,
) -> dict[str, Any]:
    """Aggregate a record iterable; this pure entry point is used by tests."""
    aggregator = _MboAggregator(
        trade_date,
        tick_size=tick_size,
        time_zone=time_zone,
        depth_radius_points=depth_radius_points,
        snapshot_flag=snapshot_flag,
    )
    for record in records:
        aggregator.consume(record)
    return aggregator.finish()


def footprint_cache_path(
    trade_date: str,
    symbol: str = "MNQ",
    *,
    root: str | Path | None = None,
) -> Path:
    base = base_symbol(symbol).lower()
    return market_data.derived_path(
        "orderflow", base, f"footprint_{base}_{trade_date}.json.gz", root=root,
    )


def write_footprint_cache(payload: dict[str, Any], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(output_path.name + ".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=6) as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    temporary.replace(output_path)
    return output_path


def build_footprint_file(
    dbn_path: str | Path,
    trade_date: str,
    *,
    symbol: str = "MNQ",
    output_path: str | Path | None = None,
    tick_size: float = DEFAULT_TICK_SIZE,
    depth_radius_points: float = DEFAULT_DEPTH_RADIUS_POINTS,
) -> tuple[Path, dict[str, Any]]:
    """Replay one DBN MBO file and atomically publish its compact chart cache."""
    import databento as db

    source = Path(dbn_path).resolve()
    store = db.DBNStore.from_file(source)
    if str(store.schema) != "mbo":
        raise ValueError(f"expected MBO DBN, got {store.schema}")
    aggregator = _MboAggregator(
        trade_date,
        tick_size=tick_size,
        depth_radius_points=depth_radius_points,
        snapshot_flag=int(db.RecordFlags.F_SNAPSHOT),
    )
    store.replay(aggregator.consume)
    payload = aggregator.finish()
    payload["meta"].update({
        "symbol": base_symbol(symbol),
        "source_file": source.name,
        "source_bytes": source.stat().st_size,
        "generated_at": datetime.now(UTC).isoformat(),
    })
    destination = Path(output_path) if output_path else footprint_cache_path(trade_date, symbol)
    write_footprint_cache(payload, destination)
    return destination, payload


def read_footprint_cache(path: str | Path) -> dict[str, Any]:
    with gzip.open(Path(path), "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or not isinstance(payload.get("bars"), list):
        raise ValueError(f"invalid footprint cache: {path}")
    return payload


def _merge_cells(target: defaultdict[int, list[int]], cells: Iterable[list[Any]]) -> None:
    for raw in cells:
        if not isinstance(raw, list) or len(raw) < 7:
            continue
        tick = int(raw[0])
        slot = target[tick]
        for index in range(1, 5):
            slot[index - 1] += int(raw[index] or 0)
        slot[4] = max(slot[4], int(raw[5] or 0))
        slot[5] = max(slot[5], int(raw[6] or 0))
        if len(raw) >= 9:
            slot[6] += int(raw[7] or 0)
            slot[7] += int(raw[8] or 0)
        for index in range(9, min(len(raw), 15)):
            slot[index - 1] += int(raw[index] or 0)


def _rollup_bars(bars: list[dict[str, Any]], interval_seconds: int) -> list[dict[str, Any]]:
    if interval_seconds == 60:
        return bars
    grouped: dict[int, dict[str, Any]] = {}
    for bar in bars:
        epoch = int(bar.get("epoch") or 0)
        bucket = (epoch // interval_seconds) * interval_seconds
        row = grouped.get(bucket)
        if row is None:
            row = {
                "time": _iso_from_epoch(bucket), "epoch": bucket,
                "open": None, "high": None, "low": None, "close": None,
                "buy": 0, "sell": 0, "delta": 0, "trades": 0,
                "buy_trades": 0, "sell_trades": 0,
                "max_buy_trade": 0, "max_sell_trade": 0,
                "buy_ge_50": 0, "buy_ge_100": 0, "buy_ge_150": 0,
                "sell_ge_50": 0, "sell_ge_100": 0, "sell_ge_150": 0,
                "adds": 0, "cancels": 0, "modifies": 0, "fills": 0,
                "add_qty": 0, "cancel_qty": 0,
                "bid_add_qty": 0, "ask_add_qty": 0,
                "bid_cancel_qty": 0, "ask_cancel_qty": 0,
                "bid_refill_qty": 0, "ask_refill_qty": 0,
                "ofi": 0, "close_bid_price": None, "close_bid_depth": 0,
                "close_ask_price": None, "close_ask_depth": 0,
                "cvd": None,
                "_cells": defaultdict(lambda: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]),
            }
            grouped[bucket] = row
        if bar.get("open") is not None and row["open"] is None:
            row["open"] = bar["open"]
        for key in ("high", "low"):
            value = bar.get(key)
            if value is None:
                continue
            if row[key] is None:
                row[key] = value
            elif key == "high":
                row[key] = max(row[key], value)
            else:
                row[key] = min(row[key], value)
        if bar.get("close") is not None:
            row["close"] = bar["close"]
        for key in ("close_bid_price", "close_bid_depth", "close_ask_price", "close_ask_depth"):
            if bar.get(key) is not None:
                row[key] = bar[key]
        for key in (
            "buy", "sell", "trades", "adds", "cancels", "modifies",
            "fills", "add_qty", "cancel_qty", "buy_trades", "sell_trades",
            "buy_ge_50", "buy_ge_100", "buy_ge_150",
            "sell_ge_50", "sell_ge_100", "sell_ge_150",
            "bid_add_qty", "ask_add_qty", "bid_cancel_qty", "ask_cancel_qty",
            "bid_refill_qty", "ask_refill_qty", "ofi",
        ):
            row[key] += int(bar.get(key) or 0)
        if bar.get("cvd") is not None:
            row["cvd"] = int(bar["cvd"])
        row["max_buy_trade"] = max(row["max_buy_trade"], int(bar.get("max_buy_trade") or 0))
        row["max_sell_trade"] = max(row["max_sell_trade"], int(bar.get("max_sell_trade") or 0))
        _merge_cells(row["_cells"], bar.get("cells") or [])
    output: list[dict[str, Any]] = []
    for bucket in sorted(grouped):
        row = grouped[bucket]
        row["delta"] = row["buy"] - row["sell"]
        row["cells"] = [
            [tick, *values] for tick, values in sorted(row.pop("_cells").items())
        ]
        output.append(row)
    return output


def load_cached_footprint(
    start: datetime,
    end: datetime,
    *,
    symbol: str = "MNQ",
    interval: str = "1m",
    limit: int = 10_000,
) -> dict[str, Any]:
    """Serialize cache decompression so rapid pans cannot multiply memory."""
    with _CACHE_READ_LOCK:
        return _load_cached_footprint_unlocked(
            start, end, symbol=symbol, interval=interval, limit=limit,
        )


def _load_cached_footprint_unlocked(
    start: datetime,
    end: datetime,
    *,
    symbol: str = "MNQ",
    interval: str = "1m",
    limit: int = 10_000,
) -> dict[str, Any]:
    """Load only compact cache rows intersecting a chart request."""
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    start_epoch = math.floor(start.astimezone(UTC).timestamp())
    end_epoch = math.ceil(end.astimezone(UTC).timestamp())
    if end_epoch < start_epoch:
        raise ValueError("end must not be earlier than start")
    interval_seconds = {"1m": 60, "5m": 300}.get(interval)
    if interval_seconds is None:
        raise ValueError("interval must be 1m or 5m")

    base = base_symbol(symbol).lower()
    cache_root = market_data.derived_path("orderflow", base)
    rows: list[dict[str, Any]] = []
    files: list[str] = []
    metadata: dict[str, Any] | None = None
    earliest_file_date = start.astimezone(UTC).date() - timedelta(days=1)
    latest_file_date = end.astimezone(UTC).date() + timedelta(days=1)
    prefix = f"footprint_{base}_"
    for path in sorted(cache_root.glob(f"footprint_{base}_*.json.gz")):
        try:
            file_date = date.fromisoformat(path.name[len(prefix):len(prefix) + 10])
        except ValueError:
            continue
        if file_date < earliest_file_date or file_date > latest_file_date:
            continue
        payload = read_footprint_cache(path)
        meta = payload.get("meta") or {}
        if base_symbol(meta.get("symbol") or base) != base.upper():
            continue
        day_cvd = 0
        selected: list[dict[str, Any]] = []
        source_bars = sorted(payload["bars"], key=lambda row: int(row.get("epoch") or 0))
        for row in source_bars:
            day_cvd += int(row.get("buy") or 0) - int(row.get("sell") or 0)
            epoch = int(row.get("epoch") or 0)
            if start_epoch <= epoch <= end_epoch:
                selected_row = dict(row)
                selected_row["cvd"] = day_cvd
                selected.append(selected_row)
        if not selected:
            continue
        rows.extend(selected)
        files.append(path.name)
        metadata = metadata or dict(meta)

    rows.sort(key=lambda row: int(row.get("epoch") or 0))
    rows = _rollup_bars(rows, interval_seconds)
    capped_limit = max(1, min(int(limit or 10_000), 10_000))
    total = len(rows)
    if total > capped_limit:
        rows = rows[-capped_limit:]
    meta = metadata or {
        "schema_version": CACHE_SCHEMA_VERSION,
        "symbol": base.upper(),
        "source_schema": "mbo",
        "tick_size": DEFAULT_TICK_SIZE,
        "cell_encoding": [
            "price_tick", "aggressive_buy", "aggressive_sell",
            "passive_bid_fill", "passive_ask_fill",
            "max_bid_depth", "max_ask_depth",
            "near_fill_bid_refill", "near_fill_ask_refill",
            "buy_50_99_qty", "buy_100_149_qty", "buy_150_plus_qty",
            "sell_50_99_qty", "sell_100_149_qty", "sell_150_plus_qty",
        ],
    }
    meta.update({
        "interval": interval,
        "interval_seconds": interval_seconds,
        "cvd": "rth_cumulative_aggressive_delta",
    })
    return {
        "available": bool(files),
        "source": "derived_orderflow_cache",
        "meta": meta,
        "bars": rows,
        "count": total,
        "shown": len(rows),
        "files": files,
    }
