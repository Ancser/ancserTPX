"""1.0.8 研究:波動率能不能「提前」知道?用成交量預測當天體制。

回答:按前一天預測(lag-1 持續性)還是當天早盤(ASIA 開盤頭 2 小時)測?

區塊:
  P1. 日波動/量的 lag-1 自相關:前一天 RV/量 能不能預測今天?(Pearson)
  P2. 前一天 RV 三分位 → 今天 RV(持續性表格)。
  P3. 早盤前 2 小時(ASIA 開盤,22:00 UTC 起)量/RV → 全天 RV 相關性
      (當天早盤能不能提前判斷今天會不會是快市)。
  P4. 可操作性:前一天 RV / 早盤量 三分位 → 回測 & 實盤當天 PnL
      (yesterday 高波動 = 今天壞交易日嗎?→ 能否當進場過濾)。

Run:  PYTHONIOENCODING=utf-8 python -m scripts.vol_predict_study
"""
from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.backtest.engine import BacktestEngine, _topstep_trade_date
from backend.data import candle_store
from backend.db.models import (
    BacktestConfig, _extract_symbol, get_commission_rt, get_fees_rt,
)
from backend.terminal_live import BUILTIN_PRESETS, CODEX_630_PRESET_3, _build_strategy_params

TRADES_FILE = Path("data/trades.json")
NEW_LOGIC_START = datetime(2026, 6, 25, tzinfo=timezone.utc)
EARLY_MIN = 120  # ASIA 開盤頭 2 小時


def pearson(xs, ys):
    pts = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pts) < 3:
        return float("nan")
    xs, ys = zip(*pts)
    mx, my = statistics.mean(xs), statistics.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in pts)
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return num / (dx * dy) if dx and dy else float("nan")


def build_daily(candles):
    by = defaultdict(list)
    for c in candles:
        by[_topstep_trade_date(c.timestamp)].append(c)
    daily = {}
    for d, cs in by.items():
        cs.sort(key=lambda c: c.timestamp)
        closes = [c.close for c in cs]
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))
                if closes[i - 1] > 0 and closes[i] > 0]
        early = cs[:EARLY_MIN]
        e_closes = [c.close for c in early]
        e_rets = [math.log(e_closes[i] / e_closes[i - 1]) for i in range(1, len(e_closes))
                  if e_closes[i - 1] > 0 and e_closes[i] > 0]
        daily[d] = {
            "vol": sum(c.volume for c in cs),
            "rv": statistics.pstdev(rets) if len(rets) > 1 else 0.0,
            "range": sum(c.high - c.low for c in cs),
            "early_vol": sum(c.volume for c in early),
            "early_rv": statistics.pstdev(e_rets) if len(e_rets) > 1 else 0.0,
            "n": len(cs),
        }
    return daily


def tercile_split(dates, key, daily):
    vals = sorted(daily[d][key] for d in dates if daily.get(d))
    if len(vals) < 3:
        return None
    lo = vals[len(vals) // 3]
    hi = vals[2 * len(vals) // 3]
    return lo, hi


def main():
    candles = candle_store.load("MNQ", 1)
    candles.sort(key=lambda c: c.timestamp)
    daily = build_daily(candles)
    dates = sorted(daily.keys())
    prev = {d: (dates[i - 1] if i > 0 else None) for i, d in enumerate(dates)}
    print(f"trade-days: {len(dates)}  {dates[0]} -> {dates[-1]}", flush=True)

    # P1 lag-1 autocorrelation
    print("\n== P1. lag-1 自相關(前一天 → 今天)==", flush=True)
    for key, lab in (("rv", "realized vol"), ("vol", "total volume"), ("range", "range sum")):
        today = [daily[d][key] for d in dates if prev[d]]
        yday = [daily[prev[d]][key] for d in dates if prev[d]]
        print(f"  {lab:<14} corr(prev, today) = {pearson(yday, today):+.3f}", flush=True)
    # cross: prev vol -> today rv
    t_rv = [daily[d]["rv"] for d in dates if prev[d]]
    y_vol = [daily[prev[d]]["vol"] for d in dates if prev[d]]
    print(f"  {'cross':<14} corr(prev VOL, today RV) = {pearson(y_vol, t_rv):+.3f}", flush=True)

    # P2 prev-day RV tercile -> today RV
    print("\n== P2. 前一天 RV 三分位 → 今天 RV ==", flush=True)
    split = tercile_split(dates, "rv", daily)
    if split:
        lo, hi = split
        buckets = {"low": [], "mid": [], "high": []}
        for d in dates:
            p = prev[d]
            if not p or not daily.get(p):
                continue
            pv = daily[p]["rv"]
            b = "low" if pv <= lo else ("high" if pv > hi else "mid")
            buckets[b].append(daily[d]["rv"])
        for b in ("low", "mid", "high"):
            xs = buckets[b]
            if xs:
                print(f"  前一天 RV {b:<4}  n={len(xs):>3}  今天 RV mean={100*statistics.mean(xs):.3f}%", flush=True)

    # P3 early session -> full day
    print(f"\n== P3. 早盤前 {EARLY_MIN}min → 全天 RV 相關性 ==", flush=True)
    fd_rv = [daily[d]["rv"] for d in dates]
    e_rv = [daily[d]["early_rv"] for d in dates]
    e_vol = [daily[d]["early_vol"] for d in dates]
    print(f"  corr(早盤 RV, 全天 RV)  = {pearson(e_rv, fd_rv):+.3f}", flush=True)
    print(f"  corr(早盤 量, 全天 RV)  = {pearson(e_vol, fd_rv):+.3f}", flush=True)
    # compare predictive power: prev-day rv vs early-rv for today's full rv
    y_rv = [daily[prev[d]]["rv"] if prev[d] else None for d in dates]
    print(f"  對照:corr(前一天 RV, 全天 RV) = {pearson(y_rv, fd_rv):+.3f}", flush=True)

    # P4 actionable: predictor tercile -> daily PnL (backtest + live)
    print("\n== P4. 預測分位 → 當天交易 PnL(可否當過濾)==", flush=True)
    preset = BUILTIN_PRESETS[CODEX_630_PRESET_3]
    base = _build_strategy_params(preset, preset.get("contract_id", "CON.F.US.MNQ.U26"))
    cid = base.contract_id
    config = BacktestConfig(
        strategies=["trend"], initial_capital=50_000.0, symbol=_extract_symbol(cid),
        commission_rt=get_commission_rt(cid), fees_rt=get_fees_rt(cid),
        value_area_pct=float(getattr(base, "value_area_pct", 0.80)),
    )
    bt = BacktestEngine(config=config, strategy_params=base,
                        zone_timeline=None, record_equity=False).run(candles).trades
    bt_day = defaultdict(float)
    for t in bt:
        if t.exit_time:
            bt_day[_topstep_trade_date(t.entry_time)] += (t.pnl or 0.0)

    # live trend daily pnl
    live_day = defaultdict(float)
    try:
        recs = json.load(open(TRADES_FILE, encoding="utf-8"))
        seen = set()
        for r in recs:
            if r.get("strategy") != "trend" or r.get("shadow"):
                continue
            et = r.get("entry_time") or ""
            key = (et[:19], r.get("entry_price"), r.get("direction"))
            if key in seen or not et:
                continue
            seen.add(key)
            dt = datetime.fromisoformat(et)
            dt = dt.replace(tzinfo=timezone.utc) if not dt.tzinfo else dt.astimezone(timezone.utc)
            live_day[_topstep_trade_date(dt)] += float(r.get("topstep_pnl") or 0.0)
    except Exception as e:
        print("  (live load failed:", e, ")", flush=True)

    def pnl_by_tercile(day_pnl, pred_key, use_prev):
        split = tercile_split(dates, pred_key, daily)
        if not split:
            return
        lo, hi = split
        buckets = {"low": [], "mid": [], "high": []}
        for d, pnl in day_pnl.items():
            src = prev.get(d) if use_prev else d
            if not src or not daily.get(src):
                continue
            pv = daily[src][pred_key]
            b = "low" if pv <= lo else ("high" if pv > hi else "mid")
            buckets[b].append(pnl)
        for b in ("low", "mid", "high"):
            xs = buckets[b]
            if xs:
                wr = 100 * sum(1 for x in xs if x > 0) / len(xs)
                print(f"    {b:<4} n={len(xs):>3}  總pnl={sum(xs):>+9.0f}  平均/日={statistics.mean(xs):>+8.1f}  獲利日%={wr:.0f}", flush=True)

    print("  [回測] 前一天 RV 分位 → 當天 pnl:", flush=True)
    pnl_by_tercile(bt_day, "rv", use_prev=True)
    print("  [回測] 早盤量 分位 → 當天 pnl:", flush=True)
    pnl_by_tercile(bt_day, "early_vol", use_prev=False)
    if live_day:
        print("  [實盤] 前一天 RV 分位 → 當天 pnl:", flush=True)
        pnl_by_tercile(live_day, "rv", use_prev=True)
        print("  [實盤] 早盤量 分位 → 當天 pnl:", flush=True)
        pnl_by_tercile(live_day, "early_vol", use_prev=False)


if __name__ == "__main__":
    main()
