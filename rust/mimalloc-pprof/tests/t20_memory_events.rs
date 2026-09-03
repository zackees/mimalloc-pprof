//! Opt-in allocation-change accounting (`include/mimalloc/memory-events.h`).
//!
//! Independent of `MI_PPROF`: `src/memory-events.c` is compiled in every configuration,
//! so this file carries no `required-features` and runs on both `rust-native` rows.
//!
//! Everything here is one `#[test]`, run in sections, and that is not laziness. Tracking,
//! the callback table and the running totals are all **process**-global, and cargo runs
//! the tests of one binary on several threads at once — so a second test allocating on
//! another thread lands in the very counters the first is asserting deltas on. One test
//! per binary is the only way these assertions mean anything.

use std::sync::atomic::{AtomicU64, Ordering};

use mimalloc_pprof::memory_events::{self, Callbacks, Change, ChangeKind};

#[global_allocator]
static ALLOCATOR: mimalloc_pprof::MiMalloc = mimalloc_pprof::MiMalloc;

#[test]
fn memory_events_end_to_end() {
    enabling_is_authoritative_and_reversible();
    snapshot_counts_allocations_while_enabled();
    tracking_off_records_nothing();
    unwrapped_allocations_are_excluded_from_accounting();
    callbacks_observe_every_kind();
}

fn enabling_is_authoritative_and_reversible() {
    let previous = memory_events::set_enabled(true);
    assert!(memory_events::is_enabled());
    memory_events::set_enabled(false);
    assert!(!memory_events::is_enabled());
    memory_events::set_enabled(previous);
}

fn snapshot_counts_allocations_while_enabled() {
    let previous = memory_events::set_enabled(true);

    let before = memory_events::snapshot().expect("snapshot while enabled");
    let held: Vec<Vec<u8>> = (0..64).map(|_| vec![0_u8; 4096]).collect();
    std::hint::black_box(&held);
    let during = memory_events::snapshot().expect("snapshot while enabled");

    assert!(
        during.accum_count >= before.accum_count + 64,
        "64 allocations should raise accum_count by at least 64: {} -> {}",
        before.accum_count,
        during.accum_count
    );
    assert!(
        during.accum_bytes >= before.accum_bytes + 64 * 4096,
        "accum_bytes should grow by at least the requested bytes: {} -> {}",
        before.accum_bytes,
        during.accum_bytes
    );
    assert!(
        during.live_bytes >= 64 * 4096,
        "live_bytes = {}",
        during.live_bytes
    );

    drop(held);
    let after = memory_events::snapshot().expect("snapshot while enabled");
    assert!(
        after.live_count < during.live_count,
        "freeing 64 blocks should lower live_count: {} -> {}",
        during.live_count,
        after.live_count
    );
    // Cumulative counters never go backwards, even as live ones fall.
    assert!(after.accum_count >= during.accum_count);

    memory_events::set_enabled(previous);
}

fn tracking_off_records_nothing() {
    let previous = memory_events::set_enabled(false);
    let before = memory_events::snapshot().expect("snapshot while disabled");
    let held: Vec<Vec<u8>> = (0..32).map(|_| vec![0_u8; 8192]).collect();
    std::hint::black_box(&held);
    let after = memory_events::snapshot().expect("snapshot while disabled");
    assert_eq!(
        before.accum_count, after.accum_count,
        "accounting must stop dead while tracking is off"
    );
    drop(held);
    memory_events::set_enabled(previous);
}

fn unwrapped_allocations_are_excluded_from_accounting() {
    let previous = memory_events::set_enabled(true);
    let before = memory_events::snapshot().expect("snapshot while enabled");

    // The unwrapped path is backed straight by the OS layer, never by the hooked
    // mi_malloc family, so it must not move the accounting counters at all.
    let p = unsafe { mimalloc_pprof::unwrapped_malloc(64 * 1024, 4096) };
    assert!(!p.is_null(), "unwrapped_malloc returned NULL");
    let after = memory_events::snapshot().expect("snapshot while enabled");
    unsafe { mimalloc_pprof::unwrapped_free(p) };

    assert_eq!(before.accum_count, after.accum_count);
    assert_eq!(before.accum_bytes, after.accum_bytes);

    memory_events::set_enabled(previous);
}

static ALLOCATES: AtomicU64 = AtomicU64::new(0);
static FREES: AtomicU64 = AtomicU64::new(0);
static RESIZES: AtomicU64 = AtomicU64::new(0);

/// A panic inside a callback is caught and swallowed at the C boundary (it must not
/// unwind across a C frame), so a failed `assert!` in one of these would silently pass
/// the test. Record contract violations in a counter and check it from the test body.
static VIOLATIONS: AtomicU64 = AtomicU64::new(0);

fn record(condition: bool) {
    if !condition {
        VIOLATIONS.fetch_add(1, Ordering::Relaxed);
    }
}

fn on_allocate(change: &Change) {
    record(change.kind == ChangeKind::Allocate && change.delta_bytes >= 0);
    ALLOCATES.fetch_add(1, Ordering::Relaxed);
}

fn on_free(change: &Change) {
    // "Caller-requested size for allocation and resize; zero for free."
    record(change.kind == ChangeKind::Free && change.delta_bytes <= 0 && change.request_size == 0);
    FREES.fetch_add(1, Ordering::Relaxed);
}

fn on_resize(change: &Change) {
    record(change.kind == ChangeKind::Resize);
    RESIZES.fetch_add(1, Ordering::Relaxed);
}

/// `&'static` is the API's answer to the header's "`arg` pointers are caller-owned and
/// must stay valid": the C side keeps this pointer until the table is replaced.
static CALLBACKS: Callbacks = Callbacks {
    allocate: Some(on_allocate),
    free: Some(on_free),
    resize: Some(on_resize),
};

fn callbacks_observe_every_kind() {
    let previous = memory_events::set_enabled(true);
    assert!(memory_events::set_callbacks(&CALLBACKS));

    let mut grown: Vec<u8> = Vec::with_capacity(64);
    grown.resize(64, 1);
    grown.reserve(64 * 1024); // realloc -> RESIZE
    std::hint::black_box(&grown);
    drop(grown);

    assert!(memory_events::clear_callbacks());
    memory_events::set_enabled(previous);

    assert_eq!(
        VIOLATIONS.load(Ordering::Relaxed),
        0,
        "a callback saw a change record that contradicts memory-events.h"
    );
    assert!(
        ALLOCATES.load(Ordering::Relaxed) > 0,
        "no ALLOCATE callbacks fired"
    );
    assert!(FREES.load(Ordering::Relaxed) > 0, "no FREE callbacks fired");
    assert!(
        RESIZES.load(Ordering::Relaxed) > 0,
        "no RESIZE callbacks fired"
    );

    // Cleared means cleared: nothing fires after this point.
    let allocates = ALLOCATES.load(Ordering::Relaxed);
    let noise: Vec<u8> = vec![3_u8; 32 * 1024];
    std::hint::black_box(&noise);
    assert_eq!(
        allocates,
        ALLOCATES.load(Ordering::Relaxed),
        "a callback fired after clear_callbacks"
    );
}
