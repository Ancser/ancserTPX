from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from backend.live import databento_orderflow as mbo


def _record(iso: str, side: str, price: float, size: int):
    return SimpleNamespace(
        ts_event=int(datetime.fromisoformat(iso).timestamp() * mbo.NANO),
        action="T",
        side=side,
        price=int(price * 1_000_000_000),
        size=size,
        order_id=0,
        flags=0,
    )


class _FakeFeed:
    def __init__(self, *, contract_id, symbol, tick_size):
        self.contract_id = contract_id
        self.raw_symbol = mbo.databento_raw_symbol(contract_id)
        self.symbol = symbol
        self.tick_size = tick_size
        self.start_calls = 0
        self.stop_calls = 0

    def start(self):
        self.start_calls += 1
        return True

    def stop(self):
        self.stop_calls += 1

    def status(self):
        return {
            "state": "connected",
            "connected": True,
            "latency_ms": 12.5,
        }


def test_record_only_feed_is_opt_in_and_is_shared(monkeypatch):
    monkeypatch.delenv("ANCSERTPX_AUTO_DATABENTO_MBO", raising=False)
    monkeypatch.setenv("DATABENTO_API_KEY", "test-key")
    monkeypatch.setattr(mbo, "_process_mbo_feed", None)
    assert mbo.start_databento_mbo_recorder("CON.F.US.MNQ.U26") is False
    assert mbo.databento_mbo_recorder_status() is None

    monkeypatch.setenv("ANCSERTPX_AUTO_DATABENTO_MBO", "true")
    monkeypatch.setattr(mbo, "DatabentoMboLiveFeed", _FakeFeed)

    assert mbo.start_databento_mbo_recorder("CON.F.US.MNQ.U26") is True
    first = mbo.databento_mbo_recorder_status()
    borrowed = mbo.acquire_databento_mbo_feed("CON.F.US.MNQ.U26")

    assert borrowed is not None
    assert first["record_only"] is True
    assert first["owner"] == "app"
    assert first["contract_id"] == "CON.F.US.MNQ.U26"
    assert borrowed is mbo._process_mbo_feed
    assert borrowed.start_calls == 2  # start() itself is idempotent in the real feed

    mbo.stop_databento_mbo_recorder()
    assert borrowed.stop_calls == 1
    assert mbo.databento_mbo_recorder_status() is None


def test_live_recorder_persists_non_rth_mbo_in_all_session_cache(tmp_path):
    feed = mbo.DatabentoMboLiveFeed(
        contract_id="CON.F.US.MNQ.Z26",
        api_key="test-key",
        root=tmp_path,
    )
    feed._snapshot_flag = 0
    feed._on_record(_record("2026-09-14T00:00:01+00:00", "B", 20000, 3))
    feed._on_record(_record("2026-09-14T13:30:01+00:00", "A", 20000.25, 2))
    feed.stop()

    path = tmp_path / "runtime" / "state" / (
        "databento_live_all_sessions_mnq_2026-09-14.json.gz"
    )
    payload = mbo.read_footprint_cache(path)
    assert payload["meta"]["session"] == "ALL"
    assert [row["session"] for row in payload["bars"]] == ["ASIA", "RTH"]
    assert sum(row["trades"] for row in payload["bars"]) == 2
    assert feed.status()["session_storage"] == "ALL"
