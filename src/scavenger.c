/* ----------------------------------------------------------------------------
Copyright (c) 2025, Microsoft Research, Daan Leijen
This is free software; you can redistribute it and/or modify it under the
terms of the MIT license. A copy of the license can be found in the file
"LICENSE" at the root of this distribution.
-----------------------------------------------------------------------------*/

// imported from oven-sh/mimalloc @ 942b8342, MIT (issue #272 / Bun parity P7a).
//
// Demand-driven background scavenger. Waits on subproc->scavenger_wake (set by
// mi_arena_schedule_purge) and runs _mi_arenas_try_purge when due, so freed
// arena memory returns to the OS without waiting for the next allocation.
//
// This file also holds the idle-handoff protocol (`mi_on_thread_idle*`, `_mi_park_leave`,
// `_mi_theap_sweep_parked`, `_mi_thread_idle_work`) and, since #366, the owner-gate acquire
// (`_mi_park_leave_gate`) and the foreign door (`_mi_thread_idle_work_ex`). Bun keeps those in `src/theap.c`;
// this fork keeps `src/theap.c` an upstream file with only a few guarded lines in it
// (CLAUDE.md rule 6), and none of them need theap file statics.
//
// DEVIATION from Bun: the second phase of `_mi_thread_idle_work` -- discarding the free
// blocks inside still-used pages (`mi_option_purge_holes`) -- lives in `src/page-holes.c`
// (CLAUDE.md rule 6), and is called from here as `_mi_purge_holes_of`. With Phase 7b landed,
// `mi_on_thread_idle` delivers all three clauses of its documented contract.

#include "mimalloc.h"
#include "mimalloc/internal.h"
#include "mimalloc/prim.h"      // _mi_prim_thread_yield
#include "mimalloc/prim-tls.h"  // _mi_theap_default

// #272 (blocker, MSVC-C): the MSVC **C** atomics wrapper in `mimalloc/atomic.h` -- taken by `cl`
// without `/std:c11 /experimental:c11atomics`, which is how `rust/mimalloc-pprof/build.rs`
// compiles the amalgamation for `x86_64-pc-windows-msvc` -- implements the generic
// `mi_atomic_load/store/exchange/cas` ONLY at `uintptr_t` width. A narrower field reached
// through them is read and written a full word at a time, past its end. These four are the
// fields this phase added; a build that narrows one again fails here rather than at runtime.
//
// The FIELDS are only half of it: the out-param of a `park_state` CAS is written through the
// same `uintptr_t*` parameter, so a narrower local is a 4-byte stack slot written 8 bytes wide.
// gcc/clang reject that outright through the C11 generic ("size mismatch in argument 2 of
// `__atomic_compare_exchange`"), but the MSVC-C wrapper only warns (C4133, an incompatible
// pointer it then casts) -- exactly the compiler this assert exists for. So every such local is
// declared through `mi_park_state_t`, which the assert below pins to the field.
typedef size_t mi_park_state_t;

typedef char mi_scav_atomic_widths_assert_t[
  (sizeof(mi_park_state_t)                 == sizeof(((mi_tld_t*)0)->park_state) &&
   sizeof(((mi_tld_t*)0)->park_state)      == sizeof(uintptr_t) &&
   sizeof(((mi_tld_t*)0)->park_reclaim)    == sizeof(uintptr_t) &&
   sizeof(((mi_tld_t*)0)->park_swept)      == sizeof(uintptr_t) &&
   sizeof(((mi_tld_t*)0)->sweeper)         == sizeof(uintptr_t) &&   // #366
   sizeof(((mi_tld_t*)0)->purge_epoch)     == sizeof(uintptr_t) &&
   sizeof(((mi_tld_t*)0)->gate_flags)      == sizeof(uintptr_t) &&
   sizeof(((mi_subproc_t*)0)->parked_count)== sizeof(uintptr_t)
   #if defined(_WIN32)
   && sizeof(((mi_subproc_t*)0)->scavenger_wake) == sizeof(uintptr_t)
   #endif
  ) ? 1 : -1];

/* -----------------------------------------------------------
  The idle handoff protocol.

  `park_state` on the tld is the whole protocol:

    RUNNING  -- only the owner may touch its theaps (the normal state)
    PARKED   -- the owner published "I will not allocate or free until I say otherwise"
    SWEEPING -- the scavenger claimed a PARKED tld and is doing its idle work right now

  Only the owner takes a tld out of RUNNING; only the scavenger takes it PARKED -> SWEEPING
  and back. SWEEPING is what keeps the tld alive across a sweep without holding
  `subproc->tlds_lock`: every path out of a park (`mi_on_thread_idle_end`, and thread
  teardown / fork-prepare via `_mi_park_leave`) waits for SWEEPING to clear first.
----------------------------------------------------------- */

// #272 test hook: how many idle-work passes have completed in this process, on the owner
// (`mi_on_thread_idle`) or on the scavenger for a parked thread. Not in `mimalloc.h` -- it is an
// internal observable for `test/test-park-handoff.c`, which otherwise has no way to tell "the
// handoff swept" from "the handoff silently did nothing". Unconditional (not `MI_DEBUG`-only, as
// the fork test hooks in `src/fork.c` are) so the test is meaningful in a Release build too; one
// relaxed increment per park costs nothing, and nothing on the alloc/free path reads it.
// Phase 7b replaces the *content* of this observable with `mi_purge_holes_stats_get().discard_calls`
// in the test; the counter stays as the "a pass ran at all" signal.
static _Atomic(size_t) mi_idle_work_count;

mi_decl_externc mi_decl_export size_t _mi_test_idle_work_count(void) {
  return mi_atomic_load_relaxed(&mi_idle_work_count);
}

// #366: pick the theap the sweep collects first for a claimed tld. The scavenger has no TLS of
// the owner's to find its default theap with; `mi_on_thread_idle_start` leaves it in
// `park_theap0`, but in a gated build the owner is PARKED by the gate, not by `_start`, and
// the field is NULL. Fall back to the tld's theap of the subproc's main heap (the one
// `mi_thread_init` creates and `mi_heap_delete` refuses to delete, so it outlives the claim),
// or, failing that, the head of the list. Taken under `tld->theaps_lock` because another
// thread's `mi_heap_delete` may be unlinking theaps from it right now.
static mi_theap_t* mi_tld_sweep_theap0(mi_tld_t* tld) {
  mi_theap_t* theap0 = tld->park_theap0;
  if (theap0 != NULL) return theap0;
  mi_heap_t* const heap_main = (tld->subproc != NULL ? mi_atomic_load_ptr_acquire(mi_heap_t, &tld->subproc->heap_main) : NULL);
  mi_lock(&tld->theaps_lock) {
    for (mi_theap_t* theap = tld->theaps; theap != NULL; theap = theap->tnext) {
      if (theap0 == NULL) { theap0 = theap; }
      if (heap_main != NULL && _mi_theap_heap_peek(theap) == heap_main) { theap0 = theap; break; }
    }
  }
  return theap0;
}

// #366: the "foreign door" into the collect body (docs/purge-all-implementation.md §6): the
// sweep of `tld`'s theaps without the trailing arena purge (the driver purges arenas once,
// process-wide). Runs on the owner (`mi_on_thread_idle`) or on whichever thread holds `tld`'s
// MI_PARK_SWEEPING claim -- the scavenger for a parked thread, or `mi_purge_all` for a gated
// one; both require that the owner is not allocating while we rewrite its free lists, and the
// claim is what authorises it (asserted below through `tld->sweeper`). Never touches
// `gate_depth`: that is the owner's, and the owner may be spinning in `_mi_park_leave_gate`
// for the whole of this with its depth already incremented.
//
// `force` reaches both per-page loops: `MI_FORCE` for the collect, and for the hole sweep it
// skips the `purge_holes_min_interval` pacing and (MI_GATE_FLAG_RECLAIM_IGNORED, set by the
// claimant) lets the sweep run to completion instead of stopping at the owner's reclaim.
void _mi_thread_idle_work_ex(mi_tld_t* tld, mi_theap_t* theap0, bool force) {
  if (tld == NULL) return;
  const bool foreign = (tld->thread_id != _mi_thread_id());
  #if MI_DEBUG
  // the claim authorises the foreign door: nobody else may be here for a tld that is not its own
  mi_assert_internal(!foreign || (mi_atomic_load_acquire(&tld->park_state) == MI_PARK_SWEEPING &&
                                  mi_atomic_load_acquire(&tld->sweeper) == (uintptr_t)_mi_thread_id()));
  // #272 profiler-interaction invariant (3): the swept thread's own sampling countdown is its
  // own. Nothing in the sweep may advance or reset it -- the profiler decides when to sample
  // from the OWNER's allocation stream, and a sweep that touched these would make a thread's
  // next sample depend on how often a sweeper happened to visit it. Here, and not in
  // `_mi_theap_sweep_parked`, so it covers the `mi_purge_all` caller as well (#366).
  const mi_profiler_tld_t prof_before = tld->profiler;
  #endif
  // each phase is a full walk: an owner waiting in `_mi_park_leave` cannot allocate until we stop
  // (unless the claimant asked for completion, see `mi_tld_reclaim_requested`)
  const bool reclaim_ignored = ((mi_atomic_load_relaxed(&tld->gate_flags) & MI_GATE_FLAG_RECLAIM_IGNORED) != 0);
  if (!reclaim_ignored && mi_atomic_load_relaxed(&tld->park_reclaim) != 0) return;
  if (theap0 != NULL && mi_theap_is_initialized(theap0)) {
    if (foreign) { _mi_theap_collect_foreign(theap0, force); }
    else         { mi_theap_collect(theap0, force); }   // the owner door (gated build: ordinary recursion)
  }
  if (!reclaim_ignored && mi_atomic_load_relaxed(&tld->park_reclaim) != 0) return;
  _mi_purge_holes_of(tld, force);   // #272 (P7b): every theap of this thread + its heaps' abandoned pages
  #if MI_DEBUG
  mi_assert_internal(tld->profiler.bytes_since_sample == prof_before.bytes_since_sample &&
                     tld->profiler.next_threshold    == prof_before.next_threshold &&
                     tld->profiler.random            == prof_before.random &&
                     tld->profiler.generation        == prof_before.generation);
  #endif
}

// Fold in pending frees and drain the arena purge queue. Runs on the owner
// (`mi_on_thread_idle`) or on the scavenger for a parked thread; both require that the owner
// of `tld` is not allocating while we rewrite its free lists.
void _mi_thread_idle_work(mi_tld_t* tld, mi_theap_t* theap0) {
  if (tld == NULL) return;
  _mi_thread_idle_work_ex(tld, theap0, false /* not forced */);
  if (mi_atomic_load_relaxed(&tld->park_reclaim) != 0) return;
  _mi_arenas_purge_now(tld->subproc);
  mi_atomic_increment_relaxed(&mi_idle_work_count);   // #272 test observable, see above
}

// The common loop of `_mi_park_leave` and `_mi_park_leave_gate` (#366): take `tld` from PARKED
// to RUNNING, waiting out a sweeper. Returns false when the tld was already RUNNING (nothing to
// take back: an initial RUNNING is "mine" -- the park protocol's default, and in a gated build
// the state a tld is registered in).
static bool mi_park_leave_loop(mi_tld_t* tld) {
  for (;;) {
    mi_park_state_t expected = MI_PARK_PARKED;   // width-pinned to the field, see the assert above
    if (mi_atomic_cas_strong_acq_rel(&tld->park_state, &expected, MI_PARK_RUNNING)) break;
    if (expected == MI_PARK_RUNNING) return false;   // not parked: nothing to take back
    // it may re-claim the moment it releases, so re-race rather than store: only a CAS from
    // PARKED may reach RUNNING
    mi_assert_internal(expected == MI_PARK_SWEEPING);
    mi_atomic_store_release(&tld->park_reclaim, 1);
    // it stops at its next page or phase: spin briefly (no syscall), and only if it is still
    // sweeping after that -- likely descheduled -- yield the CPU to it
    size_t spin = 0;
    while (mi_atomic_load_acquire(&tld->park_state) == MI_PARK_SWEEPING) {
      if (spin < 256) { mi_atomic_pause(); spin++; }
      else { _mi_prim_thread_yield(); }
    }
  }
  mi_atomic_store_release(&tld->park_reclaim, 0);
  return true;
}

// Take the theaps of `tld` back from the scavenger. Also called from teardown and from
// `_mi_process_fork_prepare`: a thread can leave a park without reaching
// `mi_on_thread_idle_end` (`epoll_wait` is a cancellation point), and freeing the tld while
// the sweeper walks it is a use-after-free.
void _mi_park_leave(mi_tld_t* tld) {
  if (tld == NULL) return;
  if (!mi_park_leave_loop(tld)) return;
  mi_atomic_decrement_relaxed(&tld->subproc->parked_count);
}

// #366: the owner-gate acquire (`_mi_gate_enter`, mimalloc/owner-gate.h): `_mi_park_leave`
// without the `parked_count` decrement -- a gated park is not a `mi_on_thread_idle_start` park
// and was never counted. The matching release is a plain release-store of PARKED in
// `_mi_gate_leave`. Defined in both builds (the header only calls it when MI_OWNER_GATE).
void _mi_park_leave_gate(mi_tld_t* tld) {
  if (tld == NULL) return;
  (void)mi_park_leave_loop(tld);
}

// Sweep the theaps of every parked thread of `subproc`; scavenger only.
//
// Returns in how many msecs a park that was passed over for `purge_holes_min_interval` becomes
// due (0: none was), so the scavenger can wake for it instead of leaving it to its safety timeout.
//
// #366 (gated build): every thread outside the allocator is PARKED, so this is the timed sweep
// of BUSY threads too, paced by `purge_holes_min_interval` (`holes_sweep_last`) and bounded per
// visit by `park_reclaim` (the owner's next allocator call). `parked_count` and `park_swept`
// are `mi_on_thread_idle_start` bookkeeping that a gated park never writes, so `parked_count`
// is not consulted there (a gated tld would be skipped forever), and `park_swept` -- which
// `_start` alone clears -- is used as a per-CALL round stamp instead: each tld is swept at
// most once per call, so a call always terminates and the scavenger gets back to its wait
// (and its stop flag) even with `purge_holes_min_interval=0`, when every PARKED tld is
// always due. Ungated, `park_swept` keeps its 0/1 "done for this park" meaning.
#if MI_OWNER_GATE
static size_t mi_sweep_round;   // scavenger thread only (the sole caller of `_mi_theap_sweep_parked`)
#endif
mi_msecs_t _mi_theap_sweep_parked(mi_subproc_t* subproc) {
  if (subproc == NULL) return 0;
  #if !MI_OWNER_GATE
  if (mi_atomic_load_relaxed(&subproc->parked_count) == 0) return 0;
  const size_t swept_mark = 1;
  #else
  if (++mi_sweep_round == 0) { mi_sweep_round = 1; }   // 0 is "never swept" (`_start` clears to it)
  const size_t swept_mark = mi_sweep_round;
  #endif
  for (;;) {
    mi_tld_t* claimed = NULL;
    mi_msecs_t due_in = 0;
    mi_lock(&subproc->tlds_lock) {
      // imported from oven-sh/mimalloc @ 942b8342, MIT (issue #272 / Bun parity P7b):
      // `purge_holes_min_interval` pacing -- a thread that parks in a tight loop must not have
      // its heaps swept on every park (the sweep is a full walk of its pages).
      const mi_msecs_t now = _mi_clock_now();
      const mi_msecs_t interval = (mi_msecs_t)mi_option_get_clamp(mi_option_purge_holes_min_interval, 0, 3600000);
      for (mi_tld_t* tld = subproc->tlds; tld != NULL; tld = tld->subproc_next) {
        #if !MI_OWNER_GATE
        if (mi_atomic_load_acquire(&tld->park_swept) != 0) continue;   // already done for this park
        #else
        if (mi_atomic_load_acquire(&tld->park_swept) == swept_mark) continue;   // already swept this round (#366)
        #endif
        // Read `park_state` BEFORE `holes_sweep_last`. That field is a PLAIN `mi_msecs_t` written
        // by whoever last swept this tld -- including the OWNER while it is RUNNING, from its own
        // `mi_on_thread_idle()` (see `_mi_purge_holes_of`). What makes reading it here race-free
        // is not this lock (the owner takes no lock to write it) but the acquire below pairing
        // with the owner's release-store of MI_PARK_PARKED in `mi_on_thread_idle_start` (or in
        // `_mi_gate_leave`, #366): a tld that reads PARKED has published every write it made
        // before parking, and it cannot write the field again until it leaves the park. A tld
        // that is not parked is skipped without the field ever being read.
        if (mi_atomic_load_acquire(&tld->park_state) != MI_PARK_PARKED) continue;
        if (interval > 0 && tld->holes_sweep_last != 0 && now - tld->holes_sweep_last < interval) {
          const mi_msecs_t due = interval - (now - tld->holes_sweep_last);
          if (due_in == 0 || due < due_in) { due_in = due; }
          continue;
        }
        mi_park_state_t expected = MI_PARK_PARKED;   // width-pinned to the field, see the assert above
        if (mi_atomic_cas_strong_acq_rel(&tld->park_state, &expected, MI_PARK_SWEEPING)) {
          // #366: the claim names its holder; that is what authorises the foreign door
          // (`_mi_thread_idle_work_ex` asserts it) and what `_mi_gate_held` checks for the
          // sweeper's own accesses in a gated debug build.
          mi_atomic_store_release(&tld->sweeper, (uintptr_t)_mi_thread_id());
          claimed = tld; break;
        }
      }
    }
    if (claimed == NULL) return due_in;   // nothing parked (any more) that is due yet
    mi_theap_t* const theap0 = mi_tld_sweep_theap0(claimed);   // #366: NULL `park_theap0` in a gated build
    // NOTE: `holes_sweep_last` is stamped by `_mi_purge_holes_of` itself, not here -- it is the
    // one path both the scavenger and a direct `mi_on_thread_idle()` go through, so the owner's
    // own sweeps are paced by, and visible to, the same clock. Stamping here as well would make
    // the interval check inside it see `now - last == 0` and skip every scavenger-driven sweep.
    // (The #272 profiler-invariant asserts around the sweep live in `_mi_thread_idle_work_ex`.)
    _mi_thread_idle_work(claimed, theap0);
    // #272 profiler-interaction invariant (3): the sweep must leave this thread without a theap
    // of its own. If any hook or stat on the sweep path forced `_mi_theap_default()` into
    // existence here, the scavenger would (a) acquire this fork's per-thread profiler/hook
    // state (`mi_tld_t.profiler` / `.hooks`) and mutate it from a thread the user never sees,
    // and (b) register itself in `subproc->tlds` as a parkable thread. Everything it runs is
    // either a pure atomic/OS operation or takes the swept `tld` explicitly, and every hook
    // accessor peeks (`_mi_hooks_tld_peek` returns NULL, `_peek_or_local` falls back to the
    // caller's stack) rather than forcing -- this asserts it stays that way.
    mi_assert_internal(!mi_theap_is_initialized(_mi_theap_default()));
    // Mark BEFORE releasing: a `park_swept` set after the store could land on the thread's *next*
    // park and silently skip that sweep. Cleared by `mi_on_thread_idle_start`. If we bailed out
    // early on `park_reclaim`, the owner is leaving the park anyway, so the rest is its next park's.
    // (#366 gated build: the round stamp -- see the function comment.)
    mi_atomic_store_release(&claimed->park_swept, swept_mark);
    // #366: drop the claim's holder BEFORE the state goes back: a `_mi_gate_held` that reads
    // PARKED never consults `sweeper`, and a later claimant overwrites it under its own CAS.
    mi_atomic_store_release(&claimed->sweeper, (uintptr_t)0);
    // Back to PARKED, not RUNNING: the owner is still blocked and still owns the transition out.
    mi_atomic_store_release(&claimed->park_state, MI_PARK_PARKED);
  }
}


#if defined(__wasi__) || (defined(__EMSCRIPTEN__) && !defined(__EMSCRIPTEN_PTHREADS__))

// No scavenger thread on these platforms; purging stays allocation-driven.
void _mi_scavenger_start(void) { }
void _mi_scavenger_stop(void)  { }
void _mi_scavenger_wake(mi_subproc_t* subproc) { MI_UNUSED(subproc); }
bool _mi_scavenger_is_running(void) { return false; }
void _mi_scavenger_forked_child(void) { }
void _mi_scavenger_start_lazy(void) { _mi_scavenger_start(); }

#else

#include <errno.h>

static _Atomic(uintptr_t) _mi_scavenger_running;  // 0 = not running, 1 = running
// Set by `_mi_scavenger_stop` and never cleared: teardown has begun, so no start may create a
// thread any more. Without it `_mi_scavenger_start_lazy` -- reachable from a thread that parks
// while the process is tearing down -- can spawn a scavenger AFTER the stop that was supposed
// to join it, leaving a thread walking a subproc that is being dismantled.
static _Atomic(uintptr_t) _mi_scavenger_shutdown;

// -----------------------------------------------------------------------------
// Wait/wake on subproc->scavenger_wake (a `mi_scav_word_t` futex/wait word).
//
//   mi_scav_wait(addr, timeout_ms) : block while *addr == 0, up to timeout_ms.
//   mi_scav_wake_one(addr)         : wake one waiter on addr.
//
// The thread loop re-reads scavenger_wake and purge_expire after every return,
// so spurious wakeups are fine; EINTR is retried in-place so signals do not
// turn the wait into a busy spin.
// -----------------------------------------------------------------------------

#if defined(__linux__)

#include <sys/syscall.h>
#include <unistd.h>

// DEVIATION from Bun, which includes <linux/futex.h>. That is a KERNEL uapi header: it
// ships in glibc distros' base development packages but NOT in Alpine's `build-base`
// (it needs the separate `linux-headers` package), so including it made the musl c-unit
// job fail to compile. CLAUDE.md rule 5 forbids adding a required build dependency, so
// define the two constants instead. They are fixed,permanently stable kernel ABI values
// (uapi/linux/futex.h: FUTEX_WAIT = 0, FUTEX_WAKE = 1, FUTEX_PRIVATE_FLAG = 128) --
// the same thing every libc's own futex wrapper hardcodes.
#define MI_FUTEX_WAIT_PRIVATE  (0 | 128)
#define MI_FUTEX_WAKE_PRIVATE  (1 | 128)

static void mi_scav_wait(_Atomic(mi_scav_word_t)* addr, mi_msecs_t timeout_ms) {
  if (timeout_ms <= 0) timeout_ms = 1;
  struct timespec ts;
  ts.tv_sec  = (time_t)(timeout_ms / 1000);
  ts.tv_nsec = (long)((timeout_ms % 1000) * 1000000L);
  while (mi_atomic_load_acquire(addr) == 0) {
    const long rc = syscall(SYS_futex, (uint32_t*)addr,   /* mi_scav_word_t is uint32_t here: futex is 32-bit only */ MI_FUTEX_WAIT_PRIVATE, (uint32_t)0, &ts, NULL, 0);
    if (rc == 0) return;                 // woken by FUTEX_WAKE
    if (errno == ETIMEDOUT) return;
    if (errno == EAGAIN) return;         // *addr != 0 at kernel check; caller re-reads
    // EINTR (or anything else unexpected): retry
  }
}

static void mi_scav_wake_one(_Atomic(mi_scav_word_t)* addr) {
  syscall(SYS_futex, (uint32_t*)addr, MI_FUTEX_WAKE_PRIVATE, 1, NULL, NULL, 0);
}

#elif defined(__APPLE__)

// Darwin's private wait-on-address syscall. The public os_sync_wait_on_address
// is macOS 14.4+; __ulock_* has been stable since 10.12 and is what libc++ and
// Rust std park on.
#if defined(__cplusplus)
extern "C" {
#endif
extern int __ulock_wait(uint32_t operation, void* addr, uint64_t value, uint32_t timeout_us);
extern int __ulock_wake(uint32_t operation, void* addr, uint64_t wake_value);
#if defined(__cplusplus)
}
#endif
#define MI_UL_COMPARE_AND_WAIT  1
#define MI_ULF_NO_ERRNO         0x01000000

static void mi_scav_wait(_Atomic(mi_scav_word_t)* addr, mi_msecs_t timeout_ms) {
  if (timeout_ms <= 0) timeout_ms = 1;
  const uint32_t timeout_us = (uint32_t)timeout_ms * 1000u;
  while (mi_atomic_load_acquire(addr) == 0) {
    const int rc = __ulock_wait(MI_UL_COMPARE_AND_WAIT | MI_ULF_NO_ERRNO, (void*)addr, 0, timeout_us);
    if (rc >= 0) return;                 // woken or value already changed
    if (rc == -ETIMEDOUT) return;
    // -EINTR / -EFAULT: retry
  }
}

static void mi_scav_wake_one(_Atomic(mi_scav_word_t)* addr) {
  __ulock_wake(MI_UL_COMPARE_AND_WAIT | MI_ULF_NO_ERRNO, (void*)addr, 0);
}

#elif defined(_WIN32)

// WaitOnAddress/WakeByAddressSingle require Windows 8+ and link against
// `synchronization.lib` (added to `mi_libraries` in CMakeLists.txt for every Windows
// toolchain, MinGW included -- the `#pragma comment` below only reaches MSVC/clang-cl).
// windows.h is already included via mimalloc/atomic.h; declare here as well (matching the
// SDK signature) so older/MinGW headers that gate on _WIN32_WINNT still resolve.
#if defined(__cplusplus)
extern "C" {
#endif
BOOL WINAPI WaitOnAddress(volatile VOID* Address, PVOID CompareAddress, SIZE_T AddressSize, DWORD dwMilliseconds);
VOID WINAPI WakeByAddressSingle(PVOID Address);
#if defined(__cplusplus)
}
#endif
#if defined(_MSC_VER)
#pragma comment(lib, "synchronization")
#endif

static void mi_scav_wait(_Atomic(mi_scav_word_t)* addr, mi_msecs_t timeout_ms) {
  if (timeout_ms <= 0) timeout_ms = 1;
  mi_scav_word_t expected = 0;
  while (mi_atomic_load_acquire(addr) == 0) {
    // the size argument must follow `scavenger_wake`'s ACTUAL width (`mi_scav_word_t`), which is
    // pointer-width on Windows so the MSVC C atomics wrapper's word-width accessors fit it.
    if (!WaitOnAddress((volatile VOID*)addr, &expected, sizeof(mi_scav_word_t), (DWORD)timeout_ms)) {
      return;  // timeout (GetLastError() == ERROR_TIMEOUT)
    }
    // woken (possibly spuriously): loop re-checks *addr
  }
}

static void mi_scav_wake_one(_Atomic(mi_scav_word_t)* addr) {
  WakeByAddressSingle((PVOID)addr);
}

#else  // generic POSIX (FreeBSD, OpenBSD, etc.)

#include <pthread.h>
#include <time.h>
#if !defined(CLOCK_REALTIME)
#include <sys/time.h>
#endif

// One scavenger per process, so a file-static mutex/cond is sufficient and
// avoids bloating mi_subproc_s with platform-conditional fields.
//
// NOTE (fork, #270): these are raw pthread primitives, not `mi_lock_t`, so they are NOT
// part of `src/fork.c`'s lock-order table and are not acquired by `_mi_process_fork_prepare`.
// They cannot deadlock against it: nothing is ever acquired while holding them, and the
// only allocator state their holders touch is `subproc->scavenger_wake` (an atomic). A
// fork() that lands with the mutex held by a thread that does not exist in the child is
// handled by `mi_scav_fork_child_reset` below, called from `_mi_scavenger_forked_child`.
static pthread_mutex_t _mi_scav_mutex;   // initialized in `mi_scav_init` (from `_mi_scavenger_start`, before any wait or wake)
static pthread_cond_t  _mi_scav_cond;

static void mi_scav_wait(_Atomic(mi_scav_word_t)* addr, mi_msecs_t timeout_ms) {
  if (timeout_ms <= 0) timeout_ms = 1;
  struct timespec ts;
  #if defined(CLOCK_REALTIME)
  clock_gettime(CLOCK_REALTIME, &ts);
  #else
  struct timeval tv; gettimeofday(&tv, NULL);
  ts.tv_sec = tv.tv_sec; ts.tv_nsec = tv.tv_usec * 1000L;
  #endif
  ts.tv_sec  += (time_t)(timeout_ms / 1000);
  ts.tv_nsec += (long)((timeout_ms % 1000) * 1000000L);
  if (ts.tv_nsec >= 1000000000L) { ts.tv_sec += 1; ts.tv_nsec -= 1000000000L; }
  pthread_mutex_lock(&_mi_scav_mutex);
  while (mi_atomic_load_acquire(addr) == 0) {
    if (pthread_cond_timedwait(&_mi_scav_cond, &_mi_scav_mutex, &ts) == ETIMEDOUT) break;
  }
  pthread_mutex_unlock(&_mi_scav_mutex);
}

static void mi_scav_wake_one(_Atomic(mi_scav_word_t)* addr) {
  MI_UNUSED(addr);
  pthread_mutex_lock(&_mi_scav_mutex);
  pthread_cond_signal(&_mi_scav_cond);
  pthread_mutex_unlock(&_mi_scav_mutex);
}

// Some thread libraries (FreeBSD's) allocate a statically initialized mutex/condvar on its first use. With
// malloc overridden that would be an allocation by a thread that just published itself as parked
// (`mi_on_thread_idle_start` wakes us after that), racing the sweep of its theaps: initialize them here.
#define MI_SCAV_HAS_INIT  1
static void mi_scav_init(void) {
  pthread_mutex_init(&_mi_scav_mutex, NULL);
  pthread_cond_init(&_mi_scav_cond, NULL);
}

// fork() can land with `_mi_scav_mutex` held by a thread that no longer exists in the child.
#define MI_SCAV_HAS_FORK_RESET  1
static void mi_scav_fork_child_reset(void) {
  mi_scav_init();
}

#endif

#if !defined(MI_SCAV_HAS_FORK_RESET)
// futex / __ulock / WaitOnAddress hold no state of ours across fork()
static void mi_scav_fork_child_reset(void) { }
#endif
#if !defined(MI_SCAV_HAS_INIT)
static void mi_scav_init(void) { }
#endif


// -----------------------------------------------------------------------------
// Scavenger thread body (shared across platforms)
// -----------------------------------------------------------------------------

static void mi_scavenger_run(void) {
  // Use the main subproc directly: this thread never allocates, so don't
  // initialise a theap/tld via _mi_subproc()'s TLS path.
  mi_subproc_t* const subproc = _mi_subproc_main();
  while (mi_atomic_load_acquire(&_mi_scavenger_running) != 0) {
    // Clear with an RMW, not a plain store: it must be totally ordered against the parker's
    // coalescing `exchange(wake, 1)` in `_mi_scavenger_wake`. With a store, our clear and the later
    // `parked_count` read below can pass the parker's increment and its exchange in opposite
    // directions (store-buffering) -- we see no parked thread, it sees a stale wake==1 and issues
    // no syscall, and that park is silently deferred to the safety timeout.
    mi_atomic_exchange_acq_rel(&subproc->scavenger_wake, (mi_scav_word_t)0);
    // Do the idle work of any thread that parked and handed us its theaps. This is the expensive
    // part and it is why the owner gets to skip it.
    const mi_msecs_t park_due = _mi_theap_sweep_parked(subproc);
    mi_msecs_t expire = mi_atomic_loadi64_acquire(&subproc->purge_expire);
    mi_msecs_t timeout_ms;
    if (expire == 0) {
      // Nothing scheduled: park until woken. The 30s bound is a pure safety
      // net so stop() is guaranteed to take effect and any per-arena expiry
      // that did not propagate to subproc is still eventually purged.
      timeout_ms = 30000;
    }
    else {
      const mi_msecs_t now = _mi_clock_now();
      if (expire > now) {
        timeout_ms = expire - now;
        if (timeout_ms > 30000) timeout_ms = 30000;
      }
      else {
        _mi_arenas_try_purge(false /* force */, true /* visit_all */, subproc, 0 /* tseq */);
        // _mi_arenas_try_purge sets subproc->purge_expire to the earliest still-pending
        // per-arena expire once every arena is visited. If it left the stale past value
        // (its CAS lost to a concurrent schedule), clear it so the next iteration parks on
        // the 30s safety net instead of spinning. CAS so a concurrently scheduled future
        // expire is never clobbered.
        mi_atomic_casi64_strong_acq_rel(&subproc->purge_expire, &expire, (mi_msecs_t)0);
        continue;
      }
    }
    // a park passed over for its minimum interval is swept when its window ends, not at the safety timeout
    if (park_due > 0 && park_due < timeout_ms) { timeout_ms = park_due; }
    if (mi_atomic_load_acquire(&_mi_scavenger_running) == 0) break;
    mi_scav_wait(&subproc->scavenger_wake, timeout_ms);
  }
  // #272 profiler-interaction invariant (3): the scavenger must never initialise a theap of
  // its own -- it walks other threads' theaps and must not have this fork's per-thread
  // profiler/hook state (`mi_tld_t.profiler`, `mi_tld_t.hooks`) attached to it, nor appear in
  // `subproc->tlds` as a parkable thread. Everything it calls is either a pure atomic/OS
  // operation or runs against an explicitly passed `tld`.
  mi_assert_internal(!mi_theap_is_initialized(_mi_theap_default()));
}

bool _mi_scavenger_is_running(void) {
  return (mi_atomic_load_relaxed(&_mi_scavenger_running) != 0);
}

void _mi_scavenger_wake(mi_subproc_t* subproc) {
  if (subproc == NULL) return;
  if (mi_atomic_load_relaxed(&_mi_scavenger_running) == 0) return;
  // Coalesce: only issue the wake syscall on the 0->1 edge. Callers sit on
  // the page-free path and would otherwise turn every arena page free into a
  // syscall on the freeing thread.
  if (mi_atomic_exchange_acq_rel(&subproc->scavenger_wake, (mi_scav_word_t)1) == 0) {
    mi_scav_wake_one(&subproc->scavenger_wake);
  }
}

// -----------------------------------------------------------------------------
// Thread lifecycle
// -----------------------------------------------------------------------------

#if defined(_WIN32)

static HANDLE _mi_scavenger_thread;
// Set by the thread body as its very last act. It distinguishes "the thread ran to completion"
// from "the handle is signalled because the OS killed the thread" -- which is not an edge case
// on Windows but the NORMAL exit path for a statically linked exe: `mi_process_done` runs from
// the `.CRT$XLY` TLS callback at DLL_PROCESS_DETACH, i.e. from inside `ExitProcess`, which
// terminates every other thread first. See `_mi_scavenger_stop`.
static _Atomic(uintptr_t) _mi_scavenger_exited;

static DWORD WINAPI mi_scavenger_thread_main(LPVOID arg) {
  MI_UNUSED(arg);
  // SetThreadDescription is Windows 10 1607+ and absent from older SDK import
  // libraries, so resolve it at runtime; naming the thread is best-effort.
  typedef HRESULT (WINAPI *mi_set_thread_description_t)(HANDLE, PCWSTR);
  const HMODULE kernel32 = GetModuleHandleA("kernel32.dll");
  if (kernel32 != NULL) {
    const mi_set_thread_description_t set_desc =
      (mi_set_thread_description_t)(void*)GetProcAddress(kernel32, "SetThreadDescription");
    if (set_desc != NULL) { set_desc(GetCurrentThread(), L"mi-scavenger"); }
  }
  mi_scavenger_run();
  mi_atomic_store_release(&_mi_scavenger_exited, (uintptr_t)1);
  return 0;
}

void _mi_scavenger_start(void) {
  if (mi_atomic_load_acquire(&_mi_scavenger_running) != 0) return;
  if (mi_atomic_load_acquire(&_mi_scavenger_shutdown) != 0) return;   // teardown has begun
  if (!mi_option_is_enabled(mi_option_scavenger)) return;
  if (mi_option_get(mi_option_purge_delay) <= 0) return;
  // Claim `running` FIRST, then re-check `shutdown`. Loading `shutdown` before publishing
  // `running` leaves a window in which `_mi_scavenger_stop` sets `shutdown`, reads `running == 0`
  // and returns having joined nothing, and we then create a thread nobody will ever stop.
  // Ordering it this way, one of the two always sees the other: either stop reads `running == 1`
  // (and joins us), or we read `shutdown != 0` (and never start).
  mi_atomic_store_release(&_mi_scavenger_exited, (uintptr_t)0);
  if (mi_atomic_exchange_acq_rel(&_mi_scavenger_running, (uintptr_t)1) != 0) return;  // someone else got there
  if (mi_atomic_load_acquire(&_mi_scavenger_shutdown) != 0) {
    mi_atomic_store_release(&_mi_scavenger_running, (uintptr_t)0);
    return;
  }
  // published under the same edge that owns `running`, so a stop that sees `running == 1` sees this
  _mi_scavenger_thread = CreateThread(NULL, 0, &mi_scavenger_thread_main, NULL, 0, NULL);
  if (_mi_scavenger_thread == NULL) {
    mi_atomic_store_release(&_mi_scavenger_running, (uintptr_t)0);
  }
}

void _mi_scavenger_stop(void) {
  mi_atomic_store_release(&_mi_scavenger_shutdown, (uintptr_t)1);   // before the exchange: no restart past here
  if (mi_atomic_exchange_acq_rel(&_mi_scavenger_running, (uintptr_t)0) == 0) return;
  mi_subproc_t* const subproc = _mi_subproc_main();
  mi_atomic_store_release(&subproc->scavenger_wake, (mi_scav_word_t)1);
  mi_scav_wake_one(&subproc->scavenger_wake);
  if (_mi_scavenger_thread != NULL) {
    // BOUNDED, never INFINITE. This runs first in `mi_process_done_once`, and on Windows that
    // is reached from the `.CRT$XLY` TLS callback at DLL_PROCESS_DETACH -- inside `ExitProcess`,
    // under the loader lock. A join that does not return there hangs the process for good, and
    // the process is exiting anyway (`_mi_scavenger_running` is already 0).
    const DWORD waited = WaitForSingleObject(_mi_scavenger_thread, 2000);
    if (waited != WAIT_OBJECT_0) {
      // Still running and not responding: leak the handle rather than close one the thread is
      // still using, and leave the arena purge guard alone -- the thread may legitimately own it.
      _mi_verbose_message("scavenger thread did not stop within 2s (wait result 0x%zx); detaching\n", (size_t)waited);
      _mi_scavenger_thread = NULL;
      return;
    }
    if (mi_atomic_load_acquire(&_mi_scavenger_exited) == 0) {
      // Signalled, but the body never reached its epilogue: `ExitProcess` terminated it where it
      // stood. If that was inside `mi_atomic_guard(&mi_arenas_purge_guard)` the guard is orphaned,
      // and the forced purge that `mi_process_done_once` runs right after us
      // (`mi_theap_collect(theap, true)` -> `_mi_arenas_try_purge(force)`) would spin on it
      // forever. We are the only thread left, so releasing it races with nobody.
      const bool was_held = _mi_arenas_purge_guard_reset();
      _mi_verbose_message("scavenger thread was terminated by the process exit (arena purge guard was %s)\n",
                          (was_held ? "held" : "free"));
    }
    CloseHandle(_mi_scavenger_thread);
    _mi_scavenger_thread = NULL;
  }
}

void _mi_scavenger_forked_child(void) { }    // no fork on Windows
void _mi_scavenger_start_lazy(void) {        // see the POSIX one
  if (mi_atomic_load_relaxed(&_mi_scavenger_running) != 0) return;
  static _Atomic(uintptr_t) started;
  if (mi_atomic_exchange_acq_rel(&started, (uintptr_t)1) != 0) return;
  _mi_scavenger_start();
}

#else  // POSIX

#include <pthread.h>
#include <signal.h>
#if defined(__linux__)
#include <sys/prctl.h>
#endif

static pthread_t          _mi_scavenger_thread;
static _Atomic(uintptr_t) _mi_scavenger_joinable;
static _Atomic(uintptr_t) _mi_scavenger_needs_restart;   // fork() took our thread; start one on next use

static void* mi_scavenger_thread_main(void* arg) {
  MI_UNUSED(arg);
  #if defined(__APPLE__)
  pthread_setname_np("mi-scavenger");
  #elif defined(__linux__)
  prctl(PR_SET_NAME, "mi-scavenger", 0, 0, 0);
  #endif
  mi_scavenger_run();
  return NULL;
}

void _mi_scavenger_start(void) {
  if (mi_atomic_load_acquire(&_mi_scavenger_running) != 0) return;
  if (mi_atomic_load_acquire(&_mi_scavenger_shutdown) != 0) return;   // teardown has begun
  if (!mi_option_is_enabled(mi_option_scavenger)) return;
  if (mi_option_get(mi_option_purge_delay) <= 0) return;
  // Claim `running` FIRST, then re-check `shutdown` -- see the Windows `_mi_scavenger_start`
  // above for why the other order lets a start slip past the stop that should have joined it.
  if (mi_atomic_exchange_acq_rel(&_mi_scavenger_running, (uintptr_t)1) != 0) return;
  if (mi_atomic_load_acquire(&_mi_scavenger_shutdown) != 0) {
    mi_atomic_store_release(&_mi_scavenger_running, (uintptr_t)0);
    return;
  }
  mi_scav_init();
  // Block all signals on the scavenger thread. It runs before the host has set
  // up its own signal masking, and a thread that leaves (e.g.) SIGCHLD
  // unblocked will have process-directed signals dispatched to it and silently
  // discarded, starving signalfd/kqueue consumers. sigfillset on glibc/musl
  // already excludes the libc-internal realtime signals used for setxid/cancel.
  //
  // Except the signals a fault on this thread itself raises: a blocked SIGSEGV/SIGBUS
  // is not queued, the kernel resets it to its default action and kills the process on
  // the spot, so the host's crash handler never runs and a corrupted free list that the
  // sweep trips over ends the process without a report. These are thread-directed by
  // nature, so leaving them unblocked starves no one.
  sigset_t all, old;
  sigfillset(&all);
  sigdelset(&all, SIGSEGV);
  sigdelset(&all, SIGBUS);
  sigdelset(&all, SIGILL);
  sigdelset(&all, SIGFPE);
  sigdelset(&all, SIGTRAP);
  sigdelset(&all, SIGABRT);
  sigdelset(&all, SIGSYS);
  pthread_sigmask(SIG_SETMASK, &all, &old);
  if (pthread_create(&_mi_scavenger_thread, NULL, &mi_scavenger_thread_main, NULL) != 0) {
    mi_atomic_store_release(&_mi_scavenger_running, (uintptr_t)0);
  }
  else {
    mi_atomic_store_release(&_mi_scavenger_joinable, (uintptr_t)1);
  }
  pthread_sigmask(SIG_SETMASK, &old, NULL);
}

void _mi_scavenger_stop(void) {
  mi_atomic_store_release(&_mi_scavenger_shutdown, (uintptr_t)1);   // before the exchange: no restart past here
  if (mi_atomic_exchange_acq_rel(&_mi_scavenger_running, (uintptr_t)0) == 0) return;
  mi_subproc_t* const subproc = _mi_subproc_main();
  mi_atomic_store_release(&subproc->scavenger_wake, (mi_scav_word_t)1);
  mi_scav_wake_one(&subproc->scavenger_wake);
  if (mi_atomic_exchange_acq_rel(&_mi_scavenger_joinable, (uintptr_t)0) != 0) {
    pthread_join(_mi_scavenger_thread, NULL);
  }
}

// The thread does not survive fork(), but every flag saying it does is inherited. Left alone the
// child would: take the wake path in `_mi_arenas_purge_now` and signal nobody (so never purge at
// all), and `pthread_join` a `pthread_t` that names no thread at exit.
void _mi_scavenger_forked_child(void) {
  mi_atomic_store_release(&_mi_scavenger_shutdown, (uintptr_t)0);   // a fresh image, not a teardown
  mi_atomic_store_release(&_mi_scavenger_joinable, (uintptr_t)0);
  mi_atomic_store_release(&_mi_scavenger_running, (uintptr_t)0);
  mi_scav_fork_child_reset();
  mi_atomic_store_release(&_mi_scavenger_needs_restart, (uintptr_t)1);
}

// Start the scavenger when a second thread initializes or a thread first parks: not at process
// initialization, which for an inserted/preloaded library runs before the other libraries' initializers
// (on macOS the Objective-C runtime aborts if a thread exists before it initializes), and would give
// every short-lived single-threaded process a thread it never uses (a purge that is due without a
// scavenger runs inline, as upstream does). Also the restart after fork(): not in the fork handler, as
// most children exec immediately.
void _mi_scavenger_start_lazy(void) {
  if (mi_atomic_load_relaxed(&_mi_scavenger_running) != 0) return;
  static _Atomic(uintptr_t) started;   // once per process image, plus once per fork
  const bool forked = (mi_atomic_exchange_acq_rel(&_mi_scavenger_needs_restart, (uintptr_t)0) != 0);
  if (mi_atomic_exchange_acq_rel(&started, (uintptr_t)1) != 0 && !forked) return;
  _mi_scavenger_start();
}

#endif

#endif


/* -----------------------------------------------------------
  Public entry points
----------------------------------------------------------- */

// The original entry point: do the work inline, on the calling thread. Kept for callers that
// have no wake-up side to pair with (and as the fallback when no scavenger is running).
void mi_on_thread_idle(void) mi_attr_noexcept {
  mi_theap_t* theap0 = _mi_theap_default();
  if (theap0 == NULL || !mi_theap_is_initialized(theap0) || theap0->tld == NULL) return;
  if (theap0->tld->thread_id != _mi_thread_id()) return;
  // #366: an allocator call, so it takes the owner door: in a gated build this thread is PARKED
  // on entry and the scavenger may be sweeping it; the hole sweep below rewrites free lists
  // outside any gated entry point, so the whole body is one enter / one leave.
  MI_GATE_ENTER(theap0);
  _mi_thread_idle_work(theap0->tld, theap0);
  MI_GATE_LEAVE(theap0->tld);
}

// Declare that this thread will not allocate or free until `mi_on_thread_idle_end` -- the sweep's
// precondition -- so the scavenger can do it while we block.
//
// Returns false when nothing was handed off, and then `mi_on_thread_idle_end` is not required.
// It deliberately does NOT sweep inline in that case: a caller parks far more often than it is
// idle, and sweeping on every park is what it is trying to avoid. Only the caller knows whether
// this park is idle enough to afford `mi_on_thread_idle()` instead.
bool mi_on_thread_idle_start(void) mi_attr_noexcept {
  mi_theap_t* const theap0 = _mi_theap_default();
  if (theap0 == NULL || !mi_theap_is_initialized(theap0) || theap0->tld == NULL) return false;
  mi_tld_t* const tld = theap0->tld;
  if (tld->thread_id != _mi_thread_id()) return false;
  // the scavenger only sweeps the main subproc, so a thread elsewhere would never be swept
  if (tld->subproc != _mi_subproc_main()) return false;
  _mi_scavenger_start_lazy();
  if (!_mi_scavenger_is_running()) return false;
  #if MI_OWNER_GATE
  // #366: in a gated build this thread is already PARKED whenever it is outside the allocator
  // (`_mi_gate_leave`), and the scavenger's timed sweep reaches it without a hand-off -- which
  // is why the lazy start above still runs: the plan (§2) needs the scavenger running to pace
  // busy threads. Report "nothing handed off" so callers keep their documented fallback;
  // `mi_on_thread_idle()` still works (it is an allocator call). Nudge the scavenger anyway:
  // a caller that announces idleness is exactly the thread whose park is due.
  _mi_scavenger_wake(tld->subproc);
  return false;
  #else

  // Already parked (a second `_start` without an `_end`): the scavenger may be reading the fields
  // below right now. Only this thread takes the state out of RUNNING, so past this check they are ours.
  if (mi_atomic_load_acquire(&tld->park_state) != MI_PARK_RUNNING) return false;
  // The scavenger has no TLS of ours to find the default theap with, so leave it here.
  tld->park_theap0 = theap0;
  mi_atomic_store_release(&tld->park_reclaim, 0);
  mi_atomic_store_release(&tld->park_swept, 0);
  size_t expected = MI_PARK_RUNNING;
  if (!mi_atomic_cas_strong_acq_rel(&tld->park_state, &expected, MI_PARK_PARKED)) return false;
  mi_atomic_increment_relaxed(&tld->subproc->parked_count);
  _mi_scavenger_wake(tld->subproc);
  return true;
  #endif
}

// The other half: we are awake and about to allocate again, so take the theaps back. Usually an
// uncontended CAS. If the scavenger is mid-sweep we ask it to stop (it checks between phases) and
// spin until it does -- normally a syscall-free wait.
void mi_on_thread_idle_end(void) mi_attr_noexcept {
  #if MI_OWNER_GATE
  // #366: no-op -- `_start` handed nothing off, and the next allocator call is the acquire.
  #else
  mi_theap_t* const theap0 = _mi_theap_default();
  if (theap0 == NULL || !mi_theap_is_initialized(theap0) || theap0->tld == NULL) return;
  mi_tld_t* const tld = theap0->tld;
  if (tld->thread_id != _mi_thread_id()) return;
  _mi_park_leave(tld);
  #endif
}

void mi_scavenger_stop(void) mi_attr_noexcept {
  _mi_scavenger_stop();
}
