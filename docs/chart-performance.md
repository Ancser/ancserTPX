# Opt-in chart performance benchmark

Measures the existing free Lightweight Charts 4.1.3 engine and custom footprint
renderer. No paid chart SDK or new chart library was introduced. The measured
Glass cost justified removing Liquid Glass from the production page; its old
assets remain temporarily for audit/recovery but are no longer loaded. Normal
test runs skip this test unless `CHART_PERFORMANCE=1`.

PowerShell, from the repository root:

```powershell
$env:CHART_PERFORMANCE='1'
$env:CHART_PERFORMANCE_LABEL='paired'
node tests/ui/playwright-cli.cjs test tests/ui/chart-performance.spec.js --output=.playwright-output/chart-performance-run
```

Each invocation alternates baseline/current JavaScript, captured once at start.
Baseline is exactly `git show HEAD:frontend/static/ancserTPX.js`; current is the
working-tree file. Both are served through the normal script URL without a
production revert. Other assets are shared. After further edits use a new label
and the identical command. Reports are timestamped under the external directory
`F:/ancserQuant/ancserMarketData/derived/research/chart_performance/` and attached
to the test. This permanent copy survives all Playwright cleanup. No market data
is written to the repository. Unset `CHART_PERFORMANCE` to restore default skip.
The JSON is checkpointed after each completed sample; `status: running` with
fewer than 36 samples is an incomplete run, not a completed benchmark.

The default source is
`F:/ancserQuant/ancserMarketData/derived/orderflow/mnq/footprint_mnq_2026-08-07.json.gz`;
`CHART_PERFORMANCE_DATA` can override it. The source must contain 390 schema-v5
bars. Reports record source SHA-256, production asset hashes, Git HEAD, browser
version, dimensions, raw timing samples and three repetitions. Variant hashes
identify the actual served JavaScript; asset hashes at start/end identify other
changes. Do not edit shared CSS/HTML/Glass assets during a run.

## Conditions and workload

- `glass-on`: all Glass scripts loaded; canvas mirroring enabled.
- `mirror-off`: the same scripts and skin, with `TpxGlass.setCanvasMirror(false)`.
  This disables copying, not all filters, observers or optical DOM; existing
  copied pixels can remain. This is deliberately a separate condition.
- `glass-off`: benchmark routing replaces all three Glass JavaScript files
  (skin, engine, tuner) and `tpx-glass.css` with empty responses. A benchmark-only
  style disables residual backdrop filters from base CSS. This changes the
  surrounding skin; it is not a production feature. The chart dimensions remain
  equal, and no Glass engine is initialized.

Each repetition rotates condition order and uses a fresh browser context per
condition/variant. Viewport is 1600x1000, DPR 1; the chart is fixed to 1000x500
and asserted wholly inside the viewport. Overview
and detail ranges are identical across conditions. Only footprint is enabled;
CVD and other overlays are disabled. Real OHLC and cells come from the same
compact file, served through the normal visible-window fetch URL. Other API
traffic is stubbed except read-only configuration and health. External resources
are blocked; the installed pinned chart bundle replaces its CDN request.

Each sample drives an actual mouse drag and wheel zoom, positively verifies the
range changed, restores the fixed view, and appends six actual completed bars
at an accelerated 150ms cadence. This is **simulated replay**, not live ticks,
real-time ingestion, or backend throughput. Responses are capped at the released
bar count, initially 200 and then advanced one completed bar at a time to 206,
so padded requests never expose unreleased footprint bars. Initial loading and
pixel validation are outside the timed sample. The ready canvas PNG is hashed
outside the browser; only SHA-256 is retained, not pixel data. Match hashes
within mode/density/repetition to check baseline/current rendering equivalence.

Positive checks require a foreground document, exact nonzero canvas dimensions,
real loaded cells, painted pixels, hidden candle bodies, footprint fill calls,
detail number labels, and frame/draw samples. Mirror-on must increase blit
counters; mirror-off must not. Existing UI-028/029, DATA-011 and Glass contracts
remain unchanged: no DBN replay, trading call, or production toggle is added.

## Reading results

Each of the 36 samples reports frame-interval and synchronous footprint-draw
p50/p95, long-task count/total/p50/p95, fetch count, and before/after mirror
counters. Five stable redraws additionally record visibility applyOptions writes
and elapsed time; total measured visibility writes are also retained. Raw values
are retained. Frame intervals include browser scheduling;
draw time excludes subsequent raster/compositor work. Long tasks use the browser
50ms definition. Instrumentation overhead is shared by all conditions. These
short runs are comparative diagnostics, not FPS guarantees or CI speed gates.
Compare matching density/repetition across baseline and after; do not treat
headless Chromium results as native WebView2 or GPU performance measurements.

The existing config starts a separate headless Chromium session and loopback
Uvicorn on port 8765 with `--lifespan off`; no server/config changes are needed.
Keep other workloads and browser versions consistent between runs.
This spec overrides inherited tracing to OFF: `retain-on-failure` still records
DOM snapshots during successful runs and disproportionately burdens Glass.

## Separate supporting checks (coordinating main agent)

### Superseded traced measurements — 2026-09-12, repetition 1

The initial run was stopped at 22 samples after discovering inherited tracing
was recording large Glass DOM snapshots. These numbers are retained only as
diagnostic evidence; they are contaminated and must not support a performance
claim. An untraced run supersedes them. All rows passed active-paint, drag/wheel,
replay and mirror-state assertions. Times are milliseconds, baseline/current.

| Glass condition | Density | Draw p50 | Draw p95 | Frame p50 | Long tasks |
|---|---|---|---|---|---|
| Full on | Overview | 25.3 / 26.0 | 35.3 / 48.9 | 66.7 / 83.3 | 16 / 18 |
| Full on | Detail | 2.2 / 1.8 | 5.2 / 4.3 | 66.7 / 83.3 | 0 / 0 |
| Mirror off | Overview | 32.3 / 32.3 | 43.4 / 47.7 | 83.3 / 33.4 | 9 / 16 |
| Mirror off | Detail | 2.2 / 1.6 | 4.5 / 4.2 | 66.7 / 16.7 | 0 / 0 |
| Full off | Overview | 30.7 / 27.3 | 42.0 / 45.0 | 16.7 / 16.7 | 2 / 5 |
| Full off | Detail | 2.1 / 2.1 | 4.4 / 5.0 | 16.7 / 16.7 | 0 / 0 |

All six baseline samples made five visibility writes for five stable redraws;
all six current samples made zero. This confirms the mechanism change, but the
first repetition does not establish a consistent overall timing improvement.
Ready pixel hashes are collected; pair equality awaits the completed report.

### Main-agent regression and backend checks

These are supplied by the coordinating main agent, not rerun by the reusable
benchmark: pytest passed 675 tests plus 8 subtests. The visibility regression
mutation test failed on old source with Expected 0 / Received 5. On 2026-09-12,
main reported the regression GREEN, including memoization mutation checks,
identical pixels, repeated-coordinate guarding, and candle restoration on toggle.

Main-agent read-only backend timings for full-RTH `load_cached_footprint` on
2026-08-07 (390 bars, 139950 cells) were 163.74, 212.48, 195.44, 167.15 and
188.77ms. These backend results are separate from browser rendering and no
backend optimization is included.
