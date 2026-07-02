"""Audit live order precision against same-day backtests.

Default date plan:
  today:                 CLAUDE #1
  yesterday:             CODEX #3
  day before yesterday
    back to last Thu:    CODEX #1

The script uses Topstep/CME trade dates (CT reset at 17:00), not UTC calendar
dates. It compares strategy-level live exits to backtest trades generated with
the assigned preset for each date.

Run:
  python scripts/audit_order_precision.py
  python scripts/audit_order_precision.py --today 2026-07-01
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import math
import os
import re
import statistics
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.api.routes import (
    _BUILTIN_PRESETS,
    _CODEX_630_PRESET_1,
    _codex_626_trend_preset,
    _normalize_strategy_name,
)
from backend.backtest.engine import BacktestEngine, _topstep_trade_date
from backend.backtest.metrics import MetricsCalculator
from backend.data import candle_store
from backend.db.models import (
    BacktestConfig,
    Direction,
    ExitReason,
    StrategyParams,
    Trade,
    _extract_symbol,
    get_commission_rt,
    get_fees_rt,
)


PRESET_FILE = ROOT / "data" / "presets.json"
LIVE_EXITS_FILE = ROOT / "data" / "live_exits.json"
TRADE_HISTORY_FILE = ROOT / "data" / "trade_history.json"
ORDERS_FILE = ROOT / "data" / "trades.json"
REPORT_FILE = ROOT / "data" / "machinelearning" / "order_precision_audit_latest.txt"
DETAIL_CSV = ROOT / "data" / "machinelearning" / "order_precision_audit_pairs_latest.csv"

TICK_SIZE = 0.25
INITIAL_CAPITAL = 50_000.0
MATCH_WINDOW_MINUTES = 20
MATCH_PRICE_TICKS = 40
MISS_CANCEL_WINDOW_MINUTES = 20
POST_TP_WINDOW_MINUTES = 60


@dataclass
class Rec:
    source: str
    preset_name: str
    trade_date: str
    entry_time: datetime
    exit_time: Optional[datetime]
    direction: str
    entry_price: float
    exit_price: Optional[float]
    sl_price: Optional[float]
    tp_price: Optional[float]
    original_tp_price: Optional[float]
    pnl: float
    exit_reason: str
    wall_id: str = ""
    zone_id: str = ""
    trail_triggered: bool = False
    post_reached_tp: Optional[bool] = None
    post_broke_trail_first: Optional[bool] = None
    post_mfe_ticks: Optional[float] = None


def parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    s = str(value)
    m = re.match(r"(.*?T\d\d:\d\d:\d\d)(?:\.(\d+))?(.*)", s)
    if m:
        base, frac, tz = m.groups()
        frac = (frac or "")[:6].ljust(6, "0")
        tz = tz or "+00:00"
        if tz == "Z":
            tz = "+00:00"
        dt = datetime.fromisoformat(f"{base}.{frac}{tz}")
    else:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def fmt_money(v: float) -> str:
    return f"${v:,.2f}"


def fmt_signed_money(v: float) -> str:
    return f"{v:+,.2f}"


def fmt_num(v: Optional[float], digits: int = 2) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    return f"{v:.{digits}f}"


def safe_mean(vals: Iterable[float]) -> Optional[float]:
    xs = [float(v) for v in vals if v is not None and not math.isnan(float(v))]
    return sum(xs) / len(xs) if xs else None


def safe_median(vals: Iterable[float]) -> Optional[float]:
    xs = [float(v) for v in vals if v is not None and not math.isnan(float(v))]
    return statistics.median(xs) if xs else None


def pct(n: float, d: float) -> str:
    if not d:
        return "n/a"
    return f"{100.0 * n / d:.1f}%"


def load_all_presets() -> Dict[str, Dict[str, Any]]:
    presets: Dict[str, Dict[str, Any]] = {}
    if PRESET_FILE.exists():
        raw = json.load(open(PRESET_FILE, encoding="utf-8"))
        presets.update(raw.get("presets") or {})
    presets.update({k: dict(v) for k, v in _BUILTIN_PRESETS.items()})
    presets.setdefault(
        _CODEX_630_PRESET_1,
        _codex_626_trend_preset(
            area_timeframe="5m",
            rr=6,
            confirm_bars=2,
            sl_ticks=80,
            trail_enabled=True,
            trail_trigger=0.50,
            trail_ticks=10,
            full_tp_lock=2,
        ),
    )
    return presets


def choose_preset(presets: Dict[str, Dict[str, Any]], token: str) -> Tuple[str, Dict[str, Any]]:
    token_l = token.lower()
    matches = [(name, params) for name, params in presets.items() if token_l in name.lower()]
    if not matches:
        raise SystemExit(f"找不到 preset: {token}")
    matches.sort(key=lambda x: x[0])
    return matches[-1]


def params_from_raw(raw: Dict[str, Any]) -> StrategyParams:
    fields = {f.name for f in dataclasses.fields(StrategyParams)}
    return StrategyParams(**{k: v for k, v in raw.items() if k in fields})


def newest_topstep_date(live_rows: List[Dict[str, Any]]) -> date:
    dates: List[date] = []
    for row in live_rows:
        ts = parse_ts(row.get("entry_time") or row.get("exit_time"))
        if ts:
            dates.append(date.fromisoformat(_topstep_trade_date(ts)))
    if not dates:
        raise SystemExit("live exits/trade history 裡沒有可用時間")
    return max(dates)


def previous_thursday(today: date) -> date:
    # Monday=0, Thursday=3. If today is Thursday, "last Thursday" means a week ago.
    delta = (today.weekday() - 3) % 7
    if delta == 0:
        delta = 7
    return today - timedelta(days=delta)


def build_date_plan(today: date, presets: Dict[str, Dict[str, Any]]) -> Dict[str, Tuple[str, Dict[str, Any]]]:
    claude1 = choose_preset(presets, "CLAUDE #1")
    codex3 = choose_preset(presets, "CODEX #3")
    codex1 = choose_preset(presets, "CODEX #1")

    plan: Dict[str, Tuple[str, Dict[str, Any]]] = {}
    plan[today.isoformat()] = claude1
    plan[(today - timedelta(days=1)).isoformat()] = codex3

    start = previous_thursday(today)
    end = today - timedelta(days=2)
    d = start
    while d <= end:
        plan[d.isoformat()] = codex1
        d += timedelta(days=1)
    return dict(sorted(plan.items()))


def run_backtest(raw_params: Dict[str, Any], candles: List[Any]) -> List[Trade]:
    params = params_from_raw(raw_params)
    cid = params.contract_id
    config = BacktestConfig(
        strategies=[_normalize_strategy_name(params.strategy)],
        initial_capital=INITIAL_CAPITAL,
        symbol=_extract_symbol(cid),
        commission_rt=get_commission_rt(cid),
        fees_rt=get_fees_rt(cid),
        value_area_pct=params.value_area_pct,
    )
    engine = BacktestEngine(config, strategy_params=params)
    return list(engine.run(list(candles)).trades)


def direction_value(v: Any) -> str:
    if hasattr(v, "value"):
        return str(v.value).lower()
    return str(v or "").lower()


def exit_value(v: Any) -> str:
    if hasattr(v, "value"):
        return str(v.value).lower()
    return str(v or "").lower()


def rec_from_bt(trade: Trade, preset_name: str) -> Rec:
    et = parse_ts(trade.entry_time.isoformat()) or trade.entry_time
    xt = parse_ts(trade.exit_time.isoformat()) if trade.exit_time else None
    meta = dict(getattr(trade, "meta", None) or {})
    return Rec(
        source="backtest",
        preset_name=preset_name,
        trade_date=_topstep_trade_date(et),
        entry_time=et,
        exit_time=xt,
        direction=direction_value(trade.direction),
        entry_price=float(trade.entry_price),
        exit_price=float(trade.exit_price) if trade.exit_price is not None else None,
        sl_price=float(trade.sl_price) if trade.sl_price is not None else None,
        tp_price=float(trade.tp_price) if trade.tp_price is not None else None,
        original_tp_price=float(trade.original_tp_price) if trade.original_tp_price is not None else None,
        pnl=float(trade.pnl or 0.0),
        exit_reason=exit_value(trade.exit_reason),
        wall_id=str(meta.get("wall_id") or ""),
        zone_id=str(getattr(trade, "zone_id", "") or ""),
        trail_triggered=exit_value(trade.exit_reason) == "trail_sl",
        post_reached_tp=getattr(trade, "post_breakout_reached_tp", None),
        post_broke_trail_first=getattr(trade, "post_breakout_broke_trail_first", None),
        post_mfe_ticks=getattr(trade, "post_breakout_max_favorable_ticks", None),
    )


def load_live_records() -> List[Rec]:
    rows: List[Dict[str, Any]] = []
    if TRADE_HISTORY_FILE.exists():
        rows = json.load(open(TRADE_HISTORY_FILE, encoding="utf-8"))
    elif LIVE_EXITS_FILE.exists():
        rows = json.load(open(LIVE_EXITS_FILE, encoding="utf-8"))

    seen: set[Tuple[Any, ...]] = set()
    out: List[Rec] = []
    for row in rows:
        et = parse_ts(row.get("entry_time"))
        if not et:
            continue
        xt = parse_ts(row.get("exit_time"))
        direction = str(row.get("direction") or "").lower()
        entry_price = row.get("entry_price")
        if entry_price is None:
            continue
        key = (
            et.replace(microsecond=0).isoformat(),
            direction,
            round(float(entry_price), 2),
            round(float(row.get("exit_price") or 0), 2),
            str(row.get("exit_reason") or ""),
        )
        if key in seen:
            continue
        seen.add(key)

        pnl = row.get("topstep_pnl")
        if pnl is None:
            pnl = row.get("pnl")
        if pnl is None and row.get("exit_price") is not None:
            size = int(row.get("size") or 1)
            pv = 2.0 if "MNQ" in str(row.get("contract_id") or "") else 20.0
            gross = (float(row["exit_price"]) - float(entry_price)) * pv * size
            if direction == "sell":
                gross = -gross
            pnl = gross

        out.append(
            Rec(
                source="live",
                preset_name="live",
                trade_date=_topstep_trade_date(et),
                entry_time=et,
                exit_time=xt,
                direction=direction,
                entry_price=float(entry_price),
                exit_price=float(row.get("exit_price")) if row.get("exit_price") is not None else None,
                sl_price=float(row.get("sl_price")) if row.get("sl_price") is not None else None,
                tp_price=float(row.get("tp_price")) if row.get("tp_price") is not None else None,
                original_tp_price=(
                    float(row.get("original_tp_price"))
                    if row.get("original_tp_price") is not None
                    else (float(row.get("tp_price")) if row.get("tp_price") is not None else None)
                ),
                pnl=float(pnl or 0.0),
                exit_reason=str(row.get("exit_reason") or "").lower(),
                wall_id=str(row.get("wall_id") or ""),
                zone_id=str(row.get("zone_id") or ""),
                trail_triggered=bool(row.get("trail_triggered")) or str(row.get("exit_reason") or "").lower() == "trail_sl",
            )
        )
    return sorted(out, key=lambda r: r.entry_time)


def load_order_records() -> List[Dict[str, Any]]:
    if not ORDERS_FILE.exists():
        return []
    rows = json.load(open(ORDERS_FILE, encoding="utf-8"))
    out = []
    for row in rows:
        ts = parse_ts(row.get("entry_time") or row.get("exit_time"))
        if not ts:
            continue
        r = dict(row)
        r["_time"] = ts
        r["_trade_date"] = _topstep_trade_date(ts)
        r["_direction"] = str(row.get("direction") or "").lower()
        out.append(r)
    return sorted(out, key=lambda r: r["_time"])


def signed_entry_slip_ticks(live: Rec, bt: Rec) -> float:
    diff = (live.entry_price - bt.entry_price) / TICK_SIZE
    return diff if live.direction == "buy" else -diff


def signed_exit_slip_ticks(live: Rec, bt: Rec) -> Optional[float]:
    if live.exit_price is None or bt.exit_price is None:
        return None
    diff = (live.exit_price - bt.exit_price) / TICK_SIZE
    return -diff if live.direction == "buy" else diff


def matched_score(live: Rec, bt: Rec) -> Tuple[int, float]:
    if live.direction != bt.direction:
        return (10_000, float("inf"))
    dt = abs((live.entry_time - bt.entry_time).total_seconds())
    px_ticks = abs(live.entry_price - bt.entry_price) / TICK_SIZE
    if live.wall_id and bt.wall_id and live.wall_id == bt.wall_id:
        return (0, dt + px_ticks)
    if dt <= MATCH_WINDOW_MINUTES * 60 and px_ticks <= MATCH_PRICE_TICKS:
        return (1, dt + px_ticks * 10)
    return (10_000, float("inf"))


def match_records(live: List[Rec], bt: List[Rec]) -> Tuple[List[Tuple[Rec, Rec]], List[Rec], List[Rec]]:
    bt_used = [False] * len(bt)
    pairs: List[Tuple[Rec, Rec]] = []
    live_only: List[Rec] = []
    for l in live:
        best_i = None
        best = (10_000, float("inf"))
        for i, b in enumerate(bt):
            if bt_used[i]:
                continue
            score = matched_score(l, b)
            if score < best:
                best = score
                best_i = i
        if best_i is None or best[0] >= 10_000:
            live_only.append(l)
        else:
            bt_used[best_i] = True
            pairs.append((l, bt[best_i]))
    bt_only = [b for i, b in enumerate(bt) if not bt_used[i]]
    return pairs, live_only, bt_only


def classify_miss(bt: Rec, live: List[Rec], orders: List[Dict[str, Any]]) -> str:
    # Stuck/open-position approximation: a live position was already open when
    # this backtest entry would have happened.
    for l in live:
        if l.entry_time <= bt.entry_time and l.exit_time and bt.entry_time <= l.exit_time:
            return "stuck_on_last_position"

    # Time miss approximation: live had a cancelled pending order near the same
    # direction/time. data/trades.json does not preserve order creation for all
    # cancelled rows, so this is conservative.
    for o in orders:
        if o.get("_trade_date") != bt.trade_date:
            continue
        if o.get("status") != "cancelled" and o.get("exit_reason") != "cancelled":
            continue
        if o.get("_direction") != bt.direction:
            continue
        dt = abs((o["_time"] - bt.entry_time).total_seconds()) / 60.0
        if dt <= MISS_CANCEL_WINDOW_MINUTES:
            return "missed_by_time_or_unfilled_limit"
    return "missed_unknown_or_not_live_running"


def candle_hit_tp_after_exit(candles: List[Any], rec: Rec) -> Tuple[bool, Optional[float]]:
    if not rec.exit_time or rec.original_tp_price is None:
        return False, None
    deadline = rec.exit_time + timedelta(minutes=POST_TP_WINDOW_MINUTES)
    mfe = 0.0
    hit = False
    for c in candles:
        ts = parse_ts(c.timestamp.isoformat()) or c.timestamp
        if ts <= rec.exit_time:
            continue
        if ts > deadline:
            break
        if rec.direction == "buy":
            mfe = max(mfe, (float(c.high) - rec.entry_price) / TICK_SIZE)
            if float(c.high) >= rec.original_tp_price:
                hit = True
        else:
            mfe = max(mfe, (rec.entry_price - float(c.low)) / TICK_SIZE)
            if float(c.low) <= rec.original_tp_price:
                hit = True
    lost = None
    if hit and rec.exit_price is not None:
        lost = abs(rec.original_tp_price - rec.exit_price) / TICK_SIZE
    return hit, lost


def max_drawdown(records: List[Rec]) -> float:
    equity = INITIAL_CAPITAL
    peak = equity
    max_dd = 0.0
    for r in sorted(records, key=lambda x: x.exit_time or x.entry_time):
        equity += r.pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def score_records(records: List[Rec]) -> Dict[str, float]:
    if not records:
        return {"trades": 0, "pnl": 0.0, "losses": 0, "wins": 0, "maxdd": 0.0}
    wins = sum(1 for r in records if r.pnl > 0)
    losses = sum(1 for r in records if r.pnl < 0)
    return {
        "trades": float(len(records)),
        "pnl": sum(r.pnl for r in records),
        "losses": float(losses),
        "wins": float(wins),
        "maxdd": max_drawdown(records),
    }


def make_report(args: argparse.Namespace) -> str:
    presets = load_all_presets()
    live_all = load_live_records()
    order_rows = load_order_records()
    all_candles = list(candle_store.load("MNQ", 1))
    if not all_candles:
        raise SystemExit("data/store/MNQ_accumulated_1m.pkl 沒有 candle")

    today = date.fromisoformat(args.today) if args.today else newest_topstep_date(
        [dataclasses.asdict(r) | {"entry_time": r.entry_time.isoformat()} for r in live_all]
    )
    plan = build_date_plan(today, presets)
    if args.from_date:
        start = date.fromisoformat(args.from_date)
        plan = {d: p for d, p in plan.items() if date.fromisoformat(d) >= start}
    if args.to_date:
        end = date.fromisoformat(args.to_date)
        plan = {d: p for d, p in plan.items() if date.fromisoformat(d) <= end}

    min_plan_date = min(date.fromisoformat(d) for d in plan)
    max_plan_date = max(date.fromisoformat(d) for d in plan)
    warmup_start = (min_plan_date - timedelta(days=args.warmup_days)).isoformat()
    run_end = max_plan_date.isoformat()
    candles = [
        c for c in all_candles
        if warmup_start <= _topstep_trade_date(parse_ts(c.timestamp.isoformat()) or c.timestamp) <= run_end
    ]
    if not candles:
        raise SystemExit(f"找不到 {warmup_start}..{run_end} 的 candle")

    bt_by_date: Dict[str, List[Rec]] = {}
    used_presets: Dict[str, str] = {}
    for preset_name, raw in sorted(set((v[0], json.dumps(v[1], sort_keys=True)) for v in plan.values())):
        raw_params = json.loads(raw)
        trades = run_backtest(raw_params, candles)
        recs = [rec_from_bt(t, preset_name) for t in trades]
        for d, (name, _) in plan.items():
            if name == preset_name:
                bt_by_date[d] = [r for r in recs if r.trade_date == d]
                used_presets[d] = name

    live_by_date = {d: [r for r in live_all if r.trade_date == d] for d in plan}
    orders_by_date = {d: [r for r in order_rows if r.get("_trade_date") == d] for d in plan}

    all_pairs: List[Tuple[str, Rec, Rec]] = []
    all_live_only: List[Tuple[str, Rec]] = []
    all_bt_only: List[Tuple[str, Rec, str]] = []
    lines: List[str] = []
    lines.append("ORDER PRECISION AUDIT")
    lines.append(f"生成時間: {datetime.now().astimezone().isoformat(timespec='seconds')}")
    lines.append(f"日期定義: Topstep trade date, CT 17:00 reset")
    lines.append(f"今天參數: {today.isoformat()}  (可用 --today 覆寫)")
    lines.append(f"回測資料: {warmup_start}..{run_end} Topstep dates, warmup={args.warmup_days} days, candles={len(candles)}")
    lines.append("")
    lines.append("日期 / preset mapping:")
    for d, (name, _) in plan.items():
        lines.append(f"  {d}: {name}")
    lines.append("")

    for d in sorted(plan):
        live = live_by_date.get(d, [])
        bt = bt_by_date.get(d, [])
        pairs, live_only, bt_only = match_records(live, bt)
        all_pairs.extend((d, l, b) for l, b in pairs)
        all_live_only.extend((d, l) for l in live_only)
        for b in bt_only:
            all_bt_only.append((d, b, classify_miss(b, live, orders_by_date.get(d, []))))

        entry_slips = [signed_entry_slip_ticks(l, b) for l, b in pairs]
        abs_entry_slips = [abs(x) for x in entry_slips]
        time_deltas = [abs((l.entry_time - b.entry_time).total_seconds()) for l, b in pairs]
        exit_slips = [signed_exit_slip_ticks(l, b) for l, b in pairs]
        exit_slips = [x for x in exit_slips if x is not None]
        one_min_diff = sum(
            1
            for l, b in pairs
            if l.entry_time.replace(second=0, microsecond=0) == b.entry_time.replace(second=0, microsecond=0)
            and abs(l.entry_price - b.entry_price) >= TICK_SIZE
        )

        bt_score = score_records(bt)
        live_score = score_records(live)
        pair_live_pnl = sum(l.pnl for l, _ in pairs)
        pair_bt_pnl = sum(b.pnl for _, b in pairs)

        lines.append(f"[{d}] {used_presets.get(d, '')}")
        lines.append(
            f"  live {len(live)} / backtest {len(bt)} / matched {len(pairs)} "
            f"/ live-only {len(live_only)} / bt-only {len(bt_only)}"
        )
        lines.append(
            f"  PnL: live {fmt_signed_money(live_score['pnl'])}, "
            f"bt {fmt_signed_money(bt_score['pnl'])}, matched gap live-bt {fmt_signed_money(pair_live_pnl - pair_bt_pnl)}"
        )
        lines.append(
            f"  entry precision: avg adverse {fmt_num(safe_mean(entry_slips))}t, "
            f"avg abs {fmt_num(safe_mean(abs_entry_slips))}t, median abs {fmt_num(safe_median(abs_entry_slips))}t, "
            f"avg time delta {fmt_num(safe_mean(time_deltas), 1)}s"
        )
        lines.append(
            f"  exit precision: avg adverse {fmt_num(safe_mean(exit_slips))}t; "
            f"same-minute but different entry price {one_min_diff}/{len(pairs)}"
        )
        miss_counts: Dict[str, int] = {}
        for _, _, cls in [x for x in all_bt_only if x[0] == d]:
            miss_counts[cls] = miss_counts.get(cls, 0) + 1
        if miss_counts:
            lines.append("  missed bt orders:")
            for k, v in sorted(miss_counts.items()):
                lines.append(f"    {k}: {v}")
        lines.append("")

    pairs = all_pairs
    entry_slips = [signed_entry_slip_ticks(l, b) for _, l, b in pairs]
    abs_entry_slips = [abs(x) for x in entry_slips]
    exit_slips = [signed_exit_slip_ticks(l, b) for _, l, b in pairs]
    exit_slips = [x for x in exit_slips if x is not None]
    time_deltas = [abs((l.entry_time - b.entry_time).total_seconds()) for _, l, b in pairs]

    live_all_plan = [r for d in plan for r in live_by_date.get(d, [])]
    bt_all_plan = [r for d in plan for r in bt_by_date.get(d, [])]
    live_score = score_records(live_all_plan)
    bt_score = score_records(bt_all_plan)
    pair_live_pnl = sum(l.pnl for _, l, _ in pairs)
    pair_bt_pnl = sum(b.pnl for _, _, b in pairs)
    implementation_gap = live_score["pnl"] - bt_score["pnl"]

    miss_counts: Dict[str, int] = {}
    for _, _, cls in all_bt_only:
        miss_counts[cls] = miss_counts.get(cls, 0) + 1

    bt_trail_missed = []
    for r in bt_all_plan:
        if r.exit_reason == "trail_sl" and r.post_reached_tp and r.original_tp_price and r.exit_price:
            bt_trail_missed.append(abs(r.original_tp_price - r.exit_price) / TICK_SIZE)

    live_trail_missed = []
    for r in live_all_plan:
        if r.trail_triggered or r.exit_reason == "trail_sl":
            hit, lost = candle_hit_tp_after_exit(candles, r)
            if hit and lost is not None:
                live_trail_missed.append(lost)

    one_min_diff_total = sum(
        1
        for _, l, b in pairs
        if l.entry_time.replace(second=0, microsecond=0) == b.entry_time.replace(second=0, microsecond=0)
        and abs(l.entry_price - b.entry_price) >= TICK_SIZE
    )

    lines.append("總結:")
    lines.append(f"  日期數: {len(plan)}")
    lines.append(
        f"  live trades {int(live_score['trades'])}, backtest trades {int(bt_score['trades'])}, "
        f"matched {len(pairs)} ({pct(len(pairs), bt_score['trades'])} of bt)"
    )
    lines.append(
        f"  live PnL {fmt_signed_money(live_score['pnl'])}, bt PnL {fmt_signed_money(bt_score['pnl'])}, "
        f"implementation gap {fmt_signed_money(implementation_gap)}"
    )
    lines.append(
        f"  matched-only gap {fmt_signed_money(pair_live_pnl - pair_bt_pnl)} "
        f"(live {fmt_signed_money(pair_live_pnl)} vs bt {fmt_signed_money(pair_bt_pnl)})"
    )
    lines.append(
        f"  entry: avg adverse {fmt_num(safe_mean(entry_slips))} ticks, "
        f"avg abs {fmt_num(safe_mean(abs_entry_slips))} ticks, median abs {fmt_num(safe_median(abs_entry_slips))} ticks"
    )
    lines.append(
        f"  time: avg delta {fmt_num(safe_mean(time_deltas), 1)}s, "
        f"median delta {fmt_num(safe_median(time_deltas), 1)}s"
    )
    lines.append(f"  exit: avg adverse {fmt_num(safe_mean(exit_slips))} ticks")
    lines.append(f"  1m candle entry-price mismatch: {one_min_diff_total}/{len(pairs)} matched trades")
    lines.append("  backtest-only missed order classification:")
    for k, v in sorted(miss_counts.items()):
        lines.append(f"    {k}: {v}")
    if not miss_counts:
        lines.append("    none: 0")
    lines.append(
        f"  full TP missed after trail: backtest {len(bt_trail_missed)} "
        f"(avg lost {fmt_num(safe_mean(bt_trail_missed))}t), "
        f"live {len(live_trail_missed)} (avg lost {fmt_num(safe_mean(live_trail_missed))}t)"
    )

    bt_pnl = bt_score["pnl"]
    if abs(bt_pnl) > 1e-9:
        degradation = max(0.0, (bt_pnl - live_score["pnl"]) / abs(bt_pnl))
    else:
        degradation = 0.0
    projected_pnl = args.baseline_pnl * (1.0 - degradation)
    projected_dd = args.baseline_dd * (1.0 + degradation)
    lines.append("")
    lines.append("兩個月 full backtest 投影:")
    lines.append(
        f"  baseline: PnL {fmt_money(args.baseline_pnl)}, maxDD {fmt_money(args.baseline_dd)}"
    )
    lines.append(
        f"  用本次 precision gap 推估 degradation={degradation:.1%}: "
        f"PnL 約 {fmt_signed_money(projected_pnl)}, maxDD 約 {fmt_money(projected_dd)}"
    )
    lines.append("  注意: 如果本次 audit 期間 BT PnL 很小或 regime 不同，這個投影只適合作為風險折扣，不是統計保證。")
    lines.append("")
    lines.append("30s data 判斷:")
    lines.append("  30s candle 可以把 1m bar 內的 entry/exit 時間不確定性大約減半，對 same-minute price mismatch 和 trail/TP 先後順序最有幫助。")
    lines.append("  但它不能修正 broker fill latency、限價單排隊、API stuck pending order、或 live/backend 狀態不同步。")
    lines.append("  實作上需要從現在開始累積 30s store，並讓 backtest/live 共用同一個 30s bar builder；只收資料但回測仍用 1m，結果不會改善。")

    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    report = "\n".join(lines) + "\n"
    REPORT_FILE.write_text(report, encoding="utf-8")

    with open(DETAIL_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "trade_date", "live_entry_time", "bt_entry_time", "direction",
            "live_entry", "bt_entry", "entry_adverse_ticks", "time_delta_sec",
            "live_exit", "bt_exit", "exit_adverse_ticks", "live_pnl", "bt_pnl",
            "live_reason", "bt_reason", "live_wall_id", "bt_wall_id",
        ])
        for d, l, b in pairs:
            w.writerow([
                d, l.entry_time.isoformat(), b.entry_time.isoformat(), l.direction,
                l.entry_price, b.entry_price, signed_entry_slip_ticks(l, b),
                abs((l.entry_time - b.entry_time).total_seconds()),
                l.exit_price, b.exit_price, signed_exit_slip_ticks(l, b),
                l.pnl, b.pnl, l.exit_reason, b.exit_reason, l.wall_id, b.wall_id,
            ])
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--today", help="Topstep trade date to treat as today, e.g. 2026-07-02")
    parser.add_argument("--from-date", help="Optional lower Topstep trade-date bound")
    parser.add_argument("--to-date", help="Optional upper Topstep trade-date bound")
    parser.add_argument("--baseline-pnl", type=float, default=7000.0)
    parser.add_argument("--baseline-dd", type=float, default=700.0)
    parser.add_argument("--warmup-days", type=int, default=3)
    args = parser.parse_args()
    print(make_report(args))
    print(f"report: {REPORT_FILE}")
    print(f"pairs : {DETAIL_CSV}")


if __name__ == "__main__":
    main()
