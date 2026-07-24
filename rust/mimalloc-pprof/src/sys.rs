//! Raw bindings to the in-tree mimalloc static amalgamation.

use core::ffi::c_char;
use core::ffi::c_int;
use core::ffi::c_void;

pub type MiProfWriteFun = unsafe extern "C" fn(*mut c_void, *const c_char, usize);

/// Mirrors `MI_PROF_STAT_VERSION` in `include/mimalloc/profile.h`.
pub const MI_PROF_STAT_VERSION: c_int = 2;

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

unsafe extern "C" {
    pub fn mi_malloc(size: usize) -> *mut c_void;
    pub fn mi_zalloc(size: usize) -> *mut c_void;
    pub fn mi_calloc(count: usize, size: usize) -> *mut c_void;
    pub fn mi_realloc(p: *mut c_void, newsize: usize) -> *mut c_void;
    pub fn mi_free(p: *mut c_void);
    pub fn mi_malloc_aligned(size: usize, alignment: usize) -> *mut c_void;
    pub fn mi_zalloc_aligned(size: usize, alignment: usize) -> *mut c_void;
    pub fn mi_realloc_aligned(p: *mut c_void, newsize: usize, alignment: usize) -> *mut c_void;
    pub fn mi_usable_size(p: *const c_void) -> usize;
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
}
