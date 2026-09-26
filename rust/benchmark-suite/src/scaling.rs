//! Sparse thread-scaling sweep over seeded random allocation patterns.
//!
//! Unlike the fixed `core-throughput-v1` card catalogue, every pattern here is
//! a deterministic pseudo-random operation stream: the planner draws each
//! operation, size, and slot from a splitmix64 chain that depends only on
//! (run seed, pattern, thread count, block, worker). It never depends on the
//! allocator, so the same worker replays the identical stream for all five
//! allocators inside one paired block.
//!
//! This protocol is explicitly a coverage-mode downgrade of the dense scaling
//! design: three blocks per cell, median with min/max, and no bootstrap
//! intervals or noise gating. Every published surface carries that label.

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::io::{Read, Seek, SeekFrom, Write};
use std::process::{Command, Stdio};
use std::ptr::NonNull;
use std::sync::atomic::{AtomicBool, Ordering};
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
use crate::scaling_diagnostic::{
    decode_live_telemetry, encode_live_telemetry, RssPhaseAccumulator, ScalingDiagnostic,
    ScalingPhase, ScalingRssPhase, DIAGNOSTIC_STATUS,
};
use crate::stats::MetricDirection;

fn is_lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

pub const SCALING_SCHEMA_VERSION: &str = "throughput-scaling-sparse-v2";
pub const SCALING_CHILD_PROTOCOL_VERSION: &str = "throughput-scaling-sparse-child-v2";
/// The RSS side-car overlaid on the same sweep. It is deliberately an optional
/// object inside the scaling report (not a mutation of the cell summaries) so
/// every already-published sparse row stays valid history: rows recorded
/// before the side-car existed simply carry no `rss` object. v2 (#534) adds
/// `floor_summaries`: the theoretical-minimum RSS the memory charts draw.
pub const SCALING_RSS_SCHEMA_VERSION: &str = "throughput-scaling-rss-v2";
/// #534: the `floor_summaries` entry of the thread-churn chart. After the
/// drain nothing is live, so its floor is the baseline alone.
pub const SCALING_RSS_FLOOR_THREAD_CHURN: &str = ScalingPattern::ThreadChurn.as_str();
/// Coverage mode: three blocks is the minimum that still permits a paired
/// comparison and still exposes a single wild outlier through min/max.
pub const SCALING_BLOCKS: u32 = 3;
/// The distribution workloads publish empirical P5--P95 bands. Forty paired
/// observations is the minimum full-run sample count; smoke remains one.
pub const DISTRIBUTION_BLOCKS: u32 = 40;
/// #508: the `thread-churn` workload. It replays `large-class-ephemeral`'s
/// stream at one worker count, joins every worker thread, and then keeps the
/// child alive and idle while it samples its own RSS at fixed offsets after
/// the drain. It is not a sweep cell: it runs only at this worker count and
/// publishes into its own side-car of the scaling report.
pub const THREAD_CHURN_SCHEMA_VERSION: &str = "thread-churn-rss-v1";
pub const THREAD_CHURN_THREADS: u32 = 8;
pub const THREAD_CHURN_BLOCKS: u32 = DISTRIBUTION_BLOCKS;
/// When the child samples its RSS, in milliseconds after the drain. 1500 is
/// the release bound #491 introduced; 3000 is the "final" RSS the release time
/// is measured against.
pub const THREAD_CHURN_POST_DRAIN_OFFSETS_MS: [u64; 6] = [100, 500, 1000, 1500, 2000, 3000];
/// perf-ab's definition (`ci/perf_ab.c` `RELEASE_TOLERANCE`): memory counts
/// as released at the first sample within this many bytes of the final RSS.
pub const THREAD_CHURN_RELEASE_TOLERANCE_BYTES: u64 = 1 << 20;
/// Fixed literal worker counts. These are deliberately not topology-resolved;
/// the runner records its own topology as metadata and labels oversubscription.
/// Dense sweep up to 2x the 4-vCPU hosted runner's logical cores; the 6/8
/// points are oversubscribed and describe contention, not core scaling. The
/// thread points are part of the metric comparison key, so changing them
/// starts a new history lineage instead of rewriting the sparse one.
pub const SCALING_THREAD_POINTS: [u32; 6] = [1, 2, 3, 4, 6, 8];

pub fn scaling_thread_points_for_shard(
    shard_index: usize,
    shard_count: usize,
) -> Result<Vec<u32>, String> {
    if shard_count == 0 {
        return Err("--shard-count must be at least 1".into());
    }
    if shard_index >= shard_count {
        return Err("--shard-index must be less than --shard-count".into());
    }
    Ok(SCALING_THREAD_POINTS
        .into_iter()
        .enumerate()
        .filter_map(|(index, threads)| (index % shard_count == shard_index).then_some(threads))
        .collect())
}
/// External RSS sampling cadence while a scaling child runs.
pub const SCALING_RSS_POLL_INTERVAL_NS: u64 = 5_000_000;
pub const SCALING_RIGOR_LABEL: &str =
    "mixed rigor - 3-block legacy coverage plus 40-repetition distribution bands";
pub const SCALING_MIN_BLOCK_NS: u64 = 400_000_000;
pub const SCALING_MAX_BLOCK_NS: u64 = 1_500_000_000;
pub const SCALING_TARGET_BLOCK_NS: u64 = 750_000_000;
const ALLOCATOR_IDS: [&str; 5] = [
    "tcmalloc",
    "jemalloc",
    "upstream-mimalloc",
    "bun-mimalloc",
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
    /// Larson & Krishnan server workload: P.-A. Larson and M. Krishnan,
    /// "Memory Allocation for Long-Running Server Applications", ISMM 1998.
    /// Sizes and live-set shape mirror mimalloc-bench's invocation
    /// (`larson 5 8 1000 5000 100 4141 N`): 8-1000 B blocks, 5000 live blocks
    /// per thread, replaced one at a time (free-and-realloc the evicted
    /// slot). Each thread's block array rotates to the next thread every
    /// round, so later frees are frequently of another thread's allocation.
    ///
    /// This is a clean-room reimplementation of the workload *shape* from
    /// the published papers and mimalloc-bench's documented parameters, not
    /// a byte-identical port of mimalloc-bench's or Hoard's (GPL-licensed)
    /// sources; no code from either was consulted or copied. `CrossThread`
    /// (`sparse-cross-thread`) remains a separate, differently-shaped
    /// workload -- this pattern is additive, not a replacement.
    Larson,
    /// xmalloc-test: the multi-threaded producer/consumer allocator stress
    /// test from C. Lever and D. Boreham, "malloc() Performance in a
    /// Multi-threaded Linux Environment", USENIX 2000, as distributed in
    /// mimalloc-bench. Dedicated producer threads allocate small blocks and
    /// hand them to dedicated consumer threads, which do all of the freeing.
    ///
    /// This is a clean-room reimplementation of the workload *shape* from
    /// the published paper and mimalloc-bench's documented parameters, not a
    /// byte-identical port of mimalloc-bench's (GPL-licensed)
    /// `xmalloc-test.c`; no code from mimalloc-bench or Hoard was consulted
    /// or copied. `CrossThread` (`sparse-cross-thread`) remains a separate,
    /// differently-shaped workload.
    XmallocTest,
    /// Exact requested sizes 2^16 through 2^22, selected uniformly. This is a
    /// requested-size workload using the normal allocation API, not an
    /// additional pointer-alignment requirement.
    PowerOfTwoLarge,
    /// Unbiased uniform integer requested sizes in [64 KiB, 4 MiB].
    RandomLarge,
    /// Unbiased uniform requested sizes in [96 KiB, 512 KiB] -- the classes a
    /// mimalloc large page serves -- on long-lived workers. The control for
    /// `LargeClassEphemeral`, which replays the identical stream (#478).
    LargeClassPersistent,
    /// `LargeClassPersistent`'s stream, but each worker runs it as
    /// `generations()` short-lived OS threads. A generation exits while its
    /// live slots are still allocated; the next generation frees them, so only
    /// thread lifetime differs from the control (#478).
    LargeClassEphemeral,
    /// `LargeClassEphemeral`'s exact stream (same seed tag, same plan), after
    /// which every worker thread is joined and the child stays alive and idle,
    /// sampling its own RSS at `THREAD_CHURN_POST_DRAIN_OFFSETS_MS` (#508).
    /// Deliberately absent from `SCALING_PATTERNS`: it runs only at
    /// `THREAD_CHURN_THREADS` workers and has its own report side-car.
    ThreadChurn,
}

/// Every pattern a child request may name: the sweep plus `thread-churn`.
const REQUESTABLE_PATTERNS: [ScalingPattern; 11] = [
    ScalingPattern::TinyHot,
    ScalingPattern::MixedGeneral,
    ScalingPattern::LargeBuffers,
    ScalingPattern::CrossThread,
    ScalingPattern::Larson,
    ScalingPattern::XmallocTest,
    ScalingPattern::PowerOfTwoLarge,
    ScalingPattern::RandomLarge,
    ScalingPattern::LargeClassPersistent,
    ScalingPattern::LargeClassEphemeral,
    ScalingPattern::ThreadChurn,
];

pub const SCALING_PATTERNS: [ScalingPattern; 10] = [
    ScalingPattern::TinyHot,
    ScalingPattern::MixedGeneral,
    ScalingPattern::LargeBuffers,
    ScalingPattern::CrossThread,
    ScalingPattern::Larson,
    ScalingPattern::XmallocTest,
    ScalingPattern::PowerOfTwoLarge,
    ScalingPattern::RandomLarge,
    ScalingPattern::LargeClassPersistent,
    ScalingPattern::LargeClassEphemeral,
];

pub const DISTRIBUTION_PATTERNS: [ScalingPattern; 4] = [
    ScalingPattern::PowerOfTwoLarge,
    ScalingPattern::RandomLarge,
    ScalingPattern::LargeClassPersistent,
    ScalingPattern::LargeClassEphemeral,
];

impl ScalingPattern {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::TinyHot => "sparse-tiny-hot",
            Self::MixedGeneral => "sparse-mixed-general",
            Self::LargeBuffers => "sparse-large-buffers",
            Self::CrossThread => "sparse-cross-thread",
            Self::Larson => "larson",
            Self::XmallocTest => "xmalloc-test",
            Self::PowerOfTwoLarge => "power-of-two-large",
            Self::RandomLarge => "random-large",
            Self::LargeClassPersistent => "large-class-persistent",
            Self::LargeClassEphemeral => "large-class-ephemeral",
            Self::ThreadChurn => "thread-churn",
        }
    }

    pub fn parse(value: &str) -> Option<Self> {
        REQUESTABLE_PATTERNS
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
            Self::Larson => 0x0000_0005_6c61_7205,
            Self::XmallocTest => 0x0000_0006_786d_6c06,
            Self::PowerOfTwoLarge => 0x0000_0007_7032_6c07,
            Self::RandomLarge => 0x0000_0008_726e_6408,
            // Deliberately one tag for both: the ephemeral workload must replay
            // the control's exact stream so thread lifetime is the only variable.
            // `ThreadChurn` shares it too: it is the ephemeral stream plus an idle tail.
            Self::LargeClassPersistent | Self::LargeClassEphemeral | Self::ThreadChurn => {
                0x0000_0009_6c63_6c09
            }
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
            Self::Larson => "Larson & Krishnan server workload: 8-1000 B random slot replacement over a 5000-block array per thread; arrays rotate between threads each round, so later frees are remote",
            Self::XmallocTest => "xmalloc-test (Lever & Boreham): dedicated producer threads allocate 8-128 B blocks and hand them to dedicated consumer threads that free them",
            Self::PowerOfTwoLarge => "normal allocations with requested sizes uniformly selected from exact powers 2^16 through 2^22; eight live slots per worker, page-touched",
            Self::RandomLarge => "normal allocations with unbiased uniform integer requested sizes from 64 KiB through 4 MiB; eight live slots per worker, page-touched",
            Self::LargeClassPersistent => "unbiased uniform requested sizes from 96 KiB through 512 KiB on long-lived workers; eight live slots per worker, page-touched",
            Self::LargeClassEphemeral => "the large-class-persistent stream, run by each worker as 8 short-lived threads that exit still owning live blocks, which the next thread frees",
            Self::ThreadChurn => "the large-class-ephemeral stream; then every worker thread is joined and the process stays alive and idle while it samples its own RSS at fixed times after the drain",
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
                mode: PatternMode::Slots,
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
                mode: PatternMode::Slots,
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
                mode: PatternMode::Slots,
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
                mode: PatternMode::Handoff,
            },
            Self::Larson => PatternSpec {
                min_size: 8,
                max_size: 1000,
                log_uniform: false,
                capacity: 5000,
                weight_alloc: 1,
                weight_free_oldest: 0,
                weight_free_random: 0,
                weight_realloc: 0,
                cross_thread: true,
                page_touch: false,
                mode: PatternMode::LarsonRotation { rounds: 8 },
            },
            Self::XmallocTest => PatternSpec {
                min_size: 8,
                max_size: 128,
                log_uniform: false,
                capacity: 256,
                weight_alloc: 1,
                weight_free_oldest: 0,
                weight_free_random: 1,
                weight_realloc: 0,
                cross_thread: true,
                page_touch: false,
                mode: PatternMode::ProducerConsumer,
            },
            Self::PowerOfTwoLarge => PatternSpec {
                min_size: 64 * 1024,
                max_size: 4 * 1024 * 1024,
                log_uniform: false,
                capacity: 8,
                weight_alloc: 8,
                weight_free_oldest: 6,
                weight_free_random: 2,
                weight_realloc: 0,
                cross_thread: false,
                page_touch: true,
                mode: PatternMode::Slots,
            },
            Self::RandomLarge => PatternSpec {
                min_size: 64 * 1024,
                max_size: 4 * 1024 * 1024,
                log_uniform: false,
                capacity: 8,
                weight_alloc: 8,
                weight_free_oldest: 6,
                weight_free_random: 2,
                weight_realloc: 0,
                cross_thread: false,
                page_touch: true,
                mode: PatternMode::Slots,
            },
            Self::LargeClassPersistent | Self::LargeClassEphemeral | Self::ThreadChurn => {
                PatternSpec {
                    min_size: 96 * 1024,
                    max_size: 512 * 1024,
                    log_uniform: false,
                    capacity: 8,
                    weight_alloc: 8,
                    weight_free_oldest: 6,
                    weight_free_random: 2,
                    weight_realloc: 0,
                    cross_thread: false,
                    page_touch: true,
                    mode: PatternMode::Slots,
                }
            }
        }
    }

    pub const fn is_distribution(self) -> bool {
        matches!(
            self,
            Self::PowerOfTwoLarge
                | Self::RandomLarge
                | Self::LargeClassPersistent
                | Self::LargeClassEphemeral
                | Self::ThreadChurn
        )
    }

    /// Whether the controller runs the separate live-telemetry replay for this
    /// pattern (live requested bytes and, since #528, phase-marked RSS). The
    /// published sweep replays the distribution workloads; a diagnostic run
    /// (#528, #422 P5) also replays `sparse-large-buffers`, so what the
    /// published mode measures is unchanged.
    pub const fn replays_live_telemetry(self, diagnostic: bool) -> bool {
        (self.is_distribution() && !self.samples_after_drain())
            || (diagnostic && matches!(self, Self::LargeBuffers))
    }

    /// Whether the child stays alive after the drain and samples its own RSS
    /// at `THREAD_CHURN_POST_DRAIN_OFFSETS_MS`.
    pub const fn samples_after_drain(self) -> bool {
        matches!(self, Self::ThreadChurn)
    }

    /// Short-lived threads each worker's stream is split across; 1 means the
    /// worker thread runs the whole stream itself.
    pub const fn generations(self) -> u32 {
        if matches!(self, Self::LargeClassEphemeral | Self::ThreadChurn) {
            8
        } else {
            1
        }
    }

    pub const fn full_blocks(self) -> u32 {
        if self.is_distribution() {
            DISTRIBUTION_BLOCKS
        } else {
            SCALING_BLOCKS
        }
    }
}

/// How `WorkerPlanner::next_action` decides the next action for a pattern.
/// Dispatch is on this, not on `PatternSpec::cross_thread`: `cross_thread`
/// stays a semantic flag consumed by the oracle and the executor's mailbox
/// setup, while `mode` picks the actual draw logic.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PatternMode {
    /// Private per-worker live set; a weighted mix of alloc/free/realloc.
    Slots,
    /// Private per-worker live set, but allocations are handed off to a
    /// randomly chosen peer's mailbox instead of kept; frees come from
    /// draining one's own mailbox.
    Handoff,
    /// Larson & Krishnan free-and-replace over `rounds` shared tables that
    /// rotate across workers every round.
    LarsonRotation { rounds: u32 },
    /// Fixed roles by worker index: even workers only produce (hand off),
    /// odd workers only drain their mailbox.
    ProducerConsumer,
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
    pub mode: PatternMode,
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
    pattern: ScalingPattern,
    spec: PatternSpec,
    state: u64,
    /// Distribution workloads keep requested-size draws independent from
    /// allocation/free lifetime draws. Existing workloads retain their v1
    /// single-stream replay unchanged.
    size_state: Option<u64>,
    remaining: u64,
    occupied: Vec<bool>,
    fifo: VecDeque<usize>,
    queued: VecDeque<PlannedAction>,
    worker: u32,
    thread_count: u32,
    /// Per-round draw budget for `LarsonRotation`, consumed independently of
    /// `remaining`. `None` means unbounded, which is every pattern except
    /// Larson and Larson itself before its first round quota is set.
    round_quota: Option<u64>,
    /// Round-robin cursor over the odd (consumer) workers, advanced once per
    /// `ProducerConsumer` producer draw. Deterministic: unlike `draw_peer`,
    /// no RNG is involved in choosing a target.
    producer_ordinal: u64,
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
            pattern,
            spec,
            state: seed,
            size_state: pattern
                .is_distribution()
                .then(|| splitmix64(seed ^ 0x7369_7a65_2d76_3101)),
            remaining: operations,
            occupied: vec![false; spec.capacity],
            fifo: VecDeque::with_capacity(spec.capacity),
            queued: VecDeque::new(),
            worker,
            thread_count,
            round_quota: None,
            producer_ordinal: 0,
        }
    }

    /// Bound the next draws to at most `draws`, independently of `remaining`;
    /// `next_action` returns `None` once the quota is spent (after flushing
    /// anything already `queued`). Used by the Larson rotation executor to
    /// hand a shared table's planner to one worker at a time for exactly one
    /// round. A planner on which this is never called behaves exactly as it
    /// did before this existed: unbounded, driven only by `remaining`.
    pub fn set_round_quota(&mut self, draws: u64) {
        self.round_quota = Some(draws);
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

    fn next_size_u64(&mut self) -> u64 {
        match self.size_state.as_mut() {
            Some(state) => {
                *state = splitmix64(*state);
                *state
            }
            None => self.next_u64(),
        }
    }

    fn uniform_below(&mut self, bound: u64) -> u64 {
        debug_assert!(bound > 0);
        let threshold = bound.wrapping_neg() % bound;
        loop {
            let value = self.next_size_u64();
            if value >= threshold {
                return value % bound;
            }
        }
    }

    fn draw_size(&mut self) -> usize {
        let spec = self.spec;
        if self.size_state.is_some() {
            // The distribution patterns share lifetime rules but deliberately
            // differ only in requested-size selection.
            if self.pattern == ScalingPattern::PowerOfTwoLarge {
                return 1usize << (16 + self.uniform_below(7) as usize);
            }
            return spec.min_size
                + self.uniform_below((spec.max_size - spec.min_size + 1) as u64) as usize;
        }
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
    ///
    /// Dispatch is on `spec.mode`, not `spec.cross_thread`. `Handoff` and
    /// `Slots` reproduce today's byte-identical draw sequence exactly -- the
    /// mode check sits where the old `cross_thread` check sat, with no extra
    /// draws inserted ahead of it, so existing seeds/streams do not change.
    /// `LarsonRotation` shares the `Slots` branch (Larson's free-and-replace
    /// is exactly `Slots`' occupied-slot eviction path with every weight but
    /// `weight_alloc` zeroed). `ProducerConsumer` is role-driven by worker
    /// index and draws no weighted `choice` at all.
    pub fn next_action(&mut self) -> Option<PlannedAction> {
        loop {
            if let Some(action) = self.queued.pop_front() {
                return Some(action);
            }
            if self.remaining == 0 {
                return None;
            }
            if self.round_quota == Some(0) {
                return None;
            }
            self.remaining -= 1;
            if let Some(quota) = self.round_quota.as_mut() {
                *quota -= 1;
            }
            let spec = self.spec;
            if spec.mode == PatternMode::ProducerConsumer {
                if self.worker.is_multiple_of(2) {
                    let size = self.draw_size();
                    let token = self.next_u64() | 1;
                    let target = self.producer_consumer_target();
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
            let choice = (self.next_u64() % u64::from(spec.total_weight())) as u32;
            if spec.mode == PatternMode::Handoff {
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
            // `Slots` and `LarsonRotation` share this branch.
            let mut threshold = spec.weight_alloc;
            if choice < threshold {
                let slot = (self.next_u64() % spec.capacity as u64) as usize;
                let size = self.draw_size();
                let token = self.next_u64() | 1;
                if self.occupied[slot] {
                    // Evicting a live slot is exactly one free plus one
                    // allocation; queueing both keeps each counted once.
                    self.occupied[slot] = false;
                    self.queued
                        .push_back(PlannedAction::Alloc { slot, size, token });
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

    /// Deterministic round-robin target for a `ProducerConsumer` producer:
    /// the odd (consumer) workers `1, 3, 5, ...` in worker-index order,
    /// cycling by this producer's own handoff ordinal. Unlike `draw_peer`, no
    /// RNG is involved in choosing the target. A single-worker run has no
    /// consumer and hands off to itself, matching `draw_peer`'s degenerate
    /// 1-thread point.
    fn producer_consumer_target(&mut self) -> u32 {
        let odd_workers = self.thread_count / 2;
        if odd_workers == 0 {
            return self.worker;
        }
        let ordinal = self.producer_ordinal;
        self.producer_ordinal = self.producer_ordinal.wrapping_add(1);
        (ordinal % u64::from(odd_workers)) as u32 * 2 + 1
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
///
/// This is an iterator rather than a `Vec` because it runs once per allocation
/// on both the measured path and the oracle path; allocating here would make
/// the harness's own cost comparable to the workload it measures.
fn touch_offsets(size: usize, page_touch: bool) -> impl Iterator<Item = usize> {
    let last = size - 1;
    let stride = if page_touch { PAGE_BYTES } else { size.max(1) };
    let mut offset = 0usize;
    let mut emitted_last = false;
    std::iter::from_fn(move || {
        if offset < size {
            let current = offset;
            offset = offset.saturating_add(stride);
            if current == last {
                emitted_last = true;
            }
            return Some(current);
        }
        if !emitted_last && size > 1 {
            emitted_last = true;
            return Some(last);
        }
        None
    })
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
    /// Optional diagnostic-only shared file containing the current requested
    /// live bytes as a little-endian u64. Production timing requests omit it;
    /// the controller uses a separate replay to correlate live payload with
    /// the externally observed RSS peak.
    #[serde(default)]
    pub live_telemetry_path: Option<String>,
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
        if !SCALING_THREAD_POINTS.contains(&self.thread_count)
            || (self.pattern()? == ScalingPattern::ThreadChurn
                && self.thread_count != THREAD_CHURN_THREADS)
        {
            return Err("scaling child request uses an undeclared thread count".into());
        }
        if self.operations_per_worker == 0
            || self.ordinal >= ALLOCATOR_IDS.len() as u8
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
    #[serde(default)]
    pub worker_seeds: Vec<u64>,
    #[serde(default)]
    pub size_histogram: Vec<ScalingSizeHistogramBucket>,
    #[serde(default)]
    pub peak_live_requested_bytes: u64,
    /// #534: the child's own resident set after setup, before any worker
    /// thread exists (`/proc/self/statm`; 0 off Linux). The memory charts'
    /// floor is this plus the live requested bytes. Defaulted: rows
    /// published before it carry none.
    #[serde(default)]
    pub baseline_rss_bytes: u64,
    pub remote_free_calls: u64,
    pub producer_fallback_frees: u64,
    pub setup_ns: u64,
    pub warmup_ns: u64,
    pub elapsed_ns: u64,
    pub teardown_ns: u64,
    pub throughput_operations_per_second: f64,
    /// `thread-churn` only (#508): nanoseconds from the drain -- every worker
    /// joined -- to each RSS sample, one per `THREAD_CHURN_POST_DRAIN_OFFSETS_MS`
    /// entry and never earlier than it. Empty, and absent from the JSON, for
    /// every other pattern, so earlier rows keep their exact shape.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub post_drain_offsets_ns: Vec<u64>,
    /// The child's own resident set (`/proc/self/statm`) at each of those
    /// offsets, in bytes.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub post_drain_rss_bytes: Vec<u64>,
}

impl ScalingChildResponse {
    /// The post-drain samples are exactly one per fixed offset, taken no
    /// earlier than it and in order, for `thread-churn`, and absent otherwise.
    pub fn validate_post_drain(&self, pattern: ScalingPattern) -> Result<(), String> {
        if !pattern.samples_after_drain() {
            if self.post_drain_offsets_ns.is_empty() && self.post_drain_rss_bytes.is_empty() {
                return Ok(());
            }
            return Err(format!(
                "{} does not sample RSS after the drain",
                pattern.as_str()
            ));
        }
        let expected = THREAD_CHURN_POST_DRAIN_OFFSETS_MS.len();
        if self.post_drain_offsets_ns.len() != expected
            || self.post_drain_rss_bytes.len() != expected
        {
            return Err(format!(
                "thread-churn must report exactly {expected} post-drain RSS samples"
            ));
        }
        let mut previous = 0u64;
        for ((observed, target_ms), rss) in self
            .post_drain_offsets_ns
            .iter()
            .zip(THREAD_CHURN_POST_DRAIN_OFFSETS_MS)
            .zip(&self.post_drain_rss_bytes)
        {
            if *observed < target_ms * NANOS_PER_MILLI || *observed <= previous || *rss == 0 {
                return Err(
                    "thread-churn post-drain samples are early, out of order, or empty".into(),
                );
            }
            previous = *observed;
        }
        Ok(())
    }
}

const NANOS_PER_MILLI: u64 = 1_000_000;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ScalingSizeHistogramBucket {
    pub lower_inclusive_bytes: u64,
    pub upper_inclusive_bytes: u64,
    pub allocation_count: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScalingPlanMetadata {
    pub worker_seeds: Vec<u64>,
    pub size_histogram: Vec<ScalingSizeHistogramBucket>,
    pub peak_live_requested_bytes: u64,
}

pub fn simulate_plan_metadata(
    pattern: ScalingPattern,
    run_seed: u64,
    thread_count: u32,
    block_id: u32,
    operations_per_worker: u64,
) -> ScalingPlanMetadata {
    let mut histogram = BTreeMap::<(u64, u64), u64>::new();
    let mut peak_live_requested_bytes = 0u64;
    let mut worker_seeds = Vec::new();
    for worker in 0..thread_count {
        let seed = stream_seed(run_seed, pattern, thread_count, block_id, worker);
        worker_seeds.push(seed);
        let mut planner =
            WorkerPlanner::new(pattern, seed, operations_per_worker, worker, thread_count);
        let mut slots = vec![0u64; planner.capacity()];
        let mut live = 0u64;
        let mut worker_peak = 0u64;
        while let Some(action) = planner.next_action() {
            match action {
                PlannedAction::Alloc { slot, size, .. } => {
                    let size = size as u64;
                    slots[slot] = size;
                    live += size;
                    worker_peak = worker_peak.max(live);
                    let lower = (size / (64 * 1024)) * (64 * 1024);
                    *histogram.entry((lower, lower + 64 * 1024 - 1)).or_default() += 1;
                }
                PlannedAction::ReallocSlot { slot, size, .. } => {
                    live -= slots[slot];
                    let size = size as u64;
                    slots[slot] = size;
                    live += size;
                    worker_peak = worker_peak.max(live);
                    let lower = (size / (64 * 1024)) * (64 * 1024);
                    *histogram.entry((lower, lower + 64 * 1024 - 1)).or_default() += 1;
                }
                PlannedAction::FreeSlot { slot } => {
                    live -= slots[slot];
                    slots[slot] = 0;
                }
                PlannedAction::Handoff { size, .. } => {
                    let size = size as u64;
                    let lower = (size / (64 * 1024)) * (64 * 1024);
                    *histogram.entry((lower, lower + 64 * 1024 - 1)).or_default() += 1;
                }
                PlannedAction::DrainMailbox { .. } => {}
            }
        }
        peak_live_requested_bytes += worker_peak;
    }
    ScalingPlanMetadata {
        worker_seeds,
        size_histogram: histogram
            .into_iter()
            .map(
                |((lower_inclusive_bytes, upper_inclusive_bytes), allocation_count)| {
                    ScalingSizeHistogramBucket {
                        lower_inclusive_bytes,
                        upper_inclusive_bytes,
                        allocation_count,
                    }
                },
            )
            .collect(),
        peak_live_requested_bytes,
    }
}

impl ScalingChildResponse {
    pub fn validate_against(&self, request: &ScalingChildRequest) -> Result<(), String> {
        let pattern = request.pattern()?;
        let expected = simulate_cell(
            pattern,
            request.run_seed,
            request.thread_count,
            request.block_id,
            request.operations_per_worker,
        );
        self.validate_against_expected(request, &expected)
    }

    /// Same contract with the plan supplied by the caller. The plan is
    /// allocator-independent, so a controller running one paired block derives
    /// it once instead of five times; replaying it per child would put the
    /// harness's own cost on the same order as the workload.
    pub fn validate_against_expected(
        &self,
        request: &ScalingChildRequest,
        expected: &ScalingCounts,
    ) -> Result<(), String> {
        request.validate()?;
        let pattern = request.pattern()?;
        let expected_throughput =
            expected.operation_count() as f64 * 1_000_000_000.0 / self.elapsed_ns as f64;
        let tolerance = (expected_throughput.abs() * 1e-12).max(f64::EPSILON);
        let metadata = simulate_plan_metadata(
            pattern,
            request.run_seed,
            request.thread_count,
            request.block_id,
            request.operations_per_worker,
        );
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
            || (pattern.is_distribution()
                && (self.worker_seeds != metadata.worker_seeds
                    || self.size_histogram != metadata.size_histogram
                    || if request.live_telemetry_path.is_some() {
                        self.peak_live_requested_bytes == 0
                            || self.peak_live_requested_bytes > metadata.peak_live_requested_bytes
                    } else {
                        self.peak_live_requested_bytes != metadata.peak_live_requested_bytes
                    }))
            || (self.throughput_operations_per_second - expected_throughput).abs() > tolerance
            || !self.throughput_operations_per_second.is_finite()
            || self.throughput_operations_per_second <= 0.0
            || (request.warmup_operations_per_worker == 0 && self.warmup_ns != 0)
            || (!pattern.spec().cross_thread
                && (self.remote_free_calls != 0 || self.producer_fallback_frees != 0))
        {
            return Err("scaling child response contradicts its derived plan".into());
        }
        self.validate_post_drain(pattern)
    }
}

#[derive(Clone, Copy)]
struct Parcel {
    pointer: NonNull<u8>,
    size: usize,
    token: u64,
    /// Worker that allocated this parcel (set for both plain allocations and
    /// `Handoff` allocations). Compared against the freeing worker to count
    /// `remote_frees` for the Larson rotation, where a shared table's parcels
    /// are frequently allocated by one worker and freed by another.
    owner: u32,
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

/// The diagnostic replay's shared file: live requested bytes and, since #528,
/// the child's current phase (`scaling_diagnostic::encode_live_telemetry`).
struct LiveTelemetry {
    current: std::sync::atomic::AtomicU64,
    peak: std::sync::atomic::AtomicU64,
    phase: std::sync::atomic::AtomicU64,
    file: Mutex<std::fs::File>,
}

impl LiveTelemetry {
    fn open(path: &str) -> Result<Arc<Self>, String> {
        let file = std::fs::OpenOptions::new()
            .write(true)
            .open(path)
            .map_err(|error| format!("open live telemetry: {error}"))?;
        Ok(Arc::new(Self {
            current: std::sync::atomic::AtomicU64::new(0),
            peak: std::sync::atomic::AtomicU64::new(0),
            phase: std::sync::atomic::AtomicU64::new(ScalingPhase::Setup.code()),
            file: Mutex::new(file),
        }))
    }

    fn add(&self, size: usize) -> Result<(), String> {
        let value = self.current.fetch_add(size as u64, Ordering::Relaxed) + size as u64;
        self.peak.fetch_max(value, Ordering::Relaxed);
        self.publish(value)
    }

    fn remove(&self, size: usize) -> Result<(), String> {
        let value = self.current.fetch_sub(size as u64, Ordering::Relaxed) - size as u64;
        self.publish(value)
    }

    fn replace(&self, old: usize, new: usize) -> Result<(), String> {
        if new >= old {
            self.add(new - old)
        } else {
            self.remove(old - new)
        }
    }

    /// Raise the phase (never lower it: with several workers a phase begins
    /// when the first worker reaches it) and publish it.
    fn mark(&self, phase: ScalingPhase) -> Result<(), String> {
        self.phase.fetch_max(phase.code(), Ordering::Relaxed);
        self.publish(self.current.load(Ordering::Relaxed))
    }

    fn publish(&self, value: u64) -> Result<(), String> {
        let mut file = self
            .file
            .lock()
            .map_err(|_| "live telemetry lock poisoned".to_string())?;
        // Read under the lock, so a later write never carries an older phase.
        let phase = ScalingPhase::from_code(self.phase.load(Ordering::Relaxed))
            .ok_or("live telemetry phase is out of range")?;
        file.seek(SeekFrom::Start(0))
            .and_then(|_| file.write_all(&encode_live_telemetry(value, phase)))
            .map_err(|error| format!("write live telemetry: {error}"))
    }
}

fn mark_phase(telemetry: Option<&Arc<LiveTelemetry>>, phase: ScalingPhase) -> Result<(), String> {
    telemetry.map_or(Ok(()), |telemetry| telemetry.mark(phase))
}

/// Execute one scaling child request against the linked allocator.
///
/// Dispatches on the pattern's `PatternMode`: `Slots`, `Handoff`, and
/// `ProducerConsumer` share the mailbox-based worker loop below --
/// `ProducerConsumer` reuses `Handoff`'s mailbox machinery unmodified because
/// its spec also sets `cross_thread: true`, it just assigns fixed roles by
/// worker index. `LarsonRotation` needs a structurally different concurrency
/// shape (tables shared and rotated across workers, not private per-worker
/// state) and gets its own executor, `execute_larson_rotation`.
pub fn execute_scaling_child_request<A: AllocatorAdapter>(
    adapter: &A,
    request: ScalingChildRequest,
) -> Result<ScalingChildResponse, String> {
    execute_scaling_child_request_with_rss_probe(adapter, request, &mut read_self_rss_bytes)
}

/// `execute_scaling_child_request` with the post-drain RSS reader supplied by
/// the caller. Production passes `read_self_rss_bytes`; a test passes a probe
/// that also checks what is true of the process at the moment it is sampled.
/// Only `thread-churn` calls it, and only after every worker has been joined.
pub fn execute_scaling_child_request_with_rss_probe<A: AllocatorAdapter>(
    adapter: &A,
    request: ScalingChildRequest,
    rss_probe: &mut dyn FnMut() -> Result<u64, String>,
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
    if let PatternMode::LarsonRotation { rounds } = spec.mode {
        return execute_larson_rotation(adapter, &request, pattern, rounds);
    }
    let threads = request.thread_count as usize;
    let telemetry = request
        .live_telemetry_path
        .as_deref()
        .map(LiveTelemetry::open)
        .transpose()?;
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
    let baseline_rss_bytes = read_baseline_rss_bytes()?;
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
            let telemetry = telemetry.clone();
            handles.push(scope.spawn(move || -> Result<WorkerTally, String> {
                let worker_index = worker as u32;
                let seed = stream_seed(
                    request.run_seed,
                    pattern,
                    request.thread_count,
                    request.block_id,
                    worker_index,
                );
                // Every barrier below is reached on both the success and the
                // failure path. `Barrier` has no poison state, so a worker that
                // returned early would strand every other worker and the main
                // thread forever; the child would then die on the parent's
                // watchdog with an empty stderr instead of reporting the real
                // error. The outcome is therefore carried, not propagated.
                let warmup = mark_phase(telemetry.as_ref(), ScalingPhase::Warmup).and_then(|()| {
                    warm_up_worker(adapter, request, pattern, seed, worker_index, threads)
                });
                ready.wait();
                start.wait();
                let mut outcome = warmup.and_then(|()| {
                    let mut planner = WorkerPlanner::new(
                        pattern,
                        seed,
                        request.operations_per_worker,
                        worker_index,
                        request.thread_count,
                    );
                    run_worker_stream(
                        adapter,
                        &mut planner,
                        worker_index,
                        &mailboxes,
                        telemetry.as_ref(),
                    )
                    .map(|tally| (tally, planner.page_touch()))
                });
                if spec.cross_thread {
                    // Every producer must finish before any final drain, so a
                    // parcel can never be published into a drained mailbox.
                    produced.wait();
                    outcome = outcome.and_then(|(mut tally, page_touch)| {
                        drain_own_mailbox(
                            adapter,
                            &mailboxes[worker],
                            usize::MAX,
                            &mut tally,
                            page_touch,
                        )
                        .map(|()| (tally, page_touch))
                    });
                }
                finished.wait();
                outcome.map(|(tally, _)| tally)
            }));
        }
        let warmup_mark = Instant::now();
        ready.wait();
        warmup_ns = if request.warmup_operations_per_worker > 0 {
            nonzero_ns(warmup_mark)
        } else {
            0
        };
        // #528: the phase marks from this thread are carried past the
        // barriers, never returned early: an early return would strand the
        // workers on `start`/`finished`.
        let measured_mark = mark_phase(telemetry.as_ref(), ScalingPhase::Measured);
        let measured = Instant::now();
        start.wait();
        finished.wait();
        elapsed_ns = nonzero_ns(measured);
        let teardown_mark = mark_phase(telemetry.as_ref(), ScalingPhase::Teardown);
        let mut tallies = Vec::with_capacity(threads);
        for handle in handles {
            tallies.push(
                handle
                    .join()
                    .map_err(|_| "scaling worker panicked".to_string())??,
            );
        }
        measured_mark.and(teardown_mark)?;
        Ok(tallies)
    })?;
    // Every worker -- and every generation thread inside it -- has been
    // joined above, so the process is now idle. Sample before anything else
    // here allocates, so the harness does not disturb what it measures.
    let (post_drain_offsets_ns, post_drain_rss_bytes) = if pattern.samples_after_drain() {
        sample_after_drain(rss_probe)?
    } else {
        (Vec::new(), Vec::new())
    };
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
    let metadata = simulate_plan_metadata(
        pattern,
        request.run_seed,
        request.thread_count,
        request.block_id,
        request.operations_per_worker,
    );
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
        worker_seeds: metadata.worker_seeds,
        size_histogram: metadata.size_histogram,
        peak_live_requested_bytes: telemetry
            .as_ref()
            .map(|value| value.peak.load(Ordering::Relaxed))
            .unwrap_or(metadata.peak_live_requested_bytes),
        baseline_rss_bytes,
        remote_free_calls,
        producer_fallback_frees,
        setup_ns,
        warmup_ns,
        elapsed_ns,
        teardown_ns,
        throughput_operations_per_second: operation_count as f64 * 1_000_000_000.0
            / elapsed_ns as f64,
        post_drain_offsets_ns,
        post_drain_rss_bytes,
    })
}

/// Sleep to each `THREAD_CHURN_POST_DRAIN_OFFSETS_MS` offset, measured from
/// one instant so the offsets do not drift, and read RSS there. The readings
/// go into fixed arrays first: nothing is allocated between the drain and the
/// last sample.
fn sample_after_drain(
    rss_probe: &mut dyn FnMut() -> Result<u64, String>,
) -> Result<(Vec<u64>, Vec<u64>), String> {
    const SAMPLES: usize = THREAD_CHURN_POST_DRAIN_OFFSETS_MS.len();
    let mut offsets = [0u64; SAMPLES];
    let mut rss = [0u64; SAMPLES];
    let drained = Instant::now();
    for (index, target_ms) in THREAD_CHURN_POST_DRAIN_OFFSETS_MS.into_iter().enumerate() {
        let target = Duration::from_millis(target_ms);
        let elapsed = drained.elapsed();
        if target > elapsed {
            std::thread::sleep(target - elapsed);
        }
        offsets[index] = drained.elapsed().as_nanos() as u64;
        rss[index] = rss_probe()?;
    }
    Ok((offsets.to_vec(), rss.to_vec()))
}

/// `_SC_PAGESIZE` on Linux (glibc and musl alike).
#[cfg(target_os = "linux")]
const LINUX_SC_PAGESIZE: std::os::raw::c_int = 30;
/// Longer than `/proc/self/statm` can be: seven decimal page counts.
const STATM_BUFFER_BYTES: usize = 256;

/// This process's resident set in bytes, from `/proc/self/statm` exactly as
/// `ci/perf_ab.c` reads it. The path and the read buffer live on the stack, so
/// sampling allocates nothing through the allocator under test.
#[cfg(target_os = "linux")]
pub fn read_self_rss_bytes() -> Result<u64, String> {
    extern "C" {
        fn sysconf(name: std::os::raw::c_int) -> std::os::raw::c_long;
    }
    let mut buffer = [0u8; STATM_BUFFER_BYTES];
    let length = std::fs::File::open("/proc/self/statm")
        .and_then(|mut file| file.read(&mut buffer))
        .map_err(|error| format!("read /proc/self/statm: {error}"))?;
    let resident_pages = std::str::from_utf8(&buffer[..length])
        .ok()
        .and_then(|text| text.split_ascii_whitespace().nth(1))
        .and_then(|value| value.parse::<u64>().ok())
        .ok_or("/proc/self/statm has no resident page count")?;
    let page_bytes = unsafe { sysconf(LINUX_SC_PAGESIZE) };
    if page_bytes <= 0 {
        return Err("sysconf(_SC_PAGESIZE) failed".into());
    }
    Ok(resident_pages * page_bytes as u64)
}

/// #534: the child's resident set after setup, before any worker thread
/// exists -- the part of every allocator's RSS the workload itself does not
/// ask for. Linux-only like every RSS reading here; 0 elsewhere.
fn read_baseline_rss_bytes() -> Result<u64, String> {
    if cfg!(target_os = "linux") {
        read_self_rss_bytes()
    } else {
        Ok(0)
    }
}

#[cfg(not(target_os = "linux"))]
pub fn read_self_rss_bytes() -> Result<u64, String> {
    let _ = STATM_BUFFER_BYTES;
    Err("post-drain RSS sampling reads /proc/self/statm and is Linux-only".into())
}

/// Live blocks for one worker, indexed by planner slot. Held outside the
/// planner so the planner can stay allocator-free.
struct SlotTable {
    slots: Vec<Option<Parcel>>,
}

/// Execute the Larson & Krishnan rotation: `threads` shared slot tables, one
/// per table index (not per executing worker -- `stream_seed`'s `worker`
/// component is the table index, exactly as the oracle's `simulate_cell`
/// assumes), rotated round-robin across workers every round so a later free
/// is frequently of another worker's allocation.
///
/// Mirrors the mailbox executor's barrier discipline: every worker reaches
/// every barrier -- `ready`, `start`, one `round_barrier` wait per round, and
/// `finished` -- on both the success and the failure path, because `Barrier`
/// has no poison state; a worker that returned early would strand every
/// other worker (and the table lock it still held) forever, and the child
/// would die on the parent's watchdog with an empty stderr instead of
/// reporting the real error. The outcome is therefore carried, not returned
/// early.
fn execute_larson_rotation<A: AllocatorAdapter>(
    adapter: &A,
    request: &ScalingChildRequest,
    pattern: ScalingPattern,
    rounds: u32,
) -> Result<ScalingChildResponse, String> {
    let rounds = rounds.max(1);
    let threads = request.thread_count as usize;
    let setup_started = Instant::now();
    let tables: Arc<Vec<Mutex<(WorkerPlanner, SlotTable)>>> = Arc::new(
        (0..threads)
            .map(|table_index| {
                let seed = stream_seed(
                    request.run_seed,
                    pattern,
                    request.thread_count,
                    request.block_id,
                    table_index as u32,
                );
                let planner = WorkerPlanner::new(
                    pattern,
                    seed,
                    request.operations_per_worker,
                    table_index as u32,
                    request.thread_count,
                );
                let capacity = planner.capacity();
                Mutex::new((
                    planner,
                    SlotTable {
                        slots: vec![None; capacity],
                    },
                ))
            })
            .collect(),
    );
    let ready = Arc::new(Barrier::new(threads + 1));
    let start = Arc::new(Barrier::new(threads + 1));
    let round_barrier = Arc::new(Barrier::new(threads));
    let finished = Arc::new(Barrier::new(threads + 1));
    // Set by the first worker that fails. Tables are shared, so a failed
    // worker can leave a table whose planner marked a slot live that holds no
    // block; a peer that kept rotating onto it would report a derived
    // "freed an empty slot" error that could mask the real one. Every worker
    // re-checks this after each round barrier and stops doing work (while
    // still reaching every barrier) once it is set.
    let aborted = Arc::new(AtomicBool::new(false));
    let setup_ns = nonzero_ns(setup_started);
    let baseline_rss_bytes = read_baseline_rss_bytes()?;
    let mut warmup_ns = 0u64;
    let mut elapsed_ns = 0u64;
    let base_quota = request.operations_per_worker / u64::from(rounds);
    let extra_rounds = request.operations_per_worker % u64::from(rounds);
    let tallies = std::thread::scope(|scope| -> Result<Vec<WorkerTally>, String> {
        let mut handles = Vec::with_capacity(threads);
        for worker in 0..threads {
            let tables = Arc::clone(&tables);
            let ready = Arc::clone(&ready);
            let start = Arc::clone(&start);
            let round_barrier = Arc::clone(&round_barrier);
            let finished = Arc::clone(&finished);
            let aborted = Arc::clone(&aborted);
            handles.push(scope.spawn(move || -> Result<WorkerTally, String> {
                let worker_index = worker as u32;
                let warmup_seed = stream_seed(
                    request.run_seed,
                    pattern,
                    request.thread_count,
                    request.block_id,
                    worker_index,
                );
                let mut outcome = warm_up_worker(
                    adapter,
                    request,
                    pattern,
                    warmup_seed,
                    worker_index,
                    threads,
                );
                if outcome.is_err() {
                    aborted.store(true, Ordering::Release);
                }
                ready.wait();
                start.wait();
                let mut tally = WorkerTally::default();
                for round in 0..rounds {
                    if outcome.is_ok() && !aborted.load(Ordering::Acquire) {
                        let table_index = (worker + round as usize) % threads;
                        let quota = base_quota + u64::from(u64::from(round) < extra_rounds);
                        outcome = larson_round(
                            adapter,
                            &tables[table_index],
                            quota,
                            worker_index,
                            &mut tally,
                        );
                        if outcome.is_err() {
                            aborted.store(true, Ordering::Release);
                        }
                    }
                    round_barrier.wait();
                }
                if outcome.is_ok() && !aborted.load(Ordering::Acquire) {
                    outcome = larson_drain(adapter, &tables[worker], worker_index, &mut tally);
                }
                finished.wait();
                outcome.map(|()| tally)
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
    for tally in &tallies {
        counts.alloc_calls += tally.counts.alloc_calls;
        counts.realloc_calls += tally.counts.realloc_calls;
        counts.free_calls += tally.counts.free_calls;
        counts.checksum = counts.checksum.wrapping_add(tally.counts.checksum);
        remote_free_calls += tally.remote_frees;
    }
    let operation_count = counts.operation_count();
    let teardown_ns = nonzero_ns(teardown_started);
    let metadata = simulate_plan_metadata(
        pattern,
        request.run_seed,
        request.thread_count,
        request.block_id,
        request.operations_per_worker,
    );
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
        worker_seeds: metadata.worker_seeds,
        size_histogram: metadata.size_histogram,
        peak_live_requested_bytes: metadata.peak_live_requested_bytes,
        baseline_rss_bytes,
        remote_free_calls,
        producer_fallback_frees: 0,
        setup_ns,
        warmup_ns,
        elapsed_ns,
        teardown_ns,
        throughput_operations_per_second: operation_count as f64 * 1_000_000_000.0
            / elapsed_ns as f64,
        post_drain_offsets_ns: Vec::new(),
        post_drain_rss_bytes: Vec::new(),
    })
}

/// One shared Larson table: the planner that owns its seeded stream plus the
/// live blocks indexed by the planner's slots.
type LarsonTable = Mutex<(WorkerPlanner, SlotTable)>;

/// Run one round of one shared Larson table on the calling worker: exactly
/// `quota` planner draws (plus any eviction already queued), each an
/// allocation into a slot or the free of that slot's previous occupant. A
/// free of a block another worker allocated counts as a remote free.
fn larson_round<A: AllocatorAdapter>(
    adapter: &A,
    table: &LarsonTable,
    quota: u64,
    worker: u32,
    tally: &mut WorkerTally,
) -> Result<(), String> {
    let mut guard = table
        .lock()
        .map_err(|_| "scaling table lock poisoned".to_string())?;
    let (planner, slots) = &mut *guard;
    let page_touch = planner.page_touch();
    planner.set_round_quota(quota);
    while let Some(action) = planner.next_action() {
        match action {
            PlannedAction::Alloc { slot, size, token } => {
                let pointer = adapter.alloc(size)?;
                let parcel = Parcel {
                    pointer,
                    size,
                    token,
                    owner: worker,
                };
                touch(&parcel, page_touch, tally)?;
                tally.counts.alloc_calls += 1;
                slots.slots[slot] = Some(parcel);
            }
            PlannedAction::FreeSlot { slot } => {
                larson_free(
                    adapter,
                    slots,
                    slot,
                    worker,
                    tally,
                    "scaling plan freed an empty slot",
                )?;
            }
            _ => return Err("larson rotation planner emitted a non-slot action".into()),
        }
    }
    Ok(())
}

/// Free every block still live in one shared Larson table after the last
/// round. The oracle counts exactly this set through `drain_actions`.
fn larson_drain<A: AllocatorAdapter>(
    adapter: &A,
    table: &LarsonTable,
    worker: u32,
    tally: &mut WorkerTally,
) -> Result<(), String> {
    let mut guard = table
        .lock()
        .map_err(|_| "scaling table lock poisoned".to_string())?;
    let (planner, slots) = &mut *guard;
    for action in planner.drain_actions() {
        if let PlannedAction::FreeSlot { slot } = action {
            larson_free(
                adapter,
                slots,
                slot,
                worker,
                tally,
                "scaling drain freed an empty slot",
            )?;
        }
    }
    Ok(())
}

fn larson_free<A: AllocatorAdapter>(
    adapter: &A,
    slots: &mut SlotTable,
    slot: usize,
    worker: u32,
    tally: &mut WorkerTally,
    empty: &str,
) -> Result<(), String> {
    let parcel = slots.slots[slot].take().ok_or(empty)?;
    if parcel.owner != worker {
        tally.remote_frees += 1;
    }
    unsafe { adapter.free(parcel.pointer) };
    tally.counts.free_calls += 1;
    Ok(())
}

/// Untimed warmup on a private mailbox set, so a warmup parcel can never be
/// drained by the measured region.
fn warm_up_worker<A: AllocatorAdapter>(
    adapter: &A,
    request: &ScalingChildRequest,
    pattern: ScalingPattern,
    seed: u64,
    worker: u32,
    threads: usize,
) -> Result<(), String> {
    if request.warmup_operations_per_worker == 0 {
        return Ok(());
    }
    let mut warm = WorkerPlanner::new(
        pattern,
        splitmix64(seed ^ 0xa076_1d64_78bd_642f),
        request.warmup_operations_per_worker,
        worker,
        request.thread_count,
    );
    let warm_mailboxes: Vec<Mutex<VecDeque<Parcel>>> =
        (0..threads).map(|_| Mutex::new(VecDeque::new())).collect();
    let outcome = run_worker_stream(adapter, &mut warm, worker, &warm_mailboxes, None);
    // Release warmup parcels even when the stream failed, so a failing warmup
    // does not also leak.
    for mailbox in &warm_mailboxes {
        if let Ok(mut queue) = mailbox.lock() {
            while let Some(parcel) = queue.pop_front() {
                unsafe { adapter.free(parcel.pointer) };
            }
        }
    }
    outcome.map(|_| ())
}

fn run_worker_stream<A: AllocatorAdapter>(
    adapter: &A,
    planner: &mut WorkerPlanner,
    worker: u32,
    mailboxes: &[Mutex<VecDeque<Parcel>>],
    telemetry: Option<&Arc<LiveTelemetry>>,
) -> Result<WorkerTally, String> {
    let mut table = SlotTable {
        slots: vec![None; planner.capacity()],
    };
    let mut tally = WorkerTally::default();
    let generations = planner.pattern.generations();
    if generations == 1 {
        run_actions(
            adapter, planner, worker, mailboxes, telemetry, &mut table, &mut tally,
        )?;
    } else {
        // Each generation is a fresh OS thread that exits still owning its live
        // slots; the next generation, or the drain below, frees them.
        let per_generation = planner.remaining.div_ceil(u64::from(generations));
        for _ in 0..generations {
            planner.set_round_quota(per_generation);
            std::thread::scope(|scope| {
                scope
                    .spawn(|| {
                        run_actions(
                            adapter, planner, worker, mailboxes, telemetry, &mut table, &mut tally,
                        )
                    })
                    .join()
            })
            .map_err(|_| "scaling generation thread panicked".to_string())??;
        }
    }
    // Slots still live at the end of the stream are freed here; the oracle
    // counts exactly the same set through `WorkerPlanner::drain_actions`.
    mark_phase(telemetry, ScalingPhase::Drain)?;
    for action in planner.drain_actions() {
        if let PlannedAction::FreeSlot { slot } = action {
            let parcel = table.slots[slot]
                .take()
                .ok_or("scaling drain freed an empty slot")?;
            unsafe { adapter.free(parcel.pointer) };
            if let Some(telemetry) = telemetry {
                telemetry.remove(parcel.size)?;
            }
            tally.counts.free_calls += 1;
        }
    }
    Ok(tally)
}

fn run_actions<A: AllocatorAdapter>(
    adapter: &A,
    planner: &mut WorkerPlanner,
    worker: u32,
    mailboxes: &[Mutex<VecDeque<Parcel>>],
    telemetry: Option<&Arc<LiveTelemetry>>,
    table: &mut SlotTable,
    tally: &mut WorkerTally,
) -> Result<(), String> {
    let page_touch = planner.page_touch();
    let capacity = planner.capacity();
    while let Some(action) = planner.next_action() {
        match action {
            PlannedAction::Alloc { slot, size, token } => {
                let pointer = adapter.alloc(size)?;
                let parcel = Parcel {
                    pointer,
                    size,
                    token,
                    owner: worker,
                };
                if let Some(telemetry) = telemetry {
                    telemetry.add(size)?;
                }
                touch(&parcel, page_touch, tally)?;
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
                    owner: worker,
                };
                if let Some(telemetry) = telemetry {
                    telemetry.replace(existing.size, size)?;
                }
                touch(&parcel, page_touch, tally)?;
                tally.counts.realloc_calls += 1;
                table.slots[slot] = Some(parcel);
            }
            PlannedAction::FreeSlot { slot } => {
                let parcel = table.slots[slot]
                    .take()
                    .ok_or("scaling plan freed an empty slot")?;
                unsafe { adapter.free(parcel.pointer) };
                if let Some(telemetry) = telemetry {
                    telemetry.remove(parcel.size)?;
                }
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
                    owner: worker,
                };
                touch(&parcel, page_touch, tally)?;
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
                    tally,
                    page_touch,
                )?;
            }
        }
    }
    Ok(())
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

/// Sample one child's RSS externally from /proc while it runs and return the
/// peak. Linux-only: production collection refuses to run anywhere else, and
/// other targets record zero so the code still compiles for the full matrix.
pub fn sample_peak_rss(pid: u32, stop: &AtomicBool) -> u64 {
    sample_rss_trace(pid, stop, None).peak_rss_bytes
}

/// What the controller's external RSS sampler saw of one child.
#[derive(Debug, Default)]
struct RssTrace {
    peak_rss_bytes: u64,
    live_requested_bytes_at_peak_rss: u64,
    /// #528: per-phase summaries, only for a replay with live telemetry.
    phases: Vec<ScalingRssPhase>,
}

fn sample_rss_trace(pid: u32, stop: &AtomicBool, live_telemetry_path: Option<&str>) -> RssTrace {
    let mut trace = RssTrace::default();
    if !cfg!(target_os = "linux") {
        return trace;
    }
    let mut phases = RssPhaseAccumulator::default();
    let started = Instant::now();
    let path = format!("/proc/{pid}/smaps_rollup");
    while !stop.load(Ordering::Relaxed) {
        match std::fs::read_to_string(&path) {
            Ok(text) => {
                if let Ok(rss) = crate::memory::parse_smaps_rollup(&text) {
                    // Only a diagnostic replay names a telemetry file; the timed
                    // run reads nothing but its own RSS.
                    let telemetry = live_telemetry_path
                        .and_then(|telemetry| std::fs::read(telemetry).ok())
                        .and_then(|bytes| decode_live_telemetry(&bytes));
                    let live = telemetry.map_or(0, |(live, _)| live);
                    if rss > trace.peak_rss_bytes {
                        trace.peak_rss_bytes = rss;
                        trace.live_requested_bytes_at_peak_rss = live;
                    }
                    if let Some((live, phase)) = telemetry {
                        phases.observe(nonzero_ns(started), rss, live, phase);
                    }
                }
            }
            Err(_) => break,
        }
        std::thread::sleep(Duration::from_nanos(SCALING_RSS_POLL_INTERVAL_NS));
    }
    trace.phases = phases.finish();
    trace
}

/// One validated scaling child run as the controller observed it.
#[derive(Debug)]
pub struct ScalingChildRun {
    pub response: ScalingChildResponse,
    pub peak_rss_bytes: u64,
    pub live_requested_bytes_at_peak_rss: u64,
    /// #528: the replay's RSS samples per phase; empty without live telemetry.
    pub rss_phases: Vec<ScalingRssPhase>,
}

/// Spawn one isolated scaling child and validate its response against the
/// derived plan. Allocator runtime features are forced off exactly as the core
/// producer does. Returns the validated response plus the externally sampled
/// peak RSS for the child.
pub fn run_scaling_child(
    child: &ChildProgram,
    request: &ScalingChildRequest,
    timeout: Duration,
) -> Result<(ScalingChildResponse, u64, u64), String> {
    let pattern = request.pattern()?;
    let expected = simulate_cell(
        pattern,
        request.run_seed,
        request.thread_count,
        request.block_id,
        request.operations_per_worker,
    );
    run_scaling_child_with_plan(child, request, timeout, &expected)
}

/// Spawn one child and validate it against a plan the caller already derived.
pub fn run_scaling_child_with_plan(
    child: &ChildProgram,
    request: &ScalingChildRequest,
    timeout: Duration,
    expected: &ScalingCounts,
) -> Result<(ScalingChildResponse, u64, u64), String> {
    let run = run_scaling_child_traced(child, request, timeout, expected)?;
    Ok((
        run.response,
        run.peak_rss_bytes,
        run.live_requested_bytes_at_peak_rss,
    ))
}

/// The command a scaling child is spawned with: an empty environment plus the
/// child's own (the diagnostic env, #528, on the mimalloc-pprof child only),
/// then the profiler switches forced off.
fn scaling_child_command(child: &ChildProgram) -> Command {
    let mut process = Command::new(&child.program);
    process
        .args(&child.arguments)
        .arg("--scaling")
        .env_clear()
        .envs(child.environment.iter().map(|(key, value)| (key, value)))
        .envs(crate::scaling_diagnostic::FORCED_CHILD_ENVIRONMENT)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    process
}

/// `run_scaling_child_with_plan`, also returning the phase-marked RSS
/// summaries of a live-telemetry replay (#528).
pub fn run_scaling_child_traced(
    child: &ChildProgram,
    request: &ScalingChildRequest,
    timeout: Duration,
    expected: &ScalingCounts,
) -> Result<ScalingChildRun, String> {
    request.validate()?;
    if child.allocator != request.allocator || timeout.is_zero() {
        return Err("scaling child identity mismatch or zero timeout".into());
    }
    let encoded = serde_json::to_vec(request)
        .map_err(|error| format!("serialize scaling request: {error}"))?;
    let mut process = scaling_child_command(child);
    let mut child_process = process
        .spawn()
        .map_err(|error| format!("spawn scaling child: {error}"))?;
    let pid = child_process.id();
    let stop = Arc::new(AtomicBool::new(false));
    let stop_for_sampler = Arc::clone(&stop);
    let telemetry_path = request.live_telemetry_path.clone();
    let sampler = std::thread::spawn(move || {
        sample_rss_trace(pid, &stop_for_sampler, telemetry_path.as_deref())
    });
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
            stop.store(true, Ordering::Relaxed);
            let _ = sampler.join();
            return Err(format!(
                "scaling child timed out: {}",
                String::from_utf8_lossy(&error_bytes)
            ));
        }
        std::thread::sleep(Duration::from_millis(2));
    };
    stop.store(true, Ordering::Relaxed);
    let trace = sampler
        .join()
        .map_err(|_| "scaling RSS sampler panicked".to_string())?;
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
    response.validate_against_expected(request, expected)?;
    Ok(ScalingChildRun {
        response,
        peak_rss_bytes: trace.peak_rss_bytes,
        live_requested_bytes_at_peak_rss: trace.live_requested_bytes_at_peak_rss,
        rss_phases: trace.phases,
    })
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
    /// Externally sampled peak RSS of the child process while the measured
    /// block ran (bytes). Observed by the runner, never reported by the child.
    ///
    /// Defaulted, not required: rows published before the RSS side-car existed
    /// carry no observation, and every validator that reads a prior
    /// `latest.json` (`--base-latest`) has to deserialize those rows. A fresh
    /// producer run always sets it, and `validate_scaling_raw_run` rejects a
    /// zero there, so the default cannot smuggle a missing observation into a
    /// new run.
    #[serde(default)]
    pub peak_rss_bytes: u64,
    #[serde(default)]
    pub diagnostic_peak_rss_bytes: u64,
    #[serde(default)]
    pub live_requested_bytes_at_diagnostic_peak_rss: u64,
    #[serde(default)]
    pub diagnostic_peak_live_requested_bytes: u64,
    /// #528: the diagnostic replay's external RSS samples, summarised per
    /// child phase. Only a diagnostic run records them, so published rows keep
    /// their exact shape.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub diagnostic_rss_phases: Vec<ScalingRssPhase>,
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
    /// `thread-churn` (#508): `THREAD_CHURN_BLOCKS` paired blocks at
    /// `THREAD_CHURN_THREADS` workers, recorded by the shard that measures that
    /// worker count. Kept out of `samples` because it is not a sweep cell.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub thread_churn_samples: Vec<ScalingRawSample>,
    /// #528: present only on a diagnostic run (status `diagnostic`), which
    /// `validate_scaling_raw_run` refuses, so it can never be published.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub diagnostic: Option<ScalingDiagnostic>,
}

pub fn merge_scaling_runs(mut shards: Vec<ScalingRawRun>) -> Result<ScalingRawRun, String> {
    if shards.is_empty() {
        return Err("at least one scaling shard is required".into());
    }
    if shards
        .iter()
        .any(|shard| (shard.status == DIAGNOSTIC_STATUS) != shard.diagnostic.is_some())
    {
        return Err("a scaling shard's diagnostic status and metadata disagree".into());
    }
    let mut merged = shards.remove(0);
    merged.status = "incomplete".into();
    for shard in shards {
        if shard.metric_schema_version != merged.metric_schema_version {
            return Err("scaling shards have different metric schema versions".into());
        }
        if shard.run_seed != merged.run_seed {
            return Err("scaling shards have different run seeds".into());
        }
        if shard.allocator_lock_sha256 != merged.allocator_lock_sha256 {
            return Err("scaling shards have different allocator lock digests".into());
        }
        if shard.allocators != merged.allocators {
            return Err("scaling shards have different allocator identities".into());
        }
        if shard.topology != merged.topology {
            return Err("scaling shards have different topology".into());
        }
        if shard.runner != merged.runner {
            return Err("scaling shards have different runner identities".into());
        }
        let left_run = merged.run.clone();
        let mut right_run = shard.run.clone();
        right_run.generated_at_utc = left_run.generated_at_utc.clone();
        if left_run != right_run {
            return Err("scaling shards have different run identities".into());
        }
        let occupied = merged
            .calibrations
            .iter()
            .map(|value| (value.pattern.as_str(), value.thread_count))
            .collect::<BTreeSet<_>>();
        if shard
            .calibrations
            .iter()
            .any(|value| occupied.contains(&(value.pattern.as_str(), value.thread_count)))
        {
            return Err("scaling shards overlap on a matrix cell".into());
        }
        if !merged.thread_churn_samples.is_empty() && !shard.thread_churn_samples.is_empty() {
            return Err("scaling shards overlap on the thread-churn workload".into());
        }
        if shard.diagnostic != merged.diagnostic {
            return Err("scaling shards disagree on their diagnostic metadata".into());
        }
        merged.calibrations.extend(shard.calibrations);
        merged.samples.extend(shard.samples);
        merged
            .thread_churn_samples
            .extend(shard.thread_churn_samples);
    }
    merged
        .thread_churn_samples
        .sort_by_key(|value| (value.block_id, value.ordinal));
    merged
        .calibrations
        .sort_by_key(|value| (value.pattern.clone(), value.thread_count));
    merged.samples.sort_by_key(|value| {
        (
            value.pattern.clone(),
            value.thread_count,
            value.block_id,
            value.ordinal,
            value.allocator_id.clone(),
        )
    });

    // #528: a diagnostic run measures a selection; it is never complete.
    if let Some(diagnostic) = &merged.diagnostic {
        diagnostic.validate()?;
        merged.status = DIAGNOSTIC_STATUS.into();
        return Ok(merged);
    }
    let expected_cells = SCALING_PATTERNS.len() * SCALING_THREAD_POINTS.len();
    if merged.calibrations.len() != expected_cells {
        return Err(format!(
            "merged scaling matrix is missing cells: got {}, expected {expected_cells}",
            merged.calibrations.len()
        ));
    }
    let complete = SCALING_PATTERNS.into_iter().all(|pattern| {
        SCALING_THREAD_POINTS.into_iter().all(|threads| {
            ALLOCATOR_IDS.into_iter().all(|allocator| {
                merged
                    .samples
                    .iter()
                    .filter(|sample| {
                        sample.pattern == pattern.as_str()
                            && sample.thread_count == threads
                            && sample.allocator_id == allocator
                    })
                    .count()
                    == pattern.full_blocks() as usize
            })
        })
    });
    let complete = complete
        && merged.thread_churn_samples.len() == THREAD_CHURN_BLOCKS as usize * ALLOCATOR_IDS.len();
    merged.status = if complete { "complete" } else { "incomplete" }.into();
    if complete {
        validate_scaling_raw_run(&merged)?;
    }
    Ok(merged)
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
    #[serde(default)]
    pub p05_throughput: f64,
    #[serde(default)]
    pub p95_throughput: f64,
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
    #[serde(default)]
    pub distribution_blocks_per_cell: u32,
    #[serde(default)]
    pub percentile_method: String,
    pub aggregation: String,
    pub operation_stream: String,
    pub seed_chain: String,
    pub pairing: String,
    pub work_normalization: String,
    pub oversubscription: String,
    pub cross_thread_backpressure: String,
    pub statistics_omitted: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ScalingRssCellSummary {
    pub pattern: String,
    pub thread_count: u32,
    pub allocator_id: String,
    pub block_count: u32,
    pub median_peak_rss_bytes: u64,
    #[serde(default)]
    pub p05_peak_rss_bytes: u64,
    #[serde(default)]
    pub p95_peak_rss_bytes: u64,
    pub min_peak_rss_bytes: u64,
    pub max_peak_rss_bytes: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ScalingRssSampling {
    pub source: String,
    pub method: String,
    pub poll_interval_ns: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ScalingRssReport {
    pub metric_schema_version: String,
    pub sampling: ScalingRssSampling,
    pub cell_summaries: Vec<ScalingRssCellSummary>,
    /// #534: one theoretical-minimum RSS per memory-chart cell. Empty (and
    /// then not serialized) on rows published before v2.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub floor_summaries: Vec<ScalingRssFloorSummary>,
}

/// #534: the least RSS any allocator could have shown in one memory-chart
/// cell: the child's baseline plus the requested bytes it held at once. It is
/// allocator-independent, so it is the smallest such sum across every sample
/// in the cell (ties to the smaller baseline): a true floor for every line.
/// `pattern` is a replayed sweep pattern, or `SCALING_RSS_FLOOR_THREAD_CHURN`
/// with no live bytes.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ScalingRssFloorSummary {
    pub pattern: String,
    pub thread_count: u32,
    pub baseline_rss_bytes: u64,
    /// The concurrent peak measured by the live-telemetry replay, never the
    /// plan's sum of per-worker peaks (an upper bound on it).
    pub peak_live_requested_bytes: u64,
    pub floor_rss_bytes: u64,
}

/// #534: the floors of every replayed sweep cell, in (pattern, workers)
/// order, then the thread-churn floor when there are churn samples.
pub fn build_rss_floors(
    samples: &[ScalingRawSample],
    thread_churn_samples: &[ScalingRawSample],
) -> Vec<ScalingRssFloorSummary> {
    let mut best: BTreeMap<(String, u32), (u64, u64)> = BTreeMap::new();
    for sample in samples {
        if !ScalingPattern::parse(&sample.pattern)
            .is_some_and(|pattern| pattern.replays_live_telemetry(false))
        {
            continue;
        }
        let candidate = (
            sample.response.baseline_rss_bytes,
            sample.diagnostic_peak_live_requested_bytes,
        );
        let rank = |(baseline, live): (u64, u64)| (baseline.saturating_add(live), baseline);
        best.entry((sample.pattern.clone(), sample.thread_count))
            .and_modify(|current| {
                if rank(candidate) < rank(*current) {
                    *current = candidate;
                }
            })
            .or_insert(candidate);
    }
    let mut floors = best
        .into_iter()
        .map(
            |((pattern, thread_count), (baseline, live))| ScalingRssFloorSummary {
                pattern,
                thread_count,
                baseline_rss_bytes: baseline,
                peak_live_requested_bytes: live,
                floor_rss_bytes: baseline.saturating_add(live),
            },
        )
        .collect::<Vec<_>>();
    if let Some(baseline) = thread_churn_samples
        .iter()
        .map(|sample| sample.response.baseline_rss_bytes)
        .min()
    {
        floors.push(ScalingRssFloorSummary {
            pattern: SCALING_RSS_FLOOR_THREAD_CHURN.into(),
            thread_count: THREAD_CHURN_THREADS,
            baseline_rss_bytes: baseline,
            peak_live_requested_bytes: 0,
            floor_rss_bytes: baseline,
        });
    }
    floors
}

/// #534: the floors must be exactly what the raw samples give, non-zero, and
/// never above an RSS any allocator was measured at -- that could only be a
/// measurement bug, so it fails the run rather than drawing a wrong floor.
fn validate_rss_floors(
    rss: &ScalingRssReport,
    samples: &[ScalingRawSample],
    thread_churn: Option<&ThreadChurnReport>,
) -> Result<(), String> {
    let churn_samples = thread_churn.map_or(&[][..], |churn| churn.raw_samples.as_slice());
    if rss.floor_summaries != build_rss_floors(samples, churn_samples) {
        return Err("scaling RSS floors differ from their raw samples".into());
    }
    let replayed = SCALING_PATTERNS
        .into_iter()
        .filter(|pattern| pattern.replays_live_telemetry(false))
        .count();
    let expected = replayed * SCALING_THREAD_POINTS.len() + usize::from(thread_churn.is_some());
    if rss.floor_summaries.len() != expected {
        return Err(format!(
            "scaling RSS side-car has {} floors, expected {expected}",
            rss.floor_summaries.len()
        ));
    }
    for floor in &rss.floor_summaries {
        let lowest_measured = if floor.pattern == SCALING_RSS_FLOOR_THREAD_CHURN {
            thread_churn
                .into_iter()
                .flat_map(|churn| &churn.cell_summaries)
                .flat_map(|cell| {
                    std::iter::once(cell.p05_peak_rss_bytes)
                        .chain(cell.p05_post_drain_rss_bytes.iter().copied())
                })
                .min()
        } else {
            rss.cell_summaries
                .iter()
                .filter(|cell| {
                    cell.pattern == floor.pattern && cell.thread_count == floor.thread_count
                })
                .map(|cell| cell.min_peak_rss_bytes)
                .min()
        };
        let live_expected = floor.pattern != SCALING_RSS_FLOOR_THREAD_CHURN;
        if floor.baseline_rss_bytes == 0
            || (floor.peak_live_requested_bytes == 0) == live_expected
            || floor.floor_rss_bytes != floor.baseline_rss_bytes + floor.peak_live_requested_bytes
            || lowest_measured.is_none_or(|lowest| floor.floor_rss_bytes > lowest)
        {
            return Err(format!(
                "scaling RSS floor for {}/{} is missing, inconsistent, or above a measured RSS",
                floor.pattern, floor.thread_count
            ));
        }
    }
    Ok(())
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ThreadChurnSampling {
    pub peak_source: String,
    pub post_drain_source: String,
    pub release_definition: String,
}

/// One allocator's `thread-churn` distribution: P5/P50/P95 of the peak and of
/// each post-drain sample (one entry per `post_drain_offsets_ms`), and of the
/// per-run release time.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ThreadChurnCellSummary {
    pub allocator_id: String,
    pub block_count: u32,
    pub median_peak_rss_bytes: u64,
    pub p05_peak_rss_bytes: u64,
    pub p95_peak_rss_bytes: u64,
    pub median_post_drain_rss_bytes: Vec<u64>,
    pub p05_post_drain_rss_bytes: Vec<u64>,
    pub p95_post_drain_rss_bytes: Vec<u64>,
    pub median_release_ms: u64,
    pub p95_release_ms: u64,
}

/// The `thread-churn` side-car (#508): RSS after the work stops. Optional in
/// the report, like `rss`, so rows published before it keep their shape.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ThreadChurnReport {
    pub metric_schema_version: String,
    pub source_pattern: String,
    pub thread_count: u32,
    pub generations: u32,
    pub block_count: u32,
    pub post_drain_offsets_ms: Vec<u64>,
    pub release_tolerance_bytes: u64,
    pub sampling: ThreadChurnSampling,
    pub cell_summaries: Vec<ThreadChurnCellSummary>,
    pub raw_samples: Vec<ScalingRawSample>,
}

pub fn thread_churn_sampling() -> ThreadChurnSampling {
    ThreadChurnSampling {
        peak_source: "external /proc/<pid>/smaps_rollup Rss polled from child spawn through exit, the same peak the sweep publishes".into(),
        post_drain_source: "the child's own /proc/self/statm resident pages x page size, read in-process after every worker thread was joined, at fixed offsets from that instant".into(),
        release_definition: "per run, the first post-drain offset whose RSS is within release_tolerance_bytes of the last sample's (perf-ab's definition); P50/P95 across runs".into(),
    }
}

/// Per-run release time: the first offset within the tolerance of the final
/// sample. The final sample always qualifies, so this is total.
pub fn thread_churn_release_ms(post_drain_rss_bytes: &[u64]) -> u64 {
    let last = post_drain_rss_bytes.last().copied().unwrap_or(0);
    post_drain_rss_bytes
        .iter()
        .zip(THREAD_CHURN_POST_DRAIN_OFFSETS_MS)
        .find(|(rss, _)| **rss <= last.saturating_add(THREAD_CHURN_RELEASE_TOLERANCE_BYTES))
        .map(|(_, offset)| offset)
        .unwrap_or(THREAD_CHURN_POST_DRAIN_OFFSETS_MS[THREAD_CHURN_POST_DRAIN_OFFSETS_MS.len() - 1])
}

fn percentiles_u64(mut values: Vec<u64>) -> (u64, u64, u64) {
    values.sort_unstable();
    (
        quantile_u64_sorted(&values, 0.05),
        quantile_u64_sorted(&values, 0.50),
        quantile_u64_sorted(&values, 0.95),
    )
}

pub fn build_thread_churn_report(samples: &[ScalingRawSample]) -> ThreadChurnReport {
    let mut cell_summaries = Vec::new();
    for allocator in ALLOCATOR_IDS {
        let runs = samples
            .iter()
            .filter(|sample| sample.allocator_id == allocator)
            .collect::<Vec<_>>();
        let (p05_peak, median_peak, p95_peak) =
            percentiles_u64(runs.iter().map(|sample| sample.peak_rss_bytes).collect());
        let mut p05_post = Vec::new();
        let mut median_post = Vec::new();
        let mut p95_post = Vec::new();
        for index in 0..THREAD_CHURN_POST_DRAIN_OFFSETS_MS.len() {
            let (p05, median, p95) = percentiles_u64(
                runs.iter()
                    .map(|sample| sample.response.post_drain_rss_bytes[index])
                    .collect(),
            );
            p05_post.push(p05);
            median_post.push(median);
            p95_post.push(p95);
        }
        let (_, median_release, p95_release) = percentiles_u64(
            runs.iter()
                .map(|sample| thread_churn_release_ms(&sample.response.post_drain_rss_bytes))
                .collect(),
        );
        cell_summaries.push(ThreadChurnCellSummary {
            allocator_id: allocator.into(),
            block_count: runs.len() as u32,
            median_peak_rss_bytes: median_peak,
            p05_peak_rss_bytes: p05_peak,
            p95_peak_rss_bytes: p95_peak,
            median_post_drain_rss_bytes: median_post,
            p05_post_drain_rss_bytes: p05_post,
            p95_post_drain_rss_bytes: p95_post,
            median_release_ms: median_release,
            p95_release_ms: p95_release,
        });
    }
    ThreadChurnReport {
        metric_schema_version: THREAD_CHURN_SCHEMA_VERSION.into(),
        source_pattern: ScalingPattern::LargeClassEphemeral.as_str().into(),
        thread_count: THREAD_CHURN_THREADS,
        generations: ScalingPattern::ThreadChurn.generations(),
        block_count: THREAD_CHURN_BLOCKS,
        post_drain_offsets_ms: THREAD_CHURN_POST_DRAIN_OFFSETS_MS.to_vec(),
        release_tolerance_bytes: THREAD_CHURN_RELEASE_TOLERANCE_BYTES,
        sampling: thread_churn_sampling(),
        cell_summaries,
        raw_samples: samples.to_vec(),
    }
}

pub fn validate_thread_churn_report(report: &ThreadChurnReport) -> Result<(), String> {
    if report.metric_schema_version != THREAD_CHURN_SCHEMA_VERSION
        || report.source_pattern != ScalingPattern::LargeClassEphemeral.as_str()
        || report.thread_count != THREAD_CHURN_THREADS
        || report.generations != ScalingPattern::ThreadChurn.generations()
        || report.block_count != THREAD_CHURN_BLOCKS
        || report.post_drain_offsets_ms != THREAD_CHURN_POST_DRAIN_OFFSETS_MS.to_vec()
        || report.release_tolerance_bytes != THREAD_CHURN_RELEASE_TOLERANCE_BYTES
        || report.sampling != thread_churn_sampling()
        || report.raw_samples.len() != THREAD_CHURN_BLOCKS as usize * ALLOCATOR_IDS.len()
    {
        return Err(
            "thread-churn side-car has an invalid schema, protocol, or sample count".into(),
        );
    }
    for sample in &report.raw_samples {
        validate_sample_identity(sample)?;
        sample
            .response
            .validate_post_drain(ScalingPattern::ThreadChurn)?;
    }
    let rebuilt = build_thread_churn_report(&report.raw_samples);
    if rebuilt.cell_summaries != report.cell_summaries {
        return Err("thread-churn summaries differ from their raw samples".into());
    }
    for summary in &report.cell_summaries {
        let ordered = |low: u64, middle: u64, high: u64| low <= middle && middle <= high;
        if summary.block_count != THREAD_CHURN_BLOCKS
            || summary.median_peak_rss_bytes == 0
            || !ordered(
                summary.p05_peak_rss_bytes,
                summary.median_peak_rss_bytes,
                summary.p95_peak_rss_bytes,
            )
            || summary.median_release_ms > summary.p95_release_ms
            || (0..THREAD_CHURN_POST_DRAIN_OFFSETS_MS.len()).any(|index| {
                !ordered(
                    summary.p05_post_drain_rss_bytes[index],
                    summary.median_post_drain_rss_bytes[index],
                    summary.p95_post_drain_rss_bytes[index],
                )
            })
        {
            return Err(format!(
                "thread-churn summary for {} is invalid",
                summary.allocator_id
            ));
        }
    }
    Ok(())
}

pub fn rss_sampling() -> ScalingRssSampling {
    ScalingRssSampling {
        source: "external /proc/<pid>/smaps_rollup Rss, parsed as integer kB * 1024".into(),
        method: "polled externally from child spawn through process exit, so startup, setup, warmup, measured work, and teardown are all in scope; the process-lifetime peak is retained and phase durations are recorded separately".into(),
        poll_interval_ns: SCALING_RSS_POLL_INTERVAL_NS,
    }
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
    /// Optional RSS side-car. Absent in rows published before it existed;
    /// its own schema version keeps the extension compatible with them.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub rss: Option<ScalingRssReport>,
    pub raw_samples: Vec<ScalingRawSample>,
    /// `thread-churn` side-car (#508). Absent in rows published before it;
    /// deliberately not carried into history rows.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub thread_churn: Option<ThreadChurnReport>,
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub rss: Option<ScalingRssReport>,
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
            rss: self.rss.clone(),
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
        distribution_blocks_per_cell: DISTRIBUTION_BLOCKS,
        percentile_method: "empirical P5/P50/P95 over paired per-run observations; linear interpolation h=(n-1)p; bands are not confidence intervals".into(),
        aggregation: "median of per-block aggregate throughput, with P5/P95 and retained min/max across the same blocks".into(),
        operation_stream: "versioned splitmix64-v1 deterministic operation streams; the two distribution workloads use separate lifetime and requested-size streams, normal allocation alignment, eight live slots per worker, and page touching; existing workloads retain their prior stream; live bytes at RSS peak and actual peak live requested bytes come from a separate instrumented replay so telemetry does not contaminate timed throughput or published peak RSS".into(),
        seed_chain: "splitmix64-v1 chain over (master seed, workload tag, worker-count cell, repetition/block, worker index); allocator identity, time and scheduling are absent; distribution size streams derive from the worker seed with the size-v1 domain".into(),
        pairing: "all five allocators run the same frozen per-worker operation count and the same stream inside one block, in a rotated near-balanced order".into(),
        work_normalization: "operations per worker are calibrated once per (pattern, thread point) against upstream-mimalloc and frozen across allocators; total work scales with worker count".into(),
        oversubscription: format!(
            "thread points are literal worker counts {}; points above the allowed logical CPU count are labeled oversubscribed and describe contention, not core scaling",
            SCALING_THREAD_POINTS
                .iter()
                .map(u32::to_string)
                .collect::<Vec<_>>()
                .join("/")
        ),
        cross_thread_backpressure: "bounded per-worker mailbox; a producer facing a full mailbox frees the block itself rather than blocking, and the fallback count is published".into(),
        statistics_omitted: "no bootstrap confidence intervals and no noise gating; legacy workloads remain three-block coverage signals, while distribution workloads use at least 40 repetitions for empirical percentile bands".into(),
    }
}

fn median(values: &mut [f64]) -> f64 {
    // total_cmp rather than partial_cmp: the validator already rejects
    // non-finite throughput, so this never sees a NaN, but a total order means
    // a future caller cannot turn that into a panic.
    values.sort_by(|left, right| left.total_cmp(right));
    let middle = values.len() / 2;
    if values.len() % 2 == 1 {
        values[middle]
    } else {
        (values[middle - 1] + values[middle]) / 2.0
    }
}

fn quantile_sorted(values: &[f64], probability: f64) -> f64 {
    let h = (values.len() - 1) as f64 * probability;
    let lower = h.floor() as usize;
    let upper = h.ceil() as usize;
    values[lower] + (values[upper] - values[lower]) * (h - lower as f64)
}

fn quantile_u64_sorted(values: &[u64], probability: f64) -> u64 {
    let floats = values.iter().map(|value| *value as f64).collect::<Vec<_>>();
    quantile_sorted(&floats, probability).round() as u64
}

pub fn validate_scaling_raw_run(raw: &ScalingRawRun) -> Result<(), String> {
    // #528: a diagnostic run changed the fork's build or environment; nothing
    // may build a report from it.
    if raw.diagnostic.is_some()
        || raw.status == DIAGNOSTIC_STATUS
        || raw
            .samples
            .iter()
            .chain(&raw.thread_churn_samples)
            .any(|sample| !sample.diagnostic_rss_phases.is_empty())
    {
        return Err("a diagnostic scaling run can never be validated for publication".into());
    }
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
        return Err("scaling raw run does not carry the five locked allocators".into());
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
        .map(|value| ((value.pattern.clone(), value.thread_count), value))
        .collect::<BTreeMap<_, _>>();
    for calibration in &raw.calibrations {
        let pattern = ScalingPattern::parse(&calibration.pattern)
            .ok_or_else(|| "scaling calibration names an unknown pattern".to_string())?;
        let valid_window = if pattern.is_distribution() {
            (25_000_000..=150_000_000).contains(&calibration.elapsed_ns)
        } else {
            (SCALING_MIN_BLOCK_NS..=SCALING_MAX_BLOCK_NS).contains(&calibration.elapsed_ns)
        };
        if calibration.operations_per_worker == 0 || !valid_window {
            return Err(format!(
                "scaling calibration for {}/{} is outside the declared block window",
                calibration.pattern, calibration.thread_count
            ));
        }
    }
    let mut blocks: BTreeMap<(String, u32, String), BTreeSet<u32>> = BTreeMap::new();
    let mut ordinals: BTreeMap<(String, u32, u32), BTreeSet<u8>> = BTreeMap::new();
    let mut plans: BTreeMap<(String, u32, u32, u64), ScalingCounts> = BTreeMap::new();
    for sample in &raw.samples {
        let pattern = ScalingPattern::parse(&sample.pattern)
            .ok_or_else(|| "scaling sample names an unknown pattern".to_string())?;
        if !SCALING_PATTERNS.contains(&pattern) {
            return Err(format!(
                "scaling sample names {}, which is not a sweep pattern",
                sample.pattern
            ));
        }
        validate_sample_identity(sample)?;
        let key = (sample.pattern.clone(), sample.thread_count);
        let calibration = frozen
            .get(&key)
            .ok_or_else(|| "scaling sample has no matching calibration".to_string())?;
        if sample.operations_per_worker != calibration.operations_per_worker {
            return Err("scaling sample did not use the frozen per-worker operation count".into());
        }
        if sample.peak_rss_bytes == 0 {
            return Err(format!(
                "scaling sample for {}/{} on {} has no RSS observation",
                sample.pattern, sample.thread_count, sample.allocator_id
            ));
        }
        if pattern.replays_live_telemetry(false) && sample.response.baseline_rss_bytes == 0 {
            return Err(format!(
                "scaling sample for {}/{} on {} has no baseline RSS for the chart floor",
                sample.pattern, sample.thread_count, sample.allocator_id
            ));
        }
        if pattern.is_distribution() && sample.diagnostic_peak_rss_bytes == 0 {
            return Err(format!(
                "scaling distribution sample for {}/{} on {} has no diagnostic RSS observation",
                sample.pattern, sample.thread_count, sample.allocator_id
            ));
        }
        // Re-derive the whole plan from the seed chain and compare every count.
        // The plan is allocator-independent, so it is derived once per
        // (pattern, thread point, block) and reused across that block's five
        // allocators rather than replayed 4x.
        let plan_key = (
            sample.pattern.clone(),
            sample.thread_count,
            sample.block_id,
            sample.operations_per_worker,
        );
        let expected = *plans.entry(plan_key).or_insert_with(|| {
            simulate_cell(
                pattern,
                raw.run_seed,
                sample.thread_count,
                sample.block_id,
                sample.operations_per_worker,
            )
        });
        let metadata = simulate_plan_metadata(
            pattern,
            raw.run_seed,
            sample.thread_count,
            sample.block_id,
            sample.operations_per_worker,
        );
        if !response_matches_plan(sample, pattern, &expected, &metadata)
            || (pattern.is_distribution()
                && (sample.live_requested_bytes_at_diagnostic_peak_rss
                    > metadata.peak_live_requested_bytes
                    || sample.diagnostic_peak_live_requested_bytes == 0
                    || sample.diagnostic_peak_live_requested_bytes
                        > metadata.peak_live_requested_bytes
                    || sample.live_requested_bytes_at_diagnostic_peak_rss
                        > sample.diagnostic_peak_live_requested_bytes))
        {
            return Err(format!(
                "scaling sample for {}/{} on {} contradicts its derived plan",
                sample.pattern, sample.thread_count, sample.allocator_id
            ));
        }
        sample.response.validate_post_drain(pattern)?;
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
                let expected_blocks = pattern.full_blocks() as usize;
                if observed.len() != expected_blocks {
                    return Err(format!(
                        "scaling matrix cell {key:?} has {} blocks, expected {expected_blocks}",
                        observed.len()
                    ));
                }
            }
        }
    }
    for (key, seen) in &ordinals {
        if seen.len() != ALLOCATOR_IDS.len() {
            return Err(format!(
                "scaling block {key:?} is not a complete paired block of five allocators"
            ));
        }
    }
    let source = frozen
        .get(&(
            ScalingPattern::LargeClassEphemeral.as_str().to_string(),
            THREAD_CHURN_THREADS,
        ))
        .ok_or("scaling raw run has no large-class-ephemeral calibration for thread-churn")?;
    validate_thread_churn_samples(
        raw.run_seed,
        &raw.thread_churn_samples,
        source.operations_per_worker,
        THREAD_CHURN_BLOCKS,
    )
}

/// Identity fields every raw sample carries, whatever it measured.
fn validate_sample_identity(sample: &ScalingRawSample) -> Result<(), String> {
    if !SCALING_THREAD_POINTS.contains(&sample.thread_count) {
        return Err("scaling sample uses an undeclared thread count".into());
    }
    if !ALLOCATOR_IDS.contains(&sample.allocator_id.as_str())
        || sample.metric_schema_version != SCALING_SCHEMA_VERSION
        || sample.ordinal >= ALLOCATOR_IDS.len() as u8
        || sample.reproduction_command.is_empty()
        || !is_lower_hex(&sample.allocator_source_sha, 40)
        || !is_lower_hex(&sample.child_binary_sha256, 64)
    {
        return Err("scaling sample has invalid identity fields".into());
    }
    Ok(())
}

/// The child's response against the plan re-derived from the seed chain.
/// The published number is throughput, so it is re-derived too; checking only
/// that it is finite would let an arbitrary value reach the chart.
fn response_matches_plan(
    sample: &ScalingRawSample,
    pattern: ScalingPattern,
    expected: &ScalingCounts,
    metadata: &ScalingPlanMetadata,
) -> bool {
    let response = &sample.response;
    let expected_throughput =
        expected.operation_count() as f64 * 1_000_000_000.0 / response.elapsed_ns as f64;
    let tolerance = (expected_throughput.abs() * 1e-12).max(f64::EPSILON);
    !(response.alloc_calls != expected.alloc_calls
        || response.realloc_calls != expected.realloc_calls
        || response.free_calls != expected.free_calls
        || response.operation_count != expected.operation_count()
        || response.checksum != expected.checksum
        || (pattern.is_distribution()
            && (response.worker_seeds != metadata.worker_seeds
                || response.size_histogram != metadata.size_histogram
                || response.peak_live_requested_bytes != metadata.peak_live_requested_bytes))
        || response.thread_count != sample.thread_count
        || response.allocator_id != sample.allocator_id
        || response.protocol_version != SCALING_CHILD_PROTOCOL_VERSION
        || response.metric_schema_version != SCALING_SCHEMA_VERSION
        || response.elapsed_ns == 0
        || !response.throughput_operations_per_second.is_finite()
        || response.throughput_operations_per_second <= 0.0
        || (response.throughput_operations_per_second - expected_throughput).abs() > tolerance
        || (!pattern.spec().cross_thread
            && (response.remote_free_calls != 0 || response.producer_fallback_frees != 0))
        || (pattern.spec().cross_thread && response.remote_free_calls > response.free_calls))
}

/// `thread-churn` samples: complete paired blocks at `THREAD_CHURN_THREADS`,
/// each replaying large-class-ephemeral's plan at that cell's frozen operation
/// count -- the same counts and checksum as the sweep's own block -- with one
/// post-drain RSS sample per fixed offset. `blocks` is `THREAD_CHURN_BLOCKS`
/// for a complete run.
pub fn validate_thread_churn_samples(
    run_seed: u64,
    samples: &[ScalingRawSample],
    operations_per_worker: u64,
    blocks: u32,
) -> Result<(), String> {
    let pattern = ScalingPattern::ThreadChurn;
    if samples.len() != blocks as usize * ALLOCATOR_IDS.len() {
        return Err(format!(
            "thread-churn has {} samples, expected {} paired blocks of {} allocators",
            samples.len(),
            blocks,
            ALLOCATOR_IDS.len()
        ));
    }
    let mut seen: BTreeSet<(u32, &str)> = BTreeSet::new();
    let mut ordinals: BTreeMap<u32, BTreeSet<u8>> = BTreeMap::new();
    let mut plans: BTreeMap<u32, (ScalingCounts, ScalingPlanMetadata)> = BTreeMap::new();
    for sample in samples {
        validate_sample_identity(sample)?;
        if sample.pattern != pattern.as_str()
            || sample.thread_count != THREAD_CHURN_THREADS
            || sample.block_id >= blocks
        {
            return Err("thread-churn sample names another pattern, worker count, or block".into());
        }
        if sample.operations_per_worker != operations_per_worker {
            return Err(
                "thread-churn did not replay large-class-ephemeral's frozen operation count".into(),
            );
        }
        if sample.peak_rss_bytes == 0
            || sample.response.baseline_rss_bytes == 0
            || sample.diagnostic_peak_rss_bytes != 0
            || sample.live_requested_bytes_at_diagnostic_peak_rss != 0
            || sample.diagnostic_peak_live_requested_bytes != 0
        {
            return Err(format!(
                "thread-churn sample on {} has no peak or baseline RSS, or carries a diagnostic replay",
                sample.allocator_id
            ));
        }
        let (expected, metadata) = plans.entry(sample.block_id).or_insert_with(|| {
            (
                simulate_cell(
                    pattern,
                    run_seed,
                    THREAD_CHURN_THREADS,
                    sample.block_id,
                    operations_per_worker,
                ),
                simulate_plan_metadata(
                    pattern,
                    run_seed,
                    THREAD_CHURN_THREADS,
                    sample.block_id,
                    operations_per_worker,
                ),
            )
        });
        if !response_matches_plan(sample, pattern, expected, metadata) {
            return Err(format!(
                "thread-churn sample on {} contradicts its derived plan",
                sample.allocator_id
            ));
        }
        sample.response.validate_post_drain(pattern)?;
        if !seen.insert((sample.block_id, sample.allocator_id.as_str())) {
            return Err("thread-churn has a duplicate sample".into());
        }
        ordinals
            .entry(sample.block_id)
            .or_default()
            .insert(sample.ordinal);
    }
    if ordinals.len() != blocks as usize
        || ordinals
            .values()
            .any(|seen| seen.len() != ALLOCATOR_IDS.len())
    {
        return Err("thread-churn blocks are not complete paired blocks of five allocators".into());
    }
    Ok(())
}

pub fn build_scaling_report(raw: &ScalingRawRun) -> Result<ScalingMetricReport, String> {
    validate_scaling_raw_run(raw)?;
    let mut grouped: BTreeMap<(String, u32, String), Vec<f64>> = BTreeMap::new();
    let mut rss_grouped: BTreeMap<(String, u32, String), Vec<u64>> = BTreeMap::new();
    for sample in &raw.samples {
        grouped
            .entry((
                sample.pattern.clone(),
                sample.thread_count,
                sample.allocator_id.clone(),
            ))
            .or_default()
            .push(sample.response.throughput_operations_per_second);
        rss_grouped
            .entry((
                sample.pattern.clone(),
                sample.thread_count,
                sample.allocator_id.clone(),
            ))
            .or_default()
            .push(sample.peak_rss_bytes);
    }
    let mut single: BTreeMap<(String, String), f64> = BTreeMap::new();
    for ((pattern, threads, allocator), values) in &grouped {
        if *threads == 1 {
            let mut values = values.clone();
            single.insert((pattern.clone(), allocator.clone()), median(&mut values));
        }
    }
    let mut cell_summaries = Vec::new();
    for ((pattern, threads, allocator), values) in grouped {
        let mut sorted = values.clone();
        let median_value = median(&mut sorted);
        let p05 = quantile_sorted(&sorted, 0.05);
        let p95 = quantile_sorted(&sorted, 0.95);
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
            p05_throughput: p05,
            p95_throughput: p95,
            min_throughput: minimum,
            max_throughput: maximum,
            speedup_vs_single_worker: median_value / baseline,
        });
    }
    cell_summaries.sort_by(|left, right| {
        (&left.pattern, left.thread_count, &left.allocator_id).cmp(&(
            &right.pattern,
            right.thread_count,
            &right.allocator_id,
        ))
    });
    let mut rss_cell_summaries = Vec::new();
    for ((pattern, threads, allocator), mut values) in rss_grouped {
        values.sort_unstable();
        let p05 = quantile_u64_sorted(&values, 0.05);
        let p50 = quantile_u64_sorted(&values, 0.50);
        let p95 = quantile_u64_sorted(&values, 0.95);
        rss_cell_summaries.push(ScalingRssCellSummary {
            pattern,
            thread_count: threads,
            allocator_id: allocator,
            block_count: values.len() as u32,
            median_peak_rss_bytes: p50,
            p05_peak_rss_bytes: p05,
            p95_peak_rss_bytes: p95,
            min_peak_rss_bytes: *values.first().unwrap_or(&0),
            max_peak_rss_bytes: *values.last().unwrap_or(&0),
        });
    }
    rss_cell_summaries.sort_by(|left, right| {
        (&left.pattern, left.thread_count, &left.allocator_id).cmp(&(
            &right.pattern,
            right.thread_count,
            &right.allocator_id,
        ))
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
        rss: Some(ScalingRssReport {
            metric_schema_version: SCALING_RSS_SCHEMA_VERSION.into(),
            sampling: rss_sampling(),
            cell_summaries: rss_cell_summaries,
            floor_summaries: build_rss_floors(&raw.samples, &raw.thread_churn_samples),
        }),
        raw_samples: raw.samples.clone(),
        thread_churn: Some(build_thread_churn_report(&raw.thread_churn_samples)),
    })
}

pub fn scaling_comparison_key(raw: &ScalingRawRun) -> Result<String, String> {
    #[derive(Serialize)]
    struct Key<'a> {
        schema: &'a str,
        thread_points: &'a [u32],
        blocks_by_pattern: BTreeMap<&'a str, u32>,
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
        blocks_by_pattern: SCALING_PATTERNS
            .iter()
            .map(|pattern| (pattern.as_str(), pattern.full_blocks()))
            .collect(),
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
        let expected_blocks = ScalingPattern::parse(&summary.pattern)
            .ok_or_else(|| "scaling summary names unknown pattern".to_string())?
            .full_blocks();
        if summary.block_count != expected_blocks
            || !summary.median_throughput.is_finite()
            || summary.median_throughput <= 0.0
            || summary.p05_throughput > summary.median_throughput
            || summary.p95_throughput < summary.median_throughput
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
    let expected_samples = SCALING_PATTERNS
        .iter()
        .map(|pattern| {
            pattern.full_blocks() as usize * SCALING_THREAD_POINTS.len() * ALLOCATOR_IDS.len()
        })
        .sum::<usize>();
    if report.raw_samples.len() != expected_samples {
        return Err("scaling report raw sample count does not match its matrix".into());
    }
    if let Some(rss) = &report.rss {
        if rss.metric_schema_version != SCALING_RSS_SCHEMA_VERSION
            || rss.sampling != rss_sampling()
            || rss.cell_summaries.len() != expected_cells
        {
            return Err("scaling RSS side-car has an invalid schema, sampling, or matrix".into());
        }
        let mut rss_seen: BTreeSet<(String, u32, String)> = BTreeSet::new();
        for summary in &rss.cell_summaries {
            let expected_blocks = ScalingPattern::parse(&summary.pattern)
                .ok_or_else(|| "scaling RSS summary names unknown pattern".to_string())?
                .full_blocks();
            if summary.block_count != expected_blocks
                || summary.min_peak_rss_bytes == 0
                || summary.p05_peak_rss_bytes > summary.median_peak_rss_bytes
                || summary.p95_peak_rss_bytes < summary.median_peak_rss_bytes
                || summary.min_peak_rss_bytes > summary.median_peak_rss_bytes
                || summary.max_peak_rss_bytes < summary.median_peak_rss_bytes
                || !rss_seen.insert((
                    summary.pattern.clone(),
                    summary.thread_count,
                    summary.allocator_id.clone(),
                ))
            {
                return Err(format!(
                    "scaling RSS cell summary for {}/{} on {} is invalid or duplicated",
                    summary.pattern, summary.thread_count, summary.allocator_id
                ));
            }
        }
        validate_rss_floors(rss, &report.raw_samples, report.thread_churn.as_ref())?;
    }
    // A fresh report always carries the thread-churn side-car; only rows
    // published before #508 lack it, and those are never re-validated here.
    let thread_churn = report
        .thread_churn
        .as_ref()
        .ok_or("scaling report is missing its thread-churn side-car")?;
    validate_thread_churn_report(thread_churn)
}

/// Build a complete, internally consistent raw run without spawning children.
/// Every count comes from the same oracle production uses, so the fixture can
/// exercise the validator, the report builder, and the renderer end to end.
pub fn synthetic_scaling_fixture(run_seed: u64) -> Result<ScalingRawRun, String> {
    use crate::model::{AffinityMetadata, PowerMetadata};
    let lock = crate::config::AllocatorLock::parse_and_validate(include_str!(
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
                static_library_sha256: crate::validate::repeated_hex(
                    (b'5' + index as u8) as char,
                    64,
                ),
                child_binary_sha256: crate::validate::repeated_hex(
                    ['9', 'a', 'b', 'c', 'd'][index],
                    64,
                ),
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
                elapsed_ns: if pattern.is_distribution() {
                    50_000_000
                } else {
                    SCALING_TARGET_BLOCK_NS
                },
            });
            for block_id in 0..pattern.full_blocks() {
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
                        "bun-mimalloc" => 98,
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
                    let metadata = simulate_plan_metadata(
                        pattern,
                        run_seed,
                        thread_count,
                        block_id,
                        operations_per_worker,
                    );
                    // Synthetic footprint: grows with worker count so the RSS
                    // panel exercises a per-thread-cache slope, plus a small
                    // per-allocator spread and per-block jitter.
                    let peak_rss_bytes = (16u64
                        + u64::from(thread_count) * 4
                        + ordinal as u64 * 3
                        + u64::from(block_id))
                        * 1024
                        * 1024;
                    // #534: the replay's concurrent live peak sits below the
                    // plan's per-worker sum, and with the baseline under
                    // every synthetic peak, so the floor is a true floor.
                    let concurrent_live = metadata.peak_live_requested_bytes.min(
                        (1 + u64::from(thread_count)) * 1024 * 1024 + u64::from(block_id) * 4096,
                    );
                    let baseline_rss_bytes = (4 * 1024 + ordinal as u64 * 4) * 1024;
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
                        peak_rss_bytes,
                        diagnostic_peak_rss_bytes: if pattern.is_distribution() {
                            peak_rss_bytes
                        } else {
                            0
                        },
                        live_requested_bytes_at_diagnostic_peak_rss: if pattern.is_distribution() {
                            concurrent_live
                        } else {
                            0
                        },
                        diagnostic_peak_live_requested_bytes: if pattern.is_distribution() {
                            concurrent_live
                        } else {
                            0
                        },
                        diagnostic_rss_phases: Vec::new(),
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
                            worker_seeds: metadata.worker_seeds.clone(),
                            size_histogram: metadata.size_histogram.clone(),
                            peak_live_requested_bytes: metadata.peak_live_requested_bytes,
                            baseline_rss_bytes,
                            remote_free_calls: 0,
                            producer_fallback_frees: 0,
                            setup_ns: 1,
                            warmup_ns: 0,
                            elapsed_ns,
                            teardown_ns: 1,
                            throughput_operations_per_second: operation_count as f64
                                * 1_000_000_000.0
                                / elapsed_ns as f64,
                            post_drain_offsets_ns: Vec::new(),
                            post_drain_rss_bytes: Vec::new(),
                        },
                    });
                }
            }
        }
    }
    let mut thread_churn_samples = Vec::new();
    for block_id in 0..THREAD_CHURN_BLOCKS {
        let pattern = ScalingPattern::ThreadChurn;
        let expected = simulate_cell(
            pattern,
            run_seed,
            THREAD_CHURN_THREADS,
            block_id,
            operations_per_worker,
        );
        let metadata = simulate_plan_metadata(
            pattern,
            run_seed,
            THREAD_CHURN_THREADS,
            block_id,
            operations_per_worker,
        );
        for (ordinal, allocator) in ALLOCATOR_IDS.into_iter().enumerate() {
            let identity = allocators
                .iter()
                .find(|value| value.allocator_id == allocator)
                .ok_or_else(|| format!("fixture is missing {allocator}"))?;
            // Synthetic release curves (MiB at each offset): the fork gives its
            // memory back by the bound, the others hold more of it, so the
            // renderer and the release-time table see distinct shapes.
            let retained_mib: [u64; 6] = match allocator {
                "mimalloc-pprof" => [150, 90, 30, 6, 5, 5],
                "bun-mimalloc" => [170, 170, 160, 150, 140, 140],
                "upstream-mimalloc" => [175, 175, 175, 170, 170, 170],
                "tcmalloc" => [190, 190, 190, 190, 190, 190],
                _ => [120, 100, 90, 90, 90, 90],
            };
            let jitter = u64::from(block_id % 3) * 4096;
            let elapsed_ns = SCALING_TARGET_BLOCK_NS + u64::from(block_id) * 1_000_000;
            thread_churn_samples.push(ScalingRawSample {
                metric_schema_version: SCALING_SCHEMA_VERSION.into(),
                block_id,
                ordinal: ordinal as u8,
                pattern: pattern.as_str().into(),
                thread_count: THREAD_CHURN_THREADS,
                allocator_id: allocator.into(),
                allocator_source_sha: identity.source_sha.clone(),
                child_binary_sha256: identity.child_binary_sha256.clone(),
                operations_per_worker,
                peak_rss_bytes: (200 + ordinal as u64 * 6 + u64::from(block_id % 5)) * 1024 * 1024,
                diagnostic_peak_rss_bytes: 0,
                live_requested_bytes_at_diagnostic_peak_rss: 0,
                diagnostic_peak_live_requested_bytes: 0,
                diagnostic_rss_phases: Vec::new(),
                reproduction_command: format!(
                    "benchmark-scaling-run --run-seed {run_seed} # thread-churn/{THREAD_CHURN_THREADS}"
                ),
                response: ScalingChildResponse {
                    protocol_version: SCALING_CHILD_PROTOCOL_VERSION.into(),
                    metric_schema_version: SCALING_SCHEMA_VERSION.into(),
                    allocator_id: allocator.into(),
                    thread_count: THREAD_CHURN_THREADS,
                    alloc_calls: expected.alloc_calls,
                    realloc_calls: expected.realloc_calls,
                    free_calls: expected.free_calls,
                    operation_count: expected.operation_count(),
                    checksum: expected.checksum,
                    worker_seeds: metadata.worker_seeds.clone(),
                    size_histogram: metadata.size_histogram.clone(),
                    peak_live_requested_bytes: metadata.peak_live_requested_bytes,
                    baseline_rss_bytes: (4 * 1024 + ordinal as u64 * 4 + u64::from(block_id % 3))
                        * 1024,
                    remote_free_calls: 0,
                    producer_fallback_frees: 0,
                    setup_ns: 1,
                    warmup_ns: 0,
                    elapsed_ns,
                    teardown_ns: 1,
                    throughput_operations_per_second: expected.operation_count() as f64
                        * 1_000_000_000.0
                        / elapsed_ns as f64,
                    post_drain_offsets_ns: THREAD_CHURN_POST_DRAIN_OFFSETS_MS
                        .iter()
                        .map(|offset| offset * NANOS_PER_MILLI + 50_000 + u64::from(block_id))
                        .collect(),
                    post_drain_rss_bytes: retained_mib
                        .iter()
                        .map(|mib| mib * 1024 * 1024 + jitter)
                        .collect(),
                },
            });
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
        thread_churn_samples,
        diagnostic: None,
    })
}

/// Allocators whose source commit is fixed by `allocator-lock.json`. These must
/// match the core run exactly: they are the comparison baseline, and a
/// competitor built from a different commit would silently change what the
/// chart means.
pub const LOCK_PINNED_ALLOCATORS: [&str; 4] =
    ["tcmalloc", "jemalloc", "upstream-mimalloc", "bun-mimalloc"];

pub fn attach_scaling_report(
    latest: &mut LatestReport,
    report: ScalingMetricReport,
) -> Result<(), String> {
    validate_scaling_report(&report)?;
    let expected = latest
        .allocators
        .iter()
        .filter(|value| LOCK_PINNED_ALLOCATORS.contains(&value.allocator_id.as_str()))
        .map(|value| (&value.allocator_id, &value.source_sha))
        .collect::<BTreeSet<_>>();
    let observed = report
        .raw_samples
        .iter()
        .filter(|value| LOCK_PINNED_ALLOCATORS.contains(&value.allocator_id.as_str()))
        .map(|value| (&value.allocator_id, &value.allocator_source_sha))
        .collect::<BTreeSet<_>>();
    if expected != observed {
        return Err("scaling allocator provenance differs from core latest".into());
    }
    // The fork's own commit is deliberately not required to match. This sweep
    // runs weekly and overlays onto whatever daily core envelope is published,
    // so mimalloc-pprof is normally built from a newer commit than the base.
    // Requiring equality would make the overlay permanently unpublishable.
    // Instead the sweep must actually contain the fork, and the commit it was
    // measured at is recorded on the report so the two sections of latest.json
    // can never be read as one build.
    let fork_sources = report
        .raw_samples
        .iter()
        .filter(|value| value.allocator_id == "mimalloc-pprof")
        .map(|value| value.allocator_source_sha.as_str())
        .collect::<BTreeSet<_>>();
    match fork_sources.len() {
        1 => {}
        0 => return Err("scaling run does not contain mimalloc-pprof".into()),
        _ => return Err("scaling run mixes several mimalloc-pprof builds".into()),
    }
    latest.scaling = Some(report);
    latest
        .pending_metrics
        .retain(|value| value.metric_id != "scaling");
    Ok(())
}
