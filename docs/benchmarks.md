# Benchmarks

*Part of the [mimalloc-pprof](../README.md) documentation.*

mimalloc-pprof is continuously benchmarked against **Microsoft mimalloc**,
**Bun's mimalloc fork** (`oven-sh/mimalloc`), **TCMalloc**, and **jemalloc** on a
dedicated Linux x86-64 runner.  Every result
is **GitHub-hosted and informational** — no self-hosted hardware, no hand-picked
runs, no unpublished baselines.

| Resource | Description |
|---|---|
| [**Benchmark dashboard**](https://zackees.github.io/mimalloc-pprof/) | Live per-scenario throughput, paired statistical effects, and full allocator provenance |
| [`benchmark-stats` branch](https://github.com/zackees/mimalloc-pprof/tree/benchmark-stats) | Raw sealed site artifacts (history, manifests, digests) |
| [`latest.json`](https://zackees.github.io/mimalloc-pprof/latest.json) | Machine-readable publication envelope for the most recent headline run |

## Methodology summary

- **5 allocators** — mimalloc-pprof, Microsoft mimalloc (pinned `dev3@6def7be9`,
  v3.5.0), Bun's mimalloc fork, TCMalloc, and jemalloc — pinned to immutable
  commits with SHA-256-verified source archives.
- **Paired balanced blocks** — every block runs all five allocators in
  randomized order under one workload seed; ≥15 complete blocks per headline
  cell.
- **Type-7 quantile bootstrap** — 10,000 resamples, splitmix64-rejection PRNG,
  percentile-block confidence intervals at 95%.  Paired effects are expressed
  relative to Microsoft mimalloc.
- **No profiling during measurement** — `MIMALLOC_PROF=0` and
  `MIMALLOC_MEMORY_EVENTS=0` are set on every child process; the allocator runs
  in its natural configuration.
- **One mimalloc recipe for all three mimalloc rows** — upstream, Bun and this
  fork are built by the same cmake-ninja command: `Release`,
  `MI_BUILD_STATIC=ON`, `MI_BUILD_SHARED=OFF`, `MI_BUILD_TESTS=OFF`,
  `MI_OPT_ARCH=OFF`, `MI_OPT_SIMD=ON`, `-O3 -fno-omit-frame-pointer`.  The only
  intended difference is `MI_PPROF` (`OFF` upstream, `ON` for this fork); Bun's
  tree has no such option, so its row simply omits it.
  `ci/build_benchmark_allocators.py` fails the build if any other flag differs.
- **The three mimalloc rows share a base.**  Since
  [#332](https://github.com/zackees/mimalloc-pprof/issues/332) the
  `upstream-mimalloc` row is pinned at `6def7be9` -- the exact commit this fork's
  overlay is based on -- so it, Bun's `b20b60d9` and this fork are all
  `MI_MALLOC_VERSION 30500` (v3.5.0) and all three compile with
  `MI_OPT_FREE_SMALL=1`.  Upstream-vs-fork therefore answers "our fork versus its
  own base", which is what the charts say it does.  It was previously pinned at
  `bcee5a88` (v3.4.3), where that comparison mixed base changes with fork changes;
  history rows recorded under that pin carry the old comparison key and are a
  separate lineage.
- **Deterministic reproducibility** — every raw sample carries its exact command
  line and workload seed; the published site manifest carries a detached SHA-256
  digest of every file.

### Pinned sources

Every competitor is an immutable archive verified by SHA-256 before extraction;
the fork itself is the workflow checkout.  The lockfile is
[`rust/benchmark-suite/allocators/allocator-lock.json`](../rust/benchmark-suite/allocators/allocator-lock.json).

TCMalloc's Bazel dependency lock is supplied by the checksummed
`tcmalloc-module-lock.patch` source patch. Both query and build use
`--lockfile_mode=error`, after the builder verifies the lock's SHA-256.
Regenerating it during every build would incorporate mutable registry metadata.
When updating TCMalloc dependencies, regenerate `MODULE.bazel.lock` with Bazel
9.1.0 and the locked query command in update mode, then refresh the source patch,
its checksum, and `generated_lock_sha256` together.

| Row | Legend label | Pin | Build |
<!-- #375: the Legend label column is ci/benchmark_report.py's ALLOCATOR_LABELS; a test asserts the charts render these. -->
|---|---|---|---|
| `tcmalloc` | TCMalloc | `google/tcmalloc@c316de3e` | bazel `-c opt` |
| `jemalloc` | jemalloc | `jemalloc/jemalloc` 5.3.1 `@81034ce1` | autoconf/make, static only |
| `upstream-mimalloc` | Microsoft mimalloc | `microsoft/mimalloc` `dev3@6def7be9` | cmake-ninja, `MI_PPROF=OFF` |
| `bun-mimalloc` | Bun mimalloc | `oven-sh/mimalloc` `bun-dev3-v2@b20b60d9` | cmake-ninja, no `MI_PPROF` option |
| `mimalloc-pprof` | mimalloc-pprof | workflow checkout | cmake-ninja, `MI_PPROF=ON` |

The Bun row's pin moves in the same PR as each future Bun-parity ingest, so the
chart always compares against the exact tree this fork has ported from.  The
pixel and SVG legends carry the compact row id (`bun-mimalloc`) for the same
reason the other four do; the full label and pin are on the dashboard's
allocator-provenance table and in the row above.

Full protocol details, JSON schemas, and reproduction commands are in
[`rust/benchmark-suite/`](../rust/benchmark-suite/).

## Headline results

Per-scenario throughput and the compatible history for the current comparison
key are rendered live on the dashboard — with full per-scenario tables and
paired effects expressed relative to Microsoft mimalloc:

- **[Per-scenario throughput →](https://zackees.github.io/mimalloc-pprof/#throughput)**
- **[Comparison history →](https://zackees.github.io/mimalloc-pprof/#history)**

The raw sealed artifacts remain available on the
[`benchmark-stats` branch](https://github.com/zackees/mimalloc-pprof/tree/benchmark-stats).

## Thread scaling by allocation pattern

How each allocator's aggregate throughput moves as worker threads go from 1 to
4 to 16, for four different allocation patterns.  Each pattern is a **seeded
random operation stream** — every operation, size, and slot is drawn from a
splitmix64 chain that never observes allocator behavior — so all five
allocators replay one identical stream inside each paired block.

> **Coverage mode: reduced statistical rigor (3 blocks per cell).**  These
> panels deliberately trade statistical rigor for thread coverage.  They carry
> no confidence intervals and no noise gating; read them for shape, not for
> headline-grade differences.  The runner allows 4 logical CPUs, so the
> 16-thread point is 4× oversubscribed and describes contention, not core
> scaling — it is shaded on every chart.

[![Tiny hot path: aggregate throughput by worker count for all five allocators](https://raw.githubusercontent.com/zackees/mimalloc-pprof/benchmark-stats/benchmark-scaling-tiny-hot.svg)](https://zackees.github.io/mimalloc-pprof/#scaling)

[![General mix including realloc: aggregate throughput by worker count for all five allocators](https://raw.githubusercontent.com/zackees/mimalloc-pprof/benchmark-stats/benchmark-scaling-mixed-general.svg)](https://zackees.github.io/mimalloc-pprof/#scaling)

[![Large page-touched buffers: aggregate throughput by worker count for all five allocators](https://raw.githubusercontent.com/zackees/mimalloc-pprof/benchmark-stats/benchmark-scaling-large-buffers.svg)](https://zackees.github.io/mimalloc-pprof/#scaling)

[![Cross-thread producer/consumer handoff: aggregate throughput by worker count for all five allocators](https://raw.githubusercontent.com/zackees/mimalloc-pprof/benchmark-stats/benchmark-scaling-cross-thread.svg)](https://zackees.github.io/mimalloc-pprof/#scaling)

| Pattern | Sizes | What it stresses |
|---|---|---|
| Tiny hot path | 16–64 B | small-object fast path, high alloc/free rate, small live set |
| General mix | 8 B–4 KiB log-uniform | everyday mix including realloc, medium live set |
| Large buffers | 64 KiB–4 MiB | large allocations with one-byte-per-page touching |
| Cross-thread handoff | 16–512 B | remote-free pressure; blocks are freed by another worker |

*Protocol `throughput-scaling-sparse-v1`, published daily (#208; it is the daily
freshness signal, and `benchmark-stats` -- the rigorous suite -- is the weekly one).  Full per-cell
tables, min/max spreads, and the metric comparison key are on the
[dashboard](https://zackees.github.io/mimalloc-pprof/#scaling).*

## Memory returned after idle — a second, separate measurement

The README's [memory-returned-after-idle chart](../README.md#memory-returned-after-idle)
is not produced by the paired throughput harness above.  It is a separate,
single-threaded measurement that answers a question the throughput matrix cannot:
when a process stops allocating, which allocators hand the memory back?  Two
scripts produce it, and they answer different questions.

`ci/bench_hole_purging_allocators.py` — **across allocators.**  One churn workload
(150k × 512 B + 100k × 1 KiB + 50k × 2 KiB blocks, a scattered 1-in-20 kept alive,
the rest freed, then a 10 s idle window) run once per allocator.  Its rules:

- **The locked builds, not ad-hoc ones.**  Sources, checksums and build commands
  come from the same
  [`allocator-lock.json`](../rust/benchmark-suite/allocators/allocator-lock.json)
  records the throughput matrix uses, so the pins and flags are identical.
  **tcmalloc is not in this chart**: its locked build needs bazel, which the
  measuring machine did not have, and a substitute build would not be the pinned
  one.
- **"Idle" is realised per allocator, and named in the chart and table.**  The
  mimalloc children call `mi_on_thread_idle()` every 100 ms — Bun's fork carries the
  same API, and Microsoft mimalloc at its pin has none, which the script detects by
  reading the extracted header rather than assuming.  jemalloc is given nothing
  beyond a normal idle process, plus a second series that calls
  `mallctl("arena.<all>.purge")` on the same tick, so the chart states what jemalloc
  can do when asked as well as what it does on its own.
- **Nothing in the harness allocates.**  The block table is `mmap`ed, RSS is read
  with `open`/`read`/`close` into a stack buffer, and samples leave through
  `write(2)`.  `libmimalloc.a` overrides libc `malloc` and the `je_`-prefixed
  jemalloc build does not, so a `printf` per idle tick would be allocator traffic in
  one arm and not the other — and allocator traffic during "idle" is exactly what
  jemalloc's decay needs in order to fire.
- **Peak is `VmHWM`**, the kernel's own high-water mark, read at the end.  Sampling
  starts at the top of the idle window, so a sampled maximum would be post-free RSS,
  not the peak.
- **Best of 3 runs by after-idle RSS, the same rule for every allocator**, pinned
  with `taskset -c 0-3`.  Every run's number is in the report JSON, not just the
  charted one.
- **A knob that did not take is fatal.**  A diagnostic row that tunes an allocator
  reads the setting back out of the allocator and refuses to publish on a mismatch —
  a `je_`-prefixed jemalloc ignores `MALLOC_CONF` (it reads `JE_MALLOC_CONF`) without
  a word of complaint, which produced a plausible, wrong 0% during development.
- **Diagnostics are measured, labelled, and kept off the chart**: `mimalloc-pprof`
  with no idle hook at all, `upstream-mimalloc` given `mi_collect(false)` and again
  given `mi_collect(true)` (which forces the purge rather than honouring
  `purge_delay`, so "upstream returns 18% even when told to collect" cannot be
  answered with "you gave it the non-forcing one"), and jemalloc with
  `background_thread:true` at both the chart's window and a longer one.
- **A series whose repetitions disagree says so on the image.**  The charted figure is
  the best of 3, so a series that returned memory in only some of its runs would
  otherwise be represented by its lucky run alone.  The table renders an "only N of M
  runs" footnote for any such series, counted from the run records rather than written
  by hand.

`ci/bench_hole_purging.py` — **inside one binary.**  The narrower A/B that no
competitor can take part in: `MIMALLOC_PURGE_HOLES=0` against `=1` in a single
build, scavenger on in both, median of 3.  It is what isolates hole purging's own
contribution, and it also carries the `mi_purge_holes_stats_t` counter table.

Both scripts commit their SVGs together with the CSV and report JSON they were
rendered from.  Two things enforce that they stay in step, because a chart and the
caption it carries drifting apart is silent otherwise: `ci/tests/` re-renders all
eight committed SVGs (both scripts, both charts, both themes) from the committed data
and fails on any difference, and `python-lint.yml` — mirrored by
`ci/verify_local.py --only lint` — runs `--check` on both scripts in both modes.
Neither re-measures, so neither needs an allocator build or a benchmark machine.

Both chart pairs are **dev-box measurements, not runner output**: unlike the
throughput matrix above, they are not produced by a scheduled GitHub-hosted run, and
each SVG names the machine, kernel and commit it was measured at in its own subtitle.
The two pairs were measured on the same machine but are separate runs with different
selection rules — median of 3 for the off-vs-on pair, best of 3 for the
cross-allocator pair — and neither is rendered from the other's data.

## Explicit empty-arena reclamation experiment (#438)

This is a separate experiment from the idle-churn chart. Its scattered survivors
keep arenas nonempty, whereas `MI_PURGE_RECLAIM` can release only completely free,
allocator-created arenas. The comparison measures the *additional* effect of an
explicit reclaim after an ordinary forced purge, plus the cost of allocating into
arenas again. Reclaim is opt-in; neither ordinary/forced purge nor the idle and
background paths enable it by default. The owner directive in
[#438](https://github.com/zackees/mimalloc-pprof/issues/438) rules out changing
that default under this work.

[`ci/bench_arena_reclaim.py`](../ci/bench_arena_reclaim.py) builds and drives the
standalone [`bench_arena_reclaim.c`](../ci/bench_arena_reclaim.c) workload. The
workflow checks out the comparison baseline at
[`d5bdb464debf59d0816eeac038d9d4423736c9c4`](https://github.com/zackees/mimalloc-pprof/commit/d5bdb464debf59d0816eeac038d9d4423736c9c4),
after the opt-in implementation merged, and builds it with the same benchmark
source. Thus the baseline is a pinned prior implementation, not an upstream
allocator or a pre-reclaim build. Both revisions run `purge-only` and `reclaim`
arms; each comparison *within* a revision uses the same build and workload.
`retained-id` runs only on the fixed branch because the baseline predates its
caller-owned-arena fix. Do not infer a safe baseline behavior from its absence.

From the measured source revision, a local Linux reproduction is:

```bash
git worktree add --detach ../mimalloc-pprof-438-main d5bdb464debf59d0816eeac038d9d4423736c9c4
python3 ci/bench_arena_reclaim.py --measure --toolchain unix --build-root /tmp/arena-reclaim-pr --data /tmp/arena-reclaim-linux-pr.json --runs 3
python3 ci/bench_arena_reclaim.py --measure --toolchain unix --build-root /tmp/arena-reclaim-main --allocator-source ../mimalloc-pprof-438-main --allocator-source-sha d5bdb464debf59d0816eeac038d9d4423736c9c4 --data /tmp/arena-reclaim-linux-main.json --runs 3
python3 ci/bench_arena_reclaim.py --render --data .github/assets/arena-reclaim-linux-pr.json --out-dir .github/assets
python3 ci/bench_arena_reclaim.py --check --data .github/assets/arena-reclaim-linux-pr.json --out-dir .github/assets
```

Replace `--toolchain unix` with `msvc` (native Microsoft `cl`) or `mingw`
(MSYS2 MINGW64) on Windows. The exact CI commands and pinned checkout are in
[`benchmark-arena-reclaim.yml`](../.github/workflows/benchmark-arena-reclaim.yml),
which can also be dispatched manually. The output JSON records each child
command and all build flags; the last two commands render and verify the
committed Linux figures byte-for-byte without rerunning the measurements.

The workload sets the arena-reserve option to 32 MiB, then allocates and
touches eight 32 MiB objects (one byte per 4 KiB page) in each of two waves.
Each wave records `peak`,
`free`, ordinary `mi_purge_all_ex(MI_PURGE_FORCE, 100, ...)`, then either
`mi_purge_all_ex(MI_PURGE_FORCE | MI_PURGE_RECLAIM, 100, ...)` or an aligned
no-op checkpoint. The second peak measures re-growth. In the `empty` scenario
all eight objects are freed before purge; in `nonempty` they remain live until
after the checkpoint, preventing their arenas from qualifying. The fixed-branch
`retained-id` scenario keeps a public, nonexclusive reservation and verifies its
area and ID still resolve after reclaim. A separate 64-byte allocation keeps
ordinary process activity outside the large-object arenas.

The matrix crosses `MI_OWNER_GATE=OFF/ON`,
`MIMALLOC_PURGE_DECOMMITS=0/1` (reset versus decommit), and no worker, a
cooperatively parked idle worker, or an active worker continuously allocating
and freeing. The idle worker uses `mi_on_thread_idle_start()`/`end()`; the
active worker tests admission under concurrent allocator use. The benchmark
sets `MI_PPROF=OFF` and uses Release static builds. Its manual artifact workflow
collects Linux, native Windows MSVC `cl`, and Windows MinGW-w64 runs. Each cell
has at least three paired repetitions; arm order alternates by repetition to
reduce order effects, and no run is discarded. The workload is deterministic
(`seed: 0`, no random operation stream). Raw JSON records source and allocator
SHAs, benchmark-source digests, host/toolchain details, build flags, command,
workload size, every sample, and the paired repetition index.

Selection is the median for each arm and phase. Incremental memory effects and
re-growth cost are medians of the *within-repetition* purge-only minus reclaim
differences, rather than a subtraction of independently selected best runs. The
two figures use the `MI_OWNER_GATE=OFF`, decommit-on, `empty`, no-worker cell;
the complete matrix remains in the raw JSON. The phase chart plots process RSS
through both waves. The tradeoff chart shows additional RSS, private-memory and
allocator-commit reductions, reclaim-call time, and extra second-wave allocation
time. It shows the allocator-reported released reservation separately. The
renderer annotates no-eligible and active-worker pending counts; raw rows also
distinguish `purge-busy`, `arena-layer-busy`, `pending-subprocess`,
`no-eligible-arena`, and `released`. A completed pass with zero eligible arenas
is a valid no-op, not an omitted measurement. Call duration is measured around
the API and re-growth duration around the next eight allocations and touches;
these are synthetic pause and re-growth costs, not application tail latency.

RSS (Windows working set) and private bytes are process measures; allocator
`committed` and metadata are allocator accounting; Linux `VmSize` is process
virtual address space. The Windows process-virtual field is unavailable and
must not be read as zero usage. `arena_reclaim_bytes` counts *reserved virtual
address space* returned to the OS. Ordinary purge may already have decommitted
most data pages, so a large reservation drop can correspond to a much smaller
physical-memory or committed-memory drop. Compare the reclaim checkpoint with
the already purged control when discussing savings. These observations establish
costs and effects for this workload; they do not by themselves prove safety in
every application.

| Policy | Tradeoff and decision |
|---|---|
| Explicit opt-in at a caller-chosen quiescent point | Chosen policy. The caller requests `MI_PURGE_RECLAIM` only when it can arrange a suitable pause or idle handoff and can inspect `reclaimed`, `arenas_reclaimed`, `arenas_kept`, and `subprocs_pending`. It pays the measured synchronous call and possible re-growth cost only at that point. Public caller-managed arenas and retained IDs must survive; the fixed-branch scenario checks one such ID. |
| Implicit reclaim during ordinary or forced purge | Would surprise existing callers with a possible pause and subsequent re-reservation cost. A forced purge does not itself prove other threads are quiescent. It also broadens exposure to retained-ID hazards. Not enabled. |
| Automatic, background, or idle-triggered reclaim | Could return address space without an explicit call, but the allocator would need a reliable quiescence protocol plus thresholds/hysteresis to avoid reclaim/re-reserve churn. Such a policy would make stalls and memory behavior workload-dependent and cannot assume that an idle notification makes every owner safe. Not enabled. |

Matching headers and library artifacts are required by this project, so a
mixed-version C ABI is not a policy requirement here. Same-build C/Rust layout
and memory-safety checks still matter; in particular, returning a caller-owned
arena while its ID is retained would be a use-after-free. The opt-in choice is
not a claim that every reclaim request will run: pending owners, an in-flight
purge, a busy arena layer, and a lack of eligible arenas are distinct outcomes.
Any proposal to enable reclamation by default needs a new owner decision.

### Measured results (three paired repetitions per row)

The benchmark source is [`10521b6c`](https://github.com/zackees/mimalloc-pprof/commit/10521b6cf6a3c4218e17acd5cae36bb2dc1d24e4);
the comparison allocator is the pinned `d5bdb464` revision above. These rows
use the decommit-on, ungated, empty-arena, no-worker cell plotted in the two
figures. “Ordinary RSS” is the peak-to-ordinary-purge drop in the reclaim arm;
all other memory reductions compare the paired purge-only and reclaim arms at
the *later* reclaim checkpoint. “Re-growth extra” is the paired second-wave
allocation-and-touch time in the reclaim arm minus the purge-only arm; a small
negative number is within measurement noise, not a speedup claim.

| Raw result | Ordinary RSS ↓ MiB | Extra RSS ↓ MiB | Extra private ↓ MiB | Extra allocator commit ↓ MiB | VA reservation released MiB | Reclaim call median / max ms | Re-growth extra ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| [Linux, fixed branch](../.github/assets/arena-reclaim-linux-pr.json) | 256.22 | 15.93 | 15.96 | 256.44 | 768 | 0.224 / 0.236 | +1.591 |
| [Linux, pinned main](../.github/assets/arena-reclaim-linux-main.json) | 256.22 | 15.97 | 15.97 | 256.50 | 768 | 0.216 / 0.217 | +0.994 |
| [Windows native `cl`, fixed branch](../.github/assets/arena-reclaim-msvc-pr.json) | 255.98 | 0.05 | 2.05 | 2.00 | 768 | 0.146 / 0.153 | +0.015 |
| [Windows native `cl`, pinned main](../.github/assets/arena-reclaim-msvc-main.json) | 255.98 | 0.04 | 2.07 | 2.00 | 768 | 0.124 / 0.127 | −0.169 |
| [Windows MinGW-w64, fixed branch](../.github/assets/arena-reclaim-mingw-pr.json) | 255.98 | 0.02 | 2.02 | 2.00 | 768 | 0.132 / 0.135 | +0.274 |
| [Windows MinGW-w64, pinned main](../.github/assets/arena-reclaim-mingw-main.json) | 255.98 | 0.03 | 1.98 | 1.94 | 768 | 0.139 / 0.144 | +0.025 |

The fixed branch also released **2.00 MiB of allocator metadata** in this
cell on each platform. The 768 MiB figure is released *virtual reservation*,
not a physical-memory saving. With decommit enabled, ordinary purge already
returned roughly 256 MiB of resident memory. The additional observed process
RSS reduction was about 16 MiB on Linux but only 0.02–0.05 MiB on these
Windows runners; Windows private committed memory fell by about 2 MiB. Linux
allocator `committed` accounting fell by about 256 MiB on reclaim while RSS
fell by about 16 MiB, so that counter must not be presented as physical RSS.
The fixed branch and pinned main have similar synthetic results; this is not
evidence that the unsafe baseline retained-ID behavior is acceptable.

The reset (`MIMALLOC_PURGE_DECOMMITS=0`) cell changes the economics: after the
ordinary purge, the fixed-branch reclaim checkpoint reduced process RSS by a
median **271.97 MiB on Linux**, **256.03 MiB with native MSVC**, and
**256.02 MiB with MinGW-w64**, at median synchronous call times of **1.179,
7.242, and 7.291 ms** respectively. Those figures are within-arm before/after
differences, not paired incremental-control differences, and do not generalize
to an application that already decommits or has live objects in the arenas.

The active-thread, ungated cell requested reclaim in all three repetitions
on each platform and got `pending-subprocess` each time: **zero arenas
released**, with a median call near the configured **100 ms owner-acquisition
wait** (99.3–99.5 ms). The cooperative-idle cell released arenas in all three
runs. With `MI_OWNER_GATE=ON`, the active-thread cell also released arenas in
all three runs; that build carries the separately measured allocation fast-path
cost in [the process-wide purge study](purge-all.md). These controls are the
practical reason a caller must inspect the report and choose its own
quiescent point. Results come from three GitHub-hosted runs per cell on
Linux 6.17 Azure and Windows Server 2025 (native `cl` and MSYS2 MINGW64);
the exact runner names, CPU identities, flags, and all raw samples are in the
linked JSON. The three toolchain jobs are distinct machines, not a
cross-toolchain speed ranking.

This favors explicit opt-in for the present implementation: a caller can pay
the measured pause and re-growth cost only after a known burst and can retry
or skip when owners are pending. Implicit reclaim would put a potential
100 ms no-op wait into otherwise ordinary forced purges under an active
ungated worker. An automatic policy could win in a service with long, reliably
quiescent phases and a sustained large metadata/VA burden, but only with a
proven safe admission protocol and a threshold/cooldown that exceeds the
measured re-growth cost; these three synthetic repetitions do not identify a
generally valid threshold. An implicit policy might make sense only for an
application that already treats *every* forced purge as a quiescent,
potentially blocking maintenance point. Neither alternative is enabled or
authorized as a default by this issue.

## Pending Phase 6 panels

The following metrics are tracked in the dashboard as explicitly pending
placeholder panels until their measurement protocols land:

| Metric | Phase issue |
|---|---|
| Pprof compilation and runtime tax | [#187](https://github.com/zackees/mimalloc-pprof/issues/187) |

Memory ([#184](https://github.com/zackees/mimalloc-pprof/issues/184)), honest
transaction latency ([#185](https://github.com/zackees/mimalloc-pprof/issues/185)),
and thread scaling ([#203](https://github.com/zackees/mimalloc-pprof/issues/203))
have landed; their panels populate on each metric's next scheduled run. The
memory section renders four views over the same sealed envelope
([#211](https://github.com/zackees/mimalloc-pprof/issues/211)): sampled-peak RSS
bars normalized to Microsoft mimalloc (1.0 = Microsoft mimalloc, matching the throughput
panel), a fragmentation-proxy panel with its own 1.0 reference line, an
RSS-over-time timeline with the workload-drained marker and the 100 ms / 1 s /
5 s return-to-OS points annotated, and a speed–memory Pareto scatter (upper-left
is better).

### The fragmentation proxy is optional per cell

The fragmentation proxy is
`(sampled_peak_rss_bytes - baseline_rss_bytes) / peak_live_requested_bytes`: peak
RSS above the post-warmup baseline, per byte the workload asked to keep live. It
only means anything where the live set is the quantity being measured, so it is
reported per cell rather than for every cell
([#222](https://github.com/zackees/mimalloc-pprof/issues/222)).

`thread-churn` is **not applicable by design**. It measures thread creation and
destruction against a live set of about one kilobyte, so the ratio would report
thread-stack and arena RSS divided by an incidental kilobyte — observed values
ran from 21x to 3195x, which is noise, not fragmentation. The scenario is named
in `FRAGMENTATION_EXCLUDED_SCENARIOS` (Rust `memory.rs` and `ci/benchmark_report.py`
agree), it carries no fragmentation summaries at all, and the panel and table
both read `n/a` for it.

Where the proxy does apply, a sample records `fragmentation_proxy: null` plus a
`fragmentation_proxy_reason` instead of a ratio when an operand is unusable:

| reason | meaning |
|---|---|
| `scenario_not_applicable` | the scenario excludes the proxy (above) |
| `non_positive_rss_delta` | peak RSS never rose above the baseline — a real outcome, not an error |
| `zero_live_bytes` | the child reported no peak live bytes, so the ratio has no denominator |
| `non_finite_ratio` | the division did not produce a finite positive ratio |

Exactly one of the value and the reason is ever set; a null without a reason, or
a ratio on an excluded scenario, is rejected by both validators. Memory sections
already published under the older contract — a ratio on every cell, no reason
field, the older `fragmentation_formula` string — keep validating and rendering
as their own lineage, the way older allocator sets do; only a producer is held
to the current shape. A cell keeps
every one of its other metrics (peak RSS, the three post-drain points, retained
bytes) when its proxy is unavailable, and the measurement run continues — this
used to abort the entire run, throwing away ~50 minutes of already-recorded
samples. Blocks where any allocator's proxy is unavailable are dropped from the
fragmentation metric for every allocator, so it keeps the same complete-block
pairing unit as the byte metrics. If that leaves an applicable cell with fewer
than the required 15 blocks, `benchmark-memory-validate` reports it by name
against the assembled run; the raw samples are already on disk either way.
`benchmark-memory.yml` therefore dispatches 17 blocks by default, so a cell can
lose two blocks and still clear the minimum.
