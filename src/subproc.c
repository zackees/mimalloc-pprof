/* ----------------------------------------------------------------------------
Copyright (c) 2018-2026, Microsoft Research, Daan Leijen
This is free software; you can redistribute it and/or modify it under the
terms of the MIT license. A copy of the license can be found in the file
"LICENSE" at the root of this distribution.
-----------------------------------------------------------------------------*/

#include "mimalloc.h"
#include "mimalloc/internal.h"
#include "mimalloc/prim-tls.h"
#include "mimalloc/prim.h"       // _mi_prim_thread_yield (test hooks, #270)

/* -----------------------------------------------------------
  #270 (Bun parity P5): pthread_atfork fork-safety handlers.

  fork() from a multithreaded process only clones the calling thread; every other
  thread simply vanishes in the child, taking whatever locks it happened to hold with
  it. If any mimalloc-internal lock was locked by a thread other than the one calling
  fork() at the moment of the fork, the child inherits it permanently locked and its
  first allocation that touches that lock hangs forever. `pthread_atfork` (registered
  once in `src/init.c`'s `mi_process_init_once`, POSIX only -- see the platform guard
  there) gives us three callbacks around every fork(): `prepare` runs in the parent
  just before the actual fork and must acquire every lock that could otherwise be held
  by some other thread; `parent` runs in the parent immediately after and must release
  them in the reverse order (so the parent process never observes anything different
  from a world where fork() were a no-op); `child` runs instead in the (now
  single-threaded) child and must put every one of those locks back into a fresh,
  unlocked state. The child uses `mi_lock_init`, never `mi_lock_release`: the lock
  state copied from the parent does not correspond to *this* thread being the logical
  owner, and `mi_lock_init` also resets the `MI_DEBUG>2` reentrancy checker's
  `debug_owner` field (diagnostic.c's `_mi_lock_debug_init`), so the child's first real
  acquire is never flagged as "owner not cleared" against a parent-side thread id that
  no longer exists here -- thread ids can (and on some platforms do) get reused, so a
  `mi_lock_release`-only reset would be a false-negative waiting to happen, not just an
  asymmetry.

  Nested-call safety: on macOS the malloc-zone `force_lock`/`force_unlock`/
  `reinit_lock` callbacks (src/prim/osx/alloc-override-zone.c) call the very same three
  functions below, and the system can invoke both the zone callbacks and
  `pthread_atfork` for one actual fork(). `mi_fork_owner`/`mi_fork_depth` (see their
  declaration below) make `_mi_process_fork_prepare/parent/child` idempotent under
  that double call: only the outermost `prepare`/`parent` pair on the OWNING thread
  does real work, and `child` does its reset exactly once no matter how many
  registered mechanisms invoke it for the same fork.

  Cross-thread exclusion: modern glibc (>= 2.34, where `__run_prefork_handlers`
  became lock-free) does NOT serialize fork() calls made concurrently by different
  threads of the same process -- verified empirically with a minimal `pthread_atfork`
  probe on glibc 2.42: concurrent `fork()` calls from different threads reliably
  interleave their prepare/parent handlers (observed nesting depth 4-5 under load).
  `mi_fork_serialize_lock` is what gives this file TRUE cross-thread exclusion instead:
  acquired once by whichever thread's `prepare` call first claims ownership
  (`mi_fork_owner`), released once that same thread's outermost `parent`/`child`
  finishes, so only one thread's prepare/parent/child sequence -- and therefore only
  one attempt to acquire the locks below -- is ever in flight process-wide at a time.
  A second thread's concurrent fork() blocks in `mi_lock_acquire` until the first
  finishes.

  `mi_fork_owner`/`mi_fork_depth` are deliberately NOT `mi_decl_thread` (`__thread`):
  this codebase already hit, and fixed, exactly that mistake for similar per-thread
  hook state (see `include/mimalloc/hooks-tld.h`'s file comment and `mi_hooks_tld_t`
  in types.h, #266) -- on a macOS dylib, the loader can lazily instantiate a thread's
  first-touched `__thread` block via a dyld-interposed `calloc`, and `prepare` can be
  reached (via the zone `force_lock` callback / `_malloc_fork_prepare` DYLD interpose,
  alloc-override-zone.c) from inside libSystem's own fork machinery before any lock
  below is held -- touching a fresh `__thread` variable there could reenter mimalloc's
  allocator at a moment this file assumes is allocation-free. `mi_fork_owner` (a
  shared `_Atomic(mi_threadid_t)`) plus a plain (non-thread-local, non-atomic) `int
  mi_fork_depth`, both read/written only by whichever thread currently holds
  `mi_fork_serialize_lock`, need no TLS at all and are exactly as safe as any other
  lock-protected shared state.

  Ported design (not code, except where an individual block says otherwise) from
  oven-sh/mimalloc @ 942b8342, MIT (`src/subproc.c`'s
  `_mi_process_fork_prepare/parent/child`). Bun's own `mi_fork_depth` is a single
  process-wide atomic with no serializing lock at all -- correct only for same-thread
  nesting, not for genuinely concurrent multi-threaded fork(), which Bun's callers do
  not appear to exercise; porting that design as-is here produced a real
  `internal_lock_release_by_non_owner` (an ABA race across overlapping fork
  "generations", caught by the P1 reentrancy checker) under a multi-threaded
  fork-storm stress test -- see the #270 PR discussion. `mi_fork_serialize_lock` plus
  the owner/depth pair above is this tree's fix. Bun's version is also entangled with
  a per-subprocess `tld` registry (`sp->tlds`/`tlds_lock`) and scavenger/park state
  that do not exist in this tree yet -- only the lock skeleton and the
  `threadlocal.c` handler are ported here; the resulting gap and every
  scavenger-specific hook point are marked `// Phase 7: scavenger` below (tracked by
  #264 item 7 / #272).

  ---- Lock order (KEEP IN SYNC with `mi_subproc_fork_prepare`/`_mi_process_fork_prepare`
  below -- `mi_fork_lock_order_assert` self-checks it in MI_DEBUG builds, but ONLY the
  sequence within one call to this handler: it cannot see, and says nothing about,
  what order some OTHER, unrelated code path in the allocator takes these same locks
  in -- that correctness is what this list itself has to get right by inspection) ----
  Acquired in `_mi_process_fork_prepare`, in this order; released in
  `_mi_process_fork_parent` in the exact reverse order; reset with `mi_lock_init`
  (order irrelevant -- nothing is acquired) in `_mi_process_fork_child`.

  Bun's stated rule -- "a lock that can still be held while a call comes back into
  mimalloc must be taken before `arena_reserve_lock`" -- turned out to assume those
  locks are never taken FROM INSIDE the plain allocation path itself, which does not
  hold in this tree: `prof_lock`/`dhat_lock`/`memevt_cb_lock` are acquired by the
  alloc/free HOOKS (`_mi_prof_on_alloc` etc, called from `alloc.c`/`page.c`) while a
  heap's `arena_pages_lock` can still be held a few frames up the same call stack
  (`_mi_malloc_generic` -> `mi_arenas_page_alloc_fresh` -> `mi_heap_ensure_arena_pages`,
  arena.c). Taking the hook locks BEFORE the heap/arena locks, as Bun's rule and an
  earlier version of this file did, is therefore the wrong order for THIS tree and
  produces a real, deterministic AB-BA deadlock under `MIMALLOC_PROF=1` (reproduced:
  see the #270 PR discussion) -- confirmed by tracing the actual nesting in this
  codebase rather than trusting the rule's justification. The corrected rule for this
  tree: a lock taken by an alloc/free HOOK (as opposed to a lock the plain allocation
  slow path takes for itself) goes LAST, innermost, alongside `out_buf_lock`.

  `mi_heap_ensure_arena_pages` has one more real-nesting wrinkle worth spelling out:
  for a NON-main heap `h` with no `arena_pages_t` entry yet for a given arena, it
  holds `h->arena_pages_lock` while calling `mi_arena_pages_alloc`, which allocates
  the entry itself via `mi_heap_zalloc_aligned(subproc->heap_main, ...)` -- an
  ordinary allocation that can, in turn, need `heap_main->arena_pages_lock` (if
  `heap_main` has no entry yet for whatever arena backs THAT allocation) before
  reaching the same alloc hooks. So `h->arena_pages_lock` (any non-main heap) can sit
  OUTSIDE `heap_main->arena_pages_lock` in real nesting -- the opposite of what an
  earlier version of this list had (`heap_main` first). Non-main heaps go first below.

    1.  mi_subprocs_lock                    (subproc.c)            subprocess registry
    2.  mi_thread_locals_lock               (threadlocal.c)        TLS slot bitmap
    3.  _mi_page_map()->lock                (page-map.c)           page-map submap growth
        for each subproc `sp` in `mi_subprocs` (newest first):
    4.    sp->heaps_lock
    5.    for each non-main heap `h` in `sp->heaps`:
            h->arena_pages_lock, then h->theaps_lock, then h->os_abandoned_pages_lock
    6.    sp->heap_main->arena_pages_lock, then ->theaps_lock, then ->os_abandoned_pages_lock
            // Phase 7: scavenger -- Bun also quiesces a per-subprocess `tlds` registry
            // and its own `tlds_lock` here (walking every thread's `tld->theaps_lock`
            // and park state). That registry does not exist yet in this tree; it lands
            // with the scavenger (#264 item 7 / #272). `mi_tld_t::theaps_lock`
            // (types.h, "sometimes accessed from another thread on mi_heap_free") is
            // therefore a KNOWN GAP not covered by this phase -- see the P5 PR
            // description and MIMALLOC_FORKS.md. It is deliberately not approximated
            // by an ad hoc walk here: a heap's theaps list can reach the same `tld`
            // through more than one theap (one thread, several heaps), and locking a
            // non-recursive mutex twice would turn a currently-rare hang into a
            // guaranteed one inside this very handler.
    7.    sp->arena_reserve_lock
    8.    sp->theap_meta_lock
    9.  prof_lock                           (profile.c, MI_PPROF)  alloc/free hook; see the AB-BA note above
    10. dhat_lock                           (dhat.c)               alloc/free hook; see the AB-BA note above
    11. memevt_cb_lock                      (memory-events.c)      alloc/free hook; never actually held across a callback (see its own comment), quiesced here anyway
    12. out_buf_lock                        (options.c)            innermost: its critical section never calls back into the allocator

  Residual, PRE-EXISTING hazard this phase does NOT close (found while re-deriving the
  order above, not introduced by it): `mi_prof_visit` (profile.c) holds `prof_lock`
  across a user-supplied visitor callback (`mi_prof_snapshot_visit` does not -- it
  visits a decoupled, already-copied snapshot under no lock at all). If a `mi_prof_visit`
  visitor allocates, the REAL nesting for that path is the reverse of the alloc-hook
  path above -- `prof_lock` OUTER, heap/arena locks INNER -- which is an AB-BA hazard
  against the alloc-hook path with no fork() involved at all. Out of scope for a
  fork-safety phase to fix (it would mean redesigning `mi_prof_visit` itself, e.g.
  try-lock-and-retry as `theap.c`'s park/reclaim code does around `heaps_lock`); the
  contract is instead now documented explicitly at `mi_prof_visit`'s declaration
  (profile.h): a visitor must not allocate.
----------------------------------------------------------- */
// pre-allocate the main subprocess structure.
static mi_decl_cache_align mi_subproc_t mi_process_subproc_main = mi_init_struct_zero;
static mi_subproc_t* mi_subprocs = NULL;
static mi_lock_t     mi_subprocs_lock = MI_LOCK_INITIALIZER;


/* -----------------------------------------------------------
  Meta-data allocation
  We allocate thread local data and theaps through a dedicated
  theap `subproc.theap_meta` which uses a detached tld with
  a detached thread id. The initial theap_meta is statically 
  allocated and can thus be used to allocate on an as yet
  uninitialized thread or process.
  We need to take a lock though to allocate safely on the
  detached `theap_meta`.
----------------------------------------------------------- */

void* _mi_meta_zalloc( mi_subproc_t* subproc, size_t size, mi_memid_t* memid ) {
  mi_assert_internal(subproc->theap_meta != NULL);
  void* p = NULL;
  mi_lock(&subproc->theap_meta_lock) {
    p = mi_theap_zalloc(subproc->theap_meta, size);
    if (memid != NULL) { *memid = (p==NULL ? _mi_memid_none() : _mi_memid_create_malloc(p,size,true) ); }
  }
  return p;
}

void* _mi_meta_zalloc_aligned( mi_subproc_t* subproc, size_t size, size_t aligned, mi_memid_t* memid ) {
  mi_assert_internal(subproc->theap_meta != NULL);
  void* p = NULL;
  mi_lock(&subproc->theap_meta_lock) {
    p = mi_theap_zalloc_aligned(subproc->theap_meta, size, aligned);
    if (memid != NULL) { *memid = (p==NULL ? _mi_memid_none() : _mi_memid_create_malloc(p,size,true) ); }
  }
  return p;
}

void* _mi_meta_rezalloc( mi_subproc_t* subproc, void* oldp, size_t newsize, mi_memid_t* memid ) {
  mi_assert_internal(subproc->theap_meta != NULL);
  // note: since we take a meta lock we cannot use `mi_theap_rezalloc` as that could call `mi_free` which
  // can call `mi_stat_free` which would try to take the meta lock again. See issue #1358.
  void* p = NULL;  
  mi_lock(&subproc->theap_meta_lock) {
    p = mi_theap_zalloc(subproc->theap_meta, newsize);
  }
  if (p!=NULL) {
    if (oldp!=NULL) {
      const size_t oldsize  = mi_usable_size(oldp);
      const size_t copysize = (newsize < oldsize ? newsize : oldsize);
      _mi_memcpy(p,oldp,copysize);
      if (memid!=NULL) { _mi_meta_free(subproc,oldp,*memid); } 
                  else { mi_free(oldp); }
    }
    if (memid!=NULL) { *memid = _mi_memid_create_malloc(p,newsize,true); }
  }
  else {
    if (memid!=NULL) { *memid = _mi_memid_none(); }  
  }
  return p;
}

void _mi_meta_free(mi_subproc_t* subproc, void* p, mi_memid_t memid) {
  if (p==NULL || mi_memid_needs_no_free(memid)) return;
  if (memid.memkind == MI_MEM_MALLOC) {
    mi_free(p);
  }
  else {
    mi_assert_internal(subproc!=NULL);  
    _mi_arenas_free(subproc, p, _mi_memid_size(memid), memid);
  }
}

bool _mi_meta_is_meta_page(const mi_subproc_t* subproc, const mi_page_t* page) {
  if (page==NULL) return false;
  mi_theap_t* theap = page->theap;
  return (theap != NULL && theap == subproc->theap_meta);
}


/* -----------------------------------------------------------
  Sub process helpers
----------------------------------------------------------- */

mi_subproc_t* _mi_subproc_main(void) {
  return &mi_process_subproc_main;
}

bool _mi_subproc_is_main(mi_subproc_t* subproc) {
  return (subproc == &mi_process_subproc_main);
}

mi_subproc_t* _mi_subproc(void) {
  mi_theap_t* theap = _mi_theap_default();
  if (theap == NULL || theap->tld == NULL) {  // see issue #1289
    return _mi_subproc_main();
  }
  else {
    return theap->tld->subproc;
  }
}

mi_heap_t* mi_heap_main(void) {
  return _mi_subproc_heap_main(_mi_subproc()); // don't use mi_theap_main_init_get() so this call works during process_init
}


mi_subproc_t* _mi_subproc_from_id(mi_subproc_id_t subproc_id) {
  return (mi_subproc_t*)(subproc_id._mi_subproc_id);
}

mi_subproc_id_t _mi_subproc_to_id(mi_subproc_t* subproc) {
  mi_subproc_id_t id = { subproc };
  return id;
}

mi_subproc_id_t mi_subproc_main(void) {
  return _mi_subproc_to_id(_mi_subproc_main());
}

mi_subproc_id_t mi_subproc_current(void) {
  return _mi_subproc_to_id(_mi_subproc());
}


/* -----------------------------------------------------------
  Sub process creation
----------------------------------------------------------- */


static mi_subproc_t* mi_subproc_init(mi_subproc_t* subproc, mi_subproc_t* parent) {
  static _Atomic(size_t) subproc_total_count;
  subproc->parent = parent;
  subproc->subproc_seq = mi_atomic_increment_relaxed(&subproc_total_count);
  mi_stats_header_init(&subproc->stats);
  mi_lock_init(&subproc->arena_reserve_lock);
  mi_lock_init(&subproc->heaps_lock);
  mi_lock_init(&subproc->theap_meta_lock);
  mi_lock(&mi_subprocs_lock) {
    // push on subproc list
    subproc->next = mi_subprocs;
    if (mi_subprocs!=NULL) { mi_subprocs->prev = subproc; }
    mi_subprocs = subproc;
  }
  return subproc;
}

mi_subproc_id_t mi_subproc_new(void) {
  mi_thread_init();
  mi_subproc_t* const parent = _mi_subproc();
  mi_memid_t memid;
  mi_subproc_t* const subproc = (mi_subproc_t*)_mi_meta_zalloc(parent, sizeof(mi_subproc_t), &memid);
  if (subproc == NULL) { return _mi_subproc_to_id(NULL); }
  subproc->memid  = memid;  
  
  mi_memid_t theap_memid;
  mi_theap_t* const theap_meta = (mi_theap_t*)_mi_meta_zalloc(parent, sizeof(mi_theap_t), &theap_memid);
  if (theap_meta==NULL) { 
    _mi_meta_free(parent, subproc, memid); 
    return _mi_subproc_to_id(NULL); 
  }
  theap_meta->memid = memid;
  
  // init subproc
  mi_subproc_init(subproc,parent);
  
  // init main heap
  mi_heap_t* heap_main = _mi_heap_new_for_subproc(subproc,0,true);
  if (heap_main==NULL) {
    _mi_meta_free(parent, theap_meta, theap_meta->memid);
    mi_subproc_destroy(_mi_subproc_to_id(subproc));
    return _mi_subproc_to_id(NULL);
  }
  mi_assert_internal(subproc->heap_main == heap_main);

  // init meta theap
  mi_assert_internal(parent->theap_meta!=NULL);
  mi_assert_internal(parent->theap_meta->tld!=NULL);
  mi_assert_internal(parent->theap_meta->tld->thread_id == MI_THREADID_DETACHED);
  _mi_theap_init(theap_meta,heap_main,parent->theap_meta->tld /* detached tld */);
  #if MI_GUARDED
  // See the matching comment in init.c's process-main theap_meta bootstrap (#266):
  // internal allocator bookkeeping must never be guarded.
  theap_meta->guarded_sample_rate = 0;
  #endif
  subproc->theap_meta = theap_meta;

  return _mi_subproc_to_id(subproc);
}


/* -----------------------------------------------------------
  Sub process destruction
----------------------------------------------------------- */

// destroy all subproc resources including arena's, heap's etc.
static void mi_subproc_unsafe_destroy(mi_subproc_t* subproc, bool acquire_subprocs_lock)
{
  if (subproc==NULL) return;

  // remove from the subproc list
  mi_lock_maybe(&mi_subprocs_lock, acquire_subprocs_lock) {
    if (subproc->next!=NULL) { subproc->next->prev = subproc->prev;  }
    if (subproc->prev!=NULL) { subproc->prev->next = subproc->next;  }
                        else { mi_assert_internal(mi_subprocs==subproc);  mi_subprocs = subproc->next; }
  }

  // destroy all subproc heaps
  mi_lock(&subproc->heaps_lock) {
    mi_heap_t* heap = subproc->heaps;
    while (heap != NULL) {
      mi_heap_t* next = heap->next;
      if (heap!=subproc->heap_main) { _mi_heap_force_destroy(heap, false /* don't re-acquire the heaps_lock */); }
      heap = next;
    }
    mi_assert_internal(subproc->heap_main==NULL || subproc->heaps == subproc->heap_main);
    if (subproc->heap_main!=NULL) {
      _mi_thread_locals_thread_done(); // release thread locals that may have been allocated (safe as the main heap uses the fast key)
      if (_mi_subproc_is_main(subproc)) {
        _mi_thread_locals_done();      
      }
      _mi_heap_force_destroy(subproc->heap_main, false /* don't re-acquire the heaps_lock */);  // no warning if destroying the main heap
    }
  }

  subproc->theap_meta = NULL; // theap meta stats are merged during heap_destroy of the main heap

  if (!_mi_subproc_is_main(subproc)) {
    // merge stats back into the main subproc  
    _mi_stats_merge_into(&mi_process_subproc_main.stats, &subproc->stats);
  }

  // remove associated arenas
  _mi_arenas_unsafe_destroy_all(subproc);

  // show stats of the main process (at process end) before releasing the heaps lock
  if (_mi_subproc_is_main(subproc)) {
    if (mi_option_is_enabled(mi_option_show_stats) || mi_option_is_enabled(mi_option_verbose)) {
      mi_subproc_stats_print_out(mi_subproc_main(), NULL, NULL);
    }
  }

  // todo: should we refcount subprocesses?
  mi_lock_done(&subproc->arena_reserve_lock);
  mi_lock_done(&subproc->heaps_lock);
  mi_lock_done(&subproc->theap_meta_lock);  
  _mi_meta_free( subproc->parent, subproc, subproc->memid);  
  if (_mi_subproc_is_main(subproc)) {
    // for the main subproc, also release the global page map
    _mi_page_map_unsafe_destroy();
  }
}

void mi_subproc_destroy(mi_subproc_id_t subproc_id) {
  mi_subproc_t* subproc = _mi_subproc_from_id(subproc_id);
  if (subproc==NULL || subproc==&mi_process_subproc_main) return;
  mi_subproc_unsafe_destroy(subproc, true /* take lock */);
}

void _mi_subprocs_unsafe_destroy_all(void) {
  mi_lock(&mi_subprocs_lock) {
    mi_subproc_t* subproc = mi_subprocs;
    while (subproc!=NULL) {
      mi_subproc_t* next = subproc->next;
      if (subproc!=&mi_process_subproc_main) {
        mi_subproc_unsafe_destroy(subproc, false /* take mi_subprocs lock */);
      }
      subproc = next;
    }
  }
  mi_subproc_unsafe_destroy(&mi_process_subproc_main, true /* take mi_subprocs lock */);
}


/* -----------------------------------------------------------
  Sub process various
----------------------------------------------------------- */

void mi_subproc_add_current_thread(mi_subproc_id_t subproc_id) {
  mi_subproc_t* subproc = _mi_subproc_from_id(subproc_id);
  mi_assert_internal(subproc!=NULL);
  if (subproc==NULL) return;
  mi_assert_internal(subproc->heap_main!=NULL);
  if (subproc->heap_main==NULL) return;
  mi_theap_t* theap = _mi_theap_default();
  if (mi_theap_is_initialized(theap)) {
    if (theap->tld!=NULL && theap->tld->subproc != subproc) {
      _mi_warning_message("unable to add thread to the subprocess as it was already in another subprocess (at %p)\n", theap->tld->subproc);
    }
    return;
  }

  // initialize this thread tld & theap
  _mi_thread_init_with_heap(subproc->heap_main);
}


bool mi_subproc_visit_heaps(mi_subproc_id_t subproc_id, mi_heap_visit_fun* visitor, void* arg) {
  mi_subproc_t* subproc = _mi_subproc_from_id(subproc_id);
  if (subproc==NULL) return false;
  bool ok = true;
  mi_lock(&subproc->heaps_lock) {
    for (mi_heap_t* heap = subproc->heaps; heap!=NULL && ok; heap = heap->next) {
      ok = (*visitor)(heap, arg);
    }
  }
  return ok;
}

#if MI_PPROF
// #267: called from mi_prof_start_seeded/mi_prof_stop (profile.c), deliberately OUTSIDE
// prof_lock so this never adds a new lock-ordering constraint against it. Walks every live
// theap of every subprocess (mi_subprocs -> heap->next -> theap->hnext) and syncs
// `prof_force_slow` -- Bun's zero-cost-when-off strategy: poisoning `pages_free_direct`
// (via `_mi_theap_pages_free_direct_poison`, page-queue.c) forces every malloc on that
// theap through `_mi_malloc_generic`, where the sampling countdown lives, for as long as
// profiling is running.
//
// Deliberately takes no `enable` argument and instead reads `mi_prof_is_enabled()` fresh
// for each theap, inside the lock that also guards a concurrently-created theap's own read
// of the same flag (see `_mi_theap_init`'s comment, theap.c). A start-then-stop (or
// stop-then-start) from two racing threads no longer has a "which walk wins" ordering
// hazard: mi_prof_start_seeded and mi_prof_stop both flip the global `prof_enabled` flag
// under `prof_lock` BEFORE either one's walk begins, so whichever walk actually visits a
// given theap last always writes that theap's CURRENT global state, not a value captured
// stale at the time its caller was invoked. (An earlier version took `enable` as a
// snapshot argument; a start-walk finishing after a chronologically-later stop-walk could
// then leave every theap poisoned while profiling was already off, or vice versa.)
//
// Lock order: mi_subprocs_lock -> subproc->heaps_lock -> heap->theaps_lock. Nothing else in
// this file nests `heap->theaps_lock` under `subproc->heaps_lock` today (`_mi_theap_init`,
// theap.c, takes them one after another, never nested), so this does not introduce a cycle.
//
// The poisoning write into `pages_free_direct` (only when the fresh read says enabled) is
// a plain cross-thread pointer write that the owning thread may concurrently read
// lock-free on its fast path -- imported from oven-sh/mimalloc @ 942b8342's
// `prof_force_slow` design (MIT). It is always safe regardless of timing: it only ever
// writes the static, immutable empty page, never a real (potentially soon-to-be-freed)
// one, so a stale or reordered read of it is never a dangling pointer, only a delayed
// poison (one more fast-path allocation slips through unsampled). It is also not the only
// thing keeping `pages_free_direct` correct while poisoned -- see
// `mi_theap_queue_first_update`'s matching comment (page-queue.c) for why the owning
// thread's OWN updates, not just this cross-thread write, are what make a poisoned entry
// safe to leave stale for a while rather than a use-after-free waiting to happen. Clearing
// the flag (fresh read says disabled) never touches `pages_free_direct` itself -- see
// `mi_prof_stop`'s comment (profile.c) for why.
void _mi_subproc_prof_sync_force_slow(void) {
  mi_lock(&mi_subprocs_lock) {
    for (mi_subproc_t* subproc = mi_subprocs; subproc != NULL; subproc = subproc->next) {
      mi_lock(&subproc->heaps_lock) {
        for (mi_heap_t* heap = subproc->heaps; heap != NULL; heap = heap->next) {
          mi_lock(&heap->theaps_lock) {
            for (mi_theap_t* theap = heap->theaps; theap != NULL; theap = theap->hnext) {
              const bool enable = mi_prof_is_enabled();
              theap->prof_force_slow = enable;
              if (enable) { _mi_theap_pages_free_direct_poison(theap); }
            }
          }
        }
      }
    }
  }
}
#endif


mi_subproc_t* _mi_subproc_main_init(void) {
  mi_lock_init(&mi_subprocs_lock);
  mi_memid_t memid = _mi_memid_create_static(&mi_process_subproc_main,sizeof(mi_subproc_t));
  mi_process_subproc_main.memid = memid;
  mi_subproc_init(&mi_process_subproc_main,NULL);
  return &mi_process_subproc_main;
}

void _mi_subproc_main_done(void) {
  mi_lock_done(&mi_subprocs_lock);
}

// #270: the handlers below are only ever registered/invoked off Windows and off
// wasi (see src/init.c's matching guard around the pthread_atfork registration, and
// alloc-override-zone.c which is macOS-only) -- guard the definitions themselves so
// this is not dead code on those platforms (rule 6). The prototypes in internal.h
// stay unconditional: nothing calls them outside this guard on those platforms, so an
// unguarded declaration with no definition is harmless and keeps the header simple.
#if !defined(_WIN32) && !defined(__wasi__)

/* -----------------------------------------------------------
  #270: pthread_atfork fork-safety handlers.
  See the lock-order block at the top of this file.
----------------------------------------------------------- */

// Cross-thread serialization + nested-call guard. See the file comment above for the
// full design rationale (why an owner+depth pair instead of a shared atomic counter,
// and why neither is `mi_decl_thread`).
static mi_lock_t mi_fork_serialize_lock = MI_LOCK_INITIALIZER;
static _Atomic(mi_threadid_t) mi_fork_owner;  // 0 = unowned; else the thread id currently mid-fork
static int mi_fork_depth;                      // nesting depth for `mi_fork_owner`; only touched while owning

// Mimalloc reserves the low bit of a thread id for exactly this purpose elsewhere
// (see diagnostic.c's `mi_lock_debug_thread`): some platform's `_mi_thread_id()` can
// legitimately return 0 for a real thread, which would collide with `mi_fork_owner`'s
// 0-means-unowned sentinel. Match that convention here.
static mi_threadid_t mi_fork_thread_id(void) {
  return (_mi_thread_id() | (mi_threadid_t)1);
}

#if (MI_DEBUG>1)
// #270: self-checks the documented lock order at the top of this file for ONE call to
// this handler -- it cannot see, and makes no claim about, the lock order any OTHER
// code path in the allocator takes these same locks in (see the file comment's "KEEP
// IN SYNC" note). Reset to 0 at the start of `_mi_process_fork_prepare`; each acquire
// below must pass a `lvl` no lower than the last one seen, or a future edit that
// reorders two steps (e.g. moves `arena_reserve_lock` ahead of `heaps_lock`) fails
// loudly here instead of only under a real, timing-dependent concurrent fork. `>=`,
// not `>`: `mi_subproc_fork_prepare` legitimately calls `mi_fork_acquire(5, ...)` once
// per non-main heap in the walked subproc (heap count is runtime-variable), and that
// repetition is the SAME numbered step (a sub-order within one heap), not a new one.
//
// Not thread-local, and does not need to be: only the thread currently holding
// `mi_fork_serialize_lock` (i.e. `mi_fork_owner`) ever runs the real prepare body that
// touches this, so it is exactly as safe as any other state protected by that lock.
static int mi_fork_lock_level;
static void mi_fork_lock_order_assert(int lvl) {
  mi_assert_internal(lvl >= mi_fork_lock_level);
  mi_fork_lock_level = lvl;
}
#define mi_fork_acquire(lvl,lock)   do { mi_fork_lock_order_assert(lvl); mi_lock_acquire(lock); } while(0)
#else
#define mi_fork_acquire(lvl,lock)   mi_lock_acquire(lock)
#endif

// See lock-order steps 4-8 at the top of this file. Non-main heaps before heap_main:
// see the file comment's `mi_heap_ensure_arena_pages` nesting note.
static void mi_subproc_fork_prepare(mi_subproc_t* sp) {
  mi_heap_t* const heap_main = _mi_subproc_heap_main(sp);
  mi_fork_acquire(4, &sp->heaps_lock);
  for (mi_heap_t* h = sp->heaps; h != NULL; h = h->next) {
    if (h == heap_main) continue;
    mi_fork_acquire(5, &h->arena_pages_lock);
    mi_lock_acquire(&h->theaps_lock);           // same step (5): sub-order within one heap, not a new level
    mi_lock_acquire(&h->os_abandoned_pages_lock);
  }
  if (heap_main != NULL) {
    mi_fork_acquire(6, &heap_main->arena_pages_lock);
    mi_lock_acquire(&heap_main->theaps_lock);   // same step (6): sub-order, not a new level
    mi_lock_acquire(&heap_main->os_abandoned_pages_lock);
  }
  // Phase 7: scavenger -- sp->tlds / sp->tlds_lock (per-thread tld->theaps_lock, park
  // state) would be quiesced here once the tld registry lands. See the file comment.
  mi_fork_acquire(7, &sp->arena_reserve_lock);
  mi_fork_acquire(8, &sp->theap_meta_lock);
}

// Exact reverse of mi_subproc_fork_prepare.
static void mi_subproc_fork_parent(mi_subproc_t* sp) {
  mi_heap_t* const heap_main = _mi_subproc_heap_main(sp);
  mi_lock_release(&sp->theap_meta_lock);
  mi_lock_release(&sp->arena_reserve_lock);
  // Phase 7: scavenger -- release sp->tlds_lock / per-tld locks here (see prepare).
  if (heap_main != NULL) {
    mi_lock_release(&heap_main->os_abandoned_pages_lock);
    mi_lock_release(&heap_main->theaps_lock);
    mi_lock_release(&heap_main->arena_pages_lock);
  }
  for (mi_heap_t* h = sp->heaps; h != NULL; h = h->next) {
    if (h == heap_main) continue;
    mi_lock_release(&h->os_abandoned_pages_lock);
    mi_lock_release(&h->theaps_lock);
    mi_lock_release(&h->arena_pages_lock);
  }
  mi_lock_release(&sp->heaps_lock);
}

// Reset every lock this subproc could have contributed to the walk above, plus the
// debug reentrancy-checker owner each carries (MI_DEBUG>2, see mi_lock_init). Order is
// irrelevant here: nothing is acquired, only re-initialized.
static void mi_subproc_fork_child(mi_subproc_t* sp) {
  mi_lock_init(&sp->arena_reserve_lock);
  mi_lock_init(&sp->heaps_lock);
  mi_lock_init(&sp->theap_meta_lock);
  // Phase 7: scavenger -- re-init sp->tlds_lock and every live tld->theaps_lock here
  // once the tld registry exists (see the file comment's KNOWN GAP note).
  for (mi_heap_t* h = sp->heaps; h != NULL; h = h->next) {
    mi_lock_init(&h->theaps_lock);
    mi_lock_init(&h->arena_pages_lock);
    mi_lock_init(&h->os_abandoned_pages_lock);
  }
}

void _mi_process_fork_prepare(void) {
  if (!_mi_process_is_initialized) return;
  const mi_threadid_t me = mi_fork_thread_id();
  if (mi_atomic_load_acquire(&mi_fork_owner) == me) {
    // Fast path for same-thread nesting (macOS zone callback + pthread_atfork firing
    // for the same fork()): we already hold `mi_fork_serialize_lock`, so touching
    // `mi_fork_depth` here is safe without re-acquiring anything. A stale/racy read of
    // `mi_fork_owner` that returns some OTHER value here is never a false positive
    // (thread ids are unique), only ever a miss that falls through to the slow path
    // below, which is always correct since `mi_fork_serialize_lock` is the real source
    // of truth.
    mi_fork_depth++;
    if (mi_fork_depth != 1) return;  // nested: outermost call for this thread already ran the body below
  }
  else {
    mi_lock_acquire(&mi_fork_serialize_lock);  // block until no other thread's fork generation is in flight
    mi_atomic_store_release(&mi_fork_owner, me);
    mi_fork_depth = 1;
  }
  #if (MI_DEBUG>1)
  mi_fork_lock_level = 0;
  #endif
  mi_fork_acquire(1, &mi_subprocs_lock);
  _mi_thread_locals_fork_prepare();               // step 2
  #if (MI_DEBUG>1)
  mi_fork_lock_order_assert(2);
  #endif
  mi_fork_acquire(3, &_mi_page_map()->lock);
  for (mi_subproc_t* sp = mi_subprocs; sp != NULL; sp = sp->next) {
    mi_subproc_fork_prepare(sp);
  }
  _mi_prof_fork_prepare();                        // step 9 (no-op when MI_PPROF is off)
  #if (MI_DEBUG>1)
  mi_fork_lock_order_assert(9);
  #endif
  _mi_dhat_fork_prepare();                        // step 10
  #if (MI_DEBUG>1)
  mi_fork_lock_order_assert(10);
  #endif
  _mi_memevt_fork_prepare();                      // step 11
  #if (MI_DEBUG>1)
  mi_fork_lock_order_assert(11);
  #endif
  _mi_options_fork_prepare();                     // step 12: innermost
  #if (MI_DEBUG>1)
  mi_fork_lock_order_assert(12);
  #endif
}

void _mi_process_fork_parent(void) {
  if (!_mi_process_is_initialized) return;
  // Symmetric with `prepare`, not a blind decrement: only the thread that actually
  // claimed ownership there does anything here. This is what makes the guard correct
  // even if `_mi_process_is_initialized` somehow differed between this thread's
  // `prepare` and `parent` calls (e.g. a very early fork() reached through the macOS
  // zone/DYLD-interpose path before `mi_process_init_once` has run) -- a `prepare`
  // that returned early left `mi_fork_owner` untouched, so this thread is never
  // mistaken for the owner and never decrements state it never incremented.
  if (mi_atomic_load_acquire(&mi_fork_owner) != mi_fork_thread_id()) return;
  mi_fork_depth--;
  if (mi_fork_depth != 0) return;  // still nested (same thread): outermost call handles the release
  _mi_options_fork_parent();
  _mi_memevt_fork_parent();
  _mi_dhat_fork_parent();
  _mi_prof_fork_parent();
  // release in reverse: last sub-process first (the registry is a stack: newest
  // pushed first, so walk it in reverse by counting then re-walking, same as Bun).
  size_t n = 0;
  for (mi_subproc_t* sp = mi_subprocs; sp != NULL; sp = sp->next) { n++; }
  while (n > 0) {
    mi_subproc_t* sp = mi_subprocs;
    for (size_t k = 1; k < n; k++) { sp = sp->next; }
    mi_subproc_fork_parent(sp);
    n--;
  }
  mi_lock_release(&_mi_page_map()->lock);
  _mi_thread_locals_fork_parent();
  mi_lock_release(&mi_subprocs_lock);
  mi_atomic_store_release(&mi_fork_owner, (mi_threadid_t)0);  // release ownership before the lock: a new
                                                                // owner must never observe the lock free but
                                                                // itself still "owned" by the outgoing thread
  mi_lock_release(&mi_fork_serialize_lock);  // let another thread's fork() (if any) proceed
}

void _mi_process_fork_child(void) {
  if (!_mi_process_is_initialized) return;
  // The child is single-threaded from here on -- only THIS thread's own (possibly
  // nested) prepare calls are relevant; no other thread's state exists in this
  // process to race against. Symmetric with `parent`'s ownership check, for the same
  // reason (a `prepare` that returned early must not be "reset" here either, though
  // in practice that only matters for the depth count -- resetting the shared locks
  // unconditionally below is always correct in the child regardless).
  if (mi_atomic_load_acquire(&mi_fork_owner) != mi_fork_thread_id()) return;
  mi_fork_depth = 0;
  mi_atomic_store_relaxed(&mi_fork_owner, (mi_threadid_t)0);
  mi_lock_init(&mi_fork_serialize_lock);  // fresh for this child's own future forks
  mi_lock_init(&mi_subprocs_lock);
  _mi_thread_locals_fork_child();
  _mi_prof_fork_child();
  _mi_dhat_fork_child();
  _mi_memevt_fork_child();
  mi_lock_init(&_mi_page_map()->lock);
  for (mi_subproc_t* sp = mi_subprocs; sp != NULL; sp = sp->next) {
    mi_subproc_fork_child(sp);
  }
  _mi_options_fork_child();
}


/* -----------------------------------------------------------
  #270: MI_DEBUG-only test hooks.

  The probabilistic repro in test/test-fork-locks.c (a churn thread racing
  mi_heap_new/mi_heap_delete against 200 forks) has near-zero discriminating power on
  a fast, lightly loaded machine: the real critical sections are microseconds, so the
  odds of a fork() landing inside one are low even across hundreds of iterations (see
  the #270 PR discussion -- 0/200 measured on both the pre- and post-fix tree here).
  These hooks let the test GUARANTEE a lock is held at the moment of fork(), instead of
  hoping for a race, giving the test real discriminating power.
----------------------------------------------------------- */
#if (MI_DEBUG>0)
static _Atomic(int) mi_test_heaps_lock_state;  // 0=not held, 1=held (ready to fork), 2=release requested

// Called from a dedicated "holder" thread. Blocks holding the main subprocess's
// `heaps_lock` until `_mi_test_release_heaps_lock` is called from another thread.
void _mi_test_hold_heaps_lock(void) {
  mi_lock_acquire(&mi_process_subproc_main.heaps_lock);
  mi_atomic_store_release(&mi_test_heaps_lock_state, 1);
  while (mi_atomic_load_acquire(&mi_test_heaps_lock_state) != 2) {
    _mi_prim_thread_yield();
  }
  mi_lock_release(&mi_process_subproc_main.heaps_lock);
  mi_atomic_store_release(&mi_test_heaps_lock_state, 0);
}

// Polled by the test's main thread before forking, to wait for the holder thread
// above to actually hold the lock (not just have been started).
bool _mi_test_heaps_lock_is_held(void) {
  return (mi_atomic_load_acquire(&mi_test_heaps_lock_state) == 1);
}

// Called from a thread OTHER than the holder to release it.
void _mi_test_release_heaps_lock(void) {
  mi_atomic_store_release(&mi_test_heaps_lock_state, 2);
}
#endif

#endif // !defined(_WIN32) && !defined(__wasi__)
