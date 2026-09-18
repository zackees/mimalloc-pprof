#include "pprof_tax_adapter.h"

/*
  build.rs supplies both macros as string/integer literals for every
  BENCH_PPROF_TAX_CONFIGURATION build, making a stale/wrong adapter build
  fail at compile time instead of silently changing a measurement's
  identity. See adapter_mimalloc.c for the analogous allocator-identity
  guard.
*/
#ifndef BENCH_PPROF_TAX_CONFIGURATION
#error "BENCH_PPROF_TAX_CONFIGURATION must be a string literal supplied by the benchmark build"
#endif

#ifndef BENCH_PPROF_TAX_HAS_PROFILER_API
#error "BENCH_PPROF_TAX_HAS_PROFILER_API must be 0 or 1, supplied by the benchmark build"
#endif

const char* bench_pprof_configuration_id(void) {
  return BENCH_PPROF_TAX_CONFIGURATION;
}

#if BENCH_PPROF_TAX_HAS_PROFILER_API

#include "mimalloc.h"
#include "mimalloc/profile.h"

int bench_pprof_compiled(void) {
  mi_prof_stats_t_decl(stats);
  return mi_prof_stats_get(&stats) ? 1 : 0;
}

int bench_pprof_start(uint64_t interval_bytes, uint64_t seed) {
  if (interval_bytes == 0) {
    return 0;
  }
  mi_prof_config_t_decl(cfg);
  cfg.mode = MI_PROF_CONFIG_OVERRIDE;
  cfg.sample_interval = (size_t)interval_bytes;
  cfg.seed = seed;
  cfg.accum = true;
  return mi_prof_start_ex(&cfg) ? 1 : 0;
}

void bench_pprof_stop(void) {
  mi_prof_stop();
}

void bench_pprof_reset(void) {
  mi_prof_reset();
}

int bench_pprof_stats(bench_pprof_stats_t* out) {
  if (out == NULL) {
    return 0;
  }
  *out = (bench_pprof_stats_t){0};

  mi_prof_stats_t_decl(stats);
  const bool ok = mi_prof_stats_get(&stats);
  out->compiled = ok ? 1 : 0;
  if (!ok) {
    return 0;
  }
  out->enabled = stats.enabled ? 1 : 0;
  out->accum = stats.accum ? 1 : 0;
  out->sample_interval_bytes = (uint64_t)stats.sample_rate;
  out->accum_samples = (uint64_t)stats.accum_samples;
  out->accum_bytes = (uint64_t)stats.accum_bytes;
  out->live_samples = (uint64_t)stats.live_samples;
  out->live_bytes = (uint64_t)stats.live_bytes;
  out->dropped_samples = (uint64_t)stats.dropped_samples;
  out->arena_committed = (uint64_t)stats.arena_committed;
  return 1;
}

int bench_pprof_dump_proto(const char* path) {
  return mi_prof_dump_proto(path) ? 1 : 0;
}

#else /* !BENCH_PPROF_TAX_HAS_PROFILER_API */

int bench_pprof_compiled(void) {
  return 0;
}

int bench_pprof_start(uint64_t interval_bytes, uint64_t seed) {
  (void)interval_bytes;
  (void)seed;
  return 0;
}

void bench_pprof_stop(void) {
}

void bench_pprof_reset(void) {
}

int bench_pprof_stats(bench_pprof_stats_t* out) {
  if (out == NULL) {
    return 0;
  }
  *out = (bench_pprof_stats_t){0};
  return 0;
}

int bench_pprof_dump_proto(const char* path) {
  (void)path;
  return 0;
}

#endif /* BENCH_PPROF_TAX_HAS_PROFILER_API */
