//! Raw bindings to the in-tree mimalloc static amalgamation.

use core::ffi::c_void;

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
}
