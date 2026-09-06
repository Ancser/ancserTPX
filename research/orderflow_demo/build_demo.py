"""Build an isolated ATAS-style footprint demo from a Databento MBO file.

This research-only tool intentionally lives outside the ancserTPX runtime. It
replays one DBN file, aggregates the regular US session into five-minute
footprint cells, and embeds a compact local dataset in a standalone HTML file.
It does not add a chart layer, route, strategy, or import to the production
application.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import databento as db


NANO = 1_000_000_000
PRICE_SCALE = 1_000_000_000
TICK_NANO = 250_000_000  # MNQ 0.25 point tick
BAR_SECONDS = 5 * 60
DEMO_DATE = "2026-09-02"
RTH_START = 13 * 3600 + 30 * 60  # 06:30 America/Los_Angeles
RTH_END = 20 * 3600  # 13:00 America/Los_Angeles
HEAT_RADIUS_POINTS = 20.0


def _utc_seconds(ts: int) -> int:
    return int(ts) // NANO


def _dt(ts: int) -> datetime:
    return datetime.fromtimestamp(_utc_seconds(ts), tz=timezone.utc)


def _in_rth(ts: int, trade_date: str = DEMO_DATE) -> bool:
    d = _dt(ts)
    seconds = d.hour * 3600 + d.minute * 60 + d.second
    return d.date().isoformat() == trade_date and RTH_START <= seconds < RTH_END


def _tick(price_nano: int) -> int:
    return int(round(price_nano / TICK_NANO))


def _price(tick: int) -> float:
    return round(tick * 0.25, 2)


def _new_bar(index: int, ts: int) -> dict:
    return {
        "i": index,
        "t": _dt(ts).strftime("%H:%M"),
        "epoch": _utc_seconds(ts),
        "open": None,
        "high": None,
        "low": None,
        "close": None,
        "buy": 0,
        "sell": 0,
        "delta": 0,
        "trades": 0,
        "adds": 0,
        "cancels": 0,
        "modifies": 0,
        "fills": 0,
        "add_qty": 0,
        "cancel_qty": 0,
        # [aggressive buy, aggressive sell, passive bid fill, passive ask fill]
        "cells": defaultdict(lambda: [0, 0, 0, 0]),
        "heat": defaultdict(lambda: [0, 0]),
    }


def _new_minute(index: int, ts: int) -> dict:
    return {
        "i": index,
        "t": _dt(ts).strftime("%H:%M"),
        "open": None,
        "high": None,
        "low": None,
        "close": None,
        "buy": 0,
        "sell": 0,
        "fill_bid": 0,
        "fill_ask": 0,
        "delta": 0,
        "trades": 0,
        "cells": defaultdict(lambda: [0, 0, 0, 0]),
    }


def _bar_index(ts: int) -> int:
    d = _dt(ts)
    seconds = d.hour * 3600 + d.minute * 60 + d.second
    return (seconds - RTH_START) // BAR_SECONDS


def build(dbn_path: Path, output_path: Path, trade_date: str = DEMO_DATE) -> dict:
    store = db.DBNStore.from_file(dbn_path)
    bars = {i: _new_bar(i, (13 * 3600 + 30 * 60 + i * BAR_SECONDS) * NANO)
            for i in range((RTH_END - RTH_START) // BAR_SECONDS)}
    minutes = {
        i: _new_minute(i, (13 * 3600 + 30 * 60 + i * 60) * NANO)
        for i in range((RTH_END - RTH_START) // 60)
    }
    orders: dict[int, list] = {}
    levels: defaultdict[tuple[str, int], int] = defaultdict(int)
    last_trade_nano: int | None = None
    record_count = 0

    def heat_update(bar: dict, side: str, price_nano: int, qty: int) -> None:
        if last_trade_nano is None or price_nano >= 10**18:
            return
        if abs(price_nano - last_trade_nano) / PRICE_SCALE > HEAT_RADIUS_POINTS:
            return
        key = _tick(price_nano)
        slot = bar["heat"][key]
        if side == "B":
            slot[0] = max(slot[0], int(qty))
        elif side == "A":
            slot[1] = max(slot[1], int(qty))

    def replay(record) -> None:
        nonlocal record_count, last_trade_nano
        record_count += 1
        flags = int(record.flags)
        action = str(record.action)
        side = str(record.side)
        order_id = int(record.order_id)
        size = int(record.size)
        price_nano = int(record.price)

        # The historical file contains a synthetic UTC-midnight snapshot. It
        # seeds the book but is not part of the RTH visual statistics.
        is_snapshot = bool(flags & int(db.RecordFlags.F_SNAPSHOT))
        if price_nano >= 10**18:
            price_nano = 0

        if is_snapshot:
            if action == "R":
                orders.clear()
                levels.clear()
            elif action == "A" and order_id and side in ("A", "B") and price_nano:
                orders[order_id] = [side, price_nano, size]
                levels[(side, price_nano)] += size
            return

        if action == "T" and side in ("A", "B") and price_nano:
            last_trade_nano = price_nano

        if not _in_rth(record.ts_event, trade_date):
            # Maintain the book outside RTH, but do not allocate chart cells.
            if action == "R":
                orders.clear()
                levels.clear()
            elif action == "A" and order_id and side in ("A", "B") and price_nano:
                orders[order_id] = [side, price_nano, size]
                levels[(side, price_nano)] += size
            elif action == "M" and order_id:
                old = orders.get(order_id)
                if old:
                    levels[(old[0], old[1])] -= old[2]
                    new_side = side if side in ("A", "B") else old[0]
                    orders[order_id] = [new_side, price_nano or old[1], size]
                    levels[(new_side, price_nano or old[1])] += size
            elif action == "C" and order_id:
                old = orders.pop(order_id, None)
                if old:
                    levels[(old[0], old[1])] -= old[2]
            elif action == "F" and order_id:
                old = orders.get(order_id)
                if old:
                    key = (old[0], old[1])
                    take = min(old[2], size)
                    levels[key] -= take
                    old[2] -= take
                    if old[2] <= 0:
                        orders.pop(order_id, None)
            return

        idx = _bar_index(record.ts_event)
        bar = bars.get(idx)
        if bar is None:
            return
        event_dt = _dt(record.ts_event)
        event_seconds = event_dt.hour * 3600 + event_dt.minute * 60 + event_dt.second
        minute_idx = (event_seconds - RTH_START) // 60
        minute = minutes.get(minute_idx)
        if minute is None:
            return

        if action == "T" and side in ("A", "B") and price_nano:
            tick = _tick(price_nano)
            px = _price(tick)
            if bar["open"] is None:
                bar["open"] = px
            bar["high"] = px if bar["high"] is None else max(bar["high"], px)
            bar["low"] = px if bar["low"] is None else min(bar["low"], px)
            bar["close"] = px
            bar["trades"] += 1
            if side == "B":
                bar["buy"] += size
                bar["cells"][tick][0] += size
                minute["buy"] += size
                minute["cells"][tick][0] += size
            else:
                bar["sell"] += size
                bar["cells"][tick][1] += size
                minute["sell"] += size
                minute["cells"][tick][1] += size
            bar["delta"] = bar["buy"] - bar["sell"]
            minute["delta"] = minute["buy"] - minute["sell"]
            for target in (minute,):
                if target["open"] is None:
                    target["open"] = px
                target["high"] = px if target["high"] is None else max(target["high"], px)
                target["low"] = px if target["low"] is None else min(target["low"], px)
                target["close"] = px
                target["trades"] += 1

        if action == "A" and order_id and side in ("A", "B") and price_nano:
            orders[order_id] = [side, price_nano, size]
            levels[(side, price_nano)] += size
            bar["adds"] += 1
            bar["add_qty"] += size
            heat_update(bar, side, price_nano, levels[(side, price_nano)])
        elif action == "M" and order_id:
            old = orders.get(order_id)
            if old:
                old_key = (old[0], old[1])
                levels[old_key] -= old[2]
                new_side = side if side in ("A", "B") else old[0]
                new_price = price_nano or old[1]
                orders[order_id] = [new_side, new_price, size]
                levels[(new_side, new_price)] += size
                heat_update(bar, new_side, new_price, levels[(new_side, new_price)])
            elif order_id and side in ("A", "B") and price_nano:
                orders[order_id] = [side, price_nano, size]
                levels[(side, price_nano)] += size
                heat_update(bar, side, price_nano, levels[(side, price_nano)])
            bar["modifies"] += 1
        elif action == "C" and order_id:
            old = orders.pop(order_id, None)
            if old:
                key = (old[0], old[1])
                levels[key] -= old[2]
                bar["cancel_qty"] += old[2]
                heat_update(bar, old[0], old[1], levels[key])
            bar["cancels"] += 1
        elif action == "F" and order_id:
            old = orders.get(order_id)
            if old:
                key = (old[0], old[1])
                take = min(old[2], size)
                levels[key] -= take
                old[2] -= take
                if old[2] <= 0:
                    orders.pop(order_id, None)
                heat_update(bar, key[0], key[1], levels[key])
                fill_tick = _tick(key[1])
                if key[0] == "B":
                    bar["cells"][fill_tick][2] += take
                    minute["cells"][fill_tick][2] += take
                    minute["fill_bid"] += take
                else:
                    bar["cells"][fill_tick][3] += take
                    minute["cells"][fill_tick][3] += take
                    minute["fill_ask"] += take
            bar["fills"] += 1
        elif action == "R":
            orders.clear()
            levels.clear()

    store.replay(replay)

    serial_bars = []
    for index in range(len(bars)):
        bar = bars[index]
        bar["cells"] = [
            [int(tick), int(values[0]), int(values[1]), int(values[2]), int(values[3])]
            for tick, values in sorted(bar["cells"].items())
            if any(values)
        ]
        bar["heat"] = [
            [int(tick), int(values[0]), int(values[1])]
            for tick, values in sorted(bar["heat"].items())
            if values[0] or values[1]
        ]
        serial_bars.append(bar)

    serial_minutes = []
    for index in range(len(minutes)):
        minute = minutes[index]
        minute["cells"] = [
            [int(tick), int(values[0]), int(values[1]), int(values[2]), int(values[3])]
            for tick, values in sorted(minute["cells"].items())
            if any(values)
        ]
        serial_minutes.append(minute)

    payload = {
        "meta": {
            "date": trade_date,
            "symbol": "MNQU6",
            "dataset": "GLBX.MDP3",
            "schema": "mbo",
            "timezone": "UTC display; RTH = 06:30–13:00 America/Los_Angeles",
            "bar_minutes": 5,
            "rth_bars": len(serial_bars),
            "rth_minutes": len(serial_minutes),
            "source_records": record_count,
            "source_file": dbn_path.name,
        },
        "bars": serial_bars,
        "minutes": serial_minutes,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    html = HTML_TEMPLATE.replace("__DATE__", trade_date).replace("__DATA__", json.dumps(payload, separators=(",", ":")))
    output_path.write_text(html, encoding="utf-8")
    return payload


HTML_TEMPLATE = r'''<div id="orderflow-demo" class="of-demo">
  <style>
    #orderflow-demo { --bg:#10151c; --panel:#151d26; --line:#2d3a47; --text:#e7edf2; --muted:#9aa8b5; --buy:#36d399; --sell:#fb7185; --heat:#f4b860; --focus:#7dd3fc; color:var(--text); background:var(--bg); font-family: ui-sans-serif, system-ui, sans-serif; max-width:1200px; margin:0 auto; padding:16px; box-sizing:border-box; }
    #orderflow-demo * { box-sizing:border-box; }
    #orderflow-demo h1 { font-size:20px; font-weight:500; margin:0 0 4px; }
    #orderflow-demo p { margin:0; color:var(--muted); font-size:12px; }
    #orderflow-demo .of-toolbar { display:flex; flex-wrap:wrap; align-items:end; gap:12px; margin:14px 0 10px; }
    #orderflow-demo label { color:var(--muted); font-size:12px; display:flex; flex-direction:column; gap:5px; }
    #orderflow-demo select, #orderflow-demo input { color:var(--text); background:var(--panel); border:1px solid var(--line); border-radius:4px; padding:6px 8px; }
    #orderflow-demo input[type=range] { width:min(380px, 62vw); padding:0; }
    #orderflow-demo .of-readout { display:flex; flex-wrap:wrap; gap:9px 16px; margin:8px 0 10px; font-size:12px; }
    #orderflow-demo .of-readout strong { font-weight:500; color:var(--text); }
    #orderflow-demo .of-canvas-wrap { position:relative; border:1px solid var(--line); background:#0c1117; overflow:hidden; }
    #orderflow-demo canvas { display:block; width:100%; height:650px; }
    #orderflow-demo .of-detail { min-height:22px; color:var(--muted); font-size:12px; padding-top:8px; }
    #orderflow-demo .of-legend { display:flex; flex-wrap:wrap; gap:12px; margin-top:8px; color:var(--muted); font-size:11px; }
    #orderflow-demo .swatch { width:10px; height:10px; display:inline-block; border-radius:2px; margin-right:4px; vertical-align:-1px; }
    #orderflow-demo .buy { background:var(--buy); } #orderflow-demo .sell { background:var(--sell); } #orderflow-demo .heat { background:var(--heat); } #orderflow-demo .bubble { background:#c084fc; }
    @media (max-width:600px) { #orderflow-demo { padding:10px; } #orderflow-demo canvas { height:560px; } #orderflow-demo .of-toolbar { gap:8px; } }
  </style>
  <h1>MNQ MBO footprint demo · __DATE__</h1>
  <p>RTH five-minute aggregation from full-session CME MBO. ATAS ladder shows normal candles on the left and the selected bar's numbers on the right; bubbles use one-minute cells.</p>
  <div class="of-toolbar">
    <label>View
      <select id="of-view"><option value="ladder" selected>ATAS ladder</option><option value="footprint">Footprint grid</option><option value="heatmap">Liquidity heatmap</option></select>
    </label>
    <label>Imbalance
      <select id="of-ratio"><option value="3">3:1</option><option value="5">5:1</option><option value="10" selected>10:1</option><option value="20">20:1</option></select>
    </label>
    <label>Min print
      <select id="of-min"><option value="50">50</option><option value="100" selected>100</option><option value="150">150</option><option value="200">200</option></select>
    </label>
    <label>Passive fill
      <select id="of-passive"><option value="0">off</option><option value="50">50</option><option value="100" selected>100</option><option value="150">150</option></select>
    </label>
    <label>Focus bar <output id="of-focus-label">—</output>
      <input id="of-focus" type="range" min="0" max="77" value="38" step="1" aria-label="Focus five-minute bar">
    </label>
  </div>
  <div class="of-readout" id="of-readout" aria-live="polite"></div>
  <div class="of-canvas-wrap"><canvas id="of-canvas" role="img" aria-label="MNQ candlesticks and ATAS footprint ladder"></canvas></div>
  <div class="of-detail" id="of-detail">Move over the candle or right ladder to inspect price, fills, and delta.</div>
  <div class="of-legend"><span><i class="swatch sell"></i>sell aggressor</span><span><i class="swatch buy"></i>buy aggressor</span><span><i class="swatch heat"></i>displayed liquidity intensity</span><span><i class="swatch bubble"></i>filtered 1m bubble</span><span>F.bid/F.ask = matched passive fills</span><span>delta = buy − sell</span></div>
  <script>
  (() => {
    const DATA = __DATA__;
    const root = document.getElementById('orderflow-demo');
    const canvas = document.getElementById('of-canvas');
    const ctx = canvas.getContext('2d');
    const view = document.getElementById('of-view');
    const ratio = document.getElementById('of-ratio');
    const minPrint = document.getElementById('of-min');
    const passive = document.getElementById('of-passive');
    const focus = document.getElementById('of-focus');
    const focusLabel = document.getElementById('of-focus-label');
    const readout = document.getElementById('of-readout');
    const detail = document.getElementById('of-detail');
    const bars = DATA.bars;
    const minutes = DATA.minutes || [];
    const visible = 31;
    let geom = null;

    const css = getComputedStyle(root);
    const palette = { bg: '#0c1117', line: '#263441', text: '#e7edf2', muted: '#9aa8b5', buy: '#36d399', sell: '#fb7185', heat: '#f4b860', focus: '#7dd3fc' };
    const tickPrice = (t) => (t * 0.25).toFixed(2);
    const fmt = (n) => Math.round(n).toLocaleString();
    const alpha = (v, max, floor=0.08) => floor + Math.min(0.75, Math.log1p(Math.max(0, v)) / Math.log1p(Math.max(1, max)) * 0.67);

    // MBO cell = [price tick, aggressive buy, aggressive sell,
    // passive bid fills, passive ask fills].  A green bubble is a buy
    // imbalance matched by resting ask fills; red is the sell analogue.
    function bubbleKind(cell) {
      const buy = Number(cell[1] || 0), sell = Number(cell[2] || 0);
      const fillBid = Number(cell[3] || 0), fillAsk = Number(cell[4] || 0);
      const r = Number(ratio.value), min = Number(minPrint.value), p = Number(passive.value);
      if (buy >= min && buy >= r * Math.max(1, sell) && (p === 0 || fillAsk >= p)) return { side: 'buy', total: buy, passive: fillAsk };
      if (sell >= min && sell >= r * Math.max(1, buy) && (p === 0 || fillBid >= p)) return { side: 'sell', total: sell, passive: fillBid };
      return null;
    }

    function bubblesForBar(bar) {
      const out = [];
      const first = Number(bar.i) * 5;
      for (const minute of minutes.slice(first, first + 5)) {
        for (const cell of (minute.cells || [])) {
          const kind = bubbleKind(cell);
          if (kind) out.push({ ...kind, tick: Number(cell[0]), minute: minute.i });
        }
      }
      return out;
    }

    function drawBubble(x, y, event, radiusScale=0.34) {
      const radius = Math.min(15, 4 + Math.sqrt(event.total) * radiusScale);
      const green = event.side === 'buy';
      ctx.beginPath(); ctx.arc(x, y, radius, 0, Math.PI * 2);
      ctx.fillStyle = green ? 'rgba(54,211,153,.78)' : 'rgba(251,113,133,.78)';
      ctx.fill(); ctx.strokeStyle = green ? 'rgba(151,255,211,.95)' : 'rgba(255,180,194,.95)'; ctx.stroke();
      if (radius >= 8) { ctx.fillStyle = palette.text; ctx.font = '10px ui-sans-serif, system-ui, sans-serif'; ctx.textAlign = 'center'; ctx.fillText(fmt(event.total), x, y + 1); }
    }

    function resize() {
      const dpr = window.devicePixelRatio || 1;
      const rect = canvas.getBoundingClientRect();
      canvas.width = Math.max(1, Math.floor(rect.width * dpr));
      canvas.height = Math.max(1, Math.floor(rect.height * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      draw();
    }

    function drawGrid() {
      const w = canvas.clientWidth, h = canvas.clientHeight;
      ctx.clearRect(0, 0, w, h);
      const center = Number(focus.value);
      const start = Math.max(0, Math.min(bars.length - visible, center - Math.floor(visible / 2)));
      const shown = bars.slice(start, start + visible);
      const selected = bars[center];
      const centerTick = Math.round((selected.close || selected.open || 0) / 0.25);
      const rows = 72;
      const step = 2; // display 0.50-point rows while retaining tick-level data in the payload
      const low = centerTick - Math.floor(rows * step / 2);
      const high = low + rows * step;
      const left = 60, right = 8, top = 24, bottom = 88;
      const plotW = Math.max(80, w - left - right), plotH = Math.max(120, h - top - bottom);
      const colW = plotW / shown.length, rowH = plotH / rows;
      geom = { start, shown, low, high, step, left, top, plotW, plotH, colW, rowH };

      ctx.fillStyle = palette.bg; ctx.fillRect(0, 0, w, h);
      ctx.font = '11px ui-sans-serif, system-ui, sans-serif';
      ctx.textBaseline = 'middle';
      ctx.strokeStyle = palette.line; ctx.lineWidth = 1;
      for (let r = 0; r <= rows; r += 4) {
        const y = top + r * rowH;
        ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(left + plotW, y); ctx.stroke();
        ctx.fillStyle = palette.muted; ctx.textAlign = 'right'; ctx.fillText(tickPrice(high - r * step), left - 7, y + 1);
      }
      for (let c = 0; c < shown.length; c++) {
        const x = left + c * colW;
        ctx.strokeStyle = c % 3 === 0 ? '#344451' : '#1b2731';
        ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, top + plotH); ctx.stroke();
        if (c % 3 === 0 || c + start === center) {
          ctx.fillStyle = palette.muted; ctx.textAlign = 'center'; ctx.fillText(shown[c].t, x + colW / 2, top - 12);
        }
      }
      const maxHeat = Math.max(1, ...shown.flatMap(b => b.heat.map(v => Math.max(v[1], v[2]))));
      const maxVol = Math.max(1, ...shown.flatMap(b => b.cells.map(v => Math.max(v[1], v[2]))));
      for (let c = 0; c < shown.length; c++) {
        const bar = shown[c];
        const x = left + c * colW;
        const cells = new Map(bar.cells.map(v => [v[0], v]));
        const heat = new Map(bar.heat.map(v => [v[0], v]));
        for (let r = 0; r < rows; r++) {
          const binLo = high - (r + 1) * step;
          const binHi = high - r * step;
          let buy = 0, sell = 0, bidDepth = 0, askDepth = 0;
          for (let t = binLo; t < binHi; t++) {
            const cell = cells.get(t); if (cell) { buy += cell[1]; sell += cell[2]; }
            const dep = heat.get(t); if (dep) { bidDepth = Math.max(bidDepth, dep[1]); askDepth = Math.max(askDepth, dep[2]); }
          }
          const y = top + r * rowH;
          if (view.value === 'heatmap') {
            const bidA = alpha(bidDepth, maxHeat), askA = alpha(askDepth, maxHeat);
            if (bidDepth) { ctx.fillStyle = `rgba(54,211,153,${bidA})`; ctx.fillRect(x + 1, y + 1, colW / 2 - 1, rowH - 2); }
            if (askDepth) { ctx.fillStyle = `rgba(251,113,133,${askA})`; ctx.fillRect(x + colW / 2, y + 1, colW / 2 - 1, rowH - 2); }
          } else if (buy || sell) {
            const a = alpha(Math.max(buy, sell), maxVol, 0.12);
            if (sell) { ctx.fillStyle = `rgba(251,113,133,${a})`; ctx.fillRect(x + 1, y + 1, colW / 2 - 1, rowH - 2); }
            if (buy) { ctx.fillStyle = `rgba(54,211,153,${a})`; ctx.fillRect(x + colW / 2, y + 1, colW / 2 - 1, rowH - 2); }
            if (rowH >= 8 && colW >= 22) {
              ctx.font = '10px ui-sans-serif, system-ui, sans-serif'; ctx.fillStyle = palette.text; ctx.textAlign = 'center';
              ctx.fillText(sell ? fmt(sell) : '·', x + colW * .25, y + rowH / 2);
              ctx.fillText(buy ? fmt(buy) : '·', x + colW * .75, y + rowH / 2);
            }
          }
        }
        // Filtered bubbles are derived from the one-minute cells, not the
        // whole five-minute total. This makes a 10:1 event genuinely short.
        for (const event of bubblesForBar(bar)) {
          const row = Math.floor((high - event.tick) / step);
          if (row < 0 || row >= rows) continue;
          const bx = x + ((event.minute % 5) + .5) / 5 * colW;
          const by = top + (row + .5) * rowH;
          drawBubble(bx, by, event);
        }
        if (c + start === center) { ctx.strokeStyle = palette.focus; ctx.lineWidth = 2; ctx.strokeRect(x + 1, top + 1, colW - 2, plotH - 2); ctx.lineWidth = 1; }
      }
      // Delta strip.
      const deltaTop = top + plotH + 18, deltaH = bottom - 30;
      const maxDelta = Math.max(1, ...shown.map(b => Math.abs(b.delta)));
      ctx.strokeStyle = palette.line; ctx.beginPath(); ctx.moveTo(left, deltaTop + deltaH / 2); ctx.lineTo(left + plotW, deltaTop + deltaH / 2); ctx.stroke();
      for (let c = 0; c < shown.length; c++) {
        const bar = shown[c], x = left + c * colW, magnitude = Math.min(1, Math.abs(bar.delta) / maxDelta);
        const bh = magnitude * (deltaH / 2 - 2); ctx.fillStyle = bar.delta >= 0 ? palette.buy : palette.sell;
        ctx.globalAlpha = .72; ctx.fillRect(x + 2, bar.delta >= 0 ? deltaTop + deltaH / 2 - bh : deltaTop + deltaH / 2, Math.max(2, colW - 4), bh); ctx.globalAlpha = 1;
      }
      ctx.fillStyle = palette.muted; ctx.textAlign = 'left'; ctx.fillText('delta', 8, deltaTop + deltaH / 2); ctx.textAlign = 'right'; ctx.fillText('0', left - 7, deltaTop + deltaH / 2);
      focusLabel.value = `${selected.t} UTC · ${selected.close?.toFixed(2) ?? '—'}`;
      readout.innerHTML = `<strong>${selected.t} UTC</strong><span>O ${selected.open?.toFixed(2) ?? '—'} · H ${selected.high?.toFixed(2) ?? '—'} · L ${selected.low?.toFixed(2) ?? '—'} · C ${selected.close?.toFixed(2) ?? '—'}</span><span>Δ ${fmt(selected.delta)} · buy ${fmt(selected.buy)} · sell ${fmt(selected.sell)} · trades ${fmt(selected.trades)}</span><span>adds ${fmt(selected.add_qty)} · cancels ${fmt(selected.cancel_qty)}</span>`;
    }

    function drawLadder() {
      const w = canvas.clientWidth, h = canvas.clientHeight;
      const center = Number(focus.value);
      const start = Math.max(0, Math.min(bars.length - visible, center - Math.floor(visible / 2)));
      const shown = bars.slice(start, start + visible), selected = bars[center];
      const left = 48, top = 34, bottom = 96, gap = 16;
      const chartW = Math.max(240, Math.min(w * .66, w - 300));
      const ladderLeft = chartW + gap, ladderW = Math.max(220, w - ladderLeft - 8);
      const plotH = Math.max(140, h - top - bottom), colW = chartW / shown.length;
      const lows = shown.filter(b => b.low != null).map(b => b.low), highs = shown.filter(b => b.high != null).map(b => b.high);
      const lo = Math.min(...lows), hi = Math.max(...highs), pad = Math.max(1, (hi - lo) * .06);
      const chartLo = lo - pad, chartHi = hi + pad, chartRange = Math.max(.01, chartHi - chartLo);
      const ladderCenter = Math.round((selected.close || selected.open || lo) / .25);
      const ladderLow = Math.floor((ladderCenter - 22) / 2) * 2, ladderRows = 44, ladderHigh = ladderLow + ladderRows * 2, ladderRowH = plotH / ladderRows;
      geom = { mode: 'ladder', start, shown, left, top, chartW, plotH, colW, chartLo, chartHi, ladderLeft, ladderW, ladderLow, ladderHigh, ladderRows, ladderRowH };
      ctx.fillStyle = palette.bg; ctx.fillRect(0, 0, w, h);
      ctx.font = '11px ui-sans-serif, system-ui, sans-serif'; ctx.textBaseline = 'middle';
      const yPrice = (p) => top + (chartHi - p) / chartRange * plotH;
      for (let k = 0; k <= 6; k++) { const y = top + k * plotH / 6; ctx.strokeStyle = palette.line; ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(left + chartW, y); ctx.stroke(); ctx.fillStyle = palette.muted; ctx.textAlign = 'right'; ctx.fillText((chartHi - k * chartRange / 6).toFixed(2), left - 7, y); }
      for (let c = 0; c < shown.length; c++) {
        const bar = shown[c], x = left + c * colW + colW / 2;
        if (bar.high != null) { const up = (bar.close ?? 0) >= (bar.open ?? 0); ctx.strokeStyle = up ? palette.buy : palette.sell; ctx.beginPath(); ctx.moveTo(x, yPrice(bar.high)); ctx.lineTo(x, yPrice(bar.low)); ctx.stroke(); ctx.fillStyle = up ? palette.buy : palette.sell; const bt = yPrice(Math.max(bar.open, bar.close)), bb = yPrice(Math.min(bar.open, bar.close)); ctx.fillRect(Math.max(left + 1, x - Math.max(2, colW * .28)), bt, Math.max(3, colW * .56), Math.max(1, bb - bt)); }
        if (c % 3 === 0 || c + start === center) { ctx.fillStyle = palette.muted; ctx.textAlign = 'center'; ctx.fillText(bar.t, x, top - 14); }
        if (c + start === center) { ctx.strokeStyle = palette.focus; ctx.lineWidth = 2; ctx.strokeRect(left + c * colW + 1, top, colW - 2, plotH); ctx.lineWidth = 1; }
        for (const event of bubblesForBar(bar)) { const yy = yPrice(event.tick * .25); if (yy >= top && yy <= top + plotH) drawBubble(left + c * colW + ((event.minute % 5) + .5) / 5 * colW, yy, event, .30); }
      }
      const deltaTop = top + plotH + 18, deltaH = bottom - 30, maxDelta = Math.max(1, ...shown.map(b => Math.abs(b.delta)));
      ctx.strokeStyle = palette.line; ctx.beginPath(); ctx.moveTo(left, deltaTop + deltaH / 2); ctx.lineTo(left + chartW, deltaTop + deltaH / 2); ctx.stroke();
      for (let c = 0; c < shown.length; c++) { const bar = shown[c], bh = Math.min(1, Math.abs(bar.delta) / maxDelta) * (deltaH / 2 - 2); ctx.fillStyle = bar.delta >= 0 ? palette.buy : palette.sell; ctx.globalAlpha = .72; ctx.fillRect(left + c * colW + 2, bar.delta >= 0 ? deltaTop + deltaH / 2 - bh : deltaTop + deltaH / 2, Math.max(2, colW - 4), bh); ctx.globalAlpha = 1; }
      ctx.fillStyle = palette.muted; ctx.textAlign = 'left'; ctx.fillText('delta', 8, deltaTop + deltaH / 2);
      ctx.fillStyle = '#111a23'; ctx.fillRect(ladderLeft, top - 4, ladderW, plotH + 8); ctx.strokeStyle = palette.focus; ctx.strokeRect(ladderLeft, top - 4, ladderW, plotH + 8);
      ctx.fillStyle = palette.text; ctx.textAlign = 'left'; ctx.font = '12px ui-sans-serif, system-ui, sans-serif'; ctx.fillText(`${selected.t} UTC - selected 5m footprint`, ladderLeft + 8, 13);
      const col = { price: ladderLeft + 8, sell: ladderLeft + ladderW * .31, buy: ladderLeft + ladderW * .48, bid: ladderLeft + ladderW * .65, ask: ladderLeft + ladderW * .79, delta: ladderLeft + ladderW * .92 };
      ctx.font = '10px ui-sans-serif, system-ui, sans-serif'; ctx.fillStyle = palette.muted; ctx.textAlign = 'left'; ctx.fillText('price', col.price, top - 15); ctx.textAlign = 'right'; ctx.fillText('sell', col.sell, top - 15); ctx.fillText('buy', col.buy, top - 15); ctx.fillText('F.bid', col.bid, top - 15); ctx.fillText('F.ask', col.ask, top - 15); ctx.fillText('d', col.delta, top - 15);
      const rows = [], selectedCells = selected.cells || [];
      for (let r = 0; r < ladderRows; r++) { const tickHi = ladderHigh - r * 2, tickLo = tickHi - 2; let buy = 0, sell = 0, fillBid = 0, fillAsk = 0; for (const cell of selectedCells) if (cell[0] >= tickLo && cell[0] < tickHi) { buy += cell[1]; sell += cell[2]; fillBid += cell[3] || 0; fillAsk += cell[4] || 0; } rows.push({ tick: tickHi - 1, buy, sell, fillBid, fillAsk }); }
      const maxCell = Math.max(1, ...rows.flatMap(r => [r.buy, r.sell, r.fillBid, r.fillAsk]));
      rows.forEach((row, r) => { const y = top + r * ladderRowH, mid = y + ladderRowH / 2, d = row.buy - row.sell; if (row.sell) { ctx.fillStyle = 'rgba(251,113,133,.22)'; ctx.fillRect(col.sell - Math.min(75, row.sell / maxCell * 75), y + 2, Math.min(75, row.sell / maxCell * 75), ladderRowH - 4); } if (row.buy) { ctx.fillStyle = 'rgba(54,211,153,.22)'; ctx.fillRect(col.buy, y + 2, Math.min(75, row.buy / maxCell * 75), ladderRowH - 4); } ctx.strokeStyle = r % 4 === 0 ? '#344451' : '#1b2731'; ctx.beginPath(); ctx.moveTo(ladderLeft, y); ctx.lineTo(ladderLeft + ladderW, y); ctx.stroke(); ctx.font = '10px ui-sans-serif, system-ui, sans-serif'; ctx.textAlign = 'left'; ctx.fillStyle = palette.text; ctx.fillText(tickPrice(row.tick), col.price, mid); ctx.textAlign = 'right'; ctx.fillStyle = row.sell ? palette.sell : palette.muted; ctx.fillText(row.sell ? fmt(row.sell) : '.', col.sell, mid); ctx.fillStyle = row.buy ? palette.buy : palette.muted; ctx.fillText(row.buy ? fmt(row.buy) : '.', col.buy, mid); ctx.fillStyle = row.fillBid ? '#b5f5d8' : palette.muted; ctx.fillText(row.fillBid ? fmt(row.fillBid) : '.', col.bid, mid); ctx.fillStyle = row.fillAsk ? '#ffc2cb' : palette.muted; ctx.fillText(row.fillAsk ? fmt(row.fillAsk) : '.', col.ask, mid); ctx.fillStyle = d >= 0 ? palette.buy : palette.sell; ctx.fillText(d ? fmt(d) : '.', col.delta, mid); });
      focusLabel.value = `${selected.t} UTC - ${selected.close?.toFixed(2) ?? '--'}`; readout.innerHTML = `<strong>${selected.t} UTC</strong><span>O ${selected.open?.toFixed(2) ?? '--'} · H ${selected.high?.toFixed(2) ?? '--'} · L ${selected.low?.toFixed(2) ?? '--'} · C ${selected.close?.toFixed(2) ?? '--'}</span><span>d ${fmt(selected.delta)} · buy ${fmt(selected.buy)} · sell ${fmt(selected.sell)} · trades ${fmt(selected.trades)}</span><span>bubbles: ${ratio.value}:1 · min ${minPrint.value} · passive ${passive.value === '0' ? 'off' : passive.value}</span>`;
    }

    function draw() { if (view.value === 'ladder') drawLadder(); else drawGrid(); }

    function inspect(event) {
      if (!geom) return;
      const rect = canvas.getBoundingClientRect();
      const x = event.clientX - rect.left, y = event.clientY - rect.top;
      if (geom.mode === 'ladder') {
        if (x >= geom.ladderLeft && x <= geom.ladderLeft + geom.ladderW && y >= geom.top && y <= geom.top + geom.plotH) {
          const row = Math.max(0, Math.min(geom.ladderRows - 1, Math.floor((y - geom.top) / geom.ladderRowH)));
          const tick = geom.ladderHigh - (row + .5) * 2, bar = bars[Number(focus.value)];
          let buy = 0, sell = 0, bid = 0, ask = 0;
          for (const c of bar.cells || []) if (Math.abs(c[0] - tick) < 2) { buy += c[1]; sell += c[2]; bid += c[3] || 0; ask += c[4] || 0; }
          detail.textContent = `${bar.t} UTC · price ${tickPrice(Math.round(tick))} · sell ${fmt(sell)} · buy ${fmt(buy)} · F.bid ${fmt(bid)} · F.ask ${fmt(ask)} · delta ${fmt(buy - sell)}`;
        }
        return;
      }
      if (x < geom.left || x > geom.left + geom.plotW || y < geom.top || y > geom.top + geom.plotH) return;
      const col = Math.max(0, Math.min(geom.shown.length - 1, Math.floor((x - geom.left) / geom.colW)));
      const row = Math.max(0, Math.min(71, Math.floor((y - geom.top) / geom.rowH)));
      const tick = geom.high - (row + .5) * geom.step;
      const bar = geom.shown[col];
      let buy = 0, sell = 0, bid = 0, ask = 0;
      for (const c of bar.cells) if (Math.abs(c[0] - tick) < geom.step) { buy += c[1]; sell += c[2]; }
      for (const c of bar.heat) if (Math.abs(c[0] - tick) < geom.step) { bid = Math.max(bid, c[1]); ask = Math.max(ask, c[2]); }
      detail.textContent = `${bar.t} UTC · price ${tickPrice(Math.round(tick))} · sell ${fmt(sell)} · buy ${fmt(buy)} · delta ${fmt(buy - sell)} · observed bid depth ${fmt(bid)} · ask depth ${fmt(ask)}`;
    }

    focus.addEventListener('input', draw); view.addEventListener('change', draw); ratio.addEventListener('change', draw); minPrint.addEventListener('change', draw); passive.addEventListener('change', draw); canvas.addEventListener('mousemove', inspect); window.addEventListener('resize', resize); resize();
  })();
  </script>
</div>'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dbn", type=Path, required=True, help="path to the .mbo.dbn.zst file")
    parser.add_argument("--date", default=DEMO_DATE, help="UTC trade date in YYYY-MM-DD format")
    parser.add_argument("--output", type=Path, help="output HTML path (defaults to orderflow_<date>.html)")
    args = parser.parse_args()
    output = args.output or Path(__file__).with_name(f"orderflow_{args.date}.html")
    payload = build(args.dbn, output, args.date)
    print(json.dumps({"output": str(output), "bars": len(payload["bars"]), "records": payload["meta"]["source_records"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
