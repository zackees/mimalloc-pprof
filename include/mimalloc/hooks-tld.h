/* Fork-internal accessors for `mi_tld_t::hooks` (mi_hooks_tld_t, see types.h).

   src/memory-events.c, src/dhat.c and src/profile.c all keep a small amount of
   per-thread reentrancy-guard / in-flight-event state. It used to live in file-local
   `mi_decl_thread` (`__thread`) variables, but on a macOS dylib the loader can lazily
   instantiate a thread's `__thread` block on first touch via a dyld-interposed `calloc` --
   including from inside `_mi_thread_init_with_heap` -> `_mi_meta_zalloc`, which already
   holds `subproc->theap_meta_lock` for that same thread's own tld/theap allocation.
   Touching a `mi_decl_thread` variable from a hook reached that way reenters mimalloc and
   re-acquires that non-recursive lock on the same thread (see issue #266 for the observed
   backtrace). Moving the state into `mi_tld_t` avoids the problem: obtaining a thread's
   `mi_tld_t*` below only ever reads the platform's non-allocating fast-path theap
   accessor (`_mi_theap_default()` -- pthread_getspecific on macOS, an eagerly-allocated
   initial-exec `__thread` on Linux, a TLS/TEB slot read on Windows; see prim-tls.h's
   header comment) and never allocates.

   IMPORTANT: there is deliberately no "force initialization if needed" accessor here.
   The obvious `&mi_theap_get_default()->tld->hooks` is unsafe not only mid-init (the
   #266 crash) but also mid *teardown*: `mi_thread_theaps_done` (init.c) resets the
   default theap to the empty sentinel BEFORE freeing this thread's theaps, specifically
   so nothing re-initializes it during that window (see its own comment), and a free that
   runs after a thread's mimalloc cleanup already ran is an explicitly supported case
   (src/free.c's "can happen if free'd after thread_done was called" comment) -- exactly
   the window forcing here would violate. Every accessor below only ever peeks.

   - `_mi_hooks_tld_peek()`: for hooks whose caller is prepared to no-op entirely when
     this thread has no tld (the per-allocation ALLOC-side hooks: _mi_memevt_on_alloc,
     _mi_prof_on_alloc, _mi_dhat_begin_alloc/_mi_dhat_finish_event -- these ARE reachable
     from inside `_mi_meta_zalloc`'s call chain, allocating this thread's own tld/theap,
     where a NULL result always means "this is a meta allocation, nothing to report" --
     see memory-events.c's `_mi_meta_is_meta_page` check) and for the suppress_begin/end
     pairs (never actually reachable with a NULL peek in practice, since they only run
     from already-initialized-thread call sites, but must still never force).

   - `_mi_hooks_tld_peek_or_local()`: for call sites that need real, possibly-mutated
     scratch state for the duration of ONE call (e.g. memevt_dispatch/dhat_prepare's
     "already inside the handler" suppression-depth bump around invoking a user
     callback), but where a NULL peek must NOT mean "drop the event" -- the free/resize
     hooks (_mi_memevt_on_free/_on_realloc_in_place/_on_resize, _mi_dhat_begin_free/
     _begin_resize) and any top-level control API that itself brackets a callback
     (mi_prof_visit, mi_dhat_dump). These are never reachable from inside
     `_mi_meta_zalloc`'s call chain (meta allocations only ever allocate), so nothing is
     lost by not forcing; a thread whose very first (or only remaining) mimalloc
     interaction is exactly such a call -- e.g. a foreign thread's first-ever call being
     a cross-thread mi_free, or a free arriving after this thread's own mimalloc
     teardown -- still gets correctly, if not persistently, tracked: see
     `test_free_from_foreign_thread` in test-memory-events.c. The returned pointer is
     valid only for the duration of the call that obtained it (it may point at the
     caller's own stack-local `mi_hooks_tld_t`); never stash it anywhere longer-lived. */
#pragma once
#ifndef MIMALLOC_HOOKS_TLD_H
#define MIMALLOC_HOOKS_TLD_H

#include "mimalloc.h"
#include "internal.h"
#include "prim-tls.h"     // _mi_theap_default

// Returns this thread's `mi_hooks_tld_t*`, or NULL if this thread currently has no tld
// (not yet initialized, or already torn down). Never allocates; never calls
// `mi_theap_get_default()` or any other initializing accessor. Must be the very first
// thing every per-allocation hook touches -- see the file comment above.
static inline mi_hooks_tld_t* _mi_hooks_tld_peek(void) {
  mi_theap_t* const theap = _mi_theap_default();
  if (!mi_theap_is_initialized(theap)) return NULL;
  return &theap->tld->hooks;
}

// Like `_mi_hooks_tld_peek()`, but falls back to `local` (caller-owned stack storage,
// zeroed here) instead of NULL. `local` must live at least as long as every use of the
// returned pointer. See the file comment above for when this is (and is not) the right
// choice over a plain peek.
static inline mi_hooks_tld_t* _mi_hooks_tld_peek_or_local(mi_hooks_tld_t* local) {
  mi_hooks_tld_t* const hooks = _mi_hooks_tld_peek();
  if (hooks != NULL) return hooks;
  _mi_memzero(local, sizeof(*local));
  return local;
}

#endif
