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

Configs run concurrently (`release`, `off`, `debug-full`, `guarded`, `shared`,
`bundle`, `memory-gate`, `diag`, `rust`, `lint`, `asan`, and `stress` and `tsan`, which have no CI
twin, #573), each building into its own directory
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

## Explaining an allocator change before timing it (#573)

Explain a memory or CPU change with event counts and residency first, and use `perf-ab` to
confirm it, not to explore. The safety nets that make that cheap:

- **Stats in the untimed replay.** A `perf-ab` dispatch with `holes_report` builds the replay
  with `MI_DIAGNOSTICS=ON` and `MI_STAT=1` and sets `MIMALLOC_SHOW_STATS=1`, so each row and arm
  prints the pages, abandoned, reclaim and retire counters next to `mi_purge_holes_report()`.
  Nothing timed is perturbed.
- **First-repetition sanity gate.** Repetition 0 of every row is compared before the rest run.
  A head arm whose peak RSS, RSS after drain or RSS at the release bound exceeds
  `1.5 x base + 8 MiB` fails the job at once (`SANITY_FACTOR`, `SANITY_SLACK_MIB` in
  `ci/perf_ab.py`). A dispatch that is meant to trade memory away sets `sanity_gate: false`.
- **One live run per PR.** `perf-ab` queues new pushes behind the running measurement
  instead of cancelling it (#585). Only the `perf-ab` label starts a labelled run;
  dispatches have independent concurrency groups. A `head_sha` dispatch always uses
  the dispatch ref's measurement harness, even when the measured allocator is older.
- **p99 latency.** A separate replay executable samples stream-operation latency
  (free or replacement, including page touches), using a deterministic power-of-two
  stride and at most 4096 samples per worker. The throughput/RSS executable has no
  latency clocks or sample buffers. Each paired repetition includes a latency replay;
  its p99 is reported and judged against the null-arm noise, with no memory credit.
- **Page-map check.** At `MI_DEBUG>=2`, `_mi_page_init` asserts that the page map resolves the
  first byte and the last byte of a page's blocks to that page. A re-carve that changes a page's
  geometry without re-registering it fails on the first local debug ctest, not in a
  multi-threaded perf run. `test-large-span` case (g2) re-carves every ordered pair of large
  sizes on one thread at a time.
- **`mi_theap_t` size budget.** `MI_THEAP_META_MAX_SIZE` (types.h) is a compile-time budget on
  `sizeof(mi_theap_t) + MI_PADDING_SIZE`: the meta-allocator size class it sits in is the edge
  that made CI ASan `test-resident-first-churn` flaky. Put new per-thread state in `mi_tld_t`.

## Perf campaign method (#575 retrospective)

The #575 campaign took `large-class-persistent/8` from 2.3-2.5x to about 1.0x jemalloc peak RSS
in two rounds; the method below is what worked. Data lives on #575 and PRs #582-#584 (#582's commits landed via #584); do not
copy it here.

1. **Step 0 is attribution, and earlier claims are hypotheses.** #575's issue text said
   `persistent` "has few retired pages"; the probe showed 43% of its peak RSS was resident,
   empty, retired large pages. Run `uv run ci/attribution_probe.py --reps 5` before designing
   anything. The tool is on `main` (landed via #584; #582 was closed as landed). It builds `MI_DIAGNOSTICS=ON MI_STAT=1`, runs `ci/perf_ab.c` rows `large-class/8`
   and the `sparse-large-buffers/8` twin with `PERF_AB_HOLES_REPORT=1`, and, with every worker
   still holding its live slots, has each print `mi_purge_holes_report()`, which `mincore`-walks
   every page. It is untimed, so it may run locally. Buckets (they sum to the snapshot RSS;
   peak equals snapshot within ~0.3 MiB):
   - *live requested* and *block rounding*: bytes the application asked for, and the rest of
     its blocks;
   - *formed free blocks*: carved, free, resident blocks in pages with a live block or in empty
     pages;
   - *empty (retired) pages, resident*: retired pages reset to `capacity = 0` whose old bytes
     are still resident (reported as "unformed");
   - *unformed tail*: resident bytes past `capacity` in pages that still have a live block
     (re-carved over dirty slices);
   - *page-geometry slack*: bytes past `reserved*block_size`, plus the header;
   - *free slices queued for purge* and *aged/dirty/fresh*: arena free-slice states (arena walk);
   - *outside the arena*: meta, stacks, binary (`smaps_rollup` for kernel RSS and
     AnonHugePages). Varies 27-33 MiB run to run for the queued bucket; the rest is stable to
     about 1 MiB. Compare ratios across hosts, not absolute MiB.
2. **Hotness predicts refault cost.** Round 1 returned retired pages to the OS and lost up to
   10x CPU (+107% to +1080%, refault ~190 us against a ~1 us op) because those bytes were the hot
   working set, reused within ~10 operations. Before any discard, decommit or purge policy,
   measure the reuse distance of the target bucket. Prefer zero-refault designs that make
   resident bytes fungible across bins, spans and sizes (#584: repurpose budget, 1 MiB span cap,
   `MI_LARGE_AGE_STEP`).
3. **Rejected, do not retry** (details on #575): discarding retired-page bodies at retire or at
   a heartbeat, with any keep count or age gate (#583, `mi_option_retired_keep`, default-off);
   raising the repurpose budget alone (saved 26%, CPU +17.1% against 8.6% allowed); a 2 MiB
   span cap; budget 256; aging on every large miss; never aging on a large miss
   (random-large/8 peak +12.3%); aging once per 8 generic mallocs (random-large/1 drain RSS
   +3.9%). Queued-slice reuse (H1) was already 98% resident-first; THP off saves only ~6%.
4. **Step 2, "CPU-model noise or real?" is settled by a same-host paired run.** Dispatch
   `benchmark-scaling` with `mode=diagnostic`, 15 blocks, the cells in question, and read the
   fork/upstream ratio-of-medians with a 95% block bootstrap. Fork, pinned upstream, jemalloc,
   TCMalloc and Bun run the same blocks on one VM, so the CPU model cancels. Result (#575
   comment 5913744857, run 36701407919): fork/upstream throughput 0.925 [0.878, 0.968] at
   persistent/8 and 0.89-0.96 across the large and larson cells, control 0.99; a real regression
   in the large/retire/purge paths. Follow-up on #585.
5. **Tooling traps.**
   - `benchmark-scaling` above 15 blocks blows the 1500 s shard budget on the 8-worker shard.
   - Pushes now queue behind in-flight `perf-ab` measurements; label and dispatch runs
     keep their isolated groups.
   - Artifacts from before #585 have no p99 measurement. Do not supply a synthetic
     latency value to re-judge them with the new harness.
   - The 8-worker cells run on a 4-vCPU EPYC 7763 VM (2 physical cores), 2:1 oversubscribed:
     they measure contention under oversubscription, not core scaling.
   - A local untimed residency probe is allowed; timing is CI-only (see the memory gate loop
     below).

### Accepting incremental performance improvements (owner decision 2026-10-21)

Small improvements are useful: there is no minimum percentage gain and a CPU optimization
need not also reduce RSS. Require a statistically supported gain on intended workloads,
representative controls, the fixed-baseline per-cell ledger, and green correctness/platform
gates. A PASS means the regression gate passed; it does not prove improvement or equality.
A beneficial point estimate whose interval crosses zero is not a demonstrated gain.

| Evidence | Disposition |
|---|---|
| Supported target gains; remaining metrics PASS | Accept a small improvement; report its size and scope |
| Limited INCONCLUSIVE ancillary metrics; CPU/throughput and controls PASS; no confirmed FAIL | Accept when the full intervals fit declared campaign risk bounds, or with explicit owner acceptance of the stated uncertainty; preserve INCONCLUSIVE labels |
| Wide uncertainty, missing relevant coverage, or an interval allowing a material scenario spike | Attribute or run a bounded, planned follow-up; do not claim equivalence |
| CPU/throughput FAIL beyond that cell's noise and permitted memory credit | Reject or revise, even if other workloads improve |
| p99 FAIL beyond noise, peak/drain RSS FAIL beyond noise, or faults beyond the existing per-cell allowance | Reject or revise; p99 receives no memory credit |
| Refault storm, declared service-bound violation, or correctness/platform failure | Reject or fix before acceptance; an average win cannot compensate |

Declare intended workloads, controls, sampling limits and material-risk/service bounds before
the campaign. Keep the existing 3:1 memory-credit rule; credit belongs only to the cell that
saved memory. Do not introduce a post-result tolerance, pool incompatible hosts, discard failed
runs, or retry unchanged code until a FAIL disappears. An INCONCLUSIVE result is neither PASS
nor a confirmed regression. Do not automatically stall a useful change or exhaustively rerun
all metrics just because a few remain uncertain. Record accepted uncertainty in the PR and
parent issue; otherwise narrow the unresolved risk with a specific attribution question.

**Accepted example: #585 / PR #609.** The owner accepted this candidate on 2026-10-21:
13 workload rows, 78 metric checks, 75 PASS, three INCONCLUSIVE, zero FAIL. Six workloads have
significant throughput gains and six have significant CPU/op gains; improved paths are roughly
1–3% faster or cheaper. All CPU/throughput checks pass. There is no established new memory win
and memory credit is zero. These counts are not an aggregate performance score.

| Accepted uncertainty | Paired estimate | 95% interval | Existing noise allowance |
|---|---:|---:|---:|
| Profiler-on large-class/8 p99 | +0.51% | -1.72% to +3.37% | 2.25% |
| Chart ephemeral/8 minor faults | +0.39% | 0% to +1.18% | 0.79% |
| Chart ephemeral/8 peak RSS | -0.003% | -0.089% to +0.117% | 0.097% |

The owner accepted those explicit risks; the ledger remains unchanged. This does not establish
universal tolerances or complete the remaining memory investigations. In contrast, the rejected
aging-setting-2 trial saved 3.9% peak RSS but worsened p99 by 9.8% [7.6%, 10.8%], against 1.4%
noise: the memory gain cannot buy that tail-latency spike. Earlier retired-body discard trials
cost +107% to +1080% CPU and remain rejected.

Evidence: [combined current ledger](https://github.com/zackees/mimalloc-pprof/issues/422#issuecomment-6329851624),
[full uncertainty ledger](https://github.com/zackees/mimalloc-pprof/issues/585#issuecomment-6328936369),
and [rejected aging trial](https://github.com/zackees/mimalloc-pprof/issues/585#issuecomment-6304942092).

### The perf-ab pipeline (#573 B3-B8)

```bash
# dispatch, wait, print the rule-12 ledger (the wrapper is the sanctioned way to wait)
uv run ci/perf_ab_dispatch.py --ref perf/my-branch --reps 15 --null-arm --max-reps 25
# several knob values in ONE rotation on ONE VM: variants split on ' || '
uv run ci/perf_ab_dispatch.py --ref perf/my-branch --head-env 'MIMALLOC_X=4 || MIMALLOC_X=16 || MIMALLOC_X=32'
# re-judge, or pool, downloaded artifacts (pooling refuses another CPU model or other commits)
uv run ci/perf_ab_dispatch.py --from-json a.json b.json
```

For one exact workload, prefix its full name with `=`:
`--workloads '=large-class-ephemeral/8'`. Unprefixed terms still match substrings;
`|` combines either form. This permits attribution of the plain row without also
selecting its chart variants. Long adaptive groups can approach the 90-minute job
limit with separate latency replays; select individual rows while preserving their
original parameters, seeds and touch policy.

- **Raw samples.** Every run uploads `perf-ab-samples` (`perf-ab-samples.json`): per-repetition
  values for every row and arm, the base and head SHAs, defines, environment, CPU model and the
  RSS floor. An atomic checkpoint is saved after each completed timing/latency pair,
  starting with an empty checkpoint before measurement. Checkpoints remain
  `complete: false` until the whole run finishes; interrupted or unbalanced data
  cannot receive a regression verdict or be pooled. Parent progress lines identify
  the workload, repetition and arm being measured. `ci/perf_ab_ledger.py` judges
  completed data; nothing is scraped from a log.
  CI also uploads `perf-ab-builds`: the exact timing and latency executables by arm/build
  kind, with SHA-256 digests. They are copied before the first measurement so forced
  CI termination can retain them even when Python cleanup never runs. A copy error
  aborts before measurements. Local runs opt in with
  `--preserve-builds <new-directory>`.
- **Arm order (#585).** Each block of `len(arms)` repetitions rotates every arm through
  every execution position; alternate blocks reverse direction. For three arms the six-run
  cycle covers all permutations. This removes the old endpoint-only reversal's confound,
  which always left head in the middle. Extra variants retain position balance, without
  guaranteeing all possible carryover pairings. Artifacts record `arm_order_protocol`;
  pooling refuses a different or missing protocol. Old failures remain evidence, and this
  correction alone does not explain them. Use complete blocks when selecting repetition counts.
- **Rule-12 ledger.** Per row: `saved%` of the reducible peak-RSS gap (against the RSS floor
  `ci/perf_ab.c` prints: the RSS before the work plus the live requested bytes), then for
  throughput, CPU, faults, peak and after-drain RSS the paired change, its allowance (`saved%/3`
  for the first three, noise for memory and for cells that saved nothing) and PASS, FAIL or
  INCONCLUSIVE. INCONCLUSIVE is not a statistical pass; delivery disposition follows
  the incremental-improvement guidance above without changing that verdict.
- **Honest intervals.** The interval on a median is the distribution-free sign-test interval, not
  a percentile bootstrap (anticonservative at 7 or 15 reps). Only the rule-12 metrics are marked.
- **Noise floor.** `null_arm` measures base a second time in the same rotation; a difference is
  marked only when it exceeds what base-vs-base produced. A control counts as unchanged when its
  whole interval lies inside +-`EQUIVALENCE_MARGIN_PCT` (3%, an owner decision that is still open).
- **Adaptive reps.** `max_reps` adds 5 reps at a time to the rows whose ledger is still
  INCONCLUSIVE, up to that many; conclusive rows stop at `reps`.
- **`fp` build.** `--force-kind fp` builds every row with `-fno-omit-frame-pointer`, for
  `perf record -g` and caller-report shims.
- **Linked-build attribution (#585).** Dispatch `perf-ab.yml` with `build_report_only=true`
  and exact `base` / `head_sha` revisions to preserve the plain timing and latency executables,
  static libraries, symbols, disassembly, hashes, compiler versions, runner image and CMake
  settings in `perf-ab-build-report`. It runs no workloads and produces no performance verdict;
  its metadata is explicitly incomplete for ledger purposes. These are fresh builds, so they
  cannot establish the exact layout of binaries discarded by an earlier measurement. Compare
  compiler/image identity before using their layout to explain an earlier result.

### Local tooling (#573 C)

The dev box is a loaded Ryzen 3700X: never time on it, but attribute on it.

- **Kernel symbols.** `kernel.kptr_restrict=0` (NixOS: `boot.kernel.sysctl."kernel.kptr_restrict" = 0;`)
  makes kernel samples (faults, madvise, THP zeroing) resolvable; tracefs access gives
  `perf trace -s` (mmap/madvise/munmap counts) and `perf probe`.
- **Instructions, not seconds.** `taskset -c 2 perf stat -r 10 -e instructions:u,cycles:u,minor-faults ./perf_ab ...`
  is largely immune to host load and answers "did this add user instructions per op" in minutes;
  `valgrind --tool=cachegrind` `Ir` is an exact pre-screen (use single-thread rows, or the
  scavenger off, for stable counts). Neither replaces perf-ab for cache contention or kernel time.
- **Call graphs.** `perf record --call-graph dwarf` plus `perf report --inline` replaces the
  frame-pointer rebuild and the `LD_PRELOAD` `clock_gettime` shim; the `fp` kind above covers
  `perf record -g`. On Zen2 use `ibs_op//`, not LBR.
- **Races that never show under gdb.** `rr record --chaos` (after `scripts/zen_workaround.py`) and
  `rr replay`; fallback `coredumpctl debug -A "-batch -ex 'thread apply all bt full'"`.
- **Layouts.** `clang -Xclang -fdump-record-layouts -fsyntax-only -Iinclude -D... src/static.c`,
  or `pahole`, per configuration; `MI_THEAP_META_MAX_SIZE` is the compile-time budget.
- **Not worth it here:** heaptrack, bytehound and massif profile an application's malloc calls,
  not the allocator's internals; magic-trace needs Intel PT; `perf c2c` needs `ldlat`, which this
  `ibs_op` lacks; coz and DAMON are too noisy for these questions.

### Debug stress lane (#573 D)

`uv run ci/verify_local.py --only stress,tsan` runs the multi-threaded perf_ab rows (large-class,
ephemeral generations, bursty idle drain, random-large, small, larson) at small operation counts
against a `MI_DEBUG_FULL` build and against a `clang -fsanitize=thread` build (`STRESS_OPS` in
`ci/verify_local.py`). A row fails on a non-zero exit, an assertion or a TSAN report. These are the
workloads that exposed #572's page-map extent and retired-slot mask bugs, which only the
label-gated release-build perf-ab job ran. The inherited TSAN row of `test.yaml` runs only on
`dev*` pushes and tags, so it never sees this fork's PRs.

**Diagnosis order: events before timing.** Explain a change with event counts and residency
first; use perf-ab to confirm, not to explore.

### Benchmark scaling: diagnostics, caches and alerts (#573 B7, B9)

- **Diagnostic sweeps take 15 blocks per cell in practice** (`MIN_DISPATCH_BLOCKS` in
  `ci/scaling_diagnostic.py` enforces exactly 15 blocks, before anything is built). A 5-block
  sweep left a 30-45 MiB spread per cell and produced a false +6-12% RSS regression; 15 resolved
  single cells. More than 15 blocks over the 8-worker shard exceeds the runner's 1500 s projected
  budget (#575: 30 blocks projected 2150 s, 40 blocks 1503 s, both failed before measuring); the
  dispatch input is capped at 15 (#585). Use 15, or narrow
  `diagnostic_patterns` / `diagnostic_threads` and pool runs.
- **Two allocator caches.** `benchmark-scaling.yml` restores the four reference allocators (TCMalloc,
  jemalloc, upstream and Bun mimalloc) keyed on their pins, patches and the builder only, and the
  fork's tree keyed on its own sources and the diagnostic defines, so a C change no longer rebuilds
  TCMalloc. `ci/check_benchmark_scaling_workflow.py` pins both keys.
- **A failing scheduled run opens an issue.** The `alert` job (schedule only) runs
  `ci/scaling_failure_alert.py`: one open issue, "benchmark-scaling: the scheduled run is
  failing", collects the run links and failed jobs of a streak; close it when a run succeeds. The
  RSS-floor validation break of 2026-09-28 failed every daily run for over a day unseen.
- **The RSS-floor rule cannot drift.** The producer (`scaling.rs`) and the validator
  (`benchmark_report.py`) decide floor consistency with `floor * 100 <= lowest * (100 + slack)`.
  `ci/check_benchmark_scaling_workflow.py` compares the constant and the formula on every `ci/` PR,
  and both test suites load `rust/benchmark-suite/tests/fixtures/rss_floor_vectors.json`.
- **Multi-threaded stress on PRs.** The `stress` job of `asan.yml` (labels `ci-test` / `ci-full`)
  runs `ci/verify_local.py --only stress,tsan`: the perf_ab large-size, generation and idle-drain
  rows under `MI_DEBUG_FULL` and under clang TSAN.

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

## Events, residency, probes and static checks (#573 A2-A5)

Explain a change with counts and residency before timing it. These need no source patch:

- **Event counters.** Build with `MI_DIAGNOSTICS=ON`; `mi_stats_print` and `mi_purge_holes_report`
  end with one line, e.g. `events (#573): large_page_request=65 large_repurpose=64
  large_repurpose_denied=1 ...` (fresh large-page requests, repurposes and denials, retired
  publish/unpublish, page-map register/re-extent, large-span grow/shrink, arena page alloc/free).
  The perf-ab `holes_report` replay prints them for both arms. `test-event-counters` uses them as a
  deterministic budget for the retire cascade of #572 (548K page requests against 292): within one
  heartbeat a thread repurposes at most `MI_LARGE_REPURPOSE_FRESH` retired pages.
- **Residency.** The arena layout walk prints `resident (mincore)` per kind (in use, fresh, dirty,
  queued, aged) next to what the bitmaps say, and the holes report lists per bin the empty pages,
  the retired pages and the RAM those hold. A "fresh" slice can be resident (THP faults in its whole
  2 MiB region); this replaces the scratch `mincore` probe.
- **A failed assertion** prints a backtrace (`backtrace_symbols_fd`, no allocation; `addr2line -e
  <binary>` resolves it without `-rdynamic`), and a page-map or geometry failure first prints the
  page with `_mi_page_debug_print`.
- **USDT probes.** `-DMI_USDT=ON` (needs `<sys/sdt.h>`, `systemtap-sdt-dev`) puts six probes in the
  slow paths (`page_fresh`, `page_repurpose`, `arena_page_alloc`, `arena_page_free`,
  `retired_publish`, `retired_release`); a probe is a NOP until a tracer attaches, so
  `perf stat -e 'sdt_mimalloc:*'` or `bpftrace -e 'usdt:./app:mimalloc:page_fresh { @[arg0] = count(); }'`
  answers "how often, with what arguments" without a rebuild. The default build has none
  (`ci/check_usdt_probes.py`).

Static checks that would have caught #572's bugs at compile time or on the first local run:

- **One writer of a page's geometry.** `block_size` and `reserved` change in `mi_page_set_geometry`
  (src/page.c), which owns the page-map re-registration; the arena writes them only when it creates
  or frees a page. `ci/check_page_geometry_writes.py` fails any other write to `page->block_size` or
  `page->reserved` under `src/`.
- **Retired-slot mask.** At `MI_DEBUG>=2` a publish asserts that the owner-private mask never claims
  fewer slots than the slots hold, and `_mi_page_unpublish_retired` takes the owner's tld from the
  caller (`_mi_page_free` clears `page->theap` first).
- **Struct size budget.** `ci/struct_size_budget.json` and `ci/check_struct_sizes.py` bound
  `mi_theap_t` (plus padding) and `mi_tld_t`, which sit at the edge of their size classes, in every
  configuration CI compiles; `MI_THEAP_META_MAX_SIZE` in types.h must equal the budget. To add a
  field to either: run the script; if it fails, the field does not fit -- put the state in the other
  struct or shrink something, and only then raise the budget and the constant together, with the
  size class checked.

**Finding (not fixed here).** A commit-stat reconciliation at `mi_process_done` (asserting
`stats.committed.current >= 0`) fails in 9 of the 84 debug-full tests, by up to 2.6 MB. It is not an
allocator bug: on POSIX the arenas are committed at reserve (every commit bit set), so `committed`
is never credited at claim time, while a debug build's purge decommits (`mprotect`, because
`_mi_prim_decommit_zero` uses `madvise` only when `!MI_DEBUG`) and debits `committed` for slices that
were never credited. Release builds never debit, so the statistic there is exact; on Windows the
arenas start uncommitted. The invariant therefore holds only where the debit path never runs, and
the check was not shipped.
