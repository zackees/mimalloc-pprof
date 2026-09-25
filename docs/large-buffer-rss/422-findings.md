<!-- grind-425-findings -->
## #425 causal investigation: large-buffer RSS on current main

This is the #425 causal investigation report. The proposal follows in a separate comment. No allocator code was changed.

### Provenance

- Data: benchmark-scaling run https://github.com/zackees/mimalloc-pprof/actions/runs/36160378572, attempt 1. Source SHA `6c93b7ba93ec21658e5f0293b213c557aa2df92d`, sealed on the `benchmark-stats` branch at commit `ba84db4a`.
- Runner: GitHub-hosted, AMD EPYC 7763, 4 logical cores (2 physical), Ubuntu 24.04.5, kernel 6.17.0-1022-azure. The 6- and 8-worker points are oversubscribed.
- Allocator sources:

  | allocator | source SHA |
  |---|---|
  | tcmalloc | `c316de3e` |
  | jemalloc | `81034ce1` |
  | upstream-mimalloc | `6def7be9` |
  | bun-mimalloc | `b20b60d9` |
  | mimalloc-pprof | `6c93b7ba` |

- Repetitions: 40 per cell for `power-of-two-large`, `random-large` and `large-class-*`. `sparse-large-buffers` (the original log-uniform 64 KiB-4 MiB workload) has 3 paired blocks per cell.
- This is a hosted runner, so treat the numbers as a screening signal. They do not count as stable-host acceptance.
- Reproduce:

  ```
  gh run download 36160378572 -R zackees/mimalloc-pprof -n benchmark-scaling-raw-36160378572 -D <dir> && uv run ci/large_buffer_rss_attribution.py <dir>/scaling-raw-run.json
  ```

  `ci/large_buffer_rss_attribution.py` lands in the same PR as this report. The same-time live-byte numbers below come from the artifact's `diagnostic_*` fields. They can be listed with:

  ```
  jq -r '.samples[] | select(.diagnostic_peak_live_requested_bytes > 0) | [.pattern, .allocator_id, .thread_count, .diagnostic_peak_rss_bytes, .diagnostic_peak_live_requested_bytes, .live_requested_bytes_at_diagnostic_peak_rss] | @tsv' <dir>/scaling-raw-run.json
  ```

### Measured medians

All values are medians. RSS and live are in MiB. Slopes are MiB per worker, from a least-squares fit over workers 1/2/3/4/6/8. `live` is the published `peak_live_requested_bytes` (see H8 for what it means). `RSS/live@8` is the ratio of those two medians (the tool's per-cell table reports the median of per-sample ratios instead, e.g. 2.45 for the fork). `tp@8` is ops/s at 8 workers.

| pattern | allocator | RSS@1 | RSS@8 | live@8 | RSS/live@8 | RSS slope | live slope | tp@8 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| sparse-large-buffers | mimalloc-pprof | 91.6 | 481.7 | 194.2 | 2.48 | 54.8 | 24.1 | 1.81M |
| sparse-large-buffers | upstream-mimalloc | 97.5 | 525.6 | 194.2 | 2.71 | 61.0 | 24.1 | 1.88M |
| sparse-large-buffers | bun-mimalloc | 97.6 | 510.0 | 194.2 | 2.63 | 59.5 | 24.1 | 1.82M |
| sparse-large-buffers | jemalloc | 43.2 | 298.4 | 194.2 | 1.54 | 36.5 | 24.1 | 1.07M |
| sparse-large-buffers | tcmalloc | 50.4 | 156.8 | 194.2 | 0.81 | 14.7 | 24.1 | 1.50M |
| random-large (uniform 64 KiB-4 MiB) | mimalloc-pprof | 97.6 | 380.5 | 163.2 | 2.33 | 40.4 | 20.2 | 1.03M |
| random-large | upstream-mimalloc | 99.5 | 415.6 | 163.2 | 2.55 | 45.4 | 20.2 | 1.01M |
| random-large | bun-mimalloc | 99.6 | 416.7 | 163.2 | 2.55 | 45.8 | 20.2 | 1.03M |
| random-large | jemalloc | 41.3 | 284.3 | 163.2 | 1.74 | 34.9 | 20.2 | 0.68M |
| random-large | tcmalloc | 50.3 | 156.7 | 163.2 | 0.96 | 15.2 | 20.2 | 0.89M |
| power-of-two-large (2^16..2^22) | mimalloc-pprof | 45.6 | 186.7 | 148.2 | 1.26 | 20.0 | 18.2 | 2.05M |
| power-of-two-large | upstream-mimalloc | 57.5 | 194.6 | 148.2 | 1.31 | 19.1 | 18.2 | 2.06M |
| power-of-two-large | bun-mimalloc | 56.6 | 193.6 | 148.2 | 1.31 | 19.4 | 18.2 | 2.10M |
| power-of-two-large | jemalloc | 37.1 | 259.1 | 148.2 | 1.75 | 31.7 | 18.2 | 1.33M |
| power-of-two-large | tcmalloc | 41.1 | 102.5 | 148.2 | 0.69 | 8.6 | 18.2 | 1.82M |

Reference only: the #478 short-lived and persistent large-class workloads (96-512 KiB) at 8 workers.

| workload | mimalloc-pprof | jemalloc | TCMalloc | live |
|---|---:|---:|---:|---:|
| ephemeral | 219.8 MiB | 52.3 MiB | 50.2 MiB | 24.3 MiB |
| persistent | 199.7 MiB | 56.0 MiB | 56.6 MiB | 24.5 MiB |

That gap belongs to the #477/#483/#484 line of work, not to this issue. All three are closed, but the gap is still there. H1 below uses the persistent workload as evidence because it is the only current workload whose every request lands in a 4 MiB large page.

#### mimalloc-pprof at 8 workers

| pattern | vs jemalloc | vs TCMalloc | vs upstream |
|---|---:|---:|---:|
| sparse-large-buffers | 1.61x | 3.07x | 0.92x |
| random-large | 1.34x | 2.43x | 0.92x |
| power-of-two-large | 0.72x | 1.82x | 0.96x |

#### Comparison with the issue's orientation numbers

The issue's orientation numbers were fork 524.8, jemalloc 310.4 and TCMalloc 159.9 MiB for `sparse-large-buffers` at 8 workers.

- The fork is now at 481.7 MiB, about 8% lower. In this run it is 0.92x upstream (525.6 MiB), because of the #457/#477 work (#481, #482, #493, #500).
- The gap to the references remains. Matching jemalloc's 298.4 MiB still needs a 38% reduction, and matching TCMalloc's 156.8 MiB needs 67%.
- **The issue is not obsolete.**

#### Throughput

mimalloc-pprof's throughput at 8 workers leads both references in all three workloads:

| pattern | vs jemalloc | vs TCMalloc |
|---|---:|---:|
| sparse-large-buffers | 1.69x | 1.21x |
| random-large | 1.51x | 1.16x |
| power-of-two-large | 1.54x | 1.13x |

At 1 worker the three are at parity: 0.98-1.10x across the three workloads. Any fix must keep the 8-worker lead and must not fall behind at 1 worker.

### Same-time live bytes (synchronized attribution)

The runner replays every distribution-pattern cell a second time with live-byte telemetry (`scaling_runner.rs`, the `pattern.is_distribution()` branch, line 310 at `6c93b7ba`).

- So `diagnostic_*` fields **are** populated for `power-of-two-large`, `random-large` and `large-class-*`: 1200 of 1200 samples each.
- They are 0 only for `sparse-large-buffers` and the other `sparse-*`/`larson`/`xmalloc` patterns, which are not distribution patterns.
- The replay serializes every alloc and free through a mutex-guarded file write (`LiveTelemetry::publish`), so its timing differs from the timed run. Its peak-RSS medians still land within about 6% of the timed runs.

Medians at 8 workers:

| pattern | allocator | replay RSS peak | same-time live peak | RSS / same-time live | live at the RSS-peak sample |
|---|---|---:|---:|---:|---:|
| random-large | mimalloc-pprof | 391.7 | 80.4 | 4.87 | 12.3 |
| random-large | upstream-mimalloc | 409.7 | 81.1 | 5.05 | 36.7 |
| random-large | bun-mimalloc | 409.5 | 80.8 | 5.07 | 40.7 |
| random-large | jemalloc | 285.1 | 85.1 | 3.35 | 36.7 |
| random-large | tcmalloc | 150.2 | 84.1 | 1.79 | 35.3 |
| power-of-two-large | mimalloc-pprof | 175.7 | 60.2 | 2.92 | 0 |
| power-of-two-large | upstream-mimalloc | 186.6 | 58.7 | 3.18 | 23.5 |
| power-of-two-large | jemalloc | 254.3 | 61.7 | 4.12 | 20.3 |
| power-of-two-large | tcmalloc | 94.1 | 59.1 | 1.59 | 25.9 |
| large-class-persistent | mimalloc-pprof | 189.5 | 12.7 | 14.9 | 6.0 |
| large-class-persistent | jemalloc | 55.2 | 12.6 | 4.38 | 5.7 |
| large-class-persistent | tcmalloc | 55.5 | 12.9 | 4.30 | 6.0 |

Three consequences:

1. The published `live@8` is roughly twice the true simultaneous live peak: 163.2 against about 80 MiB, and 148.2 against about 60 MiB. So the published RSS/live ratios understate amplification for every allocator. The ranking does not change.
2. Every allocator reaches its RSS peak at a moment when live bytes are well below their peak. RSS here is a high-water mark of retention, not of the live set.
3. For mimalloc-pprof, the median live bytes at the sample that first set the RSS maximum is 0 in `power-of-two-large` at 1/2/3/8 workers and in `random-large` at 4/6 workers. The sampler updates only on a strict increase (`sample_peak_rss_with_live`, `scaling.rs:1941` at `6c93b7ba`). So in those cells the fork's maximum was first reached after the workers' live set had drained, during teardown, and not during churn. Upstream and Bun do not show this.
   - The size of the difference is unknown. It can be as small as one OS page above the churn peak.
   - It is recorded under H8 as an open item, not as a correction to the published numbers.

### Workload geometry (from the artifact's size histograms and the planner code)

Allocation-count fractions from the recorded `size_histogram` at 8 workers:

| pattern | below 512 KiB | exactly 4 MiB |
|---|---:|---:|
| sparse-large-buffers | 42.8% | 14.3% |
| power-of-two-large | 42.7% | 14.4% |
| random-large | 10.9% | 0 |

- **sparse-large-buffers.** The log-uniform draw picks one of 7 octaves and clamps the top octave to 4 MiB (`draw_size`, `scaling.rs:582-606` at `6c93b7ba`). That clamp is where the 4 MiB mass comes from. It is not a planner bug, but it is a property of the reference workload. **Correction to the working hypothesis:** only 43% of its allocations fall in 64-512 KiB, not "most". The other 57% are singleton pages of continuously distributed size.
- **power-of-two-large.** Same count fractions below 512 KiB and at 4 MiB as sparse-large-buffers, but only four exact sizes below 512 KiB (64 KiB in a medium page; 128/256/512 KiB in large pages) and three exact singleton sizes (1/2/4 MiB).
- **random-large.** About 89% of requests exceed 512 KiB.

### Code facts checked (line numbers at `6c93b7ba`; these C files are unchanged at `main`'s HEAD)

**Page geometry.**
- `MI_ENABLE_LARGE_PAGES` defaults to 1 (`include/mimalloc/types.h:170`). The comment at `:166-168` warns about partially used large pages for random sizes above 64 KiB.
- Medium pages are 512 KiB and large pages are 4 MiB (`types.h:253-254`).
- `MI_MEDIUM_MAX_OBJ_SIZE` is (512 KiB - 4 KiB)/6, about 84.7 KiB, and `MI_LARGE_MAX_OBJ_SIZE` is 512 KiB (`types.h:597-598`). With the define at 0, both become 64 KiB (`:600-601`).
- So blocks from about 84 KiB to 512 KiB live in 4 MiB pages, one page per size bin per thread. Blocks above 512 KiB get singleton pages.
- #477 recorded the effective large bins as 96, 112, 128, 160, 192, 224, 256, 320, 384, 448 and 512 KiB.

**Large pages are hole-punchable today, which corrects #479's wording.**
- `mi_page_purge_unit` (`include/mimalloc/internal.h:1458`) doubles the purge unit to 16 KiB for a 4 MiB page on 4 KiB OS pages, so the page fits the 256-bit `purged` bitmap (`types.h:286`).
- `mi_page_can_purge_holes` (`internal.h:1488-1494`) now excludes only:
  - singleton pages (`page->reserved <= 1`, line 1489);
  - pinned memory;
  - arenas with a custom commit function.
- "4 MiB large pages cannot be hole-punched" was true before #482 and is not true on current main.

**What actually limits large-page reclamation under continuous churn.**
- The owner's busy sweep `_mi_theap_purge_large_holes` (`src/page-holes.c:981`) runs only from the generic-malloc housekeeping every 1000 generic calls (`src/page.c:1137`, call at `:1152`). It runs at most once per `purge_holes_min_interval` (100 ms, `src/options.c:194`; check at `page-holes.c:990`), and never on a thread's first tick (`:989`).
- Inside `_mi_page_purge_holes`, while `holes_busy` is set, a page whose free-list head or `(capacity, used)` changed since the previous tick is skipped entirely (`page-holes.c:710-714`). That includes its unformed-tail trim at `:716`. A fully free page is left to retirement (`:708`).
- Under the benchmarks' continuous churn every live page changes every tick. So on current main the busy sweep discards essentially nothing from pages in active use.
- This is an inference from the code. It is not measured in this run.

**Purge timing.**
- `purge_delay` defaults to 100 ms (`options.c:143`; environment `MIMALLOC_PURGE_DELAY`). `arena_purge_mult` defaults to 4 (`types.h:322`, `options.c:152`).
- Freed arena ranges are purged at the second deadline after the free (`MI_ARENA_PURGE_PERIODS` = 2, `types.h:315`). So a freed singleton range stays resident for 400-800 ms (`mi_arena_purge_delay`, `src/arena.c:2699-2709`).
- `purge_delay=0` purges synchronously (`arena.c:2783`).

**Reuse and retired pages.**
- Resident-first reuse (#493) claims a still-resident queued run before the plain search (`mi_arena_try_claim_resident`, `arena.c:254`).
- Retired large pages are released after `MI_RETIRED_RELEASE_MULT` x `purge_delay` = 1 s (`types.h:295`, `page-holes.c:872`).

**Allocation granularity.** Large pages extend one block at a time (`MI_MIN_EXTEND` = 1, `src/page.c:709`), and full large pages are not retained (`page.c:864`).

### Hypothesis status

| ID | Status | Evidence | Remaining confounders |
|---|---|---|---|
| H1 large allocator pages strand resident space | **Supported (descriptive); causal share not separated** | **Contrast with power-of-two.** It has the same 43% of requests below 512 KiB, but only 1.8 MiB/worker excess slope (20.0 - 18.2), beating jemalloc. Few exact-fit bins do not strand memory. <br>**Many large bins do.** `large-class-persistent` puts every request in a 4 MiB large page across 11 bins. There the fork's RSS slope is 23.2 MiB/worker against a live slope of 3.0; jemalloc's is 6.6 and TCMalloc's 4.4. Its RSS / same-time live is 14.9 against about 4.3. That is about 20 MiB/worker, consistent with roughly five resident 4 MiB large pages per thread. <br>**sparse-large-buffers.** Excess slope is 54.8 - 24.1 = 30.7 MiB/worker, in line with this per-bin slack plus the singleton excess (H4). <br>Large pages are hole-eligible, but under churn the busy sweep skips every changed page (`page-holes.c:710-714`). So freed blocks and resident unformed tails in active large pages stay resident. | The `MI_ENABLE_LARGE_PAGES=0` build and the exact-size / bin-edge controls have not run, so causation is not established. The split of sparse's 30.7 MiB/worker between large pages and singletons is unknown. <br>Inference, not measured: if singleton excess per worker matched random-large's 20.2, the large-page share would be about 10 MiB/worker. <br>Allocator-internal attribution (resident free blocks, unformed tails, retired pages, queued slices) has not been collected on this SHA. |
| H2 size diversity and lifetimes | **Supported as a modifier (sizes); inconclusive (lifetimes)** | Same allocator and same slot policy: amplification is 1.26x for power-of-two sizes and 2.3-2.5x for random/log-uniform sizes. <br>Power-of-two and sparse have matching count fractions below 512 KiB (42.7% vs 42.8%) and at 4 MiB (14.4% vs 14.3%). Their excess slopes still differ, 1.8 against 30.7 MiB/worker. Size diversity, not the class mix, drives the difference. | Payload per request differs between the two. The clustered/shuffled traces, the two-size alternating trace and the 1/8/32-slot controls have not run, so the lifetime question is open. |
| H3 per-thread ownership drives the slope | **Supported descriptively** | RSS slope is 2.3x the published live slope on sparse (54.8 vs 24.1) and 2.0x on random (40.4 vs 20.2) for the fork; upstream and Bun are similar. TCMalloc stays below the live slope (0.61x and 0.75x), and jemalloc sits between. | No fixed-aggregate-budget control and no persistent-versus-fresh-worker control have run. The published live slope is a sum of per-worker peaks (H8), so "per worker" mixes ownership with live-set growth. |
| H4 purge delay retains dirty slices | **Inconclusive; the most likely contributor for random-large** | About 89% of uniform 64 KiB-4 MiB requests exceed 512 KiB and are singleton pages returned to the arena on free. The 20.2 MiB/worker excess there, and the fixed offset at 1 worker (97.6 MiB RSS against 22.3 MiB live; jemalloc 41.3, TCMalloc 50.3), point to freed-but-unpurged slices (400-800 ms window) or slice fragmentation. <br>Prior evidence from an older SHA on #477: `MIMALLOC_PURGE_DELAY=0` roughly halved RSS but cost about 99.6% of throughput. | The purge_delay 0/10/100/1000 sweep has not run on this SHA. Retention and fragmentation (random slice counts against resident-first first fit) are not separated. |
| H5 arena reserve / eager commit | **Inconclusive (not run; low prior)** | `arena_eager_commit` is 2 (eager only where the OS overcommits) and `arena_reserve` is 1 GiB (`options.c:46-55`). On Linux, commit without touch is not RSS, so this is unlikely to add residency. | Not tested. Reservation is not residency, and neither has been measured separately. |
| H6 idle cooperation | **Inconclusive / not applicable to peak during churn** | The measured peak is taken during continuous churn, where no thread goes idle. Post-idle retention is #483's scope. The `thread-churn` side-car (#508) measures post-drain RSS separately. | Not tested here. |
| H7 OS huge pages (THP) | **Inconclusive** | Hosted runner. The THP policy and `AnonHugePages` were not recorded. `allow_thp` defaults to 1 (`options.c:103-107`); allocator large OS pages default off. | Needs a suitable host with the THP policy recorded. |
| H8 measurement | **Inconclusive** | The published metric is the external `/proc/<pid>/smaps_rollup` Rss, polled every 5 ms. The window runs from spawn to child exit (`sample_peak_rss_with_live`, `run_scaling_child_with_plan`, `scaling.rs` at `6c93b7ba`). The children run with a cleared environment (`env_clear`, `scaling.rs:1995`). <br>`peak_live_requested_bytes` is the **sum of per-worker peaks** from the offline plan (`simulate_plan_metadata`, `scaling.rs:997`; accumulated at `:1046`), not a same-time quantity. Every block is page-touched (one byte per 4 KiB). So TCMalloc's RSS falling below it (0.69-0.96x) is expected. The same-time peak from the diagnostic replay is about 0.4-0.5x of it at 8 workers. <br>For the fork, the RSS maximum is often first reached with zero live bytes, i.e. after the drain (see above). | No same-time attribution exists for `sparse-large-buffers`: its `diagnostic_*` fields are 0 because it is not a distribution pattern. There are no phase markers, so the peak's phase is unknown. Polling sensitivity (1/5/20 ms) has not been tested. The telemetry replay's observer cost changes timing. |
| H9 trace validity | **Supported: the trace is valid** | Across the five large patterns there are 978 (pattern, workers, block) groups. Every group has all 5 allocators with identical checksum, operation count, alloc/free call counts, worker seeds and size histogram. Checked with `jq` on the raw artifact; the trace check in `ci/large_buffer_rss_attribution.py` (exit 1 on any mismatch) encodes the same checksum / operation-count / call-count identity so the check can be rerun on any later run. <br>The 14.3% mass at exactly 4 MiB in `sparse-large-buffers` is the planner's clamp, identical for every allocator. | None for identity. The replay's transient overlap (a slot's replacement allocated before the old block is freed) is not modelled, which affects only the live-byte figure. |
| H10 fork build/harness | **Rejected as the cause** | Upstream, Bun and the fork show the same shape within about 10% (fork 0.92x upstream at 8 workers on sparse and random). The cause is common mimalloc engine policy, not fork options. <br>The fork's benchmark build compiles `MI_PPROF=ON` with memory events compiled in, but both are runtime-disabled (`MIMALLOC_PROF=0 MIMALLOC_MEMORY_EVENTS=0` in every reproduction command; artifact `options`: `pprof_runtime: disabled`, `memory_events_runtime: disabled`). Upstream is built `MI_PPROF=OFF`. | The shipped minimal build (all observability off) was not benchmarked. Given the family agreement, a large difference is not expected; that is an expectation, not a measurement. |

### Acceptance status for #425

- [x] Phase 1 merged and baseline published (#424; run 35845769997). This report re-bases on the newer run 36160378572.
- [x] The gap is reproduced on current main in both new distributions and the original workload. Trace identity is verified across all five allocators.
- [ ] **Not done:** exact 64 KiB and bin-boundary controls. These need CI diagnostic runs, since no local performance timing is allowed, and neither workflow can yet vary a size, an env var or a define for this suite (see the proposal's Step 0).
- [ ] **Not done:** the `MI_ENABLE_LARGE_PAGES=0` diagnostic build and the `MIMALLOC_PURGE_DELAY` 0/10/100/1000 sweep. Same reason.
- [~] **Partly done:** live-byte and mapping attribution.
  - Same-time live bytes exist for `random-large`, `power-of-two-large` and `large-class-*`, but not for `sparse-large-buffers`.
  - No mapping-level or allocator-internal attribution has been collected on this SHA.
  - Measurement semantics are verified: the live metric is a sum of per-worker peaks, and the RSS window covers the whole child lifetime.
- [x] This report posts supported, rejected and inconclusive hypotheses with their remaining uncertainties.

**Conclusion.** The root cause is strongly indicated but not causally proven. It has two parts:

1. **Per-bin slack in 4 MiB large pages** for 84-512 KiB requests. Many bins are used per thread, and none is swept while it churns.
2. **Retention or fragmentation of freed singleton slice runs** for requests above 512 KiB.

The proposal in the next comment therefore makes implementation conditional on the Step 0 diagnostics.
