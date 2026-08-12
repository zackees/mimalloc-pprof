//! Production runner for the bounded transaction-latency suite.

use std::collections::BTreeMap;
use std::ffi::OsString;
use std::path::PathBuf;
use std::time::{Duration, Instant};

use serde_json::json;

use crate::config::AllocatorLock;
use crate::latency::{
    choose_sample_denominator, latency_scenario_cells, minimum_transactions_per_worker,
    run_latency_child, validate_latency_raw_run, LatencyChildRequest, LatencyRawRun,
    LatencyRawSample, LATENCY_CHILD_PROTOCOL_VERSION, LATENCY_MIN_SAMPLES, LATENCY_SCHEMA_VERSION,
};
use crate::model::{
    BenchmarkChildRequest, CellCalibration, RunnerMetadata, CHILD_PROTOCOL_VERSION,
};
use crate::orchestration::{balanced_block_orders, calibrate_cell, run_child_sample};
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

pub fn benchmark_latency_run_main() -> Result<(), String> {
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
    let invalid = output_dir.join("latency-invalid.json");
    if !invalid.exists() {
        write_new_json(
            invalid,
            &json!({
                "metric_schema_version": LATENCY_SCHEMA_VERSION,
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
            "event": "latency-run-invalid",
            "metric_schema_version": LATENCY_SCHEMA_VERSION,
            "reason": reason,
        }),
    )
}

fn run(options: Options) -> Result<(), String> {
    if cfg!(not(target_os = "linux")) {
        return Err("transaction-latency-v1 production collection is Linux-only".into());
    }
    if options.output_dir.exists() {
        return Err(format!(
            "output directory already exists: {}",
            options.output_dir.display()
        ));
    }
    std::fs::create_dir_all(&options.output_dir)
        .map_err(|error| format!("create latency output: {error}"))?;
    if options.reduced_smoke {
        if options.blocks != 1 {
            return Err("latency reduced smoke requires --blocks 1".into());
        }
    } else if options.blocks < 15 {
        return Err("complete latency runs require --blocks at least 15".into());
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
    let runner = RunnerMetadata {
        os: publication_runner.os.clone(),
        architecture: publication_runner.architecture.clone(),
        physical_cores: publication_runner.physical_cores,
        logical_cores: publication_runner.logical_cores,
    };
    let run_kind = if options.reduced_smoke {
        "reduced-smoke"
    } else {
        "headline"
    };
    let cells = latency_scenario_cells(topology)?;
    let mut raw_jsonl = create_new_writer(options.output_dir.join("raw-latency-samples.jsonl"))?;
    let mut diagnostics = create_new_writer(options.output_dir.join("diagnostics.jsonl"))?;
    write_json_line(
        &mut diagnostics,
        &json!({
            "event": "latency-run-start", "metric_schema_version": LATENCY_SCHEMA_VERSION,
            "run_kind": run_kind, "blocks": options.blocks, "run_seed": options.run_seed,
            "minimum_samples_per_allocator_cell": LATENCY_MIN_SAMPLES,
        }),
    )?;

    let runner_started = Instant::now();
    let mut calibration_wall = Duration::ZERO;
    let mut block_wall = Duration::ZERO;
    let mut calibrations = Vec::with_capacity(cells.len());
    let mut denominators = BTreeMap::new();
    let mut samples = Vec::with_capacity(cells.len() * options.blocks as usize * 4);
    for (card_id, thread_point, definition) in cells {
        let mut template = BenchmarkChildRequest {
            protocol_version: CHILD_PROTOCOL_VERSION.into(),
            schema_version: RAW_SCHEMA_VERSION.into(),
            suite_version: CORE_SUITE_VERSION.into(),
            run_kind: run_kind.into(),
            execution_mode: "normal".into(),
            run_seed: options.run_seed,
            block_id: 0,
            ordinal: 0,
            workload_seed: 1,
            allocator: upstream.allocator.clone(),
            scenario_id: card_id.as_str().into(),
            scenario_version: CORE_SUITE_VERSION.into(),
            thread_point: thread_point.name().into(),
            physical_cores: topology.physical_cores as u32,
            logical_cores: topology.logical_cores as u32,
            transactions_per_worker: options.initial_transactions,
            warmup_transactions_per_worker: options.warmup_transactions,
            reproduction_command: "latency calibration placeholder".into(),
            runner: runner.clone(),
            toolchain: upstream.toolchain.clone(),
        };
        let started = Instant::now();
        let calibration = calibrate_cell(upstream, &template, options.timeout)?;
        let denominator = choose_sample_denominator(
            calibration.transactions_per_worker,
            topology
                .resolve(thread_point)
                .map_err(|error| error.to_string())?,
            options.blocks,
        )?;
        let minimum_transactions = minimum_transactions_per_worker(
            topology
                .resolve(thread_point)
                .map_err(|error| error.to_string())?,
            options.blocks,
            denominator,
            if options.reduced_smoke {
                1
            } else {
                LATENCY_MIN_SAMPLES
            },
        )?;
        // Freeze the smallest workload that guarantees the raw-sample floor.
        // The upstream calibration only selects a denominator that keeps this
        // bounded; retaining its potentially much larger transaction count
        // would make fast cells produce unbounded public JSON.
        template.transactions_per_worker = minimum_transactions;
        let mut request = template.clone();
        request.block_id = u32::MAX;
        request.workload_seed = 0x6c61_7465_6e63_79;
        request.reproduction_command = "latency realized-count calibration".into();
        let sample = run_child_sample(upstream, &request, options.timeout)?;
        let realized = crate::orchestration::CalibrationResult {
            transactions_per_worker: template.transactions_per_worker,
            operation_count: sample.operation_count,
            elapsed_ns: sample.elapsed_ns,
        };
        calibration_wall = calibration_wall.saturating_add(started.elapsed());
        let cell = ScenarioCell::new(
            card_id,
            thread_point,
            topology,
            realized.transactions_per_worker,
            1,
        )
        .map_err(|error| error.to_string())?;
        calibrations.push(CellCalibration {
            scenario_id: card_id.as_str().into(),
            thread_point: thread_point.name().into(),
            thread_count: cell.threads as u32,
            transactions_per_worker: realized.transactions_per_worker,
            warmup_transactions_per_worker: options.warmup_transactions,
            operation_count: card(card_id)
                .operation_count(&cell.expected_counts().map_err(|error| error.to_string())?),
            elapsed_ns: realized.elapsed_ns,
        });
        let cell_key = format!("{}/{}", card_id.as_str(), thread_point.name());
        denominators.insert(cell_key.clone(), denominator);

        let started = Instant::now();
        for order in balanced_block_orders(options.blocks, options.run_seed)? {
            for (ordinal, allocator_id) in order.allocator_ids.iter().enumerate() {
                let child = children
                    .iter()
                    .find(|value| value.allocator.allocator_id == *allocator_id)
                    .ok_or_else(|| format!("missing latency allocator {allocator_id}"))?;
                let mut benchmark = template.clone();
                benchmark.block_id = order.block_id;
                benchmark.ordinal = ordinal as u8;
                benchmark.workload_seed = order.workload_seed;
                benchmark.allocator = child.allocator.clone();
                benchmark.toolchain = child.toolchain.clone();
                benchmark.reproduction_command = format!("benchmark-latency-run --run-seed {} --blocks {} --output-dir <new-dir> --build-root <build-root> # cell={cell_key}", options.run_seed, options.blocks);
                let request_dir = options.output_dir.join("requests");
                std::fs::create_dir_all(&request_dir)
                    .map_err(|error| format!("create latency request dir: {error}"))?;
                let request_path = request_dir.join(format!(
                    "{}-{}-block-{:04}-ordinal-{}-{}.json",
                    card_id.as_str(),
                    thread_point.name(),
                    order.block_id,
                    ordinal,
                    allocator_id
                ));
                let measured_request = LatencyChildRequest {
                    protocol_version: LATENCY_CHILD_PROTOCOL_VERSION.into(),
                    metric_schema_version: LATENCY_SCHEMA_VERSION.into(),
                    sample_denominator: denominator,
                    control: false,
                    runner_class: publication_runner.runner_class.clone(),
                    affinity_policy: publication_runner.affinity.policy.clone(),
                    benchmark: benchmark.clone(),
                };
                write_new_json(request_path, &measured_request)?;
                let measured = run_latency_child(child, &measured_request, options.timeout)?;
                let mut control_request = measured_request.clone();
                control_request.control = true;
                let control = run_latency_child(child, &control_request, options.timeout)?;
                let sample = LatencyRawSample {
                    metric_schema_version: LATENCY_SCHEMA_VERSION.into(),
                    block_id: order.block_id,
                    ordinal: ordinal as u8,
                    workload_seed: order.workload_seed,
                    allocator_id: allocator_id.clone(),
                    allocator_source_sha: child.allocator.source_sha.clone(),
                    child_binary_sha256: child.allocator.child_binary_sha256.clone(),
                    scenario_id: card_id.as_str().into(),
                    thread_point: thread_point.name().into(),
                    thread_count: cell.threads as u32,
                    sample_denominator: denominator,
                    transaction_definition: definition.into(),
                    measured,
                    control,
                };
                write_json_line(&mut raw_jsonl, &sample)?;
                samples.push(sample);
            }
        }
        block_wall = block_wall.saturating_add(started.elapsed());
        write_json_line(
            &mut diagnostics,
            &json!({
                "event": "latency-cell-complete", "cell": cell_key, "sample_denominator": denominator,
                "transactions_per_worker": realized.transactions_per_worker, "raw_pairs": options.blocks * 4,
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
            "observed_blocks": options.blocks, "observed_runner_wall_seconds": observed_wall.as_secs_f64(),
            "observed_calibration_wall_seconds": calibration_wall.as_secs_f64(), "observed_block_wall_seconds": block_wall.as_secs_f64(),
            "native_build_elapsed_seconds": provenance.build_elapsed_seconds, "projected_headline_blocks": 15,
            "projected_full_suite_seconds": projected_full_seconds, "hard_limit_seconds": HARD_LIMIT_SECONDS,
            "fits_limit": projected_full_seconds <= HARD_LIMIT_SECONDS,
        }),
    )?;
    if projected_full_seconds > HARD_LIMIT_SECONDS {
        let reason = format!("projected complete latency runtime {projected_full_seconds:.1}s exceeds the 3600s hard limit");
        write_new_json(
            options.output_dir.join("latency-invalid.json"),
            &json!({ "metric_schema_version": LATENCY_SCHEMA_VERSION, "status": "invalid", "reason": reason }),
        )?;
        return Err(reason);
    }
    let raw = LatencyRawRun {
        metric_schema_version: LATENCY_SCHEMA_VERSION.into(),
        status: if options.reduced_smoke {
            "incomplete"
        } else {
            "complete"
        }
        .into(),
        run_seed: options.run_seed,
        run: run_identity,
        runner: publication_runner,
        allocator_lock_sha256: provenance.lockfile_sha256.clone(),
        allocators: publication_allocators(&lock, &provenance)?,
        calibrations,
        sampling_denominators: denominators,
        samples,
    };
    if !options.reduced_smoke {
        validate_latency_raw_run(&raw)?;
    }
    write_new_json(options.output_dir.join("latency-raw-run.json"), &raw)?;
    println!(
        "PASS latency run: {} paired raw records; projected complete runtime {:.1}s",
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
                .map_err(|_| "latency runner arguments must be UTF-8".to_string())
        })
        .collect::<Result<Vec<_>, _>>()?;
    let mut provenance = None;
    let mut build_root = None;
    let mut output_dir = None;
    let mut blocks = 15;
    let mut run_seed = 0x6c61_7465_6e63_79;
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
            _ => return Err(format!("unknown latency runner argument {flag}")),
        }
        index += 2;
    }
    if blocks == 0 || run_seed == 0 || timeout_secs < 2 || initial_transactions == 0 {
        return Err("latency seed/blocks/transactions/timeout must be nonzero".into());
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
