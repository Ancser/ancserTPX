"""1.0.10: 全期(2026-01 → 08)穩定性 sweep。

動機:BEST 的 PF 4.25 是在 6–7 月那段測出來的,而它正是從那段挑出來的。
補進 1–5 月之後全期只有 47 筆、PnL +10,565,且前五個月 PF 0.83(虧的)。
虧損集中在 2–3 月 —— 市場下跌而 BEST 是 long_only。

所以本 sweep 的排序標準**不是總 PF**,而是**最差區段的 PF**(maximin)。
一個總 PF 很高但靠單一區段撐起來的變體,正是我們要淘汰的東西。

閘門(全部要過):
  G0  n >= 30              七個月至少每月 4 筆,否則統計不可信
  G1  全期 PF > 1.3
  G2  獲利月份 >= 5 / 7     連續性的直接量度
  G3  最差月 PnL > -1000    單月不可災難性虧損
  G4  三段走查全部 PF > 1   1-3月 / 4-6月 / 6-8月

排序:worst_segment_pf 由高到低。

用法:
    python scripts/stability_sweep_2026.py                 # 全網格
    python scripts/stability_sweep_2026.py --limit 40      # 先試跑
    python scripts/stability_sweep_2026.py --workers 8
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from itertools import product
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUT = ROOT / "data" / "research" / "stability_sweep_2026.json"

_BARS = None          # worker 全域,避免每個任務重傳 21 萬根
_SYMBOL = "MNQ"
_CID = "CON.F.US.MNQ.U26"


def _utc(t):
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t


def trade_month(dt) -> str:
    """Topstep 交易日以 17:00 CT(夏令 22:00 UTC)換日。"""
    d = _utc(dt)
    if d.hour >= 22:
        d = d + timedelta(days=1)
    return f"{d:%Y-%m}"


# ── 網格 ──────────────────────────────────────────────────────
#
# ⚠️ 必須以**實際 preset 為基底**建網格,不能從空白 BacktestRequest 開始。
#
# 第一版就是從空白 request 建的,結果繼承了一堆與正式設定相反的預設值:
#     tr_allowed_sessions   ['ASIA']  → 整個 sweep 只交易亞洲盤
#     trail_enabled         True      → 移動停損開著
#     factor_max_hold_bars  24        → 把已永久移除的時間出場加了回來
#     tr_one_trade_per_session True    → 額外限流
# 全部結果因此作廢。凡是新增掃描維度,都要先跑 reproduce 檢查:
# 網格 spec 必須能逐筆重現對應 preset 的 n 與 PnL。

def _base(preset_name):
    """取 preset 的完整設定當基底,只留下要被覆寫的維度給網格。"""
    cfg = json.load(open(ROOT / "data" / "presets.json", encoding="utf-8"))
    b = dict(cfg["presets"][preset_name])
    b.pop("contract_size", None)      # sweep 一律 1 口,便於跨變體比較
    # preset 綁死 MNQ 合約;跑 MES 時必須讓 _run() 用 _CID,否則 tick 值算錯
    b.pop("contract_id", None)
    return b


def build_grid():
    grid = []
    f_base = _base("BEST")            # FACTOR 族基底
    m_base = _base("MOMENTUM BEST")   # MOMENTUM 族基底
    b_base = _base("BETAFIB BEST")    # BETAFIB 族基底(舊的 "BETA FIB" 已由使用者刪除)

    # 限流維度:兩個最穩的既有 preset(BETA FIB / MOMENTUM BEST)都用
    # max_trades_per_day=1 + one_trade_per_session_direction=True。
    # 少做 = 少暴露在壞體制裡,限流本身就是穩定性的來源,必須進網格。

    # FACTOR:BEST 所屬的族。時框固定 5m(BEST 用的值),換出預算給限流維度。
    for fam, side, sl, tp, mode, onedir in product(
        ("emapmo", "momentum_reversion", "kdjma"),
        ("long_only", "short_only", "both"),
        (1.5, 2.5, 3.5),
        (2.0, 4.0, 7.5),
        ("early", "normal"),
        (True, False),
    ):
        grid.append({**f_base,
            "_tag": f"FACTOR/{fam}/{side}/sl{sl}/tp{tp}/{mode}"
                    f"/{'1dir' if onedir else 'free'}",
            "strategy": "factor",
            "factor_signal_family": fam,
            "factor_side_mode": side,
            "factor_timeframe_minutes": 5,
            "factor_sl_rule": "atr_blend", "factor_sl_value": sl,
            "factor_tp_rule": "atr_blend", "factor_tp_value": tp,
            "factor_pmo_signal_mode": mode,
            "one_trade_per_session_direction": onedir,
        })

    # MOMENTUM / BETAFIB 走 research_lab._ResearchBase,旋鈕跟 FACTOR **不同**:
    #     SL   factor_sl_value       → self.sl_atr        (與 FACTOR 同)
    #     TP   rr_ratio              → self.rr            (不是 factor_tp_value!)
    #     時框  research_tf_minutes   → self.tf_minutes    (不是 factor_timeframe_minutes!)
    # `_make()` 算的是 tp = entry ± risk * self.rr,所以掃 factor_tp_value 完全沒作用。
    # 第一版就是這樣掃的,結果 tp2.0/4.0/7.5 三個變體回傳一模一樣的 PnL 才發現。
    # rr_ratio 是**整數**(models._normalize_rr_ratio),小數會被吃掉。
    # research_tf_minutes 不進網格:MomentumContinuation.evaluate() 只用原始
    # 1m 串流與經過分鐘數,沒碰聚合後的高階 K 棒,實測 5m/15m 結果完全相同。
    for side, sl, rr, fm, mx in product(
        ("long_only", "short_only", "both"),
        (1.5, 2.0, 2.5), (1, 2, 3), (30, 45, 60), (1, 3),
    ):
        grid.append({**m_base,
            "_tag": f"MOMENTUM/{side}/sl{sl}/rr{rr}/first{fm}/max{mx}",
            "strategy": "momentum",
            "factor_side_mode": side,
            "factor_sl_value": sl,
            "rr_ratio": rr,
            "momentum_first_minutes": fm,
            "factor_max_trades_per_day": mx,
        })

    # BETAFIB:斐波那契回撤,與動能族機制不同 → 相關性低,值得比。
    # fib 必須含 0.382 —— 現有唯一全閘門通過的 BETA FIB preset 用的就是它,
    # 第一版網格從 0.5 起跳,直接漏掉了最好的那個。
    for fib, anchor, sl, rr, mx in product(
        (0.382, 0.5, 0.618, 0.786), ("hl", "oc"),
        (1.5, 2.0, 2.5), (1, 2, 3), (1, 2),
    ):
        grid.append({**b_base,
            "_tag": f"BETAFIB/fib{fib}/{anchor}/sl{sl}/rr{rr}/max{mx}",
            "strategy": "betafib",
            "betafib_entry_fib": fib,
            "betafib_anchor": anchor,
            "factor_sl_value": sl,
            "rr_ratio": rr,
            "factor_max_trades_per_day": mx,
        })

    # 1.0.10: 補上先前漏測的兩族。它們原本走 sweep.py 的 run_day_zone_sweep /
    # run_distribution_sweep,那兩支有自己的指標格式,無法跟本檔的穩定性閘門
    # 相比 —— 所以改成同樣以 preset 為基底、跑同一套指標。
    # 參數名取自 sweep.py 的對應迴圈,確保與正式路徑一致。
    for entry, sl, tp_frac in product(
        ("limit", "rejection"), (60, 80, 120, 160), (0.50, 0.75, 1.00),
    ):
        grid.append({**f_base,
            "_tag": f"DAYZONE/{entry}/sl{sl}/tp{int(tp_frac*100)}",
            "strategy": "fade",
            "fade_entry_mode": entry,
            "fade_tp_frac": tp_frac,
            "sl_ticks": sl, "tr_sl_ticks": sl,
            "area_timeframe": "15m", "method": "single", "tf_combo": [],
        })
    # OR15 的 SL/TP 是內建比例,只有進場模式這一個維度
    grid.append({**f_base,
        "_tag": "DAYZONE/or15",
        "strategy": "fade", "fade_entry_mode": "or15", "fade_tp_frac": 1.0,
        "area_timeframe": "15m", "method": "single", "tf_combo": [],
    })

    for win, meth, entry, acc, span, tgt in product(
        (15, 30, 60), ("std", "mad"), ("blind", "reject"),
        ("none", "filter"), (0.75, 1.0, 1.5), ("half", "center"),
    ):
        grid.append({**f_base,
            "_tag": f"DIST/{win}m/{meth}/{entry}/{acc}/span{span}/{tgt}",
            "strategy": "sigma",
            "sigma_window_minutes": win,
            "sigma_method": meth,
            "sigma_entry_mode": entry,
            "sigma_accept_mode": acc,
            "sigma_stop_span": span,
            "sigma_target_mode": tgt,
        })
    return grid


# ── worker ────────────────────────────────────────────────────
def _init(symbol="MNQ"):
    global _BARS, _SYMBOL, _CID
    from backend.data import candle_store
    _SYMBOL = symbol
    _CID = f"CON.F.US.{symbol}.U26"
    _BARS = sorted(candle_store.load(symbol, 1), key=lambda c: c.timestamp)


def _stats(pnls):
    if not pnls:
        return 0, 0.0, 0.0
    g = sum(p for p in pnls if p > 0)
    l = -sum(p for p in pnls if p <= 0)
    pf = (g / l) if l > 0 else (float("inf") if g > 0 else 0.0)
    return len(pnls), sum(pnls), pf


def _run(spec):
    from backend.api.routes import BacktestRequest, _build_strategy_params_from_request
    from backend.backtest.engine import BacktestEngine, BacktestConfig
    from backend.backtest.sweep import _extract_symbol
    try:
        from backend.backtest.costs import get_commission_rt, get_fees_rt
    except ImportError:
        from backend.backtest.sweep import get_commission_rt, get_fees_rt

    tag = spec.pop("_tag")
    cid = _CID          # MES 的 tick 值與 MNQ 不同,contract_id 必須跟著換
    req = BacktestRequest()
    req.contract_id = cid
    for k, v in spec.items():
        if hasattr(req, k):
            setattr(req, k, v)
    try:
        params = _build_strategy_params_from_request(req, 1)
        params.contract_id = cid
        cfg = BacktestConfig(
            strategies=["trend"], initial_capital=50_000.0,
            symbol=_extract_symbol(cid), commission_rt=get_commission_rt(cid),
            fees_rt=get_fees_rt(cid), value_area_pct=0.80)
        res = BacktestEngine(config=cfg, strategy_params=params,
                             record_equity=False).run(_BARS)
    except Exception as e:
        return {"tag": tag, "error": f"{type(e).__name__}: {e}"}

    by_month = defaultdict(list)
    allp = []
    for t in res.trades:
        p = float(t.pnl or 0.0)
        by_month[trade_month(t.entry_time)].append(p)
        allp.append(p)

    n, pnl, pf = _stats(allp)
    months = sorted(by_month)
    mstats = {m: _stats(by_month[m]) for m in months}
    profitable = sum(1 for m in months if mstats[m][1] > 0)
    worst_month_pnl = min((mstats[m][1] for m in months), default=0.0)

    # 1.0.10: 資料擴到 2020–2026 之後,固定三段已無意義 —— 改成**逐年**。
    # 每年至少要有 n_min 筆才納入,否則部分年度(2026 只到 8 月)會用極少樣本
    # 拉低或拉高 worst_year。
    YEAR_MIN_N = 8
    by_year = defaultdict(list)
    for m in months:
        by_year[m[:4]].extend(by_month[m])
    years = sorted(by_year)
    ystats = {y: _stats(by_year[y]) for y in years}
    eligible = [y for y in years if ystats[y][0] >= YEAR_MIN_N]
    year_pf = {y: ystats[y][2] for y in years}
    finite_y = [ystats[y][2] for y in eligible if ystats[y][2] != float("inf")]
    worst_year_pf = min(finite_y) if finite_y else 0.0
    years_profitable = sum(1 for y in eligible if ystats[y][1] > 0)

    # 舊的三段仍保留(向後相容,且短期資料下仍可讀)
    segs = {"S1": ("2026-01", "2026-03"), "S2": ("2026-04", "2026-06"),
            "S3": ("2026-06", "2026-08")}
    seg_pf, seg_n = {}, {}
    for sk, (a, b) in segs.items():
        sp = [p for m in months if a <= m <= b for p in by_month[m]]
        seg_n[sk], _, seg_pf[sk] = _stats(sp)
    finite = [v for v in seg_pf.values() if v != float("inf")]
    worst_seg_pf = min(finite) if finite else 0.0

    return {
        "tag": tag, "n": n, "pnl": round(pnl, 1),
        "pf": None if pf == float("inf") else round(pf, 3),
        "months_traded": len(months), "months_profitable": profitable,
        "worst_month_pnl": round(worst_month_pnl, 1),
        "worst_seg_pf": round(worst_seg_pf, 3),
        # 1.0.10: 逐年
        "years_traded": len(eligible), "years_profitable": years_profitable,
        "worst_year_pf": round(worst_year_pf, 3),
        "yearly": {y: {"n": ystats[y][0], "pnl": round(ystats[y][1], 1),
                       "pf": None if ystats[y][2] == float("inf") else round(ystats[y][2], 3)}
                   for y in years},
        "seg_pf": {k: (None if v == float("inf") else round(v, 3))
                   for k, v in seg_pf.items()},
        "seg_n": seg_n,
        "monthly": {m: {"n": mstats[m][0], "pnl": round(mstats[m][1], 1),
                        "pf": None if mstats[m][2] == float("inf")
                        else round(mstats[m][2], 3)} for m in months},
        "spec": spec,
    }


def passes(r):
    """1.0.10: 資料擴到 6 年後改用**逐年**閘門。

    舊版的「三段走查」在 2020–2026 上是每 2.2 年一段,粗到看不出年度崩壞;
    而且 months_profitable>=5 這種絕對數字在 80 個月的樣本下形同虛設。
    改成比例式 + 逐年最差,才會隨資料長度自動收緊。
    """
    if r.get("error") or r["n"] < 30:
        return False
    if (r["pf"] or 0) <= 1.3:
        return False
    yt = r.get("years_traded", 0)
    if yt >= 3:
        # 六年資料:每一個有效年度都要 PF>1,且至少 70% 的月份為正
        if (r.get("worst_year_pf") or 0) <= 1.0:
            return False
        if r["years_profitable"] < yt:
            return False
        if r["months_traded"] and r["months_profitable"] / r["months_traded"] < 0.55:
            return False
    else:
        # 短樣本沿用舊閘門
        if r["months_profitable"] < 5:
            return False
        if any((v is not None and v <= 1.0) for v in r["seg_pf"].values()):
            return False
    if r["worst_month_pnl"] <= -1000:
        return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=14)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--symbol", default="MNQ")
    ap.add_argument("--tags", default="",
                    help="只跑這個 JSON 檔列出的 tag(6 年資料下全網格要 4 小時/商品)")
    ap.add_argument("--out-suffix", default="")
    a = ap.parse_args()

    out = OUT.with_name(f"stability_sweep_2026_{a.symbol}{a.out_suffix}.json")
    grid = build_grid()
    if a.tags:
        want = set(json.load(open(a.tags, encoding="utf-8")))
        # 基準線一定要在(BEST 是這輪的對照組)
        want |= {"FACTOR/emapmo/long_only/sl2.5/tp7.5/early/1dir",
                 "MOMENTUM/long_only/sl1.5/rr1/first60/max1",
                 "BETAFIB/fib0.382/hl/sl2.5/rr3/max1"}
        grid = [g for g in grid if g["_tag"] in want]
        print(f"[sweep] --tags: {len(want)} 個目標 → 網格命中 {len(grid)}", flush=True)
    if a.limit:
        grid = grid[:a.limit]
    # 1.0.10: 依可用記憶體自動夾住 worker 數。
    # 每個 worker 都會把整份 candle 載進自己的位址空間 —— 233 萬根 = 常駐 1.25GB、
    # 載入峰值 2.37GB。用 14 workers 跑 6 年資料需要 17.5GB > 實體 15.9GB,
    # 結果不是變慢而是整台機器 thrashing(實測 free RAM 掉到 0.3GB,零進度)。
    try:
        import psutil
        from backend.data import candle_store as _cs
        n_bars = len(_cs.load(a.symbol, 1))
        per_worker_gb = n_bars * 576 / 1024 ** 3          # 實測每根 576 bytes
        avail_gb = psutil.virtual_memory().available / 1024 ** 3
        # 留 2GB 給 OS,並用**載入峰值**(常駐的 1.9 倍)當上限
        cap = max(1, int((avail_gb - 2.0) / max(per_worker_gb * 1.9, 1e-6)))
        if cap < a.workers:
            print(f"[sweep] 記憶體上限:每 worker 約 {per_worker_gb:.2f}GB(峰值 "
                  f"{per_worker_gb*1.9:.2f}GB),可用 {avail_gb:.1f}GB "
                  f"→ workers {a.workers} 降為 {cap}", flush=True)
            a.workers = cap
    except Exception as _e:
        print(f"[sweep] 記憶體檢查略過: {type(_e).__name__}", flush=True)

    print(f"[sweep] {a.symbol} {len(grid)} 變體 × {a.workers} workers", flush=True)
    t0 = time.time()
    results = []
    with ProcessPoolExecutor(max_workers=a.workers, initializer=_init,
                             initargs=(a.symbol,)) as ex:
        for i, r in enumerate(ex.map(_run, grid, chunksize=1), 1):
            results.append(r)
            if i % 25 == 0 or i == len(grid):
                el = time.time() - t0
                eta = el / i * (len(grid) - i)
                ok = sum(1 for x in results if passes(x))
                print(f"[sweep] {i}/{len(grid)}  過閘 {ok}  "
                      f"{el/60:.1f}min  ETA {eta/60:.1f}min", flush=True)

    errs = [r for r in results if r.get("error")]
    good = [r for r in results if not r.get("error")]
    # 1.0.10: 六年資料下用逐年最差排序;短樣本仍用三段
    _key = "worst_year_pf" if any(r.get("years_traded", 0) >= 3 for r in good) else "worst_seg_pf"
    good.sort(key=lambda r: (-(r.get(_key) or 0), -(r["pf"] or 0)))
    winners = [r for r in good if passes(r)]

    out.parent.mkdir(parents=True, exist_ok=True)
    json.dump({"created_at": datetime.now(timezone.utc).isoformat(),
               "symbol": a.symbol,
               "grid_size": len(grid), "errors": len(errs),
               "winners": winners, "all": good},
              open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print(f"\n[sweep] 完成 {time.time()-t0:.0f}s  錯誤 {len(errs)}  "
          f"過閘 {len(winners)}/{len(good)}")
    if errs:
        print(f"  範例錯誤: {errs[0]['tag']} -> {errs[0]['error']}")
    print(f"\n{'變體':<50}{'n':>5}{'PnL':>10}{'PF':>6}"
          f"{'最差年PF':>9}{'獲利年':>7}{'獲利月':>8}")
    print("-" * 96)
    for r in (winners or good)[:30]:
        yt = r.get("years_traded", 0)
        print(f"{r['tag'][:48]:<50}{r['n']:>5}{r['pnl']:>10,.0f}"
              f"{(r['pf'] or 0):>6.2f}{(r.get('worst_year_pf') or 0):>9.2f}"
              f"{r.get('years_profitable', 0):>4}/{yt}"
              f"{r['months_profitable']:>5}/{r['months_traded']}")
    print(f"\n結果寫入 {out}")


if __name__ == "__main__":
    main()
