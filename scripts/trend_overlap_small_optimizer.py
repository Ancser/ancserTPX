"""Trend overlap-small optimizer.

Research question:
  - Require multiple trend area timeframes to overlap.
  - When the overlap gate is open, trade the smallest timeframe zone in the combo.
  - Find rows with maxDD < 2000 and PnL > 6000.

This is intentionally a research runner. It does not change the app's existing
overlap backtest path, which trades the synthetic averaged overlap zone.
"""
from __future__ import annotations

import copy
import csv
import itertools
import json
import time
from pathlib import Path
from typing import Iterable, Sequence

from backend.api.routes import _precompute_zone_timeline
from backend.backtest.engine import BacktestEngine
from backend.data import candle_store
from backend.db.models import (
    BacktestConfig,
    StrategyParams,
    _extract_symbol,
    get_commission_rt,
    get_fees_rt,
)


CONTRACT_ID = "CON.F.US.MNQ.U26"
INITIAL_CAPITAL = 50_000.0
TARGET_MAX_DD = 2_000.0
TARGET_PNL = 6_000.0
OUT_DIR = Path("data/machinelearning")
OUT_JSON = OUT_DIR / "trend_overlap_small_latest.json"
OUT_CSV = OUT_DIR / "trend_overlap_small_latest.csv"

TF_ORDER = ("5m", "15m", "30m", "1h", "4h")
TF_RANK = {tf: i for i, tf in enumerate(TF_ORDER)}


def _combo_label(combo: Sequence[str]) -> str:
    return "+".join(combo)


def _overlap_combos() -> list[tuple[str, ...]]:
    combos: list[tuple[str, ...]] = []
    for n in range(2, len(TF_ORDER) + 1):
        combos.extend(itertools.combinations(TF_ORDER, n))
    return combos


def _score_key(row: dict) -> tuple:
    passes = row["max_dd"] < TARGET_MAX_DD and row["pnl"] > TARGET_PNL
    return (
        1 if passes else 0,
        -abs(row["total_loss"]),
        row["calmar"],
        row["pnl"],
        -row["max_dd"],
        row["pf"],
    )


def _quality_key(row: dict) -> tuple:
    return (
        row["calmar"],
        row["pf"],
        row["pnl"],
        -row["max_dd"],
        -abs(row["total_loss"]),
    )


def _empty_entry() -> dict:
    return {"active": None, "recent": [], "mature": False, "overlap": 0}


def _overlap_small_timeline(per_tf: dict[str, list], combo: Sequence[str]) -> list[dict]:
    """Build a gated timeline.

    All combo timeframes must have overlapping value areas. If the gate is open,
    the strategy sees only the smallest timeframe zone. That keeps entry, SL, TP,
    and volume-profile SL geometry anchored to the smallest TF while the overlap
    combo remains visible through strategy metadata.
    """
    combo = tuple(sorted(combo, key=lambda tf: TF_RANK[tf]))
    timelines = [per_tf[tf] for tf in combo]
    n = min(len(tl) for tl in timelines)
    out: list[dict] = []
    cache: dict[tuple[str, ...], dict] = {}
    none_entry = _empty_entry()
    combo_label = _combo_label(combo)
    for i in range(n):
        actives = [tl[i].get("active") for tl in timelines]
        if any(z is None for z in actives):
            out.append(none_entry)
            continue
        lo = max(z.val_80 for z in actives)
        hi = min(z.vah_80 for z in actives)
        if lo > hi:
            out.append(none_entry)
            continue
        key = tuple(str(z.zone_id) for z in actives)
        entry = cache.get(key)
        if entry is None:
            small = copy.copy(actives[0])
            small.candles = []
            small.zone_id = "OS:" + combo_label + ":" + "+".join(key)
            small.parent_zone_id = "+".join(key)
            small.timeframe = combo[0]
            entry = {
                "active": small,
                "recent": [small],
                "mature": True,
                "overlap": len(actives),
                "overlap_tfs": list(combo),
                "overlap_zone_ids": list(key),
            }
            cache[key] = entry
        out.append(entry)
    return out


def _build_params(
    combo: Sequence[str],
    *,
    rr: int,
    confirm: int,
    sl_ticks: int,
    trail: bool,
    session_limit: bool,
    full_tp_lock: int,
) -> StrategyParams:
    combo = tuple(sorted(combo, key=lambda tf: TF_RANK[tf]))
    tp_ticks = int(sl_ticks * rr)
    return StrategyParams(
        strategy="trend",
        contract_id=CONTRACT_ID,
        contract_size=1,
        area_timeframe=combo[0],
        method="overlap",
        tf_combo=list(combo),
        value_area_pct=0.80,
        rr_ratio=int(rr),
        breakout_confirm_bars=int(confirm),
        sl_ticks=int(sl_ticks),
        tp_ticks=tp_ticks,
        tr_sl_ticks=int(sl_ticks),
        tr_tp_ticks=tp_ticks,
        trail_enabled=bool(trail),
        tr_trail_enabled=bool(trail),
        trail_trigger_pct=0.50 if trail else 0.0,
        tr_trail_trigger_pct=0.50 if trail else 0.0,
        trail_sl_ticks=10 if trail else 0,
        tr_trail_sl_ticks=10 if trail else 0,
        full_tp_lock=int(full_tp_lock),
        tr_full_tp_lock=int(full_tp_lock),
        one_trade_per_session_direction=True,
        tr_one_trade_per_session=bool(session_limit),
        tr_allowed_sessions=["ASIA"],
    )


def _metric_row(
    result,
    combo: Sequence[str],
    *,
    rr: int,
    confirm: int,
    sl_ticks: int,
    trail: bool,
    session_limit: bool,
    full_tp_lock: int,
    verified: bool = False,
) -> dict:
    m = result.metrics
    combo = tuple(sorted(combo, key=lambda tf: TF_RANK[tf]))
    return {
        "mode": "overlap_small",
        "combo": _combo_label(combo),
        "decision_tfs": _combo_label(combo),
        "overlap_tfs": _combo_label(combo),
        "trade_tf": combo[0],
        "rr": int(rr),
        "confirm": int(confirm),
        "sl_ticks": int(sl_ticks),
        "trail": "T50L10" if trail else "OFF",
        "session_limit": bool(session_limit),
        "full_tp_lock": int(full_tp_lock),
        "trades": int(m.total_trades),
        "wins": int(m.wins),
        "losses": int(m.losses),
        "win_rate": float(m.win_rate),
        "pnl": float(m.total_pnl),
        "max_dd": float(m.max_drawdown),
        "pf": float(m.profit_factor),
        "calmar": float(m.calmar_ratio),
        "expectancy": float(m.expectancy),
        "total_gain": float(getattr(m, "total_gain", 0.0)),
        "total_loss": float(getattr(m, "total_loss", 0.0)),
        "avg_win": float(m.avg_win),
        "avg_loss": float(m.avg_loss),
        "verified": bool(verified),
    }


def _interval_breakdown(trades: Iterable) -> list[dict]:
    buckets: dict[tuple, dict] = {}
    for t in trades:
        meta = getattr(t, "meta", None) or {}
        key = (
            "/".join(meta.get("decision_tfs") or []),
            "/".join(meta.get("overlap_tfs") or []) or "off",
            str(meta.get("trade_tf") or ""),
        )
        row = buckets.setdefault(
            key,
            {
                "decision_tfs": key[0],
                "overlap_tfs": key[1],
                "trade_tf": key[2],
                "trades": 0,
                "pnl": 0.0,
                "total_loss": 0.0,
            },
        )
        row["trades"] += 1
        row["pnl"] += float(t.pnl or 0.0)
        if (t.pnl or 0.0) < 0:
            row["total_loss"] += float(t.pnl or 0.0)
    return sorted(buckets.values(), key=lambda r: (r["decision_tfs"], r["trade_tf"]))


def _print_table(title: str, rows: Sequence[dict], limit: int = 20) -> None:
    print("\n" + title, flush=True)
    print(
        f"{'combo':<18} {'trTF':<4} {'RR':>2} {'C':>2} {'SL':>3} {'trail':>6} "
        f"{'ses':>3} {'FT':>2} {'trades':>6} {'win%':>6} {'pnl':>10} {'DD':>8} "
        f"{'loss':>10} {'PF':>5} {'Cal':>6}",
        flush=True,
    )
    for r in rows[:limit]:
        print(
            f"{r['combo']:<18} {r['trade_tf']:<4} {r['rr']:>2} {r['confirm']:>2} "
            f"{r['sl_ticks']:>3} {r['trail']:>6} {('ON' if r['session_limit'] else 'OFF'):>3} "
            f"{r['full_tp_lock']:>2} {r['trades']:>6} {100*r['win_rate']:>5.1f}% "
            f"{r['pnl']:>+10.1f} {r['max_dd']:>8.1f} {r['total_loss']:>10.1f} "
            f"{r['pf']:>5.2f} {r['calmar']:>6.2f}",
            flush=True,
        )


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    candles = candle_store.load("MNQ", 1)
    if not candles:
        raise SystemExit("No MNQ 1m candles in store.")
    candles.sort(key=lambda c: c.timestamp)
    config = BacktestConfig(
        strategies=["trend"],
        initial_capital=INITIAL_CAPITAL,
        symbol=_extract_symbol(CONTRACT_ID),
        commission_rt=get_commission_rt(CONTRACT_ID),
        fees_rt=get_fees_rt(CONTRACT_ID),
        value_area_pct=0.80,
    )

    print(
        f"candles {len(candles)} {candles[0].timestamp} -> {candles[-1].timestamp}",
        flush=True,
    )
    per_tf: dict[str, list] = {}
    t0 = time.time()
    for tf in TF_ORDER:
        started = time.time()
        per_tf[tf] = _precompute_zone_timeline(candles, 0.80, False, tf)
        print(f"timeline {tf:<3} {time.time() - started:.1f}s", flush=True)
    print(f"timeline build total {time.time() - t0:.1f}s", flush=True)

    overlap_cache: dict[tuple[str, ...], list] = {}

    def timeline_for(combo: Sequence[str]) -> list:
        combo = tuple(sorted(combo, key=lambda tf: TF_RANK[tf]))
        tl = overlap_cache.get(combo)
        if tl is None:
            started = time.time()
            tl = _overlap_small_timeline(per_tf, combo)
            overlap_cache[combo] = tl
            print(f"overlap-small {_combo_label(combo):<18} {time.time() - started:.1f}s", flush=True)
        return tl

    def run_one(combo, rr, confirm, sl_ticks, trail, session_limit, full_tp_lock, verified=False):
        params = _build_params(
            combo,
            rr=rr,
            confirm=confirm,
            sl_ticks=sl_ticks,
            trail=trail,
            session_limit=session_limit,
            full_tp_lock=full_tp_lock,
        )
        result = BacktestEngine(
            config=config,
            strategy_params=params,
            zone_timeline=timeline_for(combo),
            record_equity=False,
        ).run(candles)
        row = _metric_row(
            result,
            combo,
            rr=rr,
            confirm=confirm,
            sl_ticks=sl_ticks,
            trail=trail,
            session_limit=session_limit,
            full_tp_lock=full_tp_lock,
            verified=verified,
        )
        return row, result

    phase_a = []
    phase_a_grid = list(itertools.product(_overlap_combos(), range(2, 9), range(1, 5)))
    print(f"PHASE A {len(phase_a_grid)} runs", flush=True)
    for idx, (combo, rr, confirm) in enumerate(phase_a_grid, start=1):
        row, _ = run_one(combo, rr, confirm, 80, True, True, 0)
        phase_a.append(row)
        if idx % 40 == 0 or idx == len(phase_a_grid):
            print(f"  phase A {idx}/{len(phase_a_grid)}", flush=True)

    phase_a_pass = [
        r for r in phase_a
        if r["max_dd"] < TARGET_MAX_DD and r["pnl"] > TARGET_PNL
    ]
    phase_a_pass_rank = sorted(phase_a_pass, key=_score_key, reverse=True)
    phase_a_quality = sorted(phase_a, key=_quality_key, reverse=True)
    _print_table("PHASE A pass target", phase_a_pass_rank, 25)
    _print_table("PHASE A top quality", phase_a_quality, 25)

    seeds: list[tuple[str, int, int]] = []
    seen = set()
    for src in (phase_a_pass_rank[:8], phase_a_quality[:8]):
        for row in src:
            key = (row["combo"], row["rr"], row["confirm"])
            if key in seen:
                continue
            seen.add(key)
            seeds.append(key)
    forced = ("5m+15m", 4, 3)
    if forced not in seen:
        seeds.append(forced)
    seeds = seeds[:14]
    print("\nPHASE B seeds:", seeds, flush=True)

    phase_b = []
    phase_b_grid = []
    phase_b_seen = set()
    for combo_label, rr, confirm in seeds:
        combo = tuple(combo_label.split("+"))
        rr_values = sorted({max(1, rr - 1), rr, min(9, rr + 1)})
        confirm_values = sorted({max(1, confirm - 1), confirm, min(6, confirm + 1)})
        for values in itertools.product(
            rr_values,
            confirm_values,
            (40, 60, 80, 100),
            (False, True),
            (True, False),
            (0, 1, 2),
        ):
            grid_key = (combo, *values)
            if grid_key in phase_b_seen:
                continue
            phase_b_seen.add(grid_key)
            phase_b_grid.append(grid_key)
    print(f"PHASE B {len(phase_b_grid)} runs", flush=True)
    for idx, (combo, rr, confirm, sl_ticks, trail, session_limit, full_tp_lock) in enumerate(
        phase_b_grid, start=1
    ):
        row, _ = run_one(combo, rr, confirm, sl_ticks, trail, session_limit, full_tp_lock)
        phase_b.append(row)
        if idx % 80 == 0 or idx == len(phase_b_grid):
            print(f"  phase B {idx}/{len(phase_b_grid)}", flush=True)

    pass_rows = [
        r for r in phase_b
        if r["max_dd"] < TARGET_MAX_DD and r["pnl"] > TARGET_PNL
    ]
    pass_rank = sorted(pass_rows, key=_score_key, reverse=True)
    all_rank = sorted(phase_b, key=_score_key, reverse=True)
    _print_table("PASS target, sorted by lowest total loss", pass_rank, 40)
    _print_table("ALL best objective", all_rank, 40)

    verified = []
    breakdowns = {}
    print("\nFINAL VERIFY", flush=True)
    for row in pass_rank[:20]:
        combo = tuple(row["combo"].split("+"))
        v, result = run_one(
            combo,
            row["rr"],
            row["confirm"],
            row["sl_ticks"],
            row["trail"] != "OFF",
            row["session_limit"],
            row["full_tp_lock"],
            verified=True,
        )
        verified.append(v)
        session = "ON" if v["session_limit"] else "OFF"
        key = (
            f"{v['combo']}|RR{v['rr']}|C{v['confirm']}|SL{v['sl_ticks']}|"
            f"{v['trail']}|SES{session}|FT{v['full_tp_lock']}"
        )
        breakdowns[key] = _interval_breakdown(result.trades)
        print(
            f"  {v['combo']} trade={v['trade_tf']} RR{v['rr']} C{v['confirm']} SL{v['sl_ticks']} "
            f"{v['trail']} ses={'ON' if v['session_limit'] else 'OFF'} "
            f"FT{v['full_tp_lock']} -> trades={v['trades']} pnl={v['pnl']:.1f} "
            f"DD={v['max_dd']:.1f} loss={v['total_loss']:.1f} cal={v['calmar']:.2f}",
            flush=True,
        )
    verified_rank = sorted(verified, key=_score_key, reverse=True)
    _print_table("VERIFIED ranking", verified_rank, len(verified_rank))

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "target": {
            "max_dd_lt": TARGET_MAX_DD,
            "pnl_gt": TARGET_PNL,
            "mode": "trend overlap gate, trade smallest timeframe zone",
            "minimize": "abs(total_loss)",
        },
        "candles": {
            "count": len(candles),
            "start": candles[0].timestamp.isoformat(),
            "end": candles[-1].timestamp.isoformat(),
        },
        "phase_a_pass": phase_a_pass_rank,
        "phase_a_top": phase_a_quality[:100],
        "phase_b_pass": pass_rank,
        "phase_b_top": all_rank[:150],
        "verified": verified_rank,
        "interval_breakdowns": breakdowns,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    csv_rows = verified_rank + pass_rank[:200]
    fieldnames = list(csv_rows[0].keys()) if csv_rows else []
    if fieldnames:
        with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(csv_rows)
    print(f"\nSaved {OUT_JSON} and {OUT_CSV}", flush=True)


if __name__ == "__main__":
    main()
