use crate::model::{Bar, DataSource};
use arrow::array::{
    Array, BooleanArray, Date32Array, Float64Array, Int64Array, StringArray,
    TimestampMicrosecondArray,
};
use arrow::datatypes::{DataType, TimeUnit};
use chrono::{Duration, NaiveDate};
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use serde::Serialize;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::fs::{self, File};
use std::io::{BufReader, Read};
use std::path::{Path, PathBuf};

#[derive(Clone, Debug, Serialize)]
pub struct InputFileAudit {
    pub path: String,
    pub sha256: String,
    pub bytes: u64,
    pub rows: usize,
}

#[derive(Debug)]
pub struct DataBundle {
    pub bars: Vec<Bar>,
    pub files: Vec<InputFileAudit>,
    pub canonical_sha256: String,
}

fn json_file(path: &Path) -> Result<Value, Box<dyn std::error::Error>> {
    Ok(serde_json::from_reader(BufReader::new(File::open(path)?))?)
}

fn required<'a>(value: &'a Value, pointer: &str) -> Result<&'a Value, Box<dyn std::error::Error>> {
    value
        .pointer(pointer)
        .ok_or_else(|| format!("missing JSON field {pointer}").into())
}

fn required_string<'a>(
    value: &'a Value,
    pointer: &str,
) -> Result<&'a str, Box<dyn std::error::Error>> {
    required(value, pointer)?
        .as_str()
        .ok_or_else(|| format!("expected string at {pointer}").into())
}

fn date32(value: i32) -> Result<NaiveDate, Box<dyn std::error::Error>> {
    NaiveDate::from_ymd_opt(1970, 1, 1)
        .and_then(|epoch| epoch.checked_add_signed(Duration::days(i64::from(value))))
        .ok_or_else(|| format!("invalid Arrow Date32 value {value}").into())
}

pub fn sha256_file(path: &Path) -> Result<String, Box<dyn std::error::Error>> {
    let mut input = BufReader::new(File::open(path)?);
    let mut digest = Sha256::new();
    let mut buffer = vec![0_u8; 8 * 1024 * 1024];
    loop {
        let count = input.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
    }
    Ok(format!("{:x}", digest.finalize()))
}

fn verify_hash(path: &Path, expected: &str) -> Result<String, Box<dyn std::error::Error>> {
    let actual = sha256_file(path)?;
    if !actual.eq_ignore_ascii_case(expected) {
        return Err(format!(
            "Parquet SHA-256 mismatch at {}: expected {}, got {}",
            path.display(),
            expected,
            actual
        )
        .into());
    }
    Ok(actual)
}

fn read_partition(
    path: &Path,
    symbol_expected: &str,
    expected_sha256: &str,
    expected_rows: usize,
    bars: &mut Vec<Bar>,
    previous_ts_us: &mut Option<i64>,
) -> Result<InputFileAudit, Box<dyn std::error::Error>> {
    let sha256 = verify_hash(path, expected_sha256)?;
    let bytes = fs::metadata(path)?.len();
    let builder = ParquetRecordBatchReaderBuilder::try_new(File::open(path)?)?;
    let schema = builder.schema().clone();
    let symbol_i = schema.index_of("symbol")?;
    let source_i = schema.index_of("source")?;
    let timestamp_i = schema.index_of("source_timestamp_utc")?;
    let rth_date_i = schema.index_of("timestamp_date_et")?;
    let trade_date_i = schema.index_of("topstep_trade_date_ct")?;
    let is_rth_i = schema.index_of("is_rth")?;
    let open_i = schema.index_of("open")?;
    let high_i = schema.index_of("high")?;
    let low_i = schema.index_of("low")?;
    let close_i = schema.index_of("close")?;
    let volume_i = schema.index_of("volume")?;

    match schema.field(timestamp_i).data_type() {
        DataType::Timestamp(TimeUnit::Microsecond, Some(tz)) if tz.as_ref() == "UTC" => {}
        kind => {
            return Err(format!(
                "unsupported UTC timestamp type {kind:?} in {}",
                path.display()
            )
            .into());
        }
    }

    let mut reader = builder.with_batch_size(65_536).build()?;
    let mut partition_rows = 0_usize;
    while let Some(batch_result) = reader.next() {
        let batch = batch_result?;
        let symbols = batch
            .column(symbol_i)
            .as_any()
            .downcast_ref::<StringArray>()
            .ok_or("symbol column type mismatch")?;
        let sources = batch
            .column(source_i)
            .as_any()
            .downcast_ref::<StringArray>()
            .ok_or("source column type mismatch")?;
        let timestamps = batch
            .column(timestamp_i)
            .as_any()
            .downcast_ref::<TimestampMicrosecondArray>()
            .ok_or("UTC timestamp column type mismatch")?;
        let rth_dates = batch
            .column(rth_date_i)
            .as_any()
            .downcast_ref::<Date32Array>()
            .ok_or("RTH date column type mismatch")?;
        let trade_dates = batch
            .column(trade_date_i)
            .as_any()
            .downcast_ref::<Date32Array>()
            .ok_or("Topstep date column type mismatch")?;
        let rth = batch
            .column(is_rth_i)
            .as_any()
            .downcast_ref::<BooleanArray>()
            .ok_or("RTH column type mismatch")?;
        let opens = batch
            .column(open_i)
            .as_any()
            .downcast_ref::<Float64Array>()
            .ok_or("open column type mismatch")?;
        let highs = batch
            .column(high_i)
            .as_any()
            .downcast_ref::<Float64Array>()
            .ok_or("high column type mismatch")?;
        let lows = batch
            .column(low_i)
            .as_any()
            .downcast_ref::<Float64Array>()
            .ok_or("low column type mismatch")?;
        let closes = batch
            .column(close_i)
            .as_any()
            .downcast_ref::<Float64Array>()
            .ok_or("close column type mismatch")?;
        let volumes = batch
            .column(volume_i)
            .as_any()
            .downcast_ref::<Int64Array>()
            .ok_or("volume column type mismatch")?;

        for index in 0..batch.num_rows() {
            let required_columns = [
                symbol_i,
                source_i,
                timestamp_i,
                rth_date_i,
                trade_date_i,
                is_rth_i,
                open_i,
                high_i,
                low_i,
                close_i,
                volume_i,
            ];
            if required_columns
                .iter()
                .any(|column| batch.column(*column).is_null(index))
            {
                return Err(format!(
                    "null in a required field at {} row {}",
                    path.display(),
                    partition_rows + index
                )
                .into());
            }
            let symbol = symbols.value(index);
            if symbol != symbol_expected {
                return Err(format!("unexpected symbol {symbol} in {}", path.display()).into());
            }
            let timestamp_us = timestamps.value(index);
            if previous_ts_us.is_some_and(|prior| timestamp_us <= prior) {
                return Err(format!(
                    "timestamps are duplicated or unsorted at {} row {}",
                    path.display(),
                    partition_rows + index
                )
                .into());
            }
            *previous_ts_us = Some(timestamp_us);
            bars.push(Bar {
                timestamp_us,
                source: DataSource::from_label(sources.value(index))?,
                rth_date_et: date32(rth_dates.value(index))?,
                topstep_trade_date_ct: date32(trade_dates.value(index))?,
                is_rth: rth.value(index),
                open: opens.value(index),
                high: highs.value(index),
                low: lows.value(index),
                close: closes.value(index),
                volume: volumes.value(index),
            });
        }
        partition_rows += batch.num_rows();
    }

    if partition_rows != expected_rows {
        return Err(format!(
            "Parquet row count mismatch at {}: expected {}, read {}",
            path.display(),
            expected_rows,
            partition_rows
        )
        .into());
    }

    Ok(InputFileAudit {
        path: path.to_string_lossy().into_owned(),
        sha256,
        bytes,
        rows: partition_rows,
    })
}

pub fn load_symbol(
    symbol: &str,
    full_root: &Path,
    full_manifest_path: &Path,
    warmup_root: &Path,
    warmup_manifest_path: &Path,
) -> Result<DataBundle, Box<dyn std::error::Error>> {
    let full_manifest = json_file(full_manifest_path)?;
    let warm_manifest = json_file(warmup_manifest_path)?;
    let symbol_key = symbol.to_ascii_uppercase();
    let full_symbol = required(&full_manifest, &format!("/symbols/{symbol_key}"))?;
    let warm_symbol = required(&warm_manifest, &format!("/symbols/{symbol_key}"))?;
    let canonical_sha = required_string(full_symbol, "/canonical_sha256")?.to_ascii_lowercase();
    let warm_canonical_sha =
        required_string(warm_symbol, "/canonical_sha256")?.to_ascii_lowercase();
    if canonical_sha != warm_canonical_sha {
        return Err(format!(
            "canonical source hash differs between full export and warmup for {symbol_key}"
        )
        .into());
    }

    let mut files_to_read: Vec<(PathBuf, String, usize)> = Vec::new();
    let warm_name = PathBuf::from(required_string(warm_symbol, "/parquet_path")?)
        .file_name()
        .ok_or("warmup manifest parquet path has no file name")?
        .to_owned();
    files_to_read.push((
        warmup_root.join(warm_name),
        required_string(warm_symbol, "/parquet_sha256")?.to_owned(),
        required(warm_symbol, "/rows")?
            .as_u64()
            .ok_or("warmup rows is not an integer")? as usize,
    ));

    let yearly = required(full_symbol, "/files")?;
    for year in 2022..=2026 {
        let entry = yearly
            .get(year.to_string())
            .ok_or_else(|| format!("missing {symbol_key} {year} partition in manifest"))?;
        let relative = required_string(entry, "/path")?;
        let path = full_root.join(relative);
        let sha = required_string(entry, "/sha256")?.to_owned();
        let rows = required(entry, "/rows")?
            .as_u64()
            .ok_or("yearly rows is not an integer")? as usize;
        files_to_read.push((path, sha, rows));
    }

    let mut bars = Vec::new();
    let expected_total: usize = files_to_read.iter().map(|(_, _, rows)| *rows).sum();
    bars.reserve(expected_total);
    let mut files = Vec::with_capacity(files_to_read.len());
    let mut previous_ts_us = None;
    for (path, sha, rows) in files_to_read {
        files.push(read_partition(
            &path,
            &symbol_key,
            &sha,
            rows,
            &mut bars,
            &mut previous_ts_us,
        )?);
    }
    if bars.len() != expected_total {
        return Err(format!(
            "input total row mismatch: expected {expected_total}, read {}",
            bars.len()
        )
        .into());
    }

    Ok(DataBundle {
        bars,
        files,
        canonical_sha256: canonical_sha,
    })
}
