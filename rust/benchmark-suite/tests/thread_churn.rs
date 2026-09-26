//! #508: the `thread-churn` workload. It replays `large-class-ephemeral`'s
//! stream at `THREAD_CHURN_THREADS` workers, joins every worker thread, and
//! then keeps the child alive and idle while it samples its own RSS at
//! `THREAD_CHURN_POST_DRAIN_OFFSETS_MS` after the drain.
//!
//! The adapter and request builder are copied from `tests/scaling_contract.rs`
//! (this crate's integration tests do not share a `tests/common.rs`). This
//! adapter additionally counts the threads that allocated through it and the
//! ones that have exited, so a test can tell whether every worker was joined
//! before a sample was taken.

use std::alloc::{alloc, dealloc, realloc, Layout};
use std::cell::RefCell;
use std::collections::{HashMap, HashSet};
use std::ptr::NonNull;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::ThreadId;
use std::time::{Duration, Instant};

use benchmark_suite::execution::AllocatorAdapter;
use benchmark_suite::model::{AllocatorIdentity, RunnerMetadata, ToolchainMetadata};
use benchmark_suite::scaling::{
    execute_scaling_child_request, execute_scaling_child_request_with_rss_probe, simulate_cell,
    thread_churn_release_ms, validate_thread_churn_samples, ScalingChildRequest, ScalingPattern,
    SCALING_CHILD_PROTOCOL_VERSION, SCALING_PATTERNS, SCALING_SCHEMA_VERSION,
    THREAD_CHURN_POST_DRAIN_OFFSETS_MS, THREAD_CHURN_THREADS,
};

const RUN_SEED: u64 = 0x6d69_6d61_6c6c_6f63;
const OPERATIONS: u64 = 400;

/// Bumps its counter when the thread that owns it exits (thread-local
/// destructors run before `join` returns).
struct ExitMarker(Arc<AtomicU64>);

impl Drop for ExitMarker {
    fn drop(&mut self) {
        self.0.fetch_add(1, Ordering::SeqCst);
    }
}

thread_local! {
    static EXIT_MARKER: RefCell<Option<ExitMarker>> = const { RefCell::new(None) };
}

/// Leak-detecting mock allocator that also records which threads allocated
/// and how many of them have exited.
struct TrackingAdapter {
    id: &'static str,
    layouts: Mutex<HashMap<usize, Layout>>,
    alloc_threads: Mutex<HashSet<ThreadId>>,
    exited_threads: Arc<AtomicU64>,
}

impl TrackingAdapter {
    fn new(id: &'static str) -> Self {
        Self {
            id,
            layouts: Mutex::new(HashMap::new()),
            alloc_threads: Mutex::new(HashSet::new()),
            exited_threads: Arc::new(AtomicU64::new(0)),
        }
    }

    fn record(&self, pointer: NonNull<u8>, layout: Layout) {
        if self
            .alloc_threads
            .lock()
            .unwrap()
            .insert(std::thread::current().id())
        {
            let counter = Arc::clone(&self.exited_threads);
            EXIT_MARKER.with(|marker| *marker.borrow_mut() = Some(ExitMarker(counter)));
        }
        self.layouts
            .lock()
            .unwrap()
            .insert(pointer.as_ptr() as usize, layout);
    }

    /// (threads that allocated, threads among them that have exited).
    fn thread_census(&self) -> (u64, u64) {
        (
            self.alloc_threads.lock().unwrap().len() as u64,
            self.exited_threads.load(Ordering::SeqCst),
        )
    }
}

impl Drop for TrackingAdapter {
    fn drop(&mut self) {
        assert!(
            self.layouts.get_mut().unwrap().is_empty(),
            "thread-churn leaked blocks"
        );
    }
}

impl AllocatorAdapter for TrackingAdapter {
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
        self.record(pointer, layout);
        Ok(pointer)
    }
    fn calloc(&self, count: usize, size: usize) -> Result<NonNull<u8>, String> {
        self.alloc(count * size)
    }
    unsafe fn realloc(&self, pointer: NonNull<u8>, size: usize) -> Result<NonNull<u8>, String> {
        let old = self
            .layouts
            .lock()
            .unwrap()
            .remove(&(pointer.as_ptr() as usize))
            .ok_or("unknown realloc pointer")?;
        let updated = NonNull::new(unsafe { realloc(pointer.as_ptr(), old, size.max(1)) })
            .ok_or("mock realloc failed")?;
        self.record(
            updated,
            Layout::from_size_align(size.max(1), old.align()).unwrap(),
        );
        Ok(updated)
    }
    fn aligned_alloc(&self, alignment: usize, size: usize) -> Result<NonNull<u8>, String> {
        let layout = Layout::from_size_align(size.max(1), alignment).unwrap();
        let pointer = NonNull::new(unsafe { alloc(layout) }).ok_or("mock aligned alloc failed")?;
        self.record(pointer, layout);
        Ok(pointer)
    }
    unsafe fn free(&self, pointer: NonNull<u8>) {
        let layout = self
            .layouts
            .lock()
            .unwrap()
            .remove(&(pointer.as_ptr() as usize))
            .expect("mock free of an unknown pointer");
        unsafe { dealloc(pointer.as_ptr(), layout) };
    }
}

fn request_for(pattern: ScalingPattern, block_id: u32, allocator: &str) -> ScalingChildRequest {
    ScalingChildRequest {
        protocol_version: SCALING_CHILD_PROTOCOL_VERSION.into(),
        metric_schema_version: SCALING_SCHEMA_VERSION.into(),
        run_seed: RUN_SEED,
        pattern: pattern.as_str().into(),
        thread_count: THREAD_CHURN_THREADS,
        block_id,
        ordinal: 0,
        operations_per_worker: OPERATIONS,
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

#[test]
fn thread_churn_is_a_requestable_pattern_outside_the_sweep() {
    let pattern = ScalingPattern::parse("thread-churn").expect("thread-churn parses");
    assert_eq!(pattern, ScalingPattern::ThreadChurn);
    assert!(pattern.samples_after_drain());
    assert!(!SCALING_PATTERNS.contains(&pattern));
    assert!(SCALING_PATTERNS
        .iter()
        .all(|pattern| !pattern.samples_after_drain()));
    assert_eq!(
        pattern.generations(),
        ScalingPattern::LargeClassEphemeral.generations()
    );
    assert_eq!(pattern.spec(), ScalingPattern::LargeClassEphemeral.spec());
}

/// The acceptance test: after the drain the child reports one RSS sample per
/// fixed offset, never early; every worker thread (and every short-lived
/// generation thread inside it) has been joined before the first sample; and
/// the stream is large-class-ephemeral's (same counts and checksum).
#[test]
fn thread_churn_samples_rss_at_fixed_offsets_after_every_worker_is_joined() {
    let adapter = TrackingAdapter::new("mimalloc-pprof");
    let request = request_for(ScalingPattern::ThreadChurn, 0, "mimalloc-pprof");
    let mut probes: Vec<(Instant, (u64, u64))> = Vec::new();
    let response =
        execute_scaling_child_request_with_rss_probe(&adapter, request.clone(), &mut || {
            probes.push((Instant::now(), adapter.thread_census()));
            // Synthetic RSS that falls with every sample.
            Ok((64 - probes.len() as u64) << 20)
        })
        .expect("thread-churn executes");

    assert_eq!(probes.len(), THREAD_CHURN_POST_DRAIN_OFFSETS_MS.len());
    let (allocated, _) = adapter.thread_census();
    // Each worker runs its stream as `generations()` short-lived threads.
    assert!(
        allocated
            >= u64::from(THREAD_CHURN_THREADS)
                * u64::from(ScalingPattern::ThreadChurn.generations()),
        "only {allocated} threads allocated"
    );
    for (_, (allocated, exited)) in &probes {
        assert_eq!(
            allocated, exited,
            "a sample was taken while an allocating thread was still alive"
        );
    }

    // One sample per fixed offset, never before it, in order.
    assert_eq!(
        response.post_drain_offsets_ns.len(),
        THREAD_CHURN_POST_DRAIN_OFFSETS_MS.len()
    );
    for (observed, target_ms) in response
        .post_drain_offsets_ns
        .iter()
        .zip(THREAD_CHURN_POST_DRAIN_OFFSETS_MS)
    {
        assert!(
            *observed >= target_ms * 1_000_000,
            "{observed} ns < {target_ms} ms"
        );
    }
    assert!(response
        .post_drain_offsets_ns
        .windows(2)
        .all(|pair| pair[0] < pair[1]));
    // The probe was called at those offsets: the gaps between calls are at
    // least the gaps between the targets.
    for (index, pair) in probes.windows(2).enumerate() {
        let gap = pair[1].0 - pair[0].0;
        let target = Duration::from_millis(
            THREAD_CHURN_POST_DRAIN_OFFSETS_MS[index + 1]
                - THREAD_CHURN_POST_DRAIN_OFFSETS_MS[index],
        );
        // The first sample of a pair may itself have been late; allow for it.
        let slack = Duration::from_nanos(
            response.post_drain_offsets_ns[index]
                - THREAD_CHURN_POST_DRAIN_OFFSETS_MS[index] * 1_000_000,
        );
        assert!(
            gap + slack >= target,
            "samples {index}..{} were {gap:?} apart",
            index + 1
        );
    }
    assert_eq!(
        response.post_drain_rss_bytes,
        (1..=THREAD_CHURN_POST_DRAIN_OFFSETS_MS.len() as u64)
            .map(|index| (64 - index) << 20)
            .collect::<Vec<_>>()
    );
    response
        .validate_against(&request)
        .expect("thread-churn response validates against its plan");

    // The stream is large-class-ephemeral's: the same plan, and the same
    // counts and checksum when it is executed.
    let expected = simulate_cell(
        ScalingPattern::LargeClassEphemeral,
        RUN_SEED,
        THREAD_CHURN_THREADS,
        0,
        OPERATIONS,
    );
    assert_eq!(
        simulate_cell(
            ScalingPattern::ThreadChurn,
            RUN_SEED,
            THREAD_CHURN_THREADS,
            0,
            OPERATIONS
        ),
        expected
    );
    let ephemeral_adapter = TrackingAdapter::new("mimalloc-pprof");
    let ephemeral = execute_scaling_child_request(
        &ephemeral_adapter,
        request_for(ScalingPattern::LargeClassEphemeral, 0, "mimalloc-pprof"),
    )
    .expect("large-class-ephemeral executes");
    let counts = |value: &benchmark_suite::scaling::ScalingChildResponse| {
        (
            value.alloc_calls,
            value.realloc_calls,
            value.free_calls,
            value.operation_count,
            value.checksum,
            value.worker_seeds.clone(),
            value.size_histogram.clone(),
        )
    };
    assert_eq!(counts(&response), counts(&ephemeral));
    assert_eq!(
        (response.alloc_calls, response.free_calls, response.checksum),
        (expected.alloc_calls, expected.free_calls, expected.checksum)
    );
    // Only thread-churn idles after the drain.
    assert!(
        ephemeral.post_drain_offsets_ns.is_empty() && ephemeral.post_drain_rss_bytes.is_empty()
    );
}

/// The production probe reads the child's own `/proc/self/statm`.
#[cfg(target_os = "linux")]
#[test]
fn thread_churn_reads_its_own_rss_from_proc_self_statm() {
    let adapter = TrackingAdapter::new("upstream-mimalloc");
    let request = request_for(ScalingPattern::ThreadChurn, 1, "upstream-mimalloc");
    let response = execute_scaling_child_request(&adapter, request.clone()).unwrap();
    assert!(response.post_drain_rss_bytes.iter().all(|rss| *rss > 0));
    response.validate_against(&request).unwrap();
    assert!(benchmark_suite::scaling::read_self_rss_bytes().unwrap() > 0);
}

#[test]
fn post_drain_samples_are_rejected_when_missing_early_or_on_another_pattern() {
    let adapter = TrackingAdapter::new("mimalloc-pprof");
    let request = request_for(ScalingPattern::ThreadChurn, 2, "mimalloc-pprof");
    let good =
        execute_scaling_child_request_with_rss_probe(
            &adapter,
            request.clone(),
            &mut || Ok(1 << 20),
        )
        .unwrap();
    good.validate_against(&request).unwrap();

    let mut missing = good.clone();
    missing.post_drain_rss_bytes.pop();
    assert!(missing.validate_against(&request).is_err());

    let mut early = good.clone();
    early.post_drain_offsets_ns[0] = THREAD_CHURN_POST_DRAIN_OFFSETS_MS[0] * 1_000_000 - 1;
    assert!(early.validate_against(&request).is_err());

    let mut empty = good.clone();
    empty.post_drain_rss_bytes[3] = 0;
    assert!(empty.validate_against(&request).is_err());

    // A sweep pattern must not carry post-drain samples.
    let ephemeral_request = request_for(ScalingPattern::LargeClassEphemeral, 2, "mimalloc-pprof");
    let mut smuggled = good.clone();
    assert!(smuggled
        .validate_post_drain(ScalingPattern::LargeClassEphemeral)
        .is_err());
    smuggled.post_drain_offsets_ns.clear();
    smuggled.post_drain_rss_bytes.clear();
    smuggled.validate_against(&ephemeral_request).unwrap();
}

#[test]
fn release_time_is_the_first_sample_within_one_mib_of_the_final_rss() {
    const MIB: u64 = 1 << 20;
    assert_eq!(
        thread_churn_release_ms(&[200 * MIB, 150 * MIB, 40 * MIB, 5 * MIB, 5 * MIB, 5 * MIB]),
        1500
    );
    // Within the tolerance counts as released.
    assert_eq!(
        thread_churn_release_ms(&[6 * MIB, 5 * MIB, 5 * MIB, 5 * MIB, 5 * MIB, 5 * MIB]),
        100
    );
    // An allocator that never gives anything back is "released" at once:
    // perf-ab's definition measures when RSS settles, not how far it falls.
    assert_eq!(thread_churn_release_ms(&[200 * MIB; 6]), 100);
    assert_eq!(
        thread_churn_release_ms(&[
            200 * MIB,
            200 * MIB,
            200 * MIB,
            200 * MIB,
            200 * MIB,
            5 * MIB
        ]),
        3000
    );
}

#[test]
fn thread_churn_samples_must_replay_the_frozen_ephemeral_operation_count() {
    let raw = benchmark_suite::scaling::synthetic_scaling_fixture(RUN_SEED).unwrap();
    let operations = raw
        .calibrations
        .iter()
        .find(|value| {
            value.pattern == ScalingPattern::LargeClassEphemeral.as_str()
                && value.thread_count == THREAD_CHURN_THREADS
        })
        .unwrap()
        .operations_per_worker;
    let blocks = benchmark_suite::scaling::THREAD_CHURN_BLOCKS;
    validate_thread_churn_samples(RUN_SEED, &raw.thread_churn_samples, operations, blocks).unwrap();
    assert!(validate_thread_churn_samples(
        RUN_SEED,
        &raw.thread_churn_samples,
        operations + 1,
        blocks
    )
    .unwrap_err()
    .contains("frozen operation count"));
    let mut truncated = raw.thread_churn_samples.clone();
    truncated.pop();
    assert!(validate_thread_churn_samples(RUN_SEED, &truncated, operations, blocks).is_err());
    let mut tampered = raw.thread_churn_samples.clone();
    tampered[0].response.checksum ^= 1;
    assert!(
        validate_thread_churn_samples(RUN_SEED, &tampered, operations, blocks)
            .unwrap_err()
            .contains("derived plan")
    );
    let mut diagnostic = raw.thread_churn_samples.clone();
    diagnostic[0].diagnostic_peak_rss_bytes = 1;
    assert!(validate_thread_churn_samples(RUN_SEED, &diagnostic, operations, blocks).is_err());
}
