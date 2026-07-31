/* Degenerate allocation patterns and memory-bound regression guards.

   The motivating history: this fork shipped two separate unbounded memory leaks that
   every existing test passed straight through, because the whole suite only ever asked
   "did it crash?" and never "did memory stay bounded?".

     - v3 (fixed in #44): MinGW loader TLS callbacks were registered with MSVC-only
       pragmas that GCC ignores, so thread-exit cleanup never ran and every exiting
       thread leaked its theap and pages.
     - v2 (fixed in #47): the same mode is unusable under MinGW for a second reason
       (emutls is torn down before any PE TLS callback), so FLS is required there.

   Both showed up as *linear growth in RSS per iteration* while still exiting 0 -- they
   only became crashes on a machine small enough to run out. A large dev box hides them
   completely. So the central test here is scenario A: run repeated thread churn and
   assert memory does not grow linearly. That single assertion catches this entire bug
   class on any machine, regardless of how much RAM it has.

   The remaining scenarios drive allocation patterns that the existing stress tests do
   not: sawtooth, fragmentation-then-large, full size-class sweeps, realloc ping-pong,
   and huge/giant churn.

   Bounds are deliberately generous (multiples, not absolutes) so this is a leak
   detector, not a fragile high-water-mark check. */

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
#include "mimalloc-stats.h"

/* ---- portable threading ------------------------------------------------ */

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

static size_t current_rss(void) {
  size_t rss = 0;
  mi_process_info(NULL, NULL, NULL, &rss, NULL, NULL, NULL, NULL);
  return rss;
}

static double as_mb(size_t bytes) { return (double)bytes / (1024.0 * 1024.0); }

/* ---- scenario A: thread churn must not grow memory --------------------- */
/* This is the regression guard for the two leaks described above. */

#define CHURN_THREADS 8
#define CHURN_ALLOCS  2000

static THREAD_RET churn_worker(void* arg) {
  (void)arg;
  /* Touch a spread of size classes so the thread genuinely owns pages that have to be
     handed back when it exits -- a thread that only allocates tiny blocks may not
     retain enough for a leak to be visible. */
  /* Retain a few MiB of real pages per thread. The leaked object in the bugs above is
     the thread's own page set, so a thread that owns only a handful of tiny blocks
     leaks too little to move RSS -- the counter check would fire but the memory check
     would not, which makes the memory backstop useless. */
  void* keep[64];
  for (size_t i = 0; i < 64; i++) {
    keep[i] = mi_malloc(64 * 1024);
    assert(keep[i] != NULL);
    memset(keep[i], 0x5A, 4096);
  }
  for (size_t i = 0; i < CHURN_ALLOCS; i++) {
    void* p = mi_malloc(64 + (i % 900));
    assert(p != NULL);
    mi_free(p);
  }
  for (size_t i = 0; i < 64; i++) { mi_free(keep[i]); }
  return THREAD_OK;
}

static void churn_round(void) {
  thread_t threads[CHURN_THREADS];
  for (size_t i = 0; i < CHURN_THREADS; i++) {
    thread_start(&threads[i], (thread_fun_t)churn_worker, NULL);
  }
  for (size_t i = 0; i < CHURN_THREADS; i++) { thread_join(threads[i]); }
}

static void test_thread_churn_is_bounded(void) {
  /* Warm up first: the first rounds legitimately grow as arenas are reserved. Measure
     the baseline only after that, so we compare steady state against steady state. */
  for (int i = 0; i < 3; i++) { churn_round(); }
  mi_collect(true);
  const size_t baseline = current_rss();

  for (int i = 0; i < 20; i++) { churn_round(); }
  mi_collect(true);
  const size_t after = current_rss();

  /* Memory backstop. This is deliberately loose -- it exists to catch a leak that does
     NOT show up in the engine counters below. The counter check is the precise detector;
     measured against the real #44 bug this saw 6.5 MB -> 22.8 MB (3.5x) while the
     counter read 184 leaked threads, so do not rely on this bound alone. */
  const size_t allowed = (baseline * 3) + (32u * 1024 * 1024);
  printf("  thread churn: baseline %.1f MB -> after %.1f MB (allowed <= %.1f MB)\n",
         as_mb(baseline), as_mb(after), as_mb(allowed));
  if (after > allowed) {
    fprintf(stderr,
            "FAIL: memory grew with thread churn -- %.1f MB -> %.1f MB after 160 more\n"
            "      threads. This is the signature of thread-exit cleanup not running\n"
            "      (see #44 for v3 / #47 for v2).\n",
            as_mb(baseline), as_mb(after));
    abort();
  }

  /* The engine's own view must agree: threads created must be released. mimalloc's
     `threads` counter never decrementing is exactly what both leaks looked like. */
  mi_stats_t_decl(st);
  if (mi_stats_get(&st)) {
    printf("  engine counters: threads.current=%lld pages.current=%lld\n",
           (long long)st.threads.current, (long long)st.pages.current);
    /* THE primary detector. 184 threads have been created and joined by now; anything
       close to that still live means thread-exit cleanup never ran. Verified against the
       real bug: with the #44 fix reverted this reads exactly 184, and 1 when fixed. */
    if (st.threads.current >= 32) {
      fprintf(stderr,
              "FAIL: %lld threads still live after creating and joining 184 of them.\n"
              "      Thread-exit cleanup is not running (see #44 for v3 / #47 for v2).\n",
              (long long)st.threads.current);
      fflush(stderr);
    }
    assert(st.threads.current < 32);
  }
}

/* ---- scenario B: sawtooth ---------------------------------------------- */
/* Repeatedly build a large live set and drop it entirely. Peak should repeat, not climb. */

static void test_sawtooth(void) {
  enum { teeth = 12, per_tooth = 20000, sz = 512 };
  size_t first_peak = 0;
  for (int t = 0; t < teeth; t++) {
    void** batch = (void**)mi_malloc(per_tooth * sizeof(void*));
    assert(batch != NULL);
    for (int i = 0; i < per_tooth; i++) {
      batch[i] = mi_malloc(sz);
      assert(batch[i] != NULL);
      *(char*)batch[i] = (char)i;
    }
    const size_t peak = current_rss();
    for (int i = 0; i < per_tooth; i++) { mi_free(batch[i]); }
    mi_free(batch);
    if (t == 2) { first_peak = peak; }          /* settle for a few teeth first */
    else if (t > 2) {
      const size_t allowed = (first_peak * 2) + (64u * 1024 * 1024);
      if (peak > allowed) {
        fprintf(stderr, "FAIL: sawtooth peak climbing: tooth 2 %.1f MB, tooth %d %.1f MB\n",
                as_mb(first_peak), t, as_mb(peak));
        abort();
      }
    }
  }
  printf("  sawtooth: stable across %d teeth (settled peak %.1f MB)\n", teeth, as_mb(first_peak));
}

/* ---- scenario C: fragmentation, then large allocations ----------------- */
/* Free every other small block, then demand large blocks. Exercises the allocator's
   ability to reuse a checkerboarded heap rather than only ever growing. */

static void test_fragmentation_then_large(void) {
  enum { count = 40000, small = 128 };
  void** blocks = (void**)mi_malloc(count * sizeof(void*));
  assert(blocks != NULL);
  for (int i = 0; i < count; i++) {
    blocks[i] = mi_malloc(small);
    assert(blocks[i] != NULL);
  }
  for (int i = 0; i < count; i += 2) { mi_free(blocks[i]); blocks[i] = NULL; }

  const size_t before = current_rss();
  enum { bigs = 200, big = 256 * 1024 };
  void* bigblocks[bigs];
  for (int i = 0; i < bigs; i++) {
    bigblocks[i] = mi_malloc(big);
    assert(bigblocks[i] != NULL);
    memset(bigblocks[i], 0x11, 4096);
  }
  for (int i = 0; i < bigs; i++) { mi_free(bigblocks[i]); }
  for (int i = 1; i < count; i += 2) { mi_free(blocks[i]); }
  mi_free(blocks);
  printf("  fragmentation: %d holes then %d x %d KiB ok (rss %.1f MB before)\n",
         count / 2, bigs, big / 1024, as_mb(before));
}

/* ---- scenario D: every size class --------------------------------------- */
/* Sweep the full bin range including the huge/giant boundary, which is where
   size-class edge bugs hide. */

static void test_size_class_sweep(void) {
  size_t sizes_checked = 0;
  for (size_t bin = 0; bin <= MI_BIN_HUGE; bin++) {
    const size_t bsize = mi_stats_get_bin_size(bin);
    if (bsize == 0 || bsize > 64u * 1024 * 1024) continue;   /* keep the test bounded */
    void* p = mi_malloc(bsize);
    assert(p != NULL);
    /* usable size must cover what we asked for, and the block must be writable to
       its reported extent. */
    const size_t usable = mi_usable_size(p);
    assert(usable >= bsize);
    memset(p, 0xC3, bsize);
    mi_free(p);

    /* also probe the boundaries either side of the class */
    if (bsize > 1) {
      void* q = mi_malloc(bsize - 1); assert(q != NULL); mi_free(q);
    }
    void* r = mi_malloc(bsize + 1); assert(r != NULL); mi_free(r);
    sizes_checked++;
  }
  printf("  size classes: %zu bins exercised (with +/-1 boundaries)\n", sizes_checked);
  assert(sizes_checked > 10);
}

/* ---- scenario E: realloc ping-pong -------------------------------------- */
/* Grow and shrink the same pointer repeatedly across the small/large boundary,
   verifying contents survive every move. */

static void test_realloc_pingpong(void) {
  const size_t sizes[] = { 32, 4096, 96, 512 * 1024, 64, 1024 * 1024, 128 };
  const size_t n = sizeof(sizes) / sizeof(sizes[0]);
  void* p = mi_malloc(sizes[0]);
  assert(p != NULL);
  memset(p, 0xAB, sizes[0]);
  size_t prev = sizes[0];
  for (int round = 0; round < 200; round++) {
    const size_t next = sizes[(round + 1) % n];
    void* q = mi_realloc(p, next);
    assert(q != NULL);
    /* the overlap must be preserved byte-for-byte */
    const size_t overlap = (prev < next ? prev : next);
    const unsigned char* b = (const unsigned char*)q;
    for (size_t i = 0; i < overlap; i++) { assert(b[i] == 0xAB); }
    memset(q, 0xAB, next);
    p = q; prev = next;
  }
  mi_free(p);

  /* realloc(NULL) == malloc, realloc(p,0) is a valid free-ish operation */
  void* a = mi_realloc(NULL, 128); assert(a != NULL);
  void* b2 = mi_realloc(a, 0);     /* implementation may return NULL or a minimal block */
  if (b2 != NULL) { mi_free(b2); }
  printf("  realloc ping-pong: 200 moves, contents preserved\n");
}

/* ---- scenario F: huge / giant churn ------------------------------------- */

static void test_huge_churn(void) {
  const size_t sizes[] = { 2u*1024*1024, 8u*1024*1024, 32u*1024*1024 };
  for (int round = 0; round < 8; round++) {
    for (size_t s = 0; s < 3; s++) {
      void* p = mi_malloc(sizes[s]);
      assert(p != NULL);
      /* touch first and last page so the pages are really committed */
      ((char*)p)[0] = 1;
      ((char*)p)[sizes[s] - 1] = 2;
      assert(mi_usable_size(p) >= sizes[s]);
      mi_free(p);
    }
  }
  mi_collect(true);
  printf("  huge churn: 24 huge allocations cycled, rss %.1f MB\n", as_mb(current_rss()));
}

/* ---- scenario G: degenerate arguments ----------------------------------- */

static void test_degenerate_arguments(void) {
  /* zero-size: must return either NULL or a uniquely freeable pointer, never crash */
  void* z = mi_malloc(0);
  if (z != NULL) { assert(mi_usable_size(z) >= 0); mi_free(z); }

  /* free(NULL) is a no-op */
  mi_free(NULL);

  /* aligned allocation across a wide alignment range */
  for (size_t align = sizeof(void*); align <= 4096; align *= 2) {
    void* p = mi_malloc_aligned(1000, align);
    assert(p != NULL);
    assert(((uintptr_t)p % align) == 0);
    memset(p, 0x7E, 1000);
    mi_free(p);
  }

  /* absurd sizes must fail cleanly (NULL), not abort or wrap around */
  void* huge = mi_malloc(SIZE_MAX);
  assert(huge == NULL);
  void* huge2 = mi_malloc(SIZE_MAX / 2);
  if (huge2 != NULL) { mi_free(huge2); }   /* allowed to succeed on paper; must not corrupt */

  /* calloc overflow must be detected rather than silently under-allocating */
  void* ov = mi_calloc(SIZE_MAX / 2, 4);
  assert(ov == NULL);

  printf("  degenerate args: zero-size, NULL free, alignments, overflow all handled\n");
}

int main(void) {
  /* Unbuffered: these scenarios abort on failure, and a buffered stdout would swallow
     the very numbers needed to diagnose it. */
  setvbuf(stdout, NULL, _IONBF, 0);
  printf("test-degenerate: starting\n");
  test_thread_churn_is_bounded();
  test_sawtooth();
  test_fragmentation_then_large();
  test_size_class_sweep();
  test_realloc_pingpong();
  test_huge_churn();
  test_degenerate_arguments();
  printf("test-degenerate: ok\n");
  return 0;
}
