"""1.0.10: π 訊號的不對稱出場結構 —— 實際會上線的那一版。

前提(已由 engine.py:870-930 確認):TopstepX Auto OCO 的括號是**必須開啟**的基礎,
但引擎會在子單建立後用 `modify_order()` 把 SL/TP 改成策略計算的價位。
所以線上一定有 SL/TP,不存在「純時間出場、完全無 SL」的情境 ——
本腳本只測**帶 SL 的**組合,才是真實會發生的事。

使用者選定:多空用不同出場結構。歷史顯示
    藍系(做多)抱越久越好:60m PF 1.56 → 240m PF 2.80
    紫系(做空)抱越久越差:60m PF 1.12 → 240m PF 0.79

用法:  python scripts/pi_asymmetric_config.py
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pi_exit_study import (  # noqa: E402
    build, at_or_after, simulate, SYMBOL_MAP, POINT_VALUE, RT_COST, DIRECTION, _utc,
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--long-sl", type=float, default=3.5)
    ap.add_argument("--long-rr", type=float, default=3.0)
    ap.add_argument("--long-hold", type=int, default=240)
    ap.add_argument("--short-sl", type=float, default=2.5)
    ap.add_argument("--short-rr", type=float, default=2.0)
    ap.add_argument("--short-hold", type=int, default=60)
    a = ap.parse_args()

    rows = json.load(open(ROOT / "data" / "research" / "pi_signals.json", encoding="utf-8"))
    data = {s: build(s) for s in ("MNQ", "MES")}

    trades = []
    for r in rows:
        if not r.get("symbol"):
            continue
        fut = SYMBOL_MAP[r["symbol"]]
        ts = _utc(datetime.fromisoformat(r["ts"].replace("Z", "+00:00")))
        times, bars, blend = data[fut]
        i = at_or_after(times, ts)
        if i is None or (times[i] - ts) > timedelta(minutes=10) or blend[i] is None:
            continue
        for mk in r["marks"]:
            d = DIRECTION.get(mk["kind"], 0)
            if d:
                trades.append((fut, i, d, blend[i], mk["kind"], ts))

    print(f"可回測 {len(trades)} 筆\n")
    print(f"多(藍系): SL {a.long_sl}×blend  TP {a.long_rr}R  時間出場 {a.long_hold}m")
    print(f"空(紫系): SL {a.short_sl}×blend  TP {a.short_rr}R  時間出場 {a.short_hold}m\n")

    res = defaultdict(list)
    for fut, i, d, w, kind, ts in trades:
        times, bars, _ = data[fut]
        if d > 0:
            sl_k, rr, hold = a.long_sl, a.long_rr, a.long_hold
        else:
            sl_k, rr, hold = a.short_sl, a.short_rr, a.short_hold
        # 帶 SL + TP + 時間出場(三者先觸發者為準)—— 與線上的括號結構一致
        pts, why = simulate(i, d, bars, times, w, "sltp", sl_k, rr, 0)
        pts_t, why_t = simulate(i, d, bars, times, w, "sl_time", sl_k, 0, hold)
        # sltp 沒有時間出場;取「先發生」的近似:若 sl_time 更早結束就用它
        use_pts, use_why = (pts, why) if why in ("SL", "TP") and why_t == "TIME" else (pts_t, why_t)
        usd = use_pts * POINT_VALUE[fut] - RT_COST[fut]
        res["全部"].append((usd, use_why, ts))
        res["多(藍系)" if d > 0 else "空(紫系)"].append((usd, use_why, ts))
        res[f"{'多' if d>0 else '空'}/{fut}"].append((usd, use_why, ts))

    def line(k):
        v = [x for x, _, _ in res[k]]
        if not v:
            return
        g = sum(x for x in v if x > 0)
        l = -sum(x for x in v if x <= 0)
        pf = g / l if l > 0 else float("inf")
        wins = sum(1 for x in v if x > 0)
        worst = min(v)
        print(f"  {k:<14} n={len(v):>4}  ${sum(v):>8,.0f}  PF={pf:>5.2f}  "
              f"勝{wins/len(v)*100:>3.0f}%  最差單 ${worst:>7,.0f}  "
              f"每筆 ${sum(v)/len(v):>6,.0f}")

    for k in ("全部", "多(藍系)", "空(紫系)", "多/MNQ", "多/MES", "空/MNQ", "空/MES"):
        line(k)

    print("\n出場原因分布:")
    for k in ("多(藍系)", "空(紫系)"):
        c = defaultdict(int)
        for _, w, _ in res[k]:
            c[w] += 1
        print(f"  {k}: {dict(c)}")

    # 逐月,看穩定度
    print("\n逐月(全部):")
    m = defaultdict(list)
    for usd, _, ts in res["全部"]:
        m[f"{ts:%Y-%m}"].append(usd)
    for k in sorted(m):
        v = m[k]
        print(f"  {k}  n={len(v):>3}  ${sum(v):>8,.0f}")

    print("\n成本已扣:MNQ 14t/趟、MES $7/趟。")
    print("⚠️ 樣本僅 2026-06-11 → 08-07 兩個月,且 22 組裡挑出的結構有選擇偏差。")


if __name__ == "__main__":
    main()
