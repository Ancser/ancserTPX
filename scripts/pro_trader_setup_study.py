"""Research public discretionary setup families as frozen causal rules.

This runner is deliberately research-only.  It does not register a model,
change a preset, place an order, or change live behaviour.  The public
descriptions are translated into explicit rules so that the comparison is
repeatable rather than a visual or post-hoc judgement:

* opening-range breakout and first retest;
* VWAP pullback/reclaim;
* Linda Raschke-style ADX/momentum-high first 20-EMA pullback;
* prior-RTH Market Profile acceptance and failed auction;
* settled Databento MBO passive-rejection and absorption confirmation.

All candle entries are next-available 1-minute opens after completed 5/15m
signal bars.  Exits, costs, position sizing, same-bar ambiguity, walk-forward,
Monte Carlo, and slippage are delegated to the shared research backtester.
The MBO confirmation rows are limited to dates for which the local compact
schema-v5 MBO-derived cache exists; they are never treated as a long-history
edge estimate.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.backtest.prop_intraday_research import (  # noqa: E402
    ET,
    EntryCandidate,
    RthSession,
    _as_et,
    load_symbol_sessions,
    simulate_candidates,
    _score_trades,
)
from backend.backtest.robustness import evaluate, series_stats  # noqa: E402
from backend.data import market_data  # noqa: E402
from backend.db.models import current_quarterly_contract_id, get_tick_size  # noqa: E402
from backend.strategy.volume_profile import VolumeProfileCalculator  # noqa: E402
from scripts.orderflow_context_combination_study import (  # noqa: E402
    _enrich_context_day,
    _enrich_day,
)
from scripts.price_action_databento_orderflow_study import _flow_features  # noqa: E402


UTC = timezone.utc
SIGNAL_MINUTES = 5
RISK_DOLLARS = 200.0
TARGET_R = 2.0
MAX_TRADES_PER_DAY = 2
MAX_HOLD_MINUTES = 120
STOP_BUFFER_TICKS = 2
SLIPPAGE_STRESS_TICKS = 14
MC_SEED = 20260915
DISCOVERY_END = date(2024, 12, 31)
VALUE_AREA_PCT = 0.70


def _bar_range(bar: Any) -> float:
    return max(0.0, float(bar.high) - float(bar.low))


def _body(bar: Any) -> float:
    return abs(float(bar.close) - float(bar.open))


def _is_bull(bar: Any) -> bool:
    return float(bar.close) > float(bar.open)


def _is_bear(bar: Any) -> bool:
    return float(bar.close) < float(bar.open)


def _round_tick(price: float, tick: float) -> float:
    return round(float(price) / float(tick)) * float(tick)


def _ema(values: Sequence[float], length: int) -> list[float]:
    alpha = 2.0 / (float(length) + 1.0)
    result: list[float] = []
    prior: float | None = None
    for value in values:
        prior = float(value) if prior is None else alpha * float(value) + (1.0 - alpha) * prior
        result.append(prior)
    return result


def _atr(bars: Sequence[Any], length: int = 14) -> list[float | None]:
    true_ranges: list[float] = []
    for index, bar in enumerate(bars):
        previous_close = float(bars[index - 1].close) if index else float(bar.open)
        true_ranges.append(max(
            _bar_range(bar),
            abs(float(bar.high) - previous_close),
            abs(float(bar.low) - previous_close),
        ))
    result: list[float | None] = [None] * len(true_ranges)
    for index in range(length - 1, len(true_ranges)):
        result[index] = sum(true_ranges[index - length + 1:index + 1]) / float(length)
    return result


def _adx(bars: Sequence[Any], length: int = 14) -> list[float | None]:
    """Causal rolling ADX approximation using only completed bars.

    The source rule is a regime filter, not a claim that a particular charting
    vendor's Wilder smoothing is canonical.  Rolling true range and directional
    movement are intentionally frozen here before looking at results.
    """

    if not bars:
        return []
    tr: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for index, bar in enumerate(bars):
        if index == 0:
            previous_high = float(bar.high)
            previous_low = float(bar.low)
            previous_close = float(bar.open)
        else:
            previous = bars[index - 1]
            previous_high = float(previous.high)
            previous_low = float(previous.low)
            previous_close = float(previous.close)
        up_move = float(bar.high) - previous_high
        down_move = previous_low - float(bar.low)
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
        tr.append(max(
            _bar_range(bar),
            abs(float(bar.high) - previous_close),
            abs(float(bar.low) - previous_close),
        ))

    dx: list[float | None] = [None] * len(bars)
    for index in range(length - 1, len(bars)):
        tr_sum = sum(tr[index - length + 1:index + 1])
        if tr_sum <= 0:
            continue
        plus_di = 100.0 * sum(plus_dm[index - length + 1:index + 1]) / tr_sum
        minus_di = 100.0 * sum(minus_dm[index - length + 1:index + 1]) / tr_sum
        denominator = plus_di + minus_di
        dx[index] = 100.0 * abs(plus_di - minus_di) / denominator if denominator else 0.0

    result: list[float | None] = [None] * len(bars)
    for index in range(2 * length - 2, len(bars)):
        window = dx[index - length + 1:index + 1]
        if all(value is not None for value in window):
            result[index] = sum(float(value) for value in window) / float(length)
    return result


def _candidate(
    family: str,
    variant: str,
    session: RthSession,
    bar: Any,
    direction: int,
    stop_price: float,
    tick: float,
    reason: str,
    *,
    max_hold_minutes: int = MAX_HOLD_MINUTES,
    target_r: float = TARGET_R,
    meta: dict[str, Any] | None = None,
) -> EntryCandidate:
    return EntryCandidate(
        strategy=family,
        variant=variant,
        direction=int(direction),
        signal_index=int(bar.end_index),
        signal_time=bar.timestamp,
        stop_price=_round_tick(stop_price, tick),
        target1_kind="risk",
        target1_value=float(target_r),
        max_hold_minutes=int(max_hold_minutes),
        risk_dollars=RISK_DOLLARS,
        reason=reason,
        meta={"tick": tick, "target_r": target_r, **(meta or {})},
    )


def _generate_orb(
    session: RthSession,
    tick: float,
    *,
    opening_minutes: int,
    mode: str,
) -> list[EntryCandidate]:
    bars = session.signal_bars(SIGNAL_MINUTES)
    opening_bars = opening_minutes // SIGNAL_MINUTES
    if opening_minutes % SIGNAL_MINUTES or len(bars) <= opening_bars:
        return []
    opening = bars[:opening_bars]
    opening_high = max(float(bar.high) for bar in opening)
    opening_low = min(float(bar.low) for bar in opening)
    variant = f"orb{opening_minutes}_{mode}"
    fired: set[int] = set()
    result: list[EntryCandidate] = []
    tolerance = 2.0 * tick
    for index in range(opening_bars, len(bars)):
        current = bars[index]
        if index <= opening_bars:
            earlier = []
        else:
            earlier = bars[opening_bars:index]
        for direction, boundary in ((1, opening_high), (-1, opening_low)):
            if direction in fired:
                continue
            if mode == "breakout":
                crossed = (
                    _is_bull(current) and float(current.close) > boundary
                    if direction > 0
                    else _is_bear(current) and float(current.close) < boundary
                )
            else:
                broke = any(
                    float(row.close) > opening_high if direction > 0
                    else float(row.close) < opening_low
                    for row in earlier
                )
                crossed = (
                    broke
                    and _is_bull(current)
                    and boundary - tolerance <= float(current.low) <= boundary + tolerance
                    and float(current.close) > boundary
                    if direction > 0
                    else broke
                    and _is_bear(current)
                    and boundary - tolerance <= float(current.high) <= boundary + tolerance
                    and float(current.close) < boundary
                )
            if not crossed:
                continue
            stop = opening_low - STOP_BUFFER_TICKS * tick if direction > 0 else opening_high + STOP_BUFFER_TICKS * tick
            result.append(_candidate(
                "ORB", variant, session, current, direction, stop, tick,
                f"OR{opening_minutes} {mode}; boundary={boundary:.2f}",
                meta={"opening_minutes": opening_minutes, "or_high": opening_high, "or_low": opening_low},
            ))
            fired.add(direction)
    return result


def _generate_vwap_reclaim(
    session: RthSession,
    tick: float,
    *,
    minutes: int,
) -> list[EntryCandidate]:
    bars = session.signal_bars(minutes)
    if len(bars) < 4:
        return []
    variant = f"vwap_reclaim_{minutes}m"
    result: list[EntryCandidate] = []
    fired: set[int] = set()
    tolerance = 2.0 * tick
    for index in range(3, len(bars)):
        current = bars[index]
        prior = bars[index - 1]
        prior_three = bars[index - 3:index]
        for direction in (1, -1):
            if direction in fired:
                continue
            if direction > 0:
                setup = (
                    all(float(row.close) < float(row.vwap) - tick for row in prior_three)
                    and float(current.low) <= float(current.vwap) + tolerance
                    and float(current.close) > float(current.vwap) + tick
                    and _is_bull(current)
                    and float(current.close) > float(prior.high)
                )
                stop = min(float(row.low) for row in tuple(prior_three) + (current,)) - STOP_BUFFER_TICKS * tick
            else:
                setup = (
                    all(float(row.close) > float(row.vwap) + tick for row in prior_three)
                    and float(current.high) >= float(current.vwap) - tolerance
                    and float(current.close) < float(current.vwap) - tick
                    and _is_bear(current)
                    and float(current.close) < float(prior.low)
                )
                stop = max(float(row.high) for row in tuple(prior_three) + (current,)) + STOP_BUFFER_TICKS * tick
            if not setup:
                continue
            result.append(_candidate(
                "VWAP_RECLAIM", variant, session, current, direction, stop, tick,
                f"{minutes}m VWAP first pullback/reclaim",
                meta={"signal_vwap": float(current.vwap), "signal_std": float(current.vwap_std)},
            ))
            fired.add(direction)
    return result


def _generate_holy_grail(
    session: RthSession,
    tick: float,
    *,
    minutes: int,
) -> list[EntryCandidate]:
    """Generate the frozen ADX > 30 / momentum extreme / first EMA20 pullback.

    A momentum seed starts a maximum eight-bar pending window.  Only the first
    pullback touching EMA20 and closing through the prior bar's extreme can
    fire.  A later pullback cannot overwrite the seed, which keeps the rule
    from selecting the best-looking historical retracement.
    """

    bars = session.signal_bars(minutes)
    if len(bars) < 40:
        return []
    closes = [float(bar.close) for bar in bars]
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    atr14 = _atr(bars, 14)
    adx14 = _adx(bars, 14)
    variant = f"holy_grail_{minutes}m"
    pending: dict[int, tuple[int, int]] = {}
    cooldown: dict[int, int] = {1: -1, -1: -1}
    result: list[EntryCandidate] = []
    for index in range(1, len(bars)):
        current = bars[index]
        previous = bars[index - 1]
        adx = adx14[index]
        atr = atr14[index]
        if adx is None or atr is None or atr <= 0:
            continue
        for direction in (1, -1):
            armed = pending.get(direction)
            if armed is not None and index > armed[1]:
                pending.pop(direction, None)
                armed = None
            if armed is not None and index > armed[0] and index <= armed[1]:
                start = max(armed[0] + 1, index - 4)
                pullback = bars[start:index]
                touched = any(
                    float(row.low) <= ema20[pos] + tick
                    and float(row.low) >= ema20[pos] - 0.75 * float(atr14[pos] or atr)
                    if direction > 0
                    else float(row.high) >= ema20[pos] - tick
                    and float(row.high) <= ema20[pos] + 0.75 * float(atr14[pos] or atr)
                    for pos, row in ((start + offset, row) for offset, row in enumerate(pullback))
                )
                reversal = (
                    _is_bull(current)
                    and float(current.close) > float(previous.high)
                    and float(current.close) > ema20[index]
                    and ema20[index] > ema50[index]
                    if direction > 0
                    else _is_bear(current)
                    and float(current.close) < float(previous.low)
                    and float(current.close) < ema20[index]
                    and ema20[index] < ema50[index]
                )
                if touched and reversal:
                    if direction > 0:
                        stop = min(float(row.low) for row in tuple(pullback) + (current,)) - STOP_BUFFER_TICKS * tick
                    else:
                        stop = max(float(row.high) for row in tuple(pullback) + (current,)) + STOP_BUFFER_TICKS * tick
                    result.append(_candidate(
                        "HOLY_GRAIL", variant, session, current, direction, stop, tick,
                        f"ADX>{30:g}; new momentum extreme; first EMA20 pullback",
                        meta={"adx": round(float(adx), 4), "ema20": ema20[index], "ema50": ema50[index]},
                    ))
                    pending.pop(direction, None)
                    cooldown[direction] = index + 8
                    continue

            if index <= cooldown[direction] or index < 20 or pending.get(direction) is not None:
                continue
            prior_closes = closes[index - 20:index]
            seed = (
                float(current.close) > max(prior_closes)
                and float(current.close) > ema20[index]
                and ema20[index] > ema50[index]
                and float(adx) >= 30.0
                if direction > 0
                else float(current.close) < min(prior_closes)
                and float(current.close) < ema20[index]
                and ema20[index] < ema50[index]
                and float(adx) >= 30.0
            )
            if seed:
                pending[direction] = (index, index + 8)
    return result


def _previous_profile(session: RthSession, tick: float) -> dict[str, float] | None:
    try:
        profile = VolumeProfileCalculator(
            tick_size=tick, value_area_pct=VALUE_AREA_PCT,
        ).calculate(list(session.bars))
    except (ValueError, TypeError):
        return None
    return {
        "poc": float(profile.poc),
        "vah": float(profile.vah),
        "val": float(profile.val),
        "total_volume": float(profile.total_volume),
    }


def _generate_profile(
    session: RthSession,
    profile: dict[str, float] | None,
    tick: float,
    *,
    mode: str,
) -> list[EntryCandidate]:
    if not profile:
        return []
    bars = session.signal_bars(SIGNAL_MINUTES)
    if len(bars) < 3:
        return []
    vah = profile["vah"]
    val = profile["val"]
    variant = f"profile_{mode}"
    result: list[EntryCandidate] = []
    last_fired = {1: -1000, -1: -1000}
    tolerance = 2.0 * tick
    for index in range(2, len(bars)):
        current = bars[index]
        prior = bars[index - 1]
        before = bars[index - 2]
        for direction in (1, -1):
            if index - last_fired[direction] < 6:
                continue
            if mode == "acceptance":
                if direction > 0:
                    setup = (
                        float(before.close) <= vah + tolerance
                        and float(prior.close) > vah
                        and float(current.close) > vah
                        and float(prior.low) >= vah - tolerance
                        and float(current.low) >= vah - tolerance
                        and _is_bull(current)
                    )
                    stop = min(float(prior.low), float(current.low), vah) - STOP_BUFFER_TICKS * tick
                else:
                    setup = (
                        float(before.close) >= val - tolerance
                        and float(prior.close) < val
                        and float(current.close) < val
                        and float(prior.high) <= val + tolerance
                        and float(current.high) <= val + tolerance
                        and _is_bear(current)
                    )
                    stop = max(float(prior.high), float(current.high), val) + STOP_BUFFER_TICKS * tick
            else:
                if direction > 0:
                    setup = (
                        float(current.low) < val - tick
                        and float(current.close) > val
                        and _is_bull(current)
                    )
                    stop = float(current.low) - STOP_BUFFER_TICKS * tick
                else:
                    setup = (
                        float(current.high) > vah + tick
                        and float(current.close) < vah
                        and _is_bear(current)
                    )
                    stop = float(current.high) + STOP_BUFFER_TICKS * tick
            if not setup:
                continue
            result.append(_candidate(
                "MARKET_PROFILE", variant, session, current, direction, stop, tick,
                f"prior RTH 70% profile {mode}; POC={profile['poc']:.2f} VAH={vah:.2f} VAL={val:.2f}",
                meta={"prior_poc": profile["poc"], "prior_vah": vah, "prior_val": val},
            ))
            last_fired[direction] = index
    return result


def _flow_cache_paths(symbol: str) -> list[Path]:
    root = market_data.derived_path("orderflow", symbol.lower())
    preferred: dict[str, Path] = {}
    for path in sorted(root.glob(f"all_sessions_footprint_{symbol.lower()}_*.json.gz")):
        name = path.name
        prefix = f"all_sessions_footprint_{symbol.lower()}_"
        preferred[name[len(prefix):len(prefix) + 10]] = path
    for path in sorted(root.glob(f"footprint_{symbol.lower()}_*.json.gz")):
        name = path.name
        day = name[len(f"footprint_{symbol.lower()}_"):len(f"footprint_{symbol.lower()}_") + 10]
        preferred.setdefault(day, path)
    return [preferred[key] for key in sorted(preferred)]


def _load_flow_days(
    symbol: str,
    *,
    start: date | None = None,
    end: date | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Load RTH slices from local schema-v5 MBO-derived compact caches."""

    flow_by_date: dict[str, dict[str, Any]] = {}
    schema_versions: Counter[str] = Counter()
    paths = _flow_cache_paths(symbol)
    previous_profile: dict[str, float] = {}
    loaded_paths: list[str] = []
    for path in paths:
        prefix = f"all_sessions_footprint_{symbol.lower()}_" if "all_sessions" in path.name else f"footprint_{symbol.lower()}_"
        path_day = path.name[len(prefix):len(prefix) + 10]
        try:
            path_date = date.fromisoformat(path_day)
        except ValueError:
            continue
        if start and path_date < start:
            continue
        if end and path_date > end:
            continue
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        meta = payload.get("meta") or {}
        schema = int(meta.get("schema_version") or 0)
        meta_session = str(meta.get("session") or "").upper()
        date_key = str(meta.get("calendar_date") or meta.get("trade_date") or "")
        if not date_key:
            date_key = path.name[len(prefix):len(prefix) + 10]
        bars = [
            bar for bar in (payload.get("bars") or [])
            if (
                str(bar.get("session") or "").upper() == "RTH"
                or (meta_session == "RTH" and not bar.get("session"))
            )
        ]
        if not bars or schema < 5:
            continue
        day = {
            "date": date_key,
            "path": str(path),
            "schema_version": schema,
            "bars": bars,
        }
        _enrich_day(day, previous_profile)
        _enrich_context_day(day)
        try:
            from scripts.orderflow_pi_relationship_study import _profile as mbo_profile
            previous_profile = mbo_profile(bars)
        except (TypeError, ValueError):
            previous_profile = {}
        flow_by_date[date_key] = day
        loaded_paths.append(str(path))
        schema_versions[str(schema)] += 1
    dates = sorted(flow_by_date)
    return flow_by_date, {
        "days": len(dates),
        "start": dates[0] if dates else None,
        "end": dates[-1] if dates else None,
        "dates": dates,
        "schema_versions": dict(schema_versions),
        "cache_paths": loaded_paths,
    }


def _mbo_gate(features: dict[str, Any], direction: int, mode: str) -> bool:
    passive = int(features.get("mbo_passive_side") or 0) == int(direction)
    if mode == "passive_rejection":
        return passive
    delta = int(features.get("mbo_delta_side") or 0)
    volume_ratio = features.get("mbo_volume_ratio_5m")
    return bool(
        passive
        and (delta == 0 or delta == -int(direction))
        and volume_ratio is not None
        and float(volume_ratio) >= 1.0
    )


def _attach_mbo(
    sessions: Sequence[RthSession],
    base_candidates: dict[str, dict[str, list[EntryCandidate]]],
    flow_by_date: dict[str, dict[str, Any]],
    tick: float,
) -> dict[str, dict[str, list[EntryCandidate]]]:
    result: dict[str, dict[str, list[EntryCandidate]]] = {
        "mbo_passive_rejection": defaultdict(list),
        "mbo_absorption": defaultdict(list),
    }
    sessions_by_date = {session.session_date.isoformat(): session for session in sessions}
    signal_maps = {
        day: {bar.end_index: bar for bar in session.signal_bars(SIGNAL_MINUTES)}
        for day, session in sessions_by_date.items()
    }
    for base_name, rows_by_day in base_candidates.items():
        for day, candidates in rows_by_day.items():
            flow = flow_by_date.get(day)
            session = sessions_by_date.get(day)
            signal_map = signal_maps.get(day, {})
            if flow is None or session is None:
                continue
            for candidate in candidates:
                signal_bar = signal_map.get(candidate.signal_index)
                if signal_bar is None:
                    continue
                features = _flow_features(
                    flow, int(candidate.signal_time.timestamp()), signal_bar, tick=tick,
                )
                if features is None:
                    continue
                for mode in ("passive_rejection", "absorption"):
                    if not _mbo_gate(features, candidate.direction, mode):
                        continue
                    variant = f"{base_name}+mbo_{mode}"
                    result["mbo_" + mode][day].append(replace(
                        candidate,
                        strategy="MBO_CONFIRMATION",
                        variant=variant,
                        reason=(candidate.reason + f"; MBO {mode}"),
                        meta={**candidate.meta, "mbo_features": {
                            key: features.get(key) for key in (
                                "mbo_delta_5m", "mbo_volume_ratio_5m", "mbo_passive_side",
                                "mbo_imbalance_side", "mbo_ofi_side",
                            )
                        }},
                    ))
    return result


def _score(trades: Sequence[Any], *, mc_iters: int) -> dict[str, Any]:
    """Compact shared robustness output plus year/side/exit diagnostics."""

    rows = [trade.robustness_row() for trade in trades]
    robust = evaluate(
        rows,
        iters=mc_iters,
        seed=MC_SEED,
        dd_threshold=2000.0,
        slip_levels=(1, 2, 4, 8, SLIPPAGE_STRESS_TICKS),
    )
    stats = robust["stats"]
    stress = next(
        item["stats"] for item in robust["slip"]["levels"]
        if item["level"] == SLIPPAGE_STRESS_TICKS
    )
    mc = robust.get("monte_carlo") or {}
    wf = robust.get("walk_forward") or {}
    by_year: dict[str, list[float]] = defaultdict(list)
    by_side: dict[str, list[float]] = defaultdict(list)
    exits: Counter[str] = Counter()
    for trade in trades:
        by_year[str(_as_et(trade.entry_time).year)].append(float(trade.pnl))
        by_side[str(trade.direction)].append(float(trade.pnl))
        exits[str(trade.exit_reason)] += 1
    compact_mc = {
        key: mc.get(key) for key in (
            "iters", "seed", "n", "p_loss", "dd_p95", "dd_threshold", "pf_p5",
        ) if key in mc
    }
    mc_pass = bool(
        mc
        and mc.get("p_loss", 1.0) <= 0.05
        and mc.get("dd_p95", float("inf")) < 2000.0
        and mc.get("pf_p5", 0.0) > 1.0
    )
    if len(trades) < 40:
        verdict = "THIN_SAMPLE"
    elif float(stats["pf"]) <= 1.0:
        verdict = "FAIL_BASE_PF"
    elif not bool(wf.get("pass")):
        verdict = "FAIL_WALK_FORWARD"
    elif float(stress["pf"]) <= 1.0:
        verdict = "FAIL_14T_SLIPPAGE"
    elif not mc_pass:
        verdict = "FAIL_MONTE_CARLO"
    else:
        verdict = "RESEARCH_CANDIDATE"
    return {
        "trades": int(stats["n"]),
        "pnl": round(float(stats["pnl"]), 2),
        "pf": round(float(stats["pf"]), 4),
        "win": round(float(stats["win"]), 4),
        "max_dd": round(float(stats["max_dd"]), 2),
        "expectancy": round(float(stats["pnl"]) / len(trades), 2) if trades else 0.0,
        "slip14_pnl": round(float(stress["pnl"]), 2),
        "slip14_pf": round(float(stress["pf"]), 4),
        "walk_forward_pass": bool(wf.get("pass")),
        "walk_forward_segments": [
            {key: segment.get(key) for key in ("n", "pnl", "pf", "win")}
            for segment in wf.get("segments", [])
        ],
        "monte_carlo_pass": mc_pass,
        "monte_carlo": compact_mc,
        "yearly": {year: series_stats(values) for year, values in sorted(by_year.items())},
        "by_side": {side: series_stats(values) for side, values in sorted(by_side.items())},
        "exit_reasons": dict(sorted(exits.items())),
        "verdict": verdict,
    }


def _run_symbol(symbol: str, *, mc_iters: int, start: date | None, end: date | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    sessions, info = load_symbol_sessions(symbol, start_date=start, end_date=end)
    tick = get_tick_size(current_quarterly_contract_id(symbol))
    profile_by_day = {
        session.session_date.isoformat(): _previous_profile(session, tick)
        for session in sessions
    }
    base_candidates: dict[str, dict[str, list[EntryCandidate]]] = defaultdict(lambda: defaultdict(list))
    generators: tuple[tuple[str, Callable[[RthSession, RthSession | None], list[EntryCandidate]]], ...] = (
        ("orb15_breakout", lambda session, _previous: _generate_orb(session, tick, opening_minutes=15, mode="breakout")),
        ("orb15_retest", lambda session, _previous: _generate_orb(session, tick, opening_minutes=15, mode="retest")),
        ("orb30_breakout", lambda session, _previous: _generate_orb(session, tick, opening_minutes=30, mode="breakout")),
        ("orb30_retest", lambda session, _previous: _generate_orb(session, tick, opening_minutes=30, mode="retest")),
        ("vwap_reclaim_5m", lambda session, _previous: _generate_vwap_reclaim(session, tick, minutes=5)),
        ("vwap_reclaim_15m", lambda session, _previous: _generate_vwap_reclaim(session, tick, minutes=15)),
        ("holy_grail_5m", lambda session, _previous: _generate_holy_grail(session, tick, minutes=5)),
        ("holy_grail_15m", lambda session, _previous: _generate_holy_grail(session, tick, minutes=15)),
        ("profile_acceptance", lambda session, previous: _generate_profile(
            session,
            profile_by_day.get(previous.session_date.isoformat()) if previous is not None else None,
            tick,
            mode="acceptance",
        )),
        ("profile_failed_auction", lambda session, previous: _generate_profile(
            session,
            profile_by_day.get(previous.session_date.isoformat()) if previous is not None else None,
            tick,
            mode="failed_auction",
        )),
    )
    for index, session in enumerate(sessions):
        previous = sessions[index - 1] if index else None
        day = session.session_date.isoformat()
        for name, generator in generators:
            base_candidates[name][day].extend(generator(session, previous))

    flow_by_date, flow_coverage = _load_flow_days(symbol, start=start, end=end)
    mbo_candidates = _attach_mbo(sessions, base_candidates, flow_by_date, tick)
    all_sets: dict[str, dict[str, list[EntryCandidate]]] = dict(base_candidates)
    for mode, rows in mbo_candidates.items():
        for day, candidates in rows.items():
            for candidate in candidates:
                key = candidate.variant
                all_sets.setdefault(key, defaultdict(list))[day].append(candidate)

    result_rows: list[dict[str, Any]] = []
    trade_rows: list[dict[str, Any]] = []
    sessions_by_day = {session.session_date.isoformat(): session for session in sessions}
    for name in sorted(all_sets):
        rows_by_day = all_sets[name]
        nonempty_days = [day for day, rows in rows_by_day.items() if rows]
        trades: list[Any] = []
        skipped: Counter[str] = Counter()
        for day in sorted(rows_by_day):
            session = sessions_by_day.get(day)
            if session is None:
                continue
            session_trades, session_skipped = simulate_candidates(
                session, rows_by_day[day], symbol, MAX_TRADES_PER_DAY,
            )
            trades.extend(session_trades)
            skipped.update(session_skipped)
        metrics = _score(trades, mc_iters=mc_iters)
        result_rows.append({
            "symbol": symbol,
            "setup": name,
            "family": "MBO_CONFIRMATION" if name.endswith("mbo_passive_rejection") or name.endswith("mbo_absorption") else (
                "ORB" if name.startswith("orb") else
                "VWAP_RECLAIM" if name.startswith("vwap") else
                "HOLY_GRAIL" if name.startswith("holy") else "MARKET_PROFILE"
            ),
            "candidates": sum(len(rows) for rows in rows_by_day.values()),
            "candidate_days": len(nonempty_days),
            "skipped": dict(skipped),
            **metrics,
        })
        for trade in trades:
            row = asdict(trade)
            row.update({"setup": name, "symbol": symbol})
            trade_rows.append(row)

    coverage = {
        "symbol": symbol,
        "dataset": asdict(info),
        "rth_days": len(sessions),
        "rth_start": sessions[0].session_date.isoformat() if sessions else None,
        "rth_end": sessions[-1].session_date.isoformat() if sessions else None,
        "mbo": flow_coverage,
        "mbo_join_days": len(set(flow_by_date) & set(sessions_by_day)),
    }
    return {"coverage": coverage, "results": result_rows}, trade_rows


def _write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields or ["symbol"])
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in fields} for row in rows)


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Professional setup family study",
        "",
        f"Status: **{payload['status']}**",
        f"Generated: `{payload['generated_at']}`",
        "",
        "This is a causal RTH research comparison.  It is not live approval.",
        "All base entries use the next available 1m open after a completed 5/15m signal; costs, one-position-at-a-time execution, walk-forward, Monte Carlo, and 14-tick stress use the shared backtester.",
        "",
        "## Coverage",
        "",
    ]
    for symbol, item in sorted(payload["coverage"].items()):
        dataset = item["dataset"]
        mbo = item["mbo"]
        lines.append(
            f"- **{symbol}**: {item['rth_days']} RTH days ({item['rth_start']} → {item['rth_end']}); "
            f"{dataset['total_bars']:,} 1m bars; MBO RTH cache {mbo['days']} days "
            f"({mbo['start'] or '—'} → {mbo['end'] or '—'})."
        )
    lines.extend([
        "",
        "## Results",
        "",
        "Verdicts require at least 40 trades, positive base PF, walk-forward pass, positive 14-tick PF, and the shared Monte Carlo gates.  Smaller MBO rows remain thin even when PF looks high.",
        "",
        "| Symbol | Setup | Candidates | Trades | P&L | PF | Max DD | 14t PF | WF | Verdict |",
        "|---|---|---:|---:|---:|---:|---:|---:|:---:|---|",
    ])
    rows = sorted(payload["results"], key=lambda row: (row["symbol"], -float(row.get("pnl", 0.0))))
    for row in rows:
        lines.append(
            f"| {row['symbol']} | {row['setup']} | {row['candidates']} | {row['trades']} | "
            f"${_fmt(row['pnl'])} | {_fmt(row['pf'])} | ${_fmt(row['max_dd'])} | "
            f"{_fmt(row['slip14_pf'])} | {'PASS' if row['walk_forward_pass'] else 'FAIL'} | {row['verdict']} |"
        )
    lines.extend([
        "",
        "## Frozen definitions",
        "",
        "- ORB: first 15m and 30m RTH range; breakout is the first completed bar closing outside; retest requires an earlier close outside followed by a first boundary retest and close back through it.",
        "- VWAP: first three completed 5/15m bars on the opposite side of causal RTH VWAP, then a single pullback/reclaim candle.",
        "- Holy Grail: causal rolling ADX14 ≥ 30, a close making a prior-20-bar momentum extreme, then the first ≤8-bar EMA20 touch with a close through the prior bar's extreme.",
        "- Market Profile: previous complete RTH 70% value area; acceptance is two closes holding beyond VAH/VAL, failed auction is a probe beyond VAH/VAL that closes back inside.",
        "- MBO absorption: compact schema-v5 settled Databento RTH window; passive rejection agrees with the trade side, aggressive delta is zero/opposite, and five-minute volume is at least its causal baseline.",
        "",
        "The MBO cache is recent and not an audited multi-year sample.  No setup was promoted to live and no production strategy or preset was changed.",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default=["MNQ", "MES"])
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--mc-iters", type=int, default=1000)
    parser.add_argument(
        "--output",
        default=str(market_data.derived_path("research", "pro_trader_setup_study_current.json")),
    )
    parser.add_argument("--include-trades", action="store_true")
    args = parser.parse_args()
    symbols = [str(value).upper() for value in args.symbols]
    bad = [value for value in symbols if value not in {"MNQ", "MES"}]
    if bad:
        parser.error(f"unsupported symbols: {', '.join(bad)}")
    if args.mc_iters <= 0:
        parser.error("--mc-iters must be positive")
    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None
    if start and end and start > end:
        parser.error("--start must be <= --end")

    started = time.time()
    payload: dict[str, Any] = {
        "status": "provisional_research_only",
        "generated_at": datetime.now(UTC).isoformat(),
        "study": {
            "symbols": symbols,
            "signal_timeframes_minutes": [5, 15],
            "target_r": TARGET_R,
            "risk_dollars": RISK_DOLLARS,
            "max_trades_per_day": MAX_TRADES_PER_DAY,
            "value_area_pct": VALUE_AREA_PCT,
            "discovery_end": DISCOVERY_END.isoformat(),
            "entry": "next available 1m open after completed causal signal bar",
            "execution": "shared prop_intraday_research simulate_candidates",
            "costs": "canonical contract commission and fees",
            "data_scope": "canonical reconciled futures 1m store; RTH only for base setup families",
        },
        "coverage": {},
        "results": [],
        "notes": [
            "Discovery cutoff is recorded for audit context; no result was selected from the later evaluation block.",
            "The MBO confirmation rows use only local settled schema-v5 compact RTH caches and are sample-limited.",
            "A positive backtest is not evidence of a live edge; all rows remain research-only.",
        ],
    }
    all_trades: list[dict[str, Any]] = []
    for symbol in symbols:
        print(f"[{symbol}] loading canonical RTH sessions and generating frozen setup candidates")
        item, trades = _run_symbol(symbol, mc_iters=args.mc_iters, start=start, end=end)
        payload["coverage"][symbol] = item["coverage"]
        payload["results"].extend(item["results"])
        all_trades.extend(trades)
        print(f"[{symbol}] {item['coverage']['rth_days']} RTH days, {len(item['results'])} variants")
    payload["elapsed_seconds"] = round(time.time() - started, 3)
    if args.include_trades:
        payload["trades"] = all_trades

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default), encoding="utf-8")
    _write_csv(output.with_suffix(".csv"), payload["results"])
    _write_csv(output.with_name(output.stem + "_trades.csv"), all_trades)
    output.with_suffix(".md").write_text(_markdown(payload), encoding="utf-8")
    print(f"JSON: {output}")
    print(f"CSV: {output.with_suffix('.csv')}")
    print(f"Trades: {output.with_name(output.stem + '_trades.csv')}")
    print(f"Report: {output.with_suffix('.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
