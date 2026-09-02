//! The vendored amalgamation must keep selecting its atomics backend by
//! capability, not by `_MSC_VER` (see #230).
//!
//! `_MSC_VER` means "claims MSVC source compatibility", not "lacks C11
//! atomics". clang-cl defines it but is clang, so a bare `_MSC_VER` guard sends
//! it to the hand-rolled MSVC wrapper, which uses `__ldar64`/`__stlr64` --
//! intrinsics clang-cl does not declare on ARM64. That broke the
//! `aarch64-pc-windows-msvc` cross-build in #223.
//!
//! This lived in `build.rs` as a target allowlist until #230 moved it into the
//! C. A `dev3` re-sync that overwrites `include/mimalloc/atomic.h` would
//! silently reintroduce the bug for every consumer -- CMake and `static.c`
//! included, which the build script never covered. These tests fail instead.

const AMALGAMATION: &str = include_str!("../vendor/mimalloc-pprof-amalgamated.c");

#[test]
fn capability_macro_uses_the_standard_c11_atomics_test() {
    assert!(
        AMALGAMATION.contains("#define MI_HAS_C11_ATOMICS 1"),
        "MI_HAS_C11_ATOMICS is gone; the amalgamation predates #230 or a \
         re-sync dropped it"
    );
    assert!(
        AMALGAMATION.contains("__STDC_NO_ATOMICS__"),
        "the capability test must key on __STDC_NO_ATOMICS__, the standard \
         signal that <stdatomic.h> is unavailable"
    );
}

#[test]
fn msvc_wrapper_is_gated_on_missing_c11_atomics() {
    assert!(
        AMALGAMATION.contains("#elif defined(_MSC_VER) && !MI_HAS_C11_ATOMICS"),
        "the MSVC wrapper is selected on bare _MSC_VER again; clang-cl will \
         take it and fail on __ldar64/__stlr64 for ARM64"
    );
    assert!(
        AMALGAMATION.contains("|| !defined(_MSC_VER) || MI_HAS_C11_ATOMICS"),
        "the typed-ptr chain no longer tracks the backend selection; clang-cl \
         would get C11 atomics in one chain and Interlocked in the other"
    );
}

/// Issue #272: the MSVC **C** wrapper above implements `mi_atomic_load/store/exchange/cas`
/// ONLY at `uintptr_t` width (`__iso_volatile_load`/`_store` on a `volatile intptr_t*`), and
/// this crate's `build.rs` compiles the amalgamation as C -- so on `x86_64-pc-windows-msvc`
/// that wrapper is what ships. A narrower `_Atomic` field reached through those macros is
/// read and written a whole word at a time, past its end: the P7a park fields sit next to
/// `mi_theap_t* park_theap0`, so a 32-bit `park_reclaim` store zeroed half of that pointer.
///
/// `t18_thread_idle` exercises the runtime path; these assert the vendored source itself, so
/// a re-sync or a "tidy the types" edit that narrows them again fails here rather than
/// corrupting a pointer only on one target.
#[test]
fn park_fields_are_word_width() {
    for field in ["park_state", "park_reclaim", "park_swept"] {
        let decl = format!("_Atomic(size_t)       {field};");
        assert!(
            AMALGAMATION.contains(&decl),
            "mi_tld_t::{field} is no longer `_Atomic(size_t)`; the MSVC C atomics wrapper \
             would access it a full word at a time (#272)"
        );
    }
    assert!(
        AMALGAMATION.contains("_Atomic(mi_scav_word_t) scavenger_wake;"),
        "scavenger_wake is no longer `mi_scav_word_t`-typed (#272)"
    );
}

#[test]
fn the_width_static_assert_is_vendored_too() {
    assert!(
        AMALGAMATION.contains("mi_scav_atomic_widths_assert_t"),
        "src/scavenger.c's atomic-width static assert did not reach the amalgamation (#272)"
    );
}
