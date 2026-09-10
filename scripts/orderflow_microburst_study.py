"""Measure rapid aggressive-order combinations against PI marks.

This is an offline research script.  It reads the raw Databento MBO stream,
not the compact one-minute chart cache, because the cache intentionally loses
the order-by-order sequence needed to distinguish ``1 x 150`` from ``5 x
30``.  The script never changes live, backtest, preset, or chart behaviour.

The definitions are deliberately explicit:

* ``1x150`` means one aggressive trade of at least 150 contracts.
* ``3x50@5s`` means the last three same-side aggressive trades were each at
  least 50 contracts and completed inside five seconds.  Intervening
  same-side trades are included in the measured burst total.
* ``cum150@5s`` means any same-side aggressive trades whose cumulative size
  reaches 150 inside five seconds.
* ``random_split`` is a deterministic seed of random partitions of 150 into
  3--8 minimum-size pieces.  It is a robustness bucket, not a fitted trading
  rule.

Events are marked only after their final qualifying MBO trade.  Five,
30--, and 60-second forward measurements therefore do not use the event
minute's future trades.  PI matching is intentionally one-way: the burst
must precede a same-direction QQQ PI mark by at most five minutes.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import random
import re
import statistics
import sys
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.data import market_data
from backend.data.pi_history import parse_ts
from scripts.orderflow_pi_relationship_study import _load_pi


UTC = timezone.utc
SESSION_TZ = ZoneInfo("America/Los_Angeles")
TICK_SIZE = 0.25
NANO = 1_000_000_000
PI_MATCH_SECONDS = 5 * 60
OUTCOME_HORIZONS = (5, 30, 60)
SEQUENCE_WINDOWS = (1, 5, 10, 30)
RANDOM_SEED = 20260909
MAX_TRADE_HISTORY = 2_000
QUALIFY_THRESHOLDS = (5, 10, 15, 25, 30, 50, 75, 150)


@dataclass(frozen=True)
class PatternSpec:
    name: str
    parts: tuple[int, ...]
    window_seconds: int
    family: str
    random_partition: bool = False
    cumulative: bool = False
    flexible_random: bool = False


def _record_int(record: object, name: str) -> int:
    try:
        try:
            value = record[name]  # type: ignore[index]
        except (IndexError, KeyError, TypeError):
            value = getattr(record, name, 0)
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _record_text(record: object, name: str) -> str:
    try:
        value = record[name]  # type: ignore[index]
    except (IndexError, KeyError, TypeError):
        value = getattr(record, name, "")
    if isinstance(value, bytes):
        value = bytes(value).decode("ascii", errors="ignore")
    return str(value or "").upper()


def _session_bounds_ns(trade_date: str) -> tuple[int, int]:
    day = date.fromisoformat(trade_date)
    start = datetime.combine(day, time(6, 30), tzinfo=SESSION_TZ).astimezone(UTC)
    end = datetime.combine(day, time(13, 0), tzinfo=SESSION_TZ).astimezone(UTC)
    return int(start.timestamp() * NANO), int(end.timestamp() * NANO)


def _price_tick(price_nano: int) -> int | None:
    if price_nano <= 0 or price_nano >= 10**18:
        return None
    return int(round(price_nano / NANO / TICK_SIZE))


def _cache_dates() -> list[str]:
    root = market_data.derived_path("orderflow", "mnq")
    dates: list[str] = []
    for path in sorted(root.glob("footprint_mnq_*.json.gz")):
        value = path.name[len("footprint_mnq_"):len("footprint_mnq_") + 10]
        try:
            date.fromisoformat(value)
        except ValueError:
            continue
        dates.append(value)
    return dates


def _source_files(trade_date: str) -> list[Path]:
    compact = trade_date.replace("-", "")
    root = market_data.orderflow_root()
    return sorted(root.rglob(f"*{compact}*.mbo.dbn.zst"))


def _make_patterns() -> tuple[list[PatternSpec], list[tuple[int, ...]]]:
    patterns: list[PatternSpec] = [
        PatternSpec("1x150", (150,), 1, "fixed_sequence"),
    ]
    fixed = ((2, 75), (3, 50), (5, 30), (6, 25), (10, 15), (15, 10), (30, 5))
    for count, minimum in fixed:
        parts = (minimum,) * count
        label = f"{count}x{minimum}"
        for window in SEQUENCE_WINDOWS:
            patterns.append(PatternSpec(
                f"{label}@{window}s", parts, window, "fixed_sequence",
            ))

    for window in SEQUENCE_WINDOWS:
        patterns.append(PatternSpec(
            f"cum150@{window}s", (150,), window, "cumulative_150", cumulative=True,
        ))

    rng = random.Random(RANDOM_SEED)
    random_partitions: list[tuple[int, ...]] = []
    for count in range(3, 9):
        seen: set[tuple[int, ...]] = set()
        while len(seen) < 6:
            values = [5] * count
            remaining = 150 - 5 * count
            while remaining:
                index = rng.randrange(count)
                increment = min(remaining, rng.randint(1, max(1, remaining // 2)))
                values[index] += increment
                remaining -= increment
            candidate = tuple(values)
            if candidate in seen:
                continue
            seen.add(candidate)
            random_partitions.append(candidate)
    for index, parts in enumerate(random_partitions, 1):
        # Keep the deterministic partitions in the report as an audit trail,
        # but test the useful union as one flexible detector.  Running every
        # sampled partition independently would multiply overlapping events
        # without adding independent information.
        if index == 1:
            for window in (5, 30):
                patterns.append(PatternSpec(
                    f"random_split@{window}s",
                    (5,),
                    window,
                    "random_split",
                    random_partition=True,
                    flexible_random=True,
                ))
    del index, parts
    return patterns, random_partitions


def _new_second(sec: int) -> dict[str, Any]:
    return {
        "sec": sec,
        "last_tick": None,
        "high_tick": None,
        "low_tick": None,
        "buy_qty": 0,
        "sell_qty": 0,
        "trade_count_B": 0,
        "trade_count_A": 0,
        "add_A": 0,
        "add_B": 0,
        "fill_A": 0,
        "fill_B": 0,
        "cancel_A": 0,
        "cancel_B": 0,
    }


def _record_trade(bucket: dict[str, Any], side: str, tick: int, size: int) -> None:
    bucket["buy_qty" if side == "B" else "sell_qty"] += size
    bucket[f"trade_count_{side}"] += 1
    bucket["last_tick"] = tick
    bucket["high_tick"] = tick if bucket["high_tick"] is None else max(bucket["high_tick"], tick)
    bucket["low_tick"] = tick if bucket["low_tick"] is None else min(bucket["low_tick"], tick)


def _match_sequence(
    trades: deque[dict[str, Any]], spec: PatternSpec, end_sec: int,
    qualifying: deque[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    start_sec = end_sec - spec.window_seconds
    selected: list[dict[str, Any]] = []
    if spec.flexible_random:
        for row in reversed(trades):
            if int(row["sec"]) < start_sec:
                break
            if int(row["size"]) < 5:
                continue
            selected.append(row)
            if len(selected) > 8:
                return None
            if sum(int(item["size"]) for item in selected) >= 150:
                break
        if len(selected) < 3 or sum(int(item["size"]) for item in selected) < 150:
            return None
        selected.reverse()
    elif spec.cumulative:
        total = 0
        for row in reversed(trades):
            if int(row["sec"]) < start_sec:
                break
            selected.append(row)
            total += int(row["size"])
            if total >= spec.parts[0]:
                break
        if total < spec.parts[0]:
            return None
        selected.reverse()
    elif len(set(spec.parts)) == 1 and qualifying is not None:
        # The common fixed patterns can use a threshold-specific deque rather
        # than repeatedly scanning every small trade in the rolling history.
        for row in reversed(qualifying):
            if int(row["sec"]) < start_sec:
                break
            selected.append(row)
            if len(selected) >= len(spec.parts):
                break
        if len(selected) < len(spec.parts):
            return None
        selected.reverse()
    else:
        required_index = len(spec.parts) - 1
        for row in reversed(trades):
            if int(row["sec"]) < start_sec:
                break
            if int(row["size"]) >= spec.parts[required_index]:
                selected.append(row)
                required_index -= 1
                if required_index < 0:
                    break
        if required_index >= 0:
            return None
        selected.reverse()
    if not selected or int(selected[-1]["sec"]) != end_sec:
        # A burst is only new when the current second contains its final
        # qualifying trade.  This prevents a quiet second from re-emitting
        # the same old 150-contract trade after the cooldown.
        return None
    first_ts_ns = int(selected[0]["ts_ns"])
    last_ts_ns = int(selected[-1]["ts_ns"])
    span = [
        row for row in trades
        if first_ts_ns <= int(row["ts_ns"]) <= last_ts_ns
    ]
    return {
        "selected": selected,
        "span": span,
        "actual_qty": sum(int(row["size"]) for row in span),
        "trade_count": len(span),
        "first_ts_ns": int(selected[0]["ts_ns"]),
        "last_ts_ns": int(selected[-1]["ts_ns"]),
    }


def _event_from_match(
    trade_date: str, spec: PatternSpec, side: str, match: dict[str, Any],
) -> dict[str, Any]:
    direction = 1 if side == "B" else -1
    span = match["span"]
    selected = match["selected"]
    first_tick = int(selected[0]["tick"])
    last_tick = int(selected[-1]["tick"])
    prices = [int(row["tick"]) for row in span]
    actual_qty = int(match["actual_qty"])
    mode_qty = Counter()
    for row in span:
        mode_qty[int(row["tick"])] += int(row["size"])
    return {
        "trade_date": trade_date,
        "event_sec": int(selected[-1]["sec"]),
        "event_ts_ns": int(match["last_ts_ns"]),
        "pattern": spec.name,
        "family": spec.family,
        "random_partition": bool(spec.random_partition),
        "required_parts": list(spec.parts),
        "window_seconds": spec.window_seconds,
        "side": "buy" if direction > 0 else "sell",
        "direction": direction,
        "actual_qty": actual_qty,
        "trade_count": int(match["trade_count"]),
        "duration_ms": round((match["last_ts_ns"] - match["first_ts_ns"]) / 1_000_000, 3),
        "max_trade": max(int(row["size"]) for row in span),
        "first_tick": first_tick,
        "last_tick": last_tick,
        "price_span_ticks": max(prices) - min(prices),
        "event_impact_ticks": direction * (last_tick - first_tick),
        "unique_price_count": len(set(prices)),
        "same_price_qty_ratio": round(max(mode_qty.values()) / max(1, actual_qty), 4),
        "order_id_count": sum(1 for row in span if int(row.get("order_id") or 0) > 0),
        "unique_order_id_count": len({
            int(row["order_id"]) for row in span if int(row.get("order_id") or 0) > 0
        }),
    }


def _evaluate_patterns(
    trade_date: str,
    end_sec: int,
    trades: dict[str, deque[dict[str, Any]]],
    qualifying: dict[tuple[str, int], deque[dict[str, Any]]],
    current_max_size: dict[str, int],
    current_trade_qty: dict[str, int],
    patterns: Iterable[PatternSpec],
    last_event_sec: dict[tuple[str, str], int],
    events: list[dict[str, Any]],
) -> None:
    cutoff = end_sec - max(SEQUENCE_WINDOWS) - 2
    for history in trades.values():
        while history and int(history[0]["sec"]) < cutoff:
            history.popleft()
    for spec in patterns:
        for side in ("B", "A"):
            if spec.cumulative:
                if current_trade_qty[side] <= 0:
                    continue
                source = None
            else:
                if current_max_size[side] < spec.parts[-1]:
                    continue
                source = qualifying.get((side, spec.parts[0])) if len(set(spec.parts)) == 1 else None
            match = _match_sequence(trades[side], spec, end_sec, source)
            if match is None:
                continue
            key = (spec.name, side)
            previous = last_event_sec.get(key)
            if previous is not None and end_sec - previous < max(1, spec.window_seconds):
                continue
            last_event_sec[key] = end_sec
            events.append(_event_from_match(trade_date, spec, side, match))


def _nearest_tick(
    trade_seconds: list[int], price_by_sec: dict[int, int], target_sec: int,
) -> int | None:
    index = bisect_right(trade_seconds, target_sec) - 1
    if index < 0:
        return None
    return price_by_sec[trade_seconds[index]]


def _forward_outcome(
    event_sec: int,
    event_tick: int,
    direction: int,
    horizon: int,
    trade_seconds: list[int],
    seconds: dict[int, dict[str, Any]],
) -> dict[str, Any] | None:
    start_index = bisect_right(trade_seconds, event_sec)
    end_index = bisect_right(trade_seconds, event_sec + horizon)
    if start_index >= end_index:
        return None
    rows = [seconds[sec] for sec in trade_seconds[start_index:end_index]]
    last_tick = int(rows[-1]["last_tick"])
    directional_returns = [direction * (int(row["last_tick"]) - event_tick) for row in rows]
    mfe_values: list[int] = []
    mae_values: list[int] = []
    for row in rows:
        if row["high_tick"] is not None:
            mfe_values.append(direction * (int(row["high_tick"]) - event_tick))
        if row["low_tick"] is not None:
            mae_values.append(direction * (int(row["low_tick"]) - event_tick))
    return {
        "return_ticks": directional_returns[-1],
        "mfe_ticks": max(mfe_values) if mfe_values else None,
        "mae_ticks": min(mae_values) if mae_values else None,
        "win": directional_returns[-1] > 0,
        "observed_to_sec": int(rows[-1]["sec"]),
    }


def _attach_microstructure(
    event: dict[str, Any], trade_seconds: list[int], price_by_sec: dict[int, int],
    seconds: dict[int, dict[str, Any]],
) -> None:
    event_sec = int(event["event_sec"])
    event_tick = int(event["last_tick"])
    direction = int(event["direction"])
    before = _nearest_tick(trade_seconds, price_by_sec, event_sec - 30)
    event["pretrend_30s_ticks"] = direction * (event_tick - before) if before is not None else None
    opposite = "A" if direction > 0 else "B"
    for horizon in (1, 3):
        add = 0
        fill = 0
        cancel = 0
        for sec in range(event_sec + 1, event_sec + horizon + 1):
            row = seconds.get(sec)
            if row is None:
                continue
            add += int(row[f"add_{opposite}"])
            fill += int(row[f"fill_{opposite}"])
            cancel += int(row[f"cancel_{opposite}"])
        event[f"opposite_add_{horizon}s"] = add
        event[f"opposite_fill_{horizon}s"] = fill
        event[f"opposite_cancel_{horizon}s"] = cancel
    for horizon in OUTCOME_HORIZONS:
        event[f"outcome_{horizon}s"] = _forward_outcome(
            event_sec, event_tick, direction, horizon, trade_seconds, seconds,
        )

    labels: list[str] = []
    impact = abs(int(event["event_impact_ticks"]))
    return_30 = (event.get("outcome_30s") or {}).get("return_ticks")
    opposite_add = int(event.get("opposite_add_3s") or 0)
    opposite_fill = int(event.get("opposite_fill_3s") or 0)
    actual_qty = max(1, int(event["actual_qty"]))
    if int(event["event_impact_ticks"]) >= 2 and return_30 is not None and return_30 > 0:
        labels.append("active_continuation")
    if impact <= 1 and opposite_fill > 0 and opposite_add >= max(50, actual_qty // 2):
        labels.append("possible_absorption")
    if impact <= 1 and return_30 is not None and return_30 < -2:
        labels.append("exhaustion_or_reversal")
    pretrend = event.get("pretrend_30s_ticks")
    if pretrend is not None and pretrend > 8 and return_30 is not None and return_30 < -2:
        labels.append("potential_profit_taking_reversal")
    if opposite_add > 0 and opposite_fill > 0:
        labels.append("opposite_passive_activity")
    event["labels"] = labels or ["unclassified"]


def _pi_index() -> dict[int, list[tuple[int, dict[str, Any]]]]:
    result: dict[int, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for row in _load_pi():
        try:
            ts_ns = int(parse_ts(row["ts"]).timestamp() * NANO)
        except Exception:
            continue
        direction = int(row.get("direction") or 0)
        if direction in {-1, 1}:
            result[direction].append((ts_ns, row))
    for rows in result.values():
        rows.sort(key=lambda item: item[0])
    return result


def _attach_pi(event: dict[str, Any], pi_rows: dict[int, list[tuple[int, dict[str, Any]]]]) -> None:
    direction = int(event["direction"])
    rows = pi_rows.get(direction, [])
    timestamps = [item[0] for item in rows]
    event_ns = int(event["event_ts_ns"])
    index = bisect_left(timestamps, event_ns)
    match: dict[str, Any] | None = None
    if index < len(rows) and rows[index][0] <= event_ns + PI_MATCH_SECONDS * NANO:
        ts_ns, row = rows[index]
        raw_level = str(row.get("size") or "")
        level_match = re.search(r"(\d+)", raw_level)
        match = {
            "message_id": row.get("message_id"),
            "ts": row.get("ts"),
            "delay_seconds": round((ts_ns - event_ns) / NANO, 3),
            "kind": row.get("kind"),
            "size": raw_level or "?",
            "level": int(level_match.group(1)) if level_match else None,
        }
    event["pi_match"] = match


def _run_day(
    trade_date: str,
    source_files: list[Path],
    patterns: list[PatternSpec],
    pi_rows: dict[int, list[tuple[int, dict[str, Any]]]],
) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]], int]:
    import databento as db

    start_ns, end_ns = _session_bounds_ns(trade_date)
    session_start_sec = start_ns // NANO
    session_count = int((end_ns - start_ns) // NANO)
    seconds: dict[int, dict[str, Any]] = {
        session_start_sec + offset: _new_second(session_start_sec + offset)
        for offset in range(session_count)
    }
    trades = {
        "B": deque(maxlen=MAX_TRADE_HISTORY),
        "A": deque(maxlen=MAX_TRADE_HISTORY),
    }
    qualifying = {
        (side, threshold): deque(maxlen=MAX_TRADE_HISTORY)
        for side in ("B", "A")
        for threshold in QUALIFY_THRESHOLDS
    }
    events: list[dict[str, Any]] = []
    last_event_sec: dict[tuple[str, str], int] = {}
    state = {
        "current_sec": None,
        "current_had_trade": False,
        "current_max_size": {"B": 0, "A": 0},
        "current_trade_qty": {"B": 0, "A": 0},
    }
    record_count = 0
    snapshot_flag = int(db.RecordFlags.F_SNAPSHOT)

    def flush_current() -> None:
        current = state["current_sec"]
        if current is None or not state["current_had_trade"]:
            return
        _evaluate_patterns(
            trade_date,
            int(current),
            trades,
            qualifying,
            state["current_max_size"],
            state["current_trade_qty"],
            patterns,
            last_event_sec,
            events,
        )

    def consume(record: object) -> None:
        ts_ns = _record_int(record, "ts_event")
        if ts_ns < start_ns or ts_ns >= end_ns:
            return
        flags = _record_int(record, "flags")
        if flags & snapshot_flag:
            return
        sec = ts_ns // NANO
        current = state["current_sec"]
        if current is None:
            state["current_sec"] = sec
            state["current_had_trade"] = False
            state["current_max_size"] = {"B": 0, "A": 0}
            state["current_trade_qty"] = {"B": 0, "A": 0}
        elif sec > current:
            flush_current()
            state["current_sec"] = sec
            state["current_had_trade"] = False
        bucket = seconds.setdefault(sec, _new_second(sec))
        action = _record_text(record, "action")
        side = _record_text(record, "side")
        size = max(0, _record_int(record, "size"))
        if side not in {"A", "B"} or size <= 0:
            return
        if action == "T":
            tick = _price_tick(_record_int(record, "price"))
            if tick is None:
                return
            _record_trade(bucket, side, tick, size)
            trades[side].append({
                "sec": sec,
                "ts_ns": ts_ns,
                "size": size,
                "tick": tick,
                "order_id": _record_int(record, "order_id"),
            })
            trade = trades[side][-1]
            for threshold in QUALIFY_THRESHOLDS:
                if size >= threshold:
                    qualifying[(side, threshold)].append(trade)
            state["current_max_size"][side] = max(state["current_max_size"][side], size)
            state["current_trade_qty"][side] += size
            state["current_had_trade"] = True
            return
        if action in {"A", "F", "C"}:
            bucket[f"{action_name(action)}_{side}"] += size

    import numpy as np

    # Adds/fills/cancels are needed only as one-second passive-liquidity
    # totals.  Aggregate those 35M-ish book events in numpy; only the roughly
    # 1M aggressive trades need Python order-by-order sequence handling.
    passive_totals = np.zeros((3, 2, session_count), dtype=np.int64)

    for source in source_files:
        store = db.DBNStore.from_file(source)
        if str(store.schema) != "mbo":
            raise ValueError(f"expected MBO DBN, got {store.schema}: {source}")
        # A numpy stream keeps decompression in Databento's native reader and
        # applies the expensive session/snapshot filter to a whole chunk.  A
        # Python callback for every overnight book event is several times
        # slower on the entitled MBO files.
        for chunk in store.to_ndarray(schema="mbo", count=250_000):
            record_count += len(chunk)
            valid = (
                (chunk["ts_event"] >= start_ns)
                & (chunk["ts_event"] < end_ns)
                & ((chunk["flags"].astype(np.uint16) & snapshot_flag) == 0)
                & (chunk["size"] > 0)
                & np.isin(chunk["side"], np.asarray([b"A", b"B"], dtype="S1"))
            )
            trade_mask = valid & (chunk["action"] == b"T")
            for record in chunk[trade_mask]:
                consume(record)
            for action_index, action in enumerate((b"A", b"F", b"C")):
                for side_index, side in enumerate((b"A", b"B")):
                    passive_mask = valid & (chunk["action"] == action) & (chunk["side"] == side)
                    if not passive_mask.any():
                        continue
                    offsets = ((chunk["ts_event"][passive_mask] - start_ns) // NANO).astype(np.int64)
                    passive_totals[action_index, side_index] += np.bincount(
                        offsets,
                        weights=chunk["size"][passive_mask].astype(np.int64),
                        minlength=session_count,
                    ).astype(np.int64)

    for offset in range(session_count):
        row = seconds[session_start_sec + offset]
        for action_index, action in enumerate(("add", "fill", "cancel")):
            for side_index, side in enumerate(("A", "B")):
                row[f"{action}_{side}"] = int(passive_totals[action_index, side_index, offset])
    flush_current()

    trade_seconds = sorted(
        sec for sec, row in seconds.items() if row.get("last_tick") is not None
    )
    price_by_sec = {sec: int(seconds[sec]["last_tick"]) for sec in trade_seconds}
    for event in events:
        _attach_microstructure(event, trade_seconds, price_by_sec, seconds)
        _attach_pi(event, pi_rows)
        event.pop("event_ts_ns", None)
    return events, seconds, record_count


def action_name(action: str) -> str:
    return {"A": "add", "F": "fill", "C": "cancel"}[action]


def _new_accumulator() -> dict[str, Any]:
    return {
        "days": set(),
        "actual_qty": [],
        "trade_count": [],
        "duration_ms": [],
        "event_impact_ticks": [],
        "same_price_qty_ratio": [],
        "return_5s": [],
        "return_30s": [],
        "return_60s": [],
        "win_5s": [],
        "win_30s": [],
        "win_60s": [],
        "mfe_30s": [],
        "mae_30s": [],
        "opposite_add_1s": [],
        "opposite_fill_1s": [],
        "opposite_add_3s": [],
        "opposite_fill_3s": [],
        "labels": Counter(),
        "directions": Counter(),
        "pi_kinds": Counter(),
        "pi_levels": Counter(),
        "pi_ids": set(),
        "pi_matches": 0,
    }


def _add_accumulator(acc: dict[str, Any], event: dict[str, Any]) -> None:
    acc["days"].add(event["trade_date"])
    for key in (
        "actual_qty", "trade_count", "duration_ms", "event_impact_ticks",
        "same_price_qty_ratio", "opposite_add_1s", "opposite_fill_1s",
        "opposite_add_3s", "opposite_fill_3s",
    ):
        value = event.get(key)
        if value is not None:
            acc[key].append(float(value))
    acc["directions"][event["side"]] += 1
    acc["labels"].update(event.get("labels") or [])
    for horizon in OUTCOME_HORIZONS:
        outcome = event.get(f"outcome_{horizon}s")
        if not outcome:
            continue
        acc[f"return_{horizon}s"].append(float(outcome["return_ticks"]))
        acc[f"win_{horizon}s"].append(bool(outcome["win"]))
        if horizon == 30:
            if outcome.get("mfe_ticks") is not None:
                acc["mfe_30s"].append(float(outcome["mfe_ticks"]))
            if outcome.get("mae_ticks") is not None:
                acc["mae_30s"].append(float(outcome["mae_ticks"]))
    match = event.get("pi_match")
    if match:
        acc["pi_matches"] += 1
        if match.get("message_id"):
            acc["pi_ids"].add(str(match["message_id"]))
        if match.get("kind"):
            acc["pi_kinds"][str(match["kind"])] += 1
        if match.get("level") is not None:
            acc["pi_levels"][str(match["level"])] += 1


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 4) if values else None


def _finalize_accumulator(acc: dict[str, Any]) -> dict[str, Any]:
    n = len(acc["actual_qty"])
    output: dict[str, Any] = {
        "n": n,
        "days": len(acc["days"]),
        "median_actual_qty": _median(acc["actual_qty"]),
        "median_trade_count": _median(acc["trade_count"]),
        "median_duration_ms": _median(acc["duration_ms"]),
        "median_event_impact_ticks": _median(acc["event_impact_ticks"]),
        "median_same_price_qty_ratio": _median(acc["same_price_qty_ratio"]),
        "median_return_5s_ticks": _median(acc["return_5s"]),
        "median_return_30s_ticks": _median(acc["return_30s"]),
        "median_return_60s_ticks": _median(acc["return_60s"]),
        "median_mfe_30s_ticks": _median(acc["mfe_30s"]),
        "median_mae_30s_ticks": _median(acc["mae_30s"]),
        "median_opposite_add_1s": _median(acc["opposite_add_1s"]),
        "median_opposite_fill_1s": _median(acc["opposite_fill_1s"]),
        "median_opposite_add_3s": _median(acc["opposite_add_3s"]),
        "median_opposite_fill_3s": _median(acc["opposite_fill_3s"]),
        "directions": dict(acc["directions"]),
        "labels": dict(acc["labels"]),
        "pi_matches": int(acc["pi_matches"]),
        "pi_match_rate": round(acc["pi_matches"] / n, 4) if n else 0.0,
        "unique_pi_ids": len(acc["pi_ids"]),
        "pi_kinds": dict(acc["pi_kinds"]),
        "pi_levels": dict(acc["pi_levels"]),
    }
    for horizon in OUTCOME_HORIZONS:
        wins = acc[f"win_{horizon}s"]
        output[f"outcome_{horizon}s_n"] = len(wins)
        output[f"win_rate_{horizon}s"] = round(sum(wins) / len(wins), 4) if wins else None
    return output


def _summary_keys(event: dict[str, Any]) -> list[str]:
    keys = [event["pattern"], event["family"], "all"]
    if event["family"] == "random_split":
        keys.append("random_split")
    elif event["family"] == "fixed_sequence":
        keys.append("fixed_sequences")
    return keys


def _update_summaries(
    accumulators: dict[str, dict[str, Any]], event: dict[str, Any],
) -> None:
    for key in _summary_keys(event):
        acc = accumulators.setdefault(key, _new_accumulator())
        _add_accumulator(acc, event)


def _summarize_accumulators(
    accumulators: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {key: _finalize_accumulator(value) for key, value in sorted(accumulators.items())}


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _write_markdown(
    path: Path, report: dict[str, Any], summaries: dict[str, dict[str, Any]],
) -> None:
    coverage = report["coverage"]
    lines = [
        "# Raw MBO micro-burst study",
        "",
        "Offline research only; no live/backtest/frontend code was changed.",
        "",
        f"- RTH timezone: `{SESSION_TZ.key}`; session: `06:30–13:00` local.",
        f"- Dates processed: **{coverage['days']}**; raw records replayed: **{coverage['source_records']}**.",
        f"- MBO events: **{coverage['events']}**; PI match window: **0–{PI_MATCH_SECONDS // 60} minutes** after the burst.",
        "- A match is a sequence of same-side aggressive `T` events. It is not proof of one institution or one parent order.",
        "",
        "## Definitions",
        "",
        "| Family | Meaning |",
        "|---|---|",
        "| Fixed sequence | Each minimum part must be a separate same-side aggressive trade; intervening same-side trades count toward measured quantity. |",
        "| Cumulative 150 | Any same-side aggressive trades reach 150 inside the window. |",
        "| Random split | Seeded partitions of 150 into 3–8 minimum pieces, evaluated as a robustness bucket. |",
        "| Possible absorption | Descriptive label: little event price progress plus opposite-side adds/fills soon after. It does not identify the trader. |",
        "",
        "## Aggregate results",
        "",
        "| Pattern / group | n | median trades | median duration ms | median impact ticks | median return 30s | win 30s | possible absorption | PI matches |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, row in summaries.items():
        if row["n"] == 0:
            continue
        lines.append(
            f"| `{name}` | {row['n']} | {_fmt(row['median_trade_count'])} | "
            f"{_fmt(row['median_duration_ms'])} | {_fmt(row['median_event_impact_ticks'])} | "
            f"{_fmt(row['median_return_30s_ticks'])} | {_fmt(row['win_rate_30s'])} | "
            f"{row['labels'].get('possible_absorption', 0)} | {row['pi_matches']} |"
        )
    lines.extend([
        "",
        "## Interpretation guardrails",
        "",
        "A 3×50 or 5×30 label is a footprint pattern, not proof that the market saw a hidden 150-contract parent order. MBO `order_id` and exact sequencing are retained in the raw feed, but an aggressor order can be represented by multiple trade records and a displayed size can be refreshed. Results are therefore evidence about observable flow, not participant identity.",
        "",
        "Forward outcomes are raw price ticks after the final qualifying trade. They are not a simulated entry/exit strategy and exclude commission, slippage, ATR stops, and position overlap. PI matching is a lead/lag diagnostic only.",
        "",
    ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def _parse_dates(raw: str | None) -> list[str]:
    if not raw:
        return _cache_dates()
    values: list[str] = []
    for piece in raw.split(","):
        candidate = piece.strip()
        if not candidate:
            continue
        date.fromisoformat(candidate)
        values.append(candidate)
    return sorted(dict.fromkeys(values))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dates", help="comma-separated RTH dates; default is every footprint cache date")
    parser.add_argument("--max-days", type=int, default=0, help="limit dates after sorting; 0 means all")
    parser.add_argument("--output-stem", default="orderflow_microburst_study")
    args = parser.parse_args()

    patterns, random_partitions = _make_patterns()
    dates = _parse_dates(args.dates)
    if args.max_days > 0:
        dates = dates[:args.max_days]
    if not dates:
        raise SystemExit("No RTH cache dates found")

    pi_rows = _pi_index()
    report_dir = market_data.derived_path("research")
    json_path = report_dir / f"{args.output_stem}.json"
    markdown_path = report_dir / f"{args.output_stem}.md"
    events_path = report_dir / f"{args.output_stem}.events.jsonl.gz"
    report_dir.mkdir(parents=True, exist_ok=True)

    accumulators: dict[str, dict[str, Any]] = {}
    coverage = {
        "requested_dates": dates,
        "processed_dates": [],
        "missing_raw_dates": [],
        "source_files": [],
        "source_records": 0,
        "events": 0,
    }
    with gzip.open(events_path, "wt", encoding="utf-8", compresslevel=6) as event_handle:
        for trade_date in dates:
            files = _source_files(trade_date)
            if not files:
                coverage["missing_raw_dates"].append(trade_date)
                print(f"SKIP {trade_date}: no raw MBO file", flush=True)
                continue
            print(
                f"RUN {trade_date}: {len(files)} raw file(s) "
                f"({sum(path.stat().st_size for path in files) / 1024**3:.2f} GiB)",
                flush=True,
            )
            events, _, record_count = _run_day(trade_date, files, patterns, pi_rows)
            for event in events:
                event_handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
                _update_summaries(accumulators, event)
            coverage["processed_dates"].append(trade_date)
            coverage["source_files"].extend(str(path) for path in files)
            coverage["source_records"] += record_count
            coverage["events"] += len(events)
            print(
                f"DONE {trade_date}: records={record_count:,} events={len(events):,}",
                flush=True,
            )

    summaries = _summarize_accumulators(accumulators)
    report = {
        "study": "raw_mbo_microburst_vs_pi",
        "generated_at": datetime.now(UTC).isoformat(),
        "source_schema": "mbo",
        "symbol": "MNQ",
        "tick_size": TICK_SIZE,
        "rth_timezone": SESSION_TZ.key,
        "rth_local": "06:30-13:00",
        "pi_match_window_seconds": PI_MATCH_SECONDS,
        "outcome_horizons_seconds": list(OUTCOME_HORIZONS),
        "coverage": {
            **coverage,
            "days": len(coverage["processed_dates"]),
        },
        "random_seed": RANDOM_SEED,
        "random_partitions": [list(parts) for parts in random_partitions],
        "patterns": summaries,
        "events_file": str(events_path),
    }
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_markdown(markdown_path, report, summaries)
    print(f"REPORT {json_path}", flush=True)
    print(f"REPORT {markdown_path}", flush=True)
    print(f"EVENTS {events_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
