/*
  A small, allocator-neutral seam for the pprof compilation/runtime tax panel
  (issue #187, Phase 6D).

  Unlike allocator_adapter.h (which every benchmark child links exactly one
  implementation of), this header is optional: only children built with
  BENCH_PPROF_TAX_CONFIGURATION set link an implementation of it, and that
  implementation is a stub -- reporting itself uncompiled, refusing to start
  -- for the upstream-baseline configuration, which has no profiler to
  control. The fork configurations drive the fork's public sampling profiler
  (include/mimalloc/profile.h) exclusively through its public API.
*/
#ifndef MIMALLOC_PPROF_BENCHMARK_SUITE_PPROF_TAX_ADAPTER_H
#define MIMALLOC_PPROF_BENCHMARK_SUITE_PPROF_TAX_ADAPTER_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct bench_pprof_stats_s {
  int compiled;
  int enabled;
  int accum;
  uint64_t sample_interval_bytes;
  uint64_t accum_samples;
  uint64_t accum_bytes;
  uint64_t live_samples;
  uint64_t live_bytes;
  uint64_t dropped_samples;
  uint64_t arena_committed;
} bench_pprof_stats_t;

/* The compiled BENCH_PPROF_TAX_CONFIGURATION identity, e.g. "fork-pprof-on". */
const char* bench_pprof_configuration_id(void);

/* 1 iff mi_prof_stats_get succeeds (i.e. this child was built with
   MI_PPROF=ON); 0 for the upstream-baseline stub. */
int bench_pprof_compiled(void);

/* 1 on a successful start, 0 otherwise (not compiled, interval_bytes == 0,
   or the underlying mi_prof_start_ex call refused). */
int bench_pprof_start(uint64_t interval_bytes, uint64_t seed);

void bench_pprof_stop(void);
void bench_pprof_reset(void);

/* 1 on success (out filled from the live profiler state); 0 otherwise, in
   which case *out is zeroed with compiled == 0. */
int bench_pprof_stats(bench_pprof_stats_t* out);

/* 1 on success. */
int bench_pprof_dump_proto(const char* path);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* MIMALLOC_PPROF_BENCHMARK_SUITE_PPROF_TAX_ADAPTER_H */
