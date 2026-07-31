/* Benchmark + correctness harness for zero-tracking (issue #67,
   mi_option_purge_zeroes / MIMALLOC_PURGE_ZEROES).

   The feature clears the arena's dirty bits after a decommit-purge, so the next
   allocation is reported as already-zero and mi_zalloc can skip its memset.

   Three workloads, because the first one alone is misleading:

     sparse  -- zalloc big buffers, touch one byte. FAVOURS the feature, and by a lot:
                skipping the memset also skips faulting the pages in, so this measures
                lazy commit as much as it measures zeroing. Reported, but never on its
                own.
     dense   -- zalloc then touch every byte (the anti-workload the issue requires).
                The page faults happen either way here, so only the redundant memset can
                be saved. If the feature costs more than it saves, it shows up here.
     verify  -- correctness. Fills every byte with a non-zero pattern before freeing,
                then requires the next mi_zalloc to hand back genuinely zeroed memory.
                A weaker check that samples a few offsets cannot tell "zeroed" from
                "stale but happens to be zero", and getting this wrong is silent heap
                corruption rather than a visible failure.

   Prints one JSON object so ci/ can consume it. Exits non-zero on any corruption. */

#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <mimalloc.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define ROUNDS   20
#define BLOCKS   64
#define BLOCKSZ  (1u << 20)   /* 1 MiB: large enough that memset dominates the round */

static void* keep[BLOCKS];

static double now_secs(void) {
  return (double)clock() / (double)CLOCKS_PER_SEC;
}

/* Touch one byte per block: the memset skipped here is also a page fault skipped. */
static double bench_sparse(void) {
  const double t0 = now_secs();
  for (int r = 0; r < ROUNDS; r++) {
    for (int i = 0; i < BLOCKS; i++) {
      unsigned char* b = (unsigned char*)mi_zalloc(BLOCKSZ);
      if (b == NULL) { fprintf(stderr, "oom\n"); exit(1); }
      b[0] = 1;
      keep[i] = b;
    }
    for (int i = 0; i < BLOCKS; i++) mi_free(keep[i]);
    mi_collect(true);
  }
  return now_secs() - t0;
}

/* Touch every byte: the anti-workload. Faults happen in both arms. */
static double bench_dense(void) {
  volatile unsigned long long sink = 0;
  const double t0 = now_secs();
  for (int r = 0; r < ROUNDS; r++) {
    for (int i = 0; i < BLOCKS; i++) {
      unsigned char* b = (unsigned char*)mi_zalloc(BLOCKSZ);
      if (b == NULL) { fprintf(stderr, "oom\n"); exit(1); }
      memset(b, (int)(r + i + 1), BLOCKSZ);
      sink += b[BLOCKSZ - 1];
      keep[i] = b;
    }
    for (int i = 0; i < BLOCKS; i++) mi_free(keep[i]);
    mi_collect(true);
  }
  const double elapsed = now_secs() - t0;
  (void)sink;
  return elapsed;
}

/* Every byte of every zalloc must read zero, across purge/reuse cycles. */
static int verify_zeroed(void) {
  for (int r = 0; r < ROUNDS; r++) {
    for (int i = 0; i < BLOCKS; i++) {
      unsigned char* b = (unsigned char*)mi_zalloc(BLOCKSZ);
      if (b == NULL) { fprintf(stderr, "oom\n"); return 1; }
      for (size_t k = 0; k < BLOCKSZ; k++) {
        if (b[k] != 0) {
          fprintf(stderr,
                  "CORRUPTION: mi_zalloc returned non-zero memory "
                  "(round=%d block=%d offset=%zu value=0x%02x)\n",
                  r, i, k, b[k]);
          return 2;
        }
      }
      memset(b, 0xA5, BLOCKSZ);   /* dirty every byte before it goes back */
      keep[i] = b;
    }
    for (int i = 0; i < BLOCKS; i++) mi_free(keep[i]);
    mi_collect(true);
  }
  return 0;
}

int main(void) {
  const int rc = verify_zeroed();
  if (rc != 0) return rc;
  const double sparse = bench_sparse();
  const double dense = bench_dense();
  printf("{ \"purge_zeroes\": %d, \"sparse_s\": %.4f, \"dense_s\": %.4f, \"verify\": \"ok\" }\n",
         mi_option_is_enabled(mi_option_purge_zeroes) ? 1 : 0, sparse, dense);
  return 0;
}
