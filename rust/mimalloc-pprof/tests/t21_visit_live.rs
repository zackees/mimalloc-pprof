//! `mi_memory_visit_live_allocations` (include/mimalloc/memory-events.h).
//!
//! Deliberately the only test in its own binary. The walk is built on
//! `mi_heap_visit_blocks`, whose documented precondition is that no other thread is
//! freeing into the heaps being walked — and cargo's harness runs the tests of one
//! binary on several threads at once. One test, one thread, no precondition to violate.

use std::cell::Cell;

#[global_allocator]
static ALLOCATOR: mimalloc_pprof::MiMalloc = mimalloc_pprof::MiMalloc;

#[test]
fn visits_live_allocations_without_allocating_in_the_visitor() {
    let held: Vec<Vec<u8>> = (0..48).map(|_| vec![0xAB_u8; 3000]).collect();
    std::hint::black_box(&held);

    // Fixed, pre-allocated state only: the visitor must not allocate, free, or reenter
    // mimalloc, so it counts into `Cell`s that already exist.
    let seen = Cell::new(0_usize);
    let bytes = Cell::new(0_usize);
    let big_enough = Cell::new(0_usize);

    let ok = unsafe {
        mimalloc_pprof::memory_events::visit_live_allocations(|_allocation, usable_size| {
            seen.set(seen.get() + 1);
            bytes.set(bytes.get() + usable_size);
            if usable_size >= 3000 {
                big_enough.set(big_enough.get() + 1);
            }
            true
        })
    };

    assert!(ok, "mi_memory_visit_live_allocations reported failure");
    assert!(seen.get() > 0, "the walk visited nothing at all");
    assert!(
        bytes.get() >= seen.get(),
        "every allocation has a usable size"
    );
    assert!(
        big_enough.get() >= 48,
        "the 48 live 3000-byte vectors should all be visible; saw {} blocks >= 3000 bytes \
         out of {} total",
        big_enough.get(),
        seen.get()
    );

    // Returning `false` stops the walk early.
    let counted = Cell::new(0_usize);
    let ok = unsafe {
        mimalloc_pprof::memory_events::visit_live_allocations(|_, _| {
            counted.set(counted.get() + 1);
            counted.get() < 5
        })
    };
    assert!(ok || counted.get() == 5);
    assert_eq!(
        counted.get(),
        5,
        "the visitor's `false` did not stop the walk"
    );

    drop(held);
}
