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
static TEST_GATE: Mutex<()> = Mutex::new(());

thread_local! {
    static INSIDE_ADAPTER: Cell<bool> = const { Cell::new(false) };
}

struct AuditedSystem;

unsafe impl GlobalAlloc for AuditedSystem {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        if MEASURED.load(Ordering::SeqCst) && !INSIDE_ADAPTER.with(Cell::get) {
            HARNESS_ALLOCATIONS.fetch_add(1, Ordering::SeqCst);
        }
        unsafe { System.alloc(layout) }
    }

    unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
        if MEASURED.load(Ordering::SeqCst) && !INSIDE_ADAPTER.with(Cell::get) {
            HARNESS_ALLOCATIONS.fetch_add(1, Ordering::SeqCst);
        }
        unsafe { System.alloc_zeroed(layout) }
    }

    unsafe fn realloc(&self, pointer: *mut u8, layout: Layout, size: usize) -> *mut u8 {
        if MEASURED.load(Ordering::SeqCst) && !INSIDE_ADAPTER.with(Cell::get) {
            HARNESS_ALLOCATIONS.fetch_add(1, Ordering::SeqCst);
        }
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

#[test]
#[cfg_attr(
    target_os = "macos",
    ignore = "macOS realloc may route through the global allocator"
)]
fn ordinary_measured_region_performs_zero_harness_allocations() {
    let _test_guard = TEST_GATE.lock().unwrap();
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
}

fn audit_cell(adapter: &InstrumentedAdapter, cell: &ScenarioCell) {
    HARNESS_ALLOCATIONS.store(0, Ordering::SeqCst);
    set_measured_region_hook(Some(timing_hook));
    let result = execute_cell(adapter, cell);
    set_measured_region_hook(None);
    result.unwrap();
    assert_eq!(
        HARNESS_ALLOCATIONS.load(Ordering::SeqCst),
        0,
        "Rust control-plane allocation entered the measured interval for {:?}",
        cell.card
    );
}

#[test]
fn latency_sampling_buffers_do_not_allocate_after_warmup() {
    let _test_guard = TEST_GATE.lock().unwrap();
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
            set_measured_region_hook(Some(timing_hook));
            let result = execute_latency_cell(&adapter, &cell, 2, control);
            set_measured_region_hook(None);
            result.unwrap();
            assert_eq!(
                HARNESS_ALLOCATIONS.load(Ordering::SeqCst),
                0,
                "latency sampling allocated in the measured region for {card:?}, control={control}"
            );
        }
    }
}
