/* ----------------------------------------------------------------------------
Copyright (c) Microsoft Research, Daan Leijen
This is free software; you can redistribute it and/or modify it under the
terms of the MIT license.
-----------------------------------------------------------------------------*/

// imported from oven-sh/mimalloc @ 942b8342, MIT (issue #271 / Bun parity P6).
// Uses the MI_DEBUG-only mi_debug_fail_os_commit_after hook (src/os.c, ported
// from the same commit) to inject an OS commit failure deterministically.

/* Reproducer for: arena_pages->pages bit and page-map entry leaked when
   mi_arenas_page_alloc_fresh / _mi_page_init fails after the bit was set
   at arena.c:773.

   Uses the MI_DEBUG-gated mi_debug_fail_os_commit_after hook (src/os.c) to
   inject a single _mi_os_commit_ex failure after N successful commits.
   With page_commit_on_demand=1 this lands inside the fresh-page commit at
   arena.c:889, after the arena_pages->pages bit is set but before the page
   is fully initialised. The failure path calls _mi_arenas_free, which
   returns the slice to slices_free but does not clear the pages bit.

   The next allocation that picks up the same slice trips
   mi_assert_internal(mi_bitmap_is_clearN(arena_pages->pages, ...)) at
   arena.c:772.

   Expected with bug present and MI_DEBUG>=1: assertion failure (abort).
*/

#include <stdio.h>
#include <stdlib.h>
#include <mimalloc.h>

#if MI_DEBUG > 0
#ifdef __cplusplus
extern "C" volatile long mi_debug_fail_os_commit_after;
#else
extern volatile long mi_debug_fail_os_commit_after;
#endif
#else
#error "test-commit-fail requires MI_DEBUG>0"
#endif

#define ALLOC_SIZE (256 * 1024) // > MI_PAGE_MIN_COMMIT_SIZE so commit-on-demand applies

int main(void) {
  // Route page allocation through the lazy-commit path so the per-page
  // commit at arena.c:889 fires (and can be made to fail) after the
  // arena_pages->pages bit is set at arena.c:773. arena_eager_commit=0 is
  // needed on overcommit OSes (Linux/macOS) where the default (=2) commits
  // the whole arena at reserve time, hiding this path.
  mi_option_set(mi_option_arena_eager_commit, 0);
  mi_option_set(mi_option_page_commit_on_demand, 1);

  // Warm up: reserve the arena and commit its metadata (arena.c:1516) so the
  // injected fault lands on the page-body commit (arena.c:889) instead.
  void* warm = mi_malloc(ALLOC_SIZE);
  mi_free(warm);
  mi_collect(true); // purge so the next alloc takes a fresh, uncommitted slice

  // Count how many commits a fresh ALLOC_SIZE alloc performs (so we can target
  // the last one if there are intervening slice/metadata commits).
  mi_debug_fail_os_commit_after = 1000;
  void* probe = mi_malloc(ALLOC_SIZE);
  long commits_per_alloc = 1000 - mi_debug_fail_os_commit_after;
  mi_debug_fail_os_commit_after = 0;
  mi_free(probe);
  mi_collect(true);
  fprintf(stderr, "test-commit-fail: fresh alloc performs %ld commit(s)\n", commits_per_alloc);
  if (commits_per_alloc == 0) {
    fprintf(stderr, "test-commit-fail: no commits on this config (eager arena?); skipping\n");
    return 0;
  }

  // Sweep fault position from 1..commits_per_alloc; the page-body commit at
  // arena.c:889 is the one that returns NULL through to mi_malloc.
  void* p = NULL;
  for (long k = 1; k <= commits_per_alloc && p == NULL; k++) {
    mi_debug_fail_os_commit_after = k;
    p = mi_malloc(ALLOC_SIZE);
    mi_debug_fail_os_commit_after = 0;
    if (p == NULL) {
      fprintf(stderr, "test-commit-fail: commit #%ld failure -> mi_malloc returned NULL\n", k);
      break;
    }
    fprintf(stderr, "test-commit-fail: commit #%ld failure absorbed (alloc=%p), retrying next position\n", k, p);
    mi_free(p);
    mi_collect(true);
    p = NULL;
  }
  if (p != NULL || mi_debug_fail_os_commit_after != 0) {
    // unreachable given the loop above, but keep the structure clear
  }

  // Re-allocate: the freed slice (with stale arena_pages->pages bit) should be
  // picked up and trip mi_assert_internal at arena.c:772. If the cleanup is
  // correct, this succeeds.
  fprintf(stderr, "test-commit-fail: re-allocating after injected failure...\n");
  void* q = mi_malloc(ALLOC_SIZE);
  fprintf(stderr, "test-commit-fail: re-alloc returned %p (no assert => bit was cleared correctly)\n", q);
  mi_free(q);
  return 0;
}
