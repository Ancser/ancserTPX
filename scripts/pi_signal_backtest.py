"""1.0.10: π 訊號的方向與賠付研究 —— 訊號當下市價進場,測未來報酬。

使用者說「紫圈代表多頭止盈,藍圈代表空頭止盈」。但「止盈」有兩種讀法,
方向完全相反,不能用猜的:

  A 順勢:紫圈 = 看多 → 做多
  B 逆勢:紫圈 = 多頭已到停利區(動能耗盡)→ 做空

本腳本不預設方向,只量**訊號當下進場後、未來 N 分鐘的報酬分布**。
報酬為正 = 順勢有效;為負 = 逆勢有效;接近零 = 該標記沒有方向資訊。

對應:QQQ → MNQ,SPY → MES(使用者指定)。進場價取訊號時間戳當根 1m 的收盤
(市價進場的保守近似;訊號發出到人下單一定有延遲,所以不用當根開盤)。

用法:  python scripts/pi_signal_backtest.py
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend.data import candle_store  # noqa: E402

SYMBOL_MAP = {"QQQ": "MNQ", "SPY": "MES"}
POINT_VALUE = {"MNQ": 2.0, "MES": 5.0}      # $ / index point

# 使用者確認的方向:**藍色系看漲、紫色系看跌**。
#   青π 是青色(藍系)、粉π 是粉色(紫系)。
# 「空頭止盈」= 空單在此停利,代表跌勢到位 → 之後漲,所以藍=漲。
DIRECTION = {
    "淡蓝圈": +1, "深蓝圈": +1, "青π": +1,     # 做多
    "紫圈": -1, "粉π": -1,                     # 做空
}


def _utc(t):
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t


def load_index(sym):
    bars = sorted(candle_store.load(sym, 1), key=lambda c: c.timestamp)
    return [(_utc(b.timestamp), b) for b in bars]


def at_or_after(idx, ts):
    """訊號時間戳當根(或之後第一根)的 1m bar。"""
    lo, hi = 0, len(idx)
    while lo < hi:
        mid = (lo + hi) // 2
        if idx[mid][0] < ts:
            lo = mid + 1
        else:
            hi = mid
    return lo if lo < len(idx) else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizons", default="15,30,60,120,240")
    a = ap.parse_args()

    rows = json.load(open(ROOT / "data" / "research" / "pi_signals.json",
                          encoding="utf-8"))
    hs = [int(x) for x in a.horizons.split(",")]
    idx = {s: load_index(s) for s in ("MNQ", "MES")}
    for s in idx:
        print(f"  {s}: {len(idx[s]):,} 根  "
              f"{idx[s][0][0]:%Y-%m-%d} → {idx[s][-1][0]:%Y-%m-%d}")

    # 每個訊號 → 每個標記各記一筆(一則訊息可能有多個標記)
    recs = []
    miss = 0
    for r in rows:
        if not r.get("symbol"):
            continue
        fut = SYMBOL_MAP[r["symbol"]]
        ts = _utc(datetime.fromisoformat(r["ts"].replace("Z", "+00:00")))
        i = at_or_after(idx[fut], ts)
        if i is None or (idx[fut][i][0] - ts) > timedelta(minutes=10):
            miss += 1
            continue
        entry = idx[fut][i][1].close
        fwd = {}
        for h in hs:
            j = i + h
            fwd[h] = (idx[fut][j][1].close - entry) if j < len(idx[fut]) else None
        for mk in r["marks"]:
            recs.append({"fut": fut, "kind": mk["kind"], "size": mk["size"],
                         "pos": mk["pos"], "fwd": fwd, "ts": ts})

    print(f"\n訊號 {len(rows)} 則 → 標記 {len(recs)} 個(無對應K棒 {miss} 則)\n")

    def summarize(group, label, directional=True):
        """directional=True → 依 DIRECTION 把空單報酬取負,數字即「這筆賺多少」。"""
        print(f"{label:<26}{'向':>3}{'n':>5}" +
              "".join(f"{f'+{h}m':>16}" for h in hs))
        print("-" * (34 + 16 * len(hs)))
        for key in sorted(group, key=lambda k: -len(group[k])):
            rs = group[key]
            d = DIRECTION.get(rs[0]["kind"], 0) if directional else 1
            row = f"{str(key)[:24]:<26}{('多' if d>0 else '空' if d<0 else '?'):>3}{len(rs):>5}"
            for h in hs:
                vals = [r["fwd"][h] * (d or 1) for r in rs if r["fwd"].get(h) is not None]
                if len(vals) < 5:
                    row += f"{'—':>16}"
                    continue
                med = st.median(vals)
                win = sum(1 for v in vals if v > 0) / len(vals) * 100
                row += f"{med:>+8.1f}pt {win:>3.0f}%"
            print(row)
        print()

    # ── 對照組:同期同時段的隨機進場 ─────────────────────────
    # 沒有這個基準線,上面的數字全部無法解讀 —— 每種標記在長尺度都是正的,
    # 那很可能只是這兩個月大盤本身的漂移。隨機取樣限制在**訊號實際出現的
    # 時間範圍與時段分布**內,否則會拿夜盤去比日盤。
    import random
    random.seed(42)
    base = defaultdict(list)
    sig_ts = [r["ts"] for r in recs]
    lo_ts, hi_ts = min(sig_ts), max(sig_ts)
    hours = [t.hour for t in sig_ts]
    for fut in ("MNQ", "MES"):
        pool = [i for i, (t, _) in enumerate(idx[fut])
                if lo_ts <= t <= hi_ts and t.hour in set(hours)]
        if not pool:
            continue
        for _ in range(3000):
            i = random.choice(pool)
            entry = idx[fut][i][1].close
            for h in hs:
                j = i + h
                if j < len(idx[fut]):
                    base[(fut, h)].append(idx[fut][j][1].close - entry)

    print("=== 對照組:同期同時段隨機進場(3000 次/商品)===")
    print(f"{'商品':<26}{'n':>5}" + "".join(f"{f'+{h}m':>16}" for h in hs))
    print("-" * (31 + 16 * len(hs)))
    for fut in ("MNQ", "MES"):
        row = f"{fut:<26}{len(base[(fut, hs[0])]):>5}"
        for h in hs:
            v = base[(fut, h)]
            if len(v) < 5:
                row += f"{'—':>16}"
                continue
            row += f"{st.median(v):>+8.1f}pt {sum(1 for x in v if x>0)/len(v)*100:>3.0f}%"
        print(row)
    print()

    by_kind = defaultdict(list)
    for r in recs:
        by_kind[r["kind"]].append(r)
    summarize(by_kind, "依標記種類")

    by_ks = defaultdict(list)
    for r in recs:
        by_ks[f"{r['kind']}/{r['size']}"].append(r)
    summarize(by_ks, "依 標記×尺寸")

    by_kp = defaultdict(list)
    for r in recs:
        if r["pos"]:
            by_kp[f"{r['kind']}/{r['pos']}"].append(r)
    summarize(by_kp, "依 標記×位置")

    # ── 方向化總計:把每個標記當一筆交易,依 DIRECTION 決定多空 ──
    print("=== 依方向分組(藍系做多 / 紫系做空)===")
    print(f"{'方向':<26}{'n':>5}" + "".join(f"{f'+{h}m':>16}" for h in hs))
    print("-" * (31 + 16 * len(hs)))
    for lbl, sign in (("藍系(做多)", +1), ("紫系(做空)", -1)):
        rs = [r for r in recs if DIRECTION.get(r["kind"], 0) == sign]
        row = f"{lbl:<26}{len(rs):>5}"
        for h in hs:
            v = [r["fwd"][h] * sign for r in rs if r["fwd"].get(h) is not None]
            if len(v) < 5:
                row += f"{'—':>16}"
                continue
            row += f"{st.median(v):>+8.1f}pt {sum(1 for x in v if x>0)/len(v)*100:>3.0f}%"
        print(row)

    print("\n讀法:數字已依方向轉換 —— 正 = 這筆賺錢,不論多空。")
    print("      勝率 50% = 沒有邊際;對照組(隨機進場)約 50%。")
    print("      1 點 = MNQ $2 / MES $5。")


if __name__ == "__main__":
    main()
