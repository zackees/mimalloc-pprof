/* #497: the two-generation arena purge (#481) must not lose the aged part of a run.

   A freed range is queued young, aged at the next purge deadline, and purged at the one
   after. The aged queue is walked in maximal runs, so two adjacent freed blocks form ONE run.
   If one of them is reused and freed again inside the period, it is young again -- and the
   other block must still be purged: it did stay free for a whole period.

   Deterministic and single-threaded: ctest turns the scavenger off and sets the purge delay,
   so only the `mi_collect(false)` calls below run the arena purge. Residency is read with
   `mincore`, so the check is Linux-only (elsewhere the test only runs the sequence). */

#include <mimalloc.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#if defined(__linux__)
#include <sys/mman.h>
#include <unistd.h>
#define MI_TEST_RESIDENCY 1
#else
#define MI_TEST_RESIDENCY 0
#endif

#define BLOCK_SIZE  (1024 * 1024)   // a singleton page: its slices go back to the arena on free
#define PERIOD_MS   (60)            // > MIMALLOC_PURGE_DELAY (set by ctest)
#define MAX_PERIODS (50)            // give up after this many: the purge was lost, not late

static void sleep_ms(unsigned ms) {
  #if MI_TEST_RESIDENCY
  usleep(ms * 1000u);
  #else
  (void)ms;
  #endif
}

static size_t resident_pages(void* p, size_t size) {
  #if MI_TEST_RESIDENCY
  const size_t psize = (size_t)sysconf(_SC_PAGESIZE);
  unsigned char vec[BLOCK_SIZE / 4096];
  const size_t n = size / psize;
  if (n > sizeof(vec) || mincore(p, size, vec) != 0) return 0;
  size_t resident = 0;
  for (size_t i = 0; i < n; i++) { resident += (vec[i] & 1); }
  return resident;
  #else
  (void)p; (void)size;
  return 0;
  #endif
}

int main(void) {
  char* a = (char*)mi_malloc(BLOCK_SIZE);
  char* b = (char*)mi_malloc(BLOCK_SIZE);
  if (a == NULL || b == NULL) { fprintf(stderr, "allocation failed\n"); return 1; }
  memset(a, 1, BLOCK_SIZE);
  memset(b, 1, BLOCK_SIZE);
  mi_free(a);
  mi_free(b);                  // both queued young: one run in the purge bitmap
  sleep_ms(PERIOD_MS);
  mi_collect(false);           // first deadline: the run is aged, not purged
  char* c = (char*)mi_malloc(BLOCK_SIZE);   // reuses one of the two blocks' slices
  if (c == NULL) { fprintf(stderr, "allocation failed\n"); return 1; }
  memset(c, 1, BLOCK_SIZE);
  mi_free(c);                  // ... which is young again
  char* const other = (c == a ? b : c == b ? a : NULL);
  if (other == NULL) {
    fprintf(stderr, "skipped: the reuse did not land on either block\n");
    return 0;
  }
  // The other block has been free for a whole period: the next deadline purges it. Collect every
  // period until it is gone, up to MAX_PERIODS -- a loaded runner may run a collect late, but a
  // lost purge is never retried, so it still fails then.
  size_t resident = 0;
  int periods = 0;
  do {
    sleep_ms(PERIOD_MS);
    mi_collect(false);
    resident = resident_pages(other, BLOCK_SIZE);
  } while (resident > 0 && ++periods < MAX_PERIODS);
  fprintf(stderr, "the block that stayed free: %zu of %d OS pages resident after %d period(s)\n", resident, BLOCK_SIZE / 4096, periods + 1);
  #if MI_TEST_RESIDENCY
  if (resident > 0) {   // (main before the fix: 32 of 256 -- the slices of the run past the reused block)
    fprintf(stderr, "FAILED: part of its aged purge was lost\n");
    return 1;
  }
  #endif
  return 0;
}
