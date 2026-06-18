# ============================================================

# 文件: backend/api/routes.py
# 狀態: v0.17.0
# 功能 / Features:
#   - FastAPI REST routes for config, historical candles, backtest, machine learning,
#     live engine, presets, and trade history.
#   - Machine Learning uses /backtest/ml-run|full-results|full-progress.
#   - Presets use the trend strategy and full_tp_lock.
#   - Value Area is locked to 80%; live/latest-candle routes use completed 1m bars.
# ============================================================

from __future__ import annotations
import os
import csv
import json
import logging
import math
import asyncio
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.db.models import (
    BacktestConfig, BarUnit, Candle, StrategyParams,
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
    """v0.17.0 locks every route to 80% Value Area."""
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


def _normalize_value_area_pct(value) -> float:
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return 0.80
    if pct > 1.0:           # accept percent form (e.g. 80 -> 0.80)
        pct = pct / 100.0
    return max(0.50, min(0.95, pct))


def _normalize_rr_ratio(value, default: int = 2) -> int:
    """Reward:risk multiple, selectable 1..10."""
    try:
        rr = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    return max(1, min(10, rr))


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
    """Normalise the variable-RR grid. None/empty → None (fixed RR). Accepts a
    list or a comma string of RRs; returns a sorted tuple of positive floats."""
    if not val:
        return None
    if isinstance(val, str):
        parts = [p for p in val.split(",") if p.strip()]
    else:
        parts = list(val)
    try:
        rrs = sorted({float(p) for p in parts if float(p) > 0})
    except (TypeError, ValueError):
        return None
    return rrs or None


def _build_strategy_params_from_request(req, contract_size: int) -> StrategyParams:
    # v0.19: "confluence" selects the explainable ML engine; anything else is trend.
    strategy = ("confluence" if str(getattr(req, "strategy", "trend") or "").strip().lower()
                == "confluence" else _normalize_strategy_name(getattr(req, "strategy", "trend")))
    tr = _strategy_leg_params(req, "tr")
    return StrategyParams(
        strategy=strategy,
        conf_band_ticks=float(getattr(req, "conf_band_ticks", 8.0) or 8.0),
        conf_min_distinct_tf=int(getattr(req, "conf_min_distinct_tf", 3) or 3),
        conf_rr=float(getattr(req, "conf_rr", 1.5) or 1.5),
        conf_wait_minutes=int(getattr(req, "conf_wait_minutes", 60) or 60),
        conf_base_minutes=int(getattr(req, "conf_base_minutes", 1) or 1),
        conf_min_prob=float(getattr(req, "conf_min_prob", 0.0) or 0.0),
        conf_ev_floor=_conf_ev_floor_opt(getattr(req, "conf_ev_floor", None)),
        conf_rr_grid=_conf_rr_grid_opt(getattr(req, "conf_rr_grid", None)),
        conf_use_scorer=bool(getattr(req, "conf_use_scorer", True)),
        conf_enable_breakout=bool(getattr(req, "conf_enable_breakout", True)),
        conf_trail_trigger_pct=float(getattr(req, "conf_trail_trigger_pct", 0.0) or 0.0),
        conf_trail_lock_pct=float(getattr(req, "conf_trail_lock_pct", 0.0) or 0.0),
        conf_full_tp_lock=int(getattr(req, "conf_full_tp_lock", 0) or 0),
        conf_session_limit=bool(getattr(req, "conf_session_limit", True)),
        conf_shadow=bool(getattr(req, "conf_shadow", False)),
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
    strategy: str = "trend"
    tp_ticks: int = 200
    sl_ticks: int = 50
    trail_sl_ticks: int = 10
    trail_sl_pct: Optional[float] = 0.05
    trail_trigger_pct: float = 0.30
    trail_enabled: bool = True            # v0.11+: master trail switch
    tr_tp_ticks: Optional[int] = None
    tr_sl_ticks: Optional[int] = None
    tr_trail_sl_ticks: Optional[int] = None
    tr_trail_sl_pct: Optional[float] = None
    tr_trail_trigger_pct: Optional[float] = None
    tr_trail_enabled: Optional[bool] = None
    tr_full_tp_lock: Optional[int] = None
    candle_seconds: int = 60
    value_area_pct: float = 0.80
    area_timeframe: str = "5m"
    rr_ratio: int = 2                     # reward:risk multiple (1..10)
    # Contract & sizing (defaults to 3× Micro NQ)
    contract_id: str = "CON.F.US.MNQ.M26"
    contract_size: int = 3
    full_tp_lock: int = 0                 # 0=OFF, 1/2/3 TP exits
    one_trade_per_session_direction: bool = True
    tr_one_trade_per_session: bool = True
    # Zone stability is enabled by default; keep this flag for future experiments.
    skip_zone_stability: bool = False
    breakout_confirm_bars: int = 7
    # v0.18: "single" = one area timeframe; "overlap" = enter at the AVERAGE
    # overlapping VAH/VAL of the timeframes in tf_combo (reproduces an ML overlap row).
    method: str = "single"
    tf_combo: Optional[List[str]] = None
    # v0.19: confluence (explainable ML scorer) backtest. When strategy=="confluence"
    # the multi-timeframe weighted-level engine is used instead of the trend engine.
    conf_band_ticks: float = 8.0          # level-cluster band width (ticks)
    conf_min_distinct_tf: int = 3         # cluster needs >= this many timeframes
    conf_rr: float = 1.5                  # reward:risk multiple for TP
    conf_wait_minutes: int = 60           # one-shot limit-order fill timeout
    conf_base_minutes: int = 1            # input candle resolution (1 or 5)
    conf_min_prob: float = 0.0            # gate: skip signals below this win-prob (0=off)
    conf_ev_floor: Optional[float] = None # EV-priority gate: keep EV>=floor (None=win-prob gate; 0=every +EV)
    conf_rr_grid: Optional[List[float]] = None  # variable-RR grid (None=fixed conf_rr); needs EV scorer
    conf_use_scorer: bool = True          # True=trained JSON, False=heuristic prior
    conf_enable_breakout: bool = True     # include breakout-retrace candidate (False=momentum+reversion only)
    # --- STYLE: optional exit-policy (break-even / trail / lock). All-OFF == original behaviour ---
    conf_trail_trigger_pct: float = 0.0   # 0 = trailing OFF; else fraction of entry→TP distance that fires break-even
    conf_trail_lock_pct: float = 0.0      # locked SL as fraction of TP distance on trigger (0=pure break-even)
    conf_full_tp_lock: int = 0            # 0 = OFF; stop new entries after N full-TP exits/session
    conf_session_limit: bool = True       # one trade per session+direction (existing rule)


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


@router.post("/data/detect-zones")
async def detect_zones(req: DetectZonesRequest = DetectZonesRequest()):
    """Run zone detection on stored candles — returns zones with VP profiles.

    When ``all_timeframes`` is set, detection runs for every ML timeframe
    (5m/15m/30m/1h/4h) and the zones are returned together, each tagged with
    its own ``timeframe`` so the chart can overlay all VAH/VAL/POC at once.
    """
    from backend.strategy.consolidation import build_zone_detector

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
        zone_list = []
        for tf in ML_TIMEFRAMES:
            detector = build_zone_detector(area_timeframe=tf, value_area_pct=value_area_pct)
            for c in sorted_candles:
                detector.update(c)
            for z in detector.get_all_zones():
                zone_list.append(_zone_to_dict(z, tf))
        return {
            "zones": zone_list,
            "count": len(zone_list),
            "area_timeframe": "all",
            "timeframes": list(ML_TIMEFRAMES),
        }

    area_timeframe = _normalize_area_timeframe(getattr(req, "area_timeframe", "5m"))
    detector = build_zone_detector(
        area_timeframe=area_timeframe,
        value_area_pct=value_area_pct,
    )
    for c in sorted_candles:
        detector.update(c)

    zone_list = [_zone_to_dict(z, area_timeframe) for z in detector.get_all_zones()]
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


def _get_conf_timeline(candles, timeframes, tick, depth, base):
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
        return entry[1]
    import time as _t
    _t0 = _t.perf_counter()
    logger.info(f"[Confluence] building zone timeline over {len(candles)} candles…")
    timeline = build_zone_timeline(candles, timeframes, tick, depth)
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

    try:
        from backend.backtest import confluence_worker
        loop = asyncio.get_running_loop()
        out = await loop.run_in_executor(
            _get_bt_executor(), confluence_worker.run_job, ckey, send_candles, params,
        )
        _bt_last_candle_key = ckey
    except Exception as e:
        logger.warning(f"[Confluence] backtest process failed ({e}); falling back to in-thread run")
        _bt_last_candle_key = None  # worker state unknown → resend candles next time
        return await asyncio.to_thread(_run_confluence_backtest, req)

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
    return resp


def _run_confluence_backtest(req: BacktestRequest) -> BacktestResponse:
    """v0.19: explainable multi-timeframe confluence backtest.

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

    # variable-RR grid (None = fixed conf_rr) — also selects the EV scorer.
    rr_grid = _conf_rr_grid_opt(getattr(req, "conf_rr_grid", None))
    # scorer: EV (variable-RR) model when RR optimisation is on and present,
    # else trained fixed-RR JSON, else the interpretable prior. Same chooser as
    # the live engine, so live == backtest.
    scorer = resolve_scorer(req.conf_use_scorer, rr_grid)

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
    # variable-RR: pick the EV-maximising RR per candidate.
    sig_cfg.rr_grid = tuple(rr_grid) if rr_grid else None
    sig_cfg.enable_breakout = bool(getattr(req, "conf_enable_breakout", True))
    run_cfg = ConfluenceBacktestConfig(
        wait_minutes=req.conf_wait_minutes, min_score=min_score,
        base_minutes=base, timeframes=timeframes,
        one_trade_per_session_direction=bool(getattr(req, "conf_session_limit",
                                                      req.one_trade_per_session_direction)),
        trail_trigger_pct=float(getattr(req, "conf_trail_trigger_pct", 0.0) or 0.0),
        trail_lock_pct=float(getattr(req, "conf_trail_lock_pct", 0.0) or 0.0),
        full_tp_lock=int(getattr(req, "conf_full_tp_lock", 0) or 0),
    )
    bt_cfg = BacktestConfig(
        initial_capital=req.initial_capital,
        symbol=_extract_symbol(req.contract_id),
        commission_rt=get_commission_rt(req.contract_id),
        fees_rt=get_fees_rt(req.contract_id),
    )

    timeline = _get_conf_timeline(candles, timeframes, tick, MAX_RECENCY_DEPTH, base)
    bt = ConfluenceBacktester(
        signal_cfg=sig_cfg, run_cfg=run_cfg, contract_id=req.contract_id,
        contract_size=contract_size, bt_config=bt_cfg, scorer=scorer,
    )
    result = bt.run(candles, zones_timeline=timeline)
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


# Default variable-RR grid the EV model is always trained on (option B), so the
# user can flip to 變動 RR in live/backtest without re-learning.
DEFAULT_EV_RR_GRID = [1.0, 1.5, 2.0, 2.5, 3.0]


class ConfluenceTrainRequest(BaseModel):
    """Retrain the explainable confluence scorer on the loaded 1m history."""
    contract_id: str = "CON.F.US.MNQ.M26"
    train_frac: float = 1.0      # 1.0 = learn from ALL loaded data (for going live)
    stride: int = 5
    wait_min: int = 60
    horizon_min: int = 1440
    band_ticks: float = 8.0
    min_distinct_tf: int = 3
    rr: float = 1.5
    rr_grid: Optional[List[float]] = None   # when set, ALSO train the variable-RR EV model


def _train_confluence_ev_sync(train, req, timeframes, tick, wait_bars, horizon_bars, rr_grid) -> dict:
    """Train the variable-RR (EV) scorer on the same split and save it to
    confluence_scorer_ev.json. Reuses the CLI multi-RR labeler/fitter so the web
    EV model is identical to `python -m scripts.train_confluence_ev`."""
    from datetime import datetime as _dt
    import numpy as np

    from backend.strategy.confluence import ConfluenceConfig, MAX_RECENCY_DEPTH
    from backend.strategy.confluence_scorer import ConfluenceScorer, default_ev_scorer_path
    from backend.backtest.confluence_backtest import build_zone_timeline
    from scripts.train_confluence_ev import collect as collect_ev
    from scripts.train_confluence import evaluate_and_meta

    cfg = ConfluenceConfig(band_ticks=req.band_ticks, min_distinct_tf=req.min_distinct_tf, rr=rr_grid[0])
    cfg.direction_mode = "auto"
    cfg.tick_size = tick
    timeline = build_zone_timeline(train, timeframes, tick, MAX_RECENCY_DEPTH)
    X, y, meta, starts, ends = collect_ev(train, timeline, cfg, tuple(rr_grid),
                                          req.stride, wait_bars, horizon_bars)
    if len(y) < 50:
        raise ValueError(f"EV 模型可標記樣本太少 ({len(y)})。")
    weights, b_raw, info = evaluate_and_meta(
        X, y, starts, ends, n_bars=len(train), embargo=wait_bars + horizon_bars)
    scorer = ConfluenceScorer(
        weights=weights, bias=float(b_raw),
        meta={
            "kind": "logistic_ev", "trained": True, "multi_rr": True,
            "rr_grid": list(rr_grid),
            "trained_at": _dt.now().isoformat(timespec="seconds"),
            "data_start": train[0].timestamp.isoformat() if train else None,
            "data_end": train[-1].timestamp.isoformat() if train else None,
            "contract": req.contract_id, "base_min": 1, "timeframes": list(timeframes),
            "n_samples": int(len(y)), "train_win_rate": float(y.mean()),
            "train_auc": info["auc"], "train_brier": info["brier"], "C": info["C"],
            "oos_auc": info["oos_auc"], "oos_brier": info["oos_brier"],
            "oos_folds": info["oos_folds"], "dropped_features": info["dropped_features"],
            "std_weights": info["std_weights"],
            "source": "web_learn", "cfg": {"band_ticks": req.band_ticks,
            "min_distinct_tf": req.min_distinct_tf, "rr_grid": list(rr_grid),
            "wait_min": req.wait_min, "horizon_min": req.horizon_min},
        },
    )
    out = default_ev_scorer_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    scorer.save(out)
    return {"n_samples": int(len(y)), "win_rate": float(y.mean()),
            "train_auc": info["auc"], "oos_auc": info["oos_auc"],
            "saved_to": str(out), "rr_grid": list(rr_grid)}


def _train_confluence_scorer_sync(candles, req: "ConfluenceTrainRequest") -> dict:
    """Blocking trainer (run in a threadpool). Standardized on 1m base — fits a
    logistic scorer on forward-scan labels and writes confluence_scorer.json.
    Reuses the SAME collect()/evaluate_and_meta() as scripts/train_confluence.py
    so the web result is identical to the CLI trainer (drop-constant fit,
    time-series-CV C, uniqueness weighting, embargoed walk-forward OOS)."""
    from datetime import datetime as _dt

    from backend.strategy.confluence import ConfluenceConfig, MAX_RECENCY_DEPTH
    from backend.strategy.confluence_scorer import ConfluenceScorer
    from backend.strategy.consolidation import timeframes_for_base
    from backend.backtest.confluence_backtest import build_zone_timeline
    from scripts.train_confluence import collect, evaluate_and_meta

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
                    "rr": req.rr, "wait_min": req.wait_min, "horizon_min": req.horizon_min},
        },
    )
    out = _confluence_scorer_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    scorer.save(out)

    sw = info["std_weights"]
    top = sorted(sw, key=lambda k: abs(sw[k]), reverse=True)[:5]
    result = {
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
    }

    # ALWAYS train the EV (multi-RR) model on the SAME split so the user can
    # switch to 變動 RR anytime WITHOUT re-learning. Honor the panel's grid if
    # one is selected, else learn the full default grid. EV failure (e.g. too
    # few labelable samples) must NOT fail the whole LEARN — the fixed model is
    # already saved — so report it softly.
    rr_grid = _conf_rr_grid_opt(getattr(req, "rr_grid", None)) or DEFAULT_EV_RR_GRID
    try:
        result["ev_model"] = _train_confluence_ev_sync(
            train, req, timeframes, tick, wait_bars, horizon_bars, rr_grid)
    except Exception as e:  # noqa: BLE001 - keep fixed-model LEARN successful
        result["ev_model_error"] = str(e)
    return result


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
    """Return the full saved scorer(s) — meta + raw-space weights — for the
    LEARN RESULT panel. Reads the on-disk JSON the last LEARN wrote (fixed-RR +
    variable-RR EV model), so the UI shows exactly what live/backtest will use."""
    import json
    from backend.strategy.confluence_scorer import default_scorer_path, default_ev_scorer_path

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
        "ev": _load(default_ev_scorer_path()),
    }


# ════════════════════════════════════════════════════════════════════════════
# MODEL REGISTRY — the pre-trained grid (RR × BAND × MIN_DISTINCT_TF × BREAKOUT)
# Mirrors scripts/train_model_grid.py so the web MODEL dropdown can list EVERY
# combo (trained → selectable, missing → "(untrain)"), ACTIVATE one (copy its
# weights to the canonical scorer so backtest/live use it) or RETRAIN one.
# ════════════════════════════════════════════════════════════════════════════
_GRID_RR = (1.0, 2.0, 3.0)
_GRID_BAND = (4.0, 6.0, 8.0, 10.0, 12.0)
_GRID_TF = (2, 3, 4, 5)
_GRID_BRK = (True, False)


def _grid_dir() -> Path:
    from backend.strategy.confluence_scorer import default_scorer_path
    return default_scorer_path().parent / "grid"


def _grid_model_name(rr, band, tf, brk) -> str:
    """Canonical registry stem — MUST match scripts/train_model_grid.model_name."""
    return f"rr{int(round(rr))}_b{int(round(band))}_tf{tf}_{'brk' if brk else 'nobrk'}"


@router.get("/confluence/models")
async def confluence_models():
    """List the full model-registry grid for the MODEL dropdown. Every combo is
    returned; `trained` reflects whether data/models/grid/<name>.json exists on
    disk (authoritative — independent of any partial manifest)."""
    import json as _json
    gdir = _grid_dir()
    models = []
    for rr in _GRID_RR:
        for band in _GRID_BAND:
            for tf in _GRID_TF:
                for brk in _GRID_BRK:
                    name = _grid_model_name(rr, band, tf, brk)
                    p = gdir / f"{name}.json"
                    rec = {"name": name, "rr": rr, "band": band,
                           "min_distinct_tf": tf, "breakout": brk,
                           "trained": p.exists()}
                    if p.exists():
                        try:
                            meta = _json.loads(p.read_text(encoding="utf-8")).get("meta", {})
                            rec.update(
                                n_samples=meta.get("n_samples"),
                                train_auc=meta.get("train_auc"),
                                oos_auc=meta.get("oos_auc"),
                                win_rate=meta.get("train_win_rate"),
                                loss_weight=meta.get("loss_weight", 1.0),
                                trained_at=meta.get("trained_at"),
                            )
                        except Exception:  # noqa: BLE001 — a corrupt file still lists
                            pass
                    models.append(rec)
    return {"grid": {"rr": list(_GRID_RR), "band": list(_GRID_BAND),
                     "min_distinct_tf": list(_GRID_TF), "breakout": list(_GRID_BRK)},
            "n_models": len(models),
            "trained": sum(1 for m in models if m["trained"]),
            "models": models}


class ModelActivateRequest(BaseModel):
    name: str


@router.post("/confluence/models/activate")
async def confluence_model_activate(req: ModelActivateRequest):
    """Activate a pre-trained grid model: copy its JSON onto the canonical
    confluence_scorer.json so backtest/live load it, and return its cfg so the UI
    can mirror band / min_distinct_tf / rr / breakout into the MODEL panel."""
    import json as _json
    import shutil
    from backend.strategy.confluence_scorer import default_scorer_path

    src = _grid_dir() / f"{req.name}.json"
    if not src.exists():
        raise HTTPException(status_code=404, detail=f"模型未訓練 (untrain): {req.name}")
    dst = default_scorer_path()
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    meta = _json.loads(src.read_text(encoding="utf-8")).get("meta", {})
    return {"success": True, "name": req.name, "activated_to": str(dst),
            "cfg": meta.get("cfg", {}), "meta": meta}


class ModelRetrainRequest(BaseModel):
    """Retrain ONE registry model on the loaded history (cost-sensitive capable)."""
    name: str
    rr: float
    band_ticks: float
    min_distinct_tf: int
    enable_breakout: bool = False
    loss_weight: float = 1.0      # >1 = loss-averse: higher PF / lower maxDD / fewer trades
    stride: int = 5
    wait_min: int = 60
    horizon_min: int = 1440
    contract_id: str = "CON.F.US.MNQ.M26"
    activate: bool = True          # also copy to confluence_scorer.json on success


def _retrain_grid_model_sync(candles, req: "ModelRetrainRequest") -> dict:
    """Blocking single-model retrain (run off the event loop). Reuses the SAME
    collect()/evaluate_and_meta() as the CLI/grid trainers so the web-trained
    model is identical to a grid-trained one — plus the cost-sensitive loss_weight."""
    from datetime import datetime as _dt
    import shutil

    from backend.strategy.confluence import ConfluenceConfig, MAX_RECENCY_DEPTH
    from backend.strategy.confluence_scorer import ConfluenceScorer, default_scorer_path
    from backend.strategy.consolidation import timeframes_for_base
    from backend.backtest.confluence_backtest import build_zone_timeline
    from scripts.train_confluence import collect, evaluate_and_meta

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
            "kind": "logistic", "trained": True, "grid_model": req.name,
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
    out = _grid_dir() / f"{req.name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    scorer.save(out)
    if req.activate:
        shutil.copyfile(out, default_scorer_path())

    sw = info["std_weights"]
    top = sorted(sw, key=lambda k: abs(sw[k]), reverse=True)[:5]
    return {
        "success": True, "name": req.name, "activated": bool(req.activate),
        "n_samples": int(len(y)), "win_rate": float(y.mean()),
        "train_auc": info["auc"], "oos_auc": info["oos_auc"],
        "oos_brier": info["oos_brier"], "loss_weight": req.loss_weight,
        "top_weights": [{"name": n, "weight": round(sw[n], 4)} for n in top],
        "saved_to": str(out),
    }


@router.post("/confluence/models/retrain")
async def confluence_model_retrain(req: ModelRetrainRequest):
    """Retrain ONE registry model (CPU-heavy fit off the event loop). On success
    the model JSON is written to data/models/grid/ and (by default) activated."""
    if not _historical_candles:
        raise HTTPException(status_code=400, detail="請先拉取歷史數據再訓練")
    candles = sorted(_historical_candles, key=lambda c: c.timestamp)
    try:
        return await asyncio.to_thread(_retrain_grid_model_sync, candles, req)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


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

    if not _historical_candles:
        raise HTTPException(
            status_code=400,
            detail="請先通過 /api/data/fetch-historical 拉取數據"
        )

    # v0.19: explainable confluence engine (separate, read-only path)
    if str(req.strategy or "").strip().lower() == "confluence":
        await _refresh_recent_historical_candles(req.contract_id)
        # Heavy full-history backtest: run in a dedicated child PROCESS so the
        # CPU-bound work never holds the server's GIL — data-fetch / live / chart
        # stay responsive while it computes (falls back to in-thread on failure).
        return await _run_confluence_backtest_proc(req)

    await _refresh_recent_historical_candles(req.contract_id)

    # v0.11+: derive symbol + per-contract fees from the chosen contract_id so
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

    # v0.18: overlap mode reproduces an ML overlap row — enter at the AVERAGE
    # overlapping VAH/VAL of the timeframes in tf_combo via a merged zone timeline.
    method = str(getattr(req, "method", "single") or "single").lower()
    tf_combo = tuple(t for t in (getattr(req, "tf_combo", None) or []) if t in ML_TIMEFRAMES)
    overlap_mode = method == "overlap" and len(tf_combo) >= 2

    _overlap_zone_timeline = None
    if overlap_mode:
        ordered = [tf for tf in ML_TIMEFRAMES if tf in tf_combo]
        _ov_candles = sorted(_historical_candles, key=lambda c: c.timestamp)
        _ov_timelines = [
            _precompute_zone_timeline(_ov_candles, value_area_pct, False, tf)
            for tf in ordered
        ]
        _overlap_zone_timeline = _merge_zone_timelines(_ov_timelines, tuple(ordered))

    engine = BacktestEngine(
        config,
        strategy_params=strategy_params,
        zone_timeline=_overlap_zone_timeline,
    )

    # Use 1m candles directly (SessionTrendFollow works on 1m)
    candles = sorted(_historical_candles, key=lambda c: c.timestamp) if overlap_mode else list(_historical_candles)

    result = engine.run(candles)
    _backtest_results.append(result)

    # 轉換為回應格式
    trades_resp = []
    symbol_label = "/" + config.symbol   # TopstepX-style "/NQ"
    for t in result.trades:
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
                max_vol = max(vp.profile.values()) if vp.profile else 1
                profile_data = [
                    {"price": p, "volume": v, "pct": round(v / max_vol, 3)}
                    for p, v in sorted_levels
                ]
            except Exception:
                profile_data = []
        elif getattr(z, "profile", None):
            # Merged/slim zones carry a precomputed histogram instead of candles.
            prof = z.profile or {}
            max_vol = max(prof.values()) if prof else 1
            profile_data = [
                {"price": p, "volume": v, "pct": round(v / max_vol, 3)}
                for p, v in sorted(prof.items())
            ]

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


# ── Machine Learning: Run all SL/TP/Trail combinations ────────────────

_ml_results_cache: List[dict] = []
_ml_progress: dict = {"current": 0, "total": 0, "stage": ""}

import threading as _threading
_ml_progress_lock = _threading.Lock()


def _request_payload(model: BaseModel) -> dict:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def _json_safe(value):
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def _ml_total_loss(r: dict) -> float:
    if r.get("total_loss") is not None:
        try:
            return float(r.get("total_loss") or 0)
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(r.get("avg_loss") or 0) * float(r.get("losses") or 0)
    except (TypeError, ValueError):
        return 0.0


def _ml_profit_factor(r: dict) -> float:
    """Profit Factor = gross gain / gross loss. Falls back to gain/|loss| when
    the stored profit_factor is missing (older rows)."""
    pf = r.get("profit_factor")
    if pf is not None:
        try:
            return float(pf)
        except (TypeError, ValueError):
            pass
    try:
        gain = abs(float(r.get("total_gain") or 0))
    except (TypeError, ValueError):
        gain = 0.0
    loss = abs(_ml_total_loss(r))
    if loss > 0:
        return gain / loss
    return 999.0 if gain > 0 else 0.0


def _ml_valid_trade_range(r: dict) -> bool:
    # New ML model (v0.18) has no fixed SL/TP ticks; every non-errored run is valid.
    return isinstance(r, dict) and not r.get("error")


def _enrich_ml_result(r: dict) -> dict:
    # Profit Factor + all metrics are already populated by the backtest run.
    return r


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


# Columns written to data/machinelearning/ml_results.csv (Phase 8: all metrics).
_ML_CSV_COLUMNS = [
    "rank", "method", "tf_label", "tf_combo", "overlap_count", "overlap_pct",
    "rr_ratio", "area_timeframe", "value_area_pct", "contract_id",
    "contract_size", "total_trades", "wins", "losses", "win_rate",
    "total_pnl", "total_gain", "total_loss",
    "max_drawdown", "calmar_ratio", "profit_factor", "avg_rr_ratio",
    "avg_win", "avg_loss", "consistency_pct", "max_day_pct",
    "weekly_count", "weekly_mean", "weekly_std", "weekly_cv",
    "weekly_min", "weekly_max", "weekly_range", "positive_weeks",
    "weekly_consistency", "weekly_pnls", "pass_max_dd", "error",
]


def _ml_csv_row(r: dict) -> dict:
    """Flatten one ML result into the CSV column set (stringify list fields)."""
    row = {}
    for col in _ML_CSV_COLUMNS:
        val = r.get(col, "")
        if col == "tf_combo" and isinstance(val, (list, tuple)):
            val = "+".join(str(x) for x in val)
        elif col == "weekly_pnls" and isinstance(val, (list, tuple)):
            val = ";".join(str(x) for x in val)
        elif val is None:
            val = ""
        row[col] = val
    return row


def _save_ml_artifacts(req: BaseModel, ranked: List[dict], total_combinations: int) -> dict:
    """Persist the latest Machine learning run in AI-readable JSON + compact Markdown."""
    data_dir = Path(__file__).resolve().parents[2] / "data" / "machinelearning"
    data_dir.mkdir(parents=True, exist_ok=True)

    generated_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    json_path = data_dir / "ml_latest.json"
    md_path = data_dir / "ml_summary.md"
    csv_path = data_dir / "ml_results.csv"

    payload = {
        "kind": "ml_results",
        "generated_at": generated_at,
        "total_combinations": total_combinations,
        "request": _json_safe(_request_payload(req)),
        "results": _json_safe(ranked),
        "top_results": _json_safe(ranked[:50]),
    }
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str, allow_nan=False)

    # Phase 8: complete CSV with every metric + weekly variation, all combos.
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_ML_CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for r in ranked:
            writer.writerow(_ml_csv_row(_enrich_ml_result(r)))

    lines = [
        "# Machine Learning Summary",
        "",
        f"- Generated: {generated_at}",
        f"- Total combinations: {total_combinations}",
        f"- Saved JSON: `{json_path}`",
        f"- Saved CSV: `{csv_path}`",
        "",
        "| Rank | Method | Timeframes | Overlap | RR | Trades | Win% | Final PnL | Max DD | PF | Calmar | Wk Std | Wk CV |",
        "| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in [_enrich_ml_result(r) for r in ranked if not r.get("error")][:25]:
        win_pct = round(float(r.get("win_rate", 0) or 0) * 100, 1)
        tf_combo = r.get("tf_combo")
        if isinstance(tf_combo, (list, tuple)):
            tf_combo = "+".join(str(x) for x in tf_combo)
        lines.append(
            "| {rank} | {method} | {tf} | {overlap} | 1:{rr} | {trades} | {win}% | ${pnl} | ${dd} | "
            "{pf} | {calmar} | {wstd} | {wcv} |".format(
                rank=r.get("rank", ""),
                method=r.get("method", ""),
                tf=r.get("tf_label", tf_combo or ""),
                overlap=r.get("overlap_count", ""),
                rr=r.get("rr_ratio", ""),
                trades=r.get("total_trades", ""),
                win=win_pct,
                pnl=r.get("total_pnl", ""),
                dd=r.get("max_drawdown", ""),
                pf=r.get("profit_factor", ""),
                calmar=r.get("calmar_ratio", ""),
                wstd=r.get("weekly_std", ""),
                wcv=r.get("weekly_cv", ""),
            )
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {
        "json": str(json_path),
        "summary": str(md_path),
        "csv": str(csv_path),
        "generated_at": generated_at,
    }


def _save_conf_combo_artifacts(req: BaseModel, ranked: List[dict], total: int,
                               held: dict) -> dict:
    """Persist the COMBINATION (confluence Model+Style sweep) in AI-readable
    JSON + Markdown + CSV. Each row carries the explicit knob values (RR /
    breakout / trail / lock / full-tp / session) plus full metrics so an AI can
    diagnose which combination hurts win-rate/PnL without re-running."""
    data_dir = Path(__file__).resolve().parents[2] / "data" / "machinelearning"
    data_dir.mkdir(parents=True, exist_ok=True)

    generated_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    json_path = data_dir / "combination_latest.json"
    md_path = data_dir / "combination_summary.md"
    csv_path = data_dir / "combination_results.csv"

    payload = {
        "kind": "confluence_combination",
        "generated_at": generated_at,
        "total_combinations": total,
        "held_constant": _json_safe(held),
        "grid": {
            "rr": list(CONF_COMBO_RR),
            "enable_breakout": list(CONF_COMBO_BREAKOUT),
            "trail_trigger_pct": list(CONF_COMBO_TRAIL_TRIGGER),
            "full_tp_lock": list(CONF_COMBO_FULL_TP_LOCK),
            "session_limit": list(CONF_COMBO_SESSION),
        },
        "request": _json_safe(_request_payload(req)),
        "results": _json_safe(ranked),
    }
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str, allow_nan=False)

    cols = ["rank", "rr_ratio", "conf_enable_breakout", "conf_trail_trigger_pct",
            "conf_trail_lock_pct", "conf_full_tp_lock", "conf_session_limit",
            "total_trades", "wins", "losses", "win_rate", "total_pnl",
            "max_drawdown", "profit_factor", "calmar_ratio", "expectancy",
            "avg_win", "avg_loss"]
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for r in ranked:
            writer.writerow({k: r.get(k, "") for k in cols})

    lines = [
        "# Confluence COMBINATION Sweep",
        "",
        f"- Generated: {generated_at}",
        f"- Total combinations: {total}",
        f"- Held constant: {held}",
        f"- Saved JSON: `{json_path}`",
        f"- Saved CSV: `{csv_path}`",
        "",
        "| Rank | RR | Breakout | TrailTrig | Lock | FullTPLock | Session | Trades | Win% | PnL | MaxDD | PF | Calmar |",
        "| ---: | ---: | :---: | ---: | ---: | ---: | :---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in [x for x in ranked if not x.get("error")][:25]:
        lines.append(
            "| {rank} | 1:{rr} | {brk} | {trig}% | {lock}% | {ftl} | {ses} | {trades} | "
            "{win}% | ${pnl} | ${dd} | {pf} | {calmar} |".format(
                rank=r.get("rank", ""),
                rr=r.get("rr_ratio", ""),
                brk="ON" if r.get("conf_enable_breakout") else "off",
                trig=round(float(r.get("conf_trail_trigger_pct", 0) or 0) * 100),
                lock=round(float(r.get("conf_trail_lock_pct", 0) or 0) * 100),
                ftl=r.get("conf_full_tp_lock", 0) or "off",
                ses="ON" if r.get("conf_session_limit") else "off",
                trades=r.get("total_trades", ""),
                win=round(float(r.get("win_rate", 0) or 0) * 100, 1),
                pnl=r.get("total_pnl", ""),
                dd=r.get("max_drawdown", ""),
                pf=r.get("profit_factor", ""),
                calmar=r.get("calmar_ratio", ""),
            )
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {
        "json": str(json_path),
        "summary": str(md_path),
        "csv": str(csv_path),
        "generated_at": generated_at,
    }


def _ml_sort_value(r: dict, col: str):
    col = (col or "calmar").lower()
    if col == "rank":
        return r.get("rank") or 0
    if col == "strategy":
        return str(r.get("strategy") or "").lower()
    if col == "contract":
        return _extract_symbol(str(r.get("contract_id") or "")).lower()
    if col == "size":
        return r.get("contract_size") or 0
    if col == "area":
        return r.get("value_area_pct") or 0
    if col == "method":
        return str(r.get("method") or "").lower()
    if col == "tf":
        tf = r.get("tf_label")
        if not tf:
            combo = r.get("tf_combo")
            tf = "+".join(combo) if isinstance(combo, (list, tuple)) else ""
        return str(tf).lower()
    if col == "overlap":
        return r.get("overlap_count") or 0
    if col == "rr":
        return r.get("rr_ratio") or 0
    if col == "wk_std":
        return r.get("weekly_std") or 0
    if col == "wk_cv":
        return r.get("weekly_cv") or 0
    if col == "trades":
        return r.get("total_trades") or 0
    if col == "win_rate":
        return r.get("win_rate") or 0
    if col == "pnl":
        return r.get("total_pnl") or 0
    if col == "max_dd":
        return r.get("max_drawdown") or 0
    if col in ("pf", "profit_factor", "lwr", "loss_to_final_ratio", "loss_final_ratio"):
        return _ml_profit_factor(r)
    if col == "best_day":
        vals = list((r.get("daily_pnl") or {}).values())
        return max(vals) if vals else 0
    if col == "worst_day":
        vals = list((r.get("daily_pnl") or {}).values())
        return min(vals) if vals else 0
    return r.get("calmar_ratio") or 0


def _sorted_ml_results(
    results: list,
    sort_col: str = "calmar",
    sort_dir: str = "desc",
    limit: int = ML_DISPLAY_LIMIT,
) -> list:
    try:
        limit = max(1, min(int(limit or ML_DISPLAY_LIMIT), 1000))
    except (TypeError, ValueError):
        limit = ML_DISPLAY_LIMIT
    reverse = (sort_dir or "desc").lower() != "asc"
    valid = [
        _enrich_ml_result(r)
        for r in (results or [])
        if isinstance(r, dict) and not r.get("error") and _ml_valid_trade_range(r)
    ]
    errors = [
        r for r in (results or [])
        if isinstance(r, dict) and r.get("error") and _ml_valid_trade_range(r)
    ]
    sorted_valid = sorted(
        valid,
        key=lambda r: (_ml_sort_value(r, sort_col), -(r.get("rank") or 0)),
        reverse=reverse,
    )
    return (sorted_valid + errors)[:limit]


def _load_ml_artifact(
    sort_col: str = "calmar",
    sort_dir: str = "desc",
    limit: int = ML_DISPLAY_LIMIT,
) -> dict:
    """Load the latest persisted Machine learning display payload."""
    json_path = Path(__file__).resolve().parents[2] / "data" / "machinelearning" / "ml_latest.json"
    if not json_path.exists():
        return {"results": [], "artifact": None}
    try:
        with json_path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        all_results = payload.get("results") or payload.get("top_results") or []
        results = _sorted_ml_results(all_results, sort_col, sort_dir, limit)
        return {
            "results": results,
            "total_combinations": payload.get("total_combinations", len(all_results)),
            "shown": len(results),
            "sort_col": sort_col,
            "sort_dir": sort_dir,
            "generated_at": payload.get("generated_at", ""),
            "artifact": {
                "json": str(json_path),
                "summary": str(json_path.with_name("ml_summary.md")),
                "generated_at": payload.get("generated_at", ""),
            },
        }
    except Exception as e:
        logger.warning(f"[Machine Learning] Could not load latest artifact: {e}")
        return {"results": [], "artifact": None}


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


class MLRunRequest(BaseModel):
    strategy: str = "trend"
    tp_ticks: int = 200
    sl_ticks: int = 50
    trail_sl_ticks: int = 10
    trail_sl_pct: Optional[float] = 0.05
    trail_trigger_pct: float = 0.30
    tr_tp_ticks: Optional[int] = None
    tr_sl_ticks: Optional[int] = None
    tr_trail_sl_ticks: Optional[int] = None
    tr_trail_sl_pct: Optional[float] = None
    tr_trail_trigger_pct: Optional[float] = None
    tr_trail_enabled: Optional[bool] = None
    tr_full_tp_lock: Optional[int] = None
    candle_seconds: int = 60
    value_area_pct: float = 0.80
    area_timeframe: str = "5m"
    rr_ratio: int = 2                     # default RR; ML sweeps 1..10
    initial_capital: float = 50000.0
    start_date: str = ""
    end_date: str = ""
    # v0.11+: contract / size / trail switch — keep parity with BacktestRequest
    contract_id: str = "CON.F.US.MNQ.M26"
    contract_size: int = 3
    trail_enabled: bool = True
    full_tp_lock: int = 0                 # 0=OFF, 1/2/3 TP exits
    one_trade_per_session_direction: bool = True
    tr_one_trade_per_session: bool = True
    # Zone stability is enabled by default; keep this flag for future experiments.
    skip_zone_stability: bool = False
    fixed_params: List[str] = Field(default_factory=list)
    breakout_confirm_bars: int = 7


class ConfComboRunRequest(BacktestRequest):
    """COMBINATION sweep for the ML confluence strategy. Inherits every conf_*
    knob from BacktestRequest (held at panel values); adds the backtest range.
    The 240-run grid varies RR × breakout × trail-trigger × full-tp-lock × session."""
    start_date: str = ""
    end_date: str = ""


def _run_single_combo(candles, config, strategy, sl, tp, trail, trail_pct, trigger_pct, cand_secs, zone_timeline,
                      contract_id: str = "CON.F.US.MNQ.M26",
                      contract_size: int = 3,
                      trail_enabled: bool = True,
                      full_tp_lock: int = 0,
                      one_trade_per_session_direction: bool = True,
                      skip_zone_stability: bool = False,
                      breakout_confirm_bars: int = 7,
                      tr_one_trade_per_session: bool = True,
                      area_timeframe: str = "5m",
                      rr_ratio: int = 2,
                      tr_leg: Optional[dict] = None) -> dict:
    """Run one backtest combination synchronously (called from process pool).
    zone_timeline is pre-computed once and shared across all combos — avoids re-running
    the expensive SessionZoneDetector for every parameter combination.
    """
    from backend.backtest.engine import BacktestEngine
    from backend.db.models import BacktestConfig, StrategyParams

    tr_leg = tr_leg or {}
    def _leg_value(leg: dict, key: str, fallback):
        return leg.get(key, fallback) if isinstance(leg, dict) else fallback

    sp = StrategyParams(
        strategy=strategy,
        sl_ticks=sl,
        tp_ticks=tp,
        trail_sl_ticks=trail,
        trail_trigger_pct=trigger_pct,
        trail_enabled=bool(trail_enabled),
        tr_sl_ticks=_leg_value(tr_leg, "sl_ticks", sl),
        tr_tp_ticks=_leg_value(tr_leg, "tp_ticks", tp),
        tr_trail_sl_ticks=_leg_value(tr_leg, "trail_sl_ticks", trail),
        tr_trail_trigger_pct=_leg_value(tr_leg, "trail_trigger_pct", trigger_pct),
        tr_trail_enabled=bool(_leg_value(tr_leg, "trail_enabled", trail_enabled)),
        tr_full_tp_lock=_leg_value(tr_leg, "full_tp_lock", full_tp_lock),
        candle_seconds=cand_secs,
        contract_id=contract_id,
        contract_size=_normalize_contract_size(contract_id, contract_size),
        full_tp_lock=full_tp_lock,
        one_trade_per_session_direction=bool(one_trade_per_session_direction),
        tr_one_trade_per_session=bool(tr_one_trade_per_session),
        skip_zone_stability=bool(skip_zone_stability),
        breakout_confirm_bars=max(1, int(breakout_confirm_bars or 7)),
        area_timeframe=_normalize_area_timeframe(area_timeframe),
        value_area_pct=getattr(config, "value_area_pct", 0.80),
        rr_ratio=_normalize_rr_ratio(rr_ratio),
    )
    engine = BacktestEngine(config=config, strategy_params=sp, zone_timeline=zone_timeline)
    try:
        result = engine.run(list(candles))
        m = result.metrics
        return {
            "strategy": strategy,
            "sl": sl,
            "tp": tp,
            "trail": trail,
            "trail_pct": trail_pct,
            "trail_trigger_pct": trigger_pct,
            "tr_sl": getattr(sp, "tr_sl_ticks", sl),
            "tr_tp": getattr(sp, "tr_tp_ticks", tp),
            "tr_trail": getattr(sp, "tr_trail_sl_ticks", trail),
            "tr_trail_trigger_pct": getattr(sp, "tr_trail_trigger_pct", trigger_pct),
            "tr_trail_enabled": getattr(sp, "tr_trail_enabled", trail_enabled),
            "tr_full_tp_lock": getattr(sp, "tr_full_tp_lock", full_tp_lock),
            "contract_id": contract_id,
            "contract_size": _normalize_contract_size(contract_id, contract_size),
            "value_area_pct": getattr(config, "value_area_pct", 0.80),
            "area_timeframe": _normalize_area_timeframe(area_timeframe),
            "rr_ratio": _normalize_rr_ratio(rr_ratio),
            "full_tp_lock": full_tp_lock,
            "skip_zone_stability": bool(skip_zone_stability),
            "total_trades": m.total_trades,
            "wins": m.wins,
            "losses": m.losses,
            "win_rate": round(m.win_rate, 4),
            "total_pnl": round(m.total_pnl, 2),
            "total_gain": round(getattr(m, "total_gain", 0.0), 2),
            "total_loss": round(getattr(m, "total_loss", 0.0), 2),
            "max_drawdown": round(m.max_drawdown, 2),
            "calmar_ratio": round(m.calmar_ratio, 3),
            "profit_factor": round(m.profit_factor, 3),
            "daily_pnl": m.daily_pnl,
            "avg_win": round(m.avg_win, 2),
            "avg_loss": round(m.avg_loss, 2),
        }
    except Exception as e:
        return {
            "strategy": strategy,
            "sl": sl,
            "tp": tp,
            "trail": trail,
            "trail_pct": trail_pct,
            "trail_trigger_pct": trigger_pct,
            "tr_sl": getattr(sp, "tr_sl_ticks", sl),
            "tr_tp": getattr(sp, "tr_tp_ticks", tp),
            "tr_trail": getattr(sp, "tr_trail_sl_ticks", trail),
            "tr_trail_trigger_pct": getattr(sp, "tr_trail_trigger_pct", trigger_pct),
            "tr_trail_enabled": getattr(sp, "tr_trail_enabled", trail_enabled),
            "tr_full_tp_lock": getattr(sp, "tr_full_tp_lock", full_tp_lock),
            "contract_id": contract_id,
            "contract_size": _normalize_contract_size(contract_id, contract_size),
            "value_area_pct": getattr(config, "value_area_pct", 0.80),
            "area_timeframe": _normalize_area_timeframe(area_timeframe),
            "rr_ratio": _normalize_rr_ratio(rr_ratio),
            "full_tp_lock": full_tp_lock,
            "skip_zone_stability": bool(skip_zone_stability),
            "error": str(e),
        }
    finally:
        with _ml_progress_lock:
            _ml_progress["current"] += 1


# ── ML multi-timeframe overlap sweep helpers (v0.18) ──────────────────────

ML_TIMEFRAMES = ("5m", "15m", "30m", "1h", "4h")
ML_RR_VALUES = tuple(range(1, 11))   # 1:1 .. 1:10


# ── COMBINATION (confluence Model+Style sweep) grid ───────────────────
# 5 × 2 × 4 × 3 × 2 = 240 runs. Structural MODEL knobs (band / min-distinct-tf /
# min-prob / ev-floor / timeframes / trail-lock) are HELD at the panel values;
# only the suspects that move win-rate/edge are swept.
CONF_COMBO_RR = (1.0, 1.5, 2.0, 2.5, 3.0)
CONF_COMBO_BREAKOUT = (True, False)
CONF_COMBO_TRAIL_TRIGGER = (0.0, 0.30, 0.50, 0.70)
CONF_COMBO_FULL_TP_LOCK = (0, 1, 2)
CONF_COMBO_SESSION = (True, False)


def _ml_timeframe_combos() -> list:
    """All non-empty subsets of ML_TIMEFRAMES (singles + every overlap combo).
    Ordered by combo size then timeframe order — singles first, then pairs, …
    """
    from itertools import combinations
    order = {tf: i for i, tf in enumerate(ML_TIMEFRAMES)}
    combos = []
    for k in range(1, len(ML_TIMEFRAMES) + 1):
        for c in combinations(ML_TIMEFRAMES, k):
            combos.append(tuple(sorted(c, key=lambda t: order[t])))
    return combos


def _synthesize_merged_zone(actives, tfs):
    """Average the overlapping reference zones into one synthetic zone.
    Entry levels (VAH/VAL/POC) are the mean across timeframes; the VP
    histogram is summed so the lowest-volume-node SL still works.
    """
    from backend.db.models import ConsolidationZone, ZoneStatus
    n = len(actives)
    vah = sum(z.vah_80 for z in actives) / n
    val = sum(z.val_80 for z in actives) / n
    poc = sum(z.poc for z in actives) / n
    profile = {}
    for z in actives:
        for p, v in (z.profile or {}).items():
            profile[p] = profile.get(p, 0) + v
    zid = "M:" + "+".join(str(z.zone_id) for z in actives)
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


def _merge_zone_timelines(timelines: list, tfs: tuple) -> list:
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
                mz = _synthesize_merged_zone(actives, tfs)
                entry = {"active": mz, "recent": [mz], "mature": True, "overlap": len(actives)}
                _syn_cache[key] = entry
            merged.append(entry)
        else:
            merged.append(_NONE_ENTRY)
    return merged


def _run_ml_combo(candles, config, zone_timeline, rr, tf_combo,
                  contract_id, contract_size, breakout_confirm_bars,
                  full_tp_lock, trail_trigger_pct, trail_enabled,
                  one_trade_per_session_direction, tr_one_trade_per_session,
                  fallback_sl_ticks: int = 50) -> dict:
    """Run one (timeframe-combo × RR) machine-learning backtest on a pre-built
    (merged) zone timeline. SL = lowest-volume node, TP = RR × SL (handled by
    the strategy); the fixed ticks here are only the SL fallback / nominal TP.
    """
    from backend.backtest.engine import BacktestEngine
    from backend.db.models import StrategyParams

    method = "overlap" if len(tf_combo) > 1 else "single"
    tf_label = "+".join(tf_combo)
    nominal_tp = max(1, fallback_sl_ticks * rr)
    sp = StrategyParams(
        strategy="trend",
        sl_ticks=fallback_sl_ticks, tp_ticks=nominal_tp,
        tr_sl_ticks=fallback_sl_ticks, tr_tp_ticks=nominal_tp,
        trail_trigger_pct=trail_trigger_pct, trail_enabled=bool(trail_enabled),
        tr_trail_trigger_pct=trail_trigger_pct, tr_trail_enabled=bool(trail_enabled),
        full_tp_lock=full_tp_lock, tr_full_tp_lock=full_tp_lock,
        contract_id=contract_id,
        contract_size=_normalize_contract_size(contract_id, contract_size),
        one_trade_per_session_direction=bool(one_trade_per_session_direction),
        tr_one_trade_per_session=bool(tr_one_trade_per_session),
        breakout_confirm_bars=max(1, int(breakout_confirm_bars or 7)),
        area_timeframe=tf_combo[0],
        value_area_pct=getattr(config, "value_area_pct", 0.80),
        rr_ratio=_normalize_rr_ratio(rr),
    )
    overlap_hits = sum(1 for e in zone_timeline if e.get("overlap"))
    overlap_pct = round(overlap_hits / max(1, len(zone_timeline)), 3)
    base = {
        "method": method,
        "tf_combo": list(tf_combo),
        "tf_label": tf_label,
        "overlap_count": len(tf_combo),
        "overlap_pct": overlap_pct,
        "rr_ratio": _normalize_rr_ratio(rr),
        "contract_id": contract_id,
        "contract_size": _normalize_contract_size(contract_id, contract_size),
        "value_area_pct": getattr(config, "value_area_pct", 0.80),
        "full_tp_lock": full_tp_lock,
    }
    try:
        engine = BacktestEngine(config=config, strategy_params=sp, zone_timeline=zone_timeline,
                                record_equity=False)
        result = engine.run(candles)
        m = result.metrics
        base.update({
            "total_trades": m.total_trades,
            "wins": m.wins,
            "losses": m.losses,
            "win_rate": round(m.win_rate, 4),
            "total_pnl": round(m.total_pnl, 2),
            "total_gain": round(getattr(m, "total_gain", 0.0), 2),
            "total_loss": round(getattr(m, "total_loss", 0.0), 2),
            "max_drawdown": round(m.max_drawdown, 2),
            "calmar_ratio": round(m.calmar_ratio, 3),
            "profit_factor": round(m.profit_factor, 3),
            "avg_rr_ratio": round(getattr(m, "avg_rr_ratio", 0.0), 3),
            "daily_pnl": m.daily_pnl,
            "avg_win": round(m.avg_win, 2),
            "avg_loss": round(m.avg_loss, 2),
        })
    except Exception as e:
        base["error"] = str(e)
    finally:
        with _ml_progress_lock:
            _ml_progress["current"] += 1
            _cur = _ml_progress["current"]
            _tot = _ml_progress["total"]
        # Console progress: log every 10 combos (and the final one) so the run is
        # visible in the terminal alongside the frontend progress bar.
        if _tot and (_cur % 10 == 0 or _cur == _tot):
            logger.info(f"[Machine Learning] progress {_cur}/{_tot} ({_cur * 100 // _tot}%)")
    return base


def _run_conf_combo(candles, timeline, scorer, tick, base_minutes, timeframes,
                    band_ticks, min_distinct_tf, min_prob, ev_floor, wait_minutes,
                    trail_lock_pct, contract_id, contract_size, bt_cfg_kwargs,
                    rr, enable_breakout, trail_trigger_pct, full_tp_lock,
                    session_limit) -> dict:
    """Run ONE confluence Model+Style combination on the shared (read-only) zone
    timeline. The expensive zone detection is done ONCE upstream; this just
    re-clusters + re-simulates with the swept knobs. Returns a result dict with
    the same metric keys as _run_ml_combo plus explicit conf_* knob fields."""
    import math
    from backend.strategy.confluence import ConfluenceConfig
    from backend.backtest.confluence_backtest import (
        ConfluenceBacktester, ConfluenceBacktestConfig,
    )
    from backend.db.models import BacktestConfig

    bits = ["BRK" if enable_breakout else "noBRK"]
    if trail_trigger_pct > 0:
        bits.append(f"TR{int(round(trail_trigger_pct * 100))}")
        if trail_lock_pct > 0:
            bits.append(f"L{int(round(trail_lock_pct * 100))}")
    if full_tp_lock > 0:
        bits.append(f"FTL{full_tp_lock}")
    bits.append("SES" if session_limit else "noSES")

    base = {
        "method": "conf",
        "tf_combo": list(timeframes),
        "tf_label": " · ".join(bits),
        "overlap_count": len(timeframes),
        "rr_ratio": rr,
        "contract_id": contract_id,
        "contract_size": contract_size,
        "conf_enable_breakout": bool(enable_breakout),
        "conf_trail_trigger_pct": float(trail_trigger_pct),
        "conf_trail_lock_pct": float(trail_lock_pct),
        "conf_full_tp_lock": int(full_tp_lock),
        "conf_session_limit": bool(session_limit),
        "full_tp_lock": int(full_tp_lock),
    }
    try:
        min_score = 0.0
        if min_prob and 0.0 < min_prob < 1.0:
            min_score = math.log(min_prob / (1.0 - min_prob))
        sig_cfg = ConfluenceConfig(band_ticks=band_ticks,
                                   min_distinct_tf=min_distinct_tf, rr=rr)
        sig_cfg.direction_mode = "auto"
        sig_cfg.tick_size = tick
        sig_cfg.ev_floor = ev_floor
        sig_cfg.rr_grid = None
        sig_cfg.enable_breakout = bool(enable_breakout)
        run_cfg = ConfluenceBacktestConfig(
            wait_minutes=wait_minutes, min_score=min_score,
            base_minutes=base_minutes, timeframes=timeframes,
            one_trade_per_session_direction=bool(session_limit),
            trail_trigger_pct=float(trail_trigger_pct),
            trail_lock_pct=float(trail_lock_pct),
            full_tp_lock=int(full_tp_lock),
        )
        bt = ConfluenceBacktester(
            signal_cfg=sig_cfg, run_cfg=run_cfg, contract_id=contract_id,
            contract_size=contract_size, bt_config=BacktestConfig(**bt_cfg_kwargs),
            scorer=scorer,
        )
        result = bt.run(candles, zones_timeline=timeline)
        m = result.metrics
        base.update({
            "total_trades": m.total_trades,
            "wins": m.wins,
            "losses": m.losses,
            "win_rate": round(m.win_rate, 4),
            "total_pnl": round(m.total_pnl, 2),
            "total_gain": round(getattr(m, "total_gain", 0.0), 2),
            "total_loss": round(getattr(m, "total_loss", 0.0), 2),
            "max_drawdown": round(m.max_drawdown, 2),
            "calmar_ratio": round(m.calmar_ratio, 3),
            "profit_factor": round(m.profit_factor, 3),
            "expectancy": round(getattr(m, "expectancy", 0.0), 3),
            "avg_rr_ratio": round(getattr(m, "avg_rr_ratio", 0.0), 3),
            "daily_pnl": m.daily_pnl or {},
            "avg_win": round(m.avg_win, 2),
            "avg_loss": round(m.avg_loss, 2),
        })
    except Exception as e:
        base["error"] = str(e)
    finally:
        with _ml_progress_lock:
            _ml_progress["current"] += 1
            _cur = _ml_progress["current"]
            _tot = _ml_progress["total"]
        if _tot and (_cur % 10 == 0 or _cur == _tot):
            logger.info(f"[Combination] progress {_cur}/{_tot} ({_cur * 100 // _tot}%)")
    return base


@router.post("/backtest/conf-combo-run")
async def conf_combo_run(req: ConfComboRunRequest):
    """COMBINATION: sweep the confluence Model+Style grid (240 runs) and rank.

    Build the zone timeline ONCE (depends only on timeframes/tick), then reuse it
    across every combo. Structural MODEL knobs (band / min-distinct-tf / min-prob
    / ev-floor / timeframes / trail-lock) are HELD at the request values; the grid
    varies RR × breakout × trail-trigger × full-tp-lock × session. Reuses the same
    progress bar as ml-run and writes an AI-readable artifact."""
    global _ml_results_cache

    if not _historical_candles:
        raise HTTPException(400, "No candles loaded — fetch historical data first")

    await _refresh_recent_historical_candles(req.contract_id)

    import asyncio
    import os
    import time as _time
    from concurrent.futures import ThreadPoolExecutor
    from itertools import product
    from backend.db.models import BacktestConfig
    from backend.strategy.consolidation import timeframes_for_base
    from backend.strategy.confluence import MAX_RECENCY_DEPTH
    from backend.strategy.confluence_scorer import resolve_scorer
    from backend.backtest.confluence_backtest import build_zone_timeline

    contract_id = req.contract_id or "CON.F.US.MNQ.M26"
    contract_size = _normalize_contract_size(contract_id, req.contract_size)
    bt_symbol = _extract_symbol(contract_id)
    tick = get_tick_size(contract_id)
    base = max(1, int(req.conf_base_minutes or 1))
    timeframes = timeframes_for_base(base)
    trail_lock_pct = float(getattr(req, "conf_trail_lock_pct", 0.0) or 0.0)

    candles = sorted(_historical_candles, key=lambda c: c.timestamp)
    scorer = resolve_scorer(bool(req.conf_use_scorer), None)

    bt_cfg_kwargs = dict(
        initial_capital=req.initial_capital,
        symbol=bt_symbol,
        commission_rt=get_commission_rt(contract_id),
        fees_rt=get_fees_rt(contract_id),
    )

    loop = asyncio.get_running_loop()

    # ── Phase 1: build the (shared, read-only) zone timeline ONCE ──
    _ml_progress["current"] = 0
    _ml_progress["total"] = 0
    _ml_progress["stage"] = f"building zone timeline ({len(timeframes)} TFs over {len(candles)} candles)…"
    logger.info(f"[Combination] building zone timeline over {len(candles)} candles…")
    _t0 = _time.perf_counter()
    timeline = await loop.run_in_executor(
        None, build_zone_timeline, candles, timeframes, tick, MAX_RECENCY_DEPTH,
    )
    logger.info(f"[Combination] zone timeline built in {_time.perf_counter() - _t0:.1f}s")

    # ── Phase 2: enumerate the 240-combo grid ──
    combos = list(product(
        CONF_COMBO_RR, CONF_COMBO_BREAKOUT, CONF_COMBO_TRAIL_TRIGGER,
        CONF_COMBO_FULL_TP_LOCK, CONF_COMBO_SESSION,
    ))
    _ml_progress["stage"] = "running backtests"
    _ml_progress["total"] = len(combos)

    _cpu = os.cpu_count() or 4
    _n = len(candles)
    if _n > 400_000:
        WORKERS = min(_cpu, 4)
    elif _n > 200_000:
        WORKERS = min(_cpu, 8)
    elif _n > 100_000:
        WORKERS = min(_cpu, 16)
    else:
        WORKERS = min(_cpu, 32)
    logger.info(f"[Combination] {len(combos)} runs over {_n} candles using {WORKERS} workers")

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        tasks = [
            loop.run_in_executor(
                executor, _run_conf_combo,
                candles, timeline, scorer, tick, base, timeframes,
                req.conf_band_ticks, req.conf_min_distinct_tf,
                req.conf_min_prob, req.conf_ev_floor, req.conf_wait_minutes,
                trail_lock_pct, contract_id, contract_size, bt_cfg_kwargs,
                rr, enable_breakout, trail_trigger_pct, full_tp_lock, session_limit,
            )
            for (rr, enable_breakout, trail_trigger_pct, full_tp_lock, session_limit) in combos
        ]
        results = await asyncio.gather(*tasks)

    # Rank by Calmar, tie-break total PnL then lower drawdown (same as ml-run).
    _ml_progress["stage"] = "ranking results"
    ranked_source = sorted(
        results,
        key=lambda r: (
            float(r.get("calmar_ratio", 0) or 0) if not r.get("error") else -999999.0,
            float(r.get("total_pnl", 0) or 0),
            -abs(float(r.get("max_drawdown", 0) or 0)),
        ),
        reverse=True,
    )
    ranked = []
    for rank, r in enumerate(ranked_source, start=1):
        r["rank"] = rank
        r["pass_max_dd"] = r.get("max_drawdown", 9999) < 3000
        r.update(_weekly_stats(r.get("daily_pnl") or {}))
        ranked.append(r)

    _ml_results_cache = ranked
    held = {
        "band_ticks": req.conf_band_ticks,
        "min_distinct_tf": req.conf_min_distinct_tf,
        "min_prob": req.conf_min_prob,
        "ev_floor": req.conf_ev_floor,
        "trail_lock_pct": trail_lock_pct,
        "timeframes": list(timeframes),
        "base_minutes": base,
        "contract": f"{bt_symbol}@{contract_size}",
        "range": f"{req.start_date or '?'} → {req.end_date or '?'}",
    }
    artifact = _save_conf_combo_artifacts(req, ranked, len(combos), held)
    logger.info(f"[Combination] Done. Top: {ranked[0] if ranked else 'none'}")
    logger.info(f"[Combination] AI-readable results saved: {artifact}")
    display_results = _sorted_ml_results(ranked, "calmar", "desc", ML_DISPLAY_LIMIT)
    _ml_progress["stage"] = ""

    return _json_safe({
        "total_combinations": len(combos),
        "results": display_results,
        "shown": len(display_results),
        "held_constant": held,
        "artifact": artifact,
    })


@router.post("/backtest/ml-run")
async def ml_run(req: MLRunRequest):
    """Run all SL/TP/Trail combinations and rank results.
    Optimisations:
      1. Zone timeline pre-computed ONCE — all combos skip expensive zone detection
      2. ProcessPoolExecutor — true CPU parallelism, bypasses GIL
    """
    global _ml_results_cache

    if not _historical_candles:
        raise HTTPException(400, "No candles loaded — fetch historical data first")

    await _refresh_recent_historical_candles(req.contract_id)

    import asyncio
    import os
    from concurrent.futures import ThreadPoolExecutor
    from backend.db.models import BacktestConfig

    value_area_pct = _normalize_value_area_pct(req.value_area_pct)
    contract_id = req.contract_id or "CON.F.US.MNQ.M26"
    contract_size = _normalize_contract_size(contract_id, req.contract_size)
    bt_symbol = _extract_symbol(contract_id)
    full_tp_lock = max(0, min(3, int(req.full_tp_lock or 0)))
    trail_trigger_pct = _normalize_trail_trigger_pct(req.trail_trigger_pct)
    trail_enabled = bool(req.trail_enabled) and trail_trigger_pct > 0
    breakout_confirm_bars = max(1, int(req.breakout_confirm_bars or 7))

    config_base = BacktestConfig(
        initial_capital=req.initial_capital,
        start_date=req.start_date,
        end_date=req.end_date,
        symbol=bt_symbol,
        commission_rt=get_commission_rt(contract_id),
        fees_rt=get_fees_rt(contract_id),
        value_area_pct=value_area_pct,
    )

    # Sort ONCE here — both the zone precompute and each combo engine.run()
    # must see candles in the same chronological order so that _zi indices align.
    candles = sorted(_historical_candles, key=lambda c: c.timestamp)

    loop = asyncio.get_running_loop()

    import time as _time

    # ── Phase 1: pre-compute a zone timeline per timeframe (5 detectors) ──
    # This phase + Phase 2 run BEFORE the sweep and used to be silent, so on
    # large datasets the UI looked frozen at 0/0. Report stage + per-timeframe
    # timing so the console and progress bar show it is preparing, not stuck.
    logger.info(
        f"[Machine Learning] Pre-computing {len(ML_TIMEFRAMES)} per-timeframe "
        f"zone timelines over {len(candles)} candles…"
    )
    _ml_progress["current"] = 0
    _ml_progress["total"] = 0
    tf_timelines = {}
    for _idx, tf in enumerate(ML_TIMEFRAMES, start=1):
        _ml_progress["stage"] = f"preparing zone timelines ({_idx}/{len(ML_TIMEFRAMES)}: {tf})"
        _t0 = _time.perf_counter()
        tf_timelines[tf] = await loop.run_in_executor(
            None, _precompute_zone_timeline, candles, value_area_pct, False, tf,
        )
        logger.info(
            f"[Machine Learning] zone timeline {tf} done ({_idx}/{len(ML_TIMEFRAMES)}) "
            f"in {_time.perf_counter() - _t0:.1f}s"
        )

    # ── Phase 2: build merged timelines for every timeframe combination ──
    #   Method 1 = single timeframe; Method 2 = overlap of 2..5 timeframes.
    tf_combos = _ml_timeframe_combos()
    _ml_progress["stage"] = f"building {len(tf_combos)} timeframe combos…"
    logger.info(f"[Machine Learning] building {len(tf_combos)} merged timeframe timelines…")
    _t0 = _time.perf_counter()
    merged_timelines = {}
    for combo in tf_combos:
        merged_timelines[combo] = await loop.run_in_executor(
            None, _merge_zone_timelines,
            [tf_timelines[tf] for tf in combo], combo,
        )
    logger.info(
        f"[Machine Learning] merged timelines built in {_time.perf_counter() - _t0:.1f}s — "
        f"{len(tf_combos)} timeframe combos × {len(ML_RR_VALUES)} RR "
        f"= {len(tf_combos) * len(ML_RR_VALUES)} runs"
    )

    # ── Phase 3: sweep (timeframe-combo × RR) ──
    combos = [(combo, rr) for combo in tf_combos for rr in ML_RR_VALUES]
    _ml_progress["stage"] = "running backtests"
    _ml_progress["current"] = 0
    _ml_progress["total"] = len(combos)

    # Scale worker count DOWN as the dataset grows. Each parallel combo holds its
    # own trade list / breakout trackers / daily-pnl over the full candle range;
    # on full-range data (hundreds of thousands of 1m bars) running 32 at once is
    # what drove RAM to ~97% and froze the machine. Fewer workers = lower peak RAM.
    _cpu = os.cpu_count() or 4
    _n = len(candles)
    if _n > 400_000:
        WORKERS = min(_cpu, 4)
    elif _n > 200_000:
        WORKERS = min(_cpu, 8)
    elif _n > 100_000:
        WORKERS = min(_cpu, 16)
    else:
        WORKERS = min(_cpu, 32)
    logger.info(
        f"[Machine Learning] {len(combos)} runs over {_n} candles using {WORKERS} workers"
    )
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        tasks = [
            loop.run_in_executor(
                executor, _run_ml_combo,
                candles,
                BacktestConfig(
                    strategies=["trend"],
                    initial_capital=config_base.initial_capital,
                    start_date=config_base.start_date,
                    end_date=config_base.end_date,
                    symbol=bt_symbol,
                    commission_rt=get_commission_rt(contract_id),
                    fees_rt=get_fees_rt(contract_id),
                    value_area_pct=value_area_pct,
                ),
                merged_timelines[combo],
                rr,
                combo,
                contract_id, contract_size, breakout_confirm_bars,
                full_tp_lock, trail_trigger_pct, trail_enabled,
                bool(req.one_trade_per_session_direction),
                bool(getattr(req, "tr_one_trade_per_session", True)),
            )
            for (combo, rr) in combos
        ]
        results = await asyncio.gather(*tasks)

    # Rank by Calmar directly. Tie-break with total PnL, then lower drawdown.
    _ml_progress["stage"] = "ranking results"
    ranked_source = sorted(
        results,
        key=lambda r: (
            float(r.get("calmar_ratio", 0) or 0) if not r.get("error") else -999999.0,
            float(r.get("total_pnl", 0) or 0),
            -abs(float(r.get("max_drawdown", 0) or 0)),
        ),
        reverse=True,
    )

    ranked = []
    for rank, r in enumerate(ranked_source, start=1):
        r["rank"] = rank
        r["pass_max_dd"] = r.get("max_drawdown", 9999) < 3000
        daily = r.get("daily_pnl", {})
        if daily and r.get("total_pnl", 0) > 0:
            pos_days = sum(1 for v in daily.values() if v > 0)
            r["consistency_pct"] = round(pos_days / len(daily), 3)
            max_day = max(daily.values())
            r["max_day_pct"] = round(max_day / r["total_pnl"] * 100, 1) if r["total_pnl"] > 0 else 0
        else:
            r["consistency_pct"] = 0
            r["max_day_pct"] = 0
        # Phase 8: week-to-week variation metrics.
        r.update(_weekly_stats(r.get("daily_pnl") or {}))
        ranked.append(r)

    _ml_results_cache = ranked
    artifact = _save_ml_artifacts(req, ranked, len(combos))
    logger.info(f"[Machine Learning] Done. Top result: {ranked[0] if ranked else 'none'}")
    logger.info(f"[Machine Learning] AI-readable results saved: {artifact}")
    display_results = _sorted_ml_results(
        ranked, "calmar", "desc", ML_DISPLAY_LIMIT
    )
    _ml_progress["stage"] = ""

    return _json_safe({
        "total_combinations": len(combos),
        "results": display_results,
        "shown": len(display_results),
        "artifact": artifact,
    })


@router.get("/backtest/ml-results")
async def get_ml_results(
    sort_col: str = "calmar",
    sort_dir: str = "desc",
    limit: int = ML_DISPLAY_LIMIT,
):
    """Return cached Machine learning results from last run."""
    if _ml_results_cache:
        results = _sorted_ml_results(
            _ml_results_cache, sort_col, sort_dir, limit
        )
        return _json_safe({
            "results": results,
            "total_combinations": len(_ml_results_cache),
            "shown": len(results),
            "sort_col": sort_col,
            "sort_dir": sort_dir,
            "source": "cache",
        })
    payload = _load_ml_artifact(sort_col, sort_dir, limit)
    payload["source"] = "artifact"
    return _json_safe(payload)


@router.get("/backtest/ml-progress")
async def get_ml_progress():
    """Return current Machine learning progress (current / total combos done)."""
    return dict(_ml_progress)


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
    rr_ratio: int = 2                     # reward:risk multiple (1..10)
    # v0.18: "single" = one area timeframe; "overlap" = enter at the AVERAGE
    # overlapping VAH/VAL of the timeframes in tf_combo (mirrors backtest/ML).
    method: str = "single"
    tf_combo: Optional[List[str]] = None
    # Strategy params
    strategy: str = "trend"
    tp_ticks: int = 200
    sl_ticks: int = 50
    trail_sl_ticks: int = 10
    trail_sl_pct: Optional[float] = 0.05
    trail_trigger_pct: float = 0.30
    trail_enabled: bool = True            # v0.11+: master trail switch
    tr_tp_ticks: Optional[int] = None
    tr_sl_ticks: Optional[int] = None
    tr_trail_sl_ticks: Optional[int] = None
    tr_trail_sl_pct: Optional[float] = None
    tr_trail_trigger_pct: Optional[float] = None
    tr_trail_enabled: Optional[bool] = None
    tr_full_tp_lock: Optional[int] = None
    candle_seconds: int = 60
    full_tp_lock: int = 0                 # 0=OFF, 1/2/3 TP exits
    one_trade_per_session_direction: bool = True
    tr_one_trade_per_session: bool = True
    # Zone stability is enabled by default; keep this flag for future experiments.
    skip_zone_stability: bool = False
    breakout_confirm_bars: int = 7
    # v0.19: explainable confluence (ML scorer) live mode. Set strategy="confluence".
    # conf_shadow defaults False — live places real orders (practice account).
    conf_band_ticks: float = 8.0
    conf_min_distinct_tf: int = 3
    conf_rr: float = 1.5
    conf_wait_minutes: int = 60
    conf_base_minutes: int = 1
    conf_min_prob: float = 0.0
    conf_ev_floor: Optional[float] = None
    conf_rr_grid: Optional[List[float]] = None
    conf_use_scorer: bool = True
    conf_enable_breakout: bool = True
    # --- STYLE: optional exit-policy (break-even / trail / lock). All-OFF == original behaviour ---
    conf_trail_trigger_pct: float = 0.0
    conf_trail_lock_pct: float = 0.0
    conf_full_tp_lock: int = 0
    conf_session_limit: bool = True
    conf_shadow: bool = False

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


def _build_exit_index(exits: List[dict]) -> Dict[tuple, str]:
    """Index exits as {(account_id, contract_id, exit_time_str): reason} for
    O(1) lookup during fill pairing. Time matches are loose: we keep the ISO
    string as written by the engine, and pairing tries a few variants."""
    idx: Dict[tuple, str] = {}
    for e in exits or []:
        try:
            acc = e.get("account_id")
            cid = e.get("contract_id") or ""
            etime = (e.get("exit_time") or "").strip()
            reason = e.get("exit_reason") or ""
            if acc is None or not etime or not reason:
                continue
            idx[(acc, cid, etime)] = reason
            # Also key without seconds for fuzzy match
            if "T" in etime and len(etime) >= 16:
                idx.setdefault((acc, cid, etime[:16]), reason)
        except Exception:
            continue
    return idx


def _lookup_exit_reason(idx: Dict[tuple, str], account_id, contract_id, exit_time: str) -> Optional[str]:
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
            # Prefer the engine-recorded reason; fall back to pnl sign only when
            # we have nothing better (e.g. trades that pre-date the exit log).
            reason = _lookup_exit_reason(exit_idx, f.get("account_id"), _cid, f.get("time") or "")
            if not reason:
                reason = "tp" if _gross_pnl >= 0 else "sl"
            elif reason == "tp" and _gross_pnl < 0:
                reason = "sl"
            elif reason == "sl" and _gross_pnl > 0:
                reason = "tp"
            trades.append({
                "trade_id": str(opener["fill_id"]) + "_" + str(f["fill_id"]),
                "direction": opener["direction"],
                "size": _sz,
                "entry_price": opener["price"],
                "exit_price": f["price"],
                "entry_time": opener["time"],
                "exit_time": f["time"],
                "pnl": round(_gross_pnl, 2),  # gross P&L from price movement
                "commission": 1.0,
                "fees": 2.80,
                "exit_reason": reason,
                "account_id": f.get("account_id"),
                "contract_id": _cid,
                "source": "topstep",
            })
        else:
            # Orphan closer — keep as single point so nothing is lost
            reason = _lookup_exit_reason(
                exit_idx, f.get("account_id"), f.get("contract_id") or "", f.get("time") or ""
            ) or ("tp" if (f["pnl"] or 0) >= 0 else "sl")
            trades.append({
                "trade_id": str(f["fill_id"]),
                "direction": f["direction"],
                "size": f.get("size") or 1,
                "entry_price": f["price"],
                "exit_price": f["price"],
                "entry_time": f["time"],
                "exit_time": f["time"],
                "pnl": round(float(f["pnl"] or 0), 2),  # use API pnl; no paired prices
                "commission": 1.0,
                "fees": 2.80,
                "exit_reason": reason,
                "account_id": f.get("account_id"),
                "contract_id": f.get("contract_id"),
                "source": "topstep",
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
        cached = _load_trade_history_cache()
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
        cached = _load_trade_history_cache()
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
        cached = _load_trade_history_cache()
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

_DEFAULT_PRESET_NAME = "TR MNQx3 50/200 TRIG30 TRAILTP5% TPLOCKOFF"
_DEFAULT_PRESET_PARAMS = {
    "strategy": "trend",
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
    "candle_seconds": 60,
    "contract_id": "CON.F.US.MNQ.M26",
    "contract_size": 3,
    "full_tp_lock": 0,
    "one_trade_per_session_direction": True,
    "tr_one_trade_per_session": True,
    "value_area_pct": 0.80,
    "area_timeframe": "5m",
    "skip_zone_stability": False,
}

_BUILTIN_PRESETS = {}
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

    for name, params in list(presets.items()):
        if not isinstance(params, dict):
            continue
        strategy = str(params.get("strategy") or "").lower()
        if strategy != "trend":
            params["strategy"] = "trend"
            changed = True
        if params.get("value_area_pct") != 0.80:
            params["value_area_pct"] = 0.80
            changed = True

    if not presets:
        presets[_DEFAULT_PRESET_NAME] = dict(_DEFAULT_PRESET_PARAMS)
        changed = True

    for key in ("last_used_bt", "last_used_live"):
        if key not in data or data.get(key) not in presets:
            data[key] = next(iter(presets))
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
            "last_used_bt": _DEFAULT_PRESET_NAME,
            "last_used_live": _DEFAULT_PRESET_NAME,
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



