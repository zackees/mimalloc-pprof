/* Public, allocation-change accounting/monitoring API. Independent of MI_PPROF: this
   module (src/memory-events.c) is always compiled in, opt-in only via the
   `MIMALLOC_MEMORY_EVENTS` environment variable or the `mi_memory_tracking_set_enabled`
   API below.

   ## Activation

   - `MIMALLOC_MEMORY_EVENTS=1` is read lazily, exactly once, the first time any
     allocation/free/realloc hook runs -- never during process startup. The result is
     cached; later allocator operations never re-read the environment.
   - `mi_memory_tracking_set_enabled` can also enable/disable tracking at any time,
     including before the first allocation. An explicit API call is always authoritative:
     if it runs before the first allocation, the later lazy environment read is skipped
     entirely; if it runs after, it simply overrides the cached flag.
   - Disabling tracking stops all accounting immediately. Re-enabling does NOT
     reconstruct allocations made while tracking was disabled -- the running totals
     silently omit that interval. Callers that need an exact total must enable tracking
     before their first allocation and leave it enabled for the life of the process.
   - When tracking is disabled (the default), every allocate/free/realloc pays for
     exactly one relaxed flag check on the hot path -- no atomic accounting update, no
     callback-table lock, no callback-table lookup.

   ## Callback contract

   - Callbacks are invoked with no mimalloc allocator locks held, and may themselves
     call `mi_malloc`/`mi_free`/etc. without deadlocking (the callback-table lock is
     acquired only to snapshot the handler pointer, then released before the handler
     runs). Callbacks must still be short and non-blocking.
   - A memory-change hook invoked while another hook's callback is already running on
     the same thread (including as a side effect of that callback allocating/freeing)
     is suppressed: no accounting update and no nested callback invocation. This bounds
     stack depth and avoids double-counting/re-entrant surprises; it also means bytes
     allocated or freed *from inside* a callback are not reflected in the running totals.
   - `arg` pointers in `mi_memory_callbacks_t` are caller-owned: the caller must keep
     them valid for as long as the callback might still be invoked (i.e. until a
     subsequent `mi_memory_set_callbacks` call replaces/clears them, or tracking is
     permanently torn down at process exit).
*/
#pragma once
#ifndef MIMALLOC_MEMORY_EVENTS_H
#define MIMALLOC_MEMORY_EVENTS_H

#include "mimalloc.h"
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum mi_memory_change_kind_e {
  MI_MEMORY_ALLOCATE,
  MI_MEMORY_FREE,
  MI_MEMORY_RESIZE,
  MI_MEMORY_CHANGE_COUNT,
} mi_memory_change_kind_t;

typedef struct mi_memory_change_s {
  // Successful allocation, free, or realloc.
  mi_memory_change_kind_t kind;

  // Tracked global live usable bytes after this operation.
  uint64_t total_bytes;

  // Signed change in tracked live usable bytes caused by this operation.
  // Positive for allocation/growth; negative for free/shrink; zero for a
  // same-size-class resize.
  int64_t delta_bytes;

  // Caller-requested size for allocation and resize; zero for free.
  uint64_t request_size;
} mi_memory_change_t;

typedef void (mi_memory_change_fun)(
  const mi_memory_change_t* change,
  void* arg
);

typedef struct mi_memory_callbacks_s {
  mi_memory_change_fun* handlers[MI_MEMORY_CHANGE_COUNT];
  void*                 args[MI_MEMORY_CHANGE_COUNT];
} mi_memory_callbacks_t;

mi_decl_export bool mi_memory_tracking_set_enabled(bool enabled) mi_attr_noexcept;
mi_decl_nodiscard mi_decl_export bool mi_memory_tracking_is_enabled(void) mi_attr_noexcept;

// Installs, replaces, or (with callbacks == NULL) clears the callback table.
// Safe to call at any time; synchronized against concurrent dispatch.
mi_decl_export bool mi_memory_set_callbacks(const mi_memory_callbacks_t* callbacks) mi_attr_noexcept;

// Versioned/sized struct, following this codebase's mi_prof_stats_t idiom, so future
// fields can be added without breaking already-compiled callers (see
// mi_memory_snapshot_t_decl below). All counters are maintained only while tracking is
// enabled: they are not reconstructed for time spent disabled.
#define MI_MEMORY_SNAPSHOT_VERSION 1
typedef struct mi_memory_snapshot_s {
  size_t   size; int version;
  uint64_t live_bytes;         // tracked live usable bytes right now
  uint64_t accum_bytes;        // cumulative usable bytes ever allocated (grows on ALLOCATE and on growing RESIZE)
  uint64_t live_count;         // tracked live allocation count right now
  uint64_t accum_count;        // cumulative count of successful ALLOCATE events
} mi_memory_snapshot_t;
#define mi_memory_snapshot_t_decl(name) mi_memory_snapshot_t name = { 0 }; name.size = sizeof(mi_memory_snapshot_t); name.version = MI_MEMORY_SNAPSHOT_VERSION

mi_decl_nodiscard mi_decl_export bool mi_memory_snapshot(mi_memory_snapshot_t* out) mi_attr_noexcept;

/* ---------------------------------------------------------------------------------------------
   Best-effort live-allocation visitor (diagnostics only; not a consistent global snapshot).
   Built on top of this codebase's existing per-heap block-visitation facility
   (mi_heap_visit_blocks); see memory-events.c for the exact scope this walks. */

// Return false to stop the visit early.
typedef bool (mi_memory_allocation_visit_fun)(
  void*  allocation,   // Address of a live allocation observed during the walk.
  size_t usable_size,  // Its usable mimalloc size at the moment it was observed.
  void*  arg           // Caller-owned visit context.
);

// Best effort, not a consistent global snapshot: another thread may free `allocation`
// immediately after the callback begins. Do not dereference, retain, or free it. The
// visitor must not allocate, free, or reenter mimalloc while the walk is active.
mi_decl_export bool mi_memory_visit_live_allocations(
  mi_memory_allocation_visit_fun* visitor,
  void* arg
) mi_attr_noexcept;

/* ---------------------------------------------------------------------------------------------
   Stable public "unwrapped" instrumentation allocation path: backed directly by the raw
   OS layer (_mi_os_alloc / _mi_os_free), never by the hooked mi_malloc family. Page
   granular. Pointers returned here are only valid for mi_unwrapped_free/realloc -- never
   pass them to mi_free or vice versa. Excluded from normal mimalloc allocation stats and
   from the memory-change accounting above. Intended for low-level instrumentation and
   recursion avoidance (e.g. a memory-change callback that needs scratch storage without
   recursively entering mimalloc). */

mi_decl_export void* mi_unwrapped_malloc(size_t size, size_t alignment) mi_attr_noexcept;
mi_decl_export void  mi_unwrapped_free(void* p) mi_attr_noexcept;
mi_decl_export void* mi_unwrapped_realloc(void* p, size_t new_size, size_t alignment) mi_attr_noexcept;

#ifdef __cplusplus
}
#endif
#endif
