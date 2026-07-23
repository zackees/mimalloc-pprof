//! Rust global allocator support for the in-tree mimalloc build.

use core::alloc::{GlobalAlloc, Layout};
use core::ffi::c_void;

pub use libmimalloc_pprof_sys as sys;

/// A `#[global_allocator]` implementation backed by mimalloc.
pub struct MiMalloc;

unsafe impl GlobalAlloc for MiMalloc {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        sys::mi_malloc_aligned(layout.size(), layout.align()).cast()
    }

    unsafe fn dealloc(&self, ptr: *mut u8, _layout: Layout) {
        sys::mi_free(ptr.cast::<c_void>());
    }

    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        sys::mi_realloc_aligned(ptr.cast::<c_void>(), new_size, layout.align()).cast()
    }

    unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
        sys::mi_zalloc_aligned(layout.size(), layout.align()).cast()
    }
}
