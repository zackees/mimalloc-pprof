# Design: Allocator Shared-Library Cache

**Status:** Draft
**Date:** 2026-08-10
**Issue:** [#194](https://github.com/zackees/mimalloc-pprof/issues/194)

## Problem

The CI benchmark pipeline (`benchmark-stats.yml`) builds four native allocator
libraries from source on every run: tcmalloc (via Bazel), jemalloc (via
autoconf-make), upstream-mimalloc (via cmake-ninja), and mimalloc-pprof (via
cmake-ninja, from the workflow checkout).  This takes **20–30 minutes** in the
`build-and-measure` job (50-minute timeout) and is the dominant cost of every
benchmark run.  Nothing changes between runs — same pinned commits, same patches,
same toolchain — yet the entire build is repeated.

Separately, adding a new competitive allocator today requires touching five
places: the lockfile, a new C adapter, the Python builder's `ADAPTER_SYMBOLS` /
`COMPETITOR_SYMBOLS` / `adapter_include_directories` / `find_primary_library`
dispatch, `build.rs`'s `compile_adapter` match, and the Rust adapter allowlist.
The interfaces are correct but the dispatch points are scattered.

## Goals

1. **Cache compiled allocator artifacts between CI runs.**  Once built, reuse
   across runs until an input changes.
2. **Build each allocator as a shared library (`.so`)** exporting a stable,
   versioned ABI that the benchmark harness loads at runtime.
3. **Standardize the interface** so adding a new allocator means: one lockfile
   entry, one adapter C file, zero dispatch-table edits.
4. **Content-address the cache.**  Input hash → artifact.  No heuristic
   timestamp comparisons, no manual invalidation.
5. **Keep the existing security invariants:** every archive is checksummed before
   extraction, every symbol is verified post-link, and no second allocator
   contaminates a child process.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    allocator-lock.json                       │
│  Per allocator: source pin, patches, build system, flags    │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│              build_benchmark_allocators.py                   │
│                                                              │
│  For each allocator:                                         │
│   1. Compute cache key = SHA-256(                            │
│        source_tree_sha256 || patches[] || build_flags[] ||   │
│        toolchain_identity || adapter_source_sha256)          │
│   2. Lookup: cache/libbench_<id>_<key>.so                    │
│      → HIT:  verify symbols, copy to output, SKIP build     │
│      → MISS: build static lib → build .so → verify → cache  │
│   3. Write libbench_<id>.so to children/<id>/                │
│   4. Write allocator-provenance.json (with cache key)        │
└──────────────────────┬──────────────────────────────────────┘
                       │ libbench_<id>.so per allocator
                       ▼
┌─────────────────────────────────────────────────────────────┐
│               benchmark-child  (one universal binary)        │
│                                                              │
│  Runtime:                                                    │
│   1. Read BENCH_ALLOCATOR_LIBRARY_PATH env var               │
│   2. dlopen(libbench_<id>.so)                                │
│   3. dlsym → bench_alloc, bench_free, …                      │
│   4. Call bench_allocator_id() → verify identity             │
│   5. Process request (same stdin/stdout protocol)            │
└─────────────────────────────────────────────────────────────┘
```

### Why shared libraries instead of caching static children?

The current design produces four distinct `benchmark-child` binaries (one per
allocator), each statically linked.  We could cache those binaries and be done.
That is simpler but has two downsides:

1. **Not generic for future allocators.**  Each new allocator needs a Cargo
   rebuild of `benchmark-child` with different link flags.  The build.rs
   dispatch grows.
2. **The interface boundary is implicit.**  Static linking means the adapter ABI
   is verified post-hoc (`nm --defined-only`) rather than being the build
   contract.

A shared library makes the ABI the contract.  The benchmark harness links
*once* against `libloading` (or raw `dlopen`).  Any `.so` that exports the 8
adapter symbols is a valid allocator.  No Cargo rebuild, no link-flag dispatch,
no build.rs changes.

## Standardized Allocator ABI

### The contract (already exists — `allocator_adapter.h`)

```c
// Every allocator .so MUST export these 8 symbols with C linkage:
const char* bench_allocator_id(void);       // e.g. "tcmalloc", "jemalloc"
const char* bench_allocator_version(void);  // e.g. "c316de3e", "5.3.1@81034ce"
void*       bench_alloc(size_t size);
void*       bench_calloc(size_t count, size_t size);
void*       bench_realloc(void* ptr, size_t size);
int         bench_aligned_alloc(void** out, size_t alignment, size_t size);
void        bench_free(void* ptr);
size_t      bench_usable_size(void* ptr);
```

Semantics: identical to the current statically-linked contract.  `NULL` from a
pointer-returning function is an allocation failure.  `bench_aligned_alloc`
returns POSIX error codes.  `bench_usable_size(NULL)` returns 0.

### Adapter source files (already exist)

| Adapter | Allocator API wrapped |
|---|---|
| `adapter_tcmalloc.cc` | `TCMallocInternalMalloc`, … |
| `adapter_jemalloc.c` | `je_malloc`, … (`--with-jemalloc-prefix=je_`) |
| `adapter_mimalloc.c` | `mi_malloc`, … |

The adapter is a thin shim (30–50 lines).  Adding a new allocator = writing one
of these.  The `BENCH_ALLOCATOR_ID` and `BENCH_ALLOCATOR_VERSION` macros are
supplied at compile time.

## Building a Shared Library

### From static archive to `.so`

The current build produces a static archive (`.a`/`.lo`).  To make a shared
library, we need one extra link step per allocator:

```
# Step 1: Build native allocator static library (unchanged)
#   → libtcmalloc.lo, libjemalloc.a, libmimalloc.a

# Step 2: Compile adapter to position-independent object
cc -c adapter_<id>.c -o adapter.o -fPIC -O3 -fno-omit-frame-pointer \
   -DBENCH_ALLOCATOR_ID="<id>" -DBENCH_ALLOCATOR_VERSION="<version>" \
   -I<include_dirs>

# Step 3: Link shared library
cc -shared -o libbench_<id>.so adapter.o \
   -Wl,--whole-archive lib<allocator>.a -Wl,--no-whole-archive \
   -Wl,--version-script,exports.map \
   [extra deps: -lstdc++ -pthread -ldl  (tcmalloc only)]
```

Key details:
- `-fPIC` throughout (the adapter is already compiled with `-fPIC` today; the
  native libraries need it too — jemalloc's autoconf and mimalloc's CMake do
  this by default for shared builds; tcmalloc's Bazel `-c opt` includes it).
- `--whole-archive` wraps the allocator archive so every object file is pulled
  in.  Without this, the linker drops unreferenced symbols since nothing in
  `adapter.o` directly calls the internal allocator functions — it calls the
  public API which then calls internals.
- **Symbol visibility:** A linker version script (`exports.map`) restricts
  exported symbols to exactly the 8 `bench_*` symbols.  This prevents the
  allocator's internal `malloc`/`free`/`mi_*`/`je_*` from leaking into the
  process's symbol table and accidentally interposing on the system allocator or
  another `.so`.

### Linker version script (`rust/benchmark-suite/native/exports.map`)

```
{
    global:
        bench_allocator_id;
        bench_allocator_version;
        bench_alloc;
        bench_calloc;
        bench_realloc;
        bench_aligned_alloc;
        bench_free;
        bench_usable_size;
    local:
        *;
};
```

### Post-link verification (replaces `validate_link_identity`)

Instead of `nm --defined-only` on the child binary, we verify the `.so`:

1. `nm -D --defined-only libbench_<id>.so` — must show exactly 8 `bench_*`
   symbols in the `T` (text) section.  All other symbols must be absent from
   the dynamic symbol table (verified by `nm -D` showing nothing else in `T`).
2. `readelf -d libbench_<id>.so` — must not have `NEEDED` entries for another
   allocator (tcmalloc, jemalloc, mimalloc).
3. `readelf -s libbench_<id>.so` — the competitor symbol (`TCMallocInternalMalloc`,
   `je_malloc`, `mi_malloc`) must be present but with `LOCAL` binding (not
   `GLOBAL`).
4. Spawn `benchmark-child --adapter-smoke` with `BENCH_ALLOCATOR_LIBRARY_PATH`
   pointing at the `.so` — must return the expected identity JSON with positive
   checksum and `usable_size >= 128`.

### Per-allocator linker specifics

Each allocator needs slightly different flags for the shared-library link step:

**tcmalloc** (Bazel, C++ public API, thin archive):
```bash
c++ -shared -o libbench_tcmalloc.so adapter_tcmalloc.o \
   -Wl,--whole-archive \
   $(cat link-manifest.txt | awk -F'\t' '{print $2}') \
   -Wl,--no-whole-archive \
   -Wl,--version-script,exports.map \
   -Wl,--exclude-libs,ALL \
   -lstdc++ -pthread -ldl -lm
```
- `--exclude-libs,ALL` hides symbols from all static archives (belt-and-suspenders
  with the version script; prevents any symbol from leaking if the version script
  has a typo).
- `-lstdc++ -pthread -ldl -lm` are tcmalloc's transitive system deps (baked into
  the `.so` as `NEEDED` entries; these are glibc/libstdc++, not other allocators,
  so they pass the `readelf -d` check).

**jemalloc** (autoconf-make, prefixed C API, regular archive):
```bash
cc -shared -o libbench_jemalloc.so adapter_jemalloc.o \
   -Wl,--whole-archive source_dir/lib/libjemalloc.a \
   -Wl,--no-whole-archive \
   -Wl,--version-script,exports.map \
   -Wl,--exclude-libs,ALL \
   -lpthread
```
- jemalloc is self-contained except for `-lpthread`.
- `--with-jemalloc-prefix=je_` already prevents symbol conflicts; the version
  script is the primary guard.

**upstream-mimalloc / mimalloc-pprof** (cmake-ninja, C API, regular archive):
```bash
cc -shared -o libbench_<id>.so adapter_mimalloc.o \
   -Wl,--whole-archive build_dir/libmimalloc.a \
   -Wl,--no-whole-archive \
   -Wl,--version-script,exports.map \
   -Wl,--exclude-libs,ALL \
   -lpthread -lrt
```
- mimalloc is nearly self-contained; `-lpthread -lrt` on Linux.

**Why `--exclude-libs,ALL` plus a version script?**  The version script is the
primary mechanism (it controls the dynamic symbol table).  `--exclude-libs,ALL`
is a secondary guard: it tells the linker to hide symbols from static archives,
preventing them from becoming dynamic exports even if the version script has an
error.  This is defense-in-depth; the version script alone is sufficient, but
the flag is free.

## Content-Addressable Cache

### Cache key computation

For each allocator, the cache key is:

```
cache_key = SHA-256(
    source_tree_sha256      ||  # SHA-256 of the patched source tree
    source_patches_sha256   ||  # SHA-256 of concatenated patch file digests
    build_flags_canonical   ||  # sorted, semicolon-joined build flags
    toolchain_identity      ||  # "cc=<version>;cxx=<version>;ar=<version>"
    adapter_source_sha256   ||  # SHA-256 of the adapter .c/.cc file
    allocator_adapter_h_sha256 ||  # SHA-256 of the ABI header
    lockfile_sha256            # SHA-256 of allocator-lock.json
)
```

Every input is deterministic.  Same inputs → same key → same `.so`.  Changing
any input (source upgrade, new patch, flag tweak, compiler upgrade) produces a
different key and triggers a rebuild.

### Cache layout

```
rust/benchmark-suite/allocators/cache/
├── cache-manifest.json          # maps cache_key → {id, version, created, .so sha256}
├── libbench_tcmalloc_<key>.so
├── libbench_jemalloc_<key>.so
├── libbench_upstream-mimalloc_<key>.so
└── libbench_mimalloc-pprof_<key>.so
```

The `cache-manifest.json` records metadata for diagnostics and garbage
collection (old entries pruned on each build, keeping at most the last 3 per
allocator).

### Output (the "current" symlink)

After a successful build, the script writes a symlink (or copies the file) from
the cache to the output directory:

```
build_root/children/<id>/libbench_<id>.so  →  cache/libbench_<id>_<key>.so
build_root/allocator-provenance.json       →  (unchanged schema + cache_key field)
```

The `.so` path is recorded in `allocator-provenance.json` alongside the existing
fields.

### CI integration

The `build-and-measure` job uses `actions/cache@v4`:

```yaml
- name: restore allocator shared libraries
  id: cache-so
  uses: actions/cache@v4
  with:
    path: rust/benchmark-suite/allocators/cache/
    key: allocator-so-${{ runner.os }}-${{ hashFiles('rust/benchmark-suite/allocators/allocator-lock.json') }}-${{ hashFiles('rust/benchmark-suite/native/allocator_adapter.h') }}-${{ hashFiles('rust/benchmark-suite/native/adapter_*.c') }}-${{ hashFiles('rust/benchmark-suite/native/adapter_*.cc') }}
```

The `hashFiles` approach works but is coarse — it lumps all allocators into one
cache key.  A single adapter change invalidates all four.  For more precision,
the Python builder can compute per-allocator sub-keys and use `actions/cache`
with `restore-keys` for partial hits:

```yaml
- name: restore allocator cache
  uses: actions/cache/restore@v4
  with:
    path: rust/benchmark-suite/allocators/cache/
    key: allocator-so-${{ runner.os }}-${{ hashFiles('...') }}
    restore-keys: |
      allocator-so-${{ runner.os }}-
```

The Python builder then checks which cache keys resolved, rebuilds only the
misses, and the final `actions/cache/save@v4` step saves the union.

**Cache size:** Each `.so` is roughly 5–15 MB.  Four allocators × 3 versions =
~120 MB.  GitHub Actions cache is capped at 10 GB per repo, so this is
comfortable.

## Dynamic Loading in benchmark-child

### New crate dependency

Add `libloading = "0.8"` to `Cargo.toml` (pure Rust, no system deps beyond
`dlopen`/`dlsym`, already available on Linux).

### New module: `src/loader.rs`

```rust
use libloading::{Library, Symbol};
use std::path::Path;

type BenchAllocFn = unsafe extern "C" fn(usize) -> *mut std::os::raw::c_void;
// ... (one type alias per adapter symbol)

pub struct DynamicallyLoadedAdapter {
    #[allow(dead_code)]  // must stay alive for symbol lifetime
    library: Library,
    alloc: Symbol<'static, BenchAllocFn>,
    calloc: Symbol<'static, BenchCallocFn>,
    // ...
}

impl DynamicallyLoadedAdapter {
    pub fn load(path: &Path) -> Result<Self, AdapterError> {
        unsafe {
            let lib = Library::new(path)?;
            // Load each symbol...
            // Verify identity via bench_allocator_id()
        }
    }
}
```

### Changes to `child.rs`

```rust
fn run_measurement() -> Result<(), String> {
    let lib_path = std::env::var("BENCH_ALLOCATOR_LIBRARY_PATH")
        .map_err(|_| "BENCH_ALLOCATOR_LIBRARY_PATH required")?;
    let adapter = DynamicallyLoadedAdapter::load(Path::new(&lib_path))?;
    // ... rest unchanged (same request/response protocol)
}
```

### Changes to `build.rs`

When `BENCH_ALLOCATOR_LIBRARY_PATH` is set at build time (it won't be — it's a
runtime variable), or rather: we add a new cfg for the dynamic mode.

Actually, the clean approach: the `benchmark-child` binary is **always** built
in dynamic mode.  The statically-linked path stays for testing but is no longer
the production path.  The `build.rs` link-env variables (`BENCH_ALLOCATOR_LIBRARY`,
`BENCH_ALLOCATOR_INCLUDE_DIRS`, `BENCH_ALLOCATOR_LINK_MANIFEST`) become unused
for `benchmark-child` and are only needed for the `.so` build (which the Python
builder handles).

For the `benchmark-child` binary, `build.rs` needs no special link flags — it
just needs `libloading` in `Cargo.toml`.  The `cfg(benchmark_native_adapter)`
becomes `cfg(benchmark_dynamic_child)` and is set unconditionally when building
for the benchmark target.

### Adapter trait unification

The existing `LinkedAdapter` (static) and new `DynamicallyLoadedAdapter`
(dynamic) both satisfy `AllocatorAdapter` from `execution.rs` (after adding
`unsafe` bounds where needed).  The dynamic path is the production path; the
static path is kept for unit tests and the `unlinked-test-adapter` dev identity.

## Changes to Existing Files

### New files

| Path | Purpose |
|---|---|
| `design/allocator-so-cache.md` | This document |
| `rust/benchmark-suite/native/exports.map` | Linker version script for symbol visibility |
| `rust/benchmark-suite/src/loader.rs` | Dynamic adapter loading via `libloading` |

### Modified files

| Path | Change |
|---|---|
| `ci/build_benchmark_allocators.py` | Add `build_shared_library()` step; cache key computation; cache lookup/store; shared-library post-link verification; write cache manifest. The `build_child()` function shrinks (no more per-allocator Cargo builds). |
| `rust/benchmark-suite/Cargo.toml` | Add `libloading = "0.8"` dependency |
| `rust/benchmark-suite/build.rs` | Remove link-env handling for `benchmark-child` (the child no longer statically links allocators). Keep the unlinked identity for `cfg(test)`. The `benchmark-child` binary compiles with `cfg(benchmark_dynamic_child)` always set. |
| `rust/benchmark-suite/src/adapter.rs` | Add `DynamicallyLoadedAdapter` (or keep in `loader.rs`). Keep `LinkedAdapter` under `cfg(test)`. |
| `rust/benchmark-suite/src/child.rs` | Accept dynamic adapter; use `BENCH_ALLOCATOR_LIBRARY_PATH` env var. |
| `rust/benchmark-suite/src/lib.rs` | Add `pub mod loader;` |
| `rust/benchmark-suite/src/orchestration.rs` | `ChildProgram.program` stays the same (still points to `benchmark-child` binary). The `.so` path is passed via `ChildProgram.environment` as `BENCH_ALLOCATOR_LIBRARY_PATH`. |
| `rust/benchmark-suite/src/runner.rs` | `children_from_provenance` reads the `.so` path from provenance instead of per-allocator child binary paths (or reads both; the child binary is now one shared binary, the `.so` is per-allocator). |
| `rust/benchmark-suite/src/provenance.rs` | Add `shared_library: String` and `shared_library_sha256: String` fields to `AllocatorProvenance`. |
| `.github/workflows/benchmark-stats.yml` | Add `actions/cache@v4` restore/save steps for the `.so` cache directory. |
| `rust/benchmark-suite/allocators/allocator-lock.json` | Add `shared_library: "libbench_<id>.so"` field to each record (or keep it derived). |

### Files that do NOT change

The adapter C sources (`adapter_*.c`, `adapter_*.cc`, `allocator_adapter.h`)
are unchanged — they already implement the correct ABI.  The lockfile structure
is unchanged except for the optional `shared_library` field.  The benchmark
workload execution (`execution.rs`, `scenarios.rs`) is unchanged — it talks to
the adapter trait, not to C directly.  The validation pipeline (`validate.rs`,
`report.rs`, `benchmark_report.py`) is unchanged.

## Extensibility: Adding a New Allocator

With this design, adding a new allocator (e.g., `snmalloc`, `hoard`,
`microsoft-mimalloc-v2`) is:

1. **`allocator-lock.json`** — add a new record: `id`, `pin`, `source` (kind,
   repo, commit, archive URL + sha256), `build` (system + commands + flags),
   `expected_static_library`, `adapter_kind`, `license`, `patches`.
2. **`native/adapter_<id>.c`** — write a 30-line adapter that maps the 8
   `bench_*` symbols to the allocator's public API.
3. **`native/exports.map`** — no change needed (it exports `bench_*`, not
   allocator-specific symbols).

That's it.  No dispatch-table edits in Python, no `build.rs` changes, no Rust
source changes (the Rust side doesn't know allocator IDs at compile time).  The
Python builder discovers the adapter via the `adapter_kind` field or by
convention (`adapter_<id>.c`).

### Dispatching the adapter compilation

The Python builder needs to know which source file to compile for the adapter.
Options:

**Option 1 (convention):** `native/adapter_<id>.c` or `native/adapter_<id>.cc`.
The builder looks for the file by convention.  If it exists and is `.cc`, use
`CXX`; if `.c`, use `CC`.  This is what `build.rs` already does.

**Option 2 (explicit):** Add `adapter_source: "adapter_snmalloc.c"` to the
lockfile record.  More explicit, but redundant with the convention.

**Recommendation: Option 1 (convention).**  The lockfile already has
`adapter_kind` for semantic categorization; the file name follows from the
allocator ID.

### ABI forward-compatibility

The ABI can be versioned by adding new symbols with a version suffix:

```c
// v2 adds (hypothetical):
int bench_aligned_alloc_v2(void** out, size_t alignment, size_t size, unsigned flags);
```

The loader probes for versioned symbols and falls back to v1.  The lockfile can
declare `"adapter_abi_version": 2`.  This is future work — not needed for the
initial implementation.

## Cache Invalidation Rules

A cache entry is invalidated (rebuilt) when ANY of these changes:

| Input | How detected | Granularity |
|---|---|---|
| Allocator source commit | `source.commit` in lockfile changes | Per-allocator |
| Source archive checksum | `source.archive_sha256` in lockfile changes | Per-allocator |
| Source patches | Patch file SHA-256 in lockfile changes, or patch `.patch` file content changes | Per-allocator |
| Build flags | `build.flags` list changes | Per-allocator |
| Build commands | `build.commands` change (e.g., new cmake flag) | Per-allocator |
| Adapter source | SHA-256 of `adapter_<id>.c`/`.cc` | Per-allocator |
| ABI header | SHA-256 of `allocator_adapter.h` | **All allocators** |
| Lockfile schema | `cache_key` includes `lockfile_sha256` | **All allocators** |
| Compiler version | `cc --version` output changes | **All allocators** |
| OS / libc | Not in key — cache is CI-runner-specific | Per runner image |

Cache entries are **never shared between different runner OS images** (the
`${{ runner.os }}` prefix in the CI cache key handles this).

## Migration Plan

### Phase 1: Build `.so` side-by-side (non-breaking)

1. Extend `build_benchmark_allocators.py` to build `.so` files in addition to
   static children.  No caching yet — just prove the shared-library build
   produces correct allocators.
2. Add `libloading` to `Cargo.toml`, write `loader.rs`.
3. Add a CLI flag to `benchmark-child` to use dynamic loading (opt-in).
4. Verify: smoke test with dynamic loading produces identical checksums to
   static linking.

### Phase 2: Add caching

1. Implement cache key computation and cache lookup/store in Python builder.
2. Add `actions/cache@v4` to CI workflow for the `cache/` directory.
3. Set `SOURCE_DATE_EPOCH=0` in the build environment to improve cache hit
   rates (suppresses embedded timestamp variation; not required for cache
   correctness but eliminates the most common source of false cache misses).
4. Run CI.  First run: cache miss, full build.  Second run: cache hit, under
   2 minutes for the allocator build step (just symbol verification).
5. Assert that cached runs produce functionally equivalent `.so` files (same
   exported symbols, same adapter smoke checksum).  Bit-identical
   reproducibility is NOT required for cache correctness — the cache key
   captures all semantic inputs, and a nondeterministic difference simply
   causes a rebuild on the next run (acceptable, not a bug).

### Phase 3: Switch production to dynamic loading

1. Make dynamic loading the default for `benchmark-child` in CI.
2. Keep static linking for unit tests.
3. Remove the per-allocator static child build from the Python builder (or keep
   behind a `--static-children` flag for debugging).

### Phase 4 (future): Extend to new allocators

1. Add `snmalloc` as a proof-of-concept fifth allocator.
2. Verify: only lockfile + one adapter `.c` file needed.

## Resolved Design Questions

### 1. dlopen symbol interposition risk ✅ RESOLVED

**Question:** When we `dlopen("libbench_tcmalloc.so")`, do the allocator's
internal symbols (`TCMallocInternalMalloc`, `malloc`, `free`, etc.) risk
interposing on the system allocator that Rust itself uses?

**Answer: No, the risk is eliminated by two independent mechanisms.**

**Mechanism A — `RTLD_LOCAL` (libloading default):** `libloading::Library::new()`
calls `dlopen(path, RTLD_NOW | RTLD_LOCAL)` on Linux.  `RTLD_LOCAL` means
symbols from the loaded library are NOT added to the global ELF symbol table.
The process's existing resolution of `malloc` → glibc (established at startup)
is unaffected.

**Mechanism B — Linker version script (`local: *;`):** The version script marks
every symbol except the 8 `bench_*` entries as `local`.  In ELF, `STB_LOCAL`
symbols are excluded from the dynamic symbol table entirely.  They are used for
internal resolution within the `.so` at link time, but have zero visibility
outside.  `dlsym(handle, "malloc")` returns `NULL` — the symbol is invisible.

**How internal calls resolve within the `.so`:**

```
bench_alloc()                  ← GLOBAL (exported)
  → TCMallocInternalMalloc()   ← LOCAL (internal to .so)
    → tcmalloc guts            ← LOCAL
      → malloc()               ← LOCAL (resolved at .so link time to tcmalloc's own malloc)
```

External calls (Rust `Vec::push`, `String::new`, etc.) continue resolving
`malloc` → glibc, unchanged.  The two allocators coexist in the same process
without conflict: Rust/`std` uses glibc; the benchmark workload uses the loaded
`.so` exclusively through the `bench_*` adapter symbols.

**C++ static initializers:** When tcmalloc's C++ constructors run during
`dlopen`, any `operator new` calls within the `.so` resolve to tcmalloc's own
`operator new` (LOCAL, resolved at `.so` link time), not libstdc++'s.  This is
correct — allocator bootstrap must use its own heap.

**Verification test:** After dlopen, `Vec::with_capacity(1024)` must return a
pointer from a different heap region than `bench_alloc(1024)`.  The sanitizer
test: allocate via system, write sentinel, free; allocate via adapter, write
different sentinel; assert addresses are in disjoint regions.

### 2. Bazel thin archives and `--whole-archive` ✅ RESOLVED

**Question:** tcmalloc's `libtcmalloc.lo` is a Bazel-produced thin archive.
Does `--whole-archive` work correctly with it for shared-library linking?

**Answer: Yes — GNU ld and lld both natively resolve thin archive members
through `--whole-archive`.**

Bazel's `.lo` files are standard Unix thin archives (`!<thin>\n` magic).  A
thin archive contains absolute paths to `.o` files rather than embedded object
data.  When the linker encounters `--whole-archive` before a thin archive, it
reads the archive index, resolves each path to its `.o` file, and includes
every object in the link — same behavior as with regular archives.

**The approach (no Bazel BUILD changes needed):**

1. `bazel build //tcmalloc:tcmalloc` — produces `libtcmalloc.lo` + all `.o`
   files in the execution root (paths recorded in the thin archive index).
2. `bazel cquery` + Starlark formatter — discovers the transitive archive
   closure (as the Python builder already does in `tcmalloc_link_inputs()`).
3. `bazel build <all-owners>` — ensures every `.o` in the closure is built.
4. Link the `.so` using all discovered archives with `--whole-archive`:

```bash
cc -shared -o libbench_tcmalloc.so adapter.o \
   -Wl,--whole-archive \
   /path/to/execroot/bazel-bin/tcmalloc/libtcmalloc.lo \
   /path/to/execroot/bazel-bin/.../libabsl_*.a \
   ... \
   -Wl,--no-whole-archive \
   -Wl,--version-script,exports.map \
   -lstdc++ -pthread -ldl -lm
```

The resulting `.so` is self-contained — no dependency on the thin archive or
its referenced `.o` paths after linking.

**Constraint:** The `.o` files must exist at their absolute paths during the
link step.  This is satisfied because we link immediately after `bazel build`,
in the same build session.  The cached `.so` is independent of the ephemeral
Bazel execution root.

**Alternative considered (and rejected for now):** Using Bazel's `cc_binary`
with `linkshared = True` or `cc_shared_library`.  This would require either
patching the adapter source into tcmalloc's tree or creating a composite Bazel
workspace.  More Bazel-idiomatic but more invasive; the direct-link approach
works with zero Bazel rule changes.

### 3. Cache directory location ✅ RESOLVED

**Decision: Per-checkout at `rust/benchmark-suite/allocators/cache/`.**

Rationale:
- The cache is small (~120 MB for 3 versions × 4 allocators, each `.so` is
  5–15 MB).
- Per-checkout avoids version-skew: different branches may pin different
  allocator commits, and a shared cache would need sophisticated versioning.
- The same path works identically in CI (GitHub Actions `cache` action
  saves/restores this directory between runs) and locally (gitignored).
- Added to `.gitignore` as `rust/benchmark-suite/allocators/cache/`.

A user-global cache (`~/.cache/mimalloc-pprof-bench/`) is not needed: the
typical developer has one checkout, and CI runners are ephemeral anyway.

### 4. `SOURCE_DATE_EPOCH` and reproducible builds ✅ RESOLVED

**Decision: Set `SOURCE_DATE_EPOCH=0` in the build environment.  Do NOT
require bit-identical reproducibility for cache operation.**

Bit-identical `.so` reproducibility is NOT required for the cache to work
correctly.  The cache key captures all semantic inputs (source trees, patches,
flags, toolchain).  If a `.so` differs due to nondeterministic build artifacts
(embedded timestamps, build paths), the cache simply won't match and the build
will rerun — wasted time but not a correctness bug.

However, setting `SOURCE_DATE_EPOCH=0` costs nothing and improves cache hit
rates by suppressing timestamp variation:

| Source of nondeterminism | Mitigated by `SOURCE_DATE_EPOCH`? |
|---|---|
| `__DATE__` / `__TIME__` macros | Yes — reproducible standard defines RFC 5425 §4.3 |
| ELF `.comment` section timestamps | Yes — GNU ld respects it |
| Debug info `DW_AT_comp_dir` | N/A — builds use `-O3` without `-g`, no debug info |
| Build ID (`--build-id`) | N/A — already a content hash, deterministic |
| Tool-specific randomness | No — but none of our build tools (cmake, ninja, make, bazel) introduce randomness at `-c opt` |

**EDGE CASE:** C++ `__DATE__`/`__TIME__` are not used by any of our allocator
builds (they're compiled `-O3` with no timestamp-dependent logic), but
`SOURCE_DATE_EPOCH` is standard practice for reproducible builds and has zero
downside.  Set it and forget it.

### 5. dlopen failure fallback behavior ✅ RESOLVED

**Decision: Fail fast with a clear, debuggable error message.  No retry,
no fallback, no silent degradation.**

**Error path:**

1. `DynamicallyLoadedAdapter::load(path)` calls `Library::new(path)`.
2. On failure → returns `Err(AdapterError::LoadFailed { path, reason })`.
3. `child.rs` maps it to a stderr message and `process::exit(2)`.
4. The runner (`orchestration.rs:293-298`) captures stderr and the exit code,
   producing an error like:

   ```
   cell aborted at block 0 ordinal 0 allocator tcmalloc:
   benchmark child exited unsuccessfully: status=exit code: 2,
   stderr=failed to load allocator library libbench_tcmalloc.so:
   libbench_tcmalloc.so: cannot open shared object file:
   No such file or directory
   ```

5. The entire cell is aborted (all-or-nothing semantics already in place).
   No partial data enters the raw run.

**Why no retry:** The runner handles retries at the cell level via the
calibration/block protocol.  Multiple failed `dlopen` attempts would waste
time — the root cause (missing file, wrong arch, ABI mismatch) won't resolve
itself.

**Why no fallback to system allocator:** That would silently produce wrong
measurements.  A cell that measures "glibc disguised as tcmalloc" is worse
than a failed cell — it poisons the benchmark history with invalid data.

**Common failure modes and their causes:**

| Error | Likely cause |
|---|---|
| `ENOENT` (no such file) | Cache miss + build failure; `.so` path misconfigured in provenance |
| `ENOEXEC` (ELF error) | Wrong architecture (arm64 .so on x86_64 runner); corrupted cache entry |
| Undefined symbol | ABI mismatch — `.so` was built against a different `allocator_adapter.h` |
| `NEEDED` dependency missing | Allocator dynamically links a system lib not present on the runner (violates our "static-only" invariant; post-link verification catches this) |

## Validation Strategy

Before merging:

1. **Adapter smoke test** — the existing `--adapter-smoke` mode works with
   dynamic loading.  Identical checksum to static linking proves equivalence.
2. **Symbol visibility** — `nm -D libbench_<id>.so | grep ' T '` shows exactly
   8 `bench_*` symbols and nothing else.
3. **Cache determinism** — run `build_benchmark_allocators.py --build-root /tmp/a`
   and `--build-root /tmp/b` with identical inputs; assert `.so` files are
   bit-identical.
4. **No interposition** — a test that dlopens an allocator `.so` then calls
   `malloc(64)` through Rust's `Vec::with_capacity(64)` and asserts the pointer
   did NOT come from the loaded allocator.
5. **CI green** — `c-unit` with `MI_PPROF=ON` and `MI_PPROF=OFF`, `rust-native`
   on ubuntu/windows-MSVC/windows-MinGW/macos all pass (the dynamic-loading
   path is Linux-only like the rest of the benchmark suite).

## References

- `rust/benchmark-suite/native/allocator_adapter.h` — the existing ABI
- `rust/benchmark-suite/native/adapter_mimalloc.c` — reference adapter
- `ci/build_benchmark_allocators.py` — current Python build pipeline
- `rust/benchmark-suite/build.rs` — current static-linking logic
- `rust/benchmark-suite/src/adapter.rs` — current `LinkedAdapter`
- `.github/workflows/benchmark-stats.yml` — CI workflow
- [Issue #194](https://github.com/zackees/mimalloc-pprof/issues/194) — this
  handoff issue
- [`actions/cache@v4`](https://github.com/actions/cache) — GitHub Actions cache
  action
