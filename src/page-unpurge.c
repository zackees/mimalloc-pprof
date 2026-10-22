/* Keep the common clean-page case outside the OS restore body (#614). */
#include "mimalloc.h"
#include "mimalloc/internal.h"

void _mi_page_unpurge_all(mi_page_t* page) {
  // These are the same two conditions the restore body checks before doing work.
  // Read them only while the caller owns the page (after unpublishing retirement).
  // A clean page needs no state changes, including no swept_state reset.
  if (page->unformed_purged_hi <= page->unformed_purged_lo && !mi_page_has_purged(page)) return;
  _mi_page_unpurge_all_slow(page);
}
