//! pprof-tax-v1: the compilation and runtime overhead of the fork's sampled
//! heap profiler, measured as a chain of paired comparisons across seven
//! build/runtime configurations over the fixed `core-throughput-v1` workload
//! subset used for this panel.
//!
//! This module owns the data contract only: constants, the configuration and
//! comparison tables, build-equivalence (manifest) validation, the raw-run
//! schema and impossible-state rejection, sample classification, whole-block
//! paired statistics, and the `latest.json`/history sections. The child
//! protocol and the producer that spawns real children live in
//! `pprof_tax_child` / `pprof_tax_runner`.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};

use crate::model::{LatestReport, PublicationRunner, RunIdentity};
use crate::provenance::sha256_bytes;
use crate::scaling::splitmix64;
use crate::scenarios::{card, CardId, SizeDistribution, ThreadPoint, Topology};
use crate::stats::{summarize_paired, MetricDirection, MetricObservation, STATISTICS_VERSION};

pub const PPROF_TAX_SCHEMA_VERSION: &str = "pprof-tax-v1";
pub const PPROF_TAX_RAW_SCHEMA_VERSION: &str = "pprof-tax-raw-v1";
pub const PPROF_TAX_MANIFEST_SCHEMA_VERSION: &str = "pprof-tax-manifest-v1";
pub const PPROF_TAX_CHILD_PROTOCOL_VERSION: &str = "pprof-tax-child-v1";
/// Whole-block bootstrap floor: below this many paired blocks a comparison is
/// `insufficient-paired-blocks`, never `supported`. Checked verbatim by
/// `ci/check_benchmark_pprof_tax_workflow.py` against `ci/benchmark_report.py`;
/// do not reformat this line.
pub const PPROF_TAX_MIN_BLOCKS: u32 = 15;
/// The issue text names `bcee5a88`, but #332 moved the benchmark pin to the
/// fork's own base `6def7be9` (see CLAUDE.md's overlay note). The intent is
/// "the pinned base without fork patches applied"; this identifies that
/// commit and must never be moved without re-verifying the profiler hook
/// patches apply byte-identically against it.
pub const PPROF_TAX_UPSTREAM_COMMIT: &str = "6def7be9458fb8a97b8323af3fb0b0ae04387065";
pub const PPROF_TAX_SPARSE_INTERVAL_BYTES: u64 = 524288;
pub const PPROF_TAX_SPARSE_RATIONALE: &str =
    "repository default: mi_option_prof_sample_rate=524288 (include/mimalloc.h) and the profile.c fallback";
pub const PPROF_TAX_AGGRESSIVE_INTERVAL_BYTES: u64 = 4096;
pub const PPROF_TAX_STRESS_INTERVAL_BYTES: u64 = 1;
pub const PPROF_TAX_TARGET_BLOCK_NS: u64 = 250_000_000;
pub const PPROF_TAX_MIN_BLOCK_NS: u64 = 150_000_000;
pub const PPROF_TAX_MAX_BLOCK_NS: u64 = 600_000_000;
/// An active run is invalid if it observed zero samples while the lower bound
/// on allocated bytes already crossed this many sampling intervals: for a
/// Poisson sampler, P(0 samples | 20 intervals crossed) = e^-20.
pub const PPROF_TAX_ZERO_SAMPLE_CROSSING_FACTOR: u64 = 20;
pub const PPROF_TAX_MAX_DROPPED_FRACTION: f64 = 0.01;
pub const PPROF_TAX_SCOPE_WARNING: &str =
    "Measured on one GitHub-hosted ubuntu-24.04 runner. Runner-specific; not a universal pprof overhead claim.";
pub const PPROF_TAX_METRIC_ID: &str = "throughput-operations-per-second";

const BLOCK_ORDER_DOMAIN: u64 = 0x7070_726f_665f_7461;

/// The `core-throughput-v1` workload subset this panel measures.
pub const PPROF_TAX_WORKLOADS: [&str; 5] = [
    "tiny-fixed-64",
    "small-log-mixed",
    "cross-thread-producer-consumer",
    "thread-churn",
    "representative-mix",
];

/// The seven stable configuration IDs, in canonical (and block-order-seed)
/// order. Renaming or reordering any of these breaks the shared contract with
/// the sibling tasks that implement this same protocol; do not.
pub const PPROF_TAX_CONFIGURATION_IDS: [&str; 7] = [
    "upstream-baseline",
    "fork-pprof-off",
    "fork-pprof-on-stopped",
    "fork-pprof-off-frame-pointers",
    "fork-pprof-sparse",
    "fork-pprof-aggressive",
    "fork-pprof-rate-1-stress",
];

/// The four distinct compiled artifacts. Several configuration IDs above
/// share one compiled binary and differ only in runtime sampling state.
pub const PPROF_TAX_COMPILED_CONFIGURATION_IDS: [&str; 4] = [
    "upstream-baseline",
    "fork-pprof-off",
    "fork-pprof-on",
    "fork-pprof-off-frame-pointers",
];

/// CMake cache keys that must be identical (or allowlisted-different) across
/// every compiled configuration for the comparison to mean anything. Sorted.
pub const PPROF_TAX_EQUIVALENCE_CACHE_KEYS: [&str; 20] = [
    "CMAKE_AR",
    "CMAKE_BUILD_TYPE",
    "CMAKE_C_COMPILER",
    "CMAKE_C_FLAGS",
    "CMAKE_C_FLAGS_RELEASE",
    "CMAKE_EXE_LINKER_FLAGS",
    "CMAKE_INTERPROCEDURAL_OPTIMIZATION",
    "CMAKE_STATIC_LINKER_FLAGS",
    "MI_BUILD_SHARED",
    "MI_BUILD_STATIC",
    "MI_BUILD_TESTS",
    "MI_DEBUG_FULL",
    "MI_DHAT",
    "MI_OPT_ARCH",
    "MI_OPT_SIMD",
    "MI_OVERRIDE",
    "MI_PPROF",
    "MI_SECURE",
    "MI_TRACK_ASAN",
    "MI_TRACK_VALGRIND",
];

/// Cache keys that exist only on the fork's CMakeLists.txt. Upstream must
/// report these as `null`, never as an explicit "off" value: upstream simply
/// has no such option.
pub const PPROF_TAX_FORK_ONLY_CACHE_KEYS: [&str; 2] = ["MI_DHAT", "MI_PPROF"];

/// One cell of the pprof-tax workload matrix, resolved against runner
/// topology.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PprofTaxCellSpec {
    pub scenario_id: &'static str,
    pub thread_point: &'static str,
    pub thread_count: u32,
}

/// Resolve the pprof-tax cell matrix for one topology.
///
/// For each workload the candidate points are always `[One, PhysicalCores]`;
/// if the card does not declare `One`, its first declared point stands in.
/// Points the card does not declare are dropped, the remainder is resolved
/// against topology, and equal resolved thread counts are deduplicated,
/// keeping the first occurrence.
pub fn pprof_tax_cells(topology: &Topology) -> Result<Vec<PprofTaxCellSpec>, String> {
    topology.validate().map_err(|error| error.to_string())?;
    let mut cells = Vec::new();
    for &scenario_id in &PPROF_TAX_WORKLOADS {
        let card_id = CardId::parse(scenario_id).ok_or_else(|| {
            format!("pprof-tax workload names an unknown scenario: {scenario_id}")
        })?;
        let definition = card(card_id);
        let mut candidates = [ThreadPoint::One, ThreadPoint::PhysicalCores];
        if !definition.thread_points.contains(&ThreadPoint::One) {
            candidates[0] = definition.thread_points[0];
        }
        let mut seen_counts: BTreeSet<u32> = BTreeSet::new();
        for point in candidates {
            if !definition.thread_points.contains(&point) {
                continue;
            }
            let thread_count = topology.resolve(point).map_err(|error| error.to_string())? as u32;
            if seen_counts.insert(thread_count) {
                cells.push(PprofTaxCellSpec {
                    scenario_id,
                    thread_point: point.name(),
                    thread_count,
                });
            }
        }
    }
    Ok(cells)
}

/// The smallest request a workload can issue, used to reason about how many
/// sampling intervals a run's allocations could possibly have crossed.
pub fn minimum_request_bytes(scenario_id: &str) -> Result<u64, String> {
    let card_id = CardId::parse(scenario_id)
        .ok_or_else(|| format!("pprof-tax scenario id is unknown: {scenario_id}"))?;
    let definition = card(card_id);
    Ok(match definition.size_distribution() {
        SizeDistribution::Fixed(size) => size as u64,
        SizeDistribution::LogParetoLike { min, .. } => min as u64,
        SizeDistribution::AlignedRange { min_alignment, .. } => min_alignment as u64,
        // Both weighted mixes bottom out at the shared small-object draw,
        // whose declared floor is 8 bytes (see `small_size` in scenarios.rs).
        SizeDistribution::RepresentativeWeightedMix | SizeDistribution::WorkerGenerationMix => 8,
    })
}

/// One build/runtime configuration in the pprof-tax matrix.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PprofTaxConfiguration {
    pub configuration_id: &'static str,
    pub compiled_configuration_id: &'static str,
    pub allocator_id: &'static str,
    pub pprof_compiled: bool,
    pub pprof_active: bool,
    pub sampling_interval_bytes: Option<u64>,
    pub frame_pointer_policy: &'static str,
    pub role: &'static str,
}

pub const PPROF_TAX_CONFIGURATIONS: [PprofTaxConfiguration; 7] = [
    PprofTaxConfiguration {
        configuration_id: "upstream-baseline",
        compiled_configuration_id: "upstream-baseline",
        allocator_id: "upstream-mimalloc",
        pprof_compiled: false,
        pprof_active: false,
        sampling_interval_bytes: None,
        frame_pointer_policy: "omitted",
        role: "context",
    },
    PprofTaxConfiguration {
        configuration_id: "fork-pprof-off",
        compiled_configuration_id: "fork-pprof-off",
        allocator_id: "mimalloc-pprof",
        pprof_compiled: false,
        pprof_active: false,
        sampling_interval_bytes: None,
        frame_pointer_policy: "omitted",
        role: "control",
    },
    PprofTaxConfiguration {
        configuration_id: "fork-pprof-on-stopped",
        compiled_configuration_id: "fork-pprof-on",
        allocator_id: "mimalloc-pprof",
        pprof_compiled: true,
        pprof_active: false,
        sampling_interval_bytes: None,
        frame_pointer_policy: "cmake-mi-pprof-implicit",
        role: "control",
    },
    PprofTaxConfiguration {
        configuration_id: "fork-pprof-off-frame-pointers",
        compiled_configuration_id: "fork-pprof-off-frame-pointers",
        allocator_id: "mimalloc-pprof",
        pprof_compiled: false,
        pprof_active: false,
        sampling_interval_bytes: None,
        frame_pointer_policy: "forced-flag",
        role: "control",
    },
    PprofTaxConfiguration {
        configuration_id: "fork-pprof-sparse",
        compiled_configuration_id: "fork-pprof-on",
        allocator_id: "mimalloc-pprof",
        pprof_compiled: true,
        pprof_active: true,
        sampling_interval_bytes: Some(PPROF_TAX_SPARSE_INTERVAL_BYTES),
        frame_pointer_policy: "cmake-mi-pprof-implicit",
        role: "production-oriented",
    },
    PprofTaxConfiguration {
        configuration_id: "fork-pprof-aggressive",
        compiled_configuration_id: "fork-pprof-on",
        allocator_id: "mimalloc-pprof",
        pprof_compiled: true,
        pprof_active: true,
        sampling_interval_bytes: Some(PPROF_TAX_AGGRESSIVE_INTERVAL_BYTES),
        frame_pointer_policy: "cmake-mi-pprof-implicit",
        role: "aggressive",
    },
    PprofTaxConfiguration {
        configuration_id: "fork-pprof-rate-1-stress",
        compiled_configuration_id: "fork-pprof-on",
        allocator_id: "mimalloc-pprof",
        pprof_compiled: true,
        pprof_active: true,
        sampling_interval_bytes: Some(PPROF_TAX_STRESS_INTERVAL_BYTES),
        frame_pointer_policy: "cmake-mi-pprof-implicit",
        role: "stress-only",
    },
];

pub fn configuration(id: &str) -> Option<&'static PprofTaxConfiguration> {
    PPROF_TAX_CONFIGURATIONS
        .iter()
        .find(|entry| entry.configuration_id == id)
}

fn compiled_allocator_id(id: &str) -> Option<&'static str> {
    match id {
        "upstream-baseline" => Some("upstream-mimalloc"),
        "fork-pprof-off" | "fork-pprof-on" | "fork-pprof-off-frame-pointers" => {
            Some("mimalloc-pprof")
        }
        _ => None,
    }
}

fn compiled_pprof_compiled(id: &str) -> Option<bool> {
    match id {
        "upstream-baseline" | "fork-pprof-off" | "fork-pprof-off-frame-pointers" => Some(false),
        "fork-pprof-on" => Some(true),
        _ => None,
    }
}

fn compiled_frame_pointer_policy(id: &str) -> Option<&'static str> {
    match id {
        "upstream-baseline" | "fork-pprof-off" => Some("omitted"),
        "fork-pprof-on" => Some("cmake-mi-pprof-implicit"),
        "fork-pprof-off-frame-pointers" => Some("forced-flag"),
        _ => None,
    }
}

/// One of the six fixed comparisons in the panel.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PprofTaxComparisonSpec {
    pub comparison_id: &'static str,
    pub numerator_configuration_id: &'static str,
    pub denominator_configuration_id: &'static str,
    pub badge: &'static str,
    pub label: &'static str,
}

/// Fixed order; index 3 (`sparse-sampling-tax`) is the headline.
pub const PPROF_TAX_COMPARISONS: [PprofTaxComparisonSpec; 6] = [
    PprofTaxComparisonSpec {
        comparison_id: "fork-overlay-tax",
        numerator_configuration_id: "fork-pprof-off",
        denominator_configuration_id: "upstream-baseline",
        badge: "context",
        label: "Fork/overlay tax",
    },
    PprofTaxComparisonSpec {
        comparison_id: "instrumentation-tax",
        numerator_configuration_id: "fork-pprof-on-stopped",
        denominator_configuration_id: "fork-pprof-off",
        badge: "control",
        label: "Compiled-in instrumentation tax",
    },
    PprofTaxComparisonSpec {
        comparison_id: "frame-pointer-tax",
        numerator_configuration_id: "fork-pprof-off-frame-pointers",
        denominator_configuration_id: "fork-pprof-off",
        badge: "control",
        label: "Frame-pointer codegen tax",
    },
    PprofTaxComparisonSpec {
        comparison_id: "sparse-sampling-tax",
        numerator_configuration_id: "fork-pprof-sparse",
        denominator_configuration_id: "fork-pprof-on-stopped",
        badge: "production-oriented",
        label: "Production-oriented sampling tax (512 KiB)",
    },
    PprofTaxComparisonSpec {
        comparison_id: "aggressive-sampling-tax",
        numerator_configuration_id: "fork-pprof-aggressive",
        denominator_configuration_id: "fork-pprof-on-stopped",
        badge: "aggressive",
        label: "Aggressive sampling tax (4 KiB)",
    },
    PprofTaxComparisonSpec {
        comparison_id: "rate-1-stress",
        numerator_configuration_id: "fork-pprof-rate-1-stress",
        denominator_configuration_id: "fork-pprof-on-stopped",
        badge: "stress-only",
        label: "Rate-1 stress diagnostic",
    },
];

const HEADLINE_COMPARISON_INDEX: usize = 3;

pub fn throughput_overhead(ratio: f64) -> f64 {
    1.0 - ratio
}

pub fn latency_overhead(ratio: f64) -> f64 {
    ratio - 1.0
}

// ---------------------------------------------------------------------
// Manifest (build-equivalence) model and validation
// ---------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct PprofTaxToolchain {
    pub c_compiler: String,
    pub c_compiler_identity: String,
    pub linker_identity: String,
    pub cmake: String,
    pub ninja: String,
    pub rustc: String,
    pub cargo: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct PprofTaxIdentityProbe {
    pub configuration_id: String,
    pub allocator_id: String,
    pub allocator_version: String,
    pub source_sha: String,
    pub library_sha256: String,
    pub executable_sha256: String,
    pub pprof_compiled: bool,
    pub pprof_enabled: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct PprofTaxCompiledConfiguration {
    pub compiled_configuration_id: String,
    pub allocator_id: String,
    pub allocator_version: String,
    pub source_sha: String,
    pub pprof_compiled: bool,
    pub frame_pointer_policy: String,
    pub cmake_arguments: Vec<String>,
    pub cmake_cache: BTreeMap<String, Option<String>>,
    pub c_compiler_identity: String,
    pub linker_identity: String,
    pub static_library_sha256: String,
    pub executable_path: String,
    pub executable_sha256: String,
    pub identity_probe: PprofTaxIdentityProbe,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct PprofTaxManifest {
    pub manifest_schema_version: String,
    pub target: String,
    pub fork_source_sha: String,
    pub upstream_source_sha: String,
    pub upstream_archive_sha256: String,
    pub toolchain: PprofTaxToolchain,
    pub environment: BTreeMap<String, Option<String>>,
    pub compiled_configurations: Vec<PprofTaxCompiledConfiguration>,
}

fn is_lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

pub fn validate_manifest(manifest: &PprofTaxManifest) -> Result<(), String> {
    if manifest.manifest_schema_version != PPROF_TAX_MANIFEST_SCHEMA_VERSION {
        return Err("pprof-tax manifest has an unsupported schema version".into());
    }
    if manifest.compiled_configurations.len() != PPROF_TAX_COMPILED_CONFIGURATION_IDS.len() {
        return Err(
            "pprof-tax manifest does not carry exactly four compiled configurations".into(),
        );
    }
    for (expected_id, entry) in PPROF_TAX_COMPILED_CONFIGURATION_IDS
        .iter()
        .zip(&manifest.compiled_configurations)
    {
        if entry.compiled_configuration_id != *expected_id {
            return Err(format!(
                "pprof-tax manifest compiled configurations are not in canonical order (expected {expected_id})"
            ));
        }
    }
    if !is_lower_hex(&manifest.fork_source_sha, 40)
        || !is_lower_hex(&manifest.upstream_source_sha, 40)
        || !is_lower_hex(&manifest.upstream_archive_sha256, 64)
    {
        return Err("pprof-tax manifest has an invalid provenance digest".into());
    }
    if manifest.upstream_source_sha != PPROF_TAX_UPSTREAM_COMMIT {
        return Err(format!(
            "pprof-tax manifest upstream_source_sha does not match the pinned base {PPROF_TAX_UPSTREAM_COMMIT}"
        ));
    }
    let by_id: BTreeMap<&str, &PprofTaxCompiledConfiguration> = manifest
        .compiled_configurations
        .iter()
        .map(|entry| (entry.compiled_configuration_id.as_str(), entry))
        .collect();
    let upstream = by_id
        .get("upstream-baseline")
        .ok_or("pprof-tax manifest is missing upstream-baseline")?;
    if upstream.source_sha != PPROF_TAX_UPSTREAM_COMMIT
        || upstream.source_sha != manifest.upstream_source_sha
    {
        return Err(
            "pprof-tax manifest upstream-baseline source_sha does not match the pinned base".into(),
        );
    }
    for id in [
        "fork-pprof-off",
        "fork-pprof-on",
        "fork-pprof-off-frame-pointers",
    ] {
        let entry = by_id
            .get(id)
            .ok_or_else(|| format!("pprof-tax manifest is missing {id}"))?;
        if entry.source_sha != manifest.fork_source_sha {
            return Err(format!(
                "{id} does not share the manifest's fork_source_sha"
            ));
        }
    }
    for entry in &manifest.compiled_configurations {
        let expected_allocator = compiled_allocator_id(&entry.compiled_configuration_id)
            .ok_or_else(|| {
                format!(
                    "unknown pprof-tax compiled configuration id {}",
                    entry.compiled_configuration_id
                )
            })?;
        let expected_compiled = compiled_pprof_compiled(&entry.compiled_configuration_id)
            .expect("id already validated above");
        let expected_policy = compiled_frame_pointer_policy(&entry.compiled_configuration_id)
            .expect("id already validated above");
        if entry.allocator_id != expected_allocator
            || entry.pprof_compiled != expected_compiled
            || entry.frame_pointer_policy != expected_policy
        {
            return Err(format!(
                "{} does not match the compiled-id table",
                entry.compiled_configuration_id
            ));
        }
        let keys: BTreeSet<&str> = entry.cmake_cache.keys().map(String::as_str).collect();
        let expected_keys: BTreeSet<&str> = PPROF_TAX_EQUIVALENCE_CACHE_KEYS.into_iter().collect();
        if keys != expected_keys {
            return Err(format!(
                "{} cmake_cache does not carry exactly the equivalence cache keys",
                entry.compiled_configuration_id
            ));
        }
        if !is_lower_hex(&entry.source_sha, 40)
            || !is_lower_hex(&entry.static_library_sha256, 64)
            || !is_lower_hex(&entry.executable_sha256, 64)
        {
            return Err(format!(
                "{} has an invalid provenance digest",
                entry.compiled_configuration_id
            ));
        }
    }
    let compiler_identities: BTreeSet<&str> = manifest
        .compiled_configurations
        .iter()
        .map(|entry| entry.c_compiler_identity.as_str())
        .collect();
    let linker_identities: BTreeSet<&str> = manifest
        .compiled_configurations
        .iter()
        .map(|entry| entry.linker_identity.as_str())
        .collect();
    if compiler_identities.len() != 1 || linker_identities.len() != 1 {
        return Err(
            "pprof-tax manifest compiled configurations do not share one toolchain identity".into(),
        );
    }
    let executable_hashes: BTreeSet<&str> = manifest
        .compiled_configurations
        .iter()
        .map(|entry| entry.executable_sha256.as_str())
        .collect();
    if executable_hashes.len() != manifest.compiled_configurations.len() {
        return Err("pprof-tax manifest executable digests are not distinct".into());
    }
    for entry in &manifest.compiled_configurations {
        let probe = &entry.identity_probe;
        if probe.configuration_id != entry.compiled_configuration_id
            || probe.executable_sha256 != entry.executable_sha256
            || probe.library_sha256 != entry.static_library_sha256
            || probe.source_sha != entry.source_sha
            || probe.allocator_id != entry.allocator_id
            || probe.allocator_version != entry.allocator_version
            || probe.pprof_compiled != entry.pprof_compiled
            || probe.pprof_enabled
        {
            return Err(format!(
                "{} identity probe does not match its manifest entry",
                entry.compiled_configuration_id
            ));
        }
    }
    validate_flag_equivalence(&by_id)?;
    Ok(())
}

fn cache_value(entry: &PprofTaxCompiledConfiguration, key: &str) -> Option<String> {
    entry.cmake_cache.get(key).cloned().flatten()
}

fn validate_flag_equivalence(
    by_id: &BTreeMap<&str, &PprofTaxCompiledConfiguration>,
) -> Result<(), String> {
    let off = *by_id
        .get("fork-pprof-off")
        .ok_or("pprof-tax manifest is missing fork-pprof-off")?;
    let on = *by_id
        .get("fork-pprof-on")
        .ok_or("pprof-tax manifest is missing fork-pprof-on")?;
    let frame_pointers = *by_id
        .get("fork-pprof-off-frame-pointers")
        .ok_or("pprof-tax manifest is missing fork-pprof-off-frame-pointers")?;
    let upstream = *by_id
        .get("upstream-baseline")
        .ok_or("pprof-tax manifest is missing upstream-baseline")?;

    for key in PPROF_TAX_EQUIVALENCE_CACHE_KEYS {
        let off_value = cache_value(off, key);
        let on_value = cache_value(on, key);
        if key == "MI_PPROF" {
            if off_value.as_deref() != Some("OFF") || on_value.as_deref() != Some("ON") {
                return Err(
                    "fork-pprof-on/fork-pprof-off MI_PPROF values are not ON/OFF as expected"
                        .into(),
                );
            }
        } else if off_value != on_value {
            return Err(format!(
                "fork-pprof-on drifts from fork-pprof-off in disallowed key {key}"
            ));
        }
    }

    for key in PPROF_TAX_EQUIVALENCE_CACHE_KEYS {
        let off_value = cache_value(off, key);
        let fp_value = cache_value(frame_pointers, key);
        if key == "CMAKE_C_FLAGS_RELEASE" {
            let off_tokens: Vec<&str> = off_value
                .as_deref()
                .unwrap_or("")
                .split_whitespace()
                .collect();
            let fp_tokens: Vec<&str> = fp_value
                .as_deref()
                .unwrap_or("")
                .split_whitespace()
                .collect();
            let mut expected_tokens = off_tokens;
            expected_tokens.push("-fno-omit-frame-pointer");
            if fp_tokens != expected_tokens {
                return Err(
                    "fork-pprof-off-frame-pointers CMAKE_C_FLAGS_RELEASE is not fork-pprof-off plus exactly one -fno-omit-frame-pointer token"
                        .into(),
                );
            }
        } else if off_value != fp_value {
            return Err(format!(
                "fork-pprof-off-frame-pointers drifts from fork-pprof-off in disallowed key {key}"
            ));
        }
    }

    for key in PPROF_TAX_EQUIVALENCE_CACHE_KEYS {
        let upstream_value = cache_value(upstream, key);
        let off_value = cache_value(off, key);
        if PPROF_TAX_FORK_ONLY_CACHE_KEYS.contains(&key) {
            if upstream_value.is_some() {
                return Err(format!(
                    "upstream-baseline must not set fork-only key {key}"
                ));
            }
        } else if upstream_value != off_value {
            return Err(format!(
                "upstream-baseline drifts from fork-pprof-off in disallowed key {key}"
            ));
        }
    }
    Ok(())
}

// ---------------------------------------------------------------------
// Block orders
// ---------------------------------------------------------------------

/// One seeded Fisher-Yates permutation of the seven configuration IDs,
/// rotated left by `block % 7` for each block. Deterministic in `run_seed`.
pub fn block_orders(blocks: u32, run_seed: u64) -> Result<Vec<Vec<&'static str>>, String> {
    if blocks == 0 {
        return Err("pprof-tax block_orders requires at least one block".into());
    }
    let mut base: Vec<&'static str> = PPROF_TAX_CONFIGURATION_IDS.to_vec();
    let mut state = run_seed ^ BLOCK_ORDER_DOMAIN;
    for i in (1..base.len()).rev() {
        state = splitmix64(state);
        let j = (state % (i as u64 + 1)) as usize;
        base.swap(i, j);
    }
    let mut orders = Vec::with_capacity(blocks as usize);
    for block in 0..blocks {
        let mut order = base.clone();
        order.rotate_left((block % base.len() as u32) as usize);
        orders.push(order);
    }
    Ok(orders)
}

pub fn validate_block_orders(orders: &[Vec<String>]) -> Result<(), String> {
    if orders.is_empty() {
        return Err("pprof-tax block orders are empty".into());
    }
    let expected_ids: BTreeSet<&str> = PPROF_TAX_CONFIGURATION_IDS.into_iter().collect();
    let mut position_counts: BTreeMap<(String, usize), u32> = BTreeMap::new();
    for order in orders {
        let ids: BTreeSet<&str> = order.iter().map(String::as_str).collect();
        if order.len() != PPROF_TAX_CONFIGURATION_IDS.len() || ids != expected_ids {
            return Err(
                "pprof-tax block order is not a permutation of the seven configurations".into(),
            );
        }
        for (position, id) in order.iter().enumerate() {
            *position_counts.entry((id.clone(), position)).or_insert(0) += 1;
        }
    }
    for id in PPROF_TAX_CONFIGURATION_IDS {
        let counts: Vec<u32> = (0..PPROF_TAX_CONFIGURATION_IDS.len())
            .map(|position| {
                *position_counts
                    .get(&(id.to_string(), position))
                    .unwrap_or(&0)
            })
            .collect();
        let max = *counts.iter().max().unwrap();
        let min = *counts.iter().min().unwrap();
        if max - min > 1 {
            return Err(format!(
                "pprof-tax configuration {id} is not near-balanced across block positions"
            ));
        }
    }
    Ok(())
}

// ---------------------------------------------------------------------
// Raw run model
// ---------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct PprofTaxTopology {
    pub physical_cores: u32,
    pub logical_cores: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct PprofTaxRawCell {
    pub scenario_id: String,
    pub thread_point: String,
    pub thread_count: u32,
    pub operations_per_worker: u64,
    pub warmup_operations_per_worker: u64,
    pub calibration_elapsed_ns: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct PprofTaxRawSample {
    pub block_id: u32,
    pub position: u32,
    pub configuration_id: String,
    pub compiled_configuration_id: String,
    pub configuration_manifest_sha256: String,
    pub executable_sha256: String,
    pub pprof_compiled: bool,
    pub pprof_active: bool,
    pub sampling_interval_bytes: Option<u64>,
    pub frame_pointer_policy: String,
    pub scenario_id: String,
    pub thread_point: String,
    pub thread_count: u32,
    pub operations_per_worker: u64,
    pub workload_seed: u64,
    pub throughput_operations_per_second: Option<f64>,
    pub elapsed_ns: Option<u64>,
    pub operation_count: Option<u64>,
    pub allocation_calls: Option<u64>,
    pub checksum: Option<u64>,
    pub allocated_bytes_lower_bound: Option<u64>,
    pub peak_rss_bytes: Option<u64>,
    pub end_rss_delta_bytes: Option<i64>,
    pub profile_path: Option<String>,
    pub profile_sha256: Option<String>,
    pub profile_size_bytes: Option<u64>,
    pub sample_count: Option<u64>,
    pub sampled_bytes: Option<u64>,
    pub dropped_records: Option<u64>,
    pub profiler_arena_bytes: Option<u64>,
    pub interval_confirmed: Option<u64>,
    pub timed_out: bool,
    pub exit_code: Option<i32>,
    pub validity_status: String,
    pub invalid_reason: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct PprofTaxRawRun {
    pub raw_schema_version: String,
    pub metric_schema_version: String,
    pub mode: String,
    pub run: RunIdentity,
    pub runner: PublicationRunner,
    pub run_seed: u64,
    pub blocks: u32,
    pub configuration_manifest_sha256: String,
    pub manifest: PprofTaxManifest,
    pub topology: PprofTaxTopology,
    pub cells: Vec<PprofTaxRawCell>,
    pub block_orders: Vec<Vec<String>>,
    pub stress_budget_seconds: u64,
    pub samples: Vec<PprofTaxRawSample>,
}

/// Classify one raw sample in place. First matching reason wins.
pub fn classify_sample(sample: &mut PprofTaxRawSample, stress_budget_exhausted: bool) {
    let reason: Option<&'static str> = if stress_budget_exhausted {
        Some("stress-budget-exhausted")
    } else if sample.timed_out {
        Some("timeout")
    } else if matches!(sample.exit_code, Some(code) if code != 0)
        || sample.throughput_operations_per_second.is_none()
    {
        Some("child-failed")
    } else if sample.pprof_active && sample.interval_confirmed != sample.sampling_interval_bytes {
        Some("interval-not-confirmed")
    } else if sample.pprof_active && sample.profile_sha256.is_none() {
        Some("missing-profile")
    } else if sample.pprof_active
        && sample.sample_count == Some(0)
        && sample.allocated_bytes_lower_bound.unwrap_or(0)
            >= PPROF_TAX_ZERO_SAMPLE_CROSSING_FACTOR * sample.sampling_interval_bytes.unwrap_or(0)
    {
        Some("zero-samples-after-crossing-interval")
    } else if sample.pprof_active {
        let dropped = sample.dropped_records.unwrap_or(0);
        let counted = sample.sample_count.unwrap_or(0);
        let denominator = counted + dropped;
        if denominator > 0 && (dropped as f64 / denominator as f64) > PPROF_TAX_MAX_DROPPED_FRACTION
        {
            Some("dropped-records-above-threshold")
        } else {
            None
        }
    } else {
        None
    };
    match reason {
        Some(reason) => {
            sample.validity_status = "invalid".into();
            sample.invalid_reason = Some(reason.into());
        }
        None => {
            sample.validity_status = "valid".into();
            sample.invalid_reason = None;
        }
    }
}

pub fn validate_raw_sample(sample: &PprofTaxRawSample) -> Result<(), String> {
    let config = configuration(&sample.configuration_id).ok_or_else(|| {
        format!(
            "pprof-tax sample names an unknown configuration: {}",
            sample.configuration_id
        )
    })?;
    if sample.compiled_configuration_id != config.compiled_configuration_id
        || sample.pprof_compiled != config.pprof_compiled
        || sample.pprof_active != config.pprof_active
        || sample.frame_pointer_policy != config.frame_pointer_policy
        || sample.sampling_interval_bytes != config.sampling_interval_bytes
    {
        return Err(format!(
            "pprof-tax sample for {} does not match its configuration table entry",
            sample.configuration_id
        ));
    }
    if !matches!(sample.validity_status.as_str(), "valid" | "invalid") {
        return Err("pprof-tax sample has an unknown validity_status".into());
    }
    if sample.validity_status == "valid" && sample.invalid_reason.is_some() {
        return Err("a valid pprof-tax sample must not carry an invalid_reason".into());
    }
    if sample.validity_status == "invalid" && sample.invalid_reason.is_none() {
        return Err("an invalid pprof-tax sample must carry an invalid_reason".into());
    }
    if !is_lower_hex(&sample.executable_sha256, 64)
        || !is_lower_hex(&sample.configuration_manifest_sha256, 64)
    {
        return Err("pprof-tax sample has an invalid digest".into());
    }
    if let Some(profile_sha) = &sample.profile_sha256 {
        if !is_lower_hex(profile_sha, 64) {
            return Err("pprof-tax sample has an invalid profile digest".into());
        }
    }
    let active_profile_fields_present = sample.profile_sha256.is_some()
        || sample.profile_size_bytes.is_some()
        || sample.profile_path.is_some()
        || sample.sample_count.is_some()
        || sample.sampled_bytes.is_some()
        || sample.dropped_records.is_some()
        || sample.profiler_arena_bytes.is_some()
        || sample.interval_confirmed.is_some();
    if !sample.pprof_active
        && (active_profile_fields_present || sample.sample_count.unwrap_or(0) > 0)
    {
        return Err("a stopped or inactive pprof-tax sample must not claim profile data".into());
    }
    if sample.validity_status == "valid" {
        if sample.throughput_operations_per_second.is_none()
            || sample.elapsed_ns.is_none()
            || sample.operation_count.is_none()
        {
            return Err("a valid pprof-tax sample is missing timing fields".into());
        }
        let throughput = sample.throughput_operations_per_second.unwrap();
        if !throughput.is_finite() || throughput <= 0.0 {
            return Err(
                "a valid pprof-tax sample has non-finite or non-positive throughput".into(),
            );
        }
        if sample.pprof_active
            && (sample.profile_sha256.is_none()
                || sample.profile_size_bytes.is_none()
                || sample.profile_path.is_none()
                || sample.sample_count.is_none()
                || sample.sampled_bytes.is_none()
                || sample.dropped_records.is_none()
                || sample.profiler_arena_bytes.is_none()
                || sample.interval_confirmed.is_none())
        {
            return Err("a valid active pprof-tax sample is missing profile telemetry".into());
        }
    }
    Ok(())
}

pub fn validate_raw_run(raw: &PprofTaxRawRun) -> Result<(), String> {
    if raw.raw_schema_version != PPROF_TAX_RAW_SCHEMA_VERSION
        || raw.metric_schema_version != PPROF_TAX_SCHEMA_VERSION
    {
        return Err("pprof-tax raw run has an unsupported schema version".into());
    }
    match raw.mode.as_str() {
        "full" => {
            if raw.blocks < PPROF_TAX_MIN_BLOCKS {
                return Err(format!(
                    "pprof-tax full run needs at least {PPROF_TAX_MIN_BLOCKS} blocks"
                ));
            }
        }
        "smoke" => {
            if raw.blocks == 0 || raw.blocks >= PPROF_TAX_MIN_BLOCKS {
                return Err("pprof-tax smoke run must declare 1..15 blocks".into());
            }
        }
        _ => return Err("pprof-tax raw run has an unknown mode".into()),
    }
    validate_manifest(&raw.manifest)?;
    if !is_lower_hex(&raw.configuration_manifest_sha256, 64) {
        return Err("pprof-tax raw run has an invalid configuration manifest digest".into());
    }
    let topology = Topology {
        physical_cores: raw.topology.physical_cores as usize,
        logical_cores: raw.topology.logical_cores as usize,
    };
    let expected_cells = pprof_tax_cells(&topology)?;
    if raw.cells.len() != expected_cells.len() {
        return Err(
            "pprof-tax raw run cell count does not match the declared workload matrix".into(),
        );
    }
    for (expected, actual) in expected_cells.iter().zip(&raw.cells) {
        if actual.scenario_id != expected.scenario_id
            || actual.thread_point != expected.thread_point
            || actual.thread_count != expected.thread_count
            || actual.operations_per_worker == 0
        {
            return Err(
                "pprof-tax raw run cell does not match the declared workload matrix".into(),
            );
        }
    }
    validate_block_orders(&raw.block_orders)?;
    let expected_orders = block_orders(raw.blocks, raw.run_seed)?;
    if raw.block_orders.len() != expected_orders.len()
        || raw
            .block_orders
            .iter()
            .zip(expected_orders.iter())
            .any(|(actual, expected)| {
                actual.len() != expected.len()
                    || actual
                        .iter()
                        .zip(expected.iter())
                        .any(|(a, e)| a.as_str() != *e)
            })
    {
        return Err("pprof-tax raw run block orders do not match the seeded permutation".into());
    }
    let expected_total =
        raw.blocks as usize * expected_cells.len() * PPROF_TAX_CONFIGURATIONS.len();
    let mut seen: BTreeSet<(u32, String, String, String)> = BTreeSet::new();
    for sample in &raw.samples {
        validate_raw_sample(sample)?;
        if sample.configuration_manifest_sha256 != raw.configuration_manifest_sha256 {
            return Err("pprof-tax sample manifest digest does not match the raw run".into());
        }
        let compiled_entry = raw
            .manifest
            .compiled_configurations
            .iter()
            .find(|entry| entry.compiled_configuration_id == sample.compiled_configuration_id)
            .ok_or_else(|| {
                "pprof-tax sample names an unknown compiled configuration".to_string()
            })?;
        if sample.executable_sha256 != compiled_entry.executable_sha256 {
            return Err(
                "pprof-tax sample executable digest does not match its manifest entry".into(),
            );
        }
        let order = raw
            .block_orders
            .get(sample.block_id as usize)
            .ok_or_else(|| "pprof-tax sample names a block with no declared order".to_string())?;
        let expected_position = order
            .iter()
            .position(|id| id == &sample.configuration_id)
            .ok_or_else(|| {
                "pprof-tax sample configuration is not in its block's order".to_string()
            })?;
        if sample.position != expected_position as u32 {
            return Err("pprof-tax sample position does not match its block order".into());
        }
        if !expected_cells.iter().any(|cell| {
            cell.scenario_id == sample.scenario_id
                && cell.thread_point == sample.thread_point
                && cell.thread_count == sample.thread_count
        }) {
            return Err("pprof-tax sample names an undeclared cell".into());
        }
        if sample.invalid_reason.as_deref() == Some("stress-budget-exhausted")
            && sample.configuration_id != "fork-pprof-rate-1-stress"
        {
            return Err(
                "only the rate-1 stress configuration may report stress-budget-exhausted".into(),
            );
        }
        let key = (
            sample.block_id,
            sample.scenario_id.clone(),
            sample.thread_point.clone(),
            sample.configuration_id.clone(),
        );
        if !seen.insert(key) {
            return Err(
                "pprof-tax raw run has a duplicate (block, cell, configuration) sample".into(),
            );
        }
    }
    if raw.samples.len() != expected_total || seen.len() != expected_total {
        return Err(
            "pprof-tax raw run does not contain exactly one sample per (block, cell, configuration)"
                .into(),
        );
    }
    Ok(())
}

// ---------------------------------------------------------------------
// Report model
// ---------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct PprofTaxComparison {
    pub comparison_id: String,
    pub label: String,
    pub numerator_configuration_id: String,
    pub denominator_configuration_id: String,
    pub scenario_id: Option<String>,
    pub thread_point: Option<String>,
    pub metric_id: String,
    pub badge: String,
    pub support_status: String,
    pub valid_block_count: u32,
    pub ratio: Option<f64>,
    pub ratio_lower: Option<f64>,
    pub ratio_upper: Option<f64>,
    pub overhead: Option<f64>,
    pub overhead_lower: Option<f64>,
    pub overhead_upper: Option<f64>,
    pub headline_eligible: bool,
    pub reason: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct PprofTaxConfigurationSummary {
    pub configuration_id: String,
    pub compiled_configuration_id: String,
    pub role: String,
    pub pprof_compiled: bool,
    pub pprof_active: bool,
    pub sampling_interval_bytes: Option<u64>,
    pub frame_pointer_policy: String,
    pub support_status: String,
    pub unsupported_reason: Option<String>,
    pub executable_sha256: Option<String>,
    pub valid_samples: u64,
    pub invalid_samples: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct PprofTaxCellSummary {
    pub scenario_id: String,
    pub thread_point: String,
    pub thread_count: u32,
    pub operations_per_worker: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct PprofTaxIntervals {
    pub sparse_bytes: u64,
    pub sparse_rationale: String,
    pub aggressive_bytes: u64,
    pub stress_bytes: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct PprofTaxActiveTelemetry {
    pub configuration_id: String,
    pub scenario_id: String,
    pub thread_point: String,
    pub valid_runs: u64,
    pub invalid_runs: u64,
    pub median_sample_count: Option<u64>,
    pub median_sampled_bytes: Option<u64>,
    pub max_dropped_records: Option<u64>,
    pub max_profiler_arena_bytes: Option<u64>,
    pub median_profile_size_bytes: Option<u64>,
    pub validity_status: String,
    pub invalid_reasons: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct PprofTaxRssSummary {
    pub configuration_id: String,
    pub scenario_id: String,
    pub thread_point: String,
    pub median_peak_rss_bytes: Option<u64>,
    pub median_end_rss_delta_bytes: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct PprofTaxLatencyNote {
    pub status: String,
    pub reason: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct PprofTaxMetricReport {
    pub metric_schema_version: String,
    pub status: String,
    pub mode: String,
    pub metric_comparison_key: String,
    pub run: RunIdentity,
    pub runner: PublicationRunner,
    pub run_seed: u64,
    pub blocks: u32,
    pub minimum_paired_blocks: u32,
    pub hosted_runner_scope: String,
    pub configuration_manifest_sha256: String,
    pub raw_artifact_sha256: String,
    pub raw_artifact_name: String,
    pub fork_source_sha: String,
    pub upstream_source_sha: String,
    pub intervals: PprofTaxIntervals,
    pub configurations: Vec<PprofTaxConfigurationSummary>,
    pub cells: Vec<PprofTaxCellSummary>,
    pub comparisons: Vec<PprofTaxComparison>,
    pub cell_comparisons: Vec<PprofTaxComparison>,
    pub headline: PprofTaxComparison,
    pub active_telemetry: Vec<PprofTaxActiveTelemetry>,
    pub rss_summaries: Vec<PprofTaxRssSummary>,
    pub latency: PprofTaxLatencyNote,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct PprofTaxHistoryReport {
    pub metric_schema_version: String,
    pub status: String,
    pub mode: String,
    pub metric_comparison_key: String,
    pub run: RunIdentity,
    pub runner_fingerprint_sha256: String,
    pub run_seed: u64,
    pub blocks: u32,
    pub minimum_paired_blocks: u32,
    pub hosted_runner_scope: String,
    pub configuration_manifest_sha256: String,
    pub raw_artifact_sha256: String,
    pub raw_artifact_name: String,
    pub fork_source_sha: String,
    pub upstream_source_sha: String,
    pub intervals: PprofTaxIntervals,
    pub comparisons: Vec<PprofTaxComparison>,
    pub headline: PprofTaxComparison,
    pub latency: PprofTaxLatencyNote,
}

impl PprofTaxMetricReport {
    pub fn history_projection(&self) -> PprofTaxHistoryReport {
        PprofTaxHistoryReport {
            metric_schema_version: self.metric_schema_version.clone(),
            status: self.status.clone(),
            mode: self.mode.clone(),
            metric_comparison_key: self.metric_comparison_key.clone(),
            run: self.run.clone(),
            runner_fingerprint_sha256: self.runner.fingerprint_sha256.clone(),
            run_seed: self.run_seed,
            blocks: self.blocks,
            minimum_paired_blocks: self.minimum_paired_blocks,
            hosted_runner_scope: self.hosted_runner_scope.clone(),
            configuration_manifest_sha256: self.configuration_manifest_sha256.clone(),
            raw_artifact_sha256: self.raw_artifact_sha256.clone(),
            raw_artifact_name: self.raw_artifact_name.clone(),
            fork_source_sha: self.fork_source_sha.clone(),
            upstream_source_sha: self.upstream_source_sha.clone(),
            intervals: self.intervals.clone(),
            comparisons: self.comparisons.clone(),
            headline: self.headline.clone(),
            latency: self.latency.clone(),
        }
    }
}

// ---------------------------------------------------------------------
// Report construction
// ---------------------------------------------------------------------

fn configuration_support(
    manifest: &PprofTaxManifest,
    configuration_id: &str,
) -> (&'static str, Option<String>) {
    if configuration_id == "fork-pprof-off-frame-pointers" {
        let target = manifest.target.as_str();
        let supported =
            target.contains("linux") || target.contains("darwin") || target.contains("apple");
        if !supported {
            return (
                "unsupported",
                Some(format!(
                    "the frame-pointer forcing flag is not evaluated on target {target}"
                )),
            );
        }
    }
    ("supported", None)
}

fn median_u64(values: &mut [u64]) -> Option<u64> {
    if values.is_empty() {
        return None;
    }
    values.sort_unstable();
    Some(values[(values.len() - 1) / 2])
}

fn median_i64(values: &mut [i64]) -> Option<i64> {
    if values.is_empty() {
        return None;
    }
    values.sort_unstable();
    Some(values[(values.len() - 1) / 2])
}

fn find_invalid_reason(
    raw: &PprofTaxRawRun,
    configuration_ids: [&str; 2],
    scenario_id: Option<&str>,
    thread_point: Option<&str>,
) -> Option<String> {
    raw.samples
        .iter()
        .filter(|sample| configuration_ids.contains(&sample.configuration_id.as_str()))
        .filter(|sample| scenario_id.is_none_or(|id| sample.scenario_id == id))
        .filter(|sample| thread_point.is_none_or(|point| sample.thread_point == point))
        .find(|sample| sample.validity_status == "invalid")
        .and_then(|sample| sample.invalid_reason.clone())
}

#[allow(clippy::too_many_arguments)]
fn build_comparison(
    raw: &PprofTaxRawRun,
    configurations: &[PprofTaxConfigurationSummary],
    spec: &PprofTaxComparisonSpec,
    scenario_id: Option<&str>,
    thread_point: Option<&str>,
    cell_id: &str,
    numerator_series: Option<&BTreeMap<u32, f64>>,
    denominator_series: Option<&BTreeMap<u32, f64>>,
) -> Result<PprofTaxComparison, String> {
    let numerator_config = configurations
        .iter()
        .find(|entry| entry.configuration_id == spec.numerator_configuration_id);
    let denominator_config = configurations
        .iter()
        .find(|entry| entry.configuration_id == spec.denominator_configuration_id);
    let unsupported_reason = [numerator_config, denominator_config]
        .into_iter()
        .flatten()
        .find(|entry| entry.support_status != "supported")
        .and_then(|entry| entry.unsupported_reason.clone());
    let headline_eligible_candidate =
        spec.comparison_id == "sparse-sampling-tax" && scenario_id.is_none();

    if let Some(reason) = unsupported_reason {
        return Ok(PprofTaxComparison {
            comparison_id: spec.comparison_id.into(),
            label: spec.label.into(),
            numerator_configuration_id: spec.numerator_configuration_id.into(),
            denominator_configuration_id: spec.denominator_configuration_id.into(),
            scenario_id: scenario_id.map(String::from),
            thread_point: thread_point.map(String::from),
            metric_id: PPROF_TAX_METRIC_ID.into(),
            badge: spec.badge.into(),
            support_status: "unsupported".into(),
            valid_block_count: 0,
            ratio: None,
            ratio_lower: None,
            ratio_upper: None,
            overhead: None,
            overhead_lower: None,
            overhead_upper: None,
            headline_eligible: false,
            reason: Some(reason),
        });
    }

    let paired = match (numerator_series, denominator_series) {
        (Some(numerator), Some(denominator)) => {
            let mut observations = Vec::new();
            for (&block_id, &value) in numerator {
                if let Some(&reference) = denominator.get(&block_id) {
                    observations.push(MetricObservation {
                        block_id,
                        allocator_id: spec.numerator_configuration_id.into(),
                        value,
                    });
                    observations.push(MetricObservation {
                        block_id,
                        allocator_id: spec.denominator_configuration_id.into(),
                        value: reference,
                    });
                }
            }
            if observations.is_empty() {
                None
            } else {
                Some(
                    summarize_paired(
                        raw.run_seed,
                        cell_id,
                        spec.numerator_configuration_id,
                        spec.denominator_configuration_id,
                        MetricDirection::HigherIsBetter,
                        &observations,
                    )
                    .map_err(|error| error.to_string())?,
                )
            }
        }
        _ => None,
    };

    let valid_block_count = paired
        .as_ref()
        .map(|summary| summary.block_count as u32)
        .unwrap_or(0);
    if let Some(summary) = paired.filter(|_| valid_block_count >= PPROF_TAX_MIN_BLOCKS) {
        let ratio = summary.effect;
        let ratio_lower = summary.confidence_interval.lower;
        let ratio_upper = summary.confidence_interval.upper;
        Ok(PprofTaxComparison {
            comparison_id: spec.comparison_id.into(),
            label: spec.label.into(),
            numerator_configuration_id: spec.numerator_configuration_id.into(),
            denominator_configuration_id: spec.denominator_configuration_id.into(),
            scenario_id: scenario_id.map(String::from),
            thread_point: thread_point.map(String::from),
            metric_id: PPROF_TAX_METRIC_ID.into(),
            badge: spec.badge.into(),
            support_status: "supported".into(),
            valid_block_count,
            ratio: Some(ratio),
            ratio_lower: Some(ratio_lower),
            ratio_upper: Some(ratio_upper),
            overhead: Some(throughput_overhead(ratio)),
            overhead_lower: Some(throughput_overhead(ratio_upper)),
            overhead_upper: Some(throughput_overhead(ratio_lower)),
            headline_eligible: headline_eligible_candidate,
            reason: None,
        })
    } else {
        let invalid_reason = find_invalid_reason(
            raw,
            [
                spec.numerator_configuration_id,
                spec.denominator_configuration_id,
            ],
            scenario_id,
            thread_point,
        );
        let (support_status, reason) = match invalid_reason {
            Some(reason) => ("invalid".to_string(), Some(reason)),
            None => (
                "insufficient-paired-blocks".to_string(),
                Some(format!(
                    "only {valid_block_count} paired blocks are available; {PPROF_TAX_MIN_BLOCKS} are required"
                )),
            ),
        };
        Ok(PprofTaxComparison {
            comparison_id: spec.comparison_id.into(),
            label: spec.label.into(),
            numerator_configuration_id: spec.numerator_configuration_id.into(),
            denominator_configuration_id: spec.denominator_configuration_id.into(),
            scenario_id: scenario_id.map(String::from),
            thread_point: thread_point.map(String::from),
            metric_id: PPROF_TAX_METRIC_ID.into(),
            badge: spec.badge.into(),
            support_status,
            valid_block_count,
            ratio: None,
            ratio_lower: None,
            ratio_upper: None,
            overhead: None,
            overhead_lower: None,
            overhead_upper: None,
            headline_eligible: false,
            reason,
        })
    }
}

fn metric_comparison_key(raw: &PprofTaxRawRun) -> Result<String, String> {
    #[derive(Serialize)]
    struct Key<'a> {
        metric_schema_version: &'a str,
        workloads: &'a [&'a str],
        configuration_ids: [&'a str; 7],
        compiled_configuration_ids: [&'a str; 4],
        sparse_bytes: u64,
        aggressive_bytes: u64,
        stress_bytes: u64,
        upstream_source_sha: &'a str,
        statistics_version: &'a str,
    }
    let value = Key {
        metric_schema_version: PPROF_TAX_SCHEMA_VERSION,
        workloads: &PPROF_TAX_WORKLOADS,
        configuration_ids: PPROF_TAX_CONFIGURATION_IDS,
        compiled_configuration_ids: PPROF_TAX_COMPILED_CONFIGURATION_IDS,
        sparse_bytes: PPROF_TAX_SPARSE_INTERVAL_BYTES,
        aggressive_bytes: PPROF_TAX_AGGRESSIVE_INTERVAL_BYTES,
        stress_bytes: PPROF_TAX_STRESS_INTERVAL_BYTES,
        upstream_source_sha: raw.manifest.upstream_source_sha.as_str(),
        statistics_version: STATISTICS_VERSION,
    };
    serde_json::to_vec(&value)
        .map(|bytes| sha256_bytes(&bytes))
        .map_err(|error| error.to_string())
}

pub fn build_pprof_tax_report(
    raw: &PprofTaxRawRun,
    raw_artifact_sha256: &str,
    raw_artifact_name: &str,
) -> Result<PprofTaxMetricReport, String> {
    validate_raw_run(raw)?;
    if raw.mode != "full" {
        return Err("only a full-mode pprof-tax run can be published".into());
    }
    if !is_lower_hex(raw_artifact_sha256, 64) || raw_artifact_name.is_empty() {
        return Err(
            "pprof-tax report requires a valid raw artifact digest and a non-empty name".into(),
        );
    }

    let mut configurations = Vec::with_capacity(PPROF_TAX_CONFIGURATIONS.len());
    for spec in &PPROF_TAX_CONFIGURATIONS {
        let (support_status, unsupported_reason) =
            configuration_support(&raw.manifest, spec.configuration_id);
        // An unsupported configuration publishes no executable digest: nothing it
        // could have measured is attributed to a binary (ci/benchmark_report.py
        // enforces the same rule on the published section).
        let executable_sha256 = if support_status == "supported" {
            raw.manifest
                .compiled_configurations
                .iter()
                .find(|entry| entry.compiled_configuration_id == spec.compiled_configuration_id)
                .map(|entry| entry.executable_sha256.clone())
        } else {
            None
        };
        let valid_samples = raw
            .samples
            .iter()
            .filter(|sample| {
                sample.configuration_id == spec.configuration_id
                    && sample.validity_status == "valid"
            })
            .count() as u64;
        let invalid_samples = raw
            .samples
            .iter()
            .filter(|sample| {
                sample.configuration_id == spec.configuration_id
                    && sample.validity_status == "invalid"
            })
            .count() as u64;
        configurations.push(PprofTaxConfigurationSummary {
            configuration_id: spec.configuration_id.into(),
            compiled_configuration_id: spec.compiled_configuration_id.into(),
            role: spec.role.into(),
            pprof_compiled: spec.pprof_compiled,
            pprof_active: spec.pprof_active,
            sampling_interval_bytes: spec.sampling_interval_bytes,
            frame_pointer_policy: spec.frame_pointer_policy.into(),
            support_status: support_status.into(),
            unsupported_reason,
            executable_sha256,
            valid_samples,
            invalid_samples,
        });
    }

    let cells: Vec<PprofTaxCellSummary> = raw
        .cells
        .iter()
        .map(|cell| PprofTaxCellSummary {
            scenario_id: cell.scenario_id.clone(),
            thread_point: cell.thread_point.clone(),
            thread_count: cell.thread_count,
            operations_per_worker: cell.operations_per_worker,
        })
        .collect();

    // (configuration, block) -> (scenario, thread point) -> throughput.
    type CellThroughput<'a> = BTreeMap<(&'a str, &'a str), f64>;
    let mut per_config_block: BTreeMap<(&str, u32), CellThroughput> = BTreeMap::new();
    let mut per_cell_values: BTreeMap<(&str, &str, &str), BTreeMap<u32, f64>> = BTreeMap::new();
    for sample in &raw.samples {
        if sample.validity_status != "valid" {
            continue;
        }
        let throughput = sample.throughput_operations_per_second.unwrap();
        per_config_block
            .entry((sample.configuration_id.as_str(), sample.block_id))
            .or_default()
            .insert(
                (sample.scenario_id.as_str(), sample.thread_point.as_str()),
                throughput,
            );
        per_cell_values
            .entry((
                sample.scenario_id.as_str(),
                sample.thread_point.as_str(),
                sample.configuration_id.as_str(),
            ))
            .or_default()
            .insert(sample.block_id, throughput);
    }

    let cell_keys: Vec<(&str, &str)> = raw
        .cells
        .iter()
        .map(|cell| (cell.scenario_id.as_str(), cell.thread_point.as_str()))
        .collect();
    let mut aggregate_values: BTreeMap<&str, BTreeMap<u32, f64>> = BTreeMap::new();
    for spec in &PPROF_TAX_CONFIGURATIONS {
        for block in 0..raw.blocks {
            if let Some(cell_map) = per_config_block.get(&(spec.configuration_id, block)) {
                if cell_keys.iter().all(|key| cell_map.contains_key(key)) {
                    let mean_log = cell_keys.iter().map(|key| cell_map[key].ln()).sum::<f64>()
                        / cell_keys.len() as f64;
                    aggregate_values
                        .entry(spec.configuration_id)
                        .or_default()
                        .insert(block, mean_log.exp());
                }
            }
        }
    }

    let mut comparisons = Vec::with_capacity(PPROF_TAX_COMPARISONS.len());
    for spec in &PPROF_TAX_COMPARISONS {
        let cell_id = format!("pprof-tax/aggregate/{}", spec.comparison_id);
        let comparison = build_comparison(
            raw,
            &configurations,
            spec,
            None,
            None,
            &cell_id,
            aggregate_values.get(spec.numerator_configuration_id),
            aggregate_values.get(spec.denominator_configuration_id),
        )?;
        comparisons.push(comparison);
    }

    let headline = comparisons[HEADLINE_COMPARISON_INDEX].clone();
    if headline.support_status != "supported" || !headline.headline_eligible {
        return Err(
            "pprof-tax headline (sparse-sampling-tax) comparison is not supported; refusing to publish"
                .into(),
        );
    }

    let mut cell_comparisons = Vec::with_capacity(raw.cells.len() * PPROF_TAX_COMPARISONS.len());
    for cell in &raw.cells {
        for spec in &PPROF_TAX_COMPARISONS {
            let cell_id = format!(
                "pprof-tax/{}/{}/{}",
                cell.scenario_id, cell.thread_point, spec.comparison_id
            );
            let numerator_series = per_cell_values.get(&(
                cell.scenario_id.as_str(),
                cell.thread_point.as_str(),
                spec.numerator_configuration_id,
            ));
            let denominator_series = per_cell_values.get(&(
                cell.scenario_id.as_str(),
                cell.thread_point.as_str(),
                spec.denominator_configuration_id,
            ));
            let comparison = build_comparison(
                raw,
                &configurations,
                spec,
                Some(cell.scenario_id.as_str()),
                Some(cell.thread_point.as_str()),
                &cell_id,
                numerator_series,
                denominator_series,
            )?;
            cell_comparisons.push(comparison);
        }
    }

    let active_config_ids: Vec<&str> = PPROF_TAX_CONFIGURATIONS
        .iter()
        .filter(|spec| spec.pprof_active)
        .map(|spec| spec.configuration_id)
        .collect();
    let mut active_telemetry = Vec::new();
    for configuration_id in &active_config_ids {
        for cell in &raw.cells {
            let matching: Vec<&PprofTaxRawSample> = raw
                .samples
                .iter()
                .filter(|sample| {
                    sample.configuration_id == *configuration_id
                        && sample.scenario_id == cell.scenario_id
                        && sample.thread_point == cell.thread_point
                })
                .collect();
            let valid_runs = matching
                .iter()
                .filter(|sample| sample.validity_status == "valid")
                .count() as u64;
            let invalid_runs = matching
                .iter()
                .filter(|sample| sample.validity_status == "invalid")
                .count() as u64;
            let mut sample_counts: Vec<u64> = matching
                .iter()
                .filter_map(|sample| sample.sample_count)
                .collect();
            let mut sampled_bytes: Vec<u64> = matching
                .iter()
                .filter_map(|sample| sample.sampled_bytes)
                .collect();
            let dropped: Vec<u64> = matching
                .iter()
                .filter_map(|sample| sample.dropped_records)
                .collect();
            let arena: Vec<u64> = matching
                .iter()
                .filter_map(|sample| sample.profiler_arena_bytes)
                .collect();
            let mut profile_sizes: Vec<u64> = matching
                .iter()
                .filter_map(|sample| sample.profile_size_bytes)
                .collect();
            let invalid_reasons: Vec<String> = matching
                .iter()
                .filter_map(|sample| sample.invalid_reason.clone())
                .collect();
            let validity_status = if invalid_runs == 0 {
                "valid"
            } else {
                "invalid"
            };
            active_telemetry.push(PprofTaxActiveTelemetry {
                configuration_id: (*configuration_id).into(),
                scenario_id: cell.scenario_id.clone(),
                thread_point: cell.thread_point.clone(),
                valid_runs,
                invalid_runs,
                median_sample_count: median_u64(&mut sample_counts),
                median_sampled_bytes: median_u64(&mut sampled_bytes),
                max_dropped_records: dropped.iter().copied().max(),
                max_profiler_arena_bytes: arena.iter().copied().max(),
                median_profile_size_bytes: median_u64(&mut profile_sizes),
                validity_status: validity_status.into(),
                invalid_reasons,
            });
        }
    }

    let mut rss_summaries = Vec::new();
    for spec in &PPROF_TAX_CONFIGURATIONS {
        for cell in &raw.cells {
            let matching: Vec<&PprofTaxRawSample> = raw
                .samples
                .iter()
                .filter(|sample| {
                    sample.configuration_id == spec.configuration_id
                        && sample.scenario_id == cell.scenario_id
                        && sample.thread_point == cell.thread_point
                })
                .collect();
            let mut peak: Vec<u64> = matching
                .iter()
                .filter_map(|sample| sample.peak_rss_bytes)
                .collect();
            let mut delta: Vec<i64> = matching
                .iter()
                .filter_map(|sample| sample.end_rss_delta_bytes)
                .collect();
            rss_summaries.push(PprofTaxRssSummary {
                configuration_id: spec.configuration_id.into(),
                scenario_id: cell.scenario_id.clone(),
                thread_point: cell.thread_point.clone(),
                median_peak_rss_bytes: median_u64(&mut peak),
                median_end_rss_delta_bytes: median_i64(&mut delta),
            });
        }
    }

    let key = metric_comparison_key(raw)?;

    Ok(PprofTaxMetricReport {
        metric_schema_version: PPROF_TAX_SCHEMA_VERSION.into(),
        status: "valid".into(),
        mode: "full".into(),
        metric_comparison_key: key,
        run: raw.run.clone(),
        runner: raw.runner.clone(),
        run_seed: raw.run_seed,
        blocks: raw.blocks,
        minimum_paired_blocks: PPROF_TAX_MIN_BLOCKS,
        hosted_runner_scope: PPROF_TAX_SCOPE_WARNING.into(),
        configuration_manifest_sha256: raw.configuration_manifest_sha256.clone(),
        raw_artifact_sha256: raw_artifact_sha256.into(),
        raw_artifact_name: raw_artifact_name.into(),
        fork_source_sha: raw.manifest.fork_source_sha.clone(),
        upstream_source_sha: raw.manifest.upstream_source_sha.clone(),
        intervals: PprofTaxIntervals {
            sparse_bytes: PPROF_TAX_SPARSE_INTERVAL_BYTES,
            sparse_rationale: PPROF_TAX_SPARSE_RATIONALE.into(),
            aggressive_bytes: PPROF_TAX_AGGRESSIVE_INTERVAL_BYTES,
            stress_bytes: PPROF_TAX_STRESS_INTERVAL_BYTES,
        },
        configurations,
        cells,
        comparisons,
        cell_comparisons,
        headline,
        active_telemetry,
        rss_summaries,
        latency: PprofTaxLatencyNote {
            status: "not-collected".into(),
            reason: "Phase 6B transaction latency uses its own child protocol; per-transaction timing would change this panel's timed boundary".into(),
        },
    })
}

pub fn validate_pprof_tax_report(report: &PprofTaxMetricReport) -> Result<(), String> {
    if report.metric_schema_version != PPROF_TAX_SCHEMA_VERSION
        || report.status != "valid"
        || report.mode != "full"
        || !is_lower_hex(&report.metric_comparison_key, 64)
        || report.minimum_paired_blocks != PPROF_TAX_MIN_BLOCKS
        || report.blocks < PPROF_TAX_MIN_BLOCKS
        || report.hosted_runner_scope != PPROF_TAX_SCOPE_WARNING
        || !is_lower_hex(&report.configuration_manifest_sha256, 64)
        || !is_lower_hex(&report.raw_artifact_sha256, 64)
        || report.raw_artifact_name.is_empty()
        || !is_lower_hex(&report.fork_source_sha, 40)
        || report.upstream_source_sha != PPROF_TAX_UPSTREAM_COMMIT
        || report.intervals.sparse_bytes != PPROF_TAX_SPARSE_INTERVAL_BYTES
        || report.intervals.sparse_rationale != PPROF_TAX_SPARSE_RATIONALE
        || report.intervals.aggressive_bytes != PPROF_TAX_AGGRESSIVE_INTERVAL_BYTES
        || report.intervals.stress_bytes != PPROF_TAX_STRESS_INTERVAL_BYTES
    {
        return Err("pprof-tax report failed a header-level check".into());
    }
    if report.configurations.len() != PPROF_TAX_CONFIGURATIONS.len() {
        return Err("pprof-tax report does not carry exactly seven configurations".into());
    }
    for (expected, actual) in PPROF_TAX_CONFIGURATIONS.iter().zip(&report.configurations) {
        if actual.configuration_id != expected.configuration_id
            || actual.compiled_configuration_id != expected.compiled_configuration_id
            || actual.role != expected.role
            || actual.pprof_compiled != expected.pprof_compiled
            || actual.pprof_active != expected.pprof_active
            || actual.sampling_interval_bytes != expected.sampling_interval_bytes
            || actual.frame_pointer_policy != expected.frame_pointer_policy
            || !matches!(actual.support_status.as_str(), "supported" | "unsupported")
            || (actual.support_status == "unsupported") != actual.unsupported_reason.is_some()
            || (actual.support_status == "supported")
                != actual
                    .executable_sha256
                    .as_deref()
                    .is_some_and(|digest| is_lower_hex(digest, 64))
        {
            return Err(format!(
                "pprof-tax configuration {} is invalid or out of order",
                expected.configuration_id
            ));
        }
    }
    if report.comparisons.len() != PPROF_TAX_COMPARISONS.len() {
        return Err("pprof-tax report does not carry exactly six comparisons".into());
    }
    for (expected, actual) in PPROF_TAX_COMPARISONS.iter().zip(&report.comparisons) {
        if actual.comparison_id != expected.comparison_id
            || actual.label != expected.label
            || actual.numerator_configuration_id != expected.numerator_configuration_id
            || actual.denominator_configuration_id != expected.denominator_configuration_id
            || actual.badge != expected.badge
            || actual.metric_id != PPROF_TAX_METRIC_ID
            || actual.scenario_id.is_some()
            || actual.thread_point.is_some()
        {
            return Err(format!(
                "pprof-tax comparison {} does not match its fixed spec",
                expected.comparison_id
            ));
        }
    }
    match report
        .comparisons
        .iter()
        .find(|comparison| comparison.comparison_id == "frame-pointer-tax")
    {
        None => {
            return Err("pprof-tax report is missing the frame-pointer-tax comparison".into());
        }
        // Mirrors ci/benchmark_report.py: the frame-pointer control must resolve to a
        // number or an explicit platform `unsupported`, never a silent gap.
        Some(comparison)
            if !matches!(
                comparison.support_status.as_str(),
                "supported" | "unsupported"
            ) =>
        {
            return Err(
                "pprof-tax frame-pointer-tax must resolve to supported or unsupported".into(),
            );
        }
        Some(_) => {}
    }
    if report.headline != report.comparisons[HEADLINE_COMPARISON_INDEX] {
        return Err(
            "pprof-tax headline must be a clone of the aggregate sparse-sampling-tax comparison"
                .into(),
        );
    }
    if report.headline.support_status != "supported"
        || !report.headline.headline_eligible
        || report.headline.valid_block_count < PPROF_TAX_MIN_BLOCKS
    {
        return Err(
            "pprof-tax headline sparse-sampling-tax must be supported with at least the minimum paired blocks"
                .into(),
        );
    }
    let headline_eligible_count = report
        .comparisons
        .iter()
        .chain(report.cell_comparisons.iter())
        .filter(|comparison| comparison.headline_eligible)
        .count();
    if headline_eligible_count != 1 {
        return Err("exactly one pprof-tax comparison may be headline eligible".into());
    }
    for comparison in report
        .comparisons
        .iter()
        .chain(report.cell_comparisons.iter())
    {
        if comparison.headline_eligible && comparison.comparison_id != "sparse-sampling-tax" {
            return Err("only sparse-sampling-tax may be headline eligible".into());
        }
        if comparison.headline_eligible && comparison.scenario_id.is_some() {
            return Err("only the aggregate sparse-sampling-tax may be headline eligible".into());
        }
        if comparison.comparison_id == "rate-1-stress" && comparison.headline_eligible {
            return Err("rate-1-stress must never be headline eligible".into());
        }
        let has_numbers = comparison.ratio.is_some();
        if has_numbers != (comparison.support_status == "supported") {
            return Err(
                "pprof-tax comparison numeric fields must be present if and only if it is supported"
                    .into(),
            );
        }
        if let (Some(ratio), Some(overhead)) = (comparison.ratio, comparison.overhead) {
            if (overhead - (1.0 - ratio)).abs() > 1e-9 {
                return Err("pprof-tax comparison overhead must equal 1 - ratio".into());
            }
        }
        if let (Some(lower), Some(upper)) = (comparison.ratio_lower, comparison.ratio_upper) {
            if lower > upper {
                return Err("pprof-tax comparison ratio interval is inverted".into());
            }
            // Same rule ci/benchmark_report.py enforces on the published section.
            if let Some(ratio) = comparison.ratio {
                if ratio < lower || ratio > upper {
                    return Err(
                        "pprof-tax comparison ratio lies outside its own confidence interval"
                            .into(),
                    );
                }
            }
        }
        if !matches!(
            comparison.support_status.as_str(),
            "supported" | "unsupported" | "invalid" | "insufficient-paired-blocks"
        ) {
            return Err("pprof-tax comparison has an unknown support_status".into());
        }
    }
    if report.cells.is_empty() {
        return Err("pprof-tax report has no cells".into());
    }
    if report.cell_comparisons.len() != report.cells.len() * PPROF_TAX_COMPARISONS.len() {
        return Err("pprof-tax report cell comparisons do not match its cell matrix".into());
    }
    Ok(())
}

pub fn attach_pprof_tax_report(
    latest: &mut LatestReport,
    report: PprofTaxMetricReport,
) -> Result<(), String> {
    validate_pprof_tax_report(&report)?;
    let upstream_matches = latest.allocators.iter().any(|allocator| {
        allocator.allocator_id == "upstream-mimalloc"
            && allocator.source_sha == report.upstream_source_sha
    });
    if !upstream_matches {
        return Err("pprof-tax upstream provenance does not match the core latest report".into());
    }
    if report.configuration_manifest_sha256.is_empty() || report.raw_artifact_sha256.is_empty() {
        return Err("pprof-tax report is missing manifest or raw artifact provenance".into());
    }
    latest.pprof_tax = Some(report);
    latest
        .pending_metrics
        .retain(|value| value.metric_id != "pprof-tax");
    Ok(())
}

// ---------------------------------------------------------------------
// Synthetic fixture
// ---------------------------------------------------------------------

fn fold_seed(seed: u64, text: &str) -> u64 {
    let mut state = seed;
    for byte in text.bytes() {
        state = splitmix64(state ^ u64::from(byte));
    }
    state
}

fn base_throughput(configuration_id: &str) -> f64 {
    match configuration_id {
        "upstream-baseline" => 100.0,
        "fork-pprof-off" => 98.0,
        "fork-pprof-on-stopped" => 97.0,
        "fork-pprof-off-frame-pointers" => 97.5,
        "fork-pprof-sparse" => 96.0,
        "fork-pprof-aggressive" => 90.0,
        "fork-pprof-rate-1-stress" => 20.0,
        _ => 100.0,
    }
}

fn fixture_cache(
    mi_pprof: Option<&str>,
    mi_dhat: Option<&str>,
    c_flags_release: &str,
) -> BTreeMap<String, Option<String>> {
    let mut cache = BTreeMap::new();
    cache.insert("CMAKE_AR".into(), Some("/usr/bin/ar".into()));
    cache.insert("CMAKE_BUILD_TYPE".into(), Some("Release".into()));
    cache.insert("CMAKE_C_COMPILER".into(), Some("/usr/bin/clang".into()));
    cache.insert("CMAKE_C_FLAGS".into(), Some("-O3".into()));
    cache.insert("CMAKE_C_FLAGS_RELEASE".into(), Some(c_flags_release.into()));
    cache.insert("CMAKE_EXE_LINKER_FLAGS".into(), Some("-fuse-ld=lld".into()));
    cache.insert(
        "CMAKE_INTERPROCEDURAL_OPTIMIZATION".into(),
        Some("ON".into()),
    );
    cache.insert("CMAKE_STATIC_LINKER_FLAGS".into(), Some(String::new()));
    cache.insert("MI_BUILD_SHARED".into(), Some("OFF".into()));
    cache.insert("MI_BUILD_STATIC".into(), Some("ON".into()));
    cache.insert("MI_BUILD_TESTS".into(), Some("OFF".into()));
    cache.insert("MI_DEBUG_FULL".into(), Some("OFF".into()));
    cache.insert("MI_DHAT".into(), mi_dhat.map(String::from));
    cache.insert("MI_OPT_ARCH".into(), Some("OFF".into()));
    cache.insert("MI_OPT_SIMD".into(), Some("ON".into()));
    cache.insert("MI_OVERRIDE".into(), Some("ON".into()));
    cache.insert("MI_PPROF".into(), mi_pprof.map(String::from));
    cache.insert("MI_SECURE".into(), Some("OFF".into()));
    cache.insert("MI_TRACK_ASAN".into(), Some("OFF".into()));
    cache.insert("MI_TRACK_VALGRIND".into(), Some("OFF".into()));
    cache
}

/// Build a complete, internally consistent full-mode raw run without
/// spawning children or a real toolchain. Every value is fake but well-formed
/// so it can exercise the validator, report builder, and CLI end to end.
pub fn synthetic_pprof_tax_fixture(run_seed: u64) -> Result<PprofTaxRawRun, String> {
    let fork_source_sha = "b".repeat(40);
    let upstream_source_sha = PPROF_TAX_UPSTREAM_COMMIT.to_string();

    let off_cache = fixture_cache(Some("OFF"), None, "-O3 -DNDEBUG");
    let mut upstream_cache = off_cache.clone();
    upstream_cache.insert("MI_PPROF".into(), None);
    upstream_cache.insert("MI_DHAT".into(), None);
    let on_cache = {
        let mut cache = off_cache.clone();
        cache.insert("MI_PPROF".into(), Some("ON".into()));
        cache
    };
    let frame_pointer_cache = {
        let mut cache = off_cache.clone();
        cache.insert(
            "CMAKE_C_FLAGS_RELEASE".into(),
            Some("-O3 -DNDEBUG -fno-omit-frame-pointer".into()),
        );
        cache
    };

    let static_chars = ['1', '2', '3', '4'];
    let executable_chars = ['5', '6', '7', '8'];
    let mut compiled_configurations = Vec::with_capacity(4);
    for (index, id) in PPROF_TAX_COMPILED_CONFIGURATION_IDS.into_iter().enumerate() {
        let (allocator_id, source_sha, pprof_compiled, frame_pointer_policy, cache) = match id {
            "upstream-baseline" => (
                "upstream-mimalloc",
                upstream_source_sha.clone(),
                false,
                "omitted",
                upstream_cache.clone(),
            ),
            "fork-pprof-off" => (
                "mimalloc-pprof",
                fork_source_sha.clone(),
                false,
                "omitted",
                off_cache.clone(),
            ),
            "fork-pprof-on" => (
                "mimalloc-pprof",
                fork_source_sha.clone(),
                true,
                "cmake-mi-pprof-implicit",
                on_cache.clone(),
            ),
            "fork-pprof-off-frame-pointers" => (
                "mimalloc-pprof",
                fork_source_sha.clone(),
                false,
                "forced-flag",
                frame_pointer_cache.clone(),
            ),
            _ => unreachable!("PPROF_TAX_COMPILED_CONFIGURATION_IDS is fixed"),
        };
        let allocator_version = if allocator_id == "upstream-mimalloc" {
            "v3-fixture".to_string()
        } else {
            fork_source_sha.clone()
        };
        let static_library_sha256 = crate::validate::repeated_hex(static_chars[index], 64);
        let executable_sha256 = crate::validate::repeated_hex(executable_chars[index], 64);
        compiled_configurations.push(PprofTaxCompiledConfiguration {
            compiled_configuration_id: id.into(),
            allocator_id: allocator_id.into(),
            allocator_version: allocator_version.clone(),
            source_sha: source_sha.clone(),
            pprof_compiled,
            frame_pointer_policy: frame_pointer_policy.into(),
            cmake_arguments: vec![format!(
                "-DMI_PPROF={}",
                cache
                    .get("MI_PPROF")
                    .cloned()
                    .flatten()
                    .unwrap_or_else(|| "OFF".into())
            )],
            cmake_cache: cache,
            c_compiler_identity: "clang version 18.1.0".into(),
            linker_identity: "LLD 18.1.0".into(),
            static_library_sha256: static_library_sha256.clone(),
            executable_path: format!("/fixture/{id}/benchmark-child"),
            executable_sha256: executable_sha256.clone(),
            identity_probe: PprofTaxIdentityProbe {
                configuration_id: id.into(),
                allocator_id: allocator_id.into(),
                allocator_version,
                source_sha,
                library_sha256: static_library_sha256,
                executable_sha256,
                pprof_compiled,
                pprof_enabled: false,
            },
        });
    }

    let manifest = PprofTaxManifest {
        manifest_schema_version: PPROF_TAX_MANIFEST_SCHEMA_VERSION.into(),
        target: "x86_64-unknown-linux-gnu".into(),
        fork_source_sha: fork_source_sha.clone(),
        upstream_source_sha: upstream_source_sha.clone(),
        upstream_archive_sha256: crate::validate::repeated_hex('7', 64),
        toolchain: PprofTaxToolchain {
            c_compiler: "/usr/bin/clang".into(),
            c_compiler_identity: "clang version 18.1.0".into(),
            linker_identity: "LLD 18.1.0".into(),
            cmake: "cmake version 3.30.0".into(),
            ninja: "1.12.0".into(),
            rustc: "rustc fixture".into(),
            cargo: "cargo fixture".into(),
        },
        environment: BTreeMap::from([
            ("CC".to_string(), Some("/usr/bin/clang".to_string())),
            ("CXX".to_string(), Some("/usr/bin/clang++".to_string())),
            ("AR".to_string(), Some("/usr/bin/ar".to_string())),
            ("CFLAGS".to_string(), None),
            ("LDFLAGS".to_string(), None),
            ("RUSTFLAGS".to_string(), None),
            (
                "SOURCE_DATE_EPOCH".to_string(),
                Some("1700000000".to_string()),
            ),
        ]),
        compiled_configurations,
    };
    validate_manifest(&manifest)?;
    let configuration_manifest_sha256 =
        sha256_bytes(&serde_json::to_vec(&manifest).map_err(|error| error.to_string())?);

    let run = RunIdentity {
        source_repository: "https://github.com/zackees/mimalloc-pprof".into(),
        source_sha: fork_source_sha.clone(),
        source_ref: "refs/heads/main".into(),
        run_origin: "local".into(),
        run_id: "pprof-tax-fixture".into(),
        run_attempt: 1,
        generated_at_utc: "2026-09-18T00:00:00Z".into(),
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
        affinity: crate::model::AffinityMetadata {
            policy: "unrestricted".into(),
            logical_cpu_ids: Vec::new(),
        },
        power: crate::model::PowerMetadata {
            governor: "not-observable".into(),
            boost: "not-observable".into(),
            frequency_policy: "not-observable".into(),
        },
    };
    runner.fingerprint_sha256 =
        crate::validate::runner_fingerprint(&runner).map_err(|error| error.to_string())?;

    let topology = Topology {
        physical_cores: 2,
        logical_cores: 4,
    };
    let cell_specs = pprof_tax_cells(&topology)?;
    let cells: Vec<PprofTaxRawCell> = cell_specs
        .iter()
        .map(|cell| PprofTaxRawCell {
            scenario_id: cell.scenario_id.into(),
            thread_point: cell.thread_point.into(),
            thread_count: cell.thread_count,
            operations_per_worker: 4_000,
            warmup_operations_per_worker: 200,
            calibration_elapsed_ns: PPROF_TAX_TARGET_BLOCK_NS,
        })
        .collect();

    let blocks = PPROF_TAX_MIN_BLOCKS;
    let orders = block_orders(blocks, run_seed)?;
    let block_orders_owned: Vec<Vec<String>> = orders
        .iter()
        .map(|order| order.iter().map(|id| id.to_string()).collect())
        .collect();

    let mut samples =
        Vec::with_capacity(blocks as usize * cell_specs.len() * PPROF_TAX_CONFIGURATIONS.len());
    for block in 0..blocks {
        for cell in &cell_specs {
            for spec in &PPROF_TAX_CONFIGURATIONS {
                let compiled = manifest
                    .compiled_configurations
                    .iter()
                    .find(|entry| entry.compiled_configuration_id == spec.compiled_configuration_id)
                    .expect("compiled configuration exists for every spec");
                let seed_material = format!(
                    "{block}:{}:{}:{}",
                    cell.scenario_id, cell.thread_point, spec.configuration_id
                );
                let jitter_seed = fold_seed(run_seed, &seed_material);
                let jitter = 1.0 + ((jitter_seed % 1000) as f64 / 1000.0 - 0.5) * 0.01;
                let throughput = base_throughput(spec.configuration_id)
                    * f64::from(cell.thread_count)
                    * 1000.0
                    * jitter;
                let elapsed_ns = PPROF_TAX_TARGET_BLOCK_NS;
                let operation_count = ((throughput * elapsed_ns as f64 / 1_000_000_000.0)
                    .round()
                    .max(1.0)) as u64;
                let allocated_bytes_lower_bound =
                    operation_count * minimum_request_bytes(cell.scenario_id)?;

                let mut sample = PprofTaxRawSample {
                    block_id: block,
                    position: 0,
                    configuration_id: spec.configuration_id.into(),
                    compiled_configuration_id: spec.compiled_configuration_id.into(),
                    configuration_manifest_sha256: configuration_manifest_sha256.clone(),
                    executable_sha256: compiled.executable_sha256.clone(),
                    pprof_compiled: spec.pprof_compiled,
                    pprof_active: spec.pprof_active,
                    sampling_interval_bytes: spec.sampling_interval_bytes,
                    frame_pointer_policy: spec.frame_pointer_policy.into(),
                    scenario_id: cell.scenario_id.into(),
                    thread_point: cell.thread_point.into(),
                    thread_count: cell.thread_count,
                    operations_per_worker: 4_000,
                    workload_seed: jitter_seed,
                    throughput_operations_per_second: Some(throughput),
                    elapsed_ns: Some(elapsed_ns),
                    operation_count: Some(operation_count),
                    allocation_calls: Some(operation_count),
                    checksum: Some(jitter_seed | 1),
                    allocated_bytes_lower_bound: Some(allocated_bytes_lower_bound),
                    peak_rss_bytes: Some(32 * 1024 * 1024 + jitter_seed % (4 * 1024 * 1024)),
                    end_rss_delta_bytes: Some(1024 * 1024),
                    profile_path: None,
                    profile_sha256: None,
                    profile_size_bytes: None,
                    sample_count: None,
                    sampled_bytes: None,
                    dropped_records: None,
                    profiler_arena_bytes: None,
                    interval_confirmed: None,
                    timed_out: false,
                    exit_code: Some(0),
                    validity_status: "valid".into(),
                    invalid_reason: None,
                };
                if spec.pprof_active {
                    let interval = spec.sampling_interval_bytes.unwrap();
                    let approx_sample_count = (allocated_bytes_lower_bound / interval).max(1);
                    sample.profile_path = Some(format!(
                        "/fixture/profiles/{}/{}/{}/block-{}.pb.gz",
                        spec.configuration_id, cell.scenario_id, cell.thread_point, block
                    ));
                    sample.profile_sha256 = Some(crate::validate::repeated_hex('d', 64));
                    sample.profile_size_bytes = Some(4096 + approx_sample_count * 32);
                    sample.sample_count = Some(approx_sample_count);
                    sample.sampled_bytes = Some(approx_sample_count * interval);
                    sample.dropped_records = Some(0);
                    sample.profiler_arena_bytes = Some(65536);
                    sample.interval_confirmed = Some(interval);
                }
                samples.push(sample);
            }
        }
    }
    for sample in &mut samples {
        let order = &orders[sample.block_id as usize];
        sample.position = order
            .iter()
            .position(|id| *id == sample.configuration_id)
            .expect("every sample names one of the seven configurations")
            as u32;
    }

    Ok(PprofTaxRawRun {
        raw_schema_version: PPROF_TAX_RAW_SCHEMA_VERSION.into(),
        metric_schema_version: PPROF_TAX_SCHEMA_VERSION.into(),
        mode: "full".into(),
        run,
        runner,
        run_seed,
        blocks,
        configuration_manifest_sha256,
        manifest,
        topology: PprofTaxTopology {
            physical_cores: 2,
            logical_cores: 4,
        },
        cells,
        block_orders: block_orders_owned,
        stress_budget_seconds: 60,
        samples,
    })
}
