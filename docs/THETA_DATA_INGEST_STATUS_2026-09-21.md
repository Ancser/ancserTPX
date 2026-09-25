# Theta Data options ingest status

Status updated 2026-09-22. Data is stored outside the repository at
`F:\ancserQuant\ancserMarketData\source\options\thetadata`.

## Completed pilot

The direct Theta Python SDK route authenticated successfully under the isolated
Python 3.12.14 environment at
`C:\Users\Ancser\AppData\Local\Temp\theta-research-py312-5917b6cfd6f94629aa3070290fac8d60\Scripts\python.exe`
with SDK 1.0.10. The local Terminal endpoint was not needed. SDK log output was
suppressed during construction, requests, and close so authentication material
cannot appear in captured output.

The fixed six-request pilot completed with six downloaded manifest records,
742,268 rows and 12,754,696 compressed response bytes. The six data files use
about 12.16 MiB; the market-data tree including its manifest and lock file was
12,762,536 bytes at verification. Drive F had 439,611,551,744 free bytes then.
The runner enforces a 300 GiB tree ceiling and preserves at least 80 GiB free.

| Dataset | Request/effective date | Expiry/scope | Rows | Contracts | Gzip SHA-256 |
|---|---|---|---:|---:|---|
| EOD | 2026-03-30 | partial chain, max DTE 60 | 5,232 | 5,232 | `fa45654843a9aa868a462b8e0dafcf3c65a3c59878d48f91adb1ff00cca65d51` |
| Open interest | publication 2026-03-30; effective 2026-03-27 | partial chain, max DTE 60 | 5,084 | 5,084 | `2e6687c526119fcfb41aa6240c9eee52674c6a96e08c77c7da4f8acb3a206f77` |
| 1m NBBO quote | 2026-03-30 | QQQ 2026-03-31 expiry, all strikes/rights | 182,988 | 468 | `9b5dba10649592cc9afdacb917d50a6904e86c81b86cf2906b0923e794458901` |
| 1m first-order Greeks/IV/underlying | 2026-03-30 | QQQ 2026-03-31 expiry, all strikes/rights | 182,988 | 468 | `0ef323732734e0ffd26b5ac71ee805a999b3a4f53f12ab27dbf99495fea4f066` |
| 1m NBBO quote | 2026-03-31 | QQQ 2026-03-31 expiry, all strikes/rights | 182,988 | 468 | `728830319ec041981a24a6e11e8f13e579f95590d60e0fab7a19a2b91f799d` |
| 1m first-order Greeks/IV/underlying | 2026-03-31 | QQQ 2026-03-31 expiry, all strikes/rights | 182,988 | 468 | `052d5801d2cc08ee5b588818509f36a7fc4a16c2cc8a86be7855c2707e65e97a` |

Raw SDK dataframe values are serialized as CSV and gzip-compressed with a fixed
gzip timestamp. Both uncompressed-serialization and compressed-file SHA-256,
rows, unique contracts, byte counts, request parameters, timestamps and status
are recorded in `coverage_manifest.jsonl`. Writes do not replace existing raw
files. Repeated successful requests verify the saved hash and resume from the
manifest.

The OI row timestamps were preserved as returned: 2026-03-30 06:30:00.596 to
06:30:02.729 ET. The manifest records the request/publication date as March 30
and effective prior XNYS session as March 27, with no second timestamp shift.
Theta describes OI as prior-session close published around 06:30 ET in its
[OI endpoint documentation](https://docs.thetadata.us/operations_excel/option_snapshot_open_interest.html).

The 0–60 DTE snapshots are explicitly labeled partial-chain coverage. The
intraday series cover the chosen next-session expiry across the full regular
session at 1-minute resolution; the first-order endpoint includes IV and
underlying price/timestamp fields. The Standard entitlement supports these
datasets. Second-order gamma returned `PERMISSION_DENIED` in the authorized
entitlement probe, so the runner makes no second-order request. Existing
zero-rate/dividend BSM gamma remains an estimated American-ETF proxy, not an
authoritative vendor gamma value.

## Bounded runner

The runner and focused tests are `scripts/theta_data_ingest.py` and
`tests/test_theta_data_ingest.py`. The fixed `--pilot` request set remains
unchanged. Explicit episode mode accepts symbol, event date, exit date,
expiration and the OI effective session; it checks that the selected-expiry
quote has rows and contracts on both dates. `--snapshot` supports one explicit
symbol/date/max-DTE EOD+OI pair for later resumable archive batches. OI
effective session is an explicit input so a caller must provide the session
provenance for the requested publication date.

Example dry run and execution for an episode:

```powershell
python scripts/theta_data_ingest.py --plan --symbol QQQ --event-date 2026-06-05 --exit-date 2026-06-08 --expiration 2026-06-08 --oi-effective-session 2026-06-04
python scripts/theta_data_ingest.py --episode --symbol QQQ --event-date 2026-06-05 --exit-date 2026-06-08 --expiration 2026-06-08 --oi-effective-session 2026-06-04
```

The research runtime is isolated from the repository's Python 3.10 trading
runtime. Credentials remain in the existing project `.env`; the runner never
prints their values or SDK exception details. Retries are bounded, calls are
serial with a delay, and the SDK logging threshold is raised before client
construction because the SDK logs its authentication response at INFO level.

## Completed canonical QQQ episodes

All four canonical QQQ Level-2 episodes completed with their explicitly listed
next-session expiries. No fallback expiry was substituted.

| Event | Exit/expiry | OI effective session |
|---|---|---|
| 2026-06-05 | 2026-06-08 | 2026-06-04 |
| 2026-06-09 | 2026-06-10 | 2026-06-08 |
| 2026-07-20 | 2026-07-21 | 2026-07-17 |
| 2026-07-24 | 2026-07-27 | 2026-07-23 |

| Episode | Rows | Selected-expiry contracts by day | Compressed bytes |
|---|---:|---|---:|
| 2026-06-05 → 2026-06-08 | 542,448 | 338 / 338 | 10,790,292 |
| 2026-06-09 → 2026-06-10 | 528,716 | 330 / 330 | 10,797,598 |
| 2026-07-20 → 2026-07-21 | 590,696 | 340 / 400 | 10,316,357 |
| 2026-07-24 → 2026-07-27 | 701,230 | 440 / 440 | 13,506,382 |

The latest manifest view now contains 30 downloaded request records and 30 raw
gzip files, with 3,105,358 rows and 58,165,325 compressed bytes across the
March pilot and these episodes. The append-only manifest has 31 lines because
the first June 5 OI attempt recorded a transient `UNAUTHENTICATED` failure;
the fresh single-client retry superseded that record and downloaded the OI
file. Hash reconciliation found zero mismatches. Any `NoDataFoundError` from
the SDK is recorded as `missing`; permission and authentication failures remain
distinct, and a batch stops on an unexpected error.

The generic episode writer no longer adds March pilot event-time metadata. The
earlier episode records retain their append-only legacy context fields; raw
files remain immutable, and downstream episode analysis should use the
canonical PI event timestamps. The fixed March pilot context remains valid.

## Monthly EOD/OI archive

Monthly archive mode builds 69 calendar-month partitions from 2021-01 through
2026-09-21 for QQQ and SPY, four requests per month, 276 requests total. EOD
and OI use distinct monthly dataset keys and paths such as:

```text
raw/QQQ/monthly_eod_dte_0_60/month=2021-01.csv.gz
raw/QQQ/monthly_open_interest_dte_0_60/month=2021-01.csv.gz
```

The January 2021 four-request pilot completed under PID 30164:

| Symbol | Monthly EOD rows/contracts | Monthly OI rows/contracts |
|---|---:|---:|
| QQQ | 38,582 / 3,346 | 40,072 / 3,790 |
| SPY | 81,366 / 7,552 | 84,776 / 7,992 |

The pilot total is 244,796 rows and 3,490,535 compressed bytes. All four gzip
hashes verify. Monthly OI records preserve vendor publication timestamps and
set `effective_session` to `unknown_until_calendar_join`; each row will map to
the prior trading session only after a later exchange-calendar join. The
monthly request does not assign one effective session to an entire month.

The remaining February 2021 through September 21, 2026 range is running as one
serial background archive process with one SDK client:

| Item | Value |
|---|---|
| Launcher PID | 21600 |
| Active data-worker PID | 36344 |
| Progress log | `F:\ancserQuant\ancserMarketData\source\options\thetadata\monthly_archive_progress.jsonl` |
| Stdout log | `F:\ancserQuant\ancserMarketData\source\options\thetadata\monthly_archive_batch_stdout.log` |
| Stderr log | `F:\ancserQuant\ancserMarketData\source\options\thetadata\monthly_archive_batch_stderr.log` |
| Planned remaining requests | 272 |

At the latest check, the worker had completed nine of the 272 remaining
requests, reaching April 2021 QQQ EOD, with zero
stderr bytes. Each request writes a sanitized progress record containing PID,
symbol, month, dataset, status, rows, contracts, bytes and path. The final summary is appended to the same progress log. The worker
stops on `forbidden`, authentication errors, unexpected errors, capacity
limits, and orphaned paths. Bounded retries handle transient transport errors
inside an individual request.

That check found 43 downloaded raw gzip files in total, 4,002,063 latest-view
rows and 71,160,735 compressed bytes. The tree occupied about 71 MiB, and
F: had more than 439 GiB free bytes. These totals increase as the background
worker completes additional months.

The runner and tests passed 12 focused tests after the monthly extension, and
the isolated Python 3.12 runtime compiled the script successfully. The
capacity guard remains 300 GiB for the tree with an 80 GiB free-space reserve.
