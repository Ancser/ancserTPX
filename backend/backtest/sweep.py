# ============================================================
# 文件: backend/backtest/sweep.py
# 狀態: 1.0.8 新增 (高效 15m trend 參數掃描 — 0.15.0 sweep 回歸版)
# 原理: BacktestEngine 的 zone_timeline 快路徑 + ClockBucket 已完成 zone
#       凍結不變 → detector 每個 VA 只跑一次(~45s),之後每個參數變體
#       只跑引擎迴圈(~2s)。144 變體全程 ~7 分鐘(逐一重跑要 ~6 小時)。
# 關聯: → backend/api/routes.py (/backtest/sweep 端點、結果持久化)
#       → scripts/session_ladder_sweep.py / sweet_preset_sweep.py (研究版原型)
# ============================================================
"""高效 multi-model 參數掃描:trend zone timeline 建一次,其他模型直接共用引擎。"""

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
DAY_ZONE_ENTRY = ("limit", "rejection")
DAY_ZONE_SL = (80, 120, 160)
DAY_ZONE_TP_FRAC = (0.50, 0.75, 1.00)
DAY_ZONE_STOP = (0, 1, 2)
DISTRIBUTION_WINDOWS = (15, 30, 60)
DISTRIBUTION_METHODS = ("std", "mad")
DISTRIBUTION_ENTRY = ("blind", "reject")
DISTRIBUTION_ACCEPT = ("none", "filter")
DISTRIBUTION_STOP_SPAN = (0.75, 1.0, 1.5)
DISTRIBUTION_TARGET = ("half", "center")
ALL_SESSIONS = ["ASIA", "EURO", "PRE", "RTH", "AH"]


def _factor_family_label(family: str) -> str:
    key = str(family or "").lower()
    if key == "icefishball":
        return "KDJMA"
    if key == "momentum_reversion":
        return "MREV"
    return "EMAPMO"


FACTOR_GRID = (
    # EMAPMO is the only new factor that showed an initial PF>2 candidate in
    # research, so it gets the widest PMO-mode sweep.
    ("emapmo", "all", "normal", "atr", 1.5, 2.0, 24),
    ("emapmo", "all", "normal", "atr_blend", 1.5, 2.0, 24),
    ("emapmo", "all", "normal", "atr_blend", 2.0, 2.0, 24),
    ("emapmo", "all", "normal", "atr_blend", 2.5, 2.0, 24),
    ("emapmo", "all", "early", "atr_blend", 1.5, 2.0, 24),
    ("emapmo", "all", "both", "atr_blend", 1.5, 2.0, 24),
    ("emapmo", "long_only", "normal", "atr", 1.5, 2.0, 24),
    ("emapmo", "long_only", "normal", "atr_blend", 1.5, 2.0, 12),
    ("emapmo", "long_only", "normal", "atr_blend", 1.5, 2.0, 24),
    ("emapmo", "long_only", "normal", "atr_blend", 2.0, 2.0, 24),
    ("emapmo", "long_only", "normal", "atr_blend", 2.5, 2.0, 24),
    ("emapmo", "long_only", "normal", "atr_blend", 2.5, 4.0, 24),
    ("emapmo", "long_only", "normal", "range15_pct", 0.5, 0.75, 24),
    ("emapmo", "long_only", "early", "atr_blend", 1.5, 2.0, 24),
    ("emapmo", "long_only", "early", "atr_blend", 2.5, 2.0, 24),
    ("emapmo", "long_only", "both", "atr_blend", 1.5, 2.0, 24),
    ("emapmo", "long_only", "both", "atr_blend", 2.5, 2.0, 24),
    ("emapmo", "short_only", "normal", "atr", 1.5, 2.0, 24),
    ("emapmo", "short_only", "normal", "atr_blend", 1.5, 2.0, 24),
    ("emapmo", "short_only", "normal", "atr_blend", 2.5, 2.0, 24),
    ("emapmo", "short_only", "early", "atr_blend", 1.5, 2.0, 24),
    ("emapmo", "short_only", "both", "atr_blend", 1.5, 2.0, 24),
    # Momentum-reversion and icefishball are lower-frequency / lower-confidence
    # factors, so sweep side, volatility rule, and hold without PMO modes.
    ("momentum_reversion", "all", "normal", "atr", 1.0, 1.5, 12),
    ("momentum_reversion", "all", "normal", "atr_blend", 1.5, 2.0, 12),
    ("momentum_reversion", "all", "normal", "atr_blend", 2.0, 2.0, 24),
    ("momentum_reversion", "all", "normal", "range15_pct", 0.5, 0.75, 12),
    ("momentum_reversion", "long_only", "normal", "atr", 1.0, 1.5, 12),
    ("momentum_reversion", "long_only", "normal", "atr_blend", 1.5, 2.0, 12),
    ("momentum_reversion", "long_only", "normal", "atr_blend", 2.0, 2.0, 24),
    ("momentum_reversion", "short_only", "normal", "atr", 1.0, 1.5, 12),
    ("momentum_reversion", "short_only", "normal", "atr_blend", 1.5, 2.0, 12),
    ("momentum_reversion", "short_only", "normal", "atr_blend", 2.0, 2.0, 24),
    ("icefishball", "all", "normal", "atr", 1.0, 1.5, 12),
    ("icefishball", "all", "normal", "atr_blend", 1.5, 2.0, 12),
    ("icefishball", "all", "normal", "atr_blend", 2.0, 2.0, 24),
    ("icefishball", "all", "normal", "range15_pct", 0.5, 0.75, 12),
    ("icefishball", "long_only", "normal", "atr", 1.0, 1.5, 12),
    ("icefishball", "long_only", "normal", "atr_blend", 1.5, 2.0, 12),
    ("icefishball", "long_only", "normal", "atr_blend", 2.0, 2.0, 24),
    ("icefishball", "short_only", "normal", "atr", 1.0, 1.5, 12),
    ("icefishball", "short_only", "normal", "atr_blend", 1.5, 2.0, 12),
    ("icefishball", "short_only", "normal", "atr_blend", 2.0, 2.0, 24),
)

FACTOR_SESSION_VA_GRID = (
    {
        "family": "icefishball",
        "side": "all",
        "pmo_mode": "normal",
        "sl_rule": "trend_ticks",
        "tp_rule": "trend_rr",
        "sl_value": 1.0,
        "tp_value": 1.0,
        "hold_bars": 24,
        "exit_mode": "tp",
    },
    {
        "family": "icefishball",
        "side": "all",
        "pmo_mode": "normal",
        "sl_rule": "trend_ticks",
        "tp_rule": "trend_rr",
        "sl_value": 1.0,
        "tp_value": 1.0,
        "hold_bars": 24,
        "exit_mode": "ladder",
    },
)


def build_trend_zone_timeline(
    candles: List[Candle],
    area_timeframe: str = "15m",
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


# 1.0.9: preset 快照允許鍵 — `+` 存 preset 時逐位重現 sweep 條件(不吃表單狀態)
_PRESET_SNAPSHOT_KEYS = (
    "strategy", "contract_id", "contract_size", "candle_seconds",
    "value_area_pct", "area_timeframe", "method", "tf_combo", "tr_overlap_trade_tf",
    "rr_ratio", "breakout_confirm_bars", "tr_exit_mode",
    "tr_daily_loss_stop", "tr_daily_win_stop", "tr_prev_rv_gate",
    "sl_ticks", "tr_sl_ticks", "tp_ticks", "tr_tp_ticks",
    "trail_enabled", "tr_trail_enabled", "trail_trigger_pct", "tr_trail_trigger_pct",
    "trail_sl_ticks", "tr_trail_sl_ticks", "trail_sl_pct", "tr_trail_sl_pct",
    "full_tp_lock", "tr_full_tp_lock",
    "one_trade_per_session_direction", "tr_one_trade_per_session", "tr_allowed_sessions",
    "sigma_window_minutes", "sigma_method", "sigma_entry_mode", "sigma_accept_mode",
    "sigma_start", "sigma_max", "sigma_target_mode", "sigma_stop_span",
    "sigma_accept_sigma", "sigma_accept_bars",
    "fade_tp_frac", "fade_entry_mode",
    "pmo_timeframe_minutes", "pmo_signal_mode", "pmo_sl_atr", "pmo_tp_atr",
    "pmo_max_hold_bars", "pmo_max_trades_per_day", "pmo_warmup_bars",
    "factor_timeframe_minutes", "factor_signal_family", "factor_side_mode",
    "factor_pmo_signal_mode", "factor_sl_rule", "factor_tp_rule",
    "factor_sl_value", "factor_tp_value", "factor_max_hold_bars",
    "factor_max_trades_per_day", "factor_warmup_bars", "factor_session_va_filter",
)


def _preset_snapshot(params: StrategyParams) -> dict:
    out = {}
    for k in _PRESET_SNAPSHOT_KEYS:
        if hasattr(params, k):
            v = getattr(params, k)
            out[k] = list(v) if isinstance(v, (list, tuple)) else v
    return out


def _run_one(params: StrategyParams, candles: List[Candle], timeline: Optional[List[dict]]) -> dict:
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
    week = defaultdict(float)          # 1.0.9: 週變異(CV)用
    trade_pnls = []
    ordered_pnls: List[float] = []     # 1.0.9: 依序淨損益(scale 測試用)
    gain = loss = 0.0
    long_n = long_w = short_n = short_w = 0   # 1.0.9: 多/空分開勝率
    for t in result.trades:
        p = t.pnl or 0.0
        dk = _topstep_trade_date(t.entry_time)
        day[dk] += p
        trade_pnls.append((dk, p))
        ordered_pnls.append(float(p))
        from datetime import date as _d2
        _dd = _d2.fromisoformat(dk)
        week[_dd.strftime("%G-W%V")] += p
        _dir = str(getattr(t, "direction", "") or "").upper()
        is_long = "BUY" in _dir or "LONG" in _dir
        if is_long:
            long_n += 1
            if p > 0:
                long_w += 1
        else:
            short_n += 1
            if p > 0:
                short_w += 1
        if p > 0:
            gain += p
        else:
            loss += p
    # monthly_avg = 30.44 天歸一化月率(run-rate);日曆月分組平均會被
    # 部分月(月初/月末只有幾個交易日)嚴重拖低,故不用。
    monthly_rate = 0.0
    seg_pnls = [0.0, 0.0, 0.0]
    seg_gains = [0.0, 0.0, 0.0]
    seg_losses = [0.0, 0.0, 0.0]
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
        for dk, p in trade_pnls:
            off = (_date.fromisoformat(dk) - d0).days
            seg = min(2, int(off * 3 / span_days))
            if p > 0:
                seg_gains[seg] += p
            else:
                seg_losses[seg] += abs(p)
    seg_pfs = [
        (999.0 if g > 0 and l <= 0 else (g / l if l > 0 else 0.0))
        for g, l in zip(seg_gains, seg_losses)
    ]
    strategy = str(getattr(params, "strategy", "") or "").lower()
    if strategy == "fade":
        model = "DAY ZONE"
    elif strategy == "sigma":
        model = "DISTRIBUTION"
    elif strategy == "factor":
        model = "FACTOR"
    else:
        model = "TREND"
    # 1.0.9: 週變異 CV = std(週PnL) / |mean(週PnL)|(對齊 performance 卡 Σ/CV)
    wvals = list(week.values())
    weekly_std = weekly_cv = 0.0
    if len(wvals) >= 2:
        import statistics as _st
        weekly_std = _st.pstdev(wvals)
        wmean = sum(wvals) / len(wvals)
        weekly_cv = (weekly_std / abs(wmean)) if abs(wmean) > 1e-9 else 99.0
    span_days_out = 0
    if day:
        from datetime import date as _date3
        ks = sorted(day.keys())
        span_days_out = max(1, (_date3.fromisoformat(ks[-1]) - _date3.fromisoformat(ks[0])).days + 1)
    trades_per_month = (float(m.total_trades) * 30.44 / span_days_out) if span_days_out else 0.0
    return {
        "seg_pnls": [round(x, 1) for x in seg_pnls],
        "seg_pfs": [round(x, 2) for x in seg_pfs],
        "model": model,
        "wf_pass": bool(all(pnl > 0 and pf > 1.0 for pnl, pf in zip(seg_pnls, seg_pfs))),
        "trades": int(m.total_trades),
        "win_rate": round(float(m.win_rate), 4),
        "long_trades": long_n, "long_win": round(long_w / long_n, 4) if long_n else 0.0,
        "short_trades": short_n, "short_win": round(short_w / short_n, 4) if short_n else 0.0,
        "pnl": round(float(m.total_pnl), 1),
        "gain": round(gain, 1),
        "loss": round(loss, 1),
        "pf": round(float(m.profit_factor), 3),
        "max_dd": round(float(m.max_drawdown), 1),
        "expect": round(float(m.expectancy), 2),
        "worst_day": round(min(day.values()) if day else 0.0, 1),
        "monthly_avg": round(monthly_rate, 1),
        "trades_per_month": round(trades_per_month, 1),
        "weekly_std": round(weekly_std, 1),
        "weekly_cv": round(weekly_cv, 3),
        "score": round(float(m.total_pnl) / max(float(m.max_drawdown), 100.0), 3),
        "_ordered_pnls": [round(x, 2) for x in ordered_pnls],   # scale 測試用(存檔前移除)
        "preset_params": _preset_snapshot(params),   # 1.0.9: `+` 存 preset 逐位重現用
    }


def _scaled_stats(ordered_pnls: List[float], gross_mult: float,
                  cost_old_rt: float, cost_new_rt: float, n_contracts: int) -> dict:
    """把 1×MNQ 的逐筆淨損益換算成 N 張/另一合約:net' = (net+cost_old)*mult − cost_new*N。

    PF/maxDD 由縮放後逐筆序列重建(trade-level equity peak-trough)。"""
    scaled = [(p + cost_old_rt) * gross_mult - cost_new_rt * n_contracts for p in ordered_pnls]
    g = sum(p for p in scaled if p > 0)
    l = sum(-p for p in scaled if p < 0)
    eq = peak = dd = 0.0
    for p in scaled:
        eq += p
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {
        "pnl": round(sum(scaled), 1),
        "pf": round((g / l) if l > 0 else (999.0 if g > 0 else 0.0), 3),
        "max_dd": round(dd, 1),
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
        timelines[va] = build_trend_zone_timeline(candles, "15m", va)
        done += 1

    for va, exit_mode, rr, c_bars, stop in grid:
        p = copy.deepcopy(base_params)
        p.strategy = "trend"
        p.value_area_pct = va
        p.area_timeframe = "15m"
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


def run_day_zone_sweep(
    candles: List[Candle],
    base_params: StrategyParams,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> List[dict]:
    """Sweep DAY ZONE variants: previous-day VA limit/rejection plus OR15."""
    grid = [
        (entry, sl, tp_frac, stop)
        for entry in DAY_ZONE_ENTRY
        for sl in DAY_ZONE_SL
        for tp_frac in DAY_ZONE_TP_FRAC
        for stop in DAY_ZONE_STOP
    ]
    # OR15 has fixed internal SL/TP fractions, so only daily stop is swept here.
    or15_grid = [("or15", 0, 1.0, stop) for stop in DAY_ZONE_STOP]
    full_grid = grid + or15_grid
    total = len(full_grid)
    results: List[dict] = []

    for i, (entry, sl, tp_frac, stop) in enumerate(full_grid, start=1):
        p = copy.deepcopy(base_params)
        p.strategy = "fade"
        p.area_timeframe = "15m"
        p.method = "single"
        p.tf_combo = []
        p.tr_allowed_sessions = list(ALL_SESSIONS)
        p.tr_one_trade_per_session = False
        p.one_trade_per_session_direction = False
        p.tr_exit_mode = "tp"
        p.tr_daily_loss_stop = int(stop)
        p.fade_entry_mode = entry
        p.fade_tp_frac = float(tp_frac)
        if entry != "or15":
            p.sl_ticks = int(sl)
            p.tr_sl_ticks = int(sl)
        r = _run_one(p, candles, None)
        r["params"] = {
            "strategy": "fade",
            "fade_entry_mode": entry,
            "sl_ticks": int(sl),
            "fade_tp_frac": float(tp_frac),
            "tr_daily_loss_stop": int(stop),
        }
        r["label"] = (
            f"{entry.upper()} "
            + (f"SL{sl} TP{int(tp_frac * 100)}%" if entry != "or15" else "SL0.2R TP1R")
            + f" S{stop}"
        )
        results.append(r)
        if progress_cb and (i % 4 == 0 or i == total):
            progress_cb(i, total, "DAY ZONE " + r["label"])

    _annotate_plateau_and_acceptance(results)
    return results


def run_distribution_sweep(
    candles: List[Candle],
    base_params: StrategyParams,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> List[dict]:
    """Sweep DISTRIBUTION rolling-sigma variants."""
    grid = [
        (window, method, entry, accept, stop_span, target)
        for window in DISTRIBUTION_WINDOWS
        for method in DISTRIBUTION_METHODS
        for entry in DISTRIBUTION_ENTRY
        for accept in DISTRIBUTION_ACCEPT
        for stop_span in DISTRIBUTION_STOP_SPAN
        for target in DISTRIBUTION_TARGET
    ]
    total = len(grid)
    results: List[dict] = []

    for i, (window, method, entry, accept, stop_span, target) in enumerate(grid, start=1):
        p = copy.deepcopy(base_params)
        p.strategy = "sigma"
        p.area_timeframe = "15m"
        p.method = "single"
        p.tf_combo = []
        p.tr_allowed_sessions = ["RTH"]
        p.tr_one_trade_per_session = False
        p.one_trade_per_session_direction = False
        p.tr_exit_mode = "tp"
        p.tr_daily_loss_stop = 1
        p.trail_enabled = False
        p.tr_trail_enabled = False
        p.sigma_window_minutes = int(window)
        p.sigma_method = method
        p.sigma_entry_mode = entry
        p.sigma_accept_mode = accept
        p.sigma_start = 1.0
        p.sigma_max = 3.0
        p.sigma_stop_span = float(stop_span)
        p.sigma_target_mode = target
        r = _run_one(p, candles, None)
        r["params"] = {
            "strategy": "sigma",
            "sigma_window_minutes": int(window),
            "sigma_method": method,
            "sigma_entry_mode": entry,
            "sigma_accept_mode": accept,
            "sigma_stop_span": float(stop_span),
            "sigma_target_mode": target,
            "tr_daily_loss_stop": 1,
        }
        r["label"] = (
            f"Roll{window} {method.upper()} {entry} Accept{accept} "
            f"SL{stop_span:g} TP{target}"
        )
        results.append(r)
        if progress_cb and (i % 4 == 0 or i == total):
            progress_cb(i, total, "DISTRIBUTION " + r["label"])

    _annotate_plateau_and_acceptance(results)
    return results


def run_factor_sweep(
    candles: List[Candle],
    base_params: StrategyParams,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> List[dict]:
    """Sweep completed-candle factor strategies.

    FACTOR is live/backtest compatible: completed 5m signal, market entry,
    volatility-aware SL/TP, optional max hold, and session filters.
    """
    total = len(FACTOR_GRID) + len(FACTOR_SESSION_VA_GRID)
    results: List[dict] = []

    for i, (family, side, pmo_mode, rule, sl_value, tp_value, hold_bars) in enumerate(FACTOR_GRID, start=1):
        p = copy.deepcopy(base_params)
        p.strategy = "factor"
        p.area_timeframe = "15m"
        p.method = "single"
        p.tf_combo = []
        p.tr_allowed_sessions = list(ALL_SESSIONS)
        p.tr_one_trade_per_session = False
        p.one_trade_per_session_direction = False
        p.tr_exit_mode = "tp"
        p.tr_daily_loss_stop = 1
        p.trail_enabled = False
        p.tr_trail_enabled = False
        p.factor_timeframe_minutes = 5
        p.factor_signal_family = str(family)
        p.factor_side_mode = str(side)
        p.factor_pmo_signal_mode = str(pmo_mode)
        p.factor_session_va_filter = "off"
        p.factor_sl_rule = str(rule)
        p.factor_tp_rule = str(rule)
        p.factor_sl_value = float(sl_value)
        p.factor_tp_value = float(tp_value)
        p.factor_max_hold_bars = 0   # 1.0.9: HOLD 5m system removed → sweep SL/TP-only (hold_bars ignored)
        p.factor_max_trades_per_day = 3
        p.factor_warmup_bars = 150
        r = _run_one(p, candles, None)
        r["params"] = {
            "strategy": "factor",
            "tr_allowed_sessions": list(ALL_SESSIONS),
            "tr_one_trade_per_session": False,
            "one_trade_per_session_direction": False,
            "tr_exit_mode": "tp",
            "tr_daily_loss_stop": 1,
            "factor_timeframe_minutes": 5,
            "factor_signal_family": str(family),
            "factor_side_mode": str(side),
            "factor_pmo_signal_mode": str(pmo_mode),
            "factor_session_va_filter": "off",
            "factor_sl_rule": str(rule),
            "factor_tp_rule": str(rule),
            "factor_sl_value": float(sl_value),
            "factor_tp_value": float(tp_value),
            "factor_max_hold_bars": 0,   # 1.0.9: HOLD 5m system removed → SL/TP-only
            "factor_max_trades_per_day": 3,
            "factor_warmup_bars": 150,
        }
        r["label"] = (
            f"{_factor_family_label(str(family))} {side} {pmo_mode} {rule} "
            f"SL{float(sl_value):g} TP{float(tp_value):g} HOFF"
        )
        results.append(r)
        if progress_cb and (i % 4 == 0 or i == total):
            progress_cb(i, total, "FACTOR " + r["label"])

    offset = len(FACTOR_GRID)
    for j, spec in enumerate(FACTOR_SESSION_VA_GRID, start=1):
        p = copy.deepcopy(base_params)
        p.strategy = "factor"
        p.area_timeframe = "session"
        p.value_area_pct = 0.80
        p.method = "single"
        p.tf_combo = []
        p.tr_allowed_sessions = list(ALL_SESSIONS)
        p.tr_one_trade_per_session = False
        p.one_trade_per_session_direction = False
        p.tr_exit_mode = str(spec["exit_mode"])
        p.tr_daily_loss_stop = 1
        p.trail_enabled = False
        p.tr_trail_enabled = False
        p.factor_timeframe_minutes = 5
        p.factor_signal_family = str(spec["family"])
        p.factor_side_mode = str(spec["side"])
        p.factor_pmo_signal_mode = str(spec["pmo_mode"])
        p.factor_session_va_filter = "outside"
        p.factor_sl_rule = str(spec["sl_rule"])
        p.factor_tp_rule = str(spec["tp_rule"])
        p.factor_sl_value = float(spec["sl_value"])
        p.factor_tp_value = float(spec["tp_value"])
        p.factor_max_hold_bars = 0   # 1.0.9: HOLD 5m system removed → sweep SL/TP-only
        p.factor_max_trades_per_day = 3
        p.factor_warmup_bars = 150
        r = _run_one(p, candles, None)
        r["params"] = {
            "strategy": "factor",
            "area_timeframe": "session",
            "value_area_pct": 0.80,
            "tr_allowed_sessions": list(ALL_SESSIONS),
            "tr_one_trade_per_session": False,
            "one_trade_per_session_direction": False,
            "tr_exit_mode": str(spec["exit_mode"]),
            "tr_daily_loss_stop": 1,
            "factor_timeframe_minutes": 5,
            "factor_signal_family": str(spec["family"]),
            "factor_side_mode": str(spec["side"]),
            "factor_pmo_signal_mode": str(spec["pmo_mode"]),
            "factor_session_va_filter": "outside",
            "factor_sl_rule": str(spec["sl_rule"]),
            "factor_tp_rule": str(spec["tp_rule"]),
            "factor_sl_value": float(spec["sl_value"]),
            "factor_tp_value": float(spec["tp_value"]),
            "factor_max_hold_bars": 0,   # 1.0.9: HOLD 5m system removed → SL/TP-only
            "factor_max_trades_per_day": 3,
            "factor_warmup_bars": 150,
        }
        r["label"] = (
            f"{_factor_family_label(str(spec['family']))} VA80 outside "
            f"{str(spec['exit_mode']).upper()} SLtrend TPrr HOFF"
        )
        results.append(r)
        done = offset + j
        if progress_cb and (done % 4 == 0 or done == total):
            progress_cb(done, total, "FACTOR " + r["label"])

    _annotate_plateau_and_acceptance(results)
    return results


def run_model_sweep(
    candles: List[Candle],
    base_params: StrategyParams,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    models: Optional[List[str]] = None,
) -> List[dict]:
    """Run sweeps for every live/backtest-ready model and return one result list."""
    # 1.0.9: run/lock 面板 — 只跑勾選的 model(None=全跑);contract/size 鎖定 base(MNQx1)
    want = {m.strip().upper() for m in (models or []) if str(m).strip()} or None
    def _on(name):
        return want is None or name in want
    trend_total = len(SWEEP_VA) + len(SWEEP_VA) * len(SWEEP_EXITS) * len(SWEEP_CONFIRM) * len(SWEEP_STOP)
    day_total = len(DAY_ZONE_ENTRY) * len(DAY_ZONE_SL) * len(DAY_ZONE_TP_FRAC) * len(DAY_ZONE_STOP) + len(DAY_ZONE_STOP)
    dist_total = (
        len(DISTRIBUTION_WINDOWS) * len(DISTRIBUTION_METHODS) * len(DISTRIBUTION_ENTRY)
        * len(DISTRIBUTION_ACCEPT) * len(DISTRIBUTION_STOP_SPAN) * len(DISTRIBUTION_TARGET)
    )
    factor_total = len(FACTOR_GRID) + len(FACTOR_SESSION_VA_GRID)
    grand_total = trend_total + day_total + dist_total + factor_total
    done_offset = 0

    def _wrap(model: str, offset: int):
        def _progress(cur: int, total: int, detail: str) -> None:
            if progress_cb:
                progress_cb(offset + cur, grand_total, f"{model}: {detail}")
        return _progress

    import gc
    out: List[dict] = []
    if _on("TREND"):
        out.extend(run_trend_sweep(candles, base_params, _wrap("TREND", done_offset)))
    done_offset += trend_total
    gc.collect()   # 釋放 trend zone timeline(峰值記憶體)
    if _on("DAY ZONE"):
        out.extend(run_day_zone_sweep(candles, base_params, _wrap("DAY ZONE", done_offset)))
    done_offset += day_total
    gc.collect()
    if _on("DISTRIBUTION"):
        out.extend(run_distribution_sweep(candles, base_params, _wrap("DISTRIBUTION", done_offset)))
    done_offset += dist_total
    gc.collect()
    if _on("FACTOR"):
        out.extend(run_factor_sweep(candles, base_params, _wrap("FACTOR", done_offset)))
    _annotate_plateau_and_acceptance(out)
    gc.collect()
    return out


def _annotate_plateau_and_acceptance(results: List[dict]) -> None:
    """1.0.9 P1: generic plateau test + PF-first acceptance.

    Plateau = same model, all params equal except one dimension, and the changed
    value is adjacent in that dimension's sweep list. A setup is accepted only
    when edge quality passes first; raw PnL is not a promotion shortcut.
    """
    if not results:
        return

    by_model = defaultdict(list)
    for r in results:
        by_model[str(r.get("model") or "TREND")].append(r)

    def _param_value_key(value):
        if isinstance(value, list):
            return tuple(_param_value_key(v) for v in value)
        if isinstance(value, dict):
            return tuple(sorted((k, _param_value_key(v)) for k, v in value.items()))
        return value

    for model_rows in by_model.values():
        keys = sorted({k for r in model_rows for k in (r.get("params") or {}).keys()})
        values = {
            k: sorted({_param_value_key((r.get("params") or {}).get(k)) for r in model_rows}, key=lambda x: str(x))
            for k in keys
        }
        for r in model_rows:
            p = r.get("params") or {}
            vals = []
            for other in model_rows:
                if other is r:
                    continue
                op = other.get("params") or {}
                diff = [k for k in keys if _param_value_key(p.get(k)) != _param_value_key(op.get(k))]
                if len(diff) != 1:
                    continue
                k = diff[0]
                seq = values.get(k) or []
                try:
                    if abs(seq.index(_param_value_key(p.get(k))) - seq.index(_param_value_key(op.get(k)))) == 1:
                        vals.append(other.get("pnl", 0.0))
                except ValueError:
                    continue
            pos = sum(1 for v in vals if v > 0)
            r["plateau_pass"] = bool(vals and pos / len(vals) >= 0.6 and r.get("pnl", 0.0) > 0)
            # ── 1.0.9 使用者 ACC 條件(2026-07-07)──
            # ACC:月PnL>3000、PF>1.5、月均筆數≥20、maxDD<1000、週變異CV<1
            # Scale 候選:月PnL 100..3000、PF>2、月均筆數>20、maxDD<500
            #   → 換算 3×MNQ 與 1×NQ,縮放後過 ACC 就以該 contract 標記接受。
            monthly = float(r.get("monthly_avg", 0.0) or 0.0)
            pf = float(r.get("pf", 0.0) or 0.0)
            tpm = float(r.get("trades_per_month", 0.0) or 0.0)
            dd = float(r.get("max_dd", 0.0) or 0.0)
            wcv = float(r.get("weekly_cv", 99.0) or 99.0)
            base_ok = bool(r.get("wf_pass") and r["plateau_pass"])

            def _acc(mon, pf_, dd_):
                return mon > 3000 and pf_ > 1.5 and tpm >= 20 and dd_ < 1000 and wcv < 1.0

            r["contract_scale"] = "MNQx1"
            r["accept"] = bool(base_ok and _acc(monthly, pf, dd))
            if (not r["accept"] and base_ok and 100 <= monthly <= 3000
                    and pf > 2.0 and tpm > 20 and dd < 500):
                pnls = r.get("_ordered_pnls") or []
                span_factor = monthly / max(1e-9, float(r.get("pnl", 0.0) or 0.0))
                # MNQ RT 成本 1.24;NQ RT 成本 3.80;NQ 點值 = 10×MNQ
                for label, s in (
                    ("MNQx3", _scaled_stats(pnls, 3.0, 1.24, 1.24, 3)),
                    ("NQx1", _scaled_stats(pnls, 10.0, 1.24, 3.80, 1)),
                ):
                    s_monthly = s["pnl"] * span_factor
                    if _acc(s_monthly, s["pf"], s["max_dd"]):
                        r["accept"] = True
                        r["contract_scale"] = label
                        r["scaled"] = {"contract": label, "pnl": s["pnl"], "pf": s["pf"],
                                       "max_dd": s["max_dd"], "monthly_avg": round(s_monthly, 1)}
                        break
        for r in model_rows:
            r.pop("_ordered_pnls", None)   # 內部欄位不入存檔
