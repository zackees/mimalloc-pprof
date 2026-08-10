use std::alloc::{alloc, alloc_zeroed, dealloc, realloc, Layout};
use std::collections::HashMap;
use std::ptr::NonNull;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

use benchmark_suite::execution::{
    execute_cell, execute_child_request, expected_touch_checksum, AllocatorAdapter,
};
use benchmark_suite::model::{
    AllocatorIdentity, BenchmarkChildRequest, RunnerMetadata, ToolchainMetadata,
    CHILD_PROTOCOL_VERSION,
};
use benchmark_suite::scenarios::{cards, CardId, ScenarioCell, Topology};
use benchmark_suite::{CORE_SUITE_VERSION, RAW_SCHEMA_VERSION};

struct TestAdapter {
    layouts: Mutex<HashMap<usize, Layout>>,
    owners: Mutex<HashMap<usize, std::thread::ThreadId>>,
    remote_frees: AtomicU64,
    corrupt_realloc: bool,
}

impl TestAdapter {
    fn new() -> Self {
        Self {
            layouts: Mutex::new(HashMap::new()),
            owners: Mutex::new(HashMap::new()),
            remote_frees: AtomicU64::new(0),
            corrupt_realloc: false,
        }
    }

    fn corrupting_realloc() -> Self {
        Self {
            layouts: Mutex::new(HashMap::new()),
            owners: Mutex::new(HashMap::new()),
            remote_frees: AtomicU64::new(0),
            corrupt_realloc: true,
        }
    }

    fn allocate(&self, layout: Layout, zeroed: bool) -> Result<NonNull<u8>, String> {
        let pointer = unsafe {
            if zeroed {
                alloc_zeroed(layout)
            } else {
                alloc(layout)
            }
        };
        let pointer = NonNull::new(pointer).ok_or_else(|| "test allocation failed".to_string())?;
        self.layouts
            .lock()
            .unwrap()
            .insert(pointer.as_ptr() as usize, layout);
        self.owners
            .lock()
            .unwrap()
            .insert(pointer.as_ptr() as usize, std::thread::current().id());
        Ok(pointer)
    }
}

impl Drop for TestAdapter {
    fn drop(&mut self) {
        assert!(self.layouts.get_mut().unwrap().is_empty());
    }
}

impl AllocatorAdapter for TestAdapter {
    fn allocator_id(&self) -> &str {
        "tcmalloc"
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
        self.allocate(Layout::from_size_align(size, 16).unwrap(), false)
    }
    fn calloc(&self, count: usize, size: usize) -> Result<NonNull<u8>, String> {
        let bytes = count.checked_mul(size).ok_or("calloc overflow")?;
        self.allocate(Layout::from_size_align(bytes, 16).unwrap(), true)
    }
    unsafe fn realloc(&self, pointer: NonNull<u8>, size: usize) -> Result<NonNull<u8>, String> {
        let address = pointer.as_ptr() as usize;
        let old_layout = self
            .layouts
            .lock()
            .unwrap()
            .remove(&address)
            .ok_or("unknown realloc pointer")?;
        let owner = self.owners.lock().unwrap().remove(&address).unwrap();
        let updated = unsafe { realloc(pointer.as_ptr(), old_layout, size) };
        let updated = NonNull::new(updated).ok_or("test realloc failed")?;
        let new_layout = Layout::from_size_align(size, old_layout.align()).unwrap();
        self.layouts
            .lock()
            .unwrap()
            .insert(updated.as_ptr() as usize, new_layout);
        self.owners
            .lock()
            .unwrap()
            .insert(updated.as_ptr() as usize, owner);
        if self.corrupt_realloc {
            unsafe {
                updated
                    .as_ptr()
                    .write(updated.as_ptr().read().wrapping_add(1))
            };
        }
        Ok(updated)
    }
    fn aligned_alloc(&self, alignment: usize, size: usize) -> Result<NonNull<u8>, String> {
        self.allocate(Layout::from_size_align(size, alignment).unwrap(), false)
    }
    unsafe fn free(&self, pointer: NonNull<u8>) {
        let address = pointer.as_ptr() as usize;
        let layout = self.layouts.lock().unwrap().remove(&address).unwrap();
        let owner = self.owners.lock().unwrap().remove(&address).unwrap();
        if owner != std::thread::current().id() {
            self.remote_frees.fetch_add(1, Ordering::SeqCst);
        }
        unsafe { dealloc(pointer.as_ptr(), layout) };
    }
}

const TOPOLOGY: Topology = Topology {
    physical_cores: 2,
    logical_cores: 2,
};

#[test]
fn every_card_executes_exact_allocator_calls_and_touch_checksum() {
    let adapter = TestAdapter::new();
    for definition in cards() {
        let cell =
            ScenarioCell::new(definition.id, definition.thread_points[0], TOPOLOGY, 1, 77).unwrap();
        let result = execute_cell(&adapter, &cell)
            .unwrap_or_else(|error| panic!("{} failed: {error}", definition.id.as_str()));
        assert_eq!(result.counts, cell.expected_counts().unwrap());
        assert_eq!(result.checksum, expected_touch_checksum(&cell).unwrap());
        assert!(result.peak_live_requested_bytes > 0);
        assert!(result.elapsed_ns > 0);
        if definition.id == CardId::ThreadChurn {
            assert_eq!(definition.operation_count(&result.counts), 8);
            assert_eq!(result.counts.requested_transactions, 1);
        }
    }
}

#[test]
fn observed_checksum_composes_across_complete_and_partial_request_cycles() {
    let adapter = TestAdapter::new();
    let cell = ScenarioCell::new(
        CardId::TinyFixed16,
        benchmark_suite::scenarios::ThreadPoint::One,
        TOPOLOGY,
        41,
        0x5eed,
    )
    .unwrap();
    let result = execute_cell(&adapter, &cell).unwrap();
    assert_eq!(result.checksum, expected_touch_checksum(&cell).unwrap());
}

#[test]
fn cross_thread_card_really_frees_on_a_different_native_worker() {
    let adapter = TestAdapter::new();
    let cell = ScenarioCell::new(
        CardId::CrossThreadProducerConsumer,
        benchmark_suite::scenarios::ThreadPoint::Two,
        TOPOLOGY,
        4,
        91,
    )
    .unwrap();
    execute_cell(&adapter, &cell).unwrap();
    assert_eq!(adapter.remote_frees.load(Ordering::SeqCst), 8);
}

#[test]
fn immediate_and_batch_lifetimes_do_not_scale_with_transaction_count() {
    let adapter = TestAdapter::new();
    for point in [
        benchmark_suite::scenarios::ThreadPoint::One,
        benchmark_suite::scenarios::ThreadPoint::PhysicalCores,
    ] {
        let cell = ScenarioCell::new(CardId::TinyFixed16, point, TOPOLOGY, 100, 44).unwrap();
        let result = execute_cell(&adapter, &cell).unwrap();
        assert!(result.peak_live_requested_bytes >= 16);
        assert!(
            result.peak_live_requested_bytes <= cell.threads as u64 * 16,
            "tiny fixed allocations leaked across transactions"
        );
    }

    for card in [CardId::BatchLifo, CardId::BatchFifo] {
        let cell = ScenarioCell::new(
            card,
            benchmark_suite::scenarios::ThreadPoint::One,
            TOPOLOGY,
            100,
            55,
        )
        .unwrap();
        let expected_peak = requested_peak(&cell.worker_stream(0).unwrap().requests);
        let result = execute_cell(&adapter, &cell).unwrap();
        assert_eq!(result.peak_live_requested_bytes, expected_peak);
        assert!(
            result.peak_live_requested_bytes <= 16 * 1024,
            "batch allocations leaked across transaction boundaries"
        );
    }
}

fn requested_peak(requests: &[benchmark_suite::scenarios::Request]) -> u64 {
    use benchmark_suite::scenarios::RequestKind;
    let mut live = HashMap::new();
    let mut bytes = 0_u64;
    let mut peak = 0_u64;
    for request in requests {
        match request.kind {
            RequestKind::Alloc | RequestKind::Calloc | RequestKind::AlignedAlloc => {
                live.insert(request.token, request.size as u64);
                bytes += request.size as u64;
                peak = peak.max(bytes);
            }
            RequestKind::Realloc => {
                let old = live.insert(request.token, request.size as u64).unwrap();
                bytes = bytes - old + request.size as u64;
                peak = peak.max(bytes);
            }
            RequestKind::Free => bytes -= live.remove(&request.token).unwrap(),
            RequestKind::VerifyZero | RequestKind::Touch | RequestKind::WorkerGeneration => {}
        }
    }
    assert_eq!(bytes, 0);
    peak
}

#[test]
fn strict_child_response_is_derived_from_work_not_supplied_by_caller() {
    let adapter = TestAdapter::new();
    let request = BenchmarkChildRequest {
        protocol_version: CHILD_PROTOCOL_VERSION.into(),
        schema_version: RAW_SCHEMA_VERSION.into(),
        suite_version: CORE_SUITE_VERSION.into(),
        run_kind: "headline".into(),
        execution_mode: "normal".into(),
        run_seed: 5,
        block_id: 2,
        ordinal: 0,
        workload_seed: 19,
        allocator: AllocatorIdentity {
            allocator_id: "tcmalloc".into(),
            allocator_version: "test".into(),
            source_sha: "a".repeat(40),
            library_sha256: "b".repeat(64),
            child_binary_sha256: "c".repeat(64),
        },
        scenario_id: "realloc-geometric".into(),
        scenario_version: CORE_SUITE_VERSION.into(),
        thread_point: "1".into(),
        physical_cores: 2,
        logical_cores: 2,
        transactions_per_worker: 2,
        warmup_transactions_per_worker: 1,
        reproduction_command: "MIMALLOC_PROF=0 MIMALLOC_MEMORY_EVENTS=0 benchmark-child".into(),
        runner: RunnerMetadata {
            os: "linux".into(),
            architecture: "x86_64".into(),
            physical_cores: 2,
            logical_cores: 2,
        },
        toolchain: ToolchainMetadata {
            rustc: "rustc test".into(),
            target: "x86_64-unknown-linux-gnu".into(),
            compiler: "cc".into(),
            linker: "cc".into(),
        },
    };
    let response = execute_child_request(&adapter, request.clone()).unwrap();
    response.validate_against(&request).unwrap();
    assert_eq!(response.sample.requested_transactions, 2);
    assert_eq!(response.sample.realloc_calls, 12);
    assert!(response.sample.throughput_operations_per_second.is_finite());

    let mut false_count = response.clone();
    false_count.sample.allocation_calls += 1;
    assert!(false_count.validate_against(&request).is_err());
    let mut false_checksum = response.clone();
    false_checksum.sample.checksum ^= 1;
    assert!(false_checksum.validate_against(&request).is_err());
    for invalid in [0.0, f64::INFINITY, f64::NAN] {
        let mut false_throughput = response.clone();
        false_throughput.sample.throughput_operations_per_second = invalid;
        assert!(false_throughput.validate_against(&request).is_err());
    }
    let mut inconsistent_throughput = response.clone();
    inconsistent_throughput
        .sample
        .throughput_operations_per_second *= 1.01;
    assert!(inconsistent_throughput.validate_against(&request).is_err());

    let encoded = serde_json::to_vec(&response).unwrap();
    let mut extra_response = encoded.clone();
    extra_response.extend_from_slice(b"{}");
    assert!(
        serde_json::from_slice::<benchmark_suite::model::BenchmarkChildResponse>(&extra_response)
            .is_err()
    );
    assert!(
        serde_json::from_slice::<benchmark_suite::model::BenchmarkChildResponse>(b"{").is_err()
    );
    let mut value = serde_json::to_value(&response).unwrap();
    value
        .as_object_mut()
        .unwrap()
        .insert("unexpected".into(), serde_json::Value::Bool(true));
    assert!(
        serde_json::from_value::<benchmark_suite::model::BenchmarkChildResponse>(value).is_err()
    );

    for (scenario_id, specialized_calls) in
        [("calloc-zero", "calloc"), ("aligned-range", "aligned")]
    {
        let mut specialized_request = request.clone();
        specialized_request.scenario_id = scenario_id.into();
        let specialized = execute_child_request(&adapter, specialized_request.clone()).unwrap();
        assert_eq!(specialized.sample.allocation_calls, 0);
        match specialized_calls {
            "calloc" => assert!(specialized.sample.calloc_calls > 0),
            "aligned" => assert!(specialized.sample.aligned_allocation_calls > 0),
            _ => unreachable!(),
        }
        specialized.validate_against(&specialized_request).unwrap();
    }

    let mut churn_request = request;
    churn_request.scenario_id = "thread-churn".into();
    let churn = execute_child_request(&adapter, churn_request.clone()).unwrap();
    assert_eq!(churn.sample.requested_transactions, 2);
    assert_eq!(churn.sample.operation_count, 16);
    churn.validate_against(&churn_request).unwrap();
}

#[test]
fn realloc_preservation_is_checked_after_the_allocator_returns() {
    let adapter = TestAdapter::corrupting_realloc();
    let cell = ScenarioCell::new(
        CardId::ReallocGeometric,
        benchmark_suite::scenarios::ThreadPoint::One,
        TOPOLOGY,
        1,
        7,
    )
    .unwrap();
    let error = execute_cell(&adapter, &cell).unwrap_err();
    assert!(error.contains("preserve"), "unexpected error: {error}");
}
