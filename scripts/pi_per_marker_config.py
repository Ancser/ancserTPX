"""1.0.10: 逐標記最佳出場 + 雙商品共振 + 衝突盤點。

回答三個問題:
  1. 每個標記各自採用不同出場結構會怎樣?(逐標記掃 SL/RR/hold)
  2. MES + MNQ 在 10 分鐘內同時出 π,能不能加倉?(共振 vs 單獨)
  3. π 和圈圈時間接近時,不同 exit 策略會不會互相影響?

用法:  python scripts/pi_per_marker_config.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from itertools import product
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pi_exit_study import (  # noqa: E402
    build, at_or_after, simulate, SYMBOL_MAP, POINT_VALUE, RT_COST, DIRECTION, _utc,
)

FOCUS = ("青π", "深蓝圈", "粉π")      # 使用者要啟用的三種


def stats(v):
    g = sum(x for x in v if x > 0)
    l = -sum(x for x in v if x <= 0)
    return sum(v), (g / l if l > 0 else float("inf")), \
        sum(1 for x in v if x > 0) / len(v) * 100, sum(v) / len(v)


def main():
    rows = json.load(open(ROOT / "data" / "research" / "pi_signals.json", encoding="utf-8"))
    # 1.0.10: 濾掉每日 06:33 開盤回顧(前一交易日標記的重播,非即時訊號)。
    # 不濾的話訊號數虛增 27%、標記數虛增 44%,而且方向來自已走完的行情。
    from backend.live.pi_listener import is_open_recap as _recap
    from datetime import datetime as _dt
    _n0 = len(rows)
    rows = [r for r in rows if not (
        r.get("open_recap") or
        _recap(_dt.fromisoformat(str(r["ts"]).replace("Z", "+00:00"))))]
    print(f"[PI] 濾除開盤回顧 {_n0 - len(rows)} 則 → 保留 {len(rows)} 則盤中訊號")
    data = {s: build(s) for s in ("MNQ", "MES")}

    marks = []
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
                marks.append({"fut": fut, "i": i, "d": d, "w": blend[i],
                              "kind": mk["kind"], "ts": ts, "msg": r["id"]})

    # ── 1. 逐標記掃最佳出場 ─────────────────────────────
    print("=" * 86)
    print("問題 1:每個標記各自用不同出場結構")
    print("=" * 86)
    grid = [("sltp", sl, rr, 0) for sl, rr in product((1.5, 2.5, 3.5), (1, 2, 3))]
    grid += [("sl_time", sl, 0, h) for sl, h in product((1.5, 2.5, 3.5), (60, 120, 240))]
    grid += [("sl_only", sl, 0, 0) for sl in (2.5, 3.5)]

    best_cfg = {}
    for kind in FOCUS:
        sub = [m for m in marks if m["kind"] == kind]
        if not sub:
            continue
        res = []
        for mode, sl, rr, h in grid:
            vals = []
            for m in sub:
                times, bars, _ = data[m["fut"]]
                pts, _w = simulate(m["i"], m["d"], bars, times, m["w"], mode, sl, rr, h)
                vals.append(pts * POINT_VALUE[m["fut"]] - RT_COST[m["fut"]])
            pnl, pf, win, per = stats(vals)
            res.append((pf, pnl, win, per, mode, sl, rr, h))
        res.sort(reverse=True)
        best_cfg[kind] = res[0]
        print(f"\n{kind}(n={len(sub)})  前 4 名 / 共 {len(grid)} 組")
        print(f"  {'結構':<28}{'PnL':>9}{'PF':>7}{'勝率':>7}{'每筆':>8}")
        for pf, pnl, win, per, mode, sl, rr, h in res[:4]:
            lbl = (f"SL{sl}+TP{rr}R" if mode == "sltp"
                   else f"SL{sl}+{h}m" if mode == "sl_time" else f"SL{sl} only+盤末")
            print(f"  {lbl:<28}{pnl:>9,.0f}{pf:>7.2f}{win:>6.0f}%{per:>8,.0f}")
        # 統一結構(多 SL3.5/TP3R、空 SL2.5/60m)當對照
        uni = []
        for m in sub:
            times, bars, _ = data[m["fut"]]
            if m["d"] > 0:
                pts, _w = simulate(m["i"], m["d"], bars, times, m["w"], "sltp", 3.5, 3, 0)
            else:
                pts, _w = simulate(m["i"], m["d"], bars, times, m["w"], "sl_time", 2.5, 0, 60)
            uni.append(pts * POINT_VALUE[m["fut"]] - RT_COST[m["fut"]])
        p2, f2, w2, e2 = stats(uni)
        print(f"  {'← 統一結構(對照)':<28}{p2:>9,.0f}{f2:>7.2f}{w2:>6.0f}%{e2:>8,.0f}")

    # ── 2. 雙商品共振 ────────────────────────────────
    print("\n" + "=" * 86)
    print("問題 2:MNQ + MES 在 10 分鐘內同時出 π → 能不能加倉?")
    print("=" * 86)
    pi_marks = [m for m in marks if "π" in m["kind"]]
    pi_marks.sort(key=lambda m: m["ts"])
    conf = set()
    for a in pi_marks:
        for b in pi_marks:
            if a is b or b["fut"] == a["fut"]:
                continue
            if abs((b["ts"] - a["ts"]).total_seconds()) <= 600 and b["d"] == a["d"]:
                conf.add(id(a))
                break
    for lbl, sel in (("共振(另一商品同向 π)", lambda m: id(m) in conf),
                     ("單獨(無共振)", lambda m: id(m) not in conf)):
        for d, dl in ((+1, "多"), (-1, "空")):
            sub = [m for m in pi_marks if sel(m) and m["d"] == d]
            if len(sub) < 5:
                continue
            vals = []
            for m in sub:
                times, bars, _ = data[m["fut"]]
                if d > 0:
                    pts, _w = simulate(m["i"], d, bars, times, m["w"], "sltp", 3.5, 3, 0)
                else:
                    pts, _w = simulate(m["i"], d, bars, times, m["w"], "sl_time", 2.5, 0, 60)
                vals.append(pts * POINT_VALUE[m["fut"]] - RT_COST[m["fut"]])
            pnl, pf, win, per = stats(vals)
            print(f"  {lbl:<24}{dl}  n={len(sub):>3}  ${pnl:>7,.0f}  PF={pf:>5.2f}  "
                  f"勝{win:>3.0f}%  每筆 ${per:>5,.0f}")

    # ── 3. 衝突盤點(只看要啟用的三種) ──────────────────
    print("\n" + "=" * 86)
    print("問題 3:啟用 青π/深蓝圈(多) + 粉π(空) 後的實際衝突")
    print("=" * 86)
    act = [m for m in marks if m["kind"] in FOCUS]
    by_msg = defaultdict(list)
    for m in act:
        by_msg[m["msg"]].append(m)
    both = [k for k, v in by_msg.items() if len({x["d"] for x in v}) > 1]
    print(f"  啟用後的標記數 {len(act)}(多 {sum(1 for m in act if m['d']>0)} / "
          f"空 {sum(1 for m in act if m['d']<0)})")
    print(f"  同一則訊息多空並存: {len(both)} 則")
    # 同商品、10 分鐘內方向相反
    act.sort(key=lambda m: m["ts"])
    opp = 0
    for i, a in enumerate(act):
        for b in act[i + 1:]:
            if (b["ts"] - a["ts"]) > timedelta(minutes=10):
                break
            if b["fut"] == a["fut"] and b["d"] != a["d"]:
                opp += 1
    print(f"  同商品 10 分鐘內方向相反: {opp} 次")
    print("  → 衝突次數少 = 不同 exit 結構彼此獨立,不會互相干擾")

    print("\n成本已扣。⚠️ 逐標記調參是在 20 組裡挑最佳,n 又小,選擇偏差很高。")


if __name__ == "__main__":
    main()
