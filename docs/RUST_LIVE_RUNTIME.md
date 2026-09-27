# Rust live runtime — implementation and readiness report

Status: the opt-in TopstepX coordinator and guarded `live` CLI are wired. Paper
and replay demonstrations pass offline. Live trading remains unverified and
must stay disarmed until the broker event/Auto OCO contract is confirmed and a
separate authorized integration review is completed. The user authorized this
implementation scope; no real account session or order was started.

## Implementation status (baseline 2026-09-26; PI milestones 2026-09-27)

- `ancsertpx-live-runtime` now has one shared state core, exclusive-writer
  hash-chained JSONL journal, paper adapter, deterministic MES replay fixture,
  TopstepX protocol/event mapping, Robinhood fail-closed capability boundary,
  and a Rust/Python JSONL bridge.
- The `live <config> --arm --approve-vp-breakout-atr --confirm-auto-oco`
  coordinator validates the explicit flags before starting the bridge; verifies
  account/contract/lease/hubs; warms the existing VP breakout/ATR strategy on
  sorted TopstepX history; builds completed one-minute trade bars; uses the
  existing Python market clocks and exit kernel; journals actions; reconciles
  positions/orders; and shuts down on disconnect or ambiguous side effects.
- Cancel, modify, and close mutations use single-shot broker requests. Their
  ambiguous outcomes block the runtime for operator reconciliation. Manual
  children require exact custom-tag ownership; unconfirmed position IDs cannot
  be used for contract-level close. Partial fills and exits retain their net
  quantity and ownership gates.
- TopstepX order checks now use `size - fillVolume` as the remaining child
  quantity and require it to equal the reconciled broker position. Only Open
  children count as confirmed protection; Pending and unknown states retain a
  blocker. Stop modification omits the optional order `size` field, preserving
  the current total order quantity when a child has partial fills. The official
  API defines `fillVolume` as filled contracts, `size` as total order size, and
  status 6 as Pending ([orders](https://gateway.docs.projectx.com/docs/api-reference/order/order-search/),
  [realtime events](https://gateway.docs.projectx.com/docs/realtime/),
  [modify](https://gateway.docs.projectx.com/docs/api-reference/order/order-modify/)).
- Default mode remains paper. `status` reports the guarded live CLI exists;
  status alone does not authorize or connect a broker. No credentials, account
  IDs, or secret values are printed by the capability report.
- Offline checks run for this report: Rust formatting check and
  `cargo check -p ancsertpx-live-runtime --offline`; `paper-demo` with seven
  adversarial event sequences; Python syntax compilation; JSONL
  `market_context` and `exit_decision`; replay decision/ledger payloads
  identical after excluding the unique temporary journal path; CLI status and
  refusal without explicit live flags; and `git diff --check`. The broad suite
  was not run for that baseline; focused PI checks are recorded below.
- Replay uses 30 warm-up bars and 9 streamed bars and emits one VP
  breakout/retest ATR intent. It is a wiring demonstration, not a strategy
  performance study or evidence of stable profitability.
- PI now has a separate offline lifecycle command for source selectors,
  flatten tickets, stop/target invalidation, same-day reopen expiry, one-shot
  claims, and live ticket identity checks. Its new differential tests and the
  existing PI lifecycle tests pass; this contract is not wired into a trading
  entrypoint.
- PI also has an offline signal-builder command for continuation and LV2
  replacement. It shares the ordinary PI tick-rounded bracket constructor and
  is differentially checked against the Python builders; Backtest/Live routing
  and realized ledger parity remain open.

## Luna Max research and Astra High adversarial review

Luna Max summarized the offline evidence and separated implementation progress
from live readiness. Astra High's independent review found no confirmed
Critical issue and reported 7 High plus 5 Medium findings. The code and offline
paper scenarios now cover those findings:

- Untagged/manual stop adoption is blocked; stop modifications require a
  verified tagged child or a previously verified child ID.
- Close, modify, and cancel use single-shot requests; uncertain results stop
  automated operation and require reconciliation.
- A flat position with a working partial-entry remainder retains a cancel and
  reconciliation gate. Late fills and fills after flat require operator review.
- Terminal entry state survives delayed trade-fill updates. Broker position IDs
  remain distinct from entry order IDs. Partial exits reduce the owned residual
  size, and late child fills remain associated with verified exit order IDs.
- Reconciliation can release completed flat intents, retains verified trailing
  child IDs, and uses an exclusive journal writer lock. Market freshness uses
  the timestamp paired with the price. User events are account-filtered before
  entering the Rust risk state.
- The offline event walkthrough also caught and fixed a tag collision case:
  TopstepX OCO children can carry the parent's custom tag, so only an opening
  side/type order can update parent-entry state.

No authenticated broker session was used to verify TopstepX's actual Auto OCO
child tags/parent linkage, hub subscription acknowledgements, or live callback
ordering. Those broker contract questions remain open. A disconnect invalidates
warm-up/reconciliation state and stops this coordinator; an operator must
restart it after confirming broker state.

## Current limits and live readiness blockers

- No live API authentication, account discovery, market/user hub connection,
  order placement, service restart, or commit was performed in this task.
- The VP breakout/ATR signal has replay wiring evidence only. There is no
  multi-year, out-of-sample, slippage/commission-aware evidence here that
  approves it as a stable live strategy. The explicit strategy flag is an
  operator acknowledgment, not a profitability finding.
- Auto OCO child custom-tag inheritance and reliable parent linkage remain
  unverified. If exact ownership cannot be confirmed, the coordinator blocks
  child management or stops after its bounded confirmation window.
- Market/user subscription confirmation is based on hub readiness; actual
  market freshness is separately required before entry. Reconnection does not
  restore readiness automatically.
- A journal sidecar lock left by a process crash blocks reopening on purpose.
  Inspect the lock PID and confirm that process has exited before an operator
  removes the stale `.lock` file; the runtime never steals a stale lock.
- Robinhood remains a fail-closed MCP capability report. It has no live options
  execution path in this runtime.

## Rust replacement coverage — strategy and entrypoint inventory (2026-09-27)

The current `data/presets.json` snapshot is read only. SHA-256 before the
migration slice: `4899a94b277bc96f51940ced0fb6e0c5a734edd822578b903912f00643b0e64`
(17,418 bytes). It contains seven named presets. `last_used_live` resolves to
**PI 2MES BOTH BEST**; `last_used_bt` points to **VOLUME PROFILE RESEARCH**,
whose stored strategy mode is `factor`. The `BEST` preset also uses `factor`.
The registered Python strategy modes are `factor`, `momentum`, `betafib`, `pi`,
`optionwall`, `delta_absorption`, `volume_profile`, `fade`, and `sigma`.
Factor has three supported signal families: `emapmo`, `momentum_reversion`,
and `icefishball`. `trend` falls back to Factor; `confluence` is a retired
strategy path with residual request/status code.

The active PI MES preset was resolved read only by calling
`terminal_live._load_default_preset()` and `_build_strategy_params()`. Effective
values include contract `CON.F.US.MES.Z26`, size 2, 60-second input candles,
`pi_signal_set=pi_only`, `pi_long_only=false`, longs `[青π,深蓝圈]`, shorts
`[粉π]`, `pi_short_levels=None`, holds `0/60` minutes, short SL multiplier 1.5,
signal age 5 minutes, LV2 replacement enabled, long continuation `[深蓝圈]`,
short continuation empty, reopen gap cap 1R, `factor_sl_value=4`, `rr_ratio=3`,
`factor_max_trades_per_day=3`, fixed tick fields `50/150`, full-TP lock 0,
daily full-loss stop 1, full-win stop 0, and profit stop 0. The fixed tick fields
do not set PI entry geometry: PI uses the 5-minute ATR blend, SL multiplier 4,
and 3R target. `_ResearchBase._make()` counts up to three generated PI signals
per Topstep trade date. `one_trade_per_session_direction=true` is copied into
both engine objects; their entry gates read `tr_one_trade_per_session`, which
is false in this preset. The first field has no runtime read site today.
`pi_continue_long_kinds` and `pi_short_kinds` resolve as one-element lists in
Python; the terminal continuation selector is active. Tick economics resolve
from contract helpers and remain absent from the saved preset. The second PI
preset is **PI 2MNQ BOTH BEST**, contract `CON.F.US.MNQ.Z26`, size 2. No preset
was modified during this audit.

Python entrypoints remain authoritative for the existing user flows. Preset
resolution differs by entrypoint: terminal uses account-specific preset,
`last_used_live`, then `last_used_bt` in
`backend/terminal_live.py::_load_default_preset` (428); Web Backtest and Web Live
build `StrategyParams` from separate request models through the shared
`backend/api/routes.py::_build_strategy_params_from_request` (507). The native
Rust preset resolver and request snapshots remain planned. Capture exact
terminal, Web Live, and Web Backtest effective configs independently before
claiming cross-entrypoint parity.

The Python source of truth is `backend/data/pi_history.py::load_rows`
([line 84](../backend/data/pi_history.py#L84)),
`backend/live/pi_listener.py::PiListener` (construction at line 367, run loop at
638, poll at 675, dispatch at 740),
`backend/strategy/pi_signal.py::PiSignalStrategy` (configuration at 215,
replacement at 439, continuation at 479, `push` at 546, `evaluate` at 610),
`backend/live/engine.py::LiveTradingEngine` (warmup at 2849, live listener at
3069, session lifecycle around 3787, daily counters at 1612/1643),
`backend/strategy/research_lab.py::_ResearchBase` (roll/ATR/bracket at 88/116/136),
and `backend/strategy/exit_policy.py::{resolve_exit_policy,evaluate_exit_operation}`
(lines 111/182). The Rust workspace has a shared execution-state core and a
native VP breakout/retest adapter; Python supplies the current UI, terminal,
strategies, clocks, backtest fill model, and bridge transport.

| Strategy or system area | Planned native Rust slice | Native Rust implementation | Differentially verified against Python | Integrated entrypoints today | Next gate |
|---|---|---|---|---|---|
| Shared preset and parameter contract | One versioned resolver with explicit defaults, enums, unit economics, and provenance | Runtime CLI accepts hand-authored `RuntimeConfig`/strategy fixtures; active preset resolution is planned | None | Terminal, Web Live, and Web Backtest each use Python parameter paths | Capture terminal/Web Live/Web Backtest snapshots for every active preset; implement resolver and preserve explicit zero/list semantics |
| `factor` — `emapmo`, `momentum_reversion`, `icefishball` | Port family decisions, warmup, daily caps, entries, and exit parameters | None | None | Python BacktestEngine and LiveTradingEngine; presets `BEST` and `VOLUME PROFILE RESEARCH` resolve to `factor` | Freeze family-specific cohorts, starting with current `BEST` effective params |
| `momentum` | Port session clock, continuation detector, sizing, and exits | None | None | Python BacktestEngine and LiveTradingEngine; preset `MOMENTUM BEST` | Replay year/session boundaries against Python decisions and ledger |
| `betafib` | Port RTH leg/fib state, risk basis, entry windows, and brackets | None | None | Python BacktestEngine and LiveTradingEngine; preset `BETAFIB BEST` | Compare leg formation and touch-entry sequences before broker integration |
| `pi` | Port source selectors, 5-minute ATR blend, exact rounding, per-date source cap, replacement/reopen, and directional exits | `src/pi.rs`, `src/pi_lifecycle.rs`, and `src/pi_signal_builders.rs` provide offline ordinary-entry, ticket, and reopen/replacement signal contracts | Ordinary-entry parity on 217,842 MES 1m bars, 481 canonical marks, and 67 decisions; lifecycle and builder parity on frozen Backtest/Live cases and actual Python methods; no production route | Python BacktestEngine, Web Backtest, terminal Live, Web Live; presets `PI 2MES BOTH BEST` and `PI 2MNQ BOTH BEST` | Compare realized PI fills, exits, risk gates, and ledger before any entrypoint integration |
| `optionwall` | Port option snapshot inputs, wall selection, state transitions, and 60-minute exit | None | None | Python BacktestEngine and LiveTradingEngine; no saved preset in current file | Freeze option-wall inputs and compare directional exit cases |
| `delta_absorption` | Port MBO features/thresholds and ownership of live feed lifecycle | None | None | Python BacktestEngine and LiveTradingEngine; preset `DELTA ABSORPTION BEST`; Databento MBO input | Reproduce a frozen MBO session; quantify feed and feature parity |
| `volume_profile` | Port the Python profile, signal timing, and ATR bracket contract | A Rust VP breakout/retest adapter exists in `vp-replay` and is integrated into the Rust-only CLI; saved preset integration is separate | No Python decision/ledger differential | Python BacktestEngine and LiveTradingEngine; no current preset whose stored strategy mode is `volume_profile` | Compare Python VolumeProfileStrategy with Rust adapter before mapping presets |
| `fade` | Port prior-day value levels and both `prev_day_va`/`or15` variants | None | None | Python BacktestEngine and LiveTradingEngine; no saved preset | Verify previous-day construction, overnight touch, and 15-minute opening range |
| `sigma` | Port rolling distribution, session reset, and same-bar fill semantics | None | None | Python BacktestEngine and LiveTradingEngine; no saved preset | Compare distribution statistics and entry/exit bars across DST and maintenance |
| Retired aliases and residual code | Preserve source-of-truth behavior for old `trend` labels and identify unreachable `confluence` paths | No native path | Not applicable until a caller is demonstrated | `trend` normalizes/falls back to Factor; confluence strategy was removed while some request/status branches remain | Keep out of new strategy registry; remove residual code only after references and invariants are traced |
| Shared clocks, exit policy, risk state, and backtest fills | Native DST-aware clocks, unique exit contract, daily gates/persistence, and deterministic fill/ledger model | `clock.rs`, `exit.rs`, and pure `risk.rs` contracts are implemented; PI source-entry replay resolves exits natively | PI date, directional exit policy, and source decisions match on the frozen cohort; risk boundary unit cases pass; realized-PnL/ledger parity is open | Python clocks/exits/risk remain connected to BacktestEngine and LiveTradingEngine | Compare realized PnL, daily locks, persistence, ownership, partial fills, replacement, and reopen before integration |
| TopstepX execution and live lifecycle | Native REST/stream adapter, account lease, attached OCO, reconciliation, order journal, and fail-closed ownership | Rust execution core and protocol mapping exist; Python TopstepX client currently provides auth/REST/hubs through JSONL bridge | Offline synthetic event scenarios only; actual broker schema/ordering remains unverified | Rust CLI `live` is VP-only and broker effects use the Python transport; Python terminal/Web remain primary | Finish strategy/risk parity, then verify broker contract in a separately authorized integration stage; keep live disarmed |
| Robinhood | Native adapter after TopstepX: capability discovery, paper/sim execution, then explicitly gated live options | Read-only capability report only | None | No options execution integration | Define supported account/option order contract after TopstepX milestones |
| UI and terminal routing | Route Web Backtest, Web Live, and terminal decisions through the Rust service with versioned inputs/results | None | None | All three remain Python entrypoints; Rust is a separate CLI | Add an opt-in local Rust bridge after all strategy-specific differential gates pass |

**Readiness:** the native Rust runtime currently covers generic execution state,
a VP-specific route, ordinary PI entry replay, PI ticket lifecycle, and
reopen/replacement signal builders. PI decisions and lifecycle rules are not
connected to Backtest, terminal, Web, or live execution. The remaining strategy
decisions and production entrypoints stay on Python. Each row advances through
native implementation, Python differential verification, and entrypoint integration.
Keep live disarmed until the selected route passes exact-preset decision, fill,
risk, and broker-contract gates. Data coverage and current quantitative findings remain
in [`QUANT_RESEARCH_STATUS_2026-09-26.md`](QUANT_RESEARCH_STATUS_2026-09-26.md).

## Native Rust migration plan and first PI vertical slice

**OBSERVED** — Python Backtest and Live construct nine registered strategy
modes. `live.rs::run` constructs only `VpBreakoutAdapter` (line 174) and its
coordinator consumes completed bars in `run_event_loop`/`process_completed_bar`
(330/452). The existing Rust core owns generic pending/position/order lifecycle;
Rust live uses the Python TopstepX client, clocks, and exit kernel through
`backend/rust_live_bridge.py`. The PI MES preset and resolver output are frozen
by the checksum at the start of this section.

**INTENDED** — Replace the complete Python Backtest and Live decision
architecture for all registered modes with native Rust strategy, preset,
clock, exit, risk, fill/ledger, and broker contracts. Keep Python as a temporary
parity oracle while each slice advances through native implementation,
differential verification, and entrypoint integration. Migrate TopstepX first,
then Robinhood; route Web and terminal entrypoints after the strategy-specific
gates pass. The first slice implements a native PI decision contract against
canonical PI rows and actual MES 1-minute bars. Native integration remains a
later milestone.

The PI slice preserves the terminal resolver's active continuation selector
`["深蓝圈"]`. It preserves the current session-gate behavior as well:
`one_trade_per_session_direction=true` has constructor assignments at
`live/engine.py:359–360` and `backtest/engine.py:217–218`; runtime gates read
`tr_one_trade_per_session` at `362–363` and `220–221`, set to false in the PI
preset. Rust must reproduce the observed gate until source behavior receives a
separate change.

**EVIDENCE** — Strategy/preset/entrypoint coverage matrix above; `docs/INVARIANTS.md`
PI-001–015, EXEC-001/002/004–007, EXIT-001–003, LIVE-001/002/004–006/010/011;
the read-only preset SHA-256
`4899a94b277bc96f51940ced0fb6e0c5a734edd822578b903912f00643b0e64`; canonical
PI history and the current MES candle store.

**INVARIANTS** — The first PI decision slice touches PI-001–008 and PI-012–015,
EXIT-001/002, LIVE-004/005/006, and the entry intent portion of EXEC-001/002/004.
Subsequent execution slices include EXEC-005–007, EXIT-003, LIVE-001/002/010/011.
Existing focused checks are mapped in `docs/INVARIANTS.md`, including
`test_pi_pre_session_filter.py`, `test_pi_single_source.py`,
`test_pi_live_audit.py`, `test_pi_directional_exits.py`,
`test_pi_reopen_continuation.py`, `test_pi_replacement.py`,
`test_exec_protection_invariants.py`, `test_exit_policy_unification.py`,
`test_live_daily_rest.py`, `test_live_daily_locks.py`, and
`test_live_manual_guardian_integration.py`.

**BEHAVIOUR CHANGE?** — Yes. The Rust framework will add native decisions and
eventually route supported presets through a new execution service. The user
authorized the full migration. Preserve Python-observed behavior in each slice;
record source discrepancies for separate resolution.

**FILES ALLOWED FOR THIS PI SLICE** — New Rust shared modules
`research/rust_engine/crates/live-runtime/src/clock.rs`, `src/exit.rs`,
`src/risk.rs`, and `src/pi.rs`; module exports in `src/lib.rs`; the offline
`pi-replay` CLI path in `src/main.rs`; `tests/test_rust_pi_parity.py`; and this
coverage report. Later slices will add only their named Rust modules/entrypoints
to this allowlist before editing. `backend/rust_live_bridge.py`, Python
production strategy/live/backtest code, `data/presets.json`, and canonical
history remain read-only in this slice.

**TESTS** — Execute a real-data Python/Rust decision differential using the
saved terminal-effective PI params, filtered canonical PI marks, and MES
1-minute candle store. Record the exact source hash, preset hash, cohort dates,
bar/mark/decision counts, reason/time/side/quantity/entry/SL/TP/policy/risk
rows, and the first mismatch. Add boundary checks for Chicago 17:00 date reset,
daily loss/win/profit gates, PI long/short holds, and Python tie-rounding.
Run the focused Python PI checks, offline Rust compile/replay, and paper
lifecycle demonstration. Keep the broad suite and external broker session out.

**ACCEPTANCE** — The ordinary PI source-entry slice is accepted when native
Rust matches `PiSignalStrategy` for push acceptance, source attempts, decision
timestamps, side, quantity, ATR brackets, daily source cap, and resolved exit
policy on the frozen real-data cohort. This slice passes. Continuation,
replacement, realized-PnL/ledger parity, production preset resolution, entrypoint
routing, and broker contracts remain open gates. Keep live disarmed until those
gates pass. Recheck the preset checksum; leave service, account, orders, and
commits untouched.

### PI ordinary-entry milestone result — 2026-09-27

- Effective preset: `PI 2MES BOTH BEST`, source `last_used_live`; two MES,
  `pi_only`, long and short enabled, 5-minute ATR blend, SL multiplier 4,
  target 3R, three generated signals per Topstep trade date, long hold 0,
  short hold 60 minutes. The saved preset SHA-256 remains
  `4899a94b277bc96f51940ced0fb6e0c5a734edd822578b903912f00643b0e64`.
- Canonical PI history SHA-256:
  `0da002d25405144eb285f84c64f3320513574c12ba3efa26a79e32eba2c2ea38`;
  481 filtered source marks span 2026-03-05 through 2026-09-14.
- MES source: `F:\ancserQuant\ancserMarketData\source\futures\continuous_1m\MES_accumulated_1m.pkl`, 223,626,584 bytes, store mtime ns
  `1789408114561170600`. Replay cohort: 217,842 bars from
  2026-02-03 16:28 UTC through 2026-09-14 16:54 UTC.
- Parity: 481/481 push attempts matched (67 accepted, 414 filtered), 67/67
  source attempts matched, and all 67 ordinary-entry decisions matched (28
  buys, 39 sells) on source identity, timestamp, side,
  quantity, reason, ATR blend, risk width, entry, stop, target, trade date, and
  directional exit policy. The first run found a Python `str(datetime)` spacing
  difference in `signal_ts`; Rust formatting was corrected and the complete
  cohort passed on rerun. These 67 are strategy-generated decisions before the
  Backtest engine's active-position, fill, and ownership gates; they are not 67
  executed trades.
- Daily-cap edge parity also passed on real MES session bars with four isolated
  PI test marks: both engines generated the first three and rejected the fourth
  with the configured `daily_signal_cap`. The canonical cohort itself did not
  reach that limit.
- Source review found and corrected a PI-specific exit-policy edge: Python's
  `tr_exit_mode="ladder"` branch applies only to trend/factor. PI keeps the
  single-trail policy when enabled. A Rust unit case now pins that behavior.
- Verification passed: `python -m pytest tests/test_rust_pi_parity.py -q -s`,
  `cargo test --offline -p ancsertpx-live-runtime` (6 unit tests), and the
  offline `paper-demo` (0 real orders; lifecycle gates passed).
- Not covered by this milestone: the saved preset's active 深蓝圈 continuation
  selector and LV2 replacement path; same-fill Backtest ledger parity; daily
  risk transitions against Python; Rust-native preset resolution; TopstepX
  transport replacement; Robinhood; or Web/terminal routing. The candidate
  stability assessment in the 2026-09-26 research report remains unchanged.

## PI lifecycle milestone result — 2026-09-27

**OBSERVED** — `PiSignalStrategy.continuation_enabled()` and
`replacement_source_active()` define source eligibility at `pi_signal.py:332`
and `:374`. The strategy only builds a replacement from a selected fresh BUY
深蓝圈 with structured Level 2 (`:439`) and rebuilds reopen brackets from a
ticket at `:479`. Backtest arms at the 15:45 ET flatten, checks source bracket
touches while flat, invalidates on original SL/TP touch, and allows one same-day
entry within five minutes of the 18:00 ET reopen (`backtest/engine.py:1057–1181`).
Live persists the ticket by account/contract, validates identity on restore,
waits for a flat broker position before replacement entry, and routes the
claimed continuation through ordinary risk/OCO gates (`live/engine.py:1409–1588,
3292–3335, 3972–4065`).

**INTENDED** — Add a shared, pure Rust PI lifecycle contract for continuation
and replacement source eligibility, flatten-ticket creation, ticket
invalidation/expiry, and one-shot reopen claims. Preserve New York DST behavior
and the current Python Backtest/Live gate ordering. Keep broker actions,
account-state reads, and production routing outside this offline slice.

**EVIDENCE** — Current terminal-effective PI MES preset selects long continuation
`["深蓝圈"]`, enables LV2 replacement, and caps reopen gap at 1R. Python
reference tests are `tests/test_pi_reopen_continuation.py` and
`tests/test_pi_replacement.py`; session clock truth is
`backend/strategy/session_filter.py:26–128`; lifecycle invariants are PI-014 and
PI-015 in `docs/INVARIANTS.md`.

**INVARIANTS** — PI-014, PI-015, DST/session behavior in `test_market_clock.py`,
and one-shot signal/position ownership constraints from EXEC-004–007 and
LIVE-004–006. This slice computes ticket state only; it sends no close or entry
action.

**BEHAVIOUR CHANGE?** — Yes for the new Rust contract. Python production
behavior, preset contents, listener, broker client, and live disarm state stay
unchanged.

**FILES ALLOWED FOR THIS LIFECYCLE SLICE** — New
`research/rust_engine/crates/live-runtime/src/pi_lifecycle.rs`; export from
`src/lib.rs`; offline `pi-lifecycle` fixture command in `src/main.rs`;
`tests/test_rust_pi_lifecycle_parity.py`; and this migration report. Python
production code, `data/presets.json`, broker adapters, data files, and live
service state remain read-only.

**TESTS** — Compare source eligibility and ticket transitions with the actual
Python strategy/Backtest methods using frozen active preset selectors. Cover
15:45 ET arm, bracket-touch rejection, same-day stop/target invalidation,
18:00–18:05 claim window including the exact five-minute edge, late and
next-day expiry, one-shot claim, and DST transitions. Exercise live account and
contract ticket identity with synthetic identifiers. Run lifecycle pytest,
Rust unit tests, and the existing targeted PI lifecycle tests; keep real broker
sessions and the broad suite out.

**ACCEPTANCE** — Rust and Python agree on source eligibility, arm/no-arm,
ticket state, expiry/invalidation reason, claim timing, and one-shot behavior
for every frozen scenario. The Rust output is a standalone offline contract;
Backtest/Live integration and replacement/reopen bracket/ledger parity remain
later gates. Keep live disarmed.

**RESULT** — Implemented `src/pi_lifecycle.rs`, exported it from `src/lib.rs`,
and added the offline `pi-lifecycle <fixture.json>` command. The fixture models
the Backtest 15:45 bar gate separately from Live's outer 15:45 clock trigger;
Live may use the completed prior-minute bar while persisting the exact 15:45
flat time. Live account IDs accept the persisted numeric form and compare by
their string identity, while contract identity and selected PI source remain
required. Backtest tickets retain their source-risk field; Live tickets retain
the persisted version/account/contract fields and deserialize without the
Backtest-only risk field or a Rust-only normalized source field.

Differential coverage exercises long and short stop/target touches, source
selectors, exact five-minute claim, late and next-day expiry, one-shot use,
position/order gates, selector/contract mismatch, prior-minute live arm, and
spring-DST reopen timing. `python -m pytest
tests/test_rust_pi_lifecycle_parity.py tests/test_pi_reopen_continuation.py
tests/test_pi_replacement.py -q` passed (17 tests); `cargo test --offline -p
ancsertpx-live-runtime` passed (8 Rust tests); offline `paper-demo` reported
`real_orders_submitted: 0`. Targeted rustfmt could not run because the installed
stable toolchain has no rustfmt component. The strategy queue's full mark
freshness/ATR construction, reopen signal/bracket builder, replacement close
and flat-confirm flow, Backtest fills/ledger, and runtime integration remain
outside this milestone.

## PI signal-builder milestone result — 2026-09-27

**OBSERVED** — `PiSignalStrategy.build_reopen_continuation()` at
`backend/strategy/pi_signal.py:479–545` revalidates the ticket source, computes
the source-risk gap cap, rounds the current close, reads the completed 5-minute
ATR blend, scales short risk, and builds a fresh directional bracket. The
LV2 method at `:439–477` consumes the selected fresh BUY 深蓝圈 Level-2 mark
and calls the same source-signal/bracket path. Rust `pi.rs` already owns its
ATR history and tick rounding, but the ATR state and signal builder are private;
the lifecycle module currently reports mark eligibility only.

**INTENDED** — Add pure Rust constructors for one claimed reopen ticket and one
fresh structured LV2 replacement mark, reusing the existing PI ATR and tick
rounding definitions. Differentially compare eligibility, gap, entry, stop,
target, reason, source metadata, and one-shot consumption against the actual
Python builders. Keep Backtest/Live routing and all broker operations outside.

**EVIDENCE** — `pi.rs` ATR/rounding implementation at `:325–488`;
`tests/test_pi_reopen_continuation.py` covers the Python reopen builder and
`tests/test_pi_replacement.py` covers the fresh LV2 source; PI-014 and PI-015
in `docs/INVARIANTS.md` define the observable behavior. Use active preset
selectors and frozen candles, including short continuation and tick-tie cases.

**INVARIANTS** — PI-014, PI-015, exact shared ATR/tick rounding, and source
one-shot behavior. No position ownership, risk gate, persistence, or broker
action is changed in this slice.

**BEHAVIOUR CHANGE?** — Yes, in the new Rust signal contract. Python production
behavior, presets, bridge, broker adapter, and live arm state remain unchanged.

**FILES ALLOWED** — `research/rust_engine/crates/live-runtime/src/pi.rs`,
`src/pi_lifecycle.rs` for shared ticket/source metadata, a new
`src/pi_signal_builders.rs` if needed, `src/lib.rs`, `src/main.rs` for the
offline fixture command, `tests/test_rust_pi_signal_builders.py`,
`tests/test_rust_pi_lifecycle_parity.py`, and this report. Python production
modules, presets, candle/PI history, and broker/live state remain read-only.

**TESTS** — Run the new Python/Rust differential fixtures plus
`tests/test_pi_reopen_continuation.py` and `tests/test_pi_replacement.py`, Rust
unit tests, and the offline paper demo. Cover both sides, ATR warmup, exact
tick-rounding ties, the 1R gap edge and rejection, missing ATR, selected versus
unselected/fresh versus stale Level-2 marks, and exact output metadata. Do not
run broker sessions or the broad suite.

**ACCEPTANCE** — The native signal output and ticket-consumption result match
Python across every frozen case. Reopen and replacement remain offline-only;
fill/ledger/risk parity and entrypoint integration remain later gates. Keep
live disarmed.

**RESULT** — `src/pi.rs` now exposes one ties-to-even ATR bracket constructor
used by ordinary PI replay and `src/pi_signal_builders.rs`. The new offline
`pi-signal-builders <fixture.json>` command builds reopen and LV2 replacement
signals without connecting a trading entrypoint. Reopen eligibility reads the
preserved ticket `pi` payload, matching the Python builder. Replacement
eligibility reuses the shared lifecycle source selector and ordinary PI queue
filters.

The differential cases cover long and short reopen, exact 1R and over-cap
gaps, tick ties, ATR-not-ready, selector rejection, daily cap, fresh and stale
LV2 marks, visual-only level, Level 1, unselected kind, wrong future,
replacement/continuation source exclusions, and full signal metadata. During
adversarial review, a mismatch between the ticket's structured source and its
preserved `pi` payload was found and corrected; live ticket updates and signal
building now both read the payload Python checks. `python -m pytest
tests/test_rust_pi_signal_builders.py tests/test_rust_pi_lifecycle_parity.py
tests/test_pi_reopen_continuation.py tests/test_pi_replacement.py -q` passed
(18 tests), and `python -m pytest tests/test_rust_pi_parity.py -q` passed (2
tests). `cargo test --offline -p ancsertpx-live-runtime` passed (8 Rust tests);
offline `paper-demo` reported zero real orders. Formatting remains unchecked
because the installed stable toolchain has no rustfmt component. Real ATR
stream parity, Backtest fills/ledger, daily risk, persistence restore, and
production integration remain open.

## Next PI ticket: Backtest fill and trade-ledger parity

**OBSERVED** — `BacktestEngine._process_candle()` updates tickets and strategy
state before position processing (`backend/backtest/engine.py:420–438`). A
new market signal enters at its signal price and checks only SL on that candle
(`:720–729, :875–925`). Later bars check SL/TP, resolving both-touch candles
through the shared `resolve_same_bar_exit()` rule: nearest level to bar open
wins, and exact ties choose SL (`:730–768`,
`backend/backtest/intrabar.py:6`). Closed-trade PnL applies direction, contract
point value, and quantity, then subtracts round-turn commission and fees per
contract (`:989–1015`). Continuation and LV2 preemption have separate close,
claim, and flat-confirm ordering; they remain out of the ordinary fill slice.

**INTENDED** — Add a deterministic Rust PI Backtest fill/ledger simulator for
frozen ordinary PI decisions and bars. Match entry-candle SL-only behavior,
later SL/TP and tie resolution, contract point economics, costs, exit
timestamps, and net realized PnL against production `BacktestEngine`. Keep
signal generation as fixture input and leave continuation/replacement fill
routing outside until ordinary ledger parity is exact.

**EVIDENCE** — Existing shared definitions are
`backend/backtest/intrabar.py::resolve_same_bar_exit`,
`backend/db/models.py::{get_point_value,get_commission_rt,get_fees_rt}`,
`backend/strategy/exit_policy.py::evaluate_exit_operation`, and Rust
`pi_backtest.rs` fill/ledger replay. The Python exit kernel is the unique
operation source; the fixture freezes its per-bar output for Rust to apply.
Rust `exit.rs` resolves the PI policy and does not implement a second active
exit-operation formula. Canonical MES history and PI decisions are hash-pinned
in `tests/test_rust_pi_parity.py`; project handoff and the quant report contain
the active preset and current performance evidence. Use the frozen actual MES
decision cohort plus focused synthetic cases for ambiguous bar paths.

**INVARIANTS** — EXIT-001–003, position ownership/no-overlap, and the shared
conservative same-bar tie rule. The Rust simulator must preserve the boundary
between strategy exit policy and Backtest fill mechanics.

**BEHAVIOUR CHANGE?** — Yes in the new offline Rust Backtest result contract.
Python production behavior and live runtime stay unchanged and disarmed.

**FILES ALLOWED** — New
`research/rust_engine/crates/live-runtime/src/pi_backtest.rs`, export from
`src/lib.rs`, offline `pi-backtest <fixture.json>` command in `src/main.rs`,
new `tests/test_rust_pi_backtest_parity.py`, and this report. Python production
modules, strategy presets, canonical data, and broker files remain read-only.

**TESTS** — Compare the frozen MES decision/bar cohort and synthetic cases for
market entry, entry-candle stop, later target, both SL/TP touch with each
open-distance winner and exact tie, long and short PnL, MES/MNQ point values,
contract quantity, costs, daily-loss flatten, directional time exit, trail
stop, and session flatten. Run the new parity test, targeted PI tests, Rust
tests, and offline `paper-demo`; keep the broad suite and broker sessions out.

**ACCEPTANCE** — Entry/exit timestamps, reason, fill price, quantity,
gross/net PnL, costs, and final position state match Python across the frozen
cohort and focused cases. Daily risk, continuation/replacement fills, and
entrypoint integration remain later gates. Keep live disarmed.

**RESULT** — Added `src/pi_backtest.rs`, exported the offline
`pi-backtest <fixture.json>` command, and added
`tests/test_rust_pi_backtest_parity.py`. The simulator consumes frozen ordinary
PI entries and every Python-kernel operation while a position remains active;
it rejects missing, duplicate, or unused operation rows. It reproduces
entry-candle stop-only fills, subsequent SL/TP fills and nearest-to-open tie
resolution, trailing stop updates, time exits, daily-loss and NY flatten
ordering, end-of-data close, contract economics, ending capital, and the
closed-trade ledger.
The shared active-exit formula remains the Python kernel input for this slice.

The canonical `PI 2MES BOTH BEST` / MES cohort used 217,842 one-minute bars
from 2026-02-03 through 2026-09-14, with the hash-pinned preset and PI history.
All 55 ordinary Python-engine entries and closed trades matched Rust on entry
and exit instants, direction, fill prices, quantity, stop state, gross PnL,
commission, fees, net PnL, and exit reason; 3,289 Python exit-operation rows
were consumed. Twelve synthetic scenarios passed, including entry-bar
stop-only, TP-only entry-bar hold, both-hit SL-nearer/TP-nearer/ties for long
and short, trail then stop, directional timer, NY session flatten, MES/MNQ
economics at quantity 2, and the daily-loss same-bar re-entry edge. Positive
assertions pin those behaviors; negative controls verify rejection of missing,
duplicate, and unused exit-operation events.

Validation: the PI backtest plus PI replay/lifecycle/builders and existing PI
continuation/replacement tests passed (`21 passed`); `cargo test --offline -p
ancsertpx-live-runtime` passed (8 tests); offline `paper-demo` passed with
`real_orders_submitted: 0`; `status` remained in paper mode with entries
disabled pending warm-up and reconciliation; all three exit-operation negative
controls were rejected as expected; `git diff --check` passed. The
preset SHA-256 remains `4899a94b277bc96f51940ced0fb6e0c5a734edd822578b903912f00643b0e64c`.
The stable toolchain still has no rustfmt component. No market archive or
preset was edited, and no broker session, service restart, or commit occurred.

**LUNA MAX REPORT / ASTRA HIGH REVIEW** — Luna Max independently separated
the earlier Rust PI signal-replay result (481 marks pushed, 67 candidate
decisions before Backtest position/risk gates: 28 long and 39 short) from this
ticket's 55 accepted Backtest fills. The fill/ledger result proves parity for
the frozen Python entries and shared-kernel operations; native Rust signal to
fill integration remains the next gate. It also confirmed the quant research
verdict: MES remains closed to live; MNQ strict PI remains a paper/holdout
candidate with roughly six months of evidence. Astra High initially found two
P2 assertion gaps: entry-bar TP-only behavior and ending-capital reconciliation.
Both are covered now. The follow-up review confirmed those closures, no further
directly demonstrated major parity issue, and retained cross-day/DST coverage
as a residual review item. Astra also reviewed the missing, duplicate, and
unused operation negative controls.

## Next PI ticket: causal ATR-blend bar-stream parity

**OBSERVED** — Python `_ResearchBase._roll()` in
`backend/strategy/research_lab.py:88–107` aggregates input 1m candles into
UTC-aligned fixed-width bars and appends a completed bar only when the next
bucket begins. `_bars` retains 400 completed bars. `_atr()` and `_atr_blend()`
at `:109–132` compute rolling 14/50-bar true ranges; ATR-14 becomes available
after 7 completed bars, ATR-50 after 25, ATR-14 supplies the ATR-50 fallback,
and the blend is their mean. Rust `pi.rs::roll_bar`, `atr`, and `atr_blend`
implement the corresponding PI replay state. `tests/test_rust_pi_parity.py`
currently compares ATR only at emitted decisions, so its 67 decision matches
do not establish equality on every input bar.

**INTENDED** — Add an opt-in ATR trace to the existing offline `pi-replay`
fixture/result. Compare Python and Rust after every bar on the frozen canonical
MES cohort: active 5m bucket start/OHLC, completed-bucket count, ATR-14,
ATR-50, and blended value. The ordinary replay response remains unchanged when
trace capture is disabled. This ticket verifies the continuous per-bar feed
used by the current PI replay; BacktestEngine's conditional `observe()` /
`evaluate()` callback schedule remains a separate integration gate.

**EVIDENCE** — Python definitions are
`backend/strategy/research_lab.py::_roll`, `_atr`, `_atr_blend`; Rust
definitions are `research/rust_engine/crates/live-runtime/src/pi.rs::roll_bar`,
`atr`, and `atr_blend`. The canonical frozen input and prior decision-level
parity test are in `tests/test_rust_pi_parity.py`; preset and PI-history hashes
are pinned there. MES source bars currently cover the cohort through
2026-09-14.

**INVARIANTS** — CLOCK-001 (UTC instants and exchange-independent bucket
alignment), PI-006 (canonical PI history source), and the existing single
Python ATR definition. The Rust port must match the Python outputs while the
Python production definition remains the oracle.

**BEHAVIOUR CHANGE?** — Yes, the new opt-in offline replay trace adds an
observable diagnostic result field. Signal, preset, Backtest, Live, and broker
behavior stay unchanged.

**FILES ALLOWED** — `research/rust_engine/crates/live-runtime/src/pi.rs`,
`tests/test_rust_pi_parity.py`, and `docs/RUST_LIVE_RUNTIME.md`. No Python
production modules, presets, canonical history, candle archives, broker paths,
or live entrypoints.

**TESTS** — On the hash-pinned 217,842-bar MES cohort, compare every trace
point, including null warmup values, bucket rollover, ATR-14/50 availability,
400-bar eviction, and the final partial bucket. Add focused synthetic bars
around 5m, UTC-day, and long data-gap boundaries. Preserve the existing
push/source/decision parity assertions. Confirm default replay omits the
optional trace field and the trace-enabled CLI executes end-to-end.

**ACCEPTANCE** — Trace length equals input bar count. Bucket timestamps,
completed counts, and current aggregate OHLC match exactly; ATR-14, ATR-50,
and blend availability match at every index, with finite values within `1e-10`.
Existing PI replay output and decision parity remain identical with tracing
disabled. No live or broker path is invoked.

**RESULT — 2026-09-27** — The hash-pinned MES cohort compared all 217,842
1-minute inputs from `2026-02-03T16:28Z` through `2026-09-14T16:54Z`. Rust and
Python matched every active bucket start, OHLCV aggregate, completed-bar count,
ATR-14, ATR-50, and blend, including null warmup values and the final partial
bucket. The 7-bar ATR-14 and 25-bar ATR-50 availability thresholds matched;
both implementations retained exactly 400 completed bars after reaching the
history limit. A focused synthetic replay also matched through UTC midnight
and a 4h45m input gap, including volume aggregation within the gap's opening
bucket. The default fixture omitted `atr_trace`; the existing canonical
push/source/decision parity still passed with 481 source marks and 67
decisions (28 buy, 39 sell).

Verification: `tests/test_rust_pi_parity.py` passed 3 tests; the combined PI
fill/replay/lifecycle/signal-builder/reopen/replacement group passed 22 tests;
`cargo test --offline -p ancsertpx-live-runtime` passed all 8 Rust unit tests.
The trace is diagnostic and opt-in. This result covers the continuous PI replay
bar feed; it does not establish parity for BacktestEngine's conditional
`observe()` / `evaluate()` schedule or establish a stable trading strategy.

**NEXT GATE** — Feed native PI decisions into Rust fill/ledger replay, then
port and differentially verify BacktestEngine's conditional strategy callback,
position, and daily/session risk gates. Persistence restore and terminal/Web
integration follow. The current quant report keeps MES live closed and treats
MNQ strict PI as a paper/holdout candidate; strategy stability remains an
independent evidence gate.

## Windows commands and config shape

Offline use:

```powershell
Set-Location F:\ancserQuant\ancserTPX\research\rust_engine
cargo run -p ancsertpx-live-runtime --offline -- status
cargo run -p ancsertpx-live-runtime --offline -- paper-demo
cargo run -p ancsertpx-live-runtime --offline -- replay
```

The live config is a separate JSON file. Example shape (replace account `0`
and the contract with the intended account and currently verified front month):

```json
{
  "runtime": {
    "mode": "live",
    "account_id": 0,
    "contract_id": "CON.F.US.MES.U26",
    "tick_size": 0.25,
    "tick_value": 1.25,
    "contract_size": 1,
    "max_signal_age_seconds": 90,
    "max_market_staleness_seconds": 5,
    "max_entry_reference_distance_points": 50.0
  },
  "vp_parameters": {
    "value_area_pct": 0.8,
    "sl_atr": 1.0,
    "tp_atr": 1.5,
    "confirm_bars": 1,
    "breakout_buffer_ticks": 1,
    "touch_tolerance_ticks": 2,
    "reclaim_buffer_ticks": 0,
    "max_trades_per_day": 1,
    "min_source_candles": 10
  },
  "exit_strategy_mode": "factor",
  "exit_params": {
    "trail_enabled": true,
    "trail_trigger_pct": 0.3,
    "trail_sl_ticks": 10,
    "tr_exit_mode": "tp"
  },
  "history_days": 15,
  "minimum_warmup_bars": 250
}
```

The explicit live command is:

```powershell
$LiveConfig = 'F:\ancserQuant\ancserTPX\data\live_runtime\topstepx_mes.json'
cargo run -p ancsertpx-live-runtime --offline -- live $LiveConfig --arm --approve-vp-breakout-atr --confirm-auto-oco
```

The bridge reads `TOPSTEPX_USERNAME` and `TOPSTEPX_API_KEY` from the existing
environment/`.env`; they do not belong in the config. Running the live command
with all three flags can send live orders after startup checks pass. This report
did not run that command.

The pre-existing user edit in `data/presets.json` remains excluded from this
task and untouched. Existing Python Live/Web/terminal routes and presets were
not changed by this work.

## Authorized follow-up scope

The user has now authorized continuing implementation against the Astra
findings. This is a behavior change for the new opt-in Rust runtime only.

**Observed:** `core.rs` can infer untagged children from prices, fill events can
overwrite terminal entry state, and the bridge currently delegates potentially
ambiguous REST actions to retrying client methods. `Journal::open()` has no
exclusive writer guard. The CLI has no live coordinator; `_start_topstepx()`
can discover account/contract/history and acquire the existing account lease,
but no caller drives its returned history and events through strategy/runtime
execution.

**Intended:** fix every confirmed Astra High/Medium execution-state finding;
keep manual/untracked orders and positions outside bot ownership; serialize
journal writers; bind each market price to its own timestamp; account-filter
events before they reach risk state; add an explicitly armed TopstepX live CLI
coordinator that drives sorted warm-up, bar building, the existing VP
breakout/ATR strategy, the shared Python exit kernel, journal, reconciliation,
and guarded broker operations. Keep Robinhood as a second, fail-closed MCP
capability path until an authenticated official options-tool schema is
available. Never claim PI or every preset is supported by this runtime.

**Evidence and invariants:** Astra High's read-only review above, current
`core.rs`, `journal.rs`, `runtime.rs`, `adapters/topstepx.rs`,
`backend/rust_live_bridge.py`, and `backend/broker/topstepx.py`; preserve
EXEC-001/002/004/005/006/007, EXIT-001/002/003, LIVE-001/010/011, and
CLOCK-001/002/003 from `docs/INVARIANTS.md`.

**Files allowed:** this document; `research/rust_engine/Cargo.toml` and
`Cargo.lock`; files under `research/rust_engine/crates/live-runtime/`;
`backend/rust_live_bridge.py`; narrowly scoped TopstepX account/order/event
methods in `backend/broker/topstepx.py`; optional request metadata in
`backend/db/models.py`. No presets, existing strategy engine, API routes,
terminal service, credentials, or market-data archives.

**Demonstrations and acceptance:** no test files were added and no test suite
was run. The paper demonstration now covers the reviewed event-order cases.
Offline compile, replay, paper scenarios, Python syntax, JSONL bridge operations,
CLI gates, and diff checks are the validation scope. The live CLI requires all
arming inputs and reconciles before it can process signals. No broker
authentication, account connection, service restart, real order, or commit was
performed. The remaining live readiness blockers are listed at the top of this
document.

The pre-existing user change in `data/presets.json` was excluded from the
allowlist and left untouched. No tests were added or run, no service was
restarted, and no commit was created. The offline implementation criteria are
complete; authenticated broker integration and strategy evidence remain open.

## OBSERVED

- `research/rust_engine/Cargo.toml:1-3` defines the existing Rust workspace.
  `crates/vp-replay/src/strategy.rs:31-42,68-69,156-157` exposes a reusable
  stateful Volume Profile signal implementation and currently accepts only
  `entry_mode=breakout`, `target_mode=atr`, `side_mode=all`.
  `research/rust_engine/README.md:19-26` classifies the VP study as research
  with execution and prospective gates still open; production adapters were
  previously outside that workspace.
- `backend/broker/topstepx.py:85-90,185-238` provides bounded UTC search windows,
  API-key login, token refresh before expiry, and authenticated REST requests.
  Account/contract discovery exists at `:304-340`; order side/type mapping,
  attached bracket payloads and broker calls exist at `:720-794,796-894`;
  bounded order/trade searches and open-position discovery exist at
  `:896-964`; market SignalR quote/trade subscriptions exist at `:996-1084`.
  The opt-in bridge now adds a user-hub event adapter.
- At initial inspection, `backend/db/models.py:659-670` `OrderRequest` had no
  `customTag` field; the opt-in client now carries it. `backend/live/engine.py:4397-4404,4495-4617,4618-4705`
  constructs attached SL/TP brackets and blocks entries without a market
  reference. `backend/live/engine.py:855-912,914-991` selects and calibrates
  attached Auto OCO child IDs after fill.
- `backend/strategy/exit_policy.py:111-173` is the single exit-policy resolver;
  `:182-290` is the pure exit-operation kernel. Live and backtest currently
  call these shared functions.
- `backend/live/engine_lease.py:1-8,25-80,97-122` holds an OS account lock
  across the lifetime of an engine. Web acquires it at
  `backend/api/routes.py:4773-4795`; terminal acquires the same lease at
  `backend/terminal_live.py:840-845`. Rust ownership must go through this
  exact lease implementation.
- `docs/INVARIANTS.md:27-34,38-42,48,52,54,57-58,65-67` defines the required
  EXEC, EXIT, LIVE, and CLOCK contracts. `docs/HANDOFF.md:887-917` retires the
  Practice-only restriction and records that legacy BEST families failed
  long-term validation (0/60); strategy evidence does not approve live use.
- The working tree already contains a user edit in `data/presets.json`. It is
  preserved and excluded from this task's file allowlist.
- Robinhood's official Agentic Trading support page lists options tools and
  the `https://agent.robinhood.com/mcp/trading` MCP connection. Robinhood's
  developer documentation root currently redirects to crypto trading docs;
  it does not publish a direct options REST contract. The runtime must discover
  and validate MCP tool schemas and require the user's authenticated Agentic
  account before claiming capability.

## Implementation contract

- Added a reusable `ancsertpx-live-runtime` Rust crate in the current research
  workspace. Replay, paper, and broker modes share one deterministic runtime
  state machine and decision interface; separate adapters implement simulated
  fills, TopstepX futures, and Robinhood options capability discovery.
- Strategy coverage is limited to the existing Rust VP breakout/retest
  implementation with ATR targets and both directions. This is a paper/replay
  research path. PI options 60-RTH / +20% to break-even / -50% stop remains
  research-only and is not promoted or connected to TopstepX futures.
- The runtime reuses the existing Rust VP strategy/replay types and the Python
  `resolve_exit_policy()` / `evaluate_exit_operation()` definitions through a
  narrow JSON-lines bridge. The same decisions feed replay, paper, and live
  adapters; no second exit formula is introduced.
- The TopstepX adapter uses the existing `TopstepXClient` for API-key auth,
  bounded history, account/contract discovery, market and user events, order
  lifecycle, fills, positions, and trade history. The bridge acquires
  `LiveEngineLease` for the selected account before the runtime can become
  ready. Main and Express accounts remain eligible; no Practice-only check is
  added.
- Entries require a fresh market reference and request attached Auto OCO SL/TP in
  the same broker request. Side/type mappings remain the documented internal
  `Buy=1/Sell=2` to API `Bid=0/Ask=1`, and internal stop `3` to API stop `4`.
  Child IDs are identified and modified only for those attached children.
- The runtime persists deterministic order intents and broker transitions before/after
  side effects. Ambiguous submits enter an unresolved state and reconcile by
  bounded UTC order search/custom tag; the runtime never blindly resends them.
  Pending entries count against the one-position gate. Partial fills and
  cancel/fill races require broker confirmation. Restart reconciliation occurs
  before new decisions. Unowned/manual positions remain untouched and block new
  entries. Risk locks stop new entries while owned exits continue.
- Default mode is `paper`. Live readiness requires explicit arming plus
  operator-provided account, contract, Auto OCO confirmation, and an explicit
  strategy approval input. This task runs no real orders and leaves the PI
  profitability candidate unapproved.
- Robinhood is represented by a fail-closed MCP adapter boundary. It queries
  the official server's advertised tools/schemas before any operation; missing
  authenticated MCP configuration or unsupported tool schemas are surfaced as
  actionable blockers. No guessed REST endpoint, browser automation, crypto
  endpoint substitution, or mock-live label is permitted.

## EVIDENCE

- Current Rust workspace and VP strategy source: `research/rust_engine/Cargo.toml`,
  `research/rust_engine/README.md`, and `research/rust_engine/crates/vp-replay/src/`.
- Existing TopstepX client and execution contracts: `backend/broker/topstepx.py`,
  `backend/db/models.py`, `backend/live/engine.py`.
- Shared exit and ownership contracts: `backend/strategy/exit_policy.py`,
  `backend/live/engine_lease.py`, `backend/api/routes.py`,
  `backend/terminal_live.py`.
- Frozen PI options core, read-only reference:
  `F:\ancserQuant\ancserMarketData\derived\research\pi_option_rust_wf_mc_20260925T203916Z\src\lib.rs`.
- Official broker references checked 2026-09-26:
  - ProjectX/TopstepX auth: `https://gateway.docs.projectx.com/docs/getting-started/authenticate/authenticate-api-key/`
  - Realtime user/market hubs: `https://gateway.docs.projectx.com/docs/realtime/`
  - Attached bracket semantics and order enums:
    `https://gateway.docs.projectx.com/docs/api-reference/order/order-place/`
  - Bounded order search:
    `https://gateway.docs.projectx.com/docs/api-reference/order/order-search/`
  - Robinhood supported agent options tools and MCP onboarding:
    `https://robinhood.com/us/en/support/articles/trading-with-your-agent/`
    and `https://robinhood.com/us/en/support/articles/agentic-trading-overview/`.
    `https://docs.robinhood.com/` currently resolves to crypto trading docs.

## INVARIANTS AFFECTED

- Preserve EXEC-001/002 mappings; EXEC-004 attached protection; EXEC-005
  attached OCO child calibration only; EXEC-006 market-reference block;
  EXEC-007 bounded UTC order search; EXEC-003 remains retired.
- Preserve EXIT-001/002 through the existing shared Python resolver/kernel;
  preserve EXIT-003 adapter separation and tracked-position ownership.
- Preserve LIVE-001 history warm-up without entries, LIVE-004/006 new-entry-only
  risk locks, LIVE-010 cross-process single-account lease, LIVE-011 manual
  position ownership.
- Preserve CLOCK-001 UTC instants, CLOCK-002 existing flatten/cancel windows,
  and CLOCK-003 separate Topstep risk-day boundary.
- No existing default, Python engine route, active preset, or live service
  behavior changes.

## BEHAVIOUR CHANGE?

Yes — a new, explicitly selected Rust runtime and broker adapters are
authorized. Its default is paper, existing production paths stay unchanged,
and real orders are outside this task's verification.

## FILES ALLOWED

- `docs/RUST_LIVE_RUNTIME.md`
- `research/rust_engine/Cargo.toml`
- `research/rust_engine/Cargo.lock`
- `research/rust_engine/crates/live-runtime/Cargo.toml`
- `research/rust_engine/crates/live-runtime/src/lib.rs`
- `research/rust_engine/crates/live-runtime/src/main.rs`
- `research/rust_engine/crates/live-runtime/src/` modules for config, core,
  journal, events, and the paper/TopstepX/Robinhood-MCP adapters
- `research/rust_engine/crates/live-runtime/fixtures/` offline replay/paper
  fixtures
- `backend/db/models.py` (optional `custom_tag` request field only)
- `backend/broker/topstepx.py` (optional custom tag and user-hub bridge methods
  only; retain all existing public behavior)
- `backend/rust_live_bridge.py` (minimal JSON-lines bridge using current client,
  lease, strategy parameters, and exit functions)

Excluded: `data/presets.json`, `.env`, runtime credentials, frozen market-data
archives, the PI Rust reference file, `backend/live/engine.py`, API routes,
terminal runner, and all other application/UI files. These paths remain outside
the implementation.

## TESTS

No test files added or changed, and no test suite run. Validation is limited to
the explicitly requested workspace compile plus offline replay, paper, broker
protocol, and fail-closed capability demonstrations. No broker authentication,
account discovery, service restart, or real order is performed.

## Acceptance record

1. Targeted `cargo check -p ancsertpx-live-runtime --offline` passes. The full
   workspace check was not requested in this follow-up.
2. CLI status/help identify paper as default; a `live` command without the
   complete explicit flag set refuses before reading the config or spawning the
   Python bridge.
3. Offline replay and paper lifecycle scenarios run. The adversarial paper
   output reports all seven added ownership/fill/freshness/account scenarios as
   passing, and the journal rejects a second writer while the first is active.
4. The TopstepX adapter and coordinator compile. Validation never invokes the
   live command with all flags and never authenticates or submits a real order.
5. Robinhood remains fail-closed because the required authenticated options MCP
   schema was not available to this runtime.
6. `data/presets.json` remains a pre-existing user modification and was not
   edited during this work. No commit was created.
