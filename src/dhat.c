/* Exact, opt-in DHAT v2 heap/lifetime profiler (issue #238).
   All persistent collector state is bump allocated directly from the raw OS layer;
   it never enters the normal mimalloc allocation paths. */
#include "mimalloc.h"
#include "mimalloc/internal.h"
#include "mimalloc/dhat.h"
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <limits.h>
#include <stddef.h>

#define DHAT_UNINIT 0
#define DHAT_DISABLED 1
#define DHAT_ENABLED 2
#define DHAT_STACK_MAX 64
#define DHAT_CHUNK_SIZE (64*1024)
#define DHAT_DEFAULT_BUDGET (64*1024*1024)
#define DHAT_BUCKETS 4096

typedef struct dhat_chunk_s {
  struct dhat_chunk_s* next;
  mi_memid_t memid;
  size_t size;
  size_t used;
} dhat_chunk_t;

typedef struct dhat_pp_s {
  struct dhat_pp_s* next;
  uint64_t hash;
  uint32_t depth;
  uint64_t tb, tbk, tl;
  uint64_t live, livek, mb, mbk, gb, gbk;
  void* pcs[];
} dhat_pp_t;

typedef struct dhat_record_s {
  struct dhat_record_s* next;
  void* ptr;
  size_t size;
  uint64_t born;
  dhat_pp_t* pp;
} dhat_record_t;

typedef enum dhat_event_kind_e { DHAT_EVENT_NONE, DHAT_EVENT_ALLOC, DHAT_EVENT_FREE, DHAT_EVENT_RESIZE } dhat_event_kind_t;
typedef struct dhat_event_s {
  dhat_event_kind_t kind;
  void* oldp;
  void* newp;
  size_t size;
  uint64_t at;
  dhat_pp_t* pp;
  bool armed;
} dhat_event_t;

static mi_lock_t dhat_lock = MI_LOCK_INITIALIZER;
static mi_atomic_once_t dhat_once = { MI_ATOMIC_VAR_INIT(0), MI_LOCK_INITIALIZER };
static _Atomic(size_t) dhat_state;
static dhat_chunk_t* dhat_chunks;
static dhat_record_t** dhat_live_table;
static dhat_pp_t** dhat_pp_table;
static size_t dhat_budget;
static _Atomic(size_t) dhat_internal_bytes;
static _Atomic(size_t) dhat_dropped;
static _Atomic(size_t) dhat_incomplete;
static mi_msecs_t dhat_started;
static uint64_t dhat_total_bytes, dhat_total_blocks;
static uint64_t dhat_live_bytes, dhat_live_blocks, dhat_peak_bytes, dhat_peak_blocks, dhat_peak_at;
static char dhat_dump_at_exit[1024];
static mi_decl_thread int dhat_observer_depth;
static mi_decl_thread dhat_event_t dhat_event;

size_t _mi_dhat_stack_capture(void** pcs, size_t capacity);

static uint64_t dhat_hash_ptr(const void* p) {
  uintptr_t x = (uintptr_t)p;
  x ^= x >> 33; x *= UINT64_C(0xff51afd7ed558ccd); x ^= x >> 33;
  return (uint64_t)x;
}
static uint64_t dhat_hash_stack(void* const* pcs, size_t depth) {
  uint64_t h = UINT64_C(1469598103934665603);
  for (size_t i = 0; i < depth; i++) {
    uintptr_t pc = (uintptr_t)pcs[i];
    for (size_t b = 0; b < sizeof(pc); b++) { h ^= (uint8_t)(pc >> (b * 8)); h *= UINT64_C(1099511628211); }
  }
  return h;
}
static bool dhat_stack_equal(const dhat_pp_t* pp, uint64_t hash, void* const* pcs, size_t depth) {
  if (pp->hash != hash || pp->depth != depth) return false;
  for (size_t i = 0; i < depth; i++) if (pp->pcs[i] != pcs[i]) return false;
  return true;
}
static size_t dhat_align(size_t n) { const size_t a = sizeof(void*) - 1; return (n + a) & ~a; }

static void dhat_release_locked(void) {
  for (dhat_chunk_t* chunk = dhat_chunks; chunk != NULL; ) {
    dhat_chunk_t* next = chunk->next;
    _mi_os_free(_mi_subproc_main(), chunk, chunk->size, chunk->memid);
    chunk = next;
  }
  dhat_chunks = NULL; dhat_live_table = NULL; dhat_pp_table = NULL;
  dhat_total_bytes = dhat_total_blocks = 0;
  dhat_live_bytes = dhat_live_blocks = dhat_peak_bytes = dhat_peak_blocks = dhat_peak_at = 0;
  mi_atomic_store_relaxed(&dhat_internal_bytes, (size_t)0);
  mi_atomic_store_relaxed(&dhat_dropped, (size_t)0);
  mi_atomic_store_relaxed(&dhat_incomplete, (size_t)0);
}

static void* dhat_arena_alloc(size_t size) {
  size = dhat_align(size);
  dhat_chunk_t* chunk = dhat_chunks;
  if (chunk == NULL || chunk->used + size > chunk->size) {
    if (size > SIZE_MAX - sizeof(dhat_chunk_t)) return NULL;
    size_t total = DHAT_CHUNK_SIZE;
    if (total < sizeof(dhat_chunk_t) + size) total = sizeof(dhat_chunk_t) + size;
    const size_t committed = mi_atomic_load_relaxed(&dhat_internal_bytes);
    if (dhat_budget != 0 && (committed > dhat_budget || total > dhat_budget - committed)) return NULL;
    mi_memid_t memid;
    chunk = (dhat_chunk_t*)_mi_os_alloc(_mi_subproc_main(), total, &memid);
    if (chunk == NULL) return NULL;
    chunk->next = dhat_chunks; chunk->memid = memid; chunk->size = total; chunk->used = sizeof(*chunk);
    dhat_chunks = chunk;
    mi_atomic_add_relaxed(&dhat_internal_bytes, total);
  }
  void* p = (uint8_t*)chunk + chunk->used;
  chunk->used += size;
  return p;
}

static void dhat_mark_dropped(void) { mi_atomic_increment_relaxed(&dhat_dropped); mi_atomic_store_relaxed(&dhat_incomplete, (size_t)1); }

static bool dhat_init_tables_locked(void) {
  if (dhat_live_table != NULL || dhat_pp_table != NULL) {
    /* A partial first attempt cannot safely be completed: the budget may have
       been exhausted after one table allocation. Keep the collector fail-soft
       instead of dereferencing a missing sibling table. */
    return (dhat_live_table != NULL && dhat_pp_table != NULL);
  }
  dhat_live_table = (dhat_record_t**)dhat_arena_alloc(DHAT_BUCKETS * sizeof(*dhat_live_table));
  dhat_pp_table = (dhat_pp_t**)dhat_arena_alloc(DHAT_BUCKETS * sizeof(*dhat_pp_table));
  if (dhat_live_table == NULL || dhat_pp_table == NULL) { dhat_mark_dropped(); return false; }
  _mi_memzero(dhat_live_table, DHAT_BUCKETS * sizeof(*dhat_live_table));
  _mi_memzero(dhat_pp_table, DHAT_BUCKETS * sizeof(*dhat_pp_table));
  return true;
}

static dhat_pp_t* dhat_pp_intern_locked(void* const* pcs, size_t depth) {
  if (!dhat_init_tables_locked()) return NULL;
  const uint64_t hash = dhat_hash_stack(pcs, depth);
  dhat_pp_t** bucket = &dhat_pp_table[(size_t)hash & (DHAT_BUCKETS - 1)];
  for (dhat_pp_t* pp = *bucket; pp != NULL; pp = pp->next) if (dhat_stack_equal(pp, hash, pcs, depth)) return pp;
  if (depth > (SIZE_MAX - sizeof(dhat_pp_t)) / sizeof(void*)) { dhat_mark_dropped(); return NULL; }
  dhat_pp_t* pp = (dhat_pp_t*)dhat_arena_alloc(sizeof(*pp) + depth * sizeof(void*));
  if (pp == NULL) { dhat_mark_dropped(); return NULL; }
  _mi_memzero(pp, sizeof(*pp)); pp->hash = hash; pp->depth = (uint32_t)depth;
  for (size_t i = 0; i < depth; i++) pp->pcs[i] = pcs[i];
  pp->next = *bucket; *bucket = pp;
  return pp;
}

static dhat_record_t** dhat_record_slot_locked(void* p) {
  if (dhat_live_table == NULL) return NULL;
  dhat_record_t** slot = &dhat_live_table[(size_t)dhat_hash_ptr(p) & (DHAT_BUCKETS - 1)];
  while (*slot != NULL && (*slot)->ptr != p) slot = &(*slot)->next;
  return slot;
}
static uint64_t dhat_elapsed_now(void) {
  const mi_msecs_t now = _mi_clock_now();
  return (now >= dhat_started ? (uint64_t)(now - dhat_started) : 0);
}
static void dhat_snapshot_global_peak_locked(void) {
  if (dhat_live_bytes <= dhat_peak_bytes) return;
  dhat_peak_bytes = dhat_live_bytes; dhat_peak_blocks = dhat_live_blocks;
  dhat_peak_at = dhat_elapsed_now();
  for (size_t i = 0; i < DHAT_BUCKETS; i++) for (dhat_pp_t* pp = dhat_pp_table[i]; pp != NULL; pp = pp->next) { pp->gb = pp->live; pp->gbk = pp->livek; }
}
static void dhat_commit_alloc_locked(dhat_event_t* ev) {
  if (ev->pp == NULL || !dhat_init_tables_locked()) return;
  dhat_record_t** slot = dhat_record_slot_locked(ev->newp);
  if (slot == NULL) return;
  if (*slot != NULL) { dhat_mark_dropped(); return; }
  dhat_record_t* rec = (dhat_record_t*)dhat_arena_alloc(sizeof(*rec));
  if (rec == NULL) { dhat_mark_dropped(); return; }
  rec->next = NULL; rec->ptr = ev->newp; rec->size = ev->size; rec->born = ev->at; rec->pp = ev->pp; *slot = rec;
  ev->pp->tb += ev->size; ev->pp->tbk++; ev->pp->live += ev->size; ev->pp->livek++;
  if (ev->pp->live > ev->pp->mb) ev->pp->mb = ev->pp->live;
  if (ev->pp->livek > ev->pp->mbk) ev->pp->mbk = ev->pp->livek;
  dhat_total_bytes += ev->size; dhat_total_blocks++; dhat_live_bytes += ev->size; dhat_live_blocks++;
  dhat_snapshot_global_peak_locked();
}
static void dhat_commit_free_locked(void* p, uint64_t at) {
  dhat_record_t** slot = dhat_record_slot_locked(p);
  if (slot == NULL || *slot == NULL) return;
  dhat_record_t* rec = *slot; *slot = rec->next;
  dhat_pp_t* pp = rec->pp;
  if (pp->live >= rec->size) pp->live -= rec->size; else { pp->live = 0; dhat_mark_dropped(); }
  if (pp->livek != 0) pp->livek--; else dhat_mark_dropped();
  if (dhat_live_bytes >= rec->size) dhat_live_bytes -= rec->size; else { dhat_live_bytes = 0; dhat_mark_dropped(); }
  if (dhat_live_blocks != 0) dhat_live_blocks--; else dhat_mark_dropped();
  pp->tl += (at >= rec->born ? at - rec->born : 0);
}
static void dhat_commit_resize_locked(dhat_event_t* ev) {
  dhat_record_t** slot = dhat_record_slot_locked(ev->oldp);
  if (slot == NULL || *slot == NULL) return;
  dhat_record_t* rec = *slot;
  const size_t oldsize = rec->size;
  if (ev->oldp != ev->newp) {
    *slot = rec->next;
    dhat_record_t** destination = dhat_record_slot_locked(ev->newp);
    if (destination == NULL || *destination != NULL) { dhat_mark_dropped(); return; }
    rec->ptr = ev->newp; rec->next = NULL; *destination = rec;
  }
  rec->size = ev->size;
  dhat_pp_t* pp = rec->pp;
  if (ev->size >= oldsize) { const size_t delta = ev->size - oldsize; pp->tb += delta; dhat_total_bytes += delta; pp->live += delta; dhat_live_bytes += delta; }
  else { const size_t delta = oldsize - ev->size; pp->live -= delta; dhat_live_bytes -= delta; }
  if (pp->live > pp->mb) pp->mb = pp->live;
  dhat_snapshot_global_peak_locked();
}

static bool dhat_env_size(const char* name, size_t* out) {
  char buf[64];
  if (_mi_getenv(name, buf, sizeof(buf)) != 0 || buf[0] == 0) return false;
  char* end = NULL;
  const unsigned long long v = strtoull(buf, &end, 10);
  if (end == buf || *end != 0 || v > SIZE_MAX) return false;
  *out = (size_t)v;
  return true;
}
static void dhat_resolve_env(void) {
  if (_mi_atomic_once_enter(&dhat_once)) {
    char value[8] = { 0 };
    /* DHAT has its own opt-in switch; it must never inherit the unrelated
       MIMALLOC_MEMORY_EVENTS activation state. */
    const bool env_enabled = (_mi_getenv("MIMALLOC_DHAT", value, sizeof(value)) == 0 && value[0] != 0 && value[0] != '0');
    (void)_mi_getenv("MIMALLOC_DHAT_DUMP_AT_EXIT", dhat_dump_at_exit, sizeof(dhat_dump_at_exit));
    mi_atomic_store_release(&dhat_state, (size_t)(env_enabled ? DHAT_ENABLED : DHAT_DISABLED));
    _mi_atomic_once_release(&dhat_once);
    if (env_enabled) { mi_lock_acquire(&dhat_lock); dhat_budget = DHAT_DEFAULT_BUDGET; (void)dhat_env_size("MIMALLOC_DHAT_MAX_BYTES", &dhat_budget); dhat_started = _mi_clock_now(); mi_lock_release(&dhat_lock); }
  }
}

bool _mi_dhat_is_active(void) { return mi_atomic_load_relaxed(&dhat_state) == DHAT_ENABLED; }
static void dhat_prepare(dhat_event_kind_t kind, void* oldp, void* newp, size_t size) {
  dhat_event.armed = false;
  size_t state = mi_atomic_load_relaxed(&dhat_state);
  if (state == DHAT_UNINIT) { dhat_resolve_env(); state = mi_atomic_load_relaxed(&dhat_state); }
  if (state != DHAT_ENABLED || dhat_observer_depth != 0) return;
  dhat_observer_depth++;
  dhat_event.kind = kind; dhat_event.oldp = oldp; dhat_event.newp = newp; dhat_event.size = size; dhat_event.at = _mi_clock_now(); dhat_event.pp = NULL;
  if (kind == DHAT_EVENT_ALLOC) {
    void* pcs[DHAT_STACK_MAX]; const size_t depth = _mi_dhat_stack_capture(pcs, DHAT_STACK_MAX);
    if (depth == 0) { dhat_mark_dropped(); dhat_observer_depth--; return; }
    mi_lock_acquire(&dhat_lock); dhat_event.pp = dhat_pp_intern_locked(pcs, depth); mi_lock_release(&dhat_lock);
    if (dhat_event.pp == NULL) { dhat_observer_depth--; return; }
  }
  dhat_event.armed = true;
}
void _mi_dhat_begin_alloc(void* p, size_t request_size) { dhat_prepare(DHAT_EVENT_ALLOC, NULL, p, request_size); }
void _mi_dhat_begin_free(void* p) { dhat_prepare(DHAT_EVENT_FREE, p, NULL, 0); }
void _mi_dhat_begin_resize(void* oldp, void* newp, size_t request_size) { dhat_prepare(DHAT_EVENT_RESIZE, oldp, newp, request_size); }
void _mi_dhat_finish_event(void) {
  if (!dhat_event.armed) return;
  dhat_event_t ev = dhat_event; dhat_event.armed = false; dhat_observer_depth--;
  /* The observer guard is deliberately popped before mutating the ledger: a user memory
     callback runs between begin and finish, and any allocations it makes are excluded. */
  mi_lock_acquire(&dhat_lock);
  if (_mi_dhat_is_active()) {
    if (ev.kind == DHAT_EVENT_ALLOC) dhat_commit_alloc_locked(&ev);
    else if (ev.kind == DHAT_EVENT_FREE) dhat_commit_free_locked(ev.oldp, ev.at);
    else if (ev.kind == DHAT_EVENT_RESIZE) dhat_commit_resize_locked(&ev);
  }
  mi_lock_release(&dhat_lock);
}

bool mi_dhat_start(void) mi_attr_noexcept {
  if (dhat_observer_depth != 0) return false;
  if (_mi_atomic_once_enter(&dhat_once)) _mi_atomic_once_release(&dhat_once);
  mi_lock_acquire(&dhat_lock);
  if (_mi_dhat_is_active()) { mi_lock_release(&dhat_lock); return false; }
  dhat_release_locked(); dhat_budget = DHAT_DEFAULT_BUDGET; (void)dhat_env_size("MIMALLOC_DHAT_MAX_BYTES", &dhat_budget); dhat_started = _mi_clock_now();
  mi_atomic_store_release(&dhat_state, DHAT_ENABLED);
  mi_lock_release(&dhat_lock); return true;
}
void mi_dhat_stop(void) mi_attr_noexcept { if (dhat_observer_depth == 0) mi_atomic_store_release(&dhat_state, DHAT_DISABLED); }
bool mi_dhat_is_enabled(void) mi_attr_noexcept { return _mi_dhat_is_active(); }
bool mi_dhat_stats_get(mi_dhat_stats_t* out) mi_attr_noexcept {
  if (out == NULL || out->size != sizeof(*out) || out->version != MI_DHAT_STATS_VERSION) return false;
  mi_lock_acquire(&dhat_lock);
  out->enabled = _mi_dhat_is_active(); out->incomplete = mi_atomic_load_relaxed(&dhat_incomplete) != 0;
  out->total_bytes = dhat_total_bytes; out->total_blocks = dhat_total_blocks; out->live_bytes = dhat_live_bytes; out->live_blocks = dhat_live_blocks;
  out->peak_bytes = dhat_peak_bytes; out->peak_blocks = dhat_peak_blocks; out->dropped = mi_atomic_load_relaxed(&dhat_dropped); out->internal_bytes = mi_atomic_load_relaxed(&dhat_internal_bytes);
  mi_lock_release(&dhat_lock); return true;
}

/* Frame-table order is the order we visit program points below.  The dump is a
   diagnostic operation, so this allocation-free O(frames^3) lookup is preferable to
   adding a second persistent hash table solely for serialization. */
static bool dhat_frame_seen_before_locked(const dhat_pp_t* stop_pp, size_t stop_frame, const void* pc) {
  for (size_t i = 0; i < DHAT_BUCKETS; i++) {
    for (dhat_pp_t* pp = dhat_pp_table[i]; pp != NULL; pp = pp->next) {
      const size_t limit = (pp == stop_pp ? stop_frame : pp->depth);
      for (size_t frame = 0; frame < limit; frame++) if (pp->pcs[frame] == pc) return true;
      if (pp == stop_pp) return false;
    }
  }
  return false;
}
static size_t dhat_frame_index_locked(const dhat_pp_t* wanted, size_t wanted_frame) {
  size_t index = 0;
  for (size_t i = 0; i < DHAT_BUCKETS; i++) for (dhat_pp_t* pp = dhat_pp_table[i]; pp != NULL; pp = pp->next) {
    for (size_t frame = 0; frame < pp->depth; frame++) {
      if (pp == wanted && frame == wanted_frame) {
        if (!dhat_frame_seen_before_locked(pp, frame, pp->pcs[frame])) return index;
        /* Locate the matching first occurrence, whose index is the count of unique
           frames that preceded it. */
        for (size_t j = 0; j < DHAT_BUCKETS; j++) for (dhat_pp_t* prior = dhat_pp_table[j]; prior != NULL; prior = prior->next) {
          const size_t limit = (prior == pp ? frame : prior->depth);
          for (size_t pf = 0; pf < limit; pf++) if (prior->pcs[pf] == pp->pcs[frame]) return dhat_frame_index_locked(prior, pf);
        }
      }
      else if (!dhat_frame_seen_before_locked(pp, frame, pp->pcs[frame])) index++;
    }
  }
  return 0;
}
static void dhat_write_json_locked(FILE* f) {
  const uint64_t elapsed = dhat_elapsed_now();
  /* The standard viewer accepts producer-specific mode strings and extra keys.
     Keep the required v2 fields conventional; Mtu and tu truthfully say that
     these are monotonic wall-clock milliseconds rather than Valgrind instructions. */
  fprintf(f, "{\n  \"dhatFileVersion\": 2,\n  \"mode\": \"mimalloc-heap\",\n  \"verb\": \"Allocated\",\n  \"bklt\": true,\n  \"bkacc\": false,\n  \"tu\": \"ms\",\n  \"Mtu\": \"ms\",\n  \"tuth\": 1,\n  \"cmd\": \"\",\n  \"pid\": 0,\n  \"tg\": %llu,\n  \"te\": %llu,\n  \"mi_dhat_incomplete\": %s,\n  \"pps\": [\n", (unsigned long long)dhat_peak_at, (unsigned long long)elapsed, (mi_atomic_load_relaxed(&dhat_incomplete) ? "true" : "false"));
  bool first_pp = true;
  for (size_t i = 0; i < DHAT_BUCKETS; i++) for (dhat_pp_t* pp = dhat_pp_table[i]; pp != NULL; pp = pp->next) {
    fprintf(f, "%s    {\"tb\": %llu, \"tbk\": %llu, \"tl\": %llu, \"mb\": %llu, \"mbk\": %llu, \"gb\": %llu, \"gbk\": %llu, \"eb\": %llu, \"ebk\": %llu, \"fs\": [", first_pp ? "" : ",\n", (unsigned long long)pp->tb, (unsigned long long)pp->tbk, (unsigned long long)pp->tl, (unsigned long long)pp->mb, (unsigned long long)pp->mbk, (unsigned long long)pp->gb, (unsigned long long)pp->gbk, (unsigned long long)pp->live, (unsigned long long)pp->livek);
    for (size_t frame = 0; frame < pp->depth; frame++) fprintf(f, "%s%llu", frame == 0 ? "" : ", ", (unsigned long long)dhat_frame_index_locked(pp, frame));
    fprintf(f, "]}"); first_pp = false;
  }
  fprintf(f, "\n  ],\n  \"ftbl\": [");
  bool first_frame = true;
  for (size_t i = 0; i < DHAT_BUCKETS; i++) for (dhat_pp_t* pp = dhat_pp_table[i]; pp != NULL; pp = pp->next) for (size_t frame = 0; frame < pp->depth; frame++) {
    bool seen = false; for (size_t j = 0; j <= i && !seen; j++) for (dhat_pp_t* prior = dhat_pp_table[j]; prior != NULL && !seen; prior = prior->next) { const size_t lim = (prior == pp ? frame : prior->depth); for (size_t pf = 0; pf < lim; pf++) if (prior->pcs[pf] == pp->pcs[frame]) { seen = true; break; } }
    if (!seen) { fprintf(f, "%s\n    \"0x%llx\"", first_frame ? "" : ",", (unsigned long long)(uintptr_t)pp->pcs[frame]); first_frame = false; }
  }
  fprintf(f, "\n  ]\n}\n");
}
bool mi_dhat_dump(const char* path) mi_attr_noexcept {
  if (path == NULL || dhat_observer_depth != 0) return false;
  FILE* f = fopen(path, "wb"); if (f == NULL) return false;
  mi_lock_acquire(&dhat_lock); dhat_write_json_locked(f); mi_lock_release(&dhat_lock);
  const bool ok = (fclose(f) == 0); return ok;
}
void _mi_dhat_process_init(void) { dhat_resolve_env(); }
void _mi_dhat_process_done(void) { if (dhat_dump_at_exit[0] != 0) { const bool dumped = mi_dhat_dump(dhat_dump_at_exit); MI_UNUSED(dumped); } }
