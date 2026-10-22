/* Demand accounting for an already selected retired page (#614). */
#ifndef MI_LARGE_SPAN_REUSE_H
#define MI_LARGE_SPAN_REUSE_H

#if MI_LARGE_REPURPOSE
static inline void mi_large_span_account_repurpose(mi_theap_t* theap, size_t block_size, size_t index) {
  #if MI_LARGE_SPAN
  // Zero encodes compact level, no pressure, and no pending full-page signal.
  // Accounting preserves it for every runtime enable/cap setting, with no grow
  // or shrink event. Reuse already selected its page and discards the size result.
  // The caller owns this theap and supplies the index from its known target queue.
  mi_assert_internal(index < MI_LARGE_SPAN_BINS);
  if (index < MI_LARGE_SPAN_BINS && theap->large_span[index] == 0) return;
  #else
  MI_UNUSED(index);
  #endif
  (void)_mi_large_span_slices(theap, block_size, 0);
}
#endif

#endif
