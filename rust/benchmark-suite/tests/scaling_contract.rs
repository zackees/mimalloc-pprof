use std::alloc::{alloc, dealloc, realloc, Layout};
use std::collections::HashMap;
use std::ptr::NonNull;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

use benchmark_suite::execution::AllocatorAdapter;
use benchmark_suite::model::{AllocatorIdentity, RunnerMetadata, ToolchainMetadata};
use benchmark_suite::scaling::{
    build_scaling_report, execute_scaling_child_request, simulate_cell, simulate_worker,
    stream_seed, validate_scaling_raw_run, validate_scaling_report, PlannedAction, ScalingCounts,
    ScalingPattern, ScalingRawRun, WorkerPlanner, SCALING_BLOCKS, SCALING_PATTERNS,
    SCALING_RIGOR_LABEL, SCALING_THREAD_POINTS,
};

/// Leak-detecting mock allocator. `Drop` asserts every block was released, so
/// any oracle/executor drift shows up as a failure rather than a leak.
struct MockAdapter {
    id: &'static str,
    layouts: Mutex<HashMap<usize, Layout>>,
    frees: AtomicU64,
}

impl MockAdapter {
    fn new(id: &'static str) -> Self {
        Self {
            id,
            layouts: Mutex::new(HashMap::new()),
            frees: AtomicU64::new(0),
        }
    }
}

impl Drop for MockAdapter {
    fn drop(&mut self) {
        assert!(
            self.layouts.get_mut().unwrap().is_empty(),
            "scaling workload leaked blocks"
        );
    }
}

impl AllocatorAdapter for MockAdapter {
    fn allocator_id(&self) -> &str {
        self.id
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
        let address = pointer.as_ptr() as usize;
        let old = self
            .layouts
            .lock()
            .unwrap()
            .remove(&address)
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
        let layout = self
            .layouts
            .lock()
            .unwrap()
            .remove(&(pointer.as_ptr() as usize))
            .expect("mock free of an unknown pointer");
        self.frees.fetch_add(1, Ordering::Relaxed);
        unsafe { dealloc(pointer.as_ptr(), layout) };
    }
}

fn actions(pattern: ScalingPattern, seed: u64, operations: u64, threads: u32) -> Vec<PlannedAction> {
    let mut planner = WorkerPlanner::new(pattern, seed, operations, 0, threads);
    let mut collected = Vec::new();
    while let Some(action) = planner.next_action() {
        collected.push(action);
    }
    collected.extend(planner.drain_actions());
    collected
}

#[test]
fn stream_is_reproducible_for_identical_inputs() {
    for pattern in SCALING_PATTERNS {
        let seed = stream_seed(0x1234_5678_9abc_def0, pattern, 4, 2, 1);
        let first = actions(pattern, seed, 500, 4);
        let second = actions(pattern, seed, 500, 4);
        assert_eq!(first, second, "{} stream is not reproducible", pattern.as_str());
        assert!(!first.is_empty());
    }
}

#[test]
fn differing_stream_inputs_produce_differing_streams() {
    let run_seed = 0x1234_5678_9abc_def0;
    let base = stream_seed(run_seed, ScalingPattern::MixedGeneral, 4, 2, 1);
    let variants = [
        stream_seed(run_seed, ScalingPattern::MixedGeneral, 4, 2, 0),
        stream_seed(run_seed, ScalingPattern::MixedGeneral, 4, 3, 1),
        stream_seed(run_seed, ScalingPattern::MixedGeneral, 16, 2, 1),
        stream_seed(run_seed, ScalingPattern::TinyHot, 4, 2, 1),
        stream_seed(run_seed + 1, ScalingPattern::MixedGeneral, 4, 2, 1),
    ];
    for variant in variants {
        assert_ne!(base, variant, "seed chain collapsed two distinct inputs");
    }
    let baseline = actions(ScalingPattern::MixedGeneral, base, 400, 4);
    for variant in variants {
        assert_ne!(
            baseline,
            actions(ScalingPattern::MixedGeneral, variant, 400, 4),
            "distinct seed inputs replayed the same stream"
        );
    }
}

#[test]
fn every_allocator_replays_one_stream_inside_a_paired_block() {
    // The seed chain has no allocator component, so a paired block is
    // identical by construction. Prove it end to end through execution.
    for pattern in SCALING_PATTERNS {
        let mut observed: Vec<(u64, u64, u64, u64)> = Vec::new();
        for allocator in ["tcmalloc", "jemalloc", "upstream-mimalloc", "mimalloc-pprof"] {
            let adapter = MockAdapter::new(allocator);
            let request = request_for(pattern, 4, 1, allocator, 300);
            let response = execute_scaling_child_request(&adapter, request).unwrap();
            observed.push((
                response.alloc_calls,
                response.realloc_calls,
                response.free_calls,
                response.checksum,
            ));
        }
        assert!(
            observed.windows(2).all(|pair| pair[0] == pair[1]),
            "{} did not replay one stream across allocators: {observed:?}",
            pattern.as_str()
        );
    }
}

fn request_for(
    pattern: ScalingPattern,
    threads: u32,
    block_id: u32,
    allocator: &str,
    operations: u64,
) -> benchmark_suite::scaling::ScalingChildRequest {
    benchmark_suite::scaling::ScalingChildRequest {
        protocol_version: "throughput-scaling-sparse-child-v1".into(),
        metric_schema_version: "throughput-scaling-sparse-v1".into(),
        run_seed: 0x6d69_6d61_6c6c_6f63,
        pattern: pattern.as_str().into(),
        thread_count: threads,
        block_id,
        ordinal: 0,
        operations_per_worker: operations,
        warmup_operations_per_worker: 0,
        allocator: AllocatorIdentity {
            allocator_id: allocator.into(),
            allocator_version: "test".into(),
            source_sha: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".into(),
            library_sha256: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                .into(),
            child_binary_sha256:
                "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc".into(),
        },
        runner: RunnerMetadata {
            os: "linux".into(),
            architecture: "x86_64".into(),
            physical_cores: 2,
            logical_cores: 4,
        },
        toolchain: ToolchainMetadata {
            rustc: "1.94.1".into(),
            target: "x86_64-unknown-linux-gnu".into(),
            compiler: "clang".into(),
            linker: "lld".into(),
        },
        reproduction_command: "test".into(),
    }
}

#[test]
fn execution_matches_the_derived_oracle_for_every_pattern_and_thread_point() {
    for pattern in SCALING_PATTERNS {
        for threads in SCALING_THREAD_POINTS {
            let adapter = MockAdapter::new("upstream-mimalloc");
            let request = request_for(pattern, threads, 0, "upstream-mimalloc", 200);
            let response = execute_scaling_child_request(&adapter, request).unwrap();
            let expected = simulate_cell(pattern, 0x6d69_6d61_6c6c_6f63, threads, 0, 200);
            assert_eq!(
                (
                    response.alloc_calls,
                    response.realloc_calls,
                    response.free_calls,
                    response.checksum
                ),
                (
                    expected.alloc_calls,
                    expected.realloc_calls,
                    expected.free_calls,
                    expected.checksum
                ),
                "{} at {threads} threads diverged from its oracle",
                pattern.as_str()
            );
            assert_eq!(response.operation_count, expected.operation_count());
            assert!(response.free_calls > 0 && response.alloc_calls > 0);
        }
    }
}

#[test]
fn cross_thread_pattern_actually_hands_blocks_to_other_workers() {
    let adapter = MockAdapter::new("upstream-mimalloc");
    let request = request_for(ScalingPattern::CrossThread, 4, 0, "upstream-mimalloc", 400);
    let response = execute_scaling_child_request(&adapter, request).unwrap();
    assert!(
        response.remote_free_calls > 0,
        "cross-thread pattern produced no remote frees"
    );
    assert_eq!(
        response.alloc_calls, response.free_calls,
        "every handed-off block must be freed exactly once"
    );
}

#[test]
fn mixed_general_pattern_exercises_realloc_and_large_buffers_touch_pages() {
    let counts = simulate_worker(
        ScalingPattern::MixedGeneral,
        stream_seed(7, ScalingPattern::MixedGeneral, 1, 0, 0),
        2_000,
        0,
        1,
    );
    assert!(
        counts.realloc_calls > 0,
        "the general mix must include realloc operations"
    );
    let adapter = MockAdapter::new("upstream-mimalloc");
    let request = request_for(ScalingPattern::LargeBuffers, 1, 0, "upstream-mimalloc", 40);
    let response = execute_scaling_child_request(&adapter, request).unwrap();
    assert!(response.alloc_calls > 0 && response.checksum != 0);
}

fn sample_run() -> ScalingRawRun {
    let text = include_str!("fixtures/scaling/scaling-raw-run.json");
    serde_json::from_str(text).expect("scaling fixture parses")
}

#[test]
fn complete_fixture_validates_and_builds_a_report() {
    let raw = sample_run();
    validate_scaling_raw_run(&raw).expect("fixture is a complete valid run");
    let report = build_scaling_report(&raw).expect("fixture builds a report");
    validate_scaling_report(&report).expect("built report is publishable");
    assert_eq!(report.rigor_label, SCALING_RIGOR_LABEL);
    assert_eq!(report.thread_points, SCALING_THREAD_POINTS.to_vec());
    assert_eq!(
        report.cell_summaries.len(),
        SCALING_PATTERNS.len() * SCALING_THREAD_POINTS.len() * 4
    );
    assert!(report
        .cell_summaries
        .iter()
        .any(|summary| summary.oversubscribed));
}

#[test]
fn validator_rejects_an_incomplete_matrix() {
    let mut raw = sample_run();
    raw.samples.pop();
    let error = validate_scaling_raw_run(&raw).expect_err("a missing sample must fail validation");
    assert!(
        error.contains("blocks, expected") || error.contains("paired block"),
        "unexpected error: {error}"
    );

    let mut raw = sample_run();
    let victim = raw.samples[0].pattern.clone();
    raw.samples.retain(|sample| sample.pattern != victim);
    assert!(validate_scaling_raw_run(&raw).is_err(), "a missing cell must fail");

    let mut raw = sample_run();
    raw.calibrations.pop();
    assert!(
        validate_scaling_raw_run(&raw).is_err(),
        "a missing calibration must fail"
    );
}

#[test]
fn validator_rejects_a_sample_that_contradicts_its_plan() {
    let mut raw = sample_run();
    raw.samples[0].response.checksum ^= 1;
    let error = validate_scaling_raw_run(&raw).expect_err("a wrong checksum must fail");
    assert!(error.contains("contradicts its derived plan"), "{error}");

    let mut raw = sample_run();
    raw.samples[0].response.alloc_calls += 1;
    assert!(validate_scaling_raw_run(&raw).is_err(), "wrong counts must fail");

    let mut raw = sample_run();
    raw.run_seed ^= 0xff;
    assert!(
        validate_scaling_raw_run(&raw).is_err(),
        "changing the run seed must invalidate every derived plan"
    );
}

#[test]
fn report_schema_rejects_downgraded_labels_and_shapes() {
    let raw = sample_run();
    let good = build_scaling_report(&raw).unwrap();

    let mut report = good.clone();
    report.rigor_label = "headline quality".into();
    assert!(
        validate_scaling_report(&report).is_err(),
        "coverage-mode labeling is mandatory"
    );

    let mut report = good.clone();
    report.thread_points = vec![1, 4, 16, 64];
    assert!(
        validate_scaling_report(&report).is_err(),
        "thread points are fixed by the protocol version"
    );

    let mut report = good.clone();
    report.status = "pending".into();
    assert!(validate_scaling_report(&report).is_err());

    let mut report = good.clone();
    report.cell_summaries.pop();
    assert!(validate_scaling_report(&report).is_err());

    let mut report = good;
    report.cell_summaries[0].block_count = SCALING_BLOCKS + 1;
    assert!(validate_scaling_report(&report).is_err());
}

#[test]
fn child_protocol_round_trips_through_json_and_self_validates() {
    // This is the exact path `benchmark-child --scaling` takes: one JSON
    // request in, one JSON response out, validated against the request.
    let request = request_for(ScalingPattern::MixedGeneral, 4, 2, "upstream-mimalloc", 250);
    let encoded = serde_json::to_vec(&request).unwrap();
    let decoded: benchmark_suite::scaling::ScalingChildRequest =
        serde_json::from_slice(&encoded).unwrap();
    assert_eq!(request, decoded);
    let adapter = MockAdapter::new("upstream-mimalloc");
    let response = execute_scaling_child_request(&adapter, decoded.clone()).unwrap();
    let round_tripped: benchmark_suite::scaling::ScalingChildResponse =
        serde_json::from_slice(&serde_json::to_vec(&response).unwrap()).unwrap();
    round_tripped
        .validate_against(&decoded)
        .expect("a truthful response validates against its request");

    let mut tampered = round_tripped.clone();
    tampered.free_calls += 1;
    assert!(
        tampered.validate_against(&decoded).is_err(),
        "an inflated free count must not validate"
    );

    // A different block is a different seed, so a different stream entirely.
    let mut other_block = decoded.clone();
    other_block.block_id += 1;
    assert!(
        round_tripped.validate_against(&other_block).is_err(),
        "a response must not validate against another block's plan"
    );

    // A single extra operation can legitimately be absorbed by a no-op draw
    // (a free of an already-empty slot), so counts alone do not pin the
    // operation budget down. The frozen-calibration cross-check in
    // `validate_scaling_raw_run` is what makes the count itself binding; here
    // we only assert that a materially different budget is rejected.
    let mut longer = decoded;
    longer.operations_per_worker += 64;
    assert!(
        round_tripped.validate_against(&longer).is_err(),
        "a response must not validate against a materially longer plan"
    );
}

#[test]
fn requests_with_undeclared_thread_points_or_patterns_are_rejected() {
    let mut request = request_for(ScalingPattern::TinyHot, 4, 0, "upstream-mimalloc", 100);
    request.thread_count = 64;
    assert!(request.validate().is_err(), "64 is not a declared point");

    let mut request = request_for(ScalingPattern::TinyHot, 4, 0, "upstream-mimalloc", 100);
    request.pattern = "sparse-unknown".into();
    assert!(request.validate().is_err(), "unknown patterns are rejected");

    let mut request = request_for(ScalingPattern::TinyHot, 4, 0, "upstream-mimalloc", 100);
    request.run_seed = 0;
    assert!(request.validate().is_err(), "a zero run seed is rejected");
}

/// Fails every allocation after a budget, to exercise the worker error path.
struct FailingAdapter {
    inner: MockAdapter,
    budget: AtomicU64,
}

impl FailingAdapter {
    fn new(budget: u64) -> Self {
        Self {
            inner: MockAdapter::new("upstream-mimalloc"),
            budget: AtomicU64::new(budget),
        }
    }
}

impl AllocatorAdapter for FailingAdapter {
    fn allocator_id(&self) -> &str {
        self.inner.allocator_id()
    }
    fn allocator_version(&self) -> &str {
        self.inner.allocator_version()
    }
    fn source_sha(&self) -> &str {
        self.inner.source_sha()
    }
    fn library_sha256(&self) -> &str {
        self.inner.library_sha256()
    }
    fn alloc(&self, size: usize) -> Result<NonNull<u8>, String> {
        if self.budget.fetch_sub(1, Ordering::Relaxed) == 0 {
            return Err("synthetic allocation failure".into());
        }
        self.inner.alloc(size)
    }
    fn calloc(&self, count: usize, size: usize) -> Result<NonNull<u8>, String> {
        self.inner.calloc(count, size)
    }
    unsafe fn realloc(&self, pointer: NonNull<u8>, size: usize) -> Result<NonNull<u8>, String> {
        unsafe { self.inner.realloc(pointer, size) }
    }
    fn aligned_alloc(&self, alignment: usize, size: usize) -> Result<NonNull<u8>, String> {
        self.inner.aligned_alloc(alignment, size)
    }
    unsafe fn free(&self, pointer: NonNull<u8>) {
        unsafe { self.inner.free(pointer) }
    }
}

#[test]
fn a_failing_worker_reports_an_error_instead_of_stranding_the_others() {
    // A worker that returns early must still reach every barrier. `Barrier`
    // has no poison state, so skipping one would hang the remaining workers
    // and the controller until the parent's watchdog killed the child with an
    // empty stderr, destroying the real diagnostic.
    for pattern in SCALING_PATTERNS {
        let adapter = FailingAdapter::new(64);
        let request = request_for(pattern, 4, 0, "upstream-mimalloc", 400);
        let error = execute_scaling_child_request(&adapter, request)
            .expect_err("a failing allocator must surface an error");
        assert!(
            error.contains("synthetic allocation failure"),
            "{} lost the underlying error: {error}",
            pattern.as_str()
        );
        // Anything the failing run still held is intentionally not asserted:
        // the child process exits on this path. Reaching this line at all is
        // the property under test.
        std::mem::forget(adapter);
    }
}

#[test]
fn overlay_accepts_a_newer_fork_build_but_not_a_moved_competitor_pin() {
    use benchmark_suite::scaling::{attach_scaling_report, LOCK_PINNED_ALLOCATORS};

    // Reproduces the first live run's failure: the sweep runs weekly and
    // overlays onto whichever daily core envelope is published, so
    // mimalloc-pprof is normally built from a newer commit than the base.
    let raw = benchmark_suite::scaling::synthetic_scaling_fixture(0x6d69_6d61_6c6c_6f63).unwrap();
    let report = build_scaling_report(&raw).unwrap();
    let core = benchmark_suite::validate::synthetic_full_fixture().unwrap();
    let validation = benchmark_suite::validate::validate_publication_raw(&core).unwrap();
    let base = benchmark_suite::report::build_latest_report(&core, validation)
        .unwrap()
        .0;
    let mut latest = base.clone();

    let mut newer_fork = report.clone();
    for sample in &mut newer_fork.raw_samples {
        if sample.allocator_id == "mimalloc-pprof" {
            sample.allocator_source_sha = "a".repeat(40);
        }
    }
    attach_scaling_report(&mut latest, newer_fork)
        .expect("a newer fork build must still overlay");
    assert!(latest.scaling.is_some());
    assert!(!latest
        .pending_metrics
        .iter()
        .any(|value| value.metric_id == "scaling"));

    let mut moved_pin = report.clone();
    for sample in &mut moved_pin.raw_samples {
        if sample.allocator_id == "upstream-mimalloc" {
            sample.allocator_source_sha = "f".repeat(40);
        }
    }
    let mut fresh = base;
    assert!(
        attach_scaling_report(&mut fresh, moved_pin).is_err(),
        "a competitor built from a different commit must be rejected"
    );
    assert_eq!(LOCK_PINNED_ALLOCATORS.len(), 3);
}

#[test]
fn counts_helper_sums_every_allocator_call() {
    let counts = ScalingCounts {
        alloc_calls: 3,
        realloc_calls: 4,
        free_calls: 5,
        checksum: 9,
    };
    assert_eq!(counts.operation_count(), 12);
}
