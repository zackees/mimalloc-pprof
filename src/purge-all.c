/* ----------------------------------------------------------------------------
Copyright (c) 2026, the mimalloc-pprof contributors
This is free software; you can redistribute it and/or modify it under the
terms of the MIT license. A copy of the license can be found in the file
"LICENSE" at the root of this distribution.
-----------------------------------------------------------------------------*/

// #366: `mi_purge_all` -- process-wide eager purge from any thread.
//
// The driver of docs/purge-all-implementation.md §7: from one thread, return as much memory as
// the allocator's invariants allow across EVERY thread's heaps -- arenas, abandoned pages, the
// caller's own theaps, and (through the park protocol) every other thread's theaps and holes --
// and report exactly what it could not reach. Included from `src/static.c` unconditionally: the
// walk is the same in both builds, only the reach differs (§1): in a default build a thread is
// PARKED only inside `mi_on_thread_idle_start`, so a RUNNING owner will never park and is
// reported pending on the first pass; in a gated build (MI_OWNER_GATE) every thread outside the
// allocator is PARKED and is claimed as soon as its current allocator call returns.
//
// CLAUDE.md rule 4: nothing here allocates on a hooked path. The report is caller-owned, the
// pass counters live on the stack, and every hole discard goes through `_mi_page_purge_holes`
// with its `_mi_prof_debug_assert_no_records_in`.

#include "mimalloc.h"
#include "mimalloc/internal.h"
#include "mimalloc/prim.h"      // _mi_prim_thread_yield
#include "mimalloc/prim-tls.h"  // _mi_theap_default

// Process-wide (§3). `_mi_purge_admission` is the holder's thread id (0 = free): two purges
// cannot overlap, and a re-entrant call (a deferred-free handler or output hook calling
// `mi_purge_all` while one is in flight on this very thread) sees its own id and is BUSY too.
// `_mi_purge_seq` is the walk epoch: `mi_tld_register` (src/init.c) stamps it into a new tld's
// `purge_epoch` under `tlds_lock`, so a thread born during a walk is already "done" for it and
// thread churn cannot extend the walk (§7.1 registry cutoff).
mi_decl_hidden _Atomic(uintptr_t) _mi_purge_admission;   // holder thread id, 0 = free
mi_decl_hidden _Atomic(size_t)    _mi_purge_seq;         // walk epoch

void _mi_purge_all_fork_child(void) {
  mi_atomic_store_relaxed(&_mi_purge_admission, (uintptr_t)0);
}

// #272/#366 (MSVC-C): the MSVC C atomics wrapper is word-width only, so every CAS out-param
// here is a `size_t`/`uintptr_t` local, never a narrower one (see the note in scavenger.c).
typedef size_t mi_purge_park_state_t;

/* -----------------------------------------------------------
  Stat snapshots for the report

  Both numbers are deltas of monotonic process-wide counters, so a concurrent purge by anyone
  else (the scavenger's timer, another thread's `mi_collect`) during this call is attributed
  to it -- a report is "what left during this call", not "what this call's own madvise calls
  discarded", and it never under-reports what the caller asked for.

  - `hole_bytes`: `mi_purge_holes_stats_get().purged_bytes_total`, bytes ever discarded by
    hole punching (src/page-holes.c).
  - `arena_bytes`: the sum over subprocs of `stats.purged.total` -- which `_mi_os_purge*` AND
    `_mi_os_discard` (the hole path) both feed -- minus the hole delta, clamped at zero. So it
    is the OS-level bytes purged by the arena passes (A, E, and the collects' page frees).
----------------------------------------------------------- */

typedef struct mi_purge_snapshot_s {
  int64_t purged_total;   // sum of subproc `stats.purged.total`
  size_t  hole_total;     // `purged_bytes_total`
} mi_purge_snapshot_t;

static void mi_purge_snapshot(mi_purge_snapshot_t* snap) {
  int64_t purged = 0;
  mi_lock(_mi_subprocs_lock()) {
    for (mi_subproc_t* sp = _mi_subprocs_head(); sp != NULL; sp = sp->next) {
      purged += mi_atomic_loadi64_relaxed((_Atomic(int64_t)*)&sp->stats.purged.total);
    }
  }
  mi_purge_holes_stats_t holes;
  mi_purge_holes_stats_get(&holes);
  snap->purged_total = purged;
  snap->hole_total   = holes.purged_bytes_total;
}

/* -----------------------------------------------------------
  Phases A / B / E: arenas and abandoned pages, per subproc (§7)

  Phase A runs under no lock but `mi_subprocs_lock` (held only to walk the list; a subproc is
  never freed while registered and in use, and the arena purge takes no lock that nests under
  it). A FORCED arena purge waits for the arena purge guard instead of skipping -- see the
  guard comment in src/arena.c, which names this function as the second forced caller and
  spells out why that wait is bounded.
----------------------------------------------------------- */

static void mi_purge_all_arenas(bool force) {
  mi_lock(_mi_subprocs_lock()) {
    for (mi_subproc_t* sp = _mi_subprocs_head(); sp != NULL; sp = sp->next) {
      if (force) { _mi_arenas_try_purge(true /* force */, true /* visit_all */, sp, 0 /* tseq */); }
      else       { _mi_arenas_purge_now(sp); }
    }
  }
}

// Phase B: the abandoned pages of every heap of every subproc. Those have no owning thread and
// are claimed through the arena ownership protocol, so any thread may sweep them; `my_tld` is
// the sweeping thread's tld (the hole bookkeeping -- `holes_sweeping`, the per-pass counters --
// lives on it). A heap that is being deleted (`releasing`) is abandoning its pages unmapped
// right now and is skipped, as `_mi_subproc_prof_sync_force_slow` does.
static void mi_purge_all_abandoned(mi_tld_t* my_tld) {
  mi_lock(_mi_subprocs_lock()) {
    for (mi_subproc_t* sp = _mi_subprocs_head(); sp != NULL; sp = sp->next) {
      mi_lock(&sp->heaps_lock) {
        for (mi_heap_t* heap = sp->heaps; heap != NULL; heap = heap->next) {
          if (mi_atomic_load_acquire(&heap->releasing) != 0) continue;
          _mi_arenas_purge_abandoned_holes(heap, my_tld);
        }
      }
    }
  }
}

/* -----------------------------------------------------------
  Phase D: the walk (§7.1)

  Rules the loop encodes:
   - No pointer to an unclaimed tld leaves `tlds_lock`. A tld is only dereferenced outside the
     lock once this thread holds its SWEEPING claim, which is what keeps it alive: every path
     out of a park (`_mi_park_leave`, `_mi_park_leave_gate`, thread teardown, fork prepare)
     waits for SWEEPING to clear before the tld can be freed.
   - Registry cutoff: a tld with `purge_epoch == seq` is done, given up, or was registered
     after the walk began (`mi_tld_register` stamps `_mi_purge_seq`).
   - Bounded waiting: `wait_ms` bounds owner ACQUISITION only -- never a claimed sweep, never
     its madvise calls. A RUNNING owner is left for a later pass; at the deadline everything
     still unstamped is stamped and counted pending.
   - Orphans (MI_GATE_FLAG_ORPHAN: the pre-fork tld of a thread that did not survive the fork)
     are stamped and counted, never parked, never swept (§8).
----------------------------------------------------------- */

typedef struct mi_purge_walk_s {
  size_t seq;
  bool   force;
  size_t swept;
  size_t pending;
  size_t orphaned;
} mi_purge_walk_t;

// One pass over `sp->tlds` under `tlds_lock`: stamps what it can, claims at most one tld and
// returns it (SWEEPING, `sweeper == me`), or NULL. `*unstamped` reports whether any tld was
// left for a later pass (a RUNNING owner).
static mi_tld_t* mi_purge_walk_claim(mi_subproc_t* sp, mi_tld_t* my_tld, mi_purge_walk_t* w, bool* unstamped) {
  mi_tld_t* claimed = NULL;
  const uintptr_t me = (uintptr_t)_mi_thread_id();
  mi_tld_t* const scav_tld = _mi_scavenger_tld_ptr();   // NULL: the scavenger has no tld (every non-DLL build), or none runs
  *unstamped = false;
  mi_lock(&sp->tlds_lock) {
    for (mi_tld_t* tld = sp->tlds; tld != NULL; tld = tld->subproc_next) {
      if (mi_atomic_load_relaxed(&tld->purge_epoch) == w->seq) continue;   // done, given up, or born after `seq`
      if (tld == my_tld) {                                                 // phase C did it
        mi_atomic_store_relaxed(&tld->purge_epoch, w->seq);
        continue;
      }
      if (scav_tld != NULL && tld == scav_tld) {                           // the scavenger's own tld (Windows DLL build):
        mi_atomic_store_relaxed(&tld->purge_epoch, w->seq);                // it owns nothing and never parks -- neither swept nor pending
        continue;
      }
      // #366: a thread inside `_mi_thread_done` has no live owner to wait for and owns nothing a
      // purge could return -- its pages are abandoned by the teardown itself and are reached by
      // phase B afterwards. Neither swept nor reported: reporting it `pending` would make
      // `complete` unreachable for the rest of the process if that teardown never finishes
      // (Windows: a joined thread's TLS-callback teardown can outlive the join).
      if ((mi_atomic_load_relaxed(&tld->gate_flags) & MI_GATE_FLAG_EXITING) != 0) {
        mi_atomic_store_relaxed(&tld->purge_epoch, w->seq);
        continue;
      }
      if ((mi_atomic_load_relaxed(&tld->gate_flags) & MI_GATE_FLAG_ORPHAN) != 0) {
        mi_atomic_store_relaxed(&tld->purge_epoch, w->seq);
        w->orphaned++;
        continue;
      }
      mi_purge_park_state_t expected = MI_PARK_PARKED;
      if (mi_atomic_cas_strong_acq_rel(&tld->park_state, &expected, MI_PARK_SWEEPING)) {
        // the claim names its holder: that is what authorises the foreign door
        // (`_mi_thread_idle_work_ex` asserts it) and what `_mi_gate_held` checks
        mi_atomic_store_release(&tld->sweeper, me);
        if (w->force) { mi_atomic_or_acq_rel(&tld->gate_flags, (size_t)MI_GATE_FLAG_RECLAIM_IGNORED); }
        mi_atomic_store_relaxed(&tld->purge_epoch, w->seq);
        claimed = tld;
        break;
      }
      // RUNNING (or SWEEPING under the scavenger): leave it for a later pass; no pointer kept
      *unstamped = true;
    }
  }
  return claimed;
}

// Give up on everything still unstamped in `sp` (deadline, or an ungated build's first pass).
static void mi_purge_walk_stamp_rest(mi_subproc_t* sp, mi_purge_walk_t* w) {
  mi_lock(&sp->tlds_lock) {
    for (mi_tld_t* tld = sp->tlds; tld != NULL; tld = tld->subproc_next) {
      if (mi_atomic_load_relaxed(&tld->purge_epoch) == w->seq) continue;
      mi_atomic_store_relaxed(&tld->purge_epoch, w->seq);
      w->pending++;
    }
  }
}

// Sweep a tld this thread holds the claim on, then hand it back PARKED (never RUNNING: the
// owner still owns the transition out, and may be spinning in `_mi_park_leave_gate` for it).
static void mi_purge_walk_sweep(mi_tld_t* claimed, mi_purge_walk_t* w) {
  mi_assert_internal(mi_atomic_load_acquire(&claimed->park_state) == MI_PARK_SWEEPING);
  mi_assert_internal(mi_atomic_load_acquire(&claimed->sweeper) == (uintptr_t)_mi_thread_id());
  // `park_theap0` is set by `mi_on_thread_idle_start` (a default-build park); a gated park
  // leaves it NULL and the sweep takes the tld's main-heap theap instead (under its lock).
  mi_theap_t* theap0 = claimed->park_theap0;
  if (theap0 == NULL) {
    mi_heap_t* const heap_main = (claimed->subproc != NULL ? mi_atomic_load_ptr_acquire(mi_heap_t, &claimed->subproc->heap_main) : NULL);
    mi_lock(&claimed->theaps_lock) {
      for (mi_theap_t* theap = claimed->theaps; theap != NULL; theap = theap->tnext) {
        if (theap0 == NULL) { theap0 = theap; }
        if (heap_main != NULL && _mi_theap_heap_peek(theap) == heap_main) { theap0 = theap; break; }
      }
    }
  }
  _mi_thread_idle_work_ex(claimed, theap0, w->force);
  w->swept++;
  // release: flags first, then the holder, then the state (a `_mi_gate_held` that reads PARKED
  // never consults `sweeper`; a later claimant overwrites both under its own CAS)
  mi_atomic_and_acq_rel(&claimed->gate_flags, ~(size_t)MI_GATE_FLAG_RECLAIM_IGNORED);
  mi_atomic_store_release(&claimed->sweeper, (uintptr_t)0);
  mi_atomic_store_release(&claimed->park_state, (size_t)MI_PARK_PARKED);
}

static void mi_purge_walk_subproc(mi_subproc_t* sp, mi_tld_t* my_tld, mi_purge_walk_t* w, mi_msecs_t deadline) {
  size_t spin = 0;
  for (;;) {
    bool unstamped = false;
    mi_tld_t* const claimed = mi_purge_walk_claim(sp, my_tld, w, &unstamped);
    if (claimed != NULL) {
      mi_purge_walk_sweep(claimed, w);
      spin = 0;   // progress: the pause ramp restarts
      continue;
    }
    if (!unstamped) break;   // complete for this subproc
    #if !MI_OWNER_GATE
    // Default build: a RUNNING tld is a thread that is not inside `mi_on_thread_idle_start`,
    // and nothing will ever park it for us -- waiting would only burn `wait_ms`. Report it
    // pending right away (§7.1, last rule).
    MI_UNUSED(deadline); MI_UNUSED(spin);
    mi_purge_walk_stamp_rest(sp, w);
    break;
    #else
    if (_mi_clock_now() >= deadline) {
      mi_purge_walk_stamp_rest(sp, w);
      break;
    }
    // let RUNNING owners leave the allocator: spin briefly (no syscall), and only if they are
    // still inside after that -- likely descheduled -- yield the CPU to them
    if (spin < 256) { mi_atomic_pause(); spin++; }
    else { _mi_prim_thread_yield(); }
    #endif
  }
}

// The walk over every subproc. `mi_subprocs_lock` (fork level 1) is held for the WHOLE walk --
// across the claims, the sweeps and the acquisition waits -- because a subproc must stay alive
// while its `tlds` list is walked and there is no other pin on it. That is a documented cost
// (§8, §13): a concurrent `fork()` (`prepare` takes level 1 first), `mi_subproc_new/delete` or
// `mi_prof_start`'s sync waits for this call, bounded by `wait_ms` plus the claimed sweeps.
// Nothing an owner does INSIDE an allocator call takes `mi_subprocs_lock`, so an owner we are
// waiting on is never waiting on us. A claim is taken under `sp->tlds_lock` (level 4) and
// released before the sweep; the sweep itself takes only the swept tld's locks and the
// arena/OS layers -- none of which nest `mi_subprocs_lock`. (`_mi_subproc_prof_sync_force_slow` nests `heaps_lock` and
// `theaps_lock` under it the same way; `tlds_lock` is a sibling of `heaps_lock` there.)
static void mi_purge_all_walk(mi_tld_t* my_tld, mi_purge_walk_t* w, mi_msecs_t deadline) {
  mi_lock(_mi_subprocs_lock()) {
    for (mi_subproc_t* sp = _mi_subprocs_head(); sp != NULL; sp = sp->next) {
      mi_purge_walk_subproc(sp, my_tld, w, deadline);
    }
  }
}

/* -----------------------------------------------------------
  The driver (§7)
----------------------------------------------------------- */

int mi_purge_all_ex(mi_purge_flags_t flags, size_t wait_ms, mi_purge_all_report_t* report) mi_attr_noexcept {
  const bool force = ((flags & MI_PURGE_FORCE) != 0);
  if (report != NULL) { _mi_memzero(report, sizeof(*report)); }

  // Admission BEFORE any work: a loser (another purge in flight) or a re-entrant call (our own
  // id is already the holder) does nothing and says so. Nothing below may return without
  // passing the single release at the end.
  const uintptr_t me = (uintptr_t)_mi_thread_id();
  uintptr_t expected_holder = 0;
  if (!mi_atomic_cas_strong_acq_rel(&_mi_purge_admission, &expected_holder, me)) {
    if (report != NULL) { report->gated = (MI_OWNER_GATE != 0); }
    return MI_PURGE_BUSY;
  }
  // Only now: an incremented epoch with no admission would prematurely stamp new tlds "done"
  // for a walk that never runs.
  const size_t seq = mi_atomic_increment_acq_rel(&_mi_purge_seq) + 1;

  // The caller's own theap: a thread that never allocated has none yet, and the walk needs
  // our tld to skip it (and phase C needs it to sweep). `mi_theap_get_default` initialises
  // it (an allocator call: in a gated build that registers us RUNNING and parks us on return).
  mi_theap_t* my_theap = mi_theap_get_default();
  mi_tld_t* const my_tld = my_theap->tld;
  mi_assert_internal(my_tld != NULL && my_tld->thread_id == _mi_thread_id());

  mi_purge_snapshot_t before, after;
  mi_purge_snapshot(&before);

  // A. arenas: everything due (or, forced, everything purgeable) goes back to the OS first
  mi_purge_all_arenas(force);

  // B. abandoned pages of every heap of every subproc
  mi_purge_all_abandoned(my_tld);

  // C. our own tld, through the OWNER door: `mi_theap_collect` is a public (gated) entry, and
  //    the hole sweep is not, so both run under one enter / one leave of our own gate -- in a
  //    gated build we are PARKED between allocator calls and the scavenger could otherwise be
  //    sweeping these very free lists. (Ungated: the macros expand to nothing; we are RUNNING.)
  MI_GATE_ENTER(my_theap);
  mi_theap_collect(my_theap, force);
  _mi_purge_holes_of(my_tld, force);
  MI_GATE_LEAVE(my_tld);

  // D. everyone else
  mi_purge_walk_t walk; walk.seq = seq; walk.force = force; walk.swept = 1 /* us, phase C */; walk.pending = 0; walk.orphaned = 0;
  const mi_msecs_t deadline = _mi_clock_now() + (mi_msecs_t)wait_ms;
  mi_purge_all_walk(my_tld, &walk, deadline);

  // E. one final arena pass so the pages the sweeps freed leave now, not at the next purge_delay
  mi_purge_all_arenas(force);

  mi_purge_snapshot(&after);
  if (report != NULL) {
    const size_t hole_delta   = (after.hole_total >= before.hole_total ? after.hole_total - before.hole_total : 0);
    const int64_t purged_delta = after.purged_total - before.purged_total;
    const size_t purged_bytes = (purged_delta > 0 ? (size_t)purged_delta : 0);
    report->arena_bytes     = (purged_bytes > hole_delta ? purged_bytes - hole_delta : 0);
    report->hole_bytes      = hole_delta;
    report->theaps_swept    = walk.swept;
    report->theaps_pending  = walk.pending;
    report->theaps_orphaned = walk.orphaned;
    report->gated           = (MI_OWNER_GATE != 0);
    report->complete        = (walk.pending == 0 && walk.orphaned == 0);
  }
  const int status = (walk.pending == 0 ? MI_PURGE_OK : MI_PURGE_PARTIAL);

  // the single exit path
  mi_atomic_store_release(&_mi_purge_admission, (uintptr_t)0);
  return status;
}

// #366 test observable (test/test-purge-all.cpp declares it, like `mi_idle_work_count`; it is
// not part of the public header): describe every registered tld of the main
// subproc that is NOT parked -- what a `mi_purge_all` would report pending. Writes one line per
// tld into `buf`; returns the count. Reads plain owner-private words (`gate_depth`) without
// ownership, for diagnostics only: the values are a hint, never a decision.
#ifdef __cplusplus
extern "C"   // the native MSVC gate compiles the library as C++; the test references the C name
#endif
mi_decl_export size_t mi_purge_debug_unparked(char* buf, size_t buf_size) mi_attr_noexcept {
  size_t n = 0, used = 0;
  if (buf != NULL && buf_size > 0) { buf[0] = 0; }
  mi_subproc_t* const sp = _mi_subproc_main();
  if (sp == NULL) return 0;
  const mi_threadid_t me = _mi_thread_id();
  mi_lock(&sp->tlds_lock) {
    for (mi_tld_t* tld = sp->tlds; tld != NULL; tld = tld->subproc_next) {
      const size_t st = mi_atomic_load_acquire(&tld->park_state);
      if (st == MI_PARK_PARKED) continue;
      n++;
      if (buf != NULL && used + 1 < buf_size) {
        char line[192];
        _mi_snprintf(line, sizeof(line), "  tld %p: thread %zx%s state %s depth %zu epoch %zu flags %zx sweeper %zx theaps %s seq %zu\n",
                     (void*)tld, (size_t)tld->thread_id, (tld->thread_id == me ? " (caller)" : ""),
                     (st == MI_PARK_RUNNING ? "RUNNING" : st == MI_PARK_SWEEPING ? "SWEEPING" : "?"),
                     tld->gate_depth, mi_atomic_load_relaxed(&tld->purge_epoch), mi_atomic_load_relaxed(&tld->gate_flags),
                     (size_t)mi_atomic_load_relaxed(&tld->sweeper), (tld->theaps != NULL ? "yes" : "none"), tld->thread_seq);
        size_t len = 0; while (line[len] != 0) len++;
        if (used + len < buf_size) { for (size_t i = 0; i < len; i++) buf[used + i] = line[i]; used += len; buf[used] = 0; }
      }
    }
  }
  return n;
}

void mi_purge_all(bool force) mi_attr_noexcept {
  (void)mi_purge_all_ex((force ? MI_PURGE_FORCE : (mi_purge_flags_t)0), 100, NULL);
}
