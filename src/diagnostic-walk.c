// #374: compiled inside arena.c (including the Rust single-TU build).
// No allocator fast-path changes: ownership is acquired only by this diagnostic.
#include "diagnostic-walk.h"

typedef struct mi_diag_walk_s {
  mi_heap_t* heap;
  bool blocks;
  mi_block_visit_fun* visitor;
  void* arg;
  mi_diag_coverage_t* coverage;
  mi_arena_pages_t* arena_pages;
  mi_diag_alloc_fun* allocate;
} mi_diag_walk_t;

// heap->theaps_lock pins the tld. Never wait while holding a page pin: the owner
// or an in-flight sweeper might be retiring that page and waiting for our bit.
static bool mi_diag_try_tld(mi_subproc_t* subproc, mi_tld_t* tld, bool* claimed) {
  *claimed = false;
  if (tld == NULL) return false;
  if (tld->thread_id == MI_THREADID_DETACHED) {
    *claimed = mi_lock_try_acquire(&subproc->theap_meta_lock);
    return *claimed;
  }
  if (tld->thread_id == _mi_thread_id()) return true; // caller already gated
  size_t expected = MI_PARK_PARKED;
  if (!mi_atomic_cas_strong_acq_rel(&tld->park_state, &expected, (size_t)MI_PARK_SWEEPING)) return false;
  mi_atomic_store_release(&tld->sweeper, (uintptr_t)_mi_thread_id());
  *claimed = true;
  return true;
}

static void mi_diag_release_tld(mi_subproc_t* subproc, mi_tld_t* tld, bool claimed) {
  if (!claimed) return;
  if (tld->thread_id == MI_THREADID_DETACHED) {
    mi_lock_release(&subproc->theap_meta_lock);
    return;
  }
  mi_atomic_store_release(&tld->sweeper, (uintptr_t)0);
  mi_atomic_store_release(&tld->park_state, (size_t)MI_PARK_PARKED);
}

static bool mi_diag_visit_page(mi_page_t* page, mi_diag_walk_t* walk) {
  mi_heap_area_t area;
  _mi_heap_area_init(&area, page);
  if (!walk->visitor(walk->heap, &area, NULL, area.block_size, walk->arg)) return false;
  return !walk->blocks || _mi_theap_area_visit_blocks(&area, page, walk->visitor, walk->arg);
}

static bool mi_diag_arena_page(size_t index, size_t count, mi_arena_t* arena, void* arg) {
  MI_UNUSED(count);
  mi_diag_walk_t* walk = (mi_diag_walk_t*)arg;
  mi_bitmap_t* bitmap = walk->arena_pages->pages;
  if (!mi_bitmap_clear(bitmap, index)) {
    walk->coverage->skipped_pages++;
    return true;
  }
  // Pin only establishes storage lifetime, NOT a registered page map or stable
  // owner-private fields. Read just atomic ownership until exclusivity is proven.
  mi_page_t* page = mi_arena_page_at_slice(arena, index);
  const mi_threadid_t tid = mi_page_thread_id(page);
  mi_tld_t* tld = NULL;
  bool claimed = false;
  bool owned = false;
  bool ready = false;
  if (tid <= MI_THREADID_ABANDONED_MAPPED) {
    owned = mi_page_claim_ownership(page);
    ready = owned;
  }
  else {
    for (mi_theap_t* theap = walk->heap->theaps; theap != NULL; theap = theap->hnext) {
      if (theap->tld != NULL && theap->tld->thread_id == tid) { tld = theap->tld; break; }
    }
    if (mi_diag_try_tld(walk->heap->subproc, tld, &claimed)) {
      // The page could have been abandoned/reclaimed between our atomic tid read
      // and the owner claim. Never use the earlier observation as ownership proof.
      ready = (mi_page_thread_id(page) == tid);
    }
  }
  bool ok = true;
  if (ready) { ok = mi_diag_visit_page(page, walk); }
  else { walk->coverage->skipped_pages++; }
  mi_bitmap_set(bitmap, index); // before unown, which may retire the page
  if (owned) { mi_abandoned_page_unown(page, NULL); }
  mi_diag_release_tld(walk->heap->subproc, tld, claimed);
  return ok;
}

static bool mi_diag_owned_os_page(mi_theap_t* theap, mi_page_queue_t* pq, mi_page_t* page, void* arg1, void* arg2) {
  MI_UNUSED(theap); MI_UNUSED(pq); MI_UNUSED(arg2);
  return page->memid.memkind == MI_MEM_ARENA || mi_diag_visit_page(page, (mi_diag_walk_t*)arg1);
}

// Gather page OWNERSHIP under the OS list lock, not an unlocked list of borrowed
// next pointers. Batch storage is raw OS, and all unowns happen after releasing
// the list lock: unown can free/unlink a page and would otherwise self-deadlock.
typedef struct mi_diag_os_batch_s {
  struct mi_diag_os_batch_s* next;
  size_t used;
  mi_page_t* pages[64];
} mi_diag_os_batch_t;

static bool mi_diag_abandoned_os(mi_diag_walk_t* walk) {
  mi_diag_os_batch_t* batches = NULL;
  bool ok = true;
  mi_lock(&walk->heap->os_abandoned_pages_lock) {
    for (mi_page_t* page = walk->heap->os_abandoned_pages; page != NULL; page = page->next) {
      if (batches == NULL || batches->used == 64) {
        mi_diag_os_batch_t* batch = (mi_diag_os_batch_t*)walk->allocate(walk->arg, sizeof(*batch));
        if (batch == NULL) { ok = false; break; }
        batch->next = batches;
        batch->used = 0;
        batches = batch;
      }
      if (mi_page_claim_ownership(page)) { batches->pages[batches->used++] = page; }
      else { walk->coverage->skipped_pages++; }
    }
  }
  while (batches != NULL) {
    mi_diag_os_batch_t* next = batches->next;
    for (size_t i = 0; i < batches->used; i++) {
      mi_page_t* page = batches->pages[i];
      if (ok) { ok = mi_diag_visit_page(page, walk); }
      mi_abandoned_page_unown(page, NULL);
    }
    // Batch storage belongs to the capture arena and is released by its caller.
    batches = next;
  }
  return ok;
}

// This is a production capture API, not an MI_DEBUG-only `_mi_*diagnostic` hook.
bool _mi_heap_visit_capture(mi_heap_t* heap, bool blocks, mi_block_visit_fun* visitor,
                              void* arg, mi_diag_coverage_t* coverage, mi_diag_alloc_fun* allocate) {
  mi_diag_walk_t walk = { heap, blocks, visitor, arg, coverage, NULL, allocate };
  bool ok = true;
  mi_lock(&heap->theaps_lock) {
    // Arena pins + individual owner claims keep claims short (one page capture)
    // and avoid the old fixed 64-owner cap and its unsafe unclaimed fallback.
    mi_forall_arenas(heap, ((mi_arena_t*)NULL), 0, arena) {
      if (!ok) break;
      walk.arena_pages = mi_heap_arena_pages(heap, arena);
      if (walk.arena_pages != NULL) {
        ok = _mi_bitmap_forall_set(walk.arena_pages->pages, &mi_diag_arena_page, arena, &walk);
      }
    }
    mi_forall_arenas_end();
    // Live OS pages are absent from the arena bitmap and abandoned OS list.
    for (mi_theap_t* theap = heap->theaps; theap != NULL && ok; theap = theap->hnext) {
      bool claimed;
      if (!mi_diag_try_tld(heap->subproc, theap->tld, &claimed)) { coverage->busy_theaps++; continue; }
      ok = _mi_theap_visit_pages(theap, &mi_diag_owned_os_page, true, &walk, NULL);
      mi_diag_release_tld(heap->subproc, theap->tld, claimed);
    }
    if (ok) { ok = mi_diag_abandoned_os(&walk); }
  }
  return ok;
}
