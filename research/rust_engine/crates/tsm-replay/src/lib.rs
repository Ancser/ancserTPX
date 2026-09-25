//! Deterministic session-event research replay for medium-frequency futures studies.
//!
//! This first runner ports the frozen 60-session TSM and lagged CFTC TFF rules.
//! It deliberately has no broker or live-execution dependency.

use ancsertpx_vp_replay::{
    data,
    model::Bar,
    stats::{circular_block_bootstrap, shared_monte_carlo, shared_walk_forward, summarize_pnl},
};
use chrono::{DateTime, Duration, NaiveDate, SecondsFormat, TimeZone, Utc};
use chrono_tz::America::New_York;
use csv::{ReaderBuilder, StringRecord, WriterBuilder};
use flate2::{Compression, read::GzDecoder, write::GzEncoder};
use serde::{Deserialize, Deserializer, Serialize};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fs::{self, File, OpenOptions};
use std::io::BufReader;
use std::path::{Path, PathBuf};
use std::time::Instant;

type ReplayResult<T> = Result<T, Box<dyn Error>>;

const CONFIG_SCHEMA: &str = "ancsertpx.tsm-rust-replay-config.v1";
const RUN_SCHEMA: &str = "ancsertpx.tsm-rust-replay-run.v1";
const TRADE_SCHEMA: &str = "ancsertpx.tsm-trade.v1";
const STRATEGY_BASE: &str = "price_only_60_session_tsm";
const STRATEGY_TFF: &str = "60_session_tsm_with_lagged_tff_alignment";
const STRATEGY_PAIRED: &str = "price_only_paired_on_candidate_intervals";
const STRATEGY_VOL_TSM: &str = "vol20_scaled_price_tsm";
const STRATEGY_VOL_PASSIVE: &str = "vol20_scaled_passive_long_paired_on_price_tsm";
const STRATEGY_VOL_TFF_TSM: &str = "vol20_scaled_tff_tsm";
const STRATEGY_VOL_TFF_PASSIVE: &str = "vol20_scaled_passive_long_paired_on_tff_tsm";
const SHARED_MC_ITERS: usize = 1_000;
const SHARED_MC_SEED: u32 = 20_261_010;
const SHARED_DRAWDOWN_THRESHOLD: f64 = 2_000.0;
const DATE_CLUSTER_DRAWS: usize = 10_000;
const DATE_CLUSTER_EVENTS: usize = 13;
const DATE_CLUSTER_SEED: u32 = 20_260_925;

#[derive(Debug, Deserialize)]
struct Config {
    schema: String,
    study_id: String,
    reference_version: String,
    inputs: InputConfig,
    evaluation_window: DateWindow,
    protocol: ProtocolConfig,
    volatility_scaling: VolatilityScalingConfig,
    instruments: BTreeMap<String, InstrumentConfig>,
    reference_ledgers: BTreeMap<String, BTreeMap<String, PathBuf>>,
    python_reference_source: PathBuf,
    #[serde(default)]
    additional_python_reference_sources: Vec<PathBuf>,
    selected_strategy: String,
    output_root: PathBuf,
}

#[derive(Debug, Deserialize)]
struct InputConfig {
    bars_root: PathBuf,
    bars_manifest: PathBuf,
    warmup_root: PathBuf,
    warmup_manifest: PathBuf,
    cftc_csv_gz: PathBuf,
    cftc_manifest: PathBuf,
    cftc_sha256: String,
    canonical_sha256: BTreeMap<String, String>,
}

#[derive(Debug, Deserialize)]
struct DateWindow {
    start: String,
    end: String,
}

#[derive(Debug, Deserialize)]
struct ProtocolConfig {
    lookback_sessions: usize,
    report_lag_days: i64,
    release_hour_et: u32,
    release_minute_et: u32,
    min_session_bars: usize,
    stress_ticks: f64,
    candidate_strategy: String,
    tff_market_codes: BTreeMap<String, String>,
}

#[derive(Debug, Deserialize)]
struct VolatilityScalingConfig {
    lookback_returns: usize,
    target_annualized_volatility: f64,
    minimum_contracts: u32,
    maximum_contracts: u32,
}

#[derive(Debug, Deserialize)]
struct InstrumentConfig {
    point_value: f64,
    tick_size: f64,
    tick_value: f64,
    round_trip_cost: f64,
}

#[derive(Clone, Debug)]
struct Session {
    trade_date: NaiveDate,
    start_us: i64,
    open: f64,
    high: f64,
    low: f64,
    close: f64,
    bars: usize,
}

#[derive(Clone, Debug)]
struct CotRow {
    asof: NaiveDate,
    net_oi: f64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct TsmTradeRow {
    pub reference_trade_seq: u32,
    pub symbol: String,
    pub report_asof: String,
    pub available_at: String,
    pub entry_date: String,
    pub entry_time: String,
    pub exit_date: String,
    pub exit_time: String,
    pub signal: i8,
    pub trend_60_return: f64,
    pub tff_lev_money_net_oi: f64,
    #[serde(deserialize_with = "deserialize_python_bool")]
    pub tff_aligned: bool,
    pub held_cme_sessions: usize,
    pub entry_price: f64,
    pub exit_price: f64,
    pub gross_pnl: f64,
    pub cost: f64,
    pub net_pnl: f64,
    pub stress_14t_pnl: f64,
    #[serde(default = "one_contract")]
    pub contracts: u32,
    #[serde(default)]
    pub realized_vol_20: f64,
}

fn one_contract() -> u32 { 1 }

fn deserialize_python_bool<'de, D>(deserializer: D) -> Result<bool, D::Error>
where
    D: Deserializer<'de>,
{
    let value = String::deserialize(deserializer)?;
    match value.as_str() {
        "True" | "true" | "1" => Ok(true),
        "False" | "false" | "0" => Ok(false),
        _ => Err(serde::de::Error::custom(format!("invalid boolean value {value}"))),
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct EventRow {
    symbol: String,
    report_asof: String,
    available_at: String,
    entry_index: usize,
    entry_date: String,
    entry_time: String,
    entry_open: f64,
    trend_60_return: f64,
    trend_signal: i8,
    tff_lev_money_net_oi: f64,
    tff_position_signal: i8,
    tff_aligned: bool,
    realized_vol_20: f64,
}

#[derive(Clone, Debug)]
struct Event {
    row: EventRow,
    entry_index: usize,
}

#[derive(Debug, Deserialize)]
struct PythonReferenceTrade {
    symbol: String,
    report_asof: String,
    available_at: String,
    entry_date: String,
    entry_time: String,
    exit_date: String,
    exit_time: String,
    signal: i8,
    trend_60_return: f64,
    tff_lev_money_net_oi: f64,
    #[serde(deserialize_with = "deserialize_python_bool")]
    tff_aligned: bool,
    held_cme_sessions: usize,
    entry_price: f64,
    exit_price: f64,
    gross_pnl: f64,
    cost: f64,
    net_pnl: f64,
    stress_14t_pnl: f64,
    contracts: Option<u32>,
    realized_vol_20: Option<f64>,
}

fn required_header(headers: &StringRecord, name: &str) -> ReplayResult<usize> {
    headers
        .iter()
        .position(|header| header == name)
        .ok_or_else(|| format!("CFTC file is missing required column {name}").into())
}

fn parse_float(record: &StringRecord, index: usize, label: &str) -> ReplayResult<f64> {
    let value = record
        .get(index)
        .ok_or_else(|| format!("missing CFTC value for {label}"))?;
    let parsed = value.replace(',', "").parse::<f64>()?;
    if !parsed.is_finite() {
        return Err(format!("non-finite CFTC value for {label}").into());
    }
    Ok(parsed)
}

fn read_cot(path: &Path, market_code: &str) -> ReplayResult<Vec<CotRow>> {
    let decoder = GzDecoder::new(BufReader::new(File::open(path)?));
    let mut reader = ReaderBuilder::new().has_headers(true).from_reader(decoder);
    let headers = reader.headers()?.clone();
    let code_i = required_header(&headers, "cftc_contract_market_code")?;
    let date_i = required_header(&headers, "report_date_as_yyyy_mm_dd")?;
    let oi_i = required_header(&headers, "open_interest_all")?;
    let long_i = required_header(&headers, "lev_money_positions_long")?;
    let short_i = required_header(&headers, "lev_money_positions_short")?;
    let mut rows = Vec::new();
    for record in reader.records() {
        let record = record?;
        if record.get(code_i) != Some(market_code) {
            continue;
        }
        let raw_date = record
            .get(date_i)
            .ok_or("CFTC report date is missing")?;
        let date_part = raw_date.get(..10).ok_or("CFTC report date is malformed")?;
        let asof = NaiveDate::parse_from_str(date_part, "%Y-%m-%d")?;
        let oi = parse_float(&record, oi_i, "open_interest_all")?;
        if oi <= 0.0 {
            continue;
        }
        let long = parse_float(&record, long_i, "lev_money_positions_long")?;
        let short = parse_float(&record, short_i, "lev_money_positions_short")?;
        rows.push(CotRow {
            asof,
            net_oi: (long - short) / oi,
        });
    }
    rows.sort_by_key(|row| row.asof);
    if rows.windows(2).any(|pair| pair[0].asof == pair[1].asof) {
        return Err(format!("duplicate CFTC as-of rows for market code {market_code}").into());
    }
    Ok(rows)
}

fn aggregate_sessions(bars: &[Bar], min_bars: usize) -> ReplayResult<Vec<Session>> {
    let mut sessions = Vec::new();
    let mut current: Option<Session> = None;
    for bar in bars {
        match current.as_mut() {
            Some(session) if session.trade_date == bar.topstep_trade_date_ct => {
                session.high = session.high.max(bar.high);
                session.low = session.low.min(bar.low);
                session.close = bar.close;
                session.bars += 1;
            }
            _ => {
                if let Some(completed) = current.take() {
                    if completed.bars >= min_bars {
                        sessions.push(completed);
                    }
                }
                current = Some(Session {
                    trade_date: bar.topstep_trade_date_ct,
                    start_us: bar.timestamp_us,
                    open: bar.open,
                    high: bar.high,
                    low: bar.low,
                    close: bar.close,
                    bars: 1,
                });
            }
        }
    }
    if let Some(completed) = current {
        if completed.bars >= min_bars {
            sessions.push(completed);
        }
    }
    if sessions.windows(2).any(|pair| pair[0].trade_date >= pair[1].trade_date) {
        return Err("CME trade-date sessions are duplicated or unsorted".into());
    }
    Ok(sessions)
}

fn date_string(value: NaiveDate) -> String {
    value.format("%Y-%m-%d").to_string()
}

fn utc_string(value: DateTime<Utc>) -> String {
    value.to_rfc3339_opts(SecondsFormat::Secs, false)
}

fn build_events(
    symbol: &str,
    cot: &[CotRow],
    sessions: &[Session],
    config: &ProtocolConfig,
    volatility: &VolatilityScalingConfig,
) -> ReplayResult<Vec<Event>> {
    let start_times: Vec<i64> = sessions.iter().map(|row| row.start_us).collect();
    let mut mapped = BTreeMap::<usize, Event>::new();
    for row in cot {
        let available_date = row
            .asof
            .checked_add_signed(Duration::days(config.report_lag_days))
            .ok_or("CFTC availability date overflow")?;
        let local_naive = available_date
            .and_hms_opt(config.release_hour_et, config.release_minute_et, 0)
            .ok_or("invalid CFTC release time in study config")?;
        let available_at = New_York
            .from_local_datetime(&local_naive)
            .single()
            .ok_or("CFTC release time is ambiguous or invalid in America/New_York")?;
        let available_utc = available_at.with_timezone(&Utc);
        let index = start_times.partition_point(|start| *start <= available_utc.timestamp_micros());
        if index >= sessions.len() || index < config.lookback_sessions + 1 {
            continue;
        }
        let close_index = index - 1;
        if close_index < volatility.lookback_returns {
            continue;
        }
        let prior_index = close_index - config.lookback_sessions;
        let trend = sessions[close_index].close / sessions[prior_index].close - 1.0;
        let volatility_start = close_index + 1 - volatility.lookback_returns;
        let mut returns = Vec::with_capacity(volatility.lookback_returns);
        for return_end in volatility_start..=close_index {
            returns.push(sessions[return_end].close / sessions[return_end - 1].close - 1.0);
        }
        let return_mean = returns.iter().sum::<f64>() / returns.len() as f64;
        let variance = returns.iter().map(|value| (value - return_mean).powi(2)).sum::<f64>() / returns.len() as f64;
        let realized_vol_20 = variance.sqrt() * 252.0_f64.sqrt();
        if !realized_vol_20.is_finite() || realized_vol_20 <= 0.0 {
            return Err(format!("invalid annualized realized volatility for {symbol} at {}", sessions[index].trade_date).into());
        }
        if !trend.is_finite() {
            return Err(format!("non-finite {symbol} trend at {}", sessions[index].trade_date).into());
        }
        let signal = if trend > 0.0 { 1 } else if trend < 0.0 { -1 } else { 0 };
        let tff_signal = if row.net_oi > 0.0 { 1 } else if row.net_oi < 0.0 { -1 } else { 0 };
        let session = &sessions[index];
        let event = Event {
            row: EventRow {
                symbol: symbol.to_owned(),
                report_asof: date_string(row.asof),
                available_at: available_at.to_rfc3339_opts(SecondsFormat::Secs, false),
                entry_index: index,
                entry_date: date_string(session.trade_date),
                entry_time: utc_string(DateTime::from_timestamp_micros(session.start_us).ok_or("invalid session start timestamp")?),
                entry_open: session.open,
                trend_60_return: trend,
                trend_signal: signal,
                tff_lev_money_net_oi: row.net_oi,
                tff_position_signal: tff_signal,
                tff_aligned: signal != 0 && tff_signal == signal,
                realized_vol_20,
            },
            entry_index: index,
        };
        // Later reports replace earlier reports when both resolve to one session open.
        mapped.insert(index, event);
    }
    Ok(mapped.into_values().collect())
}

fn make_trade(
    symbol: &str,
    seq: u32,
    event: &Event,
    next: &Event,
    direction: i8,
    contracts: u32,
    instrument: &InstrumentConfig,
    stress_ticks: f64,
) -> TsmTradeRow {
    let exit_open = next.row.entry_open;
    let size = f64::from(contracts);
    let gross = (exit_open - event.row.entry_open) * instrument.point_value * f64::from(direction) * size;
    let cost = instrument.round_trip_cost * size;
    let net = gross - cost;
    TsmTradeRow {
        reference_trade_seq: seq,
        symbol: symbol.to_owned(),
        report_asof: event.row.report_asof.clone(),
        available_at: event.row.available_at.clone(),
        entry_date: event.row.entry_date.clone(),
        entry_time: event.row.entry_time.clone(),
        exit_date: next.row.entry_date.clone(),
        exit_time: next.row.entry_time.clone(),
        signal: direction,
        trend_60_return: event.row.trend_60_return,
        tff_lev_money_net_oi: event.row.tff_lev_money_net_oi,
        tff_aligned: event.row.tff_aligned,
        held_cme_sessions: next.entry_index - event.entry_index,
        entry_price: event.row.entry_open,
        exit_price: exit_open,
        gross_pnl: gross,
        cost,
        net_pnl: net,
        stress_14t_pnl: net - stress_ticks * instrument.tick_value * size,
        contracts,
        realized_vol_20: event.row.realized_vol_20,
    }
}

fn volatility_sized_contracts(annualized_volatility: f64, config: &VolatilityScalingConfig) -> u32 {
    let raw = config.target_annualized_volatility / annualized_volatility;
    let rounded = (raw + 0.5).floor() as u32;
    rounded.clamp(config.minimum_contracts, config.maximum_contracts)
}

fn write_gzip_csv<T: Serialize>(path: &Path, rows: &[T]) -> ReplayResult<()> {
    let file = OpenOptions::new().write(true).create_new(true).open(path)?;
    let encoder = GzEncoder::new(file, Compression::default());
    let mut writer = WriterBuilder::new().has_headers(true).from_writer(encoder);
    for row in rows {
        writer.serialize(row)?;
    }
    writer.flush()?;
    let encoder = writer.into_inner().map_err(|error| error.into_error())?;
    encoder.finish()?;
    Ok(())
}

fn read_python_ledger(path: &Path) -> ReplayResult<Vec<PythonReferenceTrade>> {
    let decoder = GzDecoder::new(BufReader::new(File::open(path)?));
    let mut reader = ReaderBuilder::new().has_headers(true).from_reader(decoder);
    let mut rows = Vec::new();
    for row in reader.deserialize() {
        rows.push(row?);
    }
    Ok(rows)
}

type TradeKey = (String, String, String, String, i8);

fn trade_key(row: &TsmTradeRow) -> TradeKey {
    (row.symbol.clone(), row.report_asof.clone(), row.entry_time.clone(), row.exit_time.clone(), row.signal)
}

fn reference_key(row: &PythonReferenceTrade) -> TradeKey {
    (row.symbol.clone(), row.report_asof.clone(), row.entry_time.clone(), row.exit_time.clone(), row.signal)
}

fn compare_ledger(
    rust_rows: &[TsmTradeRow],
    reference_path: &Path,
    symbol: &str,
    start: &str,
    end: &str,
) -> ReplayResult<Value> {
    let reference_rows: Vec<_> = read_python_ledger(reference_path)?
        .into_iter()
        .filter(|row| row.entry_date.as_str() >= start && row.entry_date.as_str() <= end)
        .collect();
    let mut rust = BTreeMap::new();
    for row in rust_rows {
        if row.symbol != symbol {
            return Err(format!("Rust ledger symbol differs from expected {symbol}").into());
        }
        if rust.insert(trade_key(row), row).is_some() {
            return Err(format!("duplicate Rust comparison key for {symbol}").into());
        }
    }
    let mut reference = BTreeMap::new();
    for row in &reference_rows {
        if row.symbol != symbol {
            return Err(format!("Python ledger symbol differs from expected {symbol}").into());
        }
        if reference.insert(reference_key(row), row).is_some() {
            return Err(format!("duplicate Python comparison key for {symbol}").into());
        }
    }
    let numeric_fields = [
        "trend_60_return", "tff_lev_money_net_oi", "entry_price", "exit_price",
        "gross_pnl", "cost", "net_pnl", "stress_14t_pnl",
    ];
    let mut field_mismatches = BTreeMap::<String, usize>::new();
    let mut max_abs_error = BTreeMap::<String, f64>::new();
    let mut matched = 0usize;
    for (key, rust_row) in &rust {
        let Some(reference_row) = reference.get(key) else { continue };
        matched += 1;
        for (name, left, right) in [
            ("held_cme_sessions", rust_row.held_cme_sessions as f64, reference_row.held_cme_sessions as f64),
            ("signal", f64::from(rust_row.signal), f64::from(reference_row.signal)),
            ("trend_60_return", rust_row.trend_60_return, reference_row.trend_60_return),
            ("tff_lev_money_net_oi", rust_row.tff_lev_money_net_oi, reference_row.tff_lev_money_net_oi),
            ("entry_price", rust_row.entry_price, reference_row.entry_price),
            ("exit_price", rust_row.exit_price, reference_row.exit_price),
            ("gross_pnl", rust_row.gross_pnl, reference_row.gross_pnl),
            ("cost", rust_row.cost, reference_row.cost),
            ("net_pnl", rust_row.net_pnl, reference_row.net_pnl),
            ("stress_14t_pnl", rust_row.stress_14t_pnl, reference_row.stress_14t_pnl),
        ] {
            let error = (left - right).abs();
            max_abs_error.entry(name.to_owned()).and_modify(|value| *value = value.max(error)).or_insert(error);
            if error > 1e-9 {
                *field_mismatches.entry(name.to_owned()).or_default() += 1;
            }
        }
        if let Some(reference_contracts) = reference_row.contracts {
            if rust_row.contracts != reference_contracts {
                *field_mismatches.entry("contracts".to_owned()).or_default() += 1;
            }
        }
        if let Some(reference_volatility) = reference_row.realized_vol_20 {
            let error = (rust_row.realized_vol_20 - reference_volatility).abs();
            max_abs_error.entry("realized_vol_20".to_owned()).and_modify(|value| *value = value.max(error)).or_insert(error);
            if error > 1e-12 {
                *field_mismatches.entry("realized_vol_20".to_owned()).or_default() += 1;
            }
        }
        for (name, equal) in [
            ("report_asof", rust_row.report_asof == reference_row.report_asof),
            ("available_at", rust_row.available_at == reference_row.available_at),
            ("entry_date", rust_row.entry_date == reference_row.entry_date),
            ("entry_time", rust_row.entry_time == reference_row.entry_time),
            ("exit_date", rust_row.exit_date == reference_row.exit_date),
            ("exit_time", rust_row.exit_time == reference_row.exit_time),
            ("tff_aligned", rust_row.tff_aligned == reference_row.tff_aligned),
        ] {
            if !equal { *field_mismatches.entry(name.to_owned()).or_default() += 1; }
        }
    }
    let rust_only = rust.len().saturating_sub(matched);
    let python_only = reference.len().saturating_sub(matched);
    let mismatch_cells: usize = field_mismatches.values().sum();
    let field_order_ok = numeric_fields.iter().all(|field| max_abs_error.contains_key(*field));
    Ok(json!({
        "applicable": true,
        "exact": rust.len() == reference.len() && mismatch_cells == 0,
        "rust_rows": rust.len(),
        "python_rows": reference.len(),
        "matched_rows": matched,
        "rust_only_rows": rust_only,
        "python_only_rows": python_only,
        "mismatch_cells": mismatch_cells,
        "field_mismatches": field_mismatches,
        "max_abs_error_by_field": max_abs_error,
        "checked_all_numeric_reference_fields": field_order_ok
    }))
}

fn summarize_strategy(rows: &[TsmTradeRow]) -> ReplayResult<Value> {
    let net: Vec<f64> = rows.iter().map(|row| row.net_pnl).collect();
    let stress: Vec<f64> = rows.iter().map(|row| row.stress_14t_pnl).collect();
    let net_points: Vec<(f64, f64)> = rows.iter().map(|row| {
        let timestamp = DateTime::parse_from_rfc3339(&row.entry_time)
            .map(|value| value.timestamp() as f64)
            .map_err(|error| format!("invalid TSM entry timestamp {}: {error}", row.entry_time))?;
        Ok((timestamp, row.net_pnl))
    }).collect::<Result<_, String>>()?;
    let stress_points: Vec<(f64, f64)> = rows.iter().zip(&net_points)
        .map(|(row, point)| (point.0, row.stress_14t_pnl)).collect();
    let mut years = BTreeMap::<String, Vec<f64>>::new();
    let mut months = BTreeSet::new();
    let mut by_entry_day = BTreeMap::<String, Vec<usize>>::new();
    let mut best_index = None;
    for (index, row) in rows.iter().enumerate() {
        years.entry(row.entry_date[..4].to_owned()).or_default().push(row.net_pnl);
        months.insert(row.entry_date[..7].to_owned());
        by_entry_day.entry(row.entry_date.clone()).or_default().push(index);
        if best_index.is_none_or(|best: usize| row.net_pnl > rows[best].net_pnl) {
            best_index = Some(index);
        }
    }
    let best_removed: Vec<f64> = best_index
        .map(|best| rows.iter().enumerate().filter(|(index, _)| *index != best).map(|(_, row)| row.net_pnl).collect())
        .unwrap_or_default();
    let best_removed_stress: Vec<f64> = best_index
        .map(|best| rows.iter().enumerate().filter(|(index, _)| *index != best).map(|(_, row)| row.stress_14t_pnl).collect())
        .unwrap_or_default();
    let best_day = by_entry_day.iter().max_by(|left, right| {
        let left_pnl: f64 = left.1.iter().map(|index| rows[*index].net_pnl).sum();
        let right_pnl: f64 = right.1.iter().map(|index| rows[*index].net_pnl).sum();
        left_pnl.total_cmp(&right_pnl)
    });
    let remaining_day_net: Vec<f64> = best_day.map(|(day, _)| rows.iter()
        .filter(|row| &row.entry_date != day).map(|row| row.net_pnl).collect()).unwrap_or_default();
    let remaining_day_stress: Vec<f64> = best_day.map(|(day, _)| rows.iter()
        .filter(|row| &row.entry_date != day).map(|row| row.stress_14t_pnl).collect()).unwrap_or_default();
    let best_day_value = best_day.map(|(day, indexes)| {
        let removed_net: f64 = indexes.iter().map(|index| rows[*index].net_pnl).sum();
        let removed_stress: f64 = indexes.iter().map(|index| rows[*index].stress_14t_pnl).sum();
        json!({
            "entry_date": day,
            "trades_removed": indexes.len(),
            "net_removed": removed_net,
            "stress_14t_removed": removed_stress,
            "remaining_net": summarize_pnl(&remaining_day_net),
            "remaining_stress_14t": summarize_pnl(&remaining_day_stress)
        })
    }).unwrap_or(Value::Null);
    let net_mc = shared_monte_carlo(&net, SHARED_MC_ITERS, SHARED_MC_SEED, SHARED_DRAWDOWN_THRESHOLD);
    let stress_mc = shared_monte_carlo(&stress, SHARED_MC_ITERS, SHARED_MC_SEED + 1, SHARED_DRAWDOWN_THRESHOLD);
    let coverage_start = rows.first().map(|row| row.entry_date.as_str());
    let coverage_end = rows.last().map(|row| row.entry_date.as_str());
    Ok(json!({
        "trade_count": rows.len(),
        "calendar_months_with_trade": months.len(),
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
        "coverage_at_least_12_months": months.len() >= 12,
        "net": summarize_pnl(&net),
        "stress_14t": summarize_pnl(&stress),
        "net_walk_forward_shared": shared_walk_forward(&net_points, 3),
        "stress_14t_walk_forward_shared": shared_walk_forward(&stress_points, 3),
        "net_shared_monte_carlo": net_mc,
        "stress_14t_shared_monte_carlo": stress_mc,
        "date_cluster_13_event_net": circular_block_bootstrap(&net, DATE_CLUSTER_DRAWS, DATE_CLUSTER_EVENTS, DATE_CLUSTER_SEED),
        "date_cluster_13_event_stress_14t": circular_block_bootstrap(&stress, DATE_CLUSTER_DRAWS, DATE_CLUSTER_EVENTS, DATE_CLUSTER_SEED + 1),
        "best_interval_removed": {
            "reference_trade_seq": best_index.map(|index| rows[index].reference_trade_seq),
            "removed_net": best_index.map(|index| rows[index].net_pnl).unwrap_or(0.0),
            "remaining_net": summarize_pnl(&best_removed),
            "remaining_stress_14t": summarize_pnl(&best_removed_stress)
        },
        "best_entry_day_removed": best_day_value,
        "by_year": years.into_iter().map(|(year, values)| (year, summarize_pnl(&values))).collect::<BTreeMap<_, _>>()
    }))
}

fn read_json(path: &Path) -> ReplayResult<Value> {
    Ok(serde_json::from_reader(BufReader::new(File::open(path)?))?)
}

fn write_json_new(path: &Path, value: &Value) -> ReplayResult<()> {
    let file = OpenOptions::new().write(true).create_new(true).open(path)?;
    serde_json::to_writer_pretty(file, value)?;
    Ok(())
}

fn source_hashes() -> ReplayResult<Vec<Value>> {
    let crate_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    ["Cargo.toml", "src/lib.rs", "../vp-replay/src/data.rs", "../vp-replay/src/stats.rs"]
        .into_iter()
        .map(|relative| {
            let path = crate_root.join(relative);
            Ok(json!({"path": relative.replace('\\', "/"), "sha256": data::sha256_file(&path)?}))
        })
        .collect()
}

fn expected_strategy_keys() -> [&'static str; 7] {
    [
        STRATEGY_BASE,
        STRATEGY_TFF,
        STRATEGY_PAIRED,
        STRATEGY_VOL_TSM,
        STRATEGY_VOL_PASSIVE,
        STRATEGY_VOL_TFF_TSM,
        STRATEGY_VOL_TFF_PASSIVE,
    ]
}

/// Run the frozen session-event TSM replay, paired ledgers, and row-level Python parity.
pub fn run_tsm_replay(config_path: impl AsRef<Path>) -> ReplayResult<Value> {
    let total_started = Instant::now();
    let config_path = config_path.as_ref();
    let config: Config = serde_json::from_reader(BufReader::new(File::open(config_path)?))?;
    if config.schema != CONFIG_SCHEMA { return Err("unsupported TSM replay config schema".into()); }
    if config.protocol.candidate_strategy != STRATEGY_TFF { return Err("TSM candidate strategy differs from frozen study".into()); }
    if !expected_strategy_keys().contains(&config.selected_strategy.as_str()) { return Err("selected strategy name is not registered".into()); }
    if config.volatility_scaling.lookback_returns == 0
        || !config.volatility_scaling.target_annualized_volatility.is_finite()
        || config.volatility_scaling.target_annualized_volatility <= 0.0
        || config.volatility_scaling.minimum_contracts == 0
        || config.volatility_scaling.minimum_contracts > config.volatility_scaling.maximum_contracts
    {
        return Err("invalid frozen volatility-scaling specification".into());
    }
    let start = NaiveDate::parse_from_str(&config.evaluation_window.start, "%Y-%m-%d")?;
    let end = NaiveDate::parse_from_str(&config.evaluation_window.end, "%Y-%m-%d")?;
    if start > end { return Err("evaluation window start is after end".into()); }

    let manifest_started = Instant::now();
    let bars_manifest_sha = data::sha256_file(&config.inputs.bars_manifest)?;
    let warmup_manifest_sha = data::sha256_file(&config.inputs.warmup_manifest)?;
    let cftc_manifest_sha = data::sha256_file(&config.inputs.cftc_manifest)?;
    let cftc_sha = data::sha256_file(&config.inputs.cftc_csv_gz)?;
    if !cftc_sha.eq_ignore_ascii_case(&config.inputs.cftc_sha256) {
        return Err("CFTC gzip SHA-256 differs from frozen config".into());
    }
    let cftc_manifest = read_json(&config.inputs.cftc_manifest)?;
    if cftc_manifest.pointer("/sha256_gzip").and_then(Value::as_str).is_some_and(|value| !value.eq_ignore_ascii_case(&cftc_sha)) {
        return Err("CFTC manifest gzip SHA-256 differs from source file".into());
    }
    let manifest_ms = manifest_started.elapsed().as_millis();

    let load_started = Instant::now();
    let mut data_bundles = BTreeMap::new();
    for symbol in ["MNQ", "MES"] {
        let bundle = data::load_symbol(
            symbol,
            &config.inputs.bars_root,
            &config.inputs.bars_manifest,
            &config.inputs.warmup_root,
            &config.inputs.warmup_manifest,
        )?;
        let expected = config.inputs.canonical_sha256.get(symbol).ok_or_else(|| format!("missing expected canonical SHA-256 for {symbol}"))?;
        if !bundle.canonical_sha256.eq_ignore_ascii_case(expected) {
            return Err(format!("canonical source SHA-256 differs from TSM config for {symbol}").into());
        }
        data_bundles.insert(symbol.to_owned(), bundle);
    }
    let load_ms = load_started.elapsed().as_millis();

    let replay_started = Instant::now();
    let mut symbol_runs = Vec::new();
    let mut run_reports = Vec::new();
    let mut output_ledgers = BTreeMap::<String, BTreeMap<String, Vec<TsmTradeRow>>>::new();
    let mut reference_reports = BTreeMap::<String, BTreeMap<String, Value>>::new();
    let mut all_input_files = BTreeMap::<String, Vec<data::InputFileAudit>>::new();

    for symbol in ["MNQ", "MES"] {
        let bundle = data_bundles.remove(symbol).ok_or("loaded symbol bundle disappeared")?;
        let instrument = config.instruments.get(symbol).ok_or_else(|| format!("missing instrument specification for {symbol}"))?;
        if !(instrument.point_value > 0.0 && instrument.tick_size > 0.0 && instrument.tick_value > 0.0 && instrument.round_trip_cost >= 0.0) {
            return Err(format!("invalid instrument specification for {symbol}").into());
        }
        let sessions = aggregate_sessions(&bundle.bars, config.protocol.min_session_bars)?;
        if sessions.len() <= config.protocol.lookback_sessions + 1 { return Err(format!("insufficient completed sessions for {symbol}").into()); }
        let code = config.protocol.tff_market_codes.get(symbol).ok_or_else(|| format!("missing TFF market code for {symbol}"))?;
        let cot = read_cot(&config.inputs.cftc_csv_gz, code)?;
        let events = build_events(symbol, &cot, &sessions, &config.protocol, &config.volatility_scaling)?;
        let evaluation_events: Vec<_> = events.into_iter().filter(|event| {
            let date = NaiveDate::parse_from_str(&event.row.entry_date, "%Y-%m-%d").expect("generated ISO date");
            date >= start && date <= end
        }).collect();
        let mut base_rows = Vec::new();
        let mut tff_rows = Vec::new();
        let mut paired_rows = Vec::new();
        let mut vol_tsm_rows = Vec::new();
        let mut vol_passive_rows = Vec::new();
        let mut vol_tff_tsm_rows = Vec::new();
        let mut vol_tff_passive_rows = Vec::new();
        for (seq, pair) in evaluation_events.windows(2).enumerate() {
            let event = &pair[0];
            let next = &pair[1];
            let direction = event.row.trend_signal;
            if direction == 0 { continue; }
            let row = make_trade(symbol, seq as u32 + 1, event, next, direction, 1, instrument, config.protocol.stress_ticks);
            base_rows.push(row.clone());
            let contracts = volatility_sized_contracts(event.row.realized_vol_20, &config.volatility_scaling);
            let scaled_tsm = make_trade(symbol, seq as u32 + 1, event, next, direction, contracts, instrument, config.protocol.stress_ticks);
            let scaled_passive = make_trade(symbol, seq as u32 + 1, event, next, 1, contracts, instrument, config.protocol.stress_ticks);
            vol_tsm_rows.push(scaled_tsm.clone());
            vol_passive_rows.push(scaled_passive.clone());
            if event.row.tff_aligned {
                tff_rows.push(row.clone());
                paired_rows.push(row);
                vol_tff_tsm_rows.push(scaled_tsm);
                vol_tff_passive_rows.push(scaled_passive);
            }
        }

        let mut parity = BTreeMap::new();
        let strategy_rows = BTreeMap::from([
            (STRATEGY_BASE.to_owned(), base_rows),
            (STRATEGY_TFF.to_owned(), tff_rows),
            (STRATEGY_PAIRED.to_owned(), paired_rows),
            (STRATEGY_VOL_TSM.to_owned(), vol_tsm_rows),
            (STRATEGY_VOL_PASSIVE.to_owned(), vol_passive_rows),
            (STRATEGY_VOL_TFF_TSM.to_owned(), vol_tff_tsm_rows),
            (STRATEGY_VOL_TFF_PASSIVE.to_owned(), vol_tff_passive_rows),
        ]);
        for (strategy, rows) in &strategy_rows {
            let reference = config.reference_ledgers.get(strategy).and_then(|by_symbol| by_symbol.get(symbol)).ok_or_else(|| format!("missing Python reference ledger for {strategy}/{symbol}"))?;
            parity.insert(strategy.to_owned(), compare_ledger(rows, reference, symbol, &config.evaluation_window.start, &config.evaluation_window.end)?);
        }
        let mut strategy_summaries = BTreeMap::new();
        let mut symbol_paths = BTreeMap::new();
        for (strategy, rows) in &strategy_rows {
            let filename = format!("trades_{symbol}_{strategy}.csv.gz");
            let path = PathBuf::from(&filename);
            symbol_paths.insert(strategy.clone(), path);
            strategy_summaries.insert(strategy.clone(), summarize_strategy(rows)?);
        }
        strategy_rows.get(&config.selected_strategy).ok_or("selected strategy ledger missing")?;
        let selected_metrics = strategy_summaries.get(&config.selected_strategy).ok_or("selected strategy metrics missing")?;
        let selected_path = symbol_paths.get(&config.selected_strategy).ok_or("selected strategy path missing")?;
        symbol_runs.push(json!({
            "symbol": symbol,
            "selected_strategy": config.selected_strategy,
            "metrics": selected_metrics,
            "ledger_path": selected_path,
            "parity_exact": parity.get(&config.selected_strategy).and_then(|value| value.get("exact")),
            "parity_report": parity.get(&config.selected_strategy),
            "strategy_metrics": strategy_summaries
        }));
        run_reports.push(json!({
            "symbol": symbol,
            "canonical_sha256": bundle.canonical_sha256,
            "input_bars_including_warmup": bundle.bars.len(),
            "completed_sessions": sessions.len(),
            "sessions_with_at_least_minimum_bars": sessions.iter().filter(|session| session.bars >= config.protocol.min_session_bars).count(),
            "cftc_reports_for_market": cot.len(),
            "eligible_events_in_evaluation_window": evaluation_events.len(),
            "tff_aligned_event_fraction": if evaluation_events.is_empty() { 0.0 } else { evaluation_events.iter().filter(|event| event.row.tff_aligned).count() as f64 / evaluation_events.len() as f64 },
            "round_trip_cost": instrument.round_trip_cost,
            "point_value": instrument.point_value,
            "tick_size": instrument.tick_size,
            "tick_value": instrument.tick_value,
            "strategy_results": strategy_summaries,
            "parity": parity
        }));
        output_ledgers.insert(symbol.to_owned(), strategy_rows);
        reference_reports.insert(symbol.to_owned(), parity);
        all_input_files.insert(symbol.to_owned(), bundle.files);
    }
    let replay_ms = replay_started.elapsed().as_millis();

    let output_root = &config.output_root;
    fs::create_dir_all(output_root)?;
    let run_id = format!("run_{}_{}", Utc::now().format("%Y%m%dT%H%M%SZ"), std::process::id());
    let output_dir = output_root.join(&run_id);
    fs::create_dir(&output_dir)?;
    for (symbol, strategies) in &output_ledgers {
        for (strategy, rows) in strategies {
            let filename = format!("trades_{symbol}_{strategy}.csv.gz");
            write_gzip_csv(&output_dir.join(filename), rows)?;
        }
    }

    let mut input_manifest_hashes = BTreeMap::<String, String>::new();
    input_manifest_hashes.insert("bars_manifest_sha256".to_owned(), bars_manifest_sha);
    input_manifest_hashes.insert("warmup_manifest_sha256".to_owned(), warmup_manifest_sha);
    input_manifest_hashes.insert("cftc_manifest_sha256".to_owned(), cftc_manifest_sha);
    input_manifest_hashes.insert("cftc_gzip_sha256".to_owned(), cftc_sha);
    let mut python_source_hashes = vec![json!({
        "path": config.python_reference_source,
        "sha256": data::sha256_file(&config.python_reference_source)?
    })];
    for path in &config.additional_python_reference_sources {
        python_source_hashes.push(json!({"path": path, "sha256": data::sha256_file(path)?}));
    }
    let mut reference_ledger_hashes = BTreeMap::new();
    for (strategy, by_symbol) in &config.reference_ledgers {
        for (symbol, path) in by_symbol {
            reference_ledger_hashes.insert(format!("{strategy}/{symbol}"), data::sha256_file(path)?);
        }
    }
    let source_code_hashes = source_hashes()?;
    let config_sha = data::sha256_file(config_path)?;
    let manifest_results: Vec<Value> = symbol_runs.iter().map(|row| {
        let symbol = row.get("symbol").and_then(Value::as_str).unwrap_or_default();
        let ledger_path = row.pointer("/ledger_path").cloned().unwrap_or(Value::Null);
        let mut result = row.clone();
        if let Some(object) = result.as_object_mut() {
            object.insert("ledger_path".to_owned(), json!(ledger_path.as_str().unwrap_or_default()));
            object.insert("input_files".to_owned(), json!(all_input_files.get(symbol)));
        }
        result
    }).collect();

    let run_manifest_path = output_dir.join("run_manifest.json");
    let total_elapsed_ms = total_started.elapsed().as_millis();
    let run_manifest = json!({
        "schema": RUN_SCHEMA,
        "trade_schema": TRADE_SCHEMA,
        "study_id": config.study_id,
        "runner_package": "ancsertpx-tsm-replay",
        "run_id": run_id,
        "reference_version": config.reference_version,
        "configuration_path": config_path,
        "configuration_sha256": config_sha,
        "output_directory": output_dir,
        "selected_strategy": config.selected_strategy,
        "volatility_scaling": {
            "lookback_completed_cme_returns": config.volatility_scaling.lookback_returns,
            "annualized_estimator": "population_standard_deviation_of_simple_close_to_close_returns_times_sqrt_252",
            "uses_data_through": "last_completed_CME_session_close_before_entry_session_open",
            "target_annualized_volatility": config.volatility_scaling.target_annualized_volatility,
            "contract_rule": "round_half_up(target_volatility / realized_volatility), clipped to configured integer bounds",
            "minimum_contracts": config.volatility_scaling.minimum_contracts,
            "maximum_contracts": config.volatility_scaling.maximum_contracts,
            "passive_long_pairing": "same event intervals, entry/exit timestamps, realized-vol sizing, costs, and stress as TSM"
        },
        "robustness_contract": {
            "shared_walk_forward": "three equal timestamp-span segments; each must contain trades, positive net, and PF greater than 1",
            "shared_monte_carlo": "CPython-compatible seeded MT19937 with replacement; 1000 iterations; default shared drawdown threshold",
            "net_seed": SHARED_MC_SEED,
            "stress_seed": SHARED_MC_SEED + 1,
            "drawdown_threshold_usd": SHARED_DRAWDOWN_THRESHOLD,
            "date_cluster": "circular 13-event block bootstrap; 10000 draws; fixed seeds recorded in each result",
            "best_day_removal": "remove the entry date with the highest aggregate net P&L, then recompute net and stress"
        },
        "evaluation_window": {"start": config.evaluation_window.start, "end": config.evaluation_window.end},
        "input_manifest_hashes": input_manifest_hashes,
        "python_source_code_hashes": python_source_hashes,
        "python_reference_ledger_hashes": reference_ledger_hashes,
        "rust_runner_source_code_hashes": source_code_hashes,
        "execution_contract": {
            "lookback_completed_cme_sessions": config.protocol.lookback_sessions,
            "tff_availability": "report_asof_plus_calendar_days_at_America/New_York_local_time",
            "entry": "first_recorded_session_open_strictly_after_available_at",
            "exit": "next_eligible_CFTC_report_event_session_open",
            "position_size": "one contract for fixed-size reference ledgers; frozen volatility scaling for paired variants",
            "round_turn_cost_usd": "instrument specific; frozen in config",
            "stress_ticks_round_turn": config.protocol.stress_ticks,
            "overlapping_positions": false
        },
        "timing_ms": {"input_manifest_hashing": manifest_ms, "parquet_load_and_hash": load_ms, "event_build_and_replay": replay_ms, "total": total_elapsed_ms},
        "research_gates_pending": [
            "candidate must pass shared walk-forward and Monte Carlo gates in net and 14-tick stress",
            "date-cluster net/stress uncertainty and best-entry-day removal must support durability",
            "Topstep intraday unrealized MLL account-path simulation",
            "prospective shadow holdout after frozen rule"
        ],
        "results": manifest_results,
        "symbol_reports": run_reports
    });
    write_json_new(&run_manifest_path, &run_manifest)?;

    let summary_path = output_dir.join("summary.json");
    let summary = json!({
        "schema": "ancsertpx.tsm-rust-replay-summary.v1",
        "study_id": config.study_id,
        "run_id": run_manifest.get("run_id"),
        "output_directory": output_dir,
        "run_manifest_path": run_manifest_path,
        "selected_strategy": config.selected_strategy,
        "timing_ms": run_manifest.get("timing_ms"),
        "symbols": run_reports
    });
    write_json_new(&summary_path, &summary)?;
    let _ = reference_reports;
    Ok(summary)
}
