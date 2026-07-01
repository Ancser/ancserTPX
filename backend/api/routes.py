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
# 1.0.8: 移除 ml_trend / ml_consolidation_v2 (mlc2) 相關 import
#        (MLTrendBacktester / MLTrendBacktestConfig / precompute_vp_timeline / MLTrendConfig)
#        mlc2 策略已整批刪除,僅保留 trend + confluence。

logger = logging.getLogger(__name__)
router = APIRouter()


def _env(key: str, default: str = "") -> str:
    """讀取 .env 環境變數"""
    return os.getenv(key, default)


MNQ_SIZE_CHOICES = (1, 3, 5, 10)
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
        size = 3
    return size if size in MNQ_SIZE_CHOICES else 3


def _normalize_trade_ticks(value, default: int) -> int:
    try:
        ticks = int(value or default)
    except (TypeError, ValueError):
        ticks = default
    return max(50, min(200, ticks))


def _normalize_value_area_pct(value=None, default: float = 0.80) -> float:
    """v1.0.6 locks every route to 80% Value Area."""
    return 0.80


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


def _normalize_strategy_name(value: str) -> str:
    return "trend"



AREA_TIMEFRAME_CHOICES = ("5m", "15m", "30m", "1h", "4h")


def _normalize_area_timeframe(value) -> str:
    tf = str(value or "5m").strip().lower()
    return tf if tf in AREA_TIMEFRAME_CHOICES else "5m"


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


def _build_strategy_params_from_request(req, contract_size: int) -> StrategyParams:
    # v1.0.6: "confluence" selects the explainable ML engine; anything else is trend.
    raw_strat = str(getattr(req, "strategy", "confluence") or "").strip().lower()
    # 1.0.8: mlc2 (ml_consolidation_v2) 已移除;僅 confluence / trend。
    if raw_strat == "confluence":
        strategy = "confluence"
    else:
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
        contract_id=getattr(req, "contract_id", "CON.F.US.MNQ.M26"),
        contract_size=contract_size,
        full_tp_lock=tr["full_tp_lock"],
        one_trade_per_session_direction=bool(getattr(req, "one_trade_per_session_direction", True)),
        tr_one_trade_per_session=bool(getattr(req, "tr_one_trade_per_session", True)),
        skip_zone_stability=False,
        breakout_confirm_bars=max(1, int(getattr(req, "breakout_confirm_bars", 7) or 7)),
        area_timeframe=_normalize_area_timeframe(getattr(req, "area_timeframe", "5m")),
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
    strategy: str = "confluence"
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
    area_timeframe: str = "5m"
    rr_ratio: int = 2                     # reward:risk multiple (1..6)
    # Contract & sizing (defaults to 3× Micro NQ)
    contract_id: str = "CON.F.US.MNQ.M26"
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
    area_timeframe: str = "5m"
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
                )
                start = 0

            for candle in candles[start:]:
                detector.update(candle)

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
        area_timeframe = _normalize_area_timeframe(getattr(req, "area_timeframe", "5m"))
        return {"zones": [], "count": 0, "area_timeframe": area_timeframe}
    sorted_candles = sorted(base_candles, key=lambda c: c.timestamp)

    if getattr(req, "all_timeframes", False):
        zone_list = await asyncio.to_thread(
            _detect_zones_sync,
            sorted_candles,
            ML_TIMEFRAMES,
            value_area_pct,
        )
        return {
            "zones": zone_list,
            "count": len(zone_list),
            "area_timeframe": "all",
            "timeframes": list(ML_TIMEFRAMES),
        }

    area_timeframe = _normalize_area_timeframe(getattr(req, "area_timeframe", "5m"))
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
            is_practice = "PRAC" in name
            result.append({
                "id": acc["id"],
                "name": name,
                "balance": acc.get("balance", 0),
                "can_trade": True,
                "is_practice": is_practice,
                "type": "PRACTICE" if is_practice else "FUNDED",
            })

        return {"success": True, "accounts": result}

    except Exception as e:
        logger.error(f"Accounts fetch failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await client.disconnect()


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
        for cid in fetch_contracts:
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
        if from_store:
            # Upsert fresh API bars into the stored set (newer wins on clash)
            by_ts: Dict[str, Candle] = {_candle_key(c): c for c in store_bars}
            for c in candles:
                by_ts[_candle_key(c)] = c
            candles = sorted(by_ts.values(), key=_candle_time)
            logger.info(f"[Store] merged: {len(store_bars)} stored + {sum(contract_counts.values())} fetched → {len(candles)} unique")

        # ── Persist to local store (1m bars only) ──
        if req.unit_number == 1 and candles:
            try:
                _store_save(candles, symbol)
            except Exception as e:
                logger.warning(f"[Store] save failed (non-fatal): {e}")

        # ── Gap detection + auto-recovery ──
        if req.unit_number == 1 and candles and not req.append:
            try:
                gaps = _store_detect_gaps(candles)
                if gaps:
                    logger.info(f"[Store] detected {len(gaps)} unexpected gap(s), attempting recovery...")
                    recovered = 0
                    for gap_start, gap_end, dur in gaps[:5]:  # cap at 5 recovery fetches
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
            _historical_candles = sorted(candles, key=_candle_time)

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
        raise HTTPException(status_code=400, detail="請先拉取歷史數據")

    candles_5m = BacktestEngine.aggregate_1m_to_5m(_historical_candles)

    return {
        "source_count": len(_historical_candles),
        "aggregated_count": len(candles_5m),
        "interval": "5m",
    }


def _confluence_scorer_path() -> Path:
    # canonical path lives in confluence_scorer.py (single source of truth)
    from backend.strategy.confluence_scorer import default_scorer_path
    return default_scorer_path()


# ── Zone-timeline cache (single-backtest speedup) ─────────────────────────────
# build_zone_timeline() is the slow detector pass (~tens of seconds over full
# history). It depends ONLY on the candle data + base/tick/depth — NOT on the
# model / RR / band / min_tf / loss_weight. So re-running a backtest with a
# different model on the SAME data can reuse it. Keep just ONE entry (latest)
# to cap memory; the key changes automatically when new candles are fetched
# (len / first / last timestamp), so stale data never gets reused.
_conf_timeline_cache: dict = {}


def _get_conf_timeline(candles, timeframes, tick, depth, base, progress_callback=None):
    from backend.backtest.confluence_backtest import build_zone_timeline
    key = (
        len(candles),
        candles[0].timestamp if candles else None,
        candles[-1].timestamp if candles else None,
        int(base), float(tick), int(depth),
    )
    entry = _conf_timeline_cache.get("entry")
    if entry and entry[0] == key:
        logger.info(f"[Confluence] reusing cached zone timeline ({len(candles)} candles) — skipped rebuild")
        if progress_callback:
            progress_callback(
                "reusing zone timeline", len(candles), len(candles),
                "cached detector timeline",
            )
        return entry[1]
    import time as _t
    _t0 = _t.perf_counter()
    logger.info(f"[Confluence] building zone timeline over {len(candles)} candles…")
    timeline = build_zone_timeline(
        candles, timeframes, tick, depth,
        progress_callback=progress_callback,
    )
    logger.info(f"[Confluence] zone timeline built in {_t.perf_counter() - _t0:.1f}s")
    _conf_timeline_cache["entry"] = (key, timeline)
    return timeline


# ── Dedicated backtest PROCESS (keeps the web server responsive) ──────────────
# A web backtest is CPU-bound pure Python: run in a thread it still holds the
# GIL for long stretches and starves the FastAPI event loop, so data-fetch /
# live / chart all freeze until it finishes ("stuck then suddenly moved"). A
# child PROCESS has its own GIL, so the server stays responsive. One long-lived
# worker (max_workers=1) serialises runs and keeps its own candle+timeline cache
# (so repeated runs on the same data skip the slow detector pass). Candles are
# only re-pickled into the child when the data actually changed.
_bt_executor = None              # lazily-created ProcessPoolExecutor
_bt_last_candle_key = None       # last candle key the worker already has
_BT_PROGRESS_FILE = Path(__file__).resolve().parents[2] / "data" / "backtest_progress.json"
_bt_progress_state = {
    "status": "idle", "stage": "", "current": 0, "total": 0,
    "detail": "", "updated_at": 0.0,
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


async def _run_confluence_backtest_proc(req: BacktestRequest) -> BacktestResponse:
    """Off-load the confluence backtest to the dedicated child process, then
    rebuild the pydantic response in this process. Falls back to the in-thread
    path if the worker process fails for any reason."""
    global _bt_last_candle_key
    candles = sorted(_historical_candles, key=lambda c: c.timestamp)
    contract_size = _normalize_contract_size(req.contract_id, req.contract_size)
    rr_grid = _conf_rr_grid_opt(getattr(req, "conf_rr_grid", None))

    payload = _request_payload(req)
    params = {**payload, "contract_size": contract_size, "rr_grid": rr_grid}

    ckey = _bt_candle_key(candles)
    send_candles = candles if ckey != _bt_last_candle_key else None
    progress = str(_BT_PROGRESS_FILE)
    _bt_progress_file_cache["data"] = None
    _bt_progress_file_cache["read_at"] = 0.0
    _update_bt_progress(
        "queued", 0, len(candles),
        f"{len(candles)} candles, {'new data' if send_candles is not None else 'cached data'}",
    )
    logger.info(f"[Confluence] single backtest started over {len(candles)} candles")

    try:
        from backend.backtest import confluence_worker
        loop = asyncio.get_running_loop()
        out = await loop.run_in_executor(
            _get_bt_executor(), confluence_worker.run_job, ckey, send_candles, params, progress,
        )
        _bt_last_candle_key = ckey
    except Exception as e:
        logger.warning(f"[Confluence] backtest process failed ({e}); falling back to in-thread run")
        _bt_last_candle_key = None  # worker state unknown → resend candles next time
        _update_bt_progress("fallback", 0, len(candles), str(e))
        try:
            return await asyncio.to_thread(
                _run_confluence_backtest, req,
                lambda stage, current, total, detail="": _update_bt_progress(
                    stage, current, total, detail,
                ),
            )
        except Exception as fallback_error:
            _update_bt_progress(
                "failed", 0, len(candles), str(fallback_error), status="error",
            )
            raise

    m = out["metrics"]
    metrics_resp = MetricsResponse(
        total_trades=m["total_trades"], wins=m["wins"], losses=m["losses"],
        win_rate=m["win_rate"], avg_win=m["avg_win"], avg_loss=m["avg_loss"],
        avg_rr_ratio=m["avg_rr_ratio"], expectancy=m["expectancy"],
        max_drawdown=m["max_drawdown"], max_drawdown_pct=m["max_drawdown_pct"],
        calmar_ratio=m["calmar_ratio"], profit_factor=m["profit_factor"],
        max_consecutive_losses=m["max_consecutive_losses"], total_pnl=m["total_pnl"],
        total_gain=m["total_gain"], total_loss=m["total_loss"],
        daily_pnl=m["daily_pnl"],
        weekly_stats=_weekly_stats(m["daily_pnl"]),
        trend_follow=None,
    )
    trades_resp = [TradeResponse(**t) for t in out["trades"]]
    resp = BacktestResponse(
        metrics=metrics_resp, trades=trades_resp, zones=[], equity_curve=out["equity"],
    )
    _backtest_results.append(resp)
    _update_bt_progress(
        "complete", len(candles), len(candles),
        f"{len(trades_resp)} trades", status="complete",
    )
    logger.info(f"[Confluence] single backtest complete ({len(trades_resp)} trades)")
    return resp


def _run_confluence_backtest(req: BacktestRequest, progress_callback=None) -> BacktestResponse:
    """v1.0.6: explainable multi-timeframe confluence backtest.

    Uses the SAME ConfluenceBacktester + trained ConfluenceScorer that the
    research scripts and (soon) the live engine use, so the web result is
    reproducible and identical to console. Read-only: never touches the trend
    backtest path."""
    import math
    from backend.strategy.consolidation import timeframes_for_base
    from backend.strategy.confluence import ConfluenceConfig, MAX_RECENCY_DEPTH
    from backend.strategy.confluence_scorer import ConfluenceScorer, resolve_scorer
    from backend.backtest.confluence_backtest import (
        ConfluenceBacktester, ConfluenceBacktestConfig, build_zone_timeline,
    )

    candles = sorted(_historical_candles, key=lambda c: c.timestamp)
    contract_size = _normalize_contract_size(req.contract_id, req.contract_size)
    tick = get_tick_size(req.contract_id)
    base = max(1, int(req.conf_base_minutes or 1))
    timeframes = timeframes_for_base(base)

    rr_grid = None
    scorer = resolve_scorer(req.conf_use_scorer, None)

    # probability gate -> raw logit (score) threshold
    min_score = 0.0
    if req.conf_min_prob and 0.0 < req.conf_min_prob < 1.0:
        min_score = math.log(req.conf_min_prob / (1.0 - req.conf_min_prob))

    sig_cfg = ConfluenceConfig(
        band_ticks=req.conf_band_ticks,
        min_distinct_tf=req.conf_min_distinct_tf,
        rr=req.conf_rr,
    )
    sig_cfg.direction_mode = "auto"
    sig_cfg.tick_size = tick
    # EV-priority gate (option C): when set, supersedes the win-prob gate above.
    sig_cfg.ev_floor = req.conf_ev_floor
    sig_cfg.rr_grid = None
    sig_cfg.enable_breakout = bool(getattr(req, "conf_enable_breakout", False))
    sig_cfg.max_risk_ticks = getattr(req, "conf_max_risk_ticks", None)
    sig_cfg.sl_reference_tf = _normalize_conf_sl_reference_tf(
        getattr(req, "conf_sl_reference_tf", "largest")
    )
    run_cfg = ConfluenceBacktestConfig(
        wait_minutes=req.conf_wait_minutes, min_score=min_score,
        base_minutes=base, timeframes=timeframes,
        one_trade_per_session_direction=bool(getattr(req, "conf_session_limit",
                                                      req.one_trade_per_session_direction)),
        trail_trigger_pct=float(getattr(req, "conf_trail_trigger_pct", 0.50) or 0.0),
        trail_lock_pct=float(getattr(req, "conf_trail_lock_pct", 0.05) or 0.0),
        full_tp_lock=int(getattr(req, "conf_full_tp_lock", 0) or 0),
        allowed_sessions=tuple(_conf_allowed_sessions_list(
            getattr(req, "conf_allowed_sessions", DEFAULT_ALLOWED_SESSIONS)
        ) or ()),
    )
    bt_cfg = BacktestConfig(
        initial_capital=req.initial_capital,
        symbol=_extract_symbol(req.contract_id),
        commission_rt=get_commission_rt(req.contract_id),
        fees_rt=get_fees_rt(req.contract_id),
    )

    timeline = _get_conf_timeline(
        candles, timeframes, tick, MAX_RECENCY_DEPTH, base,
        progress_callback=progress_callback,
    )
    bt = ConfluenceBacktester(
        signal_cfg=sig_cfg, run_cfg=run_cfg, contract_id=req.contract_id,
        contract_size=contract_size, bt_config=bt_cfg, scorer=scorer,
    )
    result = bt.run(
        candles,
        zones_timeline=timeline,
        progress_callback=progress_callback,
    )
    _backtest_results.append(result)

    symbol_label = "/" + bt_cfg.symbol
    trades_resp = []
    for t in result.trades:
        meta = t.meta or {}
        trades_resp.append(TradeResponse(
            trade_id=t.trade_id,
            strategy="confluence",
            symbol=symbol_label,
            size=t.contracts,
            direction=t.direction.value if t.direction else "",
            entry_price=t.entry_price,
            entry_time=t.entry_time.isoformat() if t.entry_time else "",
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
            zone_source=getattr(t, "zone_source", "confluence"),
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
            vol_ratio=getattr(t, "vol_ratio", 0.0),
            is_big_trend=getattr(t, "is_big_trend", False),
        ))

    m = result.metrics
    metrics_resp = MetricsResponse(
        total_trades=m.total_trades, wins=m.wins, losses=m.losses,
        win_rate=m.win_rate, avg_win=m.avg_win, avg_loss=m.avg_loss,
        avg_rr_ratio=m.avg_rr_ratio, expectancy=m.expectancy,
        max_drawdown=m.max_drawdown, max_drawdown_pct=m.max_drawdown_pct,
        calmar_ratio=m.calmar_ratio, profit_factor=m.profit_factor,
        max_consecutive_losses=m.max_consecutive_losses, total_pnl=m.total_pnl,
        total_gain=getattr(m, "total_gain", 0.0), total_loss=getattr(m, "total_loss", 0.0),
        daily_pnl=m.daily_pnl or {},
        weekly_stats=_weekly_stats(m.daily_pnl or {}),
        trend_follow=None,
    )

    # equity curve from realised pnl at each exit (confluence result has none)
    equity = []
    cap = req.initial_capital
    if candles:
        equity.append([candles[0].timestamp.timestamp() * 1000, cap])
    for t in sorted(result.trades, key=lambda x: (x.exit_time or x.entry_time)):
        cap += (t.pnl or 0.0)
        ts = (t.exit_time or t.entry_time)
        if ts:
            equity.append([ts.timestamp() * 1000, round(cap, 2)])

    return BacktestResponse(
        metrics=metrics_resp, trades=trades_resp, zones=[], equity_curve=equity,
    )


class ConfluenceTrainRequest(BaseModel):
    """Retrain the explainable confluence scorer on the loaded 1m history."""
    contract_id: str = "CON.F.US.MNQ.M26"
    train_frac: float = 1.0      # 1.0 = learn from ALL loaded data (for going live)
    stride: int = 5
    wait_min: int = 60
    horizon_min: int = 1440
    band_ticks: float = 4.0
    min_distinct_tf: int = 2
    rr: float = Field(default=3.0, ge=1.0, le=6.0)
    enable_breakout: bool = False


def _train_confluence_scorer_sync(candles, req: "ConfluenceTrainRequest") -> dict:
    """Blocking trainer (run in a threadpool). Standardized on 1m base — fits a
    logistic scorer on forward-scan labels and writes confluence_scorer.json.
    Reuses the SAME collect()/evaluate_and_meta() as backend/ml/train_confluence.py
    so the web result is identical to the CLI trainer (drop-constant fit,
    time-series-CV C, uniqueness weighting, embargoed walk-forward OOS)."""
    from datetime import datetime as _dt

    from backend.strategy.confluence import ConfluenceConfig, MAX_RECENCY_DEPTH
    from backend.strategy.confluence_scorer import ConfluenceScorer
    from backend.strategy.consolidation import timeframes_for_base
    from backend.backtest.confluence_backtest import build_zone_timeline
    from backend.ml.train_confluence import collect, evaluate_and_meta

    base = 1  # standardized base
    timeframes = timeframes_for_base(base)
    tick = get_tick_size(req.contract_id)
    wait_bars = max(1, round(req.wait_min / base))
    horizon_bars = max(1, round(req.horizon_min / base))

    frac = min(1.0, max(0.1, req.train_frac))
    split = int(len(candles) * frac)
    train = candles[:split] if frac < 1.0 else candles
    if len(train) < (wait_bars + horizon_bars + 50):
        raise ValueError(f"歷史數據太少 ({len(train)} bars)，請先載入完整歷史再學習。")

    cfg = ConfluenceConfig(band_ticks=req.band_ticks, min_distinct_tf=req.min_distinct_tf, rr=req.rr)
    cfg.direction_mode = "auto"
    cfg.tick_size = tick
    cfg.enable_breakout = bool(req.enable_breakout)

    timeline = build_zone_timeline(train, timeframes, tick, MAX_RECENCY_DEPTH)
    X, y, modes, starts, ends = collect(train, timeline, cfg, req.stride, wait_bars, horizon_bars)
    if len(y) < 50:
        raise ValueError(f"可標記樣本太少 ({len(y)})，降低 stride/min_distinct_tf 或載入更多數據。")

    # train_frac=1.0 (learn-and-live) leaves no held-out tail, so the embargoed
    # walk-forward inside evaluate_and_meta is the ONLY honest out-of-sample
    # number — it is always recorded in meta below so the shipped model can never
    # ship without one.
    weights, b_raw, info = evaluate_and_meta(
        X, y, starts, ends, n_bars=len(train), embargo=wait_bars + horizon_bars)

    scorer = ConfluenceScorer(
        weights=weights, bias=float(b_raw),
        meta={
            "kind": "logistic", "trained": True,
            "trained_at": _dt.now().isoformat(timespec="seconds"),
            "data_start": train[0].timestamp.isoformat() if train else None,
            "data_end": train[-1].timestamp.isoformat() if train else None,
            "contract": req.contract_id, "base_min": base,
            "timeframes": list(timeframes),
            "train_frac": frac, "n_samples": int(len(y)),
            "train_win_rate": float(y.mean()), "train_auc": info["auc"],
            "train_brier": info["brier"], "C": info["C"],
            "oos_auc": info["oos_auc"], "oos_brier": info["oos_brier"],
            "oos_folds": info["oos_folds"], "mean_uniqueness": info["mean_uniqueness"],
            "dropped_features": info["dropped_features"],
            "std_weights": info["std_weights"],
            "sklearn_hygiene": "drop-constant+ts-cv+uniqueness+walkforward",
            "source": "web_learn_and_live",
            "cfg": {"band_ticks": req.band_ticks, "min_distinct_tf": req.min_distinct_tf,
                    "rr": req.rr, "wait_min": req.wait_min, "horizon_min": req.horizon_min,
                    "enable_breakout": bool(req.enable_breakout)},
        },
    )
    out = _confluence_scorer_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    scorer.save(out)

    sw = info["std_weights"]
    top = sorted(sw, key=lambda k: abs(sw[k]), reverse=True)[:5]

    sweep_result = None
    try:
        from backend.ml.train_confluence import sweep_probability_threshold
        full_tl = build_zone_timeline(candles, timeframes, tick, MAX_RECENCY_DEPTH)
        rows, best_idx = sweep_probability_threshold(
            candles, full_tl, scorer, cfg, req.contract_id,
            contract_size=3, wait_minutes=req.wait_min,
        )
        sweep_result = {"rows": rows, "best_idx": best_idx,
                        "recommended": rows[best_idx]}
    except Exception:
        pass

    return {
        "success": True,
        "n_bars": len(train),
        "n_samples": int(len(y)),
        "win_rate": float(y.mean()),
        "train_auc": info["auc"],
        "train_acc": info["acc"],
        "oos_auc": info["oos_auc"],
        "oos_brier": info["oos_brier"],
        "timeframes": list(timeframes),
        "top_weights": [{"name": n, "weight": round(sw[n], 4),
                         "raw": round(weights[n], 6)} for n in top],
        "saved_to": str(out),
        "threshold_sweep": sweep_result,
    }


@router.post("/confluence/train")
async def confluence_train(req: ConfluenceTrainRequest):
    """Retrain the explainable confluence scorer (1m base) on the loaded history.

    Powers the LEARN & LIVE button: the frontend loads the full range first,
    then calls this; on success it starts the live ML engine, which reloads the
    freshly written confluence_scorer.json. CPU-heavy fit runs off the event loop.
    """
    if not _historical_candles:
        raise HTTPException(status_code=400, detail="請先拉取歷史數據再學習")
    candles = sorted(_historical_candles, key=lambda c: c.timestamp)
    try:
        return await asyncio.to_thread(_train_confluence_scorer_sync, candles, req)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/confluence/scorer")
async def confluence_scorer_info():
    """Return the single saved production scorer for the LEARN RESULT panel."""
    import json
    from backend.strategy.confluence_scorer import default_scorer_path

    def _load(p):
        try:
            if not p.exists():
                return None
            d = json.loads(p.read_text(encoding="utf-8"))
            weights = d.get("weights", {}) or {}
            top = sorted(weights.items(), key=lambda kv: abs(kv[1]), reverse=True)
            return {
                "path": str(p), "meta": d.get("meta", {}),
                "bias": d.get("bias"),
                "weights": [{"name": k, "weight": v} for k, v in top],
            }
        except Exception as e:  # noqa: BLE001
            return {"path": str(p), "error": str(e)}

    return {
        "fixed": _load(default_scorer_path()),
        "ev": None,
    }


@router.get("/confluence/models")
async def confluence_models():
    """Return every immutable model version plus the currently active id."""
    from backend.strategy.confluence_scorer import list_model_versions

    versions, active_model = list_model_versions()
    models = []
    for path, scorer in versions:
        meta = scorer.meta
        cfg = meta.get("cfg") or {}
        models.append({
            "name": str(meta.get("model_id") or path.stem),
            "rr": float(cfg.get("rr", 3.0)),
            "band": float(cfg.get("band_ticks", 4.0)),
            "min_distinct_tf": int(cfg.get("min_distinct_tf", 2)),
            "breakout": bool(cfg.get("enable_breakout", False)),
            "trained": bool(meta.get("trained", True)),
            "trainer": meta.get("trainer", "codex"),
            "description": meta.get("description", ""),
            "n_samples": meta.get("n_samples"),
            "train_auc": meta.get("train_auc"),
            "oos_auc": meta.get("oos_auc"),
            "win_rate": meta.get("train_win_rate"),
            "loss_weight": meta.get("loss_weight", 1.0),
            "trained_at": meta.get("trained_at"),
            "active": str(meta.get("model_id") or path.stem) == active_model,
        })
    return {
        "grid": None,
        "n_models": len(models),
        "trained": len(models),
        "active_model": active_model,
        "models": models,
    }


class ModelActivateRequest(BaseModel):
    name: str


@router.post("/confluence/models/activate")
async def confluence_model_activate(req: ModelActivateRequest):
    """Activate a registry version for both backtest and live."""
    from backend.strategy.confluence_scorer import (
        activate_model_version, default_scorer_path,
    )

    try:
        source, meta = activate_model_version(req.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Model not found: {req.name}") from exc
    return {
        "success": True,
        "name": req.name,
        "source": str(source),
        "activated_to": str(default_scorer_path()),
        "cfg": meta.get("cfg", {}),
        "meta": meta,
    }


class ModelRetrainRequest(BaseModel):
    """Train a new immutable model version and optionally activate it."""
    trainer: Literal["user", "codex", "claude"] = "codex"
    description: str = Field(
        default="RR3 Band4 MinTF2 retrain", min_length=3, max_length=120,
    )
    rr: float = Field(default=3.0, ge=1.0, le=6.0)
    band_ticks: float = 4.0
    min_distinct_tf: int = 2
    enable_breakout: bool = False
    loss_weight: float = 1.0      # >1 = loss-averse: higher PF / lower maxDD / fewer trades
    stride: int = 5
    wait_min: int = 60
    horizon_min: int = 1440
    contract_id: str = "CON.F.US.MNQ.M26"
    activate: bool = True


def _retrain_model_sync(candles, req: "ModelRetrainRequest") -> dict:
    """Blocking training job that appends one immutable scorer version."""
    from datetime import datetime as _dt

    from backend.strategy.confluence import ConfluenceConfig, MAX_RECENCY_DEPTH
    from backend.strategy.confluence_scorer import ConfluenceScorer, save_model_version
    from backend.strategy.consolidation import timeframes_for_base
    from backend.backtest.confluence_backtest import build_zone_timeline
    from backend.ml.train_confluence import collect, evaluate_and_meta

    base = 1
    timeframes = timeframes_for_base(base)
    tick = get_tick_size(req.contract_id)
    wait_bars = max(1, round(req.wait_min / base))
    horizon_bars = max(1, round(req.horizon_min / base))
    train = candles
    if len(train) < (wait_bars + horizon_bars + 50):
        raise ValueError(f"歷史數據太少 ({len(train)} bars)，請先載入完整歷史。")

    cfg = ConfluenceConfig(band_ticks=req.band_ticks,
                           min_distinct_tf=req.min_distinct_tf, rr=req.rr)
    cfg.direction_mode = "auto"
    cfg.tick_size = tick
    cfg.enable_breakout = bool(req.enable_breakout)

    timeline = build_zone_timeline(train, timeframes, tick, MAX_RECENCY_DEPTH)
    X, y, modes, starts, ends = collect(train, timeline, cfg, req.stride, wait_bars, horizon_bars)
    if len(y) < 50:
        raise ValueError(f"可標記樣本太少 ({len(y)})，降低 stride/min_distinct_tf 或載入更多數據。")

    weights, b_raw, info = evaluate_and_meta(
        X, y, starts, ends, n_bars=len(train), embargo=wait_bars + horizon_bars,
        loss_weight=req.loss_weight)

    scorer = ConfluenceScorer(
        weights=weights, bias=float(b_raw),
        meta={
            "kind": "logistic", "trained": True,
            "trained_at": _dt.now().isoformat(timespec="seconds"),
            "contract": req.contract_id, "base_min": base,
            "timeframes": list(timeframes), "n_samples": int(len(y)),
            "train_win_rate": float(y.mean()), "train_auc": info["auc"],
            "train_brier": info["brier"], "C": info["C"],
            "oos_auc": info["oos_auc"], "oos_brier": info["oos_brier"],
            "oos_folds": info["oos_folds"],
            "dropped_features": info["dropped_features"],
            "std_weights": info["std_weights"],
            "loss_weight": req.loss_weight, "source": "web_model_retrain",
            "cfg": {"band_ticks": req.band_ticks, "min_distinct_tf": req.min_distinct_tf,
                    "rr": req.rr, "enable_breakout": bool(req.enable_breakout),
                    "wait_min": req.wait_min, "horizon_min": req.horizon_min,
                    "loss_weight": req.loss_weight},
        },
    )
    model_id, out = save_model_version(
        scorer,
        req.trainer,
        req.description,
        activate=req.activate,
    )

    sw = info["std_weights"]
    top = sorted(sw, key=lambda k: abs(sw[k]), reverse=True)[:5]

    sweep_result = None
    try:
        from backend.ml.train_confluence import sweep_probability_threshold
        sweep_tl = build_zone_timeline(candles, timeframes, tick, MAX_RECENCY_DEPTH)
        rows, best_idx = sweep_probability_threshold(
            candles, sweep_tl, scorer, cfg, req.contract_id,
            contract_size=3, wait_minutes=req.wait_min,
        )
        sweep_result = {"rows": rows, "best_idx": best_idx,
                        "recommended": rows[best_idx]}
    except Exception:
        pass

    return {
        "success": True, "name": model_id, "model_id": model_id,
        "trainer": req.trainer, "description": scorer.meta["description"],
        "activated": bool(req.activate),
        "n_samples": int(len(y)), "win_rate": float(y.mean()),
        "train_auc": info["auc"], "oos_auc": info["oos_auc"],
        "oos_brier": info["oos_brier"], "loss_weight": req.loss_weight,
        "top_weights": [{"name": n, "weight": round(sw[n], 4)} for n in top],
        "saved_to": str(out),
        "threshold_sweep": sweep_result,
    }


@router.post("/confluence/models/retrain")
async def confluence_model_retrain(req: ModelRetrainRequest):
    """Train and append a new model version."""
    if not _historical_candles:
        raise HTTPException(status_code=400, detail="請先拉取歷史數據再訓練")
    candles = sorted(_historical_candles, key=lambda c: c.timestamp)
    try:
        return await asyncio.to_thread(_retrain_model_sync, candles, req)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))



# 1.0.8: 移除 _run_ml_consolidation_v2_backtest (mlc2 回測入口,已隨策略刪除)


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
            detail="請先通過 /api/data/fetch-historical 拉取數據"
        )

    # v1.0.6: explainable confluence engine (separate, read-only path)
    if _strat == "confluence":
        await _refresh_recent_historical_candles(req.contract_id)
        # Heavy full-history backtest: run in a dedicated child PROCESS so the
        # CPU-bound work never holds the server's GIL — data-fetch / live / chart
        # stay responsive while it computes (falls back to in-thread on failure).
        return await _run_confluence_backtest_proc(req)

    # 1.0.8: 移除 mlc2 (ml_consolidation_v2) 回測分派

    await _refresh_recent_historical_candles(req.contract_id)

    # v1.0.6: derive symbol + per-contract fees from the chosen contract_id so
    # the trade journal shows /MNQ when MNQ is selected and 10×MNQ doesn't get
    # stuck paying 10× the NQ Mini fee schedule.
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

    # v1.0.6: overlap mode reproduces an ML overlap row — enter at the AVERAGE
    # overlapping VAH/VAL of the timeframes in tf_combo via a merged zone timeline.
    method = str(getattr(req, "method", "single") or "single").lower()
    tf_combo = tuple(t for t in (getattr(req, "tf_combo", None) or []) if t in ML_TIMEFRAMES)
    overlap_mode = method == "overlap" and len(tf_combo) >= 2
    overlap_trade_tf = _normalize_tr_overlap_trade_tf(
        getattr(strategy_params, "tr_overlap_trade_tf", "merged")
    )

    _overlap_zone_timeline = None
    if overlap_mode:
        ordered = [tf for tf in ML_TIMEFRAMES if tf in tf_combo]
        _ov_candles = sorted(_historical_candles, key=lambda c: c.timestamp)
        # Emit progress BEFORE the (slow, uncached-on-first-run) detector pass so
        # the UI shows "building zone timeline…" instead of the previous run's
        # stale "done" while this churns over full history.
        _update_bt_progress(
            "building zone timeline", 0, len(_ov_candles),
            f"{len(ordered)} timeframe(s) over {len(_ov_candles)} candles",
        )
        def _build_overlap_timeline():
            return _get_merged_zone_timeline(
                _ov_candles, value_area_pct, False, tuple(ordered), overlap_trade_tf,
            )

        # Off-load to a worker thread so the event loop (and the progress poll)
        # stays responsive while the detector pass churns over full history.
        _overlap_zone_timeline = await asyncio.to_thread(_build_overlap_timeline)

    engine = BacktestEngine(
        config,
        strategy_params=strategy_params,
        zone_timeline=_overlap_zone_timeline,
    )

    # Use 1m candles directly (SessionTrendFollow works on 1m)
    candles = sorted(_historical_candles, key=lambda c: c.timestamp) if overlap_mode else list(_historical_candles)

    # Off-load the CPU-bound candle loop to a worker thread so the event loop
    # stays responsive — chart, data-fetch, live updates and the progress poll
    # keep working while a (possibly 100k+ candle) trend backtest computes.
    # Without this the whole server freezes for the run's duration and the UI
    # stutters; the freeze grows as the accumulator grows.
    _update_bt_progress("running", 0, len(candles), "回測中…")

    def _trend_progress(current, total, detail):
        # Fired from the worker thread on each date change; atomic file write.
        _update_bt_progress("running", current, total, detail)

    result = await asyncio.to_thread(engine.run, candles, _trend_progress)
    _update_bt_progress("done", len(candles), len(candles), "完成", status="done")
    _backtest_results.append(result)

    # 轉換為回應格式
    trades_resp = []
    symbol_label = "/" + config.symbol   # TopstepX-style "/NQ"
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
            original_sl_price=getattr(t, 'original_sl_price', None) or t.sl_price,
            original_tp_price=getattr(t, 'original_tp_price', None) or t.tp_price,
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
            vol_ratio=t.vol_ratio,
            is_big_trend=t.is_big_trend,
        ))

    # Overlap mode renders the merged synthetic zones from the timeline
    # (timeline runs don't populate result.zones).
    if overlap_mode and _overlap_zone_timeline:
        seen_ids = set()
        merged_zones = []
        for entry in _overlap_zone_timeline:
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
        # Calculate VP profile for frontend histogram
        profile_data = None
        if z.candles:
            try:
                vp = vp_calc.calculate(z.candles)
                sorted_levels = sorted(vp.profile.items())
                # guard: empty profile OR all-zero volumes → avoid /0
                max_vol = (max(vp.profile.values()) if vp.profile else 0) or 1
                profile_data = [
                    {"price": p, "volume": v, "pct": round(v / max_vol, 3)}
                    for p, v in sorted_levels
                ]
            except Exception:
                profile_data = []
        elif getattr(z, "profile", None):
            # Merged/slim zones carry a precomputed histogram instead of candles.
            try:
                prof = z.profile or {}
                # guard: empty profile OR all-zero volumes → avoid /0
                max_vol = (max(prof.values()) if prof else 0) or 1
                profile_data = [
                    {"price": p, "volume": v, "pct": round(v / max_vol, 3)}
                    for p, v in sorted(prof.items())
                ]
            except Exception:
                profile_data = []

        # Zone is mature only if it actually reached maturity during its lifetime
        is_mature = getattr(z, 'mature', False)

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
            timeframe=getattr(z, 'timeframe', '1m'),
            parent_zone_id=getattr(z, 'parent_zone_id', None),
            mature=is_mature,
            va_curve=getattr(z, 'va_curve', None) or None,
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

    # Persist the per-trade journal to data/backtest/ so each run is auditable.
    try:
        _write_backtest_csv(req, config, strategy_params, method, tf_combo,
                            trades_resp, metrics_resp)
    except Exception as exc:  # CSV export must never break the API response
        logger.warning("Backtest CSV export failed: %s", exc)

    return BacktestResponse(
        metrics=metrics_resp,
        trades=trades_resp,
        zones=zones_resp,
        equity_curve=equity,
    )


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
    area_timeframe: str = "5m",
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


ML_TIMEFRAMES = ("5m", "15m", "30m", "1h", "4h")
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


class LiveStartRequest(BaseModel):
    account_id: int
    contract_id: str = "CON.F.US.MNQ.M26"
    contract_size: int = 3
    value_area_pct: float = 0.80
    area_timeframe: str = "5m"
    rr_ratio: int = 2                     # reward:risk multiple (1..6)
    # v1.0.6: "single" = one area timeframe; "overlap" = enter at the AVERAGE
    # overlapping VAH/VAL of the timeframes in tf_combo (mirrors backtest/ML).
    method: str = "single"
    tf_combo: Optional[List[str]] = None
    tr_overlap_trade_tf: str = "merged"   # "merged"=average overlap zone, "smallest"=trade smallest TF zone
    # Strategy params
    strategy: str = "confluence"
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
    global _live_engine

    if _live_engine and _live_engine.is_running:
        raise HTTPException(400, "Live engine already running")

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
            logger.info(f"[LIVE START] Auto-roll 合約 {req.contract_id} -> {resolved_cid}")
            req.contract_id = resolved_cid
        global _live_contract_id
        _live_contract_id = req.contract_id
    except Exception as e:
        logger.warning(f"[LIVE START] Front-month resolve skipped: {e}")

    # ── Fetch fresh candles for live warm-up (separate from backtest data) ──
    # Don't overwrite _historical_candles — backtest needs the full dataset.
    # Fallback: use last 2 days from existing data (cap to avoid slow warmup with 30-day set)
    from datetime import datetime as _dt2, timedelta as _td2
    _cutoff = _dt2.utcnow() - _td2(days=2)
    live_warmup_candles = [c for c in _historical_candles
                           if c.timestamp.replace(tzinfo=None) >= _cutoff]
    if not live_warmup_candles:
        live_warmup_candles = list(_historical_candles[-2880:])  # last ~2d of 1m
    try:
        from datetime import datetime, timedelta
        now = datetime.utcnow()
        fresh_start = (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        fresh_end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        logger.info(f"[LIVE START] Fetching fresh 1m candles: {fresh_start} ~ {fresh_end}")

        fresh_candles = await _topstepx_client.get_historical_bars_paginated(
            contract_id=req.contract_id,
            unit=BarUnit.MINUTE,   # 1m — no settle delay (30s has ~6h lag)
            unit_number=1,
            start_time=fresh_start,
            end_time=fresh_end,
        )
        if fresh_candles and len(fresh_candles) > 0:
            live_warmup_candles = fresh_candles
            logger.info(
                f"[LIVE START] Fresh 1m candles loaded: {len(fresh_candles)} | "
                f"range: {fresh_candles[0].timestamp} ~ {fresh_candles[-1].timestamp}"
            )
        else:
            logger.warning("[LIVE START] Fresh fetch returned 0 candles, using existing data")
    except Exception as e:
        logger.error(f"[LIVE START] Failed to fetch fresh candles: {e} — using existing data")

    live_warmup_candles = sorted(live_warmup_candles, key=lambda c: c.timestamp)
    if len(live_warmup_candles) > 1:
        live_warmup_candles = live_warmup_candles[:-1]

    contract_size = _normalize_contract_size(req.contract_id, req.contract_size)
    value_area_pct = _normalize_value_area_pct(req.value_area_pct)
    live_strategy_params = _build_strategy_params_from_request(req, contract_size)

    _live_engine = LiveTradingEngine(
        client=_topstepx_client,
        account_id=req.account_id,
        contract_id=req.contract_id,
        contract_size=live_strategy_params.contract_size,
        value_area_pct=value_area_pct,
        strategy_params=live_strategy_params,
    )

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
        await _live_engine.start(live_warmup_candles)
        logger.info(f"[LIVE START] Engine started successfully")
    except Exception as e:
        logger.error(f"[LIVE START] Engine start failed: {e}")
        _live_engine = None
        raise HTTPException(500, f"Engine start failed: {e}")

    return {"success": True, "message": "Live engine started"}


@router.post("/live/stop")
async def live_stop():
    """停止即時交易引擎"""
    global _live_engine
    if not _live_engine or not _live_engine.is_running:
        return {"success": True, "message": "Not running"}

    await _live_engine.stop()
    return {"success": True, "message": "Live engine stopped"}


@router.post("/live/cancel-pending")
async def live_cancel_pending():
    """取消掛單"""
    if not _live_engine:
        raise HTTPException(400, "Live engine not started")
    cancelled = await _live_engine.cancel_pending_now()
    return {"success": cancelled, "message": "Pending order cancelled" if cancelled else "No pending order"}


@router.post("/live/flatten")
async def live_flatten():
    """緊急平倉"""
    if not _live_engine:
        raise HTTPException(400, "Live engine not started")
    await _live_engine.flatten_now()
    return {"success": True, "message": "Flatten executed"}


@router.get("/live/status")
async def live_status():
    """取得即時交易狀態"""
    if not _live_engine:
        return {"running": False}
    return _live_engine.get_status()


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


@router.get("/live/trade-history")
async def live_trade_history(refresh: bool = False, account_id: int = 0):
    """Get trade history. Returns cached data by default.
    Pass ?refresh=true to re-fetch from TopstepX API.
    Pass ?account_id=N to only return trades for that account."""
    filter_acc_id = account_id or (
        getattr(_live_engine, "account_id", 0) if _live_engine else 0
    )

    if not refresh:
        cached = _ensure_net_trade_pnl(_load_trade_history_cache())
        if cached:
            if filter_acc_id:
                cached = [t for t in cached if t.get("account_id") == filter_acc_id]
            return {
                "trades": cached,
                "source": "cache",
                "count": len(cached),
                "account_id": filter_acc_id,
            }

    if not _topstepx_client:
        cached = _ensure_net_trade_pnl(_load_trade_history_cache())
        if filter_acc_id:
            cached = [t for t in cached if t.get("account_id") == filter_acc_id]
        return {
            "trades": cached,
            "source": "cache",
            "count": len(cached),
            "account_id": filter_acc_id,
        }

    try:
        accounts = await _topstepx_client.get_accounts()
        active_accounts = [a for a in accounts if a.get("canTrade", False)]
        logger.info(
            f"[TRADE HISTORY] {len(active_accounts)}/{len(accounts)} active accounts"
        )
        all_fills: List[dict] = []
        for acc in active_accounts:
            acc_id = acc.get("id")
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

        out = all_trades
        if filter_acc_id:
            out = [t for t in out if t.get("account_id") == filter_acc_id]

        return {
            "trades": out,
            "source": "api",
            "count": len(out),
            "account_id": filter_acc_id,
        }

    except Exception as e:
        logger.error(f"[TRADE HISTORY] failed: {e}")
        cached = _ensure_net_trade_pnl(_load_trade_history_cache())
        if filter_acc_id:
            cached = [t for t in cached if t.get("account_id") == filter_acc_id]
        return {
            "trades": cached,
            "source": "cache_fallback",
            "count": len(cached),
            "account_id": filter_acc_id,
        }


# ── Presets (JSON file) ────────────────────────────────

_PRESETS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "data", "presets.json"
)

_PRESET_SCHEMA_VERSION = "2026-06-25-ml-consolidation-v2"
_DEFAULT_PRESET_NAME = "ML CONFLUENCE MNQx3 DEFAULT"
_DEFAULT_PRESET_PARAMS = {
    "strategy": "confluence",
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
    "contract_id": "CON.F.US.MNQ.U26",
    "contract_size": 1,
    "full_tp_lock": 0,
    "one_trade_per_session_direction": True,
    "tr_one_trade_per_session": True,
    "value_area_pct": 0.80,
    "area_timeframe": "5m",
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
    # 1.0.8: 移除 mlc2_* 預設(ml_consolidation_v2 已刪除)
}

_CODEX_620_MODEL = "20260618_codex_rr3-band4-mintf2-production-baseline-02"
# Legacy 06.24 CODEX presets (kept in _REMOVED for migration)
_CODEX_624_PRESET_1 = "06.24 CODEX #1 穩健測試 MNQx1 RR1:3 POFF R80 W1m Trail50L5 SesON ASIA B4 TF2"
_CODEX_624_PRESET_2 = "06.24 CODEX #2 收益較高 MNQx1 RR1:2.75 POFF R80 W1m Trail50L5 SesON ASIA B4 TF2"
_CODEX_624_PRESET_3 = "06.24 CODEX #3 卡瑪最佳 MNQx1 RR1:1.75 POFF R70 W1m Trail50L5 SesON ASIA B4 TF2"
_CODEX_624_PRESET_4 = "06.24 CODEX #4 PNL最高 MNQx1 RR1:1.5 POFF R50 W1m Trail50L5 SesON ASIA B4 TF2"
_CODEX_624_PRESET_5 = "06.24 CODEX #5 回撤最低 MNQx1 RR1:2.5 P0.65 R90 W1m Trail50L5 SesON ASIA B4 TF2"
# CLAUDE #1-5 retired: OOM sweep produced unreliable low-RR configs (RR≤1.25)
_CODEX_626_PRESET_1 = "06.26 CODEX #1 Trend穩定 MNQx1 TF1h RR1:4 C3 SL40 Trail50L10"
_CODEX_626_PRESET_2 = "06.26 CODEX #2 Trend多單 MNQx1 TF5m RR1:4 C3 SL80 Trail50L10"
_CODEX_626_PRESET_3 = "06.26 CODEX #3 Trend均衡 MNQx1 TF15m RR1:4 C3 SL40 TrailOFF"
_CODEX_626_PRESET_4 = "06.26 CODEX #4 RESEARCH Confluence舊最佳 MNQx1 RR1:2.5 P0.65 R90 B4 TF2 Shadow"
_CODEX_626_PRESET_5 = "06.26 CODEX #5 RESEARCH Confluence低回撤 MNQx1 RR1:1.5 POFF R50 B4 TF2 Shadow"
_CODEX_626_PRESET_6 = "06.26 CODEX #6 RESEARCH Confluence高TF MNQx1 RR1:1.5 P0.65 R40 B8 TF3 Shadow"
_CODEX_626_PRESET_7 = "06.26 CODEX #7 RESEARCH MLC2低回撤 MNQx1 LB240 B2 RANGE POC R40 ASIA Shadow"
_CODEX_626_PRESET_8 = "06.26 CODEX #8 RESEARCH MLC2多單 MNQx1 LB240 B1 RANGE POC R20 PRE Shadow"
_CODEX_626_PRESET_9 = "06.26 CODEX #9 RESEARCH MLC2寬Band MNQx1 LB240 B4 RANGE POC R40 ASIA Shadow"
_CODEX_630_PRESET_1 = "06.30 CODEX #1 Trend低損 MNQx1 TF5m RR1:6 C2 SL80 Trail50L10 SesON FT2"
_CODEX_630_PRESET_3 = "06.30 CODEX #3 Trend重合5m30m小TF MNQx1 TF5m+30m Trade5m RR1:6 C3 SL80 Trail50L10 SesOFF FT2"
_CODEX_630_PRESET_4 = "06.30 CODEX #4 Trend重合30m1h小TF MNQx1 TF30m+1h Trade30m RR1:7 C4 SL80 Trail50L10 SesON FT0"
_DEFAULT_LAST_USED_PRESET = _CODEX_630_PRESET_1
_PRESET_RENAMES = {
}
_REMOVED_PRESET_NAMES = {
    _DEFAULT_PRESET_NAME,
    "ML CONFLUENCE MNQx3 DEFAULT",
    "6/20 CODEX #1 baseline02 RR1:5 P0.60 R80 W1m TrailOFF SesON ASIA B4 TF2 MNQx3",
    "6/20 CODEX #2 baseline02 RR1:5 POFF R80 W1m Trail50L5 SesON ASIA B4 TF2 MNQx3",
    "6/23 CODEX #3 SAFE baseline02 RR1:5 POFF R80 W1m Trail50L5 SesON ASIA B4 TF2 MNQx1",
    "6/20 CLAUDE #1 ML RR1:3 P0.55 W1m TrailOFF SesON B4 TF2 MNQx3",
    "6/20 CLAUDE #1 SVD RR1:2 P0.55 W1m TrailOFF SesON B4 TF2 MNQx3",
    "6/22 CLAUDE #1 ML RR1:3 P0.55 ROFF W1m TrailOFF SesON B4 TF2 MNQx1",
    # Retired MLC2 preset (strategy has no edge)
    "06.25 CODEX #1 均值回歸 MNQx1 MLC2 LB30 Band2 SLB4 RR1:4 ASIA+EURO TrailOFF",
    # Retired CLAUDE #1-5: OOM sweep data was wrong, low-RR configs unprofitable
    "06.25 CLAUDE #1 Calmar最佳 MNQx1 RR1:1.0 R30 Trail50L5 ASIA B4 TF2",
    "06.25 CLAUDE #2 回撤最低 MNQx1 RR1:1.0 R50 Trail50L5 ASIA B4 TF2",
    "06.25 CLAUDE #3 均衡型 MNQx1 RR1:1.75 R70 Trail50L5 ASIA B4 TF2",
    "06.25 CLAUDE #4 收益最高 MNQx1 RR1:1.5 R50 Trail50L5 ASIA B4 TF2",
    "06.25 CLAUDE #5 高勝率 MNQx1 RR1:1.25 R40 Trail50L5 ASIA B4 TF2",
    # Retired 06.24 confluence presets: corrected-market validation on
    # 2026-06-26 showed unstable/negative May+June splits.
    _CODEX_624_PRESET_1,
    _CODEX_624_PRESET_2,
    _CODEX_624_PRESET_3,
    _CODEX_624_PRESET_4,
    _CODEX_624_PRESET_5,
    _CODEX_626_PRESET_1,
    _CODEX_626_PRESET_3,
    _CODEX_626_PRESET_4,
    _CODEX_626_PRESET_5,
    _CODEX_626_PRESET_6,
    _CODEX_626_PRESET_7,
    _CODEX_626_PRESET_8,
    _CODEX_626_PRESET_9,
}


def _codex_624_preset(
    *,
    rr: float,
    max_risk_ticks: int,
    min_prob: float = 0.0,
    trail_trigger: float = 0.50,
    contract_id: str = "CON.F.US.MNQ.U26",
    contract_size: int = 1,
) -> dict:
    params = dict(_DEFAULT_PRESET_PARAMS)
    params.update({
        "tp_ticks": int(round(50 * float(rr))),
        "tr_tp_ticks": int(round(50 * float(rr))),
        "contract_id": contract_id,
        "contract_size": contract_size,
        "rr_ratio": float(rr),
        "conf_model_name": _CODEX_620_MODEL,
        "conf_rr": float(rr),
        "conf_min_prob": float(min_prob),
        "conf_max_risk_ticks": int(max_risk_ticks),
        "conf_sl_reference_tf": "largest",
        "conf_band_ticks": 4.0,
        "conf_min_distinct_tf": 2,
        "conf_allowed_sessions": ["ASIA"],
        "conf_trail_trigger_pct": trail_trigger,
        "conf_trail_lock_pct": 0.05,
        "conf_session_limit": True,
    })
    return params


def _codex_626_trend_preset(
    *,
    area_timeframe: str,
    rr: int,
    confirm_bars: int,
    sl_ticks: int,
    trail_enabled: bool,
    trail_trigger: float = 0.50,
    trail_ticks: int = 10,
    full_tp_lock: int = 0,
    method: str = "single",
    tf_combo: Optional[list[str]] = None,
    overlap_trade_tf: str = "merged",
    session_limit: bool = True,
    contract_id: str = "CON.F.US.MNQ.U26",
    contract_size: int = 1,
) -> dict:
    params = dict(_DEFAULT_PRESET_PARAMS)
    tp_ticks = int(sl_ticks) * int(rr)
    method = "overlap" if str(method or "").lower() == "overlap" else "single"
    combo = [t for t in (tf_combo or []) if t in ML_TIMEFRAMES]
    if method != "overlap" or len(combo) < 2:
        combo = []
        method = "single"
    params.update({
        "strategy": "trend",
        "contract_id": contract_id,
        "contract_size": contract_size,
        "area_timeframe": area_timeframe,
        "method": method,
        "tf_combo": combo,
        "tr_overlap_trade_tf": _normalize_tr_overlap_trade_tf(overlap_trade_tf),
        "value_area_pct": 0.80,
        "rr_ratio": int(rr),
        "breakout_confirm_bars": int(confirm_bars),
        "tp_ticks": tp_ticks,
        "sl_ticks": int(sl_ticks),
        "tr_tp_ticks": tp_ticks,
        "tr_sl_ticks": int(sl_ticks),
        "trail_enabled": bool(trail_enabled),
        "tr_trail_enabled": bool(trail_enabled),
        "trail_trigger_pct": float(trail_trigger if trail_enabled else 0.0),
        "tr_trail_trigger_pct": float(trail_trigger if trail_enabled else 0.0),
        "trail_sl_ticks": int(trail_ticks if trail_enabled else 0),
        "tr_trail_sl_ticks": int(trail_ticks if trail_enabled else 0),
        "full_tp_lock": int(full_tp_lock),
        "tr_full_tp_lock": int(full_tp_lock),
        "tr_allowed_sessions": ["ASIA"],
        "one_trade_per_session_direction": True,
        "tr_one_trade_per_session": bool(session_limit),
    })
    return params


def _codex_626_confluence_research_preset(
    *,
    rr: float,
    max_risk_ticks: int,
    min_prob: float,
    band: float,
    min_tf: int,
    contract_id: str = "CON.F.US.MNQ.U26",
    contract_size: int = 1,
) -> dict:
    params = _codex_624_preset(
        rr=rr,
        max_risk_ticks=max_risk_ticks,
        min_prob=min_prob,
        contract_id=contract_id,
        contract_size=contract_size,
    )
    params.update({
        "strategy": "confluence",
        "conf_band_ticks": float(band),
        "conf_min_distinct_tf": int(min_tf),
        "conf_sl_reference_tf": "largest",
        "conf_allowed_sessions": ["ASIA"],
        "conf_trail_trigger_pct": 0.50,
        "conf_trail_lock_pct": 0.05,
        "conf_session_limit": True,
        # Research-only: corrected-market validation did not pass stability.
        # Selecting this preset for live logs signals without placing orders.
        "conf_shadow": True,
    })
    return params


# 1.0.8: 移除 _codex_626_mlc2_research_preset(零呼叫者;mlc2 策略已刪除)


_BUILTIN_PRESETS = {
    _CODEX_630_PRESET_1: _codex_626_trend_preset(
        area_timeframe="5m", rr=6, confirm_bars=2, sl_ticks=80,
        trail_enabled=True, trail_trigger=0.50, trail_ticks=10,
        full_tp_lock=2,
    ),
    _CODEX_630_PRESET_3: _codex_626_trend_preset(
        area_timeframe="5m", rr=6, confirm_bars=3, sl_ticks=80,
        trail_enabled=True, trail_trigger=0.50, trail_ticks=10,
        full_tp_lock=2, method="overlap", tf_combo=["5m", "30m"],
        overlap_trade_tf="smallest", session_limit=False,
    ),
    _CODEX_630_PRESET_4: _codex_626_trend_preset(
        area_timeframe="30m", rr=7, confirm_bars=4, sl_ticks=80,
        trail_enabled=True, trail_trigger=0.50, trail_ticks=10,
        full_tp_lock=0, method="overlap", tf_combo=["30m", "1h"],
        overlap_trade_tf="smallest", session_limit=True,
    ),
    _CODEX_626_PRESET_2: _codex_626_trend_preset(
        area_timeframe="5m", rr=4, confirm_bars=3, sl_ticks=80,
        trail_enabled=True, trail_trigger=0.50, trail_ticks=10,
    ),
}
_FIXED_PRESET_NAMES = ()


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

    # One-time preset migration.  The 06.24 market-entry confluence pass replaced
    # the older 6/20-6/23 presets and the temporary liquidity-sweep presets.
    # After this marker is written, user-saved presets are kept normally.
    if data.get("preset_schema") != _PRESET_SCHEMA_VERSION:
        presets.clear()
        data["preset_schema"] = _PRESET_SCHEMA_VERSION
        data["last_used_bt"] = _DEFAULT_LAST_USED_PRESET
        data["last_used_live"] = _DEFAULT_LAST_USED_PRESET
        changed = True

    for name, params in list(presets.items()):
        if not isinstance(params, dict):
            continue
        strategy = str(params.get("strategy") or "").lower()
        # 1.0.8: mlc2 已移除 — 舊存檔的 mlc2 preset 一律歸一化為 trend
        normalized_strategy = strategy if strategy == "confluence" else "trend"
        if params.get("strategy") != normalized_strategy:
            params["strategy"] = normalized_strategy
            changed = True
        if params.get("value_area_pct") != 0.80:
            params["value_area_pct"] = 0.80
            changed = True
        if normalized_strategy == "confluence" and "conf_allowed_sessions" not in params:
            params["conf_allowed_sessions"] = list(DEFAULT_ALLOWED_SESSIONS)
            changed = True
        if normalized_strategy == "trend" and "tr_allowed_sessions" not in params:
            params["tr_allowed_sessions"] = list(DEFAULT_ALLOWED_SESSIONS)
            changed = True
        if normalized_strategy == "trend" and "tr_overlap_trade_tf" not in params:
            params["tr_overlap_trade_tf"] = "merged"
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
        presets[_DEFAULT_LAST_USED_PRESET] = dict(_BUILTIN_PRESETS[_DEFAULT_LAST_USED_PRESET])
        changed = True

    for key in ("last_used_bt", "last_used_live"):
        if key not in data or (
            data.get(key) != "default" and data.get(key) not in presets
        ):
            data[key] = _DEFAULT_LAST_USED_PRESET if _DEFAULT_LAST_USED_PRESET in presets else next(iter(presets))
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
            "last_used_bt": _DEFAULT_LAST_USED_PRESET,
            "last_used_live": _DEFAULT_LAST_USED_PRESET,
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



