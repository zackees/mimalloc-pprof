/* Allocation sampling profiler.  Its records never use mimalloc: the arena
   below is backed directly by _mi_os_alloc so profiler bookkeeping cannot
   recursively enter the allocator. */
#include "mimalloc.h"
#include "mimalloc/internal.h"
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <limits.h>
#include <stddef.h>

#if MI_PPROF

#define MI_PROF_CHUNK_SIZE (64*1024)

typedef struct mi_prof_chunk_s {
  struct mi_prof_chunk_s* next;
  mi_memid_t memid;
  size_t size;
  size_t used;
} mi_prof_chunk_t;

typedef struct mi_prof_record_s {
  struct mi_prof_record_s* next;
  struct mi_prof_record_s* all_next;
  void* ptr;
  mi_page_t* page;
  size_t size;
  mi_prof_stack_t* stack;
} mi_prof_record_t;

static mi_lock_t prof_lock = MI_LOCK_INITIALIZER;
/* size_t (not bool): MSVC's plain-C atomic wrapper (mimalloc/atomic.h) only
   implements uintptr_t/int64_t width primitives, so an `_Atomic(bool)` here
   silently reinterprets this 1-byte flag as an 8-byte load/store (MSVC
   warning C4133), corrupting adjacent globals and reading garbage. All uses
   below already treat this as a 0/1 flag via implicit bool conversion. */
static _Atomic(size_t) prof_enabled = false;
static mi_prof_chunk_t* prof_chunks;
static mi_prof_record_t* prof_all;
static mi_prof_record_t* prof_free;
static _Atomic(size_t) prof_records;
static _Atomic(size_t) prof_bytes;
static _Atomic(size_t) prof_accum_records;
static _Atomic(size_t) prof_accum_bytes;
static _Atomic(size_t) prof_arena_committed;
/* All dropped-sample causes (record-alloc failure, stack-intern failure -- capture
   failure, arena-alloc failure, or the MI_PROF_STACK_CAP cap already tracked
   separately by _mi_prof_stack_overflows()). See profile.h's Bounds/failure-policy
   comment and mi_prof_stats_t.dropped_samples. */
static _Atomic(size_t) prof_dropped_samples;
static size_t prof_rate = 524288;
static uint64_t prof_seed;
static uint64_t prof_generation;
static char prof_dump_at_exit[1024];
/* Budget (bytes) for profiler-internal *persistent* arena memory (see
   mi_prof_config_t.max_profiler_bytes in profile.h); cached at start from
   mi_option_prof_max_bytes so _mi_prof_arena_alloc need not re-resolve the
   option (and any env var / mi_option_set precedence) on every call. Always
   read/written while `prof_lock` is held (mirrors prof_rate/prof_seed above). */
static size_t prof_max_bytes;
/* 0 = MI_PROF_FORMAT_TEXT (the default). Set either by mi_prof_start_ex's
   dump_format field or, for pure-env users, prof_auto_start reading
   MIMALLOC_PROF_DUMP_FORMAT; consumed by _mi_prof_process_done. */
static int prof_dump_at_exit_format;
static mi_decl_thread int prof_callback_depth;
static mi_decl_thread bool prof_lock_owner;
static inline size_t prof_min(size_t x, size_t y) { return (x < y ? x : y); }
static inline size_t prof_max(size_t x, size_t y) { return (x > y ? x : y); }

/* ---- small helpers shared by mi_prof_start_ex's env/struct precedence resolution -----------
   (see mi_prof_config_t's mode documentation in profile.h for the FALLBACK/OVERRIDE contract). */
static bool prof_env_present(const char* name) {
  char buf[64];
  return (_mi_getenv(name, buf, sizeof(buf)) == 0);
}
/* Tiny local decimal parser (mirrors options.c's mi_option_init, minus the KiB-suffix and
   boolean-string handling those don't apply to a raw byte count like MIMALLOC_PROF_SAMPLE_INTERVAL). */
static bool prof_env_get_size(const char* name, size_t* out) {
  char buf[64];
  if (_mi_getenv(name, buf, sizeof(buf)) != 0) return false;
  if (buf[0] == 0) return false;
  char* end = buf;
  unsigned long long value = strtoull(buf, &end, 10);
  if (end == buf || *end != 0) return false;
  *out = (size_t)value;
  return true;
}
/* "proto"/"1" (case-insensitive) => MI_PROF_FORMAT_PROTO, anything else => MI_PROF_FORMAT_TEXT;
   mirrors mi_option_init's uppercase-then-compare idiom in options.c. */
static int prof_parse_dump_format(const char* s) {
  char buf[16];
  size_t len = _mi_strnlen(s, sizeof(buf) - 1);
  for (size_t i = 0; i < len; i++) buf[i] = _mi_toupper(s[i]);
  buf[len] = 0;
  if (_mi_streq(buf, "PROTO") || _mi_streq(buf, "1")) return MI_PROF_FORMAT_PROTO;
  return MI_PROF_FORMAT_TEXT;
}

typedef struct prof_dump_chunk_s {
  struct prof_dump_chunk_s* next;
  mi_memid_t memid;
  size_t capacity;
  size_t used;
  char data[];
} prof_dump_chunk_t;

typedef struct prof_dump_buffer_s {
  prof_dump_chunk_t* first;
  prof_dump_chunk_t* last;
  bool ok;
} prof_dump_buffer_t;

static bool prof_dump_append(void* arg, const char* buf, size_t len) {
  prof_dump_buffer_t* out = (prof_dump_buffer_t*)arg;
  while (len > 0) {
    prof_dump_chunk_t* chunk = out->last;
    if (chunk == NULL || chunk->used == chunk->capacity) {
      const size_t capacity = prof_max(MI_PROF_CHUNK_SIZE, len);
      mi_memid_t memid;
      chunk = (prof_dump_chunk_t*)_mi_os_alloc(sizeof(*chunk) + capacity, &memid);
      if (chunk == NULL) { out->ok = false; return false; }
      chunk->next = NULL; chunk->memid = memid; chunk->capacity = capacity; chunk->used = 0;
      if (out->last != NULL) out->last->next = chunk; else out->first = chunk;
      out->last = chunk;
    }
    const size_t n = prof_min(len, chunk->capacity - chunk->used);
    memcpy(chunk->data + chunk->used, buf, n);
    chunk->used += n; buf += n; len -= n;
  }
  return true;
}

static void prof_dump_dispose(prof_dump_buffer_t* out) {
  for (prof_dump_chunk_t* chunk = out->first; chunk != NULL; ) {
    prof_dump_chunk_t* next = chunk->next;
    _mi_os_free(chunk, sizeof(*chunk) + chunk->capacity, chunk->memid);
    chunk = next;
  }
  out->first = out->last = NULL;
}

typedef struct prof_dump_totals_s { size_t cur_objs, cur_bytes, accum_objs, accum_bytes; } prof_dump_totals_t;
static bool prof_dump_total_stack(const mi_prof_sample_info_t* info, void* arg) {
  prof_dump_totals_t* totals = (prof_dump_totals_t*)arg;
  totals->cur_objs += info->live_objects; totals->cur_bytes += info->live_bytes;
  totals->accum_objs += info->accum_objects; totals->accum_bytes += info->accum_bytes;
  return true;
}
static void prof_dump_format(const mi_prof_sample_info_t* info, char* buf, size_t capacity, size_t* written) {
  if (info == NULL || buf == NULL || capacity == 0) { if (written) *written = 0; return; }
  int n = snprintf(buf, capacity, "%7llu: %llu [%7llu: %llu] @", (unsigned long long)info->live_objects, (unsigned long long)info->live_bytes, (unsigned long long)info->accum_objects, (unsigned long long)info->accum_bytes);
  size_t used = (n > 0 ? prof_min((size_t)n, capacity - 1) : 0);
  for (size_t i = 0; i < info->depth && used < capacity - 1; i++) { n = snprintf(buf+used, capacity-used, " 0x%llx", (unsigned long long)(uintptr_t)info->stack[i]); used += (n > 0 ? prof_min((size_t)n, capacity-used-1) : 0); }
  if (used < capacity - 1) { buf[used++] = '\n'; }
  buf[used] = 0;
  if (written) *written = used;
}
static bool prof_dump_stack(const mi_prof_sample_info_t* info, void* arg) {
  prof_dump_buffer_t* out = (prof_dump_buffer_t*)arg;
  if (info->live_objects == 0 && info->live_bytes == 0 && info->accum_objects == 0 && info->accum_bytes == 0) return true;
  char line[4096]; size_t len;
  prof_dump_format(info, line, sizeof(line), &len);
  if (!prof_dump_append(out, line, len)) out->ok = false;
  return out->ok;
}

static uint64_t prof_random(mi_profiler_tld_t* tld) {
  uint64_t x = tld->random;
  if (x == 0) x = prof_seed ^ (uintptr_t)tld ^ UINT64_C(0x9E3779B97F4A7C15);
  x ^= x >> 12; x ^= x << 25; x ^= x >> 27;
  tld->random = x;
  return x * UINT64_C(2685821657736338717);
}

static size_t prof_threshold(mi_profiler_tld_t* tld) {
  size_t rate = prof_rate;
  size_t value = (size_t)(prof_random(tld) % (rate * 2));
  if (value < 1) value = 1;
  if (value > rate * 64) value = rate * 64;
  return value;
}

void* _mi_prof_arena_alloc(size_t size) {
  mi_prof_chunk_t* chunk = prof_chunks;
  const size_t align = sizeof(void*) - 1;
  if (chunk == NULL || chunk->used + size + align > chunk->size) {
    if (size > SIZE_MAX - sizeof(*chunk) - align) return NULL;
    const size_t chunk_size = prof_max((size_t)MI_PROF_CHUNK_SIZE, sizeof(*chunk) + size + align);
    /* Budget only gates growing the arena with a *new* chunk (mi_prof_config_t.max_profiler_bytes);
       callers (prof_record_alloc, stack_init/stack_grow, _mi_prof_stack_intern) already treat a
       NULL return here as "drop this sample" and recycle/no-op rather than leak or crash. */
    if (prof_max_bytes != 0 && mi_atomic_load_relaxed(&prof_arena_committed) + chunk_size > prof_max_bytes) return NULL;
    mi_memid_t memid;
    void* p = _mi_os_alloc(chunk_size, &memid);
    if (p == NULL) return NULL;
    chunk = (mi_prof_chunk_t*)p;
    chunk->next = prof_chunks; chunk->memid = memid; chunk->size = chunk_size; chunk->used = sizeof(*chunk);
    prof_chunks = chunk;
    mi_atomic_add_relaxed(&prof_arena_committed, chunk_size);
  }
  uintptr_t start = ((uintptr_t)chunk + chunk->used + align) & ~(uintptr_t)align;
  chunk->used = start - (uintptr_t)chunk + size;
  return (void*)start;
}
size_t _mi_prof_arena_committed(void) { return mi_atomic_load_relaxed(&prof_arena_committed); }

static mi_prof_record_t* prof_record_alloc(void) {
  if (prof_free != NULL) { mi_prof_record_t* rec = prof_free; prof_free = rec->next; return rec; }
  return (mi_prof_record_t*)_mi_prof_arena_alloc(sizeof(mi_prof_record_t));
}

static void prof_remove_all(mi_prof_record_t* rec) {
  mi_prof_record_t** cur = &prof_all;
  while (*cur != rec) cur = &(*cur)->all_next;
  *cur = rec->all_next;
}

static void prof_free_record(mi_page_t* page, void* p) {
  mi_prof_record_t** cur = (mi_prof_record_t**)&page->metadata;
  while (*cur != NULL && (*cur)->ptr != p) cur = &(*cur)->next;
  if (*cur == NULL) return;
  mi_prof_record_t* rec = *cur;
  *cur = rec->next;
  if (page->metadata == NULL) page->has_metadata = false;
  prof_remove_all(rec);
  mi_atomic_decrement_relaxed(&prof_records); mi_atomic_sub_relaxed(&prof_bytes, rec->size);
  _mi_prof_stack_free(rec->stack, rec->size);
  _mi_prof_stack_release(rec->stack);
  rec->next = prof_free; prof_free = rec;
}

bool mi_prof_start_seeded(size_t sample_rate, uint64_t seed) mi_attr_noexcept {
  if (prof_callback_depth > 0) return false;
  if (sample_rate == 0) {
    /* MIMALLOC_PROF_SAMPLE_INTERVAL is the honest name for this env var (it is an average
       byte interval, not a rate); MIMALLOC_PROF_SAMPLE_RATE (-> mi_option_prof_sample_rate)
       stays as a compat alias. When both are set, INTERVAL wins. */
    size_t interval;
    if (prof_env_get_size("MIMALLOC_PROF_SAMPLE_INTERVAL", &interval) && interval != 0) sample_rate = interval;
    else sample_rate = (size_t)mi_option_get(mi_option_prof_sample_rate);
  }
  if (sample_rate == 0) sample_rate = 524288;
  mi_lock_acquire(&prof_lock);
  bool started = !mi_atomic_load_relaxed(&prof_enabled);
  if (started) {
    prof_rate = sample_rate; prof_seed = seed; prof_generation++;
    prof_max_bytes = (size_t)mi_option_get(mi_option_prof_max_bytes);
    mi_atomic_store_release(&prof_enabled, true);
  }
  mi_lock_release(&prof_lock);
  return started;
}
bool mi_prof_start(size_t sample_rate) mi_attr_noexcept { return mi_prof_start_seeded(sample_rate, (uint64_t)mi_option_get(mi_option_prof_seed)); }

/* mi_prof_start_ex: see mi_prof_config_t's mode documentation in profile.h for the full
   FALLBACK/OVERRIDE precedence contract. Per-field resolution below is the same shape
   throughout: OVERRIDE with a non-zero/non-NULL field always wins; otherwise, if the
   field's env var is present, the env/option value wins (struct field ignored); otherwise
   a non-zero/non-NULL struct field wins; otherwise the existing default applies. accum,
   max_stack_depth, and max_profiler_bytes are applied via mi_option_set so the profiler's
   normal (already env-aware) option-reading paths pick them up; sample_interval and seed
   are fed directly as mi_prof_start_seeded's parameters since nothing re-reads them as
   options after start; dump_at_exit/dump_format have no backing mi_option and are resolved
   by hand against the same env vars prof_auto_start uses. */
bool mi_prof_start_ex(const mi_prof_config_t* config) mi_attr_noexcept {
  if (config == NULL) return mi_prof_start(0);
  if (config->size != sizeof(mi_prof_config_t) || config->version != MI_PROF_CONFIG_VERSION) return false;
  if (prof_callback_depth > 0) return false;
  const bool is_override = (config->mode == MI_PROF_CONFIG_OVERRIDE);

  if (config->accum) {
    if (is_override || !prof_env_present("MIMALLOC_PROF_ACCUM")) mi_option_set_enabled(mi_option_prof_accum, true);
  }
  if (config->max_stack_depth != 0) {
    if (is_override || !prof_env_present("MIMALLOC_PROF_BT_MAX")) {
      size_t depth = config->max_stack_depth;
      if (depth > 128) depth = 128;  // compile cap; mirrors MI_PROF_BT_MAX_LIMIT in profile-stack.c.
      mi_option_set(mi_option_prof_bt_max, (long)depth);
    }
  }
  if (config->max_profiler_bytes != 0) {
    if (is_override || !prof_env_present("MIMALLOC_PROF_MAX_BYTES")) {
      const size_t bytes = config->max_profiler_bytes;
      mi_option_set(mi_option_prof_max_bytes, (bytes > (size_t)LONG_MAX ? LONG_MAX : (long)bytes));
    }
  }
  {
    const bool env_present = prof_env_present("MIMALLOC_PROF_DUMP_AT_EXIT");
    if (config->dump_at_exit != NULL && (is_override || !env_present)) {
      _mi_strlcpy(prof_dump_at_exit, config->dump_at_exit, sizeof(prof_dump_at_exit));
    }
    else if (env_present) {
      (void)_mi_getenv("MIMALLOC_PROF_DUMP_AT_EXIT", prof_dump_at_exit, sizeof(prof_dump_at_exit));
    }
    // else: field NULL and no env -> leave prof_dump_at_exit untouched, matching mi_prof_start(0).
  }
  {
    const bool env_present = prof_env_present("MIMALLOC_PROF_DUMP_FORMAT");
    if (config->dump_format != MI_PROF_FORMAT_TEXT && (is_override || !env_present)) {
      prof_dump_at_exit_format = config->dump_format;
    }
    else if (env_present) {
      char fmt_buf[32];
      if (_mi_getenv("MIMALLOC_PROF_DUMP_FORMAT", fmt_buf, sizeof(fmt_buf)) == 0) prof_dump_at_exit_format = prof_parse_dump_format(fmt_buf);
    }
  }

  size_t resolved_interval;
  if (is_override && config->sample_interval != 0) {
    resolved_interval = config->sample_interval;
  }
  else if (prof_env_present("MIMALLOC_PROF_SAMPLE_INTERVAL") || prof_env_present("MIMALLOC_PROF_SAMPLE_RATE")) {
    resolved_interval = 0;  // defer to mi_prof_start_seeded's default chain, which resolves these same two names.
  }
  else {
    resolved_interval = config->sample_interval;  // 0 here also defers to the default chain.
  }

  uint64_t resolved_seed;
  if (is_override && config->seed != 0) {
    resolved_seed = config->seed;
  }
  else if (prof_env_present("MIMALLOC_PROF_SEED")) {
    resolved_seed = (uint64_t)mi_option_get(mi_option_prof_seed);
  }
  else if (config->seed != 0) {
    resolved_seed = config->seed;
  }
  else {
    resolved_seed = (uint64_t)mi_option_get(mi_option_prof_seed);
  }

  return mi_prof_start_seeded(resolved_interval, resolved_seed);
}

bool mi_prof_is_enabled(void) mi_attr_noexcept { return mi_atomic_load_relaxed(&prof_enabled); }
bool mi_prof_stats_get(mi_prof_stats_t* stats) mi_attr_noexcept {
  if (stats == NULL) return false;
  /* v2 callers (current mi_prof_stats_t_decl) pass the full struct/version 2; v1 callers
     (built against the pre-dropped_samples header) pass the struct truncated right before
     dropped_samples and version 1 -- accept both, reject anything else. Writing into
     dropped_samples for a v1-sized struct would be an out-of-bounds write, so that field
     is only touched in the v2 branch. */
  const bool is_v2 = (stats->size == sizeof(mi_prof_stats_t) && stats->version == 2);
  const bool is_v1 = (stats->size == offsetof(mi_prof_stats_t, dropped_samples) && stats->version == 1);
  if (!is_v2 && !is_v1) return false;
  stats->enabled = mi_atomic_load_relaxed(&prof_enabled);
  stats->accum = mi_option_is_enabled(mi_option_prof_accum);
  stats->sample_rate = prof_rate;
  stats->live_samples = mi_atomic_load_relaxed(&prof_records);
  stats->live_bytes = mi_atomic_load_relaxed(&prof_bytes);
  stats->accum_samples = mi_atomic_load_relaxed(&prof_accum_records);
  stats->accum_bytes = mi_atomic_load_relaxed(&prof_accum_bytes);
  stats->unique_stacks = _mi_prof_stack_count();
  stats->arena_committed = _mi_prof_arena_committed();
  stats->stack_table_overflows = _mi_prof_stack_overflows();
  if (is_v2) stats->dropped_samples = mi_atomic_load_relaxed(&prof_dropped_samples);
  return true;
}
void mi_prof_debug_stats(size_t* records, size_t* bytes, size_t* unique_stacks) mi_attr_noexcept { mi_prof_stats_t_decl(stats); const bool ok = mi_prof_stats_get(&stats); MI_UNUSED(ok); if (records) *records=stats.live_samples; if (bytes) *bytes=stats.live_bytes; if (unique_stacks) *unique_stacks=stats.unique_stacks; }
void mi_prof_stop(void) mi_attr_noexcept {
  if (prof_callback_depth > 0) return;
  mi_lock_acquire(&prof_lock);
  mi_atomic_store_release(&prof_enabled, false);
  for (mi_prof_record_t* rec = prof_all; rec != NULL; rec = rec->all_next) { rec->page->metadata = NULL; rec->page->has_metadata = false; }
  _mi_prof_stack_done();
  mi_prof_chunk_t* chunk = prof_chunks;
  while (chunk != NULL) { mi_prof_chunk_t* next = chunk->next; _mi_os_free(chunk, chunk->size, chunk->memid); chunk = next; }
  prof_chunks=NULL; prof_all=NULL; prof_free=NULL;
  mi_atomic_store_relaxed(&prof_records, (size_t)0); mi_atomic_store_relaxed(&prof_bytes, (size_t)0);
  mi_atomic_store_relaxed(&prof_accum_records, (size_t)0); mi_atomic_store_relaxed(&prof_accum_bytes, (size_t)0);
  mi_atomic_store_relaxed(&prof_arena_committed, (size_t)0);
  mi_atomic_store_relaxed(&prof_dropped_samples, (size_t)0);
  mi_lock_release(&prof_lock);
}
bool mi_prof_dump_writer(mi_prof_write_fun* write, void* arg) mi_attr_noexcept {
  if (write == NULL) return false;
  if (prof_callback_depth > 0) return false;
  prof_dump_buffer_t out = { NULL, NULL, true };
#if MI_DEBUG
  bool lock_held = true;
#endif
  mi_lock_acquire(&prof_lock);
  prof_dump_totals_t totals = { 0, 0, 0, 0 };
  _mi_prof_stack_visit_info(prof_dump_total_stack, &totals);
  char header[160];
  int n = _mi_snprintf(header, sizeof(header), "heap profile: %7llu: %llu [%7llu: %llu] @ heap_v2/%llu\n", (unsigned long long)totals.cur_objs, (unsigned long long)totals.cur_bytes, (unsigned long long)totals.accum_objs, (unsigned long long)totals.accum_bytes, (unsigned long long)prof_rate);
  if (n < 0 || !prof_dump_append(&out, header, prof_min((size_t)n, sizeof(header) - 1))) out.ok = false;
  if (out.ok) _mi_prof_stack_visit_info(prof_dump_stack, &out);
  mi_lock_release(&prof_lock);
#if MI_DEBUG
  lock_held = false;
#endif
  if (out.ok && !prof_dump_append(&out, "MAPPED_LIBRARIES:\n", 18)) out.ok = false;
  if (out.ok && !_mi_prof_maps_append(prof_dump_append, &out)) out.ok = false;
#if MI_DEBUG
  mi_assert_internal(!lock_held); // callbacks must never run with the table lock held.
#endif
  if (out.ok) for (prof_dump_chunk_t* chunk = out.first; chunk != NULL; chunk = chunk->next) write(arg, chunk->data, chunk->used);
  const bool ok = out.ok;
  prof_dump_dispose(&out);
  return ok;
}
static void prof_file_write(void* arg, const char* buf, size_t len) { (void)fwrite(buf,1,len,(FILE*)arg); }
bool mi_prof_dump(const char* path) mi_attr_noexcept { if (prof_callback_depth > 0) return false; if (path==NULL) return false; FILE* f=fopen(path,"wb"); if(f==NULL)return false; bool ok=mi_prof_dump_writer(prof_file_write,f); fclose(f); return ok; }
void mi_prof_reset(void) mi_attr_noexcept {
  if (prof_callback_depth > 0) return;
  mi_lock_acquire(&prof_lock);
  _mi_prof_stack_reset();
  mi_atomic_store_relaxed(&prof_accum_records, (size_t)0); mi_atomic_store_relaxed(&prof_accum_bytes, (size_t)0);
  mi_lock_release(&prof_lock);
}
bool mi_prof_visit(mi_prof_visit_fun* visitor, void* arg) mi_attr_noexcept {
  if (prof_callback_depth > 0) return false;
  if (visitor == NULL) return false;
  mi_lock_acquire(&prof_lock);
  _mi_prof_stack_pin_all();
  prof_lock_owner = true; prof_callback_depth++;
  _mi_prof_stack_visit_info(visitor, arg);
  prof_callback_depth--; prof_lock_owner = false;
  _mi_prof_stack_unpin_all_and_sweep();
  mi_lock_release(&prof_lock);
  return true;
}
typedef struct mi_prof_snapshot_entry_s {
  size_t depth;
  size_t live_objects, live_bytes;
  size_t accum_objects, accum_bytes;
  const void* const* pcs;
} mi_prof_snapshot_entry_t;
struct mi_prof_snapshot_s {
  mi_memid_t memid;
  size_t total_size;
  size_t count;
  mi_prof_snapshot_entry_t entries[];
};
typedef struct prof_snap_count_s { size_t count; size_t pcs; } prof_snap_count_t;
static bool prof_snapshot_count(const mi_prof_sample_info_t* info, void* arg) {
  prof_snap_count_t* c = (prof_snap_count_t*)arg;
  c->count++; c->pcs += info->depth;
  return true;
}
typedef struct prof_snap_build_s { mi_prof_snapshot_entry_t* entry; void** pcpool; } prof_snap_build_t;
static bool prof_snapshot_copy(const mi_prof_sample_info_t* info, void* arg) {
  prof_snap_build_t* b = (prof_snap_build_t*)arg;
  b->entry->depth = info->depth;
  b->entry->live_objects = info->live_objects; b->entry->live_bytes = info->live_bytes;
  b->entry->accum_objects = info->accum_objects; b->entry->accum_bytes = info->accum_bytes;
  b->entry->pcs = (const void* const*)b->pcpool;
  for (size_t i = 0; i < info->depth; i++) b->pcpool[i] = (void*)info->stack[i];
  b->pcpool += info->depth; b->entry++;
  return true;
}
mi_prof_snapshot_t* mi_prof_snapshot_new(void) mi_attr_noexcept {
  if (prof_callback_depth > 0) return NULL;
  mi_lock_acquire(&prof_lock);
  prof_snap_count_t counted = { 0, 0 };
  _mi_prof_stack_visit_info(prof_snapshot_count, &counted);
  const size_t entries_size = counted.count * sizeof(mi_prof_snapshot_entry_t);
  const size_t pcs_size = counted.pcs * sizeof(void*);
  const size_t total_size = sizeof(mi_prof_snapshot_t) + entries_size + pcs_size;
  mi_memid_t memid;
  void* p = _mi_os_alloc(total_size, &memid);
  if (p == NULL) { mi_lock_release(&prof_lock); return NULL; }
  mi_prof_snapshot_t* snap = (mi_prof_snapshot_t*)p;
  snap->memid = memid; snap->total_size = total_size; snap->count = counted.count;
  prof_snap_build_t build = { snap->entries, (void**)((char*)p + sizeof(mi_prof_snapshot_t) + entries_size) };
  _mi_prof_stack_visit_info(prof_snapshot_copy, &build);
  mi_lock_release(&prof_lock);
  return snap;
}
bool mi_prof_snapshot_visit(const mi_prof_snapshot_t* snap, mi_prof_visit_fun* visitor, void* arg) mi_attr_noexcept {
  if (snap == NULL || visitor == NULL) return false;
  for (size_t i = 0; i < snap->count; i++) {
    const mi_prof_snapshot_entry_t* e = &snap->entries[i];
    mi_prof_sample_info_t info;
    info.stack = e->pcs; info.depth = e->depth;
    info.live_objects = e->live_objects; info.live_bytes = e->live_bytes;
    info.accum_objects = e->accum_objects; info.accum_bytes = e->accum_bytes;
    if (!visitor(&info, arg)) break;
  }
  return true;
}
void mi_prof_snapshot_free(mi_prof_snapshot_t* snap) mi_attr_noexcept { if (snap != NULL) _mi_os_free(snap, snap->total_size, snap->memid); }

bool mi_prof_modules_visit(mi_prof_module_visit_fun* visitor, void* arg) mi_attr_noexcept {
  if (visitor == NULL) return false;
  if (prof_callback_depth > 0) return false;
  return _mi_prof_maps_visit(visitor, arg);  // OS-owned module list: no profiler lock needed.
}

/* ---------------------------------------------------------------------------------------------
   profile.proto (google/pprof) writer.

   Minimal hand-written protobuf encoder: varint(), a tag+varint field writer, and a tag+length
   "bytes" field writer (used for both length-delimited strings/submessages and, with a
   pre-concatenated-varint payload, PACKED repeated scalar fields -- proto3 packs repeated
   numeric fields by default, and this is exactly how Go's own hand-rolled encoder for pprof
   builds Sample.location_id/Sample.value too, via `b.pb.uint64s`/`b.pb.int64s`
   (go/src/runtime/pprof/proto.go)). Submessages (ValueType/Mapping/Location/Sample) are built
   into small stack scratch buffers first (their max size is bounded: <=128 stack frames per
   sample, module paths capped below) so no first pass is needed to size them; the buffer holding
   them is then written to the top-level Profile message as one length-delimited field. All
   scratch memory here is transient stack space or _mi_os_alloc (never mi_malloc), consistent
   with the rest of the profiler.

   Field numbers verified against https://github.com/google/pprof/blob/main/proto/profile.proto:
     Profile:   sample_type=1 sample=2 mapping=3 location=4 string_table=6
                period_type=11 period=12 default_sample_type=14
     ValueType: type=1 unit=2                      (both string_table indices)
     Sample:    location_id=1 (packed uint64)  value=2 (packed int64)
     Mapping:   id=1 memory_start=2 memory_limit=3  filename=5 (string_table index)
     Location:  id=1 mapping_id=2 address=3
   No Function/Line tables are written (id-less: consumers resolve symbols via the Mapping's
   filename, same as any pprof profile with has_functions=false on its mappings). */

static size_t pb_varint(uint8_t* buf, uint64_t v) {
  size_t n = 0;
  do { uint8_t b = (uint8_t)(v & 0x7F); v >>= 7; if (v != 0) b |= 0x80; buf[n++] = b; } while (v != 0);
  return n;
}
static size_t pb_field_varint(uint8_t* buf, size_t pos, uint32_t field, uint64_t value) {
  pos += pb_varint(buf + pos, ((uint64_t)field << 3) | 0);
  pos += pb_varint(buf + pos, value);
  return pos;
}
static size_t pb_field_bytes(uint8_t* buf, size_t pos, uint32_t field, const void* data, size_t len) {
  pos += pb_varint(buf + pos, ((uint64_t)field << 3) | 2);
  pos += pb_varint(buf + pos, (uint64_t)len);
  if (len > 0) memcpy(buf + pos, data, len);
  return pos + len;
}
static bool pb_emit_bytes_field(prof_dump_buffer_t* out, uint32_t field, const void* data, size_t len) {
  uint8_t hdr[16]; size_t n = pb_varint(hdr, ((uint64_t)field << 3) | 2); n += pb_varint(hdr + n, (uint64_t)len);
  if (!prof_dump_append(out, (const char*)hdr, n)) return false;
  return (len == 0) || prof_dump_append(out, (const char*)data, len);
}
static bool pb_emit_varint_field(prof_dump_buffer_t* out, uint32_t field, uint64_t value) {
  uint8_t buf[20]; size_t n = pb_varint(buf, ((uint64_t)field << 3) | 0); n += pb_varint(buf + n, value);
  return prof_dump_append(out, (const char*)buf, n);
}
static bool pb_emit_valuetype(prof_dump_buffer_t* out, uint32_t field, int64_t type_idx, int64_t unit_idx) {
  uint8_t vt[24]; size_t n = 0;
  n = pb_field_varint(vt, n, 1, (uint64_t)type_idx);
  n = pb_field_varint(vt, n, 2, (uint64_t)unit_idx);
  return pb_emit_bytes_field(out, field, vt, n);
}

/* Mirrors go/src/runtime/pprof/protomem.go's scaleHeapSample, which this comment quotes from
   memory (verified against the live file on github.com/golang/go):
     // scaleHeapSample adjusts the data from a heap Sample to account for its probability of
     // appearing in the collected data. heap profiles are a sampling of the memory allocation
     // requests... we estimate the unsampled value by dividing the sampled value by the
     // probability of a sample.
     func scaleHeapSample(count, size, rate int64) (int64, int64) {
       if count == 0 || size == 0 { return 0, 0 }
       if rate <= 1 { return count, size }              // rate<=1: everything was sampled.
       avgSize := float64(size) / float64(count)
       scale := 1 / (1 - math.Exp(-avgSize/float64(rate)))
       return int64(float64(count) * scale), int64(float64(size) * scale)
     }
   Go calls this twice per record -- once for the Alloc* totals, once for the InUse* totals --
   each with that record's own (count,bytes,rate). We do the same per unique stack below: once
   for (accum_objects,accum_bytes) -> alloc_*, once for (live_objects,live_bytes) -> inuse_*.
   When MIMALLOC_PROF_ACCUM is off, accum_objects/accum_bytes are always 0 (profile-stack.c's
   _mi_prof_stack_alloc only updates them when mi_option_prof_accum is enabled), so alloc_* comes
   out (0,0) via the count==0||size==0 shortcut above -- this is the "accum-off => alloc_* zero"
   behavior called for in the work order. */
/* Self-contained exp(), avoiding a new libm link dependency for this one call site: this
   profiler has no other transcendental-math usage today, CMakeLists.txt (out of scope for this
   change) does not link `m` on Unix, and "no new required dependencies" is a hard rule for this
   fork's C build. Textbook range reduction + Taylor series: exp(x) = 2^k * exp(r) with
   k = round(x/ln2) so |r| <= ln2/2 ~= 0.3466, which a 17-term Taylor series resolves to full
   double precision (error ~ r^18/18!, negligible). Only ever called below with x <= 0. */
static double prof_exp(double x) {
  if (x > 700.0) return 1e308;   // saturate; unreachable from prof_scale_heap_sample (x = -avg_size/rate <= 0)
  if (x < -700.0) return 0.0;    // underflows to 0 well before double's actual limit
  const double ln2 = 0.69314718055994530942;
  const long k = (long)(x / ln2 + (x >= 0.0 ? 0.5 : -0.5));  // round(x/ln2) to nearest, ties away from zero
  const double r = x - (double)k * ln2;
  double term = 1.0, sum = 1.0;
  for (int i = 1; i <= 17; i++) { term *= r / (double)i; sum += term; }
  double result = sum;
  if (k >= 0) { for (long i = 0; i < k; i++) result *= 2.0; }
  else        { for (long i = 0; i < -k; i++) result *= 0.5; }
  return result;
}
static void prof_scale_heap_sample(size_t count, size_t bytes, size_t rate, size_t* out_count, size_t* out_bytes) {
  if (count == 0 || bytes == 0) { *out_count = 0; *out_bytes = 0; return; }
  if (rate <= 1) { *out_count = count; *out_bytes = bytes; return; }
  const double avg_size = (double)bytes / (double)count;
  const double scale = 1.0 / (1.0 - prof_exp(-avg_size / (double)rate));
  *out_count = (size_t)((double)count * scale);
  *out_bytes = (size_t)((double)bytes * scale);
}

enum { PROF_PROTO_MAX_MODULES = 512 };   // dense fixed cap, mirrors the HMODULE[1024] cap in profile-maps.c
enum { PROF_PROTO_MAX_DEPTH = 128 };     // mirrors MI_PROF_BT_MAX_LIMIT in profile-stack.c
typedef struct proto_module_s { uintptr_t base; size_t size; char path[512]; } proto_module_t;
typedef struct proto_module_ctx_s { proto_module_t* modules; size_t count; } proto_module_ctx_t;
static bool proto_collect_module(const mi_prof_module_info_t* info, void* arg) {
  proto_module_ctx_t* ctx = (proto_module_ctx_t*)arg;
  if (ctx->count >= PROF_PROTO_MAX_MODULES) return true;  // table full: keep visiting, just stop recording.
  proto_module_t* m = &ctx->modules[ctx->count++];
  m->base = info->base; m->size = info->size;
  const size_t plen = prof_min(strlen(info->path), sizeof(m->path) - 1);
  memcpy(m->path, info->path, plen); m->path[plen] = 0;
  return true;
}
static uint32_t proto_module_for_pc(const proto_module_t* modules, size_t count, const void* pc) {
  const uintptr_t addr = (uintptr_t)pc;
  for (size_t i = 0; i < count; i++) if (addr >= modules[i].base && addr < modules[i].base + modules[i].size) return (uint32_t)(i + 1);
  return 0;
}

typedef struct proto_pc_slot_s { const void* pc; uint32_t id; } proto_pc_slot_t;
static uint32_t proto_pc_intern(proto_pc_slot_t* table, size_t capacity, const void* pc, uint32_t* next_id) {
  size_t index = (size_t)(((uintptr_t)pc >> 4) * (uintptr_t)2654435761u) & (capacity - 1);
  while (table[index].pc != NULL) { if (table[index].pc == pc) return table[index].id; index = (index + 1) & (capacity - 1); }
  table[index].pc = pc; table[index].id = (*next_id)++;
  return table[index].id;
}

enum { PB_STR_EMPTY = 0, PB_STR_ALLOC_OBJECTS, PB_STR_COUNT, PB_STR_ALLOC_SPACE, PB_STR_BYTES, PB_STR_INUSE_OBJECTS, PB_STR_INUSE_SPACE, PB_STR_SPACE, PB_STR_FIXED_COUNT };
static const char* const prof_proto_fixed_strings[PB_STR_FIXED_COUNT] = { "", "alloc_objects", "count", "alloc_space", "bytes", "inuse_objects", "inuse_space", "space" };

bool mi_prof_dump_proto_writer(mi_prof_write_fun* write, void* arg) mi_attr_noexcept {
  if (write == NULL) return false;
  if (prof_callback_depth > 0) return false;
  mi_prof_snapshot_t* snap = mi_prof_snapshot_new();  // deep-copies stacks+counts under prof_lock, then releases it.
  if (snap == NULL) return false;

  mi_memid_t mods_memid;
  proto_module_t* modules = (proto_module_t*)_mi_os_alloc(PROF_PROTO_MAX_MODULES * sizeof(proto_module_t), &mods_memid);
  if (modules == NULL) { mi_prof_snapshot_free(snap); return false; }
  proto_module_ctx_t mod_ctx = { modules, 0 };
  const bool maps_ok = _mi_prof_maps_visit(proto_collect_module, &mod_ctx);
  const size_t module_count = mod_ctx.count;

  // Unique-PC table (Location ids start at 1), sized once from the total PC count so no growth
  // logic is needed (upper bound is unique_stacks * max depth, the same bound the snapshot's
  // pcpool above is already sized to).
  size_t total_pcs = 0;
  for (size_t i = 0; i < snap->count; i++) total_pcs += snap->entries[i].depth;
  size_t pc_capacity = 16;
  while (pc_capacity < total_pcs * 2 + 1) pc_capacity *= 2;
  mi_memid_t pc_memid;
  proto_pc_slot_t* pc_table = (proto_pc_slot_t*)_mi_os_alloc(pc_capacity * sizeof(proto_pc_slot_t), &pc_memid);
  if (pc_table == NULL) { _mi_os_free(modules, PROF_PROTO_MAX_MODULES * sizeof(proto_module_t), mods_memid); mi_prof_snapshot_free(snap); return false; }
  memset(pc_table, 0, pc_capacity * sizeof(proto_pc_slot_t));
  uint32_t next_location_id = 1;

  prof_dump_buffer_t out = { NULL, NULL, true };
  bool ok = maps_ok;  // parity with mi_prof_dump_writer, which also fails the whole dump if maps enumeration fails.

  ok = ok && pb_emit_valuetype(&out, 1, PB_STR_ALLOC_OBJECTS, PB_STR_COUNT);
  ok = ok && pb_emit_valuetype(&out, 1, PB_STR_ALLOC_SPACE, PB_STR_BYTES);
  ok = ok && pb_emit_valuetype(&out, 1, PB_STR_INUSE_OBJECTS, PB_STR_COUNT);
  ok = ok && pb_emit_valuetype(&out, 1, PB_STR_INUSE_SPACE, PB_STR_BYTES);

  for (size_t i = 0; ok && i < snap->count; i++) {
    const mi_prof_snapshot_entry_t* e = &snap->entries[i];
    if (e->live_objects == 0 && e->live_bytes == 0 && e->accum_objects == 0 && e->accum_bytes == 0) continue;  // parity with the TEXT emitter's zero-filter.
    uint32_t location_ids[PROF_PROTO_MAX_DEPTH];
    const size_t depth = prof_min(e->depth, (size_t)PROF_PROTO_MAX_DEPTH);
    for (size_t f = 0; f < depth; f++) location_ids[f] = proto_pc_intern(pc_table, pc_capacity, e->pcs[f], &next_location_id);  // pcs[] is already callee-first (innermost frame first).
    size_t alloc_objs, alloc_bytes, live_objs, live_bytes;
    prof_scale_heap_sample(e->accum_objects, e->accum_bytes, prof_rate, &alloc_objs, &alloc_bytes);
    prof_scale_heap_sample(e->live_objects, e->live_bytes, prof_rate, &live_objs, &live_bytes);
    uint8_t sm[1536]; size_t sn = 0;
    { uint8_t packed[PROF_PROTO_MAX_DEPTH * 10]; size_t pn = 0; for (size_t f = 0; f < depth; f++) pn += pb_varint(packed + pn, (uint64_t)location_ids[f]); sn = pb_field_bytes(sm, sn, 1, packed, pn); }
    { uint8_t packed[64]; size_t pn = 0; const uint64_t vals[4] = { (uint64_t)alloc_objs, (uint64_t)alloc_bytes, (uint64_t)live_objs, (uint64_t)live_bytes }; for (int k = 0; k < 4; k++) pn += pb_varint(packed + pn, vals[k]); sn = pb_field_bytes(sm, sn, 2, packed, pn); }
    ok = pb_emit_bytes_field(&out, 2, sm, sn);
  }

  for (size_t i = 0; ok && i < module_count; i++) {
    uint8_t mp[64]; size_t mn = 0;
    mn = pb_field_varint(mp, mn, 1, (uint64_t)(i + 1));
    mn = pb_field_varint(mp, mn, 2, (uint64_t)modules[i].base);
    mn = pb_field_varint(mp, mn, 3, (uint64_t)(modules[i].base + modules[i].size));
    mn = pb_field_varint(mp, mn, 5, (uint64_t)(PB_STR_FIXED_COUNT + i));
    ok = pb_emit_bytes_field(&out, 3, mp, mn);
  }

  for (size_t i = 0; ok && i < pc_capacity; i++) {
    if (pc_table[i].pc == NULL) continue;
    const uint32_t mapping_id = proto_module_for_pc(modules, module_count, pc_table[i].pc);
    uint8_t lc[48]; size_t ln = 0;
    ln = pb_field_varint(lc, ln, 1, (uint64_t)pc_table[i].id);
    if (mapping_id != 0) ln = pb_field_varint(lc, ln, 2, (uint64_t)mapping_id);
    ln = pb_field_varint(lc, ln, 3, (uint64_t)(uintptr_t)pc_table[i].pc);
    ok = pb_emit_bytes_field(&out, 4, lc, ln);
  }

  for (size_t i = 0; ok && i < PB_STR_FIXED_COUNT; i++) ok = pb_emit_bytes_field(&out, 6, prof_proto_fixed_strings[i], strlen(prof_proto_fixed_strings[i]));
  for (size_t i = 0; ok && i < module_count; i++) ok = pb_emit_bytes_field(&out, 6, modules[i].path, strlen(modules[i].path));

  /* Self-describing validity signal (issue #33 finding #3): a shipped profile discloses its
     own sampling bias without a separate mi_prof_stats_get call. Read the atomics directly
     without prof_lock -- this function doesn't hold the lock across its body (see
     mi_prof_snapshot_new above), same as the unguarded prof_rate read a few lines below. */
  const size_t comment_str_idx = PB_STR_FIXED_COUNT + module_count;
  if (ok) {
    char comment_buf[128];
    const unsigned long long dropped = (unsigned long long)mi_atomic_load_relaxed(&prof_dropped_samples);
    const unsigned long long overflows = (unsigned long long)_mi_prof_stack_overflows();
    int clen = _mi_snprintf(comment_buf, sizeof(comment_buf), "dropped_samples=%llu stack_table_overflows=%llu", dropped, overflows);
    if (clen < 0) clen = 0;
    if ((size_t)clen >= sizeof(comment_buf)) clen = (int)sizeof(comment_buf) - 1;
    ok = pb_emit_bytes_field(&out, 6, comment_buf, (size_t)clen);
  }
  if (ok) { uint8_t packed[10]; size_t pn = pb_varint(packed, (uint64_t)comment_str_idx); ok = pb_emit_bytes_field(&out, 13, packed, pn); } // comment: packed repeated int64 of string_table indices (one entry).

  ok = ok && pb_emit_valuetype(&out, 11, PB_STR_SPACE, PB_STR_BYTES);              // period_type: space/bytes.
  ok = ok && pb_emit_varint_field(&out, 12, (uint64_t)prof_rate);                  // period: the configured sample rate.
  ok = ok && pb_emit_varint_field(&out, 14, (uint64_t)PB_STR_INUSE_SPACE);         // default_sample_type: inuse_space.

  if (ok) for (prof_dump_chunk_t* chunk = out.first; chunk != NULL; chunk = chunk->next) write(arg, chunk->data, chunk->used);
  prof_dump_dispose(&out);
  _mi_os_free(pc_table, pc_capacity * sizeof(proto_pc_slot_t), pc_memid);
  _mi_os_free(modules, PROF_PROTO_MAX_MODULES * sizeof(proto_module_t), mods_memid);
  mi_prof_snapshot_free(snap);
  return ok;
}
bool mi_prof_dump_proto(const char* path) mi_attr_noexcept {
  if (prof_callback_depth > 0) return false;
  if (path == NULL) return false;
  FILE* f = fopen(path, "wb"); if (f == NULL) return false;
  const bool ok = mi_prof_dump_proto_writer(prof_file_write, f);
  fclose(f);
  return ok;
}

static void prof_auto_start(void) {
  /* Some statically linked MinGW programs do not retain the CRT/TLS startup
     callback. Fall back to the first allocation so MIMALLOC_PROF still works. */
  mi_atomic_do_once {
    if (mi_option_is_enabled(mi_option_prof)) { const bool started = mi_prof_start(0); MI_UNUSED(started); }
    (void)_mi_getenv("MIMALLOC_PROF_DUMP_AT_EXIT", prof_dump_at_exit, sizeof(prof_dump_at_exit));
    /* So pure-env users (no mi_prof_start_ex call at all) still get profile.proto exit dumps. */
    char fmt_buf[32];
    if (_mi_getenv("MIMALLOC_PROF_DUMP_FORMAT", fmt_buf, sizeof(fmt_buf)) == 0) prof_dump_at_exit_format = prof_parse_dump_format(fmt_buf);
  }
}
void _mi_prof_process_init(void) {
  prof_auto_start();
}
void _mi_prof_process_done(void) {
  if (prof_dump_at_exit[0] != 0) {
    const bool dumped = (prof_dump_at_exit_format == MI_PROF_FORMAT_PROTO) ? mi_prof_dump_proto(prof_dump_at_exit) : mi_prof_dump(prof_dump_at_exit);
    MI_UNUSED(dumped);
  }
}
void _mi_prof_on_alloc(mi_heap_t* heap, mi_page_t* page, void* p, size_t size) {
  prof_auto_start();
  if mi_likely(!mi_atomic_load_relaxed(&prof_enabled)) return;
  if (prof_callback_depth > 0) return;
  mi_lock_acquire(&prof_lock);
  if (!mi_atomic_load_relaxed(&prof_enabled)) { mi_lock_release(&prof_lock); return; }
  mi_profiler_tld_t* tld = &heap->tld->profiler;
  if (tld->generation != prof_generation) { tld->bytes_since_sample = 0; tld->next_threshold = 0; tld->random = 0; tld->generation = prof_generation; }
  tld->bytes_since_sample += size;
  if (tld->next_threshold == 0) tld->next_threshold = prof_threshold(tld);
  if (tld->bytes_since_sample < tld->next_threshold) { mi_lock_release(&prof_lock); return; }
  tld->bytes_since_sample = 0; tld->next_threshold = prof_threshold(tld);
  mi_prof_record_t* rec = prof_record_alloc();
  if (rec != NULL) {
    rec->stack = _mi_prof_stack_intern();
    if (rec->stack != NULL) {
      _mi_prof_stack_alloc(rec->stack, size); rec->ptr=p; rec->page=page; rec->size=size; rec->next=(mi_prof_record_t*)page->metadata; rec->all_next=prof_all; page->metadata=(struct mi_prof_record_s*)rec; page->has_metadata=true; prof_all=rec;
      mi_atomic_increment_relaxed(&prof_records); mi_atomic_add_relaxed(&prof_bytes, size);
      if (mi_option_is_enabled(mi_option_prof_accum)) { mi_atomic_increment_relaxed(&prof_accum_records); mi_atomic_add_relaxed(&prof_accum_bytes, size); }
    }
    else { rec->next = prof_free; prof_free = rec; mi_atomic_increment_relaxed(&prof_dropped_samples); }  /* dropped sample: recycle the record or every post-overflow sample leaks arena memory */
  }
  else { mi_atomic_increment_relaxed(&prof_dropped_samples); }  /* dropped sample: record-alloc itself failed (budget/arena exhausted). */
  mi_lock_release(&prof_lock);
}
static void prof_free_collect(mi_page_t* page, mi_block_t* head) { for (mi_block_t* b=head; b != NULL && page->has_metadata; b=mi_block_next(page,b)) prof_free_record(page,b); }
static void prof_realloc_in_place(mi_page_t* page, void* p, size_t size) {
  for (mi_prof_record_t* rec = (mi_prof_record_t*)page->metadata; rec != NULL; rec = rec->next) {
    if (rec->ptr == p) { mi_atomic_store_relaxed(&prof_bytes, mi_atomic_load_relaxed(&prof_bytes) - rec->size + size); _mi_prof_stack_resize(rec->stack, rec->size, size); rec->size = size; break; }
  }
}
void _mi_prof_on_free(mi_page_t* page, void* p) {
  if mi_likely(!page->has_metadata) return;
  if (prof_lock_owner) { prof_free_record(page,p); return; }
  mi_lock_acquire(&prof_lock); prof_free_record(page,p); mi_lock_release(&prof_lock);
}
void _mi_prof_on_free_collect(mi_page_t* page, mi_block_t* head) {
  if mi_likely(!page->has_metadata) return;
  if (prof_lock_owner) { prof_free_collect(page,head); return; }
  mi_lock_acquire(&prof_lock); prof_free_collect(page,head); mi_lock_release(&prof_lock);
}
void _mi_prof_on_realloc_in_place(mi_page_t* page, void* p, size_t size) {
  if mi_likely(!page->has_metadata) return;
  if (prof_lock_owner) { prof_realloc_in_place(page,p,size); return; }
  mi_lock_acquire(&prof_lock); prof_realloc_in_place(page,p,size); mi_lock_release(&prof_lock);
}

#else
bool mi_prof_start(size_t sample_rate) mi_attr_noexcept { MI_UNUSED(sample_rate); return false; }
bool mi_prof_start_seeded(size_t sample_rate, uint64_t seed) mi_attr_noexcept { MI_UNUSED(sample_rate); MI_UNUSED(seed); return false; }
bool mi_prof_start_ex(const mi_prof_config_t* config) mi_attr_noexcept { MI_UNUSED(config); return false; }
void mi_prof_stop(void) mi_attr_noexcept { }
bool mi_prof_is_enabled(void) mi_attr_noexcept { return false; }
void mi_prof_debug_stats(size_t* records, size_t* bytes, size_t* unique_stacks) mi_attr_noexcept { if (records) *records=0; if (bytes) *bytes=0; if (unique_stacks) *unique_stacks=0; }
bool mi_prof_dump_writer(mi_prof_write_fun* write, void* arg) mi_attr_noexcept { MI_UNUSED(write); MI_UNUSED(arg); return false; }
bool mi_prof_dump(const char* path) mi_attr_noexcept { MI_UNUSED(path); return false; }
bool mi_prof_dump_proto_writer(mi_prof_write_fun* write, void* arg) mi_attr_noexcept { MI_UNUSED(write); MI_UNUSED(arg); return false; }
bool mi_prof_dump_proto(const char* path) mi_attr_noexcept { MI_UNUSED(path); return false; }
void mi_prof_reset(void) mi_attr_noexcept { }
bool mi_prof_stats_get(mi_prof_stats_t* stats) mi_attr_noexcept { MI_UNUSED(stats); return false; }
bool mi_prof_visit(mi_prof_visit_fun* visitor, void* arg) mi_attr_noexcept { MI_UNUSED(visitor); MI_UNUSED(arg); return false; }
bool mi_prof_modules_visit(mi_prof_module_visit_fun* visitor, void* arg) mi_attr_noexcept { MI_UNUSED(visitor); MI_UNUSED(arg); return false; }
mi_prof_snapshot_t* mi_prof_snapshot_new(void) mi_attr_noexcept { return NULL; }
bool mi_prof_snapshot_visit(const mi_prof_snapshot_t* snap, mi_prof_visit_fun* visitor, void* arg) mi_attr_noexcept { MI_UNUSED(snap); MI_UNUSED(visitor); MI_UNUSED(arg); return false; }
void mi_prof_snapshot_free(mi_prof_snapshot_t* snap) mi_attr_noexcept { MI_UNUSED(snap); }
void _mi_prof_process_init(void) { }
void _mi_prof_process_done(void) { }
#endif
