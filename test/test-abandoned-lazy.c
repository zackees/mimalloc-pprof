/* ----------------------------------------------------------------------------
Copyright (c) 2018-2026, Microsoft Research, Daan Leijen
This is free software; you can redistribute it and/or modify it under the
terms of the MIT license.
-----------------------------------------------------------------------------*/

// ported from oven-sh/mimalloc @ 787be2a8, MIT (Bun parity P10b, issue #317). The counter
// gating and thread-helper set (`run_os_threads`, `atomic_*`) are adapted, not copied
// verbatim -- see the notes at each spot below.

/* The per-bin abandoned-page bitmaps of a (heap, arena) pair are allocated the
   first time a page of that bin is abandoned, not when the heap first touches
   the arena (`mi_arena_pages_abandoned_ensure`, src/arena.c). This drives every
   path that reads or writes them:

   - worker threads allocate blocks of several size classes from a shared heap
     and exit while the blocks are still live, so their pages are abandoned
     (the bitmaps are allocated on that path, by several threads at once);
   - the main thread then allocates the same sizes from the heap, which reclaims
     the abandoned pages, frees every block (un-abandon, page free), collects
     and deletes the heap (the bitmaps are freed with it, `_mi_arena_pages_free`);
   - the same with the main heap, whose bitmaps are never freed, and with blocks
     freed by a thread that never allocated (re-abandon of a mostly free page).

   A heap that is deleted or destroyed sets `heap->releasing` and abandons all of
   its pages unmapped during its own teardown, which claims them back through the
   `pages` bitmap instead (see `mi_heap_detach_theaps`, src/heap.c). Those pages
   never go into a per-bin bitmap, so a heap that lives only to be released never
   allocates one:

   - a debug build counts the maps allocated (`mi_debug_abandoned_maps_allocated`,
     src/arena.c) while heaps are created, used and destroyed on one thread, and
     the count must not move;
   - several threads create, use and delete heaps while other threads free the
     blocks of each heap during its delete (a free that would otherwise re-map a
     page, but must not while the heap is releasing).

   A debug build checks the bitmap invariants (`mi_assert_internal`s in arena.c)
   on each of these paths.

   > mimalloc-test-abandoned-lazy [ITER]
*/

#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>

#include "mimalloc.h"

#if defined(MI_TSAN) || defined(MI_UBSAN) || defined(MI_GUARDED)
static int ITER = 20;
#else
static int ITER = 100;
#endif

#define NTHREADS   8
#define NSIZES     6
#define NBLOCKS    64      // per thread per size: enough to span a few pages of each size class

static const size_t sizes[NSIZES] = { 16, 96, 512, 2048, 8192, 40000 };

typedef void (thread_entry_fun_t)(intptr_t tid);
static void run_os_threads(size_t nthreads, thread_entry_fun_t* entry);

static mi_heap_t* shared_heap;           // NULL: use the main heap
static void*      blocks[NTHREADS][NSIZES][NBLOCKS];

static void alloc_and_exit(intptr_t tid) {
  for (int s = 0; s < NSIZES; s++) {
    for (int b = 0; b < NBLOCKS; b++) {
      void* p = (shared_heap != NULL ? mi_heap_malloc(shared_heap, sizes[s]) : mi_malloc(sizes[s]));
      if (p == NULL) { fprintf(stderr, "allocation failed\n"); abort(); }
      memset(p, (int)(tid + s), sizes[s]);
      blocks[tid][s][b] = p;
    }
  }
  // exit with everything live: every page this thread used is abandoned
}

static void free_some(intptr_t tid) {
  // free most blocks of a page from a thread that never allocated from the heap:
  // a multi-threaded free that can re-abandon the page as mapped
  for (int s = 0; s < NSIZES; s++) {
    for (int b = 0; b < NBLOCKS; b++) {
      if ((b % 8) != 0) {
        mi_free(blocks[tid][s][b]);
        blocks[tid][s][b] = NULL;
      }
    }
  }
}

static void check_block(intptr_t tid, int s, void* p) {
  const unsigned char* q = (const unsigned char*)p;
  for (size_t i = 0; i < sizes[s]; i += 64) {
    if (q[i] != (unsigned char)(tid + s)) { fprintf(stderr, "block corrupted\n"); abort(); }
  }
}

static void run_round(bool use_main_heap) {
  shared_heap = (use_main_heap ? NULL : mi_heap_new());
  memset(blocks, 0, sizeof(blocks));

  // 1. abandon pages of several size classes from several threads at once
  run_os_threads(NTHREADS, &alloc_and_exit);

  // 2. reclaim: allocating the same sizes must find the abandoned pages again
  void* mine[NSIZES][NBLOCKS];
  for (int s = 0; s < NSIZES; s++) {
    for (int b = 0; b < NBLOCKS; b++) {
      mine[s][b] = (shared_heap != NULL ? mi_heap_malloc(shared_heap, sizes[s]) : mi_malloc(sizes[s]));
      if (mine[s][b] == NULL) { fprintf(stderr, "allocation failed\n"); abort(); }
    }
  }
  for (intptr_t t = 0; t < NTHREADS; t++) {
    for (int s = 0; s < NSIZES; s++) {
      for (int b = 0; b < NBLOCKS; b++) { check_block(t, s, blocks[t][s][b]); }
    }
  }

  // 3. frees from threads that never touched the heap, then from this thread
  run_os_threads(NTHREADS, &free_some);
  for (intptr_t t = 0; t < NTHREADS; t++) {
    for (int s = 0; s < NSIZES; s++) {
      for (int b = 0; b < NBLOCKS; b++) {
        if (blocks[t][s][b] != NULL) { check_block(t, s, blocks[t][s][b]); mi_free(blocks[t][s][b]); }
      }
    }
  }
  for (int s = 0; s < NSIZES; s++) {
    for (int b = 0; b < NBLOCKS; b++) { mi_free(mine[s][b]); }
  }

  // 4. collect and delete
  if (shared_heap != NULL) {
    mi_heap_collect(shared_heap, true);
    mi_heap_delete(shared_heap);
    shared_heap = NULL;
  }
  else {
    mi_collect(true);
  }
}

/* -----------------------------------------------------------
   Released heaps: no maps
----------------------------------------------------------- */

// Bun gates this on `!defined(NDEBUG)`; we gate on `MI_DEBUG > 0` instead, matching the
// library's own gate on `mi_debug_abandoned_maps_allocated` (internal.h) -- a Release build
// with `-DMI_DEBUG_FULL=ON` sets `MI_DEBUG=3` while `NDEBUG` stays defined by the Release
// build type, and `!defined(NDEBUG)` would then reference a symbol that is not linked in.
// Same reasoning as test-heap-teardown.c's `#if MI_DEBUG == 0 #error` gate.
#if MI_DEBUG > 0
#ifdef __cplusplus
#include <atomic>
extern "C" std::atomic<uintptr_t> mi_debug_abandoned_maps_allocated;
static uintptr_t maps_allocated(void) { return mi_debug_abandoned_maps_allocated.load(); }
#elif defined(_MSC_VER) && !(defined(__STDC_VERSION__) && __STDC_VERSION__ >= 201112L && !defined(__STDC_NO_ATOMICS__))
// MSVC C compilation without `/std:c11 /experimental:c11atomics` (not set here): <stdatomic.h>
// exists but its vcruntime_c11_stdatomic.h #errors "C atomic support is not enabled". Ported
// verbatim from test-heap-teardown.c's own fallback for the same reason: `c-unit.yml`'s
// `ctest (windows-latest)` builds C tests with Microsoft's `cl` in C mode.
extern volatile uintptr_t mi_debug_abandoned_maps_allocated;
static uintptr_t maps_allocated(void) { return mi_debug_abandoned_maps_allocated; }
#else
#include <stdatomic.h>
extern _Atomic(uintptr_t) mi_debug_abandoned_maps_allocated;
static uintptr_t maps_allocated(void) { return atomic_load(&mi_debug_abandoned_maps_allocated); }
#endif
#define HAS_MAP_COUNTER 1
#else
#define HAS_MAP_COUNTER 0
#endif

static void fill_heap(mi_heap_t* heap, void** keep) {
  for (int s = 0; s < NSIZES; s++) {
    for (int b = 0; b < NBLOCKS; b++) {
      void* p = mi_heap_malloc(heap, sizes[s]);
      if (p == NULL) { fprintf(stderr, "allocation failed\n"); abort(); }
      memset(p, s + 1, sizes[s]);
      if (keep != NULL) { keep[s*NBLOCKS + b] = p; }
    }
  }
}

static void released_heaps_map_nothing(void) {
  static void* keep[NSIZES*NBLOCKS];
  // one delete first: the main heap takes the moved pages and maps them, and its maps stay
  mi_heap_t* heap = mi_heap_new();
  fill_heap(heap, keep);
  mi_heap_delete(heap);
  for (int i = 0; i < NSIZES*NBLOCKS; i++) { mi_free(keep[i]); }

  #if HAS_MAP_COUNTER
  const uintptr_t before = maps_allocated();
  #endif
  for (int n = 0; n < 50; n++) {
    heap = mi_heap_new();
    fill_heap(heap, NULL);
    mi_heap_destroy(heap);

    heap = mi_heap_new();
    fill_heap(heap, keep);
    mi_heap_delete(heap);   // the blocks now belong to the main heap
    for (int i = 0; i < NSIZES*NBLOCKS; i++) { mi_free(keep[i]); }
  }
  #if HAS_MAP_COUNTER
  const uintptr_t after = maps_allocated();
  if (after != before) {
    fprintf(stderr, "released heaps allocated %lu abandoned maps\n", (unsigned long)(after - before));
    abort();
  }
  #endif
}

/* -----------------------------------------------------------
   Concurrent delete: blocks freed by another thread while their heap is deleted
----------------------------------------------------------- */

#define NPAIRS   4       // a deleter and a freer each
#define DBLOCKS  8       // per size: the pages stay mostly free, so a free would re-map them

static void* atomic_exchange_ptr(volatile void** p, void* newval);
static long  atomic_load_long(volatile long* p);
static void  atomic_store_long(volatile long* p, long x);

static volatile void* dslots[NPAIRS][NSIZES*DBLOCKS];
static volatile long  dgo[NPAIRS];
static volatile long  ddone[NPAIRS];
static int            dround_count;

static void delete_pair(intptr_t tid) {
  const int pair = (int)(tid / 2);
  if ((tid % 2) == 0) {
    // deleter: allocate, let the freer start, and delete while it frees
    for (int r = 1; r <= dround_count; r++) {
      mi_heap_t* heap = mi_heap_new();
      for (int s = 0; s < NSIZES; s++) {
        for (int b = 0; b < DBLOCKS; b++) {
          void* p = mi_heap_malloc(heap, sizes[s]);
          if (p == NULL) { fprintf(stderr, "allocation failed\n"); abort(); }
          memset(p, s + 1, sizes[s]);
          atomic_exchange_ptr(&dslots[pair][s*DBLOCKS + b], p);
        }
      }
      atomic_store_long(&dgo[pair], r);
      mi_heap_delete(heap);
      while (atomic_load_long(&ddone[pair]) != r) { /* spin */ }
    }
  }
  else {
    // freer: never allocates from the heap, so it may free into it during the delete
    for (int r = 1; r <= dround_count; r++) {
      while (atomic_load_long(&dgo[pair]) != r) { /* spin */ }
      for (int i = 0; i < NSIZES*DBLOCKS; i++) {
        unsigned char* p = (unsigned char*)atomic_exchange_ptr(&dslots[pair][i], NULL);
        if (p == NULL) { fprintf(stderr, "missing block\n"); abort(); }
        if (p[0] != (unsigned char)(i / DBLOCKS + 1)) { fprintf(stderr, "block corrupted\n"); abort(); }
        mi_free(p);
      }
      atomic_store_long(&ddone[pair], r);
    }
  }
}

int main(int argc, char** argv) {
  if (argc >= 2) {
    char* end;
    long n = strtol(argv[1], &end, 10);
    if (n > 0) ITER = (int)n;
  }
  for (int i = 0; i < ITER; i++) {
    run_round(false);
    run_round(true);
  }
  released_heaps_map_nothing();
  dround_count = ITER;
  run_os_threads(NPAIRS*2, &delete_pair);
  printf("test-abandoned-lazy: %d rounds, ok\n", ITER);
  mi_stats_print(NULL);
  return 0;
}


/* -----------------------------------------------------------
   OS threads
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
  DWORD*  tids     = (DWORD*) calloc(nthreads, sizeof(DWORD));
  HANDLE* thandles = (HANDLE*)calloc(nthreads, sizeof(HANDLE));
  for (size_t i = 0; i < nthreads; i++) {
    thandles[i] = CreateThread(0, 8*1024L, &thread_entry, (void*)(i), 0, &tids[i]);
  }
  for (size_t i = 0; i < nthreads; i++) {
    WaitForSingleObject(thandles[i], INFINITE);
  }
  for (size_t i = 0; i < nthreads; i++) {
    CloseHandle(thandles[i]);
  }
  free(tids);
  free(thandles);
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

static thread_entry_fun_t* thread_entry_fun;

static void* thread_entry(void* param) {
  thread_entry_fun((intptr_t)param);
  return NULL;
}

static void run_os_threads(size_t nthreads, thread_entry_fun_t* fun) {
  thread_entry_fun = fun;
  pthread_t* threads = (pthread_t*)calloc(nthreads, sizeof(pthread_t));
  memset(threads, 0, sizeof(pthread_t) * nthreads);
  for (size_t i = 0; i < nthreads; i++) {
    pthread_create(&threads[i], NULL, &thread_entry, (void*)i);
  }
  for (size_t i = 0; i < nthreads; i++) {
    pthread_join(threads[i], NULL);
  }
  free(threads);
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
