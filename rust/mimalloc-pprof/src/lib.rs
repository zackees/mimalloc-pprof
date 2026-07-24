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
use std::ffi::CString;
use std::path::PathBuf;

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

/// Allocate `size` bytes from mimalloc's raw-OS-layer "unwrapped" path.
///
/// Thin wrapper around `mi_unwrapped_malloc` (include/mimalloc/memory-events.h):
/// backed directly by `_mi_os_alloc_aligned`, never by the hooked `mi_malloc`
/// family. Page granular, so this is not meant for hot-path/small allocations
/// — it exists for low-level instrumentation and recursion avoidance (e.g.
/// scratch storage for a memory-change callback that must not recursively
/// enter mimalloc). Excluded from normal mimalloc allocation stats and from
/// the memory-change accounting.
///
/// Returns a null pointer on failure (including invalid `alignment`; see
/// `# Safety` below).
///
/// # Safety
///
/// - `alignment` must be `0` (treated as `align_of::<*const ()>()`, i.e.
///   pointer size) or a power of two. A non-power-of-two, non-zero alignment
///   is a validated input on the C side: `mi_unwrapped_malloc` returns a null
///   pointer rather than invoking undefined behavior, but callers should not
///   rely on that as anything other than a defined-failure contract — treat
///   the alignment argument as a precondition to get right, not a value to
///   probe.
/// - The returned pointer, if non-null, must be passed only to
///   [`unwrapped_free`] or [`unwrapped_realloc`] — never to `mi_free`, this
///   crate's [`MiMalloc`] allocator, or Rust's global allocator, and vice
///   versa (a pointer from `mi_malloc`/the Rust global allocator must never
///   be passed to [`unwrapped_free`]/[`unwrapped_realloc`]). Mixing these
///   families corrupts allocator-internal bookkeeping.
/// - The memory is uninitialized; reading it before writing is undefined
///   behavior, as with any raw allocation.
pub unsafe fn unwrapped_malloc(size: usize, alignment: usize) -> *mut u8 {
    unsafe { sys::mi_unwrapped_malloc(size, alignment).cast() }
}

/// Free a pointer returned by [`unwrapped_malloc`] or [`unwrapped_realloc`].
///
/// Thin wrapper around `mi_unwrapped_free` (include/mimalloc/memory-events.h).
///
/// # Safety
///
/// - `p` must be either a null pointer (a documented, safe no-op on the C
///   side) or a pointer previously returned by [`unwrapped_malloc`] or
///   [`unwrapped_realloc`] that has not already been freed.
/// - `p` must never have come from `mi_malloc`, this crate's [`MiMalloc`]
///   allocator, or Rust's global allocator — passing such a pointer here is
///   undefined behavior (the "unwrapped" and normal allocation families use
///   incompatible header layouts and are validated by a magic-number check
///   that a foreign pointer will not satisfy).
pub unsafe fn unwrapped_free(p: *mut u8) {
    unsafe { sys::mi_unwrapped_free(p.cast()) }
}

/// Resize a pointer returned by [`unwrapped_malloc`] or [`unwrapped_realloc`].
///
/// Thin wrapper around `mi_unwrapped_realloc` (include/mimalloc/memory-events.h).
/// If `p` is null, this behaves like [`unwrapped_malloc`]. If `new_size` is
/// `0`, this frees `p` (like [`unwrapped_free`]) and returns a null pointer.
/// Otherwise the existing contents are copied into a freshly allocated
/// unwrapped block (up to `min(old payload size, new_size)` bytes) and `p` is
/// freed; `p` must not be used again after this call, whether or not it
/// returns null.
///
/// Returns a null pointer on failure (including invalid `alignment`; see
/// [`unwrapped_malloc`]'s `# Safety` section), in which case `p` is left
/// valid and unfreed.
///
/// # Safety
///
/// - `p` must be either a null pointer or a pointer previously returned by
///   [`unwrapped_malloc`] or [`unwrapped_realloc`] that has not already been
///   freed, per the same family-isolation rule as [`unwrapped_free`].
/// - `alignment` has the same power-of-two-or-zero contract as
///   [`unwrapped_malloc`].
/// - After this call, `p` must not be read, written, or freed again — treat
///   it as consumed regardless of whether the return value is null.
pub unsafe fn unwrapped_realloc(p: *mut u8, new_size: usize, alignment: usize) -> *mut u8 {
    unsafe { sys::mi_unwrapped_realloc(p.cast(), new_size, alignment).cast() }
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

/// How [`ProfConfig`] fields interact with the profiler's environment
/// variables and `mi_option_*` settings.
///
/// Mirrors `mi_prof_config_mode_t` (include/mimalloc/profile.h); see that
/// header for the full FALLBACK/OVERRIDE semantics, including the caveat
/// that in `Override` mode `accum == false`, `dump_format == Text`, and
/// `max_profiler_bytes == None` cannot be distinguished from "unset" and so
/// always fall back to env-then-default rather than forcing the off/default
/// value.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub enum ProfConfigMode {
    /// Struct fields are used only where the corresponding env var / option is absent.
    #[default]
    Fallback,
    /// Non-default struct fields win over env vars / options (see the caveat above).
    Override,
}

/// Output format for [`ProfConfig::dump_at_exit`].
///
/// Mirrors `MI_PROF_FORMAT_TEXT` / `MI_PROF_FORMAT_PROTO` (include/mimalloc/profile.h).
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub enum DumpFormat {
    /// Legacy "heap profile:" text format (see [`prof::dump_to_vec`]).
    #[default]
    Text,
    /// Binary pprof `profile.proto` format (see [`prof::dump_proto_to_vec`]).
    Proto,
}

/// Ergonomic, Rust-facing sibling of `mi_prof_config_t`
/// (include/mimalloc/profile.h) for [`enable_heap_profiling_with`].
///
/// Fields mirror the C struct one-for-one, but trade its 0/NULL-means-unset
/// raw-integer conventions for `Option<T>` and enums where that reads
/// better. `#[non_exhaustive]` + `Default` keeps future fields additive:
/// build from `Default::default()` and set the fields you need, e.g.
///
/// ```
/// use mimalloc_pprof::ProfConfig;
/// let mut config = ProfConfig::default();
/// config.sample_interval = Some(4096);
/// ```
///
/// (Within this crate, struct-update syntax like
/// `ProfConfig { sample_interval: Some(4096), ..Default::default() }` also
/// works; `#[non_exhaustive]` only blocks struct-literal construction from
/// *other* crates, so new fields stay non-breaking for them.)
#[non_exhaustive]
#[derive(Debug, Clone, Default)]
pub struct ProfConfig {
    /// See [`ProfConfigMode`].
    pub mode: ProfConfigMode,
    /// Average bytes between samples. `None` = env/default (512 KiB).
    pub sample_interval: Option<usize>,
    /// Budget (bytes) for profiler-internal persistent sampling state
    /// (sample records, the stack intern table, interned stack entries).
    /// `None` = unbudgeted (cap-bounded only).
    pub max_profiler_bytes: Option<usize>,
    /// `None` = nondeterministic.
    pub seed: Option<u64>,
    pub accum: bool,
    /// `None` = default (32); compile cap 128.
    pub max_stack_depth: Option<usize>,
    /// Path to dump the profile to at process exit. `None` = no exit dump.
    pub dump_at_exit: Option<PathBuf>,
    /// Format used for the exit dump. Ignored if `dump_at_exit` is `None`.
    pub dump_format: DumpFormat,
}

/// Turn on sampled heap profiling using a struct-based configuration.
///
/// Sibling of [`enable_heap_profiling`] for callers that need more than a
/// single sample rate -- e.g. seeding the sampler, capping profiler-arena
/// memory, or registering an exit-time dump path/format. See [`ProfConfig`]
/// and, for the full FALLBACK/OVERRIDE semantics, `mi_prof_config_mode_t` in
/// `include/mimalloc/profile.h`.
///
/// Returns `false` if profiling was already enabled (the earlier session
/// stays active), or if `config.dump_at_exit` is set but is not
/// representable as a NUL-free C string (non-UTF-8 or an embedded NUL byte)
/// -- in that case `mi_prof_start_ex` is never called.
pub fn enable_heap_profiling_with(config: &ProfConfig) -> bool {
    // `dump_at_exit_c` must outlive the `mi_prof_start_ex` call below since
    // `raw.dump_at_exit` borrows its bytes; it does, as both live to the end
    // of this function.
    let dump_at_exit_c: Option<CString> = match &config.dump_at_exit {
        Some(path) => match path.to_str().and_then(|s| CString::new(s).ok()) {
            Some(c) => Some(c),
            None => return false,
        },
        None => None,
    };

    let mut raw: sys::mi_prof_config_t = unsafe { core::mem::zeroed() };
    raw.size = core::mem::size_of::<sys::mi_prof_config_t>();
    raw.version = sys::MI_PROF_CONFIG_VERSION;
    raw.mode = match config.mode {
        ProfConfigMode::Fallback => sys::MI_PROF_CONFIG_FALLBACK,
        ProfConfigMode::Override => sys::MI_PROF_CONFIG_OVERRIDE,
    };
    raw.sample_interval = config.sample_interval.unwrap_or(0);
    raw.max_profiler_bytes = config.max_profiler_bytes.unwrap_or(0);
    raw.seed = config.seed.unwrap_or(0);
    raw.accum = config.accum;
    raw.max_stack_depth = config.max_stack_depth.unwrap_or(0);
    raw.dump_at_exit = dump_at_exit_c
        .as_ref()
        .map_or(core::ptr::null(), |c| c.as_ptr());
    raw.dump_format = match config.dump_format {
        DumpFormat::Text => sys::MI_PROF_FORMAT_TEXT,
        DumpFormat::Proto => sys::MI_PROF_FORMAT_PROTO,
    };

    unsafe { sys::mi_prof_start_ex(&raw) }
}

/// Safe controls for mimalloc's sampled heap profiler.
pub mod prof {
    use core::ffi::{c_char, c_void};
    use std::ffi::{CStr, CString};
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

    /// Serialize the current heap profile as a binary pprof `profile.proto`
    /// `Profile` message (see [google/pprof's `profile.proto`][proto]),
    /// without holding the profiler lock.
    ///
    /// Sample values are pre-scaled the same way Go's `runtime/pprof` scales
    /// legacy heap samples (the `protomem.go` convention: `alloc_objects`,
    /// `alloc_space`, `inuse_objects`, `inuse_space`, each already corrected
    /// for Poisson sampling bias rather than left for a downstream tool to
    /// rescale). The `Mapping` table is included, so external symbolizers
    /// need only the binary — no text parsing of a "heap profile:" header or
    /// a `MAPPED_LIBRARIES:` section. This is the compact, machine-oriented
    /// counterpart to [`dump_to_vec`]'s text format, intended for API and
    /// transport use (issue #23) where a `pprof`-compatible tool consumes
    /// the bytes directly.
    ///
    /// [proto]: https://github.com/google/pprof/blob/main/proto/profile.proto
    pub fn dump_proto_to_vec() -> Vec<u8> {
        let mut out = Vec::new();
        let ok = unsafe {
            sys::mi_prof_dump_proto_writer(Some(write_cb), (&mut out as *mut Vec<u8>).cast())
        };
        if ok {
            out
        } else {
            Vec::new()
        }
    }

    /// Write the current heap profile to `path` in `profile.proto` format.
    ///
    /// See [`dump_proto_to_vec`] for the format details.
    pub fn dump_proto_file(path: &Path) -> io::Result<()> {
        let path = path.to_str().ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidInput, "profile path is not UTF-8")
        })?;
        let path = CString::new(path).map_err(|_| {
            io::Error::new(io::ErrorKind::InvalidInput, "profile path contains NUL")
        })?;
        if unsafe { sys::mi_prof_dump_proto(path.as_ptr()) } {
            Ok(())
        } else {
            Err(io::Error::last_os_error())
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
        /// Count of ALL dropped samples (record-alloc failure, stack-intern
        /// failure, including the stack-table cap); a superset of
        /// `stack_table_overflows`, so `dropped_samples >=
        /// stack_table_overflows` always.
        pub dropped_samples: usize,
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
                dropped_samples: raw.dropped_samples,
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

    /// One loaded module (shared library or the main executable), copied out
    /// of the OS module list by [`modules`].
    #[derive(Debug, Clone)]
    pub struct ModuleInfo {
        pub path: String,
        pub base: usize,
        pub size: usize,
    }

    unsafe extern "C" fn modules_visitor(
        info: *const sys::mi_prof_module_info_t,
        arg: *mut c_void,
    ) -> bool {
        let result = catch_unwind(AssertUnwindSafe(|| unsafe {
            let out = &mut *(arg as *mut Vec<ModuleInfo>);
            let info = &*info;
            // `info.path` is only valid for the duration of this callback (it
            // points into OS-owned module-list storage), so it must be copied
            // into an owned `String` right here rather than stashed for later.
            let path = CStr::from_ptr(info.path).to_string_lossy().into_owned();
            out.push(ModuleInfo {
                path,
                base: info.base,
                size: info.size,
            });
        }));
        result.is_ok()
    }

    /// Enumerate the process's loaded modules (shared libraries and the main
    /// executable), e.g. to build pprof `Mapping` entries yourself.
    ///
    /// Unlike [`samples`]'s `collect_visitor`, this callback is free to
    /// allocate: `mi_prof_modules_visit` never takes the profiler lock (the
    /// module list is OS-owned, not part of the sampled-allocation table), so
    /// there is no reentrant-allocation-under-the-lock hazard here.
    pub fn modules() -> Vec<ModuleInfo> {
        let mut out: Vec<ModuleInfo> = Vec::new();
        unsafe {
            sys::mi_prof_modules_visit(
                modules_visitor,
                (&mut out as *mut Vec<ModuleInfo>).cast(),
            );
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    // The profiler is process-global state, and unit tests within this
    // binary may run concurrently by default, so serialize everything that
    // starts/stops it. `unwrap_or_else` rides through a poisoned lock rather
    // than cascading a single panicking test into every other one.
    static PROF_TEST_LOCK: Mutex<()> = Mutex::new(());

    fn reset_profiler() {
        if prof::is_enabled() {
            prof::stop();
        }
    }

    #[test]
    fn enable_heap_profiling_with_default_config_starts_profiler() {
        let _guard = PROF_TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        reset_profiler();

        let config = ProfConfig::default();
        assert!(enable_heap_profiling_with(&config));
        assert!(prof::is_enabled());

        prof::stop();
    }

    #[test]
    fn enable_heap_profiling_with_override_mode_sets_sample_interval() {
        let _guard = PROF_TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        reset_profiler();

        let config = ProfConfig {
            mode: ProfConfigMode::Override,
            sample_interval: Some(4096),
            ..Default::default()
        };
        assert!(enable_heap_profiling_with(&config));
        assert!(prof::is_enabled());
        assert_eq!(prof::stats().sample_rate, 4096);

        prof::stop();
    }

    #[test]
    fn unwrapped_malloc_write_realloc_grow_verify_free() {
        unsafe {
            let size = 64usize;
            let p = unwrapped_malloc(size, 0);
            assert!(!p.is_null());

            for i in 0..size {
                *p.add(i) = (i % 256) as u8;
            }

            let new_size = 256usize;
            let p2 = unwrapped_realloc(p, new_size, 0);
            assert!(!p2.is_null());

            for i in 0..size {
                assert_eq!(*p2.add(i), (i % 256) as u8);
            }

            unwrapped_free(p2);
        }
    }

    #[test]
    fn unwrapped_free_null_is_noop() {
        unsafe {
            unwrapped_free(core::ptr::null_mut());
        }
    }

    #[test]
    fn unwrapped_malloc_rejects_non_power_of_two_alignment() {
        unsafe {
            let p = unwrapped_malloc(16, 3);
            assert!(p.is_null());
        }
    }

    #[test]
    fn unwrapped_realloc_with_null_ptr_behaves_like_malloc() {
        unsafe {
            let p = unwrapped_realloc(core::ptr::null_mut(), 32, 0);
            assert!(!p.is_null());
            unwrapped_free(p);
        }
    }

    #[test]
    fn unwrapped_realloc_with_zero_size_frees_and_returns_null() {
        unsafe {
            let p = unwrapped_malloc(32, 0);
            assert!(!p.is_null());
            let p2 = unwrapped_realloc(p, 0, 0);
            assert!(p2.is_null());
        }
    }
}
