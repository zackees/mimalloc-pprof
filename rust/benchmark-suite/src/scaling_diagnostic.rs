//! #528 (#422 step 0, P4 + P5): the thread-scaling sweep's diagnostic mode.
//!
//! A diagnostic run measures the same five allocators as the published sweep,
//! but the mimalloc-pprof child alone may run with extra environment
//! (`benchmark-scaling-run --diagnostic-env`) and from a library built with
//! extra defines (`MI_EXTRA_CPPDEFS`, applied by
//! `ci/build_benchmark_allocators.py --fork-cppdefs` and recorded in the
//! allocator provenance). The other four allocators are the unchanged same-run
//! references. The raw run carries a [`ScalingDiagnostic`] record and the
//! status [`DIAGNOSTIC_STATUS`]; `validate_scaling_raw_run` refuses both, so a
//! diagnostic run cannot be built into a report, let alone published.
//!
//! P5 lives here too: the live-telemetry file the child writes during a
//! diagnostic replay carries its current [`ScalingPhase`] next to its live
//! requested bytes, and the controller summarises its external RSS samples
//! per phase ([`RssPhaseAccumulator`]).

use std::collections::BTreeMap;
use std::ffi::OsString;

use serde::{Deserialize, Serialize};

use crate::orchestration::ChildProgram;
use crate::scaling::{
    ScalingPattern, DISTRIBUTION_BLOCKS, SCALING_PATTERNS, SCALING_THREAD_POINTS,
};

/// The one allocator a diagnostic may change.
pub const DIAGNOSTIC_TARGET_ALLOCATOR: &str = "mimalloc-pprof";
/// `ScalingRawRun::status` of a diagnostic run. Never `complete`.
pub const DIAGNOSTIC_STATUS: &str = "diagnostic";
/// Set on every scaling child by the spawner, after the child's own
/// environment. A diagnostic may not name them: the spawner would silently
/// override the value, so the record would lie.
pub const FORCED_CHILD_ENVIRONMENT: [(&str, &str); 2] =
    [("MIMALLOC_PROF", "0"), ("MIMALLOC_MEMORY_EVENTS", "0")];
/// Upper bound on a diagnostic's paired blocks per cell: the published
/// distribution workloads' own count.
pub const MAX_DIAGNOSTIC_BLOCKS: u32 = DISTRIBUTION_BLOCKS;

fn is_identifier(text: &str) -> bool {
    let mut characters = text.chars();
    characters
        .next()
        .is_some_and(|first| first.is_ascii_alphabetic() || first == '_')
        && characters.all(|value| value.is_ascii_alphanumeric() || value == '_')
}

/// A value means the same to the shell, CMake's list splitting and the
/// compiler command line: `ci/perf_ab.py`'s `CPPDEF` value class (#527).
fn is_plain_value(text: &str) -> bool {
    !text.is_empty()
        && text
            .chars()
            .all(|value| value.is_ascii_alphanumeric() || matches!(value, '_' | '.' | '+' | '-'))
}

/// `--diagnostic-env`: space-separated `KEY=VALUE` pairs (perf-ab's `head_env`
/// form), with an identifier key, a plain non-empty value, no duplicate key,
/// and none of [`FORCED_CHILD_ENVIRONMENT`].
pub fn parse_diagnostic_environment(text: &str) -> Result<BTreeMap<String, String>, String> {
    let mut environment = BTreeMap::new();
    for pair in text.split_whitespace() {
        let (key, value) = pair
            .split_once('=')
            .ok_or_else(|| format!("diagnostic env: expected KEY=VALUE, got {pair:?}"))?;
        if !is_identifier(key) || !is_plain_value(value) {
            return Err(format!(
                "diagnostic env: expected KEY=VALUE with KEY an identifier and VALUE of \
                 [A-Za-z0-9_.+-], got {pair:?}"
            ));
        }
        if FORCED_CHILD_ENVIRONMENT
            .iter()
            .any(|(forced, _)| *forced == key)
        {
            return Err(format!(
                "diagnostic env: {key} is forced to 0 on every scaling child and cannot be set"
            ));
        }
        if environment
            .insert(key.to_string(), value.to_string())
            .is_some()
        {
            return Err(format!("diagnostic env: {key} is given twice"));
        }
    }
    Ok(environment)
}

/// One define: `NAME` or `NAME=VALUE` (`ci/perf_ab.py`'s `CPPDEF`, #527).
pub fn validate_cppdef(item: &str) -> Result<(), String> {
    let valid = match item.split_once('=') {
        Some((name, value)) => is_identifier(name) && is_plain_value(value),
        None => is_identifier(item),
    };
    if valid {
        Ok(())
    } else {
        Err(format!(
            "diagnostic cppdefs: expected NAME or NAME=VALUE, got {item:?}"
        ))
    }
}

/// `--fork-cppdefs` / `diagnostic_cppdefs`: defines separated by `;` (CMake's
/// list form) or whitespace.
pub fn parse_diagnostic_cppdefs(text: &str) -> Result<Vec<String>, String> {
    let defines = text
        .split(|value: char| value == ';' || value.is_whitespace())
        .filter(|value| !value.is_empty())
        .map(str::to_string)
        .collect::<Vec<_>>();
    defines.iter().try_for_each(|item| validate_cppdef(item))?;
    Ok(defines)
}

fn selection_items(text: &str) -> impl Iterator<Item = &str> {
    text.split(|value: char| value == ',' || value.is_whitespace())
        .filter(|value| !value.is_empty())
}

/// `--patterns`: sweep pattern ids separated by commas or whitespace; empty
/// selects every sweep pattern. Returned in sweep order.
pub fn parse_pattern_selection(text: &str) -> Result<Vec<ScalingPattern>, String> {
    let mut selected = Vec::new();
    for item in selection_items(text) {
        let pattern = ScalingPattern::parse(item)
            .filter(|pattern| SCALING_PATTERNS.contains(pattern))
            .ok_or_else(|| {
                format!(
                    "diagnostic patterns: {item:?} is not a sweep pattern (expected one of {})",
                    SCALING_PATTERNS.map(ScalingPattern::as_str).join(", ")
                )
            })?;
        selected.push(pattern);
    }
    if selected.is_empty() {
        return Ok(SCALING_PATTERNS.to_vec());
    }
    Ok(SCALING_PATTERNS
        .into_iter()
        .filter(|pattern| selected.contains(pattern))
        .collect())
}

/// `--thread-points`: declared worker counts separated by commas or
/// whitespace; empty selects every point. Returned in sweep order.
pub fn parse_thread_point_selection(text: &str) -> Result<Vec<u32>, String> {
    let mut selected = Vec::new();
    for item in selection_items(text) {
        let point = item
            .parse::<u32>()
            .ok()
            .filter(|point| SCALING_THREAD_POINTS.contains(point))
            .ok_or_else(|| {
                format!(
                    "diagnostic thread points: {item:?} is not a declared point {SCALING_THREAD_POINTS:?}"
                )
            })?;
        selected.push(point);
    }
    if selected.is_empty() {
        return Ok(SCALING_THREAD_POINTS.to_vec());
    }
    Ok(SCALING_THREAD_POINTS
        .into_iter()
        .filter(|point| selected.contains(point))
        .collect())
}

/// Give the diagnostic environment to the mimalloc-pprof child and to no other.
pub fn apply_diagnostic_environment(
    children: &mut [ChildProgram],
    environment: &BTreeMap<String, String>,
) -> Result<(), String> {
    let fork = children
        .iter_mut()
        .find(|child| child.allocator.allocator_id == DIAGNOSTIC_TARGET_ALLOCATOR)
        .ok_or("diagnostic env: there is no mimalloc-pprof child to apply it to")?;
    fork.environment.extend(
        environment
            .iter()
            .map(|(key, value)| (OsString::from(key), OsString::from(value))),
    );
    Ok(())
}

/// The environment a child is spawned with, as a shell prefix for its
/// reproduction command: the forced switches, then the child's own.
pub fn reproduction_environment(child: &ChildProgram) -> String {
    FORCED_CHILD_ENVIRONMENT
        .iter()
        .map(|(key, value)| format!("{key}={value}"))
        .chain(
            child.environment.iter().map(|(key, value)| {
                format!("{}={}", key.to_string_lossy(), value.to_string_lossy())
            }),
        )
        .collect::<Vec<_>>()
        .join(" ")
}

/// What a diagnostic run changed, carried by its raw run so it can never be
/// read as a default run.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ScalingDiagnostic {
    /// One line naming the build and environment, like perf-ab's "Head arm
    /// built with ..." header (#527).
    pub label: String,
    /// Always false.
    pub publishable: bool,
    /// Always [`DIAGNOSTIC_TARGET_ALLOCATOR`].
    pub applies_to: String,
    pub environment: BTreeMap<String, String>,
    pub cppdefs: Vec<String>,
    pub patterns: Vec<String>,
    pub thread_points: Vec<u32>,
    /// Paired blocks per selected cell.
    pub blocks: u32,
    /// Whether the thread-churn workload ran (only with no pattern filter).
    pub thread_churn: bool,
}

pub fn diagnostic_label(environment: &BTreeMap<String, String>, cppdefs: &[String]) -> String {
    let build = if cppdefs.is_empty() {
        "built with the default recipe".to_string()
    } else {
        format!("built with -DMI_EXTRA_CPPDEFS={}", cppdefs.join(";"))
    };
    let run = if environment.is_empty() {
        "no extra environment".to_string()
    } else {
        environment
            .iter()
            .map(|(key, value)| format!("{key}={value}"))
            .collect::<Vec<_>>()
            .join(" ")
    };
    format!(
        "DIAGNOSTIC, not publishable: {DIAGNOSTIC_TARGET_ALLOCATOR} {build}, run with {run}; \
         the other four allocators are unchanged same-run references"
    )
}

impl ScalingDiagnostic {
    pub fn new(
        environment: BTreeMap<String, String>,
        cppdefs: Vec<String>,
        patterns: &[ScalingPattern],
        thread_points: &[u32],
        blocks: u32,
        thread_churn: bool,
    ) -> Self {
        Self {
            label: diagnostic_label(&environment, &cppdefs),
            publishable: false,
            applies_to: DIAGNOSTIC_TARGET_ALLOCATOR.into(),
            environment,
            cppdefs,
            patterns: patterns
                .iter()
                .map(|pattern| pattern.as_str().to_string())
                .collect(),
            thread_points: thread_points.to_vec(),
            blocks,
            thread_churn,
        }
    }

    /// Re-check a record read back from JSON.
    pub fn validate(&self) -> Result<(), String> {
        if self.publishable || self.applies_to != DIAGNOSTIC_TARGET_ALLOCATOR {
            return Err(
                "a scaling diagnostic is never publishable and applies to mimalloc-pprof only"
                    .into(),
            );
        }
        let text = self
            .environment
            .iter()
            .map(|(key, value)| format!("{key}={value}"))
            .collect::<Vec<_>>()
            .join(" ");
        if parse_diagnostic_environment(&text)? != self.environment {
            return Err("scaling diagnostic environment does not round-trip".into());
        }
        self.cppdefs
            .iter()
            .try_for_each(|item| validate_cppdef(item))?;
        let patterns = parse_pattern_selection(&self.patterns.join(","))?;
        let points = parse_thread_point_selection(
            &self
                .thread_points
                .iter()
                .map(u32::to_string)
                .collect::<Vec<_>>()
                .join(","),
        )?;
        if self.patterns.is_empty()
            || patterns
                .iter()
                .map(|value| value.as_str())
                .ne(self.patterns.iter().map(String::as_str))
            || self.thread_points.is_empty()
            || points != self.thread_points
        {
            return Err("scaling diagnostic selection is not a sweep-ordered subset".into());
        }
        if !(1..=MAX_DIAGNOSTIC_BLOCKS).contains(&self.blocks) {
            return Err(format!(
                "scaling diagnostic blocks must be 1..={MAX_DIAGNOSTIC_BLOCKS}"
            ));
        }
        if self.label != diagnostic_label(&self.environment, &self.cppdefs) {
            return Err("scaling diagnostic label does not name its build and environment".into());
        }
        Ok(())
    }
}

// ------------------------------------------------------------------ P5: phases

/// Where a scaling child is in its run, as marked in the live-telemetry file.
/// Monotonic: the child only ever raises it (`fetch_max`), so with several
/// workers a phase starts when the first worker reaches it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum ScalingPhase {
    /// Process start, request decoding, thread and barrier creation.
    Setup,
    /// The untimed warmup stream (from the first worker to start it).
    Warmup,
    /// The measured block (every worker's warmup done).
    Measured,
    /// The end-of-stream frees of every still-live slot (from the first worker
    /// to reach them; the measured block may still be running elsewhere).
    Drain,
    /// Every worker finished; joining, reporting and process exit.
    Teardown,
}

impl ScalingPhase {
    pub const ALL: [Self; 5] = [
        Self::Setup,
        Self::Warmup,
        Self::Measured,
        Self::Drain,
        Self::Teardown,
    ];

    pub const fn code(self) -> u64 {
        match self {
            Self::Setup => 0,
            Self::Warmup => 1,
            Self::Measured => 2,
            Self::Drain => 3,
            Self::Teardown => 4,
        }
    }

    pub fn from_code(code: u64) -> Option<Self> {
        Self::ALL.into_iter().find(|phase| phase.code() == code)
    }

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Setup => "setup",
            Self::Warmup => "warmup",
            Self::Measured => "measured",
            Self::Drain => "drain",
            Self::Teardown => "teardown",
        }
    }
}

/// The live-telemetry file: live requested bytes, then the phase code, both
/// little-endian u64. The controller creates it zeroed (setup, nothing live).
pub const LIVE_TELEMETRY_BYTES: usize = 16;

pub fn encode_live_telemetry(live_requested_bytes: u64, phase: ScalingPhase) -> [u8; 16] {
    let mut bytes = [0u8; LIVE_TELEMETRY_BYTES];
    bytes[..8].copy_from_slice(&live_requested_bytes.to_le_bytes());
    bytes[8..].copy_from_slice(&phase.code().to_le_bytes());
    bytes
}

pub fn decode_live_telemetry(bytes: &[u8]) -> Option<(u64, ScalingPhase)> {
    let live = u64::from_le_bytes(bytes.get(..8)?.try_into().ok()?);
    let code = u64::from_le_bytes(bytes.get(8..LIVE_TELEMETRY_BYTES)?.try_into().ok()?);
    Some((live, ScalingPhase::from_code(code)?))
}

/// The external RSS samples that fell in one phase of a diagnostic replay.
/// Offsets are from the child's spawn.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ScalingRssPhase {
    pub phase: String,
    pub samples: u64,
    pub first_offset_ns: u64,
    pub last_offset_ns: u64,
    pub peak_rss_bytes: u64,
    pub live_requested_bytes_at_peak_rss: u64,
    /// The phase's last sample: what the phase left behind.
    pub last_rss_bytes: u64,
}

/// Folds RSS samples into per-phase summaries without storing the series.
#[derive(Debug, Default)]
pub struct RssPhaseAccumulator {
    phases: [Option<ScalingRssPhase>; 5],
}

impl RssPhaseAccumulator {
    pub fn observe(
        &mut self,
        offset_ns: u64,
        rss_bytes: u64,
        live_requested_bytes: u64,
        phase: ScalingPhase,
    ) {
        let entry = self.phases[phase.code() as usize].get_or_insert_with(|| ScalingRssPhase {
            phase: phase.as_str().into(),
            samples: 0,
            first_offset_ns: offset_ns,
            last_offset_ns: offset_ns,
            peak_rss_bytes: 0,
            live_requested_bytes_at_peak_rss: 0,
            last_rss_bytes: 0,
        });
        entry.samples += 1;
        entry.last_offset_ns = offset_ns;
        entry.last_rss_bytes = rss_bytes;
        if rss_bytes > entry.peak_rss_bytes {
            entry.peak_rss_bytes = rss_bytes;
            entry.live_requested_bytes_at_peak_rss = live_requested_bytes;
        }
    }

    /// The observed phases, in phase order. A phase shorter than the poll
    /// interval may have no sample and is then absent.
    pub fn finish(self) -> Vec<ScalingRssPhase> {
        self.phases.into_iter().flatten().collect()
    }
}
