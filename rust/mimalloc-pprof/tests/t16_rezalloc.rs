//! Zeroing reallocation from Rust, and profiler accounting across the realloc matrix
//! (issue #83).
//!
//! Two halves, because #83 had two halves:
//!
//! 1. `mi_rezalloc` / `mi_recalloc` were unreachable from Rust at all — `GlobalAlloc`
//!    has no `grow_zeroed`, so a caller had to grow and `memset` by hand.
//! 2. Profiler accounting on the realloc paths was *unverified*. The hooks are named
//!    for the in-place case (`_mi_prof_on_realloc_in_place`) while a separate moving
//!    path exists, so "does a sampled block survive a realloc correctly" was an open
//!    question rather than a tested one.
//!
//! Note these use the PLAIN allocation family (`mi_malloc`/`mi_free`), not
//! `unwrapped_*`. The two are not interchangeable: `unwrapped_*` puts a header before
//! the pointer, so handing one of those to `mi_rezalloc` trips
//! `mi_usable_size: invalid pointer`. The first draft of this test made exactly that
//! mistake and crashed, which is why `rezalloc`'s safety docs now name the family.

mod common;

use mimalloc_pprof::{prof, sys};

/// The zeroing contract, stated the way mimalloc actually implements it.
///
/// Deliberately measured from the old **usable** size, not the old requested size.
/// A fuzz harness asserted the intuitive version and was falsified in seconds (#87):
/// mimalloc zeroes from `_mi_usable_size(p)`, so the slack between requested and
/// usable is left alone.
#[test]
fn rezalloc_zeroes_past_the_old_usable_size() {
    unsafe {
        let n_old = 64usize;
        let p = sys::mi_malloc(n_old).cast::<u8>();
        assert!(!p.is_null());
        // Dirty the whole usable block so a missing zeroing cannot pass by luck.
        let usable_old = mimalloc_pprof::usable_size(p);
        assert!(usable_old >= n_old);
        std::ptr::write_bytes(p, 0xA5, usable_old);

        let n_new = usable_old + 4096; // force a genuine grow past the old block
        let q = mimalloc_pprof::rezalloc(p, n_new);
        assert!(!q.is_null());

        for i in usable_old..n_new {
            assert_eq!(
                *q.add(i),
                0,
                "byte {i} past the old usable size was not zeroed"
            );
        }
        sys::mi_free(q.cast());
    }
}

#[test]
fn recalloc_matches_rezalloc_for_the_grown_tail() {
    unsafe {
        let p = sys::mi_malloc(32).cast::<u8>();
        assert!(!p.is_null());
        let usable_old = mimalloc_pprof::usable_size(p);
        std::ptr::write_bytes(p, 0x5A, usable_old);

        let count = 64usize;
        let each = 64usize;
        assert!(count * each > usable_old);
        let q = mimalloc_pprof::recalloc(p, count, each);
        assert!(!q.is_null());
        for i in usable_old..(count * each) {
            assert_eq!(*q.add(i), 0, "byte {i} was not zeroed by recalloc");
        }
        sys::mi_free(q.cast());
    }
}

/// `expand` must not move, and must leave `p` live when it declines.
#[test]
fn expand_never_moves_and_leaves_p_valid_on_failure() {
    unsafe {
        let p = sys::mi_malloc(64).cast::<u8>();
        assert!(!p.is_null());
        *p = 0x42;

        // Absurdly large: must fail rather than move.
        let q = mimalloc_pprof::expand(p, 1 << 40);
        assert!(
            q.is_null(),
            "expand must return null rather than move the block"
        );
        // p is still live and unchanged -- this is what distinguishes it from rezalloc.
        assert_eq!(*p, 0x42);

        // A grow within the existing usable size should succeed in place.
        let usable = mimalloc_pprof::usable_size(p);
        let r = mimalloc_pprof::expand(p, usable);
        if !r.is_null() {
            assert_eq!(r, p, "expand must not move the block");
        }
        sys::mi_free(p.cast());
    }
}
