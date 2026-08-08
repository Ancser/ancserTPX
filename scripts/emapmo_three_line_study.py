"""1.0.10: 把 EMAPMO 的三層拆開,對齊時間軸研究。

使用者提問:「把 ema pmo sig 這些參數分離計算成三條線按時間軸疊在一起,
能不能研究出什麼?如果是中性應該能看得出什麼東西。」

EMAPMO 是三層串接:
    L1  percent ROC        →  EMA100      (第一層平滑)
    L2  L1 × 10            →  EMA50       = PMO
    L3  PMO                →  EMA10       = SIG

現行進場條件同時用到 PMO 與 SIG(normal 比 PMO、early 比 SIG,並要求交叉)。
本腳本把三層分開量測,回答:
  1. 三層各自的**分布**是否隨體制漂移(中性 = 不漂移)
  2. 三層各自對**未來報酬**的預測力(哪一層才是真正有訊息的)
  3. PMO−SIG 的差(交叉訊號的本體)有沒有比單層更好

判準是「未來 N 根 5m 的報酬」與各層當下值的相關性 —— 這是最直接的訊息量度,
不受任何進出場規則污染。

用法:  python scripts/emapmo_three_line_study.py [--symbol MNQ]
"""
from __future__ import annotations

import argparse
import statistics as st
import sys
from collections import defaultdict
from datetime import timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend.strategy.factor import _ema  # noqa: E402
from backend.data import candle_store  # noqa: E402


def _utc(t):
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t


def to_tf(bars, minutes):
    out, cur, key = [], None, None
    for b in bars:
        t = _utc(b.timestamp)
        k = t.replace(minute=t.minute - t.minute % minutes, second=0, microsecond=0)
        if k != key:
            if cur:
                out.append(cur)
            key, cur = k, {"ts": k, "c": b.close}
        else:
            cur["c"] = b.close
    if cur:
        out.append(cur)
    return out


def corr(xs, ys):
    n = len(xs)
    if n < 30:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    den = (sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys)) ** 0.5
    return cov / den if den else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="MNQ")
    ap.add_argument("--tf", type=int, default=5)
    ap.add_argument("--horizons", default="6,12,24,72")   # 30m/1h/2h/6h
    a = ap.parse_args()

    bars = sorted(candle_store.load(a.symbol, 1), key=lambda c: c.timestamp)
    tf = to_tf(bars, a.tf)
    closes = [x["c"] for x in tf]
    print(f"[{a.symbol}] {a.tf}m {len(tf):,} 根  "
          f"{tf[0]['ts']:%Y-%m-%d} → {tf[-1]['ts']:%Y-%m-%d}\n")

    # 三層分開
    roc = [None] + [None if closes[i-1] == 0 else 100.0 * (closes[i] - closes[i-1]) / closes[i-1]
                    for i in range(1, len(closes))]
    L1 = _ema(roc, 100)                                    # ROC → EMA100
    PMO = _ema([None if v is None else 10.0 * v for v in L1], 50)
    SIG = _ema(PMO, 10)
    GAP = [None if (p is None or s is None) else p - s for p, s in zip(PMO, SIG)]

    layers = {"L1 (ROC→EMA100)": L1, "PMO (L1×10→EMA50)": PMO,
              "SIG (PMO→EMA10)": SIG, "PMO−SIG (交叉本體)": GAP}

    # ── 1. 分布是否隨體制漂移 ────────────────────────
    print("=== 1. 各層的年度離散度(中性 = 各年接近)===")
    print(f"{'層':<22}" + "".join(f"{y:>8}" for y in range(2020, 2027)) + f"{'最大/最小':>10}")
    print("-" * 88)
    for name, ser in layers.items():
        by = defaultdict(list)
        for x, v in zip(tf, ser):
            if v is not None:
                by[x["ts"].year].append(float(v))
        sds = {y: st.pstdev(by[y]) for y in sorted(by) if len(by[y]) > 100}
        row = f"{name:<22}"
        for y in range(2020, 2027):
            row += f"{sds.get(y, 0):>8.4f}" if y in sds else f"{'—':>8}"
        vals = list(sds.values())
        row += f"{max(vals)/max(min(vals), 1e-9):>10.1f}×"
        print(row)

    # ── 2. 對未來報酬的預測力 ────────────────────────
    print(f"\n=== 2. 各層 vs 未來報酬的相關(訊息量;|r|>0.03 才算有東西)===")
    hs = [int(x) for x in a.horizons.split(",")]
    print(f"{'層':<22}" + "".join(f"{f'+{h*a.tf}m':>10}" for h in hs))
    print("-" * (22 + 10 * len(hs)))
    for name, ser in layers.items():
        row = f"{name:<22}"
        for h in hs:
            xs, ys = [], []
            for i in range(len(closes) - h):
                v = ser[i]
                if v is None or closes[i] == 0:
                    continue
                xs.append(float(v))
                ys.append((closes[i + h] - closes[i]) / closes[i] * 100)
            row += f"{corr(xs, ys):>+10.4f}"
        print(row)

    # ── 3. 分年看預測力是否穩定 ──────────────────────
    H = hs[len(hs) // 2]
    print(f"\n=== 3. PMO 對 +{H*a.tf}m 報酬的相關,逐年(穩定 = 各年同號)===")
    by = defaultdict(lambda: ([], []))
    for i in range(len(closes) - H):
        v = PMO[i]
        if v is None or closes[i] == 0:
            continue
        y = tf[i]["ts"].year
        by[y][0].append(float(v))
        by[y][1].append((closes[i + H] - closes[i]) / closes[i] * 100)
    for y in sorted(by):
        xs, ys = by[y]
        if len(xs) > 500:
            r = corr(xs, ys)
            bar = ("+" if r > 0 else "-") * min(30, int(abs(r) * 400))
            print(f"  {y}  r = {r:+.4f}  n={len(xs):>6,}  {bar}")


if __name__ == "__main__":
    main()
