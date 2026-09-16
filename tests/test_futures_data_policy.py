"""Regression tests for the canonical MES/MNQ receive contract."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.data.futures_data import (
    SOURCE_DATABENTO,
    SOURCE_TOPSTEPX,
    flatten_rolls,
    merge_futures_by_policy,
    normalize_futures_candle,
)
from backend.db.models import Candle


UTC = timezone.utc


def _candle(ts: datetime, close: float, source: str) -> Candle:
    return Candle(
        timestamp=ts,
        open=close - 1,
        high=close + 1,
        low=close - 2,
        close=close,
        volume=10,
        symbol="MNQ",
        interval="1m",
        source=source,
    )


def test_normalize_requires_exact_minute_and_canonicalizes_fields():
    raw = _candle(datetime(2026, 9, 1, 13, 30, tzinfo=timezone(timedelta(hours=-4))), 100, "broker")
    got = normalize_futures_candle(raw, symbol="/mnq", source="Topstep")
    assert got.timestamp == datetime(2026, 9, 1, 17, 30, tzinfo=UTC)
    assert got.symbol == "MNQ"
    assert got.interval == "1m"
    assert got.source == SOURCE_TOPSTEPX

    with pytest.raises(ValueError, match="minute aligned"):
        normalize_futures_candle(
            _candle(datetime(2026, 9, 1, 13, 30, 1, tzinfo=UTC), 100, "broker"),
            symbol="MNQ",
            source="topstepx",
        )


def test_historical_databento_wins_but_recent_topstep_wins():
    cutoff = datetime(2026, 8, 14, tzinfo=UTC)
    old_ts = datetime(2026, 8, 1, 13, 30, tzinfo=UTC)
    recent_ts = datetime(2026, 9, 1, 13, 30, tzinfo=UTC)
    existing = [
        _candle(old_ts, 100, SOURCE_TOPSTEPX),
        _candle(recent_ts, 200, SOURCE_DATABENTO),
    ]
    incoming = [
        _candle(old_ts, 101, SOURCE_DATABENTO),
        _candle(recent_ts, 201, SOURCE_TOPSTEPX),
    ]
    merged = merge_futures_by_policy(
        existing,
        incoming,
        recent_cutoff=cutoff,
        start=datetime(2026, 1, 1, tzinfo=UTC),
    )
    by_ts = {c.timestamp: c for c in merged}
    assert by_ts[old_ts].close == 101
    assert by_ts[old_ts].source == SOURCE_DATABENTO
    assert by_ts[recent_ts].close == 201
    assert by_ts[recent_ts].source == SOURCE_TOPSTEPX
    assert len(merged) == 2


def test_flatten_rolls_uses_instrument_seam_and_preserves_real_move():
    seam = datetime(2026, 6, 17, tzinfo=UTC)
    bars = [
        _candle(seam - timedelta(minutes=1), 100, SOURCE_DATABENTO),
        _candle(seam, 120, SOURCE_DATABENTO),
    ]
    flattened = flatten_rolls(bars, [(seam, 20.0, 1, 2)])
    assert flattened[0].close == 120
    assert flattened[1].close == 120
    assert flattened[0].source == SOURCE_DATABENTO

