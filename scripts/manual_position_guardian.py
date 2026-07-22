"""Guard one existing TopstepX position with a software-managed SL/TP pair.

This tool is intentionally separate from the trading engine.  It exists for an
already-open (usually manual) position that has no broker-side Auto OCO orders.
It places the stop first, confirms it is working, then places the target.  The
two exits are *not* native OCO: this process must remain running so it can
cancel the surviving sibling as soon as the position closes.

Examples (read-only by default)::

    python -m scripts.manual_position_guardian --account-id 123 --sl 100 --tp 130
    python -m scripts.manual_position_guardian --account-id 123 --sl 100 --tp 130 --execute
    python -m scripts.manual_position_guardian --account-id 123 --sl 100 --tp 130 \
        --adopt-sl-order-id 456 --adopt-tp-order-id 789 --execute

Safety properties:
* ``--execute`` is required for any broker mutation; default/``--dry-run`` only
  inspects and prints a sanitized plan.
* Orders are adopted, modified, or cancelled only by exact IDs persisted by
  this tool.  Similar-looking external orders are never adopted.
* An unknown close-side order on the target contract blocks a fresh launch.
* A stop is confirmed working before a target is submitted.
* Flat/replaced/reversed positions cause immediate cancellation of every
  still-open owned exit.  A size change cancels TP, resizes SL, then rebuilds TP.

This is an emergency bridge, not a substitute for native broker OCO.
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.broker.topstepx import TopstepXClient
from backend.db.models import OrderRequest
from backend.live.manual_guardian_launcher import guardian_lock_owner_path

LOG = logging.getLogger("position_guardian")


class GuardianError(RuntimeError):
    """A safety precondition failed; no further broker mutation is allowed."""


class GuardianRetry(RuntimeError):
    """A transient safety ambiguity; retain ownership and retry under the lock."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _first(row: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return None


def _int(row: Dict[str, Any], *keys: str) -> Optional[int]:
    value = _first(row, *keys)
    try:
        return int(float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _float(row: Dict[str, Any], *keys: str) -> Optional[float]:
    value = _first(row, *keys)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _position_id(row: Dict[str, Any]) -> Optional[int]:
    return _int(row, "id", "positionId", "position_id")


def _contract(row: Dict[str, Any]) -> str:
    return str(_first(row, "contractId", "contract_id", "contractID") or "")


def _direction(row: Dict[str, Any]) -> str:
    """Normalize ProjectX position direction to ``long`` or ``short``."""
    raw_side = _first(row, "side", "positionSide")
    if raw_side is not None:
        text = str(raw_side).strip().lower()
        if text in {"0", "buy", "bid", "long"}:
            return "long"
        if text in {"1", "sell", "ask", "short"}:
            return "short"

    # ProjectX open-position payloads often expose 1=Long, 2=Short as `type`.
    raw_type = _first(row, "type", "positionType")
    text = str(raw_type).strip().lower()
    if text in {"1", "long", "buy"}:
        return "long"
    if text in {"2", "short", "sell"}:
        return "short"
    raise GuardianError("Position direction is missing or unknown")


def _order_id(row: Dict[str, Any]) -> Optional[int]:
    return _int(row, "id", "orderId", "order_id")


def _api_order_side(row: Dict[str, Any]) -> Optional[int]:
    raw = _first(row, "side", "orderSide", "order_side")
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if text in {"0", "buy", "bid"}:
        return 0
    if text in {"1", "sell", "ask"}:
        return 1
    return None


def _api_order_type(row: Dict[str, Any]) -> Optional[int]:
    raw = _first(row, "type", "orderType", "order_type")
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if text.isdigit():
        return int(text)
    if "stop" in text:
        return 4
    if "limit" in text:
        return 1
    return None


def _explicit_order_id(value: Optional[int], flag: str) -> Optional[int]:
    """Normalize an explicitly adopted broker id and reject ambiguous values."""
    if value is None:
        return None
    try:
        order_id = int(value)
    except (TypeError, ValueError) as exc:
        raise GuardianError(f"{flag} must be a positive integer") from exc
    if order_id <= 0:
        raise GuardianError(f"{flag} must be a positive integer")
    return order_id


@dataclass(frozen=True)
class TargetPosition:
    account_id: int
    position_id: int
    contract_id: str
    direction: str
    size: int
    average_price: float
    creation_timestamp: str

    @classmethod
    def from_api(cls, account_id: int, row: Dict[str, Any]) -> "TargetPosition":
        pid = _position_id(row)
        size = abs(_int(row, "size", "quantity", "qty") or 0)
        average = _float(row, "averagePrice", "avgPrice", "average_price")
        contract_id = _contract(row)
        if not pid or not contract_id or size <= 0 or average is None:
            raise GuardianError("Position is missing id, contract, size, or average price")
        return cls(
            account_id=account_id,
            position_id=pid,
            contract_id=contract_id,
            direction=_direction(row),
            size=size,
            average_price=average,
            creation_timestamp=str(
                _first(row, "creationTimestamp", "createdAt", "timestamp") or ""
            ),
        )


@dataclass(frozen=True)
class OwnedOrderDisposition:
    state: str
    fill_volume: int = 0


class StateStore:
    """Small atomic JSON state file containing only non-secret identifiers."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> Optional[Dict[str, Any]]:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GuardianError(f"Cannot read guardian state {self.path}: {exc}") from exc
        if not isinstance(data, dict) or data.get("owner") != "ancserTPX-position-guardian-v1":
            raise GuardianError(f"Refusing unrecognized state file: {self.path}")
        return data

    def save(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data["updated_at"] = _utc_now()
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(
            json.dumps(data, indent=2, sort_keys=True, ensure_ascii=True),
            encoding="utf-8",
        )
        os.replace(temp, self.path)


class InstanceLock:
    """Cross-platform process-lifetime lock backed by an OS byte-range lock.

    PID files alone have a stale-owner race: two children can both decide that a
    dead lock is removable and then delete each other's replacement. The OS lock
    is released automatically on crash and is the authority; JSON is only atomic
    owner metadata for the launcher/UI.
    """

    def __init__(self, path: Path, *, metadata: Optional[Dict[str, Any]] = None):
        self.path = path
        self.token = str(uuid.uuid4())
        self.acquired = False
        self.metadata = dict(metadata or {})
        self._handle = None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except OSError:
            return False

    def _write_owner(self, *, active: bool) -> None:
        owner_path = guardian_lock_owner_path(self.path)
        payload = {
            **self.metadata,
            "pid": os.getpid(),
            "token": self.token,
            "active": bool(active),
            "heartbeat_at": _utc_now(),
        }
        if active:
            payload.setdefault("created_at", _utc_now())
        else:
            payload["released_at"] = _utc_now()
        owner_path.parent.mkdir(parents=True, exist_ok=True)
        temp = owner_path.with_name(f".{owner_path.name}.{os.getpid()}.{self.token}.tmp")
        try:
            temp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            os.replace(temp, owner_path)
        finally:
            temp.unlink(missing_ok=True)

    @staticmethod
    def _lock_handle(handle) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK, 13, 36}:
                    raise GuardianError("Another guardian process owns the account lock") from exc
                raise
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    raise GuardianError("Another guardian process owns the account lock") from exc
                raise

    @staticmethod
    def _unlock_handle(handle) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            self._lock_handle(handle)
        except Exception:
            handle.close()
            raise
        self._handle = handle
        self.acquired = True
        self._write_owner(active=True)

    def heartbeat(self) -> None:
        if self.acquired:
            self._write_owner(active=True)

    def release(self) -> None:
        if not self.acquired:
            return
        handle = self._handle
        try:
            self._write_owner(active=False)
        except OSError:
            pass
        if handle is not None:
            try:
                self._unlock_handle(handle)
            finally:
                handle.close()
        self._handle = None
        self.acquired = False


class PositionGuardian:
    """Software-OCO guardian.  The client is injectable for deterministic tests."""

    def __init__(
        self,
        client: Any,
        *,
        account_id: Optional[int],
        contract_id: Optional[str],
        position_id: Optional[int],
        sl_price: float,
        tp_price: float,
        execute: bool,
        adopt_sl_order_id: Optional[int] = None,
        adopt_tp_order_id: Optional[int] = None,
        poll_seconds: float = 2.5,
        confirm_timeout: float = 5.0,
        state_path: Optional[Path] = None,
    ):
        self.client = client
        self.requested_account_id = account_id
        self.requested_contract_id = contract_id
        self.requested_position_id = position_id
        self.sl_price = float(sl_price)
        self.tp_price = float(tp_price)
        self.execute = execute
        self.adopt_sl_order_id = _explicit_order_id(
            adopt_sl_order_id,
            "--adopt-sl-order-id",
        )
        self.adopt_tp_order_id = _explicit_order_id(
            adopt_tp_order_id,
            "--adopt-tp-order-id",
        )
        if (
            self.adopt_sl_order_id is not None
            and self.adopt_sl_order_id == self.adopt_tp_order_id
        ):
            raise GuardianError("Explicit SL and TP order ids must be distinct")
        # One cycle uses Position/searchOpen + Order/searchOpen. The launcher
        # uses 2.5s so two supported accounts still leave API headroom; keep an
        # absolute 1s floor for explicit emergency invocations.
        self.poll_seconds = max(1.0, float(poll_seconds))
        self.confirm_timeout = max(0.2, float(confirm_timeout))
        self.requested_state_path = state_path
        self.target: Optional[TargetPosition] = None
        self.store: Optional[StateStore] = None
        self.lock: Optional[InstanceLock] = None
        self.state: Dict[str, Any] = {}
        self._stop = False
        self._recovery_without_position = False
        self._last_tp_retry_ts = 0.0

    async def _candidate_positions(self) -> List[TargetPosition]:
        if self.requested_account_id is not None:
            accounts = [self.requested_account_id]
        else:
            account_rows = await self.client.get_accounts()
            accounts = []
            for row in account_rows:
                account_id = _int(row, "id", "accountId", "account_id")
                if account_id:
                    accounts.append(account_id)

        candidates: List[TargetPosition] = []
        for account_id in accounts:
            for row in await self.client.get_positions(account_id):
                try:
                    position = TargetPosition.from_api(account_id, row)
                except GuardianError as exc:
                    LOG.warning("Ignoring malformed position on account %s: %s", account_id, exc)
                    continue
                if self.requested_contract_id and position.contract_id != self.requested_contract_id:
                    continue
                if self.requested_position_id and position.position_id != self.requested_position_id:
                    continue
                candidates.append(position)
        return candidates

    @staticmethod
    def _validate_prices(target: TargetPosition, sl_price: float, tp_price: float) -> None:
        if sl_price <= 0 or tp_price <= 0 or sl_price == tp_price:
            raise GuardianError("SL and TP must be distinct positive prices")
        if target.direction == "long" and not (sl_price < target.average_price < tp_price):
            raise GuardianError(
                f"Long protection must satisfy SL < average ({target.average_price:.2f}) < TP"
            )
        if target.direction == "short" and not (tp_price < target.average_price < sl_price):
            raise GuardianError(
                f"Short protection must satisfy TP < average ({target.average_price:.2f}) < SL"
            )

    def _default_state_path(self, target: TargetPosition) -> Path:
        return (
            ROOT
            / "data"
            / "position_guardian"
            / f"account_{target.account_id}_position_{target.position_id}.json"
        )

    def _new_state(self, target: TargetPosition) -> Dict[str, Any]:
        return {
            "owner": "ancserTPX-position-guardian-v1",
            "guardian_id": str(uuid.uuid4()),
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "status": "planned",
            "account_id": target.account_id,
            "position_id": target.position_id,
            "contract_id": target.contract_id,
            "direction": target.direction,
            "creation_timestamp": target.creation_timestamp,
            "initial_size": target.size,
            "protected_size": target.size,
            "average_price": target.average_price,
            "sl_price": self.sl_price,
            "tp_price": self.tp_price,
            "sl_order_id": None,
            "tp_order_id": None,
        }

    def _validate_resume_state(self, state: Dict[str, Any], target: TargetPosition) -> None:
        expected = {
            "account_id": target.account_id,
            "position_id": target.position_id,
            "contract_id": target.contract_id,
            "direction": target.direction,
        }
        for key, value in expected.items():
            if state.get(key) != value:
                raise GuardianError(f"State target mismatch for {key}; refusing adoption")
        saved_average = _float(state, "average_price")
        if saved_average is None:
            raise GuardianError("State average price is missing; refusing adoption")
        saved_creation = str(state.get("creation_timestamp") or "")
        if (
            saved_creation
            and target.creation_timestamp
            and saved_creation != target.creation_timestamp
        ):
            raise GuardianError("State creation timestamp differs from live position; refusing adoption")
        if abs(float(state.get("sl_price", 0)) - self.sl_price) > 1e-9:
            raise GuardianError("State SL differs from command line; refusing adoption")
        if abs(float(state.get("tp_price", 0)) - self.tp_price) > 1e-9:
            raise GuardianError("State TP differs from command line; refusing adoption")
        if abs(saved_average - target.average_price) > 1e-6:
            if not saved_creation or not target.creation_timestamp:
                raise GuardianError(
                    "State average changed without a stable creation identity; refusing adoption"
                )
            self._validate_prices(target, self.sl_price, self.tp_price)
            state["average_price"] = target.average_price

    @staticmethod
    def _target_from_state(state: Dict[str, Any]) -> TargetPosition:
        try:
            target = TargetPosition(
                account_id=int(state["account_id"]),
                position_id=int(state["position_id"]),
                contract_id=str(state["contract_id"]),
                direction=str(state["direction"]),
                size=int(state.get("protected_size") or state["initial_size"]),
                average_price=float(state["average_price"]),
                creation_timestamp=str(state.get("creation_timestamp") or ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GuardianError("Persisted state cannot reconstruct its target") from exc
        if (
            target.account_id <= 0
            or target.position_id <= 0
            or not target.contract_id
            or target.direction not in {"long", "short"}
            or target.size <= 0
            or target.average_price <= 0
        ):
            raise GuardianError("Persisted target identity is invalid")
        return target

    async def prepare(self) -> TargetPosition:
        candidates = await self._candidate_positions()
        existing: Optional[Dict[str, Any]] = None
        if len(candidates) != 1:
            # A detached restart can occur after the position closed but before
            # its non-OCO sibling was cancelled. Reconstruct only from an exact,
            # explicitly supplied owned state file so orphan cleanup can resume.
            if not self.execute or self.requested_state_path is None:
                summary = [
                    f"a={p.account_id} p={p.position_id} {p.contract_id} {p.direction} x{p.size}"
                    for p in candidates
                ]
                raise GuardianError(
                    f"Expected exactly one matching open position, found {len(candidates)}: {summary}"
                )
            self.store = StateStore(self.requested_state_path)
            existing = self.store.load()
            if not existing:
                raise GuardianError("No live target and no recognized owned state to recover")
            target = self._target_from_state(existing)
            if self.requested_account_id not in {None, target.account_id}:
                raise GuardianError("Persisted recovery account differs from command line")
            if self.requested_position_id not in {None, target.position_id}:
                raise GuardianError("Persisted recovery position differs from command line")
            if self.requested_contract_id not in {None, target.contract_id}:
                raise GuardianError("Persisted recovery contract differs from command line")
            self._recovery_without_position = True
        else:
            target = candidates[0]
        self._validate_prices(target, self.sl_price, self.tp_price)
        self.target = target
        if self.store is None:
            self.store = StateStore(self.requested_state_path or self._default_state_path(target))

        if existing is None:
            existing = self.store.load() if self.execute else None
        if existing:
            self._validate_resume_state(existing, target)
            if (
                self.adopt_sl_order_id
                and int(existing.get("sl_order_id") or 0) != self.adopt_sl_order_id
            ):
                raise GuardianError(
                    "--adopt-sl-order-id differs from the persisted owned SL id"
                )
            if (
                self.adopt_tp_order_id
                and int(existing.get("tp_order_id") or 0) != self.adopt_tp_order_id
            ):
                raise GuardianError(
                    "--adopt-tp-order-id differs from the persisted owned TP id"
                )
            self.state = existing
            LOG.info(
                "Resume guardian=%s owned SL=%s TP=%s",
                str(existing.get("guardian_id", "?"))[:8],
                existing.get("sl_order_id"),
                existing.get("tp_order_id"),
            )
        else:
            self.state = self._new_state(target)
            if self.adopt_sl_order_id or self.adopt_tp_order_id:
                # Explicit command-line adoption is the only exception to the
                # "returned-by-this-process" ownership rule.  `arm()` still
                # verifies every supplied broker id, contract, side, type, size,
                # and price before creating either missing protection leg.
                self.state.update(
                    status="adopting_explicit_exits",
                    sl_order_id=self.adopt_sl_order_id,
                    tp_order_id=self.adopt_tp_order_id,
                )

        LOG.info(
            "%s a=%s p=%s %s %s x%s avg=%.2f SL=%.2f TP=%.2f poll=%.2fs",
            "EXECUTE" if self.execute else "DRY-RUN",
            target.account_id,
            target.position_id,
            target.contract_id,
            target.direction.upper(),
            target.size,
            target.average_price,
            self.sl_price,
            self.tp_price,
            self.poll_seconds,
        )
        return target

    @property
    def _owned_ids(self) -> set[int]:
        result: set[int] = set()
        for key in ("sl_order_id", "tp_order_id"):
            try:
                value = int(self.state.get(key) or 0)
            except (TypeError, ValueError):
                value = 0
            if value:
                result.add(value)
        return result

    def _is_close_side_order(self, order: Dict[str, Any]) -> bool:
        assert self.target is not None
        expected_side = 1 if self.target.direction == "long" else 0
        return (
            _contract(order) == self.target.contract_id
            and _api_order_side(order) == expected_side
            and _api_order_type(order) in {1, 4, 5}
        )

    def _assert_no_unowned_conflicts(self, orders: Iterable[Dict[str, Any]]) -> None:
        conflicts = [
            _order_id(order)
            for order in orders
            if self._is_close_side_order(order) and _order_id(order) not in self._owned_ids
        ]
        if conflicts:
            raise GuardianError(
                f"Unknown close-side order(s) {conflicts} already exist; refusing duplicate protection"
            )

    def _validate_owned_order(
        self,
        order: Dict[str, Any],
        *,
        kind: str,
        size: int,
    ) -> None:
        assert self.target is not None
        expected_type = 4 if kind == "SL" else 1
        expected_side = 1 if self.target.direction == "long" else 0
        actual_size = abs(_int(order, "size", "quantity", "qty", "remainingSize") or 0)
        actual_price = _float(
            order,
            "stopPrice" if kind == "SL" else "limitPrice",
            "stop_price" if kind == "SL" else "limit_price",
            "price",
        )
        expected_price = self.sl_price if kind == "SL" else self.tp_price
        if (
            _contract(order) != self.target.contract_id
            or _api_order_side(order) != expected_side
            or _api_order_type(order) != expected_type
            or actual_size != size
            or actual_price is None
            or abs(actual_price - expected_price) > 1e-6
        ):
            raise GuardianError(f"Owned {kind} order no longer matches persisted protection")

    async def _wait_for_owned_order(self, key: str, kind: str, size: int) -> Dict[str, Any]:
        order_id = int(self.state.get(key) or 0)
        deadline = time.monotonic() + self.confirm_timeout
        while time.monotonic() < deadline:
            orders = await self.client.get_open_orders(self.target.account_id)  # type: ignore[union-attr]
            by_id = {_order_id(row): row for row in orders}
            order = by_id.get(order_id)
            if order:
                self._validate_owned_order(order, kind=kind, size=size)
                return order
            await asyncio.sleep(min(0.25, self.poll_seconds))
        raise GuardianError(
            f"{kind} #{order_id} was accepted but not confirmed open; TP was not added"
        )

    def _exact_target_snapshot(
        self,
        rows: Iterable[Dict[str, Any]],
        *,
        expected_size: int,
    ) -> Optional[TargetPosition]:
        """Return the target only when identity, direction, size, and average match."""
        assert self.target is not None
        current = self._matching_target(rows)
        if current is None:
            return None
        if current.size != expected_size:
            return None
        if abs(current.average_price - self.target.average_price) > 1e-6:
            return None
        return current

    async def _revalidate_position_before_tp(self, expected_size: int) -> None:
        """Confirm the exact position twice before submitting a new target.

        A stop can fill between its open-order confirmation and TP submission.
        Position/searchOpen snapshots can also be briefly stale, so a mismatch is
        confirmed by a second position snapshot before any owned SL is cancelled.
        """
        assert self.target is not None and self.store is not None
        exact_count = 0
        latest_current: Optional[TargetPosition] = None
        for attempt in range(2):
            positions = await self.client.get_positions(self.target.account_id)
            latest_current = self._matching_target(positions)
            if self._exact_target_snapshot(positions, expected_size=expected_size) is not None:
                exact_count += 1
            else:
                exact_count = 0
            if attempt == 0:
                await asyncio.sleep(0.25)
        if exact_count == 2:
            return

        if latest_current is not None:
            # Never cancel the only confirmed stop merely because a scale-in,
            # scale-out, or broker average update raced the TP confirmation.
            # The next cycle reconciles size/average while that stop remains live.
            raise GuardianRetry(
                "Position changed during TP confirmation; retaining SL for resize recovery"
            )

        orders = await self.client.get_open_orders(self.target.account_id)
        await self._cancel_owned_open(orders, "position changed after SL confirmation")
        self.state["status"] = "finished_pre_tp_position_changed"
        self.store.save(self.state)
        raise GuardianError(
            "Position changed after SL confirmation; owned SL cleared and TP was not submitted"
        )

    async def _place_sl(self, size: int) -> None:
        assert self.target is not None and self.store is not None
        close_side = 2 if self.target.direction == "long" else 1
        response = await self.client.place_order(
            OrderRequest(
                account_id=self.target.account_id,
                contract_id=self.target.contract_id,
                order_type=3,
                side=close_side,
                size=size,
                stop_price=self.sl_price,
            )
        )
        if not response.success or not response.order_id:
            raise GuardianError(
                f"SL rejected code={response.error_code} message={response.error_message or 'none'}"
            )
        self.state.update(status="sl_submitted", sl_order_id=int(response.order_id))
        self.store.save(self.state)
        await self._wait_for_owned_order("sl_order_id", "SL", size)
        self.state["status"] = "sl_confirmed"
        self.store.save(self.state)
        LOG.warning("SL CONFIRMED #%s at %.2f x%s", response.order_id, self.sl_price, size)

    async def _place_tp(self, size: int) -> None:
        assert self.target is not None and self.store is not None
        close_side = 2 if self.target.direction == "long" else 1
        response = await self.client.place_order(
            OrderRequest(
                account_id=self.target.account_id,
                contract_id=self.target.contract_id,
                order_type=1,
                side=close_side,
                size=size,
                limit_price=self.tp_price,
            )
        )
        if not response.success or not response.order_id:
            raise GuardianError(
                f"TP rejected code={response.error_code} message={response.error_message or 'none'}; SL remains live"
            )
        self.state.update(status="tp_submitted", tp_order_id=int(response.order_id))
        self.store.save(self.state)
        await self._wait_for_owned_order("tp_order_id", "TP", size)
        self.state.update(status="guarding", protected_size=size)
        self.store.save(self.state)
        LOG.warning("TP CONFIRMED #%s at %.2f x%s", response.order_id, self.tp_price, size)

    async def arm(self) -> None:
        """Validate current orders, or create a fresh stop-then-target pair."""
        assert self.target is not None and self.store is not None
        orders = await self.client.get_open_orders(self.target.account_id)
        self._assert_no_unowned_conflicts(orders)
        by_id = {_order_id(row): row for row in orders}

        sl_id = int(self.state.get("sl_order_id") or 0)
        tp_id = int(self.state.get("tp_order_id") or 0)
        protected_size = int(self.state.get("protected_size") or self.target.size)

        if (sl_id or tp_id) and protected_size != self.target.size:
            # A crash may occur after the broker accepted the resized SL but
            # before protected_size was persisted. Validate exact identity,
            # geometry and either old/new size, then let _resize reconcile the
            # exact IDs idempotently instead of rejecting forever.
            accepted_sizes = {protected_size, self.target.size}
            for order_id, kind in ((sl_id, "SL"), (tp_id, "TP")):
                if not order_id or order_id not in by_id:
                    continue
                actual_size = abs(
                    _int(by_id[order_id], "size", "quantity", "qty", "remainingSize") or 0
                )
                if actual_size not in accepted_sizes:
                    raise GuardianError(
                        f"Owned {kind} size x{actual_size} is neither persisted nor live size"
                    )
                self._validate_owned_order(by_id[order_id], kind=kind, size=actual_size)
            await self._resize(self.target, orders)
            return

        if sl_id:
            if sl_id not in by_id:
                raise GuardianError(
                    f"Persisted SL #{sl_id} is not open while position remains; refusing ambiguous replacement"
                )
            self._validate_owned_order(by_id[sl_id], kind="SL", size=protected_size)
        if tp_id:
            if tp_id not in by_id:
                raise GuardianError(
                    f"Persisted TP #{tp_id} is not open while position remains; refusing ambiguous replacement"
                )
            self._validate_owned_order(by_id[tp_id], kind="TP", size=protected_size)

        placed_sl = False
        if not sl_id:
            await self._place_sl(self.target.size)
            placed_sl = True
        if placed_sl or not tp_id:
            await self._revalidate_position_before_tp(self.target.size)
        if not tp_id:
            await self._place_tp(self.target.size)
        self.state.update(status="guarding", protected_size=self.target.size)
        self.store.save(self.state)
        LOG.warning("SOFTWARE OCO ARMED - keep this process running")

    async def _cancel_owned_order_verified(
        self,
        order_id: int,
        label: str,
        reason: str,
        *,
        request_cancel: bool,
    ) -> OwnedOrderDisposition:
        """Return a terminal exact-ID disposition only after broker proof."""
        assert self.target is not None
        if request_cancel:
            ok = await self.client.cancel_order(self.target.account_id, order_id)
            LOG.warning("Cancel owned %s #%s (%s): %s", label, order_id, reason, ok)

        safe_absent_count = 0
        for attempt in range(4):
            open_orders = await self.client.get_open_orders(self.target.account_id)
            if any(_order_id(row) == order_id for row in open_orders):
                safe_absent_count = 0
                if request_cancel and attempt in {1, 2}:
                    await self.client.cancel_order(self.target.account_id, order_id)
                await asyncio.sleep(0.25)
                continue

            all_orders = await self.client.get_orders(self.target.account_id)
            ledger = next((row for row in all_orders if _order_id(row) == order_id), None)
            status = _int(ledger or {}, "status", "orderStatus")
            fill_volume = abs(
                _int(ledger or {}, "fillVolume", "filledSize", "filledVolume") or 0
            )
            if status == 2:
                LOG.warning("Owned %s #%s filled while cancellation was resolving", label, order_id)
                return OwnedOrderDisposition("filled", fill_volume)
            if status in {3, 4, 5}:
                safe_absent_count += 1
                if safe_absent_count >= 2:
                    if fill_volume > 0:
                        LOG.warning(
                            "Owned %s #%s ended status=%s with partial fill x%s",
                            label,
                            order_id,
                            status,
                            fill_volume,
                        )
                        return OwnedOrderDisposition("partial", fill_volume)
                    return OwnedOrderDisposition("cancelled", 0)
            else:
                safe_absent_count = 0
            await asyncio.sleep(0.25)

        raise GuardianRetry(
            f"Owned {label} #{order_id} cancellation is not broker-confirmed; retaining ownership"
        )

    async def _cancel_owned_open(
        self,
        orders: Iterable[Dict[str, Any]],
        reason: str,
    ) -> Dict[int, OwnedOrderDisposition]:
        assert self.target is not None and self.store is not None
        open_ids = {_order_id(order) for order in orders}
        dispositions: Dict[int, OwnedOrderDisposition] = {}
        # TP first minimizes the chance of an old profit order opening a reverse position.
        for key, label in (("tp_order_id", "TP"), ("sl_order_id", "SL")):
            order_id = int(self.state.get(key) or 0)
            if not order_id:
                continue
            disposition = await self._cancel_owned_order_verified(
                order_id,
                label,
                reason,
                request_cancel=order_id in open_ids,
            )
            dispositions[order_id] = disposition
            if disposition.state == "cancelled":
                self.state[key] = None
                self.store.save(self.state)
        return dispositions

    def _matching_target(self, rows: Iterable[Dict[str, Any]]) -> Optional[TargetPosition]:
        assert self.target is not None
        for row in rows:
            if _position_id(row) != self.target.position_id:
                continue
            try:
                current = TargetPosition.from_api(self.target.account_id, row)
            except GuardianError:
                return None
            if (
                current.contract_id != self.target.contract_id
                or current.direction != self.target.direction
                or (
                    self.target.creation_timestamp
                    and current.creation_timestamp
                    and current.creation_timestamp != self.target.creation_timestamp
                )
            ):
                return None
            return current
        return None

    def _accept_live_target_update(self, current: TargetPosition) -> None:
        """Accept a same-identity scale/average update without changing exits."""
        assert self.target is not None and self.store is not None
        if (
            current.position_id != self.target.position_id
            or current.contract_id != self.target.contract_id
            or current.direction != self.target.direction
            or (
                self.target.creation_timestamp
                and current.creation_timestamp
                and current.creation_timestamp != self.target.creation_timestamp
            )
        ):
            raise GuardianError("Live position identity changed; refusing target update")
        self._validate_prices(current, self.sl_price, self.tp_price)
        if abs(current.average_price - self.target.average_price) > 1e-6:
            self.state["average_price"] = current.average_price
            self.store.save(self.state)
        self.target = current

    async def _resize(self, current: TargetPosition, orders: List[Dict[str, Any]]) -> bool:
        """Cancel TP, resize stop, confirm it, then rebuild TP for current size."""
        assert self.target is not None and self.store is not None
        previous_size = int(self.state.get("protected_size") or self.target.size)
        self._accept_live_target_update(current)
        LOG.warning("Position size changed x%s -> x%s; rebuilding protection", previous_size, current.size)

        tp_id = int(self.state.get("tp_order_id") or 0)
        open_ids = {_order_id(order) for order in orders}
        if tp_id:
            disposition = await self._cancel_owned_order_verified(
                tp_id,
                "TP",
                "position resize",
                request_cancel=tp_id in open_ids,
            )
            if disposition.state != "cancelled":
                raise GuardianRetry(
                    f"TP {disposition.state} x{disposition.fill_volume} while resize was starting; "
                    "waiting for position sync"
                )
            self.state["tp_order_id"] = None
            self.store.save(self.state)

        sl_id = int(self.state.get("sl_order_id") or 0)
        if not sl_id or sl_id not in open_ids:
            raise GuardianError("SL disappeared during resize; refusing ambiguous replacement")
        response = await self.client.modify_order(
            self.target.account_id,
            sl_id,
            size=current.size,
            stop_price=self.sl_price,
        )
        if not response.success:
            # An oversized exit can reverse the account. Prove it cancelled,
            # then flatten the now-unprotected position under the same lock.
            latest_orders = await self.client.get_open_orders(self.target.account_id)
            latest_open_ids = {_order_id(order) for order in latest_orders}
            disposition = await self._cancel_owned_order_verified(
                sl_id,
                "SL",
                "resize modify rejected",
                request_cancel=sl_id in latest_open_ids,
            )
            if disposition.state != "cancelled":
                raise GuardianRetry(
                    f"SL {disposition.state} x{disposition.fill_volume} while resize failed; "
                    "waiting for position sync"
                )
            self.state.update(sl_order_id=None, status="resize_failed_closing")
            self.store.save(self.state)
            await self._failsafe_close_contract("SL resize failed after exits were cleared")
            self.state["status"] = "finished_resize_failed_flattened"
            self.store.save(self.state)
            return False
        await self._wait_for_owned_order("sl_order_id", "SL", current.size)
        self.state["protected_size"] = current.size
        self.store.save(self.state)
        await self._revalidate_position_before_tp(current.size)
        await self._place_tp(current.size)
        return True

    @staticmethod
    def _fill_volume(order: Dict[str, Any]) -> int:
        return abs(_int(order, "fillVolume", "filledSize", "filledVolume") or 0)

    async def _owned_fill_snapshot(self) -> Dict[int, int]:
        """Return full-ledger fill volume for every exact persisted owned ID."""
        assert self.target is not None
        rows = await self.client.get_orders(self.target.account_id)
        by_id = {_order_id(row): row for row in rows}
        return {
            order_id: self._fill_volume(by_id.get(order_id, {}))
            for order_id in self._owned_ids
        }

    @staticmethod
    def _position_signature(rows: Iterable[Dict[str, Any]], contract_id: str) -> Tuple:
        signature = []
        for row in rows:
            if _contract(row) != contract_id:
                continue
            signature.append(
                (
                    _position_id(row),
                    str(_first(row, "side", "positionSide", "type", "positionType") or ""),
                    abs(_int(row, "size", "quantity", "qty") or 0),
                    _float(row, "averagePrice", "avgPrice", "average_price"),
                    str(_first(row, "creationTimestamp", "createdAt", "timestamp") or ""),
                )
            )
        return tuple(sorted(signature, key=lambda item: str(item[0])))

    async def _stable_contract_positions(
        self,
        *,
        timeout: float = 3.0,
        consecutive: int = 3,
    ) -> List[Dict[str, Any]]:
        """Require repeated identical contract snapshots after cancellation."""
        assert self.target is not None
        deadline = time.monotonic() + max(0.75, timeout)
        last_signature: Optional[Tuple] = None
        stable_count = 0
        latest: List[Dict[str, Any]] = []
        while time.monotonic() < deadline:
            rows = await self.client.get_positions(self.target.account_id)
            signature = self._position_signature(rows, self.target.contract_id)
            if signature == last_signature:
                stable_count += 1
            else:
                last_signature = signature
                stable_count = 1
            latest = list(rows)
            if stable_count >= max(2, consecutive):
                return latest
            await asyncio.sleep(0.25)
        raise GuardianRetry("Contract position did not reach a stable broker snapshot")

    async def _verify_contract_flat(self, timeout: float = 5.0) -> bool:
        """Confirm flat with repeated snapshots; never trust one empty response."""
        assert self.target is not None
        deadline = time.monotonic() + max(0.5, timeout)
        absent_count = 0
        while time.monotonic() < deadline:
            positions = await self.client.get_positions(self.target.account_id)
            if not any(_contract(row) == self.target.contract_id for row in positions):
                absent_count += 1
                if absent_count >= 3:
                    return True
            else:
                absent_count = 0
            await asyncio.sleep(0.25)
        return False

    async def _failsafe_close_contract(self, reason: str) -> None:
        """Close only the protected contract and prove it is flat."""
        assert self.target is not None and self.store is not None
        LOG.error("FAIL-SAFE CLOSE %s (%s)", self.target.contract_id, reason)
        response = await self.client.close_position(
            self.target.account_id,
            self.target.contract_id,
        )
        if not response.success:
            self.state["status"] = "closing_retry_rejected"
            self.store.save(self.state)
            raise GuardianRetry(
                f"Fail-safe close rejected: {response.error_message or response.error_code}"
            )
        if not await self._verify_contract_flat():
            self.state["status"] = "closing_retry_not_verified"
            self.store.save(self.state)
            raise GuardianRetry("Fail-safe close was accepted but contract is not verified flat")

    async def _exact_owned_double_fill(
        self,
        positions: Iterable[Dict[str, Any]],
    ) -> bool:
        """Prove both owned exits filled and created an equal reverse position."""
        assert self.target is not None
        protected_size = int(self.state.get("protected_size") or self.target.size)
        opposite = "short" if self.target.direction == "long" else "long"
        reverse_positions: List[TargetPosition] = []
        same_contract_count = 0
        for row in positions:
            if _contract(row) != self.target.contract_id:
                continue
            same_contract_count += 1
            try:
                candidate = TargetPosition.from_api(self.target.account_id, row)
            except GuardianError:
                return False
            if candidate.direction == opposite and candidate.size == protected_size:
                reverse_positions.append(candidate)
        if same_contract_count != 1 or len(reverse_positions) != 1:
            return False

        owned_ids = self._owned_ids
        if len(owned_ids) != 2:
            return False
        all_orders = await self.client.get_orders(self.target.account_id)
        by_id = {_order_id(row): row for row in all_orders}
        for order_id in owned_ids:
            order = by_id.get(order_id)
            if not order or _int(order, "status", "orderStatus") != 2:
                return False
            fill_volume = abs(_int(order, "fillVolume", "filledSize", "size") or 0)
            if fill_volume != protected_size:
                return False
        return True

    async def _all_owned_orders_filled(self) -> bool:
        """Return true only when both exact persisted exits fully filled."""
        assert self.target is not None
        protected_size = int(self.state.get("protected_size") or self.target.size)
        owned_ids = self._owned_ids
        if len(owned_ids) != 2:
            return False
        all_orders = await self.client.get_orders(self.target.account_id)
        by_id = {_order_id(row): row for row in all_orders}
        return all(
            order_id in by_id
            and _int(by_id[order_id], "status", "orderStatus") == 2
            and abs(_int(by_id[order_id], "fillVolume", "filledSize", "size") or 0)
            == protected_size
            for order_id in owned_ids
        )

    async def cycle(self) -> bool:
        """Run one poll.  Return False after position close/change."""
        assert self.target is not None and self.store is not None
        positions, orders = await asyncio.gather(
            self.client.get_positions(self.target.account_id),
            self.client.get_open_orders(self.target.account_id),
        )
        current = self._matching_target(positions)
        if current is None:
            # Require three consecutive misses. Two short searchOpen omissions
            # have been observed in practice and must not strip a live position.
            for _ in range(2):
                await asyncio.sleep(0.25)
                confirmed_positions = await self.client.get_positions(self.target.account_id)
                current = self._matching_target(confirmed_positions)
                if current is not None:
                    LOG.warning("Transient position omission; owned exits were left untouched")
                    return True
                positions = confirmed_positions

            ledger_before = await self._owned_fill_snapshot()
            baseline_fills = dict(ledger_before)
            for order in orders:
                order_id = _order_id(order)
                if order_id in self._owned_ids:
                    baseline_fills[order_id] = self._fill_volume(order)
            orders = await self.client.get_open_orders(self.target.account_id)
            await self._cancel_owned_open(orders, "position flat or replaced")

            # Cancellation itself can race a fill. Reconcile exact fill-volume
            # deltas with a stable post-cancel position snapshot before declaring
            # success, including terminal Cancelled orders with partial fills.
            stable_positions = await self._stable_contract_positions()
            after_fills = await self._owned_fill_snapshot()
            protected_size = int(self.state.get("protected_size") or self.target.size)
            fill_after_missing = sum(
                max(0, after_fills.get(order_id, 0) - baseline_fills.get(order_id, 0))
                for order_id in self._owned_ids
            )
            total_owned_fill = sum(after_fills.values())
            suspicious_fill = max(
                int(self.state.get("cleanup_fill_pending") or 0),
                fill_after_missing,
                max(0, total_owned_fill - protected_size),
            )
            if (
                suspicious_fill > 0
                and not any(
                    _contract(row) == self.target.contract_id
                    for row in stable_positions
                )
            ):
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    published = await self.client.get_positions(self.target.account_id)
                    if any(
                        _contract(row) == self.target.contract_id
                        for row in published
                    ):
                        stable_positions = await self._stable_contract_positions()
                        break
                    await asyncio.sleep(0.25)
                else:
                    waits = int(self.state.get("cleanup_fill_waits") or 0) + 1
                    self.state.update(
                        status="recovering_reverse_publish",
                        cleanup_fill_pending=suspicious_fill,
                        cleanup_fill_waits=waits,
                    )
                    self.store.save(self.state)
                    if waits < 2:
                        raise GuardianRetry(
                            "Owned fill occurred during cleanup; waiting for reverse publication"
                        )
                    # Two bounded publication windows plus stable empty snapshots
                    # are treated as confirmed flat rather than an infinite lock.
                    self.state.pop("cleanup_fill_pending", None)
                    self.state.pop("cleanup_fill_waits", None)
            exact_double_fill = await self._exact_owned_double_fill(stable_positions)
            reappeared = self._matching_target(stable_positions)

            reverse_positions: List[TargetPosition] = []
            opposite = "short" if self.target.direction == "long" else "long"
            for row in stable_positions:
                if _contract(row) != self.target.contract_id:
                    continue
                try:
                    candidate = TargetPosition.from_api(self.target.account_id, row)
                except GuardianError:
                    continue
                if candidate.direction == opposite:
                    reverse_positions.append(candidate)

            attributable_reverse = next(
                (
                    item
                    for item in reverse_positions
                    if exact_double_fill
                    or item.size <= fill_after_missing
                    or item.size <= max(0, total_owned_fill - protected_size)
                ),
                None,
            )

            if reappeared is not None:
                await self._failsafe_close_contract(
                    "position reappeared after its exits were cancelled"
                )
                status = "finished_reappeared_unprotected_flattened"
            elif attributable_reverse is not None:
                await self._failsafe_close_contract(
                    "owned exit fill during cleanup created a reverse position"
                )
                # A final scan/cancel is intentionally restricted to persisted
                # owned IDs (normally already filled and absent here).
                final_orders = await self.client.get_open_orders(self.target.account_id)
                await self._cancel_owned_open(final_orders, "post double-fill close")
                status = "finished_double_fill_flattened"
            elif any(_contract(row) == self.target.contract_id for row in stable_positions):
                # A different same-contract position without an attributable
                # owned fill is treated as a new discretionary position. Release
                # this old state so the engine can create a new exact guardian.
                status = "finished_position_replaced_external"
            else:
                status = "finished_position_gone"
            self.state["status"] = status
            self.store.save(self.state)
            LOG.warning("Target position is gone/replaced; owned sibling orders are cleared")
            return False

        self._accept_live_target_update(current)

        by_id = {_order_id(order): order for order in orders}
        sl_id = int(self.state.get("sl_order_id") or 0)
        tp_id = int(self.state.get("tp_order_id") or 0)
        if not sl_id and not tp_id:
            self.state["status"] = "closing_retry_unprotected"
            self.store.save(self.state)
            await self._failsafe_close_contract("no owned protection remains")
            self.state["status"] = "finished_unprotected_flattened"
            self.store.save(self.state)
            return False

        # Never recreate a missing order from a possibly stale position snapshot:
        # first prove the omission is not one stale searchOpen response.
        if (sl_id and sl_id not in by_id) or (tp_id and tp_id not in by_id):
            missing = "SL" if sl_id and sl_id not in by_id else "TP"
            missing_id = sl_id if missing == "SL" else tp_id
            await asyncio.sleep(0.25)
            confirmed_orders = await self.client.get_open_orders(self.target.account_id)
            confirmed_by_id = {_order_id(order): order for order in confirmed_orders}
            if missing_id in confirmed_by_id:
                self._validate_owned_order(
                    confirmed_by_id[missing_id],
                    kind=missing,
                    size=current.size,
                )
                LOG.warning("Transient searchOpen omission for %s #%s; no action", missing, missing_id)
                return True

            # searchOpen can remain stale.  Require the full order ledger to
            # publish a terminal state before touching either leg.  Open (1),
            # Pending (6), missing, or unknown status means wait for next poll.
            all_orders = await self.client.get_orders(self.target.account_id)
            ledger_order = next(
                (row for row in all_orders if _order_id(row) == missing_id),
                None,
            )
            ledger_status = _int(ledger_order or {}, "status", "orderStatus")
            missing_fill = self._fill_volume(ledger_order or {})
            if ledger_status not in {2, 3, 4, 5}:
                check_key = f"missing_order_{missing_id}_checks"
                checks = int(self.state.get(check_key) or 0) + 1
                self.state[check_key] = checks
                self.store.save(self.state)
                LOG.warning(
                    "%s #%s absent from searchOpen but ledger status=%s; "
                    "reconciliation check %s",
                    missing,
                    missing_id,
                    ledger_status,
                    checks,
                )
                if checks < 3:
                    return True
                disposition = await self._cancel_owned_order_verified(
                    missing_id,
                    missing,
                    "accepted order never published",
                    request_cancel=True,
                )
                if disposition.state == "cancelled":
                    self.state[
                        "sl_order_id" if missing == "SL" else "tp_order_id"
                    ] = None
                    self.store.save(self.state)
                missing_fill = max(missing_fill, disposition.fill_volume)
                ledger_status = 2 if disposition.state != "cancelled" else 3
            self.state.pop(f"missing_order_{missing_id}_checks", None)
            self.store.save(self.state)

            await self._cancel_owned_open(confirmed_orders, f"{missing} terminal status={ledger_status}")
            # Position and open-order snapshots can publish out of order.  After
            # clearing the sibling, query the position again.  If it genuinely
            # remains, close the contract rather than leave it unprotected or
            # risk recreating an exit whose original fill is merely delayed.
            latest_positions = await self._stable_contract_positions()
            exact_double = await self._exact_owned_double_fill(latest_positions)
            opposite = "short" if self.target.direction == "long" else "long"
            attributable_reverse = False
            for row in latest_positions:
                if _contract(row) != self.target.contract_id:
                    continue
                try:
                    candidate = TargetPosition.from_api(self.target.account_id, row)
                except GuardianError:
                    continue
                if candidate.direction == opposite and candidate.size <= missing_fill:
                    attributable_reverse = True
            if self._matching_target(latest_positions) is not None:
                await self._failsafe_close_contract(f"{missing} disappeared while position remained")
                self.state["status"] = f"finished_{missing.lower()}_missing_flattened"
            elif exact_double or attributable_reverse:
                await self._failsafe_close_contract(
                    f"{missing} terminal fill created a reverse position"
                )
                self.state["status"] = f"finished_{missing.lower()}_reverse_flattened"
            elif any(_contract(row) == self.target.contract_id for row in latest_positions):
                self.state["status"] = f"finished_{missing.lower()}_position_replaced_external"
            else:
                self.state["status"] = f"finished_{missing.lower()}_missing"
            self.store.save(self.state)
            LOG.warning("%s is no longer open; sibling cleared to prevent double fill", missing)
            return False

        protected_size = int(self.state.get("protected_size") or self.target.size)
        if sl_id and not tp_id:
            self._validate_owned_order(by_id[sl_id], kind="SL", size=protected_size)
            if current.size != protected_size:
                return await self._resize(current, orders)
            if time.monotonic() - self._last_tp_retry_ts >= 5.0:
                self._last_tp_retry_ts = time.monotonic()
                try:
                    await self._revalidate_position_before_tp(current.size)
                    await self._place_tp(current.size)
                except GuardianRetry:
                    raise
                except GuardianError as exc:
                    if str(self.state.get("status") or "").startswith("finished_"):
                        raise
                    self.state["status"] = "guarding_sl_only"
                    self.store.save(self.state)
                    LOG.error("TP retry failed (%s); confirmed SL remains guarded", exc)
            return True

        if tp_id and not sl_id:
            self._validate_owned_order(by_id[tp_id], kind="TP", size=protected_size)
            if current.size != protected_size:
                disposition = await self._cancel_owned_order_verified(
                    tp_id,
                    "TP",
                    "TP-only position size changed",
                    request_cancel=True,
                )
                if disposition.state != "cancelled":
                    raise GuardianRetry(
                        f"TP {disposition.state} x{disposition.fill_volume} while TP-only "
                        "recovery was resolving"
                    )
                self.state["tp_order_id"] = None
                self.store.save(self.state)
                return True
            try:
                await self._place_sl(current.size)
                await self._revalidate_position_before_tp(current.size)
            except GuardianRetry:
                raise
            except GuardianError:
                latest = await self.client.get_open_orders(self.target.account_id)
                await self._cancel_owned_open(latest, "TP-only recovery failed")
                self.state["status"] = "closing_retry_tp_only"
                self.store.save(self.state)
                await self._failsafe_close_contract("could not establish downside SL")
                self.state["status"] = "finished_tp_only_flattened"
                self.store.save(self.state)
                return False
            self.state.update(status="guarding", protected_size=current.size)
            self.store.save(self.state)
            return True

        if current.size != protected_size:
            if not await self._resize(current, orders):
                return False
        else:
            self._validate_owned_order(by_id[sl_id], kind="SL", size=current.size)
            self._validate_owned_order(by_id[tp_id], kind="TP", size=current.size)
        return True

    async def run(self) -> None:
        await self.prepare()
        if not self.execute:
            orders = await self.client.get_open_orders(self.target.account_id)  # type: ignore[union-attr]
            self._assert_no_unowned_conflicts(orders)
            by_id = {_order_id(row): row for row in orders}
            for order_id, kind in (
                (self.adopt_sl_order_id, "SL"),
                (self.adopt_tp_order_id, "TP"),
            ):
                if order_id is None:
                    continue
                adopted = by_id.get(order_id)
                if adopted is None:
                    raise GuardianError(
                        f"Explicit {kind} #{order_id} is not currently open"
                    )
                self._validate_owned_order(
                    adopted,
                    kind=kind,
                    size=self.target.size,  # type: ignore[union-attr]
                )
                LOG.info("Explicit %s #%s passed exact validation", kind, order_id)
            LOG.info("Dry run complete: no order was placed, modified, or cancelled")
            return

        assert self.store is not None
        assert self.target is not None
        self.lock = InstanceLock(
            self.store.path.parent / f"account_{self.target.account_id}.guardian.lock",
            metadata={
                "account_id": self.target.account_id,
                "position_id": self.target.position_id,
                "contract_id": self.target.contract_id,
                "state_path": str(self.store.path.resolve()),
            },
        )
        self.lock.acquire()
        try:
            self.store.save(self.state)
            if self._recovery_without_position:
                try:
                    if not await self.cycle():
                        return
                except (GuardianError, GuardianRetry) as exc:
                    LOG.error("Recovery startup is still ambiguous (%s); retaining ownership", exc)
            else:
                try:
                    await self.arm()
                except GuardianRetry as exc:
                    self.state["status"] = "recovering_arm_retry"
                    self.store.save(self.state)
                    LOG.error("Arm requires retry (%s); guardian remains active", exc)
                except GuardianError as exc:
                    if "Unknown close-side" in str(exc):
                        self.state["status"] = "blocked_external_conflict"
                        self.store.save(self.state)
                        raise
                    if self._owned_ids:
                        self.state["status"] = "recovering_arm_owned"
                    else:
                        self.state["status"] = "closing_retry_unprotected"
                    self.store.save(self.state)
                    LOG.error("Arm failed (%s); guardian remains active for safe recovery", exc)

            if str(self.state.get("status") or "").startswith("finished_"):
                return
            while not self._stop:
                started = time.monotonic()
                self.lock.heartbeat()
                try:
                    if not await self.cycle():
                        return
                except GuardianRetry as exc:
                    LOG.error("Safety state is ambiguous (%s); retaining ownership and retrying", exc)
                except GuardianError as exc:
                    status = str(self.state.get("status") or "")
                    if status.startswith("finished_") or status.startswith("blocked_"):
                        raise
                    self.state["status"] = "blocked_validation_retry"
                    self.store.save(self.state)
                    LOG.error("Validation blocked (%s); retaining ownership and retrying", exc)
                except Exception as exc:
                    # Preserve existing exits during transient API failures and keep polling.
                    LOG.error("Poll failed (%s); owned orders were left untouched", type(exc).__name__)
                elapsed = time.monotonic() - started
                await asyncio.sleep(max(0.0, self.poll_seconds - elapsed))
        finally:
            self.lock.release()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Protect one existing TopstepX position with software-managed SL/TP",
    )
    parser.add_argument("--account-id", type=int, help="Account id; omit to scan accounts")
    parser.add_argument("--contract-id", help="Exact contract id; omit only when one position exists")
    parser.add_argument("--position-id", type=int, help="Exact position id; omit only when one position exists")
    parser.add_argument("--sl", type=float, required=True, help="Fixed stop price")
    parser.add_argument("--tp", type=float, required=True, help="Fixed target price")
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=2.5,
        help="Polling cadence, clamped to at least 1.0s for API rate-limit headroom",
    )
    parser.add_argument("--confirm-timeout", type=float, default=5.0)
    parser.add_argument("--state-file", type=Path)
    parser.add_argument(
        "--adopt-sl-order-id",
        type=int,
        help="Explicitly adopt one already-placed SL by exact id after full validation",
    )
    parser.add_argument(
        "--adopt-tp-order-id",
        type=int,
        help="Explicitly adopt one already-placed TP by exact id after full validation",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true", help="Actually place/manage orders")
    mode.add_argument("--dry-run", action="store_true", help="Read-only inspection (default)")
    return parser


async def _async_main(args: argparse.Namespace) -> int:
    load_dotenv(ROOT / ".env")
    username = os.getenv("TOPSTEPX_USERNAME", "").strip()
    api_key = os.getenv("TOPSTEPX_API_KEY", "").strip()
    if not username or not api_key:
        raise GuardianError("Missing TOPSTEPX_USERNAME or TOPSTEPX_API_KEY in .env")
    use_demo = os.getenv("TOPSTEPX_USE_DEMO", "").strip().lower() in {"1", "true", "yes", "on"}
    base_url = os.getenv("TOPSTEPX_BASE_URL", "").strip() or None
    client = TopstepXClient(
        username=username,
        api_key=api_key,
        base_url=base_url,
        use_demo=use_demo,
    )
    guardian = PositionGuardian(
        client,
        account_id=args.account_id,
        contract_id=args.contract_id,
        position_id=args.position_id,
        sl_price=args.sl,
        tp_price=args.tp,
        execute=bool(args.execute),
        adopt_sl_order_id=args.adopt_sl_order_id,
        adopt_tp_order_id=args.adopt_tp_order_id,
        poll_seconds=args.poll_seconds,
        confirm_timeout=args.confirm_timeout,
        state_path=args.state_file,
    )
    try:
        await guardian.run()
        return 0
    finally:
        http = getattr(client, "_http", None)
        if http is not None and not http.is_closed:
            await http.aclose()


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # The broker client logs every REST poll at INFO.  At a one-second guard
    # cadence that would grow the redirected log needlessly; retain only the
    # guardian's state transitions and broker warnings/errors.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("backend.broker.topstepx").setLevel(logging.WARNING)
    args = _parser().parse_args(argv)
    try:
        return asyncio.run(_async_main(args))
    except KeyboardInterrupt:
        LOG.warning("Guardian stopped by operator; live owned exits were left in place")
        return 130
    except GuardianError as exc:
        LOG.error("SAFE STOP: %s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
