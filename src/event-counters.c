/* Event counters (#573 A2): how often each slow-path mechanism ran.

   WHY

   The "retire cascade" of #572 -- a bin stealing another's retired page, which then steals in
   turn -- was 548K fresh page requests where main made 292. No timing showed it; six throwaway
   patches to src/page.c, each adding atomic counters and a destructor print, did. This file makes
   that patch permanent: the counters are always where the next diagnosis needs them, and cost
   nothing in a build that does not ask.

   WHAT

   A fixed array of relaxed atomic counters indexed by `mi_event_t` (internal.h), bumped by
   `MI_EVENT(...)` at the sites of the slow paths (fresh large page requests, repurposes and
   denials, retired publish and unpublish, page-map registration and re-extent, large-span steps,
   arena page allocation and free). They never sit on the allocation or free fast paths, take no
   lock, allocate nothing (CLAUDE.md rule 4) and add nothing to any struct (no Rust surface, no
   layout). Compiled in only with MI_DIAGNOSTICS=1 (#414: every observability subsystem is opt-in);
   the stubs keep the query functions present in every configuration.

   `_mi_event_print` is called by `mi_stats_print` and by `mi_purge_holes_report`, so any
   diagnostic run that already prints one of those shows the counts. */

#include "mimalloc.h"
#include "mimalloc/internal.h"

#if MI_DIAGNOSTICS

static _Atomic(size_t) mi_event_counts[MI_EVENT_COUNT];

static const char* const mi_event_names[MI_EVENT_COUNT] = {
  "large_page_request", "large_repurpose", "large_repurpose_denied",
  "retired_publish", "retired_unpublish", "retired_trim",
  "page_map_register", "page_map_reextend",
  "large_span_grow", "large_span_shrink",
  "arena_page_alloc", "arena_page_free"
};

void _mi_event_count(mi_event_t event) {
  mi_assert_internal((size_t)event < (size_t)MI_EVENT_COUNT);
  mi_atomic_add_relaxed(&mi_event_counts[event], (size_t)1);
}

uint64_t _mi_event_get(mi_event_t event) {
  if ((size_t)event >= (size_t)MI_EVENT_COUNT) return 0;
  return (uint64_t)mi_atomic_load_relaxed(&mi_event_counts[event]);
}

const char* _mi_event_name(mi_event_t event) {
  return ((size_t)event < (size_t)MI_EVENT_COUNT ? mi_event_names[event] : "?");
}

void _mi_event_reset(void) {
  for (size_t i = 0; i < (size_t)MI_EVENT_COUNT; i++) { mi_atomic_store_relaxed(&mi_event_counts[i], (size_t)0); }
}

void _mi_event_print(void) {
  bool any = false;
  for (size_t i = 0; i < (size_t)MI_EVENT_COUNT; i++) {
    const size_t n = mi_atomic_load_relaxed(&mi_event_counts[i]);
    if (n == 0) continue;
    _mi_fprintf(NULL, NULL, "%s %s=%zu", (any ? "" : "events (#573):"), mi_event_names[i], n);
    any = true;
  }
  if (any) { _mi_fprintf(NULL, NULL, "\n"); }
}

#else  // !MI_DIAGNOSTICS: the query functions stay, `MI_EVENT` is nothing

uint64_t _mi_event_get(mi_event_t event) { MI_UNUSED(event); return 0; }
const char* _mi_event_name(mi_event_t event) { MI_UNUSED(event); return "?"; }
void _mi_event_reset(void) { }
void _mi_event_print(void) { }

#endif
