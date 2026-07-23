mod common;
use mimalloc_pprof::prof;

#[test]
fn samples_totals_match_stats_and_dump_header() {
    common::start(4096, 97);
    let blocks: Vec<_> = (0..200).map(|_| vec![0_u8; 4096]).collect();

    // Dump first: the header is fixed under the profiler lock before the
    // returned Vec<u8> ever allocates. Drop the text so its (sampled)
    // buffer does not linger in the live set.
    let text = common::dump();
    let (dump_objects, dump_bytes, _, _, _) = common::header(&text);
    drop(text);

    // stats() performs no heap allocation and samples() snapshots before
    // its own collection allocates, so these two observe the same instant
    // and must agree exactly (tier 1 == tier 2).
    let stats = prof::stats();
    let samples = prof::samples();
    assert!(!samples.is_empty());
    let summed_objects: u64 = samples.iter().map(|s| s.live_objects as u64).sum();
    let summed_bytes: u64 = samples.iter().map(|s| s.live_bytes as u64).sum();
    assert_eq!(summed_objects, stats.live_samples as u64);
    assert_eq!(summed_bytes, stats.live_bytes as u64);

    // The dump was captured a few statements earlier; lazily-initialized
    // runtime machinery (regex tables, stdio buffers) may add a stray live
    // sample between the two captures, so allow a small drift. Exact
    // three-way agreement in a truly quiescent process is covered by the
    // C test (test/test-profile.c, T8), which allocates nothing between
    // captures. A real accounting bug diverges by ~200 objects here.
    assert!(summed_objects.abs_diff(dump_objects) <= 8);
    assert!(summed_bytes.abs_diff(dump_bytes) <= 64 * 1024);

    drop(blocks);
    common::stop();
}
