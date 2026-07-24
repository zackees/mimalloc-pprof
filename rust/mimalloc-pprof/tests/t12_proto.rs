mod common;
use mimalloc_pprof::prof;

#[test]
fn proto_dump_is_compact_binary_pprof_and_supports_ci_export() {
    common::start(4096, 13);
    let blocks: Vec<_> = (0..200).map(|_| vec![0_u8; 4096]).collect();

    // Both dumps are captured while `blocks` is still alive, so they
    // describe the same profiler state and are directly comparable.
    let proto = prof::dump_proto_to_vec();
    let text = prof::dump_to_vec();

    assert!(!proto.is_empty());

    // First byte is the tag for field 1 (sample_type), wire type 2
    // (length-delimited submessage): (1 << 3) | 2 = 0x0a. `sample_type` is
    // always the first field mi_prof_dump_proto_writer emits (src/profile.c),
    // so this pins the encoding to a real pprof Profile message rather than
    // an accidental passthrough of the text format.
    assert_eq!(proto[0], 0x0a, "first byte should be the sample_type tag");

    // The binary pprof encoding is materially smaller than the legacy text
    // dump for the same live set: no repeated ASCII-decimal formatting, no
    // "heap profile:"/"MAPPED_LIBRARIES:" boilerplate, and varints instead
    // of hex text for stack addresses.
    assert!(
        proto.len() < text.len(),
        "proto ({} bytes) should be smaller than text ({} bytes)",
        proto.len(),
        text.len()
    );

    // Hook for CI's pprof gate: when set, persist the proto bytes so an
    // external pprof-compatible validator can inspect them directly.
    if let Some(path) = std::env::var_os("MIMALLOC_PROF_PROTO_PATH") {
        std::fs::write(&path, &proto).expect("write MIMALLOC_PROF_PROTO_PATH");
    }

    drop(blocks);
    common::stop();
}
