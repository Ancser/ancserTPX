import asyncio
import csv
import gzip
import json

import pytest
from fastapi import HTTPException

from backend.api import routes
from backend.data import option_wall_demo


def _write_demo(root, date="2026-09-01"):
    target = root / date / "derived.json"
    target.parent.mkdir(parents=True)
    payload = {
        "available": True,
        "symbol": "MNQ",
        "date": date,
        "snapshots": [{"as_of": f"{date}T14:00:00Z", "call_wall_mnq": 29150.0}],
    }
    target.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _write_hourly(path):
    columns = [
        "date", "as_of", "as_of_et", "qqq_spot", "mnq_entry",
        "oi_call_wall_bps", "oi_put_wall_bps", "oi_gamma_flip_bps",
        "dashboard_vol_call_wall_bps", "dashboard_vol_put_wall_bps",
        "quality_valid_contracts",
    ]
    rows = [
        {
            "date": "2026-07-29", "as_of": "2026-07-29T13:35:00Z", "as_of_et": "09:35",
            "qqq_spot": 675, "mnq_entry": 28000, "oi_call_wall_bps": 100,
            "oi_put_wall_bps": -100, "oi_gamma_flip_bps": 10,
            "dashboard_vol_call_wall_bps": "", "dashboard_vol_put_wall_bps": "",
            "quality_valid_contracts": 250,
        },
        {
            "date": "2026-07-29", "as_of": "2026-07-29T14:00:00Z", "as_of_et": "10:00",
            "qqq_spot": 670, "mnq_entry": 27750, "oi_call_wall_bps": 200,
            "oi_put_wall_bps": -50, "oi_gamma_flip_bps": 140,
            "dashboard_vol_call_wall_bps": 70, "dashboard_vol_put_wall_bps": -20,
            "quality_valid_contracts": 240,
        },
    ]
    with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def test_option_wall_loader_uses_requested_or_latest_local_demo(tmp_path):
    older = _write_demo(tmp_path, "2026-08-28")
    latest = _write_demo(tmp_path, "2026-09-01")

    assert option_wall_demo.load_option_wall_demo("2026-08-28", tmp_path) == older
    aggregate = option_wall_demo.load_option_wall_demo(root=tmp_path)
    assert aggregate["dates"] == ["2026-08-28", "2026-09-01"]
    assert aggregate["date"] == "2026-08-28..2026-09-01"
    assert [row["as_of"] for row in aggregate["snapshots"]] == [
        "2026-08-28T14:00:00Z", "2026-09-01T14:00:00Z",
    ]
    assert aggregate["coverage"]["sessions"] == 2
    assert option_wall_demo.load_option_wall_demo("2026-08-29", tmp_path) is None


def test_option_wall_loader_rejects_path_like_dates(tmp_path):
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        option_wall_demo.load_option_wall_demo("../2026-09-01", tmp_path)


def test_option_wall_loader_adds_explicit_hourly_fallback_without_overstating_resolution(tmp_path):
    hourly = tmp_path / "features.csv.gz"
    _write_hourly(hourly)

    payload = option_wall_demo.load_option_wall_demo(
        "2026-07-29", tmp_path / "demos", hourly,
    )

    assert payload["coverage"]["sessions"] == 1
    assert payload["coverage_by_resolution"]["hourly"] == ["2026-07-29"]
    assert len(payload["snapshots"]) == 2
    opening, hourly_row = payload["snapshots"]
    assert opening["wall_source"] == "oi"
    assert opening["call_wall_mnq"] == pytest.approx(28280.0)
    assert hourly_row["wall_source"] == "hourly_volume"
    assert hourly_row["call_wall_mnq"] == pytest.approx(27944.25)
    assert hourly_row["gamma_flip_mnq"] is None
    assert hourly_row["gamma_flip_quality"] == "remote_unstable"
    assert hourly_row["cadence_seconds"] == 3600


def test_option_wall_loader_prefers_five_minute_day_over_hourly_duplicate(tmp_path):
    detailed = _write_demo(tmp_path, "2026-07-29")
    hourly = tmp_path / "features.csv.gz"
    _write_hourly(hourly)

    payload = option_wall_demo.load_option_wall_demo(root=tmp_path, hourly_path=hourly)

    assert payload["dates"] == ["2026-07-29"]
    assert payload["snapshots"] == detailed["snapshots"]
    assert payload["coverage_by_resolution"]["five_minute"] == ["2026-07-29"]
    assert payload["coverage_by_resolution"]["hourly"] == []


def test_option_wall_route_is_read_only_and_mnq_scoped(tmp_path, monkeypatch):
    payload = _write_demo(tmp_path)
    monkeypatch.setattr(option_wall_demo, "DEFAULT_ROOT", tmp_path)

    assert asyncio.run(routes.options_wall_demo(date="2026-09-01", symbol="MNQ")) == payload
    unavailable = asyncio.run(routes.options_wall_demo(date="2026-09-01", symbol="MES"))
    assert unavailable["available"] is False
    assert "MNQ" in unavailable["reason"]

    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes.options_wall_demo(date="not-a-date", symbol="MNQ"))
    assert exc.value.status_code == 400
