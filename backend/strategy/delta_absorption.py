"""Delta change + prior RTH value-area absorption strategy.

The strategy consumes compact, completed one-minute MBO bars.  It deliberately
does not read raw DBN data or create a second order-flow definition: the
context providers below use the compact cache and the shared
``footprint_profile`` helper.  This keeps chart, historical backtest, and live
decisions on the same data contract.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean, median
from typing import Any, Mapping, Optional

from backend.data import market_data
from backend.data.orderflow import (
    DEFAULT_TICK_SIZE,
    footprint_cache_path,
    footprint_profile,
    read_footprint_cache,
)
from backend.db.models import Candle, Direction, StrategyParams, TradeSignal
from backend.strategy.pi_signal import PiSignalStrategy
from backend.strategy.session_filter import rth_session_bounds, rth_session_date
from backend.timebase import UTC, as_utc


def _epoch_minute(value: datetime) -> int:
    return int(math.floor(as_utc(value).timestamp() / 60.0) * 60)


def _bar_epoch(bar: Mapping[str, Any]) -> int:
    try:
        return int(bar.get("epoch") or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _valid_ohlc(bar: Mapping[str, Any]) -> bool:
    return all(bar.get(name) is not None for name in ("open", "high", "low", "close"))


def _sorted_bars(payload: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    rows = [dict(row) for row in (payload.get("bars") or []) if isinstance(row, Mapping)]
    rows.sort(key=_bar_epoch)
    return rows


def _cache_date_from_path(path: Path, symbol: str) -> Optional[str]:
    prefix = f"footprint_{symbol.lower()}_"
    name = path.name
    if not name.startswith(prefix) or not name.endswith(".json.gz"):
        return None
    value = name[len(prefix):len(prefix) + 10]
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


class CachedDeltaContextProvider:
    """Read compact historical order-flow context for Delta strategy calls.

    ``snapshot()`` exposes only bars that are at least one full minute old
    relative to the decision candle.  A normal candle backtest therefore sees
    the exact same one-minute causal delay as live MBO processing.
    """

    provider_name = "cache"

    def __init__(
        self,
        *,
        symbol: str = "MNQ",
        root: str | Path | None = None,
        tick_size: float = DEFAULT_TICK_SIZE,
        day_payloads: Mapping[str, Mapping[str, Any]] | None = None,
        require_complete_profile: bool = True,
    ) -> None:
        self.symbol = str(symbol or "MNQ").upper()
        self.root = Path(root).resolve() if root is not None else None
        self.tick_size = float(tick_size)
        if self.tick_size <= 0:
            raise ValueError("tick_size must be positive")
        self.require_complete_profile = bool(require_complete_profile)
        self._payloads: dict[str, dict[str, Any]] = {
            str(key): dict(value) for key, value in (day_payloads or {}).items()
            if isinstance(value, Mapping)
        }
        self._payload_cache: dict[str, Optional[dict[str, Any]]] = {}
        self._bars_cache: dict[str, list[dict[str, Any]]] = {}
        self._vwap_prefix_cache: dict[str, list[tuple[float, float]]] = {}
        self._profile_cache: dict[str, Optional[dict[str, float]]] = {}
        self._profile_date_cache: dict[str, Optional[str]] = {}

    def set_day_payload(self, trade_date: str, payload: Mapping[str, Any]) -> None:
        """Inject/replace a compact day for tests and controlled replay."""
        key = date.fromisoformat(str(trade_date)).isoformat()
        self._payloads[key] = dict(payload)
        self._payload_cache.pop(key, None)
        self._bars_cache.pop(key, None)
        self._vwap_prefix_cache.pop(key, None)
        self._profile_cache.clear()
        self._profile_date_cache.clear()

    def _load_payload(self, trade_date: str) -> Optional[dict[str, Any]]:
        key = date.fromisoformat(str(trade_date)).isoformat()
        if key in self._payload_cache:
            return self._payload_cache[key]
        payload = self._payloads.get(key)
        if payload is None:
            path = footprint_cache_path(key, self.symbol, root=self.root)
            try:
                payload = read_footprint_cache(path)
            except (FileNotFoundError, OSError, ValueError, EOFError):
                payload = None
        result = dict(payload) if isinstance(payload, Mapping) else None
        self._payload_cache[key] = result
        return result

    def _candidate_dates(self) -> list[str]:
        dates = set(self._payloads)
        base = market_data.derived_path(
            "orderflow", self.symbol.lower(), root=self.root,
        )
        prefix = f"footprint_{self.symbol.lower()}_"
        try:
            paths = base.glob(f"{prefix}*.json.gz")
        except OSError:
            paths = ()
        for path in paths:
            parsed = _cache_date_from_path(path, self.symbol)
            if parsed:
                dates.add(parsed)
        return sorted(dates)

    def _profile_ready(self, payload: Mapping[str, Any] | None) -> bool:
        bars = _sorted_bars(payload)
        if not bars:
            return False
        meta = payload.get("meta") if isinstance(payload, Mapping) else {}
        if isinstance(meta, Mapping):
            session = str(meta.get("session") or "RTH").upper()
            if session not in ("RTH", ""):
                return False
        # A complete CME RTH session is exactly 390 consecutive one-minute
        # bars.  Do not accept a longer/stitched file either: that can mix two
        # sessions and silently move yesterday's VAL/VAH.  The relaxed mode
        # exists only for deterministic unit tests/replay fixtures; production
        # defaults to the complete-session guard.
        if self.require_complete_profile:
            if len(bars) != 390:
                return False
            if any(
                _bar_epoch(next_bar) - _bar_epoch(bar) != 60
                for bar, next_bar in zip(bars, bars[1:])
            ):
                return False
        return bool(footprint_profile(
            bars, value_area_pct=0.70, tick_size=self.tick_size,
        ))

    def _previous_profile(self, current_date: str) -> tuple[Optional[str], Optional[dict[str, float]]]:
        if current_date in self._profile_cache:
            profile = self._profile_cache[current_date]
            return self._profile_date_cache.get(current_date), profile
        current = date.fromisoformat(current_date)
        for candidate in reversed(self._candidate_dates()):
            if date.fromisoformat(candidate) >= current:
                continue
            payload = self._load_payload(candidate)
            if not self._profile_ready(payload):
                continue
            profile = footprint_profile(
                _sorted_bars(payload), value_area_pct=0.70,
                tick_size=self.tick_size,
            )
            self._profile_cache[current_date] = profile or None
            self._profile_date_cache[current_date] = candidate
            return candidate, profile or None
        self._profile_cache[current_date] = None
        self._profile_date_cache[current_date] = None
        return None, None

    def _vwap(self, bars: list[Mapping[str, Any]]) -> Optional[float]:
        volume = 0.0
        weighted = 0.0
        for bar in bars:
            for cell in bar.get("cells") or []:
                if not isinstance(cell, (list, tuple)) or len(cell) < 3:
                    continue
                try:
                    qty = max(0, int(cell[1] or 0)) + max(0, int(cell[2] or 0))
                    price = float(cell[0]) * self.tick_size
                except (TypeError, ValueError, OverflowError):
                    continue
                volume += qty
                weighted += price * qty
        return weighted / volume if volume > 0 else None

    def _bars_for_date(
        self, trade_date: str, payload: Mapping[str, Any] | None,
    ) -> list[dict[str, Any]]:
        if trade_date not in self._bars_cache:
            self._bars_cache[trade_date] = _sorted_bars(payload)
        return self._bars_cache[trade_date]

    def _vwap_prefix(
        self, trade_date: str, bars: list[Mapping[str, Any]],
    ) -> list[tuple[float, float]]:
        """Build cumulative executed-volume VWAP once per cached MBO day.

        ``snapshot()`` is called once per platform candle during a replay.  The
        old implementation rescanned every earlier cell for every snapshot,
        turning a 390-minute day into an unnecessary quadratic workload.  The
        prefix values preserve the exact numerator/denominator definition used
        by ``_vwap`` while making each subsequent lookup O(1).
        """
        cached = self._vwap_prefix_cache.get(trade_date)
        if cached is not None and len(cached) == len(bars):
            return cached
        volume = 0.0
        weighted = 0.0
        prefix: list[tuple[float, float]] = []
        for bar in bars:
            if _bar_epoch(bar) > 0:
                for cell in bar.get("cells") or []:
                    if not isinstance(cell, (list, tuple)) or len(cell) < 3:
                        continue
                    try:
                        qty = max(0, int(cell[1] or 0)) + max(0, int(cell[2] or 0))
                        price = float(cell[0]) * self.tick_size
                    except (TypeError, ValueError, OverflowError):
                        continue
                    volume += qty
                    weighted += price * qty
            prefix.append((volume, weighted))
        self._vwap_prefix_cache[trade_date] = prefix
        return prefix

    def snapshot(self, timestamp: datetime) -> Optional[dict[str, Any]]:
        now = as_utc(timestamp)
        current_date = rth_session_date(now).isoformat()
        payload = self._load_payload(current_date)
        bars = self._bars_for_date(current_date, payload)
        if not bars:
            return None
        decision_epoch = _epoch_minute(now)
        eligible: list[dict[str, Any]] = []
        last_eligible_index = -1
        for index, row in enumerate(bars):
            epoch = _bar_epoch(row)
            if epoch > 0 and epoch + 60 <= decision_epoch:
                eligible.append(row)
                last_eligible_index = index
        if not eligible:
            return None
        previous_date, profile = self._previous_profile(current_date)
        prefix = self._vwap_prefix(current_date, bars)
        volume, weighted = prefix[last_eligible_index]
        return {
            "date": current_date,
            "bars": eligible,
            "bar": eligible[-1],
            "profile": profile,
            "previous_profile_date": previous_date,
            "vwap": weighted / volume if volume > 0 else None,
            "provider": self.provider_name,
        }

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "state": "cache",
            "connected": True,
            "provider": "cache",
            "symbol": self.symbol,
        }


class DeltaAbsorptionStrategy(PiSignalStrategy):
    """The selected ``absorption/w5/whole/location`` research rule.

    The PI base supplies ATR-blend risk construction and directional PI-style
    exits.  PI Discord rows are intentionally not read or pushed here: Delta
    is a standalone completed-bar entry model.
    """

    NAME = "DELTA ABSORPTION"

    def __init__(self, params, context_provider: Any | None = None):
        super().__init__(params)
        self.side_mode = str(getattr(params, "delta_side_mode", "all") or "all").lower()
        self.delta_window = max(1, int(getattr(params, "delta_window", 5) or 5))
        self.delta_baseline_window = max(
            2, int(getattr(params, "delta_baseline_window", 30) or 30)
        )
        self.delta_strength = max(
            0.1, float(getattr(params, "delta_strength_multiplier", 1.0) or 1.0)
        )
        self.delta_weakening = min(
            1.0, max(0.1, float(getattr(params, "delta_weakening_ratio", 0.70) or 0.70))
        )
        self.delta_stall_ticks = max(0, int(getattr(params, "delta_stall_ticks", 1) or 0))
        self.delta_value_lookback = max(
            2, int(getattr(params, "delta_value_lookback", 10) or 10)
        )
        self.delta_touch_ticks = max(
            0, int(getattr(params, "delta_value_touch_ticks", 0) or 0)
        )
        source = str(getattr(params, "delta_source", "whole") or "whole").lower()
        self.delta_source = source if source in ("whole", "outside") else "whole"
        gate = str(getattr(params, "delta_gate", "location") or "location").lower()
        self.delta_gate = gate if gate in ("raw", "location", "reclaim", "reclaim_vwap") else "location"
        pattern = str(getattr(params, "delta_pattern", "absorption") or "absorption").lower()
        self.delta_pattern = pattern if pattern in ("absorption", "exhaustion", "both") else "absorption"
        self.delta_require_profile = bool(getattr(params, "delta_require_profile", True))
        self.context_provider = context_provider or CachedDeltaContextProvider(
            symbol="MNQ", tick_size=self.tick_size,
            require_complete_profile=self.delta_require_profile,
        )
        self._observed_candle_key: Optional[int] = None
        self._last_signal_epoch: Optional[int] = None
        self._last_context: Optional[dict[str, Any]] = None
        self._last_status = "waiting_for_mbo"
        # Chart diagnostics are deliberately separate from trade metadata.
        # They show only the three pieces of evidence used by this model and
        # stay bounded so a long live session cannot grow process memory.
        self._chart_events: list[dict[str, Any]] = []
        self._chart_event_keys: set[tuple[int, str]] = set()
        self._chart_value_areas: dict[str, dict[str, Any]] = {}

    def reset(self) -> None:
        super().reset()
        self._observed_candle_key = None
        self._last_signal_epoch = None
        self._last_context = None
        self._last_status = "waiting_for_mbo"
        self._chart_events.clear()
        self._chart_event_keys.clear()
        self._chart_value_areas.clear()

    @staticmethod
    def _chart_time(epoch: int) -> str:
        return datetime.fromtimestamp(int(epoch), UTC).isoformat()

    def _record_chart_value_area(
        self, snapshot: Mapping[str, Any], event_epoch: int,
    ) -> None:
        """Keep one previous-RTH value area per current session.

        The strategy uses yesterday's complete profile as a location gate.
        A single segment per session makes that causal context visible without
        drawing one line for every MBO minute.
        """
        profile = snapshot.get("profile")
        if not isinstance(profile, Mapping):
            return
        try:
            vah = float(profile["vah"])
            val = float(profile["val"])
            poc = float(profile["poc"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return
        current_date = str(snapshot.get("date") or "")
        profile_date = str(snapshot.get("previous_profile_date") or "")
        if not current_date or not profile_date:
            return
        key = f"{current_date}|{profile_date}"
        bars = [row for row in (snapshot.get("bars") or []) if isinstance(row, Mapping)]
        first_epoch = _bar_epoch(bars[0]) if bars else event_epoch
        if first_epoch <= 0:
            first_epoch = event_epoch
        area = self._chart_value_areas.get(key)
        if area is None:
            area = {
                "session_date": current_date,
                "profile_date": profile_date,
                "poc": round(poc, 4),
                "vah": round(vah, 4),
                "val": round(val, 4),
                "start_time": self._chart_time(first_epoch),
                "end_time": self._chart_time(event_epoch),
            }
            self._chart_value_areas[key] = area
        else:
            start_epoch = min(
                first_epoch,
                int(datetime.fromisoformat(str(area["start_time"])).timestamp()),
            )
            end_epoch = max(
                event_epoch,
                int(datetime.fromisoformat(str(area["end_time"])).timestamp()),
            )
            area["start_time"] = self._chart_time(start_epoch)
            area["end_time"] = self._chart_time(end_epoch)
        # Keep a small safety bound for an unusually long process lifetime.
        if len(self._chart_value_areas) > 90:
            oldest = next(iter(self._chart_value_areas))
            self._chart_value_areas.pop(oldest, None)

    def _record_chart_event(
        self,
        *,
        snapshot: Mapping[str, Any],
        bars: list[Mapping[str, Any]],
        index: int,
        direction: Direction,
        profile: Mapping[str, Any] | None,
        family: Optional[str],
        gate_passed: bool,
        p0: float,
        p1: float,
        n0: float,
        n1: float,
        scale: float,
        absorption: bool,
        exhaustion: bool,
        stalled: bool,
        touched: bool,
        reclaimed: bool,
        confirming: bool,
        aligned: bool,
        vwap: Optional[float],
    ) -> None:
        """Record a compact, type-addressable explanation for one bar.

        ``delta_decay`` is a meaningful weakening of opposing pressure
        (recent opposing delta is lower than the prior window while still at
        or above the model's weakening floor).  ``stalled`` and ``touched``
        map directly to the price no-break and VAH/VAL evidence layers.
        """
        if index < 0 or index >= len(bars):
            return
        event_epoch = _bar_epoch(bars[index])
        if event_epoch <= 0:
            return
        opposing_previous, opposing_recent = (
            (n0, n1) if direction == Direction.BUY else (p0, p1)
        )
        delta_ratio = (
            opposing_recent / opposing_previous
            if opposing_previous > 0 else None
        )
        delta_decay = bool(
            opposing_previous > 0
            and opposing_recent < opposing_previous
            and opposing_recent >= self.delta_weakening * opposing_previous
        )
        delta_pressure = bool(opposing_recent >= self.delta_strength * scale)
        # Do not retain ordinary bars.  At least one of the displayed model
        # components must be present, so the chart remains an explanation of
        # the preset rather than a second full indicator stream.  Decay is a
        # standalone diagnostic layer; it must not disappear merely because
        # the stronger entry-pressure threshold was not also met.
        if not (touched or stalled or delta_decay):
            return
        direction_label = "long" if direction == Direction.BUY else "short"
        row = bars[index]
        try:
            anchor_price = float(row["low"] if direction == Direction.BUY else row["high"])
            close = float(row["close"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return
        vah = val = poc = None
        profile_date = snapshot.get("previous_profile_date")
        if isinstance(profile, Mapping):
            try:
                vah = round(float(profile["vah"]), 4)
                val = round(float(profile["val"]), 4)
                poc = round(float(profile["poc"]), 4)
            except (KeyError, TypeError, ValueError, OverflowError):
                pass
        key = (event_epoch, direction_label)
        if key in self._chart_event_keys:
            return
        self._chart_event_keys.add(key)
        self._chart_events.append({
            "epoch": event_epoch,
            "time": self._chart_time(event_epoch),
            "session_date": snapshot.get("date"),
            "profile_date": profile_date,
            "direction": direction_label,
            "pattern": family or (
                "absorption" if absorption else ("exhaustion" if exhaustion else "")
            ),
            "qualified": bool(family and gate_passed),
            "gate_passed": bool(gate_passed),
            "vah": vah,
            "val": val,
            "poc": poc,
            "anchor_price": round(anchor_price, 4),
            "close": round(close, 4),
            "positive_previous": round(p0, 4),
            "positive_recent": round(p1, 4),
            "negative_previous": round(n0, 4),
            "negative_recent": round(n1, 4),
            "opposing_previous": round(opposing_previous, 4),
            "opposing_recent": round(opposing_recent, 4),
            "delta_ratio": round(delta_ratio, 4) if delta_ratio is not None else None,
            "scale": round(scale, 4),
            "delta_pressure": delta_pressure,
            "delta_decay": delta_decay,
            "stalled": bool(stalled),
            "touched": bool(touched),
            "reclaimed": bool(reclaimed),
            "confirming": bool(confirming),
            "aligned": bool(aligned),
            "vwap": round(float(vwap), 4) if vwap is not None else None,
            "source": self.delta_source,
            "gate": self.delta_gate,
            "window": self.delta_window,
        })
        if len(self._chart_events) > 600:
            self._chart_events.pop(0)
            self._chart_event_keys = {
                (int(item["epoch"]), str(item["direction"]))
                for item in self._chart_events
            }

    def get_chart_overlay(self, limit: int = 600) -> dict[str, Any]:
        """Return bounded chart-only evidence for backtest and live UI."""
        count = max(0, int(limit or 0))
        events = self._chart_events[-count:] if count else []
        areas = list(self._chart_value_areas.values())
        return {
            "model": "delta_absorption",
            "pattern": self.delta_pattern,
            "source": self.delta_source,
            "gate": self.delta_gate,
            "events": [dict(event) for event in events],
            "value_areas": [dict(area) for area in areas],
        }

    def observe(self, candle: Candle, zones=None, is_mature: bool = True) -> None:
        # Keep the PI 5m ATR accumulator warm even when the RTH entry gate is
        # closed; only the engine's session gate controls whether evaluate() is
        # called for a new order.
        super().observe(candle, zones, is_mature)
        self._observed_candle_key = _epoch_minute(candle.timestamp)

    @staticmethod
    def _window_changes(values: list[float], index: int, window: int) -> tuple[float, float, float, float]:
        prior = values[index - 2 * window + 1:index - window + 1]
        recent = values[index - window + 1:index + 1]
        return tuple(
            mean(max(sign * value, 0.0) for value in block)
            for sign in (1.0, -1.0)
            for block in (prior, recent)
        )

    def _source_values(
        self, bars: list[Mapping[str, Any]], profile: Mapping[str, Any] | None,
        direction: Direction,
    ) -> list[float] | None:
        if self.delta_source == "whole":
            return [
                float(bar.get("buy") or 0) - float(bar.get("sell") or 0)
                for bar in bars
            ]
        if not profile:
            return None
        boundary = float(profile["val"] if direction == Direction.BUY else profile["vah"])
        values: list[float] = []
        for bar in bars:
            total = 0.0
            for cell in bar.get("cells") or []:
                if not isinstance(cell, (list, tuple)) or len(cell) < 3:
                    continue
                try:
                    price = float(cell[0]) * self.tick_size
                    signed = float(cell[1] or 0) - float(cell[2] or 0)
                except (TypeError, ValueError, OverflowError):
                    continue
                if (direction == Direction.BUY and price < boundary) or (
                    direction == Direction.SELL and price > boundary
                ):
                    total += signed
            values.append(total)
        return values

    def _gate_accepts(
        self,
        direction: Direction,
        *,
        touched: bool,
        reclaimed: bool,
        stalled: bool,
        confirming: bool,
        aligned: bool,
    ) -> bool:
        if self.delta_gate == "raw":
            return True
        if self.delta_gate == "location":
            return touched
        if self.delta_gate == "reclaim":
            return reclaimed and stalled and confirming
        return reclaimed and stalled and confirming and aligned

    def _candidate(
        self,
        candle: Candle,
        snapshot: Mapping[str, Any],
        direction: Direction,
    ) -> tuple[Optional[TradeSignal], Optional[dict[str, Any]]]:
        bars = [dict(row) for row in snapshot.get("bars") or []]
        profile = snapshot.get("profile")
        window = self.delta_window
        minimum_index = max(self.delta_baseline_window, 2 * window)
        index = len(bars) - 1
        if index < minimum_index:
            self._last_status = "warming_delta"
            return None, None
        if any(not _valid_ohlc(row) for row in bars[index - minimum_index:index + 1]):
            self._last_status = "waiting_for_complete_ohlc"
            return None, None
        recent_grid = bars[index - minimum_index:index + 1]
        if any(
            _bar_epoch(b) - _bar_epoch(a) != 60
            for a, b in zip(recent_grid, recent_grid[1:])
        ):
            self._last_status = "waiting_for_contiguous_mbo"
            return None, None
        if self.delta_source == "outside" and not profile:
            self._last_status = "waiting_for_prior_profile"
            return None, None

        values = self._source_values(bars, profile, direction)
        if values is None:
            self._last_status = "waiting_for_prior_profile"
            return None, None
        whole = [float(row.get("delta") if row.get("delta") is not None else (
            float(row.get("buy") or 0) - float(row.get("sell") or 0)
        )) for row in bars]
        scale_values = [abs(value) for value in whole[index - self.delta_baseline_window:index]]
        scale = max(1.0, float(median(scale_values)) if scale_values else 0.0)
        p0, p1, n0, n1 = self._window_changes(values, index, window)
        opp0, opp1 = (n0, n1) if direction == Direction.BUY else (p0, p1)
        absorption = (
            opp1 >= self.delta_strength * scale
            and opp1 >= self.delta_weakening * opp0
        )
        exhaustion = opp0 >= self.delta_strength * scale and opp1 <= self.delta_weakening * opp0

        recent = bars[index - window + 1:index + 1]
        old = bars[index - 2 * window + 1:index - window + 1]
        tolerance = self.delta_stall_ticks * self.tick_size
        if direction == Direction.BUY:
            stalled = min(float(row["low"]) for row in recent) >= (
                min(float(row["low"]) for row in old) - tolerance
            )
        else:
            stalled = max(float(row["high"]) for row in recent) <= (
                max(float(row["high"]) for row in old) + tolerance
            )

        touch_count = max(2 * window, self.delta_value_lookback)
        touched = False
        reclaimed = False
        if profile:
            touch_rows = bars[max(0, index - touch_count + 1):index + 1]
            touch_tolerance = self.delta_touch_ticks * self.tick_size
            if direction == Direction.BUY:
                touched = min(float(row["low"]) for row in touch_rows) <= float(profile["val"]) + touch_tolerance
                reclaimed = touched and float(bars[index]["close"]) > float(profile["val"])
            else:
                touched = max(float(row["high"]) for row in touch_rows) >= float(profile["vah"]) - touch_tolerance
                reclaimed = touched and float(bars[index]["close"]) < float(profile["vah"])

        previous_close = float(bars[index - 1]["close"])
        current_close = float(bars[index]["close"])
        confirming = (
            current_close - previous_close > 0
            if direction == Direction.BUY
            else current_close - previous_close < 0
        )
        vwap = snapshot.get("vwap")
        aligned = vwap is not None and (
            current_close - float(vwap) > 0
            if direction == Direction.BUY
            else current_close - float(vwap) < 0
        )
        family: Optional[str] = None
        if self.delta_pattern in ("absorption", "both") and absorption and stalled:
            family = "absorption"
        elif self.delta_pattern in ("exhaustion", "both") and exhaustion:
            family = "exhaustion"
        gate_passed = self._gate_accepts(
            direction, touched=touched, reclaimed=reclaimed,
            stalled=stalled, confirming=confirming, aligned=bool(aligned),
        )
        event_epoch = _bar_epoch(bars[index])
        self._record_chart_event(
            snapshot=snapshot,
            bars=bars,
            index=index,
            direction=direction,
            profile=profile,
            family=family,
            gate_passed=gate_passed,
            p0=p0,
            p1=p1,
            n0=n0,
            n1=n1,
            scale=scale,
            absorption=absorption,
            exhaustion=exhaustion,
            stalled=stalled,
            touched=touched,
            reclaimed=reclaimed,
            confirming=confirming,
            aligned=bool(aligned),
            vwap=vwap,
        )
        if not family or not gate_passed:
            self._last_status = "monitoring"
            return None, None

        if self._last_signal_epoch == event_epoch:
            return None, None
        width = self._atr_blend()
        if width is None or width <= 0:
            self._last_status = "warming_atr"
            return None, None
        if direction == Direction.SELL and self.sl_atr > 0:
            width *= self.pi_short_sl / self.sl_atr
        reason = (
            f"DELTA {family} w{window} {self.delta_source}/{self.delta_gate}"
            f" VA={'70%' if profile else 'none'}"
        )
        signal = self._make(candle, direction, reason, width=width)
        if signal is None:
            return None, None
        self._last_status = "confirmed"
        signal.meta.setdefault("delta_absorption", {}).update({
            "event_epoch": event_epoch,
            "pattern": family,
            "window": window,
            "source": self.delta_source,
            "gate": self.delta_gate,
            "positive_previous": round(p0, 4),
            "positive_recent": round(p1, 4),
            "negative_previous": round(n0, 4),
            "negative_recent": round(n1, 4),
            "scale": round(scale, 4),
            "stalled": stalled,
            "touched": touched,
            "reclaimed": reclaimed,
            "confirming": confirming,
            "vwap": round(float(vwap), 4) if vwap is not None else None,
            "previous_profile_date": snapshot.get("previous_profile_date"),
        })
        return signal, signal.meta["delta_absorption"]

    def evaluate(self, candle: Candle, zones=None, is_mature: bool = True):
        key = _epoch_minute(candle.timestamp)
        if self._observed_candle_key != key:
            self._roll(candle)
            self._observed_candle_key = key
        try:
            snapshot = self.context_provider.snapshot(candle.timestamp)
        except Exception:
            self._last_status = "mbo_context_error"
            return None
        self._last_context = snapshot
        if not snapshot:
            self._last_status = "waiting_for_mbo"
            return None
        if self.delta_require_profile and not snapshot.get("profile"):
            self._last_status = "waiting_for_prior_profile"
            return None
        event_epoch = _bar_epoch(snapshot.get("bar") or {})
        if event_epoch > 0:
            self._record_chart_value_area(snapshot, event_epoch)
        candidates: list[tuple[TradeSignal, dict[str, Any]]] = []
        for direction in (Direction.BUY, Direction.SELL):
            signal, meta = self._candidate(candle, snapshot, direction)
            if signal is not None and meta is not None:
                candidates.append((signal, meta))
        # A bar qualifying for both directions is ambiguous; abstain rather
        # than letting iteration order manufacture a trade.
        if len(candidates) != 1:
            if len(candidates) > 1:
                # ``_make`` records the daily attempt while constructing a
                # signal.  Roll those provisional records back when the bar is
                # two-sided and therefore must be rejected as ambiguous.
                trade_date = self._trade_date(candle.timestamp)
                self._daily[trade_date] = max(
                    0, self._daily.get(trade_date, 0) - len(candidates)
                )
                self._state = "idle"
                self._last_status = "ambiguous_two_sided_delta"
            return None
        self._last_signal_epoch = int(candidates[0][1]["event_epoch"])
        return candidates[0][0]

    def get_phase_label(self) -> str:
        status = self._last_status
        provider = self.context_provider.status() if hasattr(self.context_provider, "status") else {}
        if isinstance(provider, Mapping) and provider.get("state"):
            status = f"{status} · {provider.get('state')}"
        return f"{self.NAME} {status}"


def _overlay_session_dates(start: datetime, end: datetime) -> list[date]:
    """Return RTH session dates intersecting a chart request."""
    first = rth_session_date(start)
    last = rth_session_date(end)
    if last < first:
        first, last = last, first
    span = (last - first).days
    # The API already limits a chart request to 14 days. Keep this helper
    # defensive when called directly by a test or another local tool.
    if span > 31:
        last = first + timedelta(days=31)
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


def _overlay_cached_bars(
    provider: CachedDeltaContextProvider,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """Read only the compact MBO bars intersecting the requested window."""
    start_epoch = int(math.floor(as_utc(start).timestamp()))
    end_epoch = int(math.ceil(as_utc(end).timestamp()))
    rows: list[dict[str, Any]] = []
    for session_date in _overlay_session_dates(start, end):
        payload = provider._load_payload(session_date.isoformat())
        for row in _sorted_bars(payload):
            epoch = _bar_epoch(row)
            if start_epoch <= epoch <= end_epoch:
                rows.append(row)
    rows.sort(key=_bar_epoch)
    return rows


def _overlay_profile_area(
    session_date: date,
    profile_date: str,
    profile: Mapping[str, Any],
    start: datetime,
    end: datetime,
) -> Optional[dict[str, Any]]:
    """Clip one prior-RTH 70% profile segment to the visible request."""
    session_start, session_end = rth_session_bounds(session_date)
    visible_start = max(as_utc(start), session_start)
    visible_end = min(as_utc(end), session_end)
    if visible_end <= visible_start:
        return None
    try:
        vah = float(profile["vah"])
        val = float(profile["val"])
        poc = float(profile["poc"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return {
        "session_date": session_date.isoformat(),
        "profile_date": profile_date,
        "poc": round(poc, 4),
        "vah": round(vah, 4),
        "val": round(val, 4),
        "start_time": visible_start.isoformat(),
        "end_time": visible_end.isoformat(),
        "profile_pct": 0.70,
    }


def build_delta_chart_overlay(
    start: datetime,
    end: datetime,
    *,
    symbol: str = "MNQ",
    params: Any | None = None,
    root: str | Path | None = None,
    limit: int = 600,
) -> dict[str, Any]:
    """Replay compact MBO evidence for a visible chart window.

    This is intentionally separate from the trading backtest: selecting the
    chart layer must not create trades or replace the chart's candle workset.
    It uses the same ``DeltaAbsorptionStrategy`` and ``CachedDeltaContextProvider``
    as backtest/live, so the VAH/VAL and event definitions cannot drift. When
    the current session has no MBO cache yet, the prior complete 70% profile is
    still returned for context, while ``mbo_available`` stays false and no
    decay/no-break events are fabricated.
    """
    normalized_symbol = str(symbol or "MNQ").upper().replace("/", "")
    if "MNQ" not in normalized_symbol:
        return {
            "model": "delta_absorption",
            "available": False,
            "mbo_available": False,
            "status": "mnq_only",
            "events": [],
            "value_areas": [],
            "mbo_bars": 0,
            "files": [],
        }

    start_utc = as_utc(start)
    end_utc = as_utc(end)
    if end_utc < start_utc:
        raise ValueError("end must not be earlier than start")

    effective_params = params or StrategyParams(strategy="delta_absorption")
    effective_params.strategy = "delta_absorption"
    require_profile = bool(getattr(effective_params, "delta_require_profile", True))
    provider = CachedDeltaContextProvider(
        symbol="MNQ",
        root=root,
        tick_size=float(getattr(effective_params, "tick_size", DEFAULT_TICK_SIZE) or DEFAULT_TICK_SIZE),
        require_complete_profile=require_profile,
    )
    rows = _overlay_cached_bars(provider, start_utc, end_utc)
    strategy = DeltaAbsorptionStrategy(effective_params, context_provider=provider)
    for row in rows:
        epoch = _bar_epoch(row)
        if epoch <= 0 or not _valid_ohlc(row):
            continue
        try:
            candle = Candle(
                # The compact row at ``epoch`` is a completed MBO minute. Feed
                # the same row to the strategy one minute later so the
                # provider's causal eligibility rule can expose that row and
                # the final visible minute is not silently omitted.
                timestamp=datetime.fromtimestamp(epoch + 60, UTC),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=int(row.get("trades") or row.get("volume") or 0),
                symbol="MNQ",
                interval="1m",
                source="databento_mbo",
            )
            strategy.evaluate(candle, [], True)
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            # One malformed compact row must not blank the rest of the chart
            # overlay. The ingestion validator remains responsible for fixing
            # the source cache itself.
            continue

    overlay = strategy.get_chart_overlay(limit=limit)
    # A visible value-area line is useful even on a quiet session with no
    # event. Merge the strategy's event-driven areas with the same prior-RTH
    # profile for every intersecting session. This also makes the source/date
    # semantics explicit to the UI.
    areas_by_session = {
        str(area.get("session_date")): dict(area)
        for area in overlay.get("value_areas", [])
        if isinstance(area, Mapping) and area.get("session_date")
    }
    for session_date in _overlay_session_dates(start_utc, end_utc):
        profile_date, profile = provider._previous_profile(session_date.isoformat())
        if not profile or not profile_date:
            continue
        area = _overlay_profile_area(
            session_date, profile_date, profile, start_utc, end_utc,
        )
        if area is None:
            continue
        current = areas_by_session.get(area["session_date"])
        if current is None:
            areas_by_session[area["session_date"]] = area
        else:
            current["start_time"] = min(current["start_time"], area["start_time"])
            current["end_time"] = max(current["end_time"], area["end_time"])
            current["profile_pct"] = 0.70
    overlay["value_areas"] = [
        areas_by_session[key] for key in sorted(areas_by_session)
    ]
    overlay.update({
        "available": bool(rows),
        "mbo_available": bool(rows),
        "mbo_bars": len(rows),
        "status": (
            "ok" if rows else
            "prior_profile_only" if overlay["value_areas"] else
            "mbo_unavailable"
        ),
        "files": [
            f"footprint_mnq_{session_date.isoformat()}.json.gz"
            for session_date in _overlay_session_dates(start_utc, end_utc)
            if provider._load_payload(session_date.isoformat())
        ],
        "requested_start": start_utc.isoformat(),
        "requested_end": end_utc.isoformat(),
    })
    return overlay
