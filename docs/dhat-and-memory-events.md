# Exact DHAT profiling and the memory-events API

*Part of the [mimalloc-pprof](../README.md) documentation.*

## Exact DHAT profiling

`<mimalloc/dhat.h>` provides an **exact**, high-overhead heap/lifetime observer that
writes [DHAT file-version 2](https://valgrind.org/docs/manual/dh-manual.html) JSON for
`dh_view.html`. Unlike the production-oriented sampled pprof profiler, it retains one
raw-OS-backed record for every observed live allocation. Use it for short tests and
focused investigations, not a continuously running production workload.

```c
#include <mimalloc.h>
#include <mimalloc/dhat.h>

int main(void) {
  if (!mi_dhat_start()) return 1;
  void* p = mi_malloc(4096);
  mi_free(p);
  mi_dhat_stop();                 /* stop observing; retained report can still dump */
  return mi_dhat_dump("heap.dhat.json") ? 0 : 2;
}
```

DHAT is built independently of `MI_PPROF` and coexists with an application-installed
`mi_memory_set_callbacks` table. It observes pointer identity before the application
callback, suppresses callback-internal allocations just as memory-events does, and
commits its ledger update after the callback returns. It records requested bytes,
not allocator slack, and emits heap/lifetime metrics only (`bklt: true`, `bkacc: false`):
it does **not** claim reads, writes, copy traffic, access histograms, or instruction
counts.

Set `MIMALLOC_DHAT=1` to start at process initialization and
`MIMALLOC_DHAT_DUMP_AT_EXIT=heap.dhat.json` to write an exit report. The timestamps
are monotonic wall-clock milliseconds (`tu: "ms"`), not Valgrind instruction counts.
`MIMALLOC_DHAT_MAX_BYTES` bounds persistent raw-OS collector state (default 64 MiB).
When the budget is exhausted the application allocation still succeeds; the collector
marks the report partial (`mi_dhat_incomplete`) and exposes the drop count through
`mi_dhat_stats_t`.

## Memory-events API

[`include/mimalloc/memory-events.h`](../include/mimalloc/memory-events.h) exposes
opt-in allocation-change counters, callbacks, a best-effort live-allocation
visitor, and raw-OS-layer `mi_unwrapped_*` functions for instrumentation that must
avoid allocator recursion. It is **independent of `MI_PPROF`** and remains
available in an `MI_PPROF=OFF` build.

Enable tracking before the first allocation when exact lifetime totals matter:

```c
#include <mimalloc/memory-events.h>

mi_memory_tracking_set_enabled(true);

mi_memory_snapshot_t_decl(snapshot);
if (mi_memory_snapshot(&snapshot)) {
  /* snapshot.live_bytes and snapshot.accum_bytes are now available */
}
```

Alternatively set `MIMALLOC_MEMORY_EVENTS=1` before launch. Enabling tracking
later does not reconstruct allocations made while it was disabled. Callback
reentrancy, pointer lifetime, and live-visitor restrictions are documented in the
header.
