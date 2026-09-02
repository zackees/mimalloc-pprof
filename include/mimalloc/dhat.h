/* Exact, opt-in DHAT v2 heap profiling. Unlike the sampled pprof profiler,
   DHAT tracks every non-internal allocation and is intended for short diagnostic
   runs/tests, not production profiling. It is independent of MI_PPROF and of
   mi_memory_set_callbacks: both observers can run simultaneously.

   Start explicitly with mi_dhat_start(), or set MIMALLOC_DHAT=1 before process
   initialization. MIMALLOC_DHAT_DUMP_AT_EXIT=<path> writes a standard DHAT v2
   JSON report at process exit. MIMALLOC_DHAT_MAX_BYTES bounds raw-OS-backed
   collector state (default 64 MiB); exhaustion is fail-soft and is exposed via
   incomplete/dropped in mi_dhat_stats_t and mi_dhat_incomplete in the JSON.
   Time fields use monotonic milliseconds (tu="ms"), not instruction counts.
   Memory-access profiling is not available: emitted reports always use bkacc=false.

   MI_NO_PROCESS_DETACH interaction: MIMALLOC_DHAT_DUMP_AT_EXIT fires from the
   automatic process-exit path (_mi_auto_process_done), which a build configured with
   MI_NO_PROCESS_DETACH skips entirely. An embedder using MI_NO_PROCESS_DETACH must
   call mi_dhat_dump themselves before exit, or no report is written. */
#pragma once
#ifndef MIMALLOC_DHAT_H
#define MIMALLOC_DHAT_H

#include "mimalloc.h"
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define MI_DHAT_STATS_VERSION 1
typedef struct mi_dhat_stats_s {
  size_t size; int version;
  bool enabled;
  bool incomplete;
  uint64_t total_bytes, total_blocks;
  uint64_t live_bytes, live_blocks;
  uint64_t peak_bytes, peak_blocks;
  uint64_t dropped, internal_bytes;
} mi_dhat_stats_t;
#define mi_dhat_stats_t_decl(name) mi_dhat_stats_t name = { 0 }; name.size = sizeof(mi_dhat_stats_t); name.version = MI_DHAT_STATS_VERSION

/* Starts/stops exact tracking. Starting also activates the internal event path;
   installed mi_memory_set_callbacks observers remain installed and independent. */
mi_decl_export bool mi_dhat_start(void) mi_attr_noexcept;
mi_decl_export void mi_dhat_stop(void) mi_attr_noexcept;
mi_decl_nodiscard mi_decl_export bool mi_dhat_is_enabled(void) mi_attr_noexcept;
mi_decl_nodiscard mi_decl_export bool mi_dhat_stats_get(mi_dhat_stats_t* out) mi_attr_noexcept;

/* Writes a DHAT file-version-2 heap JSON document. `tu` is monotonic milliseconds,
   not Valgrind instruction counts; bkacc is always false. */
mi_decl_nodiscard mi_decl_export bool mi_dhat_dump(const char* path) mi_attr_noexcept;

#ifdef __cplusplus
}
#endif
#endif
