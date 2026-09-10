"""Concurrency and reuse contracts for the MNQ chart marker projection."""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone

from backend.api import routes
from backend.db.models import Candle


UTC = timezone.utc


def _bars(count: int = 8) -> list[Candle]:
    start = datetime(2026, 9, 9, 13, 30, tzinfo=UTC)
    return [
        Candle(
            timestamp=start + timedelta(minutes=index),
            open=100.0 + index,
            high=101.0 + index,
            low=99.0 + index,
            close=100.5 + index,
            volume=10 + index,
            symbol="MNQ",
            interval="1m",
        )
        for index in range(count)
    ]


def test_mnq_markers_run_off_event_loop_and_are_reused_for_same_generation(monkeypatch):
    original_candles = routes._historical_candles
    original_generation = routes._historical_candles_generation
    original_cache = routes._mnq_signal_marker_cache
    seen: list[tuple[int, int, int]] = []

    def fake_builder(candles: tuple[Candle, ...], limit: int):
        seen.append((threading.get_ident(), len(candles), limit))
        return {"signals": [], "count": 0, "shown": 0, "symbol": "MNQ"}

    try:
        routes._historical_candles = _bars()
        routes._historical_candles_generation = original_generation + 1
        routes._mnq_signal_marker_cache = {}
        caller_thread = threading.get_ident()
        monkeypatch.setattr(routes, "_build_mnq_signal_markers", fake_builder)

        first = asyncio.run(routes.get_mnq_signal_markers(limit=120))
        second = asyncio.run(routes.get_mnq_signal_markers(limit=120))

        assert first is second
        assert len(seen) == 1
        assert seen[0][1:] == (8, 120)
        assert seen[0][0] != caller_thread
    finally:
        routes._historical_candles = original_candles
        routes._historical_candles_generation = original_generation
        routes._mnq_signal_marker_cache = original_cache


def test_mnq_marker_cache_is_not_reused_after_candle_generation_changes(monkeypatch):
    original_candles = routes._historical_candles
    original_generation = routes._historical_candles_generation
    original_cache = routes._mnq_signal_marker_cache
    calls: list[int] = []

    def fake_builder(candles: tuple[Candle, ...], limit: int):
        calls.append(len(candles))
        return {"signals": [], "count": len(candles), "shown": 0, "symbol": "MNQ"}

    try:
        routes._historical_candles = _bars()
        routes._historical_candles_generation = original_generation + 1
        routes._mnq_signal_marker_cache = {}
        monkeypatch.setattr(routes, "_build_mnq_signal_markers", fake_builder)

        asyncio.run(routes.get_mnq_signal_markers(limit=120))
        routes._historical_candles.append(Candle(
            timestamp=routes._historical_candles[-1].timestamp + timedelta(minutes=1),
            open=108.0,
            high=109.0,
            low=107.0,
            close=108.5,
            volume=18,
            symbol="MNQ",
            interval="1m",
        ))
        routes._historical_candles_generation += 1
        routes._mnq_signal_marker_cache.clear()
        asyncio.run(routes.get_mnq_signal_markers(limit=120))

        assert calls == [8, 9]
    finally:
        routes._historical_candles = original_candles
        routes._historical_candles_generation = original_generation
        routes._mnq_signal_marker_cache = original_cache

