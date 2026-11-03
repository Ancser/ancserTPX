"""Order-flow research chart layers read the frozen research rows unchanged.

Each candidate event appears once (the ``all`` split), is tagged with its
phase from the evaluation/latest subsets, and the summary reproduces the
published statistics.  Nothing here may compute or alter a signal.
"""
from __future__ import annotations

import asyncio
import csv
import gzip
from pathlib import Path

import pytest

from backend.data import research_events as re_mod

HEADER = ["atr_blend", "bars_held", "context", "costs", "date", "direction",
          "directional_points", "entry_index", "entry_price", "exit_price",
          "exit_reason", "family", "filter", "gross_pnl", "net_pnl", "pi_message_id",
          "policy", "rule", "signal_epoch", "sl_price", "split", "tp_price"]


def _row(family, context, split, epoch, direction, pnl, date="2026-09-01"):
    return {"atr_blend": "50", "bars_held": "12", "context": context, "costs": "1.24",
            "date": date, "direction": str(direction), "directional_points": "1",
            "entry_index": "1", "entry_price": "30000.25", "exit_price": "30010.5",
            "exit_reason": "tp", "family": family, "filter": "context",
            "gross_pnl": str(pnl + 1.24), "net_pnl": str(pnl), "pi_message_id": "",
            "policy": "atr_blend", "rule": "passive_bid_no_breakout",
            "signal_epoch": str(epoch), "sl_price": "29950", "split": split,
            "tp_price": "30100"}


def _write(tmp_path: Path, rows) -> Path:
    path = tmp_path / "institutional_behavior_frozen_refresh_20990101T000000Z_trades.csv.gz"
    with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADER)
        writer.writeheader()
        writer.writerows(rows)
    return path


@pytest.fixture(autouse=True)
def _fresh_cache():
    re_mod._CACHE.update(key=None, value=None)
    yield
    re_mod._CACHE.update(key=None, value=None)


def test_each_event_once_with_its_phase_and_other_families_ignored(tmp_path):
    fam, ctx = "passive_touch_reject", "value_reversion"
    rows = [
        _row(fam, ctx, "all", 1000, 1, 50.0),
        _row(fam, ctx, "all", 2000, -1, -20.0),
        _row(fam, ctx, "all", 3000, 1, 10.0),
        _row(fam, ctx, "evaluation", 2000, -1, -20.0),
        _row(fam, ctx, "evaluation", 3000, 1, 10.0),
        _row(fam, ctx, "latest_two_sessions", 3000, 1, 10.0),
        _row(fam, "value_breakout", "all", 4000, 1, 99.0),      # not a layer
        _row("mbo_turnover", "cvd_aligned", "all", 5000, -1, 7.0),
    ]
    _write(tmp_path, rows)
    data = re_mod.load_orderflow_research_events(tmp_path)
    passive = [e for e in data["events"] if e["type"] == "of_passive"]
    assert [e["phase"] for e in passive] == ["discovery", "evaluation", "latest"]
    assert [e["direction"] for e in passive] == ["long", "short", "long"]
    assert passive[0]["time"] == "1970-01-01T00:16:40+00:00"
    assert passive[0]["entry"] == 30000.25 and passive[0]["net_pnl"] == 50.0
    assert len([e for e in data["events"] if e["type"] == "of_turnover"]) == 1
    assert not any(e["net_pnl"] == 99.0 for e in data["events"])      # breakout context excluded
    summary = data["summary"]["of_passive"]
    assert summary["all"]["n"] == 3 and summary["evaluation"]["n"] == 2
    assert summary["evaluation"]["pnl"] == -10.0


def test_missing_output_is_reported_not_raised(tmp_path):
    data = re_mod.load_orderflow_research_events(tmp_path)
    assert data["available"] is False and data["events"] == []


def test_endpoint_serves_the_layer_payload(tmp_path, monkeypatch):
    from backend.api import routes

    _write(tmp_path, [_row("mbo_turnover", "cvd_aligned", "all", 5000, 1, 7.0)])
    written = next(tmp_path.glob(re_mod.FROZEN_GLOB))
    monkeypatch.setattr(re_mod, "latest_frozen_trades", lambda root=None: written)
    body = asyncio.run(routes.research_orderflow_events())
    assert body["available"] is True
    assert body["events"][0]["type"] == "of_turnover"


def test_real_saved_rows_reproduce_the_published_evaluation_numbers():
    path = re_mod.latest_frozen_trades()
    if path is None or path.name != "institutional_behavior_frozen_refresh_20260923T221141Z_trades.csv.gz":
        pytest.skip("published frozen refresh not available on this machine")
    data = re_mod.load_orderflow_research_events()
    passive = data["summary"]["of_passive"]["evaluation"]
    turnover = data["summary"]["of_turnover"]["evaluation"]
    # docs/RESEARCH_REPORT.md §5.5 / institutional_behavior_frozen_refresh_*.md
    assert (passive["n"], passive["pnl"], passive["pf"], passive["stress_pf"]) == (195, 1739.2, 1.2173, 1.0428)
    assert (turnover["n"], turnover["pnl"], turnover["pf"], turnover["stress_pf"]) == (50, 515.0, 1.334, 1.0949)
