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

/// Turn on sampled heap profiling at the default sample rate.
///
/// Convenience entry point for wiring profiling to a command-line flag:
///
/// ```no_run
/// # let args_profile_heap = true;
/// if args_profile_heap {
///     mimalloc_pprof::enable_heap_profiling();
/// }
/// ```
///
/// Uses the built-in default rate (one sample per ~512 KiB allocated;
/// `MIMALLOC_PROF_SAMPLE_RATE` still overrides it). Call [`prof::start`]
/// instead to pick a rate programmatically. Allocations made before this
/// call — including process-startup and static initialization — are not
/// tracked; profiles reflect steady-state behavior from this point on,
/// which is the usual intent for an opt-in CLI switch. To capture startup
/// as well, set `MIMALLOC_PROF=1` in the environment instead.
///
/// Returns `false` if profiling was already enabled (the earlier session,
/// and its sample rate, stay active).
pub fn enable_heap_profiling() -> bool {
    prof::start(0)
}

/// Safe controls for mimalloc's sampled heap profiler.
pub mod prof {
    use core::ffi::{c_char, c_void};
    use std::ffi::CString;
    use std::io;
    use std::panic::{catch_unwind, AssertUnwindSafe};
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

    /// Snapshot of `mi_prof_stats_get`'s counters, translated from the raw
    /// sys struct into plain Rust types.
    #[derive(Debug, Clone, Default)]
    pub struct ProfStats {
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

    /// Read the profiler's current counters via `mi_prof_stats_get`.
    ///
    /// Returns `ProfStats::default()` (all zero/false) if the call fails,
    /// e.g. because the sys struct's `size`/`version` header does not match
    /// what the linked mimalloc build expects.
    pub fn stats() -> ProfStats {
        let mut raw: sys::mi_prof_stats_t = unsafe { core::mem::zeroed() };
        raw.size = core::mem::size_of::<sys::mi_prof_stats_t>();
        raw.version = sys::MI_PROF_STAT_VERSION;
        if unsafe { sys::mi_prof_stats_get(&mut raw) } {
            ProfStats {
                enabled: raw.enabled,
                accum: raw.accum,
                sample_rate: raw.sample_rate,
                live_samples: raw.live_samples,
                live_bytes: raw.live_bytes,
                accum_samples: raw.accum_samples,
                accum_bytes: raw.accum_bytes,
                unique_stacks: raw.unique_stacks,
                arena_committed: raw.arena_committed,
                stack_table_overflows: raw.stack_table_overflows,
            }
        } else {
            ProfStats::default()
        }
    }

    /// One sampled call stack, copied out of the profiler by [`samples`].
    #[derive(Debug, Clone)]
    pub struct Sample {
        pub stack: Vec<usize>,
        pub live_objects: usize,
        pub live_bytes: usize,
        pub accum_objects: usize,
        pub accum_bytes: usize,
    }

    impl Sample {
        /// Estimate the un-sampled byte volume behind this sample.
        ///
        /// Mirrors pprof's legacy heap-sample scaling formula
        /// (`scaleHeapSample` in pprof's `profile/legacy_profile.go`),
        /// which corrects for the bias a Poisson sampling process with mean
        /// interval `sample_rate` introduces toward larger allocations.
        pub fn estimated_bytes(&self, sample_rate: usize) -> u64 {
            if self.live_objects == 0 || self.live_bytes == 0 {
                return 0;
            }
            if sample_rate <= 1 {
                return self.live_bytes as u64;
            }
            let avg = self.live_bytes as f64 / self.live_objects as f64;
            let scale = 1.0 / (1.0 - (-avg / sample_rate as f64).exp());
            (self.live_bytes as f64 * scale) as u64
        }
    }

    /// Frees the snapshot handle on drop, including on unwind, so a panic
    /// partway through collection never leaks profiler-arena memory.
    struct SnapshotGuard(*mut sys::mi_prof_snapshot_t);

    impl Drop for SnapshotGuard {
        fn drop(&mut self) {
            unsafe { sys::mi_prof_snapshot_free(self.0) }
        }
    }

    unsafe extern "C" fn collect_visitor(
        info: *const sys::mi_prof_sample_info_t,
        arg: *mut c_void,
    ) -> bool {
        let result = catch_unwind(AssertUnwindSafe(|| unsafe {
            let out = &mut *(arg as *mut Vec<Sample>);
            let info = &*info;
            let stack = (0..info.depth)
                .map(|i| *info.stack.add(i) as usize)
                .collect();
            out.push(Sample {
                stack,
                live_objects: info.live_objects,
                live_bytes: info.live_bytes,
                accum_objects: info.accum_objects,
                accum_bytes: info.accum_bytes,
            });
        }));
        result.is_ok()
    }

    /// Collect a point-in-time copy of every live sampled stack.
    ///
    /// This snapshots under the profiler lock via `mi_prof_snapshot_new`,
    /// then walks and frees the snapshot outside that lock. Using
    /// `mi_prof_visit` directly here would run the (allocating) collection
    /// below from inside the visitor while the profiler lock is held,
    /// risking reentrant profiler-hook allocation and deadlock — the
    /// reentrancy hazard the snapshot API exists to avoid (issue #2,
    /// decisions 11-13).
    pub fn samples() -> Vec<Sample> {
        let snap = unsafe { sys::mi_prof_snapshot_new() };
        if snap.is_null() {
            return Vec::new();
        }
        let guard = SnapshotGuard(snap);
        let mut out: Vec<Sample> = Vec::new();
        unsafe {
            sys::mi_prof_snapshot_visit(
                guard.0,
                collect_visitor,
                (&mut out as *mut Vec<Sample>).cast(),
            );
        }
        out
    }
}
