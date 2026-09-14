"""Tests for the non-sensitive provider status contract used by the UI rail."""
from __future__ import annotations

import asyncio

from backend.api import routes
from backend.live import pi_recorder


def _status():
    return asyncio.run(routes.connection_status())


def _clear_provider_env(monkeypatch):
    for name in (
        "TOPSTEPX_USERNAME",
        "TOPSTEPX_API_KEY",
        "DISCORD_TOKEN",
        "EMAPMO_DISCORD_WEBHOOK_URL",
        "DATABENTO_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def test_connection_status_reports_empty_without_provider_input(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setattr(routes, "_topstepx_client", None)
    monkeypatch.setattr(routes, "_live_engines", {})
    monkeypatch.setattr(pi_recorder, "pi_recorder_health", lambda: {})

    providers = _status()["providers"]
    assert {name: item["state"] for name, item in providers.items()} == {
        "discord": "empty",
        "topstep": "empty",
        "databento": "empty",
    }
    assert all(item["latency_ms"] is None for item in providers.values())


def test_connection_status_maps_real_client_and_feed_states(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("TOPSTEPX_USERNAME", "ancser")
    monkeypatch.setenv("TOPSTEPX_API_KEY", "topstep-test-key")
    monkeypatch.setenv("DISCORD_TOKEN", "discord-test-token")
    monkeypatch.setenv("DATABENTO_API_KEY", "databento-test-key")

    class Client:
        token = "jwt-present"
        _last_request_latency_ms = 37.4

    class Engine:
        def get_status(self):
            return {
                "strategy_mode": "delta_absorption",
                "running": True,
                "databento_mbo": {
                    "state": "starting",
                    "latency_ms": 81.2,
                    "error": None,
                },
            }

    monkeypatch.setattr(routes, "_topstepx_client", Client())
    monkeypatch.setattr(routes, "_live_engines", {1: Engine()})
    monkeypatch.setattr(
        pi_recorder,
        "pi_recorder_health",
        lambda: {
            "task_alive": True,
            "last_success_age_seconds": 0.4,
            "consecutive_errors": 0,
            "last_fetch_latency_ms": 24.6,
        },
    )

    providers = _status()["providers"]
    assert providers["topstep"] == {
        "state": "connected",
        "configured": True,
        "connected": True,
        "latency_ms": 37.4,
        "detail": "Topstep REST client connected",
    }
    assert providers["discord"]["state"] == "connected"
    assert providers["discord"]["latency_ms"] == 24.6
    assert providers["databento"]["state"] == "starting"
    assert providers["databento"]["latency_ms"] == 81.2
