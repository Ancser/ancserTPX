# Rust research engine

This isolated workspace starts with reusable, read-only research operations. The `ancsertpx-rust-research` library exports `audit_inputs(&Path)` and `validate_study(&Path)`; its CLI calls the same functions. The `ancsertpx-vp-replay` library exports `run_vp_replay(config_path)`, and its CLI runs the frozen VP baseline. Heavy disk and replay functions are synchronous; async callers must dispatch them to a worker thread. `audit-gzip-csv` reads each `.csv.gz` under a chosen directory, validates its CSV shape, and emits one JSON report with file sizes, row counts, throughput, and elapsed time. It leaves source files untouched.

The first benchmark target is the 69 monthly QQQ OI partitions. The Python comparator uses `gzip` and `csv` over the same ordered paths and performs the same row-count workload. Hash calculation is outside the timed section; existing coverage manifests retain the archive SHA-256 evidence.

```powershell
cargo run -p ancsertpx-rust-research --release -- audit-gzip-csv F:\ancserQuant\ancserMarketData\source\options\thetadata\raw\QQQ\monthly_open_interest_dte_0_60
cargo run -p ancsertpx-rust-research --release -- validate-study studies\vp_baseline_2022_2026.json
cargo run -p ancsertpx-vp-replay --release -- --config F:\ancserQuant\ancserMarketData\derived\research\vp_rust_trade_replay_20260925T0710Z\frozen_replay_config.json
cargo run -p ancsertpx-research-api --release -- run-study studies\catalog.json vp_baseline_2022_2026
cargo run -p ancsertpx-research-api --release -- run-study studies\catalog.json tsm_60_session_tff_2022_2026 vol20_scaled_price_tsm
cargo run -p ancsertpx-research-api --release -- list-studies studies\catalog.json
cargo run -p ancsertpx-research-api --release -- compare-runs <base_run_manifest.json> <candidate_run_manifest.json>
cargo run -p ancsertpx-research-api --release -- explain-trade <run_manifest.json> MNQ 1
```

`validate-study` checks the versioned study and linked replay configuration.
Its v2 report separates runner availability from research-gate completion; it
does not run a backtest. The VP replay loads hash-checked Parquet inputs,
exports a trade ledger and decision trace, and compares the ledger against a
frozen Python reference. The baseline has not passed causal-fill, Topstep
account-path, option-regime durability, or prospective-coverage gates. Exact
baseline parity confirms implementation matching, with no profitability claim.

The next crate stages are typed market events and point-in-time features, a frozen VP/OI replay with Python ledger parity, versioned Topstep risk profiles, then a bounded CLI/API for repeatable studies. Production adapters remain out of this workspace until replay parity and risk gates pass.

`ancsertpx-research-api` provides the first stable orchestration surface. Its library exposes `list_studies`, `run_study`, `run_study_selected`, `compare_runs`, and `explain_trade`; its CLI calls those same functions. A registered study can select one of its advertised strategy ids with the optional `run-study` argument; the API snapshots that selected config under derived research and includes its hash in provenance. Run comparison requires identical frozen inputs, execution contract, volatility-sizing contract, reference version, and Python source snapshot; configuration hashes may differ. TSM comparisons pair on symbol, report date, and entry/exit interval, which permits comparing opposite trade directions on the same event. The TSM runner retains the exact Python-parity 60-session and lagged-TFF ledgers, then adds four volatility-scaled ledgers: price-only TSM and its same-interval passive-long pair, plus TFF-aligned TSM and its same-interval passive-long pair. Volatility sizing is frozen at 20 completed session returns, population standard deviation annualized by sqrt(252), a 30% target, and 1-3 contracts using round-half-up sizing. Each variant records 14-tick stress; the new reference ledger is generated independently from the canonical pickle snapshot. Trade explanation returns ledger and input hashes. Study execution writes a `study_execution.json` sidecar with catalog, spec, config, API source, and run-manifest hashes. Heavy I/O remains synchronous; async callers should use a worker thread.
