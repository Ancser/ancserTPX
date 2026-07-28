"""1.0.9: EMAPMO 進場門檻的波動校準(MNQ 基準 → MES)。

問題: EMAPMO 的進場閘是寫死的絕對值 ——
    normal_long : pmo < -0.10      normal_short: pmo > +0.06
    early_long  : signal < -0.10   early_short : signal > +0.06
(backend/strategy/factor.py:202-203, 221-222)

而 PMO 是由「百分比」ROC 疊三層 EMA 得到:
    roc = 100*(c[i]-c[i-1])/c[i-1] → EMA100 → x10 → EMA50 → sig=EMA10

也就是說 PMO 的尺度跟商品的「百分比波動」綁定,不是點數波動。ATR 只影響
SL/TP 寬度,完全不影響這個進場閘。ES/MES 的 %波動比 NQ/MNQ 小,PMO 就更少
碰到 ±0.10 → 訊號數銳減(實測 BEST 在 MES 58 天只有 7 筆,MNQ 74 天 20 筆)。

本腳本:
  1. 用實際 5m closes 重算兩商品的 PMO/SIG 全序列。
  2. 報告分佈(std / 各分位數),量化尺度差。
  3. 解出 MES 上「與 MNQ 同等稀有度」的等效門檻(分位數對齊)。
  4. 順帶給出 std 比例法的門檻,兩法交叉檢核。

用法: python scripts/emapmo_vol_calibration.py
"""
from __future__ import annotations

import json
import sys
from datetime import timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from backend.data import candle_store  # noqa: E402
from backend.strategy.factor import calculate_emapmo_series  # noqa: E402

LONG_TH = -0.10   # 現行 normal_long / early_long 門檻
SHORT_TH = 0.06   # 現行 normal_short / early_short 門檻
LOG = lambda *a: (print(*a), sys.stdout.flush())


def resample_5m(bars):
    """1m → 5m closes(取每 5 分鐘桶的最後一根收盤)。"""
    out, bucket, cur = [], None, None
    for b in bars:
        ts = b.timestamp if b.timestamp.tzinfo else b.timestamp.replace(tzinfo=timezone.utc)
        key = ts.replace(second=0, microsecond=0)
        key = key.replace(minute=key.minute - key.minute % 5)
        if bucket is None:
            bucket, cur = key, b
        elif key != bucket:
            out.append(float(cur.close))
            bucket, cur = key, b
        else:
            cur = b
    if cur is not None:
        out.append(float(cur.close))
    return out


def pct_vol(closes) -> float:
    a = np.asarray(closes, float)
    r = np.diff(a) / a[:-1] * 100.0
    return float(np.std(r))


def main() -> None:
    stats = {}
    for sym in ("MNQ", "MES"):
        bars = candle_store.load(sym, 1)
        if not bars:
            LOG(f"[{sym}] store empty — skipped")
            continue
        bars = sorted(bars, key=lambda c: c.timestamp)
        closes = resample_5m(bars)
        pmo, sig = calculate_emapmo_series(closes)
        p = np.asarray([x for x in pmo if x is not None], float)
        s = np.asarray([x for x in sig if x is not None], float)
        stats[sym] = {
            "bars_5m": len(closes),
            "px_median": float(np.median(closes)),
            "roc_std_pct": pct_vol(closes),
            "pmo_std": float(p.std()),
            "pmo": p, "sig": s,
        }
        LOG(f"[{sym}] {len(bars)} x1m → {len(closes)} x5m  "
            f"median px {np.median(closes):.1f}  "
            f"5m %ROC std {pct_vol(closes):.4f}%  PMO std {p.std():.4f}")

    if "MNQ" not in stats or "MES" not in stats:
        LOG("need both symbols")
        return

    a, b = stats["MNQ"], stats["MES"]
    LOG("\n===== 尺度比較 =====")
    LOG(f"  %ROC std     MNQ {a['roc_std_pct']:.4f}%  MES {b['roc_std_pct']:.4f}%"
        f"   ratio MES/MNQ = {b['roc_std_pct']/a['roc_std_pct']:.3f}")
    LOG(f"  PMO std      MNQ {a['pmo_std']:.4f}   MES {b['pmo_std']:.4f}"
        f"   ratio MES/MNQ = {b['pmo_std']/a['pmo_std']:.3f}")

    LOG("\n===== 現行門檻的觸及率 =====")
    for sym in ("MNQ", "MES"):
        p, s = stats[sym]["pmo"], stats[sym]["sig"]
        LOG(f"  {sym}: PMO < {LONG_TH}  {100*(p < LONG_TH).mean():6.2f}% of bars"
            f"   |  PMO > {SHORT_TH}  {100*(p > SHORT_TH).mean():6.2f}%")
        LOG(f"        SIG < {LONG_TH}  {100*(s < LONG_TH).mean():6.2f}% of bars"
            f"   |  SIG > {SHORT_TH}  {100*(s > SHORT_TH).mean():6.2f}%")

    LOG("\n===== 校準法 A:分位數對齊(讓 MES 的門檻同樣稀有)=====")
    out = {}
    for name, key, th, tail in (("normal/early LONG", "pmo", LONG_TH, "lo"),
                                ("normal/early SHORT", "pmo", SHORT_TH, "hi")):
        ref = stats["MNQ"][key]
        tgt = stats["MES"][key]
        if tail == "lo":
            q = float((ref < th).mean())
            mes_th = float(np.percentile(tgt, q * 100))
        else:
            q = float((ref > th).mean())
            mes_th = float(np.percentile(tgt, (1 - q) * 100))
        out[name] = mes_th
        LOG(f"  {name:20s} MNQ th={th:+.3f} (稀有度 {q*100:.2f}%)"
            f"  → MES th={mes_th:+.4f}")

    # SIG 用同樣稀有度換算(early 條件用 signal)
    for name, th, tail in (("early LONG (SIG)", LONG_TH, "lo"),
                           ("early SHORT (SIG)", SHORT_TH, "hi")):
        ref, tgt = stats["MNQ"]["sig"], stats["MES"]["sig"]
        if tail == "lo":
            q = float((ref < th).mean())
            mes_th = float(np.percentile(tgt, q * 100))
        else:
            q = float((ref > th).mean())
            mes_th = float(np.percentile(tgt, (1 - q) * 100))
        out[name] = mes_th
        LOG(f"  {name:20s} MNQ th={th:+.3f} (稀有度 {q*100:.2f}%)"
            f"  → MES th={mes_th:+.4f}")

    LOG("\n===== 校準法 B:PMO std 等比縮放 =====")
    k = b["pmo_std"] / a["pmo_std"]
    LOG(f"  scale k = {k:.4f}")
    LOG(f"  LONG  {LONG_TH:+.3f} → {LONG_TH*k:+.4f}")
    LOG(f"  SHORT {SHORT_TH:+.3f} → {SHORT_TH*k:+.4f}")

    rep = Path("data/research/emapmo_vol_calibration.json")
    rep.parent.mkdir(parents=True, exist_ok=True)
    rep.write_text(json.dumps({
        "current_thresholds": {"long": LONG_TH, "short": SHORT_TH},
        "roc_std_pct": {s: stats[s]["roc_std_pct"] for s in stats},
        "pmo_std": {s: stats[s]["pmo_std"] for s in stats},
        "quantile_aligned_mes_thresholds": out,
        "std_scaled_mes_thresholds": {"long": LONG_TH * k, "short": SHORT_TH * k},
        "scale_k": k,
    }, indent=1), encoding="utf-8")
    LOG(f"\nreport: {rep}")


if __name__ == "__main__":
    main()
