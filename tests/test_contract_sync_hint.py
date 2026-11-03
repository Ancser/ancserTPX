"""Chart contract-sync hint (DATA-017).

The store keeps the calendar roll; the chart's live feed and live orders follow
TopstepX's active flag.  /connection/status reports when they disagree, asking
Topstep at most once per TTL in the background.
"""
from __future__ import annotations

import asyncio
import pytest

from backend.api import routes
from backend.broker.topstepx import TopstepXClient

Z26 = "CON.F.US.MNQ.Z26"
H27 = "CON.F.US.MNQ.H27"
U26 = "CON.F.US.MNQ.U26"


def _client(contracts):
    client = TopstepXClient.__new__(TopstepXClient)
    calls = []

    async def search(text):
        calls.append(text)
        return contracts

    async def resolve(value):
        return f"literal:{value}"

    client.search_contracts = search
    client._resolve_contract_id = resolve
    client.token = "token"
    client.search_calls = calls
    return client


def _listed(active_ids):
    return [{"id": cid, "activeContract": cid in active_ids} for cid in (Z26, H27)]


# ── broker client ──────────────────────────────────────────────────────────

def test_active_contract_is_topsteps_own_flag_or_none():
    assert asyncio.run(_client(_listed({H27})).get_active_contract_id("MNQ")) == H27
    assert asyncio.run(_client(_listed(set())).get_active_contract_id("MNQ")) is None
    assert asyncio.run(_client(_listed({Z26, H27})).get_active_contract_id("MNQ")) is None


def test_front_month_still_prefers_the_flag_then_the_calendar():
    assert asyncio.run(_client(_listed({H27})).get_front_month_contract_id("MNQ")) == H27
    # No flag: nearest unexpired quarter by the calendar rule (a real contract).
    assert asyncio.run(_client(_listed(set())).get_front_month_contract_id("MNQ")) in (Z26, H27)


# ── status hint ────────────────────────────────────────────────────────────

@pytest.fixture
def sync_env(monkeypatch):
    monkeypatch.setattr(routes, "current_quarterly_contract_id", lambda symbol="MNQ": Z26)
    monkeypatch.setattr(routes, "_contract_sync_state", {
        "at": 0.0, "for": None, "active": None, "error": None, "task": None})
    monkeypatch.setattr(routes, "_topstepx_client", None)
    monkeypatch.setattr(routes, "_live_contract_id", Z26)
    return monkeypatch


def test_in_sync_shows_nothing(sync_env):
    status = asyncio.run(_status())
    assert status["in_sync"] is True
    assert status["detail"] == ""
    assert status["codes"] == {"chart": "Z26", "calendar": "Z26", "topstep": None}


async def _status():
    return routes._contract_sync_status()


def test_chart_on_a_different_contract_than_the_store_is_flagged(sync_env):
    sync_env.setattr(routes, "_live_contract_id", U26)
    status = asyncio.run(_status())
    assert status["in_sync"] is False
    assert "U26" in status["detail"] and "Z26" in status["detail"]


def test_topstep_flag_is_fetched_in_the_background_once_per_ttl(sync_env):
    client = _client(_listed({H27}))
    sync_env.setattr(routes, "_topstepx_client", client)

    async def run():
        first = routes._contract_sync_status()           # schedules the refresh
        assert first["topstep_active"] is None
        await routes._contract_sync_state["task"]
        second = routes._contract_sync_status()
        third = routes._contract_sync_status()           # within TTL: no new call
        return second, third

    second, third = asyncio.run(run())
    assert client.search_calls == ["MNQ"]
    assert second["topstep_active"] == H27
    assert second["in_sync"] is False
    assert "reconnect" in second["detail"]
    assert third["in_sync"] is False


def test_a_broker_error_never_breaks_the_status(sync_env):
    client = _client([])

    async def boom(_text):
        raise RuntimeError("network")

    client.search_contracts = boom
    sync_env.setattr(routes, "_topstepx_client", client)

    async def run():
        routes._contract_sync_status()
        await routes._contract_sync_state["task"]
        return routes._contract_sync_status()

    status = asyncio.run(run())
    assert status["in_sync"] is True                    # chart == calendar, Topstep unknown
    assert routes._contract_sync_state["error"] == "RuntimeError"


def test_connection_status_carries_the_hint(sync_env):
    body = asyncio.run(routes.connection_status())
    assert body["contract"]["in_sync"] is True
    assert set(body["contract"]) >= {"chart", "calendar", "topstep_active", "in_sync", "detail"}
