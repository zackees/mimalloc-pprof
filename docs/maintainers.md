# For maintainers

*Part of the [mimalloc-pprof](../README.md) documentation.*

## External-fork Actions approval

GitHub Actions must require maintainer approval before running workflows from **every**
external fork contributor, including returning contributors. The repository setting is
not versioned, so audit it after ownership or repository-setting changes:

```sh
gh api repos/zackees/mimalloc-pprof/actions/permissions/fork-pr-contributor-approval
```

The required response is:

```json
{"approval_policy":"all_external_contributors"}
```

If it differs, restore the policy with:

```sh
gh api --method PUT \
  repos/zackees/mimalloc-pprof/actions/permissions/fork-pr-contributor-approval \
  -f approval_policy=all_external_contributors
```

This policy applies to fork authors without repository write access. Collaborator PRs
continue to run normally. For a live audit, open one PR from a first-time external fork
author and one from a returning external fork author. In both cases GitHub must wait for
maintainer approval before scheduling any `pull_request` workflow; after approval, the
normal workflow set must run.

Keep untrusted workflows on `pull_request`, with least-privilege `permissions`. Audit
trigger changes with:

```sh
rg -n 'pull_request_target|^permissions:|^[[:space:]]+permissions:' .github/workflows
```

There must be no `pull_request_target` workflow that checks out or executes fork code.
That trigger runs in the base repository's security context and is not an alternative to
the repository-level approval policy.

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

## Manual macOS check before a release (real Mac required)

Apple's `leaks`, `vmmap`, `heap` and `malloc_history` are base-OS binaries that the
macOS Recovery guest CI uses does not carry (measured on #353: none of the five is
present in a 13.0 Recovery image, and they are not in the Command Line Tools package
either). CI drives the same `enumerator` entry point those tools call
(`test-osx-zone-introspect`, `test-osx-zone-introspect-remote`, both in the selective
Recovery lane), so the residual risk is the tools' own plumbing. Before tagging a
release that touched `src/prim/osx/**`, run this once on any Mac with a full macOS
install and paste both outputs in the release PR:

```sh
cmake -B build -DMI_OVERRIDE=ON -DMI_OSX_ZONE=ON && cmake --build build
leaks --atExit -- ./build/mimalloc-test-osx-zone-introspect
./build/mimalloc-test-osx-zone-introspect-remote & vmmap $!
```

Expected: `leaks` lists a zone named `mimalloc` with non-zero nodes and reports the
384 live blocks the in-process test still holds at exit; `vmmap` shows the arena as a
`MALLOC` region attributed to that zone. A zone that reports zero nodes means the
enumerator regressed to upstream's empty stub.

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
