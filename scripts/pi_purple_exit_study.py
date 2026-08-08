"""1.0.10: 只做多,紫系訊號當「出場」而非「進場」。

使用者的想法:紫系做空只貢獻 6.6% 獲利(MNQ 空單字面上是零),但既然紫 = 看跌,
那持多單時遇到紫系就平倉,會不會提高做多那半邊的表現?

這是把紫系從**進場訊號**改成**出場訊號**。比拿它做空合理 ——
它的方向資訊弱(47% 勝率 vs 基準 50%),但弱訊號當出場用的門檻遠低於當進場用。

比較四種做多出場:
    0  基準       SL + TP + 時間出場
    1  +同商品紫   再加:同商品出現紫系就平倉
    2  +任一紫     再加:任一商品出現紫系就平倉(QQQ/SPY 連動)
    3  只用紫      SL + 紫系平倉(無 TP、無時間出場)

用法:  python scripts/pi_purple_exit_study.py
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pi_exit_study import (  # noqa: E402
    build, at_or_after, SYMBOL_MAP, POINT_VALUE, RT_COST, DIRECTION, _utc, FLATTEN_UTC,
)


def simulate_long(i0, bars, times, width, sl_k, rr, hold_min, purple_ts):
    """做多。purple_ts = 已排序的紫系時間戳列表(空 = 不用紫系出場)。"""
    entry = bars[i0].close
    sl = entry - sl_k * width
    tp = entry + sl_k * rr * width if rr > 0 else None
    deadline = times[i0] + timedelta(minutes=hold_min) if hold_min > 0 else None
    # 第一個晚於進場的紫系訊號
    nxt = None
    for t in purple_ts:
        if t > times[i0]:
            nxt = t
            break

    for j in range(i0 + 1, min(i0 + 3000, len(bars))):
        b, t = bars[j], times[j]
        if b.low <= sl:
            return sl - entry, "SL"
        if tp is not None and b.high >= tp:
            return tp - entry, "TP"
        if nxt is not None and t >= nxt:
            return b.close - entry, "PURPLE"
        if deadline and t >= deadline:
            return b.close - entry, "TIME"
        if t.timetz().replace(tzinfo=None) >= FLATTEN_UTC and t.hour == FLATTEN_UTC.hour:
            return b.close - entry, "FLAT"
    return bars[min(i0 + 2999, len(bars) - 1)].close - entry, "EOD"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sl", type=float, default=3.5)
    ap.add_argument("--rr", type=float, default=3.0)
    ap.add_argument("--hold", type=int, default=240)
    a = ap.parse_args()

    rows = json.load(open(ROOT / "data" / "research" / "pi_signals.json", encoding="utf-8"))
    data = {s: build(s) for s in ("MNQ", "MES")}

    # 紫系時間戳:依商品分,以及全部合併
    purple_by_fut = defaultdict(list)
    purple_all = []
    longs = []
    for r in rows:
        if not r.get("symbol"):
            continue
        fut = SYMBOL_MAP[r["symbol"]]
        ts = _utc(datetime.fromisoformat(r["ts"].replace("Z", "+00:00")))
        for mk in r["marks"]:
            d = DIRECTION.get(mk["kind"], 0)
            if d < 0:
                purple_by_fut[fut].append(ts)
                purple_all.append(ts)
            elif d > 0:
                times, bars, blend = data[fut]
                i = at_or_after(times, ts)
                if i is not None and (times[i] - ts) <= timedelta(minutes=10) and blend[i]:
                    longs.append((fut, i, blend[i], mk["kind"], ts))
    for k in purple_by_fut:
        purple_by_fut[k].sort()
    purple_all.sort()

    print(f"做多訊號 {len(longs)} 筆   紫系(出場用) MNQ {len(purple_by_fut['MNQ'])} / "
          f"MES {len(purple_by_fut['MES'])}")
    print(f"設定: SL {a.sl}×blend  TP {a.rr}R  時間出場 {a.hold}m\n")

    variants = [
        ("0  基準(SL+TP+時間)", None, a.rr, a.hold),
        ("1  +同商品紫系平倉", "same", a.rr, a.hold),
        ("2  +任一紫系平倉", "any", a.rr, a.hold),
        ("3  只用 SL+紫系(無TP/無時間)", "same", 0.0, 0),
    ]

    print(f"{'變體':<30}{'n':>5}{'PnL':>10}{'PF':>7}{'勝率':>7}{'每筆':>8}{'最差單':>9}")
    print("-" * 78)
    detail = {}
    for label, mode, rr, hold in variants:
        vals, whys = [], defaultdict(int)
        for fut, i, w, kind, ts in longs:
            times, bars, _ = data[fut]
            pl = ([] if mode is None
                  else purple_by_fut[fut] if mode == "same" else purple_all)
            pts, why = simulate_long(i, bars, times, w, a.sl, rr, hold, pl)
            usd = pts * POINT_VALUE[fut] - RT_COST[fut]
            vals.append(usd)
            whys[why] += 1
        g = sum(x for x in vals if x > 0)
        l = -sum(x for x in vals if x <= 0)
        pf = g / l if l > 0 else float("inf")
        detail[label] = (vals, whys)
        print(f"{label:<30}{len(vals):>5}{sum(vals):>10,.0f}{pf:>7.2f}"
              f"{sum(1 for x in vals if x>0)/len(vals)*100:>6.0f}%"
              f"{sum(vals)/len(vals):>8,.0f}{min(vals):>9,.0f}")

    print("\n出場原因:")
    for label, (_, whys) in detail.items():
        print(f"  {label:<30}{dict(whys)}")

    print("\n逐月(比較基準 vs 同商品紫系平倉):")
    base_v = detail["0  基準(SL+TP+時間)"][0]
    pur_v = detail["1  +同商品紫系平倉"][0]
    m = defaultdict(lambda: [0.0, 0.0, 0])
    for (fut, i, w, kind, ts), b, p in zip(longs, base_v, pur_v):
        k = f"{ts:%Y-%m}"
        m[k][0] += b
        m[k][1] += p
        m[k][2] += 1
    print(f"  {'月':<10}{'n':>5}{'基準':>11}{'紫系平倉':>12}{'差':>10}")
    for k in sorted(m):
        b, p, n = m[k]
        print(f"  {k:<10}{n:>5}{b:>11,.0f}{p:>12,.0f}{p-b:>+10,.0f}")

    print("\n成本已扣。⚠️ 樣本僅 2 個月。")


if __name__ == "__main__":
    main()
