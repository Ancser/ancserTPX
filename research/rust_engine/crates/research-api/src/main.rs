use ancsertpx_research_api::{compare_runs, explain_trade, list_studies, run_study, run_study_selected};
use serde_json::Value;
use std::env;
use std::ffi::OsString;
use std::path::PathBuf;

fn usage() -> &'static str {
    "usage:\n  ancsertpx-research-api list-studies <catalog.json>\n  ancsertpx-research-api run-study <catalog.json> <study_id> [supported_strategy]\n  ancsertpx-research-api compare-runs <base_run_manifest.json> <candidate_run_manifest.json>\n  ancsertpx-research-api explain-trade <run_manifest.json> <MNQ|MES> <reference_trade_seq>"
}

fn text_arg(value: &OsString, name: &str) -> Result<String, String> {
    value
        .to_str()
        .map(str::to_owned)
        .ok_or_else(|| format!("{name} must be valid UTF-8"))
}

fn execute(args: Vec<OsString>) -> Result<Value, String> {
    let Some(command) = args.first().and_then(|value| value.to_str()) else {
        return Err(usage().to_owned());
    };
    match (command, args.len()) {
        ("list-studies", 2) => {
            list_studies(&PathBuf::from(&args[1])).map_err(|error| error.to_string())
        }
        ("run-study", 3) => {
            let study_id = text_arg(&args[2], "study_id")?;
            run_study(&PathBuf::from(&args[1]), &study_id).map_err(|error| error.to_string())
        }
        ("run-study", 4) => {
            let study_id = text_arg(&args[2], "study_id")?;
            let strategy = text_arg(&args[3], "supported_strategy")?;
            run_study_selected(&PathBuf::from(&args[1]), &study_id, Some(&strategy))
                .map_err(|error| error.to_string())
        }
        ("compare-runs", 3) => compare_runs(&PathBuf::from(&args[1]), &PathBuf::from(&args[2]))
            .map_err(|error| error.to_string()),
        ("explain-trade", 4) => {
            let symbol = text_arg(&args[2], "symbol")?;
            let seq = text_arg(&args[3], "reference_trade_seq")?
                .parse::<u32>()
                .map_err(|_| "reference_trade_seq must be a nonnegative integer".to_owned())?;
            explain_trade(&PathBuf::from(&args[1]), &symbol, seq).map_err(|error| error.to_string())
        }
        _ => Err(usage().to_owned()),
    }
}

fn main() {
    match execute(env::args_os().skip(1).collect()) {
        Ok(value) => match serde_json::to_string_pretty(&value) {
            Ok(json) => println!("{json}"),
            Err(error) => {
                eprintln!("could not serialize result: {error}");
                std::process::exit(1);
            }
        },
        Err(error) => {
            eprintln!("{error}");
            std::process::exit(2);
        }
    }
}
