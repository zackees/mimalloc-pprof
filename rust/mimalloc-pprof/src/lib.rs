//! Rust global allocator support for the in-tree mimalloc build.
//!
//! ```no_run
//! use mimalloc_pprof::{prof, MiMalloc};
//! #[global_allocator] static ALLOCATOR: MiMalloc = MiMalloc;
//! # fn main() -> std::io::Result<()> {
//! prof::start(512 * 1024);
//! prof::dump_file(std::path::Path::new("heap.prof"))?;
//! # Ok(()) }
//! ```
//!
//! See the README's Rust integration guide for frame-pointer and line-table
//! build flags. Open the resulting profile with `pprof -http=: app.exe heap.prof`.

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

/// Safe controls for mimalloc's sampled heap profiler.
pub mod prof {
    use core::ffi::{c_char, c_void};
    use std::ffi::CString;
    use std::io;
    use std::path::Path;

    use crate::sys;

    pub fn start(sample_rate: usize) -> bool {
        unsafe { sys::mi_prof_start(sample_rate) }
    }
    #[doc(hidden)]
    pub fn start_seeded(sample_rate: usize, seed: u64) -> bool {
        unsafe { sys::mi_prof_start_seeded(sample_rate, seed) }
    }
    pub fn stop() {
        unsafe { sys::mi_prof_stop() }
    }
    pub fn is_enabled() -> bool {
        unsafe { sys::mi_prof_is_enabled() }
    }
    pub fn reset() {
        unsafe { sys::mi_prof_reset() }
    }

    pub fn dump_file(path: &Path) -> io::Result<()> {
        let path = path.to_str().ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidInput, "profile path is not UTF-8")
        })?;
        let path = CString::new(path).map_err(|_| {
            io::Error::new(io::ErrorKind::InvalidInput, "profile path contains NUL")
        })?;
        if unsafe { sys::mi_prof_dump(path.as_ptr()) } {
            Ok(())
        } else {
            Err(io::Error::last_os_error())
        }
    }

    unsafe extern "C" fn write_cb(arg: *mut c_void, buf: *const c_char, len: usize) {
        let out = &mut *(arg as *mut Vec<u8>);
        out.extend_from_slice(core::slice::from_raw_parts(buf.cast::<u8>(), len));
    }

    /// Serialize the current heap profile without holding the profiler lock.
    pub fn dump_to_vec() -> Vec<u8> {
        let mut out = Vec::new();
        let ok =
            unsafe { sys::mi_prof_dump_writer(Some(write_cb), (&mut out as *mut Vec<u8>).cast()) };
        if ok {
            out
        } else {
            Vec::new()
        }
    }
}
