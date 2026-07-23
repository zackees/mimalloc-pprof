/* Allocation sampling profiler.  Its records never use mimalloc: the arena
   below is backed directly by _mi_os_alloc so profiler bookkeeping cannot
   recursively enter the allocator. */
#include "mimalloc.h"
#include "mimalloc/internal.h"

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
} mi_prof_record_t;

static mi_lock_t prof_lock = MI_LOCK_INITIALIZER;
static _Atomic(bool) prof_enabled = false;
static mi_prof_chunk_t* prof_chunks;
static mi_prof_record_t* prof_all;
static mi_prof_record_t* prof_free;
static size_t prof_records;
static size_t prof_bytes;
static size_t prof_rate = 524288;
static uint64_t prof_seed;
static uint64_t prof_generation;

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

static mi_prof_record_t* prof_record_alloc(void) {
  if (prof_free != NULL) { mi_prof_record_t* rec = prof_free; prof_free = rec->next; return rec; }
  mi_prof_chunk_t* chunk = prof_chunks;
  const size_t align = sizeof(void*) - 1;
  if (chunk == NULL || chunk->used + sizeof(mi_prof_record_t) + align > chunk->size) {
    mi_memid_t memid;
    void* p = _mi_os_alloc(MI_PROF_CHUNK_SIZE, &memid);
    if (p == NULL) return NULL;
    chunk = (mi_prof_chunk_t*)p;
    chunk->next = prof_chunks; chunk->memid = memid; chunk->size = MI_PROF_CHUNK_SIZE; chunk->used = sizeof(*chunk);
    prof_chunks = chunk;
  }
  uintptr_t start = ((uintptr_t)chunk + chunk->used + align) & ~(uintptr_t)align;
  chunk->used = start - (uintptr_t)chunk + sizeof(mi_prof_record_t);
  return (mi_prof_record_t*)start;
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
  prof_records--; prof_bytes -= rec->size;
  rec->next = prof_free; prof_free = rec;
}

bool mi_prof_start_seeded(size_t sample_rate, uint64_t seed) {
  if (sample_rate == 0) sample_rate = (size_t)mi_option_get(mi_option_prof_sample_rate);
  if (sample_rate == 0) sample_rate = 524288;
  mi_lock_acquire(&prof_lock);
  bool started = !mi_atomic_load_relaxed(&prof_enabled);
  if (started) { prof_rate = sample_rate; prof_seed = seed; prof_generation++; mi_atomic_store_release(&prof_enabled, true); }
  mi_lock_release(&prof_lock);
  return started;
}
bool mi_prof_start(size_t sample_rate) { return mi_prof_start_seeded(sample_rate, (uint64_t)mi_option_get(mi_option_prof_seed)); }
bool mi_prof_is_enabled(void) { return mi_atomic_load_relaxed(&prof_enabled); }
void mi_prof_debug_stats(size_t* records, size_t* bytes) { mi_lock_acquire(&prof_lock); if (records) *records=prof_records; if (bytes) *bytes=prof_bytes; mi_lock_release(&prof_lock); }
void mi_prof_stop(void) {
  mi_lock_acquire(&prof_lock);
  mi_atomic_store_release(&prof_enabled, false);
  for (mi_prof_record_t* rec = prof_all; rec != NULL; rec = rec->all_next) { rec->page->metadata = NULL; rec->page->has_metadata = false; }
  mi_prof_chunk_t* chunk = prof_chunks;
  while (chunk != NULL) { mi_prof_chunk_t* next = chunk->next; _mi_os_free(chunk, chunk->size, chunk->memid); chunk = next; }
  prof_chunks=NULL; prof_all=NULL; prof_free=NULL; prof_records=0; prof_bytes=0;
  mi_lock_release(&prof_lock);
}
void _mi_prof_on_alloc(mi_heap_t* heap, mi_page_t* page, void* p, size_t size) {
  if mi_likely(!mi_atomic_load_relaxed(&prof_enabled)) return;
  mi_lock_acquire(&prof_lock);
  if (!mi_atomic_load_relaxed(&prof_enabled)) { mi_lock_release(&prof_lock); return; }
  mi_profiler_tld_t* tld = &heap->tld->profiler;
  if (tld->generation != prof_generation) { tld->bytes_since_sample = 0; tld->next_threshold = 0; tld->random = 0; tld->generation = prof_generation; }
  tld->bytes_since_sample += size;
  if (tld->next_threshold == 0) tld->next_threshold = prof_threshold(tld);
  if (tld->bytes_since_sample < tld->next_threshold) { mi_lock_release(&prof_lock); return; }
  tld->bytes_since_sample = 0; tld->next_threshold = prof_threshold(tld);
  mi_prof_record_t* rec = prof_record_alloc();
  if (rec != NULL) { rec->ptr=p; rec->page=page; rec->size=size; rec->next=(mi_prof_record_t*)page->metadata; rec->all_next=prof_all; page->metadata=(struct mi_prof_record_s*)rec; page->has_metadata=true; prof_all=rec; prof_records++; prof_bytes+=size; }
  mi_lock_release(&prof_lock);
}
void _mi_prof_on_free(mi_page_t* page, void* p) { if mi_likely(!page->has_metadata) return; mi_lock_acquire(&prof_lock); prof_free_record(page,p); mi_lock_release(&prof_lock); }
void _mi_prof_on_free_collect(mi_page_t* page, mi_block_t* head) { if mi_likely(!page->has_metadata) return; mi_lock_acquire(&prof_lock); for (mi_block_t* b=head; b != NULL && page->has_metadata; b=mi_block_next(page,b)) prof_free_record(page,b); mi_lock_release(&prof_lock); }
void _mi_prof_on_realloc_in_place(mi_page_t* page, void* p, size_t size) {
  if mi_likely(!page->has_metadata) return;
  mi_lock_acquire(&prof_lock);
  for (mi_prof_record_t* rec = (mi_prof_record_t*)page->metadata; rec != NULL; rec = rec->next) {
    if (rec->ptr == p) { prof_bytes = prof_bytes - rec->size + size; rec->size = size; break; }
  }
  mi_lock_release(&prof_lock);
}

#else
bool mi_prof_start(size_t sample_rate) { MI_UNUSED(sample_rate); return false; }
bool mi_prof_start_seeded(size_t sample_rate, uint64_t seed) { MI_UNUSED(sample_rate); MI_UNUSED(seed); return false; }
void mi_prof_stop(void) { }
bool mi_prof_is_enabled(void) { return false; }
void mi_prof_debug_stats(size_t* records, size_t* bytes) { if (records) *records=0; if (bytes) *bytes=0; }
#endif
