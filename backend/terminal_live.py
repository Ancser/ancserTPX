"""
Terminal-only live runner for ancserTPX.

This path intentionally does not start FastAPI or serve the web UI. It uses the
same broker client, preset file, parameter normalization, and LiveTradingEngine
as the web version, then keeps the process alive in the terminal.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import math
import os
import signal
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from backend.broker.topstepx import TopstepXClient
from backend.db.models import (
    BarUnit, StrategyParams, _extract_symbol,
    current_quarterly_contract_id, normalize_contract_id_to_front,  # 1.0.8: 自動換月
)
from backend.live.account_roles import load_roles  # 1.0.9: main account + 每帳號 preset
from backend.live.engine import LiveTradingEngine
from backend.live.warmup import signal_warmup_progress
from backend.strategy.session_filter import DEFAULT_ALLOWED_SESSIONS, normalize_allowed_sessions


ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
PRESETS_FILE = ROOT / "data" / "presets.json"
MNQ_SIZE_CHOICES = (1, 2, 3, 5, 10)  # 1.0.8: sizing choices
TRAIL_TICK_STEP = 5
# Keep in sync with backend.api.routes.ML_TIMEFRAMES so terminal honours the
# same area-timeframe / overlap selections the web UI saves into presets.
ML_TIMEFRAMES = ("15m", "30m", "1h", "4h")
DEFAULT_PRESET_NAME = "TREND MNQx1 DEFAULT"
DEFAULT_PRESET_PARAMS = {
    "strategy": "factor",   # 1.0.9: TREND 已移除
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
    "pmo_timeframe_minutes": 5,
    "pmo_signal_mode": "normal",
    "pmo_sl_atr": 1.0,
    "pmo_tp_atr": 1.0,
    "pmo_max_hold_bars": 0,   # 1.0.9: HOLD 5m system removed → SL/TP-only
    "pmo_max_trades_per_day": 3,
    "pmo_warmup_bars": 150,
    # 1.0.9: FACTOR 策略預設(與 routes._DEFAULT_PRESET_PARAMS 同步)
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
    "factor_warmup_bars": 150,
    # 1.0.8: 移除 mlc2_* 預設(ml_consolidation_v2 已刪除)
}
# 1.0.9: PRESET_RENAMES/REMOVED/BUILTIN 清理邏輯移除 — presets.json 由 web 端維護

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("ancserTPX.terminal")


# 1.0.9: FACTOR 參數正規化 — 與 backend.api.routes 的同名函式保持同步。
# (不直接 import routes:那會拉起整個 FastAPI 模組與其模組級狀態。)
def _normalize_factor_family(value) -> str:
    v = str(value or "emapmo").strip().lower()
    aliases = {
        "pmo": "emapmo",
        "ema_pmo": "emapmo",
        "mrev": "momentum_reversion",
        "momentum": "momentum_reversion",
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


def _activate_preset_model(preset: Dict[str, Any]) -> None:
    """Retired: confluence model activation is disabled in the terminal path."""
    return


ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _set_sleep_inhibit(enabled: bool) -> None:
    """Keep Windows awake while live terminal is running.

    Uses ES_SYSTEM_REQUIRED only, so the display may turn off normally while
    the computer itself stays awake. No effect on macOS/Linux.
    """
    if os.name != "nt":
        return
    flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED if enabled else ES_CONTINUOUS
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
        logger.info(
            "Windows sleep inhibit %s (screen may still turn off)",
            "enabled" if enabled else "released",
        )
    except Exception as exc:
        logger.warning("Could not update Windows sleep inhibit: %s", exc)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


# 1.0.9: 對齊 web(routes._ensure_builtin_presets)的 preset 載入行為:
#   - schema 版本不同「不再清空」presets(web 早已改為保留使用者存檔)
#   - 不再按名稱刪 preset、不再用 allowed_keys 剝欄位 —— presets.json 由
#     web 端維護,terminal 只讀不寫,完整保留 factor_*/tr_*/新版欄位
#   - 逐 preset 正規化與 web 相同:策略白名單(+factor)、自動換月、VA 吸附、
#     HOLD 強制 0、預設 sessions、area_timeframe/tf_combo/method
def _load_presets_file() -> dict:
    data = None
    try:
        if PRESETS_FILE.exists():
            import json

            with PRESETS_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
    except Exception:
        pass
    if not isinstance(data, dict):
        data = {}
    presets = data.get("presets")
    if not isinstance(presets, dict):
        presets = {}
        data["presets"] = presets
    for name, params in list(presets.items()):
        if not isinstance(params, dict):
            continue
        strategy = str(params.get("strategy") or "").lower()
        params["strategy"] = strategy if strategy in ("fade", "sigma", "pmo", "factor") else "trend"
        params["contract_id"] = normalize_contract_id_to_front(params.get("contract_id") or "")
        params["value_area_pct"] = _normalize_value_area_pct(params.get("value_area_pct"))
        # 1.0.9: HOLD 5m 系統已移除 — 一律 SL/TP-only
        for hold_key in ("factor_max_hold_bars", "pmo_max_hold_bars"):
            if params.get(hold_key) not in (0, None):
                params[hold_key] = 0
        if params["strategy"] in ("trend", "sigma", "pmo", "factor") and "tr_allowed_sessions" not in params:
            params["tr_allowed_sessions"] = list(DEFAULT_ALLOWED_SESSIONS)
        area_tf = str(params.get("area_timeframe") or "15m").lower()
        if area_tf not in ML_TIMEFRAMES and area_tf != "session":
            area_tf = "15m"
        params["area_timeframe"] = area_tf
        params["tf_combo"] = [t for t in (params.get("tf_combo") or []) if t in ML_TIMEFRAMES]
        if params.get("method") == "overlap" and len(params["tf_combo"]) < 2:
            params["method"] = "single"
    if data.get("last_used_bt") != "default" and data.get("last_used_bt") not in presets:
        data["last_used_bt"] = "default"
    if data.get("last_used_live") != "default" and data.get("last_used_live") not in presets:
        data["last_used_live"] = "default"
    return data


def _normalize_contract_size(contract_id: str, requested: Any) -> int:
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


def _normalize_trail_trigger_pct(value: Any) -> float:
    try:
        pct = float(value)
    except (TypeError, ValueError):
        pct = 0.30
    if pct > 1:
        pct = pct / 100.0
    allowed = (0.0, 0.30, 0.50, 0.70)
    return min(allowed, key=lambda x: abs(x - pct))


def _normalize_value_area_pct(value: Any) -> float:
    # 1.0.8: 開放 70%/80% 兩檔 VA;其餘吸附最近檔,無法解析回 80%
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return 0.80
    if pct > 1:
        pct = pct / 100.0
    allowed = (0.70, 0.80)
    return min(allowed, key=lambda x: abs(x - pct))


def _normalize_trail_pct(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return None
    if abs(pct) > 1:
        pct = pct / 100.0
    return max(-0.50, min(0.50, pct))


def _floor_ticks_to_step(ticks: float, step: int = TRAIL_TICK_STEP) -> int:
    try:
        n = abs(float(ticks))
    except (TypeError, ValueError):
        return 0
    if step <= 1:
        return int(n)
    return int(n // step) * step


def _trail_max_profit_ticks(tp_ticks: Any, trigger_pct: Any) -> int:
    try:
        tp = abs(int(tp_ticks or 0))
    except (TypeError, ValueError):
        tp = 0
    trigger = _normalize_trail_trigger_pct(trigger_pct)
    if trigger <= 0:
        return 0
    trigger_ticks = _floor_ticks_to_step(tp * trigger)
    return max(0, trigger_ticks - TRAIL_TICK_STEP)


def _clamp_trail_ticks(trail: Any, sl_ticks: Any, tp_ticks: Any, trigger_pct: Any = None) -> int:
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
    return max(-sl, min(hi, t))


def _trail_ticks_from_pct(trail_pct: Any, sl_ticks: Any, tp_ticks: Any, trigger_pct: Any = None) -> int:
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

    if pct < 0:
        ticks = -_floor_ticks_to_step(sl * abs(pct))
    else:
        ticks = _floor_ticks_to_step(tp * pct)
    return _clamp_trail_ticks(ticks, sl, tp, trigger_pct)


def _resolve_trail_ticks(trail_ticks: Any, trail_pct: Any, sl_ticks: Any, tp_ticks: Any, trigger_pct: Any) -> int:
    pct = _normalize_trail_pct(trail_pct)
    if pct is not None:
        return _trail_ticks_from_pct(pct, sl_ticks, tp_ticks, trigger_pct)
    return _clamp_trail_ticks(trail_ticks, sl_ticks, tp_ticks, trigger_pct)


def _load_default_preset(account_id: Optional[str] = None) -> tuple[str, Dict[str, Any], str]:
    data = _load_presets_file()
    presets = data.get("presets") or {}
    candidates: List[tuple[str, Any]] = []
    # 1.0.9: 優先用 account_roles.json 裡「這個帳號指定的 preset」
    # (web LIVE 面板每帳號一 preset 的設定),其次才是全域 last-used。
    if account_id:
        try:
            acc_cfg = (load_roles().get("accounts") or {}).get(str(account_id)) or {}
            if acc_cfg.get("preset"):
                candidates.append(("account_roles", acc_cfg["preset"]))
        except Exception:
            pass
    candidates += [
        ("last_used_live", data.get("last_used_live")),
        ("last_used_bt", data.get("last_used_bt")),
    ]
    for source, candidate in candidates:
        name = str(candidate or "").strip()
        if name == "default":
            return "default", dict(DEFAULT_PRESET_PARAMS), source
        params = presets.get(name)
        if name and isinstance(params, dict):
            merged = dict(DEFAULT_PRESET_PARAMS)
            merged.update(params)
            return name, merged, source

    name = DEFAULT_PRESET_NAME
    params = dict(DEFAULT_PRESET_PARAMS)
    merged = dict(DEFAULT_PRESET_PARAMS)
    merged.update(params)
    return name, merged, "built_in_default"


def _select_account(accounts: list[dict]) -> Optional[dict]:
    """帳號優先序:.env TOPSTEPX_ACCOUNT_ID > account_roles.json 的 main account
    > 第一個 PRACTICE > 第一個 canTrade。"""
    active = [a for a in accounts if a.get("canTrade", False)]
    if not active:
        return None

    wanted = _env_int("TOPSTEPX_ACCOUNT_ID", 0)
    if wanted:
        for acc in active:
            if int(acc.get("id") or 0) == wanted:
                return acc
        raise RuntimeError(f"TOPSTEPX_ACCOUNT_ID={wanted} was not found in active accounts")

    # 1.0.9: 用 web LIVE 面板設定的固定主帳號(account_roles.json)
    try:
        main_id = str(load_roles().get("main_account_id") or "")
    except Exception:
        main_id = ""
    if main_id:
        for acc in active:
            if str(acc.get("id")) == main_id:
                logger.info("Using main account from account_roles.json: %s (%s)",
                            acc.get("name", ""), main_id)
                return acc
        logger.warning(
            "main_account_id=%s is not tradable; using the fallback account order",
            main_id,
        )

    practice = [a for a in active if "PRAC" in str(a.get("name", "")).upper()]
    return (practice or active)[0]


def _build_strategy_params(preset: Dict[str, Any], contract_id: str) -> StrategyParams:
    def leg(prefix: str) -> Dict[str, Any]:
        tp = int(preset.get(f"{prefix}_tp_ticks") or preset.get("tp_ticks") or DEFAULT_PRESET_PARAMS["tp_ticks"])
        sl = int(preset.get(f"{prefix}_sl_ticks") or preset.get("sl_ticks") or DEFAULT_PRESET_PARAMS["sl_ticks"])
        trigger = _normalize_trail_trigger_pct(
            preset.get(f"{prefix}_trail_trigger_pct", preset.get("trail_trigger_pct", DEFAULT_PRESET_PARAMS["trail_trigger_pct"]))
        )
        trail = _resolve_trail_ticks(
            preset.get(f"{prefix}_trail_sl_ticks", preset.get("trail_sl_ticks")),
            preset.get(f"{prefix}_trail_sl_pct", preset.get("trail_sl_pct")),
            sl,
            tp,
            trigger,
        )
        enabled = bool(preset.get(f"{prefix}_trail_enabled", preset.get("trail_enabled", True))) and trigger > 0
        lock = int(preset.get(f"{prefix}_full_tp_lock", preset.get("full_tp_lock", 0)) or 0)
        return {
            "tp": tp,
            "sl": sl,
            "trigger": trigger,
            "trail": trail,
            "enabled": enabled,
            "lock": max(0, min(3, lock)),
        }

    tr = leg("tr")
    primary = tr
    contract_size = _normalize_contract_size(
        contract_id,
        preset.get("contract_size", DEFAULT_PRESET_PARAMS["contract_size"]),
    )
    # Zone selection (v1.0.6) — keep terminal in sync with the web flow so an
    # OVERLAP or non-15m area-timeframe preset runs the same detector here.
    area_timeframe = str(preset.get("area_timeframe") or "15m").lower()
    # 1.0.8: "session" = 0.15.5 式整個-session 生長 zone(與其他 TF 互斥,強制 single)
    if area_timeframe != "session" and area_timeframe not in ML_TIMEFRAMES:
        area_timeframe = "15m"
    method = str(preset.get("method") or "single").lower()
    if method != "overlap" or area_timeframe == "session":
        method = "single"
    tf_combo = [t for t in (preset.get("tf_combo") or []) if t in ML_TIMEFRAMES]
    if area_timeframe == "session":
        tf_combo = []
    try:
        rr_ratio = int(preset.get("rr_ratio", DEFAULT_PRESET_PARAMS["rr_ratio"]) or 2)
    except (TypeError, ValueError):
        rr_ratio = 2
    rr_ratio = max(1, min(6, rr_ratio))
    try:
        confirm_bars = int(preset.get("breakout_confirm_bars", DEFAULT_PRESET_PARAMS["breakout_confirm_bars"]) or 7)
    except (TypeError, ValueError):
        confirm_bars = 7
    confirm_bars = max(1, min(10, confirm_bars))

    # v1.0.6: confluence (explainable ML) mode — driven by preset["strategy"].
    # 1.0.8: +fade(前日 VA 回歸)
    # 1.0.9: +factor(EMAPMO / MREV / KDJMA)— 修復:之前漏了 factor,
    # FACTOR preset 會被靜默降級成 trend 突破策略跑。
    strategy_mode = str(preset.get("strategy") or "trend").lower()
    if strategy_mode not in ("fade", "sigma", "pmo", "factor"):
        strategy_mode = "trend"

    def _conf_float(key, default):
        try:
            return float(preset.get(key, default))
        except (TypeError, ValueError):
            return default

    def _conf_int(key, default):
        try:
            return int(preset.get(key, default))
        except (TypeError, ValueError):
            return default

    def _conf_optional_float(key):
        value = preset.get(key)
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _conf_optional_int(key):
        value = preset.get(key)
        if value in (None, "", 0, "0"):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    return StrategyParams(
        strategy=strategy_mode,
        tp_ticks=primary["tp"],
        sl_ticks=primary["sl"],
        trail_sl_ticks=primary["trail"],
        trail_trigger_pct=primary["trigger"],
        trail_enabled=primary["enabled"],
        tr_tp_ticks=tr["tp"],
        tr_sl_ticks=tr["sl"],
        tr_trail_sl_ticks=tr["trail"],
        tr_trail_trigger_pct=tr["trigger"],
        tr_trail_enabled=tr["enabled"],
        tr_full_tp_lock=tr["lock"],
        tr_allowed_sessions=list(
            normalize_allowed_sessions(preset.get("tr_allowed_sessions", DEFAULT_ALLOWED_SESSIONS))
            or []
        ) or None,
        candle_seconds=int(preset.get("candle_seconds") or 60),
        contract_id=contract_id,
        contract_size=contract_size,
        # 1.0.8 BUGFIX: 之前漏傳 → StrategyParams 永遠用預設 0.80,
        # 引擎(live/backtest 皆讀 params.value_area_pct)VA70 preset 實際跑 80。
        value_area_pct=_normalize_value_area_pct(
            preset.get("value_area_pct", DEFAULT_PRESET_PARAMS["value_area_pct"])
        ),
        area_timeframe=area_timeframe,
        method=method,
        tf_combo=tf_combo,
        tr_overlap_trade_tf=(
            "smallest" if str(preset.get("tr_overlap_trade_tf") or "").lower() == "smallest"
            else "merged"
        ),
        rr_ratio=rr_ratio,
        breakout_confirm_bars=confirm_bars,
        # 1.0.8: 出場模式 + 日虧斷路器
        tr_exit_mode=(
            "ladder" if str(preset.get("tr_exit_mode") or "tp").lower() == "ladder" else "tp"
        ),
        tr_daily_loss_stop=max(0, min(9, _conf_int("tr_daily_loss_stop", 0))),
        tr_daily_win_stop=max(0, min(9, _conf_int("tr_daily_win_stop", 0))),  # 1.0.9: 日盈休息(之前漏傳)
        # 1.0.9: prevRV regime gate + fade 專用
        tr_prev_rv_gate=max(0, min(60, _conf_int("tr_prev_rv_gate", 0))),
        fade_tp_frac=float(preset.get("fade_tp_frac", 0.75) or 0.75),
        fade_entry_mode=(lambda m: m if m in ("limit", "rejection", "or15") else "limit")(str(preset.get("fade_entry_mode") or "limit").lower()),  # 1.0.9: +or15
        sigma_window_minutes=max(5, _conf_int("sigma_window_minutes", 15)),
        sigma_method=(
            "mad" if str(preset.get("sigma_method") or "").lower() == "mad" else "std"
        ),
        sigma_entry_mode=(
            "reject" if str(preset.get("sigma_entry_mode") or "blind").lower() == "reject" else "blind"
        ),
        sigma_accept_mode=(
            str(preset.get("sigma_accept_mode") or "none").lower()
            if str(preset.get("sigma_accept_mode") or "none").lower() in ("none", "filter", "switch")
            else "none"
        ),
        sigma_start=max(0.5, _conf_float("sigma_start", 1.0)),
        sigma_max=max(1.0, _conf_float("sigma_max", 3.0)),
        sigma_target_mode=(
            str(preset.get("sigma_target_mode") or "half").lower()
            if str(preset.get("sigma_target_mode") or "half").lower() in ("inner1", "half", "center")
            else "half"
        ),
        sigma_stop_span=max(0.25, _conf_float("sigma_stop_span", 1.0)),
        sigma_accept_sigma=max(1.0, _conf_float("sigma_accept_sigma", 2.0)),
        sigma_accept_bars=max(1, _conf_int("sigma_accept_bars", 2)),
        pmo_timeframe_minutes=max(1, _conf_int("pmo_timeframe_minutes", 5)),
        pmo_signal_mode=(
            "early" if str(preset.get("pmo_signal_mode") or "").lower() == "early" else "normal"
        ),
        pmo_sl_atr=max(0.1, _conf_float("pmo_sl_atr", 1.0)),
        pmo_tp_atr=max(0.1, _conf_float("pmo_tp_atr", 1.0)),
        pmo_max_hold_bars=0,   # 1.0.9: HOLD 5m system removed → SL/TP-only exits
        pmo_max_trades_per_day=max(0, _conf_int("pmo_max_trades_per_day", 3)),
        pmo_warmup_bars=max(20, _conf_int("pmo_warmup_bars", 150)),
        # 1.0.9: FACTOR 參數(與 routes 的 StrategyParams 組裝同步)
        factor_timeframe_minutes=max(1, _conf_int("factor_timeframe_minutes", 5)),
        factor_signal_family=_normalize_factor_family(preset.get("factor_signal_family", "emapmo")),
        factor_side_mode=_normalize_factor_side(preset.get("factor_side_mode", "all")),
        factor_pmo_signal_mode=_normalize_factor_pmo_mode(preset.get("factor_pmo_signal_mode", "normal")),
        factor_session_va_filter=_normalize_factor_session_va_filter(preset.get("factor_session_va_filter", "off")),
        factor_sl_rule=_normalize_factor_rule(preset.get("factor_sl_rule", "atr")),
        factor_tp_rule=_normalize_factor_rule(preset.get("factor_tp_rule", "atr")),
        factor_sl_value=max(0.01, _conf_float("factor_sl_value", 1.5)),
        factor_tp_value=max(0.01, _conf_float("factor_tp_value", 2.0)),
        factor_max_hold_bars=0,  # 1.0.9: HOLD 5m system removed → SL/TP-only exits
        factor_max_trades_per_day=max(0, _conf_int("factor_max_trades_per_day", 3)),
        factor_warmup_bars=max(20, _conf_int("factor_warmup_bars", 150)),
        full_tp_lock=primary["lock"],
        one_trade_per_session_direction=bool(preset.get("one_trade_per_session_direction", True)),
        tr_one_trade_per_session=bool(preset.get("tr_one_trade_per_session", True)),
        skip_zone_stability=False,
        # v1.0.6 confluence config (used only when strategy == "confluence")
        conf_band_ticks=_conf_float("conf_band_ticks", 4.0),
        conf_min_distinct_tf=_conf_int("conf_min_distinct_tf", 2),
        conf_rr=float(round(max(1.0, min(6.0, _conf_float("conf_rr", 1.0))), 2)),
        conf_wait_minutes=_conf_int("conf_wait_minutes", 1),
        conf_base_minutes=_conf_int("conf_base_minutes", 1),
        conf_min_prob=_conf_float("conf_min_prob", 0.65),
        conf_ev_floor=_conf_optional_float("conf_ev_floor"),
        conf_rr_grid=None,
        conf_use_scorer=bool(preset.get("conf_use_scorer", True)),
        conf_enable_breakout=bool(preset.get("conf_enable_breakout", False)),
        conf_max_risk_ticks=_conf_optional_int("conf_max_risk_ticks"),
        conf_sl_reference_tf=(
            "smallest" if str(preset.get("conf_sl_reference_tf") or "").lower() == "smallest"
            else "largest"
        ),
        conf_allowed_sessions=list(
            normalize_allowed_sessions(preset.get("conf_allowed_sessions", DEFAULT_ALLOWED_SESSIONS))
            or []
        ) or None,
        conf_trail_trigger_pct=_conf_float("conf_trail_trigger_pct", 0.50),
        conf_trail_lock_pct=_conf_float("conf_trail_lock_pct", 0.05),
        conf_full_tp_lock=_conf_int("conf_full_tp_lock", 0),
        conf_session_limit=bool(preset.get("conf_session_limit", True)),
        conf_shadow=bool(preset.get("conf_shadow", False)),
    )


async def _fetch_warmup_candles(
    client: TopstepXClient,
    contract_id: str,
    params: Optional[StrategyParams] = None,
):
    now = datetime.utcnow()
    end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    best: List = []
    best_completed = -1
    for days in (2, 7, 14):
        start = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        logger.info("Fetching warmup 1m candles (%sd): %s ~ %s", days, start, end)
        candles = await client.get_historical_bars_paginated(
            contract_id=contract_id,
            unit=BarUnit.MINUTE,
            unit_number=1,
            start_time=start,
            end_time=end,
        )
        if candles:
            candles = sorted(candles, key=lambda c: c.timestamp)
            completed, required = signal_warmup_progress(candles, params) if params else (0, 0)
            if not best or completed > best_completed:
                best = candles
                best_completed = completed
            logger.info(
                "Warmup candles loaded: %s bars | %s ~ %s",
                len(candles),
                candles[0].timestamp.isoformat(),
                candles[-1].timestamp.isoformat(),
            )
            if required == 0 or completed >= required:
                if required:
                    logger.info(
                        "Signal warmup ready: %s/%s completed bars",
                        completed,
                        required,
                    )
                return candles
            logger.warning(
                "Signal warmup insufficient: %s/%s completed bars in %sd; expanding range",
                completed,
                required,
                days,
            )
            continue
        logger.warning("No warmup candles in the last %s day(s); retrying wider range", days)
    if best and params:
        completed, required = signal_warmup_progress(best, params)
        logger.warning(
            "Signal warmup still incomplete after 14d: %s/%s completed bars; "
            "engine will stay gated until ready",
            completed,
            required,
        )
    return best


async def run_terminal_live() -> int:
    username = os.getenv("TOPSTEPX_USERNAME", "").strip()
    api_key = os.getenv("TOPSTEPX_API_KEY", "").strip()
    use_demo = _env_bool("TOPSTEPX_USE_DEMO", False)

    if not username or not api_key:
        logger.error("Missing TOPSTEPX_USERNAME or TOPSTEPX_API_KEY in .env")
        return 2

    client = TopstepXClient(username=username, api_key=api_key, use_demo=use_demo)
    engine: Optional[LiveTradingEngine] = None
    stop_event = asyncio.Event()

    def _stop(*_: object) -> None:
        stop_event.set()

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                asyncio.get_running_loop().add_signal_handler(sig, _stop)
            except (NotImplementedError, RuntimeError):
                signal.signal(sig, lambda *_args: _stop())

    try:
        logger.info("ancserTPX terminal starting")
        logger.info("API: %s | user=%s", "demo" if use_demo else "production", username)

        await client.authenticate()
        accounts = await client.get_accounts()
        account = _select_account(accounts)
        if not account:
            raise RuntimeError("No active tradable account found")

        # 1.0.9: 先選帳號,preset 才能按「每帳號指定」優先(account_roles.json),
        # 其次 last_used_live → last_used_bt → default。
        preset_name, preset, preset_source = _load_default_preset(str(account["id"]))
        logger.info("Preset: %s (%s)", preset_name, preset_source)
        _activate_preset_model(preset)

        contract_id = (
            str(preset.get("contract_id") or "").strip()
            or os.getenv("TOPSTEPX_CONTRACT_ID", "").strip()
        )
        # Auto front-month rollover: resolve to the current tradable contract so an
        # expired month never gets orders rejected (code=9 ContractNotActive).
        try:
            resolved = await client.get_front_month_contract_id(contract_id or "MNQ")
            if resolved:
                if resolved != contract_id:
                    logger.info("Auto front-month: %s -> %s", contract_id or "(auto)", resolved)
                contract_id = resolved
        except Exception as exc:
            logger.warning("Front-month resolve failed: %s", exc)
            if not contract_id:
                contract_id = await client.get_nq_contract_id()

        params = _build_strategy_params(preset, contract_id)
        candles = await _fetch_warmup_candles(client, contract_id, params)
        if not candles:
            raise RuntimeError("No warmup candles returned from TopstepX")

        account_name = account.get("name", "")
        account_id = int(account["id"])
        value_area_pct = _normalize_value_area_pct(
            preset.get("value_area_pct", DEFAULT_PRESET_PARAMS["value_area_pct"])
        )
        logger.info(
            "Account: %s (%s) | contract=%s | size=%s",
            account_name,
            account_id,
            contract_id,
            params.contract_size,
        )
        zone_desc = (
            "overlap[" + "+".join(params.tf_combo) + "]"
            if params.method == "overlap" and len(params.tf_combo) >= 2
            else "single " + params.area_timeframe
        )
        logger.info(
            "Params: MODE=%s ZONE=%s VA=%s SL=%s TP=%s trail=%s trigger=%s%% tp_lock=%s",
            params.strategy,
            zone_desc,
            int(value_area_pct * 100),
            params.sl_ticks,
            params.tp_ticks,
            params.trail_sl_ticks,
            int(params.trail_trigger_pct * 100),
            params.full_tp_lock,
        )

        engine = LiveTradingEngine(
            client=client,
            account_id=account_id,
            contract_id=contract_id,
            contract_size=params.contract_size,
            value_area_pct=value_area_pct,
            strategy_params=params,
        )
        await engine.start(candles)
        _set_sleep_inhibit(True)
        logger.info("LIVE started. Press Ctrl+C to stop.")

        seen_logs: set[str] = set()
        last_status = 0.0
        while engine.is_running and not stop_event.is_set():
            status = engine.get_status()
            for item in status.get("log", []):
                if item not in seen_logs:
                    seen_logs.add(item)
                    print(item, flush=True)

            now_ts = asyncio.get_running_loop().time()
            if now_ts - last_status >= 30:
                last_status = now_ts
                pos = status.get("position")
                pending = status.get("pending_order_id")
                daily = float(status.get("daily_pnl") or 0)
                phase = status.get("phase") or "--"
                order = "POSITION OPEN" if pos else ("ORDER PENDING" if pending else "NONE")
                separator = "\n" if "\n" in phase else " | "
                logger.info(
                    "[LIVE] %s%sORDER: %s | daily_pnl=$%.0f",
                    phase,
                    separator,
                    order,
                    daily,
                )
            await asyncio.sleep(1)

        logger.info("Stopping terminal live engine...")
        return 0
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt")
        return 0
    except Exception as exc:
        logger.error("Terminal live failed: %s", exc, exc_info=True)
        return 1
    finally:
        _set_sleep_inhibit(False)
        if engine and engine.is_running:
            try:
                await engine.stop()
            except Exception as exc:
                logger.error("Engine stop failed: %s", exc)
        try:
            await client.disconnect()
        except Exception:
            pass
        logger.info("ancserTPX terminal stopped")


def main() -> None:
    try:
        code = asyncio.run(run_terminal_live())
    except KeyboardInterrupt:
        code = 0
    sys.exit(code)


if __name__ == "__main__":
    main()
