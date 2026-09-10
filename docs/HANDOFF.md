# ancserTPX — Current Handoff

Updated 2026-09-09. Current HEAD + the uncommitted fixes listed below.

## State

```
tests            649 pytest passing + 8 subtests + 39 Chromium interaction tests
invariants       87 documented / 83 active / 4 explicitly retired
strategies       factor · momentum · betafib · pi · optionwall · fade · sigma  (+ confluence, live-only)
presets          BEST · MOMENTUM BEST · BETAFIB BEST · PI BEST · PI BEST 2MNQ · PI 2MNQ BOTH BEST
```

## Where truth lives

### 2026-09-09 — Event-entry research uses the production engine

`scripts/orderflow_event_engine_study.py` reads the saved `PI 2MNQ BOTH BEST`
preset and runs the actual BacktestEngine/PiSignalStrategy, replacing only the
entry feed for offline candidates. Canonical candles include seven calendar
days of warmup; comparisons retain common MBO dates. The earlier standalone
competition is not an exact preset replay: it used one contract, disabled the
short hard TP, recomputed ATR from RTH-only bars, and omitted production gates.
Its post-event three-second fields can also cross the next-minute entry time.
Do not promote old rankings without replaying the production contract.

The new runner tests two fixed strengths each of absorption/residual reversal,
depth-depletion breakout, failed breakout, and raw-derived split pressure.
The first three are minute-cache approximations, not tick-by-tick state models.
All previously researched dates remain retrospective validation. Opposite
signals mapped to the same decision candle cause abstention, never a sort-order
direction choice. Tests exercise availability, expired events and PI short
SL/TP geometry. Reports/trade records are in the external derived/research
tree under `orderflow_event_engine_study`; Live and presets are unchanged.

The footprint canvas now has two display densities. At overview scale it
aggregates cells into screen buckets, keeps only the strongest price area per
column and its dominant side, applies a 50-contract minimum when possible,
draws at most four depth walls per side, and suppresses small passive outlines,
tier beads, and imbalance labels. Quiet columns retain their strongest cell so
the price path does not disappear. Bubble radius is tied to quantity bands
(50–99 / 100–149 / 150+), while passive activity changes fill opacity only. At
detail scale the exact size bands return and up to eight strongest continuous
depth levels are shown. The raw response remains intact in `_footprintBars`;
this is a paint budget, not a data filter. A 1000-contract minimum would blank
the current MNQ 1m cache (sample maximum 257), so it is deliberately not used.
The chart legend is also capped and wraps active keys into a compact box.

The chart-only generic VAH/VAL/POC timeframe renderer, Session VA overlay, and
BETAFIB level overlay are retired. Their frontend layer rows, chart transport,
and dedicated marker payload are gone. BETAFIB execution and strategy-side
session filters remain separate production behavior; PRIOR DAY 70% VAH/VAL/POC
is still the retained three-line chart layer.

| Question | Authority |
|---|---|
| Which strategies exist | `tests/test_strategy_pipeline_classification.py` |
| Parameter defaults | `backend/db/models.py::StrategyParams` (routes reads `_PARAM_DEFAULTS`) |
| PI historical signals | `backend/data/pi_history.py::load_rows` — the **only** reader |
| Option Wall replay signals | `backend/data/option_wall_signals.py::load_primary_strict_signals` — causal entry columns only |
| Local Web security boundary | `backend/web_security.py` + WEB-001..007 |
| Behavioural invariants | `docs/INVARIANTS.md` |

Do not infer architecture from `README.md` or from `docs/1.0.x_*.md`.
Standalone **PMO / TREND / DAY ZONE / DISTRIBUTION are removed**; `pmo` survives
only as a FACTOR-family alias.

### R0.9 - Option-wall Primary Strict historical replay (2026-09-05)

`OPTION WALL` is now a registered Backtest model with one first-version
sub-model, `PRIMARY STRICT`.  It reads a fixed causal signal tape from the
external `ancserMarketData/source/options/qqq_option_ml/` artifact, maps those QQQ hourly decisions
onto MNQ 1-minute execution candles, and deliberately ignores every PnL and
future-path column.  It is MNQ/RTH historical replay only: `/live/start`
explicitly refuses the model until a real causal live option feed exists.

The QQQ option-wall scripts under `scripts/option_wall_*` remain research tools
and cannot route an order.  They cover the portable Gamma gate, book-rule
abstention, nested acceptance models, frozen-wall risk transitions, and causal
OI-wall recomputation 5/10/15 minutes after entry.  Intrahour Volume Walls
remain unavailable because the purchased option volume is hourly; the code
does not interpolate it.

The chart feed prefers the 5-minute derived demos where they exist (23
sessions, 2026-08-03 through 2026-09-02) and fills older dates from the same
purchased point-in-time feature artifact at its honest hourly cadence (185
sessions).  The aggregate therefore exposes 3,083 snapshots over 208 sessions,
2025-11-04 through 2026-09-02.  Hourly rows are labelled
`hourly_from_1m_cbbo_and_1h_volume`, use OI at 09:35 ET and completed hourly
Volume GEX thereafter, and extend only to the next snapshot/session close.
They are not interpolated or described as 5-minute data.

The promoted entry definition remains `primary_model_confidence` plus the fixed
Gamma/VWAP/wall gate.  The formal shared engine uses one MNQ, completed 5-minute
ATR blend stops (long 4x, short 1.5x), no hard TP/trailing, a 60-minute maximum
hold, and the platform's 15:45 ET flatten.  Over the available 2025-12-01 to
2026-09-05 replay it produces 74 trades, $5,877.24 net, PF 2.818, 58.1% wins,
and $476.66 max drawdown before any user-selected global daily locks.  The
older independent-trade research simulator reports $6,584 / PF 2.91 / $526
max drawdown; it remains evidence, not the app's displayed result.  Neither is
a validated live expectancy.  The 5-minute OI monitor affects only four trades
and is not worth live complexity yet.  Late-day Pin rules lose;
Deep-V and wall-break ideas have fewer than 30 trades and remain hypotheses.
An inspected-after-the-fact ensemble adds non-overlapping, long-only
`side_article_state` entries whose target wall is not collapsing: 100 trades,
$8,631 net, PF 2.84, and -$597 max drawdown.  It is a shadow/paper candidate,
not a preset, because the long-only/no-collapse choice needs later untouched
sessions.

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

### R1 — PI restart dedup and new-channel cutover

The trading `PiListener._seen` remains memory-only; its Live-window cursor is
still seeded to the newest message and then advanced with `after`. The
independent record-only listener now has a bounded today/yesterday catch-up and
continues writing a clean runtime stream from the active channel
`1547062725060993066`. Every new row carries `channel_id`; readers reject rows
from the retired channel before they reach Chart or replay.

The active audit file was cut over on 2026-09-09. The previous mixed/old audit
was moved to the primary MarketData archive before the active path was removed;
it is not loaded by the product. This preserves a recoverable forensic copy
without allowing old channel records to re-enter the strategy or Chart.

When the user explicitly runs a PI Backtest, the route adds only in-range
new-channel `received`/`recorded` marks as a temporary replay overlay. History
and audit/repost rows are deduplicated by 1m source time + symbol + kind because
a repost receives a different Discord message id. This makes a signal received
today visible immediately after clicking Backtest while leaving the immutable
history file untouched. A normal historical run still uses the history file
only. Since 2026-08-31, one Discord message that parses to two or more
supported PI marks is rejected as an aggregate/opening-summary message. The
live listener writes only a diagnostic `multi_signal_skip` row, while the
normal history loader, same-day replay loader, and audit API filter legacy
multi-mark rows as well. Actual broker/execution records and strategy push
snapshots are not rewritten.

### R0.5 — New York market clock + manual-position ownership (2026-08-30)

Raw candle/order/trade timestamps remain UTC instants. Market segments and the
15:30/15:45 close window now use `America/New_York`, so EST/EDT and year
boundaries do not depend on fixed summer UTC hours. Topstep trade-day accounting
remains `America/Chicago` 17:00; PI replay filtering remains Los Angeles 07:00.

Old derived sweep/backtest results are retained as evidence but are not loaded
as current product results; the product no longer exposes the cross-model sweep
route or result view. Presets were versioned in place. Legacy non-null BETAFIB
summer-UTC hours migrate once to ET.
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
  `hold_window_ab.py`, `pf_attribution.py`, and `public_strategy_research.py`,
  plus the EMAPMO
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

### R0.10 — Product surface cleanup (2026-09-06)

The product no longer exposes the cross-model sweep: the sidebar action/model
controls, result/preset view, `sweep_models` request field, and
`/backtest/sweep` API family were removed. Normal preset list/save/use/delete
behavior remains. The cross-model implementation and its dedicated research
scripts were deleted together with their
feature-specific tests. `test_product_scope.py` prevents the route, controls,
module, or Sweep scripts from returning while retaining normal preset CRUD.

The language switch, translation table/observer, and alternate Chinese UI copy
were removed. The UI is English-only and keeps `<html lang="en">`. Backtest
metrics for all-zone TP/SL/TRAIL and session TP/SL/TRAIL breakdowns were also
removed; the underlying exit mechanics remain unchanged.

Liquid Glass still supplies backdrop sampling/refraction, but no longer adds a
default frame, white edge/rim, specular pass, or edge stroke. The Live/Discord
chart lens uses the same no-rim treatment.

### R0.11 — Signal-attached unified exits (2026-09-06)

Exit selection now follows the same architecture as entry signals. Every
accepted `TradeSignal` carries one resolved immutable `ExitPolicy`, and the
single pure `evaluate_exit_operation()` kernel decides time close, one-shot
trailing, or R-ladder stop movement. PI resolves its directional hold time,
Option Wall resolves a 60-minute hold with no hard TP/trailing, and Factor-family
models resolve their configured TP/trail/ladder combination through that same
contract. Model-specific exit math no longer lives separately in the two engines.

Backtest and Live intentionally remain separate execution adapters: Backtest
simulates candle fills and same-bar ordering, while Live translates `CLOSE` to
the existing bot-owned `flatten_now()` flow and `MOVE_SL` to modification of the
attached Auto OCO child. Manual/untracked positions remain outside automatic
exit ownership. Factor ladder market entries now receive the same far-TP Auto
OCO bracket already used by limit entries and Backtest; this closes the one
parity gap found during integration.

### R0.12 — Canonical timebase and rolling contracts (2026-09-06)

All backend instants are based on aware UTC from `backend/timebase.py`.
Session rules convert that instant to New York, Topstep accounting converts to
Chicago with the 17:00 boundary, and PI source rules convert to Los Angeles.
The chart receives UTC instants and shifts only presentation to the configured
market timezone, America/New_York. It therefore shows the same wall-clock time
on every laptop/monitor and on Windows/macOS. UTC is not New York: New York is
UTC−5 in standard time and UTC−4 during daylight saving time.

Production defaults no longer name a fixed M26/U26 expiry. The backend computes
the current quarterly front month and publishes that mapping, contract economics,
clock version, and timezone names through `/api/config`; the browser consumes
that manifest. Fixed expiry strings remain only where tests deliberately exercise
historical parsing, rollover boundaries, or immutable fixtures.

### R0.13 — Native Windows desktop launcher (2026-09-06)

Windows now has a single-process native app entry point, `windows app.vbs`.
The hidden host starts `backend.desktop_app`, which owns both Uvicorn and an
embedded Edge WebView2 window on fixed loopback `127.0.0.1:8001`. A per-user OS
lock prevents duplicate app processes. Closing the window runs the normal FastAPI
lifespan shutdown: Web-owned engines stop, account leases and the broker client
are released, pending entries are cancelled, and broker-side protection on an
open position is preserved. The old `windows web.bat` is only a compatibility
wrapper; it no longer opens a browser or force-kills arbitrary port processes.

### R0.14 — Safe Windows launcher handoff (2026-09-07)

The former `backend/kill_old.ps1` was removed. Windows startup now uses
`backend/stop_legacy_instances.ps1`, which matches only known ancserTPX Web and
Terminal workers, closes their matching legacy BAT parent CMD, and never scans
ports `8000-8010`; it cannot terminate the native App's `pythonw` process or
its `8001` listener. The native page also polls `/api/health` every three
seconds. If the backend disappears while the WebView remains alive, the
account avatar becomes red with `BACKEND OFFLINE` and the page stays open; a
recovered backend restores the previous connection state.

### R0.15 — Lazy candle startup with incremental auto-save (2026-09-07)

The old startup accumulator decoded and rewrote the complete MNQ and MES
multi-million-bar pickles. That is the reason a native App could be charged
with roughly 3.5GB while a browser window appeared much smaller: the embedded
Python/WebView process owns the backend data objects, whereas Chrome reports
only its browser process separately.

Startup and CONNECT now fetch only a recent working window. New closed bars are
deduplicated into `ancserMarketData/source/futures/continuous_1m/{symbol}_accumulated_1m.pending.jsonl`, so the
background saver remains automatic without loading the canonical pickle. MNQ
is active by default; MES is activated after explicit MES/ES contract use.
Backtest, store-only history, chart left-edge pagination, and manual shadow
replay are the explicit full-history boundaries: they merge the pending journal
once, then may materialize the canonical store. The pending sidecars are
runtime-only and ignored by Git.

### R0.16 — Canonical MarketData tree and dual-disk mirror (2026-09-07)

Large downloaded/generated data is now external to the repository. The primary
tree is the sibling `ancserMarketData` with `source/futures/continuous_1m`,
`source/options/qqq_option_ml`, `source/orderflow/mnq_mbo`, and
`source/discord/pi`; `derived` contains research/backtest output and `runtime`
contains logs/state. The tracked preset, model registry, and small seed remain
bootstrap files only. Windows installation does not register an automatic
E:\\ mirror; `ANCSER_MARKET_DATA_ROOT` selects the canonical tree and
`ANCSER_MARKET_DATA_BACKUP_ROOTS` is reserved for explicitly configured
backups on other machines. The migration tool copies and SHA-256 verifies
before removing old copies.

### R0.17 - Databento MBO context research (2026-09-09)

The MNQ MBO research now covers 22 RTH dates, including 2026-08-07. The
2026-08-07 omission was a download-window choice (`--start 2026-08-08`), not
missing market data; that quote/download was completed and the raw file is
stored only under `ancserMarketData`. The compact cache is schema v5 with 390
RTH one-minute bars for that date.

`scripts/orderflow_context_combination_study.py` is offline research only. It
tests VWAP state, prior-day 70% value location, rolling profile POC migration,
1:10 diagonal imbalance, passive heatmap touch/rejection, and a 150+ reversal
sequence using the fixed PI ATR-blend exit. The study keeps discovery through
2026-08-14 separate from later evaluation dates. It does not change Live,
Backtest, or order routing. On the current sample the 150+ same-level reversal
family has zero qualifying events, so it is reported explicitly rather than
silently treated as a failed strategy. The generated reports live in the
external `ancserMarketData/derived/research` tree. The chart also has an
off-by-default CVD / DELTA layer that reuses the visible-window order-flow
request and draws RTH cumulative aggressive delta without changing trading
decisions. The offline context study now mixes two causal CVD filters with
the existing VWAP, previous-day value, profile-wave, 1:10 imbalance, and
passive-rejection contexts: `cvd_aligned` requires positive/negative five- and
15-minute CVD deltas to agree with the candidate direction, while
`cvd_divergence` looks for a rolling price extreme with opposite five-minute
CVD. On the current 22-day sample, `mbo_turnover + cvd_aligned` improved the
evaluation PF from 1.2494 to 2.3233 and reduced absolute maxDD from $548.14 to
$227.40, but left only 27 evaluation trades and was negative in discovery
(10 trades, PF 0.7286); it is a research lead, not a live filter. The
divergence filter was negative in evaluation and is not promoted.

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
percentile lines visible. Walk-Forward shows separate 1/3, 2/3, and 3/3 lines
in both its cumulative PNL and running maxDD charts, with $1,000/$2,000 dashed
guides on maxDD. Slippage is a compact organized table of original plus every
injected level; its scalar rows no longer use charts. PNL/MO below zero gets the Performance-style amber warning; maxDD uses
the existing risk meaning: over $1,000 amber, over $2,000 red. Topstep/XFA now
show 1, 2, 3, 5, and 10 MNQ results. No live engine, order, OCO, signal, or
preset behavior changed.

### R0.8 — Native lower navigation restored (2026-09-03)

Liquid Glass remains on the upper Research/Backtest/Live workspace dock only.
The lower BACKTEST TRADES/EXECUTE TRADES/PNL CURVE/SYSTEM LOG bar now
uses the original `.bottom-tabs` / `.bottom-tab` flat underline styling. The
skin no longer wraps those labels or inserts a segment lens/container, and a
browser contract protects both halves of that decision: upper stays Glass,
lower stays native. The skin asset cache key was advanced so a normal reload
does not reuse the older lower-nav transformation.

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
product language toggle/translation path        retired; UI is English-only
EXEC-004 reverted to the pre-1.0.10n contract after live evidence   see corrected R0
LIVE-001 / LIVE-003 named things that never existed   corrected
Execute Trades could retain an old broker cache indefinitely   bounded refresh + tab-open refresh
```
