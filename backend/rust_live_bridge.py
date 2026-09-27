"""Small JSONL boundary between the Rust runtime and existing Python kernels.

Run from the repository root with ``python -m backend.rust_live_bridge``.
The default transport uses stdin/stdout for sanitized JSON only; diagnostics go
to stderr. Live broker operations require an explicit fully armed start request.
"""

from __future__ import annotations

import asyncio
from dataclasses import fields
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import signal
import sys
import threading
import time
from typing import Any, Callable, Optional

from dotenv import load_dotenv

from backend.db.models import BarUnit, Direction, OrderRequest, StrategyParams
from backend.live.engine_lease import LiveEngineLease
from backend.strategy.exit_policy import (
    ExitState,
    evaluate_exit_operation,
    resolve_exit_policy,
)

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)
logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
logging.getLogger("signalrcore").setLevel(logging.CRITICAL)
logging.getLogger("backend.broker.topstepx").setLevel(logging.WARNING)


def _defer_interrupt_to_runtime(_signum: int, _frame: Any) -> None:
    logging.getLogger(__name__).warning(
        "Interrupt received; waiting for the Rust coordinator to send stop."
    )


signal.signal(signal.SIGINT, _defer_interrupt_to_runtime)

PROTOCOL_VERSION = 1
_OUTPUT_LOCK = threading.Lock()
_MARKET_TIME_LOCK = threading.Lock()
_LOOP = asyncio.new_event_loop()
_CLIENT = None
_LEASE: Optional[LiveEngineLease] = None
_SESSION: Optional[dict[str, Any]] = None
_LAST_MARKET_TIMESTAMPS: dict[str, datetime] = {}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _emit(message: dict[str, Any]) -> None:
    with _OUTPUT_LOCK:
        sys.stdout.write(json.dumps(message, separators=(",", ":"), default=str) + "\n")
        sys.stdout.flush()


def _response(request_id: Any, ok: bool, payload: Any, error_code: Optional[str] = None) -> None:
    _emit({
        "message_type": "response",
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "ok": ok,
        "payload": payload,
        "error_code": error_code,
    })


def _emit_event(event_type: str, payload: dict[str, Any]) -> None:
    _emit({
        "message_type": "event",
        "protocol_version": PROTOCOL_VERSION,
        "event_id": f"{event_type}:{payload.get('id', payload.get('orderId', time.time_ns()))}",
        "event_type": event_type,
        "observed_at_utc": _utc_now().isoformat().replace("+00:00", "Z"),
        "payload": payload,
    })


def _project(raw: Any, allowed: tuple[str, ...]) -> dict[str, Any]:
    if isinstance(raw, (list, tuple)):
        raw = next((item for item in raw if isinstance(item, dict)), {})
    if not isinstance(raw, dict):
        return {}
    return {key: raw[key] for key in allowed if key in raw}


def _callback_payload(args: tuple[Any, ...], allowed: tuple[str, ...]) -> dict[str, Any]:
    return _project(args, allowed)


def _exit_decision(request: dict[str, Any]) -> dict[str, Any]:
    data = request.get("payload", request)
    params = StrategyParams()
    allowed_params = {item.name for item in fields(StrategyParams)}
    for key, value in (data.get("params") or {}).items():
        if key in allowed_params:
            setattr(params, key, value)
    direction = Direction(str(data["direction"]).lower())
    policy = resolve_exit_policy(params, str(data.get("strategy_mode", params.strategy)), direction)
    state_data = data.get("state") or {}
    state = ExitState(
        trail_triggered=bool(state_data.get("trail_triggered", False)),
        ladder_max_r=float(state_data.get("ladder_max_r", 0.0)),
        ladder_lock_r=state_data.get("ladder_lock_r"),
    )
    decision = evaluate_exit_operation(
        policy=policy,
        state=state,
        direction=direction,
        entry_price=float(data["entry_price"]),
        current_sl=float(data["current_sl"]),
        original_sl=float(data["original_sl"]),
        tp_price=float(data["tp_price"]),
        market_price=float(data["market_price"]),
        held_minutes=float(data["held_minutes"]),
        tick_size=float(data["tick_size"]),
    )
    return {
        "action": decision.action.value,
        "reason": decision.reason.value if decision.reason else None,
        "stop_price": decision.stop_price,
        "favourable_ticks": decision.favourable_ticks,
        "lock_r": decision.lock_r,
        "state": {
            "trail_triggered": decision.state.trail_triggered,
            "ladder_max_r": decision.state.ladder_max_r,
            "ladder_lock_r": decision.state.ladder_lock_r,
        },
        "policy": {
            "model": policy.model,
            "max_hold_minutes": policy.max_hold_minutes,
            "hard_tp_enabled": policy.hard_tp_enabled,
            "trail_mode": policy.trail_mode.value,
            "trail_trigger_pct": policy.trail_trigger_pct,
            "trail_offset_ticks": policy.trail_offset_ticks,
            "trail_lock_pct": policy.trail_lock_pct,
            "ladder_trigger_r": policy.ladder_trigger_r,
            "ladder_gap_r": policy.ladder_gap_r,
        },
    }


def _market_context(request: dict[str, Any]) -> dict[str, Any]:
    data = request.get("payload", request)
    try:
        timestamp = datetime.fromisoformat(
            str(data["timestamp_utc"]).replace("Z", "+00:00")
        )
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("timezone_required")
        timestamp = timestamp.astimezone(timezone.utc)
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("market_context_timestamp_invalid") from None
    from backend.strategy.session_filter import market_session_code, rth_session_date
    from backend.timebase import topstep_trade_date

    return {
        "rth_date_et": rth_session_date(timestamp).isoformat(),
        "topstep_trade_date_ct": topstep_trade_date(timestamp),
        "is_rth": market_session_code(timestamp) == "RTH",
    }


def _credentials_configured() -> bool:
    return bool(os.getenv("TOPSTEPX_USERNAME", "").strip() and os.getenv("TOPSTEPX_API_KEY", "").strip())


def _event_callback(
    event_type: str,
    allowed: tuple[str, ...],
    selected_account_id: Optional[int] = None,
) -> Callable[..., None]:
    def callback(*args: Any) -> None:
        payload = _callback_payload(args, allowed)
        if event_type.startswith("user_"):
            target_account_id = selected_account_id
            if target_account_id is None and _SESSION is not None:
                target_account_id = int(_SESSION["account_id"])
            account_id = payload.get("accountId", payload.get("id"))
            if target_account_id is None or account_id is None:
                _emit_event("user_event_account_error", {"reason": "account_identity_missing"})
                return
            try:
                if int(account_id) != target_account_id:
                    return
            except (TypeError, ValueError):
                _emit_event("user_event_account_error", {"reason": "account_identity_invalid"})
                return
        if event_type in {"market_trade", "market_quote"}:
            stamp_text = payload.get("timestamp", payload.get("lastUpdated"))
            try:
                parsed_stamp = datetime.fromisoformat(str(stamp_text).replace("Z", "+00:00"))
                if parsed_stamp.tzinfo is None or parsed_stamp.utcoffset() is None:
                    raise ValueError("timezone_required")
                stamp = parsed_stamp.astimezone(timezone.utc)
            except (TypeError, ValueError):
                _emit_event("market_ordering_error", {"reason": "market_timestamp_invalid"})
                return
            with _MARKET_TIME_LOCK:
                previous = _LAST_MARKET_TIMESTAMPS.get(event_type)
                if previous is not None and stamp < previous:
                    _emit_event("market_ordering_error", {
                        "reason": "market_timestamp_regressed",
                        "stream": event_type,
                    })
                    return
                _LAST_MARKET_TIMESTAMPS[event_type] = stamp
        _emit_event(event_type, payload)
    return callback


def _connection_callback(hub_name: str, connected: bool) -> None:
    _emit_event("connection_state", {"hub": hub_name, "connected": bool(connected)})


async def _start_topstepx(payload: dict[str, Any]) -> dict[str, Any]:
    global _CLIENT, _LEASE, _SESSION
    if _CLIENT is not None:
        raise RuntimeError("bridge_already_started")
    if payload.get("mode") != "live":
        raise RuntimeError("live_mode_not_selected")
    for field_name in ("live_armed", "strategy_live_approved", "auto_oco_confirmed"):
        if payload.get(field_name) is not True:
            raise RuntimeError(f"{field_name}_required")
    try:
        account_id = int(payload["account_id"])
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("account_id_required") from None
    contract_id = str(payload.get("contract_id", "")).strip()
    if not contract_id.startswith("CON.F.US.") or len(contract_id.split(".")) < 5:
        raise RuntimeError("full_topstepx_contract_id_required")
    if not _credentials_configured():
        raise RuntimeError("topstepx_credentials_not_configured")
    with _MARKET_TIME_LOCK:
        _LAST_MARKET_TIMESTAMPS.clear()

    from backend.broker.topstepx import TopstepXClient

    lease = LiveEngineLease(account_id)
    if not lease.acquire():
        raise RuntimeError("account_lease_already_owned")
    client = TopstepXClient(
        username=os.environ["TOPSTEPX_USERNAME"].strip(),
        api_key=os.environ["TOPSTEPX_API_KEY"].strip(),
    )
    session = {
        "account_id": account_id,
        "contract_id": contract_id,
        "contract_size": int(payload.get("contract_size", 1)),
        "tick_size": float(payload.get("tick_size", 0.25)),
        "tick_value": float(payload.get("tick_value", 0.50)),
        "live_armed": True,
        "strategy_live_approved": True,
        "auto_oco_confirmed": True,
    }
    try:
        await client.authenticate()
        accounts = await client.get_accounts()
        account = next((item for item in accounts if int(item.get("id", -1)) == account_id), None)
        if account is None:
            raise RuntimeError("selected_account_not_found")
        if account.get("canTrade") is not True or account.get("isVisible") is not True:
            raise RuntimeError("selected_account_not_tradeable_or_not_visible")

        active_contract = await client.get_front_month_contract_id(contract_id)
        if active_contract != contract_id:
            raise RuntimeError("selected_contract_is_not_the_active_front_month")
        root = contract_id.split(".")[3]
        contract_rows = await client.search_contracts(root)
        contract = next((item for item in contract_rows if item.get("id") == contract_id), None)
        if contract is None:
            raise RuntimeError("selected_contract_not_found")
        active_flag = contract.get("activeContract", contract.get("active", contract.get("isActive")))
        if active_flag is False:
            raise RuntimeError("selected_contract_marked_inactive")

        history_days = max(1, min(int(payload.get("history_days", 15)), 30))
        now = _utc_now()
        start = (now - timedelta(days=history_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        bars = await client.get_historical_bars(
            contract_id,
            unit=BarUnit.MINUTE,
            unit_number=1,
            start_time=start,
            end_time=end,
            limit=20000,
        )
        bars = sorted(bars, key=lambda candle: candle.timestamp)
        minimum_bars = max(250, int(payload.get("minimum_warmup_bars", 250)))
        if len(bars) < minimum_bars:
            raise RuntimeError("historical_warmup_below_minimum")
        previous = None
        seen = set()
        warmup = []
        for candle in bars:
            stamp = candle.timestamp.astimezone(timezone.utc)
            if previous is not None and stamp < previous:
                raise RuntimeError("historical_warmup_not_monotonic")
            if stamp in seen:
                continue
            seen.add(stamp)
            previous = stamp
            from backend.strategy.session_filter import market_session_code, rth_session_date
            from backend.timebase import topstep_trade_date

            warmup.append({
                "timestamp_utc": stamp.isoformat().replace("+00:00", "Z"),
                "source": "topstepx",
                "rth_date_et": rth_session_date(stamp).isoformat(),
                "topstep_trade_date_ct": topstep_trade_date(stamp),
                "is_rth": market_session_code(stamp) == "RTH",
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
                "contract_id": contract_id,
            })

        client.add_connection_callback(_connection_callback)
        client.add_user_callback("account", _event_callback(
            "user_account", ("id", "accountId", "name", "canTrade", "isVisible", "simulated"), account_id
        ))
        client.add_user_callback("order", _event_callback(
            "user_order", ("id", "accountId", "contractId", "type", "side", "status", "size",
                           "fillVolume", "filledPrice", "limitPrice", "stopPrice", "customTag", "updateTimestamp"), account_id
        ))
        client.add_user_callback("position", _event_callback(
            "user_position", ("id", "accountId", "contractId", "type", "size", "averagePrice", "creationTimestamp"), account_id
        ))
        client.add_user_callback("trade", _event_callback(
            "user_trade", ("id", "accountId", "contractId", "orderId", "side", "size", "price",
                           "voided", "creationTimestamp"), account_id
        ))
        await client.connect_market_ws()
        client.subscribe_trades(contract_id, _event_callback(
            "market_trade", ("symbolId", "price", "timestamp", "type", "volume")
        ))
        client.subscribe_quotes(contract_id, _event_callback(
            "market_quote", ("symbol", "symbolName", "lastPrice", "bestBid", "bestAsk", "timestamp", "lastUpdated")
        ))
        await client.connect_user_ws(account_id)
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and not (client._market_ready and client._user_ready):
            await asyncio.sleep(0.1)
        if not client._market_ready or not client._user_ready:
            raise RuntimeError("topstepx_realtime_hubs_not_ready")

        orders = await client.get_orders(account_id, days=60)
        positions = await client.get_positions(account_id)
        trades = await client.get_trade_history(account_id, days=60)
        _CLIENT, _LEASE, _SESSION = client, lease, session
        return {
            "account_id": account_id,
            "contract_id": contract_id,
            "can_trade": True,
            "account_visible": True,
            "active_contract_verified": True,
            "account_lease_held": lease.held,
            "market_data_connected": client._market_ready,
            "user_hub_connected": client._user_ready,
            "history_bar_count": len(warmup),
            "history_bars": warmup,
            "orders": orders,
            "positions": positions,
            "trades": trades,
            "entry_orders_sent": 0,
        }
    except Exception:
        try:
            await client.disconnect()
        except Exception:
            pass
        lease.release()
        raise


async def _reconcile() -> dict[str, Any]:
    if _CLIENT is None or _SESSION is None:
        raise RuntimeError("topstepx_bridge_not_started")
    account_id = _SESSION["account_id"]
    orders = await _CLIENT.get_orders(account_id, days=60)
    positions = await _CLIENT.get_positions(account_id)
    trades = await _CLIENT.get_trade_history(account_id, days=60)
    return {
        "account_id": account_id,
        "observed_at_utc": _utc_now().isoformat().replace("+00:00", "Z"),
        "orders": orders,
        "positions": positions,
        "trades": trades,
    }


async def _place_entry(payload: dict[str, Any]) -> dict[str, Any]:
    if _CLIENT is None or _SESSION is None:
        raise RuntimeError("topstepx_bridge_not_started")
    if payload.get("account_id") != _SESSION["account_id"] or payload.get("contract_id") != _SESSION["contract_id"]:
        raise RuntimeError("entry_account_or_contract_mismatch")
    if payload.get("size") != _SESSION["contract_size"]:
        raise RuntimeError("entry_size_mismatch")
    side = int(payload.get("internal_side", 0))
    if side not in (1, 2):
        raise RuntimeError("unsupported_internal_order_side")
    custom_tag = str(payload.get("custom_tag", ""))
    if not custom_tag.startswith("atx-") or len(custom_tag) != 36:
        raise RuntimeError("invalid_idempotency_custom_tag")
    stop_ticks = int(payload.get("stop_loss_ticks", 0))
    target_ticks = int(payload.get("take_profit_ticks", 0))
    if (side == 1 and not (stop_ticks < 0 < target_ticks)) or (side == 2 and not (stop_ticks > 0 > target_ticks)):
        raise RuntimeError("entry_bracket_direction_invalid")
    order = OrderRequest(
        account_id=_SESSION["account_id"],
        contract_id=_SESSION["contract_id"],
        order_type=1,
        side=side,
        size=_SESSION["contract_size"],
        limit_price=float(payload["limit_price"]),
        stop_loss_bracket={"ticks": stop_ticks, "type": 4},
        take_profit_bracket={"ticks": target_ticks, "type": 1},
        custom_tag=custom_tag,
    )
    try:
        response = await _CLIENT.place_order(order)
    except Exception:
        return {"state": "ambiguous", "custom_tag": custom_tag, "retry_allowed": False}
    if response.ambiguous:
        return {"state": "ambiguous", "custom_tag": custom_tag, "retry_allowed": False}
    if not response.success:
        return {
            "state": "rejected",
            "order_id": response.order_id,
            "error_code": response.error_code,
            "error_message": response.error_message,
            "custom_tag": custom_tag,
        }
    return {"state": "accepted", "order_id": response.order_id, "custom_tag": custom_tag}


async def _cancel_entry(payload: dict[str, Any]) -> dict[str, Any]:
    if _CLIENT is None or _SESSION is None:
        raise RuntimeError("topstepx_bridge_not_started")
    order_id = int(payload["order_id"])
    custom_tag = str(payload.get("custom_tag", ""))
    if int(payload.get("account_id", -1)) != _SESSION["account_id"]:
        raise RuntimeError("cancel_account_mismatch")
    if payload.get("contract_id") != _SESSION["contract_id"]:
        raise RuntimeError("cancel_contract_mismatch")
    orders = await _CLIENT.get_orders(_SESSION["account_id"], days=60)
    target = next((order for order in orders if int(order.get("id", -1)) == order_id), None)
    if target is None or target.get("customTag") != custom_tag:
        raise RuntimeError("cancel_order_ownership_not_confirmed")
    if target.get("contractId") != _SESSION["contract_id"]:
        raise RuntimeError("cancel_contract_mismatch")
    status = int(target.get("status", 0))
    if status not in (1, 6):
        return {"state": "not_working", "order_id": order_id, "status": status}
    try:
        response = await _CLIENT.cancel_order_once(_SESSION["account_id"], order_id)
    except Exception:
        return {"state": "ambiguous", "order_id": order_id, "retry_allowed": False}
    if response.ambiguous:
        return {"state": "ambiguous", "order_id": order_id, "retry_allowed": False}
    return {
        "state": "cancel_requested" if response.success else "rejected",
        "order_id": order_id,
        "error_code": response.error_code,
        "error_message": response.error_message,
    }


async def _modify_attached_stop(payload: dict[str, Any]) -> dict[str, Any]:
    if _CLIENT is None or _SESSION is None:
        raise RuntimeError("topstepx_bridge_not_started")
    order_id = int(payload["order_id"])
    if payload.get("contract_id") != _SESSION["contract_id"]:
        raise RuntimeError("attached_stop_contract_mismatch")
    orders = await _CLIENT.get_orders(_SESSION["account_id"], days=60)
    entry_order_id = int(payload["entry_order_id"])
    custom_tag = str(payload["custom_tag"])
    parent = [
        order for order in orders
        if int(order.get("id", -1)) == entry_order_id
        and order.get("customTag") == custom_tag
        and order.get("contractId") == _SESSION["contract_id"]
    ]
    if len(parent) != 1 or int(parent[0].get("fillVolume", 0)) < int(payload["size"]):
        raise RuntimeError("attached_stop_parent_entry_not_confirmed")
    candidates = [order for order in orders if int(order.get("id", -1)) == order_id]
    if len(candidates) != 1:
        raise RuntimeError("attached_stop_not_found_or_ambiguous")
    order = candidates[0]
    if order.get("contractId") != _SESSION["contract_id"] or int(order.get("type", 0)) != 4:
        raise RuntimeError("attached_stop_contract_or_type_mismatch")
    if int(order.get("status", 0)) != 1:
        raise RuntimeError("attached_stop_not_working")
    if int(order.get("side", -1)) != int(payload["side_api"]):
        raise RuntimeError("attached_stop_side_mismatch")
    if order.get("customTag") != custom_tag:
        raise RuntimeError("attached_stop_custom_tag_mismatch")
    total_size = int(order.get("size", 0))
    filled_size = int(order.get("fillVolume", 0))
    if total_size <= 0 or filled_size < 0 or filled_size > total_size:
        raise RuntimeError("attached_stop_order_size_invalid")
    if total_size - filled_size != int(payload["size"]):
        raise RuntimeError("attached_stop_size_mismatch")
    expected_price = float(payload["expected_stop_price"])
    actual_price = float(order.get("stopPrice", 0.0))
    if abs(actual_price - expected_price) > _SESSION["tick_size"]:
        raise RuntimeError("attached_stop_ownership_price_mismatch")
    try:
        response = await _CLIENT.modify_order_once(
            _SESSION["account_id"],
            order_id,
            stop_price=float(payload["stop_price"]),
        )
    except Exception:
        return {"state": "ambiguous", "order_id": order_id, "retry_allowed": False}
    if response.ambiguous:
        return {"state": "ambiguous", "order_id": order_id, "retry_allowed": False}
    return {
        "state": "accepted" if response.success else "rejected",
        "order_id": order_id,
        "error_code": response.error_code,
        "error_message": response.error_message,
    }


async def _close_owned_position(payload: dict[str, Any]) -> dict[str, Any]:
    if _CLIENT is None or _SESSION is None:
        raise RuntimeError("topstepx_bridge_not_started")
    if payload.get("contract_id") != _SESSION["contract_id"]:
        raise RuntimeError("close_contract_mismatch")
    positions = await _CLIENT.get_positions(_SESSION["account_id"])
    contract_positions = [
        position for position in positions
        if position.get("contractId") == _SESSION["contract_id"] and int(position.get("size", 0)) != 0
    ]
    if len(contract_positions) != 1:
        raise RuntimeError("close_position_ownership_not_unique")
    position = contract_positions[0]
    if int(position.get("id", -1)) != int(payload["position_id"]):
        raise RuntimeError("close_position_id_mismatch")
    if int(position.get("size", 0)) != int(payload["size"]):
        raise RuntimeError("close_position_size_mismatch")
    if int(position.get("type", 0)) != int(payload["position_type"]):
        raise RuntimeError("close_position_direction_mismatch")
    if abs(float(position.get("averagePrice", 0.0)) - float(payload["average_price"])) > _SESSION["tick_size"]:
        raise RuntimeError("close_position_price_mismatch")
    entry_order_id = int(payload["entry_order_id"])
    custom_tag = str(payload["custom_tag"])
    orders = await _CLIENT.get_orders(_SESSION["account_id"], days=60)
    parent = [
        order for order in orders
        if int(order.get("id", -1)) == entry_order_id
        and order.get("customTag") == custom_tag
        and order.get("contractId") == _SESSION["contract_id"]
    ]
    if len(parent) != 1:
        raise RuntimeError("close_position_entry_ownership_not_confirmed")
    parent_order = parent[0]
    parent_side = int(parent_order.get("side", -1))
    tagged_child_orders = [
        order for order in orders
        if order.get("contractId") == _SESSION["contract_id"]
        and order.get("customTag") == custom_tag
        and int(order.get("side", -1)) != parent_side
        and int(order.get("type", 0)) in (1, 4)
    ]
    if any(int(order.get("size", 0)) <= 0 for order in tagged_child_orders):
        raise RuntimeError("close_position_child_size_invalid")
    for child in tagged_child_orders:
        status = int(child.get("status", 0))
        total_size = int(child.get("size", 0))
        filled_size = int(child.get("fillVolume", 0))
        if filled_size < 0 or filled_size > total_size:
            raise RuntimeError("close_position_child_fill_volume_invalid")
        if status in (1, 6):
            if status != 1:
                raise RuntimeError("close_position_child_not_open")
            if total_size - filled_size != int(payload["size"]):
                raise RuntimeError("close_position_child_remaining_size_mismatch")
        elif status == 2:
            if filled_size != total_size:
                raise RuntimeError("close_position_child_terminal_fill_mismatch")
        elif status not in (3, 4, 5):
            raise RuntimeError("close_position_child_status_unknown")
    child_ids = {
        int(value)
        for value in (
            payload.get("attached_stop_order_id"),
            payload.get("attached_target_order_id"),
        )
        if value is not None
    }
    verified_child_ids: set[int] = {
        int(order["id"]) for order in tagged_child_orders if order.get("id") is not None
    }
    for child_id in child_ids:
        matches = [order for order in orders if int(order.get("id", -1)) == child_id]
        if len(matches) != 1:
            raise RuntimeError("close_position_child_order_not_unique")
        child = matches[0]
        if (
            child.get("contractId") != _SESSION["contract_id"]
            or child.get("customTag") != custom_tag
            or int(child.get("side", -1)) == int(parent_order.get("side", -2))
            or int(child.get("type", 0)) not in (1, 4)
            or int(child.get("size", 0)) <= 0
        ):
            raise RuntimeError("close_position_child_ownership_not_confirmed")
        verified_child_ids.add(child_id)

    owned_order_ids = {entry_order_id, *verified_child_ids}
    foreign_working_orders = [
        order for order in orders
        if order.get("contractId") == _SESSION["contract_id"]
        and int(order.get("id", -1)) not in owned_order_ids
        and int(order.get("status", 0)) not in (2, 3, 4, 5)
    ]
    if foreign_working_orders:
        raise RuntimeError("close_position_foreign_working_order_present")

    trades = await _CLIENT.get_trade_history(_SESSION["account_id"], days=60)
    entry_fills = [
        trade for trade in trades
        if int(trade.get("orderId", -1)) == entry_order_id
        and trade.get("contractId") == _SESSION["contract_id"]
        and not trade.get("voided", False)
    ]
    exit_fills = [
        trade for trade in trades
        if int(trade.get("orderId", -1)) in verified_child_ids
        and trade.get("contractId") == _SESSION["contract_id"]
        and not trade.get("voided", False)
    ]
    entry_quantity = sum(int(trade.get("size", 0)) for trade in entry_fills)
    exit_quantity = sum(int(trade.get("size", 0)) for trade in exit_fills)
    if int(parent_order.get("fillVolume", 0)) != entry_quantity:
        raise RuntimeError("close_position_entry_fill_history_mismatch")
    if entry_quantity - exit_quantity != int(payload["size"]):
        raise RuntimeError("close_position_net_fill_history_mismatch")
    creation_text = parent_order.get("creationTimestamp")
    try:
        parent_created = datetime.fromisoformat(str(creation_text).replace("Z", "+00:00"))
        if parent_created.tzinfo is None:
            raise ValueError("timezone_required")
    except (TypeError, ValueError):
        raise RuntimeError("close_position_entry_timestamp_unavailable") from None
    foreign_fills = []
    for trade in trades:
        if trade.get("contractId") != _SESSION["contract_id"] or trade.get("voided", False):
            continue
        trade_order_id = int(trade.get("orderId", -1))
        if trade_order_id in owned_order_ids:
            continue
        stamp_text = trade.get("creationTimestamp")
        try:
            trade_created = datetime.fromisoformat(str(stamp_text).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            raise RuntimeError("close_position_foreign_trade_timestamp_unavailable") from None
        if trade_created.tzinfo is None:
            raise RuntimeError("close_position_foreign_trade_timezone_missing")
        if trade_created >= parent_created:
            foreign_fills.append(trade_order_id)
    if foreign_fills:
        raise RuntimeError("close_position_foreign_trade_activity_present")
    try:
        response = await _CLIENT.close_position_once(
            _SESSION["account_id"], _SESSION["contract_id"]
        )
    except Exception:
        return {"state": "ambiguous", "retry_allowed": False}
    if response.ambiguous:
        return {"state": "ambiguous", "retry_allowed": False}
    return {
        "state": "accepted" if response.success else "rejected",
        "order_id": response.order_id,
        "error_code": response.error_code,
        "error_message": response.error_message,
    }


async def _stop() -> dict[str, Any]:
    global _CLIENT, _LEASE, _SESSION
    client, lease = _CLIENT, _LEASE
    _CLIENT = None
    _LEASE = None
    _SESSION = None
    if client is not None:
        try:
            await client.disconnect()
        except Exception:
            logging.getLogger(__name__).exception("TopstepX disconnect failed during shutdown")
    if lease is not None:
        lease.release()
    return {"stopped": True}


async def _dispatch(request: dict[str, Any]) -> tuple[bool, Any, Optional[str]]:
    if request.get("protocol_version", PROTOCOL_VERSION) != PROTOCOL_VERSION:
        return False, {}, "unsupported_bridge_protocol_version"
    operation = str(request.get("operation", ""))
    payload = request.get("payload") or {}
    if operation == "capabilities":
        return True, {
            "mode_default": "paper",
            "topstepx_credentials_configured": _credentials_configured(),
            "topstepx_bridge_started": _CLIENT is not None,
            "account_lease_held": bool(_LEASE and _LEASE.held),
        }, None
    if operation == "exit_decision":
        return True, _exit_decision(request), None
    if operation == "market_context":
        return True, _market_context(request), None
    if operation == "start_topstepx":
        result = await _start_topstepx(payload)
        return True, result, None
    if operation == "reconcile":
        return True, await _reconcile(), None
    if operation == "place_entry":
        return True, await _place_entry(payload), None
    if operation == "cancel_entry":
        return True, await _cancel_entry(payload), None
    if operation == "modify_attached_stop":
        return True, await _modify_attached_stop(payload), None
    if operation == "close_owned_position":
        return True, await _close_owned_position(payload), None
    if operation == "stop":
        return True, await _stop(), None
    return False, {}, "unsupported_operation"


def main() -> int:
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            request_id = None
            try:
                request = json.loads(line)
                request_id = request.get("request_id")
                ok, payload, error_code = _LOOP.run_until_complete(_dispatch(request))
                _response(request_id, ok, payload, error_code)
                if request.get("operation") == "stop":
                    break
            except Exception as exc:
                message = str(exc)
                error_code = message if message and all(char.isalnum() or char in "._-" for char in message) else "bridge_operation_failed"
                _response(request_id, False, {}, error_code)
        if _CLIENT is not None or _LEASE is not None:
            _LOOP.run_until_complete(_stop())
    finally:
        _LOOP.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
