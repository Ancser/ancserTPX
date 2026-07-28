"""1.0.9: BEST / SECOND BEST / OR15 在 MNQ vs MES 的對照 + 掛單足跡分析。

回答三件事:
  1. 同一套「好策略」搬到 MES 還成立嗎(PF / MC / walk-forward / slip 注入)。
  2. 6 帳號 × 3 口 = 18 口 的下單量,相對當下 1m/5m 成交量佔多少 —— 亦即
     低量時段掛單會不會明顯到被獵殺。
  3. 各策略實際成交落在哪些時段(ET 小時分佈)。

註: robustness sweep 測的是 EMAPMO ... SL2.5 **TP2**;實際 live 的 BEST
preset 是 **TP7.5**,兩者不同,所以這裡直接讀 data/presets.json 的原始配置。

用法:
  python scripts/best_mes_parity_study.py
  python scripts/best_mes_parity_study.py --lots 18 --out data/research/best_mes_parity.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from backend.api.routes import BacktestRequest, _build_strategy_params_from_request  # noqa: E402
from backend.backtest.engine import BacktestConfig, BacktestEngine  # noqa: E402
from backend.data import candle_store  # noqa: E402
from backend.db.models import (  # noqa: E402
    _extract_symbol, current_quarterly_contract_id, get_commission_rt,
    get_fees_rt, get_point_value,
)

TICK = 0.25
MC_ITERS = 2000
MC_DD_LIMIT = 2000.0
SLIP_LEVELS = (1, 2, 4, 8, 14)
LOG = lambda *a: (print(*a), sys.stdout.flush())


# ── 受測策略 ─────────────────────────────────────────────────

def load_variants() -> list[dict]:
    presets = json.loads(Path("data/presets.json").read_text(encoding="utf-8"))["presets"]
    out = [
        {"name": "BEST (live preset)", "params": dict(presets["BEST"])},
        {"name": "SECOND BEST (live preset)", "params": dict(presets["SECOND BEST"])},
    ]
    # sweep 冠軍(與 BEST 同族但 TP2)—— 用來分辨 TP7.5 vs TP2 的差異
    tp2 = dict(presets["BEST"])
    tp2["factor_tp_value"] = 2.0
    out.append({"name": "BEST family TP2 (sweep winner)", "params": tp2})
    # 唯一雙商品都通過 robustness 的變體。參數逐欄取自 robustness sweep 的
    # "OR15 SL0.2R TP1R S0" 行(strategy 名是 "fade" 不是 "day_zone";用錯會
    # 變成每天 12 筆的另一個策略)。
    out.append({"name": "DAY ZONE OR15 SL0.2R TP1R", "params": {
        "strategy": "fade",
        "tf_combo": [],
        "tr_sl_ticks": 50, "tr_tp_ticks": 200,
        "tr_trail_enabled": True, "tr_trail_trigger_pct": 0.3,
        "tr_trail_sl_ticks": 10, "tr_full_tp_lock": 0,
        "one_trade_per_session_direction": False,
        "tr_one_trade_per_session": False,
        "tr_allowed_sessions": ["ASIA", "EURO", "PRE", "RTH", "AH"],
        "fade_tp_frac": 1.0, "fade_entry_mode": "or15",
        "pmo_max_hold_bars": 0, "factor_max_hold_bars": 0,
    }})
    return out


# ── 回測 ─────────────────────────────────────────────────────

def run_variant(params_dict: dict, candles, symbol: str) -> dict:
    cid = current_quarterly_contract_id(symbol)
    payload = dict(params_dict)
    payload["contract_id"] = cid
    payload["contract_size"] = 1          # 統一 1 口口徑,方便跨商品比較
    req = BacktestRequest(**{k: v for k, v in payload.items()
                             if k in BacktestRequest.model_fields})
    sp = _build_strategy_params_from_request(req, 1)
    sp.contract_id = cid
    cfg = BacktestConfig(
        strategies=["trend"], initial_capital=50_000.0,
        symbol=_extract_symbol(cid), commission_rt=get_commission_rt(cid),
        fees_rt=get_fees_rt(cid),
        value_area_pct=float(getattr(sp, "value_area_pct", 0.80)),
    )
    res = BacktestEngine(config=cfg, strategy_params=sp,
                         zone_timeline=None, record_equity=False).run(candles)
    trades = [{
        "entry_time": t.entry_time,
        "exit_time": getattr(t, "exit_time", None),
        "pnl": float(t.pnl or 0.0),
        "direction": str(getattr(t, "direction", "") or ""),
    } for t in res.trades]
    return {"trades": trades, "metrics": res.metrics}


def series_stats(pnls) -> dict:
    g = sum(p for p in pnls if p > 0)
    l = sum(-p for p in pnls if p < 0)
    eq = peak = dd = 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {
        "n": len(pnls),
        "pnl": round(eq, 1),
        "pf": round((g / l) if l > 0 else (999.0 if g > 0 else 0.0), 3),
        "max_dd": round(dd, 1),
        "win_rate": round(sum(1 for p in pnls if p > 0) / len(pnls), 3) if pnls else 0.0,
    }


def mc_test(pnls, iters=MC_ITERS, dd_limit=MC_DD_LIMIT, seed=7) -> dict:
    a = np.asarray(pnls, float)
    n = a.size
    if n < 10:
        return {"mc_ok": False, "mc_reason": f"only {n} trades", "mc_pass": False}
    rng = np.random.default_rng(seed)
    samp = a[rng.integers(0, n, size=(iters, n))]
    tot = samp.sum(axis=1)
    cum = samp.cumsum(axis=1)
    dd = (np.maximum.accumulate(cum, axis=1) - cum).max(axis=1)
    gains = np.where(samp > 0, samp, 0.0).sum(axis=1)
    losses = np.where(samp < 0, -samp, 0.0).sum(axis=1)
    pf = np.where(losses > 1e-9, gains / np.maximum(losses, 1e-9), 999.0)
    r = {
        "mc_ok": True,
        "mc_pnl_p5": round(float(np.percentile(tot, 5)), 1),
        "mc_pnl_p50": round(float(np.percentile(tot, 50)), 1),
        "mc_p_loss": round(float((tot <= 0).mean()), 3),
        "mc_dd_p95": round(float(np.percentile(dd, 95)), 1),
        "mc_pf_p5": round(float(np.percentile(pf, 5)), 3),
    }
    r["mc_pass"] = bool(r["mc_p_loss"] <= 0.05 and r["mc_dd_p95"] < dd_limit
                        and r["mc_pf_p5"] > 1.0)
    return r


def walk_forward(trades) -> dict:
    if len(trades) < 6:
        return {"wf_ok": False, "wf_pass": False, "wf_reason": f"only {len(trades)} trades"}
    ts = [_utc(t["entry_time"]) for t in trades]
    d0, d1 = min(ts), max(ts)
    span = max(timedelta(days=1), d1 - d0)
    segs = [[], [], []]
    for t, ti in zip(trades, ts):
        segs[min(2, int((ti - d0) / span * 3))].append(t["pnl"])
    stats = [series_stats(s) for s in segs]
    return {
        "wf_ok": True,
        "wf_segments": stats,
        "wf_pass": all(s["n"] > 0 and s["pnl"] > 0 and s["pf"] > 1.0 for s in stats),
    }


def slip_table(pnls, tick_value) -> dict:
    return {str(lv): series_stats([p - lv * tick_value for p in pnls])
            for lv in SLIP_LEVELS}


# ── 成交量足跡 ───────────────────────────────────────────────

def volume_index(candles):
    by_min = {}
    for c in candles:
        by_min[_utc(c.timestamp).replace(second=0, microsecond=0)] = c
    return by_min


def footprint(trades, by_min, lots: int) -> dict:
    """每筆進場當下的 1m / 5m 成交量,以及 lots 口佔比。"""
    rows = []
    for t in trades:
        m1 = _utc(t["entry_time"]).replace(second=0, microsecond=0)
        c1 = by_min.get(m1)
        if c1 is None:
            continue
        m5 = m1 - timedelta(minutes=m1.minute % 5)
        v5 = sum(getattr(by_min[m5 + timedelta(minutes=k)], "volume", 0)
                 for k in range(5) if (m5 + timedelta(minutes=k)) in by_min)
        v1 = getattr(c1, "volume", 0) or 0
        rows.append({
            "et_hour": m1.astimezone(_ET).hour,
            "vol_1m": v1,
            "vol_5m": v5,
            "share_1m_pct": round(lots / v1 * 100, 2) if v1 else None,
            "share_5m_pct": round(lots / v5 * 100, 2) if v5 else None,
        })
    if not rows:
        return {"n": 0}
    s1 = [r["share_1m_pct"] for r in rows if r["share_1m_pct"] is not None]
    v1 = [r["vol_1m"] for r in rows]
    hours = defaultdict(int)
    for r in rows:
        hours[r["et_hour"]] += 1
    return {
        "n": len(rows),
        "lots": lots,
        "vol_1m_min": int(min(v1)), "vol_1m_p10": int(np.percentile(v1, 10)),
        "vol_1m_median": int(np.median(v1)),
        "share_1m_median_pct": round(float(np.median(s1)), 2),
        "share_1m_worst_pct": round(float(max(s1)), 2),
        "trades_in_thin_minutes": int(sum(1 for x in s1 if x >= 5.0)),
        "et_hour_hist": dict(sorted(hours.items())),
    }


_ET = timezone(timedelta(hours=-4))  # 粗略 ET(僅用於小時分佈標示)


def _utc(ts) -> datetime:
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


# ── main ─────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lots", type=int, default=18, help="6 accounts x 3 contracts")
    ap.add_argument("--out", default="data/research/best_mes_parity.json")
    args = ap.parse_args()

    data = {}
    for sym in ("MNQ", "MES"):
        bars = candle_store.load(sym, 1)
        if not bars:
            LOG(f"[{sym}] store empty — skipped")
            continue
        bars = sorted(bars, key=lambda c: c.timestamp)
        data[sym] = bars
        LOG(f"[{sym}] {len(bars)} bars "
            f"{bars[0].timestamp:%Y-%m-%d} → {bars[-1].timestamp:%Y-%m-%d} "
            f"({(_utc(bars[-1].timestamp) - _utc(bars[0].timestamp)).days} days)")

    variants = load_variants()
    results = []
    for v in variants:
        for sym, bars in data.items():
            pv = get_point_value(current_quarterly_contract_id(sym))
            tick_value = TICK * pv
            LOG(f"\n=== {v['name']} on {sym} (1t = ${tick_value:.2f}) ===")
            run = run_variant(v["params"], bars, sym)
            trades = run["trades"]
            pnls = [t["pnl"] for t in trades]
            base = series_stats(pnls)
            row = {
                "variant": v["name"], "symbol": sym,
                "tick_value": tick_value, **base,
                **mc_test(pnls), **walk_forward(trades),
                "slip": slip_table(pnls, tick_value),
                "footprint": footprint(trades, volume_index(bars), args.lots),
            }
            results.append(row)
            LOG(f"  n={base['n']} PF={base['pf']} pnl=${base['pnl']} "
                f"dd=${base['max_dd']} win={base['win_rate']}")
            LOG(f"  MC pass={row.get('mc_pass')} "
                f"p_loss={row.get('mc_p_loss')} ddP95={row.get('mc_dd_p95')} "
                f"pfP5={row.get('mc_pf_p5')}")
            LOG(f"  WF pass={row.get('wf_pass')} "
                f"segs={[s['pnl'] for s in row.get('wf_segments', [])]}")
            LOG("  slip PF: " + " ".join(
                f"{k}t={x['pf']}" for k, x in row["slip"].items()))
            fp = row["footprint"]
            if fp.get("n"):
                LOG(f"  {args.lots} lots vs 1m volume: median {fp['share_1m_median_pct']}% "
                    f"worst {fp['share_1m_worst_pct']}% "
                    f"(thin minutes ≥5%: {fp['trades_in_thin_minutes']}/{fp['n']})")
                LOG(f"  ET hour histogram: {fp['et_hour_hist']}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "created_at": datetime.now(timezone.utc).isoformat(),
        "lots": args.lots,
        "mc_iters": MC_ITERS,
        "data_range": {s: [str(b[0].timestamp), str(b[-1].timestamp), len(b)]
                       for s, b in data.items()},
        "results": results,
    }, indent=1, default=str), encoding="utf-8")
    LOG(f"\nreport: {out}")


if __name__ == "__main__":
    main()
