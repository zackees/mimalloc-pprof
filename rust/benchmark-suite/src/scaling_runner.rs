//! Production runner for the sparse thread-scaling sweep.

use std::ffi::OsString;
use std::path::PathBuf;
use std::time::{Duration, Instant};

use serde_json::json;

use crate::config::AllocatorLock;
use crate::orchestration::{balanced_block_orders, ALLOCATOR_IDS};
use crate::provenance::ProducerProvenance;
use crate::runner::{
    children_from_provenance, collect_publication_runner, collect_run_identity, create_new_writer,
    detect_topology, publication_allocators, write_json_line, write_new_bytes, write_new_json,
};
use crate::scaling::{
    run_scaling_child, run_scaling_child_with_plan, simulate_cell, validate_scaling_raw_run,
    ScalingCalibration, ScalingChildRequest, ScalingChildResponse, ScalingPattern, ScalingRawRun,
    ScalingRawSample, ScalingTopology, SCALING_BLOCKS, SCALING_CHILD_PROTOCOL_VERSION,
    SCALING_MAX_BLOCK_NS, SCALING_MIN_BLOCK_NS, SCALING_PATTERNS, SCALING_SCHEMA_VERSION,
    SCALING_TARGET_BLOCK_NS, SCALING_THREAD_POINTS,
};
use crate::scenarios::Topology;

/// Coverage mode exists to keep the daily signal cheap; a sweep that cannot
/// finish inside this budget is a failure, not something to publish slowly.
const HARD_LIMIT_SECONDS: f64 = 15.0 * 60.0;
const CALIBRATION_ATTEMPTS: u32 = 12;

#[derive(Debug)]
struct Options {
    provenance: PathBuf,
    output_dir: PathBuf,
    blocks: u32,
    run_seed: u64,
    timeout: Duration,
    warmup_operations: u64,
    initial_operations: u64,
    topology: Option<Topology>,
    reduced_smoke: bool,
}

pub fn benchmark_scaling_run_main() -> Result<(), String> {
    let options = parse_options(std::env::args_os().skip(1))?;
    let output_dir = options.output_dir.clone();
    let output_preexisted = output_dir.exists();
    match run(options) {
        Ok(()) => Ok(()),
        Err(error) => {
            if !output_preexisted && output_dir.is_dir() {
                record_invalid_run(&output_dir, &error)?;
            }
            Err(error)
        }
    }
}

fn record_invalid_run(output_dir: &std::path::Path, reason: &str) -> Result<(), String> {
    let invalid = output_dir.join("scaling-invalid.json");
    if !invalid.exists() {
        write_new_json(
            invalid,
            &json!({
                "metric_schema_version": SCALING_SCHEMA_VERSION,
                "status": "invalid",
                "reason": reason,
            }),
        )?;
    }
    let diagnostics_path = output_dir.join("diagnostics.jsonl");
    let diagnostics_file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&diagnostics_path)
        .map_err(|error| format!("open {}: {error}", diagnostics_path.display()))?;
    let mut diagnostics = std::io::BufWriter::new(diagnostics_file);
    write_json_line(
        &mut diagnostics,
        &json!({
            "event": "scaling-run-invalid",
            "metric_schema_version": SCALING_SCHEMA_VERSION,
            "reason": reason,
        }),
    )
}

/// Calibrate one (pattern, thread point) against upstream-mimalloc only. The
/// resulting per-worker operation count is frozen across all five allocators,
/// which is what keeps the five lines on one facet comparable.
fn calibrate(
    child: &crate::orchestration::ChildProgram,
    template: &ScalingChildRequest,
    timeout: Duration,
) -> Result<(u64, ScalingChildResponse), String> {
    if child.allocator.allocator_id != "upstream-mimalloc" {
        return Err("only upstream-mimalloc may calibrate a scaling cell".into());
    }
    let mut operations = template.operations_per_worker.max(1);
    for _ in 0..CALIBRATION_ATTEMPTS {
        let mut probe = template.clone();
        probe.operations_per_worker = operations;
        probe.reproduction_command = format!(
            "scaling calibration probe pattern={} threads={}",
            template.pattern, template.thread_count
        );
        let (response, _peak_rss) = run_scaling_child(child, &probe, timeout)?;
        if (SCALING_MIN_BLOCK_NS..=SCALING_MAX_BLOCK_NS).contains(&response.elapsed_ns) {
            return Ok((operations, response));
        }
        let scaled = (operations as u128 * u128::from(SCALING_TARGET_BLOCK_NS)
            / u128::from(response.elapsed_ns.max(1)))
        .max(1);
        let bounded = scaled
            .min(u128::from(operations).saturating_mul(16))
            .max(u128::from(operations) / 16);
        let next = u64::try_from(bounded).map_err(|_| "scaling calibration overflowed")?;
        if next == operations {
            return Err(format!(
                "scaling calibration for {}/{} did not converge",
                template.pattern, template.thread_count
            ));
        }
        operations = next.max(1);
    }
    Err(format!(
        "scaling calibration for {}/{} exhausted its attempts",
        template.pattern, template.thread_count
    ))
}

fn run(options: Options) -> Result<(), String> {
    if cfg!(not(target_os = "linux")) {
        return Err("throughput-scaling-sparse-v1 production collection is Linux-only".into());
    }
    if options.output_dir.exists() {
        return Err(format!(
            "output directory already exists: {}",
            options.output_dir.display()
        ));
    }
    std::fs::create_dir_all(&options.output_dir)
        .map_err(|error| format!("create scaling output: {error}"))?;
    if options.reduced_smoke {
        if options.blocks != 1 {
            return Err("scaling reduced smoke requires --blocks 1".into());
        }
    } else if options.blocks != SCALING_BLOCKS {
        return Err(format!(
            "complete scaling runs use exactly --blocks {SCALING_BLOCKS}"
        ));
    }

    let lock =
        AllocatorLock::parse_and_validate(include_str!("../allocators/allocator-lock.json"))?;
    let provenance_bytes = std::fs::read(&options.provenance)
        .map_err(|error| format!("read allocator provenance: {error}"))?;
    let provenance_text = std::str::from_utf8(&provenance_bytes)
        .map_err(|error| format!("allocator provenance is not UTF-8: {error}"))?;
    let provenance = ProducerProvenance::parse_and_validate(provenance_text, &lock)?;
    provenance.validate_artifact_hashes()?;
    write_new_bytes(
        options.output_dir.join("allocator-provenance.json"),
        &provenance_bytes,
    )?;

    let topology = options.topology.map_or_else(detect_topology, Ok)?;
    let children = children_from_provenance(&provenance)?;
    let upstream = children
        .iter()
        .find(|child| child.allocator.allocator_id == "upstream-mimalloc")
        .ok_or("provenance is missing upstream-mimalloc")?;
    let run_identity = collect_run_identity(&provenance)?;
    let publication_runner = collect_publication_runner(topology)?;
    let runner = crate::model::RunnerMetadata {
        os: publication_runner.os.clone(),
        architecture: publication_runner.architecture.clone(),
        physical_cores: publication_runner.physical_cores,
        logical_cores: publication_runner.logical_cores,
    };
    let scaling_topology = ScalingTopology {
        physical_cores: publication_runner.physical_cores,
        logical_cores: publication_runner.logical_cores,
        allowed_logical_cpus: publication_runner.logical_cores,
        affinity_policy: publication_runner.affinity.policy.clone(),
    };

    let mut raw_jsonl = create_new_writer(options.output_dir.join("raw-scaling-samples.jsonl"))?;
    let mut diagnostics = create_new_writer(options.output_dir.join("diagnostics.jsonl"))?;
    write_json_line(
        &mut diagnostics,
        &json!({
            "event": "scaling-run-start", "metric_schema_version": SCALING_SCHEMA_VERSION,
            "blocks": options.blocks, "run_seed": options.run_seed,
            "thread_points": SCALING_THREAD_POINTS, "patterns": SCALING_PATTERNS.map(ScalingPattern::as_str),
        }),
    )?;

    let runner_started = Instant::now();
    let mut calibration_wall = Duration::ZERO;
    let mut block_wall = Duration::ZERO;
    let mut calibrations = Vec::new();
    let mut samples = Vec::new();
    let request_dir = options.output_dir.join("requests");
    std::fs::create_dir_all(&request_dir)
        .map_err(|error| format!("create scaling request dir: {error}"))?;

    for pattern in SCALING_PATTERNS {
        for thread_count in SCALING_THREAD_POINTS {
            let template = ScalingChildRequest {
                protocol_version: SCALING_CHILD_PROTOCOL_VERSION.into(),
                metric_schema_version: SCALING_SCHEMA_VERSION.into(),
                run_seed: options.run_seed,
                pattern: pattern.as_str().into(),
                thread_count,
                block_id: 0,
                ordinal: 0,
                operations_per_worker: options.initial_operations,
                warmup_operations_per_worker: options.warmup_operations,
                allocator: upstream.allocator.clone(),
                runner: runner.clone(),
                toolchain: upstream.toolchain.clone(),
                reproduction_command: "scaling calibration placeholder".into(),
            };
            let started = Instant::now();
            let (operations_per_worker, probe) = calibrate(upstream, &template, options.timeout)?;
            calibration_wall = calibration_wall.saturating_add(started.elapsed());
            calibrations.push(ScalingCalibration {
                pattern: pattern.as_str().into(),
                thread_count,
                operations_per_worker,
                warmup_operations_per_worker: options.warmup_operations,
                elapsed_ns: probe.elapsed_ns,
            });
            let cell_key = format!("{}/{thread_count}", pattern.as_str());

            let started = Instant::now();
            for order in balanced_block_orders(options.blocks, options.run_seed)? {
                // The plan is allocator-independent, so derive it once per
                // block and reuse it for all five children. Replaying it per
                // child would cost as much as the measurement itself.
                let plan = simulate_cell(
                    pattern,
                    options.run_seed,
                    thread_count,
                    order.block_id,
                    operations_per_worker,
                );
                for (ordinal, allocator_id) in order.allocator_ids.iter().enumerate() {
                    let child = children
                        .iter()
                        .find(|value| value.allocator.allocator_id == *allocator_id)
                        .ok_or_else(|| format!("missing scaling allocator {allocator_id}"))?;
                    let mut request = template.clone();
                    request.block_id = order.block_id;
                    request.ordinal = ordinal as u8;
                    request.operations_per_worker = operations_per_worker;
                    request.allocator = child.allocator.clone();
                    request.toolchain = child.toolchain.clone();
                    let request_path = request_dir.join(format!(
                        "{}-{}-block-{:04}-ordinal-{}-{}.json",
                        pattern.as_str(),
                        thread_count,
                        order.block_id,
                        ordinal,
                        allocator_id
                    ));
                    request.reproduction_command = format!(
                        "MIMALLOC_PROF=0 MIMALLOC_MEMORY_EVENTS=0 '{}' --scaling < '{}'",
                        child.program.display(),
                        request_path.display()
                    );
                    write_new_json(request_path.clone(), &request)?;
                    let (response, peak_rss_bytes) =
                        run_scaling_child_with_plan(child, &request, options.timeout, &plan)?;
                    let sample = ScalingRawSample {
                        metric_schema_version: SCALING_SCHEMA_VERSION.into(),
                        block_id: order.block_id,
                        ordinal: ordinal as u8,
                        pattern: pattern.as_str().into(),
                        thread_count,
                        allocator_id: allocator_id.clone(),
                        allocator_source_sha: child.allocator.source_sha.clone(),
                        child_binary_sha256: child.allocator.child_binary_sha256.clone(),
                        operations_per_worker,
                        peak_rss_bytes,
                        reproduction_command: request.reproduction_command.clone(),
                        response,
                    };
                    write_json_line(&mut raw_jsonl, &sample)?;
                    samples.push(sample);
                }
            }
            block_wall = block_wall.saturating_add(started.elapsed());
            write_json_line(
                &mut diagnostics,
                &json!({
                    "event": "scaling-cell-complete", "cell": cell_key,
                    "operations_per_worker": operations_per_worker,
                    "calibrated_elapsed_ns": probe.elapsed_ns,
                    "samples": options.blocks * ALLOCATOR_IDS.len() as u32,
                }),
            )?;
        }
    }

    let observed_wall = runner_started.elapsed();
    let fixed_wall = observed_wall.saturating_sub(calibration_wall + block_wall);
    let projected_full_seconds = provenance.build_elapsed_seconds
        + calibration_wall.as_secs_f64()
        + block_wall.as_secs_f64() * f64::from(SCALING_BLOCKS) / f64::from(options.blocks)
        + fixed_wall.as_secs_f64()
        + 1.0;
    write_new_json(
        options.output_dir.join("runtime-projection.json"),
        &json!({
            "observed_blocks": options.blocks,
            "observed_runner_wall_seconds": observed_wall.as_secs_f64(),
            "observed_calibration_wall_seconds": calibration_wall.as_secs_f64(),
            "observed_block_wall_seconds": block_wall.as_secs_f64(),
            "native_build_elapsed_seconds": provenance.build_elapsed_seconds,
            "projected_blocks": SCALING_BLOCKS,
            "projected_full_suite_seconds": projected_full_seconds,
            "hard_limit_seconds": HARD_LIMIT_SECONDS,
            "fits_limit": projected_full_seconds <= HARD_LIMIT_SECONDS,
        }),
    )?;
    if projected_full_seconds > HARD_LIMIT_SECONDS {
        let reason = format!(
            "projected complete scaling runtime {projected_full_seconds:.1}s exceeds the {HARD_LIMIT_SECONDS:.0}s budget"
        );
        write_new_json(
            options.output_dir.join("scaling-invalid.json"),
            &json!({ "metric_schema_version": SCALING_SCHEMA_VERSION, "status": "invalid", "reason": reason }),
        )?;
        return Err(reason);
    }
    let raw = ScalingRawRun {
        metric_schema_version: SCALING_SCHEMA_VERSION.into(),
        status: if options.reduced_smoke {
            "incomplete"
        } else {
            "complete"
        }
        .into(),
        run_seed: options.run_seed,
        run: run_identity,
        runner: publication_runner,
        topology: scaling_topology,
        allocator_lock_sha256: provenance.lockfile_sha256.clone(),
        allocators: publication_allocators(&lock, &provenance)?,
        calibrations,
        samples,
    };
    if !options.reduced_smoke {
        validate_scaling_raw_run(&raw)?;
    }
    write_new_json(options.output_dir.join("scaling-raw-run.json"), &raw)?;
    println!(
        "PASS scaling sweep: {} raw records across {} cells; projected runtime {:.1}s",
        raw.samples.len(),
        SCALING_PATTERNS.len() * SCALING_THREAD_POINTS.len(),
        projected_full_seconds
    );
    Ok(())
}

fn parse_options(arguments: impl Iterator<Item = OsString>) -> Result<Options, String> {
    let arguments = arguments
        .map(|value| {
            value
                .into_string()
                .map_err(|_| "arguments must be valid UTF-8".to_string())
        })
        .collect::<Result<Vec<_>, _>>()?;
    let mut provenance = None;
    let mut build_root = None;
    let mut output_dir = None;
    let mut blocks = SCALING_BLOCKS;
    let mut run_seed = 0x6d69_6d61_6c6c_6f63u64;
    let mut timeout = Duration::from_secs(120);
    let mut warmup_operations = 1024u64;
    let mut initial_operations = 4096u64;
    let mut physical_cores = None;
    let mut logical_cores = None;
    let mut reduced_smoke = false;
    let mut index = 0;
    while index < arguments.len() {
        let flag = arguments[index].as_str();
        if flag == "--reduced-smoke" {
            reduced_smoke = true;
            index += 1;
            continue;
        }
        if flag == "--help" || flag == "-h" {
            println!("usage: benchmark-scaling-run (--provenance <allocator-provenance.json> | --build-root <dir>) --output-dir <new-dir> [--blocks 3] [--run-seed N] [--timeout-secs N] [--warmup-operations N] [--initial-operations N] [--physical-cores N] [--logical-cores N] [--reduced-smoke]");
            std::process::exit(0);
        }
        let value = arguments
            .get(index + 1)
            .ok_or_else(|| format!("{flag} requires a value"))?;
        match flag {
            "--provenance" => provenance = Some(PathBuf::from(value)),
            "--build-root" => build_root = Some(PathBuf::from(value)),
            "--output-dir" => output_dir = Some(PathBuf::from(value)),
            "--blocks" => blocks = parse_number("--blocks", value)?,
            "--run-seed" => run_seed = parse_number("--run-seed", value)?,
            "--timeout-secs" => {
                timeout = Duration::from_secs(parse_number("--timeout-secs", value)?)
            }
            "--warmup-operations" => {
                warmup_operations = parse_number("--warmup-operations", value)?
            }
            "--initial-operations" => {
                initial_operations = parse_number("--initial-operations", value)?
            }
            "--physical-cores" => physical_cores = Some(parse_number("--physical-cores", value)?),
            "--logical-cores" => logical_cores = Some(parse_number("--logical-cores", value)?),
            _ => return Err(format!("unknown argument: {flag}")),
        }
        index += 2;
    }
    if run_seed == 0 {
        return Err("--run-seed must be non-zero".into());
    }
    if initial_operations == 0 {
        return Err("--initial-operations must be non-zero".into());
    }
    if provenance.is_some() && build_root.is_some() {
        return Err("pass either --provenance or --build-root, not both".into());
    }
    let provenance = provenance
        .or_else(|| build_root.map(|root| root.join("allocator-provenance.json")))
        .ok_or("--provenance or --build-root is required")?;
    let topology = match (physical_cores, logical_cores) {
        (Some(physical), Some(logical)) => Some(Topology {
            physical_cores: physical,
            logical_cores: logical,
        }),
        (None, None) => None,
        _ => return Err("--physical-cores and --logical-cores must be given together".into()),
    };
    Ok(Options {
        provenance,
        output_dir: output_dir.ok_or("--output-dir is required")?,
        blocks,
        run_seed,
        timeout,
        warmup_operations,
        initial_operations,
        topology,
        reduced_smoke,
    })
}

fn parse_number<T: std::str::FromStr>(flag: &str, value: &str) -> Result<T, String> {
    value
        .parse()
        .map_err(|_| format!("{flag} expects a non-negative integer, got {value:?}"))
}
