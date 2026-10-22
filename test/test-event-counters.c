/* #573 A2: the slow-path event counters, and the deterministic budget test for the retire cascade.

   #572's "retire cascade" -- a large bin stealing another bin's retired page, which then steals in
   turn -- was 548K page requests where main made 292. Only hand-patched counters showed it. The
   counters are permanent now (MI_DIAGNOSTICS=1, src/event-counters.c), so the cascade has a
   deterministic test: within one heartbeat a thread may repurpose at most its budget of retired
   pages, and the requests beyond that are counted as denied instead of stolen. The budget is
   MI_LARGE_REPURPOSE_PER_TICK, 1024 since #575 -- more than the fewer-than-1000 mallocs that fit
   in a heartbeat -- so the test lowers this thread's budget to CASCADE_BUDGET first.

   The thread alternates two large bins (160 KiB and 300 KiB), each cycle freeing one block so its
   page retires, ageing it one tick with mi_collect(false) and then requesting the other bin's
   size. Fewer than 1000 slow-path mallocs run, so no heartbeat refills the budget.

   Without MI_DIAGNOSTICS the counters are stubs; the test checks they stay zero and present. */

#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <mimalloc.h>
#include "mimalloc/internal.h"   // _mi_event_get, MI_LARGE_REPURPOSE_FRESH

#define SIZE_A       (160 * 1024)
#define SIZE_B       (300 * 1024)
#define CASCADE_BUDGET (8)             // the thread's repurpose budget for the test (#575: the default outruns one heartbeat)
#define CYCLES       (200)             // 2 requests each: well under the 1000 that make a heartbeat

#if MI_DIAGNOSTICS

#if MI_LARGE_REPURPOSE && MI_LARGE_SPAN
// Selected-page accounting must not call the API that also computes a fresh span.
static void test_reuse_accounting_skips_span_sizing(void) {
  mi_theap_t theap;
  memset(&theap, 0, sizeof(theap));
  const size_t index = _mi_bin(mi_good_size(SIZE_A)) - _mi_bin(MI_MEDIUM_MAX_OBJ_SIZE + 1);
  assert(index < MI_LARGE_SPAN_BINS);
  const uint64_t before = _mi_event_get(MI_EVENT_LARGE_SPAN_ACCOUNT_CALL);
  _mi_large_span_account_repurpose(&theap, index);
  assert(_mi_event_get(MI_EVENT_LARGE_SPAN_ACCOUNT_CALL) == before);
  puts("span: selected-page accounting avoids full sizing API");
}
// Compare the optimization with the existing transition for every encoded byte,
// including pending-full and signed-pressure states, while changing runtime options.
static void test_span_accounting_matches_original(void) {
  mi_theap_t original, candidate;
  memset(&original, 0, sizeof(original));
  memset(&candidate, 0, sizeof(candidate));
  const long enabled = mi_option_get(mi_option_large_span);
  const long maximum = mi_option_get(mi_option_large_span_max);
  const long caps[] = { 0, 1, MI_LARGE_SPAN_COMPACT_SLICES * MI_ARENA_SLICE_SIZE / MI_KiB,
                       MI_LARGE_PAGE_SIZE / MI_KiB };
  const size_t first_bin = _mi_bin(MI_MEDIUM_MAX_OBJ_SIZE + 1);
  const size_t last_bin = _mi_bin(MI_LARGE_MAX_OBJ_SIZE);
  for (long on = 0; on <= 1; on++) {
    mi_option_set(mi_option_large_span, on);
    for (size_t cap = 0; cap < sizeof(caps) / sizeof(caps[0]); cap++) {
      mi_option_set(mi_option_large_span_max, caps[cap]);
      for (size_t bin = first_bin; bin <= last_bin; bin++) {
        const size_t index = bin - first_bin;
        assert(index < MI_LARGE_SPAN_BINS);
        const size_t block_size = _mi_bin_size(bin);
        for (unsigned state = 0; state <= UINT8_MAX; state++) {
          original.large_span[index] = (mi_large_span_bin_t)state;
          candidate.large_span[index] = (mi_large_span_bin_t)state;
          const uint64_t grow = _mi_event_get(MI_EVENT_LARGE_SPAN_GROW);
          const uint64_t shrink = _mi_event_get(MI_EVENT_LARGE_SPAN_SHRINK);
          (void)_mi_large_span_slices(&original, block_size, 0);
          const uint64_t grew = _mi_event_get(MI_EVENT_LARGE_SPAN_GROW) - grow;
          const uint64_t shrank = _mi_event_get(MI_EVENT_LARGE_SPAN_SHRINK) - shrink;
          const uint64_t calls = _mi_event_get(MI_EVENT_LARGE_SPAN_ACCOUNT_CALL);
          _mi_large_span_account_repurpose(&candidate, index);
          assert(memcmp(original.large_span, candidate.large_span, sizeof(original.large_span)) == 0);
          assert(_mi_event_get(MI_EVENT_LARGE_SPAN_GROW) - grow == grew + grew);
          assert(_mi_event_get(MI_EVENT_LARGE_SPAN_SHRINK) - shrink == shrank + shrank);
          assert(_mi_event_get(MI_EVENT_LARGE_SPAN_ACCOUNT_CALL) - calls == 0);
        }
      }
    }
  }
  mi_option_set(mi_option_large_span, enabled);
  mi_option_set(mi_option_large_span_max, maximum);
  puts("span: reuse accounting preserves all encoded transitions and diagnostic events");
}

#endif

static int bin_is_large(size_t size) {
  const size_t b = mi_good_size(size);
  return (b > MI_MEDIUM_MAX_OBJ_SIZE && b <= MI_LARGE_MAX_OBJ_SIZE);
}

static void test_names_and_reset(void) {
  for (size_t e = 0; e < (size_t)MI_EVENT_COUNT; e++) {
    assert(_mi_event_name((mi_event_t)e)[0] != '?' && _mi_event_name((mi_event_t)e)[0] != 0);
  }
  _mi_event_reset();
  for (size_t e = 0; e < (size_t)MI_EVENT_COUNT; e++) { assert(_mi_event_get((mi_event_t)e) == 0); }
  void* p = mi_malloc(SIZE_A);
  assert(p != NULL);
  assert(_mi_event_get(MI_EVENT_ARENA_PAGE_ALLOC) >= 1);
  assert(_mi_event_get(MI_EVENT_PAGE_MAP_REGISTER) >= 1);
  assert(_mi_event_get(MI_EVENT_LARGE_PAGE_REQUEST) >= 1);
  mi_free(p);
  puts("counters: named, zeroed by reset, counted at the arena and page-map sites");
}

static void test_retire_cascade_budget(void) {
  _mi_event_reset();
  #if MI_LARGE_REPURPOSE
  {
    void* const probe = mi_malloc(SIZE_A);   // this thread's tld, through its page's theap
    assert(probe != NULL);
    _mi_ptr_page(probe)->theap->tld->large_repurpose_left = CASCADE_BUDGET;
    mi_free(probe);
  }
  #endif
  for (int i = 0; i < CYCLES; i++) {
    void* a = mi_malloc(SIZE_A);
    assert(a != NULL);
    mi_free(a);             // the bin's only page empties: retired
    mi_collect(false);      // one aging tick: idle, so any bin may take it
    void* b = mi_malloc(SIZE_B);
    assert(b != NULL);
    mi_free(b);
    mi_collect(false);
  }
  const uint64_t requests = _mi_event_get(MI_EVENT_LARGE_PAGE_REQUEST);
  const uint64_t repurposed = _mi_event_get(MI_EVENT_LARGE_REPURPOSE);
  const uint64_t denied = _mi_event_get(MI_EVENT_LARGE_REPURPOSE_DENIED);
  const uint64_t arena_allocs = _mi_event_get(MI_EVENT_ARENA_PAGE_ALLOC);
  fprintf(stderr, "cascade: %llu large page requests -> %llu repurposed, %llu denied, %llu arena pages\n",
          (unsigned long long)requests, (unsigned long long)repurposed, (unsigned long long)denied,
          (unsigned long long)arena_allocs);
  assert(requests >= 1 && requests <= (uint64_t)(2 * CYCLES));
  assert(arena_allocs <= requests);
  #if MI_LARGE_REPURPOSE
  assert(repurposed >= 1);                                     // the mechanism ran
  assert(repurposed <= (uint64_t)CASCADE_BUDGET);              // ... and the heartbeat budget bounded it
  assert(denied >= 1);                                         // the requests beyond it were counted, not stolen
  assert(repurposed + denied <= requests);
  #endif
  puts("cascade: repurposes stay within the heartbeat budget");
}

// #572: "one retired, empty page per large bin, resident" was invisible until a scratch probe walked
// it. The holes report now lists, per bin, the empty and retired pages and the RAM they hold.
static void test_report_lists_retired_pages(void) {
  char* a = (char*)mi_malloc(SIZE_A);
  assert(a != NULL);
  memset(a, 0x33, SIZE_A);   // resident
  mi_free(a);                // the bin's only page empties: retired, not freed
  mi_holes_report_t rep;
  _mi_purge_holes_report_collect(&rep);
  const mi_holes_bin_t* const bin = &rep.bin[_mi_bin(mi_good_size(SIZE_A))];   // (a page's block size is its bin's)
  fprintf(stderr, "report: bin of %d B: %zu pages, %zu empty, %zu retired, %zu bytes resident\n", SIZE_A, bin->pages, bin->empty_pages,
          bin->retired_pages, bin->retired_resident_bytes);
  assert(bin->pages >= 1 && bin->empty_pages >= 1 && bin->retired_pages >= 1);
  assert(bin->retired_pages <= bin->empty_pages && bin->empty_pages <= bin->pages);
  #if !defined(_WIN32)
  assert(bin->retired_resident_bytes >= (size_t)SIZE_A);   // the touched block is resident
  #endif
  mi_purge_holes_report();   // and prints it
  puts("report: empty and retired pages per bin, with their resident bytes");
}

int main(void) {
  mi_option_set(mi_option_purge_delay, 600000);   // nothing may be released during the run
  if (!bin_is_large(SIZE_A) || !bin_is_large(SIZE_B)) { puts("skipped: sizes are not large bins in this build"); return 0; }
  test_names_and_reset();
  #if MI_LARGE_REPURPOSE && MI_LARGE_SPAN
  test_reuse_accounting_skips_span_sizing();
  test_span_accounting_matches_original();
  #endif
  test_report_lists_retired_pages();
  test_retire_cascade_budget();
  puts("ok");
  return 0;
}

#else  // MI_DIAGNOSTICS=0: the stubs

int main(void) {
  void* p = mi_malloc(SIZE_A);
  assert(p != NULL);
  for (size_t e = 0; e < (size_t)MI_EVENT_COUNT; e++) { assert(_mi_event_get((mi_event_t)e) == 0); }
  _mi_event_reset();
  _mi_event_print();   // prints nothing
  mi_free(p);
  puts("stub: MI_DIAGNOSTICS=0 counts nothing");
  return 0;
}

#endif
