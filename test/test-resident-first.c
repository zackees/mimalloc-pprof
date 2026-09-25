/* #493 (strategy 9): a new page is placed on free arena slices that are still resident first.

   A freed range waits in the arena purge queue for the #486 retention window: free, and still
   resident. The plain free-slice search knows nothing of residency, so it can hand out purged
   (or fresh) slices while such a range is waiting -- growing RSS and zero-fill faulting the new
   page. `mi_option_resident_first` (default on) claims the queued range first.

   The setup makes the plain search pick the WRONG range. B is allocated first, so it sits at the
   lower slice index, and A right after it; both are 1 MiB singleton pages, so both ranges have
   the same slice count and live in the same size-binned chunk. B is freed and polled until the
   arena purge has released it (non-resident, no longer queued); only then is A freed, so A is
   queued and resident. The plain search is first-fit from the start of that chunk, so it finds
   B's range first -- the purged one. With resident-first, the next 1 MiB block C must land on
   A's range instead. The check is on addresses, so it is deterministic.

   Single-threaded: ctest turns the scavenger off and sets the purge delay, so only this test's
   `mi_collect(false)` calls run the arena purge. Residency is read with `mincore`, so the checks
   are Linux-only (elsewhere the test only runs the sequence). The second sub-case runs the same
   sequence with the option off (the plain search only) and asserts nothing about placement. */

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

#define BLOCK_SIZE          (1024 * 1024)   // a singleton page: its slices go back to the arena on free
#define POLL_MS             (5)
#define RELEASE_MAX_DELAYS  (200)           // give up: B was never released (a setup failure)

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

// collect every POLL_MS until `p` (freed) is no longer resident; false if that never happens
static int collect_until_released(void* p) {
  const long bound = RELEASE_MAX_DELAYS * mi_option_get(mi_option_purge_delay);
  const long start = now_ms();
  while (resident_pages(p) > 0) {
    if (now_ms() - start > bound) return 0;
    usleep(POLL_MS * 1000);
    mi_collect(false);
  }
  return 1;
}
#endif

static int overlaps(const char* x, const char* y) {
  return (x < y + BLOCK_SIZE && y < x + BLOCK_SIZE);
}

// Run the sequence; returns 0 on success. `check` asserts that C lands on A's (resident) range.
static int run_case(const char* name, int check) {
  char* b = (char*)mi_malloc(BLOCK_SIZE);   // first: the lower slice index
  char* a = (char*)mi_malloc(BLOCK_SIZE);
  if (a == NULL || b == NULL) { fprintf(stderr, "%s: allocation failed\n", name); return 1; }
  memset(b, 1, BLOCK_SIZE);
  memset(a, 1, BLOCK_SIZE);
  mi_free(b);
  #if MI_TEST_RESIDENCY
  if (!collect_until_released(b)) {
    fprintf(stderr, "%s: FAILED (setup): B was never released by the arena purge\n", name);
    mi_free(a);
    return 1;
  }
  #endif
  mi_free(a);                                // queued for purge: free and still resident
  char* c = (char*)mi_malloc(BLOCK_SIZE);    // no collect in between: A is still queued
  if (c == NULL) { fprintf(stderr, "%s: allocation failed\n", name); return 1; }
  #if MI_TEST_RESIDENCY
  const size_t c_resident = resident_pages(c);
  #endif
  memset(c, 2, BLOCK_SIZE);
  fprintf(stderr, "%s: B=%p A=%p C=%p (C on A: %s, on B: %s)\n", name, (void*)b, (void*)a, (void*)c,
          overlaps(c, a) ? "yes" : "no", overlaps(c, b) ? "yes" : "no");
  int result = 0;
  #if MI_TEST_RESIDENCY
  if (check) {
    if (!(b < a)) {
      // the premise (the plain search meets B first) does not hold, so the check proves nothing
      fprintf(stderr, "%s: FAILED (setup): B is not below A\n", name);
      result = 1;
    }
    else if (!overlaps(c, a)) {
      fprintf(stderr, "%s: FAILED: C was not placed on the resident range A (%zu of its pages resident before use)\n", name, c_resident);
      result = 1;
    }
  }
  #else
  (void)check;
  #endif
  mi_free(c);
  return result;
}

int main(void) {
  #if defined(MI_GUARDED) && MI_GUARDED
  if (mi_option_get(mi_option_guarded_sample_rate) != 0) {   // takes the plain search: see `mi_arena_try_claim_resident`
    fprintf(stderr, "skipped: guarded sampling is on, so resident-first is off\n");
    return 0;
  }
  #endif
  // resident-first: C must reuse A's resident slices, not B's purged ones (wants a fresh arena)
  const long resident_first = mi_option_get(mi_option_resident_first);
  mi_option_set(mi_option_resident_first, 1);
  int result = run_case("resident-first", 1);
  // the plain search only: run the sequence, the placement is whatever the search picks
  mi_option_set(mi_option_resident_first, 0);
  result |= run_case("plain search", 0);
  mi_option_set(mi_option_resident_first, resident_first);
  return result;
}
