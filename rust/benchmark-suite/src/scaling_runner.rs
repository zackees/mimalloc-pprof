//! Production runner for the sparse thread-scaling sweep.

use std::collections::BTreeMap;
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
    run_scaling_child, run_scaling_child_traced, run_scaling_child_with_plan, simulate_cell,
    validate_scaling_raw_run, validate_thread_churn_samples, ScalingCalibration,
    ScalingChildRequest, ScalingChildResponse, ScalingPattern, ScalingRawRun, ScalingRawSample,
    ScalingTopology, SCALING_BLOCKS, SCALING_CHILD_PROTOCOL_VERSION, SCALING_MAX_BLOCK_NS,
    SCALING_MIN_BLOCK_NS, SCALING_PATTERNS, SCALING_SCHEMA_VERSION, SCALING_TARGET_BLOCK_NS,
    SCALING_THREAD_POINTS, THREAD_CHURN_BLOCKS, THREAD_CHURN_POST_DRAIN_OFFSETS_MS,
    THREAD_CHURN_THREADS,
};
use crate::scaling_diagnostic::{
    apply_diagnostic_environment, encode_live_telemetry, parse_diagnostic_environment,
    parse_pattern_selection, parse_thread_point_selection, reproduction_environment,
    validate_cppdef, ScalingDiagnostic, ScalingPhase, DIAGNOSTIC_STATUS, MAX_DIAGNOSTIC_BLOCKS,
};
use crate::scenarios::Topology;

/// Coverage mode exists to keep the daily signal cheap; a sweep that cannot
/// finish inside this budget is a failure, not something to publish slowly.
// Leave five minutes beneath the workflow timeout for validation, artifact
// sealing, and upload. The two distribution workloads include separate
// diagnostic replays in addition to their 40 timed repetitions.
const HARD_LIMIT_SECONDS: f64 = 25.0 * 60.0;
/// #508: `thread-churn` keeps every child alive and idle for the last
/// post-drain offset (3 s) after its work -- 40 paired blocks x 5 allocators =
/// 600 s of fixed, mandated sleep that no calibration can shorten. The shard
/// that runs it (the one measuring `THREAD_CHURN_THREADS`) is allowed exactly
/// that idle time on top of `HARD_LIMIT_SECONDS`; its measured work still has
/// to fit the unchanged 25 minutes. Evidence (run 36202884622): that shard
/// projected 1011 s, the six shards together measured 57 min of the measure
/// job's 120-minute timeout, so ~11 more minutes of idle still fit it.
fn thread_churn_idle_allowance_seconds() -> f64 {
    let last_offset_ms =
        THREAD_CHURN_POST_DRAIN_OFFSETS_MS[THREAD_CHURN_POST_DRAIN_OFFSETS_MS.len() - 1];
    f64::from(THREAD_CHURN_BLOCKS) * ALLOCATOR_IDS.len() as f64 * last_offset_ms as f64 / 1000.0
}
const CALIBRATION_ATTEMPTS: u32 = 12;
const DISTRIBUTION_MIN_BLOCK_NS: u64 = 25_000_000;
const DISTRIBUTION_TARGET_BLOCK_NS: u64 = 50_000_000;
const DISTRIBUTION_MAX_BLOCK_NS: u64 = 150_000_000;

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
    shard_index: usize,
    shard_count: usize,
    /// #528: `Some` for a diagnostic run (`--diagnostic`), which is never
    /// published.
    diagnostic: Option<DiagnosticOptions>,
}

/// #528: what `--diagnostic` selects and changes.
#[derive(Debug, PartialEq)]
struct DiagnosticOptions {
    /// Applied to the mimalloc-pprof child only.
    environment: BTreeMap<String, String>,
    patterns: Vec<ScalingPattern>,
    thread_points: Vec<u32>,
    /// Whether `--patterns` narrowed the sweep; thread-churn replays the
    /// large-class-ephemeral calibration, so it runs only without it.
    patterns_filtered: bool,
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
    let pattern = ScalingPattern::parse(&template.pattern).ok_or("unknown scaling pattern")?;
    let (minimum, target, maximum) = if pattern.is_distribution() {
        (
            DISTRIBUTION_MIN_BLOCK_NS,
            DISTRIBUTION_TARGET_BLOCK_NS,
            DISTRIBUTION_MAX_BLOCK_NS,
        )
    } else {
        (
            SCALING_MIN_BLOCK_NS,
            SCALING_TARGET_BLOCK_NS,
            SCALING_MAX_BLOCK_NS,
        )
    };
    let mut operations = template.operations_per_worker.max(1);
    for _ in 0..CALIBRATION_ATTEMPTS {
        let mut probe = template.clone();
        probe.operations_per_worker = operations;
        probe.reproduction_command = format!(
            "scaling calibration probe pattern={} threads={}",
            template.pattern, template.thread_count
        );
        let (response, _peak_rss, _live_at_peak) = run_scaling_child(child, &probe, timeout)?;
        if (minimum..=maximum).contains(&response.elapsed_ns) {
            return Ok((operations, response));
        }
        let scaled = (operations as u128 * u128::from(target)
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
    if options.diagnostic.is_some() {
        // parse_options bounded --blocks to 1..=MAX_DIAGNOSTIC_BLOCKS.
    } else if options.reduced_smoke {
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

    // #528: a library built with extra defines may only feed a diagnostic run.
    provenance
        .diagnostic_cppdefs
        .iter()
        .try_for_each(|item| validate_cppdef(item))?;
    if !provenance.diagnostic_cppdefs.is_empty() && options.diagnostic.is_none() {
        return Err(format!(
            "allocator provenance says mimalloc-pprof was built with -DMI_EXTRA_CPPDEFS={}; \
             only a --diagnostic run may measure it",
            provenance.diagnostic_cppdefs.join(";")
        ));
    }

    let topology = options.topology.map_or_else(detect_topology, Ok)?;
    let mut children = children_from_provenance(&provenance)?;
    if let Some(diagnostic) = &options.diagnostic {
        apply_diagnostic_environment(&mut children, &diagnostic.environment)?;
    }
    let children = children;
    let diagnostic_record = options.diagnostic.as_ref().map(|diagnostic| {
        ScalingDiagnostic::new(
            diagnostic.environment.clone(),
            provenance.diagnostic_cppdefs.clone(),
            &diagnostic.patterns,
            &diagnostic.thread_points,
            options.blocks,
            !diagnostic.patterns_filtered
                && diagnostic.thread_points.contains(&THREAD_CHURN_THREADS),
        )
    });
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
            "diagnostic": diagnostic_record,
        }),
    )?;
    if let Some(record) = &diagnostic_record {
        println!("{}", record.label);
    }

    let runner_started = Instant::now();
    let mut calibration_wall = Duration::ZERO;
    let mut block_wall = Duration::ZERO;
    let mut projected_block_wall = Duration::ZERO;
    let mut calibrations = Vec::new();
    let mut samples = Vec::new();
    let request_dir = options.output_dir.join("requests");
    std::fs::create_dir_all(&request_dir)
        .map_err(|error| format!("create scaling request dir: {error}"))?;

    let mut shard_thread_points =
        crate::scaling::scaling_thread_points_for_shard(options.shard_index, options.shard_count)?;
    let mut patterns = SCALING_PATTERNS.to_vec();
    if let Some(diagnostic) = &options.diagnostic {
        // A shard whose points are all filtered out records an empty run.
        shard_thread_points.retain(|point| diagnostic.thread_points.contains(point));
        patterns = diagnostic.patterns.clone();
    }
    let is_diagnostic = options.diagnostic.is_some();
    for pattern in patterns.iter().copied() {
        for thread_count in shard_thread_points.iter().copied() {
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
                live_telemetry_path: None,
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
            let pattern_blocks = if is_diagnostic {
                options.blocks
            } else if options.reduced_smoke {
                1
            } else {
                pattern.full_blocks()
            };
            // What a complete run of this cell would cost: a diagnostic run
            // is complete at its own block count.
            let projected_blocks = if is_diagnostic {
                pattern_blocks
            } else {
                pattern.full_blocks()
            };
            for order in balanced_block_orders(pattern_blocks, options.run_seed)? {
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
                        "{} '{}' --scaling < '{}'",
                        reproduction_environment(child),
                        child.program.display(),
                        request_path.display()
                    );
                    write_new_json(request_path.clone(), &request)?;
                    let (response, peak_rss_bytes, _measured_live_at_peak) =
                        run_scaling_child_with_plan(child, &request, options.timeout, &plan)?;
                    let (
                        diagnostic_peak_rss_bytes,
                        live_requested_bytes_at_diagnostic_peak_rss,
                        diagnostic_peak_live_requested_bytes,
                        diagnostic_rss_phases,
                    ) = if pattern.replays_live_telemetry(is_diagnostic) {
                        let telemetry_path = request_dir.join(format!(
                            ".live-{}-{}-{:04}-{}",
                            pattern.as_str(),
                            thread_count,
                            order.block_id,
                            allocator_id
                        ));
                        write_new_bytes(
                            telemetry_path.clone(),
                            &encode_live_telemetry(0, ScalingPhase::Setup),
                        )?;
                        let mut diagnostic = request.clone();
                        diagnostic.live_telemetry_path = Some(
                            telemetry_path
                                .to_str()
                                .ok_or("telemetry path is not UTF-8")?
                                .to_string(),
                        );
                        diagnostic.reproduction_command =
                            format!("diagnostic replay of {}", request.reproduction_command);
                        let result =
                            run_scaling_child_traced(child, &diagnostic, options.timeout, &plan);
                        std::fs::remove_file(&telemetry_path).map_err(|error| {
                            format!("remove {}: {error}", telemetry_path.display())
                        })?;
                        let replay = result?;
                        (
                            replay.peak_rss_bytes,
                            replay.live_requested_bytes_at_peak_rss,
                            replay.response.peak_live_requested_bytes,
                            // Published rows keep their shape: only a
                            // diagnostic run records the phases (#528).
                            if is_diagnostic {
                                replay.rss_phases
                            } else {
                                Vec::new()
                            },
                        )
                    } else {
                        (0, 0, 0, Vec::new())
                    };
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
                        diagnostic_peak_rss_bytes,
                        live_requested_bytes_at_diagnostic_peak_rss,
                        diagnostic_peak_live_requested_bytes,
                        diagnostic_rss_phases,
                        reproduction_command: request.reproduction_command.clone(),
                        response,
                    };
                    write_json_line(&mut raw_jsonl, &sample)?;
                    samples.push(sample);
                }
            }
            let cell_wall = started.elapsed();
            block_wall = block_wall.saturating_add(cell_wall);
            projected_block_wall = projected_block_wall.saturating_add(
                cell_wall.mul_f64(f64::from(projected_blocks) / f64::from(pattern_blocks)),
            );
            write_json_line(
                &mut diagnostics,
                &json!({
                    "event": "scaling-cell-complete", "cell": cell_key,
                    "operations_per_worker": operations_per_worker,
                    "calibrated_elapsed_ns": probe.elapsed_ns,
                    "samples": pattern_blocks * ALLOCATOR_IDS.len() as u32,
                }),
            )?;
        }
    }

    // #508: thread-churn, on the shard that measures its worker count. It
    // replays large-class-ephemeral's stream at the operation count that cell
    // just froze, so the two share one plan -- same counts, same checksum --
    // and thread-churn needs no calibration of its own.
    let mut thread_churn_samples = Vec::new();
    let runs_thread_churn = shard_thread_points.contains(&THREAD_CHURN_THREADS)
        && options
            .diagnostic
            .as_ref()
            .is_none_or(|diagnostic| !diagnostic.patterns_filtered);
    if runs_thread_churn {
        let pattern = ScalingPattern::ThreadChurn;
        let source = calibrations
            .iter()
            .find(|value: &&ScalingCalibration| {
                value.pattern == ScalingPattern::LargeClassEphemeral.as_str()
                    && value.thread_count == THREAD_CHURN_THREADS
            })
            .ok_or("thread-churn found no large-class-ephemeral calibration to replay")?;
        let operations_per_worker = source.operations_per_worker;
        let blocks = if is_diagnostic {
            options.blocks
        } else if options.reduced_smoke {
            1
        } else {
            THREAD_CHURN_BLOCKS
        };
        let projected_blocks = if is_diagnostic {
            blocks
        } else {
            THREAD_CHURN_BLOCKS
        };
        let started = Instant::now();
        for order in balanced_block_orders(blocks, options.run_seed)? {
            let plan = simulate_cell(
                pattern,
                options.run_seed,
                THREAD_CHURN_THREADS,
                order.block_id,
                operations_per_worker,
            );
            for (ordinal, allocator_id) in order.allocator_ids.iter().enumerate() {
                let child = children
                    .iter()
                    .find(|value| value.allocator.allocator_id == *allocator_id)
                    .ok_or_else(|| format!("missing scaling allocator {allocator_id}"))?;
                let request_path = request_dir.join(format!(
                    "{}-{}-block-{:04}-ordinal-{}-{}.json",
                    pattern.as_str(),
                    THREAD_CHURN_THREADS,
                    order.block_id,
                    ordinal,
                    allocator_id
                ));
                let request = ScalingChildRequest {
                    protocol_version: SCALING_CHILD_PROTOCOL_VERSION.into(),
                    metric_schema_version: SCALING_SCHEMA_VERSION.into(),
                    run_seed: options.run_seed,
                    pattern: pattern.as_str().into(),
                    thread_count: THREAD_CHURN_THREADS,
                    block_id: order.block_id,
                    ordinal: ordinal as u8,
                    operations_per_worker,
                    warmup_operations_per_worker: options.warmup_operations,
                    allocator: child.allocator.clone(),
                    runner: runner.clone(),
                    toolchain: child.toolchain.clone(),
                    reproduction_command: format!(
                        "{} '{}' --scaling < '{}'",
                        reproduction_environment(child),
                        child.program.display(),
                        request_path.display()
                    ),
                    live_telemetry_path: None,
                };
                write_new_json(request_path, &request)?;
                let (response, peak_rss_bytes, _live_at_peak) =
                    run_scaling_child_with_plan(child, &request, options.timeout, &plan)?;
                let sample = ScalingRawSample {
                    metric_schema_version: SCALING_SCHEMA_VERSION.into(),
                    block_id: order.block_id,
                    ordinal: ordinal as u8,
                    pattern: pattern.as_str().into(),
                    thread_count: THREAD_CHURN_THREADS,
                    allocator_id: allocator_id.clone(),
                    allocator_source_sha: child.allocator.source_sha.clone(),
                    child_binary_sha256: child.allocator.child_binary_sha256.clone(),
                    operations_per_worker,
                    peak_rss_bytes,
                    diagnostic_peak_rss_bytes: 0,
                    live_requested_bytes_at_diagnostic_peak_rss: 0,
                    diagnostic_peak_live_requested_bytes: 0,
                    diagnostic_rss_phases: Vec::new(),
                    reproduction_command: request.reproduction_command.clone(),
                    response,
                };
                write_json_line(&mut raw_jsonl, &sample)?;
                thread_churn_samples.push(sample);
            }
        }
        let churn_wall = started.elapsed();
        block_wall = block_wall.saturating_add(churn_wall);
        projected_block_wall = projected_block_wall
            .saturating_add(churn_wall.mul_f64(f64::from(projected_blocks) / f64::from(blocks)));
        validate_thread_churn_samples(
            options.run_seed,
            &thread_churn_samples,
            operations_per_worker,
            blocks,
        )?;
        write_json_line(
            &mut diagnostics,
            &json!({
                "event": "thread-churn-complete",
                "threads": THREAD_CHURN_THREADS,
                "operations_per_worker": operations_per_worker,
                "samples": thread_churn_samples.len(),
                "wall_seconds": churn_wall.as_secs_f64(),
            }),
        )?;
    }

    let observed_wall = runner_started.elapsed();
    let fixed_wall = observed_wall.saturating_sub(calibration_wall + block_wall);
    let projected_full_seconds = provenance.build_elapsed_seconds
        + calibration_wall.as_secs_f64()
        + projected_block_wall.as_secs_f64()
        + fixed_wall.as_secs_f64()
        + 1.0;
    let hard_limit_seconds = HARD_LIMIT_SECONDS
        + if runs_thread_churn {
            thread_churn_idle_allowance_seconds()
        } else {
            0.0
        };
    write_new_json(
        options.output_dir.join("runtime-projection.json"),
        &json!({
            "observed_blocks": options.blocks,
            "observed_runner_wall_seconds": observed_wall.as_secs_f64(),
            "observed_calibration_wall_seconds": calibration_wall.as_secs_f64(),
            "observed_block_wall_seconds": block_wall.as_secs_f64(),
            "native_build_elapsed_seconds": provenance.build_elapsed_seconds,
            "projected_repetitions_by_pattern": patterns.iter().map(|pattern| (pattern.as_str(), if is_diagnostic { options.blocks } else { pattern.full_blocks() })).collect::<Vec<_>>(),
            "projected_full_suite_seconds": projected_full_seconds,
            "hard_limit_seconds": hard_limit_seconds,
            "thread_churn_idle_allowance_seconds": hard_limit_seconds - HARD_LIMIT_SECONDS,
            "fits_limit": projected_full_seconds <= hard_limit_seconds,
        }),
    )?;
    if projected_full_seconds > hard_limit_seconds {
        let reason = format!(
            "projected complete scaling runtime {projected_full_seconds:.1}s exceeds the {hard_limit_seconds:.0}s budget"
        );
        write_new_json(
            options.output_dir.join("scaling-invalid.json"),
            &json!({ "metric_schema_version": SCALING_SCHEMA_VERSION, "status": "invalid", "reason": reason }),
        )?;
        return Err(reason);
    }
    let raw = ScalingRawRun {
        metric_schema_version: SCALING_SCHEMA_VERSION.into(),
        status: if is_diagnostic {
            DIAGNOSTIC_STATUS
        } else if options.reduced_smoke || options.shard_count > 1 {
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
        thread_churn_samples,
        diagnostic: diagnostic_record,
    };
    if !is_diagnostic && !options.reduced_smoke && options.shard_count == 1 {
        validate_scaling_raw_run(&raw)?;
    }
    write_new_json(options.output_dir.join("scaling-raw-run.json"), &raw)?;
    println!(
        "PASS scaling sweep: {} raw records across {} cells; projected runtime {:.1}s",
        raw.samples.len(),
        patterns.len() * shard_thread_points.len(),
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
    let mut shard_index = 0usize;
    let mut shard_count = 1usize;
    let mut diagnostic = false;
    let mut diagnostic_environment = None;
    let mut pattern_selection = None;
    let mut thread_point_selection = None;
    let mut index = 0;
    while index < arguments.len() {
        let flag = arguments[index].as_str();
        if flag == "--reduced-smoke" {
            reduced_smoke = true;
            index += 1;
            continue;
        }
        if flag == "--diagnostic" {
            diagnostic = true;
            index += 1;
            continue;
        }
        if flag == "--help" || flag == "-h" {
            println!("usage: benchmark-scaling-run (--provenance <allocator-provenance.json> | --build-root <dir>) --output-dir <new-dir> [--blocks 3] [--run-seed N] [--timeout-secs N] [--warmup-operations N] [--initial-operations N] [--physical-cores N] [--logical-cores N] [--shard-index N] [--shard-count N] [--reduced-smoke | --diagnostic [--diagnostic-env 'KEY=VALUE ...'] [--patterns a,b] [--thread-points 1,4]]");
            println!("--diagnostic (#528): never publishable; --blocks (1..={MAX_DIAGNOSTIC_BLOCKS}) paired blocks for every selected cell; --diagnostic-env applies to the mimalloc-pprof child only; thread-churn runs only without --patterns.");
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
            "--shard-index" => shard_index = parse_number("--shard-index", value)?,
            "--shard-count" => shard_count = parse_number("--shard-count", value)?,
            "--diagnostic-env" => diagnostic_environment = Some(value.clone()),
            "--patterns" => pattern_selection = Some(value.clone()),
            "--thread-points" => thread_point_selection = Some(value.clone()),
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
    crate::scaling::scaling_thread_points_for_shard(shard_index, shard_count)?;
    let diagnostic = if diagnostic {
        if reduced_smoke {
            return Err("--diagnostic and --reduced-smoke are exclusive".into());
        }
        if !(1..=MAX_DIAGNOSTIC_BLOCKS).contains(&blocks) {
            return Err(format!(
                "--diagnostic runs 1..={MAX_DIAGNOSTIC_BLOCKS} --blocks per cell"
            ));
        }
        let patterns = parse_pattern_selection(&pattern_selection.unwrap_or_default())?;
        Some(DiagnosticOptions {
            environment: parse_diagnostic_environment(&diagnostic_environment.unwrap_or_default())?,
            patterns_filtered: patterns.len() != SCALING_PATTERNS.len(),
            patterns,
            thread_points: parse_thread_point_selection(
                &thread_point_selection.unwrap_or_default(),
            )?,
        })
    } else {
        if diagnostic_environment.is_some()
            || pattern_selection.is_some()
            || thread_point_selection.is_some()
        {
            return Err(
                "--diagnostic-env, --patterns and --thread-points require --diagnostic".into(),
            );
        }
        None
    };
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
        shard_index,
        shard_count,
        diagnostic,
    })
}

fn parse_number<T: std::str::FromStr>(flag: &str, value: &str) -> Result<T, String> {
    value
        .parse()
        .map_err(|_| format!("{flag} expects a non-negative integer, got {value:?}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(extra: &[&str]) -> Result<Options, String> {
        let mut arguments = vec!["--build-root", "target/release", "--output-dir", "out"];
        arguments.extend_from_slice(extra);
        parse_options(arguments.into_iter().map(OsString::from))
    }

    #[test]
    fn default_runs_carry_no_diagnostic() {
        let options = parse(&[]).unwrap();
        assert_eq!(options.diagnostic, None);
        assert_eq!(options.blocks, SCALING_BLOCKS);
    }

    #[test]
    fn diagnostic_selection_flags_require_diagnostic() {
        for flags in [
            &["--diagnostic-env", "MIMALLOC_PURGE_DELAY=10"][..],
            &["--patterns", "sparse-large-buffers"][..],
            &["--thread-points", "1,4"][..],
        ] {
            let error = parse(flags).unwrap_err();
            assert!(error.contains("require --diagnostic"), "{error}");
        }
    }

    #[test]
    fn diagnostic_options_parse_strictly() {
        let options = parse(&[
            "--diagnostic",
            "--blocks",
            "1",
            "--diagnostic-env",
            "MIMALLOC_PURGE_DELAY=10",
            "--patterns",
            "sparse-large-buffers",
            "--thread-points",
            "1,4",
        ])
        .unwrap();
        let diagnostic = options.diagnostic.unwrap();
        assert_eq!(
            diagnostic.environment,
            BTreeMap::from([("MIMALLOC_PURGE_DELAY".to_string(), "10".to_string())])
        );
        assert_eq!(diagnostic.patterns, vec![ScalingPattern::LargeBuffers]);
        assert_eq!(diagnostic.thread_points, vec![1, 4]);
        assert!(diagnostic.patterns_filtered);

        // Empty selections (what the workflow passes by default) mean "all".
        let options = parse(&[
            "--diagnostic",
            "--diagnostic-env",
            "",
            "--patterns",
            "",
            "--thread-points",
            "",
        ])
        .unwrap();
        let diagnostic = options.diagnostic.unwrap();
        assert!(diagnostic.environment.is_empty());
        assert_eq!(diagnostic.patterns, SCALING_PATTERNS.to_vec());
        assert_eq!(diagnostic.thread_points, SCALING_THREAD_POINTS.to_vec());
        assert!(!diagnostic.patterns_filtered);

        for bad in [
            &["--diagnostic", "--diagnostic-env", "MIMALLOC_PROF=1"][..],
            &["--diagnostic", "--diagnostic-env", "X=$(id)"][..],
            &["--diagnostic", "--patterns", "thread-churn"][..],
            &["--diagnostic", "--thread-points", "16"][..],
            &["--diagnostic", "--blocks", "0"][..],
            &["--diagnostic", "--blocks", "41"][..],
            &["--diagnostic", "--reduced-smoke", "--blocks", "1"][..],
        ] {
            assert!(parse(bad).is_err(), "accepted {bad:?}");
        }
    }
}
