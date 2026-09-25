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
   sequence with the option off (the plain search only) and asserts nothing about placement.
   The third (`run_slack_case`, PR #501) checks that a page carved from reused memory gives back
   the stale memory past its last block. */

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

/* PR #501: a page carved from reused memory must not keep the stale part it never uses.

   Large pages of OLD_SIZE blocks are formed and written in full, then freed back to the arena
   (queued, so still resident). A large page of NEW_SIZE blocks is carved from that memory: it
   holds fewer bytes of blocks, so part of the old blocks lies in its slack, past its last block,
   where neither the hole sweep nor the retired-page release (#483) ever looks. That part must go
   back to the OS -- checked while the new page is live, after collecting past the release bound;
   an idle thread's retired pages used to keep it for good.

   Page boundaries are not visible from outside, and adjacent large pages look like one run of
   blocks at a constant stride (the header gap of a page is exactly the room its predecessor
   leaves), so the first new page is found by its first block `nw[0]` (the page start is at most
   SLACK_START_MAX before it) and its block count by where the stride first breaks -- or by
   LARGE_PAGE_SIZE. The checked region runs from its last block's end to the earliest possible
   page end, `nw[0] - SLACK_START_MAX + LARGE_PAGE_SIZE`, so it is slack of that page for sure.
   With these sizes the old pages form 31 blocks each and the new one 10, so the region holds
   ~128 KiB of written old blocks (in a build that rounds the sizes up, e.g. guarded, it may be
   empty: nothing to check). */
#define OLD_SIZE         (128 * 1024 - 256)   // the 128 KiB bin, with room for debug padding
#define NEW_SIZE         (384 * 1024 - 256)   // the 384 KiB bin
#define OLD_COUNT        (64)                 // two large pages and a bit
#define NEW_MAX          (16)
#define RETIRE_COLLECTS  (16)                 // > the retire cycles of a large page: it goes back to the arena
#define LARGE_PAGE_SIZE  ((size_t)4 * 1024 * 1024)   // MI_LARGE_PAGE_SIZE on 64-bit
#define SLACK_START_MAX  ((size_t)64 * 1024)  // a page's first block starts less than a slice in

static int run_slack_case(void) {
  #if MI_TEST_RESIDENCY && (UINTPTR_MAX > 0xFFFFFFFFu)
  const size_t psize = (size_t)sysconf(_SC_PAGESIZE);
  // the old blocks: written in full, then all freed (queued in the arena, still resident)
  char* old[OLD_COUNT];
  for (size_t i = 0; i < OLD_COUNT; i++) {
    old[i] = (char*)mi_malloc(OLD_SIZE);
    if (old[i] == NULL) { fprintf(stderr, "slack: allocation failed\n"); return 1; }
    memset(old[i], 1, OLD_SIZE);
  }
  for (size_t i = 0; i < OLD_COUNT; i++) { mi_free(old[i]); }
  for (int i = 0; i < RETIRE_COLLECTS; i++) { mi_collect(false); }   // a retired page back to the arena too
  // the new blocks: the first new page, and the first block after it
  char* nw[NEW_MAX];
  size_t nnew = 0;
  while (nnew < NEW_MAX) {
    char* p = (char*)mi_malloc(NEW_SIZE);
    if (p == NULL) break;
    nw[nnew++] = p;
    if (nnew >= 3 && p != nw[nnew-2] + (nw[1] - nw[0])) break;         // the stride broke: the next page
  }
  int result = 0;
  if (nnew < 3) {
    fprintf(stderr, "slack: allocation failed\n");
    result = 1;
  }
  else {
    const size_t stride = (size_t)(nw[1] - nw[0]);
    size_t k = 1;                                                         // blocks in the first new page
    while (k < nnew && nw[k] == nw[0] + k * stride && (size_t)(nw[k] - nw[0]) + stride <= LARGE_PAGE_SIZE) { k++; }
    char* const page_hi = nw[0] - SLACK_START_MAX + LARGE_PAGE_SIZE;     // the earliest the page can end
    char* const clo = (char*)(((uintptr_t)(nw[0] + k * stride) + psize - 1) & ~(uintptr_t)(psize - 1));
    char* const chi = (char*)((uintptr_t)page_hi & ~(uintptr_t)(psize - 1));
    // it must lie on the old blocks: the old run spans from the lowest to the highest old block
    char* olo = old[0]; char* ohi = old[0];
    for (size_t i = 0; i < OLD_COUNT; i++) { if (old[i] < olo) olo = old[i]; if (old[i] > ohi) ohi = old[i]; }
    ohi += OLD_SIZE;
    size_t resident = 0;
    if (clo < chi && (clo < olo || chi > ohi)) {
      fprintf(stderr, "slack: FAILED (setup): the new page %p is not on the freed old blocks %p..%p\n", (void*)nw[0], (void*)olo, (void*)ohi);
      result = 1;
    }
    else if (clo < chi) {
      const long bound = RELEASE_MAX_DELAYS * mi_option_get(mi_option_purge_delay);
      const long start = now_ms();
      unsigned char vec[LARGE_PAGE_SIZE / 4096];
      const size_t n = (size_t)(chi - clo);
      do {
        resident = 0;
        if (n / psize > sizeof(vec) || mincore(clo, n, vec) != 0) { resident = (size_t)-1; break; }
        for (size_t i = 0; i < n / psize; i++) { resident += (vec[i] & 1); }
        if (resident == 0) break;
        usleep(POLL_MS * 1000);
        mi_collect(false);
      } while (now_ms() - start < bound);
      if (resident != 0) {
        fprintf(stderr, "slack: FAILED: the old blocks in the new page's slack stayed resident\n");
        result = 1;
      }
    }
    fprintf(stderr, "slack: new page at %p, %zu blocks of %zu KiB; slack checked %p..%p (%zu KiB), %zu OS pages resident\n",
            (void*)nw[0], k, stride / 1024, (void*)clo, (void*)chi, (clo < chi ? (size_t)(chi - clo) / 1024 : 0), resident);
  }
  for (size_t i = 0; i < nnew; i++) { mi_free(nw[i]); }
  return result;
  #else
  void* p = mi_malloc(OLD_SIZE); mi_free(p);
  return 0;
  #endif
}

int main(void) {
  // resident-first: C must reuse A's resident slices, not B's purged ones (wants a fresh arena)
  const long resident_first = mi_option_get(mi_option_resident_first);
  mi_option_set(mi_option_resident_first, 1);
  int result = run_case("resident-first", 1);
  // the plain search only: run the sequence, the placement is whatever the search picks
  mi_option_set(mi_option_resident_first, 0);
  result |= run_case("plain search", 0);
  mi_option_set(mi_option_resident_first, resident_first);
  // the slack of a page on reused memory: first purge everything the cases above left queued,
  // so the old page freed in it is the only free resident range the new page can be carved from
  mi_collect(true);
  result |= run_slack_case();
  return result;
}
