//! Stable, read-only orchestration APIs for repeatable Rust research studies.

use ancsertpx_rust_research::{ResearchResult, validate_study};
use ancsertpx_tsm_replay::{TsmTradeRow, run_tsm_replay};
use ancsertpx_vp_replay::{
    model::{DecisionTraceRow, TradeRow},
    run_vp_replay,
};
use flate2::read::GzDecoder;
use serde::de::DeserializeOwned;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File, OpenOptions};
use std::io::{BufReader, Error};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

const CATALOG_SCHEMA: &str = "ancsertpx.study-catalog.v1";
const RUN_SCHEMA: &str = "ancsertpx.vp-rust-replay-run.v1";
const TSM_RUN_SCHEMA: &str = "ancsertpx.tsm-rust-replay-run.v1";

fn required_string<'a>(value: &'a Value, pointer: &str) -> ResearchResult<&'a str> {
    value
        .pointer(pointer)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("missing string field {pointer}").into())
}

fn canonical_file(path: &Path) -> ResearchResult<PathBuf> {
    Ok(path.canonicalize()?)
}

fn read_json(path: &Path) -> ResearchResult<Value> {
    Ok(serde_json::from_reader(BufReader::new(File::open(path)?))?)
}

fn load_catalog(catalog_path: &Path) -> ResearchResult<(PathBuf, PathBuf, Value)> {
    let catalog_file = canonical_file(catalog_path)?;
    let catalog_dir = catalog_file
        .parent()
        .ok_or("catalog path has no parent directory")?
        .to_path_buf();
    let catalog = read_json(&catalog_file)?;
    if required_string(&catalog, "/schema")? != CATALOG_SCHEMA {
        return Err("unsupported study catalog schema".into());
    }
    Ok((catalog_file, catalog_dir, catalog))
}

fn catalog_entries(catalog: &Value) -> ResearchResult<&[Value]> {
    let entries = catalog
        .get("studies")
        .and_then(Value::as_array)
        .ok_or("catalog studies must be an array")?;
    let mut seen = BTreeSet::new();
    for entry in entries {
        let study_id = required_string(entry, "/study_id")?;
        required_string(entry, "/spec_path")?;
        if !seen.insert(study_id.to_owned()) {
            return Err(format!("duplicate study id in catalog: {study_id}").into());
        }
    }
    Ok(entries)
}

/// List catalog entries for AI clients and future research-data views without
/// starting a replay.
pub fn list_studies(catalog_path: &Path) -> ResearchResult<Value> {
    let (catalog_file, _, catalog) = load_catalog(catalog_path)?;
    let entries = catalog_entries(&catalog)?;
    Ok(json!({
        "schema": "ancsertpx.study-catalog-list.v1",
        "catalog_version": catalog.get("catalog_version"),
        "catalog_path": catalog_file,
        "catalog_sha256": sha256_file(&catalog_file)?,
        "studies": entries
    }))
}

fn resolve_catalog_spec(catalog_dir: &Path, spec_path: &str) -> ResearchResult<PathBuf> {
    let relative = Path::new(spec_path);
    if relative.is_absolute() {
        return Err("catalog spec_path must be relative to the catalog".into());
    }
    let spec = canonical_file(&catalog_dir.join(relative))?;
    if !spec.starts_with(catalog_dir) || !spec.is_file() {
        return Err("catalog study spec resolves outside the catalog directory".into());
    }
    Ok(spec)
}

/// Resolve a study through a versioned catalog, validate it, and run its linked
/// replay configuration. This performs blocking file and market-data I/O.
pub fn run_study(catalog_path: &Path, study_id: &str) -> ResearchResult<Value> {
    run_study_selected(catalog_path, study_id, None)
}

/// Run a registered study using one of its explicitly advertised strategies.
/// The selected runner config is snapshotted under the derived research root.
pub fn run_study_selected(
    catalog_path: &Path,
    study_id: &str,
    selected_strategy: Option<&str>,
) -> ResearchResult<Value> {
    let (catalog_file, catalog_dir, catalog) = load_catalog(catalog_path)?;
    let entries = catalog_entries(&catalog)?;
    let mut selected: Option<&Value> = None;
    for entry in entries {
        let entry_id = required_string(entry, "/study_id")?;
        if entry_id == study_id {
            selected = Some(entry);
        }
    }
    let entry = selected.ok_or_else(|| format!("study id not found in catalog: {study_id}"))?;
    let spec_path = resolve_catalog_spec(&catalog_dir, required_string(entry, "/spec_path")?)?;
    let validation = validate_study(&spec_path)?;
    if validation.study_id != study_id {
        return Err("catalog study id does not match the linked study spec".into());
    }
    if let Some(catalog_status) = entry.get("status").and_then(Value::as_str) {
        if catalog_status != validation.status {
            return Err("catalog status does not match the linked study spec".into());
        }
    }

    let mut replay_config = PathBuf::from(&validation.runner_config_path);
    if let Some(strategy) = selected_strategy {
        if validation.runner_package != "ancsertpx-tsm-replay" {
            return Err("strategy selection is currently available for registered TSM studies only".into());
        }
        let supported = spec_path
            .as_path();
        let spec_value = read_json(supported)?;
        let allowed = spec_value
            .get("supported_strategies")
            .and_then(Value::as_array)
            .ok_or("registered study has no supported_strategies list")?;
        if !allowed.iter().any(|value| value.as_str() == Some(strategy)) {
            return Err(format!("strategy is not advertised by study {study_id}: {strategy}").into());
        }
        let mut config_value = read_json(&replay_config)?;
        let object = config_value
            .as_object_mut()
            .ok_or("runner configuration must be a JSON object")?;
        object.insert("selected_strategy".to_owned(), json!(strategy));
        let output_root = PathBuf::from(required_string(&config_value, "/output_root")?);
        let snapshot_root = output_root
            .parent()
            .ok_or("runner output_root has no parent for config snapshot")?
            .join("selection_configs");
        fs::create_dir_all(&snapshot_root)?;
        let timestamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|error| format!("system clock predates Unix epoch: {error}"))?
            .as_secs();
        let snapshot_name = format!("selected_{strategy}_{timestamp}_{}.json", std::process::id());
        replay_config = snapshot_root.join(snapshot_name);
        write_json_new(&replay_config, &config_value)?;
    }
    let replay = match validation.runner_package.as_str() {
        "ancsertpx-vp-replay" => run_vp_replay(&replay_config)
            .map_err(|error| Error::other(format!("VP replay failed: {error}")))?,
        "ancsertpx-tsm-replay" => run_tsm_replay(&replay_config)
            .map_err(|error| Error::other(format!("TSM replay failed: {error}")))?,
        package => return Err(format!("study runner package is unavailable: {package}").into()),
    };
    let output_dir = canonical_file(Path::new(required_string(&replay, "/output_directory")?))?;
    let run_manifest_path = output_dir.join("run_manifest.json");
    let run_manifest = read_json(&run_manifest_path)?;
    if required_string(&run_manifest, "/run_id")? != required_string(&replay, "/run_id")? {
        return Err("replay summary run_id does not match its run manifest".into());
    }
    let provenance = json!({
        "schema": "ancsertpx.study-execution-provenance.v1",
        "run_id": required_string(&run_manifest, "/run_id")?,
        "study_id": study_id,
        "catalog_path": catalog_file,
        "catalog_sha256": sha256_file(&catalog_file)?,
        "catalog_entry": entry,
        "study_spec_path": spec_path,
        "study_spec_sha256": sha256_file(&spec_path)?,
        "runner_config_path": replay_config,
        "runner_config_sha256": sha256_file(&replay_config)?,
        "selected_strategy_override": selected_strategy,
        "runner_package": validation.runner_package,
        "run_manifest_path": run_manifest_path,
        "run_manifest_sha256": sha256_file(&run_manifest_path)?,
        "research_api_source_root": env!("CARGO_MANIFEST_DIR"),
        "research_api_source_code_hashes": research_api_source_hashes()?
    });
    write_json_new(&output_dir.join("study_execution.json"), &provenance)?;
    Ok(json!({
        "schema": "ancsertpx.run-study-result.v1",
        "catalog_entry": entry,
        "study_spec_path": spec_path,
        "validation": serde_json::to_value(validation)?,
        "run": replay,
        "execution_provenance": provenance
    }))
}

fn write_json_new(path: &Path, value: &Value) -> ResearchResult<()> {
    let file = OpenOptions::new().write(true).create_new(true).open(path)?;
    serde_json::to_writer_pretty(file, value)?;
    Ok(())
}

fn research_api_source_hashes() -> ResearchResult<Vec<Value>> {
    let api_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let source_files = [
        "../../Cargo.toml",
        "../../Cargo.lock",
        "Cargo.toml",
        "src/lib.rs",
        "src/main.rs",
        "../tsm-replay/Cargo.toml",
        "../tsm-replay/src/lib.rs",
        "../vp-replay/src/data.rs",
        "../vp-replay/src/stats.rs",
    ];
    source_files
        .iter()
        .map(|relative| {
            let path = api_root.join(relative);
            Ok(json!({
                "path": relative.replace('\\', "/"),
                "sha256": sha256_file(&path)?
            }))
        })
        .collect()
}

struct RunDocument {
    manifest_path: PathBuf,
    manifest: Value,
    output_root: PathBuf,
}

fn load_run(manifest_path: &Path) -> ResearchResult<RunDocument> {
    let manifest_path = canonical_file(manifest_path)?;
    let manifest = read_json(&manifest_path)?;
    if !matches!(required_string(&manifest, "/schema")?, RUN_SCHEMA | TSM_RUN_SCHEMA) {
        return Err(format!(
            "unsupported run manifest schema: {}",
            manifest_path.display()
        )
        .into());
    }
    let output_root = canonical_file(Path::new(required_string(&manifest, "/output_directory")?))?;
    if manifest_path.parent() != Some(output_root.as_path()) {
        return Err("run manifest must be stored directly in its output_directory".into());
    }
    Ok(RunDocument {
        manifest_path,
        manifest,
        output_root,
    })
}

fn python_source_map(manifest: &Value) -> ResearchResult<BTreeMap<String, String>> {
    let rows = manifest
        .get("python_source_code_hashes")
        .and_then(Value::as_array)
        .ok_or("run manifest python_source_code_hashes must be an array")?;
    let mut output = BTreeMap::new();
    for row in rows {
        let path = required_string(row, "/path")?.to_owned();
        let hash = required_string(row, "/sha256")?.to_ascii_lowercase();
        if output.insert(path.clone(), hash).is_some() {
            return Err(format!("duplicate Python source hash path: {path}").into());
        }
    }
    Ok(output)
}

fn assert_same_field(
    base: &Value,
    candidate: &Value,
    pointer: &str,
    label: &str,
) -> ResearchResult<()> {
    if base.pointer(pointer) != candidate.pointer(pointer) {
        return Err(format!("runs are incompatible: {label} differs").into());
    }
    Ok(())
}

fn validate_comparable_runs(base: &Value, candidate: &Value) -> ResearchResult<()> {
    for (pointer, label) in [
        ("/schema", "run schema"),
        ("/study_id", "study_id"),
        ("/reference_version", "reference_version"),
        ("/input_manifest_hashes", "input manifest hashes"),
        ("/execution_contract", "execution contract"),
        ("/volatility_scaling", "volatility-scaling contract"),
    ] {
        assert_same_field(base, candidate, pointer, label)?;
    }
    if python_source_map(base)? != python_source_map(candidate)? {
        return Err("runs are incompatible: Python source snapshot differs".into());
    }
    Ok(())
}

fn output_artifact_path(root: &Path, recorded_path: &str) -> ResearchResult<PathBuf> {
    let recorded = Path::new(recorded_path);
    let path = if recorded.is_absolute() {
        recorded.to_path_buf()
    } else {
        root.join(recorded)
    };
    let canonical = canonical_file(&path)?;
    if !canonical.starts_with(root) || !canonical.is_file() {
        return Err(format!(
            "run artifact resolves outside output_directory: {}",
            path.display()
        )
        .into());
    }
    Ok(canonical)
}

fn symbol_results(manifest: &Value) -> ResearchResult<BTreeMap<String, Value>> {
    let rows = manifest
        .get("results")
        .and_then(Value::as_array)
        .ok_or("run manifest results must be an array")?;
    let mut output = BTreeMap::new();
    for row in rows {
        let symbol = required_string(row, "/symbol")?.to_owned();
        if output.insert(symbol.clone(), row.clone()).is_some() {
            return Err(format!("duplicate symbol in run manifest: {symbol}").into());
        }
    }
    Ok(output)
}

fn read_gzip_csv<T: DeserializeOwned>(path: &Path) -> ResearchResult<Vec<T>> {
    let decoder = GzDecoder::new(File::open(path)?);
    let mut reader = csv::ReaderBuilder::new().from_reader(decoder);
    let mut rows = Vec::new();
    for record in reader.deserialize() {
        rows.push(record?);
    }
    Ok(rows)
}

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
struct TradeKey {
    contract_id: String,
    entry_time_utc: String,
    direction: String,
    setup: String,
    edge: String,
}

fn trade_key(row: &TradeRow, expected_symbol: &str) -> ResearchResult<TradeKey> {
    if row.symbol != expected_symbol {
        return Err(format!(
            "ledger symbol {} differs from manifest symbol {expected_symbol}",
            row.symbol
        )
        .into());
    }
    if !row.net_pnl.is_finite() {
        return Err("ledger contains non-finite net_pnl".into());
    }
    Ok(TradeKey {
        contract_id: row.contract_id.clone(),
        entry_time_utc: row.entry_time_utc.clone(),
        direction: row.direction.clone(),
        setup: row.setup.clone(),
        edge: row.edge.clone(),
    })
}

fn keyed_trades(rows: Vec<TradeRow>, symbol: &str) -> ResearchResult<BTreeMap<TradeKey, TradeRow>> {
    let mut output = BTreeMap::new();
    for row in rows {
        let key = trade_key(&row, symbol)?;
        if output.insert(key.clone(), row).is_some() {
            return Err(
                format!("ledger has duplicate comparison key for {symbol}: {key:?}").into(),
            );
        }
    }
    Ok(output)
}

fn summary_metrics(row: &Value) -> ResearchResult<&Value> {
    row.get("metrics")
        .ok_or_else(|| "symbol result has no metrics".into())
}

fn round_money(value: f64) -> f64 {
    (value * 100.0).round_ties_even() / 100.0
}

/// Compare two compatible runs on the exact same input and execution contract.
/// Configuration hashes may differ; no confidence interval is fabricated.
pub fn compare_runs(
    base_manifest_path: &Path,
    candidate_manifest_path: &Path,
) -> ResearchResult<Value> {
    let base = load_run(base_manifest_path)?;
    let candidate = load_run(candidate_manifest_path)?;
    validate_comparable_runs(&base.manifest, &candidate.manifest)?;
    if required_string(&base.manifest, "/schema")? == TSM_RUN_SCHEMA {
        return compare_tsm_runs(&base, &candidate);
    }

    let base_symbols = symbol_results(&base.manifest)?;
    let candidate_symbols = symbol_results(&candidate.manifest)?;
    let base_names: BTreeSet<_> = base_symbols.keys().cloned().collect();
    let candidate_names: BTreeSet<_> = candidate_symbols.keys().cloned().collect();
    if base_names != candidate_names {
        return Err("runs are incompatible: symbol coverage differs".into());
    }

    let mut symbols = Vec::with_capacity(base_names.len());
    for symbol in base_names {
        let base_row = &base_symbols[&symbol];
        let candidate_row = &candidate_symbols[&symbol];
        let base_ledger_path = output_artifact_path(
            &base.output_root,
            required_string(base_row, "/ledger_path")?,
        )?;
        let candidate_ledger_path = output_artifact_path(
            &candidate.output_root,
            required_string(candidate_row, "/ledger_path")?,
        )?;
        let base_trades = keyed_trades(read_gzip_csv::<TradeRow>(&base_ledger_path)?, &symbol)?;
        let candidate_trades =
            keyed_trades(read_gzip_csv::<TradeRow>(&candidate_ledger_path)?, &symbol)?;

        let mut matched = 0_usize;
        let mut base_only = 0_usize;
        let mut candidate_only = 0_usize;
        let mut matched_net_delta = 0.0_f64;
        let mut base_only_net = 0.0_f64;
        let mut candidate_only_net = 0.0_f64;
        for (key, base_trade) in &base_trades {
            if let Some(candidate_trade) = candidate_trades.get(key) {
                matched += 1;
                matched_net_delta += candidate_trade.net_pnl - base_trade.net_pnl;
            } else {
                base_only += 1;
                base_only_net += base_trade.net_pnl;
            }
        }
        for (key, candidate_trade) in &candidate_trades {
            if !base_trades.contains_key(key) {
                candidate_only += 1;
                candidate_only_net += candidate_trade.net_pnl;
            }
        }
        let base_total: f64 = base_trades.values().map(|row| row.net_pnl).sum();
        let candidate_total: f64 = candidate_trades.values().map(|row| row.net_pnl).sum();
        let total_net_delta = candidate_total - base_total;
        let reconciled_delta = matched_net_delta + candidate_only_net - base_only_net;
        if (total_net_delta - reconciled_delta).abs() > 0.011 {
            return Err(format!("paired ledger deltas do not reconcile for {symbol}").into());
        }

        symbols.push(json!({
            "symbol": symbol,
            "trade_pairing_key": ["contract_id", "entry_time_utc", "direction", "setup", "edge"],
            "base_trades": base_trades.len(),
            "candidate_trades": candidate_trades.len(),
            "matched_trades": matched,
            "base_only_trades": base_only,
            "candidate_only_trades": candidate_only,
            "matched_net_delta_candidate_minus_base": round_money(matched_net_delta),
            "base_only_net": round_money(base_only_net),
            "candidate_only_net": round_money(candidate_only_net),
            "base_total_net": round_money(base_total),
            "candidate_total_net": round_money(candidate_total),
            "total_net_delta_candidate_minus_base": round_money(total_net_delta),
            "base_parity_exact": base_row.get("parity_exact"),
            "candidate_parity_exact": candidate_row.get("parity_exact"),
            "base_mismatch_cells": base_row.pointer("/parity_report/mismatch_cells"),
            "candidate_mismatch_cells": candidate_row.pointer("/parity_report/mismatch_cells"),
            "base_metrics": summary_metrics(base_row)?,
            "candidate_metrics": summary_metrics(candidate_row)?
        }));
    }

    Ok(json!({
        "schema": "ancsertpx.run-comparison.v1",
        "study_id": required_string(&base.manifest, "/study_id")?,
        "base_run_id": required_string(&base.manifest, "/run_id")?,
        "candidate_run_id": required_string(&candidate.manifest, "/run_id")?,
        "configuration_hashes_differ": base.manifest.get("configuration_sha256") != candidate.manifest.get("configuration_sha256"),
        "input_manifest_hashes": base.manifest.get("input_manifest_hashes"),
        "confidence_interval": null,
        "symbols": symbols
    }))
}

type TsmTradeKey = (String, String, String, String);

fn tsm_trade_key(row: &TsmTradeRow, expected_symbol: &str) -> ResearchResult<TsmTradeKey> {
    if row.symbol != expected_symbol {
        return Err(format!("ledger symbol {} differs from manifest symbol {expected_symbol}", row.symbol).into());
    }
    if !row.net_pnl.is_finite() {
        return Err("TSM ledger contains non-finite net_pnl".into());
    }
    Ok((row.symbol.clone(), row.report_asof.clone(), row.entry_time.clone(), row.exit_time.clone()))
}

fn keyed_tsm_trades(rows: Vec<TsmTradeRow>, symbol: &str) -> ResearchResult<BTreeMap<TsmTradeKey, TsmTradeRow>> {
    let mut output = BTreeMap::new();
    for row in rows {
        let key = tsm_trade_key(&row, symbol)?;
        if output.insert(key.clone(), row).is_some() {
            return Err(format!("TSM ledger has duplicate comparison key for {symbol}: {key:?}").into());
        }
    }
    Ok(output)
}

fn compare_tsm_runs(base: &RunDocument, candidate: &RunDocument) -> ResearchResult<Value> {
    if required_string(&base.manifest, "/trade_schema")? != "ancsertpx.tsm-trade.v1"
        || required_string(&candidate.manifest, "/trade_schema")? != "ancsertpx.tsm-trade.v1"
    {
        return Err("TSM run manifest trade schema is unsupported".into());
    }
    let base_symbols = symbol_results(&base.manifest)?;
    let candidate_symbols = symbol_results(&candidate.manifest)?;
    let base_names: BTreeSet<_> = base_symbols.keys().cloned().collect();
    let candidate_names: BTreeSet<_> = candidate_symbols.keys().cloned().collect();
    if base_names != candidate_names {
        return Err("runs are incompatible: symbol coverage differs".into());
    }
    let mut symbols = Vec::with_capacity(base_names.len());
    for symbol in base_names {
        let base_row = &base_symbols[&symbol];
        let candidate_row = &candidate_symbols[&symbol];
        let base_path = output_artifact_path(&base.output_root, required_string(base_row, "/ledger_path")?)?;
        let candidate_path = output_artifact_path(&candidate.output_root, required_string(candidate_row, "/ledger_path")?)?;
        let base_rows = keyed_tsm_trades(read_gzip_csv::<TsmTradeRow>(&base_path)?, &symbol)?;
        let candidate_rows = keyed_tsm_trades(read_gzip_csv::<TsmTradeRow>(&candidate_path)?, &symbol)?;
        let mut matched = 0usize;
        let mut base_only = 0usize;
        let mut candidate_only = 0usize;
        let mut matched_net_delta = 0.0;
        let mut base_only_net = 0.0;
        let mut candidate_only_net = 0.0;
        for (key, base_trade) in &base_rows {
            if let Some(candidate_trade) = candidate_rows.get(key) {
                matched += 1;
                matched_net_delta += candidate_trade.net_pnl - base_trade.net_pnl;
            } else {
                base_only += 1;
                base_only_net += base_trade.net_pnl;
            }
        }
        for (key, candidate_trade) in &candidate_rows {
            if !base_rows.contains_key(key) {
                candidate_only += 1;
                candidate_only_net += candidate_trade.net_pnl;
            }
        }
        let base_total: f64 = base_rows.values().map(|row| row.net_pnl).sum();
        let candidate_total: f64 = candidate_rows.values().map(|row| row.net_pnl).sum();
        let total_delta = candidate_total - base_total;
        let reconciled = matched_net_delta + candidate_only_net - base_only_net;
        if (total_delta - reconciled).abs() > 0.011 {
            return Err(format!("paired TSM ledger deltas do not reconcile for {symbol}").into());
        }
        symbols.push(json!({
            "symbol": symbol,
            "trade_pairing_key": ["symbol", "report_asof", "entry_time", "exit_time"],
            "base_trades": base_rows.len(),
            "candidate_trades": candidate_rows.len(),
            "matched_trades": matched,
            "base_only_trades": base_only,
            "candidate_only_trades": candidate_only,
            "matched_net_delta_candidate_minus_base": round_money(matched_net_delta),
            "base_only_net": round_money(base_only_net),
            "candidate_only_net": round_money(candidate_only_net),
            "base_total_net": round_money(base_total),
            "candidate_total_net": round_money(candidate_total),
            "total_net_delta_candidate_minus_base": round_money(total_delta),
            "base_metrics": summary_metrics(base_row)?,
            "candidate_metrics": summary_metrics(candidate_row)?,
            "base_parity": base_row.get("parity_report"),
            "candidate_parity": candidate_row.get("parity_report")
        }));
    }
    Ok(json!({
        "schema": "ancsertpx.run-comparison.v1",
        "study_id": required_string(&base.manifest, "/study_id")?,
        "base_run_id": required_string(&base.manifest, "/run_id")?,
        "candidate_run_id": required_string(&candidate.manifest, "/run_id")?,
        "configuration_hashes_differ": base.manifest.get("configuration_sha256") != candidate.manifest.get("configuration_sha256"),
        "input_manifest_hashes": base.manifest.get("input_manifest_hashes"),
        "confidence_interval": null,
        "symbols": symbols
    }))
}

fn sha256_file(path: &Path) -> ResearchResult<String> {
    let bytes = fs::read(path)?;
    Ok(format!("{:x}", Sha256::digest(bytes)))
}

fn find_trade<'a>(rows: &'a [TradeRow], seq: u32, symbol: &str) -> ResearchResult<&'a TradeRow> {
    let mut matches = rows.iter().filter(|row| row.reference_trade_seq == seq);
    let row = matches
        .next()
        .ok_or_else(|| format!("trade sequence {seq} not found for {symbol}"))?;
    if matches.next().is_some() {
        return Err(format!("duplicate trade sequence {seq} for {symbol}").into());
    }
    if row.symbol != symbol {
        return Err(format!(
            "trade sequence {seq} belongs to {}, not {symbol}",
            row.symbol
        )
        .into());
    }
    Ok(row)
}

fn find_trace<'a>(
    rows: &'a [DecisionTraceRow],
    seq: u32,
    symbol: &str,
) -> ResearchResult<&'a DecisionTraceRow> {
    let mut matches = rows.iter().filter(|row| row.trace_seq == seq);
    let row = matches
        .next()
        .ok_or_else(|| format!("decision trace sequence {seq} not found for {symbol}"))?;
    if matches.next().is_some() {
        return Err(format!("duplicate decision trace sequence {seq} for {symbol}").into());
    }
    if row.symbol != symbol {
        return Err(format!(
            "trace sequence {seq} belongs to {}, not {symbol}",
            row.symbol
        )
        .into());
    }
    Ok(row)
}

fn verify_trade_trace(trade: &TradeRow, trace: &DecisionTraceRow, seq: u32) -> ResearchResult<()> {
    let numeric_match = |left: f64, right: f64| {
        left.is_finite() && right.is_finite() && (left - right).abs() <= 1e-9
    };
    let matches = trade.reference_trade_seq == trace.trace_seq
        && trade.symbol == trace.symbol
        && trade.entry_time_utc == trace.decision_timestamp_utc
        && trade.entry_time_et == trace.entry_timestamp_et
        && trade.rth_session_date_et == trace.rth_session_date_et
        && trade.topstep_trade_date_ct == trace.topstep_trade_date_ct
        && trade.direction == trace.direction
        && trade.setup == trace.setup
        && trade.edge == trace.edge
        && numeric_match(trade.entry_price, trace.entry_price)
        && numeric_match(trade.sl_price, trace.sl_price)
        && numeric_match(trade.tp_price, trace.tp_price);
    if !matches {
        return Err(format!("ledger and decision trace disagree for trade sequence {seq}").into());
    }
    Ok(())
}

/// Explain one executed trade by joining its ledger row to its decision trace.
/// Artifact paths are canonicalized and constrained to the run output folder.
pub fn explain_trade(
    run_manifest_path: &Path,
    symbol: &str,
    reference_trade_seq: u32,
) -> ResearchResult<Value> {
    let run = load_run(run_manifest_path)?;
    if required_string(&run.manifest, "/schema")? == TSM_RUN_SCHEMA {
        let results = symbol_results(&run.manifest)?;
        let result = results.get(symbol).ok_or_else(|| format!("symbol {symbol} is absent from run manifest"))?;
        let ledger_path = output_artifact_path(&run.output_root, required_string(result, "/ledger_path")?)?;
        let rows = read_gzip_csv::<TsmTradeRow>(&ledger_path)?;
        let mut matches = rows.iter().filter(|row| row.reference_trade_seq == reference_trade_seq);
        let row = matches.next().ok_or_else(|| format!("TSM trade sequence {reference_trade_seq} not found for {symbol}"))?;
        if matches.next().is_some() { return Err(format!("duplicate TSM trade sequence {reference_trade_seq} for {symbol}").into()); }
        if row.symbol != symbol { return Err(format!("TSM trade sequence {reference_trade_seq} belongs to {}, not {symbol}", row.symbol).into()); }
        return Ok(json!({
            "schema": "ancsertpx.trade-explanation.v1",
            "trade": row,
            "decision_trace": {
                "report_asof": row.report_asof,
                "available_at": row.available_at,
                "entry_time": row.entry_time,
                "exit_time": row.exit_time,
                "trend_60_return": row.trend_60_return,
                "signal": row.signal,
                "tff_lev_money_net_oi": row.tff_lev_money_net_oi,
                "tff_aligned": row.tff_aligned,
                "held_cme_sessions": row.held_cme_sessions
            },
            "provenance": {
                "run_id": required_string(&run.manifest, "/run_id")?,
                "study_id": required_string(&run.manifest, "/study_id")?,
                "symbol": symbol,
                "reference_trade_seq": reference_trade_seq,
                "configuration_sha256": required_string(&run.manifest, "/configuration_sha256")?,
                "input_manifest_hashes": run.manifest.get("input_manifest_hashes"),
                "run_manifest_path": run.manifest_path,
                "run_manifest_sha256": sha256_file(&run.manifest_path)?,
                "ledger_path": ledger_path,
                "ledger_sha256": sha256_file(&ledger_path)?
            }
        }));
    }
    let results = symbol_results(&run.manifest)?;
    let result = results
        .get(symbol)
        .ok_or_else(|| format!("symbol {symbol} is absent from run manifest"))?;
    let ledger_path =
        output_artifact_path(&run.output_root, required_string(result, "/ledger_path")?)?;
    let trace_path = output_artifact_path(
        &run.output_root,
        required_string(result, "/decision_trace_path")?,
    )?;
    let ledger = read_gzip_csv::<TradeRow>(&ledger_path)?;
    let trace_rows = read_gzip_csv::<DecisionTraceRow>(&trace_path)?;
    let trade = find_trade(&ledger, reference_trade_seq, symbol)?;
    let trace = find_trace(&trace_rows, reference_trade_seq, symbol)?;
    verify_trade_trace(trade, trace, reference_trade_seq)?;

    Ok(json!({
        "schema": "ancsertpx.trade-explanation.v1",
        "trade": trade,
        "decision_trace": trace,
        "provenance": {
            "run_id": required_string(&run.manifest, "/run_id")?,
            "study_id": required_string(&run.manifest, "/study_id")?,
            "symbol": symbol,
            "reference_trade_seq": reference_trade_seq,
            "configuration_sha256": required_string(&run.manifest, "/configuration_sha256")?,
            "input_manifest_hashes": run.manifest.get("input_manifest_hashes"),
            "run_manifest_path": run.manifest_path,
            "run_manifest_sha256": sha256_file(&run.manifest_path)?,
            "ledger_path": ledger_path,
            "ledger_sha256": sha256_file(&ledger_path)?,
            "decision_trace_path": trace_path,
            "decision_trace_sha256": sha256_file(&trace_path)?
        }
    }))
}
