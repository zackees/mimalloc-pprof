use std::io::Write;
use std::path::{Path, PathBuf};

use benchmark_suite::memory::{attach_memory_report, build_memory_report, MemoryRawRun};
use benchmark_suite::model::LatestReport;

fn main() {
    if let Err(error) = run() {
        eprintln!("benchmark-memory-validate: {error}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), String> {
    let mut arguments = std::env::args().skip(1);
    let mut input = None;
    let mut base_latest = None;
    let mut report_out = None;
    let mut latest_out = None;
    while let Some(argument) = arguments.next() {
        let target = match argument.as_str() {
            "--input" => &mut input,
            "--base-latest" => &mut base_latest,
            "--report-out" => &mut report_out,
            "--latest-out" => &mut latest_out,
            "--help" | "-h" => {
                println!("usage: benchmark-memory-validate --input <memory-raw-run.json> --base-latest <latest.json> --report-out <memory-report.json> --latest-out <latest.json>");
                return Ok(());
            }
            _ => return Err(format!("unknown argument: {argument}")),
        };
        *target = Some(PathBuf::from(
            arguments
                .next()
                .ok_or_else(|| format!("{argument} requires a path"))?,
        ));
    }
    let input = input.ok_or("--input is required")?;
    let base_latest = base_latest.ok_or("--base-latest is required")?;
    let report_out = report_out.ok_or("--report-out is required")?;
    let latest_out = latest_out.ok_or("--latest-out is required")?;
    let raw: MemoryRawRun = read_json(&input)?;
    let report = build_memory_report(&raw)?;
    let mut latest: LatestReport = read_json(&base_latest)?;
    if latest.validation_report.status != "valid"
        || !latest.validation_report.headline_eligible
        || !latest.validation_report.errors.is_empty()
    {
        return Err("base latest report is not validated/headline-eligible".into());
    }
    attach_memory_report(&mut latest, report.clone())?;
    write_new_json(&report_out, &report)?;
    write_new_json(&latest_out, &latest)?;
    println!(
        "PASS validated {} memory samples; key={}",
        report.raw_samples.len(),
        report.metric_comparison_key
    );
    Ok(())
}

fn read_json<T: serde::de::DeserializeOwned>(path: &Path) -> Result<T, String> {
    let bytes = std::fs::read(path).map_err(|error| format!("{}: {error}", path.display()))?;
    serde_json::from_slice(&bytes).map_err(|error| format!("{}: {error}", path.display()))
}

fn write_new_json<T: serde::Serialize>(path: &Path, value: &T) -> Result<(), String> {
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(|error| format!("{}: {error}", path.display()))?;
    serde_json::to_writer_pretty(&mut file, value)
        .map_err(|error| format!("{}: {error}", path.display()))?;
    file.write_all(b"\n")
        .map_err(|error| format!("{}: {error}", path.display()))
}
