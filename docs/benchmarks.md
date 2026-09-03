# Benchmarks

*Part of the [mimalloc-pprof](../README.md) documentation.*

mimalloc-pprof is continuously benchmarked against **upstream mimalloc**,
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

- **5 allocators** — mimalloc-pprof, upstream mimalloc (pinned `dev3@bcee5a88`,
  v3.4.3), Bun's mimalloc fork, TCMalloc, and jemalloc — pinned to immutable
  commits with SHA-256-verified source archives.
- **Paired balanced blocks** — every block runs all five allocators in
  randomized order under one workload seed; ≥15 complete blocks per headline
  cell.
- **Type-7 quantile bootstrap** — 10,000 resamples, splitmix64-rejection PRNG,
  percentile-block confidence intervals at 95%.  Paired effects are expressed
  relative to upstream mimalloc.
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
|---|---|---|---|
| `tcmalloc` | tcmalloc | `google/tcmalloc@c316de3e` | bazel `-c opt` |
| `jemalloc` | jemalloc | `jemalloc/jemalloc` 5.3.1 `@81034ce1` | autoconf/make, static only |
| `upstream-mimalloc` | upstream-mimalloc | `microsoft/mimalloc` `dev3@bcee5a88` | cmake-ninja, `MI_PPROF=OFF` |
| `bun-mimalloc` | Bun mimalloc (oven-sh @ `b20b60d9`) | `oven-sh/mimalloc` `bun-dev3-v2@b20b60d9` | cmake-ninja, no `MI_PPROF` option |
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
paired effects expressed relative to upstream mimalloc:

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

## Not covered by the five-allocator matrix

The README's [hole-purging chart](../README.md#hole-purging-measured) is a
separate, single-binary measurement (`ci/bench_hole_purging.py`) that compares
`MIMALLOC_PURGE_HOLES=0` against `=1` in one build.  Bun's fork carries the same
`mi_on_thread_idle` / `mi_purge_holes_stats_t` API, so a third `bun` series is
technically possible, but the committed light/dark SVGs, CSV and report JSON are
one measured artifact set produced together on the reference machine — adding a
series means re-measuring all of it, not editing the renderer.  It is therefore
left for a run on that machine rather than fabricated here.

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
bars normalized to upstream mimalloc (1.0 = upstream, matching the throughput
panel), a fragmentation-proxy panel with its own 1.0 reference line, an
RSS-over-time timeline with the workload-drained marker and the 100 ms / 1 s /
5 s return-to-OS points annotated, and a speed–memory Pareto scatter (upper-left
is better).
