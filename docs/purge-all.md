# `mi_purge_all` — process-wide purge from any thread

`mi_purge_all` returns as much memory as the allocator's invariants allow across **all**
threads' heaps — arenas, abandoned pages, and every thread's own pages and holes — from
one thread, and reports exactly what it could not reach. Implementation issue:
[#366](https://github.com/zackees/mimalloc-pprof/issues/366); design record:
[#335](https://github.com/zackees/mimalloc-pprof/issues/335); background:
[#365](https://github.com/zackees/mimalloc-pprof/issues/365). This document is the
user-facing contract. How it works inside is
[docs/purge-all-implementation.md](purge-all-implementation.md).

## Two builds, two contracts

| build | what `mi_purge_all` reaches | fast-path cost |
|---|---|---|
| default (`MI_OWNER_GATE=OFF`) | arenas, abandoned pages, the caller's own heaps and holes, and threads parked in `mi_on_thread_idle_start()`. Every other thread is reported **pending**. | none — `mi_malloc`/`mi_free` are byte-identical to a tree without the feature (`ci/check_fastpath_identity.py` checks this in CI) |
| `MI_OWNER_GATE=ON` (CMake option; Rust cargo feature `owner-gate`) | all of the above **plus every registered thread, busy or not**: a thread is claimed between two of its allocator calls and swept by the purging thread while it briefly stalls | one CAS + one release-store per outermost allocator call, plus one TLS load on `free` — see [the measured cost](#the-cost-of-the-gate) |

The default build is the product for everyone who never asked for a cross-thread purge.
The gated build is for clients that trade allocation speed for RSS return — long-lived
servers with an "idle now, give it back" moment that cannot rely on every worker thread
reaching an idle point.

The gate also changes the background scavenger: in a gated build its timed sweep reaches
busy threads too (paced by `purge_holes_min_interval`, bounded per visit by
`park_reclaim`), so some memory comes back even when nobody calls anything.

## API

```c
typedef enum mi_purge_flags_e {
  MI_PURGE_FORCE = 1,   // ignore purge_delay / hole-purge pacing; a claimed sweep runs to completion
} mi_purge_flags_t;

typedef struct mi_purge_all_report_s {
  size_t arena_bytes;        // returned to the OS by the arena passes
  size_t hole_bytes;         // returned by hole purging (every swept heap + abandoned pages)
  size_t theaps_swept;       // per-thread heaps claimed and swept by this call (caller included)
  size_t theaps_pending;     // registered threads not reached within wait_ms
  size_t theaps_orphaned;    // pre-fork threads that vanished in a fork child; never touched
  bool   gated;              // built with MI_OWNER_GATE (a configuration fact, not completion)
  bool   complete;           // theaps_pending == 0 && theaps_orphaned == 0
} mi_purge_all_report_t;

#define MI_PURGE_OK       0
#define MI_PURGE_PARTIAL  1
#define MI_PURGE_BUSY     2

int  mi_purge_all_ex(mi_purge_flags_t flags, size_t wait_ms, mi_purge_all_report_t* report);
void mi_purge_all(bool force);   // == mi_purge_all_ex(force ? MI_PURGE_FORCE : 0, 100, NULL)
```

Rust: `mimalloc_pprof::purge_all(force) -> PurgeAllReport` and
`purge_all_ex(flags, wait_ms) -> (PurgeStatus, PurgeAllReport)`; the FFI names are
`sys::mi_purge_all` / `sys::mi_purge_all_ex`.

### Return codes

| code | meaning | what was done |
|---|---|---|
| `MI_PURGE_OK` | every registered thread was reached (`theaps_pending == 0`) | everything |
| `MI_PURGE_PARTIAL` | some owners were still inside the allocator when `wait_ms` ran out (`theaps_pending > 0`) | everything reachable was purged — the arenas, abandoned pages, the caller, and every thread that was claimed |
| `MI_PURGE_BUSY` | another purge is in flight on some thread, or this is a re-entrant call from inside an allocator callback (a deferred-free handler, an output hook) while one runs on this thread | **nothing**; the report is untouched |

`report` may be `NULL`; the status is the return value either way. `complete` is
`pending == 0 && orphaned == 0` — stricter than `OK`, which ignores orphans.

### What `wait_ms` bounds — and what it does not

`wait_ms` bounds **owner acquisition only**: how long the call keeps retrying to claim
threads that are inside an allocator call at the moment it looks. It does not bound

- the sweep of a thread once it has been claimed — that runs to its end;
- the `madvise`/`VirtualAlloc(MEM_RESET)` calls those sweeps and the arena passes make;
- the caller's own collect (phase C).

So the wall time of a call is `wait_ms` plus the cost of the work it actually did. A
thread that is blocked *inside* the allocator — in a syscall, or in a callback that
waits on a mutex the purging thread holds — cannot be claimed; it ends up pending when
the deadline passes. The library never waits unboundedly and never claims
deadlock-freedom for a callback that waits on its caller.

In the default build no busy thread will ever park on its own, so every `RUNNING`
thread is reported pending on the first pass and `wait_ms` is not consumed.

### `PARTIAL` is a normal outcome

Under load some thread is always inside the allocator when the walk looks. A client
that wants everyone should loop, not treat `PARTIAL` as an error:

```c
mi_purge_all_report_t r;
int status;
do {
  status = mi_purge_all_ex(MI_PURGE_FORCE, 20, &r);
} while (status == MI_PURGE_PARTIAL && r.theaps_pending > 0 && --attempts > 0);
```

Each iteration re-walks the registry; threads swept last time are cheap to sweep again
(their free lists are already drained), and the ones that were pending get a fresh
`wait_ms`. Threads created *during* a walk are excluded from that walk (registry
cutoff), so thread churn cannot extend a call; the next iteration sees them.

### `MI_PURGE_FORCE`

- The arena passes ignore `purge_delay` and purge now.
- The hole sweep ignores `purge_holes_min_interval` pacing.
- A **claimed sweep runs to completion**: the per-visit `park_reclaim` budget the
  scavenger honours is ignored, and the claimed thread's owner stalls at its next
  allocator call until the sweep finishes. The stall is the cost of reaching a busy
  thread; `test-purge-all` (case G2) measures and prints the worst single-call stall a
  busy worker sees during a forced purge — see the number below.
- `FORCE` never means "wait forever": `wait_ms` still bounds acquisition.

Without `FORCE`, pacing applies and a sweep is bounded by `park_reclaim`, exactly as
when the scavenger visits.

### Fork orphans

After `fork()`, the child inherits the per-thread state of every parent thread that no
longer exists. Those are **orphans**: `mi_purge_all` counts them in `theaps_orphaned`,
never parks them and never sweeps them (their pages are reclaimed through the existing
fork-child mechanisms). A child that forked while a purge was in flight starts with a
clear admission slot, so it is never permanently `BUSY`.

### Hook and profiler attribution

A foreign sweep runs on the *purging* thread. Memory-events and profiler bookkeeping it
emits — page purges, hole discards, the per-thread counters — are attributed to the
purging thread, not to the thread whose heap was swept. Output hooks resolve to the
purging thread's hook state.

### `BUSY`

Two purges cannot overlap; the second returns `BUSY` having done nothing, and the same
holds for a re-entrant call from an allocator callback on the purging thread. A `BUSY`
caller that wants the work done should retry later, not spin: the purge in flight is
doing the same work.

## Measured reach: 4 busy threads, purge from another thread

The number that matters for "from any thread" is the case where the threads holding
the memory never cooperate. `ci/bench_hole_purging_allocators.py --busy-threads 4`
runs the README's churn workload (300k blocks, a scattered 1-in-20 kept alive, the
rest freed) split across 4 worker threads that then stay in a `malloc`/`free` loop and
never call any idle hook, while the main thread — which allocates nothing — calls the
purge every 100 ms for 10 s. Best of 3 runs, `taskset -c 0-3`, AMD Ryzen 7 3700X,
Linux 6.18.48, at commit `c7d4810d`:

| allocator | what the purging thread calls | peak RSS | after 10 s | returned |
|---|---|---:|---:|---:|
| mimalloc-pprof, `MI_OWNER_GATE=ON` | `mi_purge_all(true)` → `OK` (at most 1 pending on any tick) | 254.9 MB | 72.2 MB | **72 %** |
| mimalloc-pprof, default build | `mi_purge_all(true)` → `PARTIAL`, 4 pending | 263.0 MB | 230.6 MB | 12 % |
| mimalloc-pprof, default build | `mi_collect(true)` | 266.9 MB | 230.4 MB | 14 % |
| Bun mimalloc | `mi_collect(true)` | 267.0 MB | 230.4 MB | 14 % |
| upstream mimalloc | `mi_collect(true)` | 264.9 MB | 228.9 MB | 14 % |
| jemalloc | `mallctl("arena.<all>.purge")` | 283.0 MB | 73.3 MB | 74 % |
| glibc malloc | `malloc_trim(0)` | 277.7 MB | 76.5 MB | 72 % |
| mimalloc-pprof, default build | nothing (scavenger only) | 255.0 MB | 230.6 MB | 10 % |
| mimalloc-pprof, `MI_OWNER_GATE=ON` | nothing (scavenger only) | 274.9 MB | 126.9 MB | 54 % (best of 3; the others 45 % and 47 %) |

Reading it: with the gate on, `mi_purge_all` from a non-allocating thread reaches the
same ~72–74 % as jemalloc's and glibc's any-thread purges. Without it, the call is
honest about the gap — `PARTIAL` with all four workers pending — and returns what the
arenas and the caller can give, which is the same ~12–14 % every `mi_collect(true)`
manages. The gated scavenger-only control is the timed sweep reaching busy threads on
its own, paced by `park_reclaim`, which is why it lands between the two.

The full per-run data is in `.github/assets/allocator-purge-any-thread-report.json`;
the table image the README shows is rendered from it by the same script
(`--check --busy-threads` verifies the two agree).

## The cost of the gate

`uv run ci/dev_linux.py bench` is the docker dev-loop timing (cold/warm build and
ctest wall time), not an allocator throughput measurement, so the gate's fast-path cost
was measured directly: this tree built Release (`-DMI_PPROF=ON`, static library) twice,
`MI_OWNER_GATE=OFF` and `ON`, on the machine above.

- **Single-thread `mi_malloc`+`mi_free` pair**, a 64-slot ring of 16–512 B blocks,
  10⁸ iterations per run, 12 runs per build pinned to one core:
  OFF **25.3 ns min / 29.5 ns median**, ON **34.8 ns min / 38.0 ns median** — about
  **+9 ns per pair (+30 %)**. Run-to-run spread on this machine is ±4 ns for the same
  binary, so read the minima.
- **`mimalloc-test-stress 4 50 50`** (4 threads, `taskset -c 0-3`, 10 runs each):
  OFF 0.40 s min / 0.47 s median, ON 0.35 s min / 0.38 s median — inside the noise of
  that workload, which is dominated by page faults and cross-thread frees rather than
  the fast path.

The gated fast path is slower by a measured amount; that is the product. The default
build pays nothing, and CI proves it by disassembly.

The other cost is the owner's stall while a forced sweep of its heap runs on the
purging thread. On the same machine, `mimalloc-test-purge-all` from the gated Release
build reports for its G1/G2 case (4 busy workers, `mi_purge_all_ex(MI_PURGE_FORCE,
2000, &r)` from main): `OK` in 38.1 ms, `swept=5 pending=0`, and a **worst single
allocator call of 12.0 ms** on a worker during the purge.

## How it works, briefly

The three-state per-thread park protocol the scavenger already uses (`RUNNING` /
`PARKED` / `SWEEPING`) is the whole lock. In the default build a thread is `RUNNING`
except inside `mi_on_thread_idle_start()…_end()`; in a gated build it is `PARKED`
whenever it is outside the allocator and `RUNNING` only while inside, so a foreign
sweeper can claim it with the scavenger's existing CAS between any two calls.
`mi_purge_all_ex` purges the arenas, purges abandoned pages, collects the caller, walks
the thread registry claiming whatever is `PARKED` until `wait_ms` runs out, then runs one
final arena pass so pages freed by the sweeps leave immediately. Every detail — the
enter/leave sites, the leaf assertions that prove coverage, the fork rules, the tests —
is in [docs/purge-all-implementation.md](purge-all-implementation.md).
