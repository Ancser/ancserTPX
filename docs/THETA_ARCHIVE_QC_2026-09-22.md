# Theta archive QC status

Offline QC completed 2026-09-22 against
`F:\ancserQuant\ancserMarketData\source\options\thetadata`. The audit used
the append-only `coverage_manifest.jsonl` and every immutable `raw/**/*.csv.gz`
response. It performed no SDK authentication, network request, raw-file write,
or downloader change.

## Coverage and integrity

| Check | Result |
|---|---:|
| Manifest lines | 307 |
| Latest unique requests | 306 |
| Latest monthly requests | 276 / 276 |
| Latest episode requests | 30 / 30 |
| Raw gzip files | 306 / 306 |
| Rows | 28,300,195 |
| Decompressed CSV bytes | 2,909,038,778 |
| Compressed response bytes | 441,828,973 |
| Archive tree bytes | 442,638,668 |
| F: free bytes | 437,615,267,840 (407.561 GiB) |
| Gzip/raw hashes and manifest rows/bytes | 306 / 306 match |

The extra manifest line is the superseded June 5 OI authentication failure;
the latest record is the successful retry. All latest records have status
`downloaded`.

| Symbol | Files | Rows | Raw bytes | Gzip bytes | First observed date | Last observed date |
|---|---:|---:|---:|---:|---|---|
| QQQ | 168 | 14,760,548 | 1,557,490,801 | 232,023,774 | 2021-01-04 | 2026-09-21 |
| SPY | 138 | 13,539,647 | 1,351,547,977 | 209,805,199 | 2021-01-04 | 2026-09-21 |

The reusable audit is [theta_archive_qc.py](<F:/ancserQuant/ancserTPX/scripts/theta_archive_qc.py>).
It checks each manifest-linked file's gzip SHA-256, decompressed/raw
SHA-256, row count, raw/compressed byte count, schema parsing, duplicate keys,
date fields, numeric quality counts, monthly date coverage, and capacity state.
Use `--json` for the complete per-file and per-month machine-readable result.

## Date coverage and OI semantics

All 138 monthly symbol-month pairs were audited through the EOD `created` field
and OI `timestamp` field. Both fields had zero invalid date values. The
September 2026 partition carries the manifest bound through September 21; the
QC preserves that partial-month bound when an exchange calendar provider is
available.

No official exchange calendar package was installed in the local runtimes:
`exchange_calendars` and `pandas_market_calendars` were both unavailable.
Session-gap comparison is therefore explicitly reported as
`calendar_unavailable`; the audit does not infer holidays, weekends, or
missing exchange sessions. The JSON result retains observed date counts and
first/last dates for every monthly symbol pair for a later official-calendar
join.

OI publication timestamps were checked in America/New_York against a ±90-minute
window around 06:30 ET. Of 12,619,206 OI rows with timestamps, 12,454,968 were
inside the window and 164,238 were outside. The audit preserves raw publication
dates/times and keeps monthly OI `effective_session` as
`unknown_until_calendar_join`; it assigns no single effective session to a
month or row. Raw timestamp strings were not shifted, normalized in place, or
overwritten, and no historical publication date was silently converted into an
effective session.

The requested bounded timing diagnostic scanned the 18 raw 2026 monthly OI
files only (1,968,687 rows; January through the September 21 partial
partition). All 1,968,687 timestamps parsed, and zero were outside the
06:30±90-minute ET window. The top off-hour date/time lists for this bounded
sample are empty. This sample does not rank the 164,238 outside-window rows in
the complete archive; that ranking remains a separate optional diagnostic.

## Basic quality results

- Exact duplicate rows: 0.
- Natural contract-observation duplicates: 0.
- Malformed CSV rows: 0.
- Invalid observation dates: 0.
- Negative values occurred only in signed first-order fields, where signed
  values are legitimate: delta 635,836; epsilon 360,698; IV error 175,863;
  lambda 509,282; rho 486,580; theta 867,893. No negative OI, bid/ask,
  price, volume, or count values were found.
- Zero counts were retained as diagnostics rather than filtered: open interest
  had 3,211,935 zero values, bid 2,362,407, and ask 2,526. A zero quote or OI
  value can carry valid or missing-feed semantics depending on the response
  context; this offline QC records the value and does not reinterpret it. The
  largest expected sparsity was EOD OHLC/volume/count. The JSON output gives
  counts by field. Bid/ask NaNs totaled 5,912 each.

The focused QC tests pass 5/5 in the repository Python runtime, including
compact numeric timezone-offset parsing and bounded off-hour grouping. The audit is
read-only by design and remains separate from the Theta downloader and
production code.
