"""Test live-safe order-flow contexts with one fixed PI-style exit.

This is an offline research study.  It adds context features that can be
derived from the existing MNQ MBO compact cache:

* session VWAP and previous-RTH value location;
* a rolling RTH volume-profile wave (30-minute POC migration);
* diagonal 1:10 footprint imbalance;
* passive bid/ask heatmap touches and rejection/no-breakout entries;
* a 150+ buy-then-sell (or sell-then-buy) reversal at a persistent level.

Every entry is evaluated at the next one-minute open.  All rows use the same
fixed ATR-blend PI exit geometry; no threshold is fitted to the result set.
The study is deliberately separate from production strategy code.
"""

from __future__ import annotations

import csv
import gzip
import json
import math
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.data import market_data
from scripts.orderflow_filter_exit_study import (
    _apply_cooldown,
    _atr_history,
    _levels,
    _metrics,
    _run_variant,
    _simulate,
)
from scripts.orderflow_pi_relationship_study import (
    DISCOVERY_END,
    _candidate_events,
    _enrich_day,
    _has_ohlc,
    _load_days,
    _load_pi,
    _profile,
)


UTC = timezone.utc
TICK_SIZE = 0.25
PROFILE_WINDOW = 30
PERSISTENCE_WINDOW = 10
PERSISTENCE_MIN_BARS = 6
LEVEL_TOLERANCE_TICKS = 2
IMBALANCE_RATIO = 10
IMBALANCE_MIN_QTY = 10
CONTRACTS = 1
FIXED_EXIT_POLICY = "atr_blend"

CONTEXTS = (
    "all",
    "vwap_above",
    "vwap_below",
    "vwap_aligned",
    "value_above_vah",
    "value_between_vah_poc",
    "value_between_poc_val",
    "value_below_val",
    "value_between",
    "value_reversion",
    "value_breakout",
    "profile_wave_aligned",
    "imbalance_1_10_aligned",
    "passive_rejection",
    "cvd_aligned",
    "cvd_divergence",
    "vwap+imbalance",
    "value+wave",
    "passive+imbalance",
    "cvd+vwap",
    "cvd+value",
    "cvd+imbalance",
    "cvd+passive",
    "vwap+value+wave+imbalance",
    "vwap+value+wave+imbalance+cvd",
)
ENTRY_FAMILIES = (
    "large_150_reversal",
    "mbo_turnover",
    "passive_touch_reject",
    "pi_gated",
)


def _cell_rows(bar: dict[str, Any]) -> list[list[int]]:
    return [
        row for row in (bar.get("cells") or [])
        if isinstance(row, (list, tuple)) and len(row) >= 9
    ]


def _trade_profile(bar: dict[str, Any]) -> tuple[Counter[int], int, int]:
    volume: Counter[int] = Counter()
    total = 0
    weighted_ticks = 0
    for row in _cell_rows(bar):
        tick = int(row[0])
        quantity = int(row[1] or 0) + int(row[2] or 0)
        if quantity <= 0:
            continue
        volume[tick] += quantity
        total += quantity
        weighted_ticks += tick * quantity
    return volume, total, weighted_ticks


def _profile_stats(volume: Counter[int]) -> dict[str, int] | None:
    if not volume:
        return None
    poc_tick = max(volume, key=lambda tick: (volume[tick], -tick))
    target = sum(volume.values()) * 0.70
    included = {poc_tick}
    accumulated = volume[poc_tick]
    low = high = poc_tick
    while accumulated < target:
        lower = low - 1
        upper = high + 1
        lower_volume = volume.get(lower, 0)
        upper_volume = volume.get(upper, 0)
        if lower_volume == 0 and upper_volume == 0:
            remaining = [tick for tick in volume if tick not in included]
            if not remaining:
                break
            chosen = max(remaining, key=lambda tick: (volume[tick], -abs(tick - poc_tick)))
        else:
            chosen = upper if upper_volume >= lower_volume else lower
        included.add(chosen)
        accumulated += volume.get(chosen, 0)
        low = min(low, chosen)
        high = max(high, chosen)
    return {
        "poc_tick": int(poc_tick),
        "vah_tick": int(high),
        "val_tick": int(low),
        "mid_tick": int(round((high + low) / 2.0)),
    }


def _imbalance_1_10(bar: dict[str, Any]) -> dict[str, Any]:
    cells = {int(row[0]): row for row in _cell_rows(bar)}
    buy_levels: list[int] = []
    sell_levels: list[int] = []
    buy_ratio = 0.0
    sell_ratio = 0.0
    for tick, row in cells.items():
        buy = int(row[1] or 0)
        sell = int(row[2] or 0)
        diagonal_sell = int(cells.get(tick - 1, [0, 0, 0])[2] or 0)
        diagonal_buy = int(cells.get(tick + 1, [0, 0])[1] or 0)
        if buy >= IMBALANCE_MIN_QTY and buy >= IMBALANCE_RATIO * max(1, diagonal_sell):
            buy_levels.append(tick)
            buy_ratio = max(buy_ratio, buy / max(1, diagonal_sell))
        if sell >= IMBALANCE_MIN_QTY and sell >= IMBALANCE_RATIO * max(1, diagonal_buy):
            sell_levels.append(tick)
            sell_ratio = max(sell_ratio, sell / max(1, diagonal_buy))
    if buy_levels and not sell_levels:
        side = 1
    elif sell_levels and not buy_levels:
        side = -1
    elif buy_levels and sell_levels:
        side = 1 if len(buy_levels) > len(sell_levels) else -1 if len(sell_levels) > len(buy_levels) else 0
    else:
        side = 0
    return {
        "imbalance_1_10_side": side,
        "imbalance_1_10_buy_levels": len(buy_levels),
        "imbalance_1_10_sell_levels": len(sell_levels),
        "imbalance_1_10_buy_max_ratio": round(buy_ratio, 3),
        "imbalance_1_10_sell_max_ratio": round(sell_ratio, 3),
    }


def _passive_level(bar: dict[str, Any], side: str) -> dict[str, Any] | None:
    depth_index, fill_index, refill_index = {
        "bid": (5, 3, 7),
        "ask": (6, 4, 8),
    }[side]
    best: tuple[int, int, int, int, int] | None = None
    for row in _cell_rows(bar):
        depth = int(row[depth_index] or 0)
        fill = int(row[fill_index] or 0)
        refill = int(row[refill_index] or 0)
        if depth <= 0 or fill + refill <= 0:
            continue
        score = depth + 2 * (fill + refill)
        candidate = (score, depth, fill + refill, int(row[0]), fill)
        if best is None or candidate > best:
            best = candidate
    if best is None:
        return None
    score, depth, activity, tick, fill = best
    return {
        "tick": tick,
        "price": tick * TICK_SIZE,
        "score": score,
        "depth": depth,
        "activity": activity,
        "fill": fill,
    }


def _touches(bar: dict[str, Any], level: dict[str, Any] | None) -> bool:
    if level is None or not _has_ohlc(bar):
        return False
    price = float(level["price"])
    return float(bar["low"]) <= price + TICK_SIZE and float(bar["high"]) >= price - TICK_SIZE


def _active_ticks(bar: dict[str, Any]) -> set[int]:
    active: set[int] = set()
    for row in _cell_rows(bar):
        trade_or_fill = sum(int(row[index] or 0) for index in (1, 2, 3, 4, 7, 8))
        if trade_or_fill > 0:
            active.add(int(row[0]))
    return active


def _near_level(active: set[int], tick: int) -> bool:
    return any(tick + offset in active for offset in range(-LEVEL_TOLERANCE_TICKS, LEVEL_TOLERANCE_TICKS + 1))


def _large_levels(bar: dict[str, Any], index: int) -> tuple[set[int], set[int]]:
    buy: set[int] = set()
    sell: set[int] = set()
    for row in _cell_rows(bar):
        if len(row) >= 15 and int(row[11] or 0) > 0:
            buy.add(int(row[0]))
        if len(row) >= 15 and int(row[14] or 0) > 0:
            sell.add(int(row[0]))
    return buy, sell


def _context_matches(name: str, row: dict[str, Any], direction: int) -> bool:
    vwap_state = row.get("vwap_state")
    value_location = row.get("value_location")
    value_zone = row.get("value_zone")
    wave = row.get("profile_wave")
    imbalance = int(row.get("imbalance_1_10_side") or 0)
    passive = int(row.get("passive_rejection_side") or 0)
    cvd_state = int(row.get("cvd_state_side") or 0)
    cvd_divergence = int(row.get("cvd_divergence_side") or 0)
    vwap_aligned = vwap_state == ("above" if direction > 0 else "below")
    value_reversion = value_location == ("below_val" if direction > 0 else "above_vah")
    value_breakout = value_location == ("above_vah" if direction > 0 else "below_val")
    wave_aligned = wave == ("up" if direction > 0 else "down")
    imbalance_aligned = imbalance == direction
    passive_rejection = passive == direction
    cvd_aligned = cvd_state == direction
    cvd_reversal = cvd_divergence == direction
    return {
        "all": True,
        "vwap_above": vwap_state == "above",
        "vwap_below": vwap_state == "below",
        "vwap_aligned": vwap_aligned,
        "value_above_vah": value_location == "above_vah",
        "value_between_vah_poc": value_zone == "between_vah_poc",
        "value_between_poc_val": value_zone == "between_poc_val",
        "value_below_val": value_location == "below_val",
        "value_between": value_location == "inside_value",
        "value_reversion": value_reversion,
        "value_breakout": value_breakout,
        "profile_wave_aligned": wave_aligned,
        "imbalance_1_10_aligned": imbalance_aligned,
        "passive_rejection": passive_rejection,
        "cvd_aligned": cvd_aligned,
        "cvd_divergence": cvd_reversal,
        "vwap+imbalance": vwap_aligned and imbalance_aligned,
        "value+wave": value_reversion and wave_aligned,
        "passive+imbalance": passive_rejection and imbalance_aligned,
        "cvd+vwap": cvd_aligned and vwap_aligned,
        "cvd+value": cvd_aligned and value_reversion,
        "cvd+imbalance": cvd_aligned and imbalance_aligned,
        "cvd+passive": cvd_aligned and passive_rejection,
        "vwap+value+wave+imbalance": vwap_aligned and value_reversion and wave_aligned and imbalance_aligned,
        "vwap+value+wave+imbalance+cvd": (
            vwap_aligned and value_reversion and wave_aligned
            and imbalance_aligned and cvd_aligned
        ),
    }.get(name, False)


def _snapshot(bar: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "vwap", "vwap_state", "value_location", "value_zone", "profile_wave",
        "profile_wave_poc_delta_ticks", "imbalance_1_10_side",
        "imbalance_1_10_buy_levels", "imbalance_1_10_sell_levels",
        "rth_cvd", "cvd_5_delta", "cvd_15_delta", "cvd_state_side",
        "cvd_divergence_side",
        "passive_bid_level_tick", "passive_ask_level_tick",
        "passive_bid_touch", "passive_ask_touch",
        "passive_rejection_side", "passive_level_persistence",
    )
    return {key: bar.get(key) for key in keys}


def _enrich_context_day(day: dict[str, Any]) -> None:
    bars = day["bars"]
    profiles: list[Counter[int]] = []
    active_ticks: list[set[int]] = []
    large_levels: list[tuple[set[int], set[int]]] = []
    cumulative_delta = 0
    cumulative_history: list[int] = []
    for bar in bars:
        cumulative_delta += int(bar.get("buy") or 0) - int(bar.get("sell") or 0)
        cumulative_history.append(cumulative_delta)
        index = len(cumulative_history) - 1
        bar["rth_cvd"] = cumulative_delta
        bar["cvd_5_delta"] = cumulative_delta - (
            cumulative_history[index - 5] if index >= 5 else 0
        )
        bar["cvd_15_delta"] = cumulative_delta - (
            cumulative_history[index - 15] if index >= 15 else 0
        )
        if index >= 14 and bar["cvd_5_delta"] > 0 and bar["cvd_15_delta"] > 0:
            bar["cvd_state_side"] = 1
        elif index >= 14 and bar["cvd_5_delta"] < 0 and bar["cvd_15_delta"] < 0:
            bar["cvd_state_side"] = -1
        else:
            bar["cvd_state_side"] = 0
        bar["cvd_divergence_side"] = 0
        profile, total, weighted_ticks = _trade_profile(bar)
        profiles.append(profile)
        active_ticks.append(_active_ticks(bar))
        large_levels.append(_large_levels(bar, len(large_levels)))
        bar["executed_volume"] = total
        bar["bar_vwap_ticks"] = weighted_ticks / total if total else None
        bar["bar_vwap"] = weighted_ticks / total * TICK_SIZE if total else None
        bar.update(_imbalance_1_10(bar))
        bid = _passive_level(bar, "bid")
        ask = _passive_level(bar, "ask")
        bar["passive_bid_level_tick"] = bid["tick"] if bid else None
        bar["passive_ask_level_tick"] = ask["tick"] if ask else None
        bar["passive_bid_touch"] = _touches(bar, bid)
        bar["passive_ask_touch"] = _touches(bar, ask)
        bar["passive_bid_reject"] = bool(
            bar["passive_bid_touch"] and bid and float(bar["close"]) > float(bid["price"])
        ) if _has_ohlc(bar) else False
        bar["passive_ask_reject"] = bool(
            bar["passive_ask_touch"] and ask and float(bar["close"]) < float(ask["price"])
        ) if _has_ohlc(bar) else False
        if bar["passive_bid_reject"] and not bar["passive_ask_reject"]:
            bar["passive_rejection_side"] = 1
        elif bar["passive_ask_reject"] and not bar["passive_bid_reject"]:
            bar["passive_rejection_side"] = -1
        else:
            bar["passive_rejection_side"] = 0

        # This is a responsive, completed-bar divergence marker.  The price
        # extreme is already defined by the preceding PI/MBO enrichment; CVD
        # uses only the current and prior five deltas, so it is safe for a
        # next-bar entry and cannot look through the future.
        if bar.get("rolling_low") and bar["cvd_5_delta"] > 0:
            bar["cvd_divergence_side"] = 1
        elif bar.get("rolling_high") and bar["cvd_5_delta"] < 0:
            bar["cvd_divergence_side"] = -1

    cumulative_volume = 0
    cumulative_ticks = 0
    rolling = Counter()
    previous = Counter()
    for index, bar in enumerate(bars):
        profile = profiles[index]
        rolling.update(profile)
        if index >= PROFILE_WINDOW:
            rolling.subtract(profiles[index - PROFILE_WINDOW])
        if index >= PROFILE_WINDOW:
            previous.update(profiles[index - PROFILE_WINDOW])
        if index >= PROFILE_WINDOW * 2:
            previous.subtract(profiles[index - PROFILE_WINDOW * 2])
        rolling += Counter()
        previous += Counter()

        total = int(bar.get("executed_volume") or 0)
        if total:
            cumulative_volume += total
            cumulative_ticks += float(bar.get("bar_vwap_ticks") or 0.0) * total
        vwap = cumulative_ticks / cumulative_volume * TICK_SIZE if cumulative_volume else None
        bar["vwap"] = vwap
        if not _has_ohlc(bar) or vwap is None:
            bar["vwap_state"] = "unknown"
        elif float(bar["close"]) > vwap + TICK_SIZE:
            bar["vwap_state"] = "above"
        elif float(bar["close"]) < vwap - TICK_SIZE:
            bar["vwap_state"] = "below"
        else:
            bar["vwap_state"] = "at"

        previous_profile = bar.get("previous_profile") or {}
        if not _has_ohlc(bar) or not previous_profile:
            bar["value_location"] = "unknown"
            bar["value_zone"] = "unknown"
        elif float(bar["close"]) > float(previous_profile["vah"]) + TICK_SIZE:
            bar["value_location"] = "above_vah"
            bar["value_zone"] = "above_vah"
        elif float(bar["close"]) < float(previous_profile["val"]) - TICK_SIZE:
            bar["value_location"] = "below_val"
            bar["value_zone"] = "below_val"
        elif float(bar["close"]) >= float(previous_profile["poc"]):
            bar["value_location"] = "inside_value"
            bar["value_zone"] = "between_vah_poc"
        else:
            bar["value_location"] = "inside_value"
            bar["value_zone"] = "between_poc_val"

        current_stats = _profile_stats(rolling) if index + 1 >= PROFILE_WINDOW else None
        previous_stats = _profile_stats(previous) if index + 1 >= PROFILE_WINDOW * 2 else None
        if current_stats and previous_stats:
            delta = current_stats["poc_tick"] - previous_stats["poc_tick"]
            bar["profile_wave_poc_delta_ticks"] = delta
            bar["profile_wave"] = "up" if delta >= 4 else "down" if delta <= -4 else "flat"
            bar["rth_profile_poc"] = current_stats["poc_tick"] * TICK_SIZE
            bar["rth_profile_vah"] = current_stats["vah_tick"] * TICK_SIZE
            bar["rth_profile_val"] = current_stats["val_tick"] * TICK_SIZE
        else:
            bar["profile_wave_poc_delta_ticks"] = 0
            bar["profile_wave"] = "unknown"
            bar["rth_profile_poc"] = None
            bar["rth_profile_vah"] = None
            bar["rth_profile_val"] = None

        persistence_ticks: list[int] = []
        for level in (bar.get("passive_bid_level_tick"), bar.get("passive_ask_level_tick")):
            if level is not None:
                persistence_ticks.append(int(level))
        if persistence_ticks:
            bar["passive_level_persistence"] = max(
                sum(
                    _near_level(active_ticks[prior_index], level)
                    for prior_index in range(max(0, index - PERSISTENCE_WINDOW + 1), index + 1)
                )
                for level in persistence_ticks
            )
        else:
            bar["passive_level_persistence"] = 0

        reversal_events: list[dict[str, Any]] = []
        current_buy, current_sell = large_levels[index]
        start = max(0, index - PERSISTENCE_WINDOW + 1)
        for current_tick in current_sell:
            prior_buy = any(
                any(abs(current_tick - previous_tick) <= LEVEL_TOLERANCE_TICKS for previous_tick in large_levels[j][0])
                for j in range(max(0, index - PERSISTENCE_WINDOW), index)
            )
            persistence = sum(
                _near_level(active_ticks[j], current_tick)
                for j in range(start, index + 1)
            )
            if prior_buy and persistence >= PERSISTENCE_MIN_BARS:
                reversal_events.append({
                    "direction": -1, "level_tick": current_tick,
                    "persistence": persistence, "sequence": "buy150_then_sell150",
                })
        for current_tick in current_buy:
            prior_sell = any(
                any(abs(current_tick - previous_tick) <= LEVEL_TOLERANCE_TICKS for previous_tick in large_levels[j][1])
                for j in range(max(0, index - PERSISTENCE_WINDOW), index)
            )
            persistence = sum(
                _near_level(active_ticks[j], current_tick)
                for j in range(start, index + 1)
            )
            if prior_sell and persistence >= PERSISTENCE_MIN_BARS:
                reversal_events.append({
                    "direction": 1, "level_tick": current_tick,
                    "persistence": persistence, "sequence": "sell150_then_buy150",
                })
        bar["large_reversal_events"] = reversal_events


def _entry_row(
    day: dict[str, Any], index: int, direction: int, family: str, rule: str,
    *, extra: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    bars = day["bars"]
    if index < 0 or index + 1 >= len(bars):
        return None
    bar = bars[index]
    if not _has_ohlc(bar) or bars[index + 1].get("open") is None:
        return None
    row = {
        "family": family, "rule": rule, "filter": "context",
        "date": day["date"], "signal_epoch": int(bar["epoch"]),
        "entry_index": index + 1, "direction": int(direction),
        "pi_message_id": None, "bars": bars,
        **_snapshot(bar),
    }
    if extra:
        row.update(extra)
    return row


def _build_entries(days: list[dict[str, Any]], pi_rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {family: [] for family in ENTRY_FAMILIES}
    by_minute: dict[tuple[str, int], tuple[dict[str, Any], int]] = {}
    for day in days:
        for index, bar in enumerate(day["bars"]):
            if _has_ohlc(bar):
                by_minute[(day["date"], int(bar["epoch"]) // 60)] = (day, index)

    for pi in pi_rows:
        dt = datetime.fromisoformat(str(pi["ts"]))
        located = by_minute.get((dt.date().isoformat(), int(dt.timestamp()) // 60))
        if not located:
            continue
        day, index = located
        row = _entry_row(day, index, int(pi["direction"]), "pi_gated", "pi", extra={
            "pi_message_id": str(pi["message_id"]),
        })
        if row:
            result["pi_gated"].append(row)

    for day in days:
        bars = day["bars"]
        for index in range(1, len(bars) - 1):
            bar = bars[index]
            if not _has_ohlc(bar):
                continue
            events = _candidate_events(bar, bars[max(0, index - 5):index])
            for rule, direction in events:
                if rule not in {"bull_pressure_flip", "bear_pressure_flip"}:
                    continue
                row = _entry_row(day, index, direction, "mbo_turnover", rule)
                if row:
                    result["mbo_turnover"].append(row)
            for event in bar.get("large_reversal_events") or []:
                row = _entry_row(
                    day, index, int(event["direction"]), "large_150_reversal", event["sequence"],
                    extra={
                        "reversal_level_tick": int(event["level_tick"]),
                        "reversal_persistence": int(event["persistence"]),
                    },
                )
                if row:
                    result["large_150_reversal"].append(row)
            if bar.get("passive_bid_reject"):
                row = _entry_row(day, index, 1, "passive_touch_reject", "passive_bid_no_breakout")
                if row:
                    result["passive_touch_reject"].append(row)
            if bar.get("passive_ask_reject"):
                row = _entry_row(day, index, -1, "passive_touch_reject", "passive_ask_no_breakout")
                if row:
                    result["passive_touch_reject"].append(row)
    return dict(result)


def _prepare_days() -> list[dict[str, Any]]:
    days = _load_days(market_data.derived_path("orderflow", "mnq"))
    previous_profile: dict[str, float] = {}
    previous_date: datetime | None = None
    for day in days:
        current_date = datetime.fromisoformat(day["date"])
        profile_for_day = previous_profile
        if previous_date is None or (current_date - previous_date).days > 3:
            profile_for_day = {}
        _enrich_day(day, profile_for_day)
        _enrich_context_day(day)
        previous_profile = _profile(day["bars"])
        previous_date = current_date
    return days


def _split_metrics(trades: list[dict[str, Any]], days: list[dict[str, Any]]) -> dict[str, Any]:
    discovery_days = {day["date"] for day in days if day["date"] <= DISCOVERY_END}
    evaluation_days = {day["date"] for day in days if day["date"] > DISCOVERY_END}
    return {
        "all": _metrics(trades, len(days)),
        "discovery": _metrics([row for row in trades if row["date"] <= DISCOVERY_END], len(discovery_days)),
        "evaluation": _metrics([row for row in trades if row["date"] > DISCOVERY_END], len(evaluation_days)),
    }


def run() -> dict[str, Any]:
    days = _prepare_days()
    pi_rows = _load_pi(market_data.runtime_path("logs", "pi_live_signals.jsonl"))
    if not days:
        return {"status": "no_cache", "results": {}}
    entries = _build_entries(days, pi_rows)
    atr_history = _atr_history(days)
    results: dict[str, Any] = {}
    all_trades: list[dict[str, Any]] = []
    for family, rows in sorted(entries.items()):
        result = {
            "family": family,
            "input_candidates": len(rows),
            "contexts": {},
        }
        for context in CONTEXTS:
            selected = [row for row in rows if _context_matches(context, row, int(row["direction"]))]
            summary, trades = _run_variant(
                selected, FIXED_EXIT_POLICY, atr_history,
                len(days),
            )
            split = _split_metrics(trades, days)
            result["contexts"][context] = {
                "input_candidates": len(selected),
                **split,
            }
            for trade in trades:
                trade["context"] = context
            all_trades.extend(trades)
        results[family] = result

    environment_counts: dict[str, dict[str, int]] = {}
    for field in (
        "vwap_state", "value_location", "value_zone", "profile_wave",
        "imbalance_1_10_side", "cvd_state_side", "cvd_divergence_side",
    ):
        counts = Counter(
            str(bar.get(field)) for day in days for bar in day["bars"]
            if _has_ohlc(bar)
        )
        environment_counts[field] = dict(sorted(counts.items()))
    return {
        "status": "provisional_research_only",
        "generated_at": datetime.now(UTC).isoformat(),
        "coverage": {
            "rth_days": len(days),
            "cache_dates": [day["date"] for day in days],
            "cache_schema_versions": dict(Counter(day["schema_version"] for day in days)),
            "pi_unique": len(pi_rows),
            "entry_families": {family: len(rows) for family, rows in sorted(entries.items())},
            "discovery_end": DISCOVERY_END,
        },
        "definitions": {
            "vwap": "RTH cumulative executed MBO volume-weighted price, evaluated after the completed signal bar",
            "value_location": "current close versus previous completed RTH VAH/VAL/POC",
            "profile_wave": "30-minute RTH volume-profile POC migration versus the preceding 30 minutes; +/-4 ticks defines up/down",
            "imbalance_1_10": "diagonal aggressive footprint ratio >=10:1 with at least 10 contracts on the dominant side",
            "rth_cvd": "RTH cumulative aggressive delta: cumulative buy quantity minus sell quantity, reset at each RTH day",
            "cvd_aligned": "completed-bar CVD 5-minute and 15-minute deltas are both positive for long or both negative for short; 15 bars of warm-up required",
            "cvd_divergence": "near-term rolling price low with positive five-minute CVD delta for long, or rolling price high with negative five-minute CVD delta for short",
            "passive_level": "price cell with maximum resting depth plus passive fill/refill activity; touch means completed OHLC range reaches within one tick",
            "passive_no_breakout": "passive bid touch closes above the bid level for long, or passive ask touch closes below the ask level for short",
            "large_reversal": "150+ buy then 150+ sell, or the symmetric sequence, within 10 minutes and the level is active in at least 6 of those minutes within 2 ticks",
            "entry": "next one-minute bar open; one position at a time; 10-minute per-direction cooldown",
            "exit": "fixed PI ATR blend: long 4x ATR stop / 12x ATR target; short 1.5x ATR stop / 4.5x ATR target; completed 5-minute ATR14/ATR50 blend",
            "cost": "canonical MNQ commission plus fees per round turn",
        },
        "environment_counts": environment_counts,
        "results": results,
        "notes": [
            "The 2026-08-07 MBO day is included for independent MBO/context testing; no PI audit row exists for that date.",
            "A high PF with very few evaluation trades is not a promotion decision.",
            "MBO order IDs are anonymous; size bands are executed-volume features, not institution identity.",
        ],
    }


def _fmt(value: Any) -> str:
    if value == "inf":
        return "inf"
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _markdown(result: dict[str, Any]) -> str:
    lines = [
        "# MNQ order-flow context combination study",
        "",
        f"Status: **{result['status']}**",
        f"Generated: `{result['generated_at']}`",
        "",
        f"RTH days: **{result['coverage']['rth_days']}**; PI rows: **{result['coverage']['pi_unique']}**; "
        f"discovery ends: **{result['coverage']['discovery_end']}**",
        "",
        "All entries use the next one-minute open and the same fixed PI ATR-blend exit. "
        "Evaluation rows are the only rows used for a new candidate decision.",
        "",
        "## Evaluation results",
        "",
        "| Family | Context | N | Net P&L | Max DD (abs) | PF | Win | Pass |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    eval_rows: list[tuple[float, str, str, dict[str, Any]]] = []
    for family, family_result in sorted(result["results"].items()):
        for context, context_result in sorted(family_result["contexts"].items()):
            metrics = context_result["evaluation"]
            eval_rows.append((float(metrics.get("net_pnl") or 0), family, context, metrics))
            lines.append(
                f"| {family} | {context} | {metrics.get('trades', 0)} | ${_fmt(metrics.get('net_pnl'))} | "
                f"${_fmt(metrics.get('max_drawdown_abs'))} | {_fmt(metrics.get('pf'))} | "
                f"{_fmt(metrics.get('win_rate'))} | {'YES' if metrics.get('qualified') else 'NO'} |"
            )
    lines.extend([
        "",
        "## Evaluation candidates passing net > 0 and PF > 1",
        "",
        "| Family | Context | N | Net P&L | Max DD (abs) | PF |",
        "|---|---|---:|---:|---:|---:|",
    ])
    passing = []
    for _, family, context, metrics in sorted(eval_rows, reverse=True):
        if metrics.get("qualified"):
            passing.append((family, context, metrics))
            lines.append(
                f"| {family} | {context} | {metrics.get('trades', 0)} | ${_fmt(metrics.get('net_pnl'))} | "
                f"${_fmt(metrics.get('max_drawdown_abs'))} | {_fmt(metrics.get('pf'))} |"
            )
    if not passing:
        lines.append("| none | - | 0 | $0 | $0 | 0 |")
    lines.extend([
        "",
        "## Definitions",
        "",
    ])
    for key, value in result["definitions"].items():
        lines.append(f"- **{key}**: {value}")
    lines.extend([
        "",
        "## Interpretation limits",
        "",
        "The study separates discovery dates from evaluation dates but does not fit thresholds. "
        "The 2026-08-07 MBO day has no PI audit row, so it contributes only to independent MBO/context coverage. "
        "A result with fewer than 10 evaluation trades is a lead for more data, not a validated strategy.",
        "",
        "Sources: [Databento MBO](https://databento.com/docs/schemas-and-data-formats/mbo), "
        "[CME MBO FAQ](https://www.cmegroup.com/articles/faqs/market-by-order-mbo.html), "
        "[Jesse Rogers CZT](https://jesserogerstrading.com/czt/what-is-condition-zone-trigger), "
        "[Jesse Rogers order flow](https://jesserogerstrading.com/order-flow).",
        "",
    ])
    return "\n".join(lines)


def _write_outputs(result: dict[str, Any]) -> tuple[Path, Path, Path]:
    root = market_data.derived_path("research")
    root.mkdir(parents=True, exist_ok=True)
    json_path = root / "orderflow_context_combination_study.json"
    md_path = root / "orderflow_context_combination_study.md"
    csv_path = root / "orderflow_context_combination_study_trades.csv.gz"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_markdown(result), encoding="utf-8")
    rows: list[dict[str, Any]] = []
    days = _prepare_days()
    pi_rows = _load_pi(market_data.runtime_path("logs", "pi_live_signals.jsonl"))
    entries = _build_entries(days, pi_rows)
    atr_history = _atr_history(days)
    for family, family_rows in entries.items():
        for context in CONTEXTS:
            selected = [row for row in family_rows if _context_matches(context, row, int(row["direction"]))]
            _, trades = _run_variant(selected, FIXED_EXIT_POLICY, atr_history, len(days))
            for trade in trades:
                rows.append({"family": family, "context": context, **trade})
    fields = sorted({key for row in rows for key in row})
    with gzip.open(csv_path, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return json_path, md_path, csv_path


def main() -> int:
    result = run()
    json_path, md_path, csv_path = _write_outputs(result)
    print(md_path)
    print(json.dumps({
        "rth_days": result.get("coverage", {}).get("rth_days"),
        "pi_unique": result.get("coverage", {}).get("pi_unique"),
        "entry_families": result.get("coverage", {}).get("entry_families"),
        "qualified_evaluation_rows": sum(
            metrics.get("qualified", False)
            for family in result.get("results", {}).values()
            for context in family.get("contexts", {}).values()
            for metrics in [context.get("evaluation", {})]
        ),
        "json": str(json_path), "csv": str(csv_path),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
