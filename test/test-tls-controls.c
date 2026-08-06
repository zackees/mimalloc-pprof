/* Positive controls for the MI_DEBUG_FULL thread-local growth diagnostics.

   This file is linked with src/static.c under MI_TEST_TLS_CONTROL=1. That
   private define never reaches a shipped mimalloc library. */

#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "mimalloc.h"
#include "mimalloc/internal.h"

#ifndef _WIN32
#include <sys/resource.h>
static void disable_core_dump(void) {
  const struct rlimit limit = { 0, 0 };
  (void)setrlimit(RLIMIT_CORE, &limit);
}
#else
static void disable_core_dump(void) { }
#endif

#define CONTROL_HEAPS 40

static mi_heap_t* heaps[CONTROL_HEAPS];
static mi_theap_t* bindings[CONTROL_HEAPS];

static void bind_heaps(void) {
  for (size_t i = 0; i < CONTROL_HEAPS; i++) {
    heaps[i] = mi_heap_new();
    assert(heaps[i] != NULL);
    bindings[i] = mi_heap_theap(heaps[i]);
    assert(bindings[i] != NULL);
  }
}

static void verify_and_destroy_heaps(void) {
  for (size_t i = 0; i < CONTROL_HEAPS; i++) {
    assert(mi_heap_theap(heaps[i]) == bindings[i]);
    void* const p = mi_heap_malloc(heaps[i], i + 1);
    assert(p != NULL);
    mi_heap_destroy(heaps[i]);
  }
}

static int run_owner_control(void) {
  mi_heap_t* const owner = mi_heap_new();
  assert(owner != NULL);
  mi_theap_t* const previous = mi_theap_set_default(mi_heap_theap(owner));
  _mi_test_tls_control_set(1);
  bind_heaps();
  mi_theap_set_default(previous);
  return 99; /* the ownership diagnostic must abort first */
}

static int run_zero_control(void) {
  _mi_test_tls_control_set(2);
  bind_heaps();
  return 99; /* the zero-initialization diagnostic must abort first */
}

static int run_failure_control(void) {
  bind_heaps();

  _mi_test_tls_control_set(3);
  assert(!_mi_test_tls_force_expand(128));
  for (size_t i = 0; i < CONTROL_HEAPS; i++) {
    assert(mi_heap_theap(heaps[i]) == bindings[i]);
  }

  /* Failure must leave the old table published and usable; the same operation
     succeeds once the one-shot allocation failure is consumed. */
  assert(_mi_test_tls_force_expand(128));
  verify_and_destroy_heaps();
  puts("ok: TLS growth allocation failure was atomic and retryable");
  return 0;
}

int main(int argc, char** argv) {
  disable_core_dump();
  if (argc != 2) {
    fprintf(stderr, "usage: %s owner|zero|failure\n", argv[0]);
    return 2;
  }
  if (strcmp(argv[1], "owner") == 0) return run_owner_control();
  if (strcmp(argv[1], "zero") == 0) return run_zero_control();
  if (strcmp(argv[1], "failure") == 0) return run_failure_control();
  fprintf(stderr, "unknown control: %s\n", argv[1]);
  return 2;
}
