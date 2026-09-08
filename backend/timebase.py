"""Canonical clocks and timezone objects for the whole backend.

All stored and calculated instants use UTC. Exchange/session rules convert the
same instant to their named IANA timezone only while evaluating wall-clock
boundaries. Frontend charts receive UTC instants and apply the user's operating
system timezone for display.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from types import MappingProxyType
from zoneinfo import ZoneInfo


UTC = timezone.utc
NEW_YORK = ZoneInfo("America/New_York")
CHICAGO = ZoneInfo("America/Chicago")
LOS_ANGELES = ZoneInfo("America/Los_Angeles")

DATA_TIMEZONE_NAME = "UTC"
MARKET_TIMEZONE_NAME = NEW_YORK.key
TOPSTEP_TIMEZONE_NAME = CHICAGO.key
PI_SOURCE_TIMEZONE_NAME = LOS_ANGELES.key

TIME_ZONE_NAMES = MappingProxyType({
    "data": DATA_TIMEZONE_NAME,
    "market": MARKET_TIMEZONE_NAME,
    "topstep": TOPSTEP_TIMEZONE_NAME,
    "pi_source": PI_SOURCE_TIMEZONE_NAME,
})


def as_utc(value: datetime) -> datetime:
    """Normalize an aware instant or a legacy naive-UTC value to aware UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def utc_now() -> datetime:
    """Current aware UTC instant for new persisted/system timestamps."""
    return datetime.now(UTC)


def utc_now_naive() -> datetime:
    """Legacy-compatible naive UTC used only by existing naive engine fields."""
    return utc_now().replace(tzinfo=None)


def topstep_trade_date(value: datetime) -> str:
    """Topstep trade date; the next trade day starts at 17:00 Chicago time."""
    local = as_utc(value).astimezone(CHICAGO)
    if local.hour >= 17:
        local += timedelta(days=1)
    return local.date().isoformat()


def topstep_trade_date_bounds(trade_date: str) -> tuple[datetime, datetime]:
    """Return the DST-aware UTC window for one Topstep trade date.

    A trade date named ``YYYY-MM-DD`` starts at 17:00 America/Chicago on the
    preceding calendar date and ends at 17:00 America/Chicago on that date.
    Keeping this beside :func:`topstep_trade_date` prevents chart annotations
    from inventing a second daily boundary.
    """
    day = date.fromisoformat(str(trade_date))
    start_local = datetime.combine(
        day - timedelta(days=1), time(17, 0), tzinfo=CHICAGO,
    )
    end_local = datetime.combine(day, time(17, 0), tzinfo=CHICAGO)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def time_zone_manifest() -> dict[str, str]:
    """JSON-safe timezone names for the browser runtime."""
    return dict(TIME_ZONE_NAMES)
