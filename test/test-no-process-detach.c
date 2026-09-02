/* #268 (Bun parity P3): MI_NO_PROCESS_DETACH skips the automatic mi_process_done() call
   that would otherwise run from a static destructor / DLL_PROCESS_DETACH / atexit hook
   at process exit. This single source is built twice by CMakeLists.txt -- once plain,
   once with MI_NO_PROCESS_DETACH=1 -- and the two ctest entries assert opposite outcomes
   for the same regex.

   The test deliberately does NOT call mi_process_done() itself: whether "process done"
   appears in the output depends entirely on whether the automatic exit path ran, which is
   exactly the behavior under test. `_mi_verbose_message` (src/options.c) only emits under
   MIMALLOC_VERBOSE=1, which the ctest ENVIRONMENT property sets. */
#include <mimalloc.h>
#include <stdio.h>

int main(void) {
  void* p = mi_malloc(64);
  if (p == NULL) { printf("FAIL: mi_malloc returned NULL\n"); return 1; }
  mi_free(p);
  printf("ok: allocated and freed, exiting normally\n");
  return 0;
  /* No mi_process_done() call here on purpose -- see file comment above. */
}
