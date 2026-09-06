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

   SELF-MUTATION: the dump's own JSON buffer growth (mi_hdump_buf_expand -> mi_rezalloc)
   allocates from the calling thread's default heap, and mi_heap_dump_json walks that same
   heap if it is one of the subprocess's heaps -- so the walk can observe its own buffer's
   allocations, and because pages and blocks are two separate mi_heap_visit_blocks passes,
   a block size seen in the blocks[] pass is not guaranteed to already appear in the
   pages[] pass taken moments earlier. Same class of best-effort as Bun's implementation,
   not something either side corrects for.
*/
#include "mimalloc.h"
#include "mimalloc/internal.h"
#include "mimalloc/prim.h"      // _mi_prim_thread_yield (#366)
#include "mimalloc/prim-tls.h"  // _mi_theap_default (#366)

static char* mi_heap_dump_json_gated(bool include_blocks, bool hash_addresses);

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
  // 192, not Bun's 128: the page-line format below has 5 %zu fields plus ~71 fixed
  // characters, and a 64-bit size_t can print up to 20 digits, so the worst realistic
  // line is 71 + 5*20 = 171 bytes (+ NUL). _mi_snprintf truncates rather than overflows,
  // but a truncated line here is a *silently* malformed JSON document -- Bun's
  // JSONParse(json) on the caller side turns that into a hard `mimallocDump: null`
  // rather than a visible error. Deliberate deviation from Bun's own buffer size.
  char tmp[192];
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

/* #366: exclude FOREIGN SWEEPERS while a heap is walked.

   `mi_heap_visit_blocks` is documented as not thread-safe (include/mimalloc.h, #78): it reads
   pages through the arena page bitmap with no lock, and the dump accepts torn content as
   best-effort. What it cannot accept is a page being FREED and PURGED under the walk: in v3 the
   `mi_page_t` header lives inside the page memory, and a purge decommits on Windows
   (`purge_decommits=1`, `VirtualFree(MEM_DECOMMIT)`), so reading `page->next` of a page that
   left between the bitmap read and the dereference is an access violation, not a torn value.
   Ungated, only a page's OWNER frees it (a thread parked in `mi_on_thread_idle_start` is the
   one exception); in a gated build the scavenger and `mi_purge_all` sweep every thread that is
   between allocator calls, so a steady thread's all-free pages leave all the time.

   So for the duration of a heap's walk this thread holds the sweepers' own ownership token on
   each of the heap's theaps: the tld's MI_PARK_SWEEPING claim (`park_state` CAS + `sweeper`),
   exactly as `_mi_theap_sweep_parked` and `mi_purge_all` take it. A claim excludes both of
   them; an in-flight sweep is waited out (bounded: a sweeper never waits on us); a RUNNING
   owner -- inside an allocator call -- is left alone and raced as before (the documented,
   pre-existing contract). A claimed owner that wants to allocate waits in `_mi_park_leave*`
   until the walk ends. The dumper's OWN tld is not claimed but gated (`mi_heap_dump_json`):
   it must be RUNNING, not PARKED, while it reads its own pages, or a sweeper could claim it.

   Lock order: `mi_subproc_visit_heaps` holds `sp->heaps_lock` (2); `heap->theaps_lock` (3) is
   taken under it as `_mi_subproc_prof_sync_force_slow` does; the claim is a CAS, not a lock.
   Waiting out a sweep under those locks is safe because a sweep takes neither (page-holes.c
   "LOCK ORDER"), and `mi_purge_all` holds no claim while it holds `heaps_lock` (phase B). */
#define MI_DUMP_MAX_CLAIMS  (64)   // theaps beyond this are walked unclaimed (documented best-effort)

typedef struct mi_dump_claims_s {
  mi_tld_t* tlds[MI_DUMP_MAX_CLAIMS];
  size_t    count;
} mi_dump_claims_t;

static void mi_dump_claim_theaps(mi_heap_t* heap, mi_dump_claims_t* c) {
  c->count = 0;
  const mi_threadid_t me = _mi_thread_id();
  mi_lock(&heap->theaps_lock) {
    for (mi_theap_t* theap = heap->theaps; theap != NULL && c->count < MI_DUMP_MAX_CLAIMS; theap = theap->hnext) {
      mi_tld_t* const tld = theap->tld;
      if (tld == NULL || tld->thread_id == me || tld->thread_id == MI_THREADID_DETACHED) continue;
      bool seen = false;
      for (size_t i = 0; i < c->count; i++) { if (c->tlds[i] == tld) { seen = true; break; } }
      if (seen) continue;
      size_t spin = 0;
      for (;;) {
        size_t expected = MI_PARK_PARKED;   // word-width local: the MSVC-C atomics wrapper (see scavenger.c)
        if (mi_atomic_cas_strong_acq_rel(&tld->park_state, &expected, (size_t)MI_PARK_SWEEPING)) {
          mi_atomic_store_release(&tld->sweeper, (uintptr_t)me);
          c->tlds[c->count++] = tld;
          break;
        }
        if (expected != MI_PARK_SWEEPING) break;   // RUNNING: the owner is inside the allocator; walk unclaimed
        if (spin < 256) { mi_atomic_pause(); spin++; } else { _mi_prim_thread_yield(); }   // a sweep in flight: it ends on its own
      }
    }
  }
}

static void mi_dump_release_claims(mi_dump_claims_t* c) {
  for (size_t i = 0; i < c->count; i++) {
    mi_tld_t* const tld = c->tlds[i];
    mi_atomic_store_release(&tld->sweeper, (uintptr_t)0);
    mi_atomic_store_release(&tld->park_state, (size_t)MI_PARK_PARKED);   // back to PARKED: the owner owns the transition out
  }
  c->count = 0;
}

static bool mi_cdecl mi_dump_heap_visit(mi_heap_t* heap, void* arg) {
  mi_dump_ctx_t* ctx = (mi_dump_ctx_t*)arg;
  char tmp[64];
  mi_dump_claims_t claims;
  mi_dump_claim_theaps(heap, &claims);
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
  mi_dump_release_claims(&claims);
  mi_hdump_buf_print(&ctx->hbuf, " }");
  return true;
}

char* mi_heap_dump_json(bool include_blocks, bool hash_addresses) mi_attr_noexcept {
  // #366: be RUNNING for the whole dump (see `mi_dump_claim_theaps`): our own theaps are read
  // below too, and a PARKED dumper could have them swept from under the walk.
  mi_theap_t* self = _mi_theap_default();
  MI_GATE_ENTER(self);
  char* const result = mi_heap_dump_json_gated(include_blocks, hash_addresses);
  MI_GATE_LEAVE(self->tld);
  #if !MI_OWNER_GATE
  MI_UNUSED(self);
  #endif
  return result;
}

static char* mi_heap_dump_json_gated(bool include_blocks, bool hash_addresses) {
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
