/* mi_process_info's current_rss must be RSS, not committed bytes (issue #78).

   On Linux, `_mi_prim_process_info` used to set `peak_rss` but never `current_rss`, so
   the field kept the default `mi_process_info()` assigns it -- `current_commit`,
   mimalloc's own committed counter. Every consumer (`mi_process_info`,
   `mi_stats_print`, and the `rss_current` field of `mi_stats_as_json`) therefore
   reported committed bytes under the name RSS. Bun measured 4.91 GB vs 0.56 GB on a
   real workload.

   The test allocates a large region and touches only one byte per OS page in a small
   prefix of it. Committed then greatly exceeds resident, so a build that confuses the
   two reports the two values as equal and fails here.

   Deliberately tolerant about the exact numbers -- RSS depends on the OS, the page
   size, and what else the process has faulted in. The assertion is only the thing that
   is actually guaranteed: with this much committed-but-untouched memory, RSS must be
   meaningfully below commit. Anything stricter would flake.

   Non-Linux platforms already set current_rss from the OS (mach task_info on macOS,
   PROCESS_MEMORY_COUNTERS on Windows), so this is a portable assertion, not a
   Linux-specific one -- which is the point: it would have caught the Linux gap from
   any platform's perspective. */

#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <mimalloc.h>
#include <stdio.h>
#include <string.h>

#define BLOCKS     64
#define BLOCKSZ    (4u << 20)      /* 4 MiB each -> 256 MiB committed */
#define TOUCH_FRAC 16u             /* touch only 1/16th of each block */

static void* keep[BLOCKS];

int main(void) {
  size_t elapsed, user, sys, rss0, prss, commit0, pcommit, faults;
  mi_process_info(&elapsed, &user, &sys, &rss0, &prss, &commit0, &pcommit, &faults);

  for (int i = 0; i < BLOCKS; i++) {
    keep[i] = mi_malloc(BLOCKSZ);
    assert(keep[i] != NULL);
    /* Touch one byte every 4 KiB, but only across the first 1/TOUCH_FRAC of the block,
       so the rest stays committed-but-not-resident. */
    unsigned char* b = (unsigned char*)keep[i];
    for (size_t off = 0; off < BLOCKSZ / TOUCH_FRAC; off += 4096) {
      b[off] = (unsigned char)i;
    }
  }

  size_t rss, commit;
  mi_process_info(&elapsed, &user, &sys, &rss, &prss, &commit, &pcommit, &faults);

  printf("rss=%zu KiB  commit=%zu KiB  (baseline rss=%zu commit=%zu)\n",
         rss / 1024, commit / 1024, rss0 / 1024, commit0 / 1024);

  if (commit == 0) {
    printf("ok: no commit accounting on this platform; nothing to compare\n");
    for (int i = 0; i < BLOCKS; i++) mi_free(keep[i]);
    return 0;
  }

  if (rss == commit) {
    fprintf(stderr,
            "FAIL: current_rss == current_commit (%zu). RSS is being reported as\n"
            "committed bytes -- see _mi_prim_process_info. %u MiB was committed and\n"
            "only about 1/%u of it touched, so these must differ.\n",
            rss, (unsigned)((BLOCKS * (size_t)BLOCKSZ) >> 20), TOUCH_FRAC);
    return 1;
  }

  /* And RSS should be the smaller of the two here, since most of the commit was never
     faulted in. A little slack for allocator metadata and whatever else is resident. */
  if (rss > commit) {
    fprintf(stderr,
            "FAIL: current_rss (%zu) exceeds current_commit (%zu) with most of the\n"
            "committed memory deliberately untouched.\n", rss, commit);
    return 1;
  }

  printf("ok: rss is %.1f%% of commit, as expected for mostly-untouched memory\n",
         100.0 * (double)rss / (double)commit);

  for (int i = 0; i < BLOCKS; i++) mi_free(keep[i]);
  return 0;
}
