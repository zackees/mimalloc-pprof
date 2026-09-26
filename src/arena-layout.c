/* The arena layout walk (#519, the rest of #517 item 2), part of `mi_purge_holes_report`.

   WHY

   #514's memory-gate regression (58 -> 62 MB peak RSS) was located by hand: a walk over every
   arena slice, classified by the `slices_free` / `slices_purge` / `slices_purge_aged` bitmaps,
   showed the thread churn ending with 54 MB of free-and-queued runs instead of 32 MB, because
   resident-first claims of small and medium pages took other size classes' queued runs and
   those classes spilled into fresh chunks. This file makes that walk permanent, so the next
   such regression is attributed to chunk fragmentation from the report instead of a bisect.

   WHAT

   Every DATA slice of an arena (the `info_slices` in front are the arena's own metadata and are
   only counted) is classified into exactly one `mi_arena_layout_kind_t`:

     IN_USE       not in `slices_free`: a page (or a direct arena allocation) owns it
     FRESH        free and not dirty: never handed out (or purged to zero), costs nothing
     FREE_DIRTY   free, dirty and not queued: purged earlier by a reset (the dirty bit survives
                  it, so this is an UPPER bound on residency) or in an arena that never purges
     QUEUED       free and in `slices_purge` (or `slices_purge_short`, #506): waiting for the purge
                  delay, certainly resident
     QUEUED_AGED  free and in `slices_purge_aged` (or `slices_purge_short_aged`): queued since the
                  previous purge deadline of its queue

   and, orthogonally, counted as committed when its `slices_committed` bit is set (address
   space, not residency, on POSIX: see `mi_holes_report_t`). Then, walking each chunk
   (MI_BCHUNK_BITS slices) in index order, every maximal run of one kind inside the chunk is
   counted with its length. Everything is summed per chunk SIZE CLASS -- the `mi_chunkbin_t` the
   `slices_free` bbitmap assigned the chunk (small / other / medium / large / huge, or none for
   a chunk nothing claimed yet) -- because keeping the size classes in separate chunks is what
   `mi_bbitmap_try_find_and_clearN` relies on to limit fragmentation, and #514 was exactly one
   class eating another's runs. Run lengths go into power-of-two buckets
   (`_mi_arena_layout_bucket`), so "many short queued runs in `other` chunks next to fresh
   `small` chunks" is one glance at the table.

   HOW (the safety rules)

   Read-only: relaxed loads of the arena bitmaps, the same reads `_mi_arenas_holes_committed`
   makes, so a concurrent claim or free can move a slice between kinds while we look (the counts
   are a snapshot, not an atomic one). It takes no page ownership, no lock, and allocates
   nothing: the output is a fixed-size struct the caller owns (CLAUDE.md rule 4). The arenas
   must stay alive while it runs, which the caller guarantees by being inside the allocator
   (`_mi_purge_holes_report_collect` walks under its owner gate, so the free-arena reclaim of
   `mi_purge_all_ex(MI_PURGE_RECLAIM)` cannot run). It is never called from an allocation path.

   Compiled in only with MI_DIAGNOSTICS=1 (#414: every observability subsystem is opt-in); the
   `#else` stubs zero the output and print nothing, so `mi_holes_report_t` and its callers are
   identical in every configuration. */

#include "mimalloc.h"
#include "mimalloc/internal.h"
#include "bitmap.h"   // mi_bbitmap_is_setN, mi_bitmap_is_set, mi_bbitmap_debug_get_bin

// The `run_hist` bucket of a run of `run_slices` slices: floor(log2(run_slices)), so bucket b
// holds the lengths [2^b, 2^(b+1)). Needs no build flag: it is pure arithmetic.
size_t _mi_arena_layout_bucket(size_t run_slices) {
  size_t b = 0;
  if (!mi_bsr(run_slices, &b)) return 0;   // (a zero-length run is not a run)
  return (b < MI_ARENA_LAYOUT_RUN_BUCKETS ? b : MI_ARENA_LAYOUT_RUN_BUCKETS - 1);
}

#if MI_DIAGNOSTICS

static mi_arena_layout_kind_t mi_arena_layout_kind_at(mi_arena_t* arena, size_t slice_index) {
  if (!mi_bbitmap_is_setN(arena->slices_free, slice_index, 1)) return MI_ARENA_LAYOUT_IN_USE;
  if (mi_bitmap_is_set(arena->slices_purge_aged, slice_index) || mi_bitmap_is_set(arena->slices_purge_short_aged, slice_index)) return MI_ARENA_LAYOUT_QUEUED_AGED;
  if (mi_bitmap_is_set(arena->slices_purge, slice_index) || mi_bitmap_is_set(arena->slices_purge_short, slice_index))      return MI_ARENA_LAYOUT_QUEUED;
  if (mi_bitmap_is_set(arena->slices_dirty, slice_index))      return MI_ARENA_LAYOUT_FREE_DIRTY;
  return MI_ARENA_LAYOUT_FRESH;
}

static void mi_arena_layout_add_run(mi_arena_layout_class_t* cls, mi_arena_layout_kind_t kind, size_t run_slices) {
  if (run_slices == 0) return;
  cls->runs[kind]++;
  cls->run_hist[kind][_mi_arena_layout_bucket(run_slices)]++;
  if (run_slices > cls->max_run[kind]) { cls->max_run[kind] = run_slices; }
}

static void mi_arena_layout_walk_arena(mi_arena_t* arena, mi_arena_layout_t* out) {
  const size_t slice_count = arena->slice_count;
  const size_t info_slices = arena->info_slices;
  out->arenas++;
  out->meta_slices += info_slices;
  const size_t chunk_count = _mi_divide_up(slice_count, MI_BCHUNK_BITS);
  for (size_t chunk_idx = 0; chunk_idx < chunk_count; chunk_idx++) {
    const size_t chunk_start = chunk_idx * MI_BCHUNK_BITS;
    const size_t start = (chunk_start > info_slices ? chunk_start : info_slices);
    const size_t end   = (chunk_start + MI_BCHUNK_BITS < slice_count ? chunk_start + MI_BCHUNK_BITS : slice_count);
    if (start >= end) continue;   // the chunk holds only the arena's own info slices

    const mi_chunkbin_t bin = mi_bbitmap_debug_get_bin(arena->slices_free->chunkmap_bins, chunk_idx);
    mi_assert_internal(bin < MI_CBIN_COUNT);
    mi_arena_layout_class_t* const cls = &out->cls[bin];
    cls->chunks++;
    out->chunks++;

    mi_arena_layout_kind_t run_kind = MI_ARENA_LAYOUT_IN_USE;
    size_t run_slices = 0;
    for (size_t i = start; i < end; i++) {
      const mi_arena_layout_kind_t kind = mi_arena_layout_kind_at(arena, i);
      cls->slices[kind]++;
      if (mi_bitmap_is_set(arena->slices_committed, i)) { cls->committed_slices[kind]++; }
      if (run_slices > 0 && kind == run_kind) {
        run_slices++;
      }
      else {
        mi_arena_layout_add_run(cls, run_kind, run_slices);
        run_kind = kind;
        run_slices = 1;
      }
    }
    mi_arena_layout_add_run(cls, run_kind, run_slices);   // runs never cross a chunk
  }
}

bool _mi_arena_layout_walk(mi_subproc_t* subproc, mi_arena_t* arena, mi_arena_layout_t* out) {
  if (out == NULL) return false;
  _mi_memzero(out, sizeof(*out));
  if (subproc == NULL) return false;
  if (arena != NULL) {
    if (arena->subproc != subproc) return false;
    mi_arena_layout_walk_arena(arena, out);
    return true;
  }
  const size_t arena_count = mi_atomic_load_relaxed(&subproc->arena_count);
  for (size_t i = 0; i < arena_count; i++) {
    mi_arena_t* const a = mi_atomic_load_ptr_acquire(mi_arena_t, &subproc->arenas[i]);
    if (a != NULL) { mi_arena_layout_walk_arena(a, out); }   // a reclaimed slot is NULL
  }
  return true;
}

/* ---- printing (from `_mi_page_holes_report_print`) ----------------------- */

static const char* const mi_arena_layout_class_name[MI_CBIN_COUNT] = { "small", "other", "medium", "large", "huge", "none" };
static const char* const mi_arena_layout_kind_name[MI_ARENA_LAYOUT_KIND_COUNT] = { "in_use", "fresh", "free_dirty", "queued", "queued_aged" };

static void mi_arena_layout_mb(size_t slices, char* buf, size_t bufsize) {
  const size_t bytes = mi_size_of_slices(slices);
  _mi_snprintf(buf, bufsize, "%zu.%02zu", bytes / MI_MiB, ((bytes % MI_MiB) * 100) / MI_MiB);
}

static size_t mi_arena_layout_total(const mi_arena_layout_t* layout, mi_arena_layout_kind_t kind) {
  size_t n = 0;
  for (size_t c = 0; c < MI_CBIN_COUNT; c++) { n += layout->cls[c].slices[kind]; }
  return n;
}

void _mi_arena_layout_print(const mi_arena_layout_t* layout) {
  if (layout == NULL || layout->arenas == 0) return;
  char s[MI_ARENA_LAYOUT_KIND_COUNT][32], smeta[32];
  for (size_t k = 0; k < MI_ARENA_LAYOUT_KIND_COUNT; k++) {
    mi_arena_layout_mb(mi_arena_layout_total(layout, (mi_arena_layout_kind_t)k), s[k], sizeof(s[k]));
  }
  mi_arena_layout_mb(layout->meta_slices, smeta, sizeof(smeta));
  _mi_fprintf(NULL, NULL, "  arena layout (#519): %zu arenas, %zu chunks of %zu slices, %s MB arena meta\n",
              layout->arenas, layout->chunks, (size_t)MI_BCHUNK_BITS, smeta);
  _mi_fprintf(NULL, NULL, "    in use %s MB; free: queued %s MB + aged %s MB (resident), dirty not queued %s MB (upper bound), fresh %s MB\n",
              s[MI_ARENA_LAYOUT_IN_USE], s[MI_ARENA_LAYOUT_QUEUED], s[MI_ARENA_LAYOUT_QUEUED_AGED],
              s[MI_ARENA_LAYOUT_FREE_DIRTY], s[MI_ARENA_LAYOUT_FRESH]);
  _mi_fprintf(NULL, NULL, "    %-7s %7s %10s %10s %10s %10s %10s   (MB per chunk size class)\n",
              "class", "chunks", "in_use", "fresh", "free_dirty", "queued", "aged");
  for (size_t c = 0; c < MI_CBIN_COUNT; c++) {
    const mi_arena_layout_class_t* const cls = &layout->cls[c];
    if (cls->chunks == 0) continue;
    for (size_t k = 0; k < MI_ARENA_LAYOUT_KIND_COUNT; k++) { mi_arena_layout_mb(cls->slices[k], s[k], sizeof(s[k])); }
    _mi_fprintf(NULL, NULL, "    %-7s %7zu %10s %10s %10s %10s %10s\n", mi_arena_layout_class_name[c], cls->chunks,
                s[MI_ARENA_LAYOUT_IN_USE], s[MI_ARENA_LAYOUT_FRESH], s[MI_ARENA_LAYOUT_FREE_DIRTY],
                s[MI_ARENA_LAYOUT_QUEUED], s[MI_ARENA_LAYOUT_QUEUED_AGED]);
  }
  _mi_fprintf(NULL, NULL, "    runs per class and kind (a run never crosses a chunk): count, longest, then length:count by power of two\n");
  for (size_t c = 0; c < MI_CBIN_COUNT; c++) {
    const mi_arena_layout_class_t* const cls = &layout->cls[c];
    for (size_t k = 0; k < MI_ARENA_LAYOUT_KIND_COUNT; k++) {
      if (cls->runs[k] == 0) continue;
      char hist[MI_ARENA_LAYOUT_RUN_BUCKETS * 24];
      size_t len = 0;
      hist[0] = 0;
      for (size_t b = 0; b < MI_ARENA_LAYOUT_RUN_BUCKETS && len + 1 < sizeof(hist); b++) {
        if (cls->run_hist[k][b] == 0) continue;
        _mi_snprintf(hist + len, sizeof(hist) - len, " %zu:%zu", (size_t)1 << b, cls->run_hist[k][b]);
        len += _mi_strlen(hist + len);
      }
      _mi_fprintf(NULL, NULL, "    %-7s %-11s runs %6zu  longest %4zu |%s\n", mi_arena_layout_class_name[c],
                  mi_arena_layout_kind_name[k], cls->runs[k], cls->max_run[k], hist);
    }
  }
}

#else  // !MI_DIAGNOSTICS: the stubs keep `mi_holes_report_t` and its callers identical

bool _mi_arena_layout_walk(mi_subproc_t* subproc, mi_arena_t* arena, mi_arena_layout_t* out) {
  MI_UNUSED(subproc); MI_UNUSED(arena);
  if (out != NULL) { _mi_memzero(out, sizeof(*out)); }
  return false;
}

void _mi_arena_layout_print(const mi_arena_layout_t* layout) {
  MI_UNUSED(layout);
}

#endif
