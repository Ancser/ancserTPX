"""1.0.10: EMAPMO 進場門檻的體制依賴診斷。

問題:`EMAPMO_LONG_THRESHOLD = -0.10` / `EMAPMO_SHORT_THRESHOLD = 0.06` 是**固定常數**,
但 PMO 由**百分比 ROC** 建構,量級隨波動率縮放。factor.py 的 docstring 已經記錄了
跨商品的失效(門檻在 MNQ 觸發 6.6% 的 5m bar、MES 只有 1.9%),並用手動的
`pmo_threshold_scale` 補償。跨**時間**的體制變化沒有對應的補償。

本腳本用生產程式碼本身(`calculate_emapmo_series`)量測:
  1. 每月 PMO 的離散度
  2. 每月門檻觸發率
  3. 與當月已實現波動的關係

若觸發率隨波動大幅擺盪 → 固定門檻確實是體制依賴的來源。

用法:  python scripts/emapmo_regime_diagnosis.py [--symbol MNQ]
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

from backend.strategy.factor import (  # noqa: E402
    calculate_emapmo_series, EMAPMO_LONG_THRESHOLD, EMAPMO_SHORT_THRESHOLD,
)
from backend.data import candle_store  # noqa: E402


def _utc(t):
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t


def to_tf(bars, minutes):
    """1m → N 分鐘,與策略端同樣以整點對齊分桶。"""
    out, cur, key = [], None, None
    for b in bars:
        t = _utc(b.timestamp)
        k = t.replace(minute=t.minute - t.minute % minutes, second=0, microsecond=0)
        if k != key:
            if cur:
                out.append(cur)
            key = k
            cur = {"ts": k, "o": b.open, "h": b.high, "l": b.low, "c": b.close}
        else:
            cur["h"] = max(cur["h"], b.high)
            cur["l"] = min(cur["l"], b.low)
            cur["c"] = b.close
    if cur:
        out.append(cur)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="MNQ")
    ap.add_argument("--tf", type=int, default=5)
    a = ap.parse_args()

    bars = sorted(candle_store.load(a.symbol, 1), key=lambda c: c.timestamp)
    if not bars:
        print(f"✘ {a.symbol} 無資料"); sys.exit(1)
    tf = to_tf(bars, a.tf)
    closes = [x["c"] for x in tf]
    print(f"[{a.symbol}] 1m {len(bars):,} → {a.tf}m {len(tf):,} 根  "
          f"{tf[0]['ts']:%Y-%m-%d} → {tf[-1]['ts']:%Y-%m-%d}")
    print(f"門檻(固定): LONG < {EMAPMO_LONG_THRESHOLD}   SHORT > {EMAPMO_SHORT_THRESHOLD}\n")

    pmo, sig = calculate_emapmo_series(closes)

    by_month = defaultdict(lambda: {"pmo": [], "ret": [], "hi": [], "lo": []})
    for x, p in zip(tf, pmo):
        if p is None:
            continue
        m = f"{x['ts']:%Y-%m}"
        by_month[m]["pmo"].append(float(p))
        by_month[m]["hi"].append(x["h"])
        by_month[m]["lo"].append(x["l"])

    print(f"{'月':<9}{'n':>7}{'PMO σ':>9}{'|PMO| p95':>11}"
          f"{'觸發率':>9}{'多':>7}{'空':>7}{'日均幅':>9}")
    print("-" * 70)
    rows = []
    for m in sorted(by_month):
        d = by_month[m]
        ps = d["pmo"]
        if len(ps) < 100:
            continue
        n = len(ps)
        sd = st.pstdev(ps)
        p95 = sorted(abs(v) for v in ps)[int(n * 0.95)]
        lo_hit = sum(1 for v in ps if v < EMAPMO_LONG_THRESHOLD)
        hi_hit = sum(1 for v in ps if v > EMAPMO_SHORT_THRESHOLD)
        fire = (lo_hit + hi_hit) / n * 100
        rng = st.median([h - l for h, l in zip(d["hi"], d["lo"])]) * (390 / a.tf) ** 0.5
        rows.append((m, n, sd, p95, fire, lo_hit / n * 100, hi_hit / n * 100, rng))
        print(f"{m:<9}{n:>7,}{sd:>9.4f}{p95:>11.4f}"
              f"{fire:>8.1f}%{lo_hit/n*100:>6.1f}%{hi_hit/n*100:>6.1f}%{rng:>9.0f}")

    if len(rows) >= 3:
        fires = [r[4] for r in rows]
        sds = [r[2] for r in rows]
        print("\n" + "=" * 70)
        print(f"觸發率 最低 {min(fires):.1f}%  最高 {max(fires):.1f}%  "
              f"倍數 {max(fires)/max(min(fires),1e-9):.1f}×")
        print(f"PMO σ  最低 {min(sds):.4f}  最高 {max(sds):.4f}  "
              f"倍數 {max(sds)/max(min(sds),1e-9):.1f}×")
        # σ 與觸發率的相關性:同向 = 固定門檻隨波動漂移
        mu_s, mu_f = sum(sds)/len(sds), sum(fires)/len(fires)
        cov = sum((s-mu_s)*(f-mu_f) for s, f in zip(sds, fires))
        den = (sum((s-mu_s)**2 for s in sds) * sum((f-mu_f)**2 for f in fires)) ** 0.5
        print(f"corr(PMO σ, 觸發率) = {cov/den if den else 0:+.3f}"
              "   (接近 +1 = 門檻完全被波動牽著走)")


if __name__ == "__main__":
    main()
