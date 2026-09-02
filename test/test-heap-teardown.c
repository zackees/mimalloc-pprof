/* ----------------------------------------------------------------------------
Copyright (c) 2018-2026, Microsoft Research, Daan Leijen
This is free software; you can redistribute it and/or modify it under the
terms of the MIT license.
-----------------------------------------------------------------------------*/

// imported from oven-sh/mimalloc @ 942b8342, MIT (issue #271 / Bun parity P6).

/* Tests for the heap delete/destroy protocol (see the "Heap delete and destroy"
   section in `src/heap.c` and `mi_heap_visit_page_claim` in `src/arena.c`).

   `test-heap-mt` stresses `mi_free` against `mi_heap_delete` with small blocks.
   These cover the rest of the contract:

   pin            (debug builds) the deleter has a page pinned but not yet
                  claimed; a concurrent free that empties the page must not be
                  able to release it until the pin is dropped, and once it is
                  the deleter must see the page is gone.
   page-churn     frees that empty whole pages during a delete while other
                  threads allocate and check fresh pages in other heaps, so a
                  slice released under the deleter is reused at once.
   os-pages       heaps with OS allocated (over-aligned) blocks: held by the
                  deleting thread's theap, held by an exited thread (abandoned),
                  and freed by another thread during the delete.
   foreign-theap  a live thread that allocated from the heap earlier (and holds
                  pages of it in its theap) while another thread deletes it,
                  then frees its blocks and exits afterwards.
   two-deletes    two heaps deleted at once while other threads free into both.
   parked         a thread that used the heap is parked (`mi_on_thread_idle_start`)
                  and swept by the scavenger while the heap is deleted.

   > mimalloc-test-heap-teardown [ITER]
*/

#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>

#include "mimalloc.h"

#if defined(MI_TSAN)
static int ITER = 20;
#elif defined(MI_UBSAN) || defined(MI_GUARDED)
static int ITER = 20;
#elif !defined(NDEBUG)
static int ITER = 50;
#else
static int ITER = 400;
#endif

static int failed = 0;
#define EXPECT(what, cond)  do { if (!(cond)) { fprintf(stderr, "\n  FAILED %s: %s (%s:%d)\n", what, #cond, __FILE__, __LINE__); failed++; } } while(0)

typedef void (thread_entry_fun_t)(intptr_t tid);
static void  run_os_threads(size_t nthreads, thread_entry_fun_t* entry);
typedef struct thread_s thread_t;
static thread_t* thread_start(thread_entry_fun_t* entry, intptr_t arg);
static void  thread_join(thread_t* t);
static void* atomic_exchange_ptr(volatile void** p, void* newval);
static long  atomic_load_long(volatile long* p);
static void  atomic_store_long(volatile long* p, long x);
static long  atomic_add_long(volatile long* p, long x);
static void  sleep_millis(long ms);
static void  yield(void);

static void progress(int n) { if ((n % 16) == 0) { fprintf(stderr, "."); fflush(stderr); } }


/* -----------------------------------------------------------
  Churn: threads that allocate page sized and multi-slice blocks in heaps of
  their own, fill them, check them and free them again, so that arena slices
  released by anyone else are reused right away and a stray write shows.
----------------------------------------------------------- */

#define CHURN_THREADS 3
static volatile long churn_stop;
static volatile long churn_corrupt;

static void churn(intptr_t tid) {
  const size_t sizes[] = { 48*1024, 200*1024, 1000*1024, 8, 4096 };
  uint8_t pat = (uint8_t)(0xC0 + tid);
  while (atomic_load_long(&churn_stop) == 0) {
    mi_heap_t* heap = mi_heap_new();
    void* blocks[24];
    size_t bsize[24];
    for (int i = 0; i < 24; i++) {
      bsize[i] = sizes[i % 5];
      blocks[i] = mi_heap_malloc(heap, bsize[i]);
      memset(blocks[i], pat, bsize[i]);
    }
    yield();
    for (int i = 0; i < 24; i++) {
      const uint8_t* p = (const uint8_t*)blocks[i];
      for (size_t k = 0; k < bsize[i]; k += 8) {
        if (p[k] != pat) { atomic_add_long(&churn_corrupt, 1); fprintf(stderr, "\n  churn: block %p+%zu is 0x%02x, expected 0x%02x\n", (void*)p, k, p[k], pat); break; }
      }
      mi_free(blocks[i]);
    }
    if (tid & 1) { mi_heap_delete(heap); } else { mi_heap_destroy(heap); }
    pat++;
  }
}


/* -----------------------------------------------------------
  pin: deterministic, needs the debug hook in `mi_heap_visit_page_claim`
----------------------------------------------------------- */
#if !defined(NDEBUG)
#ifdef __cplusplus
#include <atomic>
extern "C" std::atomic<uintptr_t> mi_debug_stall_in_heap_delete_claim;
#else
#include <stdatomic.h>
extern _Atomic(uintptr_t) mi_debug_stall_in_heap_delete_claim;
#endif

static volatile void* pin_heap;
static volatile void* pin_block;
static volatile long  pin_free_go;
static volatile long  pin_free_done;

static void pin_allocator(intptr_t tid) {
  (void)tid;
  pin_block = mi_heap_malloc((mi_heap_t*)pin_heap, 100);   // the only block in its page
  // exits: the page is abandoned
}

static void pin_deleter(intptr_t tid) {
  (void)tid;
  mi_debug_stall_in_heap_delete_claim = 1;
  mi_heap_delete((mi_heap_t*)pin_heap);
}

static void pin_freer(intptr_t tid) {
  (void)tid;
  while (atomic_load_long(&pin_free_go) == 0) { yield(); }
  mi_free((void*)pin_block);   // empties the page: unabandon and release it .. once the deleter lets go
  atomic_store_long(&pin_free_done, 1);
}

static void test_pin(void) {
  for (int n = 0; n < 4; n++) {
    pin_heap = mi_heap_new();
    run_os_threads(1, &pin_allocator);
    atomic_store_long(&pin_free_go, 0);
    atomic_store_long(&pin_free_done, 0);
    atomic_store_long(&churn_stop, 0);
    thread_t* d = thread_start(&pin_deleter, 0);
    while (mi_debug_stall_in_heap_delete_claim != 2) { yield(); }   // the deleter has the page pinned
    thread_t* f = thread_start(&pin_freer, 0);
    thread_t* c = thread_start(&churn, n);
    atomic_store_long(&pin_free_go, 1);
    sleep_millis(100);
    EXPECT("pin", atomic_load_long(&pin_free_done) == 0);   // the free cannot release a pinned page
    mi_debug_stall_in_heap_delete_claim = 0;               // let the deleter claim: it finds the page owned, unpins, and retries until it is gone
    thread_join(f);
    thread_join(d);
    atomic_store_long(&churn_stop, 1);
    thread_join(c);
    EXPECT("pin", atomic_load_long(&pin_free_done) == 1);
    EXPECT("pin", atomic_load_long(&churn_corrupt) == 0);
  }
}
#endif


/* -----------------------------------------------------------
  page-churn: every free during the delete empties a page
----------------------------------------------------------- */

#define PC_PAGES   64
#define PC_FREERS  3
static volatile void* pc_heap;
static volatile void* pc_blocks[PC_PAGES];
static volatile long  pc_go;

static void pc_allocator(intptr_t tid) {
  (void)tid;
  // one block per page: 40 KiB blocks each get a 64 KiB page of their own
  for (int i = 0; i < PC_PAGES; i++) { pc_blocks[i] = mi_heap_malloc((mi_heap_t*)pc_heap, 40*1024); }
}

static void pc_worker(intptr_t tid) {
  while (atomic_load_long(&pc_go) == 0) { yield(); }
  if (tid == 0) {
    mi_heap_delete((mi_heap_t*)pc_heap);
  }
  else {
    for (int i = (int)tid - 1; i < PC_PAGES; i += PC_FREERS) {
      void* p = atomic_exchange_ptr(&pc_blocks[i], NULL);
      if (p != NULL) mi_free(p);
    }
  }
}

static void test_page_churn(void) {
  atomic_store_long(&churn_stop, 0);
  thread_t* c[CHURN_THREADS];
  for (int i = 0; i < CHURN_THREADS; i++) { c[i] = thread_start(&churn, i); }
  for (int n = 0; n < ITER; n++) {
    pc_heap = mi_heap_new();
    if (n & 1) { run_os_threads(1, &pc_allocator); }   // abandoned pages
          else { pc_allocator(0); }                     // pages held by this thread's theap, which is foreign to the deleting thread
    atomic_store_long(&pc_go, 0);
    thread_t* w[PC_FREERS + 1];
    for (int i = 0; i <= PC_FREERS; i++) { w[i] = thread_start(&pc_worker, i); }
    atomic_store_long(&pc_go, 1);
    for (int i = 0; i <= PC_FREERS; i++) { thread_join(w[i]); }
    for (int i = 0; i < PC_PAGES; i++) {
      void* p = atomic_exchange_ptr(&pc_blocks[i], NULL);
      if (p != NULL) mi_free(p);
    }
    progress(n);
  }
  atomic_store_long(&churn_stop, 1);
  for (int i = 0; i < CHURN_THREADS; i++) { thread_join(c[i]); }
  EXPECT("page-churn", atomic_load_long(&churn_corrupt) == 0);
}


/* -----------------------------------------------------------
  os-pages
----------------------------------------------------------- */

#define OS_BLOCKS 4
static volatile void* os_heap;
static volatile void* os_blocks[OS_BLOCKS];
static volatile long  os_go;

static void* os_alloc(mi_heap_t* heap, size_t i) {
  // aligned beyond MI_PAGE_MAX_OVERALLOC_ALIGN: allocated from the OS in a page of its own
  void* p = mi_heap_malloc_aligned(heap, 3000 + 1000*i, 4*1024*1024);
  memset(p, (int)i + 1, 3000 + 1000*i);
  return p;
}

static void os_check_free(void* p, size_t i) {
  EXPECT("os-pages", p != NULL && ((uintptr_t)p % (4*1024*1024)) == 0);
  EXPECT("os-pages", ((uint8_t*)p)[0] == (uint8_t)(i+1) && ((uint8_t*)p)[2999 + 1000*i] == (uint8_t)(i+1));
  EXPECT("os-pages", mi_usable_size(p) >= 3000 + 1000*i);
  EXPECT("os-pages", mi_heap_contains(mi_heap_main(), p));
  mi_free(p);
}

static void os_allocator(intptr_t tid) {
  (void)tid;
  for (size_t i = 0; i < OS_BLOCKS; i++) { os_blocks[i] = os_alloc((mi_heap_t*)os_heap, i); }
}

static void os_worker(intptr_t tid) {
  while (atomic_load_long(&os_go) == 0) { yield(); }
  if (tid == 0) {
    mi_heap_delete((mi_heap_t*)os_heap);
  }
  else {
    for (size_t i = (size_t)tid - 1; i < OS_BLOCKS; i += 2) {
      void* p = atomic_exchange_ptr(&os_blocks[i], NULL);
      ((uint8_t*)p)[1] = 0;
      mi_free(p);
    }
  }
}

static void test_os_pages(void) {
  // held by the deleting thread's own theap, freed after the delete
  for (int n = 0; n < 4; n++) {
    mi_heap_t* heap = mi_heap_new();
    for (size_t i = 0; i < OS_BLOCKS; i++) { os_blocks[i] = os_alloc(heap, i); }
    mi_heap_delete(heap);
    for (size_t i = 0; i < OS_BLOCKS; i++) { os_check_free((void*)os_blocks[i], i); }
    mi_collect(true);
  }
  // held by the deleting thread's own theap, destroyed
  for (int n = 0; n < 4; n++) {
    mi_heap_t* heap = mi_heap_new();
    for (size_t i = 0; i < OS_BLOCKS; i++) { os_blocks[i] = os_alloc(heap, i); }
    mi_heap_destroy(heap);
    mi_collect(true);
  }
  // abandoned by an exited thread, freed after the delete
  for (int n = 0; n < 4; n++) {
    os_heap = mi_heap_new();
    run_os_threads(1, &os_allocator);
    mi_heap_delete((mi_heap_t*)os_heap);
    for (size_t i = 0; i < OS_BLOCKS; i++) { os_check_free((void*)os_blocks[i], i); }
    mi_collect(true);
  }
  // abandoned by an exited thread, freed by other threads during the delete
  for (int n = 0; n < ITER; n++) {
    os_heap = mi_heap_new();
    run_os_threads(1, &os_allocator);
    atomic_store_long(&os_go, 0);
    thread_t* w[3];
    for (int i = 0; i < 3; i++) { w[i] = thread_start(&os_worker, i); }
    atomic_store_long(&os_go, 1);
    for (int i = 0; i < 3; i++) { thread_join(w[i]); }
    mi_collect(true);
    progress(n);
  }
}


/* -----------------------------------------------------------
  foreign-theap: thread B allocated from the heap and is idle (holds pages in
  its theap) while A deletes it; B frees its blocks afterwards and exits.
----------------------------------------------------------- */

#define FT_BLOCKS 512
static volatile void* ft_heap;
static volatile long  ft_state;   // 0: B allocates, 1: B done allocating, 2: heap deleted, B frees

static void ft_b(intptr_t tid) {
  (void)tid;
  void* blocks[FT_BLOCKS];
  mi_heap_t* heap = (mi_heap_t*)ft_heap;
  for (int i = 0; i < FT_BLOCKS; i++) { blocks[i] = mi_heap_malloc(heap, (i % 7 == 0 ? 2000 : 48)); memset(blocks[i], 0x5A, 48); }
  for (int i = 0; i < FT_BLOCKS; i += 3) { mi_free(blocks[i]); blocks[i] = NULL; }   // some local free lists are non-empty
  void* big = mi_heap_malloc_aligned(heap, 5000, 4*1024*1024);   // and an OS page held by B's theap
  atomic_store_long(&ft_state, 1);
  while (atomic_load_long(&ft_state) != 2) { yield(); }
  // the heap is gone; the blocks now belong to the main heap
  for (int i = 0; i < FT_BLOCKS; i++) {
    if (blocks[i] != NULL) {
      EXPECT("foreign-theap", ((uint8_t*)blocks[i])[0] == 0x5A && ((uint8_t*)blocks[i])[47] == 0x5A);
      EXPECT("foreign-theap", mi_heap_contains(mi_heap_main(), blocks[i]));
      mi_free(blocks[i]);
    }
  }
  EXPECT("foreign-theap", mi_heap_contains(mi_heap_main(), big));
  mi_free(big);
  // a new heap, quite possibly at the address of the deleted one: B must get a fresh theap for it
  mi_heap_t* heap2 = mi_heap_new();
  for (int i = 0; i < 64; i++) {
    void* p = mi_heap_malloc(heap2, 48);
    EXPECT("foreign-theap", mi_heap_contains(heap2, p));
    mi_free(p);
  }
  mi_heap_delete(heap2);
  // and B exits with a detached theap for the deleted heap still in its cache/slot
}

static void ft_deleter(intptr_t tid) { (void)tid; mi_heap_delete((mi_heap_t*)ft_heap); }

static void test_foreign_theap(void) {
  for (int n = 0; n < ITER; n++) {
    ft_heap = mi_heap_new();
    atomic_store_long(&ft_state, 0);
    thread_t* b = thread_start(&ft_b, 0);
    while (atomic_load_long(&ft_state) != 1) { yield(); }
    void* mine = mi_heap_malloc((mi_heap_t*)ft_heap, 48);   // the deleter holds a page of it too
    if (n & 1) { mi_heap_delete((mi_heap_t*)ft_heap); }
    else       { run_os_threads(1, &ft_deleter); }          // from a third thread, so that both this thread and B are foreign
    EXPECT("foreign-theap", mi_heap_contains(mi_heap_main(), mine));
    mi_free(mine);
    atomic_store_long(&ft_state, 2);
    thread_join(b);
    mi_collect(true);
    progress(n);
  }
}


/* -----------------------------------------------------------
  two-deletes
----------------------------------------------------------- */

#define TD_N 2048
static volatile void* td_heap[2];
static volatile void* td_blocks[2][TD_N];
static volatile long  td_go;

static void td_worker(intptr_t tid) {
  while (atomic_load_long(&td_go) == 0) { yield(); }
  if (tid < 2) {
    mi_heap_delete((mi_heap_t*)td_heap[tid]);
  }
  else {
    for (int i = (int)tid - 2; i < TD_N; i += 4) {
      for (int h = 0; h < 2; h++) {
        void* p = atomic_exchange_ptr(&td_blocks[h][i], NULL);
        if (p != NULL) mi_free(p);
      }
    }
  }
}

static void td_allocator(intptr_t tid) {
  for (int i = 0; i < TD_N; i++) { td_blocks[tid][i] = mi_heap_malloc((mi_heap_t*)td_heap[tid], (i % 5 == 0 ? 640 : 24)); }
}

static void test_two_deletes(void) {
  for (int n = 0; n < ITER; n++) {
    td_heap[0] = mi_heap_new();
    td_heap[1] = mi_heap_new();
    run_os_threads(2, &td_allocator);
    atomic_store_long(&td_go, 0);
    thread_t* w[6];
    for (int i = 0; i < 6; i++) { w[i] = thread_start(&td_worker, i); }
    atomic_store_long(&td_go, 1);
    for (int i = 0; i < 6; i++) { thread_join(w[i]); }
    for (int h = 0; h < 2; h++) for (int i = 0; i < TD_N; i++) {
      void* p = atomic_exchange_ptr(&td_blocks[h][i], NULL);
      if (p != NULL) mi_free(p);
    }
    progress(n);
  }
}


/* -----------------------------------------------------------
  parked: B used the heap and parks; the scavenger may be sweeping B's theaps
  (including the one for this heap) when the delete detaches them.

  #if 0 // Phase 7 (issue #272): needs mi_on_thread_idle_start/_end and the
  background scavenger, neither of which exists in this tree yet.
----------------------------------------------------------- */

#if 0 // Phase 7

static volatile void* pk_heap;
static volatile long  pk_state;

static void pk_b(intptr_t tid) {
  (void)tid;
  void* blocks[256];
  for (int r = 0; atomic_load_long(&pk_state) < 2; r++) {
    mi_heap_t* heap = (mi_heap_t*)pk_heap;
    if (atomic_load_long(&pk_state) == 0) {
      for (int i = 0; i < 256; i++) { blocks[i] = mi_heap_malloc(heap, 32 + (i % 3) * 400); }
      for (int i = 0; i < 256; i += 2) { mi_free(blocks[i]); blocks[i] = NULL; }   // holes to sweep
      atomic_store_long(&pk_state, 1);
    }
    const bool parked = mi_on_thread_idle_start();
    sleep_millis(1);
    if (parked) { mi_on_thread_idle_end(); }
  }
  for (int i = 1; i < 256; i += 2) { mi_free(blocks[i]); }
}

static void test_parked(void) {
  for (int n = 0; n < ITER/2 + 1; n++) {
    pk_heap = mi_heap_new();
    atomic_store_long(&pk_state, 0);
    thread_t* b = thread_start(&pk_b, 0);
    while (atomic_load_long(&pk_state) != 1) { yield(); }
    sleep_millis(n % 4);
    mi_heap_delete((mi_heap_t*)pk_heap);
    atomic_store_long(&pk_state, 2);
    thread_join(b);
    progress(n);
  }
}

#endif // Phase 7


/* -----------------------------------------------------------
   Main
----------------------------------------------------------- */

int main(int argc, char** argv) {
  if (argc >= 2) {
    char* end;
    long n = strtol(argv[1], &end, 10);
    if (n > 0) ITER = (int)n;
  }
  fprintf(stderr, "Using %d iterations\n", ITER);

  #if !defined(NDEBUG)
  fprintf(stderr, "test: pin...  ");            test_pin();           fprintf(stderr, " %s.\n", failed ? "FAILED" : "ok");
  #endif
  fprintf(stderr, "test: os-pages...  ");       test_os_pages();      fprintf(stderr, " %s.\n", failed ? "FAILED" : "ok");
  fprintf(stderr, "test: foreign-theap...  ");  test_foreign_theap(); fprintf(stderr, " %s.\n", failed ? "FAILED" : "ok");
  fprintf(stderr, "test: page-churn...  ");     test_page_churn();    fprintf(stderr, " %s.\n", failed ? "FAILED" : "ok");
  fprintf(stderr, "test: two-deletes...  ");    test_two_deletes();   fprintf(stderr, " %s.\n", failed ? "FAILED" : "ok");
  #if 0 // Phase 7 (issue #272): needs mi_on_thread_idle_start/_end and the background scavenger
  fprintf(stderr, "test: parked...  ");         test_parked();        fprintf(stderr, " %s.\n", failed ? "FAILED" : "ok");
  #endif

  mi_collect(true);
  mi_stats_print(NULL);
  return (failed > 0 ? 1 : 0);
}


/* -----------------------------------------------------------
   Portable threading / atomics (mirrors test-stress.c)
----------------------------------------------------------- */

#ifdef _WIN32

#include <windows.h>

static thread_entry_fun_t* thread_entry_fun;
static DWORD WINAPI thread_entry(LPVOID param) {
  thread_entry_fun((intptr_t)param);
  return 0;
}

static void run_os_threads(size_t nthreads, thread_entry_fun_t* fun) {
  thread_entry_fun = fun;
  HANDLE* thandles = (HANDLE*)calloc(nthreads, sizeof(HANDLE));
  for (size_t i = 0; i < nthreads; i++) { thandles[i] = CreateThread(0, 8*1024L, &thread_entry, (void*)(i), 0, NULL); }
  for (size_t i = 0; i < nthreads; i++) { WaitForSingleObject(thandles[i], INFINITE); CloseHandle(thandles[i]); }
  free(thandles);
}

struct thread_s { HANDLE h; thread_entry_fun_t* fun; intptr_t arg; };
static DWORD WINAPI thread_start_entry(LPVOID param) { thread_t* t = (thread_t*)param; t->fun(t->arg); return 0; }
static thread_t* thread_start(thread_entry_fun_t* fun, intptr_t arg) {
  thread_t* t = (thread_t*)calloc(1,sizeof(thread_t)); t->fun = fun; t->arg = arg;
  t->h = CreateThread(0, 8*1024L, &thread_start_entry, t, 0, NULL);
  return t;
}
static void thread_join(thread_t* t) { WaitForSingleObject(t->h, INFINITE); CloseHandle(t->h); free(t); }

static void* atomic_exchange_ptr(volatile void** p, void* newval) {
  #if (INTPTR_MAX == INT32_MAX)
  return (void*)InterlockedExchange((volatile LONG*)p, (LONG)newval);
  #else
  return (void*)InterlockedExchange64((volatile LONG64*)p, (LONG64)newval);
  #endif
}
static long atomic_load_long(volatile long* p)         { return InterlockedCompareExchange(p, 0, 0); }
static void atomic_store_long(volatile long* p, long x) { InterlockedExchange(p, x); }
static long atomic_add_long(volatile long* p, long x)   { return InterlockedExchangeAdd(p, x); }
static void sleep_millis(long ms) { Sleep((DWORD)ms); }
static void yield(void) { YieldProcessor(); Sleep(0); }

#else

#include <pthread.h>
#include <sched.h>
#include <time.h>

static thread_entry_fun_t* thread_entry_fun;
static void* thread_entry(void* param) {
  thread_entry_fun((intptr_t)param);
  return NULL;
}

static void run_os_threads(size_t nthreads, thread_entry_fun_t* fun) {
  thread_entry_fun = fun;
  pthread_t* threads = (pthread_t*)calloc(nthreads, sizeof(pthread_t));
  for (size_t i = 0; i < nthreads; i++) { pthread_create(&threads[i], NULL, &thread_entry, (void*)i); }
  for (size_t i = 0; i < nthreads; i++) { pthread_join(threads[i], NULL); }
  free(threads);
}

struct thread_s { pthread_t h; thread_entry_fun_t* fun; intptr_t arg; };
static void* thread_start_entry(void* param) { thread_t* t = (thread_t*)param; t->fun(t->arg); return NULL; }
static thread_t* thread_start(thread_entry_fun_t* fun, intptr_t arg) {
  thread_t* t = (thread_t*)calloc(1,sizeof(thread_t)); t->fun = fun; t->arg = arg;
  pthread_create(&t->h, NULL, &thread_start_entry, t);
  return t;
}
static void thread_join(thread_t* t) { pthread_join(t->h, NULL); free(t); }

static void sleep_millis(long ms) {
  if (ms <= 0) return;
  struct timespec ts; ts.tv_sec = ms / 1000; ts.tv_nsec = (ms % 1000) * 1000000L;
  nanosleep(&ts, NULL);
}
static void yield(void) { sched_yield(); }

#ifdef __cplusplus
#include <atomic>
static void* atomic_exchange_ptr(volatile void** p, void* newval) { return std::atomic_exchange((volatile std::atomic<void*>*)p, newval); }
static long  atomic_load_long(volatile long* p)                  { return std::atomic_load((volatile std::atomic<long>*)p); }
static void  atomic_store_long(volatile long* p, long x)         { std::atomic_store((volatile std::atomic<long>*)p, x); }
static long  atomic_add_long(volatile long* p, long x)           { return std::atomic_fetch_add((volatile std::atomic<long>*)p, x); }
#else
#include <stdatomic.h>
static void* atomic_exchange_ptr(volatile void** p, void* newval) { return atomic_exchange((volatile _Atomic(void*)*)p, newval); }
static long  atomic_load_long(volatile long* p)                  { return atomic_load((volatile _Atomic(long)*)p); }
static void  atomic_store_long(volatile long* p, long x)         { atomic_store((volatile _Atomic(long)*)p, x); }
static long  atomic_add_long(volatile long* p, long x)           { return atomic_fetch_add((volatile _Atomic(long)*)p, x); }
#endif

#endif
