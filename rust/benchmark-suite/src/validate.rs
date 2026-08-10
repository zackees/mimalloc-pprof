//! Fail-closed structural and semantic validation for publication input.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::path::Path;

use serde::Serialize;

use crate::config::{AllocatorLock, AllocatorPin};
use crate::execution::expected_touch_checksum;
use crate::model::{
    AffinityMetadata, AllocatorBuildIdentity, AllocatorFeatureOptions, CellCalibration,
    FeatureState, PowerMetadata, PublicationRawRun, PublicationRunner, RawSample, RunIdentity,
    SourcePatchIdentity, ValidationReport, VALIDATOR_VERSION,
};
use crate::provenance::sha256_bytes;
use crate::scenarios::{cards, CardId, ScenarioCell, ThreadPoint, Topology};
use crate::{CORE_SUITE_VERSION, RAW_SCHEMA_VERSION};

pub const MINIMUM_BLOCKS_PER_CELL: u32 = 15;
pub const EXPECTED_CELL_COUNT: u32 = 30;
pub const HEADLINE_ALLOCATORS: [&str; 4] = [
    "tcmalloc",
    "jemalloc",
    "upstream-mimalloc",
    "mimalloc-pprof",
];
pub const VALIDATION_CHECKS: [&str; 8] = [
    "schema-and-versions",
    "run-identity",
    "runner-fingerprint",
    "allocator-provenance",
    "core-matrix-completeness",
    "paired-block-integrity",
    "numeric-and-status-validity",
    "calibration-protocol",
];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ValidationError {
    message: String,
}

impl ValidationError {
    fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl fmt::Display for ValidationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for ValidationError {}

pub fn parse_and_validate(
    input: &str,
) -> Result<(PublicationRawRun, ValidationReport), ValidationError> {
    let run: PublicationRawRun = serde_json::from_str(input)
        .map_err(|error| ValidationError::new(format!("publication raw JSON: {error}")))?;
    let report = validate_publication_raw(&run)?;
    Ok((run, report))
}

pub fn validate_path(path: &Path) -> Result<ValidationReport, ValidationError> {
    let input = std::fs::read_to_string(path).map_err(|error| {
        ValidationError::new(format!("{}: unable to read: {error}", path.display()))
    })?;
    parse_and_validate(&input)
        .map(|(_, report)| report)
        .map_err(|error| ValidationError::new(format!("{}: {error}", path.display())))
}

pub fn validate_publication_raw(
    input: &PublicationRawRun,
) -> Result<ValidationReport, ValidationError> {
    validate_versions(input)?;
    validate_run_identity(&input.run)?;
    validate_runner(&input.runner)?;
    let allocator_by_id = validate_allocators(input)?;
    let topology = Topology {
        physical_cores: input.runner.physical_cores as usize,
        logical_cores: input.runner.logical_cores as usize,
    };
    topology
        .validate()
        .map_err(|error| ValidationError::new(format!("runner topology: {error}")))?;
    let calibrations = validate_calibrations(&input.calibrations, topology)?;
    validate_samples(input, topology, &calibrations, &allocator_by_id)?;

    Ok(ValidationReport {
        validator_version: VALIDATOR_VERSION.into(),
        status: "valid".into(),
        headline_eligible: true,
        sample_count: input.samples.len() as u64,
        cell_count: EXPECTED_CELL_COUNT,
        minimum_blocks_per_cell: MINIMUM_BLOCKS_PER_CELL,
        allocator_ids: HEADLINE_ALLOCATORS.iter().map(|id| (*id).into()).collect(),
        checks: VALIDATION_CHECKS
            .iter()
            .map(|check| (*check).into())
            .collect(),
        errors: Vec::new(),
    })
}

fn validate_versions(input: &PublicationRawRun) -> Result<(), ValidationError> {
    if input.schema_version != RAW_SCHEMA_VERSION
        || input.suite_version != CORE_SUITE_VERSION
        || input.run_kind != "headline"
        || input.execution_mode != "normal"
        || input.run_seed == 0
    {
        return Err(ValidationError::new(
            "publication requires exact raw/suite versions, a non-zero seed, and headline normal mode",
        ));
    }
    Ok(())
}

fn validate_run_identity(run: &RunIdentity) -> Result<(), ValidationError> {
    if run.source_repository != "https://github.com/zackees/mimalloc-pprof"
        || !is_lower_hex(&run.source_sha, 40)
        || is_blank(&run.source_ref)
        || matches!(run.source_ref.as_str(), "HEAD" | "latest")
        || !matches!(run.run_origin.as_str(), "github-actions" | "local")
        || is_blank(&run.run_id)
        || run.run_attempt == 0
        || !looks_like_utc_timestamp(&run.generated_at_utc)
    {
        return Err(ValidationError::new(
            "run identity has an empty, floating, malformed, or unsupported field",
        ));
    }
    Ok(())
}

pub fn runner_fingerprint(runner: &PublicationRunner) -> Result<String, ValidationError> {
    #[derive(Serialize)]
    struct Fingerprint<'a> {
        runner_class: &'a str,
        stable_host_id: &'a str,
        cpu_model: &'a str,
        os: &'a str,
        os_image: &'a str,
        os_version: &'a str,
        kernel: &'a str,
        architecture: &'a str,
        physical_cores: u32,
        logical_cores: u32,
        target: &'a str,
        rustc: &'a str,
        affinity: &'a AffinityMetadata,
        power: &'a PowerMetadata,
    }
    let material = Fingerprint {
        runner_class: &runner.runner_class,
        stable_host_id: &runner.stable_host_id,
        cpu_model: &runner.cpu_model,
        os: &runner.os,
        os_image: &runner.os_image,
        os_version: &runner.os_version,
        kernel: &runner.kernel,
        architecture: &runner.architecture,
        physical_cores: runner.physical_cores,
        logical_cores: runner.logical_cores,
        target: &runner.target,
        rustc: &runner.rustc,
        affinity: &runner.affinity,
        power: &runner.power,
    };
    let bytes = serde_json::to_vec(&material)
        .map_err(|error| ValidationError::new(format!("runner fingerprint: {error}")))?;
    Ok(sha256_bytes(&bytes))
}

fn validate_runner(runner: &PublicationRunner) -> Result<(), ValidationError> {
    // No stable reference host has been checked in yet. Adding one is a
    // versioned source change, not an arbitrary user-provided string.
    const STABLE_HOST_IDS: &[&str] = &[];
    let class_ok = match runner.runner_class.as_str() {
        "github-hosted" | "self-hosted-informational" => runner.stable_host_id.is_empty(),
        "stable-host" => STABLE_HOST_IDS.contains(&runner.stable_host_id.as_str()),
        _ => false,
    };
    if !class_ok
        || is_blank(&runner.cpu_model)
        || is_blank(&runner.os)
        || is_blank(&runner.os_image)
        || is_blank(&runner.os_version)
        || is_blank(&runner.kernel)
        || is_blank(&runner.architecture)
        || runner.physical_cores == 0
        || runner.logical_cores == 0
        || runner.physical_cores > runner.logical_cores
        || runner.target != "x86_64-unknown-linux-gnu"
        || is_blank(&runner.rustc)
        || is_blank(&runner.power.governor)
        || is_blank(&runner.power.boost)
        || is_blank(&runner.power.frequency_policy)
    {
        return Err(ValidationError::new(
            "runner class, CPU/OS/target/toolchain/affinity/power metadata is invalid",
        ));
    }
    match runner.affinity.policy.as_str() {
        "unrestricted" if runner.affinity.logical_cpu_ids.is_empty() => {}
        "pinned"
            if !runner.affinity.logical_cpu_ids.is_empty()
                && runner
                    .affinity
                    .logical_cpu_ids
                    .iter()
                    .all(|cpu| *cpu < runner.logical_cores)
                && runner
                    .affinity
                    .logical_cpu_ids
                    .iter()
                    .collect::<BTreeSet<_>>()
                    .len()
                    == runner.affinity.logical_cpu_ids.len() => {}
        _ => {
            return Err(ValidationError::new(
                "runner affinity policy and CPU list are inconsistent",
            ));
        }
    }
    if !is_lower_hex(&runner.fingerprint_sha256, 64)
        || runner.fingerprint_sha256 != runner_fingerprint(runner)?
    {
        return Err(ValidationError::new("mixed or stale runner fingerprint"));
    }
    Ok(())
}

fn validate_allocators<'a>(
    input: &'a PublicationRawRun,
) -> Result<BTreeMap<&'a str, &'a AllocatorBuildIdentity>, ValidationError> {
    let lock = AllocatorLock::parse_and_validate(include_str!("../allocators/allocator-lock.json"))
        .map_err(|error| ValidationError::new(format!("embedded allocator lock: {error}")))?;
    let expected_lock_sha = sha256_bytes(include_bytes!("../allocators/allocator-lock.json"));
    if input.allocator_lock_sha256 != expected_lock_sha || input.allocators.len() != 4 {
        return Err(ValidationError::new(
            "allocator provenance does not match the embedded four-allocator lock",
        ));
    }
    let ids: Vec<&str> = input
        .allocators
        .iter()
        .map(|allocator| allocator.allocator_id.as_str())
        .collect();
    if ids != HEADLINE_ALLOCATORS {
        return Err(ValidationError::new(
            "headline allocator identities are missing, extra, duplicated, or reordered",
        ));
    }
    let mut child_hashes = BTreeSet::new();
    let mut by_id = BTreeMap::new();
    for (allocator, pin) in input.allocators.iter().zip(&lock.allocators) {
        validate_allocator(input, allocator, pin)?;
        if !child_hashes.insert(allocator.child_binary_sha256.as_str()) {
            return Err(ValidationError::new(
                "allocator child binary identities must be distinct",
            ));
        }
        by_id.insert(allocator.allocator_id.as_str(), allocator);
    }
    Ok(by_id)
}

fn validate_allocator(
    input: &PublicationRawRun,
    allocator: &AllocatorBuildIdentity,
    pin: &AllocatorPin,
) -> Result<(), ValidationError> {
    let is_fork = pin.id == "mimalloc-pprof";
    let expected_source = if is_fork {
        input.run.source_sha.as_str()
    } else {
        pin.source.commit.as_str()
    };
    let expected_version = if is_fork {
        input.run.source_sha.as_str()
    } else {
        pin.pin.as_str()
    };
    let expected_archive_url = pin
        .source
        .archive_url
        .as_deref()
        .unwrap_or("not-applicable");
    let expected_archive_hash = pin
        .source
        .archive_sha256
        .as_deref()
        .unwrap_or("not-applicable");
    let expected_patches: Vec<SourcePatchIdentity> = pin
        .patches
        .source
        .iter()
        .map(|patch| SourcePatchIdentity {
            file: patch.file.clone(),
            sha256: patch.sha256.clone(),
        })
        .collect();
    if allocator.allocator_id != pin.id
        || allocator.source_kind != pin.source.kind
        || allocator.canonical_repository != pin.source.repository
        || allocator.source_sha != expected_source
        || allocator.source_archive_url != expected_archive_url
        || allocator.source_archive_sha256 != expected_archive_hash
        || !is_lower_hex(&allocator.source_tree_sha256, 64)
        || allocator.source_patches != expected_patches
        || allocator.build_system != pin.build.system
        || allocator.build_commands != pin.build.commands
        || allocator.build_flags != pin.build.flags
        || is_blank(&allocator.compiler)
        || is_blank(&allocator.linker)
        || !is_lower_hex(&allocator.static_library_sha256, 64)
        || !is_lower_hex(&allocator.child_binary_sha256, 64)
        || allocator.allocator_version != expected_version
    {
        return Err(ValidationError::new(format!(
            "allocator provenance for {} is stale, floating, incomplete, or does not match the lock",
            pin.id
        )));
    }
    let expected_options = expected_options(&pin.id);
    if allocator.options != expected_options {
        return Err(ValidationError::new(format!(
            "allocator options for {} do not explicitly match the suite",
            pin.id
        )));
    }
    Ok(())
}

fn expected_options(id: &str) -> AllocatorFeatureOptions {
    use FeatureState::{Disabled, Enabled, NotApplicable};
    match id {
        "mimalloc-pprof" => AllocatorFeatureOptions {
            pprof_compiled: Enabled,
            pprof_runtime: Disabled,
            memory_events_compiled: Enabled,
            memory_events_runtime: Disabled,
            frame_pointers: Enabled,
            opt_arch: Disabled,
            opt_simd: Enabled,
        },
        "upstream-mimalloc" => AllocatorFeatureOptions {
            pprof_compiled: Disabled,
            pprof_runtime: Disabled,
            memory_events_compiled: NotApplicable,
            memory_events_runtime: NotApplicable,
            frame_pointers: Enabled,
            opt_arch: Disabled,
            opt_simd: Enabled,
        },
        _ => AllocatorFeatureOptions {
            pprof_compiled: NotApplicable,
            pprof_runtime: NotApplicable,
            memory_events_compiled: NotApplicable,
            memory_events_runtime: NotApplicable,
            frame_pointers: Enabled,
            opt_arch: NotApplicable,
            opt_simd: NotApplicable,
        },
    }
}

type CellKey = (String, String);

#[derive(Clone, Eq, Ord, PartialEq, PartialOrd)]
struct RequestIdentity {
    cell: CellKey,
    workload_seed: u64,
    thread_count: u32,
    operation_unit: String,
    operation_count: u64,
    requested_transactions: u64,
    completed_transactions: u64,
    allocation_calls: u64,
    calloc_calls: u64,
    aligned_allocation_calls: u64,
    free_calls: u64,
    realloc_calls: u64,
    checksum: u64,
}

impl RequestIdentity {
    fn from_sample(cell: CellKey, sample: &RawSample) -> Self {
        Self {
            cell,
            workload_seed: sample.workload_seed,
            thread_count: sample.thread_count,
            operation_unit: sample.operation_unit.clone(),
            operation_count: sample.operation_count,
            requested_transactions: sample.requested_transactions,
            completed_transactions: sample.completed_transactions,
            allocation_calls: sample.allocation_calls,
            calloc_calls: sample.calloc_calls,
            aligned_allocation_calls: sample.aligned_allocation_calls,
            free_calls: sample.free_calls,
            realloc_calls: sample.realloc_calls,
            checksum: sample.checksum,
        }
    }
}

fn validate_calibrations(
    calibrations: &[CellCalibration],
    topology: Topology,
) -> Result<BTreeMap<CellKey, &CellCalibration>, ValidationError> {
    let expected: BTreeSet<CellKey> = cards()
        .iter()
        .flat_map(|card| {
            card.thread_points
                .iter()
                .map(move |point| (card.id.as_str().to_string(), point.name().to_string()))
        })
        .collect();
    if expected.len() != EXPECTED_CELL_COUNT as usize || calibrations.len() != expected.len() {
        return Err(ValidationError::new(
            "calibration matrix must contain all and only the 30 core cells",
        ));
    }
    let mut by_cell = BTreeMap::new();
    for calibration in calibrations {
        let key = (
            calibration.scenario_id.clone(),
            calibration.thread_point.clone(),
        );
        if !expected.contains(&key) || by_cell.insert(key.clone(), calibration).is_some() {
            return Err(ValidationError::new(format!(
                "duplicate or unknown calibration cell {}/{}",
                key.0, key.1
            )));
        }
        let card_id = CardId::parse(&calibration.scenario_id)
            .ok_or_else(|| ValidationError::new("calibration names an unknown scenario"))?;
        let point = ThreadPoint::parse(&calibration.thread_point)
            .ok_or_else(|| ValidationError::new("calibration names an unknown thread point"))?;
        let thread_count = topology
            .resolve(point)
            .map_err(|error| ValidationError::new(format!("calibration topology: {error}")))?;
        let cell = ScenarioCell::new(
            card_id,
            point,
            topology,
            calibration.transactions_per_worker,
            1,
        )
        .map_err(|error| ValidationError::new(format!("calibration scenario: {error}")))?;
        let counts = cell
            .expected_counts()
            .map_err(|error| ValidationError::new(format!("calibration counts: {error}")))?;
        let operation_count = crate::scenarios::card(card_id).operation_count(&counts);
        if calibration.thread_count != thread_count as u32
            || calibration.transactions_per_worker == 0
            || calibration.warmup_transactions_per_worker == 0
            || calibration.operation_count != operation_count
            || !(500_000_000..=2_000_000_000).contains(&calibration.elapsed_ns)
        {
            return Err(ValidationError::new(format!(
                "calibration for {}/{} is outside the 0.5-2.0s protocol or contradicts realized counts",
                calibration.scenario_id, calibration.thread_point
            )));
        }
    }
    Ok(by_cell)
}

fn validate_samples(
    input: &PublicationRawRun,
    topology: Topology,
    calibrations: &BTreeMap<CellKey, &CellCalibration>,
    allocators: &BTreeMap<&str, &AllocatorBuildIdentity>,
) -> Result<(), ValidationError> {
    let mut cells: BTreeMap<CellKey, BTreeMap<u32, Vec<&RawSample>>> = BTreeMap::new();
    let mut unique = BTreeSet::new();
    let mut derived_request_streams = BTreeSet::new();
    for (index, sample) in input.samples.iter().enumerate() {
        sample
            .validate()
            .map_err(|error| ValidationError::new(format!("samples[{index}]: {error}")))?;
        if sample.schema_version != input.schema_version
            || sample.suite_version != input.suite_version
            || sample.scenario_version != input.suite_version
            || sample.run_kind != input.run_kind
            || sample.execution_mode != input.execution_mode
            || sample.run_seed != input.run_seed
        {
            return Err(ValidationError::new(format!(
                "samples[{index}] has a mixed run identity"
            )));
        }
        if sample.runner.os != input.runner.os
            || sample.runner.architecture != input.runner.architecture
            || sample.runner.physical_cores != input.runner.physical_cores
            || sample.runner.logical_cores != input.runner.logical_cores
            || sample.toolchain.rustc != input.runner.rustc
            || sample.toolchain.target != input.runner.target
        {
            return Err(ValidationError::new(format!(
                "samples[{index}] has mixed runner metadata"
            )));
        }
        let allocator = allocators
            .get(sample.allocator_id.as_str())
            .ok_or_else(|| {
                ValidationError::new(format!("samples[{index}] names a non-headline allocator"))
            })?;
        if sample.allocator_version != allocator.allocator_version
            || sample.allocator_source_sha != allocator.source_sha
            || sample.allocator_library_sha256 != allocator.static_library_sha256
            || sample.child_binary_sha256 != allocator.child_binary_sha256
            || sample.toolchain.compiler != allocator.compiler
            || sample.toolchain.linker != allocator.linker
        {
            return Err(ValidationError::new(format!(
                "samples[{index}] does not match allocator build/binary provenance"
            )));
        }
        let key = (sample.scenario_id.clone(), sample.thread_point.clone());
        let calibration = calibrations.get(&key).ok_or_else(|| {
            ValidationError::new(format!("samples[{index}] is outside the core matrix"))
        })?;
        if derived_request_streams.insert(RequestIdentity::from_sample(key.clone(), sample)) {
            validate_derived_sample(index, sample, calibration, topology)?;
        }
        let unique_key = (key.clone(), sample.block_id, sample.allocator_id.clone());
        if !unique.insert(unique_key) {
            return Err(ValidationError::new(format!(
                "duplicate sample key at samples[{index}]"
            )));
        }
        cells
            .entry(key)
            .or_default()
            .entry(sample.block_id)
            .or_default()
            .push(sample);
    }
    if cells.len() != EXPECTED_CELL_COUNT as usize {
        return Err(ValidationError::new(
            "raw sample matrix is incomplete: expected exactly 30 cells",
        ));
    }
    for (cell, blocks) in &cells {
        if blocks.len() < MINIMUM_BLOCKS_PER_CELL as usize {
            return Err(ValidationError::new(format!(
                "cell {}/{} has fewer than 15 complete blocks",
                cell.0, cell.1
            )));
        }
        let mut ordinal_counts = [[0_u32; 4]; 4];
        for (block_id, samples) in blocks {
            validate_block(cell, *block_id, samples, &mut ordinal_counts)?;
        }
        for allocator_counts in ordinal_counts {
            let min = allocator_counts.iter().min().copied().unwrap_or(0);
            let max = allocator_counts.iter().max().copied().unwrap_or(0);
            if max - min > 1 {
                return Err(ValidationError::new(format!(
                    "cell {}/{} allocator order is not near-balanced",
                    cell.0, cell.1
                )));
            }
        }
    }
    Ok(())
}

fn validate_derived_sample(
    index: usize,
    sample: &RawSample,
    calibration: &CellCalibration,
    topology: Topology,
) -> Result<(), ValidationError> {
    let card_id = CardId::parse(&sample.scenario_id)
        .ok_or_else(|| ValidationError::new(format!("samples[{index}] unknown scenario")))?;
    let point = ThreadPoint::parse(&sample.thread_point)
        .ok_or_else(|| ValidationError::new(format!("samples[{index}] unknown thread point")))?;
    let cell = ScenarioCell::new(
        card_id,
        point,
        topology,
        calibration.transactions_per_worker,
        sample.workload_seed,
    )
    .map_err(|error| ValidationError::new(format!("samples[{index}] scenario: {error}")))?;
    let expected = cell
        .expected_counts()
        .map_err(|error| ValidationError::new(format!("samples[{index}] counts: {error}")))?;
    let definition = crate::scenarios::card(card_id);
    let operation_count = definition.operation_count(&expected);
    let checksum = expected_touch_checksum(&cell)
        .map_err(|error| ValidationError::new(format!("samples[{index}] checksum: {error}")))?;
    let throughput = operation_count as f64 * 1_000_000_000.0 / sample.elapsed_ns as f64;
    let tolerance = (throughput.abs() * 1e-12).max(f64::EPSILON);
    let total_ns = sample
        .setup_ns
        .checked_add(sample.warmup_ns)
        .and_then(|value| value.checked_add(sample.elapsed_ns))
        .and_then(|value| value.checked_add(sample.teardown_ns));
    if sample.thread_count != calibration.thread_count
        || sample.operation_unit != definition.operation_unit.name()
        || sample.operation_count != calibration.operation_count
        || sample.operation_count != operation_count
        || sample.requested_transactions != expected.requested_transactions
        || sample.completed_transactions != expected.completed_transactions
        || sample.allocation_calls != expected.alloc_calls
        || sample.calloc_calls != expected.calloc_calls
        || sample.aligned_allocation_calls != expected.aligned_alloc_calls
        || sample.free_calls != expected.free_calls
        || sample.realloc_calls != expected.realloc_calls
        || sample.checksum != checksum
        || (sample.throughput_operations_per_second - throughput).abs() > tolerance
        || sample.warmup_ns == 0
        || total_ns.is_none()
    {
        return Err(ValidationError::new(format!(
            "samples[{index}] is partial, non-finite, non-positive, or contradicts the calibrated scenario/request stream"
        )));
    }
    Ok(())
}

fn validate_block(
    cell: &CellKey,
    block_id: u32,
    samples: &[&RawSample],
    ordinal_counts: &mut [[u32; 4]; 4],
) -> Result<(), ValidationError> {
    if samples.len() != 4 {
        return Err(ValidationError::new(format!(
            "cell {}/{} block {block_id} does not contain exactly four allocators",
            cell.0, cell.1
        )));
    }
    let first = samples[0];
    let mut ids = BTreeSet::new();
    let mut ordinals = BTreeSet::new();
    for sample in samples {
        ids.insert(sample.allocator_id.as_str());
        ordinals.insert(sample.ordinal);
        let allocator_index = HEADLINE_ALLOCATORS
            .iter()
            .position(|id| *id == sample.allocator_id)
            .expect("allocator membership checked before grouping");
        ordinal_counts[allocator_index][sample.ordinal as usize] += 1;
        if sample.workload_seed != first.workload_seed
            || sample.thread_count != first.thread_count
            || sample.operation_unit != first.operation_unit
            || sample.operation_count != first.operation_count
            || sample.requested_transactions != first.requested_transactions
            || sample.completed_transactions != first.completed_transactions
            || sample.allocation_calls != first.allocation_calls
            || sample.calloc_calls != first.calloc_calls
            || sample.aligned_allocation_calls != first.aligned_allocation_calls
            || sample.free_calls != first.free_calls
            || sample.realloc_calls != first.realloc_calls
            || sample.checksum != first.checksum
            || sample.peak_live_requested_bytes != first.peak_live_requested_bytes
        {
            return Err(ValidationError::new(format!(
                "cell {}/{} block {block_id} has mismatched seed/count/request identity",
                cell.0, cell.1
            )));
        }
    }
    let expected_ids: BTreeSet<&str> = HEADLINE_ALLOCATORS.into_iter().collect();
    let expected_ordinals: BTreeSet<u8> = (0..4).collect();
    if ids != expected_ids || ordinals != expected_ordinals {
        return Err(ValidationError::new(format!(
            "cell {}/{} block {block_id} order is not a permutation of the four allocators",
            cell.0, cell.1
        )));
    }
    Ok(())
}

fn is_blank(value: &str) -> bool {
    value.trim().is_empty()
}

fn is_lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn looks_like_utc_timestamp(value: &str) -> bool {
    value.len() >= 20 && value.ends_with('Z') && value.contains('T')
}

/// Offline smoke for the CLI. It validates all checked-in schemas as JSON and
/// exercises both a complete 1,800-sample run and a known incomplete matrix.
pub fn selftest() -> Result<(), ValidationError> {
    for (name, schema) in [
        (
            "raw-run-v1.schema.json",
            include_str!("../schema/raw-run-v1.schema.json"),
        ),
        (
            "latest-v1.schema.json",
            include_str!("../schema/latest-v1.schema.json"),
        ),
        (
            "history-v1.schema.json",
            include_str!("../schema/history-v1.schema.json"),
        ),
    ] {
        let value: serde_json::Value = serde_json::from_str(schema)
            .map_err(|error| ValidationError::new(format!("{name}: invalid JSON: {error}")))?;
        if value.get("$schema").is_none()
            || value.get("additionalProperties") != Some(&false.into())
        {
            return Err(ValidationError::new(format!(
                "{name}: schema must be versioned and fail closed"
            )));
        }
    }
    let valid = synthetic_full_fixture()?;
    validate_publication_raw(&valid)?;
    let mut incomplete = valid;
    incomplete.samples.pop();
    if validate_publication_raw(&incomplete).is_ok() {
        return Err(ValidationError::new(
            "validator selftest accepted an incomplete matrix",
        ));
    }
    Ok(())
}

/// Deterministic fixture builder shared by integration tests. Values are test
/// protocol data only and never appear as an advanced/public metric.
#[doc(hidden)]
pub fn synthetic_full_fixture() -> Result<PublicationRawRun, ValidationError> {
    let lock = AllocatorLock::parse_and_validate(include_str!("../allocators/allocator-lock.json"))
        .map_err(ValidationError::new)?;
    let run = RunIdentity {
        source_repository: "https://github.com/zackees/mimalloc-pprof".into(),
        source_sha: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".into(),
        source_ref: "refs/heads/main".into(),
        run_origin: "local".into(),
        run_id: "validation-fixture".into(),
        run_attempt: 1,
        generated_at_utc: "2026-08-10T00:00:00Z".into(),
    };
    let mut runner = PublicationRunner {
        runner_class: "self-hosted-informational".into(),
        stable_host_id: String::new(),
        fingerprint_sha256: String::new(),
        cpu_model: "fixture-cpu".into(),
        os: "linux".into(),
        os_image: "fixture-linux".into(),
        os_version: "1".into(),
        kernel: "fixture-kernel".into(),
        architecture: "x86_64".into(),
        physical_cores: 2,
        logical_cores: 4,
        target: "x86_64-unknown-linux-gnu".into(),
        rustc: "rustc fixture".into(),
        affinity: AffinityMetadata {
            policy: "unrestricted".into(),
            logical_cpu_ids: Vec::new(),
        },
        power: PowerMetadata {
            governor: "not-observable".into(),
            boost: "not-observable".into(),
            frequency_policy: "not-observable".into(),
        },
    };
    runner.fingerprint_sha256 = runner_fingerprint(&runner)?;
    let allocators: Vec<AllocatorBuildIdentity> = lock
        .allocators
        .iter()
        .enumerate()
        .map(|(index, pin)| {
            let source_sha = if pin.id == "mimalloc-pprof" {
                run.source_sha.clone()
            } else {
                pin.source.commit.clone()
            };
            let allocator_version = if pin.id == "mimalloc-pprof" {
                run.source_sha.clone()
            } else {
                pin.pin.clone()
            };
            AllocatorBuildIdentity {
                allocator_id: pin.id.clone(),
                allocator_version,
                source_kind: pin.source.kind.clone(),
                canonical_repository: pin.source.repository.clone(),
                source_sha,
                source_archive_url: pin
                    .source
                    .archive_url
                    .clone()
                    .unwrap_or_else(|| "not-applicable".into()),
                source_archive_sha256: pin
                    .source
                    .archive_sha256
                    .clone()
                    .unwrap_or_else(|| "not-applicable".into()),
                source_tree_sha256: repeated_hex((b'1' + index as u8) as char, 64),
                source_patches: pin
                    .patches
                    .source
                    .iter()
                    .map(|patch| SourcePatchIdentity {
                        file: patch.file.clone(),
                        sha256: patch.sha256.clone(),
                    })
                    .collect(),
                build_system: pin.build.system.clone(),
                build_commands: pin.build.commands.clone(),
                build_flags: pin.build.flags.clone(),
                compiler: format!("fixture-compiler-{index}"),
                linker: format!("fixture-linker-{index}"),
                static_library_sha256: repeated_hex((b'5' + index as u8) as char, 64),
                child_binary_sha256: repeated_hex(['9', 'a', 'b', 'c'][index], 64),
                options: expected_options(&pin.id),
            }
        })
        .collect();
    let topology = Topology {
        physical_cores: 2,
        logical_cores: 4,
    };
    let mut calibrations = Vec::new();
    let mut samples = Vec::new();
    for definition in cards() {
        for &point in definition.thread_points {
            let calibration_cell = ScenarioCell::new(definition.id, point, topology, 1, 1)
                .map_err(|error| ValidationError::new(error.to_string()))?;
            let calibration_counts = calibration_cell
                .expected_counts()
                .map_err(|error| ValidationError::new(error.to_string()))?;
            let operation_count = definition.operation_count(&calibration_counts);
            calibrations.push(CellCalibration {
                scenario_id: definition.id.as_str().into(),
                thread_point: point.name().into(),
                thread_count: calibration_cell.threads as u32,
                transactions_per_worker: 1,
                warmup_transactions_per_worker: 1,
                operation_count,
                elapsed_ns: 1_000_000_000,
            });
            let sample_cell = ScenarioCell::new(definition.id, point, topology, 1, 10_000)
                .map_err(|error| ValidationError::new(error.to_string()))?;
            let sample_counts = sample_cell
                .expected_counts()
                .map_err(|error| ValidationError::new(error.to_string()))?;
            let sample_checksum = expected_touch_checksum(&sample_cell)
                .map_err(|error| ValidationError::new(error.to_string()))?;
            for block_id in 0..MINIMUM_BLOCKS_PER_CELL {
                let workload_seed = 10_000;
                for (allocator_index, allocator) in allocators.iter().enumerate() {
                    let elapsed_ns = 800_000_000
                        + u64::from(block_id) * 1_000_000
                        + allocator_index as u64 * 10_000_000;
                    samples.push(RawSample {
                        schema_version: RAW_SCHEMA_VERSION.into(),
                        suite_version: CORE_SUITE_VERSION.into(),
                        run_kind: "headline".into(),
                        execution_mode: "normal".into(),
                        run_seed: 7,
                        block_id,
                        ordinal: ((allocator_index as u32 + block_id) % 4) as u8,
                        workload_seed,
                        allocator_id: allocator.allocator_id.clone(),
                        allocator_version: allocator.allocator_version.clone(),
                        allocator_source_sha: allocator.source_sha.clone(),
                        allocator_library_sha256: allocator.static_library_sha256.clone(),
                        child_binary_sha256: allocator.child_binary_sha256.clone(),
                        scenario_id: definition.id.as_str().into(),
                        scenario_version: CORE_SUITE_VERSION.into(),
                        thread_point: point.name().into(),
                        thread_count: sample_cell.threads as u32,
                        operation_unit: definition.operation_unit.name().into(),
                        operation_count,
                        requested_transactions: sample_counts.requested_transactions,
                        completed_transactions: sample_counts.completed_transactions,
                        allocation_calls: sample_counts.alloc_calls,
                        calloc_calls: sample_counts.calloc_calls,
                        aligned_allocation_calls: sample_counts.aligned_alloc_calls,
                        free_calls: sample_counts.free_calls,
                        realloc_calls: sample_counts.realloc_calls,
                        setup_ns: 10,
                        warmup_ns: 10,
                        elapsed_ns,
                        teardown_ns: 10,
                        throughput_operations_per_second: operation_count as f64 * 1_000_000_000.0
                            / elapsed_ns as f64,
                        checksum: sample_checksum,
                        peak_live_requested_bytes: 1,
                        timed_out: false,
                        crashed: false,
                        exit_code: 0,
                        signal: None,
                        runner: crate::model::RunnerMetadata {
                            os: runner.os.clone(),
                            architecture: runner.architecture.clone(),
                            physical_cores: runner.physical_cores,
                            logical_cores: runner.logical_cores,
                        },
                        toolchain: crate::model::ToolchainMetadata {
                            rustc: runner.rustc.clone(),
                            target: runner.target.clone(),
                            compiler: allocator.compiler.clone(),
                            linker: allocator.linker.clone(),
                        },
                        reproduction_command: "fixture reproduction".into(),
                    });
                }
            }
        }
    }
    Ok(PublicationRawRun {
        schema_version: RAW_SCHEMA_VERSION.into(),
        suite_version: CORE_SUITE_VERSION.into(),
        run_kind: "headline".into(),
        execution_mode: "normal".into(),
        run_seed: 7,
        run,
        runner,
        allocator_lock_sha256: sha256_bytes(include_bytes!("../allocators/allocator-lock.json")),
        allocators,
        calibrations,
        samples,
    })
}

fn repeated_hex(character: char, length: usize) -> String {
    std::iter::repeat(character).take(length).collect()
}
