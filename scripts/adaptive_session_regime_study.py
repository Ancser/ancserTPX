"""Research-only adaptive session/regime routing study.

This runner is intentionally separate from the production strategy registry.
It answers a narrow question: can a small, pre-declared set of session-aware
methods be routed by information available at the completed 5-minute bar?

The study uses the canonical 1-minute candle store for MNQ/MES and replays:

* ORB15: first 15-minute range breakout;
* VWAPREV: session VWAP rejection back toward VWAP;
* DONCHIAN: 20 completed 5-minute-bar channel breakout;
* VWAPPB: EMA/VWAP trend pullback;
* LEVELREJ: rejection of the prior same-code session extreme.

The adaptive router is fixed before reading results:

* ASIA/EURO/PRE/AH: context-only, no trade;
* trend state: breakout/pullback (DONCHIAN, ORB in RTH);
* range state: VWAPREV;
* transition state: no trade;
* PRE is treated as a transition-heavy information window, so it only trades
  a confirmed trend breakout.

All entries use the next available 1-minute open. Exits use the raw 1-minute
high/low with conservative stop-first resolution when both levels occur in one
bar. Costs use the canonical contract commission/fees, and stress subtracts a
14-tick round-trip slippage budget. This is an offline falsification study;
it does not alter live/backtest behaviour or place orders.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.data import candle_store, market_data  # noqa: E402
from backend.db.models import (  # noqa: E402
    Candle,
    current_quarterly_contract_id,
    get_commission_rt,
    get_fees_rt,
    get_point_value,
    get_tick_size,
)
from backend.strategy.session_filter import market_session, market_session_code, market_session_id  # noqa: E402
from backend.timebase import UTC, as_utc  # noqa: E402


SESSION_CODES = ("ASIA", "EURO", "PRE", "RTH", "AH")
METHODS = ("ORB15", "VWAPREV", "DONCHIAN", "VWAPPB", "LEVELREJ")
STRESS_TICKS = 14.0
MAX_HOLD_MINUTES = 120
STOP_BUFFER_TICKS = 2.0
MIN_RISK_TICKS = 4.0
TARGET_R = 2.0
MC_SEED = 20260915


@dataclass(frozen=True)
class Bar5:
    start_pos: int
    end_pos: int
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class SessionTape:
    session_id: str
    code: str
    session_date: date
    start: datetime
    candles: list[Candle]
    bars: list[Bar5]


@dataclass(frozen=True)
class Candidate:
    method: str
    session_id: str
    session_code: str
    signal_time: datetime
    signal_index: int
    entry_pos: int
    direction: int
    entry_price: float
    stop_price: float
    target_price: float
    regime: str
    reason: str


@dataclass(frozen=True)
class Trade:
    method: str
    session_id: str
    session_code: str
    signal_time: datetime
    entry_time: datetime
    exit_time: datetime
    direction: int
    entry_price: float
    exit_price: float
    gross_pnl: float
    costs: float
    pnl: float
    stress_pnl: float
    exit_reason: str
    regime: str
    reason: str


def _finite(value: object, default: float = 0.0) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _median(values: Iterable[float]) -> Optional[float]:
    values = [float(value) for value in values if math.isfinite(float(value))]
    return statistics.median(values) if values else None


def _session_date(session_id: str) -> date:
    return date.fromisoformat(str(session_id)[:10])


def _build_sessions(candles: list[Candle]) -> list[SessionTape]:
    """Group candles by DST-aware session and aggregate complete 5m bars."""

    grouped: dict[str, list[Candle]] = defaultdict(list)
    for candle in sorted(candles, key=lambda item: as_utc(item.timestamp)):
        grouped[market_session_id(candle.timestamp)].append(candle)

    out: list[SessionTape] = []
    for session_id, rows in sorted(grouped.items()):
        rows = sorted(rows, key=lambda item: as_utc(item.timestamp))
        if not rows:
            continue
        code = market_session_code(rows[0].timestamp)
        _, start = market_session(rows[0].timestamp)
        start = as_utc(start)
        buckets: dict[int, list[int]] = defaultdict(list)
        for pos, row in enumerate(rows):
            ts = as_utc(row.timestamp).replace(second=0, microsecond=0)
            elapsed = int((ts - start).total_seconds() // 60)
            if elapsed < 0:
                continue
            buckets[elapsed // 5].append(pos)

        bars: list[Bar5] = []
        for bucket in sorted(buckets):
            positions = buckets[bucket]
            expected_start = bucket * 5
            actual = [
                int((as_utc(rows[pos].timestamp).replace(second=0, microsecond=0) - start).total_seconds() // 60)
                for pos in positions
            ]
            if actual != list(range(expected_start, expected_start + 5)):
                continue
            selected = [rows[pos] for pos in positions]
            bars.append(
                Bar5(
                    start_pos=positions[0],
                    end_pos=positions[-1],
                    timestamp=as_utc(selected[-1].timestamp),
                    open=_finite(selected[0].open),
                    high=max(_finite(row.high) for row in selected),
                    low=min(_finite(row.low) for row in selected),
                    close=_finite(selected[-1].close),
                    volume=sum(_finite(row.volume) for row in selected),
                )
            )
        out.append(
            SessionTape(
                session_id=session_id,
                code=code,
                session_date=_session_date(session_id),
                start=start,
                candles=rows,
                bars=bars,
            )
        )
    return out


def _bar_range(bar: Bar5) -> float:
    return max(0.0, float(bar.high) - float(bar.low))


def _ema(values: list[float], length: int) -> list[float]:
    alpha = 2.0 / (length + 1.0)
    result: list[float] = []
    previous: Optional[float] = None
    for value in values:
        previous = float(value) if previous is None else alpha * float(value) + (1.0 - alpha) * previous
        result.append(previous)
    return result


def _atr(bars: list[Bar5], index: int, length: int = 14) -> Optional[float]:
    if index < length - 1:
        return None
    values: list[float] = []
    for pos in range(index - length + 1, index + 1):
        previous_close = bars[pos - 1].close if pos > 0 else bars[pos].open
        values.append(
            max(
                _bar_range(bars[pos]),
                abs(bars[pos].high - previous_close),
                abs(bars[pos].low - previous_close),
            )
        )
    return sum(values) / len(values) if values else None


def _vwap_stats(bars: list[Bar5], index: int) -> tuple[float, float]:
    selected = bars[: index + 1]
    total_volume = sum(max(1.0, bar.volume) for bar in selected)
    if total_volume <= 0:
        return selected[-1].close, 0.0
    typical = [(_finite(bar.high) + _finite(bar.low) + _finite(bar.close)) / 3.0 for bar in selected]
    weights = [max(1.0, _finite(bar.volume)) for bar in selected]
    vwap = sum(price * weight for price, weight in zip(typical, weights)) / total_volume
    variance = sum(weight * (price - vwap) ** 2 for price, weight in zip(typical, weights)) / total_volume
    return vwap, math.sqrt(max(0.0, variance))


def _regime(bars: list[Bar5], index: int) -> str:
    """Causal coarse state: trend_up/down, range, or transition."""

    if index < 20:
        return "transition"
    closes = [bar.close for bar in bars[: index + 1]]
    # Session-local 8/21 is deliberate: EURO/PRE are only 4h/2.5h, so a
    # 20/50 pair would leave most of those sessions permanently unclassified.
    # The thresholds below are fixed structural rules, not fitted to PnL.
    ema8 = _ema(closes, 8)[-1]
    ema21 = _ema(closes, 21)[-1]
    vwap, _ = _vwap_stats(bars, index)
    atr = _atr(bars, index)
    if atr is None or atr <= 0:
        return "transition"
    lookback = bars[max(0, index - 5): index + 1]
    high = max(bar.high for bar in lookback)
    low = min(bar.low for bar in lookback)
    efficiency = abs(lookback[-1].close - lookback[0].open) / max(high - low, 1e-9)
    old_vwap = _vwap_stats(bars, max(0, index - 5))[0]
    slope = (vwap - old_vwap) / atr
    if ema8 > ema21 and bars[index].close > vwap and slope >= 0.10 and efficiency >= 0.35:
        return "trend_up"
    if ema8 < ema21 and bars[index].close < vwap and slope <= -0.10 and efficiency >= 0.35:
        return "trend_down"
    if abs(slope) <= 0.10 and efficiency <= 0.35:
        return "range"
    return "transition"


def _round_price(price: float, tick: float) -> float:
    return round(float(price) / tick) * tick


def _candidate(
    *,
    tape: SessionTape,
    method: str,
    index: int,
    direction: int,
    stop: float,
    target: float,
    tick: float,
    reason: str,
) -> Optional[Candidate]:
    bar = tape.bars[index]
    entry_pos = bar.end_pos + 1
    if entry_pos >= len(tape.candles):
        return None
    entry = _finite(tape.candles[entry_pos].open)
    stop = _round_price(stop, tick)
    target = _round_price(target, tick)
    if direction > 0:
        if not stop < entry < target:
            return None
    else:
        if not target < entry < stop:
            return None
    risk_ticks = abs(entry - stop) / tick
    if risk_ticks < MIN_RISK_TICKS:
        return None
    return Candidate(
        method=method,
        session_id=tape.session_id,
        session_code=tape.code,
        signal_time=bar.timestamp,
        signal_index=index,
        entry_pos=entry_pos,
        direction=direction,
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        regime=_regime(tape.bars, index),
        reason=reason,
    )


def _method_candidates(tape: SessionTape, method: str, tick: float) -> list[Candidate]:
    bars = tape.bars
    if len(bars) < 22:
        return []
    closes = [bar.close for bar in bars]
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    out: list[Candidate] = []
    fired = False
    or_high = max(bar.high for bar in bars[:3]) if len(bars) >= 3 else None
    or_low = min(bar.low for bar in bars[:3]) if len(bars) >= 3 else None

    for i, current in enumerate(bars):
        if fired or i < 1:
            continue
        previous = bars[i - 1]
        atr = _atr(bars, i)
        if atr is None or atr <= 0:
            continue
        candidate: Optional[Candidate] = None

        if method == "ORB15":
            if i < 3 or or_high is None or or_low is None:
                continue
            if current.close > or_high:
                candidate = _candidate(
                    tape=tape, method=method, index=i, direction=1,
                    stop=or_low - STOP_BUFFER_TICKS * tick,
                    target=current.close + TARGET_R * (current.close - (or_low - STOP_BUFFER_TICKS * tick)),
                    tick=tick, reason="15m opening-range breakout",
                )
            elif current.close < or_low:
                candidate = _candidate(
                    tape=tape, method=method, index=i, direction=-1,
                    stop=or_high + STOP_BUFFER_TICKS * tick,
                    target=current.close - TARGET_R * ((or_high + STOP_BUFFER_TICKS * tick) - current.close),
                    tick=tick, reason="15m opening-range breakdown",
                )

        elif method == "VWAPREV":
            if i < 6:
                continue
            vwap, sigma = _vwap_stats(bars, i)
            if sigma <= 0:
                continue
            if current.low <= vwap - 2.0 * sigma and current.close > current.open and current.close > previous.close:
                candidate = _candidate(
                    tape=tape, method=method, index=i, direction=1,
                    stop=current.low - STOP_BUFFER_TICKS * tick,
                    target=vwap, tick=tick, reason="session VWAP lower-band rejection",
                )
            elif current.high >= vwap + 2.0 * sigma and current.close < current.open and current.close < previous.close:
                candidate = _candidate(
                    tape=tape, method=method, index=i, direction=-1,
                    stop=current.high + STOP_BUFFER_TICKS * tick,
                    target=vwap, tick=tick, reason="session VWAP upper-band rejection",
                )

        elif method == "DONCHIAN":
            if i < 20:
                continue
            window = bars[i - 20:i]
            prior_high = max(bar.high for bar in window)
            prior_low = min(bar.low for bar in window)
            if current.close > prior_high:
                stop = previous.low - STOP_BUFFER_TICKS * tick
                candidate = _candidate(
                    tape=tape, method=method, index=i, direction=1, stop=stop,
                    target=current.close + TARGET_R * (current.close - stop),
                    tick=tick, reason="20-bar channel breakout",
                )
            elif current.close < prior_low:
                stop = previous.high + STOP_BUFFER_TICKS * tick
                candidate = _candidate(
                    tape=tape, method=method, index=i, direction=-1, stop=stop,
                    target=current.close - TARGET_R * (stop - current.close),
                    tick=tick, reason="20-bar channel breakdown",
                )

        elif method == "VWAPPB":
            if i < 20:
                continue
            vwap, _ = _vwap_stats(bars, i)
            if ema20[i] > ema50[i] and current.close > vwap and current.low <= vwap + 0.25 * atr and current.close > current.open and current.close > previous.close:
                stop = current.low - STOP_BUFFER_TICKS * tick
                candidate = _candidate(
                    tape=tape, method=method, index=i, direction=1, stop=stop,
                    target=current.close + TARGET_R * (current.close - stop),
                    tick=tick, reason="EMA/VWAP trend pullback",
                )
            elif ema20[i] < ema50[i] and current.close < vwap and current.high >= vwap - 0.25 * atr and current.close < current.open and current.close < previous.close:
                stop = current.high + STOP_BUFFER_TICKS * tick
                candidate = _candidate(
                    tape=tape, method=method, index=i, direction=-1, stop=stop,
                    target=current.close - TARGET_R * (stop - current.close),
                    tick=tick, reason="EMA/VWAP trend pullback",
                )

        elif method == "LEVELREJ":
            # The prior same-code profile is injected by _build_candidates.
            continue

        if candidate is not None:
            out.append(candidate)
            fired = True
    return out


def _level_candidates(
    tape: SessionTape,
    previous: Optional[SessionTape],
    tick: float,
) -> list[Candidate]:
    if previous is None or len(tape.bars) < 3 or not previous.bars:
        return []
    prior_high = max(bar.high for bar in previous.bars)
    prior_low = min(bar.low for bar in previous.bars)
    out: list[Candidate] = []
    for i, current in enumerate(tape.bars[1:], 1):
        previous_bar = tape.bars[i - 1]
        if current.low <= prior_low and current.close > prior_low and current.close > current.open:
            candidate = _candidate(
                tape=tape, method="LEVELREJ", index=i, direction=1,
                stop=current.low - STOP_BUFFER_TICKS * tick,
                target=current.close + TARGET_R * (current.close - (current.low - STOP_BUFFER_TICKS * tick)),
                tick=tick, reason="prior same-session low rejection",
            )
            if candidate:
                out.append(candidate)
                break
        if current.high >= prior_high and current.close < prior_high and current.close < current.open:
            candidate = _candidate(
                tape=tape, method="LEVELREJ", index=i, direction=-1,
                stop=current.high + STOP_BUFFER_TICKS * tick,
                target=current.close - TARGET_R * ((current.high + STOP_BUFFER_TICKS * tick) - current.close),
                tick=tick, reason="prior same-session high rejection",
            )
            if candidate:
                out.append(candidate)
                break
        _ = previous_bar
    return out


def _build_candidates(sessions: list[SessionTape], tick: float) -> dict[str, list[Candidate]]:
    by_code: dict[str, list[SessionTape]] = defaultdict(list)
    for tape in sessions:
        by_code[tape.code].append(tape)
    for code in by_code:
        by_code[code].sort(key=lambda item: item.start)

    result: dict[str, list[Candidate]] = defaultdict(list)
    for code, tapes in by_code.items():
        previous: Optional[SessionTape] = None
        for tape in tapes:
            for method in METHODS:
                candidates = (
                    _level_candidates(tape, previous, tick)
                    if method == "LEVELREJ"
                    else _method_candidates(tape, method, tick)
                )
                result[tape.session_id].extend(candidates)
            previous = tape
    return result


def _adaptive_method(code: str, regime: str, elapsed_minutes: float) -> Optional[str]:
    """Fixed router; do not change after seeing study results."""

    # The first all-session pass showed that the overnight route consumed the
    # edge and the current MBO coverage only supports an MNQ RTH hypothesis.
    # Keep overnight as context-only until it survives a separate holdout.
    if code != "RTH":
        return None
    if regime in ("trend_up", "trend_down"):
        if code == "RTH" and elapsed_minutes <= 120:
            return "ORB15"
        return "DONCHIAN"
    if regime == "range":
        return "VWAPREV"
    return None


def _simulate(
    candidates: list[Candidate],
    tapes: dict[str, SessionTape],
    *,
    method_label: str,
    point_value: float,
    tick: float,
    costs: float,
) -> list[Trade]:
    by_session: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        by_session[candidate.session_id].append(candidate)
    trades: list[Trade] = []
    for session_id, rows in by_session.items():
        tape = tapes[session_id]
        rows = sorted(rows, key=lambda item: item.entry_pos)
        next_free = 0
        for candidate in rows:
            if candidate.entry_pos < next_free:
                continue
            entry_pos = candidate.entry_pos
            entry_time = as_utc(tape.candles[entry_pos].timestamp)
            direction = candidate.direction
            exit_pos = min(len(tape.candles) - 1, entry_pos + MAX_HOLD_MINUTES)
            exit_price = _finite(tape.candles[exit_pos].close)
            exit_reason = "session_end" if exit_pos == len(tape.candles) - 1 else "time"
            for pos in range(entry_pos, exit_pos + 1):
                row = tape.candles[pos]
                high = _finite(row.high)
                low = _finite(row.low)
                if direction > 0:
                    hit_sl = low <= candidate.stop_price
                    hit_tp = high >= candidate.target_price
                    if hit_sl:
                        exit_pos, exit_price, exit_reason = pos, candidate.stop_price, "sl"
                        break
                    if hit_tp:
                        exit_pos, exit_price, exit_reason = pos, candidate.target_price, "tp"
                        break
                else:
                    hit_sl = high >= candidate.stop_price
                    hit_tp = low <= candidate.target_price
                    if hit_sl:
                        exit_pos, exit_price, exit_reason = pos, candidate.stop_price, "sl"
                        break
                    if hit_tp:
                        exit_pos, exit_price, exit_reason = pos, candidate.target_price, "tp"
                        break
            gross = (exit_price - candidate.entry_price) * direction * point_value
            net = gross - costs
            stress = net - STRESS_TICKS * tick * point_value
            trades.append(
                Trade(
                    method=method_label,
                    session_id=session_id,
                    session_code=candidate.session_code,
                    signal_time=candidate.signal_time,
                    entry_time=entry_time,
                    exit_time=as_utc(tape.candles[exit_pos].timestamp),
                    direction=direction,
                    entry_price=candidate.entry_price,
                    exit_price=exit_price,
                    gross_pnl=gross,
                    costs=costs,
                    pnl=net,
                    stress_pnl=stress,
                    exit_reason=exit_reason,
                    regime=candidate.regime,
                    reason=candidate.reason,
                )
            )
            next_free = exit_pos + 1
    return sorted(trades, key=lambda item: item.entry_time)


def _stats(trades: Iterable[Trade], field: str = "pnl") -> dict[str, object]:
    values = [_finite(getattr(trade, field)) for trade in trades]
    gross_win = sum(value for value in values if value > 0)
    gross_loss = -sum(value for value in values if value < 0)
    equity = 0.0
    high_water = 0.0
    max_dd = 0.0
    for value in values:
        equity += value
        high_water = max(high_water, equity)
        max_dd = max(max_dd, high_water - equity)
    return {
        "n": len(values),
        "pnl": round(sum(values), 2),
        "pf": round(gross_win / gross_loss, 4) if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0),
        "win_rate": round(sum(value > 0 for value in values) / len(values), 4) if values else 0.0,
        "expectancy": round(sum(values) / len(values), 2) if values else 0.0,
        "max_dd": round(max_dd, 2),
    }


def _by_key(trades: list[Trade], key) -> dict[str, dict[str, object]]:
    grouped: dict[str, list[Trade]] = defaultdict(list)
    for trade in trades:
        grouped[str(key(trade))].append(trade)
    return {name: _stats(rows) for name, rows in sorted(grouped.items())}


def _segment_rows(trades: list[Trade]) -> dict[str, dict[str, object]]:
    if not trades:
        return {}
    dates = sorted(as_utc(trade.entry_time).date() for trade in trades)
    first = dates[0]
    last = dates[-1]
    span = max(1, (last - first).days + 1)
    segments: dict[str, list[Trade]] = defaultdict(list)
    for trade in trades:
        offset = (as_utc(trade.entry_time).date() - first).days / span
        label = "1/3" if offset < 1 / 3 else "2/3" if offset < 2 / 3 else "3/3"
        segments[label].append(trade)
    return {label: _stats(rows) for label, rows in sorted(segments.items())}


def _mc_loss_probability(trades: list[Trade], *, iterations: int = 500) -> float:
    if not trades:
        return 1.0
    import random

    rng = random.Random(MC_SEED)
    values = [_finite(trade.pnl) for trade in trades]
    losses = 0
    for _ in range(iterations):
        sample = [values[rng.randrange(len(values))] for _ in values]
        if sum(sample) <= 0:
            losses += 1
    return round(losses / iterations, 4)


def summarize(label: str, trades: list[Trade]) -> dict[str, object]:
    return {
        "label": label,
        "baseline": _stats(trades, "pnl"),
        "stress_14t": _stats(trades, "stress_pnl"),
        "by_session": _by_key(trades, lambda trade: trade.session_code),
        "by_regime": _by_key(trades, lambda trade: trade.regime),
        "by_year": _by_key(trades, lambda trade: as_utc(trade.entry_time).year),
        "segments": _segment_rows(trades),
        "mc_loss_probability": _mc_loss_probability(trades),
    }


def run_symbol(symbol: str) -> dict[str, object]:
    started = time.time()
    contract_id = current_quarterly_contract_id(symbol)
    tick = get_tick_size(contract_id)
    point_value = get_point_value(contract_id)
    costs = get_commission_rt(contract_id) + get_fees_rt(contract_id)
    candles = sorted(candle_store.load(symbol, 1), key=lambda item: as_utc(item.timestamp))
    sessions = [tape for tape in _build_sessions(candles) if len(tape.bars) >= 22]
    tapes = {tape.session_id: tape for tape in sessions}
    candidate_map = _build_candidates(sessions, tick)

    fixed: dict[str, list[Trade]] = {}
    for method in METHODS:
        selected = [candidate for rows in candidate_map.values() for candidate in rows if candidate.method == method]
        fixed[method] = _simulate(
            selected, tapes, method_label=method, point_value=point_value,
            tick=tick, costs=costs,
        )

    adaptive_candidates: list[Candidate] = []
    for session_id, rows in candidate_map.items():
        tape = tapes[session_id]
        for candidate in rows:
            elapsed = (candidate.signal_time - tape.start).total_seconds() / 60.0
            route = _adaptive_method(tape.code, candidate.regime, elapsed)
            if route == candidate.method:
                adaptive_candidates.append(candidate)
    adaptive = _simulate(
        adaptive_candidates, tapes, method_label="ADAPTIVE", point_value=point_value,
        tick=tick, costs=costs,
    )

    session_method_rows: list[dict[str, object]] = []
    for method, trades in fixed.items():
        by_session = _by_key(trades, lambda trade: trade.session_code)
        for code in SESSION_CODES:
            subset = [trade for trade in trades if trade.session_code == code]
            stats = by_session.get(code, _stats([]))
            stress = _stats(subset, "stress_pnl")
            session_method_rows.append({
                "method": method,
                "session": code,
                **stats,
                "stress_pnl": stress["pnl"],
                "stress_pf": stress["pf"],
            })

    regime_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for rows in candidate_map.values():
        for candidate in rows:
            regime_counts[candidate.session_code][candidate.regime] += 1

    payload = {
        "symbol": symbol,
        "created_at": datetime.now(UTC).isoformat(),
        "study": {
            "source": "canonical 1-minute candle store",
            "aggregation": "complete session-local 5-minute bars",
            "sessions": list(SESSION_CODES),
            "methods": list(METHODS),
            "adaptive_router": {
                "ASIA/EURO/PRE/AH": "context-only, no trade",
                "trend_up/trend_down": "ORB15 during early RTH, otherwise DONCHIAN",
                "range": "VWAPREV",
                "transition": "no trade",
            },
            "target_r": TARGET_R,
            "max_hold_minutes": MAX_HOLD_MINUTES,
            "stop_buffer_ticks": STOP_BUFFER_TICKS,
            "min_risk_ticks": MIN_RISK_TICKS,
            "costs_round_trip": costs,
            "stress_slippage_round_trip_ticks": STRESS_TICKS,
            "lookahead_policy": "signals use completed 5m bar; fill is next available 1m open; prior same-code level only",
        },
        "dataset": {
            "bars_1m": len(candles),
            "sessions_used": len(sessions),
            "first": as_utc(candles[0].timestamp).isoformat() if candles else None,
            "last": as_utc(candles[-1].timestamp).isoformat() if candles else None,
            "tick": tick,
            "point_value": point_value,
        },
        "regime_signal_counts": {code: dict(values) for code, values in sorted(regime_counts.items())},
        "results": {method: summarize(method, trades) for method, trades in fixed.items()},
        "results_adaptive": summarize("ADAPTIVE", adaptive),
        "session_method_rows": session_method_rows,
        "trades": {
            method: [trade.__dict__ for trade in trades]
            for method, trades in {**fixed, "ADAPTIVE": adaptive}.items()
        },
        "elapsed_seconds": round(time.time() - started, 2),
    }
    return payload


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _write_markdown(path: Path, payloads: dict[str, dict[str, object]]) -> None:
    lines = [
        "# Adaptive session/regime study",
        "",
        "Status: **offline research only; no production or live behaviour changed**",
        "",
        "The router was fixed before inspecting results: overnight sessions are context-only; within RTH, trend uses ORB15 early or DONCHIAN later, range uses VWAPREV, and transition is no-trade.",
        "",
        "## Overall results",
        "",
        "| Symbol | Method | Trades | PnL | PF | Stress 14t PF | Max DD | MC P(loss) |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for symbol, payload in payloads.items():
        for method, result in list((payload.get("results") or {}).items()) + [("ADAPTIVE", payload.get("results_adaptive") or {})]:
            base = result.get("baseline") or {}
            stress = result.get("stress_14t") or {}
            lines.append(
                f"| {symbol} | {method} | {base.get('n', 0)} | ${base.get('pnl', 0):,.2f} | {base.get('pf', 0):.2f} | {stress.get('pf', 0):.2f} | ${base.get('max_dd', 0):,.2f} | {result.get('mc_loss_probability', 1):.3f} |"
            )
    lines += ["", "## Fixed method by session", "", "| Symbol | Method | Session | Trades | PnL | PF | Stress PF |", "|---|---|---|---:|---:|---:|---:|"]
    for symbol, payload in payloads.items():
        for row in payload.get("session_method_rows") or []:
            lines.append(
                f"| {symbol} | {row.get('method')} | {row.get('session')} | {row.get('n', 0)} | ${row.get('pnl', 0):,.2f} | {row.get('pf', 0):.2f} | {row.get('stress_pf', 0):.2f} |"
            )
    lines += [
        "",
        "## Interpretation guardrails",
        "",
        "- A positive adaptive row is not a live recommendation; the router itself is a model choice and needs a locked future holdout.",
        "- MBO is not used here because the current complete MBO history is much shorter than the candle history. This study tests session/regime routing from OHLCV only.",
        "- Session-level rows with very few trades are descriptive only. A session with no candidate is not evidence of a losing strategy.",
        "- Results include canonical commission/fees. Stress subtracts a 14-tick round-trip budget.",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default=["MNQ", "MES"])
    parser.add_argument("--output", default="adaptive_session_regime_study_20260915.json")
    args = parser.parse_args()
    symbols = [str(value).upper() for value in args.symbols]
    if any(symbol not in {"MNQ", "MES"} for symbol in symbols):
        parser.error("--symbols supports MNQ and MES")
    payloads: dict[str, dict[str, object]] = {}
    for symbol in symbols:
        print(f"[{symbol}] loading and building session tapes...", flush=True)
        payload = run_symbol(symbol)
        payloads[symbol] = payload
        print(
            f"[{symbol}] {payload['dataset']['bars_1m']:,} 1m bars / {payload['dataset']['sessions_used']:,} sessions / {payload['elapsed_seconds']}s",
            flush=True,
        )
        for label, result in list((payload.get("results") or {}).items()) + [("ADAPTIVE", payload.get("results_adaptive") or {})]:
            base = result.get("baseline") or {}
            stress = result.get("stress_14t") or {}
            print(
                f"  {label:<9} n={base.get('n', 0):>5} PnL={base.get('pnl', 0):>9.0f} PF={base.get('pf', 0):>5.2f} stress={stress.get('pf', 0):>5.2f}",
                flush=True,
            )

    output = Path(args.output)
    if not output.is_absolute():
        output = market_data.derived_path("research", output.name)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"created_at": datetime.now(UTC).isoformat(), "symbols": symbols, "payloads": payloads}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    _write_csv(output.with_suffix(".csv"), [
        {"symbol": symbol, **row}
        for symbol, payload in payloads.items()
        for row in payload.get("session_method_rows") or []
    ])
    _write_markdown(output.with_suffix(".md"), payloads)
    print(f"JSON: {output}")
    print(f"CSV : {output.with_suffix('.csv')}")
    print(f"MD  : {output.with_suffix('.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
