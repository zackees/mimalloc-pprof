/* Sequential free-list initialization, shared with the structural work-count test. */
#ifndef MI_PAGE_FREE_LIST_H
#define MI_PAGE_FREE_LIST_H

#include "page-block-at.h"

#ifndef MI_PAGE_FREE_LIST_SET_NEXT
#define MI_PAGE_FREE_LIST_SET_NEXT mi_block_set_next
#endif

static mi_decl_noinline void mi_page_free_list_extend( mi_page_t* const page, const size_t bsize, const size_t extend)
{
  #if (MI_SECURE < 2)
  mi_assert_internal(page->free == NULL);
  mi_assert_internal(page->local_free == NULL);
  #endif
  mi_assert_internal(page->capacity + extend <= page->reserved);
  mi_assert_internal(bsize == mi_page_block_size(page));
  void* const page_area = mi_page_start(page);

  mi_block_t* const start = mi_page_block_at(page, page_area, bsize, page->capacity);

  // initialize a sequential free list
  mi_block_t* const last = mi_page_block_at(page, page_area, bsize, page->capacity + extend - 1);
  mi_block_t* block = start;
  // The final link is set to the old head below; do not write it twice.
  while(block < last) {
    mi_block_t* next = (mi_block_t*)((uint8_t*)block + bsize);
    MI_PAGE_FREE_LIST_SET_NEXT(page,block,next);
    block = next;
  }
  // prepend to free list (usually `NULL`)
  MI_PAGE_FREE_LIST_SET_NEXT(page, last, page->free);
  page->free = start;
}

#endif
