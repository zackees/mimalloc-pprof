/* Skip the restore call when an owner publishes a clean retired page (#614). */
#ifndef MI_PAGE_UNPURGE_H
#define MI_PAGE_UNPURGE_H

static inline void mi_page_restore_before_retirement(mi_page_t* page) {
  // The owner has not published the page yet, so the scavenger cannot change
  // either discard record. Preserve the full restore for both kinds of holes.
  if (page->unformed_purged_hi > page->unformed_purged_lo || mi_page_has_purged(page)) {
    _mi_page_unpurge_all(page);
  }
}

#endif
