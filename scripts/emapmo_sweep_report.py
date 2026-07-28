"""1.0.9: emapmo_full_sweep 結果判讀 —— 重點是「平原」而非「尖峰」。

1890 個變體裡挑 PF 最高的那個,幾乎一定是雜訊冠軍(多重檢定偏誤:即使全部
都是隨機的,最大值也會很好看)。真正可用的訊號是**鄰域穩定**:把某一維
(rr / sl_value / pmo_mode)推一格,績效還在不在。

BEST(atr_blend SL2.5 RR3 long_only early)是**事前**選定的,不受這個偏誤影響
—— 它的作用是基準線,用來衡量新冠軍是否真的贏。

用法: python scripts/emapmo_sweep_report.py --symbol MNQ
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

LOG = lambda *a: (print(*a), sys.stdout.flush())
BEST_KEY = ("long_only", "early", "atr_blend", 2.5, 3, "tp", "off")


def key_of(r) -> tuple:
    p = r.get("params") or {}
    return (
        str(p.get("factor_side_mode")),
        str(p.get("factor_pmo_signal_mode")),
        str(p.get("factor_sl_rule")),
        float(p.get("factor_sl_value") or 0),
        int(p.get("rr_ratio") or 0) if p.get("tr_exit_mode") == "tp" else 0,
        str(p.get("tr_exit_mode")),
        str(p.get("factor_session_va_filter")),
    )


def fmt(r) -> str:
    return (f"PF={r.get('pf'):<6} n={r.get('trades'):<4} "
            f"pnl=${r.get('pnl'):<9} dd=${r.get('max_dd'):<8} "
            f"wf={'Y' if r.get('wf_pass') else 'n'} "
            f"mc={'Y' if r.get('mc_pass') else 'n'} "
            f"PF@14t={r.get('pf_at_measured_slip')}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="MNQ")
    ap.add_argument("--min-trades", type=int, default=15)
    args = ap.parse_args()

    path = Path(f"data/research/emapmo_full_sweep_{args.symbol}.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    res = data["results"]
    by_key = {key_of(r): r for r in res}
    LOG(f"[{args.symbol}] {len(res)} variants, pmo_scale={data['pmo_scale']}, "
        f"{data['elapsed_s']}s")

    elig = [r for r in res if r.get("trades", 0) >= args.min_trades]
    ok = [r for r in res if r.get("long_term_ok")]
    LOG(f"  eligible (n>={args.min_trades}): {len(elig)}   long_term_ok: {len(ok)}")

    LOG(f"\n===== top 12 by PF (n>={args.min_trades}) =====")
    LOG("  ⚠ 1890 選 1 的冠軍必然樂觀 —— 看下面的鄰域穩定度再下結論")
    for r in sorted(elig, key=lambda x: -x["pf"])[:12]:
        LOG(f"  {r['label']:<56} {fmt(r)}"
            + ("  LT-OK" if r.get("long_term_ok") else ""))

    if ok:
        LOG("\n===== 通過 MC + WF + maxDD<2k 的變體 =====")
        for r in sorted(ok, key=lambda x: -x["pf"]):
            LOG(f"  {r['label']:<56} {fmt(r)}")

    # ── BEST 基準線與鄰域 ──
    best = by_key.get(BEST_KEY)
    LOG("\n===== BEST(事前選定,無挑選偏誤)=====")
    if not best:
        LOG("  BEST coordinate not found in grid!")
        return
    LOG(f"  {best['label']:<56} {fmt(best)}")

    side, mode, rule, sl, rr, ex, va = BEST_KEY
    LOG("\n===== BEST 鄰域:改一格看塌不塌 =====")

    def show(title, keys):
        LOG(f"  -- {title} --")
        for k in keys:
            r = by_key.get(k)
            if not r:
                continue
            mark = "  <== BEST" if k == BEST_KEY else ""
            LOG(f"    {r['label']:<54} {fmt(r)}{mark}")

    show("RR 1..6 (SL 固定 2.5)",
         [(side, mode, rule, sl, x, "tp", va) for x in (1, 2, 3, 4, 5, 6)])
    show("SL 1..3 (RR 固定 3)",
         [(side, mode, rule, x, rr, "tp", va) for x in (1.0, 1.5, 2.0, 2.5, 3.0)])
    show("PMO 模式",
         [(side, m, rule, sl, rr, "tp", va) for m in ("normal", "early", "both")])
    show("方向",
         [(s, mode, rule, sl, rr, "tp", va) for s in ("all", "long_only", "short_only")])
    show("SL 規則",
         [(side, mode, x, sl, rr, "tp", va) for x in ("atr", "atr_blend")])
    show("VA 過濾 / 階梯出場",
         [(side, mode, rule, sl, rr, "tp", "off"),
          (side, mode, rule, sl, rr, "tp", "outside"),
          (side, mode, rule, sl, 0, "ladder", "off")])

    # 鄰域穩定度分數:8 個直接鄰居中有幾個 PF>1.5
    nb = []
    for x in (2, 4):
        nb.append((side, mode, rule, sl, x, "tp", va))
    for x in (2.0, 3.0):
        nb.append((side, mode, rule, x, rr, "tp", va))
    for m in ("normal", "both"):
        nb.append((side, m, rule, sl, rr, "tp", va))
    nb.append((side, mode, "atr", sl, rr, "tp", va))
    nb.append(("all", mode, rule, sl, rr, "tp", va))
    got = [by_key[k] for k in nb if k in by_key]
    good = [r for r in got if (r.get("pf") or 0) > 1.5]
    LOG(f"\n  鄰域穩定度: {len(good)}/{len(got)} 個直接鄰居 PF>1.5")
    if got:
        pfs = sorted((r.get("pf") or 0) for r in got)
        LOG(f"  鄰居 PF 中位數 {pfs[len(pfs)//2]:.2f}  最低 {pfs[0]:.2f}  最高 {pfs[-1]:.2f}")


if __name__ == "__main__":
    main()
