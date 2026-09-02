/* ----------------------------------------------------------------------------
Copyright (c) 2026, Microsoft Research, Daan Leijen
This is free software; you can redistribute it and/or modify it under the
terms of the MIT license. A copy of the license can be found in the file
"LICENSE" at the root of this distribution.
-----------------------------------------------------------------------------*/

// imported from oven-sh/mimalloc @ 942b8342, MIT (issue #271 / Bun parity P6).

/* The `_mi_theap_cached` ABA on a reused heap address.

   Every thread caches the theap of the heap it last allocated from, and `_mi_heap_theap`
   takes that cache when `theap->heap` is the requested heap. When a heap is destroyed on
   another thread, its theaps are detached but kept alive by that cache. Unless the detach
   also clears `theap->heap`, a new heap created at the same address matches the stale entry,
   and the thread allocates from a theap whose pages have been destroyed:

     B: H = mi_heap_new()
     A: mi_heap_malloc(H)                      -> A caches its theap for H
     B: mi_heap_destroy(H); H2 = mi_heap_new()  -> H2 == H (the block is reused)
     A: mi_heap_malloc(H2)                      -> must NOT be served by A's old theap for H

   Checks that every block A gets from H2 belongs to H2 (`mi_heap_of`). With the bug, the
   block lies in memory that was handed back to the arena (`mi_heap_of` returns NULL), or in
   a page that by then belongs to someone else.
*/

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <mimalloc.h>

#ifdef _WIN32
#include <windows.h>
typedef HANDLE thread_t;
static DWORD WINAPI thread_main(LPVOID arg);
static void thread_create(thread_t* t, void* arg) { *t = CreateThread(NULL, 0, thread_main, arg, 0, NULL); }
static void thread_join(thread_t t) { WaitForSingleObject(t, INFINITE); CloseHandle(t); }
static void thread_yield(void) { SwitchToThread(); }
#else
#include <pthread.h>
#include <sched.h>
typedef pthread_t thread_t;
static void* thread_main(void* arg);
static void thread_create(thread_t* t, void* arg) { pthread_create(t, NULL, thread_main, arg); }
static void thread_join(thread_t t) { pthread_join(t, NULL); }
static void thread_yield(void) { sched_yield(); }
#endif

#define ROUNDS          (64)
#define ALLOCS_PER_ROUND (16)

/* hand-shake between the two threads: B publishes a heap, A uses it, B recycles it.
   Only load/store are needed (no CAS/fetch-add), via small portable wrappers -- mirrors
   test-heap-mt.c / test-stress.c's own "Portable threading / atomics" block. Plain
   assignment/comparison on a C11 _Atomic or C++ std::atomic works everywhere except MSVC C
   compilation, where <stdatomic.h> #errors "C atomic support is not enabled" unless built
   with `/std:c11 /experimental:c11atomics` (not set here) -- see
   include/mimalloc/atomic.h's own MI_HAS_C11_ATOMICS detection for the same problem solved
   inside mimalloc itself; that header is not included here since only load/store, not the
   full atomic API, are needed. */
#ifdef _WIN32
#include <windows.h>
static void* atomic_load_ptr(void* volatile* p)        { return InterlockedCompareExchangePointer(p, NULL, NULL); }
static void  atomic_store_ptr(void* volatile* p, void* v) { InterlockedExchangePointer(p, v); }
static long  atomic_load_l(volatile long* p)            { return InterlockedCompareExchange(p, 0, 0); }
static void  atomic_store_l(volatile long* p, long v)   { InterlockedExchange(p, v); }
static void* volatile g_heap;
static volatile long  g_step;     /* even: B's turn, odd: A's turn */
#else
#ifdef __cplusplus
#include <atomic>
#define _Atomic(T) std::atomic<T>
#else
#include <stdatomic.h>
#endif
static _Atomic(void*) g_heap;
static _Atomic(long)  g_step;     /* even: B's turn, odd: A's turn */
static void* atomic_load_ptr(_Atomic(void*)* p)         { return *p; }
static void  atomic_store_ptr(_Atomic(void*)* p, void* v) { *p = v; }
static long  atomic_load_l(_Atomic(long)* p)            { return *p; }
static void  atomic_store_l(_Atomic(long)* p, long v)   { *p = v; }
#endif
static int g_same_address;        /* rounds where the recycled heap came back at the same address */
static int g_failures;

static void wait_step(long s) {
  while (atomic_load_l(&g_step) != s) { thread_yield(); }
}

#ifdef _WIN32
static DWORD WINAPI thread_main(LPVOID arg)
#else
static void* thread_main(void* arg)
#endif
{
  (void)arg;
  for (int round = 0; round < ROUNDS; round++) {
    const int base = round * 4;
    // first use of H: this caches A's theap for H
    wait_step(base + 1);
    mi_heap_t* const h = (mi_heap_t*)atomic_load_ptr(&g_heap);
    void* p = mi_heap_malloc(h, 64);
    if (p == NULL || mi_heap_of(p) != h) { g_failures++; }
    memset(p, 0xA1, 64);
    atomic_store_l(&g_step, base + 2);

    // H was destroyed and a new heap was created (usually at the same address)
    wait_step(base + 3);
    mi_heap_t* const h2 = (mi_heap_t*)atomic_load_ptr(&g_heap);
    if (h2 == h) { g_same_address++; }
    for (int i = 0; i < ALLOCS_PER_ROUND; i++) {
      void* q = mi_heap_malloc(h2, 64);
      if (q == NULL || mi_heap_of(q) != h2) {
        fprintf(stderr, "test-heap-aba: round %d: block %p from heap %p belongs to heap %p (%s)\n",
                round, q, (void*)h2, (void*)(q == NULL ? NULL : mi_heap_of(q)), (h2 == h ? "reused address" : "new address"));
        g_failures++;
        break;
      }
      memset(q, 0xB2, 64);
    }
    atomic_store_l(&g_step, base + 4);
  }
  return 0;
}

int main(void) {
  thread_t t;
  thread_create(&t, NULL);
  for (int round = 0; round < ROUNDS; round++) {
    const int base = round * 4;
    mi_heap_t* h = mi_heap_new();
    void* warm = mi_heap_malloc(h, 64);
    memset(warm, 0xC3, 64);
    atomic_store_ptr(&g_heap, h);
    atomic_store_l(&g_step, base + 1);
    wait_step(base + 2);
    // destroy while A still caches its theap for `h`, then create the next heap: a LIFO reuse
    // of the freed heap block usually puts it at the same address
    mi_heap_destroy(h);
    mi_heap_t* h2 = mi_heap_new();
    atomic_store_ptr(&g_heap, h2);
    atomic_store_l(&g_step, base + 3);
    wait_step(base + 4);
    mi_heap_destroy(h2);
  }
  thread_join(t);
  fprintf(stderr, "test-heap-aba: %d rounds, %d with the heap address reused, %d failures\n", ROUNDS, g_same_address, g_failures);
  if (g_same_address == 0) {
    fprintf(stderr, "test-heap-aba: WARNING: the heap address was never reused, the ABA was not exercised\n");
  }
  return (g_failures == 0 ? 0 : 1);
}
