mod common;
use mimalloc_pprof::prof;

#[test]
fn stats_track_start_alloc_drop_stop_lifecycle() {
    // No MIMALLOC_PROF in the cargo-test environment, so prof_auto_start
    // never fires and the profiler starts out disabled.
    let before = prof::stats();
    assert!(!before.enabled);

    common::start(4096, 101);
    let after_start = prof::stats();
    assert!(after_start.enabled);
    assert_eq!(after_start.sample_rate, 4096);

    let blocks: Vec<_> = (0..200).map(|_| vec![0_u8; 4096]).collect();
    let after_alloc = prof::stats();
    assert!(after_alloc.live_samples > 0);

    drop(blocks);
    let after_drop = prof::stats();
    assert_eq!(after_drop.live_samples, 0);

    common::stop();
    let after_stop = prof::stats();
    assert!(!after_stop.enabled);

    assert!(prof::samples().is_empty());
}
