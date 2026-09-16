"""Small, reconnecting Databento MBO feed for Delta+VA live context.

Only compact one-minute rows are retained in memory and under the canonical
primary runtime state directory.  Raw Databento records are never handed to
the chart or strategy and the API key is never included in status/log output.
"""
from __future__ import annotations

import os
import re
import threading
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Mapping, Optional

from backend.data import market_data
from backend.data.orderflow import (
    CACHE_SCHEMA_VERSION,
    DEFAULT_TICK_SIZE,
    LAST_EVENT_FLAG,
    NANO,
    _MboAggregator,
    all_session_footprint_cache_path,
    read_footprint_cache,
    write_footprint_cache,
)
from backend.strategy.delta_absorption import CachedDeltaContextProvider
from backend.strategy.session_filter import (
    MARKET_TIMEZONE,
    market_session_code,
    rth_session_bounds,
    rth_session_date,
)
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
        # RTH remains the strategy context.  This separate UTC-day stream
        # retains every received MBO event's compact 1-minute footprint so
        # ASIA/EURO/PRE/RTH/AH are available for research and audits too.
        self._all_session_date: Optional[str] = None
        self._all_session_minute: Optional[int] = None
        self._all_session_aggregator: Optional[_MboAggregator] = None
        self._all_session_days: dict[str, dict[int, dict[str, Any]]] = {}
        self._all_session_last_completed_bar: Optional[int] = None
        self._last_session_code: Optional[str] = None
        self._last_record_at: Optional[datetime] = None
        self._last_record_latency_ms: Optional[float] = None
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

    def _all_session_runtime_path(self, calendar_date: str) -> Path:
        return market_data.runtime_path(
            "state",
            f"databento_live_all_sessions_{self.symbol.lower()}_{calendar_date}.json.gz",
            root=self.root,
        )

    @staticmethod
    def _merge_payloads(
        derived: Mapping[str, Any],
        runtime: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Overlay newer runtime bars without discarding derived rows."""
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
        return self._merge_payloads(derived, runtime)

    def _all_session_payload(self, calendar_date: str) -> Optional[dict[str, Any]]:
        rows = self._all_session_days.get(calendar_date)
        if not rows:
            return None
        return {
            "meta": {
                "schema_version": CACHE_SCHEMA_VERSION,
                "calendar_date": calendar_date,
                "symbol": self.symbol,
                "source_schema": "mbo",
                "interval": "1m",
                "interval_seconds": 60,
                "tick_size": self.tick_size,
                "session": "ALL",
                "session_codes": ["ASIA", "EURO", "PRE", "RTH", "AH"],
                "session_time_zone": MARKET_TIMEZONE.key,
                "bucket": "UTC_DAY",
                "provider": self.provider_name,
            },
            "bars": [dict(rows[key]) for key in sorted(rows)],
        }

    def _load_persisted_all_session_payload(
        self,
        calendar_date: str,
    ) -> Optional[dict[str, Any]]:
        key = str(calendar_date)
        try:
            runtime = read_footprint_cache(self._all_session_runtime_path(key))
        except (FileNotFoundError, OSError, ValueError, EOFError):
            runtime = None
        derived_path = all_session_footprint_cache_path(
            key,
            self.symbol,
            root=self.root,
        )
        try:
            derived = read_footprint_cache(derived_path)
        except (FileNotFoundError, OSError, ValueError, EOFError):
            derived = None
        if not runtime:
            return derived
        if not derived:
            return runtime
        return self._merge_payloads(derived, runtime)

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

    def _persist_all_session_day(self, calendar_date: str) -> None:
        with self._lock:
            payload = self._all_session_payload(calendar_date)
        if payload is not None:
            try:
                write_footprint_cache(
                    payload,
                    self._all_session_runtime_path(calendar_date),
                )
            except OSError:
                # The compact all-session archive is best effort, just like
                # the existing RTH checkpoint.  The active feed must not die
                # because a runtime disk write was temporarily unavailable.
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
            current_utc = datetime.now(UTC).date().isoformat()
            all_cached = self._load_persisted_all_session_payload(current_utc)
            all_rows = {
                int(row.get("epoch") or 0): dict(row)
                for row in (all_cached or {}).get("bars") or []
                if isinstance(row, Mapping) and int(row.get("epoch") or 0) > 0
            }
            if all_rows:
                self._all_session_days[current_utc] = all_rows
                self._all_session_date = current_utc
                self._all_session_last_completed_bar = max(all_rows)
            self._seeded = True

    def _new_aggregator(self, trade_date: str) -> _MboAggregator:
        return _MboAggregator(
            trade_date,
            tick_size=self.tick_size,
            time_zone=LOS_ANGELES.key,
            snapshot_flag=self._snapshot_flag,
            last_event_flag=LAST_EVENT_FLAG,
        )

    def _new_all_session_aggregator(self, calendar_date: str) -> _MboAggregator:
        start = datetime.combine(
            date.fromisoformat(calendar_date), time.min, tzinfo=UTC,
        )
        return _MboAggregator(
            calendar_date,
            tick_size=self.tick_size,
            time_zone=MARKET_TIMEZONE.key,
            snapshot_flag=self._snapshot_flag,
            last_event_flag=LAST_EVENT_FLAG,
            session="ALL",
            session_start_ns=int(start.timestamp() * NANO),
            session_end_ns=int((start + timedelta(days=1)).timestamp() * NANO),
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

    def _publish_all_session_aggregator_locked(self) -> Optional[str]:
        if self._all_session_aggregator is None or self._all_session_date is None:
            return None
        payload = self._all_session_aggregator.finish()
        rows = self._all_session_days.setdefault(self._all_session_date, {})
        for row in payload.get("bars") or []:
            epoch = int(row.get("epoch") or 0)
            if epoch:
                rows[epoch] = dict(row)
        if (
            self._all_session_minute is not None
            and self._all_session_minute in rows
        ):
            self._all_session_last_completed_bar = self._all_session_minute
        return self._all_session_date

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
        record_latency_ms = round(
            max(0.0, (datetime.now(UTC) - event).total_seconds()) * 1000, 1
        )
        action = str(getattr(record, "action", "") or "").upper()
        try:
            flags = int(getattr(record, "flags", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            flags = 0
        is_snapshot = bool(self._snapshot_flag and flags & self._snapshot_flag)
        trade_date = rth_session_date(event).isoformat()
        persist_dates: set[str] = set()
        persist_all_dates: set[str] = set()
        with self._lock:
            self._last_record_latency_ms = record_latency_ms
            # Snapshot records seed the current book even though their event
            # time is outside RTH. They never create a footprint bar.
            if is_snapshot or action == "R":
                if self._active_trade_date is None:
                    self._active_trade_date = rth_session_date(datetime.now(UTC)).isoformat()
                if self._aggregator is None:
                    self._aggregator = self._new_aggregator(self._active_trade_date)
                self._aggregator.consume(record)
                current_utc = datetime.now(UTC).date().isoformat()
                if self._all_session_date is None:
                    self._all_session_date = current_utc
                if self._all_session_aggregator is None:
                    self._all_session_aggregator = self._new_all_session_aggregator(
                        self._all_session_date,
                    )
                self._all_session_aggregator.consume(record)
                return

            # The record-only stream is intentionally retained for every UTC
            # day.  Each compact bar is labelled with the shared New-York
            # market session in _MboAggregator.finish().
            calendar_date = event.date().isoformat()
            if self._all_session_date != calendar_date:
                old_all = self._publish_all_session_aggregator_locked()
                if old_all:
                    persist_all_dates.add(old_all)
                self._all_session_date = calendar_date
                self._all_session_minute = None
                self._all_session_aggregator = self._new_all_session_aggregator(
                    calendar_date,
                )
            if self._all_session_aggregator is None:
                self._all_session_aggregator = self._new_all_session_aggregator(
                    calendar_date,
                )
            minute = int(ts_ns // (60 * NANO) * 60)
            if (
                self._all_session_minute is not None
                and minute != self._all_session_minute
            ):
                old_all = self._publish_all_session_aggregator_locked()
                if old_all:
                    persist_all_dates.add(old_all)
            self._all_session_minute = minute
            self._all_session_aggregator.consume(record)
            self._all_session_days.setdefault(calendar_date, {})
            self._last_record_at = event
            self._record_count += 1
            self._last_session_code = market_session_code(event)

            # Keep the strategy-facing RTH stream and its exact 390-bar
            # profile contract unchanged.
            if not self._record_in_rth(event, trade_date):
                pass
            else:
                if self._active_trade_date != trade_date:
                    old = self._publish_aggregator_locked()
                    if old:
                        persist_dates.add(old)
                    self._active_trade_date = trade_date
                    self._active_minute = None
                    self._aggregator = self._new_aggregator(trade_date)
                if self._aggregator is None:
                    self._aggregator = self._new_aggregator(trade_date)
                if self._active_minute is not None and minute != self._active_minute:
                    old = self._publish_aggregator_locked()
                    if old:
                        persist_dates.add(old)
                self._active_minute = minute
                self._aggregator.consume(record)
                self._live_days.setdefault(trade_date, {})
        for value in persist_dates:
            self._persist_day(value)
        for value in persist_all_dates:
            self._persist_all_session_day(value)

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
            persist_all_date = None
            with self._lock:
                persist_date = self._publish_aggregator_locked()
                persist_all_date = self._publish_all_session_aggregator_locked()
                self._client = None
            if persist_date:
                self._persist_day(persist_date)
            if persist_all_date:
                self._persist_all_session_day(persist_all_date)

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
            all_session_date = self._publish_all_session_aggregator_locked()
        if trade_date:
            self._persist_day(trade_date)
        if all_session_date:
            self._persist_all_session_day(all_session_date)

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = self._state
            active_date = self._active_trade_date
            all_session_date = self._all_session_date
            last_record = self._last_record_at.isoformat() if self._last_record_at else None
            last_bar = self._last_completed_bar
            count = len(self._live_days.get(active_date, {})) if active_date else 0
            all_count = (
                len(self._all_session_days.get(all_session_date, {}))
                if all_session_date else 0
            )
            all_last_bar = self._all_session_last_completed_bar
            session_code = self._last_session_code
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
            "session_storage": "ALL",
            "capture_sessions": ["ASIA", "EURO", "PRE", "RTH", "AH"],
            "current_trade_date": active_date,
            "current_utc_date": all_session_date,
            "current_session": session_code,
            "last_record_at": last_record,
            "latency_ms": self._last_record_latency_ms,
            "record_count": self._record_count,
            "last_completed_bar": last_bar,
            "bar_count": count,
            "all_session_last_completed_bar": all_last_bar,
            "all_session_bar_count": all_count,
            "prior_profile": profile,
            "prior_profile_date": profile_date,
            "ready": bool(state == "connected" and profile and last_bar),
            "error": error,
        }


# The chart/research application needs a record-only MBO stream even when the
# selected live strategy is PI (or no live strategy is running).  Keep one
# process-owned feed so startup capture and Delta Absorption live use never
# create two Databento subscriptions for the same contract.
_AUTO_MBO_ENV = "ANCSERTPX_AUTO_DATABENTO_MBO"
_PROCESS_FEED_LOCK = threading.RLock()
_process_mbo_feed: Optional[DatabentoMboLiveFeed] = None


def databento_mbo_auto_enabled() -> bool:
    """Whether the host application opted into an unattended MBO recorder."""
    return os.environ.get(_AUTO_MBO_ENV, "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _default_mbo_contract_id() -> str:
    from backend.db.models import current_quarterly_contract_id
    from backend.timebase import utc_now

    return current_quarterly_contract_id("MNQ", utc_now())


def acquire_databento_mbo_feed(
    contract_id: str | None = None,
    *,
    tick_size: float = DEFAULT_TICK_SIZE,
) -> Optional[DatabentoMboLiveFeed]:
    """Return the app-owned MBO feed when auto-recording is enabled.

    A Delta live engine borrows this same object.  If the requested contract is
    different from the startup recorder's contract, return ``None`` so the
    engine can keep its explicit per-engine feed rather than silently changing
    the recorder underneath another consumer.
    """
    global _process_mbo_feed

    if not databento_mbo_auto_enabled() or not _read_dotenv_key():
        return None

    requested = str(contract_id or _default_mbo_contract_id()).strip()
    fallback = os.environ.get("DATABENTO_MNQ_SYMBOL", "MNQU6")
    requested_raw = databento_raw_symbol(requested, fallback)

    with _PROCESS_FEED_LOCK:
        feed = _process_mbo_feed
        if feed is None:
            feed = DatabentoMboLiveFeed(
                contract_id=requested,
                symbol="MNQ",
                tick_size=tick_size,
            )
            _process_mbo_feed = feed
        elif feed.raw_symbol != requested_raw:
            return None

    # start() is idempotent and only starts the feed's daemon worker; the
    # network handshake happens in that worker, outside the FastAPI loop.
    feed.start()
    return feed


def start_databento_mbo_recorder(contract_id: str | None = None) -> bool:
    """Start the record-only MBO stream used by the native application."""
    return acquire_databento_mbo_feed(contract_id) is not None


def databento_mbo_recorder_status() -> Optional[dict[str, Any]]:
    """Return the non-sensitive status of the process-owned recorder."""
    with _PROCESS_FEED_LOCK:
        feed = _process_mbo_feed
    if feed is None:
        return None
    status = feed.status()
    status.update({
        "record_only": True,
        "owner": "app",
        "contract_id": feed.contract_id,
    })
    return status


def stop_databento_mbo_recorder() -> None:
    """Stop and release the process-owned feed during app shutdown."""
    global _process_mbo_feed

    with _PROCESS_FEED_LOCK:
        feed = _process_mbo_feed
        _process_mbo_feed = None
    if feed is not None:
        feed.stop()
