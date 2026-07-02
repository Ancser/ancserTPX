"""1.0.8 研究(僅腳本):高效 session × ladder 掃描 — zone timeline 建一次復用。

效率核心:BacktestEngine 本就支持 zone_timeline 快路徑(跳過 detector),
但全庫沒有 trend 用的 builder。ClockBucket 的 get_recent_zones() 只回
「已完成 bucket」(完成即凍結),故逐 K 快照可直接共享引用,零拷貝。
→ detector 只跑 1 次(~2-3 分),之後每個參數變體只跑引擎迴圈(~30-60s),
   24 個變體約 15-25 分鐘(舊法 24×2.5 分 ≈ 60 分)。

掃描(基準 = FABLE 動量書:5m VA70 SL80 ladder):
  A. 全 session:ASIA / EURO / PRE / RTH / AH / ALL × ladder C3
  B. confirm 敏感度:同上 × C2 / C5(ladder)
  C. 對照:各 session 舊 trail 出場(tp 模式 RR4 C3)
  → 回答「ASIA 還是不是最好」+ ladder 下最佳 preset。
  (注:19:45-22:00 UTC 為 flatten 窗,AH 段幾乎不可交易,結果會反映)

Run:  PYTHONIOENCODING=utf-8 python -m scripts.session_ladder_sweep
"""
from __future__ import annotations

import copy
import logging
import time as time_mod

from backend.backtest.engine import BacktestEngine
from backend.data import candle_store
from backend.db.models import (
    BacktestConfig, _extract_symbol, get_commission_rt, get_fees_rt,
)
from backend.strategy.consolidation import build_zone_detector
from backend.terminal_live import (
    BUILTIN_PRESETS, FABLE_702_PRESET_1, _build_strategy_params,
)

INITIAL_CAPITAL = 50_000.0
SESSIONS = [
    ("ASIA", ["ASIA"]),
    ("EURO", ["EURO"]),
    ("PRE", ["PRE"]),
    ("RTH", ["RTH"]),
    ("AH", ["AH"]),
    ("ALL", ["ASIA", "EURO", "PRE", "RTH", "AH"]),
]


def build_trend_zone_timeline(candles, area_tf: str, va: float, tick: float = 0.25):
    """單 TF trend 用 zone timeline:每根 K 的 (recent zones, mature) 快照。

    ClockBucket 已完成 zone 凍結不變 → 直接共享引用;參考列表只在
    bucket 完成時變化,逐 K 重建列表僅於 completed 數變動時發生。
    """
    det = build_zone_detector(
        area_timeframe=area_tf, value_area_pct=va,
        tick_size=tick, max_recent=10,
    )
    tl = []
    last_n = -1
    cur = []
    for c in candles:
        det.update(c)
        n = det.completed_zone_count
        if n != last_n:
            last_n = n
            cur = list(det.get_recent_zones())
        tl.append({
            "active": cur[-1] if cur else None,
            "mature": bool(cur),
            "recent": cur,
        })
    return tl


def _run(params, candles, timeline):
    cid = params.contract_id
    config = BacktestConfig(
        strategies=["trend"], initial_capital=INITIAL_CAPITAL,
        symbol=_extract_symbol(cid), commission_rt=get_commission_rt(cid),
        fees_rt=get_fees_rt(cid),
        value_area_pct=float(getattr(params, "value_area_pct", 0.80)),
    )
    m = BacktestEngine(config=config, strategy_params=params,
                       zone_timeline=timeline, record_equity=False).run(candles).metrics
    return {
        "trades": int(m.total_trades), "win_rate": float(m.win_rate),
        "pnl": float(m.total_pnl), "max_dd": float(m.max_drawdown),
        "pf": float(m.profit_factor), "calmar": float(m.calmar_ratio),
        "expectancy": float(m.expectancy),
    }


def _row(tag, r, secs):
    print(f"{tag:<26} {r['trades']:>6} {100*r['win_rate']:>6.1f}% "
          f"{r['pnl']:>+11.1f} {r['max_dd']:>9.1f} {r['pf']:>6.2f} "
          f"{r['calmar']:>7.2f} {r['expectancy']:>+9.2f}  [{secs:.0f}s]", flush=True)


def main():
    logging.getLogger("backend").setLevel(logging.WARNING)
    candles = candle_store.load("MNQ", 1)
    candles.sort(key=lambda c: c.timestamp)   # 先排序,timeline 與引擎索引對齊
    print(f"candles {len(candles)}  {candles[0].timestamp} -> {candles[-1].timestamp}", flush=True)

    preset = BUILTIN_PRESETS[FABLE_702_PRESET_1]
    cid = preset.get("contract_id")
    base = _build_strategy_params(preset, cid)   # ladder, stop4, VA70(1.0.8 起 VA 正確傳遞)
    base.tr_daily_loss_stop = 0                  # sweep 先關斷路器,看裸體質

    t0 = time_mod.time()
    timeline = build_trend_zone_timeline(candles, "5m", 0.70)
    print(f"zone timeline built: {len(timeline)} bars in {time_mod.time()-t0:.0f}s", flush=True)

    header = (f"{'variant':<26} {'trades':>6} {'win%':>7} {'pnl':>11} "
              f"{'maxDD':>9} {'PF':>6} {'Calmar':>7} {'expect':>9}")

    print("\n== A/B: sessions × ladder × confirm ==", flush=True)
    print(header, flush=True)
    print("-" * len(header), flush=True)
    for c_bars in (3, 2, 5):
        for label, sess in SESSIONS:
            p = copy.deepcopy(base)
            p.breakout_confirm_bars = c_bars
            p.tr_allowed_sessions = list(sess)
            t1 = time_mod.time()
            r = _run(p, candles, timeline)
            _row(f"{label} ladder C{c_bars}", r, time_mod.time() - t1)

    print("\n== C: 對照 — 各 session 舊 trail 出場(tp RR4 C3)==", flush=True)
    print(header, flush=True)
    print("-" * len(header), flush=True)
    for label, sess in SESSIONS:
        p = copy.deepcopy(base)
        p.tr_exit_mode = "tp"
        p.breakout_confirm_bars = 3
        p.tr_allowed_sessions = list(sess)
        t1 = time_mod.time()
        r = _run(p, candles, timeline)
        _row(f"{label} tp RR4 C3", r, time_mod.time() - t1)


if __name__ == "__main__":
    main()
