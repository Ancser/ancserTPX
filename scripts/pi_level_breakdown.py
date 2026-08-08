"""1.0.10: π 訊號依「標記種類 × 尺寸 × 位置」拆解,回答兩個問題:

  1. 空單能不能只做 π 級別(粉π),不做紫圈?
  2. 多單的不同級別要不要區分?

用實際的出場結構(SL+TP+時間出場)算損益,不是裸報酬 —— 因為級別差異可能
在有停損的情況下被吃掉。

用法:  python scripts/pi_level_breakdown.py
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
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
    ap.add_argument("--short-hold", type=int, default=60)
    a = ap.parse_args()

    rows = json.load(open(ROOT / "data" / "research" / "pi_signals.json", encoding="utf-8"))
    # 1.0.10: 濾掉07:00 PT 之前的訊號(開盤後半小時是前一交易日的重播)。
    # 不濾的話訊號數虛增 27%、標記數虛增 44%,而且方向來自已走完的行情。
    from backend.live.pi_listener import is_pre_session as _recap
    from datetime import datetime as _dt
    _n0 = len(rows)
    rows = [r for r in rows if not (
        r.get("pre_session") or
        _recap(_dt.fromisoformat(str(r["ts"]).replace("Z", "+00:00"))))]
    print(f"[PI] 濾除開盤前重播 {_n0 - len(rows)} 則 → 保留 {len(rows)} 則盤中訊號")
    data = {s: build(s) for s in ("MNQ", "MES")}

    recs = []
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
            if not d:
                continue
            if d > 0:
                pts, why = simulate(i, d, bars, times, blend[i], "sltp",
                                    a.long_sl, a.long_rr, 0)
            else:
                pts, why = simulate(i, d, bars, times, blend[i], "sl_time",
                                    a.short_sl, 0, a.short_hold)
            recs.append({
                "fut": fut, "d": d, "kind": mk["kind"], "size": mk["size"],
                "pos": mk["pos"], "ts": ts,
                "usd": pts * POINT_VALUE[fut] - RT_COST[fut],
            })

    def block(title, keyfn, subset=None, min_n=5):
        g = defaultdict(list)
        for r in recs:
            if subset and not subset(r):
                continue
            g[keyfn(r)].append(r["usd"])
        print(f"\n{title}")
        print(f"  {'分組':<24}{'n':>5}{'PnL':>10}{'PF':>7}{'勝率':>7}{'每筆':>8}")
        print("  " + "-" * 61)
        for k in sorted(g, key=lambda k: -sum(g[k])):
            v = g[k]
            if len(v) < min_n:
                continue
            gain = sum(x for x in v if x > 0)
            loss = -sum(x for x in v if x <= 0)
            pf = gain / loss if loss > 0 else float("inf")
            print(f"  {str(k)[:22]:<24}{len(v):>5}{sum(v):>10,.0f}"
                  f"{pf:>7.2f}{sum(1 for x in v if x>0)/len(v)*100:>6.0f}%"
                  f"{sum(v)/len(v):>8,.0f}")

    print(f"總計 {len(recs)} 筆")
    print(f"多: SL{a.long_sl}×blend TP{a.long_rr}R | 空: SL{a.short_sl}×blend {a.short_hold}m")

    print("\n" + "=" * 66)
    print("問題 1:空單能不能只做 π 級別?")
    print("=" * 66)
    block("空單 依標記種類", lambda r: r["kind"], lambda r: r["d"] < 0, min_n=5)
    block("空單 依標記×尺寸", lambda r: f"{r['kind']}/{r['size']}",
          lambda r: r["d"] < 0, min_n=5)
    block("空單 依商品", lambda r: f"{r['kind']}/{r['fut']}",
          lambda r: r["d"] < 0, min_n=5)

    print("\n" + "=" * 66)
    print("問題 2:多單的級別要不要區分?")
    print("=" * 66)
    block("多單 依標記種類", lambda r: r["kind"], lambda r: r["d"] > 0, min_n=5)
    block("多單 依標記×尺寸", lambda r: f"{r['kind']}/{r['size']}",
          lambda r: r["d"] > 0, min_n=5)
    block("多單 依尺寸(合併種類)", lambda r: r["size"], lambda r: r["d"] > 0, min_n=5)
    block("多單 依位置", lambda r: str(r["pos"]), lambda r: r["d"] > 0, min_n=5)
    block("多單 依商品", lambda r: r["fut"], lambda r: r["d"] > 0, min_n=5)

    print("\n成本已扣。⚠️ 樣本 2 個月;子分組的 n 很小,拆越細越不可信。")


if __name__ == "__main__":
    main()
