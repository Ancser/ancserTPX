# ============================================================
# 文件: scripts/trend_rider_sweep.py
# 狀態: 1.0.9 研究腳本 (trend-rider 全面 sweep)
# 目標: 尋找能比肩 BEST preset(EMAPMO long_only early atr_blend
#       SL2.5 TP7.5)的低頻高PF、一單吃整段 trend 的設置。
# 與標準 sweep 的差異:
#   - 寬 TP grid(4/6/7.5/10 atr_blend)— 標準 grid 最高只有 4
#   - trail@50% 變體(tr_trail_enabled + trigger 0.5 + 20t lock)
#   - ladder 棘輪出場(引擎對 FACTOR 生效,標準 grid 只掃 2 個變體)
#   - 15m 信號 TF(標準 grid 固定 5m)
#   - 標準四模型 grid 同窗口重跑作對照 + BEST 基準行
# 輸出: data/machinelearning/trend_rider_sweep_<stamp>.json / .md
#       不動 data/sweep_results.json 與 presets.json(UI 檔案)。
# ============================================================
"""1.0.9 trend-rider 全面 sweep:FACTOR 家族 × 寬TP/trail/ladder × 5m/15m。"""

from __future__ import annotations

import copy
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
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
from backend.backtest.sweep import _annotate_plateau_and_acceptance, _run_one, run_model_sweep
from backend.data import candle_store
from backend.db.models import current_quarterly_contract_id

ALL_SESSIONS = ["ASIA", "EURO", "PRE", "RTH", "AH"]

# (family, side, pmo_mode) — pmo_mode 只對 emapmo 有效
SIGNAL_GRID = (
    [("emapmo", side, mode) for mode in ("normal", "early", "both")
     for side in ("all", "long_only", "short_only")]
    + [("icefishball", side, "normal") for side in ("all", "long_only", "short_only")]
    + [("momentum_reversion", side, "normal") for side in ("all", "long_only", "short_only")]
)

TF_GRID = (5, 15)

# 出場/風控變體(kind, sl_mult, tp_mult, trail)
EXIT_GRID = (
    [{"kind": "tp", "sl": s, "tp": t, "trail": False}
     for s in (1.5, 2.5, 3.5) for t in (4.0, 6.0, 7.5, 10.0)]
    + [{"kind": "tp", "sl": 2.5, "tp": t, "trail": True} for t in (4.0, 7.5)]
    + [{"kind": "ladder", "sl": s, "tp": 10.0, "trail": False} for s in (1.5, 2.5, 3.5)]
)

# session VA filter 額外變體(只掃 5m;developing VA 追蹤 ~75s/run,故只挑代表性信號)
VA_EXTRA_EXITS = (
    {"kind": "tp", "sl": 2.5, "tp": 7.5, "trail": False},
    {"kind": "ladder", "sl": 2.5, "tp": 10.0, "trail": False},
)
VA_EXTRA_SIGNALS = (
    ("emapmo", "long_only", "early"),
    ("emapmo", "all", "early"),
    ("emapmo", "all", "normal"),
    ("icefishball", "all", "normal"),
    ("icefishball", "short_only", "normal"),
    ("momentum_reversion", "all", "normal"),
)

FAMILY_LABEL = {"emapmo": "EMAPMO", "icefishball": "KDJMA", "momentum_reversion": "MREV"}


def _configure_factor(p, family, side, mode, tf, exit_spec, va_filter="off"):
    p.strategy = "factor"
    p.area_timeframe = "session" if va_filter != "off" else "15m"
    p.method = "single"
    p.tf_combo = []
    p.value_area_pct = 0.80
    p.tr_allowed_sessions = list(ALL_SESSIONS)
    p.tr_one_trade_per_session = False
    p.one_trade_per_session_direction = False
    p.tr_daily_loss_stop = 1
    p.tr_daily_win_stop = 0
    p.factor_timeframe_minutes = int(tf)
    p.factor_signal_family = str(family)
    p.factor_side_mode = str(side)
    p.factor_pmo_signal_mode = str(mode)
    p.factor_session_va_filter = str(va_filter)
    p.factor_sl_rule = "atr_blend"
    p.factor_tp_rule = "atr_blend"
    p.factor_sl_value = float(exit_spec["sl"])
    p.factor_tp_value = float(exit_spec["tp"])
    p.factor_max_hold_bars = 0          # HOLD 永久移除 — SL/TP-only
    p.factor_max_trades_per_day = 3
    p.factor_warmup_bars = 150
    p.tr_exit_mode = "ladder" if exit_spec["kind"] == "ladder" else "tp"
    trail = bool(exit_spec.get("trail"))
    p.trail_enabled = trail
    p.tr_trail_enabled = trail
    p.trail_trigger_pct = 0.5 if trail else 0.0
    p.tr_trail_trigger_pct = 0.5 if trail else 0.0
    p.trail_sl_ticks = 20 if trail else 0    # 20t = 5pts 小額鎖利
    p.tr_trail_sl_ticks = 20 if trail else 0


def _row_params(family, side, mode, tf, exit_spec, va_filter="off"):
    return {
        "strategy": "factor",
        "factor_timeframe_minutes": int(tf),
        "factor_signal_family": str(family),
        "factor_side_mode": str(side),
        "factor_pmo_signal_mode": str(mode),
        "factor_session_va_filter": str(va_filter),
        "factor_sl_rule": "atr_blend",
        "factor_tp_rule": "atr_blend",
        "factor_sl_value": float(exit_spec["sl"]),
        "factor_tp_value": float(exit_spec["tp"]),
        "tr_exit_mode": "ladder" if exit_spec["kind"] == "ladder" else "tp",
        "trail": bool(exit_spec.get("trail")),
        "tr_daily_loss_stop": 1,
        "factor_max_trades_per_day": 3,
    }


def _label(family, side, mode, tf, exit_spec, va_filter="off"):
    fam = FAMILY_LABEL.get(family, family.upper())
    kind = exit_spec["kind"]
    if kind == "ladder":
        exit_s = f"LADDER SL{exit_spec['sl']:g} cap{exit_spec['tp']:g}"
    elif exit_spec.get("trail"):
        exit_s = f"SL{exit_spec['sl']:g} TP{exit_spec['tp']:g} TRAIL50"
    else:
        exit_s = f"SL{exit_spec['sl']:g} TP{exit_spec['tp']:g}"
    va_s = " VAout" if va_filter != "off" else ""
    mode_s = f" {mode}" if family == "emapmo" else ""
    return f"{fam} {side}{mode_s} {tf}m {exit_s}{va_s}"


_W: dict = {}   # worker 進程內快取(base params + candles)


def _init_worker() -> None:
    cid = current_quarterly_contract_id("MNQ")
    size = _normalize_contract_size(cid, 1)
    req = BacktestRequest(contract_id=cid, contract_size=size)
    _W["base"] = _build_strategy_params_from_request(req, size)
    _W["candles"] = sorted(candle_store.load("MNQ", 1), key=lambda c: c.timestamp)


def _run_job(job) -> dict:
    family, side, mode, tf, spec, va = job
    p = copy.deepcopy(_W["base"])
    _configure_factor(p, family, side, mode, tf, spec, va)
    r = _run_one(p, _W["candles"], None)
    r["params"] = _row_params(family, side, mode, tf, spec, va)
    r["label"] = _label(family, side, mode, tf, spec, va)
    r["phase"] = "rider"
    return r


def main() -> int:
    t0 = time.time()
    symbol = "MNQ"
    contract_id = current_quarterly_contract_id(symbol)
    contract_size = _normalize_contract_size(contract_id, 1)
    req = BacktestRequest(contract_id=contract_id, contract_size=contract_size)
    base = _build_strategy_params_from_request(req, contract_size)
    candles = sorted(candle_store.load(symbol, 1), key=lambda c: c.timestamp)
    if not candles:
        raise SystemExit("No MNQ 1m candles in local candle_store.")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
    out_dir = ROOT / "data" / "machinelearning"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_json = out_dir / f"trend_rider_sweep_{stamp}.json"

    print(
        f"SWEEP_START symbol={symbol} contract={contract_id} size={contract_size} "
        f"candles={len(candles)} range={candles[0].timestamp.isoformat()}->{candles[-1].timestamp.isoformat()}",
        flush=True,
    )

    # ── Phase 1: trend-rider FACTOR 擴展 grid(多進程)──
    jobs = []
    for tf in TF_GRID:
        for family, side, mode in SIGNAL_GRID:
            for spec in EXIT_GRID:
                jobs.append((family, side, mode, tf, spec, "off"))
    for family, side, mode in VA_EXTRA_SIGNALS:
        for spec in VA_EXTRA_EXITS:
            jobs.append((family, side, mode, 5, spec, "outside"))

    rider: list[dict] = []
    total = len(jobs) + 1
    workers = max(2, min(6, (os.cpu_count() or 8) - 2))
    print(f"SWEEP_POOL workers={workers} jobs={len(jobs)}", flush=True)
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker) as pool:
        futures = [pool.submit(_run_job, j) for j in jobs]
        for i, fut in enumerate(as_completed(futures), start=1):
            r = fut.result()
            rider.append(r)
            print(f"SWEEP_PROGRESS {i}/{total} RIDER {r['label']} "
                  f"tr={r['trades']} pf={r['pf']} pnl={r['pnl']}", flush=True)
            if i % 25 == 0:
                out_json.write_text(json.dumps({"partial": True, "results": rider},
                                               ensure_ascii=False), encoding="utf-8")

    # BEST preset 基準行(MNQx1 口徑)
    p = copy.deepcopy(base)
    best_spec = {"kind": "tp", "sl": 2.5, "tp": 7.5, "trail": False}
    _configure_factor(p, "emapmo", "long_only", "early", 5, best_spec, "off")
    r = _run_one(p, candles, None)
    r["params"] = _row_params("emapmo", "long_only", "early", 5, best_spec, "off")
    r["label"] = "BASELINE BEST EMAPMO long_only early 5m SL2.5 TP7.5"
    r["phase"] = "baseline"
    rider.append(r)
    print(f"SWEEP_PROGRESS {total}/{total} {r['label']} "
          f"tr={r['trades']} pf={r['pf']} pnl={r['pnl']}", flush=True)

    _annotate_plateau_and_acceptance(rider)

    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "candles": len(candles),
        "range": [candles[0].timestamp.isoformat(), candles[-1].timestamp.isoformat()],
        "rider_results": sorted(rider, key=lambda x: -float(x.get("pf", 0.0) or 0.0)),
        "standard_results": [],
    }
    out_json.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"RIDER_DONE n={len(rider)} elapsed={time.time() - t0:.0f}s", flush=True)

    # ── Phase 2: 標準四模型 grid(同窗口對照)──
    def _progress(cur: int, total: int, detail: str) -> None:
        print(f"SWEEP_PROGRESS STD {cur}/{total} {detail}", flush=True)

    standard = run_model_sweep(candles, base, _progress)
    for r in standard:
        r["phase"] = "standard"
    payload["standard_results"] = sorted(standard, key=lambda x: -float(x.get("pf", 0.0) or 0.0))
    out_json.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    # ── 摘要 ──
    def _fmt(r):
        return (f"| {r.get('phase','')[:4]} | {r['label'][:58]} | {r['trades']} | {r['pf']:.2f} "
                f"| {r['pnl']:.0f} | {r['max_dd']:.0f} | {r['trades_per_month']:.0f} "
                f"| {'Y' if r.get('wf_pass') else 'N'} | {'Y' if r.get('plateau_pass') else 'N'} "
                f"| {r.get('weekly_cv', 0):.2f} |")

    combined = rider + standard
    eligible = [r for r in combined if r["trades"] >= 10 and r["pnl"] > 0]
    eligible.sort(key=lambda x: -float(x.get("pf", 0.0) or 0.0))
    lines = [
        f"# trend-rider sweep {stamp}",
        f"candles={len(candles)} range={payload['range'][0]} -> {payload['range'][1]}",
        "",
        "## TOP 30 by PF (trades>=10, pnl>0)",
        "| phase | label | tr | pf | pnl | dd | t/mo | wf | plat | wcv |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    lines += [_fmt(r) for r in eligible[:30]]
    base_row = next((r for r in rider if r["phase"] == "baseline"), None)
    if base_row:
        lines += ["", "## BASELINE", _fmt(base_row)]
    lad = [r for r in rider if r["params"].get("tr_exit_mode") == "ladder"
           and r["trades"] >= 10 and r["pnl"] > 0]
    lad.sort(key=lambda x: -float(x.get("pf", 0.0) or 0.0))
    lines += ["", "## TOP 15 LADDER (棘輪吃 trend)",
              "| phase | label | tr | pf | pnl | dd | t/mo | wf | plat | wcv |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    lines += [_fmt(r) for r in lad[:15]]
    (out_dir / f"trend_rider_sweep_{stamp}.md").write_text(
        "\n".join(lines), encoding="utf-8")

    print(f"SWEEP_DONE total={len(combined)} elapsed={time.time() - t0:.0f}s "
          f"out={out_json}", flush=True)
    for r in eligible[:15]:
        print(f"SWEEP_TOP {r['phase']} | {r['label']} | tr={r['trades']} pf={r['pf']} "
              f"pnl={r['pnl']} dd={r['max_dd']} wf={r.get('wf_pass')} "
              f"plat={r.get('plateau_pass')} wcv={r.get('weekly_cv')}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
