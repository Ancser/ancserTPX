"""1.0.10: G5 跨商品交集 —— 同一組參數必須在 MNQ 與 MES 上**同時**過閘。

前置:
    python scripts/stability_sweep_2026.py --symbol MNQ
    python scripts/stability_sweep_2026.py --symbol MES

按 1.0.9 的紀錄,G5 是真正的瓶頸(716 變體雙邊全過 = 0)。這次兩個商品
的測試期間終於一致(都是 2026-01 → 08),之前 MES 只有 5/27 之後的資料,
跨商品比較根本不成立。

⚠️ 已知資料瑕疵(登記在 meta 的 known_seams,依使用者決定不修改):
    MNQ 2026-06-11T00:00Z  假跳空 268.50 點
    MES 2026-06-15T00:00Z  假跳空  63.00 點
兩者都落在 S3 區段內,所以 **S3 的數字要打折看**。S1/S2 不受影響。

用法:  python scripts/g5_cross_symbol_2026.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RES = ROOT / "data" / "research"


def load(sym):
    p = RES / f"stability_sweep_2026_{sym}.json"
    if not p.exists():
        print(f"✘ 缺少 {p.name} —— 先跑 stability_sweep_2026.py --symbol {sym}",
              file=sys.stderr)
        sys.exit(1)
    d = json.load(open(p, encoding="utf-8"))
    return {r["tag"]: r for r in d["all"]}, {r["tag"] for r in d["winners"]}


def dedup(tag):
    for a in ("/max1", "/max2", "/max3", "/1dir", "/free"):
        tag = tag.replace(a, "")
    return tag


def main():
    mnq_all, mnq_win = load("MNQ")
    mes_all, mes_win = load("MES")
    common = set(mnq_all) & set(mes_all)
    print(f"MNQ 過閘 {len(mnq_win)}  |  MES 過閘 {len(mes_win)}  |  共同變體 {len(common)}")

    both = sorted(mnq_win & mes_win,
                  key=lambda t: -min(mnq_all[t]["worst_seg_pf"],
                                     mes_all[t]["worst_seg_pf"]))
    seen, uniq = set(), []
    for t in both:
        k = dedup(t)
        if k not in seen:
            seen.add(k)
            uniq.append(t)

    print(f"\n{'='*96}")
    print(f"G5 雙邊同時過閘: {len(both)} 筆 → 去重 {len(uniq)} 個相異變體")
    print(f"{'='*96}")
    if not uniq:
        print("\n沒有任何變體能在兩個商品上同時過閘。")
        print("這與 1.0.9 的結論一致(716 變體雙邊全過 = 0)。")
    else:
        print(f"\n{'變體':<46}{'—— MNQ ——':>26}{'—— MES ——':>26}")
        print(f"{'':<46}{'n':>5}{'PnL':>9}{'最差段':>7}{'月':>5}"
              f"{'n':>5}{'PnL':>9}{'最差段':>7}{'月':>5}")
        print("-" * 98)
        for t in uniq:
            a, b = mnq_all[t], mes_all[t]
            print(f"{dedup(t)[:44]:<46}"
                  f"{a['n']:>5}{a['pnl']:>9,.0f}{a['worst_seg_pf']:>7.2f}"
                  f"{a['months_profitable']:>3}/{a['months_traded']}"
                  f"{b['n']:>5}{b['pnl']:>9,.0f}{b['worst_seg_pf']:>7.2f}"
                  f"{b['months_profitable']:>3}/{b['months_traded']}")

    # ── MNQ 的贏家在 MES 上表現如何(即使沒過閘) ──────────────
    print(f"\n{'='*96}")
    print("MNQ 前 12 名在 MES 上的表現(檢查是否為 MNQ 專屬的過擬合)")
    print(f"{'='*96}")
    top = sorted(mnq_win, key=lambda t: -mnq_all[t]["worst_seg_pf"])
    seen2, shown = set(), 0
    print(f"{'變體':<46}{'MNQ最差段':>11}{'MES最差段':>11}{'MES PnL':>10}"
          f"{'MES PF':>9}  MES過閘")
    print("-" * 92)
    for t in top:
        k = dedup(t)
        if k in seen2:
            continue
        seen2.add(k)
        a, b = mnq_all[t], mes_all.get(t)
        if b is None:
            continue
        print(f"{k[:44]:<46}{a['worst_seg_pf']:>11.2f}{b['worst_seg_pf']:>11.2f}"
              f"{b['pnl']:>10,.0f}{(b['pf'] or 0):>9.2f}"
              f"     {'✔' if t in mes_win else '✘'}")
        shown += 1
        if shown >= 12:
            break

    # ── 各族在 MES 上的通過率 ────────────────────────────────
    from collections import defaultdict
    fa, fw = defaultdict(int), defaultdict(int)
    for t in mes_all:
        fa[t.split("/")[0]] += 1
    for t in mes_win:
        fw[t.split("/")[0]] += 1
    print(f"\nMES 各族通過率: " +
          "  ".join(f"{f}={fw.get(f,0)}/{fa[f]}" for f in sorted(fa)))

    out = RES / "g5_cross_symbol_2026.json"
    json.dump({"both_pass": [dedup(t) for t in uniq],
               "detail": [{"tag": dedup(t), "MNQ": mnq_all[t], "MES": mes_all[t]}
                          for t in uniq]},
              open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n寫入 {out}")


if __name__ == "__main__":
    main()
