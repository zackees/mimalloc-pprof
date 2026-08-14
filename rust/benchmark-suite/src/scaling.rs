//! Sparse thread-scaling sweep over seeded random allocation patterns.
//!
//! Unlike the fixed `core-throughput-v1` card catalogue, every pattern here is
//! a deterministic pseudo-random operation stream: the planner draws each
//! operation, size, and slot from a splitmix64 chain that depends only on
//! (run seed, pattern, thread count, block, worker). It never depends on the
//! allocator, so the same worker replays the identical stream for all four
//! allocators inside one paired block.
//!
//! This protocol is explicitly a coverage-mode downgrade of the dense scaling
//! design: three blocks per cell, median with min/max, and no bootstrap
//! intervals or noise gating. Every published surface carries that label.

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::io::{Read, Write};
use std::process::{Command, Stdio};
use std::ptr::NonNull;
use std::sync::{Arc, Barrier, Mutex};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};

use crate::execution::AllocatorAdapter;
use crate::model::{
    AllocatorBuildIdentity, AllocatorIdentity, LatestReport, PublicationRunner, RunIdentity,
    RunnerMetadata, ToolchainMetadata,
};
use crate::orchestration::ChildProgram;
use crate::provenance::sha256_bytes;
use crate::stats::MetricDirection;

fn is_lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

pub const SCALING_SCHEMA_VERSION: &str = "throughput-scaling-sparse-v1";
pub const SCALING_CHILD_PROTOCOL_VERSION: &str = "throughput-scaling-sparse-child-v1";
/// Coverage mode: three blocks is the minimum that still permits a paired
/// comparison and still exposes a single wild outlier through min/max.
pub const SCALING_BLOCKS: u32 = 3;
/// Fixed literal worker counts. These are deliberately not topology-resolved;
/// the runner records its own topology as metadata and labels oversubscription.
pub const SCALING_THREAD_POINTS: [u32; 3] = [1, 4, 16];
pub const SCALING_RIGOR_LABEL: &str = "coverage mode - reduced statistical rigor (3 blocks)";
pub const SCALING_MIN_BLOCK_NS: u64 = 400_000_000;
pub const SCALING_MAX_BLOCK_NS: u64 = 1_500_000_000;
pub const SCALING_TARGET_BLOCK_NS: u64 = 750_000_000;
const ALLOCATOR_IDS: [&str; 4] = [
    "tcmalloc",
    "jemalloc",
    "upstream-mimalloc",
    "mimalloc-pprof",
];
const SEED_DOMAIN: u64 = 0x5343_414c_494e_4721;
const GOLDEN: u64 = 0x9e37_79b9_7f4a_7c15;
const FNV_OFFSET: u64 = 0xcbf2_9ce4_8422_2325;
const FNV_PRIME: u64 = 0x0000_0100_0000_01b3;
const PAGE_BYTES: usize = 4096;
const DRAIN_BUDGET: u32 = 8;

pub fn splitmix64(mut value: u64) -> u64 {
    value = value.wrapping_add(GOLDEN);
    let mut z = value;
    z = (z ^ (z >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
    z ^ (z >> 31)
}

/// Deterministic per-stream seed. The allocator identity is intentionally
/// absent: cross-allocator stream identity inside a paired block is a
/// structural property of this function, not a runtime check.
pub fn stream_seed(
    run_seed: u64,
    pattern: ScalingPattern,
    thread_count: u32,
    block_id: u32,
    worker: u32,
) -> u64 {
    let mut state = splitmix64(run_seed ^ SEED_DOMAIN);
    for component in [
        pattern.seed_tag(),
        u64::from(thread_count),
        u64::from(block_id),
        u64::from(worker),
    ] {
        state = splitmix64(state ^ component.wrapping_mul(GOLDEN));
    }
    state | 1
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum ScalingPattern {
    TinyHot,
    MixedGeneral,
    LargeBuffers,
    CrossThread,
}

pub const SCALING_PATTERNS: [ScalingPattern; 4] = [
    ScalingPattern::TinyHot,
    ScalingPattern::MixedGeneral,
    ScalingPattern::LargeBuffers,
    ScalingPattern::CrossThread,
];

impl ScalingPattern {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::TinyHot => "sparse-tiny-hot",
            Self::MixedGeneral => "sparse-mixed-general",
            Self::LargeBuffers => "sparse-large-buffers",
            Self::CrossThread => "sparse-cross-thread",
        }
    }

    pub fn parse(value: &str) -> Option<Self> {
        SCALING_PATTERNS
            .into_iter()
            .find(|pattern| pattern.as_str() == value)
    }

    /// Stable u64 tag folded into the seed chain. These constants are part of
    /// the reproduction contract and must never be reordered or reused.
    pub const fn seed_tag(self) -> u64 {
        match self {
            Self::TinyHot => 0x0000_0001_7401_1101,
            Self::MixedGeneral => 0x0000_0002_6d69_7802,
            Self::LargeBuffers => 0x0000_0003_6c61_7203,
            Self::CrossThread => 0x0000_0004_7874_6804,
        }
    }

    pub const fn description(self) -> &'static str {
        match self {
            Self::TinyHot => "16-64 B small-object hot path, high alloc/free rate, small live set",
            Self::MixedGeneral => {
                "8 B-4 KiB log-uniform general mix including realloc, medium live set"
            }
            Self::LargeBuffers => "64 KiB-4 MiB buffers with one-byte-per-page touching",
            Self::CrossThread => {
                "16-512 B producer/consumer handoff; blocks freed by another worker"
            }
        }
    }

    pub const fn spec(self) -> PatternSpec {
        match self {
            Self::TinyHot => PatternSpec {
                min_size: 16,
                max_size: 64,
                log_uniform: false,
                capacity: 256,
                weight_alloc: 8,
                weight_free_oldest: 5,
                weight_free_random: 3,
                weight_realloc: 0,
                cross_thread: false,
                page_touch: false,
            },
            Self::MixedGeneral => PatternSpec {
                min_size: 8,
                max_size: 4096,
                log_uniform: true,
                capacity: 1024,
                weight_alloc: 7,
                weight_free_oldest: 4,
                weight_free_random: 3,
                weight_realloc: 2,
                cross_thread: false,
                page_touch: false,
            },
            Self::LargeBuffers => PatternSpec {
                min_size: 64 * 1024,
                max_size: 4 * 1024 * 1024,
                log_uniform: true,
                capacity: 8,
                weight_alloc: 8,
                weight_free_oldest: 6,
                weight_free_random: 2,
                weight_realloc: 0,
                cross_thread: false,
                page_touch: true,
            },
            Self::CrossThread => PatternSpec {
                min_size: 16,
                max_size: 512,
                log_uniform: false,
                capacity: 128,
                weight_alloc: 9,
                weight_free_oldest: 0,
                weight_free_random: 7,
                weight_realloc: 0,
                cross_thread: true,
                page_touch: false,
            },
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PatternSpec {
    pub min_size: usize,
    pub max_size: usize,
    pub log_uniform: bool,
    pub capacity: usize,
    pub weight_alloc: u32,
    pub weight_free_oldest: u32,
    pub weight_free_random: u32,
    pub weight_realloc: u32,
    pub cross_thread: bool,
    pub page_touch: bool,
}

impl PatternSpec {
    const fn total_weight(&self) -> u32 {
        self.weight_alloc + self.weight_free_oldest + self.weight_free_random + self.weight_realloc
    }
}

/// One allocator-independent action. The planner is the single source of truth
/// for the sequence: the executor performs these and the oracle counts them,
/// so the measured counts and the derived expectation cannot drift.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PlannedAction {
    Alloc {
        slot: usize,
        size: usize,
        token: u64,
    },
    FreeSlot {
        slot: usize,
    },
    ReallocSlot {
        slot: usize,
        size: usize,
        token: u64,
    },
    Handoff {
        size: usize,
        token: u64,
        target: u32,
    },
    DrainMailbox {
        budget: u32,
    },
}

/// Deterministic operation-stream state machine for exactly one worker.
///
/// The planner owns all randomness and all live-set bookkeeping. It never
/// observes allocator behavior, so driving it twice with the same seed yields
/// byte-identical action sequences.
pub struct WorkerPlanner {
    spec: PatternSpec,
    state: u64,
    remaining: u64,
    occupied: Vec<bool>,
    fifo: VecDeque<usize>,
    queued: VecDeque<PlannedAction>,
    worker: u32,
    thread_count: u32,
}

impl WorkerPlanner {
    pub fn new(
        pattern: ScalingPattern,
        seed: u64,
        operations: u64,
        worker: u32,
        thread_count: u32,
    ) -> Self {
        let spec = pattern.spec();
        Self {
            spec,
            state: seed,
            remaining: operations,
            occupied: vec![false; spec.capacity],
            fifo: VecDeque::with_capacity(spec.capacity),
            queued: VecDeque::new(),
            worker,
            thread_count,
        }
    }

    pub const fn page_touch(&self) -> bool {
        self.spec.page_touch
    }

    pub const fn cross_thread(&self) -> bool {
        self.spec.cross_thread
    }

    pub const fn capacity(&self) -> usize {
        self.spec.capacity
    }

    fn next_u64(&mut self) -> u64 {
        self.state = splitmix64(self.state);
        self.state
    }

    fn draw_size(&mut self) -> usize {
        let spec = self.spec;
        if spec.min_size >= spec.max_size {
            return spec.min_size;
        }
        let draw = self.next_u64();
        if spec.log_uniform {
            // Log-uniform: pick an octave uniformly, then a uniform offset
            // inside it, then clamp back into the declared range.
            let low_bits = usize::BITS - spec.min_size.leading_zeros();
            let high_bits = usize::BITS - spec.max_size.leading_zeros();
            let octave = low_bits + (draw % u64::from(high_bits - low_bits + 1)) as u32;
            let base = 1usize << (octave - 1);
            let offset = (self.next_u64() % base as u64) as usize;
            base.saturating_add(offset)
                .clamp(spec.min_size, spec.max_size)
        } else {
            let span = (spec.max_size - spec.min_size + 1) as u64;
            spec.min_size + (draw % span) as usize
        }
    }

    /// Advance the stream and return the next allocator-independent action.
    pub fn next_action(&mut self) -> Option<PlannedAction> {
        loop {
            if let Some(action) = self.queued.pop_front() {
                return Some(action);
            }
            if self.remaining == 0 {
                return None;
            }
            self.remaining -= 1;
            let spec = self.spec;
            let choice = (self.next_u64() % u64::from(spec.total_weight())) as u32;
            if spec.cross_thread {
                if choice < spec.weight_alloc {
                    let size = self.draw_size();
                    let token = self.next_u64() | 1;
                    let target = self.draw_peer();
                    return Some(PlannedAction::Handoff {
                        size,
                        token,
                        target,
                    });
                }
                return Some(PlannedAction::DrainMailbox {
                    budget: DRAIN_BUDGET,
                });
            }
            let mut threshold = spec.weight_alloc;
            if choice < threshold {
                let slot = (self.next_u64() % spec.capacity as u64) as usize;
                let size = self.draw_size();
                let token = self.next_u64() | 1;
                if self.occupied[slot] {
                    // Evicting a live slot is exactly one free plus one
                    // allocation; queueing both keeps each counted once.
                    self.occupied[slot] = false;
                    self.queued.push_back(PlannedAction::Alloc { slot, size, token });
                    self.occupied[slot] = true;
                    self.fifo.push_back(slot);
                    return Some(PlannedAction::FreeSlot { slot });
                }
                self.occupied[slot] = true;
                self.fifo.push_back(slot);
                return Some(PlannedAction::Alloc { slot, size, token });
            }
            threshold += spec.weight_free_oldest;
            if choice < threshold {
                if let Some(slot) = self.pop_oldest_occupied() {
                    return Some(PlannedAction::FreeSlot { slot });
                }
                continue;
            }
            threshold += spec.weight_free_random;
            if choice < threshold {
                let slot = (self.next_u64() % spec.capacity as u64) as usize;
                if self.occupied[slot] {
                    self.occupied[slot] = false;
                    return Some(PlannedAction::FreeSlot { slot });
                }
                continue;
            }
            let slot = (self.next_u64() % spec.capacity as u64) as usize;
            let size = self.draw_size();
            let token = self.next_u64() | 1;
            if self.occupied[slot] {
                return Some(PlannedAction::ReallocSlot { slot, size, token });
            }
            self.occupied[slot] = true;
            self.fifo.push_back(slot);
            return Some(PlannedAction::Alloc { slot, size, token });
        }
    }

    /// Pick a uniformly random *other* worker so every handoff crosses a
    /// thread boundary. A single-worker run has no peer and keeps its own
    /// blocks, which the report labels as the degenerate 1-thread point.
    fn draw_peer(&mut self) -> u32 {
        if self.thread_count <= 1 {
            return self.worker;
        }
        let peer = (self.next_u64() % u64::from(self.thread_count - 1)) as u32;
        if peer >= self.worker {
            peer + 1
        } else {
            peer
        }
    }

    fn pop_oldest_occupied(&mut self) -> Option<usize> {
        while let Some(slot) = self.fifo.pop_front() {
            if self.occupied[slot] {
                self.occupied[slot] = false;
                return Some(slot);
            }
        }
        None
    }

    /// Free every slot still live at the end of the measured region.
    pub fn drain_actions(&mut self) -> Vec<PlannedAction> {
        let mut actions = Vec::new();
        for slot in 0..self.occupied.len() {
            if self.occupied[slot] {
                self.occupied[slot] = false;
                actions.push(PlannedAction::FreeSlot { slot });
            }
        }
        self.fifo.clear();
        actions
    }
}

fn pattern_byte(token: u64, offset: usize) -> u8 {
    let mixed = splitmix64(token ^ (offset as u64).wrapping_mul(GOLDEN));
    (mixed >> 24) as u8
}

fn fold(checksum: u64, value: u64) -> u64 {
    (checksum ^ value).wrapping_mul(FNV_PRIME)
}

/// Byte offsets touched for one block. Large buffers touch one byte per OS
/// page so the measurement includes real page faults; small blocks touch only
/// their first and last byte.
fn touch_offsets(size: usize, page_touch: bool) -> Vec<usize> {
    let mut offsets = Vec::new();
    if page_touch {
        let mut offset = 0;
        while offset < size {
            offsets.push(offset);
            offset += PAGE_BYTES;
        }
        if *offsets.last().unwrap_or(&usize::MAX) != size - 1 {
            offsets.push(size - 1);
        }
    } else {
        offsets.push(0);
        if size > 1 {
            offsets.push(size - 1);
        }
    }
    offsets
}

/// Checksum contribution of one allocation, computed identically by the oracle
/// (from the plan alone) and by the executor (from the bytes it read back).
fn expected_touch(token: u64, size: usize, page_touch: bool) -> u64 {
    let mut value = FNV_OFFSET;
    for offset in touch_offsets(size, page_touch) {
        value = fold(value, u64::from(pattern_byte(token, offset)));
        value = fold(value, offset as u64);
    }
    fold(value, size as u64)
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct ScalingCounts {
    pub alloc_calls: u64,
    pub realloc_calls: u64,
    pub free_calls: u64,
    pub checksum: u64,
}

impl ScalingCounts {
    pub const fn operation_count(&self) -> u64 {
        self.alloc_calls + self.realloc_calls + self.free_calls
    }
}

/// Allocator-independent expectation for one worker. For the cross-thread
/// pattern `free_calls` is left at zero because the freeing worker is not
/// knowable per worker; the cell oracle balances frees against allocations
/// across the whole cell instead.
pub fn simulate_worker(
    pattern: ScalingPattern,
    seed: u64,
    operations: u64,
    worker: u32,
    thread_count: u32,
) -> ScalingCounts {
    let mut planner = WorkerPlanner::new(pattern, seed, operations, worker, thread_count);
    let page_touch = planner.page_touch();
    let mut counts = ScalingCounts::default();
    let mut checksum = 0u64;
    while let Some(action) = planner.next_action() {
        match action {
            PlannedAction::Alloc { size, token, .. } => {
                counts.alloc_calls += 1;
                checksum = checksum.wrapping_add(expected_touch(token, size, page_touch));
            }
            PlannedAction::ReallocSlot { size, token, .. } => {
                counts.realloc_calls += 1;
                checksum = checksum.wrapping_add(expected_touch(token, size, page_touch));
            }
            PlannedAction::FreeSlot { .. } => counts.free_calls += 1,
            PlannedAction::Handoff { size, token, .. } => {
                counts.alloc_calls += 1;
                checksum = checksum.wrapping_add(expected_touch(token, size, page_touch));
            }
            PlannedAction::DrainMailbox { .. } => {}
        }
    }
    counts.free_calls += planner.drain_actions().len() as u64;
    counts.checksum = checksum;
    counts
}

/// Derived expectation for one whole cell, summed over its workers. The
/// checksum is a wrapping sum so it stays order-independent: the cross-thread
/// pattern cannot promise a fixed completion interleaving, only a fixed set of
/// allocations and touches.
pub fn simulate_cell(
    pattern: ScalingPattern,
    run_seed: u64,
    thread_count: u32,
    block_id: u32,
    operations_per_worker: u64,
) -> ScalingCounts {
    let mut total = ScalingCounts::default();
    for worker in 0..thread_count {
        let seed = stream_seed(run_seed, pattern, thread_count, block_id, worker);
        let counts = simulate_worker(pattern, seed, operations_per_worker, worker, thread_count);
        total.alloc_calls += counts.alloc_calls;
        total.realloc_calls += counts.realloc_calls;
        total.free_calls += counts.free_calls;
        total.checksum = total.checksum.wrapping_add(counts.checksum);
    }
    if pattern.spec().cross_thread {
        // Every handed-off block is freed exactly once, by its consumer or by
        // the producer's full-mailbox fallback.
        total.free_calls = total.alloc_calls;
    }
    total
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ScalingChildRequest {
    pub protocol_version: String,
    pub metric_schema_version: String,
    pub run_seed: u64,
    pub pattern: String,
    pub thread_count: u32,
    pub block_id: u32,
    pub ordinal: u8,
    pub operations_per_worker: u64,
    pub warmup_operations_per_worker: u64,
    pub allocator: AllocatorIdentity,
    pub runner: RunnerMetadata,
    pub toolchain: ToolchainMetadata,
    pub reproduction_command: String,
}

impl ScalingChildRequest {
    pub fn validate(&self) -> Result<(), String> {
        if self.protocol_version != SCALING_CHILD_PROTOCOL_VERSION
            || self.metric_schema_version != SCALING_SCHEMA_VERSION
        {
            return Err("unsupported scaling child protocol or schema version".into());
        }
        if ScalingPattern::parse(&self.pattern).is_none() {
            return Err("scaling child request names an unknown pattern".into());
        }
        if !SCALING_THREAD_POINTS.contains(&self.thread_count) {
            return Err("scaling child request uses an undeclared thread count".into());
        }
        if self.operations_per_worker == 0
            || self.ordinal >= 4
            || self.run_seed == 0
            || self.reproduction_command.is_empty()
        {
            return Err("scaling child request contains invalid counts".into());
        }
        self.allocator.validate()
    }

    pub fn pattern(&self) -> Result<ScalingPattern, String> {
        ScalingPattern::parse(&self.pattern)
            .ok_or_else(|| "scaling child request names an unknown pattern".to_string())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ScalingChildResponse {
    pub protocol_version: String,
    pub metric_schema_version: String,
    pub allocator_id: String,
    pub thread_count: u32,
    pub alloc_calls: u64,
    pub realloc_calls: u64,
    pub free_calls: u64,
    pub operation_count: u64,
    pub checksum: u64,
    pub remote_free_calls: u64,
    pub producer_fallback_frees: u64,
    pub setup_ns: u64,
    pub warmup_ns: u64,
    pub elapsed_ns: u64,
    pub teardown_ns: u64,
    pub throughput_operations_per_second: f64,
}

impl ScalingChildResponse {
    pub fn validate_against(&self, request: &ScalingChildRequest) -> Result<(), String> {
        request.validate()?;
        let pattern = request.pattern()?;
        let expected = simulate_cell(
            pattern,
            request.run_seed,
            request.thread_count,
            request.block_id,
            request.operations_per_worker,
        );
        let expected_throughput =
            expected.operation_count() as f64 * 1_000_000_000.0 / self.elapsed_ns as f64;
        let tolerance = (expected_throughput.abs() * 1e-12).max(f64::EPSILON);
        if self.protocol_version != SCALING_CHILD_PROTOCOL_VERSION
            || self.metric_schema_version != SCALING_SCHEMA_VERSION
            || self.allocator_id != request.allocator.allocator_id
            || self.thread_count != request.thread_count
            || self.elapsed_ns == 0
            || self.alloc_calls != expected.alloc_calls
            || self.realloc_calls != expected.realloc_calls
            || self.free_calls != expected.free_calls
            || self.operation_count != expected.operation_count()
            || self.checksum != expected.checksum
            || (self.throughput_operations_per_second - expected_throughput).abs() > tolerance
            || !self.throughput_operations_per_second.is_finite()
            || self.throughput_operations_per_second <= 0.0
            || (request.warmup_operations_per_worker == 0 && self.warmup_ns != 0)
            || (!pattern.spec().cross_thread
                && (self.remote_free_calls != 0 || self.producer_fallback_frees != 0))
        {
            return Err("scaling child response contradicts its derived plan".into());
        }
        Ok(())
    }
}

#[derive(Clone, Copy)]
struct Parcel {
    pointer: NonNull<u8>,
    size: usize,
    token: u64,
}

// A parcel is published to its mailbox only after the producer finished
// touching it, and is read back only by the single worker that pops it.
unsafe impl Send for Parcel {}

#[derive(Default)]
struct WorkerTally {
    counts: ScalingCounts,
    remote_frees: u64,
    fallback_frees: u64,
}

/// Execute one scaling child request against the linked allocator.
pub fn execute_scaling_child_request<A: AllocatorAdapter>(
    adapter: &A,
    request: ScalingChildRequest,
) -> Result<ScalingChildResponse, String> {
    request.validate()?;
    if adapter.allocator_id() != request.allocator.allocator_id
        || adapter.allocator_version() != request.allocator.allocator_version
        || adapter.source_sha() != request.allocator.source_sha
        || adapter.library_sha256() != request.allocator.library_sha256
    {
        return Err("linked allocator identity does not match the request".into());
    }
    let pattern = request.pattern()?;
    let spec = pattern.spec();
    let threads = request.thread_count as usize;
    let setup_started = Instant::now();
    let mailboxes: Arc<Vec<Mutex<VecDeque<Parcel>>>> = Arc::new(
        (0..threads)
            .map(|_| Mutex::new(VecDeque::with_capacity(spec.capacity)))
            .collect(),
    );
    let ready = Arc::new(Barrier::new(threads + 1));
    let start = Arc::new(Barrier::new(threads + 1));
    let produced = Arc::new(Barrier::new(threads));
    let finished = Arc::new(Barrier::new(threads + 1));
    let setup_ns = nonzero_ns(setup_started);
    let mut warmup_ns = 0u64;
    let mut elapsed_ns = 0u64;
    let tallies = std::thread::scope(|scope| -> Result<Vec<WorkerTally>, String> {
        let mut handles = Vec::with_capacity(threads);
        for worker in 0..threads {
            let mailboxes = Arc::clone(&mailboxes);
            let ready = Arc::clone(&ready);
            let start = Arc::clone(&start);
            let produced = Arc::clone(&produced);
            let finished = Arc::clone(&finished);
            let request = &request;
            handles.push(scope.spawn(move || -> Result<WorkerTally, String> {
                let worker_index = worker as u32;
                let seed = stream_seed(
                    request.run_seed,
                    pattern,
                    request.thread_count,
                    request.block_id,
                    worker_index,
                );
                if request.warmup_operations_per_worker > 0 {
                    let mut warm = WorkerPlanner::new(
                        pattern,
                        splitmix64(seed ^ 0xa076_1d64_78bd_642f),
                        request.warmup_operations_per_worker,
                        worker_index,
                        request.thread_count,
                    );
                    // The warmup mailbox is private so warmup parcels can never
                    // leak into a measured drain.
                    let warm_mailboxes: Vec<Mutex<VecDeque<Parcel>>> = (0..threads)
                        .map(|_| Mutex::new(VecDeque::new()))
                        .collect();
                    run_worker_stream(adapter, &mut warm, worker_index, &warm_mailboxes)?;
                    for mailbox in &warm_mailboxes {
                        let mut queue = mailbox
                            .lock()
                            .map_err(|_| "scaling warmup mailbox lock poisoned")?;
                        while let Some(parcel) = queue.pop_front() {
                            unsafe { adapter.free(parcel.pointer) };
                        }
                    }
                }
                ready.wait();
                start.wait();
                let mut planner = WorkerPlanner::new(
                    pattern,
                    seed,
                    request.operations_per_worker,
                    worker_index,
                    request.thread_count,
                );
                let mut tally =
                    run_worker_stream(adapter, &mut planner, worker_index, &mailboxes)?;
                if spec.cross_thread {
                    // Every producer must finish before any final drain, so a
                    // parcel can never be published into a drained mailbox.
                    produced.wait();
                    drain_own_mailbox(
                        adapter,
                        &mailboxes[worker],
                        usize::MAX,
                        &mut tally,
                        planner.page_touch(),
                    )?;
                }
                finished.wait();
                Ok(tally)
            }));
        }
        let warmup_mark = Instant::now();
        ready.wait();
        warmup_ns = if request.warmup_operations_per_worker > 0 {
            nonzero_ns(warmup_mark)
        } else {
            0
        };
        let measured = Instant::now();
        start.wait();
        finished.wait();
        elapsed_ns = nonzero_ns(measured);
        let mut tallies = Vec::with_capacity(threads);
        for handle in handles {
            tallies.push(
                handle
                    .join()
                    .map_err(|_| "scaling worker panicked".to_string())??,
            );
        }
        Ok(tallies)
    })?;
    let teardown_started = Instant::now();
    let mut counts = ScalingCounts::default();
    let mut remote_free_calls = 0u64;
    let mut producer_fallback_frees = 0u64;
    for tally in &tallies {
        counts.alloc_calls += tally.counts.alloc_calls;
        counts.realloc_calls += tally.counts.realloc_calls;
        counts.free_calls += tally.counts.free_calls;
        counts.checksum = counts.checksum.wrapping_add(tally.counts.checksum);
        remote_free_calls += tally.remote_frees;
        producer_fallback_frees += tally.fallback_frees;
    }
    let operation_count = counts.operation_count();
    let teardown_ns = nonzero_ns(teardown_started);
    Ok(ScalingChildResponse {
        protocol_version: SCALING_CHILD_PROTOCOL_VERSION.into(),
        metric_schema_version: SCALING_SCHEMA_VERSION.into(),
        allocator_id: adapter.allocator_id().to_string(),
        thread_count: request.thread_count,
        alloc_calls: counts.alloc_calls,
        realloc_calls: counts.realloc_calls,
        free_calls: counts.free_calls,
        operation_count,
        checksum: counts.checksum,
        remote_free_calls,
        producer_fallback_frees,
        setup_ns,
        warmup_ns,
        elapsed_ns,
        teardown_ns,
        throughput_operations_per_second: operation_count as f64 * 1_000_000_000.0
            / elapsed_ns as f64,
    })
}

/// Live blocks for one worker, indexed by planner slot. Held outside the
/// planner so the planner can stay allocator-free.
struct SlotTable {
    slots: Vec<Option<Parcel>>,
}

fn run_worker_stream<A: AllocatorAdapter>(
    adapter: &A,
    planner: &mut WorkerPlanner,
    worker: u32,
    mailboxes: &[Mutex<VecDeque<Parcel>>],
) -> Result<WorkerTally, String> {
    let page_touch = planner.page_touch();
    let capacity = planner.capacity();
    let mut table = SlotTable {
        slots: vec![None; capacity],
    };
    let mut tally = WorkerTally::default();
    while let Some(action) = planner.next_action() {
        match action {
            PlannedAction::Alloc { slot, size, token } => {
                let pointer = adapter.alloc(size)?;
                let parcel = Parcel {
                    pointer,
                    size,
                    token,
                };
                touch(&parcel, page_touch, &mut tally)?;
                tally.counts.alloc_calls += 1;
                table.slots[slot] = Some(parcel);
            }
            PlannedAction::ReallocSlot { slot, size, token } => {
                let existing = table.slots[slot]
                    .take()
                    .ok_or("scaling plan reallocated an empty slot")?;
                let pointer = unsafe { adapter.realloc(existing.pointer, size) }?;
                let parcel = Parcel {
                    pointer,
                    size,
                    token,
                };
                touch(&parcel, page_touch, &mut tally)?;
                tally.counts.realloc_calls += 1;
                table.slots[slot] = Some(parcel);
            }
            PlannedAction::FreeSlot { slot } => {
                let parcel = table.slots[slot]
                    .take()
                    .ok_or("scaling plan freed an empty slot")?;
                unsafe { adapter.free(parcel.pointer) };
                tally.counts.free_calls += 1;
            }
            PlannedAction::Handoff {
                size,
                token,
                target,
            } => {
                let pointer = adapter.alloc(size)?;
                let parcel = Parcel {
                    pointer,
                    size,
                    token,
                };
                touch(&parcel, page_touch, &mut tally)?;
                tally.counts.alloc_calls += 1;
                let published = {
                    let mut mailbox = mailboxes[target as usize]
                        .lock()
                        .map_err(|_| "scaling mailbox lock poisoned")?;
                    if target == worker || mailbox.len() >= capacity {
                        false
                    } else {
                        mailbox.push_back(parcel);
                        true
                    }
                };
                if !published {
                    // Bounded-queue backpressure: a producer never blocks at
                    // 16x oversubscription. It frees the block itself and the
                    // fallback count is published alongside the sample.
                    verify(&parcel, page_touch)?;
                    unsafe { adapter.free(parcel.pointer) };
                    tally.counts.free_calls += 1;
                    tally.fallback_frees += 1;
                }
            }
            PlannedAction::DrainMailbox { budget } => {
                drain_own_mailbox(
                    adapter,
                    &mailboxes[worker as usize],
                    budget as usize,
                    &mut tally,
                    page_touch,
                )?;
            }
        }
    }
    // Slots still live at the end of the stream are freed here; the oracle
    // counts exactly the same set through `WorkerPlanner::drain_actions`.
    for action in planner.drain_actions() {
        if let PlannedAction::FreeSlot { slot } = action {
            let parcel = table.slots[slot]
                .take()
                .ok_or("scaling drain freed an empty slot")?;
            unsafe { adapter.free(parcel.pointer) };
            tally.counts.free_calls += 1;
        }
    }
    Ok(tally)
}

fn drain_own_mailbox<A: AllocatorAdapter>(
    adapter: &A,
    mailbox: &Mutex<VecDeque<Parcel>>,
    budget: usize,
    tally: &mut WorkerTally,
    page_touch: bool,
) -> Result<(), String> {
    for _ in 0..budget {
        let parcel = {
            let mut queue = mailbox
                .lock()
                .map_err(|_| "scaling mailbox lock poisoned")?;
            match queue.pop_front() {
                Some(value) => value,
                None => break,
            }
        };
        verify(&parcel, page_touch)?;
        unsafe { adapter.free(parcel.pointer) };
        tally.counts.free_calls += 1;
        tally.remote_frees += 1;
    }
    Ok(())
}

fn touch(parcel: &Parcel, page_touch: bool, tally: &mut WorkerTally) -> Result<(), String> {
    let mut value = FNV_OFFSET;
    for offset in touch_offsets(parcel.size, page_touch) {
        let byte = pattern_byte(parcel.token, offset);
        unsafe { parcel.pointer.as_ptr().add(offset).write_volatile(byte) };
        let observed = unsafe { parcel.pointer.as_ptr().add(offset).read_volatile() };
        if observed != byte {
            return Err("scaling touch read back a different byte than it wrote".into());
        }
        value = fold(value, u64::from(observed));
        value = fold(value, offset as u64);
    }
    value = fold(value, parcel.size as u64);
    tally.counts.checksum = tally.counts.checksum.wrapping_add(value);
    Ok(())
}

/// Re-read a handed-off block before freeing it. This catches cross-thread
/// corruption without contributing to the checksum, which must stay
/// order-independent.
fn verify(parcel: &Parcel, page_touch: bool) -> Result<(), String> {
    for offset in touch_offsets(parcel.size, page_touch) {
        let observed = unsafe { parcel.pointer.as_ptr().add(offset).read_volatile() };
        if observed != pattern_byte(parcel.token, offset) {
            return Err("scaling consumer observed a corrupted handoff block".into());
        }
    }
    Ok(())
}

fn nonzero_ns(started: Instant) -> u64 {
    started.elapsed().as_nanos().max(1) as u64
}

/// Spawn one isolated scaling child and validate its response against the
/// derived plan. Allocator runtime features are forced off exactly as the core
/// producer does.
pub fn run_scaling_child(
    child: &ChildProgram,
    request: &ScalingChildRequest,
    timeout: Duration,
) -> Result<ScalingChildResponse, String> {
    request.validate()?;
    if child.allocator != request.allocator || timeout.is_zero() {
        return Err("scaling child identity mismatch or zero timeout".into());
    }
    let encoded = serde_json::to_vec(request)
        .map_err(|error| format!("serialize scaling request: {error}"))?;
    let mut process = Command::new(&child.program);
    process
        .args(&child.arguments)
        .arg("--scaling")
        .env_clear()
        .envs(child.environment.iter().map(|(key, value)| (key, value)))
        .env("MIMALLOC_PROF", "0")
        .env("MIMALLOC_MEMORY_EVENTS", "0")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child_process = process
        .spawn()
        .map_err(|error| format!("spawn scaling child: {error}"))?;
    child_process
        .stdin
        .take()
        .ok_or_else(|| "scaling child stdin was not piped".to_string())?
        .write_all(&encoded)
        .map_err(|error| format!("write scaling request: {error}"))?;
    let mut stdout = child_process
        .stdout
        .take()
        .ok_or_else(|| "scaling child stdout was not piped".to_string())?;
    let mut stderr = child_process
        .stderr
        .take()
        .ok_or_else(|| "scaling child stderr was not piped".to_string())?;
    let stdout_reader = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        stdout
            .read_to_end(&mut bytes)
            .map_err(|error| format!("read scaling child stdout: {error}"))?;
        Ok::<_, String>(bytes)
    });
    let stderr_reader = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        stderr
            .read_to_end(&mut bytes)
            .map_err(|error| format!("read scaling child stderr: {error}"))?;
        Ok::<_, String>(bytes)
    });
    let started = Instant::now();
    let status = loop {
        if let Some(status) = child_process
            .try_wait()
            .map_err(|error| format!("poll scaling child: {error}"))?
        {
            break status;
        }
        if started.elapsed() >= timeout {
            let _ = child_process.kill();
            let _ = child_process.wait();
            let _ = stdout_reader.join();
            let error_bytes = stderr_reader
                .join()
                .map_err(|_| "scaling stderr reader panicked".to_string())??;
            return Err(format!(
                "scaling child timed out: {}",
                String::from_utf8_lossy(&error_bytes)
            ));
        }
        std::thread::sleep(Duration::from_millis(2));
    };
    let output = stdout_reader
        .join()
        .map_err(|_| "scaling stdout reader panicked".to_string())??;
    let error_bytes = stderr_reader
        .join()
        .map_err(|_| "scaling stderr reader panicked".to_string())??;
    if !status.success() || !error_bytes.is_empty() {
        return Err(format!(
            "scaling child failed: {}",
            String::from_utf8_lossy(&error_bytes)
        ));
    }
    let response: ScalingChildResponse = serde_json::from_slice(&output)
        .map_err(|error| format!("decode scaling child response: {error}"))?;
    response.validate_against(request)?;
    Ok(response)
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ScalingTopology {
    pub physical_cores: u32,
    pub logical_cores: u32,
    pub allowed_logical_cpus: u32,
    pub affinity_policy: String,
}

impl ScalingTopology {
    /// Workers per allowed logical CPU. Points above 1.0 are oversubscribed
    /// and are labeled as contention data, never as core scaling.
    pub fn oversubscription_factor(&self, thread_count: u32) -> f64 {
        f64::from(thread_count) / f64::from(self.allowed_logical_cpus.max(1))
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ScalingCalibration {
    pub pattern: String,
    pub thread_count: u32,
    pub operations_per_worker: u64,
    pub warmup_operations_per_worker: u64,
    pub elapsed_ns: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ScalingRawSample {
    pub metric_schema_version: String,
    pub block_id: u32,
    pub ordinal: u8,
    pub pattern: String,
    pub thread_count: u32,
    pub allocator_id: String,
    pub allocator_source_sha: String,
    pub child_binary_sha256: String,
    pub operations_per_worker: u64,
    pub reproduction_command: String,
    pub response: ScalingChildResponse,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ScalingRawRun {
    pub metric_schema_version: String,
    pub status: String,
    pub run_seed: u64,
    pub run: RunIdentity,
    pub runner: PublicationRunner,
    pub topology: ScalingTopology,
    pub allocator_lock_sha256: String,
    pub allocators: Vec<AllocatorBuildIdentity>,
    pub calibrations: Vec<ScalingCalibration>,
    pub samples: Vec<ScalingRawSample>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ScalingCellSummary {
    pub pattern: String,
    pub thread_count: u32,
    pub oversubscription_factor: f64,
    pub oversubscribed: bool,
    pub allocator_id: String,
    pub block_count: u32,
    pub median_throughput: f64,
    pub min_throughput: f64,
    pub max_throughput: f64,
    /// Median throughput at this point divided by the same allocator's median
    /// at one worker, for the same pattern.
    pub speedup_vs_single_worker: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ScalingPatternDefinition {
    pub pattern: String,
    pub description: String,
    pub min_size_bytes: u64,
    pub max_size_bytes: u64,
    pub live_set_capacity: u64,
    pub cross_thread: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ScalingMethodology {
    pub rigor: String,
    pub blocks_per_cell: u32,
    pub aggregation: String,
    pub operation_stream: String,
    pub seed_chain: String,
    pub pairing: String,
    pub work_normalization: String,
    pub oversubscription: String,
    pub cross_thread_backpressure: String,
    pub statistics_omitted: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ScalingMetricReport {
    pub metric_schema_version: String,
    pub status: String,
    pub invalid_reason: Option<String>,
    pub metric_comparison_key: String,
    pub run: RunIdentity,
    pub runner: PublicationRunner,
    pub topology: ScalingTopology,
    pub direction: MetricDirection,
    pub informational: bool,
    pub rigor_label: String,
    pub thread_points: Vec<u32>,
    pub patterns: Vec<ScalingPatternDefinition>,
    pub methodology: ScalingMethodology,
    pub cell_summaries: Vec<ScalingCellSummary>,
    pub raw_samples: Vec<ScalingRawSample>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ScalingHistoryReport {
    pub metric_schema_version: String,
    pub status: String,
    pub metric_comparison_key: String,
    pub run: RunIdentity,
    pub runner_fingerprint_sha256: String,
    pub direction: MetricDirection,
    pub informational: bool,
    pub rigor_label: String,
    pub thread_points: Vec<u32>,
    pub methodology: ScalingMethodology,
    pub cell_summaries: Vec<ScalingCellSummary>,
}

impl ScalingMetricReport {
    pub fn history_projection(&self) -> ScalingHistoryReport {
        ScalingHistoryReport {
            metric_schema_version: self.metric_schema_version.clone(),
            status: self.status.clone(),
            metric_comparison_key: self.metric_comparison_key.clone(),
            run: self.run.clone(),
            runner_fingerprint_sha256: self.runner.fingerprint_sha256.clone(),
            direction: self.direction,
            informational: self.informational,
            rigor_label: self.rigor_label.clone(),
            thread_points: self.thread_points.clone(),
            methodology: self.methodology.clone(),
            cell_summaries: self.cell_summaries.clone(),
        }
    }
}

pub fn pattern_definitions() -> Vec<ScalingPatternDefinition> {
    SCALING_PATTERNS
        .into_iter()
        .map(|pattern| {
            let spec = pattern.spec();
            ScalingPatternDefinition {
                pattern: pattern.as_str().into(),
                description: pattern.description().into(),
                min_size_bytes: spec.min_size as u64,
                max_size_bytes: spec.max_size as u64,
                live_set_capacity: spec.capacity as u64,
                cross_thread: spec.cross_thread,
            }
        })
        .collect()
}

pub fn methodology() -> ScalingMethodology {
    ScalingMethodology {
        rigor: SCALING_RIGOR_LABEL.into(),
        blocks_per_cell: SCALING_BLOCKS,
        aggregation: "median of per-block aggregate throughput, with min and max across the same blocks".into(),
        operation_stream: "seeded random operation stream; each operation, size, and slot is drawn from a splitmix64 stream that never observes allocator behavior".into(),
        seed_chain: "splitmix64 chain over (run seed, pattern tag, thread count, block, worker); the allocator is deliberately absent so all four allocators replay one stream".into(),
        pairing: "all four allocators run the same frozen per-worker operation count and the same stream inside one block, in a rotated near-balanced order".into(),
        work_normalization: "operations per worker are calibrated once per (pattern, thread point) against upstream-mimalloc and frozen across allocators; total work scales with worker count".into(),
        oversubscription: "thread points are literal worker counts 1/4/16; points above the allowed logical CPU count are labeled oversubscribed and describe contention, not core scaling".into(),
        cross_thread_backpressure: "bounded per-worker mailbox; a producer facing a full mailbox frees the block itself rather than blocking, and the fallback count is published".into(),
        statistics_omitted: "no bootstrap confidence intervals and no noise gating; three blocks cannot support them and the panel says so".into(),
    }
}

fn median(values: &mut [f64]) -> f64 {
    values.sort_by(|left, right| left.partial_cmp(right).expect("finite throughput"));
    let middle = values.len() / 2;
    if values.len() % 2 == 1 {
        values[middle]
    } else {
        (values[middle - 1] + values[middle]) / 2.0
    }
}

pub fn validate_scaling_raw_run(raw: &ScalingRawRun) -> Result<(), String> {
    if raw.metric_schema_version != SCALING_SCHEMA_VERSION || raw.status != "complete" {
        return Err("scaling raw run is not a complete run of this metric version".into());
    }
    if raw.run_seed == 0 {
        return Err("scaling raw run has a zero run seed".into());
    }
    if !is_lower_hex(&raw.allocator_lock_sha256, 64) {
        return Err("scaling raw run has an invalid allocator lock digest".into());
    }
    if raw.allocators.len() != ALLOCATOR_IDS.len()
        || raw
            .allocators
            .iter()
            .map(|value| value.allocator_id.as_str())
            .collect::<BTreeSet<_>>()
            != ALLOCATOR_IDS.into_iter().collect::<BTreeSet<_>>()
    {
        return Err("scaling raw run does not carry the four locked allocators".into());
    }
    if raw.topology.allowed_logical_cpus == 0
        || raw.topology.physical_cores == 0
        || raw.topology.logical_cores == 0
        || raw.topology.affinity_policy.is_empty()
    {
        return Err("scaling raw run has incomplete topology metadata".into());
    }
    let expected_cells = SCALING_PATTERNS
        .into_iter()
        .flat_map(|pattern| {
            SCALING_THREAD_POINTS
                .into_iter()
                .map(move |threads| (pattern.as_str().to_string(), threads))
        })
        .collect::<BTreeSet<_>>();
    let calibrated = raw
        .calibrations
        .iter()
        .map(|value| (value.pattern.clone(), value.thread_count))
        .collect::<BTreeSet<_>>();
    if calibrated != expected_cells || raw.calibrations.len() != expected_cells.len() {
        return Err("scaling raw run is missing a calibration for some declared cell".into());
    }
    let frozen = raw
        .calibrations
        .iter()
        .map(|value| {
            ((value.pattern.clone(), value.thread_count), value)
        })
        .collect::<BTreeMap<_, _>>();
    for calibration in &raw.calibrations {
        if calibration.operations_per_worker == 0
            || !(SCALING_MIN_BLOCK_NS..=SCALING_MAX_BLOCK_NS).contains(&calibration.elapsed_ns)
        {
            return Err(format!(
                "scaling calibration for {}/{} is outside the declared block window",
                calibration.pattern, calibration.thread_count
            ));
        }
    }
    let mut blocks: BTreeMap<(String, u32, String), BTreeSet<u32>> = BTreeMap::new();
    let mut ordinals: BTreeMap<(String, u32, u32), BTreeSet<u8>> = BTreeMap::new();
    for sample in &raw.samples {
        let pattern = ScalingPattern::parse(&sample.pattern)
            .ok_or_else(|| "scaling sample names an unknown pattern".to_string())?;
        if !SCALING_THREAD_POINTS.contains(&sample.thread_count) {
            return Err("scaling sample uses an undeclared thread count".into());
        }
        if !ALLOCATOR_IDS.contains(&sample.allocator_id.as_str())
            || sample.metric_schema_version != SCALING_SCHEMA_VERSION
            || sample.ordinal >= 4
            || sample.reproduction_command.is_empty()
            || !is_lower_hex(&sample.allocator_source_sha, 40)
            || !is_lower_hex(&sample.child_binary_sha256, 64)
        {
            return Err("scaling sample has invalid identity fields".into());
        }
        let key = (sample.pattern.clone(), sample.thread_count);
        let calibration = frozen
            .get(&key)
            .ok_or_else(|| "scaling sample has no matching calibration".to_string())?;
        if sample.operations_per_worker != calibration.operations_per_worker {
            return Err(
                "scaling sample did not use the frozen per-worker operation count".into(),
            );
        }
        // Re-derive the whole plan from the seed chain and compare every count.
        let expected = simulate_cell(
            pattern,
            raw.run_seed,
            sample.thread_count,
            sample.block_id,
            sample.operations_per_worker,
        );
        let response = &sample.response;
        if response.alloc_calls != expected.alloc_calls
            || response.realloc_calls != expected.realloc_calls
            || response.free_calls != expected.free_calls
            || response.operation_count != expected.operation_count()
            || response.checksum != expected.checksum
            || response.thread_count != sample.thread_count
            || response.allocator_id != sample.allocator_id
            || response.elapsed_ns == 0
            || !response.throughput_operations_per_second.is_finite()
            || response.throughput_operations_per_second <= 0.0
        {
            return Err(format!(
                "scaling sample for {}/{} on {} contradicts its derived plan",
                sample.pattern, sample.thread_count, sample.allocator_id
            ));
        }
        blocks
            .entry((
                sample.pattern.clone(),
                sample.thread_count,
                sample.allocator_id.clone(),
            ))
            .or_default()
            .insert(sample.block_id);
        ordinals
            .entry((sample.pattern.clone(), sample.thread_count, sample.block_id))
            .or_default()
            .insert(sample.ordinal);
    }
    for pattern in SCALING_PATTERNS {
        for threads in SCALING_THREAD_POINTS {
            for allocator in ALLOCATOR_IDS {
                let key = (pattern.as_str().to_string(), threads, allocator.to_string());
                let observed = blocks
                    .get(&key)
                    .ok_or_else(|| format!("scaling matrix is missing {key:?}"))?;
                if observed.len() != SCALING_BLOCKS as usize {
                    return Err(format!(
                        "scaling matrix cell {key:?} has {} blocks, expected {SCALING_BLOCKS}",
                        observed.len()
                    ));
                }
            }
        }
    }
    for (key, seen) in &ordinals {
        if seen.len() != ALLOCATOR_IDS.len() {
            return Err(format!(
                "scaling block {key:?} is not a complete paired block of four allocators"
            ));
        }
    }
    Ok(())
}

pub fn build_scaling_report(raw: &ScalingRawRun) -> Result<ScalingMetricReport, String> {
    validate_scaling_raw_run(raw)?;
    let mut grouped: BTreeMap<(String, u32, String), Vec<f64>> = BTreeMap::new();
    for sample in &raw.samples {
        grouped
            .entry((
                sample.pattern.clone(),
                sample.thread_count,
                sample.allocator_id.clone(),
            ))
            .or_default()
            .push(sample.response.throughput_operations_per_second);
    }
    let mut single: BTreeMap<(String, String), f64> = BTreeMap::new();
    for ((pattern, threads, allocator), values) in &grouped {
        if *threads == 1 {
            let mut values = values.clone();
            single.insert(
                (pattern.clone(), allocator.clone()),
                median(&mut values),
            );
        }
    }
    let mut cell_summaries = Vec::new();
    for ((pattern, threads, allocator), values) in grouped {
        let mut sorted = values.clone();
        let median_value = median(&mut sorted);
        let minimum = sorted.first().copied().unwrap_or_default();
        let maximum = sorted.last().copied().unwrap_or_default();
        let baseline = single
            .get(&(pattern.clone(), allocator.clone()))
            .copied()
            .ok_or_else(|| "scaling report has no single-worker baseline".to_string())?;
        let factor = raw.topology.oversubscription_factor(threads);
        cell_summaries.push(ScalingCellSummary {
            pattern,
            thread_count: threads,
            oversubscription_factor: factor,
            oversubscribed: factor > 1.0,
            allocator_id: allocator,
            block_count: values.len() as u32,
            median_throughput: median_value,
            min_throughput: minimum,
            max_throughput: maximum,
            speedup_vs_single_worker: median_value / baseline,
        });
    }
    cell_summaries.sort_by(|left, right| {
        (
            &left.pattern,
            left.thread_count,
            &left.allocator_id,
        )
            .cmp(&(&right.pattern, right.thread_count, &right.allocator_id))
    });
    Ok(ScalingMetricReport {
        metric_schema_version: SCALING_SCHEMA_VERSION.into(),
        status: "complete".into(),
        invalid_reason: None,
        metric_comparison_key: scaling_comparison_key(raw)?,
        run: raw.run.clone(),
        runner: raw.runner.clone(),
        topology: raw.topology.clone(),
        direction: MetricDirection::HigherIsBetter,
        informational: true,
        rigor_label: SCALING_RIGOR_LABEL.into(),
        thread_points: SCALING_THREAD_POINTS.to_vec(),
        patterns: pattern_definitions(),
        methodology: methodology(),
        cell_summaries,
        raw_samples: raw.samples.clone(),
    })
}

pub fn scaling_comparison_key(raw: &ScalingRawRun) -> Result<String, String> {
    #[derive(Serialize)]
    struct Key<'a> {
        schema: &'a str,
        thread_points: &'a [u32],
        blocks: u32,
        patterns: Vec<ScalingPatternDefinition>,
        runner_fingerprint: &'a str,
        affinity_policy: &'a str,
        allowed_logical_cpus: u32,
        allocator_lock_sha256: &'a str,
        allocator_sources: BTreeMap<&'a str, &'a str>,
        operations_per_worker: BTreeMap<String, u64>,
    }
    let value = Key {
        schema: SCALING_SCHEMA_VERSION,
        thread_points: &SCALING_THREAD_POINTS,
        blocks: SCALING_BLOCKS,
        patterns: pattern_definitions(),
        runner_fingerprint: &raw.runner.fingerprint_sha256,
        affinity_policy: &raw.topology.affinity_policy,
        allowed_logical_cpus: raw.topology.allowed_logical_cpus,
        allocator_lock_sha256: &raw.allocator_lock_sha256,
        allocator_sources: raw
            .allocators
            .iter()
            .map(|value| (value.allocator_id.as_str(), value.source_sha.as_str()))
            .collect(),
        operations_per_worker: raw
            .calibrations
            .iter()
            .map(|value| {
                (
                    format!("{}/{}", value.pattern, value.thread_count),
                    value.operations_per_worker,
                )
            })
            .collect(),
    };
    serde_json::to_vec(&value)
        .map(|bytes| sha256_bytes(&bytes))
        .map_err(|error| error.to_string())
}

pub fn validate_scaling_report(report: &ScalingMetricReport) -> Result<(), String> {
    if report.status != "complete"
        || report.invalid_reason.is_some()
        || report.metric_schema_version != SCALING_SCHEMA_VERSION
        || report.direction != MetricDirection::HigherIsBetter
        || !report.informational
        || report.rigor_label != SCALING_RIGOR_LABEL
        || report.thread_points != SCALING_THREAD_POINTS.to_vec()
        || report.patterns != pattern_definitions()
        || report.methodology != methodology()
        || !is_lower_hex(&report.metric_comparison_key, 64)
    {
        return Err("only a complete validated scaling report can replace pending".into());
    }
    let expected_cells = SCALING_PATTERNS.len() * SCALING_THREAD_POINTS.len() * ALLOCATOR_IDS.len();
    if report.cell_summaries.len() != expected_cells {
        return Err(format!(
            "scaling report has {} cell summaries, expected {expected_cells}",
            report.cell_summaries.len()
        ));
    }
    for summary in &report.cell_summaries {
        if summary.block_count != SCALING_BLOCKS
            || !summary.median_throughput.is_finite()
            || summary.median_throughput <= 0.0
            || summary.min_throughput > summary.median_throughput
            || summary.max_throughput < summary.median_throughput
            || !summary.speedup_vs_single_worker.is_finite()
            || summary.speedup_vs_single_worker <= 0.0
            || summary.oversubscribed != (summary.oversubscription_factor > 1.0)
        {
            return Err(format!(
                "scaling cell summary for {}/{} on {} is invalid",
                summary.pattern, summary.thread_count, summary.allocator_id
            ));
        }
    }
    if report.raw_samples.len() != expected_cells * SCALING_BLOCKS as usize {
        return Err("scaling report raw sample count does not match its matrix".into());
    }
    Ok(())
}

/// Build a complete, internally consistent raw run without spawning children.
/// Every count comes from the same oracle production uses, so the fixture can
/// exercise the validator, the report builder, and the renderer end to end.
pub fn synthetic_scaling_fixture(run_seed: u64) -> Result<ScalingRawRun, String> {
    use crate::model::{AffinityMetadata, PowerMetadata};
    let lock =
        crate::config::AllocatorLock::parse_and_validate(include_str!(
            "../allocators/allocator-lock.json"
        ))?;
    let run = RunIdentity {
        source_repository: "https://github.com/zackees/mimalloc-pprof".into(),
        source_sha: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".into(),
        source_ref: "refs/heads/main".into(),
        run_origin: "local".into(),
        run_id: "scaling-fixture".into(),
        run_attempt: 1,
        generated_at_utc: "2026-08-13T00:00:00Z".into(),
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
    runner.fingerprint_sha256 =
        crate::validate::runner_fingerprint(&runner).map_err(|error| error.to_string())?;
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
                source_tree_sha256: crate::validate::repeated_hex((b'1' + index as u8) as char, 64),
                source_patches: pin
                    .patches
                    .source
                    .iter()
                    .map(|patch| crate::model::SourcePatchIdentity {
                        file: patch.file.clone(),
                        sha256: patch.sha256.clone(),
                    })
                    .collect(),
                build_system: pin.build.system.clone(),
                build_commands: pin.build.commands.clone(),
                build_flags: pin.build.flags.clone(),
                compiler: format!("fixture-compiler-{index}"),
                linker: format!("fixture-linker-{index}"),
                static_library_sha256: crate::validate::repeated_hex((b'5' + index as u8) as char, 64),
                child_binary_sha256: crate::validate::repeated_hex(['9', 'a', 'b', 'c'][index], 64),
                options: crate::validate::expected_options(&pin.id),
            }
        })
        .collect();
    let operations_per_worker = 64u64;
    let mut calibrations = Vec::new();
    let mut samples = Vec::new();
    for pattern in SCALING_PATTERNS {
        for thread_count in SCALING_THREAD_POINTS {
            calibrations.push(ScalingCalibration {
                pattern: pattern.as_str().into(),
                thread_count,
                operations_per_worker,
                warmup_operations_per_worker: 0,
                elapsed_ns: SCALING_TARGET_BLOCK_NS,
            });
            for block_id in 0..SCALING_BLOCKS {
                let expected = simulate_cell(
                    pattern,
                    run_seed,
                    thread_count,
                    block_id,
                    operations_per_worker,
                );
                for (ordinal, allocator) in ALLOCATOR_IDS.into_iter().enumerate() {
                    let identity = allocators
                        .iter()
                        .find(|value| value.allocator_id == allocator)
                        .ok_or_else(|| format!("fixture is missing {allocator}"))?;
                    // Deterministic synthetic timing. Allocators are given
                    // distinct scale factors and oversubscribed points a
                    // contention penalty, so the fixture also exercises the
                    // renderer with separable lines and a realistic knee.
                    let allocator_scale = match allocator {
                        "mimalloc-pprof" => 96,
                        "upstream-mimalloc" => 100,
                        "tcmalloc" => 109,
                        _ => 118,
                    };
                    let contention_scale = if thread_count > 4 { 165 } else { 100 };
                    let base = SCALING_TARGET_BLOCK_NS
                        + u64::from(block_id) * 9_000_000
                        + u64::from(thread_count) * 3_000_000;
                    let elapsed_ns = base * allocator_scale / 100 * contention_scale / 100;
                    let operation_count = expected.operation_count();
                    samples.push(ScalingRawSample {
                        metric_schema_version: SCALING_SCHEMA_VERSION.into(),
                        block_id,
                        ordinal: ordinal as u8,
                        pattern: pattern.as_str().into(),
                        thread_count,
                        allocator_id: allocator.into(),
                        allocator_source_sha: identity.source_sha.clone(),
                        child_binary_sha256: identity.child_binary_sha256.clone(),
                        operations_per_worker,
                        reproduction_command: format!(
                            "benchmark-scaling-run --run-seed {run_seed} # {}/{thread_count}",
                            pattern.as_str()
                        ),
                        response: ScalingChildResponse {
                            protocol_version: SCALING_CHILD_PROTOCOL_VERSION.into(),
                            metric_schema_version: SCALING_SCHEMA_VERSION.into(),
                            allocator_id: allocator.into(),
                            thread_count,
                            alloc_calls: expected.alloc_calls,
                            realloc_calls: expected.realloc_calls,
                            free_calls: expected.free_calls,
                            operation_count,
                            checksum: expected.checksum,
                            remote_free_calls: 0,
                            producer_fallback_frees: 0,
                            setup_ns: 1,
                            warmup_ns: 0,
                            elapsed_ns,
                            teardown_ns: 1,
                            throughput_operations_per_second: operation_count as f64
                                * 1_000_000_000.0
                                / elapsed_ns as f64,
                        },
                    });
                }
            }
        }
    }
    Ok(ScalingRawRun {
        metric_schema_version: SCALING_SCHEMA_VERSION.into(),
        status: "complete".into(),
        run_seed,
        run,
        topology: ScalingTopology {
            physical_cores: runner.physical_cores,
            logical_cores: runner.logical_cores,
            allowed_logical_cpus: runner.logical_cores,
            affinity_policy: runner.affinity.policy.clone(),
        },
        runner,
        allocator_lock_sha256: sha256_bytes(
            include_str!("../allocators/allocator-lock.json").as_bytes(),
        ),
        allocators,
        calibrations,
        samples,
    })
}

pub fn attach_scaling_report(
    latest: &mut LatestReport,
    report: ScalingMetricReport,
) -> Result<(), String> {
    validate_scaling_report(&report)?;
    let expected = latest
        .allocators
        .iter()
        .map(|value| (&value.allocator_id, &value.source_sha))
        .collect::<BTreeSet<_>>();
    let observed = report
        .raw_samples
        .iter()
        .map(|value| (&value.allocator_id, &value.allocator_source_sha))
        .collect::<BTreeSet<_>>();
    if expected != observed {
        return Err("scaling allocator provenance differs from core latest".into());
    }
    latest.scaling = Some(report);
    latest
        .pending_metrics
        .retain(|value| value.metric_id != "scaling");
    Ok(())
}
