"""1.0.9: 模型死因排序 —— 哪些模型是徹底沒用的,以及「為什麼」。

判定不是看 PF 高低,而是看每個模型死在哪一關。關卡由淺到深:

  G0 有訊號      trades >= 15
  G1 帳面獲利    pf > 1.0
  G2 撐得住滑價  每筆淨邊際 > 實測 14t 往返滑價(市價進場的硬成本)
  G3 走查        三段日期各自獲利
  G4 蒙地卡羅    P(虧損)<=5% 且 maxDD P95 < $2k 且 PF P5 > 1.0

最關鍵的是 **G2**:把每筆平均淨損益換算成 ticks,直接跟實測的 14 ticks 比。
邊際小於滑價的模型,無論回測 PF 多漂亮,實盤都是負的 —— 這是「高頻失敗」與
「實盤承受不住 slip」的量化版本,不需要猜。

另外針對 TREND 做專門診斷(SL 太小? 突破太少? 被盤整殺死?):
  - 交易頻率(次/日)—— 分辨「突破太少」還是「訊號太多」
  - 實際勝率 vs 該 RR 的損益兩平勝率 —— 分辨「SL 太小(被雜訊掃掉)」
  - 多空對稱性 —— 統計是否平衡
  - 滑價半衰點 —— 幾 tick 的滑價會把 PF 打到 1.0

用法: python scripts/model_death_cause.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TICK = 0.25
POINT_VALUE = {"MNQ": 2.0, "MES": 5.0}
MEASURED_SLIP_TICKS = 14.0
MIN_TRADES = 15
LOG = lambda *a: (print(*a), sys.stdout.flush())


def tick_value(symbol: str) -> float:
    return TICK * POINT_VALUE.get(symbol, 2.0)


def edge_ticks(r) -> float | None:
    """每筆平均淨損益,換算成 ticks(size=1 口徑)。"""
    n = r.get("trades") or 0
    if not n:
        return None
    return (float(r.get("pnl") or 0.0) / n) / tick_value(r.get("symbol", "MNQ"))


def slip_halflife(r) -> float | None:
    """PF 掉到 1.0 所需的往返滑價 ticks(線性內插 slip_table)。"""
    tbl = r.get("slip_table") or {}
    pts = sorted((float(k), float(v.get("pf") or 0)) for k, v in tbl.items())
    if not pts:
        return None
    base = float(r.get("pf") or 0)
    prev_t, prev_pf = 0.0, base
    for t, pf in pts:
        if pf <= 1.0:
            if prev_pf == pf:
                return t
            return prev_t + (prev_pf - 1.0) / (prev_pf - pf) * (t - prev_t)
        prev_t, prev_pf = t, pf
    return None  # 撐過最大測試等級


def gate_of(r) -> str:
    if (r.get("trades") or 0) < MIN_TRADES:
        return "G0 訊號不足"
    if float(r.get("pf") or 0) <= 1.0:
        return "G1 帳面就虧"
    e = edge_ticks(r)
    if e is None or e <= MEASURED_SLIP_TICKS:
        return "G2 邊際小於滑價"
    if not r.get("wf_pass"):
        return "G3 走查失敗"
    if not r.get("mc_pass"):
        return "G4 蒙地卡羅失敗"
    return "PASS"


GATE_ORDER = ["G0 訊號不足", "G1 帳面就虧", "G2 邊際小於滑價",
              "G3 走查失敗", "G4 蒙地卡羅失敗", "PASS"]


def main() -> None:
    data = json.loads(Path("data/research/robustness_sweep_latest.json")
                      .read_text(encoding="utf-8"))
    res = data["results"]
    LOG(f"樣本: {len(res)} 個變體 "
        f"(MNQ {sum(1 for r in res if r['symbol']=='MNQ')} / "
        f"MES {sum(1 for r in res if r['symbol']=='MES')})")
    LOG(f"實測往返滑價 = {MEASURED_SLIP_TICKS:g} ticks "
        f"(MNQ ${MEASURED_SLIP_TICKS*tick_value('MNQ'):.2f} / "
        f"MES ${MEASURED_SLIP_TICKS*tick_value('MES'):.2f} 每口)\n")

    # ── 死因矩陣 ──
    LOG("=" * 74)
    LOG("死因矩陣:每個模型的變體卡在哪一關")
    LOG("=" * 74)
    tally = defaultdict(lambda: defaultdict(int))
    for r in res:
        tally[(r["symbol"], r["model"])][gate_of(r)] += 1

    hdr = f"{'商品/模型':<22}" + "".join(f"{g:>16}" for g in GATE_ORDER)
    LOG(hdr)
    for key in sorted(tally):
        row = tally[key]
        tot = sum(row.values())
        line = f"{key[0] + ' ' + key[1]:<22}"
        for g in GATE_ORDER:
            c = row.get(g, 0)
            line += f"{(str(c) + f' ({100*c/tot:.0f}%)' if c else '-'):>16}"
        LOG(line)

    # ── 每模型最佳邊際 ──
    LOG("\n" + "=" * 74)
    LOG("每筆邊際 vs 滑價門檻 —— 這是實盤能不能活的硬指標")
    LOG("=" * 74)
    LOG(f"{'商品/模型':<22}{'最佳變體邊際':>14}{'中位邊際':>12}"
        f"{'>14t 的變體':>14}{'最佳PF':>10}")
    for key in sorted(tally):
        sym, model = key
        rows = [r for r in res
                if r["symbol"] == sym and r["model"] == model
                and (r.get("trades") or 0) >= MIN_TRADES]
        if not rows:
            LOG(f"{sym + ' ' + model:<22}{'(無合格樣本)':>14}")
            continue
        es = [edge_ticks(r) for r in rows]
        es = [e for e in es if e is not None]
        over = sum(1 for e in es if e > MEASURED_SLIP_TICKS)
        LOG(f"{sym + ' ' + model:<22}{max(es):>13.1f}t{np.median(es):>11.1f}t"
            f"{f'{over}/{len(es)}':>14}"
            f"{max(float(r.get('pf') or 0) for r in rows):>10.2f}")

    # ── TREND 專門診斷 ──
    LOG("\n" + "=" * 74)
    LOG("TREND 專門診斷:到底死在哪")
    LOG("=" * 74)
    for sym in ("MNQ", "MES"):
        rows = [r for r in res if r["symbol"] == sym and r["model"] == "TREND"
                and (r.get("trades") or 0) >= MIN_TRADES]
        if not rows:
            continue
        best = max(rows, key=lambda r: float(r.get("pf") or 0))
        tpm = [float(r.get("trades_per_month") or 0) for r in rows]
        wr = [float(r.get("win_rate") or 0) for r in rows]
        LOG(f"\n[{sym}] {len(rows)} 個 TREND 變體")
        LOG(f"  交易頻率     中位 {np.median(tpm):.0f} 次/月 "
            f"(≈{np.median(tpm)/21:.1f} 次/日)  範圍 {min(tpm):.0f}–{max(tpm):.0f}")
        LOG(f"  勝率         中位 {100*np.median(wr):.1f}%  "
            f"範圍 {100*min(wr):.1f}–{100*max(wr):.1f}%")
        es = [e for e in (edge_ticks(r) for r in rows) if e is not None]
        LOG(f"  每筆邊際     中位 {np.median(es):+.1f}t  最佳 {max(es):+.1f}t"
            f"   (需 > {MEASURED_SLIP_TICKS:g}t 才能市價進場)")
        hl = [h for h in (slip_halflife(r) for r in rows) if h is not None]
        if hl:
            LOG(f"  滑價半衰點   中位 {np.median(hl):.1f}t  最佳 {max(hl):.1f}t"
                f"   (PF 掉到 1.0 所需的滑價)")
        # 多空對稱
        ln = [float(r.get("long_win") or 0) for r in rows]
        sn = [float(r.get("short_win") or 0) for r in rows]
        LOG(f"  多空勝率     多 {100*np.median(ln):.1f}%  空 {100*np.median(sn):.1f}%"
            f"   (差 {100*abs(np.median(ln)-np.median(sn)):.1f}pp)")
        # 走查一致性
        wfp = sum(1 for r in rows if r.get("wf_pass"))
        LOG(f"  走查通過     {wfp}/{len(rows)} ({100*wfp/len(rows):.0f}%)")
        LOG(f"  最佳變體     {best['label']}  PF={best.get('pf')} "
            f"n={best.get('trades')} 勝率={100*float(best.get('win_rate') or 0):.1f}% "
            f"邊際={edge_ticks(best):+.1f}t")
        st = best.get("slip_table") or {}
        LOG("               滑價後 PF: "
            + "  ".join(f"{k}t→{v['pf']}" for k, v in sorted(st.items(), key=lambda x: int(x[0]))))

        # 損益兩平勝率 vs 實際勝率(用 RR 推)
        LOG("  ── SL 太小? 用損益兩平勝率檢驗 ──")
        for r in sorted(rows, key=lambda x: -float(x.get("pf") or 0))[:4]:
            p = r.get("params") or {}
            rr = float(p.get("rr_ratio") or 0)
            if rr <= 0:
                continue
            need = 1.0 / (1.0 + rr)
            act = float(r.get("win_rate") or 0)
            LOG(f"    {r['label']:<26} RR={rr:g} 需勝率>{100*need:.1f}% "
                f"實際{100*act:.1f}%  {'✓' if act > need else '✗'} "
                f"(差 {100*(act-need):+.1f}pp)")

    # ── 總結 ──
    LOG("\n" + "=" * 74)
    LOG("結論")
    LOG("=" * 74)
    for key in sorted(tally):
        rows = [r for r in res if r["symbol"] == key[0] and r["model"] == key[1]]
        passed = [r for r in rows if gate_of(r) == "PASS"]
        alive = [r for r in rows if (r.get("trades") or 0) >= MIN_TRADES
                 and (edge_ticks(r) or -9) > MEASURED_SLIP_TICKS]
        verdict = ("可用" if passed else
                   ("邊際夠但穩健性不足" if alive else "徹底沒用"))
        LOG(f"  {key[0]} {key[1]:<14} → {verdict}"
            f"  (通過 {len(passed)}, 邊際過關 {len(alive)}, 總數 {len(rows)})")


if __name__ == "__main__":
    main()
