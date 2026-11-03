"""Live side of the PI options account (Robinhood): settings + ALERT mode.

ALERT mode watches the PI audit stream that both Discord listeners already
write (``recorded`` by the record-only worker, ``received`` by a PI Live
engine; backend/data/pi_live_audit.py) and turns each qualifying signal into
an order ticket for the options account, using the same selection as the
backtest model (backend/options/pi_options.py): long 深蓝圈 (LV2) / 青π (LV3)
on QQQ/SPY, the first per symbol and day, next-session expiry call.

No order is sent.  Placing orders needs the Robinhood Agentic Trading MCP
connection (OAuth sign-in by the account owner), which is not built yet;
PAPER and LIVE are therefore refused here.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.data import market_data
from backend.data.candle_store import _is_us_futures_holiday
from backend.options.pi_options import (
    EXIT_TIMES, STRIKE_CHOICES, TIER_KINDS, PiOptionSettings)
from backend.timebase import NEW_YORK, UTC, utc_now

logger = logging.getLogger(__name__)

MODES = ("alert", "paper", "live")
BROKER_READY_MODES = ("alert",)
SIGNAL_EVENTS = ("recorded", "received")
MAX_SIGNAL_AGE = timedelta(minutes=5)
POLL_SECONDS = 10.0
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)
KIND_TIER = {kind: tier for tier, kind in TIER_KINDS.items()}
STRIKE_OFFSETS = {"atm": 0, "otm_3": 3, "otm_5": 5, "otm_7": 7, "otm_10": 10}


# ── calendar (US equity options) ────────────────────────────────────────────

def _easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def options_market_closed(day: date) -> bool:
    """Weekends, the shared US holiday list, and Good Friday (NYSE/Cboe close;
    CME equity futures sometimes open a short session, so the futures list
    does not carry it)."""
    return (day.weekday() >= 5 or _is_us_futures_holiday(day)
            or day == _easter(day.year) - timedelta(days=2))


def next_session(day: date) -> date:
    candidate = day + timedelta(days=1)
    while options_market_closed(candidate):
        candidate += timedelta(days=1)
    return candidate


# ── settings persistence ────────────────────────────────────────────────────

def settings_path() -> Path:
    return market_data.runtime_path("state", "pi_options_account.json")


def tickets_path() -> Path:
    return market_data.runtime_path("logs", "pi_options_tickets.jsonl")


def _settings_to_json(settings: PiOptionSettings) -> Dict[str, Any]:
    data = asdict(settings)
    data["tiers"] = list(settings.tiers)
    data.pop("start", None)
    data.pop("end", None)
    return data


def settings_from_json(data: Dict[str, Any]) -> PiOptionSettings:
    return PiOptionSettings(
        tiers=tuple(data.get("tiers") or ("LV2", "LV3")),
        strike=str(data.get("strike") or "otm_3"),
        entry_discount=float(data.get("entry_discount") or 0.0),
        stop=data.get("stop"), target=data.get("target"),
        exit_time=str(data.get("exit_time") or "15:30"),
        contracts=data.get("contracts", PiOptionSettings.contracts),
        budget=float(data.get("budget") or 1000.0),
    ).validate()


def load_account(path: Optional[Path] = None) -> Dict[str, Any]:
    path = path or settings_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    settings = settings_from_json(raw.get("settings") or {})
    mode = raw.get("mode") if raw.get("mode") in MODES else "alert"
    return {"mode": mode, "enabled": bool(raw.get("enabled")),
            "settings": _settings_to_json(settings)}


def save_account(mode: str, enabled: bool, settings: Dict[str, Any],
                 path: Optional[Path] = None) -> Dict[str, Any]:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if mode not in BROKER_READY_MODES:
        raise ValueError(f"{mode.upper()} needs the Robinhood connection, which is not set up yet")
    parsed = settings_from_json(settings)
    payload = {"mode": mode, "enabled": bool(enabled), "settings": _settings_to_json(parsed),
               "saved_at": utc_now().isoformat()}
    path = path or settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    temp.replace(path)
    return load_account(path)


# ── tickets ─────────────────────────────────────────────────────────────────

def _parse(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _is_long(row: Dict[str, Any]) -> bool:
    direction = row.get("direction")
    side = str(row.get("side") or "").lower()
    try:
        if direction is not None and int(direction) > 0:
            return True
    except (TypeError, ValueError):
        pass
    return side in {"long", "buy", "bull", "call"}


def build_ticket(row: Dict[str, Any], settings: PiOptionSettings) -> Optional[Dict[str, Any]]:
    """A qualifying audit row -> order ticket; None when the row does not qualify."""
    tier = KIND_TIER.get(str(row.get("kind") or ""))
    symbol = str(row.get("equity") or "").upper()
    source = _parse(row.get("ts"))
    if tier is None or tier not in settings.tiers or symbol not in {"QQQ", "SPY"}:
        return None
    if source is None or not _is_long(row):
        return None
    local = source.astimezone(NEW_YORK)
    if not (SESSION_OPEN <= local.time() < SESSION_CLOSE) or options_market_closed(local.date()):
        return None
    expiry = next_session(local.date())
    exit_at = datetime.combine(expiry, EXIT_TIMES[settings.exit_time], tzinfo=NEW_YORK)
    offset = STRIKE_OFFSETS[settings.strike]
    strike_rule = ("nearest strike to spot" if offset == 0
                   else f"lowest listed strike >= spot + ${offset}")
    entry = ("MARKET (buy at the ask)" if settings.entry_discount == 0
             else f"LIMIT {int(settings.entry_discount * 100)}% below the ask")
    size = (f"{settings.contracts} contract{'s' if settings.contracts > 1 else ''}"
            if settings.contracts else f"budget ${settings.budget:,.0f}")
    return {
        "key": f"{symbol}|{local.date().isoformat()}",
        "message_id": row.get("message_id"),
        "symbol": symbol, "tier": tier, "kind": row.get("kind"),
        "signal_ts": source.isoformat(), "received_at": row.get("received_at"),
        "right": "CALL", "expiration": expiry.isoformat(),
        "strike_rule": strike_rule, "entry": entry,
        "contracts": settings.contracts, "budget": settings.budget,
        "stop": settings.stop, "target": settings.target,
        "exit_at": exit_at.isoformat(),
        "text": (f"BUY {symbol} {expiry:%m/%d} CALL ({strike_rule}), {entry}; {size}; "
                 f"exit {expiry:%m/%d} {settings.exit_time} ET"
                 + (f"; stop -{int(settings.stop * 100)}%" if settings.stop else "")
                 + (f"; target +{int(settings.target * 100)}%" if settings.target else "")),
    }


class PiOptionsAlertRunner:
    """Poll the PI audit stream and emit one ticket per symbol and day."""

    def __init__(self, reader=None, now=utc_now):
        from backend.data.pi_live_audit import load_recent_events

        self._reader = reader or (lambda: load_recent_events(100, events=SIGNAL_EVENTS))
        self._now = now
        self._task: Optional[asyncio.Task] = None
        self._seen: set = set()
        self._taken: set = set()
        self.tickets: List[Dict[str, Any]] = []
        self.last_poll: Optional[str] = None
        self.last_error: Optional[str] = None
        self.started_at: Optional[datetime] = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def poll_once(self, settings: PiOptionSettings) -> List[Dict[str, Any]]:
        now = self._now()
        new: List[Dict[str, Any]] = []
        for row in self._reader():
            ident = (row.get("message_id"), row.get("kind"), row.get("equity"))
            if ident in self._seen:
                continue
            self._seen.add(ident)
            source = _parse(row.get("ts"))
            received = _parse(row.get("received_at")) or source
            # Only fresh signals seen after this runner started (no replay of
            # history into tickets after a restart).
            if source is None or now - source > MAX_SIGNAL_AGE:
                continue
            if self.started_at and received and received < self.started_at - timedelta(seconds=1):
                continue
            ticket = build_ticket(row, settings)
            if ticket is None or ticket["key"] in self._taken:
                continue
            self._taken.add(ticket["key"])
            ticket["created_at"] = now.isoformat()
            new.append(ticket)
        self.last_poll = now.isoformat()
        for ticket in new:
            self.tickets.append(ticket)
            try:
                path = tickets_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(ticket, ensure_ascii=False) + "\n")
            except OSError as exc:
                self.last_error = f"ticket log: {exc}"
            logger.info("[OPTIONS] ticket %s", ticket["text"])
        self.tickets = self.tickets[-50:]
        return new

    def start(self, settings_provider) -> None:
        if self.running:
            return
        self.started_at = self._now()

        async def loop() -> None:
            while True:
                try:
                    self.poll_once(settings_provider())
                    self.last_error = None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # keep alerting after a bad row
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    logger.exception("[OPTIONS] alert poll failed")
                await asyncio.sleep(POLL_SECONDS)

        self._task = asyncio.get_running_loop().create_task(loop(), name="pi-options-alerts")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def status(self) -> Dict[str, Any]:
        return {"running": self.running, "started_at": self.started_at.isoformat() if self.started_at else None,
                "last_poll": self.last_poll, "last_error": self.last_error,
                "tickets": list(reversed(self.tickets[-20:]))}


BROKER_STATUS = {
    "provider": "Robinhood Agentic Trading (MCP)",
    "endpoint": "https://agent.robinhood.com/mcp/trading",
    "connected": False,
    "detail": "Not connected. Orders need an Agentic account and an OAuth sign-in by the account owner.",
}
