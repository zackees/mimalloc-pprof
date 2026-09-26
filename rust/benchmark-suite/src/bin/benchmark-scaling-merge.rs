use std::io::Write;
use std::path::{Path, PathBuf};

use benchmark_suite::scaling::{
    merge_scaling_runs, scaling_thread_points_for_shard, synthetic_scaling_fixture,
    validate_scaling_raw_run, ScalingRawRun, SCALING_THREAD_POINTS, THREAD_CHURN_THREADS,
};

fn main() {
    if let Err(error) = run() {
        eprintln!("benchmark-scaling-merge: {error}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), String> {
    let mut inputs = Vec::new();
    let mut report_out = None;
    let mut arguments = std::env::args().skip(1);
    while let Some(argument) = arguments.next() {
        match argument.as_str() {
            "--input" => inputs.push(PathBuf::from(
                arguments.next().ok_or("--input requires a path")?,
            )),
            "--report-out" => {
                report_out = Some(PathBuf::from(
                    arguments.next().ok_or("--report-out requires a path")?,
                ))
            }
            "--selftest" => return selftest(),
            "--help" | "-h" => {
                println!("usage: benchmark-scaling-merge --input <shard.json> [--input <shard.json> ...] --report-out <scaling-raw-run.json> | --selftest");
                return Ok(());
            }
            _ => return Err(format!("unknown argument: {argument}")),
        }
    }
    let runs = inputs
        .iter()
        .map(|path| read_json(path))
        .collect::<Result<Vec<ScalingRawRun>, _>>()?;
    let merged = merge_scaling_runs(runs)?;
    write_new_json(&report_out.ok_or("--report-out is required")?, &merged)?;
    println!(
        "PASS merged {} scaling samples across {} cells ({})",
        merged.samples.len(),
        merged.calibrations.len(),
        merged.status
    );
    Ok(())
}

fn selftest() -> Result<(), String> {
    let raw = synthetic_scaling_fixture(0x6d69_6d61_6c6c_6f63)?;
    let shards = (0..SCALING_THREAD_POINTS.len())
        .map(|shard_index| {
            let threads =
                scaling_thread_points_for_shard(shard_index, SCALING_THREAD_POINTS.len())?;
            let mut shard = raw.clone();
            shard.status = "incomplete".into();
            shard
                .calibrations
                .retain(|value| threads.contains(&value.thread_count));
            shard
                .samples
                .retain(|value| threads.contains(&value.thread_count));
            // thread-churn is recorded by the shard that measures its worker count.
            if !threads.contains(&THREAD_CHURN_THREADS) {
                shard.thread_churn_samples.clear();
            }
            Ok(shard)
        })
        .collect::<Result<Vec<_>, String>>()?;
    let merged = merge_scaling_runs(shards)?;
    validate_scaling_raw_run(&merged)?;
    println!("PASS benchmark-scaling-merge selftest");
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
