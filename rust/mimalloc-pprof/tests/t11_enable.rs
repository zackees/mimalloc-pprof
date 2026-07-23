mod common;
use mimalloc_pprof::{enable_heap_profiling, prof};

#[test]
fn enable_heap_profiling_uses_default_rate() {
    // No MIMALLOC_PROF* env vars in the cargo-test environment, so the
    // profiler starts out disabled and the default rate applies.
    assert!(!prof::is_enabled());
    assert!(enable_heap_profiling());
    assert!(prof::is_enabled());
    assert_eq!(prof::stats().sample_rate, 512 * 1024);

    // A second call reports the already-running session untouched.
    assert!(!enable_heap_profiling());
    assert!(prof::is_enabled());

    let blocks: Vec<_> = (0..64).map(|_| vec![0_u8; 64 * 1024]).collect();
    assert!(prof::stats().live_samples > 0);
    drop(blocks);
    prof::stop();
}
