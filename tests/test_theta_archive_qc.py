from __future__ import annotations

import csv
import gzip
import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

from scripts.theta_archive_qc import audit_archive, _coverage_compare


def _write_gzip_csv(path: Path, rows: list[dict[str, str]]) -> tuple[int, int, str, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0])
    stream = __import__("io").StringIO()
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    raw = stream.getvalue().encode("utf-8")
    compressed = gzip.compress(raw, mtime=0)
    path.write_bytes(compressed)
    return len(raw), len(compressed), hashlib.sha256(raw).hexdigest(), hashlib.sha256(compressed).hexdigest()


def _record(root: Path, *, request_id: str, dataset: str, path: Path, rows: list[dict[str, str]]) -> dict:
    raw_bytes, compressed_bytes, raw_hash, gzip_hash = _write_gzip_csv(path, rows)
    relative = str(path.relative_to(root)).replace("\\", "/")
    return {
        "request_id": request_id,
        "dataset": dataset,
        "symbol": "QQQ",
        "archive_month": "2021-01",
        "coverage_start_date": "2021-01-01",
        "coverage_end_date": "2021-01-31",
        "response_path": relative,
        "status": "downloaded",
        "rows": len(rows),
        "raw_bytes": raw_bytes,
        "compressed_bytes": compressed_bytes,
        "sha256_raw": raw_hash,
        "sha256_gzip": gzip_hash,
    }


def test_audit_validates_hashes_rows_dates_duplicates_and_quality(tmp_path: Path):
    eod_rows = [
        {
            "symbol": "QQQ", "expiration": "2021-01-15", "strike": "100", "right": "CALL",
            "created": "2021-01-04T19:00:00-0500", "volume": "0", "bid": "1.0", "ask": "1.1",
        },
        {
            "symbol": "QQQ", "expiration": "2021-01-15", "strike": "100", "right": "CALL",
            "created": "2021-01-05T19:00:00-0500", "volume": "2", "bid": "0.0", "ask": "1.1",
        },
    ]
    oi_rows = [
        {
            "symbol": "QQQ", "expiration": "2021-01-15", "strike": "100", "right": "CALL",
            "timestamp": "2021-01-04T06:30:01-0500", "open_interest": "0",
        },
        {
            "symbol": "QQQ", "expiration": "2021-01-15", "strike": "100", "right": "CALL",
            "timestamp": "2021-01-04T06:30:01-0500", "open_interest": "-1",
        },
        {
            "symbol": "QQQ", "expiration": "2021-01-15", "strike": "100", "right": "CALL",
            "timestamp": "2021-01-04T06:30:01-0500", "open_interest": "0",
        },
    ]
    eod_path = tmp_path / "raw/QQQ/monthly_eod_dte_0_60/month=2021-01.csv.gz"
    oi_path = tmp_path / "raw/QQQ/monthly_open_interest_dte_0_60/month=2021-01.csv.gz"
    records = [
        _record(tmp_path, request_id="eod", dataset="monthly_eod_dte_0_60", path=eod_path, rows=eod_rows),
        _record(tmp_path, request_id="oi", dataset="monthly_open_interest_dte_0_60", path=oi_path, rows=oi_rows),
    ]
    (tmp_path / "coverage_manifest.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )

    result = audit_archive(tmp_path, expected_monthly=2, expected_episode=0)

    assert result["request_counts"]["total_count_match"] is True
    assert result["integrity"]["all_hashes_and_rows_match"] is True
    assert result["raw_file_counts"]["actual_gzip_files"] == 2
    assert result["quality"]["exact_duplicate_rows"] == 1
    assert result["quality"]["natural_duplicate_rows"] == 2
    assert result["quality"]["negative_counts"]["open_interest"] == 1
    assert result["quality"]["zero_counts"]["open_interest"] == 2
    assert result["calendar"]["available"] is False
    oi_coverage = result["monthly_coverage"][0]["oi_timestamp"]
    assert oi_coverage["effective_session"] == "unknown_until_calendar_join"
    assert oi_coverage["calendar"]["status"] == "calendar_unavailable"
    assert result["quality"]["oi_publication"]["outside_90_minutes_of_0630_et"] == 0


def test_off_hour_publication_diagnostic_is_bounded_and_grouped(tmp_path: Path):
    rows = [{
        "symbol": "QQQ", "expiration": "2021-01-15", "strike": "100", "right": "CALL",
        "timestamp": "2021-01-04T10:00:00-0500", "open_interest": "1",
    }]
    path = tmp_path / "raw/QQQ/monthly_open_interest_dte_0_60/month=2021-01.csv.gz"
    record = _record(tmp_path, request_id="off-hour", dataset="monthly_open_interest_dte_0_60", path=path, rows=rows)
    (tmp_path / "coverage_manifest.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

    result = audit_archive(tmp_path, expected_monthly=1, expected_episode=0)

    publication = result["quality"]["oi_publication"]
    assert publication["outside_90_minutes_of_0630_et"] == 1
    assert publication["top_off_hour_dates"] == [["2021-01-04", 1]]
    assert publication["top_off_hour_times_et"] == [["10:00:00-0500", 1]]


def test_audit_detects_manifest_supersession_and_hash_mismatch(tmp_path: Path):
    rows = [{
        "symbol": "SPY", "expiration": "2021-01-15", "strike": "100", "right": "PUT",
        "created": "2021-01-04T19:00:00-0500", "volume": "1",
    }]
    path = tmp_path / "raw/SPY/monthly_eod_dte_0_60/month=2021-01.csv.gz"
    record = _record(tmp_path, request_id="same", dataset="monthly_eod_dte_0_60", path=path, rows=rows)
    record["sha256_gzip"] = "0" * 64
    superseded = dict(record, status="forbidden", response_path="raw/SPY/missing.csv.gz")
    (tmp_path / "coverage_manifest.jsonl").write_text(
        json.dumps(superseded) + "\n" + json.dumps(record) + "\n", encoding="utf-8"
    )

    result = audit_archive(tmp_path, expected_monthly=1, expected_episode=0)

    assert result["manifest_lines"] == 2
    assert result["latest_unique_requests"] == 1
    assert result["superseded_manifest_records"] == 1
    assert result["integrity"]["all_hashes_and_rows_match"] is False
    assert result["integrity"]["hash_or_row_mismatches"] == [record["response_path"]]


def test_calendar_comparison_is_explicit_when_a_provider_is_available():
    class FakeCalendar:
        def schedule(self, *, start_date, end_date):
            return SimpleNamespace(index=[
                datetime(2021, 1, 4), datetime(2021, 1, 5), datetime(2021, 1, 6)
            ])

    compared = _coverage_compare({"2021-01-04", "2021-01-06"}, date(2021, 1, 4), date(2021, 1, 6), FakeCalendar())

    assert compared["status"] == "compared"
    assert compared["expected_session_count"] == 3
    assert compared["missing_dates"] == ["2021-01-05"]


def test_partial_month_coverage_uses_manifest_bounds(tmp_path: Path):
    class FakeCalendar:
        def schedule(self, *, start_date, end_date):
            assert start_date == "2026-09-01"
            assert end_date == "2026-09-21"
            return SimpleNamespace(index=[datetime(2026, 9, 1), datetime(2026, 9, 21)])

    root = tmp_path
    rows = [{
        "symbol": "QQQ", "expiration": "2026-10-16", "strike": "500", "right": "CALL",
        "created": "2026-09-21T19:00:00-0400", "volume": "1",
    }]
    path = root / "raw/QQQ/monthly_eod_dte_0_60/month=2026-09.csv.gz"
    record = _record(root, request_id="partial", dataset="monthly_eod_dte_0_60", path=path, rows=rows)
    record["archive_month"] = "2026-09"
    record["coverage_start_date"] = "2026-09-01"
    record["coverage_end_date"] = "2026-09-21"
    (root / "coverage_manifest.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

    result = audit_archive(root, expected_monthly=1, expected_episode=0, calendar=FakeCalendar(), calendar_name="fake")
    coverage = result["monthly_coverage"][0]["eod_created"]["calendar"]
    assert coverage["expected_session_count"] == 2
