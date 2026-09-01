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

**Platforms.** Linux, macOS, and Windows — with both **MSVC and MinGW** as required
CI targets, on every commit, in Debug and Release. MinGW being a gate rather than an
afterthought is how the leaks in
[docs/upstream-bugs.md](docs/upstream-bugs.md) were found; upstream has no MinGW job.

**Contents**

- [Quick start](#quick-start)
- [Performance](#performance) — continuous benchmarks vs. upstream mimalloc, TCMalloc, and jemalloc
- [Choosing a version: v2 or v3](#choosing-a-version-v2-or-v3)
- [Adding it to your build](#adding-it-to-your-build) — Rust and C/C++
- [Profiling and observability](#profiling-and-observability) — sampled pprof, exact stats, DHAT, memory events
- [Upstream bugs found and fixed](#upstream-bugs-found-and-fixed) — including two unbounded memory leaks
- [Documentation](#documentation) — the full docs index
- [Release history](#release-history)
- [Prior art and credits](#prior-art-and-credits)

---

## Quick start

### Rust

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

### C

```sh
cmake -S . -B build -DMI_PPROF=ON -DCMAKE_BUILD_TYPE=RelWithDebInfo
cmake --build build --config RelWithDebInfo
```

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

### Or without touching the code at all

```sh
MIMALLOC_PROF=1 MIMALLOC_PROF_DUMP_AT_EXIT=heap.prof ./my_app
```

### Then read the profile

```sh
pprof -http=:0 ./my_app heap.prof     # interactive
pprof -top ./my_app heap.prof         # text summary
```

Build with debug info, and on MSVC keep the matching PDB next to the binary.

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

[![General mix including realloc: aggregate throughput by worker count for all four allocators](https://raw.githubusercontent.com/zackees/mimalloc-pprof/benchmark-stats/benchmark-scaling-mixed-general.svg)](https://zackees.github.io/mimalloc-pprof/#scaling)

Full methodology, the other thread-scaling panels, and the pending metric roadmap:
**[docs/benchmarks.md](docs/benchmarks.md)**.

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
| Allocator statistics | **per-heap and per-subprocess** | process-wide totals only |
| Memory under thread churn (MinGW) | **flat** | leaked in published 0.8.0; fixed on the `v2` branch — [bug 1](docs/upstream-bugs.md#bug-1-thread-exit-cleanup-never-runs-on-mingw--unbounded-memory-leak) |

**Use v3 (0.9.x)** unless you have a specific reason not to. It has strictly more
test coverage, richer statistics, and fixes two upstream bugs that v2 still had.

**The caveat worth stating plainly:** upstream mimalloc v3 (`dev3`) is still a
pre-release branch. It has had less field exposure than v2 no matter how green a
test suite looks. That risk is real and testing cannot retire it — see
[how v3 was validated](docs/fork-divergence.md#how-v3-was-validated) for exactly
what was measured.

---

## Adding it to your build

### Rust

Add the crate and install the global allocator (see [Quick start](#quick-start)).
On Linux/macOS, keep frame pointers so stacks resolve:

```toml
# .cargo/config.toml
[build]
rustflags = ["-Cforce-frame-pointers=yes"]
```

Windows x64 uses unwind tables instead — keep the PDB for symbolization.
Cross-compilation works wherever `cc-rs` finds a C compiler, including
`cargo-xwin` for `aarch64-pc-windows-msvc`.
Details: **[docs/rust-integration.md](docs/rust-integration.md)**.

### C / C++

```sh
cmake -S . -B build -DMI_PPROF=ON -DCMAKE_BUILD_TYPE=RelWithDebInfo
cmake --build build --config RelWithDebInfo
```

```cmake
add_subdirectory(path/to/mimalloc-pprof)
target_link_libraries(my_app PRIVATE mimalloc-static)

# Linux/macOS: the profiler walks frame pointers, so your code must keep them.
if(NOT WIN32)
  target_compile_options(my_app PRIVATE -fno-omit-frame-pointer)
endif()
```

Use `MI_PPROF=OFF` to omit the profiler hooks entirely (no-op stubs stay
linkable). **Do not link two mimalloc implementations into one process.**
Build/install details, the zeroing-realloc family, and stack-flag guidance:
**[docs/c-integration.md](docs/c-integration.md)**.

---

## Profiling and observability

Four complementary facilities, all documented in depth in `docs/`:

**Sampled pprof profiler** — the headline feature. Runtime opt-in
(`mi_prof_start` / `prof::start` / `MIMALLOC_PROF=1`), ~512 KiB default sample
interval, dumps `heap_v2` text or `profile.proto`. Environment variables,
deterministic seeding, config-override API, and the measured cost of shipping
`MI_PPROF=ON`: **[docs/profiler.md](docs/profiler.md)**.

**Exact allocator statistics (v3)** — alongside the sampled numbers,
`mi_prof_stats_t` / Rust `ProfStats::heap` carry the allocator's **exact**
per-heap counters (`committed`, `reserved`, `malloc_requested`, page counts), and
every text dump embeds them as pprof-ignored `#` comment lines. Comparing exact
`malloc_requested` against sampled `live_bytes` measures the sampling error
directly:
**[docs/profiler.md#allocator-statistics-in-the-profile-v3-only](docs/profiler.md#allocator-statistics-in-the-profile-v3-only)**.

**Exact DHAT profiling** — `<mimalloc/dhat.h>` is an exact, high-overhead
heap/lifetime observer writing DHAT v2 JSON for Valgrind's `dh_view.html`. For
short tests and focused investigations rather than production:
**[docs/dhat-and-memory-events.md](docs/dhat-and-memory-events.md)**.

**Memory-events API** — opt-in allocation-change counters, callbacks, and a
live-allocation visitor, independent of `MI_PPROF` and available even in OFF
builds:
**[docs/dhat-and-memory-events.md#memory-events-api](docs/dhat-and-memory-events.md#memory-events-api)**.

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
