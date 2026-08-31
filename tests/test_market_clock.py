from __future__ import annotations

import asyncio
import json
import runpy
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from backend.db.models import Candle, StrategyParams
from backend.strategy.consolidation import SessionZoneDetector
from backend.strategy.factor import _session_id
from backend.strategy.research_lab import BetaFibRetrace
from backend.strategy.session_filter import (
    MARKET_PHASE_FLATTEN,
    MARKET_PHASE_OPEN,
    MARKET_PHASE_PRE_FLATTEN,
    market_close_phase,
    market_session,
    market_session_code,
    market_session_id,
    rth_session_date,
)
from backend.strategy.sigma import _session_for


UTC = timezone.utc


def _utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


@pytest.mark.parametrize(
    ("ts", "expected"),
    [
        (_utc(2026, 7, 15, 13, 29), "PRE"),
        (_utc(2026, 7, 15, 13, 30), "RTH"),
        (_utc(2026, 1, 15, 14, 29), "PRE"),
        (_utc(2026, 1, 15, 14, 30), "RTH"),
        (_utc(2026, 7, 15, 21, 59), "AH"),
        (_utc(2026, 7, 15, 22, 0), "ASIA"),
        (_utc(2026, 1, 15, 22, 59), "AH"),
        (_utc(2026, 1, 15, 23, 0), "ASIA"),
        # US DST starts: 01:59 EST jumps directly to 03:00 EDT.
        (_utc(2026, 3, 8, 6, 59), "ASIA"),
        (_utc(2026, 3, 8, 7, 0), "EURO"),
        # US DST ends: the repeated 01:00 hour remains ASIA until 03:00 EST.
        (_utc(2026, 11, 1, 7, 59), "ASIA"),
        (_utc(2026, 11, 1, 8, 0), "EURO"),
    ],
)
def test_market_session_code_tracks_new_york_dst(ts: datetime, expected: str):
    assert market_session_code(ts) == expected


def test_market_session_id_keeps_asia_date_across_utc_year_boundary():
    assert market_session_id(_utc(2027, 1, 1, 7, 59)) == "2026-12-31-ASIA"
    assert market_session_id(_utc(2027, 1, 1, 8, 0)) == "2027-01-01-EURO"


@pytest.mark.parametrize(
    ("ts", "expected_phase"),
    [
        (_utc(2026, 7, 15, 19, 29), MARKET_PHASE_OPEN),
        (_utc(2026, 7, 15, 19, 30), MARKET_PHASE_PRE_FLATTEN),
        (_utc(2026, 7, 15, 19, 45), MARKET_PHASE_FLATTEN),
        (_utc(2026, 7, 15, 22, 0), MARKET_PHASE_OPEN),
        (_utc(2026, 1, 15, 20, 29), MARKET_PHASE_OPEN),
        (_utc(2026, 1, 15, 20, 30), MARKET_PHASE_PRE_FLATTEN),
        (_utc(2026, 1, 15, 20, 45), MARKET_PHASE_FLATTEN),
        (_utc(2026, 1, 15, 22, 30), MARKET_PHASE_FLATTEN),
        (_utc(2026, 1, 15, 23, 0), MARKET_PHASE_OPEN),
    ],
)
def test_close_window_is_1530_and_1545_new_york_year_round(
    ts: datetime, expected_phase: str,
):
    assert market_close_phase(ts) == expected_phase


def test_market_session_returns_the_real_utc_start_in_summer_and_winter():
    assert market_session(_utc(2026, 7, 15, 22, 5)) == (
        "ASIA",
        _utc(2026, 7, 15, 22, 0),
    )
    assert market_session(_utc(2026, 1, 15, 23, 5)) == (
        "ASIA",
        _utc(2026, 1, 15, 23, 0),
    )


def test_market_session_spans_the_spring_dst_jump_without_fixed_offset_math():
    assert market_session(_utc(2026, 3, 8, 6, 59)) == (
        "ASIA",
        _utc(2026, 3, 7, 23, 0),
    )
    assert market_session(_utc(2026, 3, 8, 7, 0)) == (
        "EURO",
        _utc(2026, 3, 8, 7, 0),
    )


def test_all_session_consumers_share_the_same_winter_boundary():
    ts = _utc(2026, 1, 15, 14, 30)
    candle = Candle(ts, 100, 101, 99, 100, 10)

    assert SessionZoneDetector()._get_session_id(candle) == "2026-01-15-RTH"
    assert _session_id(ts) == "2026-01-15-RTH"
    assert _session_for(ts) == ("RTH", ts)


def test_betafib_session_day_uses_new_york_rth_open_not_utc_calendar_day():
    strategy = BetaFibRetrace(StrategyParams(strategy="betafib"))

    assert rth_session_date(_utc(2027, 1, 1, 14, 29)) == date(2026, 12, 31)
    assert strategy._session_day(_utc(2027, 1, 1, 14, 29)) == date(2026, 12, 31)
    assert strategy._session_day(_utc(2027, 1, 1, 14, 30)) == date(2027, 1, 1)


def test_legacy_betafib_preset_hours_are_migrated_once():
    from backend.api.routes import _ensure_builtin_presets
    from backend.strategy.session_filter import MARKET_CLOCK_VERSION

    data = {
        "preset_schema": "legacy",
        "presets": {
            "old": {
                "strategy": "betafib",
                "betafib_entry_start_hour": 22,
                "betafib_entry_end_hour": 1,
            }
        },
    }

    migrated, changed = _ensure_builtin_presets(data)
    params = migrated["presets"]["old"]
    assert changed is True
    assert params["market_clock_version"] == MARKET_CLOCK_VERSION
    assert params["betafib_entry_start_hour"] == 18
    assert params["betafib_entry_end_hour"] == 21

    migrated_again, _ = _ensure_builtin_presets(migrated)
    params_again = migrated_again["presets"]["old"]
    assert params_again["betafib_entry_start_hour"] == 18
    assert params_again["betafib_entry_end_hour"] == 21


def test_old_sweep_results_are_preserved_on_disk_but_hidden_until_rerun(
    tmp_path, monkeypatch,
):
    from backend.api import routes
    from backend.strategy.session_filter import MARKET_CLOCK_VERSION

    path = tmp_path / "sweep_results.json"
    old_payload = {"created_at": "2026-07-09T00:00:00Z", "results": [{"pf": 9.9}]}
    path.write_text(json.dumps(old_payload), encoding="utf-8")
    monkeypatch.setattr(routes, "_SWEEP_RESULTS_FILE", path)

    response = asyncio.run(routes.get_backtest_sweep_results())

    assert response["results"] == []
    assert response["market_clock_version"] == MARKET_CLOCK_VERSION
    assert "rerun required" in response["stale_reason"]
    assert json.loads(path.read_text(encoding="utf-8")) == old_payload


def test_pi_purple_study_imports_shared_close_clock_and_history_loader():
    root = Path(__file__).resolve().parents[1]
    namespace = runpy.run_path(
        str(root / "scripts" / "pi_purple_exit_study.py"),
        run_name="_market_clock_import_test",
    )

    assert callable(namespace["simulate_long"])
    assert callable(namespace["market_close_phase"])
    assert callable(namespace["load_rows"])
    assert "FLATTEN_UTC" not in namespace
