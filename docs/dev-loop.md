# Native local verification (Linux)

`ci/verify_local.py` is a fast, parallel local mirror of the Linux-runnable subset of
CI: `.github/workflows/c-unit.yml`, `rust-native.yml`, `python-lint.yml`, and
`asan.yml`. It exists because the alternative -- running Release `MI_PPROF=ON` ctest,
`MI_PPROF=OFF` ctest, Debug `MI_DEBUG_FULL=ON` ctest, the guarded job, the shared-lib
job, the memory-gate, the diagnostic gates, the Rust workspace, and python-lint one at
a time by hand -- takes a long time and is easy to shortcut under time pressure. Each
config's cmake flags and env are copied verbatim from the workflow file it mirrors
(not re-derived), and `ci/tests/test_verify_local.py` parses the workflow files itself
and fails if a job's flags drift out of sync with the script.

```bash
uv run ci/verify_local.py                     # everything fast (slow ctest tier excluded)
uv run ci/verify_local.py --only release,lint  # just these configs
uv run ci/verify_local.py --slow               # also run the long-tail ctest tier
uv run ci/verify_local.py --list                # print the config + bundle tables
uv run ci/verify_local.py --jobs 8              # override the total build/ctest job budget
uv run ci/verify_local.py --keep-going          # run every config even after one fails
uv run ci/verify_local.py --selftest            # trivially fast dry-run, no real builds
```

Eleven configs run concurrently (`release`, `off`, `debug-full`, `guarded`, `shared`,
`bundle`, `memory-gate`, `diag`, `rust`, `lint`, `asan`), each building into its own directory
under `out/verify/<config>/` (gitignored, incremental across invocations) with Ninja
and ccache when available. `asan` needs `clang`/`clang++` on `PATH` and reports
SKIPPED with a reason otherwise. The long tests (`test-profile-race`,
`test-subproc-lifecycle`, `test-zero-tracking*`) are excluded from ctest by default
via `-E`; pass `--slow` to include them. On a 16-core machine with a warm cache,
expect on the order of a few minutes wall-clock for the fast tier -- well under the
sum of the per-config times, which the final table reports alongside the wall clock so
the parallel speedup is visible. A failed config prints the last ~40 lines of its full
log (`out/verify/<config>/verify.log`) inline.

## Reproducing a macOS or Windows CI bundle here

The macOS and Windows gates do not build on the platform they target: `macos-bundles.yml`
and `windows-bundles.yml` cross-compile the test binaries on Linux through soldr and ship
them to one runner per OS as a portable *test bundle* (#277). That means everything up to
the execution step is reproducible on this box, and `--bundle` does it:

```bash
uv run ci/verify_local.py --list                          # names, triples, cmake flags
uv run ci/verify_local.py --bundle macos-arm64-release
uv run ci/verify_local.py --bundle windows-gnu-x64-debug-full
```

Fourteen names, exactly the two workflows' build matrices:
`macos-{arm64,x64}-{release,debug-full,leak}` and
`windows-{gnu,msvc}-x64-{release,debug-full,shared,leak}`. Each one runs the same
`soldr prepare --target <triple>`, the same `cmake/toolchains/soldr-<triple>.cmake`, the
same matrix flags and the same `ci/bundle_tests.py` arguments CI uses, asserts the resolved
`-- Link libraries` line the build job asserts, and prints the bundle path, its manifest
summary and the `ci/run_test_bundle.py` command to replay it on the target OS. Output goes
to `out/verify/bundles/<name>/` (gitignored, incremental).

It cannot *run* the bundle -- that needs the target OS. Everything else fails here first:
a configure that picked up a host library, a link that lost `__interpose` or its TLS
directory, a bundle missing a toolchain runtime DLL, a manifest carrying an absolute path.
`ci/tests/test_verify_local.py` parses both workflow matrices and fails if a name, triple,
cmake flag or `bundle_tests.py` argument drifts.

# Fast local Linux build loop

On Windows, run the C and Rust test loops through `uv run ci/dev_linux.py`.
The source tree is a live, read-only Docker bind mount: its NTFS mtimes are
shared with the container and remain stable across container restarts. Build
trees and caches are Docker named volumes. Do not build into a host bind mount:
writing build output through that layer is what causes the costly mtime and
filesystem translation problems.

```powershell
uv run ci/dev_linux.py doctor
uv run ci/dev_linux.py c-test
uv run ci/dev_linux.py rust-test
uv run ci/dev_linux.py bench
uv run ci/dev_linux.py bench --reuse  # verify volumes after docker stop/start
```

`bench` is the acceptance check. It measures a cold run, three warm no-op C
runs, and one source edit. It fails when the warm median exceeds 60 seconds,
the edit exceeds 60 seconds, or the volume/no-op/cache invariants are absent.
The local C profile runs CTest's API tests and both upstream stress binaries
with at most four threads (their supported CLI arguments), matching the Docker
Desktop CPU allocation. Native CI remains responsible for the unmodified full
CTest suite.

Use PowerShell. Git Bash requires `MSYS_NO_PATHCONV=1`. Docker Desktop must use
the WSL2 backend and have at least four CPUs and 8 GiB available. `doctor`
checks container/host clock skew; over one second makes comparisons between
host-stamped source files and VM-stamped build output appear stale. A branch
switch also rewrites source mtimes; run `git restore-mtime` if the cache
matters. zccache hashes contents, so an mtime-driven rebuild should remain a
cache-hit path rather than a full recompilation.

For a cold-start recovery, run `uv run ci/dev_linux.py clean`, then
`uv run ci/dev_linux.py c-test`. `clean` deliberately removes the Docker
container and named volumes, so it should not be part of normal iteration.
Use `bench --reuse` after restarting the named container to verify that volumes
survive without deliberately wiping them first.

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| warm no-op takes minutes | host/VM clock skew over 1 s | Run `doctor`; enable Docker Desktop clock=host. |
| everything rebuilds after a branch switch | checkout rewrote mtimes | Run `git restore-mtime`. |
| every run builds an image | container reuse was bypassed | Use the script; only `up` builds images. |
| first run each session is cold | container/volumes were recreated | Restart the named container; do not run `clean`. |
| `docker: invalid working directory` | MSYS path conversion | Use PowerShell or set `MSYS_NO_PATHCONV=1`. |
| C compilation misses cache | launcher was omitted | Reconfigure with `CMAKE_C_COMPILER_LAUNCHER=zccache`. |
| Ninja rebuilds a fixed subset | generated outputs are retouched | Run `ninja -d explain -n` in `/target/c-build`. |

If Docker Desktop is unavailable, the documented fallback is a host-side soldr
cross-build followed by a slim Linux runtime container. It is slower because
the build runs on the Windows filesystem; run the Docker recovery tool first.

## Memory gate: fast local loop (#517)

Memory measurements are fine to take locally: a peak RSS on this box is stable to about
+-0.1 MB run to run. Timing and performance measurements are not -- a local number says
nothing about the runner, so CPU-cost questions go through the `perf-ab` label workflow.

Why this loop exists: #501 raised the memory gate's minimum peak from 58.2 MB to
61-63.7 MB and nobody noticed, because minimal-lane pushes skip the gate (#514, #517).
Check it yourself before pushing anything that touches the arena, purge or page paths.

One warm Release build, then rebuild only the gate binary:

```bash
cmake -S . -B build-mem -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DMI_PPROF=ON -DMI_MEMEVT=ON -DMI_DIAGNOSTICS=ON -DMI_BUILD_TESTS=ON
ninja -C build-mem mimalloc-test-memory-gate
```

Take the minimum of 3-4 runs and check it against the committed baseline:

```bash
rm -f gate-*.json
for i in 1 2 3 4; do
  MI_BENCH_JSON=gate-$i.json build-mem/mimalloc-test-memory-gate > /dev/null
done
uv run ci/memory_gate.py check gate-*.json
```

`check` warns that it got fewer than 8 runs; that is expected here. CI takes the minimum
of 8 runs and compares it with `ci/memory-baselines/linux-pprof1.json`, allowing +5%
(`PEAK_TOLERANCE`). With four runs the minimum is already within the noise of CI's.

Per-scenario numbers: `check` prints a table of the high-water mark after each scenario
(from each run's `scenarios` array), so the scenario whose line first jumps is the one that
moved the peak. To iterate on a single workload, set `MI_GATE_SCENARIO` to one of
`thread_churn`, `sawtooth`, `cross_thread_free`, `rolling_heaps` or `huge_churn`: the
warm-up still runs, then only that scenario, in a few seconds.

```bash
MI_GATE_SCENARIO=thread_churn MI_BENCH_JSON=one.json build-mem/mimalloc-test-memory-gate
```

A filtered run is not comparable to the baseline (the full battery's peak includes every
scenario), so `memory_gate.py check` refuses it with exit 2. Read its `scenarios` entry or
stdout line and compare it with the same filtered run on another commit.

Toggle a feature at run time before rebuilding anything:

| Variable | Effect |
| --- | --- |
| `MIMALLOC_RESIDENT_FIRST=0` | plain arena search only, no resident-first claiming (#493) |
| `MIMALLOC_ARENA_PURGE_MULT=<n>` | purge delay multiplier for arenas (#486) |
| `MIMALLOC_PAGE_RESERVE=0` | do not keep an exiting thread's empty large pages (#493) |
| `MIMALLOC_PURGE_HOLES=0` | no hole purging on `mi_on_thread_idle` |

Compile-time knobs are `#ifndef` defines (CLAUDE.md rule 9), so override them through
the C flags into a separate build dir, e.g.
`-DCMAKE_C_FLAGS=-DMI_RESIDENT_FIRST_MIN_SLICES=<n>`.

To bisect across merges, use ONE scratch worktree and one build dir inside it, not a
fresh checkout per commit:

```bash
git worktree add ../mimalloc-bisect <sha>
# configure ../mimalloc-bisect/build-mem once, as above; then per step:
git -C ../mimalloc-bisect checkout <next-sha>
ninja -C ../mimalloc-bisect/build-mem mimalloc-test-memory-gate
```

The exact regression check for #514 is a ctest in the same build:

```bash
ninja -C build-mem mimalloc-test-resident-first-churn
ctest --test-dir build-mem -R test-resident-first-churn --output-on-failure
```

It runs the gate's thread-churn pattern with the scavenger off and asserts that no fresh
(never-dirty) arena slice is claimed after the warm-up round, using the `MI_DIAGNOSTICS`
claim counters (`_mi_arena_claim_counters`). It needs `-DMI_DIAGNOSTICS=ON`.

To see *where* the memory went, call `mi_purge_holes_report()` (or
`_mi_purge_holes_report_collect` for the numbers) in the same `MI_DIAGNOSTICS` build: its
"arena layout" section (#519, `src/arena-layout.c`) classifies every arena data slice as
in use / fresh / free-dirty / queued / queued-aged from the arena bitmaps and, per chunk
size class (`mi_chunkbin_t`), counts the runs of each kind with power-of-two run-length
histograms. #514's signature was queued runs growing while small/medium claims spilled into
fresh chunks; that shows up here without a bisect. Without `MI_DIAGNOSTICS` the section is
absent and `mi_holes_report_t.arena_layout` stays zero.
