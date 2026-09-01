# Profiler reference

*Part of the [mimalloc-pprof](../README.md) documentation.*

## What it costs when it is not running

Measured, so you can decide whether to ship `MI_PPROF=ON`. Windows/MinGW, Release,
4M alloc/free pairs single-threaded, both arms built from the same commit and run
interleaved, minimum of four runs of three reps each:

| build | ns per allocation | within-arm spread |
|---|---|---|
| `MI_PPROF=OFF` | **11.75** | 4% |
| `MI_PPROF=ON`, profiler stopped | **20.00** | 14% |

**About +70% per allocation with the profiler switched off.** That is the cost of the
unconditional `_mi_prof_on_alloc` call on the allocation fast path, which then checks an
atomic flag and returns. Single-threaded on purpose — this is per-allocation instruction
count, not lock contention (contention was a separate defect, fixed in #152).

This is larger than it should be and is a known gap rather than a design choice. Bun's
fork takes a different approach: it leaves the fast path completely untouched and, when
profiling is switched on, poisons `pages_free_direct` so allocations divert into the
already-cold `_mi_malloc_generic`, where the sampling check lives. That is strictly
better when disabled, which is the common case for a shipping build. Tracked in #50.

Until then: if allocation throughput matters more to you than being able to turn
profiling on at runtime, build with `MI_PPROF=OFF`. Enabling the profiler at runtime
costs more again, but the sampling decision itself is now lock-free (#152).

## Environment variables

Set these before process launch to capture allocations made during startup,
before `main` runs:

```sh
MIMALLOC_PROF=1 \
MIMALLOC_PROF_DUMP_AT_EXIT=heap.prof \
MIMALLOC_PROF_SAMPLE_INTERVAL=524288 \
./my_app
```

| Setting | Meaning |
|---|---|
| `MIMALLOC_PROF=1` | Start the profiler automatically at process start |
| `MIMALLOC_PROF_DUMP_AT_EXIT=path` | Write a profile at exit |
| `MIMALLOC_PROF_SAMPLE_INTERVAL=N` | Bytes between samples (default ~512 KiB) |
| `MIMALLOC_PROF_ACCUM=1` | Keep cumulative counters until `mi_prof_reset` |
| `MIMALLOC_PROF_BT_MAX=32` | Maximum captured stack depth (compile-time cap 128) |
| `MIMALLOC_PROF_MAX_BYTES=N` | Bound persistent profiler arena memory |
| `MIMALLOC_PROF_SEED=N` | Deterministic sampling, for repeatable tests (see the note below) |
| `MIMALLOC_PROF_DUMP_FORMAT=proto` | Write pprof `profile.proto` instead of text |

`MIMALLOC_PROF_SAMPLE_RATE` remains a compatibility alias for
`MIMALLOC_PROF_SAMPLE_INTERVAL`; when both are set, `..._INTERVAL` wins.

**What `MIMALLOC_PROF_SEED` guarantees.** Two runs of the same workload with the same
seed sample at the same points, **provided the threads are created in the same order** —
each thread's stream is derived from the seed and its creation ordinal. It does not make
a workload whose threads race to allocate reproducible, because which thread reaches a
given allocation first is still nondeterministic. Single-threaded and
deterministic-startup workloads are fully repeatable.

Until 0.9.1 this was not true at all: the per-thread stream mixed in the *address* of the
thread's allocator state, which ASLR randomises, so seeded runs differed in every
process ([#91](https://github.com/zackees/mimalloc-pprof/issues/91)).

## If your process contains more than one mimalloc

Environment variables are process-global, and **mimalloc is often embedded in
libraries you did not choose to load** — NVIDIA's display driver ships mimalloc 3.1.6,
for instance. A process can therefore end up with our build *and* a stock one, each
with its own options table, both reading the same `MIMALLOC_*` environment.

We evaluated namespacing our additions — `MIMALLOC_PPROF_*` instead of
`MIMALLOC_PROF_*` — and **decided against it**, because it does not address the actual
hazard:

- **Our additions are already inert to a stock mimalloc.** `mi_option_init` looks
  options up **by name** (`_mi_getenv("mimalloc_" + option_name)`); nothing enumerates
  the environment. A stock build never asks for `mimalloc_prof_sample_rate` or
  `mimalloc_memory_events`, so it does not see them, does not warn, and does not
  misbehave. Renaming them would prevent a collision that cannot occur.
- **The real collision is on upstream's own option names**, and it is inherited rather
  than introduced by us. `MIMALLOC_VERBOSE`, `MIMALLOC_SHOW_STATS`,
  `MIMALLOC_PURGE_DELAY` and friends are read by *every* mimalloc in the process, so
  setting one to debug our allocator also reconfigures the embedded one. Renaming *our*
  options does nothing about that — and we cannot rename upstream's without ceasing to
  be a drop-in replacement.

So: no namespacing. It would break every 0.9.x user's configuration to solve a problem
that does not exist, while leaving the one that does.

**What to do if it bites you.** There is no per-instance environment scoping in
mimalloc. Configure our instance through the API instead — `mi_prof_start_ex`,
`mi_option_set` — and leave the environment alone; API calls affect only the instance
you call them on. If you need the environment for startup-time capture, be aware it is
process-wide.

**If you are embedding this library and need to be immune to the ambient environment,
do not rely on the variable names — use `MI_PROF_CONFIG_OVERRIDE`:**

```c
#include <mimalloc.h>
#include <mimalloc/profile.h>

mi_prof_config_t_decl(cfg);           /* zeroed, with size + version filled in */
cfg.mode = MI_PROF_CONFIG_OVERRIDE;   /* struct wins over env, field by field */
cfg.sample_interval = 2048;
if (!mi_prof_start_ex(&cfg)) return 1;
```

The default mode, `MI_PROF_CONFIG_FALLBACK`, is the opposite: env wins and the struct
only fills gaps, so ops can tune a shipped binary without a rebuild. Both directions are
covered by the test suite for every env-backed field, not just some of them.

One asymmetry to know about: because `0`/`NULL` doubles as "field not set", `OVERRIDE`
cannot force `accum` *off* or `max_profiler_bytes` back to unbudgeted — those fall
through to env-then-default. It can force them on.

## Allocator statistics in the profile (v3 only)

v3 exposes per-heap and per-"subprocess" counters (`mi_heap_stats_get`,
`mi_subproc_stats_get`) that v2 had no API for.

> **"Subprocess" does not mean an OS child process.** Despite the name, a mimalloc
> `mi_subproc_t` is an **in-process partition**: a walled-off set of arenas and heaps
> inside one OS process, whose threads never share or reclaim pages across the wall.
> Upstream added it so an embedder like CPython can quarantine each subinterpreter's
> allocations within a single process. Nothing spans OS process boundaries — an
> actual child process gets its own independent allocator, as always. Every process
> starts with exactly one default partition, so unless you call `mi_subproc_new`,
> "per-subprocess" totals and process-wide totals are the same numbers.

The profiler surfaces these counters in two places:

**1. `mi_prof_stats_t` v3 fields** (`MI_PROF_STAT_VERSION` 3) — `heap_committed`,
`heap_reserved`, `heap_malloc_requested`, `heap_pages`, `heap_pages_abandoned`,
`heap_count`, `theap_count`, `heap_purged`. In Rust these are `ProfStats::heap`,
a `HeapStats` struct.

**2. A comment block in the text dump**, after the samples and before
`MAPPED_LIBRARIES:`. google/pprof's legacy heap parser skips `#` lines, so
existing tooling reads the profile unchanged:

```text
# mimalloc heap stats
# committed = 3080192
# reserved = 5308416
# malloc_requested = 2097152
# pages = 43
...
```

**Why this matters.** Everything else in `mi_prof_stats_t` is *sampled*; these
fields are **exact**. A sampled profile alone cannot tell you whether it
under-counted, but comparing `heap_malloc_requested` against `live_bytes` measures
the sampling error directly — which is what makes an assertion on a sampled
profile meaningful in tests and in production monitoring.

`mi_prof_stats_get` still accepts v1- and v2-sized structs from older callers and
leaves the newer fields untouched, so upgrading the header does not break an
existing binary.

Two counters carry caveats:

- **`heap_malloc_requested` requires `MI_STAT >= 2`.** Upstream enables that level
  by default only for debug builds; a default **release** build has `MI_STAT == 0`
  and reports 0. Because 0 is also a legitimate value,
  `mi_prof_stats_t.heap_stats_detailed` (Rust: `HeapStats::detailed`) tells you
  which case you are in, and the dump records `# detailed_stats = 0|1` so a saved
  profile is self-describing. Build with `-DMI_STAT=2` to enable it in release, at
  some allocation-path cost.
- **`theap_count` excludes the main thread's statically-initialized theap**, so a
  single-threaded process reports 0. It also lags thread exit, since v3
  reference-counts theaps for cached thread-locals.
