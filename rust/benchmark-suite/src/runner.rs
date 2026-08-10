//! Production all-card/all-allocator benchmark runner.

use std::collections::BTreeSet;
use std::ffi::OsString;
use std::fs::{File, OpenOptions};
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use serde::Serialize;
use serde_json::json;

use crate::config::AllocatorLock;
use crate::model::{
    AllocatorIdentity, BenchmarkChildRequest, RawRun, RunnerMetadata, ToolchainMetadata,
    CHILD_PROTOCOL_VERSION,
};
use crate::orchestration::{calibrate_cell, run_balanced_cell, CellRunPlan, ChildProgram};
use crate::provenance::{sha256_bytes, ProducerProvenance};
use crate::scenarios::{cards, Topology};
use crate::{CORE_SUITE_VERSION, RAW_SCHEMA_VERSION};

const FULL_RUNTIME_LIMIT_SECONDS: f64 = 50.0 * 60.0;

#[derive(Debug)]
struct Options {
    provenance: PathBuf,
    output_dir: PathBuf,
    blocks: u32,
    run_seed: u64,
    timeout: Duration,
    warmup_transactions: u64,
    initial_transactions: u64,
    topology: Option<Topology>,
    reduced_smoke: bool,
}

pub fn benchmark_run_main() -> Result<(), String> {
    let options = parse_options(std::env::args_os().skip(1))?;
    run(options)
}

fn run(options: Options) -> Result<(), String> {
    if cfg!(not(target_os = "linux")) {
        return Err("the four-allocator producer is Linux-only".into());
    }
    if options.output_dir.exists() {
        return Err(format!(
            "output directory already exists: {}",
            options.output_dir.display()
        ));
    }
    std::fs::create_dir_all(&options.output_dir)
        .map_err(|error| format!("create output directory: {error}"))?;
    let lock =
        AllocatorLock::parse_and_validate(include_str!("../allocators/allocator-lock.json"))?;
    let provenance_bytes = std::fs::read(&options.provenance)
        .map_err(|error| format!("read allocator provenance: {error}"))?;
    let provenance_text = std::str::from_utf8(&provenance_bytes)
        .map_err(|error| format!("allocator provenance is not UTF-8: {error}"))?;
    let provenance = ProducerProvenance::parse_and_validate(provenance_text, &lock)?;
    provenance.validate_artifact_hashes()?;
    let provenance_sha256 = sha256_bytes(&provenance_bytes);
    write_new_bytes(
        options.output_dir.join("allocator-provenance.json"),
        &provenance_bytes,
    )?;
    let topology = options.topology.map_or_else(detect_topology, Ok)?;
    topology.validate().map_err(|error| error.to_string())?;
    let run_kind = if options.reduced_smoke {
        "reduced-smoke"
    } else {
        "headline"
    };
    if options.reduced_smoke && options.blocks != 1 {
        return Err("--reduced-smoke requires --blocks 1".into());
    }
    if !options.reduced_smoke && options.blocks < 15 {
        return Err("headline runs require --blocks at least 15".into());
    }

    let children = children_from_provenance(&provenance)?;
    let upstream = children
        .iter()
        .find(|child| child.allocator.allocator_id == "upstream-mimalloc")
        .ok_or_else(|| "provenance is missing upstream-mimalloc".to_string())?;
    let runner = RunnerMetadata {
        os: std::env::consts::OS.into(),
        architecture: std::env::consts::ARCH.into(),
        physical_cores: topology.physical_cores as u32,
        logical_cores: topology.logical_cores as u32,
    };

    let mut sample_output = create_new_writer(options.output_dir.join("raw-samples.jsonl"))?;
    let mut diagnostic_output = create_new_writer(options.output_dir.join("diagnostics.jsonl"))?;
    write_json_line(
        &mut diagnostic_output,
        &json!({
            "event": "run-start",
            "run_kind": run_kind,
            "run_seed": options.run_seed,
            "blocks": options.blocks,
            "runtime_environment": {
                "MIMALLOC_PROF": "0",
                "MIMALLOC_MEMORY_EVENTS": "0"
            }
        }),
    )?;
    let mut all_samples = Vec::new();
    let runner_started = Instant::now();
    let mut calibration_wall = Duration::ZERO;
    let mut block_wall = Duration::ZERO;

    for definition in cards() {
        for &thread_point in definition.thread_points {
            let mut request = BenchmarkChildRequest {
                protocol_version: CHILD_PROTOCOL_VERSION.into(),
                schema_version: RAW_SCHEMA_VERSION.into(),
                suite_version: CORE_SUITE_VERSION.into(),
                run_kind: run_kind.into(),
                execution_mode: "normal".into(),
                run_seed: options.run_seed,
                block_id: 0,
                ordinal: 0,
                workload_seed: 0,
                allocator: upstream.allocator.clone(),
                scenario_id: definition.id.as_str().into(),
                scenario_version: CORE_SUITE_VERSION.into(),
                thread_point: thread_point.name().into(),
                physical_cores: topology.physical_cores as u32,
                logical_cores: topology.logical_cores as u32,
                transactions_per_worker: options.initial_transactions,
                warmup_transactions_per_worker: options.warmup_transactions,
                reproduction_command: "calibration placeholder".into(),
                runner: runner.clone(),
                toolchain: upstream.toolchain.clone(),
            };
            write_json_line(
                &mut diagnostic_output,
                &json!({
                    "event": "cell-calibration-start",
                    "scenario_id": request.scenario_id,
                    "thread_point": request.thread_point,
                }),
            )?;
            let calibration_started = Instant::now();
            let calibration = calibrate_cell(upstream, &request, options.timeout)?;
            calibration_wall = calibration_wall.saturating_add(calibration_started.elapsed());
            request.transactions_per_worker = calibration.transactions_per_worker;
            write_json_line(
                &mut diagnostic_output,
                &json!({
                    "event": "cell-calibrated",
                    "scenario_id": request.scenario_id,
                    "thread_point": request.thread_point,
                    "transactions_per_worker": calibration.transactions_per_worker,
                    "operation_count": calibration.operation_count,
                    "elapsed_ns": calibration.elapsed_ns,
                }),
            )?;
            let request_directory = options.output_dir.join("requests");
            let plan = CellRunPlan {
                request_template: request,
                children: children.clone(),
                blocks: options.blocks,
                timeout: options.timeout,
                request_directory: Some(request_directory),
            };
            let block_started = Instant::now();
            let raw_cell =
                run_balanced_cell(&plan, |sample| write_json_line(&mut sample_output, sample))?;
            block_wall = block_wall.saturating_add(block_started.elapsed());
            write_json_line(
                &mut diagnostic_output,
                &json!({
                    "event": "cell-complete",
                    "scenario_id": definition.id.as_str(),
                    "thread_point": thread_point.name(),
                    "samples": raw_cell.samples.len(),
                }),
            )?;
            all_samples.extend(raw_cell.samples);
        }
    }

    let raw_run = RawRun {
        schema_version: RAW_SCHEMA_VERSION.into(),
        suite_version: CORE_SUITE_VERSION.into(),
        run_kind: run_kind.into(),
        execution_mode: "normal".into(),
        run_seed: options.run_seed,
        samples: all_samples,
    };
    let measured_seconds = raw_run
        .samples
        .iter()
        .map(|sample| {
            (sample.setup_ns + sample.warmup_ns + sample.elapsed_ns + sample.teardown_ns) as f64
                / 1_000_000_000.0
        })
        .sum::<f64>();
    write_new_json(options.output_dir.join("raw-run.json"), &raw_run)?;
    write_new_json(
        options.output_dir.join("run-provenance.json"),
        &json!({
            "run_kind": run_kind,
            "headline_eligible": !options.reduced_smoke,
            "runtime_environment": {
                "MIMALLOC_PROF": "0",
                "MIMALLOC_MEMORY_EVENTS": "0"
            },
            "allocator_provenance_file": "allocator-provenance.json",
            "allocator_provenance_sha256": provenance_sha256,
        }),
    )?;
    let observed_runner_wall = runner_started.elapsed();
    let fixed_wall = observed_runner_wall.saturating_sub(calibration_wall + block_wall);
    let native_build_seconds = provenance.build_elapsed_seconds;
    // One second conservatively covers the final projection-file write and
    // process shutdown, which occur after this observation is captured.
    let fixed_projection_seconds = fixed_wall.as_secs_f64() + 1.0;
    let projected_full_seconds = native_build_seconds
        + calibration_wall.as_secs_f64()
        + block_wall.as_secs_f64() * 15.0 / f64::from(options.blocks)
        + fixed_projection_seconds;
    if !projected_full_seconds.is_finite() {
        return Err("runtime projection is non-finite".into());
    }
    write_new_json(
        options.output_dir.join("runtime-projection.json"),
        &json!({
            "observed_blocks": options.blocks,
            "child_reported_observed_seconds": measured_seconds,
            "observed_runner_wall_seconds": observed_runner_wall.as_secs_f64(),
            "observed_calibration_wall_seconds": calibration_wall.as_secs_f64(),
            "observed_block_wall_seconds": block_wall.as_secs_f64(),
            "observed_fixed_wall_seconds": fixed_wall.as_secs_f64(),
            "native_build_elapsed_seconds": provenance.build_elapsed_seconds,
            "projected_calibration_seconds": calibration_wall.as_secs_f64(),
            "projected_block_seconds": block_wall.as_secs_f64() * 15.0 / f64::from(options.blocks),
            "projected_fixed_seconds": fixed_projection_seconds,
            "projected_headline_blocks": 15,
            "projected_full_suite_seconds": projected_full_seconds,
            "hard_limit_seconds": FULL_RUNTIME_LIMIT_SECONDS,
            "fits_limit": projected_full_seconds <= FULL_RUNTIME_LIMIT_SECONDS,
        }),
    )?;
    if projected_full_seconds > FULL_RUNTIME_LIMIT_SECONDS {
        return Err(format!(
            "projected full suite runtime {:.1}s exceeds the 3000s hard limit",
            projected_full_seconds
        ));
    }
    println!(
        "PASS {} cells, {} samples; projected full runtime {:.1}s",
        cards()
            .iter()
            .map(|card| card.thread_points.len())
            .sum::<usize>(),
        raw_run.samples.len(),
        projected_full_seconds
    );
    Ok(())
}

fn children_from_provenance(provenance: &ProducerProvenance) -> Result<Vec<ChildProgram>, String> {
    provenance
        .allocators
        .iter()
        .map(|item| {
            let path = PathBuf::from(&item.child_binary);
            if !path.is_file() {
                return Err(format!(
                    "allocator child does not exist: {}",
                    path.display()
                ));
            }
            Ok(ChildProgram {
                allocator: AllocatorIdentity {
                    allocator_id: item.allocator_id.clone(),
                    allocator_version: item.allocator_version.clone(),
                    source_sha: item.source_sha.clone(),
                    library_sha256: item.static_library_sha256.clone(),
                    child_binary_sha256: item.child_binary_sha256.clone(),
                },
                program: path,
                arguments: Vec::new(),
                toolchain: ToolchainMetadata {
                    rustc: rustc_version(),
                    target: "x86_64-unknown-linux-gnu".into(),
                    compiler: item.toolchain.compiler.clone(),
                    linker: item.toolchain.linker.clone(),
                },
                environment: Vec::new(),
            })
        })
        .collect()
}

fn detect_topology() -> Result<Topology, String> {
    let logical = std::thread::available_parallelism()
        .map_err(|error| format!("detect logical CPU count: {error}"))?
        .get();
    let cpu_root = Path::new("/sys/devices/system/cpu");
    let mut physical = BTreeSet::new();
    for entry in
        std::fs::read_dir(cpu_root).map_err(|error| format!("read Linux CPU topology: {error}"))?
    {
        let entry = entry.map_err(|error| format!("read Linux CPU topology entry: {error}"))?;
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if !name
            .strip_prefix("cpu")
            .is_some_and(|suffix| !suffix.is_empty() && suffix.bytes().all(|b| b.is_ascii_digit()))
        {
            continue;
        }
        let topology = entry.path().join("topology");
        let package = std::fs::read_to_string(topology.join("physical_package_id"))
            .map_err(|error| format!("read physical package ID: {error}"))?;
        let core = std::fs::read_to_string(topology.join("core_id"))
            .map_err(|error| format!("read physical core ID: {error}"))?;
        physical.insert((package.trim().to_owned(), core.trim().to_owned()));
    }
    if physical.is_empty() {
        return Err(
            "Linux physical-core topology unavailable; pass --physical-cores and --logical-cores"
                .into(),
        );
    }
    Ok(Topology {
        physical_cores: physical.len(),
        logical_cores: logical,
    })
}

fn rustc_version() -> String {
    std::process::Command::new("rustc")
        .arg("--version")
        .output()
        .ok()
        .filter(|output| output.status.success())
        .and_then(|output| String::from_utf8(output.stdout).ok())
        .map(|value| value.trim().to_owned())
        .unwrap_or_else(|| "rustc-version-unavailable".into())
}

fn create_new_writer(path: PathBuf) -> Result<BufWriter<File>, String> {
    OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&path)
        .map(BufWriter::new)
        .map_err(|error| format!("create {}: {error}", path.display()))
}

fn write_json_line<T: Serialize>(writer: &mut BufWriter<File>, value: &T) -> Result<(), String> {
    serde_json::to_writer(&mut *writer, value)
        .map_err(|error| format!("serialize JSONL record: {error}"))?;
    writer
        .write_all(b"\n")
        .and_then(|()| writer.flush())
        .map_err(|error| format!("append JSONL record: {error}"))
}

fn write_new_json<T: Serialize>(path: PathBuf, value: &T) -> Result<(), String> {
    let mut writer = create_new_writer(path)?;
    serde_json::to_writer_pretty(&mut writer, value)
        .map_err(|error| format!("serialize JSON artifact: {error}"))?;
    writer
        .write_all(b"\n")
        .and_then(|()| writer.flush())
        .map_err(|error| format!("finish JSON artifact: {error}"))
}

fn write_new_bytes(path: PathBuf, value: &[u8]) -> Result<(), String> {
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&path)
        .map_err(|error| format!("create {}: {error}", path.display()))?;
    file.write_all(value)
        .and_then(|()| file.flush())
        .map_err(|error| format!("write {}: {error}", path.display()))
}

fn parse_options(arguments: impl Iterator<Item = OsString>) -> Result<Options, String> {
    let arguments = arguments
        .map(|value| {
            value
                .into_string()
                .map_err(|_| "runner arguments must be UTF-8".to_string())
        })
        .collect::<Result<Vec<_>, _>>()?;
    let mut provenance = None;
    let mut build_root = None;
    let mut output_dir = None;
    let mut blocks = 15;
    let mut run_seed = 0x6d69_6d61_6c6c_6f63;
    let mut timeout_secs = 60;
    let mut warmup_transactions = 1;
    let mut initial_transactions = 1;
    let mut physical_cores = None;
    let mut logical_cores = None;
    let mut reduced_smoke = false;
    let mut index = 0;
    while index < arguments.len() {
        let flag = &arguments[index];
        if flag == "--reduced-smoke" {
            reduced_smoke = true;
            index += 1;
            continue;
        }
        let value = arguments
            .get(index + 1)
            .ok_or_else(|| format!("missing value for {flag}"))?;
        match flag.as_str() {
            "--provenance" => provenance = Some(PathBuf::from(value)),
            "--build-root" => build_root = Some(PathBuf::from(value)),
            "--output-dir" => output_dir = Some(PathBuf::from(value)),
            "--blocks" => blocks = parse_number(flag, value)?,
            "--run-seed" => run_seed = parse_number(flag, value)?,
            "--timeout-secs" => timeout_secs = parse_number(flag, value)?,
            "--warmup-transactions" => warmup_transactions = parse_number(flag, value)?,
            "--initial-transactions" => initial_transactions = parse_number(flag, value)?,
            "--physical-cores" => physical_cores = Some(parse_number(flag, value)?),
            "--logical-cores" => logical_cores = Some(parse_number(flag, value)?),
            _ => return Err(format!("unknown argument {flag}")),
        }
        index += 2;
    }
    if blocks == 0 || timeout_secs == 0 || initial_transactions == 0 {
        return Err("blocks, timeout, and initial transactions must be nonzero".into());
    }
    let topology = match (physical_cores, logical_cores) {
        (None, None) => None,
        (Some(physical_cores), Some(logical_cores)) => Some(Topology {
            physical_cores,
            logical_cores,
        }),
        _ => return Err("physical and logical core overrides must be supplied together".into()),
    };
    if provenance.is_some() && build_root.is_some() {
        return Err("pass either --provenance or --build-root, not both".into());
    }
    let provenance = provenance
        .or_else(|| build_root.map(|root| root.join("allocator-provenance.json")))
        .ok_or("--provenance or --build-root is required")?;
    Ok(Options {
        provenance,
        output_dir: output_dir.ok_or("--output-dir is required")?,
        blocks,
        run_seed,
        timeout: Duration::from_secs(timeout_secs),
        warmup_transactions,
        initial_transactions,
        topology,
        reduced_smoke,
    })
}

fn parse_number<T: std::str::FromStr>(flag: &str, value: &str) -> Result<T, String> {
    value
        .parse()
        .map_err(|_| format!("invalid numeric value for {flag}: {value}"))
}
