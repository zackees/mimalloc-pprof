//! Raw bindings to the in-tree mimalloc static amalgamation.

use core::ffi::c_char;
use core::ffi::c_int;
use core::ffi::c_void;

pub type MiProfWriteFun = unsafe extern "C" fn(*mut c_void, *const c_char, usize);

/// Mirrors `MI_PROF_STAT_VERSION` in `include/mimalloc/profile.h`.
pub const MI_PROF_STAT_VERSION: c_int = 1;

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
    pub fn mi_prof_stop();
    pub fn mi_prof_is_enabled() -> bool;
    pub fn mi_prof_dump(path: *const c_char) -> bool;
    pub fn mi_prof_dump_writer(write: Option<MiProfWriteFun>, arg: *mut c_void) -> bool;
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
}
