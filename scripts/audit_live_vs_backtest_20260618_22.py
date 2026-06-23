"""Independent live-vs-backtest audit for 2026-06-18 through 2026-06-22 PT.

Read-only inputs:
  data/store/MNQ_accumulated_1m.pkl
  data/trade_history.json
  data/trades.json
  data/models/registry/*.json

Outputs:
  data/machinelearning/audit_20260618_22_report.md
  data/machinelearning/audit_20260618_22_trades.csv
  data/machinelearning/audit_20260618_22_results.json
  data/machinelearning/audit_20260618_22_equity.png

This script deliberately does not call the running API, alter the active model,
or touch live-engine state.
"""

from __future__ import annotations

import csv
import json
import math
import os
import pickle
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from dateutil.parser import isoparse

from backend.backtest.confluence_backtest import (
    ConfluenceBacktestConfig,
    ConfluenceBacktester,
    build_zone_timeline,
)
from backend.db.models import (
    BacktestConfig,
    Direction,
    get_commission_rt,
    get_fees_rt,
)
from backend.strategy.confluence import ConfluenceConfig, MAX_RECENCY_DEPTH
from backend.strategy.confluence_features import CONTEXT_WINDOW
from backend.strategy.confluence_scorer import ConfluenceScorer
from backend.strategy.consolidation import timeframes_for_base


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / "machinelearning"
STORE = DATA / "store" / "MNQ_accumulated_1m.pkl"
TIMELINE_CACHE = OUT / "audit_zone_timeline_20260623.pkl"

ACCOUNT_ID = 22373660
CONTRACT_ID = "CON.F.US.MNQ.U26"
CONTRACT_SIZE = 3
TICK_SIZE = 0.25
POINT_VALUE = 2.0
PT = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc

AUDIT_START_PT = datetime(2026, 6, 18, 0, 0, tzinfo=PT)
AUDIT_END_PT = datetime(2026, 6, 23, 0, 0, tzinfo=PT)

MODEL_A = "20260618_codex_rr3-band4-mintf2-production-baseline"
MODEL_B = "20260618_codex_rr3-band4-mintf2-production-baseline-02"

REPORT_MD = OUT / "audit_20260618_22_report.md"
TRADES_CSV = OUT / "audit_20260618_22_trades.csv"
RESULTS_JSON = OUT / "audit_20260618_22_results.json"
EQUITY_PNG = OUT / "audit_20260618_22_equity.png"


def parse_ts(value: str) -> datetime:
    """Ledger timestamps without an offset are UTC, not local time."""
    dt = isoparse(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def pt_text(value: datetime) -> str:
    return value.astimezone(PT).strftime("%Y-%m-%d %H:%M:%S")


def in_audit(value: datetime) -> bool:
    local = value.astimezone(PT)
    return AUDIT_START_PT <= local < AUDIT_END_PT


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def get_feature(record: dict, name: str) -> Optional[float]:
    for row in ((record.get("confluence") or {}).get("contributions") or []):
        if row.get("feature") == name:
            try:
                return float(row.get("value"))
            except (TypeError, ValueError):
                return None
    return None


def original_geometry(record: dict) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Reconstruct the entry-decision SL/TP from the saved risk feature.

    Old records predate original_sl_price/original_tp_price persistence, and
    signal.sl_price may have been mutated by the trail. risk_ticks is frozen at
    scoring time, so it is the safest source for original geometry.
    """
    entry = record.get("entry_price")
    if entry is None:
        return None, None, None
    risk_ticks = get_feature(record, "risk_ticks")
    if risk_ticks is None:
        sl = record.get("original_sl_price")
        if sl is None:
            sl = record.get("sl_price")
        if sl is None:
            return None, None, None
        risk_ticks = abs(float(entry) - float(sl)) / TICK_SIZE
    rr = float((record.get("config") or {}).get("rr") or 0.0)
    risk = risk_ticks * TICK_SIZE
    is_buy = str(record.get("direction")).lower() == "buy"
    sl = float(entry) - risk if is_buy else float(entry) + risk
    tp = float(entry) + rr * risk if is_buy else float(entry) - rr * risk
    return risk_ticks, sl, tp


def session_code(ts: datetime) -> str:
    """Same UTC buckets used by the frontend."""
    h, m = ts.astimezone(UTC).hour, ts.astimezone(UTC).minute
    if h >= 22 or h < 7:
        return "ASIA"
    if h < 11:
        return "EURO"
    if h < 13 or (h == 13 and m < 30):
        return "PRE"
    if h < 20:
        return "RTH"
    return "AH"


def risk_bucket(risk: Optional[float]) -> str:
    if risk is None:
        return "unknown"
    if risk < 20:
        return "<20t"
    if risk < 40:
        return "20-39t"
    if risk < 60:
        return "40-59t"
    if risk < 80:
        return "60-79t"
    if risk < 160:
        return "80-159t"
    return ">=160t"


def max_drawdown(pnls: Iterable[float]) -> Tuple[float, List[float]]:
    equity = 0.0
    peak = 0.0
    dd = 0.0
    curve = [0.0]
    for pnl in pnls:
        equity += float(pnl)
        curve.append(equity)
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    return dd, curve


def summarize(rows: List[dict], pnl_key: str = "pnl") -> dict:
    pnls = [float(r.get(pnl_key) or 0.0) for r in rows]
    dd, _ = max_drawdown(pnls)
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    return {
        "trades": len(rows),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(rows) if rows else 0.0,
        "pnl": sum(pnls),
        "max_dd": dd,
        "profit_factor": gross_win / gross_loss if gross_loss else (math.inf if gross_win else 0.0),
        "expectancy": sum(pnls) / len(rows) if rows else 0.0,
    }


def group_summary(rows: List[dict], field: str, pnl_key: str) -> List[dict]:
    groups: Dict[str, List[dict]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(field) or "unknown")].append(row)
    out = []
    for key, values in groups.items():
        summary = summarize(values, pnl_key)
        summary[field] = key
        out.append(summary)
    return sorted(out, key=lambda r: (-r["pnl"], r[field]))


def match_engine_to_topstep(engine_rows: List[dict], history_rows: List[dict]) -> List[dict]:
    used = set()
    out = []
    for engine in sorted(engine_rows, key=lambda r: parse_ts(r["entry_time"])):
        et = parse_ts(engine["entry_time"])
        candidates = []
        for idx, live in enumerate(history_rows):
            if idx in used:
                continue
            if str(live.get("direction")) != str(engine.get("direction")):
                continue
            lt = parse_ts(live["entry_time"])
            seconds = abs((lt - et).total_seconds())
            price_diff = abs(float(live["entry_price"]) - float(engine["entry_price"]))
            if seconds <= 180 and price_diff <= 2.0:
                candidates.append((seconds + price_diff * 30.0, idx, live, seconds, price_diff))
        if not candidates:
            continue
        _, idx, live, seconds, price_diff = min(candidates, key=lambda x: x[0])
        used.add(idx)

        risk_ticks, original_sl, original_tp = original_geometry(engine)
        direction_sign = 1.0 if engine["direction"] == "buy" else -1.0
        actual_gross = (
            (float(live["exit_price"]) - float(live["entry_price"]))
            * POINT_VALUE * float(live.get("size") or CONTRACT_SIZE) * direction_sign
        )
        actual_net = actual_gross - float(live.get("commission") or 0) - float(live.get("fees") or 0)
        confluence = engine.get("confluence") or {}
        contributions = confluence.get("contributions") or []
        top_positive = sorted(
            (x for x in contributions if float(x.get("contribution") or 0) > 0),
            key=lambda x: float(x.get("contribution") or 0),
            reverse=True,
        )[:3]
        top_negative = sorted(
            (x for x in contributions if float(x.get("contribution") or 0) < 0),
            key=lambda x: float(x.get("contribution") or 0),
        )[:3]
        out.append({
            "entry_time": et,
            "exit_time": parse_ts(live["exit_time"]),
            "direction": engine["direction"],
            "entry_price": float(live["entry_price"]),
            "actual_exit_price": float(live["exit_price"]),
            "actual_gross": actual_gross,
            "actual_net": actual_net,
            "engine_exit_reason": engine.get("exit_reason"),
            "topstep_exit_reason": live.get("exit_reason"),
            "risk_ticks": risk_ticks,
            "original_sl": original_sl,
            "original_tp": original_tp,
            "persisted_sl": engine.get("sl_price"),
            "persisted_tp": engine.get("tp_price"),
            "mode": confluence.get("mode"),
            "side": confluence.get("side"),
            "largest_tf": confluence.get("largest_tf"),
            "labels": confluence.get("labels") or [],
            "prob": confluence.get("prob"),
            "score": confluence.get("score"),
            "cluster_weight": confluence.get("cluster_weight"),
            "scorer": (engine.get("scorer") or {}).get("source"),
            "rr": (engine.get("config") or {}).get("rr"),
            "session": session_code(et),
            "risk_bucket": risk_bucket(risk_ticks),
            "top_positive": top_positive,
            "top_negative": top_negative,
            "engine_record": engine,
            "topstep_record": live,
            "ledger_time_diff_sec": seconds,
            "ledger_entry_diff": price_diff,
        })
    return out


class WindowedBacktester(ConfluenceBacktester):
    """Warm context before activation without placing pre-activation orders."""

    def __init__(self, *args, active_start: datetime, active_end: datetime, **kwargs):
        super().__init__(*args, **kwargs)
        self.active_start = active_start.astimezone(UTC)
        self.active_end = active_end.astimezone(UTC)

    def _maybe_open(self, candle, zones_by_tf=None, recent_candles=None):
        ts = candle.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if not (self.active_start <= ts < self.active_end):
            return
        return super()._maybe_open(candle, zones_by_tf, recent_candles)


def load_bars_and_timeline():
    with STORE.open("rb") as handle:
        bars = sorted(pickle.load(handle), key=lambda c: c.timestamp)
    last_ts = bars[-1].timestamp.isoformat()
    cache_key = {
        "bars": len(bars),
        "last_ts": last_ts,
        "store_mtime": STORE.stat().st_mtime_ns,
    }
    timeline = None
    if TIMELINE_CACHE.exists():
        try:
            with TIMELINE_CACHE.open("rb") as handle:
                cached = pickle.load(handle)
            if cached.get("key") == cache_key:
                timeline = cached.get("timeline")
                print(f"[audit] timeline cache hit: {len(timeline)} bars", flush=True)
        except Exception:
            timeline = None
    if timeline is None:
        print(f"[audit] building timeline: {len(bars)} bars", flush=True)
        timeline = build_zone_timeline(
            bars, timeframes_for_base(1), TICK_SIZE, MAX_RECENCY_DEPTH,
        )
        temp = TIMELINE_CACHE.with_suffix(".tmp")
        with temp.open("wb") as handle:
            pickle.dump({"key": cache_key, "timeline": timeline}, handle)
        os.replace(temp, TIMELINE_CACHE)
    return bars, timeline


def scorer_for(model_id: str) -> ConfluenceScorer:
    path = DATA / "models" / "registry" / f"{model_id}.json"
    return ConfluenceScorer.load(path)


def run_backtest_variant(
    name: str,
    bars,
    timeline,
    active_start: datetime,
    active_end: datetime,
    model_id: str,
    rr: float,
    min_prob: float,
    max_risk: Optional[float],
    trail_trigger: float,
    session_limit: bool = True,
) -> dict:
    prewarm = active_start.astimezone(UTC) - timedelta(minutes=CONTEXT_WINDOW + 2)
    stop = min(active_end.astimezone(UTC) + timedelta(hours=6), bars[-1].timestamp)
    indices = [
        i for i, candle in enumerate(bars)
        if prewarm <= candle.timestamp <= stop
    ]
    if not indices:
        raise RuntimeError(f"No bars for {name}")
    lo, hi = indices[0], indices[-1] + 1
    run_bars = bars[lo:hi]
    run_timeline = timeline[lo:hi]
    min_score = math.log(min_prob / (1.0 - min_prob)) if 0.0 < min_prob < 1.0 else 0.0

    signal_cfg = ConfluenceConfig(band_ticks=4.0, min_distinct_tf=2, rr=rr)
    signal_cfg.direction_mode = "auto"
    signal_cfg.tick_size = TICK_SIZE
    signal_cfg.enable_breakout = False
    signal_cfg.max_risk_ticks = max_risk
    signal_cfg.ev_floor = None
    signal_cfg.rr_grid = None

    run_cfg = ConfluenceBacktestConfig(
        wait_minutes=1,
        one_trade_per_session_direction=session_limit,
        timeframes=timeframes_for_base(1),
        min_score=min_score,
        base_minutes=1,
        trail_trigger_pct=trail_trigger,
        trail_lock_pct=0.05 if trail_trigger > 0 else 0.0,
        full_tp_lock=0,
    )
    bt_cfg = BacktestConfig(
        initial_capital=50000.0,
        symbol="MNQ",
        commission_rt=get_commission_rt(CONTRACT_ID),
        fees_rt=get_fees_rt(CONTRACT_ID),
    )
    bt = WindowedBacktester(
        signal_cfg=signal_cfg,
        run_cfg=run_cfg,
        contract_id=CONTRACT_ID,
        contract_size=CONTRACT_SIZE,
        bt_config=bt_cfg,
        scorer=scorer_for(model_id),
        active_start=active_start,
        active_end=active_end,
    )
    print(
        f"[audit] run {name}: {pt_text(active_start)} -> {pt_text(active_end)} "
        f"RR{rr:g} P{min_prob:g} R{max_risk} trail={trail_trigger}",
        flush=True,
    )
    result = bt.run(run_bars, zones_timeline=run_timeline)
    rows = []
    for trade in result.trades:
        if not (active_start <= trade.entry_time < active_end):
            continue
        meta = trade.meta or {}
        risk = abs(trade.entry_price - trade.original_sl_price) / TICK_SIZE
        rows.append({
            "trade_id": trade.trade_id,
            "entry_time": trade.entry_time,
            "exit_time": trade.exit_time,
            "direction": trade.direction.value,
            "entry_price": trade.entry_price,
            "exit_price": trade.exit_price,
            "sl_price": trade.sl_price,
            "original_sl_price": trade.original_sl_price,
            "original_tp_price": trade.original_tp_price,
            "pnl": trade.pnl,
            "exit_reason": trade.exit_reason.value if trade.exit_reason else None,
            "mode": meta.get("mode"),
            "side": meta.get("side"),
            "largest_tf": meta.get("largest_tf"),
            "labels": meta.get("labels") or [],
            "prob": meta.get("prob"),
            "score": meta.get("score"),
            "weight": meta.get("weight"),
            "features": meta.get("features") or {},
            "risk_ticks": risk,
            "risk_bucket": risk_bucket(risk),
            "session": session_code(trade.entry_time),
            "variant": name,
        })
    return {
        "name": name,
        "params": {
            "active_start": active_start.isoformat(),
            "active_end": active_end.isoformat(),
            "model_id": model_id,
            "rr": rr,
            "min_prob": min_prob,
            "max_risk": max_risk,
            "trail_trigger": trail_trigger,
            "session_limit": session_limit,
        },
        "summary": summarize(rows, "pnl"),
        "trades": rows,
    }


def match_live_to_backtest(live_rows: List[dict], bt_rows: List[dict]) -> Tuple[List[dict], List[dict]]:
    candidates = []
    for li, live in enumerate(live_rows):
        for bi, bt in enumerate(bt_rows):
            if live["direction"] != bt["direction"]:
                continue
            minutes = abs((live["entry_time"] - bt["entry_time"]).total_seconds()) / 60.0
            price = abs(live["entry_price"] - bt["entry_price"])
            same_tf = live.get("largest_tf") == bt.get("largest_tf")
            same_side = live.get("side") == bt.get("side")
            if minutes <= 30 and price <= 20:
                penalty = (0 if same_tf else 8) + (0 if same_side else 5)
                candidates.append((minutes * 2 + price * 3 + penalty, li, bi, minutes, price))
    used_live, used_bt = set(), set()
    matched = []
    for _, li, bi, minutes, price in sorted(candidates):
        if li in used_live or bi in used_bt:
            continue
        used_live.add(li)
        used_bt.add(bi)
        live, bt = live_rows[li], bt_rows[bi]
        same_tf = live.get("largest_tf") == bt.get("largest_tf")
        same_side = live.get("side") == bt.get("side")
        if minutes <= 2 and price <= 1 and same_tf and same_side:
            quality = "基本正確"
        elif minutes <= 10 and price <= 4 and same_tf and same_side:
            quality = "接近"
        else:
            quality = "不同"
        merged = dict(live)
        merged.update({
            "match_quality": quality,
            "bt_entry_time": bt["entry_time"],
            "bt_exit_time": bt["exit_time"],
            "bt_entry_price": bt["entry_price"],
            "bt_exit_price": bt["exit_price"],
            "bt_pnl": bt["pnl"],
            "bt_exit_reason": bt["exit_reason"],
            "bt_largest_tf": bt["largest_tf"],
            "bt_side": bt["side"],
            "bt_labels": bt["labels"],
            "bt_risk_ticks": bt["risk_ticks"],
            "entry_time_diff_min": minutes,
            "entry_price_diff": price,
            "pnl_diff_live_minus_bt": live["actual_net"] - bt["pnl"],
        })
        matched.append(merged)
    for li, live in enumerate(live_rows):
        if li not in used_live:
            merged = dict(live)
            merged.update({
                "match_quality": "實盤獨有",
                "bt_pnl": None,
                "pnl_diff_live_minus_bt": None,
            })
            matched.append(merged)
    bt_only = [bt for idx, bt in enumerate(bt_rows) if idx not in used_bt]
    return sorted(matched, key=lambda r: r["entry_time"]), bt_only


def feature_text(rows: List[dict]) -> str:
    return "; ".join(
        f"{x['feature']}={float(x.get('contribution') or 0):+.2f}"
        for x in rows
    )


def md_table(headers: List[str], rows: List[List[object]]) -> str:
    line1 = "| " + " | ".join(headers) + " |"
    line2 = "| " + " | ".join("---" for _ in headers) + " |"
    body = [
        "| " + " | ".join(str(v).replace("|", "/") for v in row) + " |"
        for row in rows
    ]
    return "\n".join([line1, line2] + body)


def format_summary(name: str, summary: dict) -> List[object]:
    pf = summary["profit_factor"]
    return [
        name,
        summary["trades"],
        f"{summary['win_rate'] * 100:.1f}%",
        f"${summary['pnl']:.2f}",
        f"${summary['max_dd']:.2f}",
        "∞" if math.isinf(pf) else f"{pf:.2f}",
        f"${summary['expectancy']:.2f}",
    ]


def plot_equity(live_rows: List[dict], bt_rows: List[dict]) -> None:
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return
    _, live_curve = max_drawdown(r["actual_net"] for r in live_rows)
    _, bt_curve = max_drawdown(r["pnl"] for r in bt_rows)
    plt.figure(figsize=(11, 5))
    plt.plot(live_curve, label="Live net", color="#ff9f1a", linewidth=2)
    plt.plot(bt_curve, label="Backtest net", color="#64dcff", linewidth=2)
    plt.axhline(0, color="#777", linewidth=0.7)
    plt.title("2026-06-18 to 2026-06-22 PT: sequential equity")
    plt.xlabel("Trade sequence")
    plt.ylabel("Cumulative P&L ($)")
    plt.grid(alpha=0.15)
    plt.legend()
    plt.tight_layout()
    plt.savefig(EQUITY_PNG, dpi=150)
    plt.close()


def serialize(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Direction):
        return value.value
    raise TypeError(type(value).__name__)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    history = [
        row for row in load_json(DATA / "trade_history.json")
        if row.get("account_id") == ACCOUNT_ID and in_audit(parse_ts(row["entry_time"]))
    ]
    engine_all = [
        row for row in load_json(DATA / "trades.json")
        if row.get("account_id") == ACCOUNT_ID and in_audit(parse_ts(row.get("entry_time") or row["exit_time"]))
    ]
    engine_closed = [row for row in engine_all if row.get("status") == "closed"]
    live_rows = match_engine_to_topstep(engine_closed, history)
    if len(live_rows) != len(history):
        print(f"[audit] warning: matched {len(live_rows)}/{len(history)} Topstep trades", flush=True)

    first_by_model = {}
    for row in sorted(engine_all, key=lambda r: parse_ts(r.get("entry_time") or r["exit_time"])):
        source = (row.get("scorer") or {}).get("source")
        if source in (MODEL_A, MODEL_B) and source not in first_by_model:
            first_by_model[source] = parse_ts(row.get("entry_time") or row["exit_time"])
    start_a = first_by_model[MODEL_A]
    start_b = first_by_model[MODEL_B]
    end = AUDIT_END_PT.astimezone(UTC)

    bars, timeline = load_bars_and_timeline()
    variants = []
    # Historical segment A did not persist the trail toggle. Run both to make
    # that uncertainty explicit; A_off is used for the combined comparison.
    variants.append(run_backtest_variant(
        "A_RR1_P65_noRisk_trailOFF", bars, timeline, start_a, start_b,
        MODEL_A, 1.0, 0.65, None, 0.0,
    ))
    variants.append(run_backtest_variant(
        "A_RR1_P65_noRisk_trail50", bars, timeline, start_a, start_b,
        MODEL_A, 1.0, 0.65, None, 0.50,
    ))
    for risk in (None, 40, 60, 80, 100, 120, 160):
        variants.append(run_backtest_variant(
            f"B_RR5_POFF_R{risk or 'OFF'}_trail50",
            bars, timeline, start_b, end,
            MODEL_B, 5.0, 0.0, risk, 0.50,
        ))

    by_name = {v["name"]: v for v in variants}
    exact_bt = (
        by_name["A_RR1_P65_noRisk_trailOFF"]["trades"]
        + by_name["B_RR5_POFF_R80_trail50"]["trades"]
    )
    exact_bt.sort(key=lambda r: r["entry_time"])
    matched, bt_only = match_live_to_backtest(live_rows, exact_bt)

    live_summary = summarize(live_rows, "actual_net")
    bt_summary = summarize(exact_bt, "pnl")
    match_counts = Counter(r["match_quality"] for r in matched)

    live_groups = {
        "largest_tf": group_summary(live_rows, "largest_tf", "actual_net"),
        "risk_bucket": group_summary(live_rows, "risk_bucket", "actual_net"),
        "side": group_summary(live_rows, "side", "actual_net"),
        "session": group_summary(live_rows, "session", "actual_net"),
    }
    bt_groups = {
        "largest_tf": group_summary(exact_bt, "largest_tf", "pnl"),
        "risk_bucket": group_summary(exact_bt, "risk_bucket", "pnl"),
        "side": group_summary(exact_bt, "side", "pnl"),
        "session": group_summary(exact_bt, "session", "pnl"),
    }

    gaps = []
    sorted_live = sorted(live_rows, key=lambda r: r["entry_time"])
    for prev, cur in zip(sorted_live, sorted_live[1:]):
        hours = (cur["entry_time"] - prev["entry_time"]).total_seconds() / 3600
        gaps.append({
            "from": prev["entry_time"],
            "to": cur["entry_time"],
            "hours": hours,
        })
    cancel_count = sum(1 for r in engine_all if r.get("status") == "cancelled")
    closed_count = sum(1 for r in engine_all if r.get("status") == "closed")

    results = {
        "generated_at": datetime.now(UTC).isoformat(),
        "account_id": ACCOUNT_ID,
        "audit_range_pt": [AUDIT_START_PT.isoformat(), AUDIT_END_PT.isoformat()],
        "live_summary": live_summary,
        "backtest_summary": bt_summary,
        "match_counts": dict(match_counts),
        "bt_only_count": len(bt_only),
        "engine_cancelled": cancel_count,
        "engine_closed": closed_count,
        "live_groups": live_groups,
        "backtest_groups": bt_groups,
        "variants": variants,
        "matched": matched,
        "bt_only": bt_only,
        "gaps": gaps,
    }
    RESULTS_JSON.write_text(
        json.dumps(results, ensure_ascii=False, indent=2, default=serialize),
        encoding="utf-8",
    )

    csv_fields = [
        "entry_time_pt", "direction", "actual_net", "actual_exit_price",
        "match_quality", "bt_entry_time_pt", "bt_pnl", "pnl_diff_live_minus_bt",
        "entry_time_diff_min", "entry_price_diff", "largest_tf", "side", "mode",
        "risk_ticks", "risk_bucket", "prob", "cluster_weight", "session", "labels",
        "top_positive", "top_negative",
    ]
    with TRADES_CSV.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_fields)
        writer.writeheader()
        for row in matched:
            writer.writerow({
                "entry_time_pt": pt_text(row["entry_time"]),
                "direction": row["direction"],
                "actual_net": round(row["actual_net"], 2),
                "actual_exit_price": row["actual_exit_price"],
                "match_quality": row["match_quality"],
                "bt_entry_time_pt": pt_text(row["bt_entry_time"]) if row.get("bt_entry_time") else "",
                "bt_pnl": round(row["bt_pnl"], 2) if row.get("bt_pnl") is not None else "",
                "pnl_diff_live_minus_bt": (
                    round(row["pnl_diff_live_minus_bt"], 2)
                    if row.get("pnl_diff_live_minus_bt") is not None else ""
                ),
                "entry_time_diff_min": round(row.get("entry_time_diff_min") or 0, 2),
                "entry_price_diff": round(row.get("entry_price_diff") or 0, 2),
                "largest_tf": row.get("largest_tf"),
                "side": row.get("side"),
                "mode": row.get("mode"),
                "risk_ticks": round(row.get("risk_ticks") or 0, 1),
                "risk_bucket": row.get("risk_bucket"),
                "prob": row.get("prob"),
                "cluster_weight": row.get("cluster_weight"),
                "session": row.get("session"),
                "labels": ",".join(row.get("labels") or []),
                "top_positive": feature_text(row.get("top_positive") or []),
                "top_negative": feature_text(row.get("top_negative") or []),
            })

    summary_rows = [
        format_summary("Live actual net", live_summary),
        format_summary("Segment-matched backtest", bt_summary),
    ] + [
        format_summary(v["name"], v["summary"]) for v in variants
    ]
    trade_rows = []
    for row in matched:
        trade_rows.append([
            pt_text(row["entry_time"])[5:16],
            row["direction"],
            f"${row['actual_net']:+.0f}",
            row["match_quality"],
            "" if row.get("bt_pnl") is None else f"${row['bt_pnl']:+.0f}",
            row.get("largest_tf"),
            row.get("side"),
            f"{row.get('risk_ticks') or 0:.0f}t",
            ",".join(row.get("labels") or []),
            feature_text(row.get("top_positive") or []),
            feature_text(row.get("top_negative") or []),
        ])

    group_sections = []
    for field, title in [
        ("largest_tf", "Timeframe"),
        ("risk_bucket", "Risk bucket"),
        ("side", "VA side"),
        ("session", "Session"),
    ]:
        rows = []
        for item in live_groups[field]:
            rows.append([
                item[field], item["trades"], f"{item['win_rate']*100:.1f}%",
                f"${item['pnl']:.0f}", f"${item['max_dd']:.0f}",
            ])
        group_sections.append(
            f"### Live by {title}\n\n"
            + md_table([title, "N", "Win", "P&L", "DD"], rows)
        )

    largest_gaps = sorted(gaps, key=lambda x: x["hours"], reverse=True)[:10]
    gap_rows = [
        [pt_text(g["from"])[5:16], pt_text(g["to"])[5:16], f"{g['hours']:.1f}h"]
        for g in largest_gaps
    ]
    report = f"""# 2026-06-18 — 2026-06-22 Live vs Backtest Audit

Generated: {datetime.now(PT).isoformat(timespec="seconds")}

Account: `{ACCOUNT_ID}`  
Time interpretation: ledger timestamps without timezone are treated as UTC, then converted to Pacific.

## Executive metrics

{md_table(["Run", "N", "Win", "P&L", "MaxDD", "PF", "Expectancy"], summary_rows)}

Live/backtest entry matching: `{dict(match_counts)}`; backtest-only trades: `{len(bt_only)}`.

## Every live trade

{md_table(["PT entry", "Dir", "Live", "Match", "BT", "TF", "Side", "Risk", "Levels", "Top +", "Top -"], trade_rows)}

## Distribution

{chr(10).join(group_sections)}

## Trade clustering / silence

Engine records during the audit window: `{closed_count}` filled/closed and `{cancel_count}` cancelled one-shot limits.

{md_table(["Previous", "Next", "Gap"], gap_rows)}

## Important implementation facts

1. The clusterer uses 20/40/60/80/100% VA bands. It does **not** trade only VAH80/VAL80.
2. VAH and VAL levels never mix inside one cluster; at least two distinct timeframes must agree within 4 ticks.
3. Model `dist_to_price_ticks` is absolute (unsigned). The live universe display is signed: positive = level above market; negative = below.
4. Reversion: cluster above market -> short; below -> long. Momentum is the opposite. Therefore `VAL short` and `VAH long` are valid under the current implementation.
5. 80% VAH/VAL is used for the primary-zone drawing and SL search span, but the entry cluster may come from any band.
6. Current SL code selects the newest zone of the largest timeframe (`zones[-1]`), even when the contributing label is an older zone such as `4h-2`. This can make the displayed contributing wall and the SL source different.
7. Session lock is keyed by largest timeframe + direction, not a unique physical wall ID. Once a 4h/down lock is consumed, other 4h/down walls are blocked for the session.

## Files

- `{TRADES_CSV.name}`: row-level audit
- `{RESULTS_JSON.name}`: complete machine-readable results
- `{EQUITY_PNG.name}`: sequential equity comparison
"""
    REPORT_MD.write_text(report, encoding="utf-8")
    plot_equity(live_rows, exact_bt)
    print(f"[audit] wrote {REPORT_MD}", flush=True)
    print(f"[audit] wrote {TRADES_CSV}", flush=True)
    print(f"[audit] wrote {RESULTS_JSON}", flush=True)


if __name__ == "__main__":
    main()
