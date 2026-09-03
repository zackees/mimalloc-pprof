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
3. **Merge gates for every PR:** `c-unit` green on ubuntu and windows-MSVC with
   `MI_PPROF=ON`, the `OFF` configuration green (profiler hooks disabled; upstream allocator
   behavior with independent memory-events tracking left runtime-disabled), win-gnu green in
   `windows-bundles.yml`, `rust-native` green. Since #307 `c-unit.yml` is two stages —
   every configuration built exactly once in `build`, then `run-linux` executes every
   bundle at once — so "the OFF job" is a `build` matrix row plus its slice of that wave,
   not a job of its own. A test that only passes with the machine to itself belongs in the
   `RUN_SERIAL` group in `CMakeLists.txt`, never behind a retry.
   MSVC **and** win-gnu are priority platforms — both, always.
   The **macOS** gate is `macos-bundles.yml` and uses no Apple hardware (#277 phase B2):
   both Apple arches are cross-built on Linux through soldr, `x86_64` is *executed* inside a
   macOS Recovery guest on a Linux runner (`run-macos-x64-recovery`), and `aarch64` is
   **compile-only** — a build plus Mach-O header assertions, with its test-name set checked
   against the executed x86_64 bundle. Never add a `macos-*` runner label to a workflow or
   to `azure-pipelines.yml`; `ci/lint_no_macos_runners.py` fails `python-lint` if you do.
   The guest boots macOS **Recovery** straight off the image every run via the
   `zackees/docker-mac-x64` action — no golden disk, no Actions cache, nothing to expire
   (the hand-built-image design and its keep-alive workflow are gone). Recovery has no
   dyld shared cache, so three profiler tests asserting that stack PCs resolve to loaded
   modules cannot pass there; they are not skipped — the full bundle runs and
   `ci/recovery_expected_failures.py` requires the failure set to be *exactly* those
   three, so both a new failure and one of them starting to pass are red.
   The **win-gnu** gate is `windows-bundles.yml` (#277 phase C): the bundles are cross-built
   on Linux by soldr's mingw-w64 and run on one Windows runner. Phase C changed *how*
   win-gnu is built, not whether it is tested — soldr's mingw-w64 is **UCRT**, while the
   native MSYS2 MINGW64 jobs it replaces were msvcrt, so those stayed informational for a
   ≥10-push window — which elapsed, so #307 deleted `c-unit.yml`'s three MSYS2 rows.
   Keep win-gnu gated; do not drop it to "MSVC covers Windows".
   The **MSVC** gate is split in two (#277 phase D), and the split is the rule, not an
   implementation detail. `windows-bundles.yml` cross-builds the MSVC-ABI bundles on Linux
   with soldr's **clang-cl** and runs them on the same single Windows runner as win-gnu —
   but **`c-unit.yml`'s `ctest (windows-latest)` (Release) stays native, stays built by
   Microsoft's `cl`, and stays a hard gate.** A clang-cl binary is not a `cl` binary:
   clang-cl accepts `__attribute__`s cl rejects, and cl's codegen, TLS lowering and DLL
   runtime are its own. Since #307 that gate is two jobs — `build-windows-native` compiles
   it with `cl`, `run-windows-native` runs the result and still reports under the name
   **`ctest (windows-latest)`** — and both halves are hard gates. Do NOT make either
   `continue-on-error` and do not delete them without amending this rule explicitly. Every
   *other* `windows-latest` row was informational for a ≥10-push window with a dated TODO;
   `c-unit.yml`'s share of those is gone (#307), the rest still stand. One further consequence:
   soldr's CRT splat has no `msvcrtd.lib`, so the cross lane is `/MD` only and cannot
   reproduce the native debug-full job's `/MDd` compile — it reproduces its defines
   (`MI_DEBUG=3`), which is what `MI_DEBUG_FULL` is for. See docs/ci-gates.md.
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

- Branch layout: `main` is the **v3** line (crate 0.9.x, overlay pinned to `upstream/dev3`
  commit `6def7be9`; was `bcee5a88` before the #266 bump, and `579f8c0e` before that (#80)).
  The previous v2 line is preserved on the **`v2`** branch and is what
  crates.io still serves as 0.8.x. Do not move the v3 overlay to a newer `dev3` without
  re-verifying the profiler hook patches — they only apply byte-identically against the
  pinned base. Bumped 2026-09-01 to Bun's merge-base for Bun parity (#264/#266); do not
  move again without re-verifying hooks.
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
- Fast local iteration: `uv run ci/dev_linux.py c-test | rust-test | bench` (issue #10) once
  landed. `bench` is the speed acceptance test; paste its output on #10 when touched.
  `uv run ci/verify_local.py` mirrors the Linux-runnable subset of CI (c-unit/rust-native/
  python-lint/asan) as ten concurrent configs; see `docs/dev-loop.md`.
- Upstreaming to microsoft/mimalloc: v3-targeted `pr/*` branches are cut from
  `upstream/dev3` (or its eventual renamed/default v3 branch), and receive only the
  fork-specific C diff; use `upstream/main` only for genuinely v2-compatible work until it
  becomes the v3 line. Cherry-pick only commits based on the matching upstream line. This is
  why rule 2 exists.
