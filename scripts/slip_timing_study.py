"""1.0.8 研究:實盤滑價有多嚴重 + lower-TP 回測 + live/backtest 觸發時間對比。

只看「新邏輯」窗口:上周四 2026-06-25 → 今天。資料來自 data/trades.json
(有 strategy=='trend' 標記與 original_sl/tp,可用幾何 RR 分辨 preset):
  RR = |entry-orig_tp| / |entry-orig_sl|  →  4=#2, 6=#3, 7=#4

三個區塊:
  A. 每個 preset 的滑價(SL 成交 vs sl_price、TP 成交 vs tp_price,單位 tick)+
     勝率 / exit 分佈 —— 回答「slip 有多嚴重、為什麼 #2#3 都不對」。
  B. lower-TP 回測:preset #2/#3 在原 RR 及較低 RR 的績效對比。
  C. live vs backtest 觸發時間:新窗口內把 live #3 單配對到 backtest #3 單,
     看觸發時間/價格是否一致 —— 找「不對」的根因。

Run:  PYTHONIOENCODING=utf-8 python -m scripts.slip_timing_study
"""
from __future__ import annotations

import copy
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.backtest.engine import BacktestEngine
from backend.data import candle_store
from backend.db.models import (
    BacktestConfig,
    _extract_symbol,
    get_commission_rt,
    get_fees_rt,
)
from backend.terminal_live import (
    BUILTIN_PRESETS,
    CODEX_626_PRESET_2,
    CODEX_630_PRESET_3,
    _build_strategy_params,
)

TRADES_FILE = Path("data/trades.json")
NEW_LOGIC_START = datetime(2026, 6, 25, tzinfo=timezone.utc)  # 上周四
TICK = 0.25
MNQ_PV = 2.0
INITIAL_CAPITAL = 50_000.0


def parse_ts(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def preset_from_rr(entry, osl, otp):
    if not (entry and osl and otp):
        return None
    sld = abs(entry - osl)
    tpd = abs(entry - otp)
    if sld <= 0:
        return None
    rr = round(tpd / sld)
    return {4: "#2", 6: "#3", 7: "#4"}.get(rr), rr


def load_live_trend():
    """trend, closed, non-shadow, new-logic window, deduped across accounts."""
    recs = json.load(open(TRADES_FILE, encoding="utf-8"))
    seen = {}
    for r in recs:
        if r.get("strategy") != "trend" or r.get("shadow"):
            continue
        et = parse_ts(r.get("entry_time"))
        if et is None or et < NEW_LOGIC_START:
            continue
        key = (r.get("entry_time", "")[:19], r.get("entry_price"), r.get("direction"))
        if key in seen:
            continue
        tag = preset_from_rr(r.get("entry_price"), r.get("original_sl_price"),
                             r.get("original_tp_price"))
        seen[key] = {
            "entry_time": et,
            "exit_time": parse_ts(r.get("exit_time")),
            "direction": r.get("direction"),
            "entry_price": r.get("entry_price"),
            "exit_price": r.get("exit_price"),
            "sl_price": r.get("sl_price"),
            "tp_price": r.get("tp_price"),
            "orig_sl": r.get("original_sl_price"),
            "orig_tp": r.get("original_tp_price"),
            "pnl": float(r.get("topstep_pnl") or 0.0),
            "exit_reason": r.get("exit_reason"),
            "status": r.get("status"),
            "preset": tag[0] if tag else None,
            "rr": tag[1] if tag else None,
        }
    return sorted(seen.values(), key=lambda x: x["entry_time"])


def sl_slip_ticks(t):
    """+ ticks = filled WORSE than the stop level (adverse). SL exits only."""
    if t["exit_reason"] != "sl" or t["exit_price"] is None or t["sl_price"] is None:
        return None
    long = (t["direction"] == "buy")
    diff = (t["exit_price"] - t["sl_price"]) / TICK
    return -diff if long else diff  # long stop below: worse=lower; short stop above: worse=higher


def tp_slip_ticks(t):
    """+ ticks = filled WORSE than the TP level (gave up edge). TP exits only."""
    if t["exit_reason"] != "tp" or t["exit_price"] is None or t["tp_price"] is None:
        return None
    long = (t["direction"] == "buy")
    diff = (t["exit_price"] - t["tp_price"]) / TICK
    return diff if long else -diff  # long TP above: worse=lower→ +?  handled by sign below


def _stats(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    xs.sort()
    p90 = xs[min(len(xs) - 1, int(0.9 * len(xs)))]
    return {"n": len(xs), "mean": statistics.mean(xs), "med": statistics.median(xs),
            "p90": p90, "max": xs[-1]}


# ─────────────────────── A. slippage per preset ───────────────────────

def block_slippage(live):
    print(f"\n== A. 新邏輯窗口 {NEW_LOGIC_START.date()} → today 實盤 trend 滑價 ==", flush=True)
    print(f"deduped trend trades: {len(live)}", flush=True)
    by = defaultdict(list)
    for t in live:
        by[t["preset"] or "?"].append(t)
    for tag in ("#2", "#3", "#4", "?"):
        ts = by.get(tag)
        if not ts:
            continue
        wins = sum(1 for t in ts if t["pnl"] > 0)
        reasons = Counter(t["exit_reason"] for t in ts)
        pnl = sum(t["pnl"] for t in ts)
        sl = _stats([sl_slip_ticks(t) for t in ts])
        print(f"\n  preset {tag}  n={len(ts)}  win%={100*wins/len(ts):.1f}  "
              f"pnl={pnl:+.0f}  exit={dict(reasons)}", flush=True)
        if sl:
            print(f"    SL 成交滑價(tick,+=更差): mean {sl['mean']:+.2f}  med {sl['med']:+.2f}  "
                  f"p90 {sl['p90']:+.2f}  max {sl['max']:+.2f}  "
                  f"→ 平均每筆 ${sl['mean']*TICK*MNQ_PV:+.2f}", flush=True)


# ─────────────────────── B. lower-TP backtest ───────────────────────

def _run_bt(params, candles):
    cid = params.contract_id
    config = BacktestConfig(
        strategies=["trend"], initial_capital=INITIAL_CAPITAL,
        symbol=_extract_symbol(cid), commission_rt=get_commission_rt(cid),
        fees_rt=get_fees_rt(cid), value_area_pct=float(getattr(params, "value_area_pct", 0.80)),
    )
    m = BacktestEngine(config=config, strategy_params=params,
                       zone_timeline=None, record_equity=False).run(candles).metrics
    return m


def _with_rr(base, rr):
    p = copy.deepcopy(base)
    sl = int(getattr(p, "tr_sl_ticks", None) or getattr(p, "sl_ticks", 80) or 80)
    tp = int(rr * sl)
    p.rr_ratio = int(rr)
    p.tp_ticks = tp
    p.tr_tp_ticks = tp
    return p


def block_lower_tp(candles):
    print("\n== B. lower-TP 回測(全區間;SL 不變,只改 RR)==", flush=True)
    print(f"{'preset':<8}{'RR':>4}{'trades':>8}{'win%':>7}{'pnl':>10}{'maxDD':>9}{'PF':>6}{'Calmar':>8}{'expect':>9}", flush=True)
    for tag, name in (("#2", CODEX_626_PRESET_2), ("#3", CODEX_630_PRESET_3)):
        preset = BUILTIN_PRESETS[name]
        base = _build_strategy_params(preset, preset.get("contract_id", "CON.F.US.MNQ.U26"))
        orig_rr = int(getattr(base, "rr_ratio", 4) or 4)
        for rr in sorted({1, 2, 3, 4, orig_rr}):
            m = _run_bt(_with_rr(base, rr), candles)
            star = " *orig" if rr == orig_rr else ""
            print(f"{tag:<8}{rr:>4}{m.total_trades:>8}{100*m.win_rate:>6.1f}%"
                  f"{m.total_pnl:>+10.0f}{m.max_drawdown:>9.0f}{m.profit_factor:>6.2f}"
                  f"{m.calmar_ratio:>8.2f}{m.expectancy:>+9.2f}{star}", flush=True)


# ─────────────────────── C. live vs backtest timing ───────────────────────

def block_timing(candles, live):
    print("\n== C. 新窗口 live #3 vs backtest #3 觸發時間對比 ==", flush=True)
    preset = BUILTIN_PRESETS[CODEX_630_PRESET_3]
    base = _build_strategy_params(preset, preset.get("contract_id", "CON.F.US.MNQ.U26"))
    cid = base.contract_id
    config = BacktestConfig(
        strategies=["trend"], initial_capital=INITIAL_CAPITAL,
        symbol=_extract_symbol(cid), commission_rt=get_commission_rt(cid),
        fees_rt=get_fees_rt(cid), value_area_pct=float(getattr(base, "value_area_pct", 0.80)),
    )
    bt = BacktestEngine(config=config, strategy_params=base,
                        zone_timeline=None, record_equity=False).run(candles).trades
    bt3 = [{
        "entry_time": t.entry_time if t.entry_time.tzinfo else t.entry_time.replace(tzinfo=timezone.utc),
        "direction": t.direction.value if hasattr(t.direction, "value") else str(t.direction),
        "entry_price": t.entry_price, "pnl": t.pnl or 0.0, "exit_reason": t.exit_reason,
    } for t in bt if t.entry_time and (t.entry_time.replace(tzinfo=timezone.utc) if not t.entry_time.tzinfo else t.entry_time) >= NEW_LOGIC_START]

    live3 = [t for t in live if t["preset"] == "#3"]
    print(f"live #3: {len(live3)}   backtest #3 (同窗口): {len(bt3)}", flush=True)

    win = timedelta(minutes=15)
    px_tol = 24 * TICK
    used = [False] * len(bt3)
    pairs, live_only = [], []
    for l in live3:
        best, bdt = None, win + timedelta(seconds=1)
        for j, b in enumerate(bt3):
            if used[j] or b["direction"] != l["direction"]:
                continue
            if l["entry_price"] is None or abs(l["entry_price"] - b["entry_price"]) > px_tol:
                continue
            dt = abs(b["entry_time"] - l["entry_time"])
            if dt <= win and dt < bdt:
                best, bdt = j, dt
        if best is None:
            live_only.append(l)
        else:
            used[best] = True
            pairs.append((l, bt3[best]))
    bt_only = [b for j, b in enumerate(bt3) if not used[j]]

    print(f"  配對成功 : {len(pairs)}", flush=True)
    print(f"  LIVE-only: {len(live_only)}  pnl={sum(x['pnl'] for x in live_only):+.0f}  (backtest 不會進的單)", flush=True)
    print(f"  BT-only  : {len(bt_only)}  pnl={sum(x['pnl'] for x in bt_only):+.0f}  (backtest 進、live 沒抓到)", flush=True)
    if pairs:
        ent = []
        for l, b in pairs:
            long = (l["direction"] == "buy")
            d = (l["entry_price"] - b["entry_price"]) / TICK
            ent.append(d if long else -d)
        offs = [abs((l["entry_time"] - b["entry_time"]).total_seconds()) / 60 for l, b in pairs]
        print(f"  配對進場滑價(tick,+=更差): mean {statistics.mean(ent):+.2f}  med {statistics.median(ent):+.2f}", flush=True)
        print(f"  配對觸發時間差(分鐘): mean {statistics.mean(offs):.1f}  med {statistics.median(offs):.1f}", flush=True)
        lp = sum(l["pnl"] for l, _ in pairs)
        bp = sum(b["pnl"] for _, b in pairs)
        print(f"  配對 live pnl {lp:+.0f} vs backtest pnl {bp:+.0f}  gap {lp-bp:+.0f}", flush=True)


def main():
    candles = candle_store.load("MNQ", 1)
    candles.sort(key=lambda c: c.timestamp)
    print(f"candles {len(candles)}  {candles[0].timestamp} -> {candles[-1].timestamp}", flush=True)
    live = load_live_trend()
    block_slippage(live)
    block_lower_tp(candles)
    block_timing(candles, live)


if __name__ == "__main__":
    main()
