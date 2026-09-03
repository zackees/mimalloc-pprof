/* ----------------------------------------------------------------------------
Copyright (c) 2018-2026, Microsoft Research, Daan Leijen
This is free software; you can redistribute it and/or modify it under the
terms of the MIT license.
-----------------------------------------------------------------------------*/

// ported from oven-sh/mimalloc commit 04ced98d, MIT (issue #316 / Bun parity P10a):
// "Collect the page when heap meta data is freed into it". Pure public API, so this ports
// unchanged (no adaptation for our tree's `mi_heap_t`/`mi_theap_t` split was needed).

/* test-heap-burst-destroy.c

   Destroy many heaps at once, round after round, and check that the meta data
   of a destroyed heap (`mi_heap_t`, `mi_arena_pages_t`) really goes away.

   mimalloc abandons a page once it is full. The only way such a page comes back
   is a free into it that "collects" the page: frees it when it is empty, reclaims
   it into the current theap, or re-maps it once enough blocks are free.
   `mi_heap_free` frees the meta data through `_mi_free_subproc_safe`, which did
   not collect (a collect must not cross sub-processes). Inside the same
   sub-process that stranded the block: the page stayed abandoned and resident
   for good. With enough heaps alive at the same time their meta data fills
   whole pages of the main heap, so from then on every `mi_heap_destroy` leaked
   the heap struct and, once the heap had allocated, its arena page bitmaps
   (about 150 KiB for a 1 GiB arena).

   The test counts the bytes in use in the main heap with `mi_heap_visit_blocks`
   (that walks every page of the heap, abandoned ones included, so a stranded
   block is counted) before and after the rounds. Without the fix the count
   grows by the meta data of every destroyed heap.

   > mimalloc-test-heap-burst-destroy [LIVE_HEAPS] [ROUNDS]
*/

#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>

#include "mimalloc.h"

static int failed = 0;
#define EXPECT(what, cond)  do { if (!(cond)) { fprintf(stderr, "\n  FAILED %s: %s (%s:%d)\n", what, #cond, __FILE__, __LINE__); failed++; } } while(0)

static bool count_used_bytes(const mi_heap_t* heap, const mi_heap_area_t* area, void* block, size_t block_size, void* arg) {
  (void)heap; (void)block; (void)block_size;
  if (area != NULL) {
    *((size_t*)arg) += area->used * area->block_size;
  }
  return true;
}

// Bytes in use in the main heap, over every page of it (also the abandoned ones).
static size_t main_heap_used_bytes(void) {
  size_t used = 0;
  mi_heap_visit_blocks(mi_heap_main(), false /* pages only */, &count_used_bytes, &used);
  return used;
}

static size_t current_rss(void) {
  size_t elapsed, user, sys, current_rss, peak_rss, current_commit, peak_commit, page_faults;
  mi_process_info(&elapsed, &user, &sys, &current_rss, &peak_rss, &current_commit, &peak_commit, &page_faults);
  return current_rss;
}

// One round: `live` heaps alive at the same time, each with one allocation (so
// that each heap also has its `mi_arena_pages_t`), then all of them destroyed.
static void round_of_heaps(mi_heap_t** heaps, int live, bool allocate) {
  for (int i = 0; i < live; i++) {
    heaps[i] = mi_heap_new();
    EXPECT("heap_new", heaps[i] != NULL);
    if (allocate) {
      void* p = mi_heap_malloc(heaps[i], 200);
      EXPECT("heap_malloc", p != NULL);
      memset(p, (int)(i & 0xFF), 200);
    }
  }
  for (int i = 0; i < live; i++) {
    mi_heap_destroy(heaps[i]);
    heaps[i] = NULL;
  }
}

int main(int argc, char** argv) {
  int live   = (argc > 1 ? atoi(argv[1]) : 200);
  int rounds = (argc > 2 ? atoi(argv[2]) : 20);
  if (live <= 0) live = 200;
  if (rounds <= 0) rounds = 20;

  mi_heap_t** heaps = (mi_heap_t**)mi_calloc((size_t)live, sizeof(mi_heap_t*));
  EXPECT("alloc heaps array", heaps != NULL);
  if (heaps == NULL) return 1;

  // warm up: the first round pays for thread-local slots, theap meta data, and the like
  round_of_heaps(heaps, live, true);
  const size_t used_before = main_heap_used_bytes();
  const size_t rss_before  = current_rss();

  for (int r = 0; r < rounds; r++) {
    round_of_heaps(heaps, live, true);
  }
  const size_t used_after = main_heap_used_bytes();
  const size_t rss_after  = current_rss();

  // heaps without any allocation strand only their `mi_heap_t`
  for (int r = 0; r < rounds; r++) {
    round_of_heaps(heaps, live, false);
  }
  const size_t used_after_empty = main_heap_used_bytes();

  mi_free(heaps);

  const size_t destroyed = (size_t)live * (size_t)rounds;
  printf("heap-burst-destroy: %d live heaps x %d rounds\n", live, rounds);
  printf("  main heap in use: %zu KiB before, %zu KiB after (%zu B per destroyed heap)\n",
         used_before / 1024, used_after / 1024,
         (used_after > used_before ? (used_after - used_before) / destroyed : 0));
  printf("  main heap in use after %zu empty heaps: %zu KiB (%zu B per destroyed heap)\n",
         destroyed, used_after_empty / 1024,
         (used_after_empty > used_after ? (used_after_empty - used_after) / destroyed : 0));
  printf("  rss: %zu KiB before, %zu KiB after\n", rss_before / 1024, rss_after / 1024);

  // Without the fix every destroyed heap leaves about 7 KiB (`mi_heap_t`) plus about 150 KiB
  // (`mi_arena_pages_t`) in use: `destroyed * 157 KiB`. With it the count stays where the warm-up
  // round left it; allow a few pages of slack for meta data that legitimately grows.
  const size_t slack = 4 * 64 * 1024;
  EXPECT("meta data of destroyed heaps is freed", used_after <= used_before + slack);
  EXPECT("meta data of destroyed empty heaps is freed", used_after_empty <= used_before + slack);

  if (failed > 0) {
    fprintf(stderr, "test-heap-burst-destroy: %d failure(s)\n", failed);
    return 1;
  }
  printf("test-heap-burst-destroy: ok\n");
  return 0;
}
