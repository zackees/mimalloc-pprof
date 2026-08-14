use std::io::Write;
use std::path::{Path, PathBuf};

use benchmark_suite::model::LatestReport;
use benchmark_suite::scaling::{
    attach_scaling_report, build_scaling_report, synthetic_scaling_fixture,
    validate_scaling_raw_run, validate_scaling_report, ScalingRawRun,
};

fn main() {
    if let Err(error) = run() {
        eprintln!("benchmark-scaling-validate: {error}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), String> {
    let mut arguments = std::env::args().skip(1);
    let mut input = None;
    let mut base_latest = None;
    let mut report_out = None;
    let mut latest_out = None;
    let mut fixture_out = None;
    while let Some(argument) = arguments.next() {
        let target = match argument.as_str() {
            "--input" => &mut input,
            "--base-latest" => &mut base_latest,
            "--report-out" => &mut report_out,
            "--latest-out" => &mut latest_out,
            "--fixture-out" => &mut fixture_out,
            "--selftest" => return selftest(),
            "--help" | "-h" => {
                println!("usage: benchmark-scaling-validate --selftest | --fixture-out <scaling-raw-run.json> | --input <scaling-raw-run.json> --base-latest <latest.json> --report-out <scaling-report.json> --latest-out <latest.json>");
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
    if let Some(path) = fixture_out {
        let raw = synthetic_scaling_fixture(0x6d69_6d61_6c6c_6f63)?;
        validate_scaling_raw_run(&raw)?;
        write_new_json(&path, &raw)?;
        println!("PASS wrote a complete scaling fixture with {} samples", raw.samples.len());
        return Ok(());
    }
    let raw: ScalingRawRun = read_json(&input.ok_or("--input is required")?)?;
    let report = build_scaling_report(&raw)?;
    let mut latest: LatestReport = read_json(&base_latest.ok_or("--base-latest is required")?)?;
    if latest.validation_report.status != "valid"
        || !latest.validation_report.headline_eligible
        || !latest.validation_report.errors.is_empty()
    {
        return Err("base latest report is not validated/headline-eligible".into());
    }
    attach_scaling_report(&mut latest, report.clone())?;
    write_new_json(&report_out.ok_or("--report-out is required")?, &report)?;
    write_new_json(&latest_out.ok_or("--latest-out is required")?, &latest)?;
    println!(
        "PASS validated {} scaling samples across {} cells; key={}",
        report.raw_samples.len(),
        report.cell_summaries.len(),
        report.metric_comparison_key
    );
    Ok(())
}

/// Negative controls: a complete fixture must validate, and each declared
/// failure mode must be rejected rather than silently published.
fn selftest() -> Result<(), String> {
    let raw = synthetic_scaling_fixture(0x6d69_6d61_6c6c_6f63)?;
    validate_scaling_raw_run(&raw)?;
    let report = build_scaling_report(&raw)?;
    validate_scaling_report(&report)?;

    let mut truncated = raw.clone();
    truncated.samples.pop();
    if validate_scaling_raw_run(&truncated).is_ok() {
        return Err("selftest: an incomplete matrix was accepted".into());
    }

    let mut tampered = raw.clone();
    tampered.samples[0].response.checksum ^= 1;
    if validate_scaling_raw_run(&tampered).is_ok() {
        return Err("selftest: a tampered checksum was accepted".into());
    }

    let mut reseeded = raw.clone();
    reseeded.run_seed ^= 0xff;
    if validate_scaling_raw_run(&reseeded).is_ok() {
        return Err("selftest: a mismatched run seed was accepted".into());
    }

    let mut relabeled = report.clone();
    relabeled.rigor_label = "headline".into();
    if validate_scaling_report(&relabeled).is_ok() {
        return Err("selftest: a report without the coverage-mode label was accepted".into());
    }

    let mut widened = report;
    widened.thread_points = vec![1, 4, 16, 64];
    if validate_scaling_report(&widened).is_ok() {
        return Err("selftest: a report with undeclared thread points was accepted".into());
    }
    println!("PASS benchmark-scaling-validate selftest");
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
