# Live order / Auto-OCO ownership

Current contract as of 2026-08-30. This is a current design record, not a
historical version report. Executable authority remains
`tests/test_exec_protection_invariants.py` and
`tests/test_live_manual_guardian_integration.py`.

## One rule

The engine may manage SL/TP only when the current process can attribute the
position to its own pending entry and has promoted that entry's
`_pending_signal` to `_active_signal`.

A broker position with `_active_signal is None` is **external/untracked**. It
may be a manual trade or a position that survived a process restart. It blocks
new bot entries, but the engine does not inspect, create, modify, cancel, or
flatten its exit orders.

`_active_signal` is intentionally process-local and is not reconstructed from
price, size, direction, or nearby orders. Those properties are not proof of
ownership. Therefore a bot trade that survives a restart is conservatively
treated as untracked; its already-attached broker Auto-OCO remains broker-side,
but the restarted engine does not reprice or trail it.

## State and permitted actions

| State | Ownership proof | Bot action |
|---|---|---|
| Flat, no pending entry | none required | Strategy may evaluate and open a new entry |
| Bot pending entry | `_pending_order_id` plus broker-ledger fill attribution and `_pending_signal` | May cancel its pending entry; entry request already carries both attached brackets |
| Bot-owned open position | `_active_signal is not None` in the same engine process | May scan attached child IDs, `modify_order()` them to strategy prices, trail, apply max-hold/session close, and run missing-OCO fail-safe |
| Manual/restart untracked position | broker position exists and `_active_signal is None` | Observe only; block strategy entry; do not query/claim exits; do not run guardian, trailing, max-hold, session flatten, or missing-OCO fail-safe |
| Explicit `/live/flatten` | direct user command | Emergency operation: flatten the account and cancel/sweep working orders for the configured contract |

When an untracked position disappears, `_sync_position()` confirms absence on
three broker reads. It preserves external working orders and then allows normal
strategy evaluation to resume. It does **not** infer that every external order
is safe or stale.

## Bot-owned Auto-OCO flow — unchanged

The 2026-08-30 manual-position change did not modify these methods or their
contract:

1. `_normalize_entry_protection()` keeps SL/TP on the correct side and enforces
   minimum non-zero distances.
2. `_entry_brackets_for_signal()` builds signed ProjectX tick offsets: Stop
   Market type `4` for SL and Limit type `1` for TP.
3. Both `_place_order()` and `_place_market_entry()` put
   `stop_loss_bracket` and `take_profit_bracket` on the **same entry request**.
4. `TopstepXClient.place_order()` maps those objects to
   `stopLossBracket`/`takeProfitBracket` in the broker payload.
5. After the broker ledger proves that the pending entry filled,
   `_sync_position()` promotes `_pending_signal` to `_active_signal`.
6. `_place_sl_tp()` does not place a second pair. It calls
   `_scan_auto_oco_order_ids()` and `_sync_auto_oco_protection()` to find the
   attached children and calibrate their absolute prices with `modify_order()`.
7. `_monitor_auto_oco_protection()` retries missing IDs/prices. For a
   bot-owned position only, failure to confirm both children for five minutes
   invokes the existing flatten-and-pause fail-safe.
8. On a bot-owned close, the engine cancels its recorded residual child IDs and
   sweeps remaining configured-contract orders. Existing exact double-fill
   detection remains active.

Do not replace steps 3–6 with plain entry plus later independent SL/TP orders.
Do not remove `modify_order()`: the attached offsets are based on the intended
entry, while market fills can require calibration back to strategy prices.

## Exactly what changed on 2026-08-30

The ownership boundary changed; the Auto-OCO construction and synchronization
logic above did not.

- `MANUAL_POSITION_POLICY` now defaults to `"observe_only"`.
- `_resume_persisted_manual_guardian()` returns before reading or relaunching
  legacy guardian state.
- `_ensure_manual_position_guardian()` returns before reading open orders or
  launching a guardian.
- `_auto_oco_missing_timed_out()` requires `_active_signal`, so an untracked
  position cannot trigger bot fail-safe flatten.
- Session-close flatten, strategy max-hold, trailing SL, and position-size
  fail-safe require `_active_signal`.
- Manual/untracked close cleanup preserves external contract orders and does
  not notify strategy trade state or consume bot-only daily locks.

The legacy guardian implementation remains below the policy guard so its old
state and safety tests are not silently deleted. Production Web and Terminal
paths do not expose a switch that enables it.

## Broker-side OCO limitation

Topstep's current documentation says Auto-OCO is per entry and that when one
linked leg fills, the sibling cancels automatically. It also says the UI's
explicit Flatten All closes positions and cancels working orders:

- <https://help.topstep.com/en/articles/14434175-topstepx>
- <https://help.topstep.com/en/articles/8765442-order-types-fills-and-slippage>

That documentation does not explicitly guarantee that an arbitrary manual
close action cancels every attached or independent order. The engine therefore
must not claim this as guaranteed. Under observe-only policy it deliberately
does not clean up external orders. If a manual close leaves a non-OCO or orphan
order, the operator must cancel it; use Practice to verify the exact TopstepX
action used in the manual workflow.

## Regression gates

Before changing order ownership or OCO code, all of these must stay green:

```text
python -m pytest tests/test_exec_protection_invariants.py -q
python -m pytest tests/test_live_manual_guardian_integration.py -q
python -m pytest tests/test_broker_order_mapping.py -q
python -m pytest tests/ -q
```

Required behavior checks:

- Limit and market entries both reach the broker with one attached SL/TP pair.
- Signed offsets and ProjectX order types remain directionally correct.
- Attached child IDs feed `modify_order()`; no second independent pair exists.
- Manual/untracked positions cause zero guardian launches, zero order scans,
  and zero automatic closes.
- Bot-owned positions still flatten at the configured close window and still
  trigger the missing-Auto-OCO fail-safe.
- Explicit `/live/flatten` remains a separate, user-triggered emergency action.

Any proposal that changes one of these outcomes is a behavior change and needs
explicit approval, updated invariants, and a new failing regression test before
production code is edited.
