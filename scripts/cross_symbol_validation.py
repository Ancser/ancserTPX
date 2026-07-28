"""1.0.9: 跨商品交叉驗證 —— 目前唯一真正的樣本外測試。

為什麼這是關鍵測試:
  所有策略都在同一個 2.5 個月視窗上被選出來,時間上沒有樣本外可用。但
  MNQ 與 MES 追蹤不同指數(NQ / ES),相關約 0.9 卻不完全同步 —— 在一邊
  選出的參數搬到另一邊,是我們手上唯一近似樣本外的檢定。

  真實的結構性邊際應該至少「部分」轉移。若一搬過去就變負,那個參數組
  就是對該商品這段特定路徑的曲線擬合。

實測(scripts/rsi2_robustness.py 之後):
  RSI2 MNQ 冠軍 → MES 得 PF 0.745;MES 冠軍 → MNQ 得 PF 0.853。
  完美對角線,兩個交叉方向都虧 → RSI2 被推翻。

本腳本把這個檢定套用到所有候選(含現役 preset)當作對照組。

用法: python scripts/cross_symbol_validation.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import public_strategy_research as R  # noqa: E402
from best_mes_parity_study import run_variant, series_stats  # noqa: E402
from backend.data import candle_store  # noqa: E402

TICKV = {"MNQ": 0.5, "MES": 1.25}
SLIP = 14.0
LOG = lambda *a: (print(*a), sys.stdout.flush())

# 研究策略候選(來自 public_strategy_research / rsi2_robustness 的通過者)
RESEARCH = [
    ("RSI2  MNQ-win", "RSI2", {"research_rsi_len": 2, "research_rsi_low": 2.0,
                               "research_tf_minutes": 5, "factor_side_mode": "all",
                               "factor_sl_value": 1.5, "rr_ratio": 3}),
    ("RSI2  MES-win", "RSI2", {"research_rsi_len": 2, "research_rsi_low": 10.0,
                               "research_tf_minutes": 15, "factor_side_mode": "long_only",
                               "factor_sl_value": 2.5, "rr_ratio": 3}),
    ("BBREV MES-win", "BBREV", {"research_bb_len": 50, "research_k": 2.5,
                                "research_tf_minutes": 15, "factor_side_mode": "all",
                                "factor_sl_value": 2.5, "rr_ratio": 3}),
    ("DONCHIAN best", "DONCHIAN", {"research_lookback": 20, "research_tf_minutes": 15,
                                   "factor_side_mode": "all",
                                   "factor_sl_value": 2.5, "rr_ratio": 3}),
    ("INTRAMOM best", "INTRAMOM", {"research_first_minutes": 30, "research_entry_hour": 19,
                                   "factor_side_mode": "all",
                                   "factor_sl_value": 2.5, "rr_ratio": 3}),
]

OR15 = {"strategy": "fade", "tf_combo": [], "tr_sl_ticks": 50, "tr_tp_ticks": 200,
        "tr_trail_enabled": True, "tr_trail_trigger_pct": 0.3, "tr_trail_sl_ticks": 10,
        "tr_full_tp_lock": 0, "one_trade_per_session_direction": False,
        "tr_one_trade_per_session": False,
        "tr_allowed_sessions": ["ASIA", "EURO", "PRE", "RTH", "AH"],
        "fade_tp_frac": 1.0, "fade_entry_mode": "or15",
        "pmo_max_hold_bars": 0, "factor_max_hold_bars": 0}


def stat(trades, sym):
    pn = [t["pnl"] for t in trades]
    if not pn:
        return {"n": 0, "pf": 0.0, "edge": None, "pnl": 0.0}
    s = series_stats(pn)
    return {"n": s["n"], "pf": s["pf"], "pnl": s["pnl"],
            "edge": float(np.mean(pn)) / TICKV[sym], "dd": s["max_dd"]}


def main() -> None:
    presets = json.loads(Path("data/presets.json").read_text(encoding="utf-8"))["presets"]
    engine_cands = [
        ("BEST   (preset)", dict(presets["BEST"])),
        ("BETTER (preset)", {**presets["BEST"], "factor_pmo_early_scale": 0.9}),
        ("OR15   (DAY ZONE)", dict(OR15)),
    ]
    bars = {s: sorted(candle_store.load(s, 1), key=lambda c: c.timestamp)
            for s in ("MNQ", "MES")}

    LOG("跨商品交叉驗證 —— 同一組參數在兩個商品上的表現")
    LOG(f"實盤門檻:每筆邊際 > {SLIP:g}t\n")
    LOG(f"{'策略':<20}{'MNQ n':>7}{'MNQ PF':>9}{'MNQ 邊際':>11}"
        f"{'MES n':>7}{'MES PF':>9}{'MES 邊際':>11}   判定")
    LOG("-" * 88)

    rows = []
    for label, name, cfg in RESEARCH:
        r = {}
        for sym in ("MNQ", "MES"):
            R._W.clear(); R._init(sym)
            r[sym] = stat(R._run_job((name, dict(cfg)))["trades"], sym)
        rows.append((label, r))

    for label, p in engine_cands:
        r = {}
        for sym in ("MNQ", "MES"):
            pp = dict(p)
            pp["contract_id"] = f"CON.F.US.{sym}.U26"
            r[sym] = stat(run_variant(pp, bars[sym], sym)["trades"], sym)
        rows.append((label, r))

    for label, r in rows:
        a, b = r["MNQ"], r["MES"]
        ea = a["edge"] if a["edge"] is not None else -999
        eb = b["edge"] if b["edge"] is not None else -999
        both = ea > SLIP and eb > SLIP
        one = (ea > SLIP) != (eb > SLIP)
        verdict = ("✅ 雙商品成立" if both
                   else ("⚠ 僅單邊" if one else "❌ 兩邊都不過"))
        LOG(f"{label:<20}{a['n']:>7}{a['pf']:>9.3f}{ea:>10.1f}t"
            f"{b['n']:>7}{b['pf']:>9.3f}{eb:>10.1f}t   {verdict}")

    out = Path("data/research/cross_symbol_validation.json")
    out.write_text(json.dumps([{"label": l, **r} for l, r in rows],
                              indent=1, default=str), encoding="utf-8")
    LOG(f"\nreport: {out}")


if __name__ == "__main__":
    main()
