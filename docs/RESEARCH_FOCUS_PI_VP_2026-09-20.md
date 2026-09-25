# PI / Volume Profile research focus — 2026-09-20

## Scope and decision

Audit uses the current working-tree code and saved research outputs. The worktree already contains unrelated changes; they remain untouched. No tests, backtests, data requests, purchases, or production edits were made. This report is the sole new file. Production-code allowlist for this strategic turn: **empty**.

**PROPOSED focus:** (1) PI event lifecycle, separating live open-position carry from fresh-signal handling; (2) a small Volume Profile acceptance / rejection / no-trade study. Keep GEX as an incremental context feature. Production behavior changes remain unauthorized.

## 1. PI lifecycle — current behavior

`PiListener` polls 06:30–13:00 Pacific, equivalent to 09:30–16:00 ET during the current daylight-saving period (`backend/live/pi_listener.py:14,359,710`). Its engine callback pushes accepted events into `PiSignalStrategy`; the bounded queue holds at most 32 (`backend/live/engine.py:3036`, `backend/strategy/pi_signal.py:241,474`). Source age defaults to five minutes and stale events are dropped during `evaluate()` (`backend/strategy/pi_signal.py:36,289,516`).

The market clock labels 15:30–15:45 ET `PRE_FLATTEN` and 15:45–18:00 `FLATTEN` (`backend/strategy/session_filter.py:108-115`). At 15:30 the engine cancels an existing pending order (`backend/live/engine.py:3935`), then continues to ordinary strategy evaluation until 15:45 (`:4035`). The inspected PI preset has `tr_allowed_sessions: null` and `pi_max_signal_age_min: 5` (`data/presets.json:242,280`), so its session setting leaves fresh 15:30–15:44 signals eligible for evaluation. Runtime order timing in that interval remains unverified.

From 15:45 until 18:00, the close handler observes the candle, handles flattening, and returns before strategy evaluation (`backend/live/engine.py:3912`). A fresh event received through 16:00 can remain queued; its source age exceeds five minutes by 18:00, so the next evaluation discards it. The only reopen path is a one-use ticket for a selected bot-owned position that is open at the 15:45 flatten; ticket creation and same-day / bracket-touch invalidation are in `backend/live/engine.py:1420,1474` and `docs/INVARIANTS.md:98`. **Current result:** selected open-position carry can re-enter at 18:00; deferred fresh-signal revalidation is absent. Messages arriving after the listener window are not polled that day.

PI blockers include pre-session replay filtering, symbol/kind/side selectors, duplicate or aggregate-message rejection, the five-minute source-age check, ATR warm-up, allowed-session filtering, and the engine’s daily/full-TP/session-direction risk gates (`backend/live/pi_listener.py:91,215-261`; `backend/strategy/pi_signal.py:438-538`; `backend/live/engine.py:4117+`). Structured source `level` is retained separately from visual `size` (`backend/live/pi_listener.py:180,197,250-261`). Long entry/continuation selection is kind-based; its long path does not select a source Level. Explicit Level 1/2 gating applies to short purple circles (`backend/strategy/pi_signal.py:355-367,457-460`). This leaves long Level-2 selection as an audit/research question.

## 2. Volume Profile — model limits and simpler test

The profile builder assigns each candle’s total volume uniformly to every tick from its rounded low through high, using integer-truncated per-tick allocations (`backend/strategy/volume_profile.py:215-246`). This is an OHLCV range proxy; it supplies no observed trades-at-price distribution. The model carries the previous complete RTH 70% profile, with range rejection, confirmed-breakout/later-retest, and failed-break states. The current selected candidate is breakout, two closes, 2-tick buffers, ATR SL 1.5 / TP 2.0 (`docs/HANDOFF.md:48-80`). Five-year MNQ training PF 0.946 and 2026 holdout PF 0.914; no candidate passed the full robustness gate (`docs/HANDOFF.md:65-80`).

An accepted signal locks that edge for the displayed profile day; a closed trade leaves the lock in place, a canceled order releases it, and a new profile day resets it (`backend/strategy/volume_profile.py:601-640,716,841`). Research output groups trades by edge, with no episode-level repeated-cross/re-arm metric. The next study should count edge episodes, acceptance/rejection, and explicit no-trade outcomes before proposing any re-arm rule.

The saved MBO context covers 26 RTH days, 22 VP trades, 18 complete MBO joins, and 9 pre-entry GEX snapshots. The breakout-flow gate had zero hits. Its conjunctive rule requires aligned delta and OFI, 5-minute volume ratio ≥1.25, and decay ratio ≥0.75 (`scripts/volume_profile_context_study.py:181-211,576`). In the saved 18-row sample only one trade reached the volume threshold; that row’s decay ratio was 0.075. The ratio currently divides absolute delta summed over the last two minutes by the first three-minute sum; steady per-minute pressure therefore yields 2/3 mechanically (`:166-168,204`). Test a per-minute-normalized version beside the frozen legacy metric. These short, viewed 2026 samples remain exploratory; the next holdout must be chronologically later and untouched.

## 3. Data and GEX — proposed acquisition order

**First reuse what exists.** QQQ option artifacts already reside under `ancserMarketData/source/options/qqq_option_ml` and `derived/option_wall_demo`; the handoff records 208 sessions of mixed hourly / demo snapshots. A source search found no Massive or ThetaData adapter in `backend/` or `scripts/`. The current context study maps QQQ GEX to MNQ, so use normalized state and distances rather than asserting a fixed QQQ-to-NQ wall-price conversion.

For research, consider one month of ThetaData Options Pro only after a sample-day entitlement check. The current retail page lists Pro at $160/month and Standard at $80; historical second-order Greeks (including gamma, vanna, charm) are Pro-tier, while open interest reports prior-day positions around 06:30 ET. Confirm personal-use licensing and QQQ date/quote coverage before subscribing ([pricing](https://www.thetadata.net/pricing), [tier access](https://docs.thetadata.us/Articles/Getting-Started/Subscriptions.html), [second-order Greeks](https://docs.thetadata.us/operations/option_history_greeks_second_order.html), [OI timing](https://docs.thetadata.us/operations/option_history_open_interest.html)). For new real-time snapshots, Massive Advanced is listed at $199/month; Starter/Developer are 15-minute delayed, and the snapshot endpoint lists no historical entitlement. It needs a recorder for future point-in-time snapshots ([chain snapshot and plan access](https://massive.com/docs/rest/options/snapshots/option-chain-snapshot)). No purchase is authorized here.

Track contract/underlying, call-put, strike, exact expiry, multiplier, known OI and publication time, bid/ask and quote time, gamma/IV calculation time, and underlying price/time. A signed dollar-gamma-per-1%-move proxy can be `0.01 × S² × Σ(sign × gamma × known OI × multiplier)`; call-positive / put-negative signs encode an assumed dealer inventory, not observed dealer positions. Prioritize absolute gamma concentration, 0DTE share, and normalized distance to large-gamma strikes. Positive-gamma dampening and negative-gamma amplification are regime hypotheses; they supply no directional forecast. A gamma flip requires repricing gamma under candidate spot/IV assumptions. Existing futures MBO is sufficient for the first profile comparison; trades-at-price supports profile/delta work, MBP-1 supports top-of-book OFI, and full MBO is warranted for queue/cancel/replenishment questions ([Databento trades](https://databento.com/docs/schemas-and-data-formats/trades), [MBP-1](https://databento.com/docs/schemas-and-data-formats/mbp-1)).

## 4. 1MNQ slow-hold benchmark

The saved diagnostic covers 1,668 RTH sessions (2020-01-02–2026-09-11), one MNQ, 1-minute entries, canonical per-contract costs, and a 14-tick stress. Its 10:30 `open_to_entry` rule reports +$17,819 / PF 1.10 through 15:59, versus +$18,995 / PF 1.12 through 15:44; stress PFs are 1.03 and 1.04 (`daily_direction_hold_mnq_current.md/json`). Production begins its close phase at 15:45 (`backend/strategy/session_filter.py:108-115`). The study marks fixed close prices and costs; it models no stop path, intrabar MLL, or production bracket execution (`scripts/daily_direction_hold_study.py:70-75,230-246`). It is full-sample diagnostic evidence, not an expectancy estimate. One contract sets dollar exposure; contract count adds no directional edge.

## 5. Follow-on research tickets

### Ticket A — PI lifecycle audit

- **OBSERVED:** 15:30–15:44 evaluation can remain eligible; 15:45–18:00 fresh events age out; reopen ticket applies to selected open-position carry.
- **INTENDED:** Compare current baseline, active carry, and a research-only 18:00 revalidation of fresh source events across chronological folds, preserving source time, actual receive time, invalidation, expiry, and risk gates.
- **EVIDENCE:** Current source cited above; saved PI continuation study is short and exploratory (`docs/HANDOFF.md:14-33`).
- **INVARIANTS:** PI-001/004/014, CLOCK-002, LIVE-004.
- **BEHAVIOUR CHANGE?** No; research only.
- **FILES ALLOWED:** `scripts/pi_reopen_continuation_study.py`, `tests/test_pi_reopen_continuation.py`, `tests/test_pi_pre_session_filter.py`, and an external report under `F:\ancserQuant\ancserMarketData\derived\research\`. Production allowlist: empty.
- **TESTS:** Focused event-timing, queue-expiry, selector, and invalidation tests.
- **ACCEPTANCE:** Reproduce the current path first; compare all variants on common source events and chronological folds, with risk gates unchanged.

### Ticket B — VP acceptance / rejection / no-trade audit

- **OBSERVED:** OHLCV range allocation drives levels; edge locks are day-scoped; MBO gate has zero hits and a length-biased decay ratio.
- **INTENDED:** On existing overlap, compare proxy profiles with trades-at-price profiles; report episode-level acceptance, rejection, no-trade, and edge re-arms. A/B baseline, flow only, GEX only, and both on common coverage; keep signed GEX secondary.
- **EVIDENCE:** Saved VP context report and 26-day MBO overlap; 2026 labels have already been viewed.
- **INVARIANTS:** DATA-011–013; profile causality and lock tests in `tests/test_volume_profile_strategy.py`.
- **BEHAVIOUR CHANGE?** No; research only.
- **FILES ALLOWED:** `scripts/volume_profile_context_study.py`, a new `scripts/volume_profile_acceptance_study.py`, `tests/test_volume_profile_context.py`, a new focused study test, and an external report under `F:\ancserQuant\ancserMarketData\derived\research\`. Production allowlist: empty.
- **TESTS:** Causality, trades-at-price aggregation, normalized-delta arithmetic, and no-trade classification tests.
- **ACCEPTANCE:** Freeze the current candidate; use a later untouched chronological block, common coverage, costs, intrabar MLL, day-cluster uncertainty, and neighbor-parameter stability before any promotion.
