# QQQ PI theta exit-path study — 2026-09-22

## Result

This bounded offline replay covers five independent canonical QQQ level-2 PI dates, three same-expiration call selections per date, and four predeclared exit rules. It produces 60 scenario rows: 58 exit quotes have positive bid size and 2 are mark-only economic-zero bids. The two mark-only rows are the $10 OTM 15:30 exits on 2026-06-09 and 2026-07-24. They remain in the economic mark totals and are excluded from executable-bid totals.

The study is descriptive evidence for the saved episodes. It does not select a bracket or make a preset recommendation. Quote age is unknown. A positive bid size establishes an execution-eligible quote in the saved row; it does not prove that a historical order filled.

The ATM aggregate illustrates the exit-timing tradeoff: bracket A totals **+$427.50** per one-contract case across five dates, compared with **+$1,048.50** for the fixed 10:00 exit and **+$1,122.50** for the fixed 15:30 exit. On 2026-03-30, bracket A stopped the ATM call at 15:26 for **-$87.30** per contract; the same saved path later showed **+$543.70** at 10:00 and **+$1,486.70** at 15:30. These observations support the tradeoff description and do not establish a chosen winner.

## Inputs and provenance

- Canonical PI source: `F:\ancserQuant\ancserMarketData\source\discord\pi\pi_signals.json`
- Saved Theta root: `F:\ancserQuant\ancserMarketData\source\options\thetadata`
- Six verified files per episode: `coverage_manifest.jsonl`, EOD, OI, event quote/Greeks, and next-session quote/Greeks. Quote paths use `raw/QQQ/quote_1m/date=YYYY-MM-DD/expiration=YYYY-MM-DD.csv.gz`; first-order Greek paths use the corresponding `greeks_first_order_1m` directory.
- Canonical timestamps come from the PI loader and canonical signal helpers. The Theta manifest event-context time is not used for the other four episodes.
- All timestamps and scheduled exits below are America/New_York. The replay is source-time retrospective; quote arrival age and point-in-time availability are unknown.

| Event date | PI source | Derived entry | Next-session expiry / exit date | ATM / $3 OTM / $10 OTM strikes | Verified rows | Compressed bytes |
|---|---:|---:|---:|---:|---:|---:|
| 2026-03-30 | 15:01 | 15:02 | 2026-03-31 | 558 / 561 / 568 | 742,268 | 12,754,696 |
| 2026-06-05 | 15:01 | 15:02 | 2026-06-08 | 709 / 713 / 720 | 542,448 | 10,790,292 |
| 2026-06-09 | 12:03 | 12:04 | 2026-06-10 | 699 / 702 / 709 | 528,716 | 10,797,598 |
| 2026-07-20 | 15:18 | 15:19 | 2026-07-21 | 697 / 700 / 707 | 590,696 | 10,316,357 |
| 2026-07-24 | 15:19 | 15:20 | 2026-07-27 | 683 / 687 / 694 | 701,230 | 13,506,382 |
| **Total** |  |  |  |  | **3,105,358** | **58,165,325** |

## Predeclared replay rules

1. Buy the same next-session-expiry QQQ call selected by the existing pilot: ATM is the listed strike nearest synchronized entry spot; $3 and $10 OTM are the lowest listed calls at or above spot plus the requested amount.
2. Enter at the canonical signal-next-minute ask. Entry ask size is the entry-side eligibility flag. The entry bid is included as the first bid observation for bracket detection, so a wide entry spread can trigger an immediate observed stop.
3. Baselines sell at the exact next-session 10:00 and 15:30 bid.
4. Bracket A stops at bid <= 75% of entry ask or takes at bid >= 150%. Bracket B stops at bid <= 50% or takes at bid >= 200%. The first observed mark-eligible bid wins, including the entry bid; the actual observed bid is retained for gap outcomes. Both brackets cap at next-session 15:30.
5. A mark-eligible row has a finite nonnegative bid, a positive ask and ask size, and no crossed or malformed price. A row with bid size <= 0 is mark-only. A positive bid with zero bid size remains an economic mark and cannot be counted as executable.
6. Zero/invalid-ask 09:30 boundary markers are excluded from the bid path. The zero bid is retained when the ask and ask size make the row an economic mark.
7. Gross one-contract P&L is `(exit bid - entry ask) * 100`. Reported net subtracts the `$1.30` round-trip fee per contract. The standard ETF multiplier of 100 is an explicit assumption; contract-definition verification is outside this saved-data study.
8. Fixed-budget sensitivity uses `floor($1,000 / (entry ask * 100))` whole contracts independently for each date and selection. Budget totals sum those per-case whole-contract outcomes; they do not form an equal-risk portfolio.

## Event-by-event outcomes

Each cell is `exit timestamp ET; observed bid; net per contract after $1.30; status`. `exec` means positive bid size. `mark-only` means the economic mark is retained and no executable exit is claimed.

| Event | Selection, entry | Baseline 10:00 | Baseline 15:30 | Bracket A | Bracket B |
|---|---|---|---|---|---|
| 2026-03-30 | ATM 558, entry 15:02 ask 3.43 | 2026-03-31 10:00; 8.88; +543.70; exec | 2026-03-31 15:30; 18.31; +1,486.70; exec | 2026-03-30 15:26; 2.57; stop; -87.30; exec | 2026-03-31 09:31; 7.48; take; +403.70; exec |
| 2026-03-30 | $3 OTM 561, entry 15:02 ask 2.11 | 2026-03-31 10:00; 7.25; +512.70; exec | 2026-03-31 15:30; 15.28; +1,315.70; exec | 2026-03-30 15:12; 1.58; stop; -54.30; exec | 2026-03-31 09:31; 5.04; take; +291.70; exec |
| 2026-03-30 | $10 OTM 568, entry 15:02 ask 0.45 | 2026-03-31 10:00; 2.40; +193.70; exec | 2026-03-31 15:30; 8.33; +786.70; exec | 2026-03-30 15:12; 0.31; stop; -15.30; exec | 2026-03-31 09:31; 1.18; take; +71.70; exec |
| 2026-06-05 | ATM 709, entry 15:02 ask 6.28 | 2026-06-08 10:00; 6.31; +1.70; exec | 2026-06-08 15:30; 6.54; +24.70; exec | 2026-06-05 15:18; 4.54; stop; -175.30; exec | 2026-06-08 11:18; 12.57; take; +627.70; exec |
| 2026-06-05 | $3 OTM 713, entry 15:02 ask 4.23 | 2026-06-08 10:00; 3.69; -55.30; exec | 2026-06-08 15:30; 2.85; -139.30; exec | 2026-06-05 15:16; 3.12; stop; -112.30; exec | 2026-06-08 10:59; 8.60; take; +435.70; exec |
| 2026-06-05 | $10 OTM 720, entry 15:02 ask 1.76 | 2026-06-08 10:00; 0.90; -87.30; exec | 2026-06-08 15:30; 0.01; -176.30; exec | 2026-06-05 15:16; 1.22; stop; -55.30; exec | 2026-06-08 10:05; 0.83; stop; -94.30; exec |
| 2026-06-09 | ATM 699, entry 12:04 ask 7.20 | 2026-06-10 10:00; 8.76; +154.70; exec | 2026-06-10 15:30; 0.14; -707.30; exec | 2026-06-09 12:17; 5.22; stop; -199.30; exec | 2026-06-09 12:41; 3.60; stop; -361.30; exec |
| 2026-06-09 | $3 OTM 702, entry 12:04 ask 5.64 | 2026-06-10 10:00; 6.61; +95.70; exec | 2026-06-10 15:30; 0.02; -563.30; exec | 2026-06-09 12:16; 4.16; stop; -149.30; exec | 2026-06-09 12:39; 2.81; stop; -284.30; exec |
| 2026-06-09 | $10 OTM 709, entry 12:04 ask 2.87 | 2026-06-10 10:00; 2.80; -8.30; exec | 2026-06-10 15:30; 0.00; -288.30; mark-only zero bid | 2026-06-09 12:16; 2.02; stop; -86.30; exec | 2026-06-09 12:39; 1.37; stop; -151.30; exec |
| 2026-07-20 | ATM 697, entry 15:19 ask 4.08 | 2026-07-21 10:00; 8.26; +416.70; exec | 2026-07-21 15:30; 11.31; +721.70; exec | 2026-07-21 09:31; 9.13; take; +503.70; exec | 2026-07-21 09:31; 9.13; take; +503.70; exec |
| 2026-07-20 | $3 OTM 700, entry 15:19 ask 2.58 | 2026-07-21 10:00; 5.78; +318.70; exec | 2026-07-21 15:30; 8.31; +571.70; exec | 2026-07-21 09:31; 6.57; take; +397.70; exec | 2026-07-21 09:31; 6.57; take; +397.70; exec |
| 2026-07-20 | $10 OTM 707, entry 15:19 ask 0.54 | 2026-07-21 10:00; 1.32; +76.70; exec | 2026-07-21 15:30; 1.51; +95.70; exec | 2026-07-20 15:58; 0.40; stop; -15.30; exec | 2026-07-21 09:31; 1.88; take; +132.70; exec |
| 2026-07-24 | ATM 683, entry 15:20 ask 5.04 | 2026-07-27 10:00; 4.37; -68.30; exec | 2026-07-27 15:30; 1.02; -403.30; exec | 2026-07-27 09:31; 8.91; take; +385.70; exec | 2026-07-27 10:21; 2.46; stop; -259.30; exec |
| 2026-07-24 | $3 OTM 687, entry 15:20 ask 3.00 | 2026-07-27 10:00; 2.00; -101.30; exec | 2026-07-27 15:30; 0.04; -297.30; exec | 2026-07-27 09:31; 5.52; take; +250.70; exec | 2026-07-27 10:04; 1.42; stop; -159.30; exec |
| 2026-07-24 | $10 OTM 694, entry 15:20 ask 0.88 | 2026-07-27 10:00; 0.21; -68.30; exec | 2026-07-27 15:30; 0.00; -89.30; mark-only zero bid | 2026-07-27 09:31; 1.42; take; +52.70; exec | 2026-07-27 09:44; 0.41; stop; -48.30; exec |

## Aggregate comparison

All rows below use the `$1.30` fee. “Observed mark net” includes mark-only rows at their saved bid. “Executable-bid net” includes rows with positive bid size. Every row has five independent event dates. The budget contract count is the sum of five per-case whole-contract counts, each based on an independent $1,000 premium budget.

| Selection | Scenario | n / executable / mark-only | Positive observed net cases | One-contract observed net | One-contract executable-bid net | $1,000 contracts summed | $1,000 observed-mark net | $1,000 executable-bid net |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| ATM | baseline 10:00 | 5 / 5 / 0 | 4 | +1,048.50 | +1,048.50 | 7 | +2,008.90 | +2,008.90 |
| ATM | baseline 15:30 | 5 / 5 / 0 | 3 | +1,122.50 | +1,122.50 | 7 | +3,330.90 | +3,330.90 |
| ATM | bracket A | 5 / 5 / 0 | 2 | +427.50 | +427.50 | 7 | +843.90 | +843.90 |
| ATM | bracket B | 5 / 5 / 0 | 3 | +914.50 | +914.50 | 7 | +1,821.90 | +1,821.90 |
| $3 OTM | baseline 10:00 | 5 / 5 / 0 | 3 | +770.50 | +770.50 | 13 | +2,688.10 | +2,688.10 |
| $3 OTM | baseline 15:30 | 5 / 5 / 0 | 2 | +887.50 | +887.50 | 13 | +5,244.10 | +5,244.10 |
| $3 OTM | bracket A | 5 / 5 / 0 | 2 | +332.50 | +332.50 | 13 | +1,354.10 | +1,354.10 |
| $3 OTM | bracket B | 5 / 5 / 0 | 3 | +681.50 | +681.50 | 13 | +2,469.10 | +2,469.10 |
| $10 OTM | baseline 10:00 | 5 / 5 / 0 | 2 | +106.50 | +106.50 | 59 | +4,429.30 | +4,429.30 |
| $10 OTM | baseline 15:30 | 5 / 3 / 2 | 2 | +328.50 | +706.10 | 59 | +16,301.30 | +18,148.50 |
| $10 OTM | bracket A | 5 / 5 / 0 | 1 | -119.50 | -119.50 | 59 | -567.70 | -567.70 |
| $10 OTM | bracket B | 5 / 5 / 0 | 2 | -89.50 | -89.50 | 59 | +2,509.30 | +2,509.30 |

## Missing and quality accounting

- Entry selection succeeded for all 15 calls. The study accepts a buy-at-ask row when ask and ask size are valid, including a possible zero bid size; all 15 saved entries also had positive bid size, so that bounded relaxation changed no result in this sample. The five-date result does not project that observation to a larger cohort.
- No selected quote was missing or unusable at the two scheduled baselines or at the bracket cap. No case was skipped because its entry premium exceeded the `$1,000` budget; every case purchased at least one whole contract under the stated multiplier assumption.
- The two mark-only exits are scheduled 15:30 rows with `bid=0`, positive ask and ask size, and `bid_size=0`: 2026-06-09 $10 OTM 709 and 2026-07-24 $10 OTM 694. Their net values are economic marks. They are excluded from executable-bid counts and executable-bid net totals.
- 09:30 zero/invalid-ask boundary markers are excluded before first-touch evaluation. They do not create artificial overnight -100% stops.
- All bracket touches in these five episodes have positive bid size. No bracket row is mark-only.

## Reproduction and limits

Run the study with the saved archive:

```text
python scripts/pi_theta_exit_path_study.py --data-root F:\ancserQuant\ancserMarketData\source\options\thetadata --commission-round-trip 1.30 --budget 1000 --compact
```

The implementation is [scripts/pi_theta_exit_path_study.py](../scripts/pi_theta_exit_path_study.py), with focused coverage in [tests/test_pi_theta_exit_path_study.py](../tests/test_pi_theta_exit_path_study.py). It reuses the existing pilot’s canonical episode derivation, strike selection, quote-quality classification, and bid-path excursion helper.

This is five dates and 15 entry cases. The selections within each date share the same underlying episode, so the rows are not 15 independent market events. The fixed-budget figures are a whole-contract sensitivity with independent per-case budgets. Quote age, order latency, queue position, slippage beyond the observed bid, and actual fills remain unobserved. The output supports descriptive path comparison only.
