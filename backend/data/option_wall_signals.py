"""Read-only loader for the causal Option Wall strategy signal tape.

The paid Databento-derived data stays outside the application repository under
``ancserData``.  Production code deliberately reads only entry-time columns;
PnL and future-path columns in the research artifact are never loaded.
"""
from __future__ import annotations

import csv
import gzip
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Optional


DEFAULT_DATA_ROOT = (
    Path(__file__).resolve().parents[3] / "ancserData" / "qqq_option_ml"
)
SIGNAL_FILE = "option_wall_gamma_gate_trades.csv.gz"
PRIMARY_STRICT_GATE = "gate_consensus_article_alignment_wall_room"


@dataclass(frozen=True)
class OptionWallSignal:
    timestamp: datetime
    direction: int
    oi_gamma_state: int
    volume_gamma_state: int
    vwap_distance_bps: float
    return_15m_bps: float
    call_wall_bps: float
    put_wall_bps: float


def data_root() -> Path:
    configured = os.environ.get("ANCSER_OPTION_WALL_DATA_ROOT", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_DATA_ROOT


def signal_path(root: Optional[Path] = None) -> Path:
    return Path(root or data_root()) / SIGNAL_FILE


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _integer(value: object, field: str) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid Option Wall {field}: {value!r}") from exc


def _number(value: object, field: str) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid Option Wall {field}: {value!r}") from exc


def _timestamp(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid Option Wall as_of: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@lru_cache(maxsize=8)
def _load_cached(path_text: str, modified_ns: int, size: int) -> tuple[OptionWallSignal, ...]:
    del modified_ns, size  # cache-key material; the values need no further use
    path = Path(path_text)
    required = {
        "as_of", "direction", PRIMARY_STRICT_GATE,
        "oi_gamma_state", "volume_gamma_state",
        "article_price_vwap_distance_bps", "article_price_return_15m_bps",
        "dashboard_vol_call_wall_bps", "dashboard_vol_put_wall_bps",
    }
    signals: list[OptionWallSignal] = []
    seen: set[tuple[datetime, int]] = set()
    with gzip.open(path, mode="rt", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(
                "Option Wall signal artifact is missing columns: "
                + ", ".join(sorted(missing))
            )
        for row in reader:
            if not _truthy(row.get(PRIMARY_STRICT_GATE)):
                continue
            timestamp = _timestamp(row.get("as_of"))
            direction = _integer(row.get("direction"), "direction")
            if direction not in (-1, 1):
                raise ValueError(f"Option Wall direction must be +/-1, got {direction}")
            key = (timestamp, direction)
            if key in seen:
                raise ValueError(f"duplicate Option Wall signal: {timestamp.isoformat()} {direction}")
            seen.add(key)
            signals.append(OptionWallSignal(
                timestamp=timestamp,
                direction=direction,
                oi_gamma_state=_integer(row.get("oi_gamma_state"), "oi_gamma_state"),
                volume_gamma_state=_integer(
                    row.get("volume_gamma_state"), "volume_gamma_state",
                ),
                vwap_distance_bps=_number(
                    row.get("article_price_vwap_distance_bps"), "vwap_distance_bps",
                ),
                return_15m_bps=_number(
                    row.get("article_price_return_15m_bps"), "return_15m_bps",
                ),
                call_wall_bps=_number(
                    row.get("dashboard_vol_call_wall_bps"), "call_wall_bps",
                ),
                put_wall_bps=_number(
                    row.get("dashboard_vol_put_wall_bps"), "put_wall_bps",
                ),
            ))
    signals.sort(key=lambda item: item.timestamp)
    return tuple(signals)


def load_primary_strict_signals(root: Optional[Path] = None) -> list[OptionWallSignal]:
    """Load the fixed Primary Strict entry tape, invalidating cache on rewrite."""
    path = signal_path(root)
    try:
        stat = path.stat()
    except OSError:
        return []
    return list(_load_cached(str(path.resolve()), stat.st_mtime_ns, stat.st_size))


def primary_strict_status(root: Optional[Path] = None) -> dict[str, object]:
    path = signal_path(root)
    signals = load_primary_strict_signals(root)
    return {
        "available": path.is_file() and bool(signals),
        "path": str(path),
        "signals": len(signals),
        "first": signals[0].timestamp.isoformat() if signals else None,
        "last": signals[-1].timestamp.isoformat() if signals else None,
    }
