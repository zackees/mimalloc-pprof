// #374: internal ownership-protected capture. Not a general user-callback API.
#pragma once
#include "mimalloc/internal.h"

typedef struct mi_diag_coverage_s {
  size_t skipped_pages;
  size_t busy_theaps;
} mi_diag_coverage_t;

// Caller holds subproc->heaps_lock and its own owner gate. The callback must only
// copy metadata to raw-OS storage: no user code, allocator reentry, or payload reads.
bool _mi_heap_visit_diagnostic(mi_heap_t* heap, bool blocks, mi_block_visit_fun* visitor,
                              void* arg, mi_diag_coverage_t* coverage);
