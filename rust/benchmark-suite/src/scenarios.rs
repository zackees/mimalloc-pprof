//! The fixed `core-throughput-v1` workload catalogue.
//!
//! This module deliberately contains no allocator calls.  It is the common,
//! deterministic request producer used by every directly-linked child, so an
//! allocator cannot influence the next requested operation, its touch value,
//! or the checksum used to validate the sample.

use std::fmt;

/// The version is part of every raw record.  Changing a card's semantics,
/// membership, or its declared thread points requires a new suite version.
pub const CORE_THROUGHPUT_V1: &str = "core-throughput-v1";

const BATCH_WIDTH: u32 = 16;
const SAWTOOTH_WIDTH: u32 = 12;
const REALLOC_STEPS: u32 = 6;
const CHURN_GENERATIONS: u32 = 8;
/// The largest declared transaction is a 16-item batch: alloc + touch + free.
/// Executors allocate this buffer before the start gate and reuse it forever.
pub const MAX_REQUESTS_PER_TRANSACTION: usize = 3 * BATCH_WIDTH as usize;
/// Request entropy repeats on this checked-in cycle. Besides making long
/// calibrations reproducible, this lets the controller derive data-dependent
/// touch checksums without regenerating millions of transactions.
pub const REQUEST_CYCLE_OPERATIONS: u64 = 20;

/// A declared point, before it is expanded using runner topology.  Keeping
/// these symbolic is important: a machine with 12 physical cores must not
/// silently turn a "physical-core" card into a generic 12-thread Cartesian
/// product.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash)]
pub enum ThreadPoint {
    One,
    Two,
    PhysicalCores,
    TwiceLogicalCores,
}

impl ThreadPoint {
    pub const fn name(self) -> &'static str {
        match self {
            Self::One => "1",
            Self::Two => "2",
            Self::PhysicalCores => "physical-core",
            Self::TwiceLogicalCores => "2x-logical",
        }
    }

    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "1" => Some(Self::One),
            "2" => Some(Self::Two),
            "physical-core" => Some(Self::PhysicalCores),
            "2x-logical" => Some(Self::TwiceLogicalCores),
            _ => None,
        }
    }
}

/// Runner topology needed to resolve symbolic thread points.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Topology {
    pub physical_cores: usize,
    pub logical_cores: usize,
}

impl Topology {
    pub fn validate(self) -> Result<(), ScenarioError> {
        if self.physical_cores == 0 || self.logical_cores == 0 {
            return Err(ScenarioError::InvalidTopology(
                "physical and logical core counts must both be non-zero",
            ));
        }
        if self.physical_cores > self.logical_cores {
            return Err(ScenarioError::InvalidTopology(
                "physical core count cannot exceed logical core count",
            ));
        }
        Ok(())
    }

    pub fn resolve(self, point: ThreadPoint) -> Result<usize, ScenarioError> {
        self.validate()?;
        match point {
            ThreadPoint::One => Ok(1),
            ThreadPoint::Two => {
                if self.logical_cores < 2 {
                    Err(ScenarioError::InvalidExpansion {
                        point,
                        threads: 2,
                        logical_cores: self.logical_cores,
                    })
                } else {
                    Ok(2)
                }
            }
            ThreadPoint::PhysicalCores => Ok(self.physical_cores),
            ThreadPoint::TwiceLogicalCores => self
                .logical_cores
                .checked_mul(2)
                .ok_or(ScenarioError::ThreadCountOverflow),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ScenarioError {
    UnknownCard,
    UnsupportedThreadPoint {
        card: &'static str,
        point: ThreadPoint,
    },
    InvalidTopology(&'static str),
    InvalidExpansion {
        point: ThreadPoint,
        threads: usize,
        logical_cores: usize,
    },
    ThreadCountOverflow,
    CountOverflow,
    ZeroTransactions,
    WorkerOutOfRange {
        worker: usize,
        threads: usize,
    },
    TransactionOutOfRange {
        operation: u64,
        transactions: u64,
    },
}

impl fmt::Display for ScenarioError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnknownCard => write!(f, "unknown core-throughput-v1 scenario card"),
            Self::UnsupportedThreadPoint { card, point } => {
                write!(f, "thread point {} is not declared by {card}", point.name())
            }
            Self::InvalidTopology(reason) => write!(f, "invalid CPU topology: {reason}"),
            Self::InvalidExpansion {
                point,
                threads,
                logical_cores,
            } => write!(
                f,
                "cannot expand {} to {threads} threads on {logical_cores} logical cores",
                point.name()
            ),
            Self::ThreadCountOverflow => write!(f, "thread count expansion overflowed"),
            Self::CountOverflow => write!(f, "scenario transaction or token count overflowed"),
            Self::ZeroTransactions => write!(f, "a scenario cell needs at least one transaction"),
            Self::WorkerOutOfRange { worker, threads } => {
                write!(f, "worker {worker} is outside a {threads}-worker cell")
            }
            Self::TransactionOutOfRange {
                operation,
                transactions,
            } => write!(
                f,
                "operation {operation} is outside a {transactions}-transaction worker stream"
            ),
        }
    }
}

impl std::error::Error for ScenarioError {}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OperationUnit {
    Transaction,
    Batch,
    WorkerGeneration,
    MixedTransaction,
}

impl OperationUnit {
    pub const fn name(self) -> &'static str {
        match self {
            Self::Transaction => "transaction",
            Self::Batch => "batch",
            Self::WorkerGeneration => "worker-generation",
            Self::MixedTransaction => "mixed-transaction",
        }
    }
}

/// Requested-byte distributions. These are independent of allocator usable
/// size and make the workload contract inspectable without parsing its name.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SizeDistribution {
    Fixed(usize),
    LogParetoLike {
        min: usize,
        max: usize,
    },
    AlignedRange {
        min_alignment: usize,
        max_alignment: usize,
    },
    RepresentativeWeightedMix,
    WorkerGenerationMix,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum LifetimeRule {
    ImmediateFree,
    BatchLifo,
    BatchFifo,
    CrossThreadProducerConsumer,
    OwnerPermutation,
    GeometricRealloc,
    RetainThenDrain,
    NativeWorkerGenerations,
    WeightedMix,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TouchRule {
    DeterministicBytePattern,
    PagePattern,
    ZeroThenBytePattern,
    AlignedAddressAndPattern,
    PreserveThenBytePattern,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ScenarioInvariant {
    AllocTouchFree,
    FreeOrderIsLifo,
    FreeOrderIsFifo,
    FreeIsRemote,
    OwnershipIsPermutation,
    ReallocPreservesPrefix,
    CallocIsZero,
    AddressHonorsAlignment,
    RetainedSubsetDrains,
    GenerationCompletionExact,
    WeightedActionMix,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CardId {
    TinyFixed16,
    TinyFixed64,
    SmallLogMixed,
    MediumLogMixed,
    LargeObjects,
    BatchLifo,
    BatchFifo,
    CrossThreadProducerConsumer,
    RandomOwnership,
    ReallocGeometric,
    CallocZero,
    AlignedRange,
    SawtoothRetainDrain,
    ThreadChurn,
    RepresentativeMix,
}

impl CardId {
    pub const ALL: [Self; 15] = [
        Self::TinyFixed16,
        Self::TinyFixed64,
        Self::SmallLogMixed,
        Self::MediumLogMixed,
        Self::LargeObjects,
        Self::BatchLifo,
        Self::BatchFifo,
        Self::CrossThreadProducerConsumer,
        Self::RandomOwnership,
        Self::ReallocGeometric,
        Self::CallocZero,
        Self::AlignedRange,
        Self::SawtoothRetainDrain,
        Self::ThreadChurn,
        Self::RepresentativeMix,
    ];

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::TinyFixed16 => "tiny-fixed-16",
            Self::TinyFixed64 => "tiny-fixed-64",
            Self::SmallLogMixed => "small-log-mixed",
            Self::MediumLogMixed => "medium-log-mixed",
            Self::LargeObjects => "large-objects",
            Self::BatchLifo => "batch-lifo",
            Self::BatchFifo => "batch-fifo",
            Self::CrossThreadProducerConsumer => "cross-thread-producer-consumer",
            Self::RandomOwnership => "random-ownership",
            Self::ReallocGeometric => "realloc-geometric",
            Self::CallocZero => "calloc-zero",
            Self::AlignedRange => "aligned-range",
            Self::SawtoothRetainDrain => "sawtooth-retain-drain",
            Self::ThreadChurn => "thread-churn",
            Self::RepresentativeMix => "representative-mix",
        }
    }

    pub fn parse(id: &str) -> Option<Self> {
        Self::ALL
            .into_iter()
            .find(|candidate| candidate.as_str() == id)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ScenarioCard {
    pub id: CardId,
    pub operation_unit: OperationUnit,
    pub thread_points: &'static [ThreadPoint],
    pub description: &'static str,
}

impl ScenarioCard {
    pub fn operation_count(self, counts: &ExpectedCounts) -> u64 {
        match self.operation_unit {
            OperationUnit::WorkerGeneration => counts.worker_generations,
            OperationUnit::Transaction | OperationUnit::Batch | OperationUnit::MixedTransaction => {
                counts.requested_transactions
            }
        }
    }

    pub const fn max_live_allocations_per_worker(self) -> usize {
        match self.id {
            CardId::BatchLifo | CardId::BatchFifo | CardId::RepresentativeMix => {
                BATCH_WIDTH as usize
            }
            CardId::SawtoothRetainDrain => SAWTOOTH_WIDTH as usize,
            _ => 1,
        }
    }

    pub const fn size_distribution(self) -> SizeDistribution {
        match self.id {
            CardId::TinyFixed16 => SizeDistribution::Fixed(16),
            CardId::TinyFixed64 => SizeDistribution::Fixed(64),
            CardId::SmallLogMixed => SizeDistribution::LogParetoLike { min: 8, max: 1024 },
            CardId::MediumLogMixed | CardId::SawtoothRetainDrain => {
                SizeDistribution::LogParetoLike {
                    min: 4 * 1024,
                    max: 64 * 1024,
                }
            }
            CardId::LargeObjects => SizeDistribution::LogParetoLike {
                min: 1024 * 1024,
                max: 16 * 1024 * 1024,
            },
            CardId::BatchLifo
            | CardId::BatchFifo
            | CardId::CrossThreadProducerConsumer
            | CardId::RandomOwnership
            | CardId::CallocZero => SizeDistribution::LogParetoLike { min: 8, max: 1024 },
            CardId::ReallocGeometric => SizeDistribution::LogParetoLike { min: 16, max: 8192 },
            CardId::AlignedRange => SizeDistribution::AlignedRange {
                min_alignment: 16,
                max_alignment: 4096,
            },
            CardId::ThreadChurn => SizeDistribution::WorkerGenerationMix,
            CardId::RepresentativeMix => SizeDistribution::RepresentativeWeightedMix,
        }
    }

    pub const fn lifetime(self) -> LifetimeRule {
        match self.id {
            CardId::TinyFixed16
            | CardId::TinyFixed64
            | CardId::SmallLogMixed
            | CardId::MediumLogMixed
            | CardId::LargeObjects
            | CardId::CallocZero
            | CardId::AlignedRange => LifetimeRule::ImmediateFree,
            CardId::BatchLifo => LifetimeRule::BatchLifo,
            CardId::BatchFifo => LifetimeRule::BatchFifo,
            CardId::CrossThreadProducerConsumer => LifetimeRule::CrossThreadProducerConsumer,
            CardId::RandomOwnership => LifetimeRule::OwnerPermutation,
            CardId::ReallocGeometric => LifetimeRule::GeometricRealloc,
            CardId::SawtoothRetainDrain => LifetimeRule::RetainThenDrain,
            CardId::ThreadChurn => LifetimeRule::NativeWorkerGenerations,
            CardId::RepresentativeMix => LifetimeRule::WeightedMix,
        }
    }

    pub const fn touch_rule(self) -> TouchRule {
        match self.id {
            CardId::LargeObjects => TouchRule::PagePattern,
            CardId::CallocZero => TouchRule::ZeroThenBytePattern,
            CardId::AlignedRange => TouchRule::AlignedAddressAndPattern,
            CardId::ReallocGeometric => TouchRule::PreserveThenBytePattern,
            _ => TouchRule::DeterministicBytePattern,
        }
    }

    pub const fn invariant(self) -> ScenarioInvariant {
        match self.id {
            CardId::TinyFixed16
            | CardId::TinyFixed64
            | CardId::SmallLogMixed
            | CardId::MediumLogMixed
            | CardId::LargeObjects => ScenarioInvariant::AllocTouchFree,
            CardId::BatchLifo => ScenarioInvariant::FreeOrderIsLifo,
            CardId::BatchFifo => ScenarioInvariant::FreeOrderIsFifo,
            CardId::CrossThreadProducerConsumer => ScenarioInvariant::FreeIsRemote,
            CardId::RandomOwnership => ScenarioInvariant::OwnershipIsPermutation,
            CardId::ReallocGeometric => ScenarioInvariant::ReallocPreservesPrefix,
            CardId::CallocZero => ScenarioInvariant::CallocIsZero,
            CardId::AlignedRange => ScenarioInvariant::AddressHonorsAlignment,
            CardId::SawtoothRetainDrain => ScenarioInvariant::RetainedSubsetDrains,
            CardId::ThreadChurn => ScenarioInvariant::GenerationCompletionExact,
            CardId::RepresentativeMix => ScenarioInvariant::WeightedActionMix,
        }
    }
}

const ONE_AND_PHYSICAL: &[ThreadPoint] = &[ThreadPoint::One, ThreadPoint::PhysicalCores];
const ONE_PHYSICAL_TWICE_LOGICAL: &[ThreadPoint] = &[
    ThreadPoint::One,
    ThreadPoint::PhysicalCores,
    ThreadPoint::TwiceLogicalCores,
];
const ONE_AND_TWO: &[ThreadPoint] = &[ThreadPoint::One, ThreadPoint::Two];
const TWO_AND_PHYSICAL: &[ThreadPoint] = &[ThreadPoint::Two, ThreadPoint::PhysicalCores];
const PHYSICAL_ONLY: &[ThreadPoint] = &[ThreadPoint::PhysicalCores];

const CARDS: [ScenarioCard; 15] = [
    ScenarioCard {
        id: CardId::TinyFixed16,
        operation_unit: OperationUnit::Transaction,
        thread_points: ONE_AND_PHYSICAL,
        description: "fixed 16 B alloc, touch, and free",
    },
    ScenarioCard {
        id: CardId::TinyFixed64,
        operation_unit: OperationUnit::Transaction,
        thread_points: ONE_AND_PHYSICAL,
        description: "fixed 64 B alloc, touch, and free",
    },
    ScenarioCard {
        id: CardId::SmallLogMixed,
        operation_unit: OperationUnit::Transaction,
        thread_points: ONE_PHYSICAL_TWICE_LOGICAL,
        description: "deterministic log/Pareto-like 8-1024 B requests",
    },
    ScenarioCard {
        id: CardId::MediumLogMixed,
        operation_unit: OperationUnit::Transaction,
        thread_points: ONE_AND_PHYSICAL,
        description: "deterministic 4-64 KiB requests",
    },
    ScenarioCard {
        id: CardId::LargeObjects,
        operation_unit: OperationUnit::Transaction,
        thread_points: ONE_AND_TWO,
        description: "deterministic 1-16 MiB requests with page touching",
    },
    ScenarioCard {
        id: CardId::BatchLifo,
        operation_unit: OperationUnit::Batch,
        thread_points: ONE_AND_PHYSICAL,
        description: "allocate a batch then free it in reverse order",
    },
    ScenarioCard {
        id: CardId::BatchFifo,
        operation_unit: OperationUnit::Batch,
        thread_points: ONE_AND_PHYSICAL,
        description: "allocate a batch then free it in insertion order",
    },
    ScenarioCard {
        id: CardId::CrossThreadProducerConsumer,
        operation_unit: OperationUnit::Transaction,
        thread_points: TWO_AND_PHYSICAL,
        description: "distinct producer and consumer ownership",
    },
    ScenarioCard {
        id: CardId::RandomOwnership,
        operation_unit: OperationUnit::Transaction,
        thread_points: PHYSICAL_ONLY,
        description: "deterministic ownership permutation",
    },
    ScenarioCard {
        id: CardId::ReallocGeometric,
        operation_unit: OperationUnit::Transaction,
        thread_points: ONE_AND_PHYSICAL,
        description: "geometric grow and shrink with preservation checks",
    },
    ScenarioCard {
        id: CardId::CallocZero,
        operation_unit: OperationUnit::Transaction,
        thread_points: ONE_AND_PHYSICAL,
        description: "calloc zero verification followed by touch",
    },
    ScenarioCard {
        id: CardId::AlignedRange,
        operation_unit: OperationUnit::Transaction,
        thread_points: ONE_AND_PHYSICAL,
        description: "16-4096 B alignment with address and content checks",
    },
    ScenarioCard {
        id: CardId::SawtoothRetainDrain,
        operation_unit: OperationUnit::Batch,
        thread_points: ONE_AND_PHYSICAL,
        description: "burst, deterministic retention, and drain",
    },
    ScenarioCard {
        id: CardId::ThreadChurn,
        operation_unit: OperationUnit::WorkerGeneration,
        thread_points: ONE_AND_PHYSICAL,
        description: "repeated native worker generations",
    },
    ScenarioCard {
        id: CardId::RepresentativeMix,
        operation_unit: OperationUnit::MixedTransaction,
        thread_points: ONE_AND_PHYSICAL,
        description: "checked-in weighted small/medium/realloc/batch mix",
    },
];

pub const fn cards() -> &'static [ScenarioCard; 15] {
    &CARDS
}

/// Compatibility name used by the phase runner; the catalogue is intentionally
/// the fixed core suite rather than a parameter-generated scenario matrix.
pub const fn core_scenarios() -> &'static [ScenarioCard; 15] {
    cards()
}

pub fn card(id: CardId) -> &'static ScenarioCard {
    // `CardId::ALL` and CARDS intentionally have matching stable order.
    &CARDS[id as usize]
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RequestKind {
    Alloc,
    Calloc,
    Realloc,
    AlignedAlloc,
    Free,
    /// Checks that a calloc result is all zero before it is touched.
    VerifyZero,
    /// Writes and reads a deterministic byte pattern, contributing to checksum.
    Touch,
    /// Separates native worker generations in the churn scenario.
    WorkerGeneration,
}

/// A single executor request. `phase` is a deterministic barrier number: all
/// phase N allocation actions must finish before their phase N+1 frees are
/// attempted.  This is what makes cross-thread ownership executable rather
/// than merely a label in a result row.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Request {
    pub kind: RequestKind,
    pub phase: u32,
    pub transaction: u64,
    pub token: u64,
    pub owner_worker: usize,
    pub executor_worker: usize,
    pub size: usize,
    pub alignment: usize,
    pub touch: u64,
    pub preserve_bytes: usize,
}

impl Request {
    const fn alloc(
        phase: u32,
        transaction: u64,
        token: u64,
        owner_worker: usize,
        executor_worker: usize,
        size: usize,
        touch: u64,
    ) -> Self {
        Self {
            kind: RequestKind::Alloc,
            phase,
            transaction,
            token,
            owner_worker,
            executor_worker,
            size,
            alignment: 0,
            touch,
            preserve_bytes: 0,
        }
    }

    const fn calloc(transaction: u64, token: u64, worker: usize, size: usize, touch: u64) -> Self {
        Self {
            kind: RequestKind::Calloc,
            phase: 0,
            transaction,
            token,
            owner_worker: worker,
            executor_worker: worker,
            size,
            alignment: 0,
            touch,
            preserve_bytes: 0,
        }
    }

    const fn free(
        phase: u32,
        transaction: u64,
        token: u64,
        owner_worker: usize,
        executor_worker: usize,
    ) -> Self {
        Self {
            kind: RequestKind::Free,
            phase,
            transaction,
            token,
            owner_worker,
            executor_worker,
            size: 0,
            alignment: 0,
            touch: 0,
            preserve_bytes: 0,
        }
    }

    const fn touch(
        phase: u32,
        transaction: u64,
        token: u64,
        worker: usize,
        size: usize,
        touch: u64,
    ) -> Self {
        Self {
            kind: RequestKind::Touch,
            phase,
            transaction,
            token,
            owner_worker: worker,
            executor_worker: worker,
            size,
            alignment: 0,
            touch,
            preserve_bytes: 0,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WorkerStream {
    pub worker: usize,
    pub requests: Vec<Request>,
    pub checksum: u64,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct ExpectedCounts {
    pub requested_transactions: u64,
    /// Completion count required for a valid sample. A partial execution must
    /// not be representable as a valid cell result.
    pub completed_transactions: u64,
    pub allocator_calls: u64,
    pub alloc_calls: u64,
    pub calloc_calls: u64,
    pub realloc_calls: u64,
    pub aligned_alloc_calls: u64,
    pub free_calls: u64,
    pub touches: u64,
    pub worker_generations: u64,
}

impl ExpectedCounts {
    pub fn record(&mut self, request: &Request) {
        match request.kind {
            RequestKind::Alloc => {
                self.allocator_calls += 1;
                self.alloc_calls += 1;
            }
            RequestKind::Calloc => {
                self.allocator_calls += 1;
                self.calloc_calls += 1;
            }
            RequestKind::Realloc => {
                self.allocator_calls += 1;
                self.realloc_calls += 1;
            }
            RequestKind::AlignedAlloc => {
                self.allocator_calls += 1;
                self.aligned_alloc_calls += 1;
            }
            RequestKind::Free => {
                self.allocator_calls += 1;
                self.free_calls += 1;
            }
            RequestKind::Touch => self.touches += 1,
            RequestKind::WorkerGeneration => self.worker_generations += 1,
            RequestKind::VerifyZero => {}
        }
    }
}

/// A resolved cell. `transactions_per_worker` is deliberately explicit so the
/// runner can freeze a calibrated count and replay it unchanged for all five
/// allocators in a paired block.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ScenarioCell {
    pub suite_version: &'static str,
    pub card: CardId,
    pub thread_point: ThreadPoint,
    pub threads: usize,
    pub seed: u64,
    pub transactions_per_worker: u64,
}

impl ScenarioCell {
    pub fn new(
        card_id: CardId,
        thread_point: ThreadPoint,
        topology: Topology,
        transactions_per_worker: u64,
        seed: u64,
    ) -> Result<Self, ScenarioError> {
        if transactions_per_worker == 0 {
            return Err(ScenarioError::ZeroTransactions);
        }
        let definition = card(card_id);
        if !definition.thread_points.contains(&thread_point) {
            return Err(ScenarioError::UnsupportedThreadPoint {
                card: card_id.as_str(),
                point: thread_point,
            });
        }
        let threads = topology.resolve(thread_point)?;
        let total_transactions = transactions_per_worker
            .checked_mul(threads as u64)
            .ok_or(ScenarioError::CountOverflow)?;
        total_transactions
            .checked_mul(64)
            .and_then(|value| value.checked_add(64))
            .ok_or(ScenarioError::CountOverflow)?;
        // The two cross-worker cards would otherwise be falsely labelled on a
        // one-core machine whose physical point resolves to one worker.
        if matches!(
            card_id,
            CardId::CrossThreadProducerConsumer | CardId::RandomOwnership
        ) && threads < 2
        {
            return Err(ScenarioError::InvalidExpansion {
                point: thread_point,
                threads,
                logical_cores: topology.logical_cores,
            });
        }
        Ok(Self {
            suite_version: CORE_THROUGHPUT_V1,
            card: card_id,
            thread_point,
            threads,
            seed,
            transactions_per_worker,
        })
    }

    pub fn requested_transactions(&self) -> u64 {
        self.transactions_per_worker * self.threads as u64
    }

    /// Generate one transaction into a caller-owned fixed-capacity buffer.
    /// This is the production execution path; `worker_stream` remains a small
    /// test/inspection convenience and is never used by the benchmark child.
    pub fn fill_worker_transaction(
        &self,
        worker: usize,
        operation: u64,
        requests: &mut Vec<Request>,
    ) -> Result<(), ScenarioError> {
        if worker >= self.threads {
            return Err(ScenarioError::WorkerOutOfRange {
                worker,
                threads: self.threads,
            });
        }
        if operation >= self.transactions_per_worker {
            return Err(ScenarioError::TransactionOutOfRange {
                operation,
                transactions: self.transactions_per_worker,
            });
        }
        requests.clear();
        let mut checksum = None;
        let transaction = worker as u64 * self.transactions_per_worker + operation;
        let cycle_operation = operation % REQUEST_CYCLE_OPERATIONS;
        self.append_transaction(
            worker,
            cycle_operation,
            transaction,
            requests,
            &mut checksum,
        );
        debug_assert!(requests.len() <= MAX_REQUESTS_PER_TRANSACTION);
        Ok(())
    }

    pub fn worker_stream(&self, worker: usize) -> Result<WorkerStream, ScenarioError> {
        if worker >= self.threads {
            return Err(ScenarioError::WorkerOutOfRange {
                worker,
                threads: self.threads,
            });
        }
        let mut requests = Vec::new();
        let mut checksum = FNV_OFFSET;
        let mut checksum_sink = Some(&mut checksum);
        for operation in 0..self.transactions_per_worker {
            let transaction = (worker as u64) * self.transactions_per_worker + operation;
            self.append_transaction(
                worker,
                operation % REQUEST_CYCLE_OPERATIONS,
                transaction,
                &mut requests,
                &mut checksum_sink,
            );
        }
        Ok(WorkerStream {
            worker,
            requests,
            checksum,
        })
    }

    pub fn streams(&self) -> Result<Vec<WorkerStream>, ScenarioError> {
        (0..self.threads)
            .map(|worker| self.worker_stream(worker))
            .collect()
    }

    /// Requests actually performed by one native worker, ordered first by the
    /// per-worker operation ordinal and then by barrier phase. For ordinary
    /// cards the executor uses the owner stream directly; cross-thread cards
    /// use this view to interleave one local allocation and one remote free per
    /// operation without stretching lifetimes across the full sample.
    pub fn executor_stream(&self, worker: usize) -> Result<WorkerStream, ScenarioError> {
        if worker >= self.threads {
            return Err(ScenarioError::WorkerOutOfRange {
                worker,
                threads: self.threads,
            });
        }
        let mut requests: Vec<Request> = self
            .streams()?
            .into_iter()
            .flat_map(|stream| stream.requests)
            .filter(|request| request.executor_worker == worker)
            .collect();
        requests.sort_by_key(|request| {
            (
                request.transaction % self.transactions_per_worker,
                request.phase,
                request.transaction,
                request.token,
            )
        });
        let checksum = requests.iter().fold(FNV_OFFSET, |state, request| {
            checksum_request(state, request)
        });
        Ok(WorkerStream {
            worker,
            requests,
            checksum,
        })
    }

    pub fn expected_counts(&self) -> Result<ExpectedCounts, ScenarioError> {
        let requested_transactions = self.requested_transactions();
        let mut counts = ExpectedCounts {
            requested_transactions,
            completed_transactions: requested_transactions,
            ..ExpectedCounts::default()
        };
        let per_worker = counts_for_operations(self.card, self.transactions_per_worker);
        let workers = self.threads as u64;
        counts.alloc_calls = per_worker.alloc_calls * workers;
        counts.calloc_calls = per_worker.calloc_calls * workers;
        counts.realloc_calls = per_worker.realloc_calls * workers;
        counts.aligned_alloc_calls = per_worker.aligned_alloc_calls * workers;
        counts.free_calls = per_worker.free_calls * workers;
        counts.touches = per_worker.touches * workers;
        counts.worker_generations = per_worker.worker_generations * workers;
        counts.allocator_calls = counts.alloc_calls
            + counts.calloc_calls
            + counts.realloc_calls
            + counts.aligned_alloc_calls
            + counts.free_calls;
        Ok(counts)
    }

    pub fn worker_contract_checksum(&self, worker: usize) -> Result<u64, ScenarioError> {
        if worker >= self.threads {
            return Err(ScenarioError::WorkerOutOfRange {
                worker,
                threads: self.threads,
            });
        }
        let mut state = FNV_OFFSET;
        state = fnv_u64(state, self.card as u64);
        state = fnv_u64(state, self.thread_point as u64);
        state = fnv_u64(state, self.threads as u64);
        state = fnv_u64(state, self.seed);
        state = fnv_u64(state, self.transactions_per_worker);
        Ok(fnv_u64(state, worker as u64))
    }

    /// A stable bounded-size contract checksum for the cell identity. The raw
    /// sample's separately-derived touch checksum remains data-dependent on
    /// values observed through the native adapter.
    pub fn expected_checksum(&self) -> Result<u64, ScenarioError> {
        let mut state = FNV_OFFSET;
        for worker in 0..self.threads {
            state = fnv_u64(state, worker as u64);
            state = fnv_u64(state, self.worker_contract_checksum(worker)?);
        }
        Ok(state)
    }

    pub fn topology_oracle(&self) -> Result<TopologyOracle, ScenarioError> {
        let streams = self.streams()?;
        let mut cross_thread_frees = 0_u64;
        let mut allocation_tokens = 0_u64;
        for stream in &streams {
            for request in &stream.requests {
                if matches!(
                    request.kind,
                    RequestKind::Alloc | RequestKind::Calloc | RequestKind::AlignedAlloc
                ) {
                    allocation_tokens += 1;
                }
                if request.kind == RequestKind::Free
                    && request.owner_worker != request.executor_worker
                {
                    cross_thread_frees += 1;
                }
            }
        }
        let claims_cross_thread = matches!(
            self.card,
            CardId::CrossThreadProducerConsumer | CardId::RandomOwnership
        );
        Ok(TopologyOracle {
            claims_cross_thread,
            allocation_tokens,
            cross_thread_frees,
        })
    }

    /// Checks the card's asserted ownership topology directly from generated
    /// requests. For random ownership every operation must map owners to a
    /// one-to-one remote consumer permutation.
    pub fn ownership_oracle(&self) -> Result<bool, ScenarioError> {
        if self.card != CardId::RandomOwnership {
            return Ok(true);
        }
        let streams = self.streams()?;
        for operation in 0..self.transactions_per_worker {
            let mut consumers = vec![false; self.threads];
            for (owner, stream) in streams.iter().enumerate() {
                let transaction = (owner as u64) * self.transactions_per_worker + operation;
                let free = stream.requests.iter().find(|request| {
                    request.kind == RequestKind::Free && request.transaction == transaction
                });
                let Some(free) = free else { return Ok(false) };
                if free.executor_worker == owner || consumers[free.executor_worker] {
                    return Ok(false);
                }
                consumers[free.executor_worker] = true;
            }
            if consumers.iter().any(|seen| !seen) {
                return Ok(false);
            }
        }
        Ok(true)
    }

    fn append_transaction(
        &self,
        worker: usize,
        operation: u64,
        transaction: u64,
        out: &mut Vec<Request>,
        checksum: &mut Option<&mut u64>,
    ) {
        let entropy = mix(self.seed ^ ((worker as u64) << 32) ^ operation);
        let base_token = transaction * 64 + 1;
        match self.card {
            CardId::TinyFixed16 => {
                self.simple_alloc_free(worker, transaction, base_token, 16, entropy, out, checksum)
            }
            CardId::TinyFixed64 => {
                self.simple_alloc_free(worker, transaction, base_token, 64, entropy, out, checksum)
            }
            CardId::SmallLogMixed => self.simple_alloc_free(
                worker,
                transaction,
                base_token,
                small_size(entropy),
                entropy,
                out,
                checksum,
            ),
            CardId::MediumLogMixed => self.simple_alloc_free(
                worker,
                transaction,
                base_token,
                medium_size(entropy),
                entropy,
                out,
                checksum,
            ),
            CardId::LargeObjects => self.simple_alloc_free(
                worker,
                transaction,
                base_token,
                large_size(entropy),
                entropy,
                out,
                checksum,
            ),
            CardId::BatchLifo => self.batch(
                worker,
                transaction,
                base_token,
                entropy,
                true,
                out,
                checksum,
            ),
            CardId::BatchFifo => self.batch(
                worker,
                transaction,
                base_token,
                entropy,
                false,
                out,
                checksum,
            ),
            CardId::CrossThreadProducerConsumer => self.cross_thread(
                worker,
                operation,
                transaction,
                base_token,
                entropy,
                false,
                out,
                checksum,
            ),
            CardId::RandomOwnership => self.cross_thread(
                worker,
                operation,
                transaction,
                base_token,
                entropy,
                true,
                out,
                checksum,
            ),
            CardId::ReallocGeometric => {
                self.realloc_geometric(worker, transaction, base_token, entropy, out, checksum)
            }
            CardId::CallocZero => {
                self.calloc_zero(worker, transaction, base_token, entropy, out, checksum)
            }
            CardId::AlignedRange => {
                self.aligned(worker, transaction, base_token, entropy, out, checksum)
            }
            CardId::SawtoothRetainDrain => {
                self.sawtooth(worker, transaction, base_token, entropy, out, checksum)
            }
            CardId::ThreadChurn => {
                self.thread_churn(worker, transaction, base_token, entropy, out, checksum)
            }
            CardId::RepresentativeMix => self.representative_mix(
                worker,
                operation,
                transaction,
                base_token,
                entropy,
                out,
                checksum,
            ),
        }
    }

    fn push(&self, out: &mut Vec<Request>, checksum: &mut Option<&mut u64>, request: Request) {
        if let Some(state) = checksum.as_deref_mut() {
            *state = checksum_request(*state, &request);
        }
        out.push(request);
    }

    fn simple_alloc_free(
        &self,
        worker: usize,
        tx: u64,
        token: u64,
        size: usize,
        touch: u64,
        out: &mut Vec<Request>,
        sum: &mut Option<&mut u64>,
    ) {
        self.push(
            out,
            sum,
            Request::alloc(0, tx, token, worker, worker, size, touch),
        );
        self.push(out, sum, Request::touch(0, tx, token, worker, size, touch));
        self.push(out, sum, Request::free(1, tx, token, worker, worker));
    }

    fn batch(
        &self,
        worker: usize,
        tx: u64,
        token: u64,
        entropy: u64,
        lifo: bool,
        out: &mut Vec<Request>,
        sum: &mut Option<&mut u64>,
    ) {
        for slot in 0..BATCH_WIDTH {
            let slot_token = token + slot as u64;
            let size = 8usize << ((entropy.wrapping_add(slot as u64) % 8) as usize);
            self.push(
                out,
                sum,
                Request::alloc(
                    0,
                    tx,
                    slot_token,
                    worker,
                    worker,
                    size,
                    mix(entropy ^ slot as u64),
                ),
            );
            self.push(
                out,
                sum,
                Request::touch(0, tx, slot_token, worker, size, mix(entropy ^ slot as u64)),
            );
        }
        for order in 0..BATCH_WIDTH {
            let slot = if lifo { BATCH_WIDTH - 1 - order } else { order };
            self.push(
                out,
                sum,
                Request::free(1, tx, token + slot as u64, worker, worker),
            );
        }
    }

    fn cross_thread(
        &self,
        worker: usize,
        operation: u64,
        tx: u64,
        token: u64,
        entropy: u64,
        random: bool,
        out: &mut Vec<Request>,
        sum: &mut Option<&mut u64>,
    ) {
        let consumer = if random {
            // Every operation gets a seed-derived cyclic derangement. Using
            // one offset for all owners makes this a true permutation.
            let offset = 1 + (mix(self.seed ^ operation) as usize % (self.threads - 1));
            (worker + offset) % self.threads
        } else {
            (worker + 1) % self.threads
        };
        let size = small_size(entropy);
        self.push(
            out,
            sum,
            Request::alloc(0, tx, token, worker, worker, size, entropy),
        );
        self.push(
            out,
            sum,
            Request::touch(0, tx, token, worker, size, entropy),
        );
        // The owner stream records the expected remote consumer; the executor
        // dispatches phase-1 frees to the stream whose worker equals consumer.
        self.push(out, sum, Request::free(1, tx, token, worker, consumer));
    }

    fn realloc_geometric(
        &self,
        worker: usize,
        tx: u64,
        token: u64,
        entropy: u64,
        out: &mut Vec<Request>,
        sum: &mut Option<&mut u64>,
    ) {
        let mut size = 16usize << ((entropy % 5) as usize);
        self.push(
            out,
            sum,
            Request::alloc(0, tx, token, worker, worker, size, entropy),
        );
        self.push(
            out,
            sum,
            Request::touch(0, tx, token, worker, size, entropy),
        );
        for step in 0..REALLOC_STEPS {
            let grow = step < REALLOC_STEPS / 2;
            let next = if grow {
                size.saturating_mul(2)
            } else {
                (size / 2).max(16)
            };
            let request = Request {
                kind: RequestKind::Realloc,
                phase: 0,
                transaction: tx,
                token,
                owner_worker: worker,
                executor_worker: worker,
                size: next,
                alignment: 0,
                touch: mix(entropy ^ step as u64),
                preserve_bytes: size.min(next),
            };
            self.push(out, sum, request);
            size = next;
            self.push(
                out,
                sum,
                Request::touch(0, tx, token, worker, size, mix(entropy ^ step as u64)),
            );
        }
        self.push(out, sum, Request::free(1, tx, token, worker, worker));
    }

    fn calloc_zero(
        &self,
        worker: usize,
        tx: u64,
        token: u64,
        entropy: u64,
        out: &mut Vec<Request>,
        sum: &mut Option<&mut u64>,
    ) {
        let size = 8usize << ((entropy % 8) as usize);
        self.push(out, sum, Request::calloc(tx, token, worker, size, entropy));
        self.push(
            out,
            sum,
            Request {
                kind: RequestKind::VerifyZero,
                phase: 0,
                transaction: tx,
                token,
                owner_worker: worker,
                executor_worker: worker,
                size,
                alignment: 0,
                touch: 0,
                preserve_bytes: 0,
            },
        );
        self.push(
            out,
            sum,
            Request::touch(0, tx, token, worker, size, entropy),
        );
        self.push(out, sum, Request::free(1, tx, token, worker, worker));
    }

    fn aligned(
        &self,
        worker: usize,
        tx: u64,
        token: u64,
        entropy: u64,
        out: &mut Vec<Request>,
        sum: &mut Option<&mut u64>,
    ) {
        let alignment = 16usize << ((entropy % 9) as usize); // 16 through 4096
        let size = alignment + ((entropy >> 12) as usize & (alignment - 1));
        self.push(
            out,
            sum,
            Request {
                kind: RequestKind::AlignedAlloc,
                phase: 0,
                transaction: tx,
                token,
                owner_worker: worker,
                executor_worker: worker,
                size,
                alignment,
                touch: entropy,
                preserve_bytes: 0,
            },
        );
        self.push(
            out,
            sum,
            Request::touch(0, tx, token, worker, size, entropy),
        );
        self.push(out, sum, Request::free(1, tx, token, worker, worker));
    }

    fn sawtooth(
        &self,
        worker: usize,
        tx: u64,
        token: u64,
        entropy: u64,
        out: &mut Vec<Request>,
        sum: &mut Option<&mut u64>,
    ) {
        for slot in 0..SAWTOOTH_WIDTH {
            let size = medium_size(mix(entropy ^ slot as u64));
            self.push(
                out,
                sum,
                Request::alloc(
                    0,
                    tx,
                    token + slot as u64,
                    worker,
                    worker,
                    size,
                    mix(entropy ^ slot as u64),
                ),
            );
            self.push(
                out,
                sum,
                Request::touch(
                    0,
                    tx,
                    token + slot as u64,
                    worker,
                    size,
                    mix(entropy ^ slot as u64),
                ),
            );
        }
        // Drain unretained blocks first, then retained blocks in a later phase.
        for slot in 0..SAWTOOTH_WIDTH {
            if !retained(entropy, slot) {
                self.push(
                    out,
                    sum,
                    Request::free(1, tx, token + slot as u64, worker, worker),
                );
            }
        }
        for slot in 0..SAWTOOTH_WIDTH {
            if retained(entropy, slot) {
                self.push(
                    out,
                    sum,
                    Request::free(2, tx, token + slot as u64, worker, worker),
                );
            }
        }
    }

    fn thread_churn(
        &self,
        worker: usize,
        tx: u64,
        token: u64,
        entropy: u64,
        out: &mut Vec<Request>,
        sum: &mut Option<&mut u64>,
    ) {
        for generation in 0..CHURN_GENERATIONS {
            self.push(
                out,
                sum,
                Request {
                    kind: RequestKind::WorkerGeneration,
                    phase: generation,
                    transaction: tx,
                    token: token + generation as u64,
                    owner_worker: worker,
                    executor_worker: worker,
                    size: 0,
                    alignment: 0,
                    touch: generation as u64,
                    preserve_bytes: 0,
                },
            );
            let local_token = token + generation as u64;
            let size = small_size(mix(entropy ^ generation as u64));
            self.push(
                out,
                sum,
                Request::alloc(
                    generation,
                    tx,
                    local_token,
                    worker,
                    worker,
                    size,
                    mix(entropy ^ generation as u64),
                ),
            );
            self.push(
                out,
                sum,
                Request::touch(
                    generation,
                    tx,
                    local_token,
                    worker,
                    size,
                    mix(entropy ^ generation as u64),
                ),
            );
            self.push(
                out,
                sum,
                Request::free(generation + 1, tx, local_token, worker, worker),
            );
        }
    }

    fn representative_mix(
        &self,
        worker: usize,
        operation: u64,
        tx: u64,
        token: u64,
        entropy: u64,
        out: &mut Vec<Request>,
        sum: &mut Option<&mut u64>,
    ) {
        // Checked-in weights: 50% small, 20% medium, 15% realloc, 15% batch.
        match operation % 20 {
            0..=9 => {
                self.simple_alloc_free(worker, tx, token, small_size(entropy), entropy, out, sum)
            }
            10..=13 => {
                self.simple_alloc_free(worker, tx, token, medium_size(entropy), entropy, out, sum)
            }
            14..=16 => self.realloc_geometric(worker, tx, token, entropy, out, sum),
            _ => self.batch(worker, tx, token, entropy, true, out, sum),
        }
    }
}

fn counts_for_operations(card: CardId, operations: u64) -> ExpectedCounts {
    let mut counts = ExpectedCounts::default();
    match card {
        CardId::TinyFixed16
        | CardId::TinyFixed64
        | CardId::SmallLogMixed
        | CardId::MediumLogMixed
        | CardId::LargeObjects
        | CardId::CrossThreadProducerConsumer
        | CardId::RandomOwnership => {
            counts.alloc_calls = operations;
            counts.free_calls = operations;
            counts.touches = operations;
        }
        CardId::BatchLifo | CardId::BatchFifo => {
            counts.alloc_calls = operations * u64::from(BATCH_WIDTH);
            counts.free_calls = counts.alloc_calls;
            counts.touches = counts.alloc_calls;
        }
        CardId::ReallocGeometric => {
            counts.alloc_calls = operations;
            counts.realloc_calls = operations * u64::from(REALLOC_STEPS);
            counts.free_calls = operations;
            counts.touches = operations * (u64::from(REALLOC_STEPS) + 1);
        }
        CardId::CallocZero => {
            counts.calloc_calls = operations;
            counts.free_calls = operations;
            counts.touches = operations;
        }
        CardId::AlignedRange => {
            counts.aligned_alloc_calls = operations;
            counts.free_calls = operations;
            counts.touches = operations;
        }
        CardId::SawtoothRetainDrain => {
            counts.alloc_calls = operations * u64::from(SAWTOOTH_WIDTH);
            counts.free_calls = counts.alloc_calls;
            counts.touches = counts.alloc_calls;
        }
        CardId::ThreadChurn => {
            counts.alloc_calls = operations * u64::from(CHURN_GENERATIONS);
            counts.free_calls = counts.alloc_calls;
            counts.touches = counts.alloc_calls;
            counts.worker_generations = counts.alloc_calls;
        }
        CardId::RepresentativeMix => {
            let cycles = operations / 20;
            counts.alloc_calls = cycles * 65;
            counts.free_calls = cycles * 65;
            counts.realloc_calls = cycles * 18;
            counts.touches = cycles * 83;
            for operation in 0..operations % 20 {
                match operation {
                    0..=13 => {
                        counts.alloc_calls += 1;
                        counts.free_calls += 1;
                        counts.touches += 1;
                    }
                    14..=16 => {
                        counts.alloc_calls += 1;
                        counts.free_calls += 1;
                        counts.realloc_calls += u64::from(REALLOC_STEPS);
                        counts.touches += u64::from(REALLOC_STEPS) + 1;
                    }
                    _ => {
                        counts.alloc_calls += u64::from(BATCH_WIDTH);
                        counts.free_calls += u64::from(BATCH_WIDTH);
                        counts.touches += u64::from(BATCH_WIDTH);
                    }
                }
            }
        }
    }
    counts
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct TopologyOracle {
    pub claims_cross_thread: bool,
    pub allocation_tokens: u64,
    pub cross_thread_frees: u64,
}

impl TopologyOracle {
    pub fn validates(self) -> bool {
        !self.claims_cross_thread
            || (self.allocation_tokens > 0 && self.cross_thread_frees == self.allocation_tokens)
    }
}

const FNV_OFFSET: u64 = 0xcbf2_9ce4_8422_2325;
const FNV_PRIME: u64 = 0x0000_0100_0000_01b3;

fn fnv_u64(mut state: u64, value: u64) -> u64 {
    for byte in value.to_le_bytes() {
        state ^= byte as u64;
        state = state.wrapping_mul(FNV_PRIME);
    }
    state
}

fn checksum_request(mut state: u64, request: &Request) -> u64 {
    state = fnv_u64(state, request.kind as u64);
    state = fnv_u64(state, request.phase as u64);
    state = fnv_u64(state, request.transaction);
    state = fnv_u64(state, request.token);
    state = fnv_u64(state, request.owner_worker as u64);
    state = fnv_u64(state, request.executor_worker as u64);
    state = fnv_u64(state, request.size as u64);
    state = fnv_u64(state, request.alignment as u64);
    state = fnv_u64(state, request.touch);
    fnv_u64(state, request.preserve_bytes as u64)
}

fn mix(mut value: u64) -> u64 {
    value = value.wrapping_add(0x9e37_79b9_7f4a_7c15);
    value = (value ^ (value >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
    value = (value ^ (value >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
    value ^ (value >> 31)
}

// The high-half-biased exponent produces a deterministic log/Pareto-like
// mixture while retaining the advertised inclusive size range.
fn small_size(entropy: u64) -> usize {
    let exponent = ((entropy.trailing_zeros().min(7)) as usize).min(7);
    let base = 8usize << exponent;
    (base + (((entropy >> 16) as usize) & (base - 1)))
        .min(1024)
        .max(8)
}

fn medium_size(entropy: u64) -> usize {
    let exponent = ((entropy.trailing_zeros().min(4)) as usize).min(4);
    let base = 4usize << (10 + exponent);
    (base + (((entropy >> 20) as usize) & (base - 1)))
        .min(64 * 1024)
        .max(4 * 1024)
}

fn large_size(entropy: u64) -> usize {
    let exponent = ((entropy.trailing_zeros().min(4)) as usize).min(4);
    let base = 1usize << (20 + exponent);
    (base + (((entropy >> 24) as usize) & (base - 1)))
        .min(16 * 1024 * 1024)
        .max(1024 * 1024)
}

fn retained(entropy: u64, slot: u32) -> bool {
    // Exactly one third of each burst is retained, independent of allocator.
    (mix(entropy ^ slot as u64) % 3) == 0
}
