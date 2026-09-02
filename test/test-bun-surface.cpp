/* ----------------------------------------------------------------------------
Bun consumer-surface parity check (issue #274, Bun parity P9a).

This is NOT a CMake-registered test (CLAUDE.md: keep C-core and CI-script concerns
separate; `ci/check_bun_surface.py` builds and links this TU by hand, outside CMake,
using Bun's *exact* `scripts/build/deps/mimalloc.ts` DirectBuild define set, so the link
step reproduces what actually happens when `oven-sh/bun` vendors this repo).

Two independent checks live in this one TU, mirroring how Bun itself consumes mimalloc:

  1. **Link-time symbol check.** Bun's `src/mimalloc_sys/mimalloc.rs` hand-declares
     `extern "C"` prototypes for the C symbols it calls (it never includes our headers);
     Bun's C++ consumers (`src/jsc/bindings/MimallocWTFMalloc.h`,
     `src/jsc/modules/BunJSCModule.h`) `#include "mimalloc.h"` directly. This file takes
     the address of every symbol from BOTH consumer classes into a volatile array, which
     forces the linker to resolve each one. If a symbol used to be there and is now gone
     (or not yet landed -- see `mi_on_thread_idle` below), linking this TU fails, and
     `ci/check_bun_surface.py` reports exactly which symbol.

  2. **ABI static_asserts.** Bun hardcodes `MI_MAX_ALIGN_SIZE`, the `mi_heap_area_t`
     layout, and the numeric values of `mi_option_t` slots 0-42 on the Rust side
     (`mimalloc_sys.rs`) instead of generating bindings. A silent renumber there is a
     silent ABI break Bun would not catch at their own compile time (Rust's FFI has no
     way to check it against our header) -- only ours can.

The symbol list, struct layout, and option values below were re-derived directly from
`oven-sh/bun@main`'s `src/mimalloc_sys/mimalloc.rs`, `src/bun_alloc/MimallocArena.rs`,
`src/jsc/bindings/MimallocWTFMalloc.h`, and `src/jsc/modules/BunJSCModule.h` on
2026-09-02 (byte-identical to the 2026-09-01 gap analysis; Bun's `mimalloc.ts` pin has
not moved off `942b8342`). Re-derive at the next refresh rather than trusting this
comment -- see `docs/bun-gap-analysis-2026-09-02.md` section 2a for the extraction
method.
-----------------------------------------------------------------------------*/

#include <mimalloc.h>
#include <mimalloc-stats.h>  // mi_stats_get_json, mi_heap_dump_json
#include <mimalloc/types.h>  // MI_MAX_ALIGN_SIZE

#include <cstddef>  // offsetof

// ---------------------------------------------------------------------------
// 1. ABI static_asserts -- MI_MAX_ALIGN_SIZE
// ---------------------------------------------------------------------------

// mimalloc_sys.rs:182 -- `pub const MI_MAX_ALIGN_SIZE: usize = 16;`
static_assert(MI_MAX_ALIGN_SIZE == 16, "Bun hardcodes MI_MAX_ALIGN_SIZE == 16 (mimalloc_sys.rs:182)");

// ---------------------------------------------------------------------------
// 2. ABI static_asserts -- mi_heap_area_t (mimalloc_sys.rs:97-107, struct_mi_heap_area_s)
// ---------------------------------------------------------------------------
//
// Bun's field order: blocks, reserved, committed, used, block_size, full_block_size,
// reserved1 (7 machine words: 2 pointers + 4 size_t + 1 pointer). `reserved1` is
// documented "internal" in our header and Bun never reads it, but it still occupies a
// word Bun's struct must reserve to keep every later field's offset correct -- so the
// total-size assert includes it even though there is no dedicated offsetof for it.

static_assert(sizeof(mi_heap_area_t) == 7 * sizeof(void*),
              "mi_heap_area_t grew/shrank relative to Bun's struct_mi_heap_area_s");

static_assert(offsetof(mi_heap_area_t, blocks) == 0 * sizeof(void*),
              "mi_heap_area_t::blocks offset drifted from Bun's mirror");
static_assert(offsetof(mi_heap_area_t, reserved) == 1 * sizeof(void*),
              "mi_heap_area_t::reserved offset drifted from Bun's mirror");
static_assert(offsetof(mi_heap_area_t, committed) == 2 * sizeof(void*),
              "mi_heap_area_t::committed offset drifted from Bun's mirror");
static_assert(offsetof(mi_heap_area_t, used) == 3 * sizeof(void*),
              "mi_heap_area_t::used offset drifted from Bun's mirror");
static_assert(offsetof(mi_heap_area_t, block_size) == 4 * sizeof(void*),
              "mi_heap_area_t::block_size offset drifted from Bun's mirror");
static_assert(offsetof(mi_heap_area_t, full_block_size) == 5 * sizeof(void*),
              "mi_heap_area_t::full_block_size offset drifted from Bun's mirror");

// ---------------------------------------------------------------------------
// 3. ABI static_asserts -- mi_option_t slots 0-42 (mimalloc_sys.rs:127-171, `enum Option`)
// ---------------------------------------------------------------------------
//
// Bun's Rust enum names are cosmetically different at a few slots (3: `eager_commit`
// here is `mi_option_deprecated_eager_commit`; 12: `abandoned_page_purge` here is
// `mi_option_deprecated_abandoned_page_purge`; 14: `eager_commit_delay` here is
// `mi_option_deprecated_eager_commit_delay`; 28: `visit_abandoned` here is
// `mi_option_deprecated_visit_abandoned`) -- the numeric slot is what Bun's FFI actually
// depends on (it passes the enum by value, cast to a C `long`), so these asserts check
// slot number against OUR canonical name, not Bun's spelling.

static_assert(mi_option_show_errors == 0, "mi_option_t slot 0 drifted from Bun's enum");
static_assert(mi_option_show_stats == 1, "mi_option_t slot 1 drifted from Bun's enum");
static_assert(mi_option_verbose == 2, "mi_option_t slot 2 drifted from Bun's enum");
static_assert(mi_option_deprecated_eager_commit == 3, "mi_option_t slot 3 (Bun: eager_commit) drifted");
static_assert(mi_option_arena_eager_commit == 4, "mi_option_t slot 4 drifted from Bun's enum");
static_assert(mi_option_purge_decommits == 5, "mi_option_t slot 5 drifted from Bun's enum");
static_assert(mi_option_allow_large_os_pages == 6, "mi_option_t slot 6 drifted from Bun's enum");
static_assert(mi_option_reserve_huge_os_pages == 7, "mi_option_t slot 7 drifted from Bun's enum");
static_assert(mi_option_reserve_huge_os_pages_at == 8, "mi_option_t slot 8 drifted from Bun's enum");
static_assert(mi_option_reserve_os_memory == 9, "mi_option_t slot 9 drifted from Bun's enum");
static_assert(mi_option_deprecated_segment_cache == 10, "mi_option_t slot 10 drifted from Bun's enum");
static_assert(mi_option_deprecated_page_reset == 11, "mi_option_t slot 11 drifted from Bun's enum");
static_assert(mi_option_deprecated_abandoned_page_purge == 12,
              "mi_option_t slot 12 (Bun: abandoned_page_purge) drifted");
static_assert(mi_option_deprecated_segment_reset == 13, "mi_option_t slot 13 drifted from Bun's enum");
static_assert(mi_option_deprecated_eager_commit_delay == 14,
              "mi_option_t slot 14 (Bun: eager_commit_delay) drifted");
static_assert(mi_option_purge_delay == 15, "mi_option_t slot 15 drifted from Bun's enum");
static_assert(mi_option_use_numa_nodes == 16, "mi_option_t slot 16 drifted from Bun's enum");
static_assert(mi_option_disallow_os_alloc == 17, "mi_option_t slot 17 drifted from Bun's enum");
static_assert(mi_option_os_tag == 18, "mi_option_t slot 18 drifted from Bun's enum");
static_assert(mi_option_max_errors == 19, "mi_option_t slot 19 drifted from Bun's enum");
static_assert(mi_option_max_warnings == 20, "mi_option_t slot 20 drifted from Bun's enum");
static_assert(mi_option_deprecated_max_segment_reclaim == 21, "mi_option_t slot 21 drifted from Bun's enum");
static_assert(mi_option_destroy_on_exit == 22, "mi_option_t slot 22 drifted from Bun's enum");
static_assert(mi_option_arena_reserve == 23, "mi_option_t slot 23 drifted from Bun's enum");
static_assert(mi_option_arena_purge_mult == 24, "mi_option_t slot 24 drifted from Bun's enum");
static_assert(mi_option_deprecated_purge_extend_delay == 25, "mi_option_t slot 25 drifted from Bun's enum");
static_assert(mi_option_disallow_arena_alloc == 26, "mi_option_t slot 26 drifted from Bun's enum");
static_assert(mi_option_retry_on_oom == 27, "mi_option_t slot 27 drifted from Bun's enum");
static_assert(mi_option_deprecated_visit_abandoned == 28, "mi_option_t slot 28 (Bun: visit_abandoned) drifted");
static_assert(mi_option_guarded_min == 29, "mi_option_t slot 29 drifted from Bun's enum");
static_assert(mi_option_guarded_max == 30, "mi_option_t slot 30 drifted from Bun's enum");
static_assert(mi_option_guarded_precise == 31, "mi_option_t slot 31 drifted from Bun's enum");
static_assert(mi_option_guarded_sample_rate == 32, "mi_option_t slot 32 drifted from Bun's enum");
static_assert(mi_option_guarded_sample_seed == 33, "mi_option_t slot 33 drifted from Bun's enum");
static_assert(mi_option_generic_collect == 34, "mi_option_t slot 34 drifted from Bun's enum");
static_assert(mi_option_page_reclaim_on_free == 35, "mi_option_t slot 35 drifted from Bun's enum");
static_assert(mi_option_page_full_retain == 36, "mi_option_t slot 36 drifted from Bun's enum");
static_assert(mi_option_page_max_candidates == 37, "mi_option_t slot 37 drifted from Bun's enum");
static_assert(mi_option_max_vabits == 38, "mi_option_t slot 38 drifted from Bun's enum");
static_assert(mi_option_pagemap_commit == 39, "mi_option_t slot 39 drifted from Bun's enum");
static_assert(mi_option_page_commit_on_demand == 40, "mi_option_t slot 40 drifted from Bun's enum");
static_assert(mi_option_page_max_reclaim == 41, "mi_option_t slot 41 drifted from Bun's enum");
static_assert(mi_option_page_cross_thread_max_reclaim == 42, "mi_option_t slot 42 drifted from Bun's enum");

// ---------------------------------------------------------------------------
// 4. Link-time symbol check -- exactly the mi_* symbols Bun links.
// ---------------------------------------------------------------------------
//
// oven-sh/bun@main, re-derived 2026-09-02:
//   src/mimalloc_sys/mimalloc.rs   -- extern "C" fn declarations (the Rust FFI surface)
//   src/bun_alloc/MimallocArena.rs -- calls into the above
//   src/jsc/bindings/MimallocWTFMalloc.h -- WTF fastMalloc shim, #include "mimalloc.h"
//   src/jsc/modules/BunJSCModule.h -- `bun:jsc` heapStats(), #include "mimalloc.h"
//
// `mi_on_thread_idle` is declared `pub safe fn mi_on_thread_idle();` at
// mimalloc_sys.rs:30 but does not exist in this tree yet (Bun parity P7a, #299 -- open
// as of this writing). It is declared and referenced unconditionally below, same as
// every other symbol -- this TU is EXPECTED to fail to link (undefined reference to
// `mi_on_thread_idle`) until #299 merges. That is not a bug in this test: it is the
// signal `ci/check_bun_surface.py` exists to surface clearly and gate with
// `continue-on-error: true` until then. Do not silence it with an #ifdef; a guard that
// only checks the symbol when it happens to be present would stop catching a
// regression the day after #299 lands.
extern "C" {
// Not in include/mimalloc.h on this branch yet. Prototype matches what it declares on
// `bun-parity/p7-scavenger` (issue #299) exactly, INCLUDING the noexcept specifier --
// `mi_attr_noexcept` expands to `noexcept` in C++, and two extern "C" declarations of
// the same symbol with different exception specifications are a hard C++17 compile
// error. Get this wrong and the day #299 merges, this TU stops compiling instead of
// linking clean, and continue-on-error hides that from CI. Verified by building this
// exact file against the p7-scavenger branch: PASS, zero missing symbols.
mi_decl_export void mi_on_thread_idle(void) mi_attr_noexcept;
}

// The address-of array. `reinterpret_cast<void*>` on a function pointer is
// implementation-defined but universally supported on every platform this project
// targets (POSIX explicitly requires it to work for dlsym-style code); it is the
// standard idiom for "force the linker to resolve this symbol" checks like this one.
static void* const g_bun_linked_symbols[] = {
    reinterpret_cast<void*>(&mi_malloc),
    reinterpret_cast<void*>(&mi_calloc),
    reinterpret_cast<void*>(&mi_realloc),
    reinterpret_cast<void*>(&mi_expand),
    reinterpret_cast<void*>(&mi_free),
    reinterpret_cast<void*>(&mi_zalloc),
    reinterpret_cast<void*>(&mi_usable_size),
    reinterpret_cast<void*>(&mi_malloc_usable_size),
    reinterpret_cast<void*>(&mi_free_size),
    reinterpret_cast<void*>(&mi_free_size_aligned),
    reinterpret_cast<void*>(&mi_malloc_aligned),
    reinterpret_cast<void*>(&mi_zalloc_aligned),
    reinterpret_cast<void*>(&mi_realloc_aligned),
    reinterpret_cast<void*>(&mi_heap_new),
    reinterpret_cast<void*>(&mi_heap_destroy),
    reinterpret_cast<void*>(&mi_heap_main),
    reinterpret_cast<void*>(&mi_heap_malloc),
    reinterpret_cast<void*>(&mi_heap_zalloc),
    reinterpret_cast<void*>(&mi_heap_calloc),
    reinterpret_cast<void*>(&mi_heap_realloc),
    reinterpret_cast<void*>(&mi_heap_malloc_aligned),
    reinterpret_cast<void*>(&mi_heap_zalloc_aligned),
    reinterpret_cast<void*>(&mi_heap_realloc_aligned),
    reinterpret_cast<void*>(&mi_heap_visit_blocks),
    reinterpret_cast<void*>(&mi_heap_collect),
    reinterpret_cast<void*>(&mi_is_in_heap_region),
    reinterpret_cast<void*>(&mi_collect),
    reinterpret_cast<void*>(&mi_on_thread_idle),  // EXPECTED unresolved until #299 merges
    reinterpret_cast<void*>(&mi_stats_print_out),
    reinterpret_cast<void*>(&mi_process_info),
    reinterpret_cast<void*>(&mi_option_set),
    reinterpret_cast<void*>(&mi_stats_get_json),
    reinterpret_cast<void*>(&mi_heap_dump_json),
};

int main() {
  // Touch the array so it (and therefore every address-of above) cannot be optimized
  // away as dead code; the return value only needs to be observably data-dependent.
  volatile const void* sink = g_bun_linked_symbols[0];
  for (auto* p : g_bun_linked_symbols) {
    if (p == nullptr) {
      return 1;
    }
  }
  return sink == nullptr ? 1 : 0;
}
