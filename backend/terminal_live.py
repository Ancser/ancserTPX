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
from typing import Any, Dict, Optional

from dotenv import load_dotenv

from backend.broker.topstepx import TopstepXClient
from backend.db.models import BarUnit, StrategyParams, _extract_symbol
from backend.live.engine import LiveTradingEngine
from backend.strategy.session_filter import DEFAULT_ALLOWED_SESSIONS, normalize_allowed_sessions


ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
PRESETS_FILE = ROOT / "data" / "presets.json"
MNQ_SIZE_CHOICES = (1, 3, 5, 10)
TRAIL_TICK_STEP = 5
# Keep in sync with backend.api.routes.ML_TIMEFRAMES so terminal honours the
# same area-timeframe / overlap selections the web UI saves into presets.
ML_TIMEFRAMES = ("5m", "15m", "30m", "1h", "4h")
PRESET_SCHEMA_VERSION = "2026-06-25-ml-consolidation-v2"
DEFAULT_PRESET_NAME = "ML CONFLUENCE MNQx3 DEFAULT"
DEFAULT_PRESET_PARAMS = {
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
    "conf_allowed_sessions": ["ASIA"],
    "conf_trail_trigger_pct": 0.50,
    "conf_trail_lock_pct": 0.05,
    "conf_full_tp_lock": 0,
    "conf_session_limit": True,
    "conf_shadow": False,
    "mlc2_lookback": 30,
    "mlc2_band_ticks": 2.0,
    "mlc2_sl_buffer_ticks": 4.0,
    "mlc2_tp_mode": "rr",
    "mlc2_rr": 4.0,
    "mlc2_trail_trigger_pct": 0.0,
    "mlc2_trail_lock_pct": 0.0,
    "mlc2_session_limit": False,
    "mlc2_min_score": 0.0,
    "mlc2_allowed_sessions": ["ASIA", "EURO"],
    "mlc2_shadow": False,
}
CODEX_620_MODEL = "20260618_codex_rr3-band4-mintf2-production-baseline-02"
CODEX_624_PRESET_1 = "06.24 CODEX #1 穩健測試 MNQx1 RR1:3 POFF R80 W1m Trail50L5 SesON ASIA B4 TF2"
CODEX_624_PRESET_2 = "06.24 CODEX #2 收益較高 MNQx1 RR1:2.75 POFF R80 W1m Trail50L5 SesON ASIA B4 TF2"
CODEX_624_PRESET_3 = "06.24 CODEX #3 卡瑪最佳 MNQx1 RR1:1.75 POFF R70 W1m Trail50L5 SesON ASIA B4 TF2"
CODEX_624_PRESET_4 = "06.24 CODEX #4 PNL最高 MNQx1 RR1:1.5 POFF R50 W1m Trail50L5 SesON ASIA B4 TF2"
CODEX_624_PRESET_5 = "06.24 CODEX #5 回撤最低 MNQx1 RR1:2.5 P0.65 R90 W1m Trail50L5 SesON ASIA B4 TF2"
MLC2_PRESET = "06.25 CODEX #1 均值回歸 MNQx1 MLC2 LB30 Band2 SLB4 RR1:4 ASIA+EURO TrailOFF"
DEFAULT_LAST_USED_PRESET = CODEX_624_PRESET_1
PRESET_RENAMES = {}
REMOVED_PRESET_NAMES = {
    DEFAULT_PRESET_NAME,
    "ML CONFLUENCE MNQx3 DEFAULT",
    "6/20 CODEX #1 baseline02 RR1:5 P0.60 R80 W1m TrailOFF SesON ASIA B4 TF2 MNQx3",
    "6/20 CODEX #2 baseline02 RR1:5 POFF R80 W1m Trail50L5 SesON ASIA B4 TF2 MNQx3",
    "6/23 CODEX #3 SAFE baseline02 RR1:5 POFF R80 W1m Trail50L5 SesON ASIA B4 TF2 MNQx1",
    "6/20 CLAUDE #1 ML RR1:3 P0.55 W1m TrailOFF SesON B4 TF2 MNQx3",
    "6/20 CLAUDE #1 SVD RR1:2 P0.55 W1m TrailOFF SesON B4 TF2 MNQx3",
    "6/22 CLAUDE #1 ML RR1:3 P0.55 ROFF W1m TrailOFF SesON B4 TF2 MNQx1",
}


def _codex_624_preset(
    *,
    rr: float,
    max_risk_ticks: int,
    min_prob: float = 0.0,
    trail_trigger: float = 0.50,
    contract_id: str = "CON.F.US.MNQ.U26",
    contract_size: int = 1,
) -> Dict[str, Any]:
    params = dict(DEFAULT_PRESET_PARAMS)
    params.update({
        "tp_ticks": int(round(50 * float(rr))),
        "tr_tp_ticks": int(round(50 * float(rr))),
        "contract_id": contract_id,
        "contract_size": contract_size,
        "rr_ratio": float(rr),
        "conf_model_name": CODEX_620_MODEL,
        "conf_rr": float(rr),
        "conf_min_prob": float(min_prob),
        "conf_max_risk_ticks": int(max_risk_ticks),
        "conf_band_ticks": 4.0,
        "conf_min_distinct_tf": 2,
        "conf_allowed_sessions": ["ASIA"],
        "conf_trail_trigger_pct": trail_trigger,
        "conf_trail_lock_pct": 0.05,
        "conf_session_limit": True,
    })
    return params


BUILTIN_PRESETS = {
    CODEX_624_PRESET_1: _codex_624_preset(rr=3.0, max_risk_ticks=80),
    CODEX_624_PRESET_2: _codex_624_preset(rr=2.75, max_risk_ticks=80),
    CODEX_624_PRESET_3: _codex_624_preset(rr=1.75, max_risk_ticks=70),
    CODEX_624_PRESET_4: _codex_624_preset(rr=1.50, max_risk_ticks=50),
    CODEX_624_PRESET_5: _codex_624_preset(rr=2.50, max_risk_ticks=90, min_prob=0.65),
    MLC2_PRESET: {
        **dict(DEFAULT_PRESET_PARAMS),
        "strategy": "ml_consolidation_v2",
        "contract_id": "CON.F.US.MNQ.U26",
        "contract_size": 1,
        "mlc2_lookback": 30,
        "mlc2_band_ticks": 2.0,
        "mlc2_sl_buffer_ticks": 4.0,
        "mlc2_tp_mode": "rr",
        "mlc2_rr": 4.0,
        "mlc2_trail_trigger_pct": 0.0,
        "mlc2_trail_lock_pct": 0.0,
        "mlc2_session_limit": False,
        "mlc2_min_score": 0.0,
        "mlc2_allowed_sessions": ["ASIA", "EURO"],
        "mlc2_shadow": False,
    },
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("ancserTPX.terminal")


def _activate_preset_model(preset: Dict[str, Any]) -> None:
    """Keep terminal/mac direct launch in sync with UI presets."""
    if str(preset.get("strategy") or "").lower() != "confluence":
        return
    name = str(preset.get("conf_model_name") or "").strip()
    if not name:
        return
    try:
        from backend.strategy.confluence_scorer import activate_model_version

        activate_model_version(name)
        logger.info("Preset model active: %s", name)
    except Exception as exc:
        logger.warning("Preset model activate failed (%s): %s", name, exc)


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
    if data.get("preset_schema") != PRESET_SCHEMA_VERSION:
        presets.clear()
        data["preset_schema"] = PRESET_SCHEMA_VERSION
        data["last_used_bt"] = DEFAULT_LAST_USED_PRESET
        data["last_used_live"] = DEFAULT_LAST_USED_PRESET
    for name, params in list(presets.items()):
        if not isinstance(params, dict):
            continue
        strategy = str(params.get("strategy") or "").lower()
        params["strategy"] = strategy if strategy in {"confluence", "ml_consolidation_v2"} else "trend"
        allowed_keys = {
            "strategy", "tp_ticks", "sl_ticks", "trail_sl_ticks", "trail_sl_pct",
            "trail_trigger_pct", "trail_enabled", "candle_seconds", "contract_id",
            "contract_size", "full_tp_lock", "one_trade_per_session_direction",
            "value_area_pct", "area_timeframe", "method", "tf_combo",
            "rr_ratio", "breakout_confirm_bars", "skip_zone_stability",
            "tr_tp_ticks", "tr_sl_ticks", "tr_trail_sl_ticks", "tr_trail_sl_pct",
            "tr_trail_trigger_pct", "tr_trail_enabled", "tr_full_tp_lock",
            "tr_one_trade_per_session",
            "conf_band_ticks", "conf_min_distinct_tf", "conf_rr", "conf_model_name",
            "conf_wait_minutes", "conf_base_minutes", "conf_min_prob",
            "conf_ev_floor", "conf_rr_grid", "conf_use_scorer",
            "conf_enable_breakout", "conf_max_risk_ticks", "conf_trail_trigger_pct",
            "conf_trail_lock_pct", "conf_full_tp_lock",
            "conf_session_limit", "conf_allowed_sessions", "conf_shadow",
            "mlc2_lookback", "mlc2_band_ticks", "mlc2_sl_buffer_ticks",
            "mlc2_tp_mode", "mlc2_rr", "mlc2_trail_trigger_pct",
            "mlc2_trail_lock_pct", "mlc2_session_limit", "mlc2_min_score",
            "mlc2_allowed_sessions", "mlc2_shadow",
        }
        for key in list(params.keys()):
            if key not in allowed_keys:
                params.pop(key, None)
        params["value_area_pct"] = 0.80
        if params["strategy"] == "confluence" and "conf_allowed_sessions" not in params:
            params["conf_allowed_sessions"] = list(DEFAULT_ALLOWED_SESSIONS)
        new_name = None
        if str(name).startswith("BR "):
            new_name = "TR " + str(name)[3:]
        if new_name and new_name != name:
            if new_name not in presets:
                presets[new_name] = params
            del presets[name]
    for old_name, new_name in PRESET_RENAMES.items():
        if old_name in presets:
            presets[new_name] = presets.pop(old_name)
            for key in ("last_used_bt", "last_used_live"):
                if data.get(key) == old_name:
                    data[key] = new_name
    for name in REMOVED_PRESET_NAMES:
        presets.pop(name, None)
    for name, params in BUILTIN_PRESETS.items():
        if presets.get(name) != params:
            presets[name] = dict(params)
    if not presets:
        presets[DEFAULT_LAST_USED_PRESET] = dict(BUILTIN_PRESETS[DEFAULT_LAST_USED_PRESET])
    if data.get("last_used_bt") != "default" and data.get("last_used_bt") not in presets:
        data["last_used_bt"] = DEFAULT_LAST_USED_PRESET if DEFAULT_LAST_USED_PRESET in presets else next(iter(presets))
    if data.get("last_used_live") != "default" and data.get("last_used_live") not in presets:
        data["last_used_live"] = DEFAULT_LAST_USED_PRESET if DEFAULT_LAST_USED_PRESET in presets else next(iter(presets))
    data["fixed_presets"] = []
    return data


def _normalize_contract_size(contract_id: str, requested: Any) -> int:
    sym = _extract_symbol(contract_id)
    if sym in ("NQ", "ENQ"):
        return 1
    try:
        size = int(requested or 1)
    except (TypeError, ValueError):
        size = 3
    return size if size in MNQ_SIZE_CHOICES else 3


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
    return 0.80


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


def _load_default_preset() -> tuple[str, Dict[str, Any], str]:
    data = _load_presets_file()
    presets = data.get("presets") or {}
    candidates = [
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
    active = [a for a in accounts if a.get("canTrade", False)]
    if not active:
        return None

    wanted = _env_int("TOPSTEPX_ACCOUNT_ID", 0)
    if wanted:
        for acc in active:
            if int(acc.get("id") or 0) == wanted:
                return acc
        raise RuntimeError(f"TOPSTEPX_ACCOUNT_ID={wanted} was not found in active accounts")

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
    # OVERLAP or non-5m area-timeframe preset runs the same detector here.
    area_timeframe = str(preset.get("area_timeframe") or "5m").lower()
    if area_timeframe not in ML_TIMEFRAMES:
        area_timeframe = "5m"
    method = str(preset.get("method") or "single").lower()
    if method != "overlap":
        method = "single"
    tf_combo = [t for t in (preset.get("tf_combo") or []) if t in ML_TIMEFRAMES]
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
    strategy_mode = str(preset.get("strategy") or "confluence").lower()
    if strategy_mode != "confluence":
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
        candle_seconds=int(preset.get("candle_seconds") or 60),
        contract_id=contract_id,
        contract_size=contract_size,
        area_timeframe=area_timeframe,
        method=method,
        tf_combo=tf_combo,
        rr_ratio=rr_ratio,
        breakout_confirm_bars=confirm_bars,
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


async def _fetch_warmup_candles(client: TopstepXClient, contract_id: str):
    now = datetime.utcnow()
    end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
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
            logger.info(
                "Warmup candles loaded: %s bars | %s ~ %s",
                len(candles),
                candles[0].timestamp.isoformat(),
                candles[-1].timestamp.isoformat(),
            )
            return candles
        logger.warning("No warmup candles in the last %s day(s); retrying wider range", days)
    return []


async def run_terminal_live() -> int:
    username = os.getenv("TOPSTEPX_USERNAME", "").strip()
    api_key = os.getenv("TOPSTEPX_API_KEY", "").strip()
    use_demo = _env_bool("TOPSTEPX_USE_DEMO", False)

    if not username or not api_key:
        logger.error("Missing TOPSTEPX_USERNAME or TOPSTEPX_API_KEY in .env")
        return 2

    preset_name, preset, preset_source = _load_default_preset()

    # v1.0.6: one-line console confluence switch (no preset editing needed).
    #   TOPSTEPX_CONFLUENCE=1            -> run the explainable ML confluence mode
    #   TOPSTEPX_CONFLUENCE_SHADOW=1     -> log signals only (default: LIVE places orders)
    #   TOPSTEPX_CONF_MIN_PROB=0.55      -> skip signals below this win-probability
    # Base candle resolution is standardized at 1m (matches stored data + backtest).
    if _env_bool("TOPSTEPX_CONFLUENCE", False):
        preset = dict(preset)
        preset["strategy"] = "confluence"
        preset["conf_shadow"] = _env_bool("TOPSTEPX_CONFLUENCE_SHADOW", False)
        preset["conf_base_minutes"] = 1
        try:
            preset["conf_min_prob"] = float(os.getenv("TOPSTEPX_CONF_MIN_PROB", "0.65") or 0.65)
        except ValueError:
            preset["conf_min_prob"] = 0.65
        preset_source = f"{preset_source}+confluence_env"

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
        logger.info("Preset: %s (%s)", preset_name, preset_source)
        _activate_preset_model(preset)

        await client.authenticate()
        accounts = await client.get_accounts()
        account = _select_account(accounts)
        if not account:
            raise RuntimeError("No active tradable account found")

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
        candles = await _fetch_warmup_candles(client, contract_id)
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
                order = "持倉中" if pos else ("掛單中" if pending else "無")
                logger.info("[LIVE] %s | 訂單: %s | daily_pnl=$%.0f", phase, order, daily)
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
