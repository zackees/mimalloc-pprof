/* #614: run the production queue-removal body with counted flag writes.
   These predicates model the synthetic queues; no allocator queue is modified. */
#ifndef MI_TEST_RETIRED_QUEUE_REMOVE_H
#define MI_TEST_RETIRED_QUEUE_REMOVE_H
#if MI_LARGE_REPURPOSE
static size_t retired_queue_flag_writes;
static void retired_queue_set_in_full(mi_page_t* page, bool value) {
  retired_queue_flag_writes++;
  mi_page_set_in_full(page, value);
}
// Maybe unused: these predicates appear only in debug assertions in the shared body.
MI_DECL_MAYBE_UNUSED static bool mi_page_queue_contains(const mi_page_queue_t* q, const mi_page_t* p) {
  for (const mi_page_t* it = q->first; it != NULL; it = it->next) if (it == p) return true;
  return false;
}
MI_DECL_MAYBE_UNUSED static bool mi_theap_contains_queue(const mi_theap_t* t, const mi_page_queue_t* q) {
  return q >= &t->pages[0] && q <= &t->pages[MI_BIN_FULL];
}
MI_DECL_MAYBE_UNUSED static bool mi_page_queue_is_huge(const mi_page_queue_t* q) {
  return q->block_size == MI_LARGE_MAX_OBJ_SIZE + sizeof(uintptr_t);
}
MI_DECL_MAYBE_UNUSED static bool mi_page_queue_is_full(const mi_page_queue_t* q) {
  return q->block_size == MI_LARGE_MAX_OBJ_SIZE + 2 * sizeof(uintptr_t);
}
static void mi_theap_queue_first_update(mi_theap_t* t, const mi_page_queue_t* q) {
  MI_UNUSED(t);
  // The real updater returns immediately for every large queue tested here.
  assert(q->block_size > MI_SMALL_SIZE_MAX);
}
#define MI_PAGE_QUEUE_SET_IN_FULL retired_queue_set_in_full
#include "../src/page-queue-remove.h"
#undef MI_PAGE_QUEUE_SET_IN_FULL

static void case_retired_queue_remove(void) {
  mi_theap_t* real = mi_theap_get_default();
  MI_GATE_ENTER(real);
  for (size_t flag = 0; flag <= MI_PAGE_HAS_INTERIOR_POINTERS; flag += MI_PAGE_HAS_INTERIOR_POINTERS) {
    mi_theap_t owner;
    mi_page_t page;
    memset(&owner, 0, sizeof(owner));
    memset(&page, 0, sizeof(page));
    owner.tld = real->tld;
    mi_atomic_store_ptr_relaxed(mi_heap_t, &owner.heap, _mi_theap_heap(real));
    owner.page_count = 2;
    owner.pages_full_size = SIZE_A;
    const size_t bin = _mi_bin(SIZE_A);
    mi_page_queue_t* q = &owner.pages[bin];
    q->first = q->last = &page; q->count = 1; q->block_size = SIZE_A;
    page.theap = &owner; page.block_size = SIZE_A;
    page.retire_expire = 1;
    mi_atomic_store_relaxed(&page.xthread_id, _mi_thread_id() | flag);
    const mi_threadid_t before = mi_atomic_load_relaxed(&page.xthread_id);
    retired_queue_flag_writes = 0;
    mi_page_queue_remove_retired(&owner, q, &page);
    CHECK(q->first == NULL && q->last == NULL && q->count == 0 && q->block_size == SIZE_A,
          "retired removal must empty only the selected queue");
    CHECK(owner.page_count == 1 && owner.pages_full_size == SIZE_A,
          "retired removal must preserve full-page accounting");
    CHECK(page.next == NULL && page.prev == NULL && page.theap == &owner &&
          mi_atomic_load_relaxed(&page.xthread_id) == before,
          "retired removal must preserve page links, owner and flags");
    for (size_t i = 0; i <= MI_BIN_FULL; i++) if (i != bin) {
      CHECK(owner.pages[i].first == NULL && owner.pages[i].last == NULL && owner.pages[i].count == 0,
            "retired removal must preserve other queues");
    }
    for (size_t i = 0; i < MI_PAGES_DIRECT; i++) {
      CHECK(owner.pages_free_direct[i] == NULL, "retired removal must preserve direct cache");
    }
    CHECK(retired_queue_flag_writes == 0, "retired removal wrote full flag %zu times, expected zero", retired_queue_flag_writes);
    // Keep the original production remover as a counted control: the eliminated
    // operation really is present, and its observable queue result is the same.
    q->first = q->last = &page; q->count = 1; owner.page_count = 2;
    retired_queue_flag_writes = 0;
    mi_page_queue_remove(q, &page);
    CHECK(retired_queue_flag_writes == 1 && q->first == NULL && q->last == NULL &&
          q->count == 0 && owner.page_count == 1 && owner.pages_full_size == SIZE_A &&
          mi_atomic_load_relaxed(&page.xthread_id) == before,
          "generic removal control must produce the same state with one flag write");
  }
  MI_GATE_LEAVE(real->tld);
}
#else
static void case_retired_queue_remove(void) { }
#endif
#endif
