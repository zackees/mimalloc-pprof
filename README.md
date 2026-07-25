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

For a checkout-to-checkout integration:

```toml
[dependencies]
mimalloc-pprof = { path = "../mimalloc-pprof/rust/mimalloc-pprof" }

[profile.release]
debug = "line-tables-only"
strip = false
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
   `src/static.c`; profiler-only files must be guarded by `MI_PPROF`.
3. `MI_PPROF=OFF` must remain an upstream-equivalent build. Memory-events hooks
   remain unconditional, tiny calls whose runtime work is opt-in.
4. Public profiler and memory-events structs stay size/version tagged. Extend
   them additively rather than changing existing fields or function
   signatures.
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
