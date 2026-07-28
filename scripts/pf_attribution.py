"""1.0.9: PF 歸因 —— 為什麼實際 BEST 是 PF 3.4,而母體研究只有 1.66?

279 個原始訊號最後只成交 21 筆,中間有三道過濾。這支腳本逐層加上去,
看 PF 是在哪一層被推高的:

  L0  全部訊號,允許重疊持倉      ← scripts/sl_rr_population_test.py 測的是這個
  L1  + 一次只持有一個部位       ← 引擎的真實行為(訊號在持倉中會被忽略)
  L2  + max_trades_per_day = 3
  L3  + daily_loss_stop = 1      ← 當日第一筆虧損後停止新單

L0 的問題:它把「同一波行情裡連續發射的訊號」全部算成獨立交易。那些訊號
高度相關(同一根 5m bar 附近反覆觸發),等於把同一次判斷重複計分,而且
在下跌途中會累積一整串必敗的重疊倉 —— 現實中根本不會發生。

用法: python scripts/pf_attribution.py
"""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from backend.backtest.engine import _topstep_trade_date  # noqa: E402
from backend.data import candle_store  # noqa: E402
from backend.strategy.factor import calculate_emapmo_series  # noqa: E402

SL_MULT, RR = 2.5, 3
PV, SIZE = 2.0, 1
COMMISSION_RT = 1.50 + 2.22
MAX_HOLD = 2000
LOG = lambda *a: (print(*a), sys.stdout.flush())


def to_5m(bars):
    out, k0, o, h, l, c, ts = [], None, None, None, None, None, None
    for b in bars:
        k = b.timestamp.replace(second=0, microsecond=0)
        k = k.replace(minute=k.minute - k.minute % 5)
        if k0 is None:
            k0, o, h, l, c, ts = k, b.open, b.high, b.low, b.close, k
        elif k != k0:
            out.append((ts, o, h, l, c)); k0, o, h, l, c, ts = k, b.open, b.high, b.low, b.close, k
        else:
            h = max(h, b.high); l = min(l, b.low); c = b.close
    out.append((ts, o, h, l, c))
    return out


def sma(a, n):
    o = np.full(len(a), np.nan); cs = np.cumsum(np.insert(a, 0, 0))
    o[n - 1:] = (cs[n:] - cs[:-n]) / n
    return o


def pf_of(p):
    p = np.asarray(p, float)
    if not p.size:
        return 0.0
    g, l = p[p > 0].sum(), -p[p < 0].sum()
    return float(g / l) if l > 0 else 99.0


def report(name, trades):
    if not trades:
        LOG(f"{name:<34}{'(無交易)':>8}")
        return
    p = np.array([t["pnl"] for t in trades])
    LOG(f"{name:<34}{len(p):>6}{pf_of(p):>9.2f}{100 * (p > 0).mean():>8.1f}%"
        f"{p.sum():>+10.0f}{p.mean():>+9.0f}")


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

    sigs = []
    for i in range(200, n - 1):
        if not np.isfinite(blend[i]) or blend[i] <= 0:
            continue
        if not (np.isfinite(sig[i]) and np.isfinite(pmo[i])):
            continue
        if not (sig[i] < -0.10 and pmo[i] < sig[i] and qgap[i] < qgap[i - 1] < qgap[i - 2]):
            continue
        entry = O[i + 1]
        slw = SL_MULT * blend[i]; tpw = slw * RR
        sl, tp = entry - slw, entry + tpw
        out, exit_i = None, min(n - 1, i + MAX_HOLD - 1)
        for j in range(i + 1, min(n, i + MAX_HOLD)):
            if L[j] <= sl:
                out, exit_i = -slw, j; break
            if H[j] >= tp:
                out, exit_i = tpw, j; break
        if out is None:
            out = C[exit_i] - entry
        sigs.append({"i": i, "exit_i": exit_i, "t": T[i],
                     "pnl": out * PV * SIZE - COMMISSION_RT * SIZE})

    LOG(f"BEST 訊號條件 (long_only + early, SL{SL_MULT:g} RR{RR}) — "
        f"原始訊號 {len(sigs)} 筆\n")
    LOG(f"{'層級':<34}{'筆數':>6}{'PF':>9}{'勝率':>9}{'總計$':>10}{'每筆$':>9}")
    LOG("-" * 77)

    report("L0 全訊號(允許重疊持倉)", sigs)

    # L1 一次只持有一個部位
    l1, busy_until = [], -1
    for s in sigs:
        if s["i"] <= busy_until:
            continue
        l1.append(s); busy_until = s["exit_i"]
    report("L1 + 一次一個部位", l1)

    # L2 每日最多 3 筆
    l2, cnt = [], defaultdict(int)
    busy_until = -1
    for s in sigs:
        if s["i"] <= busy_until:
            continue
        d = _topstep_trade_date(s["t"])
        if cnt[d] >= 3:
            continue
        l2.append(s); cnt[d] += 1; busy_until = s["exit_i"]
    report("L2 + 每日最多 3 筆", l2)

    # L3 當日第一筆虧損後停新單
    l3, cnt, lost = [], defaultdict(int), defaultdict(int)
    busy_until = -1
    for s in sigs:
        if s["i"] <= busy_until:
            continue
        d = _topstep_trade_date(s["t"])
        if cnt[d] >= 3 or lost[d] >= 1:
            continue
        l3.append(s); cnt[d] += 1; busy_until = s["exit_i"]
        if s["pnl"] < 0:
            lost[d] += 1
    report("L3 + 日虧鎖單(實際 BEST)", l3)

    # 被各層濾掉的那些訊號本身表現如何
    LOG("\n" + "=" * 77)
    LOG("被濾掉的訊號本身賺不賺?(這決定過濾是真本事還是事後諸葛)")
    LOG("=" * 77)
    keep1 = {id(s) for s in l1}
    dropped_overlap = [s for s in sigs if id(s) not in keep1]
    report("被『重疊持倉』濾掉的", dropped_overlap)
    keep3 = {id(s) for s in l3}
    dropped_caps = [s for s in l1 if id(s) not in keep3]
    report("被『每日上限+日虧鎖』濾掉的", dropped_caps)

    LOG("\n" + "=" * 77)
    LOG("日虧鎖單的資訊性檢驗")
    LOG("=" * 77)
    byday = defaultdict(list)
    for s in l1:
        byday[_topstep_trade_date(s["t"])].append(s)
    first, later_after_loss, later_after_win = [], [], []
    for d, lst in byday.items():
        for k, s in enumerate(lst):
            if k == 0:
                first.append(s)
            elif any(x["pnl"] < 0 for x in lst[:k]):
                later_after_loss.append(s)
            else:
                later_after_win.append(s)
    report("當日第 1 筆", first)
    report("當日後續(前面已有虧損)", later_after_loss)
    report("當日後續(前面全贏)", later_after_win)


if __name__ == "__main__":
    main()
