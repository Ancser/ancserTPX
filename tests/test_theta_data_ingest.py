from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path

from scripts import theta_data_ingest as ingest


class _Series:
    def __init__(self, values):
        self.values = values

    def min(self):
        return min(self.values) if self.values else None

    def max(self):
        return max(self.values) if self.values else None


class _Frame:
    def __init__(self, rows, columns=None):
        self.rows = rows
        self.columns = columns or list(rows[0]) if rows else columns or []
        self.height = len(rows)

    def write_csv(self):
        import csv
        import io

        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=self.columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(self.rows)
        return stream.getvalue()

    def get_column(self, column):
        return _Series([row[column] for row in self.rows if row.get(column) is not None])

    def select(self, columns):
        return _Frame([{key: row[key] for key in columns} for row in self.rows], columns)

    def unique(self):
        seen = set()
        rows = []
        for row in self.rows:
            item = tuple(row[key] for key in self.columns)
            if item not in seen:
                seen.add(item)
                rows.append(row)
        return _Frame(rows, self.columns)


class _FakeClient:
    def __init__(self):
        self.calls = []

    def _response(self, method, **params):
        self.calls.append((method, params))
        return _Frame([
            {
                "timestamp": "2026-03-30T15:02:00-04:00",
                "expiration": "2026-03-31",
                "strike": 558.0,
                "right": "CALL",
                "bid_size": 7,
                "ask_size": 9,
                "implied_vol": 0.29,
                "underlying_timestamp": "2026-03-30T15:02:00-04:00",
                "underlying_price": 557.89,
            },
            {
                "timestamp": "2026-03-30T15:03:00-04:00",
                "expiration": "2026-03-31",
                "strike": 558.0,
                "right": "CALL",
                "bid_size": 5,
                "ask_size": 8,
                "implied_vol": 0.30,
                "underlying_timestamp": "2026-03-30T15:03:00-04:00",
                "underlying_price": 558.01,
            },
        ])

    def option_history_eod(self, **params):
        return self._response("eod", **params)

    def option_history_open_interest(self, **params):
        return self._response("open_interest", **params)

    def option_history_quote(self, **params):
        return self._response("quote", **params)

    def option_history_greeks_first_order(self, **params):
        return self._response("first_order", **params)


def test_pilot_plan_is_bounded_to_two_dates_and_partial_0_60_dte_chain():
    requests = ingest.pilot_requests()

    assert len(requests) == 6
    eod, oi, *intraday = requests
    assert eod["params"]["max_dte"] == 60
    assert eod["scope"] == "partial_chain_calendar_dte_0_60"
    assert oi["params"]["date"] == ingest.PILOT_EVENT_DATE
    assert oi["effective_session"] == ingest.PILOT_OI_EFFECTIVE_SESSION
    assert oi["params"]["expiration"] == "*"
    assert {r["params"]["date"] for r in intraday} == {
        ingest.PILOT_EVENT_DATE,
        ingest.PILOT_EXIT_DATE,
    }
    assert {r["method"] for r in intraday} == {
        "option_history_quote",
        "option_history_greeks_first_order",
    }
    assert all(r["params"]["interval"] == "1m" for r in intraday)
    assert all(r["params"]["strike"] == "*" and r["params"]["right"] == "both" for r in intraday)
    assert not any("second_order" in r["method"] for r in requests)


def test_explicit_episode_and_daily_snapshot_build_exact_dates_and_paths():
    selected = ingest.selected_expiry_requests(
        symbol="qqq",
        event_date=ingest.date(2026, 6, 5),
        exit_date=ingest.date(2026, 6, 8),
        expiration=ingest.date(2026, 6, 8),
    )
    assert len(selected) == 4
    assert [item["method"] for item in selected] == [
        "option_history_quote", "option_history_greeks_first_order",
        "option_history_quote", "option_history_greeks_first_order",
    ]
    assert all(item["scope"] == "selected_expiry_all_strikes_1m" for item in selected)

    episode = ingest.episode_requests(
        symbol="qqq",
        event_date=ingest.date(2026, 6, 5),
        exit_date=ingest.date(2026, 6, 8),
        expiration=ingest.date(2026, 6, 8),
        oi_effective_session=ingest.date(2026, 6, 4),
    )
    assert len(episode) == 6
    assert all(item["symbol"] == "QQQ" for item in episode)
    assert {item["params"].get("date") for item in episode if item["method"] == "option_history_quote"} == {
        ingest.date(2026, 6, 5), ingest.date(2026, 6, 8)
    }
    assert all("QQQ" in item["relative_path"].parts for item in episode)
    assert next(item for item in episode if item["method"] == "option_history_open_interest")["effective_session"] == ingest.date(2026, 6, 4)
    assert [ingest.request_id(item) for item in episode[2:]] == [ingest.request_id(item) for item in selected]

    daily = ingest.daily_snapshot_requests(
        symbol="SPY", snapshot_date=ingest.date(2021, 1, 4),
        oi_effective_session=ingest.date(2020, 12, 31), max_dte=60,
    )
    assert [item["method"] for item in daily] == ["option_history_eod", "option_history_open_interest"]
    assert all(item["params"]["max_dte"] == 60 for item in daily)
    assert all("SPY" in item["relative_path"].parts for item in daily)


def test_monthly_archive_plan_is_four_requests_per_month_with_distinct_paths():
    pilot = ingest.monthly_pilot_requests()
    assert len(pilot) == 4
    assert {request["symbol"] for request in pilot} == {"QQQ", "SPY"}
    assert {request["method"] for request in pilot} == {
        "option_history_eod", "option_history_open_interest"
    }
    assert all(request["archive_month"] == "2021-01" for request in pilot)
    assert all("month=2021-01.csv.gz" in str(request["relative_path"]) for request in pilot)
    assert all("date=" not in str(request["relative_path"]) for request in pilot)
    oi = next(request for request in pilot if request["method"] == "option_history_open_interest")
    assert oi["effective_session"] is None
    assert oi["oi_effective_session_status"] == "unknown_until_calendar_join"

    full = ingest.monthly_archive_requests(
        start_date=ingest.date(2021, 1, 1), end_date=ingest.date(2026, 9, 21),
    )
    assert len(full) == 69 * 2 * 2
    assert len({str(request["relative_path"]) for request in full}) == len(full)
    assert next(request for request in full if request["archive_month"] == "2026-09")["coverage_end"] == ingest.date(2026, 9, 21)


def test_monthly_archive_saves_unknown_oi_provenance_and_resumes(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ingest, "check_capacity", lambda _root, _incoming=0: None)
    progress = tmp_path / "monthly-progress.jsonl"
    client = _FakeClient()
    result = ingest.run_monthly_archive(
        start_date=ingest.date(2021, 1, 1), end_date=ingest.date(2021, 1, 31),
        symbols=("QQQ", "SPY"), root=tmp_path, client=client,
        sleep=lambda _: None, request_gap_seconds=0,
        progress_log=progress,
    )
    assert result["status"] == "complete"
    assert result["planned_requests"] == 4
    assert result["status_counts"] == {"downloaded": 4}
    assert len(client.calls) == 4
    records = [json.loads(line) for line in (tmp_path / "coverage_manifest.jsonl").read_text(encoding="utf-8").splitlines()]
    monthly_oi = next(record for record in records if record["dataset"] == "monthly_open_interest_dte_0_60")
    assert monthly_oi["oi_provenance"]["effective_session"] == "unknown_until_calendar_join"
    assert monthly_oi["oi_provenance"]["timestamp_shift_applied"] is False
    assert monthly_oi["coverage_start_date"] == "2021-01-01"
    assert monthly_oi["coverage_end_date"] == "2021-01-31"
    progress_events = [json.loads(line)["event"] for line in progress.read_text(encoding="utf-8").splitlines()]
    assert progress_events == ["run_started", "request_complete", "request_complete", "request_complete", "request_complete", "run_summary"]

    resumed = ingest.run_monthly_archive(
        start_date=ingest.date(2021, 1, 1), end_date=ingest.date(2021, 1, 31),
        symbols=("QQQ", "SPY"), root=tmp_path, client=client,
        sleep=lambda _: None, request_gap_seconds=0,
        progress_log=progress,
    )
    assert resumed["status"] == "complete"
    assert resumed["status_counts"] == {"cached": 4}
    assert len(client.calls) == 4


def test_generic_episode_records_do_not_receive_pilot_event_times(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ingest, "check_capacity", lambda _root, _incoming=0: None)
    ingest.run_episode(
        symbol="QQQ", event_date=ingest.date(2026, 6, 5),
        exit_date=ingest.date(2026, 6, 8), expiration=ingest.date(2026, 6, 8),
        oi_effective_session=ingest.date(2026, 6, 4), root=tmp_path,
        client=_FakeClient(), sleep=lambda _: None, request_gap_seconds=0,
    )
    records = [json.loads(line) for line in (tmp_path / "coverage_manifest.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all("event_context" not in record for record in records if record["sdk_method"] == "option_history_quote")


def test_monthly_run_stops_after_authentication_failure(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ingest, "check_capacity", lambda _root, _incoming=0: None)

    class AuthenticationError(Exception):
        pass

    AuthenticationError.__module__ = "thetadata.errors"

    class FailingClient(_FakeClient):
        def option_history_eod(self, **params):
            self.calls.append(("eod", params))
            raise AuthenticationError()

    result = ingest.run_monthly_archive(
        start_date=ingest.date(2021, 1, 1), end_date=ingest.date(2021, 2, 28),
        root=tmp_path, client=FailingClient(), sleep=lambda _: None, request_gap_seconds=0,
    )
    assert result["status"] == "partial"
    assert result["attempted_or_cached_requests"] == 1
    assert result["status_counts"] == {"forbidden": 1}


def test_episode_reports_selected_expiry_quote_coverage_on_both_days(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ingest, "check_capacity", lambda _root, _incoming=0: None)
    result = ingest.run_episode(
        symbol="QQQ", event_date=ingest.date(2026, 6, 5),
        exit_date=ingest.date(2026, 6, 8), expiration=ingest.date(2026, 6, 8),
        oi_effective_session=ingest.date(2026, 6, 4), root=tmp_path,
        client=_FakeClient(), sleep=lambda _: None, request_gap_seconds=0,
    )
    assert result["status"] == "complete"
    assert result["episode"]["selected_expiry_available_with_quotes_on_both_days"] is True
    assert set(result["episode"]["selected_expiry_quotes_by_date"]) == {"2026-06-05", "2026-06-08"}


def test_pilot_saves_compressed_immutable_rows_and_resumes_from_manifest(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ingest, "check_capacity", lambda _root, _incoming=0: None)
    client = _FakeClient()
    result = ingest.run_pilot(root=tmp_path, client=client, sleep=lambda _: None, request_gap_seconds=0)

    assert result["status"] == "complete"
    assert result["planned_requests"] == 6
    assert result["status_counts"] == {"downloaded": 6}
    assert result["rows"] == 12
    assert result["contracts"] == 6
    assert len(client.calls) == 6
    manifest_lines = (tmp_path / "coverage_manifest.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(manifest_lines) == 6
    records = [json.loads(line) for line in manifest_lines]
    oi_record = next(record for record in records if record["dataset"] == "open_interest_dte_0_60")
    assert oi_record["oi_provenance"]["effective_session"] == "2026-03-27"
    assert oi_record["oi_provenance"]["timestamp_shift_applied"] is False
    quote_record = next(record for record in records if record["dataset"] == "quote_1m")
    compressed_path = tmp_path / quote_record["response_path"]
    assert gzip.decompress(compressed_path.read_bytes()).decode("utf-8").startswith("timestamp,expiration,strike,right,")
    assert quote_record["sha256_gzip"] == ingest._file_sha256(compressed_path)
    assert quote_record["sha256_raw"]

    resumed = ingest.run_pilot(root=tmp_path, client=client, sleep=lambda _: None, request_gap_seconds=0)
    assert resumed["status"] == "complete"
    assert resumed["status_counts"] == {"cached": 6}
    assert len(client.calls) == 6
    assert len((tmp_path / "coverage_manifest.jsonl").read_text(encoding="utf-8").splitlines()) == 6


def test_client_initialization_runs_with_sdk_logging_disabled_and_restores_state(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "check_capacity", lambda _root, _incoming=0: None)
    client = _FakeClient()
    observed = []

    def create_client():
        observed.append(logging.root.manager.disable)
        return client

    monkeypatch.setattr(ingest, "_create_client", create_client)
    old_disable = logging.root.manager.disable
    result = ingest.run_pilot(root=tmp_path, sleep=lambda _: None, request_gap_seconds=0)

    assert result["status"] == "complete"
    assert observed == [logging.CRITICAL]
    assert logging.root.manager.disable == old_disable


def test_capacity_guard_preserves_tree_budget_and_free_space_reserve():
    assert ingest.capacity_error(current_tree_bytes=100, free_bytes=1000, incoming_bytes=50, max_tree_bytes=149, min_free_bytes=100) == "tree_budget_exceeded"
    assert ingest.capacity_error(current_tree_bytes=100, free_bytes=149, incoming_bytes=50, max_tree_bytes=1000, min_free_bytes=100) == "free_space_reserve_would_be_breached"
    assert ingest.capacity_error(current_tree_bytes=100, free_bytes=1000, incoming_bytes=50, max_tree_bytes=1000, min_free_bytes=100) is None


def test_retry_and_permission_classification_are_bounded():
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise TimeoutError("redacted")
        return "ok"

    value, status, code, count = ingest.call_with_retries(flaky, sleep=lambda _: None)
    assert (value, status, code, count) == ("ok", "ok", None, 3)
    assert len(attempts) == 3

    class PermissionFailure(Exception):
        status_code = 403

    value, status, code, count = ingest.call_with_retries(
        lambda: (_ for _ in ()).throw(PermissionFailure()), sleep=lambda _: None
    )
    assert value is None
    assert status == "forbidden"
    assert code == "403"
    assert count == 1

    class NoDataFoundError(Exception):
        pass

    NoDataFoundError.__module__ = "thetadata.errors"
    value, status, code, count = ingest.call_with_retries(
        lambda: (_ for _ in ()).throw(NoDataFoundError()), sleep=lambda _: None
    )
    assert value is None
    assert status == "missing"
    assert code == "NO_DATA_FOUND"
    assert count == 1


def test_lock_preserves_errors_raised_during_the_protected_body(tmp_path: Path):
    try:
        with ingest._exclusive_run_lock(tmp_path):
            raise OSError("body failure")
    except OSError as exc:
        assert str(exc) == "body failure"
    else:
        raise AssertionError("body exception should propagate unchanged")
