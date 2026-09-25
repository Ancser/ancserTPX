use ancsertpx_rust_research::{audit_inputs, validate_study};
use std::env;
use std::path::PathBuf;

fn main() {
    let mut args = env::args_os().skip(1);
    let command = args.next();
    let input = args.next();
    let extra = args.next();

    let Some(command) = command else {
        eprintln!("usage: ancsertpx-rust-research <audit-gzip-csv|validate-study> <path>");
        std::process::exit(2);
    };
    let Some(input) = input else {
        eprintln!("usage: ancsertpx-rust-research <audit-gzip-csv|validate-study> <path>");
        std::process::exit(2);
    };
    if extra.is_some() {
        eprintln!("usage: ancsertpx-rust-research <audit-gzip-csv|validate-study> <path>");
        std::process::exit(2);
    }

    let path = PathBuf::from(input);
    let result = match command.to_string_lossy().as_ref() {
        "audit-gzip-csv" => {
            audit_inputs(&path).and_then(|report| serde_json::to_value(report).map_err(Into::into))
        }
        "validate-study" => validate_study(&path)
            .and_then(|report| serde_json::to_value(report).map_err(Into::into)),
        other => {
            eprintln!("unknown command: {other}");
            eprintln!("usage: ancsertpx-rust-research <audit-gzip-csv|validate-study> <path>");
            std::process::exit(2);
        }
    };

    match result {
        Ok(value) => match serde_json::to_string_pretty(&value) {
            Ok(json) => println!("{json}"),
            Err(error) => {
                eprintln!("could not serialize report: {error}");
                std::process::exit(1);
            }
        },
        Err(error) => {
            eprintln!("command failed: {error}");
            std::process::exit(1);
        }
    }
}
