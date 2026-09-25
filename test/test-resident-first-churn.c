/* #517 (root cause on #514): once warmed up, the memory gate's thread-churn scenario must not
   claim arena slices that were never dirty.

   #501 made resident-first claiming (#493, `mi_option_resident_first`) apply to every claim size.
   A small or medium page claim then took a queued (free, still resident) run that another size
   class's pages were about to reuse, so those pages spilled into fresh, never-dirty chunks. On
   main the churn below made 110 fresh claims after warm-up, and the memory gate's peak RSS went
   from 58.2 MB to 61-63.7 MB. The fix only claims resident-first for claims of at least
   MI_RESIDENT_FIRST_MIN_SLICES slices.

   The test runs `churn_worker` / `scenario_thread_churn` copied verbatim from
   test/test-memory-gate.c (minus the leak injection): one warm-up pass and a collect, exactly as
   the gate does, then resets the arena claim counters and runs the churn again. It asserts that
   the second pass made no plain-search claim on never-dirty slices, and (as a check that the
   counters are wired at all) that it made some claims.

   It asserts on claim counts, not RSS: the counters are exact, while RSS depends on the kernel.
   They exist only with MI_DIAGNOSTICS=1 (`_mi_arena_claim_counters` returns false otherwise, and
   the test skips). ctest turns the scavenger off, so no background purge runs, and sets
   MIMALLOC_PURGE_ZEROES=0, because a purge that zeroes a range clears its dirty bits and the
   next claim of it would count as fresh. */

#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <mimalloc.h>
#include "mimalloc/internal.h"   // _mi_arena_claim_counters (#517)

#define CHURN_THREADS       8
#define CHURN_ROUNDS        20
#define CHURN_KEEP_BLOCKS   48
#define CHURN_KEEP_SIZE     (64*1024)
#define CHURN_KEEP_TOUCH    4096
#define CHURN_SMALL_ALLOCS  1500
#define CHURN_SMALL_BASE    64
#define CHURN_SMALL_SPREAD  900
#define MAX_FRESH_CLAIMS    0      // after warm-up the churn must reuse dirty slices only

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
typedef pthread_t thread_t;
typedef void* (*thread_fun_t)(void*);
#define THREAD_RET void*
#define THREAD_OK  NULL
static void thread_start(thread_t* t, thread_fun_t fn, void* arg) {
  assert(pthread_create(t, NULL, fn, arg) == 0);
}
static void thread_join(thread_t t) { assert(pthread_join(t, NULL) == 0); }
#endif

/* ---- portable rendezvous barrier (from test/test-memory-gate.c) ---------- */

#ifdef _WIN32
typedef struct barrier_s {
  CRITICAL_SECTION    lock;
  CONDITION_VARIABLE  cond;
  int n, waiting, generation;
} barrier_t;
static void barrier_init(barrier_t* b, int n) {
  InitializeCriticalSection(&b->lock);
  InitializeConditionVariable(&b->cond);
  b->n = n; b->waiting = 0; b->generation = 0;
}
static void barrier_destroy(barrier_t* b) { DeleteCriticalSection(&b->lock); }
static void barrier_wait(barrier_t* b) {
  EnterCriticalSection(&b->lock);
  const int gen = b->generation;
  if (++b->waiting == b->n) {
    b->waiting = 0; b->generation++;
    WakeAllConditionVariable(&b->cond);
  }
  else {
    while (b->generation == gen) { SleepConditionVariableCS(&b->cond, &b->lock, INFINITE); }
  }
  LeaveCriticalSection(&b->lock);
}
#else
typedef struct barrier_s {
  pthread_mutex_t lock;
  pthread_cond_t  cond;
  int n, waiting, generation;
} barrier_t;
static void barrier_init(barrier_t* b, int n) {
  assert(pthread_mutex_init(&b->lock, NULL) == 0);
  assert(pthread_cond_init(&b->cond, NULL) == 0);
  b->n = n; b->waiting = 0; b->generation = 0;
}
static void barrier_destroy(barrier_t* b) {
  pthread_mutex_destroy(&b->lock); pthread_cond_destroy(&b->cond);
}
static void barrier_wait(barrier_t* b) {
  pthread_mutex_lock(&b->lock);
  const int gen = b->generation;
  if (++b->waiting == b->n) {
    b->waiting = 0; b->generation++;
    pthread_cond_broadcast(&b->cond);
  }
  else {
    while (b->generation == gen) { pthread_cond_wait(&b->cond, &b->lock); }
  }
  pthread_mutex_unlock(&b->lock);
}
#endif

/* ---- the memory gate's scenario 1 (thread churn) ------------------------- */

static THREAD_RET churn_worker(void* arg) {
  barrier_t* rendezvous = (barrier_t*)arg;
  void* keep[CHURN_KEEP_BLOCKS];
  for (size_t i = 0; i < CHURN_KEEP_BLOCKS; i++) {
    keep[i] = mi_malloc(CHURN_KEEP_SIZE);
    assert(keep[i] != NULL);
    memset(keep[i], 0x5A, CHURN_KEEP_TOUCH);
  }
  barrier_wait(rendezvous);
  for (size_t i = 0; i < CHURN_SMALL_ALLOCS; i++) {
    void* p = mi_malloc(CHURN_SMALL_BASE + (i % CHURN_SMALL_SPREAD));
    assert(p != NULL);
    mi_free(p);
  }
  barrier_wait(rendezvous);
  for (size_t i = 0; i < CHURN_KEEP_BLOCKS; i++) { mi_free(keep[i]); }
  return THREAD_OK;
}

static void scenario_thread_churn(void) {
  for (int r = 0; r < CHURN_ROUNDS; r++) {
    barrier_t rendezvous;
    barrier_init(&rendezvous, CHURN_THREADS);
    thread_t th[CHURN_THREADS];
    for (int i = 0; i < CHURN_THREADS; i++) thread_start(&th[i], (thread_fun_t)churn_worker, &rendezvous);
    for (int i = 0; i < CHURN_THREADS; i++) thread_join(th[i]);
    barrier_destroy(&rendezvous);
  }
}

int main(void) {
  mi_arena_claim_counters_t c;
  if (!_mi_arena_claim_counters(&c)) {
    fprintf(stderr, "skipped: claim counters need MI_DIAGNOSTICS=1\n");
    return 0;
  }
  #if defined(MI_GUARDED) && MI_GUARDED
  if (mi_option_get(mi_option_guarded_sample_rate) != 0) {   // takes the plain search: see `mi_arena_try_claim_resident`
    fprintf(stderr, "skipped: guarded sampling is on, so resident-first is off\n");
    return 0;
  }
  #endif

  // warm-up, exactly as the memory gate does before its measured pass
  scenario_thread_churn();
  mi_collect(true);

  _mi_arena_claim_counters_reset();
  scenario_thread_churn();
  memset(&c, 0, sizeof(c));
  _mi_arena_claim_counters(&c);

  fprintf(stderr, "claims after warm-up: resident_first=%zu (%zu slices) plain_reused=%zu (%zu slices) plain_fresh=%zu (%zu slices)\n",
          c.resident_first_claims, c.resident_first_slices,
          c.plain_reused_claims, c.plain_reused_slices,
          c.plain_fresh_claims, c.plain_fresh_slices);

  if (c.plain_reused_claims + c.resident_first_claims == 0) {
    fprintf(stderr, "FAILED (setup): no reused claims were counted, so the claim counters are not wired\n");
    return 1;
  }
  if (c.plain_fresh_claims > MAX_FRESH_CLAIMS) {
    fprintf(stderr, "FAILED (#517): the warmed-up churn claimed never-dirty arena slices %zu times; "
                    "resident-first on small claims (below MI_RESIDENT_FIRST_MIN_SLICES) takes other size classes' queued runs\n",
            c.plain_fresh_claims);
    return 1;
  }
  return 0;
}
