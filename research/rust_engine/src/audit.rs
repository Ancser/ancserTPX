use crate::ResearchResult;
use csv::{ByteRecord, ReaderBuilder};
use flate2::read::GzDecoder;
use serde::Serialize;
use std::fs::{self, File};
use std::io::BufReader;
use std::path::{Path, PathBuf};
use std::time::Instant;

#[derive(Serialize)]
pub struct FileAudit {
    pub path: String,
    pub compressed_bytes: u64,
    pub rows: u64,
}

#[derive(Serialize)]
pub struct AuditReport {
    pub schema: &'static str,
    pub input_root: String,
    pub file_count: usize,
    pub total_compressed_bytes: u64,
    pub total_rows: u64,
    pub elapsed_ms: u128,
    pub rows_per_second: u64,
    pub files: Vec<FileAudit>,
}

fn collect_csv_gz(current: &Path, files: &mut Vec<PathBuf>) -> std::io::Result<()> {
    for entry in fs::read_dir(current)? {
        let entry = entry?;
        let path = entry.path();
        let kind = entry.file_type()?;
        if kind.is_dir() {
            collect_csv_gz(&path, files)?;
        } else if kind.is_file()
            && path
                .file_name()
                .is_some_and(|name| name.to_string_lossy().ends_with(".csv.gz"))
        {
            files.push(path);
        }
    }
    Ok(())
}

fn audit_file(root: &Path, path: &Path) -> ResearchResult<FileAudit> {
    let compressed_bytes = fs::metadata(path)?.len();
    let input = BufReader::new(File::open(path)?);
    let decoder = GzDecoder::new(input);
    let mut reader = ReaderBuilder::new().has_headers(true).from_reader(decoder);
    let headers = reader.byte_headers()?.clone();
    if headers.is_empty() || headers.iter().any(|field| field.is_empty()) {
        return Err(format!("invalid or empty CSV header: {}", path.display()).into());
    }

    let mut record = ByteRecord::new();
    let mut rows = 0_u64;
    while reader.read_byte_record(&mut record)? {
        rows += 1;
    }

    Ok(FileAudit {
        path: path
            .strip_prefix(root)
            .unwrap_or(path)
            .to_string_lossy()
            .replace('\\', "/"),
        compressed_bytes,
        rows,
    })
}

/// Recursively audit sorted `.csv.gz` inputs without modifying source files.
///
/// This operation performs blocking disk and decompression work. Async callers
/// should dispatch it to a worker thread.
pub fn audit_inputs(root: &Path) -> ResearchResult<AuditReport> {
    if !root.is_dir() {
        return Err(format!("input path is not a directory: {}", root.display()).into());
    }

    let started = Instant::now();
    let mut paths = Vec::new();
    collect_csv_gz(root, &mut paths)?;
    paths.sort();
    if paths.is_empty() {
        return Err(format!("no .csv.gz files found under {}", root.display()).into());
    }

    let mut files = Vec::with_capacity(paths.len());
    for path in &paths {
        files.push(audit_file(root, path)?);
    }

    let total_compressed_bytes = files.iter().map(|file| file.compressed_bytes).sum();
    let total_rows: u64 = files.iter().map(|file| file.rows).sum();
    let elapsed = started.elapsed();
    let elapsed_seconds = elapsed.as_secs_f64();
    let rows_per_second = if elapsed_seconds > 0.0 {
        (total_rows as f64 / elapsed_seconds) as u64
    } else {
        0
    };

    Ok(AuditReport {
        schema: "ancsertpx.archive-audit.v1",
        input_root: root.to_string_lossy().into_owned(),
        file_count: files.len(),
        total_compressed_bytes,
        total_rows,
        elapsed_ms: elapsed.as_millis(),
        rows_per_second,
        files,
    })
}
