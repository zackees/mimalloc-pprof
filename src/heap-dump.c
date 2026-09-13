/* Live heap JSON capture (#269, #374).
   The public result is still released with mi_free. Capture and serialization use
   raw-OS scratch storage; no hooked allocation or user callback runs while page
   ownership is held. A busy owner is OMITTED, never read optimistically.
   "complete" describes observed coverage, not a process-wide atomic instant:
   pages are captured independently. Cooperatively park foreign owners for coverage.
   The generic mi_heap_visit_blocks API retains its existing concurrency contract. */
#include "mimalloc.h"
#include "mimalloc/internal.h"
#include "mimalloc/prim-tls.h"
#include "diagnostic-walk.h"

#if MI_DEBUG > 0
mi_decl_export _Atomic(uintptr_t) mi_debug_dump_fail_after;
#endif

// A bump arena amortizes raw OS allocation across thousands of block records.
// It never installs profiler records or participates in memory-event accounting.
typedef struct mi_dump_chunk_s {
  struct mi_dump_chunk_s* next;
  mi_memid_t memid;
  size_t used;
  uint8_t data[64 * MI_KiB];
} mi_dump_chunk_t;

typedef struct mi_dump_block_s {
  struct mi_dump_block_s* next;
  uintptr_t id;
  size_t size;
} mi_dump_block_t;

typedef struct mi_dump_page_s {
  struct mi_dump_page_s* next;
  mi_dump_block_t* blocks;
  mi_dump_block_t* last_block;
  uintptr_t id;
  size_t block_size, used, reserved;
  mi_threadid_t tid;
} mi_dump_page_t;

typedef struct mi_dump_heap_s {
  struct mi_dump_heap_s* next;
  mi_dump_page_t* pages;
  mi_dump_page_t* last_page;
  size_t seq;
} mi_dump_heap_t;

typedef struct mi_dump_text_s {
  struct mi_dump_text_s* next;
  size_t used;
  char data[8 * MI_KiB];
} mi_dump_text_t;

typedef struct mi_dump_ctx_s {
  mi_dump_chunk_t* chunks;
  mi_dump_heap_t* heaps;
  mi_dump_heap_t* last_heap;
  mi_dump_text_t* text;
  mi_dump_text_t* last_text;
  mi_subproc_t* subproc;
  mi_diag_coverage_t coverage;
  size_t text_size;
  size_t alloc_count;
  uintptr_t key;
  bool include_blocks, hash_addresses, failed, formatting;
} mi_dump_ctx_t;

static void* mi_dump_alloc(void* arg, size_t size) {
  mi_dump_ctx_t* ctx = (mi_dump_ctx_t*)arg;
  ctx->alloc_count++;
  #if MI_DEBUG > 0
  const uintptr_t fail = mi_atomic_load_relaxed(&mi_debug_dump_fail_after);
  if (fail > 0 && (ctx->alloc_count == fail || (fail == UINTPTR_MAX && ctx->formatting))) {
    ctx->failed = true;
    return NULL;
  }
  #endif
  size = _mi_align_up(size, sizeof(void*));
  mi_assert_internal(size <= sizeof(ctx->chunks->data));
  if (ctx->failed) return NULL;
  if (ctx->chunks == NULL || size > sizeof(ctx->chunks->data) - ctx->chunks->used) {
    mi_memid_t memid;
    mi_dump_chunk_t* chunk = (mi_dump_chunk_t*)_mi_os_alloc(ctx->subproc, sizeof(*chunk), &memid);
    if (chunk == NULL) { ctx->failed = true; return NULL; }
    chunk->memid = memid;
    chunk->used = 0;
    chunk->next = ctx->chunks;
    ctx->chunks = chunk;
  }
  void* p = ctx->chunks->data + ctx->chunks->used;
  ctx->chunks->used += size;
  _mi_memzero(p, size);
  return p;
}

static void mi_dump_dispose(mi_dump_ctx_t* ctx) {
  while (ctx->chunks != NULL) {
    mi_dump_chunk_t* chunk = ctx->chunks;
    ctx->chunks = chunk->next;
    _mi_os_free(ctx->subproc, chunk, sizeof(*chunk), chunk->memid);
  }
}

static uintptr_t mi_dump_id(const mi_dump_ctx_t* ctx, uintptr_t x) {
  if (!ctx->hash_addresses) return x;
  x ^= ctx->key;
#if MI_INTPTR_SIZE == 8
  x ^= x >> 33; x *= 0xff51afd7ed558ccdULL;
  x ^= x >> 33; x *= 0xc4ceb9fe1a85ec53ULL;
  x ^= x >> 33;
#else
  x ^= x >> 16; x *= 0x7feb352dU;
  x ^= x >> 15; x *= 0x846ca68bU;
  x ^= x >> 16;
#endif
  return x;
}

static bool mi_cdecl mi_dump_capture_block(const mi_heap_t* heap, const mi_heap_area_t* area,
                                           void* block, size_t block_size, void* arg) {
  MI_UNUSED(heap);
  mi_dump_ctx_t* ctx = (mi_dump_ctx_t*)arg;
  mi_dump_heap_t* out = ctx->last_heap;
  if (block == NULL) {
    mi_dump_page_t* p = (mi_dump_page_t*)mi_dump_alloc(ctx, sizeof(*p));
    if (p == NULL) return false;
    const mi_page_t* page = (const mi_page_t*)area->reserved1;
    p->id = (uintptr_t)area->blocks;
    p->block_size = area->block_size;
    // The block walker collects pending remote frees after its area callback.
    // For block dumps count the captured records, not the pre-collect counter.
    p->used = ctx->include_blocks ? 0 : area->used;
    p->reserved = area->reserved / (area->block_size > 0 ? area->block_size : 1);
    p->tid = mi_page_thread_id(page);
    if (out->last_page == NULL) { out->pages = p; }
    else { out->last_page->next = p; }
    out->last_page = p;
  }
  else {
    mi_dump_block_t* b = (mi_dump_block_t*)mi_dump_alloc(ctx, sizeof(*b));
    if (b == NULL) return false;
    b->id = (uintptr_t)block;
    b->size = block_size;
    mi_dump_page_t* p = out->last_page;
    if (p->last_block == NULL) { p->blocks = b; }
    else { p->last_block->next = b; }
    p->last_block = b;
    p->used++;
  }
  return true;
}

static bool mi_cdecl mi_dump_capture_heap(mi_heap_t* heap, void* arg) {
  mi_dump_ctx_t* ctx = (mi_dump_ctx_t*)arg;
  mi_dump_heap_t* out = (mi_dump_heap_t*)mi_dump_alloc(ctx, sizeof(*out));
  if (out == NULL) return false;
  out->seq = heap->heap_seq;
  if (ctx->last_heap == NULL) { ctx->heaps = out; }
  else { ctx->last_heap->next = out; }
  ctx->last_heap = out;
  return _mi_heap_visit_capture(heap, ctx->include_blocks, &mi_dump_capture_block, ctx, &ctx->coverage, &mi_dump_alloc);
}

static bool mi_dump_print(mi_dump_ctx_t* ctx, const char* msg) {
  if (ctx->failed) return false;
  size_t len = _mi_strlen(msg);
  if (len > SIZE_MAX - 1 - ctx->text_size) { ctx->failed = true; return false; }
  ctx->text_size += len;
  while (len > 0) {
    if (ctx->last_text == NULL || ctx->last_text->used == sizeof(ctx->last_text->data)) {
      mi_dump_text_t* text = (mi_dump_text_t*)mi_dump_alloc(ctx, sizeof(*text));
      if (text == NULL) return false;
      if (ctx->last_text == NULL) { ctx->text = text; }
      else { ctx->last_text->next = text; }
      ctx->last_text = text;
    }
    const size_t available = sizeof(ctx->last_text->data) - ctx->last_text->used;
    const size_t take = len < available ? len : available;
    _mi_memcpy(ctx->last_text->data + ctx->last_text->used, msg, take);
    ctx->last_text->used += take;
    msg += take;
    len -= take;
  }
  return true;
}

static bool mi_dump_serialize(mi_dump_ctx_t* ctx) {
  char tmp[256];
  mi_dump_print(ctx, "{ \"heaps\": [\n");
  bool first_heap = true;
  for (mi_dump_heap_t* h = ctx->heaps; h != NULL && !ctx->failed; h = h->next) {
    if (!first_heap) mi_dump_print(ctx, ",\n");
    first_heap = false;
    _mi_snprintf(tmp, sizeof(tmp), "  { \"seq\": %zu,\n    \"pages\": [\n", h->seq);
    mi_dump_print(ctx, tmp);
    bool first = true;
    for (mi_dump_page_t* p = h->pages; p != NULL && !ctx->failed; p = p->next) {
      if (!first) mi_dump_print(ctx, ",\n");
      first = false;
      _mi_snprintf(tmp, sizeof(tmp),
        "      { \"id\": %zu, \"block_size\": %zu, \"used\": %zu, \"reserved\": %zu, \"thread_id\": %zu }",
        mi_dump_id(ctx, p->id), p->block_size, p->used, p->reserved, (size_t)p->tid);
      mi_dump_print(ctx, tmp);
    }
    mi_dump_print(ctx, "\n    ]");
    if (ctx->include_blocks) {
      mi_dump_print(ctx, ",\n    \"blocks\": [");
      first = true;
      for (mi_dump_page_t* p = h->pages; p != NULL && !ctx->failed; p = p->next) {
        for (mi_dump_block_t* b = p->blocks; b != NULL && !ctx->failed; b = b->next) {
          if (!first) mi_dump_print(ctx, ",");
          first = false;
          _mi_snprintf(tmp, sizeof(tmp), "[%zu,%zu]", mi_dump_id(ctx, b->id), b->size);
          mi_dump_print(ctx, tmp);
        }
      }
      mi_dump_print(ctx, "]");
    }
    mi_dump_print(ctx, " }");
  }
  _mi_snprintf(tmp, sizeof(tmp),
    "\n], \"complete\": %s, \"skipped_pages\": %zu, \"busy_theaps\": %zu }\n",
    ctx->coverage.skipped_pages == 0 && ctx->coverage.busy_theaps == 0 ? "true" : "false",
    ctx->coverage.skipped_pages, ctx->coverage.busy_theaps);
  return mi_dump_print(ctx, tmp);
}

char* mi_heap_dump_json(bool include_blocks, bool hash_addresses) mi_attr_noexcept {
  mi_theap_t* self = _mi_theap_default();
  if (!mi_theap_is_initialized(self)) { self = _mi_thread_init(); }
  if (self == NULL) return NULL; // fresh-thread initialization can fail under OOM
  MI_GATE_ENTER(self);
  mi_dump_ctx_t ctx;
  _mi_memzero(&ctx, sizeof(ctx));
  ctx.subproc = self->tld->subproc;
  ctx.include_blocks = include_blocks;
  ctx.hash_addresses = hash_addresses;
  ctx.key = _mi_os_random_weak((uintptr_t)&ctx) | 1;
  const bool captured = mi_subproc_visit_heaps(mi_subproc_current(), &mi_dump_capture_heap, &ctx);
  MI_GATE_LEAVE(self->tld);
  ctx.formatting = true;
  // No page/theap claims or registry locks remain. Format saved values only.
  char* result = NULL;
  if (captured && mi_dump_serialize(&ctx)) {
    result = (char*)mi_malloc(ctx.text_size + 1);
    if (result != NULL) {
      size_t offset = 0;
      for (mi_dump_text_t* text = ctx.text; text != NULL; text = text->next) {
        _mi_memcpy(result + offset, text->data, text->used);
        offset += text->used;
      }
      result[offset] = 0;
    }
  }
  mi_dump_dispose(&ctx);
  return result;
}

size_t mi_heap_get_seq(mi_heap_t* heap) mi_attr_noexcept {
  return heap != NULL ? heap->heap_seq : 0;
}
