"""Market-segment entry filter shared by live, backtest, and presets.

Segments match the penta-session buckets used by the zone detector:

- ASIA: 22:00-06:59 UTC
- EURO: 07:00-10:59 UTC
- PRE:  11:00-13:29 UTC
- RTH:  13:30-19:59 UTC
- AH:   20:00-21:59 UTC

``None`` / empty / ALL means no filter.  A list such as ``["ASIA", "PRE"]``
means new entries are allowed only in those market segments.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional


SESSION_CODES = ("ASIA", "EURO", "PRE", "RTH", "AH")
DEFAULT_ALLOWED_SESSIONS = ("ASIA", "PRE")


def market_session_code(ts: datetime) -> str:
    """Return ASIA/EURO/PRE/RTH/AH for a UTC-aware or naive-UTC timestamp."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ts = ts.astimezone(timezone.utc)
    h, m = ts.hour, ts.minute
    if h >= 22 or h < 7:
        return "ASIA"
    if h < 11:
        return "EURO"
    if h < 13 or (h == 13 and m < 30):
        return "PRE"
    if h < 20:
        return "RTH"
    return "AH"


def normalize_allowed_sessions(value) -> Optional[tuple[str, ...]]:
    """Normalize UI/API/preset values to a sorted tuple or ``None`` for ALL."""
    if value is None:
        return None
    if isinstance(value, str):
        raw = [part.strip() for part in value.replace("|", ",").replace("+", ",").split(",")]
    elif isinstance(value, Iterable):
        raw = [str(part).strip() for part in value]
    else:
        raw = [str(value).strip()]

    allowed: list[str] = []
    for item in raw:
        code = item.upper()
        if not code:
            continue
        if code in ("ALL", "*", "ANY"):
            return None
        if code in SESSION_CODES and code not in allowed:
            allowed.append(code)

    return tuple(code for code in SESSION_CODES if code in allowed) or None


def allowed_sessions_label(value) -> str:
    allowed = normalize_allowed_sessions(value)
    return "ALL" if allowed is None else "+".join(allowed)


def is_allowed_session(ts: datetime, allowed_sessions) -> bool:
    allowed = normalize_allowed_sessions(allowed_sessions)
    if allowed is None:
        return True
    return market_session_code(ts) in allowed
