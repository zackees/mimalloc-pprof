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

int main(void) {
  test_bootstrap_race();
  test_cross_thread_free();
  test_snapshot_under_mutation();
  printf("test-profile-race: ok\n");
  return 0;
}
