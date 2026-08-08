"""1.0.10: π 訊號的出場結構研究 —— 四種出場方式對打。

使用者要測:
  A  atr_blend SL + atr_blend TP        (目前 BEST 的結構)
  B  純時間出場 (N 分鐘平倉,無 SL/TP)
  C  atr_blend SL only + 抱到自動停止    (盤末強平)
  D  atr_blend SL + 時間出場             (兩者取先觸發)

並分開看多空:藍系(淡蓝圈/深蓝圈/青π)做多、紫系(紫圈/粉π)做空。
使用者的觀察:做空若採用長抱結構會不會更好。

進場:訊號時間戳當根 1m 收盤(市價進場的保守近似)。
      QQQ → MNQ、SPY → MES。
出場:逐根 1m 前進,先觸發者為準;盤末(19:45 UTC 強平)一律平倉。

用法:
    python scripts/pi_exit_study.py
    python scripts/pi_exit_study.py --sl 1.5,2.5,3.5 --rr 1,2,3
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend.data import candle_store  # noqa: E402

SYMBOL_MAP = {"QQQ": "MNQ", "SPY": "MES"}
POINT_VALUE = {"MNQ": 2.0, "MES": 5.0}
TICK = 0.25
# 每口每趟往返成本(佣金+手續+滑價),與其他研究同口徑
RT_COST = {"MNQ": 14 * TICK * 2.0, "MES": 7.0}
FLATTEN_UTC = dtime(19, 45)          # 盤末強平(與 bot 一致)

DIRECTION = {"淡蓝圈": +1, "深蓝圈": +1, "青π": +1, "紫圈": -1, "粉π": -1}


def _utc(t):
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t


def build(sym):
    """回傳 (times, bars, atr_blend_by_index)。atr_blend 用 5m ATR14/ATR50 平均。"""
    bars = sorted(candle_store.load(sym, 1), key=lambda c: c.timestamp)
    times = [_utc(b.timestamp) for b in bars]

    # 5m 聚合
    f, cur, key = [], None, None
    idx5 = []                                  # 每根 1m 對應的 5m 索引
    for i, b in enumerate(bars):
        t = times[i]
        k = t.replace(minute=t.minute - t.minute % 5, second=0, microsecond=0)
        if k != key:
            if cur:
                f.append(cur)
            key = k
            cur = {"h": b.high, "l": b.low, "c": b.close}
        else:
            cur["h"] = max(cur["h"], b.high)
            cur["l"] = min(cur["l"], b.low)
            cur["c"] = b.close
        idx5.append(len(f))
    if cur:
        f.append(cur)

    # True Range → Wilder ATR14 / ATR50
    tr = [0.0] * len(f)
    for i in range(1, len(f)):
        pc = f[i - 1]["c"]
        tr[i] = max(f[i]["h"] - f[i]["l"], abs(f[i]["h"] - pc), abs(pc - f[i]["l"]))

    def wilder(n):
        out = [None] * len(f)
        if len(f) <= n:
            return out
        prev = sum(tr[1:n + 1]) / n
        out[n] = prev
        for i in range(n + 1, len(f)):
            prev = (prev * (n - 1) + tr[i]) / n
            out[i] = prev
        return out

    a14, a50 = wilder(14), wilder(50)
    blend = [None if (a14[i] is None or a50[i] is None) else (a14[i] + a50[i]) / 2
             for i in range(len(f))]
    return times, bars, [blend[min(j, len(blend) - 1)] for j in idx5]


def at_or_after(times, ts):
    lo, hi = 0, len(times)
    while lo < hi:
        mid = (lo + hi) // 2
        if times[mid] < ts:
            lo = mid + 1
        else:
            hi = mid
    return lo if lo < len(times) else None


def simulate(i0, d, bars, times, width, mode, sl_k, rr, hold_min):
    """回傳(點數損益, 出場原因)。d=+1 多 / −1 空。"""
    entry = bars[i0].close
    sl = tp = None
    if mode in ("sltp", "sl_only", "sl_time"):
        sl = entry - d * sl_k * width
    if mode == "sltp":
        tp = entry + d * sl_k * rr * width
    deadline = times[i0] + timedelta(minutes=hold_min) if mode in ("time", "sl_time") else None

    for j in range(i0 + 1, min(i0 + 3000, len(bars))):
        b, t = bars[j], times[j]
        if sl is not None:
            hit = (b.low <= sl) if d > 0 else (b.high >= sl)
            if hit:
                return d * (sl - entry), "SL"
        if tp is not None:
            hit = (b.high >= tp) if d > 0 else (b.low <= tp)
            if hit:
                return d * (tp - entry), "TP"
        if deadline and t >= deadline:
            return d * (b.close - entry), "TIME"
        # 盤末強平(所有模式都套用,C 模式的「自動停止」就是這個)
        if t.timetz().replace(tzinfo=None) >= FLATTEN_UTC and t.hour == FLATTEN_UTC.hour:
            return d * (b.close - entry), "FLAT"
    return d * (bars[min(i0 + 2999, len(bars) - 1)].close - entry), "EOD"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sl", default="2.5")
    ap.add_argument("--rr", default="2")
    ap.add_argument("--hold", default="120")
    a = ap.parse_args()

    rows = json.load(open(ROOT / "data" / "research" / "pi_signals.json", encoding="utf-8"))
    data = {s: build(s) for s in ("MNQ", "MES")}
    for s, (t, b, _) in data.items():
        print(f"  {s}: {len(b):,} 根")

    sls = [float(x) for x in a.sl.split(",")]
    rrs = [float(x) for x in a.rr.split(",")]
    holds = [int(x) for x in a.hold.split(",")]

    # 展開成「一個標記 = 一筆交易」
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
                trades.append((fut, i, d, blend[i], mk["kind"]))
    print(f"\n可回測交易 {len(trades)} 筆"
          f"(多 {sum(1 for t in trades if t[2]>0)} / 空 {sum(1 for t in trades if t[2]<0)})\n")

    def report(label, mode, sl_k, rr, hold_min):
        buckets = defaultdict(list)
        for fut, i, d, w, kind in trades:
            times, bars, _ = data[fut]
            pts, why = simulate(i, d, bars, times, w, mode, sl_k, rr, hold_min)
            usd = pts * POINT_VALUE[fut] - RT_COST[fut]
            buckets["全部"].append((usd, why))
            buckets["多(藍系)" if d > 0 else "空(紫系)"].append((usd, why))
        out = []
        for k in ("全部", "多(藍系)", "空(紫系)"):
            v = [x for x, _ in buckets[k]]
            if not v:
                continue
            g = sum(x for x in v if x > 0)
            l = -sum(x for x in v if x <= 0)
            pf = g / l if l > 0 else float("inf")
            out.append(f"{k} n={len(v)} ${sum(v):>7,.0f} PF={pf:>4.2f} "
                       f"勝{sum(1 for x in v if x>0)/len(v)*100:>3.0f}%")
        why = defaultdict(int)
        for _, w in buckets["全部"]:
            why[w] += 1
        print(f"{label:<34}" + " | ".join(out))
        print(f"{'':<34}出場: {dict(why)}")

    print("=" * 108)
    for sl_k in sls:
        for rr in rrs:
            report(f"A  SL{sl_k}×blend + TP{rr}R", "sltp", sl_k, rr, 0)
    for h in holds:
        report(f"B  純時間出場 {h}m", "time", 0, 0, h)
    for sl_k in sls:
        report(f"C  SL{sl_k}×blend only + 盤末強平", "sl_only", sl_k, 0, 0)
    for sl_k in sls:
        for h in holds:
            report(f"D  SL{sl_k}×blend + {h}m 時間出場", "sl_time", sl_k, 0, h)
    print("=" * 108)
    print("成本已扣:MNQ 14t/趟、MES $7/趟。1 點 = MNQ $2 / MES $5。")


if __name__ == "__main__":
    main()
