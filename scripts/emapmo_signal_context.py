"""1.0.9: EMAPMO 原始訊號母體的情境診斷 —— 為什麼有些 long 是接下跌的刀?

觀察:圖上虧損的 long 都出現在價格連續走低的過程中,而獲利的那筆是價格
已經打底反轉之後。EMAPMO long 的條件是 PMO < -0.10 且 PMO 上穿 SIG ——
本質是「超賣反彈」,而強勢下跌裡超賣會一直超賣。

但只用 21 筆成交去加過濾器必然是過擬合。這裡改用**未受每日上限限制的
原始訊號母體**(每個滿足條件的 5m bar 都算一筆),樣本大得多,才有檢定力。

每個訊號記錄進場當下的情境特徵,再依特徵分桶看勝率/期望值:
    trend_200      收盤 vs EMA200(5m ≈ 16.7 小時)
    slope_50       EMA50 斜率(近 10 根的變化率)
    newlow_50      收盤距離近 50 根最低點的位置(0 = 正在創新低)
    ret_48         近 48 根(4 小時)報酬
    atr_pct        當下 ATR 在歷史中的分位
    pmo            訊號當下的 PMO 值(多深的超賣)

輸出的是「哪個特徵能分開贏家和輸家」,不是直接給參數。

用法: python scripts/emapmo_signal_context.py
"""
from __future__ import annotations

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

SL_MULT, TP_MULT = 2.5, 7.5     # BEST: atr_blend SL2.5 / TP7.5
PV, TICK, SIZE = 2.0, 0.25, 2   # MNQ, 2 口
COMMISSION_RT = 1.50 + 2.22     # 與回測一致(佣金+費用,單口往返)
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


def ema(a, n):
    out = np.empty(len(a)); k = 2.0 / (n + 1); prev = a[0]
    for i, v in enumerate(a):
        prev = v if i == 0 else k * v + (1 - k) * prev
        out[i] = prev
    return out


def sma(a, n):
    o = np.full(len(a), np.nan)
    cs = np.cumsum(np.insert(a, 0, 0)); o[n - 1:] = (cs[n:] - cs[:-n]) / n
    return o


def main() -> None:
    bars = sorted(candle_store.load("MNQ", 1), key=lambda c: c.timestamp)
    m5 = to_5m(bars)
    T = [x[0] for x in m5]
    O = np.array([x[1] for x in m5]); H = np.array([x[2] for x in m5])
    L = np.array([x[3] for x in m5]); C = np.array([x[4] for x in m5])
    n = len(C)
    LOG(f"MNQ 5m bars: {n}  {T[0]:%Y-%m-%d} → {T[-1]:%Y-%m-%d}")

    pmo, sig = calculate_emapmo_series(list(C))
    pmo = np.array([np.nan if v is None else v for v in pmo])
    sig = np.array([np.nan if v is None else v for v in sig])

    pc = np.concatenate([[C[0]], C[:-1]])
    tr = np.maximum(H - L, np.maximum(np.abs(H - pc), np.abs(L - pc)))
    blend = (sma(tr, 14) + sma(tr, 50)) / 2.0
    e200 = ema(C, 200); e50 = ema(C, 50)

    # BEST 的訊號:long_only + early。early_long = SIG < -0.10 且 PMO < SIG
    # 且 (SIG-PMO) 連續收斂。
    qgap = sig - pmo
    sigs = []
    for i in range(200, n - 1):
        if not np.isfinite(blend[i]) or blend[i] <= 0:
            continue
        if not (np.isfinite(sig[i]) and np.isfinite(pmo[i])):
            continue
        early_long = (sig[i] < -0.10 and pmo[i] < sig[i]
                      and qgap[i] < qgap[i - 1] < qgap[i - 2])
        if not early_long:
            continue
        entry = O[i + 1]
        slw, tpw = SL_MULT * blend[i], TP_MULT * blend[i]
        sl, tp = entry - slw, entry + tpw
        out = None
        for j in range(i + 1, min(n, i + 2000)):
            if L[j] <= sl:
                out = -slw; break
            if H[j] >= tp:
                out = tpw; break
        if out is None:
            out = C[min(n - 1, i + 1999)] - entry
        pnl = out * PV * SIZE - COMMISSION_RT * SIZE
        lo50 = L[max(0, i - 49):i + 1].min()
        hi50 = H[max(0, i - 49):i + 1].max()
        sigs.append({
            "t": T[i], "pnl": pnl,
            "trend_200": (C[i] - e200[i]) / e200[i] * 100,
            "slope_50": (e50[i] - e50[i - 10]) / e50[i - 10] * 100,
            "newlow_50": (C[i] - lo50) / max(1e-9, hi50 - lo50),
            "ret_48": (C[i] - C[i - 48]) / C[i - 48] * 100,
            "atr": blend[i],
            "pmo": pmo[i],
        })

    LOG(f"原始 early-long 訊號母體: {len(sigs)} 筆 "
        f"(vs 實際成交 21 筆 —— 每日上限與日虧鎖單擋掉其餘)\n")
    pn = np.array([s["pnl"] for s in sigs])
    g, l = pn[pn > 0].sum(), -pn[pn < 0].sum()
    LOG(f"母體整體: PF={g / l:.3f}  勝率={100 * (pn > 0).mean():.1f}%  "
        f"總計=${pn.sum():+,.0f}  平均=${pn.mean():+.0f}/筆\n")

    LOG("=" * 78)
    LOG("依情境特徵分桶(每桶約等量) —— 找能分開贏輸的特徵")
    LOG("=" * 78)
    for feat, desc in (
        ("trend_200", "收盤 vs EMA200 (%)  ← 負=在長期均線下方"),
        ("slope_50",  "EMA50 斜率 (%/10根)  ← 負=下降趨勢"),
        ("ret_48",    "近4小時報酬 (%)     ← 負=正在下跌"),
        ("newlow_50", "在近50根區間的位置   ← 0=正在創新低"),
        ("pmo",       "PMO 深度            ← 越負=越超賣"),
        ("atr",       "ATR 絕對值(點)      ← 波動大小"),
    ):
        v = np.array([s[feat] for s in sigs])
        qs = np.percentile(v, [0, 25, 50, 75, 100])
        LOG(f"\n  {desc}")
        LOG(f"    {'區間':<22}{'筆數':>5}{'勝率':>8}{'平均$':>10}{'PF':>8}")
        for a, b in zip(qs[:-1], qs[1:]):
            m = (v >= a) & (v <= b) if b == qs[-1] else (v >= a) & (v < b)
            if m.sum() < 3:
                continue
            p = pn[m]
            gg, ll = p[p > 0].sum(), -p[p < 0].sum()
            LOG(f"    [{a:>8.2f}, {b:>8.2f}]{m.sum():>5}"
                f"{100 * (p > 0).mean():>7.1f}%{p.mean():>+10.0f}"
                f"{(gg / ll if ll else 99):>8.2f}")


if __name__ == "__main__":
    main()
