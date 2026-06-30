"""
Live-fill vs backtest-fill comparison (slippage + signal alignment).

Goal: explain the live (-$) vs backtest (+$) gap by matching each LIVE signal to
the BACKTEST signal that fires at the same time/direction, then measuring:
  (a) entry/exit price slippage on MATCHED pairs (live worse fill than backtest?)
  (b) LIVE-ONLY trades  — live took a signal the backtest never takes (over-trade)
  (c) BACKTEST-ONLY trades — backtest took a signal live missed

Live trades come from data/trade_history.json. Because the bot copy-trades 5-6
TopStep accounts, identical signals appear once per account; we DEDUPE to one
signal per (entry-second, entry-price, direction) so we compare the *strategy*,
not the account count. MNQ point value = $2.

Backtest is the SAME engine/preset #2 used by defense_backtest_compare.py.
Both sides are UTC. A live and backtest trade MATCH if same direction and entry
within MATCH_WIN_MIN minutes.

Run:  python -m scripts.live_vs_backtest_slippage
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from backend.data import candle_store
from backend.db.models import (
    BacktestConfig, get_commission_rt, get_fees_rt, _extract_symbol,
)
from backend.backtest.engine import BacktestEngine, _topstep_trade_date
from backend.api.routes import _normalize_strategy_name
from scripts.defense_backtest_compare import load_preset_params, _utc

LIVE_FILE     = os.path.join("data", "trade_history.json")
MATCH_WIN_MIN = 15        # entry-time tolerance for calling a live/bt pair "the same signal"
MATCH_PX_TICKS = 24       # entry-price tolerance (same breakout level + slippage), in ticks
MNQ_PV        = 2.0       # $ per point
TICK          = 0.25


def parse_ts(s: str) -> datetime:
    """Robust ISO parse — pad/trim fractional seconds to 6 digits (live logs vary)."""
    m = re.match(r"(.*?T\d\d:\d\d:\d\d)(?:\.(\d+))?(.*)", s)
    if not m:
        return datetime.fromisoformat(s)
    base, frac, tz = m.groups()
    frac = (frac or "")[:6].ljust(6, "0")
    return datetime.fromisoformat(f"{base}.{frac}{tz or '+00:00'}")


def load_live_signals():
    """Dedupe multi-account copies → one record per (entry-second, price, dir)."""
    recs = json.load(open(LIVE_FILE, encoding="utf-8"))
    seen = {}
    for r in recs:
        et = r.get("entry_time", "")
        key = (et[:19], r.get("entry_price"), r.get("direction"))
        if key in seen:
            continue
        seen[key] = {
            "entry_time": parse_ts(et),
            "exit_time": parse_ts(r.get("exit_time")) if r.get("exit_time") else None,
            "direction": r.get("direction"),
            "entry_price": r.get("entry_price"),
            "exit_price": r.get("exit_price"),
            "pnl": r.get("pnl") or 0.0,
            "exit_reason": r.get("exit_reason"),
        }
    return sorted(seen.values(), key=lambda x: x["entry_time"])


def run_backtest():
    params, name = load_preset_params()
    candles = candle_store.load("MNQ", 1)
    cid = params.contract_id
    config = BacktestConfig(
        strategies=[_normalize_strategy_name(params.strategy)],
        initial_capital=50_000.0,
        symbol=_extract_symbol(cid),
        commission_rt=get_commission_rt(cid),
        fees_rt=get_fees_rt(cid),
        value_area_pct=params.value_area_pct,
    )
    engine = BacktestEngine(config, strategy_params=params)
    result = engine.run(list(candles))
    bt_span = (_utc(candles[0].timestamp), _utc(candles[-1].timestamp))
    return result.trades, name, bt_span, params


def main():
    bt_trades, preset_name, bt_span, params = run_backtest()
    bt_start, bt_end = bt_span
    live = load_live_signals()

    # restrict both sides to the window the backtest data actually covers
    live = [l for l in live if bt_start <= l["entry_time"] <= bt_end]
    bt = [{
        "entry_time": _utc(t.entry_time),
        "exit_time": _utc(t.exit_time) if t.exit_time else None,
        "direction": t.direction.value if hasattr(t.direction, "value") else str(t.direction),
        "entry_price": t.entry_price,
        "exit_price": t.exit_price,
        "pnl": t.pnl or 0.0,
        "exit_reason": t.exit_reason,
    } for t in bt_trades if bt_start <= _utc(t.entry_time) <= bt_end]

    print(f"preset      : {preset_name}  (sessions={params.tr_allowed_sessions})")
    print(f"bt data span: {bt_start:%Y-%m-%d} → {bt_end:%Y-%m-%d}")
    print(f"live signals: {len(live)}   backtest signals: {len(bt)}")
    print(f"match window: same direction, entry within {MATCH_WIN_MIN} min")
    print()

    # ── greedy nearest-time matching ──────────────────────────────────────
    bt_used = [False] * len(bt)
    pairs = []
    live_only = []
    win = timedelta(minutes=MATCH_WIN_MIN)
    px_tol = MATCH_PX_TICKS * TICK
    for l in live:
        best_j, best_dt = None, win + timedelta(seconds=1)
        for j, b in enumerate(bt):
            if bt_used[j] or b["direction"] != l["direction"]:
                continue
            if l["entry_price"] is None or b["entry_price"] is None:
                continue
            if abs(l["entry_price"] - b["entry_price"]) > px_tol:
                continue  # different breakout level → not the same signal
            dt = abs(b["entry_time"] - l["entry_time"])
            if dt <= win and dt < best_dt:
                best_j, best_dt = j, dt
        if best_j is None:
            live_only.append(l)
        else:
            bt_used[best_j] = True
            pairs.append((l, bt[best_j]))
    bt_only = [b for j, b in enumerate(bt) if not bt_used[j]]

    # ── slippage on matched pairs ─────────────────────────────────────────
    def signed_slip_ticks(l, b, leg):
        """+ = live filled WORSE than backtest (adverse), in ticks."""
        lp, bp = l[leg], b[leg]
        if lp is None or bp is None:
            return None
        diff = (lp - bp) / TICK
        # entry: buy worse if higher price; sell worse if lower price
        # exit:  buy(close=sell) worse if lower; sell(close=buy) worse if higher
        long = (l["direction"] == "buy")
        if leg == "entry_price":
            return diff if long else -diff
        else:  # exit
            return -diff if long else diff

    ent_slip = [signed_slip_ticks(l, b, "entry_price") for l, b in pairs]
    ext_slip = [signed_slip_ticks(l, b, "exit_price") for l, b in pairs]
    ent_slip = [x for x in ent_slip if x is not None]
    ext_slip = [x for x in ext_slip if x is not None]

    live_pair_pnl = sum(l["pnl"] for l, _ in pairs)
    bt_pair_pnl = sum(b["pnl"] for _, b in pairs)

    print("── MATCHED PAIRS ──────────────────────────────────────────────")
    print(f"  pairs                 : {len(pairs)}")
    if ent_slip:
        print(f"  entry slippage (ticks): avg {sum(ent_slip)/len(ent_slip):+.2f}  "
              f"(+ = live worse)   total ${sum(ent_slip)*TICK*MNQ_PV:+.0f}")
    if ext_slip:
        print(f"  exit  slippage (ticks): avg {sum(ext_slip)/len(ext_slip):+.2f}  "
              f"total ${sum(ext_slip)*TICK*MNQ_PV:+.0f}")
    print(f"  matched live PnL      : {live_pair_pnl:+.1f}")
    print(f"  matched backtest PnL  : {bt_pair_pnl:+.1f}")
    print(f"  pair PnL gap (live-bt): {live_pair_pnl - bt_pair_pnl:+.1f}")
    print()
    print("── UNMATCHED ──────────────────────────────────────────────────")
    print(f"  LIVE-ONLY trades : {len(live_only):4d}   PnL {sum(x['pnl'] for x in live_only):+.1f}  "
          f"(signals live took, backtest never did)")
    print(f"  BACKTEST-ONLY    : {len(bt_only):4d}   PnL {sum(x['pnl'] for x in bt_only):+.1f}  "
          f"(signals backtest took, live missed)")
    print()

    # live-only by UTC hour — where does the over-trading happen?
    by_h = defaultdict(lambda: [0, 0.0])
    for x in live_only:
        h = x["entry_time"].hour
        by_h[h][0] += 1
        by_h[h][1] += x["pnl"]
    print("  LIVE-ONLY by UTC hour (count | pnl):")
    for h in sorted(by_h):
        print(f"    {h:02d}:00 | {by_h[h][0]:4d} | {by_h[h][1]:+9.1f}")

    # ── sample of worst matched pairs ─────────────────────────────────────
    print()
    print("── 10 WORST live-vs-bt pair gaps ──────────────────────────────")
    ranked = sorted(pairs, key=lambda p: p[0]["pnl"] - p[1]["pnl"])[:10]
    for l, b in ranked:
        es = signed_slip_ticks(l, b, "entry_price")
        print(f"  {l['entry_time']:%m-%d %H:%M} {l['direction']:4} "
              f"live {l['entry_price']}->{l['exit_price']} {l['pnl']:+7.1f} {l['exit_reason']:4} | "
              f"bt {b['entry_price']}->{b['exit_price']} {b['pnl']:+7.1f} {b['exit_reason']} | "
              f"entrySlip {es:+.1f}t")


if __name__ == "__main__":
    main()
