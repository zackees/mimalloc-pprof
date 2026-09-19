//! Production runner for the pprof compilation/runtime tax matrix (#187).
//!
//! This is the measurement PRODUCER: it drives the paired seven-configuration
//! matrix (see `crate::pprof_tax`), one freshly spawned child per measurement
//! via the `crate::pprof_tax_child` protocol, and writes the raw evidence a
//! later publication phase can summarize.

use std::collections::BTreeMap;
use std::ffi::OsString;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde_json::json;

use crate::model::{
    AllocatorIdentity, BenchmarkChildRequest, RunIdentity, RunnerMetadata, ToolchainMetadata,
    CHILD_PROTOCOL_VERSION,
};
use crate::pprof_tax::{
    self, PprofTaxCellSpec, PprofTaxCompiledConfiguration, PprofTaxConfiguration, PprofTaxManifest,
    PprofTaxRawCell, PprofTaxRawRun, PprofTaxRawSample,
};
use crate::pprof_tax_child::{PprofTaxChildRequest, PprofTaxChildResponse};
use crate::provenance::{sha256_bytes, sha256_file};
use crate::runner::{
    collect_publication_runner, create_new_writer, detect_topology, write_json_line,
    write_new_bytes, write_new_json,
};
use crate::scaling::{sample_peak_rss, splitmix64};
use crate::{CORE_SUITE_VERSION, RAW_SCHEMA_VERSION};

/// Whole measuring phase hard limit. A sweep that cannot finish inside this
/// budget is a failure, not something to publish slowly.
const HARD_LIMIT_SECONDS: f64 = 35.0 * 60.0;
const CALIBRATION_ATTEMPTS: u32 = 12;
const CALIBRATION_INITIAL_OPERATIONS: u64 = 4096;
/// The single-binary reference configuration calibration runs against.
const CALIBRATION_CONFIGURATION_ID: &str = "fork-pprof-off";
/// The one configuration whose failures are recorded rather than fatal, and
/// whose accumulated wall time is bounded independently of the hard limit.
const STRESS_CONFIGURATION_ID: &str = "fork-pprof-rate-1-stress";
/// Prefix of the error `run_pprof_tax_child` returns when its watchdog fired,
/// so a rate-1 failure is classified `timeout` rather than `child-failed`.
const CHILD_TIMEOUT_PREFIX: &str = "pprof-tax child timed out";

#[derive(Debug)]
struct Options {
    manifest: PathBuf,
    output_dir: PathBuf,
    blocks: u32,
    run_seed: u64,
    reduced_smoke: bool,
    timeout: Duration,
    stress_budget: Duration,
}

pub fn benchmark_pprof_tax_run_main() -> Result<(), String> {
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

fn record_invalid_run(output_dir: &Path, reason: &str) -> Result<(), String> {
    let invalid = output_dir.join("pprof-tax-invalid.json");
    if !invalid.exists() {
        write_new_json(
            invalid,
            &json!({
                "metric_schema_version": pprof_tax::PPROF_TAX_SCHEMA_VERSION,
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
            "event": "pprof-tax-run-invalid",
            "metric_schema_version": pprof_tax::PPROF_TAX_SCHEMA_VERSION,
            "reason": reason,
        }),
    )
}

fn run(options: Options) -> Result<(), String> {
    if cfg!(not(target_os = "linux")) {
        return Err("pprof-tax production collection is Linux-only".into());
    }
    if options.output_dir.exists() {
        return Err(format!(
            "output directory already exists: {}",
            options.output_dir.display()
        ));
    }
    std::fs::create_dir_all(&options.output_dir)
        .map_err(|error| format!("create pprof-tax output: {error}"))?;

    let run_kind = if options.reduced_smoke {
        "reduced-smoke"
    } else {
        "headline"
    };
    let mode = if options.reduced_smoke {
        "smoke"
    } else {
        "full"
    };
    if options.reduced_smoke {
        if options.blocks == 0 || options.blocks >= pprof_tax::PPROF_TAX_MIN_BLOCKS {
            return Err("--reduced-smoke requires 1 <= --blocks < 15".into());
        }
    } else if options.blocks < pprof_tax::PPROF_TAX_MIN_BLOCKS {
        return Err(format!(
            "headline pprof-tax runs require at least {} blocks",
            pprof_tax::PPROF_TAX_MIN_BLOCKS
        ));
    }

    let manifest_bytes = std::fs::read(&options.manifest)
        .map_err(|error| format!("read pprof-tax manifest: {error}"))?;
    let manifest_text = std::str::from_utf8(&manifest_bytes)
        .map_err(|error| format!("pprof-tax manifest is not UTF-8: {error}"))?;
    let manifest: PprofTaxManifest = serde_json::from_str(manifest_text)
        .map_err(|error| format!("parse pprof-tax manifest: {error}"))?;
    pprof_tax::validate_manifest(&manifest)?;
    let configuration_manifest_sha256 = sha256_bytes(&manifest_bytes);
    write_new_bytes(
        options.output_dir.join("pprof-tax-manifest.json"),
        &manifest_bytes,
    )?;

    for compiled in &manifest.compiled_configurations {
        let actual = sha256_file(Path::new(&compiled.executable_path))?;
        if actual != compiled.executable_sha256 {
            return Err(format!(
                "executable hash mismatch for {}: expected {}, got {actual}",
                compiled.compiled_configuration_id, compiled.executable_sha256
            ));
        }
    }

    let topology = detect_topology()?;
    let publication_runner = collect_publication_runner(topology)?;
    let runner_metadata = RunnerMetadata {
        os: publication_runner.os.clone(),
        architecture: publication_runner.architecture.clone(),
        physical_cores: publication_runner.physical_cores,
        logical_cores: publication_runner.logical_cores,
    };
    let pprof_topology = pprof_tax::PprofTaxTopology {
        physical_cores: publication_runner.physical_cores,
        logical_cores: publication_runner.logical_cores,
    };

    let cells = pprof_tax::pprof_tax_cells(&topology)?;
    if cells.is_empty() {
        return Err("pprof-tax cell matrix is empty".into());
    }

    let reference_configuration = pprof_tax::configuration(CALIBRATION_CONFIGURATION_ID)
        .ok_or_else(|| {
            format!("pprof-tax configuration table is missing {CALIBRATION_CONFIGURATION_ID}")
        })?;
    let reference_compiled = manifest
        .compiled_configurations
        .iter()
        .find(|entry| {
            entry.compiled_configuration_id == reference_configuration.compiled_configuration_id
        })
        .ok_or_else(|| {
            format!(
                "pprof-tax manifest is missing compiled configuration {}",
                reference_configuration.compiled_configuration_id
            )
        })?;

    let mut diagnostics = create_new_writer(options.output_dir.join("diagnostics.jsonl"))?;
    write_json_line(
        &mut diagnostics,
        &json!({
            "event": "pprof-tax-run-start",
            "metric_schema_version": pprof_tax::PPROF_TAX_SCHEMA_VERSION,
            "blocks": options.blocks,
            "run_seed": options.run_seed,
            "mode": mode,
        }),
    )?;

    let runner_started = Instant::now();
    let mut calibrations: Vec<PprofTaxRawCell> = Vec::with_capacity(cells.len());
    for cell in &cells {
        let calibration = calibrate_pprof_tax_cell(
            reference_configuration,
            reference_compiled,
            cell,
            &runner_metadata,
            run_kind,
            options.run_seed,
            options.timeout,
        )?;
        write_json_line(
            &mut diagnostics,
            &json!({
                "event": "pprof-tax-cell-calibrated",
                "scenario_id": calibration.scenario_id,
                "thread_point": calibration.thread_point,
                "operations_per_worker": calibration.operations_per_worker,
                "calibration_elapsed_ns": calibration.calibration_elapsed_ns,
            }),
        )?;
        calibrations.push(calibration);
    }
    let calibration_by_cell: BTreeMap<(&str, &str), &PprofTaxRawCell> = calibrations
        .iter()
        .map(|value| {
            (
                (value.scenario_id.as_str(), value.thread_point.as_str()),
                value,
            )
        })
        .collect();

    let orders = pprof_tax::block_orders(options.blocks, options.run_seed)?;
    let profile_root = options.output_dir.join("profiles");
    let mut samples: Vec<PprofTaxRawSample> = Vec::new();
    let mut rate1_wall = Duration::ZERO;
    let mut rate1_budget_exhausted = false;

    for (block_index, order) in orders.iter().enumerate() {
        let block_id = block_index as u32;
        for cell in &cells {
            let calibration = *calibration_by_cell
                .get(&(cell.scenario_id, cell.thread_point))
                .ok_or_else(|| {
                    format!(
                        "missing pprof-tax calibration for {}/{}",
                        cell.scenario_id, cell.thread_point
                    )
                })?;
            for (position, &configuration_id) in order.iter().enumerate() {
                let position = position as u8;
                let configuration = pprof_tax::configuration(configuration_id)
                    .ok_or_else(|| format!("unknown pprof-tax configuration {configuration_id}"))?;
                let compiled = manifest
                    .compiled_configurations
                    .iter()
                    .find(|entry| {
                        entry.compiled_configuration_id == configuration.compiled_configuration_id
                    })
                    .ok_or_else(|| {
                        format!(
                            "pprof-tax manifest is missing compiled configuration {}",
                            configuration.compiled_configuration_id
                        )
                    })?;
                let is_rate1_stress = configuration_id == STRESS_CONFIGURATION_ID;
                let workload_seed = pprof_tax_workload_seed(
                    options.run_seed,
                    block_id,
                    cell.scenario_id,
                    cell.thread_point,
                );

                if is_rate1_stress && rate1_budget_exhausted {
                    samples.push(build_invalid_rate1_sample(
                        block_id,
                        position,
                        configuration,
                        compiled,
                        &configuration_manifest_sha256,
                        cell,
                        calibration,
                        workload_seed,
                        false,
                        None,
                        true,
                    ));
                    continue;
                }

                let request = build_pprof_tax_child_request(
                    configuration,
                    compiled,
                    cell,
                    calibration.operations_per_worker,
                    calibration.warmup_operations_per_worker,
                    options.run_seed,
                    block_id,
                    position,
                    run_kind,
                    &runner_metadata,
                    Some(&profile_root),
                )?;
                let attempt_started = Instant::now();
                let outcome = run_pprof_tax_child(compiled, &request, options.timeout);
                let attempt_elapsed = attempt_started.elapsed();
                if is_rate1_stress {
                    rate1_wall = rate1_wall.saturating_add(attempt_elapsed);
                    if rate1_wall > options.stress_budget {
                        rate1_budget_exhausted = true;
                    }
                }
                match outcome {
                    Ok((response, peak_rss_bytes)) => {
                        let sample = build_raw_sample(
                            block_id,
                            position,
                            configuration,
                            compiled,
                            &configuration_manifest_sha256,
                            cell,
                            calibration,
                            &request,
                            &response,
                            peak_rss_bytes,
                            &options.output_dir,
                        )?;
                        samples.push(sample);
                    }
                    Err(error) => {
                        if is_rate1_stress {
                            let timed_out = error.starts_with(CHILD_TIMEOUT_PREFIX);
                            write_json_line(
                                &mut diagnostics,
                                &json!({
                                    "event": "pprof-tax-rate1-stress-failure",
                                    "block_id": block_id,
                                    "scenario_id": cell.scenario_id,
                                    "thread_point": cell.thread_point,
                                    "error": error,
                                }),
                            )?;
                            samples.push(build_invalid_rate1_sample(
                                block_id,
                                position,
                                configuration,
                                compiled,
                                &configuration_manifest_sha256,
                                cell,
                                calibration,
                                workload_seed,
                                timed_out,
                                None,
                                false,
                            ));
                        } else {
                            return Err(format!(
                                "pprof-tax child failed at block {block_id} position {position} configuration {configuration_id}: {error}"
                            ));
                        }
                    }
                }
            }
        }
        if runner_started.elapsed().as_secs_f64() > HARD_LIMIT_SECONDS {
            return Err(format!(
                "pprof-tax measuring phase exceeded the {HARD_LIMIT_SECONDS:.0}s hard limit"
            ));
        }
    }

    let run_identity = collect_pprof_tax_run_identity(&manifest.fork_source_sha)?;
    let raw = PprofTaxRawRun {
        raw_schema_version: pprof_tax::PPROF_TAX_RAW_SCHEMA_VERSION.into(),
        metric_schema_version: pprof_tax::PPROF_TAX_SCHEMA_VERSION.into(),
        mode: mode.into(),
        run: run_identity,
        runner: publication_runner,
        run_seed: options.run_seed,
        blocks: options.blocks,
        configuration_manifest_sha256: configuration_manifest_sha256.clone(),
        manifest,
        topology: pprof_topology,
        cells: calibrations,
        block_orders: orders
            .iter()
            .map(|order| order.iter().map(|value| value.to_string()).collect())
            .collect(),
        stress_budget_seconds: options.stress_budget.as_secs(),
        samples,
    };
    pprof_tax::validate_raw_run(&raw)?;
    write_new_json(options.output_dir.join("pprof-tax-raw-run.json"), &raw)?;

    let valid = raw
        .samples
        .iter()
        .filter(|sample| sample.validity_status == "valid")
        .count();
    let invalid = raw.samples.len() - valid;
    println!(
        "PASS pprof-tax matrix: {valid} valid, {invalid} invalid samples across {} cells in {:.1}s",
        cells.len(),
        runner_started.elapsed().as_secs_f64()
    );
    Ok(())
}

/// Calibrate one cell once, on the fork-pprof-off executable, and freeze its
/// per-worker operation count for every configuration in the paired matrix.
#[allow(clippy::too_many_arguments)]
fn calibrate_pprof_tax_cell(
    reference_configuration: &PprofTaxConfiguration,
    reference_compiled: &PprofTaxCompiledConfiguration,
    cell: &PprofTaxCellSpec,
    runner_metadata: &RunnerMetadata,
    run_kind: &str,
    run_seed: u64,
    timeout: Duration,
) -> Result<PprofTaxRawCell, String> {
    let mut operations = CALIBRATION_INITIAL_OPERATIONS;
    for _ in 0..CALIBRATION_ATTEMPTS {
        let warmup = (operations / 10).max(1);
        let request = build_pprof_tax_child_request(
            reference_configuration,
            reference_compiled,
            cell,
            operations,
            warmup,
            run_seed,
            u32::MAX,
            0,
            run_kind,
            runner_metadata,
            None,
        )?;
        let (response, _peak_rss_bytes) =
            run_pprof_tax_child(reference_compiled, &request, timeout)?;
        let elapsed = response.inner.sample.elapsed_ns;
        if (pprof_tax::PPROF_TAX_MIN_BLOCK_NS..=pprof_tax::PPROF_TAX_MAX_BLOCK_NS)
            .contains(&elapsed)
        {
            return Ok(PprofTaxRawCell {
                scenario_id: cell.scenario_id.to_string(),
                thread_point: cell.thread_point.to_string(),
                thread_count: cell.thread_count,
                operations_per_worker: operations,
                warmup_operations_per_worker: warmup,
                calibration_elapsed_ns: elapsed,
            });
        }
        let scaled = (u128::from(operations) * u128::from(pprof_tax::PPROF_TAX_TARGET_BLOCK_NS))
            / u128::from(elapsed.max(1));
        let bounded = scaled
            .max(1)
            .min(u128::from(operations).saturating_mul(16))
            .max(u128::from(operations) / 16);
        let next =
            u64::try_from(bounded).map_err(|_| "pprof-tax calibration overflowed".to_string())?;
        if next == operations {
            return Err(format!(
                "pprof-tax calibration for {}/{} did not converge",
                cell.scenario_id, cell.thread_point
            ));
        }
        operations = next.max(1);
    }
    Err(format!(
        "pprof-tax calibration for {}/{} exhausted its attempts",
        cell.scenario_id, cell.thread_point
    ))
}

/// Deterministic workload seed shared by every configuration inside one
/// paired block: it depends only on (run seed, block, scenario, thread
/// point), never on the configuration, so all seven children replay the same
/// operation stream.
fn pprof_tax_workload_seed(
    run_seed: u64,
    block_id: u32,
    scenario_id: &str,
    thread_point: &str,
) -> u64 {
    const DOMAIN: u64 = 0x7072_6f66_5f74_6178; // "prof_tax"
    let mut state = splitmix64(run_seed ^ DOMAIN);
    state = splitmix64(state ^ u64::from(block_id));
    for chunk in scenario_id.as_bytes().chunks(8) {
        let mut buffer = [0_u8; 8];
        buffer[..chunk.len()].copy_from_slice(chunk);
        state = splitmix64(state ^ u64::from_le_bytes(buffer));
    }
    for chunk in thread_point.as_bytes().chunks(8) {
        let mut buffer = [0_u8; 8];
        buffer[..chunk.len()].copy_from_slice(chunk);
        state = splitmix64(state ^ u64::from_le_bytes(buffer));
    }
    state | 1
}

#[allow(clippy::too_many_arguments)]
fn build_pprof_tax_child_request(
    configuration: &PprofTaxConfiguration,
    compiled: &PprofTaxCompiledConfiguration,
    cell: &PprofTaxCellSpec,
    operations_per_worker: u64,
    warmup_operations_per_worker: u64,
    run_seed: u64,
    block_id: u32,
    position: u8,
    run_kind: &str,
    runner_metadata: &RunnerMetadata,
    profile_root: Option<&Path>,
) -> Result<PprofTaxChildRequest, String> {
    let workload_seed =
        pprof_tax_workload_seed(run_seed, block_id, cell.scenario_id, cell.thread_point);
    let allocator = AllocatorIdentity {
        allocator_id: compiled.allocator_id.clone(),
        allocator_version: compiled.allocator_version.clone(),
        source_sha: compiled.source_sha.clone(),
        library_sha256: compiled.static_library_sha256.clone(),
        child_binary_sha256: compiled.executable_sha256.clone(),
    };
    let toolchain = ToolchainMetadata {
        rustc: rustc_version(),
        target: "x86_64-unknown-linux-gnu".into(),
        compiler: compiled.c_compiler_identity.clone(),
        linker: compiled.linker_identity.clone(),
    };
    let inner = BenchmarkChildRequest {
        protocol_version: CHILD_PROTOCOL_VERSION.into(),
        schema_version: RAW_SCHEMA_VERSION.into(),
        suite_version: CORE_SUITE_VERSION.into(),
        run_kind: run_kind.into(),
        execution_mode: "normal".into(),
        run_seed,
        block_id,
        ordinal: 0,
        workload_seed,
        allocator,
        scenario_id: cell.scenario_id.into(),
        scenario_version: CORE_SUITE_VERSION.into(),
        thread_point: cell.thread_point.into(),
        physical_cores: runner_metadata.physical_cores,
        logical_cores: runner_metadata.logical_cores,
        transactions_per_worker: operations_per_worker,
        warmup_transactions_per_worker: warmup_operations_per_worker,
        reproduction_command: format!(
            "MIMALLOC_PROF=0 MIMALLOC_MEMORY_EVENTS=0 BENCH_CHILD_BINARY_SHA256={} '{}' --pprof-tax",
            compiled.executable_sha256, compiled.executable_path,
        ),
        runner: runner_metadata.clone(),
        toolchain,
    };
    let (profile_path, profiler_seed) = if configuration.pprof_active {
        let root = profile_root.ok_or("active pprof-tax configuration requires a profile root")?;
        let directory = root
            .join(format!("b{block_id}"))
            .join(format!("{}-{}", cell.scenario_id, cell.thread_point));
        std::fs::create_dir_all(&directory)
            .map_err(|error| format!("create pprof-tax profile directory: {error}"))?;
        let path = directory.join(format!("{}.pb", configuration.configuration_id));
        let seed = splitmix64(run_seed ^ u64::from(block_id) ^ u64::from(position)) | 1;
        (Some(path.to_string_lossy().into_owned()), seed)
    } else {
        (None, 0)
    };
    Ok(PprofTaxChildRequest {
        protocol_version: pprof_tax::PPROF_TAX_CHILD_PROTOCOL_VERSION.into(),
        configuration_id: configuration.configuration_id.to_string(),
        compiled_configuration_id: configuration.compiled_configuration_id.to_string(),
        pprof_active: configuration.pprof_active,
        sampling_interval_bytes: configuration.sampling_interval_bytes,
        profiler_seed,
        profile_path,
        inner,
    })
}

#[allow(clippy::too_many_arguments)]
fn build_raw_sample(
    block_id: u32,
    position: u8,
    configuration: &PprofTaxConfiguration,
    compiled: &PprofTaxCompiledConfiguration,
    configuration_manifest_sha256: &str,
    cell: &PprofTaxCellSpec,
    calibration: &PprofTaxRawCell,
    request: &PprofTaxChildRequest,
    response: &PprofTaxChildResponse,
    peak_rss_bytes: u64,
    output_dir: &Path,
) -> Result<PprofTaxRawSample, String> {
    let inner = &response.inner.sample;
    let (profile_path, profile_sha256, profile_size_bytes) = if response.profile_written {
        let absolute = request
            .profile_path
            .as_ref()
            .ok_or_else(|| "active pprof-tax response has no profile_path to record".to_string())?;
        let absolute = Path::new(absolute);
        let relative = absolute
            .strip_prefix(output_dir)
            .map_err(|_| "pprof-tax profile path escaped the output directory".to_string())?
            .to_string_lossy()
            .into_owned();
        let sha256 = sha256_file(absolute)?;
        let size = std::fs::metadata(absolute)
            .map_err(|error| format!("stat pprof-tax profile: {error}"))?
            .len();
        (Some(relative), Some(sha256), Some(size))
    } else {
        (None, None, None)
    };
    let allocated_bytes_lower_bound =
        pprof_tax::minimum_request_bytes(cell.scenario_id)
            .ok()
            .map(|minimum| {
                (inner.allocation_calls
                    + inner.calloc_calls
                    + inner.aligned_allocation_calls
                    + inner.realloc_calls)
                    .saturating_mul(minimum)
            });
    let (sample_count, sampled_bytes, dropped_records, profiler_arena_bytes) =
        if configuration.pprof_active {
            (
                Some(response.telemetry.sample_count),
                Some(response.telemetry.sampled_bytes),
                Some(response.telemetry.dropped_records),
                Some(response.telemetry.profiler_arena_bytes),
            )
        } else {
            (None, None, None, None)
        };
    let mut sample = PprofTaxRawSample {
        block_id,
        position: u32::from(position),
        configuration_id: configuration.configuration_id.to_string(),
        compiled_configuration_id: configuration.compiled_configuration_id.to_string(),
        configuration_manifest_sha256: configuration_manifest_sha256.to_string(),
        // run_pprof_tax_child already required the response to echo this.
        executable_sha256: compiled.executable_sha256.clone(),
        pprof_compiled: configuration.pprof_compiled,
        pprof_active: configuration.pprof_active,
        sampling_interval_bytes: configuration.sampling_interval_bytes,
        frame_pointer_policy: configuration.frame_pointer_policy.to_string(),
        scenario_id: cell.scenario_id.to_string(),
        thread_point: cell.thread_point.to_string(),
        thread_count: cell.thread_count,
        operations_per_worker: calibration.operations_per_worker,
        workload_seed: inner.workload_seed,
        throughput_operations_per_second: Some(inner.throughput_operations_per_second),
        elapsed_ns: Some(inner.elapsed_ns),
        operation_count: Some(inner.operation_count),
        allocation_calls: Some(inner.allocation_calls),
        checksum: Some(inner.checksum),
        allocated_bytes_lower_bound,
        peak_rss_bytes: Some(peak_rss_bytes),
        end_rss_delta_bytes: response.end_rss_delta_bytes,
        profile_path,
        profile_sha256,
        profile_size_bytes,
        sample_count,
        sampled_bytes,
        dropped_records,
        profiler_arena_bytes,
        interval_confirmed: response.interval_confirmed,
        timed_out: false,
        exit_code: Some(0),
        validity_status: String::new(),
        invalid_reason: None,
    };
    pprof_tax::classify_sample(&mut sample, false);
    Ok(sample)
}

#[allow(clippy::too_many_arguments)]
fn build_invalid_rate1_sample(
    block_id: u32,
    position: u8,
    configuration: &PprofTaxConfiguration,
    compiled: &PprofTaxCompiledConfiguration,
    configuration_manifest_sha256: &str,
    cell: &PprofTaxCellSpec,
    calibration: &PprofTaxRawCell,
    workload_seed: u64,
    timed_out: bool,
    exit_code: Option<i32>,
    stress_budget_exhausted: bool,
) -> PprofTaxRawSample {
    let mut sample = PprofTaxRawSample {
        block_id,
        position: u32::from(position),
        configuration_id: configuration.configuration_id.to_string(),
        compiled_configuration_id: configuration.compiled_configuration_id.to_string(),
        configuration_manifest_sha256: configuration_manifest_sha256.to_string(),
        executable_sha256: compiled.executable_sha256.clone(),
        pprof_compiled: configuration.pprof_compiled,
        pprof_active: configuration.pprof_active,
        sampling_interval_bytes: configuration.sampling_interval_bytes,
        frame_pointer_policy: configuration.frame_pointer_policy.to_string(),
        scenario_id: cell.scenario_id.to_string(),
        thread_point: cell.thread_point.to_string(),
        thread_count: cell.thread_count,
        operations_per_worker: calibration.operations_per_worker,
        workload_seed,
        throughput_operations_per_second: None,
        elapsed_ns: None,
        operation_count: None,
        allocation_calls: None,
        checksum: None,
        allocated_bytes_lower_bound: None,
        peak_rss_bytes: None,
        end_rss_delta_bytes: None,
        profile_path: None,
        profile_sha256: None,
        profile_size_bytes: None,
        sample_count: None,
        sampled_bytes: None,
        dropped_records: None,
        profiler_arena_bytes: None,
        interval_confirmed: None,
        timed_out,
        exit_code,
        validity_status: String::new(),
        invalid_reason: None,
    };
    pprof_tax::classify_sample(&mut sample, stress_budget_exhausted);
    sample
}

/// Spawn one fresh pprof-tax child with a strict watchdog, sample its peak
/// RSS externally, and validate the response identity before returning.
fn run_pprof_tax_child(
    compiled: &PprofTaxCompiledConfiguration,
    request: &PprofTaxChildRequest,
    timeout: Duration,
) -> Result<(PprofTaxChildResponse, u64), String> {
    let encoded = serde_json::to_vec(request)
        .map_err(|error| format!("serialize pprof-tax request: {error}"))?;
    let mut command = Command::new(&compiled.executable_path);
    command
        .arg("--pprof-tax")
        .env_clear()
        .env("MIMALLOC_PROF", "0")
        .env("MIMALLOC_MEMORY_EVENTS", "0")
        .env("BENCH_CHILD_BINARY_SHA256", &compiled.executable_sha256)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut process = command
        .spawn()
        .map_err(|error| format!("spawn pprof-tax child: {error}"))?;
    let pid = process.id();
    let stop = Arc::new(AtomicBool::new(false));
    let stop_for_sampler = Arc::clone(&stop);
    let sampler = std::thread::spawn(move || sample_peak_rss(pid, &stop_for_sampler));
    process
        .stdin
        .take()
        .ok_or_else(|| "pprof-tax child stdin was not piped".to_string())?
        .write_all(&encoded)
        .map_err(|error| format!("write pprof-tax request: {error}"))?;
    let mut stdout = process
        .stdout
        .take()
        .ok_or_else(|| "pprof-tax child stdout was not piped".to_string())?;
    let mut stderr = process
        .stderr
        .take()
        .ok_or_else(|| "pprof-tax child stderr was not piped".to_string())?;
    let stdout_reader = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        stdout
            .read_to_end(&mut bytes)
            .map_err(|error| format!("read pprof-tax child stdout: {error}"))?;
        Ok::<_, String>(bytes)
    });
    let stderr_reader = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        stderr
            .read_to_end(&mut bytes)
            .map_err(|error| format!("read pprof-tax child stderr: {error}"))?;
        Ok::<_, String>(bytes)
    });
    let started = Instant::now();
    let status = loop {
        if let Some(status) = process
            .try_wait()
            .map_err(|error| format!("poll pprof-tax child: {error}"))?
        {
            break status;
        }
        if started.elapsed() >= timeout {
            let _ = process.kill();
            let _ = process.wait();
            let _ = stdout_reader.join();
            let error_bytes = stderr_reader
                .join()
                .map_err(|_| "pprof-tax stderr reader panicked".to_string())??;
            stop.store(true, Ordering::Relaxed);
            let _ = sampler.join();
            return Err(format!(
                "{CHILD_TIMEOUT_PREFIX}: {}",
                String::from_utf8_lossy(&error_bytes)
            ));
        }
        std::thread::sleep(Duration::from_millis(2));
    };
    stop.store(true, Ordering::Relaxed);
    let peak_rss_bytes = sampler
        .join()
        .map_err(|_| "pprof-tax RSS sampler panicked".to_string())?;
    let output = stdout_reader
        .join()
        .map_err(|_| "pprof-tax stdout reader panicked".to_string())??;
    let error_bytes = stderr_reader
        .join()
        .map_err(|_| "pprof-tax stderr reader panicked".to_string())??;
    if !status.success() || !error_bytes.is_empty() {
        return Err(format!(
            "pprof-tax child failed: {}",
            String::from_utf8_lossy(&error_bytes)
        ));
    }
    let response: PprofTaxChildResponse = serde_json::from_slice(&output)
        .map_err(|error| format!("decode pprof-tax child response: {error}"))?;
    if response.configuration_id != request.configuration_id
        || response.compiled_configuration_id != request.compiled_configuration_id
        || response.executable_sha256 != compiled.executable_sha256
    {
        return Err("pprof-tax child response identity mismatch".into());
    }
    response.inner.validate_against(&request.inner)?;
    Ok((response, peak_rss_bytes))
}

fn collect_pprof_tax_run_identity(source_sha: &str) -> Result<RunIdentity, String> {
    let run_origin = if std::env::var("GITHUB_ACTIONS").as_deref() == Ok("true") {
        "github-actions"
    } else {
        "local"
    };
    let source_ref = std::env::var("GITHUB_REF").unwrap_or_else(|_| {
        command_text("git", &["symbolic-ref", "--short", "HEAD"])
            .map(|branch| format!("refs/heads/{branch}"))
            .unwrap_or_else(|| "refs/heads/local-benchmark".into())
    });
    let short_sha = &source_sha[..source_sha.len().min(12)];
    let run_id = std::env::var("GITHUB_RUN_ID")
        .unwrap_or_else(|_| format!("local-{short_sha}-{}", std::process::id()));
    let run_attempt = std::env::var("GITHUB_RUN_ATTEMPT")
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(1);
    let generated_at_utc = command_text("date", &["-u", "+%Y-%m-%dT%H:%M:%SZ"])
        .ok_or_else(|| "unable to collect UTC generation timestamp".to_string())?;
    Ok(RunIdentity {
        source_repository: "https://github.com/zackees/mimalloc-pprof".into(),
        source_sha: source_sha.into(),
        source_ref,
        run_origin: run_origin.into(),
        run_id,
        run_attempt,
        generated_at_utc,
    })
}

fn command_text(program: &str, arguments: &[&str]) -> Option<String> {
    std::process::Command::new(program)
        .args(arguments)
        .output()
        .ok()
        .filter(|output| output.status.success())
        .and_then(|output| String::from_utf8(output.stdout).ok())
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
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

fn parse_options(arguments: impl Iterator<Item = OsString>) -> Result<Options, String> {
    let arguments = arguments
        .map(|value| {
            value
                .into_string()
                .map_err(|_| "arguments must be valid UTF-8".to_string())
        })
        .collect::<Result<Vec<_>, _>>()?;
    let mut manifest = None;
    let mut output_dir = None;
    let mut blocks = None;
    let mut run_seed = None;
    let mut reduced_smoke = false;
    let mut timeout_seconds = 30_u64;
    let mut stress_budget_seconds = 420_u64;
    let mut index = 0;
    while index < arguments.len() {
        let flag = arguments[index].as_str();
        if flag == "--reduced-smoke" {
            reduced_smoke = true;
            index += 1;
            continue;
        }
        if flag == "--help" || flag == "-h" {
            println!("usage: benchmark-pprof-tax-run --manifest <pprof-tax-manifest.json> --output-dir <new-dir> --blocks <n> --run-seed <u64> [--reduced-smoke] [--timeout-seconds 30] [--stress-budget-seconds 420]");
            std::process::exit(0);
        }
        let value = arguments
            .get(index + 1)
            .ok_or_else(|| format!("{flag} requires a value"))?;
        match flag {
            "--manifest" => manifest = Some(PathBuf::from(value)),
            "--output-dir" => output_dir = Some(PathBuf::from(value)),
            "--blocks" => blocks = Some(parse_number("--blocks", value)?),
            "--run-seed" => run_seed = Some(parse_number("--run-seed", value)?),
            "--timeout-seconds" => timeout_seconds = parse_number("--timeout-seconds", value)?,
            "--stress-budget-seconds" => {
                stress_budget_seconds = parse_number("--stress-budget-seconds", value)?
            }
            _ => return Err(format!("unknown argument: {flag}")),
        }
        index += 2;
    }
    let run_seed = run_seed.ok_or("--run-seed is required")?;
    if run_seed == 0 {
        return Err("--run-seed must be non-zero".into());
    }
    if timeout_seconds == 0 {
        return Err("--timeout-seconds must be nonzero".into());
    }
    Ok(Options {
        manifest: manifest.ok_or("--manifest is required")?,
        output_dir: output_dir.ok_or("--output-dir is required")?,
        blocks: blocks.ok_or("--blocks is required")?,
        run_seed,
        reduced_smoke,
        timeout: Duration::from_secs(timeout_seconds),
        stress_budget: Duration::from_secs(stress_budget_seconds),
    })
}

fn parse_number<T: std::str::FromStr>(flag: &str, value: &str) -> Result<T, String> {
    value
        .parse()
        .map_err(|_| format!("{flag} expects a non-negative integer, got {value:?}"))
}
