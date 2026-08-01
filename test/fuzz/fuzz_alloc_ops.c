/* Structure-aware fuzzer over the allocator API (issue #87).

   The fuzz input is interpreted as a *program*: a sequence of allocator operations
   whose sizes, alignments and pointer operands are drawn from the input, against a
   table of live blocks the harness maintains.

   Why structure-aware: an allocator takes sizes and pointers, not documents. A raw
   byte-stream mutator spends nearly its whole budget on inputs rejected in one branch.
   Interpreting the input as an op sequence is what actually reaches interleavings of
   size classes, alignment boundaries and realloc growth/shrink chains.

   The oracle is AddressSanitizer (#86), which is why this was not worth building
   before ASan was live and proven. Without a sanitizer, fuzzing an allocator mostly
   proves it does not segfault -- a weak claim. On top of that this harness asserts
   properties a crash would not reveal:

     - mi_usable_size(p) >= the requested size
     - every zeroing call returns memory that is zero across the WHOLE block
     - alignment is honoured for aligned calls
     - a byte pattern survives a realloc that moves

   The zeroing check is deliberately over the whole block: my first zero-tracking
   benchmark sampled three offsets and could not tell "zeroed" from "stale but happens
   to be zero", which made its result meaningless. */

#include <assert.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "mimalloc.h"

#define MAX_LIVE 64
/* Bounded so a single input cannot OOM the fuzzer; large enough to cross the
   small/large/huge boundaries mimalloc treats differently. */
#define MAX_SIZE (1u << 20)

typedef struct {
  void*  ptr;
  size_t size;   /* requested size */
  uint8_t tag;   /* pattern written into the block, 0 = untouched */
} live_t;

static live_t live[MAX_LIVE];

typedef struct {
  const uint8_t* p;
  size_t         len;
  size_t         pos;
} reader_t;

static uint8_t rd8(reader_t* r) {
  if (r->pos >= r->len) return 0;
  return r->p[r->pos++];
}

static size_t rd_size(reader_t* r) {
  /* Two bytes of magnitude plus a class selector, so the corpus can reach small,
     page-ish and huge sizes without needing improbably specific bytes. */
  const uint8_t lo = rd8(r);
  const uint8_t hi = rd8(r);
  size_t v = (size_t)lo | ((size_t)hi << 8);
  switch (rd8(r) & 3) {
    case 0: return v & 0xFF;              /* tiny */
    case 1: return v;                     /* small */
    case 2: return (v & 0x3FF) * 64;      /* page-ish */
    default: return (v % MAX_SIZE) + 1;   /* up to 1 MiB */
  }
}

static size_t rd_align(reader_t* r) {
  /* Powers of two from 1 to 4096; mi_*_aligned requires a power of two. */
  return (size_t)1 << (rd8(r) & 12);
}

#if MI_FUZZ_PLANT_BUG
/* Positive control (CI only, -DMI_FUZZ_PLANT_BUG=ON). A deliberate one-byte overflow
   on an easily-reachable input, so the fuzz job can demonstrate that the harness
   actually finds bugs. A fuzzer that has never found anything is indistinguishable
   from one that is not running the code under test -- and this repository has now
   shipped six checks that were silently verifying nothing.
   Kept in the harness rather than in src/, so production code is never built with a
   planted defect and the control cannot escape into a release. */
static void plant_bug(void* p, size_t requested) {
  if (p != NULL && requested == 64) {
    ((unsigned char*)p)[requested] = 0xAA;   /* one past the end -- ASan must catch */
  }
}
#endif

static void check_block(void* p, size_t requested, int must_be_zero, size_t alignment) {
  if (p == NULL) return;  /* OOM is a legal outcome, not a defect */
#if MI_FUZZ_PLANT_BUG
  plant_bug(p, requested);
#endif
  const size_t usable = mi_usable_size(p);
  assert(usable >= requested);
  if (alignment > 1) {
    assert(((uintptr_t)p % alignment) == 0);
  }
  if (must_be_zero) {
    const unsigned char* b = (const unsigned char*)p;
    for (size_t i = 0; i < requested; i++) {
      assert(b[i] == 0);   /* whole block, not a sample */
    }
  }
}

static void release_all(void) {
  for (size_t i = 0; i < MAX_LIVE; i++) {
    if (live[i].ptr != NULL) { mi_free(live[i].ptr); live[i].ptr = NULL; }
  }
}

int LLVMFuzzerTestOneInput(const uint8_t* data, size_t size) {
  reader_t r = { data, size, 0 };
  memset(live, 0, sizeof(live));

  while (r.pos < r.len) {
    const uint8_t op = rd8(&r) % 8;
    const size_t slot = rd8(&r) % MAX_LIVE;

    switch (op) {
      case 0: {  /* malloc */
        if (live[slot].ptr != NULL) break;
        const size_t n = rd_size(&r);
        void* p = mi_malloc(n);
        check_block(p, n, 0, 1);
        live[slot].ptr = p; live[slot].size = n; live[slot].tag = 0;
        break;
      }
      case 1: {  /* zalloc -- must come back zeroed */
        if (live[slot].ptr != NULL) break;
        const size_t n = rd_size(&r);
        void* p = mi_zalloc(n);
        check_block(p, n, 1, 1);
        live[slot].ptr = p; live[slot].size = n; live[slot].tag = 0;
        break;
      }
      case 2: {  /* calloc -- must come back zeroed */
        if (live[slot].ptr != NULL) break;
        const size_t count = (rd8(&r) % 16) + 1;
        const size_t each  = (rd_size(&r) % 4096) + 1;
        void* p = mi_calloc(count, each);
        check_block(p, count * each, 1, 1);
        live[slot].ptr = p; live[slot].size = count * each; live[slot].tag = 0;
        break;
      }
      case 3: {  /* aligned_alloc */
        if (live[slot].ptr != NULL) break;
        const size_t a = rd_align(&r);
        const size_t n = rd_size(&r);
        void* p = mi_malloc_aligned(n, a);
        check_block(p, n, 0, a);
        live[slot].ptr = p; live[slot].size = n; live[slot].tag = 0;
        break;
      }
      case 4: {  /* realloc -- contents up to min(old,new) must survive a move */
        if (live[slot].ptr == NULL) break;
        const size_t n = rd_size(&r);
        const size_t old = live[slot].size;
        const uint8_t tag = live[slot].tag;
        void* p = mi_realloc(live[slot].ptr, n);
        if (p == NULL) break;             /* original still live on failure */
        if (tag != 0) {
          const unsigned char* b = (const unsigned char*)p;
          const size_t keep = (old < n ? old : n);
          for (size_t i = 0; i < keep; i++) assert(b[i] == tag);
        }
        check_block(p, n, 0, 1);
        live[slot].ptr = p; live[slot].size = n;
        break;
      }
      case 5: {  /* rezalloc -- the GROWN region must be zero */
        if (live[slot].ptr == NULL) break;
        const size_t old = live[slot].size;
        const size_t n = rd_size(&r);
        void* p = mi_rezalloc(live[slot].ptr, n);
        if (p == NULL) break;
        if (n > old) {
          const unsigned char* b = (const unsigned char*)p;
          for (size_t i = old; i < n; i++) assert(b[i] == 0);
        }
        assert(mi_usable_size(p) >= n);
        live[slot].ptr = p; live[slot].size = n;
        break;
      }
      case 6: {  /* write a pattern, so realloc moves are checkable */
        if (live[slot].ptr == NULL) break;
        uint8_t tag = rd8(&r);
        if (tag == 0) tag = 1;
        memset(live[slot].ptr, tag, live[slot].size);
        live[slot].tag = tag;
        break;
      }
      default: {  /* free */
        if (live[slot].ptr == NULL) break;
        mi_free(live[slot].ptr);
        live[slot].ptr = NULL; live[slot].size = 0; live[slot].tag = 0;
        break;
      }
    }
  }

  release_all();
  return 0;
}
