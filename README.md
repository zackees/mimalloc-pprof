# mimalloc-pprof

`mimalloc-pprof` is a fork of
[microsoft/mimalloc](https://github.com/microsoft/mimalloc) with
pprof-compatible sampled heap profiling. It supports native Windows as a
first-class target as well as Linux and macOS.

The C allocator tracks sampled live allocations and writes either the
gperftools `heap_v2` text format or an uncompressed pprof `profile.proto`.
Both formats can be opened with [google/pprof](https://github.com/google/pprof)
for flame graphs, call graphs, top reports, and profile comparisons.

## Current status

| Component | Status |
|---|---|
| C allocator | Based on mimalloc v3 (`dev3`) |
| Sampled heap profiler | Implemented; built when `MI_PPROF=ON` (the default) |
| Memory-events API | Implemented; always compiled and disabled at runtime by default |
| Windows | MSVC and MinGW are required CI targets |
| Rust crate | `rust/mimalloc-pprof`; builds its own checked-in C amalgamation |

The profiler is opt-in at runtime. A build with `MI_PPROF=ON` does not start
sampling until the application calls a start API or sets `MIMALLOC_PROF=1`.
When profiling is disabled, the allocation path only performs the guarded
hook checks.

The fork-specific profiler and memory-events function signatures are preserved
across the v3 port. The underlying allocator follows upstream mimalloc v3, so
applications moving from mimalloc v2 should still review upstream's v3 API and
behavior changes.

The design history and milestone decisions are recorded in
[issue #2](https://github.com/zackees/mimalloc-pprof/issues/2).

## Which version: v2 or v3?

The allocator engine underneath this fork tracks upstream mimalloc. Two engine
lines are published, as two version ranges of the same `mimalloc-pprof` crate:

| | **v2 engine** | **v3 engine** |
|---|---|---|
| Crate | [`mimalloc-pprof` 0.8.x](https://crates.io/crates/mimalloc-pprof) — [crates.io](https://crates.io/crates/mimalloc-pprof/0.8.0), [docs.rs](https://docs.rs/mimalloc-pprof/0.8.0) | 0.9.x — **not yet published to crates.io** |
| Where it lives | the [`v2`](https://github.com/zackees/mimalloc-pprof/tree/v2) branch | `main` |
| Upstream base | mimalloc v2 (`upstream/main`) | mimalloc v3 (`upstream/dev3`) |
| Maturity | Previous line; still what crates.io serves. | **Current mainline.** Upstream v3 is still pre-release. |
| Allocator design | segment allocator | arena-of-slices + page-map; `mi_heap_t`/`mi_theap_t` split |
| Allocator statistics | process-wide totals only | **per-heap and per-subprocess** (see below) |
| Profiler API | identical | identical, plus the v3 `mi_prof_stats_t` fields below |

```toml
# v2 engine, from crates.io (what `cargo add mimalloc-pprof` gives you today)
mimalloc-pprof = "0.8"

# v3 engine -- on `main`, not on crates.io yet
mimalloc-pprof = { git = "https://github.com/zackees/mimalloc-pprof" }
```

The profiler API, the environment variables, and the pprof output format are the
same in both, so moving between them is a version bump rather than a code change.

**v3 is now the mainline** and carries crate version 0.9.0 in-tree. It is not yet
published to crates.io; 0.8.x remains what crates.io serves until 0.9.0 is tagged.

### Why v3 became the mainline

v3 was held to the same bar as v2 before promoting it, on identical workloads and
the same machine (Windows/MinGW, 32 threads):

| | v2 (0.8.x) | v3 (0.9.x) |
|---|---|---|
| `ctest`, Debug and Release, 3 runs each | 9/9 | **12/12** (superset of v2's tests) |
| `MI_PPROF=OFF` | 6/6 | 8/8 |
| Rust workspace suite | green | green |
| `test-stress` peak RSS @ 50 iterations | 11.98 GB | **0.24 GB** |
| `test-stress` peak RSS @ 100 iterations | 23.50 GB | flat |
| `test-stress-heaps` @ 25/50/100/200 iterations | (test not present) | flat ~0.82 GB |

v2's memory grows linearly with iteration count on MinGW — an unbounded leak that
is still present on the `v2` branch and in published 0.8.x. v3's is flat. v3 also
carries two fixes for upstream bugs that v2 still has: `mi_heap_new`/`mi_subproc_new`
failing to bootstrap the library, and thread-exit TLS callbacks never being
registered under MinGW.

The one thing testing cannot retire is that upstream mimalloc v3 (`dev3`) is itself
a pre-release branch, so it has had less field exposure than v2 regardless of how
green the suite is. That is the reason 0.9.0 is not auto-published.

Choose v3 (0.9.x) when you want upstream v3's allocator improvements or the
richer allocator statistics described in
[Allocator statistics in the profile](#allocator-statistics-in-the-profile).
Because upstream v3 is pre-release, the v3 line carries extra test coverage in
this fork — a dedicated concurrency suite (`test/test-profile-race.c` and
`rust/mimalloc-pprof/tests/t15_heap_stats.rs`) exercises the thread-bootstrap
race, cross-thread frees, and snapshot stability against the reorganized v3
engine, on top of the shared profiler tests.

The full `ctest` suite passes 12/12 on Windows (MSVC and MinGW), Linux, and
macOS, in both Debug and Release. Two stress tests (`test-stress-heaps`,
`test-stress-subprocs`) used to abort on Windows; that turned out to be a real
initialization-order bug rather than pre-existing upstream breakage, and is
fixed here — see `mi_heap_new_in_arena` in `src/heap.c` and `mi_subproc_new` in
`src/init.c`. Both are candidates for upstreaming to microsoft/mimalloc.

### Allocator statistics in the profile

v3 exposes per-heap and per-subprocess allocator counters (`mi_heap_stats_get`,
`mi_subproc_stats_get`) that the v2 engine had no API for. The profiler surfaces
them in two places, both v3-only:

1. **`mi_prof_stats_t` v3 fields** (`MI_PROF_STAT_VERSION` 3) — `heap_committed`,
   `heap_reserved`, `heap_malloc_requested`, `heap_pages`,
   `heap_pages_abandoned`, `heap_count`, `theap_count`, `heap_purged`. In Rust
   these are `ProfStats::heap`, a `HeapStats` struct.
2. **A comment block in the text dump**, emitted after the samples and before
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

Everything else in `mi_prof_stats_t` is *sampled*; these fields are **exact**.
That difference is the point: a sampled profile alone cannot tell you whether it
under-counted, but comparing `heap_malloc_requested` against `live_bytes`
measures the sampling error directly. This is what makes assertions on a sampled
profile meaningful in tests and in production monitoring.

`mi_prof_stats_get` still accepts v1- and v2-sized structs from older callers and
leaves the newer fields untouched, so upgrading the header does not break an
existing binary.

Two counters need a caveat:

- **`heap_malloc_requested` requires `MI_STAT >= 2`.** Upstream enables that level
  by default only for debug builds (`MI_DEBUG > 0`); a default **release** build
  has `MI_STAT == 0` and reports 0 for this field. Because 0 is also a legitimate
  value, `mi_prof_stats_t.heap_stats_detailed` (Rust: `HeapStats::detailed`)
  reports which case you are in, and the text dump records it as
  `# detailed_stats = 0|1` so a saved profile is self-describing. Build with
  `-DMI_STAT=2` to enable it in release, at some allocation-path cost.
- **`theap_count` does not count the main thread's statically-initialized theap**,
  so a single-threaded process reports 0. It also lags thread exit, since v3
  reference-counts theaps for cached thread-locals.

## Choose an integration path

- C or C++ application: build this repository with CMake, link one mimalloc
  target, and include `mimalloc/profile.h`.
- Rust application: depend on the `mimalloc-pprof` crate and install
  `MiMalloc` as the one global allocator.
- Allocator instrumentation: include `mimalloc/memory-events.h`; this API is
  independent of `MI_PPROF`.

Do not link a second mimalloc implementation into the same process. In
particular, a Rust binary using the crate's vendored allocator should not also
link the root CMake library.

## C and C++ integration

### 1. Build and install the library

```sh
cmake -S . -B build -DMI_PPROF=ON -DCMAKE_BUILD_TYPE=RelWithDebInfo
cmake --build build --config RelWithDebInfo
cmake --install build --config RelWithDebInfo --prefix /path/to/prefix
```

Use `MI_PPROF=OFF` when the profiler implementation and allocation hooks must
be omitted. The public profiler functions remain linkable as no-op stubs, and
the memory-events API remains fully available.

In a CMake consumer, link the installed or in-tree `mimalloc` shared target or
the `mimalloc-static` static target in the same way as upstream mimalloc:

```cmake
add_subdirectory(path/to/mimalloc-pprof)
target_link_libraries(my_app PRIVATE mimalloc-static)
```

### 2. Start, dump, and stop profiling

```c
#include <mimalloc.h>
#include <mimalloc/profile.h>

int main(void) {
  if (!mi_prof_start(0)) {  /* 0 uses env/default: about 512 KiB */
    return 1;
  }

  /* Run the workload whose live heap you want to inspect. */
  void* allocation = mi_malloc(1024 * 1024);

  if (!mi_prof_dump("heap.prof")) {
    mi_free(allocation);
    mi_prof_stop();
    return 2;
  }

  mi_free(allocation);
  mi_prof_stop();
  return 0;
}
```

`mi_prof_start_ex` provides structured configuration for the sample interval,
sampling seed, cumulative mode, stack depth, profiler-memory budget, and
exit-time dump. The complete, versioned API contract is in
[`include/mimalloc/profile.h`](include/mimalloc/profile.h).

To include allocations performed during process startup, configure profiling
before launching the program instead of starting it from `main`:

```sh
MIMALLOC_PROF=1 \
MIMALLOC_PROF_DUMP_AT_EXIT=heap.prof \
MIMALLOC_PROF_SAMPLE_INTERVAL=524288 \
./my_app
```

Useful optional settings include:

| Setting | Meaning |
|---|---|
| `MIMALLOC_PROF_ACCUM=1` | Keep cumulative allocation counters until `mi_prof_reset` |
| `MIMALLOC_PROF_BT_MAX=32` | Maximum captured stack depth (compile-time cap: 128) |
| `MIMALLOC_PROF_MAX_BYTES=N` | Bound persistent profiler arena memory |
| `MIMALLOC_PROF_SEED=N` | Use deterministic sampling for repeatable tests |
| `MIMALLOC_PROF_DUMP_FORMAT=proto` | Write pprof `profile.proto` at exit |

`MIMALLOC_PROF_SAMPLE_RATE` remains a compatibility alias for
`MIMALLOC_PROF_SAMPLE_INTERVAL`; when both are set, `..._INTERVAL` wins.

### 3. Keep symbols and inspect the profile

Build the application with debug information. Keep the executable and, on
MSVC, its matching PDB next to the profile used for analysis.

```sh
pprof -http=:0 ./my_app heap.prof
pprof -top ./my_app heap.prof
```

On Windows, replace `./my_app` with the path to the matching `.exe`.

## Rust integration

Pick the engine line you want (see
[Which version: v2 or v3?](#which-version-v2-or-v3)):

```toml
[dependencies]
# v2 engine, from crates.io
mimalloc-pprof = "0.8"
# v3 engine -- current mainline, not yet on crates.io:
# mimalloc-pprof = { git = "https://github.com/zackees/mimalloc-pprof" }

[profile.release]
debug = "line-tables-only"
strip = false
```

Or for a checkout-to-checkout integration:

```toml
[dependencies]
mimalloc-pprof = { path = "../mimalloc-pprof/rust/mimalloc-pprof" }
```

Install the allocator once, start profiling before the workload, and dump while
the allocations of interest are still live:

```rust
use mimalloc_pprof::{prof, MiMalloc};
use std::path::Path;

#[global_allocator]
static ALLOCATOR: MiMalloc = MiMalloc;

fn main() -> std::io::Result<()> {
    if !prof::start(0) {
        return Err(std::io::Error::new(
            std::io::ErrorKind::AlreadyExists,
            "heap profiler already active",
        ));
    }

    let retained = vec![0_u8; 1024 * 1024];
    prof::dump_file(Path::new("heap.prof"))?;
    std::hint::black_box(&retained);
    prof::stop();
    Ok(())
}
```

On the v3 engine (0.9.x), `prof::stats()` also carries the exact allocator
counters alongside the sampled ones:

```rust
let s = mimalloc_pprof::prof::stats();
println!(
    "sampled live: {} bytes in {} samples; allocator committed: {} bytes, requested: {}",
    s.live_bytes, s.live_samples, s.heap.committed, s.heap.malloc_requested,
);
```

On Linux and macOS, retain frame pointers for reliable stack walking:

```toml
# .cargo/config.toml
[build]
rustflags = ["-Cforce-frame-pointers=yes"]
```

Windows x64 stack capture uses unwind information instead; keep the generated
PDB for symbolization.

Important source-layout rule: the Rust package compiles
`rust/mimalloc-pprof/vendor/mimalloc-pprof-amalgamated.c`, not the root
`src/` tree. After an intentional C-core update, maintainers must regenerate
and validate the vendored source in a separate Rust-only commit:

```sh
cd rust
soldr cargo run -p xtask -- amalgamate-c
soldr cargo run -p xtask -- amalgamate-h
soldr cargo run -p xtask -- check
soldr cargo test --workspace --locked
```

Keeping C-core and `rust/` changes in separate commits allows the C changes to
be cherry-picked cleanly upstream.

## Memory-events integration

`include/mimalloc/memory-events.h` exposes opt-in allocation-change counters,
callbacks, a best-effort live-allocation visitor, and raw-OS-layer
`mi_unwrapped_*` allocation functions for instrumentation that must avoid
allocator recursion.

Enable tracking before the first allocation when exact lifetime totals are
required:

```c
#include <mimalloc/memory-events.h>

mi_memory_tracking_set_enabled(true);

mi_memory_snapshot_t_decl(snapshot);
if (mi_memory_snapshot(&snapshot)) {
  /* snapshot.live_bytes and snapshot.accum_bytes are now available */
}
```

Alternatively, set `MIMALLOC_MEMORY_EVENTS=1` before process launch. Enabling
tracking later does not reconstruct allocations made while it was disabled.
Callback reentrancy, pointer lifetime, and live-visitor restrictions are
documented in
[`include/mimalloc/memory-events.h`](include/mimalloc/memory-events.h).

## Integration contract for maintainers and coding agents

When changing or embedding this fork, preserve all of the following:

1. Profiler-internal allocations must use the raw OS-layer arena
   (`_mi_os_alloc`), never `mi_malloc`, C++ `new`, or Rust `GlobalAlloc`.
2. Every new C source file must be added to the normal CMake source list and to
   `src/static.c`. `src/profile.c` remains compiled to provide the OFF stubs and
   gates its implementation internally; profiler helper files and engine hook
   call sites must be guarded by `MI_PPROF`.
3. `MI_PPROF=OFF` must remove the profiler hooks and preserve upstream allocator
   behavior when independent memory-events tracking remains runtime-disabled.
   The memory-events API, hooks, and tests remain available in the OFF build.
4. `mi_prof_config_t`, `mi_prof_stats_t`, and `mi_memory_snapshot_t` stay
   size/version tagged and must be extended compatibly. Other public structs
   and function signatures must not be changed incompatibly.
5. Validate C changes on Ubuntu, Windows MSVC, Windows MinGW, and macOS with
   `MI_PPROF=ON`, plus an `MI_PPROF=OFF` build and the Rust workspace.
6. Never mix root C-core paths and `rust/` paths in one commit.

These rules are also summarized in `AGENTS.md` when that file is present in a
working checkout.

## Repository layout

```text
.
|-- include/ src/ test/ CMakeLists.txt  # mimalloc v3 C core and profiler
|-- README.md                           # fork and integration guide
|-- readme-upstream.md                  # upstream mimalloc documentation
`-- rust/
    |-- mimalloc-pprof/                 # allocator crate, safe API, raw FFI
    |   `-- vendor/                     # generated single-file C snapshot
    `-- xtask/                          # vendored-source regeneration checks
```

The repository root is mimalloc and retains upstream git history. The
`readme-upstream.md` rename avoids a Windows case collision with this file.
For upstream mimalloc build modes, overrides, options, and platform notes, see
[readme-upstream.md](readme-upstream.md). For the repository's fast local
development loop, see [docs/dev-loop.md](docs/dev-loop.md).

## Prior art and credits

- [microsoft/mimalloc](https://github.com/microsoft/mimalloc), by Daan Leijen
  (MIT).
- [microsoft/mimalloc#1266](https://github.com/microsoft/mimalloc/pull/1266),
  the sampled-allocation-hook design this fork builds on.
- [gperftools](https://github.com/gperftools/gperftools), whose `heap_v2`
  format is accepted by google/pprof.

## License

MIT, the same as upstream. See [LICENSE](LICENSE).
