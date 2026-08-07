"""1.0.10: BEST preset 在 2026 年 1–5 月(Databento 補的資料)表現不佳的歸因。

回答兩件事:
  1. 是資料問題還是體制問題?
  2. 若是體制,是哪一個維度(方向/波動/訊號頻率)崩掉的?

先驗證過的事實(見 memory project_candle_store_provenance):
  · 兩來源 volume 在重疊區 5,396/5,400 逐根相同 → 資料本身沒問題
  · 日均根數 1133 vs 1125 → session 覆蓋相同
  · 唯一已知瑕疵是 2026-06-11 的 268.50 點假跳空,已登記且刻意不修

用法:  python scripts/best_regime_diagnosis.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import date, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend.api.routes import BacktestRequest, _build_strategy_params_from_request
from backend.backtest.engine import BacktestEngine, BacktestConfig
from backend.backtest.sweep import _extract_symbol
from backend.data import candle_store
from backend.db.models import Candle

try:
    from backend.backtest.costs import get_commission_rt, get_fees_rt
except ImportError:
    from backend.backtest.sweep import get_commission_rt, get_fees_rt


def _utc(t):
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t


def topstep_trade_date(dt) -> date:
    """Topstep 交易日以 17:00 CT 換日。夏令 = 22:00 UTC。"""
    d = _utc(dt)
    return (d.date() if d.hour < 22 else
            (d + __import__("datetime").timedelta(days=1)).date())


def build_params(preset_name="BEST"):
    cfg = json.load(open(ROOT / "data" / "presets.json", encoding="utf-8"))
    preset = cfg["presets"][preset_name]
    req = BacktestRequest()
    for k, v in preset.items():
        if hasattr(req, k):
            setattr(req, k, v)
    params = _build_strategy_params_from_request(req, 1)
    params.contract_id = preset.get("contract_id") or params.contract_id
    return params, preset


def run(params, candles):
    cid = params.contract_id
    config = BacktestConfig(
        strategies=["trend"], initial_capital=50_000.0,
        symbol=_extract_symbol(cid), commission_rt=get_commission_rt(cid),
        fees_rt=get_fees_rt(cid),
        value_area_pct=float(getattr(params, "value_area_pct", 0.80)),
    )
    return BacktestEngine(config=config, strategy_params=params,
                          record_equity=False).run(candles)


def stats(pnls):
    if not pnls:
        return dict(n=0, pnl=0.0, pf=0.0, wr=0.0, avg=0.0, worst=0.0)
    g = sum(p for p in pnls if p > 0)
    l = -sum(p for p in pnls if p <= 0)
    return dict(
        n=len(pnls), pnl=sum(pnls),
        pf=(g / l) if l > 0 else float("inf"),
        wr=100.0 * sum(1 for p in pnls if p > 0) / len(pnls),
        avg=sum(pnls) / len(pnls), worst=min(pnls),
    )


def main():
    params, preset = build_params("BEST")
    size = int(preset.get("contract_size", 1) or 1)
    print(f"BEST = {params.strategy} / {preset.get('factor_signal_family')}"
          f" / {preset.get('factor_side_mode')} / {preset.get('factor_timeframe_minutes')}m"
          f" / SL {preset.get('factor_sl_rule')} {preset.get('factor_sl_value')}"
          f" / TP {preset.get('factor_tp_rule')} {preset.get('factor_tp_value')}"
          f" / {size} 口")

    bars = sorted(candle_store.load("MNQ", 1), key=lambda c: c.timestamp)
    print(f"資料 {len(bars):,} 根  {_utc(bars[0].timestamp):%Y-%m-%d}"
          f" → {_utc(bars[-1].timestamp):%Y-%m-%d}\n")

    res = run(params, bars)
    trades = res.trades
    print(f"全期 {len(trades)} 筆交易\n")

    # ── 逐月拆解 ────────────────────────────────────────────
    by_month = defaultdict(list)
    src_of_month = {}
    for t in trades:
        k = f"{topstep_trade_date(t.entry_time):%Y-%m}"
        by_month[k].append((t.pnl or 0.0) * size)
    # 每月資料來源
    for b in bars:
        src_of_month.setdefault(f"{_utc(b.timestamp):%Y-%m}", set()).add(
            getattr(b, "source", "topstepx"))

    print(f"{'月':<9}{'筆數':>5}{'淨損益':>11}{'PF':>7}{'勝率':>7}"
          f"{'每筆':>9}{'最差':>10}   來源")
    print("-" * 72)
    for k in sorted(by_month):
        s = stats(by_month[k])
        src = "/".join(sorted(src_of_month.get(k, {"?"})))
        pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
        print(f"{k:<9}{s['n']:>5}{s['pnl']:>11,.0f}{pf:>7}{s['wr']:>6.0f}%"
              f"{s['avg']:>9,.0f}{s['worst']:>10,.0f}   {src}")

    # ── 兩個體制對比 ────────────────────────────────────────
    early = [p for k in sorted(by_month) if k <= "2026-05" for p in by_month[k]]
    late = [p for k in sorted(by_month) if k >= "2026-06" for p in by_month[k]]
    print("\n" + "=" * 72)
    for label, pnls in [("1–5月 (Databento)", early), ("6–8月 (TopstepX)", late)]:
        s = stats(pnls)
        pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
        print(f"{label:<22} n={s['n']:>4}  PnL={s['pnl']:>9,.0f}  PF={pf:>6}"
              f"  勝率={s['wr']:>4.0f}%  每筆={s['avg']:>7,.0f}")

    # ── 訊號頻率:是沒訊號還是訊號錯? ─────────────────────
    print("\n每月交易頻率(判斷是「沒機會」還是「做錯方向」):")
    days_per_month = defaultdict(set)
    for b in bars:
        days_per_month[f"{_utc(b.timestamp):%Y-%m}"].add(_utc(b.timestamp).date())
    for k in sorted(days_per_month):
        n = len(by_month.get(k, []))
        d = len(days_per_month[k])
        print(f"  {k}  {n:>3} 筆 / {d:>2} 天 = {n/d:.2f} 筆/天")


if __name__ == "__main__":
    main()
