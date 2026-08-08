"""1.0.10: 把策略的逐年表現對上市場體制,回答「為什麼某些年不行」。

BEST 在 2020–2026 的逐年 PnL 是 +3341 / -548 / -1151 / +706 / -11 / +3540 / +5077,
4 賺 3 賠。本腳本量測每年的市場特徵,看哪一個維度能分開賺錢年與虧錢年。

量的維度(全部用日線導出,與策略無關,避免循環論證):
  trend      年報酬
  chop       日內反轉度 = 1 - |收-開| / (高-低),越高越震盪
  vol        日均真實區間 / 價格(%)
  vol_of_vol 波動率本身的變異(體制是否穩定)
  up_days    上漲日比例(long_only 的直接環境指標)
  gap        隔夜跳空佔日幅的比例

用法:  python scripts/regime_attribution.py [--symbol MNQ]
"""
from __future__ import annotations

import argparse
import statistics as st
import sys
from collections import defaultdict
from datetime import timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend.data import candle_store  # noqa: E402


def _utc(t):
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t


def trade_date(ts):
    """Topstep 交易日:17:00 CT 換日(夏令 22:00 UTC)。"""
    d = _utc(ts)
    return (d + timedelta(days=1)).date() if d.hour >= 22 else d.date()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="MNQ")
    a = ap.parse_args()

    bars = sorted(candle_store.load(a.symbol, 1), key=lambda c: c.timestamp)
    days = defaultdict(list)
    for b in bars:
        days[trade_date(b.timestamp)].append(b)

    daily = []
    for d in sorted(days):
        v = days[d]
        if len(v) < 200:          # 半日/假日不納入
            continue
        o, c = v[0].open, v[-1].close
        hi = max(x.high for x in v)
        lo = min(x.low for x in v)
        daily.append({"d": d, "o": o, "c": c, "h": hi, "l": lo})

    print(f"[{a.symbol}] {len(daily):,} 個完整交易日  "
          f"{daily[0]['d']} → {daily[-1]['d']}\n")

    by_year = defaultdict(list)
    for i, x in enumerate(daily):
        x["gap"] = abs(x["o"] - daily[i - 1]["c"]) if i else 0.0
        by_year[x["d"].year].append(x)

    print(f"{'年':<6}{'交易日':>6}{'年報酬':>9}{'上漲日':>8}{'日均幅%':>9}"
          f"{'震盪度':>8}{'波動變異':>9}{'跳空佔比':>9}")
    print("-" * 66)
    rows = {}
    for y in sorted(by_year):
        v = by_year[y]
        if len(v) < 60:
            continue
        ret = (v[-1]["c"] / v[0]["o"] - 1) * 100
        up = sum(1 for x in v if x["c"] > x["o"]) / len(v) * 100
        rngs = [(x["h"] - x["l"]) / x["o"] * 100 for x in v]
        vol = st.median(rngs)
        vov = st.pstdev(rngs) / max(vol, 1e-9)
        chop = st.median([1 - abs(x["c"] - x["o"]) / max(x["h"] - x["l"], 1e-9) for x in v])
        gapr = st.median([x["gap"] / max(x["h"] - x["l"], 1e-9) for x in v[1:]])
        rows[y] = dict(ret=ret, up=up, vol=vol, vov=vov, chop=chop, gap=gapr)
        print(f"{y:<6}{len(v):>6}{ret:>8.1f}%{up:>7.1f}%{vol:>9.2f}"
              f"{chop:>8.3f}{vov:>9.3f}{gapr:>9.3f}")

    # BEST 逐年(來自 6 年 sweep 的實測值)
    best = {2020: 3341.3, 2021: -548.2, 2022: -1151.3, 2023: 706.3,
            2024: -11.3, 2025: 3539.6, 2026: 5076.5}
    common = [y for y in rows if y in best]
    if len(common) >= 5:
        print(f"\n{'='*66}\nBEST 逐年 PnL 與各維度的相關性(n={len(common)} 年)")
        print("-" * 66)
        pnl = [best[y] for y in common]
        mu_p = sum(pnl) / len(pnl)
        for k, label in [("ret", "年報酬"), ("up", "上漲日比例"), ("vol", "日均幅%"),
                         ("chop", "震盪度"), ("vov", "波動變異"), ("gap", "跳空佔比")]:
            xs = [rows[y][k] for y in common]
            mu_x = sum(xs) / len(xs)
            cov = sum((x - mu_x) * (p - mu_p) for x, p in zip(xs, pnl))
            den = (sum((x - mu_x) ** 2 for x in xs) * sum((p - mu_p) ** 2 for p in pnl)) ** 0.5
            r = cov / den if den else 0.0
            bar = ("+" if r > 0 else "-") * int(abs(r) * 20)
            print(f"  {label:<12} r = {r:+.3f}  {bar}")
        print("\n  賺錢年 vs 虧錢年的維度中位數:")
        win = [y for y in common if best[y] > 500]
        los = [y for y in common if best[y] < 0]
        print(f"    賺 {win}   賠 {los}")
        for k, label in [("ret", "年報酬"), ("up", "上漲日%"), ("vol", "日均幅%"),
                         ("chop", "震盪度"), ("vov", "波動變異")]:
            w = st.median([rows[y][k] for y in win]) if win else 0
            l = st.median([rows[y][k] for y in los]) if los else 0
            print(f"    {label:<10} 賺={w:>8.3f}   賠={l:>8.3f}   差={w-l:+.3f}")


if __name__ == "__main__":
    main()
