# ============================================================
# 文件: scripts/factor_trend_confluence_study.py
# 狀態: 1.0.9 研究腳本 (FACTOR × TREND 雙重過濾)
# 假設: FACTOR 信號(5m 收盤)與 TREND 突破信號(1m,VA80/15m zone)
#       在 W 分鐘內同向 → 下一根 1m 開盤市價進場。主軸 long_only。
# 方法: 兩邊各自採集「原始信號流」(不經引擎倉位/日額審查,factor 日額=0,
#       trend 用 mark_breakout_used 去重 = 一個 zone+方向一次事件),
#       再用統一的輕量模擬器比較 雙重過濾 vs FACTOR-only vs TREND-only。
# 出場: factor_tp(atr_blend SL2.5/TP7.5 = BEST 口徑)| ladder(2R 棘輪)
#       | trend_rr(zone SL × RR2)。 一次一倉、日上限 3 單、19:45 UTC 強平。
# 輸出: data/machinelearning/factor_trend_confluence_<stamp>.json / .md
# ============================================================
"""FACTOR×TREND 雙重過濾研究:W 分鐘內同向信號才操作,long-only 主軸。"""

from __future__ import annotations

import json
import math
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.api.routes import (
    BacktestRequest,
    _build_strategy_params_from_request,
    _normalize_contract_size,
)
from backend.backtest.engine import _topstep_trade_date
from backend.backtest.sweep import build_trend_zone_timeline
from backend.data import candle_store
from backend.db.models import (
    Direction, current_quarterly_contract_id,
    get_commission_rt, get_fees_rt, get_point_value, get_tick_size,
)
from backend.strategy.factor import FactorSignalStrategy
from backend.strategy.trend_follow import SessionTrendFollow

FLATTEN_UTC_MIN = 19 * 60 + 45      # 19:45 UTC 強平
PRE_FLATTEN_UTC_MIN = 19 * 60 + 30  # 19:30 起不進新單
SESSION_START_UTC_MIN = 22 * 60

FACTOR_SETS = (
    ("emapmo", "early"),
    ("emapmo", "normal"),
    ("icefishball", "normal"),
    ("momentum_reversion", "normal"),
)
TREND_CONFIRMS = (3, 5)
WINDOWS_MIN = (5, 10, 15, 30)   # 主軸 5m,其餘看敏感度
PRIMARY_WINDOW = 5
EXITS = ("factor_tp", "ladder", "trend_rr")
MAX_TRADES_PER_DAY = 3
LADDER_TRIGGER_R = 2.0
LADDER_GAP_R = 2.0

FAMILY_LABEL = {"emapmo": "EMAPMO", "icefishball": "KDJMA", "momentum_reversion": "MREV"}


def _utc_min(ts) -> int:
    return ts.hour * 60 + ts.minute


def _in_no_entry_window(ts) -> bool:
    m = _utc_min(ts)
    return PRE_FLATTEN_UTC_MIN <= m < SESSION_START_UTC_MIN


def _is_flatten(ts) -> bool:
    m = _utc_min(ts)
    return FLATTEN_UTC_MIN <= m < SESSION_START_UTC_MIN


def harvest_factor_signals(base, candles, family: str, mode: str) -> list[dict]:
    """原始 FACTOR 信號流:side=all、日額=0(不審查),SL/TP 用 BEST 口徑 atr_blend 2.5/7.5。"""
    import copy
    p = copy.deepcopy(base)
    p.strategy = "factor"
    p.factor_timeframe_minutes = 5
    p.factor_signal_family = family
    p.factor_side_mode = "all"
    p.factor_pmo_signal_mode = mode
    p.factor_session_va_filter = "off"
    p.factor_sl_rule = "atr_blend"
    p.factor_tp_rule = "atr_blend"
    p.factor_sl_value = 2.5
    p.factor_tp_value = 7.5
    p.factor_max_hold_bars = 0
    p.factor_max_trades_per_day = 0     # 不設日額 → 原始信號
    p.factor_warmup_bars = 150
    strat = FactorSignalStrategy(params=p)
    out = []
    for i, c in enumerate(candles):
        sig = strat.evaluate(c, [], True)
        if sig is None:
            continue
        out.append({
            "idx": i,
            "ts": c.timestamp,
            "dir": "long" if sig.direction == Direction.BUY else "short",
            "sl_w": abs(float(sig.entry_price) - float(sig.sl_price)),
            "tp_w": abs(float(sig.tp_price) - float(sig.entry_price)),
        })
    return out


def harvest_trend_signals(base, candles, timeline, confirm_bars: int) -> list[dict]:
    """原始 TREND 突破信號流:mark_breakout_used 去重 → 一個 zone+方向只記一次。"""
    import copy
    p = copy.deepcopy(base)
    p.strategy = "trend"
    p.area_timeframe = "15m"
    p.value_area_pct = 0.80
    p.method = "single"
    p.tf_combo = []
    p.breakout_confirm_bars = int(confirm_bars)
    p.rr_ratio = 2
    strat = SessionTrendFollow(params=p)
    out = []
    for i, c in enumerate(candles):
        tl = timeline[i]
        sig = strat.evaluate(c, tl["recent"], tl["mature"])
        if sig is None:
            continue
        d = "long" if sig.direction == Direction.BUY else "short"
        out.append({
            "idx": i,
            "ts": c.timestamp,
            "dir": d,
            "sl_w": abs(float(sig.entry_price) - float(sig.sl_price)),
        })
        zid = str(sig.zone_id or "")
        strat.mark_breakout_used(zid, "up" if d == "long" else "down")
    return out


def match_confluence(f_sigs, t_sigs, window_min: int) -> list[dict]:
    """同向且 |Δt| ≤ window → 事件記在較晚信號的 idx(市價下一根進場)。

    每個 (factor 信號, trend 信號) 只配一次;同一根 K 多事件只留第一個。
    """
    events: list[dict] = []
    ti = 0
    t_by_dir: dict[str, list[dict]] = defaultdict(list)
    f_by_dir: dict[str, list[dict]] = defaultdict(list)
    for s in t_sigs:
        t_by_dir[s["dir"]].append(s)
    for s in f_sigs:
        f_by_dir[s["dir"]].append(s)
    for d in ("long", "short"):
        used_pairs = set()
        for f in f_by_dir[d]:
            for t in t_by_dir[d]:
                dt_min = abs((f["ts"] - t["ts"]).total_seconds()) / 60.0
                if dt_min > window_min:
                    continue
                pair = (f["idx"], t["idx"])
                if pair in used_pairs:
                    continue
                used_pairs.add(pair)
                events.append({
                    "idx": max(f["idx"], t["idx"]),
                    "ts": max(f["ts"], t["ts"]),
                    "dir": d,
                    "f_sl_w": f["sl_w"],
                    "f_tp_w": f["tp_w"],
                    "t_sl_w": t["sl_w"],
                    "gap_min": round(dt_min, 1),
                })
    events.sort(key=lambda e: e["idx"])
    dedup = []
    seen_idx = set()
    for e in events:
        if e["idx"] in seen_idx:
            continue
        seen_idx.add(e["idx"])
        dedup.append(e)
    return dedup


def simulate(events, candles, exit_mode: str, side: str,
             point_value: float, cost_rt: float, tick: float) -> dict:
    """輕量模擬:下一根開盤進場、SL/TP 同根先算 SL(保守)、19:45 強平、
    一次一倉、日上限 3 單。ladder 依引擎口徑(2R 觸發、恆落後 2R,收盤棘輪)。"""
    n = len(candles)
    trades = []
    busy_until = -1
    daily_n: dict[str, int] = defaultdict(int)
    for e in events:
        if side != "all" and e["dir"] != side:
            continue
        i0 = e["idx"] + 1
        if i0 >= n or i0 <= busy_until:
            continue
        ts0 = candles[i0].timestamp
        if _in_no_entry_window(ts0):
            continue
        dkey = _topstep_trade_date(ts0)
        if daily_n[dkey] >= MAX_TRADES_PER_DAY:
            continue
        entry = float(candles[i0].open)
        is_long = e["dir"] == "long"
        if exit_mode == "factor_tp":
            sl_w, tp_w = e["f_sl_w"], e["f_tp_w"]
        elif exit_mode == "trend_rr":
            sl_w, tp_w = e["t_sl_w"], e["t_sl_w"] * 2.0
        else:   # ladder
            sl_w, tp_w = e["f_sl_w"], None
        if sl_w <= 0:
            continue
        sl = entry - sl_w if is_long else entry + sl_w
        tp = (entry + tp_w if is_long else entry - tp_w) if tp_w else None
        max_r = 0.0
        exit_px = exit_reason = None
        j = i0 + 1
        while j < n:
            c = candles[j]
            if _is_flatten(c.timestamp):
                exit_px, exit_reason = float(c.close), "flatten"
                break
            hit_sl = (c.low <= sl) if is_long else (c.high >= sl)
            hit_tp = tp is not None and ((c.high >= tp) if is_long else (c.low <= tp))
            if hit_sl:                      # 同根雙觸 → 保守算 SL
                exit_px, exit_reason = sl, "sl"
                break
            if hit_tp:
                exit_px, exit_reason = tp, "tp"
                break
            if exit_mode == "ladder":
                fav = (c.close - entry) if is_long else (entry - c.close)
                r = fav / sl_w
                max_r = max(max_r, r)
                if max_r >= LADDER_TRIGGER_R:
                    lock_r = math.floor(max_r) - LADDER_GAP_R
                    new_sl = entry + lock_r * sl_w if is_long else entry - lock_r * sl_w
                    new_sl = round(new_sl / tick) * tick
                    if (is_long and new_sl > sl) or (not is_long and new_sl < sl):
                        sl = new_sl
            j += 1
        if exit_px is None:
            exit_px, exit_reason, j = float(candles[-1].close), "eod", n - 1
        pts = (exit_px - entry) if is_long else (entry - exit_px)
        pnl = pts * point_value - cost_rt
        daily_n[dkey] += 1
        busy_until = j
        trades.append({
            "entry_ts": ts0.isoformat(), "dir": e["dir"], "entry": entry,
            "exit": exit_px, "reason": exit_reason, "pnl": round(pnl, 2),
            "hold_min": (candles[j].timestamp - ts0).total_seconds() / 60.0,
            "gap_min": e.get("gap_min"),
        })
    return _metrics(trades)


def _metrics(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0, "pf": 0.0, "pnl": 0.0, "win_rate": 0.0, "max_dd": 0.0,
                "expect": 0.0, "avg_hold_min": 0.0, "monthly_avg": 0.0, "wf_pass": False,
                "trade_list": []}
    gain = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    loss = sum(-t["pnl"] for t in trades if t["pnl"] < 0)
    eq = peak = dd = 0.0
    for t in trades:
        eq += t["pnl"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    days = sorted({t["entry_ts"][:10] for t in trades})
    from datetime import date
    span = max(1, (date.fromisoformat(days[-1]) - date.fromisoformat(days[0])).days + 1)
    total = sum(t["pnl"] for t in trades)
    # 三段 walk-forward(依交易序三等分)
    k = max(1, len(trades) // 3)
    segs = [trades[:k], trades[k:2 * k], trades[2 * k:]]
    wf = all(sum(t["pnl"] for t in s) > 0 for s in segs if s)
    return {
        "trades": len(trades),
        "win_rate": round(sum(1 for t in trades if t["pnl"] > 0) / len(trades), 3),
        "pf": round(gain / loss, 3) if loss > 0 else (999.0 if gain > 0 else 0.0),
        "pnl": round(total, 1),
        "max_dd": round(dd, 1),
        "expect": round(total / len(trades), 2),
        "avg_hold_min": round(sum(t["hold_min"] for t in trades) / len(trades), 1),
        "monthly_avg": round(total * 30.44 / span, 1),
        "wf_pass": wf,
        "trade_list": trades,
    }


def main() -> int:
    t0 = time.time()
    symbol = "MNQ"
    cid = current_quarterly_contract_id(symbol)
    size = _normalize_contract_size(cid, 1)
    req = BacktestRequest(contract_id=cid, contract_size=size)
    base = _build_strategy_params_from_request(req, size)
    candles = sorted(candle_store.load(symbol, 1), key=lambda c: c.timestamp)
    if not candles:
        raise SystemExit("No MNQ 1m candles.")
    point_value = float(get_point_value(cid))
    cost_rt = float(get_commission_rt(cid)) + float(get_fees_rt(cid))
    tick = float(get_tick_size(cid))
    print(f"STUDY_START candles={len(candles)} "
          f"range={candles[0].timestamp.isoformat()}->{candles[-1].timestamp.isoformat()} "
          f"ptval={point_value} cost_rt={cost_rt}", flush=True)

    # ── 信號採集 ──
    f_streams: dict[str, list[dict]] = {}
    for family, mode in FACTOR_SETS:
        key = f"{FAMILY_LABEL[family]}-{mode}"
        f_streams[key] = harvest_factor_signals(base, candles, family, mode)
        ls = sum(1 for s in f_streams[key] if s["dir"] == "long")
        print(f"FACTOR_SIGNALS {key} n={len(f_streams[key])} long={ls}", flush=True)

    print("BUILD_TIMELINE 15m VA80 ...", flush=True)
    timeline = build_trend_zone_timeline(candles, "15m", 0.80)
    t_streams: dict[str, list[dict]] = {}
    for cb in TREND_CONFIRMS:
        key = f"T{cb}"
        t_streams[key] = harvest_trend_signals(base, candles, timeline, cb)
        ls = sum(1 for s in t_streams[key] if s["dir"] == "long")
        print(f"TREND_SIGNALS {key} n={len(t_streams[key])} long={ls}", flush=True)

    # ── 雙重過濾 sims ──
    rows = []
    for f_key, f_sigs in f_streams.items():
        for t_key, t_sigs in t_streams.items():
            for w in WINDOWS_MIN:
                events = match_confluence(f_sigs, t_sigs, w)
                for exit_mode in EXITS:
                    for side in ("long", "all"):
                        m = simulate(events, candles, exit_mode, side,
                                     point_value, cost_rt, tick)
                        trade_list = m.pop("trade_list")
                        rows.append({
                            "kind": "confluence", "factor": f_key, "trend": t_key,
                            "window": w, "exit": exit_mode, "side": side,
                            "events": len(events), **m,
                            "trade_list": trade_list if (w == PRIMARY_WINDOW and side == "long") else None,
                        })
                if w == PRIMARY_WINDOW:
                    print(f"CONFLUENCE {f_key} x {t_key} W{w} events={len(events)}", flush=True)

    # ── 單邊 baselines(同模擬器同規則)──
    for f_key, f_sigs in f_streams.items():
        events = [{"idx": s["idx"], "ts": s["ts"], "dir": s["dir"],
                   "f_sl_w": s["sl_w"], "f_tp_w": s["tp_w"], "t_sl_w": s["sl_w"],
                   "gap_min": 0.0} for s in f_sigs]
        for exit_mode in ("factor_tp", "ladder"):
            for side in ("long", "all"):
                m = simulate(events, candles, exit_mode, side, point_value, cost_rt, tick)
                m.pop("trade_list")
                rows.append({"kind": "factor_only", "factor": f_key, "trend": "-",
                             "window": 0, "exit": exit_mode, "side": side,
                             "events": len(events), **m, "trade_list": None})
    for t_key, t_sigs in t_streams.items():
        events = [{"idx": s["idx"], "ts": s["ts"], "dir": s["dir"],
                   "f_sl_w": s["sl_w"], "f_tp_w": s["sl_w"] * 3.0, "t_sl_w": s["sl_w"],
                   "gap_min": 0.0} for s in t_sigs]
        for exit_mode in ("trend_rr", "ladder"):
            for side in ("long", "all"):
                m = simulate(events, candles, exit_mode, side, point_value, cost_rt, tick)
                m.pop("trade_list")
                rows.append({"kind": "trend_only", "factor": "-", "trend": t_key,
                             "window": 0, "exit": exit_mode, "side": side,
                             "events": len(events), **m, "trade_list": None})

    # ── 輸出 ──
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
    out_dir = ROOT / "data" / "machinelearning"
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "candles": len(candles),
        "range": [candles[0].timestamp.isoformat(), candles[-1].timestamp.isoformat()],
        "signal_counts": {
            **{k: len(v) for k, v in f_streams.items()},
            **{k: len(v) for k, v in t_streams.items()},
        },
        "rows": rows,
    }
    (out_dir / f"factor_trend_confluence_{stamp}.json").write_text(
        json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")

    def _fmt(r):
        return (f"| {r['factor']} | {r['trend']} | {r['exit']} | {r['events']} | {r['trades']} "
                f"| {r['win_rate']:.0%} | {r['pf']:.2f} | {r['pnl']:.0f} | {r['max_dd']:.0f} "
                f"| {r['monthly_avg']:.0f} | {'Y' if r['wf_pass'] else 'N'} |")

    hdr = ("| factor | trend | exit | ev | tr | wr | pf | pnl | dd | mo | wf |",
           "|---|---|---|---|---|---|---|---|---|---|---|")
    prim = [r for r in rows if r["kind"] == "confluence" and r["window"] == PRIMARY_WINDOW
            and r["side"] == "long"]
    prim.sort(key=lambda r: -r["pf"])
    fb = [r for r in rows if r["kind"] == "factor_only" and r["side"] == "long"]
    tb = [r for r in rows if r["kind"] == "trend_only" and r["side"] == "long"]
    lines = [f"# FACTOR×TREND confluence study {stamp}",
             f"candles={len(candles)} range={payload['range'][0]} -> {payload['range'][1]}",
             f"signal_counts={json.dumps(payload['signal_counts'])}",
             "", f"## 雙重過濾 LONG-ONLY (W={PRIMARY_WINDOW}m)", *hdr,
             *[_fmt(r) for r in prim],
             "", "## FACTOR-only LONG baselines", *hdr, *[_fmt(r) for r in fb],
             "", "## TREND-only LONG baselines", *hdr, *[_fmt(r) for r in tb],
             "", "## 窗口敏感度 (long, factor_tp)",
             "| factor | trend | W | ev | tr | wr | pf | pnl |", "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if (r["kind"] == "confluence" and r["side"] == "long" and r["exit"] == "factor_tp"):
            lines.append(f"| {r['factor']} | {r['trend']} | {r['window']} | {r['events']} "
                         f"| {r['trades']} | {r['win_rate']:.0%} | {r['pf']:.2f} | {r['pnl']:.0f} |")
    (out_dir / f"factor_trend_confluence_{stamp}.md").write_text(
        "\n".join(lines), encoding="utf-8")

    print(f"STUDY_DONE rows={len(rows)} elapsed={time.time() - t0:.0f}s", flush=True)
    for r in prim[:10]:
        print("STUDY_TOP " + _fmt(r), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
