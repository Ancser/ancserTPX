"""Relate MNQ MBO-derived order flow to PI marks and RTH auction levels.

This is an offline research script.  It never changes presets or live behavior.
All candidate events use information available at the event minute.  Full-day
high/low proximity is reported separately as diagnostic hindsight.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import random
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.data import market_data
from backend.data.pi_history import load_rows
from backend.live.pi_listener import DIRECTION, SYMBOL_MAP


UTC = timezone.utc
TICK_SIZE = 0.25
PI_LEAD_MINUTES = 5
PROFILE_PCT = 0.70
DISCOVERY_END = "2026-08-14"
SIGNAL_COOLDOWN_MINUTES = 10


def _median(values: Iterable[float], default: float = 0.0) -> float:
    rows = [float(value) for value in values if math.isfinite(float(value))]
    return statistics.median(rows) if rows else default


def _safe_ratio(numerator: float, denominator: float) -> float:
    return float(numerator) / float(denominator) if denominator else 0.0


def _has_ohlc(bar: dict[str, Any]) -> bool:
    return all(bar.get(field) is not None for field in ("open", "high", "low", "close"))


def _load_days(root: Path) -> list[dict[str, Any]]:
    days: list[dict[str, Any]] = []
    for path in sorted(root.glob("footprint_mnq_*.json.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        bars = payload.get("bars") or []
        if not bars:
            continue
        days.append({
            "date": path.name[len("footprint_mnq_"):len("footprint_mnq_") + 10],
            "path": str(path),
            "schema_version": int((payload.get("meta") or {}).get("schema_version") or 0),
            "bars": bars,
        })
    return days


def _load_pi(path: Path | None = None) -> list[dict[str, Any]]:
    """Load the same filtered PI history used by backtest/live research.

    The old version read the append-only runtime audit log.  That log is an
    execution trace, not the canonical Discord history, so a fresh channel
    backfill could be silently omitted from this study.  Keep ``path`` in the
    signature for callers that still pass it, but use ``load_rows()`` as the
    single source of truth and flatten its one-mark messages for this script.
    """
    unique: dict[str, dict[str, Any]] = {}
    for source_row in load_rows():
        symbol = str(source_row.get("symbol") or "").upper()
        if SYMBOL_MAP.get(symbol) != "MNQ":
            continue
        message_id = source_row.get("id")
        if not message_id:
            continue
        for mark in source_row.get("marks") or []:
            kind = mark.get("kind")
            if kind not in DIRECTION:
                continue
            unique[str(message_id)] = {
                "message_id": str(message_id),
                "ts": source_row["ts"],
                "future": "MNQ",
                "equity": symbol,
                "direction": int(DIRECTION[kind]),
                "side": "long" if int(DIRECTION[kind]) > 0 else "short",
                "kind": kind,
                "size": mark.get("size") or "?",
                "pos": mark.get("pos"),
            }
    return sorted(unique.values(), key=lambda row: row["ts"])


def _profile(bars: list[dict[str, Any]]) -> dict[str, float]:
    volume: Counter[int] = Counter()
    for bar in bars:
        for cell in bar.get("cells") or []:
            if len(cell) >= 3:
                volume[int(cell[0])] += int(cell[1] or 0) + int(cell[2] or 0)
    if not volume:
        return {}
    poc_tick = max(volume, key=lambda tick: (volume[tick], -tick))
    target = sum(volume.values()) * PROFILE_PCT
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
        "poc": poc_tick * TICK_SIZE,
        "vah": high * TICK_SIZE,
        "val": low * TICK_SIZE,
    }


def _cell_features(bar: dict[str, Any]) -> dict[str, float]:
    cells = {int(row[0]): row for row in bar.get("cells") or [] if len(row) >= 7}
    if not cells or not _has_ohlc(bar):
        return {
            "buy_stack": 0, "sell_stack": 0, "bid_depth": 0,
            "ask_depth": 0, "bid_absorption": 0, "ask_absorption": 0,
            "buy_50_99_qty": 0, "buy_100_149_qty": 0, "buy_150_plus_qty": 0,
            "sell_50_99_qty": 0, "sell_100_149_qty": 0, "sell_150_plus_qty": 0,
        }
    low_tick = round(float(bar["low"]) / TICK_SIZE)
    high_tick = round(float(bar["high"]) / TICK_SIZE)
    buy_stack = sell_stack = 0
    bid_depth = ask_depth = 0
    bid_fill_low = ask_fill_high = 0
    band_totals = {
        "buy_50_99_qty": 0, "buy_100_149_qty": 0, "buy_150_plus_qty": 0,
        "sell_50_99_qty": 0, "sell_100_149_qty": 0, "sell_150_plus_qty": 0,
    }
    for tick, row in cells.items():
        buy = int(row[1] or 0)
        sell = int(row[2] or 0)
        bid_depth += int(row[5] or 0)
        ask_depth += int(row[6] or 0)
        if len(row) >= 15:
            for index, name in zip(
                range(9, 15), band_totals,
            ):
                band_totals[name] += int(row[index] or 0)
        if tick <= low_tick + 3:
            bid_fill_low += int(row[3] or 0)
        if tick >= high_tick - 3:
            ask_fill_high += int(row[4] or 0)
        diagonal_sell = int(cells.get(tick - 1, [0, 0, 0])[2] or 0)
        diagonal_buy = int(cells.get(tick + 1, [0, 0])[1] or 0)
        if buy >= 10 and buy >= 3 * max(1, diagonal_sell):
            buy_stack += 1
        if sell >= 10 and sell >= 3 * max(1, diagonal_buy):
            sell_stack += 1
    return {
        "buy_stack": buy_stack,
        "sell_stack": sell_stack,
        "bid_depth": bid_depth,
        "ask_depth": ask_depth,
        "bid_absorption": _safe_ratio(bid_fill_low, int(bar.get("sell") or 0)),
        "ask_absorption": _safe_ratio(ask_fill_high, int(bar.get("buy") or 0)),
        **band_totals,
    }


def _enrich_day(day: dict[str, Any], previous_profile: dict[str, float]) -> None:
    bars = day["bars"]
    ohlc_bars = [bar for bar in bars if _has_ohlc(bar)]
    day_high = max(float(bar["high"]) for bar in ohlc_bars)
    day_low = min(float(bar["low"]) for bar in ohlc_bars)
    for index, bar in enumerate(bars):
        bar["dt"] = datetime.fromtimestamp(int(bar["epoch"]), UTC)
        bar["date"] = day["date"]
        bar["depth_valid"] = day["schema_version"] >= 4
        bar["ohlc_valid"] = _has_ohlc(bar)
        bar["volume"] = int(bar.get("buy") or 0) + int(bar.get("sell") or 0)
        bar["delta"] = int(bar.get("buy") or 0) - int(bar.get("sell") or 0)
        prior30 = bars[max(0, index - 30):index]
        bar["volume_ratio"] = _safe_ratio(
            bar["volume"], _median((prior.get("volume", 0) for prior in prior30), 1.0),
        )
        bar["delta_ratio"] = _safe_ratio(
            abs(bar["delta"]),
            _median((abs(prior.get("delta", 0)) for prior in prior30), 1.0),
        )
        bar["ofi_ratio"] = _safe_ratio(
            abs(int(bar.get("ofi") or 0)),
            _median((abs(int(prior.get("ofi") or 0)) for prior in prior30), 1.0),
        )
        bar["bid_refill_ratio"] = _safe_ratio(
            int(bar.get("bid_refill_qty") or 0),
            _median((int(prior.get("bid_refill_qty") or 0) for prior in prior30), 1.0),
        )
        bar["ask_refill_ratio"] = _safe_ratio(
            int(bar.get("ask_refill_qty") or 0),
            _median((int(prior.get("ask_refill_qty") or 0) for prior in prior30), 1.0),
        )
        bar["cvd5"] = sum(int(x.get("delta") or 0) for x in bars[max(0, index - 4):index + 1])
        if not bar["ohlc_valid"]:
            # Databento can continue sending depth/cancel events after the
            # last trade.  Keep the timestamp and book fields, but do not
            # invent OHLC values or let these book-only bars enter the replay.
            bar["close_location"] = 0.0
            bar["rolling_high"] = False
            bar["rolling_low"] = False
            bar.update(_cell_features(bar))
            depth_total = bar["bid_depth"] + bar["ask_depth"]
            bar["depth_imbalance"] = _safe_ratio(
                bar["bid_depth"] - bar["ask_depth"], depth_total,
            )
            close_depth_total = int(bar.get("close_bid_depth") or 0) + int(bar.get("close_ask_depth") or 0)
            bar["queue_imbalance"] = _safe_ratio(
                int(bar.get("close_bid_depth") or 0) - int(bar.get("close_ask_depth") or 0),
                close_depth_total,
            )
            bar["near_day_high_hindsight"] = False
            bar["near_day_low_hindsight"] = False
            bar["day_high_hindsight"] = day_high
            bar["day_low_hindsight"] = day_low
            bar["previous_profile"] = previous_profile
            continue
        spread = max(TICK_SIZE, float(bar["high"]) - float(bar["low"]))
        bar["close_location"] = (float(bar["close"]) - float(bar["low"])) / spread
        prior15 = [row for row in bars[max(0, index - 14):index + 1] if _has_ohlc(row)]
        bar["rolling_high"] = float(bar["high"]) >= max(float(x["high"]) for x in prior15)
        bar["rolling_low"] = float(bar["low"]) <= min(float(x["low"]) for x in prior15)
        bar.update(_cell_features(bar))
        depth_total = bar["bid_depth"] + bar["ask_depth"]
        bar["depth_imbalance"] = _safe_ratio(
            bar["bid_depth"] - bar["ask_depth"], depth_total,
        )
        close_depth_total = int(bar.get("close_bid_depth") or 0) + int(bar.get("close_ask_depth") or 0)
        bar["queue_imbalance"] = _safe_ratio(
            int(bar.get("close_bid_depth") or 0) - int(bar.get("close_ask_depth") or 0),
            close_depth_total,
        )
        bar["near_day_high_hindsight"] = day_high - float(bar["high"]) <= 8.0
        bar["near_day_low_hindsight"] = float(bar["low"]) - day_low <= 8.0
        bar["day_high_hindsight"] = day_high
        bar["day_low_hindsight"] = day_low
        bar["previous_profile"] = previous_profile


def _candidate_events(
    bar: dict[str, Any], prior: list[dict[str, Any]],
) -> list[tuple[str, int]]:
    if not bar.get("ohlc_valid", _has_ohlc(bar)):
        return []
    events: list[tuple[str, int]] = []
    buy = int(bar.get("buy") or 0)
    sell = int(bar.get("sell") or 0)
    total = max(1, buy + sell)
    close_location = float(bar["close_location"])
    volume_ratio = float(bar["volume_ratio"])
    profile = bar.get("previous_profile") or {}

    # Exploratory "turnover bubbles": a large one-sided auction reaches a
    # rolling extreme.  The immediate impulse is responsive but noisy; the
    # separate flip rule waits for opposing aggression and price rejection.
    bull_pressure = (
        bar["rolling_low"] and volume_ratio >= 1.40
        and bar["sell_stack"] >= 7 and bar["delta"] < 0
    )
    bear_pressure = (
        bar["rolling_high"] and volume_ratio >= 1.20
        and bar["buy_stack"] >= 7 and bar["delta"] > 0
    )
    if bull_pressure:
        events.append(("bull_sell_pressure_extreme", 1))
    if bear_pressure:
        events.append(("bear_buy_pressure_extreme", -1))
    prior_bull_pressure = any(
        row["rolling_low"] and row["volume_ratio"] >= 1.40
        and row["sell_stack"] >= 7 and row["delta"] < 0
        for row in prior[-5:]
    )
    prior_bear_pressure = any(
        row["rolling_high"] and row["volume_ratio"] >= 1.20
        and row["buy_stack"] >= 7 and row["delta"] > 0
        for row in prior[-5:]
    )
    if prior_bull_pressure and bar["delta"] > 0 and close_location >= 0.55:
        events.append(("bull_pressure_flip", 1))
    if prior_bear_pressure and bar["delta"] < 0 and close_location <= 0.45:
        events.append(("bear_pressure_flip", -1))

    if (bar["rolling_low"] and volume_ratio >= 1.35 and sell / total >= 0.53
            and bar["bid_absorption"] >= 0.25 and close_location >= 0.60):
        events.append(("bull_absorption", 1))
    if (bar["rolling_high"] and volume_ratio >= 1.35 and buy / total >= 0.53
            and bar["ask_absorption"] >= 0.25 and close_location <= 0.40):
        events.append(("bear_absorption", -1))
    if (bar["rolling_low"] and close_location >= 0.55 and bar["cvd5"] > 0
            and bar["sell_stack"] <= 1):
        events.append(("bull_cvd_divergence", 1))
    if (bar["rolling_high"] and close_location <= 0.45 and bar["cvd5"] < 0
            and bar["buy_stack"] <= 1):
        events.append(("bear_cvd_divergence", -1))
    if (bar["depth_valid"] and bar["rolling_low"] and bar["queue_imbalance"] >= 0.20
            and bar["delta"] > 0 and close_location >= 0.55):
        events.append(("bull_book_flip", 1))
    if (bar["depth_valid"] and bar["rolling_high"] and bar["queue_imbalance"] <= -0.20
            and bar["delta"] < 0 and close_location <= 0.45):
        events.append(("bear_book_flip", -1))
    if (bar["depth_valid"] and bar["rolling_low"] and volume_ratio >= 1.20
            and sell / total >= 0.52 and bar["bid_refill_ratio"] >= 1.50
            and close_location >= 0.55):
        events.append(("bull_passive_refill", 1))
    if (bar["depth_valid"] and bar["rolling_high"] and volume_ratio >= 1.20
            and buy / total >= 0.52 and bar["ask_refill_ratio"] >= 1.50
            and close_location <= 0.45):
        events.append(("bear_passive_refill", -1))
    if (bar["depth_valid"] and prior_bull_pressure and int(bar.get("ofi") or 0) > 0
            and bar["ofi_ratio"] >= 1.20 and close_location >= 0.55):
        events.append(("bull_ofi_turnover", 1))
    if (bar["depth_valid"] and prior_bear_pressure and int(bar.get("ofi") or 0) < 0
            and bar["ofi_ratio"] >= 1.20 and close_location <= 0.45):
        events.append(("bear_ofi_turnover", -1))

    if profile:
        val = profile["val"]
        vah = profile["vah"]
        if float(bar["low"]) <= val < float(bar["close"]) and bar["delta"] > 0:
            events.append(("bull_val_reclaim", 1))
        if float(bar["high"]) >= vah > float(bar["close"]) and bar["delta"] < 0:
            events.append(("bear_vah_reject", -1))
        if (float(bar["close"]) > vah and bar["rolling_high"] and bar["delta"] > 0
                and volume_ratio >= 1.20 and close_location >= 0.60):
            events.append(("bull_vah_accept", 1))
        if (float(bar["close"]) < val and bar["rolling_low"] and bar["delta"] < 0
                and volume_ratio >= 1.20 and close_location <= 0.40):
            events.append(("bear_val_accept", -1))
    return events


def _forward_metrics(bars: list[dict[str, Any]], index: int, direction: int) -> dict[str, float]:
    entry = float(bars[index]["close"])
    result: dict[str, float] = {}
    for minutes in (5, 15, 30, 60):
        future: list[dict[str, Any]] = []
        for row in bars[index + 1:min(len(bars), index + minutes + 1)]:
            if not _has_ohlc(row):
                break
            future.append(row)
        if not future:
            continue
        last = float(future[-1]["close"])
        result[f"return_{minutes}m"] = direction * (last - entry)
        if direction > 0:
            result[f"mfe_{minutes}m"] = max(float(row["high"]) - entry for row in future)
            result[f"mae_{minutes}m"] = min(float(row["low"]) - entry for row in future)
        else:
            result[f"mfe_{minutes}m"] = max(entry - float(row["low"]) for row in future)
            result[f"mae_{minutes}m"] = min(entry - float(row["high"]) for row in future)
    return result


def _precursor_features(
    bars: list[dict[str, Any]], index: int, direction: int,
) -> dict[str, float]:
    """Summarize only the five complete MBO minutes before a PI timestamp."""
    window = bars[max(0, index - PI_LEAD_MINUTES):index]

    if not window:
        return {
            "opposing_attack_delta": 0.0,
            "same_side_turnover_delta": 0.0,
            "volume_burst": 0.0,
            "opposing_large_trade": 0.0,
        }

    def signed_delta_ratio(row: dict[str, Any]) -> float:
        return math.copysign(float(row.get("delta_ratio") or 0.0), float(row.get("delta") or 0.0))

    result = {
        # A long PI is commonly preceded by sell aggression, and vice versa.
        "opposing_attack_delta": max(
            -direction * signed_delta_ratio(row) for row in window
        ),
        "same_side_turnover_delta": max(
            direction * signed_delta_ratio(row) for row in window
        ),
        "volume_burst": max(float(row.get("volume_ratio") or 0.0) for row in window),
        "opposing_large_trade": float(max(
            int(row.get("max_sell_trade") or 0) if direction > 0
            else int(row.get("max_buy_trade") or 0)
            for row in window
        )),
    }
    if all(bool(row.get("depth_valid")) for row in window):
        result.update({
            "passive_defense_refill": max(
                float(row.get("bid_refill_ratio") or 0.0) if direction > 0
                else float(row.get("ask_refill_ratio") or 0.0)
                for row in window
            ),
            "directional_ofi": max(
                direction * math.copysign(
                    float(row.get("ofi_ratio") or 0.0), float(row.get("ofi") or 0.0),
                )
                for row in window
            ),
            "directional_queue": max(
                direction * float(row.get("queue_imbalance") or 0.0) for row in window
            ),
        })
    return result


def _precursor_feature_permutation(
    observations: list[tuple[list[dict[str, Any]], int, int]],
    *, trials: int = 1000,
) -> dict[str, dict[str, float]]:
    if not observations:
        return {}
    observed_rows = [_precursor_features(*row) for row in observations]
    feature_names = sorted(set.intersection(*(set(row) for row in observed_rows)))
    day_pool = list({id(bars): bars for bars, _, _ in observations}.values())
    rng = random.Random(20260908)
    null_same_day: defaultdict[str, list[float]] = defaultdict(list)
    null_same_clock: defaultdict[str, list[float]] = defaultdict(list)
    for _ in range(trials):
        sampled_day = []
        sampled_clock = []
        for bars, index, direction in observations:
            # Keep controls inside the same RTH date and leave enough history
            # for the six-minute precursor window.
            random_index = rng.randrange(min(PI_LEAD_MINUTES, len(bars) - 1), len(bars))
            sampled_day.append(_precursor_features(bars, random_index, direction))
            clock_bars = rng.choice(day_pool)
            clock_index = min(index, len(clock_bars) - 1)
            sampled_clock.append(_precursor_features(clock_bars, clock_index, direction))
        for name in feature_names:
            null_same_day[name].append(_median(row[name] for row in sampled_day))
            null_same_clock[name].append(_median(row[name] for row in sampled_clock))
    result: dict[str, dict[str, float]] = {}
    for name in feature_names:
        observed = _median(row[name] for row in observed_rows)
        day_values = null_same_day[name]
        clock_values = null_same_clock[name]
        result[name] = {
            "observed_median": round(observed, 4),
            "same_day_random_median_mean": round(statistics.mean(day_values), 4),
            "p_same_day_random_ge_observed": round(
                (1 + sum(value >= observed for value in day_values)) / (trials + 1), 4,
            ),
            "same_clock_random_median_mean": round(statistics.mean(clock_values), 4),
            "p_same_clock_random_ge_observed": round(
                (1 + sum(value >= observed for value in clock_values)) / (trials + 1), 4,
            ),
        }
    return result


def _summarize(rows: list[dict[str, Any]], day_count: int) -> dict[str, Any]:
    if not rows:
        return {"events": 0, "events_per_day": 0.0}
    result: dict[str, Any] = {
        "events": len(rows),
        "events_per_day": round(len(rows) / max(1, day_count), 2),
        "aligned_extreme_events_within_8_points_hindsight": sum(
            float(row.get("distance_to_aligned_day_extreme_hindsight", math.inf)) <= 8.0
            for row in rows
        ),
        "aligned_extreme_days_within_8_points_hindsight": len({
            row["date"] for row in rows
            if float(row.get("distance_to_aligned_day_extreme_hindsight", math.inf)) <= 8.0
        }),
        "previous_profile_events_within_20_points": sum(
            row.get("distance_to_previous_profile") is not None
            and float(row["distance_to_previous_profile"]) <= 20.0
            for row in rows
        ),
    }
    for key in ("return_5m", "return_15m", "return_30m", "return_60m", "mfe_15m", "mae_15m"):
        values = [float(row[key]) for row in rows if key in row]
        if values:
            result[f"median_{key}"] = round(_median(values), 3)
            result[f"win_{key}"] = round(sum(value > 0 for value in values) / len(values), 3)
    matched_ids = {
        str(match["message_id"])
        for row in rows for match in row.get("pi_match") or []
    }
    result["pi_matches"] = len(matched_ids)
    result["pi_match_lags"] = dict(Counter(
        int(match["lag_min"])
        for row in rows for match in row.get("pi_match") or []
    ))
    return result


def _summarize_pi_groups(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Describe the live-safe order-flow state by Discord Level.

    ``size``/Level is a source annotation, not a validated numeric strength
    field.  This summary is therefore descriptive: it reports sample size,
    kind mix, event-time features and forward outcomes, without turning a
    small group into a trading rule.
    """
    result: dict[str, Any] = {}
    fields = (
        "opposing_attack_delta", "same_side_turnover_delta", "volume_burst",
        "opposing_large_trade", "passive_defense_refill", "directional_ofi",
        "directional_queue",
    )

    def summarize(group: list[dict[str, Any]]) -> dict[str, Any]:
        returns15 = [float(row["return_15m"]) for row in group if "return_15m" in row]
        returns60 = [float(row["return_60m"]) for row in group if "return_60m" in row]
        precursor = {}
        for name in fields:
            values = [
                float((row.get("precursor_features") or {}).get(name))
                for row in group
                if (row.get("precursor_features") or {}).get(name) is not None
            ]
            precursor[name] = round(_median(values), 4)
        return {
            "signals": len(group),
            "kinds": dict(Counter(str(row.get("kind") or "unknown") for row in group)),
            "matched_by_candidate": sum(bool(row.get("matched_candidates")) for row in group),
            "median_return_15m": round(_median(returns15), 3),
            "win_rate_15m": round(_safe_ratio(sum(value > 0 for value in returns15), len(returns15)), 3),
            "median_return_60m": round(_median(returns60), 3),
            "win_rate_60m": round(_safe_ratio(sum(value > 0 for value in returns60), len(returns60)), 3),
            "precursor_medians": precursor,
        }

    for level in sorted({str(row.get("size") or "unknown") for row in rows}):
        group = [row for row in rows if str(row.get("size") or "unknown") == level]
        result[level] = summarize(group)
        result[level]["directions"] = dict(Counter(
            "long" if int(row["direction"]) > 0 else "short" for row in group
        ))
        result[level]["by_direction"] = {
            side: summarize([
                row for row in group
                if ("long" if int(row["direction"]) > 0 else "short") == side
            ])
            for side in ("long", "short")
            if any(("long" if int(row["direction"]) > 0 else "short") == side for row in group)
        }
    return result


def _apply_cooldown(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn minute conditions into alerts rather than repeated indicator paint."""
    accepted: list[dict[str, Any]] = []
    last_epoch: dict[tuple[str, str], int] = {}
    for row in sorted(rows, key=lambda value: (value["time"], value["name"])):
        epoch = int(datetime.fromisoformat(row["time"]).timestamp())
        key = (row["date"], row["name"])
        if epoch - last_epoch.get(key, -10**12) < SIGNAL_COOLDOWN_MINUTES * 60:
            continue
        accepted.append(row)
        last_epoch[key] = epoch
    return accepted


def _pi_overlap_permutation(
    candidates: list[dict[str, Any]],
    pi_rows: list[dict[str, Any]],
    names: set[str] | None,
    *,
    trials: int = 1000,
) -> dict[str, Any]:
    event_minutes: defaultdict[tuple[str, int], set[int]] = defaultdict(set)
    for row in candidates:
        if names is not None and row["name"] not in names:
            continue
        event_minutes[(row["date"], int(row["direction"]))].add(
            int(datetime.fromisoformat(row["time"]).timestamp()) // 60
        )

    def matched(date_key: str, direction: int, minute: int) -> bool:
        events = event_minutes[(date_key, direction)]
        return any(minute - lag in events for lag in range(PI_LEAD_MINUTES + 1))

    observations = []
    for row in pi_rows:
        dt = datetime.fromisoformat(row["time"])
        key = (dt.date().isoformat(), int(row["direction"]))
        observations.append((key[0], key[1], int(dt.timestamp()) // 60))
    observed = sum(matched(*row) for row in observations)
    if not observations:
        return {"signals": 0, "observed": 0, "null_mean": 0.0, "p_ge_observed": 1.0}

    rng = random.Random(20260908)
    null: list[int] = []
    for _ in range(trials):
        count = 0
        for date_key, direction, minute in observations:
            day_start = (minute // 1440) * 1440
            random_minute = day_start + 13 * 60 + 30 + rng.randrange(390)
            count += matched(date_key, direction, random_minute)
        null.append(count)
    ordered = sorted(null)
    return {
        "signals": len(observations),
        "observed": observed,
        "null_mean": round(statistics.mean(null), 3),
        "null_p95": ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))],
        "p_ge_observed": round((1 + sum(value >= observed for value in null)) / (trials + 1), 4),
    }


def run() -> dict[str, Any]:
    cache_root = market_data.derived_path("orderflow", "mnq")
    audit_path = market_data.runtime_path("logs", "pi_live_signals.jsonl")
    days = _load_days(cache_root)
    pi_rows = _load_pi(audit_path)
    pi_by_minute: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in pi_rows:
        epoch_minute = int(datetime.fromisoformat(row["ts"]).timestamp()) // 60
        pi_by_minute[epoch_minute].append(row)

    previous_profile: dict[str, float] = {}
    previous_date: datetime | None = None
    candidates: list[dict[str, Any]] = []
    pi_diagnostics: list[dict[str, Any]] = []
    pi_feature_observations: list[tuple[list[dict[str, Any]], int, int]] = []
    covered_pi: set[str] = set()
    available_minutes: dict[int, tuple[list[dict[str, Any]], int]] = {}
    for day in days:
        current_date = datetime.fromisoformat(day["date"])
        profile_for_day = previous_profile
        if previous_date is None or (current_date - previous_date).days > 3:
            profile_for_day = {}
        _enrich_day(day, profile_for_day)
        bars = day["bars"]
        for index, bar in enumerate(bars):
            if not _has_ohlc(bar):
                continue
            available_minutes[int(bar["epoch"]) // 60] = (bars, index)
            for name, direction in _candidate_events(bar, bars[max(0, index - 5):index]):
                profile = bar.get("previous_profile") or {}
                distance_to_previous_profile = (
                    min(abs(float(bar["close"]) - float(level)) for level in profile.values())
                    if profile else None
                )
                distance_to_aligned_extreme = (
                    float(bar["low"]) - float(bar["day_low_hindsight"])
                    if direction > 0 else
                    float(bar["day_high_hindsight"]) - float(bar["high"])
                )
                row = {
                    "name": name,
                    "direction": direction,
                    "date": day["date"],
                    "time": bar["dt"].isoformat(),
                    "price": float(bar["close"]),
                    "near_day_high_hindsight": bar["near_day_high_hindsight"],
                    "near_day_low_hindsight": bar["near_day_low_hindsight"],
                    "distance_to_aligned_day_extreme_hindsight": distance_to_aligned_extreme,
                    "distance_to_previous_profile": distance_to_previous_profile,
                    **_forward_metrics(bars, index, direction),
                }
                matches = []
                event_minute = int(bar["epoch"]) // 60
                for lag in range(PI_LEAD_MINUTES + 1):
                    for pi in pi_by_minute.get(event_minute + lag, []):
                        if int(pi["direction"]) == direction:
                            matches.append({"message_id": pi["message_id"], "lag_min": lag})
                            covered_pi.add(str(pi["message_id"]))
                row["pi_match"] = matches
                candidates.append(row)
        previous_profile = _profile(bars)
        previous_date = current_date

    for pi in pi_rows:
        minute = int(datetime.fromisoformat(pi["ts"]).timestamp()) // 60
        located = available_minutes.get(minute)
        if not located:
            continue
        bars, index = located
        bar = bars[index]
        if not _has_ohlc(bar):
            continue
        pi_feature_observations.append((bars, index, int(pi["direction"])))
        profile = bar.get("previous_profile") or {}
        price = float(bar["close"])
        nearest_level = None
        nearest_distance = None
        if profile:
            nearest_level = min(profile, key=lambda key: abs(price - profile[key]))
            nearest_distance = abs(price - profile[nearest_level])
        pi_diagnostics.append({
            "message_id": pi["message_id"],
            "time": pi["ts"],
            "direction": int(pi["direction"]),
            "kind": pi.get("kind"),
            "size": pi.get("size"),
            "price": price,
            "nearest_previous_profile": nearest_level,
            "distance_to_previous_profile": nearest_distance,
            "near_day_high_hindsight": bar["near_day_high_hindsight"],
            "near_day_low_hindsight": bar["near_day_low_hindsight"],
            "distance_to_aligned_day_extreme_hindsight": (
                price - float(bar["day_low_hindsight"])
                if int(pi["direction"]) > 0
                else float(bar["day_high_hindsight"]) - price
            ),
            "volume_ratio": round(float(bar["volume_ratio"]), 3),
            "delta": int(bar["delta"]),
            "cvd5": int(bar["cvd5"]),
            "depth_imbalance": round(float(bar["depth_imbalance"]), 4),
            "queue_imbalance": round(float(bar["queue_imbalance"]), 4),
            "ofi": int(bar.get("ofi") or 0),
            "bid_refill_qty": int(bar.get("bid_refill_qty") or 0),
            "ask_refill_qty": int(bar.get("ask_refill_qty") or 0),
            "max_buy_trade": int(bar.get("max_buy_trade") or 0),
            "max_sell_trade": int(bar.get("max_sell_trade") or 0),
            "event_path": [
                {
                    "offset_min": offset,
                    "aligned_price": round(
                        int(pi["direction"]) * (float(bars[index + offset]["close"]) - price), 4,
                    ),
                    "aligned_delta_ratio": round(
                        int(pi["direction"])
                        * math.copysign(float(bars[index + offset]["delta_ratio"]), bars[index + offset]["delta"]),
                        4,
                    ),
                }
                for offset in range(-10, 61)
                if 0 <= index + offset < len(bars)
                and _has_ohlc(bars[index + offset])
            ],
            "precursor_features": _precursor_features(bars, index, int(pi["direction"])),
            **_forward_metrics(bars, index, int(pi["direction"])),
        })

    raw_candidate_count = len(candidates)
    candidates = _apply_cooldown(candidates)
    pi_match_map: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        for match in candidate.get("pi_match") or []:
            pi_match_map[str(match["message_id"])].append({
                "rule": candidate["name"], "lag_min": int(match["lag_min"]),
            })
    for row in pi_diagnostics:
        row["matched_candidates"] = pi_match_map.get(str(row["message_id"]), [])
    covered_pi = {
        str(match["message_id"])
        for row in candidates for match in row.get("pi_match") or []
    }
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        grouped[row["name"]].append(row)
    summary = {
        name: _summarize(rows, len(days)) for name, rows in sorted(grouped.items())
    }
    split_summary = {}
    discovery_days = {day["date"] for day in days if day["date"] <= DISCOVERY_END}
    evaluation_days = {day["date"] for day in days if day["date"] > DISCOVERY_END}
    for name, rows in sorted(grouped.items()):
        split_summary[name] = {
            "discovery": _summarize(
                [row for row in rows if row["date"] in discovery_days], len(discovery_days),
            ),
            "evaluation": _summarize(
                [row for row in rows if row["date"] in evaluation_days], len(evaluation_days),
            ),
        }
    pi_available_ids = {str(row["message_id"]) for row in pi_diagnostics}
    pi_direction = Counter("long" if row["direction"] > 0 else "short" for row in pi_diagnostics)
    aligned_extreme = {
        str(row["message_id"]): (
            row["near_day_low_hindsight"] if row["direction"] > 0
            else row["near_day_high_hindsight"]
        ) for row in pi_diagnostics
    }
    profile_distances = [
        float(row["distance_to_previous_profile"])
        for row in pi_diagnostics if row["distance_to_previous_profile"] is not None
    ]
    pi_returns = [float(row["return_15m"]) for row in pi_diagnostics if "return_15m" in row]
    pi_returns_60 = [float(row["return_60m"]) for row in pi_diagnostics if "return_60m" in row]
    extreme_distances = [
        float(row["distance_to_aligned_day_extreme_hindsight"])
        for row in pi_diagnostics
    ]
    pi_kind_summary: dict[str, Any] = {}
    for kind in sorted({str(row.get("kind") or "unknown") for row in pi_diagnostics}):
        kind_rows = [row for row in pi_diagnostics if str(row.get("kind") or "unknown") == kind]
        returns15 = [float(row["return_15m"]) for row in kind_rows if "return_15m" in row]
        returns60 = [float(row["return_60m"]) for row in kind_rows if "return_60m" in row]
        pi_kind_summary[kind] = {
            "signals": len(kind_rows),
            "matched_by_candidate": sum(bool(row["matched_candidates"]) for row in kind_rows),
            "median_return_15m": round(_median(returns15), 3),
            "win_rate_15m": round(_safe_ratio(sum(x > 0 for x in returns15), len(returns15)), 3),
            "median_return_60m": round(_median(returns60), 3),
            "win_rate_60m": round(_safe_ratio(sum(x > 0 for x in returns60), len(returns60)), 3),
        }
    overlap_tests = {
        "pressure_only": _pi_overlap_permutation(
            candidates, pi_diagnostics,
            {
                "bull_sell_pressure_extreme", "bear_buy_pressure_extreme",
                "bull_pressure_flip", "bear_pressure_flip",
            },
        ),
        "czt_value_only": _pi_overlap_permutation(
            candidates, pi_diagnostics,
            {
                "bull_val_reclaim", "bear_vah_reject",
                "bull_vah_accept", "bear_val_accept",
            },
        ),
        "all_candidates": _pi_overlap_permutation(candidates, pi_diagnostics, None),
        "pressure_evaluation_dates": _pi_overlap_permutation(
            [row for row in candidates if row["date"] > DISCOVERY_END],
            [row for row in pi_diagnostics if row["time"][:10] > DISCOVERY_END],
            {
                "bull_sell_pressure_extreme", "bear_buy_pressure_extreme",
                "bull_pressure_flip", "bear_pressure_flip",
            },
        ),
    }
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "method": {
            "pi_match": "same-direction candidate at PI minute or 1-5 minutes before",
            "profile": "previous completed RTH, 70% of executed MBO volume by price",
            "hindsight_warning": "day-high/day-low proximity is diagnostic only",
            "depth_warning": "depth rules require corrected cache schema v5; depth remains a per-minute event-boundary maximum",
        },
        "coverage": {
            "cache_dates": [day["date"] for day in days],
            "cache_schema_versions": dict(Counter(day["schema_version"] for day in days)),
            "depth_valid_days": sum(day["schema_version"] >= 4 for day in days),
            "rth_days": len(days),
            "pi_total_unique": len(pi_rows),
            "pi_in_cached_rth": len(pi_diagnostics),
            "pi_direction": dict(pi_direction),
            "pi_covered_by_any_candidate": len(covered_pi & pi_available_ids),
            "raw_candidate_minutes": raw_candidate_count,
            "cooldown_candidate_alerts": len(candidates),
        },
        "pi_summary": {
            "aligned_day_extreme_within_8_points_hindsight": sum(aligned_extreme.values()),
            "aligned_day_extreme_within_20_points_hindsight": sum(value <= 20 for value in extreme_distances),
            "aligned_day_extreme_within_40_points_hindsight": sum(value <= 40 for value in extreme_distances),
            "previous_profile_within_20_points": sum(value <= 20 for value in profile_distances),
            "previous_profile_observations": len(profile_distances),
            "median_directional_return_15m": round(_median(pi_returns), 3),
            "directional_win_rate_15m": round(
                _safe_ratio(sum(value > 0 for value in pi_returns), len(pi_returns)), 3,
            ),
            "median_directional_return_60m": round(_median(pi_returns_60), 3),
            "directional_win_rate_60m": round(
                _safe_ratio(sum(value > 0 for value in pi_returns_60), len(pi_returns_60)), 3,
            ),
        },
        "pi_kind_summary": pi_kind_summary,
        "pi_level_summary": _summarize_pi_groups(pi_diagnostics),
        "pi_overlap_permutation": overlap_tests,
        "pi_precursor_feature_permutation": _precursor_feature_permutation(
            pi_feature_observations,
        ),
        "candidate_summary": summary,
        "discovery_evaluation_summary": split_summary,
        "pi_diagnostics": pi_diagnostics,
        "candidate_events": candidates,
    }


def _markdown(result: dict[str, Any]) -> str:
    coverage = result["coverage"]
    lines = [
        "# MNQ order flow × PI relationship study",
        "",
        f"Generated: `{result['generated_at']}`",
        "",
        "## Coverage",
        "",
        f"- RTH days: **{coverage['rth_days']}** ({', '.join(coverage['cache_dates'])})",
        f"- Corrected depth-cache days (schema v5): **{coverage['depth_valid_days']}**; versions: `{coverage['cache_schema_versions']}`",
        f"- Unique MNQ PI marks: **{coverage['pi_total_unique']}**; inside cached RTH: **{coverage['pi_in_cached_rth']}**",
        f"- Same-direction candidate in PI minute or prior 1–5m: **{coverage['pi_covered_by_any_candidate']}**",
        f"- Candidate minute states: **{coverage['raw_candidate_minutes']}**; after 10m alert cooldown: **{coverage['cooldown_candidate_alerts']}**",
        f"- PI 15m median/win: **{result['pi_summary']['median_directional_return_15m']} / {result['pi_summary']['directional_win_rate_15m']}**; PI 60m: **{result['pi_summary']['median_directional_return_60m']} / {result['pi_summary']['directional_win_rate_60m']}**",
        "",
        "> Day-high/day-low proximity is hindsight diagnostics, never a live feature. "
        "Displayed depth is a per-minute maximum rather than a synchronized closing book.",
        "",
        "## Fixed candidate rules",
        "",
        "| Rule | Events | /day | PI matches | 15m median | 15m win | 15m MFE | 15m MAE |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, row in result["candidate_summary"].items():
        lines.append(
            f"| {name} | {row.get('events', 0)} | {row.get('events_per_day', 0)} | "
            f"{row.get('pi_matches', 0)} | {row.get('median_return_15m', '—')} | "
            f"{row.get('win_return_15m', '—')} | {row.get('median_mfe_15m', '—')} | "
            f"{row.get('median_mae_15m', '—')} |"
        )
    lines.extend([
        "",
        "## Discovery versus evaluation",
        "",
        f"Rules were frozen from dates through **{DISCOVERY_END}**. Later dates are evaluation data.",
        "",
        "| Rule | Discovery N | Discovery 15m median/win | Evaluation N | Evaluation 15m median/win |",
        "|---|---:|---:|---:|---:|",
    ])
    for name, split in result["discovery_evaluation_summary"].items():
        discovery = split["discovery"]
        evaluation = split["evaluation"]
        lines.append(
            f"| {name} | {discovery.get('events', 0)} | "
            f"{discovery.get('median_return_15m', '—')} / {discovery.get('win_return_15m', '—')} | "
            f"{evaluation.get('events', 0)} | "
            f"{evaluation.get('median_return_15m', '—')} / {evaluation.get('win_return_15m', '—')} |"
        )
    lines.extend([
        "",
        "## Location diagnostics (hindsight labels only)",
        "",
        "The event rules do not use the completed day's high or low. The table only checks afterward whether "
        "a real-time event happened near the final extreme.",
        "",
        "| Rule | Alerts | Within 8 points of aligned day extreme | Distinct extreme days | Within 20 points of prior VAH/VAL/POC |",
        "|---|---:|---:|---:|---:|",
    ])
    for name, row in result["candidate_summary"].items():
        lines.append(
            f"| {name} | {row.get('events', 0)} | "
            f"{row.get('aligned_extreme_events_within_8_points_hindsight', 0)} | "
            f"{row.get('aligned_extreme_days_within_8_points_hindsight', 0)} | "
            f"{row.get('previous_profile_events_within_20_points', 0)} |"
        )
    lines.extend([
        "",
        "## PI type diagnostics",
        "",
        "| PI type | N | matched precursor | 15m median/win | 60m median/win |",
        "|---|---:|---:|---:|---:|",
    ])
    for kind, row in result["pi_kind_summary"].items():
        lines.append(
            f"| {kind} | {row['signals']} | {row['matched_by_candidate']} | "
            f"{row['median_return_15m']} / {row['win_rate_15m']} | "
            f"{row['median_return_60m']} / {row['win_rate_60m']} |"
        )
    lines.extend([
        "",
        "## PI overlap versus random same-day minutes",
        "",
        "| Candidate family | PI N | Observed | Random mean | Random P95 | p(null ≥ observed) |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for name, row in result["pi_overlap_permutation"].items():
        lines.append(
            f"| {name} | {row['signals']} | {row['observed']} | {row['null_mean']} | "
            f"{row.get('null_p95', '—')} | {row['p_ge_observed']} |"
        )
    lines.extend([
        "",
        "## PI precursor features versus random same-day minutes",
        "",
        "Each value summarizes the signal minute plus its prior five minutes. No threshold is fitted here. "
        "Same-day controls preserve the date; same-clock controls preserve the RTH minute across dates.",
        "",
        "| Feature | PI median | Same-day random | p | Same-clock random | p |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for name, row in result["pi_precursor_feature_permutation"].items():
        lines.append(
            f"| {name} | {row['observed_median']} | {row['same_day_random_median_mean']} | "
            f"{row['p_same_day_random_ge_observed']} | {row['same_clock_random_median_mean']} | "
            f"{row['p_same_clock_random_ge_observed']} |"
        )
    lines.extend([
        "",
        "## Responsive signal blueprint",
        "",
        "This study treats a signal as a one-shot state transition, not a continuously painted indicator:",
        "",
        "1. **Condition** — price is accepted above prior VAH, accepted below prior VAL, or rotating inside prior value.",
        "2. **Zone** — prior VAH/VAL/POC, a rolling auction extreme, or a repeatedly replenished MBO level.",
        "3. **Attack** — unusually concentrated aggressive trades and stacked diagonal imbalance reach that zone.",
        "4. **Failure** — high effort produces little price progress while passive fills/refills persist.",
        "5. **Turnover** — OFI, aggressor delta, and the close flip away from the defended level; emit once, then cool down.",
        "",
        "The attack is the earliest PI-like precursor but is noisy. The turnover is later and should be the executable alert. "
        "Acceptance through the zone invalidates the reversal interpretation and becomes a continuation candidate.",
        "",
        "### Proposed turnover-bubble encoding",
        "",
        "| Visual property | Meaning | Live-safe input |",
        "|---|---|---|",
        "| X/Y | Event time and defended price | MBO event timestamp and price |",
        "| Bubble area | Aggressive attack size, square-root scaled | Side-specific executed quantity versus rolling baseline |",
        "| Fill opacity | Passive defense confidence | Same-level fills plus near-fill replenishment |",
        "| Outer ring | Turnover confirmation | Opposite delta/OFI and close away from the defended level |",
        "| Blue / white | Buyer / seller defense | Resting side, not guessed participant identity |",
        "| A / D / T | Attack / defended / turnover state | One-shot state transition with expiry and cooldown |",
        "",
        "Do not draw a reversal bubble from a large trade alone. An attack without defense is continuation risk; "
        "a wall without executions is only displayed intent and may be cancelled.",
        "",
        "## Data-to-feature map",
        "",
        "| Question | MBO evidence | Research feature |",
        "|---|---|---|",
        "| Who attacked? | Trade side and size | buy/sell delta, trade burst, ≥50/100/150 counts |",
        "| Did passive liquidity hold? | Fill side/price plus subsequent book updates | passive fill and near-fill refill |",
        "| Did the queue strengthen? | F_LAST event-boundary BBO | queue imbalance and OFI |",
        "| Where did it happen? | Prior completed RTH trades by price | 70% VAH/VAL/POC |",
        "| Was effort rewarded? | Trade volume versus price progress | absorption/failure then opposite-side flip |",
        "",
        "MBO is anonymous. These are behavior signatures, not proof that an order belongs to an institution, market maker, "
        "or algorithm. High cancellation alone must not be called spoofing.",
        "",
        "## PI marks in available RTH data",
        "",
        "| Time (UTC) | PI | Kind | Price | Nearest prior VA level | Distance | 15m return | Day extreme (hindsight) |",
        "|---|---|---|---:|---|---:|---:|---|",
    ])
    for row in result["pi_diagnostics"]:
        extreme = "LOW" if row["near_day_low_hindsight"] else ("HIGH" if row["near_day_high_hindsight"] else "—")
        direction = "LONG" if row["direction"] > 0 else "SHORT"
        distance = row["distance_to_previous_profile"]
        lines.append(
            f"| {row['time']} | {direction} | {row.get('kind') or '—'} | {row['price']:.2f} | "
            f"{row.get('nearest_previous_profile') or '—'} | "
            f"{distance:.2f} | {row.get('return_15m', float('nan')):.2f} | {extreme} |"
            if distance is not None else
            f"| {row['time']} | {direction} | {row.get('kind') or '—'} | {row['price']:.2f} | — | — | "
            f"{row.get('return_15m', float('nan')):.2f} | {extreme} |"
        )
    lines.extend([
        "",
        "## Evidence references",
        "",
        "- [Databento MBO schema](https://databento.com/docs/schemas-and-data-formats/mbo)",
        "- [Databento CME state-management details](https://databento.com/docs/knowledge-base/datasets)",
        "- [CME Market by Order FAQ](https://www.cmegroup.com/articles/faqs/market-by-order-mbo.html)",
        "- [Cont, Kukanov & Stoikov — The Price Impact of Order Book Events](https://arxiv.org/abs/1011.6402)",
        "- [Gould & Bonart — Queue Imbalance as a One-Tick-Ahead Price Predictor](https://arxiv.org/abs/1512.03492)",
        "- [Xu, Gould & Howison — Multi-Level Order-Flow Imbalance](https://arxiv.org/abs/1907.06230)",
        "- [Bechler & Ludkovski — Order-flow and limit-book resiliency](https://arxiv.org/abs/1708.02715)",
        "- [Tolusic — Initiative and responsive flow is location-dependent](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7135258)",
        "- [Jesse Rogers — Condition, Zone, Trigger](https://jesserogerstrading.com/czt/what-is-condition-zone-trigger)",
        "- [Jesse Rogers — Previous-day value as condition](https://jesserogerstrading.com/czt/how-do-you-read-yesterdays-value)",
        "- [Jesse Rogers — Order flow, effort versus result](https://jesserogerstrading.com/order-flow)",
        "- [Rodas — Pre-registered null for touch OFI in NQ futures](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7330219)",
        "- [Andersen & Bondarenko — VPIN classification caution](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2292602)",
        "- [CFTC caution on inferring spoofing intent](https://www.cftc.gov/node?page=327)",
    ])
    return "\n".join(lines) + "\n"


def _write_event_chart(result: dict[str, Any], path: Path) -> None:
    import matplotlib.pyplot as plt

    by_offset: defaultdict[int, list[tuple[float, float]]] = defaultdict(list)
    for row in result["pi_diagnostics"]:
        for point in row.get("event_path") or []:
            by_offset[int(point["offset_min"])].append((
                float(point["aligned_price"]), float(point["aligned_delta_ratio"]),
            ))
    if not by_offset:
        return
    offsets = sorted(by_offset)
    price_median = [_median(value[0] for value in by_offset[offset]) for offset in offsets]
    delta_median = [_median(value[1] for value in by_offset[offset]) for offset in offsets]
    figure, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True, constrained_layout=True)
    figure.patch.set_facecolor("#080d15")
    for axis in axes:
        axis.set_facecolor("#080d15")
        axis.tick_params(colors="#aab7ca")
        for spine in axis.spines.values():
            spine.set_color("#26364a")
        axis.axvline(0, color="#f0a020", linewidth=1, linestyle="--")
        axis.axhline(0, color="#53657c", linewidth=0.8)
        axis.grid(color="#1a2636", alpha=0.55)
    axes[0].plot(offsets, price_median, color="#54d6ff", linewidth=2)
    axes[0].set_ylabel("direction-aligned MNQ points", color="#d9e3f0")
    axes[0].set_title(
        f"PI event study — median path, N={len(result['pi_diagnostics'])}",
        color="#eef5ff",
    )
    axes[1].plot(offsets, delta_median, color="#ff5f86", linewidth=1.6)
    axes[1].set_ylabel("aligned |delta| / rolling median", color="#d9e3f0")
    axes[1].set_xlabel("minutes from PI source timestamp", color="#d9e3f0")
    path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(path, dpi=160, facecolor=figure.get_facecolor())
    plt.close(figure)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-stem", default="orderflow_pi_relationship")
    args = parser.parse_args()
    result = run()
    output_dir = market_data.derived_path("research")
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{args.output_stem}.json"
    md_path = output_dir / f"{args.output_stem}.md"
    chart_path = output_dir / f"{args.output_stem}_event_study.png"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_markdown(result), encoding="utf-8")
    _write_event_chart(result, chart_path)
    print(md_path)
    print(json.dumps(result["coverage"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
