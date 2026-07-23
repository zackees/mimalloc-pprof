/* Public, allocation-only sampling profiler API. */
#pragma once
#ifndef MIMALLOC_PROFILE_H
#define MIMALLOC_PROFILE_H

#include "mimalloc.h"

#ifdef __cplusplus
extern "C" {
#endif

mi_decl_nodiscard mi_decl_export bool mi_prof_start(size_t sample_rate) mi_attr_noexcept;
mi_decl_nodiscard mi_decl_export bool mi_prof_start_seeded(size_t sample_rate, uint64_t seed) mi_attr_noexcept;
mi_decl_export void mi_prof_stop(void) mi_attr_noexcept;
mi_decl_nodiscard mi_decl_export bool mi_prof_is_enabled(void) mi_attr_noexcept;
mi_decl_export void mi_prof_debug_stats(size_t* records, size_t* bytes, size_t* unique_stacks) mi_attr_noexcept;
/* With MIMALLOC_PROF_ACCUM=1 stack entries stay interned until mi_prof_reset;
   this preserves cumulative counters at the cost of profiler arena memory. */
typedef void (mi_prof_write_fun)(void* arg, const char* buf, size_t len);
mi_decl_nodiscard mi_decl_export bool mi_prof_dump(const char* path) mi_attr_noexcept;
mi_decl_nodiscard mi_decl_export bool mi_prof_dump_writer(mi_prof_write_fun* write, void* arg) mi_attr_noexcept;
mi_decl_export void mi_prof_reset(void) mi_attr_noexcept;

#ifdef __cplusplus
}
#endif
#endif
