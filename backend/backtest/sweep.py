# ============================================================
# 文件: backend/backtest/sweep.py
# 狀態: 1.0.8 新增 (高效 5m trend 參數掃描 — 0.15.0 sweep 回歸版)
# 原理: BacktestEngine 的 zone_timeline 快路徑 + ClockBucket 已完成 zone
#       凍結不變 → detector 每個 VA 只跑一次(~45s),之後每個參數變體
#       只跑引擎迴圈(~2s)。144 變體全程 ~7 分鐘(逐一重跑要 ~6 小時)。
# 關聯: → backend/api/routes.py (/backtest/sweep 端點、結果持久化)
#       → scripts/session_ladder_sweep.py / sweet_preset_sweep.py (研究版原型)
# ============================================================
"""高效 trend 參數掃描:zone timeline 建一次,全 grid 共用。"""

from __future__ import annotations

import copy
import logging
from collections import defaultdict
from typing import Callable, List, Optional

from backend.backtest.engine import BacktestEngine, _topstep_trade_date
from backend.db.models import (
    BacktestConfig, Candle, StrategyParams,
    _extract_symbol, get_commission_rt, get_fees_rt,
)
from backend.strategy.consolidation import build_zone_detector

logger = logging.getLogger(__name__)

# 預設 grid(與 1.0.8 研究掃描一致):
SWEEP_VA = (0.70, 0.80)
SWEEP_EXITS = (("tp", 2), ("tp", 3), ("tp", 4), ("tp", 5), ("tp", 6), ("ladder", 4))
SWEEP_CONFIRM = (2, 3, 4, 5)
SWEEP_STOP = (0, 3, 4)


def build_trend_zone_timeline(
    candles: List[Candle],
    area_timeframe: str = "5m",
    value_area_pct: float = 0.80,
    tick_size: float = 0.25,
    max_recent: int = 10,
) -> List[dict]:
    """單 TF trend 用 zone timeline:每根 K 的 (recent zones, mature) 快照。

    ClockBucket 已完成 zone 凍結不變 → 直接共享引用零拷貝;
    參考列表只在 bucket 完成時變化。candles 必須已按時間排序
    (timeline 模式引擎跳過內部排序,索引需對齊)。
    """
    det = build_zone_detector(
        area_timeframe=area_timeframe, value_area_pct=value_area_pct,
        tick_size=tick_size, max_recent=max_recent,
    )
    tl: List[dict] = []
    last_n = -1
    cur: List = []
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


def _run_one(params: StrategyParams, candles: List[Candle], timeline: List[dict]) -> dict:
    cid = params.contract_id
    config = BacktestConfig(
        strategies=["trend"], initial_capital=50_000.0,
        symbol=_extract_symbol(cid), commission_rt=get_commission_rt(cid),
        fees_rt=get_fees_rt(cid),
        value_area_pct=float(getattr(params, "value_area_pct", 0.80)),
    )
    result = BacktestEngine(config=config, strategy_params=params,
                            zone_timeline=timeline, record_equity=False).run(candles)
    m = result.metrics
    day = defaultdict(float)
    gain = loss = 0.0
    for t in result.trades:
        p = t.pnl or 0.0
        day[_topstep_trade_date(t.entry_time)] += p
        if p > 0:
            gain += p
        else:
            loss += p
    # monthly_avg = 30.44 天歸一化月率(run-rate);日曆月分組平均會被
    # 部分月(月初/月末只有幾個交易日)嚴重拖低,故不用。
    monthly_rate = 0.0
    seg_pnls = [0.0, 0.0, 0.0]
    if day:
        keys = sorted(day.keys())
        from datetime import date as _date
        d0 = _date.fromisoformat(keys[0])
        d1 = _date.fromisoformat(keys[-1])
        span_days = max(1, (d1 - d0).days + 1)
        monthly_rate = float(m.total_pnl) * 30.44 / span_days
        # 1.0.9 P1: walk-forward 三段(日期跨度三等分)— 各段獨立 pnl
        for dk, v in day.items():
            off = (_date.fromisoformat(dk) - d0).days
            seg = min(2, int(off * 3 / span_days))
            seg_pnls[seg] += v
    return {
        "seg_pnls": [round(x, 1) for x in seg_pnls],
        "wf_pass": bool(all(x > 0 for x in seg_pnls)),   # 1.0.9 P1 接受條件之一
        "trades": int(m.total_trades),
        "win_rate": round(float(m.win_rate), 4),
        "pnl": round(float(m.total_pnl), 1),
        "gain": round(gain, 1),
        "loss": round(loss, 1),
        "pf": round(float(m.profit_factor), 3),
        "max_dd": round(float(m.max_drawdown), 1),
        "expect": round(float(m.expectancy), 2),
        "worst_day": round(min(day.values()) if day else 0.0, 1),
        "monthly_avg": round(monthly_rate, 1),
        "score": round(float(m.total_pnl) / max(float(m.max_drawdown), 100.0), 3),
    }


def run_trend_sweep(
    candles: List[Candle],
    base_params: StrategyParams,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> List[dict]:
    """跑完整 sweep grid。candles 必須已排序。回傳結果列表(未排序)。

    base_params 提供固定項(合約、SL、trail、sessions 等);grid 只動
    VA / 出場模式 / RR / confirm / 斷路器。
    """
    grid = [
        (va, exit_mode, rr, c_bars, stop)
        for va in SWEEP_VA
        for (exit_mode, rr) in SWEEP_EXITS
        for c_bars in SWEEP_CONFIRM
        for stop in SWEEP_STOP
    ]
    total = len(grid) + len(SWEEP_VA)  # +timeline builds
    done = 0
    results: List[dict] = []
    timelines = {}

    for va in SWEEP_VA:
        if progress_cb:
            progress_cb(done, total, f"building zone timeline VA{int(va * 100)}")
        timelines[va] = build_trend_zone_timeline(candles, "5m", va)
        done += 1

    for va, exit_mode, rr, c_bars, stop in grid:
        p = copy.deepcopy(base_params)
        p.strategy = "trend"
        p.value_area_pct = va
        p.area_timeframe = "5m"
        p.method = "single"
        p.tf_combo = []
        p.tr_exit_mode = exit_mode
        p.rr_ratio = int(rr)
        p.breakout_confirm_bars = int(c_bars)
        p.tr_daily_loss_stop = int(stop)
        r = _run_one(p, candles, timelines[va])
        r["params"] = {
            "value_area_pct": va,
            "tr_exit_mode": exit_mode,
            "rr_ratio": int(rr),
            "breakout_confirm_bars": int(c_bars),
            "tr_daily_loss_stop": int(stop),
        }
        r["label"] = (
            f"VA{int(va * 100)} "
            f"{'ladder' if exit_mode == 'ladder' else 'RR' + str(rr)} "
            f"C{c_bars} S{stop}"
        )
        results.append(r)
        done += 1
        if progress_cb and (done % 4 == 0 or done == total):
            progress_cb(done, total, r["label"])

    _annotate_plateau_and_acceptance(results)
    return results


def _annotate_plateau_and_acceptance(results: List[dict]) -> None:
    """1.0.9 P1: 平原測試 + 預註冊接受標準。

    平原 = 鄰近參數(單一維度 ±1 檔)多數仍為正 — 真 edge 是平原,
    尖點是過擬合指紋。接受 = wf 三段各正 + 平原 + 樣本 ≥80 + 期望 >0。
    """
    by_key = {}
    for r in results:
        p = r["params"]
        by_key[(p["value_area_pct"], p["tr_exit_mode"], p["rr_ratio"],
                p["breakout_confirm_bars"], p["tr_daily_loss_stop"])] = r

    va_list = list(SWEEP_VA)
    rr_list = [rr for (mode, rr) in SWEEP_EXITS if mode == "tp"]
    c_list = list(SWEEP_CONFIRM)
    s_list = list(SWEEP_STOP)

    def _step_neighbors(seq, v):
        try:
            i = seq.index(v)
        except ValueError:
            return []
        out = []
        if i > 0:
            out.append(seq[i - 1])
        if i < len(seq) - 1:
            out.append(seq[i + 1])
        return out

    for r in results:
        p = r["params"]
        va, mode, rr = p["value_area_pct"], p["tr_exit_mode"], p["rr_ratio"]
        c, s = p["breakout_confirm_bars"], p["tr_daily_loss_stop"]
        neigh = []
        for va2 in _step_neighbors(va_list, va):
            neigh.append((va2, mode, rr, c, s))
        if mode == "tp":
            for rr2 in _step_neighbors(rr_list, rr):
                neigh.append((va, mode, rr2, c, s))
        for c2 in _step_neighbors(c_list, c):
            neigh.append((va, mode, rr, c2, s))
        for s2 in _step_neighbors(s_list, s):
            neigh.append((va, mode, rr, c, s2))
        vals = [by_key[k]["pnl"] for k in neigh if k in by_key]
        pos = sum(1 for v in vals if v > 0)
        r["plateau_pass"] = bool(vals and pos / len(vals) >= 0.6 and r["pnl"] > 0)
        r["accept"] = bool(
            r.get("wf_pass") and r["plateau_pass"]
            and r["trades"] >= 80 and r["expect"] > 0
        )
