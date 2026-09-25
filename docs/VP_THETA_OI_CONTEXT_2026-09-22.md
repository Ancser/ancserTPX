# VP / QQQ OI context join — 2026-09-22

## Scope and conclusion

This bounded offline study joins saved QQQ monthly open interest to the saved
22-row MNQ Volume Profile baseline. It reads only the August and September
2026 QQQ monthly OI partitions, compares each raw vendor publication timestamp
with the VP entry timestamp in UTC, and leaves the saved trade stream unchanged.

The join yields 21 same-ET-date OI rows and one explicitly stale prior-date row.
All 22 trades therefore have timestamp-eligible OI structure within the bounded
files. The stale row is 2026-09-07, whose latest available OI publication is
2026-09-04. The older saved GEX context has 9 timestamp-eligible rows, 7 rows
whose entry precedes the saved same-date snapshot, and 6 dates without an older
OI-GEX snapshot.

The 22-row baseline is 13 long / 9 short over 17 dates, with five two-trade
date clusters and twelve single-trade dates. The saved baseline is PnL
`-$147.28`, PF `0.8754`. The OI joins are descriptive context labels. They do
not filter, rerun, or improve the VP strategy.

## Inputs and source semantics

| Input | Path | Observed coverage |
|---|---|---:|
| Saved VP trades | `F:\ancserQuant\ancserMarketData\derived\research\volume_profile_context_mnq_mbo_gex.csv` | 22 rows / 17 dates |
| QQQ monthly OI, August | `F:\ancserQuant\ancserMarketData\source\options\thetadata\raw\QQQ\monthly_open_interest_dte_0_60\month=2026-08.csv.gz` | 139,268 rows / 929,769 compressed bytes |
| QQQ monthly OI, September | `F:\ancserQuant\ancserMarketData\source\options\thetadata\raw\QQQ\monthly_open_interest_dte_0_60\month=2026-09.csv.gz` | 78,784 rows / 533,338 compressed bytes |
| Archive manifest | `F:\ancserQuant\ancserMarketData\source\options\thetadata\coverage_manifest.jsonl` | latest rows and hashes recorded |
| Older GEX context | `F:\ancserQuant\ancserMarketData\derived\research\volume_profile_context_mnq_mbo_gex.csv` and the saved QQQ snapshot source | existing 9-row timestamp-eligible comparison |

The two raw OI partitions contain 218,052 valid QQQ rows and 63,884 zero-OI
rows. Zero OI is retained as a value. No same-day EOD file is used. The raw
vendor timestamps span 2026-08-03 through 2026-09-21 and are concentrated near
06:30 ET; the comparison retains the original timestamp strings and also stores
normalized UTC copies.

The manifest labels OI effective sessions
`unknown_until_calendar_join`. The join keeps the effective OI session unknown
while preserving raw vendor timestamps. The OI universe is the saved monthly
partial-chain DTE 0–60 scope, with expirations before the VP trade date
excluded.

The official [NYSE 2026 holiday and early-close calendar](https://www.nyse.com/trade/hours-calendars)
lists Monday 2026-09-07 (Labor Day) as closed for NYSE markets. The saved
2026-09-07 VP row is therefore marked `ETF options market closed; prior Fri OI
context only`. Its VP entry timestamp is 12:05 ET. The row documents a saved
futures/VP observation; it does not establish executable ETF-option availability.
The CME holiday schedule is a separate market-calendar question and this audit
makes no exact CME close assertion.

## Join policy

1. Parse the vendor OI timestamp as an aware timestamp, normalize the comparison copy to UTC, and retain the raw value.
2. Keep only OI rows with publication timestamp `<= entry UTC`.
3. Prefer rows whose publication date in `America/New_York` equals the VP trade date. If no such eligible publication exists, use the latest earlier ET publication date and label the row `prior_date_stale`.
4. Exclude same-date rows published after entry. Select the latest eligible observation per `(expiration, strike, right)` contract key.
5. Compute unsigned total OI, put/call OI ratio, calendar-date expiry share for `0 <= expiration - trade_date <= 1`, and strike HHI after combining call and put OI by strike.
6. Use no dealer sign. Put/call values describe contract-right totals; they do not identify dealer inventory.

The `<=1-day` expiry share uses calendar dates. The code performs no
exchange-calendar conversion to trading days, so holiday and weekend effects
remain in that feature. The OI metrics describe the partial saved chain and the
saved universe, rather than total market-wide OI.

## Exact per-trade rows

`OI publication UTC` shows the selected raw-publication range after the
point-in-time filter. `OI total` is unsigned OI. `P/C` is put OI divided by call
OI. `Near1` is the calendar `<=1` expiry share. HHI is strike-level OI HHI.
The older GEX status is independent of the new OI status.

| Trade date | Entry ET | Dir | PnL | OI status / snapshot ET date | OI publication UTC | OI total | P/C | Near1 | HHI | Older GEX |
|---|---|---:|---:|---|---|---:|---:|---:|---:|---|
| 2026-08-07 | 10:05 | long | +218.76 | same / 2026-08-07 | 10:30:00.572–10:30:34.737 | 5,946,196 | 1.2323 | 0.1620 | 0.015216 | matched |
| 2026-08-10 | 09:32 | long | -69.74 | same / 2026-08-10 | 10:30:00.000–10:30:33.678 | 5,362,502 | 1.2048 | 0.0822 | 0.014854 | future excluded |
| 2026-08-11 | 09:43 | short | +103.26 | same / 2026-08-11 | 10:30:00.606–10:30:33.706 | 5,496,498 | 1.2040 | 0.0738 | 0.014807 | future excluded |
| 2026-08-14 | 11:01 | short | +129.26 | same / 2026-08-14 | 10:30:00.000–10:30:34.000 | 5,877,686 | 1.2448 | 0.1291 | 0.014550 | matched |
| 2026-08-17 | 09:33 | long | -63.74 | same / 2026-08-17 | 10:30:00.566–10:30:33.967 | 6,271,961 | 1.1987 | 0.0767 | 0.014368 | future excluded |
| 2026-08-19 | 09:34 | long | -105.24 | same / 2026-08-19 | 10:30:00.647–10:30:34.172 | 6,791,727 | 1.2005 | 0.0815 | 0.014117 | future excluded |
| 2026-08-19 | 10:40 | short | -198.24 | same / 2026-08-19 | 10:30:00.647–10:30:34.172 | 6,791,727 | 1.2005 | 0.0815 | 0.014117 | matched |
| 2026-08-21 | 10:24 | short | -128.24 | same / 2026-08-21 | 10:30:00.511–10:30:39.262 | 6,984,657 | 1.2571 | 0.2668 | 0.014133 | matched |
| 2026-08-21 | 12:12 | long | +30.76 | same / 2026-08-21 | 10:30:00.511–10:30:39.262 | 6,984,657 | 1.2571 | 0.2668 | 0.014133 | matched |
| 2026-08-25 | 10:35 | long | +25.76 | same / 2026-08-25 | 10:30:00.456–10:30:40.000 | 5,779,678 | 1.2300 | 0.0836 | 0.013809 | matched |
| 2026-08-26 | 09:45 | long | -103.74 | same / 2026-08-26 | 10:30:00.000–10:30:40.318 | 5,747,125 | 1.2114 | 0.0656 | 0.013945 | future excluded |
| 2026-08-26 | 12:11 | short | -104.74 | same / 2026-08-26 | 10:30:00.000–10:30:40.318 | 5,747,125 | 1.2114 | 0.0656 | 0.013945 | matched |
| 2026-08-28 | 09:51 | long | +101.26 | same / 2026-08-28 | 10:30:00.000–10:30:40.651 | 6,037,930 | 1.2963 | 0.1341 | 0.013652 | future excluded |
| 2026-08-28 | 12:20 | short | +12.76 | same / 2026-08-28 | 10:30:00.000–10:30:40.651 | 6,037,930 | 1.2963 | 0.1341 | 0.013652 | matched |
| 2026-08-31 | 10:25 | long | +160.76 | same / 2026-08-31 | 10:30:00.066–10:30:39.554 | 5,688,997 | 1.4007 | 0.1692 | 0.014112 | matched |
| 2026-09-02 | 09:50 | short | -88.24 | same / 2026-09-02 | 10:30:00.225–10:30:39.778 | 5,694,850 | 1.5860 | 0.0767 | 0.013867 | future excluded |
| 2026-09-03 | 09:36 | long | +130.26 | same / 2026-09-03 | 10:30:00.349–10:30:39.282 | 5,812,714 | 1.5626 | 0.1333 | 0.014280 | missing |
| 2026-09-04 | 09:45 | long | +121.76 | same / 2026-09-04 | 10:30:00.510–10:30:39.939 | 5,881,952 | 1.6000 | 0.1150 | 0.014107 | missing |
| 2026-09-07 | 12:05 | long | -48.74 | prior stale / 2026-09-04; ETF options market closed; prior Fri OI context only | 10:30:00.510–10:30:39.939 | 5,205,734 | 1.6486 | 0.0285 | 0.015349 | missing |
| 2026-09-08 | 12:48 | short | -90.74 | same / 2026-09-08 | 10:30:00.517–10:30:38.337 | 5,557,521 | 1.6612 | 0.0838 | 0.014284 | missing |
| 2026-09-08 | 13:06 | long | -98.24 | same / 2026-09-08 | 10:30:00.517–10:30:38.337 | 5,557,521 | 1.6612 | 0.0838 | 0.014284 | missing |
| 2026-09-09 | 09:49 | short | -82.24 | same / 2026-09-09 | 10:30:00.502–10:30:38.194 | 5,620,670 | 1.6731 | 0.0669 | 0.014307 | missing |

The 2026-09-07 row is the only stale OI join. The bounded September file has
no eligible same-date publication for that trade, so the latest earlier raw
publication, 2026-09-04, is retained and labelled. The NYSE closure explains
the absence of an ETF-options session for that date; it does not change the
raw OI timestamp or prove an OI effective-session lineage.

## Availability comparison with older GEX

All slices retain the same 22 saved trades, directions, dates, and saved PnL.
The older GEX status `future excluded` means an older same-date snapshot exists
after the trade entry and is excluded by its own timestamp rule. `Missing` means
the older snapshot source has no timestamp-eligible OI-GEX row for that trade.

| Slice | Trades / dates | Directions | PnL | PF |
|---|---:|---|---:|---:|
| All saved baseline | 22 / 17 | 13 long / 9 short | -$147.28 | 0.8754 |
| OI same-date | 21 / 16 | 12 long / 9 short | -$98.54 | 0.9130 |
| OI prior-date stale | 1 / 1 | 1 long | -$48.74 | 0.0000 |
| OI timestamp-eligible, same + stale | 22 / 17 | 13 long / 9 short | -$147.28 | 0.8754 |
| Older GEX matched | 9 / 8 | 4 long / 5 short | +$146.84 | 1.3405 |
| Older GEX future excluded | 7 / 7 | 5 long / 2 short | -$226.18 | 0.4749 |
| Older GEX missing | 6 / 5 | 4 long / 2 short | -$67.94 | 0.7877 |
| Older GEX unavailable, future + missing | 13 / 12 | 9 long / 4 short | -$294.12 | 0.6082 |

The cross-availability counts are: same-date OI + old-GEX matched `9`,
same-date OI + old-GEX future-excluded `7`, same-date OI + old-GEX missing `5`,
and prior-stale OI + old-GEX missing `1`. These are saved-row partitions, not
counterfactual gated trades.

## Predeclared chronological median split

The split was declared before reading the outcome partitions: use the first
chronological half of timestamp-eligible OI rows to compute each feature's
median, then classify only the later half as `<= median` or `> median`. The
minimum was 10 matched rows. The actual sample has 22 timestamp-eligible rows,
so the first half has 11 and the later half has 11. The stale 2026-09-07 row is
included in the timestamp-eligible split and remains labelled in the trade rows.

| Feature | First-half median | Later low/equal: n / dates / PnL / PF | Later high: n / dates / PnL / PF |
|---|---:|---:|---:|
| Unsigned total OI | 5,946,196 | 9 / 8 / -$100.16 / 0.8047 | 2 / 1 / +$114.02 / 999.0000* |
| Put/call ratio | 1.21138614 | 1 / 1 / -$104.74 / 0.0000 | 10 / 8 / +$118.60 / 1.2905 |
| Calendar <=1-day expiry share | 0.08223624 | 4 / 4 / -$323.96 / 0.0000 | 7 / 5 / +$337.82 / 2.7876 |
| Strike HHI | 0.01413333 | 6 / 5 / +$203.56 / 2.0548 | 5 / 4 / -$189.70 / 0.4071 |

\* The total-OI high bucket has two positive rows and no loss, so the existing
PF helper reports its no-loss ceiling value `999.0`. The split has overlapping
date clusters, tiny buckets, and no untouched long holdout. It supplies a
descriptive partition only; no threshold or feature is selected.

The calendar and time ordering create visible confounds. The later-half
calendar-`<=1-day` split has only 4 low/equal rows across 4 dates versus 7 high
rows across 5 dates; the 2026-09-07 stale row is in the low/equal side with
`Near1 = 0.0285`. The September 7 NYSE closure and the calendar-date expiry
definition can therefore create a holiday/expiry-calendar artifact. Put/call
ratio also drifts chronologically from approximately 1.20–1.40 across the
August rows to 1.56–1.67 across the September rows. These effects, plus the
multi-trade date clusters, confound the tiny chronological buckets. The split
does not support a causal uplift or a promoted gate.

## Limitations and reproduction

- Monthly OI publication time supports a timestamp-eligible retrospective join. Vendor receipt/availability lineage and the OI effective trading session are unavailable.
- The raw 2026-09-07 gap is preserved as stale and marked `ETF options market closed; prior Fri OI context only` using the official NYSE calendar. The OI effective session remains unknown, and no exact CME close is inferred.
- The monthly files are partial-chain DTE 0–60 data. Ratios, expiry share, and HHI are within that saved universe.
- OI has no dealer-side sign. No signed gamma, dealer inventory, or option PnL is inferred.
- The older GEX comparison uses the existing saved context labels and timestamp status. It does not rerun the VP engine or create a GEX/OI-gated counterfactual.
- PnL and PF are descriptive statistics of the same saved 22 baseline trades. The split does not claim an improvement or a strategy effect.

Reproduce the machine-readable result from the repository root:

```text
python scripts/vp_theta_oi_context.py --compact
```

Focused verification:

```text
python -m pytest tests/test_vp_theta_oi_context.py -q
```

The implementation is [scripts/vp_theta_oi_context.py](../scripts/vp_theta_oi_context.py), and the focused tests are [tests/test_vp_theta_oi_context.py](../tests/test_vp_theta_oi_context.py).
