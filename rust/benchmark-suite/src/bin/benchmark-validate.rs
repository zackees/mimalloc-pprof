use std::path::PathBuf;

use benchmark_suite::report::build_latest_report;
use benchmark_suite::validate::{self, parse_and_validate};

fn main() {
    if let Err(error) = run() {
        eprintln!("benchmark-validate: {error}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), String> {
    let mut arguments = std::env::args().skip(1);
    let mut input = None;
    let mut report_out = None;
    let mut latest_out = None;
    let mut history_row_out = None;
    let mut fixture_out = None;
    let mut selftest = false;
    while let Some(argument) = arguments.next() {
        match argument.as_str() {
            "--selftest" => selftest = true,
            "--input" => {
                input = Some(PathBuf::from(
                    arguments
                        .next()
                        .ok_or_else(|| "--input requires a path".to_string())?,
                ));
            }
            "--report-out" => {
                report_out = Some(PathBuf::from(
                    arguments
                        .next()
                        .ok_or_else(|| "--report-out requires a path".to_string())?,
                ));
            }
            "--latest-out" => {
                latest_out = Some(PathBuf::from(
                    arguments
                        .next()
                        .ok_or_else(|| "--latest-out requires a path".to_string())?,
                ));
            }
            "--history-row-out" => {
                history_row_out =
                    Some(PathBuf::from(arguments.next().ok_or_else(|| {
                        "--history-row-out requires a path".to_string()
                    })?));
            }
            "--fixture-out" => {
                fixture_out =
                    Some(PathBuf::from(arguments.next().ok_or_else(|| {
                        "--fixture-out requires a path".to_string()
                    })?));
            }
            "--help" | "-h" => {
                println!(
                    "usage: benchmark-validate --selftest | (--input <publication-raw.json> | --fixture-out <publication-raw.json>) [--report-out <validation-report.json>] [--latest-out <latest.json>] [--history-row-out <history-row.json>]"
                );
                return Ok(());
            }
            _ => return Err(format!("unknown argument: {argument}")),
        }
    }
    if selftest {
        if input.is_some()
            || report_out.is_some()
            || latest_out.is_some()
            || history_row_out.is_some()
            || fixture_out.is_some()
        {
            return Err("--selftest cannot be combined with validation paths".into());
        }
        validate::selftest().map_err(|error| error.to_string())?;
        let raw = validate::synthetic_full_fixture().map_err(|error| error.to_string())?;
        let report = validate::validate_publication_raw(&raw).map_err(|error| error.to_string())?;
        let (latest, history) = build_latest_report(&raw, report)?;
        serde_json::to_vec(&latest).map_err(|error| error.to_string())?;
        serde_json::to_vec(&history).map_err(|error| error.to_string())?;
        println!("PASS benchmark validator selftest");
        return Ok(());
    }
    if input.is_some() && fixture_out.is_some() {
        return Err("--input and --fixture-out are mutually exclusive".into());
    }
    let (raw, report) = if let Some(path) = fixture_out {
        let raw = validate::synthetic_full_fixture().map_err(|error| error.to_string())?;
        let bytes = serde_json::to_vec_pretty(&raw).map_err(|error| error.to_string())?;
        write_new(&path, &bytes)?;
        let report = validate::validate_publication_raw(&raw).map_err(|error| error.to_string())?;
        (raw, report)
    } else {
        let input = input.ok_or_else(|| "missing --input (or use --selftest)".to_string())?;
        let input_bytes = std::fs::read_to_string(&input)
            .map_err(|error| format!("{}: unable to read: {error}", input.display()))?;
        parse_and_validate(&input_bytes).map_err(|error| format!("{}: {error}", input.display()))?
    };
    let encoded = serde_json::to_vec_pretty(&report).map_err(|error| error.to_string())?;
    if let Some(path) = report_out {
        write_new(&path, &encoded)?;
    }
    if latest_out.is_some() || history_row_out.is_some() {
        let (latest, history) = build_latest_report(&raw, report.clone())?;
        if let Some(path) = latest_out {
            let bytes = serde_json::to_vec_pretty(&latest).map_err(|error| error.to_string())?;
            write_new(&path, &bytes)?;
        }
        if let Some(path) = history_row_out {
            let bytes = serde_json::to_vec(&history).map_err(|error| error.to_string())?;
            write_new(&path, &bytes)?;
        }
    }
    println!("{}", String::from_utf8_lossy(&encoded));
    Ok(())
}

fn write_new(path: &std::path::Path, bytes: &[u8]) -> Result<(), String> {
    use std::io::Write;

    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(|error| format!("{}: unable to create: {error}", path.display()))?;
    file.write_all(bytes)
        .and_then(|()| file.write_all(b"\n"))
        .map_err(|error| format!("{}: unable to write: {error}", path.display()))
}
