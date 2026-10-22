/* #614: observe actual link writes and decode the resulting shared initializer's list. */
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "mimalloc.h"
#include "mimalloc/internal.h"

static size_t link_writes;
static void count_link_write(const mi_page_t* page, mi_block_t* block, const mi_block_t* next) {
  link_writes++;
  mi_block_set_next(page, block, next);
}
#define MI_PAGE_FREE_LIST_SET_NEXT count_link_write
#include "../src/page-free-list.h"

static void check_extension(size_t block_size, size_t capacity, size_t extend, bool prepend) {
  const size_t header_size = mi_page_info_size();
  const size_t area_size = (capacity + extend + 1) * block_size;
  mi_page_t* const page = (mi_page_t*)mi_zalloc(header_size + area_size);
  assert(page != NULL);
  page->page_ma_offset = (uint32_t)(header_size / MI_MAX_ALIGN_SIZE);
  page->block_size = block_size;
  page->capacity = (uint16_t)capacity;
  page->reserved = (uint16_t)(capacity + extend + 1);
  #if MI_ENCODE_FREELIST
  page->keys[0] = (uintptr_t)page;
  page->keys[1] = (uintptr_t)mi_page_start(page);
  #endif
  uint8_t* const area = mi_page_start(page);
  memset(area, 0xA5, area_size);
  mi_block_t* const old_head = (prepend ? (mi_block_t*)(area + (capacity + extend) * block_size) : NULL);
  page->free = old_head;
  link_writes = 0;
  mi_page_free_list_extend(page, block_size, extend);
  assert(page->capacity == capacity && page->reserved == capacity + extend + 1);
  const mi_block_t* block = page->free;
  for (size_t i = 0; i < extend; i++) {
    assert(block == (const mi_block_t*)(area + (capacity + i) * block_size));
    block = mi_block_nextx(page, block,
      #if MI_ENCODE_FREELIST
      page->keys
      #else
      NULL
      #endif
    );
  }
  assert(block == old_head);
  for (size_t i = 0; i < capacity * block_size; i++) { assert(area[i] == 0xA5); }
  fprintf(stderr, "links: size=%zu capacity=%zu extend=%zu prepend=%d writes=%zu\n",
          block_size, capacity, extend, (int)prepend, link_writes);
  assert(link_writes == extend);   // RED: the old loop also overwrites the last link once.
  mi_free(page);
}

int main(void) {
  const size_t sizes[] = { sizeof(mi_block_t), 128, 160 * 1024 };
  const size_t capacities[] = { 0, 3 };
  const size_t extensions[] = { 1, 2, 8, 17 };
  for (size_t s = 0; s < sizeof(sizes)/sizeof(sizes[0]); s++) {
    for (size_t c = 0; c < sizeof(capacities)/sizeof(capacities[0]); c++) {
      for (size_t e = 0; e < sizeof(extensions)/sizeof(extensions[0]); e++) {
        check_extension(sizes[s], capacities[c], extensions[e], false);
        #if MI_SECURE >= 2
        check_extension(sizes[s], capacities[c], extensions[e], true);
        #endif
      }
    }
  }
  puts("free-list links: exactly one write per new block, contents preserved");
  return 0;
}
