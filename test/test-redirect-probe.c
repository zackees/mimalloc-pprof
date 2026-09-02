/* ----------------------------------------------------------------------------
Copyright (c) 2026, Microsoft Research, Daan Leijen
This is free software; you can redistribute it and/or modify it under the
terms of the MIT license. A copy of the license can be found in the file
"LICENSE" at the root of this distribution.
-----------------------------------------------------------------------------*/

/* A *behavioural* check that the Windows override is engaged (mimalloc-pprof #277).

   `mi_is_redirected()` -- which is what "mimalloc: malloc is redirected." in verbose
   output reports -- only says that `mimalloc-redirect.dll` believed it patched something.
   That is not the same claim as "this program's allocations reach mimalloc", and the
   difference is not academic: a MinGW/msvcrt binary whose malloc comes from `msvcrt.dll`
   reports `true`, because the redirection module (v1.3.3 knows only `ucrtbase` and
   `ucrtbased`) patched a `ucrtbase.dll` that some system DLL had dragged into the
   process, while the binary's own `msvcrt.dll!malloc` stayed untouched.

   So ask the allocator instead: take a pointer from the *C runtime's* `malloc` and ask
   mimalloc whether it came out of one of its regions. Only a real override can make that
   true.

   This always exits 0 and reports on stdout. Whether a given configuration is *required*
   to redirect is a property of that configuration, not of this program, so the gate lives
   where that is known (`.github/workflows/windows-bundles.yml`), and this test stays
   runnable -- and informative -- everywhere.
*/
#include <stdio.h>
#include <stdlib.h>
#include <mimalloc.h>

int main(void) {
  // Touch the mimalloc API so the DLL is imported and loaded even if nothing else in this
  // translation unit needs it (the same reason `test-stress-dynamic` links `mi_version`).
  const int version = mi_version();

  // `p` escapes into `mi_is_in_heap_region`, an external call, so the compiler cannot
  // elide the allocation.
  void* p = malloc(1);
  const int served_by_mimalloc = ((p != NULL && mi_is_in_heap_region(p)) ? 1 : 0);
  const int module_reports_redirected = (mi_is_redirected() ? 1 : 0);
  free(p);

  printf("REDIRECT_BEHAVIOURAL=%d\n", served_by_mimalloc);
  printf("REDIRECT_FLAG=%d\n", module_reports_redirected);
  printf("mi_version=%d\n", version);
  fflush(stdout);
  return 0;
}
