mod common;
use mimalloc_pprof::prof;

#[test]
fn samples_totals_match_dump_header() {
    common::start(4096, 97);
    let blocks: Vec<_> = (0..200).map(|_| vec![0_u8; 4096]).collect();

    // Dump first: the header is fixed under the profiler lock before the
    // returned Vec<u8> ever allocates, so it reflects exactly the live set
    // above. Drop the dump text before snapshotting so the snapshot sees
    // that same live set (the text buffer itself gets sampled otherwise).
    let text = common::dump();
    let (objects, bytes, _, _, _) = common::header(&text);
    drop(text);

    let samples = prof::samples();
    assert!(!samples.is_empty());
    let summed_objects: u64 = samples.iter().map(|s| s.live_objects as u64).sum();
    let summed_bytes: u64 = samples.iter().map(|s| s.live_bytes as u64).sum();
    assert_eq!(summed_objects, objects);
    assert_eq!(summed_bytes, bytes);

    drop(blocks);
    common::stop();
}
