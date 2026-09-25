/* #493 (strategy 4): refault feedback on the arena retention window.

   Freed arena memory stays resident for the #486 window (1-2 arena periods of `arena_purge_mult`
   x `purge_delay`) and is then purged. When a workload keeps claiming back memory right after the
   purge released it, the purge was premature: `mi_option_retain_feedback` (default on) then
   holds each purge deadline for more base periods (`mi_arena_retain_evaluate`, src/arena.c), and
   drops back to the base window as soon as a whole base period passes without any arena claim.

   The scenario, single-threaded (ctest turns the scavenger off and sets the purge delay, so only
   this test's `mi_collect(false)` calls run the arena purge, every POLL_MS):
   - the churn heap below is warmed up first: its first use allocates live metadata in the main
     arena, which must not land on (and pin) the tracked blocks' freed slices;
   - every round allocates and touches BLOCKS 1 MiB blocks (singleton pages: their slices go back
     to the arena on free), frees them, and polls until the arena purge has released them; from
     round 2 on the allocation claims those purged slices again -- the refault pattern;
   - between polls a churn heap in its own exclusive arena allocates and frees a 1 MiB block, so
     the process is not idle (it makes arena claims) without touching the tracked blocks' arena;
   - round 1 is released within the base window; by the last round the feedback must have raised
     the retention, so the blocks are still resident RETAIN_PROBE_PERIODS base periods after their
     free (on the code before strategy 4 they were released after two);
   - then the churn stops: with no claim for a base period the boost is dropped and the blocks go
     back within IDLE_MAX_PERIODS base periods, and within the #491 release bound;
   - with the option off the same rounds keep the base window.
   Residency is read with `mincore`, so the checks are Linux-only. */

#include <mimalloc.h>
#include "mimalloc/internal.h"   // _mi_release_bound_ms (#491)
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

#define BLOCK_SIZE            (1024 * 1024)   // a singleton page: its slices go back to the arena on free
#define BLOCKS                (4)
#define ROUNDS                (6)             // the boost rises by one per refaulted round (MI_RETAIN_BOOST_MAX is 3)
#define POLL_MS               (5)
#define RETAIN_PROBE_PERIODS  (3)             // the probe, in base arena periods: past the base window (2)
#define IDLE_MAX_PERIODS      (4)             // after the churn stops: the idle reset takes 2 periods at most
#define RELEASE_MAX_PERIODS   (40)            // give up: the memory was never released
#define RELEASE_TEST_MARGIN   (2)             // #491: a loaded runner may delay a collect

#if MI_TEST_RESIDENCY
static mi_heap_t* churn_heap;

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

static size_t resident_all(char** p) {
  size_t resident = 0;
  for (int i = 0; i < BLOCKS; i++) { resident += resident_pages(p[i]); }
  return resident;
}

// one poll: an arena claim in the churn arena (unless idle), then the arena purge
static void poll_once(int churn) {
  usleep(POLL_MS * 1000);
  if (churn) {
    void* c = mi_heap_malloc(churn_heap, BLOCK_SIZE);
    if (c != NULL) { memset(c, 3, 4096); mi_free(c); }
  }
  mi_collect(false);
}

// poll until `until_ms` after `start`, or until the blocks are released when `stop_when_released`;
// returns the resident page count at the end
static size_t poll_until(char** p, long start, long until_ms, int churn, int stop_when_released) {
  size_t resident = resident_all(p);
  while (now_ms() - start < until_ms) {
    poll_once(churn);
    resident = resident_all(p);
    if (stop_when_released && resident == 0) break;
  }
  return resident;
}

static int alloc_blocks(char** p) {
  for (int i = 0; i < BLOCKS; i++) {
    p[i] = (char*)mi_malloc(BLOCK_SIZE);
    if (p[i] == NULL) { while (i-- > 0) { mi_free(p[i]); } return 0; }
    memset(p[i], 1, BLOCK_SIZE);   // touch: the blocks are resident (and their slices dirty)
  }
  return 1;
}

static void free_blocks(char** p) {
  for (int i = 0; i < BLOCKS; i++) { mi_free(p[i]); }
}

// Run ROUNDS refault rounds. Returns 0 on success. `boosted`: the last round must still be
// resident at the probe (feedback on); otherwise it must be released by then (the base window).
static int run_rounds(const char* name, long period, int boosted) {
  const size_t total = (size_t)BLOCKS * (BLOCK_SIZE / (size_t)sysconf(_SC_PAGESIZE));
  const long probe = RETAIN_PROBE_PERIODS * period;
  char* p[BLOCKS];
  for (int round = 1; round <= ROUNDS; round++) {
    if (!alloc_blocks(p)) { fprintf(stderr, "%s: allocation failed\n", name); return 1; }
    const long start = now_ms();
    free_blocks(p);
    const size_t kept = poll_until(p, start, probe, 1, !boosted || round < ROUNDS);
    const long at = now_ms() - start;
    if (round == ROUNDS && boosted) {
      fprintf(stderr, "%s: round %d: %zu of %zu OS pages resident after %ld ms (base period %ld ms)\n",
              name, round, kept, total, at, period);
      if (kept * 4 < total * 3) {
        fprintf(stderr, "%s: FAILED: the refaulted blocks were released inside %d base periods: the retention was not raised\n",
                name, RETAIN_PROBE_PERIODS);
        return 1;
      }
      // idle: no claims any more. The boost must drop and the blocks go back within the base window.
      const long idle_start = now_ms();
      const long limit = _mi_release_bound_ms() * RELEASE_TEST_MARGIN;
      const size_t left = poll_until(p, idle_start, limit, 0, 1);
      const long idled = now_ms() - idle_start;
      fprintf(stderr, "%s: idle: %zu OS pages resident after %ld ms (release bound %ld ms)\n",
              name, left, idled, _mi_release_bound_ms());
      if (left > 0) {
        fprintf(stderr, "%s: FAILED: not released within %d x the release bound once idle\n", name, RELEASE_TEST_MARGIN);
        return 1;
      }
      if (idled > IDLE_MAX_PERIODS * period) {
        fprintf(stderr, "%s: FAILED: idle release took %ld ms, over %d base periods: the boost was not dropped\n",
                name, idled, IDLE_MAX_PERIODS);
        return 1;
      }
      return 0;
    }
    fprintf(stderr, "%s: round %d: %zu of %zu OS pages resident after %ld ms\n", name, round, kept, total, at);
    if (round == 1 || !boosted) {
      if (kept > 0) {   // the base window: released within two periods, well before the probe
        fprintf(stderr, "%s: FAILED: round %d not released within %d base periods\n", name, round, RETAIN_PROBE_PERIODS);
        return 1;
      }
    }
    else if (kept > 0) {   // boosted: still waiting for the release before the next round
      if (poll_until(p, start, RELEASE_MAX_PERIODS * period, 1, 1) > 0) {
        fprintf(stderr, "%s: FAILED: round %d never released\n", name, round);
        return 1;
      }
    }
  }
  return 0;
}
#endif

int main(void) {
  #if MI_TEST_RESIDENCY
  const long delay = mi_option_get(mi_option_purge_delay);
  const long mult  = mi_option_get(mi_option_arena_purge_mult);
  if (delay <= 0 || mult <= 0) {
    fprintf(stderr, "skipped: the arena purge is not deferred (purge_delay %ld, arena_purge_mult %ld)\n", delay, mult);
    return 0;
  }
  const long period = delay * mult;   // the base arena period
  mi_arena_id_t churn_arena;
  if (mi_reserve_os_memory_ex(64 * 1024 * 1024, false /* commit */, false /* allow large */, true /* exclusive */, &churn_arena) != 0) {
    fprintf(stderr, "skipped: could not reserve the churn arena\n");
    return 0;
  }
  churn_heap = mi_heap_new_in_arena(churn_arena);
  if (churn_heap == NULL) { fprintf(stderr, "skipped: no churn heap\n"); return 0; }
  // Warm the churn heap up BEFORE round 1. Its first allocation on this thread creates the heap's
  // per-thread state, and part of that is metadata taken from the MAIN arena that stays live. Done
  // in the first poll, right after round 1's free, resident-first placed it on the tracked blocks'
  // still-queued slices (2 slices stayed resident for good: round 1 "not released").
  poll_once(1);
  const long feedback = mi_option_get(mi_option_retain_feedback);
  mi_option_set(mi_option_retain_feedback, 1);
  int result = run_rounds("feedback on", period, 1);
  mi_option_set(mi_option_retain_feedback, 0);
  result |= run_rounds("feedback off", period, 0);
  mi_option_set(mi_option_retain_feedback, feedback);
  return result;
  #else
  char* p = (char*)mi_malloc(BLOCK_SIZE);
  if (p != NULL) { memset(p, 1, BLOCK_SIZE); }
  mi_free(p);
  return 0;
  #endif
}
