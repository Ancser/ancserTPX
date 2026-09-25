from datetime import date
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo

import pytest

from scripts.vp_theta_oi_context import (
    OI_PATHS,
    VP_TRADES_PATH,
    build_context,
    join_oi_context,
    _parse_timestamp,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")


def _trade(entry_time_utc: str, direction: str = "long") -> dict[str, object]:
    entry = _parse_timestamp(entry_time_utc)
    assert entry is not None
    trade_date = entry.astimezone(ET).date()
    return {
        "trade_date": trade_date.isoformat(),
        "entry_time_utc": entry.isoformat(),
        "entry_time_et": entry.astimezone(ET).isoformat(),
        "entry_epoch": int(entry.timestamp()),
        "direction": direction,
        "direction_int": 1 if direction == "long" else -1,
        "edge": "vah",
        "setup": "breakout_retest",
        "period": "test",
        "pnl": 10.0,
    }


def _oi(timestamp: str, expiration: str, strike: float, right: str, open_interest: float, source: str = "month=2026-08.csv.gz") -> dict[str, object]:
    parsed = _parse_timestamp(timestamp)
    assert parsed is not None
    return {
        "symbol": "QQQ",
        "expiration": date.fromisoformat(expiration),
        "strike": strike,
        "right": right,
        "open_interest": open_interest,
        "timestamp": parsed,
        "timestamp_utc": parsed.isoformat(),
        "timestamp_et_date": parsed.astimezone(ET).date(),
        "timestamp_raw": timestamp,
        "source_path": source,
    }


def test_publication_after_entry_is_excluded_from_same_date_join():
    trade = _trade("2026-08-07T14:00:00Z")
    rows = [
        _oi("2026-08-07T09:30:00.000-0400", "2026-08-07", 600, "CALL", 10),
        _oi("2026-08-07T09:30:01.000-0400", "2026-08-08", 590, "PUT", 20),
        _oi("2026-08-07T11:00:00.000-0400", "2026-08-07", 600, "CALL", 999),
    ]

    joined = join_oi_context(trade, rows)

    assert joined["oi_join_status"] == "same_date"
    assert joined["oi_unsigned_total"] == 30
    assert joined["oi_put_call_ratio"] == 2.0
    assert joined["oi_future_same_date_rows_excluded"] == 1
    assert joined["oi_publication_max_utc"] == "2026-08-07T13:30:01+00:00"


def test_weekend_without_publication_uses_labeled_prior_stale_snapshot():
    trade = _trade("2026-08-08T14:00:00Z")
    rows = [_oi("2026-08-07T09:30:00.000-0400", "2026-08-10", 600, "CALL", 25)]

    joined = join_oi_context(trade, rows)

    assert joined["oi_join_status"] == "prior_date_stale"
    assert joined["oi_snapshot_date_et"] == "2026-08-07"
    assert joined["oi_stale_days"] == 1
    assert joined["oi_unsigned_total"] == 25


def test_zero_open_interest_is_retained_and_distinguished_from_missing():
    trade = _trade("2026-08-07T14:00:00Z")
    rows = [
        _oi("2026-08-07T09:30:00.000-0400", "2026-08-07", 600, "CALL", 0),
        _oi("2026-08-07T09:30:00.000-0400", "2026-08-07", 590, "PUT", 0),
    ]

    joined = join_oi_context(trade, rows)

    assert joined["oi_join_status"] == "same_date"
    assert joined["oi_metrics_status"] == "matched_zero_total"
    assert joined["oi_unsigned_total"] == 0
    assert joined["oi_zero_rows"] == 2
    assert joined["oi_put_call_ratio"] is None


def test_cli_help_smoke_from_repo_root():
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "vp_theta_oi_context.py"), "--help"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--oi-august" in result.stdout


def test_saved_vp_and_aug_sep_oi_join_runs_when_external_files_exist():
    required = [VP_TRADES_PATH, *OI_PATHS]
    if any(not Path(path).is_file() for path in required):
        pytest.skip("saved VP rows or bounded QQQ OI partitions are unavailable")

    result = build_context()

    assert result["trade_inventory"]["trades"] == 22
    assert result["trade_inventory"]["independent_dates"] == 17
    assert result["trade_inventory"]["directions"] == {"long": 13, "short": 9}
    assert result["oi_input"]["rows_read"] == 218052
    assert result["availability"]["oi_join_status"]["same_date"]["trades"] == 21
    assert result["availability"]["oi_join_status"]["prior_date_stale"]["trades"] == 1
    assert result["availability"]["old_gex_oi_status"]["matched"]["trades"] == 9
    assert result["median_split"]["status"] == "ready"
    assert result["median_split"]["first_half_trades"] == 11
    assert result["median_split"]["later_half_trades"] == 11
    assert all(
        row["oi_publication_max_utc"] <= row["entry_time_utc"]
        for row in result["trades"]
    )
