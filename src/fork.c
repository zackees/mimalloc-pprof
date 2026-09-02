/* ----------------------------------------------------------------------------
Copyright (c) 2018-2026, Microsoft Research, Daan Leijen
This is free software; you can redistribute it and/or modify it under the
terms of the MIT license. A copy of the license can be found in the file
"LICENSE" at the root of this distribution.
-----------------------------------------------------------------------------*/

#include "mimalloc.h"
#include "mimalloc/internal.h"
#include "mimalloc/prim.h"       // _mi_prim_thread_yield (test hooks, #270)

// #270 (Bun parity P5): everything in this file is POSIX-only. `pthread_atfork` does
// not exist on Windows (fork() does not either) and wasi is single-process; the
// registration in src/init.c and src/static.c's `#include "fork.c"` carry the same
// guard, so on those platforms this translation unit is empty by design.
#if !defined(_WIN32) && !defined(__wasi__)

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
  them (so the parent process never observes anything different from a world where
  fork() were a no-op); `child` runs instead in the (now single-threaded) child and
  must put every one of those locks back into a fresh, unlocked state. The child uses
  `mi_lock_init`, never `mi_lock_release`: the lock state copied from the parent does
  not correspond to *this* thread being the logical owner, and `mi_lock_init` also
  resets the `MI_DEBUG>2` reentrancy checker's `debug_owner` field (diagnostic.c's
  `_mi_lock_debug_init`), so the child's first real acquire is never flagged as "owner
  not cleared" against a parent-side thread id that no longer exists here -- thread ids
  can (and on some platforms do) get reused, so a `mi_lock_release`-only reset would be
  a false-negative waiting to happen, not just an asymmetry.

  Nothing in `prepare`/`parent`/`child` may allocate: `prepare` holds most of the
  allocator's internal locks by the time it finishes, and the child runs before any of
  them are reset. The only calls made from here are lock operations, list walks over
  structures those locks already protect, and (in debug builds) the lock-order
  self-check below, which writes to fixed, statically allocated storage.

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

  Ported design (not code) from oven-sh/mimalloc @ 942b8342, MIT (`src/subproc.c`'s
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

  =====================================================================================
  ==  LOCK ORDER  =====================================================================
  =====================================================================================

  The order below is NOT a policy choice and NOT a port of Bun's stated rule (which
  assumed a nesting structure this tree does not have, see the note at the end). It is
  a topological order of the ACTUAL nesting graph of this tree's internal locks:
  "X -> Y" below means some real code path holds X while acquiring Y, so X must be
  acquired BEFORE Y here (outer before inner). Every edge is cited with the file and
  the call chain that creates it. A prepare that took them in any other order could
  deadlock against a thread walking one of those paths, since prepare blocks on each
  acquire in turn.

  ---- Nesting graph (lock -> locks it may acquire while held) ----

    mi_subprocs_lock                subproc.c        registry of sub-processes
      -> sp->heaps_lock             `_mi_subproc_prof_sync_force_slow` (subproc.c) and
                                    `_mi_subprocs_unsafe_destroy_all` -> `mi_subproc_unsafe_destroy`
      -> heap->theaps_lock          (same, transitively)

    sp->heaps_lock                  subproc.c        the subproc's list of heaps
      -> heap->theaps_lock          `_mi_subproc_prof_sync_force_slow` (subproc.c)
      -> mi_thread_locals_lock      `mi_subproc_unsafe_destroy` -> `_mi_thread_locals_done` (threadlocal.c)
      -> heap->arena_pages_lock     `mi_subproc_unsafe_destroy` -> `_mi_heap_force_destroy` -> `mi_heap_free` (heap.c:203)
      -> heap->os_abandoned_pages_lock, sp->theap_meta_lock   (same teardown path, via frees)

    heap->theaps_lock               heap.c/theap.c   the heap's list of theaps
      -> sp->theap_meta_lock        `mi_heap_free_theaps` (heap.c:174) -> `_mi_theap_decref`
                                    -> `mi_theap_free_mem` -> `_mi_meta_free` (theap.c:363)
                                    ... and, for a page owned by `theap_meta`, `mi_free`
                                    -> `mi_stat_free` (free.c:741) takes `theap_meta_lock`
      -> tld->theaps_lock           `_mi_heap_detach_theaps` -- but `mi_lock_TRY_acquire`
                                    with a back-off retry (theap.c:412), so NOT a blocking
                                    edge; see the Phase 7 gap note below

    heap->arena_pages_lock          arena.c:685      per-heap arena page-info table
      (NON-main heap only; for `heap_main` the body is a plain atomic store, no nesting)
      -> heap_main->arena_pages_lock, sp->arena_reserve_lock, page_map->lock, hook locks
                                    `mi_heap_ensure_arena_pages` holds it across
                                    `mi_arena_pages_alloc` (arena.c:1544), which runs a FULL
                                    `mi_heap_zalloc_aligned(subproc->heap_main, ...)`
      -> sp->theap_meta_lock        `mi_heap_free` (heap.c:203-208) holds it across
                                    `_mi_free_subproc_safe` -> `mi_stat_free` (free.c:741)
      -> heap->os_abandoned_pages_lock   (same free, via `mi_arena_page_abandon`, arena.c:1224)

    mi_thread_locals_lock           threadlocal.c    TLS slot bitmap
      -> sp->theap_meta_lock        `_mi_thread_local_create` holds it across
                                    `mi_thread_local_create_expand` -> `_mi_meta_zalloc_aligned`
                                    (threadlocal.c:349); `_mi_thread_locals_done` likewise
                                    across `_mi_meta_free` (threadlocal.c:311)

    sp->theap_meta_lock             subproc.c:181    the detached meta theap
      -> heap_main->arena_pages_lock, sp->arena_reserve_lock, page_map->lock,
         heap_main->os_abandoned_pages_lock, hook locks
                                    `_mi_meta_zalloc` holds it across a full
                                    `mi_theap_zalloc(subproc->theap_meta, ...)`, and
                                    `theap_meta`'s heap IS `heap_main` (subproc.c:339,
                                    init.c's process bootstrap) -- so an ordinary
                                    allocation slow path runs inside this lock:
                                    `_mi_malloc_generic` -> `_mi_arenas_page_alloc`
                                    -> `mi_heap_ensure_arena_pages` (arena.c:685),
                                    -> `mi_arenas_try_alloc` -> `arena_reserve_lock` (arena.c:534),
                                    -> `_mi_page_map_register` -> `pmap->lock` (page-map.c:393)
                                    THIS is the edge the first version of this order got
                                    backwards (it took the page-map/arena locks BEFORE
                                    `theap_meta_lock`), which deadlocks against any thread
                                    starting up (`init.c:268` / `theap.c:329` allocate a
                                    fresh tld/theap through `_mi_meta_zalloc`).

    heap_main->arena_pages_lock     arena.c:685      LEAF. For the main heap
                                    `mi_heap_ensure_arena_pages` only stores
                                    `&arena->pages_main` -- it never allocates -- and
                                    `mi_heap_free` skips the arena-pages loop entirely for
                                    a main heap (`if (!is_main)`, heap.c:202).

    sp->arena_reserve_lock          arena.c:534      LEAF. `mi_arena_reserve` ->
                                    `mi_reserve_os_memory_ex2` -> `mi_arena_initialize` ->
                                    `mi_arenas_add` is raw-OS + atomics only; no mimalloc
                                    lock other than `out_buf_lock` (warnings).

    heap->os_abandoned_pages_lock   arena.c:1224     LEAF. Pure list splice.
    page_map->lock                  page-map.c:393   LEAF. `_mi_os_zalloc` of a submap only.

    prof_lock / dhat_lock / memevt_cb_lock                   INNERMOST (alloc/free HOOKS)
                                    Acquired by `_mi_prof_on_alloc`/`_mi_dhat_*`/
                                    `memevt_dispatch`, which alloc.c/free.c/page.c call from
                                    INSIDE the allocation path -- i.e. potentially with any
                                    of the locks above still held further up the stack.
                                    Their own critical sections take no allocator lock
                                    (profiler/DHAT memory comes from the raw-OS arena per
                                    CLAUDE.md rule 4; `memevt_dispatch` releases
                                    `memevt_cb_lock` before invoking the handler).
    out_buf_lock                    options.c:388    LAST: a plain memcpy into a fixed
                                    buffer, reachable from a warning message under any
                                    lock above.

  ---- Derived acquisition order (what `_mi_process_fork_prepare` does) ----

  A topological order of the graph above. Note that it is NOT a single per-subprocess
  walk: `mi_thread_locals_lock` is process-global but sits BETWEEN two per-subprocess
  levels (`sp->heaps_lock` -> ... -> `sp->theap_meta_lock`), and a heap's three locks do
  NOT share one level either (`theaps_lock` must precede `theap_meta_lock`, while
  `arena_pages_lock` of the MAIN heap must follow it). So prepare runs one pass per
  level, each pass walking all subprocesses / all heaps:

     1. mi_subprocs_lock
     2. for each sp:                sp->heaps_lock
     3. for each sp, each heap h:   h->theaps_lock
     4. for each sp, each NON-main h: h->arena_pages_lock
     5. mi_thread_locals_lock
     6. for each sp:                sp->theap_meta_lock
     7. for each sp:                sp->heap_main->arena_pages_lock
     8. for each sp:                sp->arena_reserve_lock
     9. for each sp, each heap h:   h->os_abandoned_pages_lock
    10. _mi_page_map()->lock
    11. prof_lock                   (profile.c, MI_PPROF; no-op otherwise)
    12. dhat_lock                   (dhat.c)
    13. memevt_cb_lock              (memory-events.c)
    14. out_buf_lock                (options.c)

  Both list walks are stable from step 2 onwards: `mi_subprocs` is pinned by step 1 and
  every `sp->heaps` by step 2, so later passes see exactly the same sets.

  `_mi_process_fork_parent` releases the levels in reverse. Within one level the order
  is irrelevant -- only ACQUIRE order can deadlock -- so each release pass walks its
  list forward rather than reconstructing a reverse walk.

  // Phase 7: scavenger -- Bun also quiesces a per-subprocess `tlds` registry and its own
  // `tlds_lock` here (walking every thread's `tld->theaps_lock` and park state). That
  // registry does not exist yet in this tree; it lands with the scavenger (#264 item 7 /
  // #272). `mi_tld_t::theaps_lock` (types.h, "sometimes accessed from another thread on
  // mi_heap_free") is therefore a KNOWN GAP not covered by this phase -- see the P5 PR
  // description and MIMALLOC_FORKS.md. It is deliberately not approximated by an ad hoc
  // walk here: a heap's theaps list can reach the same `tld` through more than one theap
  // (one thread, several heaps), and locking a non-recursive mutex twice would turn a
  // currently-rare hang into a guaranteed one inside this very handler.

  ---- Why Bun's stated rule does not transfer ----

  Bun's rule -- "a lock that can still be held while a call comes back into mimalloc
  must be taken before `arena_reserve_lock`" -- assumes the hook locks are never taken
  from inside the plain allocation path itself, which is false here:
  `prof_lock`/`dhat_lock`/`memevt_cb_lock` are acquired by the alloc/free HOOKS while a
  heap's `arena_pages_lock` can still be held a few frames up the same stack. Taking the
  hook locks BEFORE the heap/arena locks, as Bun's rule and the first version of this
  file did, produces a real, deterministic AB-BA deadlock under `MIMALLOC_PROF=1`
  (reproduced; see the #270 PR discussion). Hooks go last here.

  ---- Residual, PRE-EXISTING hazards this phase does NOT close ----

  Both are inversions that exist with no fork() involved at all, and closing either means
  redesigning the API contract around it, which is out of scope for a fork-safety phase:

   * `mi_prof_visit` (profile.c) holds `prof_lock` across a user-supplied visitor
     callback. If that visitor allocates, the real nesting is `prof_lock` OUTER and the
     heap/arena locks INNER -- the reverse of the alloc-hook path. Contract documented at
     `mi_prof_visit`'s declaration (profile.h): a visitor must not allocate.
     (`mi_prof_snapshot_visit` is unaffected: it visits an already-copied snapshot under
     no lock at all.)
   * `mi_out_buf_flush` (options.c:411) calls the registered `mi_output_fun` while
     holding `out_buf_lock`; an output function that allocates inverts the innermost
     level. That is upstream mimalloc's own contract for `mi_register_output`.

  In an `MI_DEBUG>2` build the checker below OBSERVES both of these if they ever happen,
  and reports them at the next fork() -- see `mi_fork_lock_order_check`.
----------------------------------------------------------- */

// The documented levels above, as an enum: `prepare` tags every acquire with one, the
// MI_DEBUG>1 sequence check asserts they are non-decreasing within one prepare, and the
// MI_DEBUG>2 checker uses them to classify locks seen elsewhere in the process.
typedef enum mi_fork_lock_level_e {
  MI_FORK_LOCK_NONE              = 0,
  MI_FORK_LOCK_SUBPROCS          = 1,
  MI_FORK_LOCK_HEAPS             = 2,
  MI_FORK_LOCK_THEAPS            = 3,
  MI_FORK_LOCK_ARENA_PAGES       = 4,   // non-main heaps
  MI_FORK_LOCK_THREAD_LOCALS     = 5,
  MI_FORK_LOCK_THEAP_META        = 6,
  MI_FORK_LOCK_ARENA_PAGES_MAIN  = 7,
  MI_FORK_LOCK_ARENA_RESERVE     = 8,
  MI_FORK_LOCK_OS_ABANDONED      = 9,
  MI_FORK_LOCK_PAGE_MAP          = 10,
  MI_FORK_LOCK_PROF              = 11,
  MI_FORK_LOCK_DHAT              = 12,
  MI_FORK_LOCK_MEMEVT            = 13,
  MI_FORK_LOCK_OUT_BUF           = 14,
  MI_FORK_LOCK_LEVEL_COUNT       = 15
} mi_fork_lock_level_t;

#if MI_FORK_LOCK_ORDER_CHECK
static const char* mi_fork_lock_level_name(int lvl) {
  switch (lvl) {
    case MI_FORK_LOCK_SUBPROCS:         return "mi_subprocs_lock";
    case MI_FORK_LOCK_HEAPS:            return "subproc->heaps_lock";
    case MI_FORK_LOCK_THEAPS:           return "heap->theaps_lock";
    case MI_FORK_LOCK_ARENA_PAGES:      return "heap->arena_pages_lock";
    case MI_FORK_LOCK_THREAD_LOCALS:    return "mi_thread_locals_lock";
    case MI_FORK_LOCK_THEAP_META:       return "subproc->theap_meta_lock";
    case MI_FORK_LOCK_ARENA_PAGES_MAIN: return "heap_main->arena_pages_lock";
    case MI_FORK_LOCK_ARENA_RESERVE:    return "subproc->arena_reserve_lock";
    case MI_FORK_LOCK_OS_ABANDONED:     return "heap->os_abandoned_pages_lock";
    case MI_FORK_LOCK_PAGE_MAP:         return "page_map->lock";
    case MI_FORK_LOCK_PROF:             return "prof_lock";
    case MI_FORK_LOCK_DHAT:             return "dhat_lock";
    case MI_FORK_LOCK_MEMEVT:           return "memevt_cb_lock";
    case MI_FORK_LOCK_OUT_BUF:          return "out_buf_lock";
    default:                            return "?";
  }
}
#endif

// Cross-thread serialization + nested-call guard. See the file comment above for the
// full design rationale (why an owner+depth pair instead of a shared atomic counter,
// and why neither is `mi_decl_thread`).
static mi_lock_t mi_fork_serialize_lock = MI_LOCK_INITIALIZER;
static _Atomic(mi_threadid_t) mi_fork_owner;   // 0 = unowned; else the thread id currently mid-fork
static int mi_fork_depth;                      // nesting depth for `mi_fork_owner`; only touched while owning

// imported from oven-sh/mimalloc @ 942b8342, MIT (issue #271 / Bun parity P6): set once in
// `_mi_process_fork_child`, never cleared. The child is single-threaded from the moment
// `fork()` returns, but it can still be holding page-map/arena-pages bits published by a
// sibling thread that did not survive the fork -- that thread's theap may be torn mid
// -update (an allocation or free that was between its bitmap-set and its owned-bit-set/
// bookkeeping-update when the fork happened). `mi_heap_visit_page_claim` (arena.c) and
// `mi_heap_detach_theaps` (heap.c) consult this flag to avoid waiting on / walking pages
// belonging to those dead threads, since they will never be relinquished normally.
mi_decl_hidden bool _mi_process_is_forked_child;

// Mimalloc reserves the low bit of a thread id for exactly this purpose elsewhere
// (see diagnostic.c's `mi_lock_debug_thread`): some platform's `_mi_thread_id()` can
// legitimately return 0 for a real thread, which would collide with `mi_fork_owner`'s
// 0-means-unowned sentinel. Match that convention here.
static mi_threadid_t mi_fork_thread_id(void) {
  return (_mi_thread_id() | (mi_threadid_t)1);
}


/* -----------------------------------------------------------
  #270: lock-order self-checks.

  Two independent, complementary checks:

  (a) MI_DEBUG>1 -- SEQUENCE check. Purely local: it asserts that this handler's own
      acquires happen in non-decreasing level order, so an edit that swaps two steps in
      `_mi_process_fork_prepare` fails loudly instead of only under a timing-dependent
      concurrent fork. It says nothing about what any OTHER code path does.

  (b) MI_DEBUG>2 -- OBSERVED-EDGE check (the real one). Every internal lock acquire in
      the process already goes through diagnostic.c's reentrancy checker, which records
      the owning thread in `mi_lock_t::debug_owner` and clears it on release. So at any
      acquire we can read off which OTHER tracked locks this same thread already holds,
      and record the resulting nesting edge "level(held) -> level(acquired)" in a global
      bitmap. `mi_fork_lock_order_check`, run at the top of every `prepare`, then asserts
      that every edge observed SO FAR is consistent with the documented order, i.e. that
      no thread was ever seen holding an inner lock while acquiring an outer one. That
      turns the order above from a comment into a runtime detector: the exact inversion
      this file shipped with in its first two revisions (`theap_meta_lock` acquired after
      the page-map/arena locks) is reported by an ordinary Debug-FULL test run.

  Scope of (b): only locks with PROCESS-LIFETIME storage are tracked -- the main
  subprocess's own three locks, the process main heap's three, and the five global ones.
  A non-main heap's locks are freed with the heap (heap.c's `mi_heap_free`), and this
  table is keyed by address, so tracking them would mean reading a `debug_owner` field
  out of freed memory; they are deliberately left unclassified (their level is simply
  never observed). Everything in the inversion this check exists to catch lives in the
  tracked set. Locks are registered lazily, by `prepare` itself: `mi_fork_declare_level`
  is set around each tracked acquire, and the observer (running on the fork owner thread,
  which holds `mi_fork_serialize_lock`, so no other declare can race) registers whatever
  address arrives. Consequently the table is populated by the FIRST fork() of the process
  and edges are recorded from then on. Acquires made by `prepare` ITSELF are excluded:
  prepare deliberately holds every level at once, in the documented order, so recording
  its own nesting would just re-derive that order and make the check tautological. Only
  ordinary allocator paths count as evidence.

  Coverage is therefore a property of the workload, not of the mechanism: it reports an
  inversion only for nestings the process actually performs after its first fork(). In
  `test/test-fork-locks.c` (200 forks, a heap-churn thread and, in spawn mode, continuous
  thread starts) the edges observed are `mi_subprocs_lock -> heaps_lock -> theaps_lock`
  and `mi_thread_locals_lock -> theap_meta_lock`; the `theap_meta_lock -> arena/page-map`
  edges are real but rare (a meta allocation only reaches `mi_heap_ensure_arena_pages` /
  `mi_arena_reserve` / page-map growth on a cold page, which is why the inversion this
  file shipped with was a latent rather than an immediately reproducible deadlock).
  End-to-end verification that the check does fire: deliberately swapping the documented
  levels of `mi_thread_locals_lock` and `theap_meta_lock` (and prepare's matching acquire
  order) makes an ordinary `test-fork-locks` run report
  "fork lock-order violation: mi_thread_locals_lock (step 6) was held while acquiring
  subproc->theap_meta_lock (step 5)". See the #270 PR discussion.
----------------------------------------------------------- */

#if (MI_DEBUG>1)
// (a) sequence check. `>=`, not `>`: one numbered level is acquired once per subprocess
// (and per heap for the per-heap levels), and that repetition is the SAME step.
static int mi_fork_lock_level;
static void mi_fork_lock_order_assert(int lvl) {
  mi_assert_internal(lvl >= mi_fork_lock_level);
  mi_fork_lock_level = lvl;
}
#else
static void mi_fork_lock_order_assert(int lvl) { MI_UNUSED(lvl); }
#endif

#if MI_FORK_LOCK_ORDER_CHECK
#define MI_FORK_TRACKED_MAX  (16)
static _Atomic(uintptr_t) mi_fork_tracked_lock[MI_FORK_TRACKED_MAX];   // lock address (published last)
static unsigned char      mi_fork_tracked_level[MI_FORK_TRACKED_MAX];
static _Atomic(size_t)    mi_fork_tracked_count;
static _Atomic(uintptr_t) mi_fork_observed[MI_FORK_LOCK_LEVEL_COUNT];  // row i, bit j: level i was held while acquiring level j
static int                mi_fork_declare_level;                       // != 0 while `prepare` acquires a tracked lock
static bool               mi_fork_order_reported;                      // report each violation set once

// Same tagging as diagnostic.c's `mi_lock_debug_thread` -- must match, since we compare
// against the `debug_owner` values it writes.
static uintptr_t mi_fork_debug_thread(void) {
  return ((uintptr_t)_mi_thread_id() | (uintptr_t)1);
}

static int mi_fork_tracked_level_of(const mi_lock_t* lock, size_t n) {
  for (size_t i = 0; i < n; i++) {
    if (mi_atomic_load_relaxed(&mi_fork_tracked_lock[i]) == (uintptr_t)lock) { return (int)mi_fork_tracked_level[i]; }
  }
  return MI_FORK_LOCK_NONE;
}

// Called from diagnostic.c's `_mi_lock_debug_after_acquire`, i.e. after `lock`'s
// `debug_owner` has been set to this thread. Never allocates and takes no lock.
void _mi_fork_lock_order_observe(const mi_lock_t* lock) {
  const size_t n = mi_atomic_load_acquire(&mi_fork_tracked_count);
  int lvl = mi_fork_tracked_level_of(lock, n);
  if (lvl == MI_FORK_LOCK_NONE) {
    // not tracked yet: register it if `prepare` (which alone knows the documented level,
    // and alone holds `mi_fork_serialize_lock`) is the one acquiring it right now.
    if (mi_fork_declare_level == MI_FORK_LOCK_NONE) return;
    if (mi_atomic_load_acquire(&mi_fork_owner) != mi_fork_thread_id()) return;
    if (n >= MI_FORK_TRACKED_MAX) return;
    lvl = mi_fork_declare_level;
    mi_fork_tracked_level[n] = (unsigned char)lvl;
    mi_atomic_store_release(&mi_fork_tracked_lock[n], (uintptr_t)lock);
    mi_atomic_store_release(&mi_fork_tracked_count, n + 1);
  }
  // Record an edge from every other tracked lock this thread already holds -- but never
  // from inside `prepare` itself: prepare deliberately holds every level at once, in the
  // documented order, so recording its own acquires would just re-derive that order and
  // make the check tautological. Only ORDINARY allocator paths are evidence here.
  if (mi_atomic_load_acquire(&mi_fork_owner) == mi_fork_thread_id()) return;
  const uintptr_t me = mi_fork_debug_thread();
  for (size_t i = 0; i < n; i++) {
    const uintptr_t addr = mi_atomic_load_relaxed(&mi_fork_tracked_lock[i]);
    if (addr == 0 || addr == (uintptr_t)lock) continue;
    if (mi_atomic_load_relaxed(&((const mi_lock_t*)addr)->debug_owner) != me) continue;
    const int held = (int)mi_fork_tracked_level[i];
    uintptr_t row = mi_atomic_load_relaxed(&mi_fork_observed[held]);
    const uintptr_t bit = ((uintptr_t)1 << lvl);
    while ((row & bit) == 0) {
      if (mi_atomic_cas_weak_acq_rel(&mi_fork_observed[held], &row, row | bit)) break;
    }
  }
}

// (b) the real check: assert the documented order against every edge observed so far.
// Run at the TOP of `prepare`, before any lock is taken -- reporting goes through
// `_mi_error_message`, which itself takes `out_buf_lock`.
static void mi_fork_lock_order_check(void) {
  if (mi_fork_order_reported) return;
  for (int held = 1; held < MI_FORK_LOCK_LEVEL_COUNT; held++) {
    const uintptr_t row = mi_atomic_load_relaxed(&mi_fork_observed[held]);
    if (row == 0) continue;
    for (int acq = 1; acq < held; acq++) {   // acq < held == an inner lock held while taking an outer one
      if ((row & ((uintptr_t)1 << acq)) != 0) {
        mi_fork_order_reported = true;
        _mi_error_message(EFAULT,
          "fork lock-order violation: %s (step %d) was held while acquiring %s (step %d) -- "
          "the order documented in src/fork.c is wrong, or a new nesting was introduced\n",
          mi_fork_lock_level_name(held), held, mi_fork_lock_level_name(acq), acq);
        mi_assert_internal(false);
      }
    }
  }
}

#define mi_fork_declare(lvl)      (mi_fork_declare_level = (lvl))
#define mi_fork_declare_end()     (mi_fork_declare_level = MI_FORK_LOCK_NONE)
#else
static void mi_fork_lock_order_check(void) { }
#define mi_fork_declare(lvl)      (void)0
#define mi_fork_declare_end()     (void)0
#endif // MI_FORK_LOCK_ORDER_CHECK

// Acquire a lock that is one of the documented steps. `tracked` says whether its storage
// lives for the whole process (see the scope note above); only those get classified.
#define mi_fork_acquire(lvl,lock)        do { mi_fork_lock_order_assert(lvl); mi_fork_declare(lvl); mi_lock_acquire(lock); mi_fork_declare_end(); } while(0)
#define mi_fork_acquire_local(lvl,lock)  do { mi_fork_lock_order_assert(lvl); mi_lock_acquire(lock); } while(0)
// Same, for a step whose acquire lives in another file (`_mi_*_fork_prepare`).
#define mi_fork_enter(lvl)               do { mi_fork_lock_order_assert(lvl); mi_fork_declare(lvl); } while(0)
#define mi_fork_leave()                  mi_fork_declare_end()


/* -----------------------------------------------------------
  #270: the handlers. See the LOCK ORDER block above; the pass structure below is that
  order, one pass per level.
----------------------------------------------------------- */

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
  mi_fork_lock_order_check();   // before taking anything: it may print

  mi_lock_t* const subprocs_lock = _mi_subprocs_lock();
  mi_fork_acquire(MI_FORK_LOCK_SUBPROCS, subprocs_lock);                        // 1
  mi_subproc_t* const sp_main = _mi_subproc_main();

  for (mi_subproc_t* sp = _mi_subprocs_head(); sp != NULL; sp = sp->next) {     // 2
    if (sp == sp_main) { mi_fork_acquire(MI_FORK_LOCK_HEAPS, &sp->heaps_lock); }
                  else { mi_fork_acquire_local(MI_FORK_LOCK_HEAPS, &sp->heaps_lock); }
  }
  for (mi_subproc_t* sp = _mi_subprocs_head(); sp != NULL; sp = sp->next) {     // 3
    mi_heap_t* const heap_main = _mi_subproc_heap_main(sp);
    for (mi_heap_t* h = sp->heaps; h != NULL; h = h->next) {
      if (h == heap_main && sp == sp_main) { mi_fork_acquire(MI_FORK_LOCK_THEAPS, &h->theaps_lock); }
                                      else { mi_fork_acquire_local(MI_FORK_LOCK_THEAPS, &h->theaps_lock); }
    }
  }
  for (mi_subproc_t* sp = _mi_subprocs_head(); sp != NULL; sp = sp->next) {     // 4
    mi_heap_t* const heap_main = _mi_subproc_heap_main(sp);
    for (mi_heap_t* h = sp->heaps; h != NULL; h = h->next) {
      if (h == heap_main) continue;   // main heap's arena_pages_lock is step 7
      mi_fork_acquire_local(MI_FORK_LOCK_ARENA_PAGES, &h->arena_pages_lock);
    }
  }
  mi_fork_enter(MI_FORK_LOCK_THREAD_LOCALS); _mi_thread_locals_fork_prepare(); mi_fork_leave();   // 5
  // Phase 7: scavenger -- sp->tlds / sp->tlds_lock (per-thread tld->theaps_lock, park
  // state) would be quiesced here once the tld registry lands. See the file comment.
  for (mi_subproc_t* sp = _mi_subprocs_head(); sp != NULL; sp = sp->next) {     // 6
    if (sp == sp_main) { mi_fork_acquire(MI_FORK_LOCK_THEAP_META, &sp->theap_meta_lock); }
                  else { mi_fork_acquire_local(MI_FORK_LOCK_THEAP_META, &sp->theap_meta_lock); }
  }
  for (mi_subproc_t* sp = _mi_subprocs_head(); sp != NULL; sp = sp->next) {     // 7
    mi_heap_t* const heap_main = _mi_subproc_heap_main(sp);
    if (heap_main == NULL) continue;
    if (sp == sp_main) { mi_fork_acquire(MI_FORK_LOCK_ARENA_PAGES_MAIN, &heap_main->arena_pages_lock); }
                  else { mi_fork_acquire_local(MI_FORK_LOCK_ARENA_PAGES_MAIN, &heap_main->arena_pages_lock); }
  }
  for (mi_subproc_t* sp = _mi_subprocs_head(); sp != NULL; sp = sp->next) {     // 8
    if (sp == sp_main) { mi_fork_acquire(MI_FORK_LOCK_ARENA_RESERVE, &sp->arena_reserve_lock); }
                  else { mi_fork_acquire_local(MI_FORK_LOCK_ARENA_RESERVE, &sp->arena_reserve_lock); }
  }
  for (mi_subproc_t* sp = _mi_subprocs_head(); sp != NULL; sp = sp->next) {     // 9
    mi_heap_t* const heap_main = _mi_subproc_heap_main(sp);
    for (mi_heap_t* h = sp->heaps; h != NULL; h = h->next) {
      if (h == heap_main && sp == sp_main) { mi_fork_acquire(MI_FORK_LOCK_OS_ABANDONED, &h->os_abandoned_pages_lock); }
                                      else { mi_fork_acquire_local(MI_FORK_LOCK_OS_ABANDONED, &h->os_abandoned_pages_lock); }
    }
  }
  mi_fork_acquire(MI_FORK_LOCK_PAGE_MAP, &_mi_page_map()->lock);                                 // 10
  mi_fork_enter(MI_FORK_LOCK_PROF);   _mi_prof_fork_prepare();    mi_fork_leave();               // 11 (no-op when MI_PPROF is off)
  mi_fork_enter(MI_FORK_LOCK_DHAT);   _mi_dhat_fork_prepare();    mi_fork_leave();               // 12
  mi_fork_enter(MI_FORK_LOCK_MEMEVT); _mi_memevt_fork_prepare();  mi_fork_leave();               // 13
  mi_fork_enter(MI_FORK_LOCK_OUT_BUF);_mi_options_fork_prepare(); mi_fork_leave();               // 14: innermost
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
  // Release the levels in reverse. Within a level the order does not matter (only
  // acquire order can deadlock), so each pass walks its list forward.
  _mi_options_fork_parent();                                                   // 14
  _mi_memevt_fork_parent();                                                    // 13
  _mi_dhat_fork_parent();                                                      // 12
  _mi_prof_fork_parent();                                                      // 11
  mi_lock_release(&_mi_page_map()->lock);                                      // 10
  for (mi_subproc_t* sp = _mi_subprocs_head(); sp != NULL; sp = sp->next) {    // 9
    for (mi_heap_t* h = sp->heaps; h != NULL; h = h->next) { mi_lock_release(&h->os_abandoned_pages_lock); }
  }
  for (mi_subproc_t* sp = _mi_subprocs_head(); sp != NULL; sp = sp->next) {    // 8
    mi_lock_release(&sp->arena_reserve_lock);
  }
  for (mi_subproc_t* sp = _mi_subprocs_head(); sp != NULL; sp = sp->next) {    // 7
    mi_heap_t* const heap_main = _mi_subproc_heap_main(sp);
    if (heap_main != NULL) { mi_lock_release(&heap_main->arena_pages_lock); }
  }
  for (mi_subproc_t* sp = _mi_subprocs_head(); sp != NULL; sp = sp->next) {    // 6
    mi_lock_release(&sp->theap_meta_lock);
  }
  _mi_thread_locals_fork_parent();                                             // 5
  // Phase 7: scavenger -- release sp->tlds_lock / per-tld locks here (see prepare).
  for (mi_subproc_t* sp = _mi_subprocs_head(); sp != NULL; sp = sp->next) {    // 4
    mi_heap_t* const heap_main = _mi_subproc_heap_main(sp);
    for (mi_heap_t* h = sp->heaps; h != NULL; h = h->next) {
      if (h == heap_main) continue;
      mi_lock_release(&h->arena_pages_lock);
    }
  }
  for (mi_subproc_t* sp = _mi_subprocs_head(); sp != NULL; sp = sp->next) {    // 3
    for (mi_heap_t* h = sp->heaps; h != NULL; h = h->next) { mi_lock_release(&h->theaps_lock); }
  }
  for (mi_subproc_t* sp = _mi_subprocs_head(); sp != NULL; sp = sp->next) {    // 2
    mi_lock_release(&sp->heaps_lock);
  }
  mi_lock_release(_mi_subprocs_lock());                                        // 1
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
  _mi_process_is_forked_child = true;  // set before anything below can touch a heap/theap (issue #271)
  mi_fork_depth = 0;
  mi_atomic_store_relaxed(&mi_fork_owner, (mi_threadid_t)0);
  mi_lock_init(&mi_fork_serialize_lock);  // fresh for this child's own future forks
  // Reset every lock the walk above could have taken, plus the debug reentrancy-checker
  // owner each carries (MI_DEBUG>2, see mi_lock_init). Order is irrelevant here: nothing
  // is acquired, only re-initialized.
  mi_lock_init(_mi_subprocs_lock());
  _mi_thread_locals_fork_child();
  _mi_prof_fork_child();
  _mi_dhat_fork_child();
  _mi_memevt_fork_child();
  mi_lock_init(&_mi_page_map()->lock);
  for (mi_subproc_t* sp = _mi_subprocs_head(); sp != NULL; sp = sp->next) {
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
  _mi_options_fork_child();
}


/* -----------------------------------------------------------
  #270: MI_DEBUG-only test hooks.

  The probabilistic repro in test/test-fork-locks.c (a churn thread racing
  mi_heap_new/mi_heap_delete against 200 forks) has near-zero discriminating power on a
  fast, lightly loaded machine: the real critical sections are microseconds, so the odds
  of a fork() landing inside one are low even across hundreds of iterations (measured
  0/200 on both the pre- and post-fix tree here -- see the #270 PR discussion).

  These hooks let the test GUARANTEE that `heaps_lock` is held at the moment of fork(),
  AND make that fact observable in the child. A holder thread takes the main
  subprocess's `heaps_lock` and, while holding it, POISONS the structure that lock
  guards: it detaches `sp->heaps` (saving the head) so the list the lock protects is
  transiently, visibly wrong. It restores the list and clears the saved head before
  releasing, so the poisoned window is exactly the locked window.

  A correct `_mi_process_fork_prepare` therefore cannot fork inside that window: it
  blocks on `heaps_lock` until the holder restores and releases, and the child observes
  a normal heap list. If a future change turned that acquire into a no-op (or moved
  `heaps_lock` out of the prepare walk), fork() would proceed immediately and the child
  would inherit `sp->heaps == NULL` with a non-NULL saved head -- which
  `_mi_test_heaps_lock_poison_observed` reports, and the test fails. Verified by
  deliberately no-op'ing the acquire: 20/20 children observe the poison; with the acquire
  restored, 0/20. See the #270 PR discussion.
----------------------------------------------------------- */
#if (MI_DEBUG>0)
static _Atomic(int) mi_test_heaps_lock_state;   // 0=not held, 1=held+poisoned (ready to fork), 2=release requested
static mi_heap_t*   mi_test_heaps_saved;        // non-NULL only inside the poisoned window

// Called from a dedicated "holder" thread. Blocks holding the main subprocess's
// `heaps_lock`, with `sp->heaps` detached, until `_mi_test_release_heaps_lock` is
// called from another thread.
void _mi_test_hold_heaps_lock(void) {
  mi_subproc_t* const sp = _mi_subproc_main();
  mi_lock_acquire(&sp->heaps_lock);
  mi_test_heaps_saved = sp->heaps;    // poison: the guarded list is detached while we hold the lock
  sp->heaps = NULL;
  mi_atomic_store_release(&mi_test_heaps_lock_state, 1);
  while (mi_atomic_load_acquire(&mi_test_heaps_lock_state) != 2) {
    _mi_prim_thread_yield();
  }
  sp->heaps = mi_test_heaps_saved;    // restore BEFORE clearing `saved`, so no window reads as poisoned
  mi_test_heaps_saved = NULL;
  mi_lock_release(&sp->heaps_lock);
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

// Called in the CHILD after a fork(): true iff this process's copy of the main
// subprocess caught the poisoned window, i.e. fork() happened while the holder thread
// still held `heaps_lock` -- which a working `_mi_process_fork_prepare` makes impossible.
bool _mi_test_heaps_lock_poison_observed(void) {
  return (mi_test_heaps_saved != NULL && _mi_subproc_main()->heaps == NULL);
}
#endif // MI_DEBUG>0

#endif // !defined(_WIN32) && !defined(__wasi__)
