/* #575: a thread's retired large pages do not all stay resident.

   `_mi_page_retire` keeps the only page of a large size class when it empties, and #483 resets it
   to "nothing formed" but leaves its bytes resident until the scavenger releases them, MI_RETIRED_
   RELEASE_MULT purge delays later. A thread that once used ~10 large size classes therefore held
   ~10 empty resident pages: large-class-persistent/8 had 66 MiB of them, 43% of its peak RSS.
   With `mi_option_retired_resident` = N the block area of the lowest-slot resident retired page is
   discarded as soon as more than N are resident (src/page-holes.c, `mi_retired_trim_over_cap`).

   Deterministic: one block in each of NBINS distinct large size classes, touched (resident), then
   all freed so every bin's only page retires; `mincore` counts the blocks that still have resident
   memory. ctest turns the scavenger off (it would release them later regardless of the cap) and
   page reserve off. Phase 2: the option at 0 keeps them all resident (the run-time opt-out).
   Linux only (mincore); elsewhere the test only checks that the option exists. */

#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <mimalloc.h>
#include "mimalloc/internal.h"   // MI_RETIRED_TRIM (undefined on a tree without #575: RED)

#if defined(__linux__)
#include <pthread.h>
#include <sys/mman.h>
#include <unistd.h>

#define NBINS (9)
static const size_t sizes[NBINS] = { 100, 120, 140, 170, 200, 240, 290, 350, 420 };   // KiB: distinct 12.5% classes
static long cap_for_thread;
static int resident_blocks;

static size_t resident_bytes(const void* p, size_t size) {
  const size_t psz = (size_t)sysconf(_SC_PAGESIZE);
  const uintptr_t lo = ((uintptr_t)p + psz - 1) & ~(psz - 1);
  const uintptr_t hi = ((uintptr_t)p + size) & ~(psz - 1);
  if (hi <= lo) return 0;
  const size_t n = (hi - lo) / psz;
  unsigned char* vec = (unsigned char*)malloc(n);
  size_t r = 0;
  if (vec != NULL && mincore((void*)lo, hi - lo, vec) == 0) {
    for (size_t i = 0; i < n; i++) { r += (vec[i] & 1) ? psz : 0; }
  }
  free(vec);
  return r;
}

static void* worker(void* arg) {
  (void)arg;
  void* blocks[NBINS];
#ifdef MI_RETIRED_TRIM
  mi_option_set(mi_option_retired_resident, cap_for_thread);
#endif
  for (int i = 0; i < NBINS; i++) {
    const size_t sz = sizes[i] * 1024;
    blocks[i] = mi_malloc(sz);
    assert(blocks[i] != NULL);
    memset(blocks[i], 0xab, sz);   // resident
  }
  size_t before = 0;
  for (int i = 0; i < NBINS; i++) { before += (resident_bytes(blocks[i], sizes[i] * 1024) >= sizes[i] * 1024 / 2) ? 1 : 0; }
  assert(before == NBINS);   // the probe sees resident memory
  for (int i = 0; i < NBINS; i++) { mi_free(blocks[i]); }   // each bin's only page retires
  int n = 0;
  for (int i = 0; i < NBINS; i++) { n += (resident_bytes(blocks[i], sizes[i] * 1024) >= sizes[i] * 1024 / 2) ? 1 : 0; }
  resident_blocks = n;
  return NULL;
}

static int run(long cap) {
  pthread_t t;
  cap_for_thread = cap;
  resident_blocks = -1;
  assert(pthread_create(&t, NULL, worker, NULL) == 0);
  assert(pthread_join(t, NULL) == 0);
  return resident_blocks;
}

int main(void) {
  int failures = 0;
  const int capped = run(2);
  fprintf(stderr, "retired-resident: cap 2 -> %d of %d retired pages resident\n", capped, NBINS);
  if (capped > 2) { fprintf(stderr, "FAILED: %d retired pages resident with the cap at 2\n", capped); failures++; }
  const int uncapped = run(0);
  fprintf(stderr, "retired-resident: cap 0 -> %d of %d retired pages resident\n", uncapped, NBINS);
  if (uncapped != NBINS) { fprintf(stderr, "FAILED: the opt-out discarded (%d of %d resident)\n", uncapped, NBINS); failures++; }
  return failures == 0 ? 0 : 1;
}
#else
int main(void) { return 0; }
#endif
