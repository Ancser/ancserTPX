"""Causal RTH research harness for three prop-firm intraday ideas.

This module is deliberately research-only.  It does not register a production
``strategy_mode`` and cannot place an order.  The existing public-strategy lab
contains strategies named ORB and VWAPREV, but those are not faithful versions
of the rules evaluated here: its ORB starts from the first allowed engine bar
and VWAPREV is a bare deviation fade.  This harness instead pins the session to
09:30-16:00 America/New_York (including DST) and implements:

* Opening Range Breakout (close/body/volume/VWAP/range filters, breakout or
  retest entry, opposite-side or midpoint stop, scale-out and break-even);
* VWAP trend pullback (multi-bar trend and slope, bounded VWAP penetration,
  reversal candle, shrinking volume and RSI confirmation, structural trail);
* range-day VWAP mean reversion (small opening gap, flat VWAP, non-expanding
  Bollinger width, standard-deviation rejection and time stop).

Signals are evaluated on completed bars and filled at the next available 1m
open.  Only one position can be open at a time.  SL/TP ambiguity uses the same
shared OHLC heuristic as the production backtester; after a partial TP, any
same-minute ambiguity is resolved conservatively to the new stop.
"""

from __future__ import annotations

import csv
import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import fmean, pstdev
from typing import Iterable, Optional, Sequence, Union
from zoneinfo import ZoneInfo

from backend.backtest.intrabar import resolve_same_bar_exit
from backend.backtest.robustness import evaluate as evaluate_robustness
from backend.backtest.robustness import series_stats
from backend.db.models import (
    Candle,
    current_quarterly_contract_id,
    get_commission_rt,
    get_fees_rt,
    get_point_value,
    get_tick_size,
)


ET = ZoneInfo("America/New_York")
UTC = timezone.utc
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)
ENTRY_CUTOFF = time(15, 0)
FORCE_FLAT = time(15, 50)


@dataclass(frozen=True)
class SignalBar:
    """A completed, RTH-anchored N-minute bar."""

    start_index: int
    end_index: int
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float
    vwap_std: float
    rsi14: Optional[float]
    bb_mid: Optional[float]
    bb_std: Optional[float]
    bb_width: Optional[float]


@dataclass
class RthSession:
    """One New York RTH session and only information known up to each bar."""

    session_date: date
    bars: tuple[Candle, ...]
    et_times: tuple[datetime, ...]
    vwap: tuple[float, ...]
    vwap_std: tuple[float, ...]
    previous_rth_close: Optional[float]
    _signal_cache: dict[int, tuple[SignalBar, ...]] = field(
        default_factory=dict, repr=False
    )

    def signal_bars(self, minutes: int) -> tuple[SignalBar, ...]:
        minutes = max(1, int(minutes))
        cached = self._signal_cache.get(minutes)
        if cached is not None:
            return cached

        grouped: dict[int, list[int]] = defaultdict(list)
        for i, et_ts in enumerate(self.et_times):
            minute_of_rth = (et_ts.hour * 60 + et_ts.minute) - (9 * 60 + 30)
            if minute_of_rth >= 0:
                grouped[minute_of_rth // minutes].append(i)

        raw: list[tuple[int, int, float, float, float, float, float]] = []
        for bucket in sorted(grouped):
            indices = grouped[bucket]
            expected_start = bucket * minutes
            actual_minutes = [
                (self.et_times[i].hour * 60 + self.et_times[i].minute)
                - (9 * 60 + 30)
                for i in indices
            ]
            # A signal bar must be complete and consecutive.  A data gap must
            # not silently turn a 4-minute fragment into a "5m close".
            if actual_minutes != list(range(expected_start, expected_start + minutes)):
                continue
            bars = [self.bars[i] for i in indices]
            raw.append(
                (
                    indices[0],
                    indices[-1],
                    float(bars[0].open),
                    max(float(b.high) for b in bars),
                    min(float(b.low) for b in bars),
                    float(bars[-1].close),
                    sum(float(b.volume or 0.0) for b in bars),
                )
            )

        closes: list[float] = []
        out: list[SignalBar] = []
        for start_i, end_i, op, hi, lo, close, volume in raw:
            closes.append(close)
            rsi = _rolling_rsi(closes, 14)
            if len(closes) >= 20:
                window = closes[-20:]
                bb_mid = fmean(window)
                bb_std = pstdev(window)
                bb_width = 4.0 * bb_std
            else:
                bb_mid = bb_std = bb_width = None
            out.append(
                SignalBar(
                    start_index=start_i,
                    end_index=end_i,
                    timestamp=self.bars[end_i].timestamp,
                    open=op,
                    high=hi,
                    low=lo,
                    close=close,
                    volume=volume,
                    vwap=self.vwap[end_i],
                    vwap_std=self.vwap_std[end_i],
                    rsi14=rsi,
                    bb_mid=bb_mid,
                    bb_std=bb_std,
                    bb_width=bb_width,
                )
            )
        self._signal_cache[minutes] = tuple(out)
        return self._signal_cache[minutes]


@dataclass(frozen=True)
class DatasetInfo:
    symbol: str
    total_bars: int
    first_timestamp: Optional[datetime]
    last_timestamp: Optional[datetime]
    source_counts: dict[str, int]
    rth_sessions: int
    skipped_short_sessions: int
    start_date: Optional[str]
    end_date: Optional[str]


@dataclass(frozen=True)
class SymbolRules:
    opening_width_min: float
    opening_width_max: float
    pullback_penetration_tight: float
    pullback_penetration_wide: float
    pullback_stop_tight: float
    pullback_stop_wide: float
    mean_stop_tight: float
    mean_stop_wide: float


SYMBOL_RULES = {
    "MNQ": SymbolRules(15.0, 60.0, 3.0, 8.0, 8.0, 15.0, 4.0, 8.0),
    "MES": SymbolRules(8.0, 30.0, 1.5, 4.0, 4.0, 8.0, 2.0, 4.0),
}


@dataclass(frozen=True)
class OrbConfig:
    name: str
    opening_minutes: int = 15
    confirm_minutes: int = 5
    volume_multiple: float = 1.2
    opening_width_min: Optional[float] = None
    opening_width_max: Optional[float] = None
    entry_mode: str = "breakout"  # breakout | retest
    stop_mode: str = "opposite"  # opposite | midpoint
    tp2_multiple: float = 2.0
    retest_timeout_minutes: int = 30
    retest_tolerance_ticks: int = 2
    stop_buffer_ticks: int = 2
    max_hold_minutes: int = 120
    risk_dollars: float = 200.0
    max_trades_per_day: int = 2
    news_buffer_minutes: int = 10


@dataclass(frozen=True)
class VwapPullbackConfig:
    name: str
    confirm_minutes: int
    penetration_points: float
    stop_buffer_points: float
    tp2_mode: str = "vwap_band"  # vwap_band | day_extreme
    trend_bars: int = 3
    volume_lookback: int = 5
    rsi_low: float = 30.0
    rsi_high: float = 70.0
    max_hold_minutes: int = 90
    risk_dollars: float = 200.0
    max_trades_per_day: int = 2
    news_buffer_minutes: int = 10


@dataclass(frozen=True)
class MeanReversionConfig:
    name: str
    confirm_minutes: int = 5
    entry_sigma: float = 2.0
    stop_buffer_points: float = 4.0
    target_mode: str = "vwap"  # vwap | opposite_band
    max_gap_pct: float = 0.003
    vwap_flat_lookback_bars: int = 6
    vwap_flat_max_pct: float = 0.0003
    bb_expansion_lookback_bars: int = 3
    bb_expansion_max_ratio: float = 1.10
    max_hold_minutes: int = 45
    risk_dollars: float = 200.0
    max_trades_per_day: int = 2
    news_buffer_minutes: int = 10


ResearchConfig = Union[OrbConfig, VwapPullbackConfig, MeanReversionConfig]


@dataclass(frozen=True)
class EntryCandidate:
    strategy: str
    variant: str
    direction: int  # +1 long, -1 short
    signal_index: int
    signal_time: datetime
    stop_price: float
    target1_kind: str  # points | risk | absolute
    target1_value: float
    target2_kind: Optional[str] = None
    target2_value: Optional[float] = None
    target1_fraction: float = 0.50
    move_stop_to_breakeven: bool = True
    structural_trail_minutes: int = 0
    trail_buffer_ticks: int = 2
    vwap_invalidation_buffer: Optional[float] = None
    max_hold_minutes: int = 0
    risk_dollars: float = 200.0
    reason: str = ""
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ResearchTrade:
    symbol: str
    strategy: str
    variant: str
    direction: str
    signal_time: datetime
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    initial_stop: float
    final_stop: float
    contracts: int
    risk_points: float
    planned_risk_dollars: float
    gross_pnl: float
    costs: float
    pnl: float
    exit_reason: str
    tp1_hit: bool
    reason: str

    def robustness_row(self) -> dict:
        return {
            "entry_time": self.entry_time,
            "pnl": self.pnl,
            "size": self.contracts,
            "symbol": self.symbol,
        }


@dataclass(frozen=True)
class RunResult:
    symbol: str
    strategy: str
    variant: str
    config: dict
    trades: tuple[ResearchTrade, ...]
    diagnostics: dict
    summary: dict


def _as_utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


def _as_et(ts: datetime) -> datetime:
    return _as_utc(ts).astimezone(ET)


def _rolling_rsi(closes: Sequence[float], length: int = 14) -> Optional[float]:
    """Simple rolling RSI on completed closes, with no cross-session state."""

    if len(closes) < length + 1:
        return None
    changes = [closes[i] - closes[i - 1] for i in range(len(closes) - length, len(closes))]
    gains = sum(max(0.0, change) for change in changes)
    losses = sum(max(0.0, -change) for change in changes)
    if losses <= 0:
        return 100.0
    if gains <= 0:
        return 0.0
    rs = gains / losses
    return 100.0 - 100.0 / (1.0 + rs)


def _session_from_bars(
    session_date: date,
    bars: Sequence[Candle],
    previous_rth_close: Optional[float],
) -> RthSession:
    ordered = tuple(sorted(bars, key=lambda bar: bar.timestamp))
    et_times = tuple(_as_et(bar.timestamp) for bar in ordered)
    means: list[float] = []
    stds: list[float] = []
    total_weight = mean = m2 = 0.0
    for bar in ordered:
        typical = (float(bar.high) + float(bar.low) + float(bar.close)) / 3.0
        weight = max(1.0, float(bar.volume or 0.0))
        new_weight = total_weight + weight
        delta = typical - mean
        mean += delta * weight / new_weight
        m2 += weight * delta * (typical - mean)
        total_weight = new_weight
        means.append(mean)
        stds.append(math.sqrt(max(0.0, m2 / total_weight)))
    return RthSession(
        session_date=session_date,
        bars=ordered,
        et_times=et_times,
        vwap=tuple(means),
        vwap_std=tuple(stds),
        previous_rth_close=previous_rth_close,
    )


def build_rth_sessions(
    candles: Iterable[Candle],
    *,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    require_flatten_bar: bool = True,
) -> tuple[list[RthSession], int]:
    """Group UTC candles into DST-correct New York RTH sessions.

    Returns ``(sessions, skipped_short_sessions)``.  Normal research skips
    early-close/incomplete days that do not reach 15:50 ET because the tested
    rule explicitly requires that flatten point.  Unit tests may disable this.
    """

    grouped: dict[date, list[Candle]] = defaultdict(list)
    for candle in candles:
        et_ts = _as_et(candle.timestamp)
        day = et_ts.date()
        if start_date and day < start_date:
            continue
        if end_date and day > end_date:
            continue
        if et_ts.weekday() >= 5:
            continue
        if RTH_OPEN <= et_ts.time().replace(tzinfo=None) < RTH_CLOSE:
            grouped[day].append(candle)

    sessions: list[RthSession] = []
    skipped = 0
    previous_close: Optional[float] = None
    for day in sorted(grouped):
        day_bars = sorted(grouped[day], key=lambda bar: bar.timestamp)
        if not day_bars:
            continue
        et_times = [_as_et(bar.timestamp) for bar in day_bars]
        has_open = any(ts.time().replace(tzinfo=None) == RTH_OPEN for ts in et_times)
        has_flatten = any(ts.time().replace(tzinfo=None) >= FORCE_FLAT for ts in et_times)
        session = _session_from_bars(day, day_bars, previous_close)
        previous_close = float(day_bars[-1].close)
        if not has_open or (require_flatten_bar and not has_flatten):
            skipped += 1
            continue
        sessions.append(session)
    return sessions, skipped


def load_symbol_sessions(
    symbol: str,
    *,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> tuple[list[RthSession], DatasetInfo]:
    """Load the current candle store once and retain only RTH bars."""

    from backend.data import candle_store

    symbol = str(symbol).upper()
    bars = candle_store.load(symbol, 1)
    if any(bars[i].timestamp > bars[i + 1].timestamp for i in range(len(bars) - 1)):
        bars.sort(key=lambda bar: bar.timestamp)
    sessions, skipped = build_rth_sessions(
        bars, start_date=start_date, end_date=end_date, require_flatten_bar=True
    )
    info = DatasetInfo(
        symbol=symbol,
        total_bars=len(bars),
        first_timestamp=bars[0].timestamp if bars else None,
        last_timestamp=bars[-1].timestamp if bars else None,
        source_counts=dict(Counter(getattr(bar, "source", "topstepx") for bar in bars)),
        rth_sessions=len(sessions),
        skipped_short_sessions=skipped,
        start_date=start_date.isoformat() if start_date else None,
        end_date=end_date.isoformat() if end_date else None,
    )
    return sessions, info


def load_news_events(path: Optional[Union[str, Path]]) -> tuple[datetime, ...]:
    """Load optional historical news times from a small audit CSV.

    The CSV must contain ``timestamp_et`` or ``timestamp``.  Zoned ISO values
    are preferred; naive values are explicitly interpreted in New York time.
    No synthetic recurring times are invented when the file is absent.
    """

    if not path:
        return ()
    source = Path(path)
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not ({"timestamp_et", "timestamp"} & set(reader.fieldnames)):
            raise ValueError("news CSV needs a timestamp_et or timestamp column")
        out: list[datetime] = []
        for row in reader:
            raw = str(row.get("timestamp_et") or row.get("timestamp") or "").strip()
            if not raw:
                continue
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=ET)
            out.append(parsed.astimezone(ET))
    return tuple(sorted(out))


def _news_index(events: Sequence[datetime]) -> dict[date, tuple[datetime, ...]]:
    grouped: dict[date, list[datetime]] = defaultdict(list)
    for event in events:
        et_event = event.astimezone(ET) if event.tzinfo else event.replace(tzinfo=ET)
        grouped[et_event.date()].append(et_event)
    return {day: tuple(sorted(rows)) for day, rows in grouped.items()}


def is_news_blocked(
    entry_time: datetime,
    events_by_day: dict[date, tuple[datetime, ...]],
    buffer_minutes: int,
) -> bool:
    et_entry = _as_et(entry_time)
    radius = abs(int(buffer_minutes)) * 60
    return any(
        abs((event - et_entry).total_seconds()) <= radius
        for event in events_by_day.get(et_entry.date(), ())
    )


def _round_tick(price: float, tick_size: float) -> float:
    return round(float(price) / tick_size) * tick_size


def _entry_index(session: RthSession, signal_index: int) -> Optional[int]:
    idx = signal_index + 1
    if idx >= len(session.bars):
        return None
    # A missing block cannot be treated as a normal next-minute market fill.
    gap = session.bars[idx].timestamp - session.bars[signal_index].timestamp
    if gap > timedelta(minutes=2):
        return None
    if session.et_times[idx].time().replace(tzinfo=None) >= ENTRY_CUTOFF:
        return None
    return idx


def _bullish_reversal(current: SignalBar, previous: Optional[SignalBar]) -> bool:
    body = abs(current.close - current.open)
    body_floor = max(body, 1e-9)
    lower_wick = min(current.open, current.close) - current.low
    upper_wick = current.high - max(current.open, current.close)
    hammer = current.close > current.open and lower_wick >= 2.0 * body_floor and upper_wick <= body_floor
    engulf = bool(
        previous
        and previous.close < previous.open
        and current.close > current.open
        and current.open <= previous.close
        and current.close >= previous.open
    )
    return hammer or engulf


def _bearish_reversal(current: SignalBar, previous: Optional[SignalBar]) -> bool:
    body = abs(current.close - current.open)
    body_floor = max(body, 1e-9)
    lower_wick = min(current.open, current.close) - current.low
    upper_wick = current.high - max(current.open, current.close)
    shooting_star = current.close < current.open and upper_wick >= 2.0 * body_floor and lower_wick <= body_floor
    engulf = bool(
        previous
        and previous.close > previous.open
        and current.close < current.open
        and current.open >= previous.close
        and current.close <= previous.open
    )
    return shooting_star or engulf


def _candidate_or_news(
    candidate: EntryCandidate,
    session: RthSession,
    events_by_day: dict[date, tuple[datetime, ...]],
    buffer_minutes: int,
) -> tuple[Optional[EntryCandidate], bool]:
    entry_idx = _entry_index(session, candidate.signal_index)
    if entry_idx is None:
        return None, False
    if is_news_blocked(session.bars[entry_idx].timestamp, events_by_day, buffer_minutes):
        return None, True
    return candidate, False


def generate_orb_candidates(
    session: RthSession,
    config: OrbConfig,
    symbol: str,
    *,
    news_events: Sequence[datetime] = (),
) -> tuple[list[EntryCandidate], int]:
    symbol = symbol.upper()
    rules = SYMBOL_RULES[symbol]
    tick = get_tick_size(current_quarterly_contract_id(symbol))
    open_cutoff = datetime.combine(session.session_date, RTH_OPEN, tzinfo=ET) + timedelta(
        minutes=config.opening_minutes
    )
    opening_indices = [
        i for i, ts in enumerate(session.et_times) if ts < open_cutoff
    ]
    minute_keys = {
        (session.et_times[i].hour * 60 + session.et_times[i].minute) - (9 * 60 + 30)
        for i in opening_indices
    }
    if minute_keys != set(range(config.opening_minutes)):
        return [], 0
    or_high = max(float(session.bars[i].high) for i in opening_indices)
    or_low = min(float(session.bars[i].low) for i in opening_indices)
    width = or_high - or_low
    width_min = (
        rules.opening_width_min
        if config.opening_width_min is None
        else float(config.opening_width_min)
    )
    width_max = (
        rules.opening_width_max
        if config.opening_width_max is None
        else float(config.opening_width_max)
    )
    if not (width_min <= width <= width_max):
        return [], 0

    midpoint = (or_high + or_low) / 2.0
    signal_bars = session.signal_bars(config.confirm_minutes)
    events_by_day = _news_index(news_events)
    candidates: list[EntryCandidate] = []
    news_blocked = 0
    done: set[int] = set()
    pending_retest: dict[int, tuple[datetime, float]] = {}

    for pos, current in enumerate(signal_bars):
        start_et = session.et_times[current.start_index]
        if start_et < open_cutoff:
            continue
        if _entry_index(session, current.end_index) is None:
            continue

        previous_rows = signal_bars[max(0, pos - 5):pos]
        avg_volume = fmean(row.volume for row in previous_rows) if len(previous_rows) == 5 else None
        volume_ok = bool(avg_volume and current.volume > avg_volume * config.volume_multiple)
        bullish = current.close > current.open
        bearish = current.close < current.open
        long_break = bullish and current.close > or_high and current.close > current.vwap and volume_ok
        short_break = bearish and current.close < or_low and current.close < current.vwap and volume_ok

        if config.entry_mode == "retest":
            for direction, (deadline, boundary) in list(pending_retest.items()):
                if start_et > deadline:
                    pending_retest.pop(direction, None)
                    done.add(direction)
                    continue
                tolerance = config.retest_tolerance_ticks * tick
                if direction > 0:
                    retest = (
                        current.low <= boundary + tolerance
                        and current.low >= boundary - tolerance
                        and current.close > boundary
                        and current.close > current.open
                        and current.close > current.vwap
                    )
                else:
                    retest = (
                        current.high >= boundary - tolerance
                        and current.high <= boundary + tolerance
                        and current.close < boundary
                        and current.close < current.open
                        and current.close < current.vwap
                    )
                if not retest:
                    continue
                stop = (
                    or_low - config.stop_buffer_ticks * tick
                    if direction > 0 and config.stop_mode == "opposite"
                    else or_high + config.stop_buffer_ticks * tick
                    if direction < 0 and config.stop_mode == "opposite"
                    else midpoint
                )
                candidate = EntryCandidate(
                    strategy="ORB",
                    variant=config.name,
                    direction=direction,
                    signal_index=current.end_index,
                    signal_time=current.timestamp,
                    stop_price=_round_tick(stop, tick),
                    target1_kind="points",
                    target1_value=width,
                    target2_kind="points",
                    target2_value=width * config.tp2_multiple,
                    max_hold_minutes=config.max_hold_minutes,
                    risk_dollars=config.risk_dollars,
                    reason=f"OR{config.opening_minutes} retest; width={width:.2f}",
                    meta={"or_high": or_high, "or_low": or_low, "width": width},
                )
                accepted, blocked = _candidate_or_news(
                    candidate, session, events_by_day, config.news_buffer_minutes
                )
                news_blocked += int(blocked)
                if accepted:
                    candidates.append(accepted)
                pending_retest.pop(direction, None)
                done.add(direction)

        for direction, crossed, boundary in (
            (1, long_break, or_high),
            (-1, short_break, or_low),
        ):
            if not crossed or direction in done or direction in pending_retest:
                continue
            if config.entry_mode == "retest":
                pending_retest[direction] = (
                    start_et + timedelta(minutes=config.retest_timeout_minutes),
                    boundary,
                )
                continue
            stop = (
                or_low - config.stop_buffer_ticks * tick
                if direction > 0 and config.stop_mode == "opposite"
                else or_high + config.stop_buffer_ticks * tick
                if direction < 0 and config.stop_mode == "opposite"
                else midpoint
            )
            candidate = EntryCandidate(
                strategy="ORB",
                variant=config.name,
                direction=direction,
                signal_index=current.end_index,
                signal_time=current.timestamp,
                stop_price=_round_tick(stop, tick),
                target1_kind="points",
                target1_value=width,
                target2_kind="points",
                target2_value=width * config.tp2_multiple,
                max_hold_minutes=config.max_hold_minutes,
                risk_dollars=config.risk_dollars,
                reason=f"OR{config.opening_minutes} breakout; vol>{config.volume_multiple:.1f}x; width={width:.2f}",
                meta={"or_high": or_high, "or_low": or_low, "width": width},
            )
            accepted, blocked = _candidate_or_news(
                candidate, session, events_by_day, config.news_buffer_minutes
            )
            news_blocked += int(blocked)
            if accepted:
                candidates.append(accepted)
            done.add(direction)
    return sorted(candidates, key=lambda row: row.signal_index), news_blocked


def generate_vwap_pullback_candidates(
    session: RthSession,
    config: VwapPullbackConfig,
    symbol: str,
    *,
    news_events: Sequence[datetime] = (),
) -> tuple[list[EntryCandidate], int]:
    symbol = symbol.upper()
    tick = get_tick_size(current_quarterly_contract_id(symbol))
    signal_bars = session.signal_bars(config.confirm_minutes)
    events_by_day = _news_index(news_events)
    candidates: list[EntryCandidate] = []
    news_blocked = 0

    warmup = max(config.trend_bars, config.volume_lookback, 15)
    for pos in range(warmup, len(signal_bars)):
        current = signal_bars[pos]
        previous = signal_bars[pos - 1]
        if _entry_index(session, current.end_index) is None:
            continue
        trend = signal_bars[pos - config.trend_bars + 1:pos + 1]
        volume_rows = signal_bars[pos - config.volume_lookback:pos]
        avg_volume = fmean(row.volume for row in volume_rows)
        volume_shrunk = current.volume < avg_volume
        rsi_ok = current.rsi14 is not None and config.rsi_low <= current.rsi14 <= config.rsi_high
        if not (volume_shrunk and rsi_ok):
            continue

        long_trend = all(row.close > row.vwap for row in trend) and trend[-1].vwap > trend[0].vwap
        short_trend = all(row.close < row.vwap for row in trend) and trend[-1].vwap < trend[0].vwap
        long_touch = current.low <= current.vwap and current.low >= current.vwap - config.penetration_points
        short_touch = current.high >= current.vwap and current.high <= current.vwap + config.penetration_points
        long_reversal = _bullish_reversal(current, previous) or (
            current.close > current.vwap and current.close > current.open
        )
        short_reversal = _bearish_reversal(current, previous) or (
            current.close < current.vwap and current.close < current.open
        )

        direction = 1 if long_trend and long_touch and long_reversal else -1 if short_trend and short_touch and short_reversal else 0
        if not direction:
            continue
        seen = session.bars[:current.end_index + 1]
        if direction > 0:
            stop = min(current.vwap - config.stop_buffer_points, current.low - 2 * tick)
            day_extreme = max(float(bar.high) for bar in seen)
            band_target = current.vwap + current.vwap_std
        else:
            stop = max(current.vwap + config.stop_buffer_points, current.high + 2 * tick)
            day_extreme = min(float(bar.low) for bar in seen)
            band_target = current.vwap - current.vwap_std
        target2 = band_target if config.tp2_mode == "vwap_band" else day_extreme
        candidate = EntryCandidate(
            strategy="VWAP_PULLBACK",
            variant=config.name,
            direction=direction,
            signal_index=current.end_index,
            signal_time=current.timestamp,
            stop_price=_round_tick(stop, tick),
            target1_kind="risk",
            target1_value=1.0,
            target2_kind="absolute_or_1_5r",
            target2_value=_round_tick(target2, tick),
            target1_fraction=0.50,
            move_stop_to_breakeven=True,
            structural_trail_minutes=config.confirm_minutes,
            trail_buffer_ticks=2,
            vwap_invalidation_buffer=config.stop_buffer_points,
            max_hold_minutes=config.max_hold_minutes,
            risk_dollars=config.risk_dollars,
            reason=(
                f"{config.confirm_minutes}m VWAP pullback; penetration<="
                f"{config.penetration_points:g}; RSI={current.rsi14:.1f}"
            ),
            meta={"signal_vwap": current.vwap, "signal_std": current.vwap_std},
        )
        accepted, blocked = _candidate_or_news(
            candidate, session, events_by_day, config.news_buffer_minutes
        )
        news_blocked += int(blocked)
        if accepted:
            candidates.append(accepted)
    return candidates, news_blocked


def generate_mean_reversion_candidates(
    session: RthSession,
    config: MeanReversionConfig,
    symbol: str,
    *,
    news_events: Sequence[datetime] = (),
) -> tuple[list[EntryCandidate], int]:
    symbol = symbol.upper()
    if not session.previous_rth_close or session.previous_rth_close <= 0:
        return [], 0
    opening_price = float(session.bars[0].open)
    gap_pct = abs(opening_price - session.previous_rth_close) / session.previous_rth_close
    if gap_pct >= config.max_gap_pct:
        return [], 0

    tick = get_tick_size(current_quarterly_contract_id(symbol))
    signal_bars = session.signal_bars(config.confirm_minutes)
    events_by_day = _news_index(news_events)
    candidates: list[EntryCandidate] = []
    news_blocked = 0
    warmup = max(20, config.vwap_flat_lookback_bars, config.bb_expansion_lookback_bars) + 1

    for pos in range(warmup, len(signal_bars)):
        current = signal_bars[pos]
        previous = signal_bars[pos - 1]
        if _entry_index(session, current.end_index) is None:
            continue
        earlier = signal_bars[pos - config.vwap_flat_lookback_bars]
        if current.vwap <= 0:
            continue
        vwap_drift = abs(current.vwap - earlier.vwap) / current.vwap
        if vwap_drift > config.vwap_flat_max_pct:
            continue
        width_then = signal_bars[pos - config.bb_expansion_lookback_bars].bb_width
        if current.bb_width is None or width_then is None or width_then <= 0:
            continue
        if current.bb_width > width_then * config.bb_expansion_max_ratio:
            continue
        if current.vwap_std <= 0:
            continue

        lower = current.vwap - config.entry_sigma * current.vwap_std
        upper = current.vwap + config.entry_sigma * current.vwap_std
        long_signal = (
            current.low <= lower
            and current.close > lower
            and current.close < current.vwap
            and _bullish_reversal(current, previous)
        )
        short_signal = (
            current.high >= upper
            and current.close < upper
            and current.close > current.vwap
            and _bearish_reversal(current, previous)
        )
        direction = 1 if long_signal else -1 if short_signal else 0
        if not direction:
            continue

        seen = session.bars[:current.end_index + 1]
        if direction > 0:
            stop = min(
                current.vwap - 2.0 * current.vwap_std,
                min(float(bar.low) for bar in seen) - config.stop_buffer_points,
            )
            opposite = current.vwap + config.entry_sigma * current.vwap_std
        else:
            stop = max(
                current.vwap + 2.0 * current.vwap_std,
                max(float(bar.high) for bar in seen) + config.stop_buffer_points,
            )
            opposite = current.vwap - config.entry_sigma * current.vwap_std

        extended = config.target_mode == "opposite_band"
        candidate = EntryCandidate(
            strategy="MEAN_REVERSION",
            variant=config.name,
            direction=direction,
            signal_index=current.end_index,
            signal_time=current.timestamp,
            stop_price=_round_tick(stop, tick),
            target1_kind="absolute",
            target1_value=_round_tick(current.vwap, tick),
            target2_kind="absolute" if extended else None,
            target2_value=_round_tick(opposite, tick) if extended else None,
            target1_fraction=0.75 if extended else 1.0,
            move_stop_to_breakeven=extended,
            max_hold_minutes=config.max_hold_minutes,
            risk_dollars=config.risk_dollars,
            reason=(
                f"range day {config.entry_sigma:g}sigma rejection; gap={gap_pct:.3%}; "
                f"vwap_drift={vwap_drift:.3%}"
            ),
            meta={"signal_vwap": current.vwap, "signal_std": current.vwap_std},
        )
        accepted, blocked = _candidate_or_news(
            candidate, session, events_by_day, config.news_buffer_minutes
        )
        news_blocked += int(blocked)
        if accepted:
            candidates.append(accepted)
    return candidates, news_blocked


def _resolve_target(
    kind: Optional[str],
    value: Optional[float],
    *,
    entry: float,
    risk: float,
    direction: int,
) -> Optional[float]:
    if kind is None or value is None:
        return None
    if kind == "points":
        return entry + direction * float(value)
    if kind == "risk":
        return entry + direction * risk * float(value)
    if kind == "absolute":
        return float(value)
    if kind == "absolute_or_1_5r":
        absolute = float(value)
        fallback = entry + direction * risk * 1.5
        return max(absolute, fallback) if direction > 0 else min(absolute, fallback)
    raise ValueError(f"unknown target kind: {kind}")


def _level_hit(bar: Candle, direction: int, stop: float, target: float) -> str:
    if direction > 0:
        hit_stop = float(bar.low) <= stop
        hit_target = float(bar.high) >= target
    else:
        hit_stop = float(bar.high) >= stop
        hit_target = float(bar.low) <= target
    if hit_stop and hit_target:
        return resolve_same_bar_exit(float(bar.open), stop, target)
    if hit_stop:
        return "sl"
    if hit_target:
        return "tp"
    return ""


def _stop_fill(bar: Candle, direction: int, stop: float) -> float:
    if direction > 0 and float(bar.open) < stop:
        return float(bar.open)
    if direction < 0 and float(bar.open) > stop:
        return float(bar.open)
    return stop


def _close_part(
    fills: list[tuple[int, float, str]],
    quantity: int,
    price: float,
    reason: str,
) -> None:
    if quantity > 0:
        fills.append((quantity, float(price), reason))


def simulate_candidate(
    session: RthSession,
    candidate: EntryCandidate,
    symbol: str,
) -> tuple[Optional[ResearchTrade], int, str]:
    """Simulate one candidate; return trade, exit index, or a skip reason."""

    symbol = symbol.upper()
    contract_id = current_quarterly_contract_id(symbol)
    tick = get_tick_size(contract_id)
    point_value = get_point_value(contract_id)
    entry_idx = _entry_index(session, candidate.signal_index)
    if entry_idx is None:
        return None, candidate.signal_index, "no_causal_entry"
    entry_bar = session.bars[entry_idx]
    entry = _round_tick(float(entry_bar.open), tick)
    stop = _round_tick(candidate.stop_price, tick)
    direction = candidate.direction
    if (direction > 0 and stop >= entry) or (direction < 0 and stop <= entry):
        return None, candidate.signal_index, "gapped_through_stop"
    risk = abs(entry - stop)
    contracts = int(candidate.risk_dollars // (risk * point_value))
    if contracts < 1:
        return None, candidate.signal_index, "risk_too_large"

    target1 = _resolve_target(
        candidate.target1_kind,
        candidate.target1_value,
        entry=entry,
        risk=risk,
        direction=direction,
    )
    target2 = _resolve_target(
        candidate.target2_kind,
        candidate.target2_value,
        entry=entry,
        risk=risk,
        direction=direction,
    )
    if target1 is None:
        return None, candidate.signal_index, "missing_target"
    target1 = _round_tick(target1, tick)
    target2 = _round_tick(target2, tick) if target2 is not None else None
    if (direction > 0 and target1 <= entry) or (direction < 0 and target1 >= entry):
        return None, candidate.signal_index, "gapped_through_target"
    if target2 is not None and (
        (direction > 0 and target2 <= target1) or (direction < 0 and target2 >= target1)
    ):
        target2 = _round_tick(entry + direction * max(risk * 1.5, abs(target1 - entry) * 1.25), tick)

    if target2 is None or contracts == 1 or candidate.target1_fraction >= 1.0:
        first_qty = contracts
    else:
        first_qty = max(
            1,
            min(contracts - 1, int(math.floor(contracts * candidate.target1_fraction + 0.5))),
        )
    remaining = contracts
    active_stop = stop
    active_target = target1
    first_stage = True
    fills: list[tuple[int, float, str]] = []
    tp1_hit = False
    pending_market_exit: Optional[str] = None
    exit_idx = len(session.bars) - 1

    signal_by_end = {
        bar.end_index: bar
        for bar in session.signal_bars(candidate.structural_trail_minutes)
    } if candidate.structural_trail_minutes else {}

    for idx in range(entry_idx, len(session.bars)):
        bar = session.bars[idx]
        et_time = session.et_times[idx].time().replace(tzinfo=None)
        elapsed = (bar.timestamp - entry_bar.timestamp).total_seconds() / 60.0
        if pending_market_exit:
            _close_part(fills, remaining, float(bar.open), pending_market_exit)
            remaining = 0
            exit_idx = idx
            break
        if idx > entry_idx and et_time >= FORCE_FLAT:
            _close_part(fills, remaining, float(bar.open), "force_flat")
            remaining = 0
            exit_idx = idx
            break
        if idx > entry_idx and candidate.max_hold_minutes and elapsed >= candidate.max_hold_minutes:
            _close_part(fills, remaining, float(bar.open), "time_stop")
            remaining = 0
            exit_idx = idx
            break

        outcome = _level_hit(bar, direction, active_stop, active_target)
        if outcome == "sl":
            reason = "breakeven" if tp1_hit and active_stop == entry else "sl"
            _close_part(fills, remaining, _stop_fill(bar, direction, active_stop), reason)
            remaining = 0
            exit_idx = idx
            break
        if outcome == "tp":
            quantity = first_qty if first_stage else remaining
            _close_part(fills, quantity, active_target, "tp1" if first_stage else "tp2")
            remaining -= quantity
            if remaining <= 0:
                exit_idx = idx
                break
            tp1_hit = True
            first_stage = False
            if candidate.move_stop_to_breakeven:
                active_stop = entry
            active_target = float(target2)
            # OHLC has no post-TP1 path.  If both the new stop and TP2 sit
            # inside this minute, take the stop: partial management must not
            # manufacture an optimistic sequence from unknown ticks.
            stop_touched = float(bar.low) <= active_stop if direction > 0 else float(bar.high) >= active_stop
            target_touched = float(bar.high) >= active_target if direction > 0 else float(bar.low) <= active_target
            if stop_touched:
                _close_part(fills, remaining, _stop_fill(bar, direction, active_stop), "breakeven")
                remaining = 0
                exit_idx = idx
                break
            if target_touched:
                _close_part(fills, remaining, active_target, "tp2")
                remaining = 0
                exit_idx = idx
                break

        if remaining <= 0:
            break

        completed = signal_by_end.get(idx)
        if tp1_hit and completed is not None:
            if direction > 0:
                proposed = _round_tick(completed.low - candidate.trail_buffer_ticks * tick, tick)
                if active_stop < proposed < float(completed.close):
                    active_stop = proposed
            else:
                proposed = _round_tick(completed.high + candidate.trail_buffer_ticks * tick, tick)
                if float(completed.close) < proposed < active_stop:
                    active_stop = proposed

        if candidate.vwap_invalidation_buffer is not None:
            buffer = candidate.vwap_invalidation_buffer
            invalid = (
                float(bar.close) < session.vwap[idx] - buffer
                if direction > 0
                else float(bar.close) > session.vwap[idx] + buffer
            )
            if invalid:
                pending_market_exit = "vwap_invalidated"

    if remaining > 0:
        last = session.bars[-1]
        _close_part(fills, remaining, float(last.close), "session_end")
        exit_idx = len(session.bars) - 1
        remaining = 0

    if not fills:
        return None, exit_idx, "no_exit"
    filled_qty = sum(qty for qty, _, _ in fills)
    if filled_qty != contracts:
        raise AssertionError(f"partial fill accounting mismatch: {filled_qty} != {contracts}")
    weighted_exit = sum(qty * price for qty, price, _ in fills) / contracts
    gross = sum(qty * direction * (price - entry) * point_value for qty, price, _ in fills)
    costs = contracts * (get_commission_rt(contract_id) + get_fees_rt(contract_id))
    net = gross - costs
    final_reason = fills[-1][2]
    trade = ResearchTrade(
        symbol=symbol,
        strategy=candidate.strategy,
        variant=candidate.variant,
        direction="long" if direction > 0 else "short",
        signal_time=candidate.signal_time,
        entry_time=entry_bar.timestamp,
        exit_time=session.bars[exit_idx].timestamp,
        entry_price=entry,
        exit_price=weighted_exit,
        initial_stop=stop,
        final_stop=active_stop,
        contracts=contracts,
        risk_points=risk,
        planned_risk_dollars=contracts * risk * point_value,
        gross_pnl=gross,
        costs=costs,
        pnl=net,
        exit_reason=final_reason,
        tp1_hit=tp1_hit or any(reason == "tp1" for _, _, reason in fills),
        reason=candidate.reason,
    )
    return trade, exit_idx, ""


def simulate_candidates(
    session: RthSession,
    candidates: Sequence[EntryCandidate],
    symbol: str,
    max_trades_per_day: int,
) -> tuple[list[ResearchTrade], Counter]:
    trades: list[ResearchTrade] = []
    skipped: Counter = Counter()
    last_exit_index = -1
    for candidate in sorted(candidates, key=lambda row: row.signal_index):
        if len(trades) >= max(0, int(max_trades_per_day)):
            skipped["max_trades"] += 1
            continue
        # A signal formed on the exit bar is known after the intrabar exit and
        # may enter next minute.  Earlier signals overlapped an open position.
        if candidate.signal_index < last_exit_index:
            skipped["overlap"] += 1
            continue
        trade, exit_idx, reason = simulate_candidate(session, candidate, symbol)
        if trade is None:
            skipped[reason or "invalid"] += 1
            continue
        trades.append(trade)
        last_exit_index = exit_idx
    return trades, skipped


def _score_trades(
    trades: Sequence[ResearchTrade],
    *,
    monte_carlo_iters: int = 1000,
) -> dict:
    rows = [trade.robustness_row() for trade in trades]
    robust = evaluate_robustness(
        rows,
        iters=monte_carlo_iters,
        dd_threshold=2000.0,
        slip_levels=(1, 2, 4, 8, 14),
    )
    yearly_rows: dict[str, list[float]] = defaultdict(list)
    side_rows: dict[str, list[float]] = defaultdict(list)
    daily_rows: dict[str, float] = defaultdict(float)
    sizes: list[int] = []
    risks: list[float] = []
    for trade in trades:
        et_entry = _as_et(trade.entry_time)
        yearly_rows[str(et_entry.year)].append(trade.pnl)
        side_rows[trade.direction].append(trade.pnl)
        daily_rows[et_entry.date().isoformat()] += trade.pnl
        sizes.append(trade.contracts)
        risks.append(trade.planned_risk_dollars)
    robust["yearly"] = {year: series_stats(pnls) for year, pnls in sorted(yearly_rows.items())}
    robust["by_side"] = {side: series_stats(pnls) for side, pnls in sorted(side_rows.items())}
    robust["position_size"] = {
        "min": min(sizes) if sizes else 0,
        "median": sorted(sizes)[len(sizes) // 2] if sizes else 0,
        "max": max(sizes) if sizes else 0,
        "max_planned_risk": max(risks) if risks else 0.0,
    }
    positive_days = [pnl for pnl in daily_rows.values() if pnl > 0]
    total_pnl = robust["stats"]["pnl"]
    robust["consistency"] = {
        "trading_days": len(daily_rows),
        "best_day": max(positive_days) if positive_days else 0.0,
        "best_day_share_of_total": (
            max(positive_days) / total_pnl if positive_days and total_pnl > 0 else None
        ),
    }
    slip14 = next(
        (row["stats"] for row in robust["slip"]["levels"] if row["level"] == 14),
        series_stats([]),
    )
    wf_pass = bool(robust.get("walk_forward") and robust["walk_forward"].get("pass"))
    if len(trades) < 100:
        verdict = "INSUFFICIENT_SAMPLE"
    elif robust["stats"]["pf"] <= 1.0:
        verdict = "FAIL_BASE_PF"
    elif not wf_pass:
        verdict = "FAIL_WALK_FORWARD"
    elif slip14["pf"] <= 1.0:
        verdict = "FAIL_14T_SLIPPAGE"
    elif not robust.get("monte_carlo_pass"):
        verdict = "FAIL_MONTE_CARLO"
    else:
        verdict = "RESEARCH_CANDIDATE"
    robust["slip_14t"] = slip14
    robust["verdict"] = verdict
    return robust


def run_config(
    sessions: Sequence[RthSession],
    config: ResearchConfig,
    symbol: str,
    *,
    news_events: Sequence[datetime] = (),
    monte_carlo_iters: int = 1000,
) -> RunResult:
    symbol = symbol.upper()
    all_trades: list[ResearchTrade] = []
    skipped: Counter = Counter()
    candidate_count = news_blocked = 0
    if isinstance(config, OrbConfig):
        strategy = "ORB"
        generator = generate_orb_candidates
    elif isinstance(config, VwapPullbackConfig):
        strategy = "VWAP_PULLBACK"
        generator = generate_vwap_pullback_candidates
    elif isinstance(config, MeanReversionConfig):
        strategy = "MEAN_REVERSION"
        generator = generate_mean_reversion_candidates
    else:
        raise TypeError(f"unsupported config: {type(config)!r}")

    for session in sessions:
        candidates, blocked = generator(
            session, config, symbol, news_events=news_events
        )
        candidate_count += len(candidates)
        news_blocked += blocked
        trades, session_skips = simulate_candidates(
            session,
            candidates,
            symbol,
            config.max_trades_per_day,
        )
        all_trades.extend(trades)
        skipped.update(session_skips)
    diagnostics = {
        "sessions": len(sessions),
        "candidates": candidate_count,
        "news_filter_applied": bool(news_events),
        "news_events": len(news_events),
        "news_blocked": news_blocked,
        "skipped": dict(skipped),
        "causal_entry": "next_available_1m_open",
        "position_overlap": "forbidden",
        "same_bar_partial_ambiguity": "stop_first",
        "costs_included": True,
        "slippage_injection_ticks_rt": [1, 2, 4, 8, 14],
    }
    return RunResult(
        symbol=symbol,
        strategy=strategy,
        variant=config.name,
        config=asdict(config),
        trades=tuple(all_trades),
        diagnostics=diagnostics,
        summary=_score_trades(all_trades, monte_carlo_iters=monte_carlo_iters),
    )


def recommended_configs(
    symbol: str,
    *,
    risk_dollars: float = 200.0,
    max_trades_per_day: int = 2,
) -> tuple[ResearchConfig, ...]:
    """Small, pre-declared sensitivity set covering every suggested branch."""

    symbol = symbol.upper()
    rules = SYMBOL_RULES[symbol]
    common = {
        "risk_dollars": risk_dollars,
        "max_trades_per_day": max_trades_per_day,
    }
    return (
        OrbConfig("orb15_5m_breakout_std_v12_tp20", **common),
        OrbConfig("orb15_5m_retest_std_v12_tp20", entry_mode="retest", **common),
        OrbConfig("orb30_5m_breakout_std_v12_tp20", opening_minutes=30, **common),
        OrbConfig("orb15_1m_breakout_std_v12_tp20", confirm_minutes=1, **common),
        OrbConfig("orb15_5m_breakout_std_v15_tp20", volume_multiple=1.5, **common),
        OrbConfig("orb15_5m_breakout_mid_v12_tp20", stop_mode="midpoint", **common),
        OrbConfig("orb15_5m_breakout_std_v12_tp15", tp2_multiple=1.5, **common),
        VwapPullbackConfig(
            "vwap_pb_5m_tight_band",
            confirm_minutes=5,
            penetration_points=rules.pullback_penetration_tight,
            stop_buffer_points=rules.pullback_stop_tight,
            **common,
        ),
        VwapPullbackConfig(
            "vwap_pb_5m_wide_band",
            confirm_minutes=5,
            penetration_points=rules.pullback_penetration_wide,
            stop_buffer_points=rules.pullback_stop_wide,
            **common,
        ),
        VwapPullbackConfig(
            "vwap_pb_15m_wide_band",
            confirm_minutes=15,
            penetration_points=rules.pullback_penetration_wide,
            stop_buffer_points=rules.pullback_stop_wide,
            **common,
        ),
        VwapPullbackConfig(
            "vwap_pb_5m_wide_day_extreme",
            confirm_minutes=5,
            penetration_points=rules.pullback_penetration_wide,
            stop_buffer_points=rules.pullback_stop_wide,
            tp2_mode="day_extreme",
            **common,
        ),
        MeanReversionConfig(
            "meanrev_5m_15sd_vwap_tight",
            entry_sigma=1.5,
            stop_buffer_points=rules.mean_stop_tight,
            **common,
        ),
        MeanReversionConfig(
            "meanrev_5m_20sd_vwap_wide",
            entry_sigma=2.0,
            stop_buffer_points=rules.mean_stop_wide,
            **common,
        ),
        MeanReversionConfig(
            "meanrev_5m_20sd_opposite_wide",
            entry_sigma=2.0,
            stop_buffer_points=rules.mean_stop_wide,
            target_mode="opposite_band",
            **common,
        ),
        MeanReversionConfig(
            "meanrev_5m_20sd_vwap_flat05",
            entry_sigma=2.0,
            stop_buffer_points=rules.mean_stop_wide,
            vwap_flat_max_pct=0.0005,
            **common,
        ),
    )


def run_recommended_suite(
    sessions: Sequence[RthSession],
    symbol: str,
    *,
    news_events: Sequence[datetime] = (),
    risk_dollars: float = 200.0,
    max_trades_per_day: int = 2,
    monte_carlo_iters: int = 1000,
) -> list[RunResult]:
    return [
        run_config(
            sessions,
            config,
            symbol,
            news_events=news_events,
            monte_carlo_iters=monte_carlo_iters,
        )
        for config in recommended_configs(
            symbol,
            risk_dollars=risk_dollars,
            max_trades_per_day=max_trades_per_day,
        )
    ]


def result_to_dict(result: RunResult, *, include_trades: bool = False) -> dict:
    payload = {
        "symbol": result.symbol,
        "strategy": result.strategy,
        "variant": result.variant,
        "config": result.config,
        "diagnostics": result.diagnostics,
        "summary": result.summary,
    }
    if include_trades:
        payload["trades"] = [asdict(trade) for trade in result.trades]
    return payload
