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
//! Profiling is enabled by default. To build the allocator without profiler
//! hooks, depend on this crate with `default-features = false`; in that mode the
//! profiling API remains available but cannot start a profiler.
//!
//! See the README's Rust integration guide for frame-pointer and line-table
//! build flags. Open the resulting profile with `pprof -http=: app.exe heap.prof`.

use core::alloc::{GlobalAlloc, Layout};
use core::ffi::c_void;
use std::ffi::CString;
use std::path::PathBuf;

pub mod sys;

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

/// Grow or shrink an allocation, zeroing any newly-exposed tail.
///
/// Thin wrapper around `mi_rezalloc`. This is the operation Rust's [`GlobalAlloc`]
/// cannot express — that trait has no `grow_zeroed` — so without it a caller has to
/// grow and then `memset` by hand, repeating work the allocator has already done, and
/// (with zero-tracking) work it may be able to skip entirely.
///
/// # What is actually zeroed
///
/// **Not** `[old_requested_size, new_size)`. mimalloc measures from the block's old
/// *usable* size, so the slack between what you asked for and what the block actually
/// holds is left untouched:
///
/// ```text
/// requested 64  ->  usable 80  ->  rezalloc to 70
/// bytes [64,70) are NOT zeroed: the grow was served in place, within the old block
/// ```
///
/// The guarantee is: everything past [`usable_size`] of the *original* block is zero.
/// If you need a specific range zeroed, capture [`usable_size`] before the call and
/// zero the remainder yourself.
///
/// (This is documented so precisely because a fuzz harness asserted the intuitive
/// version and was falsified within seconds — see issue #87.)
///
/// # Safety
///
/// - `p` must be null, or a pointer from the **plain** allocation family — the global
///   allocator, [`sys::mi_malloc`], or a previous [`rezalloc`]/[`recalloc`] — that has
///   not been freed.
/// - **Not interchangeable with [`unwrapped_malloc`]/[`unwrapped_realloc`].** Those
///   place a header before the pointer, so passing one here fails the pointer check
///   (`mi_usable_size: invalid pointer`) rather than working by accident.
/// - After this call `p` is consumed: do not read, write, or free it again, whether or
///   not the return value is null.
/// - On failure a null pointer is returned and `p` is left valid and unfreed.
pub unsafe fn rezalloc(p: *mut u8, new_size: usize) -> *mut u8 {
    unsafe { sys::mi_rezalloc(p.cast(), new_size).cast() }
}

/// Grow or shrink an allocation to `count * size` bytes, zeroing any newly-exposed tail.
///
/// The [`rezalloc`] contract applies, including what is and is not zeroed. Thin wrapper
/// around `mi_recalloc`; the element-count form exists to mirror `calloc`.
///
/// # Safety
///
/// Same contract as [`rezalloc`].
pub unsafe fn recalloc(p: *mut u8, count: usize, size: usize) -> *mut u8 {
    unsafe { sys::mi_recalloc(p.cast(), count, size).cast() }
}

/// Try to grow an allocation **in place**, without moving it.
///
/// Returns a null pointer if the block cannot be extended where it is — in which case
/// `p` remains valid and unchanged, unlike [`rezalloc`]. Useful when moving would be
/// more expensive than falling back to a different strategy.
///
/// # Safety
///
/// - `p` must be a pointer from this allocator that has not been freed.
/// - Unlike [`rezalloc`], `p` is **not** consumed: on failure it is still live and must
///   still be freed.
pub unsafe fn expand(p: *mut u8, new_size: usize) -> *mut u8 {
    unsafe { sys::mi_expand(p.cast(), new_size).cast() }
}

/// Bytes actually available in an allocation, which may exceed what was requested.
///
/// # Safety
///
/// `p` must be a live pointer from this allocator.
pub unsafe fn usable_size(p: *const u8) -> usize {
    unsafe { sys::mi_usable_size(p.cast()) }
}

/// Live per-heap -> per-page -> (optional) per-block JSON snapshot of the current
/// subprocess (issue #269, Bun parity P4). Backs Bun's shipped `bun:jsc`
/// `heapStats({dump:true|"blocks"}).mimallocDump`; see `src/heap-dump.c` for the JSON
/// shape (`{"heaps":[{"seq":N,"pages":[{"id","block_size","used","reserved","thread_id"}],
/// "blocks":[[id,size],...]}]}`, `blocks` present only when `include_blocks`).
///
/// Set `hash_addresses` to mix every reported address through a per-process key so a
/// dump can be shared or diffed without exposing raw ASLR-derived pointers.
///
/// Best-effort under concurrent frees on the heaps being walked (mimalloc's
/// `mi_heap_visit_blocks`/`mi_subproc_visit_heaps` contract; see the caveat on
/// `src/heap-dump.c` and issue #78) -- never `unsafe` to call, but a heap another
/// thread is actively freeing into may be under- or over-reported in the returned JSON.
///
/// Returns `None` only on allocation failure (out of memory building the JSON buffer),
/// not for an empty subprocess.
pub fn heap_dump_json(include_blocks: bool, hash_addresses: bool) -> Option<String> {
    use std::ffi::CStr;
    let ptr = unsafe { sys::mi_heap_dump_json(include_blocks, hash_addresses) };
    if ptr.is_null() {
        return None;
    }
    let json = unsafe { CStr::from_ptr(ptr) }
        .to_string_lossy()
        .into_owned();
    unsafe { sys::mi_free(ptr.cast()) };
    Some(json)
}

/// Write a binary heap snapshot to `path` (issue #338, Bun parity).
///
/// A compact description of every arena and page -- and, with `blocks`, per-block free
/// maps for the pages this thread owns -- in a format byte-identical to oven-sh/mimalloc's
/// (version 1). Read it with `mi-heapview` (built with the C library) or the Python
/// reference reader in `examples/heap-snapshot/`. Point-in-time and best-effort: other
/// threads keep allocating while it is written, so their pages' counts may be slightly
/// stale. Allocation-free on the writer's side, so it is safe to call from anywhere.
///
/// Errors: the file could not be created or written. The same snapshot can be produced
/// without code by setting `MIMALLOC_SNAPSHOT_ON_EXIT=1|2` (and `MIMALLOC_SNAPSHOT_PATH`).
pub fn heap_snapshot_to_file(path: impl AsRef<std::path::Path>, blocks: bool) -> std::io::Result<()> {
    use std::ffi::CString;
    let path = path.as_ref();
    let c_path = CString::new(path.as_os_str().as_encoded_bytes()).map_err(|_| {
        std::io::Error::new(std::io::ErrorKind::InvalidInput, "path contains an interior NUL byte")
    })?;
    let flags = if blocks { sys::MI_SNAPSHOT_BLOCKS } else { 0 };
    let rc = unsafe { sys::mi_heap_snapshot_to_file(c_path.as_ptr(), flags) };
    if rc == 0 {
        Ok(())
    } else {
        Err(std::io::Error::new(
            std::io::ErrorKind::Other,
            format!("mi_heap_snapshot_to_file({}) failed", path.display()),
        ))
    }
}

/// Tell mimalloc this thread is idle (issue #272, Bun parity P7a).
///
/// Collects this thread's pending frees, discards the free blocks inside its still-used
/// pages, and hands the arena purge to the background scavenger thread so freed memory
/// returns to the OS now instead of at the next allocation that happens to run a purge --
/// which, on a genuinely idle process, is never.
///
/// Safe on any thread; a no-op on a thread that never allocated. Call it when the thread
/// has nothing to do (an event loop about to block, a worker pool waiting on its queue),
/// not on a hot path: it costs a few `madvise`/`DiscardVirtualMemory` calls.
pub fn on_thread_idle() {
    unsafe { sys::mi_on_thread_idle() }
}

/// Guard form of [`on_thread_idle`] for a thread that is about to BLOCK: hands this
/// thread's heaps to the background scavenger, which does the idle work above while this
/// thread sits in the kernel, and takes them back on drop.
///
/// Returns `None` when nothing was handed off (no scavenger running, this thread never
/// allocated, or it is already parked). That case is deliberately NOT an inline sweep: a
/// caller blocks far more often than it is truly idle. If this park is idle enough to
/// afford the work, call [`on_thread_idle`] instead.
///
/// The thread must not allocate or free between the call and the drop -- that is the
/// precondition that lets another thread rewrite its free lists -- which is why the guard
/// is `!Send` and holds no data.
#[must_use = "the park ends when the guard is dropped"]
pub fn park_while_idle() -> Option<IdlePark> {
    if unsafe { sys::mi_on_thread_idle_start() } {
        Some(IdlePark {
            _not_send: core::marker::PhantomData,
        })
    } else {
        None
    }
}

/// Returned by [`park_while_idle`]; ends the park when dropped.
pub struct IdlePark {
    // the park is per-thread state: `mi_on_thread_idle_end` must run on the parking thread
    _not_send: core::marker::PhantomData<*const ()>,
}

impl Drop for IdlePark {
    fn drop(&mut self) {
        unsafe { sys::mi_on_thread_idle_end() }
    }
}

/// Stop the background scavenger thread (issue #272).
///
/// It restarts on demand (the next [`park_while_idle`], or the next thread that
/// initializes), so this is a way to quiesce it -- e.g. before a `fork`/`exec` that counts
/// threads, or in a test -- not a way to disable it permanently. For that, set the
/// `scavenger` option to 0 (`MIMALLOC_SCAVENGER=0`) before the first allocation.
pub fn scavenger_stop() {
    unsafe { sys::mi_scavenger_stop() }
}

/// What page hole purging has reclaimed, process wide (issue #272, Bun parity P7b).
///
/// Hole purging discards the memory of the free blocks sitting inside pages that are still
/// in use, at each [`on_thread_idle`] / [`park_while_idle`] point -- without it a page stays
/// fully resident until every block in it is free, so one long-lived object pins a whole
/// 64 KiB/512 KiB page. These counters are the only way to see how much that gets back;
/// they are deliberately not part of `mi_stats_t`, because the sweep also covers pages that
/// no heap owns.
///
/// Most fields are monotonic. `purged_bytes`, `purged_blocks` and `unformed_bytes` are
/// gauges ("right now"), and the three `ineligible_*` fields are a gauge over the LAST sweep
/// only. Everything is zero when the `purge_holes` option is off (`MIMALLOC_PURGE_HOLES=0`).
///
/// ```
/// # use mimalloc_pprof as mi;
/// let before = mi::purge_holes_stats().purged_bytes_total;
/// mi::on_thread_idle();
/// let after = mi::purge_holes_stats().purged_bytes_total;
/// assert!(after >= before);
/// ```
#[must_use]
pub fn purge_holes_stats() -> sys::MiPurgeHolesStats {
    let mut stats = sys::MiPurgeHolesStats::default();
    unsafe { sys::mi_purge_holes_stats_get(&raw mut stats) };
    stats
}

/// Flags for [`purge_all_ex`] (issue #366).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct PurgeFlags {
    /// Ignore `purge_delay` and hole-purge pacing, and let a claimed sweep run to
    /// completion (`MI_PURGE_FORCE`). [`purge_all`]`(true)` is this flag set.
    pub force: bool,
}

impl PurgeFlags {
    /// Every flag clear: honour the purge pacing options.
    pub const NONE: PurgeFlags = PurgeFlags { force: false };
    /// `MI_PURGE_FORCE`.
    pub const FORCE: PurgeFlags = PurgeFlags { force: true };

    fn to_c(self) -> sys::mi_purge_flags_t {
        if self.force {
            sys::MI_PURGE_FORCE
        } else {
            0
        }
    }
}

/// The outcome of a [`purge_all`] / [`purge_all_ex`] call (issue #366).
///
/// `Partial` is a **normal outcome**, not an error: in a default build only threads
/// parked in [`park_while_idle`] can be swept from another thread, so every running
/// thread is reported as pending. Only `Busy` means nothing happened at all.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PurgeStatus {
    /// Every registered thread was reached (`MI_PURGE_OK`).
    Ok,
    /// Some owners were still pending when `wait_ms` ran out; everything reachable was
    /// purged (`MI_PURGE_PARTIAL`). See [`PurgeAllReport::theaps_pending`].
    Partial,
    /// Another purge is in flight, or this is a re-entrant call: nothing was done
    /// (`MI_PURGE_BUSY`).
    Busy,
}

impl PurgeStatus {
    fn from_c(rc: core::ffi::c_int) -> PurgeStatus {
        match rc {
            sys::MI_PURGE_OK => PurgeStatus::Ok,
            sys::MI_PURGE_PARTIAL => PurgeStatus::Partial,
            sys::MI_PURGE_BUSY => PurgeStatus::Busy,
            other => unreachable!("mi_purge_all_ex returned an unknown status {other}"),
        }
    }
}

/// What a [`purge_all`] / [`purge_all_ex`] call returned to the OS and which threads it
/// could not reach (issue #366). Mirrors `mi_purge_all_report_t`.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct PurgeAllReport {
    /// Bytes returned to the OS by the arena passes.
    pub arena_bytes: usize,
    /// Bytes returned by hole purging (every swept thread plus abandoned pages).
    pub hole_bytes: usize,
    /// Threads claimed and swept by this call, the caller included.
    pub theaps_swept: usize,
    /// Registered threads not reached within `wait_ms`. Non-zero is the expected shape in
    /// a default (ungated) build with running threads.
    pub theaps_pending: usize,
    /// Pre-fork threads of vanished threads, never touched.
    pub theaps_orphaned: usize,
    /// Whether the allocator was built with the owner gate (the crate's `owner-gate`
    /// feature, C's `MI_OWNER_GATE=1`). Configuration, not completion.
    pub gated: bool,
    /// `theaps_pending == 0 && theaps_orphaned == 0`.
    pub complete: bool,
}

impl From<sys::mi_purge_all_report_t> for PurgeAllReport {
    fn from(r: sys::mi_purge_all_report_t) -> PurgeAllReport {
        PurgeAllReport {
            arena_bytes: r.arena_bytes,
            hole_bytes: r.hole_bytes,
            theaps_swept: r.theaps_swept,
            theaps_pending: r.theaps_pending,
            theaps_orphaned: r.theaps_orphaned,
            gated: r.gated,
            complete: r.complete,
        }
    }
}

/// Process-wide eager purge from any thread (issue #366): the full form of [`purge_all`].
///
/// Returns as much memory as the allocator's invariants allow across ALL threads' heaps --
/// arenas, abandoned pages, the caller's own pages and holes, and every other registered
/// thread's pages and holes that can be claimed: any thread parked in [`park_while_idle`],
/// and, with the `owner-gate` feature (C `MI_OWNER_GATE=1`), any thread that is outside an
/// allocator call for long enough to be caught. The report says exactly what was returned
/// and what could not be reached.
///
/// `wait_ms` bounds **owner-acquisition waiting only** -- how long this call keeps trying
/// to claim other threads' state. It does not bound a claimed thread's sweep, nor the
/// `madvise`/`DiscardVirtualMemory` syscalls that sweep makes, so the call can take longer
/// than `wait_ms` once it has something to purge.
///
/// [`PurgeStatus::Partial`] is a normal outcome, not a failure: everything reachable was
/// purged and `theaps_pending` counts the threads that were not. In a default build with
/// other threads running it is the *usual* outcome. Only [`PurgeStatus::Busy`] means
/// nothing was done (another purge is in flight, or this call re-entered one), and the
/// report is then all zeros apart from `gated`.
///
/// ```
/// # use mimalloc_pprof as mi;
/// let (status, report) = mi::purge_all_ex(mi::PurgeFlags::FORCE, 100);
/// assert_ne!(status, mi::PurgeStatus::Busy);
/// assert_eq!(report.complete, report.theaps_pending == 0 && report.theaps_orphaned == 0);
/// ```
pub fn purge_all_ex(flags: PurgeFlags, wait_ms: usize) -> (PurgeStatus, PurgeAllReport) {
    let mut report = sys::mi_purge_all_report_t::default();
    let rc = unsafe { sys::mi_purge_all_ex(flags.to_c(), wait_ms, &raw mut report) };
    (PurgeStatus::from_c(rc), PurgeAllReport::from(report))
}

/// Process-wide eager purge from any thread (issue #366), with the C default of a 100 ms
/// owner-acquisition wait: `mi_purge_all(force)`, but with the report kept.
///
/// `force` ignores the `purge_delay` / hole-purge pacing options and lets each claimed
/// sweep run to completion. See [`purge_all_ex`] for what the wait bounds (owner
/// acquisition only, never a sweep or its syscalls) and why a partial result -- some
/// threads pending -- is the normal outcome in a build without the `owner-gate` feature.
/// The status is [`PurgeAllReport::complete`]; a busy (nothing-done) call reports zero
/// `theaps_swept`. Use [`purge_all_ex`] when the status itself is needed.
///
/// Goes through `mi_purge_all_ex` rather than C's `mi_purge_all`, which is the same call
/// with the report thrown away.
pub fn purge_all(force: bool) -> PurgeAllReport {
    let flags = if force {
        PurgeFlags::FORCE
    } else {
        PurgeFlags::NONE
    };
    purge_all_ex(flags, PURGE_ALL_DEFAULT_WAIT_MS).1
}

/// The `wait_ms` C's `mi_purge_all` passes to `mi_purge_all_ex`, and what [`purge_all`]
/// uses.
pub const PURGE_ALL_DEFAULT_WAIT_MS: usize = 100;

/// Exact DHAT v2 heap/lifetime profiling controls.
///
/// DHAT records every non-internal allocation from the moment [`start`] succeeds.
/// It is intended for short diagnostic runs and tests rather than continuous production
/// telemetry. The generated JSON opens in the standard Valgrind `dh_view.html` viewer.
/// It is independent of sampled [`prof`] profiling and of `mi_memory_set_callbacks`.
pub mod dhat {
    use std::ffi::CString;
    use std::io;
    use std::path::Path;

    use crate::sys;

    /// Snapshot of exact DHAT collector state.
    #[derive(Debug, Clone, Default, PartialEq, Eq)]
    pub struct Stats {
        pub enabled: bool,
        /// True when raw-OS collector storage hit its configured budget or an internal
        /// allocation failed. The application allocation still completed, but the
        /// resulting profile is intentionally marked partial.
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

    /// Start exact allocation/lifetime tracking. Returns `false` if it is already active.
    pub fn start() -> bool {
        unsafe { sys::mi_dhat_start() }
    }

    /// Stop observing allocation events. Retained records remain available to [`dump_file`]
    /// so a caller can stop a measurement window before serializing its report.
    pub fn stop() {
        unsafe { sys::mi_dhat_stop() }
    }

    /// Whether exact DHAT tracking is currently active.
    pub fn is_enabled() -> bool {
        unsafe { sys::mi_dhat_is_enabled() }
    }

    /// Read the collector's exact counters. Returns a zero/default snapshot only if the
    /// linked C library rejected the versioned ABI structure.
    pub fn stats() -> Stats {
        let mut raw: sys::mi_dhat_stats_t = unsafe { core::mem::zeroed() };
        raw.size = core::mem::size_of::<sys::mi_dhat_stats_t>();
        raw.version = sys::MI_DHAT_STATS_VERSION;
        if unsafe { sys::mi_dhat_stats_get(&mut raw) } {
            Stats {
                enabled: raw.enabled,
                incomplete: raw.incomplete,
                total_bytes: raw.total_bytes,
                total_blocks: raw.total_blocks,
                live_bytes: raw.live_bytes,
                live_blocks: raw.live_blocks,
                peak_bytes: raw.peak_bytes,
                peak_blocks: raw.peak_blocks,
                dropped: raw.dropped,
                internal_bytes: raw.internal_bytes,
            }
        } else {
            Stats::default()
        }
    }

    /// Serialize the current or stopped measurement window as a DHAT v2 JSON file.
    pub fn dump_file(path: &Path) -> io::Result<()> {
        let path = path
            .to_str()
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "DHAT path is not UTF-8"))?;
        let path = CString::new(path)
            .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "DHAT path contains NUL"))?;
        if unsafe { sys::mi_dhat_dump(path.as_ptr()) } {
            Ok(())
        } else {
            Err(io::Error::last_os_error())
        }
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
/// and its sample rate, stay active), or if the crate was built with
/// `default-features = false`.
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
/// stays active), if the crate was built with `default-features = false`, or
/// if `config.dump_at_exit` is set but is not
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
        /// Allocator-level ("ground truth") counters, read from the mimalloc v3
        /// engine's per-heap statistics at the time of the call. Every field
        /// above is *sampled*; these are exact, so comparing them against
        /// `live_bytes` measures the sampler's error directly -- which is what
        /// makes an assertion on a sampled profile meaningful in a test.
        pub heap: HeapStats,
    }

    /// Exact allocator counters accompanying a [`ProfStats`] reading.
    ///
    /// These come from mimalloc v3's per-heap statistics
    /// (`mi_heap_stats_get`/`mi_subproc_stats_get`), which the v2 engine did not
    /// expose. They are valid even when the profiler is stopped.
    #[derive(Debug, Clone, Default)]
    pub struct HeapStats {
        /// Bytes currently committed from the OS.
        pub committed: usize,
        /// Bytes currently reserved from the OS (always `>= committed`).
        pub reserved: usize,
        /// Bytes the application actually requested and still holds.
        ///
        /// Only maintained when the C library was built with `MI_STAT >= 2`;
        /// otherwise this is 0. Check [`HeapStats::detailed`] before using it.
        pub malloc_requested: usize,
        /// Live mimalloc pages.
        pub pages: usize,
        /// Pages abandoned by exited threads.
        pub pages_abandoned: usize,
        /// Live first-class heaps.
        pub heaps: usize,
        /// Live thread-local heaps. The main thread's statically-initialized
        /// theap is not counted, so a single-threaded process reports 0.
        pub theaps: usize,
        /// Cumulative bytes purged back to the OS.
        pub purged: usize,
        /// Whether the C library was built with `MI_STAT >= 2` ("detailed"
        /// statistics), which upstream enables by default only for debug
        /// builds. [`HeapStats::malloc_requested`] is maintained only at that
        /// level; every other field here is maintained at any level.
        ///
        /// Without this flag you cannot tell "the application allocated
        /// nothing" from "this build does not track that counter".
        pub detailed: bool,
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
                heap: HeapStats {
                    committed: raw.heap_committed,
                    reserved: raw.heap_reserved,
                    malloc_requested: raw.heap_malloc_requested,
                    pages: raw.heap_pages,
                    pages_abandoned: raw.heap_pages_abandoned,
                    heaps: raw.heap_count,
                    theaps: raw.theap_count,
                    purged: raw.heap_purged,
                    detailed: raw.heap_stats_detailed,
                },
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
            sys::mi_prof_modules_visit(modules_visitor, (&mut out as *mut Vec<ModuleInfo>).cast());
        }
        out
    }
}

/// Print, per size class, what hole purging leaves behind (issue #272, Bun parity P7b).
///
/// Read-only: it purges nothing and mutates no free list. The report goes to mimalloc's
/// own output sink (stderr by default), not to a returned `String` — building a `String`
/// here would allocate from inside a walk over the very free lists being reported.
/// `mi_purge_holes_report` takes no sink argument at all; to capture the text, install a
/// process-wide sink with C's `mi_register_output` (not bound by this crate).
///
/// Like the idle sweep it only covers what the calling thread may safely read: its own
/// theaps, plus the abandoned pages of the heaps behind them. Call it right after an
/// [`on_thread_idle`] sweep, when the numbers still describe that sweep.
pub fn purge_holes_report() {
    unsafe { sys::mi_purge_holes_report() }
}

/// mimalloc's `mi_option_*` settings: the runtime knobs behind every `MIMALLOC_*`
/// environment variable.
///
/// Options are read once, lazily, the first time the allocator needs them, so setting one
/// after the allocation it governs has already happened has no effect. In particular
/// [`Opt::SCAVENGER`] and the profiler options must be set before the first allocation to
/// matter; [`Opt::PURGE_HOLES`] and its companions are re-read per sweep and can be
/// changed at any time.
///
/// ```
/// use mimalloc_pprof::options::{self, Opt};
/// let previous = options::get(Opt::PURGE_HOLES_MIN_INTERVAL);
/// options::set(Opt::PURGE_HOLES_MIN_INTERVAL, 0); // sweep on every idle call
/// mimalloc_pprof::on_thread_idle();
/// options::set(Opt::PURGE_HOLES_MIN_INTERVAL, previous);
/// ```
pub mod options {
    use core::ffi::c_long;

    use crate::sys;

    /// One `mi_option_t` setting.
    ///
    /// The associated constants name this fork's own options plus the handful of upstream
    /// ones that interact with them; [`Opt::from_raw`] reaches any other enumerator in
    /// [`sys`] — it range-checks against `_mi_option_last`, because the C side indexes an
    /// array with this value and an out-of-range option would read out of bounds.
    #[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
    pub struct Opt(sys::mi_option_t);

    impl Opt {
        /// **Fork addition.** Enable the sampled profiler at process start (`MIMALLOC_PROF`).
        pub const PROF: Self = Self(sys::mi_option_prof);
        /// **Fork addition.** Average byte interval between profiler samples.
        pub const PROF_SAMPLE_RATE: Self = Self(sys::mi_option_prof_sample_rate);
        /// **Fork addition.** Maximum captured stack depth for the profiler.
        pub const PROF_BT_MAX: Self = Self(sys::mi_option_prof_bt_max);
        /// **Fork addition.** Keep cumulative profiler counters until [`crate::prof::reset`].
        pub const PROF_ACCUM: Self = Self(sys::mi_option_prof_accum);
        /// **Fork addition.** Profiler sampling PRNG seed; 0 = nondeterministic.
        pub const PROF_SEED: Self = Self(sys::mi_option_prof_seed);
        /// **Fork addition.** Budget in bytes for profiler-internal arena memory.
        pub const PROF_MAX_BYTES: Self = Self(sys::mi_option_prof_max_bytes);
        /// **Fork addition.** Enable [`crate::memory_events`] accounting
        /// (`MIMALLOC_MEMORY_EVENTS`).
        pub const MEMORY_EVENTS: Self = Self(sys::mi_option_memory_events);
        /// **Fork addition, dead since #80.** Parses, but has no effect. Kept so nothing
        /// renumbers; unrelated to [`Opt::PURGE_HOLES_EAGER_ZERO`].
        pub const PURGE_ZEROES: Self = Self(sys::mi_option_purge_zeroes);
        /// **Fork addition (Bun).** Run the background scavenger thread.
        pub const SCAVENGER: Self = Self(sys::mi_option_scavenger);
        /// **Fork addition (Bun).** Discard free blocks inside still-used pages on
        /// [`crate::on_thread_idle`].
        pub const PURGE_HOLES: Self = Self(sys::mi_option_purge_holes);
        /// **Fork addition (Bun).** Zero a range before discarding it, so a mis-scoped
        /// discard corrupts visibly. Forced on in debug builds.
        pub const PURGE_HOLES_EAGER_ZERO: Self = Self(sys::mi_option_purge_holes_eager_zero);
        /// **Fork addition (Bun).** Minimum milliseconds between sweeps of one thread's heaps.
        pub const PURGE_HOLES_MIN_INTERVAL: Self = Self(sys::mi_option_purge_holes_min_interval);
        /// **Fork addition (Bun).** Every N-th sweep walks every page; 0 disables.
        pub const PURGE_HOLES_FULL_EVERY: Self = Self(sys::mi_option_purge_holes_full_every);
        /// **Fork addition (Bun parity, #338).** Write a heap snapshot at process exit: 0 = off,
        /// 1 = pages, 2 = pages + per-block free maps (`MIMALLOC_SNAPSHOT_PATH` names the file).
        pub const SNAPSHOT_ON_EXIT: Self = Self(sys::mi_option_snapshot_on_exit);

        /// Upstream: milliseconds to delay purging, which the scavenger also honours.
        pub const PURGE_DELAY: Self = Self(sys::mi_option_purge_delay);
        /// Upstream: print statistics on process termination.
        pub const SHOW_STATS: Self = Self(sys::mi_option_show_stats);
        /// Upstream: print error messages.
        pub const SHOW_ERRORS: Self = Self(sys::mi_option_show_errors);
        /// Upstream: print verbose messages.
        pub const VERBOSE: Self = Self(sys::mi_option_verbose);

        /// Wrap a raw `mi_option_t` from [`sys`], or `None` if it is not a real option.
        ///
        /// The range check is load bearing: the C implementation indexes its option table
        /// with this value, so an out-of-range option is an out-of-bounds read.
        #[must_use]
        pub fn from_raw(raw: sys::mi_option_t) -> Option<Self> {
            if (0..sys::_mi_option_last).contains(&raw) {
                Some(Self(raw))
            } else {
                None
            }
        }

        /// The raw `mi_option_t` value.
        #[must_use]
        pub fn as_raw(self) -> sys::mi_option_t {
            self.0
        }

        /// The C enumerator's name, e.g. `mi_option_purge_holes`.
        #[must_use]
        pub fn name(self) -> &'static str {
            sys::MI_OPTIONS_IN_ORDER
                .get(self.0 as usize)
                .map_or("<unknown>", |(name, _)| *name)
        }
    }

    /// Read an option's value.
    ///
    /// Note the width: mimalloc stores option values in a C `long`, which is 32-bit on
    /// Windows and 64-bit on Linux/macOS. Use [`get_size`] for byte counts.
    #[must_use]
    pub fn get(option: Opt) -> c_long {
        unsafe { sys::mi_option_get(option.as_raw()) }
    }

    /// Read an option's value, clamped into `min..=max`.
    #[must_use]
    pub fn get_clamp(option: Opt, min: c_long, max: c_long) -> c_long {
        unsafe { sys::mi_option_get_clamp(option.as_raw(), min, max) }
    }

    /// Read an option's value as a `size_t`, for options that count bytes.
    #[must_use]
    pub fn get_size(option: Opt) -> usize {
        unsafe { sys::mi_option_get_size(option.as_raw()) }
    }

    /// Set an option's value, overriding both the default and the environment.
    pub fn set(option: Opt, value: c_long) {
        unsafe { sys::mi_option_set(option.as_raw(), value) }
    }

    /// Set an option's value only if the environment did not already set it.
    pub fn set_default(option: Opt, value: c_long) {
        unsafe { sys::mi_option_set_default(option.as_raw(), value) }
    }

    /// Whether a boolean option is on.
    #[must_use]
    pub fn is_enabled(option: Opt) -> bool {
        unsafe { sys::mi_option_is_enabled(option.as_raw()) }
    }

    /// Turn a boolean option on.
    pub fn enable(option: Opt) {
        unsafe { sys::mi_option_enable(option.as_raw()) }
    }

    /// Turn a boolean option off.
    pub fn disable(option: Opt) {
        unsafe { sys::mi_option_disable(option.as_raw()) }
    }

    /// Turn a boolean option on or off.
    pub fn set_enabled(option: Opt, enabled: bool) {
        unsafe { sys::mi_option_set_enabled(option.as_raw(), enabled) }
    }

    /// Set a boolean option's default, which the environment still overrides.
    pub fn set_enabled_default(option: Opt, enabled: bool) {
        unsafe { sys::mi_option_set_enabled_default(option.as_raw(), enabled) }
    }

    /// Print every option's current value to mimalloc's output sink.
    ///
    /// Goes to the sink rather than to a returned `String` for the same reason as
    /// [`crate::purge_holes_report`]: capturing it would mean allocating from inside a
    /// callback the allocator drives.
    pub fn print() {
        unsafe { sys::mi_options_print_out(None, core::ptr::null_mut()) }
    }

    /// Every option this build knows about, in C declaration order, as
    /// `(name, value)` pairs — including the ones without an [`Opt`] constant.
    #[must_use]
    pub fn all() -> &'static [(&'static str, sys::mi_option_t)] {
        sys::MI_OPTIONS_IN_ORDER
    }
}

/// The allocator's own **exact** statistics, as opposed to the sampled numbers
/// [`crate::prof::stats`] reports.
///
/// This is upstream mimalloc's `mimalloc-stats.h` surface. Note what is *not* here:
/// hole-purging and idle-sweep gauges are **not** part of `mi_stats_t` — they live in
/// [`crate::purge_holes_stats`], because the sweep also covers pages that no heap owns
/// and `mi_stats_t` cannot grow (it is embedded in a theap, at the meta-allocator's 8 KB
/// block limit).
///
/// `malloc_requested` is only maintained when the C library was built with `MI_STAT >= 2`
/// (upstream enables that for debug builds only); a default release build reports 0 for
/// it and for nothing else.
pub mod stats {
    use core::ops::Deref;
    use std::ffi::CStr;

    use crate::sys;

    /// An owned copy of `mi_stats_t`, boxed because it is ~4 KB.
    ///
    /// Deref to reach every counter, e.g. `stats.committed.current`.
    #[derive(Clone, Debug)]
    pub struct Stats(Box<sys::mi_stats_t>);

    impl Deref for Stats {
        type Target = sys::mi_stats_t;
        fn deref(&self) -> &Self::Target {
            &self.0
        }
    }

    impl Stats {
        /// Render these counters as mimalloc's statistics JSON.
        ///
        /// Returns `None` on allocation failure. Wraps `mi_stats_as_json`.
        #[must_use]
        pub fn to_json(&self) -> Option<String> {
            // `mi_stats_as_json` takes a non-const pointer but only reads through it.
            let mut copy = self.0.clone();
            let ptr = unsafe { sys::mi_stats_as_json(&raw mut *copy, 0, core::ptr::null_mut()) };
            take_c_string(ptr)
        }

        /// The raw C struct.
        #[must_use]
        pub fn as_raw(&self) -> &sys::mi_stats_t {
            &self.0
        }
    }

    /// A zeroed `mi_stats_t` with its `size`/`version` header filled in, which every
    /// `*_stats_get` entry point checks before writing a single counter.
    fn empty() -> Box<sys::mi_stats_t> {
        let mut raw: Box<sys::mi_stats_t> = Box::new(unsafe { core::mem::zeroed() });
        raw.size = size_of::<sys::mi_stats_t>();
        raw.version = sys::MI_STAT_VERSION;
        raw
    }

    /// Copy a `mi_malloc`-family C string out and release it with `mi_free`.
    fn take_c_string(ptr: *mut core::ffi::c_char) -> Option<String> {
        if ptr.is_null() {
            return None;
        }
        let owned = unsafe { CStr::from_ptr(ptr) }
            .to_string_lossy()
            .into_owned();
        unsafe { sys::mi_free(ptr.cast()) };
        Some(owned)
    }

    /// Which subprocess a subprocess-scoped call refers to.
    #[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
    pub enum Subproc {
        /// The process-wide default subprocess (`mi_subproc_main`).
        #[default]
        Main,
        /// The subprocess this thread belongs to (`mi_subproc_current`).
        Current,
    }

    impl Subproc {
        fn id(self) -> sys::mi_subproc_id_t {
            unsafe {
                match self {
                    Self::Main => sys::mi_subproc_main(),
                    Self::Current => sys::mi_subproc_current(),
                }
            }
        }
    }

    /// Statistics for the current subprocess and all its heaps, aggregated.
    ///
    /// Wraps `mi_stats_get`. Returns `None` only if the C library rejects the struct
    /// header, which would mean this crate's `mi_stats_t` mirror has drifted from the
    /// library it is linked against (`tests/t19_layout.rs` gates exactly that).
    #[must_use]
    pub fn get() -> Option<Stats> {
        let mut raw = empty();
        unsafe { sys::mi_stats_get(&raw mut *raw) }.then_some(Stats(raw))
    }

    /// The same statistics as [`get`], rendered as JSON by the C library.
    ///
    /// Wraps `mi_stats_get_json`; returns `None` on allocation failure.
    #[must_use]
    pub fn json() -> Option<String> {
        take_c_string(unsafe { sys::mi_stats_get_json(0, core::ptr::null_mut()) })
    }

    /// Print the current subprocess's statistics to mimalloc's output sink.
    ///
    /// Wraps `mi_stats_print_out(NULL, NULL)`. Use [`json`] to capture them instead:
    /// routing the sink through a Rust closure would allocate from inside a callback the
    /// allocator drives.
    pub fn print() {
        unsafe { sys::mi_stats_print_out(None, core::ptr::null_mut()) }
    }

    /// The block size served by size bin `bin` (`0..=`[`sys::MI_BIN_HUGE`]), matching the
    /// `malloc_bins`/`page_bins` indices.
    #[must_use]
    pub fn bin_size(bin: usize) -> usize {
        unsafe { sys::mi_stats_get_bin_size(bin) }
    }

    /// Statistics for one subprocess and all its heaps, aggregated.
    #[must_use]
    pub fn subproc_get(which: Subproc) -> Option<Stats> {
        let mut raw = empty();
        unsafe { sys::mi_subproc_stats_get(which.id(), &raw mut *raw) }.then_some(Stats(raw))
    }

    /// Statistics for one subprocess **without** aggregating its heaps.
    #[must_use]
    pub fn subproc_get_exclusive(which: Subproc) -> Option<Stats> {
        let mut raw = empty();
        unsafe { sys::mi_subproc_stats_get_exclusive(which.id(), &raw mut *raw) }
            .then_some(Stats(raw))
    }

    /// One subprocess's aggregated statistics as JSON.
    #[must_use]
    pub fn subproc_json(which: Subproc) -> Option<String> {
        take_c_string(unsafe {
            sys::mi_subproc_stats_get_json(which.id(), 0, core::ptr::null_mut())
        })
    }

    /// Print one subprocess's aggregated statistics to mimalloc's output sink.
    pub fn subproc_print(which: Subproc) {
        unsafe { sys::mi_subproc_stats_print_out(which.id(), None, core::ptr::null_mut()) }
    }

    /// Print one subprocess **and each of its heaps separately** to mimalloc's output sink.
    pub fn subproc_heap_print(which: Subproc) {
        unsafe {
            sys::mi_subproc_heap_stats_print_out(which.id(), None, core::ptr::null_mut());
        }
    }
}

/// Opt-in allocation-change accounting and callbacks (`include/mimalloc/memory-events.h`).
///
/// Independent of the sampled profiler: this module is compiled into the C library in
/// every configuration, including `default-features = false`. Tracking is **off** by
/// default; while it is off every allocate/free/realloc pays for exactly one relaxed flag
/// check and nothing else.
///
/// ```
/// use mimalloc_pprof::{memory_events, MiMalloc};
///
/// // The counters only move for allocations that actually reach mimalloc, so this
/// // example is only meaningful once mimalloc is the global allocator.
/// #[global_allocator]
/// static ALLOCATOR: MiMalloc = MiMalloc;
///
/// fn main() {
///     memory_events::set_enabled(true);
///     let before = memory_events::snapshot().expect("snapshot").accum_count;
///     let v = vec![0_u8; 4096];
///     std::hint::black_box(&v);
///     let after = memory_events::snapshot().expect("snapshot").accum_count;
///     assert!(after > before);
///     memory_events::set_enabled(false);
/// }
/// ```
pub mod memory_events {
    use core::ffi::c_void;
    use std::panic::{catch_unwind, AssertUnwindSafe};

    use crate::sys;

    /// Which kind of change a [`Change`] describes.
    #[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
    #[non_exhaustive]
    pub enum ChangeKind {
        /// A successful allocation.
        Allocate,
        /// A successful free.
        Free,
        /// A successful realloc, whether it grew or shrank.
        Resize,
    }

    impl ChangeKind {
        fn from_raw(raw: sys::mi_memory_change_kind_t) -> Option<Self> {
            match raw {
                sys::MI_MEMORY_ALLOCATE => Some(Self::Allocate),
                sys::MI_MEMORY_FREE => Some(Self::Free),
                sys::MI_MEMORY_RESIZE => Some(Self::Resize),
                _ => None,
            }
        }

        fn slot(self) -> usize {
            match self {
                Self::Allocate => sys::MI_MEMORY_ALLOCATE as usize,
                Self::Free => sys::MI_MEMORY_FREE as usize,
                Self::Resize => sys::MI_MEMORY_RESIZE as usize,
            }
        }
    }

    /// One observed allocation change, copied out of `mi_memory_change_t`.
    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    pub struct Change {
        /// What happened.
        pub kind: ChangeKind,
        /// Tracked global live usable bytes after this operation.
        pub total_bytes: u64,
        /// Signed change in tracked live usable bytes: positive for allocation/growth,
        /// negative for free/shrink, zero for a same-size-class resize.
        pub delta_bytes: i64,
        /// Caller-requested size for allocate and resize; zero for free.
        pub request_size: u64,
    }

    /// Running totals maintained while tracking is enabled.
    ///
    /// Counters are **not** reconstructed for time spent with tracking off: a total is
    /// exact only if tracking was enabled before the first allocation and never disabled.
    #[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
    pub struct Snapshot {
        /// Tracked live usable bytes right now.
        pub live_bytes: u64,
        /// Cumulative usable bytes ever allocated.
        pub accum_bytes: u64,
        /// Tracked live allocation count right now.
        pub live_count: u64,
        /// Cumulative count of successful allocate events.
        pub accum_count: u64,
    }

    /// Enable or disable tracking; returns the previous state.
    ///
    /// An explicit call is always authoritative over the `MIMALLOC_MEMORY_EVENTS`
    /// environment read: called before the first allocation it *replaces* that read;
    /// called after, it overrides the cached flag. Re-enabling does not reconstruct what
    /// happened while tracking was off.
    pub fn set_enabled(enabled: bool) -> bool {
        unsafe { sys::mi_memory_tracking_set_enabled(enabled) }
    }

    /// Whether tracking is on.
    #[must_use]
    pub fn is_enabled() -> bool {
        unsafe { sys::mi_memory_tracking_is_enabled() }
    }

    /// Read the running totals.
    ///
    /// Returns `None` only if the C library rejects the struct header, which would mean
    /// this crate's mirror has drifted from the library (`tests/t19_layout.rs` gates that).
    #[must_use]
    pub fn snapshot() -> Option<Snapshot> {
        let mut raw: sys::mi_memory_snapshot_t = unsafe { core::mem::zeroed() };
        raw.size = size_of::<sys::mi_memory_snapshot_t>();
        raw.version = sys::MI_MEMORY_SNAPSHOT_VERSION;
        if !unsafe { sys::mi_memory_snapshot(&raw mut raw) } {
            return None;
        }
        Some(Snapshot {
            live_bytes: raw.live_bytes,
            accum_bytes: raw.accum_bytes,
            live_count: raw.live_count,
            accum_count: raw.accum_count,
        })
    }

    /// Handlers to install with [`set_callbacks`], one per [`ChangeKind`].
    ///
    /// Plain `fn` pointers rather than closures on purpose: the C side keeps the
    /// registration until it is replaced, so anything captured would have to outlive the
    /// process. Route per-instance state through a `static` (an atomic counter, a channel
    /// sender in a `OnceLock`) instead.
    #[derive(Clone, Copy, Debug, Default)]
    pub struct Callbacks {
        /// Called after each successful allocation.
        pub allocate: Option<fn(&Change)>,
        /// Called after each successful free.
        pub free: Option<fn(&Change)>,
        /// Called after each successful realloc.
        pub resize: Option<fn(&Change)>,
    }

    impl Callbacks {
        fn handler(&self, kind: ChangeKind) -> Option<fn(&Change)> {
            match kind {
                ChangeKind::Allocate => self.allocate,
                ChangeKind::Free => self.free,
                ChangeKind::Resize => self.resize,
            }
        }
    }

    /// The single C-ABI entry point for all three slots. `arg` is the `&'static Callbacks`
    /// the caller handed to [`set_callbacks`]; the kind comes out of the change record, so
    /// one trampoline serves every slot.
    unsafe extern "C" fn dispatch(change: *const sys::mi_memory_change_t, arg: *mut c_void) {
        if change.is_null() || arg.is_null() {
            return;
        }
        let raw = unsafe { &*change };
        let callbacks = unsafe { &*(arg as *const Callbacks) };
        // An unknown kind means the C enum grew: ignore it rather than guessing.
        let Some(kind) = ChangeKind::from_raw(raw.kind) else {
            return;
        };
        let Some(handler) = callbacks.handler(kind) else {
            return;
        };
        let change = Change {
            kind,
            total_bytes: raw.total_bytes,
            delta_bytes: raw.delta_bytes,
            request_size: raw.request_size,
        };
        // A panic must not unwind across the C frame that called us.
        let _ = catch_unwind(AssertUnwindSafe(|| handler(&change)));
    }

    /// Install `callbacks`, replacing any previous table. Returns `false` if the C library
    /// refused the table.
    ///
    /// `'static` is what makes this safe: the C side keeps the pointer until the table is
    /// replaced or cleared, which is exactly the header's "`arg` pointers are caller-owned
    /// and must stay valid" requirement.
    ///
    /// Callbacks run with no allocator locks held and **may** allocate, but a hook that
    /// fires while another hook's callback is running on the same thread is suppressed —
    /// so bytes a callback itself allocates never reach the running totals. Keep them
    /// short, and let them return normally: a panic is caught and swallowed here, but a
    /// C `longjmp` out of one is unsupported.
    pub fn set_callbacks(callbacks: &'static Callbacks) -> bool {
        let mut raw = sys::mi_memory_callbacks_t {
            handlers: [None; sys::MI_MEMORY_CHANGE_COUNT],
            args: [core::ptr::null_mut(); sys::MI_MEMORY_CHANGE_COUNT],
        };
        let arg = (callbacks as *const Callbacks).cast_mut().cast::<c_void>();
        for kind in [ChangeKind::Allocate, ChangeKind::Free, ChangeKind::Resize] {
            if callbacks.handler(kind).is_some() {
                raw.handlers[kind.slot()] = Some(dispatch);
                raw.args[kind.slot()] = arg;
            }
        }
        unsafe { sys::mi_memory_set_callbacks(&raw const raw) }
    }

    /// Remove every installed callback. Accounting (and [`snapshot`]) keeps working.
    pub fn clear_callbacks() -> bool {
        unsafe { sys::mi_memory_set_callbacks(core::ptr::null()) }
    }

    unsafe extern "C" fn visit_trampoline<F>(
        allocation: *mut c_void,
        usable_size: usize,
        arg: *mut c_void,
    ) -> bool
    where
        F: FnMut(*mut u8, usize) -> bool,
    {
        let visitor = unsafe { &mut *(arg as *mut F) };
        catch_unwind(AssertUnwindSafe(|| visitor(allocation.cast(), usable_size))).unwrap_or(false)
    }

    /// Walk the live allocations this thread may safely observe, calling `visitor` with
    /// each one's address and usable size. Return `false` from `visitor` to stop early.
    ///
    /// Diagnostics only. This is **not** a consistent global snapshot: it is built on
    /// `mi_heap_visit_blocks`, so another thread may free a reported allocation the
    /// instant the callback begins.
    ///
    /// # Safety
    ///
    /// - `visitor` must not allocate, free, or otherwise reenter mimalloc while the walk
    ///   is active — that includes anything that allocates indirectly, such as `println!`,
    ///   growing a `Vec`, or formatting. Collect into a fixed-size buffer, or into
    ///   [`crate::unwrapped_malloc`] memory, and process it after this returns.
    /// - `visitor` must not panic. Raising a panic allocates its payload and its message
    ///   through the global allocator, which reenters mimalloc in the middle of the walk
    ///   -- the very thing the bullet above forbids. The `catch_unwind` inside the
    ///   trampoline stops the unwind from crossing the C frame; it does **not** and
    ///   cannot prevent that allocation, which has already happened by the time it runs.
    ///   Report failures by setting a flag the caller reads after the walk returns.
    /// - The pointers handed to `visitor` must not be dereferenced, retained, or freed:
    ///   they may already be dead. Treat them as addresses, not as references.
    /// - No other thread may be freeing into the heaps being walked (the
    ///   `mi_heap_visit_blocks` precondition; see `include/mimalloc.h`).
    pub unsafe fn visit_live_allocations<F>(mut visitor: F) -> bool
    where
        F: FnMut(*mut u8, usize) -> bool,
    {
        unsafe {
            sys::mi_memory_visit_live_allocations(
                visit_trampoline::<F>,
                (&raw mut visitor).cast::<c_void>(),
            )
        }
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
    #[cfg(feature = "pprof")]
    static PROF_TEST_LOCK: Mutex<()> = Mutex::new(());
    static DHAT_TEST_LOCK: Mutex<()> = Mutex::new(());

    #[cfg(feature = "pprof")]
    fn reset_profiler() {
        if prof::is_enabled() {
            prof::stop();
        }
    }

    #[test]
    #[cfg(feature = "pprof")]
    fn enable_heap_profiling_with_default_config_starts_profiler() {
        let _guard = PROF_TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        reset_profiler();

        let config = ProfConfig::default();
        assert!(enable_heap_profiling_with(&config));
        assert!(prof::is_enabled());

        prof::stop();
    }

    #[test]
    #[cfg(feature = "pprof")]
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
    #[cfg(not(feature = "pprof"))]
    fn heap_profiling_is_unavailable_when_compiled_out() {
        assert!(!enable_heap_profiling_with(&ProfConfig::default()));
        assert!(!prof::is_enabled());
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

    #[test]
    fn dhat_controls_report_lifecycle() {
        let _guard = DHAT_TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        if dhat::is_enabled() {
            dhat::stop();
        }
        assert!(dhat::start());
        let active = dhat::stats();
        assert!(active.enabled);
        dhat::stop();
        assert!(!dhat::is_enabled());
        assert!(!dhat::stats().enabled);
    }
    #[test]
    fn heap_dump_json_reports_well_formed_json_with_current_heap() {
        // The default/main heap always has at least one live allocation by the time any
        // Rust test runs (the runtime itself allocates), so a pages-only dump of the
        // current subprocess must come back non-empty and syntactically balanced.
        let json = heap_dump_json(false, false).expect("heap_dump_json should not fail");
        assert!(json.contains("\"heaps\""));
        assert!(!json.contains("\"blocks\""));
        let opens = json.matches('{').count();
        let closes = json.matches('}').count();
        assert_eq!(opens, closes);

        let with_blocks = heap_dump_json(true, true).expect("heap_dump_json should not fail");
        assert!(with_blocks.contains("\"blocks\""));
    }
}
