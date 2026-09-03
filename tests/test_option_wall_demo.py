import asyncio
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
