/* ----------------------------------------------------------------------------
Copyright (c) 2018-2025 Microsoft Research, Daan Leijen
This is free software; you can redistribute it and/or modify it under the
terms of the MIT license.
-----------------------------------------------------------------------------*/

// imported from oven-sh/mimalloc @ 942b8342, MIT (issue #271 / Bun parity P6).

/* Multi-threaded heap lifecycle stress tests.

   These exercise concurrent `mi_heap_new` / `mi_heap_delete` / `mi_heap_destroy`
   against `mi_free` and against each other. In v3, heaps are first-class and
   can be used from any thread; these tests check that the documented patterns
   are race-free under contention.

   test 1: `mi_free` from worker threads concurrent with `mi_heap_delete`
           on the producer thread (blocks survive delete and are freed "later
           on as usual" per the API docs). Three variants: the delete after the
           frees, the delete overlapping the frees, and the delete overlapping
           frees into abandoned pages by threads that used the heap themselves.

   test 2: thread A creates heap H1 and allocates once, then creates heap H2
           and allocates, while thread B concurrently destroys H1. A never
           touches H1 after publishing it, so this is within contract.

   > mimalloc-test-heap-mt [ITER]
*/

#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>

#include "mimalloc.h"

// argument defaults
#if defined(MI_TSAN)
static int ITER = 200;
#elif defined(MI_UBSAN) || defined(MI_GUARDED)
static int ITER = 200;
#else
static int ITER = 1000;   // (a 3-core CI VM runs ~10 iterations/s; pass a count on the command line for more)
#endif

#define custom_calloc(n,s)    calloc(n,s)
#define custom_free(p)        free(p)

typedef void (thread_entry_fun_t)(intptr_t tid);
static thread_entry_fun_t* thread_entry_fun;
static void run_os_threads(size_t nthreads, thread_entry_fun_t* entry);
static void* atomic_exchange_ptr(volatile void** p, void* newval);
static long  atomic_load_long(volatile long* p);
static void  atomic_store_long(volatile long* p, long x);


/* -----------------------------------------------------------
   Test 1: mi_free concurrent with mi_heap_delete
----------------------------------------------------------- */

#define T1_FREERS  4
#define T1_NPTRS   4096

static volatile void* t1_ptrs[T1_NPTRS];
static volatile long  t1_go;

static void t1_freer(intptr_t tid) {
  while (atomic_load_long(&t1_go) == 0) { /* spin */ }
  for (intptr_t i = tid; i < T1_NPTRS; i += T1_FREERS) {
    void* p = atomic_exchange_ptr(&t1_ptrs[i], NULL);
    if (p != NULL) mi_free(p);
  }
}

static void test_free_during_delete(void) {
  for (int n = 0; n < ITER; n++) {
    mi_heap_t* heap = mi_heap_new();
    void* big = mi_heap_malloc(heap, 16*1024);
    memset(big, 0xA5, 16*1024);
    for (int i = 0; i < T1_NPTRS; i++) {
      t1_ptrs[i] = mi_heap_malloc(heap, 32);
    }
    mi_free(big);

    atomic_store_long(&t1_go, 0);
    // start freers, then release them and delete the heap concurrently
    // (run_os_threads joins, so the delete happens on the main thread
    //  while workers free)
    thread_entry_fun = &t1_freer;
    atomic_store_long(&t1_go, 1);     // release before spawn so workers run immediately
    run_os_threads(T1_FREERS, &t1_freer);
    // workers have joined; any remaining ptrs were not yet picked up
    for (int i = 0; i < T1_NPTRS; i++) {
      void* p = atomic_exchange_ptr(&t1_ptrs[i], NULL);
      if (p != NULL) mi_free(p);
    }
    mi_heap_delete(heap);
    if ((n % 64) == 0) { fprintf(stderr, "."); fflush(stderr); }
  }
}

// Variant where the main thread participates as the deleter so the
// delete genuinely overlaps the worker frees.
static volatile void* t1_heap;

static void t1_worker(intptr_t tid) {
  if (tid == 0) {
    mi_heap_t* heap = (mi_heap_t*)atomic_exchange_ptr(&t1_heap, NULL);
    mi_heap_delete(heap);
  }
  else {
    t1_freer(tid - 1);
  }
}

static void test_free_during_delete_overlap(void) {
  for (int n = 0; n < ITER; n++) {
    mi_heap_t* heap = mi_heap_new();
    void* big = mi_heap_malloc(heap, 16*1024);
    for (int i = 0; i < T1_NPTRS; i++) { t1_ptrs[i] = mi_heap_malloc(heap, 32); }
    mi_free(big);
    t1_heap = heap;
    atomic_store_long(&t1_go, 1);
    run_os_threads(T1_FREERS + 1, &t1_worker);
    for (int i = 0; i < T1_NPTRS; i++) {
      void* p = atomic_exchange_ptr(&t1_ptrs[i], NULL);
      if (p != NULL) mi_free(p);
    }
    if ((n % 64) == 0) { fprintf(stderr, "."); fflush(stderr); }
  }
}

// Variant where the pages are abandoned when the delete starts, and where every
// freer has used the heap before:
//  - the blocks are allocated on a thread that exits before the frees start, so
//    its theap abandons the pages. In the two tests above the pages are still held
//    by a theap, and the delete moves those without contention; abandoned pages
//    are claimed by every `mi_free` into them (`mi_free_try_collect_mt`) and by
//    the delete, so here both run the ownership protocol against each other.
//  - every freer allocates from the heap once before it starts, so its thread
//    local slot for the heap refers to a theap that the delete detaches and frees
//    while the freer is still freeing (`_mi_page_associated_theap_peek` on the
//    re-abandon path of `mi_free_try_collect_mt`).
//  Reclaiming on free is disabled for this test: a freer that reclaims a page
//  holds it like an allocating thread does, and a thread that holds pages of a heap
//  must not free into them while another thread deletes the heap (the delete moves
//  a held page without waiting for its thread).
static volatile long t1_ready[T1_FREERS];

static void t1_allocator(intptr_t tid) {
  (void)tid;
  mi_heap_t* heap = (mi_heap_t*)t1_heap;
  for (int i = 0; i < T1_NPTRS; i++) { t1_ptrs[i] = mi_heap_malloc(heap, 32); }
  // the thread exits here and abandons the pages it allocated
}

static void t1_abandoned_worker(intptr_t tid) {
  if (tid == 0) {
    for (int i = 0; i < T1_FREERS; i++) {
      while (atomic_load_long(&t1_ready[i]) == 0) { /* spin */ }
    }
    mi_heap_t* heap = (mi_heap_t*)atomic_exchange_ptr(&t1_heap, NULL);
    atomic_store_long(&t1_go, 1);     // release the freers together with the delete
    mi_heap_delete(heap);
  }
  else {
    // a different size class than the blocks under test, so this does not reclaim one of their pages
    mi_free(mi_heap_malloc((mi_heap_t*)t1_heap, 128));
    atomic_store_long(&t1_ready[tid - 1], 1);
    t1_freer(tid - 1);
  }
}

static void test_free_during_delete_abandoned(void) {
  const long reclaim_on_free = mi_option_get(mi_option_page_reclaim_on_free);
  mi_option_set(mi_option_page_reclaim_on_free, -1);
  for (int n = 0; n < ITER; n++) {
    mi_heap_t* heap = mi_heap_new();
    t1_heap = heap;
    run_os_threads(1, &t1_allocator);
    for (int i = 0; i < T1_FREERS; i++) { atomic_store_long(&t1_ready[i], 0); }
    atomic_store_long(&t1_go, 0);
    run_os_threads(T1_FREERS + 1, &t1_abandoned_worker);
    for (int i = 0; i < T1_NPTRS; i++) {
      void* p = atomic_exchange_ptr(&t1_ptrs[i], NULL);
      if (p != NULL) mi_free(p);
    }
    if ((n % 64) == 0) { fprintf(stderr, "."); fflush(stderr); }
  }
  mi_option_set(mi_option_page_reclaim_on_free, reclaim_on_free);
}


/* -----------------------------------------------------------
   Test 2: mi_heap_new (on A) concurrent with mi_heap_destroy (on B)
   of a heap that A previously used.
----------------------------------------------------------- */

static volatile void* t2_h1;
static volatile long  t2_go;

static void t2_worker(intptr_t tid) {
  if (tid == 0) {
    // thread A
    for (int n = 0; n < ITER; n++) {
      mi_heap_t* h1 = mi_heap_new();
      mi_free(mi_heap_malloc(h1, 32));    // ensure A has a theap for h1 on its tld
      t2_h1 = h1;
      atomic_store_long(&t2_go, 1);
      // create a second heap; its theap-init reads tld->theaps which B may be mutating
      mi_heap_t* h2 = mi_heap_new();
      mi_free(mi_heap_malloc(h2, 32));
      while (atomic_load_long(&t2_go) != 0) { /* wait for B */ }
      mi_heap_destroy(h2);
    }
  }
  else {
    // thread B
    for (int n = 0; n < ITER; n++) {
      while (atomic_load_long(&t2_go) == 0) { /* spin */ }
      mi_heap_t* h1 = (mi_heap_t*)atomic_exchange_ptr(&t2_h1, NULL);
      mi_heap_destroy(h1);
      atomic_store_long(&t2_go, 0);
      if ((n % 256) == 0) { fprintf(stderr, "."); fflush(stderr); }
    }
  }
}

static void test_new_during_destroy(void) {
  t2_h1 = NULL;
  atomic_store_long(&t2_go, 0);
  run_os_threads(2, &t2_worker);
}


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

  fprintf(stderr, "test: heap-free-during-delete...  ");
  test_free_during_delete();
  fprintf(stderr, " ok.\n");

  fprintf(stderr, "test: heap-free-during-delete-overlap...  ");
  test_free_during_delete_overlap();
  fprintf(stderr, " ok.\n");

  fprintf(stderr, "test: heap-free-during-delete-abandoned...  ");
  test_free_during_delete_abandoned();
  fprintf(stderr, " ok.\n");

  fprintf(stderr, "test: heap-new-during-destroy...  ");
  test_new_during_destroy();
  fprintf(stderr, " ok.\n");

  #ifndef USE_STD_MALLOC
  mi_collect(true);
  mi_stats_print(NULL);
  #endif
  return 0;
}


/* -----------------------------------------------------------
   Portable threading / atomics (mirrors test-stress.c)
----------------------------------------------------------- */

#ifdef _WIN32

#include <windows.h>

static DWORD WINAPI thread_entry(LPVOID param) {
  thread_entry_fun((intptr_t)param);
  return 0;
}

static void run_os_threads(size_t nthreads, thread_entry_fun_t* fun) {
  thread_entry_fun = fun;
  DWORD*  tids     = (DWORD*) custom_calloc(nthreads, sizeof(DWORD));
  HANDLE* thandles = (HANDLE*)custom_calloc(nthreads, sizeof(HANDLE));
  for (size_t i = 0; i < nthreads; i++) {
    thandles[i] = CreateThread(0, 8*1024L, &thread_entry, (void*)(i), 0, &tids[i]);
  }
  for (size_t i = 0; i < nthreads; i++) {
    WaitForSingleObject(thandles[i], INFINITE);
  }
  for (size_t i = 0; i < nthreads; i++) {
    CloseHandle(thandles[i]);
  }
  custom_free(tids);
  custom_free(thandles);
}

static void* atomic_exchange_ptr(volatile void** p, void* newval) {
  #if (INTPTR_MAX == INT32_MAX)
  return (void*)InterlockedExchange((volatile LONG*)p, (LONG)newval);
  #else
  return (void*)InterlockedExchange64((volatile LONG64*)p, (LONG64)newval);
  #endif
}
static long atomic_load_long(volatile long* p) {
  return InterlockedCompareExchange(p, 0, 0);
}
static void atomic_store_long(volatile long* p, long x) {
  InterlockedExchange(p, x);
}

#else

#include <pthread.h>

static void* thread_entry(void* param) {
  thread_entry_fun((intptr_t)param);
  return NULL;
}

static void run_os_threads(size_t nthreads, thread_entry_fun_t* fun) {
  thread_entry_fun = fun;
  pthread_t* threads = (pthread_t*)custom_calloc(nthreads, sizeof(pthread_t));
  for (size_t i = 0; i < nthreads; i++) {
    pthread_create(&threads[i], NULL, &thread_entry, (void*)i);
  }
  for (size_t i = 0; i < nthreads; i++) {
    pthread_join(threads[i], NULL);
  }
  custom_free(threads);
}

#ifdef __cplusplus
#include <atomic>
static void* atomic_exchange_ptr(volatile void** p, void* newval) {
  return std::atomic_exchange((volatile std::atomic<void*>*)p, newval);
}
static long atomic_load_long(volatile long* p) {
  return std::atomic_load((volatile std::atomic<long>*)p);
}
static void atomic_store_long(volatile long* p, long x) {
  std::atomic_store((volatile std::atomic<long>*)p, x);
}
#else
#include <stdatomic.h>
static void* atomic_exchange_ptr(volatile void** p, void* newval) {
  return atomic_exchange((volatile _Atomic(void*)*)p, newval);
}
static long atomic_load_long(volatile long* p) {
  return atomic_load((volatile _Atomic(long)*)p);
}
static void atomic_store_long(volatile long* p, long x) {
  atomic_store((volatile _Atomic(long)*)p, x);
}
#endif

#endif
