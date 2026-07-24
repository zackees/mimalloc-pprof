/* Opt-in global-heap allocation-change accounting and callbacks (issue #20).

   Independent of MI_PPROF: always compiled in (see src/static.c), gated only by the
   runtime `memevt_state` flag below (env var MIMALLOC_MEMORY_EVENTS, or the
   mi_memory_tracking_set_enabled API). Structurally this module mirrors src/profile.c's
   patterns (mi_atomic_do_once-style lazy env read, snapshot-then-release callback
   dispatch, a thread-local reentrancy depth counter) but is a separate, independent
   feature: see include/mimalloc/memory-events.h for the full API contract.

   Like the profiler, this module's own bookkeeping never uses mi_malloc: the callback
   table and counters below are static storage (no dynamic allocation at all), so there
   is nothing here that could recursively enter the hooked allocation paths. The public
   mi_unwrapped_* family (backed directly by _mi_os_alloc/_mi_os_free) is provided as a
   stable API for *callers* (e.g. a memory-change callback) that need non-recursive
   scratch storage; this module does not need to consume its own API for that purpose. */
#include "mimalloc.h"
#include "mimalloc/internal.h"
#include "mimalloc/prim.h"   // mi_prim_get_default_heap
#include <string.h>
#include <stdint.h>

// ---------------------------------------------------------------------------------------
// Activation state.
//
// State machine (single _Atomic(size_t), not a bool -- see profile.c's prof_enabled
// comment on MSVC's plain-C atomic wrapper only implementing uintptr_t/int64_t widths):
//   MEMEVT_UNINIT   (0): never resolved; the disabled-hot-path check below treats this
//                        the same as "maybe active" and falls into the slow path once.
//   MEMEVT_DISABLED (1): resolved off, by env or by explicit API call. Steady-state
//                        common case: the hot-path check below is exactly one relaxed
//                        atomic load + compare, no lock, no callback-table touch.
//   MEMEVT_ENABLED  (2): resolved on, by env or by explicit API call.
//
// A shared mi_atomic_once_t (memevt_once) synchronizes the two ways this can first
// resolve -- the lazy env read at the first allocation hook, and an explicit
// mi_memory_tracking_set_enabled call, which may happen before any allocation at all.
// Whichever runs first "wins" the once (so the other is a no-op for the *env read*);
// but mi_memory_tracking_set_enabled always writes memevt_state directly afterwards too,
// so an explicit call remains authoritative even if it runs after the once already
// resolved via the lazy env path -- matching "tracking may also be enabled/disabled by
// API" as an override, not just a fallback.
// ---------------------------------------------------------------------------------------
#define MEMEVT_UNINIT    0
#define MEMEVT_DISABLED  1
#define MEMEVT_ENABLED   2

static _Atomic(size_t) memevt_state;
static mi_atomic_once_t memevt_once = { MI_ATOMIC_VAR_INIT(0), MI_LOCK_INITIALIZER };

// Counters (see mi_memory_snapshot_t). Maintained only while MEMEVT_ENABLED; never
// reset on disable/re-enable (the running totals just stop advancing while disabled --
// this is the documented "partial accounting" caveat in memory-events.h).
static _Atomic(size_t) memevt_live_bytes;
static _Atomic(size_t) memevt_accum_bytes;
static _Atomic(size_t) memevt_live_count;
static _Atomic(size_t) memevt_accum_count;

// Callback table + its lock. The lock only ever guards a snapshot-copy of the table
// (mi_memory_set_callbacks writing it, or memevt_dispatch reading one entry out of it);
// it is never held while a user handler runs.
static mi_lock_t memevt_cb_lock = MI_LOCK_INITIALIZER;
static mi_memory_change_fun* memevt_handlers[MI_MEMORY_CHANGE_COUNT];
static void*                 memevt_args[MI_MEMORY_CHANGE_COUNT];

// Reentrancy / internal-op suppression (mirrors profile.c's prof_callback_depth).
// >0 means: skip accounting and skip dispatch entirely. Two callers increment this:
//   (a) memevt_dispatch, around invoking the user's handler -- so if the handler itself
//       calls mi_malloc/mi_free, that nested allocation is not itself accounted for or
//       dispatched (bounds recursion depth; see memory-events.h's callback contract).
//   (b) _mi_heap_realloc_zero's moving-realloc path, around its internal
//       mi_heap_umalloc+mi_free pair, so those two calls don't leak an ALLOCATE/FREE
//       pair to consumers; the caller then explicitly calls _mi_memevt_on_resize once,
//       after suppression is lifted, to emit the single synthesized RESIZE.
static mi_decl_thread int memevt_suppress_depth;

void _mi_memevt_suppress_begin(void) { memevt_suppress_depth++; }
void _mi_memevt_suppress_end(void)   { memevt_suppress_depth--; }

// ---------------------------------------------------------------------------------------
// Lazy activation.
// ---------------------------------------------------------------------------------------

static void memevt_resolve_env(void) {
  if (_mi_atomic_once_enter(&memevt_once)) {
    const bool enabled = mi_option_is_enabled(mi_option_memory_events);
    mi_atomic_store_release(&memevt_state, (size_t)(enabled ? MEMEVT_ENABLED : MEMEVT_DISABLED));
    _mi_atomic_once_release(&memevt_once);
  }
  // else: either a concurrent thread is mid-resolution (we blocked until it finished, in
  // which case memevt_state now holds its result) or an explicit API call already won
  // the once before any allocation occurred (memevt_state already holds that value).
}

bool mi_memory_tracking_set_enabled(bool enabled) mi_attr_noexcept {
  const size_t new_state = (size_t)(enabled ? MEMEVT_ENABLED : MEMEVT_DISABLED);
  if (_mi_atomic_once_enter(&memevt_once)) {
    // First-ever activation path, and it is this explicit call: resolve the once
    // without ever reading the environment, so a later first-allocation lazy read is
    // permanently skipped (memevt_resolve_env's `else` branch above).
    mi_atomic_store_release(&memevt_state, new_state);
    _mi_atomic_once_release(&memevt_once);
  }
  else {
    // Once already resolved (by a prior lazy env read or a prior API call): an explicit
    // call always overrides the cached flag, matching "tracking may also be enabled or
    // disabled by API" as an authoritative override, not merely a fallback default.
    mi_atomic_store_release(&memevt_state, new_state);
  }
  return true;
}

bool mi_memory_tracking_is_enabled(void) mi_attr_noexcept {
  return (mi_atomic_load_relaxed(&memevt_state) == MEMEVT_ENABLED);
}

// ---------------------------------------------------------------------------------------
// Callback table.
// ---------------------------------------------------------------------------------------

bool mi_memory_set_callbacks(const mi_memory_callbacks_t* callbacks) mi_attr_noexcept {
  mi_lock_acquire(&memevt_cb_lock);
  for (int i = 0; i < MI_MEMORY_CHANGE_COUNT; i++) {
    memevt_handlers[i] = (callbacks != NULL ? callbacks->handlers[i] : NULL);
    memevt_args[i]     = (callbacks != NULL ? callbacks->args[i]     : NULL);
  }
  mi_lock_release(&memevt_cb_lock);
  return true;
}

bool mi_memory_snapshot(mi_memory_snapshot_t* out) mi_attr_noexcept {
  if (out == NULL) return false;
  if (out->size != sizeof(mi_memory_snapshot_t) || out->version != MI_MEMORY_SNAPSHOT_VERSION) return false;
  out->live_bytes  = (uint64_t)mi_atomic_load_relaxed(&memevt_live_bytes);
  out->accum_bytes = (uint64_t)mi_atomic_load_relaxed(&memevt_accum_bytes);
  out->live_count  = (uint64_t)mi_atomic_load_relaxed(&memevt_live_count);
  out->accum_count = (uint64_t)mi_atomic_load_relaxed(&memevt_accum_count);
  return true;
}

// ---------------------------------------------------------------------------------------
// Dispatch. Called only once tracking is confirmed MEMEVT_ENABLED. Updates counters
// (total_bytes-affecting update happens before the callback, per spec), then snapshots
// the relevant handler/arg pair under memevt_cb_lock, releases the lock, and only then
// invokes the handler -- so the handler runs with neither memevt_cb_lock nor any
// mimalloc allocator lock held (the latter is already guaranteed by hook placement: see
// the call sites in alloc.c/free.c, all positioned after the corresponding page-local
// work / list push is already complete).
// ---------------------------------------------------------------------------------------

static void memevt_dispatch(mi_memory_change_kind_t kind, int64_t delta_bytes, uint64_t request_size) {
  if (memevt_suppress_depth > 0) return;

  size_t live_bytes_after;
  if (delta_bytes >= 0) {
    live_bytes_after = mi_atomic_add_relaxed(&memevt_live_bytes, (size_t)delta_bytes) + (size_t)delta_bytes;
  }
  else {
    const size_t magnitude = (size_t)(-delta_bytes);
    live_bytes_after = mi_atomic_sub_relaxed(&memevt_live_bytes, magnitude) - magnitude;
  }

  switch (kind) {
    case MI_MEMORY_ALLOCATE:
      mi_atomic_increment_relaxed(&memevt_live_count);
      mi_atomic_increment_relaxed(&memevt_accum_count);
      if (delta_bytes > 0) mi_atomic_add_relaxed(&memevt_accum_bytes, (size_t)delta_bytes);
      break;
    case MI_MEMORY_FREE:
      mi_atomic_decrement_relaxed(&memevt_live_count);
      break;
    case MI_MEMORY_RESIZE:
      if (delta_bytes > 0) mi_atomic_add_relaxed(&memevt_accum_bytes, (size_t)delta_bytes);
      break;
    default:
      break;
  }

  mi_lock_acquire(&memevt_cb_lock);
  mi_memory_change_fun* handler = memevt_handlers[kind];
  void* handler_arg = memevt_args[kind];
  mi_lock_release(&memevt_cb_lock);
  if (handler == NULL) return;

  mi_memory_change_t change;
  change.kind = kind;
  change.total_bytes = (uint64_t)live_bytes_after;
  change.delta_bytes = delta_bytes;
  change.request_size = request_size;

  memevt_suppress_depth++;
  handler(&change, handler_arg);
  memevt_suppress_depth--;
}

// ---------------------------------------------------------------------------------------
// Hook entry points. Each begins with the single disabled-hot-path flag check: a plain
// relaxed load compared against MEMEVT_DISABLED. Only when that check is *not* true
// (either MEMEVT_UNINIT -- resolved once, here -- or MEMEVT_ENABLED) does any further
// work happen; no accounting atomic and no callback-table lock/lookup occur on the
// disabled path.
// ---------------------------------------------------------------------------------------

void _mi_memevt_on_alloc(mi_page_t* page, void* p, size_t request_size) {
  MI_UNUSED(p);
  size_t state = mi_atomic_load_relaxed(&memevt_state);
  if mi_likely(state == MEMEVT_DISABLED) return;
  if (state == MEMEVT_UNINIT) { memevt_resolve_env(); state = mi_atomic_load_relaxed(&memevt_state); }
  if (state != MEMEVT_ENABLED) return;
  const size_t usable = mi_page_usable_block_size(page);
  memevt_dispatch(MI_MEMORY_ALLOCATE, (int64_t)usable, (uint64_t)request_size);
}

void _mi_memevt_on_free(mi_page_t* page, void* p) {
  MI_UNUSED(p);
  const size_t state = mi_atomic_load_relaxed(&memevt_state);
  if mi_likely(state == MEMEVT_DISABLED) return;
  if (state != MEMEVT_ENABLED) return; // MEMEVT_UNINIT: nothing can have been counted yet, nothing to free-account.
  const size_t usable = mi_page_usable_block_size(page);
  memevt_dispatch(MI_MEMORY_FREE, -(int64_t)usable, 0);
}

void _mi_memevt_on_realloc_in_place(mi_page_t* page, void* p, size_t request_size) {
  MI_UNUSED(p);
  const size_t state = mi_atomic_load_relaxed(&memevt_state);
  if mi_likely(state == MEMEVT_DISABLED) return;
  if (state != MEMEVT_ENABLED) return;
  // Same page => same block-size class => usable size is identical before and after;
  // delta is always exactly 0 (still a real, dispatched RESIZE event, per spec).
  MI_UNUSED(page);
  memevt_dispatch(MI_MEMORY_RESIZE, 0, (uint64_t)request_size);
}

void _mi_memevt_on_resize(size_t usable_pre, size_t usable_post, size_t request_size) {
  const size_t state = mi_atomic_load_relaxed(&memevt_state);
  if mi_likely(state == MEMEVT_DISABLED) return;
  if (state != MEMEVT_ENABLED) return;
  const int64_t delta = (int64_t)usable_post - (int64_t)usable_pre;
  memevt_dispatch(MI_MEMORY_RESIZE, delta, (uint64_t)request_size);
}

// ---------------------------------------------------------------------------------------
// Best-effort live-allocation visitor. Built on the existing per-heap block-visitation
// facility (mi_heap_visit_blocks / mi_block_visit_fun), per the issue's explicit
// instruction to use that as the basis rather than new page-walking logic.
//
// Scope note (deviation from a literal "global" reading -- see final report): this
// walks every heap on the *calling thread* (mi_tld_t::heaps, the same list the runtime
// itself uses to abandon all of a thread's heaps on thread exit). mimalloc keeps no
// process-wide registry of every thread's heaps (unlike the segment/arena layer, heaps
// are pure thread-local structures with no cross-thread linkage), and building one would
// mean new cross-thread bookkeeping infrastructure -- out of scope for "the existing
// per-heap visitation facilities are the basis". The API is already documented as
// best-effort/non-consistent, and single-threaded or per-thread-tracked callers (the
// common case for this kind of diagnostic) get full coverage; multi-threaded callers get
// their own thread's live allocations only.
// ---------------------------------------------------------------------------------------

typedef struct memevt_visit_ctx_s {
  mi_memory_allocation_visit_fun* visitor;
  void* arg;
} memevt_visit_ctx_t;

static bool mi_cdecl memevt_visit_adapter(const mi_heap_t* heap, const mi_heap_area_t* area, void* block, size_t block_size, void* arg) {
  MI_UNUSED(heap); MI_UNUSED(area);
  if (block == NULL) return true; // area-only callback; visit_blocks=true below still yields one of these per area too.
  memevt_visit_ctx_t* ctx = (memevt_visit_ctx_t*)arg;
  return ctx->visitor(block, block_size, ctx->arg);
}

bool mi_memory_visit_live_allocations(mi_memory_allocation_visit_fun* visitor, void* arg) mi_attr_noexcept {
  if (visitor == NULL) return false;
  if (memevt_suppress_depth > 0) return false; // do not reenter while a callback/internal-op is in flight on this thread.
  mi_heap_t* heap = mi_prim_get_default_heap();
  if (heap == NULL || !mi_heap_is_initialized(heap)) return true; // nothing to visit yet on this thread.
  memevt_visit_ctx_t ctx = { visitor, arg };
  for (mi_heap_t* h = heap->tld->heaps; h != NULL; h = h->next) {
    if (!mi_heap_visit_blocks(h, true /* visit_blocks */, &memevt_visit_adapter, &ctx)) break;
  }
  return true;
}

// ---------------------------------------------------------------------------------------
// Stable public "unwrapped" instrumentation allocation path: backed directly by
// _mi_os_alloc/_mi_os_free (never mi_malloc), page granular, with a small prepended
// header carrying the mi_memid_t provenance token _mi_os_free requires. This mirrors
// _mi_prof_arena_alloc's chunk-header shape in profile.c, but each allocation here owns
// its own OS mapping (paired release, not an arena) since these are meant to be
// individually freed/resized by instrumentation callers, not bump-allocated bookkeeping.
// ---------------------------------------------------------------------------------------

#define MI_UNWRAPPED_MAGIC ((uint32_t)0x6D697577) /* "miuw" */

typedef struct mi_unwrapped_header_s {
  void*      base;          // OS allocation base, for _mi_os_free
  size_t     total_size;    // OS allocation total size, for _mi_os_free
  size_t     payload_size;  // current payload size, for realloc's memcpy
  mi_memid_t memid;         // provenance token, for _mi_os_free
  uint32_t   magic;
} mi_unwrapped_header_t;

static size_t memevt_align_up(size_t sz, size_t alignment) {
  return (sz + (alignment - 1)) & ~(alignment - 1);
}

void* mi_unwrapped_malloc(size_t size, size_t alignment) mi_attr_noexcept {
  if (alignment == 0) alignment = sizeof(void*);
  if ((alignment & (alignment - 1)) != 0) return NULL; // must be a power of two
  const size_t hdr_reserved = memevt_align_up(sizeof(mi_unwrapped_header_t), alignment);
  if (size > SIZE_MAX - hdr_reserved) return NULL; // overflow guard
  const size_t total = hdr_reserved + size;
  mi_memid_t memid;
  uint8_t* base = (uint8_t*)_mi_os_alloc_aligned(total, alignment, true /* commit */, false /* allow_large */, &memid);
  if (base == NULL) return NULL;
  uint8_t* user = base + hdr_reserved;
  mi_unwrapped_header_t* hdr = (mi_unwrapped_header_t*)(user - sizeof(mi_unwrapped_header_t));
  hdr->base = base;
  hdr->total_size = total;
  hdr->payload_size = size;
  hdr->memid = memid;
  hdr->magic = MI_UNWRAPPED_MAGIC;
  return user;
}

static mi_unwrapped_header_t* mi_unwrapped_header_of(void* p, const char* msg) {
  mi_unwrapped_header_t* hdr = (mi_unwrapped_header_t*)((uint8_t*)p - sizeof(mi_unwrapped_header_t));
  if (hdr->magic != MI_UNWRAPPED_MAGIC) {
    _mi_error_message(EINVAL, "%s: pointer %p was not returned by mi_unwrapped_malloc/realloc\n", msg, p);
    return NULL;
  }
  return hdr;
}

void mi_unwrapped_free(void* p) mi_attr_noexcept {
  if (p == NULL) return;
  mi_unwrapped_header_t* hdr = mi_unwrapped_header_of(p, "mi_unwrapped_free");
  if (hdr == NULL) return;
  _mi_os_free(hdr->base, hdr->total_size, hdr->memid);
}

void* mi_unwrapped_realloc(void* p, size_t new_size, size_t alignment) mi_attr_noexcept {
  if (p == NULL) return mi_unwrapped_malloc(new_size, alignment);
  if (new_size == 0) { mi_unwrapped_free(p); return NULL; }
  mi_unwrapped_header_t* hdr = mi_unwrapped_header_of(p, "mi_unwrapped_realloc");
  if (hdr == NULL) return NULL;
  void* newp = mi_unwrapped_malloc(new_size, alignment);
  if (newp == NULL) return NULL;
  const size_t copy = (hdr->payload_size < new_size ? hdr->payload_size : new_size);
  _mi_memcpy(newp, p, copy);
  mi_unwrapped_free(p);
  return newp;
}
