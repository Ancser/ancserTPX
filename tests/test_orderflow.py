from __future__ import annotations

import asyncio
import gzip
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from backend.api import routes
from backend.data import orderflow


UTC = timezone.utc
PRICE_SCALE = 1_000_000_000


def _ns(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1_000_000_000)


def _row(ts: str, action: str, side: str = "N", price: float = 0,
         size: int = 0, order_id: int = 0, flags: int = 128):
    return SimpleNamespace(
        ts_event=_ns(ts), action=action, side=side,
        price=int(price * PRICE_SCALE), size=size,
        order_id=order_id, flags=flags,
    )


def test_mbo_aggregation_tracks_aggression_passive_fill_and_depth():
    # 2026-09-01 06:30 Los Angeles = 13:30 UTC (PDT).
    rows = [
        _row("2026-09-01T13:30:01+00:00", "A", "B", 20000, 20, 1),
        _row("2026-09-01T13:30:02+00:00", "A", "A", 20000.25, 15, 2),
        _row("2026-09-01T13:30:03+00:00", "T", "B", 20000.25, 7, flags=0),
        _row("2026-09-01T13:30:03+00:00", "F", "A", 20000.25, 7, 2, flags=0),
        _row("2026-09-01T13:30:04+00:00", "T", "A", 20000, 5, flags=0),
        _row("2026-09-01T13:30:04+00:00", "F", "B", 20000, 5, 1, flags=0),
    ]
    payload = orderflow.aggregate_mbo_records(rows, "2026-09-01")

    assert len(payload["bars"]) == 1
    bar = payload["bars"][0]
    assert (bar["buy"], bar["sell"], bar["delta"], bar["trades"]) == (7, 5, 2, 2)
    cells = {row[0] * 0.25: row[1:] for row in bar["cells"]}
    assert cells[20000.25][:4] == [7, 0, 0, 7]
    assert cells[20000][:4] == [0, 5, 5, 0]
    assert cells[20000.25][5] >= 15
    assert cells[20000][4] >= 20


def test_snapshot_seeds_book_but_does_not_create_old_bar():
    snapshot = 8
    rows = [
        _row("2026-08-30T12:00:00+00:00", "A", "B", 20000, 10, 1, snapshot),
        _row("2026-09-01T13:30:01+00:00", "F", "B", 20000, 4, 1),
    ]
    payload = orderflow.aggregate_mbo_records(
        rows, "2026-09-01", snapshot_flag=snapshot,
    )
    assert len(payload["bars"]) == 1
    assert payload["bars"][0]["cells"][0][3] == 4


def test_new_minute_seeds_unchanged_resting_depth():
    rows = [
        _row("2026-09-01T13:30:01+00:00", "A", "B", 20000, 50, 1),
        _row("2026-09-01T13:30:02+00:00", "T", "A", 20000, 1),
        # The bid does not change in the next minute, but must still appear.
        _row("2026-09-01T13:31:02+00:00", "T", "A", 20000, 1),
    ]
    payload = orderflow.aggregate_mbo_records(rows, "2026-09-01")
    second = payload["bars"][1]
    cell = next(row for row in second["cells"] if row[0] == 80000)
    assert cell[5] == 50


def test_fill_then_partial_cancel_updates_book_exactly_once():
    rows = [
        _row("2026-09-01T13:30:01+00:00", "A", "B", 20000, 10, 1),
        _row("2026-09-01T13:30:02+00:00", "T", "A", 20000, 4, flags=0),
        _row("2026-09-01T13:30:02+00:00", "F", "B", 20000, 4, 1, flags=0),
        _row("2026-09-01T13:30:02+00:00", "C", "B", 20000, 4, 1),
        _row("2026-09-01T13:31:02+00:00", "T", "A", 20000, 1, flags=0),
    ]
    payload = orderflow.aggregate_mbo_records(rows, "2026-09-01")
    first, second = payload["bars"]
    first_cell = next(row for row in first["cells"] if row[0] == 80000)
    second_cell = next(row for row in second["cells"] if row[0] == 80000)
    assert first["cancel_qty"] == 4
    assert first_cell[3] == 4
    assert second_cell[5] == 6


def test_research_fields_capture_large_trade_refill_bbo_and_ofi():
    rows = [
        _row("2026-09-01T13:30:01+00:00", "A", "B", 20000, 200, 1),
        _row("2026-09-01T13:30:01+00:00", "A", "A", 20000.25, 100, 2),
        _row("2026-09-01T13:30:02+00:00", "T", "A", 20000, 150, flags=0),
        _row("2026-09-01T13:30:02+00:00", "F", "B", 20000, 150, 1, flags=0),
        _row("2026-09-01T13:30:02+00:00", "C", "B", 20000, 150, 1),
        _row("2026-09-01T13:30:02.100000+00:00", "A", "B", 20000, 150, 3),
    ]
    payload = orderflow.aggregate_mbo_records(rows, "2026-09-01")
    bar = payload["bars"][0]
    cell = next(row for row in bar["cells"] if row[0] == 80000)
    assert bar["sell_trades"] == 1
    assert bar["max_sell_trade"] == 150
    assert (bar["sell_ge_50"], bar["sell_ge_100"], bar["sell_ge_150"]) == (1, 1, 1)
    assert bar["bid_refill_qty"] == 150
    assert cell[7] == 150
    assert cell[14] == 150
    assert (bar["close_bid_price"], bar["close_bid_depth"]) == (20000.0, 200)
    assert (bar["close_ask_price"], bar["close_ask_depth"]) == (20000.25, 100)
    assert isinstance(bar["ofi"], int)


def test_per_price_trade_size_bands_are_exact_and_side_specific():
    rows = [
        _row("2026-09-01T13:30:01+00:00", "T", "B", 20000, 50, flags=0),
        _row("2026-09-01T13:30:01+00:00", "T", "B", 20000, 99, flags=0),
        _row("2026-09-01T13:30:01+00:00", "T", "B", 20000, 100, flags=0),
        _row("2026-09-01T13:30:01+00:00", "T", "B", 20000, 149, flags=0),
        _row("2026-09-01T13:30:01+00:00", "T", "B", 20000, 150, flags=0),
        _row("2026-09-01T13:30:01+00:00", "T", "A", 20000, 150, flags=0),
    ]
    payload = orderflow.aggregate_mbo_records(rows, "2026-09-01")
    cell = payload["bars"][0]["cells"][0]
    assert cell[9:12] == [149, 249, 150]
    assert cell[12:15] == [0, 0, 150]


def test_load_cache_filters_visible_range_and_rolls_up(monkeypatch, tmp_path):
    root = tmp_path / "market"
    monkeypatch.setenv("ANCSER_MARKET_DATA_ROOT", str(root))
    path = orderflow.footprint_cache_path("2026-09-01", "MNQ")
    bars = []
    for minute in range(6):
        epoch = 1_788_269_400 + minute * 60
        bars.append({
            "time": datetime.fromtimestamp(epoch, UTC).isoformat(), "epoch": epoch,
            "open": 100 + minute, "high": 101 + minute, "low": 99 + minute,
            "close": 100.5 + minute, "buy": 2, "sell": 1, "delta": 1,
            "trades": 1, "adds": 1, "cancels": 0, "modifies": 0,
            "fills": 1, "add_qty": 2, "cancel_qty": 0,
            "cells": [[400 + minute, 2, 1, 0, 0, 3, 4]],
        })
    path.parent.mkdir(parents=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump({"meta": {"symbol": "MNQ", "tick_size": 0.25}, "bars": bars}, handle)
    # A cache far outside the visible window must not even be decompressed.
    # This is deliberately invalid gzip and would fail the test if scanned.
    far = orderflow.footprint_cache_path("2025-01-01", "MNQ")
    far.write_bytes(b"not gzip")

    result = orderflow.load_cached_footprint(
        datetime.fromtimestamp(bars[0]["epoch"], UTC),
        datetime.fromtimestamp(bars[-1]["epoch"], UTC),
        interval="5m",
    )
    assert result["available"] is True
    assert result["count"] == 2
    assert result["bars"][0]["buy"] == 10
    assert result["bars"][0]["sell"] == 5
    assert result["bars"][0]["cvd"] == 5
    assert result["bars"][1]["cvd"] == 6
    assert result["meta"]["cvd"] == "rth_cumulative_aggressive_delta"
    assert result["bars"][0]["cells"][0][5:7] == [3, 4]


def test_footprint_route_delegates_to_compact_cache(monkeypatch):
    expected = {"available": True, "bars": [], "count": 0}

    def fake_load(start, end, **kwargs):
        assert kwargs == {"symbol": "MNQ", "interval": "1m", "limit": 100}
        return expected

    monkeypatch.setattr(orderflow, "load_cached_footprint", fake_load)
    result = asyncio.run(routes.get_orderflow_footprint(
        start="2026-09-01T13:30:00Z",
        end="2026-09-01T20:00:00Z",
        symbol="MNQ",
        interval="1m",
        limit=100,
    ))
    assert result is expected


def test_frontend_footprint_is_lazy_visible_window_layer():
    root = Path(__file__).resolve().parents[1]
    js = (root / "frontend" / "static" / "ancserTPX.js").read_text(encoding="utf-8")
    html = (root / "frontend" / "static" / "ancserTPX.html").read_text(encoding="utf-8")
    assert "{ key: 'footprint', label: 'FOOTPRINT / LEVEL 2', on: false }" in js
    assert "{ key: 'cvd',      label: 'CVD / DELTA',             on: false }" in js
    assert "API + '/data/orderflow/footprint?'" in js
    assert "getVisibleLogicalRange()" in js
    assert "scheduleFootprintRefresh" in js
    assert "drawFootprintLayer" in js
    assert "drawCvdLayer" in js
    assert "rth_cumulative_aggressive_delta" in js or "CVD RTH" in js
    assert "passiveStrength" in js
    assert "ORDERFLOW_DETAIL_CELL_LIMIT" in js
    assert "ORDERFLOW_COMPACT_CELL_LIMIT" in js
    assert "_compactFootprintBars" in js
    assert "FOOTPRINT COMPACT · ZOOM IN FOR LEVELS" in js
    assert "ORDERFLOW_COMPACT_COLUMN_LIMIT = 1" in js
    assert "ORDERFLOW_COMPACT_MIN_DELTA = 50" in js
    assert "eligible.length ? eligible : allItems" in js
    assert "let _orderflowRequestSerial = 0" in js
    assert "requestId !== _orderflowRequestSerial" in js
    assert "function _orderflowChartSpacing" in js
    assert "_orderflowChartSpacing() || 8" in js
    assert "function _sizeOrderflowCanvas" in js
    draw_start = js.index("function drawIndicatorSignalOverlay")
    draw_end = js.index("function drawPiSignalOverlay", draw_start)
    indicator = js[draw_start:draw_end]
    assert "_sizeOrderflowCanvas(canvas, container, dpr);" in indicator
    assert "canvas.width = W * dpr" not in indicator
    assert "markOrderflowInteraction();" in js
    assert "performance.now() < _orderflowInteractionUntil" in js
    assert "function drawFootprintDeltaBar" in js
    assert "negative delta extends left in red" in js
    assert "const delta = buy - sell" in js
    assert "drawFootprintDeltaBar(" in js
    assert "const magnitude = Math.abs(signedDelta)" in js
    assert "function _syncFootprintCandleVisibility" in js
    assert "candleSeries.applyOptions({visible: !hideCandles})" in js
    assert "drawFootprintVolumeBar" not in js
    assert "drawPseudoBubble" not in js
    assert "_footprintBubbleRadius" not in js
    assert "bubble.passive &&" not in js
    assert "ORDERFLOW_MAX_DEPTH_LEVELS" in js
    assert "Math.floor(ORDERFLOW_MAX_DEPTH_LEVELS / 2)" in js
    assert "const depthLevels = {bid: new Map(), ask: new Map()};" in js
    assert "class=\"chart-signal-legend\"" in html
    assert "rgba(245,248,252" in js
    assert "rgba(255,45,70" in js
    assert "PASSIVE=FILL OPACITY" in html
    assert 'data-switch-proxy="lp-footprint"' in html
    assert 'data-switch-proxy="lp-cvd"' in html
    assert "FOOTPRINT DELTA BARS" in html
    assert "Δ=BUY−SELL" in html
