"""New-York market clock shared by live, backtest, charts, and research.

Stored candle timestamps remain UTC instants.  Market labels and risk windows
are defined in ``America/New_York`` so EST/EDT and year boundaries are handled
by the timezone database instead of fixed UTC offsets.

``None`` / empty / ALL means no entry filter.  A list such as
``["ASIA", "PRE"]`` allows new entries only in those market segments.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, Optional
from zoneinfo import ZoneInfo


SESSION_CODES = ("ASIA", "EURO", "PRE", "RTH", "AH")
DEFAULT_ALLOWED_SESSIONS = ("ASIA",)
MARKET_TIMEZONE = ZoneInfo("America/New_York")
MARKET_CLOCK_VERSION = "america-new-york-v1"

MARKET_PHASE_OPEN = "open"
MARKET_PHASE_PRE_FLATTEN = "pre_flatten"
MARKET_PHASE_FLATTEN = "flatten"

_SESSION_STARTS = {
    "ASIA": time(18, 0),
    "EURO": time(3, 0),
    "PRE": time(7, 0),
    "RTH": time(9, 30),
    "AH": time(16, 0),
}
_PRE_FLATTEN = time(15, 30)
_FLATTEN = time(15, 45)
_REOPEN = time(18, 0)


def as_utc(ts: datetime) -> datetime:
    """Normalize an aware timestamp or a legacy naive-UTC timestamp to UTC."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def as_new_york(ts: datetime) -> datetime:
    """Return the same instant in the New York market timezone."""
    return as_utc(ts).astimezone(MARKET_TIMEZONE)


def market_session_code(ts: datetime) -> str:
    """Return ASIA/EURO/PRE/RTH/AH using New York local wall time."""
    local = as_new_york(ts)
    tod = local.time().replace(tzinfo=None)
    if tod >= _SESSION_STARTS["ASIA"] or tod < _SESSION_STARTS["EURO"]:
        return "ASIA"
    if tod < _SESSION_STARTS["PRE"]:
        return "EURO"
    if tod < _SESSION_STARTS["RTH"]:
        return "PRE"
    if tod < _SESSION_STARTS["AH"]:
        return "RTH"
    return "AH"


def market_session(ts: datetime) -> tuple[str, datetime]:
    """Return the market segment and its DST-aware UTC start instant."""
    local = as_new_york(ts)
    code = market_session_code(ts)
    start_day = local.date()
    if code == "ASIA" and local.time().replace(tzinfo=None) < _SESSION_STARTS["EURO"]:
        start_day -= timedelta(days=1)
    start_local = datetime.combine(
        start_day,
        _SESSION_STARTS[code],
        tzinfo=MARKET_TIMEZONE,
    )
    return code, start_local.astimezone(timezone.utc)


def market_session_id(ts: datetime) -> str:
    """Return ``YYYY-MM-DD-CODE`` keyed by the segment's New York start date."""
    code, start_utc = market_session(ts)
    start_day = start_utc.astimezone(MARKET_TIMEZONE).date()
    return f"{start_day.isoformat()}-{code}"


def rth_session_date(ts: datetime) -> date:
    """Date of the RTH open governing this candle's RTH/overnight cycle."""
    local = as_new_york(ts)
    day = local.date()
    if local.time().replace(tzinfo=None) < _SESSION_STARTS["RTH"]:
        day -= timedelta(days=1)
    return day


def market_close_phase(ts: datetime) -> str:
    """Classify the 15:30/15:45 ET pending-cancel and flatten windows."""
    tod = as_new_york(ts).time().replace(tzinfo=None)
    if _FLATTEN <= tod < _REOPEN:
        return MARKET_PHASE_FLATTEN
    if _PRE_FLATTEN <= tod < _FLATTEN:
        return MARKET_PHASE_PRE_FLATTEN
    return MARKET_PHASE_OPEN


def is_market_reopen(ts: datetime) -> bool:
    """True during the post-maintenance ASIA segment beginning at 18:00 ET."""
    local = as_new_york(ts)
    return (
        market_session_code(ts) == "ASIA"
        and local.time().replace(tzinfo=None) >= _REOPEN
    )


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
