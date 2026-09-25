/* #457: the DEFERRED arena purge must return freed memory while the process only idles.

   Adapted from @han1548772930's test in PR #467. Worker threads churn large blocks, free
   them, and then idle: no allocation, no `mi_on_thread_idle`, no collect. Only the
   scavenger's `purge_delay` deadline can give the memory back. Before the fix that deadline
   could be orphaned (`subproc->purge_expire == 0` while an arena still had a pending purge),
   so most of the freed memory stayed resident.

   Several threads are needed: concurrent churn is what makes the scavenger's pass race the
   frees that re-arm the arena deadline. ctest sets `MIMALLOC_PURGE_DELAY=20` so the deadline
   is observable in-process.

   The residual is asserted only where RSS is a statement about the allocator: not under ASan
   (quarantine), DHAT (per-block bookkeeping) or on Darwin (this purge does not move RSS
   there). Elsewhere the numbers are still printed. */

#if MI_TRACK_ASAN || MI_DHAT || defined(__APPLE__)
#  define MI_TEST_RSS_ASSERTED 0
#else
#  define MI_TEST_RSS_ASSERTED 1
#endif

#include <mimalloc.h>
#include "mimalloc/internal.h"   // _mi_release_bound_ms (#491)
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#if defined(_WIN32)
#include <windows.h>
typedef HANDLE thread_t;
static void sleep_ms(unsigned ms) { Sleep(ms); }
#else
#include <pthread.h>
#include <unistd.h>
typedef pthread_t thread_t;
static void sleep_ms(unsigned ms) { usleep(ms * 1000u); }
#endif

#define NTHREADS   (4)
#define BLOCK      (1u << 20)  /* above MI_LARGE_MAX_OBJ_SIZE: frees schedule a deferred arena purge */
#define BLOCKS     (48)        /* 48 MiB per thread */
#define ROUNDS     (30)
#define POLL_MS    (5)
#define RELEASE_TEST_MARGIN (4)  /* #491: idle up to this many release bounds; a loaded runner may delay the scavenger */

static volatile int g_done[NTHREADS];
static volatile int g_release = 0;
static volatile int g_failed = 0;

static size_t rss_bytes(void) {
  size_t elapsed, user, sys, rss, peak_rss, commit, peak_commit, faults;
  mi_process_info(&elapsed, &user, &sys, &rss, &peak_rss, &commit, &peak_commit, &faults);
  return rss;
}

static void worker_body(int idx) {
  void* p[BLOCKS];
  for (int r = 0; r < ROUNDS && !g_failed; r++) {
    for (int i = 0; i < BLOCKS; i++) {
      p[i] = mi_malloc(BLOCK);
      if (p[i] == NULL) { g_failed = 1; while (i-- > 0) { mi_free(p[i]); } break; }
      memset(p[i], (i + r) & 0xFF, BLOCK);
    }
    if (g_failed) break;
    for (int i = 0; i < BLOCKS; i++) { mi_free(p[i]); }
  }
  g_done[idx] = 1;
  while (!g_release) { sleep_ms(POLL_MS); }  /* stay alive: thread exit would release the memory */
}

#if defined(_WIN32)
static DWORD WINAPI worker_main(LPVOID a) { worker_body((int)(intptr_t)a); return 0; }
static void thread_start(thread_t* t, int idx) { *t = CreateThread(NULL, 0, &worker_main, (LPVOID)(intptr_t)idx, 0, NULL); }
static void thread_join(thread_t t) { WaitForSingleObject(t, INFINITE); CloseHandle(t); }
#else
static void* worker_main(void* a) { worker_body((int)(intptr_t)a); return NULL; }
static void thread_start(thread_t* t, int idx) { pthread_create(t, NULL, &worker_main, (void*)(intptr_t)idx); }
static void thread_join(thread_t t) { pthread_join(t, NULL); }
#endif

int main(void) {
  const size_t rss0 = rss_bytes();
  thread_t t[NTHREADS];
  for (int i = 0; i < NTHREADS; i++) { thread_start(&t[i], i); }

  size_t high = rss0;   /* high-water RSS while the workers churn */
  for (int done = 0; done < NTHREADS; sleep_ms(POLL_MS)) {
    const size_t now = rss_bytes();
    if (now > high) high = now;
    done = 0;
    for (int i = 0; i < NTHREADS; i++) { done += g_done[i]; }
  }
  const size_t held = high - rss0;

  /* #491: idle until the memory is back (the residual check below) or the release bound passes */
  size_t low = high;    /* lowest RSS over the idle window */
  const long limit = _mi_release_bound_ms() * RELEASE_TEST_MARGIN;
  long idled = 0;
  for (; idled <= limit && !g_failed && held >= 64u * 1024 * 1024; idled += POLL_MS) {
    const size_t now = rss_bytes();
    if (now < low) low = now;
    if ((low > rss0 ? low - rss0 : 0) <= held / 4) break;
    sleep_ms(POLL_MS);
  }
  g_release = 1;
  for (int i = 0; i < NTHREADS; i++) { thread_join(t[i]); }

  if (g_failed || held < 64u * 1024 * 1024) {
    fprintf(stderr, "test-arena-purge-rearm: skipped (allocation failed or only %zu MiB resident)\n", held >> 20);
    return 0;
  }
  const size_t residual = (low > rss0 ? low - rss0 : 0);
  fprintf(stderr, "test-arena-purge-rearm: held %zu MiB, residual after %ld ms idle %zu MiB (release bound %ld ms)\n",
          held >> 20, idled, residual >> 20, _mi_release_bound_ms());
#if MI_TEST_RSS_ASSERTED
  /* held/4, not tighter: RSS also moves for reasons outside the purge queue (runs with 0 bytes
     still queued have read up to ~24 MiB on a loaded runner), while the orphaned deadline this
     test exists for keeps ~190 of ~200 MiB resident. */
  if (residual > held / 4) {
    fprintf(stderr, "test-arena-purge-rearm: FAILED -- the deferred arena purge did not return the "
                    "freed memory; its deadline was orphaned (#457)\n");
    return 1;
  }
#endif
  return 0;
}
