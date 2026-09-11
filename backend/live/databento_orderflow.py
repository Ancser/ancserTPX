"""Small, reconnecting Databento MBO feed for Delta+VA live context.

Only compact one-minute rows are retained in memory and under the canonical
primary runtime state directory.  Raw Databento records are never handed to
the chart or strategy and the API key is never included in status/log output.
"""
from __future__ import annotations

import os
import re
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Optional

from backend.data import market_data
from backend.data.orderflow import (
    CACHE_SCHEMA_VERSION,
    DEFAULT_TICK_SIZE,
    LAST_EVENT_FLAG,
    NANO,
    _MboAggregator,
    read_footprint_cache,
    write_footprint_cache,
)
from backend.strategy.delta_absorption import CachedDeltaContextProvider
from backend.strategy.session_filter import rth_session_bounds, rth_session_date
from backend.timebase import LOS_ANGELES, UTC, as_utc


_MONTH_CODES = "FGHJKMNQUVXZ"


def databento_raw_symbol(contract_id: str, fallback: str = "MNQU6") -> str:
    """Map ``CON.F.US.MNQ.U26`` to Databento's raw ``MNQU6`` symbol."""
    value = str(contract_id or "").strip().upper()
    match = re.search(r"(?:^|\.)([A-Z]{2,4})\.([%s])(\d{1,2})(?:$|\.)" % _MONTH_CODES, value)
    if match:
        return f"{match.group(1)}{match.group(2)}{match.group(3)[-1]}"
    compact = re.fullmatch(r"([A-Z]{2,4})([%s])(\d{1,2})" % _MONTH_CODES, value)
    if compact:
        return f"{compact.group(1)}{compact.group(2)}{compact.group(3)[-1]}"
    return str(fallback or "MNQU6").upper()


def _read_dotenv_key() -> str:
    key = os.environ.get("DATABENTO_API_KEY", "").strip()
    if key:
        return key
    env_path = Path(__file__).resolve().parents[2] / ".env"
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() == "DATABENTO_API_KEY":
                return value.strip().strip("'\"")
    except (FileNotFoundError, OSError):
        pass
    return ""


class DatabentoMboLiveFeed(CachedDeltaContextProvider):
    """Thread-owned Databento MBO client with a compact context-provider API."""

    provider_name = "databento_live_mbo"

    def __init__(
        self,
        *,
        contract_id: str,
        symbol: str | None = None,
        dataset: str | None = None,
        api_key: str | None = None,
        root: str | Path | None = None,
        tick_size: float = DEFAULT_TICK_SIZE,
    ) -> None:
        product = str(symbol or "MNQ").upper()
        if "." in product:
            product = "MNQ" if "MNQ" in product else product
        super().__init__(
            symbol=product, root=root, tick_size=tick_size,
            require_complete_profile=True,
        )
        self.contract_id = str(contract_id or "")
        self.dataset = str(dataset or os.environ.get("DATABENTO_DATASET", "GLBX.MDP3")).strip()
        self.raw_symbol = databento_raw_symbol(
            self.contract_id,
            os.environ.get("DATABENTO_MNQ_SYMBOL", "MNQU6"),
        )
        self._api_key_override = api_key
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._client: Any = None
        self._state = "idle"
        self._error: Optional[str] = None
        self._snapshot_flag = 0
        self._active_trade_date: Optional[str] = None
        self._active_minute: Optional[int] = None
        self._aggregator: Optional[_MboAggregator] = None
        self._live_days: dict[str, dict[int, dict[str, Any]]] = {}
        self._last_record_at: Optional[datetime] = None
        self._last_completed_bar: Optional[int] = None
        self._record_count = 0
        self._seeded = False

    def _live_payload(self, trade_date: str) -> Optional[dict[str, Any]]:
        rows = self._live_days.get(trade_date)
        if not rows:
            return None
        return {
            "meta": {
                "schema_version": CACHE_SCHEMA_VERSION,
                "trade_date": trade_date,
                "symbol": self.symbol,
                "source_schema": "mbo",
                "interval": "1m",
                "interval_seconds": 60,
                "tick_size": self.tick_size,
                "session": "RTH",
                "session_time_zone": LOS_ANGELES.key,
                "provider": self.provider_name,
            },
            "bars": [dict(rows[key]) for key in sorted(rows)],
        }

    def _runtime_path(self, trade_date: str) -> Path:
        return market_data.runtime_path(
            "state", f"databento_live_footprint_{self.symbol.lower()}_{trade_date}.json.gz",
            root=self.root,
        )

    def _load_persisted_payload(self, trade_date: str) -> Optional[dict[str, Any]]:
        """Load the compact live checkpoint, merged over any derived cache.

        The runtime checkpoint may contain today's bars collected after the
        last historical-cache build. Keep it as the newest row source while
        retaining any earlier rows that were already derived.
        """
        key = str(trade_date)
        try:
            runtime = read_footprint_cache(self._runtime_path(key))
        except (FileNotFoundError, OSError, ValueError, EOFError):
            runtime = None
        derived = super()._load_payload(key)
        if not runtime:
            return derived
        if not derived:
            return runtime
        merged = dict(derived)
        merged_meta = dict(derived.get("meta") or {})
        merged_meta.update(runtime.get("meta") or {})
        merged["meta"] = merged_meta
        rows = {
            int(row.get("epoch") or 0): dict(row)
            for row in derived.get("bars") or []
            if isinstance(row, Mapping) and int(row.get("epoch") or 0) > 0
        }
        rows.update({
            int(row.get("epoch") or 0): dict(row)
            for row in runtime.get("bars") or []
            if isinstance(row, Mapping) and int(row.get("epoch") or 0) > 0
        })
        merged["bars"] = [rows[epoch] for epoch in sorted(rows)]
        return merged

    def _candidate_dates(self) -> list[str]:
        """Include live-only checkpoints when selecting a prior VA session."""
        dates = set(super()._candidate_dates())
        state_dir = market_data.runtime_path("state", root=self.root)
        prefix = f"databento_live_footprint_{self.symbol.lower()}_"
        try:
            paths = state_dir.glob(f"{prefix}*.json.gz")
        except OSError:
            paths = ()
        for path in paths:
            name = path.name
            if not name.startswith(prefix) or not name.endswith(".json.gz"):
                continue
            value = name[len(prefix):-len(".json.gz")]
            try:
                dates.add(date.fromisoformat(value).isoformat())
            except ValueError:
                continue
        return sorted(dates)

    def _load_payload(self, trade_date: str) -> Optional[dict[str, Any]]:
        key = str(trade_date)
        with self._lock:
            live = self._live_payload(key)
        if live is not None:
            return live
        return self._load_persisted_payload(key)

    def _persist_day(self, trade_date: str) -> None:
        with self._lock:
            payload = self._live_payload(trade_date)
        if payload is not None:
            try:
                write_footprint_cache(payload, self._runtime_path(trade_date))
            except OSError:
                # Persistence is best effort; a feed failure must still leave
                # status and in-memory safety visible to the engine.
                pass

    def _seed_current_day(self) -> None:
        current = rth_session_date(datetime.now(UTC)).isoformat()
        cached = self._load_persisted_payload(current)
        rows = {
            int(row.get("epoch") or 0): dict(row)
            for row in (cached or {}).get("bars") or []
            if isinstance(row, Mapping) and int(row.get("epoch") or 0) > 0
        }
        with self._lock:
            if rows:
                self._live_days[current] = rows
                self._active_trade_date = current
                self._last_completed_bar = max(rows)
            self._seeded = True

    def _new_aggregator(self, trade_date: str) -> _MboAggregator:
        return _MboAggregator(
            trade_date,
            tick_size=self.tick_size,
            time_zone=LOS_ANGELES.key,
            snapshot_flag=self._snapshot_flag,
            last_event_flag=LAST_EVENT_FLAG,
        )

    def _publish_aggregator_locked(self) -> Optional[str]:
        if self._aggregator is None or self._active_trade_date is None:
            return None
        payload = self._aggregator.finish()
        rows = self._live_days.setdefault(self._active_trade_date, {})
        for row in payload.get("bars") or []:
            epoch = int(row.get("epoch") or 0)
            if epoch:
                rows[epoch] = dict(row)
        if self._active_minute is not None and self._active_minute in rows:
            self._last_completed_bar = self._active_minute
        self._payload_cache.pop(self._active_trade_date, None)
        self._profile_cache.clear()
        return self._active_trade_date

    def _record_in_rth(self, event: datetime, trade_date: str) -> bool:
        start, end = rth_session_bounds(trade_date)
        return start <= event < end

    def _on_record(self, record: Any) -> None:
        try:
            ts_ns = int(getattr(record, "ts_event", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            return
        if ts_ns <= 0:
            return
        event = datetime.fromtimestamp(ts_ns / NANO, tz=UTC)
        action = str(getattr(record, "action", "") or "").upper()
        try:
            flags = int(getattr(record, "flags", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            flags = 0
        is_snapshot = bool(self._snapshot_flag and flags & self._snapshot_flag)
        trade_date = rth_session_date(event).isoformat()
        persist_dates: set[str] = set()
        with self._lock:
            # Snapshot records seed the current book even though their event
            # time is outside RTH. They never create a footprint bar.
            if is_snapshot or action == "R":
                if self._active_trade_date is None:
                    self._active_trade_date = rth_session_date(datetime.now(UTC)).isoformat()
                if self._aggregator is None:
                    self._aggregator = self._new_aggregator(self._active_trade_date)
                self._aggregator.consume(record)
                return
            if not self._record_in_rth(event, trade_date):
                return
            if self._active_trade_date != trade_date:
                old = self._publish_aggregator_locked()
                if old:
                    persist_dates.add(old)
                self._active_trade_date = trade_date
                self._active_minute = None
                self._aggregator = self._new_aggregator(trade_date)
            if self._aggregator is None:
                self._aggregator = self._new_aggregator(trade_date)
            minute = int(ts_ns // (60 * NANO) * 60)
            if self._active_minute is not None and minute != self._active_minute:
                old = self._publish_aggregator_locked()
                if old:
                    persist_dates.add(old)
            self._active_minute = minute
            self._aggregator.consume(record)
            self._live_days.setdefault(trade_date, {})
            self._last_record_at = event
            self._record_count += 1
        for value in persist_dates:
            self._persist_day(value)

    def _on_exception(self, exc: BaseException) -> None:
        with self._lock:
            self._error = f"{type(exc).__name__}: {exc}"
            self._state = "error"

    def _set_error(self, message: str) -> None:
        with self._lock:
            self._error = message
            self._state = "error"

    def _run(self) -> None:
        client = None
        try:
            import databento as db
            self._snapshot_flag = int(getattr(db.RecordFlags, "F_SNAPSHOT", 0) or 0)
            key = self._api_key_override or _read_dotenv_key()
            if not key:
                self._set_error("DATABENTO_API_KEY is not configured")
                return
            client = db.Live(
                key=key,
                reconnect_policy=db.ReconnectPolicy.RECONNECT,
                heartbeat_interval_s=30,
            )
            with self._lock:
                self._client = client
            client.add_callback(self._on_record, self._on_exception)
            client.subscribe(
                self.dataset,
                "mbo",
                symbols=self.raw_symbol,
                stype_in="raw_symbol",
                snapshot=True,
            )
            client.start()
            with self._lock:
                if self._state not in ("error", "stopped"):
                    self._state = "connected"
                    self._error = None
            client.block_for_close()
            with self._lock:
                if self._state == "connected" and not self._stop_event.is_set():
                    self._state = "stopped"
        except Exception as exc:
            self._set_error(f"{type(exc).__name__}: {exc}")
        finally:
            persist_date = None
            with self._lock:
                persist_date = self._publish_aggregator_locked()
                self._client = None
            if persist_date:
                self._persist_day(persist_date)

    def start(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self._state in ("starting", "connected")
            self._seed_current_day()
            key = self._api_key_override or _read_dotenv_key()
            if not key:
                self._state = "missing_key"
                self._error = "DATABENTO_API_KEY is not configured"
                return False
            self._stop_event.clear()
            self._error = None
            self._state = "starting"
            self._thread = threading.Thread(
                target=self._run,
                name="ancserTPX-databento-mbo",
                daemon=True,
            )
            self._thread.start()
            return True

    def stop(self) -> None:
        with self._lock:
            self._stop_event.set()
            client = self._client
            thread = self._thread
        if client is not None:
            for name in ("stop", "terminate"):
                method = getattr(client, name, None)
                if method is None:
                    continue
                try:
                    method()
                except Exception:
                    pass
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)
        with self._lock:
            if self._state not in ("missing_key", "error"):
                self._state = "stopped"
            trade_date = self._publish_aggregator_locked()
        if trade_date:
            self._persist_day(trade_date)

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = self._state
            active_date = self._active_trade_date
            last_record = self._last_record_at.isoformat() if self._last_record_at else None
            last_bar = self._last_completed_bar
            count = len(self._live_days.get(active_date, {})) if active_date else 0
            error = self._error
        profile_date = None
        profile = None
        if active_date:
            profile_date, profile = self._previous_profile(active_date)
        return {
            "enabled": True,
            "state": state,
            "connected": state == "connected",
            "dataset": self.dataset,
            "schema": "mbo",
            "symbol": self.raw_symbol,
            "current_trade_date": active_date,
            "last_record_at": last_record,
            "last_completed_bar": last_bar,
            "bar_count": count,
            "prior_profile": profile,
            "prior_profile_date": profile_date,
            "ready": bool(state == "connected" and profile and last_bar),
            "error": error,
        }
