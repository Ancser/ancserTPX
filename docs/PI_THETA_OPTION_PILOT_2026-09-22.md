# PI / Theta QQQ Option Bid-Ask Pilot

**Run date:** 2026-09-22  
**Cohort:** five canonical QQQ source-level-2 deep-blue PI events  
**Contract set:** one ATM call, one $3 OTM call, and one $10 OTM call per event  
**Exits:** next listed session at 10:00 ET and 15:30 ET  
**Status:** retrospective source-time quote study; one-contract marks only

## Method and event timestamps

Each event timestamp comes from `backend.data.pi_history.load_rows()` followed by the existing `canonical_buy_signals` and `expected_entry` helpers. Entry is the next eligible one-minute timestamp after the canonical PI source timestamp. Theta manifest `event_context` timestamps are informational metadata and are excluded from event selection.

The cohort contains **five independent event dates**. The March 30 case contains six displayed outcome rows because it combines three contracts with two scheduled exits; those rows represent one event day.

| Event date | Canonical source ET | Derived entry ET | Exit / expiry | Entry spot | Actual call strikes: ATM / $3 OTM / $10 OTM |
|---|---|---|---|---:|---|
| 2026-03-30 | 15:01 | 15:02 | 2026-03-31 | 557.89 | 558 / 561 / 568 |
| 2026-06-05 | 15:01 | 15:02 | 2026-06-08 | 709.29 | 709 / 713 / 720 |
| 2026-06-09 | 12:03 | 12:04 | 2026-06-10 | 698.83 | 699 / 702 / 709 |
| 2026-07-20 | 15:18 | 15:19 | 2026-07-21 | 696.86 | 697 / 700 / 707 |
| 2026-07-24 | 15:19 | 15:20 | 2026-07-27 | 683.18 | 683 / 687 / 694 |

The moneyness rule selects the listed call nearest spot for ATM and the lowest listed call strike at or above spot plus $3 or $10. The 2026-06-09 positive-entry regression test confirms the canonical 12:03 → 12:04 ET derivation and entry asks 7.20 / 5.64 / 2.87.

## Bid-to-ask P&L results

Each cell is `entry ask → scheduled exit bid; gross P&L / P&L after $1.30 round-trip commission sensitivity`. The calculation uses one long call and an explicit 100-share contract multiplier assumption. Default script output remains gross with commission parameter 0.00.

### Next session 10:00 ET

| Event date | ATM | $3 OTM | $10 OTM |
|---|---:|---:|---:|
| 2026-03-30 | 3.43 → 8.88; +$545.00 / +$543.70 | 2.11 → 7.25; +$514.00 / +$512.70 | 0.45 → 2.40; +$195.00 / +$193.70 |
| 2026-06-05 | 6.28 → 6.31; +$3.00 / +$1.70 | 4.23 → 3.69; -$54.00 / -$55.30 | 1.76 → 0.90; -$86.00 / -$87.30 |
| 2026-06-09 | 7.20 → 8.76; +$156.00 / +$154.70 | 5.64 → 6.61; +$97.00 / +$95.70 | 2.87 → 2.80; -$7.00 / -$8.30 |
| 2026-07-20 | 4.08 → 8.26; +$418.00 / +$416.70 | 2.58 → 5.78; +$320.00 / +$318.70 | 0.54 → 1.32; +$78.00 / +$76.70 |
| 2026-07-24 | 5.04 → 4.37; -$67.00 / -$68.30 | 3.00 → 2.00; -$100.00 / -$101.30 | 0.88 → 0.21; -$67.00 / -$68.30 |

Fixed one-contract net sums across the five event dates are **+$1,048.50 ATM, +$770.50 $3 OTM, and +$106.50 $10 OTM**.

### Next session 15:30 ET

| Event date | ATM | $3 OTM | $10 OTM |
|---|---:|---:|---:|
| 2026-03-30 | 3.43 → 18.31; +$1,488.00 / +$1,486.70 | 2.11 → 15.28; +$1,317.00 / +$1,315.70 | 0.45 → 8.33; +$788.00 / +$786.70 |
| 2026-06-05 | 6.28 → 6.54; +$26.00 / +$24.70 | 4.23 → 2.85; -$138.00 / -$139.30 | 1.76 → 0.01; -$175.00 / -$176.30 |
| 2026-06-09 | 7.20 → 0.14; -$706.00 / -$707.30 | 5.64 → 0.02; -$562.00 / -$563.30 | 2.87 → 0.00; -$287.00 / -$288.30 |
| 2026-07-20 | 4.08 → 11.31; +$723.00 / +$721.70 | 2.58 → 8.31; +$573.00 / +$571.70 | 0.54 → 1.51; +$97.00 / +$95.70 |
| 2026-07-24 | 5.04 → 1.02; -$402.00 / -$403.30 | 3.00 → 0.04; -$296.00 / -$297.30 | 0.88 → 0.00; -$88.00 / -$89.30 |

Fixed one-contract net sums across the five event dates are **+$1,122.50 ATM, +$887.50 $3 OTM, and +$328.50 $10 OTM**. Positive afternoon net-event counts are **3/5 ATM, 2/5 $3 OTM, and 2/5 $10 OTM**. Excluding the March 30 event, the afternoon net sums are **-$364.20, -$428.20, and -$458.20**, respectively.

These aggregates are descriptive one-contract sums with equal event weighting. Equal-capital comparison is omitted; such a comparison requires an explicit whole-contract budget and rounding rule.

## Bid-path MFE / MAE and immediate entry bid

The path diagnostic includes the immediate entry bid as its first bid observation, then uses valid bid marks through each scheduled exit. It measures dollar excursions relative to entry ask with the 100 multiplier assumption.

| Event date | 15:30 ATM MAE | 15:30 $3 OTM MAE | 15:30 $10 OTM MAE |
|---|---:|---:|---:|
| 2026-03-30 | -$95 | -$69 | -$18 |
| 2026-06-05 | -$201 | -$232 | -$175 |
| 2026-06-09 | -$711 | -$562 | -$287 |
| 2026-07-20 | -$59 | -$43 | -$17 |
| 2026-07-24 | -$497 | -$299 | -$88 |

The March 30 immediate entry bids were 3.42 / 2.09 / 0.44. The path includes them even when a later bid supplies the worst excursion.

## Zero bids, missing rows, and spreads

- Entry coverage: **15/15** selected rows present and valid. Entry missing and invalid flags are zero.
- Scheduled exit coverage: **30/30** selected timestamp rows present. Missing exit flags are zero.
- Two scheduled marks show an observed economic zero bid: the 2026-06-09 and 2026-07-24 $10 OTM calls at 15:30 ET. Each row has `bid=0.00`, `ask=0.01`, positive ask size, and `bid_size=0`. The pipeline retains the row as a conservative zero-value mark, sets `no_executable_bid=true`, and applies the full premium loss plus the $1.30 sensitivity. These rows carry no executed positive-bid exit.
- Zero bid and missing quote are separate states. A missing timestamp/key receives `exit_quote_missing=true`; an observed zero bid retains its row and economic value.
- 09:30 boundary rows with `bid=0`, `ask=0`, and zero sizes fail mark-quality validation. They remain data-quality observations and stay out of the bid path, preventing a false overnight -100% mark.
- Positive bid with zero bid size remains mark-only. It receives no executable-bid flag while remaining eligible for conservative path marking when the ask and ask size form a valid book.
- Entry spreads range from $0.01 to $0.04. The broadest scheduled spreads occur on March 30 at 10:00 ET: ATM $3.12, $3 OTM $1.25, and $10 OTM $0.28. The report uses exit bids, preserving the conservative side of those spreads.
- Quote age remains unknown. One-minute snapshot timestamps do not expose the last underlying NBBO update time.

## Saved-input verification

Root: `F:\ancserQuant\ancserMarketData\source\options\thetadata`

The pipeline verified **30 manifest-backed files**, each with `status=downloaded`, an existing gzip artifact, and an exact manifest row count. The six-file totals by episode are:

| Event date | Verified rows | Compressed bytes |
|---|---:|---:|
| 2026-03-30 | 742,268 | 12,754,696 |
| 2026-06-05 | 542,448 | 10,790,292 |
| 2026-06-09 | 528,716 | 10,797,598 |
| 2026-07-20 | 590,696 | 10,316,357 |
| 2026-07-24 | 701,230 | 13,506,382 |
| **Total** | **3,105,358** | **58,165,325** |

Quote and first-order Greek files use the selected next-session expiry and both rights. EOD and open-interest files are retained for pilot provenance and inventory verification. No Theta network request or authentication was performed by this study.

## Interpretation and limits

The study contains five independent source dates, fifteen contract selections per scheduled exit, and thirty scheduled bid marks. It is a source-time retrospective quote replay. It supplies concrete bid/ask coverage, spread, zero-bid, missing-row, and one-contract P&L evidence. It supplies no production integration, portfolio sizing result, event-study win-rate claim, or best-contract recommendation. The contract multiplier is an explicit standard ETF assumption and remains unverified from the saved files.

## Reproduction

```text
python scripts/pi_theta_option_pilot.py --event-date 2026-03-30 --commission-round-trip 1.30 --compact
python scripts/pi_theta_option_pilot.py --event-date 2026-06-09 --commission-round-trip 1.30 --compact
python -m pytest tests/test_pi_theta_option_pilot.py -q
```

Files:

- `scripts/pi_theta_option_pilot.py`
- `tests/test_pi_theta_option_pilot.py`
- `docs/PI_THETA_OPTION_PILOT_2026-09-22.md`
