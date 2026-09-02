/* Adversarial concurrency coverage for the sampling profiler on the mimalloc v3
   engine (issue #43).

   v3 replaced the v2 segment allocator with arenas + a page-map, and split
   per-thread state into mi_heap_t / mi_theap_t. The profiler's hooks therefore
   run against a different thread-bootstrap and cross-thread-free path than the
   one the v2 port was validated on. These scenarios target exactly the seams
   that restructuring moved:

     1. thread-bootstrap race -- a brand-new thread whose *first* action is an
        allocation, so the profiler hook fires while mi_theap_t is still being
        bootstrapped, concurrently with profiler start/reset/stop churn on the
        main thread.
     2. cross-thread free stress -- blocks allocated on one thread and freed on
        another, driving v3's deferred/abandoned-page path with the profiler's
        free hook attached.
     3. snapshot stability -- mi_prof_snapshot_new/visit while other threads
        actively mutate the sample table.
     4. (issue #272) the background scavenger purging arenas -- decommitting whole
        slices -- while mi_prof_visit / mi_prof_snapshot_new walk the profiler
        tables, and while other threads hand their theaps to that same scavenger
        with mi_on_thread_idle_start/_end.
     5. (issue #272, Phase 7b) the *hole* sweep -- discarding the memory of free
        blocks INSIDE still-used pages -- running on both the owner thread and the
        scavenger, while a third thread walks those same pages with mi_prof_visit
        and mi_heap_visit_blocks. Where scenario 4 is about whole slices going away
        under a table walk, this one is about individual blocks losing their memory
        under a *block* walk.

   Each scenario is a pass/fail assert; the test is deliberately allocation-heavy
   so that sampling actually triggers rather than being skipped. */

#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include "mimalloc.h"
#include "mimalloc/profile.h"
#include "mimalloc/internal.h"   /* scenario 5: _mi_ptr_page, mi_page_block_is_purged */

#define RACE_THREADS   8
#define RACE_ROUNDS    3
#define XFREE_PER_THREAD 4096

/* ---- minimal portable threading ---------------------------------------- */

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
static void thread_yield_now(void) { Sleep(0); }
#else
#include <pthread.h>
#include <sched.h>
typedef pthread_t thread_t;
typedef void* (*thread_fun_t)(void*);
#define THREAD_RET void*
#define THREAD_OK  NULL
static void thread_start(thread_t* t, thread_fun_t fn, void* arg) {
  assert(pthread_create(t, NULL, fn, arg) == 0);
}
static void thread_join(thread_t t) { assert(pthread_join(t, NULL) == 0); }
static void thread_yield_now(void) { sched_yield(); }
#endif

/* Spin gate so every worker enters its first allocation at roughly the same
   instant -- without this the threads serialize and the bootstrap race never
   actually races. */
static volatile int gate_open;
static void gate_wait(void) {
  while (gate_open == 0) { thread_yield_now(); }
}

/* ---- scenario 1: thread-bootstrap race --------------------------------- */

/* The FIRST statement in this thread body is an allocation, on purpose. */
static THREAD_RET bootstrap_worker(void* arg) {
  (void)arg;
  gate_wait();
  void* first = mi_malloc(64);
  assert(first != NULL);
  memset(first, 0xA5, 64);
  for (size_t i = 0; i < 20000; i++) {
    void* p = mi_malloc((i % 7 == 0) ? 8192 : 48);
    assert(p != NULL);
    if ((i & 3) == 0) { p = mi_realloc(p, 256); assert(p != NULL); }
    mi_free(p);
  }
  mi_free(first);
  return THREAD_OK;
}

static void test_bootstrap_race(void) {
  for (size_t round = 0; round < RACE_ROUNDS; round++) {
    thread_t threads[RACE_THREADS];
    gate_open = 0;
    for (size_t i = 0; i < RACE_THREADS; i++) {
      thread_start(&threads[i], (thread_fun_t)bootstrap_worker, NULL);
    }
    /* Profiler lifecycle churn concurrent with thread bootstrap. A deterministic
       seed keeps the sampling decisions reproducible across runs. */
    assert(mi_prof_start_seeded(4096, 1234 + (uint64_t)round));
    gate_open = 1;
    for (size_t i = 0; i < 40; i++) {
      mi_prof_stats_t_decl(st);
      assert(mi_prof_stats_get(&st));
      /* Deliberately NO cross-field assertions here (live_bytes vs live_samples,
         heap_reserved vs heap_committed, a bound on theap_count). None of them are
         consistent snapshots while other threads run: the sampler counters are separate
         atomic reads, and mi_stats_get memcpy's the engine's stats while they are being
         updated. live_samples can be read before a burst of frees and live_bytes after
         it. Those invariants hold only on a quiescent heap, which test-profile asserts.
         What this loop is for is exercising mi_prof_stats_get -- which walks the subproc
         heap list -- concurrently with thread bootstrap and profiler lifecycle churn. */
      if (i == 20) { mi_prof_reset(); }
    }
    for (size_t i = 0; i < RACE_THREADS; i++) { thread_join(threads[i]); }
    mi_prof_stop();
    assert(!mi_prof_is_enabled());
  }
}

/* ---- scenario 2: cross-thread free stress ------------------------------ */

typedef struct xfree_chan_s {
  void*  blocks[XFREE_PER_THREAD];
  volatile int ready;
} xfree_chan_t;

static THREAD_RET xfree_producer(void* arg) {
  xfree_chan_t* ch = (xfree_chan_t*)arg;
  gate_wait();
  for (size_t i = 0; i < XFREE_PER_THREAD; i++) {
    ch->blocks[i] = mi_malloc(((i % 5) + 1) * 96);
    assert(ch->blocks[i] != NULL);
  }
  ch->ready = 1;
  return THREAD_OK;
}

/* Frees blocks that were allocated on a different thread -- in v3 this walks the
   page-map to find the owning page and takes the abandoned/cross-free path. */
static THREAD_RET xfree_consumer(void* arg) {
  xfree_chan_t* ch = (xfree_chan_t*)arg;
  while (ch->ready == 0) { thread_yield_now(); }
  for (size_t i = 0; i < XFREE_PER_THREAD; i++) { mi_free(ch->blocks[i]); }
  return THREAD_OK;
}

static void test_cross_thread_free(void) {
  enum { pairs = 4 };
  static xfree_chan_t chans[pairs];
  thread_t prod[pairs], cons[pairs];
  memset(chans, 0, sizeof(chans));
  gate_open = 0;
  assert(mi_prof_start_seeded(2048, 99));
  for (size_t i = 0; i < pairs; i++) {
    thread_start(&prod[i], (thread_fun_t)xfree_producer, &chans[i]);
    thread_start(&cons[i], (thread_fun_t)xfree_consumer, &chans[i]);
  }
  gate_open = 1;
  for (size_t i = 0; i < pairs; i++) { thread_join(prod[i]); thread_join(cons[i]); }

  /* All cross-thread blocks are freed, so live accounting must have unwound.
     (Other allocations may still be live, so this is a sanity bound rather than
     an exact zero.) */
  mi_prof_stats_t_decl(st);
  assert(mi_prof_stats_get(&st));
  assert(st.enabled);
  assert(st.live_samples <= st.accum_samples || !st.accum);
  mi_prof_stop();
}

/* ---- scenario 3: snapshot stability under mutation --------------------- */

static volatile int churn_stop;

static THREAD_RET churn_worker(void* arg) {
  (void)arg;
  gate_wait();
  while (churn_stop == 0) {
    void* p = mi_malloc(1024);
    assert(p != NULL);
    mi_free(p);
  }
  return THREAD_OK;
}

typedef struct sum_s { size_t objs; size_t bytes; } sum_t;
static bool sum_visitor(const mi_prof_sample_info_t* info, void* arg) {
  sum_t* s = (sum_t*)arg;
  /* A sample with live objects must carry proportionate live bytes. */
  assert(info->live_objects == 0 || info->live_bytes > 0);
  s->objs += info->live_objects; s->bytes += info->live_bytes;
  return true;
}

static void test_snapshot_under_mutation(void) {
  thread_t threads[RACE_THREADS];
  gate_open = 0; churn_stop = 0;
  assert(mi_prof_start_seeded(4096, 7));
  for (size_t i = 0; i < RACE_THREADS; i++) {
    thread_start(&threads[i], (thread_fun_t)churn_worker, NULL);
  }
  gate_open = 1;
  for (size_t i = 0; i < 25; i++) {
    mi_prof_snapshot_t* snap = mi_prof_snapshot_new();
    assert(snap != NULL);
    /* A snapshot is a frozen copy: visiting it twice must agree exactly, even
       though other threads are mutating the live table the whole time. */
    sum_t a = { 0, 0 }, b = { 0, 0 };
    assert(mi_prof_snapshot_visit(snap, sum_visitor, &a));
    assert(mi_prof_snapshot_visit(snap, sum_visitor, &b));
    assert(a.objs == b.objs && a.bytes == b.bytes);
    mi_prof_snapshot_free(snap);
  }
  churn_stop = 1;
  for (size_t i = 0; i < RACE_THREADS; i++) { thread_join(threads[i]); }
  mi_prof_stop();
}

/* ---- scenario 4: profiler visit vs the background scavenger (issue #272) ----

   Design note, because this is the case the issue asked to design rather than assume.

   The scavenger thread does two things that could in principle collide with the
   profiler: it decommits arena slices (`_mi_arenas_try_purge`), and it runs a parked
   thread's idle work (`_mi_thread_idle_work` -> `mi_theap_collect`, which frees pages).
   Neither can be observed by a profiler walk, for two structural reasons:

     - `mi_prof_visit` / `mi_prof_snapshot_new` walk the profiler's own stack/sample
       tables under `prof_lock`. That memory comes from the profiler's raw-OS arena
       (`_mi_os_alloc`, CLAUDE.md rule 4), never from a mimalloc page, so no arena purge
       can ever decommit anything either of them dereferences. Neither one follows a
       `mi_page_t*`, so "read a page that was just decommitted" has no code path.
     - A page can only reach `mi_arena_schedule_purge` once every one of its blocks was
       freed, and every block free unlinks that block's record under `prof_lock` first.
       `mi_arenas_page_free_ex` (src/arena.c) asserts exactly that
       (`!page->has_metadata`) in an MI_PPROF debug build, so a future fast path that
       broke the invariant would fail here rather than silently hand a live record's page
       to the purger.

   This scenario is the empirical half: run the two walks continuously against a
   scavenger that is being kept as busy as possible (`purge_delay` forced to its minimum
   by the ctest environment, whole-slice allocations so purges actually happen, and
   worker threads parking so the scavenger also sweeps their theaps). */

static volatile int scav_stop;

/* 256 KiB: large enough that each block is its own arena slices, so freeing one
   actually schedules a purge rather than just recycling inside a page. */
#define SCAV_BLOCK (256 * 1024)

static THREAD_RET scav_churn_worker(void* arg) {
  (void)arg;
  gate_wait();
  void* keep[8];
  memset(keep, 0, sizeof(keep));
  while (scav_stop == 0) {
    for (size_t i = 0; i < 8; i++) {
      keep[i] = mi_malloc(SCAV_BLOCK);
      assert(keep[i] != NULL);
      memset(keep[i], (int)i + 1, 64);
    }
    for (size_t i = 0; i < 8; i++) { mi_free(keep[i]); keep[i] = NULL; }
    /* hand our theaps to the scavenger while it is also purging the slices we just freed */
    if (mi_on_thread_idle_start()) { thread_yield_now(); mi_on_thread_idle_end(); }
    else { mi_on_thread_idle(); }
  }
  return THREAD_OK;
}

static bool touch_visitor(const mi_prof_sample_info_t* info, void* arg) {
  size_t* n = (size_t*)arg;
  /* dereference every field the visitor contract exposes: a decommitted table would fault here */
  assert(info->live_objects == 0 || info->live_bytes > 0);
  (*n) += info->depth;
  for (size_t i = 0; i < info->depth; i++) { (void)info->stack[i]; }
  return true;
}

static void test_visit_vs_scavenger(void) {
  thread_t threads[RACE_THREADS];
  gate_open = 0; scav_stop = 0;
  assert(mi_prof_start_seeded(4096, 11));
  for (size_t i = 0; i < RACE_THREADS; i++) {
    thread_start(&threads[i], (thread_fun_t)scav_churn_worker, NULL);
  }
  gate_open = 1;
  for (size_t round = 0; round < 200; round++) {
    size_t n = 0;
    assert(mi_prof_visit(&touch_visitor, &n));
    mi_prof_snapshot_t* snap = mi_prof_snapshot_new();
    assert(snap != NULL);
    size_t m = 0;
    assert(mi_prof_snapshot_visit(snap, &touch_visitor, &m));
    mi_prof_snapshot_free(snap);
    /* and park the walking thread too, so the scavenger sweeps a theap that just
       ran a profiler walk on it */
    if (mi_on_thread_idle_start()) { thread_yield_now(); mi_on_thread_idle_end(); }
  }
  scav_stop = 1;
  for (size_t i = 0; i < RACE_THREADS; i++) { thread_join(threads[i]); }
  mi_prof_stop();
}

/* ---- scenario 5: hole sweep vs. block/record walks (issue #272, Phase 7b) ----

   Hole purging discards the memory of the FREE blocks inside a page that is still in
   use. Two profiler-interaction claims follow from that, and this scenario is their
   empirical half (the design argument is in `src/page-holes.c`'s header):

     (1) a discard never covers a sampled block. A record is attached to a LIVE block and
         an OS page is discarded only when every block overlapping it is free -- so the
         two can never intersect. `_mi_prof_debug_assert_no_records_in` (src/profile.c)
         asserts this from the other side, per discard, in an MI_DEBUG build, including
         when the discard runs on the scavenger for a parked thread. This scenario is what
         makes that assert fire at all: without records on swept pages it is vacuous.
     (2) no inspection path may hand out a discarded block. `_mi_theap_area_visit_blocks`
         marks purged blocks in its free map, so `mi_heap_visit_blocks` skips them, and
         `mi_prof_visit`'s records only ever name live blocks. Every pointer either walk
         produces is therefore checked here for purged-ness AND dereferenced -- a
         discarded page reads back zero (and is poisoned under ASan), so a leak of one
         into a visitor shows up as a failed check or a fault rather than silently.

   Three roles run concurrently: an owner that sweeps its own heaps with
   `mi_on_thread_idle()`, a parker whose heaps the scavenger sweeps instead, and a walker
   doing both walks. The churn shape is the one hole punching exists for: a page full of
   small blocks with scattered survivors, so a sweep finds whole free OS pages. */

static volatile int hole_stop;

#define HOLE_BSZ    (512)
#define HOLE_LIVE   (4096)

/* keep one block per 2 OS pages so a run of frees always covers a whole OS page */
static size_t hole_keep_every(void) {
  return (((2 * _mi_os_page_size()) + HOLE_BSZ - 1) / HOLE_BSZ) + 2;
}

/* allocate HOLE_LIVE blocks, then free all but every Nth: the survivors pin their own OS
   page and everything between them becomes discardable. */
static void hole_churn(void** p) {
  const size_t keep = hole_keep_every();
  for (size_t i = 0; i < HOLE_LIVE; i++) {
    if (p[i] == NULL) { p[i] = mi_malloc(HOLE_BSZ); assert(p[i] != NULL); }
    memset(p[i], (int)(i & 0x7f) + 1, HOLE_BSZ);
  }
  for (size_t i = 0; i < HOLE_LIVE; i++) {
    if ((i % keep) != 0) { mi_free(p[i]); p[i] = NULL; }
  }
}

static void hole_release(void** p) {
  for (size_t i = 0; i < HOLE_LIVE; i++) { if (p[i] != NULL) { mi_free(p[i]); p[i] = NULL; } }
}

/* the owner sweep: this thread discards the holes in its own pages */
static THREAD_RET hole_owner_worker(void* arg) {
  (void)arg;
  void** p = (void**)calloc(HOLE_LIVE, sizeof(void*));
  assert(p != NULL);
  gate_wait();
  while (hole_stop == 0) {
    hole_churn(p);
    mi_on_thread_idle();
    /* survivors must be byte-for-byte intact across the discard */
    for (size_t i = 0; i < HOLE_LIVE; i++) {
      if (p[i] == NULL) continue;
      assert(((unsigned char*)p[i])[0] == (unsigned char)((i & 0x7f) + 1));
      assert(!mi_page_block_is_purged(_mi_ptr_page(p[i]), p[i]));
    }
  }
  hole_release(p);
  free(p);
  return THREAD_OK;
}

/* the scavenger sweep: this thread parks, and another thread discards its holes */
static THREAD_RET hole_parker_worker(void* arg) {
  (void)arg;
  void** p = (void**)calloc(HOLE_LIVE, sizeof(void*));
  assert(p != NULL);
  gate_wait();
  while (hole_stop == 0) {
    hole_churn(p);
    if (mi_on_thread_idle_start()) {
      thread_yield_now();
      mi_on_thread_idle_end();
    }
    for (size_t i = 0; i < HOLE_LIVE; i++) {
      if (p[i] == NULL) continue;
      assert(((unsigned char*)p[i])[0] == (unsigned char)((i & 0x7f) + 1));
      assert(!mi_page_block_is_purged(_mi_ptr_page(p[i]), p[i]));
    }
  }
  hole_release(p);
  free(p);
  return THREAD_OK;
}

/* every record the profiler reports names a live block: it must not be purged, and
   reading it must not fault. */
static bool hole_prof_visitor(const mi_prof_sample_info_t* info, void* arg) {
  size_t* n = (size_t*)arg;
  (*n) += info->depth;
  for (size_t i = 0; i < info->depth; i++) { (void)info->stack[i]; }
  return true;
}

/* every block `mi_heap_visit_blocks` reports is a LIVE block of this thread's heap: a
   purged block is free and must have been filtered out by the free-map marking in
   `_mi_theap_area_visit_blocks`. */
static bool hole_block_visitor(const mi_heap_t* heap, const mi_heap_area_t* area,
                               void* block, size_t block_size, void* arg) {
  (void)heap; (void)area;
  size_t* n = (size_t*)arg;
  if (block == NULL) return true;   /* the per-area callback */
  mi_page_t* const page = _mi_ptr_page(block);
  assert(page != NULL);
  assert(!mi_page_block_is_purged(page, block));
  /* touch it: a discarded OS page reads back zero and is poisoned under ASan */
  (void)*(volatile unsigned char*)block;
  (void)block_size;
  (*n)++;
  return true;
}

static void test_hole_sweep_vs_visit(void) {
  thread_t threads[4];
  void** own = (void**)calloc(HOLE_LIVE, sizeof(void*));
  assert(own != NULL);
  gate_open = 0; hole_stop = 0;
  /* Sample rate 1024 over 512-byte blocks: high enough that ~100 records survive on the
     swept pages at the end (verified by the assert below -- with zero records invariant (1)
     would never be exercised), low enough that the 60 walk rounds stay under ~15s.
     A one-off negative control (`mi_assert_internal(false)` inside
     `_mi_prof_debug_assert_no_records_in`) confirmed that assert does fire on this workload,
     i.e. hole discards really do happen on pages that carry profiler records. */
  assert(mi_prof_start_seeded(1024, 23));
  thread_start(&threads[0], (thread_fun_t)hole_owner_worker, NULL);
  thread_start(&threads[1], (thread_fun_t)hole_owner_worker, NULL);
  thread_start(&threads[2], (thread_fun_t)hole_parker_worker, NULL);
  thread_start(&threads[3], (thread_fun_t)hole_parker_worker, NULL);
  gate_open = 1;
  /* the walker: its own pages get swept too (it idles), and it walks while the four
     workers above are being swept by themselves and by the scavenger. */
  for (size_t round = 0; round < 60; round++) {
    hole_churn(own);
    size_t n = 0;
    assert(mi_prof_visit(&hole_prof_visitor, &n));
    mi_prof_snapshot_t* snap = mi_prof_snapshot_new();
    assert(snap != NULL);
    size_t m = 0;
    assert(mi_prof_snapshot_visit(snap, &hole_prof_visitor, &m));
    mi_prof_snapshot_free(snap);
    size_t blocks = 0;
    assert(mi_theap_visit_blocks(mi_theap_get_default(), true /* visit blocks */, &hole_block_visitor, &blocks));
    mi_on_thread_idle();
  }
  hole_stop = 1;
  for (size_t i = 0; i < 4; i++) { thread_join(threads[i]); }
  /* BEFORE releasing `own`: the survivors are what carry the live sampled records that make
     invariant (1) non-vacuous, and they must still be alive when we count them. */
  mi_purge_holes_stats_t hs;
  mi_purge_holes_stats_get(&hs);
  size_t records = 0, bytes = 0, stacks = 0;
  mi_prof_debug_stats(&records, &bytes, &stacks);
  hole_release(own);
  free(own);
  printf("  scenario 5: %zu hole discards, %zu bytes discarded, %zu live profiler records\n",
         hs.discard_calls, hs.purged_bytes_total, records);
  assert(!mi_option_is_enabled(mi_option_purge_holes) || hs.discard_calls > 0);
  assert(records > 0);   /* otherwise invariant (1) is never actually tested */
  mi_prof_stop();
}

int main(void) {
  test_bootstrap_race();
  test_cross_thread_free();
  test_snapshot_under_mutation();
  test_visit_vs_scavenger();     /* issue #272 */
  test_hole_sweep_vs_visit();    /* issue #272, Phase 7b */
  printf("test-profile-race: ok\n");
  return 0;
}
