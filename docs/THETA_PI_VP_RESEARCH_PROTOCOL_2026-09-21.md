# Theta / PI / Volume Profile: research protocol

Date: 2026-09-21. Scope: research and data acquisition only. Production presets, execution and live account controls stay unchanged.

## Decision we want to make

Can point-in-time option information improve the existing PI or Volume Profile strategy after costs on the same eligible dates? Separately, does buying a listed QQQ/SPY option after a PI event have positive, repeatable expectancy?

These are separate experiments. A profitable underlying move does not establish a profitable option purchase. A useful option-volatility feature does not establish that the signed GEX proxy measures dealer inventory.

## Current evidence and limits

The prior reports `RESEARCH_PI_OPTIONS_SELECTION_2026-09-21.md` and `RESEARCH_PI_VP_NEXT_EVIDENCE_2026-09-20.md` are the starting inventory, subject to the new audit. They describe predominantly QQQ 0DTE quotes, insufficient for the proposed overnight option study; six explicit QQQ deep-blue long LV2 marks; and nine VP trades joined to the limited GEX sample. These counts are historical inventory, not new Theta results.

Previously inspected 2026 performance is retrospective. New tuning must not relabel it as untouched holdout. A fresh forward period starts only after hypotheses and parameters are frozen.

## Acquisition priorities

| Priority | Coverage | Purpose |
|---|---|---|
| Pilot | QQQ and SPY, one completed session, listed expirations, quotes, OI and available IV/Greeks | Verify entitlement, timestamps, usable prices, size and speed |
| 1 | Actual canonical PI event sessions and the following five trading sessions; listed expirations covering next session and approximately 3/7 calendar DTE | Executable next-day option and path studies |
| 2 | Daily chains/OI, initially 2021 onward; near-term 0–60 DTE intraday context on matched research dates | VP/PI regime comparison with documented partial-chain scope |
| 3 | Expand intraday context and older history after pilot coverage/cost checks | Increase independent days and market regimes |

Use official trading calendars and actual listed contracts. A holiday or an unavailable historical daily expiration must not generate a synthetic option. Expiry buckets retain actual calendar DTE and trading-session distance separately. Acquire all strikes for chosen small expiry cohorts where feasible; contract selection must use information available at the event time.

Canonical raw target: `F:/ancserQuant/ancserMarketData/source/options/thetadata`. Initial storage ceiling: 300 GiB; preserve at least 80 GiB free on F. Prioritize useful coverage and resumable requests. Whole-market OPRA ticks are outside this first scope. No subscription upgrade or duplicate raw mirror is authorized.

Each request needs a redacted manifest: endpoint/version, parameters, retrieval timestamp, effective dates, rows/contracts, compressed bytes and SHA-256. Record empty, missing, permission-denied and transient failure separately. Validate a completed artifact before skipping it on resume. Never put credentials in manifests or reports.

Theta's REST route uses a local Terminal. The current Python SDK supports direct access with an API key, Python >=3.12 and SDK >=1.0.9; prioritize this route for the user's configured key. Keep its runtime isolated from the app's Python environment. Sources: [current SDK documentation](https://thetadata.net/docs/Python-Library/Getting-Started.html), [release announcement](https://www.thetadata.net/blog/2026-05-05-introducing-the-theta-data-python-library).

The published Standard matrix includes IV/first-order Greeks; second-order Greeks are listed under Pro. Actual account probes determine this account's access. Separate stock/index history access must also be checked. Sources: [pricing](https://thetadata.net/pricing), [subscription matrix](https://www.thetadata.net/docs/Articles/Getting-Started/Subscriptions.html).

## Point-in-time data contract

- PI events come from canonical `load_rows()`, retaining symbol, kind, explicit source level, event ID and timestamp. Visual bubble size cannot supply a missing level. Preserve replay/dedup rules.
- Keep source timestamp and actual receipt timestamp distinct. Missing receipt time makes a historical source-time experiment retrospective; show 1/5-minute entry-delay sensitivity.
- Join each option/underlying observation at or before the decision time. Place simulated orders at the next available quote after the decision; minute-end snapshots cannot fill an order earlier within that minute.
- Store OI effective session separately from its publication/observation date. Theta describes morning publication around 06:30 ET reflecting the previous trading day's close. Verify historical endpoint date semantics before shifting dates. Source: [Theta OI documentation](https://docs.thetadata.us/operations_excel/option_snapshot_open_interest.html).
- Keep exchange timezone, DST and early closes explicit. A futures overnight signal may have no executable ETF-option market; defer to the declared next option session and report the delay.
- Track option multiplier/deliverable, quote age, bid/ask size, crossed/locked market, zero bids, IV failure, missing underlying and corporate actions. A genuine zero bid is an economic outcome, distinct from absent data.
- A regular 1-minute snapshot timestamp alone does not prove the age of its underlying NBBO update. Mark raw-update age unknown until tick-level evidence is available. Validate selected entry/exit windows with narrow tick requests; never label all minute snapshots as fresh solely because their grid timestamp matches the order minute.

## Experiment A: options after PI

Primary cohort: each bullish/bearish PI event, with kind and source level retained independently; unknown level receives its own group. Evaluate QQQ and SPY separately.

Two entry schedules answer the user's two possible interpretations:

1. First tradable quote after the signal; contract expires in the next listed trading session.
2. Next trading session at 09:35 ET; select a fresh contract using that moment's spot and quotes.

Primary selection is ATM, with exits at next-session 10:00 and 15:30 ET as separate predeclared outcomes. Approximate 3/7-calendar-DTE contracts are the expiry sensitivity. The user's $3/$10 OTM alternatives are explicit challengers, with actual moneyness and delta reported. Avoid multiplying tiny LV2 groups into a large optimized grid. Delta-selected variants need reliable point-in-time delta first.

Use entry ask, exit bid, contract multiplier, commissions and adverse-fill stress. Display both one-contract P&L and a fixed premium-risk-budget portfolio; round down contract counts and allow zero contracts when premium exceeds the budget. Long-option research assumes premium can be entirely lost. Exits occur before expiry/exercise cutoffs; failures to exit need an explicit settlement/delivery model. ETF options have American-style exercise and physical-delivery considerations: [OIC](https://www.optionseducation.org/referencelibrary/faq/options-exercise).

Report premium return, net dollars, win rate, mean/median, loss-of-premium frequency, tail loss, trade count, independent event days and uncertainty. The event study may contain overlapping observations; the executable portfolio enforces the repository's single-position constraint. Do not present independent event results as a directly achievable equity curve.

Path outcomes at 1/2/3/5 sessions: direction-adjusted return, MFE/MAE, first adverse move before favorable threshold, first favorable move before adverse threshold, and time to recovery. Predeclare threshold sizes in underlying percent or decision-time ATR. Bars hitting both thresholds have unknown ordering without finer data and must retain an ambiguity category. Report all-event denominators as well as the subset that eventually recovers.

## Experiment B: option context for PI and VP

First freeze current preset JSON/code/data hashes and reproduce baseline trades one by one. Prior preset results with different continuation/replacement settings cannot serve as the current control.

On exactly the same eligible dates, compare only four initial ablations:

1. Frozen baseline.
2. Baseline plus signed OI-gamma proxy / concentration context.
3. Baseline plus IV, expected-move and quote-liquidity context.
4. Baseline plus both feature families.

Missing option data belongs to an explicit unknown state. Report coverage and excluded baseline profits/losses; a selected subset cannot be compared to all baseline dates. Later add MBO/order-flow/delta-decay only on the actual intersecting sample and show the incremental effect over the option-only model.

### Gamma interpretation

For a declared sign convention, a dollar-gamma-per-1%-move proxy is the sum of `sign_assumption * OI * gamma * multiplier * spot^2 * 0.01`. A call-positive/put-negative convention is an inventory assumption. Long calls and long puts both have positive mathematical gamma. Public OI does not reveal each dealer's side, opening/closing transactions or full inventory.

Actual dealer long gamma can induce contrarian hedging; short gamma can induce momentum hedging. This motivates hypotheses about volatility and persistence, with direction supplied by price/PI evidence. Cboe illustrates both the mechanism and the importance of actual positioning: [Cboe research](https://www.cboe.com/insights/posts/volatility-insights-evaluating-the-market-impact-of-spx-0-dte-options).

Keep signed proxy, unsigned concentration, 0DTE contribution and expiry buckets separate. A 0–60 DTE chain is a partial-chain measure. Volume-weighted gamma is a flow-activity proxy. Gamma-flip estimates depend on the model, included strikes/expiries and assumed IV behavior under spot shocks.

Existing `scripts/option_wall_demo.py::_gamma` uses a zero-rate/dividend European-style approximation. Reuse existing shared calculations where appropriate; quantify its limitations for ETF/dividend/longer-dated contracts before adoption. Vendor gamma and reconstructed gamma require provenance/version labels and spot/IV timestamp checks. Avoid quietly replacing failed gamma values with zero.

A possible consistency check available within first-order data is the European-model identity `gamma = vega / (spot^2 * IV * T)`, where vega is per unit volatility and T is the model's remaining year fraction. If supplied vega is per one volatility percentage point, convert its units first. Vendor time-to-expiry floors, rate/dividend/model conventions and American-exercise treatment must be aligned before applying the identity. This is an unimplemented model-consistency check, not independent evidence of actual dealer gamma or permission to label all reconstructed values as vendor gamma.

### Small set of useful hypotheses

- VP: does value-area acceptance plus momentum persist more under negative signed-proxy conditions? Does rejection/reclaim work better under positive conditions? Test the interaction against acceptance/rejection alone.
- PI: does remaining implied move, relative to the distance already traveled, distinguish late expensive option entries from signals with room to develop?
- PI: does IV/term structure explain weak option returns despite correct next-day underlying direction? Compare longer DTE on the same events.
- Both: do distance to concentrated strikes and partial-chain concentration improve outcomes beyond ordinary realized volatility and time of day?
- Both: compare signed-GEX additions against unsigned concentration and IV-only controls. Incremental predictive value is the criterion.

For QQQ-to-MNQ and SPY-to-MES context, use normalized same-time distances/returns and test basis mismatch. Raw ETF strikes cannot be compared directly to futures price levels.

ATR blend scales backward-looking realized volatility. Option-implied move may add a forward-looking risk estimate, but both can miss shocks. Initially test risk classification and entry filtering; leave live stops unchanged. Any later volatility blend uses decision-time values and must not widen an already-set loss budget silently.

## Validation and promotion gates

Chronological splits reuse the project's shared segmentation helper. Keep all variants of one event in the same fold; purge training labels overlapping validation horizons. Fit thresholds on training data only. Cluster uncertainty by trading day because signals, contracts and overlapping holding periods are dependent.

Present chronological-fold performance, costs/slippage stress, drawdown, tail loss, missed winners and parameter plateaus. Thirty events per cell is a warning-screen minimum, with uncertainty and independent days still decisive; the five canonical QQQ long LV2 events support case studies only. Record all attempted variants to expose selection effects.

Promotion requires a reproducible baseline, point-in-time data audit, meaningful cost-net improvement across multiple periods, and a frozen prospective paper test. Research completion does not change live execution or authorize a new preset.

## Deliverables and ownership

- Luna / Plato: ingestion script, resumable raw manifests, entitlement/pilot report, storage accounting.
- Luna / Boyle: existing-data PI path and VP/GEX evidence audit with sample counts and coverage limits.
- Main agent: this protocol, review of data quality and experiment interpretation, synthesis and next decision.

Actual download progress and new empirical results belong in the ingestion/audit reports. This document defines the experiment and makes no new profitability claim.

### Authenticated API checkpoint, 2026-09-22

The project `.env` key authenticated successfully through the isolated Python 3.12.14 / Theta SDK 1.0.10 runtime. Returned subscription codes: options=2, stocks=0, indices=0. A restricted-network attempt returned `ConnectError`; the approved official-network probe succeeded. SDK authentication logging was disabled because its info-level success message includes session credentials.

Actual historical probes for QQQ 2026-03-31 558 call on 2026-03-30:

- Quotes: five 1-minute records for 15:01–15:05 ET; at 15:02 bid 3.42, ask 3.43, bid size 4, ask size 22.
- OI: one record, publication timestamp 2026-03-30 06:30:00.796 ET, open interest 75. Preserve that returned publication clock.
- First-order Greeks: two records for 15:02–15:03 ET. At 15:02 delta 0.5006, IV 0.29, IV error 0, underlying 557.89 with matching underlying timestamp.
- Second-order Greeks: `PERMISSION_DENIED`, with subscription-related error classification. No subscription upgrade was performed.
- A ten-second tick-quote probe for the same contract (15:01:55–15:02:05 ET) returned 1,061 records with millisecond timestamps. This supports targeted execution-window QC without downloading full-chain tick history. The minute snapshot ask was 3.43 at 15:02; the last returned tick before 15:02:05 had ask 3.49, illustrating why a snapshot fill remains an execution assumption.

These are read-only connectivity/entitlement samples; they do not establish full-chain coverage, stored download completion or option strategy profitability. Full stock/index history is outside the observed subscription codes; option first-order output supplies a synchronized underlying sample for this tested contract.

### Stored pilot QC, 2026-09-22

The subsequent six-request pilot stored 742,268 rows in 12,754,696 compressed bytes. Main-agent independent readback matched every saved SHA-256 and manifest row count. EOD/OI contain 5,232/5,084 contract rows; each selected-expiry quote/first-order file contains 182,988 rows covering 468 contracts. This is one event and two observation dates.

Raw full-chain quality counts: March 30 quotes have 54,114 zero-bid rows, 468 zero-ask rows and zero crossed rows; March 31 has 70,261 zero-bid rows, 468 zero-ask rows and 35 crossed rows. These include untradeable contracts/times and need explicit screening at selected entry/exit points. Zero-bid economic exits must remain distinguishable from missing observations.

First-order files contain 37,347/37,184 rows with nonpositive IV on the two dates. Nonzero `iv_error` is common and alone does not define unusable data. March 30's median absolute error field is 0.0026 and its 95th percentile is 100; vendor error semantics and failure sentinels require validation before a full-chain model-gamma calculation. At the selected 15:02 call strikes 558/561/568, underlying is 557.89 for all three and IV is 0.29/0.2863/0.2734. Quote-P&L measurement can proceed independently of an IV quality threshold.

## Working-tree checkpoint

At 2026-09-21 22:35 America/Los_Angeles, observed SHA-256 values were:

| File | SHA-256 |
|---|---|
| data/presets.json | 03238142994996865B837C54CBD7D8706E0E96D049AE1293E5786815C70533A7 |
| backend/strategy/pi_signal.py | 5F51A8BAFAFD058BA9737BF80C066399543BE1AF592F2189C6C55E023AB40B86 |
| backend/strategy/volume_profile.py | 727F76B91E3071DE866F5BC3984B23A838A474E50B1D589922BD2E69F7C73E03 |
| backend/backtest/engine.py | 7AB48C4ECE78B60EA14805161451701ED16EBBF27FFB12B4B364CDD83618D4A6 |

These identify the inspected dirty working tree; trade-by-trade baseline reproduction is still required for a new strategy comparison. No preset or production change was made by this protocol work.

Requested baseline command `python -m pytest tests/ -q` completed in 120.60 seconds: 765 passed, 8 subtests passed, 7 failed. Three failures concern Git ownership/safe-directory checks; three concern attempts to write the external futures store from the restricted sandbox; one is the previously documented Volume Profile terminal-builder round-trip returning `factor`. Research work does not fix or claim clearance of these failures. Do not grant broad store writes just to make this test run green; isolate tests from live data before any such rerun.
