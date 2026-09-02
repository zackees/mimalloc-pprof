/* Live heap dump: mi_heap_dump_json / mi_heap_get_seq (issue #269, Bun parity P4).

   Backs Bun's shipped `bun:jsc` `heapStats({ dump: true | "blocks" }).mimallocDump`
   (declared in `include/mimalloc-stats.h`, called from `BunJSCModule.h`). Independent of
   MI_PPROF: Bun builds this unconditionally (see src/static.c), so it is a plain
   diagnostics API, not part of the sampling profiler.

   Per rule 6 this is a new file so upstream files stay untouched beyond the two
   declarations in include/mimalloc-stats.h. The JSON-buffer helpers below intentionally
   duplicate (rather than share) stats.c's private `mi_json_buf_t` machinery, to keep this
   feature self-contained in its own file instead of exporting stats.c internals.

   Ported from oven-sh/mimalloc @ 942b8342 (src/stats.c:840-949), MIT license -- see the
   "imported from" comment below for the exact scope. Adapted: renamed the buffer type to
   avoid colliding with stats.c's private `mi_json_buf_t`, and reads the heap sequence
   number from our own `mi_heap_t::heap_seq` field (already present at our pin; Bun added
   the equivalent field to their `mi_heap_t` in the same commit).

   THREAD SAFETY (#78): this walks heaps with `mi_subproc_visit_heaps` and pages/blocks
   with `mi_heap_visit_blocks`, both of which are documented in include/mimalloc.h as NOT
   safe against a concurrent free into the heap being visited -- the dump is best-effort
   under concurrent mutation, matching Bun's own implementation. Separately (see the
   walk-order comment above mi_memory_visit_live_allocations in src/memory-events.c),
   mi_heap_visit_blocks only sees pages registered in the heap's arena-page bitmap or its
   os_abandoned_pages list; a page a live theap owns that was allocated directly from the
   OS (memid.memkind == MI_MEM_OS, e.g. before this process's first arena reservation) is
   visible to neither, so it can be silently absent from a dump. This mirrors Bun's own
   mi_heap_dump_json, which has the identical gap for the identical reason.
*/
#include "mimalloc.h"
#include "mimalloc/internal.h"

// -----------------------------------------------------------
// small growable buffer for building the JSON string.
// mirrors (does not share) stats.c's private mi_json_buf_t.
// -----------------------------------------------------------

typedef struct mi_hdump_buf_s {
  char*   buf;
  size_t  size;
  size_t  used;
} mi_hdump_buf_t;

static bool mi_hdump_buf_expand(mi_hdump_buf_t* hbuf) {
  if (hbuf == NULL) return false;
  if (hbuf->buf != NULL && hbuf->size > 0) {
    hbuf->buf[hbuf->size - 1] = 0;
  }
  if (hbuf->size > SIZE_MAX / 2) return false;
  const size_t newsize = (hbuf->size == 0 ? mi_good_size(12 * MI_KiB) : 2 * hbuf->size);
  char* const  newbuf  = (char*)mi_rezalloc(hbuf->buf, newsize);
  if (newbuf == NULL) return false;
  hbuf->buf = newbuf;
  hbuf->size = newsize;
  return true;
}

static void mi_hdump_buf_print(mi_hdump_buf_t* hbuf, const char* msg) {
  if (msg == NULL || hbuf == NULL) return;
  for (const char* src = msg; *src != 0; src++) {
    if (hbuf->used + 1 >= hbuf->size) {
      if (!mi_hdump_buf_expand(hbuf)) return;
    }
    mi_assert_internal(hbuf->used < hbuf->size);
    hbuf->buf[hbuf->used++] = *src;
  }
  mi_assert_internal(hbuf->used < hbuf->size);
  hbuf->buf[hbuf->used] = 0;
}

/* -----------------------------------------------------------
  imported from oven-sh/mimalloc @ 942b8342, MIT
  (src/stats.c:857-949: mi_dump_ctx_t, mi_dump_id, mi_dump_block_visit,
  mi_dump_heap_visit, mi_heap_dump_json, mi_heap_get_seq)

  Live heap dump: per-heap -> per-page -> (optional) per-block JSON.
  Addresses are mixed through a per-process key when `hash_addresses`
  so snapshots can be diffed without exposing ASLR.
----------------------------------------------------------- */

typedef struct mi_dump_ctx_s {
  mi_hdump_buf_t hbuf;
  bool           include_blocks;
  bool           hash_addresses;
  bool           in_block_pass;
  uintptr_t      key;
  bool           first_heap;
  bool           first_page;
  bool           first_block;
} mi_dump_ctx_t;

static uintptr_t mi_dump_id(mi_dump_ctx_t* ctx, const void* p) {
  uintptr_t x = (uintptr_t)p;
  if (!ctx->hash_addresses) return x;
  x ^= ctx->key;
  x ^= x >> 33; x *= 0xff51afd7ed558ccdULL;
  x ^= x >> 33; x *= 0xc4ceb9fe1a85ec53ULL;
  x ^= x >> 33;
  return x;
}

static bool mi_cdecl mi_dump_block_visit(const mi_heap_t* heap, const mi_heap_area_t* area, void* block, size_t block_size, void* arg) {
  MI_UNUSED(heap);
  mi_dump_ctx_t* ctx = (mi_dump_ctx_t*)arg;
  char tmp[128];
  if (block == NULL) {
    if (ctx->in_block_pass) return true;
    if (!ctx->first_page) { mi_hdump_buf_print(&ctx->hbuf, ",\n"); }
    ctx->first_page = false;
    const mi_page_t* page = (const mi_page_t*)area->reserved1;
    const uintptr_t tid = (page != NULL ? mi_page_thread_id(page) : 0);
    _mi_snprintf(tmp, sizeof(tmp),
      "      { \"id\": %zu, \"block_size\": %zu, \"used\": %zu, \"reserved\": %zu, \"thread_id\": %zu }",
      mi_dump_id(ctx, area->blocks), area->block_size, area->used,
      area->reserved / (area->block_size > 0 ? area->block_size : 1), tid);
    mi_hdump_buf_print(&ctx->hbuf, tmp);
  }
  else if (ctx->in_block_pass) {
    if (!ctx->first_block) { mi_hdump_buf_print(&ctx->hbuf, ","); }
    ctx->first_block = false;
    _mi_snprintf(tmp, sizeof(tmp), "[%zu,%zu]", mi_dump_id(ctx, block), block_size);
    mi_hdump_buf_print(&ctx->hbuf, tmp);
  }
  return true;
}

static bool mi_cdecl mi_dump_heap_visit(mi_heap_t* heap, void* arg) {
  mi_dump_ctx_t* ctx = (mi_dump_ctx_t*)arg;
  char tmp[64];
  if (!ctx->first_heap) { mi_hdump_buf_print(&ctx->hbuf, ",\n"); }
  ctx->first_heap = false;
  _mi_snprintf(tmp, sizeof(tmp), "  { \"seq\": %zu,\n    \"pages\": [\n", heap->heap_seq);
  mi_hdump_buf_print(&ctx->hbuf, tmp);
  ctx->first_page = true; ctx->in_block_pass = false;
  mi_heap_visit_blocks(heap, false, &mi_dump_block_visit, ctx);
  mi_hdump_buf_print(&ctx->hbuf, "\n    ]");
  if (ctx->include_blocks) {
    mi_hdump_buf_print(&ctx->hbuf, ",\n    \"blocks\": [");
    ctx->first_block = true; ctx->in_block_pass = true;
    mi_heap_visit_blocks(heap, true, &mi_dump_block_visit, ctx);
    mi_hdump_buf_print(&ctx->hbuf, "]");
  }
  mi_hdump_buf_print(&ctx->hbuf, " }");
  return true;
}

char* mi_heap_dump_json(bool include_blocks, bool hash_addresses) mi_attr_noexcept {
  mi_dump_ctx_t ctx;
  _mi_memzero(&ctx, sizeof(ctx));
  ctx.include_blocks = include_blocks;
  ctx.hash_addresses = hash_addresses;
  ctx.key             = _mi_os_random_weak((uintptr_t)&ctx) | 1;
  if (!mi_hdump_buf_expand(&ctx.hbuf)) return NULL;
  mi_hdump_buf_print(&ctx.hbuf, "{ \"heaps\": [\n");
  ctx.first_heap = true;
  mi_subproc_visit_heaps(mi_subproc_current(), &mi_dump_heap_visit, &ctx);
  mi_hdump_buf_print(&ctx.hbuf, "\n] }\n");
  if (ctx.hbuf.used >= ctx.hbuf.size) { mi_free(ctx.hbuf.buf); return NULL; }
  return ctx.hbuf.buf;
}

size_t mi_heap_get_seq(mi_heap_t* heap) mi_attr_noexcept {
  return (heap != NULL ? heap->heap_seq : 0);
}
