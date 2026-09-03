/* ----------------------------------------------------------------------------
Copyright (c) 2018-2025, Microsoft Research, Daan Leijen
This is free software; you can redistribute it and/or modify it under the
terms of the MIT license. A copy of the license can be found in the file
"LICENSE" at the root of this distribution.
-----------------------------------------------------------------------------*/

// imported from oven-sh/mimalloc @ 942b8342, MIT (issue #272 / Bun parity P7b), verbatim.
//
// Note for this fork: this is NOT a test of `mi_option_purge_zeroes` (dead here since the
// #80 pin bump -- see issue #67 and `test-zero-tracking`). It tests the unconditional
// property that a recycled, previously purged slice never hands one allocation's bytes to
// the next through `mi_zalloc`/`mi_calloc`, which is exactly what hole purging and the
// arena purge could break.

// The arena purge may tell the allocator "this range reads back zero" (slices_dirty cleared),
// which lets mi_zalloc skip its memset. If that claim is ever wrong, one allocation's bytes are
// handed to the next -- a disclosure bug. This test tries hard to make the claim wrong:
// fill everything with poison, churn until slices are purged and recycled, and verify that every
// byte of every recycled mi_zalloc allocation is zero.
#include <mimalloc.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <stdint.h>

#define POISON 0xA5
static int failures = 0;

static void check_zero(const unsigned char* p, size_t n, const char* what, size_t round) {
  for (size_t i = 0; i < n; i++) {
    if (p[i] != 0) {
      printf("  FAIL: %s round %zu: byte %zu = 0x%02x (expected 0)\n", what, round, i, p[i]);
      failures++;
      return;
    }
  }
}

// Sizes that span small / medium / large pages and cross OS-page boundaries.
static const size_t sizes[] = { 64, 1024, 8192, 16384, 100*1024, 700*1024 };
#define NSIZES (sizeof(sizes)/sizeof(sizes[0]))
#define NPTR 512

int main(void) {
  mi_option_set(mi_option_purge_delay, 0);   // purge immediately: maximize recycling of purged slices
  static void* ptrs[NPTR];

  for (size_t round = 0; round < 40; round++) {
    const size_t sz = sizes[round % NSIZES];

    // 1) dirty a generation with poison, so any stale reuse is visible
    for (size_t i = 0; i < NPTR; i++) {
      ptrs[i] = mi_malloc(sz);
      if (ptrs[i] == NULL) { printf("  oom\n"); return 1; }
      memset(ptrs[i], POISON, sz);
    }
    for (size_t i = 0; i < NPTR; i++) { mi_free(ptrs[i]); }

    // 2) let the idle path purge (this is what clears slices_dirty)
    mi_on_thread_idle();

    // 3) reuse: every byte must be zero, no matter which slice we got back
    for (size_t i = 0; i < NPTR; i++) {
      ptrs[i] = mi_zalloc(sz);
      if (ptrs[i] == NULL) { printf("  oom\n"); return 1; }
      check_zero((const unsigned char*)ptrs[i], sz, "mi_zalloc after purge", round);
      if (failures) return 1;
    }
    // also verify calloc and a zero-realloc growth path
    for (size_t i = 0; i < NPTR; i++) { mi_free(ptrs[i]); }
    void* c = mi_calloc(1024, 64);
    check_zero((const unsigned char*)c, 1024*64, "mi_calloc after purge", round);
    mi_free(c);
    if (failures) return 1;
  }

  // cross-size churn: free large, allocate small out of the same recycled slices
  for (size_t round = 0; round < 20; round++) {
    void* big[16];
    for (int i = 0; i < 16; i++) { big[i] = mi_malloc(2u<<20); memset(big[i], POISON, 2u<<20); }
    for (int i = 0; i < 16; i++) { mi_free(big[i]); }
    mi_on_thread_idle();
    for (size_t i = 0; i < NPTR; i++) {
      ptrs[i] = mi_zalloc(4096);
      check_zero((const unsigned char*)ptrs[i], 4096, "small zalloc over freed large", round);
      if (failures) return 1;
    }
    for (size_t i = 0; i < NPTR; i++) { mi_free(ptrs[i]); }
  }

  printf("purge-zero: all recycled zalloc/calloc allocations read back zero\n");
  return failures == 0 ? 0 : 1;
}
