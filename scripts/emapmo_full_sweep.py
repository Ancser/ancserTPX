"""1.0.9: EMAPMO 完整掃描 —— 網格改成「UI 真正能表達的參數空間」。

為什麼要重掃:
  舊的 backend/backtest/sweep.py:FACTOR_GRID 把 tp_value 寫成絕對 ATR 倍數,
  而且上限只到 4.0。但 UI 實際送出的是(frontend/static/ancserTPX.js:1197):
      factor_tp_rule  = factor_sl_rule          # TP 規則永遠鏡射 SL
      factor_tp_value = factor_sl_value * rr    # rr ∈ 1..6
  BEST preset = atr_blend × SL 2.5 × rr 3 → tp_value 7.5,**超出舊 grid 上限**,
  所以 sweep 從來沒測過它。實測 BEST PF 4.10 vs 舊 grid 冠軍(同族 TP2)2.95。

本腳本用 (sl_rule, sl_value, rr, exit_mode) 當座標,保證每個變體都能原樣存成
preset —— 掃出來的東西一定調得出來。

參數清單與 UI 完全對齊:
  sl_rule      atr / atr_blend      → sl_value ∈ {1, 1.5, 2, 2.5, 3}
               range15_pct          → sl_value ∈ {0.10, 0.15, 0.20, 0.50, 0.75}
  rr           1..6                 (exit_mode=ladder 時 rr 由引擎固定,不掃)
  side         all / long_only / short_only
  pmo_mode     normal / early / both
  exit_mode    tp / ladder
  va_filter    off / outside

用法:
  python scripts/emapmo_full_sweep.py --symbol MNQ
  python scripts/emapmo_full_sweep.py --symbol MES --pmo-scale 0.56
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from backend.api.routes import BacktestRequest, _build_strategy_params_from_request  # noqa: E402
from backend.backtest.sweep import ALL_SESSIONS, _run_one  # noqa: E402
from backend.data import candle_store  # noqa: E402
from backend.db.models import current_quarterly_contract_id, get_point_value  # noqa: E402

TICK = 0.25
MC_ITERS = 2000
MC_DD_LIMIT = 2000.0
SLIP_LEVELS = (1, 2, 4, 8, 14)
SLIP_MEASURED_TICKS = 14.0     # 有據的 EMAPMO 實盤成交(3.5 pts)

ATR_SL_VALUES = (1.0, 1.5, 2.0, 2.5, 3.0)
RANGE_SL_VALUES = (0.10, 0.15, 0.20, 0.50, 0.75)
RR_VALUES = (1, 2, 3, 4, 5, 6)
SIDES = ("all", "long_only", "short_only")
PMO_MODES = ("normal", "early", "both")
VA_FILTERS = ("off", "outside")

LOG = lambda *a: (print(*a), sys.stdout.flush())
_W: dict = {}


# ── grid ─────────────────────────────────────────────────────

def sl_specs():
    for rule in ("atr", "atr_blend"):
        for v in ATR_SL_VALUES:
            yield rule, v
    for v in RANGE_SL_VALUES:
        yield "range15_pct", v


def build_jobs():
    jobs = []
    for side in SIDES:
        for mode in PMO_MODES:
            for rule, sl in sl_specs():
                for va in VA_FILTERS:
                    for rr in RR_VALUES:
                        jobs.append((side, mode, rule, sl, rr, "tp", va))
                    # ladder 的 TP 階梯由引擎固定,rr 無作用 → 只掃一次
                    jobs.append((side, mode, rule, sl, 0, "ladder", va))
    return jobs


def label(job) -> str:
    side, mode, rule, sl, rr, exit_mode, va = job
    tp = "LADDER" if exit_mode == "ladder" else f"RR{rr}(TP{sl * rr:g})"
    return (f"EMAPMO {side} {mode} {rule} SL{sl:g} {tp}"
            + (" VA80" if va == "outside" else ""))


# ── worker ───────────────────────────────────────────────────

def _init_worker(symbol: str, pmo_scale: float) -> None:
    cid = current_quarterly_contract_id(symbol)
    req = BacktestRequest(contract_id=cid, contract_size=1)
    _W["base"] = _build_strategy_params_from_request(req, 1)
    _W["base"].contract_id = cid
    _W["candles"] = sorted(candle_store.load(symbol, 1), key=lambda c: c.timestamp)
    _W["scale"] = pmo_scale


def _apply(p, job):
    side, mode, rule, sl, rr, exit_mode, va = job
    p.strategy = "factor"
    p.area_timeframe = "session" if va == "outside" else "15m"
    p.value_area_pct = 0.80
    p.method = "single"
    p.tf_combo = []
    p.tr_allowed_sessions = list(ALL_SESSIONS)
    p.tr_one_trade_per_session = False
    p.one_trade_per_session_direction = False
    p.tr_exit_mode = exit_mode
    p.tr_daily_loss_stop = 1
    p.trail_enabled = False
    p.tr_trail_enabled = False
    p.rr_ratio = rr if rr else 2
    p.factor_timeframe_minutes = 5
    p.factor_signal_family = "emapmo"
    p.factor_side_mode = side
    p.factor_pmo_signal_mode = mode
    p.factor_session_va_filter = va
    # UI 契約:TP 規則鏡射 SL 規則,TP 值 = SL 值 × rr
    p.factor_sl_rule = rule
    p.factor_tp_rule = rule
    p.factor_sl_value = float(sl)
    p.factor_tp_value = float(sl * rr) if rr else float(sl * 2)
    p.factor_max_hold_bars = 0
    p.factor_max_trades_per_day = 3
    p.factor_warmup_bars = 150
    p.factor_pmo_threshold_scale = _W["scale"]
    return p


def _run_job(job) -> dict:
    p = _apply(copy.deepcopy(_W["base"]), job)
    r = _run_one(p, _W["candles"], None)
    side, mode, rule, sl, rr, exit_mode, va = job
    r["label"] = label(job)
    r["params"] = {
        "strategy": "factor",
        "tr_exit_mode": exit_mode,
        "rr_ratio": rr if rr else 2,
        "tr_allowed_sessions": list(ALL_SESSIONS),
        "tr_daily_loss_stop": 1,
        "area_timeframe": "session" if va == "outside" else "15m",
        "factor_timeframe_minutes": 5,
        "factor_signal_family": "emapmo",
        "factor_side_mode": side,
        "factor_pmo_signal_mode": mode,
        "factor_session_va_filter": va,
        "factor_sl_rule": rule,
        "factor_tp_rule": rule,
        "factor_sl_value": float(sl),
        "factor_tp_value": float(sl * rr) if rr else float(sl * 2),
        "factor_max_hold_bars": 0,
        "factor_max_trades_per_day": 3,
        "factor_warmup_bars": 150,
        "factor_pmo_threshold_scale": _W["scale"],
    }
    return r


# ── robustness ───────────────────────────────────────────────

def mc_test(pnls, seed=7) -> dict:
    a = np.asarray(pnls, float)
    n = a.size
    if n < 10:
        return {"mc_pass": False, "mc_reason": f"only {n} trades"}
    rng = np.random.default_rng(seed)
    samp = a[rng.integers(0, n, size=(MC_ITERS, n))]
    tot = samp.sum(axis=1)
    cum = samp.cumsum(axis=1)
    dd = (np.maximum.accumulate(cum, axis=1) - cum).max(axis=1)
    gains = np.where(samp > 0, samp, 0.0).sum(axis=1)
    losses = np.where(samp < 0, -samp, 0.0).sum(axis=1)
    pf = np.where(losses > 1e-9, gains / np.maximum(losses, 1e-9), 999.0)
    r = {
        "mc_pnl_p5": round(float(np.percentile(tot, 5)), 1),
        "mc_p_loss": round(float((tot <= 0).mean()), 3),
        "mc_dd_p95": round(float(np.percentile(dd, 95)), 1),
        "mc_pf_p5": round(float(np.percentile(pf, 5)), 3),
    }
    r["mc_pass"] = bool(r["mc_p_loss"] <= 0.05 and r["mc_dd_p95"] < MC_DD_LIMIT
                        and r["mc_pf_p5"] > 1.0)
    return r


def slipped(pnls, cost) -> dict:
    s = [p - cost for p in pnls]
    g = sum(x for x in s if x > 0)
    l = sum(-x for x in s if x < 0)
    eq = peak = dd = 0.0
    for x in s:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {"pnl": round(sum(s), 1),
            "pf": round((g / l) if l > 0 else (999.0 if g > 0 else 0.0), 3),
            "max_dd": round(dd, 1)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="MNQ")
    ap.add_argument("--pmo-scale", type=float, default=None,
                    help="EMAPMO 門檻縮放;預設 MNQ=1.0 / MES=0.56")
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--va-off", action="store_true",
                    help="只掃 va=off(第一次跑全 1890 格時,va=outside 那半邊要逐根建 "
                         "session volume profile,慢一個數量級且 BEST 本來就是 off)")
    args = ap.parse_args()
    if args.va_off:
        global VA_FILTERS
        VA_FILTERS = ("off",)

    scale = args.pmo_scale
    if scale is None:
        scale = 0.56 if args.symbol.upper() == "MES" else 1.0

    bars = sorted(candle_store.load(args.symbol, 1), key=lambda c: c.timestamp)
    if not bars:
        raise SystemExit(f"{args.symbol} store empty")
    tick_value = TICK * get_point_value(current_quarterly_contract_id(args.symbol))

    jobs = build_jobs()
    workers = args.workers or max(2, min(14, (os.cpu_count() or 8) - 2))
    LOG(f"[{args.symbol}] {len(bars)} bars "
        f"{bars[0].timestamp:%Y-%m-%d} → {bars[-1].timestamp:%Y-%m-%d}")
    LOG(f"[{args.symbol}] {len(jobs)} variants, pmo_scale={scale}, "
        f"{workers} workers, 1t=${tick_value:.2f}")

    t0 = time.time()
    results = []
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker,
                             initargs=(args.symbol, scale)) as pool:
        futures = [pool.submit(_run_job, j) for j in jobs]
        for i, fut in enumerate(as_completed(futures), start=1):
            r = fut.result()
            pnls = r.pop("_ordered_pnls", None) or []
            r["symbol"] = args.symbol
            r["pmo_scale"] = scale
            r["entry_type"] = "market"
            if pnls:
                r.update(mc_test(pnls))
                r["slip"] = {str(lv): slipped(pnls, lv * tick_value)
                             for lv in SLIP_LEVELS}
                r["pf_at_measured_slip"] = r["slip"][str(int(SLIP_MEASURED_TICKS))]["pf"]
            else:
                r["mc_pass"] = False
            r["long_term_ok"] = bool(
                r.get("mc_pass") and r.get("wf_pass")
                and r.get("trades", 0) >= 15 and r.get("max_dd", 9e9) < MC_DD_LIMIT)
            results.append(r)
            if i % 50 == 0 or i == len(jobs):
                LOG(f"  {i}/{len(jobs)}  ({time.time() - t0:.0f}s)  {r['label']}")

    results.sort(key=lambda x: -float(x.get("pf") or 0.0))
    out = Path(f"data/research/emapmo_full_sweep_{args.symbol}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "created_at": datetime.now(timezone.utc).isoformat(),
        "symbol": args.symbol, "pmo_scale": scale,
        "variants": len(results), "elapsed_s": round(time.time() - t0),
        "grid": {"sl_rules": ["atr", "atr_blend", "range15_pct"],
                 "atr_sl_values": list(ATR_SL_VALUES),
                 "range_sl_values": list(RANGE_SL_VALUES),
                 "rr_values": list(RR_VALUES),
                 "sides": list(SIDES), "pmo_modes": list(PMO_MODES),
                 "va_filters": list(VA_FILTERS)},
        "results": results,
    }, indent=1, default=str), encoding="utf-8")

    LOG(f"\n[{args.symbol}] done in {time.time() - t0:.0f}s")
    ok = [r for r in results if r.get("long_term_ok")]
    LOG(f"long_term_ok: {len(ok)} / {len(results)}")
    LOG("\n-- top 15 by PF (>=15 trades) --")
    for r in [x for x in results if x.get("trades", 0) >= 15][:15]:
        LOG(f"  {r['label']:<58} PF={r['pf']:<6} n={r['trades']:<4} "
            f"pnl=${r['pnl']:<9} dd=${r['max_dd']:<8} "
            f"wf={'Y' if r.get('wf_pass') else 'n'} "
            f"mc={'Y' if r.get('mc_pass') else 'n'} "
            f"PF@14t={r.get('pf_at_measured_slip')}"
            + ("  LT-OK" if r.get("long_term_ok") else ""))
    LOG(f"\nreport: {out}")


if __name__ == "__main__":
    main()
