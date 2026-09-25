/* #486: freed arena memory is retained for a window before it goes back to the OS.

   A freed block stays resident for at least one arena purge period (`arena_purge_mult` x
   `purge_delay`), so a reuse inside that window finds it resident instead of refaulting it;
   after the window it is released. ctest sets `MIMALLOC_PURGE_DELAY=20` and turns the scavenger
   off, so only this test's `mi_collect(false)` calls run the arena purge, every POLL_MS.

   Probed at RETAIN_PROBE_DELAYS purge delays: past the old window (1-2 delays with
   `arena_purge_mult` 1) but inside the new one (at least MI_ARENA_PURGE_MULT_DEFAULT delays).
   Residency is read with `mincore`, so the checks are Linux-only. */

#include <mimalloc.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#if defined(__linux__)
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>
#define MI_TEST_RESIDENCY 1
#else
#define MI_TEST_RESIDENCY 0
#endif

#define BLOCK_SIZE           (1024 * 1024)   // a singleton page: its slices go back to the arena on free
#define POLL_MS              (5)
#define RETAIN_PROBE_DELAYS  (3)             // the probe, in purge delays
#define RELEASE_MAX_DELAYS   (100)           // give up: the memory was never released

#if MI_TEST_RESIDENCY
static long now_ms(void) {
  struct timespec t;
  clock_gettime(CLOCK_MONOTONIC, &t);
  return (long)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}

static size_t resident_pages(void* p) {
  unsigned char vec[BLOCK_SIZE / 4096];
  const size_t psize = (size_t)sysconf(_SC_PAGESIZE);
  const size_t n = BLOCK_SIZE / psize;
  if (n > sizeof(vec) || mincore(p, BLOCK_SIZE, vec) != 0) return 0;
  size_t resident = 0;
  for (size_t i = 0; i < n; i++) { resident += (vec[i] & 1); }
  return resident;
}

// collect every POLL_MS until `until_ms` after `start`, or until `stop(resident)` holds
static size_t collect_until(void* p, long start, long until_ms, int stop_when_released) {
  size_t resident = resident_pages(p);
  while (now_ms() - start < until_ms) {
    usleep(POLL_MS * 1000);
    mi_collect(false);
    resident = resident_pages(p);
    if (stop_when_released && resident == 0) break;
  }
  return resident;
}
#endif

int main(void) {
  char* p = (char*)mi_malloc(BLOCK_SIZE);
  if (p == NULL) { fprintf(stderr, "allocation failed\n"); return 1; }
  memset(p, 1, BLOCK_SIZE);
  #if MI_TEST_RESIDENCY
  const long delay = mi_option_get(mi_option_purge_delay);
  const size_t total = BLOCK_SIZE / (size_t)sysconf(_SC_PAGESIZE);
  const long start = now_ms();
  mi_free(p);
  const size_t kept = collect_until(p, start, RETAIN_PROBE_DELAYS * delay, 0);
  const size_t left = collect_until(p, start, RELEASE_MAX_DELAYS * delay, 1);
  fprintf(stderr, "freed block: %zu of %zu OS pages resident after %ld ms, %zu after %ld ms\n",
          kept, total, RETAIN_PROBE_DELAYS * delay, left, now_ms() - start);
  if (kept * 4 < total * 3) { fprintf(stderr, "FAILED: released inside the retention window\n"); return 1; }
  if (left > 0) { fprintf(stderr, "FAILED: never released\n"); return 1; }
  #else
  mi_free(p);
  #endif
  return 0;
}
