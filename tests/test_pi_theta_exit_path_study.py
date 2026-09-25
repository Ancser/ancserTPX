from datetime import datetime
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo

import pytest

from scripts.pi_theta_exit_path_study import (
    DEFAULT_DATA_ROOT,
    first_touch,
    build_study,
    _usable_bid_observations,
)
from scripts.pi_theta_option_pilot import (
    canonical_episode_specs,
    default_pi_path,
    pilot_files,
)


ET = ZoneInfo("America/New_York")
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _observation(timestamp: str, bid: float) -> dict[str, object]:
    return {
        "status": "mark",
        "timestamp": datetime.fromisoformat(timestamp).replace(tzinfo=ET),
        "timestamp_et": timestamp,
        "bid": bid,
        "ask": max(bid, 0.01) + 0.01,
        "bid_size": 1.0,
        "ask_size": 1.0,
        "executable_bid": True,
        "mark_only": False,
        "economic_zero_bid": bid == 0,
        "no_executable_bid": False,
    }


def test_first_touch_uses_first_observed_bid_and_actual_gap_price():
    observations = [
        _observation("2026-03-31T09:31:00", 1.20),
        _observation("2026-03-31T09:32:00", 0.70),
        _observation("2026-03-31T09:33:00", 1.60),
    ]

    touch, reason = first_touch(observations, 1.00, 0.75, 1.50)

    assert reason == "stop"
    assert touch is observations[1]
    assert touch["bid"] == 0.70


def test_first_touch_returns_take_when_take_is_first_threshold():
    observations = [
        _observation("2026-03-31T09:31:00", 1.60),
        _observation("2026-03-31T09:32:00", 0.40),
    ]

    touch, reason = first_touch(observations, 1.00, 0.50, 1.50)

    assert reason == "take"
    assert touch is observations[0]


def test_first_touch_includes_immediate_entry_bid_for_wide_spread_stop():
    entry_time = datetime(2026, 3, 31, 9, 30, tzinfo=ET)
    entry_row = {
        "bid": "0.70",
        "ask": "1.00",
        "bid_size": "1",
        "ask_size": "1",
    }

    observations = _usable_bid_observations(
        {entry_time: entry_row},
        {},
        entry_time,
        datetime(2026, 3, 31, 15, 30, tzinfo=ET),
        entry_row=entry_row,
    )
    touch, reason = first_touch(observations, 1.00, 0.75, 1.50)

    assert len(observations) == 1
    assert reason == "stop"
    assert touch["source"] == "entry"
    assert touch["timestamp_et"].endswith("09:30:00-04:00")


def test_cli_help_smoke_from_repository_root():
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "pi_theta_exit_path_study.py"), "--help"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--commission-round-trip" in result.stdout


def test_saved_five_episode_exit_path_study_runs_when_external_archive_exists():
    pi_path = default_pi_path()
    if not pi_path.is_file():
        pytest.skip("canonical PI archive is unavailable")
    manifest_path = DEFAULT_DATA_ROOT / "coverage_manifest.jsonl"
    if not manifest_path.is_file():
        pytest.skip("saved Theta coverage manifest is unavailable")

    # Keep CI portable: resolve canonical episodes only after the archive check.
    episodes = canonical_episode_specs(pi_path)
    if len(episodes) != 5:
        pytest.skip(f"expected five saved canonical episodes, found {len(episodes)}")
    if any(
        not all((DEFAULT_DATA_ROOT / relative).is_file() for relative in pilot_files(spec))
        for spec, _ in episodes
    ):
        pytest.skip("one or more saved Theta episode file groups are unavailable")

    result = build_study(DEFAULT_DATA_ROOT, 1.30, 1000.0, pi_path)

    assert result["independent_event_dates"] == 5
    assert len(set(result["event_dates"])) == 5
    assert all(len(episode["cases"]) == 3 for episode in result["episodes"])
    assert all(
        len(case["scenarios"]) == 4
        for episode in result["episodes"]
        for case in episode["cases"]
    )
    assert all(
        case["entry_ask"] > 0
        for episode in result["episodes"]
        for case in episode["cases"]
    )
    assert all(
        case["entry_ask_executable"]
        for episode in result["episodes"]
        for case in episode["cases"]
    )
    assert all(
        case["entry_bid_executable"]
        for episode in result["episodes"]
        for case in episode["cases"]
    )
    assert result["aggregate_by_selection_and_scenario"]["atm"]["bracket_A"]["ok_cases"] == 5
    assert result["aggregate_by_selection_and_scenario"]["atm"]["bracket_A"]["quote_executable_exit_cases"] == 5
    assert result["aggregate_by_selection_and_scenario"]["otm_10"]["baseline_15_30"]["mark_only_bid_cases"] == 2
    assert result["aggregate_by_selection_and_scenario"]["otm_10"]["baseline_15_30"]["quote_executable_exit_cases"] == 3
