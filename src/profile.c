/* Allocation sampling profiler.  Its records never use mimalloc: the arena
   below is backed directly by _mi_os_alloc so profiler bookkeeping cannot
   recursively enter the allocator. */
#include "mimalloc.h"
#include "mimalloc/internal.h"
#include <stdio.h>
#include <string.h>

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
static size_t prof_rate = 524288;
static uint64_t prof_seed;
static uint64_t prof_generation;
static char prof_dump_at_exit[1024];
static mi_decl_thread int prof_callback_depth;
static mi_decl_thread bool prof_lock_owner;
static inline size_t prof_min(size_t x, size_t y) { return (x < y ? x : y); }
static inline size_t prof_max(size_t x, size_t y) { return (x > y ? x : y); }

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
  if (sample_rate == 0) sample_rate = (size_t)mi_option_get(mi_option_prof_sample_rate);
  if (sample_rate == 0) sample_rate = 524288;
  mi_lock_acquire(&prof_lock);
  bool started = !mi_atomic_load_relaxed(&prof_enabled);
  if (started) { prof_rate = sample_rate; prof_seed = seed; prof_generation++; mi_atomic_store_release(&prof_enabled, true); }
  mi_lock_release(&prof_lock);
  return started;
}
bool mi_prof_start(size_t sample_rate) mi_attr_noexcept { return mi_prof_start_seeded(sample_rate, (uint64_t)mi_option_get(mi_option_prof_seed)); }
bool mi_prof_is_enabled(void) mi_attr_noexcept { return mi_atomic_load_relaxed(&prof_enabled); }
bool mi_prof_stats_get(mi_prof_stats_t* stats) mi_attr_noexcept {
  if (stats == NULL || stats->size != sizeof(mi_prof_stats_t) || stats->version != MI_PROF_STAT_VERSION) return false;
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
static void prof_auto_start(void) {
  /* Some statically linked MinGW programs do not retain the CRT/TLS startup
     callback. Fall back to the first allocation so MIMALLOC_PROF still works. */
  mi_atomic_do_once {
    if (mi_option_is_enabled(mi_option_prof)) { const bool started = mi_prof_start(0); MI_UNUSED(started); }
    (void)_mi_getenv("MIMALLOC_PROF_DUMP_AT_EXIT", prof_dump_at_exit, sizeof(prof_dump_at_exit));
  }
}
void _mi_prof_process_init(void) {
  prof_auto_start();
}
void _mi_prof_process_done(void) { if (prof_dump_at_exit[0] != 0) { const bool dumped = mi_prof_dump(prof_dump_at_exit); MI_UNUSED(dumped); } }
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
  }
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
void mi_prof_stop(void) mi_attr_noexcept { }
bool mi_prof_is_enabled(void) mi_attr_noexcept { return false; }
void mi_prof_debug_stats(size_t* records, size_t* bytes, size_t* unique_stacks) mi_attr_noexcept { if (records) *records=0; if (bytes) *bytes=0; if (unique_stacks) *unique_stacks=0; }
bool mi_prof_dump_writer(mi_prof_write_fun* write, void* arg) mi_attr_noexcept { MI_UNUSED(write); MI_UNUSED(arg); return false; }
bool mi_prof_dump(const char* path) mi_attr_noexcept { MI_UNUSED(path); return false; }
void mi_prof_reset(void) mi_attr_noexcept { }
bool mi_prof_stats_get(mi_prof_stats_t* stats) mi_attr_noexcept { MI_UNUSED(stats); return false; }
bool mi_prof_visit(mi_prof_visit_fun* visitor, void* arg) mi_attr_noexcept { MI_UNUSED(visitor); MI_UNUSED(arg); return false; }
mi_prof_snapshot_t* mi_prof_snapshot_new(void) mi_attr_noexcept { return NULL; }
bool mi_prof_snapshot_visit(const mi_prof_snapshot_t* snap, mi_prof_visit_fun* visitor, void* arg) mi_attr_noexcept { MI_UNUSED(snap); MI_UNUSED(visitor); MI_UNUSED(arg); return false; }
void mi_prof_snapshot_free(mi_prof_snapshot_t* snap) mi_attr_noexcept { MI_UNUSED(snap); }
void _mi_prof_process_init(void) { }
void _mi_prof_process_done(void) { }
#endif
