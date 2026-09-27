use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::fs::{File, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct JournalRecord {
    pub sequence: u64,
    pub kind: String,
    pub payload: Value,
    pub previous_sha256: String,
    pub sha256: String,
}

pub struct Journal {
    path: PathBuf,
    lock_path: PathBuf,
    _lock_file: Option<File>,
    file: File,
    next_sequence: u64,
    previous_sha256: String,
}

impl Journal {
    pub fn open(path: impl AsRef<Path>) -> Result<Self, Box<dyn std::error::Error>> {
        let path = path.as_ref().to_path_buf();
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let lock_path = PathBuf::from(format!("{}.lock", path.display()));
        let mut lock_file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&lock_path)
            .map_err(|error| {
                std::io::Error::new(
                    error.kind(),
                    format!(
                        "journal_writer_lock_unavailable:{}:{error}",
                        lock_path.display()
                    ),
                )
            })?;
        use std::io::Write as _;
        if let Err(error) =
            writeln!(lock_file, "pid={}", std::process::id()).and_then(|_| lock_file.sync_all())
        {
            drop(lock_file);
            let _ = std::fs::remove_file(&lock_path);
            return Err(error.into());
        }
        let (next_sequence, previous_sha256) = match verify(&path) {
            Ok(state) => state,
            Err(error) => {
                drop(lock_file);
                let _ = std::fs::remove_file(&lock_path);
                return Err(error);
            }
        };
        let file = match OpenOptions::new().create(true).append(true).open(&path) {
            Ok(file) => file,
            Err(error) => {
                drop(lock_file);
                let _ = std::fs::remove_file(&lock_path);
                return Err(error.into());
            }
        };
        Ok(Self {
            path,
            lock_path,
            _lock_file: Some(lock_file),
            file,
            next_sequence,
            previous_sha256,
        })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn append<T: Serialize>(
        &mut self,
        kind: &str,
        payload: &T,
    ) -> Result<JournalRecord, Box<dyn std::error::Error>> {
        if kind.trim().is_empty() {
            return Err("journal record kind must be non-empty".into());
        }
        let value = serde_json::to_value(payload)?;
        let sha256 = record_hash(self.next_sequence, kind, &value, &self.previous_sha256)?;
        let record = JournalRecord {
            sequence: self.next_sequence,
            kind: kind.to_owned(),
            payload: value,
            previous_sha256: self.previous_sha256.clone(),
            sha256: sha256.clone(),
        };
        serde_json::to_writer(&mut self.file, &record)?;
        self.file.write_all(b"\n")?;
        self.file.sync_data()?;
        self.next_sequence += 1;
        self.previous_sha256 = sha256;
        Ok(record)
    }

    pub fn records(&self) -> Result<Vec<JournalRecord>, Box<dyn std::error::Error>> {
        read_verified(&self.path)
    }
}

impl Drop for Journal {
    fn drop(&mut self) {
        self._lock_file.take();
        let _ = std::fs::remove_file(&self.lock_path);
    }
}

fn verify(path: &Path) -> Result<(u64, String), Box<dyn std::error::Error>> {
    if !path.exists() {
        return Ok((0, String::new()));
    }
    let records = read_verified(path)?;
    let next = records
        .last()
        .map(|record| record.sequence + 1)
        .unwrap_or(0);
    let previous = records
        .last()
        .map(|record| record.sha256.clone())
        .unwrap_or_default();
    Ok((next, previous))
}

fn read_verified(path: &Path) -> Result<Vec<JournalRecord>, Box<dyn std::error::Error>> {
    if !path.exists() {
        return Ok(Vec::new());
    }
    let file = File::open(path)?;
    let reader = BufReader::new(file);
    let mut records = Vec::new();
    let mut expected_sequence = 0_u64;
    let mut previous = String::new();
    for (line_number, line) in reader.lines().enumerate() {
        let line = line?;
        if line.trim().is_empty() {
            return Err(format!("empty journal record at line {}", line_number + 1).into());
        }
        let record: JournalRecord = serde_json::from_str(&line).map_err(|error| {
            format!(
                "invalid journal record at line {}: {error}",
                line_number + 1
            )
        })?;
        if record.sequence != expected_sequence || record.previous_sha256 != previous {
            return Err(format!(
                "journal sequence or chain mismatch at line {}",
                line_number + 1
            )
            .into());
        }
        let expected_hash = record_hash(
            record.sequence,
            &record.kind,
            &record.payload,
            &record.previous_sha256,
        )?;
        if !expected_hash.eq_ignore_ascii_case(&record.sha256) {
            return Err(format!("journal checksum mismatch at line {}", line_number + 1).into());
        }
        expected_sequence += 1;
        previous = record.sha256.clone();
        records.push(record);
    }
    Ok(records)
}

fn record_hash(
    sequence: u64,
    kind: &str,
    payload: &Value,
    previous_sha256: &str,
) -> Result<String, Box<dyn std::error::Error>> {
    let canonical = serde_json::to_vec(&(sequence, kind, payload, previous_sha256))?;
    let digest = Sha256::digest(canonical);
    Ok(digest.iter().map(|byte| format!("{byte:02x}")).collect())
}
