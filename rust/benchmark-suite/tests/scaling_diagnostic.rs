//! #528 (#422 step 0, P4 + P5): the scaling sweep's diagnostic mode.
//!
//! P4: a diagnostic dispatch may give the mimalloc-pprof child extra
//! environment; the other four allocators stay the unchanged same-run
//! references. A diagnostic raw run carries its metadata and can never be
//! validated for publication.
//!
//! P5: the live-telemetry replay marks the child's phases (setup, warmup,
//! measured block, drain, teardown), and the controller's RSS samples are
//! summarised per phase.
//!
//! The adapter and request builder are copied from `tests/thread_churn.rs`
//! (this crate's integration tests do not share a `tests/common.rs`).

use std::alloc::{alloc, dealloc, realloc, Layout};
use std::collections::{BTreeMap, HashMap};
use std::ffi::OsString;
use std::path::PathBuf;
use std::ptr::NonNull;
use std::sync::Mutex;
use std::time::Duration;

use benchmark_suite::execution::AllocatorAdapter;
use benchmark_suite::model::{AllocatorIdentity, RunnerMetadata, ToolchainMetadata};
use benchmark_suite::orchestration::{ChildProgram, ALLOCATOR_IDS};
use benchmark_suite::scaling::{
    build_scaling_report, execute_scaling_child_request, merge_scaling_runs,
    run_scaling_child_with_plan, scaling_thread_points_for_shard, simulate_cell, stream_seed,
    synthetic_scaling_fixture, validate_scaling_raw_run, ScalingChildRequest, ScalingPattern,
    ScalingRawRun, WorkerPlanner, DISTRIBUTION_PATTERNS, SCALING_CHILD_PROTOCOL_VERSION,
    SCALING_PATTERNS, SCALING_SCHEMA_VERSION, SCALING_THREAD_POINTS,
};
use benchmark_suite::scaling_diagnostic::{
    apply_diagnostic_environment, decode_live_telemetry, encode_live_telemetry,
    parse_diagnostic_cppdefs, parse_diagnostic_environment, parse_pattern_selection,
    parse_thread_point_selection, RssPhaseAccumulator, ScalingDiagnostic, ScalingPhase,
    DIAGNOSTIC_STATUS, DIAGNOSTIC_TARGET_ALLOCATOR, LIVE_TELEMETRY_BYTES,
};

const RUN_SEED: u64 = 0x6d69_6d61_6c6c_6f63;
const OPERATIONS: u64 = 400;

fn identity(allocator: &str) -> AllocatorIdentity {
    AllocatorIdentity {
        allocator_id: allocator.into(),
        allocator_version: "test".into(),
        source_sha: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".into(),
        library_sha256: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb".into(),
        child_binary_sha256: "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
            .into(),
    }
}

fn toolchain() -> ToolchainMetadata {
    ToolchainMetadata {
        rustc: "1.94.1".into(),
        target: "x86_64-unknown-linux-gnu".into(),
        compiler: "clang".into(),
        linker: "lld".into(),
    }
}

fn request_for(pattern: ScalingPattern, threads: u32, allocator: &str) -> ScalingChildRequest {
    ScalingChildRequest {
        protocol_version: SCALING_CHILD_PROTOCOL_VERSION.into(),
        metric_schema_version: SCALING_SCHEMA_VERSION.into(),
        run_seed: RUN_SEED,
        pattern: pattern.as_str().into(),
        thread_count: threads,
        block_id: 0,
        ordinal: 0,
        operations_per_worker: OPERATIONS,
        warmup_operations_per_worker: 16,
        allocator: identity(allocator),
        runner: RunnerMetadata {
            os: "linux".into(),
            architecture: "x86_64".into(),
            physical_cores: 2,
            logical_cores: 4,
        },
        toolchain: toolchain(),
        reproduction_command: "test".into(),
        live_telemetry_path: None,
    }
}

fn children(program: PathBuf) -> Vec<ChildProgram> {
    ALLOCATOR_IDS
        .into_iter()
        .map(|allocator| ChildProgram {
            allocator: identity(allocator),
            program: program.clone(),
            arguments: Vec::new(),
            toolchain: toolchain(),
            environment: Vec::new(),
        })
        .collect()
}

fn scratch(name: &str) -> PathBuf {
    let directory = std::env::temp_dir().join(format!(
        "benchmark-scaling-diagnostic-{}-{name}",
        std::process::id()
    ));
    let _ = std::fs::remove_dir_all(&directory);
    std::fs::create_dir_all(&directory).unwrap();
    directory
}

// ---------------------------------------------------------------- P4: environment

#[test]
fn diagnostic_environment_grammar_is_strict() {
    let parsed =
        parse_diagnostic_environment(" MIMALLOC_PURGE_DELAY=10  MIMALLOC_ARENA_PURGE_MULT=1 ")
            .unwrap();
    assert_eq!(
        parsed,
        BTreeMap::from([
            ("MIMALLOC_ARENA_PURGE_MULT".to_string(), "1".to_string()),
            ("MIMALLOC_PURGE_DELAY".to_string(), "10".to_string()),
        ])
    );
    assert!(parse_diagnostic_environment("").unwrap().is_empty());
    for bad in [
        "MIMALLOC_PURGE_DELAY",                          // no value
        "=10",                                           // no key
        "MIMALLOC_PURGE_DELAY=",                         // empty value
        "1ABC=2",                                        // key is not an identifier
        "MIMALLOC_PURGE_DELAY=$(id)",                    // shell metacharacters
        "MIMALLOC_PURGE_DELAY=1;2",                      // list separator
        "MIMALLOC_PURGE_DELAY=1 MIMALLOC_PURGE_DELAY=2", // duplicate key
        "MIMALLOC_PROF=1",                               // forced off by the spawner
        "MIMALLOC_MEMORY_EVENTS=1",                      // forced off by the spawner
    ] {
        assert!(
            parse_diagnostic_environment(bad).is_err(),
            "accepted diagnostic env {bad:?}"
        );
    }
}

#[test]
fn diagnostic_cppdefs_grammar_matches_perf_ab() {
    assert_eq!(
        parse_diagnostic_cppdefs("MI_ENABLE_LARGE_PAGES=0;MI_DEBUG_X  FOO=1.5").unwrap(),
        vec!["MI_ENABLE_LARGE_PAGES=0", "MI_DEBUG_X", "FOO=1.5"]
    );
    assert!(parse_diagnostic_cppdefs("").unwrap().is_empty());
    for bad in ["1X=0", "X=", "X=\"0\"", "X=$(id)", "X=a,b", "-DX=1"] {
        assert!(
            parse_diagnostic_cppdefs(bad).is_err(),
            "accepted cppdefs {bad:?}"
        );
    }
}

#[test]
fn workload_selection_accepts_only_declared_patterns_and_points() {
    assert_eq!(
        parse_pattern_selection("").unwrap(),
        SCALING_PATTERNS.to_vec()
    );
    assert_eq!(
        parse_pattern_selection("random-large, sparse-large-buffers").unwrap(),
        // sweep order, not argument order
        vec![ScalingPattern::LargeBuffers, ScalingPattern::RandomLarge]
    );
    assert!(parse_pattern_selection("thread-churn").is_err());
    assert!(parse_pattern_selection("sparse-large-buffer").is_err());
    assert_eq!(
        parse_thread_point_selection("").unwrap(),
        SCALING_THREAD_POINTS.to_vec()
    );
    assert_eq!(parse_thread_point_selection("4,1").unwrap(), vec![1, 4]);
    assert!(parse_thread_point_selection("5").is_err());
    assert!(parse_thread_point_selection("1,x").is_err());
}

#[test]
fn diagnostic_environment_is_applied_to_the_fork_child_only() {
    let mut programs = children(PathBuf::from("/bin/false"));
    let environment = parse_diagnostic_environment("MIMALLOC_PURGE_DELAY=10").unwrap();
    apply_diagnostic_environment(&mut programs, &environment).unwrap();
    for child in &programs {
        let expected: Vec<(OsString, OsString)> =
            if child.allocator.allocator_id == DIAGNOSTIC_TARGET_ALLOCATOR {
                vec![("MIMALLOC_PURGE_DELAY".into(), "10".into())]
            } else {
                Vec::new()
            };
        assert_eq!(
            child.environment, expected,
            "{}",
            child.allocator.allocator_id
        );
    }
    // No fork child: nothing to apply the diagnostic to is an error, not a no-op.
    let mut others = children(PathBuf::from("/bin/false"));
    others.retain(|child| child.allocator.allocator_id != DIAGNOSTIC_TARGET_ALLOCATOR);
    assert!(apply_diagnostic_environment(&mut others, &environment).is_err());
}

/// End to end through the real spawner: a stand-in child prints its whole
/// environment to stderr and fails, and the spawner's error carries that
/// stderr. Only the fork sees the diagnostic variable; every child still gets
/// the forced-off profiler switches and nothing inherited from this process.
#[cfg(unix)]
#[test]
fn spawned_children_see_the_diagnostic_environment_only_on_the_fork() {
    use std::os::unix::fs::PermissionsExt;

    let directory = scratch("spawn");
    let script = directory.join("print-environment");
    std::fs::write(&script, "#!/bin/sh\n/usr/bin/env 1>&2\nexit 3\n").unwrap();
    std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();
    let mut programs = children(script);
    let environment = parse_diagnostic_environment("MIMALLOC_PURGE_DELAY=10").unwrap();
    apply_diagnostic_environment(&mut programs, &environment).unwrap();

    let plan = simulate_cell(ScalingPattern::LargeBuffers, RUN_SEED, 1, 0, OPERATIONS);
    for child in &programs {
        let request = request_for(
            ScalingPattern::LargeBuffers,
            1,
            &child.allocator.allocator_id,
        );
        let error = run_scaling_child_with_plan(child, &request, Duration::from_secs(30), &plan)
            .expect_err("the stand-in child always fails");
        let is_fork = child.allocator.allocator_id == DIAGNOSTIC_TARGET_ALLOCATOR;
        assert_eq!(
            error.contains("MIMALLOC_PURGE_DELAY=10"),
            is_fork,
            "{}: {error}",
            child.allocator.allocator_id
        );
        assert!(error.contains("MIMALLOC_PROF=0"), "{error}");
        assert!(error.contains("MIMALLOC_MEMORY_EVENTS=0"), "{error}");
        assert!(
            !error.contains("CARGO_PKG_NAME="),
            "inherited env leaked: {error}"
        );
    }
    std::fs::remove_dir_all(directory).unwrap();
}

// ------------------------------------------------------ P4: never publishable

fn diagnostic_record() -> ScalingDiagnostic {
    ScalingDiagnostic::new(
        parse_diagnostic_environment("MIMALLOC_PURGE_DELAY=10").unwrap(),
        parse_diagnostic_cppdefs("MI_ENABLE_LARGE_PAGES=0").unwrap(),
        &[ScalingPattern::LargeBuffers],
        &[1, 4],
        1,
        false,
    )
}

#[test]
fn diagnostic_record_names_the_build_and_environment() {
    let record = diagnostic_record();
    record.validate().unwrap();
    assert!(!record.publishable);
    assert_eq!(record.applies_to, DIAGNOSTIC_TARGET_ALLOCATOR);
    assert!(
        record
            .label
            .contains("built with -DMI_EXTRA_CPPDEFS=MI_ENABLE_LARGE_PAGES=0"),
        "{}",
        record.label
    );
    assert!(
        record.label.contains("MIMALLOC_PURGE_DELAY=10"),
        "{}",
        record.label
    );
    let mut forged = record.clone();
    forged.publishable = true;
    assert!(forged.validate().is_err());
    let mut relabelled = record.clone();
    relabelled.label = "default build".into();
    assert!(relabelled.validate().is_err());
    let mut retargeted = record;
    retargeted.applies_to = "upstream-mimalloc".into();
    assert!(retargeted.validate().is_err());
}

#[test]
fn a_diagnostic_raw_run_can_never_be_validated_for_publication() {
    let mut raw: ScalingRawRun = synthetic_scaling_fixture(RUN_SEED).unwrap();
    validate_scaling_raw_run(&raw).unwrap();
    raw.diagnostic = Some(diagnostic_record());
    let error = validate_scaling_raw_run(&raw).unwrap_err();
    assert!(error.contains("diagnostic"), "{error}");
    assert!(build_scaling_report(&raw).is_err());
    // Nor with the metadata stripped but the status left behind.
    raw.diagnostic = None;
    raw.status = DIAGNOSTIC_STATUS.into();
    assert!(validate_scaling_raw_run(&raw).is_err());
}

#[test]
fn diagnostic_shards_merge_to_a_diagnostic_run_and_must_agree() {
    let full: ScalingRawRun = synthetic_scaling_fixture(RUN_SEED).unwrap();
    let record = diagnostic_record();
    let shard = |index: usize| {
        let points = scaling_thread_points_for_shard(index, SCALING_THREAD_POINTS.len()).unwrap();
        let mut shard = full.clone();
        shard.status = DIAGNOSTIC_STATUS.into();
        shard.diagnostic = Some(record.clone());
        // A diagnostic run measures only the selected cells: shards whose
        // points were filtered out entirely contribute nothing.
        let keep = |pattern: &str, threads: u32| {
            pattern == ScalingPattern::LargeBuffers.as_str()
                && points.contains(&threads)
                && record.thread_points.contains(&threads)
        };
        shard
            .calibrations
            .retain(|value| keep(&value.pattern, value.thread_count));
        shard
            .samples
            .retain(|value| keep(&value.pattern, value.thread_count));
        shard.thread_churn_samples.clear();
        shard
    };
    let shards = (0..SCALING_THREAD_POINTS.len())
        .map(shard)
        .collect::<Vec<_>>();
    let merged = merge_scaling_runs(shards.clone()).unwrap();
    assert_eq!(merged.status, DIAGNOSTIC_STATUS);
    assert_eq!(merged.diagnostic.as_ref(), Some(&record));
    assert_eq!(merged.calibrations.len(), 2);
    assert!(validate_scaling_raw_run(&merged).is_err());

    let mut mismatched = shards.clone();
    mismatched[1].diagnostic.as_mut().unwrap().environment =
        parse_diagnostic_environment("MIMALLOC_PURGE_DELAY=1000").unwrap();
    assert!(merge_scaling_runs(mismatched)
        .unwrap_err()
        .contains("diagnostic"));

    let mut mixed = shards;
    mixed[2].diagnostic = None;
    mixed[2].status = "incomplete".into();
    assert!(merge_scaling_runs(mixed)
        .unwrap_err()
        .contains("diagnostic"));
}

// ------------------------------------------------------------ P5: replay + phases

#[test]
fn sparse_large_buffers_gets_the_live_replay_only_in_diagnostic_mode() {
    assert!(!ScalingPattern::LargeBuffers.replays_live_telemetry(false));
    assert!(ScalingPattern::LargeBuffers.replays_live_telemetry(true));
    for pattern in DISTRIBUTION_PATTERNS {
        assert!(
            pattern.replays_live_telemetry(false),
            "{}",
            pattern.as_str()
        );
        assert!(pattern.replays_live_telemetry(true), "{}", pattern.as_str());
    }
    for pattern in SCALING_PATTERNS {
        if pattern != ScalingPattern::LargeBuffers && !pattern.is_distribution() {
            assert!(
                !pattern.replays_live_telemetry(true),
                "{}",
                pattern.as_str()
            );
        }
    }
}

#[test]
fn live_telemetry_carries_live_bytes_and_phase() {
    assert_eq!(LIVE_TELEMETRY_BYTES, 16);
    let zero = [0u8; LIVE_TELEMETRY_BYTES];
    assert_eq!(decode_live_telemetry(&zero), Some((0, ScalingPhase::Setup)));
    for phase in ScalingPhase::ALL {
        let bytes = encode_live_telemetry(123_456, phase);
        assert_eq!(decode_live_telemetry(&bytes), Some((123_456, phase)));
    }
    assert_eq!(decode_live_telemetry(&zero[..8]), None);
    let mut unknown = encode_live_telemetry(1, ScalingPhase::Teardown);
    unknown[8] = 99;
    assert_eq!(decode_live_telemetry(&unknown), None);
    assert_eq!(
        ScalingPhase::ALL.map(ScalingPhase::as_str),
        ["setup", "warmup", "measured", "drain", "teardown"]
    );
}

#[test]
fn rss_samples_are_summarised_per_phase() {
    let mut phases = RssPhaseAccumulator::default();
    phases.observe(1_000, 10, 0, ScalingPhase::Setup);
    phases.observe(2_000, 12, 0, ScalingPhase::Setup);
    phases.observe(3_000, 40, 30, ScalingPhase::Measured);
    phases.observe(4_000, 55, 20, ScalingPhase::Measured);
    phases.observe(5_000, 50, 25, ScalingPhase::Measured);
    phases.observe(6_000, 60, 0, ScalingPhase::Teardown);
    phases.observe(7_000, 58, 0, ScalingPhase::Teardown);
    let summary = phases.finish();
    // Only observed phases, in phase order.
    assert_eq!(
        summary
            .iter()
            .map(|value| value.phase.as_str())
            .collect::<Vec<_>>(),
        ["setup", "measured", "teardown"]
    );
    let measured = &summary[1];
    assert_eq!(measured.samples, 3);
    assert_eq!(measured.first_offset_ns, 3_000);
    assert_eq!(measured.last_offset_ns, 5_000);
    assert_eq!(measured.peak_rss_bytes, 55);
    assert_eq!(measured.live_requested_bytes_at_peak_rss, 20);
    assert_eq!(measured.last_rss_bytes, 50);
    assert_eq!(summary[2].peak_rss_bytes, 60);
    assert_eq!(summary[2].last_rss_bytes, 58);
}

/// Adapter that reads the live-telemetry file on every call, recording the
/// phase the child had marked at that moment.
struct PhaseProbe {
    telemetry: PathBuf,
    layouts: Mutex<HashMap<usize, Layout>>,
    seen: Mutex<Vec<PhaseNote>>,
}

/// (calling thread, phase marked at the call, which call).
type PhaseNote = (std::thread::ThreadId, ScalingPhase, &'static str);

impl PhaseProbe {
    fn note(&self, what: &'static str) {
        let bytes = std::fs::read(&self.telemetry).unwrap();
        let (_, phase) = decode_live_telemetry(&bytes).expect("telemetry decodes");
        self.seen
            .lock()
            .unwrap()
            .push((std::thread::current().id(), phase, what));
    }
}

impl AllocatorAdapter for PhaseProbe {
    fn allocator_id(&self) -> &str {
        "mimalloc-pprof"
    }
    fn allocator_version(&self) -> &str {
        "test"
    }
    fn source_sha(&self) -> &str {
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    }
    fn library_sha256(&self) -> &str {
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    }
    fn alloc(&self, size: usize) -> Result<NonNull<u8>, String> {
        self.note("alloc");
        let layout = Layout::from_size_align(size.max(1), 16).unwrap();
        let pointer = NonNull::new(unsafe { alloc(layout) }).ok_or("mock allocation failed")?;
        self.layouts
            .lock()
            .unwrap()
            .insert(pointer.as_ptr() as usize, layout);
        Ok(pointer)
    }
    fn calloc(&self, count: usize, size: usize) -> Result<NonNull<u8>, String> {
        self.alloc(count * size)
    }
    unsafe fn realloc(&self, pointer: NonNull<u8>, size: usize) -> Result<NonNull<u8>, String> {
        self.note("realloc");
        let old = self
            .layouts
            .lock()
            .unwrap()
            .remove(&(pointer.as_ptr() as usize))
            .ok_or("unknown realloc pointer")?;
        let updated = NonNull::new(unsafe { realloc(pointer.as_ptr(), old, size.max(1)) })
            .ok_or("mock realloc failed")?;
        self.layouts.lock().unwrap().insert(
            updated.as_ptr() as usize,
            Layout::from_size_align(size.max(1), old.align()).unwrap(),
        );
        Ok(updated)
    }
    fn aligned_alloc(&self, alignment: usize, size: usize) -> Result<NonNull<u8>, String> {
        let layout = Layout::from_size_align(size.max(1), alignment).unwrap();
        let pointer = NonNull::new(unsafe { alloc(layout) }).ok_or("mock aligned alloc failed")?;
        self.layouts
            .lock()
            .unwrap()
            .insert(pointer.as_ptr() as usize, layout);
        Ok(pointer)
    }
    unsafe fn free(&self, pointer: NonNull<u8>) {
        self.note("free");
        let layout = self
            .layouts
            .lock()
            .unwrap()
            .remove(&(pointer.as_ptr() as usize))
            .expect("mock free of an unknown pointer");
        unsafe { dealloc(pointer.as_ptr(), layout) };
    }
}

/// The first operation count from `OPERATIONS` whose stream leaves a live
/// slot on every worker, so the end-of-stream drain has frees to observe.
fn operations_leaving_live_slots(threads: u32) -> u64 {
    (OPERATIONS..OPERATIONS + 1000)
        .find(|&operations| {
            (0..threads).all(|worker| {
                let seed = stream_seed(RUN_SEED, ScalingPattern::LargeBuffers, threads, 0, worker);
                let mut planner = WorkerPlanner::new(
                    ScalingPattern::LargeBuffers,
                    seed,
                    operations,
                    worker,
                    threads,
                );
                while planner.next_action().is_some() {}
                !planner.drain_actions().is_empty()
            })
        })
        .expect("some operation count leaves live slots")
}

fn run_with_phase_probe(threads: u32, name: &str) -> (Vec<PhaseNote>, (u64, ScalingPhase)) {
    let directory = scratch(name);
    let telemetry = directory.join("live");
    std::fs::write(&telemetry, [0u8; LIVE_TELEMETRY_BYTES]).unwrap();
    let probe = PhaseProbe {
        telemetry: telemetry.clone(),
        layouts: Mutex::new(HashMap::new()),
        seen: Mutex::new(Vec::new()),
    };
    let mut request = request_for(ScalingPattern::LargeBuffers, threads, "mimalloc-pprof");
    request.operations_per_worker = operations_leaving_live_slots(threads);
    request.live_telemetry_path = Some(telemetry.to_str().unwrap().into());
    let response = execute_scaling_child_request(&probe, request.clone()).unwrap();
    response.validate_against(&request).unwrap();
    let last = decode_live_telemetry(&std::fs::read(&telemetry).unwrap()).unwrap();
    assert!(
        probe.layouts.lock().unwrap().is_empty(),
        "the replay leaked"
    );
    std::fs::remove_dir_all(directory).unwrap();
    (probe.seen.into_inner().unwrap(), last)
}

#[test]
fn one_worker_replay_walks_the_phases_in_order() {
    let (seen, last) = run_with_phase_probe(1, "one-worker");
    // Everything is freed and the child ends in teardown.
    assert_eq!(last, (0, ScalingPhase::Teardown));
    let phases = seen.iter().map(|(_, phase, _)| *phase).collect::<Vec<_>>();
    assert!(
        phases.windows(2).all(|pair| pair[0] <= pair[1]),
        "{phases:?}"
    );
    // Warmup allocations happen in warmup, the stream in the measured block,
    // and the end-of-stream frees in the drain.
    assert_eq!(phases.first(), Some(&ScalingPhase::Warmup));
    assert!(phases.contains(&ScalingPhase::Measured));
    assert_eq!(phases.last(), Some(&ScalingPhase::Drain));
    assert!(seen
        .iter()
        .filter(|(_, phase, _)| *phase == ScalingPhase::Drain)
        .all(|(_, _, what)| *what == "free"));
}

#[test]
fn multi_worker_replay_phases_never_go_backwards_on_a_thread() {
    let threads = 4;
    let (seen, last) = run_with_phase_probe(threads, "four-workers");
    assert_eq!(last, (0, ScalingPhase::Teardown));
    let mut per_thread: HashMap<std::thread::ThreadId, Vec<ScalingPhase>> = HashMap::new();
    for (thread, phase, _) in &seen {
        per_thread.entry(*thread).or_default().push(*phase);
    }
    assert_eq!(per_thread.len(), threads as usize);
    for phases in per_thread.values() {
        assert!(
            phases.windows(2).all(|pair| pair[0] <= pair[1]),
            "{phases:?}"
        );
        assert!(phases.contains(&ScalingPhase::Measured) || phases.contains(&ScalingPhase::Drain));
    }
    assert!(seen
        .iter()
        .all(|(_, phase, _)| *phase != ScalingPhase::Setup && *phase != ScalingPhase::Teardown));
}
