# Changelog

## 0.9.3

- Fix the Rust crate's bundled allocator build on `aarch64-pc-windows-msvc` by using
  C11 atomics for that target, avoiding unsupported MSVC C atomic syntax (#223).
- Carries the v4 line's work, merged into `main`
  ([#129](https://github.com/zackees/mimalloc-pprof/pull/129)) after 0.9.2 shipped:
  - the upstream engine pin moves from `579f8c0e` to `bcee5a88`
    ([#80](https://github.com/zackees/mimalloc-pprof/issues/80),
    [#113](https://github.com/zackees/mimalloc-pprof/pull/113));
  - experimental zero-tracking behind `mi_option_purge_zeroes`
    ([#67](https://github.com/zackees/mimalloc-pprof/issues/67),
    [#79](https://github.com/zackees/mimalloc-pprof/pull/79)) -- the new public option
    the 0.9.1 notes deferred to v4;
  - the zeroing-`realloc` family exposed from Rust, with profiler accounting tests
    ([#83](https://github.com/zackees/mimalloc-pprof/issues/83),
    [#108](https://github.com/zackees/mimalloc-pprof/pull/108));
  - allocator fixes: `mi_subproc_destroy` leaked the subproc's own main heap, the TLS
    slot array is allocated from the main heap and zeroed after growth, and
    `mi_process_info`'s `current_rss` reported committed bytes on Linux
    ([#128](https://github.com/zackees/mimalloc-pprof/issues/128),
    [#78](https://github.com/zackees/mimalloc-pprof/issues/78));
  - the sampling decision no longer takes the global lock
    ([#78](https://github.com/zackees/mimalloc-pprof/issues/78)).

## 0.9.2

One behavioural fix: **seeded sampling is now actually reproducible.**

- **Fix: `mi_prof_start_seeded` and `MIMALLOC_PROF_SEED` were not deterministic**
  ([#91](https://github.com/zackees/mimalloc-pprof/issues/91)). The per-thread sampling
  PRNG seeded from `prof_seed ^ (uintptr_t)tld` -- the *address* of the thread's
  allocator state, which ASLR randomises -- so the same seed produced a different sample
  sequence in every process, while the documentation promised "deterministic sampling,
  for repeatable tests". It now derives from the thread's creation ordinal
  (`mi_tld_t.thread_seq`), which is per-thread without being address-dependent.

  If you pinned a seed to get repeatable profiles, **0.9.1 and earlier did not give you
  one.** This does.

  The guarantee is bounded, and the README now says so: runs reproduce when thread
  *creation order* is deterministic. Threads racing to allocate remain only
  statistically repeatable.

  Covered by `test-prof-seed-determinism`, which spawns itself and compares two
  processes -- an in-process loop shares an address layout and would have passed while
  the bug was live. Verified to fail without the fix on Linux and macOS.

## 0.9.1

Hardening release. No API changes -- everything here is a fix or a CI gate.

- **Fix: shared-library builds were broken.** Four test targets hardcoded
  `mimalloc-static`, so a `-DMI_BUILD_SHARED=ON -DMI_BUILD_STATIC=OFF` build failed to
  link with `cannot find -lmimalloc-static` (#62, #68). This is the one that unbreaks
  someone.
- Fix: `-Wmaybe-uninitialized` on `fmt_buf` in `src/profile.c` (#72).
- `MI_PROF_CONFIG_OVERRIDE` is now tested against a hostile ambient environment for
  *every* env-backed field, not just `sample_interval` (#65, #73). The documented way to
  be immune to process-global `MIMALLOC_*` is now covered by tests rather than by
  assertion.
- New CI gates, each with a positive control proving it can actually fail: peak-memory
  and thread-counter regression gate with per-platform baselines (#63), CPU-baseline
  instruction scanner for portable builds (#64), AddressSanitizer with an
  allocator-mediated use-after-free control (#86), vendored-amalgamation drift guard
  (#88), and `ruff` + `pyright --strict` over the CI scripts (#74).

Four of those gates were found to be silently checking nothing when first written; the
controls are why that was noticed. See the README's "CI gates" section.

## 0.9.0

First release of the **v3 line**. The profiler and the memory-events API are ported onto
mimalloc v3 (`upstream/dev3`) and published as 0.9.x, while 0.8.x keeps serving the v2
engine from the [`v2`](https://github.com/zackees/mimalloc-pprof/tree/v2) branch. The
public API, environment variables, and pprof output are unchanged from 0.8.x -- moving
between the lines is a version bump, not a code change.

- **Sampling profiler and memory-events ported to v3**
  ([#29](https://github.com/zackees/mimalloc-pprof/issues/29),
  [#44](https://github.com/zackees/mimalloc-pprof/pull/44)). v3 replaced the segment
  allocator with an arena-of-slices allocator plus a page-map and split per-thread state
  into `mi_heap_t` / `mi_theap_t`, so every hook site moved.
- **v3 promoted to mainline**, v2 preserved on the `v2` branch
  ([#46](https://github.com/zackees/mimalloc-pprof/pull/46)).
- **New: allocator statistics in the profile.** `mi_prof_stats_t` mirrors the v3
  allocator counters (`MI_PROF_STAT_VERSION` 3) and Rust surfaces them as
  `ProfStats::heap`, a `HeapStats` struct
  ([#43](https://github.com/zackees/mimalloc-pprof/issues/43)). `tests/t15_heap_stats.rs`
  doubles as the ABI guard for that layout mirror -- without it `prof::stats()` would
  silently return all zeros on a size/version mismatch rather than fail to compile.
- **Fix: thread-exit cleanup never ran on Windows/MinGW** (#43). Every exiting thread
  leaked its thread-local heap and all its pages -- about 0.24 GB per `test-stress`
  iteration. A defect in upstream mimalloc, not in the profiler; see the README's
  "Bugs fixed in older versions".
- **Fix: `mi_heap_new` / `mi_subproc_new` did not bootstrap the library** (#43) when
  either was the first mimalloc call in a process, so both allocated from a still-`NULL`
  `subproc->heap_main`. Also an upstream defect, and not debug-only.
- Test coverage for degenerate memory patterns, a leak regression guard, aligned
  allocations, and empty-profile dumps
  ([#49](https://github.com/zackees/mimalloc-pprof/pull/49),
  [#51](https://github.com/zackees/mimalloc-pprof/pull/51)).

## 0.8.0

First real release of `mimalloc-pprof`.

- mimalloc allocator (Windows-first fork of microsoft/mimalloc), drop-in `mi_malloc`/`mi_free`/`mi_realloc`.
- pprof-compatible statistical sampling heap profiler (`mi_prof_start`/`mi_prof_start_ex`,
  `mi_prof_dump`/`mi_prof_dump_proto`).
- Opt-in memory-event accounting/callback API (`mi_memory_tracking_set_enabled`,
  `mi_memory_set_callbacks`, issue #20).
- Single-file C amalgamation (`vendor/mimalloc-pprof-amalgamated.{c,h}`) for non-CMake
  consumers, shipped as a release ZIP alongside the crates.io publish.

0.9.0 is reserved for a v3 mimalloc port (issue #29, in progress); 1.0 lands after further
stress testing.
