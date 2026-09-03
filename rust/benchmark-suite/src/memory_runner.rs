//! Production runner for the bounded Linux process-memory suite.

use std::ffi::OsString;
use std::path::PathBuf;
use std::time::{Duration, Instant};

use serde_json::json;

use crate::config::AllocatorLock;
use crate::memory::{
    collect_memory_environment, memory_scenario_cells, run_memory_child_sample,
    validate_memory_raw_run, MemoryRawRun,
};
use crate::model::{
    BenchmarkChildRequest, CellCalibration, RunnerMetadata, CHILD_PROTOCOL_VERSION,
};
use crate::orchestration::{balanced_block_orders, calibrate_cell, ALLOCATOR_IDS};
use crate::provenance::ProducerProvenance;
use crate::runner::{
    children_from_provenance, collect_publication_runner, collect_run_identity, create_new_writer,
    detect_topology, publication_allocators, write_json_line, write_new_bytes, write_new_json,
};
use crate::scenarios::{card, ScenarioCell, Topology};
use crate::{CORE_SUITE_VERSION, RAW_SCHEMA_VERSION};

const HARD_LIMIT_SECONDS: f64 = 60.0 * 60.0;

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

pub fn benchmark_memory_run_main() -> Result<(), String> {
    run(parse_options(std::env::args_os().skip(1))?)
}

fn run(options: Options) -> Result<(), String> {
    if cfg!(not(target_os = "linux")) {
        return Err("linux-process-memory-v1 is Linux-only".into());
    }
    if options.output_dir.exists() {
        return Err(format!(
            "output directory already exists: {}",
            options.output_dir.display()
        ));
    }
    std::fs::create_dir_all(&options.output_dir)
        .map_err(|error| format!("create memory output directory: {error}"))?;
    if options.reduced_smoke {
        if options.blocks != 1 {
            return Err("memory reduced smoke requires --blocks 1".into());
        }
    } else if options.blocks < 15 {
        return Err("complete memory runs require --blocks at least 15".into());
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
        .ok_or_else(|| "provenance is missing upstream-mimalloc".to_string())?;
    let run = collect_run_identity(&provenance)?;
    let publication_runner = collect_publication_runner(topology)?;
    let runner = RunnerMetadata {
        os: publication_runner.os.clone(),
        architecture: publication_runner.architecture.clone(),
        physical_cores: publication_runner.physical_cores,
        logical_cores: publication_runner.logical_cores,
    };
    let environment = collect_memory_environment(&publication_runner.kernel)?;
    let cells = memory_scenario_cells(topology)?;
    let run_kind = if options.reduced_smoke {
        "reduced-smoke"
    } else {
        "headline"
    };
    let mut raw_output = create_new_writer(options.output_dir.join("raw-memory-samples.jsonl"))?;
    let mut timeline_output = create_new_writer(options.output_dir.join("proc-timelines.jsonl"))?;
    let mut diagnostic_output = create_new_writer(options.output_dir.join("diagnostics.jsonl"))?;
    write_json_line(
        &mut diagnostic_output,
        &json!({
            "event": "memory-run-start",
            "metric_schema_version": crate::memory::MEMORY_SCHEMA_VERSION,
            "run_kind": run_kind,
            "blocks": options.blocks,
            "run_seed": options.run_seed,
            "sampling_target_interval_ns": crate::memory::MEMORY_SAMPLE_TARGET_NS,
            "purge_policy": "natural-only",
            "environment": environment,
        }),
    )?;

    let runner_started = Instant::now();
    let mut calibration_wall = Duration::ZERO;
    let mut block_wall = Duration::ZERO;
    let mut calibrations = Vec::with_capacity(cells.len());
    let mut samples =
        Vec::with_capacity(cells.len() * options.blocks as usize * ALLOCATOR_IDS.len());
    for (card_id, thread_point, _) in cells {
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
            scenario_id: card_id.as_str().into(),
            scenario_version: CORE_SUITE_VERSION.into(),
            thread_point: thread_point.name().into(),
            physical_cores: topology.physical_cores as u32,
            logical_cores: topology.logical_cores as u32,
            transactions_per_worker: options.initial_transactions,
            warmup_transactions_per_worker: options.warmup_transactions,
            reproduction_command: "memory calibration placeholder".into(),
            runner: runner.clone(),
            toolchain: upstream.toolchain.clone(),
        };
        let calibration_started = Instant::now();
        let calibration = calibrate_cell(upstream, &request, options.timeout)?;
        calibration_wall = calibration_wall.saturating_add(calibration_started.elapsed());
        request.transactions_per_worker = calibration.transactions_per_worker;
        let cell = ScenarioCell::new(
            card_id,
            thread_point,
            topology,
            calibration.transactions_per_worker,
            1,
        )
        .map_err(|error| error.to_string())?;
        calibrations.push(CellCalibration {
            scenario_id: card_id.as_str().into(),
            thread_point: thread_point.name().into(),
            thread_count: cell.threads as u32,
            transactions_per_worker: calibration.transactions_per_worker,
            warmup_transactions_per_worker: options.warmup_transactions,
            operation_count: calibration.operation_count,
            elapsed_ns: calibration.elapsed_ns,
        });

        let block_started = Instant::now();
        for order in balanced_block_orders(options.blocks, options.run_seed)? {
            for (ordinal, allocator_id) in order.allocator_ids.iter().enumerate() {
                let child = children
                    .iter()
                    .find(|candidate| candidate.allocator.allocator_id == *allocator_id)
                    .ok_or_else(|| {
                        format!(
                            "validated provenance lost allocator {allocator_id} at block {}",
                            order.block_id
                        )
                    })?;
                let mut measured = request.clone();
                measured.block_id = order.block_id;
                measured.ordinal = ordinal as u8;
                measured.workload_seed = order.workload_seed;
                measured.allocator = child.allocator.clone();
                measured.toolchain = child.toolchain.clone();
                let request_path = options.output_dir.join("requests").join(format!(
                    "{}-{}-block-{:04}-ordinal-{}-{}.json",
                    measured.scenario_id,
                    measured.thread_point,
                    measured.block_id,
                    measured.ordinal,
                    measured.allocator.allocator_id,
                ));
                std::fs::create_dir_all(
                    request_path
                        .parent()
                        .ok_or_else(|| "memory request has no parent directory".to_string())?,
                )
                .map_err(|error| format!("create memory request directory: {error}"))?;
                measured.reproduction_command = format!(
                    "benchmark-memory-run --run-seed {} --blocks {} --output-dir <new-dir> --build-root <build-root> # scenario={} thread-point={}",
                    options.run_seed,
                    options.blocks,
                    measured.scenario_id,
                    measured.thread_point,
                );
                write_new_json(request_path, &measured)?;
                let sample = match run_memory_child_sample(
                    child,
                    &measured,
                    &environment,
                    options.timeout,
                ) {
                    Ok(sample) => sample,
                    Err(error) => {
                        let invalid = json!({
                            "metric_schema_version": crate::memory::MEMORY_SCHEMA_VERSION,
                            "status": "invalid",
                            "scenario_id": measured.scenario_id,
                            "thread_point": measured.thread_point,
                            "block_id": order.block_id,
                            "ordinal": ordinal,
                            "allocator_id": allocator_id,
                            "reason": &error,
                        });
                        write_json_line(
                            &mut diagnostic_output,
                            &json!({
                                "event": "memory-sample-invalid",
                                "status": "invalid",
                                "scenario_id": measured.scenario_id,
                                "thread_point": measured.thread_point,
                                "block_id": order.block_id,
                                "ordinal": ordinal,
                                "allocator_id": allocator_id,
                                "reason": &error,
                            }),
                        )?;
                        write_new_json(options.output_dir.join("memory-invalid.json"), &invalid)?;
                        return Err(format!(
                            "memory cell aborted at block {} ordinal {} allocator {}: {error}",
                            order.block_id, ordinal, allocator_id
                        ));
                    }
                };
                write_json_line(&mut raw_output, &sample)?;
                write_json_line(
                    &mut timeline_output,
                    &json!({
                        "metric_schema_version": sample.metric_schema_version,
                        "scenario_id": sample.scenario_id,
                        "thread_point": sample.thread_point,
                        "block_id": sample.block_id,
                        "allocator_id": sample.allocator_id,
                        "sampled_pid": sample.sampled_pid,
                        "workload_active_ns": sample.workload_active_ns,
                        "workload_drained_ns": sample.workload_drained_ns,
                        "timeline": sample.timeline,
                    }),
                )?;
                samples.push(sample);
            }
        }
        block_wall = block_wall.saturating_add(block_started.elapsed());
        write_json_line(
            &mut diagnostic_output,
            &json!({
                "event": "memory-cell-complete",
                "scenario_id": card_id.as_str(),
                "thread_point": thread_point.name(),
                "operation_unit": card(card_id).operation_unit.name(),
                "samples": options.blocks * ALLOCATOR_IDS.len() as u32,
            }),
        )?;
    }

    let observed_wall = runner_started.elapsed();
    let fixed_wall = observed_wall.saturating_sub(calibration_wall + block_wall);
    let projected_full_seconds = provenance.build_elapsed_seconds
        + calibration_wall.as_secs_f64()
        + block_wall.as_secs_f64() * 15.0 / f64::from(options.blocks)
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
            "projected_headline_blocks": 15,
            "projected_full_suite_seconds": projected_full_seconds,
            "hard_limit_seconds": HARD_LIMIT_SECONDS,
            "fits_limit": projected_full_seconds <= HARD_LIMIT_SECONDS,
        }),
    )?;
    if projected_full_seconds > HARD_LIMIT_SECONDS {
        let reason = format!(
            "projected complete memory runtime {:.1}s exceeds the 3600s hard limit",
            projected_full_seconds
        );
        write_new_json(
            options.output_dir.join("memory-invalid.json"),
            &json!({
                "metric_schema_version": crate::memory::MEMORY_SCHEMA_VERSION,
                "status": "invalid",
                "reason": reason,
            }),
        )?;
        return Err(reason);
    }
    let raw = MemoryRawRun {
        metric_schema_version: crate::memory::MEMORY_SCHEMA_VERSION.into(),
        status: if options.reduced_smoke {
            "incomplete"
        } else {
            "complete"
        }
        .into(),
        run_seed: options.run_seed,
        run,
        runner: publication_runner,
        allocator_lock_sha256: provenance.lockfile_sha256.clone(),
        allocators: publication_allocators(&lock, &provenance)?,
        calibrations,
        samples,
    };
    if !options.reduced_smoke {
        validate_memory_raw_run(&raw)?;
    }
    write_new_json(options.output_dir.join("memory-raw-run.json"), &raw)?;
    println!(
        "PASS memory run: {} samples; projected complete runtime {:.1}s",
        raw.samples.len(),
        projected_full_seconds
    );
    Ok(())
}

fn parse_options(arguments: impl Iterator<Item = OsString>) -> Result<Options, String> {
    let arguments = arguments
        .map(|value| {
            value
                .into_string()
                .map_err(|_| "memory runner arguments must be UTF-8".to_string())
        })
        .collect::<Result<Vec<_>, _>>()?;
    let mut provenance = None;
    let mut build_root = None;
    let mut output_dir = None;
    let mut blocks = 15;
    let mut run_seed = 0x6d65_6d6f_7279_7631;
    let mut timeout_secs = 30;
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
            _ => return Err(format!("unknown memory runner argument {flag}")),
        }
        index += 2;
    }
    if blocks == 0 || run_seed == 0 || timeout_secs <= 5 || initial_transactions == 0 {
        return Err(
            "memory seed/blocks/transactions must be nonzero and timeout must exceed 5s".into(),
        );
    }
    if provenance.is_some() && build_root.is_some() {
        return Err("pass either --provenance or --build-root, not both".into());
    }
    let provenance = provenance
        .or_else(|| build_root.map(|root| root.join("allocator-provenance.json")))
        .ok_or("--provenance or --build-root is required")?;
    let topology = match (physical_cores, logical_cores) {
        (None, None) => None,
        (Some(physical_cores), Some(logical_cores)) => Some(Topology {
            physical_cores,
            logical_cores,
        }),
        _ => return Err("physical and logical core overrides must be supplied together".into()),
    };
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
