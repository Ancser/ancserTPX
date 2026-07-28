# ============================================================
# 文件: scripts/robustness_sweep_mnq_mes.py
# 狀態: 1.0.9 新增 (MNQ + MES 全模型 robustness sweep)
# 目的: 跑標準 sweep grid(TREND / DAY ZONE / DISTRIBUTION / FACTOR)於
#       MNQ 與 MES,每個變體加上:
#         1. Monte Carlo bootstrap(PnL / maxDD / PF 分佈)
#         2. Walk-forward 三段(_run_one 內建 seg_pnls / seg_pfs)
#         3. Slippage 注入 — 由 data/trade_history.json 實盤成交 vs
#            回測假設進場(5m open)量測實際 slip,逐筆扣成本重算 PF
#       閘門: maxDD<2000、PF>2/3/4、MC 通過、WF 通過、slip 後 PF。
# 執行: python -m scripts.robustness_sweep_mnq_mes
#       (MES 需要 .env broker 憑證抓 60 天 1m 資料)
# 輸出: data/research/robustness_sweep_latest.json + console 摘要
# ============================================================
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Windows console 預設 cp1252 — 摘要輸出含 Unicode(→ 等),強制 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from backend.data import candle_store
from backend.db.models import (
    current_quarterly_contract_id, get_point_value, _extract_symbol,
)
import backend.backtest.sweep as sweep_mod
from backend.backtest.sweep import (
    run_day_zone_sweep, run_distribution_sweep,
    run_factor_sweep,
)
from backend.api.routes import BacktestRequest, _build_strategy_params_from_request

OUT_DIR = Path("data") / "research"
TICK = 0.25
MC_ITERS = 500
MC_DD_LIMIT = 2000.0
# slip 注入等級(RT ticks,進場市價單 slip;bracket 跟隨 fill 的近似成本)
SLIP_LEVELS_TICKS = (1, 2, 4, 8, 14)
LOG = lambda *a: (print(*a), sys.stdout.flush())


# ── data ─────────────────────────────────────────────────────

def load_mnq():
    bars = candle_store.load("MNQ", 1)
    if not bars:
        raise SystemExit("MNQ store empty — run FETCH FULL DATA in the web UI first")
    return sorted(bars, key=lambda c: c.timestamp)


async def _fetch_mes_async():
    from backend.broker.topstepx import TopstepXClient
    from backend.db.models import BarUnit
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    username = os.getenv("TOPSTEPX_USERNAME")
    api_key = os.getenv("TOPSTEPX_API_KEY")
    if not username or not api_key:
        raise SystemExit("no TOPSTEPX credentials in .env — cannot fetch MES")
    cid = current_quarterly_contract_id("MES")
    LOG(f"[MES] fetching 1m history for {cid} ...")
    client = TopstepXClient(username=username, api_key=api_key,
                            use_demo=os.getenv("TOPSTEPX_USE_DEMO", "false").lower() == "true")
    await client.authenticate()
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=70)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    bars = await client.get_historical_bars_paginated(
        cid, BarUnit.MINUTE, 1, start_time=start, end_time=end)
    try:
        await client.close()
    except Exception:
        pass
    return bars


def load_mes():
    bars = candle_store.load("MES", 1)
    fresh = False
    if bars:
        last = max(b.timestamp for b in bars)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        fresh = (datetime.now(timezone.utc) - last) < timedelta(days=3)
    if not bars or not fresh:
        new = asyncio.run(_fetch_mes_async())
        if new:
            candle_store.merge(new, "MES", 1)
            bars = candle_store.load("MES", 1)
    if not bars:
        raise SystemExit("MES data unavailable")
    return sorted(bars, key=lambda c: c.timestamp)


# ── live slip measurement (MNQ, factor-style market entry) ───

def measure_live_slip(candles_1m):
    """實盤 topstep 成交 vs 回測假設 fill(所屬 5m bar 開盤 / 1m bar 開盤)。

    FACTOR 模型: 訊號 = 5m 收盤 → 下一 5m open 市價進場。回測 fill = 該
    5m bar 的 open;實際 fill 在該 5m 期間內某秒。adverse slip(+ = 實盤
    更差)= buy: fill-open / sell: open-fill。"""
    hist_path = Path("data") / "trade_history.json"
    if not hist_path.exists():
        return {"available": False, "reason": "no trade_history.json"}
    recs = json.loads(hist_path.read_text(encoding="utf-8"))
    # dedupe multi-account copies → one per (entry-second, price, dir)
    seen = {}
    for r in recs:
        if str(r.get("source")) != "topstep":
            continue
        key = ((r.get("entry_time") or "")[:19], r.get("entry_price"), r.get("direction"))
        seen.setdefault(key, r)
    recs = list(seen.values())

    by_min = {}
    for c in candles_1m:
        ts = c.timestamp if c.timestamp.tzinfo else c.timestamp.replace(tzinfo=timezone.utc)
        by_min[ts.replace(second=0, microsecond=0)] = c

    slips_all, slips_market = [], []
    used = 0
    for r in recs:
        try:
            et = datetime.fromisoformat(str(r["entry_time"]).replace("Z", "+00:00"))
        except Exception:
            continue
        et = et.astimezone(timezone.utc)
        px = r.get("entry_price")
        d = str(r.get("direction") or "").lower()
        if px is None or d not in ("buy", "sell"):
            continue
        m1 = et.replace(second=0, microsecond=0)
        m5 = m1 - timedelta(minutes=m1.minute % 5)
        off_s = (et - m5).total_seconds()
        c5 = by_min.get(m5)
        if c5 is None:
            continue
        sign = 1.0 if d == "buy" else -1.0
        slip = sign * (float(px) - float(c5.open)) / TICK
        slips_all.append(slip)
        # market-entry signature: fill lands within 120s of the 5m boundary AND
        # is not a deep price-improvement fill(大幅負 slip = limit 單成交,
        # 不是市價追價)。FACTOR 市價單延遲 ~60s,adverse 幅度 |slip|<60t 合理。
        if off_s < 120 and -4.0 <= slip <= 60.0:
            slips_market.append(slip)
        used += 1

    def _stats(a):
        if not a:
            return None
        arr = np.asarray(a, float)
        return {
            "n": int(arr.size),
            "mean_ticks": round(float(arr.mean()), 2),
            "median_ticks": round(float(np.median(arr)), 2),
            "p75_ticks": round(float(np.percentile(arr, 75)), 2),
            "adverse_share": round(float((arr > 0).mean()), 3),
        }

    out = {
        "available": bool(slips_all),
        "fills_used": used,
        "vs_5m_open_all": _stats(slips_all),        # 全部 fill(多為 limit 單,負=價格改善)
        "vs_5m_open_market_like": _stats(slips_market),  # 市價單特徵 fill
        "note": ("market-entry sample is tiny (FACTOR went live recently); "
                 "anchor = documented EMAPMO fill 2026-07-23 14:31:03Z "
                 "(+14t vs strategy log / +16t vs 5m open). measured_rt_ticks "
                 "uses that anchor unless the market-like sample grows past 10."),
    }
    # market-like 樣本目前仍混入開盤附近的 limit 成交(中位數被拉低),
    # 樣本 ≥30 筆之前一律用有據可查的 EMAPMO 實例錨點(3.5 pts = 14 ticks)。
    ml = out["vs_5m_open_market_like"]
    if ml and ml["n"] >= 30:
        out["measured_rt_ticks"] = max(0.0, float(ml["median_ticks"]))
    else:
        out["measured_rt_ticks"] = 14.0   # documented EMAPMO case: 3.5 pts = 14 ticks
    return out


# ── robustness math ──────────────────────────────────────────

def mc_test(pnls, iters=MC_ITERS, dd_limit=MC_DD_LIMIT, seed=7):
    a = np.asarray(pnls, float)
    n = a.size
    if n < 10:
        return {"mc_ok": False, "mc_reason": f"only {n} trades"}
    rng = np.random.default_rng(seed)
    samp = a[rng.integers(0, n, size=(iters, n))]
    tot = samp.sum(axis=1)
    cum = samp.cumsum(axis=1)
    dd = (np.maximum.accumulate(cum, axis=1) - cum).max(axis=1)
    gains = np.where(samp > 0, samp, 0.0).sum(axis=1)
    losses = np.where(samp < 0, -samp, 0.0).sum(axis=1)
    pf = np.where(losses > 1e-9, gains / np.maximum(losses, 1e-9), 999.0)
    res = {
        "mc_ok": True,
        "mc_pnl_p5": round(float(np.percentile(tot, 5)), 1),
        "mc_pnl_p50": round(float(np.percentile(tot, 50)), 1),
        "mc_p_loss": round(float((tot <= 0).mean()), 3),
        "mc_dd_p50": round(float(np.percentile(dd, 50)), 1),
        "mc_dd_p95": round(float(np.percentile(dd, 95)), 1),
        "mc_p_dd_gt_limit": round(float((dd > dd_limit).mean()), 3),
        "mc_pf_p5": round(float(np.percentile(pf, 5)), 3),
    }
    res["mc_pass"] = bool(
        res["mc_p_loss"] <= 0.05
        and res["mc_dd_p95"] < dd_limit
        and res["mc_pf_p5"] > 1.0
    )
    return res


def slipped_stats(pnls, per_trade_cost):
    s = [p - per_trade_cost for p in pnls]
    g = sum(x for x in s if x > 0)
    l = sum(-x for x in s if x < 0)
    eq = peak = dd = 0.0
    for x in s:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {
        "pnl": round(sum(s), 1),
        "pf": round((g / l) if l > 0 else (999.0 if g > 0 else 0.0), 3),
        "max_dd": round(dd, 1),
    }


def entry_type(r):
    """market|limit 進場分類(依模型/參數)。"""
    model = str(r.get("model") or "")
    p = r.get("params") or {}
    if model == "DAY ZONE":
        return "limit" if str(p.get("fade_entry_mode")) == "limit" else "market"
    if model == "DISTRIBUTION":
        return "limit" if str(p.get("sigma_entry_mode")) == "blind" else "market"
    # TREND(確認K收盤市價)與 FACTOR(下一5m open 市價)都是市價
    return "market"


def add_robustness(results, symbol, measured_rt_ticks):
    pv = get_point_value(f"CON.F.US.{symbol}.U26")
    tick_val = TICK * pv                      # 1 tick $ (size=1)
    for r in results:
        pnls = r.get("_ordered_pnls") or []
        r["symbol"] = symbol
        r["entry_type"] = entry_type(r)
        r.update(mc_test(pnls))
        # slip 注入表: 每 RT tick 等級 → pnl/pf/maxdd
        table = {}
        for lv in SLIP_LEVELS_TICKS:
            table[str(lv)] = slipped_stats(pnls, lv * tick_val)
        r["slip_table"] = table
        # measured slip(市價進場模型全額;limit 進場出場 stop 仍市價 → 半額)
        if measured_rt_ticks is not None and pnls:
            eff = measured_rt_ticks if r["entry_type"] == "market" else measured_rt_ticks / 2.0
            st = slipped_stats(pnls, eff * tick_val)
            r["slip_measured_ticks"] = round(eff, 1)
            r["pf_slip"] = st["pf"]
            r["pnl_slip"] = st["pnl"]
            r["max_dd_slip"] = st["max_dd"]
        else:
            r["slip_measured_ticks"] = None
            r["pf_slip"] = r.get("pf")
            r["pnl_slip"] = r.get("pnl")
            r["max_dd_slip"] = r.get("max_dd")
        # 使用者閘門
        r["gate_dd_lt_2k"] = bool(float(r.get("max_dd", 9e9)) < 2000.0)
        pf = float(r.get("pf", 0.0) or 0.0)
        r["gate_pf"] = ">4" if pf > 4 else (">3" if pf > 3 else (">2" if pf > 2 else "fail"))
        r["long_term_ok"] = bool(
            r.get("wf_pass") and r.get("mc_pass")
            and r["gate_dd_lt_2k"] and pf > 2.0
            and float(r.get("pf_slip") or 0.0) > 1.5
            and float(r.get("trades_per_month") or 0.0) >= 8.0
        )


# ── sweep runner ─────────────────────────────────────────────

def run_symbol(symbol, candles, measured_rt_ticks):
    cid = current_quarterly_contract_id(symbol)
    req = BacktestRequest()
    req.contract_id = cid
    req.contract_size = 1
    base = _build_strategy_params_from_request(req, 1)
    base.contract_id = cid

    LOG(f"[{symbol}] {len(candles)} bars "
        f"{candles[0].timestamp:%Y-%m-%d} → {candles[-1].timestamp:%Y-%m-%d}")

    # patch out annotate so `_ordered_pnls` survives the run_* helpers
    real_annotate = sweep_mod._annotate_plateau_and_acceptance
    sweep_mod._annotate_plateau_and_acceptance = lambda rows: None
    out = []
    t0 = time.time()
    try:
        def prog(tag):
            def _p(cur, total, detail):
                if cur % 20 == 0 or cur == total:
                    LOG(f"[{symbol}] {tag} {cur}/{total} {detail}  ({time.time()-t0:.0f}s)")
            return _p
        out.extend(run_day_zone_sweep(candles, base, prog("DAYZONE")))
        out.extend(run_distribution_sweep(candles, base, prog("DIST")))
        out.extend(run_factor_sweep(candles, base, prog("FACTOR")))
    finally:
        sweep_mod._annotate_plateau_and_acceptance = real_annotate

    add_robustness(out, symbol, measured_rt_ticks)   # needs _ordered_pnls
    real_annotate(out)                               # accept/plateau; pops _ordered_pnls
    LOG(f"[{symbol}] sweep done: {len(out)} variants in {time.time()-t0:.0f}s")
    return out


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    mnq = load_mnq()
    slip = measure_live_slip(mnq)
    LOG("[SLIP] measured from live fills:", json.dumps(slip, ensure_ascii=False))
    measured = slip.get("measured_rt_ticks")

    mes = load_mes()

    results = []
    results += run_symbol("MNQ", mnq, measured)
    results += run_symbol("MES", mes, measured)

    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "slip_measurement": slip,
        "mc_iters": MC_ITERS,
        "slip_levels_ticks": list(SLIP_LEVELS_TICKS),
        "symbols": {
            "MNQ": {"bars": len(mnq),
                    "range": [mnq[0].timestamp.isoformat(), mnq[-1].timestamp.isoformat()]},
            "MES": {"bars": len(mes),
                    "range": [mes[0].timestamp.isoformat(), mes[-1].timestamp.isoformat()]},
        },
        "results": results,
    }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
    (OUT_DIR / f"robustness_sweep_{stamp}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    (OUT_DIR / "robustness_sweep_latest.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    # ── console findings ────────────────────────────────────
    LOG("\n===== SUMMARY =====")
    for sym in ("MNQ", "MES"):
        rows = [r for r in results if r["symbol"] == sym]
        rows.sort(key=lambda r: -(r.get("pf") or 0.0))
        LOG(f"\n-- {sym}: top 12 by PF (min 15 trades) --")
        shown = 0
        for r in rows:
            if (r.get("trades") or 0) < 15:
                continue
            LOG(f"  {r['model']:<12} {r['label']:<38} PF={r.get('pf'):>7} "
                f"PFslip={r.get('pf_slip'):>7} pnl={r.get('pnl'):>9} dd={r.get('max_dd'):>7} "
                f"n={r.get('trades'):>4} wf={'Y' if r.get('wf_pass') else 'n'} "
                f"mc={'Y' if r.get('mc_pass') else 'n'} {r.get('entry_type')} "
                f"{'LT-OK' if r.get('long_term_ok') else ''}")
            shown += 1
            if shown >= 12:
                break
        lt = [r for r in rows if r.get("long_term_ok")]
        LOG(f"  long_term_ok: {len(lt)} / {len(rows)}")
    LOG("\nreport: data/research/robustness_sweep_latest.json")


if __name__ == "__main__":
    main()
