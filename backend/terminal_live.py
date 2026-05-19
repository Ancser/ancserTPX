"""
Terminal-only live runner for ancserTPX.

This path intentionally does not start FastAPI or serve the web UI. It uses the
same broker client, preset file, parameter normalization, and LiveTradingEngine
as the web version, then keeps the process alive in the terminal.
"""

from __future__ import annotations

import asyncio
import logging
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


ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
PRESETS_FILE = ROOT / "data" / "presets.json"
MNQ_SIZE_CHOICES = (1, 3, 5, 10)
TRAIL_TICK_STEP = 5
DEFAULT_PRESET_NAME = "TR 50SL 150TP +5% TRAIL SL"
DEFAULT_PRESET_PARAMS = {
    "strategy": "trend",
    "tp_ticks": 150,
    "sl_ticks": 50,
    "trail_sl_ticks": 5,
    "trail_sl_pct": 0.05,
    "trail_trigger_pct": 0.30,
    "trail_enabled": True,
    "candle_seconds": 60,
    "contract_id": "CON.F.US.MNQ.M26",
    "contract_size": 3,
    "max_profit_lock": 0,
    "skip_zone_stability": False,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("ancserTPX.terminal")


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
    try:
        if PRESETS_FILE.exists():
            import json

            with PRESETS_FILE.open("r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {
        "presets": {DEFAULT_PRESET_NAME: dict(DEFAULT_PRESET_PARAMS)},
        "last_used_bt": DEFAULT_PRESET_NAME,
        "last_used_live": DEFAULT_PRESET_NAME,
    }


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
    allowed = (0.0, 0.10, 0.30, 0.50, 0.70)
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


def _load_default_preset() -> tuple[str, Dict[str, Any]]:
    data = _load_presets_file()
    presets = data.get("presets") or {}
    name = data.get("last_used_live") or data.get("last_used_bt") or DEFAULT_PRESET_NAME
    params = presets.get(name)
    if not isinstance(params, dict):
        name = DEFAULT_PRESET_NAME
        params = dict(DEFAULT_PRESET_PARAMS)
    merged = dict(DEFAULT_PRESET_PARAMS)
    merged.update(params)
    return name, merged


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
    tp_ticks = int(preset.get("tp_ticks") or DEFAULT_PRESET_PARAMS["tp_ticks"])
    sl_ticks = int(preset.get("sl_ticks") or DEFAULT_PRESET_PARAMS["sl_ticks"])
    trigger_pct = _normalize_trail_trigger_pct(
        preset.get("trail_trigger_pct", DEFAULT_PRESET_PARAMS["trail_trigger_pct"])
    )
    trail_ticks = _resolve_trail_ticks(
        preset.get("trail_sl_ticks"),
        preset.get("trail_sl_pct"),
        sl_ticks,
        tp_ticks,
        trigger_pct,
    )
    contract_size = _normalize_contract_size(
        contract_id,
        preset.get("contract_size", DEFAULT_PRESET_PARAMS["contract_size"]),
    )
    return StrategyParams(
        strategy=str(preset.get("strategy") or "trend"),
        tp_ticks=tp_ticks,
        sl_ticks=sl_ticks,
        trail_sl_ticks=trail_ticks,
        trail_trigger_pct=trigger_pct,
        trail_enabled=bool(preset.get("trail_enabled", True)) and trigger_pct > 0,
        candle_seconds=int(preset.get("candle_seconds") or 60),
        contract_id=contract_id,
        contract_size=contract_size,
        max_profit_lock=int(preset.get("max_profit_lock") or 0),
        skip_zone_stability=False,
    )


async def _fetch_warmup_candles(client: TopstepXClient, contract_id: str):
    now = datetime.utcnow()
    start = (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    logger.info("Fetching warmup 1m candles: %s ~ %s", start, end)
    candles = await client.get_historical_bars_paginated(
        contract_id=contract_id,
        unit=BarUnit.MINUTE,
        unit_number=1,
        start_time=start,
        end_time=end,
    )
    return sorted(candles, key=lambda c: c.timestamp)


async def run_terminal_live() -> int:
    username = os.getenv("TOPSTEPX_USERNAME", "").strip()
    api_key = os.getenv("TOPSTEPX_API_KEY", "").strip()
    use_demo = _env_bool("TOPSTEPX_USE_DEMO", False)

    if not username or not api_key:
        logger.error("Missing TOPSTEPX_USERNAME or TOPSTEPX_API_KEY in .env")
        return 2

    preset_name, preset = _load_default_preset()
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
        logger.info("Preset: %s", preset_name)

        await client.authenticate()
        accounts = await client.get_accounts()
        account = _select_account(accounts)
        if not account:
            raise RuntimeError("No active tradable account found")

        contract_id = (
            str(preset.get("contract_id") or "").strip()
            or os.getenv("TOPSTEPX_CONTRACT_ID", "").strip()
        )
        if not contract_id:
            contract_id = await client.get_nq_contract_id()

        params = _build_strategy_params(preset, contract_id)
        candles = await _fetch_warmup_candles(client, contract_id)
        if not candles:
            raise RuntimeError("No warmup candles returned from TopstepX")

        account_name = account.get("name", "")
        account_id = int(account["id"])
        logger.info(
            "Account: %s (%s) | contract=%s | size=%s",
            account_name,
            account_id,
            contract_id,
            params.contract_size,
        )
        logger.info(
            "Params: SL=%s TP=%s trail=%s trigger=%s%% lock=%s",
            params.sl_ticks,
            params.tp_ticks,
            params.trail_sl_ticks,
            int(params.trail_trigger_pct * 100),
            params.max_profit_lock,
        )

        engine = LiveTradingEngine(
            client=client,
            account_id=account_id,
            contract_id=contract_id,
            contract_size=params.contract_size,
            value_area_pct=0.80,
            strategy_params=params,
        )
        await engine.start(candles)
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
                state = "POSITION" if pos else ("PENDING" if pending else "FLAT")
                logger.info("Status: %s | daily_pnl=$%.0f | %s", state, daily, phase)
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
