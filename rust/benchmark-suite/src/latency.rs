//! Honest end-to-end transaction latency measurement and publication.
//!
//! This protocol never derives latency from throughput. Every duration is a
//! paired monotonic-clock observation around one declared transaction.

use std::collections::{BTreeMap, BTreeSet};
use std::io::{Read, Write};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};

use crate::execution::{
    execute_cell, execute_latency_cell, expected_touch_checksum, AllocatorAdapter,
};
use crate::model::{
    AllocatorBuildIdentity, BenchmarkChildRequest, CellCalibration, LatestReport,
    PublicationRunner, RunIdentity,
};
use crate::orchestration::{balanced_block_orders, ChildProgram};
use crate::provenance::sha256_bytes;
use crate::scenarios::{card, CardId, ScenarioCell, ThreadPoint, Topology};
use crate::stats::{
    BootstrapMetadata, ConfidenceInterval, MetricDirection, PairedEffectSummary, BOOTSTRAP_PRNG,
};

pub const LATENCY_SCHEMA_VERSION: &str = "transaction-latency-v1";
pub const LATENCY_CHILD_PROTOCOL_VERSION: &str = "transaction-latency-child-v1";
pub const LATENCY_MIN_BLOCKS: u32 = 15;
pub const LATENCY_MIN_SAMPLES: usize = 10_000;
pub const LATENCY_INITIAL_SAMPLE_DENOMINATOR: u64 = 1024;
pub const LATENCY_BOOTSTRAP_RESAMPLES: u32 = 10_000;
const REFERENCE_ALLOCATOR: &str = "upstream-mimalloc";
const ALLOCATOR_IDS: [&str; 5] = [
    "tcmalloc",
    "jemalloc",
    "upstream-mimalloc",
    "bun-mimalloc",
    "mimalloc-pprof",
];

fn is_lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct LatencyObservation {
    pub thread_index: u32,
    pub transaction_index: u64,
    pub duration_ns: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LatencyExecutionResult {
    pub observations: Vec<LatencyObservation>,
    pub completed_transactions: u64,
    pub checksum: u64,
    pub actual_cpu_ids: Vec<Option<u32>>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct LatencyClock {
    pub source: String,
    pub implementation: String,
    pub resolution_ns: u64,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ContextSwitchCounts {
    pub voluntary: u64,
    pub involuntary: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct LatencyScheduling {
    pub affinity_policy: String,
    pub actual_cpu_ids: Vec<Option<u32>>,
    pub thread_count: u32,
    pub physical_cores: u32,
    pub logical_cores: u32,
    pub context_switches: ContextSwitchCounts,
    pub runner_class: String,
    pub clock: LatencyClock,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct LatencyChildRequest {
    pub protocol_version: String,
    pub metric_schema_version: String,
    pub sample_denominator: u64,
    pub control: bool,
    pub runner_class: String,
    pub affinity_policy: String,
    pub benchmark: BenchmarkChildRequest,
}

impl LatencyChildRequest {
    pub fn validate(&self) -> Result<(), String> {
        if self.protocol_version != LATENCY_CHILD_PROTOCOL_VERSION
            || self.metric_schema_version != LATENCY_SCHEMA_VERSION
            || self.sample_denominator == 0
            || self.runner_class.is_empty()
            || self.affinity_policy.is_empty()
        {
            return Err("unsupported latency child protocol, schema, or sample rate".into());
        }
        self.benchmark.validate()?;
        latency_cell(
            &self.benchmark.scenario_id,
            &self.benchmark.thread_point,
            Topology {
                physical_cores: self.benchmark.physical_cores as usize,
                logical_cores: self.benchmark.logical_cores as usize,
            },
            self.benchmark.transactions_per_worker,
            self.benchmark.workload_seed,
        )?;
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct LatencyChildResponse {
    pub protocol_version: String,
    pub metric_schema_version: String,
    pub control: bool,
    pub completed_transactions: u64,
    pub checksum: u64,
    pub observations: Vec<LatencyObservation>,
    pub scheduling: LatencyScheduling,
}

impl LatencyChildResponse {
    pub fn validate_against(&self, request: &LatencyChildRequest) -> Result<(), String> {
        request.validate()?;
        if self.protocol_version != LATENCY_CHILD_PROTOCOL_VERSION
            || self.metric_schema_version != LATENCY_SCHEMA_VERSION
            || self.control != request.control
        {
            return Err("latency child response identity mismatch".into());
        }
        let cell = latency_cell(
            &request.benchmark.scenario_id,
            &request.benchmark.thread_point,
            Topology {
                physical_cores: request.benchmark.physical_cores as usize,
                logical_cores: request.benchmark.logical_cores as usize,
            },
            request.benchmark.transactions_per_worker,
            request.benchmark.workload_seed,
        )?;
        let expected = all_sample_indices(&cell, request.sample_denominator)?;
        let observed = self
            .observations
            .iter()
            .map(|value| (value.thread_index, value.transaction_index))
            .collect::<Vec<_>>();
        let expected_checksum = if request.control {
            1
        } else {
            expected_touch_checksum(&cell)?
        };
        if observed != expected
            || self.completed_transactions != cell.requested_transactions()
            || self.checksum != expected_checksum
            || self.scheduling.thread_count != cell.threads as u32
            || self.scheduling.physical_cores != request.benchmark.physical_cores
            || self.scheduling.logical_cores != request.benchmark.logical_cores
            || self.scheduling.runner_class != request.runner_class
            || self.scheduling.affinity_policy != request.affinity_policy
            || self.scheduling.actual_cpu_ids.len() != cell.threads
            || self.scheduling.clock.source.is_empty()
            || self.scheduling.clock.implementation.is_empty()
            || self.scheduling.clock.resolution_ns == 0
            || self.observations.iter().any(|value| value.duration_ns == 0)
        {
            return Err(
                "latency child response contradicts its schedule, clock, or topology".into(),
            );
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct LatencyRawSample {
    pub metric_schema_version: String,
    pub block_id: u32,
    pub ordinal: u8,
    pub workload_seed: u64,
    pub allocator_id: String,
    pub allocator_source_sha: String,
    pub child_binary_sha256: String,
    pub scenario_id: String,
    pub thread_point: String,
    pub thread_count: u32,
    pub sample_denominator: u64,
    pub transaction_definition: String,
    pub measured: LatencyChildResponse,
    pub control: LatencyChildResponse,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct LatencyRawRun {
    pub metric_schema_version: String,
    pub status: String,
    pub run_seed: u64,
    pub run: RunIdentity,
    pub runner: PublicationRunner,
    pub allocator_lock_sha256: String,
    pub allocators: Vec<AllocatorBuildIdentity>,
    pub calibrations: Vec<CellCalibration>,
    pub sampling_denominators: BTreeMap<String, u64>,
    pub samples: Vec<LatencyRawSample>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct LatencyDistribution {
    pub count: u64,
    pub p50_ns: f64,
    pub p95_ns: f64,
    pub p99_ns: f64,
    pub min_ns: u64,
    pub max_ns: u64,
    pub median_absolute_deviation_ns: f64,
    pub iqr_ns: f64,
    pub zero_count: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct LatencyBlockSummary {
    pub block_id: u32,
    pub allocator_id: String,
    pub scenario_id: String,
    pub thread_point: String,
    pub measured: LatencyDistribution,
    pub control: LatencyDistribution,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct LatencyAbsoluteSummary {
    pub allocator_id: String,
    pub scenario_id: String,
    pub thread_point: String,
    pub transaction_definition: String,
    pub measured: LatencyDistribution,
    pub control: LatencyDistribution,
    pub overhead_valid: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct LatencyPairedSummary {
    pub scenario_id: String,
    pub thread_point: String,
    pub quantile: String,
    pub summary: PairedEffectSummary,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct LatencyMethodology {
    pub transaction_boundaries: BTreeMap<String, String>,
    pub quantile_method: String,
    pub sampling_schedule: String,
    pub overhead_control: String,
    pub bootstrap: String,
    pub storage_decision: String,
    pub tail_policy: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct LatencyMetricReport {
    pub metric_schema_version: String,
    pub status: String,
    pub invalid_reason: Option<String>,
    pub metric_comparison_key: String,
    pub run: RunIdentity,
    pub runner: PublicationRunner,
    pub direction: MetricDirection,
    pub informational: bool,
    pub sampling_denominators: BTreeMap<String, u64>,
    pub methodology: LatencyMethodology,
    pub absolute_summaries: Vec<LatencyAbsoluteSummary>,
    pub paired_summaries: Vec<LatencyPairedSummary>,
    pub block_summaries: Vec<LatencyBlockSummary>,
    pub raw_samples: Vec<LatencyRawSample>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct LatencyHistoryReport {
    pub metric_schema_version: String,
    pub status: String,
    pub metric_comparison_key: String,
    pub run: RunIdentity,
    pub runner_fingerprint_sha256: String,
    pub direction: MetricDirection,
    pub informational: bool,
    pub sampling_denominators: BTreeMap<String, u64>,
    pub methodology: LatencyMethodology,
    pub absolute_summaries: Vec<LatencyAbsoluteSummary>,
    pub paired_summaries: Vec<LatencyPairedSummary>,
    pub block_summaries: Vec<LatencyBlockSummary>,
}

impl LatencyMetricReport {
    pub fn history_projection(&self) -> LatencyHistoryReport {
        LatencyHistoryReport {
            metric_schema_version: self.metric_schema_version.clone(),
            status: self.status.clone(),
            metric_comparison_key: self.metric_comparison_key.clone(),
            run: self.run.clone(),
            runner_fingerprint_sha256: self.runner.fingerprint_sha256.clone(),
            direction: self.direction,
            informational: self.informational,
            sampling_denominators: self.sampling_denominators.clone(),
            methodology: self.methodology.clone(),
            absolute_summaries: self.absolute_summaries.clone(),
            paired_summaries: self.paired_summaries.clone(),
            block_summaries: self.block_summaries.clone(),
        }
    }
}

pub fn latency_scenario_cells(
    topology: Topology,
) -> Result<Vec<(CardId, ThreadPoint, &'static str)>, String> {
    topology.validate().map_err(|error| error.to_string())?;
    let cells = vec![
        (
            CardId::TinyFixed64,
            ThreadPoint::One,
            transaction_definition(CardId::TinyFixed64),
        ),
        (
            CardId::SmallLogMixed,
            ThreadPoint::One,
            transaction_definition(CardId::SmallLogMixed),
        ),
        (
            CardId::SmallLogMixed,
            ThreadPoint::PhysicalCores,
            transaction_definition(CardId::SmallLogMixed),
        ),
        (
            CardId::CrossThreadProducerConsumer,
            ThreadPoint::PhysicalCores,
            transaction_definition(CardId::CrossThreadProducerConsumer),
        ),
        (
            CardId::LargeObjects,
            ThreadPoint::One,
            transaction_definition(CardId::LargeObjects),
        ),
    ];
    for (card, point, _) in &cells {
        ScenarioCell::new(*card, *point, topology, 1, 1).map_err(|error| error.to_string())?;
    }
    Ok(cells)
}

pub const fn transaction_definition(card: CardId) -> &'static str {
    match card {
        CardId::CrossThreadProducerConsumer => "producer allocation through consumer free completion, including queue and ownership transfer",
        CardId::LargeObjects => "allocation plus one-byte-per-page touch/checksum plus free",
        CardId::TinyFixed64 | CardId::SmallLogMixed => "allocation plus required touch/checksum plus free",
        _ => "not part of transaction-latency-v1",
    }
}

fn latency_cell(
    scenario: &str,
    point: &str,
    topology: Topology,
    transactions: u64,
    seed: u64,
) -> Result<ScenarioCell, String> {
    let card = CardId::parse(scenario).ok_or_else(|| "unknown latency scenario".to_string())?;
    let point =
        ThreadPoint::parse(point).ok_or_else(|| "unknown latency thread point".to_string())?;
    if !latency_scenario_cells(topology)?
        .iter()
        .any(|(candidate_card, candidate_point, _)| {
            *candidate_card == card && *candidate_point == point
        })
    {
        return Err("undeclared transaction-latency-v1 cell".into());
    }
    ScenarioCell::new(card, point, topology, transactions, seed).map_err(|error| error.to_string())
}

/// Deterministic 1/N schedule. The seed-controlled offset prevents a fixed
/// request-cycle phase from being overrepresented while remaining identical
/// for all allocators in a paired block.
pub fn deterministic_sample_indices(
    workload_seed: u64,
    worker: u32,
    transactions: u64,
    denominator: u64,
) -> Result<Vec<u64>, String> {
    if workload_seed == 0 || transactions == 0 || denominator == 0 {
        return Err("latency schedule requires nonzero seed, transactions, and denominator".into());
    }
    let offset =
        splitmix64(workload_seed ^ 0x6c61_7465_6e63_7901 ^ u64::from(worker)) % denominator;
    let capacity = transactions.saturating_add(denominator - 1) / denominator;
    let mut output = Vec::with_capacity(capacity as usize);
    let mut index = offset;
    while index < transactions {
        output.push(index);
        index = index
            .checked_add(denominator)
            .ok_or_else(|| "latency sample schedule overflowed".to_string())?;
    }
    Ok(output)
}

fn all_sample_indices(cell: &ScenarioCell, denominator: u64) -> Result<Vec<(u32, u64)>, String> {
    let mut output = Vec::new();
    for worker in 0..cell.threads {
        output.extend(
            deterministic_sample_indices(
                cell.seed,
                worker as u32,
                cell.transactions_per_worker,
                denominator,
            )?
            .into_iter()
            .map(|index| (worker as u32, index)),
        );
    }
    Ok(output)
}

pub fn minimum_transactions_per_worker(
    threads: usize,
    blocks: u32,
    denominator: u64,
    minimum_samples: usize,
) -> Result<u64, String> {
    if threads == 0 || blocks == 0 || denominator == 0 || minimum_samples == 0 {
        return Err("latency minimum calculation received zero".into());
    }
    let per_block = (minimum_samples as u64).div_ceil(u64::from(blocks));
    let per_worker = per_block.div_ceil(threads as u64);
    per_worker
        .checked_mul(denominator)
        .and_then(|value| value.checked_add(denominator))
        .ok_or_else(|| "latency transaction count overflowed".into())
}

pub fn choose_sample_denominator(
    calibrated_transactions_per_worker: u64,
    threads: usize,
    blocks: u32,
) -> Result<u64, String> {
    if threads == 0 || blocks == 0 || calibrated_transactions_per_worker == 0 {
        return Err("latency rate selection received zero".into());
    }
    let total = calibrated_transactions_per_worker
        .checked_mul(threads as u64)
        .and_then(|value| value.checked_mul(u64::from(blocks)))
        .ok_or_else(|| "latency rate selection overflowed".to_string())?;
    Ok((total / LATENCY_MIN_SAMPLES as u64).clamp(1, LATENCY_INITIAL_SAMPLE_DENOMINATOR))
}

pub fn execute_latency_child_request<A: AllocatorAdapter>(
    adapter: &A,
    request: LatencyChildRequest,
) -> Result<LatencyChildResponse, String> {
    request.validate()?;
    let expected = &request.benchmark.allocator;
    if adapter.allocator_id() != expected.allocator_id
        || adapter.allocator_version() != expected.allocator_version
        || adapter.source_sha() != expected.source_sha
        || adapter.library_sha256() != expected.library_sha256
    {
        return Err("latency request allocator does not match linked adapter".into());
    }
    let topology = Topology {
        physical_cores: request.benchmark.physical_cores as usize,
        logical_cores: request.benchmark.logical_cores as usize,
    };
    if request.benchmark.warmup_transactions_per_worker > 0 {
        let warmup = latency_cell(
            &request.benchmark.scenario_id,
            &request.benchmark.thread_point,
            topology,
            request.benchmark.warmup_transactions_per_worker,
            request.benchmark.workload_seed ^ 0xa076_1d64_78bd_642f,
        )?;
        execute_cell(adapter, &warmup)?;
    }
    let cell = latency_cell(
        &request.benchmark.scenario_id,
        &request.benchmark.thread_point,
        topology,
        request.benchmark.transactions_per_worker,
        request.benchmark.workload_seed,
    )?;
    let before = read_context_switches();
    let execution =
        execute_latency_cell(adapter, &cell, request.sample_denominator, request.control)?;
    let after = read_context_switches();
    let response = LatencyChildResponse {
        protocol_version: LATENCY_CHILD_PROTOCOL_VERSION.into(),
        metric_schema_version: LATENCY_SCHEMA_VERSION.into(),
        control: request.control,
        completed_transactions: execution.completed_transactions,
        checksum: execution.checksum,
        observations: execution.observations,
        scheduling: LatencyScheduling {
            affinity_policy: request.affinity_policy.clone(),
            actual_cpu_ids: execution.actual_cpu_ids,
            thread_count: cell.threads as u32,
            physical_cores: request.benchmark.physical_cores,
            logical_cores: request.benchmark.logical_cores,
            context_switches: ContextSwitchCounts {
                voluntary: after.voluntary.saturating_sub(before.voluntary),
                involuntary: after.involuntary.saturating_sub(before.involuntary),
            },
            runner_class: request.runner_class.clone(),
            clock: monotonic_clock(),
        },
    };
    response.validate_against(&request)?;
    Ok(response)
}

pub fn run_latency_child(
    child: &ChildProgram,
    request: &LatencyChildRequest,
    timeout: Duration,
) -> Result<LatencyChildResponse, String> {
    request.validate()?;
    if child.allocator != request.benchmark.allocator || timeout.is_zero() {
        return Err("latency child identity mismatch or zero timeout".into());
    }
    let encoded = serde_json::to_vec(request)
        .map_err(|error| format!("serialize latency request: {error}"))?;
    let mut process = Command::new(&child.program);
    process
        .args(&child.arguments)
        .arg("--latency")
        .env_clear()
        .envs(child.environment.iter().map(|(key, value)| (key, value)))
        .env("MIMALLOC_PROF", "0")
        .env("MIMALLOC_MEMORY_EVENTS", "0")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child_process = process
        .spawn()
        .map_err(|error| format!("spawn latency child: {error}"))?;
    child_process
        .stdin
        .take()
        .ok_or_else(|| "latency child stdin was not piped".to_string())?
        .write_all(&encoded)
        .map_err(|error| format!("write latency request: {error}"))?;
    let mut stdout = child_process
        .stdout
        .take()
        .ok_or_else(|| "latency child stdout was not piped".to_string())?;
    let mut stderr = child_process
        .stderr
        .take()
        .ok_or_else(|| "latency child stderr was not piped".to_string())?;
    // Drain both pipes while polling so a valid raw vector larger than the
    // platform pipe capacity cannot deadlock the child before exit.
    let stdout_reader = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        stdout
            .read_to_end(&mut bytes)
            .map_err(|error| format!("read latency child stdout: {error}"))?;
        Ok::<_, String>(bytes)
    });
    let stderr_reader = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        stderr
            .read_to_end(&mut bytes)
            .map_err(|error| format!("read latency child stderr: {error}"))?;
        Ok::<_, String>(bytes)
    });
    let started = Instant::now();
    let status = loop {
        if let Some(status) = child_process
            .try_wait()
            .map_err(|error| format!("poll latency child: {error}"))?
        {
            break status;
        }
        if started.elapsed() >= timeout {
            let _ = child_process.kill();
            let _ = child_process.wait();
            let _ = stdout_reader.join();
            let error_bytes = stderr_reader
                .join()
                .map_err(|_| "latency stderr reader panicked".to_string())??;
            return Err(format!(
                "latency child timed out: {}",
                String::from_utf8_lossy(&error_bytes)
            ));
        }
        std::thread::sleep(Duration::from_millis(2));
    };
    let output = stdout_reader
        .join()
        .map_err(|_| "latency stdout reader panicked".to_string())??;
    let error_bytes = stderr_reader
        .join()
        .map_err(|_| "latency stderr reader panicked".to_string())??;
    if !status.success() || !error_bytes.is_empty() {
        return Err(format!(
            "latency child failed: {}",
            String::from_utf8_lossy(&error_bytes)
        ));
    }
    let response: LatencyChildResponse = serde_json::from_slice(&output)
        .map_err(|error| format!("decode latency child response: {error}"))?;
    response.validate_against(request)?;
    Ok(response)
}

fn read_context_switches() -> ContextSwitchCounts {
    let Ok(status) = std::fs::read_to_string("/proc/self/status") else {
        return ContextSwitchCounts {
            voluntary: 0,
            involuntary: 0,
        };
    };
    let parse = |prefix: &str| {
        status
            .lines()
            .find_map(|line| line.strip_prefix(prefix))
            .and_then(|value| value.trim().parse().ok())
            .unwrap_or(0)
    };
    ContextSwitchCounts {
        voluntary: parse("voluntary_ctxt_switches:"),
        involuntary: parse("nonvoluntary_ctxt_switches:"),
    }
}

#[cfg(target_os = "linux")]
fn monotonic_clock() -> LatencyClock {
    #[repr(C)]
    struct Timespec {
        tv_sec: std::os::raw::c_long,
        tv_nsec: std::os::raw::c_long,
    }
    unsafe extern "C" {
        fn clock_getres(clock_id: std::os::raw::c_int, value: *mut Timespec)
            -> std::os::raw::c_int;
    }
    let mut value = Timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    let result = unsafe { clock_getres(1, &mut value) };
    let resolution = if result == 0 {
        (value.tv_sec as u64)
            .saturating_mul(1_000_000_000)
            .saturating_add(value.tv_nsec as u64)
            .max(1)
    } else {
        1
    };
    LatencyClock {
        source: "monotonic".into(),
        implementation: "std::time::Instant/CLOCK_MONOTONIC".into(),
        resolution_ns: resolution,
    }
}

#[cfg(not(target_os = "linux"))]
fn monotonic_clock() -> LatencyClock {
    LatencyClock {
        source: "monotonic".into(),
        implementation: "std::time::Instant".into(),
        resolution_ns: 1,
    }
}

pub fn summarize_latency(values: &[u64]) -> Result<LatencyDistribution, String> {
    if values.is_empty() {
        return Err("latency distribution is empty".into());
    }
    let zero_count = values.iter().filter(|value| **value == 0).count() as u64;
    if zero_count != 0 {
        return Err("zero or negative latency duration invalidates the cell".into());
    }
    let mut numeric = values.iter().map(|value| *value as f64).collect::<Vec<_>>();
    numeric.sort_by(f64::total_cmp);
    let p50 = type7_sorted(&numeric, 0.50);
    let p95 = type7_sorted(&numeric, 0.95);
    let p99 = type7_sorted(&numeric, 0.99);
    let q1 = type7_sorted(&numeric, 0.25);
    let q3 = type7_sorted(&numeric, 0.75);
    let mut deviations = numeric
        .iter()
        .map(|value| (value - p50).abs())
        .collect::<Vec<_>>();
    deviations.sort_by(f64::total_cmp);
    let mad = type7_sorted(&deviations, 0.50);
    Ok(LatencyDistribution {
        count: values.len() as u64,
        p50_ns: p50,
        p95_ns: p95,
        p99_ns: p99,
        min_ns: *values.iter().min().expect("nonempty"),
        max_ns: *values.iter().max().expect("nonempty"),
        median_absolute_deviation_ns: mad,
        iqr_ns: q3 - q1,
        zero_count,
    })
}

pub fn overhead_is_valid(measured: &LatencyDistribution, control: &LatencyDistribution) -> bool {
    control.p50_ns <= measured.p50_ns * 0.05 && measured.p99_ns > control.p99_ns * 2.0
}

pub fn validate_latency_raw_run(raw: &LatencyRawRun) -> Result<(), String> {
    if raw.metric_schema_version != LATENCY_SCHEMA_VERSION
        || raw.status != "complete"
        || raw.run_seed == 0
        || raw.run.source_repository.is_empty()
        || !is_lower_hex(&raw.run.source_sha, 40)
        || raw.run.run_id.is_empty()
        || raw.run.run_attempt == 0
        || raw.runner.runner_class.is_empty()
        || !is_lower_hex(&raw.runner.fingerprint_sha256, 64)
        || raw.runner.physical_cores == 0
        || raw.runner.logical_cores == 0
        || raw.runner.physical_cores > raw.runner.logical_cores
        || !matches!(raw.runner.affinity.policy.as_str(), "unrestricted" | "pinned")
        || (raw.runner.affinity.policy == "pinned"
            && raw.runner.affinity.logical_cpu_ids.is_empty())
        || raw
            .runner
            .affinity
            .logical_cpu_ids
            .iter()
            .collect::<BTreeSet<_>>()
            .len()
            != raw.runner.affinity.logical_cpu_ids.len()
        || !is_lower_hex(&raw.allocator_lock_sha256, 64)
    {
        return Err("latency raw run must be complete transaction-latency-v1".into());
    }
    let topology = Topology {
        physical_cores: raw.runner.physical_cores as usize,
        logical_cores: raw.runner.logical_cores as usize,
    };
    let cells = latency_scenario_cells(topology)?;
    let expected_cells = cells
        .iter()
        .map(|(card, point, _)| format!("{}/{}", card.as_str(), point.name()))
        .collect::<BTreeSet<_>>();
    if raw
        .sampling_denominators
        .keys()
        .cloned()
        .collect::<BTreeSet<_>>()
        != expected_cells
    {
        return Err("latency sampling-rate matrix is incomplete".into());
    }
    if raw
        .sampling_denominators
        .values()
        .any(|value| *value == 0 || *value > LATENCY_INITIAL_SAMPLE_DENOMINATOR)
    {
        return Err("latency sampling denominator is outside the versioned range".into());
    }

    let allocators = raw
        .allocators
        .iter()
        .map(|value| (value.allocator_id.as_str(), value))
        .collect::<BTreeMap<_, _>>();
    if raw.allocators.len() != ALLOCATOR_IDS.len()
        || allocators.keys().copied().collect::<BTreeSet<_>>()
            != ALLOCATOR_IDS.into_iter().collect()
    {
        return Err("latency run does not contain the exact allocator set".into());
    }
    for allocator in raw.allocators.iter() {
        if !is_lower_hex(&allocator.source_sha, 40)
            || !is_lower_hex(&allocator.child_binary_sha256, 64)
        {
            return Err("latency allocator provenance contains an invalid digest".into());
        }
    }

    let calibrations = raw
        .calibrations
        .iter()
        .map(|value| {
            (
                (value.scenario_id.as_str(), value.thread_point.as_str()),
                value,
            )
        })
        .collect::<BTreeMap<_, _>>();
    if calibrations.len() != cells.len() || raw.calibrations.len() != cells.len() {
        return Err("latency calibration matrix is incomplete or duplicated".into());
    }
    for (card_id, thread_point, _) in &cells {
        let calibration = calibrations
            .get(&(card_id.as_str(), thread_point.name()))
            .ok_or("latency calibration matrix is missing a declared cell")?;
        let cell = ScenarioCell::new(
            *card_id,
            *thread_point,
            topology,
            calibration.transactions_per_worker,
            1,
        )
        .map_err(|error| error.to_string())?;
        let expected_operations = card(*card_id)
            .operation_count(&cell.expected_counts().map_err(|error| error.to_string())?);
        if calibration.thread_count != cell.threads as u32
            || calibration.transactions_per_worker == 0
            || calibration.operation_count != expected_operations
            || calibration.elapsed_ns == 0
        {
            return Err("latency calibration contradicts its declared workload".into());
        }
    }

    let mut grouped: BTreeMap<(String, String, String), Vec<&LatencyRawSample>> = BTreeMap::new();
    let mut common_clock: Option<LatencyClock> = None;
    for sample in &raw.samples {
        if sample.metric_schema_version != LATENCY_SCHEMA_VERSION
            || sample.ordinal >= ALLOCATOR_IDS.len() as u8
            || sample.workload_seed == 0
        {
            return Err("latency sample has invalid identity".into());
        }
        let key = format!("{}/{}", sample.scenario_id, sample.thread_point);
        if !expected_cells.contains(&key) {
            return Err("latency sample names an undeclared cell".into());
        }
        let allocator = allocators
            .get(sample.allocator_id.as_str())
            .ok_or("latency sample names an undeclared allocator")?;
        let calibration = calibrations
            .get(&(sample.scenario_id.as_str(), sample.thread_point.as_str()))
            .ok_or("latency sample has no matching calibration")?;
        let card_id = CardId::parse(&sample.scenario_id).ok_or("unknown latency card")?;
        let thread_point =
            ThreadPoint::parse(&sample.thread_point).ok_or("unknown latency thread point")?;
        let cell = ScenarioCell::new(
            card_id,
            thread_point,
            topology,
            calibration.transactions_per_worker,
            sample.workload_seed,
        )
        .map_err(|error| error.to_string())?;
        let expected_schedule = all_sample_indices(&cell, sample.sample_denominator)?;
        let expected_checksum = expected_touch_checksum(&cell)?;
        if raw.sampling_denominators.get(&key) != Some(&sample.sample_denominator)
            || sample.allocator_source_sha != allocator.source_sha
            || sample.child_binary_sha256 != allocator.child_binary_sha256
            || sample.thread_count != cell.threads as u32
            || sample.transaction_definition != transaction_definition(card_id)
            || sample.measured.protocol_version != LATENCY_CHILD_PROTOCOL_VERSION
            || sample.control.protocol_version != LATENCY_CHILD_PROTOCOL_VERSION
            || sample.measured.metric_schema_version != LATENCY_SCHEMA_VERSION
            || sample.control.metric_schema_version != LATENCY_SCHEMA_VERSION
            || sample.measured.control
            || !sample.control.control
            || sample.measured.completed_transactions != cell.requested_transactions()
            || sample.control.completed_transactions != cell.requested_transactions()
            || sample.measured.checksum != expected_checksum
            || sample.control.checksum != 1
        {
            return Err("latency sample contradicts protocol metadata".into());
        }
        let measured_schedule = sample
            .measured
            .observations
            .iter()
            .map(|value| (value.thread_index, value.transaction_index))
            .collect::<Vec<_>>();
        let control_schedule = sample
            .control
            .observations
            .iter()
            .map(|value| (value.thread_index, value.transaction_index))
            .collect::<Vec<_>>();
        if measured_schedule != expected_schedule
            || control_schedule != expected_schedule
            || sample
                .measured
                .observations
                .iter()
                .chain(&sample.control.observations)
                .any(|value| value.duration_ns == 0)
        {
            return Err(
                "latency measured/control schedules differ or contain zero duration".into(),
            );
        }
        if sample.measured.scheduling.clock != sample.control.scheduling.clock
            || sample.measured.scheduling.affinity_policy
                != sample.control.scheduling.affinity_policy
            || sample.measured.scheduling.thread_count != sample.thread_count
            || sample.control.scheduling.thread_count != sample.thread_count
            || sample.measured.scheduling.physical_cores != raw.runner.physical_cores
            || sample.control.scheduling.physical_cores != raw.runner.physical_cores
            || sample.measured.scheduling.logical_cores != raw.runner.logical_cores
            || sample.control.scheduling.logical_cores != raw.runner.logical_cores
            || sample.measured.scheduling.runner_class != raw.runner.runner_class
            || sample.control.scheduling.runner_class != raw.runner.runner_class
            || sample.measured.scheduling.affinity_policy != raw.runner.affinity.policy
            || sample.measured.scheduling.actual_cpu_ids.len() != cell.threads
            || sample.control.scheduling.actual_cpu_ids.len() != cell.threads
            || sample.measured.scheduling.clock.source.is_empty()
            || sample.measured.scheduling.clock.implementation.is_empty()
            || sample.measured.scheduling.clock.resolution_ns == 0
        {
            return Err("latency control uses incompatible scheduling or clock metadata".into());
        }
        if raw.runner.affinity.policy == "pinned"
            && sample
                .measured
                .scheduling
                .actual_cpu_ids
                .iter()
                .chain(&sample.control.scheduling.actual_cpu_ids)
                .any(|cpu| {
                    cpu.is_none()
                        || !raw
                            .runner
                            .affinity
                            .logical_cpu_ids
                            .contains(&cpu.unwrap_or_default())
                })
        {
            return Err("latency pinned affinity was not observed on an allowed CPU".into());
        }
        match &common_clock {
            Some(clock) if clock != &sample.measured.scheduling.clock => {
                return Err("latency run mixes incompatible clocks".into());
            }
            None => common_clock = Some(sample.measured.scheduling.clock.clone()),
            _ => {}
        }
        grouped
            .entry((
                sample.scenario_id.clone(),
                sample.thread_point.clone(),
                sample.allocator_id.clone(),
            ))
            .or_default()
            .push(sample);
    }
    let mut common_blocks: Option<BTreeSet<u32>> = None;
    for (card, point, _) in cells {
        let mut reference_seeds: BTreeMap<u32, (u64, Vec<(u32, u64)>)> = BTreeMap::new();
        let mut cell_blocks: Option<BTreeSet<u32>> = None;
        for allocator in ALLOCATOR_IDS {
            let key = (
                card.as_str().to_string(),
                point.name().to_string(),
                allocator.to_string(),
            );
            let samples = grouped.get(&key).ok_or_else(|| {
                format!(
                    "missing latency allocator/cell {allocator} {}/{}",
                    card.as_str(),
                    point.name()
                )
            })?;
            if samples.len() < LATENCY_MIN_BLOCKS as usize {
                return Err(
                    "every latency allocator/cell requires at least 15 complete blocks".into(),
                );
            }
            let blocks = samples
                .iter()
                .map(|value| value.block_id)
                .collect::<BTreeSet<_>>();
            if blocks.len() != samples.len() {
                return Err("duplicate latency allocator/cell block".into());
            }
            match &cell_blocks {
                Some(expected) if expected != &blocks => {
                    return Err("latency allocators do not share an exact block set".into());
                }
                None => cell_blocks = Some(blocks.clone()),
                _ => {}
            }
            let measured_count: usize = samples
                .iter()
                .map(|value| value.measured.observations.len())
                .sum();
            let control_count: usize = samples
                .iter()
                .map(|value| value.control.observations.len())
                .sum();
            if measured_count < LATENCY_MIN_SAMPLES || control_count < LATENCY_MIN_SAMPLES {
                return Err(
                    "every latency allocator/cell requires 10000 measured and control samples"
                        .into(),
                );
            }
            for sample in samples {
                let schedule = sample
                    .measured
                    .observations
                    .iter()
                    .map(|value| (value.thread_index, value.transaction_index))
                    .collect::<Vec<_>>();
                match reference_seeds.get(&sample.block_id) {
                    Some((seed, expected))
                        if *seed != sample.workload_seed || *expected != schedule =>
                    {
                        return Err(
                            "latency sample schedule must match across allocators within a block"
                                .into(),
                        );
                    }
                    None => {
                        reference_seeds.insert(sample.block_id, (sample.workload_seed, schedule));
                    }
                    _ => {}
                }
            }
            let measured = samples
                .iter()
                .flat_map(|value| {
                    value
                        .measured
                        .observations
                        .iter()
                        .map(|observation| observation.duration_ns)
                })
                .collect::<Vec<_>>();
            let control = samples
                .iter()
                .flat_map(|value| {
                    value
                        .control
                        .observations
                        .iter()
                        .map(|observation| observation.duration_ns)
                })
                .collect::<Vec<_>>();
            if !overhead_is_valid(
                &summarize_latency(&measured)?,
                &summarize_latency(&control)?,
            ) {
                return Err(format!(
                    "latency overhead threshold failed for {allocator} {}/{}",
                    card.as_str(),
                    point.name()
                ));
            }
        }
        let blocks = cell_blocks.ok_or("latency cell has no blocks")?;
        match &common_blocks {
            Some(expected) if expected != &blocks => {
                return Err("latency cells do not share an exact block set".into());
            }
            None => common_blocks = Some(blocks.clone()),
            _ => {}
        }
        for block_id in blocks {
            let block = raw
                .samples
                .iter()
                .filter(|sample| {
                    sample.scenario_id == card.as_str()
                        && sample.thread_point == point.name()
                        && sample.block_id == block_id
                })
                .collect::<Vec<_>>();
            if block.len() != ALLOCATOR_IDS.len()
                || block
                    .iter()
                    .map(|sample| sample.allocator_id.as_str())
                    .collect::<BTreeSet<_>>()
                    != ALLOCATOR_IDS.into_iter().collect()
                || block
                    .iter()
                    .map(|sample| sample.ordinal)
                    .collect::<BTreeSet<_>>()
                    != (0_u8..ALLOCATOR_IDS.len() as u8).collect()
            {
                return Err("latency block is not an exact five-allocator permutation".into());
            }
        }
    }
    let blocks = common_blocks.ok_or("latency run contains no complete blocks")?;
    if blocks != (0..blocks.len() as u32).collect::<BTreeSet<_>>() {
        return Err("latency block IDs must be contiguous from zero".into());
    }
    let orders = balanced_block_orders(blocks.len() as u32, raw.run_seed)?;
    for order in orders {
        for sample in raw
            .samples
            .iter()
            .filter(|sample| sample.block_id == order.block_id)
        {
            if sample.workload_seed != order.workload_seed
                || order.allocator_ids.get(sample.ordinal as usize) != Some(&sample.allocator_id)
            {
                return Err("latency block order or seed differs from the paired protocol".into());
            }
        }
    }
    if raw.samples.len() != expected_cells.len() * ALLOCATOR_IDS.len() * blocks.len() {
        return Err("latency raw sample matrix contains missing or extra records".into());
    }
    Ok(())
}

pub fn build_latency_report(raw: &LatencyRawRun) -> Result<LatencyMetricReport, String> {
    validate_latency_raw_run(raw)?;
    let topology = Topology {
        physical_cores: raw.runner.physical_cores as usize,
        logical_cores: raw.runner.logical_cores as usize,
    };
    let mut absolute_summaries = Vec::new();
    let mut block_summaries = Vec::new();
    let mut paired_summaries = Vec::new();
    for (card, point, definition) in latency_scenario_cells(topology)? {
        for allocator in ALLOCATOR_IDS {
            let samples = raw
                .samples
                .iter()
                .filter(|sample| {
                    sample.scenario_id == card.as_str()
                        && sample.thread_point == point.name()
                        && sample.allocator_id == allocator
                })
                .collect::<Vec<_>>();
            let measured = samples
                .iter()
                .flat_map(|sample| {
                    sample
                        .measured
                        .observations
                        .iter()
                        .map(|value| value.duration_ns)
                })
                .collect::<Vec<_>>();
            let control = samples
                .iter()
                .flat_map(|sample| {
                    sample
                        .control
                        .observations
                        .iter()
                        .map(|value| value.duration_ns)
                })
                .collect::<Vec<_>>();
            let measured_summary = summarize_latency(&measured)?;
            let control_summary = summarize_latency(&control)?;
            absolute_summaries.push(LatencyAbsoluteSummary {
                allocator_id: allocator.into(),
                scenario_id: card.as_str().into(),
                thread_point: point.name().into(),
                transaction_definition: definition.into(),
                overhead_valid: overhead_is_valid(&measured_summary, &control_summary),
                measured: measured_summary,
                control: control_summary,
            });
            for sample in samples {
                block_summaries.push(LatencyBlockSummary {
                    block_id: sample.block_id,
                    allocator_id: allocator.into(),
                    scenario_id: card.as_str().into(),
                    thread_point: point.name().into(),
                    measured: summarize_latency(
                        &sample
                            .measured
                            .observations
                            .iter()
                            .map(|value| value.duration_ns)
                            .collect::<Vec<_>>(),
                    )?,
                    control: summarize_latency(
                        &sample
                            .control
                            .observations
                            .iter()
                            .map(|value| value.duration_ns)
                            .collect::<Vec<_>>(),
                    )?,
                });
            }
            if allocator != REFERENCE_ALLOCATOR {
                for (name, summary) in block_bootstrap_quantile_effects(
                    raw.run_seed,
                    &format!("{}/{}", card.as_str(), point.name()),
                    allocator,
                    REFERENCE_ALLOCATOR,
                    &raw.samples,
                )? {
                    paired_summaries.push(LatencyPairedSummary {
                        scenario_id: card.as_str().into(),
                        thread_point: point.name().into(),
                        quantile: name.into(),
                        summary,
                    });
                }
            }
        }
    }
    Ok(LatencyMetricReport {
        metric_schema_version: LATENCY_SCHEMA_VERSION.into(),
        status: "complete".into(),
        invalid_reason: None,
        metric_comparison_key: latency_comparison_key(raw)?,
        run: raw.run.clone(),
        runner: raw.runner.clone(),
        direction: MetricDirection::LowerIsBetter,
        informational: true,
        sampling_denominators: raw.sampling_denominators.clone(),
        methodology: methodology(),
        absolute_summaries,
        paired_summaries,
        block_summaries,
        raw_samples: raw.samples.clone(),
    })
}

pub fn attach_latency_report(
    latest: &mut LatestReport,
    report: LatencyMetricReport,
) -> Result<(), String> {
    validate_latency_report(&report)?;
    let expected = latest
        .allocators
        .iter()
        .map(|value| (&value.allocator_id, &value.source_sha))
        .collect::<BTreeSet<_>>();
    let observed = report
        .raw_samples
        .iter()
        .map(|value| (&value.allocator_id, &value.allocator_source_sha))
        .collect::<BTreeSet<_>>();
    if expected != observed {
        return Err("latency allocator provenance differs from core latest".into());
    }
    latest.latency = Some(report);
    latest
        .pending_metrics
        .retain(|value| value.metric_id != "latency");
    Ok(())
}

pub fn validate_latency_report(report: &LatencyMetricReport) -> Result<(), String> {
    if report.status != "complete"
        || report.invalid_reason.is_some()
        || report.metric_schema_version != LATENCY_SCHEMA_VERSION
        || report.direction != MetricDirection::LowerIsBetter
        || !report.informational
        || report.metric_comparison_key.len() != 64
        || !report
            .metric_comparison_key
            .bytes()
            .all(|value| value.is_ascii_digit() || (b'a'..=b'f').contains(&value))
        || report.methodology != methodology()
    {
        return Err("only a complete validated latency report can replace pending".into());
    }
    let topology = Topology {
        physical_cores: report.runner.physical_cores as usize,
        logical_cores: report.runner.logical_cores as usize,
    };
    let cells = latency_scenario_cells(topology)?;
    let expected_cells = cells
        .iter()
        .map(|(card, point, _)| format!("{}/{}", card.as_str(), point.name()))
        .collect::<BTreeSet<_>>();
    if report
        .sampling_denominators
        .keys()
        .cloned()
        .collect::<BTreeSet<_>>()
        != expected_cells
        || report
            .sampling_denominators
            .values()
            .any(|value| *value == 0 || *value > LATENCY_INITIAL_SAMPLE_DENOMINATOR)
    {
        return Err("latency report sampling-rate matrix is invalid".into());
    }
    if report.absolute_summaries.len() != cells.len() * ALLOCATOR_IDS.len()
        || report.paired_summaries.len() != cells.len() * (ALLOCATOR_IDS.len() - 1) * 3
        || report.raw_samples.is_empty()
        || report.block_summaries.len() != report.raw_samples.len()
    {
        return Err("latency report summary matrix is incomplete".into());
    }
    for sample in &report.raw_samples {
        let key = format!("{}/{}", sample.scenario_id, sample.thread_point);
        let measured_schedule = sample
            .measured
            .observations
            .iter()
            .map(|value| (value.thread_index, value.transaction_index))
            .collect::<Vec<_>>();
        let control_schedule = sample
            .control
            .observations
            .iter()
            .map(|value| (value.thread_index, value.transaction_index))
            .collect::<Vec<_>>();
        if sample.metric_schema_version != LATENCY_SCHEMA_VERSION
            || !expected_cells.contains(&key)
            || !ALLOCATOR_IDS.contains(&sample.allocator_id.as_str())
            || sample.ordinal >= ALLOCATOR_IDS.len() as u8
            || sample.workload_seed == 0
            || report.sampling_denominators.get(&key) != Some(&sample.sample_denominator)
            || sample.measured.protocol_version != LATENCY_CHILD_PROTOCOL_VERSION
            || sample.control.protocol_version != LATENCY_CHILD_PROTOCOL_VERSION
            || sample.measured.metric_schema_version != LATENCY_SCHEMA_VERSION
            || sample.control.metric_schema_version != LATENCY_SCHEMA_VERSION
            || sample.measured.control
            || !sample.control.control
            || measured_schedule != control_schedule
            || measured_schedule.windows(2).any(|pair| pair[0] >= pair[1])
            || sample
                .measured
                .observations
                .iter()
                .chain(&sample.control.observations)
                .any(|value| value.duration_ns == 0 || value.thread_index >= sample.thread_count)
            || sample.measured.scheduling.clock != sample.control.scheduling.clock
            || sample.measured.scheduling.thread_count != sample.thread_count
            || sample.control.scheduling.thread_count != sample.thread_count
            || sample.measured.scheduling.physical_cores != report.runner.physical_cores
            || sample.measured.scheduling.logical_cores != report.runner.logical_cores
            || sample.measured.scheduling.runner_class != report.runner.runner_class
            || sample.measured.scheduling.affinity_policy != report.runner.affinity.policy
            || sample.measured.scheduling.actual_cpu_ids.len() != sample.thread_count as usize
            || sample.control.scheduling.actual_cpu_ids.len() != sample.thread_count as usize
            || sample.measured.scheduling.clock.resolution_ns == 0
        {
            return Err("latency report raw evidence contradicts its protocol".into());
        }
    }
    let mut common_blocks: Option<BTreeSet<u32>> = None;
    for (card, point, definition) in cells {
        let cell_key = format!("{}/{}", card.as_str(), point.name());
        let cell_blocks = report
            .raw_samples
            .iter()
            .filter(|value| {
                value.scenario_id == card.as_str() && value.thread_point == point.name()
            })
            .map(|value| value.block_id)
            .collect::<BTreeSet<_>>();
        if cell_blocks.len() < LATENCY_MIN_BLOCKS as usize {
            return Err("latency report is missing complete raw blocks".into());
        }
        match &common_blocks {
            Some(expected) if expected != &cell_blocks => {
                return Err("latency report cells do not share an exact block set".into());
            }
            None => common_blocks = Some(cell_blocks.clone()),
            _ => {}
        }
        for block_id in &cell_blocks {
            let block = report
                .raw_samples
                .iter()
                .filter(|value| {
                    value.scenario_id == card.as_str()
                        && value.thread_point == point.name()
                        && value.block_id == *block_id
                })
                .collect::<Vec<_>>();
            if block.len() != ALLOCATOR_IDS.len()
                || block
                    .iter()
                    .map(|value| value.allocator_id.as_str())
                    .collect::<BTreeSet<_>>()
                    != ALLOCATOR_IDS.into_iter().collect()
                || block
                    .iter()
                    .map(|value| value.ordinal)
                    .collect::<BTreeSet<_>>()
                    != (0_u8..ALLOCATOR_IDS.len() as u8).collect()
                || block
                    .iter()
                    .map(|value| value.workload_seed)
                    .collect::<BTreeSet<_>>()
                    .len()
                    != 1
            {
                return Err("latency report raw block is not a paired permutation".into());
            }
        }
        for allocator in ALLOCATOR_IDS {
            let raw_samples = report
                .raw_samples
                .iter()
                .filter(|value| {
                    value.scenario_id == card.as_str()
                        && value.thread_point == point.name()
                        && value.allocator_id == allocator
                })
                .collect::<Vec<_>>();
            if raw_samples.len() != cell_blocks.len()
                || raw_samples
                    .iter()
                    .map(|value| value.block_id)
                    .collect::<BTreeSet<_>>()
                    != cell_blocks
            {
                return Err("latency report raw allocator/block matrix is incomplete".into());
            }
            let measured = raw_samples
                .iter()
                .flat_map(|value| {
                    value
                        .measured
                        .observations
                        .iter()
                        .map(|item| item.duration_ns)
                })
                .collect::<Vec<_>>();
            let control = raw_samples
                .iter()
                .flat_map(|value| {
                    value
                        .control
                        .observations
                        .iter()
                        .map(|item| item.duration_ns)
                })
                .collect::<Vec<_>>();
            let expected_measured = summarize_latency(&measured)?;
            let expected_control = summarize_latency(&control)?;
            let absolute = report
                .absolute_summaries
                .iter()
                .filter(|value| {
                    value.scenario_id == card.as_str()
                        && value.thread_point == point.name()
                        && value.allocator_id == allocator
                })
                .collect::<Vec<_>>();
            if absolute.len() != 1
                || absolute[0].transaction_definition != definition
                || !absolute[0].overhead_valid
                || absolute[0].measured != expected_measured
                || absolute[0].control != expected_control
                || !overhead_is_valid(&expected_measured, &expected_control)
                || absolute[0].measured.count < LATENCY_MIN_SAMPLES as u64
                || absolute[0].control.count < LATENCY_MIN_SAMPLES as u64
                || absolute[0].measured.zero_count != 0
                || absolute[0].control.zero_count != 0
            {
                return Err(
                    "latency report has incomplete allocator/control/sample evidence".into(),
                );
            }
            let block_count = report
                .block_summaries
                .iter()
                .filter(|value| {
                    value.scenario_id == card.as_str()
                        && value.thread_point == point.name()
                        && value.allocator_id == allocator
                })
                .count();
            if block_count != cell_blocks.len() {
                return Err("latency report is missing complete block summaries".into());
            }
            for raw_sample in raw_samples {
                let block = report
                    .block_summaries
                    .iter()
                    .filter(|value| {
                        value.scenario_id == card.as_str()
                            && value.thread_point == point.name()
                            && value.allocator_id == allocator
                            && value.block_id == raw_sample.block_id
                    })
                    .collect::<Vec<_>>();
                let measured = raw_sample
                    .measured
                    .observations
                    .iter()
                    .map(|value| value.duration_ns)
                    .collect::<Vec<_>>();
                let control = raw_sample
                    .control
                    .observations
                    .iter()
                    .map(|value| value.duration_ns)
                    .collect::<Vec<_>>();
                if block.len() != 1
                    || block[0].measured != summarize_latency(&measured)?
                    || block[0].control != summarize_latency(&control)?
                {
                    return Err("latency block summary differs from raw evidence".into());
                }
            }
            if allocator != REFERENCE_ALLOCATOR {
                for (quantile, probability) in [("p50", 0.50), ("p95", 0.95), ("p99", 0.99)] {
                    let paired = report
                        .paired_summaries
                        .iter()
                        .filter(|value| {
                            value.scenario_id == card.as_str()
                                && value.thread_point == point.name()
                                && value.quantile == quantile
                                && value.summary.candidate_id == allocator
                                && value.summary.reference_id == REFERENCE_ALLOCATOR
                        })
                        .collect::<Vec<_>>();
                    if paired.len() != 1
                        || paired[0].summary.direction != MetricDirection::LowerIsBetter
                        || !paired[0].summary.informational
                        || paired[0].summary.block_count != cell_blocks.len() as u64
                        || paired[0].summary.bootstrap.resample_count != LATENCY_BOOTSTRAP_RESAMPLES
                        || paired[0].summary.bootstrap.prng != BOOTSTRAP_PRNG
                        || paired[0].summary.effect
                            != point_quantile_effect(
                                &cell_key,
                                allocator,
                                REFERENCE_ALLOCATOR,
                                probability,
                                &report.raw_samples,
                            )?
                        || !paired[0].summary.effect.is_finite()
                        || paired[0].summary.effect <= 0.0
                        || !paired[0].summary.confidence_interval.lower.is_finite()
                        || !paired[0].summary.confidence_interval.upper.is_finite()
                        || paired[0].summary.confidence_interval.lower <= 0.0
                        || paired[0].summary.confidence_interval.upper <= 0.0
                        || paired[0].summary.confidence_interval.lower
                            > paired[0].summary.confidence_interval.upper
                        || paired[0].summary.confidence_interval.confidence_level != 0.95
                    {
                        return Err("latency report paired quantile matrix is incomplete".into());
                    }
                }
            }
        }
    }
    let blocks = common_blocks.ok_or("latency report contains no blocks")?;
    if blocks != (0..blocks.len() as u32).collect::<BTreeSet<_>>()
        || report.raw_samples.len() != expected_cells.len() * ALLOCATOR_IDS.len() * blocks.len()
    {
        return Err("latency report contains missing, extra, or noncontiguous blocks".into());
    }
    Ok(())
}

pub fn latency_comparison_key(raw: &LatencyRawRun) -> Result<String, String> {
    #[derive(Serialize)]
    struct Key<'a> {
        schema: &'a str,
        rates: &'a BTreeMap<String, u64>,
        runner_fingerprint: &'a str,
        affinity_policy: &'a str,
        clock: &'a LatencyClock,
        allocator_lock_sha256: &'a str,
        allocator_sources: BTreeMap<&'a str, &'a str>,
        definitions: BTreeMap<&'a str, &'a str>,
    }
    let first = raw
        .samples
        .first()
        .ok_or_else(|| "latency run has no samples".to_string())?;
    let value = Key {
        schema: LATENCY_SCHEMA_VERSION,
        rates: &raw.sampling_denominators,
        runner_fingerprint: &raw.runner.fingerprint_sha256,
        affinity_policy: &first.measured.scheduling.affinity_policy,
        clock: &first.measured.scheduling.clock,
        allocator_lock_sha256: &raw.allocator_lock_sha256,
        allocator_sources: raw
            .allocators
            .iter()
            .map(|value| (value.allocator_id.as_str(), value.source_sha.as_str()))
            .collect(),
        definitions: latency_scenario_cells(Topology {
            physical_cores: raw.runner.physical_cores as usize,
            logical_cores: raw.runner.logical_cores as usize,
        })?
        .into_iter()
        .map(|(card, _, definition)| (card.as_str(), definition))
        .collect(),
    };
    serde_json::to_vec(&value)
        .map(|bytes| sha256_bytes(&bytes))
        .map_err(|error| error.to_string())
}

fn methodology() -> LatencyMethodology {
    LatencyMethodology {
        transaction_boundaries: BTreeMap::from([
            ("local".into(), "allocation plus required touch/checksum plus free".into()),
            ("cross-thread".into(), "producer allocation through consumer free completion, including queue and ownership transfer".into()),
            ("large-object".into(), "allocation plus one-byte-per-page touch/checksum plus free".into()),
        ]),
        quantile_method: "R/NumPy Type 7 linear interpolation".into(),
        sampling_schedule: "deterministic seed-offset 1/N indices, identical across allocators within each block".into(),
        overhead_control: "separate no-op/black-box distribution; never subtracted; control p50 <= 5% measured p50 and measured p99 > 2x control p99".into(),
        bootstrap: "paired lower-is-better log effect; whole blocks resampled and target quantile recomputed from every selected transaction".into(),
        storage_decision: "raw block/thread/index/duration vectors fit the checked public latest size cap and are retained verbatim; history retains summaries only".into(),
        tail_policy: "all scheduler/preemption tails retained; no outlier deletion".into(),
    }
}

pub fn block_bootstrap_quantile_effect(
    run_seed: u64,
    cell_id: &str,
    candidate_id: &str,
    reference_id: &str,
    probability: f64,
    samples: &[LatencyRawSample],
) -> Result<PairedEffectSummary, String> {
    let (cell_id, quantile) = match cell_id.rsplit_once('/') {
        Some((cell, "p50")) => (cell, "p50"),
        Some((cell, "p95")) => (cell, "p95"),
        Some((cell, "p99")) => (cell, "p99"),
        _ if probability == 0.50 => (cell_id, "p50"),
        _ if probability == 0.95 => (cell_id, "p95"),
        _ if probability == 0.99 => (cell_id, "p99"),
        _ => return Err("latency bootstrap supports only p50, p95, and p99".into()),
    };
    if (quantile == "p50" && probability != 0.50)
        || (quantile == "p95" && probability != 0.95)
        || (quantile == "p99" && probability != 0.99)
    {
        return Err("latency bootstrap quantile label and probability differ".into());
    }
    block_bootstrap_quantile_effects(run_seed, cell_id, candidate_id, reference_id, samples)?
        .into_iter()
        .find_map(|(name, summary)| (name == quantile).then_some(summary))
        .ok_or_else(|| "latency bootstrap omitted a required quantile".into())
}

fn latency_block_pairs(
    cell_id: &str,
    candidate_id: &str,
    reference_id: &str,
    samples: &[LatencyRawSample],
) -> Result<Vec<(Vec<u64>, Vec<u64>)>, String> {
    if cell_id.is_empty()
        || candidate_id.is_empty()
        || reference_id.is_empty()
        || candidate_id == reference_id
    {
        return Err("latency bootstrap identity is invalid".into());
    }
    let mut blocks: BTreeMap<u32, BTreeMap<&str, Vec<u64>>> = BTreeMap::new();
    for sample in samples {
        if format!("{}/{}", sample.scenario_id, sample.thread_point) != cell_id {
            continue;
        }
        if sample.allocator_id == candidate_id || sample.allocator_id == reference_id {
            if blocks
                .entry(sample.block_id)
                .or_default()
                .insert(
                    &sample.allocator_id,
                    sample
                        .measured
                        .observations
                        .iter()
                        .map(|value| value.duration_ns)
                        .collect(),
                )
                .is_some()
            {
                return Err("latency bootstrap block duplicates an allocator".into());
            }
        }
    }
    if blocks.is_empty() {
        return Err("latency bootstrap has no blocks".into());
    }
    blocks
        .into_iter()
        .map(|(block, values)| {
            let candidate = values
                .get(candidate_id)
                .ok_or_else(|| format!("block {block} missing candidate"))?;
            let reference = values
                .get(reference_id)
                .ok_or_else(|| format!("block {block} missing reference"))?;
            if candidate.is_empty()
                || reference.is_empty()
                || candidate.iter().chain(reference).any(|value| *value == 0)
            {
                return Err(format!("block {block} has an invalid latency distribution"));
            }
            Ok((candidate.clone(), reference.clone()))
        })
        .collect()
}

fn point_quantile_effect(
    cell_id: &str,
    candidate_id: &str,
    reference_id: &str,
    probability: f64,
    samples: &[LatencyRawSample],
) -> Result<f64, String> {
    let pairs = latency_block_pairs(cell_id, candidate_id, reference_id, samples)?;
    let logs = pairs
        .iter()
        .map(|(candidate, reference)| {
            Ok(
                (quantile_u64(reference, probability)? / quantile_u64(candidate, probability)?)
                    .ln(),
            )
        })
        .collect::<Result<Vec<_>, String>>()?;
    crate::stats::type7_quantile(&logs, 0.5)
        .map_err(|error| error.to_string())
        .map(f64::exp)
}

fn block_bootstrap_quantile_effects(
    run_seed: u64,
    cell_id: &str,
    candidate_id: &str,
    reference_id: &str,
    samples: &[LatencyRawSample],
) -> Result<Vec<(&'static str, PairedEffectSummary)>, String> {
    let blocks = latency_block_pairs(cell_id, candidate_id, reference_id, samples)?;
    let probabilities = [0.50, 0.95, 0.99];
    let mut point_logs: [Vec<f64>; 3] = std::array::from_fn(|_| Vec::with_capacity(blocks.len()));
    for (candidate, reference) in &blocks {
        for (index, probability) in probabilities.into_iter().enumerate() {
            point_logs[index].push(
                (quantile_u64(reference, probability)? / quantile_u64(candidate, probability)?)
                    .ln(),
            );
        }
    }
    let mut point_effects = [0.0; 3];
    for (index, logs) in point_logs.iter().enumerate() {
        point_effects[index] = crate::stats::type7_quantile(logs, 0.5)
            .map_err(|error| error.to_string())?
            .exp();
    }

    let mut candidate_sorted = blocks
        .iter()
        .enumerate()
        .flat_map(|(block, (candidate, _))| {
            candidate.iter().copied().map(move |value| (value, block))
        })
        .collect::<Vec<_>>();
    let mut reference_sorted = blocks
        .iter()
        .enumerate()
        .flat_map(|(block, (_, reference))| {
            reference.iter().copied().map(move |value| (value, block))
        })
        .collect::<Vec<_>>();
    candidate_sorted.sort_unstable();
    reference_sorted.sort_unstable();

    let seed_material =
        format!("latency-bootstrap-v2\0{run_seed}\0{cell_id}\0{candidate_id}\0{reference_id}");
    let digest = sha256_bytes(seed_material.as_bytes());
    let seed = u64::from_str_radix(&digest[..16], 16).map_err(|error| error.to_string())?;
    let mut rng = SplitMix64 { state: seed };
    let mut effects: [Vec<f64>; 3] =
        std::array::from_fn(|_| Vec::with_capacity(LATENCY_BOOTSTRAP_RESAMPLES as usize));
    let mut multiplicities = vec![0_u32; blocks.len()];
    for _ in 0..LATENCY_BOOTSTRAP_RESAMPLES {
        multiplicities.fill(0);
        for _ in 0..blocks.len() {
            let selected = rng.uniform_below(blocks.len() as u64) as usize;
            multiplicities[selected] += 1;
        }
        let candidate_quantiles = weighted_type7_three(&candidate_sorted, &multiplicities)?;
        let reference_quantiles = weighted_type7_three(&reference_sorted, &multiplicities)?;
        for index in 0..3 {
            effects[index].push((reference_quantiles[index] / candidate_quantiles[index]).ln());
        }
    }
    for values in &mut effects {
        values.sort_by(f64::total_cmp);
    }
    Ok(["p50", "p95", "p99"]
        .into_iter()
        .enumerate()
        .map(|(index, name)| {
            (
                name,
                PairedEffectSummary {
                    candidate_id: candidate_id.into(),
                    reference_id: reference_id.into(),
                    direction: MetricDirection::LowerIsBetter,
                    block_count: blocks.len() as u64,
                    effect: point_effects[index],
                    confidence_interval: ConfidenceInterval {
                        lower: type7_sorted(&effects[index], 0.025).exp(),
                        upper: type7_sorted(&effects[index], 0.975).exp(),
                        confidence_level: 0.95,
                    },
                    bootstrap: BootstrapMetadata {
                        seed,
                        resample_count: LATENCY_BOOTSTRAP_RESAMPLES,
                        method: "percentile-whole-block-transaction-quantile-type7-v1".into(),
                        prng: BOOTSTRAP_PRNG.into(),
                    },
                    informational: true,
                },
            )
        })
        .collect())
}

fn weighted_type7_three(
    sorted: &[(u64, usize)],
    multiplicities: &[u32],
) -> Result<[f64; 3], String> {
    let total = sorted.iter().try_fold(0_u64, |total, (_, block)| {
        total
            .checked_add(u64::from(*multiplicities.get(*block).ok_or(
                "latency bootstrap block index is outside the multiplicity vector",
            )?))
            .ok_or("latency bootstrap weighted sample count overflowed")
    })?;
    if total == 0 {
        return Err("latency bootstrap selected no observations".into());
    }
    let probabilities = [0.50, 0.95, 0.99];
    let mut ranks = [0_u64; 6];
    let mut fractions = [0.0; 3];
    for (index, probability) in probabilities.into_iter().enumerate() {
        let position = (total - 1) as f64 * probability;
        ranks[index * 2] = position.floor() as u64;
        ranks[index * 2 + 1] = position.ceil() as u64;
        fractions[index] = position - position.floor();
    }
    let mut values = [0_u64; 6];
    let mut rank_cursor = 0;
    let mut cumulative = 0_u64;
    for (value, block) in sorted {
        let weight = u64::from(multiplicities[*block]);
        let next = cumulative + weight;
        while rank_cursor < ranks.len() && ranks[rank_cursor] < next {
            values[rank_cursor] = *value;
            rank_cursor += 1;
        }
        cumulative = next;
        if rank_cursor == ranks.len() {
            break;
        }
    }
    if rank_cursor != ranks.len() {
        return Err("latency bootstrap could not resolve a weighted quantile".into());
    }
    Ok(std::array::from_fn(|index| {
        let lower = values[index * 2] as f64;
        let upper = values[index * 2 + 1] as f64;
        lower + fractions[index] * (upper - lower)
    }))
}

fn quantile_u64(values: &[u64], probability: f64) -> Result<f64, String> {
    crate::stats::type7_quantile(
        &values.iter().map(|value| *value as f64).collect::<Vec<_>>(),
        probability,
    )
    .map_err(|error| error.to_string())
}

fn type7_sorted(values: &[f64], probability: f64) -> f64 {
    if values.len() == 1 {
        return values[0];
    }
    let index = (values.len() - 1) as f64 * probability;
    let lower = index.floor() as usize;
    values[lower]
        + (index - lower as f64) * (values[(lower + 1).min(values.len() - 1)] - values[lower])
}

fn splitmix64(mut value: u64) -> u64 {
    value = value.wrapping_add(0x9e37_79b9_7f4a_7c15);
    value = (value ^ (value >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
    value = (value ^ (value >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
    value ^ (value >> 31)
}

struct SplitMix64 {
    state: u64,
}
impl SplitMix64 {
    fn next(&mut self) -> u64 {
        self.state = self.state.wrapping_add(0x9e37_79b9_7f4a_7c15);
        let mut value = self.state;
        value = (value ^ (value >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
        value = (value ^ (value >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
        value ^ (value >> 31)
    }
    fn uniform_below(&mut self, bound: u64) -> u64 {
        let zone = u64::MAX - u64::MAX % bound;
        loop {
            let value = self.next();
            if value < zone {
                return value % bound;
            }
        }
    }
}
