import gzip
from pathlib import Path

import pytest

from scripts.pi_theta_option_pilot import (
    DEFAULT_DATA_ROOT,
    ENTRY_TIME,
    PILOT_FILES,
    _entry_selection,
    _bid_mark_quality,
    _quote_quality,
    _timestamp,
    build_pilot,
    canonical_episode_for_date,
    default_pi_path,
    pilot_files,
)


def test_theta_timestamp_accepts_vendor_offset_without_colon():
    assert _timestamp("2026-03-30T15:02:00.000-0400") == ENTRY_TIME


def test_quote_quality_keeps_zero_bid_when_size_and_ask_are_valid():
    valid, reasons, values = _quote_quality({
        "bid": "0.0",
        "ask": "0.01",
        "bid_size": "1",
        "ask_size": "2",
    })
    assert valid
    assert reasons == []
    assert values["bid"] == 0.0

    crossed, reasons, _ = _quote_quality({
        "bid": "1.01",
        "ask": "1.00",
        "bid_size": "1",
        "ask_size": "1",
    })
    assert not crossed
    assert reasons == ["crossed_market"]


def test_zero_bid_mark_requires_positive_ask_and_separates_execution():
    mark, executable, economic_zero, no_executable, _ = _bid_mark_quality({
        "bid": "0.0",
        "ask": "0.01",
        "bid_size": "0",
        "ask_size": "2",
    })
    assert mark is True
    assert executable is False
    assert economic_zero is True
    assert no_executable is True

    boundary = _bid_mark_quality({
        "bid": "0.0",
        "ask": "0.0",
        "bid_size": "0",
        "ask_size": "0",
    })
    assert boundary[:4] == (False, False, False, False)

    positive_mark_only = _bid_mark_quality({
        "bid": "1.00",
        "ask": "1.10",
        "bid_size": "0",
        "ask_size": "1",
    })
    assert positive_mark_only[:4] == (True, False, False, True)


def test_entry_selection_uses_synchronized_spot_and_actual_listed_strikes(tmp_path: Path):
    path = tmp_path / "greeks.csv.gz"
    fields = [
        "symbol", "expiration", "strike", "right", "timestamp", "bid", "ask",
        "delta", "theta", "vega", "rho", "epsilon", "lambda", "implied_vol",
        "iv_error", "underlying_timestamp", "underlying_price",
    ]
    rows = []
    for strike in (558, 561, 568):
        rows.append({
            "symbol": "QQQ", "expiration": "2026-03-31", "strike": str(strike),
            "right": "CALL", "timestamp": "2026-03-30T15:02:00.000-0400",
            "bid": "1", "ask": "2", "delta": "0.5", "theta": "-1",
            "vega": "1", "rho": "1", "epsilon": "1", "lambda": "1",
            "implied_vol": "0.29", "iv_error": "0",
            "underlying_timestamp": "2026-03-30T15:02:00.000-0400",
            "underlying_price": "557.89",
        })
    with gzip.open(path, "wt", newline="") as stream:
        import csv

        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    spot, selected, details, greeks = _entry_selection(path)
    assert spot == 557.89
    assert selected == {"atm": 558.0, "otm_3": 561.0, "otm_10": 568.0}
    assert details["synchronized_call_rows"] == 3
    assert greeks[558.0]["implied_vol"] == 0.29


def test_saved_pilot_has_three_contracts_and_two_scheduled_exits():
    if not default_pi_path().is_file() or not (DEFAULT_DATA_ROOT / "coverage_manifest.jsonl").is_file() or any(
        not (DEFAULT_DATA_ROOT / relative).is_file() for relative in PILOT_FILES
    ):
        pytest.skip("saved Theta pilot files are unavailable")
    result = build_pilot(DEFAULT_DATA_ROOT, 1.30)
    assert DEFAULT_DATA_ROOT.is_dir()
    assert result["manifest_files_verified_downloaded"] == 6
    assert result["inventory_rows"] == 742268
    assert result["selection"]["underlying_spot"] == 557.89
    assert result["selection"]["actual_strikes"] == {
        "atm": 558.0,
        "otm_3": 561.0,
        "otm_10": 568.0,
    }
    assert len(result["results"]) == 3
    for row in result["results"]:
        assert row["entry_quote_missing"] is False
        assert row["entry_quote_invalid"] is False
        assert set(row["exits"]) == {"next_day_10_00_et", "next_day_15_30_et"}
        assert all(exit_row["exit_quote_invalid"] is False for exit_row in row["exits"].values())
        assert all(exit_row["exit_bid_executable"] is True for exit_row in row["exits"].values())


def test_june9_parameterized_episode_derives_positive_entry_quotes():
    if not default_pi_path().is_file():
        pytest.skip("canonical PI archive is unavailable")
    spec, provenance = canonical_episode_for_date("2026-06-09")
    if not all((DEFAULT_DATA_ROOT / relative).is_file() for relative in pilot_files(spec)):
        pytest.skip("saved June 9 Theta pilot files are unavailable")
    result = build_pilot(DEFAULT_DATA_ROOT, 1.30, spec=spec)
    assert provenance["derived_entry_ts_et"].endswith("12:04:00-04:00")
    assert result["selection"]["actual_strikes"] == {
        "atm": 699.0,
        "otm_3": 702.0,
        "otm_10": 709.0,
    }
    assert [row["entry_ask"] for row in result["results"]] == [7.20, 5.64, 2.87]
    assert all(not row["entry_quote_missing"] and not row["entry_quote_invalid"] for row in result["results"])
