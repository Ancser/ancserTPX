"""Build a two-session, scrollable order-flow research chart.

This stays in ``research/orderflow_demo`` and deliberately does not import the
production TPX runtime.  It reuses the already-built offline day payloads,
normalises their timestamps/indexes, then renders the same ATAS-style views in
a TPX-like viewport: drag to pan, wheel to pan/zoom, and redraw only the
visible bars.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path


DATA_RE = re.compile(r"const DATA = (\{.*?\});\s*const root", re.S)


def load_day(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    match = DATA_RE.search(text)
    if not match:
        raise ValueError(f"cannot find embedded DATA payload in {path}")
    return json.loads(match.group(1))


def _epoch(date: str, hhmm: str) -> int:
    value = datetime.strptime(f"{date} {hhmm}", "%Y-%m-%d %H:%M")
    return int(value.replace(tzinfo=timezone.utc).timestamp())


def combine(paths: list[Path]) -> dict:
    all_bars: list[dict] = []
    all_minutes: list[dict] = []
    dates: list[str] = []
    record_count = 0

    for day_no, path in enumerate(paths):
        data = load_day(path)
        date = str(data.get("meta", {}).get("date") or path.stem[-10:])
        dates.append(date)
        bar_offset = len(all_bars)
        minute_offset = len(all_minutes)
        record_count += int(data.get("meta", {}).get("source_records") or 0)

        for old in data.get("bars", []):
            bar = dict(old)
            old_i = int(old.get("i", 0))
            bar["i"] = bar_offset + old_i
            bar["day"] = day_no
            bar["date"] = date
            bar["time"] = _epoch(date, str(old.get("t", "00:00")))
            bar["label"] = f"{date} {old.get('t', '')}"
            bar["minuteStart"] = minute_offset + old_i * 5
            all_bars.append(bar)

        for old in data.get("minutes", []):
            minute = dict(old)
            minute["i"] = minute_offset + int(old.get("i", 0))
            minute["day"] = day_no
            minute["date"] = date
            minute["time"] = _epoch(date, str(old.get("t", "00:00")))
            all_minutes.append(minute)

    return {
        "meta": {
            "dates": dates,
            "symbol": "MNQU6",
            "dataset": "GLBX.MDP3",
            "schema": "mbo",
            "timezone": "UTC display; RTH = 06:30–13:00 America/Los_Angeles",
            "bar_minutes": 5,
            "rth_bars": len(all_bars),
            "rth_minutes": len(all_minutes),
            "source_records": record_count,
            "source_files": [p.name for p in paths],
        },
        "bars": all_bars,
        "minutes": all_minutes,
    }


HTML_TEMPLATE = r'''<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MNQ orderflow · 2026-09-01 + 2026-09-02</title>
<style>
:root { color-scheme: dark; --bg:#080c12; --panel:#111923; --line:#263746; --text:#e6edf3; --muted:#91a0ad; --buy:#36d399; --sell:#fb7185; --heat:#f4b860; --focus:#7dd3fc; }
* { box-sizing:border-box; }
html,body { margin:0; min-height:100%; background:var(--bg); color:var(--text); font:13px ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }
#app { max-width:1440px; margin:0 auto; padding:16px; }
h1 { margin:0 0 4px; font-size:20px; font-weight:500; letter-spacing:.2px; }
.sub { margin:0 0 12px; color:var(--muted); font-size:12px; }
.toolbar { display:flex; flex-wrap:wrap; align-items:end; gap:10px 12px; padding:10px; border:1px solid var(--line); background:var(--panel); border-radius:6px; }
label { display:flex; flex-direction:column; gap:4px; color:var(--muted); font-size:11px; }
select,button,input[type=range] { color:var(--text); background:#182330; border:1px solid #3a4b5a; border-radius:4px; padding:6px 8px; }
button { cursor:pointer; }
button:hover { border-color:var(--focus); }
input[type=range] { width:min(340px,48vw); padding:0; }
.readout { display:flex; flex-wrap:wrap; gap:6px 18px; min-height:27px; padding:9px 2px 7px; color:var(--muted); }
.readout strong { color:var(--text); font-weight:500; }
.viewport { position:relative; border:1px solid var(--line); background:#0b1118; border-radius:5px; overflow:hidden; }
canvas { display:block; width:100%; height:700px; touch-action:none; cursor:grab; }
canvas.dragging { cursor:grabbing; }
.detail { min-height:24px; padding:8px 2px 0; color:var(--muted); font-size:12px; }
.legend { display:flex; flex-wrap:wrap; gap:10px 16px; padding-top:7px; color:var(--muted); font-size:11px; }
.swatch { display:inline-block; width:10px; height:10px; margin-right:4px; border-radius:2px; vertical-align:-1px; }
.day { background:#9b6cf6; } .buy { background:var(--buy); } .sell { background:var(--sell); } .heat { background:var(--heat); }
.hint { margin-top:8px; color:#7f909f; font-size:11px; }
@media (max-width:700px) { #app { padding:10px; } canvas { height:610px; } .toolbar { gap:8px; } }
</style>
</head>
<body>
<main id="app">
  <h1>MNQ MBO footprint · 2026-09-01 + 2026-09-02</h1>
  <p class="sub">研究專用合併圖。視窗／拖曳／滾輪邏輯仿 ancserTPX chart；跨日只畫分隔線，不把兩天的成交連成一條。</p>
  <div class="toolbar">
    <label>View<select id="view"><option value="ladder" selected>ATAS ladder</option><option value="footprint">Footprint grid</option><option value="heatmap">Liquidity heatmap</option></select></label>
    <label>Imbalance<select id="ratio"><option value="3">3:1</option><option value="5">5:1</option><option value="10" selected>10:1</option><option value="20">20:1</option></select></label>
    <label>Min print<select id="min"><option value="50">50</option><option value="100" selected>100</option><option value="150">150</option><option value="200">200</option></select></label>
    <label>Passive fill<select id="passive"><option value="0">off</option><option value="50">50</option><option value="100" selected>100</option><option value="150">150</option></select></label>
    <label>Focus bar <output id="focusLabel">—</output><input id="focus" type="range" min="0" max="155" value="77" step="1"></label>
    <button id="day1">9/1</button><button id="day2">9/2</button><button id="all">兩天</button><button id="latest">跳到最新</button>
  </div>
  <div class="readout" id="readout"></div>
  <div class="viewport"><canvas id="canvas" aria-label="MNQ combined orderflow chart"></canvas></div>
  <div class="detail" id="detail">拖曳圖表左右移動；滾輪上下移動，按 Ctrl+滾輪縮放。移到 ladder 或價格格可讀取數值。</div>
  <div class="legend"><span><i class="swatch sell"></i>sell aggressor</span><span><i class="swatch buy"></i>buy aggressor</span><span><i class="swatch heat"></i>displayed liquidity</span><span><i class="swatch day"></i>session divider</span><span>F.bid/F.ask = matched passive fills</span><span>delta = buy − sell</span></div>
  <div class="hint">資料：兩個已購買的 CME MBO 日檔；只渲染目前可見的 5m bars，避免左移時重新計算整份資料。</div>
</main>
<script>
(() => {
  const DATA = __DATA__;
  const bars = DATA.bars || [], minutes = DATA.minutes || [];
  const canvas = document.getElementById('canvas'), ctx = canvas.getContext('2d');
  const view = document.getElementById('view'), ratio = document.getElementById('ratio'), minPrint = document.getElementById('min'), passive = document.getElementById('passive');
  const focus = document.getElementById('focus'), focusLabel = document.getElementById('focusLabel'), readout = document.getElementById('readout'), detail = document.getElementById('detail');
  focus.max = Math.max(0, bars.length - 1);
  let visible = Math.min(31, Math.max(1, bars.length)), viewStart = 0, focusIndex = Math.min(77, Math.max(0, bars.length - 1)), geom = null, raf = 0, drag = null;
  focus.value = String(focusIndex);
  const palette = { bg:'#0b1118', line:'#263746', text:'#e6edf3', muted:'#91a0ad', buy:'#36d399', sell:'#fb7185', heat:'#f4b860', focus:'#7dd3fc', day:'#9b6cf6' };
  const fmt = (n) => Math.round(Number(n)||0).toLocaleString();
  const tickPrice = (t) => (Number(t) * .25).toFixed(2);
  const alpha = (v,max,floor=.08) => floor + Math.min(.75, Math.log1p(Math.max(0,v))/Math.log1p(Math.max(1,max))*.67);
  const clamp = (n,lo,hi) => Math.max(lo,Math.min(hi,n));
  function rangeStart(center=focusIndex) { return clamp(Math.round(center - visible/2), 0, Math.max(0,bars.length-visible)); }
  function schedule() { if (raf) return; raf = requestAnimationFrame(() => { raf=0; draw(); }); }
  function bubbleKind(cell) {
    const buy=Number(cell[1]||0), sell=Number(cell[2]||0), fillBid=Number(cell[3]||0), fillAsk=Number(cell[4]||0), r=Number(ratio.value), m=Number(minPrint.value), p=Number(passive.value);
    if (buy>=m && buy>=r*Math.max(1,sell) && (p===0 || fillAsk>=p)) return {side:'buy',total:buy,passive:fillAsk};
    if (sell>=m && sell>=r*Math.max(1,buy) && (p===0 || fillBid>=p)) return {side:'sell',total:sell,passive:fillBid};
    return null;
  }
  function bubblesForBar(bar) {
    const out=[], start=Number(bar.minuteStart||0);
    for (const minute of minutes.slice(start,start+5)) for (const cell of (minute.cells||[])) { const kind=bubbleKind(cell); if(kind) out.push({...kind,tick:Number(cell[0]),minute:Number(minute.i)}); }
    return out;
  }
  function drawBubble(x,y,event,scale=.30) {
    const radius=Math.min(15,4+Math.sqrt(event.total)*scale), green=event.side==='buy';
    ctx.beginPath(); ctx.arc(x,y,radius,0,Math.PI*2); ctx.fillStyle=green?'rgba(54,211,153,.78)':'rgba(251,113,133,.78)'; ctx.fill(); ctx.strokeStyle=green?'rgba(151,255,211,.95)':'rgba(255,180,194,.95)'; ctx.stroke();
    if(radius>=8){ctx.fillStyle=palette.text;ctx.font='10px ui-sans-serif';ctx.textAlign='center';ctx.fillText(fmt(event.total),x,y+1);}
  }
  function setupCanvas(){const dpr=window.devicePixelRatio||1, r=canvas.getBoundingClientRect();canvas.width=Math.max(1,Math.floor(r.width*dpr));canvas.height=Math.max(1,Math.floor(r.height*dpr));ctx.setTransform(dpr,0,0,dpr,0,0);schedule();}
  function drawDivider(x, label, top, height){ctx.save();ctx.strokeStyle='rgba(155,108,246,.95)';ctx.setLineDash([5,4]);ctx.lineWidth=1.5;ctx.beginPath();ctx.moveTo(x,top);ctx.lineTo(x,top+height);ctx.stroke();ctx.setLineDash([]);ctx.fillStyle='rgba(198,174,255,.95)';ctx.font='11px ui-sans-serif';ctx.textAlign='left';ctx.fillText(label,x+4,top+9);ctx.restore();}
  function common(){const w=canvas.clientWidth,h=canvas.clientHeight, start=rangeStart(), shown=bars.slice(start,start+visible), selected=bars[focusIndex]||shown[0];ctx.clearRect(0,0,w,h);return {w,h,start,shown,selected};}
  function drawGrid(){
    const {w,h,start,shown,selected}=common(), rows=72, step=2,left=62,right=8,top=30,bottom=88,plotW=Math.max(80,w-left-right),plotH=Math.max(120,h-top-bottom),colW=plotW/Math.max(1,shown.length),centerTick=Math.round((selected.close||selected.open||0)/.25),low=centerTick-Math.floor(rows*step/2),high=low+rows*step;
    geom={mode:'grid',start,shown,left,top,plotW,plotH,colW,rows,step,high};ctx.fillStyle=palette.bg;ctx.fillRect(0,0,w,h);ctx.font='11px ui-sans-serif';ctx.textBaseline='middle';ctx.strokeStyle=palette.line;ctx.lineWidth=1;
    for(let r=0;r<=rows;r+=4){const y=top+r*plotH/rows;ctx.beginPath();ctx.moveTo(left,y);ctx.lineTo(left+plotW,y);ctx.stroke();ctx.fillStyle=palette.muted;ctx.textAlign='right';ctx.fillText(tickPrice(high-r*step),left-7,y+1);}
    const maxHeat=Math.max(1,...shown.flatMap(b=>(b.heat||[]).map(v=>Math.max(v[1],v[2])))),maxVol=Math.max(1,...shown.flatMap(b=>(b.cells||[]).map(v=>Math.max(v[1],v[2]))));
    for(let c=0;c<shown.length;c++){const bar=shown[c],x=left+c*colW,cells=new Map((bar.cells||[]).map(v=>[v[0],v])),heat=new Map((bar.heat||[]).map(v=>[v[0],v]));
      if(c>0&&bar.date!==shown[c-1].date)drawDivider(x,bar.date,top,plotH+bottom-20);
      if(c%3===0||bar.i===focusIndex){ctx.fillStyle=palette.muted;ctx.textAlign='center';ctx.fillText(bar.label.slice(5,16),x+colW/2,top-14);}
      for(let r=0;r<rows;r++){const lo=high-(r+1)*step,hi=high-r*step;let buy=0,sell=0,bid=0,ask=0;for(let t=lo;t<hi;t++){const cell=cells.get(t);if(cell){buy+=cell[1];sell+=cell[2];}const dep=heat.get(t);if(dep){bid=Math.max(bid,dep[1]);ask=Math.max(ask,dep[2]);}}const y=top+r*plotH/rows;
        if(view.value==='heatmap'){if(bid){ctx.fillStyle=`rgba(54,211,153,${alpha(bid,maxHeat)})`;ctx.fillRect(x+1,y+1,colW/2-1,plotH/rows-2);}if(ask){ctx.fillStyle=`rgba(251,113,133,${alpha(ask,maxHeat)})`;ctx.fillRect(x+colW/2,y+1,colW/2-1,plotH/rows-2);}}
        else if(buy||sell){const a=alpha(Math.max(buy,sell),maxVol,.12);if(sell){ctx.fillStyle=`rgba(251,113,133,${a})`;ctx.fillRect(x+1,y+1,colW/2-1,plotH/rows-2);}if(buy){ctx.fillStyle=`rgba(54,211,153,${a})`;ctx.fillRect(x+colW/2,y+1,colW/2-1,plotH/rows-2);}if(plotH/rows>=8&&colW>=22){ctx.font='10px ui-sans-serif';ctx.fillStyle=palette.text;ctx.textAlign='center';ctx.fillText(sell?fmt(sell):'·',x+colW*.25,y+plotH/rows/2);ctx.fillText(buy?fmt(buy):'·',x+colW*.75,y+plotH/rows/2);}}
      }
      for(const event of bubblesForBar(bar)){const row=Math.floor((high-event.tick)/step);if(row<0||row>=rows)continue;drawBubble(x+((event.minute-(bar.minuteStart||0))+.5)/5*colW,top+(row+.5)*plotH/rows,event);}
      if(bar.i===focusIndex){ctx.strokeStyle=palette.focus;ctx.lineWidth=2;ctx.strokeRect(x+1,top+1,colW-2,plotH-2);ctx.lineWidth=1;}
    }
    drawDelta(shown,left,top+plotH+18,plotW,bottom-30,colW);updateReadout(selected);
  }
  function drawDelta(shown,left,top,width,height,colW){const max=Math.max(1,...shown.map(b=>Math.abs(b.delta||0)));ctx.strokeStyle=palette.line;ctx.beginPath();ctx.moveTo(left,top+height/2);ctx.lineTo(left+width,top+height/2);ctx.stroke();for(let c=0;c<shown.length;c++){const b=shown[c],bh=Math.min(1,Math.abs(b.delta||0)/max)*(height/2-2);ctx.fillStyle=(b.delta||0)>=0?palette.buy:palette.sell;ctx.globalAlpha=.72;ctx.fillRect(left+c*colW+2,(b.delta||0)>=0?top+height/2-bh:top+height/2,Math.max(2,colW-4),bh);ctx.globalAlpha=1;}ctx.fillStyle=palette.muted;ctx.textAlign='left';ctx.fillText('delta',8,top+height/2);}
  function drawLadder(){
    const {w,h,start,shown,selected}=common(),left=48,top=38,bottom=96,gap=16,chartW=Math.max(240,Math.min(w*.66,w-300)),ladderLeft=chartW+gap,ladderW=Math.max(220,w-ladderLeft-8),plotH=Math.max(140,h-top-bottom),colW=chartW/Math.max(1,shown.length),lows=shown.filter(b=>b.low!=null).map(b=>b.low),highs=shown.filter(b=>b.high!=null).map(b=>b.high),lo=Math.min(...lows),hi=Math.max(...highs),pad=Math.max(1,(hi-lo)*.06),chartLo=lo-pad,chartHi=hi+pad,range=Math.max(.01,chartHi-chartLo),center=Math.round((selected.close||selected.open||lo)/.25),ladderLow=Math.floor((center-22)/2)*2,ladderRows=44,ladderHigh=ladderLow+ladderRows*2,rowH=plotH/ladderRows;
    geom={mode:'ladder',start,shown,left,top,chartW,plotH,colW,chartLo,chartHi,ladderLeft,ladderW,ladderLow,ladderHigh,ladderRows,rowH};ctx.fillStyle=palette.bg;ctx.fillRect(0,0,w,h);ctx.font='11px ui-sans-serif';ctx.textBaseline='middle';const yPrice=p=>top+(chartHi-p)/range*plotH;
    for(let k=0;k<=6;k++){const y=top+k*plotH/6;ctx.strokeStyle=palette.line;ctx.beginPath();ctx.moveTo(left,y);ctx.lineTo(left+chartW,y);ctx.stroke();ctx.fillStyle=palette.muted;ctx.textAlign='right';ctx.fillText((chartHi-k*range/6).toFixed(2),left-7,y);}
    for(let c=0;c<shown.length;c++){const b=shown[c],x=left+c*colW+colW/2;if(c>0&&b.date!==shown[c-1].date)drawDivider(left+c*colW,b.date,top,plotH+bottom-20);if(b.high!=null){const up=(b.close??0)>=(b.open??0);ctx.strokeStyle=up?palette.buy:palette.sell;ctx.beginPath();ctx.moveTo(x,yPrice(b.high));ctx.lineTo(x,yPrice(b.low));ctx.stroke();ctx.fillStyle=up?palette.buy:palette.sell;const bt=yPrice(Math.max(b.open,b.close)),bb=yPrice(Math.min(b.open,b.close));ctx.fillRect(Math.max(left+1,x-Math.max(2,colW*.28)),bt,Math.max(3,colW*.56),Math.max(1,bb-bt));}if(c%3===0||b.i===focusIndex){ctx.fillStyle=palette.muted;ctx.textAlign='center';ctx.fillText(b.label.slice(5,16),x,top-16);}if(b.i===focusIndex){ctx.strokeStyle=palette.focus;ctx.lineWidth=2;ctx.strokeRect(left+c*colW+1,top,colW-2,plotH);ctx.lineWidth=1;}for(const e of bubblesForBar(b)){const yy=yPrice(e.tick*.25);if(yy>=top&&yy<=top+plotH)drawBubble(left+c*colW+((e.minute-(b.minuteStart||0))+.5)/5*colW,yy,e,.30);}}
    drawDelta(shown,left,top+plotH+18,chartW,bottom-30,colW);ctx.fillStyle='#111a23';ctx.fillRect(ladderLeft,top-4,ladderW,plotH+8);ctx.strokeStyle=palette.focus;ctx.strokeRect(ladderLeft,top-4,ladderW,plotH+8);ctx.fillStyle=palette.text;ctx.textAlign='left';ctx.font='12px ui-sans-serif';ctx.fillText(`${selected.label} · selected 5m footprint`,ladderLeft+8,16);
    const col={price:ladderLeft+8,sell:ladderLeft+ladderW*.31,buy:ladderLeft+ladderW*.48,bid:ladderLeft+ladderW*.65,ask:ladderLeft+ladderW*.79,delta:ladderLeft+ladderW*.92};ctx.font='10px ui-sans-serif';ctx.fillStyle=palette.muted;ctx.textAlign='left';ctx.fillText('price',col.price,top-15);ctx.textAlign='right';['sell','buy','F.bid','F.ask','d'].forEach((v,i)=>ctx.fillText(v,[col.sell,col.buy,col.bid,col.ask,col.delta][i],top-15));
    const rows=[], cells=selected.cells||[];for(let r=0;r<ladderRows;r++){const hiTick=ladderHigh-r*2,loTick=hiTick-2;let buy=0,sell=0,bid=0,ask=0;for(const cell of cells)if(cell[0]>=loTick&&cell[0]<hiTick){buy+=cell[1];sell+=cell[2];bid+=cell[3]||0;ask+=cell[4]||0;}rows.push({tick:hiTick-1,buy,sell,bid,ask});}const maxCell=Math.max(1,...rows.flatMap(r=>[r.buy,r.sell,r.bid,r.ask]));
    rows.forEach((r,i)=>{const y=top+i*rowH,mid=y+rowH/2,d=r.buy-r.sell;if(r.sell){ctx.fillStyle='rgba(251,113,133,.22)';ctx.fillRect(col.sell-Math.min(75,r.sell/maxCell*75),y+2,Math.min(75,r.sell/maxCell*75),rowH-4);}if(r.buy){ctx.fillStyle='rgba(54,211,153,.22)';ctx.fillRect(col.buy,y+2,Math.min(75,r.buy/maxCell*75),rowH-4);}ctx.strokeStyle=i%4===0?'#344451':'#1b2731';ctx.beginPath();ctx.moveTo(ladderLeft,y);ctx.lineTo(ladderLeft+ladderW,y);ctx.stroke();ctx.font='10px ui-sans-serif';ctx.textAlign='left';ctx.fillStyle=palette.text;ctx.fillText(tickPrice(r.tick),col.price,mid);ctx.textAlign='right';ctx.fillStyle=r.sell?palette.sell:palette.muted;ctx.fillText(r.sell?fmt(r.sell):'.',col.sell,mid);ctx.fillStyle=r.buy?palette.buy:palette.muted;ctx.fillText(r.buy?fmt(r.buy):'.',col.buy,mid);ctx.fillStyle=r.bid?'#b5f5d8':palette.muted;ctx.fillText(r.bid?fmt(r.bid):'.',col.bid,mid);ctx.fillStyle=r.ask?'#ffc2cb':palette.muted;ctx.fillText(r.ask?fmt(r.ask):'.',col.ask,mid);ctx.fillStyle=d>=0?palette.buy:palette.sell;ctx.fillText(d?fmt(d):'.',col.delta,mid);});updateReadout(selected);
  }
  function updateReadout(selected){if(!selected)return;focusLabel.value=`${selected.label} · ${Number(selected.close||0).toFixed(2)}`;readout.innerHTML=`<strong>${selected.label} UTC</strong><span>O ${selected.open?.toFixed(2)??'—'} · H ${selected.high?.toFixed(2)??'—'} · L ${selected.low?.toFixed(2)??'—'} · C ${selected.close?.toFixed(2)??'—'}</span><span>Δ ${fmt(selected.delta)} · buy ${fmt(selected.buy)} · sell ${fmt(selected.sell)} · trades ${fmt(selected.trades)}</span><span>adds ${fmt(selected.add_qty)} · cancels ${fmt(selected.cancel_qty)} · bubbles ${ratio.value}:1</span>`;}
  function draw(){if(!bars.length)return;if(view.value==='ladder')drawLadder();else drawGrid();}
  function setFocus(i){focusIndex=clamp(Math.round(i),0,bars.length-1);focus.value=String(focusIndex);viewStart=rangeStart(focusIndex);schedule();}
  function setWindow(start, count=visible){visible=clamp(Math.round(count),12,Math.max(12,bars.length));viewStart=clamp(Math.round(start),0,Math.max(0,bars.length-visible));if(focusIndex<viewStart)focusIndex=viewStart;if(focusIndex>=viewStart+visible)focusIndex=viewStart+visible-1;focus.value=String(focusIndex);schedule();}
  function pan(delta){setWindow(viewStart+delta,visible);}
  function zoom(factor, anchor=focusIndex){const next=clamp(Math.round(visible*factor),12,Math.min(78,bars.length));const ratioAnchor=visible?((anchor-viewStart)/visible):.5;const start=anchor-Math.round(ratioAnchor*next);setWindow(start,next);}
  canvas.addEventListener('wheel',e=>{e.preventDefault();if(e.ctrlKey||e.metaKey){zoom(e.deltaY>0?1.18:.84,focusIndex);}else pan(e.deltaY>0?5:-5);},{passive:false});
  canvas.addEventListener('pointerdown',e=>{drag={x:e.clientX,start:viewStart};canvas.classList.add('dragging');canvas.setPointerCapture?.(e.pointerId);});
  canvas.addEventListener('pointermove',e=>{if(!drag)return;const px=Math.max(1,canvas.clientWidth),step=px/Math.max(1,visible);const delta=Math.round((drag.x-e.clientX)/step);setWindow(drag.start+delta,visible);});
  const endDrag=()=>{drag=null;canvas.classList.remove('dragging');};canvas.addEventListener('pointerup',endDrag);canvas.addEventListener('pointercancel',endDrag);
  canvas.addEventListener('mousemove',e=>{if(!geom)return;const r=canvas.getBoundingClientRect(),x=e.clientX-r.left,y=e.clientY-r.top;if(geom.mode==='ladder'&&x>=geom.ladderLeft&&x<=geom.ladderLeft+geom.ladderW&&y>=geom.top&&y<=geom.top+geom.plotH){const row=clamp(Math.floor((y-geom.top)/geom.rowH),0,geom.ladderRows-1),tick=geom.ladderHigh-(row+.5)*2,bar=bars[focusIndex];let buy=0,sell=0,bid=0,ask=0;for(const c of bar.cells||[])if(Math.abs(c[0]-tick)<2){buy+=c[1];sell+=c[2];bid+=c[3]||0;ask+=c[4]||0;}detail.textContent=`${bar.label} · price ${tickPrice(Math.round(tick))} · sell ${fmt(sell)} · buy ${fmt(buy)} · F.bid ${fmt(bid)} · F.ask ${fmt(ask)} · delta ${fmt(buy-sell)}`;return;}detail.textContent='拖曳圖表左右移動；滾輪上下移動，按 Ctrl+滾輪縮放。';});
  focus.addEventListener('input',()=>setFocus(Number(focus.value)));[view,ratio,minPrint,passive].forEach(el=>el.addEventListener('change',schedule));
  document.getElementById('day1').onclick=()=>{const i=bars.findIndex(b=>b.day===0);setFocus(i<0?0:i);setWindow(i<0?0:i,visible);};document.getElementById('day2').onclick=()=>{const i=bars.findIndex(b=>b.day===1);setFocus(i<0?bars.length-1:i);setWindow(i<0?Math.max(0,bars.length-visible):i,visible);};document.getElementById('all').onclick=()=>setWindow(0,bars.length);document.getElementById('latest').onclick=()=>setWindow(Math.max(0,bars.length-visible),visible);
  window.addEventListener('keydown',e=>{if(e.key==='ArrowLeft'){e.preventDefault();setFocus(focusIndex-1);}else if(e.key==='ArrowRight'){e.preventDefault();setFocus(focusIndex+1);}else if(e.key==='Home'){e.preventDefault();setWindow(0,visible);}else if(e.key==='End'){e.preventDefault();setWindow(Math.max(0,bars.length-visible),visible);}});
  new ResizeObserver(setupCanvas).observe(canvas);setupCanvas();
})();
</script>
</body>
</html>
'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", action="append", type=Path, dest="days", help="existing orderflow day HTML; repeat twice")
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("preview-combined.html"))
    args = parser.parse_args()
    days = args.days or [Path(__file__).with_name("orderflow_2026-09-01.html"), Path(__file__).with_name("orderflow_2026-09-02.html")]
    if len(days) < 2:
        raise SystemExit("provide two --day HTML files")
    payload = combine(days)
    html = HTML_TEMPLATE.replace("__DATA__", json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    args.output.write_text(html, encoding="utf-8")
    print(json.dumps({"output": str(args.output), "dates": payload["meta"]["dates"], "bars": len(payload["bars"]), "minutes": len(payload["minutes"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
