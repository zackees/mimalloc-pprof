/* #506: small-object pages go back to the OS within the SHORT retention window, while a large
   page keeps #500's long one.

   #500 raised the default `arena_purge_mult` from 1 to MI_ARENA_PURGE_MULT_DEFAULT (4), so a freed
   arena range stays resident for 4 to 8 purge delays before it is released. That retention was
   meant for large, bursty reuse (random-large-bursty/8), but it applies to every freed arena
   range -- including the 64 KiB small-object pages a remote thread empties -- and so Larson's peak
   RSS rose 10-30% against Bun. The fix keeps the long window for large pages only.

   Case A: the main thread allocates SMALL_BYTES of SMALL_SIZE blocks (dozens of small pages), a
   second thread frees every one of them (remote frees), and the main thread then calls
   `mi_collect(false)` every POLL_MS: that collects the remote frees, frees the emptied pages to
   the arena and runs the arena purge (ctest turns the scavenger off). The pages must be released
   before SHORT_PROBE_DELAYS purge delays -- past the short window's 2 delays, but before the long
   window's MI_ARENA_PURGE_MULT_DEFAULT delays can open.

   Case B: LARGE_COUNT blocks of LARGE_SIZE (a large-page size class, not a singleton) are freed
   on the main thread and must still be resident at SHORT_PROBE_DELAYS purge delays (#500's win).

   Residency is read with `mincore`, so the checks are Linux-only. Guarded builds (where every
   sampled block is its own guarded page) skip. */

#include <mimalloc.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#if defined(__linux__)
#include <pthread.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>
#define MI_TEST_RESIDENCY 1
#else
#define MI_TEST_RESIDENCY 0
#endif

#define SMALL_SIZE             (1000)              // a small size class: 64 KiB small pages
#define SMALL_BYTES            (4 * 1024 * 1024)   // bytes of small blocks: dozens of small pages
#define SMALL_COUNT            (SMALL_BYTES / SMALL_SIZE)
#define LARGE_SIZE             (256 * 1024)        // a large-page size class, not a singleton
#define LARGE_COUNT            (8)
#define POLL_MS                (5)
#define SHORT_PROBE_DELAYS     (3)                 // the probe, in purge delays
#define RESIDENT_BEFORE_PCT    (90)                // case A precondition: the range starts resident
#define RELEASED_MAX_PCT       (25)                // case A: released once at most this is resident
#define LARGE_KEPT_MIN_PCT     (75)                // case B: at least this must still be resident

#if MI_TEST_RESIDENCY
static long now_ms(void) {
  struct timespec t;
  clock_gettime(CLOCK_MONOTONIC, &t);
  return (long)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}

static uintptr_t align_down(uintptr_t x, uintptr_t a) { return x - (x % a); }
static uintptr_t align_up(uintptr_t x, uintptr_t a) { return align_down(x + a - 1, a); }

// resident OS pages in the page-aligned range [start, end); `*total` receives the page count
static size_t resident_range(uintptr_t start, uintptr_t end, size_t* total) {
  const size_t psize = (size_t)sysconf(_SC_PAGESIZE);
  const size_t n = (size_t)(end - start) / psize;
  *total = n;
  unsigned char* vec = (unsigned char*)calloc(n == 0 ? 1 : n, 1);   // libc: not the allocator under test
  if (vec == NULL) return 0;
  size_t resident = 0;
  if (mincore((void*)start, (size_t)(end - start), vec) == 0) {
    for (size_t i = 0; i < n; i++) { resident += (vec[i] & 1); }
  }
  free(vec);
  return resident;
}

static size_t resident_block(uintptr_t p, size_t size, size_t* total) {
  const uintptr_t psize = (uintptr_t)sysconf(_SC_PAGESIZE);
  return resident_range(align_down(p, psize), align_up(p + size, psize), total);
}

typedef struct remote_free_s {
  void** blocks;
  size_t count;
} remote_free_t;

static void* remote_free_all(void* arg) {
  remote_free_t* r = (remote_free_t*)arg;
  for (size_t i = 0; i < r->count; i++) { mi_free(r->blocks[i]); }
  return NULL;
}

// Case A: small pages emptied by remote frees are released inside the short window.
static int small_pages_released_short(long delay) {
  void** blocks = (void**)calloc(SMALL_COUNT, sizeof(void*));
  if (blocks == NULL) { fprintf(stderr, "calloc failed\n"); return 1; }
  uintptr_t lo = UINTPTR_MAX, hi = 0;
  for (size_t i = 0; i < SMALL_COUNT; i++) {
    blocks[i] = mi_malloc(SMALL_SIZE);
    if (blocks[i] == NULL) { fprintf(stderr, "allocation failed\n"); free(blocks); return 1; }
    memset(blocks[i], 1, SMALL_SIZE);
    const uintptr_t a = (uintptr_t)blocks[i];
    if (a < lo) lo = a;
    if (a > hi) hi = a;
  }
  const uintptr_t psize = (uintptr_t)sysconf(_SC_PAGESIZE);
  const uintptr_t start = align_down(lo, psize);
  const uintptr_t end = align_up(hi + SMALL_SIZE, psize);

  remote_free_t r = { blocks, SMALL_COUNT };
  pthread_t t;
  if (pthread_create(&t, NULL, &remote_free_all, &r) != 0) { fprintf(stderr, "pthread_create failed\n"); free(blocks); return 1; }
  pthread_join(t, NULL);
  free(blocks);

  size_t total = 0;
  size_t resident = resident_range(start, end, &total);
  if (total == 0 || resident * 100 < total * RESIDENT_BEFORE_PCT) {
    fprintf(stderr, "FAILED: small range only %zu of %zu OS pages resident before the clock (test would be vacuous)\n",
            resident, total);
    return 1;
  }

  const long clock_start = now_ms();
  const long limit = SHORT_PROBE_DELAYS * delay;
  int released = 0;
  long elapsed = 0;
  while ((elapsed = now_ms() - clock_start) < limit) {
    usleep(POLL_MS * 1000);
    mi_collect(false);
    resident = resident_range(start, end, &total);
    elapsed = now_ms() - clock_start;
    if (resident * 100 <= total * RELEASED_MAX_PCT) { released = (elapsed < limit); break; }
  }
  fprintf(stderr, "small pages (remote free): %zu of %zu OS pages resident after %ld ms (limit %ld ms)\n",
          resident, total, elapsed, limit);
  if (!released) {
    fprintf(stderr, "FAILED: small pages not released within the short retention window\n");
    return 1;
  }
  return 0;
}

// Case B: a large page keeps the long retention window (#500).
static int large_page_kept_long(long delay) {
  void* blocks[LARGE_COUNT];
  uintptr_t addrs[LARGE_COUNT];   // residency is read by address once the blocks are freed
  for (size_t i = 0; i < LARGE_COUNT; i++) {
    blocks[i] = mi_malloc(LARGE_SIZE);
    if (blocks[i] == NULL) { fprintf(stderr, "allocation failed\n"); return 1; }
    memset(blocks[i], 1, LARGE_SIZE);
    addrs[i] = (uintptr_t)blocks[i];
  }
  const long clock_start = now_ms();
  for (size_t i = 0; i < LARGE_COUNT; i++) { mi_free(blocks[i]); }
  // Poll through the short window. If the page is kept by the #483 retired-page slots (never freed
  // to the arena) it simply stays resident, which is also a pass: the point is that the short
  // window must not release it.
  while (now_ms() - clock_start < SHORT_PROBE_DELAYS * delay) {
    usleep(POLL_MS * 1000);
    mi_collect(false);
  }
  size_t kept = 0, total = 0;
  for (size_t i = 0; i < LARGE_COUNT; i++) {
    size_t n = 0;
    kept += resident_block(addrs[i], LARGE_SIZE, &n);
    total += n;
  }
  fprintf(stderr, "large page: %zu of %zu OS pages resident after %ld ms\n",
          kept, total, now_ms() - clock_start);
  if (total == 0 || kept * 100 < total * LARGE_KEPT_MIN_PCT) {
    fprintf(stderr, "FAILED: large page released inside the short window (the long retention is lost)\n");
    return 1;
  }
  return 0;
}
#endif

int main(void) {
  if (mi_option_get(mi_option_guarded_sample_rate) != 0) {
    fprintf(stderr, "skipped: guarded sampling is on (every sampled block is its own guarded page)\n");
    return 0;
  }
  #if MI_TEST_RESIDENCY
  const long delay = mi_option_get(mi_option_purge_delay);
  if (small_pages_released_short(delay) != 0) return 1;
  if (large_page_kept_long(delay) != 0) return 1;
  #else
  void* p = mi_malloc(SMALL_SIZE);
  mi_free(p);
  void* q = mi_malloc(LARGE_SIZE);
  mi_free(q);
  #endif
  return 0;
}
