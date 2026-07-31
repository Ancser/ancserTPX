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


# TPX chart theme, mirrored from createChart() in
# frontend/static/ancserTPX.js so a Discord alert and the app read as the
# same product.  Keep these in sync if the frontend palette moves.
_MONO = ["IBM Plex Mono", "DejaVu Sans Mono", "monospace"]
_CHART_BG = "#08090d"
_CHART_TEXT = "#556178"
_CHART_LINE = "#64dcff"
_GRID_ALPHA = 0.10
_SEPARATOR_ALPHA = 0.24
_BORDER_ALPHA = 0.30
_BOX_EDGE = "#22303c"
_CANDLE_UP = "#888888"
_CANDLE_DOWN = "#555555"
# EMAPMO long/short, from the #signal-legend swatches in ancserTPX.html.
_SIGNAL_LONG = "#38bdf8"
_SIGNAL_SHORT = "#a855f7"
_LINE_ENTRY = "#ffa726"
_LINE_TP = "#00e5a0"
_LINE_SL = "#ff4060"


# ── liquid glass, ported from frontend/static/tpx-glass.js ─────────────
# capsuleCoordinate / createDisplacementMap / createShrinkMap /
# createSpecularMap and the feFilter chain, reproduced on the rendered
# raster.  The frontend hands these maps to feDisplacementMap; numpy does
# the same resample here, so the alert carries the app's actual optics
# rather than a lookalike.
#
# Element geometry is .chart-lens (200x140) and .optical-surface's
# border-radius: 999px, which clamps to a full capsule.  Coefficients are
# the `precision` row of that file's `defaults` table -- the pointer lens,
# the one tuned for looking through.
_LENS_W = 200
_LENS_H = 140
_LENS_BEZEL = 30.0
_LENS_THICKNESS = 150.0
_LENS_REFRACTION = 1.5
_LENS_SHRINK = -0.20
_LENS_SPECULAR = 0.60
_LENS_BLUR = 0.0
_LENS_SATURATION = 1.30


def _capsule_coordinate(value, size, radius):
    """Vectorised capsuleCoordinate(): distance from the capsule's spine,
    zero along the straight body."""
    import numpy as np

    body = size - radius * 2
    return np.where(
        value < radius, value - radius,
        np.where(value >= size - radius, value - radius - body, 0.0),
    )


def _refraction_profile(bezel, thickness, samples=256):
    """physicalProfile(): Snell through the convex-squircle bezel at
    eta = 1/1.5, giving the refraction offset across the bezel width."""
    import numpy as np

    x = np.linspace(0.0, 1.0, samples, endpoint=False)
    surface = lambda t: (1.0 - (1.0 - t) ** 4) ** 0.25  # noqa: E731
    y = surface(x)
    step = 1e-4
    derivative = (surface(np.clip(x + step, 0.0, 1.0)) - y) / step
    magnitude = np.hypot(derivative, 1.0)
    normal_x = -derivative / magnitude
    normal_y = -1.0 / magnitude

    eta = 1.0 / 1.5
    dot = normal_y
    k = 1.0 - eta * eta * (1.0 - dot * dot)
    root = np.sqrt(np.clip(k, 0.0, None))
    refracted_x = -(eta * dot + root) * normal_x
    refracted_y = eta - (eta * dot + root) * normal_y

    safe_y = np.where(np.abs(refracted_y) < 1e-6, 1.0, refracted_y)
    profile = refracted_x * ((y * bezel + thickness) / safe_y)
    profile[np.abs(refracted_y) < 1e-6] = 0.0
    profile[k < 0] = 0.0
    return profile


def _displacement_map(width, height, radius, bezel, thickness):
    """createDisplacementMap(): R/G channels carrying the bezel's
    refraction.  The interior stays neutral -- only the bezel band bends
    light; the centre is moved by the shrink pass alone."""
    import numpy as np

    profile = _refraction_profile(bezel, thickness)
    maximum = max(1e-6, float(np.max(np.abs(profile))))
    ys, xs = np.mgrid[0:height, 0:width]
    cx = _capsule_coordinate(xs, width, radius)
    cy = _capsule_coordinate(ys, height, radius)
    squared = cx * cx + cy * cy

    outer_squared = (radius + 1.0) ** 2
    radius_squared = radius ** 2
    inner_squared = max(0.0, radius - bezel) ** 2
    band = (squared <= outer_squared) & (squared >= inner_squared)

    distance = np.sqrt(np.maximum(squared, 1e-12))
    alpha = np.where(
        squared < radius_squared, 1.0,
        1.0 - (distance - radius) / (np.sqrt(outer_squared) - radius),
    )
    index = np.clip((radius - distance) / bezel, 0.0, 1.0)
    index = np.clip((index * profile.size).astype(np.int32), 0, profile.size - 1)
    displacement = profile[index]
    normal_x = np.where(distance > 0, -cx / distance, 0.0)
    normal_y = np.where(distance > 0, -cy / distance, 0.0)

    scaled = (displacement / maximum) * 127.0 * alpha
    red = np.where(band, np.clip(128.0 + normal_x * scaled, 0, 255), 128.0)
    green = np.where(band, np.clip(128.0 + normal_y * scaled, 0, 255), 128.0)
    return red, green, maximum


def _shrink_map(width, height, shrink):
    """createShrinkMap(): uniform push from the centre.  Negative shrink
    displaces inward, which magnifies -- `precision` uses -0.20."""
    import numpy as np

    shrink = float(min(max(shrink, -0.8), 0.8))
    zoom_out = (1.0 / (1.0 - shrink) - 1.0) if shrink else 0.0
    maximum = max(
        1e-6, abs(width * 0.5 * zoom_out), abs(height * 0.5 * zoom_out)
    )
    ys, xs = np.mgrid[0:height, 0:width]
    dx = (xs - width / 2.0) * zoom_out
    dy = (ys - height / 2.0) * zoom_out
    red = np.clip(128.0 + dx / maximum * 127.0, 0, 255)
    green = np.clip(128.0 + dy / maximum * 127.0, 0, 255)
    return red, green, (maximum * 2.0 if shrink else 0.0)


def _specular_map(width, height, radius):
    """createSpecularMap(): lit rim, 1.8px wide, from a fixed key light."""
    import numpy as np
    import math

    ys, xs = np.mgrid[0:height, 0:width]
    cx = _capsule_coordinate(xs, width, radius)
    cy = _capsule_coordinate(ys, height, radius)
    squared = cx * cx + cy * cy
    outer_squared = (radius + 1.0) ** 2
    inner_squared = max(0.0, radius - 1.8) ** 2
    band = (squared <= outer_squared) & (squared >= inner_squared)

    light_x = math.cos(-math.pi * 0.72)
    light_y = math.sin(-math.pi * 0.72)
    distance = np.sqrt(np.maximum(squared, 1e-12))
    normal_x = np.where(distance > 0, cx / distance, 0.0)
    normal_y = np.where(distance > 0, -cy / distance, 0.0)
    dot = np.abs(normal_x * light_x + normal_y * light_y)
    edge = np.clip((radius - distance) / 1.8, 0.0, 1.0)
    curve = dot * np.sqrt(np.clip(1.0 - (1.0 - edge) ** 2, 0.0, None))
    channel = np.where(band, np.clip(255.0 * curve, 0, 255), 0.0)
    alpha = np.where(band, np.clip(channel * curve, 0, 255), 0.0)
    return channel, alpha


def _displace(source, origin_x, origin_y, red, green, scale, box):
    """One feDisplacementMap pass over `box` = (x0, y0, x1, y1).

    Channels are the element-space maps placed at (origin_x, origin_y);
    everything outside them decodes to the neutral 128, i.e. no shift --
    the same job the filter's feFlood/feMerge pad does.
    """
    import numpy as np

    x0, y0, x1, y1 = box
    ys, xs = np.mgrid[y0:y1, x0:x1]
    map_h, map_w = red.shape
    local_x = xs - int(origin_x)
    local_y = ys - int(origin_y)
    inside = (
        (local_x >= 0) & (local_x < map_w) & (local_y >= 0) & (local_y < map_h)
    )
    safe_x = np.clip(local_x, 0, map_w - 1)
    safe_y = np.clip(local_y, 0, map_h - 1)
    red_channel = np.where(inside, red[safe_y, safe_x], 128.0)
    green_channel = np.where(inside, green[safe_y, safe_x], 128.0)

    shift_x = scale * (red_channel / 255.0 - 0.5)
    shift_y = scale * (green_channel / 255.0 - 0.5)
    return _sample_bilinear(source, xs + shift_x, ys + shift_y)


def _sample_bilinear(source, sx, sy):
    import numpy as np

    height, width = source.shape[:2]
    sx = np.clip(sx, 0.0, width - 1.001)
    sy = np.clip(sy, 0.0, height - 1.001)
    x0 = np.floor(sx).astype(np.int32)
    y0 = np.floor(sy).astype(np.int32)
    fx = (sx - x0)[..., None]
    fy = (sy - y0)[..., None]
    top = source[y0, x0] * (1 - fx) + source[y0, x0 + 1] * fx
    bottom = source[y0 + 1, x0] * (1 - fx) + source[y0 + 1, x0 + 1] * fx
    return top * (1 - fy) + bottom * fy


def _apply_glass_lens(buffer, centre_x, centre_y):
    """Run the app's filter chain over a .chart-lens-sized box.

    Order matches the SVG exactly: blur -> shrink displacement -> bezel
    displacement -> saturate -> screen-blend the specular.
    """
    import numpy as np

    height, width = buffer.shape[:2]
    lens_w, lens_h = _LENS_W, _LENS_H
    radius = min(lens_w, lens_h) / 2.0 - 1.0  # border-radius: 999px

    # Keep the capsule on-canvas; the caller only picks where to look.
    origin_x = int(round(min(max(centre_x - lens_w / 2.0, 0), width - lens_w)))
    origin_y = int(round(min(max(centre_y - lens_h / 2.0, 0), height - lens_h)))

    disp_r, disp_g, maximum = _displacement_map(
        lens_w, lens_h, radius, _LENS_BEZEL, _LENS_THICKNESS
    )
    disp_scale = maximum * _LENS_REFRACTION
    shrink_r, shrink_g, shrink_scale = _shrink_map(lens_w, lens_h, _LENS_SHRINK)
    spec_channel, spec_alpha = _specular_map(lens_w, lens_h, radius)

    # The second pass reads what the first produced at displaced
    # coordinates, so the first has to cover that reach.
    pad = int(np.ceil(max(abs(disp_scale), abs(shrink_scale)) / 2.0)) + 2
    box = (
        max(0, origin_x - pad), max(0, origin_y - pad),
        min(width, origin_x + lens_w + pad), min(height, origin_y + lens_h + pad),
    )
    inner = (origin_x, origin_y, origin_x + lens_w, origin_y + lens_h)

    source = buffer
    if _LENS_BLUR > 0:
        source = _box_blur(buffer, _LENS_BLUR)

    shrunk = _displace(source, origin_x, origin_y, shrink_r, shrink_g,
                       shrink_scale, box)
    # Re-frame the intermediate so the next pass can index it directly.
    padded = buffer.copy()
    padded[box[1]:box[3], box[0]:box[2]] = shrunk
    refracted = _displace(padded, origin_x, origin_y, disp_r, disp_g,
                          disp_scale, inner)

    rgb = refracted[..., :3]
    grey = rgb.mean(axis=-1, keepdims=True)
    rgb = np.clip(grey + (rgb - grey) * _LENS_SATURATION, 0.0, 255.0)

    # feComponentTransfer slope on alpha, then feBlend mode="screen".
    spec_rgb = (spec_channel / 255.0)[..., None]
    spec_a = ((spec_alpha / 255.0) * _LENS_SPECULAR)[..., None]
    premultiplied = spec_rgb * spec_a
    base = rgb / 255.0
    rgb = np.clip(base + premultiplied - base * premultiplied, 0.0, 1.0) * 255.0

    # Clip to the capsule, feathered over the last pixel.
    ys, xs = np.mgrid[0:lens_h, 0:lens_w]
    cx = _capsule_coordinate(xs, lens_w, radius)
    cy = _capsule_coordinate(ys, lens_h, radius)
    distance = np.sqrt(cx * cx + cy * cy)
    coverage = np.clip(radius + 0.5 - distance, 0.0, 1.0)[..., None]

    out = buffer.copy()
    patch = out[inner[1]:inner[3], inner[0]:inner[2]]
    patch[..., :3] = patch[..., :3] * (1 - coverage) + rgb * coverage
    out[inner[1]:inner[3], inner[0]:inner[2]] = patch
    return out


def _candle_artists(ax, xs, bars, min_body, linewidth=0.85, zorder=3):
    """Add one candle per bar, returning the artists."""
    from matplotlib.patches import Rectangle

    point = lambda x, y: (x, y)  # noqa: E731
    made = []
    for x, bar in zip(xs, bars):
        rising = bar.close >= bar.open
        color = _CANDLE_UP if rising else _CANDLE_DOWN
        wx, wy_high = point(x, bar.high)
        _, wy_low = point(x, bar.low)
        made.append(ax.vlines(wx, wy_low, wy_high, color=color,
                              linewidth=linewidth, zorder=zorder))
        lower = min(bar.open, bar.close)
        height = max(abs(bar.close - bar.open), min_body)
        bx, by = point(x - 0.31, lower)
        tx, ty = point(x + 0.31, lower + height)
        made.append(ax.add_patch(Rectangle(
            (bx, by), max(tx - bx, 1e-9), max(ty - by, 1e-9),
            facecolor=color, edgecolor=color,
            linewidth=linewidth * 0.7, zorder=zorder,
        )))
    return made


def _render_signal_chart_png(snapshot: _SignalSnapshot) -> bytes:
    """Render an exact 1280x720 in-memory PNG.  Raises for text-only fallback.

    Drawn to match the in-app chart: same near-black background, grey
    candles, right-hand price scale, faint cyan grid and dashed day
    separators.  The entry sits under a liquid-glass lens -- the same
    optical idea the frontend uses -- which magnifies the signal bars and
    doubles as the "look here" pointer.
    """
    if not snapshot.bars:
        raise ValueError("no_chart_bars")

    with _PLOT_LOCK:
        import matplotlib

        matplotlib.use("Agg", force=True)
        matplotlib.rcParams["font.monospace"] = (
            _MONO + list(matplotlib.rcParams["font.monospace"])
        )
        import matplotlib.pyplot as plt

        fig = plt.figure(figsize=(12.8, 7.2), dpi=100, facecolor=_CHART_BG)
        try:
            # Full-bleed like the app: the price scale sits on the right and
            # the time scale along the bottom, with no outer margin.
            ax = fig.add_axes([0.006, 0.072, 0.93, 0.918])
            ax.set_facecolor(_CHART_BG)
            ax.grid(True, axis="y", color=_CHART_LINE, alpha=_GRID_ALPHA,
                    linewidth=0.6)
            ax.grid(True, axis="x", color=_CHART_LINE, alpha=_GRID_ALPHA,
                    linewidth=0.7, linestyle=(0, (4, 4)))
            ax.set_axisbelow(True)
            ax.yaxis.tick_right()
            ax.yaxis.set_label_position("right")
            ax.tick_params(colors=_CHART_TEXT, labelsize=9, length=0, pad=6)
            for label in ax.get_xticklabels() + ax.get_yticklabels():
                label.set_fontfamily("monospace")
            for side, spine in ax.spines.items():
                if side in ("right", "bottom"):
                    spine.set_color(_CHART_LINE)
                    spine.set_alpha(_BORDER_ALPHA)
                else:
                    spine.set_color("none")

            bars = snapshot.bars
            xs = list(range(len(bars)))
            low = min(b.low for b in bars)
            high = max(b.high for b in bars)
            price_span = max(high - low, 1e-6)
            min_body = max(price_span * 0.0008, 0.01)

            _candle_artists(ax, xs, bars, min_body, linewidth=0.85, zorder=3)

            display_tz = _display_timezone(snapshot.timezone_name)
            stamps = [_utc(b.timestamp).astimezone(display_tz) for b in bars]

            # Dashed day separators, as on the app's time scale.
            for i in range(1, len(stamps)):
                if stamps[i].date() != stamps[i - 1].date():
                    ax.axvline(i - 0.5, color=_CHART_LINE,
                               alpha=_SEPARATOR_ALPHA, linestyle=(0, (5, 5)),
                               linewidth=0.9, zorder=1)

            is_long = snapshot.direction == "long"
            signal_color = _SIGNAL_LONG if is_long else _SIGNAL_SHORT
            last_x = xs[-1]
            last_bar = bars[-1]
            offset = max(price_span * 0.035, 0.25)
            marker_y = last_bar.low - offset if is_long else last_bar.high + offset

            # Entry / SL / TP, only where they stay near the visible band --
            # a far-away bracket would otherwise flatten the candles.
            band_lo, band_hi = low - price_span * 0.6, high + price_span * 0.6
            for value, color, tag in (
                (snapshot.entry, _LINE_ENTRY, "ENTRY"),
                (snapshot.tp, _LINE_TP, "TP"),
                (snapshot.sl, _LINE_SL, "SL"),
            ):
                level = _finite(value)
                if level is None or not (band_lo <= level <= band_hi):
                    continue
                ax.axhline(level, color=color, linewidth=0.9, alpha=0.55,
                           linestyle=(0, (6, 4)), zorder=4)
                ax.text(0.012, level, "{} {:,.2f}".format(tag, level),
                        transform=ax.get_yaxis_transform(), ha="left",
                        va="bottom", color=color, fontsize=8.5,
                        fontfamily="monospace", alpha=0.9, zorder=5)

            # Last price: dotted rule plus the boxed tag the app pins to the
            # right scale.
            ax.axhline(last_bar.close, color="#8fa3bd", linewidth=0.7,
                       alpha=0.5, linestyle=(0, (1, 3)), zorder=4)
            ax.text(
                1.0, last_bar.close, " {:,.2f} ".format(last_bar.close),
                transform=ax.get_yaxis_transform(), ha="left", va="center",
                color="#0b0d12", fontsize=9, fontfamily="monospace", zorder=9,
                bbox=dict(boxstyle="square,pad=0.30", facecolor="#c9d4e4",
                          edgecolor="none"),
            )

            marker = "^" if is_long else "v"
            ax.scatter([last_x], [marker_y], marker=marker, s=190,
                       color=signal_color, edgecolors="#eaf6ff",
                       linewidths=0.9, zorder=8)

            right_pad = len(bars) * 0.22

            ax.text(
                0.006, 0.985,
                "ICE entry  |  {} {}".format(snapshot.symbol, snapshot.timeframe),
                transform=ax.transAxes, ha="left", va="top",
                color="#c9d4e4", fontsize=12, fontfamily="monospace", zorder=16,
            )

            tick_count = min(9, len(bars))
            tick_step = max(1, len(bars) // tick_count)
            ticks = list(range(0, len(bars), tick_step))
            if ticks[-1] != len(bars) - 1:
                ticks.append(len(bars) - 1)
            # Date on the first tick of a day, clock time otherwise -- the
            # app's tickMarkFormatter behaviour.
            labels = []
            for i in ticks:
                stamp = stamps[i]
                new_day = i == 0 or stamp.date() != stamps[i - 1].date()
                labels.append(stamp.strftime("%m.%d" if new_day else "%H:%M"))
            ax.set_xticks(ticks, labels)
            ax.set_xlim(-1.0, len(bars) + right_pad)

            pad = price_span * 0.06
            # Reserve room past the marker for the capsule's lower half and
            # the caption beneath it.  Both are fixed pixel sizes, so this
            # is a comfortable over-estimate rather than an exact figure --
            # _apply_glass_lens clamps the capsule on-canvas regardless.
            # Headroom on both sides for the capsule's half-height, plus a
            # little more on the marker's side for the caption.  A rally puts
            # the newest bars hard against the top, and without this the
            # capsule clamped against the canvas edge instead of centring on
            # the signal.
            headroom = price_span * 0.16
            reserve = price_span * 0.12
            ax.set_ylim(
                min(low, marker_y) - pad - headroom - (reserve if is_long else 0.0),
                max(high, marker_y) + pad + headroom + (0.0 if is_long else reserve),
            )

            # The signal's own timestamp, boxed on the time scale.
            ax.text(
                last_x, 0.0,
                " {} ".format(stamps[-1].strftime("%Y.%m.%d %H:%M")),
                transform=ax.get_xaxis_transform(), ha="center", va="top",
                color="#c9d4e4", fontsize=9, fontfamily="monospace", zorder=9,
                bbox=dict(boxstyle="square,pad=0.30", facecolor="#1b212c",
                          edgecolor=_BOX_EDGE),
            )

            # .chart-lens is a fixed 200x140 element, so where it sits and
            # where its caption goes are device-space questions.  Limits are
            # final by now, so transData is trustworthy.
            figure_h = fig.get_size_inches()[1] * fig.dpi
            marker_px, marker_py = ax.transData.transform((last_x, marker_y))
            lens_x = marker_px
            # Nudge off the marker toward the bars it came from, so the
            # capsule frames the signal instead of empty space below it.
            lens_y = (figure_h - marker_py) + (
                -_LENS_H * 0.16 if is_long else _LENS_H * 0.16
            )
            caption_y = lens_y + (_LENS_H / 2.0 + 15.0) * (1 if is_long else -1)
            caption_point = ax.transData.inverted().transform(
                (lens_x, figure_h - caption_y)
            )
            # Knocked out of the background: where the caption lands relative
            # to the SL/TP rules depends on the trade, and a dashed line
            # struck straight through the words.
            ax.text(caption_point[0], caption_point[1], "ICE entry",
                    ha="center", va="top" if is_long else "bottom",
                    color=signal_color, fontsize=14, fontweight="bold",
                    fontfamily="monospace", zorder=16,
                    bbox=dict(boxstyle="square,pad=0.34", facecolor=_CHART_BG,
                              edgecolor="none"))

            import numpy as np
            import matplotlib.image as mpimg

            fig.canvas.draw()
            raster = np.asarray(fig.canvas.buffer_rgba()).astype(np.float32)

            # Data space -> buffer pixels.  transData has a bottom-left
            # origin; the raster is top-left, hence the flip.
            raster = _apply_glass_lens(raster, lens_x, lens_y)

            with io.BytesIO() as out:
                mpimg.imsave(out, raster.astype(np.uint8), format="png")
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
