//! Profiler accounting across the realloc matrix (issue #83, second half).
//!
//! In its own file, and that is load-bearing rather than tidiness: cargo runs the tests
//! within one binary CONCURRENTLY, while `prof::stats()` reports process-wide live
//! sampling. Sharing a binary with the `t16_rezalloc` allocation tests made this read
//! their allocations as its own and fail with a phantom +1 leak. Each `tests/*.rs` is a
//! separate binary, so isolating the file isolates the profiler state.
//!
//! Verified before splitting: probing each resize path on its own gives delta=0 for
//! no-resize, grow, shrink, rezalloc and recalloc alike -- no path leaks a sample.

mod common;

use mimalloc_pprof::{prof, sys};

/// The half of #83 that was unverified: sampled allocations must survive the whole
/// realloc matrix without the profiler dropping or double-counting them.
///
/// Uses a small sample rate so blocks are reliably sampled, and asserts the invariant
/// that actually matters — that live sample bookkeeping returns to its starting point
/// once everything is freed. A leak or a double-count both violate that, while a raw
/// count comparison mid-sequence would be probabilistic and flaky.
#[test]
fn profiler_accounting_survives_the_realloc_matrix() {
    common::start(4096, 0x83);

    let before = prof::stats();

    unsafe {
        let mut blocks: Vec<*mut u8> = Vec::new();
        for round in 0..64usize {
            let p = sys::mi_malloc(8192).cast::<u8>();
            assert!(!p.is_null());

            // Cycle through every resize path, including the moving and in-place cases.
            let q = match round % 4 {
                0 => sys::mi_realloc(p.cast(), 16384).cast::<u8>(), // grow, likely moves
                1 => sys::mi_realloc(p.cast(), 128).cast::<u8>(),   // shrink, in place
                2 => mimalloc_pprof::rezalloc(p, 32768),            // zeroing grow
                _ => mimalloc_pprof::recalloc(p, 512, 64),          // zeroing grow, count form
            };
            assert!(!q.is_null());
            blocks.push(q);
        }
        for b in blocks {
            sys::mi_free(b.cast());
        }
    }

    let after = prof::stats();

    // Everything allocated above has been freed, so live sampling must be back where it
    // started. Growth here would mean a resize path leaked a sample record; a decrease
    // would mean one was released twice.
    assert_eq!(
        after.live_samples, before.live_samples,
        "live sample count changed across a fully-freed realloc matrix \
         (before={}, after={})",
        before.live_samples, after.live_samples
    );
    assert_eq!(
        after.live_bytes, before.live_bytes,
        "live sampled bytes changed across a fully-freed realloc matrix \
         (before={}, after={})",
        before.live_bytes, after.live_bytes
    );

    prof::stop();
}
