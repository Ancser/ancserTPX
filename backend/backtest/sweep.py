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
from backend.db.models import (
    current_quarterly_contract_id, get_point_value, get_tick_size,
)

logger = logging.getLogger(__name__)

# 預設 grid(與 1.0.8 研究掃描一致):
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

# ── PI grid (1.0.10p) ────────────────────────────────────────────────────
# The other models sweep pre-baked combinations. PI deliberately sweeps every
# signal kind as its OWN on/off switch instead of the named PI_SIGNAL_SETS
# presets, so "青π alone" and "青π + 深蓝圈" are separate, separately-scored
# grid points rather than being hidden inside a set name. PiSignalStrategy
# already honours explicit pi_long_kinds/pi_short_kinds over pi_signal_set.
#
# 紫圈 is absent on purpose: PI-004 makes short bubbles record-only and
# PiSignalStrategy strips SHORT_BUBBLE_KINDS unconditionally, so a grid point
# containing it would silently collapse onto the identical run without it.
# That also means the "short level 1 / level 2 are mixed"問題 does not reach
# this sweep — 粉π is the only tradeable short kind.
PI_LONG_KINDS = ("青π", "深蓝圈", "淡蓝圈")
PI_SHORT_KINDS = ("粉π",)
PI_SL = (2.5, 3.0, 3.5, 4.0, 4.5)      # ×ATR blend; PI BEST is 4.0
PI_RR = (2, 3, 4, 6)                   # PI BEST is 3; 6 ≈ "ride to the close"
PI_MAX_AGE = (5, 10)                   # PI BEST is 5


def _subsets(items: tuple) -> tuple:
    """Every on/off combination of `items`, shortest first."""
    out = [()]
    for item in items:
        out += [combo + (item,) for combo in out]
    return tuple(sorted(out, key=len))


PI_KIND_COMBOS = tuple(
    (longs, shorts)
    for longs in _subsets(PI_LONG_KINDS)
    for shorts in _subsets(PI_SHORT_KINDS)
    if longs or shorts          # a grid point that trades nothing is not a variant
)


def _factor_family_label(family: str) -> str:
    key = str(family or "").lower()
    if key == "icefishball":
        return "KDJMA"
    if key == "momentum_reversion":
        return "MREV"
    return "EMAPMO"


# 1.0.9: 網格座標改成 (family, side, pmo_mode, sl_rule, sl_value, rr) —— TP 不再
# 是獨立的絕對值。原因:UI 送出的參數契約是(frontend/static/ancserTPX.js:1197)
#     factor_tp_rule  = factor_sl_rule        # TP 規則永遠鏡射 SL
#     factor_tp_value = factor_sl_value * rr  # rr ∈ 1..6
# 舊網格把 tp_value 寫死成絕對 ATR 倍數且上限只到 4.0,於是 BEST preset
# (atr_blend × SL2.5 × rr3 → TP7.5)整個掉在搜索空間之外 —— 實測 BEST PF 4.10,
# 而舊網格冠軍(同訊號但 TP2)只有 2.95。用 rr 當座標後,每個變體都能原樣存成
# preset,掃得到的一定調得出來。
#
# UI 的合法值(_factorRiskOptionList):
#     atr / atr_blend → sl_value ∈ {1, 1.5, 2, 2.5, 3}
#     range15_pct     → sl_value ∈ {0.10, 0.15, 0.20, 0.50, 0.75}
# 這裡是 app 內建 sweep,為了維持約 10–15 分鐘的執行時間只取重點切片;
# 完整 1890 格窮舉見 scripts/emapmo_full_sweep.py。
FACTOR_GRID = (
    # EMAPMO: 唯一在研究中出現 PF>2 的因子,給最寬的 rr 掃描(含 BEST 的 rr3)。
    ("emapmo", "all", "normal", "atr_blend", 1.5, 2),
    ("emapmo", "all", "normal", "atr_blend", 2.5, 2),
    ("emapmo", "all", "normal", "atr_blend", 2.5, 3),
    ("emapmo", "all", "early", "atr_blend", 2.5, 3),
    ("emapmo", "all", "both", "atr_blend", 2.5, 3),
    ("emapmo", "long_only", "normal", "atr", 1.5, 2),
    ("emapmo", "long_only", "normal", "atr", 2.5, 3),
    ("emapmo", "long_only", "normal", "atr_blend", 1.5, 2),
    ("emapmo", "long_only", "normal", "atr_blend", 2.0, 2),
    ("emapmo", "long_only", "normal", "atr_blend", 2.5, 1),
    ("emapmo", "long_only", "normal", "atr_blend", 2.5, 2),
    ("emapmo", "long_only", "normal", "atr_blend", 2.5, 3),
    ("emapmo", "long_only", "normal", "atr_blend", 2.5, 4),
    ("emapmo", "long_only", "normal", "atr_blend", 2.5, 6),
    ("emapmo", "long_only", "normal", "atr_blend", 3.0, 3),
    ("emapmo", "long_only", "normal", "range15_pct", 0.20, 3),
    ("emapmo", "long_only", "normal", "range15_pct", 0.50, 3),
    ("emapmo", "long_only", "early", "atr_blend", 1.5, 2),
    ("emapmo", "long_only", "early", "atr_blend", 2.5, 2),
    ("emapmo", "long_only", "early", "atr_blend", 2.5, 3),   # ← BEST preset
    ("emapmo", "long_only", "early", "atr_blend", 2.5, 4),
    ("emapmo", "long_only", "early", "atr_blend", 3.0, 3),
    ("emapmo", "long_only", "both", "atr_blend", 2.5, 2),
    ("emapmo", "long_only", "both", "atr_blend", 2.5, 3),
    ("emapmo", "short_only", "normal", "atr", 1.5, 2),
    ("emapmo", "short_only", "normal", "atr_blend", 2.5, 2),
    ("emapmo", "short_only", "normal", "atr_blend", 2.5, 3),
    ("emapmo", "short_only", "early", "atr_blend", 2.5, 3),
    ("emapmo", "short_only", "both", "atr_blend", 2.5, 3),
    # Momentum-reversion 與 icefishball 頻率低、信心低,只掃 side / 規則 / rr。
    ("momentum_reversion", "all", "normal", "atr", 1.0, 2),
    ("momentum_reversion", "all", "normal", "atr_blend", 1.5, 2),
    ("momentum_reversion", "all", "normal", "atr_blend", 2.0, 3),
    ("momentum_reversion", "long_only", "normal", "atr", 1.0, 2),
    ("momentum_reversion", "long_only", "normal", "atr", 1.0, 3),
    ("momentum_reversion", "long_only", "normal", "atr_blend", 1.5, 2),
    ("momentum_reversion", "long_only", "normal", "atr_blend", 2.0, 3),
    ("momentum_reversion", "short_only", "normal", "atr", 1.0, 2),
    ("momentum_reversion", "short_only", "normal", "atr_blend", 2.0, 3),
    ("icefishball", "all", "normal", "atr", 1.0, 2),
    ("icefishball", "all", "normal", "atr_blend", 1.5, 2),
    ("icefishball", "all", "normal", "atr_blend", 2.0, 3),
    ("icefishball", "long_only", "normal", "atr", 1.0, 2),
    ("icefishball", "long_only", "normal", "atr_blend", 2.0, 3),
    ("icefishball", "short_only", "normal", "atr", 1.0, 2),
    ("icefishball", "short_only", "normal", "atr_blend", 2.0, 3),
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


_PRESET_SNAPSHOT_KEYS = (
    "strategy", "contract_id", "contract_size", "candle_seconds",
    "value_area_pct", "area_timeframe", "method", "tf_combo", "tr_overlap_trade_tf",
    "rr_ratio", "breakout_confirm_bars", "tr_exit_mode",
    "tr_daily_loss_stop", "tr_daily_win_stop",
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
    # 1.0.9: 新增參數也要進快照,否則 sweep 結果存成 preset 時會遺失,
    # 而且 G5 跨商品重跑會用到錯的門檻/風險上限。
    "factor_pmo_threshold_scale", "factor_pmo_normal_scale", "factor_pmo_early_scale",
    "max_profit_ticks",
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


# 1.0.9: run_trend_sweep / TREND_GRID / build_trend_zone_timeline 已移除 —
# TREND 288 個變體 0 通過 MC+WF+PF>2。見 docs/1.0.9_DELETE_LIST.md。


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

    for i, (family, side, pmo_mode, rule, sl_value, rr) in enumerate(FACTOR_GRID, start=1):
        # UI 契約:TP 規則鏡射 SL 規則,TP 值 = SL 值 x rr(見 FACTOR_GRID 註解)
        tp_value = float(sl_value) * float(rr)
        p = copy.deepcopy(base_params)
        p.strategy = "factor"
        p.area_timeframe = "15m"
        p.method = "single"
        p.tf_combo = []
        p.tr_allowed_sessions = list(ALL_SESSIONS)
        p.tr_one_trade_per_session = False
        p.one_trade_per_session_direction = False
        p.tr_exit_mode = "tp"
        p.rr_ratio = int(rr)
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
            "rr_ratio": int(rr),
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
            f"SL{float(sl_value):g} RR{int(rr)}(TP{tp_value:g}) HOFF"
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



# ── 1.0.9 G5:跨商品交叉驗證 ────────────────────────────────
# G0–G4(訊號數 / 獲利 / 滑價 / 走查 / 蒙地卡羅)全部是「同一份資料的內部
# 檢定」,擋不住曲線擬合 —— 研究實測:8 個公開策略族的冠軍全都通過 G0–G4,
# 但把同一組參數搬到另一個商品後全數變負(RSI2 MNQ冠軍→MES PF 0.745;
# MES冠軍→MNQ PF 0.853,完美對角線)。
#
# 時間上沒有樣本外可用(資料只有 2.5 個月),但 MNQ 與 MES 追蹤不同指數,
# 相關約 0.9 卻不完全同步 —— 這是目前唯一近似樣本外的檢定。
#
# 判定:同一組參數在另一個商品上,每筆平均淨損益仍要 > 實測往返滑價。
# 只驗已 accept 的變體(通常個位數),成本很低。
# 詳見 docs/1.0.9_RESEARCH_FINDINGS.md。

G5_SLIP_TICKS = 14.0          # 實測 EMAPMO 市價成交往返滑價(3.5 pts)
G5_CROSS = {"MNQ": "MES", "MES": "MNQ"}
G5_MAX_VARIANTS = 40          # 上限,避免 accept 過多時拖垮 sweep


def _edge_ticks(pnl: float, trades: int, contract_id: str) -> Optional[float]:
    if not trades:
        return None
    tv = get_tick_size(contract_id) * get_point_value(contract_id)
    return (float(pnl) / trades / tv) if tv else None


def cross_symbol_validate(results: List[dict], base_params: StrategyParams,
                          progress_cb: Optional[Callable] = None) -> None:
    """把已 accept 的變體搬到另一個商品重跑,不過 G5 就撤銷 accept。"""
    from backend.data import candle_store

    sym = _extract_symbol(getattr(base_params, "contract_id", "") or "")
    other = G5_CROSS.get(sym)
    accepted = [r for r in results if r.get("accept")]
    if not other or not accepted:
        for r in results:
            r.setdefault("g5_pass", None)
        return

    other_bars = sorted(candle_store.load(other, 1), key=lambda c: c.timestamp)
    if len(other_bars) < 5000:
        logger.info("[G5] %s store 太小(%d 根),跳過跨商品驗證", other, len(other_bars))
        for r in results:
            r.setdefault("g5_pass", None)
            r.setdefault("g5_reason", f"{other} 資料不足")
        return

    other_cid = current_quarterly_contract_id(other)
    todo = sorted(accepted, key=lambda r: -float(r.get("pf") or 0))[:G5_MAX_VARIANTS]
    logger.info("[G5] %s → %s:驗證 %d/%d 個已接受變體",
                sym, other, len(todo), len(accepted))

    for i, r in enumerate(todo, 1):
        snap = r.get("preset_params") or {}
        p = copy.deepcopy(base_params)
        for k, v in snap.items():
            if hasattr(p, k) and k != "contract_id":
                setattr(p, k, list(v) if isinstance(v, (list, tuple)) else v)
        p.contract_id = other_cid
        try:
            cross = _run_one(p, other_bars, None)
        except Exception as exc:      # 單一變體失敗不該中斷整個 sweep
            r["g5_pass"] = False
            r["g5_reason"] = f"{type(exc).__name__}: {exc}"
            continue
        cross.pop("_ordered_pnls", None)
        home_edge = _edge_ticks(r.get("pnl", 0.0), r.get("trades", 0),
                                getattr(base_params, "contract_id", ""))
        cross_edge = _edge_ticks(cross.get("pnl", 0.0), cross.get("trades", 0), other_cid)
        r["g5_symbol"] = other
        r["g5_trades"] = cross.get("trades")
        r["g5_pf"] = cross.get("pf")
        r["g5_edge_ticks"] = None if cross_edge is None else round(cross_edge, 1)
        r["g5_home_edge_ticks"] = None if home_edge is None else round(home_edge, 1)
        r["g5_pass"] = bool(cross_edge is not None and cross_edge > G5_SLIP_TICKS
                            and home_edge is not None and home_edge > G5_SLIP_TICKS)
        if not r["g5_pass"]:
            r["accept"] = False
            r["g5_reason"] = (f"{other} 每筆邊際 "
                              f"{'n/a' if cross_edge is None else round(cross_edge, 1)}t "
                              f"≤ {G5_SLIP_TICKS:g}t")
        if progress_cb:
            progress_cb(i, len(todo), f"G5 {other}: {r.get('label', '')}")

    for r in results:
        r.setdefault("g5_pass", None)   # 未驗證(本來就沒 accept)


def _pi_signal_window(candles: List[Candle]) -> List[Candle]:
    """Trim candles to the span where PI signals actually exist.

    PI is signal-driven: the Discord history covers about two months, while the
    candle store holds six years. Feeding the whole store to the engine spends
    97% of every run stepping through bars that can never produce a PI trade —
    measured 58.8s per variant full-range versus 1.66s windowed, with byte-
    identical results (trades=11, PF 3.173 both ways). The warm-up margin keeps
    the ATR/blend state the SL rule needs.

    Returns the input unchanged if the history is unreadable, so a broken or
    empty history degrades to "slow but correct" instead of "silently zero".
    """
    from datetime import timedelta
    try:
        from backend.data.pi_history import load_rows, parse_ts
        stamps = sorted(parse_ts(r["ts"]) for r in load_rows())
    except Exception as exc:                      # pragma: no cover - defensive
        logger.warning("[PI] signal window unavailable (%s: %s); using full range",
                       type(exc).__name__, exc)
        return candles
    if not stamps or not candles:
        return candles
    lo = stamps[0] - timedelta(days=3)            # warm-up for ATR/blend
    hi = stamps[-1] + timedelta(days=1)
    window = [c for c in candles if lo <= c.timestamp <= hi]
    return window or candles


def run_pi_sweep(
    candles: List[Candle],
    base_params: StrategyParams,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> List[dict]:
    """Sweep PI signal-kind switches against SL / RR / signal-age.

    Every dimension is independent — each signal kind is its own switch — so
    the result table can answer "is 淡蓝圈 carrying its weight?" directly
    instead of only comparing named presets against each other.
    """
    window = _pi_signal_window(candles)
    total = len(PI_KIND_COMBOS) * len(PI_SL) * len(PI_RR) * len(PI_MAX_AGE)
    results: List[dict] = []
    i = 0

    for longs, shorts in PI_KIND_COMBOS:
        for sl_value in PI_SL:
            for rr in PI_RR:
                for age in PI_MAX_AGE:
                    i += 1
                    p = copy.deepcopy(base_params)
                    p.strategy = "pi"
                    p.tr_allowed_sessions = list(ALL_SESSIONS)
                    p.tr_one_trade_per_session = False
                    p.one_trade_per_session_direction = False
                    p.tr_exit_mode = "tp"
                    p.tr_daily_loss_stop = 1
                    p.trail_enabled = False
                    p.tr_trail_enabled = False
                    p.rr_ratio = int(rr)
                    p.factor_sl_rule = "atr_blend"
                    p.factor_tp_rule = "atr_blend"
                    p.factor_sl_value = float(sl_value)
                    p.factor_tp_value = float(sl_value) * float(rr)
                    p.factor_max_hold_bars = 0
                    p.factor_max_trades_per_day = 3
                    p.factor_warmup_bars = 150
                    # Explicit kinds beat pi_signal_set inside PiSignalStrategy;
                    # pi_long_only is a hard switch that would blank the shorts,
                    # so it has to follow the grid rather than the base preset.
                    p.pi_long_kinds = list(longs)
                    p.pi_short_kinds = list(shorts)
                    p.pi_long_only = not shorts
                    p.pi_max_signal_age_min = int(age)
                    r = _run_one(p, window, None)
                    r["params"] = {
                        "strategy": "pi",
                        "tr_allowed_sessions": list(ALL_SESSIONS),
                        "tr_exit_mode": "tp",
                        "tr_daily_loss_stop": 1,
                        "rr_ratio": int(rr),
                        "factor_sl_rule": "atr_blend",
                        "factor_tp_rule": "atr_blend",
                        "factor_sl_value": float(sl_value),
                        "factor_tp_value": float(sl_value) * float(rr),
                        "factor_max_hold_bars": 0,
                        "factor_max_trades_per_day": 3,
                        "factor_warmup_bars": 150,
                        "pi_long_kinds": list(longs),
                        "pi_short_kinds": list(shorts),
                        "pi_long_only": not shorts,
                        "pi_max_signal_age_min": int(age),
                    }
                    r["label"] = (
                        f"PI L[{'+'.join(longs) or '—'}] S[{'+'.join(shorts) or '—'}] "
                        f"SL{sl_value:g} RR{int(rr)} AGE{int(age)}"
                    )
                    results.append(r)
                    if progress_cb and (i % 8 == 0 or i == total):
                        progress_cb(i, total, "PI " + r["label"])
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
    day_total = len(DAY_ZONE_ENTRY) * len(DAY_ZONE_SL) * len(DAY_ZONE_TP_FRAC) * len(DAY_ZONE_STOP) + len(DAY_ZONE_STOP)
    dist_total = (
        len(DISTRIBUTION_WINDOWS) * len(DISTRIBUTION_METHODS) * len(DISTRIBUTION_ENTRY)
        * len(DISTRIBUTION_ACCEPT) * len(DISTRIBUTION_STOP_SPAN) * len(DISTRIBUTION_TARGET)
    )
    factor_total = len(FACTOR_GRID) + len(FACTOR_SESSION_VA_GRID)
    pi_total = len(PI_KIND_COMBOS) * len(PI_SL) * len(PI_RR) * len(PI_MAX_AGE)
    grand_total = day_total + dist_total + factor_total + pi_total
    done_offset = 0

    def _wrap(model: str, offset: int):
        def _progress(cur: int, total: int, detail: str) -> None:
            if progress_cb:
                progress_cb(offset + cur, grand_total, f"{model}: {detail}")
        return _progress

    import gc
    out: List[dict] = []
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
    done_offset += factor_total
    gc.collect()
    if _on("PI"):
        out.extend(run_pi_sweep(candles, base_params, _wrap("PI", done_offset)))
    _annotate_plateau_and_acceptance(out)
    gc.collect()
    # 1.0.9 G5:最後一道關卡 —— 已 accept 的變體必須在另一個商品上也站得住
    try:
        cross_symbol_validate(out, base_params, progress_cb)
    except Exception as exc:
        logger.warning("[G5] 跨商品驗證跳過: %s: %s", type(exc).__name__, exc)
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
