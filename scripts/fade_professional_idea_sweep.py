"""Professional-style fade idea sweep.

This is a research-only reset of the Fade playbook. It converts common
professional/public frameworks into testable rules:
  - Volume Profile / Auction Market Theory value rotation
  - VWAP stretch reversion
  - Opening range false-break fade
  - Liquidity sweep reclaim
  - Exhaustion filters: Bollinger-style z-score, RSI/KDJ, volume wick rejection
  - Pyramid / staged take-profit exits

It uses a lightweight simulator so partial/pyramid exits can be tested without
changing the production backtest/live engine.

Run:
  PYTHONIOENCODING=utf-8 python -m scripts.fade_professional_idea_sweep
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import time
from collections import defaultdict, deque
from dataclasses import dataclass, asdict
from datetime import datetime, time as dt_time, timezone
from pathlib import Path
from typing import Any, Optional

from backend.backtest.engine import _topstep_trade_date
from backend.data import candle_store
from backend.db.models import (
    Candle,
    Direction,
    _extract_symbol,
    get_commission_rt,
    get_fees_rt,
    get_point_value,
)
from backend.strategy.session_filter import market_session_code
from backend.strategy.volume_profile import VolumeProfileCalculator
from backend.terminal_live import BUILTIN_PRESETS, CLAUDE_701_PRESET_1

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "machinelearning" / "fade_professional_ideas"
OUT_CSV = OUT_DIR / "results.csv"
OUT_TOP = OUT_DIR / "top_latest.csv"
OUT_JSON = OUT_DIR / "latest.json"
OUT_MD = OUT_DIR / "report.md"
OUT_CHECKPOINT = OUT_DIR / "checkpoint.json"

TICK = 0.25
MIN_STOP_POINTS = 4 * TICK
MIN_TARGET_POINTS = 4 * TICK

IDEAS = (
    "va_resting",
    "va_reject",
    "outside_reclaim",
    "pdh_pdl_sweep",
    "or15_false_break",
    "or30_false_break",
    "vwap_stretch",
    "bb_z_reject",
    "rsi_kdj_exhaust",
    "volume_wick_reject",
    "gap_fade",
    "lvn_reject",
)

TP_SCHEMES = (
    "full_poc",
    "full_1r",
    "ladder30",
    "pyr_10_30_L",
    "pyr_20_50_L",
    "pyr_poc_L",
)

SESSIONS = ("ALL", "ASIA", "EURO", "RTH")
SL_FRACS = (0.15, 0.20, 0.30, 0.50)
MAX_ENTRIES = (1, 3)


@dataclass(frozen=True)
class Variant:
    idea: str
    session: str
    sl_frac: float
    tp_scheme: str
    max_entries: int

    @property
    def tag(self) -> str:
        return (
            f"{self.idea}|{self.session}|SL={self.sl_frac:.2g}Rng|"
            f"{self.tp_scheme}|maxE={self.max_entries}"
        )


@dataclass
class Signal:
    direction: int
    entry: float
    order_type: str
    idea: str
    day: str
    timestamp: datetime
    poc: float
    vah: float
    val: float
    rng: float
    note: str


@dataclass
class Pending:
    signal: Signal
    expires_index: int


@dataclass
class Target:
    price: float
    qty: float
    label: str
    hit: bool = False


@dataclass
class Position:
    signal: Signal
    entry: float
    stop: float
    remaining: float
    targets: list[Target]
    ladder_step: float
    max_ladder_step: int = 0
    pnl: float = 0.0
    closed: bool = False
    exit_reason: str = ""
    legs: int = 0


class RollingStats:
    def __init__(self, n: int):
        self.n = n
        self.values: deque[float] = deque()
        self.s = 0.0
        self.s2 = 0.0

    def add(self, x: float) -> None:
        self.values.append(float(x))
        self.s += float(x)
        self.s2 += float(x) * float(x)
        if len(self.values) > self.n:
            old = self.values.popleft()
            self.s -= old
            self.s2 -= old * old

    @property
    def mean(self) -> float:
        return self.s / len(self.values) if self.values else 0.0

    @property
    def std(self) -> float:
        if len(self.values) < 2:
            return 0.0
        m = self.mean
        var = max(0.0, self.s2 / len(self.values) - m * m)
        return math.sqrt(var)

    def z(self, x: float) -> float:
        sd = self.std
        return (float(x) - self.mean) / sd if sd > 1e-9 else 0.0


def _round_tick(x: float) -> float:
    return round(round(x / TICK) * TICK, 4)


def _utc_time(ts: datetime) -> dt_time:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).time()


def _in_flatten_window(ts: datetime) -> bool:
    t = _utc_time(ts)
    return dt_time(19, 45) <= t < dt_time(22, 0)


def _in_pre_flatten(ts: datetime) -> bool:
    t = _utc_time(ts)
    return dt_time(19, 30) <= t < dt_time(22, 0)


def _direction_value(direction: Direction | int | str) -> int:
    if direction in (1, Direction.BUY, "buy", "BUY"):
        return 1
    return -1


def _lvn_between(profile: dict[float, int], a: float, b: float) -> Optional[float]:
    lo, hi = sorted((a, b))
    candidates = [(p, v) for p, v in profile.items() if lo <= float(p) <= hi]
    if not candidates:
        return None
    min_v = min(v for _, v in candidates)
    nodes = [p for p, v in candidates if v == min_v]
    return min(nodes, key=lambda p: abs(p - b))


def _build_prev_day_levels(candles: list[Candle]) -> dict[str, dict[str, Any]]:
    by_day: dict[str, list[Candle]] = defaultdict(list)
    for c in candles:
        by_day[_topstep_trade_date(c.timestamp)].append(c)
    days = sorted(by_day)
    calc = VolumeProfileCalculator(TICK, 0.80)
    levels: dict[str, dict[str, Any]] = {}
    for i in range(1, len(days)):
        prev_day = days[i - 1]
        day = days[i]
        prev = by_day[prev_day]
        try:
            vp = calc.calculate(prev)
        except ValueError:
            continue
        high = max(c.high for c in prev)
        low = min(c.low for c in prev)
        rng = max(TICK, float(vp.vah - vp.val))
        levels[day] = {
            "date": day,
            "source_day": prev_day,
            "poc": float(vp.poc),
            "vah": float(vp.vah),
            "val": float(vp.val),
            "high": float(high),
            "low": float(low),
            "range": rng,
            "mid": float((vp.vah + vp.val) / 2.0),
            "upper_lvn": _lvn_between(vp.profile, vp.poc, vp.vah),
            "lower_lvn": _lvn_between(vp.profile, vp.val, vp.poc),
        }
    return levels


def _build_features(candles: list[Candle], levels: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    close20 = RollingStats(20)
    close60 = RollingStats(60)
    vol60 = RollingStats(60)
    highs14: deque[float] = deque()
    lows14: deque[float] = deque()
    gains14: deque[float] = deque()
    losses14: deque[float] = deque()
    k = 50.0
    d = 50.0
    prev_close: Optional[float] = None
    cur_day: Optional[str] = None
    day_index = -1
    day_open = 0.0
    day_gap = "inside"
    cum_pv = 0.0
    cum_vol = 0.0
    or15_high = or15_low = None
    or30_high = or30_low = None
    out: list[dict[str, Any]] = []

    for i, c in enumerate(candles):
        day = _topstep_trade_date(c.timestamp)
        lv = levels.get(day)
        if day != cur_day:
            cur_day = day
            day_index = 0
            day_open = c.open
            cum_pv = 0.0
            cum_vol = 0.0
            or15_high = or15_low = None
            or30_high = or30_low = None
            if lv and day_open > lv["vah"]:
                day_gap = "above_va"
            elif lv and day_open < lv["val"]:
                day_gap = "below_va"
            else:
                day_gap = "inside"
        else:
            day_index += 1

        typical = (c.high + c.low + c.close) / 3.0
        vol = max(0, int(c.volume or 0))
        cum_pv += typical * vol
        cum_vol += vol
        vwap = cum_pv / cum_vol if cum_vol > 0 else c.close

        t = _utc_time(c.timestamp)
        in_or15 = dt_time(13, 30) <= t < dt_time(13, 45)
        in_or30 = dt_time(13, 30) <= t < dt_time(14, 0)
        if in_or15:
            or15_high = c.high if or15_high is None else max(or15_high, c.high)
            or15_low = c.low if or15_low is None else min(or15_low, c.low)
        if in_or30:
            or30_high = c.high if or30_high is None else max(or30_high, c.high)
            or30_low = c.low if or30_low is None else min(or30_low, c.low)

        if prev_close is None:
            change = 0.0
        else:
            change = c.close - prev_close
        gains14.append(max(0.0, change))
        losses14.append(max(0.0, -change))
        if len(gains14) > 14:
            gains14.popleft()
            losses14.popleft()
        avg_gain = sum(gains14) / len(gains14) if gains14 else 0.0
        avg_loss = sum(losses14) / len(losses14) if losses14 else 0.0
        rs = avg_gain / avg_loss if avg_loss > 1e-9 else 99.0
        rsi = 100.0 - 100.0 / (1.0 + rs)

        highs14.append(c.high)
        lows14.append(c.low)
        if len(highs14) > 14:
            highs14.popleft()
            lows14.popleft()
        hh = max(highs14)
        ll = min(lows14)
        rsv = 50.0 if hh <= ll else 100.0 * (c.close - ll) / (hh - ll)
        k = (2.0 / 3.0) * k + (1.0 / 3.0) * rsv
        d = (2.0 / 3.0) * d + (1.0 / 3.0) * k
        j = 3.0 * k - 2.0 * d

        f = {
            "i": i,
            "candle": c,
            "day": day,
            "day_index": day_index,
            "day_gap": day_gap,
            "level": lv,
            "session": market_session_code(c.timestamp),
            "vwap": vwap,
            "z20": close20.z(c.close),
            "z60": close60.z(c.close),
            "vol_z": vol60.z(vol),
            "rsi": rsi,
            "k": k,
            "d": d,
            "j": j,
            "or15_high": or15_high,
            "or15_low": or15_low,
            "or15_ready": t >= dt_time(13, 45) and or15_high is not None,
            "or30_high": or30_high,
            "or30_low": or30_low,
            "or30_ready": t >= dt_time(14, 0) and or30_high is not None,
            "prev_close": prev_close,
        }
        out.append(f)
        close20.add(c.close)
        close60.add(c.close)
        vol60.add(vol)
        prev_close = c.close
    return out


def _variants() -> list[Variant]:
    return [
        Variant(idea, session, sl_frac, tp_scheme, max_entries)
        for idea in IDEAS
        for session in SESSIONS
        for sl_frac in SL_FRACS
        for tp_scheme in TP_SCHEMES
        for max_entries in MAX_ENTRIES
    ]


def _allowed_session(variant: Variant, feature: dict[str, Any]) -> bool:
    return variant.session == "ALL" or feature["session"] == variant.session


def _mk_signal(feature: dict[str, Any], direction: int, entry: float, order_type: str, variant: Variant, note: str) -> Signal:
    lv = feature["level"]
    c = feature["candle"]
    return Signal(
        direction=direction,
        entry=_round_tick(entry),
        order_type=order_type,
        idea=variant.idea,
        day=feature["day"],
        timestamp=c.timestamp,
        poc=float(lv["poc"]),
        vah=float(lv["vah"]),
        val=float(lv["val"]),
        rng=max(TICK, float(lv["range"])),
        note=note,
    )


def _signal_for(variant: Variant, f: dict[str, Any]) -> Optional[Signal]:
    c: Candle = f["candle"]
    lv = f["level"]
    if not lv or not _allowed_session(variant, f) or _in_pre_flatten(c.timestamp):
        return None
    poc = float(lv["poc"])
    vah = float(lv["vah"])
    val = float(lv["val"])
    prev_high = float(lv["high"])
    prev_low = float(lv["low"])
    rng = max(TICK, float(lv["range"]))
    mid = float(lv["mid"])
    near = 0.05 * rng
    bar_rng = max(TICK, c.high - c.low)
    upper_wick = c.high - max(c.open, c.close)
    lower_wick = min(c.open, c.close) - c.low
    prev_close = f.get("prev_close")

    if variant.idea == "va_resting":
        if val < c.close < vah:
            if c.close >= mid:
                return _mk_signal(f, -1, vah, "limit", variant, "resting sell at prev VAH")
            return _mk_signal(f, 1, val, "limit", variant, "resting buy at prev VAL")

    elif variant.idea == "va_reject":
        if c.high >= vah and c.close < vah and c.close < c.open:
            return _mk_signal(f, -1, c.close, "market", variant, "VAH rejection close")
        if c.low <= val and c.close > val and c.close > c.open:
            return _mk_signal(f, 1, c.close, "market", variant, "VAL rejection close")

    elif variant.idea == "outside_reclaim":
        if prev_close is not None and prev_close > vah and c.close < vah:
            return _mk_signal(f, -1, c.close, "market", variant, "above VAH reclaimed inside")
        if prev_close is not None and prev_close < val and c.close > val:
            return _mk_signal(f, 1, c.close, "market", variant, "below VAL reclaimed inside")

    elif variant.idea == "pdh_pdl_sweep":
        if c.high > prev_high and c.close < prev_high:
            return _mk_signal(f, -1, c.close, "market", variant, "previous high sweep reclaim")
        if c.low < prev_low and c.close > prev_low:
            return _mk_signal(f, 1, c.close, "market", variant, "previous low sweep reclaim")

    elif variant.idea == "or15_false_break":
        if f["or15_ready"]:
            if c.high > float(f["or15_high"]) and c.close < float(f["or15_high"]):
                return _mk_signal(f, -1, c.close, "market", variant, "15m OR high false break")
            if c.low < float(f["or15_low"]) and c.close > float(f["or15_low"]):
                return _mk_signal(f, 1, c.close, "market", variant, "15m OR low false break")

    elif variant.idea == "or30_false_break":
        if f["or30_ready"]:
            if c.high > float(f["or30_high"]) and c.close < float(f["or30_high"]):
                return _mk_signal(f, -1, c.close, "market", variant, "30m OR high false break")
            if c.low < float(f["or30_low"]) and c.close > float(f["or30_low"]):
                return _mk_signal(f, 1, c.close, "market", variant, "30m OR low false break")

    elif variant.idea == "vwap_stretch":
        stretch = (c.close - float(f["vwap"])) / rng
        if stretch >= 0.25 and c.high >= vah - near:
            return _mk_signal(f, -1, c.close, "market", variant, "VWAP upper stretch")
        if stretch <= -0.25 and c.low <= val + near:
            return _mk_signal(f, 1, c.close, "market", variant, "VWAP lower stretch")

    elif variant.idea == "bb_z_reject":
        if f["z60"] >= 1.5 and c.high >= vah - near:
            return _mk_signal(f, -1, c.close, "market", variant, "rolling z upper rejection")
        if f["z60"] <= -1.5 and c.low <= val + near:
            return _mk_signal(f, 1, c.close, "market", variant, "rolling z lower rejection")

    elif variant.idea == "rsi_kdj_exhaust":
        if f["rsi"] >= 70 and f["j"] >= 90 and c.high >= vah - near:
            return _mk_signal(f, -1, c.close, "market", variant, "RSI/KDJ upper exhaustion")
        if f["rsi"] <= 30 and f["j"] <= 10 and c.low <= val + near:
            return _mk_signal(f, 1, c.close, "market", variant, "RSI/KDJ lower exhaustion")

    elif variant.idea == "volume_wick_reject":
        if f["vol_z"] >= 1.5 and upper_wick >= 0.35 * bar_rng and c.high >= vah and c.close < vah:
            return _mk_signal(f, -1, c.close, "market", variant, "volume spike upper wick")
        if f["vol_z"] >= 1.5 and lower_wick >= 0.35 * bar_rng and c.low <= val and c.close > val:
            return _mk_signal(f, 1, c.close, "market", variant, "volume spike lower wick")

    elif variant.idea == "gap_fade":
        if f["day_index"] <= 180:
            if f["day_gap"] == "above_va" and c.close < vah:
                return _mk_signal(f, -1, c.close, "market", variant, "session opened above VA then failed")
            if f["day_gap"] == "below_va" and c.close > val:
                return _mk_signal(f, 1, c.close, "market", variant, "session opened below VA then failed")

    elif variant.idea == "lvn_reject":
        upper = lv.get("upper_lvn")
        lower = lv.get("lower_lvn")
        if upper is not None and c.high >= float(upper) and c.close < float(upper) and c.close >= poc:
            return _mk_signal(f, -1, c.close, "market", variant, "upper LVN rejection")
        if lower is not None and c.low <= float(lower) and c.close > float(lower) and c.close <= poc:
            return _mk_signal(f, 1, c.close, "market", variant, "lower LVN rejection")

    return None


def _favorable_price(entry: float, direction: int, dist: float) -> float:
    return _round_tick(entry + direction * max(MIN_TARGET_POINTS, dist))


def _poc_target(signal: Signal) -> Optional[float]:
    if signal.direction == 1 and signal.poc > signal.entry + MIN_TARGET_POINTS:
        return _round_tick(signal.poc)
    if signal.direction == -1 and signal.poc < signal.entry - MIN_TARGET_POINTS:
        return _round_tick(signal.poc)
    return None


def _build_position(signal: Signal, variant: Variant) -> Position:
    sl_dist = max(MIN_STOP_POINTS, variant.sl_frac * signal.rng)
    stop = _round_tick(signal.entry - signal.direction * sl_dist)
    targets: list[Target] = []
    ladder_step = 0.0

    def add(qty: float, price: float, label: str) -> None:
        targets.append(Target(price=_round_tick(price), qty=max(0.0, min(1.0, qty)), label=label))

    if variant.tp_scheme == "full_poc":
        poc = _poc_target(signal)
        add(1.0, poc if poc is not None else _favorable_price(signal.entry, signal.direction, 0.5 * signal.rng), "POC")
    elif variant.tp_scheme == "full_1r":
        add(1.0, _favorable_price(signal.entry, signal.direction, signal.rng), "1Rng")
    elif variant.tp_scheme == "ladder30":
        ladder_step = max(MIN_TARGET_POINTS, 0.30 * signal.rng)
    elif variant.tp_scheme == "pyr_10_30_L":
        add(0.33, _favorable_price(signal.entry, signal.direction, 0.10 * signal.rng), "pyr10")
        add(0.33, _favorable_price(signal.entry, signal.direction, 0.30 * signal.rng), "pyr30")
        ladder_step = max(MIN_TARGET_POINTS, 0.30 * signal.rng)
    elif variant.tp_scheme == "pyr_20_50_L":
        add(0.50, _favorable_price(signal.entry, signal.direction, 0.20 * signal.rng), "pyr20")
        add(0.25, _favorable_price(signal.entry, signal.direction, 0.50 * signal.rng), "pyr50")
        ladder_step = max(MIN_TARGET_POINTS, 0.30 * signal.rng)
    elif variant.tp_scheme == "pyr_poc_L":
        poc = _poc_target(signal)
        add(0.50, poc if poc is not None else _favorable_price(signal.entry, signal.direction, 0.30 * signal.rng), "pyrPOC")
        ladder_step = max(MIN_TARGET_POINTS, 0.30 * signal.rng)

    return Position(
        signal=signal,
        entry=signal.entry,
        stop=stop,
        remaining=1.0,
        targets=targets,
        ladder_step=ladder_step,
    )


class Simulator:
    def __init__(self, features: list[dict[str, Any]], variant: Variant, point_value: float, rt_cost: float):
        self.features = features
        self.variant = variant
        self.point_value = point_value
        self.rt_cost = rt_cost
        self.pending: Optional[Pending] = None
        self.pos: Optional[Position] = None
        self.counts: defaultdict[str, int] = defaultdict(int)
        self.closed: list[dict[str, Any]] = []
        self.equity = 0.0
        self.peak = 0.0
        self.max_dd = 0.0
        self.side_counts = defaultdict(int)

    def _realize(self, pos: Position, exit_price: float, qty: float, reason: str, ts: datetime) -> None:
        qty = min(qty, pos.remaining)
        if qty <= 0:
            return
        gross = pos.signal.direction * (exit_price - pos.entry) * self.point_value * qty
        pnl = gross - self.rt_cost * qty
        pos.pnl += pnl
        pos.remaining = max(0.0, pos.remaining - qty)
        pos.legs += 1
        self.equity += pnl
        if self.equity > self.peak:
            self.peak = self.equity
        self.max_dd = max(self.max_dd, self.peak - self.equity)
        if pos.remaining <= 1e-9:
            pos.closed = True
            pos.exit_reason = reason
            self.closed.append({
                "entry_time": pos.signal.timestamp.isoformat(),
                "exit_time": ts.isoformat(),
                "direction": "buy" if pos.signal.direction == 1 else "sell",
                "idea": pos.signal.idea,
                "pnl": round(pos.pnl, 4),
                "legs": pos.legs,
                "reason": reason,
            })
            self.side_counts["buy" if pos.signal.direction == 1 else "sell"] += 1

    def _enter(self, signal: Signal) -> None:
        key = f"{signal.day}:{signal.idea}:{'buy' if signal.direction == 1 else 'sell'}"
        self.counts[key] += 1
        self.pos = _build_position(signal, self.variant)

    def _can_trade_signal(self, signal: Signal) -> bool:
        key = f"{signal.day}:{signal.idea}:{'buy' if signal.direction == 1 else 'sell'}"
        return self.counts[key] < self.variant.max_entries

    def _check_pending(self, f: dict[str, Any]) -> None:
        if not self.pending or self.pos:
            return
        c = f["candle"]
        sig = self.pending.signal
        if f["i"] > self.pending.expires_index:
            self.pending = None
            return
        filled = (sig.direction == 1 and c.low <= sig.entry) or (sig.direction == -1 and c.high >= sig.entry)
        if filled and self._can_trade_signal(sig):
            self._enter(sig)
        self.pending = None

    def _check_exit(self, f: dict[str, Any]) -> None:
        pos = self.pos
        if not pos:
            return
        c = f["candle"]
        if _in_flatten_window(c.timestamp):
            self._realize(pos, c.close, pos.remaining, "flatten", c.timestamp)
            self.pos = None
            return

        stop_hit = (pos.signal.direction == 1 and c.low <= pos.stop) or (pos.signal.direction == -1 and c.high >= pos.stop)
        if stop_hit:
            self._realize(pos, pos.stop, pos.remaining, "sl", c.timestamp)
            self.pos = None
            return

        targets_hit = False
        for t in sorted(pos.targets, key=lambda x: pos.signal.direction * (x.price - pos.entry)):
            if t.hit or pos.remaining <= 1e-9:
                continue
            hit = (pos.signal.direction == 1 and c.high >= t.price) or (pos.signal.direction == -1 and c.low <= t.price)
            if hit:
                t.hit = True
                self._realize(pos, t.price, t.qty, t.label, c.timestamp)
                targets_hit = True
                if pos.closed:
                    self.pos = None
                    return

        if targets_hit:
            if pos.signal.direction == 1:
                pos.stop = max(pos.stop, pos.entry)
            else:
                pos.stop = min(pos.stop, pos.entry)

        if pos.ladder_step > 0 and pos.remaining > 1e-9:
            fav = pos.signal.direction * (c.close - pos.entry)
            step_n = int(math.floor(fav / pos.ladder_step))
            if step_n > pos.max_ladder_step:
                pos.max_ladder_step = step_n
            if pos.max_ladder_step >= 1:
                lock_steps = pos.max_ladder_step - 1
                new_stop = _round_tick(pos.entry + pos.signal.direction * lock_steps * pos.ladder_step)
                if pos.signal.direction == 1 and new_stop > pos.stop:
                    pos.stop = new_stop
                elif pos.signal.direction == -1 and new_stop < pos.stop:
                    pos.stop = new_stop

    def run(self) -> dict[str, Any]:
        for f in self.features:
            c = f["candle"]
            if _in_flatten_window(c.timestamp):
                self.pending = None
            self._check_exit(f)
            self._check_pending(f)
            if self.pos or self.pending:
                continue
            sig = _signal_for(self.variant, f)
            if not sig or not self._can_trade_signal(sig):
                continue
            if sig.order_type == "market":
                self._enter(sig)
            else:
                self.pending = Pending(sig, f["i"] + 1)

        if self.pos:
            last = self.features[-1]["candle"]
            self._realize(self.pos, last.close, self.pos.remaining, "data_end", last.timestamp)
            self.pos = None

        pnls = [float(t["pnl"]) for t in self.closed]
        gains = sum(p for p in pnls if p > 0)
        losses = sum(p for p in pnls if p < 0)
        trades = len(pnls)
        pf = gains / abs(losses) if losses < 0 else (999.0 if gains > 0 else 0.0)
        return {
            "trades": trades,
            "pnl": round(sum(pnls), 2),
            "max_dd": round(self.max_dd, 2),
            "profit_factor": round(pf, 4),
            "win_rate": round(sum(1 for p in pnls if p > 0) / trades, 4) if trades else 0.0,
            "expectancy": round(sum(pnls) / trades, 3) if trades else 0.0,
            "total_loss": round(losses, 2),
            "total_gain": round(gains, 2),
            "avg_legs": round(statistics.mean([t["legs"] for t in self.closed]), 3) if self.closed else 0.0,
            "side_counts": " ".join(f"{k}:{v}" for k, v in sorted(self.side_counts.items())),
        }


def _score(row: dict[str, Any]) -> float:
    pnl = float(row["pnl"])
    dd = max(100.0, float(row["max_dd"]))
    loss = abs(float(row["total_loss"]))
    pf = float(row["profit_factor"])
    trades = int(row["trades"])
    if trades < 35 or pnl <= 0:
        return -1e9
    return pnl - 0.9 * dd - max(0.0, loss - pnl) * 0.15 + 350.0 * max(0.0, pf - 1.5)


def _enrich(row: dict[str, Any]) -> dict[str, Any]:
    r = dict(row)
    reasons = []
    if int(r["trades"]) < 40:
        reasons.append("sample<40")
    if float(r["pnl"]) <= 0:
        reasons.append("pnl<=0")
    if float(r["max_dd"]) > 1000:
        reasons.append("maxDD>1000")
    if abs(float(r["total_loss"])) > float(r["pnl"]):
        reasons.append("loss>pnl")
    if float(r["profit_factor"]) < 1.4:
        reasons.append("PF<1.4")
    r["score"] = round(_score(r), 2)
    r["reasons"] = ",".join(reasons)
    r["verdict"] = "PASS" if not reasons and float(r["pnl"]) > 2000 else ("CAUTION" if float(r["pnl"]) > 0 else "FAIL")
    return r


def _write(rows: list[dict[str, Any]], next_index: int, total: int, done: bool) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    enriched = sorted((_enrich(r) for r in rows), key=lambda r: r["score"], reverse=True)
    if enriched:
        fields = list(enriched[0].keys())
        with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(enriched)
        with OUT_TOP.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(enriched[:80])
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "done": done,
        "next_index": next_index,
        "total": total,
        "tested": len(rows),
        "passes": sum(1 for r in enriched if r["verdict"] == "PASS"),
        "ideas": list(IDEAS),
        "tp_schemes": list(TP_SCHEMES),
        "top": enriched[:30],
        "files": {
            "results_csv": str(OUT_CSV.relative_to(ROOT)),
            "top_csv": str(OUT_TOP.relative_to(ROOT)),
            "report_md": str(OUT_MD.relative_to(ROOT)),
        },
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_CHECKPOINT.write_text(json.dumps({
        "next_index": next_index,
        "total": total,
        "done": done,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# Fade Professional Idea Sweep",
        "",
        f"Generated: {payload['created_at']}",
        f"Progress: {len(rows)}/{total}; done={done}",
        "",
        "Ideas tested: " + ", ".join(IDEAS),
        "",
        "| rank | verdict | variant | trades | pnl | maxDD | PF | win% | total loss | avg legs | reasons |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for i, r in enumerate(enriched[:30], 1):
        lines.append(
            f"| {i} | {r['verdict']} | {r['variant']} | {r['trades']} | {float(r['pnl']):+.0f} | "
            f"{float(r['max_dd']):.0f} | {float(r['profit_factor']):.2f} | "
            f"{100*float(r['win_rate']):.1f}% | {float(r['total_loss']):+.0f} | "
            f"{float(r['avg_legs']):.2f} | {r['reasons']} |"
        )
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _load_existing() -> tuple[list[dict[str, Any]], set[int]]:
    if not OUT_CSV.exists():
        return [], set()
    rows: list[dict[str, Any]] = []
    done: set[int] = set()
    with OUT_CSV.open("r", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            idx = int(float(r.get("index", -1)))
            if idx >= 0:
                done.add(idx)
            base = {k: v for k, v in r.items() if k not in ("score", "reasons", "verdict")}
            for k in ("index", "max_entries", "trades"):
                if k in base and base[k] != "":
                    base[k] = int(float(base[k]))
            for k in ("sl_frac", "pnl", "max_dd", "profit_factor", "win_rate", "expectancy", "total_loss", "total_gain", "avg_legs", "elapsed_sec"):
                if k in base and base[k] != "":
                    base[k] = float(base[k])
            rows.append(base)
    return rows, done


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="maximum new variants; 0 = all remaining")
    ap.add_argument("--flush-every", type=int, default=25)
    ap.add_argument("--reset", action="store_true", help="delete previous professional idea sweep output first")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if args.reset:
        for p in (OUT_CSV, OUT_TOP, OUT_JSON, OUT_MD, OUT_CHECKPOINT):
            if p.exists():
                p.unlink()

    preset = BUILTIN_PRESETS[CLAUDE_701_PRESET_1]
    contract_id = preset.get("contract_id", "CON.F.US.MNQ.U26")
    symbol = _extract_symbol(contract_id)
    point_value = get_point_value(contract_id)
    rt_cost = get_commission_rt(contract_id) + get_fees_rt(contract_id)

    candles = candle_store.load(symbol, 1)
    candles.sort(key=lambda c: c.timestamp)
    print(f"candles={len(candles)} symbol={symbol} point_value={point_value} rt_cost={rt_cost}", flush=True)

    levels = _build_prev_day_levels(candles)
    features = _build_features(candles, levels)
    variants = _variants()
    rows, done_indexes = _load_existing()
    print(f"variants={len(variants)} existing={len(done_indexes)} ideas={len(IDEAS)}", flush=True)

    new_runs = 0
    last_flush = time.time()
    for idx, variant in enumerate(variants):
        if idx in done_indexes:
            continue
        if args.limit and new_runs >= args.limit:
            break
        t0 = time.time()
        result = Simulator(features, variant, point_value, rt_cost).run()
        row = {
            "index": idx,
            "variant": variant.tag,
            **asdict(variant),
            **result,
            "elapsed_sec": round(time.time() - t0, 3),
        }
        rows.append(row)
        new_runs += 1
        if new_runs % max(1, args.flush_every) == 0 or time.time() - last_flush > 60:
            _write(rows, idx + 1, len(variants), done=False)
            best = sorted((_enrich(r) for r in rows), key=lambda r: r["score"], reverse=True)[0]
            print(
                f"[{len(rows)}/{len(variants)}] best {float(best['pnl']):+.0f} "
                f"DD={float(best['max_dd']):.0f} PF={float(best['profit_factor']):.2f} {best['variant']}",
                flush=True,
            )
            last_flush = time.time()

    complete = len({int(r["index"]) for r in rows if "index" in r}) >= len(variants)
    next_index = len(variants) if complete else max((int(r["index"]) for r in rows if "index" in r), default=-1) + 1
    _write(rows, next_index, len(variants), done=complete)
    best = sorted((_enrich(r) for r in rows), key=lambda r: r["score"], reverse=True)[0] if rows else None
    if best:
        print(
            f"DONE best {float(best['pnl']):+.0f} DD={float(best['max_dd']):.0f} "
            f"PF={float(best['profit_factor']):.2f} {best['variant']}",
            flush=True,
        )
    print(f"Wrote {OUT_MD}", flush=True)


if __name__ == "__main__":
    main()
