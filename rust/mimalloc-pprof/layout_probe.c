/* ABI layout probe for the Rust mirrors in src/sys.rs (issue: C/Rust API parity).

   Every `#[repr(C)]` struct in `src/sys.rs` is a hand-written mirror of a C struct, and
   every `mi_option_*` constant there is a hand-written mirror of an enumerator whose
   value is positional. Both are silent-corruption hazards: a mirror that drifts writes
   the wrong field, or sets the wrong option, with no diagnostic anywhere.

   So the C side publishes what it actually laid out. This file exports a name/value
   table -- `sizeof:<type>`, `offset:<type>.<field>`, `option:<enumerator>`, and the
   versioned/sentinel constants -- which `tests/t19_layout.rs` looks up BY NAME and
   compares against `core::mem::size_of` / `core::mem::offset_of!` and the Rust
   constants. Lookup by name (rather than by index) is deliberate: a field renamed or
   dropped on either side fails as "missing key", never as a coincidentally-equal number.

   This file lives under rust/ on purpose: it is a binding-verification artifact, not
   part of the C library, so it never enters the CMake build or the amalgamation. It is
   compiled into the crate's static library but referenced only from the test, so the
   linker drops it from ordinary consumers.

   Nothing here is gated on MI_PPROF: the profiler's public *types* are declared
   unconditionally in include/mimalloc/profile.h (only the implementation is gated), so
   the layout the mirrors must match is the same in both feature modes. */

#include <stddef.h>

#include "mimalloc-pprof-amalgamated.h"
#include "mimalloc-stats.h"

typedef struct mi_rs_layout_entry_s {
  const char* name;
  size_t      value;
} mi_rs_layout_entry_t;

#define MI_RS_SIZEOF(T)      { "sizeof:" #T, sizeof(T) },
#define MI_RS_OFFSET(T, F)   { "offset:" #T "." #F, offsetof(T, F) },
#define MI_RS_CONST(N)       { "const:" #N, (size_t)(N) },
#define MI_RS_OPTION(N)      { "option:" #N, (size_t)(N) },

static const mi_rs_layout_entry_t mi_rs_layout_entries[] = {
  /* ---- include/mimalloc/profile.h ---- */
  MI_RS_SIZEOF(mi_prof_stats_t)
  MI_RS_OFFSET(mi_prof_stats_t, size)
  MI_RS_OFFSET(mi_prof_stats_t, version)
  MI_RS_OFFSET(mi_prof_stats_t, enabled)
  MI_RS_OFFSET(mi_prof_stats_t, accum)
  MI_RS_OFFSET(mi_prof_stats_t, sample_rate)
  MI_RS_OFFSET(mi_prof_stats_t, live_samples)
  MI_RS_OFFSET(mi_prof_stats_t, live_bytes)
  MI_RS_OFFSET(mi_prof_stats_t, accum_samples)
  MI_RS_OFFSET(mi_prof_stats_t, accum_bytes)
  MI_RS_OFFSET(mi_prof_stats_t, unique_stacks)
  MI_RS_OFFSET(mi_prof_stats_t, arena_committed)
  MI_RS_OFFSET(mi_prof_stats_t, stack_table_overflows)
  MI_RS_OFFSET(mi_prof_stats_t, dropped_samples)
  MI_RS_OFFSET(mi_prof_stats_t, heap_committed)
  MI_RS_OFFSET(mi_prof_stats_t, heap_reserved)
  MI_RS_OFFSET(mi_prof_stats_t, heap_malloc_requested)
  MI_RS_OFFSET(mi_prof_stats_t, heap_pages)
  MI_RS_OFFSET(mi_prof_stats_t, heap_pages_abandoned)
  MI_RS_OFFSET(mi_prof_stats_t, heap_count)
  MI_RS_OFFSET(mi_prof_stats_t, theap_count)
  MI_RS_OFFSET(mi_prof_stats_t, heap_purged)
  MI_RS_OFFSET(mi_prof_stats_t, heap_stats_detailed)

  MI_RS_SIZEOF(mi_prof_config_t)
  MI_RS_OFFSET(mi_prof_config_t, size)
  MI_RS_OFFSET(mi_prof_config_t, version)
  MI_RS_OFFSET(mi_prof_config_t, mode)
  MI_RS_OFFSET(mi_prof_config_t, sample_interval)
  MI_RS_OFFSET(mi_prof_config_t, max_profiler_bytes)
  MI_RS_OFFSET(mi_prof_config_t, seed)
  MI_RS_OFFSET(mi_prof_config_t, accum)
  MI_RS_OFFSET(mi_prof_config_t, max_stack_depth)
  MI_RS_OFFSET(mi_prof_config_t, dump_at_exit)
  MI_RS_OFFSET(mi_prof_config_t, dump_format)

  MI_RS_SIZEOF(mi_prof_sample_info_t)
  MI_RS_OFFSET(mi_prof_sample_info_t, stack)
  MI_RS_OFFSET(mi_prof_sample_info_t, depth)
  MI_RS_OFFSET(mi_prof_sample_info_t, live_objects)
  MI_RS_OFFSET(mi_prof_sample_info_t, live_bytes)
  MI_RS_OFFSET(mi_prof_sample_info_t, accum_objects)
  MI_RS_OFFSET(mi_prof_sample_info_t, accum_bytes)

  MI_RS_SIZEOF(mi_prof_module_info_t)
  MI_RS_OFFSET(mi_prof_module_info_t, path)
  MI_RS_OFFSET(mi_prof_module_info_t, base)
  MI_RS_OFFSET(mi_prof_module_info_t, size)

  MI_RS_CONST(MI_PROF_STAT_VERSION)
  MI_RS_CONST(MI_PROF_CONFIG_VERSION)
  MI_RS_CONST(MI_PROF_FORMAT_TEXT)
  MI_RS_CONST(MI_PROF_FORMAT_PROTO)
  MI_RS_CONST(MI_PROF_CONFIG_FALLBACK)
  MI_RS_CONST(MI_PROF_CONFIG_OVERRIDE)

  /* ---- include/mimalloc/dhat.h ---- */
  MI_RS_SIZEOF(mi_dhat_stats_t)
  MI_RS_OFFSET(mi_dhat_stats_t, size)
  MI_RS_OFFSET(mi_dhat_stats_t, version)
  MI_RS_OFFSET(mi_dhat_stats_t, enabled)
  MI_RS_OFFSET(mi_dhat_stats_t, incomplete)
  MI_RS_OFFSET(mi_dhat_stats_t, total_bytes)
  MI_RS_OFFSET(mi_dhat_stats_t, total_blocks)
  MI_RS_OFFSET(mi_dhat_stats_t, live_bytes)
  MI_RS_OFFSET(mi_dhat_stats_t, live_blocks)
  MI_RS_OFFSET(mi_dhat_stats_t, peak_bytes)
  MI_RS_OFFSET(mi_dhat_stats_t, peak_blocks)
  MI_RS_OFFSET(mi_dhat_stats_t, dropped)
  MI_RS_OFFSET(mi_dhat_stats_t, internal_bytes)
  MI_RS_CONST(MI_DHAT_STATS_VERSION)

  /* ---- include/mimalloc/memory-events.h ---- */
  MI_RS_SIZEOF(mi_memory_change_t)
  MI_RS_OFFSET(mi_memory_change_t, kind)
  MI_RS_OFFSET(mi_memory_change_t, total_bytes)
  MI_RS_OFFSET(mi_memory_change_t, delta_bytes)
  MI_RS_OFFSET(mi_memory_change_t, request_size)

  MI_RS_SIZEOF(mi_memory_callbacks_t)
  MI_RS_OFFSET(mi_memory_callbacks_t, handlers)
  MI_RS_OFFSET(mi_memory_callbacks_t, args)

  MI_RS_SIZEOF(mi_memory_snapshot_t)
  MI_RS_OFFSET(mi_memory_snapshot_t, size)
  MI_RS_OFFSET(mi_memory_snapshot_t, version)
  MI_RS_OFFSET(mi_memory_snapshot_t, live_bytes)
  MI_RS_OFFSET(mi_memory_snapshot_t, accum_bytes)
  MI_RS_OFFSET(mi_memory_snapshot_t, live_count)
  MI_RS_OFFSET(mi_memory_snapshot_t, accum_count)

  MI_RS_CONST(MI_MEMORY_SNAPSHOT_VERSION)
  MI_RS_CONST(MI_MEMORY_ALLOCATE)
  MI_RS_CONST(MI_MEMORY_FREE)
  MI_RS_CONST(MI_MEMORY_RESIZE)
  MI_RS_CONST(MI_MEMORY_CHANGE_COUNT)

  /* ---- include/mimalloc.h: hole purging (issue #272, Bun parity P7b) ---- */
  MI_RS_SIZEOF(mi_purge_holes_stats_t)
  MI_RS_OFFSET(mi_purge_holes_stats_t, purged_bytes)
  MI_RS_OFFSET(mi_purge_holes_stats_t, purged_blocks)
  MI_RS_OFFSET(mi_purge_holes_stats_t, purged_bytes_total)
  MI_RS_OFFSET(mi_purge_holes_stats_t, discard_calls)
  MI_RS_OFFSET(mi_purge_holes_stats_t, reuse_calls)
  MI_RS_OFFSET(mi_purge_holes_stats_t, pages_freed)
  MI_RS_OFFSET(mi_purge_holes_stats_t, ineligible_pages)
  MI_RS_OFFSET(mi_purge_holes_stats_t, ineligible_bytes)
  MI_RS_OFFSET(mi_purge_holes_stats_t, ineligible_free_bytes)
  MI_RS_OFFSET(mi_purge_holes_stats_t, unformed_bytes)
  MI_RS_OFFSET(mi_purge_holes_stats_t, unformed_bytes_total)
  MI_RS_OFFSET(mi_purge_holes_stats_t, unformed_discard_calls)
  MI_RS_OFFSET(mi_purge_holes_stats_t, unformed_reuse_calls)
  MI_RS_OFFSET(mi_purge_holes_stats_t, pages_skipped)
  MI_RS_OFFSET(mi_purge_holes_stats_t, blocks_visited)
  MI_RS_OFFSET(mi_purge_holes_stats_t, full_sweeps)

  /* ---- include/mimalloc.h: mi_purge_all (issue #366) ---- */
  MI_RS_SIZEOF(mi_purge_all_report_t)
  MI_RS_OFFSET(mi_purge_all_report_t, arena_bytes)
  MI_RS_OFFSET(mi_purge_all_report_t, hole_bytes)
  MI_RS_OFFSET(mi_purge_all_report_t, theaps_swept)
  MI_RS_OFFSET(mi_purge_all_report_t, theaps_pending)
  MI_RS_OFFSET(mi_purge_all_report_t, theaps_orphaned)
  MI_RS_OFFSET(mi_purge_all_report_t, gated)
  MI_RS_OFFSET(mi_purge_all_report_t, complete)
  MI_RS_SIZEOF(mi_purge_flags_t)
  MI_RS_CONST(MI_PURGE_FORCE)
  MI_RS_CONST(MI_PURGE_OK)
  MI_RS_CONST(MI_PURGE_PARTIAL)
  MI_RS_CONST(MI_PURGE_BUSY)

  /* ---- include/mimalloc-stats.h ---- */
  MI_RS_SIZEOF(mi_stat_count_t)
  MI_RS_OFFSET(mi_stat_count_t, total)
  MI_RS_OFFSET(mi_stat_count_t, peak)
  MI_RS_OFFSET(mi_stat_count_t, current)
  MI_RS_SIZEOF(mi_stat_counter_t)
  MI_RS_OFFSET(mi_stat_counter_t, total)

  MI_RS_SIZEOF(mi_stats_t)
  MI_RS_OFFSET(mi_stats_t, size)
  MI_RS_OFFSET(mi_stats_t, version)
  /* Generated straight from the header's own field list, so a field added or reordered
     upstream shows up here without this file being touched -- and then fails the Rust
     test as a missing/moved key, which is exactly the intent. */
#define MI_STAT_COUNT(stat)     MI_RS_OFFSET(mi_stats_t, stat)
#define MI_STAT_COUNTER(stat)   MI_RS_OFFSET(mi_stats_t, stat)
  MI_STAT_FIELDS()
#undef MI_STAT_COUNT
#undef MI_STAT_COUNTER
  MI_RS_OFFSET(mi_stats_t, _stat_reserved)
  MI_RS_OFFSET(mi_stats_t, _stat_counter_reserved)
  MI_RS_OFFSET(mi_stats_t, malloc_bins)
  MI_RS_OFFSET(mi_stats_t, page_bins)
  MI_RS_OFFSET(mi_stats_t, chunk_bins)
  MI_RS_CONST(MI_STAT_VERSION)
  MI_RS_CONST(MI_BIN_HUGE)
  MI_RS_CONST(MI_CBIN_COUNT)

  MI_RS_SIZEOF(mi_subproc_id_t)

  /* ---- include/mimalloc.h: mi_option_t ----
     Positional and silently wrong when mirrored stale: the fork inserted thirteen
     enumerators (prof_*, memory_events, purge_zeroes, scavenger, purge_holes*) BEFORE
     `_mi_option_last`, so any Rust mirror copied from upstream sets a different option
     than the caller named. Every enumerator the Rust side mirrors is listed here. */
  MI_RS_OPTION(mi_option_show_errors)
  MI_RS_OPTION(mi_option_show_stats)
  MI_RS_OPTION(mi_option_verbose)
  MI_RS_OPTION(mi_option_deprecated_eager_commit)
  MI_RS_OPTION(mi_option_arena_eager_commit)
  MI_RS_OPTION(mi_option_purge_decommits)
  MI_RS_OPTION(mi_option_allow_large_os_pages)
  MI_RS_OPTION(mi_option_reserve_huge_os_pages)
  MI_RS_OPTION(mi_option_reserve_huge_os_pages_at)
  MI_RS_OPTION(mi_option_reserve_os_memory)
  MI_RS_OPTION(mi_option_deprecated_segment_cache)
  MI_RS_OPTION(mi_option_deprecated_page_reset)
  MI_RS_OPTION(mi_option_deprecated_abandoned_page_purge)
  MI_RS_OPTION(mi_option_deprecated_segment_reset)
  MI_RS_OPTION(mi_option_deprecated_eager_commit_delay)
  MI_RS_OPTION(mi_option_purge_delay)
  MI_RS_OPTION(mi_option_use_numa_nodes)
  MI_RS_OPTION(mi_option_disallow_os_alloc)
  MI_RS_OPTION(mi_option_os_tag)
  MI_RS_OPTION(mi_option_max_errors)
  MI_RS_OPTION(mi_option_max_warnings)
  MI_RS_OPTION(mi_option_deprecated_max_segment_reclaim)
  MI_RS_OPTION(mi_option_destroy_on_exit)
  MI_RS_OPTION(mi_option_arena_reserve)
  MI_RS_OPTION(mi_option_arena_purge_mult)
  MI_RS_OPTION(mi_option_deprecated_purge_extend_delay)
  MI_RS_OPTION(mi_option_disallow_arena_alloc)
  MI_RS_OPTION(mi_option_retry_on_oom)
  MI_RS_OPTION(mi_option_deprecated_visit_abandoned)
  MI_RS_OPTION(mi_option_guarded_min)
  MI_RS_OPTION(mi_option_guarded_max)
  MI_RS_OPTION(mi_option_guarded_precise)
  MI_RS_OPTION(mi_option_guarded_sample_rate)
  MI_RS_OPTION(mi_option_guarded_sample_seed)
  MI_RS_OPTION(mi_option_generic_collect)
  MI_RS_OPTION(mi_option_page_reclaim_on_free)
  MI_RS_OPTION(mi_option_page_full_retain)
  MI_RS_OPTION(mi_option_page_max_candidates)
  MI_RS_OPTION(mi_option_max_vabits)
  MI_RS_OPTION(mi_option_pagemap_commit)
  MI_RS_OPTION(mi_option_page_commit_on_demand)
  MI_RS_OPTION(mi_option_page_max_reclaim)
  MI_RS_OPTION(mi_option_page_cross_thread_max_reclaim)
  MI_RS_OPTION(mi_option_allow_thp)
  MI_RS_OPTION(mi_option_minimal_purge_size)
  MI_RS_OPTION(mi_option_arena_max_object_size)
  MI_RS_OPTION(mi_option_arena_is_numa_local)
  /* fork additions start here (indices 47..59) */
  MI_RS_OPTION(mi_option_prof)
  MI_RS_OPTION(mi_option_prof_sample_rate)
  MI_RS_OPTION(mi_option_prof_bt_max)
  MI_RS_OPTION(mi_option_prof_accum)
  MI_RS_OPTION(mi_option_prof_seed)
  MI_RS_OPTION(mi_option_prof_max_bytes)
  MI_RS_OPTION(mi_option_memory_events)
  MI_RS_OPTION(mi_option_purge_zeroes)
  MI_RS_OPTION(mi_option_scavenger)
  MI_RS_OPTION(mi_option_purge_holes)
  MI_RS_OPTION(mi_option_purge_holes_eager_zero)
  MI_RS_OPTION(mi_option_purge_holes_min_interval)
  MI_RS_OPTION(mi_option_purge_holes_full_every)
  MI_RS_OPTION(mi_option_snapshot_on_exit)
  MI_RS_OPTION(_mi_option_last)
  /* deprecated aliases, defined after the sentinel with explicit values */
  MI_RS_OPTION(mi_option_large_os_pages)
  MI_RS_OPTION(mi_option_eager_region_commit)
  MI_RS_OPTION(mi_option_reset_decommits)
  MI_RS_OPTION(mi_option_reset_delay)
  MI_RS_OPTION(mi_option_limit_os_alloc)
};

/* Returns the entry table and writes its length to `*count`. The table is static
   `const` data with static-string names; the caller never frees anything. */
mi_decl_export const mi_rs_layout_entry_t* mi_rs_layout_table(size_t* count) mi_attr_noexcept {
  if (count != NULL) { *count = sizeof(mi_rs_layout_entries) / sizeof(mi_rs_layout_entries[0]); }
  return mi_rs_layout_entries;
}
