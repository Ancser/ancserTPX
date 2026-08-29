# ancserTPX — Current Handoff

Updated 2026-08-27. Current HEAD + the uncommitted fixes listed below.

## State

```
tests            418 pytest passing + 8 subtests + browser interaction coverage
invariants       60 total / 60 automated / 0 unprotected
strategies       factor · momentum · betafib · pi · fade · sigma  (+ confluence, live-only)
presets          BEST · MOMENTUM BEST · BETAFIB BEST · PI BEST · PI BEST 2MNQ
```

## Where truth lives

| Question | Authority |
|---|---|
| Which strategies exist | `tests/test_strategy_pipeline_classification.py` |
| Parameter defaults | `backend/db/models.py::StrategyParams` (routes reads `_PARAM_DEFAULTS`) |
| PI historical signals | `backend/data/pi_history.py::load_rows` — the **only** reader |
| Behavioural invariants | `docs/INVARIANTS.md` |

Do not infer architecture from `README.md` or from `docs/1.0.x_*.md`.
Standalone **PMO / TREND / DAY ZONE / DISTRIBUTION are removed**; `pmo` survives
only as a FACTOR-family alias.

---

## Open questions — resolve before touching the related code

### R0 — Protective-order ownership corrected (2026-08-17)

This supersedes the 2026-08-11 / 1.0.10n decision. That decision was made
before the bracket parameters were understood and incorrectly removed the
engine's attached brackets. Current live evidence showed a PI BEST entry with
valid intended SL/TP but no child orders, while TopstepX explicitly reported
that Position brackets are disabled and only Auto OCO brackets can be used.

The approved behavior is now:

1. Every live limit/market entry request carries one attached Auto OCO SL/TP pair.
2. The engine scans for those attached child IDs after fill and uses
   `modify_order()` to align them with the strategy's absolute prices.
3. The engine never places a second independent SL/TP pair.

`tests/test_exec_protection_invariants.py` executes both entry paths and also
protects the scan → modify path.

The same incident exposed a separate broker-contract failure: current
`/api/Order/search` rejects the historical account-only payload with HTTP 400.
It now receives a bounded UTC `startTimestamp` / `endTimestamp` window, so a
filled entry can leave pending state and reach protection synchronization. The
exact date ProjectX began strictly enforcing the documented timestamp is not
established.

The same incident also proved that two concurrent web/terminal starts could
run two PI listeners for one account. `LiveEngineLease` and the per-account
web start lock now make account ownership single-instance across processes.

### R1 — PI restart dedup

The trading `PiListener._seen` remains memory-only; its Live-window cursor is
still seeded to the newest message and then advanced with `after`. The
independent record-only listener now has a bounded today/yesterday catch-up and
uses durable audit message ids as its stop boundary, so web/terminal restarts
repair missing audit/chart rows without replaying the strategy.

When the user explicitly runs a PI Backtest, the route now adds any in-range
`received`/`recorded` audit marks as a temporary, deduplicated replay overlay.
This makes a signal received today visible to the calculation immediately after
clicking Backtest, while leaving `data/research/pi_signals.json` and the Live
listener untouched. A normal historical run still uses the immutable history
file only.

### R2 — Zone-age gate

There is **no** zone-age trading block. The 0.17.0 code some notes refer to was
a `>24h logger.warning`, never a block, and was deleted in the 1.0 rewrite.
A gate is a *proposal*, not a regression to restore.

### R3 — Live/backtest decision parity

`shadow_replay.py` matches with ±12min / ±120tick tolerance — a diagnostic, not
a parity contract. Exact decision equivalence (separate from execution/slippage
parity) does not exist yet.

---

## Strategy validity — read before optimising anything

The engineering safety net is in reasonable shape. **The strategies it protects
are not validated.**

- `BEST` / `MOMENTUM BEST` / `BETAFIB BEST` — the six-year re-validation put
  58 gate-passing variants through both symbols: **0/60 passed**. 2026 was 98%
  positive, out-of-sample 45% (coin flip). Treat these as known-overfit.
- `PI BEST` / `PI BEST 2MNQ` — n=17 over two months (~8–9 trades/month).
  PF 3.37 is not a reliable estimate at that sample size.

278 tests prove the engine executes settings faithfully. They prove nothing
about whether the settings make money.

---

## Suggested order

Provisional — re-derive from current evidence rather than following blindly.

1. **R0** — protective-order ownership (blocks all execution work)
2. **R1** — restart-safe PI dedup
3. **R3** — design exact decision parity, split from execution parity
4. **Strategy Registry** — replace the manual `FACTOR_PIPELINE_STRATEGIES` /
   `ZONELESS_STRATEGIES` / `ZONELESS_ZONE_RENDER` tuples + dispatch + route
   aliases with one `StrategySpec`. Behaviour-preserving, one strategy at a
   time, golden decisions unchanged. **Before** engine decomposition — ownership
   should be settled before splitting 5,088 lines.
5. **Broker lifecycle characterisation** — timeout after broker accepted,
   retry/idempotency, partial fills, cancel/replace, reconnect, restart with an
   open position, unknown broker state. All currently untested.
6. **LiveTradingEngine decomposition** — session clock → risk gates → order/
   protection → position/recovery → strategy runtime → thin coordinator.
   No strategy-math changes during extraction.
7. **Glass/frontend** — UI-002..005. Needs a node test harness first
   (`ancserTPX.js` 9,333 lines + `tpx-glass.js` 3,395 lines, currently 0 tests;
   only `node --check` in CI). Prefer structural assertions over screenshots.
   Do not start this merely to raise a coverage number.

## Recently fixed (2026-08-08) — do not "re-fix"

```
PI listener died on a malformed timestamp (P0)   parse_message + run() now guarded
routes.py held a third copy of PI defaults       now reads _PARAM_DEFAULTS
candle_store.load() leaked its cache list        cache-miss path now copies
pi_signal / routes each re-read the signal json  both go through load_rows()
glass skin removed the language toggle           now re-parents it
EXEC-004 reverted to the pre-1.0.10n contract after live evidence   see corrected R0
LIVE-001 / LIVE-003 named things that never existed   corrected
Execute Trades could retain an old broker cache indefinitely   bounded refresh + tab-open refresh
```
