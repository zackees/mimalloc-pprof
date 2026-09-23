# Reclaiming idle arenas after an allocation peak

Status: design proposal, based on [PR #432](https://github.com/zackees/mimalloc-pprof/pull/432)
and its predecessors [#428](https://github.com/zackees/mimalloc-pprof/pull/428) and
[#429](https://github.com/zackees/mimalloc-pprof/pull/429). This document describes the
submitted implementation, the parts worth retaining, and the changes required before
integration. It does not claim that arena reclamation is implemented on `main`.

Related work: [#366](https://github.com/zackees/mimalloc-pprof/issues/366) introduced
`mi_purge_all_ex` and owner gating. See [purge-all.md](purge-all.md) for its current
contract and [purge-all-implementation.md](purge-all-implementation.md) for the existing
walk and lock protocol.

## Decision sought

Develop an explicit operation that releases *allocator-created*, completely empty arenas
at a caller-chosen quiescent point. Retain the contributor's central approach: run after
the ordinary process-wide purge, claim every other thread in a subprocess, clear all
references to an empty arena, and release its OS reservation. Do not merge the submitted
patch as written. It can invalidate a caller-owned `mi_arena_id_t`, and its added report
fields are incompatible with an older caller of the shared-library ABI.

Success means that a long-running service can reduce its allocator-owned committed
metadata and reserved address space after a large temporary allocation peak. It does
not mean that every purge must shrink RSS: live pages, caller-owned arenas, unclaimable
threads, and OS reset semantics may keep physical memory or accounting unchanged.

## Why the existing purge is insufficient

`mi_arena_reserve` in `src/arena.c` creates an arena when existing arenas cannot satisfy
an allocation. The arena begins with an info block containing its header, slice bitmaps,
and page metadata. `_mi_arenas_free` makes data slices available; later purging can
decommit those slices. Neither operation removes the arena header, its metadata, or the
OS reservation. The existing `mi_arena_unload` code is a compiled-out sketch, and
`mi_arenas_unsafe_destroy` is a teardown path.

The result is peak-dependent allocator overhead. PR #432's focused test made four 96 MiB
arenas and measured a 384 MiB increase in reserved address space and roughly 1.5 MiB
of arena metadata. Its reclaim path released the four reservations and reduced metadata
to roughly 0.2 MiB. These are examples from that test configuration, not a universal
bytes-saved prediction. Most of the 384 MiB can be virtual address space on Linux;
committed metadata and Windows commit behavior are the stronger production motivations.

The operation should remain opt-in. Releasing warm arenas can make the next allocation
spike slower because their reservations and metadata must be created again. A call at a
known idle boundary lets the application choose that tradeoff.

## What the submitted code contributes

PR #432 changes C, Rust, the Rust-vendored C amalgamation, documentation, and tests in
two commits. Its proposed `MI_PURGE_RECLAIM` flag runs a new phase after the existing
`mi_purge_all_ex` walk. `src/arena-reclaim.c` contains the reclaim machinery; the new
Rust flag and report fields mirror the C surface.

The useful core of its eligibility check is a bitmap test over *data* slices. The info
slices are permanently occupied by the arena itself and must be excluded:

```c
/* From PR #432, shortened for the design discussion. */
return mi_bbitmap_is_xsetN(MI_BIT_SET, arena->slices_free,
                           arena->info_slices,
                           arena->slice_count - arena->info_slices);
```

The patch then claims every other registered TLD in the subprocess, holds the registry
and heap locks, clears per-heap arena tracking, unregisters the page-map prefix, removes
the arena slot, adjusts committed accounting, and calls `_mi_os_free_ex`. It also retries
within the caller's `wait_ms` budget if a thread is temporarily inside the allocator.

| Submitted location | Useful responsibility | Required revision |
| --- | --- | --- |
| `src/arena-reclaim.c` | Quiescence, empty-arena check, cleanup, OS release | Require allocator-owned provenance; audit teardown and status paths |
| `src/arena.c`, `include/mimalloc/types.h` | Arena creation and ownership data | Mark auto-reserved versus caller-reserved arenas at creation |
| `src/purge-all.c` | Admission and post-purge integration | Return a result that covers requested reclamation |
| `include/mimalloc.h` | Flag and report | Use an ABI-safe extension rather than growing the old report |
| `rust/mimalloc-pprof/` | Safe wrapper, raw bindings, ABI checks | Mirror the final C ABI and keep C/Rust commits separate |
| `test/test-arena-reclaim.cpp` | Accounting, owner, and slot-reuse tests | Add retained-ID and old-ABI regressions |

## Ownership: the release boundary

`mi_memkind_is_os` means the arena's *memory* came from the OS. It does not mean the
allocator created the arena for its own expansion. Both `mi_arena_reserve` and the public
`mi_reserve_os_memory_ex` call the same internal reservation function and can create
non-exclusive OS-backed arenas. The submitted predicate therefore admits a caller's
non-exclusive arena:

```c
/* PR #432: necessary checks, but insufficient ownership information. */
return mi_memkind_is_os(arena->memid.memkind) &&
       arena->parent == NULL && !arena->memid.is_pinned &&
       !arena->is_exclusive;
```

`exclusive` controls where allocations may be placed. It is not proof of who owns the
arena or whether a caller still holds its ID. The public `mi_arena_id_t` is a pointer to
arena state; `mi_arena_area(id, ...)` dereferences it. In a direct ASan reproduction,
reserve a non-exclusive 64 MiB arena with an ID, call the submitted reclaim flag, then
call `mi_arena_area(id, &size)`: the final call faults in `src/arena.c` after the arena
has been unmapped. The submitted source comment acknowledges this lifetime problem.

Record creation provenance in the arena, separately from `is_exclusive` and `memid`.
The implementation shape below is illustrative C, not a complete patch:

```c
typedef enum mi_arena_origin_e {
  MI_ARENA_CALLER_RESERVED,
  MI_ARENA_AUTO_RESERVED,
  MI_ARENA_CALLER_MANAGED
} mi_arena_origin_t;

/* Set only when mi_arena_reserve creates an arena for allocator expansion.
   Public reservation and management entry points never set AUTO_RESERVED. */
arena->origin = origin;

static bool mi_arena_reclaim_is_ours(const mi_arena_t* arena) {
  return arena->origin == MI_ARENA_AUTO_RESERVED &&
         mi_memkind_is_os(arena->memid.memkind) &&
         arena->parent == NULL && !arena->memid.is_pinned &&
         !arena->is_exclusive;
}
```

The `origin` value must be assigned before publishing the arena in
`subproc->arenas[]`, preserved for any child/sub-arena creation, and initialized on every
creation and fork path. Passing an explicit origin through the shared internal reserve
function is preferable to inferring it from a non-null `arena_id`: public callers can
request an arena without retaining an ID, and internal callers may request one.

Contract: a caller-reserved or caller-managed arena is never reclaimed by this global
operation, regardless of `exclusive` and regardless of whether the caller requested an
ID. A separate explicit release API would need its own ownership and lifetime rules.
This restriction may reduce the number of eligible arenas, but it makes existing public
arena handles safe.

## Concurrency and release sequence

An empty arena can still be observed by a concurrent allocation scanning
`subproc->arenas[]`, or by a background purge reading its bitmaps. The empty bitmap alone
does not make the arena safe to unmap. The submitted coordination protocol is the right
starting point, subject to a dedicated lock-order and lifetime review:

```text
existing mi_purge_all_ex admission
  -> ordinary page/hole purge walk
  -> acquire arena purge guard (excludes background arena purge)
  -> hold subprocess list lock
  -> for each subprocess:
       hold heaps_lock, then tlds_lock
       claim every other registered TLD: PARKED -> SWEEPING
       verify arena is allocator-owned and all data slices are free
       detach non-main and main-heap arena tracking
       hold theap_meta_lock (closes pre-registration metadata allocation)
       recheck eligibility under the final locks
       unregister page-map range; unpublish arena slot
       debit only the committed bytes still credited to this arena
       free OS reservation; release claims and locks
```

The exact lock nesting must be checked against `src/fork.c`, owner teardown, thread
bootstrap, and scavenger paths. No lock body may `return` through the `mi_lock` macro's
release expression. The caller's TLD is excluded from the claim because it runs the
operation; in an `MI_OWNER_GATE` build it must hold its own gate throughout the phase.
On failure to claim any other TLD, release partial claims and leave that subprocess
untouched. Orphaned TLDs also prevent a release.

With `MI_OWNER_GATE=ON`, a thread outside an allocator call is parked automatically.
With the default owner gate off, another thread must explicitly enter its idle park
before it can be claimed. This is a safety property and a product limitation: a service
with permanently running worker threads may get no arena release until it coordinates
an idle point. The API must report that outcome.

Before freeing an arena, clear both its per-heap bitmap allocations and the main heap's
pointer to `arena->pages_main`. These pointers otherwise become stale when an arena slot
is reused; the next arena in that slot may also be a different size. Unregister the
accessed page-map range and remove the arena from `subproc->arenas[]` before OS free.
The arena count can shrink only at the trailing slot. Audit the order in which external
readers can see these transitions, including failed OS release and fork paths.

The committed-statistic debit is distinct from the reservation size. On an
overcommitting OS, reservation accounting can be canceled at creation and data slices
credited later; after decommit, dirty bits may remain set. On Windows, reset can leave
data slices committed. The implementation must debit only the arena's outstanding
credit, never the full reserved size by default. Assert nonnegative accounting and
test decommit and reset modes separately.

## Public API and ABI

The feature needs an explicit request and a report of what happened. The submitted
`MI_PURGE_RECLAIM` flag is reasonable for source-level ergonomics, but PR #432 appends
fields to `mi_purge_all_report_t` without a size parameter. An older binary can allocate
the old struct and call a newer shared library, which would write the new fields past
its buffer. Rust layout tests prove that two *new* definitions agree; they do not protect
old binaries.

Keep the old `mi_purge_all_ex` symbol and its report size unchanged. One possible ABI-safe
extension is a new symbol with a sized report:

```c
typedef struct mi_purge_all_report_v2_s {
  size_t size;                 /* caller sets sizeof(report) */
  mi_purge_all_report_t purge; /* existing report, unchanged */
  size_t arenas_reclaimed;
  size_t arena_reclaim_bytes;
  size_t arenas_kept;
  size_t subprocs_pending;
  bool reclaim_complete;
} mi_purge_all_report_v2_t;

int mi_purge_all_ex2(mi_purge_flags_t flags, size_t wait_ms,
                     mi_purge_all_report_v2_t* report);
```

This is a proposed shape, not a fixed name or signature. It must define accepted `size`
values, zero-initialization, null-report behavior, and whether short future versions
can be written by prefix length. The old symbol must never write beyond the original
struct. If the old flags enum receives `MI_PURGE_RECLAIM`, decide whether old callers
may request it without the detailed report; document that choice explicitly.

The result must describe *both* operations. The submitted implementation can return
`MI_PURGE_OK` when the ordinary walk succeeds but the requested reclaim phase has
`subprocs_pending > 0` or a busy arena layer. For the new entry point, use `OK` only if
the requested phases complete; use `PARTIAL` for work done with a pending phase and
`BUSY` when admission prevents any work. `reclaim_complete` means the phase ran without
an unvisited subprocess or busy arena layer; zero reclaimed arenas is still a complete
run. `_mi_os_free_ex` currently returns `void`, so an OS-release failure cannot be
reported separately without changing that lower-level contract.

Rust should expose a result that carries both the general purge result and the reclaim
report, call the new C symbol, and assert the final C/Rust layout through
`layout_probe.c` and `t19_layout.rs`. The public surface must remain available in every
build configuration. The Rust-vendored header and amalgamation must be regenerated from
the final C sources.

## Validation plan

The submitted tests cover useful cases: an allocation peak, ordinary purge as a
control, live objects surviving the pass, parked and running owners, accounting, and
arena slot reuse. Before integration, add the following targeted proofs:

1. Retain IDs from `mi_reserve_os_memory_ex` with both `exclusive=false` and `true`;
   reclaim and then successfully use `mi_arena_area` and `mi_heap_new_in_arena`.
2. Preserve arenas from `mi_reserve_os_memory` without an ID and from
   `mi_manage_os_memory_ex`; never return caller-managed memory to the OS.
3. Reclaim an allocator-created empty arena in the same process as a caller arena;
   verify that provenance does not disable useful reclamation.
4. Exercise a reader, a running owner, a cooperatively parked owner, a registering
   thread, a scavenger, a fork child, and heap destruction around candidate selection.
5. Test decommit and reset-style purge accounting on Linux and Windows; check exact
   reservation deltas, outstanding committed credit, and slot reuse.
6. Compile an old-header client against the new shared library, call the old entry
   point with a guarded report buffer, and verify no write beyond the old struct.
7. Check all required C and Rust gates: Linux, native Microsoft `cl`, win-gnu,
   `pprof-off`/minimal, the cross-built macOS bundles, and the repository's sanitizer
   and layout checks. Run the manual macOS guest only if macOS-specific paths change.

PR #432's three focused reclaim tests passed locally in Debug, in a 100-repeat Release
run without owner gating, and in a 30-repeat GCC/ASan run. That is useful preliminary
evidence. The complete required CI matrix has not run on the submitted head because
its Actions runs await approval; the results above do not substitute for those gates.

## Production measurements and acceptance

Run the same burst-and-idle workload before and after integration, with at least two
arena sizes and multiple worker counts. Record the following separately:

| Measurement | Why it matters |
| --- | --- |
| OS reserved bytes and `arena_reclaim_bytes` | Confirms address ranges were released |
| Committed bytes and process RSS or Windows private bytes | Shows actual memory pressure relief |
| Reclaim latency and pending subprocess count | Shows pause cost and whether the operation succeeds in normal service states |
| Time and allocations during the next burst | Quantifies the cost of losing warm arenas |
| Owner gate on versus off | Shows whether cooperative parking is sufficient for target deployments |

Treat the submitted test's memory reduction as evidence of feasibility, not a promised
production percentage. Accept the feature when it returns allocator-owned metadata
after a representative peak, preserves all caller arena handles, keeps accounting
correct across supported OS purge modes, and does not affect the default allocation
fast path. The `MI_PURGE_RECLAIM` branch should execute only in the explicit purge call.

## Implementation sequence

1. Agree on the public ABI and the exact ownership invariant in an issue before code
   changes. Record the initial ASan retained-ID reproduction there.
2. Add provenance to arena construction, with a regression test for every public
   reservation path. Preserve these tests when adapting PR #432.
3. Integrate the submitted quiescence and release code after a focused concurrency,
   lock-order, accounting, and failure-path review.
4. Add the new C API, then the Rust API and generated vendor files in a separate commit.
5. Run the required CI matrix and production-style measurements before merge.

This sequence retains the contributor's substantial implementation work while making
arena ownership and binary compatibility explicit acceptance conditions.
