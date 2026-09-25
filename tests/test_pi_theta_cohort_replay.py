from __future__ import annotations

import subprocess
import sys
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts.pi_theta_cohort_replay import (
    DEFAULT_DATA_ROOT,
    EXIT_TIMES,
    _leave_one_market_date_out,
    _market_date_clusters,
    build_replay,
    mark_timing,
    replay_mark,
    select_atm_from_greeks,
    underlying_lag_summary,
)
from scripts.pi_underlying_path_study_20260921 import following_sessions


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")


def _stamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _greek_row(
    stamp: datetime,
    strike: float,
    right: str,
    *,
    spot: float = 100.0,
    underlying_timestamp: datetime | None = None,
    bid: float = 1.0,
    ask: float = 1.2,
    iv_error: float = 0.0,
) -> dict[str, str]:
    return {
        "timestamp": _stamp(stamp),
        "strike": str(strike),
        "right": right,
        "bid": str(bid),
        "ask": str(ask),
        "underlying_timestamp": _stamp(underlying_timestamp or stamp),
        "underlying_price": str(spot),
        "implied_vol": "0.25",
        "iv_error": str(iv_error),
    }


def _quote_row(
    stamp: datetime,
    strike: float,
    right: str,
    *,
    bid: float = 1.0,
    ask: float = 1.2,
    bid_size: float = 5.0,
    ask_size: float = 5.0,
) -> dict[str, str]:
    return {
        "timestamp": _stamp(stamp),
        "strike": str(strike),
        "right": right,
        "bid": str(bid),
        "ask": str(ask),
        "bid_size": str(bid_size),
        "ask_size": str(ask_size),
    }


def _cohort_cache(
    *,
    entry: datetime,
    exit_day: date,
    greek_rows: list[dict[str, str]],
    quote_rows: list[dict[str, str]],
    exit_rows: list[dict[str, str]],
) -> dict:
    from scripts.pi_theta_option_pilot import _timestamp, _number

    greeks = {}
    for row in greek_rows:
        stamp = _timestamp(row["timestamp"])
        greeks.setdefault((stamp, row["right"]), []).append(row)
    quotes = {}
    for row in quote_rows:
        quotes[(_timestamp(row["timestamp"]), _number(row["strike"]), row["right"])] = row
    exits = {}
    for row in exit_rows:
        exits[(_timestamp(row["timestamp"]), _number(row["strike"]), row["right"])] = row
    return {
        "event_greeks": greeks,
        "event_quotes": quotes,
        "exit_quotes": exits,
        "timings": {},
        "diagnostics": {},
    }


def _mark(source: datetime, *, direction: int = 1, kind: str = "青π", level: int = 2) -> dict:
    return {
        "symbol": "QQQ",
        "event_date": source.astimezone(ET).date(),
        "kind": kind,
        "level": level,
        "direction": direction,
        "option_right": "CALL" if direction > 0 else "PUT",
        "source_timestamp_utc": source.astimezone(ZoneInfo("UTC")).isoformat(),
    }


def _cohort(mark: dict, exit_date: date = date(2026, 3, 9)) -> dict:
    return {
        "cohort_id": f"QQQ:{mark['event_date']}:{exit_date}",
        "symbol": "QQQ",
        "event_date": mark["event_date"],
        "exit_date": exit_date,
        "expiration": exit_date,
        "marks": [mark],
    }


def test_atm_selection_supports_call_and_put_and_rejects_later_underlying_spot():
    entry = datetime(2026, 3, 6, 9, 31, tzinfo=ET)
    rows = [
        _greek_row(entry, 99, "CALL"),
        _greek_row(entry, 100, "CALL"),
        _greek_row(entry, 101, "CALL"),
        _greek_row(entry, 99, "PUT"),
        _greek_row(entry, 100, "PUT"),
        _greek_row(entry, 101, "PUT"),
        _greek_row(entry, 102, "CALL", underlying_timestamp=entry.replace(minute=entry.minute + 1)),
    ]
    call = select_atm_from_greeks(rows, entry, "CALL")
    put = select_atm_from_greeks(rows, entry, "PUT")
    assert call["status"] == "ok"
    assert call["selected_strike"] == 100
    assert call["underlying_timestamp_lag_seconds"] == 0
    assert put["status"] == "ok"
    assert put["selected_strike"] == 100
    assert call["rejected_reason_counts"].get("underlying_timestamp_after_entry") == 1


def test_outside_requested_window_mark_remains_denominator_without_invented_entry():
    source = datetime(2026, 3, 6, 16, 1, tzinfo=ET)
    mark = _mark(source)
    timing = mark_timing(mark)
    assert timing["timing_status"] == "outside_requested_1600_window"
    assert timing["extended_session_status"] == "extended_session_unobserved"
    cache = _cohort_cache(entry=source, exit_day=date(2026, 3, 9), greek_rows=[], quote_rows=[], exit_rows=[])
    result = replay_mark(mark, _cohort(mark), cache)
    assert result["status"] == "outside_requested_1600_window"
    assert result["exits"] == {}


def test_zero_bid_mark_only_and_missing_exit_are_separate():
    source = datetime(2026, 3, 6, 9, 30, tzinfo=ET)
    entry = datetime(2026, 3, 6, 9, 31, tzinfo=ET)
    mark = _mark(source)
    greek_rows = [_greek_row(entry, 100, "CALL")]
    quote_rows = [_quote_row(entry, 100, "CALL", bid=0, ask=2.0, bid_size=0, ask_size=10)]
    exit_rows = [
        _quote_row(
            datetime(2026, 3, 9, 10, 0, tzinfo=ET),
            100,
            "CALL",
            bid=0,
            ask=0.1,
            bid_size=5,
            ask_size=5,
        )
    ]
    cache = _cohort_cache(
        entry=entry,
        exit_day=date(2026, 3, 9),
        greek_rows=greek_rows,
        quote_rows=quote_rows,
        exit_rows=exit_rows,
    )
    result = replay_mark(mark, _cohort(mark), cache)
    assert result["status"] == "entry_valid"
    assert result["entry"]["entry_ask_executable"] is True
    assert result["exits"]["next_day_10_00"]["status"] == "mark"
    assert result["exits"]["next_day_10_00"]["quote_executable_exit"] is True
    assert result["exits"]["next_day_10_00"]["economic_zero_bid"] is True
    assert result["exits"]["next_day_15_30"]["status"] == "missing"


def test_market_date_denominator_and_mixed_direction_cluster():
    def case(day: str, direction: str, pnl: float) -> dict:
        row = {
            "status": "mark",
            "gross_pnl_per_contract_usd": pnl,
            "net_pnl_per_contract_usd": pnl - 1.3,
            "quote_executable_exit": True,
            "mark_only": False,
            "economic_zero_bid": False,
        }
        return {
            "event_date": day,
            "symbol": "QQQ",
            "direction": direction,
            "kind": "青π",
            "level": 2,
            "status": "entry_valid",
            "exits": {"next_day_10_00": row, "next_day_15_30": row},
        }

    cases = [
        case("2026-03-06", "long", 10),
        case("2026-03-06", "short", -5),
        case("2026-03-09", "long", 7),
    ]
    clusters = _market_date_clusters(cases, [
        {"cohort_id": "QQQ:2026-03-06:2026-03-09", "event_date": date(2026, 3, 6)},
        {"cohort_id": "SPY:2026-03-06:2026-03-09", "event_date": date(2026, 3, 6)},
        {"cohort_id": "QQQ:2026-03-09:2026-03-10", "event_date": date(2026, 3, 9)},
    ])
    assert clusters[0]["market_date"] == "2026-03-06"
    assert clusters[0]["marks"] == 2
    assert clusters[0]["mixed_direction_marks"] is True
    influence = _leave_one_market_date_out(cases)
    assert influence["market_dates"] == 2
    assert influence["by_excluded_market_date"]["2026-03-06"]["excluded_marks"] == 2
    assert influence["by_excluded_market_date"]["2026-03-06"]["remaining_market_dates"] == 1


def test_calendar_next_session_skips_weekend():
    assert following_sessions(date(2026, 3, 6), 1) == [date(2026, 3, 9)]


def test_stale_underlying_lag_has_explicit_exclusion_sensitivity():
    exit_row = {
        "status": "mark",
        "gross_pnl_per_contract_usd": 10.0,
        "net_pnl_per_contract_usd": 8.7,
        "quote_executable_exit": True,
        "mark_only": False,
        "economic_zero_bid": False,
    }
    case = {
        "mark_identity": "QQQ|2026-03-06T14:30:00+00:00|青π|2",
        "status": "entry_valid",
        "selection": {"status": "ok", "underlying_timestamp_lag_seconds": 61.0},
        "exits": {"next_day_10_00": exit_row, "next_day_15_30": exit_row},
    }
    summary = underlying_lag_summary([case])
    assert summary["max_seconds"] == 61.0
    assert summary["over_60_seconds"] == 1
    assert summary["over_5_minutes"] == 0
    assert summary["sensitivity_excluding_stale"]["excluded_entry_valid_marks"] == 1
    assert summary["sensitivity_excluding_stale"]["aggregate"]["marks_denominator"] == 0


def test_direct_cli_help_smoke():
    completed = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "pi_theta_cohort_replay.py"), "--help"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    assert "--month" in completed.stdout


def test_underlying_lag_summary_keeps_unobserved_extended_window_mark():
    outside = {
        "mark_identity": "QQQ|2026-04-02T20:01:00+00:00|cyan|1",
        "status": "outside_requested_1600_window",
        "selection": None,
        "exits": {},
    }
    result = underlying_lag_summary([outside])
    assert result["selection_rows_with_lag"] == 0
    assert result["sensitivity_excluding_stale"]["excluded_entry_valid_marks"] == 0
    assert result["sensitivity_excluding_stale"]["aggregate"]["marks_denominator"] == 0


def test_saved_march_replay_actual_files():
    required = [
        DEFAULT_DATA_ROOT / "theta_pi_cohort_manifest.json",
        DEFAULT_DATA_ROOT / "coverage_manifest.jsonl",
        Path(r"F:\ancserQuant\ancserMarketData\source\discord\pi\pi_signals.json"),
    ]
    if any(not path.is_file() for path in required):
        pytest.skip("saved Theta cohort files are unavailable")
    result = build_replay(DEFAULT_DATA_ROOT, month="2026-03")
    assert result["trade_inventory"]["cohorts"] == 28
    assert result["trade_inventory"]["marks"] == 50
    assert result["trade_inventory"]["independent_trading_dates"] == 15
    assert result["trade_inventory"]["directions"] == {"long": 34, "short": 16}
    assert result["global_canonical_inventory"]["marks"] == 481
    assert result["global_canonical_inventory"]["outside_requested_1600_window"] == 8
    assert result["primary"]["aggregate"]["marks_denominator"] == 50
    assert result["input"]["coverage_validation"]["files_validated"] == 112
    assert result["input"]["coverage_validation"]["files_by_role"] == {
        "march_monthly_batch": 104,
        "pilot": 8,
    }
    assert result["trade_inventory"]["mixed_direction_cohorts"] == ["QQQ:2026-03-06:2026-03-09"]
    assert result["primary"]["underlying_lag"]["selection_rows_with_lag"] > 0
