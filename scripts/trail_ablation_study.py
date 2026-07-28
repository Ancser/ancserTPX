"""1.0.9: 移動停損(trail)消融實驗 —— trail 到底救了誰、殺了誰?

背景:死因分析(scripts/model_death_cause.py)顯示 TREND 的名目 RR 只實現了
47%(MNQ)/ 34%(MES),而輸單照樣吃滿 50t 的 SL。嫌疑犯是 trail
(trigger 0.3 / trail_sl 10t):贏單一走到 30% 就被 10 ticks 的移動停損掃出場。

另一個問題是目前四個模型的 trail 設定根本不一致:
    TREND / DAY ZONE   繼承 base_params → trail ON
    DISTRIBUTION       backend/backtest/sweep.py:496 寫死 OFF
    FACTOR             backend/backtest/sweep.py:556 寫死 OFF
所以「trail 好不好」從來沒有被公平比較過。

做法:monkeypatch sweep_mod._run_one,在每次回測前強制統一 trail 設定,
這樣四個家族的既有 grid 都能在相同 trail 條件下重跑,唯一變數就是 trail。

判定指標不是 PF,而是**每筆邊際(ticks)**:市價進場的實測往返滑價是 14t,
邊際低於它的模型無論回測多漂亮,實盤都是負的。

用法:
  python scripts/trail_ablation_study.py --symbol MNQ
  python scripts/trail_ablation_study.py --symbol MNQ --workers 6
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TICK = 0.25
POINT_VALUE = {"MNQ": 2.0, "MES": 5.0}
MEASURED_SLIP_TICKS = 14.0
MC_ITERS = 1000
MC_DD_LIMIT = 2000.0
MIN_TRADES = 15

# 受測的 trail 設定。OFF 是對照組;ON_TIGHT 是現行 TREND/DAY ZONE 的設定;
# ON_LOOSE 檢驗「是 trail 本身不好,還是只是收得太緊」。
TRAIL_CONFIGS = {
    "OFF":       {"enabled": False, "trigger": 0.0, "sl_ticks": 0},
    "ON_TIGHT":  {"enabled": True,  "trigger": 0.3, "sl_ticks": 10},
    "ON_LOOSE":  {"enabled": True,  "trigger": 0.5, "sl_ticks": 30},
}
FAMILIES = ("DAY ZONE", "DISTRIBUTION", "FACTOR")   # 1.0.9: TREND 已移除

LOG = lambda *a: (print(*a), sys.stdout.flush())


def tick_value(symbol: str) -> float:
    return TICK * POINT_VALUE.get(symbol, 2.0)


# ── worker ───────────────────────────────────────────────────

def _run_family(args) -> list:
    symbol, family, cfg_name = args
    cfg = TRAIL_CONFIGS[cfg_name]

    import backend.backtest.sweep as sweep_mod
    from backend.api.routes import BacktestRequest, _build_strategy_params_from_request
    from backend.data import candle_store
    from backend.db.models import current_quarterly_contract_id

    # 統一 trail:攔在 _run_one 前面,蓋掉各家族 grid 自己設的值
    real_run_one = sweep_mod._run_one

    def forced(p, candles, timeline):
        p.trail_enabled = cfg["enabled"]
        p.tr_trail_enabled = cfg["enabled"]
        p.trail_trigger_pct = cfg["trigger"]
        p.tr_trail_trigger_pct = cfg["trigger"]
        p.trail_sl_ticks = cfg["sl_ticks"]
        p.tr_trail_sl_ticks = cfg["sl_ticks"]
        return real_run_one(p, candles, timeline)

    sweep_mod._run_one = forced
    # acceptance 註解會 pop 掉 _ordered_pnls,這裡要留著算邊際
    real_annotate = sweep_mod._annotate_plateau_and_acceptance
    sweep_mod._annotate_plateau_and_acceptance = lambda rows: None

    cid = current_quarterly_contract_id(symbol)
    req = BacktestRequest(contract_id=cid, contract_size=1)
    base = _build_strategy_params_from_request(req, 1)
    base.contract_id = cid
    candles = sorted(candle_store.load(symbol, 1), key=lambda c: c.timestamp)

    runner = {
        "DAY ZONE": sweep_mod.run_day_zone_sweep,
        "DISTRIBUTION": sweep_mod.run_distribution_sweep,
        "FACTOR": sweep_mod.run_factor_sweep,
    }[family]
    try:
        rows = runner(candles, base, None)
    finally:
        sweep_mod._run_one = real_run_one
        sweep_mod._annotate_plateau_and_acceptance = real_annotate

    tv = tick_value(symbol)
    out = []
    for r in rows:
        pnls = r.pop("_ordered_pnls", None) or []
        n = r.get("trades") or 0
        rec = {
            "symbol": symbol, "family": family, "trail": cfg_name,
            "label": r.get("label"), "trades": n,
            "pf": r.get("pf"), "pnl": r.get("pnl"),
            "max_dd": r.get("max_dd"), "win_rate": r.get("win_rate"),
            "wf_pass": r.get("wf_pass"),
            "rr_ratio": (r.get("params") or {}).get("rr_ratio"),
        }
        rec["edge_ticks"] = (float(r.get("pnl") or 0) / n / tv) if n else None
        if pnls and n >= 10:
            rec.update(_mc(pnls))
            rec["pf_at_slip"] = _slipped_pf(pnls, MEASURED_SLIP_TICKS * tv)
        else:
            rec["mc_pass"] = False
            rec["pf_at_slip"] = None
        out.append(rec)
    return out


def _mc(pnls, seed=7) -> dict:
    a = np.asarray(pnls, float)
    n = a.size
    rng = np.random.default_rng(seed)
    samp = a[rng.integers(0, n, size=(MC_ITERS, n))]
    tot = samp.sum(axis=1)
    cum = samp.cumsum(axis=1)
    dd = (np.maximum.accumulate(cum, axis=1) - cum).max(axis=1)
    g = np.where(samp > 0, samp, 0.0).sum(axis=1)
    l = np.where(samp < 0, -samp, 0.0).sum(axis=1)
    pf = np.where(l > 1e-9, g / np.maximum(l, 1e-9), 999.0)
    p_loss = float((tot <= 0).mean())
    dd95 = float(np.percentile(dd, 95))
    pf5 = float(np.percentile(pf, 5))
    return {"mc_p_loss": round(p_loss, 3), "mc_dd_p95": round(dd95, 1),
            "mc_pf_p5": round(pf5, 3),
            "mc_pass": bool(p_loss <= 0.05 and dd95 < MC_DD_LIMIT and pf5 > 1.0)}


def _slipped_pf(pnls, cost) -> float:
    s = [p - cost for p in pnls]
    g = sum(x for x in s if x > 0)
    l = sum(-x for x in s if x < 0)
    return round((g / l) if l > 0 else (999.0 if g > 0 else 0.0), 3)


# ── report ───────────────────────────────────────────────────

def summarize(rows, symbol) -> None:
    LOG("\n" + "=" * 86)
    LOG(f"{symbol}: trail 消融 —— 每筆邊際是實盤生死線(需 > {MEASURED_SLIP_TICKS:g}t)")
    LOG("=" * 86)
    LOG(f"{'家族':<14}{'trail':<11}{'變體':>6}{'最佳PF':>9}{'最佳邊際':>11}"
        f"{'中位邊際':>11}{'>14t':>8}{'WF過':>7}{'MC過':>7}")
    for fam in FAMILIES:
        for cfg in TRAIL_CONFIGS:
            sel = [r for r in rows if r["family"] == fam and r["trail"] == cfg
                   and (r["trades"] or 0) >= MIN_TRADES]
            if not sel:
                LOG(f"{fam:<14}{cfg:<11}{'(無合格變體)':>6}")
                continue
            es = [r["edge_ticks"] for r in sel if r["edge_ticks"] is not None]
            LOG(f"{fam:<14}{cfg:<11}{len(sel):>6}"
                f"{max(r['pf'] or 0 for r in sel):>9.3f}"
                f"{max(es):>10.1f}t{np.median(es):>10.1f}t"
                f"{sum(1 for e in es if e > MEASURED_SLIP_TICKS):>8}"
                f"{sum(1 for r in sel if r['wf_pass']):>7}"
                f"{sum(1 for r in sel if r.get('mc_pass')):>7}")
        LOG("")

    LOG("=" * 86)
    LOG("每個家族的最佳 trail 設定(以最佳邊際排序)")
    LOG("=" * 86)
    for fam in FAMILIES:
        LOG(f"\n[{fam}]")
        for cfg in TRAIL_CONFIGS:
            sel = [r for r in rows if r["family"] == fam and r["trail"] == cfg
                   and (r["trades"] or 0) >= MIN_TRADES and r["edge_ticks"] is not None]
            if not sel:
                continue
            b = max(sel, key=lambda r: r["edge_ticks"])
            live = "可實盤" if b["edge_ticks"] > MEASURED_SLIP_TICKS else "撐不住滑價"
            LOG(f"  {cfg:<10} {b['label'][:34]:<34} PF={b['pf']:<6} "
                f"n={b['trades']:<5} edge={b['edge_ticks']:+7.1f}t "
                f"PF@14t={b['pf_at_slip']}  {live}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="MNQ")
    ap.add_argument("--workers", type=int, default=0)
    args = ap.parse_args()

    jobs = [(args.symbol, fam, cfg) for fam in FAMILIES for cfg in TRAIL_CONFIGS]
    workers = args.workers or max(2, min(12, (os.cpu_count() or 8) - 4))
    LOG(f"[{args.symbol}] {len(jobs)} 個 (家族 x trail) 組合, {workers} workers")
    LOG(f"  trail 設定: " + json.dumps(TRAIL_CONFIGS))

    t0 = time.time()
    rows = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_run_family, j): j for j in jobs}
        for i, fut in enumerate(as_completed(futs), start=1):
            j = futs[fut]
            try:
                out = fut.result()
            except Exception as exc:
                LOG(f"  [{i}/{len(jobs)}] {j[1]} {j[2]} FAILED: {exc}")
                continue
            rows.extend(out)
            LOG(f"  [{i}/{len(jobs)}] {j[1]:<13} {j[2]:<9} "
                f"{len(out):>4} variants  ({time.time() - t0:.0f}s)")

    out_path = Path(f"data/research/trail_ablation_{args.symbol}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "created_at": datetime.now(timezone.utc).isoformat(),
        "symbol": args.symbol,
        "measured_slip_ticks": MEASURED_SLIP_TICKS,
        "trail_configs": TRAIL_CONFIGS,
        "elapsed_s": round(time.time() - t0),
        "results": rows,
    }, indent=1, default=str), encoding="utf-8")

    summarize(rows, args.symbol)
    LOG(f"\nreport: {out_path}  ({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
