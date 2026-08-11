//! Allocator-neutral execution of one `core-throughput-v1` child request.

use std::ptr::NonNull;
use std::sync::atomic::{AtomicBool, AtomicPtr, AtomicU64, Ordering};
use std::sync::{Arc, Barrier, Mutex};
use std::time::Instant;

use crate::adapter::LinkedAdapter;
use crate::model::{
    BenchmarkChildRequest, BenchmarkChildResponse, RawSample, CHILD_PROTOCOL_VERSION,
};
use crate::scenarios::{
    card, CardId, ExpectedCounts, Request, RequestKind, ScenarioCell, ThreadPoint, Topology,
    MAX_REQUESTS_PER_TRANSACTION, REQUEST_CYCLE_OPERATIONS,
};

const FNV_OFFSET: u64 = 0xcbf2_9ce4_8422_2325;
const FNV_PRIME: u64 = 0x0000_0100_0000_01b3;
const PAGE_BYTES: usize = 4096;

type MeasuredRegionHook = fn(bool);
static MEASURED_REGION_HOOK: AtomicPtr<()> = AtomicPtr::new(std::ptr::null_mut());

/// Install a process-global, allocation-free timing hook used by the contract
/// test to audit system-allocator calls. Production leaves it unset.
pub fn set_measured_region_hook(hook: Option<MeasuredRegionHook>) {
    let pointer = hook
        .map(|callback| callback as *const () as *mut ())
        .unwrap_or(std::ptr::null_mut());
    MEASURED_REGION_HOOK.store(pointer, Ordering::SeqCst);
}

fn notify_measured_region(active: bool) {
    let pointer = MEASURED_REGION_HOOK.load(Ordering::SeqCst);
    if !pointer.is_null() {
        let callback: MeasuredRegionHook = unsafe { std::mem::transmute(pointer) };
        callback(active);
    }
}

/// Small Rust abstraction matching the native adapter ABI. Tests can provide
/// an instrumented implementation without linking a competitor allocator.
pub trait AllocatorAdapter: Sync {
    fn allocator_id(&self) -> &str;
    fn allocator_version(&self) -> &str;
    fn source_sha(&self) -> &str;
    fn library_sha256(&self) -> &str;
    fn alloc(&self, size: usize) -> Result<NonNull<u8>, String>;
    fn calloc(&self, count: usize, size: usize) -> Result<NonNull<u8>, String>;
    unsafe fn realloc(&self, pointer: NonNull<u8>, size: usize) -> Result<NonNull<u8>, String>;
    fn aligned_alloc(&self, alignment: usize, size: usize) -> Result<NonNull<u8>, String>;
    unsafe fn free(&self, pointer: NonNull<u8>);
}

struct SerializedAdapter<'a, A> {
    inner: &'a A,
    gate: Mutex<()>,
}

impl<A: AllocatorAdapter> AllocatorAdapter for SerializedAdapter<'_, A> {
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
        let _guard = self
            .gate
            .lock()
            .map_err(|_| "serialized control lock poisoned")?;
        self.inner.alloc(size)
    }
    fn calloc(&self, count: usize, size: usize) -> Result<NonNull<u8>, String> {
        let _guard = self
            .gate
            .lock()
            .map_err(|_| "serialized control lock poisoned")?;
        self.inner.calloc(count, size)
    }
    unsafe fn realloc(&self, pointer: NonNull<u8>, size: usize) -> Result<NonNull<u8>, String> {
        let _guard = self
            .gate
            .lock()
            .map_err(|_| "serialized control lock poisoned")?;
        unsafe { self.inner.realloc(pointer, size) }
    }
    fn aligned_alloc(&self, alignment: usize, size: usize) -> Result<NonNull<u8>, String> {
        let _guard = self
            .gate
            .lock()
            .map_err(|_| "serialized control lock poisoned")?;
        self.inner.aligned_alloc(alignment, size)
    }
    unsafe fn free(&self, pointer: NonNull<u8>) {
        let _guard = self.gate.lock().expect("serialized control lock poisoned");
        unsafe { self.inner.free(pointer) }
    }
}

impl AllocatorAdapter for LinkedAdapter {
    fn allocator_id(&self) -> &str {
        self.identity().allocator_id
    }
    fn allocator_version(&self) -> &str {
        self.identity().allocator_version
    }
    fn source_sha(&self) -> &str {
        self.identity().source_sha
    }
    fn library_sha256(&self) -> &str {
        self.identity().library_sha256
    }
    fn alloc(&self, size: usize) -> Result<NonNull<u8>, String> {
        self.alloc(size).map_err(|error| error.to_string())
    }
    fn calloc(&self, count: usize, size: usize) -> Result<NonNull<u8>, String> {
        self.calloc(count, size).map_err(|error| error.to_string())
    }
    unsafe fn realloc(&self, pointer: NonNull<u8>, size: usize) -> Result<NonNull<u8>, String> {
        unsafe { self.realloc(pointer, size) }.map_err(|error| error.to_string())
    }
    fn aligned_alloc(&self, alignment: usize, size: usize) -> Result<NonNull<u8>, String> {
        self.aligned_alloc(alignment, size)
            .map_err(|error| error.to_string())
    }
    unsafe fn free(&self, pointer: NonNull<u8>) {
        unsafe { self.free(pointer) }
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct ExecutionResult {
    pub counts: ExpectedCounts,
    pub checksum: u64,
    pub peak_live_requested_bytes: u64,
    pub setup_ns: u64,
    pub elapsed_ns: u64,
    pub teardown_ns: u64,
}

#[derive(Debug, Clone, Copy)]
struct LiveAllocation {
    pointer: NonNull<u8>,
    size: usize,
    pattern: Option<u64>,
}

type LiveSlots = Vec<(u64, LiveAllocation)>;
type HandoffQueues = Arc<Vec<Mutex<Vec<(Request, LiveAllocation)>>>>;

// The pointer is transferred only after its owner has stopped accessing it and
// before the receiving worker starts its next barrier phase.
unsafe impl Send for LiveAllocation {}

#[derive(Debug)]
struct WorkerOutcome {
    counts: ExpectedCounts,
    checksum: u64,
}

/// Fixed control hooks used by the Linux external-memory sampler. The normal
/// throughput child uses the zero-cost no-op implementation below.
pub trait MeasurementObserver {
    fn baseline_ready_and_wait_for_begin(&mut self) -> Result<(), String>;
    fn workload_active(&mut self) -> Result<(), String>;
    fn workload_drained(&mut self, outcome: &ExecutionResult) -> Result<(), String>;
}

struct NoopObserver;

impl MeasurementObserver for NoopObserver {
    fn baseline_ready_and_wait_for_begin(&mut self) -> Result<(), String> {
        Ok(())
    }

    fn workload_active(&mut self) -> Result<(), String> {
        Ok(())
    }

    fn workload_drained(&mut self, _outcome: &ExecutionResult) -> Result<(), String> {
        Ok(())
    }
}

/// Execute one strict request and construct its raw response. This is the
/// reusable entrypoint used by `benchmark-child` and native wrapper builds.
pub fn execute_child_request<A: AllocatorAdapter>(
    adapter: &A,
    request: BenchmarkChildRequest,
) -> Result<BenchmarkChildResponse, String> {
    execute_child_request_with_observer(adapter, request, &mut NoopObserver)
}

pub fn execute_child_request_with_observer<A: AllocatorAdapter, O: MeasurementObserver>(
    adapter: &A,
    request: BenchmarkChildRequest,
    observer: &mut O,
) -> Result<BenchmarkChildResponse, String> {
    request.validate()?;
    validate_linked_identity(adapter, &request)?;
    let card_id = CardId::parse(&request.scenario_id)
        .ok_or_else(|| format!("unknown scenario {}", request.scenario_id))?;
    let thread_point = ThreadPoint::parse(&request.thread_point)
        .ok_or_else(|| format!("unknown thread point {}", request.thread_point))?;
    let topology = Topology {
        physical_cores: request.physical_cores as usize,
        logical_cores: request.logical_cores as usize,
    };

    let setup_started = Instant::now();
    let measured_cell = ScenarioCell::new(
        card_id,
        thread_point,
        topology,
        request.transactions_per_worker,
        request.workload_seed,
    )
    .map_err(|error| error.to_string())?;
    let expected = measured_cell
        .expected_counts()
        .map_err(|error| error.to_string())?;
    let request_setup_ns = nonzero_ns(setup_started);

    let warmup_started = Instant::now();
    if request.warmup_transactions_per_worker > 0 {
        let warmup = ScenarioCell::new(
            card_id,
            thread_point,
            topology,
            request.warmup_transactions_per_worker,
            request.workload_seed ^ 0xa076_1d64_78bd_642f,
        )
        .map_err(|error| error.to_string())?;
        execute_for_mode(adapter, &warmup, &request.execution_mode, &mut NoopObserver)?;
    }
    let warmup_ns = if request.warmup_transactions_per_worker == 0 {
        0
    } else {
        nonzero_ns(warmup_started)
    };

    let outcome = execute_for_mode(adapter, &measured_cell, &request.execution_mode, observer)?;
    let setup_ns = request_setup_ns.saturating_add(outcome.setup_ns);
    let elapsed_ns = outcome.elapsed_ns;

    let teardown_started = Instant::now();
    if outcome.counts != expected {
        return Err(format!(
            "executed allocator counts differ from scenario contract: {:?} != {:?}",
            outcome.counts, expected
        ));
    }
    if outcome.checksum != expected_touch_checksum(&measured_cell)? {
        return Err("observed touch checksum differs from deterministic workload".into());
    }
    let teardown_ns = outcome
        .teardown_ns
        .saturating_add(nonzero_ns(teardown_started));
    let definition = card(card_id);
    let operation_count = definition.operation_count(&expected);
    let throughput = operation_count as f64 * 1_000_000_000.0 / elapsed_ns as f64;
    if !throughput.is_finite() || throughput <= 0.0 {
        return Err("derived throughput is non-finite or non-positive".into());
    }

    let sample = RawSample {
        schema_version: request.schema_version.clone(),
        suite_version: request.suite_version.clone(),
        run_kind: request.run_kind.clone(),
        execution_mode: request.execution_mode.clone(),
        run_seed: request.run_seed,
        block_id: request.block_id,
        ordinal: request.ordinal,
        workload_seed: request.workload_seed,
        allocator_id: request.allocator.allocator_id.clone(),
        allocator_version: request.allocator.allocator_version.clone(),
        allocator_source_sha: request.allocator.source_sha.clone(),
        allocator_library_sha256: request.allocator.library_sha256.clone(),
        child_binary_sha256: request.allocator.child_binary_sha256.clone(),
        scenario_id: request.scenario_id.clone(),
        scenario_version: request.scenario_version.clone(),
        thread_point: request.thread_point.clone(),
        thread_count: measured_cell.threads as u32,
        operation_unit: definition.operation_unit.name().into(),
        operation_count,
        requested_transactions: expected.requested_transactions,
        completed_transactions: outcome.counts.completed_transactions,
        allocation_calls: outcome.counts.alloc_calls,
        calloc_calls: outcome.counts.calloc_calls,
        aligned_allocation_calls: outcome.counts.aligned_alloc_calls,
        free_calls: outcome.counts.free_calls,
        realloc_calls: outcome.counts.realloc_calls,
        setup_ns,
        warmup_ns,
        elapsed_ns,
        teardown_ns,
        throughput_operations_per_second: throughput,
        checksum: outcome.checksum,
        peak_live_requested_bytes: outcome.peak_live_requested_bytes,
        timed_out: false,
        crashed: false,
        exit_code: 0,
        signal: None,
        runner: request.runner.clone(),
        toolchain: request.toolchain.clone(),
        reproduction_command: request.reproduction_command.clone(),
    };
    let response = BenchmarkChildResponse {
        protocol_version: CHILD_PROTOCOL_VERSION.into(),
        sample,
    };
    response.validate_against(&request)?;
    Ok(response)
}

fn execute_for_mode<A: AllocatorAdapter, O: MeasurementObserver>(
    adapter: &A,
    cell: &ScenarioCell,
    mode: &str,
    observer: &mut O,
) -> Result<ExecutionResult, String> {
    match mode {
        "normal" => execute_cell_with_observer(adapter, cell, observer),
        "serialized-control" => execute_cell_with_observer(
            &SerializedAdapter {
                inner: adapter,
                gate: Mutex::new(()),
            },
            cell,
            observer,
        ),
        _ => Err("unknown benchmark execution mode".into()),
    }
}

fn validate_linked_identity<A: AllocatorAdapter>(
    adapter: &A,
    request: &BenchmarkChildRequest,
) -> Result<(), String> {
    let expected = &request.allocator;
    if adapter.allocator_id() != expected.allocator_id
        || adapter.allocator_version() != expected.allocator_version
        || adapter.source_sha() != expected.source_sha
        || adapter.library_sha256() != expected.library_sha256
    {
        return Err("requested allocator identity does not match the linked adapter".into());
    }
    Ok(())
}

pub fn execute_cell<A: AllocatorAdapter>(
    adapter: &A,
    cell: &ScenarioCell,
) -> Result<ExecutionResult, String> {
    execute_cell_with_observer(adapter, cell, &mut NoopObserver)
}

fn execute_cell_with_observer<A: AllocatorAdapter, O: MeasurementObserver>(
    adapter: &A,
    cell: &ScenarioCell,
    observer: &mut O,
) -> Result<ExecutionResult, String> {
    if cell.card == CardId::ThreadChurn {
        return execute_thread_churn(adapter, cell, observer);
    }
    if matches!(
        cell.card,
        CardId::CrossThreadProducerConsumer | CardId::RandomOwnership
    ) {
        execute_cross_thread_cell(adapter, cell, observer)
    } else {
        execute_ordered_cell(adapter, cell, observer)
    }
}

fn execute_ordered_cell<A: AllocatorAdapter, O: MeasurementObserver>(
    adapter: &A,
    cell: &ScenarioCell,
    observer: &mut O,
) -> Result<ExecutionResult, String> {
    let setup_started = Instant::now();
    let ready_barrier = Arc::new(Barrier::new(cell.threads + 1));
    let start_barrier = Arc::new(Barrier::new(cell.threads + 1));
    let end_barrier = Arc::new(Barrier::new(cell.threads + 1));
    let cancelled = Arc::new(AtomicBool::new(false));
    let live = Arc::new(AtomicU64::new(0));
    let peak = Arc::new(AtomicU64::new(0));

    let (outcomes, setup_ns, elapsed_ns) = std::thread::scope(|scope| {
        let mut handles = Vec::with_capacity(cell.threads);
        for worker in 0..cell.threads {
            let ready_barrier = Arc::clone(&ready_barrier);
            let start_barrier = Arc::clone(&start_barrier);
            let end_barrier = Arc::clone(&end_barrier);
            let cancelled = Arc::clone(&cancelled);
            let live = Arc::clone(&live);
            let peak = Arc::clone(&peak);
            let slot_capacity = card(cell.card).max_live_allocations_per_worker();
            handles.push(scope.spawn(move || {
                let mut allocations = Vec::with_capacity(slot_capacity);
                let mut requests = Vec::with_capacity(MAX_REQUESTS_PER_TRANSACTION);
                let mut counts = ExpectedCounts::default();
                let mut observation_checksum = 0_u64;
                let mut failure: Option<String> = None;
                ready_barrier.wait();
                start_barrier.wait();
                if cancelled.load(Ordering::SeqCst) {
                    end_barrier.wait();
                    return Ok(WorkerOutcome {
                        counts,
                        checksum: observation_checksum,
                    });
                }
                for operation in 0..cell.transactions_per_worker {
                    if let Err(error) =
                        cell.fill_worker_transaction(worker, operation, &mut requests)
                    {
                        failure = Some(error.to_string());
                        break;
                    }
                    for request in &requests {
                        if let Err(error) = execute_one(
                            adapter,
                            cell.card,
                            request,
                            &mut allocations,
                            &mut counts,
                            &mut observation_checksum,
                            &live,
                            &peak,
                        ) {
                            failure = Some(error);
                            break;
                        }
                    }
                    if failure.is_some() {
                        break;
                    }
                }
                std::hint::black_box(observation_checksum);
                end_barrier.wait();
                for (_, allocation) in allocations {
                    unsafe { adapter.free(allocation.pointer) };
                    subtract_live(&live, allocation.size as u64);
                }
                match failure {
                    Some(error) => Err(error),
                    None => Ok(WorkerOutcome {
                        counts,
                        checksum: observation_checksum,
                    }),
                }
            }));
        }
        // Separate ready/start gates ensure thread creation and per-worker
        // state construction are setup, not measured work.
        ready_barrier.wait();
        let setup_ns = nonzero_ns(setup_started);
        if let Err(error) = observer
            .baseline_ready_and_wait_for_begin()
            .and_then(|()| observer.workload_active())
        {
            cancelled.store(true, Ordering::SeqCst);
            start_barrier.wait();
            end_barrier.wait();
            return Err(error);
        }
        notify_measured_region(true);
        let measured_started = Instant::now();
        start_barrier.wait();
        end_barrier.wait();
        let elapsed_ns = nonzero_ns(measured_started);
        notify_measured_region(false);
        let outcomes = handles
            .into_iter()
            .map(|handle| {
                handle
                    .join()
                    .map_err(|_| "benchmark worker panicked".to_string())?
            })
            .collect::<Result<Vec<_>, String>>()?;
        Ok::<_, String>((outcomes, setup_ns, elapsed_ns))
    })?;
    let teardown_started = Instant::now();
    let outcome = finish_execution(
        cell,
        outcomes,
        &live,
        &peak,
        setup_ns,
        elapsed_ns,
        nonzero_ns(teardown_started),
    )?;
    observer.workload_drained(&outcome)?;
    Ok(outcome)
}

fn execute_cross_thread_cell<A: AllocatorAdapter, O: MeasurementObserver>(
    adapter: &A,
    cell: &ScenarioCell,
    observer: &mut O,
) -> Result<ExecutionResult, String> {
    let setup_started = Instant::now();
    let ready_barrier = Arc::new(Barrier::new(cell.threads + 1));
    let start_barrier = Arc::new(Barrier::new(cell.threads + 1));
    let end_barrier = Arc::new(Barrier::new(cell.threads + 1));
    let cancelled = Arc::new(AtomicBool::new(false));
    let operation_barrier = Arc::new(Barrier::new(cell.threads));
    let live = Arc::new(AtomicU64::new(0));
    let peak = Arc::new(AtomicU64::new(0));
    let handoffs: HandoffQueues = Arc::new(
        (0..cell.threads)
            .map(|_| Mutex::new(Vec::with_capacity(1)))
            .collect::<Vec<_>>(),
    );

    let (outcomes, setup_ns, elapsed_ns) = std::thread::scope(|scope| {
        let mut handles = Vec::with_capacity(cell.threads);
        for worker in 0..cell.threads {
            let ready_barrier = Arc::clone(&ready_barrier);
            let start_barrier = Arc::clone(&start_barrier);
            let end_barrier = Arc::clone(&end_barrier);
            let cancelled = Arc::clone(&cancelled);
            let operation_barrier = Arc::clone(&operation_barrier);
            let live = Arc::clone(&live);
            let peak = Arc::clone(&peak);
            let handoffs = Arc::clone(&handoffs);
            let slot_capacity = card(cell.card).max_live_allocations_per_worker();
            let transactions_per_worker = cell.transactions_per_worker;
            handles.push(scope.spawn(move || {
                let mut allocations = Vec::with_capacity(slot_capacity);
                let mut requests = Vec::with_capacity(MAX_REQUESTS_PER_TRANSACTION);
                let mut counts = ExpectedCounts::default();
                let mut observation_checksum = 0_u64;
                let mut failure: Option<String> = None;
                ready_barrier.wait();
                start_barrier.wait();
                if cancelled.load(Ordering::SeqCst) {
                    end_barrier.wait();
                    return Ok(WorkerOutcome {
                        counts,
                        checksum: observation_checksum,
                    });
                }
                for operation in 0..transactions_per_worker {
                    if failure.is_none() {
                        if let Err(error) =
                            cell.fill_worker_transaction(worker, operation, &mut requests)
                        {
                            failure = Some(error.to_string());
                        }
                    }

                    let mut transfer = None;
                    if failure.is_none() {
                        for request in &requests {
                            if request.kind == RequestKind::Free
                                && request.owner_worker != request.executor_worker
                            {
                                if transfer.replace(*request).is_some() {
                                    failure = Some("operation has multiple remote frees".into());
                                    break;
                                }
                                continue;
                            }
                            if let Err(error) = execute_one(
                                adapter,
                                cell.card,
                                request,
                                &mut allocations,
                                &mut counts,
                                &mut observation_checksum,
                                &live,
                                &peak,
                            ) {
                                failure = Some(error);
                                break;
                            }
                        }
                    }
                    if failure.is_none() {
                        match transfer {
                            Some(request) => {
                                let destination = request.executor_worker;
                                match remove_slot(&mut allocations, request.token) {
                                    Some(allocation) => handoffs[destination]
                                        .lock()
                                        .expect("handoff queue lock poisoned")
                                        .push((request, allocation)),
                                    None => {
                                        failure = Some(format!(
                                            "remote transfer token {} was not live",
                                            request.token
                                        ))
                                    }
                                }
                            }
                            None => failure = Some("operation is missing its remote free".into()),
                        }
                    }
                    operation_barrier.wait();
                    {
                        let mut inbox = handoffs[worker]
                            .lock()
                            .expect("handoff queue lock poisoned");
                        for (request, allocation) in inbox.drain(..) {
                            if failure.is_none() {
                                if allocations.len() == allocations.capacity() {
                                    failure =
                                        Some("cross-thread live-slot capacity exhausted".into());
                                    unsafe { adapter.free(allocation.pointer) };
                                    subtract_live(&live, allocation.size as u64);
                                } else {
                                    allocations.push((request.token, allocation));
                                    if let Err(error) = execute_one(
                                        adapter,
                                        cell.card,
                                        &request,
                                        &mut allocations,
                                        &mut counts,
                                        &mut observation_checksum,
                                        &live,
                                        &peak,
                                    ) {
                                        failure = Some(error);
                                    }
                                }
                            } else {
                                unsafe { adapter.free(allocation.pointer) };
                                subtract_live(&live, allocation.size as u64);
                            }
                        }
                    }
                    operation_barrier.wait();
                }
                std::hint::black_box(observation_checksum);
                end_barrier.wait();
                for (_, allocation) in allocations {
                    unsafe { adapter.free(allocation.pointer) };
                    subtract_live(&live, allocation.size as u64);
                }
                match failure {
                    Some(error) => Err(error),
                    None => Ok(WorkerOutcome {
                        counts,
                        checksum: observation_checksum,
                    }),
                }
            }));
        }
        ready_barrier.wait();
        let setup_ns = nonzero_ns(setup_started);
        if let Err(error) = observer
            .baseline_ready_and_wait_for_begin()
            .and_then(|()| observer.workload_active())
        {
            cancelled.store(true, Ordering::SeqCst);
            start_barrier.wait();
            end_barrier.wait();
            return Err(error);
        }
        notify_measured_region(true);
        let measured_started = Instant::now();
        start_barrier.wait();
        end_barrier.wait();
        let elapsed_ns = nonzero_ns(measured_started);
        notify_measured_region(false);
        let outcomes = handles
            .into_iter()
            .map(|handle| {
                handle
                    .join()
                    .map_err(|_| "benchmark worker panicked".to_string())?
            })
            .collect::<Result<Vec<_>, String>>()?;
        Ok::<_, String>((outcomes, setup_ns, elapsed_ns))
    })?;
    let teardown_started = Instant::now();
    let outcome = finish_execution(
        cell,
        outcomes,
        &live,
        &peak,
        setup_ns,
        elapsed_ns,
        nonzero_ns(teardown_started),
    )?;
    observer.workload_drained(&outcome)?;
    Ok(outcome)
}

fn execute_thread_churn<A: AllocatorAdapter, O: MeasurementObserver>(
    adapter: &A,
    cell: &ScenarioCell,
    observer: &mut O,
) -> Result<ExecutionResult, String> {
    let setup_started = Instant::now();
    let mut request_buffers = (0..cell.threads)
        .map(|_| Vec::with_capacity(MAX_REQUESTS_PER_TRANSACTION))
        .collect::<Vec<_>>();
    let live = Arc::new(AtomicU64::new(0));
    let peak = Arc::new(AtomicU64::new(0));
    let mut totals = (0..cell.threads)
        .map(|_| WorkerOutcome {
            counts: ExpectedCounts::default(),
            checksum: 0,
        })
        .collect::<Vec<_>>();

    let setup_ns = nonzero_ns(setup_started);
    // Spawning each generation is part of this card's declared workload.
    observer.baseline_ready_and_wait_for_begin()?;
    observer.workload_active()?;
    let measured_started = Instant::now();
    for operation in 0..cell.transactions_per_worker {
        for (worker, requests) in request_buffers.iter_mut().enumerate() {
            cell.fill_worker_transaction(worker, operation, requests)
                .map_err(|error| error.to_string())?;
        }
        for generation in 0..8_u32 {
            let outcomes = std::thread::scope(|scope| {
                let mut handles = Vec::with_capacity(cell.threads);
                for requests in &request_buffers {
                    let token_remainder = 1 + generation as u64;
                    let live = Arc::clone(&live);
                    let peak = Arc::clone(&peak);
                    handles.push(scope.spawn(move || {
                        let mut allocations = Vec::with_capacity(1);
                        let mut counts = ExpectedCounts::default();
                        let mut checksum = 0_u64;
                        for request in requests.iter().filter(|request| {
                            (request.kind == RequestKind::WorkerGeneration
                                && request.phase == generation)
                                || (request.kind != RequestKind::WorkerGeneration
                                    && request.token % 64 == token_remainder)
                        }) {
                            execute_one(
                                adapter,
                                cell.card,
                                request,
                                &mut allocations,
                                &mut counts,
                                &mut checksum,
                                &live,
                                &peak,
                            )?;
                        }
                        if !allocations.is_empty() {
                            return Err("thread-churn generation leaked an allocation".into());
                        }
                        Ok(WorkerOutcome { counts, checksum })
                    }));
                }
                handles
                    .into_iter()
                    .map(|handle| {
                        handle
                            .join()
                            .map_err(|_| "thread-churn worker panicked".to_string())?
                    })
                    .collect::<Result<Vec<_>, String>>()
            })?;
            for (total, outcome) in totals.iter_mut().zip(outcomes) {
                add_counts(&mut total.counts, outcome.counts);
                total.checksum = total.checksum.wrapping_add(outcome.checksum);
            }
        }
    }
    for total in &totals {
        std::hint::black_box(total.checksum);
    }
    let elapsed_ns = nonzero_ns(measured_started);
    let teardown_started = Instant::now();
    let outcome = finish_execution(
        cell,
        totals,
        &live,
        &peak,
        setup_ns,
        elapsed_ns,
        nonzero_ns(teardown_started),
    )?;
    observer.workload_drained(&outcome)?;
    Ok(outcome)
}

fn finish_execution(
    cell: &ScenarioCell,
    outcomes: Vec<WorkerOutcome>,
    live: &AtomicU64,
    peak: &AtomicU64,
    setup_ns: u64,
    elapsed_ns: u64,
    teardown_ns: u64,
) -> Result<ExecutionResult, String> {
    if live.load(Ordering::SeqCst) != 0 {
        return Err("workload finished with live requested bytes".into());
    }
    let mut counts = ExpectedCounts {
        requested_transactions: cell.requested_transactions(),
        completed_transactions: cell.requested_transactions(),
        ..ExpectedCounts::default()
    };
    let mut checksum = FNV_OFFSET;
    for (worker, outcome) in outcomes.into_iter().enumerate() {
        add_counts(&mut counts, outcome.counts);
        checksum = fnv_u64(checksum, worker as u64);
        checksum = fnv_u64(checksum, outcome.checksum);
    }
    Ok(ExecutionResult {
        counts,
        checksum,
        peak_live_requested_bytes: peak.load(Ordering::SeqCst),
        setup_ns,
        elapsed_ns,
        teardown_ns,
    })
}

fn execute_one<A: AllocatorAdapter>(
    adapter: &A,
    card_id: CardId,
    request: &Request,
    allocations: &mut LiveSlots,
    counts: &mut ExpectedCounts,
    checksum: &mut u64,
    live: &AtomicU64,
    peak: &AtomicU64,
) -> Result<(), String> {
    match request.kind {
        RequestKind::Alloc => {
            let pointer = adapter.alloc(request.size)?;
            insert_live(
                allocations,
                request.token,
                pointer,
                request.size,
                live,
                peak,
            )?;
        }
        RequestKind::Calloc => {
            let pointer = adapter.calloc(1, request.size)?;
            insert_live(
                allocations,
                request.token,
                pointer,
                request.size,
                live,
                peak,
            )?;
        }
        RequestKind::AlignedAlloc => {
            let pointer = adapter.aligned_alloc(request.alignment, request.size)?;
            if pointer.as_ptr() as usize % request.alignment != 0 {
                unsafe { adapter.free(pointer) };
                return Err("adapter returned a misaligned allocation".into());
            }
            insert_live(
                allocations,
                request.token,
                pointer,
                request.size,
                live,
                peak,
            )?;
        }
        RequestKind::Realloc => {
            let allocation = find_slot_mut(allocations, request.token)
                .ok_or_else(|| format!("realloc token {} was not live", request.token))?;
            let preserved_pattern = allocation.pattern;
            let old_size = allocation.size;
            let new_pointer = unsafe { adapter.realloc(allocation.pointer, request.size) }?;
            allocation.pointer = new_pointer;
            allocation.size = request.size;
            if let Some(pattern) = preserved_pattern {
                verify_pattern(*allocation, pattern, request.preserve_bytes)?;
            }
            if request.size >= old_size {
                add_live(live, peak, (request.size - old_size) as u64);
            } else {
                subtract_live(live, (old_size - request.size) as u64);
            }
        }
        RequestKind::Free => {
            let allocation = remove_slot(allocations, request.token)
                .ok_or_else(|| format!("free token {} was not live", request.token))?;
            unsafe { adapter.free(allocation.pointer) };
            subtract_live(live, allocation.size as u64);
        }
        RequestKind::VerifyZero => {
            let allocation = find_slot(allocations, request.token)
                .ok_or_else(|| format!("zero-check token {} was not live", request.token))?;
            for offset in 0..request.size {
                let value = unsafe { allocation.pointer.as_ptr().add(offset).read_volatile() };
                if value != 0 {
                    return Err(format!("calloc byte {offset} was nonzero"));
                }
                *checksum = checksum_byte(*checksum, request.token, offset, value);
            }
        }
        RequestKind::Touch => {
            let allocation = find_slot_mut(allocations, request.token)
                .ok_or_else(|| format!("touch token {} was not live", request.token))?;
            if request.size > allocation.size {
                return Err("touch exceeds requested allocation size".into());
            }
            for offset in touched_offsets_for_card(card_id, request.size) {
                let value = pattern_byte(request.touch, offset);
                unsafe {
                    allocation
                        .pointer
                        .as_ptr()
                        .add(offset)
                        .write_volatile(value);
                    let observed = allocation.pointer.as_ptr().add(offset).read_volatile();
                    if observed != value {
                        return Err(format!("touch verification failed at byte {offset}"));
                    }
                    *checksum = checksum_byte(*checksum, request.token, offset, observed);
                }
            }
            allocation.pattern = Some(request.touch);
        }
        RequestKind::WorkerGeneration => {}
    }
    counts.record(request);
    Ok(())
}

fn insert_live(
    allocations: &mut LiveSlots,
    token: u64,
    pointer: NonNull<u8>,
    size: usize,
    live: &AtomicU64,
    peak: &AtomicU64,
) -> Result<(), String> {
    if find_slot(allocations, token).is_some() {
        return Err(format!("allocation token {token} was reused while live"));
    }
    if allocations.len() == allocations.capacity() {
        return Err("preallocated live-slot capacity was exhausted".into());
    }
    allocations.push((
        token,
        LiveAllocation {
            pointer,
            size,
            pattern: None,
        },
    ));
    add_live(live, peak, size as u64);
    Ok(())
}

fn find_slot(allocations: &LiveSlots, token: u64) -> Option<&LiveAllocation> {
    allocations
        .iter()
        .find(|(candidate, _)| *candidate == token)
        .map(|(_, allocation)| allocation)
}

fn find_slot_mut(allocations: &mut LiveSlots, token: u64) -> Option<&mut LiveAllocation> {
    allocations
        .iter_mut()
        .find(|(candidate, _)| *candidate == token)
        .map(|(_, allocation)| allocation)
}

fn remove_slot(allocations: &mut LiveSlots, token: u64) -> Option<LiveAllocation> {
    allocations
        .iter()
        .position(|(candidate, _)| *candidate == token)
        .map(|position| allocations.swap_remove(position).1)
}

fn add_live(live: &AtomicU64, peak: &AtomicU64, bytes: u64) {
    let current = live
        .fetch_add(bytes, Ordering::SeqCst)
        .saturating_add(bytes);
    peak.fetch_max(current, Ordering::SeqCst);
}

fn subtract_live(live: &AtomicU64, bytes: u64) {
    live.fetch_sub(bytes, Ordering::SeqCst);
}

fn verify_pattern(allocation: LiveAllocation, pattern: u64, preserve: usize) -> Result<(), String> {
    for offset in touched_offsets(preserve.min(allocation.size)) {
        let expected = pattern_byte(pattern, offset);
        let observed = unsafe { allocation.pointer.as_ptr().add(offset).read_volatile() };
        if observed != expected {
            return Err(format!("realloc failed to preserve byte {offset}"));
        }
    }
    Ok(())
}

pub fn expected_touch_checksum(cell: &ScenarioCell) -> Result<u64, String> {
    let mut checksum = FNV_OFFSET;
    for worker in 0..cell.threads {
        checksum = fnv_u64(checksum, worker as u64);
        checksum = fnv_u64(checksum, expected_worker_touch_checksum(cell, worker)?);
    }
    Ok(checksum)
}

fn expected_worker_touch_checksum(cell: &ScenarioCell, worker: usize) -> Result<u64, String> {
    let full_cycles = cell.transactions_per_worker / REQUEST_CYCLE_OPERATIONS;
    let remainder = cell.transactions_per_worker % REQUEST_CYCLE_OPERATIONS;
    let generated_operations = if full_cycles == 0 {
        remainder
    } else {
        REQUEST_CYCLE_OPERATIONS
    };
    let mut requests = Vec::with_capacity(MAX_REQUESTS_PER_TRANSACTION);
    let mut cycle_checksum = 0_u64;
    let mut remainder_checksum = 0_u64;
    for operation in 0..generated_operations {
        cell.fill_worker_transaction(worker, operation, &mut requests)
            .map_err(|error| error.to_string())?;
        let transaction_checksum = expected_requests_touch_checksum(cell.card, &requests);
        cycle_checksum = cycle_checksum.wrapping_add(transaction_checksum);
        if operation < remainder {
            remainder_checksum = remainder_checksum.wrapping_add(transaction_checksum);
        }
    }
    Ok(cycle_checksum
        .wrapping_mul(full_cycles)
        .wrapping_add(remainder_checksum))
}

fn expected_requests_touch_checksum(card_id: CardId, requests: &[Request]) -> u64 {
    let mut checksum = 0_u64;
    for request in requests {
        match request.kind {
            RequestKind::VerifyZero => {
                for offset in 0..request.size {
                    checksum = checksum_byte(checksum, request.token, offset, 0);
                }
            }
            RequestKind::Touch => {
                for offset in touched_offsets_for_card(card_id, request.size) {
                    checksum = checksum_byte(
                        checksum,
                        request.token,
                        offset,
                        pattern_byte(request.touch, offset),
                    );
                }
            }
            _ => {}
        }
    }
    checksum
}

fn touched_offsets(size: usize) -> TouchedOffsets {
    let stride = if size < 1024 * 1024 { 1 } else { PAGE_BYTES };
    touched_offsets_with_stride(size, stride)
}

fn touched_offsets_for_card(card_id: CardId, size: usize) -> TouchedOffsets {
    if card_id == CardId::LargeObjects {
        touched_offsets_with_stride(size, 64)
    } else {
        touched_offsets(size)
    }
}

fn touched_offsets_with_stride(size: usize, stride: usize) -> TouchedOffsets {
    TouchedOffsets {
        size,
        next: 0,
        stride,
        emitted_last: false,
    }
}

struct TouchedOffsets {
    size: usize,
    next: usize,
    stride: usize,
    emitted_last: bool,
}

impl Iterator for TouchedOffsets {
    type Item = usize;

    fn next(&mut self) -> Option<Self::Item> {
        if self.size == 0 {
            return None;
        }
        if self.next < self.size {
            let current = self.next;
            self.next = self.next.saturating_add(self.stride);
            if current == self.size - 1 {
                self.emitted_last = true;
            }
            return Some(current);
        }
        if !self.emitted_last {
            self.emitted_last = true;
            return Some(self.size - 1);
        }
        None
    }
}

fn pattern_byte(seed: u64, offset: usize) -> u8 {
    let mixed = seed
        .wrapping_add((offset as u64).wrapping_mul(0x9e37_79b9_7f4a_7c15))
        .rotate_left((offset & 63) as u32);
    (mixed ^ (mixed >> 17) ^ (mixed >> 41)) as u8
}

fn checksum_byte(checksum: u64, token: u64, offset: usize, value: u8) -> u64 {
    // Each event is hashed independently and then accumulated commutatively.
    // The token's low six bits identify its transaction-local slot, allowing
    // a full deterministic request cycle to be composed arithmetically while
    // the `value` remains the byte actually read from allocator-owned memory.
    let mut event = fnv_u64(FNV_OFFSET, token % 64);
    event = fnv_u64(event, offset as u64);
    event = fnv_u64(event, value as u64);
    checksum.wrapping_add(event)
}

fn fnv_u64(mut state: u64, value: u64) -> u64 {
    for byte in value.to_le_bytes() {
        state ^= byte as u64;
        state = state.wrapping_mul(FNV_PRIME);
    }
    state
}

fn add_counts(total: &mut ExpectedCounts, increment: ExpectedCounts) {
    total.allocator_calls += increment.allocator_calls;
    total.alloc_calls += increment.alloc_calls;
    total.calloc_calls += increment.calloc_calls;
    total.realloc_calls += increment.realloc_calls;
    total.aligned_alloc_calls += increment.aligned_alloc_calls;
    total.free_calls += increment.free_calls;
    total.touches += increment.touches;
    total.worker_generations += increment.worker_generations;
}

fn nonzero_ns(started: Instant) -> u64 {
    u64::try_from(started.elapsed().as_nanos())
        .unwrap_or(u64::MAX)
        .max(1)
}

#[cfg(test)]
mod tests {
    use super::*;

    struct PanicAdapter;

    impl AllocatorAdapter for PanicAdapter {
        fn allocator_id(&self) -> &str {
            "panic-adapter"
        }

        fn allocator_version(&self) -> &str {
            "test"
        }

        fn source_sha(&self) -> &str {
            "0000000000000000000000000000000000000000"
        }

        fn library_sha256(&self) -> &str {
            "0000000000000000000000000000000000000000000000000000000000000000"
        }

        fn alloc(&self, _size: usize) -> Result<NonNull<u8>, String> {
            panic!("cancelled workers must not allocate")
        }

        fn calloc(&self, _count: usize, _size: usize) -> Result<NonNull<u8>, String> {
            panic!("cancelled workers must not allocate")
        }

        unsafe fn realloc(
            &self,
            _pointer: NonNull<u8>,
            _size: usize,
        ) -> Result<NonNull<u8>, String> {
            panic!("cancelled workers must not reallocate")
        }

        fn aligned_alloc(&self, _alignment: usize, _size: usize) -> Result<NonNull<u8>, String> {
            panic!("cancelled workers must not allocate")
        }

        unsafe fn free(&self, _pointer: NonNull<u8>) {
            panic!("cancelled workers must not free")
        }
    }

    struct FailingObserver;

    impl MeasurementObserver for FailingObserver {
        fn baseline_ready_and_wait_for_begin(&mut self) -> Result<(), String> {
            Err("planted observer failure".into())
        }

        fn workload_active(&mut self) -> Result<(), String> {
            panic!("active must not follow a failed baseline callback")
        }

        fn workload_drained(&mut self, _outcome: &ExecutionResult) -> Result<(), String> {
            panic!("drained must not follow a failed baseline callback")
        }
    }

    #[test]
    fn observer_failure_releases_ordered_and_cross_thread_workers() {
        for card in [CardId::LargeObjects, CardId::CrossThreadProducerConsumer] {
            let (sender, receiver) = std::sync::mpsc::channel();
            let handle = std::thread::spawn(move || {
                let point = if card == CardId::LargeObjects {
                    ThreadPoint::One
                } else {
                    ThreadPoint::PhysicalCores
                };
                let cell = ScenarioCell::new(
                    card,
                    point,
                    Topology {
                        physical_cores: 2,
                        logical_cores: 2,
                    },
                    1,
                    1,
                )
                .unwrap();
                let result = execute_cell_with_observer(&PanicAdapter, &cell, &mut FailingObserver);
                sender.send(result).unwrap();
            });
            let result = receiver
                .recv_timeout(std::time::Duration::from_secs(2))
                .expect("observer failure must not strand workers at a barrier");
            assert_eq!(result.unwrap_err(), "planted observer failure");
            handle.join().unwrap();
        }
    }

    #[test]
    fn large_objects_touch_every_cache_line_and_include_the_final_byte() {
        let size = 1024 * 1024 + 17;
        let large_offsets =
            touched_offsets_for_card(CardId::LargeObjects, size).collect::<Vec<_>>();
        assert_eq!(&large_offsets[..4], &[0, 64, 128, 192]);
        assert_eq!(large_offsets.last(), Some(&(size - 1)));
        assert!(large_offsets.windows(2).all(|pair| pair[1] - pair[0] <= 64));
        for page in (0..size).step_by(PAGE_BYTES) {
            assert!(large_offsets.contains(&page));
        }

        let legacy_offsets =
            touched_offsets_for_card(CardId::MediumLogMixed, size).collect::<Vec<_>>();
        assert_eq!(&legacy_offsets[..3], &[0, PAGE_BYTES, PAGE_BYTES * 2]);
        assert!(legacy_offsets.len() < large_offsets.len());

        let request = Request {
            kind: RequestKind::Touch,
            phase: 0,
            transaction: 0,
            token: 1,
            owner_worker: 0,
            executor_worker: 0,
            size,
            alignment: 0,
            touch: 0x5eed,
            preserve_bytes: 0,
        };
        let expected = large_offsets.into_iter().fold(0_u64, |checksum, offset| {
            checksum_byte(
                checksum,
                request.token,
                offset,
                pattern_byte(request.touch, offset),
            )
        });
        assert_eq!(
            expected_requests_touch_checksum(CardId::LargeObjects, &[request]),
            expected
        );
        assert_ne!(
            expected_requests_touch_checksum(CardId::MediumLogMixed, &[request]),
            expected
        );
    }
}
