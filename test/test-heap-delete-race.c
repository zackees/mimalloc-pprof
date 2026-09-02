/* ----------------------------------------------------------------------------
Copyright (c) Microsoft Research, Daan Leijen
This is free software; you can redistribute it and/or modify it under the
terms of the MIT license.
-----------------------------------------------------------------------------*/

// imported from oven-sh/mimalloc @ 942b8342, MIT (issue #271 / Bun parity P6).

/* Reproducer for: NULL deref in mi_theap_merge_stats during a
   mi_heap_delete vs thread-exit race.

   Thread B allocates from heap H then exits, which runs
   mi_thread_theaps_done -> _mi_theap_collect_abandon -> mi_theap_collect_ex
   -> mi_theap_merge_stats(theap), which loads _mi_theap_heap(theap) and
   dereferences ->stats.

   Concurrently the main thread calls mi_heap_delete(H) ->
   mi_heap_free_theaps -> _mi_theap_free, which atomically exchanges
   theap->heap to NULL *before* it blocks on tld->theaps_lock. If that
   exchange lands between collect_ex's entry guard and merge_stats, the
   dereference faults.

   Expected outcome with the bug present: SIGSEGV (test fails).
*/

#include <stdio.h>
#include <stdlib.h>
#include <mimalloc.h>

#ifdef _WIN32
#include <windows.h>
typedef HANDLE thread_t;
static DWORD WINAPI thread_main(LPVOID arg);
static void thread_create(thread_t* t, void* arg) { *t = CreateThread(NULL, 0, thread_main, arg, 0, NULL); }
static void thread_join(thread_t t) { WaitForSingleObject(t, INFINITE); CloseHandle(t); }
#else
#include <pthread.h>
#include <sched.h>
typedef pthread_t thread_t;
static void* thread_main(void* arg);
static void thread_create(thread_t* t, void* arg) { pthread_create(t, NULL, thread_main, arg); }
static void thread_join(thread_t t) { pthread_join(t, NULL); }
#endif

#ifdef __cplusplus
#include <atomic>
#define _Atomic(T) std::atomic<T>
#else
#include <stdatomic.h>
#endif
static _Atomic(mi_heap_t*) g_heap;
static _Atomic(int) g_allocated;

#ifdef _WIN32
static DWORD WINAPI thread_main(LPVOID arg)
#else
static void* thread_main(void* arg)
#endif
{
  (void)arg;
  void* p = mi_heap_malloc(g_heap, 64);
  mi_free(p);
  g_allocated = 1;
  // thread exit -> mi_thread_done -> mi_thread_theaps_done ->
  // _mi_theap_collect_abandon -> mi_theap_merge_stats
  return 0;
}

int main(void) {
  const int iters = 20000;
  fprintf(stderr, "test-heap-delete-race: %d iterations\n", iters);
  for (int i = 0; i < iters; i++) {
    g_heap = mi_heap_new();
    g_allocated = 0;
    thread_t t;
    thread_create(&t, NULL);
    // spin until B has allocated (so a theap exists on H), then race delete
    // against B's imminent thread-exit collect.
    while (!g_allocated) {
      #ifdef _WIN32
      SwitchToThread();
      #else
      sched_yield();
      #endif
    }
    mi_heap_delete(g_heap);
    thread_join(t);
    if ((i % 2000) == 0) fprintf(stderr, "  iter %d\n", i);
  }
  fprintf(stderr, "test-heap-delete-race: completed without crash (race not hit in %d iters)\n", iters);
  return 0;
}
