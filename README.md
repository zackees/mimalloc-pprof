# mimalloc-pprof

> ## mimalloc with native pprof-compatible heap profiling — on Windows, Linux, and macOS alike.

> **The one mimalloc heap profiler that runs natively on Windows.** Upstream mimalloc
> has no profiler at all, and the only other known implementation
> ([Bun's](https://github.com/oven-sh/mimalloc), surveyed in
> [`MIMALLOC_FORKS.md`](MIMALLOC_FORKS.md)) is POSIX-only — its stack capture is guarded
> behind glibc/Apple `<execinfo.h>`.

A fork of [microsoft/mimalloc](https://github.com/microsoft/mimalloc) that adds
**pprof-compatible sampled heap profiling**, with native Windows as a first-class
target alongside Linux and macOS.

```
   __  __ ___ __  __    _    _     _     ___   ____
  |  \/  |_ _|  \/  |  / \  | |   | |   / _ \ / ___|
  | |\/| || || |\/| | / _ \ | |   | |  | | | | |
  | |  | || || |  | |/ ___ \| |___| |__| |_| | |___
  |_|  |_|___|_|  |_/_/   \_\_____|_____\___/ \____|

   ____  ____  ____   ___  _____
  |  _ \|  _ \|  _ \ / _ \|  ___|
  | |_) | |_) | |_) | | | | |_
  |  __/|  __/|  _ <| |_| |  _|
  |_|   |_|   |_| \_\\___/|_|

      PPROF-COMPATIBLE SAMPLED HEAP PROFILING
      WINDOWS FIRST-CLASS | LINUX | MACOS


    malloc / free
          |
          v
          +------------------+
          |     mimalloc     |
          |   [ live heap ]  |
          +---------+--------+
                    |
                    | sampled allocations
                    v
+------------------+      +--------------------------+
|    heap.prof     | ---> |      google/pprof        |
| heap_v2 / proto  |      | flamegraphs | top | diff |
+------------------+      +--------------------------+
```

The allocator tracks sampled live allocations and writes either the gperftools
`heap_v2` text format or an uncompressed pprof `profile.proto`. Both open directly
in [google/pprof](https://github.com/google/pprof) for flame graphs, call graphs,
top reports, and profile diffs.

Profiling is **opt-in at runtime**: a build with `MI_PPROF=ON` (the default) does
not sample until you call a start API or set `MIMALLOC_PROF=1`.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset=".github/assets/star-history-dark.svg" />
  <source media="(prefers-color-scheme: light)" srcset=".github/assets/star-history-light.svg" />
  <img alt="Star history for zackees/mimalloc-pprof" src=".github/assets/star-history-light.svg" width="100%" />
</picture>

**Contents**

- [Quick start](#quick-start)
- [Performance](#performance) — continuous benchmarks vs. upstream mimalloc, TCMalloc, and jemalloc
- [Why use this fork](#why-use-this-fork) — the most tested mimalloc fork in existence
- [Choosing a version: v2 or v3](#choosing-a-version-v2-or-v3)
- [Profiling and observability](#profiling-and-observability) — sampled pprof, exact stats, DHAT, memory events
- [Upstream bugs found and fixed](#upstream-bugs-found-and-fixed) — including two unbounded memory leaks
- [Documentation](#documentation) — the full docs index
- [Release history](#release-history)
- [Prior art and credits](#prior-art-and-credits)

---

## Quick start

Three instruments, each shown in Rust and C: **pprof** sampled profiling for
production, **exact allocator stats** to check a sampled profile against, and
**DHAT** exact profiling for focused investigations.

### pprof: sampled heap profiling

#### Rust

```toml
[dependencies]
mimalloc-pprof = "0.9"

[profile.release]
debug = "line-tables-only"
strip = false
```

```rust
use mimalloc_pprof::{prof, MiMalloc};
use std::path::Path;

#[global_allocator]
static ALLOCATOR: MiMalloc = MiMalloc;

fn main() -> std::io::Result<()> {
    assert!(prof::start(0), "profiler already running"); // 0 = default, ~512 KiB

    let retained = vec![0_u8; 1024 * 1024];
    prof::dump_file(Path::new("heap.prof"))?;            // dump while still live
    std::hint::black_box(&retained);

    prof::stop();
    Ok(())
}
```

#### C

```sh
cmake -S . -B build -DMI_PPROF=ON -DCMAKE_BUILD_TYPE=RelWithDebInfo
cmake --build build --config RelWithDebInfo
```

`MI_PPROF` now defaults to `1` in the header (`include/mimalloc/types.h`), so a
build that compiles `src/static.c` directly instead of using CMake gets the
profiler by default too — CMake keeps passing `-DMI_PPROF=0/1` explicitly, which
still wins over the header default. On non-Windows you still need to pass
`-fno-omit-frame-pointer` yourself in that case — without it the profiler's stack
capture is unreliable.

```c
#include <mimalloc.h>
#include <mimalloc/profile.h>

int main(void) {
  if (!mi_prof_start(0)) return 1;          /* 0 = default interval, ~512 KiB */

  void* p = mi_malloc(1024 * 1024);
  if (!mi_prof_dump("heap.prof")) return 2; /* dump while it is still live */

  mi_free(p);
  mi_prof_stop();
  return 0;
}
```

`mi_prof_start` and `mi_prof_dump` are `nodiscard` — check their results or the
compiler will warn.

#### Or without touching the code at all

```sh
MIMALLOC_PROF=1 MIMALLOC_PROF_DUMP_AT_EXIT=heap.prof ./my_app
```

Built with `MI_NO_PROCESS_DETACH` (issue #268)? The automatic exit path that
`*_DUMP_AT_EXIT` relies on is skipped by design — call `mi_prof_dump` / `mi_dhat_dump`
yourself before the process exits.

#### Then read the profile

```sh
pprof -http=:0 ./my_app heap.prof     # interactive
pprof -top ./my_app heap.prof         # text summary
```

#### ⚠️ The flags your stacks depend on

On Linux/macOS the profiler walks frame pointers, and **your** code must keep
them — the failure mode is silently truncated stacks, not an error:

| Toolchain | Required flag |
|---|---|
| C / C++ | `-fno-omit-frame-pointer` |
| Rust | `-Cforce-frame-pointers=yes` (in `.cargo/config.toml` rustflags) |

Apply it to every library you want to see in a profile, and build with debug
info so addresses resolve to names. Windows x64 needs no flag — it uses unwind
tables — but keep the matching **PDB** next to the binary.

### Exact allocator stats

Alongside the sampled profile, v3 exposes the allocator's own **exact** counters
— useful for checking how much the sampled numbers under-count.

#### Rust

```rust
let s = mimalloc_pprof::prof::stats();
println!(
    "sampled live: {} bytes; exact committed: {}, requested: {}",
    s.live_bytes, s.heap.committed, s.heap.malloc_requested,
);
```

#### C

```c
#include <stdio.h>
#include <mimalloc.h>
#include <mimalloc/profile.h>

mi_prof_stats_t_decl(stats);   /* zeroed, with size + version filled in */
if (mi_prof_stats_get(&stats)) {
  printf("sampled live: %zu bytes; exact committed: %zu, requested: %zu\n",
         stats.live_bytes, stats.heap_committed, stats.heap_malloc_requested);
}
```

Every text dump also embeds the same counters as pprof-ignored `#` comment lines,
so a saved profile carries them without any code at all.

One caveat: `malloc_requested` is only maintained when the library was built with
`-DMI_STAT=2` — a default release build reports 0. Since 0 is also a legitimate
value, check `heap.detailed` (Rust) / `stats.heap_stats_detailed` (C) to tell the
two apart. Full field list and the rest of the caveats:
**[docs/profiler.md → allocator statistics](docs/profiler.md#allocator-statistics-in-the-profile-v3-only)**.

### DHAT: exact heap profiling

When sampling isn't enough — you want **every** allocation's size and lifetime —
run a short, focused session under the exact DHAT observer and open the result
in Valgrind's [`dh_view.html`](https://valgrind.org/docs/manual/dh-manual.html).
No code needed at all:

```sh
MIMALLOC_DHAT=1 MIMALLOC_DHAT_DUMP_AT_EXIT=heap.dhat.json ./my_app
```

Built with `MI_NO_PROCESS_DETACH` (issue #268)? The automatic exit path that
`*_DUMP_AT_EXIT` relies on is skipped by design — call `mi_prof_dump` / `mi_dhat_dump`
yourself before the process exits.

#### Rust

```rust
use mimalloc_pprof::dhat;
use std::path::Path;

assert!(dhat::start(), "DHAT already running");

let retained = vec![0_u8; 1024 * 1024];
std::hint::black_box(&retained);

dhat::stop();  // stop observing; the retained records still dump
dhat::dump_file(Path::new("heap.dhat.json")).expect("write DHAT report");
```

#### C

```c
#include <mimalloc.h>
#include <mimalloc/dhat.h>

if (!mi_dhat_start()) return 1;

void* p = mi_malloc(4096);
mi_free(p);

mi_dhat_stop();                             /* stop observing; report still dumps */
if (!mi_dhat_dump("heap.dhat.json")) return 2;
```

Unlike the sampled profiler this keeps a record for **every** live allocation, so
it is exact but high-overhead — use it for tests and focused investigations, not a
continuously running production workload. Memory budgeting
(`MIMALLOC_DHAT_MAX_BYTES`), partial-report semantics, and the stats API:
**[docs/dhat-and-memory-events.md](docs/dhat-and-memory-events.md)**.

### Going deeper

Everything deeper — CMake install and linking, the zeroing-realloc family,
stack-flag guidance, cross-compilation — is in
**[docs/c-integration.md](docs/c-integration.md)** and
**[docs/rust-integration.md](docs/rust-integration.md)**. The full instrument
lineup, including the memory-events API, is in
[Profiling and observability](#profiling-and-observability).

---

## Performance

mimalloc-pprof is continuously benchmarked against **upstream mimalloc**,
**TCMalloc**, and **jemalloc** on a dedicated Linux x86-64 runner. Every result
is **GitHub-hosted and informational** — no self-hosted hardware, no hand-picked
runs, no unpublished baselines.

| Resource | Description |
|---|---|
| [**Benchmark dashboard**](https://zackees.github.io/mimalloc-pprof/) | Live per-scenario throughput, paired statistical effects, and full allocator provenance |
| [`benchmark-stats` branch](https://github.com/zackees/mimalloc-pprof/tree/benchmark-stats) | Raw sealed site artifacts (history, manifests, digests) |
| [`latest.json`](https://zackees.github.io/mimalloc-pprof/latest.json) | Machine-readable publication envelope for the most recent headline run |

In brief: all four allocators are pinned to immutable, SHA-256-verified sources;
every block runs them in randomized order under one workload seed; paired effects
are bootstrap confidence intervals at 95%, expressed relative to upstream
mimalloc; and profiling is disabled during measurement so the allocator runs in
its natural configuration.

### Thread scaling by allocation pattern

Aggregate throughput as worker threads go from 1 to 4 to 16, for four allocation
patterns. Each pattern is a seeded random operation stream, so all four
allocators replay one identical stream inside each paired block.

> **Coverage mode: reduced statistical rigor (3 blocks per cell).** These panels
> trade statistical rigor for thread coverage — no confidence intervals, no noise
> gating; read them for shape. The runner allows 4 logical CPUs, so the 16-thread
> point is 4× oversubscribed and describes contention, not core scaling — it is
> shaded on every chart.

[![Tiny hot path: aggregate throughput by worker count for all four allocators](https://raw.githubusercontent.com/zackees/mimalloc-pprof/benchmark-stats/benchmark-scaling-tiny-hot.svg)](https://zackees.github.io/mimalloc-pprof/#scaling)

[![General mix including realloc: aggregate throughput by worker count for all four allocators](https://raw.githubusercontent.com/zackees/mimalloc-pprof/benchmark-stats/benchmark-scaling-mixed-general.svg)](https://zackees.github.io/mimalloc-pprof/#scaling)

[![Large page-touched buffers: aggregate throughput by worker count for all four allocators](https://raw.githubusercontent.com/zackees/mimalloc-pprof/benchmark-stats/benchmark-scaling-large-buffers.svg)](https://zackees.github.io/mimalloc-pprof/#scaling)

[![Cross-thread producer/consumer handoff: aggregate throughput by worker count for all four allocators](https://raw.githubusercontent.com/zackees/mimalloc-pprof/benchmark-stats/benchmark-scaling-cross-thread.svg)](https://zackees.github.io/mimalloc-pprof/#scaling)

| Pattern | Sizes | What it stresses |
|---|---|---|
| Tiny hot path | 16–64 B | small-object fast path, high alloc/free rate, small live set |
| General mix | 8 B–4 KiB log-uniform | everyday mix including realloc, medium live set |
| Large buffers | 64 KiB–4 MiB | large allocations with one-byte-per-page touching |
| Cross-thread handoff | 16–512 B | remote-free pressure; blocks are freed by another worker |

Full methodology, per-cell tables, and the pending metric roadmap:
**[docs/benchmarks.md](docs/benchmarks.md)**.

---

## Why use this fork

Beyond being the one mimalloc with a native-Windows heap profiler, this is — as of
September 2026 — **the most tested mimalloc fork in existence**, and that testing
regime has caught real allocator bugs that **Microsoft has since upstreamed fixes
for** ([`60c4f031`](https://github.com/microsoft/mimalloc/commit/60c4f031c9d878da05ffa6066777accd51458b98),
crediting [#56](https://github.com/zackees/mimalloc-pprof/issues/56)).

**Every platform, every commit.** Ubuntu, Windows **MSVC**, Windows **MinGW**, and
macOS are all required CI gates, in Debug and Release, with the profiler compiled
in and out, plus shared-library builds. Upstream has no MinGW job at all — running
one here is exactly how two unbounded memory leaks were found
([docs/upstream-bugs.md](docs/upstream-bugs.md)).

**Cross-compilation is tested, not assumed.** The Rust crate builds wherever
`cc-rs` reaches a C compiler, and CI exercises cross builds including
`cargo-xwin` for `aarch64-pc-windows-msvc` — which is how a real upstream
ARM64-atomics incompatibility was caught and fixed
([#223](https://github.com/zackees/mimalloc-pprof/issues/223)).

**Tests that must prove they can fail.** Beyond correctness suites there is a
memory-regression gate, an instruction-set baseline scanner, AddressSanitizer,
and structured fuzzing — and every gate that can carry a *positive control* has
one: a deliberately injected bug it must catch, verified on every run. A
regression test that has never been observed to fail proves nothing
([docs/ci-gates.md](docs/ci-gates.md)).

**The entire fork constellation was scoured for improvements.** All 1,146 GitHub
forks of mimalloc were enumerated and the living ones byte-diffed — every fork
pushed since mid-2024 plus every older starred one — and each real change rated
for adoption ([`MIMALLOC_FORKS.md`](MIMALLOC_FORKS.md)). The good ideas came in:
[Bun's](https://github.com/oven-sh/mimalloc) zero-tracking optimization and its
TLS-slot zeroing fix are on `main` — in the latter case each fork had found half
the bug, and this one now carries both halves. Just as deliberately, changes that
failed review stayed out, each with its reasoning recorded
([docs/fork-divergence.md](docs/fork-divergence.md)).

**Measured against its peers, continuously.** Throughput is benchmarked against
upstream mimalloc, TCMalloc, and jemalloc on every publication cycle, with sealed
artifacts and reproduction commands — the charts above.

**Committed to for the long term.** [Zach Vorhies](https://github.com/zackees),
the author, commits to maintaining this fork long-term.
[Pull requests](https://github.com/zackees/mimalloc-pprof/pulls) are welcome and
will be reviewed — and held to the same bar as everything else here: every
contribution passes the full four-platform CI matrix and its gates before it
lands, so quality is maintained by machinery, not just intent.

---

## Choosing a version: v2 or v3

**When in doubt choose v3, the current default.**

Two engine lines are published as two version ranges of the same crate. The
profiler API, environment variables, and output formats are **identical** in both,
so switching is a version bump, not a code change.

| | **v3 — current** | **v2 — previous** |
|---|---|---|
| Crate | [`mimalloc-pprof` 0.9.x](https://crates.io/crates/mimalloc-pprof) | [0.8.x](https://crates.io/crates/mimalloc-pprof/0.8.0) |
| Lives on | `main` | the [`v2`](https://github.com/zackees/mimalloc-pprof/tree/v2) branch |
| Upstream base | mimalloc v3 (`upstream/dev3`) | mimalloc v2 (`upstream/main`) |
| Allocator design | arena-of-slices + page-map; `mi_heap_t`/`mi_theap_t` split | segment allocator |
| Allocator statistics | **per-heap and per-partition** (a "subproc" — mimalloc's in-process isolation domain, *not* an OS child process) | process-wide totals only |
| Memory under thread churn (MinGW) | **flat** | leaked in published 0.8.0; fixed on the `v2` branch — [bug 1](docs/upstream-bugs.md#bug-1-thread-exit-cleanup-never-runs-on-mingw--unbounded-memory-leak) |

**Use v3 (0.9.x)** unless you have a specific reason not to. It has strictly more
test coverage, richer statistics, and fixes two upstream bugs that v2 still had.

**The caveat worth stating plainly:** upstream mimalloc v3 (`dev3`) is still a
pre-release branch. It has had less field exposure than v2 no matter how green a
test suite looks. That risk is real and testing cannot retire it — see
[how v3 was validated](docs/fork-divergence.md#how-v3-was-validated) for exactly
what was measured.

---

## Profiling and observability

Four complementary instruments. Pick by the question you're asking:

| Instrument | Use it when you want… | Overhead | Deep dive |
|---|---|---|---|
| **Sampled pprof profiler** | flame graphs of live heap in production | low (sampled, ~512 KiB interval) | **[docs/profiler.md](docs/profiler.md)** |
| **Exact allocator statistics** (v3) | ground truth to check the profile against | none — counters the allocator already keeps | **[docs/profiler.md → allocator statistics](docs/profiler.md#allocator-statistics-in-the-profile-v3-only)** |
| **Exact DHAT profiling** | every allocation's lifetime, in a short focused run | high (exact; not for production) | **[docs/dhat-and-memory-events.md](docs/dhat-and-memory-events.md)** |
| **Memory-events API** | your own counters/callbacks on allocation events | opt-in, works even with `MI_PPROF=OFF` | **[docs/dhat-and-memory-events.md → memory events](docs/dhat-and-memory-events.md#memory-events-api)** |

Two details worth knowing before you go deeper:

- **The pprof profiler** is runtime opt-in (`mi_prof_start` / `prof::start` /
  `MIMALLOC_PROF=1`) and dumps `heap_v2` text or `profile.proto`. Env vars,
  deterministic seeding, the config-override API, and the measured cost of
  shipping `MI_PPROF=ON` are all in its doc.
- **The exact stats are what make a sampled profile trustworthy**: comparing the
  allocator's exact `malloc_requested` against the profiler's sampled
  `live_bytes` measures the sampling error directly. They ride along inside
  every dump as pprof-ignored `#` comment lines.

---

## Upstream bugs found and fixed

Working on this fork surfaced **defects in upstream microsoft/mimalloc** that
affect anyone using mimalloc on Windows/MinGW, fork or not — each reproduced on
stock upstream before being claimed:

1. **Thread-exit cleanup never runs on MinGW** — an unbounded leak (~0.24 GB per
   `test-stress` iteration) because upstream registers TLS callbacks with
   MSVC-only pragmas that GCC silently ignores. Fixed here on both lines; the fix
   was adopted upstream (with a follow-up we still carry).
2. **`mi_heap_new` / `mi_subproc_new` don't bootstrap the library** — a crash
   when either is the first mimalloc call in a process. Fixed in 0.9.0.
3. **`test-stress.c` dereferences unchecked allocations**, turning allocation
   failure into an opaque segfault.

Root causes, measurements, and the regression tests that keep them fixed:
**[docs/upstream-bugs.md](docs/upstream-bugs.md)**. The CI gates that guard every
PR — including the seven gates that were found to be verifying nothing —
are in **[docs/ci-gates.md](docs/ci-gates.md)**.

---

## Documentation

| Document | Contents |
|---|---|
| [docs/c-integration.md](docs/c-integration.md) | CMake build/install, linking, zeroing-realloc variants, frame-pointer flags |
| [docs/rust-integration.md](docs/rust-integration.md) | Crate setup, stats API, cargo config, cross-compilation |
| [docs/profiler.md](docs/profiler.md) | Profiler reference: cost, env vars, seeding, embedded-mimalloc concerns, exact stats |
| [docs/dhat-and-memory-events.md](docs/dhat-and-memory-events.md) | Exact DHAT profiling and the memory-events API |
| [docs/benchmarks.md](docs/benchmarks.md) | Benchmark methodology, thread-scaling panels, metric roadmap |
| [docs/upstream-bugs.md](docs/upstream-bugs.md) | The upstream bugs in depth, and how they are kept fixed |
| [docs/ci-gates.md](docs/ci-gates.md) | Every CI gate, what it catches, and its positive control |
| [docs/fork-divergence.md](docs/fork-divergence.md) | Every divergence from upstream, with origin and status; how v3 was validated |
| [docs/maintainers.md](docs/maintainers.md) | Integration contract, vendored-source regeneration, repo layout |
| [docs/dev-loop.md](docs/dev-loop.md) | Fast local development loop |
| [docs/upstreaming.md](docs/upstreaming.md) | Fixes prepared for submission back to microsoft/mimalloc |
| [readme-upstream.md](readme-upstream.md) | Upstream mimalloc documentation (build modes, overrides, options) |
| [MIMALLOC_FORKS.md](MIMALLOC_FORKS.md) | Survey of other mimalloc forks and what was (not) adopted |

Design history and milestone decisions are in
[issue #2](https://github.com/zackees/mimalloc-pprof/issues/2).

---

## Release history

The authoritative release record, with the reasoning behind each fix, is
[`rust/mimalloc-pprof/CHANGELOG.md`](rust/mimalloc-pprof/CHANGELOG.md). The v3
line ships as [`mimalloc-pprof` 0.9.x](https://crates.io/crates/mimalloc-pprof);
the v2 line (0.8.x) is maintained on the
[`v2`](https://github.com/zackees/mimalloc-pprof/tree/v2) branch.

---

## Prior art and credits

- [microsoft/mimalloc](https://github.com/microsoft/mimalloc), by Daan Leijen (MIT).
- [microsoft/mimalloc#1266](https://github.com/microsoft/mimalloc/pull/1266), the
  sampled-allocation-hook design this fork builds on.
- [gperftools](https://github.com/gperftools/gperftools), whose `heap_v2` format is
  accepted by google/pprof.

## License

MIT, the same as upstream. See [LICENSE](LICENSE).
