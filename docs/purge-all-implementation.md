# `mi_purge_all` and `MI_OWNER_GATE` — implementation plan

Implementation issue: #366. Research/design record: #335. Background: #365. This document is the consolidated plan; it
supersedes the design comments on #335 and is what the implementation PRs follow.

## 1. Goal

A public call that, from one thread, returns as much memory as the allocator's invariants
allow across **all** threads' heaps — arenas, abandoned pages, and every thread's own
pages and holes — and reports exactly what it could not reach.

Two build configurations, two contracts:

| build | reach of `mi_purge_all` | fast-path cost |
|---|---|---|
| default (`MI_OWNER_GATE=OFF`) | arenas, abandoned pages, the caller's own tld, threads parked in `mi_on_thread_idle_start` | none — `malloc`/`free` byte-identical to today |
| `MI_OWNER_GATE=ON` | all of the above **plus every registered thread**, busy or not | one CAS + one release-store per outermost allocator call, plus one TLS load on `free` |

The gated build is for clients that trade allocation speed for RSS return.

## 2. The mechanism: the park protocol with its default inverted

`mi_tld_t::park_state` (`include/mimalloc/types.h`) is already a three-state ownership
lock:

| state | meaning |
|---|---|
| `MI_PARK_RUNNING` | only the owner may touch its theaps |
| `MI_PARK_PARKED` | the owner is not inside the allocator; a foreign sweeper may claim it |
| `MI_PARK_SWEEPING` | a foreign sweeper holds it; the owner waits to enter |

Today a thread is `RUNNING` by default and `PARKED` only inside
`mi_on_thread_idle_start()…_end()`. In a gated build a thread is **`PARKED` whenever it is
outside the allocator and `RUNNING` only while inside it**. Entering the allocator is the
owner acquire (`_mi_park_leave`: CAS `PARKED→RUNNING`, or wait out `SWEEPING`); leaving is a
release-store of `PARKED`. Foreign sweepers use the existing claim CAS `PARKED→SWEEPING`.

Consequences that fall out for free:

- the master list of thread locks is `subproc->tlds` under `tlds_lock` (fork level 4);
- the sweep body is `_mi_thread_idle_work` (`src/scavenger.c`), already proven on a foreign
  thread with the owner blocked;
- the background scavenger's timed sweep now reaches busy threads too, paced by
  `purge_holes_min_interval` and bounded per visit by `park_reclaim`;
- `mi_on_thread_idle_start/_end` become no-ops in a gated build (the thread is already
  parked); `mi_on_thread_idle()` keeps working (it is an allocator call).

No new `mi_lock_t` is introduced.

## 3. Data structures

New fields on `mi_tld_t`, appended at the **tail** (after `holes_sweep_visited`;
`mi_tld_detached` in `src/init.c` is initialised positionally), width-pinned by the
`mi_scav_atomic_widths_assert_t` assert in `src/scavenger.c`, reset in the fork child loop:

| field | type | written by | purpose |
|---|---|---|---|
| `gate_depth` | `size_t` (plain) | owner only | recursion count; acquire on `0→1`, release on `1→0` |
| `sweeper` | `_Atomic(uintptr_t)` | claimant | thread id of the thread holding the `SWEEPING` claim; authorises the foreign door |
| `purge_epoch` | `_Atomic(size_t)` | purge driver, `mi_tld_register` | walk progress / cutoff |
| `gate_flags` | `_Atomic(size_t)` | fork child, purge driver | bit 0 `orphan` (inherited from a vanished pre-fork thread); bit 1 `reclaim_ignored` (this claimed sweep ignores `park_reclaim`) |

Process-wide: `_Atomic(uintptr_t) mi_purge_admission` (holder thread id, 0 = free),
`_Atomic(size_t) mi_purge_seq`.

`gate_depth` and `gate_flags` exist in both builds (so `mi_tld_t` layout does not depend
on the flag and the Rust layout probe stays single); only the gated build reads them.

## 4. Public API

<!-- doc-snippet: skip (mirrors the declarations in include/mimalloc.h) -->
```c
typedef enum mi_purge_flags_e {
  MI_PURGE_FORCE = 1,   // ignore purge_delay / hole-purge pacing; a claimed sweep ignores park_reclaim
} mi_purge_flags_t;

typedef struct mi_purge_all_report_s {
  size_t arena_bytes;        // returned by the arena passes
  size_t hole_bytes;         // returned by hole purging (all theaps + abandoned)
  size_t theaps_swept;       // tlds claimed and swept by this call (caller included)
  size_t theaps_pending;     // registered tlds not reached within wait_ms
  size_t theaps_orphaned;    // pre-fork tlds of vanished threads, never touched
  bool   gated;              // MI_OWNER_GATE build (configuration, not completion)
  bool   complete;           // theaps_pending == 0 && theaps_orphaned == 0
} mi_purge_all_report_t;

#define MI_PURGE_OK       0
#define MI_PURGE_PARTIAL  1   // some owners pending; see report
#define MI_PURGE_BUSY     2   // another purge in flight, or re-entrant call; nothing was done

mi_decl_export int  mi_purge_all_ex(mi_purge_flags_t flags, size_t wait_ms,
                                    mi_purge_all_report_t* report /* may be NULL */) mi_attr_noexcept;
mi_decl_export void mi_purge_all(bool force) mi_attr_noexcept;   // == _ex(force ? MI_PURGE_FORCE : 0, 100, NULL)
```

Placed next to `mi_on_thread_idle` in `include/mimalloc.h`. `wait_ms` bounds
**owner-acquisition waiting only** — not a claimed tld's sweep and not its `madvise`
calls. The status is the return value so it is observable with `report == NULL`.

In an ungated build the walk in §7 phase D claims only `PARKED` tlds (those inside
`mi_on_thread_idle_start`) and reports every `RUNNING` one as pending.

## 5. The gate (`include/mimalloc/owner-gate.h`, new)

```c
#if MI_OWNER_GATE
static inline mi_theap_t* _mi_gate_enter(mi_theap_t* theap);   // returns the (possibly just-initialised) theap
static inline void        _mi_gate_leave(mi_tld_t* tld);
static inline bool        _mi_gate_held(const mi_tld_t* tld);  // MI_DEBUG leaf assertion predicate
#define MI_GATE_ENTER(theap)  (theap = _mi_gate_enter(theap))
#define MI_GATE_LEAVE(tld)    _mi_gate_leave(tld)
#else
#define MI_GATE_ENTER(theap)  ((void)0)
#define MI_GATE_LEAVE(tld)    ((void)0)
#endif
```

**Enter.** If `theap` is not initialised: `theap = mi_theap_get_default()` first (this runs
`mi_thread_init`, which registers the tld `RUNNING`), then continue — so a tld is published
with a live outer depth and nested allocations during initialisation are ordinary recursion.
Then `if (tld->gate_depth++ == 0) _mi_park_leave_gate(tld)`, where `_mi_park_leave_gate`
is `_mi_park_leave` without the `parked_count` decrement (and treats an initial `RUNNING`
as "mine", as `_mi_park_leave` already does).

**Leave.** `if (--tld->gate_depth == 0) mi_atomic_store_release(&tld->park_state, MI_PARK_PARKED)`.

**Held** (debug): `(tld->gate_depth > 0 && tld->thread_id == _mi_thread_id()) ||
(park_state == SWEEPING && sweeper == _mi_thread_id())`.

### 5.1 Enter/leave sites (a few guarded lines each, upstream files)

| site | file | why this one |
|---|---|---|
| `mi_theap_malloc_small_zero_nonnull` | `src/alloc.c` | the real small-path choke point; `mi_heap_malloc_small` and `_mi_theap_malloc_zero_ex` call it directly |
| `mi_theap_malloc_generic` | `src/alloc.c` | every non-small allocation |
| `mi_theap_malloc_zero_aligned_at`, before `_mi_theap_get_free_small_page` | `src/alloc-aligned.c` | the aligned fast path reads `page->free` and calls `_mi_page_malloc_zero` itself |
| `mi_theap_malloc_guarded_hooked` | `src/alloc.c` (`MI_GUARDED`) | separate path |
| `mi_free_ex` | `src/free.c` | every free; gated on `_mi_theap_default()->tld` regardless of branch, because the mt branch may reclaim an abandoned page into the caller's theap |
| `mi_theap_collect`, `mi_collect`, `mi_heap_collect` | `src/theap.c` | the **public** collect entries — not `mi_theap_collect_ex` (§6) |
| `_mi_thread_done` | `src/init.c` | teardown; enter, abandon theaps, `mi_tld_unregister`, free the tld — **no leave** (an unregistered `RUNNING` tld can never be found or claimed; nothing dereferences the freed tld) |
| `mi_heap_delete`, `mi_heap_destroy`, `mi_theap_set_default` | `src/heap.c`, `src/theap.c` | mutate theap ownership |

Every gated body has exactly one leave: `r = inner(...); MI_GATE_LEAVE(tld); return r;`
with `inner` being the existing body. No leave on an early-return path.

Not gated: `mi_free_block_mt`'s atomic push onto `xthread_free`; arena, page-map and OS
layers; profiler and memory-events internals (rule 4 memory, never theap state).

### 5.2 Coverage is a tested property

In `MI_DEBUG` gated builds every owner-private leaf accessor asserts `_mi_gate_held(theap->tld)`:
`_mi_theap_get_free_small_page` (`internal.h`), `mi_page_malloc_zero` and
`_mi_page_malloc_zero` (`src/alloc.c`), `mi_free_block_local` (`src/free.c`),
`mi_page_queue_push/remove/enqueue_from`, `mi_page_retire`, `_mi_page_free_collect`.
The full C suite in the Debug gated ASan row then proves that no path reads
owner-private state outside the gate — now and after every upstream sync. The site list
in §5.1 is the starting point; the assertion is the proof.

## 6. Two doors into the collect body

`mi_theap_collect_ex` (internal, `src/theap.c`) becomes the un-gated body and takes a
context `{ bool foreign; bool force; }`.

- **Owner door**: the public collect entries in §5.1 — enter, body with `foreign=false`, leave.
- **Foreign door**: `_mi_thread_idle_work_ex(mi_tld_t* tld, mi_theap_t* theap0, bool force)`
  in `src/scavenger.c` — `_mi_thread_idle_work` with a `force` bit and **without** the
  trailing `_mi_arenas_purge_now` (the driver purges arenas once). Callable only by the
  thread holding the claim: asserts `tld->sweeper == _mi_thread_id()`. Never touches
  `gate_depth`. `_mi_deferred_free` stays owner-only through its existing `thread_id` guard
  (`src/page.c`). The profiler per-thread invariant asserts move from
  `_mi_theap_sweep_parked` into this function so they cover both callers.
  `_mi_thread_idle_work(tld, theap0)` becomes `_ex(tld, theap0, false)` followed by the arena purge.
- `force` reaches the per-page loops: `mi_theap_page_collect` already sees `MI_FORCE`;
  `_mi_purge_holes_of(mi_tld_t*, bool force)` (signature change; two existing callers pass
  `false`) skips the `purge_holes_min_interval` check but still stamps `holes_sweep_last`,
  sets `holes_sweep_full` for the pass, and reads `gate_flags.reclaim_ignored` instead of
  `park_reclaim` when set.

Audit result for a sweeper that has its own initialised theap (the scavenger has none):
`_mi_page_purge_holes_in_progress` reads the **caller's** `holes_sweeping`, which is the
correct question for a nested allocation on the caller's own theaps;
`_mi_purge_holes_report_collect` is owner-only and not on the sweep path; hook accessors
(`hooks-tld.h`) resolve to the caller's hook state. Documented consequence: memory-events /
profiler bookkeeping emitted by a foreign sweep is attributed to the purging thread. The
"no default theap" assertion in `_mi_theap_sweep_parked` stays where it is.

## 7. `mi_purge_all_ex` (`src/purge-all.c`, new; included from `src/static.c` unconditionally)

```
seq = ++mi_purge_seq
if !CAS(mi_purge_admission, 0 -> me): return MI_PURGE_BUSY        // before any work
A. for each subproc: _mi_arenas_try_purge(force, visit_all=true, sp, 0)   (FORCE)
                     or _mi_arenas_purge_now(sp)                            (otherwise)
B. for each subproc, under sp->heaps_lock: for each heap (skip heap->releasing):
     _mi_arenas_purge_abandoned_holes(heap, my_tld)
C. my own tld, owner door: mi_theap_collect_ex(my theap, force) + _mi_purge_holes_of(my_tld, force)
D. the walk (below)
E. one final _mi_arenas_try_purge(force, ...) per subproc so pages freed by D leave now
release admission (one exit path); fill report from stat deltas; return OK / PARTIAL
```

Phase A runs under no lock — the forced-purge guard argument in `src/arena.c` ("the only
forced caller is a user thread holding no lock") is restated for this second caller.

### 7.1 The walk (phase D)

```
deadline = now + wait_ms
loop:
  claimed = NULL
  mi_lock(sp->tlds_lock):
    for tld in sp->tlds:
      if tld->purge_epoch == seq: continue                 // done, given up, or registered after seq
      if tld == my_tld: stamp; continue                    // phase C did it
      if gate_flags.orphan: stamp; orphaned++; continue
      if CAS(park_state, PARKED -> SWEEPING):
         sweeper = me; if FORCE set reclaim_ignored; stamp; claimed = tld; break
      // RUNNING: leave for a later pass; no pointer kept
  if claimed:
     _mi_thread_idle_work_ex(claimed, claimed->park_theap0 or first theap, force); swept++
     clear reclaim_ignored; sweeper = 0; store_release(park_state, PARKED); continue
  if no unstamped tld remained: break                       // complete
  if now >= deadline: under tlds_lock stamp all remaining, pending += n; break
  pause 256x, then yield                                     // let RUNNING owners leave
```

Rules the loop encodes:

- **No pointer to an unclaimed tld leaves `tlds_lock`** — the scavenger's rule, and what
  makes the no-leave teardown in §5.1 safe.
- **Registry cutoff**: `mi_tld_register` (`src/init.c`) stamps `purge_epoch = mi_purge_seq`
  under `tlds_lock`, so threads created during a walk are excluded and thread churn cannot
  extend it.
- **Bounded waiting**: `wait_ms` bounds acquisition only. A worker blocked inside an
  allocator callback on an application mutex held by the purge caller ends up `pending`;
  the library never waits unboundedly and does not claim universal deadlock-freedom.
- **Orphans** are skipped, counted, never parked, never swept (§8).
- In an ungated build the same loop runs; every `RUNNING` tld is stamped pending on the
  first pass (no owner will ever park), so `wait_ms` is not consumed.

## 8. Fork (`src/fork.c`)

- `_mi_process_fork_prepare` already `_mi_park_leave`s the caller's tld; in a gated build
  that is the caller's acquire. Its `gate_depth` is live (prepare may run inside an
  allocator hook) and is **preserved** in both parent and child; the matching leave happens
  when the enclosing operation returns.
- Child reset loop (the existing per-tld loop): add `gate_flags.orphan = (tld != survivor)`
  where `survivor` is the forking thread's tld; reset `sweeper = 0`, `purge_epoch = 0`,
  clear `reclaim_ignored`. Orphans keep `park_state = RUNNING` as today; their pages are
  reclaimed by the existing #271 mechanisms (`_mi_process_is_forked_child`-gated
  re-derivation from the arena bitmaps), which this plan does not touch.
- Child reset also clears `mi_purge_admission` (a purge in flight in the parent must not
  leave the child permanently `BUSY`; same class as `_mi_arenas_fork_child`).
- Lock-order table: no new `mi_lock_t`. Add entries under "non-lock state with child
  resets" for `mi_purge_admission`, `sweeper`, and the documented cross-tld ordering
  *self `RUNNING` → other `SWEEPING`* (a sweeper never waits on the owner it holds).
- `MI_DEBUG>2` checker: the walk's edges (`tlds_lock` → claim CAS → release → sweep body's
  existing edges) are already tabled; `test-fork-locks` gets a case to prove it.

## 9. Other interactions

- **Rule 4**: the driver allocates nothing on a hooked path; the report is caller-owned;
  every hole discard goes through `_mi_page_purge_holes` with its
  `_mi_prof_debug_assert_no_records_in`.
- **Windows atexit scavenger stop**: not needed by anything here; safe from an `atexit`
  handler on the main thread before `mi_process_done`; not called from `mi_process_done_once`.
- **Sub-processes**: the walk covers all subprocs; in a gated build their threads are
  `PARKED` by the gate and are reached; `mi_on_thread_idle_start`'s non-main refusal stays.
- **`MI_NO_PROCESS_DETACH`**: unaffected.
- **`mi_collect_reduce`**: an orphan declaration (`include/mimalloc.h`, no definition in the
  v3 tree). Left alone; removal belongs in a `pr/*` against `dev3`.

## 10. Build, CI, measurement

- CMake option `MI_OWNER_GATE` (OFF) → `-DMI_OWNER_GATE=1`. `src/static.c` includes
  `purge-all.c` unconditionally. Rust: cargo feature `owner-gate` → the same define in
  `build.rs` (separate commit).
- **c-unit.yml**: one new `build` matrix row `MI_OWNER_GATE=ON, MI_PPROF=ON` on ubuntu,
  native MSVC (`cl`) and win-gnu (`windows-bundles.yml`), running the **whole** suite (the
  gate touches every path). ASan row: Debug, `MI_DEBUG=3`. `verify_local.py` gets the
  matching config. `docs/ci-gates.md` rows.
- **`ci/check_fastpath_identity.py`** (new, stdlib only): build `libmimalloc.a` Release at
  `--base <rev>` and HEAD, `objdump -d --no-show-raw-insn --disassemble=<sym>` for
  `mi_malloc`, `mi_zalloc`, `mi_free`, `mi_heap_malloc_small`, `mi_malloc_small`,
  address-normalised diff must be empty for the OFF build; `--expect-dirty` positive
  control via `-DMI_PURGE_ALL_FASTPATH_CANARY`. Runs in `verify_local.py` and the Linux
  `c-unit` stage.
- **Speed acceptance**: `uv run ci/dev_linux.py bench` ON vs OFF, pasted on #10 and the PR.
  The gated build has a measured cost, not a "no cost" claim.
- **Sizing run before phase 1 merges** (#365 §6): `ci/bench_hole_purging_allocators.py`
  gains a mode with N busy allocating threads and the purge issued from a separate thread,
  run for this fork ungated (`mi_collect(true)`) and gated (`mi_purge_all(true)`), upstream
  (`mi_collect(true)`), jemalloc (`arena.<ALL>.purge`) and glibc (`malloc_trim(0)`). Those
  are the numbers the README cell cites.
- Memory gate (`test-memory-gate.c`) runs in both builds; workload untouched.

## 11. Tests — `test/test-purge-all.cpp` (new; threading modelled on `test-thread-idle-rss.cpp`)

Ungated and gated builds run the same binary; each case states its expectation per build.

| id | case | expectation |
|---|---|---|
| T1 | reference: N=4 workers churn (park-handoff shape, 1-in-20 survivors), each calls `mi_on_thread_idle()` | records `R_ref` from `mi_purge_holes_stats_get().purged_bytes` (machine-measured, not hard-coded) |
| T2 | abandoned reach: workers exit; `mi_collect(true)` then `mi_purge_all(true)` from main | `mi_collect` leaves `purged_bytes` unchanged; `mi_purge_all` reaches them (both builds) |
| T3 | re-entrancy: `mi_purge_all` from a deferred-free handler; from an output hook | `BUSY`; no deadlock; `gate_depth` back to 0 (debug-asserted at every leave) |
| G1 | busy owners: 4 workers in a tight hot-set `malloc`/`free` loop that never idles (negative control: `discard_calls` flat for 200 ms); `mi_purge_all(true)` from main | gated: `swept == 5`, `pending == 0`, `purged_bytes ≥ 0.9·R_ref`, return `OK`. **Ungated (positive control): `pending == 4`, return `PARTIAL`** |
| G2 | stall: each worker records its max single-call latency during G1 | printed; asserted `< 250 ms` on CI hardware — a measurement, not a documented bound |
| G3 | owner inside the allocator: a `MI_DEBUG` pause hook holds one worker `RUNNING` | `_ex(0, 20, &r)` → `PARTIAL`, `pending == 1`, returns within `wait_ms` + one sweep; after the hook releases, a retry → `OK` |
| G4 | registry churn: `mi_purge_all(true)` in a loop while 4 threads churn and 2 threads start/allocate/exit repeatedly; ASan + `MI_DEBUG=3` | terminates every call; no UAF; cutoff excludes threads born mid-walk |
| G5 | two callers simultaneously | exactly one `BUSY`, one `OK`; admission released on both exits |
| G6 | callback blocked on the caller's mutex: a worker's deferred-free handler waits on a mutex main holds while main calls `_ex(FORCE, 50, &r)` | `PARTIAL`, `pending == 1`, returns within `wait_ms` + one sweep; after main releases the mutex, retry → `OK` |
| G7 | starvation: a worker re-enters the allocator in a tight loop with no work between calls | claimant gets it within `wait_ms` or reports it; never spins past the deadline |
| G8 | scavenger pacing: scavenger on, `purge_holes_min_interval=50`, busy workers, nobody calls anything | gated: `mi_idle_work_count` advances; `-no-scavenger` variant (`MIMALLOC_SCAVENGER=0`): it does not |
| C1 | coverage: deterministic pause points before the page lookup in small, explicit-heap, aligned-fast, guarded, first-allocation-of-a-thread and output-hook-recursion-during-`mi_thread_init` paths, each raced by a claimant | leaf assertion + ASan are the oracle; ungated: `check_fastpath_identity` unchanged |
| C2 | foreign collect with the owner spinning to enter; purge caller has an allocating output hook | progress; owner `gate_depth` and `mi_tld_t::profiler` unchanged across the sweep |
| F1 | `test-fork-locks`: fork with a live registered sibling; forced purge in the child before any new thread; again after a child worker exists; fork *during* a purge; fork from inside an allocator hook | `orphaned == 1, pending == 0` immediately; child worker swept; child admission clear; survivor depth balanced; checker reports no unlisted edge |

`add_test` for both the default and `-no-scavenger` variants; the gated rows run the
whole existing suite as well. `ctest TIMEOUT 120` turns any hang into a failure.

## 12. Phases (one PR each; C-core paths and `rust/` never in one commit)

**Phase 1 — `feat: mi_purge_all + MI_OWNER_GATE`**, branch `feat/purge-all-gate`.
`include/mimalloc.h` API; `include/mimalloc/owner-gate.h`; `src/purge-all.c`;
`mi_tld_t` fields + `mi_tld_detached` initialiser + width assert; §5.1 sites; §5.2 leaf
asserts; §6 refactor (`mi_theap_collect_ex` context, `_mi_thread_idle_work_ex`,
`_mi_purge_holes_of(tld, force)`); `mi_tld_register` epoch stamp; §8 fork changes and
table entries; CMake option; `src/static.c`; `ci/check_fastpath_identity.py`;
`test/test-purge-all.cpp` (all rows) + `test-fork-locks` F1; CI rows; `docs/ci-gates.md`.
Gates: c-unit ON/OFF ubuntu + MSVC + win-gnu, the new gated rows, rust-native, asan,
windows-bundles, python-lint. Bench output ON vs OFF on #10.

**Phase 2 — `feat(rust): purge_all + owner-gate feature`** then **`docs:`** (two commits).
`rust/mimalloc-pprof/src/{sys,lib}.rs` (`mi_purge_all_ex`, `mi_purge_all_report_t`
`repr(C)`, safe `purge_all(force) -> PurgeAllReport`, `PurgeStatus`), `tests/t19_layout.rs`
size check, `ci/check_rust_surface.py` rows, cargo feature; then the sizing-run mode in
`ci/bench_hole_purging_allocators.py`, regenerated SVGs, README feature-table cell
"✅ with `MI_OWNER_GATE=ON` (N %); ⚠️ default build: parked threads + arenas", README API
table row, `docs/purge-all.md` (user-facing contract: what `wait_ms` bounds and does not,
`PARTIAL` as a normal outcome, hook attribution, the gated fast-path cost with the bench
number).

**Phase 3 (optional, needs phase 1's bench number)** — asymmetric gate: owner side becomes
a plain store + compiler barrier + plain load; the sweeper pays the fence via
`membarrier(MEMBARRIER_CMD_PRIVATE_EXPEDITED)` / `FlushProcessWriteBuffers()` (macOS:
`mprotect` trick). Drop-in behind the same two macros; only worth it with a number.

## 13. Contract summary and accepted limits

- Default build: fast path byte-identical (checked); `mi_purge_all` reaches arenas,
  abandoned pages, the caller, parked threads; everything else is reported pending.
- Gated build: every registered thread is reachable; a `RUNNING` owner is waited for up to
  `wait_ms` then reported pending. `FORCE` ignores pacing and lets a claimed sweep run to
  completion (the owner stalls for it); it does not mean "wait forever".
- A thread blocked inside the allocator (syscall, or a callback waiting on the caller)
  delays or defeats acquisition; reported, never hung on.
- Fork orphans are never swept by this path.
- Hook/profiler bookkeeping from a foreign sweep is attributed to the purging thread.
- Two purges cannot overlap; the second returns `BUSY` having done nothing.
- The gated fast path is slower by a measured amount; that is the product.
