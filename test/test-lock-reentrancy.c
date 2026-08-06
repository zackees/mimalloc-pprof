/* Positive control for MI_DEBUG_FULL's internal lock diagnostics (issue #167).

   This program deliberately reacquires one non-recursive mimalloc lock on the same
   thread. A useful diagnostic must terminate it immediately with the stable message
   checked by run-negative.cmake. Without the diagnostic the second acquisition hangs,
   and the wrapper reports a bounded timeout instead of letting CI wedge.

   This target is built only for MI_DEBUG_FULL and is never linked into the shipped
   allocator. */

#include "mimalloc.h"
#include "mimalloc/atomic.h"
#include <stdio.h>
#include <string.h>

#ifndef _WIN32
#include <sys/resource.h>
static void disable_core_dump(void) {
  const struct rlimit limit = { 0, 0 };
  (void)setrlimit(RLIMIT_CORE, &limit);
}
#else
static void disable_core_dump(void) { }
#endif

static mi_lock_t control_lock = MI_LOCK_INITIALIZER;

int main(int argc, char** argv) {
  disable_core_dump();
  if (argc != 2) {
    fprintf(stderr, "usage: %s reentrant|uncleared|nonowner-release|destroy-owned\n", argv[0]);
    return 2;
  }
  if (strcmp(argv[1], "reentrant") == 0) {
    mi_lock_acquire(&control_lock);
    mi_lock_acquire(&control_lock);
  }
  else if (strcmp(argv[1], "uncleared") == 0) {
    mi_atomic_store_relaxed(&control_lock.debug_owner, (uintptr_t)2);
    mi_lock_acquire(&control_lock);
  }
  else if (strcmp(argv[1], "nonowner-release") == 0) {
    mi_lock_release(&control_lock);
  }
  else if (strcmp(argv[1], "destroy-owned") == 0) {
    mi_lock_acquire(&control_lock);
    mi_lock_done(&control_lock);
  }
  else {
    fprintf(stderr, "unknown control: %s\n", argv[1]);
    return 2;
  }
  return 99; /* every mode must terminate through its exact diagnostic */
}
