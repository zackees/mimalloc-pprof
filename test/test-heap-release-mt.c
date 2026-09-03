/* ----------------------------------------------------------------------------
Copyright (c) 2026, Microsoft Research, Daan Leijen
This is free software; you can redistribute it and/or modify it under the
terms of the MIT license.
-----------------------------------------------------------------------------*/

// ported from oven-sh/mimalloc @ a26c5de7 (+ 92930d0c, 47851447), MIT (Bun parity P10b,
// issue #317). Written against Bun's own park protocol API (`mi_on_thread_idle_start`,
// `mi_on_thread_idle_end`, `mi_on_thread_idle`, `mi_option_purge_holes_min_interval`,
// `mi_option_scavenger`), which this tree carries unchanged from Bun parity P7a/P7b -- so no
// adaptation was needed for that part. Skipped on Windows/win-gnu (uses pthreads directly,
// like `#if defined(_WIN32)` below): state that explicitly rather than letting a green
// Windows run imply coverage from this file.

/* Release heaps on many threads at once while other threads free into them.

   - NCHURN threads loop: `mi_heap_new`, allocate mixed sizes, hand a third of the blocks to a shared
     ring, free a third, leak a third, then `mi_heap_delete` (or `mi_heap_destroy` when nothing was
     handed out). Some heaps are also published for a while so that short-lived threads allocate from
     them and exit with their blocks live (the abandon path, and the per-bin abandoned maps).
   - NFREE threads pop blocks from the ring and `mi_free` them: those frees land before, during and
     after the delete of the owning heap, so pages are freed and re-abandoned by a foreign thread while
     `mi_heap_delete` claims them (`arena.c:mi_heap_visit_page_claim` against `mi_arenas_page_free_prim`).
   - NSPAWN threads keep creating batches of threads that allocate from a published heap (or the main
     heap) and exit at once.

   The second half runs the same churn "parked": before its heap is released, a churn thread hands
   its theaps to the scavenger (`mi_on_thread_idle_start`), which then sweeps the holes of that
   thread's pages and of the abandoned pages of its heaps -- including the per-bin abandoned maps of
   the heap that is about to go (`_mi_arenas_purge_abandoned_holes`, arena.c; the engine itself is
   src/page-holes.c in this tree). Half of those heaps are deleted by the parked thread itself right
   after `mi_on_thread_idle_end` (racing the tail of the sweep), the other half by a freer thread
   *while the owner is still parked* (a heap created on one thread and released on another while the
   first sits idle in the kernel), so `mi_heap_delete`'s detach has to get past the scavenger holding
   that thread's theaps (the `_mi_park_leave` loop at the head of `mi_heap_detach_theaps`, src/heap.c).

   Meant to be run under TSAN and ASAN as well as with MI_DEBUG_FULL: it must finish without a report.

   > mimalloc-test-heap-release-mt [SECONDS]
*/
#if defined(_WIN32)
#include <stdio.h>
int main(void) { printf("test-heap-release-mt: skipped on Windows (uses pthreads)\n"); return 0; }
#else

#include <mimalloc.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <stdbool.h>

#define NCHURN 8
#define NFREE  4
#define NSPAWN 4
#define RING (1<<16)
static _Atomic(void*) ring[RING];
static _Atomic unsigned long ring_head, ring_tail;
static _Atomic int stop;
static _Atomic int park_mode;       // second half: park around the release, see the header
static _Atomic unsigned long heaps_done, frees_done, exits_done, parks_done, parks_handed_off, remote_deletes;
static _Atomic(mi_heap_t*) to_delete[NCHURN];   // park mode: heap i's owner is parked and waits for a freer to delete it

static const size_t sizes[] = {8,16,48,96,200,512,1024,2048,5000,8192,20000,40000,70000,200000};
#define NS (sizeof(sizes)/sizeof(sizes[0]))

static void push(void* p) {
  for (;;) {
    unsigned long h = atomic_fetch_add(&ring_head, 1);
    void* expect = NULL;
    if (atomic_compare_exchange_strong(&ring[h % RING], &expect, p)) return;
    // slot busy: free it ourselves instead of spinning
    mi_free(p); return;
  }
}
static void* pop(void) {
  unsigned long t = atomic_fetch_add(&ring_tail, 1);
  return atomic_exchange(&ring[t % RING], NULL);
}

static unsigned rnd(unsigned* s){ *s = *s*1103515245u+12345u; return *s>>8; }

// shared live heaps for the exiting threads
#define NSHARED 4
static _Atomic(mi_heap_t*) shared[NSHARED];
static _Atomic int users[NSHARED];

static void* churn(void* arg) {
  const int self = (int)(uintptr_t)arg - 1;
  unsigned seed = (unsigned)(uintptr_t)arg * 7919u + 1;
  while (!atomic_load(&stop)) {
    mi_heap_t* h = mi_heap_new();
    int slot = -1;
    // sometimes publish as a shared heap for exiting threads to allocate from
    if ((rnd(&seed) % 4) == 0) {
      slot = rnd(&seed) % NSHARED;
      mi_heap_t* expect = NULL;
      if (!atomic_compare_exchange_strong(&shared[slot], &expect, h)) slot = -1;
    }
    int handed = 0;
    int n = 50 + rnd(&seed) % 400;
    for (int i = 0; i < n; i++) {
      size_t sz = sizes[rnd(&seed) % NS];
      void* p = mi_heap_malloc(h, sz);
      if (!p) { fprintf(stderr,"oom\n"); abort(); }
      memset(p, 0xAB, sz < 64 ? sz : 64);
      unsigned r = rnd(&seed) % 3;
      if (r == 0) { push(p); handed++; }
      else if (r == 1) mi_free(p);
      // else leak into the heap; delete moves it to main / destroy frees it
    }
    if (slot >= 0) {
      // let exiting threads use it for a bit, then unpublish
      usleep(200);
      atomic_store(&shared[slot], NULL);
      while (atomic_load(&users[slot]) != 0) usleep(10); // in-flight allocators finish (contract: no alloc during delete)
      handed = 1; // exiting threads may hold blocks: must delete, not destroy
    }
    if (atomic_load(&park_mode)) {
      const bool remote = ((rnd(&seed) & 1) != 0);
      if (remote) { atomic_store(&to_delete[self], h); }   // a freer deletes it while we are parked
      const bool parked = mi_on_thread_idle_start();
      if (parked) { atomic_fetch_add(&parks_handed_off, 1); }
      if (remote) {
        // idle in the kernel as far as mimalloc is concerned: we do not allocate or free until `_end`
        while (atomic_load(&to_delete[self]) != NULL && !atomic_load(&stop)) { usleep(20); }
      }
      else if ((rnd(&seed) & 3) == 0) {
        usleep(300);   // sometimes let the sweep get well into our heaps, sometimes race its start
      }
      mi_on_thread_idle_end();
      if (!parked) { mi_on_thread_idle(); }   // no scavenger (MIMALLOC_SCAVENGER=0): sweep inline so the pass is not vacuous
      atomic_fetch_add(&parks_done, 1);
      if (remote) {
        mi_heap_t* const left = atomic_exchange(&to_delete[self], NULL);   // only non-NULL if we are stopping
        if (left != NULL) { mi_heap_delete(left); }
        atomic_fetch_add(&heaps_done, 1);
        continue;
      }
    }
    if (handed == 0 && (rnd(&seed) & 1)) mi_heap_destroy(h);
    else mi_heap_delete(h);
    atomic_fetch_add(&heaps_done, 1);
  }
  return NULL;
}

// park mode: delete the heaps whose owners are parked and waiting for it
static void delete_parked_heaps(void) {
  for (int i = 0; i < NCHURN; i++) {
    if (atomic_load(&to_delete[i]) == NULL) continue;
    mi_heap_t* const h = atomic_exchange(&to_delete[i], NULL);
    if (h != NULL) { mi_heap_delete(h); atomic_fetch_add(&remote_deletes, 1); }
  }
}

static void* freer(void* arg) {
  (void)arg;
  while (!atomic_load(&stop) || atomic_load(&ring_tail) < atomic_load(&ring_head)) {
    delete_parked_heaps();
    void* p = pop();
    if (p) { mi_free(p); atomic_fetch_add(&frees_done,1); }
    else if (atomic_load(&stop)) break;
  }
  return NULL;
}

static void* exiter(void* arg) {
  unsigned seed = (unsigned)(uintptr_t)arg;
  int slot = rnd(&seed) % NSHARED;
  atomic_fetch_add(&users[slot], 1);
  mi_heap_t* h = atomic_load(&shared[slot]);
  for (int i = 0; i < 40; i++) {
    size_t sz = sizes[rnd(&seed) % 8];
    void* p = h ? mi_heap_malloc(h, sz) : mi_malloc(sz);
    if (p) { memset(p, 0xCD, sz < 64 ? sz : 64); push(p); }
  }
  atomic_fetch_sub(&users[slot], 1);
  atomic_fetch_add(&exits_done,1);
  return NULL; // exit with blocks live -> pages abandoned (mapped, lazy bitmap alloc)
}

static void* spawner(void* arg) {
  unsigned k = (unsigned)(uintptr_t)arg * 100000u;
  while (!atomic_load(&stop)) {
    pthread_t t[8];
    for (int i = 0; i < 8; i++) pthread_create(&t[i], NULL, exiter, (void*)(uintptr_t)(k++));
    for (int i = 0; i < 8; i++) pthread_join(t[i], NULL);
  }
  return NULL;
}

static void run(int secs) {
  atomic_store(&stop, 0);
  atomic_store(&ring_head, 0); atomic_store(&ring_tail, 0);
  pthread_t tc[NCHURN], tf[NFREE], ts[NSPAWN];
  for (long i = 0; i < NFREE; i++) pthread_create(&tf[i], NULL, freer, (void*)i);
  for (long i = 0; i < NCHURN; i++) pthread_create(&tc[i], NULL, churn, (void*)(i+1));
  for (long i = 0; i < NSPAWN; i++) pthread_create(&ts[i], NULL, spawner, (void*)(i+1));
  sleep((unsigned)secs);
  atomic_store(&stop, 1);
  for (int i = 0; i < NCHURN; i++) pthread_join(tc[i], NULL);
  for (int i = 0; i < NSPAWN; i++) pthread_join(ts[i], NULL);
  for (int i = 0; i < NFREE; i++) pthread_join(tf[i], NULL);
  // drain
  for (unsigned i = 0; i < RING; i++) { void* q = atomic_exchange(&ring[i], NULL); if (q) mi_free(q); }
  mi_collect(true);
}

int main(int argc, char** argv) {
  int secs = (argc > 1 ? atoi(argv[1]) : 0);
  if (secs <= 0) {
    #if defined(MI_TSAN) || !defined(NDEBUG)
    secs = 4;
    #else
    secs = 6;
    #endif
  }
  // 1. plain
  run((secs + 1) / 2);
  printf("test-heap-release-mt: churn ok  (heaps=%lu frees=%lu thread-exits=%lu)\n",
         (unsigned long)heaps_done, (unsigned long)frees_done, (unsigned long)exits_done);
  // 2. parked around the release, every park swept (no rate limit)
  mi_option_set(mi_option_purge_holes_min_interval, 0);
  atomic_store(&park_mode, 1);
  run((secs + 1) / 2);
  printf("test-heap-release-mt: parked ok (heaps=%lu frees=%lu thread-exits=%lu parks=%lu handed-off=%lu remote-deletes=%lu scavenger=%s)\n",
         (unsigned long)heaps_done, (unsigned long)frees_done, (unsigned long)exits_done,
         (unsigned long)parks_done, (unsigned long)parks_handed_off, (unsigned long)remote_deletes,
         mi_option_is_enabled(mi_option_scavenger) ? "on" : "off");
  return 0;
}

#endif
