# Benchmarks

*Part of the [mimalloc-pprof](../README.md) documentation.*

mimalloc-pprof is continuously benchmarked against **upstream mimalloc**,
**TCMalloc**, and **jemalloc** on a dedicated Linux x86-64 runner.  Every result
is **GitHub-hosted and informational** — no self-hosted hardware, no hand-picked
runs, no unpublished baselines.

| Resource | Description |
|---|---|
| [**Benchmark dashboard**](https://zackees.github.io/mimalloc-pprof/) | Live per-scenario throughput, paired statistical effects, and full allocator provenance |
| [`benchmark-stats` branch](https://github.com/zackees/mimalloc-pprof/tree/benchmark-stats) | Raw sealed site artifacts (history, manifests, digests) |
| [`latest.json`](https://zackees.github.io/mimalloc-pprof/latest.json) | Machine-readable publication envelope for the most recent headline run |

## Methodology summary

- **4 allocators** — mimalloc-pprof, upstream mimalloc (same `dev3` base),
  TCMalloc, and jemalloc — pinned to immutable commits with SHA-256-verified
  source archives.
- **Paired balanced blocks** — every block runs all four allocators in
  randomized order under one workload seed; ≥15 complete blocks per headline
  cell.
- **Type-7 quantile bootstrap** — 10,000 resamples, splitmix64-rejection PRNG,
  percentile-block confidence intervals at 95%.  Paired effects are expressed
  relative to upstream mimalloc.
- **No profiling during measurement** — `MIMALLOC_PROF=0` and
  `MIMALLOC_MEMORY_EVENTS=0` are set on every child process; the allocator runs
  in its natural configuration.
- **Deterministic reproducibility** — every raw sample carries its exact command
  line and workload seed; the published site manifest carries a detached SHA-256
  digest of every file.

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
splitmix64 chain that never observes allocator behavior — so all four
allocators replay one identical stream inside each paired block.

> **Coverage mode: reduced statistical rigor (3 blocks per cell).**  These
> panels deliberately trade statistical rigor for thread coverage.  They carry
> no confidence intervals and no noise gating; read them for shape, not for
> headline-grade differences.  The runner allows 4 logical CPUs, so the
> 16-thread point is 4× oversubscribed and describes contention, not core
> scaling — it is shaded on every chart.

[![Tiny hot path: aggregate throughput by worker count for all four allocators](https://raw.githubusercontent.com/zackees/mimalloc-pprof/benchmark-stats/benchmark-scaling-tiny-hot.svg)](https://zackees.github.io/mimalloc-pprof/#scaling)

[![General mix including realloc: aggregate throughput by worker count for all four allocators](https://raw.githubusercontent.com/zackees/mimalloc-pprof/benchmark-stats/benchmark-scaling-mixed-general.svg)](https://zackees.github.io/mimalloc-pprof/#scaling)

[![Large page-touched buffers: aggregate throughput by worker count for all four allocators](https://raw.githubusercontent.com/zackees/mimalloc-pprof/benchmark-stats/benchmark-scaling-large-buffers.svg)](https://zackees.github.io/mimalloc-pprof/#scaling)

[![Cross-thread producer/consumer handoff: aggregate throughput by worker count for all four allocators](https://raw.githubusercontent.com/zackees/mimalloc-pprof/benchmark-stats/benchmark-scaling-cross-thread.svg)](https://zackees.github.io/mimalloc-pprof/#scaling)

| Pattern | Sizes | What it stresses |
|---|---|---|
| Tiny hot path | 16–64 B | small-object fast path, high alloc/free rate, small live set |
| General mix | 8 B–4 KiB log-uniform | everyday mix including realloc, medium live set |
| Large buffers | 64 KiB–4 MiB | large allocations with one-byte-per-page touching |
| Cross-thread handoff | 16–512 B | remote-free pressure; blocks are freed by another worker |

*Protocol `throughput-scaling-sparse-v1`, published weekly.  Full per-cell
tables, min/max spreads, and the metric comparison key are on the
[dashboard](https://zackees.github.io/mimalloc-pprof/#scaling).*

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
