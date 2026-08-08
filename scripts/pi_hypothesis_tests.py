"""1.0.10: PI 訊號的八個假設檢定(使用者 2026-08-08)。

  H1  大圈 / 中 / 小 威力是否不同?值不值得各給一組設定?
  H2  大深色圈(深蓝圈)是不是比 青π 更強?
  H3  當天不行、但隔天或夜盤才起飛的有多少?盤末強平砍掉多少利潤?
  H4  出現「大」等級時,平掉舊倉重新進場會怎樣?
  H5  深蓝圈 出現時取代 青π 倉位重新進場?
  H6  圈圈密集出現時,限制 1 筆/日 能不能避免連續失敗?
  H7  空單:被軋空 vs 大多數時候能吃到 10 分鐘 —— 超短持有是否更好?
  H8  小藍/小紅圈改用更小的 atr_blend SL,是否能避免大虧又保住小漲?

共用 pi_long_only_study 的資料建構與模擬核心,但擴充了:
  - flatten 模式(session / nextday / none)—— 跨夜持倉要用
  - cut_at ——「被更強訊號搶倉」時把當前部位在指定時間平掉
  - 依 (kind, size) 給不同 sl/rr/hold

⚠️ 分組後每格 n 都是個位到二十幾。這裡的目的是**看方向與量級**,
   不是估計績效。任何單一格點的漂亮數字都不要當真。

用法:  python scripts/pi_hypothesis_tests.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend.data.pi_history import load_rows  # noqa: E402
from backend.data import candle_store              # noqa: E402

SYMBOL_MAP = {"QQQ": "MNQ", "SPY": "MES"}
POINT_VALUE = {"MNQ": 2.0, "MES": 5.0}
TICK = 0.25
RT_COST = {"MNQ": 14 * TICK * 2.0, "MES": 7.0}
FLATTEN_UTC = dtime(19, 45)
LONG_KINDS = ("青π", "深蓝圈", "淡蓝圈")
SHORT_KINDS = ("粉π", "紫圈")
DIRN = {k: +1 for k in LONG_KINDS} | {k: -1 for k in SHORT_KINDS}

MAX_BARS = 6000        # ~100 小時,跨夜測試需要比 3000 更長


def _utc(t):
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t


def build(sym):
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


def simulate(i0, d, bars, times, width, *, sl_k, rr, hold_min,
             flatten="session", cut_at=None):
    """回傳 (點數, 出場原因, 出場時間)。d=+1 多 / −1 空。

    flatten:
      'session'  第一個 19:45 UTC 強平(現行 bot 行為)
      'nextday'  跳過第一個,在**第二個** 19:45 UTC 才強平(過夜一晚)
      'none'     完全不強平,只靠 SL/TP/時間
    cut_at: 更強的訊號搶倉 —— 到這個時間就以市價平掉。
    """
    entry = bars[i0].close
    w = width[i0]
    sl = entry - d * sl_k * w if sl_k > 0 else None
    tp = entry + d * sl_k * rr * w if (sl_k > 0 and rr > 0) else None
    deadline = times[i0] + timedelta(minutes=hold_min) if hold_min > 0 else None
    seen_close = 0

    for j in range(i0 + 1, min(i0 + MAX_BARS, len(bars))):
        b, t = bars[j], times[j]
        if cut_at is not None and t >= cut_at:
            return d * (b.close - entry), "PREEMPT", t
        if sl is not None and ((b.low <= sl) if d > 0 else (b.high >= sl)):
            return d * (sl - entry), "SL", t
        if tp is not None and ((b.high >= tp) if d > 0 else (b.low <= tp)):
            return d * (tp - entry), "TP", t
        if deadline and t >= deadline:
            return d * (b.close - entry), "TIME", t
        if flatten != "none" and t.hour == FLATTEN_UTC.hour and \
                t.timetz().replace(tzinfo=None) >= FLATTEN_UTC:
            seen_close += 1 if (j == i0 + 1 or times[j - 1].hour != FLATTEN_UTC.hour
                                or times[j - 1].timetz().replace(tzinfo=None) < FLATTEN_UTC) else 0
            need = 1 if flatten == "session" else 2
            if seen_close >= need:
                return d * (b.close - entry), "FLAT", t
    last = min(i0 + MAX_BARS - 1, len(bars) - 1)
    return d * (bars[last].close - entry), "EOD", times[last]


def run(sigs, idx, cfg, *, one_at_a_time=True, preempt=None,
        max_per_day=0, flatten="session"):
    """cfg(kind, size) -> dict(sl_k, rr, hold_min) 或 None(跳過這個訊號)。

    preempt(kind, size) -> bool:此訊號強到可以平掉現有部位接手。
    """
    trades, day_n = [], defaultdict(int)
    open_pos = None          # (進場索引, d, sym, 出場時間, kind, size, cfg)
    out = []

    for ts, sym, kind, size in sigs:
        d0 = ts.date()
        c = cfg(kind, size)
        if c is None:
            continue
        # 現有部位還沒平 → 除非這是可搶倉的強訊號,否則丟棄
        if one_at_a_time and open_pos is not None and ts < open_pos["exit_t"]:
            if not (preempt and preempt(kind, size)):
                continue
            # 搶倉:把舊倉在此刻切掉,用實際切點的損益取代原紀錄
            p = open_pos
            times, bars, width = idx[p["sym"]]
            pts, why, xt = simulate(p["i0"], p["d"], bars, times, width,
                                    cut_at=ts, flatten=flatten, **p["c"])
            p["rec"]["usd"] = pts * POINT_VALUE[p["sym"]] - RT_COST[p["sym"]]
            p["rec"]["why"] = why
            open_pos = None
        if max_per_day and day_n[d0] >= max_per_day:
            continue
        times, bars, width = idx[sym]
        i0 = at_or_after(times, ts)
        if i0 is None or i0 + 1 >= len(bars) or width[i0] <= 0:
            continue
        d = DIRN[kind]
        pts, why, xt = simulate(i0, d, bars, times, width, flatten=flatten, **c)
        rec = {"d": d0, "usd": pts * POINT_VALUE[sym] - RT_COST[sym],
               "why": why, "sym": sym, "kind": kind, "size": size, "ts": ts}
        out.append(rec)
        day_n[d0] += 1
        open_pos = {"i0": i0, "d": d, "sym": sym, "exit_t": xt, "c": c, "rec": rec}
    return out


def stats(tr):
    if not tr:
        return None
    us = [t["usd"] for t in tr]
    g = sum(x for x in us if x > 0)
    l = -sum(x for x in us if x < 0)
    eq = peak = dd = 0.0
    for x in us:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    daily = defaultdict(float)
    for t in tr:
        daily[t["d"]] += t["usd"]
    return {"n": len(us), "pnl": sum(us), "pf": (g / l) if l else float("inf"),
            "wr": sum(1 for x in us if x > 0) / len(us), "dd": dd,
            "worst": min(daily.values()), "per": sum(us) / len(us)}


def P(lbl, s, w=30):
    if not s:
        print(f"  {lbl:<{w}} (無交易)")
        return
    pf = " inf" if s["pf"] == float("inf") else f"{s['pf']:5.2f}"
    print(f"  {lbl:<{w}} n={s['n']:>3} ${s['pnl']:>7,.0f} PF={pf} 勝{s['wr']*100:>3.0f}% "
          f"DD=${s['dd']:>6,.0f} 最差日=${s['worst']:>6,.0f} 每筆=${s['per']:>5,.0f}")


def main():
    # 1.0.10: 走共用 loader(backend/data/pi_history.py)。先前八個研究腳本
    # 各自複製一份過濾邏輯,結果 loader 修好了它們卻還在用污染資料。
    rows = load_rows()

    sigs = []
    for r in rows:
        sym = SYMBOL_MAP.get(r.get("symbol") or "")
        if not sym:
            continue
        ts = datetime.fromisoformat(str(r["ts"]).replace("Z", "+00:00")).astimezone(timezone.utc)
        for mk in r.get("marks", []):
            k = mk.get("kind")
            if k in DIRN:
                sigs.append((ts, sym, k, mk.get("size") or "?"))
    sigs.sort(key=lambda x: x[0])
    print(f"標記總數 {len(sigs)}")
    cnt = defaultdict(int)
    for _, _, k, s in sigs:
        cnt[f"{k}/{s}"] += 1
    print("  " + "  ".join(f"{k}={v}" for k, v in sorted(cnt.items(), key=lambda x: -x[1])))

    idx = {s: build(s) for s in ("MNQ", "MES")}
    BASE = dict(sl_k=4.0, rr=3.0, hold_min=0)          # 現行 preset 結構
    only = lambda ks: (lambda k, s: dict(BASE) if k in ks else None)

    # ── H1 / H2:分尺寸、分種類的威力(不含引擎約束,先看訊號本身)──
    print("\n" + "=" * 112)
    print("H1/H2  各標記×尺寸的原始威力(不套引擎約束,看訊號本身)")
    print("=" * 112)
    groups = sorted({(k, s) for _, _, k, s in sigs}, key=lambda x: (DIRN[x[0]], x[0], x[1]))
    for k, s in groups:
        sel = [x for x in sigs if x[2] == k and x[3] == s]
        if len(sel) < 4:
            continue
        P(f"{k}/{s}", stats(run(sel, idx, lambda *_: dict(BASE), one_at_a_time=False)))
    print("\n  合併尺寸(多單):")
    for s in ("大", "中", "小"):
        sel = [x for x in sigs if DIRN[x[2]] > 0 and x[3] == s]
        P(f"    多 · {s}", stats(run(sel, idx, lambda *_: dict(BASE), one_at_a_time=False)), 28)
    print("\n  深蓝圈 vs 青π 正面對決(多單,同結構):")
    for k in ("青π", "深蓝圈", "淡蓝圈"):
        sel = [x for x in sigs if x[2] == k]
        P(f"    {k}", stats(run(sel, idx, lambda *_: dict(BASE), one_at_a_time=False)), 28)

    # ── H3:盤末強平砍掉多少?隔天/夜盤才起飛的有多少?──
    print("\n" + "=" * 112)
    print("H3  盤末強平 vs 過夜 vs 不強平(多單 青π+深蓝圈,引擎約束)")
    print("=" * 112)
    L = [x for x in sigs if x[2] in ("青π", "深蓝圈")]
    for tag, fl in (("盤末強平(現行)", "session"), ("過夜到隔日盤末", "nextday"), ("不強平(只靠SL/TP)", "none")):
        P(tag, stats(run(L, idx, only(("青π", "深蓝圈")), flatten=fl)))
    print("\n  純多單 · 固定持有時間(無 SL,看訊號的自然衰減曲線):")
    for h in (10, 30, 60, 120, 240, 480, 1440, 2880):
        cf = (lambda hh: (lambda k, s: dict(sl_k=0, rr=0, hold_min=hh)))(h)
        P(f"    持有 {h:>4}m", stats(run(L, idx, cf, one_at_a_time=False, flatten="none")), 28)

    # ── H4 / H5:強訊號搶倉 ──
    print("\n" + "=" * 112)
    print("H4/H5  強訊號搶倉(平掉舊倉重新進場)")
    print("=" * 112)
    P("不搶倉(基準)", stats(run(L, idx, only(("青π", "深蓝圈")))))
    P("『大』等級搶倉", stats(run(L, idx, only(("青π", "深蓝圈")),
                                   preempt=lambda k, s: s == "大")))
    P("深蓝圈搶倉", stats(run(L, idx, only(("青π", "深蓝圈")),
                               preempt=lambda k, s: k == "深蓝圈")))
    LA = [x for x in sigs if DIRN[x[2]] > 0]
    P("(含淡蓝圈)不搶倉", stats(run(LA, idx, only(LONG_KINDS))))
    P("(含淡蓝圈)深蓝圈搶倉", stats(run(LA, idx, only(LONG_KINDS),
                                          preempt=lambda k, s: k == "深蓝圈")))

    # ── H6:密集出現時限 1 筆/日 ──
    print("\n" + "=" * 112)
    print("H6  訊號密集日 → 限制每日筆數")
    print("=" * 112)
    per_day = defaultdict(int)
    for _, _, k, _ in sigs:
        pass
    for t in sigs:
        per_day[t[0].date()] += 1
    busy = sorted(per_day.values())
    print(f"  每日標記數:中位 {busy[len(busy)//2]}  最多 {busy[-1]}  "
          f"(>=5 個標記的天數 {sum(1 for v in busy if v >= 5)}/{len(busy)})")
    for m in (0, 1, 2, 3):
        P(f"max {m or '∞'} 筆/日 · 青π+深蓝圈", stats(run(L, idx, only(("青π", "深蓝圈")), max_per_day=m)))
    for m in (0, 1, 2, 3):
        P(f"max {m or '∞'} 筆/日 · 全部藍系", stats(run(LA, idx, only(LONG_KINDS), max_per_day=m)))

    # ── H7:空單超短持有 ──
    print("\n" + "=" * 112)
    print("H7  空單:被軋空 vs 吃 10 分鐘就走")
    print("=" * 112)
    S = [x for x in sigs if DIRN[x[2]] < 0]
    for h in (5, 10, 15, 20, 30, 45, 60, 120):
        cf = (lambda hh: (lambda k, s: dict(sl_k=2.5, rr=0, hold_min=hh)))(h)
        P(f"空 · SL2.5 + {h:>3}m 出場", stats(run(S, idx, cf, one_at_a_time=False)))
    print("\n  只做 粉π(排除紫圈):")
    for h in (10, 15, 30, 60):
        cf = (lambda hh: (lambda k, s: dict(sl_k=2.5, rr=0, hold_min=hh) if k == "粉π" else None))(h)
        P(f"    粉π · {h:>3}m", stats(run(S, idx, cf, one_at_a_time=False)), 28)

    # ── H8:小尺寸用更小的 SL ──
    print("\n" + "=" * 112)
    print("H8  小尺寸訊號改用更小的 atr_blend SL")
    print("=" * 112)
    SM = [x for x in sigs if x[3] == "小"]
    print(f"  小尺寸標記 {len(SM)} 個  " +
          "  ".join(f"{k}={sum(1 for x in SM if x[2] == k)}" for k in sorted({x[2] for x in SM})))
    for k in (1.0, 1.5, 2.0, 2.5, 3.0, 4.0):
        cf = (lambda kk: (lambda kd, s: dict(sl_k=kk, rr=3.0, hold_min=0)))(k)
        P(f"小尺寸 · SL {k}×blend", stats(run(SM, idx, cf, one_at_a_time=False)))
    print("\n  依尺寸給不同 SL(小1.5 / 中3.0 / 大4.0)vs 一律 4.0 —— 多單,引擎約束:")
    P("一律 SL 4.0", stats(run(LA, idx, only(LONG_KINDS))))
    P("依尺寸 1.5/3.0/4.0", stats(run(LA, idx, lambda k, s: (
        dict(sl_k={"小": 1.5, "中": 3.0}.get(s, 4.0), rr=3.0, hold_min=0)
        if k in LONG_KINDS else None))))

    print("\n成本已扣:MNQ 14t/趟、MES $7/趟。1 點 = MNQ $2 / MES $5。")
    print("⚠️ 2026-06-11 → 08-07 兩個月。分組後每格 n 都很小,只看方向與量級。")


if __name__ == "__main__":
    main()
