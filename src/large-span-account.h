/* Internal implementation included by large-span.c after its private helpers. */
#ifndef MI_LARGE_SPAN_ACCOUNT_H
#define MI_LARGE_SPAN_ACCOUNT_H

void _mi_large_span_account_repurpose(mi_theap_t* theap, size_t index) {
  #if MI_LARGE_SPAN
  const size_t full = mi_slice_count_of_size(MI_LARGE_PAGE_SIZE);
  if (theap == NULL || MI_LARGE_SPAN_COMPACT_SLICES >= full) return;
  if (!mi_option_is_enabled(mi_option_large_span)) return;
  mi_assert_internal(index < MI_LARGE_SPAN_BINS);
  if (index >= MI_LARGE_SPAN_BINS) return;
  // The owner already selected a retired page and knows its target bin. Only
  // the demand transition is needed here. Block size and overhead influence
  // the unused size result, never the transition or its grow/shrink events.
  // Keep the original calculation for every state; there is no zero-state guard.
  (void)mi_large_span_request(theap->large_span[index], 0, 0, &theap->large_span[index]);
  #else
  MI_UNUSED(theap); MI_UNUSED(index);
  #endif
}

#endif
