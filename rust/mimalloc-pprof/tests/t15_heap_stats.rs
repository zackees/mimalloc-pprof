//! v3 allocator ground-truth counters exposed alongside the sampled profiler
//! stats (`ProfStats::heap`).
//!
//! These also serve as an ABI guard: `prof::stats()` returns
//! `ProfStats::default()` (all zeros) when the Rust `mi_prof_stats_t` mirror
//! disagrees with the linked C struct on size/version. A drift in either would
//! therefore show up as zeroed `heap` fields rather than a compile error, so
//! asserting they are populated is what actually catches it.

mod common;
use mimalloc_pprof::prof;

#[test]
fn heap_stats_are_populated_and_exact() {
    // Valid even with the profiler stopped: these are allocator counters, not
    // sampler state.
    let before = prof::stats();
    assert!(!before.enabled);
    assert!(
        before.heap.reserved >= before.heap.committed,
        "reserved ({}) must cover committed ({})",
        before.heap.reserved,
        before.heap.committed
    );

    common::start(4096, 137);

    const COUNT: usize = 256;
    const SIZE: usize = 8192;
    let blocks: Vec<_> = (0..COUNT).map(|_| vec![0_u8; SIZE]).collect();

    let during = prof::stats();
    assert!(during.enabled);
    assert!(during.live_samples > 0);

    // malloc_requested is exact and unsampled, so it must account for at least
    // everything just allocated. This is the assertion a sampled profile alone
    // cannot make -- but it is only tracked at MI_STAT >= 2, which upstream
    // enables by default only for debug builds, so a release build must assert
    // the opposite rather than skipping the check.
    if during.heap.detailed {
        assert!(
            during.heap.malloc_requested >= COUNT * SIZE,
            "malloc_requested ({}) must cover the {} bytes just allocated",
            during.heap.malloc_requested,
            COUNT * SIZE
        );
    } else {
        assert_eq!(
            during.heap.malloc_requested, 0,
            "malloc_requested must be 0 when the build does not track it"
        );
    }
    assert!(during.heap.committed > 0, "heap.committed must be populated");
    assert!(during.heap.reserved >= during.heap.committed);
    assert!(during.heap.pages > 0, "heap.pages must be populated");
    assert!(during.heap.heaps > 0, "heap.heaps must be populated");
    assert!(
        during.heap.committed >= before.heap.committed,
        "committed must not shrink after allocating"
    );

    // The profiler is global process state, so this file deliberately keeps a
    // single test: a second #[test] in the same binary would run concurrently
    // and stop the profiler out from under this one.
    let dump = common::dump();

    assert!(
        dump.contains("# mimalloc heap stats\n"),
        "dump must carry the allocator stats comment block"
    );
    assert!(dump.contains("# committed = "));
    assert!(dump.contains("# theaps = "));
    // The profile records whether the build tracks the MI_STAT>=2 counters, so a
    // reader can tell a real 0 from an untracked one.
    assert!(dump.contains(if during.heap.detailed {
        "# detailed_stats = 1\n"
    } else {
        "# detailed_stats = 0\n"
    }));

    // The block is a pprof comment section: it must sit after the samples and
    // before MAPPED_LIBRARIES so google/pprof's legacy heap parser skips it.
    let stats_at = dump.find("# mimalloc heap stats").expect("stats block");
    let maps_at = dump.find("MAPPED_LIBRARIES:").expect("maps section");
    assert!(
        stats_at < maps_at,
        "stats block ({stats_at}) must precede MAPPED_LIBRARIES ({maps_at})"
    );

    drop(blocks);
    common::stop();

    // Allocator counters survive prof_stop; sampler state does not.
    let after = prof::stats();
    assert!(!after.enabled);
    assert_eq!(after.live_samples, 0);
    assert!(after.heap.committed > 0);
}
