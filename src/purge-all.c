/* ----------------------------------------------------------------------------
Copyright (c) 2026, the mimalloc-pprof contributors
This is free software; you can redistribute it and/or modify it under the
terms of the MIT license. A copy of the license can be found in the file
"LICENSE" at the root of this distribution.
-----------------------------------------------------------------------------*/

// #366: `mi_purge_all` -- process-wide eager purge from any thread.
// See docs/purge-all-implementation.md §7. STUB: the driver is implemented in this file
// by the purge-all workstream; the globals below are the fixed interface.

#include "mimalloc.h"
#include "mimalloc/internal.h"

mi_decl_hidden _Atomic(uintptr_t) _mi_purge_admission;   // holder thread id, 0 = free
mi_decl_hidden _Atomic(size_t)    _mi_purge_seq;         // walk epoch

void _mi_purge_all_fork_child(void) {
  mi_atomic_store_relaxed(&_mi_purge_admission, (uintptr_t)0);
}
