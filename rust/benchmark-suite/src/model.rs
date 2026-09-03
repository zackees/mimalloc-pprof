use serde::{Deserialize, Serialize};

use crate::{CORE_SUITE_VERSION, RAW_SCHEMA_VERSION};

pub const CHILD_PROTOCOL_VERSION: &str = "benchmark-child-v1";
pub const VALIDATOR_VERSION: &str = "benchmark-validator-v1";
pub const LATEST_SCHEMA_VERSION: &str = "benchmark-latest-v1";
pub const HISTORY_SCHEMA_VERSION: &str = "benchmark-history-v1";
pub const STATISTICS_VERSION: &str = "paired-log-median-bootstrap-v1";

/// Strict publication input. `RawRun` intentionally remains the small Phase 2
/// producer value so existing child/controller APIs stay source-compatible.
/// Publication must use this envelope; a bare or aggregate-only `RawRun` is
/// never sufficient evidence for a headline result.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct PublicationRawRun {
    pub schema_version: String,
    pub suite_version: String,
    pub run_kind: String,
    pub execution_mode: String,
    pub run_seed: u64,
    pub run: RunIdentity,
    pub runner: PublicationRunner,
    pub allocator_lock_sha256: String,
    pub allocators: Vec<AllocatorBuildIdentity>,
    pub calibrations: Vec<CellCalibration>,
    pub samples: Vec<RawSample>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RunIdentity {
    pub source_repository: String,
    pub source_sha: String,
    pub source_ref: String,
    /// `github-actions` or `local`; the paired run ID remains explicit in both
    /// cases so two attempts can never be conflated.
    pub run_origin: String,
    pub run_id: String,
    pub run_attempt: u32,
    pub generated_at_utc: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct PublicationRunner {
    pub runner_class: String,
    pub stable_host_id: String,
    pub fingerprint_sha256: String,
    pub cpu_model: String,
    pub os: String,
    pub os_image: String,
    pub os_version: String,
    pub kernel: String,
    pub architecture: String,
    pub physical_cores: u32,
    pub logical_cores: u32,
    pub target: String,
    pub rustc: String,
    pub affinity: AffinityMetadata,
    pub power: PowerMetadata,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct AffinityMetadata {
    /// `unrestricted` or `pinned`.
    pub policy: String,
    pub logical_cpu_ids: Vec<u32>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct PowerMetadata {
    /// Non-empty observed values; `not-observable` is an explicit value, not
    /// missing data.
    pub governor: String,
    pub boost: String,
    pub frequency_policy: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum FeatureState {
    Enabled,
    Disabled,
    NotApplicable,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct AllocatorFeatureOptions {
    pub pprof_compiled: FeatureState,
    pub pprof_runtime: FeatureState,
    pub memory_events_compiled: FeatureState,
    pub memory_events_runtime: FeatureState,
    pub frame_pointers: FeatureState,
    pub opt_arch: FeatureState,
    pub opt_simd: FeatureState,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SourcePatchIdentity {
    pub file: String,
    pub sha256: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct AllocatorBuildIdentity {
    pub allocator_id: String,
    pub allocator_version: String,
    pub source_kind: String,
    pub canonical_repository: String,
    pub source_sha: String,
    /// Exact URL/hash for archive inputs, or the literal `not-applicable` for
    /// the workflow checkout. This makes absence explicit in JSON.
    pub source_archive_url: String,
    pub source_archive_sha256: String,
    pub source_tree_sha256: String,
    pub source_patches: Vec<SourcePatchIdentity>,
    pub build_system: String,
    pub build_commands: Vec<Vec<String>>,
    pub build_flags: Vec<String>,
    pub compiler: String,
    pub linker: String,
    pub static_library_sha256: String,
    pub child_binary_sha256: String,
    pub options: AllocatorFeatureOptions,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CellCalibration {
    pub scenario_id: String,
    pub thread_point: String,
    pub thread_count: u32,
    pub transactions_per_worker: u64,
    pub warmup_transactions_per_worker: u64,
    pub operation_count: u64,
    pub elapsed_ns: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ValidationReport {
    pub validator_version: String,
    pub status: String,
    pub headline_eligible: bool,
    pub sample_count: u64,
    pub cell_count: u32,
    pub minimum_blocks_per_cell: u32,
    pub allocator_ids: Vec<String>,
    pub checks: Vec<String>,
    pub errors: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct AbsoluteCellSummary {
    pub scenario_id: String,
    pub thread_point: String,
    pub metric_id: String,
    pub direction: crate::stats::MetricDirection,
    pub allocator_id: String,
    pub summary: crate::stats::AbsoluteSummary,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct PairedCellSummary {
    pub scenario_id: String,
    pub thread_point: String,
    pub metric_id: String,
    pub summary: crate::stats::PairedEffectSummary,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct MethodologyContract {
    pub absolute_summary: String,
    pub paired_effect: String,
    pub confidence_interval: String,
    pub quantile_method: String,
    pub noise_threshold_relative_iqr: f64,
    pub informational: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct PendingMetric {
    pub metric_id: String,
    pub status: String,
    pub reason: String,
    pub phase_issue_url: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CanonicalUrls {
    pub pages: String,
    pub stats_branch: String,
    pub latest_json: String,
    pub methodology: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct LatestReport {
    pub latest_schema_version: String,
    pub raw_schema_version: String,
    pub statistics_version: String,
    pub suite_version: String,
    pub validation_report: ValidationReport,
    pub run: RunIdentity,
    pub runner: PublicationRunner,
    pub allocators: Vec<AllocatorBuildIdentity>,
    pub calibrations: Vec<CellCalibration>,
    pub raw_samples: Vec<RawSample>,
    pub absolute_summaries: Vec<AbsoluteCellSummary>,
    pub paired_summaries: Vec<PairedCellSummary>,
    pub comparison_key: String,
    pub methodology: MethodologyContract,
    pub pending_metrics: Vec<PendingMetric>,
    /// Backward-compatible Phase 6 extension. Absence means the metric was not
    /// collected under `linux-process-memory-v1`; it never means zero.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub memory: Option<crate::memory::MemoryMetricReport>,
    /// Backward-compatible Phase 6 extension. Absence means no validated
    /// `transaction-latency-v1` collection is available; it never means zero.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub latency: Option<crate::latency::LatencyMetricReport>,
    /// Backward-compatible Phase 6 extension. Absence means no validated
    /// `throughput-scaling-sparse-v1` sweep is available; it never means zero.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub scaling: Option<crate::scaling::ScalingMetricReport>,
    pub canonical_urls: CanonicalUrls,
    pub reproduction_command: String,
    pub actions_run_url: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct HistoryRow {
    pub history_schema_version: String,
    pub statistics_version: String,
    pub suite_version: String,
    pub run: RunIdentity,
    pub comparison_key: String,
    pub runner: PublicationRunner,
    pub allocator_identities: Vec<AllocatorIdentity>,
    pub absolute_summaries: Vec<AbsoluteCellSummary>,
    pub paired_summaries: Vec<PairedCellSummary>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub memory: Option<crate::memory::MemoryHistoryReport>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub latency: Option<crate::latency::LatencyHistoryReport>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub scaling: Option<crate::scaling::ScalingHistoryReport>,
}

/// Immutable identity supplied by the producer after it hashes the directly
/// linked artifacts. The child echoes these values into every raw record.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct AllocatorIdentity {
    pub allocator_id: String,
    pub allocator_version: String,
    pub source_sha: String,
    pub library_sha256: String,
    pub child_binary_sha256: String,
}

impl AllocatorIdentity {
    pub fn validate(&self) -> Result<(), String> {
        if self.allocator_id.is_empty() || self.allocator_version.is_empty() {
            return Err("allocator identity is incomplete".into());
        }
        if !is_lower_hex(&self.source_sha, 40)
            || !is_lower_hex(&self.library_sha256, 64)
            || !is_lower_hex(&self.child_binary_sha256, 64)
        {
            return Err("allocator identity contains an invalid digest".into());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RunnerMetadata {
    pub os: String,
    pub architecture: String,
    pub physical_cores: u32,
    pub logical_cores: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ToolchainMetadata {
    pub rustc: String,
    pub target: String,
    pub compiler: String,
    pub linker: String,
}

/// Versioned, strict request consumed by one freshly spawned benchmark child.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct BenchmarkChildRequest {
    pub protocol_version: String,
    pub schema_version: String,
    pub suite_version: String,
    /// `headline` for publishable paired cells, `reduced-smoke` only for the
    /// explicit one-block Linux acceptance probe.
    pub run_kind: String,
    pub execution_mode: String,
    pub run_seed: u64,
    pub block_id: u32,
    pub ordinal: u8,
    pub workload_seed: u64,
    pub allocator: AllocatorIdentity,
    pub scenario_id: String,
    pub scenario_version: String,
    pub thread_point: String,
    pub physical_cores: u32,
    pub logical_cores: u32,
    pub transactions_per_worker: u64,
    pub warmup_transactions_per_worker: u64,
    pub reproduction_command: String,
    pub runner: RunnerMetadata,
    pub toolchain: ToolchainMetadata,
}

impl BenchmarkChildRequest {
    pub fn validate(&self) -> Result<(), String> {
        if self.protocol_version != CHILD_PROTOCOL_VERSION
            || self.schema_version != RAW_SCHEMA_VERSION
            || self.suite_version != CORE_SUITE_VERSION
            || self.scenario_version != CORE_SUITE_VERSION
        {
            return Err("unsupported benchmark child protocol or schema version".into());
        }
        if !matches!(self.run_kind.as_str(), "headline" | "reduced-smoke") {
            return Err("unknown benchmark run kind".into());
        }
        if !matches!(
            self.execution_mode.as_str(),
            "normal" | "serialized-control"
        ) {
            return Err("unknown benchmark execution mode".into());
        }
        self.allocator.validate()?;
        if self.ordinal >= crate::orchestration::ALLOCATOR_IDS.len() as u8
            || self.physical_cores == 0
            || self.logical_cores == 0
            || self.physical_cores > self.logical_cores
            || self.transactions_per_worker == 0
            || self.reproduction_command.is_empty()
        {
            return Err("benchmark child request contains invalid counts or topology".into());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct BenchmarkChildResponse {
    pub protocol_version: String,
    pub sample: RawSample,
}

impl BenchmarkChildResponse {
    pub fn validate_against(&self, request: &BenchmarkChildRequest) -> Result<(), String> {
        if self.protocol_version != CHILD_PROTOCOL_VERSION {
            return Err("child response protocol version mismatch".into());
        }
        self.sample.validate()?;
        if self.sample.schema_version != request.schema_version
            || self.sample.suite_version != request.suite_version
            || self.sample.run_kind != request.run_kind
            || self.sample.execution_mode != request.execution_mode
            || self.sample.run_seed != request.run_seed
            || self.sample.block_id != request.block_id
            || self.sample.ordinal != request.ordinal
            || self.sample.workload_seed != request.workload_seed
            || self.sample.allocator_id != request.allocator.allocator_id
            || self.sample.allocator_version != request.allocator.allocator_version
            || self.sample.allocator_source_sha != request.allocator.source_sha
            || self.sample.allocator_library_sha256 != request.allocator.library_sha256
            || self.sample.child_binary_sha256 != request.allocator.child_binary_sha256
            || self.sample.scenario_id != request.scenario_id
            || self.sample.scenario_version != request.scenario_version
            || self.sample.thread_point != request.thread_point
            || self.sample.reproduction_command != request.reproduction_command
            || self.sample.runner != request.runner
            || self.sample.toolchain != request.toolchain
        {
            return Err("child response does not exactly match its request".into());
        }
        let card_id = crate::scenarios::CardId::parse(&request.scenario_id)
            .ok_or_else(|| "child response names an unknown scenario".to_string())?;
        let thread_point = crate::scenarios::ThreadPoint::parse(&request.thread_point)
            .ok_or_else(|| "child response names an unknown thread point".to_string())?;
        let cell = crate::scenarios::ScenarioCell::new(
            card_id,
            thread_point,
            crate::scenarios::Topology {
                physical_cores: request.physical_cores as usize,
                logical_cores: request.logical_cores as usize,
            },
            request.transactions_per_worker,
            request.workload_seed,
        )
        .map_err(|error| error.to_string())?;
        let expected = cell.expected_counts().map_err(|error| error.to_string())?;
        let definition = crate::scenarios::card(card_id);
        let expected_operation_count = definition.operation_count(&expected);
        let expected_checksum = crate::execution::expected_touch_checksum(&cell)?;
        let expected_throughput =
            expected_operation_count as f64 * 1_000_000_000.0 / self.sample.elapsed_ns as f64;
        let throughput_error =
            (self.sample.throughput_operations_per_second - expected_throughput).abs();
        let throughput_tolerance = (expected_throughput.abs() * 1e-12).max(f64::EPSILON);
        if self.sample.thread_count != cell.threads as u32
            || self.sample.operation_unit != definition.operation_unit.name()
            || self.sample.operation_count != expected_operation_count
            || self.sample.requested_transactions != expected.requested_transactions
            || self.sample.completed_transactions != expected.completed_transactions
            || self.sample.allocation_calls != expected.alloc_calls
            || self.sample.calloc_calls != expected.calloc_calls
            || self.sample.aligned_allocation_calls != expected.aligned_alloc_calls
            || self.sample.free_calls != expected.free_calls
            || self.sample.realloc_calls != expected.realloc_calls
            || self.sample.checksum != expected_checksum
            || throughput_error > throughput_tolerance
            || (request.warmup_transactions_per_worker == 0 && self.sample.warmup_ns != 0)
        {
            return Err("child response contradicts the derived scenario contract".into());
        }
        Ok(())
    }
}

/// One append-only child outcome. All raw quantities use integer units except
/// the explicitly derived, finite throughput value.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RawSample {
    pub schema_version: String,
    pub suite_version: String,
    pub run_kind: String,
    pub execution_mode: String,
    pub run_seed: u64,
    pub block_id: u32,
    pub ordinal: u8,
    pub workload_seed: u64,
    pub allocator_id: String,
    pub allocator_version: String,
    pub allocator_source_sha: String,
    pub allocator_library_sha256: String,
    pub child_binary_sha256: String,
    pub scenario_id: String,
    pub scenario_version: String,
    pub thread_point: String,
    pub thread_count: u32,
    pub operation_unit: String,
    pub operation_count: u64,
    pub requested_transactions: u64,
    pub completed_transactions: u64,
    pub allocation_calls: u64,
    pub calloc_calls: u64,
    pub aligned_allocation_calls: u64,
    pub free_calls: u64,
    pub realloc_calls: u64,
    pub setup_ns: u64,
    pub warmup_ns: u64,
    pub elapsed_ns: u64,
    pub teardown_ns: u64,
    pub throughput_operations_per_second: f64,
    pub checksum: u64,
    pub peak_live_requested_bytes: u64,
    pub timed_out: bool,
    pub crashed: bool,
    pub exit_code: i32,
    pub signal: Option<i32>,
    pub runner: RunnerMetadata,
    pub toolchain: ToolchainMetadata,
    pub reproduction_command: String,
}

impl RawSample {
    pub fn validate(&self) -> Result<(), String> {
        if self.schema_version != RAW_SCHEMA_VERSION
            || self.suite_version != CORE_SUITE_VERSION
            || self.scenario_version != CORE_SUITE_VERSION
            || !matches!(self.run_kind.as_str(), "headline" | "reduced-smoke")
            || !matches!(
                self.execution_mode.as_str(),
                "normal" | "serialized-control"
            )
            || self.elapsed_ns == 0
            || self.operation_count == 0
            || self.requested_transactions == 0
            || self.completed_transactions != self.requested_transactions
            || self.ordinal >= crate::orchestration::ALLOCATOR_IDS.len() as u8
            || self.thread_count == 0
        {
            return Err("raw sample has invalid identity, timing, or transaction counts".into());
        }
        if !self.throughput_operations_per_second.is_finite()
            || self.throughput_operations_per_second <= 0.0
        {
            return Err("raw sample has non-finite or non-positive throughput".into());
        }
        if self
            .allocation_calls
            .saturating_add(self.calloc_calls)
            .saturating_add(self.aligned_allocation_calls)
            == 0
            || self.free_calls == 0
            || self.checksum == 0
            || self.peak_live_requested_bytes == 0
        {
            return Err("raw sample has impossible allocator counts or checksum".into());
        }
        if self.timed_out || self.crashed || self.exit_code != 0 || self.signal.is_some() {
            return Err("raw sample reports a failed child".into());
        }
        AllocatorIdentity {
            allocator_id: self.allocator_id.clone(),
            allocator_version: self.allocator_version.clone(),
            source_sha: self.allocator_source_sha.clone(),
            library_sha256: self.allocator_library_sha256.clone(),
            child_binary_sha256: self.child_binary_sha256.clone(),
        }
        .validate()
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RawRun {
    pub schema_version: String,
    pub suite_version: String,
    pub run_kind: String,
    pub execution_mode: String,
    pub run_seed: u64,
    pub samples: Vec<RawSample>,
}

fn is_lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}
