"""1.0.9: SL 倍數 x RR 在「原始訊號母體」上的驗證。

為什麼要這個:實際成交只有 21 筆(被 max_trades_per_day / daily_loss_stop
砍到剩這麼多),在 21 筆上比較 PF 3.40 vs 3.25 是沒有意義的 —— 標準誤遠大於
差異。原始 early-long 訊號母體有 279 筆,才有檢定力。

母體 = 每個滿足 BEST 訊號條件(long_only + early)的 5m bar 都算一筆,
不套用任何每日上限。出場一律用 SL/TP 命中先到者。

輸出:
  1. 全母體的 SL x RR 網格(PF / 勝率 / 每筆期望值)
  2. 三段走查 —— 高原是否在每一段都成立
  3. 鄰域穩定度 —— 尖峰 vs 高原的量化比較
  4. 加上 ATR 下限後的交互作用

用法: python scripts/sl_rr_population_test.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from backend.data import candle_store  # noqa: E402
from backend.strategy.factor import calculate_emapmo_series  # noqa: E402

PV, SIZE = 2.0, 1               # 1 口口徑(PF/勝率與手數無關)
COMMISSION_RT = 1.50 + 2.22
MAX_HOLD_BARS = 2000
SL_MULTS = (2.0, 2.5, 3.0, 3.5, 4.0)
RRS = (2, 3, 4)
LOG = lambda *a: (print(*a), sys.stdout.flush())


def to_5m(bars):
    out, k0, o, h, l, c, ts = [], None, None, None, None, None, None
    for b in bars:
        k = b.timestamp.replace(second=0, microsecond=0)
        k = k.replace(minute=k.minute - k.minute % 5)
        if k0 is None:
            k0, o, h, l, c, ts = k, b.open, b.high, b.low, b.close, k
        elif k != k0:
            out.append((ts, o, h, l, c))
            k0, o, h, l, c, ts = k, b.open, b.high, b.low, b.close, k
        else:
            h = max(h, b.high); l = min(l, b.low); c = b.close
    out.append((ts, o, h, l, c))
    return out


def sma(a, n):
    o = np.full(len(a), np.nan)
    cs = np.cumsum(np.insert(a, 0, 0)); o[n - 1:] = (cs[n:] - cs[:-n]) / n
    return o


def pf_of(p):
    p = np.asarray(p)
    if not p.size:
        return 0.0
    g, l = p[p > 0].sum(), -p[p < 0].sum()
    return float(g / l) if l > 0 else 99.0


def main() -> None:
    bars = sorted(candle_store.load("MNQ", 1), key=lambda c: c.timestamp)
    m5 = to_5m(bars)
    T = [x[0] for x in m5]
    O = np.array([x[1] for x in m5]); H = np.array([x[2] for x in m5])
    L = np.array([x[3] for x in m5]); C = np.array([x[4] for x in m5])
    n = len(C)

    pmo, sig = calculate_emapmo_series(list(C))
    pmo = np.array([np.nan if v is None else v for v in pmo])
    sig = np.array([np.nan if v is None else v for v in sig])
    pc = np.concatenate([[C[0]], C[:-1]])
    tr = np.maximum(H - L, np.maximum(np.abs(H - pc), np.abs(L - pc)))
    blend = (sma(tr, 14) + sma(tr, 50)) / 2.0
    qgap = sig - pmo

    idx = []
    for i in range(200, n - 1):
        if not np.isfinite(blend[i]) or blend[i] <= 0:
            continue
        if not (np.isfinite(sig[i]) and np.isfinite(pmo[i])):
            continue
        if sig[i] < -0.10 and pmo[i] < sig[i] and qgap[i] < qgap[i - 1] < qgap[i - 2]:
            idx.append(i)
    LOG(f"MNQ 5m {n} 根 {T[0]:%Y-%m-%d}→{T[-1]:%Y-%m-%d};"
        f"early-long 訊號母體 {len(idx)} 筆(實際成交僅 21 筆)\n")

    def simulate(mult, rr):
        pnl = []
        for i in idx:
            entry = O[i + 1]
            slw = mult * blend[i]
            tpw = slw * rr
            sl, tp = entry - slw, entry + tpw
            out = None
            for j in range(i + 1, min(n, i + MAX_HOLD_BARS)):
                if L[j] <= sl:
                    out = -slw; break
                if H[j] >= tp:
                    out = tpw; break
            if out is None:
                out = C[min(n - 1, i + MAX_HOLD_BARS - 1)] - entry
            pnl.append(out * PV * SIZE - COMMISSION_RT * SIZE)
        return np.array(pnl)

    grid = {}
    LOG("=" * 72)
    LOG(f"全母體 {len(idx)} 筆 — SL 倍數 x RR")
    LOG("=" * 72)
    LOG(f"{'SL':>5}" + "".join(f"{'RR' + str(r):>21}" for r in RRS))
    LOG(f"{'':>5}" + "".join(f"{'PF   勝率   每筆$':>21}" for _ in RRS))
    for mult in SL_MULTS:
        line = f"{mult:>5.1f}"
        for rr in RRS:
            p = simulate(mult, rr)
            grid[(mult, rr)] = p
            line += f"{pf_of(p):>8.2f}{100 * (p > 0).mean():>6.1f}%{p.mean():>+7.0f}"
        LOG(line)

    LOG("\n" + "=" * 72)
    LOG("三段走查 — 高原是否每段都成立")
    LOG("=" * 72)
    k = len(idx) // 3
    segs = [(0, k), (k, 2 * k), (2 * k, len(idx))]
    cands = [(2.5, 3), (3.0, 2), (2.5, 2), (3.5, 2), (3.0, 3)]
    LOG(f"{'配置':<14}" + "".join(f"{'第' + str(s + 1) + '段 PF':>12}" for s in range(3))
        + f"{'全部 PF':>12}{'三段全>1.5':>12}")
    for mult, rr in cands:
        p = grid[(mult, rr)]
        pfs = [pf_of(p[a:b]) for a, b in segs]
        allok = "✓" if all(x > 1.5 for x in pfs) else "✗"
        tag = f"SL{mult:g}/RR{rr}" + (" ←現行" if (mult, rr) == (2.5, 3) else "")
        LOG(f"{tag:<14}" + "".join(f"{x:>12.2f}" for x in pfs)
            + f"{pf_of(p):>12.2f}{allok:>12}")

    LOG("\n" + "=" * 72)
    LOG("鄰域穩定度 — 把任一維推一格,PF 掉多少")
    LOG("=" * 72)
    for mult, rr in ((2.5, 3), (3.0, 2)):
        base = pf_of(grid[(mult, rr)])
        nb = []
        for m2 in SL_MULTS:
            if m2 != mult and (m2, rr) in grid:
                nb.append((f"SL{m2:g}", pf_of(grid[(m2, rr)])))
        for r2 in RRS:
            if r2 != rr and (mult, r2) in grid:
                nb.append((f"RR{r2}", pf_of(grid[(mult, r2)])))
        vals = [v for _, v in nb]
        LOG(f"\n  SL{mult:g}/RR{rr}  本身 PF={base:.2f}")
        LOG(f"    鄰居: " + "  ".join(f"{k2}={v:.2f}" for k2, v in nb))
        LOG(f"    鄰居中位 {np.median(vals):.2f}  最低 {min(vals):.2f}  "
            f"相對本身的最大跌幅 {100 * (1 - min(vals) / base):.0f}%")

    LOG("\n" + "=" * 72)
    LOG("與 ATR 下限的交互作用(下限取母體 p25)")
    LOG("=" * 72)
    atr = np.array([blend[i] for i in idx])
    thr = float(np.percentile(atr, 25))
    keep = atr >= thr
    LOG(f"  ATR 下限 {thr:.1f} 點 → 保留 {keep.sum()}/{len(idx)} 筆\n")
    LOG(f"{'配置':<14}{'全母體 PF':>12}{'加ATR下限 PF':>15}{'改善':>10}")
    for mult, rr in cands:
        p = grid[(mult, rr)]
        LOG(f"{f'SL{mult:g}/RR{rr}':<14}{pf_of(p):>12.2f}{pf_of(p[keep]):>15.2f}"
            f"{pf_of(p[keep]) - pf_of(p):>+10.2f}")

    Path("data/research").mkdir(parents=True, exist_ok=True)
    Path("data/research/sl_rr_population_test.json").write_text(json.dumps({
        "signals": len(idx),
        "grid": {f"SL{m:g}_RR{r}": {"pf": pf_of(v), "win": float((v > 0).mean()),
                                    "mean": float(v.mean()), "n": int(v.size)}
                 for (m, r), v in grid.items()},
        "atr_floor": thr,
    }, indent=1), encoding="utf-8")
    LOG("\nreport: data/research/sl_rr_population_test.json")


if __name__ == "__main__":
    main()
