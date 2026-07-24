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

/* Structured start configuration (see mi_prof_start_ex below). `mode` selects how
   non-default (non-zero/non-NULL) struct fields interact with the environment:
     MI_PROF_CONFIG_FALLBACK (=0): the struct is a fallback -- if the corresponding
       env var (or, for accum/max_stack_depth/max_profiler_bytes, its mi_option_*)
       is present, the env/option value wins and the struct field is ignored; the
       struct field only applies where the env var is absent.
     MI_PROF_CONFIG_OVERRIDE (=1): non-zero/non-NULL struct fields always win
       (applied via mi_option_set where applicable), regardless of env; zeroed
       fields fall back to env-then-default, same as FALLBACK.
   In both modes a fully zeroed config (mi_prof_config_t_decl's initial state)
   behaves exactly like mi_prof_start(0). Note that because 0/NULL doubles as
   "field not set", `accum == false`, `dump_format == MI_PROF_FORMAT_TEXT`, and
   `max_profiler_bytes == 0` cannot be distinguished from "unset" in OVERRIDE
   mode -- they always fall back to env-then-default rather than forcing the
   off/default value. This matches their existing defaults, so it only matters
   if you need OVERRIDE to force off a value enabled via the environment. */
#define MI_PROF_FORMAT_TEXT  0
#define MI_PROF_FORMAT_PROTO 1
#define MI_PROF_CONFIG_VERSION 1
typedef enum mi_prof_config_mode_e { MI_PROF_CONFIG_FALLBACK = 0, MI_PROF_CONFIG_OVERRIDE = 1 } mi_prof_config_mode_t;
typedef struct mi_prof_config_s {
  size_t size; int version;
  int mode;                     // mi_prof_config_mode_t
  size_t sample_interval;       // avg bytes between samples; 0 = env/default (512 KiB)
  /* Budget (bytes) for profiler-internal *persistent sampling state* only (sample
     records, the stack intern table, and interned stack entries -- everything
     backed by _mi_prof_arena_alloc). 0 = unbudgeted (cap-bounded only). Dump,
     snapshot, and profile.proto scratch buffers are transient and always use
     _mi_os_alloc directly, never this arena, so they are never counted here. */
  size_t max_profiler_bytes;
  uint64_t seed;                // 0 = nondeterministic
  bool accum;
  size_t max_stack_depth;       // 0 = default (32); compile cap 128
  const char* dump_at_exit;     // NULL = none; copied into the internal buffer
  int dump_format;              // MI_PROF_FORMAT_TEXT / _PROTO, for the exit dump
} mi_prof_config_t;
#define mi_prof_config_t_decl(name)  mi_prof_config_t name = { 0 }; name.size = sizeof(mi_prof_config_t); name.version = MI_PROF_CONFIG_VERSION
mi_decl_nodiscard mi_decl_export bool mi_prof_start_ex(const mi_prof_config_t* config) mi_attr_noexcept;

mi_decl_nodiscard mi_decl_export bool mi_prof_is_enabled(void) mi_attr_noexcept;
/* deprecated: use mi_prof_stats_get */
mi_decl_export void mi_prof_debug_stats(size_t* records, size_t* bytes, size_t* unique_stacks) mi_attr_noexcept;
/* With MIMALLOC_PROF_ACCUM=1 stack entries stay interned until mi_prof_reset;
   this preserves cumulative counters at the cost of profiler arena memory.
   mi_prof_stats_get and the visitor API below report raw sampled counts;
   multiply by sample_rate to estimate the un-sampled allocation stream. */
typedef void (mi_prof_write_fun)(void* arg, const char* buf, size_t len);
mi_decl_nodiscard mi_decl_export bool mi_prof_dump(const char* path) mi_attr_noexcept;
mi_decl_nodiscard mi_decl_export bool mi_prof_dump_writer(mi_prof_write_fun* write, void* arg) mi_attr_noexcept;
/* profile.proto (google/pprof) writer: same sample/period/mapping semantics as
   mi_prof_dump/mi_prof_dump_writer above but encoded as an uncompressed, binary
   pprof Profile message instead of the legacy "heap profile:" text. */
mi_decl_nodiscard mi_decl_export bool mi_prof_dump_proto(const char* path) mi_attr_noexcept;
mi_decl_nodiscard mi_decl_export bool mi_prof_dump_proto_writer(mi_prof_write_fun* write, void* arg) mi_attr_noexcept;
mi_decl_export void mi_prof_reset(void) mi_attr_noexcept;

#define MI_PROF_STAT_VERSION 1
typedef struct mi_prof_stats_s {
  size_t size; int version;
  bool   enabled; bool accum;
  size_t sample_rate;
  size_t live_samples;   size_t live_bytes;
  size_t accum_samples;  size_t accum_bytes;
  size_t unique_stacks;
  size_t arena_committed;
  size_t stack_table_overflows;
} mi_prof_stats_t;
#define mi_prof_stats_t_decl(name) mi_prof_stats_t name = { 0 }; name.size = sizeof(mi_prof_stats_t); name.version = MI_PROF_STAT_VERSION
mi_decl_nodiscard mi_decl_export bool mi_prof_stats_get(mi_prof_stats_t* stats) mi_attr_noexcept;

typedef struct mi_prof_sample_info_s {
  const void* const* stack;
  size_t depth;
  size_t live_objects;  size_t live_bytes;
  size_t accum_objects; size_t accum_bytes;
} mi_prof_sample_info_t;
typedef bool (mi_prof_visit_fun)(const mi_prof_sample_info_t* info, void* arg);
mi_decl_nodiscard mi_decl_export bool mi_prof_visit(mi_prof_visit_fun* visitor, void* arg) mi_attr_noexcept;

typedef struct mi_prof_snapshot_s mi_prof_snapshot_t;
mi_decl_nodiscard mi_decl_export mi_prof_snapshot_t* mi_prof_snapshot_new(void) mi_attr_noexcept;
mi_decl_nodiscard mi_decl_export bool mi_prof_snapshot_visit(const mi_prof_snapshot_t* snap, mi_prof_visit_fun* visitor, void* arg) mi_attr_noexcept;
mi_decl_export void mi_prof_snapshot_free(mi_prof_snapshot_t* snap) mi_attr_noexcept;

/* Structured module (mapping) enumeration, e.g. to build pprof Mapping entries
   yourself. No profiler lock is taken: module lists are OS-owned, not part of
   the sampled-allocation table. `info` (and `info->path`) are valid only for
   the duration of the callback. */
typedef struct mi_prof_module_info_s { const char* path; uintptr_t base; size_t size; } mi_prof_module_info_t;
typedef bool (mi_prof_module_visit_fun)(const mi_prof_module_info_t* info, void* arg);
mi_decl_nodiscard mi_decl_export bool mi_prof_modules_visit(mi_prof_module_visit_fun* visitor, void* arg) mi_attr_noexcept;

#ifdef __cplusplus
}
#endif
#endif
