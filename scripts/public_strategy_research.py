"""1.0.9: 公開策略研究掃描 —— 用現有 BacktestEngine 跑外部策略構想。

把 backend/strategy/research_lab.py 的策略類別塞進 BacktestEngine 的策略
插槽(monkeypatch 建構分派),因此成交假設、佣金費用、時段、每日上限、
trail 全部與 BEST / OR15 完全相同 —— 這些外部策略是在同一條起跑線上被比較。

判定沿用同一套關卡:
  G0 訊號足夠      trades >= 15
  G1 帳面獲利      pf > 1.0
  G2 撐得住滑價    每筆邊際 > 14t 實測往返滑價   ← 實盤生死線
  G3 走查          三段日期各自獲利
  G4 蒙地卡羅      P(虧)<=5% 且 ddP95<$2k 且 PF_P5>1.0

用法:
  python scripts/public_strategy_research.py --symbol MNQ
  python scripts/public_strategy_research.py --symbol MES --workers 8
"""
from __future__ import annotations

import argparse
import itertools
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
MC_ITERS = 2000
MC_DD_LIMIT = 2000.0
MIN_TRADES = 15
LOG = lambda *a: (print(*a), sys.stdout.flush())


# 每個策略的參數網格。刻意保持小 —— 樣本只有 ~2.5 個月,格點開太大
# 等於在噪音裡挑冠軍(前面 EMAPMO 已經踩過這個坑)。
GRIDS = {
    "ORB":      {"research_or_minutes": [5, 15, 30, 60], "factor_side_mode": ["all"],
                 "research_tf_minutes": [5]},
    "VWAPREV":  {"research_k": [1.5, 2.0, 2.5, 3.0], "factor_side_mode": ["all"],
                 "research_tf_minutes": [5]},
    "IBS":      {"research_ibs_low": [0.05, 0.10, 0.20], "research_tf_minutes": [5, 15],
                 "factor_side_mode": ["all", "long_only"]},
    "RSI2":     {"research_rsi_low": [2.0, 5.0, 10.0], "research_tf_minutes": [5, 15],
                 "factor_side_mode": ["all", "long_only"]},
    "GAPFADE":  {"research_gap_atr": [0.3, 0.5, 1.0], "factor_side_mode": ["all"],
                 "research_tf_minutes": [5]},
    "INTRAMOM": {"research_first_minutes": [30, 60], "research_entry_hour": [18, 19],
                 "factor_side_mode": ["all"], "research_tf_minutes": [5]},
    "DONCHIAN": {"research_lookback": [10, 20, 40], "research_tf_minutes": [5, 15],
                 "factor_side_mode": ["all"]},
    "BBREV":    {"research_bb_len": [20, 50], "research_k": [2.0, 2.5],
                 "research_tf_minutes": [5, 15], "factor_side_mode": ["all"]},
}
# 風險口徑與 FACTOR 一致,讓 SL/TP 寬度可比
RISK_GRID = {"factor_sl_value": [1.5, 2.5], "rr_ratio": [2, 3]}


def tick_value(symbol: str) -> float:
    return TICK * POINT_VALUE.get(symbol, 2.0)


def build_jobs():
    jobs = []
    for name, grid in GRIDS.items():
        keys = list(grid) + list(RISK_GRID)
        vals = [grid[k] for k in grid] + [RISK_GRID[k] for k in RISK_GRID]
        for combo in itertools.product(*vals):
            jobs.append((name, dict(zip(keys, combo))))
    return jobs


_W: dict = {}


def _init(symbol: str):
    from backend.api.routes import BacktestRequest, _build_strategy_params_from_request
    from backend.data import candle_store
    from backend.db.models import current_quarterly_contract_id
    cid = current_quarterly_contract_id(symbol)
    req = BacktestRequest(contract_id=cid, contract_size=1)
    base = _build_strategy_params_from_request(req, 1)
    base.contract_id = cid
    _W["base"] = base
    _W["candles"] = sorted(candle_store.load(symbol, 1), key=lambda c: c.timestamp)
    _W["symbol"] = symbol


def _run_job(job):
    import copy
    from backend.backtest.engine import BacktestConfig, BacktestEngine
    from backend.db.models import (_extract_symbol, get_commission_rt, get_fees_rt)
    from backend.strategy.research_lab import RESEARCH_STRATEGIES

    name, cfg = job
    p = copy.deepcopy(_W["base"])
    for k, v in cfg.items():
        setattr(p, k, v)
    # FACTOR 的每日上限/日虧鎖沿用 BEST 的口徑,才能公平比較
    p.factor_max_trades_per_day = 3
    p.tr_daily_loss_stop = 1
    p.trail_enabled = False
    p.tr_trail_enabled = False
    p.strategy = "factor"          # 走 factor 分派,稍後被 patch 換掉

    cid = p.contract_id
    conf = BacktestConfig(
        strategies=["trend"], initial_capital=50_000.0,
        symbol=_extract_symbol(cid), commission_rt=get_commission_rt(cid),
        fees_rt=get_fees_rt(cid), value_area_pct=0.80,
    )
    eng = BacktestEngine(config=conf, strategy_params=p,
                         zone_timeline=None, record_equity=False)
    # 換掉策略插槽 —— 引擎其餘管線(成交、成本、時段、日鎖)完全不動
    eng.trend_follow = RESEARCH_STRATEGIES[name](p)
    eng._pending_max_age = eng.trend_follow.PENDING_TIMEOUT_CANDLES
    res = eng.run(_W["candles"])

    trades = [{"entry_time": str(t.entry_time), "pnl": float(t.pnl or 0.0)}
              for t in res.trades]
    return {"strategy": name, "params": cfg, "trades": trades}


# ── 驗證 ─────────────────────────────────────────────────────

def evaluate(rec, symbol):
    tv = tick_value(symbol)
    pn = np.array([t["pnl"] for t in rec["trades"]], float)
    out = {"strategy": rec["strategy"], "params": rec["params"],
           "n": int(pn.size), "symbol": symbol}
    if pn.size == 0:
        out.update({"gate": "G0 無訊號", "pf": 0.0}); return out
    g, l = pn[pn > 0].sum(), -pn[pn < 0].sum()
    pf = float(g / l) if l > 0 else 99.0
    eq = np.cumsum(pn)
    out.update({
        "pf": round(pf, 3), "pnl": round(float(pn.sum()), 1),
        "win": round(float((pn > 0).mean()), 3),
        "max_dd": round(float((np.maximum.accumulate(eq) - eq).max()), 1),
        "edge_ticks": round(float(pn.mean() / tv), 1),
    })
    if pn.size < MIN_TRADES:
        out["gate"] = "G0 訊號不足"; return out
    if pf <= 1.0:
        out["gate"] = "G1 帳面就虧"; return out
    if out["edge_ticks"] <= MEASURED_SLIP_TICKS:
        out["gate"] = "G2 邊際小於滑價"; return out
    # 走查
    ts = [np.datetime64(t["entry_time"][:19]) for t in rec["trades"]]
    order = np.argsort(ts); pns = pn[order]
    k = len(pns) // 3
    segs = [pns[:k], pns[k:2 * k], pns[2 * k:]]
    seg_pf = []
    for s in segs:
        if not s.size: seg_pf.append(0.0); continue
        sg, sl_ = s[s > 0].sum(), -s[s < 0].sum()
        seg_pf.append(float(sg / sl_) if sl_ > 0 else 99.0)
    out["seg_pf"] = [round(x, 2) for x in seg_pf]
    out["wf_pass"] = all(s.sum() > 0 and p > 1.0 for s, p in zip(segs, seg_pf))
    if not out["wf_pass"]:
        out["gate"] = "G3 走查失敗"; return out
    # 蒙地卡羅
    rng = np.random.default_rng(7)
    samp = pn[rng.integers(0, pn.size, size=(MC_ITERS, pn.size))]
    tot = samp.sum(axis=1); cum = samp.cumsum(axis=1)
    dd = (np.maximum.accumulate(cum, axis=1) - cum).max(axis=1)
    gg = np.where(samp > 0, samp, 0).sum(axis=1)
    ll = np.where(samp < 0, -samp, 0).sum(axis=1)
    pfs = np.where(ll > 1e-9, gg / np.maximum(ll, 1e-9), 99.0)
    out.update({"mc_p_loss": round(float((tot <= 0).mean()), 3),
                "mc_dd_p95": round(float(np.percentile(dd, 95)), 1),
                "mc_pf_p5": round(float(np.percentile(pfs, 5)), 3)})
    out["mc_pass"] = bool(out["mc_p_loss"] <= 0.05
                          and out["mc_dd_p95"] < MC_DD_LIMIT
                          and out["mc_pf_p5"] > 1.0)
    out["gate"] = "PASS" if out["mc_pass"] else "G4 蒙地卡羅失敗"
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="MNQ")
    ap.add_argument("--workers", type=int, default=0)
    args = ap.parse_args()

    jobs = build_jobs()
    workers = args.workers or max(2, min(12, (os.cpu_count() or 8) - 4))
    LOG(f"[{args.symbol}] {len(jobs)} 個變體 / {len(GRIDS)} 個策略族, {workers} workers")
    LOG(f"  滑價門檻 {MEASURED_SLIP_TICKS:g}t = ${MEASURED_SLIP_TICKS*tick_value(args.symbol):.2f}/口\n")

    t0 = time.time(); rows = []
    with ProcessPoolExecutor(max_workers=workers, initializer=_init,
                             initargs=(args.symbol,)) as pool:
        futs = {pool.submit(_run_job, j): j for j in jobs}
        for i, f in enumerate(as_completed(futs), 1):
            try:
                rows.append(evaluate(f.result(), args.symbol))
            except Exception as exc:
                j = futs[f]
                LOG(f"  [{i}/{len(jobs)}] {j[0]} FAILED: {type(exc).__name__}: {exc}")
                continue
            if i % 20 == 0 or i == len(jobs):
                LOG(f"  {i}/{len(jobs)}  ({time.time()-t0:.0f}s)")

    out = Path(f"data/research/public_strategies_{args.symbol}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"created_at": datetime.now(timezone.utc).isoformat(),
                               "symbol": args.symbol, "results": rows},
                              indent=1, default=str), encoding="utf-8")

    LOG(f"\n{'='*92}\n[{args.symbol}] 各策略族的最佳變體(依每筆邊際排序 —— 實盤生死線)\n{'='*92}")
    LOG(f"{'策略':<11}{'變體':>5}{'最佳PF':>9}{'筆數':>6}{'邊際':>9}{'PnL':>9}"
        f"{'maxDD':>8}{'走查':>6}{'MC':>5}   最遠關卡")
    for name in GRIDS:
        sel = [r for r in rows if r["strategy"] == name]
        elig = [r for r in sel if r.get("n", 0) >= MIN_TRADES]
        if not elig:
            LOG(f"{name:<11}{len(sel):>5}{'—':>9}{'—':>6}   (所有變體訊號不足)")
            continue
        b = max(elig, key=lambda r: r.get("edge_ticks", -1e9))
        far = max(sel, key=lambda r: ["G0", "G1", "G2", "G3", "G4", "PA"].index(r["gate"][:2]))
        LOG(f"{name:<11}{len(sel):>5}{max(r['pf'] for r in elig):>9.2f}{b['n']:>6}"
            f"{b['edge_ticks']:>8.1f}t{b['pnl']:>+9.0f}{b['max_dd']:>8.0f}"
            f"{'Y' if b.get('wf_pass') else 'n':>6}{'Y' if b.get('mc_pass') else 'n':>5}"
            f"   {far['gate']}")

    passed = [r for r in rows if r["gate"] == "PASS"]
    LOG(f"\n通過全部關卡: {len(passed)} / {len(rows)}")
    for r in sorted(passed, key=lambda r: -r["edge_ticks"]):
        LOG(f"  {r['strategy']:<10} {json.dumps(r['params'], ensure_ascii=False)}")
        LOG(f"     PF={r['pf']} n={r['n']} 邊際={r['edge_ticks']}t PnL=${r['pnl']} "
            f"maxDD=${r['max_dd']} 走查={r['seg_pf']} ddP95=${r['mc_dd_p95']}")
    LOG(f"\nreport: {out}  ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
