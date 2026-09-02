use std::alloc::{GlobalAlloc, Layout, System};
use std::cell::Cell;
use std::collections::HashMap;
use std::ptr::NonNull;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Mutex;

use benchmark_suite::execution::{
    execute_cell, execute_latency_cell, set_measured_region_hook, AllocatorAdapter,
};
use benchmark_suite::scenarios::{cards, CardId, ScenarioCell, Topology};

static MEASURED: AtomicBool = AtomicBool::new(false);
static HARNESS_ALLOCATIONS: AtomicU64 = AtomicU64::new(0);
// Size in bytes of the first allocation that tripped `HARNESS_ALLOCATIONS` in
// the current measured interval, `u64::MAX` when none has. Recorded so a
// failure names *something* about the culprit without the audit hook itself
// allocating (a `Backtrace` capture here would recurse into this allocator).
static FIRST_OFFENDING_SIZE: AtomicU64 = AtomicU64::new(u64::MAX);

thread_local! {
    static INSIDE_ADAPTER: Cell<bool> = const { Cell::new(false) };
}

struct AuditedSystem;

impl AuditedSystem {
    fn record(&self, layout: Layout) {
        if MEASURED.load(Ordering::SeqCst) && !INSIDE_ADAPTER.with(Cell::get) {
            let previous = HARNESS_ALLOCATIONS.fetch_add(1, Ordering::SeqCst);
            if previous == 0 {
                FIRST_OFFENDING_SIZE.store(layout.size() as u64, Ordering::SeqCst);
            }
        }
    }
}

unsafe impl GlobalAlloc for AuditedSystem {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        self.record(layout);
        unsafe { System.alloc(layout) }
    }

    unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
        self.record(layout);
        unsafe { System.alloc_zeroed(layout) }
    }

    unsafe fn realloc(&self, pointer: *mut u8, layout: Layout, size: usize) -> *mut u8 {
        self.record(layout);
        unsafe { System.realloc(pointer, layout, size) }
    }

    unsafe fn dealloc(&self, pointer: *mut u8, layout: Layout) {
        unsafe { System.dealloc(pointer, layout) }
    }
}

#[global_allocator]
static GLOBAL: AuditedSystem = AuditedSystem;

fn timing_hook(active: bool) {
    MEASURED.store(active, Ordering::SeqCst);
}

struct InstrumentedAdapter {
    layouts: Mutex<HashMap<usize, Layout>>,
}

impl InstrumentedAdapter {
    fn allocate(&self, layout: Layout, zeroed: bool) -> Result<NonNull<u8>, String> {
        INSIDE_ADAPTER.set(true);
        let pointer = unsafe {
            if zeroed {
                std::alloc::alloc_zeroed(layout)
            } else {
                std::alloc::alloc(layout)
            }
        };
        let pointer = NonNull::new(pointer).ok_or_else(|| "audit allocation failed".to_string())?;
        self.layouts
            .lock()
            .unwrap()
            .insert(pointer.as_ptr() as usize, layout);
        INSIDE_ADAPTER.set(false);
        Ok(pointer)
    }
}

impl AllocatorAdapter for InstrumentedAdapter {
    fn allocator_id(&self) -> &str {
        "audit"
    }
    fn allocator_version(&self) -> &str {
        "audit"
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
        self.allocate(
            Layout::from_size_align(count.checked_mul(size).ok_or("overflow")?, 16).unwrap(),
            true,
        )
    }
    unsafe fn realloc(&self, pointer: NonNull<u8>, size: usize) -> Result<NonNull<u8>, String> {
        INSIDE_ADAPTER.set(true);
        let old = self
            .layouts
            .lock()
            .unwrap()
            .remove(&(pointer.as_ptr() as usize))
            .ok_or("unknown pointer")?;
        let updated = NonNull::new(unsafe { std::alloc::realloc(pointer.as_ptr(), old, size) })
            .ok_or("audit realloc failed")?;
        self.layouts.lock().unwrap().insert(
            updated.as_ptr() as usize,
            Layout::from_size_align(size, old.align()).unwrap(),
        );
        INSIDE_ADAPTER.set(false);
        Ok(updated)
    }
    fn aligned_alloc(&self, alignment: usize, size: usize) -> Result<NonNull<u8>, String> {
        self.allocate(Layout::from_size_align(size, alignment).unwrap(), false)
    }
    unsafe fn free(&self, pointer: NonNull<u8>) {
        INSIDE_ADAPTER.set(true);
        let layout = self
            .layouts
            .lock()
            .unwrap()
            .remove(&(pointer.as_ptr() as usize))
            .unwrap();
        unsafe { std::alloc::dealloc(pointer.as_ptr(), layout) };
        INSIDE_ADAPTER.set(false);
    }
}

fn ordinary_measured_region_performs_zero_harness_allocations() {
    if cfg!(target_os = "macos") {
        println!(
            "test ordinary_measured_region_performs_zero_harness_allocations ... skipped (macOS realloc may route through the global allocator)"
        );
        return;
    }
    let adapter = InstrumentedAdapter {
        layouts: Mutex::new(HashMap::new()),
    };
    let topology = Topology {
        physical_cores: 2,
        logical_cores: 2,
    };
    let mut audited_cards = 0;
    for definition in cards()
        .iter()
        .filter(|definition| definition.id != CardId::ThreadChurn)
    {
        audited_cards += 1;
        for &thread_point in definition.thread_points {
            let transactions = if definition.id == CardId::RepresentativeMix {
                20
            } else {
                2
            };
            let cell =
                ScenarioCell::new(definition.id, thread_point, topology, transactions, 0x5eed)
                    .unwrap();
            audit_cell(&adapter, &cell);
        }
    }
    assert_eq!(audited_cards, 14);
    assert_eq!(
        cards()
            .iter()
            .filter(|definition| definition.id == CardId::ThreadChurn)
            .count(),
        1,
        "native thread creation is the sole explicit timed-allocation exemption"
    );
    println!("test ordinary_measured_region_performs_zero_harness_allocations ... ok");
}

fn audit_cell(adapter: &InstrumentedAdapter, cell: &ScenarioCell) {
    HARNESS_ALLOCATIONS.store(0, Ordering::SeqCst);
    FIRST_OFFENDING_SIZE.store(u64::MAX, Ordering::SeqCst);
    set_measured_region_hook(Some(timing_hook));
    let result = execute_cell(adapter, cell);
    set_measured_region_hook(None);
    result.unwrap();
    assert_eq!(
        HARNESS_ALLOCATIONS.load(Ordering::SeqCst),
        0,
        "Rust control-plane allocation entered the measured interval for {:?} (first offending allocation size = {} bytes)",
        cell.card,
        FIRST_OFFENDING_SIZE.load(Ordering::SeqCst)
    );
}

fn latency_sampling_buffers_do_not_allocate_after_warmup() {
    if cfg!(target_os = "macos") {
        println!(
            "test latency_sampling_buffers_do_not_allocate_after_warmup ... skipped (macOS realloc may route through the global allocator)"
        );
        return;
    }
    let adapter = InstrumentedAdapter {
        layouts: Mutex::new(HashMap::new()),
    };
    let topology = Topology {
        physical_cores: 2,
        logical_cores: 2,
    };
    for (card, point) in [
        (
            CardId::TinyFixed64,
            benchmark_suite::scenarios::ThreadPoint::One,
        ),
        (
            CardId::SmallLogMixed,
            benchmark_suite::scenarios::ThreadPoint::PhysicalCores,
        ),
        (
            CardId::CrossThreadProducerConsumer,
            benchmark_suite::scenarios::ThreadPoint::PhysicalCores,
        ),
        (
            CardId::LargeObjects,
            benchmark_suite::scenarios::ThreadPoint::One,
        ),
    ] {
        let cell = ScenarioCell::new(card, point, topology, 8, 0x5eed).unwrap();
        for control in [false, true] {
            HARNESS_ALLOCATIONS.store(0, Ordering::SeqCst);
            FIRST_OFFENDING_SIZE.store(u64::MAX, Ordering::SeqCst);
            set_measured_region_hook(Some(timing_hook));
            let result = execute_latency_cell(&adapter, &cell, 2, control);
            set_measured_region_hook(None);
            result.unwrap();
            assert_eq!(
                HARNESS_ALLOCATIONS.load(Ordering::SeqCst),
                0,
                "latency sampling allocated in the measured region for {card:?}, control={control} (first offending allocation size = {} bytes)",
                FIRST_OFFENDING_SIZE.load(Ordering::SeqCst)
            );
        }
    }
    println!("test latency_sampling_buffers_do_not_allocate_after_warmup ... ok");
}

fn main() {
    // No libtest harness: both audits run sequentially, on this thread only.
    // libtest's default harness spawns each `#[test]` fn on its own OS thread
    // and initializes per-thread output capture the moment that thread
    // starts -- an allocation with no ordering relative to `MEASURED`. That
    // stray allocation from the *sibling* test's thread could land inside
    // this test's measured interval and be misattributed to the code under
    // audit (reproduced locally: ~10% failure rate under `--test-threads=4`
    // with two logical CPUs pinned via `taskset`, 0/60 with
    // `--test-threads=1`). Running everything on one thread with no sibling
    // test threads in the process removes the interference at the source.
    // See #236 / #206.
    println!("running 2 tests");
    ordinary_measured_region_performs_zero_harness_allocations();
    latency_sampling_buffers_do_not_allocate_after_warmup();
}
