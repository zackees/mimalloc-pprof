# For maintainers

*Part of the [mimalloc-pprof](../README.md) documentation.*

## Integration contract

When changing or embedding this fork, preserve all of the following:

1. Profiler-internal allocations must use the raw OS-layer arena
   (`_mi_os_alloc`) — never `mi_malloc`, C++ `new`, or Rust `GlobalAlloc`.
2. Every new C source file must be added to the CMake source list **and** to
   `src/static.c`. `src/profile.c` stays compiled to provide the OFF stubs and
   gates its implementation internally; profiler helper files and engine hook call
   sites must be guarded by `MI_PPROF`.
3. `MI_PPROF=OFF` must remove the profiler hooks and preserve upstream allocator
   behavior when memory-events tracking remains runtime-disabled. The
   memory-events API, hooks, and tests remain available in the OFF build.
4. `mi_prof_config_t`, `mi_prof_stats_t`, and `mi_memory_snapshot_t` stay
   size/version tagged and must be extended compatibly. Other public structs and
   signatures must not change incompatibly.
5. Validate C changes on Ubuntu, Windows MSVC, Windows MinGW, and macOS with
   `MI_PPROF=ON`, plus an `MI_PPROF=OFF` build and the Rust workspace.
6. Never mix root C-core paths and `rust/` paths in one commit — it keeps the C
   changes cherry-pickable upstream.

## Regenerating the vendored Rust source

The Rust package compiles
`rust/mimalloc-pprof/vendor/mimalloc-pprof-amalgamated.c`, **not** the root `src/`
tree. After an intentional C-core change, regenerate and validate it in a separate
Rust-only commit:

```sh
cd rust
soldr cargo run -p xtask -- amalgamate-c
soldr cargo run -p xtask -- amalgamate-h
soldr cargo run -p xtask -- check
soldr cargo test --workspace --locked
```

## Repository layout

```text
.
|-- include/ src/ test/ CMakeLists.txt  # mimalloc v3 C core and profiler
|-- README.md                           # project front page
|-- readme-upstream.md                  # upstream mimalloc documentation
|-- docs/                               # fork documentation (this file and siblings)
`-- rust/
    |-- mimalloc-pprof/                 # allocator crate, safe API, raw FFI
    |   `-- vendor/                     # generated single-file C snapshot
    `-- xtask/                          # vendored-source regeneration checks
```

The repository root is mimalloc and retains upstream git history. The
`readme-upstream.md` rename avoids a Windows case collision with `README.md`.

## Further reading

For upstream mimalloc build modes, overrides, options, and platform notes, see
[readme-upstream.md](../readme-upstream.md). For the fast local development loop, see
[dev-loop.md](dev-loop.md). The fixes prepared for submission back to
microsoft/mimalloc, with their validation evidence, are in
[upstreaming.md](upstreaming.md). Design history and milestone decisions are in
[issue #2](https://github.com/zackees/mimalloc-pprof/issues/2); the survey of other
mimalloc v3 forks is in
[issue #50](https://github.com/zackees/mimalloc-pprof/issues/50).
