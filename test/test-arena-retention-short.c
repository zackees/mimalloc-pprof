/* #506: only large and singleton pages freed by `free` keep #486's LONG retention window. Small
   and medium pages, and the buffer a growing realloc moved out of, go back to the OS within the
   SHORT one (one to two purge delays, as before #486).

   #486 (#500) retains freed arena memory for `arena_purge_mult` (MI_ARENA_PURGE_MULT_DEFAULT)
   purge delays so bursty large reuse finds it resident. Applied to every freed range it also held:
   - the small pages of a server workload (larson: 8-1000 B blocks freed by other threads), which
     are cheap to fault back and reused inside their own theap long before the arena sees them;
   - every buffer a growing Vec (a doubling realloc) outgrew, whose size nobody asks for again:
     held until the new buffer had filled, it made the growing buffer's peak three times its size
     instead of two -- the larson chart's harness grows one such log per table, and its peak RSS
     rose 10-30% over Bun's mimalloc.

   The test queues three things for purge within a few milliseconds of each other:
   - SMALL_PAGES pages of SMALL_SIZE blocks, filled by the main thread and freed by a second one
     (remote frees, as larson's rotating tables do); the main thread's collect hands them back;
   - the old block of a realloc growing a GROW_FROM buffer to GROW_TO (both singleton pages);
   - a plain freed block of LARGE_SIZE (a singleton page too: the long window).
   ctest sets `MIMALLOC_PURGE_DELAY` and turns the scavenger off, so only this test's
   `mi_collect(false)` calls run the arena purge, every POLL_MS. Residency is read with `mincore`
   (Linux only). The first two must be released while the large block is still resident; with one
   retention window for all three they go back in the same purge pass and the test fails. */

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

#define SMALL_SIZE           (64)                  // a small size class (64 KiB pages)
#define SMALL_PAGE_SIZE      (64 * 1024)           // MI_SMALL_PAGE_SIZE (the arena slice)
#define SMALL_PAGES          (16)                  // pages' worth of small blocks
#define SMALL_BLOCKS         (SMALL_PAGES * (SMALL_PAGE_SIZE / SMALL_SIZE))
#define LARGE_SIZE           (1024 * 1024)         // a singleton page: the long window
#define GROW_FROM            (1024 * 1024)         // a singleton page ...
#define GROW_TO              (4 * GROW_FROM)       // ... that a realloc outgrows (and moves)
#define POLL_MS              (2)
#define RELEASE_MAX_DELAYS   (100)                 // give up: never released
#define RELEASED_FRACTION    (4)                   // "released": at most 1/4 of the pages still resident

#if MI_TEST_RESIDENCY
static void* blocks[SMALL_BLOCKS];

static long now_ms(void) {
  struct timespec t;
  clock_gettime(CLOCK_MONOTONIC, &t);
  return (long)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}

// resident OS pages of the whole OS pages inside [p, p+size) (a block need not be OS-page aligned)
static size_t resident_pages(const void* p, size_t size) {
  static unsigned char vec[GROW_FROM / 4096];
  const uintptr_t psize = (uintptr_t)sysconf(_SC_PAGESIZE);
  const uintptr_t lo = ((uintptr_t)p + psize - 1) & ~(psize - 1);
  const uintptr_t hi = ((uintptr_t)p + size) & ~(psize - 1);
  if (hi <= lo) return 0;
  const size_t n = (size_t)((hi - lo) / psize);
  if (n > sizeof(vec) || mincore((void*)lo, (size_t)(hi - lo), vec) != 0) return 0;
  size_t resident = 0;
  for (size_t i = 0; i < n; i++) { resident += (vec[i] & 1); }
  return resident;
}

// the distinct small pages (64 KiB slices) the blocks live in
static uintptr_t spans[SMALL_BLOCKS];
static size_t span_count;

static void collect_spans(void) {
  for (size_t i = 0; i < SMALL_BLOCKS; i++) {
    const uintptr_t s = (uintptr_t)blocks[i] & ~(uintptr_t)(SMALL_PAGE_SIZE - 1);
    size_t j = 0;
    while (j < span_count && spans[j] != s) j++;
    if (j == span_count) spans[span_count++] = s;
  }
}

static size_t small_resident(void) {
  size_t resident = 0;
  for (size_t i = 0; i < span_count; i++) { resident += resident_pages((void*)spans[i], SMALL_PAGE_SIZE); }
  return resident;
}

static void* remote_free(void* arg) {
  (void)arg;
  for (size_t i = 0; i < SMALL_BLOCKS; i++) { mi_free(blocks[i]); }
  return NULL;
}

static bool released(size_t left, size_t total) { return (left * RELEASED_FRACTION <= total); }
#endif

int main(void) {
  #if MI_TEST_RESIDENCY
  if (mi_option_get(mi_option_arena_purge_mult) <= 1) {   // one window for all: nothing to tell apart
    fprintf(stderr, "skipped: arena_purge_mult <= 1\n");
    return 0;
  }
  for (size_t i = 0; i < SMALL_BLOCKS; i++) {
    blocks[i] = mi_malloc(SMALL_SIZE);
    if (blocks[i] == NULL) { fprintf(stderr, "allocation failed\n"); return 1; }
    memset(blocks[i], 1, SMALL_SIZE);
  }
  char* large = (char*)mi_malloc(LARGE_SIZE);
  char* grow  = (char*)mi_malloc(GROW_FROM);
  if (large == NULL || grow == NULL) { fprintf(stderr, "allocation failed\n"); return 1; }
  memset(large, 1, LARGE_SIZE);
  memset(grow, 1, GROW_FROM);
  collect_spans();
  const size_t small_total = small_resident();
  const size_t large_total = resident_pages(large, LARGE_SIZE);
  const size_t grow_total  = resident_pages(grow, GROW_FROM);

  pthread_t t;
  if (pthread_create(&t, NULL, &remote_free, NULL) != 0) { fprintf(stderr, "thread failed\n"); return 1; }
  pthread_join(t, NULL);
  mi_collect(false);    // the owner takes the remote frees: its empty pages go back to the arena
  const long start = now_ms();
  char* const outgrown = grow;
  grow = (char*)mi_realloc(grow, GROW_TO);
  if (grow == NULL || grow == outgrown) { fprintf(stderr, "the realloc did not move the buffer\n"); return 1; }
  memset(grow + GROW_FROM, 1, GROW_TO - GROW_FROM);
  mi_free(large);

  const long delay = mi_option_get(mi_option_purge_delay);
  size_t small_left = small_resident();
  size_t grow_left  = resident_pages(outgrown, GROW_FROM);
  size_t large_left = resident_pages(large, LARGE_SIZE);
  long released_at = -1;
  while (now_ms() - start < RELEASE_MAX_DELAYS * delay) {
    usleep(POLL_MS * 1000);
    mi_collect(false);
    small_left = small_resident();
    grow_left  = resident_pages(outgrown, GROW_FROM);
    large_left = resident_pages(large, LARGE_SIZE);
    if (released(small_left, small_total) && released(grow_left, grow_total)) { released_at = now_ms() - start; break; }
  }
  fprintf(stderr, "%zu small pages (%zu OS pages resident), an outgrown %d KiB buffer (%zu), a freed %d KiB block (%zu); purge delay %ld ms\n",
          span_count, small_total, GROW_FROM / 1024, grow_total, LARGE_SIZE / 1024, large_total, delay);
  fprintf(stderr, "after %ld ms: small pages %zu OS pages resident, outgrown buffer %zu, freed block %zu\n",
          released_at, small_left, grow_left, large_left);
  int failed = 0;
  if (released_at < 0) { fprintf(stderr, "FAILED: the small pages or the outgrown buffer were never released\n"); failed = 1; }
  else if (large_left * 4 < large_total * 3) {
    fprintf(stderr, "FAILED: the small pages and the outgrown buffer were held as long as a freed large block (one retention window for all)\n");
    failed = 1;
  }
  // and the large block still goes back, at the end of its own (long) window
  while (large_left > 0 && now_ms() - start < RELEASE_MAX_DELAYS * delay) {
    usleep(POLL_MS * 1000);
    mi_collect(false);
    large_left = resident_pages(large, LARGE_SIZE);
  }
  fprintf(stderr, "the freed large block released after %ld ms: %zu OS pages left\n", now_ms() - start, large_left);
  if (large_left > 0) { fprintf(stderr, "FAILED: the freed large block was never released\n"); failed = 1; }
  mi_free(grow);
  return failed;
  #else
  return 0;
  #endif
}
