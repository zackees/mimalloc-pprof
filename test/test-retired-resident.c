/* #575: a retired large page does not keep its whole block area resident.

   `_mi_page_retire` keeps the only page of a large size class when it empties, and #483 resets it
   to "nothing formed" but leaves its bytes resident until the scavenger releases them, MI_RETIRED_
   RELEASE_MULT purge delays later. A thread that once used ~10 large size classes therefore held
   ~10 empty resident pages: large-class-persistent/8 had 66 MiB of them, 43% of its peak RSS.
   A page reset to "nothing formed" re-forms its blocks from the start, so with
   `mi_option_retired_keep` = N the owner's heartbeat (`_mi_theap_collect_retired`) discards the
   block area past the first N blocks of a retired page (src/page-holes.c, `_mi_page_retired_trim`).

   Deterministic: two blocks in each of NBINS distinct large size classes (the two land in one
   page), touched (resident), then both freed so each bin's only page retires; one heartbeat; then
   `mincore` says whether the first and the second block still have resident memory. ctest turns the
   scavenger off (it would release everything later regardless) and page reserve off. Phase 2: the
   option at 0 keeps both blocks of every page resident (the run-time opt-out).
   Linux only (mincore); elsewhere the test does nothing. */

#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <mimalloc.h>
#include "mimalloc/internal.h"
#include "mimalloc/prim-tls.h"   // _mi_theap_default; MI_RETIRED_TRIM (undefined on a tree without #575: RED)

#if defined(__linux__)
#include <pthread.h>
#include <sys/mman.h>
#include <unistd.h>

#define NBINS (9)
static const size_t sizes[NBINS] = { 100, 120, 140, 170, 200, 240, 290, 350, 420 };   // KiB: distinct 12.5% classes
static long cap_for_thread;
static int is_resident(const void* p, size_t size);
static int first_resident, second_resident;

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

static int is_resident(const void* p, size_t size) { return resident_bytes(p, size) >= size / 2; }

static void* worker(void* arg) {
  (void)arg;
  void* first[NBINS]; void* second[NBINS];
#ifdef MI_RETIRED_TRIM
  mi_option_set(mi_option_retired_keep, cap_for_thread);
#endif
  for (int i = 0; i < NBINS; i++) {
    const size_t sz = sizes[i] * 1024;
    void* a = mi_malloc(sz);
    void* b = mi_malloc(sz);
    assert(a != NULL && b != NULL && _mi_ptr_page(a) == _mi_ptr_page(b));
    memset(a, 0xab, sz);
    memset(b, 0xab, sz);   // both resident
    first[i] = (a < b ? a : b);
    second[i] = (a < b ? b : a);
  }
  for (int i = 0; i < NBINS; i++) {
    assert(is_resident(first[i], sizes[i] * 1024) && is_resident(second[i], sizes[i] * 1024));   // the probe sees resident memory
    mi_free(first[i]); mi_free(second[i]);   // the bin's only page retires
  }
  _mi_theap_collect_retired(_mi_theap_default(), false);   // one heartbeat
  int n1 = 0, n2 = 0;
  for (int i = 0; i < NBINS; i++) {
    n1 += is_resident(first[i], sizes[i] * 1024);
    n2 += is_resident(second[i], sizes[i] * 1024);
  }
  first_resident = n1; second_resident = n2;
  return NULL;
}

static void run(long keep) {
  pthread_t t;
  cap_for_thread = keep;
  first_resident = second_resident = -1;
  assert(pthread_create(&t, NULL, worker, NULL) == 0);
  assert(pthread_join(t, NULL) == 0);
}

int main(void) {
  int failures = 0;
  run(1);
  fprintf(stderr, "retired-keep: keep 1 -> first blocks resident %d/%d, second blocks resident %d/%d\n", first_resident, NBINS, second_resident, NBINS);
  if (second_resident != 0) { fprintf(stderr, "FAILED: %d retired pages kept their second block resident with keep=1\n", second_resident); failures++; }
  if (first_resident != NBINS) { fprintf(stderr, "FAILED: the kept prefix was discarded (%d of %d resident)\n", first_resident, NBINS); failures++; }
  run(0);
  fprintf(stderr, "retired-keep: keep 0 -> first blocks resident %d/%d, second blocks resident %d/%d\n", first_resident, NBINS, second_resident, NBINS);
  if (first_resident != NBINS || second_resident != NBINS) { fprintf(stderr, "FAILED: the opt-out discarded memory\n"); failures++; }
  return failures == 0 ? 0 : 1;
}
#else
int main(void) { return 0; }
#endif
