from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time
from pathlib import Path

from backend.data import orderflow
from backend.timebase import CHICAGO, UTC
from scripts.databento_orderflow_download import (
    _file_name,
    _folder_name,
    download_request,
    request_payload,
)
from scripts.databento_orderflow_settlement import (
    EXPECTED_RTH_BARS,
    completed_topstep_dates,
    expected_rth_epochs,
    inspect_settlement_day,
    mbo_request_range,
    previous_completed_topstep_date,
    reconcile_payloads,
    validate_cache_payload,
    validate_raw_file,
)


def _ct(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=CHICAGO)


def _bar(epoch: int, delta: int = 1) -> dict:
    return {
        "time": datetime.fromtimestamp(epoch).isoformat(),
        "epoch": epoch,
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "buy": 10 + max(delta, 0),
        "sell": 9 + max(-delta, 0),
        "delta": delta,
        "trades": 1,
        "adds": 0,
        "cancels": 0,
        "fills": 1,
        "ofi": 0,
        "cells": [],
    }


def test_previous_completed_topstep_date_uses_boundary_and_skips_weekend_holiday():
    assert previous_completed_topstep_date(_ct(date(2026, 9, 10), 16)) == date(2026, 9, 9)
    assert previous_completed_topstep_date(_ct(date(2026, 9, 10), 17)) == date(2026, 9, 10)
    assert previous_completed_topstep_date(_ct(date(2026, 9, 13), 18)) == date(2026, 9, 11)
    # 2026-09-07 is the shared Labor Day holiday set.
    assert previous_completed_topstep_date(_ct(date(2026, 9, 8), 16)) == date(2026, 9, 4)


def test_completed_dates_are_newest_first_and_skip_non_sessions():
    assert completed_topstep_dates(anchor=date(2026, 9, 8), count=3) == [
        date(2026, 9, 8), date(2026, 9, 4), date(2026, 9, 3),
    ]


def test_mbo_request_range_is_dst_aware_but_one_utc_calendar_day():
    assert mbo_request_range(date(2026, 9, 10)) == (
        date(2026, 9, 10), date(2026, 9, 11),
    )
    assert mbo_request_range(date(2027, 1, 5)) == (
        date(2027, 1, 5), date(2027, 1, 6),
    )
    assert len(expected_rth_epochs(date(2026, 9, 10))) == EXPECTED_RTH_BARS


def test_settlement_report_treats_naive_run_time_as_utc(tmp_path):
    from scripts.databento_orderflow_settlement import run_settlement

    naive = datetime(2026, 9, 10, 23, 0)
    aware = datetime(2026, 9, 10, 23, 0, tzinfo=UTC)
    naive_report = run_settlement(
        now=naive,
        dates=[date(2026, 9, 4)],
        root=tmp_path / "naive",
        verify_hash=False,
    )
    aware_report = run_settlement(
        now=aware,
        dates=[date(2026, 9, 4)],
        root=tmp_path / "aware",
        verify_hash=False,
    )
    assert naive_report["generated_at"] == aware_report["generated_at"]


def test_cache_validation_requires_schema_and_exact_rth_coverage():
    day = date(2026, 9, 10)
    epochs = expected_rth_epochs(day)
    payload = {
        "meta": {
            "schema_version": orderflow.CACHE_SCHEMA_VERSION,
            "trade_date": day.isoformat(),
        },
        "bars": [_bar(epoch) for epoch in epochs],
    }
    good = validate_cache_payload(payload, day)
    assert good["complete"] is True
    assert good["bar_count"] == EXPECTED_RTH_BARS

    broken = dict(payload, bars=payload["bars"][1:])
    result = validate_cache_payload(broken, day)
    assert result["complete"] is False
    assert "cache_missing_minutes" in result["errors"]


def test_raw_manifest_validation_detects_mutation(tmp_path):
    day = date(2026, 9, 10)
    start, end = mbo_request_range(day)
    symbol = "MNQU6"
    folder = tmp_path / "raw"
    folder.mkdir()
    raw = folder / _file_name("mbo", start, end)
    raw.write_bytes(b"stable raw dbn placeholder")
    request = request_payload("mbo", symbol, start, end)
    digest = hashlib.sha256(raw.read_bytes()).hexdigest()
    (folder / "metadata.json").write_text(
        json.dumps({"query": request}), encoding="utf-8",
    )
    (folder / "manifest.json").write_text(
        json.dumps({"files": [{"name": raw.name, "bytes": raw.stat().st_size, "sha256": digest}]}),
        encoding="utf-8",
    )
    assert validate_raw_file(raw, symbol, day)["complete"] is True

    raw.write_bytes(b"changed raw dbn placeholder")
    invalid = validate_raw_file(raw, symbol, day)
    assert invalid["complete"] is False
    assert "manifest_hash_mismatch" in invalid["errors"]


def test_inspection_finds_missing_day_without_calling_databento(tmp_path):
    result = inspect_settlement_day(
        date(2026, 9, 10), root=tmp_path, symbol_override="MNQU6",
    )
    assert result["action"] == "download_required"
    assert result["raw"]["status"] == "missing"
    assert result["request"]["start"] == "2026-09-10"
    assert result["request"]["end"] == "2026-09-11"


def test_reconciliation_reports_overlap_and_delta_difference():
    history = {"bars": [_bar(1), _bar(2)]}
    live = {"bars": [_bar(1), _bar(2, delta=6), _bar(3)]}
    result = reconcile_payloads(live, history)
    assert result["status"] == "mismatch"
    assert result["overlap_bars"] == 2
    assert result["live_only_bars"] == 1
    assert result["mismatched_bars"] == 1
    assert result["max_abs_delta_diff"] == 5


class _FakeTimeseries:
    def get_range(self, **kwargs):
        Path(kwargs["path"]).write_bytes(b"new dbn payload")


class _FakeClient:
    timeseries = _FakeTimeseries()


def test_download_replacement_keeps_recoverable_old_files(tmp_path):
    source_root = tmp_path / "source" / "orderflow" / "mnq_mbo"
    day = date(2026, 9, 10)
    start, end = mbo_request_range(day)
    symbol = "MNQU6"
    folder = source_root / _folder_name(symbol, "mbo", start, end)
    folder.mkdir(parents=True)
    target = folder / _file_name("mbo", start, end)
    target.write_bytes(b"old dbn payload")
    (folder / "metadata.json").write_text("old metadata", encoding="utf-8")
    (folder / "manifest.json").write_text("old manifest", encoding="utf-8")

    request = request_payload("mbo", symbol, start, end)
    request.update({"record_count": 10, "quoted_cost_usd": 0.0})
    saved, _digest = download_request(
        _FakeClient(), request, source_root, replace_existing=True,
    )
    assert saved.read_bytes() == b"new dbn payload"
    replaced = list(folder.glob("*.replaced-*"))
    assert len(replaced) == 3
    assert {
        path.read_text(encoding="utf-8")
        for path in replaced
        if "metadata.json" in path.name or "manifest.json" in path.name
    } == {
        "old metadata", "old manifest",
    }
