/* #519 (the rest of #517 item 2): the arena layout walk behind `mi_purge_holes_report`.

   #514 was located by hand with a layout walk: every arena slice classified as in use / free and
   queued for purge / free and not queued, which showed the thread churn leaving 54 MB of queued
   runs where 32 MB used to be, because small claims spilled into fresh chunks. #519 makes that
   walk permanent: `_mi_arena_layout_walk` classifies every data slice by the arena bitmaps and
   summarises the runs of each kind per chunk size class, and `_mi_purge_holes_report_collect`
   fills it into `mi_holes_report_t.arena_layout`.

   The test is deterministic: it reserves an exclusive arena that nothing else allocates from,
   claims three ranges of TEST_RANGE_SLICES slices directly with `_mi_arenas_alloc`, frees the
   middle one with a purge delay far longer than the test, and asserts the exact slice counts,
   runs and histogram bucket of each kind; then forces the purge and checks the queued run left
   the queue. It asserts on the bitmaps' classification, never on RSS.

   The walk exists only with MI_DIAGNOSTICS=1. Without it, the test checks the stub instead: the
   walk returns false and zeroes its output, and the report's `arena_layout` is all zero. ctest
   sets MIMALLOC_SCAVENGER=0 (no background purge) and MIMALLOC_PURGE_ZEROES=0 (a purge that
   zeroes a range clears its dirty bits, which would turn FREE_DIRTY into FRESH). */

#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <mimalloc.h>
#include "mimalloc/internal.h"   // _mi_arena_layout_walk, mi_holes_report_t (#519)

#define TEST_ARENA_SIZE      (64 * MI_MiB)   // two chunks of 512 slices on 64-bit
#define TEST_RANGE_SLICES    (4)             // lands in a MI_CBIN_OTHER chunk
#define TEST_RANGES          (3)             // claim three, free the middle one
#define TEST_PURGE_DELAY_MS  (600000)        // nothing queued may expire while the test runs
#define TEST_POISON          (0xA5)

static void test_bucket(void) {
  assert(_mi_arena_layout_bucket(1) == 0);
  assert(_mi_arena_layout_bucket(2) == 1);
  assert(_mi_arena_layout_bucket(3) == 1);
  assert(_mi_arena_layout_bucket(TEST_RANGE_SLICES) == 2);
  assert(_mi_arena_layout_bucket(7) == 2);
  assert(_mi_arena_layout_bucket(MI_BCHUNK_BITS - 1) == MI_ARENA_LAYOUT_RUN_BUCKETS - 2);
  assert(_mi_arena_layout_bucket(MI_BCHUNK_BITS) == MI_ARENA_LAYOUT_RUN_BUCKETS - 1);
  puts("bucket: power-of-two run-length buckets");
}

static bool is_all_zero(const void* p, size_t size) {
  const unsigned char* b = (const unsigned char*)p;
  for (size_t i = 0; i < size; i++) { if (b[i] != 0) return false; }
  return true;
}

#if MI_DIAGNOSTICS
static size_t total_of(const mi_arena_layout_t* L, mi_arena_layout_kind_t kind) {
  size_t n = 0;
  for (size_t c = 0; c < MI_CBIN_COUNT; c++) { n += L->cls[c].slices[kind]; }
  return n;
}

static size_t runs_of(const mi_arena_layout_t* L, mi_arena_layout_kind_t kind) {
  size_t n = 0;
  for (size_t c = 0; c < MI_CBIN_COUNT; c++) { n += L->cls[c].runs[kind]; }
  return n;
}

static size_t hist_of(const mi_arena_layout_t* L, mi_arena_layout_kind_t kind, size_t bucket) {
  size_t n = 0;
  for (size_t c = 0; c < MI_CBIN_COUNT; c++) { n += L->cls[c].run_hist[kind][bucket]; }
  return n;
}

// Every slice is classified exactly once, and every run is counted in exactly one bucket.
static void check_consistent(const mi_arena_layout_t* L, size_t data_slices) {
  size_t slices = 0, chunks = 0;
  for (size_t c = 0; c < MI_CBIN_COUNT; c++) {
    const mi_arena_layout_class_t* const k = &L->cls[c];
    chunks += k->chunks;
    for (size_t kind = 0; kind < MI_ARENA_LAYOUT_KIND_COUNT; kind++) {
      slices += k->slices[kind];
      assert(k->committed_slices[kind] <= k->slices[kind]);
      assert(k->max_run[kind] <= k->slices[kind]);
      assert(k->max_run[kind] <= MI_BCHUNK_BITS);   // runs never cross a chunk
      assert((k->runs[kind] == 0) == (k->slices[kind] == 0));
      size_t hist = 0;
      for (size_t b = 0; b < MI_ARENA_LAYOUT_RUN_BUCKETS; b++) { hist += k->run_hist[kind][b]; }
      assert(hist == k->runs[kind]);
    }
  }
  assert(chunks == L->chunks);
  assert(slices == data_slices);
}

static void test_walk(void) {
  mi_arena_id_t id;
  assert(mi_reserve_os_memory_ex(TEST_ARENA_SIZE, false /* commit */, false /* allow_large */, true /* exclusive */, &id) == 0);
  mi_arena_t* const arena = _mi_arena_from_id(id);
  assert(arena != NULL);
  const size_t data_slices = arena->slice_count - arena->info_slices;
  const size_t R = TEST_RANGE_SLICES;

  mi_arena_layout_t L;
  memset(&L, TEST_POISON, sizeof(L));
  assert(!_mi_arena_layout_walk(NULL, NULL, &L) && is_all_zero(&L, sizeof(L)));   // no subproc: nothing walked, zeroed

  memset(&L, TEST_POISON, sizeof(L));
  assert(_mi_arena_layout_walk(arena->subproc, arena, &L));
  assert(L.arenas == 1 && L.meta_slices == arena->info_slices);
  check_consistent(&L, data_slices);
  assert(total_of(&L, MI_ARENA_LAYOUT_FRESH) == data_slices);   // an untouched arena is all fresh
  assert(total_of(&L, MI_ARENA_LAYOUT_IN_USE) == 0);

  // claim three ranges straight from the arena bitmaps, free the middle one
  void* p[TEST_RANGES];
  mi_memid_t memid[TEST_RANGES];
  for (size_t i = 0; i < TEST_RANGES; i++) {
    p[i] = _mi_arenas_alloc(mi_heap_main(), R * MI_ARENA_SLICE_SIZE, true /* commit */, false, arena, 0, -1, &memid[i]);
    assert(p[i] != NULL && memid[i].memkind == MI_MEM_ARENA && memid[i].mem.arena.arena == arena);
  }
  _mi_arenas_free(arena->subproc, p[1], R * MI_ARENA_SLICE_SIZE, memid[1]);

  memset(&L, TEST_POISON, sizeof(L));
  assert(_mi_arena_layout_walk(arena->subproc, arena, &L));
  check_consistent(&L, data_slices);
  const size_t queued = total_of(&L, MI_ARENA_LAYOUT_QUEUED) + total_of(&L, MI_ARENA_LAYOUT_QUEUED_AGED);
  const size_t queued_runs = runs_of(&L, MI_ARENA_LAYOUT_QUEUED) + runs_of(&L, MI_ARENA_LAYOUT_QUEUED_AGED);
  const size_t bucket = _mi_arena_layout_bucket(R);
  fprintf(stderr, "after free: in_use=%zu fresh=%zu free_dirty=%zu queued=%zu (%zu runs) of %zu data slices\n",
          total_of(&L, MI_ARENA_LAYOUT_IN_USE), total_of(&L, MI_ARENA_LAYOUT_FRESH),
          total_of(&L, MI_ARENA_LAYOUT_FREE_DIRTY), queued, queued_runs, data_slices);
  assert(total_of(&L, MI_ARENA_LAYOUT_IN_USE) == (TEST_RANGES - 1) * R);
  assert(queued == R && queued_runs == 1);                       // exactly the freed range, as one run
  assert(hist_of(&L, MI_ARENA_LAYOUT_QUEUED, bucket) + hist_of(&L, MI_ARENA_LAYOUT_QUEUED_AGED, bucket) == 1);
  assert(total_of(&L, MI_ARENA_LAYOUT_FREE_DIRTY) == 0);
  assert(total_of(&L, MI_ARENA_LAYOUT_FRESH) == data_slices - TEST_RANGES * R);
  // The three claims share one chunk, so one size class holds all of them and the freed run.
  // (Which class is the bbitmap's business: the chunk holding the arena's info slices has no free
  // slice 0, so it is never binned and stays "none"; a chunk whose first claim is at slice 0
  // takes that claim's class.)
  size_t cls_used = MI_CBIN_COUNT;
  for (size_t c = 0; c < MI_CBIN_COUNT; c++) {
    if (L.cls[c].slices[MI_ARENA_LAYOUT_IN_USE] == 0) continue;
    assert(cls_used == MI_CBIN_COUNT);   // only one class holds in-use slices
    cls_used = c;
  }
  assert(cls_used < MI_CBIN_COUNT);
  const mi_arena_layout_class_t* const k = &L.cls[cls_used];
  assert(k->chunks >= 1);
  assert(k->slices[MI_ARENA_LAYOUT_IN_USE] == (TEST_RANGES - 1) * R);
  assert(k->slices[MI_ARENA_LAYOUT_QUEUED] + k->slices[MI_ARENA_LAYOUT_QUEUED_AGED] == R);
  assert(k->max_run[MI_ARENA_LAYOUT_QUEUED] + k->max_run[MI_ARENA_LAYOUT_QUEUED_AGED] == R);
  assert(k->committed_slices[MI_ARENA_LAYOUT_IN_USE] == (TEST_RANGES - 1) * R);   // claimed with commit

  // the report carries the same walk (over every arena, so at least ours)
  mi_holes_report_t rep;
  _mi_purge_holes_report_collect(&rep);
  assert(rep.arena_layout.arenas >= 1);
  assert(total_of(&rep.arena_layout, MI_ARENA_LAYOUT_IN_USE) >= (TEST_RANGES - 1) * R);
  mi_purge_holes_report();   // and prints it

  // a forced purge takes the run off the queue; it stays dirty (MIMALLOC_PURGE_ZEROES=0) or turns fresh
  _mi_arenas_try_purge(true /* force */, true /* visit_all */, arena->subproc, 0);
  memset(&L, TEST_POISON, sizeof(L));
  assert(_mi_arena_layout_walk(arena->subproc, arena, &L));
  check_consistent(&L, data_slices);
  fprintf(stderr, "after purge: in_use=%zu fresh=%zu free_dirty=%zu queued=%zu\n",
          total_of(&L, MI_ARENA_LAYOUT_IN_USE), total_of(&L, MI_ARENA_LAYOUT_FRESH),
          total_of(&L, MI_ARENA_LAYOUT_FREE_DIRTY),
          total_of(&L, MI_ARENA_LAYOUT_QUEUED) + total_of(&L, MI_ARENA_LAYOUT_QUEUED_AGED));
  assert(total_of(&L, MI_ARENA_LAYOUT_QUEUED) + total_of(&L, MI_ARENA_LAYOUT_QUEUED_AGED) == 0);
  assert(total_of(&L, MI_ARENA_LAYOUT_FREE_DIRTY) + total_of(&L, MI_ARENA_LAYOUT_FRESH) == data_slices - (TEST_RANGES - 1) * R);

  _mi_arenas_free(arena->subproc, p[0], R * MI_ARENA_SLICE_SIZE, memid[0]);
  _mi_arenas_free(arena->subproc, p[2], R * MI_ARENA_SLICE_SIZE, memid[2]);
  puts("walk: in-use / fresh / queued / purged runs of an exclusive arena, by chunk size class");
}

#else
// MI_DIAGNOSTICS=0: the walk is a stub that zeroes its output, and so is the report's copy.
static void test_stub(void) {
  mi_arena_layout_t L;
  memset(&L, TEST_POISON, sizeof(L));
  assert(!_mi_arena_layout_walk(_mi_subproc(), NULL, &L));
  assert(is_all_zero(&L, sizeof(L)));
  void* keep = mi_malloc(1024);   // the report walks this thread's heaps: make sure there is one
  assert(keep != NULL);
  mi_holes_report_t rep;
  memset(&rep, TEST_POISON, sizeof(rep));
  _mi_purge_holes_report_collect(&rep);
  assert(is_all_zero(&rep.arena_layout, sizeof(rep.arena_layout)));
  mi_purge_holes_report();   // prints no layout section
  mi_free(keep);
  puts("stub: MI_DIAGNOSTICS=0 zeroes the layout");
}
#endif

int main(void) {
  mi_option_set(mi_option_purge_delay, TEST_PURGE_DELAY_MS);
  test_bucket();
  #if MI_DIAGNOSTICS
  test_walk();
  #else
  test_stub();
  #endif
  return 0;
}
