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
   (e) one overflow is not demand: a bin that once holds one block more than its compact page
       gets a second compact page, not a larger one (the growth hysteresis,
       MI_LARGE_SPAN_GROW_REQUESTS).
   (f) a live set that exactly fills a compact page stays in it (#544): filling the page abandons
       it, and at one block short of full it is still "mostly used", so before #544 neither the
       owner's free nor its next allocation found it again -- every free/alloc cycle at the
       edge opened another page and grew the bin (exact 128 KiB/1: +48% peak RSS with THP off).
       The owner's free now reclaims its own large page (MI_RECLAIM_ON_FREE_MAX_SIZE).
   (g) a bin's retired page serves another bin (#530): once a bin's only page empties it is kept
       (retired); after an aging tick without reuse, a different large bin that needs a page
       re-carves it instead of taking new arena slices (MI_LARGE_REPURPOSE), so a thread does not
       hold one empty page per bin. The page map covers `block_size * reserved` of a page only,
       so a re-carve that reaches further (2 x 384 KiB, mapped to 768 KiB, -> 4 x 256 KiB whose last starts at 768 KiB) must map
       its new tail: every block of the new geometry maps back to the page.
   (h) the owner's retired-slot mask (#530, `mi_tld_t.retired_used`) matches the slots after
       retired large pages are published and then freed (`_mi_page_free` clears the page's theap
       before unpublishing it: a mask that missed that path leaked every slot within 16 publishes).

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
#define SIZE_BLIP   (250 * 1024)
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

static int bin_of_size_is_large(size_t size) { const size_t b = mi_good_size(size); return (b > MI_MEDIUM_MAX_OBJ_SIZE && b <= MI_LARGE_MAX_OBJ_SIZE); }

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

/* ---- (e) one overflow does not grow the span -------------------------------- */

// on a fresh theap: an earlier case's retired page of another bin could otherwise be repurposed
// (#530) as this bin's first page, with that page's span
static THREAD_RET blip_main(void* arg);
static void case_blip(void) {
  thread_t t;
  thread_start(&t, &blip_main, NULL);
  thread_join(t);
}

static THREAD_RET blip_main(void* arg) {
  (void)arg;
  #if MI_LARGE_SPAN
  void* blocks[MAX_BLOCKS];
  size_t n = 0;
  void* const first = mi_malloc(SIZE_BLIP);
  assert(first != NULL);
  blocks[n++] = first;
  const mi_page_t* const page = _mi_ptr_page(first);
  while (n < MAX_BLOCKS && page->used < page->reserved) { blocks[n] = mi_malloc(SIZE_BLIP); assert(blocks[n] != NULL); n++; }
  void* const extra = mi_malloc(SIZE_BLIP);   // the compact page is full: one page request, after a full page
  assert(extra != NULL);
  fprintf(stderr, "(e) %zu blocks fill the first page (span %zu); the one more lands in a page of span %zu\n",
          n, span_of(first), span_of(extra));
  CHECK(span_is_compact(first) && span_is_compact(extra), "(e) one overflow should not grow the span (%zu -> %zu slices)", span_of(first), span_of(extra));
  mi_free(extra);
  free_all(blocks, n);
  #endif
  return THREAD_OK;
}

/* ---- (f) an exactly filled compact page is reused ------------------------- */

#define EDGE_CYCLES  (64)

// on a fresh theap (so the bin has seen nothing): fill the bin's first page exactly, then cycle
// free-one / allocate-one; every allocation must land in that page, and the bin must not grow
static THREAD_RET edge_main(void* arg) {
  const size_t size = *(const size_t*)arg;
  void* blocks[MAX_BLOCKS];
  size_t n = 0;
  blocks[n++] = mi_malloc(size);
  assert(blocks[0] != NULL);
  const mi_page_t* const page = _mi_ptr_page(blocks[0]);
  const size_t span = span_of(blocks[0]);
  const size_t bsize = mi_page_block_size(page);   // (read now: on a RED build the page may be gone by the report)
  if (bsize > MI_LARGE_MAX_OBJ_SIZE) {   // debug/guard padding pushed it into a singleton page: not a large bin
    fprintf(stderr, "(f) %zu B: a singleton page, not a large bin -- skipped\n", bsize);
    mi_free(blocks[0]);
    return THREAD_OK;
  }
  while (n < MAX_BLOCKS && page->used < page->reserved) { blocks[n] = mi_malloc(size); assert(blocks[n] != NULL); n++; }
  size_t moved = 0;
  size_t max_span = span;
  for (size_t i = 0; i < EDGE_CYCLES; i++) {
    const size_t k = i % n;
    mi_free(blocks[k]);
    blocks[k] = mi_malloc(size);
    assert(blocks[k] != NULL);
    memset(blocks[k], 0x33, 64);
    if (_mi_ptr_page(blocks[k]) != page) { moved++; }
    if (span_of(blocks[k]) > max_span) { max_span = span_of(blocks[k]); }
  }
  fprintf(stderr, "(f) %zu B: %zu live blocks fill a page of span %zu; %zu of %d free/alloc cycles left it, largest span %zu\n",
          bsize, n, span, moved, EDGE_CYCLES, max_span);
  CHECK(span < full_slices(), "(f) setup: the bin's first page should be compact (span %zu)", span);
  CHECK(moved == 0, "(f) %zu of %d allocations after freeing a block of the exactly filled page went to another page", moved, EDGE_CYCLES);
  CHECK(max_span == span, "(f) the bin grew (%zu -> %zu slices) though its live set never exceeded one page", span, max_span);
  free_all(blocks, n);
  return THREAD_OK;
}

static void case_edge(void) {
  static const size_t sizes[] = { 128 * 1024, 256 * 1024, 512 * 1024 };   // 8, 4 and 2 blocks per compact page
  for (size_t i = 0; i < sizeof(sizes) / sizeof(sizes[0]); i++) {
    thread_t t;
    thread_start(&t, &edge_main, (void*)&sizes[i]);
    thread_join(t);
  }
}

/* ---- (g) a retired page is repurposed for another bin --------------------- */

static THREAD_RET repurpose_main(void* arg) {
  (void)arg;
  void* const a = mi_malloc(160 * 1024);
  assert(a != NULL);
  const mi_page_t* const page_a = _mi_ptr_page(a);
  uint8_t* const slices_a = mi_page_slice_start(page_a);
  const size_t bsize_a = mi_page_block_size(page_a);
  mi_free(a);   // the bin's only page empties: it is retired, not freed
  mi_collect(false);   // one aging tick without reuse: the retired page is idle, so any bin may take it
  void* const b = mi_malloc(300 * 1024);
  assert(b != NULL);
  memset(b, 0x44, 300 * 1024);   // the whole block is usable
  const mi_page_t* const page_b = _mi_ptr_page(b);
  fprintf(stderr, "(g) %zu B block freed; a %zu B block then lands in %s slices (span %zu)\n",
          bsize_a, mi_page_block_size(page_b), mi_page_slice_start(page_b) == slices_a ? "the same" : "other", span_of(b));
  #if MI_LARGE_REPURPOSE
  CHECK(mi_page_slice_start(page_b) == slices_a, "(g) another bin's retired page should have been repurposed");
  CHECK(mi_page_block_size(page_b) != bsize_a && page_b->reserved >= 2, "(g) the repurposed page should have the new geometry");
  #endif
  mi_free(b);
  return THREAD_OK;
}

// the tail a 2-block geometry left unmapped is mapped once the page is re-carved
static THREAD_RET repurpose_extent_main(void* arg) {
  (void)arg;
  void* const a = mi_malloc(384 * 1024);
  assert(a != NULL);
  uint8_t* const slices_a = mi_page_slice_start(_mi_ptr_page(a));
  mi_free(a);
  mi_collect(false);
  void* blocks[8];
  size_t n = 0;
  const mi_page_t* page = NULL;
  for (; n < 8; n++) {
    blocks[n] = mi_malloc(256 * 1024);
    assert(blocks[n] != NULL);
    const mi_page_t* const pg = _mi_ptr_page(blocks[n]);
    if (n == 0) { page = pg; }
    if (pg != page) break;   // the page is full
    memset(blocks[n], 0x55, 256 * 1024);
    CHECK(mi_usable_size(blocks[n]) >= 256 * 1024, "(g) block %zu of the re-carved page has no usable size", n);
  }
  fprintf(stderr, "(g) extent: %zu blocks of 256 KiB in the page (%s slices as the freed 384 KiB block), all mapped\n",
          n, mi_page_slice_start(page) == slices_a ? "same" : "other");
  #if MI_LARGE_REPURPOSE
  CHECK(mi_page_slice_start(page) == slices_a, "(g) extent: the retired page should have been repurposed");
  CHECK(n == (size_t)page->reserved, "(g) extent: every block of the new geometry should map to the page (%zu of %u)", n, (unsigned)page->reserved);
  #endif
  for (size_t i = 0; i <= n && i < 8; i++) { mi_free(blocks[i]); }
  return THREAD_OK;
}

static void case_repurpose(void) {
  if (bin_of_size_is_large(160 * 1024) && bin_of_size_is_large(300 * 1024)) {
    thread_t t;
    thread_start(&t, &repurpose_main, NULL);
    thread_join(t);
  }
  if (bin_of_size_is_large(384 * 1024) && bin_of_size_is_large(256 * 1024)) {
    thread_t t;
    thread_start(&t, &repurpose_extent_main, NULL);
    thread_join(t);
  }
}

/* ---- (g2) every pair of large sizes: the re-carved page maps all its blocks (#573) ------
   A page re-carved from bin A to bin B must map every block of B's geometry to itself, whatever
   the two sizes. (g) checks two pairs; the page-map bug of #572 needed a specific pair and showed
   only in a multi-threaded debug run, so this walks all of them on one thread at a time. A debug
   build also asserts the first and last byte of the page at every `_mi_page_init`. */

static const size_t RECARVE_SIZES[] = { 96, 112, 128, 160, 192, 224, 256, 300, 384, 448, 512 };   // KiB
#define RECARVE_COUNT  (sizeof(RECARVE_SIZES) / sizeof(RECARVE_SIZES[0]))

typedef struct recarve_s { size_t size_a, size_b; } recarve_t;

static THREAD_RET recarve_main(void* arg) {
  const recarve_t* const pair = (const recarve_t*)arg;
  void* const a = mi_malloc(pair->size_a);
  assert(a != NULL);
  const mi_page_t* const original = _mi_ptr_page(a);
  const size_t old_extent = mi_slice_count_of_size(
      (size_t)(mi_page_start(original) - mi_page_slice_start(original)) + mi_page_size(original));
  mi_free(a);          // the bin's only page empties: it is retired
  mi_collect(false);   // one aging tick without reuse: any bin may take it
  const uint64_t registrations = _mi_event_get(MI_EVENT_PAGE_MAP_REGISTER);
  void* blocks[32];
  size_t n = 0;
  const mi_page_t* page = NULL;
  for (; n < 32; n++) {   // fill B's page, then one block more
    blocks[n] = mi_malloc(pair->size_b);
    assert(blocks[n] != NULL);
    const mi_page_t* const pg = _mi_ptr_page(blocks[n]);
    if (n == 0) {
      page = pg;
      if (page->memid.memkind == MI_MEM_ARENA && mi_page_meta_is_separated(page)) {
        const size_t extent = mi_slice_count_of_size(mi_page_size(page));
        if (extent < page->memid.mem.arena.slice_count) {
          CHECK(_mi_safe_ptr_page(mi_page_slice_start(page) + mi_size_of_slices(extent)) != page,
                "(g2) %zu -> %zu KiB: stale tail still maps to the re-carved page", pair->size_a >> 10, pair->size_b >> 10);
        }
        #if MI_DIAGNOSTICS
        if (page == original && extent < old_extent) {
          CHECK(_mi_event_get(MI_EVENT_PAGE_MAP_REGISTER) == registrations,
                "(g2) shrinking an owned page must preserve its already registered prefix");
        }
        #endif
      }
    }
    if (pg != page) { n++; break; }
    memset(blocks[n], 0x66, pair->size_b);
    CHECK(mi_usable_size(blocks[n]) >= pair->size_b, "(g2) %zu -> %zu KiB: block %zu has no usable size", pair->size_a >> 10, pair->size_b >> 10, n);
  }
  for (size_t i = 0; i < n; i++) { mi_free(blocks[i]); }
  MI_UNUSED(old_extent); MI_UNUSED(registrations);
  return THREAD_OK;
}

static void case_recarve_matrix(void) {
  size_t pairs = 0;
  for (size_t i = 0; i < RECARVE_COUNT; i++) {
    for (size_t j = 0; j < RECARVE_COUNT; j++) {
      recarve_t pair = { RECARVE_SIZES[i] << 10, RECARVE_SIZES[j] << 10 };
      if (!bin_of_size_is_large(pair.size_a) || !bin_of_size_is_large(pair.size_b)) { continue; }
      thread_t t;
      thread_start(&t, &recarve_main, &pair);
      thread_join(t);
      pairs++;
    }
  }
  fprintf(stderr, "(g2) %zu ordered pairs of large sizes re-carved, every block of the new geometry mapped\n", pairs);
  CHECK(pairs > 0, "(g2) no pair of sizes is a large bin in this build");
}

/* ---- (h) the retired-slot mask stays exact ------------------------------- */

static size_t slots_in_use(const mi_tld_t* tld) {
  size_t used = 0;
  for (size_t k = 0; k < MI_RETIRED_PAGE_SLOTS; k++) {
    if (mi_atomic_load_ptr_relaxed(mi_page_t, (_Atomic(mi_page_t*)*)&tld->retired_pages[k]) != NULL) { used |= ((size_t)1 << k); }
  }
  return used;
}

static THREAD_RET slots_main(void* arg) {
  (void)arg;
  static const size_t sizes[] = { 100 * 1024, 130 * 1024, 170 * 1024, 220 * 1024, 290 * 1024, 380 * 1024, 500 * 1024 };
  const size_t nsizes = sizeof(sizes) / sizeof(sizes[0]);
  void* const probe = mi_malloc(sizes[0]);   // this thread's tld, through its page's theap
  assert(probe != NULL);
  const mi_tld_t* const tld = _mi_ptr_page(probe)->theap->tld;
  mi_free(probe);
  size_t mismatches = 0;
  for (size_t round = 0; round < 8 * MI_RETIRED_PAGE_SLOTS; round++) {
    void* const p = mi_malloc(sizes[round % nsizes]);   // a bin's only page ...
    assert(p != NULL);
    mi_free(p);                                        // ... empties: retired and published
    if (round % 3 == 2) { mi_collect(false); }         // expire some: freed via `_mi_page_free`
    if (tld->retired_used != slots_in_use(tld)) { mismatches++; }
  }
  mi_collect(true);
  fprintf(stderr, "(h) retired-slot mask vs slots after %d publish/free rounds: %zu mismatches (mask %zx, slots %zx)\n",
          8 * MI_RETIRED_PAGE_SLOTS, mismatches, (size_t)tld->retired_used, slots_in_use(tld));
  CHECK(mismatches == 0, "(h) the owner's retired-slot mask drifted from the slots (%zu times)", mismatches);
  return THREAD_OK;
}

static void case_slots(void) {
  thread_t t;
  thread_start(&t, &slots_main, NULL);
  thread_join(t);
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

#include "retired-search.h"
#include "retired-queue-remove.h"

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
  #ifdef MI_LARGE_SPAN_MAX_KIB
  mi_option_set(mi_option_large_span_max, 0);   // #575 caps the span at 1 MiB; these cases test the growth to the full span (test-large-fungible.c tests the cap)
  #endif
  case_a();
  case_b();
  case_decay();
  case_c();
  case_off();
  case_blip();
  case_edge();
  case_repurpose();
  case_recarve_matrix();
  case_slots();
  case_retired_queue_remove();
  case_retired_search();
  case_retired_search_budget();
  #endif
  if (failures > 0) { fprintf(stderr, "%d check(s) failed\n", failures); return 1; }
  fprintf(stderr, "ok\n");
  return 0;
  #endif
}
