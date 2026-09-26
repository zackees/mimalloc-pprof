/* #532: demand-sized large-page spans (src/large-span.c).

   A large page (blocks of ~84-512 KiB) used to get MI_LARGE_PAGE_SIZE (4 MiB) whatever its size
   class ("bin") held, so a thread with a couple of live blocks in a bin kept a 4 MiB page for them,
   almost all of it never formed (#529, E4). Now the span comes from the bin's demand on the theap:

   (a) a theap with few live blocks in a large bin gets a compact page, with a small unformed tail;
   (b) a theap whose demand in the bin exceeds the compact page grows the span geometrically to the
       full MI_LARGE_PAGE_SIZE -- and it decays back once the bin's pages stop filling up;
   (c) a bin now holds pages of different spans, and an abandoned compact page reclaimed by a theap
       whose fresh pages of that bin are full-size is validated over its OWN span. (#443 found the
       out-of-range read: `mi_arenas_page_try_find_abandoned` checked the caller's span, which
       runs past a compact page into slices of no page; an MI_DEBUG_INTERNAL build aborts on it.)
   (d) the opt-out: with `mi_option_large_span` off every large page is 4 MiB again.

   Deterministic: structural checks on the pages the blocks land in (`page->memid`, `reserved`,
   `capacity`), no RSS and no timing. ctest turns the scavenger and the hole sweep off so no
   background pass claims an abandoned page while (c) looks for it. Each case uses its own bin, so
   no case reclaims another's pages, and page reserve (#493) is off so an exiting thread frees its
   empty pages instead of leaving them in the bins' abandoned maps. */

#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <mimalloc.h>
#include "mimalloc/internal.h"   // _mi_ptr_page, mi_page_t, the span constants

#ifndef MI_LARGE_SPAN_COMPACT_SLICES   // (so the test also builds against a tree without #532: RED)
#define MI_LARGE_SPAN_COMPACT_SLICES  (16)
#endif
#ifndef MI_LARGE_SPAN_DECAY_REQUESTS
#define MI_LARGE_SPAN_DECAY_REQUESTS  (4)
#endif

// one bin per case (distinct 12.5% size classes, also with debug padding on top)
#define SIZE_A      (128 * 1024)
#define SIZE_B      (200 * 1024)
#define SIZE_C      (300 * 1024)
#define SIZE_DECAY  (100 * 1024)
#define SIZE_OFF    (400 * 1024)
#define MAX_BLOCKS  (512)

/* ---- portable threading (from test/test-memory-gate.c) ------------------- */

#ifdef _WIN32
#include <windows.h>
typedef HANDLE thread_t;
typedef DWORD (WINAPI *thread_fun_t)(void*);
#define THREAD_RET DWORD WINAPI
#define THREAD_OK  0
static void thread_start(thread_t* t, thread_fun_t fn, void* arg) {
  *t = CreateThread(NULL, 0, fn, arg, 0, NULL);
  assert(*t != NULL);
}
static void thread_join(thread_t t) {
  assert(WaitForSingleObject(t, INFINITE) == WAIT_OBJECT_0);
  CloseHandle(t);
}
#else
#include <pthread.h>
#include <time.h>
typedef pthread_t thread_t;
typedef void* (*thread_fun_t)(void*);
#define THREAD_RET void*
#define THREAD_OK  NULL
static void thread_start(thread_t* t, thread_fun_t fn, void* arg) {
  assert(pthread_create(t, NULL, fn, arg) == 0);
}
static void thread_join(thread_t t) { assert(pthread_join(t, NULL) == 0); }
#endif

/* ---- helpers ------------------------------------------------------------- */

static int failures = 0;
#define CHECK(cond, ...) do { if (!(cond)) { fprintf(stderr, "FAILED: " __VA_ARGS__); fprintf(stderr, "\n"); failures++; } } while (0)

static size_t full_slices(void) { return mi_slice_count_of_size(MI_LARGE_PAGE_SIZE); }

// the span of the page `p` lives in, in slices (0 if it is not an arena page)
static size_t span_of(const void* p) {
  const mi_page_t* const page = _mi_ptr_page(p);
  return (page->memid.memkind == MI_MEM_ARENA ? page->memid.mem.arena.slice_count : 0);
}

// the smallest span that holds two blocks of the page `p` lives in (what src/large-span.c allows)
static size_t compact_expected(const void* p) {
  const size_t bsize = mi_page_block_size(_mi_ptr_page(p));
  size_t slices = MI_LARGE_SPAN_COMPACT_SLICES;
  const size_t two = mi_slice_count_of_size(2 * bsize);
  if (slices < two) { slices = two; }   // (+ at most one slice of worst-case overhead, see span_is_compact)
  return slices;
}

static int span_is_compact(const void* p) {
  const size_t span = span_of(p);
  const size_t expect = compact_expected(p);
  return (span >= expect && span <= expect + 1 && span < full_slices());
}

// allocate `size` blocks until the latest lands in a page of the full span that is now full;
// returns the count (the blocks are kept in `blocks`), and the distinct spans in page order
static size_t grow_to_full(void** blocks, size_t size, size_t* spans, size_t* nspans) {
  size_t n = 0;
  const mi_page_t* last = NULL;
  *nspans = 0;
  while (n < MAX_BLOCKS) {
    void* const p = mi_malloc(size);
    assert(p != NULL);
    memset(p, 0x5a, 64);
    blocks[n++] = p;
    const mi_page_t* const page = _mi_ptr_page(p);
    if (page != last) { last = page; if (*nspans < 16) { spans[(*nspans)++] = span_of(p); } }
    if (span_of(p) == full_slices() && page->used == page->reserved) break;
  }
  return n;
}

static void free_all(void** blocks, size_t n) {
  for (size_t i = 0; i < n; i++) { mi_free(blocks[i]); blocks[i] = NULL; }
}

/* ---- (a) few live blocks: a compact page, a small unformed tail ------------ */

static void case_a(void) {
  void* const x = mi_malloc(SIZE_A);
  void* const y = mi_malloc(SIZE_A);
  assert(x != NULL && y != NULL);
  const mi_page_t* const page = _mi_ptr_page(x);
  CHECK(_mi_ptr_page(y) == page, "(a) two blocks of one bin should share a page");
  const size_t bsize = mi_page_block_size(page);
  const size_t span = span_of(x);
  const size_t unformed = mi_size_of_slices(span) - (size_t)page->capacity * bsize;
  fprintf(stderr, "(a) %zu B blocks: page span %zu slices (%zu KiB), %u formed of %u, unformed tail %zu KiB\n",
          bsize, span, mi_size_of_slices(span) / 1024, (unsigned)page->capacity, (unsigned)page->reserved, unformed / 1024);
  CHECK(span_is_compact(x), "(a) the first page of a lightly used large bin should be compact (%zu slices, full span %zu)", span, full_slices());
  CHECK(unformed <= mi_size_of_slices(MI_LARGE_SPAN_COMPACT_SLICES), "(a) unformed tail of %zu KiB exceeds a compact span", unformed / 1024);
  mi_free(x);
  mi_free(y);
}

/* ---- (b) demand beyond the compact page: the span grows to full ------------ */

static void case_b(void) {
  void* blocks[MAX_BLOCKS];
  size_t spans[16];
  size_t nspans = 0;
  const size_t n = grow_to_full(blocks, SIZE_B, spans, &nspans);
  fprintf(stderr, "(b) %zu blocks, page spans:", n);
  for (size_t i = 0; i < nspans; i++) { fprintf(stderr, " %zu", spans[i]); }
  fprintf(stderr, "\n");
  CHECK(nspans >= 2, "(b) expected several pages");
  CHECK(nspans >= 1 && spans[0] < full_slices(), "(b) the bin's first page should be compact (got %zu)", nspans > 0 ? spans[0] : 0);
  for (size_t i = 1; i < nspans; i++) {
    CHECK(spans[i] >= spans[i-1], "(b) the span shrank while the bin kept filling its pages (%zu -> %zu)", spans[i-1], spans[i]);
  }
  CHECK(n < MAX_BLOCKS && nspans >= 1 && spans[nspans-1] == full_slices(), "(b) a bin that keeps filling its pages should reach the full span");
  free_all(blocks, n);
}

/* ---- (b') ... and decays once the bin's pages stop filling up ------------- */

static void case_decay(void) {
  #if MI_LARGE_SPAN
  void* blocks[MAX_BLOCKS];
  size_t spans[16];
  size_t nspans = 0;
  const size_t n = grow_to_full(blocks, SIZE_DECAY, spans, &nspans);
  CHECK(nspans >= 1 && spans[nspans-1] == full_slices(), "(decay) setup: the bin did not reach the full span");
  free_all(blocks, n);
  mi_collect(true);   // the emptied pages go back to the arena: the next allocation is a page request
  // every round: one block, freed, its page freed -- a page request whose previous page never filled
  size_t last_span = full_slices();
  const size_t rounds = 4 * MI_LARGE_SPAN_DECAY_REQUESTS;
  size_t fresh_compact = 0;
  for (size_t i = 0; i < rounds; i++) {
    void* const p = mi_malloc(SIZE_DECAY);
    assert(p != NULL);
    last_span = span_of(p);
    if (span_is_compact(p)) fresh_compact++;
    mi_free(p);
    mi_collect(true);
  }
  fprintf(stderr, "(decay) after %zu quiet page requests: last page span %zu slices (%zu compact pages)\n", rounds, last_span, fresh_compact);
  CHECK(fresh_compact > 0 && last_span < full_slices(), "(decay) the span should step back down once the bin's pages stop filling up");
  #endif
}

/* ---- (c) a mixed-span bin: reclaim an abandoned compact page -------------- */

// written by one thread before it publishes a stage, read by the other after it sees the stage
typedef struct shared_s {
  void*            t1_block;   // the block t1 leaves behind in its compact page
  const mi_page_t* t1_page;
  size_t           t1_span;
} shared_t;

static shared_t shared;
static _Atomic(uintptr_t) stage;   // 0: the grower fills; 1: it waits for t1; 2: t1 is gone

static void wait_stage(uintptr_t s) {
  while (mi_atomic_load_acquire(&stage) < s) {
    #ifdef _WIN32
    Sleep(1);
    #else
    struct timespec ts = { 0, 1000000 };
    nanosleep(&ts, NULL);
    #endif
  }
}
static void set_stage(uintptr_t s) { mi_atomic_store_release(&stage, s); }

// t1: a fresh theap takes one block of the bin -- a compact page -- and exits holding it, so the
// page is abandoned into the heap's map for that bin
static THREAD_RET t1_main(void* arg) {
  (void)arg;
  void* const p = mi_malloc(SIZE_C);
  assert(p != NULL);
  memset(p, 0x11, 64);
  shared.t1_block = p;
  shared.t1_page = _mi_ptr_page(p);
  shared.t1_span = span_of(p);
  return THREAD_OK;
}

// the grower: fills the bin until its fresh pages are full-size, then (after t1 abandoned its
// compact page) needs one more page -- and reclaims t1's
static THREAD_RET grower_main(void* arg) {
  (void)arg;
  void* blocks[MAX_BLOCKS];
  size_t spans[16];
  size_t nspans = 0;
  const size_t n = grow_to_full(blocks, SIZE_C, spans, &nspans);
  CHECK(nspans >= 1 && spans[nspans-1] == full_slices(), "(c) setup: the grower did not reach the full span");
  set_stage(1);
  wait_stage(2);
  // the grower's current page is full: this is a page request at the full span, and the only
  // abandoned page of the bin with a free block is t1's compact one
  void* const p = mi_malloc(SIZE_C);
  assert(p != NULL);
  memset(p, 0x22, 64);
  const mi_page_t* const page = _mi_ptr_page(p);
  fprintf(stderr, "(c) grower (fresh span %zu) reclaimed a page of span %zu (t1's: %s)\n",
          full_slices(), span_of(p), page == shared.t1_page ? "yes" : "no");
  CHECK(page == shared.t1_page, "(c) the grower should have reclaimed t1's abandoned compact page");
  CHECK(span_of(p) == shared.t1_span, "(c) a reclaimed page keeps its own span");
  // the page is the grower's now: it can take t1's block back and the rest of its blocks
  mi_free(shared.t1_block);
  mi_free(p);
  free_all(blocks, n);
  return THREAD_OK;
}

static void case_c(void) {
  memset(&shared, 0, sizeof(shared));
  set_stage(0);
  thread_t grower, t1;
  thread_start(&grower, &grower_main, NULL);
  wait_stage(1);
  thread_start(&t1, &t1_main, NULL);
  thread_join(t1);
  fprintf(stderr, "(c) t1 left a page of span %zu slices\n", shared.t1_span);
  CHECK(shared.t1_span < full_slices(), "(c) setup: t1's page should be compact (span %zu)", shared.t1_span);
  set_stage(2);
  thread_join(grower);
}

/* ---- (d) the opt-out ----------------------------------------------------- */

static void case_off(void) {
  #if MI_LARGE_SPAN
  const long saved = mi_option_get(mi_option_large_span);
  mi_option_set(mi_option_large_span, 0);
  void* const p = mi_malloc(SIZE_OFF);
  assert(p != NULL);
  fprintf(stderr, "(d) option off: page span %zu slices\n", span_of(p));
  CHECK(span_of(p) == full_slices(), "(d) with mi_option_large_span off a large page should have the full span");
  mi_free(p);
  mi_option_set(mi_option_large_span, saved);
  #endif
}

int main(void) {
  #if !MI_ENABLE_LARGE_PAGES
  fprintf(stderr, "skipped: no large pages in this build\n");
  return 0;
  #else
  #if defined(MI_GUARDED) && MI_GUARDED
  if (mi_option_get(mi_option_guarded_sample_rate) != 0) {   // guarded blocks do not land in regular pages
    fprintf(stderr, "skipped: guarded sampling is on\n");
    return 0;
  }
  #endif
  #if defined(MI_LARGE_SPAN) && !MI_LARGE_SPAN
  // compiled out: every large page has the full span
  void* const p = mi_malloc(SIZE_A);
  CHECK(span_of(p) == full_slices(), "MI_LARGE_SPAN=0: a large page should have the full span");
  mi_free(p);
  #else
  mi_option_set(mi_option_page_reserve, 0);   // an exiting thread frees its empty pages (see the header)
  case_a();
  case_b();
  case_decay();
  case_c();
  case_off();
  #endif
  if (failures > 0) { fprintf(stderr, "%d check(s) failed\n", failures); return 1; }
  fprintf(stderr, "ok\n");
  return 0;
  #endif
}
