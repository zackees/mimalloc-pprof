//! `mi_purge_holes_report` (include/mimalloc.h, issue #272 / Bun parity P7b).
//!
//! Its contract is that it is **read-only**: it reports what the last idle sweep could
//! not discard, and purges nothing itself. Proving that means comparing hole-purging
//! counters across the call — which only works if nothing else is sweeping. The
//! background scavenger sweeps on a timer, and cargo runs the tests of one binary
//! concurrently, so this is deliberately the only test in its own binary, and it stops
//! the scavenger before it measures.

#[global_allocator]
static ALLOCATOR: mimalloc_pprof::MiMalloc = mimalloc_pprof::MiMalloc;

#[test]
fn report_is_read_only() {
    // Churn first, so the sweep below has holes to find and to report on, and so every
    // theap this thread needs already exists.
    let mut held: Vec<Vec<u8>> = (0..512).map(|_| vec![0_u8; 512]).collect();
    // scattered survivors: exactly the case hole purging exists for
    held.retain(|v| (v.as_ptr() as usize).is_multiple_of(3));
    std::hint::black_box(&held);

    mimalloc_pprof::on_thread_idle();

    // Only now: a later allocation can bring the scavenger back, so stopping it has to be
    // the last thing before the measurement.
    mimalloc_pprof::scavenger_stop();

    let before = mimalloc_pprof::purge_holes_stats();
    mimalloc_pprof::purge_holes_report();
    let after = mimalloc_pprof::purge_holes_stats();

    assert_eq!(
        before.purged_bytes_total, after.purged_bytes_total,
        "mi_purge_holes_report discarded memory; it is documented read-only"
    );
    assert_eq!(before.discard_calls, after.discard_calls);
    assert_eq!(before.reuse_calls, after.reuse_calls);
    assert_eq!(before.blocks_visited, after.blocks_visited);
    assert_eq!(before.pages_freed, after.pages_freed);

    drop(held);
}
