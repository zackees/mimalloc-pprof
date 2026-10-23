/* Queue removal shared with its focused work-count test. Included after the
   queue predicates and direct-cache updater in page-queue.c. */
#ifndef MI_PAGE_QUEUE_REMOVE_H
#define MI_PAGE_QUEUE_REMOVE_H
#ifndef MI_PAGE_QUEUE_SET_IN_FULL
#define MI_PAGE_QUEUE_SET_IN_FULL mi_page_set_in_full
#endif

static void mi_page_queue_remove(mi_page_queue_t* queue, mi_page_t* page) {
  mi_assert_internal(page != NULL);
  mi_assert_expensive(mi_page_queue_contains(queue, page));
  mi_assert_internal(queue->count >= 1);
  mi_assert_internal(mi_page_block_size(page) == queue->block_size ||
                      (mi_page_is_huge(page) && mi_page_queue_is_huge(queue)) ||
                        (mi_page_is_in_full(page) && mi_page_queue_is_full(queue)));
  mi_theap_t* theap = mi_page_theap(page);
  MI_GATE_ASSERT_HELD(theap);   // #366: owner-private leaf (docs/purge-all-implementation.md §5.2)
  if (page->prev != NULL) page->prev->next = page->next;
  if (page->next != NULL) page->next->prev = page->prev;
  if (page == queue->last)  queue->last = page->prev;
  if (page == queue->first) {
    queue->first = page->next;
    // update first
    mi_assert_internal(mi_theap_contains_queue(theap, queue));
    mi_theap_queue_first_update(theap,queue);
  }
  theap->page_count--;
  queue->count--;
  page->next = NULL;
  page->prev = NULL;
  MI_PAGE_QUEUE_SET_IN_FULL(page,false);
}


#if MI_LARGE_REPURPOSE
static void mi_page_queue_remove_retired(mi_theap_t* theap, mi_page_queue_t* queue, mi_page_t* page) {
  // Selection proved a retired singleton in a normal large bin. Unpublication
  // has completed before this call; only the owner can change its queue/full bit.
  // The generic remover's full-bit clear is a redundant atomic RMW here.
  MI_GATE_ASSERT_HELD(theap);
  mi_assert_internal(mi_page_theap(page) == theap);
  mi_assert_internal(queue->first == page && queue->last == page && queue->count == 1);
  mi_assert_internal(page->next == NULL && page->prev == NULL);
  mi_assert_internal(queue->block_size > MI_MEDIUM_MAX_OBJ_SIZE &&
                     queue->block_size <= MI_LARGE_MAX_OBJ_SIZE);
  mi_assert_internal(queue->block_size == mi_page_block_size(page));
  mi_assert_internal(page->retire_expire != 0 && page->used == 0);
  mi_assert_internal(page->retired_slot == NULL && !mi_page_is_in_full(page));
  mi_assert_internal(theap->page_count > 0);
  queue->first = NULL;
  queue->last = NULL;
  queue->count = 0;
  theap->page_count--;
  // Null links, the already-clear full bit, full-size accounting and the
  // direct small-page cache are unchanged, exactly as for generic removal.
}
#endif
#endif
