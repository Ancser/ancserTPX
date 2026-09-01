# ancserTPX — Current Handoff

Updated 2026-09-01. Current HEAD + the uncommitted fixes listed below.

## State

```
tests            489 pytest passing + 8 subtests + 32 Chromium interaction tests
invariants       74 documented / 72 active / 2 explicitly retired
strategies       factor · momentum · betafib · pi · fade · sigma  (+ confluence, live-only)
presets          BEST · MOMENTUM BEST · BETAFIB BEST · PI BEST · PI BEST 2MNQ · PI 2MNQ BOTH BEST
```

## Where truth lives

| Question | Authority |
|---|---|
| Which strategies exist | `tests/test_strategy_pipeline_classification.py` |
| Parameter defaults | `backend/db/models.py::StrategyParams` (routes reads `_PARAM_DEFAULTS`) |
| PI historical signals | `backend/data/pi_history.py::load_rows` — the **only** reader |
| Local Web security boundary | `backend/web_security.py` + WEB-001..005 |
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
file only. Since 2026-08-31, one Discord message that parses to two or more
supported PI marks is rejected as an aggregate/opening-summary message. The
live listener writes only a diagnostic `multi_signal_skip` row, while the
normal history loader, same-day replay loader, and audit API filter legacy
multi-mark rows as well. This is message-level and independent of whether a
candidate mark would have been accepted or profitable; raw audit backups remain
for investigation and actual broker/execution records are not rewritten.

### R0.5 — New York market clock + manual-position ownership (2026-08-30)

Raw candle/order/trade timestamps remain UTC instants. Market segments and the
15:30/15:45 close window now use `America/New_York`, so EST/EDT and year
boundaries do not depend on fixed summer UTC hours. Topstep trade-day accounting
remains `America/Chicago` 17:00; PI replay filtering remains Los Angeles 07:00.

Old derived sweep/backtest results are retained as evidence but are not loaded
as current results unless tagged `america-new-york-v1`; rerun them. Presets were
versioned in place. Legacy non-null BETAFIB summer-UTC hours migrate once to ET.
Raw candles, broker fills, `live_exits`, and historical research reports are not
rewritten. Pre-clock-version research reports remain historical evidence and
must be regenerated before making a current winter/cross-year comparison.

Manual/untracked positions are observe-only: they block new bot entries, but the
engine does not launch a guardian, add/cancel exits, run trailing/max-hold, or
session-close flatten them. When flat, strategy evaluation resumes. Bot-owned
positions retain attached Auto OCO monitoring and fail-safe flatten behavior.
The exact ownership matrix, unchanged OCO flow, broker caveat, and regression
gates are recorded in `docs/LIVE_ORDER_OWNERSHIP.md`.

Current script dependency audit:

- Direct close-window users now call the shared clock:
  `best_reopen_carry_study.py`, `emapmo_best_threshold_study.py`,
  `pi_exit_study.py`, `pi_hypothesis_tests.py`, `pi_long_only_study.py`, and
  `pi_purple_exit_study.py`.
- PI studies importing `pi_exit_study.simulate` inherit the new clock:
  `pi_asymmetric_config.py`, `pi_level_breakdown.py`, and
  `pi_per_marker_config.py`.
- Backtest/strategy consumers can produce different EST/cross-year results even
  without source edits, including `best_mes_parity_study.py`,
  `best_regime_diagnosis.py`, `clamp_cap_study.py`, `emapmo_adaptive_ab.py`,
  `hold_window_ab.py`, `pf_attribution.py`, `preset_stability_baseline.py`,
  `public_strategy_research.py`, and `stability_sweep_2026.py`, plus the EMAPMO
  factor/session diagnostic scripts.

All 42 files under `scripts/` passed an import-smoke after the migration. This
proves interface/import compatibility, not that their old numeric reports remain
valid; rerun any study containing EST months. `MOMENTUM`'s researched UTC
`entry_hour` remains unchanged; Topstep day accounting remains DST-aware CT.

### R0.6 — PI directional exits + current-form Lens (2026-08-31)

PI now has separate, end-to-end time exits: `pi_long_hold_min` defaults to
`0` (OFF), while `pi_short_hold_min` defaults to `60`. An explicit zero
survives request normalization. Backtest and Live select the limit by the
position/signal direction; Live applies it only while `_active_signal` proves
the position is bot-owned, then calls the existing `flatten_now()` path.
Attached Auto OCO creation/synchronization was not changed. That existing
flatten path cancels known bot orders, flattens, and performs the residual
broker-order sweep; manual/untracked positions remain observe-only.

The PI exit form presents the same `1` through `4` ATR choices (0.5 steps) on
both sides, with `LONG SL | LONG TIME EXIT` and
`SHORT SL | SHORT TIME EXIT` as equal-width rows. Label-to-control spacing is
the normal 4px used by the rest of the form.

Precision Lens previously cloned initial markup attributes, so later
JavaScript changes to `select.value`, selected options, account lists, preset
lists, checkboxes, and text inputs could remain stale inside the lens. Optical
clones now carry stable form keys and synchronize live DOM properties before
paint, including late option-list changes. Password, hidden, and file values
are deliberately blank in optical DOM. If a future regression ever shows a
Lens/source disagreement, the real source control remains authoritative.

### R0.7 — Research robustness presentation (2026-09-01)

The Research panel now starts with six equal-width baseline cards (TRADES,
CONTRACT, DATE, PF, PNL/MO, MAXDD), followed by Monte Carlo, Walk-Forward,
Slippage, then Topstep/XFA. The long combined heading and repeated baseline
sentence were removed. Topstep program explanations are available through the
same dynamic `?` tooltip mechanism as parameter help.

Monte Carlo P5/P25/P50/P75/P95 values come from the existing seeded backend
bootstrap; the browser does not compute a second distribution. PNL/MO and
maxDD now show the full point-by-point P5–P95 replay envelope: P25–P75 is the
lighter inner band and P5–P95 is the lower-opacity outer band, with all five
percentile lines visible. Walk-Forward shows its chronological cumulative
curve and running maxDD; Slippage shows original plus every injected level on
both curves. The scalar stat lines remain below each chart and wrap one row per
result. PNL/MO below zero gets the Performance-style amber warning; maxDD uses
the existing risk meaning: over $1,000 amber, over $2,000 red. Topstep/XFA now
show 1, 2, 3, 5, and 10 MNQ results. No live engine, order, OCO, signal, or
preset behavior changed.

### R2 — Zone-age gate

There is **no** zone-age trading block. The 0.17.0 code some notes refer to was
a `>24h logger.warning`, never a block, and was deleted in the 1.0 rewrite.
A gate is a *proposal*, not a regression to restore.

### R3 — Live/backtest decision parity

`shadow_replay.py` matches with ±12min / ±120tick tolerance — a diagnostic, not
a parity contract. Exact decision equivalence (separate from execution/slippage
parity) does not exist yet.

### R4 — Local Web boundary enforced; Practice-only retired (2026-08-31)

Main/Express live trading is the intended workflow. The user explicitly retired
Practice-only enforcement, so `verify_practice_account()` remains an unwired
helper and must not be inserted into Web start, terminal, or `place_order()`.

The Web control plane is now same-origin and loopback-only: production entry
points bind `127.0.0.1`, wildcard CORS is removed, unexpected Host/Origin values
are rejected, and every mutating `/api` request requires a process-local
HttpOnly session plus a port-scoped CSRF cookie/header pair. Security headers are
applied globally and API docs are disabled unless `ANCSERTPX_DEV_DOCS=true` is
deliberately set for local development. This protects the browser control plane;
root/API/static responses use `Cache-Control: no-store`, so one refresh on the
same port loads the current process and asset set. This protects the browser
control plane; it is not a defence against malware already running as the same
OS user.

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
7. **Glass/frontend** — UI-002..005. Keep the Playwright browser contracts in
   `tests/ui/` running alongside structural Python assertions and `node --check`.
   The production JS remains large; prefer structural assertions over screenshots.
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
