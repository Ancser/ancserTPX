from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
from pathlib import Path

from scripts import theta_data_ingest as ingest
from scripts import theta_pi_cohort_ingest as cohort_ingest


def _write_eod(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=["symbol", "expiration", "created"], lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    path.write_bytes(gzip.compress(stream.getvalue().encode("utf-8"), mtime=0))


def _fixture_root(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "thetadata"
    _write_eod(
        root / "raw/QQQ/monthly_eod_dte_0_60/month=2026-03.csv.gz",
        [
            {"symbol": "QQQ", "expiration": "2026-03-06", "created": "2026-03-05T17:19:00-0500"},
            {"symbol": "QQQ", "expiration": "2026-03-06", "created": "2026-03-06T17:19:00-0500"},
        ],
    )
    _write_eod(
        root / "raw/SPY/monthly_eod_dte_0_60/month=2026-03.csv.gz",
        [
            {"symbol": "SPY", "expiration": "2026-03-10", "created": "2026-03-09T17:19:00-0500"},
            {"symbol": "SPY", "expiration": "2026-03-10", "created": "2026-03-10T17:19:00-0500"},
        ],
    )
    pi_path = tmp_path / "pi_signals.json"
    pi_path.write_text(json.dumps([
        {
            "id": "qqq-1",
            "ts": "2026-03-05T17:43:00+00:00",
            "symbol": "QQQ",
            "marks": [{"kind": "淡蓝圈", "level": 1, "size": "Level 1", "count": 1}],
        },
        {
            "id": "spy-1",
            "ts": "2026-03-09T16:48:00+00:00",
            "symbol": "SPY",
            "marks": [{"kind": "紫圈", "level": 1, "size": "Level 1", "count": 1}],
        },
    ], ensure_ascii=False), encoding="utf-8")
    return root, pi_path


def test_build_cohorts_preserves_mark_provenance_and_exact_next_expiry(tmp_path: Path):
    root, pi_path = _fixture_root(tmp_path)

    cohorts = cohort_ingest.build_cohorts(
        root=root, pi_path=pi_path, expected_total=None, expected_by_symbol=None,
    )

    assert [(item["symbol"], item["event_date"].isoformat()) for item in cohorts] == [
        ("QQQ", "2026-03-05"), ("SPY", "2026-03-09"),
    ]
    qqq, spy = cohorts
    assert qqq["expiration"] == qqq["exit_date"]
    assert qqq["expiration"].isoformat() == "2026-03-06"
    assert qqq["marks"][0]["kind"] == "淡蓝圈"
    assert qqq["marks"][0]["option_right"] == "CALL"
    assert qqq["marks"][0]["source_timestamp_utc"] == "2026-03-05T17:43:00+00:00"
    assert spy["marks"][0]["option_right"] == "PUT"
    assert spy["expiration"].isoformat() == "2026-03-10"


def test_cohort_requests_reuse_existing_episode_ids_and_paths(tmp_path: Path):
    root, pi_path = _fixture_root(tmp_path)
    cohorts = cohort_ingest.build_cohorts(
        root=root, pi_path=pi_path, expected_total=None, expected_by_symbol=None,
    )
    cohort = cohorts[0]
    selected = cohort_ingest.cohort_requests(cohort)
    episode = ingest.episode_requests(
        symbol="QQQ", event_date=cohort["event_date"], exit_date=cohort["exit_date"],
        expiration=cohort["expiration"], oi_effective_session=cohort["event_date"].fromordinal(cohort["event_date"].toordinal() - 1),
    )

    assert len(selected) == 4
    assert [ingest.request_id(item) for item in selected] == [
        ingest.request_id(item) for item in episode[2:]
    ]
    assert [str(item["relative_path"]) for item in selected] == [
        str(item["relative_path"]) for item in episode[2:]
    ]
    assert all(item["params"]["right"] == "both" for item in selected)


def test_plan_requires_matching_manifest_hash_before_cache_skip(tmp_path: Path):
    root, pi_path = _fixture_root(tmp_path)
    cohorts = cohort_ingest.build_cohorts(
        root=root, pi_path=pi_path, expected_total=None, expected_by_symbol=None,
    )
    request = cohort_ingest.cohort_requests(cohorts[0])[0]
    path = root / request["relative_path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = gzip.compress(b"timestamp,expiration,strike,right\n", mtime=0)
    path.write_bytes(payload)
    record = {
        "request_id": ingest.request_id(request),
        "status": "downloaded",
        "sha256_gzip": hashlib.sha256(payload).hexdigest(),
        "rows": 0,
        "contracts": 0,
        "response_path": str(request["relative_path"]),
    }
    (root / "coverage_manifest.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

    first = cohort_ingest.build_request_manifest(cohorts[:1], root=root)
    first_item = next(item for item in first["requests"] if item["request_id"] == record["request_id"])
    assert first_item["cache_status"] == "cached"
    assert first["actual_selected_pilot"] is None

    path.write_bytes(gzip.compress(b"different\n", mtime=0))
    second = cohort_ingest.build_request_manifest(cohorts[:1], root=root)
    second_item = next(item for item in second["requests"] if item["request_id"] == record["request_id"])
    assert second_item["cache_status"] == "orphaned_existing_file"


def test_verify_cohort_requires_both_datasets_on_both_days(tmp_path: Path):
    root, pi_path = _fixture_root(tmp_path)
    cohort = cohort_ingest.build_cohorts(
        root=root, pi_path=pi_path, expected_total=None, expected_by_symbol=None,
    )[0]
    requests = cohort_ingest.cohort_requests(cohort)
    result = {
        "requests": [
            {"request_id": ingest.request_id(request), "key": request["key"], "status": "cached", "rows": 1, "contracts": 1}
            for request in requests
        ]
    }
    verification = cohort_ingest.verify_cohort_coverage(cohort, result)
    assert verification["complete_quote_and_first_order_both_days"] is True
    assert verification["missing_or_invalid"] == []
