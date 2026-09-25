# Theta PI cohort ingest status — 2026-09-22

Research-only acquisition completed under the predeclared cohort plan in
[THETA_PI_COHORT_ACQUISITION_PLAN_2026-09-22.md](THETA_PI_COHORT_ACQUISITION_PLAN_2026-09-22.md).
Production code, presets, live processes, and trading operations were not
changed. The preceding archive QC remains documented in
[THETA_ARCHIVE_QC_2026-09-22.md](THETA_ARCHIVE_QC_2026-09-22.md); its completed
306/306 integrity result was not rescanned for this acquisition.

## Offline plan

The runner uses `backend.data.pi_history.load_rows()` and the stored monthly
EOD `created`/`expiration` fields. It found 481 canonical marks grouped into
240 symbol/event/next-session-expiry cohorts: QQQ 122 and SPY 118. Every
cohort resolved to an observed next EOD session that was also an exact listed
expiration. Each cohort has four requests: quote and first-order Greeks on the
event date and next session, with all strikes and both rights.

| Plan item | Result |
|---|---:|
| Cohorts | 240 |
| Requests | 960 |
| Existing hash-verified minute files before pilot | 20 |
| Freshly uncached requests before pilot | 940 |
| Exact selected-expiry request builder | shared with `theta_data_ingest.py` |
| Second-order Greeks / GEX | not requested |
| Tree/free-space guard | 300 GiB / 80 GiB reserve |

The derived manifest is
`F:\ancserQuant\ancserMarketData\source\options\thetadata\theta_pi_cohort_manifest.json`.
It stores request IDs, exact parameters, paths, prior manifest hashes, and
cache decisions. A file is skipped only when the latest request-manifest hash
matches the raw gzip file. The plan labels the earliest candidates separately
from `actual_selected_pilot`; the actual QQQ pilot was chosen from the fully
cached QQQ candidates.

## Completed pilot

The fresh network pilot ran under isolated Python 3.12.14 with SDK 1.0.10.
The initial worker PID was 8484. A cache-only rerun persisted the corrected
summary under PID 47944.

| Role | Cohort / expiry | Quote and Greek coverage | Rows | Contracts | Compressed bytes |
|---|---|---|---:|---:|---:|
| SPY short | 2026-03-09 / 2026-03-10 | both datasets on both days | 534,888 | 1,368 | 9,885,722 |
| QQQ long | 2026-03-30 / 2026-03-31 | both datasets on both days, hash-cached | 731,952 | 1,872 | 12,586,619 |

SPY day-level rows were 130,594 for each dataset on March 9 and 136,850 for
each dataset on March 10; the day-level contract counts were 334 and 350.
QQQ had 182,988 rows per dataset and 468 contracts on each day. The SPY four
files were downloaded successfully through the authorized SDK session. The
QQQ four files were verified from their existing raw hashes and rows.

Pilot raw files are under
`F:\ancserQuant\ancserMarketData\source\options\thetadata\raw\SPY\` and
the existing QQQ selected-expiry paths. Progress and the sanitized final pilot
summary are:

- `F:\ancserQuant\ancserMarketData\source\options\thetadata\theta_pi_cohort_progress.jsonl`
- `F:\ancserQuant\ancserMarketData\source\options\thetadata\theta_pi_cohort_summary.json`

## Remaining bounded run

The remaining runner was launched hidden as one serial process. Launcher PID:
`38140`. The active Python worker observed in the progress records is PID
`17064`; its isolated environment has Python 3.12.14 and Theta SDK 1.0.10
available from the research environment. Each monthly batch creates and closes
one fresh SDK client. Authentication, permission, capacity, corrupted-raw,
and other request failures stop the run at the failing batch.

The shared minute request covers 09:30–16:00 ET. Nasdaq's
[2026 options hours](https://www.nasdaq.com/holiday-trading-hours) list QQQ
and SPY option trading through 16:15 ET. Eight canonical PI marks stamped
16:01–16:02 ET fall outside the original request. After the serial run
completed, a separate 16:01–16:15 follow-up with distinct request IDs/raw
paths downloaded 16 quote/Greek files, 76,980 rows. All 16 gzip/raw hashes,
row counts and 15-minute timestamp sets passed independent offline QC;
the eight next-minute entries all had positive ask/ask size. See
[extended-window event report](PI_THETA_EXTENDED_WINDOW_2026-09-23.md).
The original 09:30–16:00 immutable files remain unchanged.

At launch, 24 minute files were hash-verified cached (the original 20 plus the
four fresh SPY pilot files), leaving 936 uncached requests. The user-scoped
remaining acquisition count is 236 cohorts; cached request slots remain in the
serial plan for resumability and are never redownloaded. The first observed
batch is March 2026 with 26 cohorts and 104 request slots. Its first QQQ
event-day quote response completed with 159,528 rows, 408 contracts, and
1,654,921 compressed bytes.

Launcher output and error paths:

- `F:\ancserQuant\ancserMarketData\source\options\thetadata\theta_pi_cohort_stdout.log`
- `F:\ancserQuant\ancserMarketData\source\options\thetadata\theta_pi_cohort_stderr.log`

The run summary is updated after each completed batch at
`F:\ancserQuant\ancserMarketData\source\options\thetadata\theta_pi_cohort_summary.json`.
The process uses no command-line credential, and SDK authentication logging is
suppressed before client construction.

## Completion observed 2026-09-23

The serial worker finished all seven month batches (2026-03 through 2026-09)
with no recorded error event. Its `request_complete` records cover 952
remaining-plan slots: 936 freshly downloaded and 16 hash-cached, totaling
138,454,664 rows and 2,409,354,662 compressed bytes. The separate two-cohort
pilot supplies eight more slots, so the complete 240-cohort plan has 960
request slots. The worker exited after the September `monthly_batch_complete`
event; its process no longer running is the expected terminal state. This is
a progress-manifest completion count; a final all-file hash/readability audit
and analytical quality checks remain separate from it.

An independent offline manifest join confirms 240 cohorts, 960 unique planned
request IDs, 960 latest `downloaded` coverage entries, and zero missing raw
paths. The downstream month-by-month replay will recompute gzip hashes and
inspect quote/Greek timestamps before analytical use.

## Verification

Focused tests pass:

- `tests/test_theta_pi_cohort_ingest.py`: 4 passed.
- `tests/test_theta_data_ingest.py`: 12 passed.

The tests cover offline cohort construction, preserved kind/level/source time,
CALL/PUT direction metadata, exact request ID/path reuse, manifest-hash cache
verification, and both-day quote/first-order coverage checks.
