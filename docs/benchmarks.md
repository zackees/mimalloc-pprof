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

- **5 allocators** — mimalloc-pprof, Microsoft mimalloc (pinned `dev3@bcee5a88`,
  v3.4.3), Bun's mimalloc fork, TCMalloc, and jemalloc — pinned to immutable
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
- **The three mimalloc rows are not the same base.**  One recipe is not one
  tree: the `upstream-mimalloc` row is pinned at `bcee5a88`, which is
  `MI_MALLOC_VERSION 30403` (v3.4.3), while Bun's `b20b60d9` and this fork's
  base `6def7be9` are both `30500` (v3.5.0).  Among other things,
  `MI_OPT_FREE_SMALL` does not exist in v3.4.3 and auto-enables in the two
  v3.5.0 trees, so the Bun and fork rows are compiled with `MI_OPT_FREE_SMALL=1`
  and the upstream row is not.  Read upstream-vs-Bun and upstream-vs-fork
  differences as *base plus fork*, never as fork alone; the Bun-vs-fork
  comparison is the like-for-like one.  Bumping the upstream row to the fork's
  own base is tracked in
  [#332](https://github.com/zackees/mimalloc-pprof/issues/332).
- **Deterministic reproducibility** — every raw sample carries its exact command
  line and workload seed; the published site manifest carries a detached SHA-256
  digest of every file.

### Pinned sources

Every competitor is an immutable archive verified by SHA-256 before extraction;
the fork itself is the workflow checkout.  The lockfile is
[`rust/benchmark-suite/allocators/allocator-lock.json`](../rust/benchmark-suite/allocators/allocator-lock.json).

| Row | Legend label | Pin | Build |
<!-- #375: the Legend label column is ci/benchmark_report.py's ALLOCATOR_LABELS; a test asserts the charts render these. -->
|---|---|---|---|
| `tcmalloc` | TCMalloc | `google/tcmalloc@c316de3e` | bazel `-c opt` |
| `jemalloc` | jemalloc | `jemalloc/jemalloc` 5.3.1 `@81034ce1` | autoconf/make, static only |
| `upstream-mimalloc` | Microsoft mimalloc | `microsoft/mimalloc` `dev3@bcee5a88` | cmake-ninja, `MI_PPROF=OFF` |
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

*Protocol `throughput-scaling-sparse-v1`, published weekly.  Full per-cell
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
