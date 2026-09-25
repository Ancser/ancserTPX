from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from scripts.vp_theta_oi_5yr_study import (
    COMPLETE_CHAIN_STATES,
    _join_trade_snapshot,
    _structure_metrics,
    choose_snapshot_date,
    parity_gate,
)


def _row(
    timestamp: str,
    *,
    expiration: str = "2026-03-20",
    strike: float = 500.0,
    right: str = "CALL",
    open_interest: float = 100.0,
) -> dict:
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return {
        "symbol": "QQQ",
        "expiration": date.fromisoformat(expiration),
        "strike": strike,
        "right": right,
        "open_interest": open_interest,
        "timestamp": parsed,
        "timestamp_raw": timestamp,
        "timestamp_et_date": parsed.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York")).date(),
        "source_path": r"F:\oi\month=2026-03.csv.gz",
    }


def _trade(entry: str, *, direction: str = "long", pnl: float = 10.0) -> dict:
    parsed = datetime.fromisoformat(entry.replace("Z", "+00:00"))
    et = parsed.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York"))
    return {
        "entry_time_utc": parsed.astimezone(timezone.utc).isoformat(),
        "entry_date_et": et.date().isoformat(),
        "year": str(et.year),
        "period": "training",
        "direction": direction,
        "edge": "vah" if direction == "long" else "val",
        "pnl": pnl,
    }


def test_publication_leakage_is_excluded_and_zero_oi_listing_is_economic_neutral() -> None:
    trade = _trade("2026-03-05T12:00:00+00:00")
    rows = [
        _row("2026-03-05T11:30:00+00:00", strike=500, right="CALL", open_interest=100),
        _row("2026-03-05T11:30:00+00:00", strike=500, right="PUT", open_interest=50),
        _row(
            "2026-03-05T20:15:00+00:00",
            expiration="2026-04-17",
            strike=510,
            right="CALL",
            open_interest=0,
        ),
    ]
    joined = _join_trade_snapshot(
        trade,
        rows,
        snapshot_date=date(2026, 3, 5),
        preferred_status="same_date",
        same_date_session_rows=3,
        same_date_reason=None,
    )
    assert joined["oi_future_same_date_rows_excluded"] == 1
    assert joined["oi_selected_raw_rows"] == 2
    assert joined["oi_snapshot_session_rows"] == 3
    assert joined["oi_chain_completeness"] == "partial_same_date_chain"
    assert joined["oi_selected_raw_row_fraction"] == pytest.approx(2 / 3)
    assert joined["oi_positive_oi_coverage"] == 1.0
    assert joined["oi_positive_oi_shortfall"] == 0.0
    assert joined["oi_unsigned_total"] == 150.0


def test_weekend_or_holiday_like_missing_same_date_uses_prior_vendor_date() -> None:
    sessions = {
        date(2026, 3, 6): {
            "min_timestamp": datetime(2026, 3, 6, 11, 30, tzinfo=timezone.utc),
            "rows": 1,
        },
        date(2026, 3, 9): {
            "min_timestamp": datetime(2026, 3, 9, 20, 15, tzinfo=timezone.utc),
            "rows": 1,
        },
    }
    snapshot, status, reason = choose_snapshot_date(
        date(2026, 3, 9),
        datetime(2026, 3, 9, 14, 35, tzinfo=timezone.utc),
        sessions,
    )
    assert snapshot == date(2026, 3, 6)
    assert status == "prior_date_stale"
    assert reason == "same_date_future_only"

    trade = _trade("2026-03-09T14:35:00+00:00")
    joined = _join_trade_snapshot(
        trade,
        [_row("2026-03-06T11:30:00+00:00")],
        snapshot_date=date(2026, 3, 6),
        preferred_status="prior_date_stale",
        same_date_session_rows=1,
        same_date_reason="no_same_date_vendor_publication",
    )
    assert joined["oi_join_status"] == "prior_date_stale"
    assert joined["oi_stale_days"] == 3
    assert joined["oi_effective_session"] == "unknown_until_calendar_join"


def test_expired_contracts_are_excluded_from_structure_metrics() -> None:
    rows = [
        _row(
            "2026-03-05T11:30:00+00:00",
            expiration="2026-03-01",
            strike=490,
            right="PUT",
            open_interest=900,
        ),
        _row(
            "2026-03-05T11:30:00+00:00",
            expiration="2026-03-20",
            strike=500,
            right="CALL",
            open_interest=100,
        ),
    ]
    metrics = _structure_metrics(rows, date(2026, 3, 5))
    assert metrics["oi_expired_rows_excluded"] == 1
    assert metrics["oi_unsigned_total"] == 100.0
    assert metrics["oi_call_total"] == 100.0
    assert metrics["oi_put_total"] == 0.0


def test_zero_oi_is_retained_and_zero_total_is_explicit() -> None:
    rows = [
        _row("2026-03-05T11:30:00+00:00", right="CALL", open_interest=0),
        _row("2026-03-05T11:30:00+00:00", right="PUT", open_interest=0),
    ]
    metrics = _structure_metrics(rows, date(2026, 3, 5))
    assert metrics["oi_zero_rows"] == 2
    assert metrics["oi_unsigned_total"] == 0.0
    assert metrics["oi_put_call_ratio"] is None
    assert metrics["oi_metrics_status"] == "matched_zero_total"


def test_baseline_parity_gate_checks_n_pnl_and_pf() -> None:
    expected = {"baseline": {"n": 3, "pnl": -1.25, "pf": 0.9123}}
    actual = {"baseline": {"n": 3, "pnl": -1.25, "pf": 0.9123}}
    assert parity_gate(actual, expected)["pass"]
    assert not parity_gate(
        {"baseline": {"n": 4, "pnl": -1.25, "pf": 0.9123}},
        expected,
    )["pass"]


def test_chain_state_constants_keep_partial_out_of_primary_calibration() -> None:
    assert COMPLETE_CHAIN_STATES == {
        "complete_same_date_chain",
        "complete_prior_date_chain",
    }
