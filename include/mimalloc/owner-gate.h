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
  if (tld->gate_depth++ == 0) {
    _mi_park_leave_gate(tld);
  }
  return theap;
}

// Release for the owner: the outermost leave parks the thread.
static inline void _mi_gate_leave(mi_tld_t* tld) {
  mi_assert_internal(tld != NULL && tld->gate_depth > 0);
  if (--tld->gate_depth == 0) {
    mi_atomic_store_release(&tld->park_state, (size_t)MI_PARK_PARKED);
  }
}

// MI_DEBUG leaf assertion predicate (§5.2): the calling thread may touch `tld`'s theaps.
static inline bool _mi_gate_held(const mi_tld_t* tld) {
  if (tld == NULL) return false;
  const mi_threadid_t me = _mi_thread_id();
  if (tld->gate_depth > 0 && tld->thread_id == me) return true;
  return (mi_atomic_load_acquire((_Atomic(size_t)*)&tld->park_state) == MI_PARK_SWEEPING &&
          mi_atomic_load_acquire((_Atomic(uintptr_t)*)&tld->sweeper) == (uintptr_t)me);
}

#define MI_GATE_ENTER(theap)      ((theap) = _mi_gate_enter(theap))
#define MI_GATE_LEAVE(tld)        _mi_gate_leave(tld)
#define MI_GATE_ASSERT_HELD(tld)  mi_assert_internal(_mi_gate_held(tld))

#else  // !MI_OWNER_GATE

#define MI_GATE_ENTER(theap)      ((void)0)
#define MI_GATE_LEAVE(tld)        ((void)0)
#define MI_GATE_ASSERT_HELD(tld)  ((void)0)

#endif

#endif // MI_OWNER_GATE_H
