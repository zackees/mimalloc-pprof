/* ----------------------------------------------------------------------------
Copyright (c) 2018-2025, Microsoft Research, Daan Leijen
This is free software; you can redistribute it and/or modify it under the
terms of the MIT license. A copy of the license can be found in the file
"LICENSE" at the root of this distribution.
-----------------------------------------------------------------------------*/

/* -----------------------------------------------------------
  Page hole purging  (issue #272 / Bun parity P7b)
  imported from oven-sh/mimalloc @ 942b8342, MIT.

  Upstream mimalloc returns a page's memory to the OS only when EVERY block in
  it is free. One long-lived object therefore keeps a whole 64KB/512KB page
  resident, and a server that churns allocations ends up paying for pages that
  are 95% free (oven-sh/bun#39844: heap peaks 1.4-2.6x Node's). Hole punching
  fixes exactly that: at an idle point (`mi_on_thread_idle`) it discards the
  memory of the free blocks inside a still-used page.

  The unit is an OS page, because that is the unit `madvise`/`MEM_RESET` works
  in. `page->purged` is a bitmap over the OS pages of the block area
  (`mi_page_purge_base` is its origin, so bit `k` always names an OS-page
  aligned range in absolute terms). An OS page may be discarded only when
  *every* block overlapping it is free, which is what
  `mi_page_block_index_is_purged` computes. A purged block lost bytes, so it is
  taken OFF every free list: mimalloc threads its free list through the free
  blocks themselves and a discarded block cannot carry a `next` pointer.
  `_mi_page_unpurge_run` hands a whole run of discarded OS pages back at once:
  it calls `_mi_os_reuse` on exactly that byte range before any byte of it is
  written again (on macOS a discarded page stays MADV_FREE_REUSABLE --
  reclaimable by the kernel -- until it is REUSE'd), and pushes every block that
  is whole again back onto the free list.

  An OS page that is not entirely inside the block area is never discarded: it
  holds bytes we do not own (the page header, which lives *before* `page_start`,
  or blocks beyond `capacity` that are not formed yet). See
  `_mi_page_purge_os_page_blocks`, which is the only place this arithmetic lives.

  The discard goes through `_mi_os_discard`, which NEVER changes commit state
  (MADV_FREE_REUSABLE on macOS, MADV_DONTNEED on Linux -- both keep the mapping
  and demand-fault zeroes -- MEM_RESET on Windows). The arena tracks commit per
  64KB slice and cannot represent a sub-slice hole, so commit state MUST stay
  untouched: otherwise a page returned to the arena would be re-handed-out as
  "committed" and the first write into a hole would fault (on Windows only --
  silently fine on macOS/Linux). Note that `_mi_os_purge` is NOT usable here:
  with the default `MIMALLOC_PURGE_DECOMMITS=1` it decommits (and in debug
  builds it also mprotects the range PROT_NONE).

  DEVIATION from Bun (CLAUDE.md rule 6): Bun keeps all of this inside
  `src/page.c` (+1038 lines) and the sweep drivers inside `src/theap.c`. Here
  the whole engine lives in this file; `src/page.c` carries only the five hook
  calls, and `src/theap.c` only exports its page walker. The shared inline
  helpers (`mi_page_can_purge_holes`, `mi_page_block_index_is_purged`, ...) are
  in `mimalloc/internal.h` next to the other page inlines, because both this
  file and those hooks need them.

  PROFILER INTERACTION (this fork, #272). Our profiler attaches sampled
  allocation records to a page (`page->metadata`), which Bun's does not. Three
  things make that safe here:
   1. a record is attached to a LIVE block and the sweep only discards OS pages
      in which every block is free, so no record's block can be inside a
      discarded range; the record structs themselves come from the profiler's
      raw-OS arena (CLAUDE.md rule 4) and are never inside a page at all. Both
      are asserted, per discard, by `_mi_prof_debug_assert_no_records_in`.
   2. heap inspection (`mi_prof_visit`, `mi_heap_visit_blocks`, the DHAT and
      memory-events walkers) goes through `_mi_theap_area_visit_blocks`, which
      counts a purged block as free -- so a discarded block is never handed to a
      visitor -- and through `_mi_page_free_collect_no_unpurge`, so inspecting a
      heap never faults a hole back in.
   3. the sweep of a parked thread never touches `mi_tld_t::profiler`: nothing
      in this file reads or writes it (asserted in `_mi_theap_sweep_parked`).

  TEARDOWN AND HEAP DELETION (7a's exit-path hardening, PR #299). Neither needs a
  check in this file, for reasons worth writing down:
   - after `_mi_scavenger_stop` sets `_mi_scavenger_shutdown`, no sweep can start on
     the scavenger: its run loop exits on `_mi_scavenger_running == 0` and the stop
     joins it, and `_mi_scavenger_start_lazy` refuses to restart, so
     `mi_on_thread_idle_start` returns false and hands nothing off. A direct
     `mi_on_thread_idle()` after that still sweeps -- on the CALLING thread, over its
     own theaps, which is safe at any point in the process's life.
   - `mi_heap_delete`/`_destroy` calls `_mi_park_leave` on every parked owner of the
     heap BEFORE `_mi_heap_detach_theaps`, and `_mi_park_leave` does not return until
     MI_PARK_SWEEPING has cleared. So a sweep is never in progress while a theap is
     being detached. `_mi_park_leave` terminates against this sweep because every
     phase of it re-reads `tld->park_reclaim`: between heaps in `_mi_purge_holes_of`,
     between pages in `mi_theap_page_purge_holes`, and between abandoned pages in
     `mi_arena_page_purge_holes_at` -- so the wait is bounded by one page's walk, and
     `tld->theaps_lock` (which the sweep holds across its passes, and which
     `_mi_heap_detach_theaps` try-acquires) is always released before the deleter
     needs it.
----------------------------------------------------------- */

#include "mimalloc.h"
#include "mimalloc/internal.h"
#include "mimalloc/prim-tls.h"   // _mi_theap_default

#include <stddef.h>              // offsetof


/* -----------------------------------------------------------
  Page-geometry helpers shared with the hooks in `page.c`
----------------------------------------------------------- */

// The blocks that overlap OS page `k` of a page whose block area starts at `page_start` and
// holds `capacity` blocks of `block_size` bytes. OS pages are counted from
// `align_down(page_start, os_page_size)`, so OS page `k` is an OS-page aligned range in
// absolute terms -- exactly what `_mi_os_discard` and `_mi_os_reuse` need.
// Returns `false` when OS page `k` is not *entirely* inside the block area: such an OS page
// holds bytes of the page header or of blocks that are not formed yet, and may never be
// discarded. Exposed for `test-purge-holes.c`.
bool _mi_page_purge_os_page_blocks(size_t os_page_size, size_t block_size, uintptr_t page_start,
                                   size_t capacity, size_t k, size_t* first, size_t* last)
{
  *first = 0;
  *last = 0;
  const uintptr_t base = _mi_align_down(page_start, os_page_size);
  const uintptr_t lo = base + (k * os_page_size);
  const uintptr_t hi = lo + os_page_size;
  const uintptr_t pend = page_start + (capacity * block_size);
  if (lo < page_start || hi > pend) return false;   // not entirely inside the block area
  *first = (size_t)(lo - page_start) / block_size;
  *last = (size_t)((hi - 1) - page_start) / block_size;
  mi_assert_internal(*first <= *last && *last < capacity);
  return true;
}

#if !MI_PADDING && !MI_ENCODE_FREELIST
// imported from oven-sh/mimalloc @ 942b8342, MIT. The fields up to and including `theap` are read
// by `mi_free` and `mi_page_alloc`; they must all sit in the first cache line. This is the static
// proof that `purged`/`swept_state` (and this fork's `metadata`/`has_metadata`) stay COLD, i.e.
// that hole purging costs the alloc/free fast path nothing in layout terms.
typedef char mi_page_hot_fields_first_cacheline[(offsetof(mi_page_t,theap) + sizeof(mi_theap_t*) <= 64) ? 1 : -1];
#endif

void _mi_page_purged_reset(mi_page_t* page) {
  for (size_t i = 0; i < MI_PAGE_PURGE_WORDS; i++) { page->purged[i] = 0; }
  page->unformed_purged_lo = 0;
  page->unformed_purged_hi = 0;
  page->swept_state = MI_PAGE_SWEPT_NONE;   // a fresh (or recycled) page was never swept
}

// `mi_page_sweep_state` is lossless while `capacity` fits 16 bits and `used` 32, and then
// `MI_PAGE_SWEPT_NONE` (all ones) is an impossible state:
typedef char mi_page_sweep_state_fits[(sizeof(((mi_page_t*)0)->capacity) <= 2 &&
                                       sizeof(((mi_page_t*)0)->used) <= 4) ? 1 : -1];

// The number of blocks that are purged: the blocks overlapping a discarded OS page.
// (the blocks of a maximal run of discarded OS pages are contiguous)
size_t _mi_page_purged_count(const mi_page_t* page) {
  if (!mi_page_has_purged(page)) return 0;
  size_t total = 0;
  size_t k = 0;
  while (k < MI_PAGE_PURGE_BITS) {
    if (!mi_page_os_page_purged(page, k)) { k++; continue; }
    const size_t k0 = k;
    while (k < MI_PAGE_PURGE_BITS && mi_page_os_page_purged(page, k)) { k++; }
    size_t first, last, first1, last1;
    if (!mi_page_os_page_blocks(page, k0, &first, &last)) { mi_assert_internal(false); continue; }
    if (!mi_page_os_page_blocks(page, k - 1, &first1, &last1)) { mi_assert_internal(false); continue; }
    total += (last1 - first) + 1;
  }
  return total;
}

// The hole invariants, called from `_mi_page_is_valid` (`src/page.c`). Kept here rather than
// inlined there so `page.c` keeps its five hook calls (CLAUDE.md rule 6). These are what make
// every existing test in the suite a test of the purge machinery.
void _mi_page_holes_assert_valid(const mi_page_t* page) {
  #if MI_DEBUG > 1
  if (!mi_page_has_purged(page)) return;
  mi_assert_internal(mi_page_can_purge_holes(page));   // only eligible pages may carry holes
  // A purged block is OFF every free list: if it were on one, its `next` pointer would live in
  // discarded memory. (checked from the lists, which is O(list) instead of the O(capacity * list)
  // of the other direction -- pages can hold thousands of blocks)
  mi_page_t* const p = (mi_page_t*)page;
  for (mi_block_t* b = p->free; b != NULL; b = mi_block_next(p, b)) {
    mi_assert_internal(!mi_page_block_is_purged(p, b));
  }
  for (mi_block_t* b = p->local_free; b != NULL; b = mi_block_next(p, b)) {
    mi_assert_internal(!mi_page_block_is_purged(p, b));
  }
  #if !MI_TRACK_ENABLED && !MI_TSAN
  for (mi_block_t* b = mi_page_thread_free(p); b != NULL; b = mi_block_next(p, b)) {
    mi_assert_internal(!mi_page_block_is_purged(p, b));
  }
  #endif
  #else
  MI_UNUSED(page);
  #endif
}


/* -----------------------------------------------------------
  Process-wide counters

  This is the only way to see how much hole punching actually reclaims
  (`mi_stats_t` cannot grow, see `mi_purge_holes_stats_t` in `mimalloc.h`).
  Plain `int64_t` updated through the atomic i64 helpers, exactly as
  `mi_stat_counter_t` is.
----------------------------------------------------------- */

static int64_t mi_holes_bytes;          // currently discarded
static int64_t mi_holes_blocks;         // currently held off the free lists
static int64_t mi_holes_bytes_total;
static int64_t mi_holes_discard_calls;
static int64_t mi_holes_reuse_calls;
static int64_t mi_holes_pages_freed;
static int64_t mi_holes_inelig_pages;   // pages the sweep cannot purge at all (see `mi_page_can_purge_holes`)
static int64_t mi_holes_inelig_bytes;
static int64_t mi_holes_inelig_free_bytes;
static int64_t mi_holes_unformed_bytes;         // unformed tail discarded right now
static int64_t mi_holes_unformed_bytes_total;
static int64_t mi_holes_unformed_discard_calls;
static int64_t mi_holes_unformed_reuse_calls;
static int64_t mi_holes_pages_skipped;          // pages the sweep skipped: unchanged since it last swept them
static int64_t mi_holes_blocks_visited;         // free-list blocks the sweep walked (the cost the skip avoids)
static int64_t mi_holes_full_sweeps;            // sweeps that walked every page regardless (`purge_holes_full_every`)

static inline size_t mi_holes_load(int64_t* c) {
  const int64_t v = mi_atomic_addi64_relaxed(c, 0);
  return (v < 0 ? 0 : (size_t)v);
}

void mi_purge_holes_stats_get(mi_purge_holes_stats_t* stats) mi_attr_noexcept {
  if (stats == NULL) return;
  stats->purged_bytes       = mi_holes_load(&mi_holes_bytes);
  stats->purged_blocks      = mi_holes_load(&mi_holes_blocks);
  stats->purged_bytes_total = mi_holes_load(&mi_holes_bytes_total);
  stats->discard_calls      = mi_holes_load(&mi_holes_discard_calls);
  stats->reuse_calls        = mi_holes_load(&mi_holes_reuse_calls);
  stats->pages_freed        = mi_holes_load(&mi_holes_pages_freed);
  stats->ineligible_pages      = mi_holes_load(&mi_holes_inelig_pages);
  stats->ineligible_bytes      = mi_holes_load(&mi_holes_inelig_bytes);
  stats->ineligible_free_bytes = mi_holes_load(&mi_holes_inelig_free_bytes);
  stats->unformed_bytes         = mi_holes_load(&mi_holes_unformed_bytes);
  stats->unformed_bytes_total   = mi_holes_load(&mi_holes_unformed_bytes_total);
  stats->unformed_discard_calls = mi_holes_load(&mi_holes_unformed_discard_calls);
  stats->unformed_reuse_calls   = mi_holes_load(&mi_holes_unformed_reuse_calls);
  stats->pages_skipped          = mi_holes_load(&mi_holes_pages_skipped);
  stats->blocks_visited         = mi_holes_load(&mi_holes_blocks_visited);
  stats->full_sweeps            = mi_holes_load(&mi_holes_full_sweeps);
}

void _mi_page_holes_count_page_freed(void) {
  mi_atomic_addi64_relaxed(&mi_holes_pages_freed, 1);
}

// The pages the sweep could not touch at all, so it is visible what hole punching does
// *not* reach. A gauge over the last sweep: `_mi_purge_holes_of` zeroes it before it starts.
void _mi_page_holes_reset_ineligible(void) {
  mi_atomic_addi64_relaxed(&mi_holes_inelig_pages, -(int64_t)mi_holes_load(&mi_holes_inelig_pages));
  mi_atomic_addi64_relaxed(&mi_holes_inelig_bytes, -(int64_t)mi_holes_load(&mi_holes_inelig_bytes));
  mi_atomic_addi64_relaxed(&mi_holes_inelig_free_bytes, -(int64_t)mi_holes_load(&mi_holes_inelig_free_bytes));
}

void _mi_page_holes_count_ineligible(const mi_page_t* page) {
  // Blocks are conserved (see `mi_page_is_valid_init`): free-listed == capacity - used - purged.
  // Eligibility is fixed for a page's lifetime (page size, block size, memid), so an ineligible
  // page never carries a purged block and the last term is 0 -- O(1), on every page of every sweep.
  mi_assert_internal(!mi_page_has_purged(page));
  const size_t nfree = (size_t)(page->capacity - page->used);
  mi_atomic_addi64_relaxed(&mi_holes_inelig_pages, 1);
  mi_atomic_addi64_relaxed(&mi_holes_inelig_bytes, (int64_t)mi_page_size(page));
  mi_atomic_addi64_relaxed(&mi_holes_inelig_free_bytes, (int64_t)(nfree * page->block_size));
}

static void mi_holes_count_discard(size_t bytes) {
  mi_atomic_addi64_relaxed(&mi_holes_discard_calls, 1);
  mi_atomic_addi64_relaxed(&mi_holes_bytes_total, (int64_t)bytes);
  mi_atomic_addi64_relaxed(&mi_holes_bytes, (int64_t)bytes);
}

static void mi_holes_count_blocks_off(size_t blocks) {
  mi_atomic_addi64_relaxed(&mi_holes_blocks, (int64_t)blocks);
}

static void mi_holes_count_reuse(size_t bytes, size_t blocks, bool reused) {
  if (reused) {
    mi_atomic_addi64_relaxed(&mi_holes_reuse_calls, 1);
    mi_atomic_addi64_relaxed(&mi_holes_bytes, -(int64_t)bytes);
  }
  mi_atomic_addi64_relaxed(&mi_holes_blocks, -(int64_t)blocks);
}


/* -----------------------------------------------------------
  Sweep state

  The state of a running sweep lives on the tld being swept (`tld->holes_sweep*`, see
  `types.h`), never in thread-locals of the sweeping thread. Besides the scavenger sweeping
  many tlds from one thread, this must not touch a `__thread` variable at all:
  `_mi_page_purge_holes_in_progress` is read from `mi_page_free_collect_ex`, inside the
  allocator, and on targets where `__thread` is emulated (Android before API 29) the first
  access on a thread allocates -- re-entering the collect that is reading it, without bound
  (oven-sh/bun#38051). The tld of the calling thread is reached through the default theap,
  which every TLS model can read without allocating.
----------------------------------------------------------- */

// Re-entrancy guard: while a sweep is rewriting a page's free list and bitmap, a nested `mi_malloc`
// on the sweeping thread (only reachable through a user output function from a warning message)
// must not un-purge a hole from under it. That nested allocation comes out of the calling thread's
// own theaps, so it is its own tld that matters here: on the owner that is the tld being swept;
// the scavenger has no theaps of its own being swept (it sweeps a parked thread's theaps, and the
// abandoned pages it touches are claimed), so un-purging there is harmless.
bool _mi_page_purge_holes_in_progress(void) {
  mi_theap_t* const theap = _mi_theap_default();
  if (theap == NULL || theap->tld == NULL) return false;
  return theap->tld->holes_sweeping;
}

void _mi_page_purge_holes_begin(mi_tld_t* tld) {
  mi_assert_internal(tld != NULL && !tld->holes_sweeping);
  tld->holes_sweeping = true;
}

// Also folds the per-sweep counters into the process-wide ones. The sweep runs over every page of
// the thread, so a process-wide atomic per page would be a real cost on the very path we are making
// cheap: they accumulate on the tld and are folded in once per pass.
void _mi_page_purge_holes_end(mi_tld_t* tld) {
  mi_assert_internal(tld != NULL && tld->holes_sweeping);
  tld->holes_sweeping = false;
  if (tld->holes_sweep_skipped > 0) {
    mi_atomic_addi64_relaxed(&mi_holes_pages_skipped, (int64_t)tld->holes_sweep_skipped);
    tld->holes_sweep_skipped = 0;
  }
  if (tld->holes_sweep_visited > 0) {
    mi_atomic_addi64_relaxed(&mi_holes_blocks_visited, (int64_t)tld->holes_sweep_visited);
    tld->holes_sweep_visited = 0;
  }
}

// Called once per idle sweep of `tld`'s heaps, before its passes (`_mi_purge_holes_of`): decides
// whether this sweep ignores `page->swept_state` (see `_mi_page_purge_holes`).
void _mi_page_purge_holes_sweep_begin(mi_tld_t* tld) {
  const long every = mi_option_get(mi_option_purge_holes_full_every);
  const size_t seq = ++tld->holes_sweep_seq;
  tld->holes_sweep_full = (every > 0 && (seq % (size_t)every) == 0);
  if (tld->holes_sweep_full) { mi_atomic_addi64_relaxed(&mi_holes_full_sweeps, 1); }
}


/* -----------------------------------------------------------
  Discarding and un-discarding
----------------------------------------------------------- */

static inline bool mi_page_bits_at(const uint64_t* bits, size_t k) {
  mi_assert_internal(k < MI_PAGE_PURGE_BITS);
  return ((bits[k / 64] >> (k % 64)) & 1) != 0;
}

// does the block at index `idx` overlap any OS page in `bits`?
static bool mi_page_block_overlaps(const mi_page_t* page, size_t idx, const uint64_t* bits) {
  size_t kfirst, klast;
  mi_page_block_os_pages(page, idx, &kfirst, &klast);
  for (size_t k = kfirst; k <= klast && k < MI_PAGE_PURGE_BITS; k++) {
    if (mi_page_bits_at(bits, k)) return true;
  }
  return false;
}

// Discard `[dstart,dstart+dsize)` of `page`, checking the profiler invariant first (#272).
static bool mi_page_holes_discard(mi_page_t* page, uintptr_t dstart, size_t dsize) {
  #if MI_PPROF && MI_DEBUG
  // #272 profiler-interaction invariant (1): before the discard, so the debug eager-zero in
  // `_mi_os_discard` cannot destroy the evidence of a mis-scoped range.
  _mi_prof_debug_assert_no_records_in(page, (void*)dstart, dsize);
  #endif
  return _mi_os_discard(mi_page_subproc(page), (void*)dstart, dsize);
}

// Bring the discarded OS pages [k0,k1] back. `discarded` is false when the discard itself
// failed, in which case the memory was never released and needs no `reuse`.
// Tells the OS we are using the memory again *before* any block in it is written to, then
// pushes every block that is whole again back onto the free list. A block at either end of
// the range can still overlap a hole we are not touching: those stay purged.
static void mi_page_unpurge_range(mi_page_t* page, size_t k0, size_t k1, bool discarded) {
  mi_assert_internal(k0 <= k1 && k1 < MI_PAGE_PURGE_BITS);
  const size_t os_size = _mi_os_page_size();
  const uintptr_t dstart = mi_page_purge_base(page) + (k0 * os_size);
  const size_t dsize = ((k1 - k0) + 1) * os_size;
  if (discarded) { _mi_os_reuse(mi_page_subproc(page), (void*)dstart, dsize); }

  // clear the bits first: `mi_page_block_index_is_purged` then tells us exactly which
  // blocks are whole again
  for (size_t k = k0; k <= k1; k++) { mi_page_purged_clear(page, k); }
  mi_page_sweep_state_invalidate(page);   // the free list is about to grow, but `used`/`capacity` will not
                                          // (before any early return below: the bits are already cleared)

  size_t first, last, first1, last1;
  if (!mi_page_os_page_blocks(page, k0, &first, &last) ||
      !mi_page_os_page_blocks(page, k1, &first1, &last1)) {
    mi_assert_internal(false);   // a discarded OS page is always entirely inside the block area
    return;
  }
  // every block in [first,last1] was free when we discarded, and a purged block cannot be
  // allocated or freed, so they are all still free
  size_t nblocks = 0;
  for (size_t i = last1 + 1; i > first; i--) {   // descending: the free list stays in address order
    const size_t idx = i - 1;
    if (mi_page_block_index_is_purged(page, idx)) continue;   // still overlaps another hole
    mi_block_t* const block = mi_page_block_index_at(page, idx);
    mi_block_set_next(page, block, page->free);
    page->free = block;
    nblocks++;
  }
  page->free_is_zero = false;
  mi_page_sweep_state_invalidate(page);   // the free list grew, but `used`/`capacity` did not
  mi_holes_count_reuse(dsize, nblocks, discarded);
}


/* -----------------------------------------------------------
  The unformed tail.

  The blocks in `[capacity, reserved)` were never handed out: `mi_page_extend_free`
  formats them lazily, a few OS pages worth at a time. They still cost memory though --
  a page is carved from an arena slice that had a previous life, so its tail is already
  resident, dirtying memory for blocks that may never exist.

  So we discard it too, but NOT through the `purged` bitmap: a bit there means "this block
  is free and off every free list" (`_mi_page_holes_assert_valid`), and an unformed block is
  on no list and has no identity yet. The region is contiguous and only shrinks from the left
  as `capacity` grows, so two offsets say everything there is to say.

  `mi_page_extend_free` calls `_mi_page_unpurge_unformed_upto` on exactly the range it is
  about to format, *before* it writes the first free-list pointer into it.
----------------------------------------------------------- */

// Can this page's memory be discarded at ALL? Pinned (large/huge OS pages) memory cannot be
// madvise'd away, and an arena with a custom commit function owns its own decommit. Note this
// is deliberately weaker than `mi_page_can_purge_holes`, which also rejects pages whose OS
// pages do not fit the `purged` bitmap -- the unformed tail needs no bitmap.
static bool mi_page_holes_madvisable(const mi_page_t* page) {
  if (page->memid.is_pinned) return false;
  const mi_arena_t* const arena = mi_memid_arena(page->memid);
  return (arena == NULL || arena->commit_fun == NULL);
}

// The OS pages that lie wholly inside the unformed tail *and* inside the committed part of
// the page: `[align_up(page_start + capacity*bs), align_down(min(page_start + reserved*bs, committed_end)))`.
// Empty (lo == hi) when there is no tail, when it is smaller than an OS page, or when the
// page has no committed memory there (`slice_committed`).
static void mi_page_unformed_tail_range(const mi_page_t* page, uintptr_t* lo, uintptr_t* hi) {
  *lo = 0; *hi = 0;
  if (page->capacity >= page->reserved) return;    // no tail
  const size_t os_size = _mi_os_page_size();
  const uintptr_t pstart = (uintptr_t)mi_page_start(page);
  const uintptr_t tlo = pstart + ((size_t)page->capacity * page->block_size);
  uintptr_t thi = pstart + ((size_t)page->reserved * page->block_size);
  const uintptr_t climit = pstart + mi_page_committed(page);   // never discard memory that is not committed
  if (thi > climit) { thi = climit; }
  const uintptr_t alo = _mi_align_up(tlo, os_size);
  const uintptr_t ahi = _mi_align_down(thi, os_size);
  if (alo >= ahi) return;
  *lo = alo;
  *hi = ahi;
}

size_t _mi_page_unformed_purged_bytes(const mi_page_t* page) {
  return (page->unformed_purged_hi > page->unformed_purged_lo
            ? (size_t)(page->unformed_purged_hi - page->unformed_purged_lo) : 0);
}

// Discard the OS pages of the unformed tail that are not discarded already.
static void mi_page_purge_unformed_tail(mi_page_t* page) {
  if (!mi_page_holes_madvisable(page)) return;
  uintptr_t lo, hi;
  mi_page_unformed_tail_range(page, &lo, &hi);
  if (lo >= hi) return;
  const uintptr_t pstart = (uintptr_t)mi_page_start(page);
  mi_assert_internal(hi - pstart <= UINT32_MAX);   // only a huge page can be that big, and it has no tail
  if (hi - pstart > UINT32_MAX) return;

  // the part of the tail that is not discarded yet (the tail can only grow to the right,
  // when `mi_page_extend_free` commits more of the page)
  uintptr_t dlo = lo;
  const size_t already = _mi_page_unformed_purged_bytes(page);
  if (already > 0) {
    mi_assert_internal(pstart + page->unformed_purged_lo >= lo);   // extend un-discards what it formats
    const uintptr_t uhi = pstart + page->unformed_purged_hi;
    if (uhi > dlo) { dlo = uhi; }
    lo = pstart + page->unformed_purged_lo;
  }
  if (dlo >= hi) return;   // nothing new

  if (!mi_page_holes_discard(page, dlo, (size_t)(hi - dlo))) return;   // the discard failed: leave the page as it was
  page->unformed_purged_lo = (uint32_t)(lo - pstart);
  page->unformed_purged_hi = (uint32_t)(hi - pstart);
  mi_atomic_addi64_relaxed(&mi_holes_unformed_discard_calls, 1);
  mi_atomic_addi64_relaxed(&mi_holes_unformed_bytes_total, (int64_t)(hi - dlo));
  mi_atomic_addi64_relaxed(&mi_holes_unformed_bytes, (int64_t)(hi - dlo));
}

// Tell the OS we are using the discarded unformed tail below `end` again, *before* anything
// in it is written to. `end` is an absolute address (`UINTPTR_MAX` for the whole tail); it is
// rounded up to an OS page, as the discard covers whole OS pages.
void _mi_page_unpurge_unformed_upto(mi_page_t* page, uintptr_t end) {
  if (_mi_page_unformed_purged_bytes(page) == 0) return;
  const size_t os_size = _mi_os_page_size();
  const uintptr_t pstart = (uintptr_t)mi_page_start(page);
  const uintptr_t rlo = pstart + page->unformed_purged_lo;
  const uintptr_t rhi = pstart + page->unformed_purged_hi;
  uintptr_t rend;
  if (end >= rhi) { rend = rhi; }   // (also the `UINTPTR_MAX` case: `_mi_align_up` would overflow)
  else {
    rend = _mi_align_up(end, os_size);
    if (rend > rhi) { rend = rhi; }
  }
  if (rend <= rlo) return;   // nothing of the discarded tail is needed yet

  _mi_os_reuse(mi_page_subproc(page), (void*)rlo, (size_t)(rend - rlo));
  if (rend >= rhi) { page->unformed_purged_lo = 0; page->unformed_purged_hi = 0; }
  else { page->unformed_purged_lo = (uint32_t)(rend - pstart); }
  mi_atomic_addi64_relaxed(&mi_holes_unformed_reuse_calls, 1);
  mi_atomic_addi64_relaxed(&mi_holes_unformed_bytes, -(int64_t)(rend - rlo));
}


/* -----------------------------------------------------------
  The sweep of a single page
----------------------------------------------------------- */

// Walk the free list of a page and discard every OS page in it that holds no live block.
// Returns false if any discard failed: those blocks went straight back on the free list and the
// page must be swept again, so the caller must not record it as swept.
static bool mi_page_purge_holes_walk(mi_page_t* page, mi_tld_t* tld) {
  if (page->free == NULL) return true;                    // nothing to take off the free list

  const size_t os_size = _mi_os_page_size();
  const size_t nbits = mi_page_purge_bits(page);
  mi_assert_internal(nbits <= MI_PAGE_PURGE_BITS);
  if (nbits > MI_PAGE_PURGE_BITS) return true;
  bool complete = true;

  // 1. count, per OS page, the blocks on the free list that overlap it
  uint16_t nfree[MI_PAGE_PURGE_BITS];
  _mi_memzero(nfree, nbits * sizeof(uint16_t));
  size_t nvisited = 0;
  for (mi_block_t* b = page->free; b != NULL; b = mi_block_next(page, b)) {
    const size_t idx = mi_page_block_index(page, b);
    mi_assert_internal(idx < page->capacity);
    mi_assert_internal(!mi_page_block_index_is_purged(page, idx));   // it is on the free list, so not purged
    nvisited++;
    size_t kfirst, klast;
    mi_page_block_os_pages(page, idx, &kfirst, &klast);
    for (size_t k = kfirst; k <= klast && k < nbits; k++) {
      mi_assert_internal(nfree[k] < UINT16_MAX);
      nfree[k]++;
    }
  }
  tld->holes_sweep_visited += nvisited;   // folded into the process-wide counter at the end of the pass

  // 2. an OS page can be discarded when *every* block overlapping it is free -- either on the
  //    free list, or purged already. Of the blocks overlapping an OS page, only the first and
  //    the last can stick out into another OS page, so only those two can be purged already
  //    (any other block lies entirely inside this OS page, whose bit is clear here).
  uint64_t todo[MI_PAGE_PURGE_WORDS];
  for (size_t i = 0; i < MI_PAGE_PURGE_WORDS; i++) { todo[i] = 0; }
  size_t ntodo = 0;
  for (size_t k = 0; k < nbits; k++) {
    if (mi_page_os_page_purged(page, k)) continue;                 // discarded already
    size_t first, last;
    if (!mi_page_os_page_blocks(page, k, &first, &last)) continue; // not entirely inside the block area
    size_t nfreek = nfree[k];
    if (mi_page_block_index_is_purged(page, first)) { nfreek++; }
    if (last != first && mi_page_block_index_is_purged(page, last)) { nfreek++; }
    mi_assert_internal(nfreek <= (last - first) + 1);
    if (nfreek != (last - first) + 1) continue;                    // some block overlapping it is still live
    todo[k / 64] |= ((uint64_t)1 << (k % 64));
    ntodo++;
  }
  if (ntodo == 0) return true;   // nothing discardable: the page IS fully swept

  // 3. rebuild the free list without the blocks that are about to lose memory. This must
  //    happen *before* the discard: it walks `next` pointers that live in the very memory
  //    we are about to discard.
  mi_block_t* keep = NULL;
  size_t ndropped = 0;
  mi_block_t* b = page->free;
  while (b != NULL) {
    mi_block_t* const next = mi_block_next(page, b);
    if (mi_page_block_overlaps(page, mi_page_block_index(page, b), todo)) {
      ndropped++;   // it becomes purged in step 4
    }
    else {
      mi_block_set_next(page, b, keep);
      keep = b;
    }
    b = next;
  }
  page->free = keep;
  page->free_is_zero = false;   // discarded memory reads back zero or stale; never assume

  // 4. mark the OS pages as discarded (this is what makes the blocks we just dropped
  //    "purged"), then discard them a maximal run at a time.
  for (size_t i = 0; i < MI_PAGE_PURGE_WORDS; i++) { page->purged[i] |= todo[i]; }
  mi_holes_count_blocks_off(ndropped);
  size_t k = 0;
  while (k < nbits) {
    if (!mi_page_bits_at(todo, k)) { k++; continue; }
    const size_t k0 = k;
    while (k < nbits && mi_page_bits_at(todo, k)) { k++; }
    const size_t dsize = (k - k0) * os_size;
    if (mi_page_holes_discard(page, mi_page_purge_base(page) + (k0 * os_size), dsize)) {
      mi_holes_count_discard(dsize);
    }
    else {
      // the discard failed: the memory is intact, so hand these blocks straight back
      mi_page_unpurge_range(page, k0, k - 1, false /* nothing was discarded, so no reuse */);
      complete = false;
    }
  }
  return complete;
}

// Discard the memory of the free blocks in a still-used page.
//
// The free blocks a sweep leaves behind are the ones it could NOT discard (their OS page still
// holds a live block), and they stay on `page->free`. Walking them again finds exactly the same
// thing, so a sweep that follows one with nothing in between must not walk them: on a thread that
// parks often that re-walk is the dominant cost, and it grows with uptime. `page->swept_state` --
// the `(capacity,used)` we left the page in -- is the O(1) guard, and it is sound in one
// direction:
//
//   an OS page that is discardable now but was not at the end of the last sweep must have gained
//   a free block since (that sweep discarded EVERY OS page all of whose blocks were free), and a
//   block can only become free by being freed (`used` down) or by being formed (`capacity` up).
//
// So an unchanged `(capacity,used)` means at most that the page CHURNED: as many frees as allocs,
// leaving `used` where it was but a different set of blocks free -- which can hide a discardable
// OS page from the check. That is a missed discard, never a correctness bug (the block bookkeeping
// is exact either way), but a steady-state server can sit at the same `used` at every park, so it
// would not heal on its own. `purge_holes_full_every` bounds it: every N'th sweep walks every page
// regardless, which caps the delay of a missed discard at N parks for 1/N of the old cost.
// (An exact "was anything freed in this page" bit is the alternative, and it costs a store in
// `mi_free` itself -- the hot path this whole feature stays off.)
void _mi_page_purge_holes(mi_page_t* page, mi_tld_t* tld) {
  mi_assert_internal(page != NULL && tld != NULL);
  mi_assert_internal(tld->holes_sweeping);
  if (!mi_option_is_enabled(mi_option_purge_holes)) return;
  if (mi_page_all_free(page)) return;                     // the page itself is about to be freed
  if (mi_option_get(mi_option_purge_delay) < 0) return;   // purging disabled
  mi_page_purge_unformed_tail(page);                      // the blocks that are not formed yet: resident, but never handed out
  if (!mi_page_can_purge_holes(page)) { _mi_page_holes_count_ineligible(page); return; }

  if (!tld->holes_sweep_full && page->swept_state == mi_page_sweep_state(page)) {
    tld->holes_sweep_skipped++;   // nothing was allocated or freed in this page since we swept it
    return;
  }
  // Record the state we LEAVE the page in, read back from the page: a nested `mi_malloc` (see
  // `_mi_page_purge_holes_in_progress`) may have taken a block out of it while we walked.
  //
  // Only if the walk got everything. A failed `_mi_os_discard` (ENOMEM under pressure) puts its
  // blocks straight back, and changes neither `capacity` nor `used` -- so recording here would
  // say "already swept" for a page that still has holes, and the skip check would then park them
  // until the next full sweep, or forever with `purge_holes_full_every=0`.
  if (mi_page_purge_holes_walk(page, tld)) {
    page->swept_state = mi_page_sweep_state(page);
  }
}

// Bring the first run of discarded OS pages back onto the free list. Only that run is
// touched, so the other holes in the page stay discarded. A whole run at a time (and not
// one OS page at a time) so that the `_mi_os_reuse` is one call and the following
// allocations from this page hit the fast path instead of a syscall per block.
bool _mi_page_unpurge_run(mi_page_t* page) {
  if (!mi_page_has_purged(page)) return false;
  size_t k0 = 0;
  while (k0 < MI_PAGE_PURGE_BITS && !mi_page_os_page_purged(page, k0)) { k0++; }
  mi_assert_internal(k0 < MI_PAGE_PURGE_BITS);
  if (k0 >= MI_PAGE_PURGE_BITS) return false;
  size_t k1 = k0;
  while (k1 + 1 < MI_PAGE_PURGE_BITS && mi_page_os_page_purged(page, k1 + 1)) { k1++; }
  mi_page_unpurge_range(page, k0, k1, true);
  return true;
}

// Undo every hole in the page (the page is going back to the arena, which may hand the
// memory out as committed without any further `reuse` call). The page is dead here (every
// block is free), so we do NOT rebuild its free list: writing `next` pointers into the
// holes would fault every discarded OS page right back in.
void _mi_page_unpurge_all(mi_page_t* page) {
  _mi_page_unpurge_unformed_upto(page, UINTPTR_MAX);   // the unformed tail goes back as well
  if (!mi_page_has_purged(page)) return;
  const size_t os_size = _mi_os_page_size();
  const uintptr_t base = mi_page_purge_base(page);
  size_t k = 0;
  while (k < MI_PAGE_PURGE_BITS) {
    if (!mi_page_os_page_purged(page, k)) { k++; continue; }
    const size_t k0 = k;
    while (k < MI_PAGE_PURGE_BITS && mi_page_os_page_purged(page, k)) { k++; }
    const size_t dsize = (k - k0) * os_size;
    _mi_os_reuse(mi_page_subproc(page), (void*)(base + (k0 * os_size)), dsize);
    size_t first, last, first1, last1;
    if (mi_page_os_page_blocks(page, k0, &first, &last) &&
        mi_page_os_page_blocks(page, k - 1, &first1, &last1)) {
      mi_holes_count_reuse(dsize, (last1 - first) + 1, true);
    }
    else {
      mi_assert_internal(false);   // a discarded OS page is always entirely inside the block area
    }
  }
  _mi_page_purged_reset(page);
}


/* -----------------------------------------------------------
  The idle sweep

  Visit every page (INCLUDING the full queue, which a normal collect skips -- see
  `mi_theap_collect_ex`) and discard the memory of free blocks inside pages that are still
  partially used. Meant to be called when the application knows it is idle (e.g. from an
  event loop about to park): it costs a few madvise calls and nothing on the alloc/free
  hot path.

  DEVIATION from Bun (CLAUDE.md rule 6): these drivers are `src/theap.c` statics there.
----------------------------------------------------------- */

static bool mi_theap_page_purge_holes(mi_theap_t* theap, mi_page_queue_t* pq, mi_page_t* page, void* arg_tld, void* arg2) {
  MI_UNUSED(arg2);
  mi_tld_t* const tld = (mi_tld_t*)arg_tld;   // the tld being swept (== theap->tld)
  // When the scavenger is doing this for a parked thread, the owner may wake at any moment and
  // has to wait for us. Stopping between pages bounds that wait to one page's walk; the pages we
  // skip are simply swept at the next park (`swept_state` makes the re-walk cheap).
  if (theap->tld != NULL && mi_atomic_load_relaxed(&theap->tld->park_reclaim) != 0) return false;
  // force: fold local_free (and thread_free) into `free` first. Never un-purge here: we are about
  // to purge, and a run brought back now would be discarded again right away (see `mi_theap_page_collect`).
  _mi_page_free_collect_no_unpurge(page, true);
  if (mi_page_all_free(page)) {
    // the forced collect emptied the page: hand it back instead of leaving it resident
    _mi_page_holes_count_page_freed();
    _mi_page_free(page, pq);
    return true;
  }
  _mi_page_purge_holes(page, tld);
  mi_assert_expensive(_mi_page_is_valid(page));
  return true; // continue
}

static void mi_theap_purge_holes(mi_theap_t* theap) mi_attr_noexcept {
  if (theap == NULL || !mi_theap_is_initialized(theap)) return;
  if (!mi_option_is_enabled(mi_option_purge_holes)) return;
  // This rewrites the thread-local free list of every page, so it may only run when the owner is
  // not allocating. Two ways to know that: we ARE the owner, or the owner published MI_PARK_PARKED
  // and the scavenger claimed it (MI_PARK_SWEEPING) -- the same "owner is quiesced" precondition
  // `mi_theap_collect` already relies on for its non-owner callers (see python/cpython#112532).
  if (theap->tld == NULL) return;
  if (theap->tld->thread_id != _mi_thread_id() &&
      mi_atomic_load_acquire(&theap->tld->park_state) != MI_PARK_SWEEPING) return;
  mi_tld_t* const tld = theap->tld;
  _mi_page_purge_holes_begin(tld);
  _mi_theap_visit_pages(theap, &mi_theap_page_purge_holes, true /* include full pages */, tld, NULL);
  _mi_page_purge_holes_end(tld);
}

// Purge the holes in every page this thread may safely touch:
//  - the pages of every theap of this thread (`page->free`/`used` are plain fields that only the
//    owning thread may write, so we can never do this for a theap of another thread), and
//  - the abandoned pages of the heaps behind those theaps: those have no owning thread and are
//    claimed through the arena ownership protocol (see `_mi_arenas_purge_abandoned_holes`).
// The abandoned pages matter: with the default `allow_page_abandon`, every page that ever became
// full ends up there. Non-default heaps matter too (JSC allocates its structure heap with
// `mi_heap_new_in_arena`), which is why we sweep every theap and not just the default one.
#define MI_PURGE_HOLES_MAX_HEAPS  (8)

void _mi_purge_holes_of(mi_tld_t* tld) {
  if (!mi_option_is_enabled(mi_option_purge_holes)) return;
  if (tld == NULL) return;
  _mi_page_purge_holes_sweep_begin(tld);  // decides whether this sweep skips unchanged pages
  _mi_page_holes_reset_ineligible();      // the ineligible counters are a gauge over this sweep

  mi_heap_t* heaps[MI_PURGE_HOLES_MAX_HEAPS];
  size_t heap_count = 0;

  // Hold `tld->theaps_lock` for the whole sweep, including the abandoned-page pass below:
  //  - another thread can unlink a theap from this list in `_mi_heap_detach_theaps`, and
  //  - it keeps every `heaps[i]` alive: a heap is only freed by `mi_heap_delete`/`mi_heap_destroy`
  //    *after* `_mi_heap_detach_theaps` detached every theap of it, and detaching our theap needs this
  //    lock (it try-acquires it and retries), so it cannot complete while we hold it. Reading a
  //    `heaps[i]` outside the lock is a use-after-free (`heap->subproc`).
  mi_lock(&tld->theaps_lock) {
    for (mi_theap_t* theap = tld->theaps; theap != NULL; theap = theap->tnext) {
      mi_theap_purge_holes(theap);
      mi_heap_t* const heap = _mi_theap_heap(theap);
      if (heap != NULL && heap_count < MI_PURGE_HOLES_MAX_HEAPS) {
        bool seen = false;
        for (size_t i = 0; i < heap_count; i++) { if (heaps[i] == heap) { seen = true; break; } }
        if (!seen) { heaps[heap_count++] = heap; }
      }
    }
    for (size_t i = 0; i < heap_count; i++) {
      if (mi_atomic_load_relaxed(&tld->park_reclaim) != 0) break;
      _mi_arenas_purge_abandoned_holes(heaps[i], tld);
    }
  }
}


/* -----------------------------------------------------------
  Hole report

  After a sweep, the memory that hole punching did *not* get back is the free blocks that
  share an OS page with a live block: an OS page is discarded only when every block
  overlapping it is free, so a single live block pins the whole OS page. This accounts for
  that, per size class, and says how many live blocks each pinned OS page is holding.

  Read-only: it does not purge, un-purge, collect, or touch a free list. It walks the three
  free lists in place instead of collecting them.

  A block is exactly one of:
   - free-listed: on `free`, `local_free`, or `xthread_free`;
   - purged: free but held off every list because its memory is discarded. This is derived
     from the OS-page bitmap (`mi_page_block_index_is_purged`: a block is purged iff it
     overlaps a discarded OS page), which is how the rest of the code derives it too;
   - live: everything else. Note `page->used` counts the not-yet-collected `xthread_free`
     blocks as used, so live is *not* `page->used` -- we take it as the complement of the
     other two (the conservation invariant is asserted in `mi_page_is_valid_init`).

  Bytes are attributed per OS page, by overlap: every byte of every block lies in exactly
  one OS page, so nothing is double counted even for a block straddling a boundary.
----------------------------------------------------------- */

#define MI_HOLES_MAX_CAP  (1 << 16)   // `page->capacity` is a uint16_t

static void mi_holes_mark_free_list(const mi_page_t* page, mi_block_t* b, uint64_t* set) {
  const size_t cap = page->capacity;
  for (size_t n = 0; b != NULL && n <= cap; n++) {   // `n` bounds a corrupt or cyclic list
    const size_t idx = mi_page_block_index(page, b);
    if (idx >= cap) break;
    set[idx / 64] |= ((uint64_t)1 << (idx % 64));
    b = mi_block_next((mi_page_t*)page, b);
  }
}

static size_t mi_holes_hist_bucket(size_t nlive) {
  if (nlive <= 1) return 0;
  if (nlive == 2) return 1;
  if (nlive <= 4) return 2;
  if (nlive <= 8) return 3;
  return 4;
}

// the hypothetical OS page sizes of the granularity curve
size_t mi_holes_granularity(size_t g) {
  static const size_t grans[MI_HOLES_GRAN_COUNT] = { 4*MI_KiB, 8*MI_KiB, 16*MI_KiB, 32*MI_KiB, 64*MI_KiB };
  return (g < MI_HOLES_GRAN_COUNT ? grans[g] : 0);
}

// is the block at `idx` free? Either on a free list, or purged (free, but held off every list
// because its memory is already discarded). Anything else is live.
static bool mi_holes_block_is_free(const mi_page_t* page, const uint64_t* freelisted, size_t idx) {
  if (((freelisted[idx / 64] >> (idx % 64)) & 1) != 0) return true;
  return mi_page_block_index_is_purged(page, idx);
}

// How many bytes of this page would be discardable if the OS page size were `G`? A G-aligned,
// G-sized span can be discarded exactly when it lies wholly inside the block area and every
// block overlapping it is free -- the same rule the real sweep applies at `_mi_os_page_size()`.
static void mi_page_holes_granularity_curve(const mi_page_t* page, const uint64_t* freelisted, mi_holes_report_t* rep) {
  const size_t bs = page->block_size;
  const size_t cap = page->capacity;
  const uintptr_t pstart = (uintptr_t)mi_page_start(page);
  const uintptr_t pend = pstart + (cap * bs);
  for (size_t g = 0; g < MI_HOLES_GRAN_COUNT; g++) {
    const size_t gran = mi_holes_granularity(g);
    for (uintptr_t lo = _mi_align_down(pstart, gran); lo + gran <= pend; lo += gran) {
      if (lo < pstart) continue;   // not entirely inside the block area
      const size_t first = (size_t)(lo - pstart) / bs;
      const size_t last = (size_t)((lo + gran - 1) - pstart) / bs;
      bool all_free = true;
      for (size_t idx = first; idx <= last && idx < cap; idx++) {
        if (!mi_holes_block_is_free(page, freelisted, idx)) { all_free = false; break; }
      }
      if (all_free) { rep->discardable_at[g] += gran; }
    }
  }
}

void _mi_page_holes_report_page(const mi_page_t* page, mi_holes_report_t* rep) {
  if (page == NULL || rep == NULL) return;
  const size_t bs = page->block_size;
  const size_t cap = page->capacity;
  if (bs == 0 || cap > MI_HOLES_MAX_CAP) return;
  mi_holes_bin_t* const r = &rep->bin[_mi_bin(bs)];
  r->pages++;
  if (bs > r->block_size) { r->block_size = bs; }
  rep->total_pages++;
  rep->page_committed_bytes += mi_page_committed(page);
  if (page->reserved > cap) { rep->unformed_bytes += ((size_t)page->reserved - cap) * bs; }
  rep->unformed_discarded_bytes += _mi_page_unformed_purged_bytes(page);
  if (cap == 0) return;

  uint64_t freelisted[MI_HOLES_MAX_CAP / 64];
  const size_t nwords = _mi_divide_up(cap, 64);
  _mi_memzero(freelisted, nwords * sizeof(uint64_t));
  mi_holes_mark_free_list(page, page->free, freelisted);
  mi_holes_mark_free_list(page, page->local_free, freelisted);
  mi_holes_mark_free_list(page, mi_page_thread_free((mi_page_t*)page), freelisted);   // a concurrent free can push after this read: that block reads as live (a diagnostic, so this is fine)

  if (mi_page_holes_madvisable(page)) { mi_page_holes_granularity_curve(page, freelisted, rep); }
  else { rep->unmadvisable_pages++; }

  // An ineligible page has no discardable OS page at all (and carries no holes, see
  // `_mi_page_holes_assert_valid`), so every free block in it is undiscardable by definition.
  if (!mi_page_can_purge_holes(page)) {
    size_t nlive = 0;
    for (size_t idx = 0; idx < cap; idx++) {
      if (!mi_holes_block_is_free(page, freelisted, idx)) { nlive++; }
    }
    r->live_bytes += nlive * bs;
    r->free_bytes += (cap - nlive) * bs;
    r->undiscardable_bytes += (cap - nlive) * bs;
    r->ineligible_pages++;
    rep->ineligible_pages++;
    return;
  }

  const size_t os_size = _mi_os_page_size();
  const uintptr_t pstart = (uintptr_t)mi_page_start(page);
  const uintptr_t pend = pstart + (cap * bs);
  const uintptr_t base = mi_page_purge_base(page);
  const size_t nbits = mi_page_purge_bits(page);
  mi_assert_internal(nbits <= MI_PAGE_PURGE_BITS);

  for (size_t k = 0; k < nbits; k++) {
    const uintptr_t lo = base + (k * os_size);
    const uintptr_t hi = lo + os_size;
    const uintptr_t clo = (lo < pstart ? pstart : lo);
    const uintptr_t chi = (hi > pend ? pend : hi);
    if (clo >= chi) continue;                            // holds no block byte at all (the page header, or memory past `capacity`)
    const bool whole = (lo >= pstart && hi <= pend);     // entirely inside the block area -- only such an OS page can ever be discarded
    const size_t first = (size_t)(clo - pstart) / bs;
    const size_t last = (size_t)((chi - 1) - pstart) / bs;
    size_t live_ov = 0, free_ov = 0, nlive = 0;
    for (size_t idx = first; idx <= last && idx < cap; idx++) {
      const uintptr_t blo = pstart + (idx * bs);
      const uintptr_t bhi = blo + bs;
      const uintptr_t olo = (blo < lo ? lo : blo);
      const uintptr_t ohi = (bhi > hi ? hi : bhi);
      const size_t ov = (size_t)(ohi - olo);
      if (mi_holes_block_is_free(page, freelisted, idx)) { free_ov += ov; }
      else { live_ov += ov; nlive++; }
    }
    r->live_bytes += live_ov;
    r->free_bytes += free_ov;
    if (mi_page_os_page_purged(page, k)) {
      mi_assert_internal(whole && live_ov == 0 && free_ov == os_size);
      r->discarded_bytes += os_size;
    }
    else if (!whole) {
      // Not entirely inside the block area (it holds the page header, or memory past `capacity`),
      // so it is never discardable whatever lives in it. Checked BEFORE liveness on purpose:
      // counting it as pinned would blame a live block for an OS page that freeing that block
      // cannot release anyway, and `pinned_ospages` / the histogram are the whole point here.
      r->undiscardable_bytes += free_ov;
      r->edge_bytes += free_ov;
    }
    else if (nlive > 0) {
      r->undiscardable_bytes += free_ov;                 // pinned: a live block in this OS page keeps it resident
      r->pinned_ospages++;
      r->pinned_live_blocks += nlive;
      r->pinned_free_bytes += free_ov;
      r->pinned_live_bytes += live_ov;
      r->hist[mi_holes_hist_bucket(nlive)]++;
    }
    else {
      r->pending_bytes += free_ov;                       // fully free and discardable, but not discarded (no sweep yet, or the discard failed)
    }
  }
}

// bytes as "MB.hh", since the mimalloc printf has no %f
static void mi_holes_mb(size_t bytes, char* buf, size_t bufsize) {
  const size_t mb = bytes / MI_MiB;
  const size_t hundredths = ((bytes % MI_MiB) * 100) / MI_MiB;
  _mi_snprintf(buf, bufsize, "%zu.%02zu", mb, hundredths);
}

static void mi_holes_print_row(const char* name, const mi_holes_bin_t* r) {
  char slive[32], sfree[32], sundisc[32], sdisc[32];
  mi_holes_mb(r->live_bytes, slive, sizeof(slive));
  mi_holes_mb(r->free_bytes, sfree, sizeof(sfree));
  mi_holes_mb(r->undiscardable_bytes, sundisc, sizeof(sundisc));
  mi_holes_mb(r->discarded_bytes, sdisc, sizeof(sdisc));
  // live blocks per pinned OS page, to two decimals
  const size_t avg100 = (r->pinned_ospages == 0 ? 0 : (r->pinned_live_blocks * 100) / r->pinned_ospages);
  _mi_fprintf(NULL, NULL, "%10s %8zu %10s %10s %18s %13s %10zu.%02zu\n",
              name, r->pages, slive, sfree, sundisc, sdisc, avg100 / 100, avg100 % 100);
}

void _mi_page_holes_report_print(const mi_holes_report_t* rep) {
  if (rep == NULL) return;
  static const char* hist_name[MI_HOLES_HIST_BUCKETS] = { "1", "2", "3-4", "5-8", "9+" };

  _mi_fprintf(NULL, NULL, "\nholes report: os page = %zu bytes, %zu pages (%zu ineligible, %zu never madvise-able)\n",
              _mi_os_page_size(), rep->total_pages, rep->ineligible_pages, rep->unmadvisable_pages);

  mi_holes_bin_t total;
  _mi_memzero(&total, sizeof(total));
  for (size_t bin = 0; bin < MI_BIN_COUNT; bin++) {
    const mi_holes_bin_t* const r = &rep->bin[bin];
    total.live_bytes += r->live_bytes;
    total.free_bytes += r->free_bytes;
    total.pinned_ospages += r->pinned_ospages;
    total.pinned_free_bytes += r->pinned_free_bytes;
    total.pinned_live_bytes += r->pinned_live_bytes;
  }

  // THE measurement: what a smaller OS page would buy us. `discardable@4K - discardable@16K` is
  // the memory the 16KB darwin page costs us, measured directly here -- no cross-platform
  // subtraction, no RSS arithmetic. Nothing is discarded to produce these numbers.
  char sgran[32];
  _mi_fprintf(NULL, NULL, "  discardable bytes vs hypothetical OS page size (nothing is discarded to measure this):\n");
  for (size_t g = 0; g < MI_HOLES_GRAN_COUNT; g++) {
    const size_t gran = mi_holes_granularity(g);
    mi_holes_mb(rep->discardable_at[g], sgran, sizeof(sgran));
    _mi_fprintf(NULL, NULL, "    @%6zu : %10s MB%s\n", gran, sgran,
                (gran == _mi_os_page_size() ? "   <-- this machine's OS page size" : ""));
  }
  char slive[32], sfree[32], spfree[32], splive[32];
  mi_holes_mb(total.live_bytes, slive, sizeof(slive));
  mi_holes_mb(total.free_bytes, sfree, sizeof(sfree));
  mi_holes_mb(total.pinned_free_bytes, spfree, sizeof(spfree));
  mi_holes_mb(total.pinned_live_bytes, splive, sizeof(splive));
  _mi_fprintf(NULL, NULL, "  live %s MB, free %s MB\n", slive, sfree);
  _mi_fprintf(NULL, NULL, "  %zu pinned OS pages (>= 1 live block): %s MB live + %s MB free trapped in them\n",
              total.pinned_ospages, splive, spfree);

  // Where the memory IS. If the curve is flat, the free memory is not sitting inside the pages --
  // and then it is sitting here. "in-page free" is memory the PAGES are holding (contamination);
  // "arena slack" is memory held in NO page at all (a purge/arena problem). Those want completely
  // different fixes, so the split has to be explicit -- and so does what each number can't tell us.
  {
    char spage[32], soverhead[32], sslack[32], spend[32], smeta[32], scommit[32], sresv[32], sother[32];
    const size_t page_overhead = (rep->page_committed_bytes > total.live_bytes + total.free_bytes
                                   ? rep->page_committed_bytes - total.live_bytes - total.free_bytes : 0);
    const size_t touched = rep->page_committed_bytes + rep->arena_free_dirty_bytes + rep->arena_meta_bytes;
    mi_holes_mb(rep->page_committed_bytes, spage, sizeof(spage));
    mi_holes_mb(page_overhead, soverhead, sizeof(soverhead));
    mi_holes_mb(rep->arena_free_dirty_bytes, sslack, sizeof(sslack));
    mi_holes_mb(rep->arena_purge_pending_bytes, spend, sizeof(spend));
    mi_holes_mb(rep->arena_meta_bytes, smeta, sizeof(smeta));
    mi_holes_mb(touched, sother, sizeof(sother));
    mi_holes_mb(rep->arena_committed_bytes, scommit, sizeof(scommit));
    mi_holes_mb(rep->arena_reserved_bytes, sresv, sizeof(sresv));
    _mi_fprintf(NULL, NULL, "  memory partition (walking the arena bitmaps):\n");
    _mi_fprintf(NULL, NULL, "    in pages           : %10s MB  = live %s + in-page free %s + page overhead %s (header/unformed)\n",
                spage, slive, sfree, soverhead);
    _mi_fprintf(NULL, NULL, "    arena slack        : %10s MB  (in NO page, touched at least once; %s MB of it is queued for purge and so certainly still resident)\n",
                sslack, spend);
    _mi_fprintf(NULL, NULL, "    arena meta (ROUGH) : %10s MB  (arena bitmaps only; excludes the mi_meta heaps)\n", smeta);
    _mi_fprintf(NULL, NULL, "    ---- ever-touched  : %10s MB  (in-pages + slack + meta; the ceiling on what we can be paying for)\n", sother);
    _mi_fprintf(NULL, NULL, "    reserved %s MB, slices_committed %s MB -- NOTE: on POSIX every slice is marked committed at reserve\n", sresv, scommit);
    _mi_fprintf(NULL, NULL, "      time and a reset-purge never clears it, so slices_committed is address space, NOT residency.\n");
    _mi_fprintf(NULL, NULL, "      'arena slack' is an UPPER bound: a slice purged earlier still reads as dirty here.\n");
    _mi_fprintf(NULL, NULL, "      'in pages' misses pages owned by OTHER threads' theaps -- this walk cannot read them.\n");
  }

  _mi_fprintf(NULL, NULL, "%10s %8s %10s %10s %18s %13s %13s\n",
              "size_class", "pages", "live_MB", "free_MB", "undiscardable_MB", "discarded_MB", "avg_live_blocks_per_pinned_ospage");
  _mi_memzero(&total, sizeof(total));
  char name[32];
  for (size_t bin = 0; bin < MI_BIN_COUNT; bin++) {
    const mi_holes_bin_t* const r = &rep->bin[bin];
    if (r->pages == 0) continue;
    _mi_snprintf(name, sizeof(name), "%zu", r->block_size);
    mi_holes_print_row(name, r);
    total.pages += r->pages;
    total.live_bytes += r->live_bytes;
    total.free_bytes += r->free_bytes;
    total.undiscardable_bytes += r->undiscardable_bytes;
    total.discarded_bytes += r->discarded_bytes;
    total.edge_bytes += r->edge_bytes;
    total.pending_bytes += r->pending_bytes;
    total.pinned_ospages += r->pinned_ospages;
    total.pinned_live_blocks += r->pinned_live_blocks;
  }
  mi_holes_print_row("TOTAL", &total);

  char edge[32], pending[32], unformed[32], unformed_disc[32];
  mi_holes_mb(total.edge_bytes, edge, sizeof(edge));
  mi_holes_mb(total.pending_bytes, pending, sizeof(pending));
  mi_holes_mb(rep->unformed_bytes, unformed, sizeof(unformed));
  mi_holes_mb(rep->unformed_discarded_bytes, unformed_disc, sizeof(unformed_disc));
  _mi_fprintf(NULL, NULL, "  of undiscardable: %s MB lies in a partial OS page (page header / past capacity)\n", edge);
  _mi_fprintf(NULL, NULL, "  free and discardable but not discarded: %s MB;  blocks not formed yet: %s MB (of which discarded: %s MB)\n", pending, unformed, unformed_disc);

  // the 3 worst size classes by undiscardable bytes: how many live blocks pin each pinned OS page?
  size_t taken[3] = { MI_BIN_COUNT, MI_BIN_COUNT, MI_BIN_COUNT };
  for (size_t n = 0; n < 3; n++) {
    size_t worst = MI_BIN_COUNT;
    for (size_t bin = 0; bin < MI_BIN_COUNT; bin++) {
      const mi_holes_bin_t* const r = &rep->bin[bin];
      if (r->pinned_ospages == 0 || r->undiscardable_bytes == 0) continue;
      bool already = false;
      for (size_t i = 0; i < n; i++) { if (taken[i] == bin) { already = true; break; } }
      if (already) continue;
      if (worst == MI_BIN_COUNT || r->undiscardable_bytes > rep->bin[worst].undiscardable_bytes) { worst = bin; }
    }
    if (worst == MI_BIN_COUNT) break;
    taken[n] = worst;
    const mi_holes_bin_t* const r = &rep->bin[worst];
    char worst_undisc[32];
    mi_holes_mb(r->undiscardable_bytes, worst_undisc, sizeof(worst_undisc));
    _mi_fprintf(NULL, NULL, "  block_size %zu: %s MB undiscardable over %zu pinned OS pages; live blocks per pinned OS page:",
                r->block_size, worst_undisc, r->pinned_ospages);
    for (size_t h = 0; h < MI_HOLES_HIST_BUCKETS; h++) {
      _mi_fprintf(NULL, NULL, "  %s: %zu", hist_name[h], r->hist[h]);
    }
    _mi_fprintf(NULL, NULL, "\n");
  }
  _mi_fprintf(NULL, NULL, "  (a live block straddling two pinned OS pages counts in both; abandoned pages are only reached when they are in the arena's abandoned map)\n");
}

// Report what hole punching leaves behind. Same traversal and same ownership rules as
// `_mi_purge_holes_of` -- every theap of this thread, plus the abandoned pages of the heaps
// behind them -- but read-only: it collects nothing, purges nothing, un-purges nothing, and
// never touches a free list.
static bool mi_theap_page_holes_report(mi_theap_t* theap, mi_page_queue_t* pq, mi_page_t* page, void* arg1, void* arg2) {
  MI_UNUSED(theap); MI_UNUSED(pq); MI_UNUSED(arg2);
  _mi_page_holes_report_page(page, (mi_holes_report_t*)arg1);
  return true; // continue
}

void _mi_purge_holes_report_collect(mi_holes_report_t* rep) {
  if (rep == NULL) return;
  _mi_memzero(rep, sizeof(*rep));
  mi_theap_t* const theap0 = _mi_theap_default();
  if (theap0 == NULL || !mi_theap_is_initialized(theap0) || theap0->tld == NULL) return;
  mi_tld_t* const tld = theap0->tld;
  if (tld->thread_id != _mi_thread_id()) return;   // owner thread only, exactly as for the sweep

  mi_heap_t* heaps[MI_PURGE_HOLES_MAX_HEAPS];
  size_t heap_count = 0;

  // hold the lock for the whole walk -- it also keeps the heaps alive, see `_mi_purge_holes_of`
  mi_lock(&tld->theaps_lock) {
    for (mi_theap_t* theap = tld->theaps; theap != NULL; theap = theap->tnext) {
      if (!mi_theap_is_initialized(theap)) continue;
      _mi_theap_visit_pages(theap, &mi_theap_page_holes_report, true /* include full pages */, rep, NULL);
      mi_heap_t* const heap = _mi_theap_heap(theap);
      if (heap != NULL && heap_count < MI_PURGE_HOLES_MAX_HEAPS) {
        bool seen = false;
        for (size_t i = 0; i < heap_count; i++) { if (heaps[i] == heap) { seen = true; break; } }
        if (!seen) { heaps[heap_count++] = heap; }
      }
    }
    for (size_t i = 0; i < heap_count; i++) {
      _mi_arenas_holes_report(heaps[i], rep);
    }
    // The committed partition is a property of the subprocess's arenas, not of a heap, so count it
    // once: every heap of this thread reaches the same arenas.
    if (heap_count > 0) { _mi_arenas_holes_committed(heaps[0], rep); }
  }
}

void mi_purge_holes_report(void) mi_attr_noexcept {
  mi_holes_report_t rep;
  _mi_purge_holes_report_collect(&rep);
  _mi_page_holes_report_print(&rep);
}
