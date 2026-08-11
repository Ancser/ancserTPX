# ancserTPX — Current Handoff

Updated 2026-08-08. Baseline `1.0.10i` + the uncommitted fixes listed below.

## State

```
tests            278 passing (~45s), CI green (pytest + frontend node --check)
invariants       36 total / 32 automated / 4 unprotected (UI-002..005, all Glass)
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

### R0 — Who owns the protective orders? *(highest risk)*

Two mechanisms coexist and nothing documents their relationship:

1. `_entry_brackets_for_signal()` attaches `stopLossBracket` / `takeProfitBracket`
   to the entry order, computed from the **strategy's own** SL/TP prices
   (`engine.py:3914`, `:3999`)
2. `_scan_auto_oco_order_ids()` + `modify_order()` waits for TopstepX Auto OCO
   children and rewrites them (`engine.py:878–930`), erroring on timeout

Unknown: is (1) primary and (2) a correction? Is (2) primary and (1) a
belt-and-braces? **Do both fire, producing duplicate children?**

`docs/INVARIANTS.md::EXEC-004` originally asserted the opposite of (1) and was
wrong — corrected 2026-08-08. Answering this needs **live evidence** (actual
child-order count and prices on a real fill), not code reading.

**Do not delete bracket code on the strength of any document.** Removing (1)
opens a naked-position window between fill and the modify step.

### R1 — PI restart dedup

The trading `PiListener._seen` remains memory-only; its Live-window cursor is
still seeded to the newest message and then advanced with `after`. The
independent record-only listener now has a bounded today/yesterday catch-up and
uses durable audit message ids as its stop boundary, so web/terminal restarts
repair missing audit/chart rows without replaying the strategy.

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
EXEC-004 invariant stated the opposite of code   corrected; see R0
LIVE-001 / LIVE-003 named things that never existed   corrected
```
