pub mod atr;
pub mod data;
pub mod model;
pub mod parity;
pub mod profile;
pub mod simulator;
pub mod stats;
pub mod strategy;

use chrono::{NaiveDate, SecondsFormat, Utc};
use model::{EntryFillMode, ExecutionConfig, Instrument, StrategyParams, TimestampSourcePolicy};
use parity::{ParityReport, compare_gzip_csv};
use serde::Serialize;
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::fs::{self, File, OpenOptions};
use std::io::BufReader;
use std::path::{Path, PathBuf};
use std::time::Instant;

#[derive(Serialize)]
struct CodeFileAudit {
    path: String,
    sha256: String,
}

#[derive(Serialize)]
struct SymbolRunReport {
    symbol: String,
    entry_fill_mode: String,
    timestamp_source_policy: String,
    execution_parity_applicable: bool,
    python_reference_rows: usize,
    rust_rows: usize,
    parity_exact: bool,
    rust_load_ms: u128,
    rust_replay_ms: u128,
    rust_end_to_end_ms: u128,
    python_reference_runtime_seconds: f64,
    input_bars_including_warmup: usize,
    input_files: Vec<data::InputFileAudit>,
    canonical_sha256: String,
    total_signals_before_window_filter: usize,
    signals_without_next_bar: usize,
    signals_filtered_by_source_policy: usize,
    source_excluded_rth_sessions: usize,
    input_source_rows: BTreeMap<String, usize>,
    warmup_or_out_of_window_trades: usize,
    forced_end_exits: usize,
    metrics: Value,
    parity_report: ParityReport,
    ledger_path: String,
    decision_trace_path: String,
    reference_ledger_sha256: String,
}

fn json_file(path: &Path) -> Result<Value, Box<dyn std::error::Error>> {
    Ok(serde_json::from_reader(BufReader::new(File::open(path)?))?)
}

fn required_string<'a>(
    value: &'a Value,
    pointer: &str,
) -> Result<&'a str, Box<dyn std::error::Error>> {
    value
        .pointer(pointer)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("missing string field {pointer}").into())
}

fn required_number(value: &Value, pointer: &str) -> Result<f64, Box<dyn std::error::Error>> {
    value
        .pointer(pointer)
        .and_then(Value::as_f64)
        .filter(|number| number.is_finite())
        .ok_or_else(|| format!("missing finite number field {pointer}").into())
}

fn required_usize(value: &Value, pointer: &str) -> Result<usize, Box<dyn std::error::Error>> {
    value
        .pointer(pointer)
        .and_then(Value::as_u64)
        .map(|number| number as usize)
        .ok_or_else(|| format!("missing nonnegative integer field {pointer}").into())
}

fn parse_date(value: &Value, pointer: &str) -> Result<NaiveDate, Box<dyn std::error::Error>> {
    Ok(NaiveDate::parse_from_str(
        required_string(value, pointer)?,
        "%Y-%m-%d",
    )?)
}

fn write_json_new(path: &Path, value: &Value) -> Result<(), Box<dyn std::error::Error>> {
    let file = OpenOptions::new().write(true).create_new(true).open(path)?;
    serde_json::to_writer_pretty(file, value)?;
    Ok(())
}

fn write_csv_gz_new<T: Serialize>(
    path: &Path,
    rows: &[T],
) -> Result<(), Box<dyn std::error::Error>> {
    let file = OpenOptions::new().write(true).create_new(true).open(path)?;
    let encoder = flate2::write::GzEncoder::new(file, flate2::Compression::default());
    let mut writer = csv::WriterBuilder::new().from_writer(encoder);
    for row in rows {
        writer.serialize(row)?;
    }
    let encoder = writer.into_inner().map_err(|error| error.into_error())?;
    encoder.finish()?;
    Ok(())
}

fn instrument_from_summary(
    symbol: &str,
    summary: &Value,
) -> Result<Instrument, Box<dyn std::error::Error>> {
    if required_string(summary, "/symbol")? != symbol {
        return Err(format!("reference summary symbol mismatch for {symbol}").into());
    }
    let tick_size = 0.25;
    let tick_value = required_number(summary, "/tick_value")?;
    Ok(Instrument {
        symbol: symbol.to_owned(),
        contract_id: required_string(summary, "/contract_id")?.to_owned(),
        point_value: tick_value / tick_size,
        tick_size,
        tick_value,
        commission_rt: required_number(summary, "/commission_rt")?,
        fees_rt: required_number(summary, "/fees_rt")?,
        start_date_utc: parse_date(summary, "/window/start")?,
        end_date_utc: parse_date(summary, "/window/end")?,
    })
}

fn strategy_params(summary: &Value) -> Result<StrategyParams, Box<dyn std::error::Error>> {
    let p = summary
        .pointer("/parameters")
        .ok_or("Python reference summary has no parameters")?;
    Ok(StrategyParams {
        entry_mode: required_string(p, "/entry_mode")?.to_owned(),
        target_mode: required_string(p, "/target_mode")?.to_owned(),
        side_mode: required_string(p, "/side_mode")?.to_owned(),
        value_area_pct: required_number(p, "/value_area_pct")?,
        sl_atr: required_number(p, "/sl_atr")?,
        tp_atr: required_number(p, "/tp_atr")?,
        confirm_bars: required_usize(p, "/confirm_bars")? as u32,
        breakout_buffer_ticks: required_usize(p, "/breakout_buffer_ticks")? as u32,
        touch_tolerance_ticks: required_usize(p, "/touch_tolerance_ticks")? as u32,
        reclaim_buffer_ticks: required_usize(p, "/reclaim_buffer_ticks")? as u32,
        max_trades_per_day: required_usize(p, "/max_trades_per_day")? as u32,
        min_source_candles: required_usize(p, "/min_source_candles")?,
    })
}

fn verify_python_source_snapshot(
    repo_root: &Path,
    summary: &Value,
) -> Result<Vec<CodeFileAudit>, Box<dyn std::error::Error>> {
    let expected = summary
        .pointer("/code_hashes")
        .and_then(Value::as_object)
        .ok_or("Python reference summary has no code_hashes")?;
    let mut audits = Vec::with_capacity(expected.len());
    for (relative, value) in expected {
        let expected_hash = value
            .as_str()
            .ok_or("reference code hash is not a string")?;
        let path = repo_root.join(relative);
        let actual_hash = data::sha256_file(&path)?;
        if !actual_hash.eq_ignore_ascii_case(expected_hash) {
            return Err(format!(
                "Python source snapshot hash mismatch for {}: expected {}, got {}",
                path.display(),
                expected_hash,
                actual_hash
            )
            .into());
        }
        audits.push(CodeFileAudit {
            path: path.to_string_lossy().into_owned(),
            sha256: actual_hash,
        });
    }
    Ok(audits)
}

fn rust_source_audits(
    project_root: &Path,
) -> Result<Vec<CodeFileAudit>, Box<dyn std::error::Error>> {
    let mut relative_paths: Vec<PathBuf> = [
        "../../Cargo.toml",
        "../../Cargo.lock",
        "Cargo.toml",
        "src/main.rs",
        "src/lib.rs",
        "src/model.rs",
        "src/data.rs",
        "src/profile.rs",
        "src/atr.rs",
        "src/strategy.rs",
        "src/simulator.rs",
        "src/parity.rs",
        "src/stats.rs",
    ]
    .into_iter()
    .map(PathBuf::from)
    .collect();
    relative_paths.sort();
    relative_paths
        .into_iter()
        .map(|relative| {
            let path = project_root.join(&relative);
            Ok(CodeFileAudit {
                path: relative.to_string_lossy().replace('\\', "/"),
                sha256: data::sha256_file(&path)?,
            })
        })
        .collect()
}

/// Run the frozen VP replay described by a versioned JSON configuration.
///
/// This is the in-process API used by other Rust callers. The CLI serializes
/// its `Value` result as JSON, so research orchestration can use either path.
pub fn run_vp_replay(config_path: impl AsRef<Path>) -> Result<Value, Box<dyn std::error::Error>> {
    let config_path = config_path.as_ref();
    let project_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let config = json_file(&config_path)?;
    if required_string(&config, "/schema")? != "ancsertpx.vp-rust-baseline-replay-config.v1" {
        return Err("unsupported replay config schema".into());
    }
    let config_sha256 = data::sha256_file(&config_path)?;
    let inputs = config.pointer("/inputs").ok_or("missing inputs")?;
    let bars_root = PathBuf::from(required_string(inputs, "/bars_root")?);
    let bars_manifest = PathBuf::from(required_string(inputs, "/bars_manifest")?);
    let warmup_root = PathBuf::from(required_string(inputs, "/warmup_root")?);
    let warmup_manifest = PathBuf::from(required_string(inputs, "/warmup_manifest")?);
    let repo_root = PathBuf::from(required_string(&config, "/repo/path")?);
    let frozen_git_head = required_string(&config, "/repo/git_head")?.to_owned();
    let execution_value = config
        .pointer("/execution")
        .ok_or("missing execution config")?;
    let entry_fill_mode =
        EntryFillMode::from_config(required_string(execution_value, "/entry_fill")?)?;
    let timestamp_source_policy = TimestampSourcePolicy::from_config(
        execution_value
            .get("timestamp_source_policy")
            .and_then(Value::as_str)
            .unwrap_or("reference_parity"),
    )?;
    if entry_fill_mode == EntryFillMode::NextOneMinuteOpenAfterCompletedSignal
        && timestamp_source_policy == TimestampSourcePolicy::ReferenceParity
    {
        return Err("causal next-bar fill requires an explicit timestamp_source_policy".into());
    }
    if entry_fill_mode == EntryFillMode::SignalBarClose
        && timestamp_source_policy != TimestampSourcePolicy::ReferenceParity
    {
        return Err("signal_bar_close is reserved for the frozen reference-parity contract".into());
    }
    let execution = ExecutionConfig {
        contracts: required_usize(&config, "/execution/contracts")? as i32,
        max_daily_loss_usd: required_number(&config, "/execution/max_daily_loss_usd")?,
        live_edge_guard_bars: required_usize(&config, "/execution/live_edge_guard_bars")?,
        entry_fill_mode,
        timestamp_source_policy,
    };
    let quality_flags = config
        .pointer("/quality_flags_by_symbol_entry_date_et")
        .ok_or("missing quality flag policies")?;

    let output_root = PathBuf::from(required_string(&config, "/output_root")?);
    fs::create_dir_all(&output_root)?;
    let run_id = format!(
        "run_{}_{}",
        Utc::now().format("%Y%m%dT%H%M%SZ"),
        std::process::id()
    );
    let output_dir = output_root.join(&run_id);
    fs::create_dir(&output_dir)?;

    let mut reports = Vec::new();
    let mut python_source_audits: Option<Vec<CodeFileAudit>> = None;
    let manifest_hashes = json!({
        "bars_manifest_sha256": data::sha256_file(&bars_manifest)?,
        "warmup_manifest_sha256": data::sha256_file(&warmup_manifest)?
    });

    for symbol in ["MNQ", "MES"] {
        let reference_config = config
            .pointer(&format!("/references/{symbol}"))
            .ok_or_else(|| format!("missing reference config for {symbol}"))?;
        let summary_path = PathBuf::from(required_string(reference_config, "/summary")?);
        let reference_ledger = PathBuf::from(required_string(reference_config, "/ledger")?);
        let reference_summary = json_file(&summary_path)?;
        if required_string(&reference_summary, "/git_head")? != frozen_git_head {
            return Err(format!("reference commit mismatch for {symbol}").into());
        }

        let source_audits = verify_python_source_snapshot(&repo_root, &reference_summary)?;
        if let Some(prior) = &python_source_audits {
            if serde_json::to_value(prior)? != serde_json::to_value(&source_audits)? {
                return Err("Python source hashes differ between MNQ and MES references".into());
            }
        } else {
            python_source_audits = Some(source_audits);
        }

        let expected_canonical_sha =
            required_string(&reference_summary, "/input/canonical_sha256")?;
        let expected_ledger_sha = required_string(&reference_summary, "/ledger_sha256_gzip")?;
        let actual_ledger_sha = data::sha256_file(&reference_ledger)?;
        if !actual_ledger_sha.eq_ignore_ascii_case(expected_ledger_sha) {
            return Err(format!("frozen Python ledger hash mismatch for {symbol}").into());
        }

        let load_started = Instant::now();
        let bundle = data::load_symbol(
            symbol,
            &bars_root,
            &bars_manifest,
            &warmup_root,
            &warmup_manifest,
        )?;
        let rust_load_ms = load_started.elapsed().as_millis();
        if !bundle
            .canonical_sha256
            .eq_ignore_ascii_case(expected_canonical_sha)
        {
            return Err(format!("canonical snapshot hash mismatch for {symbol}").into());
        }
        let expected_rows = required_usize(&reference_summary, "/bar_count_including_warmup")?;
        if bundle.bars.len() != expected_rows {
            return Err(format!(
                "Python/Rust input row count mismatch for {symbol}: Python {expected_rows}, Rust {}",
                bundle.bars.len()
            ).into());
        }
        let mut input_source_rows = BTreeMap::<String, usize>::new();
        for bar in &bundle.bars {
            *input_source_rows
                .entry(bar.source.as_str().to_owned())
                .or_default() += 1;
        }

        let instrument = instrument_from_summary(symbol, &reference_summary)?;
        let params = strategy_params(&reference_summary)?;
        let simulation_started = Instant::now();
        let simulation =
            simulator::Simulator::new(instrument.clone(), params, execution.clone(), quality_flags)
                .run(&bundle.bars)?;
        let rust_replay_ms = simulation_started.elapsed().as_millis();
        let metrics = stats::summarize_ledger(&simulation.rows, instrument.tick_value, 14.0);

        let ledger_path = output_dir.join(format!("ledger_{symbol}.csv.gz"));
        let trace_path = output_dir.join(format!("decision_trace_{symbol}.csv.gz"));
        write_csv_gz_new(&ledger_path, &simulation.rows)?;
        write_csv_gz_new(&trace_path, &simulation.traces)?;
        let parity_report = compare_gzip_csv(&reference_ledger, &ledger_path)?;
        let parity_path = output_dir.join(format!("parity_{symbol}.json"));
        write_json_new(&parity_path, &serde_json::to_value(&parity_report)?)?;

        let python_runtime = required_number(&reference_summary, "/runtime_seconds")?;
        let report = SymbolRunReport {
            symbol: symbol.to_owned(),
            entry_fill_mode: execution.entry_fill_mode.as_str().to_owned(),
            timestamp_source_policy: execution.timestamp_source_policy.as_str().to_owned(),
            execution_parity_applicable: execution.entry_fill_mode == EntryFillMode::SignalBarClose,
            python_reference_rows: parity_report.reference_rows,
            rust_rows: parity_report.rust_rows,
            parity_exact: parity_report.exact_field_parity,
            rust_load_ms,
            rust_replay_ms,
            rust_end_to_end_ms: rust_load_ms + rust_replay_ms,
            python_reference_runtime_seconds: python_runtime,
            input_bars_including_warmup: bundle.bars.len(),
            input_files: bundle.files,
            canonical_sha256: bundle.canonical_sha256,
            total_signals_before_window_filter: simulation.total_signals,
            signals_without_next_bar: simulation.signals_without_next_bar,
            signals_filtered_by_source_policy: simulation.signals_filtered_by_source_policy,
            source_excluded_rth_sessions: simulation.source_excluded_rth_sessions,
            input_source_rows,
            warmup_or_out_of_window_trades: simulation.warmup_or_out_of_window_trades,
            forced_end_exits: simulation.forced_end_exits,
            metrics,
            parity_report,
            ledger_path: ledger_path.to_string_lossy().into_owned(),
            decision_trace_path: trace_path.to_string_lossy().into_owned(),
            reference_ledger_sha256: actual_ledger_sha,
        };
        write_json_new(
            &output_dir.join(format!("summary_{symbol}.json")),
            &serde_json::to_value(&report)?,
        )?;
        reports.push(report);
    }

    let created_utc = Utc::now().to_rfc3339_opts(SecondsFormat::AutoSi, false);
    let manifest = json!({
        "schema": "ancsertpx.vp-rust-replay-run.v1",
        "run_id": run_id,
        "created_utc": created_utc,
        "study_id": required_string(&config, "/study_id")?,
        "reference_version": required_string(&config, "/reference_version")?,
        "source_commit": frozen_git_head,
        "configuration_path": config_path.to_string_lossy(),
        "configuration_sha256": config_sha256,
        "input_manifest_hashes": manifest_hashes,
        "python_source_code_hashes": python_source_audits.unwrap_or_default(),
        "rust_source_code_hashes": rust_source_audits(&project_root)?,
        "execution_contract": config.pointer("/execution").cloned().unwrap_or(Value::Null),
        "results": reports,
        "output_directory": output_dir.to_string_lossy()
    });
    write_json_new(&output_dir.join("run_manifest.json"), &manifest)?;

    let status = match (
        execution.entry_fill_mode,
        reports.iter().all(|report| report.parity_exact),
    ) {
        (EntryFillMode::SignalBarClose, true) => "exact_parity",
        (EntryFillMode::SignalBarClose, false) => "parity_mismatch",
        (EntryFillMode::NextOneMinuteOpenAfterCompletedSignal, _) => {
            "candidate_execution_completed_reference_parity_not_applicable"
        }
    };
    Ok(json!({
        "status": status,
        "run_id": run_id,
        "output_directory": output_dir,
        "symbols": reports.iter().map(|report| json!({
            "symbol": report.symbol,
            "trades": report.rust_rows,
            "parity_exact": report.parity_exact,
            "mismatch_cells": report.parity_report.mismatch_cells,
            "net": report.metrics.pointer("/baseline/pnl"),
            "stress_14t": report.metrics.pointer("/stress_14t/pnl"),
            "rust_load_ms": report.rust_load_ms,
            "rust_replay_ms": report.rust_replay_ms
        })).collect::<Vec<_>>()
    }))
}
