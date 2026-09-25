use crate::ResearchResult;
use serde::Serialize;
use serde_json::Value;
use std::fs::File;
use std::io::BufReader;
use std::path::{Path, PathBuf};

#[derive(Serialize)]
pub struct StudyValidationReport {
    pub schema: &'static str,
    pub study_id: String,
    pub config_path: String,
    pub runner_config_path: String,
    pub runner_package: String,
    pub status: String,
    pub runner_available: bool,
    pub executable: bool,
    pub research_gates_passed: bool,
    pub symbols: Vec<String>,
    pub retrospective_start: String,
    pub retrospective_end: String,
    pub implementation_gaps: Vec<String>,
}

fn required_string<'a>(value: &'a Value, pointer: &str) -> ResearchResult<&'a str> {
    value
        .pointer(pointer)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("missing string field {pointer}").into())
}

fn resolve_runner_config(study_path: &Path, value: &Value) -> ResearchResult<(String, PathBuf, Value)> {
    let runner = value
        .pointer("/replay")
        .ok_or("study replay config is required")?;
    let package = required_string(runner, "/runner_package")?;
    if !matches!(package, "ancsertpx-vp-replay" | "ancsertpx-tsm-replay") {
        return Err(format!("unsupported study runner package: {package}").into());
    }

    let configured_path = PathBuf::from(required_string(runner, "/config_path")?);
    let runner_path = if configured_path.is_absolute() {
        configured_path
    } else {
        study_path
            .parent()
            .unwrap_or_else(|| Path::new("."))
            .join(configured_path)
    };
    if !runner_path.is_file() {
        return Err(format!(
            "study replay config does not exist: {}",
            runner_path.display()
        )
        .into());
    }

    let runner_config: Value = serde_json::from_reader(BufReader::new(File::open(&runner_path)?))?;
    let expected_schema = match package {
        "ancsertpx-vp-replay" => "ancsertpx.vp-rust-baseline-replay-config.v1",
        "ancsertpx-tsm-replay" => "ancsertpx.tsm-rust-replay-config.v1",
        _ => unreachable!(),
    };
    if required_string(&runner_config, "/schema")? != expected_schema {
        return Err(format!("runner config schema does not match package {package}").into());
    }
    Ok((package.to_owned(), runner_path, runner_config))
}

/// Validate a versioned study specification and its linked replay config.
///
/// This operation performs blocking file I/O. Async callers should dispatch it
/// to a worker thread. Runner availability and research-gate completion are
/// reported independently.
pub fn validate_study(path: &Path) -> ResearchResult<StudyValidationReport> {
    let input = BufReader::new(File::open(path)?);
    let value: Value = serde_json::from_reader(input)?;

    let schema = required_string(&value, "/schema")?;
    if schema != "ancsertpx.study.v1" {
        return Err(format!("unsupported study schema: {schema}").into());
    }

    let study_id = required_string(&value, "/study_id")?.to_owned();
    if study_id.trim().is_empty() {
        return Err("study_id must not be empty".into());
    }

    let (runner_package, runner_path, runner_config) = resolve_runner_config(path, &value)?;
    if required_string(&runner_config, "/study_id")? != study_id {
        return Err("study replay config study_id does not match study spec".into());
    }

    let start = required_string(&value, "/retrospective_window/start")?.to_owned();
    let end = required_string(&value, "/retrospective_window/end")?.to_owned();
    if start.len() != 10 || end.len() != 10 || start > end {
        return Err("retrospective window must use ordered YYYY-MM-DD dates".into());
    }

    let symbols_value = value
        .pointer("/input_contract/symbols")
        .and_then(Value::as_array)
        .ok_or("input_contract.symbols must be an array")?;
    let mut symbols = Vec::with_capacity(symbols_value.len());
    for symbol in symbols_value {
        let symbol = symbol
            .as_str()
            .ok_or("each input symbol must be a string")?;
        if !matches!(symbol, "MNQ" | "MES") {
            return Err(format!("unsupported futures symbol: {symbol}").into());
        }
        symbols.push(symbol.to_owned());
    }
    if symbols.is_empty() {
        return Err("at least one futures symbol is required".into());
    }

    for pointer in [
        "/input_contract/MNQ_canonical_sha256",
        "/input_contract/MES_canonical_sha256",
    ] {
        let digest = required_string(&value, pointer)?;
        if digest.len() != 64 || !digest.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return Err(format!("{pointer} must be a 64-character SHA-256 hex digest").into());
        }
    }

    if runner_package == "ancsertpx-vp-replay" {
        let baseline = value
            .pointer("/price_baseline")
            .and_then(Value::as_object)
            .ok_or("price_baseline must be an object")?;
        for key in ["value_area_pct", "stop_atr", "target_atr"] {
            let number = baseline
                .get(key)
                .and_then(Value::as_f64)
                .ok_or_else(|| format!("price_baseline.{key} must be numeric"))?;
            if !number.is_finite() || number <= 0.0 {
                return Err(format!("price_baseline.{key} must be finite and positive").into());
            }
        }

        let option_context_enabled = value
            .pointer("/option_context/enabled")
            .and_then(Value::as_bool)
            .ok_or("option_context.enabled must be boolean")?;
        if option_context_enabled {
            return Err("the first parity study must keep option_context disabled".into());
        }
    } else if required_string(&runner_config, "/protocol/candidate_strategy")?
        != "60_session_tsm_with_lagged_tff_alignment"
    {
        return Err("unsupported TSM candidate strategy in runner config".into());
    }

    let status = required_string(&value, "/status")?.to_owned();
    let gaps_value = value
        .pointer("/research_gates_pending")
        .and_then(Value::as_array)
        .ok_or("research_gates_pending must be an array")?;
    let mut implementation_gaps = Vec::with_capacity(gaps_value.len());
    for gap in gaps_value {
        let gap = gap
            .as_str()
            .ok_or("each pending research gate must be a string")?;
        if gap.trim().is_empty() {
            return Err("pending research gate names must not be empty".into());
        }
        implementation_gaps.push(gap.to_owned());
    }

    let runner_available = true;
    let research_gates_passed = status == "research_gates_passed" && implementation_gaps.is_empty();
    if status == "research_gates_passed" && !research_gates_passed {
        return Err("research_gates_passed status requires an empty pending-gates list".into());
    }
    if status == "runner_available_research_gates_pending" && implementation_gaps.is_empty() {
        return Err("pending research status requires at least one open gate".into());
    }
    if !matches!(
        status.as_str(),
        "runner_available_research_gates_pending" | "research_gates_passed"
    ) {
        return Err(format!("unsupported study readiness status: {status}").into());
    }

    Ok(StudyValidationReport {
        schema: "ancsertpx.study-validation.v2",
        study_id,
        config_path: path.to_string_lossy().into_owned(),
        runner_config_path: runner_path.to_string_lossy().into_owned(),
        runner_package,
        status,
        runner_available,
        executable: runner_available,
        research_gates_passed,
        symbols,
        retrospective_start: start,
        retrospective_end: end,
        implementation_gaps,
    })
}
