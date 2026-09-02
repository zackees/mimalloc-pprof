/* ----------------------------------------------------------------------------
Copyright (c) 2026 Microsoft Research, Daan Leijen
This is free software; you can redistribute it and/or modify it under the
terms of the MIT license. A copy of the license can be found in the file
"LICENSE" at the root of this distribution.
-----------------------------------------------------------------------------*/

/* MI_DEBUG_FULL-only internal diagnostics (issue #167).

   This file must never allocate: lock diagnostics run during allocator bootstrap,
   teardown, and failure paths where calling a hooked allocator would recurse. */

#include "mimalloc.h"
#include "mimalloc/internal.h"
#include "mimalloc/prim.h"

#if MI_DEBUG > 2

#ifdef _WIN32
#include <process.h>
#else
#include <unistd.h>
#endif

// #266 macOS escalation, round 2: a real same-thread reentrant_internal_lock_acquisition
// (current_tid==owner_tid, both non-degenerate -- run 33581454510) needs to know WHICH
// lock and WHAT nested call path, not just that thread ids match. backtrace_symbols_fd
// (unlike backtrace_symbols) never calls malloc -- it writes directly to the fd -- so it
// is safe to use from this file's never-allocate failure path; backtrace() itself is
// widely relied on as safe here too (glibc/Darwin resolve their unwind tables lazily on
// first use elsewhere in the process, not from this crash-only, _exit-terminated path).
#if defined(__APPLE__) || defined(__GLIBC__)
#include <execinfo.h>
#define MI_DIAG_HAVE_BACKTRACE 1
#endif

static char* mi_diag_append(char* out, const char* end, const char* text) {
  while (out < end && *text != 0) { *out++ = *text++; }
  return out;
}

static char* mi_diag_append_uint(char* out, const char* end, uintptr_t value) {
  char digits[3*sizeof(uintptr_t)];
  size_t count = 0;
  do {
    digits[count++] = (char)('0' + (value % 10));
    value /= 10;
  } while (value != 0 && count < sizeof(digits));
  while (out < end && count > 0) { *out++ = digits[--count]; }
  return out;
}

// #266 macOS escalation: `current`/`owner` are the same mi_lock_debug_thread()-tagged
// values compared by the callers below (0 when not applicable to the failure kind, e.g.
// the TLS/zero checks below), so a printed value of 0 always means "no owner" the same
// way it does in the comparisons themselves. Reported to tell a colliding/zero thread id
// during dylib bootstrap (this is macOS's actual failure) apart from a genuine
// cross-thread reentrancy: identical current/owner with both nonzero is reentrancy;
// owner==0 with the fail still firing, or current==owner==some degenerate value across
// unrelated threads, points at the thread-id primitive instead.
static void mi_lock_debug_fail(const char* reason, const void* lock, uintptr_t current, uintptr_t owner,
                               const char* file, unsigned line, const char* func) {
  char message[512] = { 0 };
  char* out = message;
  const char* const end = message + sizeof(message) - 1;
  out = mi_diag_append(out, end, "mimalloc: ");
  out = mi_diag_append(out, end, reason);
  out = mi_diag_append(out, end, " lock=");
  out = mi_diag_append_uint(out, end, (uintptr_t)lock);
  out = mi_diag_append(out, end, " current_tid=");
  out = mi_diag_append_uint(out, end, current);
  out = mi_diag_append(out, end, " owner_tid=");
  out = mi_diag_append_uint(out, end, owner);
  out = mi_diag_append(out, end, " at ");
  out = mi_diag_append(out, end, file);
  out = mi_diag_append(out, end, ":");
  out = mi_diag_append_uint(out, end, (uintptr_t)line);
  out = mi_diag_append(out, end, " (");
  out = mi_diag_append(out, end, func);
  out = mi_diag_append(out, end, ")\n");
  *out = 0;
  _mi_prim_out_stderr(message);

  // Identify the lock by address against the subprocess locks we can reach without any
  // new declarations in upstream headers (mi_subproc_t is a complete type already
  // visible here via internal.h/types.h, and _mi_subproc_main is already declared in
  // internal.h). mi_thread_locals_lock (threadlocal.c) is `static` to that TU and not
  // reachable from here, so it is intentionally not named -- the backtrace below still
  // covers it.
  {
    const mi_subproc_t* const subproc = _mi_subproc_main();
    const char* lock_name = NULL;
    if (lock == &subproc->theap_meta_lock)     { lock_name = "subproc->theap_meta_lock"; }
    else if (lock == &subproc->heaps_lock)      { lock_name = "subproc->heaps_lock"; }
    else if (lock == &subproc->arena_reserve_lock) { lock_name = "subproc->arena_reserve_lock"; }
    if (lock_name != NULL) {
      char name_msg[96] = { 0 };
      char* nout = name_msg;
      const char* const nend = name_msg + sizeof(name_msg) - 1;
      nout = mi_diag_append(nout, nend, "mimalloc: lock identified as ");
      nout = mi_diag_append(nout, nend, lock_name);
      nout = mi_diag_append(nout, nend, "\n");
      *nout = 0;
      _mi_prim_out_stderr(name_msg);
    }
  }

#if defined(MI_DIAG_HAVE_BACKTRACE)
  {
    void* frames[64];
    const int n = backtrace(frames, 64);
    if (n > 0) { backtrace_symbols_fd(frames, n, 2 /* stderr */); }
  }
#endif

  /* Do not run allocator teardown from a corrupted state, and do not spend
     seconds writing a core file in a timeout-bounded diagnostic test. */
  _exit(134);
}

void _mi_diagnostic_check_tls_owner(const void* p) {
  mi_page_t* const page = _mi_ptr_page(p);
  if (page == NULL || !_mi_is_heap_main(mi_page_heap(page))) {
    mi_lock_debug_fail("internal_tls_storage_not_main_owned", p, 0, 0,
                       __FILE__, __LINE__, __func__);
  }
}

void _mi_diagnostic_check_zero(const void* p, size_t size, const char* reason) {
  const uint8_t* const bytes = (const uint8_t*)p;
  for (size_t i = 0; i < size; i++) {
    if (bytes[i] != 0) {
      mi_lock_debug_fail(reason, p, 0, 0, __FILE__, __LINE__, __func__);
    }
  }
}

#if defined(MI_TEST_TLS_CONTROL) && (MI_TEST_TLS_CONTROL != 0)
static _Atomic(int) mi_test_tls_control = MI_ATOMIC_VAR_INIT(0);

void _mi_test_tls_control_set(int mode) {
  mi_atomic_store_relaxed(&mi_test_tls_control, mode);
}

int _mi_test_tls_control_mode(void) {
  return mi_atomic_load_relaxed(&mi_test_tls_control);
}

bool _mi_test_tls_control_fail_growth(size_t old_count) {
  if (old_count == 0 || mi_atomic_load_relaxed(&mi_test_tls_control) != 3) {
    return false;
  }
  return (mi_atomic_exchange_relaxed(&mi_test_tls_control, 0) == 3);
}
#endif

static uintptr_t mi_lock_debug_thread(void) {
  /* Mimalloc reserves the low two thread-id bits. Set one so zero remains the
     unowned sentinel even on a platform whose primitive can return zero. */
  return ((uintptr_t)_mi_thread_id() | (uintptr_t)1);
}

void _mi_lock_debug_before_acquire(const void* lock, const _Atomic(uintptr_t)* owner,
                                   const char* file, unsigned line, const char* func) {
  const uintptr_t current = mi_lock_debug_thread();
  const uintptr_t owned_by = mi_atomic_load_relaxed(owner);
  if (owned_by == current) {
    mi_lock_debug_fail("reentrant_internal_lock_acquisition", lock, current, owned_by, file, line, func);
  }
}

void _mi_lock_debug_after_acquire(const void* lock, _Atomic(uintptr_t)* owner,
                                  const char* file, unsigned line, const char* func) {
  const uintptr_t owned_by = mi_atomic_load_relaxed(owner);
  if (owned_by != 0) {
    mi_lock_debug_fail("internal_lock_owner_not_cleared", lock, mi_lock_debug_thread(), owned_by, file, line, func);
  }
  mi_atomic_store_relaxed(owner, mi_lock_debug_thread());
}

void _mi_lock_debug_before_release(const void* lock, _Atomic(uintptr_t)* owner,
                                   const char* file, unsigned line, const char* func) {
  const uintptr_t current = mi_lock_debug_thread();
  const uintptr_t owned_by = mi_atomic_load_relaxed(owner);
  if (owned_by != current) {
    mi_lock_debug_fail("internal_lock_release_by_non_owner", lock, current, owned_by, file, line, func);
  }
  mi_atomic_store_relaxed(owner, (uintptr_t)0);
}

void _mi_lock_debug_init(_Atomic(uintptr_t)* owner) {
  mi_atomic_store_relaxed(owner, (uintptr_t)0);
}

void _mi_lock_debug_done(const void* lock, const _Atomic(uintptr_t)* owner,
                         const char* file, unsigned line, const char* func) {
  const uintptr_t owned_by = mi_atomic_load_relaxed(owner);
  if (owned_by != 0) {
    mi_lock_debug_fail("destroying_owned_internal_lock", lock, mi_lock_debug_thread(), owned_by, file, line, func);
  }
}

#endif /* MI_DEBUG > 2 */
