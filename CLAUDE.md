# mimalloc-pprof — agent guidance

Fork of microsoft/mimalloc adding pprof-compatible sampled heap profiling (Windows-first)
plus Rust crates in `rust/`. **All design decisions and work orders live in GitHub issues —
start at issue #2 (the epic), which links the ordered sub-issues.**

## Execution order

#10 (dev loop) → #4 → #5 → #6 → #7 → #8, with #9 (cross-CI) landing incrementally after #4.
Do not start a phase before its blocker is merged. Work from the sub-issue, not from memory;
if the sub-issue conflicts with older prose in #2, the sub-issue + #2's Decisions log win.

## Hard rules

1. **Never commit directly to `main`.** Feature branch → PR → merge. Branch names come from
   the sub-issue. One PR per phase. Conventional commits (`feat:`, `fix:`, `ci:`, `docs:`, `test:`).
2. **Never mix C-core paths (`src/`, `include/`, `test/`, `CMakeLists.txt`) and `rust/` paths
   in one commit.** This keeps upstream cherry-picks clean.
3. **Merge gates for every PR:** `c-unit` green on ubuntu/windows-MSVC/windows-MinGW/macos with
   `MI_PPROF=ON`, the `OFF` job green (profiler hooks disabled; upstream allocator behavior
   with independent memory-events tracking left runtime-disabled), `rust-native` green.
   MSVC **and** win-gnu are priority platforms — both, always.
4. **Profiler memory-safety invariant:** profiler-internal memory (sample records, intern table,
   dump buffers) comes ONLY from the raw-OS-layer arena (`_mi_os_alloc`), never from hooked
   allocation paths (`mi_malloc`/`operator new`/`GlobalAlloc`). Debug builds assert this.
5. **No new required dependencies for the C build** (no mandatory libunwind/protobuf/zlib).
6. New logic goes in new files (`src/profile*.c`, `include/mimalloc/profile.h`,
   `src/memory-events.c`, `include/mimalloc/memory-events.h`); edits to upstream files stay to
   a few guarded lines — `#if MI_PPROF` for the profiler hooks, unconditional (but tiny, one
   function-call each) for the always-on memory-events hooks in `src/alloc.c`/`src/free.c`.
7. **Escalate, don't improvise:** when reality diverges from a sub-issue (API drift, toolchain
   fights, unreachable threshold), comment on that issue with evidence and stop.

## Repo facts

- Repo root IS mimalloc (currently tracking the v3/`dev3` line; remote `upstream` =
  microsoft/mimalloc). The fork's `main` and `upstream/dev3` have unrelated histories:
  **never merge them directly and never use `--allow-unrelated-histories` or `commit-tree`
  parent rewriting.** For a v3 sync, fetch `upstream/dev3`, then follow an issue-scoped,
  reviewed selective C-engine overlay that reapplies the fork hooks and verifies protected
  path/Rust allowlists before push. Upstream's `readme.md` was renamed
  `readme-upstream.md` (Windows case-collision with `README.md`); carry its edits over
  deliberately rather than replacing the fork README.
- v3 replaced the old segment allocator (`src/segment.c`, gone) with an arena-of-slices
  allocator (`src/arena.c`) plus a page-map (`src/page-map.c`), and split per-thread state
  into `mi_heap_t` (the shared, non-thread-local heap grouping its theaps) + `mi_theap_t`
  (the thread-local owner of allocation pages, one per thread per heap) with a narrower
  `mi_tld_t`.
  Profiler/memory-events hooks that need per-thread scratch state (e.g. the profiler's
  sampling counters) live on `mi_theap_t`/`mi_tld_t`, not `mi_heap_t`.
- `src/static.c` is the single-TU amalgamation the Rust sys crate compiles — every new C file
  must be included there (guarded by `MI_PPROF` where appropriate) or Rust builds silently
  miss it.
- Fast local iteration: `python ci/dev_linux.py c-test | rust-test | bench` (issue #10) once
  landed. `bench` is the speed acceptance test; paste its output on #10 when touched.
- Upstreaming to microsoft/mimalloc: v3-targeted `pr/*` branches are cut from
  `upstream/dev3` (or its eventual renamed/default v3 branch), and receive only the
  fork-specific C diff; use `upstream/main` only for genuinely v2-compatible work until it
  becomes the v3 line. Cherry-pick only commits based on the matching upstream line. This is
  why rule 2 exists.
