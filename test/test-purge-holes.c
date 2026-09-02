/* ----------------------------------------------------------------------------
Copyright (c) 2018-2025, Microsoft Research, Daan Leijen
This is free software; you can redistribute it and/or modify it under the
terms of the MIT license. A copy of the license can be found in the file
"LICENSE" at the root of this distribution.
-----------------------------------------------------------------------------*/

// imported from oven-sh/mimalloc @ 942b8342, MIT (issue #272 / Bun parity P7b).
//
// Tests for "hole punching": discarding the memory of free blocks that sit
// inside a still-used page (the idle sweep in `mi_on_thread_idle`, see the hole purging section in
// `src/page-holes.c` -- Bun keeps the engine in `src/page.c`, CLAUDE.md rule 6 puts it in its
// own file here; that file reference is the only adaptation in this test besides using
// `mi_page_sweep_state()` directly, which is a shared inline in `mimalloc/internal.h` here
// and a `page.c` static in Bun).
//
// Run with MIMALLOC_PURGE_HOLES=0 to check the feature is a no-op when off:
// every data-integrity check still runs, and we additionally assert that no
// block is ever discarded.

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <time.h>      // nanosleep, for the min_interval pacing case

#include "mimalloc.h"
#include "mimalloc-stats.h"
#include "mimalloc/internal.h"   // _mi_ptr_page, _mi_page_purged_count, _mi_page_purge_os_page_blocks

#include "testhelper.h"

static bool purging_enabled = true;   // MIMALLOC_PURGE_HOLES

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

// the process-wide hole-purging counters
typedef struct hole_stats_s {
  int64_t bytes_now;      // bytes discarded right now
  int64_t bytes_total;    // bytes ever discarded
  int64_t blocks_now;     // blocks held off the free lists right now
  int64_t discards;       // discard syscalls
  int64_t reuses;         // reuse syscalls
  int64_t pages_freed;    // pages the sweep found all-free and gave back to the arena
  int64_t inelig_pages;   // pages the last sweep could not purge at all
  int64_t inelig_bytes;
  int64_t inelig_free;
  int64_t unformed_now;       // bytes of unformed tail discarded right now
  int64_t unformed_total;     // bytes of unformed tail ever discarded
  int64_t unformed_discards;
  int64_t unformed_reuses;
  int64_t pages_skipped;      // pages the sweep skipped because nothing changed in them
  int64_t visited;            // free-list blocks the sweep walked
  int64_t full_sweeps;        // sweeps that walked every page regardless
} hole_stats_t;

static hole_stats_t hole_stats(void) {
  mi_purge_holes_stats_t s;
  mi_purge_holes_stats_get(&s);
  hole_stats_t h;
  h.bytes_now    = (int64_t)s.purged_bytes;
  h.bytes_total  = (int64_t)s.purged_bytes_total;
  h.blocks_now   = (int64_t)s.purged_blocks;
  h.discards     = (int64_t)s.discard_calls;
  h.reuses       = (int64_t)s.reuse_calls;
  h.pages_freed  = (int64_t)s.pages_freed;
  h.inelig_pages = (int64_t)s.ineligible_pages;
  h.inelig_bytes = (int64_t)s.ineligible_bytes;
  h.inelig_free  = (int64_t)s.ineligible_free_bytes;
  h.unformed_now      = (int64_t)s.unformed_bytes;
  h.unformed_total    = (int64_t)s.unformed_bytes_total;
  h.unformed_discards = (int64_t)s.unformed_discard_calls;
  h.unformed_reuses   = (int64_t)s.unformed_reuse_calls;
  h.pages_skipped     = (int64_t)s.pages_skipped;
  h.visited           = (int64_t)s.blocks_visited;
  h.full_sweeps       = (int64_t)s.full_sweeps;
  return h;
}

// a byte pattern that depends on both the block and the offset, so a hole
// punched one OS page too far in either direction shows up as a mismatch.
static uint8_t pattern_byte(size_t id, size_t off) {
  return (uint8_t)((id * 131u) ^ (off * 7u) ^ (off >> 8));
}

static void pattern_fill(void* p, size_t size, size_t id) {
  uint8_t* b = (uint8_t*)p;
  for (size_t i = 0; i < size; i++) { b[i] = pattern_byte(id, i); }
}

// returns the offset of the first mismatching byte, or `size` when intact.
static size_t pattern_check(const void* p, size_t size, size_t id) {
  const uint8_t* b = (const uint8_t*)p;
  for (size_t i = 0; i < size; i++) {
    if (b[i] != pattern_byte(id, i)) return i;
  }
  return size;
}

// total number of discarded blocks over the pages holding the given pointers
static size_t purged_blocks(void** ptrs, size_t n) {
  size_t total = 0;
  mi_page_t* last = NULL;
  for (size_t i = 0; i < n; i++) {
    if (ptrs[i] == NULL) continue;
    mi_page_t* const page = _mi_ptr_page(ptrs[i]);
    if (page == last) continue;   // pointers are handed out per page, so this dedups most of it
    last = page;
    total += _mi_page_purged_count(page);
  }
  return total;
}

// every test that depends on a purge asserts one actually happened (or, with the feature
// off, that none did) -- otherwise the test passes vacuously when the pages turn out to be
// ineligible or the sweep no-ops.
static bool expect_purged(hole_stats_t before, const char* what) {
  const hole_stats_t after = hole_stats();
  const long long dbytes = (long long)(after.bytes_total - before.bytes_total);
  const long long ddisc  = (long long)(after.discards - before.discards);
  if (purging_enabled) {
    if (dbytes <= 0 || ddisc <= 0) {
      fprintf(stderr, "\n  %s: NOTHING was purged (bytes=%lld, discards=%lld)\n", what, dbytes, ddisc);
      return false;
    }
  }
  else if (dbytes != 0 || ddisc != 0) {
    fprintf(stderr, "\n  %s: purging is off but %lld bytes were discarded in %lld calls\n", what, dbytes, ddisc);
    return false;
  }
  return true;
}

static uint32_t rng_state = 0x853c49e6;
static uint32_t rng_next(void) {
  rng_state = (rng_state * 1103515245u) + 12345u;
  return (rng_state >> 8);
}

// ---------------------------------------------------------------------------
// 1. survivors: scattered live blocks must be byte-for-byte intact after purging
// ---------------------------------------------------------------------------

// A hole is discarded one OS page at a time, so a run of free blocks only yields something
// once it covers a whole OS page: leave at least 2 OS pages worth of free blocks between the
// survivors, whatever the block size (this is what makes the small size classes purge at all).
static bool test_survivors(size_t bsize, bool* out_purged) {
  size_t npurged;
  const size_t os_page = _mi_os_page_size();
  const size_t keep_every = (((2 * os_page) + bsize - 1) / bsize) + 2;
  size_t count = keep_every * 32;
  if (count < 256) { count = 256; }

  void** ptrs = (void**)calloc(count, sizeof(void*));
  if (ptrs == NULL) return false;
  bool ok_all = true;
  size_t usable = 0;

  for (size_t i = 0; i < count; i++) {
    ptrs[i] = mi_malloc(bsize);
    if (ptrs[i] == NULL) { ok_all = false; goto done; }
    usable = mi_usable_size(ptrs[i]);
    pattern_fill(ptrs[i], usable, i);
  }
  // free all but every `keep_every`-th block
  for (size_t i = 0; i < count; i++) {
    if ((i % keep_every) != 0) { mi_free(ptrs[i]); ptrs[i] = NULL; }
  }

  mi_on_thread_idle();

  npurged = purged_blocks(ptrs, count);
  if (out_purged != NULL) { *out_purged = (npurged > 0); }
  if (!purging_enabled && npurged != 0) {
    fprintf(stderr, "\n  purging is off but %zu blocks were discarded!\n", npurged);
    ok_all = false;
  }

  // every survivor must be intact
  for (size_t i = 0; i < count; i++) {
    if (ptrs[i] == NULL) continue;
    const size_t bad = pattern_check(ptrs[i], usable, i);
    if (bad != usable) {
      fprintf(stderr, "\n  CORRUPT survivor: bsize=%zu block=%zu offset=%zu (of %zu)\n", bsize, i, bad, usable);
      ok_all = false;
      break;
    }
  }
  // and the memory must still be writable (a decommit would fault here)
  for (size_t i = 0; i < count; i++) {
    if (ptrs[i] != NULL) { memset(ptrs[i], 0xA5, usable); }
  }

done:
  for (size_t i = 0; i < count; i++) { if (ptrs[i] != NULL) mi_free(ptrs[i]); }
  free(ptrs);
  return ok_all;
}

// ---------------------------------------------------------------------------
// 2. randomized churn: no aliasing, no lost blocks, contents conserved
// ---------------------------------------------------------------------------

#define CHURN_LIVE_MAX   400
#define CHURN_ITERS      20000

typedef struct live_s {
  void*  p;
  size_t usable;
  size_t id;
} live_t;

static bool test_churn(void) {
  // sizes straddle the small/medium boundary (10240) and the 16KB OS page of arm64
  static const size_t sizes[] = { 64, 512, 1024, 4096, 8192, 10239, 10240, 10241, 16384, 16385, 24576 };
  const size_t nsizes = sizeof(sizes) / sizeof(sizes[0]);

  live_t live[CHURN_LIVE_MAX];
  size_t nlive = 0;
  size_t next_id = 1;
  size_t total_allocs = 0, total_frees = 0, total_purges = 0;
  memset(live, 0, sizeof(live));

  for (size_t iter = 0; iter < CHURN_ITERS; iter++) {
    const uint32_t r = rng_next();
    const bool do_alloc = (nlive == 0) || (nlive < CHURN_LIVE_MAX && (r % 100) < 55);

    if (do_alloc) {
      const size_t sz = sizes[rng_next() % nsizes];
      void* const p = mi_malloc(sz);
      if (p == NULL) { fprintf(stderr, "\n  out of memory\n"); return false; }
      const size_t usable = mi_usable_size(p);
      if (usable < sz) { fprintf(stderr, "\n  usable %zu < requested %zu\n", usable, sz); return false; }
      // the fresh block may not alias any live block
      for (size_t i = 0; i < nlive; i++) {
        uint8_t* const a = (uint8_t*)p;
        uint8_t* const b = (uint8_t*)live[i].p;
        if (a < b + live[i].usable && b < a + usable) {
          fprintf(stderr, "\n  ALIAS: new %p+%zu overlaps live %p+%zu\n", p, usable, live[i].p, live[i].usable);
          return false;
        }
      }
      if (((uintptr_t)p % MI_MAX_ALIGN_SIZE) != 0) {
        fprintf(stderr, "\n  misaligned block %p\n", p);
        return false;
      }
      pattern_fill(p, usable, next_id);
      live[nlive].p = p;
      live[nlive].usable = usable;
      live[nlive].id = next_id;
      nlive++; next_id++; total_allocs++;
    }
    else {
      const size_t idx = rng_next() % nlive;
      const size_t bad = pattern_check(live[idx].p, live[idx].usable, live[idx].id);
      if (bad != live[idx].usable) {
        fprintf(stderr, "\n  CORRUPT live block %p (size %zu) at offset %zu\n", live[idx].p, live[idx].usable, bad);
        return false;
      }
      mi_free(live[idx].p);
      live[idx] = live[nlive - 1];
      nlive--; total_frees++;
    }

    if ((iter % 97) == 0) { mi_on_thread_idle(); total_purges++; }

    // spot-check a couple of survivors right after a purge
    if ((iter % 97) == 1 && nlive > 0) {
      for (size_t k = 0; k < 8 && k < nlive; k++) {
        const size_t idx = rng_next() % nlive;
        const size_t bad = pattern_check(live[idx].p, live[idx].usable, live[idx].id);
        if (bad != live[idx].usable) {
          fprintf(stderr, "\n  CORRUPT after purge: %p (size %zu) at offset %zu\n", live[idx].p, live[idx].usable, bad);
          return false;
        }
      }
    }
  }

  // final: every live block is intact and writable
  for (size_t i = 0; i < nlive; i++) {
    const size_t bad = pattern_check(live[i].p, live[i].usable, live[i].id);
    if (bad != live[i].usable) {
      fprintf(stderr, "\n  CORRUPT at end: %p (size %zu) at offset %zu\n", live[i].p, live[i].usable, bad);
      return false;
    }
    memset(live[i].p, 0x5A, live[i].usable);
    mi_free(live[i].p);
  }
  fprintf(stderr, "(%zu allocs, %zu frees, %zu purges) ", total_allocs, total_frees, total_purges);
  return true;
}

// ---------------------------------------------------------------------------
// 3. aligned allocation (JSC's MarkedBlock is 16KB aligned to 16KB)
// ---------------------------------------------------------------------------

static bool test_aligned(void) {
  enum { N = 128, ALIGN = 16384 };
  void* ptrs[N];
  const hole_stats_t before = hole_stats();
  for (size_t i = 0; i < N; i++) {
    ptrs[i] = mi_malloc_aligned(ALIGN, ALIGN);
    if (ptrs[i] == NULL) return false;
    if (((uintptr_t)ptrs[i] % ALIGN) != 0) {
      fprintf(stderr, "\n  not aligned: %p\n", ptrs[i]);
      return false;
    }
    pattern_fill(ptrs[i], ALIGN, i);
  }
  // free every other block, punch holes, then take the blocks back out of the bitmap
  for (size_t i = 1; i < N; i += 2) { mi_free(ptrs[i]); ptrs[i] = NULL; }
  mi_on_thread_idle();

  // these 16KB blocks must actually have been discarded (this is the JSC MarkedBlock case)
  if (!expect_purged(before, "aligned-16k")) return false;
  const size_t npurged = purged_blocks(ptrs, N);
  if (purging_enabled && npurged == 0) {
    fprintf(stderr, "\n  aligned-16k: no block of these pages is discarded\n");
    return false;
  }
  if (!purging_enabled && npurged != 0) return false;

  for (size_t i = 1; i < N; i += 2) {
    ptrs[i] = mi_malloc_aligned(ALIGN, ALIGN);
    if (ptrs[i] == NULL) return false;
    if (((uintptr_t)ptrs[i] % ALIGN) != 0) {
      fprintf(stderr, "\n  alignment lost after unpurge: %p\n", ptrs[i]);
      return false;
    }
    memset(ptrs[i], 0xC3, ALIGN);   // must be writable
  }
  // the survivors are untouched
  for (size_t i = 0; i < N; i += 2) {
    const size_t bad = pattern_check(ptrs[i], ALIGN, i);
    if (bad != ALIGN) {
      fprintf(stderr, "\n  CORRUPT aligned survivor %zu at offset %zu\n", i, bad);
      return false;
    }
  }
  for (size_t i = 0; i < N; i++) { mi_free(ptrs[i]); }
  return true;
}

// ---------------------------------------------------------------------------
// 4. page lifecycle: hole-punched pages go back to the arena and are re-used
//    (a MEM_DECOMMIT regression would fault on the first write below, on Windows)
// ---------------------------------------------------------------------------

static bool test_page_lifecycle(void) {
  enum { N = 512, SZ = 8192 };
  void** ptrs = (void**)calloc(N, sizeof(void*));
  if (ptrs == NULL) return false;
  const hole_stats_t before = hole_stats();

  for (size_t i = 0; i < N; i++) {
    ptrs[i] = mi_malloc(SZ);
    if (ptrs[i] == NULL) { free(ptrs); return false; }
    memset(ptrs[i], (int)(i & 0xFF), SZ);
  }
  // keep one block in every page: free all but every 8th
  for (size_t i = 0; i < N; i++) {
    if ((i % 8) != 0) { mi_free(ptrs[i]); ptrs[i] = NULL; }
  }
  mi_on_thread_idle();

  // the pages we are about to recycle must really carry holes, or the test below
  // (which is what would catch a MEM_DECOMMIT on Windows) proves nothing
  if (!expect_purged(before, "page-lifecycle")) { free(ptrs); return false; }
  const size_t npurged = purged_blocks(ptrs, N);
  if (purging_enabled && npurged == 0) {
    fprintf(stderr, "\n  page-lifecycle: no block of these pages is discarded\n");
    free(ptrs);
    return false;
  }

  // now free the survivors: the pages (holes and all) return to the arena
  for (size_t i = 0; i < N; i++) {
    if (ptrs[i] != NULL) { mi_free(ptrs[i]); ptrs[i] = NULL; }
  }
  mi_collect(true);

  // force the arena to hand the same slices back out, and write every byte
  for (size_t i = 0; i < N; i++) {
    ptrs[i] = mi_malloc(SZ);
    if (ptrs[i] == NULL) { free(ptrs); return false; }
    memset(ptrs[i], 0x77, SZ);
  }
  for (size_t i = 0; i < N; i++) {
    const uint8_t* b = (const uint8_t*)ptrs[i];
    for (size_t j = 0; j < SZ; j++) {
      if (b[j] != 0x77) { fprintf(stderr, "\n  re-used page byte %zu of block %zu is %u\n", j, i, b[j]); free(ptrs); return false; }
    }
    mi_free(ptrs[i]);
  }
  free(ptrs);
  return true;
}

// ---------------------------------------------------------------------------
// 5. abandoned pages: a thread exits while blocks in its pages are still live, so
//    the pages end up in the arena's abandoned list with no owning thread. This is
//    where most of the holes are (every page that ever became full is abandoned),
//    so the idle sweep must reach them.
// ---------------------------------------------------------------------------

#define ABANDON_N   (256)
#define ABANDON_SZ  (8192)
static void* abandoned_ptrs[ABANDON_N];

static void run_one_thread(void (*fun)(void));   // joins; defined at the bottom

static void abandoned_worker(void) {
  for (size_t i = 0; i < ABANDON_N; i++) {
    abandoned_ptrs[i] = mi_malloc(ABANDON_SZ);
    if (abandoned_ptrs[i] != NULL) { pattern_fill(abandoned_ptrs[i], ABANDON_SZ, i); }
  }
  // keep one live block in every 64KB page (8 blocks of 8KB); the rest become holes
  for (size_t i = 0; i < ABANDON_N; i++) {
    if ((i % 8) != 0) { mi_free(abandoned_ptrs[i]); abandoned_ptrs[i] = NULL; }
  }
  // the thread now exits: the pages are abandoned with a live block in each
}

static bool test_abandoned(void) {
  const hole_stats_t before = hole_stats();
  memset(abandoned_ptrs, 0, sizeof(abandoned_ptrs));
  run_one_thread(&abandoned_worker);

  mi_on_thread_idle();

  const size_t npurged = purged_blocks(abandoned_ptrs, ABANDON_N);
  if (purging_enabled) {
    if (!expect_purged(before, "abandoned-pages")) return false;
    if (npurged == 0) {
      fprintf(stderr, "\n  the abandoned pages of the exited thread were not purged\n");
      return false;
    }
  }
  else if (npurged != 0) {
    fprintf(stderr, "\n  purging is off but %zu abandoned blocks were discarded\n", npurged);
    return false;
  }

  // the survivors in those pages must be intact and writable
  for (size_t i = 0; i < ABANDON_N; i++) {
    if (abandoned_ptrs[i] == NULL) continue;
    const size_t bad = pattern_check(abandoned_ptrs[i], ABANDON_SZ, i);
    if (bad != ABANDON_SZ) {
      fprintf(stderr, "\n  CORRUPT survivor in abandoned page: block %zu at offset %zu\n", i, bad);
      return false;
    }
    memset(abandoned_ptrs[i], 0x3C, ABANDON_SZ);
  }
  // re-use the holes from this thread (they are reclaimed on allocation)
  void* re[ABANDON_N];
  for (size_t i = 0; i < ABANDON_N; i++) {
    re[i] = mi_malloc(ABANDON_SZ);
    if (re[i] == NULL) return false;
    memset(re[i], 0x5E, ABANDON_SZ);
  }
  for (size_t i = 0; i < ABANDON_N; i++) { mi_free(re[i]); }
  for (size_t i = 0; i < ABANDON_N; i++) { if (abandoned_ptrs[i] != NULL) { mi_free(abandoned_ptrs[i]); abandoned_ptrs[i] = NULL; } }
  return true;
}

// ---------------------------------------------------------------------------
// 6. large pages (4MB, for blocks over ~84KB). Whether they fit the OS-page bitmap depends
//    on the OS page size: 4MB/4KB = 1024 bits does not fit, 4MB/16KB = 256 bits does. So we
//    assert what the page itself reports: either it is eligible and its holes are discarded,
//    or it is ineligible and the sweep counts it (and discards nothing). Either way its data
//    must survive.
// ---------------------------------------------------------------------------

#define LARGE_N   (32)
#define LARGE_SZ  (128 * 1024)   // > MI_MEDIUM_MAX_OBJ_SIZE, so these land in large (4MB) pages

static bool test_large_pages(void) {
  bool eligible, singleton;
  hole_stats_t before;
  hole_stats_t after;
  size_t npurged;
  void** ptrs = (void**)calloc(LARGE_N, sizeof(void*));
  if (ptrs == NULL) return false;
  bool ok_all = true;

  for (size_t i = 0; i < LARGE_N; i++) {
    ptrs[i] = mi_malloc(LARGE_SZ);
    if (ptrs[i] == NULL) { ok_all = false; goto done; }
    pattern_fill(ptrs[i], LARGE_SZ, i);
  }
  eligible = mi_page_can_purge_holes(_mi_ptr_page(ptrs[0]));
  singleton = (_mi_ptr_page(ptrs[0])->reserved <= 1);
  for (size_t i = 1; i < LARGE_N; i += 2) { mi_free(ptrs[i]); ptrs[i] = NULL; }

  before = hole_stats();
  mi_on_thread_idle();
  after = hole_stats();
  npurged = purged_blocks(ptrs, LARGE_N);

  if (purging_enabled && eligible) {
    if (npurged == 0) {
      fprintf(stderr, "\n  the large pages are eligible but nothing was discarded in them\n");
      ok_all = false;
    }
    if (!expect_purged(before, "large-pages")) { ok_all = false; }
  }
  else if (purging_enabled && !eligible) {
    if (npurged != 0) {
      fprintf(stderr, "\n  an ineligible large page was purged after all\n");
      ok_all = false;
    }
    // (a singleton page -- 32-bit, where 128 KiB does not fit a large page -- is full while in use, and a full page
    //  may be abandoned, out of reach of the sweep; there is nothing to purge or report in it either way)
    if (!singleton && (after.inelig_pages <= 0 || after.inelig_free <= 0)) {
      fprintf(stderr, "\n  the ineligible large pages are not reported (%lld pages, %lld free bytes)\n",
              (long long)after.inelig_pages, (long long)after.inelig_free);
      ok_all = false;
    }
  }
  else if (npurged != 0) {
    fprintf(stderr, "\n  purging is off but a large page was purged\n");
    ok_all = false;
  }

  for (size_t i = 0; i < LARGE_N; i += 2) {
    const size_t bad = pattern_check(ptrs[i], LARGE_SZ, i);
    if (bad != LARGE_SZ) {
      fprintf(stderr, "\n  CORRUPT large-page survivor %zu at offset %zu\n", i, bad);
      ok_all = false;
      break;
    }
    memset(ptrs[i], 0x6D, LARGE_SZ);   // and still writable
  }
  fprintf(stderr, "(%s; %lld ineligible pages, %lld bytes, %lld of them free) ",
          (eligible ? "eligible" : "INELIGIBLE"),
          (long long)after.inelig_pages, (long long)after.inelig_bytes, (long long)after.inelig_free);

done:
  for (size_t i = 0; i < LARGE_N; i++) { if (ptrs[i] != NULL) mi_free(ptrs[i]); }
  free(ptrs);
  return ok_all;
}

// ---------------------------------------------------------------------------
// 6. option off == no-op
// ---------------------------------------------------------------------------

static bool test_option_off(void) {
  enum { N = 256, SZ = 4096 };
  void** ptrs = (void**)calloc(N, sizeof(void*));
  if (ptrs == NULL) return false;
  bool ok_all = true;

  const long saved = mi_option_get(mi_option_purge_holes);
  mi_option_set(mi_option_purge_holes, 0);
  const hole_stats_t before = hole_stats();

  for (size_t i = 0; i < N; i++) {
    ptrs[i] = mi_malloc(SZ);
    if (ptrs[i] == NULL) { ok_all = false; goto done; }
    pattern_fill(ptrs[i], SZ, i);
  }
  for (size_t i = 0; i < N; i++) {
    if ((i % 4) != 0) { mi_free(ptrs[i]); ptrs[i] = NULL; }
  }
  mi_on_thread_idle();

  if (purged_blocks(ptrs, N) != 0) {
    fprintf(stderr, "\n  purge_holes=0 but blocks were discarded\n");
    ok_all = false;
  }
  if (hole_stats().unformed_total != before.unformed_total) {
    fprintf(stderr, "\n  purge_holes=0 but unformed tail bytes were discarded\n");
    ok_all = false;
  }
  for (size_t i = 0; i < N; i++) {
    if (ptrs[i] == NULL) continue;
    if (pattern_check(ptrs[i], SZ, i) != SZ) { ok_all = false; break; }
  }

done:
  for (size_t i = 0; i < N; i++) { if (ptrs[i] != NULL) mi_free(ptrs[i]); }
  free(ptrs);
  mi_option_set(mi_option_purge_holes, saved);
  return ok_all;
}

// ---------------------------------------------------------------------------
// 6b. the unformed tail: the blocks in `[capacity,reserved)` of a page are never
//     handed out, but on a recycled arena slice their memory is already resident.
//     The sweep must discard the OS pages that lie wholly inside that tail, and
//     `mi_page_extend_free` must hand them back before it formats a block in one.
// ---------------------------------------------------------------------------

// Dirty a lot of arena slices and give them all back, so the pages we allocate next are
// carved from recycled (already resident) memory -- which is what makes the unformed tail
// cost anything at all.
static void dirty_arena_slices(size_t bsize, size_t n) {
  void** ptrs = (void**)calloc(n, sizeof(void*));
  if (ptrs == NULL) return;
  for (size_t i = 0; i < n; i++) {
    ptrs[i] = mi_malloc(bsize);
    if (ptrs[i] != NULL) { memset(ptrs[i], 0x5A, bsize); }
  }
  for (size_t i = 0; i < n; i++) { if (ptrs[i] != NULL) mi_free(ptrs[i]); }
  free(ptrs);
  mi_collect(true);   // hand the pages back to the arena
}

static bool malloc_is_mimalloc(void) {
  void* p = malloc(24);
  const bool is = mi_is_in_heap_region(p);
  free(p);
  return is;
}

// The page holding `p` must have a tail worth discarding (several OS pages).
static bool tail_page_is_interesting(const mi_page_t* page, const char* what) {
  const size_t os_page = _mi_os_page_size();
  if (page->capacity >= page->reserved) {
    fprintf(stderr, "\n  %s: the page has no unformed tail (capacity == reserved == %u)\n", what, page->reserved);
    return false;
  }
  const size_t tail = ((size_t)page->reserved - page->capacity) * page->block_size;
  if (tail < 2 * os_page) {
    fprintf(stderr, "\n  %s: the unformed tail is only %zu bytes (< 2 OS pages)\n", what, tail);
    return false;
  }
  return true;
}

// (a) the tail is discarded, and (b) the blocks later formed in it are usable and keep what
// is written into them.
static bool test_unformed_tail(void) {
  const size_t bsize = 64;
  dirty_arena_slices(bsize, 20000);

  void* first = mi_malloc(bsize);
  if (first == NULL) return false;
  mi_page_t* const page = _mi_ptr_page(first);
  const size_t usable = mi_usable_size(first);
  const uint16_t cap0 = page->capacity;
  const uint16_t reserved = page->reserved;
  bool ok = true;
  void** ptrs = NULL;
  size_t nlive = 0;

  if (!tail_page_is_interesting(page, "unformed-tail")) { mi_free(first); return false; }

  const hole_stats_t before = hole_stats();
  mi_on_thread_idle();
  const hole_stats_t after = hole_stats();
  const size_t discarded = _mi_page_unformed_purged_bytes(page);

  // (a) the mechanism: THIS page's tail must be discarded (not some other page's)
  if (purging_enabled) {
    if (discarded == 0) {
      fprintf(stderr, "\n  unformed-tail: the tail of the page was NOT discarded (capacity=%u reserved=%u bsize=%zu)\n",
              cap0, reserved, bsize);
      ok = false;
    }
    if (after.unformed_total <= before.unformed_total || after.unformed_discards <= before.unformed_discards) {
      fprintf(stderr, "\n  unformed-tail: no unformed bytes were discarded (bytes=%lld calls=%lld)\n",
              (long long)(after.unformed_total - before.unformed_total),
              (long long)(after.unformed_discards - before.unformed_discards));
      ok = false;
    }
  }
  else if (discarded != 0 || after.unformed_total != before.unformed_total) {
    fprintf(stderr, "\n  unformed-tail: purging is off but %zu bytes of tail were discarded\n", discarded);
    ok = false;
  }
  if (!ok) { mi_free(first); return false; }

  // the discarded range must lie strictly inside the unformed tail: never the page header,
  // never a formed block, never past the end of the page
  const uintptr_t pstart = (uintptr_t)mi_page_start(page);
  if (discarded > 0) {
    const uintptr_t lo = pstart + page->unformed_purged_lo;
    const uintptr_t hi = pstart + page->unformed_purged_hi;
    const size_t stride = mi_page_block_size(page);   // includes the debug padding
    if (lo < pstart + ((size_t)cap0 * stride) || hi > pstart + ((size_t)reserved * stride)) {
      fprintf(stderr, "\n  unformed-tail: the discarded range [%zu,%zu) escapes the unformed tail [%zu,%zu)!\n",
              (size_t)page->unformed_purged_lo, (size_t)page->unformed_purged_hi, (size_t)cap0 * stride, (size_t)reserved * stride);
      mi_free(first);
      return false;
    }
  }

  // (b) fill the page: every block must be usable and keep its contents. This drives
  //     `mi_page_extend_free` straight through the discarded tail, a few OS pages at a time.
  ptrs = (void**)calloc(reserved, sizeof(void*));
  if (ptrs == NULL) { mi_free(first); return false; }
  ptrs[0] = first;
  pattern_fill(first, usable, 0);
  nlive = 1;
  for (size_t i = 1; i < (size_t)reserved; i++) {
    void* const q = mi_malloc(bsize);
    if (q == NULL) { ok = false; goto done; }
    if (_mi_ptr_page(q) != page) { mi_free(q); break; }   // this page is full: the rest comes from another one
    ptrs[i] = q;
    nlive++;
    pattern_fill(q, usable, i);
  }

  if (page->capacity <= cap0) {
    fprintf(stderr, "\n  unformed-tail: the page never extended (capacity is still %u)\n", page->capacity);
    ok = false;
    goto done;
  }
  // the tail we discarded is formed by now, so nothing of it may still be discarded
  if (page->capacity >= page->reserved && _mi_page_unformed_purged_bytes(page) != 0) {
    fprintf(stderr, "\n  unformed-tail: the page is fully formed but %zu bytes of tail are still discarded\n",
            _mi_page_unformed_purged_bytes(page));
    ok = false;
    goto done;
  }
  if (purging_enabled && hole_stats().unformed_reuses <= before.unformed_reuses) {
    fprintf(stderr, "\n  unformed-tail: the page extended into the discarded tail without a reuse call\n");
    ok = false;
    goto done;
  }
  // every block handed out of the once-discarded tail must have kept what we wrote into it
  for (size_t i = 0; i < nlive; i++) {
    const size_t bad = pattern_check(ptrs[i], usable, i);
    if (bad != usable) {
      fprintf(stderr, "\n  unformed-tail: CORRUPT block %zu at offset %zu (of %zu)\n", i, bad, usable);
      ok = false;
      break;
    }
  }

done:
  for (size_t i = 0; i < nlive; i++) { if (ptrs[i] != NULL) mi_free(ptrs[i]); }
  free(ptrs);
  return ok;
}

// A page whose tail was discarded and then extended into, and one whose tail was discarded and
// never extended into, both go back to the arena without tripping an assertion -- and the arena
// hands the memory out again as writable (this is what a decommit here would break on Windows).
static bool test_unformed_tail_freed(void) {
  const size_t bsize = 128;
  dirty_arena_slices(bsize, 10000);

  void* keep = mi_malloc(bsize);
  if (keep == NULL) return false;
  mi_page_t* const page = _mi_ptr_page(keep);
  if (!tail_page_is_interesting(page, "unformed-tail-freed")) { mi_free(keep); return false; }

  mi_on_thread_idle();
  const size_t discarded = _mi_page_unformed_purged_bytes(page);
  if (purging_enabled && discarded == 0) {
    fprintf(stderr, "\n  unformed-tail-freed: the tail of the page was not discarded\n");
    mi_free(keep);
    return false;
  }

  // free the only live block: the page goes back to the arena with its tail still discarded.
  // (Compare deltas, not absolutes: when malloc is overridden the C runtime has live pages of its
  // own whose tails the sweep above discarded too, and those stay.)
  const hole_stats_t before = hole_stats();
  mi_free(keep);
  mi_collect(true);

  const hole_stats_t after = hole_stats();
  if (before.unformed_now - after.unformed_now < (long long)discarded) {
    fprintf(stderr, "\n  unformed-tail-freed: only %lld of the %zu discarded tail bytes came back when the page was freed\n",
            (long long)(before.unformed_now - after.unformed_now), discarded);
    return false;
  }

  // take the memory back from the arena and write every byte of it
  enum { N = 4000 };
  void** ptrs = (void**)calloc(N, sizeof(void*));
  if (ptrs == NULL) return false;
  bool ok = true;
  for (size_t i = 0; i < N; i++) {
    ptrs[i] = mi_malloc(bsize);
    if (ptrs[i] == NULL) { ok = false; break; }
    memset(ptrs[i], 0x3C, bsize);
  }
  for (size_t i = 0; i < N && ok; i++) {
    const uint8_t* const b = (const uint8_t*)ptrs[i];
    for (size_t j = 0; j < bsize; j++) {
      if (b[j] != 0x3C) {
        fprintf(stderr, "\n  unformed-tail-freed: re-used byte %zu of block %zu is %u\n", j, i, b[j]);
        ok = false;
        break;
      }
    }
  }
  for (size_t i = 0; i < N; i++) { if (ptrs[i] != NULL) mi_free(ptrs[i]); }
  free(ptrs);
  mi_collect(true);
  return ok;
}

// ---------------------------------------------------------------------------
// 7. unit test of the OS-page -> overlapping-blocks arithmetic. Everything else
//    is derived from it: getting `last` one block short is a silent over-discard
//    of a live block. (this is also what catches the arm64/16KB bug class on a
//    4KB CI box)
// ---------------------------------------------------------------------------

// brute force: does block `i` overlap the byte range [lo,hi)?
static bool block_overlaps(size_t i, size_t bsize, uintptr_t pstart, uintptr_t lo, uintptr_t hi) {
  const uintptr_t blo = pstart + (i * bsize);
  const uintptr_t bhi = blo + bsize;
  return (blo < hi && lo < bhi);
}

static bool check_os_page_blocks(size_t ospage, size_t bsize, uintptr_t pstart, size_t capacity, size_t k) {
  size_t first = 0, last = 0;
  const bool inside = _mi_page_purge_os_page_blocks(ospage, bsize, pstart, capacity, k, &first, &last);

  const uintptr_t base = pstart & ~(uintptr_t)(ospage - 1);
  const uintptr_t lo = base + (k * ospage);
  const uintptr_t hi = lo + ospage;
  const uintptr_t pend = pstart + (capacity * bsize);
  const bool want_inside = (lo >= pstart && hi <= pend);

  if (inside != want_inside) {
    fprintf(stderr, "\n  inside=%d but the OS page [%p,%p) vs area [%p,%p) says %d\n",
            (int)inside, (void*)lo, (void*)hi, (void*)pstart, (void*)pend, (int)want_inside);
    return false;
  }
  if (!inside) {
    if (first != 0 || last != 0) { fprintf(stderr, "\n  not inside but got [%zu,%zu]\n", first, last); return false; }
    return true;
  }
  // [first,last] must be EXACTLY the blocks overlapping the OS page
  for (size_t i = 0; i < capacity; i++) {
    const bool overlaps = block_overlaps(i, bsize, pstart, lo, hi);
    const bool reported = (i >= first && i <= last);
    if (overlaps != reported) {
      fprintf(stderr, "\n  block %zu overlaps=%d but reported=%d (range [%zu,%zu], os page [%p,%p))\n",
              i, (int)overlaps, (int)reported, first, last, (void*)lo, (void*)hi);
      return false;
    }
  }
  if (last >= capacity) { fprintf(stderr, "\n  last=%zu is beyond capacity=%zu\n", last, capacity); return false; }
  return true;
}

static bool test_os_page_arithmetic(void) {
  static const size_t ospages[] = { 4096, 16384 };
  static const size_t bsizes[]  = { 16, 32, 64, 512, 1024, 4096, 8192, 10240, 10256, 16384, 21856, 65536 };
  // page_start offsets: the block area does not have to be OS-page aligned
  static const size_t offsets[] = { 0, 8, 64, 128, 4096, 8192, 12288 };

  size_t cases = 0;
  for (size_t oi = 0; oi < sizeof(ospages)/sizeof(ospages[0]); oi++) {
    const size_t ospage = ospages[oi];
    for (size_t bi = 0; bi < sizeof(bsizes)/sizeof(bsizes[0]); bi++) {
      const size_t bsize = bsizes[bi];
      for (size_t fi = 0; fi < sizeof(offsets)/sizeof(offsets[0]); fi++) {
        const uintptr_t pstart = (uintptr_t)0x40000000u + offsets[fi];
        // a 64KB and a 512KB page worth of blocks, plus a degenerate one
        const size_t caps[] = { 0, 1, (64*1024) / bsize, (512*1024) / bsize };
        for (size_t ci = 0; ci < sizeof(caps)/sizeof(caps[0]); ci++) {
          const size_t capacity = caps[ci];
          const size_t nk = (((capacity * bsize) + (pstart - (pstart & ~(uintptr_t)(ospage-1)))) / ospage) + 2;
          for (size_t k = 0; k < nk; k++) {
            if (!check_os_page_blocks(ospage, bsize, pstart, capacity, k)) {
              fprintf(stderr, "  (case: ospage=%zu bsize=%zu offset=%zu capacity=%zu k=%zu)\n",
                      ospage, bsize, offsets[fi], capacity, k);
              return false;
            }
            cases++;
          }
        }
      }
    }
  }
  fprintf(stderr, "(%zu cases) ", cases);
  return true;
}

// ---------------------------------------------------------------------------
// 8. the hole report (`mi_purge_holes_report`): is its "undiscardable" number the truth?
//
//    We build a KNOWN pinned situation -- exactly one live block per OS page -- then
//    recompute what the report must say from first principles and from the test's OWN
//    record of which blocks are live (never from the allocator's free lists), and compare.
//    The report is what we will use to chase the memory JSC still retains: if it lies, it
//    sends us after the wrong bug.
// ---------------------------------------------------------------------------

static bool bit_get(const uint64_t* s, size_t i) { return ((s[i / 64] >> (i % 64)) & 1) != 0; }

typedef struct expect_s {
  size_t live, freeb, undisc, disc, pending, pinned_ospages, pinned_live;
  size_t hist[MI_HOLES_HIST_BUCKETS];
} expect_t;

static size_t expect_hist_bucket(size_t n) {
  if (n <= 1) return 0;
  if (n == 2) return 1;
  if (n <= 4) return 2;
  if (n <= 8) return 3;
  return 4;
}

// `live` is the test's own live set for this page. This also asserts the allocator discarded
// EXACTLY the OS pages that lie entirely inside the block area and hold no live block --
// which is the definition the report's "undiscardable" rests on.
static bool expect_page(mi_page_t* page, const uint64_t* live, expect_t* e) {
  const size_t os = _mi_os_page_size();
  const size_t bs = page->block_size;
  const size_t cap = page->capacity;
  const uintptr_t pstart = (uintptr_t)mi_page_start(page);
  const uintptr_t pend = pstart + (cap * bs);
  const uintptr_t base = pstart & ~(uintptr_t)(os - 1);
  const size_t nbits = (size_t)(((pstart + mi_page_size(page)) - base + os - 1) / os);

  for (size_t k = 0; k < nbits; k++) {
    const uintptr_t lo = base + (k * os);
    const uintptr_t hi = lo + os;
    const uintptr_t clo = (lo < pstart ? pstart : lo);
    const uintptr_t chi = (hi > pend ? pend : hi);
    if (clo >= chi) continue;   // no block byte lands in this OS page
    const bool whole = (lo >= pstart && hi <= pend);
    const size_t first = (size_t)(clo - pstart) / bs;
    const size_t last = (size_t)((chi - 1) - pstart) / bs;
    size_t live_ov = 0, free_ov = 0, nlive = 0;
    for (size_t idx = first; idx <= last && idx < cap; idx++) {
      const uintptr_t blo = pstart + (idx * bs);
      const uintptr_t bhi = blo + bs;
      const uintptr_t olo = (blo < lo ? lo : blo);
      const uintptr_t ohi = (bhi > hi ? hi : bhi);
      const size_t ov = (size_t)(ohi - olo);
      if (bit_get(live, idx)) { live_ov += ov; nlive++; }
      else { free_ov += ov; }
    }
    const bool want_purged = purging_enabled && whole && (nlive == 0);
    const bool got_purged = mi_page_os_page_purged(page, k);
    if (want_purged != got_purged) {
      fprintf(stderr, "\n  os page %zu of page %p: purged=%d, expected %d (nlive=%zu, whole=%d)\n",
              k, (void*)page, (int)got_purged, (int)want_purged, nlive, (int)whole);
      return false;
    }
    e->live += live_ov;
    e->freeb += free_ov;
    if (got_purged) { e->disc += os; }
    else if (!whole) { e->undisc += free_ov; }   // never discardable, live block in it or not -- so NOT "pinned"
    else if (nlive > 0) {
      e->undisc += free_ov;
      e->pinned_ospages++;
      e->pinned_live += nlive;
      e->hist[expect_hist_bucket(nlive)]++;
    }
    else { e->pending += free_ov; }
  }
  return true;
}

#define REPORT_N   (2048)
#define REPORT_SZ  (512)

// mode 0: keep one live block per OS page       -> nothing is discardable (maximally pinned)
// mode 1: keep one live block per mimalloc page -> almost everything is discardable
static bool test_holes_report(int mode) {
  const size_t os = _mi_os_page_size();
  void** ptrs = (void**)calloc(REPORT_N, sizeof(void*));
  uintptr_t* kept = (uintptr_t*)calloc(REPORT_N, sizeof(uintptr_t));
  mi_page_t** pages = (mi_page_t**)calloc(REPORT_N, sizeof(mi_page_t*));
  mi_holes_report_t* rep = (mi_holes_report_t*)calloc(1, sizeof(mi_holes_report_t));
  size_t nkept = 0, npages = 0, bs = 0, block_total = 0;
  const mi_holes_bin_t* r = NULL;
  expect_t e;
  bool ok = true;
  memset(&e, 0, sizeof(e));
  if (ptrs == NULL || kept == NULL || pages == NULL || rep == NULL) { ok = false; goto done; }

  for (size_t i = 0; i < REPORT_N; i++) {
    ptrs[i] = mi_malloc(REPORT_SZ);
    if (ptrs[i] == NULL) { ok = false; goto done; }
    pattern_fill(ptrs[i], REPORT_SZ, i);
  }
  // keep the first block that starts in each OS page (mode 0) / in each mimalloc page (mode 1)
  for (size_t i = 0; i < REPORT_N; i++) {
    const uintptr_t key = (mode == 0
                             ? ((uintptr_t)ptrs[i] & ~(uintptr_t)(os - 1))
                             : (uintptr_t)_mi_ptr_page(ptrs[i]));
    bool seen = false;
    for (size_t j = 0; j < nkept; j++) { if (kept[j] == key) { seen = true; break; } }
    if (seen) { mi_free(ptrs[i]); ptrs[i] = NULL; }
    else { kept[nkept++] = key; }
  }

  mi_on_thread_idle();

  for (size_t i = 0; i < REPORT_N; i++) {
    if (ptrs[i] == NULL) continue;
    mi_page_t* const page = _mi_ptr_page(ptrs[i]);
    bool seen = false;
    for (size_t j = 0; j < npages; j++) { if (pages[j] == page) { seen = true; break; } }
    if (!seen) { pages[npages++] = page; }
  }
  if (npages == 0) { fprintf(stderr, "\n  no pages left to report on\n"); ok = false; goto done; }
  bs = pages[0]->block_size;

  for (size_t j = 0; j < npages; j++) {
    mi_page_t* const page = pages[j];
    if (page->block_size != bs) { fprintf(stderr, "\n  mixed block sizes in one bin\n"); ok = false; goto done; }
    const size_t cap = page->capacity;
    uint64_t* const live = (uint64_t*)calloc((cap + 63) / 64, sizeof(uint64_t));
    if (live == NULL) { ok = false; goto done; }
    size_t nmine = 0;
    for (size_t i = 0; i < REPORT_N; i++) {
      if (ptrs[i] == NULL || _mi_ptr_page(ptrs[i]) != page) continue;
      const size_t idx = (size_t)((uint8_t*)ptrs[i] - mi_page_start(page)) / bs;
      live[idx / 64] |= ((uint64_t)1 << (idx % 64));
      nmine++;
    }
    // the ground truth assumes every OTHER block in the page is free, so the page must be
    // exclusively ours (`used` also counts uncollected thread frees; this test is single-threaded)
    if (page->used != nmine) {
      fprintf(stderr, "\n  page %p is not exclusively ours: used=%zu but we hold %zu\n", (void*)page, (size_t)page->used, nmine);
      free(live);
      ok = false; goto done;
    }
    const bool page_ok = expect_page(page, live, &e);
    free(live);
    if (!page_ok) { ok = false; goto done; }
    _mi_page_holes_report_page(page, rep);
    block_total += cap * bs;
  }

  r = &rep->bin[_mi_bin(bs)];
  #define REPORT_EQ(field, want)                                                                          \
    if ((size_t)(r->field) != (size_t)(want)) {                                                           \
      fprintf(stderr, "\n  report." #field " = %zu but the hand-computed value is %zu (mode %d, bs %zu)\n", \
              (size_t)(r->field), (size_t)(want), mode, bs);                                              \
      ok = false;                                                                                         \
    }
  REPORT_EQ(pages, npages);
  REPORT_EQ(live_bytes, e.live);
  REPORT_EQ(free_bytes, e.freeb);
  REPORT_EQ(undiscardable_bytes, e.undisc);
  REPORT_EQ(discarded_bytes, e.disc);
  REPORT_EQ(pending_bytes, e.pending);
  REPORT_EQ(pinned_ospages, e.pinned_ospages);
  REPORT_EQ(pinned_live_blocks, e.pinned_live);
  for (size_t h = 0; h < MI_HOLES_HIST_BUCKETS; h++) { REPORT_EQ(hist[h], e.hist[h]); }
  #undef REPORT_EQ
  if (!ok) goto done;

  // every byte of every formed block is accounted for, exactly once
  if (r->live_bytes + r->free_bytes != block_total) {
    fprintf(stderr, "\n  live+free = %zu but the pages hold %zu bytes of blocks\n", r->live_bytes + r->free_bytes, block_total);
    ok = false; goto done;
  }

  // The granularity curve, checked against the accounting above at the ONE granularity where the
  // two must agree: at the real OS page size, "would be discardable" is exactly "is discarded, or
  // is a fully free OS page the sweep has not gotten to". If this identity does not hold, the
  // curve -- the number the page-size hypothesis rests on -- is measuring something else.
  {
    const size_t os = _mi_os_page_size();
    size_t gi = MI_HOLES_GRAN_COUNT;
    for (size_t g = 0; g < MI_HOLES_GRAN_COUNT; g++) { if (mi_holes_granularity(g) == os) { gi = g; break; } }
    if (gi == MI_HOLES_GRAN_COUNT) { fprintf(stderr, "\n  the curve has no entry for the real OS page size %zu\n", os); ok = false; goto done; }
    const size_t want = r->discarded_bytes + r->pending_bytes;
    if (rep->discardable_at[gi] != want) {
      fprintf(stderr, "\n  curve@%zu = %zu but discarded+pending = %zu\n", os, rep->discardable_at[gi], want);
      ok = false; goto done;
    }
    // and it must be monotone: a smaller granularity can only ever discard more
    for (size_t g = 1; g < MI_HOLES_GRAN_COUNT; g++) {
      if (rep->discardable_at[g] > rep->discardable_at[g - 1]) {
        fprintf(stderr, "\n  curve is not monotone: @%zu = %zu > @%zu = %zu\n",
                mi_holes_granularity(g), rep->discardable_at[g], mi_holes_granularity(g - 1), rep->discardable_at[g - 1]);
        ok = false; goto done;
      }
    }
    fprintf(stderr, "[curve 4K=%zu 8K=%zu 16K=%zu 32K=%zu 64K=%zu] ",
            rep->discardable_at[0], rep->discardable_at[1], rep->discardable_at[2],
            rep->discardable_at[3], rep->discardable_at[4]);
  }

  if (mode == 0) {
    // the whole point: with one live block per OS page nothing around them is discardable, so every free
    // byte next to a live block is undiscardable -- the amplification we are chasing in JSC. (Not every free
    // byte: the last page can have formed blocks past the last one we were handed -- how far
    // `mi_page_extend_free` reaches ahead depends on the configuration and, under MI_SECURE, on a random
    // draw -- and an OS page of only those holds no live block and is discarded or pending. The per-page
    // ground truth above accounts for those exactly; here we only check the split adds up.)
    if (r->undiscardable_bytes + r->discarded_bytes + r->pending_bytes != r->free_bytes) {
      fprintf(stderr, "\n  mode 0: undiscardable %zu + discarded %zu + pending %zu != free %zu\n",
              r->undiscardable_bytes, r->discarded_bytes, r->pending_bytes, r->free_bytes);
      ok = false; goto done;
    }
    if (r->pinned_ospages == 0) { fprintf(stderr, "\n  mode 0: no pinned OS page\n"); ok = false; goto done; }
    fprintf(stderr, "(bs=%zu: %zu pinned OS pages hold %zu live blocks and pin %zu free bytes; %zux amplification) ",
            bs, r->pinned_ospages, r->pinned_live_blocks, r->undiscardable_bytes,
            (r->live_bytes == 0 ? (size_t)0 : (r->live_bytes + r->undiscardable_bytes) / r->live_bytes));
  }
  else if (purging_enabled) {
    // one live block per 64KB page: most OS pages are fully free and must be gone
    if (r->discarded_bytes == 0) {
      fprintf(stderr, "\n  mode 1: nothing was discarded, so the report distinguishes nothing\n");
      ok = false; goto done;
    }
// (how much is discardable next to the one pinned OS page per page depends on the OS page size and on how far
    // the page's capacity was extended; the exact split was checked against the per-page ground truth above)
    fprintf(stderr, "(bs=%zu: %zu bytes discarded, %zu still pinned) ", bs, r->discarded_bytes, r->undiscardable_bytes);
  }

  // the report must not have moved anything: the survivors are intact
  for (size_t i = 0; i < REPORT_N; i++) {
    if (ptrs[i] == NULL) continue;
    const size_t bad = pattern_check(ptrs[i], REPORT_SZ, i);
    if (bad != REPORT_SZ) {
      fprintf(stderr, "\n  CORRUPT survivor %zu at offset %zu after the report\n", i, bad);
      ok = false; goto done;
    }
  }

done:
  if (ptrs != NULL) {
    for (size_t i = 0; i < REPORT_N; i++) { if (ptrs[i] != NULL) mi_free(ptrs[i]); }
    free(ptrs);
  }
  free(kept); free(pages); free(rep);
  return ok;
}

// the whole-process report must not purge, un-purge, or free anything, and must be self-consistent
static bool test_report_is_read_only(void) {
  hole_stats_t before;
  hole_stats_t after;
  enum { N = 1024, SZ = 1024 };
  void** ptrs = (void**)calloc(N, sizeof(void*));
  mi_holes_report_t* rep = (mi_holes_report_t*)calloc(1, sizeof(mi_holes_report_t));
  size_t total_disc = 0;
  bool ok = true;
  if (ptrs == NULL || rep == NULL) { free(ptrs); free(rep); return false; }
  for (size_t i = 0; i < N; i++) {
    ptrs[i] = mi_malloc(SZ);
    if (ptrs[i] == NULL) { ok = false; goto done; }
    pattern_fill(ptrs[i], SZ, i);
  }
  // keep every 64th block: whatever the block size, the free runs between the survivors cover
  // several whole OS pages, so there is always something for the sweep to discard
  for (size_t i = 0; i < N; i++) { if ((i % 64) != 0) { mi_free(ptrs[i]); ptrs[i] = NULL; } }
  mi_on_thread_idle();

  before = hole_stats();
  mi_purge_holes_report();               // the public entry point: prints the table
  _mi_purge_holes_report_collect(rep);   // and again, for the numbers
  after = hole_stats();

  if (before.bytes_now != after.bytes_now || before.discards != after.discards ||
      before.reuses != after.reuses || before.pages_freed != after.pages_freed) {
    fprintf(stderr, "\n  the report is not read-only: bytes %lld->%lld, discards %lld->%lld, reuses %lld->%lld, pages freed %lld->%lld\n",
            (long long)before.bytes_now, (long long)after.bytes_now, (long long)before.discards, (long long)after.discards,
            (long long)before.reuses, (long long)after.reuses, (long long)before.pages_freed, (long long)after.pages_freed);
    ok = false; goto done;
  }
  for (size_t bin = 0; bin < MI_BIN_COUNT; bin++) {
    const mi_holes_bin_t* const r = &rep->bin[bin];
    if (r->undiscardable_bytes + r->discarded_bytes + r->pending_bytes > r->free_bytes) {
      fprintf(stderr, "\n  bin %zu: undiscardable+discarded+pending %zu > free %zu\n", bin,
              r->undiscardable_bytes + r->discarded_bytes + r->pending_bytes, r->free_bytes);
      ok = false; goto done;
    }
    total_disc += r->discarded_bytes;
  }
  if (purging_enabled && total_disc == 0) {
    fprintf(stderr, "\n  the report sees no discarded bytes at all\n");
    ok = false; goto done;
  }
  if (!purging_enabled && total_disc != 0) {
    fprintf(stderr, "\n  purging is off but the report claims %zu discarded bytes\n", total_disc);
    ok = false; goto done;
  }
  for (size_t i = 0; i < N; i++) {
    if (ptrs[i] == NULL) continue;
    if (pattern_check(ptrs[i], SZ, i) != SZ) { fprintf(stderr, "\n  CORRUPT after the report\n"); ok = false; goto done; }
    memset(ptrs[i], 0x9B, SZ);   // and still writable
  }

done:
  if (ptrs != NULL) {
    for (size_t i = 0; i < N; i++) { if (ptrs[i] != NULL) mi_free(ptrs[i]); }
    free(ptrs);
  }
  free(rep);
  return ok;
}

// The abandoned pages are where most of the holes are (every page that ever became full ends up
// there), so a report that cannot see them would be describing the wrong heap. And they are where
// the report is most at risk of not being read-only: reaching one means claiming it through the
// arena's ownership protocol. This checks BOTH: the report reaches every abandoned page we made,
// and it changes nothing while doing so.
#define RO_N   (512)
#define RO_SZ  (1024)
static void* ro_ptrs[RO_N];

static void ro_worker(void) {
  for (size_t i = 0; i < RO_N; i++) {
    ro_ptrs[i] = mi_malloc(RO_SZ);
    if (ro_ptrs[i] != NULL) { pattern_fill(ro_ptrs[i], RO_SZ, i); }
  }
  // keep one block in every 32 and exit: the pages are abandoned, each with a live block and a
  // long run of free blocks (so the sweep has whole OS pages to discard in them)
  for (size_t i = 0; i < RO_N; i++) {
    if ((i % 32) != 0) { mi_free(ro_ptrs[i]); ro_ptrs[i] = NULL; }
  }
}

static bool test_report_read_only_abandoned(void) {
  hole_stats_t before;
  hole_stats_t after;
  mi_page_t* pages[RO_N];
  mi_holes_report_t* rep = (mi_holes_report_t*)calloc(1, sizeof(mi_holes_report_t));
  size_t npages = 0;
  bool ok = true;
  if (rep == NULL) return false;
  memset(ro_ptrs, 0, sizeof(ro_ptrs));
  run_one_thread(&ro_worker);
  mi_on_thread_idle();

  for (size_t i = 0; i < RO_N; i++) {
    if (ro_ptrs[i] == NULL) continue;
    mi_page_t* const page = _mi_ptr_page(ro_ptrs[i]);
    bool seen = false;
    for (size_t j = 0; j < npages; j++) { if (pages[j] == page) { seen = true; break; } }
    if (!seen) { pages[npages++] = page; }
  }
  if (npages == 0) { fprintf(stderr, "\n  the worker left no pages behind\n"); ok = false; goto done; }

  before = hole_stats();
  _mi_purge_holes_report_collect(rep);
  after = hole_stats();

  // it must have reached the abandoned pages: they are not in any theap of this thread
  if (rep->bin[_mi_bin(pages[0]->block_size)].pages < npages) {
    fprintf(stderr, "\n  the report only saw %zu pages of this size class but %zu are abandoned with holes\n",
            rep->bin[_mi_bin(pages[0]->block_size)].pages, npages);
    ok = false; goto done;
  }
  // and it must have changed nothing: no discard, no reuse, no page handed back
  if (before.bytes_now != after.bytes_now || before.reuses != after.reuses ||
      before.discards != after.discards || before.pages_freed != after.pages_freed) {
    fprintf(stderr, "\n  the report is not read-only on abandoned pages: bytes %lld->%lld, reuses %lld->%lld, discards %lld->%lld, pages freed %lld->%lld\n",
            (long long)before.bytes_now, (long long)after.bytes_now, (long long)before.reuses, (long long)after.reuses,
            (long long)before.discards, (long long)after.discards, (long long)before.pages_freed, (long long)after.pages_freed);
    ok = false; goto done;
  }
  for (size_t i = 0; i < RO_N; i++) {
    if (ro_ptrs[i] == NULL) continue;
    if (pattern_check(ro_ptrs[i], RO_SZ, i) != RO_SZ) { fprintf(stderr, "\n  CORRUPT abandoned survivor %zu\n", i); ok = false; goto done; }
  }
  fprintf(stderr, "(%zu abandoned pages reached) ", npages);

done:
  for (size_t i = 0; i < RO_N; i++) { if (ro_ptrs[i] != NULL) { mi_free(ro_ptrs[i]); ro_ptrs[i] = NULL; } }
  free(rep);
  return ok;
}

// ---------------------------------------------------------------------------
// the sweep is not a treadmill
//
// The free blocks a sweep leaves on `page->free` are the ones it could NOT discard (their OS
// page still holds a live block). Re-walking them on the next sweep can only find the same
// thing, so a sweep that follows one with no allocation and no free in between must not walk a
// single free list, must discard nothing new, and must un-purge nothing. And once something IS
// freed, the sweep must catch every OS page that became discardable -- the skip may not cost
// memory. (Before the `page->swept_state` check, `visited` grew by the full free-list length of
// every page on EVERY sweep, forever: the cost the profile showed growing with uptime.)
// ---------------------------------------------------------------------------

#define SKIP_N     (4096)
#define SKIP_SZ    (512)

static bool test_sweep_skip(void) {
  const size_t os = _mi_os_page_size();
  const long full_every = mi_option_get(mi_option_purge_holes_full_every);
  mi_option_set(mi_option_purge_holes_full_every, 0);   // no periodic full walk: this tests the skip itself
  void** ptrs = (void**)calloc(SKIP_N, sizeof(void*));
  uint64_t* live = NULL;
  expect_t e;
  bool ok = true;
  hole_stats_t s0, s1, s2, s3, s4, s5;
  mi_page_t* page = NULL;
  size_t bs = 0, cap = 0, nmine = 0, victim = SKIP_N;
  uintptr_t pstart = 0;
  memset(&e, 0, sizeof(e));
  if (ptrs == NULL) { ok = false; goto done; }

  for (size_t i = 0; i < SKIP_N; i++) {
    ptrs[i] = mi_malloc(SKIP_SZ);
    if (ptrs[i] == NULL) { ok = false; goto done; }
    pattern_fill(ptrs[i], SKIP_SZ, i);
  }
  // In every other OS page keep the first block that starts in it, free everything else: half the OS
  // pages become free (discardable), the others are each pinned by exactly one live block, and every
  // mimalloc page keeps a few live blocks -- whatever the block size (padding) and the OS page size are.
  for (size_t i = 0; i < SKIP_N; i++) {
    const uintptr_t os_page = (uintptr_t)ptrs[i] / os;
    bool keep = ((os_page % 2) == 0);
    for (size_t j = 0; keep && j < i; j++) {   // only the first block of that OS page (blocks are not always handed out in address order)
      if (ptrs[j] != NULL && ((uintptr_t)ptrs[j] / os) == os_page) { keep = false; }
    }
    if (!keep) { mi_free(ptrs[i]); ptrs[i] = NULL; }
  }

  // The page we will make a new hole in (any page we hold blocks in will do), the live blocks left in it,
  // and the block to free later: the ONLY live block in an OS page that lies wholly inside the block
  // area, so freeing it -- and nothing else -- makes that OS page discardable.
  for (size_t p0 = 0; p0 < SKIP_N && victim == SKIP_N; p0++) {
    if (ptrs[p0] == NULL) continue;
    if (page != NULL && _mi_ptr_page(ptrs[p0]) == page) continue;   // (pages come in runs; only re-examine on a new one)
    page = _mi_ptr_page(ptrs[p0]);
    bs = page->block_size;
    cap = page->capacity;
    pstart = (uintptr_t)mi_page_start(page);
    free(live);
    live = (uint64_t*)calloc((cap + 63) / 64, sizeof(uint64_t));
    if (live == NULL) { ok = false; goto done; }
    nmine = 0;
    for (size_t i = 0; i < SKIP_N; i++) {
      if (ptrs[i] == NULL || _mi_ptr_page(ptrs[i]) != page) continue;
      const size_t idx = (size_t)((uintptr_t)ptrs[i] - pstart) / bs;
      live[idx / 64] |= ((uint64_t)1 << (idx % 64));
      nmine++;
    }
    if (page->used != nmine) {   // the oracle below assumes every other block in the page is free
      fprintf(stderr, "\n  page %p is not exclusively ours: used=%zu but we hold %zu\n", (void*)page, (size_t)page->used, nmine);
      ok = false; goto done;
    }
    if (nmine < 2) continue;     // freeing the victim must not free the page
    for (size_t k = 0; k < (mi_page_size(page) / os) + 1 && victim == SKIP_N; k++) {
      size_t first, last;
      if (!_mi_page_purge_os_page_blocks(os, bs, pstart, cap, k, &first, &last)) continue;
      size_t nlive = 0, only = 0;
      for (size_t idx = first; idx <= last; idx++) {
        if (bit_get(live, idx)) { nlive++; only = idx; }
      }
      if (nlive != 1) continue;
      for (size_t i = 0; i < SKIP_N; i++) {
        if (ptrs[i] == NULL || _mi_ptr_page(ptrs[i]) != page) continue;
        if ((size_t)((uintptr_t)ptrs[i] - pstart) / bs == only) { victim = i; break; }
      }
    }
  }
  if (victim == SKIP_N) {
    fprintf(stderr, "\n  no OS page in any of our pages is pinned by exactly one of our blocks\n");
    ok = false; goto done;
  }

  // From here to the last sweep: NOT ONE allocation or free, except the single `mi_free` below.
  // Everything is measured through the process-wide counters, so an allocation anywhere would
  // legitimately un-skip its page.
  s0 = hole_stats();
  mi_on_thread_idle();     // 1st sweep: walks every free list, discards the free OS pages
  s1 = hole_stats();
  mi_on_thread_idle();     // 2nd: nothing changed, so it must do nothing at all
  s2 = hole_stats();
  mi_on_thread_idle();     // 3rd: still nothing
  s3 = hole_stats();

  {
    const size_t idx = (size_t)((uintptr_t)ptrs[victim] - pstart) / bs;
    live[idx / 64] &= ~((uint64_t)1 << (idx % 64));
  }
  mi_free(ptrs[victim]);   // its OS page is now entirely free
  ptrs[victim] = NULL;

  mi_on_thread_idle();     // 4th: `used` dropped, so this page is walked again
  s4 = hole_stats();
  mi_on_thread_idle();     // 5th: and it is quiet again
  s5 = hole_stats();

  if (purging_enabled && s1.visited <= s0.visited) {
    fprintf(stderr, "\n  the first sweep walked no free list at all (visited %lld)\n", (long long)(s1.visited - s0.visited));
    ok = false; goto done;
  }
  if (purging_enabled && s1.discards <= s0.discards) {
    fprintf(stderr, "\n  the first sweep discarded nothing, so there is no treadmill to test\n");
    ok = false; goto done;
  }

  // THE FIX: an unchanged page is skipped without its free list being walked.
  #define SKIP_QUIET(a, b, which)                                                                     \
    if ((b).visited != (a).visited) {                                                                 \
      fprintf(stderr, "\n  the %s sweep re-walked %lld free-list blocks (nothing changed in between)\n", \
              which, (long long)((b).visited - (a).visited));                                         \
      ok = false;                                                                                     \
    }                                                                                                 \
    if ((b).discards != (a).discards || (b).reuses != (a).reuses || (b).bytes_now != (a).bytes_now) { \
      fprintf(stderr, "\n  the %s sweep churned the OS: discards %lld, reuses %lld, bytes now %lld -> %lld\n", \
              which, (long long)((b).discards - (a).discards), (long long)((b).reuses - (a).reuses),  \
              (long long)(a).bytes_now, (long long)(b).bytes_now);                                    \
      ok = false;                                                                                     \
    }
  SKIP_QUIET(s1, s2, "second")
  SKIP_QUIET(s2, s3, "third")
  SKIP_QUIET(s4, s5, "fifth")
  #undef SKIP_QUIET
  if (!ok) goto done;
  if (purging_enabled && s2.pages_skipped <= s1.pages_skipped) {
    fprintf(stderr, "\n  the second sweep skipped no page, so it did nothing for another reason\n");
    ok = false; goto done;
  }

  // ...and the skip costs no memory: the free that made an OS page discardable is not missed.
  if (purging_enabled) {
    if (s4.visited <= s3.visited) {
      fprintf(stderr, "\n  the sweep after the free did not walk the page it was freed in\n");
      ok = false; goto done;
    }
    if (s4.discards <= s3.discards || s4.bytes_now < s3.bytes_now + (int64_t)os) {
      fprintf(stderr, "\n  the freed OS page was not discarded: discards %lld, bytes now %lld -> %lld (os page %zu)\n",
              (long long)(s4.discards - s3.discards), (long long)s3.bytes_now, (long long)s4.bytes_now, os);
      ok = false; goto done;
    }
  }

  // The strong version of "no memory lost": the page must hold EXACTLY the holes an
  // implementation without any skip would have -- every OS page inside the block area with no
  // live block in it is discarded. (`expect_page` is the same oracle the report test uses.)
  if (!expect_page(page, live, &e)) { ok = false; goto done; }

  // and the survivors are intact
  for (size_t i = 0; i < SKIP_N; i++) {
    if (ptrs[i] == NULL) continue;
    if (pattern_check(ptrs[i], SKIP_SZ, i) != SKIP_SZ) {
      fprintf(stderr, "\n  CORRUPT survivor %zu\n", i);
      ok = false; goto done;
    }
  }
  fprintf(stderr, "(sweep 1: %lld blocks walked; sweeps 2+3: %lld; %lld pages skipped) ",
          (long long)(s1.visited - s0.visited), (long long)(s3.visited - s1.visited),
          (long long)(s3.pages_skipped - s0.pages_skipped));

done:
  mi_option_set(mi_option_purge_holes_full_every, full_every);
  if (ptrs != NULL) {
    for (size_t i = 0; i < SKIP_N; i++) { if (ptrs[i] != NULL) mi_free(ptrs[i]); }
    free(ptrs);
  }
  free(live);
  return ok;
}

// A page whose free blocks are ALL discarded has an empty free list, and a collect used to hand
// a run of its holes straight back (`_mi_page_free_collect` un-purges when `page->free == NULL`).
// `mi_on_thread_idle` collects before it sweeps, so that page was un-purged and re-discarded on
// every park -- two syscalls per page, forever, for no memory. Only the allocation path may
// un-purge. One block per OS page makes every free block discardable, so the free list empties.
static bool test_sweep_no_unpurge_on_collect(void) {
  const size_t os = _mi_os_page_size();
  const size_t n = 256;
  const long full_every = mi_option_get(mi_option_purge_holes_full_every);
  mi_option_set(mi_option_purge_holes_full_every, 0);   // a full sweep would legitimately walk again
  void** ptrs = (void**)calloc(n, sizeof(void*));
  hole_stats_t s1, s2;
  bool ok = true;
  if (ptrs == NULL) { mi_option_set(mi_option_purge_holes_full_every, full_every); return false; }

  for (size_t i = 0; i < n; i++) {
    ptrs[i] = mi_malloc(os);
    if (ptrs[i] == NULL) { ok = false; goto done; }
  }
  for (size_t i = 0; i < n; i++) {
    if ((i % 64) != 0) { mi_free(ptrs[i]); ptrs[i] = NULL; }   // 63 of every 64 blocks: whole OS pages
  }

  mi_on_thread_idle();
  s1 = hole_stats();
  for (int r = 0; r < 4; r++) { mi_on_thread_idle(); }   // no allocation, no free: must be free of charge
  s2 = hole_stats();

  if (s2.visited != s1.visited || s2.discards != s1.discards || s2.reuses != s1.reuses) {
    fprintf(stderr, "\n  4 idle sweeps with nothing to do: %lld blocks walked, %lld discards, %lld reuses\n",
            (long long)(s2.visited - s1.visited), (long long)(s2.discards - s1.discards),
            (long long)(s2.reuses - s1.reuses));
    ok = false; goto done;
  }
  if (s2.bytes_now != s1.bytes_now) {   // and they gave nothing back to the process
    fprintf(stderr, "\n  the discarded bytes moved from %lld to %lld with no allocation in between\n",
            (long long)s1.bytes_now, (long long)s2.bytes_now);
    ok = false; goto done;
  }
  if (purging_enabled && s1.bytes_now <= 0) {
    fprintf(stderr, "\n  nothing is discarded, so this proves nothing\n");
    ok = false; goto done;
  }
  // the memory must still be usable: the allocation path hands a hole back
  for (size_t i = 0; i < n; i++) {
    if (ptrs[i] != NULL) continue;
    ptrs[i] = mi_malloc(os);
    if (ptrs[i] == NULL) { fprintf(stderr, "\n  allocation failed after the holes were kept\n"); ok = false; goto done; }
    memset(ptrs[i], 0x5A, os);
  }

done:
  mi_option_set(mi_option_purge_holes_full_every, full_every);
  for (size_t i = 0; i < n; i++) { if (ptrs[i] != NULL) mi_free(ptrs[i]); }
  free(ptrs);
  return ok;
}

// ---------------------------------------------------------------------------
// the skip cannot lose memory forever
//
// `(capacity,used)` cannot see a page that CHURNED: as many frees as allocs between two sweeps
// leaves `used` where it was, but the set of free blocks -- and so the set of discardable OS
// pages -- can be different. A steady-state server can sit at the same `used` at every park, so
// the miss would never heal on its own. `purge_holes_full_every` bounds it: every N'th sweep
// walks every page whatever its state says.
//
// The churn is built by hand (free a live block, then stamp back the `(capacity,used)` a churned
// page would have) because we cannot make the allocator hand us a replacement block out of one
// chosen page. The state it stamps is one a real balanced churn produces.
// ---------------------------------------------------------------------------

#define BOUND_N       (2048)
#define BOUND_SZ      (512)
#define BOUND_EVERY   (8)      // full sweep every 8th

static bool test_sweep_full_every(void) {
  const long full_every = mi_option_get(mi_option_purge_holes_full_every);
  void** ptrs = (void**)calloc(BOUND_N, sizeof(void*));
  mi_page_t* page = NULL;
  hole_stats_t s0, s1;
  bool ok = true;
  int swept = 0;
  if (ptrs == NULL) { ok = false; goto done; }
  mi_option_set(mi_option_purge_holes_full_every, 0);

  for (size_t i = 0; i < BOUND_N; i++) {
    ptrs[i] = mi_malloc(BOUND_SZ);
    if (ptrs[i] == NULL) { ok = false; goto done; }
  }
  for (size_t i = 0; i < BOUND_N; i++) {
    if ((i % 64) != 0) { mi_free(ptrs[i]); ptrs[i] = NULL; }   // 1 live block per pinned OS page
  }
  mi_on_thread_idle();   // sweep: discards what it can, leaves the pinned OS pages alone
  page = _mi_ptr_page(ptrs[0]);

  // free a live block -- some OS page in this page may now be entirely free -- and then stamp the
  // page's swept state back to what it is NOW, which is exactly what a balanced churn (this free
  // plus one allocation elsewhere in the page) would have left behind. The `(capacity,used)` check
  // is blind to it by construction.
  for (size_t i = 0; i < BOUND_N; i++) {
    if (ptrs[i] != NULL && _mi_ptr_page(ptrs[i]) == page) { mi_free(ptrs[i]); ptrs[i] = NULL; break; }
  }
  s0 = hole_stats();
  page->swept_state = mi_page_sweep_state(page);   // (Bun open-codes this: theirs is a `page.c` static)

  // with no periodic full sweep the page is now wedged: no amount of parking finds the hole
  for (int r = 0; r < BOUND_EVERY * 2; r++) { mi_on_thread_idle(); }
  s1 = hole_stats();
  if (purging_enabled && (s1.visited != s0.visited || s1.full_sweeps != s0.full_sweeps)) {
    fprintf(stderr, "\n  the churned page was not actually hidden from the sweep (visited %lld, full %lld)\n",
            (long long)(s1.visited - s0.visited), (long long)(s1.full_sweeps - s0.full_sweeps));
    ok = false; goto done;
  }

  // ...and the periodic full sweep is what unwedges it, within N parks
  mi_option_set(mi_option_purge_holes_full_every, BOUND_EVERY);
  s0 = hole_stats();
  for (swept = 1; swept <= BOUND_EVERY; swept++) {
    mi_on_thread_idle();
    s1 = hole_stats();
    if (s1.full_sweeps > s0.full_sweeps) break;
  }
  if (purging_enabled) {
    if (s1.full_sweeps != s0.full_sweeps + 1 || swept > BOUND_EVERY) {
      fprintf(stderr, "\n  no full sweep in %d parks with purge_holes_full_every=%d\n", swept, BOUND_EVERY);
      ok = false; goto done;
    }
    if (s1.visited <= s0.visited) {
      fprintf(stderr, "\n  the full sweep walked no free list, so it cannot have found the hidden hole\n");
      ok = false; goto done;
    }
    fprintf(stderr, "(hidden hole found by the full sweep after %d parks) ", swept);
  }

done:
  mi_option_set(mi_option_purge_holes_full_every, full_every);
  if (ptrs != NULL) {
    for (size_t i = 0; i < BOUND_N; i++) { if (ptrs[i] != NULL) mi_free(ptrs[i]); }
    free(ptrs);
  }
  return ok;
}

// ---------------------------------------------------------------------------
// portable "run one thread and join" (mirrors test-stress.c)
// ---------------------------------------------------------------------------

#ifdef _WIN32
#include <windows.h>
static void (*thread_fun)(void);
static DWORD WINAPI thread_entry(LPVOID param) {
  (void)param;
  thread_fun();
  return 0;
}
static void run_one_thread(void (*fun)(void)) {
  thread_fun = fun;
  DWORD tid = 0;
  HANDLE h = CreateThread(0, 8*1024L, &thread_entry, NULL, 0, &tid);
  if (h == NULL) return;
  WaitForSingleObject(h, INFINITE);
  CloseHandle(h);
}
#else
#include <pthread.h>
static void* thread_entry(void* param) {
  ((void (*)(void))param)();
  return NULL;
}
static void run_one_thread(void (*fun)(void)) {
  pthread_t t;
  if (pthread_create(&t, NULL, &thread_entry, (void*)(uintptr_t)fun) != 0) return;
  pthread_join(t, NULL);
}
#endif

// ---------------------------------------------------------------------------
// 12. `purge_holes_min_interval` paces the OWNER's own sweeps, not only the
//     scavenger's claim of a parked thread's tld.
//
//     `mi_on_thread_idle()` is the "do the idle work here, on this thread" entry
//     point, and a sweep is a full walk of every page of every theap of the
//     calling thread. An event loop that calls it on every turn must not pay that
//     on every turn -- which is exactly what the option promises ("do not sweep
//     one thread's heaps more often than every N milli-seconds", with no clause
//     about which thread does the sweeping). The pacing lives in
//     `_mi_purge_holes_of`, the one path the owner and the scavenger share.
//
//     Observable: `blocks_visited` + `pages_skipped`, both monotonic and both moved
//     only from inside a sweep -- so they distinguish "the sweep ran and found
//     nothing" from "the sweep did not run", which `discard_calls` cannot.
// ---------------------------------------------------------------------------
#define PACE_INTERVAL_MS  (1000)

static void pace_sleep_ms(long ms) {
  #if defined(_WIN32)
  Sleep((DWORD)ms);
  #else
  struct timespec ts;
  ts.tv_sec = (time_t)(ms / 1000);
  ts.tv_nsec = (long)(ms % 1000) * 1000000L;
  nanosleep(&ts, NULL);
  #endif
}

static int64_t pace_sweep_work(void) {   // monotonic; moves iff a sweep actually walked pages
  const hole_stats_t h = hole_stats();
  return h.visited + h.pages_skipped;
}

static bool pace_churn(void** ptrs, size_t n, size_t sz) {
  for (size_t i = 0; i < n; i++) {
    if (ptrs[i] == NULL) { ptrs[i] = mi_malloc(sz); if (ptrs[i] == NULL) return false; }
    pattern_fill(ptrs[i], sz, i);
  }
  for (size_t i = 0; i < n; i++) {   // keep every 4th: scattered survivors, whole free OS pages
    if ((i % 4) != 0) { mi_free(ptrs[i]); ptrs[i] = NULL; }
  }
  return true;
}

static bool test_owner_sweep_pacing(void) {
  enum { N = 1024, SZ = 512 };
  void** ptrs = (void**)calloc(N, sizeof(void*));
  if (ptrs == NULL) return false;
  bool ok_all = true;
  const long saved = mi_option_get(mi_option_purge_holes_min_interval);

  // (a) baseline: an unpaced sweep, which stamps this thread's `holes_sweep_last`.
  mi_option_set(mi_option_purge_holes_min_interval, 0);
  if (!pace_churn(ptrs, N, SZ)) { ok_all = false; goto done; }
  mi_on_thread_idle();
  const mi_msecs_t stamped_at = _mi_clock_now();

  // (b) inside the window: a second `mi_on_thread_idle()` must not sweep at all.
  mi_option_set(mi_option_purge_holes_min_interval, PACE_INTERVAL_MS);
  if (!pace_churn(ptrs, N, SZ)) { ok_all = false; goto done; }
  const int64_t before_in = pace_sweep_work();
  mi_on_thread_idle();
  const int64_t after_in = pace_sweep_work();
  const mi_msecs_t elapsed = _mi_clock_now() - stamped_at;
  if (elapsed >= PACE_INTERVAL_MS) {
    // the churn itself outran the window (a very loaded box): the negative half is not
    // decidable, say so rather than fail on the machine's timing
    fprintf(stderr, "\n  SKIP in-window half: churn took %ldms, window is %dms\n",
            (long)elapsed, PACE_INTERVAL_MS);
  }
  else if (after_in != before_in) {
    fprintf(stderr, "\n  swept %ldms into a %dms window: sweep work %lld -> %lld\n",
            (long)elapsed, PACE_INTERVAL_MS, (long long)before_in, (long long)after_in);
    ok_all = false;
  }

  // (c) past the window: the very next `mi_on_thread_idle()` sweeps again. The deadline has
  // to EXPIRE, not merely be disabled -- so wait it out rather than setting the option to 0.
  pace_sleep_ms(PACE_INTERVAL_MS + (PACE_INTERVAL_MS / 2));
  if (!pace_churn(ptrs, N, SZ)) { ok_all = false; goto done; }
  const int64_t before_out = pace_sweep_work();
  mi_on_thread_idle();
  const int64_t after_out = pace_sweep_work();
  if (purging_enabled && after_out <= before_out) {
    fprintf(stderr, "\n  did not sweep %dms past a %dms window: sweep work %lld -> %lld\n",
            PACE_INTERVAL_MS + (PACE_INTERVAL_MS / 2), PACE_INTERVAL_MS,
            (long long)before_out, (long long)after_out);
    ok_all = false;
  }
  // with `purge_holes=0` there is no hole phase to count, so (c) has no observable -- the
  // stamp still runs (see `_mi_purge_holes_of`), which is what keeps the scavenger's own
  // pacing identical in the two builds.

done:
  for (size_t i = 0; i < N; i++) { if (ptrs[i] != NULL) mi_free(ptrs[i]); }
  free(ptrs);
  mi_option_set(mi_option_purge_holes_min_interval, saved);
  return ok_all;
}

// ADAPTATION for this fork (Bun has no MI_GUARDED lane): `ctest-guarded`'s second pass runs
// with `MIMALLOC_GUARDED_SAMPLE_RATE=1`, which turns EVERY allocation into an oversized,
// guard-page-backed one that hands back an INTERIOR pointer. A test can then neither compute a
// block's index in its page from the pointer it got, nor keep a page to itself (the test's own
// `calloc`s and mimalloc's internals land in the same bins). Six cases below need both; the
// other nineteen do not and still run, as does the whole file in the lane's FIRST pass at the
// default sample rate. This is about what a test can observe, not about what the engine does:
// guarded blocks are ordinary free-listed blocks to the sweep.
static bool layout_is_predictable(void) {
  #if defined(MI_GUARDED)
  return (mi_option_get(mi_option_guarded_sample_rate) != 1);
  #else
  return true;
  #endif
}

int main(void) {
  mi_version();
  purging_enabled = mi_option_is_enabled(mi_option_purge_holes);
  // Zero every hole before it is discarded. Without this the survivor checks below are
  // VACUOUS in a release build on macOS: MADV_FREE_REUSABLE is lazy, so a discard that
  // wrongly covers a live block leaves its data intact until the kernel reclaims the page.
  mi_option_set(mi_option_purge_holes_eager_zero, 1);
  // Every case below drives the sweep directly, back to back, and asserts what one specific
  // `mi_on_thread_idle()` discarded. `purge_holes_min_interval` (100ms by default) now paces the
  // OWNER's own sweeps too, not just the scavenger's claim -- so with it left at the default the
  // second and later cases would silently be handed a skipped sweep and the whole file would
  // become a slow way of testing nothing. The pacing itself has its own case
  // (`test_owner_sweeps_are_paced`), which sets the option back for its own duration.
  mi_option_set(mi_option_purge_holes_min_interval, 0);
  fprintf(stderr, "purge_holes is %s, os page size is %zu\n",
          (purging_enabled ? "ON" : "OFF"), (size_t)_mi_os_page_size());

  CHECK("os-page-arithmetic", test_os_page_arithmetic());

  // The bitmap is indexed by OS page, so eligibility no longer depends on the block count:
  // every size class of a small (64KB) or medium (512KB) page is eligible, down to the
  // smallest one. A run of free blocks still has to cover a whole OS page.
  bool purged_any = false;
  bool ps[8];
  for (size_t i = 0; i < 8; i++) { ps[i] = false; }
  CHECK("survivors-16",    test_survivors(16, &ps[0]));
  CHECK("survivors-64",    test_survivors(64, &ps[1]));
  CHECK("survivors-256",   test_survivors(256, &ps[2]));
  CHECK("survivors-1024",  test_survivors(1024, &ps[3]));
  CHECK("survivors-4096",  test_survivors(4096, &ps[4]));
  CHECK("survivors-8192",  test_survivors(8192, &ps[5]));
  CHECK("survivors-16384", test_survivors(16384, &ps[6]));
  CHECK("survivors-65536", test_survivors(65536, &ps[7]));   // medium page (512KB)
  purged_any = false;
  for (size_t i = 0; i < 8; i++) { purged_any = purged_any || ps[i]; }

  if (purging_enabled) {
    CHECK("purging-actually-happened", purged_any);
    // the small size classes are what this rework unlocked: they must purge now
    CHECK("small-blocks-are-eligible", (ps[0] && ps[1] && ps[2] && ps[3]));
    CHECK("medium-page-is-eligible", ps[7]);
  }
  else {
    CHECK("nothing-purged-when-off", !purged_any);
  }

  CHECK("churn-no-aliasing", test_churn());
  CHECK("aligned-16k", test_aligned());
  CHECK("page-lifecycle", test_page_lifecycle());
  CHECK("abandoned-pages", test_abandoned());
  CHECK("large-pages", test_large_pages());
  if (layout_is_predictable()) {   // see `layout_is_predictable` above
    CHECK("report-pinned-ospages", test_holes_report(0));
    CHECK("report-discardable-ospages", test_holes_report(1));
    CHECK("report-is-read-only-on-abandoned", test_report_read_only_abandoned());
    CHECK("unformed-tail", test_unformed_tail());
    CHECK("unformed-tail-freed", test_unformed_tail_freed());
    CHECK("sweep-skips-unchanged-pages", test_sweep_skip());
  }
  else {
    fprintf(stderr, "skipped (every allocation is guarded, so block layout is unobservable): "
                    "report-pinned-ospages, report-discardable-ospages, report-is-read-only-on-abandoned, "
                    "unformed-tail, unformed-tail-freed, sweep-skips-unchanged-pages\n");
  }
  CHECK("report-is-read-only", test_report_is_read_only());
  CHECK("option-off-is-noop", test_option_off());
  CHECK("sweep-does-not-unpurge-on-collect", test_sweep_no_unpurge_on_collect());
  CHECK("sweep-full-every-bounds-a-missed-hole", test_sweep_full_every());
  CHECK("owner-sweeps-are-paced", test_owner_sweep_pacing());

  // everything above is freed by now, so every hole must have been handed back
  mi_collect(true);
  const hole_stats_t end = hole_stats();
  fprintf(stderr, "holes: %lld bytes discarded in total over %lld discards / %lld reuses; %lld pages freed by the sweep; %lld bytes still discarded\n",
          (long long)end.bytes_total, (long long)end.discards, (long long)end.reuses,
          (long long)end.pages_freed, (long long)end.bytes_now);
  fprintf(stderr, "holes: the last sweep could not touch %lld pages (%lld bytes, of which %lld bytes free)\n",
          (long long)end.inelig_pages, (long long)end.inelig_bytes, (long long)end.inelig_free);
  fprintf(stderr, "holes: unformed tail: %lld bytes discarded in total over %lld discards / %lld reuses; %lld bytes still discarded\n",
          (long long)end.unformed_total, (long long)end.unformed_discards, (long long)end.unformed_reuses,
          (long long)end.unformed_now);
  if (malloc_is_mimalloc()) {
    // the C runtime (stdio buffers, the `calloc`s in this file) has live pages of its own then
    fprintf(stderr, "(malloc is overridden: not checking that no hole is outstanding at exit)\n");
  }
  else {
    CHECK("no-holes-outstanding-at-exit", (end.bytes_now == 0 && end.blocks_now == 0));
    CHECK("no-unformed-tail-outstanding-at-exit", (end.unformed_now == 0));
  }

  mi_stats_print(NULL);
  return print_test_summary();
}
