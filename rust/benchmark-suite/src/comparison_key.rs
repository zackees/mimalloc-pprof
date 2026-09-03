use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::orchestration::ALLOCATOR_IDS;
use crate::provenance::sha256_bytes;

/// The locked headline allocator count; a comparison key is only meaningful
/// when every locked allocator contributed to the run.
const ALLOCATOR_COUNT: usize = ALLOCATOR_IDS.len();

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ComparisonKeyError(String);

impl ComparisonKeyError {
    fn new(message: impl Into<String>) -> Self {
        Self(message.into())
    }
}

impl fmt::Display for ComparisonKeyError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl Error for ComparisonKeyError {}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SchemaCompatibility {
    pub raw_schema_version: String,
    pub latest_schema_version: String,
    pub history_schema_version: String,
    pub statistics_version: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CardCompatibility {
    pub card_id: String,
    pub card_version: String,
    pub definition: BTreeMap<String, String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct MetricCompatibility {
    pub metric_id: String,
    pub unit: String,
    pub direction: String,
    pub definition: BTreeMap<String, String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SuiteCompatibility {
    pub suite_version: String,
    pub cards: Vec<CardCompatibility>,
    pub metrics: Vec<MetricCompatibility>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RealizedOperationCount {
    pub scenario_id: String,
    pub thread_point: String,
    pub operation_count: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct PatchCompatibility {
    pub patch_id: String,
    pub sha256: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct AllocatorCompatibility {
    pub allocator_id: String,
    pub allocator_version: String,
    pub fork_kind: String,
    pub source_repository: String,
    pub source_sha: String,
    pub source_archive_sha256: Option<String>,
    pub source_tree_sha256: String,
    pub patches: Vec<PatchCompatibility>,
    pub lockfile_sha256: String,
    pub build_system: String,
    pub build_commands: Vec<Vec<String>>,
    pub compiler: String,
    pub linker: String,
    pub build_flags: Vec<String>,
    pub options: BTreeMap<String, String>,
    pub static_library_sha256: String,
    pub child_binary_sha256: String,
    pub profiler_enabled: bool,
    pub memory_events_enabled: bool,
    pub frame_pointers_enabled: bool,
    pub architecture: String,
    pub simd: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RunnerCompatibility {
    pub runner_class: String,
    pub fingerprint: String,
    pub target: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ToolchainCompatibility {
    pub compiler: String,
    pub linker: String,
    pub target: String,
    pub build_flags: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct AffinityCompatibility {
    pub policy: String,
    pub logical_cpus: Vec<u32>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct PowerCompatibility {
    pub policy: String,
    pub observable_values: BTreeMap<String, String>,
}

/// Inputs that can affect whether two benchmark rows are comparable.
/// `timestamp` and `run_url` are deliberately present here so callers cannot
/// accidentally smuggle them into generic extension data; they are omitted
/// from the canonical material below.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ComparisonKeyInput {
    pub schemas: SchemaCompatibility,
    pub suite: SuiteCompatibility,
    pub realized_operation_counts: Vec<RealizedOperationCount>,
    pub allocators: Vec<AllocatorCompatibility>,
    pub runner: RunnerCompatibility,
    pub toolchain: ToolchainCompatibility,
    pub affinity: AffinityCompatibility,
    pub power: PowerCompatibility,
    pub timestamp: String,
    pub run_url: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ComparisonKey {
    pub sha256: String,
    pub canonical_json: String,
}

#[derive(Serialize)]
struct IncludedCompatibility<'a> {
    schemas: &'a SchemaCompatibility,
    suite: SuiteCompatibility,
    realized_operation_counts: Vec<RealizedOperationCount>,
    allocators: Vec<AllocatorCompatibility>,
    runner: &'a RunnerCompatibility,
    toolchain: &'a ToolchainCompatibility,
    affinity: AffinityCompatibility,
    power: &'a PowerCompatibility,
}

pub fn comparison_key(input: &ComparisonKeyInput) -> Result<ComparisonKey, ComparisonKeyError> {
    validate_shape(input)?;

    let mut suite = input.suite.clone();
    suite.cards.sort_by(|left, right| {
        (&left.card_id, &left.card_version).cmp(&(&right.card_id, &right.card_version))
    });
    suite.metrics.sort_by(|left, right| {
        (&left.metric_id, &left.unit, &left.direction).cmp(&(
            &right.metric_id,
            &right.unit,
            &right.direction,
        ))
    });

    let mut operation_counts = input.realized_operation_counts.clone();
    operation_counts.sort_by(|left, right| {
        (&left.scenario_id, &left.thread_point).cmp(&(&right.scenario_id, &right.thread_point))
    });

    let mut allocators = input.allocators.clone();
    allocators.sort_by(|left, right| left.allocator_id.cmp(&right.allocator_id));

    let mut affinity = input.affinity.clone();
    affinity.logical_cpus.sort_unstable();

    let included = IncludedCompatibility {
        schemas: &input.schemas,
        suite,
        realized_operation_counts: operation_counts,
        allocators,
        runner: &input.runner,
        toolchain: &input.toolchain,
        affinity,
        power: &input.power,
    };
    let value = serde_json::to_value(included)
        .map_err(|error| ComparisonKeyError::new(format!("serialize comparison key: {error}")))?;
    let canonical_json = serde_json::to_string(&sort_json(value)).map_err(|error| {
        ComparisonKeyError::new(format!("encode canonical comparison key: {error}"))
    })?;
    Ok(ComparisonKey {
        sha256: sha256_bytes(canonical_json.as_bytes()),
        canonical_json,
    })
}

fn validate_shape(input: &ComparisonKeyInput) -> Result<(), ComparisonKeyError> {
    if input.allocators.len() != ALLOCATOR_COUNT {
        return Err(ComparisonKeyError::new(
            "comparison key requires exactly five allocator identities",
        ));
    }
    let allocator_ids: BTreeSet<&str> = input
        .allocators
        .iter()
        .map(|allocator| allocator.allocator_id.as_str())
        .collect();
    if allocator_ids.len() != input.allocators.len()
        || allocator_ids.iter().any(|identifier| identifier.is_empty())
    {
        return Err(ComparisonKeyError::new(
            "comparison key allocator IDs must be unique and non-empty",
        ));
    }
    if input.suite.cards.is_empty()
        || input.suite.metrics.is_empty()
        || input.realized_operation_counts.is_empty()
    {
        return Err(ComparisonKeyError::new(
            "comparison key suite definitions and operation counts must be non-empty",
        ));
    }
    let cards: BTreeSet<&str> = input
        .suite
        .cards
        .iter()
        .map(|card| card.card_id.as_str())
        .collect();
    let metrics: BTreeSet<&str> = input
        .suite
        .metrics
        .iter()
        .map(|metric| metric.metric_id.as_str())
        .collect();
    if cards.len() != input.suite.cards.len() || metrics.len() != input.suite.metrics.len() {
        return Err(ComparisonKeyError::new(
            "comparison key card and metric IDs must be unique",
        ));
    }
    Ok(())
}

fn sort_json(value: Value) -> Value {
    match value {
        Value::Array(values) => Value::Array(values.into_iter().map(sort_json).collect()),
        Value::Object(values) => {
            let sorted: BTreeMap<String, Value> = values
                .into_iter()
                .map(|(key, value)| (key, sort_json(value)))
                .collect();
            Value::Object(sorted.into_iter().collect())
        }
        scalar => scalar,
    }
}
