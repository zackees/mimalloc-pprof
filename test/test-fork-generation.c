/* Regression test for issue #293: clear `_mi_process_is_forked_child` (really: stop
   treating every heap in a forked child as if it might hold a torn page) once a forked
   child's thread population has resynced with its own generation.

   Background (see src/fork.c / src/arena.c / src/heap.c):

   After a multi-threaded fork(), only the calling thread survives in the child. Any
   thread that was mid-allocation on a heap at the moment of the fork can leave that
   heap's page state torn (a half-updated `used`/`local_free`/`xthread_free`). To cope,
   `mi_heap_visit_page_claim` (arena.c) has a "forked-child" branch that force-seizes a
   page instead of trusting its abandoned/owned state, reconciling it from the bitmaps.

   Before the fix landed by T1-T4 of #293, that branch fired for EVERY heap deleted in a
   forked child, keyed only on the process-wide `_mi_process_is_forked_child` flag --
   including heaps that were created entirely AFTER the fork, or heaps whose only theaps
   belong to threads that themselves survived (or were started after) the fork and are
   therefore not torn at all. After the fix, the branch is narrowed per heap: it only
   fires for a heap that already existed at the fork (`heap->prefork_theaps`, set on every
   such heap by src/fork.c's child handler, and again by `mi_heap_detach_theaps` in heap.c
   when it skips a pre-fork theap). A pre-existing heap stays conservative even with no
   pre-fork theap of its own: a vanished thread may have been inside a cross-thread free
   that claimed ownership of one of its abandoned pages, and that page stays owned forever.

   RED mechanism: `mi_debug_forked_claim_seized` (arena.c, `#if MI_DEBUG > 0` only) counts
   every page taken by that force-seize branch. On the unfixed tree, deleting ANY heap in
   a forked child -- even one created after the fork -- moves this counter, because the
   branch is gated on the flag alone. On the fixed tree it only moves for a heap that
   existed at the fork. Case B, and the fresh post-fork heaps in cases C and D, assert the
   counter does NOT move: they fail (RED) against the unfixed tree and pass with the fix.
   Cases A, C and D also delete pre-fork heaps and assert the counter DOES move; those
   checks pass on both trees and exist so a future change cannot "narrow" the branch into
   never firing (or into waiting forever on a page a dead thread owns).

   The counter only exists in `MI_DEBUG > 0` builds (arena.c's own gate), so every
   comparison against it is compiled out in a Release build; there this file still
   builds and runs as a plain no-crash/no-hang smoke test of the same fork/heap-delete
   shapes. */

#if defined(_WIN32) || defined(__wasi__)
#include <stdio.h>
int main(void) { fprintf(stderr, "test-fork-generation: skipped on Windows/wasi (POSIX-only, #293)\n"); return 0; }
#else

#include <mimalloc.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <errno.h>
#include <sys/wait.h>

// ---------------------------------------------------------------------------------------
// The force-seize counter. See test-abandoned-lazy.c lines ~152-176 for the three-way
// declaration this is copied from (C++ std::atomic / MSVC-C-without-c11atomics volatile /
// C11 <stdatomic.h>) and the reasoning behind each branch.
// ---------------------------------------------------------------------------------------
#if MI_DEBUG > 0
#ifdef __cplusplus
#include <atomic>
extern "C" std::atomic<uintptr_t> mi_debug_forked_claim_seized;
static uintptr_t seized(void) { return mi_debug_forked_claim_seized.load(); }
#elif defined(_MSC_VER) && !(defined(__STDC_VERSION__) && __STDC_VERSION__ >= 201112L && !defined(__STDC_NO_ATOMICS__))
// MSVC C compilation without /std:c11 /experimental:c11atomics: <stdatomic.h> #errors.
extern volatile uintptr_t mi_debug_forked_claim_seized;
static uintptr_t seized(void) { return mi_debug_forked_claim_seized; }
#else
#include <stdatomic.h>
extern _Atomic(uintptr_t) mi_debug_forked_claim_seized;
static uintptr_t seized(void) { return atomic_load(&mi_debug_forked_claim_seized); }
#endif
#define HAS_COUNTER 1
#else
static uintptr_t seized(void) { return 0; }
#define HAS_COUNTER 0
#endif

static void on_alarm(int sig) {
  (void)sig;
  _exit(99);   // watchdog: this process hung
}

static void arm_watchdog(void) {
  signal(SIGALRM, on_alarm);
  alarm(20);
}

// ---------------------------------------------------------------------------------------
// parked_alloc_thread: allocates a batch of mixed-size blocks from a caller-supplied heap,
// signals "ready", then blocks (no busy-wait) until released. Deliberately does not free
// its own blocks -- once its heap is deleted they belong to the main heap, and the caller
// frees them with mi_free() after joining.
// ---------------------------------------------------------------------------------------
static const size_t parked_sizes[] = {
  16, 24, 48, 64, 96, 128, 192, 256, 384, 512, 768, 1024, 1536, 2048, 3072, 4096
};
#define PARKED_NSIZES  (sizeof(parked_sizes) / sizeof(parked_sizes[0]))
#define PARKED_BLOCKS  200

typedef struct parked_ctx_s {
  mi_heap_t*      heap;
  void*           blocks[PARKED_BLOCKS];
  pthread_mutex_t mutex;
  pthread_cond_t  cond;
  bool            ready;
  bool            release;
} parked_ctx_t;

static void parked_ctx_init(parked_ctx_t* ctx, mi_heap_t* heap) {
  memset(ctx, 0, sizeof(*ctx));
  ctx->heap = heap;
  pthread_mutex_init(&ctx->mutex, NULL);
  pthread_cond_init(&ctx->cond, NULL);
}

static void parked_ctx_destroy(parked_ctx_t* ctx) {
  pthread_mutex_destroy(&ctx->mutex);
  pthread_cond_destroy(&ctx->cond);
}

static void* parked_alloc_thread(void* arg) {
  parked_ctx_t* ctx = (parked_ctx_t*)arg;
  for (int i = 0; i < PARKED_BLOCKS; i++) {
    ctx->blocks[i] = mi_heap_malloc(ctx->heap, parked_sizes[i % PARKED_NSIZES]);
  }
  pthread_mutex_lock(&ctx->mutex);
  ctx->ready = true;
  pthread_cond_signal(&ctx->cond);
  while (!ctx->release) { pthread_cond_wait(&ctx->cond, &ctx->mutex); }
  pthread_mutex_unlock(&ctx->mutex);
  return NULL;
}

static void parked_wait_ready(parked_ctx_t* ctx) {
  pthread_mutex_lock(&ctx->mutex);
  while (!ctx->ready) { pthread_cond_wait(&ctx->cond, &ctx->mutex); }
  pthread_mutex_unlock(&ctx->mutex);
}

static void parked_release(parked_ctx_t* ctx) {
  pthread_mutex_lock(&ctx->mutex);
  ctx->release = true;
  pthread_cond_signal(&ctx->cond);
  pthread_mutex_unlock(&ctx->mutex);
}

static void parked_free_blocks(parked_ctx_t* ctx) {
  for (int i = 0; i < PARKED_BLOCKS; i++) { mi_free(ctx->blocks[i]); ctx->blocks[i] = NULL; }
}

// ---------------------------------------------------------------------------------------
// Case A: survivor heap. Parent creates heap K, allocates from it, then forks. The child
// deletes K; K has no pre-fork orphan theap (only the surviving thread touched it), but it
// existed at the fork, so it must stay on the conservative branch (the counter must move):
// in general a vanished thread could have left one of its abandoned pages owned mid
// cross-thread free, and the normal claim loop would wait on that page forever. The
// survivor's own theap is abandoned normally by the detach (its tld was restamped).
// ---------------------------------------------------------------------------------------
static int run_case_a(void) {
  mi_heap_t* k = mi_heap_new();
  void* blocks[100];
  for (int i = 0; i < 100; i++) { blocks[i] = mi_heap_malloc(k, parked_sizes[i % PARKED_NSIZES]); }

  fflush(stderr);
  const pid_t pid = fork();
  if (pid < 0) {
    fprintf(stderr, "case_a: fork() failed: %s\n", strerror(errno));
    for (int i = 0; i < 100; i++) { mi_free(blocks[i]); }
    mi_heap_delete(k);
    return -1;
  }
  if (pid == 0) {
    arm_watchdog();
    #if HAS_COUNTER
    const uintptr_t c0 = seized();
    #endif
    mi_heap_delete(k);
    #if HAS_COUNTER
    if (!(seized() > c0)) {
      fprintf(stderr, "case_a: FAIL (a heap that existed at the fork did not force-seize: counter stayed at %lu)\n",
              (unsigned long)c0);
      _exit(1);
    }
    #endif
    _exit(0);
  }

  int status = 0;
  waitpid(pid, &status, 0);
  for (int i = 0; i < 100; i++) { mi_free(blocks[i]); }
  mi_heap_delete(k);
  return (WIFEXITED(status) && WEXITSTATUS(status) == 0) ? 0 : 1;
}

// ---------------------------------------------------------------------------------------
// Case B: post-fork heap and post-fork thread. Everything -- the fork, the heap, and the
// thread that uses it -- happens inside the child, so nothing predates the fork. Neither
// delete may move the counter. RED on the unfixed tree.
// ---------------------------------------------------------------------------------------
static int run_case_b(void) {
  fflush(stderr);
  const pid_t pid = fork();
  if (pid < 0) {
    fprintf(stderr, "case_b: fork() failed: %s\n", strerror(errno));
    return -1;
  }
  if (pid == 0) {
    arm_watchdog();

    // a heap and a worker thread, both created after the fork
    mi_heap_t* h = mi_heap_new();
    parked_ctx_t ctx;
    parked_ctx_init(&ctx, h);
    pthread_t t;
    if (pthread_create(&t, NULL, parked_alloc_thread, &ctx) != 0) {
      fprintf(stderr, "case_b: could not start the post-fork worker thread\n");
      _exit(3);
    }
    parked_wait_ready(&ctx);

    #if HAS_COUNTER
    const uintptr_t c0 = seized();
    #endif
    mi_heap_delete(h);
    #if HAS_COUNTER
    if (seized() != c0) {
      fprintf(stderr, "case_b: FAIL (post-fork heap + post-fork thread moved the force-seize counter %lu -> %lu)\n",
              (unsigned long)c0, (unsigned long)seized());
      _exit(2);
    }
    #endif

    parked_release(&ctx);
    pthread_join(t, NULL);
    parked_free_blocks(&ctx);
    parked_ctx_destroy(&ctx);

    // a second post-fork heap, used only from the main (surviving) thread
    mi_heap_t* h2 = mi_heap_new();
    void* p = mi_heap_malloc(h2, 64);
    #if HAS_COUNTER
    const uintptr_t c1 = seized();
    #endif
    mi_heap_delete(h2);
    #if HAS_COUNTER
    if (seized() != c1) {
      fprintf(stderr, "case_b: FAIL (post-fork main-thread-only heap moved the force-seize counter %lu -> %lu)\n",
              (unsigned long)c1, (unsigned long)seized());
      _exit(4);
    }
    #endif
    mi_free(p);
    _exit(0);
  }

  int status = 0;
  waitpid(pid, &status, 0);
  return (WIFEXITED(status) && WEXITSTATUS(status) == 0) ? 0 : 1;
}

// ---------------------------------------------------------------------------------------
// Case C: a true pre-fork orphan is still handled conservatively. The parent starts a
// worker thread against heap G BEFORE forking; that thread does not survive the fork, so
// G genuinely has a torn pre-fork theap and the counter MUST increase when G is deleted.
// A second heap (H3), used only from the child's surviving main thread, must not move the
// counter any further -- the narrowing is per-heap, not "off entirely once we've seen one
// forked child". The G check passes on both the unfixed and fixed trees (it catches
// over-narrowing); the H3 check is RED on the unfixed tree.
// ---------------------------------------------------------------------------------------
static int run_case_c(void) {
  mi_heap_t* g = mi_heap_new();
  parked_ctx_t ctx;
  parked_ctx_init(&ctx, g);
  pthread_t t;
  if (pthread_create(&t, NULL, parked_alloc_thread, &ctx) != 0) {
    fprintf(stderr, "case_c: could not start the pre-fork worker thread\n");
    parked_ctx_destroy(&ctx);
    mi_heap_delete(g);
    return -1;
  }
  parked_wait_ready(&ctx);

  fflush(stderr);
  const pid_t pid = fork();
  if (pid < 0) {
    fprintf(stderr, "case_c: fork() failed: %s\n", strerror(errno));
    parked_release(&ctx);
    pthread_join(t, NULL);
    parked_free_blocks(&ctx);
    parked_ctx_destroy(&ctx);
    mi_heap_delete(g);
    return -1;
  }
  if (pid == 0) {
    arm_watchdog();

    // t did not survive the fork: g has a genuine pre-fork orphan theap
    #if HAS_COUNTER
    const uintptr_t c0 = seized();
    #endif
    mi_heap_delete(g);
    #if HAS_COUNTER
    if (!(seized() > c0)) {
      fprintf(stderr, "case_c: FAIL (a true pre-fork orphan heap did not force-seize: counter stayed at %lu)\n",
              (unsigned long)c0);
      _exit(5);
    }
    const uintptr_t c1 = seized();
    #endif

    // a fresh heap, used only from the (surviving) main thread: no pre-fork orphan here
    mi_heap_t* h3 = mi_heap_new();
    void* p = mi_heap_malloc(h3, 64);
    mi_heap_delete(h3);
    #if HAS_COUNTER
    if (seized() != c1) {
      fprintf(stderr, "case_c: FAIL (an unrelated main-thread-only heap moved the force-seize counter %lu -> %lu "
                      "after a true orphan was already seen -- over-narrowing would show as an UNDER-count here, "
                      "but a still-too-broad branch shows as this)\n",
              (unsigned long)c1, (unsigned long)seized());
      _exit(6);
    }
    #endif
    mi_free(p);
    _exit(0);
  }

  int status = 0;
  waitpid(pid, &status, 0);
  parked_release(&ctx);
  pthread_join(t, NULL);
  parked_free_blocks(&ctx);
  parked_ctx_destroy(&ctx);
  mi_heap_delete(g);
  return (WIFEXITED(status) && WEXITSTATUS(status) == 0) ? 0 : 1;
}

// ---------------------------------------------------------------------------------------
// Case D: nested fork. The (first) child creates heap N and a worker thread for it, then
// forks a grandchild. The worker thread is a generation-1 tld from the grandchild's point
// of view -- it predates the grandchild's fork and does not survive it -- so deleting N in
// the grandchild must increase the counter, exactly like case C but one fork deeper. A
// fresh, main-thread-only heap in the grandchild must not move it again.
// ---------------------------------------------------------------------------------------
static int run_case_d(void) {
  fflush(stderr);
  const pid_t pid = fork();
  if (pid < 0) {
    fprintf(stderr, "case_d: fork() failed: %s\n", strerror(errno));
    return -1;
  }
  if (pid == 0) {
    arm_watchdog();

    mi_heap_t* n = mi_heap_new();
    parked_ctx_t ctx;
    parked_ctx_init(&ctx, n);
    pthread_t t;
    if (pthread_create(&t, NULL, parked_alloc_thread, &ctx) != 0) {
      fprintf(stderr, "case_d: could not start the generation-1 worker thread\n");
      _exit(7);
    }
    parked_wait_ready(&ctx);

    fflush(stderr);
    const pid_t gpid = fork();
    if (gpid < 0) {
      fprintf(stderr, "case_d: nested fork() failed: %s\n", strerror(errno));
      _exit(8);
    }
    if (gpid == 0) {
      arm_watchdog();   // fresh watchdog for the grandchild

      #if HAS_COUNTER
      const uintptr_t c0 = seized();
      #endif
      mi_heap_delete(n);
      #if HAS_COUNTER
      if (!(seized() > c0)) {
        fprintf(stderr, "case_d: FAIL (a generation-1 (stale) tld's heap did not force-seize in the grandchild: "
                        "counter stayed at %lu)\n", (unsigned long)c0);
        _exit(10);
      }
      const uintptr_t c1 = seized();
      #endif

      mi_heap_t* fresh = mi_heap_new();
      void* p = mi_heap_malloc(fresh, 64);
      mi_heap_delete(fresh);
      #if HAS_COUNTER
      if (seized() != c1) {
        fprintf(stderr, "case_d: FAIL (a fresh main-thread-only heap in the grandchild moved the force-seize "
                        "counter %lu -> %lu)\n", (unsigned long)c1, (unsigned long)seized());
        _exit(11);
      }
      #endif
      mi_free(p);
      _exit(0);
    }

    int gstatus = 0;
    waitpid(gpid, &gstatus, 0);

    parked_release(&ctx);
    pthread_join(t, NULL);
    parked_free_blocks(&ctx);
    parked_ctx_destroy(&ctx);
    mi_heap_delete(n);

    const int gcode = WIFEXITED(gstatus) ? WEXITSTATUS(gstatus) : 98;
    _exit(gcode);   // propagate the grandchild's failure (or 0) as this child's own status
  }

  int status = 0;
  waitpid(pid, &status, 0);
  return (WIFEXITED(status) && WEXITSTATUS(status) == 0) ? 0 : 1;
}

int main(void) {
  #if !HAS_COUNTER
  fprintf(stderr, "counter checks skipped (MI_DEBUG==0)\n");
  (void)seized();   // keep the Release fallback referenced so this build does not warn on an unused static
  #endif

  int rc = 0;

  int r = run_case_a();
  fprintf(stderr, "case_a: %s\n", r == 0 ? "ok" : "FAIL (pre-fork survivor heap; see child output above)");
  if (r != 0) { rc = 1; }

  r = run_case_b();
  fprintf(stderr, "case_b: %s\n", r == 0 ? "ok" : "FAIL (post-fork heap/thread; see child output above)");
  if (r != 0) { rc = 1; }

  r = run_case_c();
  fprintf(stderr, "case_c: %s\n", r == 0 ? "ok" : "FAIL (true pre-fork orphan; see child output above)");
  if (r != 0) { rc = 1; }

  r = run_case_d();
  fprintf(stderr, "case_d: %s\n", r == 0 ? "ok" : "FAIL (nested fork; see child output above)");
  if (r != 0) { rc = 1; }

  return rc;
}

#endif // !_WIN32 && !__wasi__
