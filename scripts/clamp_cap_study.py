"""1.0.9: SL/TP 上下限「夾住(clamp)」的代價評估。

需求來自 prop firm 的兩條線:
  - maxDD:單筆風險不能太大
  - consistency rule:單日獲利佔比太高會推高通關/出金門檻

做法是把 ATR 推導出的 SL/TP 等比縮放到上限(維持 RR),而不是丟掉訊號。

但這**不是免費的** —— SL 離進場更近,高波動時被掃出場的機率上升。本腳本
把同一組訊號在各種 (max_risk, max_profit) 上限下重跑,直接量化:
  勝率掉多少 / PF 掉多少 / 最大單日獲利壓到多少 / 最大單日虧損壓到多少

同時對照 "block"(超過就跳過)模式,以及「不夾、改用降手數」的等效比較。

用法: python scripts/clamp_cap_study.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from best_mes_parity_study import run_variant  # noqa: E402
from backend.backtest.engine import _topstep_trade_date  # noqa: E402
from backend.data import candle_store  # noqa: E402

SIZE = 2          # 目前 BEST 的手數
PV = 2.0          # MNQ $/point
TICK = 0.25
TICK_VALUE = PV * TICK          # 單口 1 tick = $0.50
LOG = lambda *a: (print(*a), sys.stdout.flush())


def dollars_to_ticks(d: float) -> int:
    """單日/單筆的 $ 上限 → 單口 ticks(每天最多 1 筆,故兩者等價)。"""
    return int(round(d / (TICK_VALUE * SIZE)))


def daily(trades):
    d = defaultdict(float)
    for t in trades:
        d[_topstep_trade_date(t["entry_time"])] += t["pnl"] * SIZE
    return np.array(sorted(d.values()))


def stats(trades) -> dict:
    p = np.array([t["pnl"] * SIZE for t in trades]) if trades else np.array([])
    if not len(p):
        return {"n": 0}
    g = p[p > 0].sum()
    l = -p[p < 0].sum()
    eq = np.cumsum(p)
    dd = float((np.maximum.accumulate(eq) - eq).max())
    dl = daily(trades)
    return {
        "n": len(p),
        "pnl": float(p.sum()),
        "pf": float(g / l) if l > 0 else 999.0,
        "win": float((p > 0).mean()),
        "max_dd": dd,
        "best_day": float(dl.max()),
        "worst_day": float(dl.min()),
        "days": len(dl),
        "best_day_share": float(dl.max() / p.sum()) if p.sum() > 0 else float("nan"),
    }


def main() -> None:
    best = json.loads(Path("data/presets.json").read_text(encoding="utf-8"))["presets"]["BEST"]
    bars = sorted(candle_store.load("MNQ", 1), key=lambda c: c.timestamp)

    LOG(f"BEST @ {SIZE} 口 MNQ — SL/TP 上下限的代價 (1 tick = ${TICK_VALUE * SIZE:.2f})\n")

    cases = [("原始(無上限)", None, None, "clamp")]
    for risk_d, prof_d in ((1000, None), (None, 1200), (1000, 1200),
                           (800, 1200), (600, 1200), (1000, 900), (800, 900)):
        cases.append((
            f"risk≤${risk_d or '-'} / profit≤${prof_d or '-'}",
            dollars_to_ticks(risk_d) if risk_d else None,
            dollars_to_ticks(prof_d) if prof_d else None,
            "clamp"))
    cases.append((f"risk≤$1000 BLOCK 模式", dollars_to_ticks(1000), None, "block"))
    cases.append((f"risk≤$1000 profit≤$1200 BLOCK", dollars_to_ticks(1000),
                  dollars_to_ticks(1200), "block"))

    rows = []
    hdr = (f"{'設定':<30}{'筆數':>5}{'PnL':>9}{'PF':>7}{'勝率':>7}"
           f"{'maxDD':>8}{'最佳日':>9}{'最差日':>9}{'最佳日佔比':>11}")
    LOG(hdr)
    LOG("-" * len(hdr))
    for name, mr, mp, mode in cases:
        p = dict(best)
        if mr:
            p["max_risk_ticks"] = mr
        if mp:
            p["max_profit_ticks"] = mp
        p["risk_cap_mode"] = mode
        r = run_variant(p, bars, "MNQ")
        s = stats(r["trades"])
        rows.append({"case": name, "max_risk_ticks": mr,
                     "max_profit_ticks": mp, "mode": mode, **s})
        if not s.get("n"):
            LOG(f"{name:<30}{'(無交易)':>5}")
            continue
        LOG(f"{name:<30}{s['n']:>5}{s['pnl']:>+9.0f}{s['pf']:>7.2f}"
            f"{100 * s['win']:>6.1f}%{s['max_dd']:>8.0f}"
            f"{s['best_day']:>+9.0f}{s['worst_day']:>+9.0f}"
            f"{100 * s['best_day_share']:>10.1f}%")

    out = Path("data/research/clamp_cap_study.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=1, default=str), encoding="utf-8")
    LOG(f"\nreport: {out}")

    base = rows[0]
    LOG("\n對照基準(原始):")
    LOG(f"  PnL ${base['pnl']:+.0f}  PF {base['pf']:.2f}  勝率 {100*base['win']:.1f}%  "
        f"最佳日 ${base['best_day']:+.0f} (佔總獲利 {100*base['best_day_share']:.1f}%)")


if __name__ == "__main__":
    main()
