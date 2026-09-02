/* ----------------------------------------------------------------------------
Copyright (c) 2026, Microsoft Research, Daan Leijen
This is free software; you can redistribute it and/or modify it under the
terms of the MIT license.
-----------------------------------------------------------------------------*/

// imported from oven-sh/mimalloc @ 942b8342, MIT (issue #271 / Bun parity P6).

// test-heap-churn.c
//
// Multi-threaded mi_heap_new/mi_heap_malloc/mi_heap_destroy churn stress.
//
// Reproduces (on the unfixed code) the `mi_thread_locals` slot-array
// resurrection bug: `mi_thread_locals_expand()` grows the per-thread
// heap->theap slot array with `mi_rezalloc` and assumes the new slots are
// zero, but `mi_rezalloc` copies `mi_usable_size(old)` bytes — the live slots
// plus the uninitialized slack between the old requested size and the old bin
// size — and only zeroes beyond that. Under heap churn that slack holds the
// previous tenant of the recycled page (application data from a destroyed
// heap). `_mi_thread_local_get` validates a slot only by comparing its
// `version` lane against the key's version (a small sequential counter), so a
// garbage lane that happens to equal a live key's version makes the adjacent
// garbage lane get dereferenced as a cached `mi_theap_t*`.
//
// Triggering it requires enough concurrently-live heap keys to force the slot
// array past its previous capacity (the default 64-thread x 3-heaps-per-job
// configuration reaches slot indices > 64), plus recycled pages carrying
// application data into the slack. On the unfixed code this crashes roughly
// once per several hundred process runs:
//
//   ulimit -c unlimited
//   for i in $(seq 1 500); do ./mimalloc-test-heap-churn || break; done
//
// The test also stamps every allocated block with an ownership signature and
// re-verifies it at several points, so a double allocation (the same block
// handed to two owners) is reported deterministically with both owners
// identified rather than only when it happens to crash.
//
//   ./mimalloc-test-heap-churn [nthreads] [njobs]     (defaults: 64 100)
#if defined(_WIN32)
#include <stdio.h>
int main(void) { printf("test-heap-churn: skipped on Windows (uses pthreads/fork)\n"); return 0; }
#else

#include <mimalloc.h>

#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

static int g_nthreads = 64;
static int g_njobs    = 100;
static int g_quiet    = 0;

#define MAX_BLOCKS 600  // per job, explicit-heap blocks
#define MAX_DEF    64   // per job, default-heap blocks

#define STAMP_MAGIC 0xC0FFEE00DEADBEEFULL

// 32-byte stamp written at offset 0 of every block, and again at
// (size - 32) when size >= 64.
typedef struct stamp_s {
  uint64_t magic;   // STAMP_MAGIC
  uint64_t ptr;     // the block's own address
  uint64_t owner;   // (thread_id << 32) | job_number
  uint64_t size;    // usable size we requested
} stamp_t;

typedef struct block_s {
  void*    p;
  size_t   size;
  uint8_t  fill;    // middle filler byte
  uint8_t  live;
  int8_t   heap;    // which of the 3 job heaps owns this (-1 = default heap)
} block_t;

// ---------------------------------------------------------------------------
// Cross-thread free mailbox (tiny lock-free ring of default-heap pointers).
// Bun does cross-thread mi_free; this exercises the deferred-free path.
// ---------------------------------------------------------------------------

#define MAILBOX_SLOTS 256
static _Atomic(void*) g_mailbox[MAILBOX_SLOTS];

static void mailbox_push(void* p) {
  // try a few slots; if all full, free it ourselves
  for (int i = 0; i < 8; i++) {
    unsigned idx = (unsigned)(((uintptr_t)p >> 4) + (uintptr_t)i * 33u) % MAILBOX_SLOTS;
    void* expected = NULL;
    if (atomic_compare_exchange_strong_explicit(&g_mailbox[idx], &expected, p,
                                                memory_order_acq_rel, memory_order_relaxed)) {
      return;
    }
  }
  mi_free(p);
}

static void mailbox_drain_some(unsigned seed) {
  for (int i = 0; i < 4; i++) {
    unsigned idx = (seed + (unsigned)i * 97u) % MAILBOX_SLOTS;
    void* p = atomic_exchange_explicit(&g_mailbox[idx], NULL, memory_order_acq_rel);
    if (p != NULL) {
      mi_free(p);  // cross-thread free
    }
  }
}

static void mailbox_drain_all(void) {
  for (int i = 0; i < MAILBOX_SLOTS; i++) {
    void* p = atomic_exchange_explicit(&g_mailbox[i], NULL, memory_order_acq_rel);
    if (p != NULL) mi_free(p);
  }
}

// ---------------------------------------------------------------------------
// PRNG (xorshift64*) — per thread, deterministic per (seed,thread)
// ---------------------------------------------------------------------------

static inline uint64_t rng_next(uint64_t* s) {
  uint64_t x = *s;
  x ^= x >> 12;
  x ^= x << 25;
  x ^= x >> 27;
  *s = x;
  return x * 0x2545F4914F6CDD1DULL;
}

// ---------------------------------------------------------------------------
// Stamping & verification
// ---------------------------------------------------------------------------

// NOTE: never write more than `size` bytes -- for blocks smaller than 32
// bytes the header is truncated to the block size.
static void stamp_block(void* p, size_t size, uint32_t tid, uint32_t job, uint8_t fill) {
  stamp_t st;
  st.magic = STAMP_MAGIC;
  st.ptr   = (uint64_t)(uintptr_t)p;
  st.owner = ((uint64_t)tid << 32) | (uint64_t)job;
  st.size  = (uint64_t)size;
  size_t head = (size < sizeof(st)) ? size : sizeof(st);
  memcpy(p, &st, head);
  if (size >= 64) {
    // fill the middle with a thread-derived byte
    memset((uint8_t*)p + sizeof(st), fill, size - 2 * sizeof(st));
    memcpy((uint8_t*)p + size - sizeof(st), &st, sizeof(st));
  } else if (size > sizeof(st)) {
    memset((uint8_t*)p + sizeof(st), fill, size - sizeof(st));
  }
}

static void dump_bytes(const char* label, const uint8_t* b, size_t n) {
  fprintf(stderr, "    %s:", label);
  for (size_t i = 0; i < n; i++) {
    if (i % 8 == 0) fprintf(stderr, " ");
    fprintf(stderr, "%02x", b[i]);
  }
  fprintf(stderr, "\n");
}

static void decode_found(const uint8_t* found) {
  stamp_t got;
  memcpy(&got, found, sizeof(got));
  fprintf(stderr, "    decoded-as-stamp: magic=%016llx ptr=%016llx owner_tid=%u owner_job=%u size=%llu\n",
          (unsigned long long)got.magic, (unsigned long long)got.ptr,
          (uint32_t)(got.owner >> 32), (uint32_t)(got.owner & 0xffffffffu),
          (unsigned long long)got.size);
  if (got.magic == STAMP_MAGIC) {
    fprintf(stderr, "    >>> the foreign bytes ARE a valid stamp from thread %u job %u, block %p size %llu\n",
            (uint32_t)(got.owner >> 32), (uint32_t)(got.owner & 0xffffffffu),
            (void*)(uintptr_t)got.ptr, (unsigned long long)got.size);
    fprintf(stderr, "    >>> i.e. mimalloc handed the same memory to two live owners.\n");
  }
}

static void verify_fail(const char* where, const block_t* b, uint32_t tid, uint32_t job,
                        const uint8_t* found, const uint8_t* expect, const char* which) {
  fprintf(stderr, "\n=== CORRUPTION DETECTED (%s, %s stamp) ===\n", where, which);
  fprintf(stderr, "    block %p size %zu owned by thread %u job %u\n", b->p, b->size, tid, job);
  dump_bytes("expected", expect, 32);
  dump_bytes("found   ", found, 32);
  decode_found(found);
  fflush(stderr);
  abort();
}

static void verify_block(const char* where, const block_t* b, uint32_t tid, uint32_t job) {
  if (!b->live || b->p == NULL) return;
  stamp_t st;
  st.magic = STAMP_MAGIC;
  st.ptr   = (uint64_t)(uintptr_t)b->p;
  st.owner = ((uint64_t)tid << 32) | (uint64_t)job;
  st.size  = (uint64_t)b->size;
  size_t head = (b->size < sizeof(st)) ? b->size : sizeof(st);
  if (memcmp(b->p, &st, head) != 0) {
    verify_fail(where, b, tid, job, (const uint8_t*)b->p, (const uint8_t*)&st, "head");
  }
  if (b->size >= 64) {
    const uint8_t* tail = (const uint8_t*)b->p + b->size - sizeof(st);
    if (memcmp(tail, &st, sizeof(st)) != 0) {
      verify_fail(where, b, tid, job, tail, (const uint8_t*)&st, "tail");
    }
    // spot-check the middle filler (not the whole thing -- keep it fast)
    const uint8_t* mid = (const uint8_t*)b->p + sizeof(st);
    size_t midlen = b->size - 2 * sizeof(st);
    size_t step = midlen > 256 ? midlen / 16 : 1;
    for (size_t i = 0; i < midlen; i += step) {
      if (mid[i] != b->fill) {
        uint8_t found[32]; uint8_t expect[32];
        size_t off = (i > 8 ? i - 8 : 0);
        size_t avail = midlen - off; if (avail > 32) avail = 32;
        memset(found, 0, sizeof(found)); memcpy(found, mid + off, avail);
        memset(expect, b->fill, sizeof(expect));
        fprintf(stderr, "\n(middle filler mismatch at offset %zu of %zu)\n", i + sizeof(st), b->size);
        verify_fail(where, b, tid, job, found, expect, "middle");
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Worker
// ---------------------------------------------------------------------------

typedef struct worker_arg_s {
  uint32_t tid;
  uint64_t seed;
} worker_arg_t;

static const size_t k_small_sizes[] = { 8, 16, 24, 32, 48, 64, 96, 136, 192, 256, 384, 512 };
#define N_SMALL_SIZES (sizeof(k_small_sizes) / sizeof(k_small_sizes[0]))

static void* worker(void* argp) {
  worker_arg_t* arg = (worker_arg_t*)argp;
  const uint32_t tid = arg->tid;
  uint64_t rng = arg->seed;
  const uint8_t fill = (uint8_t)(0x40 | (tid & 0x3f));

  block_t  blocks[MAX_BLOCKS];
  block_t  defblocks[MAX_DEF];

  for (uint32_t job = 0; job < (uint32_t)g_njobs; job++) {
    // 1. three heaps per job (scratch arena, AST arena, spill heap)
    mi_heap_t* heaps[3];
    heaps[0] = mi_heap_new();
    heaps[1] = mi_heap_new();
    heaps[2] = mi_heap_new();
    if (!heaps[0] || !heaps[1] || !heaps[2]) {
      fprintf(stderr, "mi_heap_new failed\n");
      abort();
    }

    int nblocks = 0;
    int ndef = 0;
    memset(blocks, 0, sizeof(blocks));
    memset(defblocks, 0, sizeof(defblocks));

    // 2. allocation mix
    // ~400 small blocks 8..512 from h1/h2/h3 round-robin, with ~50
    // default-heap mi_malloc blocks interleaved (free ~half mid-job).
    int def_budget = 50;
    for (int i = 0; i < 400 && nblocks < MAX_BLOCKS; i++) {
      size_t sz = k_small_sizes[rng_next(&rng) % N_SMALL_SIZES];
      int hidx = i % 3;
      mi_heap_t* h = heaps[hidx];
      void* p = mi_heap_malloc(h, sz);
      if (!p) { fprintf(stderr, "mi_heap_malloc(%zu) failed\n", sz); abort(); }
      blocks[nblocks].p = p; blocks[nblocks].size = sz; blocks[nblocks].fill = fill; blocks[nblocks].live = 1; blocks[nblocks].heap = (int8_t)hidx;
      stamp_block(p, sz, tid, job, fill);
      nblocks++;

      // interleave default-heap allocations
      if (def_budget > 0 && (i % 8) == 3) {
        size_t dsz = 8 + (rng_next(&rng) % 1017); // 8..1024
        void* dp = mi_malloc(dsz);
        if (!dp) { fprintf(stderr, "mi_malloc(%zu) failed\n", dsz); abort(); }
        if (ndef < MAX_DEF) {
          defblocks[ndef].p = dp; defblocks[ndef].size = dsz; defblocks[ndef].fill = fill; defblocks[ndef].live = 1;
          stamp_block(dp, dsz, tid, job, fill);
          ndef++;
        } else {
          mi_free(dp);
        }
        def_budget--;
      }
      // randomly free ~half the default-heap blocks mid-job
      if (ndef > 0 && (rng_next(&rng) % 16) == 0) {
        int di = (int)(rng_next(&rng) % (uint64_t)ndef);
        if (defblocks[di].live) {
          verify_block("mid-job def free", &defblocks[di], tid, job);
          // occasionally hand it to another thread to free
          if ((rng_next(&rng) % 4) == 0) {
            mailbox_push(defblocks[di].p);
          } else {
            mi_free(defblocks[di].p);
          }
          defblocks[di].live = 0;
        }
      }
    }

    // ~20 medium blocks 1..8 KB, including 3944 exactly
    for (int i = 0; i < 20 && nblocks < MAX_BLOCKS; i++) {
      size_t sz = (i == 0) ? 3944 : (1024 + (rng_next(&rng) % (8192 - 1024 + 1)));
      int hidx = (int)(rng_next(&rng) % 3);
      void* p = mi_heap_malloc(heaps[hidx], sz);
      if (!p) { fprintf(stderr, "mi_heap_malloc(%zu) failed\n", sz); abort(); }
      blocks[nblocks].p = p; blocks[nblocks].size = sz; blocks[nblocks].fill = fill; blocks[nblocks].live = 1; blocks[nblocks].heap = (int8_t)hidx;
      stamp_block(p, sz, tid, job, fill);
      nblocks++;
    }

    // ~3 blocks of 16 KB
    for (int i = 0; i < 3 && nblocks < MAX_BLOCKS; i++) {
      size_t sz = 16 * 1024;
      int hidx = (int)(rng_next(&rng) % 3);
      void* p = mi_heap_malloc(heaps[hidx], sz);
      if (!p) { fprintf(stderr, "mi_heap_malloc(%zu) failed\n", sz); abort(); }
      blocks[nblocks].p = p; blocks[nblocks].size = sz; blocks[nblocks].fill = fill; blocks[nblocks].live = 1; blocks[nblocks].heap = (int8_t)hidx;
      stamp_block(p, sz, tid, job, fill);
      nblocks++;
    }

    // 1-2 blocks of 64..512 KB
    {
      int nbig = 1 + (int)(rng_next(&rng) % 2);
      for (int i = 0; i < nbig && nblocks < MAX_BLOCKS; i++) {
        size_t sz = (64 * 1024) + (rng_next(&rng) % (448 * 1024 + 1));
        int hidx = (int)(rng_next(&rng) % 3);
        void* p = mi_heap_malloc(heaps[hidx], sz);
        if (!p) { fprintf(stderr, "mi_heap_malloc(%zu) failed\n", sz); abort(); }
        blocks[nblocks].p = p; blocks[nblocks].size = sz; blocks[nblocks].fill = fill; blocks[nblocks].live = 1; blocks[nblocks].heap = (int8_t)hidx;
        stamp_block(p, sz, tid, job, fill);
        nblocks++;
      }
    }

    // a few realloc / rezalloc calls to exercise the realloc path
    for (int i = 0; i < 6 && nblocks > 0; i++) {
      int bi = (int)(rng_next(&rng) % (uint64_t)nblocks);
      block_t* b = &blocks[bi];
      if (!b->live || b->size > 8192) continue;
      verify_block("pre-realloc", b, tid, job);
      size_t newsz = 32 + (rng_next(&rng) % 4065); // 32..4096
      int hidx = (int)(rng_next(&rng) % 3);
      void* np = mi_heap_realloc(heaps[hidx], b->p, newsz);
      if (!np) { fprintf(stderr, "mi_heap_realloc(%zu) failed\n", newsz); abort(); }
      b->p = np; b->size = newsz; b->heap = (int8_t)hidx;
      stamp_block(np, newsz, tid, job, fill);
    }
    if (ndef > 0) {
      int di = (int)(rng_next(&rng) % (uint64_t)ndef);
      if (defblocks[di].live) {
        verify_block("pre-rezalloc", &defblocks[di], tid, job);
        size_t newsz = 32 + (rng_next(&rng) % 993);
        void* np = mi_rezalloc(defblocks[di].p, newsz);
        if (!np) { fprintf(stderr, "mi_rezalloc(%zu) failed\n", newsz); abort(); }
        defblocks[di].p = np; defblocks[di].size = newsz;
        stamp_block(np, newsz, tid, job, fill);
      }
    }

    // 4a. verify immediately after all allocations
    for (int i = 0; i < nblocks; i++) verify_block("post-alloc", &blocks[i], tid, job);
    for (int i = 0; i < ndef; i++)    verify_block("post-alloc(def)", &defblocks[i], tid, job);

    // 4b. jitter to widen the race window, then verify again
    usleep((useconds_t)(rng_next(&rng) % 501));
    for (int i = 0; i < nblocks; i++) verify_block("post-sleep", &blocks[i], tid, job);
    for (int i = 0; i < ndef; i++)    verify_block("post-sleep(def)", &defblocks[i], tid, job);

    // drain a few cross-thread frees pushed by other threads
    mailbox_drain_some((unsigned)rng_next(&rng));

    // 4c. verify right before destroying the heaps
    for (int i = 0; i < nblocks; i++) verify_block("pre-destroy", &blocks[i], tid, job);

    // free remaining default-heap blocks
    for (int i = 0; i < ndef; i++) {
      if (defblocks[i].live) {
        verify_block("pre-free(def)", &defblocks[i], tid, job);
        if ((rng_next(&rng) % 8) == 0) {
          mailbox_push(defblocks[i].p);
        } else {
          mi_free(defblocks[i].p);
        }
        defblocks[i].live = 0;
      }
    }

    // 5./6. destroy the heaps; every ~16 jobs delete one instead of destroying.
    // mi_heap_delete migrates live pages to the default heap, so we must
    // explicitly free that heap's blocks afterwards (verifying them first).
    int delete_one = ((job % 16) == 15) ? (int)(rng_next(&rng) % 3) : -1;
    for (int hi = 0; hi < 3; hi++) {
      if (hi == delete_one) {
        mi_heap_delete(heaps[hi]);
        for (int i = 0; i < nblocks; i++) {
          if (blocks[i].live && blocks[i].heap == (int8_t)hi) {
            verify_block("post-delete", &blocks[i], tid, job);
            mi_free(blocks[i].p);
            blocks[i].live = 0;
          }
        }
      } else {
        mi_heap_destroy(heaps[hi]);
      }
    }
    // Anything still marked live was owned by a destroyed heap: forget it.
    for (int i = 0; i < nblocks; i++) blocks[i].live = 0;

    if (!g_quiet && ((job + 1) % 50) == 0) {
      fprintf(stderr, "[t%02u] %u/%d jobs\n", tid, job + 1, g_njobs);
    }
  }
  return NULL;
}

// ---------------------------------------------------------------------------

int main(int argc, char** argv) {
  if (argc > 1) g_nthreads = atoi(argv[1]);
  if (argc > 2) g_njobs    = atoi(argv[2]);
  if (getenv("STRESS_QUIET")) g_quiet = 1;
  if (g_nthreads < 1 || g_nthreads > 256) { fprintf(stderr, "bad nthreads\n"); return 2; }
  if (g_njobs < 1) { fprintf(stderr, "bad njobs\n"); return 2; }

  uint64_t base_seed = 0x9E3779B97F4A7C15ULL;
  const char* se = getenv("STRESS_SEED");
  if (se) base_seed ^= strtoull(se, NULL, 0);
  base_seed ^= ((uint64_t)getpid() << 32) ^ (uint64_t)time(NULL);

  pthread_t* threads = (pthread_t*)malloc(sizeof(pthread_t) * (size_t)g_nthreads);
  worker_arg_t* args = (worker_arg_t*)malloc(sizeof(worker_arg_t) * (size_t)g_nthreads);

  for (int i = 0; i < g_nthreads; i++) {
    args[i].tid = (uint32_t)i;
    args[i].seed = base_seed + (uint64_t)i * 0xD1B54A32D192ED03ULL;
    if (args[i].seed == 0) args[i].seed = 1;
    if (pthread_create(&threads[i], NULL, worker, &args[i]) != 0) {
      fprintf(stderr, "pthread_create failed\n");
      return 2;
    }
  }
  for (int i = 0; i < g_nthreads; i++) {
    pthread_join(threads[i], NULL);
  }
  mailbox_drain_all();
  free(threads);
  free(args);
  printf("OK: %d jobs x %d threads, no corruption\n", g_njobs, g_nthreads);
  return 0;
}

#endif  // !_WIN32
