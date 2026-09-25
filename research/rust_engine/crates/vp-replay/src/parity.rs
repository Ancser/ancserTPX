use flate2::read::GzDecoder;
use serde::Serialize;

use std::fs::File;
use std::path::Path;

#[derive(Clone, Debug, Serialize)]
pub struct CellMismatch {
    pub row_number: usize,
    pub field: String,
    pub reference: String,
    pub rust: String,
}

#[derive(Clone, Debug, Serialize)]
pub struct ParityReport {
    pub schema: &'static str,
    pub reference_rows: usize,
    pub rust_rows: usize,
    pub reference_columns: Vec<String>,
    pub rust_columns: Vec<String>,
    pub header_equal: bool,
    pub mismatch_cells: usize,
    pub sample_mismatches: Vec<CellMismatch>,
    pub numeric_tolerance: f64,
    pub exact_field_parity: bool,
}

fn is_numeric(field: &str) -> bool {
    matches!(
        field,
        "entry_price"
            | "exit_price"
            | "sl_price"
            | "tp_price"
            | "original_sl_price"
            | "original_tp_price"
            | "gross_pnl"
            | "commission"
            | "fees"
            | "net_pnl"
    )
}

fn equal_cell(field: &str, reference: &str, candidate: &str, tolerance: f64) -> bool {
    if is_numeric(field) {
        match (reference.parse::<f64>(), candidate.parse::<f64>()) {
            (Ok(left), Ok(right)) => {
                left.is_finite() && right.is_finite() && (left - right).abs() <= tolerance
            }
            _ => reference == candidate,
        }
    } else {
        reference == candidate
    }
}

pub fn compare_gzip_csv(
    reference_path: &Path,
    candidate_path: &Path,
) -> Result<ParityReport, Box<dyn std::error::Error>> {
    let mut reference =
        csv::ReaderBuilder::new().from_reader(GzDecoder::new(File::open(reference_path)?));
    let mut candidate =
        csv::ReaderBuilder::new().from_reader(GzDecoder::new(File::open(candidate_path)?));
    let ref_headers = reference.headers()?.clone();
    let cand_headers = candidate.headers()?.clone();
    let reference_columns: Vec<String> = ref_headers.iter().map(str::to_owned).collect();
    let rust_columns: Vec<String> = cand_headers.iter().map(str::to_owned).collect();
    let header_equal = reference_columns == rust_columns;
    let fields: Vec<String> = if header_equal {
        reference_columns.clone()
    } else {
        Vec::new()
    };
    let tolerance = 1e-9;
    let mut reference_rows = 0;
    let mut rust_rows = 0;
    let mut mismatch_cells = 0;
    let mut samples = Vec::new();

    loop {
        let ref_row = reference.records().next().transpose()?;
        let cand_row = candidate.records().next().transpose()?;
        if ref_row.is_none() && cand_row.is_none() {
            break;
        }
        reference_rows += usize::from(ref_row.is_some());
        rust_rows += usize::from(cand_row.is_some());
        let row_number = reference_rows.max(rust_rows);
        let ref_present = ref_row.is_some();
        let cand_present = cand_row.is_some();
        match (ref_row, cand_row) {
            (Some(left), Some(right)) => {
                let width = left.len().max(right.len()).max(fields.len());
                for index in 0..width {
                    let field = fields
                        .get(index)
                        .cloned()
                        .unwrap_or_else(|| format!("column_{index}"));
                    let left_value = left.get(index).unwrap_or("");
                    let right_value = right.get(index).unwrap_or("");
                    if !equal_cell(&field, left_value, right_value, tolerance) {
                        mismatch_cells += 1;
                        if samples.len() < 200 {
                            samples.push(CellMismatch {
                                row_number,
                                field,
                                reference: left_value.to_owned(),
                                rust: right_value.to_owned(),
                            });
                        }
                    }
                }
            }
            (Some(_), None) | (None, Some(_)) => {
                mismatch_cells += fields.len().max(1);
                if samples.len() < 200 {
                    samples.push(CellMismatch {
                        row_number,
                        field: "__row_count__".to_owned(),
                        reference: if ref_present {
                            "present".to_owned()
                        } else {
                            "missing".to_owned()
                        },
                        rust: if cand_present {
                            "present".to_owned()
                        } else {
                            "missing".to_owned()
                        },
                    });
                }
            }
            (None, None) => break,
        }
    }

    let exact_field_parity = header_equal && reference_rows == rust_rows && mismatch_cells == 0;
    Ok(ParityReport {
        schema: "ancsertpx.vp-ledger-field-parity.v1",
        reference_rows,
        rust_rows,
        reference_columns,
        rust_columns,
        header_equal,
        mismatch_cells,
        sample_mismatches: samples,
        numeric_tolerance: tolerance,
        exact_field_parity,
    })
}
