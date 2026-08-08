# ============================================================

# 文件: backend/api/routes.py
# 狀態: v1.0.6
# 功能 / Features:
#   - FastAPI REST routes for config, historical candles, backtest,
#     live engine, presets, and trade history.
#   - 1.0.8: 移除 ML sweep / conf-combo / ml_consolidation_v2 相關端點與機制。
#   - Presets preserve both trend and confluence strategy parameters.
#   - Value Area is locked to 80%; live/latest-candle routes use completed 1m bars.
# ============================================================

from __future__ import annotations
import os
import csv
import json
import logging
import math
import asyncio
import threading
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional, Tuple

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.db.models import (
    current_quarterly_contract_id, normalize_contract_id_to_front,  # 1.0.8: 自動換月
    BacktestConfig, BarUnit, Candle, Direction, StrategyParams,
    get_point_value, get_contract_label, get_tick_size,
    get_commission_rt, get_fees_rt, _extract_symbol,
)
from backend.backtest.engine import BacktestEngine
from backend.data.candle_store import (
    load as _store_load, save as _store_save, merge as _store_merge,
    detect_gaps as _store_detect_gaps, advance_frozen as _store_advance_frozen,
    last_complete_day_end as _store_last_complete_day_end,
    _as_utc as _store_utc,
)
from backend.strategy.volume_profile import VolumeProfileCalculator
from backend.strategy.session_filter import (
    DEFAULT_ALLOWED_SESSIONS, allowed_sessions_label, normalize_allowed_sessions,
)
from backend.strategy.factor import (
    FACTOR_EMAPMO_HISTORY_BARS,
    calculate_emapmo_snapshot,
)
from backend.live.warmup import signal_warmup_progress

# 1.0.10: StrategyParams 的欄位預設 = 參數預設值的唯一真相。
# routes 建 StrategyParams 時一律從這裡取 fallback,不要各自寫死
# (見 docs/INVARIANTS.md CONFIG-002)。
_PARAM_DEFAULTS = StrategyParams()
# 1.0.8: 移除 ml_trend / ml_consolidation_v2 (mlc2) 相關 import
#        (MLTrendBacktester / MLTrendBacktestConfig / precompute_vp_timeline / MLTrendConfig)
#        mlc2 策略已整批刪除,僅保留 trend + confluence。

logger = logging.getLogger(__name__)
router = APIRouter()


def _env(key: str, default: str = "") -> str:
    """讀取 .env 環境變數"""
    return os.getenv(key, default)


MNQ_SIZE_CHOICES = (1, 2, 3, 5, 10)  # 1.0.8: sizing choices
TRAIL_TICK_STEP = 5
ML_TRAIL_PCT_CHOICES = (
    0.05, 0.10, 0.20, 0.30, 0.40, 0.50,
)


def _normalize_contract_size(contract_id: str, requested) -> int:
    """Enforce the UI/API size contract: MNQ=1/3/5/10, NQ=1."""
    sym = _extract_symbol(contract_id)
    if sym in ("NQ", "ENQ"):
        return 1
    try:
        size = int(requested or 1)
    except (TypeError, ValueError):
        size = 3 if sym == "MNQ" else 1
    if sym == "MNQ":
        return size if size in MNQ_SIZE_CHOICES else 3
    return max(1, size)


def _normalize_trade_ticks(value, default: int) -> int:
    try:
        ticks = int(value or default)
    except (TypeError, ValueError):
        ticks = default
    return max(50, min(200, ticks))


def _normalize_value_area_pct(value=None, default: float = 0.80) -> float:
    """1.0.8: 開放 70%/80% 兩檔 VA(70% = 較窄邊界);
    其餘任何值一律吸附到最近的允許檔,無法解析則回 default(80%)。"""
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return default
    if pct > 1:
        pct = pct / 100.0
    allowed = (0.70, 0.80)
    return min(allowed, key=lambda x: abs(x - pct))


def _normalize_trail_trigger_pct(value) -> float:
    try:
        pct = float(value)
    except (TypeError, ValueError):
        pct = 0.30
    if pct > 1:
        pct = pct / 100.0
    allowed = (0.0, 0.30, 0.50, 0.70)
    return min(allowed, key=lambda x: abs(x - pct))


def _normalize_trail_pct(value) -> Optional[float]:
    if value is None:
        return None
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return None
    if abs(pct) > 1:
        pct = pct / 100.0
    return max(0.05, min(0.50, pct))


def _floor_ticks_to_step(ticks: float, step: int = TRAIL_TICK_STEP) -> int:
    try:
        n = abs(float(ticks))
    except (TypeError, ValueError):
        return 0
    if step <= 1:
        return int(n)
    return int(n // step) * step


def _trail_max_profit_ticks(tp_ticks, trigger_pct) -> int:
    try:
        tp = abs(int(tp_ticks or 0))
    except (TypeError, ValueError):
        tp = 0
    trigger = _normalize_trail_trigger_pct(trigger_pct)
    if trigger <= 0:
        return 0
    trigger_ticks = _floor_ticks_to_step(tp * trigger)
    return max(0, trigger_ticks - TRAIL_TICK_STEP)


def _clamp_trail_ticks(trail, sl_ticks, tp_ticks, trigger_pct: Optional[float] = None) -> int:
    try:
        t = int(trail or 0)
    except (TypeError, ValueError):
        t = 5
    try:
        sl = abs(int(sl_ticks or 0))
        tp = abs(int(tp_ticks or 0))
    except (TypeError, ValueError):
        sl, tp = 50, 150
    hi = tp
    if trigger_pct is not None:
        hi = min(hi, _trail_max_profit_ticks(tp, trigger_pct))
    return max(0, min(hi, t))


def _trail_ticks_from_pct(trail_pct, sl_ticks, tp_ticks, trigger_pct: Optional[float] = None) -> int:
    pct = _normalize_trail_pct(trail_pct)
    if pct is None:
        pct = 0.05
    if trigger_pct is not None and _normalize_trail_trigger_pct(trigger_pct) <= 0:
        return 0
    try:
        sl = abs(int(sl_ticks or 0))
        tp = abs(int(tp_ticks or 0))
    except (TypeError, ValueError):
        sl, tp = 50, 150

    ticks = _floor_ticks_to_step(tp * pct)
    return _clamp_trail_ticks(ticks, sl, tp, trigger_pct)


def _resolve_trail_ticks(trail_ticks, trail_pct, sl_ticks, tp_ticks, trigger_pct) -> int:
    pct = _normalize_trail_pct(trail_pct)
    if pct is not None:
        return _trail_ticks_from_pct(pct, sl_ticks, tp_ticks, trigger_pct)
    return _clamp_trail_ticks(trail_ticks, sl_ticks, tp_ticks, trigger_pct)


def _trail_grid_for(sl_ticks: int, tp_ticks: int, trigger_pct: float) -> List[Tuple[int, Optional[float]]]:
    trigger = _normalize_trail_trigger_pct(trigger_pct)
    if trigger <= 0:
        return [(0, None)]
    ticks_to_pct: Dict[int, float] = {}
    pct_values = {
        pct for pct in ML_TRAIL_PCT_CHOICES
        if pct <= 0.50 and pct < trigger - 1e-9
    }
    for pct in sorted(pct_values):
        ticks = _trail_ticks_from_pct(pct, sl_ticks, tp_ticks, trigger_pct)
        prev = ticks_to_pct.get(ticks)
        if prev is None or abs(pct) < abs(prev):
            ticks_to_pct[ticks] = pct
    return sorted((ticks, pct) for ticks, pct in ticks_to_pct.items())


# 1.0.9: 需要「共識 zone timeline」的策略白名單。TREND 移除後目前為空 ——
# 建 timeline 是數十秒~數分鐘的 detector 全掃,不需要就別建。
_ZONE_TIMELINE_STRATEGIES: frozenset = frozenset()


def _normalize_strategy_name(value: str) -> str:
    # 1.0.8: +fade(前日 VA 回歸);confluence 由呼叫端先行判斷
    v = str(value or "").strip().lower()
    # 1.0.9: 改名相容 —— 舊 preset 存的是 intramom / sessfib
    v = {"intramom": "momentum", "claudefib": "momentum", "sessfib": "betafib"}.get(v, v)
    # 1.0.9: TREND 已移除 —— 未知值一律落到 factor
    return v if v in ("fade", "sigma", "factor", "momentum", "betafib", "pi") else "factor"



# 1.0.10: TopstepX 的 1m 歷史保留期(天)。超過這個範圍的破洞不該向券商重抓 ——
# 那段歷史是 Databento 補的,券商本來就沒有。
BROKER_HISTORY_DAYS = 60

AREA_TIMEFRAME_CHOICES = ("15m", "30m", "1h", "4h", "session")  # 1.0.8: +session 生長區間


def _normalize_area_timeframe(value) -> str:
    tf = str(value or "15m").strip().lower()
    return tf if tf in AREA_TIMEFRAME_CHOICES else "15m"


def _normalize_factor_family(value) -> str:
    v = str(value or "emapmo").strip().lower()
    aliases = {
        "pmo": "emapmo",
        "ema_pmo": "emapmo",
        "mrev": "momentum_reversion",
        "kdjma": "icefishball",
        "ifb": "icefishball",
    }
    v = aliases.get(v, v)
    return v if v in ("emapmo", "momentum_reversion", "icefishball") else "emapmo"


def _normalize_factor_side(value) -> str:
    v = str(value or "all").strip().lower()
    return v if v in ("all", "long_only", "short_only") else "all"


def _normalize_factor_rule(value) -> str:
    v = str(value or "atr").strip().lower()
    aliases = {
        "ticks": "trend_ticks",
        "trend": "trend_ticks",
        "trend_sl": "trend_ticks",
        "rr": "trend_rr",
        "trend_tp": "trend_rr",
    }
    v = aliases.get(v, v)
    return v if v in ("fixed", "atr", "atr_blend", "range15_pct", "trend_ticks", "trend_rr") else "atr"


def _normalize_factor_session_va_filter(value) -> str:
    v = str(value or "off").strip().lower()
    return "outside" if v in ("outside", "outside_va", "session_outside", "va_outside") else "off"


def _normalize_factor_pmo_mode(value) -> str:
    v = str(value or "normal").strip().lower()
    return v if v in ("normal", "early", "both") else "normal"


# 1.0.8: 移除重複的 _normalize_value_area_pct clamp 版本(原在此)。
# 原本此處的 clamp(0.50-0.95) 版會 shadow 上面 line 85 的 return-0.80 版,
# 導致「鎖 80」實際失效。刪除後全域統一由 line 85 的 lock-80 版本提供。


def _normalize_rr_ratio(value, default: int = 2) -> int:
    """Reward:risk multiple, selectable 1..6."""
    try:
        rr = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    return max(1, min(6, rr))


def _normalize_conf_rr(value, default: float = 1.0) -> float:
    """Fixed confluence RR supports fractional market-entry tuning, 1.0..6.0."""
    try:
        rr = float(value)
    except (TypeError, ValueError):
        rr = float(default)
    return round(max(1.0, min(6.0, rr)), 2)


def _normalize_conf_sl_reference_tf(value) -> str:
    return "smallest" if str(value or "").strip().lower() == "smallest" else "largest"


def _normalize_tr_overlap_trade_tf(value) -> str:
    return "smallest" if str(value or "").strip().lower() == "smallest" else "merged"


def _strategy_leg_params(req, prefix: str) -> dict:
    tp_raw = getattr(req, f"{prefix}_tp_ticks", None)
    sl_raw = getattr(req, f"{prefix}_sl_ticks", None)
    trigger_raw = getattr(req, f"{prefix}_trail_trigger_pct", None)
    trail_raw = getattr(req, f"{prefix}_trail_sl_ticks", None)
    trail_pct_raw = getattr(req, f"{prefix}_trail_sl_pct", None)
    enabled_raw = getattr(req, f"{prefix}_trail_enabled", None)
    lock_raw = getattr(req, f"{prefix}_full_tp_lock", None)

    tp_ticks = _normalize_trade_ticks(
        tp_raw if tp_raw is not None else getattr(req, "tp_ticks", 200),
        200,
    )
    sl_ticks = _normalize_trade_ticks(
        sl_raw if sl_raw is not None else getattr(req, "sl_ticks", 50),
        50,
    )
    trigger_pct = _normalize_trail_trigger_pct(
        trigger_raw if trigger_raw is not None else getattr(req, "trail_trigger_pct", 0.30)
    )
    trail_pct = trail_pct_raw if trail_pct_raw is not None else getattr(req, "trail_sl_pct", None)
    trail_ticks = _resolve_trail_ticks(
        trail_raw if trail_raw is not None else getattr(req, "trail_sl_ticks", None),
        trail_pct,
        sl_ticks,
        tp_ticks,
        trigger_pct,
    )
    enabled = bool(
        enabled_raw if enabled_raw is not None else getattr(req, "trail_enabled", True)
    ) and trigger_pct > 0
    try:
        full_tp_lock = int(lock_raw if lock_raw is not None else getattr(req, "full_tp_lock", 0) or 0)
    except (TypeError, ValueError):
        full_tp_lock = 0

    return {
        "tp_ticks": tp_ticks,
        "sl_ticks": sl_ticks,
        "trail_sl_ticks": trail_ticks,
        "trail_trigger_pct": trigger_pct,
        "trail_enabled": enabled,
        "full_tp_lock": max(0, min(3, full_tp_lock)),
    }


def _conf_ev_floor_opt(val):
    """Blank/None/non-numeric → None (legacy win-prob gate); number → EV floor."""
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _conf_rr_grid_opt(val):
    """Variable RR is retired; production always uses a fixed RR."""
    return None


def _conf_allowed_sessions_list(val) -> Optional[List[str]]:
    allowed = normalize_allowed_sessions(val)
    return list(allowed) if allowed is not None else None


def _parse_iso_utc(v) -> Optional[datetime]:
    """1.0.10: 解析 "2026-06-01T00:00:00Z" 這類字串成 aware UTC。
    解析失敗回 None(呼叫端會當成「不限制」),不讓格式問題炸掉整個抓取。"""
    if not v:
        return None
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _betafib_hour(val) -> Optional[int]:
    """1.0.10: 進場時窗的 UTC 小時。空字串 / None / 非法值 = 不限制。

    UI 用 select 傳字串,空值代表「整個夜盤」(原本的行為),不能被 int("")
    炸掉,也不能被 `or None` 把合法的 0 點吃掉。
    """
    if val is None or val == "":
        return None
    try:
        h = int(val)
    except (TypeError, ValueError):
        return None
    return h if 0 <= h <= 23 else None


def _build_strategy_params_from_request(req, contract_size: int) -> StrategyParams:
    # v1.0.6: "confluence" selects the explainable ML engine; anything else is trend.
    raw_strat = str(getattr(req, "strategy", "factor") or "").strip().lower()
    # 1.0.8: mlc2 (ml_consolidation_v2) 已移除;僅 confluence / trend。
    strategy = _normalize_strategy_name(raw_strat)
    tr = _strategy_leg_params(req, "tr")
    return StrategyParams(
        strategy=strategy,
        conf_band_ticks=float(getattr(req, "conf_band_ticks", 4.0) or 4.0),
        conf_min_distinct_tf=int(getattr(req, "conf_min_distinct_tf", 2) or 2),
        conf_rr=_normalize_conf_rr(getattr(req, "conf_rr", 1.0), 1.0),
        conf_wait_minutes=int(getattr(req, "conf_wait_minutes", 1) or 1),
        conf_base_minutes=int(getattr(req, "conf_base_minutes", 1) or 1),
        conf_min_prob=float(getattr(req, "conf_min_prob", 0.65) or 0.0),
        conf_ev_floor=_conf_ev_floor_opt(getattr(req, "conf_ev_floor", None)),
        conf_rr_grid=None,
        conf_use_scorer=bool(getattr(req, "conf_use_scorer", True)),
        conf_enable_breakout=bool(getattr(req, "conf_enable_breakout", False)),
        conf_max_risk_ticks=getattr(req, "conf_max_risk_ticks", None),
        max_profit_ticks=getattr(req, "max_profit_ticks", None),
        conf_sl_reference_tf=_normalize_conf_sl_reference_tf(
            getattr(req, "conf_sl_reference_tf", "largest")
        ),
        conf_allowed_sessions=_conf_allowed_sessions_list(
            getattr(req, "conf_allowed_sessions", DEFAULT_ALLOWED_SESSIONS)
        ),
        conf_trail_trigger_pct=float(getattr(req, "conf_trail_trigger_pct", 0.50) or 0.0),
        conf_trail_lock_pct=float(getattr(req, "conf_trail_lock_pct", 0.05) or 0.0),
        conf_full_tp_lock=int(getattr(req, "conf_full_tp_lock", 0) or 0),
        conf_session_limit=bool(getattr(req, "conf_session_limit", True)),
        conf_shadow=bool(getattr(req, "conf_shadow", False)),
        # 1.0.8: 移除 mlc2_* 參數(ml_consolidation_v2 策略已刪除)
        tp_ticks=tr["tp_ticks"],
        sl_ticks=tr["sl_ticks"],
        trail_sl_ticks=tr["trail_sl_ticks"],
        trail_trigger_pct=tr["trail_trigger_pct"],
        trail_enabled=tr["trail_enabled"],
        tr_tp_ticks=tr["tp_ticks"],
        tr_sl_ticks=tr["sl_ticks"],
        tr_trail_sl_ticks=tr["trail_sl_ticks"],
        tr_trail_trigger_pct=tr["trail_trigger_pct"],
        tr_trail_enabled=tr["trail_enabled"],
        tr_full_tp_lock=tr["full_tp_lock"],
        tr_allowed_sessions=_conf_allowed_sessions_list(
            getattr(req, "tr_allowed_sessions", DEFAULT_ALLOWED_SESSIONS)
        ),
        candle_seconds=60,
        contract_id=normalize_contract_id_to_front(getattr(req, "contract_id", "") or current_quarterly_contract_id("MNQ")),  # 1.0.8: 自動換月
        contract_size=contract_size,
        full_tp_lock=tr["full_tp_lock"],
        one_trade_per_session_direction=bool(getattr(req, "one_trade_per_session_direction", True)),
        tr_one_trade_per_session=bool(getattr(req, "tr_one_trade_per_session", True)),
        skip_zone_stability=False,
        breakout_confirm_bars=max(1, int(getattr(req, "breakout_confirm_bars", 7) or 7)),
        # 1.0.8: ladder 出場 + 日虧斷路器
        tr_exit_mode=(
            "ladder"
            if str(getattr(req, "tr_exit_mode", "tp") or "tp").lower() == "ladder"
            else "tp"
        ),
        tr_daily_loss_stop=max(0, min(9, int(getattr(req, "tr_daily_loss_stop", 0) or 0))),
        tr_daily_win_stop=max(0, min(9, int(getattr(req, "tr_daily_win_stop", 0) or 0))),  # 1.0.9
        # 1.0.9: PDPT 夾在 0–20000;超過 $20k 的單日目標對任何 Topstep 帳戶都無意義
        tr_daily_profit_stop=max(0.0, min(20000.0, float(
            getattr(req, "tr_daily_profit_stop", 0) or 0))),
        # 1.0.9: prevRV regime gate + fade 專用
        fade_tp_frac=float(getattr(req, "fade_tp_frac", 0.75) or 0.75),
        fade_entry_mode=(lambda m: m if m in ("limit", "rejection", "or15") else "limit")(str(getattr(req, "fade_entry_mode", "limit") or "limit").lower()),  # 1.0.9: +or15
        sigma_window_minutes=max(5, int(getattr(req, "sigma_window_minutes", 15) or 15)),
        sigma_method=(
            "mad" if str(getattr(req, "sigma_method", "std") or "std").lower() == "mad" else "std"
        ),
        sigma_entry_mode=(
            "reject" if str(getattr(req, "sigma_entry_mode", "blind") or "blind").lower() == "reject" else "blind"
        ),
        sigma_accept_mode=(
            str(getattr(req, "sigma_accept_mode", "none") or "none").lower()
            if str(getattr(req, "sigma_accept_mode", "none") or "none").lower() in ("none", "filter", "switch")
            else "none"
        ),
        sigma_start=max(0.5, float(getattr(req, "sigma_start", 1.0) or 1.0)),
        sigma_max=max(1.0, float(getattr(req, "sigma_max", 3.0) or 3.0)),
        sigma_target_mode=(
            str(getattr(req, "sigma_target_mode", "half") or "half").lower()
            if str(getattr(req, "sigma_target_mode", "half") or "half").lower() in ("inner1", "half", "center")
            else "half"
        ),
        sigma_stop_span=max(0.25, float(getattr(req, "sigma_stop_span", 1.0) or 1.0)),
        sigma_accept_sigma=max(1.0, float(getattr(req, "sigma_accept_sigma", 2.0) or 2.0)),
        sigma_accept_bars=max(1, int(getattr(req, "sigma_accept_bars", 2) or 2)),
        # 1.0.9: HOLD 5m-candle system removed — exits are SL/TP only, always,
        # for every current and future preset. Forced 0 here (authoritative for
        # both backtest and live) so no stored/incoming value can re-enable it.
        factor_timeframe_minutes=max(1, int(getattr(req, "factor_timeframe_minutes", 5) or 5)),
        factor_signal_family=_normalize_factor_family(getattr(req, "factor_signal_family", "emapmo")),
        factor_side_mode=_normalize_factor_side(getattr(req, "factor_side_mode", "all")),
        factor_pmo_signal_mode=_normalize_factor_pmo_mode(getattr(req, "factor_pmo_signal_mode", "normal")),
        factor_session_va_filter=_normalize_factor_session_va_filter(getattr(req, "factor_session_va_filter", "off")),
        factor_sl_rule=_normalize_factor_rule(getattr(req, "factor_sl_rule", "atr")),
        factor_tp_rule=_normalize_factor_rule(getattr(req, "factor_tp_rule", "atr")),
        factor_sl_value=max(0.01, float(getattr(req, "factor_sl_value", 1.5) or 1.5)),
        factor_tp_value=max(0.01, float(getattr(req, "factor_tp_value", 2.0) or 2.0)),
        # 1.0.9: HOLD 5m-candle system removed — FACTOR exits are SL/TP only. See pmo note above.
        factor_max_hold_bars=0,
        factor_max_trades_per_day=max(0, int(getattr(req, "factor_max_trades_per_day", 3) or 0)),
        factor_warmup_bars=max(20, int(getattr(req, "factor_warmup_bars", 320) or 320)),
        factor_pmo_threshold_scale=abs(float(
            getattr(req, "factor_pmo_threshold_scale", 1.0) or 1.0)),
        factor_pmo_normal_scale=abs(float(
            getattr(req, "factor_pmo_normal_scale", 0) or 0)),
        factor_pmo_adaptive_window=max(0, int(
            getattr(req, "factor_pmo_adaptive_window", 0) or 0)),
        factor_pmo_early_scale=abs(float(
            getattr(req, "factor_pmo_early_scale", 0) or 0)),
        momentum_first_minutes=max(5, int(
            getattr(req, "momentum_first_minutes", 30) or 30)),
        momentum_entry_hour=int(
            getattr(req, "momentum_entry_hour", 18) or 18),
        # 1.0.9 SESSFIB。entry_fib 夾在 0.1–0.95:低於 0.1 等於等崩盤,
        # 高於 0.95 幾乎每晚觸價(0.786 實測就已經是 94%),兩端都無意義。
        betafib_entry_fib=min(0.95, max(0.10, float(
            getattr(req, "betafib_entry_fib", 0.618) or 0.618))),
        betafib_anchor=("oc" if str(
            getattr(req, "betafib_anchor", "hl") or "hl").lower() == "oc" else "hl"),
        betafib_risk_basis=(str(
            getattr(req, "betafib_risk_basis", "atr_blend") or "atr_blend").lower()
            if str(getattr(req, "betafib_risk_basis", "atr_blend") or "").lower()
            in ("atr_blend", "daily", "fib") else "atr_blend"),
        betafib_min_move_pct=max(0.0, float(
            getattr(req, "betafib_min_move_pct", 0.0) or 0.0)),
        # 1.0.10: 腿幅上限(0 = 無上限)、進場時窗、fib 基準的 SL/TP 層級
        betafib_max_move_pct=max(0.0, float(
            getattr(req, "betafib_max_move_pct", 0.0) or 0.0)),
        betafib_entry_start_hour=_betafib_hour(
            getattr(req, "betafib_entry_start_hour", None)),
        betafib_entry_end_hour=_betafib_hour(
            getattr(req, "betafib_entry_end_hour", None)),
        # 1.0.10 BUG FIX:這裡原本自己寫死 fallback(pi_long_only=False /
        # pi_signal_set="pi_only")。那是 StrategyParams 之外的**第三份真相** ——
        # 改了 dataclass 預設(1.0.10:濾除開盤重播後空方轉為淨虧,預設只做多)
        # 卻沒改這裡,任何沒帶這兩個欄位的 API 請求就還是會把做空打開。
        # 現在一律回退到 dataclass 的預設,不再各寫一份。
        pi_long_only=bool(getattr(req, "pi_long_only", _PARAM_DEFAULTS.pi_long_only)),
        pi_signal_set=str(getattr(req, "pi_signal_set", None)
                          or _PARAM_DEFAULTS.pi_signal_set).lower(),
        pi_max_signal_age_min=max(1, min(60, int(
            getattr(req, "pi_max_signal_age_min", None)
            or _PARAM_DEFAULTS.pi_max_signal_age_min))),
        pi_short_sl_value=max(0.1, float(
            getattr(req, "pi_short_sl_value", None)
            or _PARAM_DEFAULTS.pi_short_sl_value)),
        pi_short_hold_min=max(0, int(
            getattr(req, "pi_short_hold_min", None)
            or _PARAM_DEFAULTS.pi_short_hold_min or 0)),
        betafib_sl_fib=min(1.5, max(-0.5, float(
            getattr(req, "betafib_sl_fib", 0.75) or 0.75))),
        betafib_tp_fib=min(1.5, max(-0.5, float(
            getattr(req, "betafib_tp_fib", 0.90) or 0.90))),
        area_timeframe=_normalize_area_timeframe(getattr(req, "area_timeframe", "15m")),
        value_area_pct=_normalize_value_area_pct(getattr(req, "value_area_pct", 0.80)),
        rr_ratio=_normalize_rr_ratio(getattr(req, "rr_ratio", 2)),
        method=str(getattr(req, "method", "single") or "single").lower(),
        tf_combo=[t for t in (getattr(req, "tf_combo", None) or []) if t in ML_TIMEFRAMES],
        tr_overlap_trade_tf=_normalize_tr_overlap_trade_tf(
            getattr(req, "tr_overlap_trade_tf", "merged")
        ),
    )

# ── 臨時存儲（後續改用 SQLite）──────────────────────────
_backtest_results = []
_historical_candles: List[Candle] = []
_topstepx_client = None  # TopstepXClient instance (set after connect)
_live_contract_id = "CON.F.US.ENQ.M26"  # Set after connect
_candle_cache = {"data": None, "time": 0}  # Cache for latest-candles (avoid API spam)
ML_DISPLAY_LIMIT = 200

def _candle_time(c: Candle) -> datetime:
    ts = c.timestamp
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _candle_key(c: Candle) -> str:
    return _candle_time(c).isoformat()


def _dedupe_candles(candles: List[Candle]) -> List[Candle]:
    """Deduplicate candles by timestamp, preserving later list entries on overlap."""
    by_ts: Dict[str, Candle] = {}
    for c in candles:
        by_ts[_candle_key(c)] = c
    return sorted(by_ts.values(), key=_candle_time)


def _shift_candle(c: Candle, offset: float) -> Candle:
    if abs(offset) < 1e-9:
        return c
    return Candle(
        timestamp=c.timestamp,
        open=c.open + offset,
        high=c.high + offset,
        low=c.low + offset,
        close=c.close + offset,
        volume=c.volume,
        symbol=c.symbol,
        interval=c.interval,
    )


def _build_continuous_candles(
    contract_batches: Dict[str, List[Candle]],
    fetch_contracts: List[str],
    roll_at: Optional[datetime],
) -> Tuple[List[Candle], Dict[str, Any]]:
    """Back-adjust previous contract history and splice it at the rollover date.

    The current front-month is the price anchor. Older candles are shifted by the
    close-to-close difference at the nearest shared timestamp around roll_at.
    """
    raw = []
    for cid in fetch_contracts:
        raw.extend(contract_batches.get(cid, []))

    meta: Dict[str, Any] = {
        "roll_at": roll_at.isoformat() if roll_at else None,
        "price_adjustment": 0.0,
        "adjusted_contract": None,
        "anchor_time": None,
    }
    if len(fetch_contracts) < 2 or not roll_at:
        return _dedupe_candles(raw), meta

    prev_id = fetch_contracts[-2]
    current_id = fetch_contracts[-1]
    prev_bars = sorted(contract_batches.get(prev_id, []), key=_candle_time)
    current_bars = sorted(contract_batches.get(current_id, []), key=_candle_time)
    if not prev_bars or not current_bars:
        return _dedupe_candles(raw), meta

    prev_by_ts = {_candle_key(c): c for c in prev_bars}
    current_by_ts = {_candle_key(c): c for c in current_bars}
    common_keys = sorted(set(prev_by_ts).intersection(current_by_ts), key=lambda k: _candle_time(prev_by_ts[k]))
    roll_ts = roll_at.astimezone(timezone.utc) if roll_at.tzinfo else roll_at.replace(tzinfo=timezone.utc)

    anchor_key = None
    if common_keys:
        after_roll = [k for k in common_keys if _candle_time(prev_by_ts[k]) >= roll_ts]
        anchor_key = after_roll[0] if after_roll else common_keys[-1]

    adjustment = 0.0
    if anchor_key:
        prev_anchor = prev_by_ts[anchor_key]
        current_anchor = current_by_ts[anchor_key]
        adjustment = current_anchor.close - prev_anchor.close
        meta.update({
            "price_adjustment": adjustment,
            "adjusted_contract": prev_id,
            "anchor_time": _candle_time(prev_anchor).isoformat(),
        })

    adjusted_prev = [
        _shift_candle(c, adjustment)
        for c in prev_bars
        if _candle_time(c) < roll_ts
    ]
    current_after_roll = [
        c for c in current_bars
        if _candle_time(c) >= roll_ts
    ]

    # If the requested range is entirely before/after the roll date, one side may
    # be empty. That is expected; the active side still becomes the continuous set.
    return _dedupe_candles(adjusted_prev + current_after_roll), meta


def _upsert_historical_candles(candles: List[Candle]) -> None:
    """Merge candles by timestamp so forming-bar snapshots get replaced."""
    global _historical_candles
    if not candles:
        return
    by_ts = {_candle_key(c): c for c in _historical_candles}
    for c in candles:
        by_ts[_candle_key(c)] = c
    _historical_candles = sorted(by_ts.values(), key=_candle_time)


def _ema_series(values: List[Optional[float]], span: int) -> List[Optional[float]]:
    alpha = 2.0 / (float(span) + 1.0)
    out: List[Optional[float]] = []
    prev: Optional[float] = None
    for value in values:
        if value is None or not math.isfinite(float(value)):
            out.append(prev)
            continue
        v = float(value)
        prev = v if prev is None else alpha * v + (1.0 - alpha) * prev
        out.append(prev)
    return out


def _rma_series(values: List[Optional[float]], length: int) -> List[float]:
    alpha = 1.0 / float(length)
    out: List[float] = []
    prev = 0.0
    seeded = False
    for value in values:
        v = 0.0 if value is None or not math.isfinite(float(value)) else float(value)
        if not seeded:
            prev = v
            seeded = True
        else:
            prev = alpha * v + (1.0 - alpha) * prev
        out.append(prev)
    return out


def _bcwsma_series(values: List[Optional[float]], length: int, multiplier: int) -> List[float]:
    out: List[float] = []
    prev = 0.0
    for value in values:
        raw = 0.0 if value is None or not math.isfinite(float(value)) else float(value)
        prev = (multiplier * raw + (length - multiplier) * prev) / float(length)
        out.append(prev)
    return out


def _atr_series(bars: List[Candle], length: int = 14, min_periods: int = 7) -> List[Optional[float]]:
    trs: List[float] = []
    atrs: List[Optional[float]] = []
    for i, cur in enumerate(bars):
        prev_close = float(bars[i - 1].close) if i > 0 else float(cur.close)
        tr = max(
            float(cur.high) - float(cur.low),
            abs(float(cur.high) - prev_close),
            abs(float(cur.low) - prev_close),
        )
        trs.append(tr)
        start = max(0, len(trs) - length)
        window = trs[start:]
        atrs.append((sum(window) / len(window)) if len(window) >= min_periods else None)
    return atrs


def _signal_marker(
    *,
    marker_time: datetime,
    signal_type: str,
    subtype: str,
    direction: str,
    price: float,
    source_time: datetime,
    detail: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "time": marker_time.isoformat(),
        "source_time": source_time.isoformat(),
        "type": signal_type,
        "subtype": subtype,
        "direction": direction,
        "price": round(float(price), 4),
        "detail": detail,
    }


def _collect_pmo_markers(bars: List[Candle], cutoff: Optional[datetime]) -> List[Dict[str, Any]]:
    """Collect EMAPMO markers with the same bounded EMA seed as FACTOR.

    FACTOR live/backtest retains a rolling completed-5m deque.  Calculating the
    overlay once across the entire chart gives the EMA a different seed and can
    draw a threshold signal that the trading strategy never saw.
    """
    closes = [float(c.close) for c in bars]

    markers: List[Dict[str, Any]] = []
    for i in range(149, len(bars) - 1):
        start = max(0, i + 1 - FACTOR_EMAPMO_HISTORY_BARS)
        snapshot = calculate_emapmo_snapshot(closes[start:i + 1])
        p1 = snapshot.get("pmo")
        s1 = snapshot.get("signal")
        if p1 is None or s1 is None:
            continue

        subtypes: List[str] = []
        direction = ""
        if snapshot["normal_short"]:
            subtypes.append("normal")
            direction = "short"
        if snapshot["normal_long"]:
            subtypes.append("normal")
            direction = "long"
        if snapshot["early_short"]:
            subtypes.append("early")
            direction = direction or "short"
        if snapshot["early_long"]:
            subtypes.append("early")
            direction = direction or "long"

        if not subtypes:
            continue
        entry_bar = bars[i + 1]
        entry_time = _candle_time(entry_bar)
        if cutoff and entry_time < cutoff:
            continue
        markers.append(_signal_marker(
            marker_time=entry_time,
            signal_type="emapmo",
            subtype="+".join(dict.fromkeys(subtypes)),
            direction=direction,
            price=float(entry_bar.open),
            source_time=_candle_time(bars[i]),
            detail={"pmo": round(float(p1), 5), "signal": round(float(s1), 5)},
        ))
    return markers



def _collect_betafib_levels(bars: List[Candle], cutoff: Optional[datetime],
                            entry_fib: float = 0.618,
                            anchor: str = "hl",
                            min_move_pct: float = 0.0) -> List[Dict[str, Any]]:
    """SESSFIB 疊圖 —— 每個 session day 的 fib 掛單位,畫成夜盤區間的水平線。

    與 research_lab.BetaFibRetrace 相同的推動腿定義:
      session day 以 RTH 開盤(13:30 UTC)為界 —— 夜盤等單會跨過 UTC 午夜,
      用日曆日切會把掛單價在半夜清掉(那是 1.0.9 修掉的 bug)。
      上漲日推動腿 = 「最高點之前」的最低點 → 最高點;下跌日鏡像。

    回傳每晚一筆,含 anchor0 / anchor1 / 掛單價與夜盤時間範圍,
    讓前端在 20:00 UTC → 隔日 13:30 UTC 這段畫水平線。
    """
    RTH_OPEN, RTH_CLOSE = (13, 30), (20, 0)

    def _sday(ts: datetime):
        d = ts.date()
        return d - timedelta(days=1) if (ts.hour, ts.minute) < RTH_OPEN else d

    days: Dict[Any, Dict[str, Any]] = {}
    for bar in bars:
        ts = _candle_time(bar)
        hm = (ts.hour, ts.minute)
        d = _sday(ts)
        slot = days.setdefault(d, {"rth": [], "night": []})
        slot["rth" if RTH_OPEN <= hm < RTH_CLOSE else "night"].append(bar)

    out: List[Dict[str, Any]] = []
    for d in sorted(days):
        rth, night = days[d]["rth"], days[d]["night"]
        if len(rth) < 40 or not night:      # 5m bar:RTH 完整約 78 根
            continue
        up = float(rth[-1].close) > float(rth[0].open)
        if anchor == "hl":
            if up:
                hi_i = max(range(len(rth)), key=lambda i: float(rth[i].high))
                a1 = float(rth[hi_i].high)
                a0 = min(float(k.low) for k in rth[:hi_i + 1])
            else:
                lo_i = min(range(len(rth)), key=lambda i: float(rth[i].low))
                a1 = float(rth[lo_i].low)
                a0 = max(float(k.high) for k in rth[:lo_i + 1])
        else:
            a0, a1 = float(rth[0].open), float(rth[-1].close)
        move = a1 - a0
        if move == 0 or a0 <= 0:
            continue
        if min_move_pct > 0 and abs(move) / a0 * 100.0 < min_move_pct:
            continue
        t_to = _candle_time(night[-1])
        if cutoff and t_to < cutoff:
            continue
        out.append({
            "day": str(d),
            "t_from": _candle_time(night[0]).isoformat(),
            "t_to": t_to.isoformat(),
            "anchor0": round(a0, 2),
            "anchor1": round(a1, 2),
            "entry_fib": round(float(entry_fib), 3),
            "level": round(a0 + float(entry_fib) * move, 2),
            "direction": "long" if move > 0 else "short",
            "move_pct": round(abs(move) / a0 * 100.0, 2),
        })
    return out


def _collect_momentum_markers(bars: List[Candle], cutoff: Optional[datetime],
                              first_minutes: int = 30,
                              entry_hour: int = 18) -> List[Dict[str, Any]]:
    """INTRAMOM 疊圖 —— 與 research_lab.MomentumContinuation 相同的判定。

    交易日(Topstep 邊界 22:00 UTC)開始後 first_minutes 分鐘的報酬方向 →
    同向進場。實測進場時刻絕大多數是 22:30 UTC(= 3:30pm PT)。
    注意這裡跑在 5m bar 上,而策略跑在 1m —— 進場點落在 5m 邊界,
    22:30 剛好是 5m 邊界所以對得上。
    """
    from backend.strategy.factor import _topstep_trade_date

    markers: List[Dict[str, Any]] = []
    day = None
    open_px = open_ts = first_ret = None
    fired = False
    for i, bar in enumerate(bars[:-1]):
        ts = _candle_time(bar)
        d = _topstep_trade_date(ts)
        if d != day:
            day, open_px, open_ts, first_ret, fired = d, float(bar.open), ts, None, False
        if open_px is None or open_ts is None:
            continue
        elapsed = (ts - open_ts).total_seconds() / 60.0
        if first_ret is None and elapsed >= first_minutes:
            first_ret = (float(bar.close) - open_px) / open_px
        if fired or first_ret is None or ts.hour < entry_hour:
            continue
        fired = True
        if abs(first_ret) < 1e-5:
            continue
        entry_bar = bars[i + 1]
        entry_time = _candle_time(entry_bar)
        if cutoff and entry_time < cutoff:
            continue
        markers.append(_signal_marker(
            marker_time=entry_time,
            signal_type="momentum",
            subtype=f"{first_minutes}m",
            direction="long" if first_ret > 0 else "short",
            price=float(entry_bar.open),
            source_time=ts,
            detail={"first_ret_pct": round(float(first_ret) * 100, 3),
                    "first_minutes": first_minutes},
        ))
    return markers


def _collect_icefishball_markers(bars: List[Candle], cutoff: Optional[datetime]) -> List[Dict[str, Any]]:
    closes = [float(c.close) for c in bars]
    highs = [float(c.high) for c in bars]
    lows = [float(c.low) for c in bars]

    rsv: List[Optional[float]] = []
    for i, close in enumerate(closes):
        if i < 8:
            rsv.append(None)
            continue
        hi = max(highs[i - 8:i + 1])
        lo = min(lows[i - 8:i + 1])
        rsv.append(None if hi <= lo else 100.0 * ((close - lo) / (hi - lo)))
    k = _bcwsma_series(rsv, 3, 1)
    d = _bcwsma_series(k, 3, 1)
    j = [(3.0 * kk) - (2.0 * dd) for kk, dd in zip(k, d)]

    delta: List[Optional[float]] = [None]
    for i in range(1, len(closes)):
        delta.append(closes[i] - closes[i - 1])
    up = _rma_series([None if v is None else max(v, 0.0) for v in delta], 14)
    down = _rma_series([None if v is None else max(-v, 0.0) for v in delta], 14)
    rsi: List[float] = []
    for u, dn in zip(up, down):
        if dn == 0:
            rsi.append(100.0)
        elif u == 0:
            rsi.append(0.0)
        else:
            rsi.append(100.0 - (100.0 / (1.0 + (u / dn))))

    markers: List[Dict[str, Any]] = []
    for i in range(9, len(bars) - 1):
        short_sig = j[i] > 80 and j[i] < j[i - 1] and closes[i] > closes[i - 1] and rsi[i] > 60
        long_sig = j[i] < 20 and j[i] > j[i - 1] and closes[i] < closes[i - 1] and rsi[i] < 40
        if not (short_sig or long_sig):
            continue
        entry_bar = bars[i + 1]
        entry_time = _candle_time(entry_bar)
        if cutoff and entry_time < cutoff:
            continue
        markers.append(_signal_marker(
            marker_time=entry_time,
            signal_type="icefishball",
            subtype="kdjma",
            direction="short" if short_sig else "long",
            price=float(entry_bar.open),
            source_time=_candle_time(bars[i]),
            detail={"j": round(float(j[i]), 3), "rsi": round(float(rsi[i]), 3)},
        ))
    return markers


def _collect_momentum_reversion_markers(bars: List[Candle], cutoff: Optional[datetime]) -> List[Dict[str, Any]]:
    lookback = 40
    mom_threshold = 0.4
    rev_span = 12
    rev_threshold = 1.1

    closes = [float(c.close) for c in bars]
    atrs = _atr_series(bars, 14, 7)
    mean = _ema_series([float(c) for c in closes], rev_span)

    markers: List[Dict[str, Any]] = []
    min_i = max(lookback + 2, rev_span + 2, 20)
    for i in range(min_i, len(bars) - 1):
        atr = atrs[i]
        avg = mean[i]
        if atr is None or avg is None or atr <= 0:
            continue
        mom = (closes[i] - closes[i - lookback]) / (atr * math.sqrt(lookback))
        rev = (closes[i] - avg) / atr
        if not (math.isfinite(mom) and math.isfinite(rev)):
            continue
        direction = ""
        if mom >= mom_threshold and rev <= -rev_threshold:
            direction = "long"
        elif mom <= -mom_threshold and rev >= rev_threshold:
            direction = "short"
        if not direction:
            continue
        entry_bar = bars[i + 1]
        entry_time = _candle_time(entry_bar)
        if cutoff and entry_time < cutoff:
            continue
        markers.append(_signal_marker(
            marker_time=entry_time,
            signal_type="momentum_reversion",
            subtype="m200r5_0362",
            direction=direction,
            price=float(entry_bar.open),
            source_time=_candle_time(bars[i]),
            detail={"mom_norm": round(float(mom), 4), "rev_z": round(float(rev), 4)},
        ))
    return markers


def _mnq_signal_scope_allowed(candles: List[Candle]) -> Tuple[bool, str]:
    blocked = {"ZL", "GC", "MGC"}
    live_sym = ""
    try:
        live_sym = _extract_symbol(_live_contract_id or "")
    except Exception:
        live_sym = ""
    candle_syms = {
        str(getattr(c, "symbol", "") or "").upper().replace("/", "")
        for c in candles[-50:]
        if str(getattr(c, "symbol", "") or "").strip()
    }
    if live_sym in blocked or candle_syms.intersection(blocked):
        return False, f"Skipped non-MNQ scope: live={live_sym or '-'} candles={','.join(sorted(candle_syms)) or '-'}"
    return True, live_sym or (",".join(sorted(candle_syms)) or "MNQ")

async def _refresh_recent_historical_candles(contract_id: str, limit: int = 240) -> None:
    """Refresh recent 1m bars before simulation so backtest uses final OHLC."""
    if not _topstepx_client:
        return
    try:
        candles = await _topstepx_client.get_historical_bars(
            contract_id=contract_id,
            unit=BarUnit.MINUTE,
            unit_number=1,
            limit=limit,
        )
        _upsert_historical_candles(candles)
    except Exception as e:
        logger.warning(f"Recent candle refresh skipped: {e}")


# ── Pydantic 請求/回應模型 ────────────────────────────

class BacktestRequest(BaseModel):
    initial_capital: float = 50000.0
    # Strategy params
    strategy: str = "factor"
    tp_ticks: int = 200
    sl_ticks: int = 50
    trail_sl_ticks: int = 10
    trail_sl_pct: Optional[float] = 0.05
    trail_trigger_pct: float = 0.30
    trail_enabled: bool = True            # v1.0.6: master trail switch
    tr_tp_ticks: Optional[int] = None
    tr_sl_ticks: Optional[int] = None
    tr_trail_sl_ticks: Optional[int] = None
    tr_trail_sl_pct: Optional[float] = None
    tr_trail_trigger_pct: Optional[float] = None
    tr_trail_enabled: Optional[bool] = None
    tr_full_tp_lock: Optional[int] = None
    tr_allowed_sessions: Optional[List[str]] = Field(
        default_factory=lambda: list(DEFAULT_ALLOWED_SESSIONS)
    )
    candle_seconds: int = 60
    value_area_pct: float = 0.80
    area_timeframe: str = "15m"
    rr_ratio: int = 2                     # reward:risk multiple (1..6)
    tr_exit_mode: str = "tp"              # 1.0.8: "tp" 固定 TP | "ladder" 階梯滾動
    tr_daily_loss_stop: int = 0           # 1.0.8: 日虧 N 單斷路器(0=OFF;UI=FULL LOSS LOCK)
    tr_daily_win_stop: int = 0            # 1.0.9: FULL WIN LOCK — 日贏 N 單停新單(0=OFF)
    # 1.0.9: PDPT — 當日獲利達此金額($)後停開新單(0=OFF)。Topstep XFA 一致性用。
    tr_daily_profit_stop: float = 0.0
    sweep_models: Optional[List[str]] = None  # 1.0.9: sweep run/lock — 要跑的 model 清單(None=全部)
    fade_tp_frac: float = 0.75            # 1.0.9: DAY ZONE TP=VAL→POC 比例
    fade_entry_mode: str = "limit"        # 1.0.9: DAY ZONE 進場 limit|rejection|or15
    # Contract & sizing (defaults to 3× Micro NQ)
    sigma_window_minutes: int = 15
    sigma_method: str = "std"
    sigma_entry_mode: str = "blind"
    sigma_accept_mode: str = "none"
    sigma_start: float = 1.0
    sigma_max: float = 3.0
    sigma_target_mode: str = "half"
    sigma_stop_span: float = 1.0
    sigma_accept_sigma: float = 2.0
    sigma_accept_bars: int = 2
    factor_timeframe_minutes: int = 5
    factor_signal_family: str = "emapmo"
    factor_side_mode: str = "all"
    factor_pmo_signal_mode: str = "normal"
    factor_session_va_filter: str = "off"
    factor_sl_rule: str = "atr"
    factor_tp_rule: str = "atr"
    factor_sl_value: float = 1.5
    factor_tp_value: float = 2.0
    factor_max_hold_bars: int = 24
    factor_max_trades_per_day: int = 3
    factor_warmup_bars: int = 320
    factor_pmo_threshold_scale: float = 1.0
    factor_pmo_normal_scale: float = 0.0
    factor_pmo_early_scale: float = 0.0
    momentum_first_minutes: int = 30
    momentum_entry_hour: int = 18
    # 1.0.9 SESSFIB —— fib 級別可調。0.618 是掃描中唯一通過 G0–G4 的進場位。
    factor_pmo_adaptive_window: int = 0
    betafib_entry_fib: float = 0.618
    betafib_anchor: str = "hl"
    betafib_risk_basis: str = "atr_blend"
    betafib_min_move_pct: float = 0.0
    # 1.0.10: 腿幅上限 + 進場時窗(UTC 小時) + fib 基準的 SL/TP 層級
    betafib_max_move_pct: float = 0.0
    betafib_entry_start_hour: Optional[int] = None
    betafib_entry_end_hour: Optional[int] = None
    # 1.0.10: π 外部訊號策略
    pi_long_only: bool = False
    pi_signal_set: str = "pi_only"
    pi_max_signal_age_min: int = 5
    pi_short_sl_value: float = 2.5
    pi_short_hold_min: int = 60
    betafib_sl_fib: float = 0.75
    betafib_tp_fib: float = 0.90
    contract_id: str = Field(default_factory=lambda: current_quarterly_contract_id("MNQ"))
    contract_size: int = 3
    full_tp_lock: int = 0                 # 0=OFF, 1/2/3 TP exits
    one_trade_per_session_direction: bool = True
    tr_one_trade_per_session: bool = True
    # Zone stability is enabled by default; keep this flag for future experiments.
    skip_zone_stability: bool = False
    breakout_confirm_bars: int = 7
    # v1.0.6: "single" = one area timeframe; "overlap" = enter at the AVERAGE
    # overlapping VAH/VAL of the timeframes in tf_combo (reproduces an ML overlap row).
    method: str = "single"
    tf_combo: Optional[List[str]] = None
    tr_overlap_trade_tf: str = "merged"   # "merged"=average overlap zone, "smallest"=trade smallest TF zone
    # v1.0.6: confluence (explainable ML scorer) backtest. When strategy=="confluence"
    # the multi-timeframe weighted-level engine is used instead of the trend engine.
    conf_band_ticks: float = 4.0          # level-cluster band width (ticks)
    conf_min_distinct_tf: int = 2         # cluster needs >= this many timeframes
    conf_rr: float = Field(default=1.0, ge=1.0, le=6.0)
    conf_wait_minutes: int = 1            # live parity: one-shot limit-order timeout
    conf_base_minutes: int = 1            # input candle resolution (1 or 5)
    conf_min_prob: float = 0.65           # optimized gate: skip signals below this win-prob
    conf_ev_floor: Optional[float] = None # EV-priority gate: keep EV>=floor (None=win-prob gate; 0=every +EV)
    conf_rr_grid: Optional[List[float]] = None
    conf_use_scorer: bool = True          # True=trained JSON, False=heuristic prior
    conf_enable_breakout: bool = False    # include breakout-retrace candidate (False=momentum+reversion only)
    conf_max_risk_ticks: Optional[int] = None  # drop signals with SL > N ticks (None=no cap)
    max_profit_ticks: Optional[int] = None     # 1.0.9: TP width cap (prop-firm consistency rule)
    conf_sl_reference_tf: str = "largest" # "largest"=original, "smallest"=lowest contributing TF anchors SL/TP
    conf_allowed_sessions: Optional[List[str]] = Field(
        default_factory=lambda: list(DEFAULT_ALLOWED_SESSIONS)
    )
    # --- STYLE: optional exit-policy (break-even / trail / lock). All-OFF == original behaviour ---
    conf_trail_trigger_pct: float = 0.50  # optimized: fire after 50% of TP distance
    conf_trail_lock_pct: float = 0.05     # optimized: lock +5% of TP distance
    conf_full_tp_lock: int = 0            # 0 = OFF; stop new entries after N full-TP exits/session
    conf_session_limit: bool = True       # live-style one trade per zone+direction/session
    # 1.0.8: 移除 ML Consolidation V2 (mlc2_*) 請求欄位 — 該策略已刪除


class FetchHistoricalRequest(BaseModel):
    username: str = ""             # 空 = 從 .env 讀取
    api_key: str = ""              # 空 = 從 .env 讀取
    contract_id: str = ""          # 空 = 自動找 NQ
    unit: int = 2                  # 2=分鐘
    unit_number: int = 5           # 5=5分鐘
    start_time: str = ""           # ISO format
    end_time: str = ""
    use_demo: Optional[bool] = None  # None = 從 .env 讀取
    append: bool = False           # True = merge into existing historical candles
    continuous_contract: bool = True  # True = merge previous quarterly contract for rollover history
    force_full: bool = False        # True = ignore local store, re-pull everything from API
    # 1.0.10: True = 完全不打券商,只用本機 store。券商維護時間仍可回測 ——
    # store 已有 2020 起的 233 萬根,不需要 API 也能跑完整回測。
    store_only: bool = False


class TradeResponse(BaseModel):
    trade_id: str
    strategy: str
    symbol: str = "/NQ"
    size: int = 1
    direction: str
    entry_price: float
    entry_time: str
    exit_price: Optional[float]
    exit_time: Optional[str]
    sl_price: float
    tp_price: float
    original_sl_price: Optional[float] = None
    original_tp_price: Optional[float] = None
    pnl: Optional[float]            # NET (after commission + fees)
    commission: float = 0.0
    fees: float = 0.0
    exit_reason: Optional[str]
    zone_id: str
    zone_source: Optional[str] = None
    mode: Optional[str] = None
    side: Optional[str] = None
    largest_tf: Optional[str] = None
    risk_tf: Optional[str] = None
    decision_tfs: List[str] = Field(default_factory=list)
    overlap_tfs: List[str] = Field(default_factory=list)
    trade_tf: Optional[str] = None
    wall_id: Optional[str] = None
    labels: List[str] = Field(default_factory=list)
    primary_zone: Optional[Dict[str, Any]] = None
    or_range: Optional[Dict[str, Any]] = None
    reason: Optional[str] = None
    vol_ratio: Optional[float]
    is_big_trend: bool


class ZoneResponse(BaseModel):
    zone_id: str
    formed_at: str
    left_at: Optional[str]
    poc: float
    vah_80: float
    val_80: float
    high_100: float
    low_100: float
    total_volume: int
    duration_minutes: int
    num_candles: int
    status: str
    exit_direction: Optional[str]
    profile: Optional[list] = None  # VP histogram data [{price, volume, pct}]
    timeframe: str = "5m"
    parent_zone_id: Optional[str] = None
    mature: bool = False  # Session zone maturity flag
    va_curve: Optional[list] = None  # [{ts, vah, val}] developing VA boundary curve


class SubMetricsResponse(BaseModel):
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    avg_rr_ratio: float = 0.0
    total_pnl: float = 0.0
    profit_factor: float = 0.0

class MetricsResponse(BaseModel):
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    avg_win: float
    avg_loss: float
    avg_rr_ratio: float
    expectancy: float
    max_drawdown: float
    max_drawdown_pct: float
    calmar_ratio: float = 0.0
    profit_factor: float
    max_consecutive_losses: int
    total_pnl: float
    total_gain: float = 0.0
    total_loss: float = 0.0
    daily_pnl: Dict[str, float] = {}
    # Post-breakout 60m path stats (averaged across confirmed-breakout trades)
    post_breakout_sample_size: int = 0
    post_breakout_avg_max_fav_ticks: float = 0.0
    post_breakout_avg_max_adv_ticks: float = 0.0
    post_breakout_tp_clean: int = 0
    post_breakout_tp_after_trail: int = 0
    post_breakout_tp_after_sl: int = 0
    current_zone_trades: int = 0
    current_zone_wins: int = 0
    current_zone_win_rate: float = 0.0
    current_zone_avg_pnl: float = 0.0
    current_zone_total_pnl: float = 0.0
    # Week-to-week variation (std/cv/range/consistency) — see _weekly_stats()
    weekly_stats: Dict[str, Any] = {}
    # Per-strategy breakdown
    trend_follow: Optional[SubMetricsResponse] = None


class BacktestResponse(BaseModel):
    metrics: MetricsResponse
    trades: List[TradeResponse]
    zones: List[ZoneResponse]
    equity_curve: List[List[float]]   # [[timestamp_ms, equity], ...]


# ── 路由 ──────────────────────────────────────────────

@router.get("/health")
async def health():
    return {"status": "ok", "service": "ancserTPX"}


@router.get("/config")
async def get_config():
    """
    返回 .env 中的配置（API key 只顯示前 6 碼）
    前端用來自動填入表單
    """
    username = _env("TOPSTEPX_USERNAME")
    api_key = _env("TOPSTEPX_API_KEY")
    contract_id = _env("TOPSTEPX_CONTRACT_ID")
    use_demo = _env("TOPSTEPX_USE_DEMO", "false").lower() == "true"

    return {
        "username": username,
        "has_api_key": bool(api_key),
        "api_key_preview": api_key[:6] + "***" if api_key else "",
        "contract_id": contract_id,
        "use_demo": use_demo,
        "env_loaded": bool(username and api_key),
    }


@router.post("/save-config")
async def save_config(body: dict):
    """Save credentials to .env so they persist across restarts."""
    env_path = Path(__file__).parent.parent.parent / ".env"

    existing: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                existing[k.strip()] = v.strip()

    if body.get("username"):
        existing["TOPSTEPX_USERNAME"] = body["username"]
    if body.get("api_key"):
        existing["TOPSTEPX_API_KEY"] = body["api_key"]

    lines = [f"{k}={v}" for k, v in existing.items()]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    os.environ["TOPSTEPX_USERNAME"] = existing.get("TOPSTEPX_USERNAME", "")
    os.environ["TOPSTEPX_API_KEY"] = existing.get("TOPSTEPX_API_KEY", "")

    logger.info(f".env saved: username={existing.get('TOPSTEPX_USERNAME', '')}")
    return {"success": True}


@router.get("/data/candles")
async def get_stored_candles(limit: int = 60000):
    """返回已載入的歷史 K 線數據 (用於前端圖表顯示)。

    limit 只限制「回傳給前端畫圖」的最近 K 線數量，避免全範圍 (數十萬根)
    一次送出造成瀏覽器主執行緒卡死。回測 / 機器學習仍使用完整的
    _historical_candles，不受此上限影響。limit<=0 表示不限制。
    """
    if not _historical_candles:
        return {"candles": [], "count": 0}

    rows = _historical_candles
    total = len(rows)
    if limit and limit > 0 and total > limit:
        rows = rows[-limit:]

    return {
        "candles": [
            {
                "time": c.timestamp.isoformat(),
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            }
            for c in rows
        ],
        "count": total,
        "shown": len(rows),
    }


@router.get("/data/mnq-signals")
async def get_mnq_signal_markers(limit: int = 60000):
    """Return read-only MNQ factor markers for the 1m chart.

    The factors are evaluated on completed 5m bars. Marker timestamps are the
    next 5m open so chart markers line up with actionable, non-repainting bars.
    """
    if not _historical_candles:
        return {"signals": [], "count": 0, "shown": 0, "symbol": "MNQ"}

    candles = sorted(_historical_candles, key=_candle_time)
    allowed, symbol = _mnq_signal_scope_allowed(candles)
    if not allowed:
        return {
            "signals": [],
            "count": 0,
            "shown": 0,
            "symbol": symbol,
            "skipped": symbol,
        }

    cutoff: Optional[datetime] = None
    if limit and limit > 0 and len(candles) > limit:
        cutoff = _candle_time(candles[-limit])

    try:
        bars_5m = BacktestEngine.aggregate_1m_to_5m(candles)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not aggregate MNQ markers: {exc}")

    signals: List[Dict[str, Any]] = []
    signals.extend(_collect_pmo_markers(bars_5m, cutoff))
    signals.extend(_collect_momentum_reversion_markers(bars_5m, cutoff))
    signals.extend(_collect_icefishball_markers(bars_5m, cutoff))
    signals.extend(_collect_momentum_markers(bars_5m, cutoff))
    signals.sort(key=lambda row: row["time"])
    # 1.0.9: SESSFIB 是水平掛單線,不是點狀 marker —— 走獨立欄位,
    # 不混進 signals(那條路徑只認得箭頭/三角/圓點)。
    try:
        betafib_levels = _collect_betafib_levels(bars_5m, cutoff)
    except Exception:
        logger.exception("SESSFIB 疊圖計算失敗")
        betafib_levels = []

    max_markers = 5000
    shown = signals[-max_markers:] if len(signals) > max_markers else signals
    counts: Dict[str, int] = {}
    for row in shown:
        key = str(row.get("type") or "")
        counts[key] = counts.get(key, 0) + 1

    return {
        "signals": shown,
        "count": len(signals),
        "shown": len(shown),
        "symbol": "MNQ",
        "source_interval": "5m",
        "display_interval": "1m",
        "counts": counts,
        "betafib_levels": betafib_levels,
    }


@router.get("/research/institution/latest")
async def institution_research_latest():
    """Latest hunter/sweep/liquidity research summary for the Data tab."""
    path = Path("data") / "machinelearning" / "institution_research" / "latest.json"
    if not path.exists():
        return {
            "available": False,
            "message": "No institution research output yet. Run: python -m scripts.institution_behavior_research",
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        validation_path = Path("data") / "machinelearning" / "edge_validation" / "latest.json"
        if validation_path.exists():
            data["edge_validation"] = json.loads(validation_path.read_text(encoding="utf-8"))
        futures_port_path = Path("data") / "machinelearning" / "futures_repo_port" / "latest.json"
        if futures_port_path.exists():
            data["futures_repo_port"] = json.loads(futures_port_path.read_text(encoding="utf-8"))
        data["available"] = True
        return data
    except Exception as exc:
        raise HTTPException(500, f"Could not read institution research: {exc}")


@router.get("/data/latest-candles")
async def get_latest_candles(since: str = ""):
    """
    Fetch fresh candles from TopstepX API (for live polling).
    If `since` is provided (ISO timestamp), only return candles after that time.
    Also appends new candles to _historical_candles store.
    """
    if not _topstepx_client:
        # Fallback: return last few stored candles
        if _historical_candles:
            recent = _historical_candles[-5:]
            return {
                "candles": [
                    {
                        "time": c.timestamp.isoformat(),
                        "open": c.open, "high": c.high, "low": c.low,
                        "close": c.close, "volume": c.volume,
                    }
                    for c in recent
                ],
                "count": len(recent),
            }
        return {"candles": [], "count": 0}

    try:
        import time as _time
        from backend.db.models import BarUnit

        # Cache: only fetch from API every 5 seconds to avoid rate limits
        now_ts = _time.time()
        if _candle_cache["data"] and (now_ts - _candle_cache["time"]) < 5:
            candles = _candle_cache["data"]
        else:
            candles = await _topstepx_client.get_historical_bars(
                contract_id=_live_contract_id,
                unit=BarUnit.MINUTE,   # 1m — 30s has ~6h settle delay
                unit_number=1,
                limit=60,
            )
            _candle_cache["data"] = candles
            _candle_cache["time"] = now_ts

        if not candles:
            return {"candles": [], "count": 0}

        candles = sorted(candles, key=lambda c: c.timestamp)
        # Live/chart polling uses only completed bars. Even with
        # includePartialBar=False, keep a one-bar safety buffer so live
        # decisions match backtest's closed-candle timing.
        closed_candles = candles[:-1] if len(candles) > 1 else []
        if not closed_candles:
            return {"candles": [], "count": 0}
        _upsert_historical_candles(closed_candles)

        # Filter by `since` if provided
        result = closed_candles
        if since:
            from datetime import datetime as dt
            try:
                since_dt = dt.fromisoformat(since.replace("Z", "+00:00"))
                result = [c for c in closed_candles if c.timestamp > since_dt]
            except Exception:
                pass

        return {
            "candles": [
                {
                    "time": c.timestamp.isoformat(),
                    "open": c.open, "high": c.high, "low": c.low,
                    "close": c.close, "volume": c.volume,
                }
                for c in result
            ],
            "count": len(result),
        }
    except Exception as e:
        logger.error(f"latest-candles error: {e}")
        # Fallback to stored
        if _historical_candles:
            recent = _historical_candles[-5:]
            return {
                "candles": [
                    {
                        "time": c.timestamp.isoformat(),
                        "open": c.open, "high": c.high, "low": c.low,
                        "close": c.close, "volume": c.volume,
                    }
                    for c in recent
                ],
                "count": len(recent),
            }
        return {"candles": [], "count": 0}


class DetectZonesRequest(BaseModel):
    min_candles_for_zone: int = 6
    poc_drift_threshold: float = 3.0
    value_area_pct: float = 0.80
    area_timeframe: str = "15m"
    all_timeframes: bool = False   # ML: draw every timeframe's VAH/VAL/POC at once


def _zone_to_dict(z, fallback_tf: str) -> dict:
    """Serialise one consolidation zone (with VP histogram) for the chart."""
    zd = {
        "zone_id": z.zone_id,
        "poc": z.poc,
        "vah_80": z.vah_80,
        "val_80": z.val_80,
        "high_100": z.high_100,
        "low_100": z.low_100,
        "status": z.status.value,
        "formed_at": z.formed_at.isoformat() if z.formed_at else None,
        "left_at": z.left_at.isoformat() if z.left_at else None,
        "exit_direction": z.exit_direction,
        "num_candles": z.num_candles,
        "timeframe": getattr(z, 'timeframe', fallback_tf),
        "parent_zone_id": getattr(z, 'parent_zone_id', None),
        "va_curve": getattr(z, 'va_curve', None) or None,
        "mature": getattr(z, 'mature', False),
    }
    profile = getattr(z, "profile", None) or {}
    if profile:
        max_vol = max(profile.values()) or 1
        zd["profile"] = [
            {"price": p, "volume": v, "pct": round(v / max_vol, 3)}
            for p, v in sorted(profile.items())
        ]
    else:
        zd["profile"] = []
    return zd


_chart_zone_cache: dict = {}
_chart_zone_cache_lock = threading.Lock()


def _chart_candle_signature(candle: Candle) -> tuple:
    return (
        candle.timestamp,
        candle.open,
        candle.high,
        candle.low,
        candle.close,
        candle.volume,
    )


def _detect_zones_sync(candles, timeframes, value_area_pct: float) -> List[dict]:
    """Build or incrementally advance chart-zone detectors off the event loop."""
    from backend.strategy.consolidation import build_zone_detector

    zone_list = []
    with _chart_zone_cache_lock:
        for tf in timeframes:
            key = (tf, float(value_area_pct))
            entry = _chart_zone_cache.get(key)
            count = int(entry["count"]) if entry else 0
            can_extend = bool(
                entry
                and len(candles) >= count
                and (count == 0 or (
                    candles[0].timestamp == entry["first_timestamp"]
                    and _chart_candle_signature(candles[count - 1]) == entry["last_signature"]
                ))
            )

            if can_extend:
                detector = entry["detector"]
                start = count
            else:
                detector = build_zone_detector(
                    area_timeframe=tf,
                    value_area_pct=value_area_pct,
                    # Chart-zone detection consumes completed buckets only.
                    # Rebuilding the active volume profile after every bar is
                    # quadratic within each clock bucket and can pin a worker
                    # for minutes on a full candle history.
                    recalc_active_each_bar=False,
                )
                start = 0

            for candle in candles[start:]:
                detector.update(candle)

            # recalc_active_each_bar=False deliberately skips intermediate
            # forming-bucket profiles. Refresh it once here so the chart still
            # receives the same current-bucket values as the live detector.
            refresh_forming = getattr(detector, "refresh_forming_zone", None)
            if refresh_forming is not None:
                refresh_forming()

            serialized = [_zone_to_dict(z, tf) for z in detector.get_all_zones()]
            _chart_zone_cache[key] = {
                "detector": detector,
                "count": len(candles),
                "first_timestamp": candles[0].timestamp if candles else None,
                "last_signature": _chart_candle_signature(candles[-1]) if candles else None,
                "zones": serialized,
            }
            zone_list.extend(serialized)
    return zone_list


@router.post("/data/detect-zones")
async def detect_zones(req: DetectZonesRequest = DetectZonesRequest()):
    """Run zone detection on stored candles — returns zones with VP profiles.

    When ``all_timeframes`` is set, detection runs for every ML timeframe
    (5m/15m/30m/1h/4h) and the zones are returned together, each tagged with
    its own ``timeframe`` so the chart can overlay all VAH/VAL/POC at once.
    """
    value_area_pct = _normalize_value_area_pct(req.value_area_pct)
    # During a live session, prefer the engine's rolling candle history (warm-up +
    # live) so the chart's multi-timeframe zone filter reflects the freshest bars.
    base_candles = _historical_candles
    if _live_engine is not None and getattr(_live_engine, "is_running", False):
        live_hist = _live_engine.get_candle_history()
        if live_hist:
            base_candles = live_hist
    if not base_candles:
        # No candles yet (e.g. live just connecting / warm-up not produced bars).
        # This is a normal transient state — return empty zones instead of 400 so
        # the chart's multi-timeframe filter doesn't spam Bad Request.
        if getattr(req, "all_timeframes", False):
            return {
                "zones": [],
                "count": 0,
                "area_timeframe": "all",
                "timeframes": list(ML_TIMEFRAMES),
            }
        area_timeframe = _normalize_area_timeframe(getattr(req, "area_timeframe", "15m"))
        return {"zones": [], "count": 0, "area_timeframe": area_timeframe}
    sorted_candles = sorted(base_candles, key=lambda c: c.timestamp)

    if getattr(req, "all_timeframes", False):
        # 1.0.8: +session 生長區間,圖表 TF filter 勾 SESSION 時才有 zone 可畫
        _all_tfs = ML_TIMEFRAMES + ("session",)
        zone_list = await asyncio.to_thread(
            _detect_zones_sync,
            sorted_candles,
            _all_tfs,
            value_area_pct,
        )
        return {
            "zones": zone_list,
            "count": len(zone_list),
            "area_timeframe": "all",
            "timeframes": list(_all_tfs),
        }

    area_timeframe = _normalize_area_timeframe(getattr(req, "area_timeframe", "15m"))
    zone_list = await asyncio.to_thread(
        _detect_zones_sync,
        sorted_candles,
        (area_timeframe,),
        value_area_pct,
    )
    return {"zones": zone_list, "count": len(zone_list), "area_timeframe": area_timeframe}


@router.post("/accounts")
async def get_accounts():
    """
    取得 TopstepX 帳戶列表

    用於前端帳戶切換 (Practice / Funded)
    """
    from backend.broker.topstepx import TopstepXClient

    username = _env("TOPSTEPX_USERNAME")
    api_key = _env("TOPSTEPX_API_KEY")

    if not username or not api_key:
        raise HTTPException(status_code=400, detail="Missing credentials in .env")

    client = TopstepXClient(username=username, api_key=api_key)
    try:
        await client.authenticate()
        accounts = await client.get_accounts()

        result = []
        for acc in accounts:
            # Only show active (canTrade) accounts — skip closed/blown ones
            if not acc.get("canTrade", False):
                continue
            name = acc.get("name", "")
            result.append({
                "id": acc["id"],
                "name": name,
                "balance": acc.get("balance", 0),
                "can_trade": True,
                "is_practice": "PRAC" in name,
            })

        # 1.0.9: 套上帳號類型(express/practice/exam)+ 持久化的 preset/live/main 指派。
        from backend.live.account_roles import annotate_accounts, load_roles
        roles = load_roles()
        result = annotate_accounts(result, roles)
        return {"success": True, "accounts": result, "roles": roles}

    except Exception as e:
        logger.error(f"Accounts fetch failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await client.disconnect()


@router.get("/accounts/roles")
async def get_account_roles():
    """1.0.9: 讀取 Live 帳號槽的 preset/main 指派。"""
    from backend.live.account_roles import load_roles
    return {"success": True, "roles": load_roles()}


class AccountRolesRequest(BaseModel):
    email: str = ""
    main_account_id: str = ""
    accounts: dict = {}      # {"<accId>": {"preset": "<name>"|null, "live": bool}}


@router.post("/accounts/roles")
async def save_account_roles(req: AccountRolesRequest):
    """1.0.9: 存檔每帳號 preset/live + 固定主帳號(驗證:最多 2 個 live、main 須為有效帳號)。"""
    from backend.live.account_roles import save_roles
    saved = save_roles({
        "email": req.email,
        "main_account_id": req.main_account_id,
        "accounts": req.accounts,
    })
    return {"success": True, "roles": saved}


@router.post("/data/fetch-historical")
async def fetch_historical(req: FetchHistoricalRequest):
    """
    從 TopstepX API 拉取歷史 K 線數據

    優先使用請求中的值，空則從 .env 讀取
    """
    global _historical_candles

    from backend.broker.topstepx import TopstepXClient, contract_roll_start

    # .env fallback
    username = req.username or _env("TOPSTEPX_USERNAME")
    api_key = req.api_key or _env("TOPSTEPX_API_KEY")
    contract_id = req.contract_id or _env("TOPSTEPX_CONTRACT_ID")
    use_demo = req.use_demo if req.use_demo is not None else (
        _env("TOPSTEPX_USE_DEMO", "false").lower() == "true"
    )

    if not username or not api_key:
        raise HTTPException(
            status_code=400,
            detail="Missing credentials: set TOPSTEPX_USERNAME and TOPSTEPX_API_KEY in .env"
        )

    logger.info(f"Connecting as '{username}', demo={use_demo}, contract='{contract_id or 'auto'}'")

    client = TopstepXClient(
        username=username,
        api_key=api_key,
        use_demo=use_demo,
    )

    try:
        await client.authenticate()
        logger.info("Auth OK")

        # Store client globally for live trading
        global _topstepx_client, _live_contract_id
        if _topstepx_client:
            try:
                await _topstepx_client.disconnect()
            except Exception:
                pass
        _topstepx_client = client

        # Auto front-month rollover: resolve to the CURRENT tradable contract so
        # an expired month (e.g. MNQM26 after June) never gets used. Defaults to
        # MNQ when nothing was specified.
        try:
            resolved = await client.get_front_month_contract_id(contract_id or "MNQ")
            if resolved:
                if resolved != contract_id:
                    logger.info(f"Auto front-month: {contract_id or '(auto)'} -> {resolved}")
                contract_id = resolved
        except Exception as e:
            logger.warning(f"Front-month resolve failed: {e}")
            if not contract_id:
                contract_id = await client.get_nq_contract_id()
        _live_contract_id = contract_id

        fetch_contracts: List[str] = [contract_id]
        if req.continuous_contract:
            try:
                prev_contract = await client.get_previous_quarter_contract_id(contract_id)
                if prev_contract and prev_contract not in fetch_contracts:
                    fetch_contracts.insert(0, prev_contract)
                    logger.info(f"Continuous contract merge: {prev_contract} + {contract_id}")
            except Exception as e:
                logger.warning(f"Previous-contract resolve skipped: {e}")

        # ── Local store: load cached bars, narrow the API fetch ──
        symbol = _extract_symbol(contract_id)
        store_bars: List[Candle] = []
        fetch_start = req.start_time
        from_store = False
        if not req.force_full and not req.append and req.unit_number == 1:
            store_bars = _store_load(symbol)
            if store_bars:
                from_store = True
                # Only fetch the tail: from 2h before the last stored bar (overlap
                # for dedup safety) to now. This turns a 60k-bar full pull into
                # a few-hundred-bar incremental pull.
                last_ts = _store_utc(store_bars[-1].timestamp)
                overlap_start = last_ts - timedelta(hours=2)
                fetch_start = overlap_start.strftime("%Y-%m-%dT%H:%M:%SZ")
                logger.info(
                    f"[Store] loaded {len(store_bars)} bars from local store "
                    f"(last: {last_ts.isoformat()}) → incremental fetch from {fetch_start}")

        contract_batches: Dict[str, List[Candle]] = {}
        contract_counts: Dict[str, int] = {}
        # 1.0.10: store_only → 完全跳過券商。維護時段或斷線時仍可回測。
        _skip_api = bool(getattr(req, "store_only", False)) and from_store
        if _skip_api:
            logger.info("[Store] store_only=True — 跳過券商,只用本機 %d 根", len(store_bars))
        for cid in ([] if _skip_api else fetch_contracts):
            batch = await client.get_historical_bars_paginated(
                contract_id=cid,
                unit=BarUnit(req.unit),
                unit_number=req.unit_number,
                start_time=fetch_start,
                end_time=req.end_time,
            )
            contract_batches[cid] = batch
            contract_counts[cid] = len(batch)

        roll_at = contract_roll_start(fetch_contracts[0]) if len(fetch_contracts) > 1 else None
        candles, continuous_meta = _build_continuous_candles(contract_batches, fetch_contracts, roll_at)
        if len(fetch_contracts) > 1:
            logger.info(
                "Continuous contract adjusted: %s -> %s roll_at=%s anchor=%s offset=%.2f",
                fetch_contracts[0],
                fetch_contracts[-1],
                continuous_meta.get("roll_at"),
                continuous_meta.get("anchor_time"),
                continuous_meta.get("price_adjustment", 0.0),
            )

        # ── Merge store + fresh fetch ──
        _store_dirty = False
        if from_store:
            # Upsert fresh API bars into the stored set (newer wins on clash)
            by_ts: Dict[str, Candle] = {_candle_key(c): c for c in store_bars}
            for c in candles:
                k = _candle_key(c)
                old = by_ts.get(k)
                # 1.0.10: 逐根比對,只有真的新增或修訂才算「有變更」。
                # 實測連線後常見 `2331102 stored + 121 fetched → 2331102 unique`
                # —— 那 121 根本來就在 store 裡、內容也一樣,卻仍觸發整份重寫。
                if old is None or (old.open, old.high, old.low, old.close, old.volume) != \
                        (c.open, c.high, c.low, c.close, c.volume):
                    by_ts[k] = c
                    _store_dirty = True
            candles = sorted(by_ts.values(), key=_candle_time)
            logger.info(
                f"[Store] merged: {len(store_bars)} stored + "
                f"{sum(contract_counts.values())} fetched → {len(candles)} unique"
                f"{'' if _store_dirty else ' (無變更,略過寫盤)'}")

        # ── Persist to local store (1m bars only) ──
        # 1.0.10: 只有真的變更才寫盤。233 萬根整份重寫要 14–17 秒,而一次連線
        # 流程會觸發多次 fetch —— 實測啟動時寫了三遍、合計約 48 秒,前端就卡在
        # LOADING DATA。沒有 store 基底時(首次抓取)一律寫。
        if req.unit_number == 1 and candles and not _skip_api and (_store_dirty or not from_store):
            try:
                _store_save(candles, symbol)
            except Exception as e:
                logger.warning(f"[Store] save failed (non-fatal): {e}")

        # ── Gap detection + auto-recovery ──
        # 1.0.10: 資料沒變就不必重掃 —— 233 萬根掃一次約 2 秒,而且結果必定相同。
        if (req.unit_number == 1 and candles and not req.append and not _skip_api
                and (_store_dirty or not from_store)):
            try:
                gaps = _store_detect_gaps(candles)
                if gaps:
                    logger.info(f"[Store] detected {len(gaps)} unexpected gap(s), attempting recovery...")
                    # 1.0.10 BUG FIX:原本是 `gaps[:5]`,而 gaps 依時間排序 ——
                    # 永遠取到最舊的 5 個(2020 年)。券商只保留約 60 天,那 5 次
                    # 請求必定回 0 bars;更糟的是**真正該修的近期破洞永遠排不進
                    # 那 5 個名額**。改成只回補券商真的拿得到的範圍,並取最新的。
                    _now = datetime.now(timezone.utc)
                    _reach = _now - timedelta(days=BROKER_HISTORY_DAYS)
                    _fixable = [g for g in gaps if _store_utc(g[0]) >= _reach]
                    _old = len(gaps) - len(_fixable)
                    if _old:
                        logger.info(
                            "[Store] %d 個破洞早於券商保留期(%d 天),不嘗試回補 —— "
                            "那段是 Databento 補的歷史,券商沒有", _old, BROKER_HISTORY_DAYS)
                    recovered = 0
                    for gap_start, gap_end, dur in _fixable[-5:]:  # 取**最新**的 5 個
                        pad = timedelta(minutes=5)
                        gs = (gap_start - pad).strftime("%Y-%m-%dT%H:%M:%SZ")
                        ge = (gap_end + pad).strftime("%Y-%m-%dT%H:%M:%SZ")
                        logger.info(f"[Store] re-fetching gap: {gap_start.isoformat()} → {gap_end.isoformat()} ({dur:.0f}min)")
                        # Fetch from the FRONT contract only (gap is in the recent/live range)
                        gap_bars = await client.get_historical_bars_paginated(
                            contract_id=contract_id,
                            unit=BarUnit(req.unit),
                            unit_number=req.unit_number,
                            start_time=gs, end_time=ge,
                        )
                        if gap_bars:
                            by_ts2: Dict[str, Candle] = {_candle_key(c): c for c in candles}
                            for c in gap_bars:
                                by_ts2[_candle_key(c)] = c
                            candles = sorted(by_ts2.values(), key=_candle_time)
                            recovered += len(gap_bars)
                    if recovered:
                        logger.info(f"[Store] recovered {recovered} bars; re-saving store")
                        try:
                            _store_save(candles, symbol)
                        except Exception:
                            pass
                    # Re-check remaining gaps
                    remaining = _store_detect_gaps(candles)
                    if remaining:
                        logger.warning(f"[Store] {len(remaining)} gap(s) remain after recovery (may be real market closures)")
                # Advance frozen boundary if tail is clean
                _store_advance_frozen(candles, symbol)
            except Exception as e:
                logger.warning(f"[Store] gap detection failed (non-fatal): {e}")

        if req.append:
            _upsert_historical_candles(candles)
        else:
            # 1.0.10: store 是累積器(保留全部),但**記憶體工作集只放要求的範圍**。
            # 先前不管請求什麼日期,_historical_candles 一律是整份 233 萬根 ——
            # 回測就在這上面跑,單次約 219 秒(3.7 分鐘),使用者只看到畫面不動。
            # PI 只需要 2026-06 起的 6.8 萬根,縮到範圍內約 7 秒。
            _win = sorted(candles, key=_candle_time)
            _lo = _parse_iso_utc(req.start_time) if req.start_time else None
            _hi = _parse_iso_utc(req.end_time) if req.end_time else None
            if _lo or _hi:
                _before = len(_win)
                _win = [c for c in _win
                        if (_lo is None or _store_utc(c.timestamp) >= _lo)
                        and (_hi is None or _store_utc(c.timestamp) <= _hi)]
                if len(_win) != _before:
                    logger.info(
                        "[Store] 工作集裁到請求範圍: %d → %d 根 (%s → %s)",
                        _before, len(_win), req.start_time or "-", req.end_time or "-")
            _historical_candles = _win

        stored = _historical_candles

        return {
            "success": True,
            "contract_id": contract_id,
            "contracts": fetch_contracts,
            "contract_counts": contract_counts,
            "continuous": continuous_meta,
            "candles_count": len(stored),
            "fetched_count": sum(contract_counts.values()),
            "from_store": from_store,
            "interval": f"{req.unit_number}{'m' if req.unit == 2 else 's'}",
            "first": stored[0].timestamp.isoformat() if stored else None,
            "last": stored[-1].timestamp.isoformat() if stored else None,
        }

    except Exception as e:
        logger.error(f"Fetch failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/data/aggregate")
async def aggregate_data():
    """將已拉取的 1 分鐘數據聚合為 5 分鐘"""
    global _historical_candles

    if not _historical_candles:
        raise HTTPException(status_code=400, detail="Fetch historical data first")

    candles_5m = BacktestEngine.aggregate_1m_to_5m(_historical_candles)

    return {
        "source_count": len(_historical_candles),
        "aggregated_count": len(candles_5m),
        "interval": "5m",
    }


_BT_PROGRESS_FILE = Path("data") / "backtest_progress.json"
_bt_progress_state = {
    "status": "idle",
    "stage": "idle",
    "current": 0,
    "total": 0,
    "detail": "",
    "updated_at": 0.0,
}
_bt_progress_file_cache = {
    "read_at": 0.0,
    "data": None,
}


def _update_bt_progress(stage: str, current: int = 0, total: int = 0,
                        detail: str = "", status: str = "running") -> None:
    global _bt_progress_state
    state = {
        "status": status,
        "stage": stage,
        "current": int(current),
        "total": int(total),
        "detail": detail,
        "updated_at": datetime.now(timezone.utc).timestamp(),
    }
    _bt_progress_state = state
    try:
        _BT_PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _BT_PROGRESS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(_BT_PROGRESS_FILE)
    except Exception:
        pass


def _bt_candle_key(candles):
    return (len(candles),
            candles[0].timestamp.isoformat() if candles else None,
            candles[-1].timestamp.isoformat() if candles else None)


def _get_bt_executor():
    global _bt_executor
    if _bt_executor is None:
        from concurrent.futures import ProcessPoolExecutor
        _bt_executor = ProcessPoolExecutor(max_workers=1)
        logger.info("[Confluence] backtest process pool started (1 worker)")
    return _bt_executor


@router.post("/backtest/run", response_model=BacktestResponse)
async def run_backtest(req: BacktestRequest):
    """
    執行回測

    流程：
    1. 用已拉取的歷史數據（或聚合後的 5m 數據）
    2. 餵入回測引擎
    3. 返回績效、交易列表、盤整區間、equity curve
    """
    global _historical_candles, _backtest_results

    _strat = str(req.strategy or "").strip().lower()
    _sess = getattr(req, "tr_allowed_sessions", None) or getattr(req, "conf_allowed_sessions", None)
    logger.info(
        "[BACKTEST] strategy=%s  session=%s  TF=%s  RR=%s  SL=%s  confirm=%s",
        _strat,
        _sess,
        getattr(req, "area_timeframe", "?"),
        getattr(req, "rr_ratio", "?"),
        getattr(req, "sl_ticks", "?"),
        getattr(req, "breakout_confirm_bars", "?"),
    )

    if not _historical_candles:
        raise HTTPException(
            status_code=400,
            detail="Fetch data through /api/data/fetch-historical first"
        )


    # 1.0.9: confluence/ML removed wholesale (docs/1.0.9_DELETE_LIST.md)
    return await _run_trend_backtest(req)


async def _run_trend_backtest(req: BacktestRequest) -> BacktestResponse:
    """Run the trend backtest path and always return BacktestResponse."""
    global _historical_candles, _backtest_results

    await _refresh_recent_historical_candles(req.contract_id)

    # v1.0.6: derive symbol + per-contract fees from the chosen contract_id so
    # the trade journal shows /MNQ when MNQ is selected and 10xMNQ doesn't get
    # stuck paying 10x the NQ Mini fee schedule.
    contract_size = _normalize_contract_size(req.contract_id, req.contract_size)
    value_area_pct = _normalize_value_area_pct(req.value_area_pct)
    strategy_name = _normalize_strategy_name(req.strategy)

    bt_symbol = _extract_symbol(req.contract_id)
    config = BacktestConfig(
        strategies=[strategy_name],
        initial_capital=req.initial_capital,
        symbol=bt_symbol,
        commission_rt=get_commission_rt(req.contract_id),
        fees_rt=get_fees_rt(req.contract_id),
        value_area_pct=value_area_pct,
    )

    strategy_params = _build_strategy_params_from_request(req, contract_size)
    # 1.0.9: zone timeline 是最慢的 detector 全掃(數十秒~數分鐘),只有「用
    # 共識 zone 進場」的策略才需要。原本用「不是 sigma / fade / pmo / factor」
    # 的黑名單寫法 —— 每加一個新策略就會忘記加進去,結果新策略卡在建 zone
    # (INTRAMOM 上線時就踩到)。改成白名單:只有明確需要的才建。
    #
    # 目前沒有任何策略需要 —— 唯一的消費者 TREND 已在 1.0.9 移除;
    # fade 自己算前日 VP、factor/pmo/sigma/intramom 完全不看 zone。
    # 未來若有新策略需要,把它的 strategy 值加進 _ZONE_TIMELINE_STRATEGIES。
    _strategy_name = str(getattr(strategy_params, "strategy", "") or "").lower()
    needs_zone_timeline = _strategy_name in _ZONE_TIMELINE_STRATEGIES

    method = str(getattr(req, "method", "single") or "single").lower()
    tf_combo = tuple(t for t in (getattr(req, "tf_combo", None) or []) if t in ML_TIMEFRAMES)
    overlap_mode = method == "overlap" and len(tf_combo) >= 2
    overlap_trade_tf = _normalize_tr_overlap_trade_tf(
        getattr(strategy_params, "tr_overlap_trade_tf", "merged")
    )

    zone_timeline = None
    if overlap_mode and needs_zone_timeline:
        ordered = [tf for tf in ML_TIMEFRAMES if tf in tf_combo]
        ov_candles = sorted(_historical_candles, key=lambda c: c.timestamp)
        _update_bt_progress(
            "building zone timeline", 0, len(ov_candles),
            f"{len(ordered)} timeframe(s) over {len(ov_candles)} candles",
        )

        def _build_overlap_timeline():
            return _get_merged_zone_timeline(
                ov_candles, value_area_pct, False, tuple(ordered), overlap_trade_tf,
            )

        zone_timeline = await asyncio.to_thread(_build_overlap_timeline)
    elif (needs_zone_timeline
          and str(getattr(strategy_params, "area_timeframe", "15m") or "15m").lower() != "session"):
        sg_candles = sorted(_historical_candles, key=lambda c: c.timestamp)
        _update_bt_progress(
            "building zone timeline", 0, len(sg_candles),
            f"single {strategy_params.area_timeframe} over {len(sg_candles)} candles",
        )
        zone_timeline = await asyncio.to_thread(
            _get_precomputed_zone_timeline,
            sg_candles, value_area_pct, False, strategy_params.area_timeframe,
        )

    engine = BacktestEngine(
        config,
        strategy_params=strategy_params,
        zone_timeline=zone_timeline,
    )

    candles = (
        sorted(_historical_candles, key=lambda c: c.timestamp)
        if zone_timeline is not None
        else list(_historical_candles)
    )

    _update_bt_progress("running", 0, len(candles), "Backtest in progress...")

    def _trend_progress(current, total, detail):
        _update_bt_progress("running", current, total, detail)

    result = await asyncio.to_thread(engine.run, candles, _trend_progress)
    _update_bt_progress("done", len(candles), len(candles), "Complete", status="done")
    _backtest_results.append(result)

    trades_resp = []
    symbol_label = "/" + config.symbol
    for t in result.trades:
        meta = getattr(t, "meta", None) or {}
        trades_resp.append(TradeResponse(
            trade_id=t.trade_id,
            strategy=t.strategy.value,
            symbol=symbol_label,
            size=t.contracts,
            direction=t.direction.value,
            entry_price=t.entry_price,
            entry_time=t.entry_time.isoformat(),
            exit_price=t.exit_price,
            exit_time=t.exit_time.isoformat() if t.exit_time else None,
            sl_price=t.sl_price,
            tp_price=t.tp_price,
            original_sl_price=getattr(t, "original_sl_price", None) or t.sl_price,
            original_tp_price=getattr(t, "original_tp_price", None) or t.tp_price,
            pnl=t.pnl,
            commission=t.commission,
            fees=t.fees,
            exit_reason=t.exit_reason.value if t.exit_reason else None,
            zone_id=t.zone_id,
            zone_source=getattr(t, "zone_source", None),
            mode=meta.get("mode"),
            side=meta.get("side"),
            largest_tf=meta.get("largest_tf"),
            risk_tf=meta.get("risk_tf"),
            decision_tfs=meta.get("decision_tfs") or [],
            overlap_tfs=meta.get("overlap_tfs") or [],
            trade_tf=meta.get("trade_tf"),
            wall_id=meta.get("wall_id"),
            labels=meta.get("labels") or [],
            primary_zone=meta.get("primary_zone"),
            or_range=meta.get("or_range"),
            reason=meta.get("signal_reason") or meta.get("reason"),
            vol_ratio=t.vol_ratio,
            is_big_trend=t.is_big_trend,
        ))

    if overlap_mode and zone_timeline:
        seen_ids = set()
        merged_zones = []
        for entry in zone_timeline:
            mz = entry.get("active")
            if mz is not None and mz.zone_id not in seen_ids:
                seen_ids.add(mz.zone_id)
                merged_zones.append(mz)
        result_zones = merged_zones
    else:
        result_zones = result.zones

    zones_resp = []
    vp_calc = VolumeProfileCalculator(tick_size=0.25, value_area_pct=value_area_pct)
    for z in result_zones:
        profile_data = None
        if z.candles:
            try:
                vp = vp_calc.calculate(z.candles)
                max_vol = (max(vp.profile.values()) if vp.profile else 0) or 1
                profile_data = [
                    {"price": p, "volume": v, "pct": round(v / max_vol, 3)}
                    for p, v in sorted(vp.profile.items())
                ]
            except Exception:
                profile_data = []
        elif getattr(z, "profile", None):
            try:
                prof = z.profile or {}
                max_vol = (max(prof.values()) if prof else 0) or 1
                profile_data = [
                    {"price": p, "volume": v, "pct": round(v / max_vol, 3)}
                    for p, v in sorted(prof.items())
                ]
            except Exception:
                profile_data = []

        zones_resp.append(ZoneResponse(
            zone_id=z.zone_id,
            formed_at=z.formed_at.isoformat(),
            left_at=z.left_at.isoformat() if z.left_at else None,
            poc=z.poc,
            vah_80=z.vah_80,
            val_80=z.val_80,
            high_100=z.high_100,
            low_100=z.low_100,
            total_volume=z.total_volume,
            duration_minutes=z.duration_minutes,
            num_candles=z.num_candles,
            status=z.status.value,
            exit_direction=z.exit_direction,
            profile=profile_data,
            timeframe=getattr(z, "timeframe", "1m"),
            parent_zone_id=getattr(z, "parent_zone_id", None),
            mature=getattr(z, "mature", False),
            va_curve=getattr(z, "va_curve", None) or None,
        ))

    m = result.metrics

    def _sub_resp(sm):
        if not sm:
            return None
        return SubMetricsResponse(
            total_trades=sm.total_trades, wins=sm.wins, losses=sm.losses,
            win_rate=sm.win_rate, avg_win=sm.avg_win, avg_loss=sm.avg_loss,
            avg_rr_ratio=sm.avg_rr_ratio, total_pnl=sm.total_pnl,
            profit_factor=sm.profit_factor,
        )

    metrics_resp = MetricsResponse(
        total_trades=m.total_trades,
        wins=m.wins,
        losses=m.losses,
        win_rate=m.win_rate,
        avg_win=m.avg_win,
        avg_loss=m.avg_loss,
        avg_rr_ratio=m.avg_rr_ratio,
        expectancy=m.expectancy,
        max_drawdown=m.max_drawdown,
        max_drawdown_pct=m.max_drawdown_pct,
        calmar_ratio=m.calmar_ratio,
        profit_factor=m.profit_factor,
        max_consecutive_losses=m.max_consecutive_losses,
        total_pnl=m.total_pnl,
        total_gain=getattr(m, "total_gain", 0.0),
        total_loss=getattr(m, "total_loss", 0.0),
        daily_pnl=m.daily_pnl or {},
        post_breakout_sample_size=getattr(m, "post_breakout_sample_size", 0),
        post_breakout_avg_max_fav_ticks=getattr(m, "post_breakout_avg_max_fav_ticks", 0.0),
        post_breakout_avg_max_adv_ticks=getattr(m, "post_breakout_avg_max_adv_ticks", 0.0),
        post_breakout_tp_clean=getattr(m, "post_breakout_tp_clean", 0),
        post_breakout_tp_after_trail=getattr(m, "post_breakout_tp_after_trail", 0),
        post_breakout_tp_after_sl=getattr(m, "post_breakout_tp_after_sl", 0),
        current_zone_trades=getattr(m, "current_zone_trades", 0),
        current_zone_wins=getattr(m, "current_zone_wins", 0),
        current_zone_win_rate=getattr(m, "current_zone_win_rate", 0.0),
        current_zone_avg_pnl=getattr(m, "current_zone_avg_pnl", 0.0),
        current_zone_total_pnl=getattr(m, "current_zone_total_pnl", 0.0),
        weekly_stats=_weekly_stats(m.daily_pnl or {}),
        trend_follow=_sub_resp(m.trend_follow_metrics),
    )

    equity = [
        [ts.timestamp() * 1000, val]
        for ts, val in result.equity_curve
    ]

    try:
        _write_backtest_csv(req, config, strategy_params, method, tf_combo,
                            trades_resp, metrics_resp)
    except Exception as exc:
        logger.warning("Backtest CSV export failed: %s", exc)

    return BacktestResponse(
        metrics=metrics_resp,
        trades=trades_resp,
        zones=zones_resp,
        equity_curve=equity,
    )


# ── 1.0.8: 高效參數掃描(0.15.0 sweep 回歸版,timeline 快路徑)────────
_SWEEP_RESULTS_FILE = Path("data") / "sweep_results.json"
_sweep_running = False
_LATEST_SWEEP_PRESET_PREFIX = "SWEEP "


def _sync_latest_sweep_presets(payload: dict, req: BacktestRequest, contract_size: int) -> list[str]:
    results = list((payload or {}).get("results") or [])
    if not results:
        return []
    grouped: dict[str, list[dict]] = {}
    for row in results:
        grouped.setdefault(str(row.get("model") or "TREND").upper(), []).append(row)

    def _rank(row: dict) -> tuple:
        return (
            1 if row.get("accept") else 0,
            1 if row.get("wf_pass") else 0,
            float(row.get("pf") or 0.0),
            float(row.get("score") or 0.0),
            float(row.get("pnl") or 0.0),
            -float(row.get("max_dd") or 0.0),
        )

    model_to_strategy = {
        "TREND": "trend",
        "DAY ZONE": "fade",
        "DISTRIBUTION": "sigma",
        "FACTOR": "factor",
    }
    model_order = ["FACTOR", "DISTRIBUTION", "DAY ZONE", "TREND"]

    def _factor_family_key(row: dict) -> str:
        params = row.get("params") or {}
        return str(params.get("factor_signal_family") or row.get("label") or "factor").lower()

    def _factor_pf_rank(row: dict) -> tuple:
        return (
            float(row.get("pf") or 0.0),
            1 if row.get("wf_pass") else 0,
            float(row.get("pnl") or 0.0),
            -float(row.get("max_dd") or 0.0),
            float(row.get("score") or 0.0),
        )

    def _select_latest_rows(model: str, rows: list[dict]) -> list[dict]:
        ranked = sorted(rows, key=_rank, reverse=True)
        if model != "FACTOR":
            return ranked[:3]
        best_by_family: dict[str, dict] = {}
        for row in sorted(rows, key=_factor_pf_rank, reverse=True):
            family = _factor_family_key(row)
            if family not in best_by_family:
                best_by_family[family] = row
        return sorted(best_by_family.values(), key=_factor_pf_rank, reverse=True)[:3]

    data = _load_presets_file()
    presets = data.setdefault("presets", {})
    previous_latest = set(str(n) for n in (data.get("latest_sweep_presets") or []))
    for name in list(presets.keys()):
        if name in previous_latest or str(name).startswith(_LATEST_SWEEP_PRESET_PREFIX):
            presets.pop(name, None)

    created: list[str] = []
    fallback_cid = current_quarterly_contract_id("MNQ")
    contract_id = normalize_contract_id_to_front(getattr(req, "contract_id", "") or fallback_cid)
    try:
        raw_created = str((payload or {}).get("created_at") or "")
        sweep_dt = datetime.fromisoformat(raw_created.replace("Z", "+00:00")).astimezone()
    except Exception:
        sweep_dt = datetime.now(timezone.utc).astimezone()
    date_prefix = sweep_dt.strftime("%m%d")
    for model in model_order:
        rows = _select_latest_rows(model, grouped.get(model, []))
        for idx, row in enumerate(rows, start=1):
            row_params = dict(row.get("preset_params") or row.get("params") or {})
            strategy = str(row_params.get("strategy") or model_to_strategy.get(model, "trend"))
            row_params["strategy"] = strategy
            params = dict(_DEFAULT_PRESET_PARAMS)
            params.update({
                "strategy": strategy,
                "contract_id": contract_id,
                "contract_size": int(contract_size),
                "candle_seconds": int(getattr(req, "candle_seconds", 60) or 60),
                "area_timeframe": row_params.get("area_timeframe", "15m"),
                "method": row_params.get("method", "single"),
                "tf_combo": row_params.get("tf_combo", []),
            })
            params.update(row_params)
            label = " ".join(str(row.get("label") or "").replace("#", "").split())[:48]
            name = (
                f"{date_prefix} {model} #{idx} "
                f"{label} PF{float(row.get('pf') or 0.0):.2f}"
            )
            presets[name] = params
            created.append(name)
    data["latest_sweep_presets"] = created
    data["latest_sweep_created_at"] = str((payload or {}).get("created_at") or datetime.now(timezone.utc).isoformat())
    _save_presets_file(data)
    return created


@router.post("/backtest/sweep")
async def run_backtest_sweep(req: BacktestRequest = BacktestRequest()):
    """跑完整 multi-model 參數掃描(TREND / DAY ZONE / DISTRIBUTION),結果持久化供 SWEEP 分頁。"""
    global _sweep_running
    if _sweep_running:
        raise HTTPException(400, "A sweep is already running")
    if not _historical_candles:
        raise HTTPException(400, "No historical data — connect and load data first")

    from backend.backtest.sweep import run_model_sweep

    _sweep_running = True
    try:
        await _refresh_recent_historical_candles(req.contract_id)
        contract_size = _normalize_contract_size(req.contract_id, req.contract_size)
        base = _build_strategy_params_from_request(req, contract_size)
        candles = sorted(_historical_candles, key=lambda c: c.timestamp)
        _update_bt_progress("sweeping", 0, 1, "preparing")

        def _progress(cur, total, detail):
            _update_bt_progress("sweeping", cur, total, detail)

        results = await asyncio.to_thread(run_model_sweep, candles, base, _progress, getattr(req, 'sweep_models', None))
        results.sort(key=lambda r: -r.get("score", 0.0))
        qualified_by_model = {"TREND": [], "DAY ZONE": [], "DISTRIBUTION": [], "FACTOR": []}
        for r in results:
            if r.get("accept"):
                model = str(r.get("model") or "TREND").upper()
                qualified_by_model.setdefault(model, []).append(r)
        payload = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "candles": len(candles),
            "range": [
                candles[0].timestamp.isoformat(),
                candles[-1].timestamp.isoformat(),
            ],
            "results": results,
            "qualified_by_model": qualified_by_model,
        }
        try:
            payload["latest_sweep_presets"] = _sync_latest_sweep_presets(payload, req, contract_size)
        except Exception as e:
            logger.warning(f"sync latest sweep presets failed: {e}")
        _SWEEP_RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SWEEP_RESULTS_FILE.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        # 1.0.9: 長期記錄 — 每次 sweep 存時間戳全檔 + 追加摘要到 sweep_history.jsonl
        try:
            import gc as _gc
            runs_dir = Path("data") / "sweep_runs"
            runs_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
            (runs_dir / f"sweep_{stamp}.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            top = results[0] if results else {}
            hist = {
                "created_at": payload["created_at"], "stamp": stamp,
                "candles": len(candles), "range": payload["range"],
                "variants": len(results),
                "accepted": sum(len(v) for v in qualified_by_model.values()),
                "by_model": {m: len(v) for m, v in qualified_by_model.items()},
                "top": {"model": top.get("model"), "label": top.get("label"),
                        "pf": top.get("pf"), "pnl": top.get("pnl"),
                        "max_dd": top.get("max_dd"), "trades": top.get("trades")},
            }
            with (Path("data") / "sweep_history.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(hist, ensure_ascii=False) + "\n")
            _gc.collect()   # 掃描後釋放記憶體
        except Exception as e:
            logger.warning(f"sweep archive failed: {e}")
        _update_bt_progress("done", 1, 1, "Sweep complete", status="done")
        return payload
    finally:
        _sweep_running = False


@router.get("/backtest/sweep/history")
async def get_backtest_sweep_history(limit: int = 50):
    """1.0.9: 近 N 次 sweep 的長期摘要記錄(data/sweep_history.jsonl)。"""
    path = Path("data") / "sweep_history.jsonl"
    rows: List[dict] = []
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            rows.append(json.loads(line))
                        except Exception:
                            continue
    except Exception as e:
        logger.warning(f"read sweep history failed: {e}")
    rows = rows[-max(1, int(limit)):][::-1]   # 最新在前
    return {"runs": rows, "count": len(rows)}


@router.get("/backtest/sweep/results")
async def get_backtest_sweep_results():
    """回傳最近一次 sweep 結果(啟動時 SWEEP 分頁自動載入)。"""
    try:
        if _SWEEP_RESULTS_FILE.exists():
            return json.loads(_SWEEP_RESULTS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"read sweep results failed: {e}")
    return {
        "results": [],
        "qualified_by_model": {"TREND": [], "DAY ZONE": [], "DISTRIBUTION": [], "FACTOR": []},
        "created_at": None,
    }


# ── 1.0.9 P0: 影子重放(實盤 vs 同參數回測 逐筆對賬)──────────────
# 1.0.9 BUGFIX: 用完整歷史(不裁窗口),單 TF 走快取 timeline 保速度、正確性優先。
def _shadow_timeline_provider(candles):
    def _provide(params):
        try:
            method = str(getattr(params, "method", "single") or "single").lower()
            area = str(getattr(params, "area_timeframe", "15m") or "15m").lower()
            combo = list(getattr(params, "tf_combo", None) or [])
            if (method == "overlap" and len(combo) >= 2) or area == "session":
                return None      # overlap/session 走引擎內建 detector(正確性優先)
            return _get_precomputed_zone_timeline(
                candles, float(getattr(params, "value_area_pct", 0.80) or 0.80),
                False, area,
            )
        except Exception as e:
            logger.warning(f"shadow timeline provider failed: {e}")
            return None
    return _provide


def _run_shadow_replay_sync(trade_date: Optional[str] = None) -> dict:
    from backend.backtest.shadow_replay import run_shadow_replay
    from backend.data import candle_store

    candles = _historical_candles
    if not candles:
        candles = candle_store.load("MNQ", 1)
    candles = sorted(candles, key=lambda c: c.timestamp)
    return run_shadow_replay(candles, trade_date, _shadow_timeline_provider(candles))


@router.post("/live/shadow-replay")
async def shadow_replay_run(trade_date: Optional[str] = None):
    """手動觸發影子重放(預設 = 最新交易日)。"""
    report = await asyncio.to_thread(_run_shadow_replay_sync, trade_date)
    return report


@router.get("/live/shadow-replay/recent")
async def shadow_replay_recent(n: int = 14):
    """近 n 份影子重放日報 + P0 連續通過統計。"""
    from backend.backtest.shadow_replay import PASS_MATCH_RATE, load_recent_reports
    reports = load_recent_reports(n)
    streak = 0
    for r in reversed(reports):
        if r.get("day_pass"):
            streak += 1
        else:
            break
    return {
        "reports": reports,
        "pass_line": PASS_MATCH_RATE,
        "consecutive_pass_days": streak,
        "p0_cleared": streak >= 10,   # ~2 週交易日
    }


async def shadow_replay_daily_task():
    """1.0.9 P0: 每日 20:10 UTC(flatten 後)自動跑影子重放並寫日報。"""
    from datetime import timedelta as _td
    while True:
        try:
            now = datetime.now(timezone.utc)
            target = now.replace(hour=20, minute=10, second=0, microsecond=0)
            if target <= now:
                target += _td(days=1)
            await asyncio.sleep((target - now).total_seconds())
            if not _historical_candles:
                logger.info("[shadow] No historical candles; skipping today's shadow replay")
                continue
            report = await asyncio.to_thread(_run_shadow_replay_sync, None)
            ok = report.get("day_pass")
            summary = " | ".join(
                f"{r.get('snapshot_id','?')[:8]} live={r.get('live_n')} bt={r.get('bt_n')} "
                f"match={r.get('match_rate')}"
                for r in report.get("reports", []) if "error" not in r
            ) or report.get("note", "no data")
            logger.warning(
                f"[shadow] {report.get('date')} shadow replay "
                f"{'PASS ✅' if ok else 'FAIL ❌'} | {summary}"
            )
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("[shadow] daily task error")
            await asyncio.sleep(600)

# Columns written to data/backtest/backtest_<timestamp>.csv (one row per trade).
_BACKTEST_CSV_COLUMNS = [
    "trade_id", "strategy", "symbol", "direction", "size",
    "entry_time", "entry_price", "exit_time", "exit_price",
    "sl_price", "tp_price", "original_sl_price", "original_tp_price",
    "exit_reason", "pnl", "commission", "fees",
    "zone_id", "zone_source", "vol_ratio", "is_big_trend",
    "decision_tfs", "overlap_tfs", "trade_tf",
]


def _write_backtest_csv(req, config, strategy_params, method, tf_combo,
                        trades_resp, metrics_resp) -> Path:
    """Write the trade journal + a run-summary header to data/backtest/.

    Returns the path of the per-trade CSV. A companion *_summary.csv carries
    the config + headline metrics so each run is self-describing.
    """
    out_dir = Path(__file__).resolve().parents[2] / "data" / "backtest"
    out_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    tf_label = "+".join(tf_combo) if (method == "overlap" and tf_combo) else (
        getattr(req, "area_timeframe", None) or "1m")
    rr = getattr(req, "rr_ratio", None)
    base = f"backtest_{method}_{tf_label.replace('+', '-')}_rr{rr}_{stamp}"

    trades_path = out_dir / f"{base}.csv"
    with open(trades_path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=_BACKTEST_CSV_COLUMNS,
                                extrasaction="ignore")
        writer.writeheader()
        for t in trades_resp:
            row = t.model_dump() if hasattr(t, "model_dump") else t.dict()
            writer.writerow({k: row.get(k) for k in _BACKTEST_CSV_COLUMNS})

    # Run summary (config + headline metrics + weekly variation).
    wk = metrics_resp.weekly_stats or {}
    summary = {
        "generated": stamp,
        "method": method,
        "timeframes": tf_label,
        "rr_ratio": rr,
        "contract_id": getattr(req, "contract_id", None),
        "contract_size": _normalize_contract_size(
            getattr(req, "contract_id", None), getattr(req, "contract_size", None)),
        "value_area_pct": config.value_area_pct,
        "total_trades": metrics_resp.total_trades,
        "wins": metrics_resp.wins,
        "losses": metrics_resp.losses,
        "win_rate": metrics_resp.win_rate,
        "total_pnl": metrics_resp.total_pnl,
        "total_gain": metrics_resp.total_gain,
        "total_loss": metrics_resp.total_loss,
        "max_drawdown": metrics_resp.max_drawdown,
        "calmar_ratio": metrics_resp.calmar_ratio,
        "profit_factor": metrics_resp.profit_factor,
        "avg_rr_ratio": metrics_resp.avg_rr_ratio,
        "weekly_count": wk.get("weekly_count", 0),
        "weekly_mean": wk.get("weekly_mean", 0.0),
        "weekly_std": wk.get("weekly_std", 0.0),
        "weekly_cv": wk.get("weekly_cv", 0.0),
        "weekly_min": wk.get("weekly_min", 0.0),
        "weekly_max": wk.get("weekly_max", 0.0),
        "weekly_consistency": wk.get("weekly_consistency", 0.0),
    }
    summary_path = out_dir / f"{base}_summary.csv"
    with open(summary_path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(list(summary.keys()))
        writer.writerow(list(summary.values()))

    logger.info("Backtest CSV written: %s (%d trades)", trades_path, len(trades_resp))
    return trades_path


@router.get("/backtest/results")
async def list_backtests():
    """列出所有回測結果摘要"""
    return [
        {
            "index": i,
            "total_trades": r.metrics.total_trades,
            "win_rate": r.metrics.win_rate,
            "total_pnl": r.metrics.total_pnl,
            "max_drawdown": r.metrics.max_drawdown,
        }
        for i, r in enumerate(_backtest_results)
    ]


# 1.0.8: 移除 ML sweep 狀態快取 (_ml_results_cache/_ml_progress)


def _request_payload(model: BaseModel) -> dict:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


# 1.0.8: 移除 _json_safe + ML 排序/篩選 helper (_ml_total_loss/_ml_profit_factor/_ml_valid_trade_range/_enrich_ml_result)


def _weekly_stats(daily_pnl: Dict[str, float]) -> dict:
    """Group daily PnL by ISO week and measure week-to-week variation.

    Returns: weekly_pnls (list, chronological), weekly_count, weekly_mean,
    weekly_std (population std-dev of weekly PnL), weekly_cv (coefficient of
    variation = std/|mean|), weekly_min, weekly_max, weekly_range,
    positive_weeks, weekly_consistency (fraction of weeks with PnL > 0).
    """
    empty = {
        "weekly_pnls": [], "weekly_count": 0, "weekly_mean": 0.0,
        "weekly_std": 0.0, "weekly_cv": 0.0, "weekly_min": 0.0,
        "weekly_max": 0.0, "weekly_range": 0.0, "positive_weeks": 0,
        "weekly_consistency": 0.0,
    }
    if not daily_pnl:
        return empty

    buckets: Dict[str, float] = {}
    for day, pnl in daily_pnl.items():
        try:
            dt = datetime.fromisoformat(str(day)[:10])
        except (TypeError, ValueError):
            continue
        iso = dt.isocalendar()
        key = f"{iso[0]}-W{iso[1]:02d}"
        try:
            buckets[key] = buckets.get(key, 0.0) + float(pnl or 0)
        except (TypeError, ValueError):
            continue

    if not buckets:
        return empty

    weekly = [buckets[k] for k in sorted(buckets.keys())]
    n = len(weekly)
    mean = sum(weekly) / n
    var = sum((w - mean) ** 2 for w in weekly) / n
    std = math.sqrt(var)
    cv = (std / abs(mean)) if mean else 0.0
    positive = sum(1 for w in weekly if w > 0)
    return {
        "weekly_pnls": [round(w, 2) for w in weekly],
        "weekly_count": n,
        "weekly_mean": round(mean, 2),
        "weekly_std": round(std, 2),
        "weekly_cv": round(cv, 4),
        "weekly_min": round(min(weekly), 2),
        "weekly_max": round(max(weekly), 2),
        "weekly_range": round(max(weekly) - min(weekly), 2),
        "positive_weeks": positive,
        "weekly_consistency": round(positive / n, 3),
    }


# 1.0.8: 移除 ML CSV 匯出 + artifact 儲存 + 排序/載入 (_ml_csv_row/_save_ml_artifacts/_save_conf_combo_artifacts/_ml_sort_value/_sorted_ml_results/_load_ml_artifact)


def _precompute_zone_timeline(
    candles: list,
    value_area_pct: float = 0.80,
    skip_zone_stability: bool = False,
    area_timeframe: str = "15m",
) -> list:
    """Run the clock-bucket zone detector ONCE on all candles.
    Returns a list[dict] — one entry per candle — with pre-computed zone state:
      - 'active': most recent completed reference zone
      - 'recent': up to 10 recent reference zones (for multi-zone breakout)
      - 'mature': whether at least one reference zone exists
    Slim zones (candles list stripped) are safe for strategy evaluation; the VP
    histogram (`profile`) is preserved for the lowest-volume-node SL.
    """
    import copy
    from backend.strategy.consolidation import build_zone_detector

    value_area_pct = _normalize_value_area_pct(value_area_pct)
    area_timeframe = _normalize_area_timeframe(area_timeframe)
    detector = build_zone_detector(
        area_timeframe=area_timeframe,
        value_area_pct=value_area_pct,
        max_recent=10,
    )
    timeline = []

    # Completed/reference zones are immutable after finalisation, and each one
    # appears in ~50 consecutive timeline entries (as 'active' then in 'recent').
    # Memoise the slim copy by unique zone_id so each zone is copied ONCE instead
    # of ~50×. On full-range data this collapses millions of zone copies and is
    # the single biggest ML RAM saving.
    _slim_cache: dict = {}

    def _slim(z):
        if z is None:
            return None
        cached = _slim_cache.get(z.zone_id)
        if cached is not None:
            return cached
        c = copy.copy(z)
        c.candles = []   # strip raw candle list — strategy only reads price levels + profile
        _slim_cache[z.zone_id] = c
        return c

    for candle in candles:
        detector.update(candle)
        recent = [_slim(z) for z in detector.get_recent_zones(10)]
        timeline.append({
            'active': _slim(detector.get_active_zone()),
            'recent': recent,
            'mature': detector.is_zone_mature,
        })

    return timeline


# ── Overlap-mode zone-timeline cache (trend backtest speedup) ─────────────────
# The detector pass above is the slow part (~minutes over full history) and
# depends ONLY on candle data + value-area + area timeframe — NOT on RR / SL /
# session / confirm. So re-running the trend backtest with different params on
# the SAME data can reuse it (mirrors the confluence path's _get_conf_timeline).
# Keyed per timeframe; the whole cache is dropped when the candle set changes
# (new fetch), so stale data never gets reused.
_zone_timeline_cache: dict = {}
_zone_timeline_cache_sig = None
_merged_zone_timeline_cache: dict = {}
_merged_zone_timeline_cache_sig = None
_MERGED_ZONE_TIMELINE_CACHE_MAX = 4


def _candles_sig(candles):
    return (len(candles),
            candles[0].timestamp if candles else None,
            candles[-1].timestamp if candles else None)


def _get_precomputed_zone_timeline(candles, value_area_pct, skip_zone_stability,
                                   area_timeframe):
    """Cached wrapper around _precompute_zone_timeline (per area_timeframe)."""
    global _zone_timeline_cache, _zone_timeline_cache_sig
    sig = _candles_sig(candles)
    if sig != _zone_timeline_cache_sig:
        _zone_timeline_cache = {}            # candle set changed → invalidate all
        _zone_timeline_cache_sig = sig
    key = (_normalize_value_area_pct(value_area_pct), bool(skip_zone_stability),
           _normalize_area_timeframe(area_timeframe))
    cached = _zone_timeline_cache.get(key)
    if cached is not None:
        logger.info(f"[Trend] reusing cached zone timeline tf={key[2]} "
                    f"({len(candles)} candles) — skipped rebuild")
        return cached
    import time as _t
    _t0 = _t.perf_counter()
    logger.info(f"[Trend] building zone timeline tf={key[2]} over {len(candles)} candles…")
    timeline = _precompute_zone_timeline(
        candles, value_area_pct, skip_zone_stability, area_timeframe,
    )
    logger.info(f"[Trend] zone timeline tf={key[2]} built in {_t.perf_counter() - _t0:.1f}s")
    _zone_timeline_cache[key] = timeline
    return timeline


def _get_merged_zone_timeline(candles, value_area_pct, skip_zone_stability,
                              tfs, overlap_trade_tf):
    """Cached merged overlap timeline for Trend overlap backtests.

    Per-TF timelines are already cached; this also caches the full merged result
    so rerunning the same TF combo/risk settings avoids the O(N candles) merge.
    """
    global _merged_zone_timeline_cache, _merged_zone_timeline_cache_sig
    sig = _candles_sig(candles)
    if sig != _merged_zone_timeline_cache_sig:
        _merged_zone_timeline_cache = {}
        _merged_zone_timeline_cache_sig = sig

    ordered = tuple(tf for tf in ML_TIMEFRAMES if tf in set(tfs or ()))
    key = (
        _normalize_value_area_pct(value_area_pct),
        bool(skip_zone_stability),
        ordered,
        _normalize_tr_overlap_trade_tf(overlap_trade_tf),
    )
    cached = _merged_zone_timeline_cache.get(key)
    if cached is not None:
        logger.info(
            "[Trend] reusing cached merged overlap timeline tf=%s trade=%s (%s candles)",
            "+".join(ordered),
            key[3],
            len(candles),
        )
        return cached

    import time as _t
    _t0 = _t.perf_counter()
    timelines = [
        _get_precomputed_zone_timeline(candles, value_area_pct, skip_zone_stability, tf)
        for tf in ordered
    ]
    merged = _merge_zone_timelines(timelines, ordered, key[3])
    logger.info(
        "[Trend] merged overlap timeline tf=%s trade=%s built in %.1fs",
        "+".join(ordered),
        key[3],
        _t.perf_counter() - _t0,
    )
    _merged_zone_timeline_cache[key] = merged
    while len(_merged_zone_timeline_cache) > _MERGED_ZONE_TIMELINE_CACHE_MAX:
        _merged_zone_timeline_cache.pop(next(iter(_merged_zone_timeline_cache)))
    return merged


# 1.0.8: 移除 MLRunRequest / ConfComboRunRequest 請求模型


ML_TIMEFRAMES = ("15m", "30m", "1h", "4h")
ML_RR_VALUES = tuple(range(1, 7))   # 1:1 .. 1:6


# ── COMBINATION (confluence Model+Style sweep) grid ───────────────────
# 6 × 2 × 4 × 3 × 2 = 288 runs. Structural MODEL knobs (band / min-distinct-tf /
# min-prob / ev-floor / timeframes / trail-lock) are HELD at the panel values;
# only the suspects that move win-rate/edge are swept.
# 1.0.8: 移除 CONF_COMBO_* 掃描網格 + _ml_timeframe_combos


def _synthesize_merged_zone(actives, tfs, overlap_trade_tf: str = "merged"):
    """Average the overlapping reference zones into one synthetic zone.
    Entry levels (VAH/VAL/POC) are the mean across timeframes; the VP
    histogram is summed so the lowest-volume-node SL still works.
    """
    from backend.db.models import ConsolidationZone, ZoneStatus
    import copy
    ids = [str(z.zone_id) for z in actives]
    if _normalize_tr_overlap_trade_tf(overlap_trade_tf) == "smallest":
        zone = copy.copy(actives[0])
        zone.candles = []
        zone.zone_id = "OS:" + "+".join(tfs) + ":" + "+".join(ids)
        zone.parent_zone_id = "+".join(ids)
        zone.timeframe = tfs[0]
        return zone

    n = len(actives)
    vah = sum(z.vah_80 for z in actives) / n
    val = sum(z.val_80 for z in actives) / n
    poc = sum(z.poc for z in actives) / n
    profile = {}
    for z in actives:
        for p, v in (z.profile or {}).items():
            profile[p] = profile.get(p, 0) + v
    zid = "M:" + "+".join(ids)
    return ConsolidationZone(
        zone_id=zid,
        formed_at=actives[-1].formed_at,
        left_at=actives[-1].left_at,
        poc=poc, vah_80=vah, val_80=val,
        high_100=max(z.high_100 for z in actives),
        low_100=min(z.low_100 for z in actives),
        total_volume=sum(z.total_volume for z in actives),
        duration_minutes=0, num_candles=0,
        status=ZoneStatus.LEFT, exit_direction=None, candles=[],
        timeframe="+".join(tfs), profile=profile,
    )


def _merge_zone_timelines(timelines: list, tfs: tuple, overlap_trade_tf: str = "merged") -> list:
    """Combine per-timeframe timelines into one merged timeline.
    A merged reference zone exists at a candle only when ALL timeframes in the
    combo have an active zone whose value areas overlap (intersection non-empty).
    Entry happens at the AVERAGE overlapping VAH/VAL level.
    """
    if len(timelines) == 1:
        return timelines[0]
    n = min(len(t) for t in timelines)
    merged = []
    # Consecutive candles usually share the SAME active-zone combination, which
    # synthesises an identical merged zone. Memoise by the tuple of active
    # zone_ids so we build each synthetic zone (with its summed profile) once
    # instead of per-candle — a large RAM saving for overlap combos.
    _syn_cache: dict = {}
    _NONE_ENTRY = {"active": None, "recent": [], "mature": False, "overlap": 0}
    for i in range(n):
        actives = [t[i].get("active") for t in timelines]
        if any(a is None for a in actives):
            merged.append(_NONE_ENTRY)
            continue
        lo = max(a.val_80 for a in actives)
        hi = min(a.vah_80 for a in actives)
        if lo <= hi:   # value areas of all timeframes overlap
            key = tuple(a.zone_id for a in actives)
            entry = _syn_cache.get(key)
            if entry is None:
                mz = _synthesize_merged_zone(actives, tfs, overlap_trade_tf)
                entry = {"active": mz, "recent": [mz], "mature": True, "overlap": len(actives)}
                _syn_cache[key] = entry
            merged.append(entry)
        else:
            merged.append(_NONE_ENTRY)
    return merged


# 1.0.8: 移除 ML/conf-combo 單組回測 worker (_run_ml_combo/_run_conf_combo)


# 1.0.8: 移除 conf-combo-run / ml-run / ml-results / ml-progress 端點


@router.get("/backtest/progress")
async def get_backtest_progress():
    """Return progress for the currently running single confluence backtest."""
    import time as _t
    now = _t.time()
    cached = _bt_progress_file_cache
    if now - cached["read_at"] < 1.0 and cached["data"] is not None:
        return cached["data"]
    try:
        if _BT_PROGRESS_FILE.exists():
            data = json.loads(_BT_PROGRESS_FILE.read_text(encoding="utf-8"))
            cached["data"] = data
            cached["read_at"] = now
            return data
    except Exception:
        pass
    return dict(_bt_progress_state)


# ============================================================

# 即時交易 (Live Trading)
# ============================================================

_live_engine = None
# 1.0.9: 多帳號 —— 最多 2 個 leader 引擎並行,keyed by account_id(int)。
# _live_engine 永遠指向 primary(第一個啟動的),讓所有既有「單引擎」引用零改動。
_live_engines: Dict[int, Any] = {}
MAX_LIVE_ENGINES = 2


def _primary_live_engine():
    """primary 引擎(第一個註冊的,或 None)。既有單引擎路徑沿用。"""
    return next(iter(_live_engines.values()), None)


def _resolve_live_engine(account_id: Optional[int] = None):
    """account_id 指定 → 該帳號引擎;否則 primary。"""
    if account_id:
        return _live_engines.get(int(account_id))
    return _primary_live_engine()


def _sync_primary_engine():
    """把模組級 _live_engine 對齊 primary(供既有引用)。"""
    global _live_engine
    _live_engine = _primary_live_engine()


class LiveStartRequest(BaseModel):
    account_id: int
    contract_id: str = Field(default_factory=lambda: current_quarterly_contract_id("MNQ"))
    contract_size: int = 3
    value_area_pct: float = 0.80
    area_timeframe: str = "15m"
    rr_ratio: int = 2                     # reward:risk multiple (1..6)
    tr_exit_mode: str = "tp"              # 1.0.8: "tp" 固定 TP | "ladder" 階梯滾動
    tr_daily_loss_stop: int = 0           # 1.0.8: 日虧 N 單斷路器(0=OFF;UI=FULL LOSS LOCK)
    tr_daily_win_stop: int = 0            # 1.0.9: FULL WIN LOCK — 日贏 N 單停新單(0=OFF)
    # 1.0.9: PDPT — 當日獲利達此金額($)後停開新單(0=OFF)。Topstep XFA 一致性用。
    tr_daily_profit_stop: float = 0.0
    fade_tp_frac: float = 0.75            # 1.0.9: DAY ZONE TP=VAL→POC 比例
    fade_entry_mode: str = "limit"        # 1.0.9: DAY ZONE 進場 limit|rejection|or15
    # v1.0.6: "single" = one area timeframe; "overlap" = enter at the AVERAGE
    # overlapping VAH/VAL of the timeframes in tf_combo (mirrors backtest/ML).
    method: str = "single"
    tf_combo: Optional[List[str]] = None
    tr_overlap_trade_tf: str = "merged"   # "merged"=average overlap zone, "smallest"=trade smallest TF zone
    # Strategy params
    strategy: str = "factor"
    tp_ticks: int = 200
    sl_ticks: int = 50
    trail_sl_ticks: int = 10
    trail_sl_pct: Optional[float] = 0.05
    trail_trigger_pct: float = 0.30
    trail_enabled: bool = True            # v1.0.6: master trail switch
    tr_tp_ticks: Optional[int] = None
    tr_sl_ticks: Optional[int] = None
    tr_trail_sl_ticks: Optional[int] = None
    tr_trail_sl_pct: Optional[float] = None
    tr_trail_trigger_pct: Optional[float] = None
    tr_trail_enabled: Optional[bool] = None
    tr_full_tp_lock: Optional[int] = None
    tr_allowed_sessions: Optional[List[str]] = Field(
        default_factory=lambda: list(DEFAULT_ALLOWED_SESSIONS)
    )
    candle_seconds: int = 60
    sigma_window_minutes: int = 15
    sigma_method: str = "std"
    sigma_entry_mode: str = "blind"
    sigma_accept_mode: str = "none"
    sigma_start: float = 1.0
    sigma_max: float = 3.0
    sigma_target_mode: str = "half"
    sigma_stop_span: float = 1.0
    sigma_accept_sigma: float = 2.0
    sigma_accept_bars: int = 2
    factor_timeframe_minutes: int = 5
    factor_signal_family: str = "emapmo"
    factor_side_mode: str = "all"
    factor_pmo_signal_mode: str = "normal"
    factor_session_va_filter: str = "off"
    factor_sl_rule: str = "atr"
    factor_tp_rule: str = "atr"
    factor_sl_value: float = 1.5
    factor_tp_value: float = 2.0
    factor_max_hold_bars: int = 24
    factor_max_trades_per_day: int = 3
    factor_warmup_bars: int = 320
    factor_pmo_threshold_scale: float = 1.0
    factor_pmo_normal_scale: float = 0.0
    factor_pmo_early_scale: float = 0.0
    momentum_first_minutes: int = 30
    momentum_entry_hour: int = 18
    # 1.0.9 SESSFIB —— fib 級別可調。0.618 是掃描中唯一通過 G0–G4 的進場位。
    betafib_entry_fib: float = 0.618
    betafib_anchor: str = "hl"
    betafib_risk_basis: str = "atr_blend"
    betafib_min_move_pct: float = 0.0
    full_tp_lock: int = 0                 # 0=OFF, 1/2/3 TP exits
    one_trade_per_session_direction: bool = True
    tr_one_trade_per_session: bool = True
    # Zone stability is enabled by default; keep this flag for future experiments.
    skip_zone_stability: bool = False
    breakout_confirm_bars: int = 7
    # v1.0.6: explainable confluence (ML scorer) live mode. Set strategy="confluence".
    # conf_shadow defaults False — live places real orders (practice account).
    conf_band_ticks: float = 4.0
    conf_min_distinct_tf: int = 2
    conf_rr: float = Field(default=1.0, ge=1.0, le=6.0)
    conf_wait_minutes: int = 1
    conf_base_minutes: int = 1
    conf_min_prob: float = 0.65
    conf_ev_floor: Optional[float] = None
    conf_rr_grid: Optional[List[float]] = None
    conf_use_scorer: bool = True
    conf_enable_breakout: bool = False
    conf_max_risk_ticks: Optional[int] = None
    max_profit_ticks: Optional[int] = None
    conf_sl_reference_tf: str = "largest"
    conf_allowed_sessions: Optional[List[str]] = Field(
        default_factory=lambda: list(DEFAULT_ALLOWED_SESSIONS)
    )
    # --- STYLE: optional exit-policy (break-even / trail / lock). All-OFF == original behaviour ---
    conf_trail_trigger_pct: float = 0.50
    conf_trail_lock_pct: float = 0.05
    conf_full_tp_lock: int = 0
    conf_session_limit: bool = True
    conf_shadow: bool = False
    # 1.0.8: 移除 ML Consolidation V2 (mlc2_*) 欄位 — 該策略已刪除

@router.post("/live/start")
async def live_start(req: LiveStartRequest):
    """啟動即時交易引擎"""
    global _live_engine, _live_engines

    # 1.0.9: 多帳號 —— 同一帳號不可重複啟動;最多 MAX_LIVE_ENGINES 個 leader 並行。
    existing = _live_engines.get(int(req.account_id))
    if existing and getattr(existing, "is_running", False):
        raise HTTPException(400, f"Account {req.account_id} live engine already running")
    running_others = [aid for aid, e in _live_engines.items()
                      if aid != int(req.account_id) and getattr(e, "is_running", False)]
    if len(running_others) >= MAX_LIVE_ENGINES:
        raise HTTPException(400, f"Max {MAX_LIVE_ENGINES} concurrent leader engines already running")

    if not _historical_candles:
        raise HTTPException(400, "No candles loaded — connect first")

    if not _topstepx_client:
        raise HTTPException(400, "TopstepX client not initialized — connect first")

    # Safety: verify account is practice
    try:
        accounts = await _topstepx_client.get_accounts()
        logger.info(f"[LIVE START] accounts found: {[{a.get('id'): a.get('name')} for a in accounts]}")
        logger.info(f"[LIVE START] requested account_id={req.account_id}")
        target = None
        for acc in accounts:
            if acc.get("id") == req.account_id:
                target = acc
                break
        if not target:
            avail_ids = [a.get("id") for a in accounts]
            raise HTTPException(400, f"Account {req.account_id} not found. Available: {avail_ids}")
        name = target.get("name", "")
        logger.info(f"[LIVE START] account verified: {name} (id={req.account_id})")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[LIVE START] Account check error: {e}")
        raise HTTPException(500, f"Account check failed: {e}")

    from backend.live.engine import LiveTradingEngine

    # ── Auto front-month rollover ──
    # Resolve the configured contract to the CURRENT tradable front month so an
    # expired/stale contract (e.g. MNQM26 after June expiry) doesn't get orders
    # rejected with code=9 ContractNotActive. Everything below uses the resolved id.
    try:
        resolved_cid = await _topstepx_client.get_front_month_contract_id(req.contract_id)
        if resolved_cid and resolved_cid != req.contract_id:
            logger.info(f"[LIVE START] Auto-roll contract {req.contract_id} -> {resolved_cid}")
            req.contract_id = resolved_cid
        global _live_contract_id
        _live_contract_id = req.contract_id
    except Exception as e:
        logger.warning(f"[LIVE START] Front-month resolve skipped: {e}")

    contract_size = _normalize_contract_size(req.contract_id, req.contract_size)
    value_area_pct = _normalize_value_area_pct(req.value_area_pct)
    live_strategy_params = _build_strategy_params_from_request(req, contract_size)

    # ── Fetch fresh candles for live warm-up (separate from backtest data) ──
    # A non-empty 2-day response can still be far too short on a weekend. Use
    # the same completed-TF/session-aware count as the strategy and expand the
    # range until FACTOR/PMO is genuinely ready.
    # Don't overwrite _historical_candles — backtest needs the full dataset.
    from datetime import datetime as _dt2, timedelta as _td2
    _now = _dt2.utcnow()
    live_warmup_candles = []
    for _days in (2, 7, 14):
        _cutoff = _now - _td2(days=_days)
        _candidate = [
            c for c in _historical_candles
            if c.timestamp.replace(tzinfo=None) >= _cutoff
        ]
        if _candidate:
            live_warmup_candles = sorted(_candidate, key=lambda c: c.timestamp)
            if len(live_warmup_candles) > 1:
                live_warmup_candles = live_warmup_candles[:-1]
            _completed, _required = signal_warmup_progress(
                live_warmup_candles,
                live_strategy_params,
            )
            if _required == 0 or _completed >= _required:
                break
    if not live_warmup_candles:
        live_warmup_candles = sorted(_historical_candles[-2880:], key=lambda c: c.timestamp)
        if len(live_warmup_candles) > 1:
            live_warmup_candles = live_warmup_candles[:-1]

    try:
        fresh_end = _now.strftime("%Y-%m-%dT%H:%M:%SZ")
        best_fresh = []
        best_fresh_completed = -1
        for days in (2, 7, 14):
            fresh_start = (_now - _td2(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
            logger.info(f"[LIVE START] Fetching fresh 1m candles ({days}d): {fresh_start} ~ {fresh_end}")
            fresh_candles = await _topstepx_client.get_historical_bars_paginated(
                contract_id=req.contract_id,
                unit=BarUnit.MINUTE,   # 1m — no settle delay (30s has ~6h lag)
                unit_number=1,
                start_time=fresh_start,
                end_time=fresh_end,
            )
            if not fresh_candles:
                logger.warning(f"[LIVE START] Fresh {days}d fetch returned 0 candles")
                continue
            fresh_candidate = sorted(fresh_candles, key=lambda c: c.timestamp)
            if len(fresh_candidate) > 1:
                fresh_candidate = fresh_candidate[:-1]
            completed, required = signal_warmup_progress(fresh_candidate, live_strategy_params)
            if not best_fresh or completed > best_fresh_completed:
                best_fresh = fresh_candidate
                best_fresh_completed = completed
            logger.info(
                f"[LIVE START] Fresh 1m candles loaded ({days}d): {len(fresh_candidate)} | "
                f"signal warmup={completed}/{required or '-'}"
            )
            if required == 0 or completed >= required:
                best_fresh = fresh_candidate
                best_fresh_completed = completed
                break
            logger.warning(
                f"[LIVE START] Signal warmup insufficient: {completed}/{required}; "
                "expanding history range"
            )
        if best_fresh:
            fresh_completed, fresh_required = signal_warmup_progress(
                best_fresh,
                live_strategy_params,
            )
            fallback_completed, _ = signal_warmup_progress(
                live_warmup_candles,
                live_strategy_params,
            )
            if (
                fresh_required == 0
                or fresh_completed >= fresh_required
                or fresh_completed >= fallback_completed
            ):
                live_warmup_candles = best_fresh
            else:
                logger.warning(
                    f"[LIVE START] Fresh history remained incomplete "
                    f"({fresh_completed}/{fresh_required}); keeping fuller stored "
                    f"warmup ({fallback_completed}/{fresh_required})"
                )
    except Exception as e:
        logger.error(f"[LIVE START] Failed to fetch fresh candles: {e} — using existing data")

    engine = LiveTradingEngine(
        client=_topstepx_client,
        account_id=req.account_id,
        contract_id=req.contract_id,
        contract_size=live_strategy_params.contract_size,
        value_area_pct=value_area_pct,
        strategy_params=live_strategy_params,
    )
    # 1.0.9: 註冊進多帳號表;_live_engine 對齊 primary(既有引用零改動)。
    _live_engines[int(req.account_id)] = engine
    _sync_primary_engine()

    # Log candle date range
    if live_warmup_candles:
        first_ts = live_warmup_candles[0].timestamp
        last_ts = live_warmup_candles[-1].timestamp
        logger.info(
            f"[LIVE START] {len(live_warmup_candles)} warmup candles | "
            f"range: {first_ts} ~ {last_ts}"
        )
    else:
        logger.warning("[LIVE START] NO warmup candles!")
    try:
        await engine.start(live_warmup_candles)
        logger.info(f"[LIVE START] Engine started successfully (account {req.account_id})")
    except Exception as e:
        logger.error(f"[LIVE START] Engine start failed: {e}")
        _live_engines.pop(int(req.account_id), None)
        _sync_primary_engine()
        raise HTTPException(500, f"Engine start failed: {e}")

    return {"success": True, "message": "Live engine started", "account_id": req.account_id}


@router.post("/live/stop")
async def live_stop(account_id: int = 0):
    """停止即時交易引擎(account_id 指定某 leader;0=primary)"""
    eng = _resolve_live_engine(account_id or None)
    if not eng or not eng.is_running:
        return {"success": True, "message": "Not running"}
    await eng.stop()
    return {"success": True, "message": "Live engine stopped", "account_id": getattr(eng, "account_id", account_id)}


@router.post("/live/cancel-pending")
async def live_cancel_pending(account_id: int = 0):
    """取消掛單(account_id 指定某 leader;0=primary)"""
    eng = _resolve_live_engine(account_id or None)
    if not eng:
        raise HTTPException(400, "Live engine not started")
    cancelled = await eng.cancel_pending_now()
    return {"success": cancelled, "message": "Pending order cancelled" if cancelled else "No pending order"}


@router.post("/live/flatten")
async def live_flatten(account_id: int = 0):
    """緊急平倉(account_id 指定某 leader;0=primary)"""
    eng = _resolve_live_engine(account_id or None)
    if not eng:
        raise HTTPException(400, "Live engine not started")
    await eng.flatten_now()
    return {"success": True, "message": "Flatten executed", "account_id": getattr(eng, "account_id", account_id)}


@router.get("/live/status")
async def live_status(account_id: int = 0):
    """取得即時交易狀態(account_id 指定某 leader;0=primary)"""
    eng = _resolve_live_engine(account_id or None)
    if not eng:
        return {"running": False}
    return eng.get_status()


@router.get("/live/status-all")
async def live_status_all():
    """1.0.9: 所有 leader 引擎的即時狀態(Live 帳號槽監控用)。"""
    out = []
    for aid, eng in _live_engines.items():
        try:
            st = eng.get_status()
        except Exception as e:
            st = {"running": False, "error": str(e)}
        out.append({"account_id": aid, "status": st})
    return {"success": True, "engines": out, "count": len(out)}


@router.get("/live/account-state")
async def live_account_state():
    """
    讀取 TopstepX 帳戶的真實狀態 (持倉 + 掛單 + 餘額)
    直接從 TopstepX API 讀取, 不依賴 live engine 狀態
    """
    if not _topstepx_client:
        raise HTTPException(400, "TopstepX client not initialized — connect first")

    try:
        # Get all accounts
        accounts = await _topstepx_client.get_accounts()
        results = []

        for acc in accounts:
            acc_id = acc.get("id")
            acc_name = acc.get("name", "?")
            acc_balance = acc.get("balance", 0)

            # Get positions
            try:
                positions = await _topstepx_client.get_positions(acc_id)
            except Exception as e:
                positions = [{"error": str(e)}]

            # Get orders
            try:
                orders = await _topstepx_client.get_orders(acc_id)
            except Exception as e:
                orders = [{"error": str(e)}]

            results.append({
                "account_id": acc_id,
                "name": acc_name,
                "balance": acc_balance,
                "is_practice": "PRAC" in acc_name,
                "positions": positions,
                "orders": orders,
            })

        # Also include live engine state for comparison
        engine_state = None
        if _live_engine:
            engine_state = {
                "running": _live_engine._running,
                "pending_order_id": _live_engine._pending_order_id,
                "pending_signal": {
                    "direction": _live_engine._pending_signal.direction.value,
                    "entry": _live_engine._pending_signal.entry_price,
                    "sl": _live_engine._pending_signal.sl_price,
                    "tp": _live_engine._pending_signal.tp_price,
                    "strategy": _live_engine._pending_signal.strategy.value,
                } if _live_engine._pending_signal else None,
                "open_position": _live_engine._open_position,
                "candles_processed": _live_engine._candles_processed,
                "log": _live_engine._log[-30:],
            }

        return {
            "accounts": results,
            "engine": engine_state,
            "timestamp": datetime.utcnow().isoformat(),
        }

    except Exception as e:
        raise HTTPException(500, f"Failed to read account state: {e}")


# ── Trade History (from TopstepX API, cached to disk) ──

import json as _json

_TRADE_HISTORY_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "data", "trade_history.json"
)

# Live engine writes confirmed exit reasons here (TP / SL / TRAIL_SL / FLATTEN).
# We merge by (account_id, contract_id, exit_time) so live trade history can
# accurately bucket trail-SL exits — without this, TP-vs-SL is inferred from
# pnl sign which collapses TRAIL into TP/SL.
_LIVE_EXITS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "data", "live_exits.json"
)


def _load_trade_history_cache() -> List[dict]:
    try:
        if os.path.exists(_TRADE_HISTORY_FILE):
            with open(_TRADE_HISTORY_FILE, "r", encoding="utf-8") as f:
                return _json.load(f)
    except Exception:
        pass
    return []


def _save_trade_history_cache(trades: List[dict]):
    os.makedirs(os.path.dirname(_TRADE_HISTORY_FILE), exist_ok=True)
    with open(_TRADE_HISTORY_FILE, "w", encoding="utf-8") as f:
        _json.dump(trades, f, indent=2)


def _load_live_exits() -> List[dict]:
    """Read the persisted live-engine exit log written by LiveTradingEngine.
    Each row: {account_id, contract_id, exit_time, exit_reason, entry_time, ...}.
    Tolerates a missing or malformed file."""
    try:
        if os.path.exists(_LIVE_EXITS_FILE):
            with open(_LIVE_EXITS_FILE, "r", encoding="utf-8") as f:
                data = _json.load(f)
                if isinstance(data, list):
                    return data
    except Exception:
        pass
    return []


def _build_exit_index(exits: List[dict]) -> Dict[tuple, dict]:
    """Index exits as {(account_id, contract_id, exit_time_str): row} for
    O(1) lookup during fill pairing. Time matches are loose: we keep the ISO
    string as written by the engine, and pairing tries a few variants."""
    idx: Dict[tuple, dict] = {}
    for e in exits or []:
        try:
            acc = e.get("account_id")
            cid = e.get("contract_id") or ""
            etime = (e.get("exit_time") or "").strip()
            reason = e.get("exit_reason") or ""
            if acc is None or not etime or not reason:
                continue
            idx[(acc, cid, etime)] = e
            # Also key without seconds for fuzzy match
            if "T" in etime and len(etime) >= 16:
                idx.setdefault((acc, cid, etime[:16]), e)
        except Exception:
            continue
    return idx


def _lookup_exit_record(idx: Dict[tuple, dict], account_id, contract_id, exit_time: str) -> Optional[dict]:
    if not exit_time:
        return None
    cid = contract_id or ""
    candidates = [exit_time, exit_time[:19] if len(exit_time) > 19 else exit_time]
    if "T" in exit_time and len(exit_time) >= 16:
        candidates.append(exit_time[:16])
    for c in candidates:
        v = idx.get((account_id, cid, c))
        if v:
            return v
    return None


def _lookup_exit_reason(idx: Dict[tuple, dict], account_id, contract_id, exit_time: str) -> Optional[str]:
    rec = _lookup_exit_record(idx, account_id, contract_id, exit_time)
    return (rec or {}).get("exit_reason")


def _decision_fields_from_exit_record(rec: Optional[dict]) -> dict:
    rec = rec or {}
    return {
        "sl_price": rec.get("sl_price"),
        "tp_price": rec.get("tp_price"),
        "original_sl_price": rec.get("original_sl_price") or rec.get("sl_price"),
        "original_tp_price": rec.get("original_tp_price") or rec.get("tp_price"),
        "mode": rec.get("mode"),
        "side": rec.get("side"),
        "largest_tf": rec.get("largest_tf"),
        "risk_tf": rec.get("risk_tf"),
        "wall_id": rec.get("wall_id"),
        "labels": rec.get("labels") or [],
        "primary_zone": rec.get("primary_zone"),
        "zone_id": rec.get("zone_id"),
    }


def _normalize_topstep_fill(t: dict) -> dict:
    """Convert TopstepX raw fill (ProjectX Gateway /api/Trade/search item)
    to a normalized intermediate format."""
    side = t.get("side")
    if side is None:
        side = t.get("Side", 0)
    if isinstance(side, str):
        direction = "buy" if side.lower() in ("buy", "long", "0") else "sell"
    else:
        direction = "buy" if side == 0 else "sell"

    price = (
        t.get("price")
        or t.get("Price")
        or t.get("fillPrice")
        or t.get("averagePrice")
        or 0
    )

    pnl_raw = t.get("profitAndLoss")
    if pnl_raw is None:
        pnl_raw = t.get("ProfitAndLoss")
    if pnl_raw is None:
        pnl_raw = t.get("pnl")
    is_close = pnl_raw is not None and pnl_raw != 0
    pnl = pnl_raw if pnl_raw is not None else 0

    ts = (
        t.get("creationTimestamp")
        or t.get("CreationTimestamp")
        or t.get("createdAt")
        or t.get("timestamp")
        or t.get("fillTime")
        or ""
    )

    return {
        "fill_id": t.get("id") or t.get("Id") or t.get("tradeId"),
        "account_id": t.get("accountId") or t.get("AccountId"),
        "contract_id": t.get("contractId") or t.get("ContractId") or "",
        "time": ts,
        "price": round(round(float(price) / 0.25) * 0.25, 2) if price else 0.0,
        "direction": direction,
        "size": t.get("size") or t.get("Size") or 1,
        "pnl": float(pnl) if pnl else 0.0,
        "fees": t.get("fees") or 0,
        "is_close": is_close,
        "order_id": t.get("orderId") or t.get("OrderId"),
    }


def _fill_qty(value) -> int:
    try:
        return max(1, int(abs(float(value or 1))))
    except (TypeError, ValueError):
        return 1


def _round_turn_costs(contract_id: str, size) -> tuple[float, float]:
    qty = _fill_qty(size)
    return get_commission_rt(contract_id) * qty, get_fees_rt(contract_id) * qty


def _ensure_net_trade_pnl(trades: List[dict]) -> List[dict]:
    """Migrate older cached live rows where pnl was gross price P/L."""
    out = []
    for row in trades or []:
        if not isinstance(row, dict):
            continue
        r = dict(row)
        old_commission = float(r.get("commission") or 0.0)
        old_fees = float(r.get("fees") or 0.0)
        if r.get("gross_pnl") is not None:
            gross = float(r.get("gross_pnl") or 0.0)
        elif r.get("pnl_is_net"):
            gross = float(r.get("pnl") or 0.0) + old_commission + old_fees
        else:
            gross = float(r.get("pnl") or 0.0)
        commission, fees = _round_turn_costs(
            str(r.get("contract_id") or ""),
            r.get("size") or r.get("contracts") or 1,
        )
        r["gross_pnl"] = round(gross, 2)
        r["pnl"] = round(gross - commission - fees, 2)
        r["commission"] = round(commission, 2)
        r["fees"] = round(fees, 2)
        r["pnl_is_net"] = True
        out.append(r)
    return out


def _pair_fills_to_trades(fills: List[dict]) -> List[dict]:
    """Pair opening fills with closing fills into round-trip trades per
    (account_id, contract_id) using FIFO. Unpaired fills become single-point
    entries so nothing is dropped.

    Exit reasons are merged from data/live_exits.json (written by the live
    engine on every position-close). Without that merge, the only signal is
    pnl sign, which silently collapses trail-SL exits into the TP / SL
    buckets and breaks the live performance breakdown.
    """
    fills_sorted = sorted(fills, key=lambda f: f.get("time") or "")
    exit_idx = _build_exit_index(_load_live_exits())

    open_legs: Dict[tuple, list] = {}
    trades: List[dict] = []

    for f in fills_sorted:
        key = (f.get("account_id"), f.get("contract_id"))
        if not f["is_close"]:
            open_legs.setdefault(key, []).append(f)
            continue

        queue = open_legs.get(key) or []
        opener = None
        for i, o in enumerate(queue):
            if o["direction"] != f["direction"]:
                opener = queue.pop(i)
                break

        if opener is not None:
            _ep = float(opener["price"] or 0)
            _xp = float(f["price"] or 0)
            _sz = opener.get("size") or f.get("size") or 1
            _cid = f.get("contract_id") or ""
            _pt = get_point_value(_cid)  # NQ=$20, MNQ=$2
            if opener["direction"] == "buy":
                _gross_pnl = (_xp - _ep) * _pt * _sz
            else:
                _gross_pnl = (_ep - _xp) * _pt * _sz
            _commission, _fees = _round_turn_costs(_cid, _sz)
            _net_pnl = _gross_pnl - _commission - _fees
            # Prefer the engine-recorded reason; fall back to pnl sign only when
            # we have nothing better (e.g. trades that pre-date the exit log).
            exit_rec = _lookup_exit_record(exit_idx, f.get("account_id"), _cid, f.get("time") or "")
            reason = (exit_rec or {}).get("exit_reason")
            if not reason:
                reason = "tp" if _net_pnl >= 0 else "sl"
            elif reason == "tp" and _net_pnl < 0:
                reason = "sl"
            elif reason == "sl" and _net_pnl > 0:
                reason = "tp"
            trades.append({
                "trade_id": str(opener["fill_id"]) + "_" + str(f["fill_id"]),
                "direction": opener["direction"],
                "size": _sz,
                "entry_price": opener["price"],
                "exit_price": f["price"],
                "entry_time": opener["time"],
                "exit_time": f["time"],
                "gross_pnl": round(_gross_pnl, 2),
                "pnl": round(_net_pnl, 2),  # net P&L after commission + fees
                "pnl_is_net": True,
                "commission": round(_commission, 2),
                "fees": round(_fees, 2),
                "exit_reason": reason,
                "account_id": f.get("account_id"),
                "contract_id": _cid,
                "source": "topstep",
                **_decision_fields_from_exit_record(exit_rec),
            })
        else:
            # Orphan closer — keep as single point so nothing is lost
            exit_rec = _lookup_exit_record(
                exit_idx, f.get("account_id"), f.get("contract_id") or "", f.get("time") or ""
            )
            _cid = f.get("contract_id") or ""
            _commission, _fees = _round_turn_costs(_cid, f.get("size") or 1)
            _gross_pnl = float(f["pnl"] or 0)
            _net_pnl = _gross_pnl - _commission - _fees
            reason = (exit_rec or {}).get("exit_reason") or ("tp" if _net_pnl >= 0 else "sl")
            trades.append({
                "trade_id": str(f["fill_id"]),
                "direction": f["direction"],
                "size": f.get("size") or 1,
                "entry_price": f["price"],
                "exit_price": f["price"],
                "entry_time": f["time"],
                "exit_time": f["time"],
                "gross_pnl": round(_gross_pnl, 2),
                "pnl": round(_net_pnl, 2),  # use API pnl minus costs; no paired prices
                "pnl_is_net": True,
                "commission": round(_commission, 2),
                "fees": round(_fees, 2),
                "exit_reason": reason,
                "account_id": f.get("account_id"),
                "contract_id": f.get("contract_id"),
                "source": "topstep",
                **_decision_fields_from_exit_record(exit_rec),
            })

    # Remaining unpaired openers → single-point markers
    for queue in open_legs.values():
        for o in queue:
            trades.append({
                "trade_id": str(o["fill_id"]),
                "direction": o["direction"],
                "size": o.get("size") or 1,
                "entry_price": o["price"],
                "exit_price": o["price"],
                "entry_time": o["time"],
                "exit_time": o["time"],
                "pnl": 0,
                "commission": 0,
                "fees": 0,
                "exit_reason": "open",
                "account_id": o.get("account_id"),
                "contract_id": o.get("contract_id"),
                "source": "topstep",
            })

    return trades


# 1.0.9: 多帳號交易紀錄 —— account_id → name 快取(refresh 時填),供分帳號標註/彙總。
_account_name_cache: Dict[int, str] = {}


def _annotate_trade_accounts(trades: List[dict], roles=None) -> List[dict]:
    """替每筆交易標上 account_name(快取)、account_type(express/practice/exam)、is_main。"""
    from backend.live.account_roles import load_roles, classify_type
    roles = roles or load_roles()
    main = str(roles.get("main_account_id") or "")
    for t in trades:
        aid = t.get("account_id")
        sid = str(aid) if aid is not None else None
        if not t.get("account_name") and aid in _account_name_cache:
            t["account_name"] = _account_name_cache[aid]
        nm = t.get("account_name") or ""
        t["account_type"] = classify_type(nm) if nm else None
        t["is_main"] = (sid == main)
    return trades


def _trade_history_by_account(trades: List[dict]) -> List[dict]:
    """分帳號彙總:每帳號的筆數與淨 PnL(主帳號優先、筆數多者在前)。"""
    agg: Dict[Any, dict] = {}
    for t in trades:
        aid = t.get("account_id")
        if aid is None:
            continue
        g = agg.setdefault(aid, {"account_id": aid, "account_name": t.get("account_name"),
                                 "account_type": t.get("account_type"), "is_main": t.get("is_main", False),
                                 "trades": 0, "net_pnl": 0.0})
        g["trades"] += 1
        g["net_pnl"] += float(t.get("net_pnl", t.get("pnl", 0)) or 0)
        if not g["account_name"] and t.get("account_name"):
            g["account_name"] = t["account_name"]
        if t.get("account_type") and not g["account_type"]:
            g["account_type"] = t["account_type"]
    return sorted(agg.values(), key=lambda x: (not x["is_main"], -x["trades"]))


def _trade_history_response(full_trades: List[dict], filter_acc_id: int, source: str) -> dict:
    """統一組裝回應:全帳號彙總(by_account,不受 filter 影響)+ 可選單帳號過濾的 trades。"""
    from backend.live.account_roles import load_roles
    roles = load_roles()
    _annotate_trade_accounts(full_trades, roles)
    by_account = _trade_history_by_account(full_trades)
    out = full_trades
    if filter_acc_id:
        out = [t for t in full_trades if t.get("account_id") == filter_acc_id]
    return {
        "trades": out,
        "source": source,
        "count": len(out),
        "account_id": filter_acc_id,
        "by_account": by_account,       # 1.0.9: 分帳號彙總(所有帳號)
        "email": roles.get("email", ""),
    }


@router.get("/live/trade-history")
async def live_trade_history(refresh: bool = False, account_id: int = 0):
    """Get trade history. Returns cached data by default.
    Pass ?refresh=true to re-fetch from TopstepX API.
    Pass ?account_id=N to only return trades for that account (0 = all accounts).
    1.0.9: 回應含 by_account 分帳號彙總 + email;預設不再自動限縮到 primary 引擎帳號。"""
    # 1.0.9: 預設仍過濾到「固定主帳號」,保持既有 calendar/monitor 單帳號檢視不變
    # (避免其他帳號複製單灌水);多帳號總覽改用回應中的 by_account 彙總。
    # 優先序:指定 account_id > 執行中 primary 引擎 > roles.main_account_id > 0(全部)。
    filter_acc_id = account_id
    if not filter_acc_id:
        _prim = _primary_live_engine()
        if _prim is not None:
            filter_acc_id = getattr(_prim, "account_id", 0) or 0
    if not filter_acc_id:
        from backend.live.account_roles import main_account_id as _main_id
        _m = _main_id()
        if _m:
            try:
                filter_acc_id = int(_m)
            except (TypeError, ValueError):
                filter_acc_id = 0

    if not refresh:
        cached = _ensure_net_trade_pnl(_load_trade_history_cache())
        if cached:
            return _trade_history_response(cached, filter_acc_id, "cache")

    if not _topstepx_client:
        cached = _ensure_net_trade_pnl(_load_trade_history_cache())
        return _trade_history_response(cached, filter_acc_id, "cache")

    try:
        accounts = await _topstepx_client.get_accounts()
        active_accounts = [a for a in accounts if a.get("canTrade", False)]
        logger.info(
            f"[TRADE HISTORY] {len(active_accounts)}/{len(accounts)} active accounts"
        )
        all_fills: List[dict] = []
        for acc in active_accounts:
            acc_id = acc.get("id")
            _account_name_cache[acc_id] = acc.get("name", "")   # 1.0.9: 供分帳號標註
            try:
                raw_trades = await _topstepx_client.get_trade_history(acc_id)
                for t in raw_trades:
                    norm = _normalize_topstep_fill(t)
                    if not norm.get("account_id"):
                        norm["account_id"] = acc_id
                    all_fills.append(norm)
            except Exception as e:
                logger.warning(f"[TRADE HISTORY] account {acc_id} failed: {e}")

        all_trades = _pair_fills_to_trades(all_fills)
        logger.info(
            f"[TRADE HISTORY] {len(all_fills)} fills → {len(all_trades)} trades"
        )

        if all_trades:
            _save_trade_history_cache(all_trades)

        return _trade_history_response(all_trades, filter_acc_id, "api")

    except Exception as e:
        logger.error(f"[TRADE HISTORY] failed: {e}")
        cached = _ensure_net_trade_pnl(_load_trade_history_cache())
        return _trade_history_response(cached, filter_acc_id, "cache_fallback")


# ── Presets (JSON file) ────────────────────────────────

_PRESETS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "data", "presets.json"
)

_PRESET_SCHEMA_VERSION = "2026-07-03-sigma-resting"
_DEFAULT_PRESET_NAME = "TREND MNQx1 DEFAULT"
_DEFAULT_PRESET_PARAMS = {
    "strategy": "factor",
    "tp_ticks": 200,
    "sl_ticks": 50,
    "trail_sl_ticks": 10,
    "trail_sl_pct": 0.05,
    "trail_trigger_pct": 0.30,
    "trail_enabled": True,
    "tr_tp_ticks": 200,
    "tr_sl_ticks": 50,
    "tr_trail_sl_ticks": 10,
    "tr_trail_sl_pct": 0.05,
    "tr_trail_trigger_pct": 0.30,
    "tr_trail_enabled": True,
    "tr_full_tp_lock": 0,
    "tr_allowed_sessions": ["ASIA"],
    "candle_seconds": 60,
    "contract_id": current_quarterly_contract_id("MNQ"),  # 1.0.8: 自動換月
    "contract_size": 1,
    "full_tp_lock": 0,
    "one_trade_per_session_direction": True,
    "tr_one_trade_per_session": True,
    "value_area_pct": 0.80,
    "area_timeframe": "15m",
    "method": "single",
    "tf_combo": [],
    "tr_overlap_trade_tf": "merged",
    "rr_ratio": 2,
    "breakout_confirm_bars": 7,
    "skip_zone_stability": False,
    "conf_band_ticks": 4.0,
    "conf_min_distinct_tf": 2,
    "conf_rr": 3.0,
    "conf_model_name": None,
    "conf_wait_minutes": 1,
    "conf_base_minutes": 1,
    "conf_min_prob": 0.0,
    "conf_ev_floor": None,
    "conf_rr_grid": None,
    "conf_use_scorer": True,
    "conf_enable_breakout": False,
    "conf_max_risk_ticks": 80,
    "conf_sl_reference_tf": "largest",
    "conf_allowed_sessions": ["ASIA"],
    "conf_trail_trigger_pct": 0.50,
    "conf_trail_lock_pct": 0.05,
    "conf_full_tp_lock": 0,
    "conf_session_limit": True,
    "conf_shadow": False,
    "sigma_window_minutes": 15,
    "sigma_method": "std",
    "sigma_entry_mode": "blind",
    "sigma_accept_mode": "none",
    "sigma_start": 1.0,
    "sigma_max": 3.0,
    "sigma_target_mode": "half",
    "sigma_stop_span": 1.0,
    "sigma_accept_sigma": 2.0,
    "sigma_accept_bars": 2,
    "factor_timeframe_minutes": 5,
    "factor_signal_family": "emapmo",
    "factor_side_mode": "all",
    "factor_pmo_signal_mode": "normal",
    "factor_session_va_filter": "off",
    "factor_sl_rule": "atr",
    "factor_tp_rule": "atr",
    "factor_sl_value": 1.5,
    "factor_tp_value": 2.0,
    "factor_max_hold_bars": 0,   # 1.0.9: HOLD 5m system removed → SL/TP-only
    "factor_max_trades_per_day": 3,
    "factor_warmup_bars": 320,
    "factor_pmo_threshold_scale": 1.0,
    # 1.0.8: 移除 mlc2_* 預設(ml_consolidation_v2 已刪除)
}

_PRESET_RENAMES = {}
_REMOVED_PRESET_NAMES = set()
_BUILTIN_PRESETS = {}
_FIXED_PRESET_NAMES = ()


def _preset_name_uses_allowed_model(name: str) -> bool:
    parts = str(name or "").split()
    if len(parts) < 2:
        return False
    if parts[0].upper() == "SWEEP":
        model_parts = parts[1:]
    elif len(parts) >= 2 and len(parts[0]) == 4 and parts[0].isdigit():
        model_parts = parts[1:]
    elif len(parts) >= 3 and len(parts[0]) == 5 and parts[0][2] == "." and ":" in parts[1]:
        model_parts = parts[2:]
    elif len(parts) >= 2 and len(parts[0]) == 5 and parts[0][2] == ".":
        model_parts = parts[1:]
    else:
        model_parts = parts
    model_part = " ".join(model_parts).upper()
    return (
        model_part.startswith("TREND #")
        or model_part.startswith("DAY ZONE #")
        or model_part.startswith("DISTRIBUTION #")
        or model_part.startswith("PMO #")
        or model_part.startswith("FACTOR #")
    )


def _ensure_builtin_presets(data: dict) -> tuple[dict, bool]:
    changed = False
    if not isinstance(data, dict):
        data = {}
        changed = True
    presets = data.get("presets")
    if not isinstance(presets, dict):
        presets = {}
        data["presets"] = presets
        changed = True

    # Preserve user-saved presets across schema bumps. Older builds cleared the
    # whole file here, which made manual presets appear to save and then vanish
    # after the next /presets load.
    if data.get("preset_schema") != _PRESET_SCHEMA_VERSION:
        data["preset_schema"] = _PRESET_SCHEMA_VERSION
        changed = True

    for name, params in list(presets.items()):
        if not isinstance(params, dict):
            continue
        strategy = str(params.get("strategy") or "").lower()
        # 1.0.8: mlc2 已移除 — 舊存檔的 mlc2 preset 一律歸一化為 trend;+fade 放行
        # 1.0.9: TREND 已移除,未知/舊值一律落到 factor
        normalized_strategy = strategy if strategy in ("fade", "sigma", "factor", "momentum", "betafib", "pi") else "factor"
        # 1.0.8: 舊存檔的到期合約自動改寫成目前前月季約
        _cid_new = normalize_contract_id_to_front(params.get("contract_id") or "")
        if _cid_new != params.get("contract_id"):
            params["contract_id"] = _cid_new
            changed = True
        if params.get("strategy") != normalized_strategy:
            params["strategy"] = normalized_strategy
            changed = True
        # 1.0.8: 吸附到 70/80 兩檔而非硬鎖 80,保留舊 70% 設定
        _va = _normalize_value_area_pct(params.get("value_area_pct"))
        if params.get("value_area_pct") != _va:
            params["value_area_pct"] = _va
            changed = True
        # 1.0.9: HOLD 5m-candle system removed — force every stored preset to
        # SL/TP-only exits (hold OFF). Applies to all current presets on load.
        # 1.0.10: 獨立 PMO 策略已移除,但舊 preset 檔裡可能還留著 pmo_max_hold_bars,
        # 保留在清單中讓它一併被歸零,避免殘值被寫回。
        for _hold_key in ("factor_max_hold_bars", "pmo_max_hold_bars"):
            if params.get(_hold_key) not in (0, None):
                params[_hold_key] = 0
                changed = True
        if normalized_strategy in ("sigma", "factor", "momentum", "betafib", "pi") and "tr_allowed_sessions" not in params:
            params["tr_allowed_sessions"] = list(DEFAULT_ALLOWED_SESSIONS)
            changed = True
            changed = True
        area_tf = _normalize_area_timeframe(params.get("area_timeframe"))
        if params.get("area_timeframe") != area_tf:
            params["area_timeframe"] = area_tf
            changed = True
        tf_combo = [t for t in (params.get("tf_combo") or []) if t in ML_TIMEFRAMES]
        if params.get("tf_combo") != tf_combo:
            params["tf_combo"] = tf_combo
            changed = True
        if params.get("method") == "overlap" and len(tf_combo) < 2:
            params["method"] = "single"
            changed = True

    for old_name, new_name in _PRESET_RENAMES.items():
        if old_name in presets:
            old_params = presets.pop(old_name)
            presets[new_name] = old_params
            for key in ("last_used_bt", "last_used_live"):
                if data.get(key) == old_name:
                    data[key] = new_name
            changed = True

    for name in _REMOVED_PRESET_NAMES:
        if name in presets:
            presets.pop(name, None)
            changed = True

    for name, params in _BUILTIN_PRESETS.items():
        if presets.get(name) != params:
            presets[name] = dict(params)
            changed = True

    if not presets:
        for key in ("last_used_bt", "last_used_live"):
            if data.get(key) != "default":
                data[key] = "default"
                changed = True

    for key in ("last_used_bt", "last_used_live"):
        if key not in data or (
            data.get(key) != "default" and data.get(key) not in presets
        ):
            data[key] = "default"
            changed = True

    data["fixed_presets"] = []
    return data, changed


def _load_presets_file() -> dict:
    data = None
    try:
        if os.path.exists(_PRESETS_FILE):
            with open(_PRESETS_FILE, "r", encoding="utf-8") as f:
                data = _json.load(f)
    except Exception:
        pass
    if data is None:
        data = {
            "presets": {},
            "preset_schema": _PRESET_SCHEMA_VERSION,
            "last_used_bt": "default",
            "last_used_live": "default",
        }
    data, changed = _ensure_builtin_presets(data)
    if changed:
        _save_presets_file(data)
    return data


def _presets_payload(data: dict) -> dict:
    data = dict(data or {})
    data["fixed_presets"] = list(_FIXED_PRESET_NAMES)
    return data


def _save_presets_file(data: dict):
    os.makedirs(os.path.dirname(_PRESETS_FILE), exist_ok=True)
    with open(_PRESETS_FILE, "w", encoding="utf-8") as f:
        _json.dump(data, f, indent=2, ensure_ascii=False)


@router.get("/presets")
async def get_presets():
    """列出所有 presets + last used"""
    return _presets_payload(_load_presets_file())


class PresetSaveRequest(BaseModel):
    name: str
    params: dict


@router.post("/presets/save")
async def save_preset(req: PresetSaveRequest):
    """儲存 preset"""
    data = _load_presets_file()
    data["presets"][req.name] = req.params
    _save_presets_file(data)
    return {"success": True, "name": req.name}


class PresetUseRequest(BaseModel):
    name: str
    mode: str  # "bt" or "live"


@router.post("/presets/use")
async def use_preset(req: PresetUseRequest):
    """記錄 last used preset"""
    data = _load_presets_file()
    key = "last_used_bt" if req.mode == "bt" else "last_used_live"
    data[key] = req.name
    _save_presets_file(data)
    return {"success": True}


class PresetDeleteRequest(BaseModel):
    name: str


def _delete_preset_by_name(name: str):
    data = _load_presets_file()
    deleted = False
    if name in data.get("presets", {}):
        del data["presets"][name]
        deleted = True
        if data.get("last_used_bt") == name:
            data["last_used_bt"] = "default"
        if data.get("last_used_live") == name:
            data["last_used_live"] = "default"
        _save_presets_file(data)
    return {"success": True, "deleted": deleted, "name": name}


@router.post("/presets/delete")
async def delete_preset_body(req: PresetDeleteRequest):
    """Delete preset by JSON body; supports names containing '/', '%', and spaces."""
    return _delete_preset_by_name(req.name)


@router.delete("/presets/{name:path}")
async def delete_preset(name: str):
    """刪除 preset"""
    return _delete_preset_by_name(name)

@router.get("/pi/signals")
async def pi_signals(symbol: str = "", start: str = "", end: str = ""):
    """1.0.10: 給圖表用的 PI 標記。

    只回時間 + 標記種類/尺寸 —— 價格由前端拿當根 K 棒的高低點決定,
    因為 PI 訊號來自 SPY/QQQ,本身沒有 MNQ/MES 的價位。
    """
    # 1.0.10: 走共用 loader —— 圖表必須跟回測/實盤看到同一組訊號
    # (見 docs/INVARIANTS.md PI-006)。過濾規則只有 pi_history 那一份。
    from backend.data.pi_history import load_rows

    rows = load_rows()

    lo, hi = _parse_iso_utc(start), _parse_iso_utc(end)
    want = (symbol or "").upper()
    out = []
    for r in rows:
        ts = _parse_iso_utc(r.get("ts", ""))
        if ts is None:
            continue
        if lo and ts < lo:
            continue
        if hi and ts > hi:
            continue
        # MNQ 跟 QQQ、MES 跟 SPY。沒指定商品就全給。
        sym = str(r.get("symbol") or "").upper()
        if want:
            if want.startswith("MNQ") and sym != "QQQ":
                continue
            if want.startswith("MES") and sym != "SPY":
                continue
        out.append({
            "ts": ts.isoformat(),
            "symbol": sym,
            "marks": [{"kind": m.get("kind"), "size": m.get("size"),
                       "count": m.get("count", 1)} for m in (r.get("marks") or [])],
        })
    return {"signals": out, "total": len(out)}
