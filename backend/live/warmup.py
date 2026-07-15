"""Helpers for deciding whether live signal strategies have enough history.

The broker warm-up endpoint returns 1-minute candles.  FACTOR/PMO strategies
aggregate those candles into completed signal-timeframe bars and apply the same
entry-session filter in both live and backtest engines.  These helpers mirror
that aggregation so a non-empty weekend response is not mistaken for a
complete warm-up.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional, Tuple

from backend.db.models import Candle
from backend.strategy.session_filter import DEFAULT_ALLOWED_SESSIONS, is_allowed_session


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def signal_warmup_spec(params) -> Optional[Tuple[int, int, int]]:
    """Return ``(required bars, timeframe minutes, source seconds)`` if needed."""
    strategy = str(getattr(params, "strategy", "") or "").lower()
    if strategy == "factor":
        required = max(20, int(getattr(params, "factor_warmup_bars", 150) or 150))
        timeframe = max(1, int(getattr(params, "factor_timeframe_minutes", 5) or 5))
    elif strategy == "pmo":
        required = max(20, int(getattr(params, "pmo_warmup_bars", 150) or 150))
        timeframe = max(1, int(getattr(params, "pmo_timeframe_minutes", 5) or 5))
    else:
        return None
    source_seconds = max(1, int(getattr(params, "candle_seconds", 60) or 60))
    return required, timeframe, source_seconds


def completed_signal_bars(candles: Iterable[Candle], params) -> int:
    """Count bars the live strategy would append while replaying ``candles``."""
    spec = signal_warmup_spec(params)
    if spec is None:
        return 0
    _, timeframe, source_seconds = spec
    allowed = getattr(params, "tr_allowed_sessions", DEFAULT_ALLOWED_SESSIONS) or None
    ordered = sorted(candles, key=lambda candle: _utc(candle.timestamp))

    completed = 0
    last_appended: Optional[datetime] = None
    working: Optional[datetime] = None
    for candle in ordered:
        if not is_allowed_session(candle.timestamp, allowed):
            continue
        ts = _utc(candle.timestamp)
        finalized: Optional[datetime] = None
        if source_seconds >= timeframe * 60:
            finalized = ts
        else:
            minute = (ts.minute // timeframe) * timeframe
            bucket = ts.replace(minute=minute, second=0, microsecond=0)
            if working is not None and bucket != working:
                finalized = working
                working = None
            if working is None:
                working = bucket
            # Match FactorSignalStrategy/EMAPMOStrategy exactly: when a gap both
            # rolls the previous bucket and completes the new bucket on this
            # same input candle, the new finalized bar overwrites the old one.
            if (ts.minute % timeframe) == (timeframe - 1):
                finalized = working
                working = None
        if finalized is not None and finalized != last_appended:
            completed += 1
            last_appended = finalized
    return completed


def signal_warmup_progress(candles: Iterable[Candle], params) -> Tuple[int, int]:
    """Return completed/required signal bars; ``required=0`` means not applicable."""
    spec = signal_warmup_spec(params)
    if spec is None:
        return 0, 0
    required, _, _ = spec
    return completed_signal_bars(candles, params), required


def has_sufficient_signal_warmup(candles: Iterable[Candle], params) -> bool:
    completed, required = signal_warmup_progress(candles, params)
    return required == 0 or completed >= required
