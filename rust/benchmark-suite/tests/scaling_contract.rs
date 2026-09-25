use std::alloc::{alloc, dealloc, realloc, Layout};
use std::collections::{HashMap, HashSet};
use std::ptr::NonNull;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use std::thread::ThreadId;

use benchmark_suite::execution::AllocatorAdapter;
use benchmark_suite::model::{AllocatorIdentity, RunnerMetadata, ToolchainMetadata};
use benchmark_suite::scaling::{
    build_scaling_report, execute_scaling_child_request, simulate_cell, simulate_worker,
    stream_seed, validate_scaling_raw_run, validate_scaling_report, PlannedAction, ScalingCounts,
    ScalingPattern, ScalingRawRun, WorkerPlanner, SCALING_BLOCKS, SCALING_CHILD_PROTOCOL_VERSION,
    SCALING_PATTERNS, SCALING_RIGOR_LABEL, SCALING_SCHEMA_VERSION, SCALING_THREAD_POINTS,
};
use benchmark_suite::scaling::{merge_scaling_runs, scaling_thread_points_for_shard};
use benchmark_suite::scaling::{
    ScalingChildResponse, CHURN_BLOCKS, CHURN_POST_DRAIN_OFFSETS_MS, CHURN_RELEASE_TOLERANCE_BYTES,
    CHURN_THREAD_POINTS, SCALING_CHURN_SCHEMA_VERSION,
};

/// Leak-detecting mock allocator. `Drop` asserts every block was released, so
/// any oracle/executor drift shows up as a failure rather than a leak.
struct MockAdapter {
    id: &'static str,
    layouts: Mutex<HashMap<usize, Layout>>,
    frees: AtomicU64,
    /// Threads that allocated, and the allocating thread of each live block.
    alloc_threads: Mutex<HashSet<ThreadId>>,
    owners: Mutex<HashMap<usize, ThreadId>>,
    foreign_frees: AtomicU64,
}

impl MockAdapter {
    fn new(id: &'static str) -> Self {
        Self {
            id,
            layouts: Mutex::new(HashMap::new()),
            frees: AtomicU64::new(0),
            alloc_threads: Mutex::new(HashSet::new()),
            owners: Mutex::new(HashMap::new()),
            foreign_frees: AtomicU64::new(0),
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
        let thread = std::thread::current().id();
        self.alloc_threads.lock().unwrap().insert(thread);
        self.owners
            .lock()
            .unwrap()
            .insert(pointer.as_ptr() as usize, thread);
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
        let owner = self
            .owners
            .lock()
            .unwrap()
            .remove(&(pointer.as_ptr() as usize));
        if owner.is_some_and(|owner| owner != std::thread::current().id()) {
            self.foreign_frees.fetch_add(1, Ordering::Relaxed);
        }
        self.frees.fetch_add(1, Ordering::Relaxed);
        unsafe { dealloc(pointer.as_ptr(), layout) };
    }
}

fn actions(
    pattern: ScalingPattern,
    seed: u64,
    operations: u64,
    threads: u32,
) -> Vec<PlannedAction> {
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
        assert_eq!(
            first,
            second,
            "{} stream is not reproducible",
            pattern.as_str()
        );
        assert!(!first.is_empty());
    }
}

#[test]
fn distribution_sizes_obey_bounds_and_power_of_two_contract() {
    for pattern in [ScalingPattern::PowerOfTwoLarge, ScalingPattern::RandomLarge] {
        let seed = stream_seed(0x1234_5678_9abc_def0, pattern, 4, 9, 2);
        for action in actions(pattern, seed, 20_000, 4) {
            let size = match action {
                PlannedAction::Alloc { size, .. } | PlannedAction::ReallocSlot { size, .. } => size,
                _ => continue,
            };
            assert!((64 * 1024..=4 * 1024 * 1024).contains(&size));
            if pattern == ScalingPattern::PowerOfTwoLarge {
                assert!(size.is_power_of_two());
            }
        }
    }
}

#[test]
fn distribution_size_draws_do_not_change_lifetime_playback() {
    fn lifetime(action: PlannedAction) -> (u8, usize) {
        match action {
            PlannedAction::Alloc { slot, .. } => (0, slot),
            PlannedAction::FreeSlot { slot } => (1, slot),
            PlannedAction::ReallocSlot { slot, .. } => (2, slot),
            PlannedAction::Handoff { target, .. } => (3, target as usize),
            PlannedAction::DrainMailbox { budget } => (4, budget as usize),
        }
    }
    let master = 0x1234_5678_9abc_def0;
    let p2_seed = stream_seed(master, ScalingPattern::PowerOfTwoLarge, 4, 3, 1);
    let random_seed = stream_seed(master, ScalingPattern::RandomLarge, 4, 3, 1);
    // Workload tags deliberately give workers different root seeds. Reusing
    // one root here isolates the promised property: the independent size
    // stream cannot perturb the lifetime stream.
    let p2 = actions(ScalingPattern::PowerOfTwoLarge, p2_seed, 2_000, 4)
        .into_iter()
        .map(lifetime)
        .collect::<Vec<_>>();
    let random = actions(ScalingPattern::RandomLarge, p2_seed, 2_000, 4)
        .into_iter()
        .map(lifetime)
        .collect::<Vec<_>>();
    assert_eq!(p2, random);
    assert_ne!(
        p2_seed, random_seed,
        "workload identity must affect worker seeds"
    );
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
        for allocator in [
            "tcmalloc",
            "jemalloc",
            "upstream-mimalloc",
            "bun-mimalloc",
            "mimalloc-pprof",
        ] {
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
fn ephemeral_large_class_replays_the_control_on_short_lived_threads() {
    let run = |pattern: ScalingPattern| {
        let adapter = MockAdapter::new("upstream-mimalloc");
        let request = request_for(pattern, 1, 0, "upstream-mimalloc", 800);
        let response = execute_scaling_child_request(&adapter, request).unwrap();
        let threads = adapter.alloc_threads.lock().unwrap().len();
        let foreign = adapter.foreign_frees.load(Ordering::Relaxed);
        (
            (response.alloc_calls, response.free_calls, response.checksum),
            threads,
            foreign,
        )
    };
    let (control, control_threads, control_foreign) = run(ScalingPattern::LargeClassPersistent);
    let (ephemeral, ephemeral_threads, ephemeral_foreign) =
        run(ScalingPattern::LargeClassEphemeral);
    assert_eq!(
        control, ephemeral,
        "thread lifetime must be the only variable"
    );
    assert_eq!((control_threads, control_foreign), (1, 0));
    // Every generation is its own thread (ThreadIds are never reused), and
    // blocks outlive the thread that allocated them.
    assert!(ephemeral_threads >= ScalingPattern::LargeClassEphemeral.generations() as usize);
    assert!(
        ephemeral_foreign > 0,
        "no block outlived its allocating thread"
    );
}

#[test]
fn thread_churn_replays_large_class_ephemeral_and_samples_after_join() {
    let run_seed = 0x6d69_6d61_6c6c_6f63;
    let run = |pattern: ScalingPattern| {
        let adapter = MockAdapter::new("upstream-mimalloc");
        let request = request_for(pattern, 8, 0, "upstream-mimalloc", 800);
        let response = execute_scaling_child_request(&adapter, request.clone()).unwrap();
        (request, response)
    };
    let counts = |response: &ScalingChildResponse| {
        (
            response.alloc_calls,
            response.realloc_calls,
            response.free_calls,
            response.checksum,
        )
    };
    let (churn_request, churn) = run(ScalingPattern::ThreadChurn);
    let (_, ephemeral) = run(ScalingPattern::LargeClassEphemeral);

    // The churn cell replays large-class-ephemeral's exact stream.
    let expected = simulate_cell(ScalingPattern::ThreadChurn, run_seed, 8, 0, 800);
    assert_eq!(
        expected,
        simulate_cell(ScalingPattern::LargeClassEphemeral, run_seed, 8, 0, 800)
    );
    assert_eq!(counts(&churn), counts(&ephemeral));
    assert_eq!(
        counts(&churn),
        (
            expected.alloc_calls,
            expected.realloc_calls,
            expected.free_calls,
            expected.checksum
        )
    );

    // It samples at every offset, no earlier than the offset, after the join.
    assert_eq!(CHURN_POST_DRAIN_OFFSETS_MS.len(), 6);
    assert_eq!(churn.post_drain_rss_bytes.len(), 6);
    assert_eq!(churn.post_drain_sample_ns.len(), 6);
    for (sample_ns, offset_ms) in churn
        .post_drain_sample_ns
        .iter()
        .zip(CHURN_POST_DRAIN_OFFSETS_MS)
    {
        assert!(
            *sample_ns >= offset_ms * 1_000_000,
            "sample read at {sample_ns} ns, before its {offset_ms} ms offset"
        );
    }
    assert_eq!(churn.live_worker_threads_at_first_sample, Some(0));
    if cfg!(target_os = "linux") {
        assert!(
            churn.post_drain_rss_bytes.iter().all(|rss| *rss > 0),
            "every post-drain sample must observe RSS on Linux"
        );
    }
    churn
        .validate_against(&churn_request)
        .expect("a truthful thread-churn response validates");

    let mut still_alive = churn.clone();
    still_alive.live_worker_threads_at_first_sample = Some(1);
    assert!(
        still_alive.validate_against(&churn_request).is_err(),
        "sampling while a worker is still alive must not validate"
    );
    let mut truncated = churn;
    truncated.post_drain_rss_bytes.pop();
    assert!(truncated.validate_against(&churn_request).is_err());

    // Every other pattern carries no post-drain samples.
    assert!(ephemeral.post_drain_rss_bytes.is_empty());
    assert!(ephemeral.post_drain_sample_ns.is_empty());
    assert_eq!(ephemeral.live_worker_threads_at_first_sample, None);
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

/// A complete raw run for the current pattern catalogue, round-tripped
/// through JSON so the wire shape is exercised exactly as the controller
/// writes and the validator reads it. Built from the same oracle production
/// uses rather than read from a checked-in file, so adding a pattern (#216)
/// cannot leave the contract tests validating a stale catalogue.
fn sample_run() -> ScalingRawRun {
    let raw = benchmark_suite::scaling::synthetic_scaling_fixture(0x6d69_6d61_6c6c_6f63)
        .expect("synthetic scaling fixture builds");
    let text = serde_json::to_string(&raw).expect("scaling fixture serializes");
    serde_json::from_str(&text).expect("scaling fixture parses")
}

fn split_fixture(raw: &ScalingRawRun, shard_count: usize) -> Vec<ScalingRawRun> {
    (0..shard_count)
        .map(|shard_index| {
            let threads = scaling_thread_points_for_shard(shard_index, shard_count).unwrap();
            let mut shard = raw.clone();
            shard.status = "incomplete".into();
            shard
                .calibrations
                .retain(|value| threads.contains(&value.thread_count));
            shard
                .samples
                .retain(|value| threads.contains(&value.thread_count));
            shard
                .churn_samples
                .retain(|value| threads.contains(&value.thread_count));
            shard.run.generated_at_utc = format!("2026-08-13T00:00:0{shard_index}Z");
            shard
        })
        .collect()
}

#[test]
fn scaling_shards_are_deterministic_and_cover_the_matrix_once() {
    let assigned = (0..SCALING_THREAD_POINTS.len())
        .flat_map(|index| scaling_thread_points_for_shard(index, 6).unwrap())
        .collect::<Vec<_>>();
    assert_eq!(assigned, SCALING_THREAD_POINTS);
    assert_eq!(scaling_thread_points_for_shard(2, 6).unwrap(), vec![3]);
    assert!(scaling_thread_points_for_shard(0, 0).is_err());
    assert!(scaling_thread_points_for_shard(6, 6).is_err());
}

#[test]
fn scaling_shards_merge_to_a_complete_valid_run() {
    let raw = sample_run();
    let shards = split_fixture(&raw, 6);
    // The churn side-car travels with the shard that owns its thread point.
    for (index, shard) in shards.iter().enumerate() {
        let threads = scaling_thread_points_for_shard(index, 6).unwrap();
        let owns_churn = CHURN_THREAD_POINTS
            .iter()
            .any(|churn| threads.contains(churn));
        assert_eq!(shard.churn_samples.is_empty(), !owns_churn, "{index}");
    }
    let merged = merge_scaling_runs(shards).unwrap();
    validate_scaling_raw_run(&merged).unwrap();
    assert_eq!(merged.status, "complete");
    assert_eq!(merged.run.generated_at_utc, raw.run.generated_at_utc);
    assert_eq!(merged.calibrations.len(), raw.calibrations.len());
    assert_eq!(merged.samples.len(), raw.samples.len());
    assert_eq!(merged.churn_samples, raw.churn_samples);

    // Fewer shards route churn the same way.
    let merged = merge_scaling_runs(split_fixture(&raw, 2)).unwrap();
    assert_eq!(merged.churn_samples, raw.churn_samples);
}

#[test]
fn scaling_merge_rejects_mismatch_missing_and_overlap() {
    let raw = sample_run();

    let mut shards = split_fixture(&raw, 6);
    shards[1].run_seed ^= 1;
    assert!(merge_scaling_runs(shards).unwrap_err().contains("run seed"));

    let mut shards = split_fixture(&raw, 6);
    shards[1].runner.cpu_model.push_str("-different");
    assert!(merge_scaling_runs(shards).unwrap_err().contains("runner"));

    let mut shards = split_fixture(&raw, 6);
    shards[1].topology.logical_cores += 1;
    assert!(merge_scaling_runs(shards).unwrap_err().contains("topology"));

    let mut shards = split_fixture(&raw, 6);
    shards[1].allocators[0].compiler.push_str("-different");
    assert!(merge_scaling_runs(shards)
        .unwrap_err()
        .contains("allocator identities"));

    let mut shards = split_fixture(&raw, 6);
    shards.pop();
    assert!(merge_scaling_runs(shards).is_err());

    let mut shards = split_fixture(&raw, 6);
    shards.push(shards[0].clone());
    assert!(merge_scaling_runs(shards).unwrap_err().contains("overlap"));

    // A second copy of the churn cell in another shard is an overlap too.
    let mut shards = split_fixture(&raw, 6);
    shards[0].churn_samples = raw.churn_samples.clone();
    assert!(merge_scaling_runs(shards).unwrap_err().contains("overlap"));

    // Without its churn side-car the merged run is not complete.
    let mut shards = split_fixture(&raw, 6);
    for shard in &mut shards {
        shard.churn_samples.clear();
    }
    assert_eq!(merge_scaling_runs(shards).unwrap().status, "incomplete");
}

#[test]
fn churn_side_car_is_required_and_summarised() {
    let raw = sample_run();
    validate_scaling_raw_run(&raw).expect("fixture carries a complete churn side-car");
    assert_eq!(
        raw.churn_samples.len(),
        CHURN_THREAD_POINTS.len() * CHURN_BLOCKS as usize * 5
    );
    let report = build_scaling_report(&raw).expect("fixture builds a report");
    validate_scaling_report(&report).expect("report with churn side-car is publishable");
    assert_eq!(report.churn_raw_samples, raw.churn_samples);
    let churn = report.churn.as_ref().expect("report carries the churn side-car");
    assert_eq!(churn.metric_schema_version, SCALING_CHURN_SCHEMA_VERSION);
    assert_eq!(churn.pattern, ScalingPattern::ThreadChurn.as_str());
    assert_eq!(
        churn.replays_pattern,
        ScalingPattern::LargeClassEphemeral.as_str()
    );
    assert_eq!(churn.sampling.offsets_ms, CHURN_POST_DRAIN_OFFSETS_MS.to_vec());
    assert_eq!(
        churn.sampling.release_tolerance_bytes,
        CHURN_RELEASE_TOLERANCE_BYTES
    );
    assert_eq!(churn.cell_summaries.len(), 5);
    for summary in &churn.cell_summaries {
        assert_eq!(summary.thread_count, 8);
        assert_eq!(summary.block_count, CHURN_BLOCKS);
        assert_eq!(summary.median_post_drain_rss_bytes.len(), 6);
        assert_eq!(summary.p05_post_drain_rss_bytes.len(), 6);
        assert_eq!(summary.p95_post_drain_rss_bytes.len(), 6);
    }
    assert!(
        churn
            .cell_summaries
            .windows(2)
            .all(|pair| pair[0].allocator_id < pair[1].allocator_id),
        "summaries are sorted by (thread_count, allocator_id)"
    );
    assert_eq!(
        report.history_projection().churn.as_ref(),
        Some(churn),
        "history carries the churn side-car"
    );

    // release_ms by hand from jemalloc's raw samples: the first offset whose
    // RSS is within the tolerance of the RSS at the last offset, then the
    // linear-interpolated median across blocks.
    fn linear_median(sorted: &[u64]) -> u64 {
        let h = (sorted.len() - 1) as f64 * 0.5;
        let lower = h.floor() as usize;
        let upper = h.ceil() as usize;
        let span = (sorted[upper] - sorted[lower]) as f64;
        (sorted[lower] as f64 + span * (h - lower as f64)).round() as u64
    }
    let allocator = "jemalloc";
    let mine = raw
        .churn_samples
        .iter()
        .filter(|sample| sample.allocator_id == allocator)
        .collect::<Vec<_>>();
    assert_eq!(mine.len(), CHURN_BLOCKS as usize);
    let mut release = mine
        .iter()
        .map(|sample| {
            let rss = &sample.response.post_drain_rss_bytes;
            let last = *rss.last().unwrap();
            let index = rss
                .iter()
                .position(|value| *value <= last + CHURN_RELEASE_TOLERANCE_BYTES)
                .unwrap();
            CHURN_POST_DRAIN_OFFSETS_MS[index]
        })
        .collect::<Vec<_>>();
    release.sort_unstable();
    let mut first_offset = mine
        .iter()
        .map(|sample| sample.response.post_drain_rss_bytes[0])
        .collect::<Vec<_>>();
    first_offset.sort_unstable();
    let summary = churn
        .cell_summaries
        .iter()
        .find(|summary| summary.allocator_id == allocator)
        .expect("jemalloc churn summary");
    assert_eq!(summary.median_release_ms, linear_median(&release));
    assert_eq!(
        summary.median_post_drain_rss_bytes[0],
        linear_median(&first_offset)
    );
    let distinct_release = churn
        .cell_summaries
        .iter()
        .map(|summary| summary.median_release_ms)
        .collect::<HashSet<_>>();
    assert!(
        distinct_release.len() > 1,
        "the fixture must separate allocators by release time"
    );

    // A fresh run must carry the complete side-car.
    let mut missing = raw.clone();
    missing.churn_samples.clear();
    assert!(validate_scaling_raw_run(&missing).is_err());

    let mut still_alive = raw.clone();
    still_alive.churn_samples[0].response.live_worker_threads_at_first_sample = Some(1);
    assert!(validate_scaling_raw_run(&still_alive).is_err());

    let mut duplicated = raw.clone();
    duplicated.churn_samples.push(raw.churn_samples[0].clone());
    assert!(validate_scaling_raw_run(&duplicated).is_err());

    let mut recalibrated = raw.clone();
    recalibrated.churn_samples[0].operations_per_worker += 1;
    assert!(validate_scaling_raw_run(&recalibrated).is_err());

    // Rows of every other pattern serialize without the new fields.
    let text = serde_json::to_string(&raw.samples[0]).unwrap();
    assert!(!text.contains("post_drain") && !text.contains("live_worker_threads"));

    let mut unordered = report.clone();
    let summaries = &mut unordered.churn.as_mut().unwrap().cell_summaries;
    summaries.swap(0, 1);
    assert!(validate_scaling_report(&unordered).is_err());
}

#[test]
fn checked_in_pre_named_workload_fixture_still_deserializes() {
    // `fixtures/scaling/scaling-raw-run.json` was recorded under the four
    // `sparse-*` patterns, before larson/xmalloc-test (#216). Raw runs of that
    // lineage must still deserialize (published rows and `--base-latest`
    // inputs carry the same shapes), even though a fresh run is now required
    // to cover all six patterns.
    let text = include_str!("fixtures/scaling/scaling-raw-run.json");
    let raw: ScalingRawRun = serde_json::from_str(text).expect("legacy scaling fixture parses");
    assert!(!raw.samples.is_empty());
    assert!(raw
        .samples
        .iter()
        .all(|sample| ScalingPattern::parse(&sample.pattern).is_some()));
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
        SCALING_PATTERNS.len() * SCALING_THREAD_POINTS.len() * 5
    );
    assert!(report
        .cell_summaries
        .iter()
        .any(|summary| summary.oversubscribed));
}

#[test]
fn distribution_rss_uses_linear_interpolated_p5_p50_p95() {
    let mut raw = sample_run();
    let mut ordinal = 1u64;
    for sample in raw.samples.iter_mut().filter(|sample| {
        sample.pattern == ScalingPattern::RandomLarge.as_str()
            && sample.thread_count == 1
            && sample.allocator_id == "tcmalloc"
    }) {
        sample.peak_rss_bytes = ordinal;
        ordinal += 1;
    }
    assert_eq!(ordinal, 41, "fixture must provide exactly 40 repetitions");
    let report = build_scaling_report(&raw).expect("modified RSS observations remain valid");
    let summary = report
        .rss
        .expect("RSS report exists")
        .cell_summaries
        .into_iter()
        .find(|summary| {
            summary.pattern == ScalingPattern::RandomLarge.as_str()
                && summary.thread_count == 1
                && summary.allocator_id == "tcmalloc"
        })
        .expect("target RSS cell exists");
    assert_eq!(summary.p05_peak_rss_bytes, 3);
    assert_eq!(summary.median_peak_rss_bytes, 21);
    assert_eq!(summary.p95_peak_rss_bytes, 38);
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
    assert!(
        validate_scaling_raw_run(&raw).is_err(),
        "a missing cell must fail"
    );

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
    assert!(
        validate_scaling_raw_run(&raw).is_err(),
        "wrong counts must fail"
    );

    let mut raw = sample_run();
    raw.run_seed ^= 0xff;
    assert!(
        validate_scaling_raw_run(&raw).is_err(),
        "changing the run seed must invalidate every derived plan"
    );

    let mut raw = sample_run();
    let sample = raw
        .samples
        .iter_mut()
        .find(|sample| sample.pattern == ScalingPattern::RandomLarge.as_str())
        .expect("fixture has random-large samples");
    sample.response.worker_seeds[0] ^= 1;
    assert!(
        validate_scaling_raw_run(&raw).is_err(),
        "changing a recorded worker seed must invalidate the distribution plan"
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
    attach_scaling_report(&mut latest, newer_fork).expect("a newer fork build must still overlay");
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
    assert_eq!(LOCK_PINNED_ALLOCATORS.len(), 4);
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
