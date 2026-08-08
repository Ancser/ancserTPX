"""1.0.10: PI 純多單的完整結構研究。

回答四個問題(使用者 2026-08-08):
  1. 全部做多要用哪種持倉結構?
  2. trail SL/TP 有沒有幫助?
  3. max trade 之類的風控呢?
  4. atr_blend 要用哪個倍數?

**方法上的兩個硬規矩**(見 memory feedback_sweep_must_reproduce_preset):
  - 先逐筆重現現行 preset,對不上就不要往下看任何掃描結果
  - 每個維度都要確認真的會改變結果,而且要看**高原**不是看尖峰
    —— n 只有 34/84,任何格點的「最佳」都必然好看

資料已濾除每日 06:33 開盤回顧(見 backend/live/pi_listener.is_open_recap)。

用法:
    python scripts/pi_long_only_study.py
    python scripts/pi_long_only_study.py --set long_all
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
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend.data import candle_store              # noqa: E402
from backend.live.pi_listener import is_open_recap  # noqa: E402

SYMBOL_MAP = {"QQQ": "MNQ", "SPY": "MES"}
POINT_VALUE = {"MNQ": 2.0, "MES": 5.0}
TICK = 0.25
RT_COST = {"MNQ": 14 * TICK * 2.0, "MES": 7.0}     # 往返成本,與其他 PI 研究同口徑
FLATTEN_UTC = dtime(19, 45)

LONG_SETS = {
    "long_pi_only": ("青π", "深蓝圈"),
    "long_all":     ("青π", "深蓝圈", "淡蓝圈"),
}


def _utc(t):
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t


def build(sym):
    """(times, bars, width_by_index)。width = 5m ATR14/ATR50 平均(atr_blend)。"""
    bars = sorted(candle_store.load(sym, 1), key=lambda c: c.timestamp)
    times = [_utc(b.timestamp) for b in bars]
    f, cur, key, idx5 = [], None, None, []
    for i, b in enumerate(bars):
        t = times[i]
        k = t.replace(minute=t.minute - t.minute % 5, second=0, microsecond=0)
        if k != key:
            if cur:
                f.append(cur)
            cur = {"h": b.high, "l": b.low, "c": b.close}
            key = k
        else:
            cur["h"] = max(cur["h"], b.high)
            cur["l"] = min(cur["l"], b.low)
            cur["c"] = b.close
        idx5.append(len(f))
    if cur:
        f.append(cur)

    trs, prev = [], None
    for x in f:
        trs.append(x["h"] - x["l"] if prev is None
                   else max(x["h"] - x["l"], abs(x["h"] - prev), abs(x["l"] - prev)))
        prev = x["c"]

    def roll(n):
        out, s = [], 0.0
        for i, v in enumerate(trs):
            s += v
            if i >= n:
                s -= trs[i - n]
            out.append(s / min(i + 1, n))
        return out

    a14, a50 = roll(14), roll(50)
    blend = [(a14[i] + a50[i]) / 2 for i in range(len(f))]
    width = [blend[min(idx5[i], len(blend) - 1)] for i in range(len(bars))]
    return times, bars, width


def at_or_after(times, ts):
    lo, hi = 0, len(times)
    while lo < hi:
        mid = (lo + hi) // 2
        if times[mid] < ts:
            lo = mid + 1
        else:
            hi = mid
    return lo if lo < len(times) else None


def simulate(i0, bars, times, width, *, sl_k, rr, hold_min, trail_trig, trail_lock):
    """純多單逐根 1m 前進。回傳 (點數, 出場原因, 出場時間)。

    sl_k=0     → 不設 SL
    rr=0       → 不設 TP
    hold_min=0 → 不設時間出場
    trail_trig>0 → 價格走到 entry→TP 距離的 trail_trig 時,SL 上移到
                   entry + trail_lock×TP距離(專案既有語意,見 CLAUDE.md)
    盤末 19:45 UTC 一律強平 —— 「抱到自動停止」指的就是這個。
    """
    entry = bars[i0].close
    w = width[i0]
    sl = entry - sl_k * w if sl_k > 0 else None
    tp_dist = sl_k * rr * w if (sl_k > 0 and rr > 0) else None
    tp = entry + tp_dist if tp_dist else None
    deadline = times[i0] + timedelta(minutes=hold_min) if hold_min > 0 else None
    armed = False

    for j in range(i0 + 1, min(i0 + 3000, len(bars))):
        b, t = bars[j], times[j]
        # 先判 SL 再判 TP:同一根同時觸及時取保守的一邊
        if sl is not None and b.low <= sl:
            return sl - entry, ("TRAIL" if armed else "SL"), t
        if tp is not None and b.high >= tp:
            return tp - entry, "TP", t
        if not armed and tp_dist and trail_trig > 0 and \
                b.high >= entry + tp_dist * trail_trig:
            sl = entry + tp_dist * trail_lock
            armed = True
        if deadline and t >= deadline:
            return b.close - entry, "TIME", t
        if t.hour == FLATTEN_UTC.hour and \
                t.timetz().replace(tzinfo=None) >= FLATTEN_UTC:
            return b.close - entry, "FLAT", t
    _last = min(i0 + 2999, len(bars) - 1)
    return bars[_last].close - entry, "EOD", times[_last]


def run(sigs, idx, *, sl_k, rr, hold_min, trail_trig=0.0, trail_lock=0.05,
        max_per_day=0, daily_stop=0.0, one_at_a_time=False):
    """跑一組設定。max_per_day / daily_stop 是風控閘門(0 = 關閉)。

    ⚠️ daily_stop 必須只看**已平倉**的損益。第一版把每筆的最終損益在「進場
    當下」就記進當日累計,於是第 3 筆能看到第 1、2 筆的結果 —— 但那兩筆
    可能還沒平倉。那是前視偏差,會讓當日停損看起來遠比實際有效。
    """
    trades, day_n = [], defaultdict(int)
    pending = []                      # (出場時間, 日期, 損益) —— 尚未結算的部位
    closed_pnl = defaultdict(float)   # 已平倉的當日累計
    busy_until = None                 # one_at_a_time:目前部位的出場時間

    for ts, sym in sigs:
        d = ts.date()
        # 先把在本次進場**之前**就已平倉的部位結算掉
        still = []
        for xt, xd, xu in pending:
            if xt <= ts:
                closed_pnl[xd] += xu
            else:
                still.append((xt, xd, xu))
        pending = still

        # 引擎同時只持有一個部位 —— 訊號在持倉期間到達會被直接丟棄,
        # 不是排隊。不模擬這條的話 n 會虛增(先前 MNQ 159→38 就是這個原因)。
        if one_at_a_time and busy_until is not None and ts < busy_until:
            continue
        if max_per_day and day_n[d] >= max_per_day:
            continue
        if daily_stop and closed_pnl[d] <= -abs(daily_stop):
            continue
        times, bars, width = idx[sym]
        i0 = at_or_after(times, ts)
        if i0 is None or i0 + 1 >= len(bars) or width[i0] <= 0:
            continue
        pts, why, xt = simulate(i0, bars, times, width, sl_k=sl_k, rr=rr,
                                hold_min=hold_min, trail_trig=trail_trig,
                                trail_lock=trail_lock)
        usd = pts * POINT_VALUE[sym] - RT_COST[sym]
        day_n[d] += 1
        busy_until = xt
        pending.append((xt, d, usd))
        trades.append({"d": d, "usd": usd, "why": why, "sym": sym})
    return trades


def stats(trades):
    if not trades:
        return None
    us = [t["usd"] for t in trades]
    g = sum(x for x in us if x > 0)
    l = -sum(x for x in us if x < 0)
    eq, peak, dd = 0.0, 0.0, 0.0
    for x in us:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    daily = defaultdict(float)
    for t in trades:
        daily[t["d"]] += t["usd"]
    return {"n": len(us), "pnl": sum(us), "pf": (g / l) if l else float("inf"),
            "wr": sum(1 for x in us if x > 0) / len(us),
            "dd": dd, "worst": min(daily.values()), "best": max(daily.values()),
            "per": sum(us) / len(us),
            "why": dict(sorted(defaultdict(int, {w: sum(1 for t in trades if t["why"] == w)
                                                 for w in {t["why"] for t in trades}}).items()))}


def line(label, s, width=30):
    if not s:
        return f"  {label:<{width}}  (無交易)"
    pf = "  inf" if s["pf"] == float("inf") else f"{s['pf']:5.2f}"
    return (f"  {label:<{width}} n={s['n']:>3} ${s['pnl']:>7,.0f} PF={pf} "
            f"勝{s['wr']*100:>3.0f}% DD=${s['dd']:>6,.0f} 最差日=${s['worst']:>6,.0f} "
            f"每筆=${s['per']:>5,.0f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="long_all", choices=list(LONG_SETS))
    a = ap.parse_args()
    kinds = LONG_SETS[a.set]

    rows = json.load(open(ROOT / "data" / "research" / "pi_signals.json", encoding="utf-8"))
    n0 = len(rows)
    rows = [r for r in rows if not (r.get("open_recap") or
            is_open_recap(datetime.fromisoformat(str(r["ts"]).replace("Z", "+00:00"))))]
    print(f"[PI] 濾除開盤回顧 {n0 - len(rows)} 則 → 保留 {len(rows)} 則盤中訊號")

    sigs = []
    for r in rows:
        sym = SYMBOL_MAP.get(r.get("symbol") or "")
        if not sym:
            continue
        ts = datetime.fromisoformat(str(r["ts"]).replace("Z", "+00:00")).astimezone(timezone.utc)
        for mk in r.get("marks", []):
            if mk.get("kind") in kinds:
                sigs.append((ts, sym))
    sigs.sort()
    print(f"訊號組合 {a.set} = {kinds} → 多單 {len(sigs)} 筆\n")

    idx = {s: build(s) for s in ("MNQ", "MES")}

    # ── 0. 基準重現 ───────────────────────────────────────────────
    print("=" * 116)
    print("0. 基準:現行 preset(SL 3.5×blend / TP 3.0R / 無時間出場 / 無 trail)")
    print("=" * 116)
    base = run(sigs, idx, sl_k=3.5, rr=3.0, hold_min=0)
    bs = stats(base)
    print(line("PI 1MNQ 現行設定", bs))
    print(f"    出場原因 {bs['why']}\n")

    # ── 1. 持倉結構 ───────────────────────────────────────────────
    print("=" * 116)
    print("1. 持倉結構(SL 固定 3.5×blend,只換出場方式)")
    print("=" * 116)
    structures = [
        ("SL + TP 3R (現行)",        dict(sl_k=3.5, rr=3.0, hold_min=0)),
        ("SL + TP 2R",               dict(sl_k=3.5, rr=2.0, hold_min=0)),
        ("SL + TP 4R",               dict(sl_k=3.5, rr=4.0, hold_min=0)),
        ("SL only + 抱到盤末",        dict(sl_k=3.5, rr=0.0, hold_min=0)),
        ("純時間出場 120m(無SL)",     dict(sl_k=0.0, rr=0.0, hold_min=120)),
        ("純時間出場 240m(無SL)",     dict(sl_k=0.0, rr=0.0, hold_min=240)),
        ("SL + 時間 120m",           dict(sl_k=3.5, rr=0.0, hold_min=120)),
        ("SL + 時間 240m",           dict(sl_k=3.5, rr=0.0, hold_min=240)),
        ("SL + TP 3R + 時間 240m",   dict(sl_k=3.5, rr=3.0, hold_min=240)),
    ]
    for name, kw in structures:
        print(line(name, stats(run(sigs, idx, **kw))))

    # ── 2. atr_blend 倍數 ─────────────────────────────────────────
    print("\n" + "=" * 116)
    print("2. atr_blend SL 倍數(兩種結構各掃一遍 —— 看高原,不要挑尖峰)")
    print("=" * 116)
    for tag, rr, hold in (("SL+TP3R", 3.0, 0), ("SL only 抱到盤末", 0.0, 0)):
        print(f"\n  [{tag}]")
        for k in (1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0):
            print(line(f"    SL {k}×blend", stats(run(sigs, idx, sl_k=k, rr=rr, hold_min=hold)), 28))

    # ── 3. trail ─────────────────────────────────────────────────
    print("\n" + "=" * 116)
    print("3. trail SL(觸發 50% 鎖 5%,專案既有語意)—— 只在有 TP 時有意義")
    print("=" * 116)
    for k in (2.5, 3.0, 3.5, 4.0):
        off = stats(run(sigs, idx, sl_k=k, rr=3.0, hold_min=0))
        on = stats(run(sigs, idx, sl_k=k, rr=3.0, hold_min=0, trail_trig=0.5, trail_lock=0.05))
        print(line(f"SL{k} TP3R  trail OFF", off))
        print(line(f"SL{k} TP3R  trail ON ", on))
        if off and on:
            print(f"      → 差異 ${on['pnl']-off['pnl']:+,.0f}  PF {off['pf']:.2f}→{on['pf']:.2f}  "
                  f"DD ${off['dd']:,.0f}→${on['dd']:,.0f}")

    # ── 4. 風控 ──────────────────────────────────────────────────
    print("\n" + "=" * 116)
    print("4. 風控閘門(套在第 1 節的最佳結構上)")
    print("=" * 116)
    best_kw = dict(sl_k=3.5, rr=0.0, hold_min=0)      # SL only,由第 1 節決定是否改
    print("  基準(無閘門):")
    print(line("    無限制", stats(run(sigs, idx, **best_kw)), 28))
    print("\n  每日最多筆數:")
    for m in (1, 2, 3, 5):
        print(line(f"    max {m} 筆/日", stats(run(sigs, idx, max_per_day=m, **best_kw)), 28))
    print("\n  當日虧損停止:")
    for s in (300, 500, 800, 1200):
        print(line(f"    停損 ${s}/日", stats(run(sigs, idx, daily_stop=s, **best_kw)), 28))

    # ── 5. 逐月穩定度 ────────────────────────────────────────────
    print("\n" + "=" * 116)
    print("5. 逐月(現行結構 vs SL only)")
    print("=" * 116)
    for tag, kw in (("SL+TP3R", dict(sl_k=3.5, rr=3.0, hold_min=0)),
                    ("SL only", dict(sl_k=3.5, rr=0.0, hold_min=0))):
        m = defaultdict(float)
        nn = defaultdict(int)
        for t in run(sigs, idx, **kw):
            k = f"{t['d']:%Y-%m}"
            m[k] += t["usd"]
            nn[k] += 1
        print(f"  {tag:10} " + "  ".join(f"{k} n={nn[k]:>2} ${m[k]:>7,.0f}" for k in sorted(m)))

    print("\n成本已扣:MNQ 14t/趟、MES $7/趟。1 點 = MNQ $2 / MES $5。")
    print(f"⚠️ 樣本只有 2026-06-11 → 08-07 兩個月、{len(sigs)} 筆。")
    print("   任何格點的『最佳』在這種 n 底下都必然好看 —— 只採信整片高原一致的方向。")


if __name__ == "__main__":
    main()
