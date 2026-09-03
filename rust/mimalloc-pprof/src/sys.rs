//! Raw bindings to the in-tree mimalloc static amalgamation.

use core::ffi::c_char;
use core::ffi::c_int;
use core::ffi::c_void;

pub type MiProfWriteFun = unsafe extern "C" fn(*mut c_void, *const c_char, usize);

/// Mirrors `MI_PROF_STAT_VERSION` in `include/mimalloc/profile.h`.
pub const MI_PROF_STAT_VERSION: c_int = 3;

/// Mirrors `MI_DHAT_STATS_VERSION` in `include/mimalloc/dhat.h`.
pub const MI_DHAT_STATS_VERSION: c_int = 1;

/// Mirrors `mi_dhat_stats_t` (include/mimalloc/dhat.h) field-for-field.
#[repr(C)]
#[allow(non_camel_case_types)]
pub struct mi_dhat_stats_t {
    pub size: usize,
    pub version: c_int,
    pub enabled: bool,
    pub incomplete: bool,
    pub total_bytes: u64,
    pub total_blocks: u64,
    pub live_bytes: u64,
    pub live_blocks: u64,
    pub peak_bytes: u64,
    pub peak_blocks: u64,
    pub dropped: u64,
    pub internal_bytes: u64,
}

/// Mirrors `mi_prof_stats_t` (include/mimalloc/profile.h) field-for-field.
#[repr(C)]
#[allow(non_camel_case_types)]
pub struct mi_prof_stats_t {
    pub size: usize,
    pub version: c_int,
    pub enabled: bool,
    pub accum: bool,
    pub sample_rate: usize,
    pub live_samples: usize,
    pub live_bytes: usize,
    pub accum_samples: usize,
    pub accum_bytes: usize,
    pub unique_stacks: usize,
    pub arena_committed: usize,
    pub stack_table_overflows: usize,
    /// v2. Mirrors `mi_prof_stats_t.dropped_samples`: count of ALL dropped
    /// samples (record-alloc failure, stack-intern failure, including the
    /// `MI_PROF_STACK_CAP` cap); `stack_table_overflows` is a subset, so
    /// `dropped_samples >= stack_table_overflows` always.
    pub dropped_samples: usize,
    /// v3. Allocator-level ("ground truth") counters read from `mi_stats_get()`.
    /// Unlike every field above, these are exact rather than sampled.
    pub heap_committed: usize,
    pub heap_reserved: usize,
    pub heap_malloc_requested: usize,
    pub heap_pages: usize,
    pub heap_pages_abandoned: usize,
    pub heap_count: usize,
    /// Live thread-local heaps. The main thread's statically-initialized theap
    /// is not counted, so a single-threaded process reports 0.
    pub theap_count: usize,
    pub heap_purged: usize,
    /// True when the C library was built with `MI_STAT >= 2`. `heap_malloc_requested`
    /// is only maintained at that level; a default release build reports 0.
    pub heap_stats_detailed: bool,
}

/// Mirrors `MI_PROF_CONFIG_VERSION` in `include/mimalloc/profile.h`.
pub const MI_PROF_CONFIG_VERSION: c_int = 1;

/// Mirrors `MI_PROF_FORMAT_TEXT` / `MI_PROF_FORMAT_PROTO` (include/mimalloc/profile.h).
pub const MI_PROF_FORMAT_TEXT: c_int = 0;
pub const MI_PROF_FORMAT_PROTO: c_int = 1;

/// Mirrors `mi_prof_config_mode_t`'s `MI_PROF_CONFIG_FALLBACK` /
/// `MI_PROF_CONFIG_OVERRIDE` (include/mimalloc/profile.h).
pub const MI_PROF_CONFIG_FALLBACK: c_int = 0;
pub const MI_PROF_CONFIG_OVERRIDE: c_int = 1;

/// Mirrors `mi_prof_config_t` (include/mimalloc/profile.h) field-for-field.
#[repr(C)]
#[allow(non_camel_case_types)]
pub struct mi_prof_config_t {
    pub size: usize,
    pub version: c_int,
    pub mode: c_int, // mi_prof_config_mode_t
    pub sample_interval: usize,
    pub max_profiler_bytes: usize,
    pub seed: u64,
    pub accum: bool,
    pub max_stack_depth: usize,
    pub dump_at_exit: *const c_char,
    pub dump_format: c_int,
}

/// Mirrors `mi_prof_sample_info_t` (include/mimalloc/profile.h) field-for-field.
#[repr(C)]
#[allow(non_camel_case_types)]
pub struct mi_prof_sample_info_t {
    pub stack: *const *const c_void,
    pub depth: usize,
    pub live_objects: usize,
    pub live_bytes: usize,
    pub accum_objects: usize,
    pub accum_bytes: usize,
}

#[allow(non_camel_case_types)]
pub type mi_prof_visit_fun =
    unsafe extern "C" fn(info: *const mi_prof_sample_info_t, arg: *mut c_void) -> bool;

/// Opaque handle for `mi_prof_snapshot_t`; the profiler never hands out a
/// value, only a pointer, so this type is never constructed on the Rust side.
#[allow(non_camel_case_types)]
pub enum mi_prof_snapshot_t {}

/// Mirrors `mi_prof_module_info_t` (include/mimalloc/profile.h) field-for-field.
#[repr(C)]
#[allow(non_camel_case_types)]
pub struct mi_prof_module_info_t {
    pub path: *const c_char,
    pub base: usize,
    pub size: usize,
}

#[allow(non_camel_case_types)]
pub type mi_prof_module_visit_fun =
    unsafe extern "C" fn(info: *const mi_prof_module_info_t, arg: *mut c_void) -> bool;

/// Opaque handle for `mi_heap_t` (issue #269, Bun parity P4): only ever seen behind a
/// pointer here (mi_heap_get_seq), so this type is never constructed on the Rust side.
#[allow(non_camel_case_types)]
pub enum mi_heap_t {}

/// Mirrors `mi_purge_holes_stats_t` (issue #272, Bun parity P7b): what page hole purging
/// actually reclaimed, process wide. Field order and types must match `mimalloc.h` exactly.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct MiPurgeHolesStats {
    pub purged_bytes: usize,
    pub purged_blocks: usize,
    pub purged_bytes_total: usize,
    pub discard_calls: usize,
    pub reuse_calls: usize,
    pub pages_freed: usize,
    pub ineligible_pages: usize,
    pub ineligible_bytes: usize,
    pub ineligible_free_bytes: usize,
    pub unformed_bytes: usize,
    pub unformed_bytes_total: usize,
    pub unformed_discard_calls: usize,
    pub unformed_reuse_calls: usize,
    pub pages_skipped: usize,
    pub blocks_visited: usize,
    pub full_sweeps: usize,
}

// ---------------------------------------------------------------------------------------
// include/mimalloc-stats.h -- exact allocator statistics (upstream API)
// ---------------------------------------------------------------------------------------

/// Mirrors `MI_STAT_VERSION` in `include/mimalloc-stats.h`.
pub const MI_STAT_VERSION: usize = 5;

/// Mirrors `MI_BIN_HUGE` in `include/mimalloc-stats.h`; the bin arrays below hold
/// `MI_BIN_HUGE + 1` entries.
pub const MI_BIN_HUGE: usize = 73;

/// Mirrors `MI_CBIN_COUNT` (`mi_chunkbin_t`) in `include/mimalloc-stats.h`.
pub const MI_CBIN_COUNT: usize = 6;

/// Mirrors `mi_stat_count_t`: a quantity tracked over time.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
#[allow(non_camel_case_types)]
pub struct mi_stat_count_t {
    /// Total ever allocated.
    pub total: i64,
    /// Peak simultaneous value.
    pub peak: i64,
    /// Value right now.
    pub current: i64,
}

/// Mirrors `mi_stat_counter_t`: a monotonically increasing counter.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
#[allow(non_camel_case_types)]
pub struct mi_stat_counter_t {
    /// Total count.
    pub total: i64,
}

/// Mirrors `mi_stats_t` (include/mimalloc-stats.h) field-for-field, including the
/// reserved-for-future-use arrays and the three size-segregated bin arrays.
///
/// This is upstream's struct, unchanged by the fork -- in particular it carries **no**
/// hole-purging or idle-sweep gauges. Those live in [`MiPurgeHolesStats`], because the
/// sweep also covers pages no heap owns and `mi_stats_t` cannot grow (it is embedded in
/// a theap, at the meta-allocator's 8 KB block limit).
///
/// Field order, offsets and total size are checked against the C library at test time by
/// `tests/t19_layout.rs` (see `layout_probe.c`), which walks the header's own
/// `MI_STAT_FIELDS` list -- so an upstream reordering fails loudly instead of silently
/// reading the wrong counter.
#[repr(C)]
#[derive(Clone, Copy, Debug)]
#[allow(non_camel_case_types)]
pub struct mi_stats_t {
    /// `sizeof(mi_stats_t)` as the C library sees it.
    pub size: usize,
    /// [`MI_STAT_VERSION`] as the C library sees it.
    pub version: usize,
    /// Count of mimalloc pages.
    pub pages: mi_stat_count_t,
    /// Reserved memory bytes.
    pub reserved: mi_stat_count_t,
    /// Committed bytes.
    pub committed: mi_stat_count_t,
    /// Reset bytes.
    pub reset: mi_stat_counter_t,
    /// Purged bytes.
    pub purged: mi_stat_counter_t,
    /// Committed memory inside pages.
    pub page_committed: mi_stat_count_t,
    /// Abandoned page count.
    pub pages_abandoned: mi_stat_count_t,
    /// Number of threads.
    pub threads: mi_stat_count_t,
    /// Allocated bytes `<= MI_LARGE_OBJ_SIZE_MAX`.
    pub malloc_normal: mi_stat_count_t,
    /// Allocated bytes in huge pages.
    pub malloc_huge: mi_stat_count_t,
    /// Bytes the application actually asked for; only maintained at `MI_STAT >= 2`.
    pub malloc_requested: mi_stat_count_t,
    /// `mmap`/`VirtualAlloc` calls.
    pub mmap_calls: mi_stat_counter_t,
    /// Commit calls.
    pub commit_calls: mi_stat_counter_t,
    /// Reset calls.
    pub reset_calls: mi_stat_counter_t,
    /// Purge calls.
    pub purge_calls: mi_stat_counter_t,
    /// Number of memory arenas.
    pub arena_count: mi_stat_counter_t,
    /// Number of blocks `<= MI_LARGE_OBJ_SIZE_MAX`.
    pub malloc_normal_count: mi_stat_counter_t,
    /// Number of huge blocks.
    pub malloc_huge_count: mi_stat_counter_t,
    /// Number of allocations with guard pages.
    pub malloc_guarded_count: mi_stat_counter_t,
    /// Internal: arena rollbacks.
    pub arena_rollback_count: mi_stat_counter_t,
    /// Internal: arena purges.
    pub arena_purges: mi_stat_counter_t,
    /// Internal: page extensions.
    pub pages_extended: mi_stat_counter_t,
    /// Internal: retired pages.
    pub pages_retire: mi_stat_counter_t,
    /// Internal: total pages searched for a fresh page.
    pub page_searches: mi_stat_counter_t,
    /// Internal: searched count for a fresh page.
    pub page_searches_count: mi_stat_counter_t,
    /// v1/v2 only; always zero on this v3 line.
    pub segments: mi_stat_count_t,
    /// v1/v2 only; always zero on this v3 line.
    pub segments_abandoned: mi_stat_count_t,
    /// v1/v2 only; always zero on this v3 line.
    pub segments_cache: mi_stat_count_t,
    /// v1/v2 only; always zero on this v3 line.
    pub _segments_reserved: mi_stat_count_t,
    /// v3 only: first-class heaps.
    pub heaps: mi_stat_count_t,
    /// v3 only: thread-local heaps (`mi_theap_t`).
    pub theaps: mi_stat_count_t,
    /// v3 only: pages reclaimed on allocation.
    pub pages_reclaim_on_alloc: mi_stat_counter_t,
    /// v3 only: pages reclaimed on free.
    pub pages_reclaim_on_free: mi_stat_counter_t,
    /// v3 only: full pages re-abandoned.
    pub pages_reabandon_full: mi_stat_counter_t,
    /// v3 only: busy waits while unabandoning a page.
    pub pages_unabandon_busy_wait: mi_stat_counter_t,
    /// v3 only: waits while deleting a heap.
    pub heaps_delete_wait: mi_stat_counter_t,
    /// Upstream's future-extension padding; do not read.
    pub _stat_reserved: [mi_stat_count_t; 4],
    /// Upstream's future-extension padding; do not read.
    pub _stat_counter_reserved: [mi_stat_counter_t; 4],
    /// Allocation per size bin.
    pub malloc_bins: [mi_stat_count_t; MI_BIN_HUGE + 1],
    /// Pages allocated per size bin.
    pub page_bins: [mi_stat_count_t; MI_BIN_HUGE + 1],
    /// Chunks per page size (`mi_chunkbin_t`).
    pub chunk_bins: [mi_stat_count_t; MI_CBIN_COUNT],
}

/// Mirrors `mi_subproc_id_t` (include/mimalloc.h): an abstract, pointer-sized handle.
#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[allow(non_camel_case_types)]
pub struct mi_subproc_id_t {
    /// The opaque identifier. Only ever produced by `mi_subproc_main`/`mi_subproc_current`.
    pub _mi_subproc_id: *mut c_void,
}

/// Mirrors `mi_output_fun` (include/mimalloc.h): mimalloc's text output sink.
#[allow(non_camel_case_types)]
pub type mi_output_fun = unsafe extern "C" fn(msg: *const c_char, arg: *mut c_void);

// ---------------------------------------------------------------------------------------
// include/mimalloc/memory-events.h -- allocation-change accounting (always compiled in)
// ---------------------------------------------------------------------------------------

/// Mirrors `MI_MEMORY_SNAPSHOT_VERSION` in `include/mimalloc/memory-events.h`.
pub const MI_MEMORY_SNAPSHOT_VERSION: c_int = 1;

/// Mirrors `mi_memory_change_kind_t`. Declared as a plain `c_int` rather than a Rust
/// `enum` on purpose: the value is produced by C, and materialising an out-of-range
/// discriminant into a `#[repr(i32)]` enum would be undefined behaviour.
#[allow(non_camel_case_types)]
pub type mi_memory_change_kind_t = c_int;

/// Mirrors `MI_MEMORY_ALLOCATE`.
pub const MI_MEMORY_ALLOCATE: mi_memory_change_kind_t = 0;
/// Mirrors `MI_MEMORY_FREE`.
pub const MI_MEMORY_FREE: mi_memory_change_kind_t = 1;
/// Mirrors `MI_MEMORY_RESIZE`.
pub const MI_MEMORY_RESIZE: mi_memory_change_kind_t = 2;
/// Mirrors `MI_MEMORY_CHANGE_COUNT`: the number of callback slots, not a kind.
pub const MI_MEMORY_CHANGE_COUNT: usize = 3;

/// Mirrors `mi_memory_change_t` (include/mimalloc/memory-events.h) field-for-field.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
#[allow(non_camel_case_types)]
pub struct mi_memory_change_t {
    /// One of [`MI_MEMORY_ALLOCATE`] / [`MI_MEMORY_FREE`] / [`MI_MEMORY_RESIZE`].
    pub kind: mi_memory_change_kind_t,
    /// Tracked global live usable bytes after this operation.
    pub total_bytes: u64,
    /// Signed change in tracked live usable bytes caused by this operation.
    pub delta_bytes: i64,
    /// Caller-requested size for allocation and resize; zero for free.
    pub request_size: u64,
}

/// Mirrors `mi_memory_change_fun` (include/mimalloc/memory-events.h).
#[allow(non_camel_case_types)]
pub type mi_memory_change_fun =
    unsafe extern "C" fn(change: *const mi_memory_change_t, arg: *mut c_void);

/// Mirrors `mi_memory_callbacks_t` (include/mimalloc/memory-events.h): one handler and
/// one caller-owned `arg` per [`mi_memory_change_kind_t`], indexed by the kind.
#[repr(C)]
#[derive(Clone, Copy)]
#[allow(non_camel_case_types)]
pub struct mi_memory_callbacks_t {
    /// Handler per change kind; `None` leaves that kind unobserved.
    pub handlers: [Option<mi_memory_change_fun>; MI_MEMORY_CHANGE_COUNT],
    /// Caller-owned context per change kind, passed back to the matching handler.
    pub args: [*mut c_void; MI_MEMORY_CHANGE_COUNT],
}

/// Mirrors `mi_memory_snapshot_t` (include/mimalloc/memory-events.h) field-for-field.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
#[allow(non_camel_case_types)]
pub struct mi_memory_snapshot_t {
    /// `sizeof(mi_memory_snapshot_t)`; fill in before calling `mi_memory_snapshot`.
    pub size: usize,
    /// [`MI_MEMORY_SNAPSHOT_VERSION`]; fill in before calling `mi_memory_snapshot`.
    pub version: c_int,
    /// Tracked live usable bytes right now.
    pub live_bytes: u64,
    /// Cumulative usable bytes ever allocated.
    pub accum_bytes: u64,
    /// Tracked live allocation count right now.
    pub live_count: u64,
    /// Cumulative count of successful allocate events.
    pub accum_count: u64,
}

/// Mirrors `mi_memory_allocation_visit_fun` (include/mimalloc/memory-events.h).
#[allow(non_camel_case_types)]
pub type mi_memory_allocation_visit_fun =
    unsafe extern "C" fn(allocation: *mut c_void, usable_size: usize, arg: *mut c_void) -> bool;

// ---------------------------------------------------------------------------------------
// include/mimalloc.h -- mi_option_t
// ---------------------------------------------------------------------------------------

/// Mirrors `mi_option_t` (include/mimalloc.h).
///
/// **Positional, and silently wrong if it drifts.** This fork inserts thirteen
/// enumerators (`mi_option_prof*`, `mi_option_memory_events`, `mi_option_purge_zeroes`,
/// `mi_option_scavenger`, `mi_option_purge_holes*`) at indices 47..=59, immediately
/// before `_mi_option_last` -- so a mirror copied from upstream mimalloc would set a
/// *different* option than the caller named, with no diagnostic. Every value below is
/// checked against the C enum at test time by `tests/t19_layout.rs`, and the ordering is
/// checked against `include/mimalloc.h` by `ci/check_rust_surface.py`.
#[allow(non_camel_case_types)]
pub type mi_option_t = c_int;

macro_rules! mi_options {
    ($($(#[$attr:meta])* $name:ident = $value:expr;)*) => {
        $(
            $(#[$attr])*
            #[allow(non_upper_case_globals)]
            pub const $name: mi_option_t = $value;
        )*
        /// Every enumerator of `mi_option_t` up to and including `_mi_option_last`, in C
        /// declaration order, as `(name, value)` pairs. Used by `tests/t19_layout.rs` to
        /// check this mirror against the C enum, and by `ci/check_rust_surface.py` to
        /// check it against `include/mimalloc.h`.
        pub const MI_OPTIONS_IN_ORDER: &[(&str, mi_option_t)] = &[$((stringify!($name), $name)),*];
    };
}

mi_options! {
    /// Print error messages.
    mi_option_show_errors = 0;
    /// Print statistics on termination.
    mi_option_show_stats = 1;
    /// Print verbose messages.
    mi_option_verbose = 2;
    /// Deprecated; kept for numbering.
    mi_option_deprecated_eager_commit = 3;
    /// Eagerly commit arena memory.
    mi_option_arena_eager_commit = 4;
    /// Purge decommits rather than resets.
    mi_option_purge_decommits = 5;
    /// Allow large (2 MiB) OS pages.
    mi_option_allow_large_os_pages = 6;
    /// Reserve N huge OS pages at startup.
    mi_option_reserve_huge_os_pages = 7;
    /// Reserve huge OS pages at a specific NUMA node.
    mi_option_reserve_huge_os_pages_at = 8;
    /// Reserve N KiB of OS memory at startup.
    mi_option_reserve_os_memory = 9;
    /// Deprecated; kept for numbering.
    mi_option_deprecated_segment_cache = 10;
    /// Deprecated; kept for numbering.
    mi_option_deprecated_page_reset = 11;
    /// Deprecated; kept for numbering.
    mi_option_deprecated_abandoned_page_purge = 12;
    /// Deprecated; kept for numbering.
    mi_option_deprecated_segment_reset = 13;
    /// Deprecated; kept for numbering.
    mi_option_deprecated_eager_commit_delay = 14;
    /// Milliseconds to delay purging.
    mi_option_purge_delay = 15;
    /// Number of NUMA nodes to use.
    mi_option_use_numa_nodes = 16;
    /// Refuse to allocate from the OS.
    mi_option_disallow_os_alloc = 17;
    /// Tag passed to the OS allocator.
    mi_option_os_tag = 18;
    /// Maximum error messages printed.
    mi_option_max_errors = 19;
    /// Maximum warning messages printed.
    mi_option_max_warnings = 20;
    /// Deprecated; kept for numbering.
    mi_option_deprecated_max_segment_reclaim = 21;
    /// Release all memory on process exit.
    mi_option_destroy_on_exit = 22;
    /// Bytes to reserve per new arena.
    mi_option_arena_reserve = 23;
    /// Multiplier on the arena purge delay.
    mi_option_arena_purge_mult = 24;
    /// Deprecated; kept for numbering.
    mi_option_deprecated_purge_extend_delay = 25;
    /// Refuse to allocate from arenas.
    mi_option_disallow_arena_alloc = 26;
    /// Retry count on OS out-of-memory.
    mi_option_retry_on_oom = 27;
    /// Deprecated; kept for numbering.
    mi_option_deprecated_visit_abandoned = 28;
    /// Minimum size for guard-page allocation.
    mi_option_guarded_min = 29;
    /// Maximum size for guard-page allocation.
    mi_option_guarded_max = 30;
    /// Place guard pages precisely after the block.
    mi_option_guarded_precise = 31;
    /// Sample every N-th allocation for guard pages.
    mi_option_guarded_sample_rate = 32;
    /// Seed for guard-page sampling.
    mi_option_guarded_sample_seed = 33;
    /// Collect generically every N-th generic allocation.
    mi_option_generic_collect = 34;
    /// Reclaim abandoned pages on free.
    mi_option_page_reclaim_on_free = 35;
    /// Number of full pages to retain per bin.
    mi_option_page_full_retain = 36;
    /// Candidate pages searched per allocation.
    mi_option_page_max_candidates = 37;
    /// Maximum virtual address bits to assume.
    mi_option_max_vabits = 38;
    /// Commit the page map eagerly.
    mi_option_pagemap_commit = 39;
    /// Commit page memory on demand.
    mi_option_page_commit_on_demand = 40;
    /// Maximum pages reclaimed at once.
    mi_option_page_max_reclaim = 41;
    /// Maximum cross-thread pages reclaimed at once.
    mi_option_page_cross_thread_max_reclaim = 42;
    /// Allow transparent huge pages.
    mi_option_allow_thp = 43;
    /// Smallest range worth purging.
    mi_option_minimal_purge_size = 44;
    /// Largest object served from an arena.
    mi_option_arena_max_object_size = 45;
    /// Treat arenas as NUMA local.
    mi_option_arena_is_numa_local = 46;
    /// **Fork addition.** Enable the allocation sampling profiler at process start
    /// (`MIMALLOC_PROF`).
    mi_option_prof = 47;
    /// **Fork addition.** Average byte interval between profiler samples.
    mi_option_prof_sample_rate = 48;
    /// **Fork addition.** Maximum captured stack depth for the profiler.
    mi_option_prof_bt_max = 49;
    /// **Fork addition.** Keep cumulative profiler counters until `mi_prof_reset`.
    mi_option_prof_accum = 50;
    /// **Fork addition.** Profiler sampling PRNG seed; 0 = nondeterministic.
    mi_option_prof_seed = 51;
    /// **Fork addition.** Budget in bytes for profiler-internal arena memory.
    mi_option_prof_max_bytes = 52;
    /// **Fork addition.** Enable allocation-change accounting/callbacks
    /// (`MIMALLOC_MEMORY_EVENTS`).
    mi_option_memory_events = 53;
    /// **Fork addition, dead since #80.** The slot is kept so nothing renumbers; setting
    /// it parses but has no effect. Unrelated to `mi_option_purge_holes_eager_zero`.
    mi_option_purge_zeroes = 54;
    /// **Fork addition (Bun).** Run a background thread that purges scheduled arena memory.
    mi_option_scavenger = 55;
    /// **Fork addition (Bun).** Discard the memory of free blocks inside still-used pages
    /// on `mi_on_thread_idle`.
    mi_option_purge_holes = 56;
    /// **Fork addition (Bun).** Zero a range before discarding it, so a mis-scoped discard
    /// corrupts visibly.
    mi_option_purge_holes_eager_zero = 57;
    /// **Fork addition (Bun).** Minimum milliseconds between sweeps of one thread's heaps.
    mi_option_purge_holes_min_interval = 58;
    /// **Fork addition (Bun).** Every N-th sweep walks every page, ignoring the per-page
    /// skip check; 0 disables.
    mi_option_purge_holes_full_every = 59;
    /// Sentinel: one past the last real option.
    _mi_option_last = 60;
}

/// Deprecated upstream alias for [`mi_option_allow_large_os_pages`].
#[allow(non_upper_case_globals)]
pub const mi_option_large_os_pages: mi_option_t = mi_option_allow_large_os_pages;
/// Deprecated upstream alias for [`mi_option_arena_eager_commit`].
#[allow(non_upper_case_globals)]
pub const mi_option_eager_region_commit: mi_option_t = mi_option_arena_eager_commit;
/// Deprecated upstream alias for [`mi_option_purge_decommits`].
#[allow(non_upper_case_globals)]
pub const mi_option_reset_decommits: mi_option_t = mi_option_purge_decommits;
/// Deprecated upstream alias for [`mi_option_purge_delay`].
#[allow(non_upper_case_globals)]
pub const mi_option_reset_delay: mi_option_t = mi_option_purge_delay;
/// Deprecated upstream alias for [`mi_option_disallow_os_alloc`].
#[allow(non_upper_case_globals)]
pub const mi_option_limit_os_alloc: mi_option_t = mi_option_disallow_os_alloc;

/// One entry of the ABI layout table published by [`mi_rs_layout_table`].
#[repr(C)]
#[derive(Clone, Copy)]
pub struct MiRsLayoutEntry {
    /// NUL-terminated static key, e.g. `sizeof:mi_stats_t` or `option:mi_option_prof`.
    pub name: *const c_char,
    /// The value the C compiler computed for that key.
    pub value: usize,
}

unsafe extern "C" {
    pub fn mi_malloc(size: usize) -> *mut c_void;
    pub fn mi_zalloc(size: usize) -> *mut c_void;
    pub fn mi_calloc(count: usize, size: usize) -> *mut c_void;
    pub fn mi_realloc(p: *mut c_void, newsize: usize) -> *mut c_void;
    pub fn mi_free(p: *mut c_void);
    pub fn mi_malloc_aligned(size: usize, alignment: usize) -> *mut c_void;
    pub fn mi_zalloc_aligned(size: usize, alignment: usize) -> *mut c_void;
    pub fn mi_realloc_aligned(p: *mut c_void, newsize: usize, alignment: usize) -> *mut c_void;
    // Zeroing reallocation (issue #83). These grow a block AND zero the new tail, which
    // `GlobalAlloc` cannot express -- it has no `grow_zeroed` -- so a Rust caller would
    // otherwise grow and `memset` by hand, redoing work mimalloc has already done.
    //
    // What they zero is NOT [old_requested, new): mimalloc measures from the block's
    // old *usable* size, so the slack between requested and usable is left as-is. A
    // block requested at 64 bytes may be usable to 80, and growing to 70 is served in
    // place with nothing zeroed. See `prof::rezalloc` for the safe-wrapper docs.
    pub fn mi_rezalloc(p: *mut c_void, newsize: usize) -> *mut c_void;
    pub fn mi_recalloc(p: *mut c_void, newcount: usize, size: usize) -> *mut c_void;
    pub fn mi_rezalloc_aligned(p: *mut c_void, newsize: usize, alignment: usize) -> *mut c_void;
    pub fn mi_recalloc_aligned(
        p: *mut c_void,
        newcount: usize,
        size: usize,
        alignment: usize,
    ) -> *mut c_void;
    /// Grow in place only; returns NULL if the block cannot be extended without moving.
    pub fn mi_expand(p: *mut c_void, newsize: usize) -> *mut c_void;
    pub fn mi_usable_size(p: *const c_void) -> usize;
    pub fn mi_dhat_start() -> bool;
    pub fn mi_dhat_stop();
    pub fn mi_dhat_is_enabled() -> bool;
    pub fn mi_dhat_stats_get(stats: *mut mi_dhat_stats_t) -> bool;
    pub fn mi_dhat_dump(path: *const c_char) -> bool;
    pub fn mi_prof_start(sample_rate: usize) -> bool;
    pub fn mi_prof_start_seeded(sample_rate: usize, seed: u64) -> bool;
    pub fn mi_prof_start_ex(config: *const mi_prof_config_t) -> bool;
    pub fn mi_prof_stop();
    pub fn mi_prof_is_enabled() -> bool;
    pub fn mi_prof_dump(path: *const c_char) -> bool;
    pub fn mi_prof_dump_writer(write: Option<MiProfWriteFun>, arg: *mut c_void) -> bool;
    /// profile.proto (google/pprof) writer: same sample/period/mapping semantics as
    /// `mi_prof_dump`/`mi_prof_dump_writer` but encoded as an uncompressed, binary
    /// pprof Profile message instead of the legacy "heap profile:" text.
    pub fn mi_prof_dump_proto(path: *const c_char) -> bool;
    pub fn mi_prof_dump_proto_writer(write: Option<MiProfWriteFun>, arg: *mut c_void) -> bool;
    pub fn mi_prof_reset();
    pub fn mi_prof_debug_stats(records: *mut usize, bytes: *mut usize, unique_stacks: *mut usize);
    pub fn mi_prof_stats_get(stats: *mut mi_prof_stats_t) -> bool;
    pub fn mi_prof_visit(visitor: mi_prof_visit_fun, arg: *mut c_void) -> bool;
    pub fn mi_prof_snapshot_new() -> *mut mi_prof_snapshot_t;
    pub fn mi_prof_snapshot_visit(
        snap: *const mi_prof_snapshot_t,
        visitor: mi_prof_visit_fun,
        arg: *mut c_void,
    ) -> bool;
    pub fn mi_prof_snapshot_free(snap: *mut mi_prof_snapshot_t);
    /// Structured module (mapping) enumeration, e.g. to build pprof Mapping entries
    /// yourself. No profiler lock is taken: module lists are OS-owned, not part of
    /// the sampled-allocation table. `info` (and `info->path`) are valid only for
    /// the duration of the callback.
    pub fn mi_prof_modules_visit(visitor: mi_prof_module_visit_fun, arg: *mut c_void) -> bool;

    /// Mirrors `mi_unwrapped_malloc` (include/mimalloc/memory-events.h): backed
    /// directly by the raw OS layer, never by the hooked `mi_malloc` family.
    /// See that header's "Stable public unwrapped instrumentation allocation
    /// path" comment for the full contract.
    pub fn mi_unwrapped_malloc(size: usize, alignment: usize) -> *mut c_void;
    /// Mirrors `mi_unwrapped_free` (include/mimalloc/memory-events.h).
    pub fn mi_unwrapped_free(p: *mut c_void);
    /// Mirrors `mi_unwrapped_realloc` (include/mimalloc/memory-events.h).
    pub fn mi_unwrapped_realloc(p: *mut c_void, new_size: usize, alignment: usize) -> *mut c_void;

    /// Live per-heap -> per-page -> (optional) per-block JSON snapshot (issue #269, Bun
    /// parity P4). Backs Bun's shipped `bun:jsc` `heapStats({dump:true|"blocks"})`. Returns
    /// NULL on allocation failure; a non-NULL result is `mi_malloc`-family memory the
    /// caller must free with `mi_free` (see `prof::heap_dump_json` for the safe wrapper).
    pub fn mi_heap_dump_json(include_blocks: bool, hash_addresses: bool) -> *mut c_char;
    /// Mirrors `mi_heap_get_seq` (include/mimalloc-stats.h): the monotonic sequence
    /// number assigned to `heap` at creation, or 0 for a NULL heap.
    pub fn mi_heap_get_seq(heap: *mut mi_heap_t) -> usize;

    /// Mirrors `mi_on_thread_idle` (issue #272, Bun parity P7a).
    pub fn mi_on_thread_idle();
    /// Mirrors `mi_on_thread_idle_start`; `false` means nothing was handed off and
    /// `mi_on_thread_idle_end` must NOT be called.
    pub fn mi_on_thread_idle_start() -> bool;
    /// Mirrors `mi_on_thread_idle_end`.
    pub fn mi_on_thread_idle_end();
    /// Mirrors `mi_scavenger_stop` (issue #272).
    pub fn mi_scavenger_stop();
    /// Mirrors `mi_purge_holes_stats_get` (issue #272, Bun parity P7b).
    pub fn mi_purge_holes_stats_get(stats: *mut MiPurgeHolesStats);

    /// Mirrors `mi_purge_holes_report` (issue #272, Bun parity P7b): prints, per size
    /// class, the free bytes hole purging could NOT discard. Read-only; purges nothing.
    pub fn mi_purge_holes_report();

    // ---- include/mimalloc.h: options ----
    /// Mirrors `mi_option_is_enabled`.
    pub fn mi_option_is_enabled(option: mi_option_t) -> bool;
    /// Mirrors `mi_option_enable`.
    pub fn mi_option_enable(option: mi_option_t);
    /// Mirrors `mi_option_disable`.
    pub fn mi_option_disable(option: mi_option_t);
    /// Mirrors `mi_option_set_enabled`.
    pub fn mi_option_set_enabled(option: mi_option_t, enable: bool);
    /// Mirrors `mi_option_set_enabled_default`.
    pub fn mi_option_set_enabled_default(option: mi_option_t, enable: bool);
    /// Mirrors `mi_option_get`.
    pub fn mi_option_get(option: mi_option_t) -> core::ffi::c_long;
    /// Mirrors `mi_option_get_clamp`.
    pub fn mi_option_get_clamp(
        option: mi_option_t,
        min: core::ffi::c_long,
        max: core::ffi::c_long,
    ) -> core::ffi::c_long;
    /// Mirrors `mi_option_get_size`.
    pub fn mi_option_get_size(option: mi_option_t) -> usize;
    /// Mirrors `mi_option_set`.
    pub fn mi_option_set(option: mi_option_t, value: core::ffi::c_long);
    /// Mirrors `mi_option_set_default`.
    pub fn mi_option_set_default(option: mi_option_t, value: core::ffi::c_long);
    /// Mirrors `mi_options_print_out`: prints every option's current value.
    pub fn mi_options_print_out(out: Option<mi_output_fun>, arg: *mut c_void);

    // ---- include/mimalloc-stats.h: exact statistics ----
    /// Mirrors `mi_stats_get`: aggregated stats for the current subprocess and its heaps.
    pub fn mi_stats_get(stats: *mut mi_stats_t) -> bool;
    /// Mirrors `mi_stats_get_json`. With `buf == NULL` the result is `mi_malloc`-family
    /// memory the caller must release with [`mi_free`].
    pub fn mi_stats_get_json(buf_size: usize, buf: *mut c_char) -> *mut c_char;
    /// Mirrors `mi_stats_as_json`: render an already-captured [`mi_stats_t`].
    pub fn mi_stats_as_json(
        stats: *mut mi_stats_t,
        buf_size: usize,
        buf: *mut c_char,
    ) -> *mut c_char;
    /// Mirrors `mi_stats_print_out`.
    pub fn mi_stats_print_out(out: Option<mi_output_fun>, arg: *mut c_void);
    /// Mirrors `mi_stats_get_bin_size`: the block size served by size bin `bin`.
    pub fn mi_stats_get_bin_size(bin: usize) -> usize;
    /// Mirrors `mi_heap_stats_get`.
    pub fn mi_heap_stats_get(heap: *mut mi_heap_t, stats: *mut mi_stats_t) -> bool;
    /// Mirrors `mi_heap_stats_get_json`.
    pub fn mi_heap_stats_get_json(
        heap: *mut mi_heap_t,
        buf_size: usize,
        buf: *mut c_char,
    ) -> *mut c_char;
    /// Mirrors `mi_heap_stats_print_out`.
    pub fn mi_heap_stats_print_out(
        heap: *mut mi_heap_t,
        out: Option<mi_output_fun>,
        arg: *mut c_void,
    );
    /// Mirrors `mi_heap_stats_merge_to_subproc`: fold a heap's stats into its subprocess
    /// and clear the heap's own.
    pub fn mi_heap_stats_merge_to_subproc(heap: *mut mi_heap_t);
    /// Mirrors `mi_subproc_stats_get`.
    pub fn mi_subproc_stats_get(subproc_id: mi_subproc_id_t, stats: *mut mi_stats_t) -> bool;
    /// Mirrors `mi_subproc_stats_get_exclusive`: the subprocess's own stats, without
    /// aggregating its heaps.
    pub fn mi_subproc_stats_get_exclusive(
        subproc_id: mi_subproc_id_t,
        stats: *mut mi_stats_t,
    ) -> bool;
    /// Mirrors `mi_subproc_stats_get_json`.
    pub fn mi_subproc_stats_get_json(
        subproc_id: mi_subproc_id_t,
        buf_size: usize,
        buf: *mut c_char,
    ) -> *mut c_char;
    /// Mirrors `mi_subproc_stats_print_out`.
    pub fn mi_subproc_stats_print_out(
        subproc_id: mi_subproc_id_t,
        out: Option<mi_output_fun>,
        arg: *mut c_void,
    );
    /// Mirrors `mi_subproc_heap_stats_print_out`: the subprocess and each of its heaps,
    /// printed separately.
    pub fn mi_subproc_heap_stats_print_out(
        subproc_id: mi_subproc_id_t,
        out: Option<mi_output_fun>,
        arg: *mut c_void,
    );
    /// Mirrors `mi_subproc_main`: the process-wide default subprocess.
    pub fn mi_subproc_main() -> mi_subproc_id_t;
    /// Mirrors `mi_subproc_current`: the subprocess this thread belongs to.
    pub fn mi_subproc_current() -> mi_subproc_id_t;

    // ---- include/mimalloc/memory-events.h ----
    /// Mirrors `mi_memory_tracking_set_enabled`; returns the previous state. An explicit
    /// call is always authoritative over the `MIMALLOC_MEMORY_EVENTS` environment read.
    pub fn mi_memory_tracking_set_enabled(enabled: bool) -> bool;
    /// Mirrors `mi_memory_tracking_is_enabled`.
    pub fn mi_memory_tracking_is_enabled() -> bool;
    /// Mirrors `mi_memory_set_callbacks`. `callbacks == NULL` clears the table. The `arg`
    /// pointers are caller-owned and must stay valid until replaced or cleared.
    pub fn mi_memory_set_callbacks(callbacks: *const mi_memory_callbacks_t) -> bool;
    /// Mirrors `mi_memory_snapshot`; fill `size`/`version` in before calling.
    pub fn mi_memory_snapshot(out: *mut mi_memory_snapshot_t) -> bool;
    /// Mirrors `mi_memory_visit_live_allocations`. Best effort, not a consistent global
    /// snapshot; the visitor must not allocate, free, or reenter mimalloc.
    pub fn mi_memory_visit_live_allocations(
        visitor: mi_memory_allocation_visit_fun,
        arg: *mut c_void,
    ) -> bool;

    /// Publishes what the C compiler laid out for every mirrored type, constant and
    /// option in this module. Defined by `layout_probe.c` (a rust/-side verification
    /// artifact, not part of the C library) and consumed by `tests/t19_layout.rs`.
    pub fn mi_rs_layout_table(count: *mut usize) -> *const MiRsLayoutEntry;
}
