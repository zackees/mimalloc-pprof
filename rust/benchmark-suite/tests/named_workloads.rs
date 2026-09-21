//! Named-workload coverage for issue #216 part 2: `larson` and `xmalloc-test`,
//! added to the thread-scaling sweep alongside the four `sparse-*` synthetic
//! patterns. These are clean-room reimplementations of the workload *shapes*
//! described in the papers cited on `ScalingPattern::Larson` and
//! `ScalingPattern::XmallocTest`'s doc comments in `src/scaling.rs`, run
//! through the same seeded planner/executor/oracle protocol as every other
//! pattern -- no code from mimalloc-bench or Hoard was consulted or copied.
//!
//! The adapter and request builder below are copied from
//! `tests/scaling_contract.rs` (they are private there, and this is the
//! established convention for this crate's integration tests, which do not
//! share a `tests/common.rs`).

use std::alloc::{alloc, dealloc, realloc, Layout};
use std::collections::HashMap;
use std::ptr::NonNull;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

use benchmark_suite::execution::AllocatorAdapter;
use benchmark_suite::model::{AllocatorIdentity, RunnerMetadata, ToolchainMetadata};
use benchmark_suite::scaling::{
    execute_scaling_child_request, pattern_definitions, simulate_cell, stream_seed, PlannedAction,
    ScalingChildRequest, ScalingPattern, WorkerPlanner, SCALING_CHILD_PROTOCOL_VERSION,
    SCALING_PATTERNS, SCALING_SCHEMA_VERSION,
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

fn request_for(
    pattern: ScalingPattern,
    threads: u32,
    block_id: u32,
    allocator: &str,
    operations: u64,
) -> ScalingChildRequest {
    ScalingChildRequest {
        protocol_version: SCALING_CHILD_PROTOCOL_VERSION.into(),
        metric_schema_version: SCALING_SCHEMA_VERSION.into(),
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
            child_binary_sha256: "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
                .into(),
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
        live_telemetry_path: None,
    }
}

/// Drive a planner to exhaustion and collect every action, including the
/// final drain. Mirrors `scaling_contract.rs`'s private `actions` helper.
fn actions_for_worker(
    pattern: ScalingPattern,
    seed: u64,
    operations: u64,
    worker: u32,
    threads: u32,
) -> Vec<PlannedAction> {
    let mut planner = WorkerPlanner::new(pattern, seed, operations, worker, threads);
    let mut collected = Vec::new();
    while let Some(action) = planner.next_action() {
        collected.push(action);
    }
    collected.extend(planner.drain_actions());
    collected
}

const RUN_SEED: u64 = 0x6d69_6d61_6c6c_6f63;

#[test]
fn larson_and_xmalloc_test_are_registered_scaling_patterns() {
    assert_eq!(SCALING_PATTERNS.len(), 8);
    assert_eq!(
        ScalingPattern::parse("larson"),
        Some(ScalingPattern::Larson)
    );
    assert_eq!(
        ScalingPattern::parse("xmalloc-test"),
        Some(ScalingPattern::XmallocTest)
    );
    for pattern in SCALING_PATTERNS {
        assert_eq!(
            ScalingPattern::parse(pattern.as_str()),
            Some(pattern),
            "{} does not round-trip through as_str/parse",
            pattern.as_str()
        );
    }
    let mut seed_tags: Vec<u64> = SCALING_PATTERNS.iter().map(|p| p.seed_tag()).collect();
    seed_tags.sort_unstable();
    seed_tags.dedup();
    assert_eq!(
        seed_tags.len(),
        SCALING_PATTERNS.len(),
        "every scaling pattern must have a pairwise distinct seed tag"
    );
}

#[test]
fn larson_execution_matches_the_derived_oracle_at_several_thread_points() {
    for threads in [1, 2, 3, 4] {
        let operations = 4_000;
        let adapter = MockAdapter::new("upstream-mimalloc");
        let request = request_for(
            ScalingPattern::Larson,
            threads,
            0,
            "upstream-mimalloc",
            operations,
        );
        let response = execute_scaling_child_request(&adapter, request).unwrap();
        let expected = simulate_cell(ScalingPattern::Larson, RUN_SEED, threads, 0, operations);
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
            "larson at {threads} threads diverged from its oracle"
        );
        assert_eq!(response.operation_count, expected.operation_count());
        assert!(response.alloc_calls > 0 && response.free_calls > 0);
    }
}

#[test]
fn larson_remote_frees_appear_only_once_there_is_more_than_one_worker() {
    let adapter = MockAdapter::new("upstream-mimalloc");
    let request = request_for(ScalingPattern::Larson, 4, 0, "upstream-mimalloc", 4_000);
    let response = execute_scaling_child_request(&adapter, request).unwrap();
    assert!(
        response.remote_free_calls > 0,
        "larson's rotating tables must free some blocks on a worker other than the allocator"
    );

    let adapter = MockAdapter::new("upstream-mimalloc");
    let request = request_for(ScalingPattern::Larson, 1, 0, "upstream-mimalloc", 4_000);
    let response = execute_scaling_child_request(&adapter, request).unwrap();
    assert_eq!(
        response.remote_free_calls, 0,
        "a single worker can only ever free blocks it allocated itself"
    );
}

#[test]
fn xmalloc_test_assigns_fixed_producer_consumer_roles_by_worker_index() {
    let threads = 4u32;
    let operations = 300u64;
    for worker in 0..threads {
        let seed = stream_seed(RUN_SEED, ScalingPattern::XmallocTest, threads, 0, worker);
        let observed = actions_for_worker(
            ScalingPattern::XmallocTest,
            seed,
            operations,
            worker,
            threads,
        );
        assert!(!observed.is_empty(), "worker {worker} produced no actions");
        if worker % 2 == 0 {
            assert!(
                observed.iter().all(|action| matches!(
                    action,
                    PlannedAction::Handoff { target, .. } if target % 2 == 1
                )),
                "worker {worker} (producer) must only hand off to odd (consumer) workers: {observed:?}"
            );
        } else {
            assert!(
                observed
                    .iter()
                    .all(|action| matches!(action, PlannedAction::DrainMailbox { .. })),
                "worker {worker} (consumer) must only drain its own mailbox: {observed:?}"
            );
        }
    }

    let adapter = MockAdapter::new("upstream-mimalloc");
    let request = request_for(
        ScalingPattern::XmallocTest,
        threads,
        0,
        "upstream-mimalloc",
        operations,
    );
    let response = execute_scaling_child_request(&adapter, request).unwrap();
    assert_eq!(
        response.alloc_calls, response.free_calls,
        "every block a producer hands off must be freed exactly once"
    );
    assert!(
        response.remote_free_calls > 0,
        "xmalloc-test consumers must free blocks a different worker produced"
    );
}

#[test]
fn larson_and_xmalloc_test_streams_are_reproducible_for_identical_seeds() {
    for pattern in [ScalingPattern::Larson, ScalingPattern::XmallocTest] {
        let seed = stream_seed(0x1234_5678_9abc_def0, pattern, 4, 1, 2);
        let first = actions_for_worker(pattern, seed, 500, 2, 4);
        let second = actions_for_worker(pattern, seed, 500, 2, 4);
        assert_eq!(
            first,
            second,
            "{} stream is not reproducible for an identical seed",
            pattern.as_str()
        );
        assert!(!first.is_empty());
    }
}

#[test]
fn pattern_definitions_list_all_patterns_including_the_two_named_workloads() {
    let definitions = pattern_definitions();
    assert_eq!(definitions.len(), 8);
    let names: Vec<&str> = definitions.iter().map(|d| d.pattern.as_str()).collect();
    assert!(names.contains(&"larson"), "{names:?}");
    assert!(names.contains(&"xmalloc-test"), "{names:?}");
    // Adding patterns is part of `scaling_comparison_key`'s input by
    // construction (`pattern_definitions()` feeds it directly), so a run
    // recorded against the old four-pattern catalogue can never compare
    // equal to one against this eight-pattern catalogue.
    assert!(!names.contains(&"sparse-unknown"));
}
