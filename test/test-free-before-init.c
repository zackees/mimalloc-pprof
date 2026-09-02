/* ----------------------------------------------------------------------------
Copyright (c) Microsoft Research, Daan Leijen
This is free software; you can redistribute it and/or modify it under the
terms of the MIT license.
-----------------------------------------------------------------------------*/

/* imported from oven-sh/mimalloc @ 942b8342 (commit 7ac561ab), MIT -- see #266.

   `free(NULL)` must work before mimalloc has initialized (upstream issue #1341).

   glibc 2.44's `__newlocale` calls `free(NULL)` from the dynamic loader, before any constructor
   of the executable has run. With `malloc` overridden that lands in `mi_free`, which looks `p`
   up in the page map without a NULL check: `_mi_unchecked_ptr_page` reads `submaps[0][0]`. The
   initial (static) page map has to carry a real all-NULL submap at index 0; with a NULL submap
   the lookup faults at address 0 and the process dies before `main()`.

   On ELF the call is made from `.preinit_array`, which the loader runs before every
   `.init_array` entry of the executable and its libraries -- the same point in process startup
   as the glibc call. Elsewhere a plain constructor is the closest available approximation (its
   order relative to mimalloc's own constructor is not guaranteed).

   musl's dynamic linker does not invoke DT_PREINIT_ARRAY for the main executable at all (verified
   empirically on alpine:3.20/musl 1.2.5: a `.preinit_array` entry with `used` never runs, though
   the section and dynamic-table entry are emitted correctly by the linker -- this is a musl
   limitation, not a linker or mimalloc bug, and the original glibc 2.44 race this test targets is
   glibc-specific to begin with). Fall back to the constructor approximation under MI_LIBC_MUSL,
   per #273 (8a). */

#include <stdio.h>
#include <stdlib.h>
#include <mimalloc.h>

static int calls_before_init = 0;

static void free_null_before_init(void) {
  free(NULL);      // reaches mi_free only when malloc is overridden
  mi_free(NULL);   // always reaches the page-map lookup
  calls_before_init++;
}

#if defined(__ELF__) && !defined(MI_LIBC_MUSL)
__attribute__((section(".preinit_array"), used))
static void (*mi_test_preinit)(void) = &free_null_before_init;
#elif defined(__GNUC__) || defined(__clang__)
__attribute__((constructor))
static void free_null_before_init_ctor(void) { free_null_before_init(); }
#endif

int main(void) {
  if (calls_before_init != 1) {
    printf("test-free-before-init: FAILED, the pre-init hook ran %d times\n", calls_before_init);
    return 1;
  }
  // the real page map replaced the static one: allocation and free still work
  void* p = mi_malloc(64);
  if (p == NULL) {
    printf("test-free-before-init: FAILED, mi_malloc returned NULL after the pre-init free\n");
    return 1;
  }
  mi_free(p);
  free(NULL);
  mi_free(NULL);
  printf("test-free-before-init: ok\n");
  return 0;
}
