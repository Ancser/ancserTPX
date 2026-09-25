use std::path::PathBuf;

fn main() {
    let mut args = std::env::args_os().skip(1);
    if args.next().as_deref() != Some(std::ffi::OsStr::new("--config")) {
        eprintln!("usage: ancsertpx-vp-replay --config <path>");
        std::process::exit(2);
    }
    let Some(config_path) = args.next() else {
        eprintln!("missing config path");
        std::process::exit(2);
    };
    if args.next().is_some() {
        eprintln!("usage: ancsertpx-vp-replay --config <path>");
        std::process::exit(2);
    }

    match ancsertpx_vp_replay::run_vp_replay(PathBuf::from(config_path)) {
        Ok(report) => println!(
            "{}",
            serde_json::to_string_pretty(&report).expect("JSON serialization")
        ),
        Err(error) => {
            eprintln!("Rust VP replay failed: {error}");
            std::process::exit(1);
        }
    }
}
