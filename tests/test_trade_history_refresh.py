from __future__ import annotations

import asyncio
import json
import os
import time

from backend.api import routes


ACCOUNT_ID = 22373660
CONTRACT_ID = "CON.F.US.MNQ.U26"


class _TradeHistoryClient:
    def __init__(self) -> None:
        self.history_calls = 0

    async def get_accounts(self):
        return [{"id": ACCOUNT_ID, "name": "MAIN", "canTrade": True}]

    async def get_trade_history(self, account_id: int):
        assert account_id == ACCOUNT_ID
        self.history_calls += 1
        return [
            {
                "id": 3010999438,
                "accountId": ACCOUNT_ID,
                "contractId": CONTRACT_ID,
                "side": 0,
                "price": 29216.0,
                "size": 1,
                "profitAndLoss": 0,
                "creationTimestamp": "2026-08-20T18:11:03.104848+00:00",
            },
            {
                "id": 3011352573,
                "accountId": ACCOUNT_ID,
                "contractId": CONTRACT_ID,
                "side": 1,
                "price": 29308.75,
                "size": 1,
                "profitAndLoss": 185.5,
                "creationTimestamp": "2026-08-20T19:45:04.445174+00:00",
            },
        ]


def _cached_trade(trade_id: str = "stale") -> dict:
    return {
        "trade_id": trade_id,
        "account_id": ACCOUNT_ID,
        "contract_id": CONTRACT_ID,
        "direction": "buy",
        "size": 1,
        "entry_price": 29000.0,
        "exit_price": 29001.0,
        "entry_time": "2026-08-11T17:00:00+00:00",
        "exit_time": "2026-08-11T17:01:00+00:00",
        "gross_pnl": 2.0,
        "pnl": 0.76,
        "pnl_is_net": True,
        "commission": 0.5,
        "fees": 0.74,
        "exit_reason": "tp",
        "source": "topstep",
    }


def _install_cache(monkeypatch, tmp_path, *, age_seconds: float) -> None:
    cache = tmp_path / "trade_history.json"
    cache.write_text(json.dumps([_cached_trade()]), encoding="utf-8")
    stamp = time.time() - age_seconds
    os.utime(cache, (stamp, stamp))
    monkeypatch.setattr(routes, "_TRADE_HISTORY_FILE", str(cache))
    monkeypatch.setattr(routes, "_LIVE_EXITS_FILE", str(tmp_path / "live_exits.json"))
    monkeypatch.setattr(routes, "_account_name_cache", {})


def test_fresh_trade_history_cache_avoids_broker_request(monkeypatch, tmp_path):
    client = _TradeHistoryClient()
    _install_cache(monkeypatch, tmp_path, age_seconds=0)
    monkeypatch.setattr(routes, "_topstepx_client", client)

    result = asyncio.run(routes.live_trade_history(refresh=False, account_id=ACCOUNT_ID))

    assert result["source"] == "cache"
    assert result["trades"][0]["trade_id"] == "stale"
    assert client.history_calls == 0


def test_stale_trade_history_cache_refreshes_from_broker(monkeypatch, tmp_path):
    client = _TradeHistoryClient()
    _install_cache(monkeypatch, tmp_path, age_seconds=3600)
    monkeypatch.setattr(routes, "_topstepx_client", client)

    result = asyncio.run(routes.live_trade_history(refresh=False, account_id=ACCOUNT_ID))

    assert result["source"] == "api_stale_refresh"
    assert result["count"] == 1
    assert result["trades"][0]["trade_id"] == "3010999438_3011352573"
    assert result["trades"][0]["entry_time"] == "2026-08-20T18:11:03.104848+00:00"
    assert client.history_calls == 1

