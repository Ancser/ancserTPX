"""Bounded, best-effort EMAPMO signal delivery for live trading.

The module is deliberately independent from the trading engine.  It copies the
completed bars and the exact PMO/SIG arrays already owned by the live strategy,
then performs chart rendering, persistence, and network I/O on a background
worker.  Notification failures therefore never affect order placement.

Only signal metadata is retained on disk.  PNGs exist only as short-lived bytes
while a message is being sent.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import math
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv


logger = logging.getLogger(__name__)

_UTC = timezone.utc
_PLOT_LOCK = threading.Lock()
_STOP_TIMEOUT_SECONDS = 8.0


def _utc(value: Optional[datetime]) -> datetime:
    if value is None:
        return datetime.now(_UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=_UTC)
    return value.astimezone(_UTC)


def _iso_utc(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _display_timezone(name: str):
    try:
        return ZoneInfo(str(name or "America/Chicago"))
    except Exception:
        return _UTC


def _finite(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _symbol_root(contract_id: str) -> str:
    upper = str(contract_id or "").upper()
    for symbol in ("MNQ", "NQ", "MES", "ES", "MGC", "GC"):
        if symbol in upper:
            return symbol
    pieces = [p for p in upper.replace("-", ".").split(".") if p]
    return pieces[-1] if pieces else "UNKNOWN"


def _direction_name(value: Any) -> str:
    raw = str(getattr(value, "value", value) or "").strip().lower()
    if raw in {"buy", "long", "1"}:
        return "long"
    if raw in {"sell", "short", "2"}:
        return "short"
    return raw or "unknown"


def _safe_error(exc: BaseException) -> str:
    """Return a secret-free error label suitable for logs and SQLite."""
    if isinstance(exc, DiscordSendError):
        return exc.code[:200]
    return exc.__class__.__name__[:200]


@dataclass(frozen=True)
class _Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True)
class _SignalSnapshot:
    event_key: str
    created_at_epoch: int
    source_time: datetime
    signal_time: datetime
    contract_id: str
    symbol: str
    timeframe: str
    timezone_name: str
    signal_mode: str
    direction: str
    contract_size: int
    entry: Optional[float]
    sl: Optional[float]
    tp: Optional[float]
    pmo_value: Optional[float]
    signal_value: Optional[float]
    bars: tuple[_Bar, ...]
    pmo_series: tuple[Optional[float], ...]
    signal_series: tuple[Optional[float], ...]

    @staticmethod
    def _series_at(values: tuple[Optional[float], ...], offset: int) -> Optional[float]:
        if len(values) < abs(offset):
            return None
        value = values[offset]
        return None if value is None else float(value)

    def _condition_values(self) -> dict[str, Optional[float]]:
        pmo_now = self.pmo_value
        sig_now = self.signal_value
        if pmo_now is None:
            pmo_now = self._series_at(self.pmo_series, -1)
        if sig_now is None:
            sig_now = self._series_at(self.signal_series, -1)
        return {
            "pmo_now": None if pmo_now is None else float(pmo_now),
            "sig_now": None if sig_now is None else float(sig_now),
            "pmo_prev": self._series_at(self.pmo_series, -2),
            "sig_prev": self._series_at(self.signal_series, -2),
            "pmo_prev2": self._series_at(self.pmo_series, -3),
            "sig_prev2": self._series_at(self.signal_series, -3),
        }

    def matched_signal_mode(self) -> str:
        """Return the branch that fired when the configured mode is ``both``."""
        configured = str(self.signal_mode or "normal").strip().lower()
        if configured in {"normal", "early"}:
            return configured

        values = self._condition_values()
        pmo_now = values["pmo_now"]
        sig_now = values["sig_now"]
        pmo_prev = values["pmo_prev"]
        sig_prev = values["sig_prev"]
        pmo_prev2 = values["pmo_prev2"]
        sig_prev2 = values["sig_prev2"]
        if None in (pmo_now, sig_now, pmo_prev, sig_prev):
            return "both"

        assert pmo_now is not None and sig_now is not None
        assert pmo_prev is not None and sig_prev is not None
        if self.direction == "long":
            if pmo_now < -0.10 and pmo_now > sig_now and pmo_prev <= sig_prev:
                return "normal"
            if None not in (pmo_prev2, sig_prev2):
                assert pmo_prev2 is not None and sig_prev2 is not None
                gaps = (sig_now - pmo_now, sig_prev - pmo_prev, sig_prev2 - pmo_prev2)
                if sig_now < -0.10 and pmo_now < sig_now and gaps[0] < gaps[1] < gaps[2]:
                    return "early"
        else:
            if pmo_now > 0.06 and pmo_now < sig_now and pmo_prev >= sig_prev:
                return "normal"
            if None not in (pmo_prev2, sig_prev2):
                assert pmo_prev2 is not None and sig_prev2 is not None
                gaps = (pmo_now - sig_now, pmo_prev - sig_prev, pmo_prev2 - sig_prev2)
                if sig_now > 0.06 and pmo_now > sig_now and gaps[0] < gaps[1] < gaps[2]:
                    return "early"
        return "both"

    @staticmethod
    def _format_indicator(value: Optional[float]) -> str:
        return "?" if value is None else f"{value:.5f}"

    def condition_lines(self) -> list[str]:
        values = self._condition_values()
        pmo_now = values["pmo_now"]
        sig_now = values["sig_now"]
        pmo_prev = values["pmo_prev"]
        sig_prev = values["sig_prev"]
        pmo_prev2 = values["pmo_prev2"]
        sig_prev2 = values["sig_prev2"]
        pmo = self._format_indicator(pmo_now)
        sig = self._format_indicator(sig_now)
        mode = self.matched_signal_mode()

        if mode == "early" and self.direction == "long":
            rows = [
                f"SIG < -0.10000: {sig}",
                f"PMO < SIG: {pmo} < {sig}",
            ]
            if None not in (pmo_now, sig_now, pmo_prev, sig_prev, pmo_prev2, sig_prev2):
                assert pmo_now is not None and sig_now is not None
                assert pmo_prev is not None and sig_prev is not None
                assert pmo_prev2 is not None and sig_prev2 is not None
                rows.append(
                    "SIG-PMO: "
                    f"NOW {sig_now - pmo_now:.5f} < "
                    f"PREV {sig_prev - pmo_prev:.5f} < "
                    f"PREV2 {sig_prev2 - pmo_prev2:.5f}"
                )
            return rows

        if mode == "early" and self.direction == "short":
            rows = [
                f"SIG > 0.06000: {sig}",
                f"PMO > SIG: {pmo} > {sig}",
            ]
            if None not in (pmo_now, sig_now, pmo_prev, sig_prev, pmo_prev2, sig_prev2):
                assert pmo_now is not None and sig_now is not None
                assert pmo_prev is not None and sig_prev is not None
                assert pmo_prev2 is not None and sig_prev2 is not None
                rows.append(
                    "PMO-SIG: "
                    f"NOW {pmo_now - sig_now:.5f} < "
                    f"PREV {pmo_prev - sig_prev:.5f} < "
                    f"PREV2 {pmo_prev2 - sig_prev2:.5f}"
                )
            return rows

        if mode == "normal" and self.direction == "long":
            rows = [f"PMO < -0.10000: {pmo}"]
            if None not in (pmo_now, sig_now, pmo_prev, sig_prev):
                assert pmo_now is not None and sig_now is not None
                assert pmo_prev is not None and sig_prev is not None
                rows.append(
                    f"CROSS UP: PMO {pmo_prev:.5f} <= SIG {sig_prev:.5f} -> "
                    f"PMO {pmo_now:.5f} > SIG {sig_now:.5f}"
                )
            else:
                rows.append(f"PMO > SIG: {pmo} > {sig}")
            return rows

        if mode == "normal" and self.direction == "short":
            rows = [f"PMO > 0.06000: {pmo}"]
            if None not in (pmo_now, sig_now, pmo_prev, sig_prev):
                assert pmo_now is not None and sig_now is not None
                assert pmo_prev is not None and sig_prev is not None
                rows.append(
                    f"CROSS DOWN: PMO {pmo_prev:.5f} >= SIG {sig_prev:.5f} -> "
                    f"PMO {pmo_now:.5f} < SIG {sig_now:.5f}"
                )
            else:
                rows.append(f"PMO < SIG: {pmo} < {sig}")
            return rows

        return [f"PMO {pmo} | SIG {sig}"]

    def message_text(self, *, chart_available: bool = True) -> str:
        side = self.direction.upper()
        mode = self.matched_signal_mode().upper()
        rows = [
            "ICE PI signal",
            f"{self.symbol} {self.timeframe} | {mode} {side}",
            *self.condition_lines(),
        ]
        if not chart_available:
            rows.append("Chart unavailable; text signal delivered.")
        return "\n".join(rows)


@dataclass(frozen=True)
class _SendResult:
    message_id: Optional[str]
    attempts: int


class DiscordSendError(RuntimeError):
    """Sanitized Discord failure; ``code`` never contains credentials/URLs."""

    def __init__(self, code: str, attempts: int = 1):
        self.code = str(code)
        self.attempts = max(1, int(attempts))
        super().__init__(self.code)


class _DiscordTransport:
    def __init__(
        self,
        *,
        webhook_url: str = "",
        token: str = "",
        channel_id: str = "",
        auth_mode: str = "bot",
        client: Optional[httpx.AsyncClient] = None,
    ):
        self._webhook_url = str(webhook_url or "").strip()
        self._token = str(token or "").strip()
        self._channel_id = str(channel_id or "").strip()
        mode = str(auth_mode or "bot").strip().lower()
        self._auth_mode = mode if mode in {"bot", "user"} else "bot"
        self._client = client
        self._owns_client = client is None

    @property
    def mode(self) -> str:
        return "webhook" if self._webhook_url else self._auth_mode

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        return self._client

    async def send(self, content: str, image_bytes: Optional[bytes]) -> _SendResult:
        payload = {
            "content": str(content)[:2000],
            "allowed_mentions": {"parse": []},
        }
        if self._webhook_url:
            url = self._webhook_url
            params = {"wait": "true"}
            headers: dict[str, str] = {}
        else:
            if not self._token or not self._channel_id:
                raise DiscordSendError("discord_credentials_missing")
            url = f"https://discord.com/api/v10/channels/{self._channel_id}/messages"
            params = {}
            authorization = self._token if self._auth_mode == "user" else f"Bot {self._token}"
            headers = {"Authorization": authorization}

        client = await self._get_client()
        for attempt in range(1, 4):
            try:
                if image_bytes:
                    response = await client.post(
                        url,
                        params=params,
                        headers=headers,
                        data={"payload_json": json.dumps(payload, ensure_ascii=False)},
                        files={"files[0]": ("emapmo_signal.png", image_bytes, "image/png")},
                    )
                else:
                    response = await client.post(
                        url,
                        params=params,
                        headers=headers,
                        json=payload,
                    )
            # A read/write failure can happen after Discord accepted the body.
            # Do not retry that ambiguous case: avoiding a duplicate alert is
            # more important than retrying an outcome we cannot prove failed.
            except (httpx.ReadTimeout, httpx.ReadError,
                    httpx.WriteTimeout, httpx.WriteError):
                raise DiscordSendError("discord_delivery_uncertain", attempt)
            except (httpx.ConnectTimeout, httpx.ConnectError, httpx.PoolTimeout):
                if attempt >= 3:
                    raise DiscordSendError("discord_network_failure", attempt)
                await asyncio.sleep(0.25 * (2 ** (attempt - 1)))
                continue
            except (httpx.TimeoutException, httpx.NetworkError):
                raise DiscordSendError("discord_delivery_uncertain", attempt)

            if response.status_code == 429:
                if attempt >= 3:
                    raise DiscordSendError("discord_rate_limited", attempt)
                retry_after = 0.5
                try:
                    retry_after = float(response.json().get("retry_after", retry_after))
                except Exception:
                    try:
                        retry_after = float(response.headers.get("Retry-After", retry_after))
                    except (TypeError, ValueError):
                        pass
                await asyncio.sleep(max(0.05, min(10.0, retry_after)))
                continue

            if response.status_code >= 500:
                if attempt >= 3:
                    raise DiscordSendError(f"discord_http_{response.status_code}", attempt)
                await asyncio.sleep(0.25 * (2 ** (attempt - 1)))
                continue
            if response.status_code not in {200, 201, 204}:
                raise DiscordSendError(f"discord_http_{response.status_code}", attempt)

            message_id: Optional[str] = None
            if response.status_code != 204:
                try:
                    body = response.json()
                    if isinstance(body, dict) and body.get("id") is not None:
                        message_id = str(body["id"])
                except Exception:
                    pass
            return _SendResult(message_id=message_id, attempts=attempt)

        raise DiscordSendError("discord_send_exhausted", 3)  # pragma: no cover

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None


def _render_signal_chart_png(snapshot: _SignalSnapshot) -> bytes:
    """Render an exact 1280x720 in-memory PNG.  Raises for text-only fallback."""
    if not snapshot.bars:
        raise ValueError("no_chart_bars")

    with _PLOT_LOCK:
        import matplotlib

        matplotlib.use("Agg", force=True)
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle

        fig = plt.figure(figsize=(12.8, 7.2), dpi=100, facecolor="#081018")
        try:
            price_ax = fig.add_subplot(111)
            price_ax.set_facecolor("#081018")
            price_ax.grid(True, color="#263746", alpha=0.42, linewidth=0.6)
            price_ax.tick_params(colors="#9fb3c8", labelsize=8)
            for spine in price_ax.spines.values():
                spine.set_color("#31475b")

            bars = snapshot.bars
            x_values = list(range(len(bars)))
            price_span = max(b.high for b in bars) - min(b.low for b in bars)
            min_body = max(price_span * 0.0008, 0.01)
            for x, bar in zip(x_values, bars):
                rising = bar.close >= bar.open
                color = "#19d3ae" if rising else "#ff5577"
                price_ax.vlines(x, bar.low, bar.high, color=color, linewidth=0.85, alpha=0.95)
                lower = min(bar.open, bar.close)
                height = max(abs(bar.close - bar.open), min_body)
                price_ax.add_patch(Rectangle(
                    (x - 0.31, lower), 0.62, height,
                    facecolor=color, edgecolor=color, linewidth=0.6,
                ))

            last_x = x_values[-1]
            last_bar = bars[-1]
            marker = "^" if snapshot.direction == "long" else "v"
            marker_color = "#19d3ae" if snapshot.direction == "long" else "#ff5577"
            marker_offset = max(price_span * 0.035, abs(last_bar.close) * 0.00025, 0.25)
            marker_y = (
                last_bar.low - marker_offset
                if snapshot.direction == "long"
                else last_bar.high + marker_offset
            )
            price_ax.scatter([last_x], [marker_y], marker=marker, s=150, color=marker_color,
                             edgecolors="#f2f7fb", linewidths=0.8, zorder=8)
            annotation_offset = marker_offset * (0.75 if snapshot.direction == "long" else -0.75)
            price_ax.annotate(
                snapshot.direction.upper(),
                xy=(last_x, marker_y),
                xytext=(last_x, marker_y - annotation_offset),
                ha="right",
                va="top" if snapshot.direction == "long" else "bottom",
                color=marker_color,
                fontsize=10,
                fontweight="bold",
            )

            title = (
                f"ICE PI {snapshot.direction.upper()}  |  "
                f"{snapshot.symbol} {snapshot.timeframe}"
            )
            price_ax.set_title(title, color="#e5eef7", fontsize=12, loc="left", pad=9)
            price_ax.set_ylabel("Price", color="#9fb3c8")

            tick_count = min(8, len(bars))
            tick_step = max(1, len(bars) // tick_count)
            ticks = list(range(0, len(bars), tick_step))
            if ticks[-1] != len(bars) - 1:
                ticks.append(len(bars) - 1)
            display_tz = _display_timezone(snapshot.timezone_name)
            labels = [_utc(bars[i].timestamp).astimezone(display_tz).strftime("%m-%d\n%H:%M") for i in ticks]
            price_ax.set_xticks(ticks, labels)
            price_ax.set_xlim(-1.0, len(bars))
            low = min(b.low for b in bars)
            high = max(b.high for b in bars)
            price_ax.set_ylim(
                min(low, marker_y) - marker_offset * 1.6,
                max(high, marker_y) + marker_offset * 1.6,
            )
            fig.subplots_adjust(left=0.065, right=0.985, top=0.94, bottom=0.105)

            with io.BytesIO() as out:
                fig.savefig(out, format="png", dpi=100, facecolor=fig.get_facecolor())
                return out.getvalue()
        finally:
            plt.close(fig)


class EMAPMOSignalMessenger:
    """One bounded background sender per live engine.

    ``enqueue_from_live`` is synchronous and non-blocking.  It only copies the
    strategy's already-bounded 5m arrays and calls ``Queue.put_nowait``.
    """

    def __init__(
        self,
        *,
        root: Path,
        enabled: bool,
        webhook_url: str = "",
        token: str = "",
        channel_id: str = "",
        auth_mode: str = "bot",
        history_days: int = 30,
        chart_bars: int = 96,
        queue_size: int = 8,
        timezone_name: str = "America/Chicago",
        transport: Optional[Any] = None,
        now_fn: Optional[Callable[[], datetime]] = None,
    ):
        self.root = Path(root).resolve()
        # The user requested one month at most; do not permit an environment
        # typo to silently turn this into an unbounded long-term ledger.
        self.history_days = max(1, min(30, int(history_days)))
        self.chart_bars = max(20, min(320, int(chart_bars)))
        requested_timezone = str(timezone_name or "America/Chicago").strip()
        self.timezone_name = (
            requested_timezone
            if _display_timezone(requested_timezone) is not _UTC or requested_timezone.upper() == "UTC"
            else "UTC"
        )
        self._queue: asyncio.Queue[_SignalSnapshot] = asyncio.Queue(
            maxsize=max(1, min(64, int(queue_size)))
        )
        self._db_path = self.root / "data" / "messenger" / "emapmo_signals.sqlite3"
        self._now_fn = now_fn or (lambda: datetime.now(_UTC))
        credentials_ok = transport is not None or bool(str(webhook_url or "").strip()) or bool(
            str(token or "").strip() and str(channel_id or "").strip()
        )
        self.enabled = bool(enabled and credentials_ok)
        self._transport = transport or _DiscordTransport(
            webhook_url=webhook_url,
            token=token,
            channel_id=channel_id,
            auth_mode=auth_mode,
        )
        self._worker_task: Optional[asyncio.Task[None]] = None
        self._started = False
        self._accepting = False
        self._last_prune_date: Optional[str] = None

    @classmethod
    def from_env(cls, root: Path | str) -> "EMAPMOSignalMessenger":
        root_path = Path(root).resolve()
        load_dotenv(root_path / ".env", override=False)
        webhook_url = os.getenv("EMAPMO_DISCORD_WEBHOOK_URL", "").strip()
        token = os.getenv("DISCORD_TOKEN", "").strip()
        channel_id = os.getenv("EMAPMO_DISCORD_CHANNEL_ID", "").strip()
        auth_mode = os.getenv("EMAPMO_DISCORD_AUTH_MODE", "bot").strip().lower()
        credentials_ok = bool(webhook_url) or bool(token and channel_id)
        # Require an explicit opt-in even when a shared shell already happens
        # to contain Discord credentials. This prevents accidental live posts.
        enabled = _env_bool("EMAPMO_MESSENGER_ENABLED", False)
        if enabled and not credentials_ok:
            logger.warning("EMAPMO messenger disabled: Discord credentials are incomplete")
            enabled = False
        return cls(
            root=root_path,
            enabled=enabled,
            webhook_url=webhook_url,
            token=token,
            channel_id=channel_id,
            auth_mode=auth_mode,
            history_days=_env_int("EMAPMO_SIGNAL_HISTORY_DAYS", 30, 1, 30),
            chart_bars=_env_int("EMAPMO_SIGNAL_CHART_BARS", 96, 20, 320),
            queue_size=_env_int("EMAPMO_SIGNAL_QUEUE_SIZE", 8, 1, 64),
            timezone_name=os.getenv("EMAPMO_SIGNAL_TIMEZONE", "America/Chicago").strip(),
        )

    @property
    def queue_maxsize(self) -> int:
        return self._queue.maxsize

    @property
    def delivery_mode(self) -> str:
        return str(getattr(self._transport, "mode", "custom"))

    @property
    def history_path(self) -> Path:
        return self._db_path

    async def start(self) -> None:
        if not self.enabled or self._started:
            return
        try:
            await asyncio.to_thread(self._init_db_sync)
            await self._prune_if_due(force=True)
        except Exception as exc:
            self.enabled = False
            self._accepting = False
            logger.warning("EMAPMO messenger disabled: history initialization failed (%s)",
                           _safe_error(exc))
            return
        self._started = True
        self._accepting = True
        self._worker_task = asyncio.create_task(
            self._worker(), name="emapmo-signal-messenger"
        )

    def enqueue_from_live(
        self,
        signal: Any,
        strategy: Any,
        contract_id: str,
        contract_size: int,
    ) -> bool:
        """Copy and queue one live EMAPMO signal without awaiting any I/O."""
        if not self.enabled or not self._started or not self._accepting:
            return False
        meta = dict(getattr(signal, "meta", None) or {})
        factor_emapmo = str(meta.get("signal_family") or "").strip().lower() == "emapmo"
        legacy_emapmo = str(meta.get("mode") or "").strip().lower() == "emapmo"
        if not (factor_emapmo or legacy_emapmo):
            return False
        try:
            snapshot = self._snapshot_from_live(
                signal, strategy, contract_id, contract_size, meta
            )
            self._queue.put_nowait(snapshot)
            return True
        except asyncio.QueueFull:
            logger.warning("EMAPMO messenger queue full; newest signal was not queued")
            return False
        except Exception as exc:
            logger.warning("EMAPMO signal snapshot failed: %s", _safe_error(exc))
            return False

    async def stop(self) -> None:
        self._accepting = False
        task = self._worker_task
        timed_out = False
        if task is not None:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=_STOP_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                timed_out = True
                logger.warning("EMAPMO messenger stop timed out; cancelling remaining work")
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if timed_out:
            while True:
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                else:
                    self._queue.task_done()
        self._worker_task = None
        self._started = False
        try:
            await self._transport.close()
        except Exception as exc:
            logger.warning("EMAPMO transport close failed: %s", _safe_error(exc))

    def _snapshot_from_live(
        self,
        signal: Any,
        strategy: Any,
        contract_id: str,
        contract_size: int,
        meta: Optional[dict[str, Any]] = None,
    ) -> _SignalSnapshot:
        meta = dict(meta or getattr(signal, "meta", None) or {})
        bars_raw: Sequence[Any] = tuple(getattr(strategy, "_bars", ()) or ())
        pmo_raw: Sequence[Optional[float]] = ()
        sig_raw: Sequence[Optional[float]] = ()
        series_fn = getattr(strategy, "_pmo_series", None)
        if callable(series_fn):
            result = series_fn()
            if isinstance(result, tuple) and len(result) == 2:
                pmo_raw, sig_raw = result

        aligned = min(len(bars_raw), len(pmo_raw), len(sig_raw))
        if aligned > 0:
            keep = min(self.chart_bars, aligned)
            bars_raw = bars_raw[-keep:]
            pmo_raw = pmo_raw[-keep:]
            sig_raw = sig_raw[-keep:]
        else:
            keep = min(self.chart_bars, len(bars_raw))
            bars_raw = bars_raw[-keep:]
            pmo_raw = [None] * keep
            sig_raw = [None] * keep

        bars: list[_Bar] = []
        for bar in bars_raw:
            bars.append(_Bar(
                timestamp=_utc(getattr(bar, "timestamp", None)),
                open=float(getattr(bar, "open")),
                high=float(getattr(bar, "high")),
                low=float(getattr(bar, "low")),
                close=float(getattr(bar, "close")),
                volume=int(getattr(bar, "volume", 0) or 0),
            ))
        pmo_series = tuple(_finite(v) for v in pmo_raw)
        signal_series = tuple(_finite(v) for v in sig_raw)

        detail = dict(meta.get("signal_detail") or {})
        pmo_value = _finite(detail.get("pmo"))
        signal_value = _finite(detail.get("signal"))
        if pmo_value is None:
            pmo_value = _finite(meta.get("pmo"))
        if signal_value is None:
            signal_value = _finite(meta.get("pmo_signal"))
        if pmo_value is None and pmo_series:
            pmo_value = pmo_series[-1]
        if signal_value is None and signal_series:
            signal_value = signal_series[-1]

        signal_time = _utc(getattr(signal, "timestamp", None))
        source_time = bars[-1].timestamp if bars else signal_time
        timeframe = str(meta.get("trade_tf") or "").strip()
        if not timeframe:
            timeframe = f"{max(1, int(getattr(strategy, 'timeframe_minutes', 5) or 5))}m"
        mode = str(
            meta.get("signal_mode")
            or getattr(strategy, "pmo_signal_mode", "")
            or getattr(strategy, "signal_mode", "")
            or "normal"
        ).strip().lower()
        if mode not in {"normal", "early", "both"}:
            mode = "normal"
        direction = _direction_name(getattr(signal, "direction", None))
        symbol = _symbol_root(contract_id)
        canonical_key = "|".join((
            symbol,
            timeframe.lower(),
            "emapmo",
            mode,
            direction,
            _iso_utc(source_time),
        ))
        event_key = hashlib.sha256(canonical_key.encode("utf-8")).hexdigest()
        created_at = _utc(self._now_fn())
        return _SignalSnapshot(
            event_key=event_key,
            created_at_epoch=int(created_at.timestamp()),
            source_time=source_time,
            signal_time=signal_time,
            contract_id=str(contract_id or ""),
            symbol=symbol,
            timeframe=timeframe,
            timezone_name=self.timezone_name,
            signal_mode=mode,
            direction=direction,
            contract_size=max(1, int(contract_size or 1)),
            entry=_finite(getattr(signal, "entry_price", None)),
            sl=_finite(getattr(signal, "sl_price", None)),
            tp=_finite(getattr(signal, "tp_price", None)),
            pmo_value=pmo_value,
            signal_value=signal_value,
            bars=tuple(bars),
            pmo_series=pmo_series,
            signal_series=signal_series,
        )

    async def _worker(self) -> None:
        while True:
            snapshot = await self._queue.get()
            claimed = False
            finished = False
            try:
                await self._prune_if_due()
                claimed = await asyncio.to_thread(self._claim_sync, snapshot)
                if not claimed:
                    continue

                image_bytes: Optional[bytes] = None
                chart_error: Optional[str] = None
                try:
                    image_bytes = await asyncio.to_thread(_render_signal_chart_png, snapshot)
                except Exception as exc:
                    chart_error = f"chart_{_safe_error(exc)}"
                    logger.warning("EMAPMO chart unavailable; sending text only: %s", chart_error)

                try:
                    result = await self._transport.send(
                        snapshot.message_text(chart_available=image_bytes is not None),
                        image_bytes,
                    )
                    if isinstance(result, _SendResult):
                        message_id, attempts = result.message_id, result.attempts
                    else:
                        message_id, attempts = (str(result) if result else None), 1
                    await asyncio.to_thread(
                        self._finish_sync,
                        snapshot.event_key,
                        "sent",
                        attempts,
                        message_id,
                        chart_error,
                    )
                    finished = True
                    logger.info(
                        "EMAPMO signal sent: %s %s %s event=%s",
                        snapshot.symbol,
                        snapshot.timeframe,
                        snapshot.direction.upper(),
                        snapshot.event_key[:12],
                    )
                except Exception as exc:
                    attempts = int(getattr(exc, "attempts", 1) or 1)
                    code = _safe_error(exc)
                    await asyncio.to_thread(
                        self._finish_sync,
                        snapshot.event_key,
                        "failed",
                        attempts,
                        None,
                        code,
                    )
                    finished = True
                    logger.warning("EMAPMO Discord delivery failed: %s", code)
            except asyncio.CancelledError:
                if claimed and not finished:
                    try:
                        await asyncio.to_thread(
                            self._finish_sync,
                            snapshot.event_key,
                            "failed",
                            1,
                            None,
                            "messenger_shutdown",
                        )
                    except Exception:
                        pass
                raise
            except Exception as exc:
                if claimed and not finished:
                    try:
                        await asyncio.to_thread(
                            self._finish_sync,
                            snapshot.event_key,
                            "failed",
                            1,
                            None,
                            f"worker_{_safe_error(exc)}",
                        )
                    except Exception:
                        pass
                logger.warning("EMAPMO messenger worker error: %s", _safe_error(exc))
            finally:
                self._queue.task_done()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=5.0)
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _init_db_sync(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS emapmo_signals (
                    event_key TEXT PRIMARY KEY,
                    created_at_epoch INTEGER NOT NULL,
                    source_time_utc TEXT NOT NULL,
                    signal_time_utc TEXT NOT NULL,
                    contract_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    signal_mode TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    contract_size INTEGER NOT NULL,
                    entry REAL,
                    sl REAL,
                    tp REAL,
                    pmo REAL,
                    pmo_signal REAL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    sent_at_utc TEXT,
                    discord_message_id TEXT,
                    last_error TEXT
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS ix_emapmo_signals_created "
                "ON emapmo_signals(created_at_epoch)"
            )

    def _claim_sync(self, snapshot: _SignalSnapshot) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO emapmo_signals (
                    event_key, created_at_epoch, source_time_utc, signal_time_utc,
                    contract_id, symbol, timeframe, signal_mode, direction,
                    contract_size, entry, sl, tp, pmo, pmo_signal, status, attempts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0)
                """,
                (
                    snapshot.event_key,
                    snapshot.created_at_epoch,
                    _iso_utc(snapshot.source_time),
                    _iso_utc(snapshot.signal_time),
                    snapshot.contract_id,
                    snapshot.symbol,
                    snapshot.timeframe,
                    snapshot.signal_mode,
                    snapshot.direction,
                    snapshot.contract_size,
                    snapshot.entry,
                    snapshot.sl,
                    snapshot.tp,
                    snapshot.pmo_value,
                    snapshot.signal_value,
                ),
            )
            connection.commit()
            return cursor.rowcount == 1

    def _finish_sync(
        self,
        event_key: str,
        status: str,
        attempts: int,
        message_id: Optional[str],
        last_error: Optional[str],
    ) -> None:
        sent_at = _iso_utc(_utc(self._now_fn())) if status == "sent" else None
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE emapmo_signals
                   SET status = ?, attempts = ?, sent_at_utc = ?,
                       discord_message_id = ?, last_error = ?
                 WHERE event_key = ?
                """,
                (
                    status,
                    max(1, int(attempts)),
                    sent_at,
                    message_id,
                    str(last_error)[:200] if last_error else None,
                    event_key,
                ),
            )

    async def _prune_if_due(self, *, force: bool = False) -> None:
        now = _utc(self._now_fn())
        today = now.date().isoformat()
        if not force and self._last_prune_date == today:
            return
        cutoff_epoch = int((now - timedelta(days=self.history_days)).timestamp())
        await asyncio.to_thread(self._prune_sync, cutoff_epoch)
        self._last_prune_date = today

    def _prune_sync(self, cutoff_epoch: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM emapmo_signals WHERE created_at_epoch < ?",
                (int(cutoff_epoch),),
            )
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(PASSIVE)")


__all__ = ["EMAPMOSignalMessenger"]
