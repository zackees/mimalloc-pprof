<!-- grind-425-proposal -->
## #425 proposal: large-buffer RSS toward jemalloc/TCMalloc without a throughput loss

This proposal follows the #425 findings comment above.

**Baseline.** Run [36160378572](https://github.com/zackees/mimalloc-pprof/actions/runs/36160378572), source `6c93b7ba`. Every number without an "expected" label comes from that run.

**Mechanisms the findings point to.** They are strongly indicated but not proven:

- **M1: per-bin slack in 4 MiB large pages (about 84-512 KiB requests).** Each thread keeps one 4 MiB large page per size bin. Pages that churn are never swept.
- **M2: freed singleton slice runs above 512 KiB.** These stay resident for the 400-800 ms arena window or fragment the slice space.

**What this builds on.** It extends existing work and does not repeat it:

- #477 / #482: R1 16 KiB purge units, which make large pages hole-eligible, plus the busy sweep and the thread-exit sweep.
- #483: publication of retired large pages.
- #484 / #493: page reserve and resident-first reuse.
- #486 / #500: the 400-800 ms arena retention window.
- #491: the release-bound ratchet.

**Out of scope.** The owner's benchmark plan (power-of-two and random workloads, 40 repetitions, P5-P95 bands) was implemented and published by #424, so it is not re-planned here.

### 1. Step 0: diagnostics that must run before any code

**Prerequisite: CI plumbing (diagnostic only, no shipped default changes).**

- **benchmark-scaling on `main`.** `.github/workflows/benchmark-scaling.yml`'s `workflow_dispatch` accepts only `mode`, `run_seed` and `blocks`. Setting an environment variable on the workflow step would not help either: the child is spawned with `env_clear()` (`rust/benchmark-suite/src/scaling.rs`, `run_scaling_child_with_plan`), and its `ChildProgram.environment` is always empty (`runner.rs:331`).
- **perf-ab on `main`.** `perf-ab.yml` accepts `base`, `reps` and `workloads`. The `head_env` input (space-separated `KEY=VALUE`, applied to the head arm only) belongs to the open #506 work and is not on `main`.
- **Compile-time defines.** Neither workflow can vary one.

So these small diagnostic-dispatch changes come first. Each is its own PR, and none touches the allocator:

- **P1.** Land `head_env` on perf-ab (#506), or an equivalent.
- **P2.** Add a perf-ab `head_cppdefs` input. `ci/perf_ab.py`'s `build()` would pass it as `-DMI_EXTRA_CPPDEFS=<value>` to the head arm only; `CMakeLists.txt:50` already supports `MI_EXTRA_CPPDEFS`.
- **P3.** Add perf-ab rows:
  - exact-size and bin-edge rows, using the existing `(threads, generations, min, max, ops, pause, larson)` tuple with `min == max`, which `ci/perf_ab.c` already handles;
  - a log-uniform stream mode in `ci/perf_ab.c`, so `sparse-large-buffers` has a perf-ab twin.
- **P4.** Add a benchmark-scaling `mode: diagnostic` that uploads only the raw artifact, never publishes, and takes two inputs:
  - `diagnostic_env`: plumbed into the mimalloc-pprof child's environment only;
  - `diagnostic_cppdefs`: applied to the mimalloc-pprof build only.

  The other four allocators run unchanged as same-run references.
- **P5.** Extend the live-telemetry diagnostic replay to `sparse-large-buffers` (today it runs only for `is_distribution()` patterns, `scaling_runner.rs:310` at `6c93b7ba`). Add phase markers to the RSS samples: setup, warmup, measured block, drain, teardown.

**Experiments.** Screen at 1 and 4 workers first, then confirm across 1/2/3/4/6/8. 6 and 8 are labelled oversubscribed. Use perf-ab for paired CPU, minor faults, peak and after-drain RSS; use benchmark-scaling for the worker sweep with same-run references.

| ID | Experiment (exact knobs) | Where | Confirms | Refutes |
|---|---|---|---|---|
| E1 (M1/H1) | Head arm built with `MI_EXTRA_CPPDEFS=MI_ENABLE_LARGE_PAGES=0`, which sets both `MI_MEDIUM_MAX_OBJ_SIZE` and `MI_LARGE_MAX_OBJ_SIZE` to 64 KiB (`types.h:600-601`). Patterns: `large-class-persistent`, `sparse-large-buffers`, `random-large`, `power-of-two-large`. | P2 + P4 | The RSS slope drops mainly in `large-class-persistent` and `sparse-large-buffers` (toward the 3.0 and 24.1 MiB/worker live slopes), with little change in `random-large` (89% singletons). Throughput and faults are recorded as the cost of the diagnostic, not as a candidate. | No drop, or an equal drop in `random-large` (then the cause is arena-wide, not large-page geometry). |
| E2 (M2/H4) | `MIMALLOC_PURGE_DELAY=0`, `10`, `100` (default, `options.c:143`) and `1000`, with `MIMALLOC_ARENA_PURGE_MULT=4` held at its default. Effective arena windows are synchronous, 40 ms, 400 ms and 4 s (`mi_arena_purge_delay`, `arena.c:2699-2709`). Separate arms: `MIMALLOC_ARENA_PURGE_MULT=1`, and `MIMALLOC_RESIDENT_FIRST=0` with the default delay. | P1 + P4 | RSS is monotone in the delay on `random-large` and at 1 worker (97.6 MiB against 22.3 MiB live), with minor faults rising as the delay falls. That confirms retention. If RSS stays high at short delays while `RESIDENT_FIRST=0` moves it, fragmentation or first-fit reuse is indicated instead. | RSS flat across the delays: retention is not the cause, and slice fragmentation is next. |
| E3 (M1/H1/H2) | Exact sizes 64 KiB, 128 KiB, 512 KiB, 1 MiB and 4 MiB. Bin-edge probes: 80 KiB and 80 KiB + 1 byte (the last medium bin against the first large bin, 96 KiB, per #477's bin list), and 512 KiB and 512 KiB + 1 byte (large page against singleton). The diagnostic prints each probe's effective bin and page kind, so the edges are confirmed, not assumed. Run with the large-page define at 1 and at 0. | P3 (+ P2) | RSS per same-time live byte jumps across the 80 KiB edge and falls across the 512 KiB edge with the define at 1, and the steps vanish at 0. | Smooth across the edges. |
| E4 (H8/H3) | Synchronized live-byte replay of `sparse-large-buffers` with phase-marked RSS. A fixed-aggregate-budget arm (64 slots in total, split over the workers). An allocator-internal snapshot in a separate replay, outside timing: `mi_purge_holes_stats_get` / `mi_purge_holes_report()` for resident free blocks in large pages, unformed tails, retired and reserved pages, and queued-for-purge slices. | P4 + P5 | This splits sparse's 30.7 MiB/worker excess between M1 and M2 and finds the phase in which the fork's maximum occurs (the findings show it is often after the drain). | If most of the excess sits in neither bucket, re-open H5 and H7. |

**Cost.** Diagnostic runs use existing options and defines only. The shipped defaults do not change.

**Decision rule.** Implement design A only if E1 and E3 confirm M1, and design C only if E2 and E4 confirm M2. If neither is confirmed, post the evidence here and stop.

### 2. Candidate designs

All candidates must:

- keep memory return on by default, under the existing `purge_holes` / `purge_delay` options;
- keep the alloc/free fast path byte-identical. Nothing goes in front of the observer flag test, and all new work stays in the generic-malloc housekeeping and free-collect slow paths (CLAUDE.md rule 6);
- allocate nothing. Any sweep-internal state lives in `mi_page_t`'s cold fields or in BSS. Profiler memory still comes only from `_mi_os_alloc` (rule 4).

#### A. Release cold free blocks in churning 4 MiB large pages (M1)

**The gap.** Large pages are already hole-eligible (R1, `mi_page_purge_unit`, `internal.h:1458`), and the owner already sweeps them while busy (#482). But the busy sweep skips a page whose free-list head or `(capacity, used)` moved since the last tick (`page-holes.c:710-714`), and that also skips the unformed-tail trim at `:716`. mimalloc also alternates `free` and `local_free`, so steady churn rotates through every formed block. As a result no block in an active page is ever cold for a whole tick.

**The design.** Every block in a large page is at least 84 KiB, which is 21 or more OS pages and 5 or more 16 KiB purge units, so a per-block discard amortizes well.

- **A1: address-ordered reuse for large bins only.** When `_mi_page_free_collect` folds `local_free` into `free` in the slow path, re-thread the free list of a large-class page in address order. A 4 MiB page holds at most about 48 blocks. Allocation then takes the lowest free block, live blocks cluster at the start of the page, and the high blocks stay untouched.
- **A2: per-block decay, which is #477's original T2 rule.** #482 simplified it to a page signature. Store a 64-bit "free at the previous tick" snapshot per large page; it fits because there are at most 48 blocks. At the busy tick, discard each free block that was also free at the previous tick, even when the page as a whole changed. A tight alloc/free loop never leaves its hot block free across two ticks, so it never re-faults.
- **A3: tail trim.** At the same tick, trim the unformed tail of a page carved from resident slices once its `capacity` has not grown for a tick. Blocks that were never formed are never re-faulted by that page. This follows the #493 note (`page-holes.c:543-548`) that trimming at page creation only re-faults.

**Alternatives.**

- A': discard only the blocks above the page's recent `used` high-water mark. This is simpler, but without A1 it misses cold blocks at low addresses.
- #477's S1: hand cold, non-full large pages to the abandoned pool so other threads reuse them. It is held back and stays a follow-up if A leaves per-thread slack.

**Expected effect (expectation, not a measurement).**

- `large-class-persistent` bounds what A can do. Its excess over the live slope is 23.2 - 3.0 = 20.2 MiB/worker, and its excess over jemalloc is 23.2 - 6.6 = 16.6 MiB/worker.
- For `sparse-large-buffers` only 43% of requests are in this class, so A is expected to remove somewhere between a few MiB and about 20 MiB per worker. That is up to about 480 -> 340 MiB at 8 workers. E4 narrows this range before any code.
- The persistent and ephemeral large-class charts (199.7 and 219.8 MiB at 8 workers) would benefit directly.

#### B. Size-bin consolidation for the large class (M1)

**The idea.** Fewer, coarser large bins: for example 96/128/192/256/384/512 KiB, 6 instead of 11. Or let a thread's first page in a low-occupancy bin be a 512 KiB medium page, and move to a 4 MiB page only when that one fills.

**Trade-off.**

- Internal fragmentation rises from at most about 12.5% to up to about 33% per block. Most of the extra space is address space, not RSS, because the harness and most applications do not touch the rounding tail.
- Coarser bins change `_mi_bin` and the `MI_BIN_HUGE == 73` invariant (`types.h:258`), which is a large upstream divergence.
- A mixed-span bin re-creates the "bin implies span" hazard #477 found in #443.

**Expected effect (expectation).** Per-thread large-page slack scales with the bins in use, so halving the bins would at most halve M1's share.

**Ranking.** Behind A. Pursue B only if A leaves measurable slack.

#### C. Singleton pages above 512 KiB (M2)

- **C1: best-fit resident reuse.** `mi_arena_try_claim_resident` (`arena.c:254`) takes the first queued resident run that fits, within `MI_RESIDENT_FIRST_MAX_TRIES` = 8. Prefer the smallest sufficient run, with an exact slice-count match first. Then random sizes stop splitting large resident runs and leaving unusable remainders.
- **C2: churn-aware purge deadline.** Keep the 400-800 ms window (#486/#500) by default, but purge a freed singleton range at its first deadline instead of its second when the arena's resident queued bytes exceed a budget proportional to the recent peak live singleton bytes. The #491 bound (`_mi_release_bound_ms`) can only get tighter under this, never looser, so `ci/release_ratchet.json` keeps holding.

**Expected effect (expectation).** C targets `random-large`'s 20.2 MiB/worker excess and the fixed 1-worker offset: 97.6 MiB against 41.3 for jemalloc and 50.3 for TCMalloc. Removing the excess would take `random-large` at 8 workers from 380.5 to about 240-246 MiB, below jemalloc's 284.3. That range comes from subtracting 7 x 20.2 from the 8-worker point, or from refitting with the live slope.

**Risk.** Re-faults. #500 measured about 120,000 minor faults at `arena_purge_mult=1` against about 1,300 at 4 on the bursty row. C2 must hold the `random-large-bursty/8` perf-ab row's faults.

#### D. Rejected

| Option | Why it is rejected |
|---|---|
| Globally shorter `purge_delay` / `arena_purge_mult`, or `purge_delay=0` | Re-faults under churn. Measured on #477 at an older SHA: about 99.6% throughput loss at 0. It also contradicts #486 and #500. It stays a diagnostic only. |
| Shipping `MI_ENABLE_LARGE_PAGES=0` | Every 64-512 KiB request becomes its own slice run, so arena traffic rises and throughput is lost. It stays a diagnostic only (E1). |
| #443's thread-count-driven 1 MiB spans | Timing-dependent geometry and mixed-span bins (#477). |
| Owner gate on by default | About +30% per single-thread malloc/free pair (#477). |

#### Combined expectation and residual gap (expectations)

**If A and C together remove all of `sparse-large-buffers`' 30.7 MiB/worker excess:**

- 8 workers would drop from 481.7 MiB to about 238-267 MiB (refit intercept plus live slope, or subtracting 7 x 30.7). That is below jemalloc's 298.4.
- TCMalloc's 156.8 MiB would still be 81-110 MiB away. The residual has two parts:
  - the 1-worker offset: 91.6 against 50.4 MiB;
  - TCMalloc's slope of 14.7 MiB/worker, which is below the 24.1 MiB/worker published live slope. The published live slope is a sum of per-worker peaks, while TCMalloc shares large free memory across threads.
- Reaching TCMalloc would take cross-thread reuse of large free memory (S1, or a shared large-object pool), which v3's per-thread page ownership does not provide. That is not proposed here.

**Minimum target.** Match jemalloc in the same run. TCMalloc is the stretch target, and this proposal does not claim it is reachable.

### 3. Risks, likely files, tuning constants

**Risks.**
- **Re-faults** if a discarded block is reused within a tick or two. A2's two-tick rule and A1's address order are the guards, and perf-ab minor faults are the check.
- **Free-list re-threading correctness.** It is owner-only in the slow path. `xthread_free` stays untouched. It must re-encode under `MI_ENCODE_FREELIST` (secure/debug builds) and respect `MI_MIN_EXTEND` under `MI_SECURE`.
- **Platform discard cost.** Windows discard is `MEM_RESET` + `VirtualUnlock` (#477). macOS has its own discard path, which is why the selective lane is included.
- **32-bit.** 128 KiB does not fit a large page there (see `test_large_pages`).
- **Scavenger overlap.** C2 must not fight the scavenger's abandoned-page sweep (#510, open) or the #491 ratchet.
- **Upstream divergence.** B, if pursued, is the largest; per rule 6 it must be stated in the PR.

**Likely files.**
- `src/page-holes.c`: A2 and A3, and the busy-sweep changes.
- `include/mimalloc/types.h`: cold per-page snapshot field and new constants.
- `include/mimalloc/internal.h`: inline helpers.
- `src/page.c`: at most one guarded call in the large-page free-collect path for A1.
- `src/arena.c`: C1, confined to `mi_arena_try_claim_resident`.
- `src/scavenger.c`: C2 pacing.
- New logic goes in new files per rule 6, for example `src/large-reuse.c`, included from `src/static.c`.
- Tests: `test/test-purge-holes.c`, plus arena-retention tests. New tests are registered in `CMakeLists.txt`.
- The Rust amalgamation is regenerated in a separate commit (rule 2).

**Tuning constants (rule 9).** The names are provisional. Each is an `#ifndef`-guarded define, and becomes an `mi_option` only if it needs to be set at run time:
- `MI_LARGE_COLD_TICKS` (2): busy ticks a free block must stay free before it is discarded.
- `MI_LARGE_FREE_ORDER_MAX_BLOCKS` (64): the largest block count for which a large page's free list is re-threaded in address order.
- `MI_LARGE_TAIL_TRIM_TICKS` (1): ticks without `capacity` growth before the unformed tail of a resident-carved page is trimmed.
- `MI_RESIDENT_FIRST_BEST_FIT` (1): prefer the smallest sufficient resident run.
- `MI_ARENA_RESIDENT_BUDGET_MULT` (2): resident queued singleton bytes allowed per recent peak live singleton byte before the first-deadline purge.

### 4. RED -> GREEN

**New test: `test_large_churn_releases_cold_blocks`** in `test/test-purge-holes.c`.
- Linux only (`mincore`), like the #491 release tests, and it polls up to `_mi_release_bound_ms()` x `RELEASE_TEST_MARGIN`.
- **Setup.** Set `purge_delay` and `purge_holes_min_interval` to 20. Allocate 192 KiB blocks (an exact large bin; 21 per 4 MiB page) until the page is one block short of full. A full large page is abandoned at once (`page.c:1196`), as `large_page_with_holes` already notes. Pattern-fill every block.
- **Churn.** Free all but blocks 0 and 1. Then churn alloc/touch/free of one 192 KiB block through the generic path for 3 x `purge_holes_min_interval`.
- **Assert.** Fewer than a quarter of the OS pages of blocks 4..19 are resident. The survivors pass `pattern_check`. `mi_purge_holes_stats_get` shows discards.
- **RED today.** Free-list rotation touches every block, and the busy sweep skips the changed page (`page-holes.c:710-714`), so the freed blocks stay resident.
- **GREEN after A.** A1 and A2 leave blocks 3 and up cold, and they are discarded.

**Companion test: hot-loop guard.** A single-block alloc/free loop in a large page shows no growth in `ru_minflt` beyond a small bound and zero discards of the hot block (#477's criterion).

**For C.** A test in the style of `test-arena-retention-small.c`. Churn singleton blocks of random slice counts, then assert that resident queued bytes stay within `MI_ARENA_RESIDENT_BUDGET_MULT` x peak live and that the #491 bound holds.

**Expected scaling-chart numbers (GREEN thresholds; expectations, not measurements).** At 8 workers, against same-run references:

| workload | expected RSS | reference |
|---|---|---|
| `sparse-large-buffers` | at or below jemalloc (298.4 MiB in this run); expected 238-267 MiB with A+C | stretch: TCMalloc 156.8 MiB |
| `random-large` | at or below jemalloc (284.3 MiB); expected about 240-246 MiB with C | |
| `large-class-persistent` | below 199.7 MiB, toward jemalloc's 56.0 MiB | |
| `power-of-two-large` | no increase over 186.7 MiB | |

Throughput must stay at or above 1.81M, 1.03M, 2.05M and the other current medians, within the demonstrated precision.

### 5. Non-regression matrix

**Rules.**
- Every cell is compared against the unchanged fork at the same SHA's parent, per workload and per worker count. Averages are not accepted.
- **No regression is accepted.** A cell that is inconclusive gets more repetitions. It is not treated as "noise".
- The `perf-ab` label gate applies: CPU cost under the owner's bar, automatic with opt-out, age-gated for releases.

| Workload | Source | Workers | Throughput | p99 latency | CPU | Peak RSS | Retained / after-drain RSS |
|---|---|---|---|---|---|---|---|
| tiny (`sparse-tiny-hot`), `small/8 (control)` | scaling, perf-ab | 1-8 | yes | see note | yes | yes | yes |
| mixed (`sparse-mixed-general`, which includes realloc) | scaling | 1-8 | yes | see note | yes | yes | yes |
| large (`sparse-large-buffers`, `random-large`, `power-of-two-large`, `large-class-*`, `random-large-bursty/8`) | scaling, perf-ab | 1-8 | yes | see note | yes | yes | yes |
| cross-thread (`sparse-cross-thread`, `xmalloc-test`) | scaling | 1-8 | yes | see note | yes | yes | yes |
| realloc-grow large, calloc/zeroing large | new perf-ab rows (P3) | 1, 8 | yes | see note | yes | yes | yes |
| larson (`larson/1`, `larson/8`, chart build) | scaling, perf-ab | 1-8 | yes | see note | yes | yes | yes |
| thread churn (`large-class-ephemeral`, `thread-churn` side-car #508) | scaling, perf-ab | 8 | yes | see note | yes | yes | yes |
| idle / bursty (`random-large-bursty/8`, #483 idle rows) | perf-ab | 8 | yes | see note | yes | yes | release ms against the #491 bound |
| profiler on (`large-class/8 (profiler on)`) | perf-ab | 8 | yes | see note | yes | yes | yes |

Minor faults are recorded for every perf-ab row.

**p99 latency is a gap.** Neither perf-ab nor benchmark-scaling collects per-operation latency today. A latency histogram in `ci/perf_ab.c` is a prerequisite before any implementation PR can claim "no tail regression".

### 6. Gates for an implementation PR

- `c-unit`:
  - Linux with `MI_PPROF=ON`;
  - the minimal `pprof-off` row (every observability subsystem compiled out).
- Native MSVC `ctest (windows-latest)`: both `build-windows-native` and `run-windows-native`, which are hard gates.
- `windows-bundles.yml`: win-gnu (soldr mingw-w64, UCRT) and clang-cl.
- `macos-bundles.yml`: cross-build for both arches. Also the selective Recovery lane (`needs-macos`, or automatically if a Darwin path is touched), and a manual `run-macos-x64-recovery` if `src/prim/osx` discard paths change.
- `rust-native`, with the amalgamation regenerated in its own commit.
- `ci/check_fastpath_identity.py`: the minimal build's fast path stays byte-identical, with no `lock`/`xchg`.
- `test-observer-scaling`.
- `perf-ab` (label).
- One benchmark-scaling run on the candidate, compared with the same run's references.

### 7. Closing

This proposal completes #425's deliverable. It does not authorize implementation.

- Step 0's CI prerequisites (P1-P5) are diagnostic-only. Even so, they need the owner's go-ahead.
- Any allocator change (A, B or C) needs separate owner authorization after this proposal is reviewed.
- After review, a new implementation issue should be opened, conditional on the Step 0 results.

The work stops here.
