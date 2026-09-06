/* ----------------------------------------------------------------------------
Copyright (c) 2026, the mimalloc-pprof contributors
This is free software; you can redistribute it and/or modify it under the
terms of the MIT license. A copy of the license can be found in the file
"LICENSE" at the root of this distribution.
-----------------------------------------------------------------------------*/
#pragma once
#ifndef MI_OWNER_GATE_H
#define MI_OWNER_GATE_H

/* #366: the owner gate (docs/purge-all-implementation.md §5).

   In a build with MI_OWNER_GATE=1 a thread is MI_PARK_PARKED whenever it is outside the
   allocator and MI_PARK_RUNNING only while inside it: entering is the owner acquire
   (`_mi_park_leave_gate`: CAS PARKED->RUNNING, or wait out a foreign SWEEPING claim),
   leaving is a release-store of PARKED. That is the whole per-thread lock -- `park_state`,
   the scavenger's claim CAS and `park_reclaim` are reused unchanged.

   The gate is recursive for the owner through the plain, owner-private `tld->gate_depth`
   (realloc -> malloc+free, output hooks -> malloc under override, deferred-free handlers).
   Only the owner reads or writes `gate_depth`; a foreign sweeper never does.

   Entry sites (one enter, exactly ONE leave per body) are listed in the plan §5.1; coverage
   is proven by `_mi_gate_held` assertions at the owner-private leaf accessors (§5.2), not by
   the site list. When MI_OWNER_GATE is 0 every macro here expands to nothing and the default
   build's fast path is byte-identical (ci/check_fastpath_identity.py). */

// Included from the tail of mimalloc/internal.h (it needs `_mi_thread_id`, `mi_theap_is_initialized`,
// `mi_theap_get_default`, `_mi_park_leave_gate`); do not include it directly.

#if MI_OWNER_GATE

// Acquire for the owner. `theap` may be uninitialised (`_mi_theap_empty`): the thread is
// initialised FIRST (registering its tld as RUNNING), so the tld is published with a live
// outer depth and nested allocations during initialisation are ordinary recursion.
// Returns the (possibly just-initialised) theap the caller must continue with.
static inline mi_theap_t* _mi_gate_enter(mi_theap_t* theap) {
  if mi_unlikely(!mi_theap_is_initialized(theap)) {
    theap = mi_theap_get_default();
  }
  mi_tld_t* const tld = theap->tld;
  mi_assert_internal(tld != NULL);
  // The detached tld (`mi_tld_detached`, thread_id MI_THREADID_DETACHED) backs every subproc's
  // `theap_meta` and is shared by all threads: those theaps are protected by `theap_meta_lock`,
  // not by the park protocol, and it is never registered -- so it is never gated.
  if mi_unlikely(tld->thread_id == MI_THREADID_DETACHED) return theap;
  if (tld->gate_depth++ == 0) {
    _mi_park_leave_gate(tld);
  }
  return theap;
}

// Release for the owner: the outermost leave parks the thread.
static inline void _mi_gate_leave(mi_tld_t* tld) {
  if mi_unlikely(tld->thread_id == MI_THREADID_DETACHED) return;   // see `_mi_gate_enter`
  mi_assert_internal(tld != NULL && tld->gate_depth > 0);
  if (--tld->gate_depth == 0) {
    mi_atomic_store_release(&tld->park_state, (size_t)MI_PARK_PARKED);
  }
}

// MI_DEBUG leaf assertion predicate (§5.2): the calling thread may touch `tld`'s theaps.
static inline bool _mi_gate_held(const mi_tld_t* tld) {
  if (tld == NULL) return false;
  if (tld->thread_id == MI_THREADID_DETACHED) return true;   // `theap_meta`: guarded by theap_meta_lock, never gated
  const mi_threadid_t me = _mi_thread_id();
  if (tld->gate_depth > 0 && tld->thread_id == me) return true;
  return (mi_atomic_load_acquire((_Atomic(size_t)*)&tld->park_state) == MI_PARK_SWEEPING &&
          mi_atomic_load_acquire((_Atomic(uintptr_t)*)&tld->sweeper) == (uintptr_t)me);
}

#define MI_GATE_ENTER(theap)      ((theap) = _mi_gate_enter(theap))
#define MI_GATE_LEAVE(tld)        _mi_gate_leave(tld)
// Leaf form: a theap that has been DETACHED from its heap (`theap->heap == NULL`, the heap
// teardown ABA claim protocol in `mi_heap_detach_theaps`) is mutated by the deleting thread,
// never again by its owner -- that protocol is its exclusivity, not the gate.
static inline bool _mi_gate_held_theap(const mi_theap_t* theap) {
  if (theap == NULL) return false;
  if (_mi_theap_heap_peek(theap) == NULL) return true;
  return _mi_gate_held(theap->tld);
}

#define MI_GATE_ASSERT_HELD(theap)  mi_assert_internal(_mi_gate_held_theap(theap))

#else  // !MI_OWNER_GATE

// Ungated, "may touch this theap's pages" is simply owner-or-claim-holder: the owner (no gate,
// it is RUNNING unless it parked itself in `mi_on_thread_idle_start`), or the thread holding
// the tld's MI_PARK_SWEEPING claim. Used by `_mi_page_free_collect_no_unpurge` so a foreign
// reader never folds a page's thread frees (same rule in both builds); the gated build's
// version above additionally requires the owner to be inside the gate.
static inline bool _mi_gate_held_theap(const mi_theap_t* theap) {
  if (theap == NULL) return false;
  if (_mi_theap_heap_peek(theap) == NULL) return true;   // detached by heap teardown: the deleter's
  const mi_tld_t* const tld = theap->tld;
  if (tld == NULL) return false;
  if (tld->thread_id == MI_THREADID_DETACHED) return true;
  const mi_threadid_t me = _mi_thread_id();
  return (tld->thread_id == me ||
          mi_atomic_load_acquire((_Atomic(uintptr_t)*)&tld->sweeper) == (uintptr_t)me);
}

#define MI_GATE_ENTER(theap)      ((void)0)
#define MI_GATE_LEAVE(tld)        ((void)0)
#define MI_GATE_ASSERT_HELD(theap)  ((void)0)

#endif

#endif // MI_OWNER_GATE_H
