//! Raw bindings to the in-tree mimalloc static amalgamation.

use core::ffi::c_char;
use core::ffi::c_void;

pub type MiProfWriteFun = unsafe extern "C" fn(*mut c_void, *const c_char, usize);

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
}
