/* ----------------------------------------------------------------------------
Copyright (c) 2026, the mimalloc-pprof authors
This is free software; you can redistribute it and/or modify it under the
terms of the MIT license. A copy of the license can be found in the file
"LICENSE" at the root of this distribution.
-----------------------------------------------------------------------------*/

/* -----------------------------------------------------------
  Demand-sized large-page spans  (#532, supersedes #443)

  Upstream gives every page of a large bin (blocks of ~84-512 KiB) the same 4 MiB span
  (MI_LARGE_PAGE_SIZE). Each thread holds one such page per large bin it uses, so a thread that
  touches all eleven bins holds ~44 MiB of large pages whether it keeps 5 or 50 MiB of blocks in
  them. #529 (E4) measured it: 98-99% of that memory was never formed into blocks, and it was
  resident all the same (2 MiB transparent huge pages fault it in with the first block, and a
  page carved over resident slices keeps the previous tenant's bytes). At a fixed live budget the
  peak grew ~49 MiB per added worker.

  So a new large page is sized from what its bin demands on the theap that asks for it:

  - Accounting, per theap and per large bin, in slow paths only:
      * `_mi_large_span_on_full` (page.c, `mi_page_to_full`): a page of the bin filled up;
      * `_mi_large_span_slices` (arena.c, `_mi_arenas_page_alloc`): a page request, which comes
        only when the bin has no page with a free block left on the theap.
    Nothing is allocated (rule 4) and the malloc/free fast paths are untouched (rule 6): the
    state is a small table at the end of `mi_theap_t`.
  - Policy: a request after a page of the bin filled up is demand beyond the pages the theap has;
    a request without one means the bin's last page emptied and went away. The bin counts them
    against each other (+1 / -1): at MI_LARGE_SPAN_GROW_REQUESTS the span steps up
    (x 2^MI_LARGE_SPAN_GROW_SHIFT, up to MI_LARGE_PAGE_SIZE), at -MI_LARGE_SPAN_DECAY_REQUESTS it
    steps back down. A bin's first page is compact (MI_LARGE_SPAN_COMPACT_SLICES). A bin a theap
    keeps filling reaches and keeps the full span; a bin with a few live blocks stays compact, and
    one extra block now and then (a compact page of the top bins holds two) does not grow it.
  - Deterministic per theap: no thread count, no clock. (#443 chose the span from the number of
    live threads at the time the page was created; the owner rejected that.)

  A consequence every reader of a large page must respect: the pages of one bin no longer all have
  the same span, so a page found by bin does not imply its size. Everything reads the span from the
  page itself (`page->memid`, `mi_page_arena_pages`); `mi_arenas_page_try_find_abandoned` used to
  validate a reclaimed page over the size a fresh page of the caller would get, and now reads the
  page's own (the out-of-range read #443 found, test/test-large-span.c case c).
----------------------------------------------------------- */

#include "mimalloc.h"
#include "mimalloc/internal.h"

#if MI_LARGE_SPAN

// the table index of a large block size, or MI_LARGE_SPAN_BINS if it is not a large one
static size_t mi_large_span_index(size_t block_size) {
  if (block_size <= MI_MEDIUM_MAX_OBJ_SIZE || block_size > MI_LARGE_MAX_OBJ_SIZE) return MI_LARGE_SPAN_BINS;
  const size_t bin_lo = _mi_bin(MI_MEDIUM_MAX_OBJ_SIZE + 1);
  const size_t bin = _mi_bin(block_size);
  mi_assert_internal(bin >= bin_lo);
  const size_t idx = bin - bin_lo;
  mi_assert_internal(idx < MI_LARGE_SPAN_BINS);   // MI_LARGE_SPAN_BINS must cover every large bin
  return (idx < MI_LARGE_SPAN_BINS ? idx : MI_LARGE_SPAN_BINS);
}

#if MI_LARGE_SPAN_GROW_REQUESTS < 1 || MI_LARGE_SPAN_GROW_REQUESTS > 7 || MI_LARGE_SPAN_DECAY_REQUESTS < 1 || MI_LARGE_SPAN_DECAY_REQUESTS > 8
#error "MI_LARGE_SPAN_GROW_REQUESTS must be 1..7 and MI_LARGE_SPAN_DECAY_REQUESTS 1..8 (a 4-bit signed count, see mi_large_span_bin_t)"
#endif
#if MI_LARGE_SPAN_GROW_SHIFT < 1 || MI_LARGE_SPAN_COMPACT_SLICES < 1
#error "MI_LARGE_SPAN_GROW_SHIFT and MI_LARGE_SPAN_COMPACT_SLICES must be at least 1"
#endif

// the fields of the one-byte state of a bin (see mi_large_span_bin_t in types.h); all zero is a
// bin that has seen nothing: compact, no pressure
#define MI_LARGE_SPAN_LEVEL_MASK      (0x07)
#define MI_LARGE_SPAN_FULL_BIT        (0x08)
#define MI_LARGE_SPAN_PRESSURE_SHIFT  (4)

static size_t mi_large_span_level(mi_large_span_bin_t b) { return (b & MI_LARGE_SPAN_LEVEL_MASK); }
static long mi_large_span_pressure(mi_large_span_bin_t b) {
  const long p = (long)(b >> MI_LARGE_SPAN_PRESSURE_SHIFT);   // 0..15
  return (p >= 8 ? p - 16 : p);                               // -8..7
}
static mi_large_span_bin_t mi_large_span_pack(size_t level, long pressure) {
  mi_assert_internal(level <= MI_LARGE_SPAN_LEVEL_MASK && pressure >= -8 && pressure <= 7);
  return (mi_large_span_bin_t)((((unsigned long)pressure & 0x0F) << MI_LARGE_SPAN_PRESSURE_SHIFT) | level);   // (the full bit clear)
}

// the span of `level`, uncapped (the shift is bounded: a level only grows while below the full span)
static size_t mi_large_span_level_slices(size_t level) {
  return ((size_t)MI_LARGE_SPAN_COMPACT_SLICES << (level * MI_LARGE_SPAN_GROW_SHIFT));
}

void _mi_large_span_on_full(mi_theap_t* theap, const mi_page_t* page) {
  if (theap == NULL) return;
  const size_t idx = mi_large_span_index(mi_page_block_size(page));
  if (idx >= MI_LARGE_SPAN_BINS) return;
  theap->large_span[idx] |= MI_LARGE_SPAN_FULL_BIT;
}

// A page request of a large bin on `theap`: account it, and return the span (in slices) of the
// page to create for it if no abandoned page of the bin is reclaimed instead. `overhead` is what a
// page of `block_size` spends besides its blocks in the worst case (meta in front, guard page).
size_t _mi_large_span_slices(mi_theap_t* theap, size_t block_size, size_t overhead) {
  const size_t full = mi_slice_count_of_size(MI_LARGE_PAGE_SIZE);
  if (theap == NULL || MI_LARGE_SPAN_COMPACT_SLICES >= full) return full;
  if (!mi_option_is_enabled(mi_option_large_span)) return full;
  const size_t idx = mi_large_span_index(block_size);
  if (idx >= MI_LARGE_SPAN_BINS) return full;

  const mi_large_span_bin_t b = theap->large_span[idx];
  size_t level = mi_large_span_level(b);
  long pressure = mi_large_span_pressure(b);
  if ((b & MI_LARGE_SPAN_FULL_BIT) != 0) {
    // a page of the bin filled up since its last request: demand beyond what the theap holds
    pressure++;
    if (pressure >= MI_LARGE_SPAN_GROW_REQUESTS) {
      if (mi_large_span_level_slices(level) < full && level < MI_LARGE_SPAN_LEVEL_MASK) { level++; }
      pressure = 0;
    }
  }
  else if (level > 0) {
    // the bin's last page emptied and went away without filling up
    pressure--;
    if (pressure <= -MI_LARGE_SPAN_DECAY_REQUESTS) {
      level--;
      pressure = 0;
    }
  }
  else if (pressure > 0) {
    pressure--;   // (at the compact span only a pending step up can fade)
  }
  theap->large_span[idx] = mi_large_span_pack(level, pressure);

  size_t slices = mi_large_span_level_slices(level);
  const size_t min_slices = mi_slice_count_of_size(MI_LARGE_SPAN_MIN_BLOCKS * block_size + overhead);
  if (slices < min_slices) { slices = min_slices; }
  if (slices > full) { slices = full; }
  return slices;
}

#else // !MI_LARGE_SPAN: every large page gets MI_LARGE_PAGE_SIZE (and the hook sites compile out)

size_t _mi_large_span_slices(mi_theap_t* theap, size_t block_size, size_t overhead) {
  MI_UNUSED(theap); MI_UNUSED(block_size); MI_UNUSED(overhead);
  return mi_slice_count_of_size(MI_LARGE_PAGE_SIZE);
}

void _mi_large_span_on_full(mi_theap_t* theap, const mi_page_t* page) {
  MI_UNUSED(theap); MI_UNUSED(page);
}

#endif // MI_LARGE_SPAN
