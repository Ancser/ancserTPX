from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.db.models import OrderResponse
from scripts.manual_position_guardian import (
    GuardianError,
    GuardianRetry,
    InstanceLock,
    PositionGuardian,
    _parser,
)


POSITION = {
    "id": 7001,
    "accountId": 123,
    "contractId": "CON.F.US.MNQ.U26",
    "type": 1,
    "size": 3,
    "averagePrice": 29062.0,
    "creationTimestamp": "2026-07-17T00:53:22Z",
}


class FakeClient:
    def __init__(self):
        self.positions = [dict(POSITION)]
        self.orders = []
        self.order_history = []
        self.next_id = 9000
        self.events = []

    async def get_accounts(self):
        self.events.append("get_accounts")
        return [{"id": 123}]

    async def get_positions(self, account_id):
        self.events.append("get_positions")
        return [dict(row) for row in self.positions]

    async def get_open_orders(self, account_id):
        self.events.append("get_open_orders")
        return [dict(row) for row in self.orders]

    async def get_orders(self, account_id):
        self.events.append("get_orders")
        return [dict(row) for row in self.order_history + self.orders]

    async def place_order(self, request):
        self.next_id += 1
        kind = "sl" if request.order_type == 3 else "tp"
        self.events.append(f"place_{kind}")
        row = {
            "id": self.next_id,
            "contractId": request.contract_id,
            "side": 0 if request.side == 1 else 1,
            "type": 4 if request.order_type == 3 else request.order_type,
            "size": request.size,
            "stopPrice": request.stop_price,
            "limitPrice": request.limit_price,
        }
        self.orders.append(row)
        return OrderResponse(order_id=self.next_id, success=True)

    async def cancel_order(self, account_id, order_id):
        self.events.append(f"cancel_{order_id}")
        for row in self.orders:
            if row["id"] == order_id:
                cancelled = dict(row)
                cancelled["status"] = 3
                self.order_history.append(cancelled)
        self.orders = [row for row in self.orders if row["id"] != order_id]
        return True

    async def modify_order(self, account_id, order_id, **changes):
        self.events.append(f"modify_{order_id}")
        for row in self.orders:
            if row["id"] != order_id:
                continue
            if changes.get("size") is not None:
                row["size"] = changes["size"]
            if changes.get("stop_price") is not None:
                row["stopPrice"] = changes["stop_price"]
            if changes.get("limit_price") is not None:
                row["limitPrice"] = changes["limit_price"]
            return OrderResponse(order_id=order_id, success=True)
        return OrderResponse(order_id=order_id, success=False, error_message="not found")

    async def close_position(self, account_id, contract_id):
        self.events.append(f"close_{contract_id}")
        self.positions = [row for row in self.positions if row["contractId"] != contract_id]
        return OrderResponse(order_id=0, success=True)


def guardian(client: FakeClient, tmp_path: Path, **kwargs) -> PositionGuardian:
    return PositionGuardian(
        client,
        account_id=123,
        contract_id="CON.F.US.MNQ.U26",
        position_id=7001,
        sl_price=28987.50,
        tp_price=29285.25,
        execute=True,
        state_path=tmp_path / "guardian.json",
        poll_seconds=0.2,
        confirm_timeout=0.5,
        **kwargs,
    )


def test_stop_is_confirmed_before_target_is_placed(tmp_path):
    async def scenario():
        client = FakeClient()
        item = guardian(client, tmp_path)
        await item.prepare()
        item.store.save(item.state)
        await item.arm()

        assert [row["type"] for row in client.orders] == [4, 1]
        sl_place = client.events.index("place_sl")
        tp_place = client.events.index("place_tp")
        assert "get_open_orders" in client.events[sl_place + 1 : tp_place]
        assert item.state["status"] == "guarding"
        assert item.state["sl_order_id"]
        assert item.state["tp_order_id"]

    asyncio.run(scenario())


def test_position_change_after_sl_confirmation_retains_sl_then_recovers_tp(tmp_path):
    async def scenario():
        client = FakeClient()
        original_get_positions = client.get_positions
        calls = 0

        async def changed_after_prepare(account_id):
            nonlocal calls
            calls += 1
            rows = await original_get_positions(account_id)
            if calls >= 2:
                rows[0]["averagePrice"] = POSITION["averagePrice"] + 1.0
            return rows

        client.get_positions = changed_after_prepare
        item = guardian(client, tmp_path)
        await item.prepare()
        item.store.save(item.state)

        with pytest.raises(GuardianRetry, match="retaining SL"):
            await item.arm()

        assert calls == 3  # prepare + two confirmation snapshots
        assert client.events.count("place_sl") == 1
        assert "place_tp" not in client.events
        assert not any(event.startswith("cancel_") for event in client.events)
        assert len(client.orders) == 1
        assert item.state["sl_order_id"] is not None
        assert item.state["tp_order_id"] is None

        assert await item.cycle() is True
        assert len(client.orders) == 2
        assert item.state["status"] == "guarding"
        assert item.state["average_price"] == POSITION["averagePrice"] + 1.0

    asyncio.run(scenario())


def test_explicit_existing_stop_is_validated_and_not_duplicated(tmp_path):
    async def scenario():
        client = FakeClient()
        client.orders.append(
            {
                "id": 3282311480,
                "contractId": "CON.F.US.MNQ.U26",
                "side": 1,
                "type": 4,
                "size": 3,
                "stopPrice": 28987.50,
            }
        )
        item = guardian(client, tmp_path, adopt_sl_order_id=3282311480)
        await item.prepare()
        item.store.save(item.state)
        await item.arm()

        assert "place_sl" not in client.events
        assert client.events.count("place_tp") == 1
        assert item.state["sl_order_id"] == 3282311480

    asyncio.run(scenario())


def test_explicit_existing_target_is_validated_and_not_duplicated(tmp_path):
    async def scenario():
        client = FakeClient()
        client.orders.append(
            {
                "id": 3282311481,
                "contractId": "CON.F.US.MNQ.U26",
                "side": 1,
                "type": 1,
                "size": 3,
                "limitPrice": 29285.25,
            }
        )
        item = guardian(client, tmp_path, adopt_tp_order_id=3282311481)
        await item.prepare()
        item.store.save(item.state)
        await item.arm()

        assert client.events.count("place_sl") == 1
        assert "place_tp" not in client.events
        assert item.state["tp_order_id"] == 3282311481
        assert item.state["status"] == "guarding"

    asyncio.run(scenario())


def test_explicit_existing_pair_is_adopted_without_new_orders(tmp_path):
    async def scenario():
        client = FakeClient()
        client.orders.extend(
            [
                {
                    "id": 3282311480,
                    "contractId": "CON.F.US.MNQ.U26",
                    "side": 1,
                    "type": 4,
                    "size": 3,
                    "stopPrice": 28987.50,
                },
                {
                    "id": 3282311481,
                    "contractId": "CON.F.US.MNQ.U26",
                    "side": 1,
                    "type": 1,
                    "size": 3,
                    "limitPrice": 29285.25,
                },
            ]
        )
        item = guardian(
            client,
            tmp_path,
            adopt_sl_order_id=3282311480,
            adopt_tp_order_id=3282311481,
        )
        await item.prepare()
        item.store.save(item.state)
        await item.arm()

        assert not any(event.startswith("place_") for event in client.events)
        assert item.state["sl_order_id"] == 3282311480
        assert item.state["tp_order_id"] == 3282311481
        assert item.state["status"] == "guarding"

    asyncio.run(scenario())


def test_explicit_target_must_match_type_side_size_contract_and_price(tmp_path):
    async def scenario():
        client = FakeClient()
        client.orders.append(
            {
                "id": 3282311481,
                "contractId": "CON.F.US.MNQ.U26",
                "side": 1,
                "type": 1,
                "size": 3,
                "limitPrice": 29280.00,
            }
        )
        item = guardian(client, tmp_path, adopt_tp_order_id=3282311481)
        await item.prepare()
        item.store.save(item.state)

        with pytest.raises(GuardianError, match="Owned TP order no longer matches"):
            await item.arm()
        assert not any(event.startswith("place_") for event in client.events)
        assert not any(event.startswith("cancel_") for event in client.events)

    asyncio.run(scenario())


def test_explicit_target_must_match_persisted_owned_id(tmp_path):
    async def scenario():
        client = FakeClient()
        first = guardian(client, tmp_path)
        await first.prepare()
        first.state["tp_order_id"] = 3282311481
        first.store.save(first.state)

        resumed = guardian(client, tmp_path, adopt_tp_order_id=3282311482)
        with pytest.raises(
            GuardianError,
            match="--adopt-tp-order-id differs from the persisted owned TP id",
        ):
            await resumed.prepare()

    asyncio.run(scenario())


def test_explicit_order_ids_are_positive_distinct_and_cli_exposed(tmp_path):
    client = FakeClient()
    with pytest.raises(GuardianError, match="--adopt-tp-order-id must be a positive integer"):
        guardian(client, tmp_path, adopt_tp_order_id=0)
    with pytest.raises(GuardianError, match="must be distinct"):
        guardian(
            client,
            tmp_path,
            adopt_sl_order_id=3282311480,
            adopt_tp_order_id=3282311480,
        )

    args = _parser().parse_args(
        [
            "--account-id", "123",
            "--sl", "28987.50",
            "--tp", "29285.25",
            "--adopt-tp-order-id", "3282311481",
        ]
    )
    assert args.adopt_tp_order_id == 3282311481


def test_flat_cancels_only_owned_sibling_orders(tmp_path):
    async def scenario():
        client = FakeClient()
        item = guardian(client, tmp_path)
        await item.prepare()
        item.store.save(item.state)
        await item.arm()
        owned = set(item._owned_ids)
        client.orders.append(
            {
                "id": 777777,
                "contractId": "CON.F.US.MES.U26",
                "side": 1,
                "type": 1,
                "size": 1,
                "limitPrice": 6000,
            }
        )
        client.positions = []

        assert await item.cycle() is False
        cancelled = {int(event.split("_")[1]) for event in client.events if event.startswith("cancel_")}
        assert cancelled == owned
        assert any(row["id"] == 777777 for row in client.orders)

    asyncio.run(scenario())


def test_size_decrease_cancels_tp_then_resizes_sl_and_rebuilds_tp(tmp_path):
    async def scenario():
        client = FakeClient()
        item = guardian(client, tmp_path)
        await item.prepare()
        item.store.save(item.state)
        await item.arm()
        old_tp = item.state["tp_order_id"]
        sl_id = item.state["sl_order_id"]
        client.events.clear()
        client.positions[0]["size"] = 2

        assert await item.cycle() is True
        assert client.events.index(f"cancel_{old_tp}") < client.events.index(f"modify_{sl_id}")
        assert client.events.index(f"modify_{sl_id}") < client.events.index("place_tp")
        assert item.state["protected_size"] == 2
        assert all(row["size"] == 2 for row in client.orders)

    asyncio.run(scenario())


def test_unknown_close_side_order_blocks_fresh_guardian(tmp_path):
    async def scenario():
        client = FakeClient()
        client.orders.append(
            {
                "id": 111,
                "contractId": "CON.F.US.MNQ.U26",
                "side": 1,
                "type": 4,
                "size": 3,
                "stopPrice": 28980,
            }
        )
        item = guardian(client, tmp_path)
        await item.prepare()
        item.store.save(item.state)
        with pytest.raises(GuardianError, match="Unknown close-side"):
            await item.arm()
        assert not any(event.startswith("place_") for event in client.events)

    asyncio.run(scenario())


def test_exact_owned_double_fill_closes_reverse_and_verifies_flat(tmp_path):
    async def scenario():
        client = FakeClient()
        item = guardian(client, tmp_path)
        await item.prepare()
        item.store.save(item.state)
        await item.arm()
        for row in client.orders:
            filled = dict(row)
            filled.update(status=2, fillVolume=3)
            client.order_history.append(filled)
        client.orders = []
        client.positions = [
            {
                **POSITION,
                "id": 7002,
                "type": 2,
                "creationTimestamp": "2026-07-17T01:00:00Z",
            }
        ]

        assert await item.cycle() is False
        assert f"close_{POSITION['contractId']}" in client.events
        assert client.positions == []
        assert item.state["status"] == "finished_double_fill_flattened"

    asyncio.run(scenario())


def test_missing_leg_rechecks_then_failsafe_closes_remaining_position(tmp_path):
    async def scenario():
        client = FakeClient()
        item = guardian(client, tmp_path)
        await item.prepare()
        item.store.save(item.state)
        await item.arm()
        tp_id = item.state["tp_order_id"]
        terminal_tp = next(dict(row) for row in client.orders if row["id"] == tp_id)
        terminal_tp["status"] = 3
        client.order_history.append(terminal_tp)
        client.orders = [row for row in client.orders if row["id"] != tp_id]

        assert await item.cycle() is False
        assert f"close_{POSITION['contractId']}" in client.events
        assert client.positions == []
        assert item.state["status"] == "finished_tp_missing_flattened"

    asyncio.run(scenario())


def test_one_transient_open_order_omission_does_not_cancel_or_flatten(tmp_path):
    async def scenario():
        client = FakeClient()
        item = guardian(client, tmp_path)
        await item.prepare()
        item.store.save(item.state)
        await item.arm()
        tp_id = item.state["tp_order_id"]
        original_get_open_orders = client.get_open_orders
        calls = 0

        async def transient_get_open_orders(account_id):
            nonlocal calls
            calls += 1
            rows = await original_get_open_orders(account_id)
            if calls == 1:
                return [row for row in rows if row["id"] != tp_id]
            return rows

        client.get_open_orders = transient_get_open_orders
        client.events.clear()

        assert await item.cycle() is True
        assert not any(event.startswith("cancel_") for event in client.events)
        assert not any(event.startswith("close_") for event in client.events)
        assert client.positions
        assert len(client.orders) == 2

    asyncio.run(scenario())


def test_resume_accepts_same_creation_scale_average_without_mutation(tmp_path):
    async def scenario():
        client = FakeClient()
        first = guardian(client, tmp_path)
        await first.prepare()
        first.store.save(first.state)

        client.positions[0]["averagePrice"] = 29063.0
        resumed = guardian(client, tmp_path)
        await resumed.prepare()

        assert not any(
            event.startswith(("place_", "cancel_", "modify_", "close_"))
            for event in client.events
        )
        assert resumed.state["average_price"] == 29063.0

    asyncio.run(scenario())


def test_one_transient_position_omission_keeps_both_exits(tmp_path):
    async def scenario():
        client = FakeClient()
        item = guardian(client, tmp_path)
        await item.prepare()
        item.store.save(item.state)
        await item.arm()
        original = client.get_positions
        calls = 0

        async def transient_positions(account_id):
            nonlocal calls
            calls += 1
            if calls == 1:
                return []
            return await original(account_id)

        client.get_positions = transient_positions
        client.events.clear()

        assert await item.cycle() is True
        assert len(client.orders) == 2
        assert not any(event.startswith(("cancel_", "close_")) for event in client.events)

    asyncio.run(scenario())


def test_cancel_failure_retains_exact_ownership_for_retry(tmp_path):
    async def scenario():
        client = FakeClient()
        item = guardian(client, tmp_path)
        await item.prepare()
        item.store.save(item.state)
        await item.arm()
        owned = set(item._owned_ids)
        client.positions = []

        async def never_cancel(account_id, order_id):
            client.events.append(f"cancel_{order_id}")
            return False

        client.cancel_order = never_cancel
        with pytest.raises(GuardianRetry, match="retaining ownership"):
            await item.cycle()

        assert item._owned_ids == owned
        assert {row["id"] for row in client.orders} == owned

    asyncio.run(scenario())


def test_resume_with_changed_size_resizes_before_rebuilding_target(tmp_path):
    async def scenario():
        client = FakeClient()
        first = guardian(client, tmp_path)
        await first.prepare()
        first.store.save(first.state)
        await first.arm()
        old_tp = first.state["tp_order_id"]
        sl_id = first.state["sl_order_id"]
        client.positions[0]["size"] = 2
        client.events.clear()

        resumed = guardian(client, tmp_path)
        await resumed.prepare()
        await resumed.arm()

        assert f"cancel_{old_tp}" in client.events
        assert f"modify_{sl_id}" in client.events
        assert resumed.state["protected_size"] == 2
        assert all(row["size"] == 2 for row in client.orders)

    asyncio.run(scenario())


def test_pre_tp_requires_two_consecutive_exact_position_snapshots(tmp_path):
    async def scenario():
        client = FakeClient()
        item = guardian(client, tmp_path)
        await item.prepare()
        item.store.save(item.state)
        original = client.get_positions
        calls = 0

        async def changes_on_second_confirmation(account_id):
            nonlocal calls
            calls += 1
            rows = await original(account_id)
            if calls >= 2:
                rows[0]["averagePrice"] += 1.0
            return rows

        client.get_positions = changes_on_second_confirmation
        with pytest.raises(GuardianRetry, match="retaining SL"):
            await item.arm()

        assert "place_tp" not in client.events
        assert len(client.orders) == 1
        assert client.orders[0]["type"] == 4

    asyncio.run(scenario())


def test_flat_restart_recovers_and_cancels_owned_orphan(tmp_path):
    async def scenario():
        client = FakeClient()
        first = guardian(client, tmp_path)
        await first.prepare()
        first.store.save(first.state)
        await first.arm()
        tp_id = first.state["tp_order_id"]
        sl_id = first.state["sl_order_id"]
        filled_tp = next(dict(row) for row in client.orders if row["id"] == tp_id)
        filled_tp.update(status=2, fillVolume=3)
        client.order_history.append(filled_tp)
        client.orders = [row for row in client.orders if row["id"] != tp_id]
        client.positions = []
        client.events.clear()

        resumed = guardian(client, tmp_path)
        await resumed.prepare()
        assert resumed._recovery_without_position is True
        assert await resumed.cycle() is False

        assert f"cancel_{sl_id}" in client.events
        assert client.orders == []
        assert resumed.state["status"] == "finished_position_gone"

    asyncio.run(scenario())


def test_resize_modify_failure_clears_exits_then_flattens(tmp_path):
    async def scenario():
        client = FakeClient()
        item = guardian(client, tmp_path)
        await item.prepare()
        item.store.save(item.state)
        await item.arm()
        client.positions[0]["size"] = 2

        async def reject_modify(account_id, order_id, **changes):
            return OrderResponse(order_id=order_id, success=False, error_message="rejected")

        client.modify_order = reject_modify
        assert await item.cycle() is False

        assert client.positions == []
        assert client.orders == []
        assert item.state["status"] == "finished_resize_failed_flattened"

    asyncio.run(scenario())


def test_restart_missing_persisted_leg_enters_cycle_recovery(tmp_path):
    async def scenario():
        client = FakeClient()
        first = guardian(client, tmp_path)
        await first.prepare()
        first.store.save(first.state)
        await first.arm()
        tp_id = first.state["tp_order_id"]
        missing_tp = next(dict(row) for row in client.orders if row["id"] == tp_id)
        missing_tp["status"] = 3
        client.order_history.append(missing_tp)
        client.orders = [row for row in client.orders if row["id"] != tp_id]

        resumed = guardian(client, tmp_path)
        await resumed.run()

        assert client.positions == []
        assert client.orders == []
        assert resumed.state["status"] == "finished_tp_missing_flattened"

    asyncio.run(scenario())


def test_two_transient_position_omissions_do_not_strip_live_exits(tmp_path):
    async def scenario():
        client = FakeClient()
        item = guardian(client, tmp_path)
        await item.prepare()
        item.store.save(item.state)
        await item.arm()
        original = client.get_positions
        calls = 0

        async def omitted_twice(account_id):
            nonlocal calls
            calls += 1
            if calls <= 2:
                return []
            return await original(account_id)

        client.get_positions = omitted_twice
        client.events.clear()

        assert await item.cycle() is True
        assert len(client.orders) == 2
        assert not any(event.startswith(("cancel_", "close_")) for event in client.events)

    asyncio.run(scenario())


def test_flat_verification_rejects_one_empty_snapshot(tmp_path):
    async def scenario():
        client = FakeClient()
        item = guardian(client, tmp_path)
        await item.prepare()
        calls = 0

        async def transient_empty(account_id):
            nonlocal calls
            calls += 1
            if calls == 1:
                return []
            return [dict(POSITION)]

        client.get_positions = transient_empty

        assert await item._verify_contract_flat(timeout=0.8) is False
        assert calls >= 2

    asyncio.run(scenario())


def test_fill_during_flat_sibling_cancel_closes_attributable_reverse(tmp_path):
    async def scenario():
        client = FakeClient()
        item = guardian(client, tmp_path)
        await item.prepare()
        item.store.save(item.state)
        await item.arm()
        tp_id = item.state["tp_order_id"]
        original_cancel = client.cancel_order
        client.positions = []

        async def fill_tp_while_cancelling(account_id, order_id):
            if order_id != tp_id:
                return await original_cancel(account_id, order_id)
            row = next(row for row in client.orders if row["id"] == order_id)
            filled = {**row, "status": 2, "fillVolume": 3}
            client.order_history.append(filled)
            client.orders = [row for row in client.orders if row["id"] != order_id]
            client.positions = [
                {
                    **POSITION,
                    "id": 7002,
                    "type": 2,
                    "size": 3,
                    "creationTimestamp": "2026-07-17T01:00:00Z",
                }
            ]
            return False

        client.cancel_order = fill_tp_while_cancelling

        assert await item.cycle() is False
        assert client.positions == []
        assert item.state["status"] == "finished_double_fill_flattened"
        assert f"close_{POSITION['contractId']}" in client.events

    asyncio.run(scenario())


def test_partial_fill_reported_cancelled_during_cleanup_closes_reverse(tmp_path):
    async def scenario():
        client = FakeClient()
        item = guardian(client, tmp_path)
        await item.prepare()
        item.store.save(item.state)
        await item.arm()
        tp_id = item.state["tp_order_id"]
        original_cancel = client.cancel_order
        client.positions = []

        async def partial_tp_while_cancelling(account_id, order_id):
            if order_id != tp_id:
                return await original_cancel(account_id, order_id)
            row = next(row for row in client.orders if row["id"] == order_id)
            terminal = {**row, "status": 3, "fillVolume": 1}
            client.order_history.append(terminal)
            client.orders = [row for row in client.orders if row["id"] != order_id]
            client.positions = [
                {
                    **POSITION,
                    "id": 7002,
                    "type": 2,
                    "size": 1,
                    "creationTimestamp": "2026-07-17T01:00:00Z",
                }
            ]
            return True

        client.cancel_order = partial_tp_while_cancelling

        assert await item.cycle() is False
        assert client.positions == []
        assert item.state["status"] == "finished_double_fill_flattened"

    asyncio.run(scenario())


def test_restart_recovers_crash_after_sl_resize_before_state_save(tmp_path):
    async def scenario():
        client = FakeClient()
        first = guardian(client, tmp_path)
        await first.prepare()
        first.store.save(first.state)
        await first.arm()
        tp_id = first.state["tp_order_id"]
        sl_id = first.state["sl_order_id"]

        await client.cancel_order(123, tp_id)
        await client.modify_order(123, sl_id, size=2, stop_price=first.sl_price)
        client.positions[0]["size"] = 2
        assert first.state["protected_size"] == 3
        assert first.state["tp_order_id"] == tp_id

        resumed = guardian(client, tmp_path)
        await resumed.prepare()
        await resumed.arm()

        assert resumed.state["protected_size"] == 2
        assert resumed.state["tp_order_id"] != tp_id
        assert all(row["size"] == 2 for row in client.orders)

    asyncio.run(scenario())


def test_scale_in_size_and_average_rebuilds_complete_pair(tmp_path):
    async def scenario():
        client = FakeClient()
        item = guardian(client, tmp_path)
        await item.prepare()
        item.store.save(item.state)
        await item.arm()
        client.positions[0].update(size=4, averagePrice=29063.0)

        assert await item.cycle() is True

        assert item.state["protected_size"] == 4
        assert item.state["average_price"] == 29063.0
        assert len(client.orders) == 2
        assert all(row["size"] == 4 for row in client.orders)

    asyncio.run(scenario())


def test_os_account_lock_blocks_second_owner_and_recovers_stale_metadata(tmp_path):
    lock_path = tmp_path / "account_123.guardian.lock"
    first = InstanceLock(lock_path, metadata={"position_id": 7001})
    second = InstanceLock(lock_path, metadata={"position_id": 7002})
    first.acquire()
    try:
        with pytest.raises(GuardianError, match="owns the account lock"):
            second.acquire()
    finally:
        first.release()

    # The lock byte file and metadata intentionally persist after release. The
    # OS lock, not stale JSON/PID text, decides whether a new owner can proceed.
    second.acquire()
    second.release()


def test_accepted_but_unpublished_stop_is_actively_cancelled_by_exact_id(tmp_path):
    async def scenario():
        client = FakeClient()
        item = guardian(client, tmp_path)
        await item.prepare()
        item.state.update(status="sl_submitted", sl_order_id=9999, tp_order_id=None)
        item.store.save(item.state)

        assert await item.cycle() is True
        assert await item.cycle() is True
        with pytest.raises(GuardianRetry, match="retaining ownership"):
            await item.cycle()

        assert item.state["sl_order_id"] == 9999
        assert "cancel_9999" in client.events
        assert client.positions

    asyncio.run(scenario())
