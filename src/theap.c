/*----------------------------------------------------------------------------
Copyright (c) 2018-2026, Microsoft Research, Daan Leijen
This is free software; you can redistribute it and/or modify it under the
terms of the MIT license. A copy of the license can be found in the file
"LICENSE" at the root of this distribution.
-----------------------------------------------------------------------------*/

#include "mimalloc.h"
#include "mimalloc/internal.h"
#include "mimalloc/prim.h"      // _mi_prim_thread_yield
#include "mimalloc/prim-tls.h"  // _mi_theap_default

#if defined(_MSC_VER) && (_MSC_VER < 1920)
#pragma warning(disable:4204)  // non-constant aggregate initializer
#endif

/* -----------------------------------------------------------
  Helpers
----------------------------------------------------------- */

// return `true` if ok, `false` to break
// #272: `theap_page_visitor_fun` moved to `mimalloc/internal.h` with `_mi_theap_visit_pages`.

// Visit all pages in a theap; returns `false` if break was called.
// #272 (Bun parity P7b): exported so the hole engine in `src/page-holes.c` can drive the same
// walk without a second copy of it (CLAUDE.md rule 6 keeps that engine out of this file).
bool _mi_theap_visit_pages(mi_theap_t* theap, theap_page_visitor_fun* fn, bool include_full, void* arg1, void* arg2)
{
  if (theap==NULL || theap->page_count==0) return true;

  // visit all pages
  #if MI_DEBUG>1
  size_t total = theap->page_count;
  size_t count = 0;
  #endif

  const size_t max_bin = (include_full ? MI_BIN_FULL : MI_BIN_FULL - 1);
  for (size_t i = 0; i <= max_bin; i++) {
    mi_page_queue_t* pq = &theap->pages[i];
    mi_page_t* page = pq->first;
    while(page != NULL) {
      mi_page_t* next = page->next; // save next in case the page gets removed from the queue
      mi_assert_internal(mi_page_theap(page) == theap);
      #if MI_DEBUG>1
      count++;
      #endif
      if (!fn(theap, pq, page, arg1, arg2)) return false;
      page = next; // and continue
    }
  }
  mi_assert_internal(!include_full || count == total);
  return true;
}


#if MI_DEBUG>=3
static bool mi_theap_page_is_valid(mi_theap_t* theap, mi_page_queue_t* pq, mi_page_t* page, void* arg1, void* arg2) {
  MI_UNUSED(arg1);
  MI_UNUSED(arg2);
  MI_UNUSED(pq);
  mi_assert_internal(mi_page_theap(page) == theap);
  mi_theap_t* const page_theap = _mi_heap_theap_peek(page->heap);
  // a detached theap (e.g. `subproc->theap_meta`, used under a lock from any thread for
  // meta-data allocation) is not what `_mi_heap_theap_peek` returns for the calling
  // thread's own theap of the same heap -- that mismatch is expected, not a bug. Same
  // exception already used for this in page.c:91,133 (`_mi_page_is_valid`); it was
  // missing here, which is what the multi-threaded mi_heap_new/mi_heap_delete churn
  // repro in issue #271 / PR #289 tripped (mi_theap_collect on `theap_meta` itself).
  // imported from oven-sh/mimalloc @ 942b8342, MIT (issue #272 / Bun parity P7a, `theap.c:61`):
  // ... and only the OWNING thread's lookup has to agree at all -- the scavenger sweeps a
  // parked thread's theaps while having theaps of its own, so `_mi_heap_theap_peek` returns
  // the scavenger's theap for that heap, not the one being walked.
  mi_assert_internal(page_theap == NULL || theap == page_theap || mi_theap_is_detached(theap)
                     || theap->tld == NULL || theap->tld->thread_id != _mi_thread_id());
  mi_assert_expensive(_mi_page_is_valid(page));
  return true;
}

static bool mi_theap_is_valid(mi_theap_t* theap) {
  mi_assert_internal(theap!=NULL);
  mi_heap_t* const heap = _mi_theap_heap_peek(theap);
  mi_assert_internal(heap != NULL);
  mi_theap_t* const heap_theap = _mi_heap_theap_peek(heap);  // don't use mi_heap_theap as that may re-initialize the thread
  // see the comment in mi_theap_page_is_valid above
  // ... plus the scavenger-sweeps-a-parked-thread case, see mi_theap_page_is_valid (#272)
  mi_assert_internal(heap_theap==NULL || heap_theap == theap || mi_theap_is_detached(theap)
                     || theap->tld == NULL || theap->tld->thread_id != _mi_thread_id());
  _mi_theap_visit_pages(theap, &mi_theap_page_is_valid, true, NULL, NULL);
  for (size_t bin = 0; bin < MI_BIN_COUNT; bin++) {
    mi_assert_internal(_mi_page_queue_is_valid(theap, &theap->pages[bin]));
  }
  return true;
}
#endif




/* -----------------------------------------------------------
  "Collect" pages by migrating `local_free` and `thread_free`
  lists and freeing empty pages. This is done when a thread
  stops (and in that case abandons pages if there are still
  blocks alive)
----------------------------------------------------------- */

typedef enum mi_collect_e {
  MI_NORMAL,
  MI_FORCE,
  MI_ABANDON
} mi_collect_t;


static bool mi_theap_page_collect(mi_theap_t* theap, mi_page_queue_t* pq, mi_page_t* page, void* arg_collect, void* arg2 ) {
  MI_UNUSED(arg2);
  MI_UNUSED(theap);
  mi_assert_expensive(mi_theap_page_is_valid(theap, pq, page, NULL, NULL));
  mi_collect_t collect = *((mi_collect_t*)arg_collect);
  // imported from oven-sh/mimalloc @ 942b8342, MIT (issue #272 / Bun parity P7a): when the
  // scavenger is doing this for a parked thread, the owner may wake at any moment and has to
  // wait for us in `_mi_park_leave`. Stopping between pages bounds that wait to one page.
  if (theap->tld != NULL && mi_atomic_load_relaxed(&theap->tld->park_reclaim) != 0) return false;
  // imported from oven-sh/mimalloc @ 942b8342, MIT (issue #272 / Bun parity P7b): never un-purge here. A collect
  // serves no allocation, so a run brought back would only be re-discarded by the next sweep --
  // and on macOS bringing it back costs an `_mi_os_reuse` syscall.
  _mi_page_free_collect_no_unpurge(page, collect >= MI_FORCE);
  if (mi_page_all_free(page)) {
    // no more used blocks, possibly free the page.
    if (collect >= MI_FORCE || page->retire_expire == 0) {  // either forced/abandon, or not already retired
      // note: this will potentially free retired pages as well.
      _mi_page_free(page, pq);
    }
  }
  else if (collect == MI_ABANDON) {
    // still used blocks but the thread is done; abandon the page
    _mi_page_abandon(page, pq);
  }
  return true; // don't break
}

void _mi_theap_merge_stats(mi_theap_t* theap) {
  mi_assert_internal(mi_theap_is_initialized(theap));
  mi_heap_t* const heap = _mi_theap_heap(theap);
  _mi_stats_merge_into(&heap->stats, &theap->stats);
}

// #366: the UN-GATED collect body (docs/purge-all-implementation.md §6). Two doors lead here:
// the owner door -- the public `mi_theap_collect`/`mi_collect`/`mi_heap_collect` below, which
// take the owner gate first -- and the foreign door, `_mi_theap_collect_foreign`, used by the
// thread holding this tld's MI_PARK_SWEEPING claim (`_mi_thread_idle_work_ex`, scavenger.c).
// Nothing in here touches `gate_depth`; `_mi_deferred_free` stays owner-only through its own
// thread_id guard (page.c).
static void mi_theap_collect_ex(mi_theap_t* theap, mi_collect_t collect)
{
  if (theap==NULL || !mi_theap_is_initialized(theap)) return;
  mi_assert_expensive(mi_theap_is_valid(theap));

  const bool force = (collect >= MI_FORCE);
  _mi_deferred_free(theap, force);

  // python/cpython#112532: we may be called from a thread that is not the owner of the theap
  // const bool is_main_thread = (_mi_is_main_thread() && theap->thread_id == _mi_thread_id());

  // collect retired pages (and full pages if theap->allow_page_abandon is false)
  _mi_theap_collect_retired(theap, force); 

  // collect all pages owned by this thread
  _mi_theap_visit_pages(theap, &mi_theap_page_collect, (collect!=MI_NORMAL), &collect, NULL);  // dont normally visit full pages, see issue #1220

  // collect arenas (this is program wide so don't force purges on abandonment of threads).
  // imported from oven-sh/mimalloc @ 942b8342, MIT (issue #272 / Bun parity P7a): not from a
  // claimed parked sweep though -- a woken owner spins in `_mi_park_leave` for the whole of it
  // and nothing in the arena purge reads `park_reclaim`, so the "bounded by one page" wait would
  // become an unbounded subproc-wide madvise pass. `_mi_thread_idle_work` runs the arena purge
  // itself, as its own reclaim-gated phase (`_mi_arenas_purge_now`).
  //mi_atomic_storei64_release(&theap->tld->subproc->purge_expire, 1);
  if (theap->tld == NULL || mi_atomic_load_relaxed(&theap->tld->park_state) != MI_PARK_SWEEPING) {
    _mi_arenas_collect(collect == MI_FORCE /* force purge? */, collect >= MI_FORCE /* visit all? */, theap->tld);
  }

  // merge statistics
  _mi_theap_merge_stats(theap);
}

void _mi_theap_collect_abandon(mi_theap_t* theap) {
  mi_theap_collect_ex(theap, MI_ABANDON);
}

// imported from oven-sh/mimalloc @ 942b8342, MIT (issue #271 / Bun parity P6, commit
// 8286bfb6): abandon every page of a theap that mi_heap_delete/mi_heap_destroy detached
// from its heap (heap.c:mi_heap_detach_theaps -> _mi_heap_detach_theaps), as if its thread
// had terminated. That thread no longer reaches the theap (_mi_heap_theap_peek /
// _mi_page_associated_theap_peek return NULL for a detached theap, see prim-tls.h), and by
// the contract of mi_heap_delete it is not allocating from or freeing into these pages
// itself -- so after this call every page of the heap is an abandoned page, and the only
// other party that can touch one is a concurrent mi_free collecting it. Called from the
// deleting thread, on behalf of the (possibly different) thread that owned the theap --
// _mi_arenas_page_abandon's assertions use _mi_theap_can_touch, not
// mi_theap_matches_thread, to allow that.
static bool mi_theap_page_abandon(mi_theap_t* theap, mi_page_queue_t* pq, mi_page_t* page, void* arg1, void* arg2) {
  MI_UNUSED(theap); MI_UNUSED(arg1); MI_UNUSED(arg2);
  _mi_page_abandon(page, pq);  // frees it instead if all blocks turn out to be free
  return true;
}

void _mi_theap_abandon(mi_theap_t* theap) {
  mi_assert_internal(_mi_theap_heap_peek(theap)==NULL);  // must already be detached
  mi_assert_internal(theap->tnext==NULL && theap->tprev==NULL);
  _mi_theap_visit_pages(theap, &mi_theap_page_abandon, true /* include full pages */, NULL, NULL);
  mi_assert_internal(theap->page_count==0);
  #if MI_DEBUG>1
  for (size_t i = 0; i <= MI_BIN_FULL; i++) { mi_assert_internal(theap->pages[i].first == NULL); }
  #endif
}

void _mi_theap_collect_foreign(mi_theap_t* theap, bool force) {
  mi_theap_collect_ex(theap, (force ? MI_FORCE : MI_NORMAL));
}

// #366: owner-gate site (docs/purge-all-implementation.md §5.1). The gate is the CALLER's own
// tld, not `theap->tld`: `theap` may belong to another thread (python/cpython#112532, see the
// body), and `gate_depth` is owner-private -- a foreign theap's count must never be touched.
// For the usual case (`mi_collect`, `mi_heap_collect`, an own theap) both are the same tld.
// One enter, exactly one leave.
void mi_theap_collect(mi_theap_t* theap, bool force) mi_attr_noexcept {
  mi_theap_t* self = _mi_theap_default();
  #if MI_OWNER_GATE
  // #366: a collect must never INITIALISE a thread (that allocates the tld under
  // `theap_meta_lock`, and `mi_malloc_generic_admin` collects `theap_meta` from inside
  // `_mi_meta_zalloc` on a thread that has no theap yet -- a self-deadlock), and the meta
  // theap is guarded by `theap_meta_lock`, not the gate. Neither case has owner-private
  // state of the CALLER to protect, so both go straight to the body.
  if (!mi_theap_is_initialized(self) || (theap != NULL && theap->tld != NULL && theap->tld->thread_id == MI_THREADID_DETACHED)) {
    mi_theap_collect_ex(theap, (force ? MI_FORCE : MI_NORMAL));
    return;
  }
  #endif
  MI_GATE_ENTER(self);
  mi_theap_collect_ex(theap, (force ? MI_FORCE : MI_NORMAL));
  MI_GATE_LEAVE(self->tld);
  #if !MI_OWNER_GATE
  MI_UNUSED(self);
  #endif
}

void mi_collect(bool force) mi_attr_noexcept {
  // cannot really collect process wide, just a theap..
  mi_theap_collect(_mi_theap_default(), force);   // #366: gated in `mi_theap_collect`
}

void mi_heap_collect(mi_heap_t* heap, bool force) {
  // cannot really collect a heap, just a theap..
  mi_theap_collect(mi_heap_theap(heap), force);   // #366: gated in `mi_theap_collect`
}

/* -----------------------------------------------------------
  Heap new
----------------------------------------------------------- */

mi_theap_t* mi_theap_get_default(void) {
  mi_theap_t* theap = _mi_theap_default();
  if mi_unlikely(!mi_theap_is_initialized(theap)) {
    mi_thread_init();
    theap = _mi_theap_default();
    mi_assert_internal(mi_theap_is_initialized(theap));
  }
  return theap;
}

static mi_decl_forceinline mi_theap_t* mi_theap_set_default_inner(mi_theap_t* theap) {
  mi_theap_t* const previous = mi_theap_get_default();
  if (mi_theap_is_initialized(theap)) {
    _mi_theap_default_set(theap);
  }
  return previous;
}

// #366: owner-gate site (docs/purge-all-implementation.md §5.1): mutates theap ownership. Gated
// on the caller's CURRENT default theap (captured before the switch; every theap of a thread
// shares its tld). One enter, exactly one leave.
mi_theap_t* mi_theap_set_default(mi_theap_t* theap) {
  mi_theap_t* self = _mi_theap_default();
  MI_GATE_ENTER(self);
  mi_theap_t* const previous = mi_theap_set_default_inner(theap);
  MI_GATE_LEAVE(self->tld);
  #if !MI_OWNER_GATE
  MI_UNUSED(self);
  #endif
  return previous;
}

#if MI_GUARDED
mi_decl_export void mi_theap_guarded_set_sample_rate(mi_theap_t* theap, size_t sample_rate, size_t seed) {
  theap->guarded_sample_rate  = sample_rate;
  theap->guarded_sample_count = sample_rate;  // count down samples
  if (theap->guarded_sample_rate > 1) {
    if (seed == 0) {
      seed = _mi_theap_random_next(theap);
    }
    theap->guarded_sample_count = (seed % theap->guarded_sample_rate) + 1;  // start at random count between 1 and `sample_rate`
  }
}

mi_decl_export void mi_theap_guarded_set_size_bound(mi_theap_t* theap, size_t min, size_t max) {
  theap->guarded_size_min = min;
  theap->guarded_size_max = (min > max ? min : max);
}

static void mi_theap_guarded_init(mi_theap_t* theap) {
  mi_theap_guarded_set_sample_rate(theap,
    (size_t)mi_option_get_clamp(mi_option_guarded_sample_rate, 0, LONG_MAX),
    (size_t)mi_option_get(mi_option_guarded_sample_seed));
  mi_theap_guarded_set_size_bound(theap,
    (size_t)mi_option_get_clamp(mi_option_guarded_min, 0, LONG_MAX),
    (size_t)mi_option_get_clamp(mi_option_guarded_max, 0, LONG_MAX) );
}
#else
mi_decl_export void mi_theap_guarded_set_sample_rate(mi_theap_t* theap, size_t sample_rate, size_t seed) {
  MI_UNUSED(theap); MI_UNUSED(sample_rate); MI_UNUSED(seed);
}

mi_decl_export void mi_theap_guarded_set_size_bound(mi_theap_t* theap, size_t min, size_t max) {
  MI_UNUSED(theap); MI_UNUSED(min); MI_UNUSED(max);
}
static void mi_theap_guarded_init(mi_theap_t* theap) {
  MI_UNUSED(theap);
}
#endif

static void mi_theap_options_init(mi_theap_t* theap) {
  theap->allow_page_reclaim = (mi_option_get(mi_option_page_reclaim_on_free) >= 0);
  theap->allow_page_abandon = (mi_option_get(mi_option_page_full_retain) >= 0);
  theap->page_full_retain = mi_option_get_clamp(mi_option_page_full_retain, -1, 32);
  theap->is_detached = (theap->tld->thread_id == MI_THREADID_DETACHED);
}

// todo: make order of parameters consistent (but would that break compat with CPython?)
void _mi_theap_init(mi_theap_t* theap, mi_heap_t* heap, mi_tld_t* tld)
{
  mi_assert_internal(theap!=NULL);
  mi_assert_internal(heap!=NULL);
  mi_assert_internal(tld!=NULL);
  mi_memid_t memid = theap->memid;
  _mi_memcpy_aligned(theap, &_mi_theap_empty, sizeof(mi_theap_t));
  theap->memid = memid;
  theap->tld   = tld;  // avoid reading the thread-local tld during initialization
  mi_atomic_store_release(&theap->refcount,1);  
  mi_atomic_store_ptr_release(mi_subproc_t,&theap->subproc,heap->subproc);
  mi_assert_internal(theap->stats.size == sizeof(mi_stats_t));
  mi_theap_options_init(theap);

  if (theap->tld->is_in_threadpool) {
    // if we run as part of a thread pool it is better to not arbitrarily reclaim abandoned pages into our theap.
    // this is checked in `free.c:mi_free_try_collect_mt`
    // .. but abandoning is good in this case: quarter the full page retain (possibly to 0)
    // (so blocked threads do not hold on to too much memory)
    if (theap->page_full_retain > 0) {
      theap->page_full_retain = theap->page_full_retain / 4;
    }
  }

  // push on the thread local theaps list
  mi_theap_t* head = NULL;
  mi_random_ctx_t head_random;
  mi_lock(&theap->tld->theaps_lock) {
    head = theap->tld->theaps;
    theap->tprev = NULL;
    theap->tnext = head;
    theap->tld->theaps = theap;
    if (head!=NULL) { 
      head->tprev = theap; 
      head_random = head->random;
    }    
  }

  // initialize random if heap==NULL
  if (head==NULL) {  // first theap of the first thread?
    #if defined(_WIN32) && !defined(MI_SHARED_LIB)
    if (tld->thread_seq==0) {
      _mi_random_init_weak(&theap->random);    // prevent allocation failure during bcrypt dll initialization with static linking (issue #1185)
    }
    else
    #endif
    {
      _mi_random_init(&theap->random);
    }
  }
  else {
    _mi_random_split(&head_random, &theap->random); // &theap->random is used as nonce so it is ok if threads capture the same head->random
  }
  theap->cookie = _mi_theap_random_next(theap) | 1;
  mi_theap_guarded_init(theap); // needs theap->random
  if (!theap->is_detached) {
    mi_subproc_stat_increase(_mi_theap_subproc(theap),theaps,1);  // on subproc to match theap_free_mem
  }

  // only now set the heap member as it is used to determine if a theap is initialized
  mi_atomic_store_ptr_release(mi_heap_t,&theap->heap,heap);
  
  // push on the heap's theap list
  mi_lock(&heap->theaps_lock) {
    head = heap->theaps;
    theap->hprev = NULL;
    theap->hnext = head;
    if (head!=NULL) { head->hprev = theap; }
    heap->theaps = theap;
    #if MI_PPROF
    // #267: read the profiler's current run state and push under the SAME lock that
    // `_mi_subproc_prof_sync_force_slow` (subproc.c) holds while poisoning every theap in
    // this heap's list -- this serializes "am I in the list yet" against "is profiling
    // enabled yet" so a theap created concurrently with `mi_prof_start`/`mi_prof_stop` is
    // never missed: either this push is ordered before that walk (which then sees and
    // sets this theap itself) or after it (in which case `mi_prof_is_enabled()` already
    // reflects the new state). `pages_free_direct` starts out fully poisoned regardless
    // (copied from the empty-theap template above, before any real page is queued), so no
    // separate poison call is needed here when force_slow comes back true.
    theap->prof_force_slow = mi_prof_is_enabled();
    #endif
  }
}

mi_theap_t* _mi_theap_alloc(mi_heap_t* heap, mi_tld_t* tld) {
  mi_assert_internal(tld!=NULL);
  mi_assert_internal(heap!=NULL);
  mi_assert_internal(tld->thread_id == MI_THREADID_DETACHED || _mi_thread_id() == tld->thread_id);
  // mi_assert_internal(_mi_heap_theap_peek(heap)==NULL);  // don't access thread locals as this is called on thread init

  // allocate and initialize a theap
  mi_memid_t memid;
  mi_theap_t* theap;
  
  if (heap->exclusive_arena == NULL) {
    theap = (mi_theap_t*)_mi_meta_zalloc(heap->subproc, sizeof(mi_theap_t), &memid);
  }
  else {
    // theaps associated with a specific arena are allocated in that arena
    // note: takes up at least one slice which is quite wasteful...
    const size_t size = _mi_align_up(sizeof(mi_theap_t),MI_ARENA_MIN_OBJ_SIZE);
    theap = (mi_theap_t*)_mi_arenas_alloc(heap, size, true, true, heap->exclusive_arena, tld->thread_seq, tld->numa_node, &memid);    
  }
  if (theap==NULL) {
    _mi_error_message(ENOMEM, "unable to allocate theap meta-data\n");
    return NULL;
  }

  theap->memid = memid;
  return theap;
}

mi_theap_t* _mi_theap_create(mi_heap_t* heap, mi_tld_t* tld) {
  mi_theap_t* theap = _mi_theap_alloc(heap,tld);
  if (theap == NULL) return NULL;
  _mi_theap_init(theap, heap, tld);
  return theap;
}

uintptr_t _mi_theap_random_next(mi_theap_t* theap) {
  return _mi_random_next(&theap->random);
}

static void mi_theap_free_mem(mi_theap_t* theap) {
  if (theap!=NULL) {
    mi_subproc_t* const subproc = mi_atomic_load_ptr_relaxed(mi_subproc_t,&theap->subproc);      
    if (!theap->is_detached) {
      mi_subproc_stat_decrease(subproc,theaps,1);  
    }
    _mi_meta_free(subproc, theap, theap->memid);
  }
}

// we need to reference count theaps due to the _mi_theap_cached thread locals
void _mi_theap_incref(mi_theap_t* theap) {
  if (theap!=NULL && !mi_memid_needs_no_free(theap->memid)) {
    mi_atomic_increment_acq_rel(&theap->refcount);
  }
}

void _mi_theap_decref(mi_theap_t* theap) {
  if (theap!=NULL && !mi_memid_needs_no_free(theap->memid)) {
    if (mi_atomic_decrement_acq_rel(&theap->refcount) == 1) {
      mi_theap_free_mem(theap);
    }
  }
}

// Thread termination and heap delete/destroy might run concurrently
// and we need to ensure we free the memory correctly. A heap or tld
// will first "detach" its theaps so it has a list with theaps that are
// no longer shared, and only then free's the theaps in that list.
// To detach we need to hold both the `heap->theaps_lock` and the `tld->theaps_lock`.
// Due to lock-inversion we need to use `mi_lock_try_acquire` and if that fails
// we back-off, release the outer lock, and try again until we succeed.
//
// NOTE (mimalloc-pprof, issue #128 / #78): this non-blocking try-acquire-and-retry
// design is exactly the fix that used to live here as a KNOWN-ISSUE comment on the
// fork's own (now removed) `_mi_theap_free`, which took `heap->theaps_lock` and
// `tld->theaps_lock` in opposite orders via BLOCKING acquisitions -- an AB-BA
// deadlock risk. Upstream restructured thread/heap teardown between our previous
// pin and 6def7be9 into `_mi_heap_detach_theaps` / `_mi_tld_detach_theaps` below,
// which use `mi_lock_try_acquire` with a back-off retry loop instead, so the
// deadlock this fork was tracking is now structurally avoided. `_mi_theap_free`
// itself no longer exists at this pin (no callers, no declaration) -- confirmed
// via `git grep _mi_theap_free` across src/include at 6def7be9.

// Remove the theaps in this heap from any thread local tld lists.
// imported from oven-sh/mimalloc @ 942b8342, MIT (issue #271 / Bun parity P6, commit
// ec238987): a detached theap has `theap->heap == NULL`, so `_mi_heap_theap_peek` /
// `_mi_page_associated_theap_peek` no longer return it and a concurrent `mi_free` of a
// block in the heap no longer reclaims into it or re-abandons through it -- and, just as
// importantly, no thread's `_mi_theap_cached()` fast path can mistake it for a theap of a
// *different*, later heap allocated at the same (reused) address (the ABA reproduced by
// test-heap-aba.c / the standalone repro in this PR). The theap struct itself (and its
// `tld`) stays valid until `heap.c:mi_heap_free_theaps`, which runs after all the pages
// have left the heap, for a free that found the theap just before it was detached here.
// Previously this cleared `theap->tld` instead, which left `theap->heap` stale.
void _mi_heap_detach_theaps( mi_heap_t* heap ) {
  bool all_detached;
  do {
    all_detached = true;
    mi_lock(&heap->theaps_lock) {
      mi_theap_t* theap = heap->theaps;
      while (theap != NULL) {
        mi_theap_t* next = theap->hnext;
        if (_mi_theap_heap_peek(theap) != NULL) {   // not detached yet in an earlier round?
          mi_tld_t* const tld = theap->tld;
          mi_assert_internal(tld != NULL);
          if (mi_lock_try_acquire(&tld->theaps_lock)) {
            // remove the theap from the tld theaps list
            if (theap->tnext != NULL) { theap->tnext->tprev = theap->tprev;  }
            if (theap->tprev != NULL) { theap->tprev->tnext = theap->tnext;  }
                                else { mi_assert_internal(tld->theaps == theap); tld->theaps = theap->tnext; }
            theap->tnext = theap->tprev = NULL;
            mi_atomic_store_ptr_release(mi_heap_t, &theap->heap, NULL);
            mi_lock_release(&tld->theaps_lock);
          }
          else {
            all_detached = false;
          }
        }
        theap = next;
      }
    }
    if (!all_detached) {
      mi_subproc_stat_counter_increase(heap->subproc,heaps_delete_wait,1);
      _mi_prim_thread_yield();
    }
  } while (!all_detached);
}

// Remove the theaps in this thread from the heaps that own them.
void _mi_tld_detach_theaps( mi_tld_t* tld ) {
  bool all_detached;
  do {
    all_detached = true;
    mi_lock(&tld->theaps_lock) {
      mi_theap_t* theap = tld->theaps;
      while (theap != NULL) {
        mi_theap_t* next = theap->tnext;
        mi_assert_internal(theap->page_count==0);
        mi_heap_t* heap = _mi_theap_heap_peek(theap); // now the heap might be NULL from an earlier iteration
        if (heap != NULL) {
          if (mi_lock_try_acquire(&heap->theaps_lock)) {
            // merge stats into the owning heap stats
            _mi_stats_merge_into(&heap->stats, &theap->stats);
            // remove the theap from the heap list
            if (theap->hnext != NULL) { theap->hnext->hprev = theap->hprev; }
            if (theap->hprev != NULL) { theap->hprev->hnext = theap->hnext; }
                                else { mi_assert_internal(heap->theaps == theap); heap->theaps = theap->hnext; }
            theap->hnext = theap->hprev = NULL;
            // and set `heap` to NULL
            mi_atomic_store_ptr_release(mi_heap_t, &theap->heap, NULL);
            mi_lock_release(&heap->theaps_lock);
          }
          else {
            all_detached = false;
          }
        }
        theap = next;
      }
    }
    if (!all_detached) {
      mi_subproc_stat_counter_increase(tld->subproc,heaps_delete_wait,1);
      _mi_prim_thread_yield();
    }
  } while (!all_detached);
}



/* -----------------------------------------------------------
  Safe theap delete
----------------------------------------------------------- */

// Safe delete a theap without freeing any still allocated blocks in that theap.
// void _mi_theap_delete(mi_theap_t* theap, bool acquire_tld_theaps_lock)
// {
//   mi_assert(theap != NULL);
//   mi_assert(mi_theap_is_initialized(theap));
//   mi_assert_expensive(mi_theap_is_valid(theap));
//   if (theap==NULL || !mi_theap_is_initialized(theap)) return;

//   // abandon all pages
//   _mi_theap_collect_abandon(theap);

//   mi_assert_internal(theap->page_count==0);
//   _mi_theap_free(theap, true /* acquire heap->theaps_lock */, acquire_tld_theaps_lock);
// }



/* -----------------------------------------------------------
  Load/unload theaps
----------------------------------------------------------- */
/*
void mi_theap_unload(mi_theap_t* theap) {
  mi_assert(mi_theap_is_initialized(theap));
  mi_assert_expensive(mi_theap_is_valid(theap));
  if (theap==NULL || !mi_theap_is_initialized(theap)) return;
  if (_mi_theap_heap(theap)->exclusive_arena == NULL) {
    _mi_warning_message("cannot unload theaps that are not associated with an exclusive arena\n");
    return;
  }

  // abandon all pages so all thread'id in the pages are cleared
  _mi_theap_collect_abandon(theap);
  mi_assert_internal(theap->page_count==0);

  // remove from theap list
  mi_theap_free(theap, false); // but don't actually free the memory

  // disassociate from the current thread-local and static state
  theap->tld = NULL;
  return;
}

bool mi_theap_reload(mi_theap_t* theap, mi_arena_id_t arena_id) {
  mi_assert(mi_theap_is_initialized(theap));
  if (theap==NULL || !mi_theap_is_initialized(theap)) return false;
  if (_mi_theap_heap(theap)->exclusive_arena == NULL) {
    _mi_warning_message("cannot reload theaps that were not associated with an exclusive arena\n");
    return false;
  }
  if (theap->tld != NULL) {
    _mi_warning_message("cannot reload theaps that were not unloaded first\n");
    return false;
  }
  mi_arena_t* arena = _mi_arena_from_id(arena_id);
  if (_mi_theap_heap(theap)->exclusive_arena != arena) {
    _mi_warning_message("trying to reload a theap at a different arena address: %p vs %p\n", _mi_theap_heap(theap)->exclusive_arena, arena);
    return false;
  }

  mi_assert_internal(theap->page_count==0);

  // re-associate with the current thread-local and static state
  theap->tld = mi_theap_get_default()->tld;

  // reinit direct pages (as we may be in a different process)
  mi_assert_internal(theap->page_count == 0);
  for (size_t i = 0; i < MI_PAGES_DIRECT; i++) {
    theap->pages_free_direct[i] = _mi_page_empty_get();
  }

  // push on the thread local theaps list
  theap->tnext = theap->tld->theaps;
  theap->tld->theaps = theap;
  return true;
}
*/


/* -----------------------------------------------------------
  Visit all theap blocks and areas
  Todo: enable visiting abandoned pages, and
        enable visiting all blocks of all theaps across threads
----------------------------------------------------------- */

void _mi_heap_area_init(mi_heap_area_t* area, mi_page_t* page) {
  const size_t bsize = mi_page_block_size(page);
  const size_t ubsize = mi_page_usable_block_size(page);
  area->reserved = page->reserved * bsize;
  area->committed = page->capacity * bsize;
  area->blocks = mi_page_start(page);
  area->used = page->used;   // number of blocks in use (#553)
  area->block_size = ubsize;
  area->full_block_size = bsize;
  area->reserved1 = page;
}

static void mi_get_fast_divisor(size_t divisor, uint64_t* magic, size_t* shift) {
  mi_assert_internal(divisor > 0 && divisor <= UINT32_MAX);
  *shift = MI_SIZE_BITS - mi_clz(divisor - 1);
  *magic = ((((uint64_t)1 << 32) * (((uint64_t)1 << *shift) - divisor)) / divisor + 1);
}

static size_t mi_fast_divide(size_t n, uint64_t magic, size_t shift) {
  mi_assert_internal(n <= UINT32_MAX);
  const uint64_t hi = ((uint64_t)n * magic) >> 32;
  return (size_t)((hi + n) >> shift);
}

bool _mi_theap_area_visit_blocks(const mi_heap_area_t* area, mi_page_t* page, mi_block_visit_fun* visitor, void* arg) {
  mi_assert(area != NULL);
  if (area==NULL) return true;
  mi_assert(page != NULL);
  if (page == NULL) return true;

  // imported from oven-sh/mimalloc @ 942b8342, MIT (issue #272 / Bun parity P7b):
  // visiting must not un-purge a hole -- inspection may not mutate the heap, and a discarded
  // block is reported as free here either way (see the `purged` marking below).
  _mi_page_free_collect_no_unpurge(page,true);   // collect both thread_delayed and local_free
  mi_assert_internal(page->local_free == NULL);
  if (page->used == 0) return true;

  size_t psize;
  uint8_t* const pstart = mi_page_area(page, &psize);
  mi_heap_t* const heap = mi_page_heap(page);
  const size_t bsize    = mi_page_block_size(page);
  const size_t ubsize   = mi_page_usable_block_size(page); // without padding

  // optimize page with one block
  if (page->capacity == 1) {
    mi_assert_internal(page->used == 1 && page->free == NULL);
    return visitor(heap, area, pstart, ubsize, arg);
  }
  mi_assert(bsize <= UINT32_MAX);

  // optimize full pages
  if (page->used == page->capacity) {
    uint8_t* block = pstart;
    for (size_t i = 0; i < page->capacity; i++) {
      if (!visitor(heap, area, block, ubsize, arg)) return false;
      block += bsize;
    }
    return true;
  }

  // create a bitmap of free blocks.
  #define MI_MAX_BLOCKS   (MI_SMALL_PAGE_SIZE / sizeof(void*))
  uintptr_t free_map[MI_MAX_BLOCKS / MI_INTPTR_BITS];
  const uintptr_t bmapsize = _mi_divide_up(page->capacity, MI_INTPTR_BITS);
  memset(free_map, 0, bmapsize * sizeof(intptr_t));
  if (page->capacity % MI_INTPTR_BITS != 0) {
    // mark left-over bits at the end as free
    size_t shift   = (page->capacity % MI_INTPTR_BITS);
    uintptr_t mask = (UINTPTR_MAX << shift);
    free_map[bmapsize - 1] = mask;
  }

  // fast repeated division by the block size
  uint64_t magic;
  size_t   shift;
  mi_get_fast_divisor(bsize, &magic, &shift);

  #if MI_DEBUG>1
  size_t free_count = 0;
  #endif
  for (mi_block_t* block = page->free; block != NULL; block = mi_block_next(page, block)) {
    #if MI_DEBUG>1
    free_count++;
    #endif
    mi_assert_internal((uint8_t*)block >= pstart && (uint8_t*)block < (pstart + psize));
    size_t offset = (uint8_t*)block - pstart;
    mi_assert_internal(offset % bsize == 0);
    mi_assert_internal(offset <= UINT32_MAX);
    size_t blockidx = mi_fast_divide(offset, magic, shift);
    mi_assert_internal(blockidx == offset / bsize);
    mi_assert_internal(blockidx < MI_MAX_BLOCKS);
    size_t bitidx = (blockidx / MI_INTPTR_BITS);
    size_t bit = blockidx - (bitidx * MI_INTPTR_BITS);
    free_map[bitidx] |= ((uintptr_t)1 << bit);
  }
  // imported from oven-sh/mimalloc @ 942b8342, MIT (issue #272 / Bun parity P7b):
  // purged blocks are free too, but held off the free list (see `src/page-holes.c`): a block is
  // purged exactly when it overlaps a discarded OS page. Marking them free here is what keeps a
  // visitor (mi_heap_visit_blocks, the DHAT/memory-events walkers, mi_prof_snapshot) from ever
  // being handed a pointer into discarded memory (#272 profiler-interaction point 2).
  size_t purged_count = 0;
  if (mi_page_has_purged(page)) {
    for (size_t blockidx = 0; blockidx < page->capacity; blockidx++) {
      if (!mi_page_block_index_is_purged(page, blockidx)) continue;
      purged_count++;
      size_t bitidx = (blockidx / MI_INTPTR_BITS);
      size_t bit = blockidx - (bitidx * MI_INTPTR_BITS);
      free_map[bitidx] |= ((uintptr_t)1 << bit);
    }
  }
  mi_assert_internal(page->capacity == (free_count + purged_count + page->used));
  MI_UNUSED(purged_count);

  // walk through all blocks skipping the free ones
  #if MI_DEBUG>1
  size_t used_count = 0;
  #endif
  uint8_t* block = pstart;
  for (size_t i = 0; i < bmapsize; i++) {
    if (free_map[i] == 0) {
      // every block is in use
      for (size_t j = 0; j < MI_INTPTR_BITS; j++) {
        #if MI_DEBUG>1
        used_count++;
        #endif
        if (!visitor(heap, area, block, ubsize, arg)) return false;
        block += bsize;
      }
    }
    else {
      // visit the used blocks in the mask
      uintptr_t m = ~free_map[i];
      while (m != 0) {
        #if MI_DEBUG>1
        used_count++;
        #endif
        size_t bitidx = mi_ctz(m);
        if (!visitor(heap, area, block + (bitidx * bsize), ubsize, arg)) return false;
        m &= m - 1;  // clear least significant bit
      }
      block += bsize * MI_INTPTR_BITS;
    }
  }
  mi_assert_internal(page->used == used_count);
  return true;
}

// bool _mi_page_visit_blocks( mi_page_t* page, mi_block_visit_fun* visitor, void* arg ) {
//   mi_heap_area_t area;
//   _mi_heap_area_init(&area, page);
//   return _mi_theap_area_visit_blocks(&area, page, visitor, arg);
// }


// Separate struct to keep `mi_page_t` out of the public interface
typedef struct mi_theap_area_ex_s {
  mi_heap_area_t area;
  mi_page_t* page;
} mi_theap_area_ex_t;

typedef bool (mi_theap_area_visit_fun)(const mi_theap_t* theap, const mi_theap_area_ex_t* area, void* arg);

static bool mi_theap_visit_areas_page(mi_theap_t* theap, mi_page_queue_t* pq, mi_page_t* page, void* vfun, void* arg) {
  MI_UNUSED(theap);
  MI_UNUSED(pq);
  mi_theap_area_visit_fun* fun = (mi_theap_area_visit_fun*)vfun;
  mi_theap_area_ex_t xarea;
  xarea.page = page;
  _mi_heap_area_init(&xarea.area, page);
  return fun(theap, &xarea, arg);
}

// Visit all theap pages as areas
static bool mi_theap_visit_areas(const mi_theap_t* theap, mi_theap_area_visit_fun* visitor, void* arg) {
  if (visitor == NULL) return false;
  return _mi_theap_visit_pages((mi_theap_t*)theap, &mi_theap_visit_areas_page, true, (void*)(visitor), arg); // note: function pointer to void* :-{
}

// Just to pass arguments
typedef struct mi_visit_blocks_args_s {
  bool  visit_blocks;
  mi_block_visit_fun* visitor;
  void* arg;
} mi_visit_blocks_args_t;

static bool mi_theap_area_visitor(const mi_theap_t* theap, const mi_theap_area_ex_t* xarea, void* arg) {
  mi_visit_blocks_args_t* args = (mi_visit_blocks_args_t*)arg;
  if (!args->visitor(_mi_theap_heap(theap), &xarea->area, NULL, xarea->area.block_size, args->arg)) return false;
  if (args->visit_blocks) {
    return _mi_theap_area_visit_blocks(&xarea->area, xarea->page, args->visitor, args->arg);
  }
  else {
    return true;
  }
}

// Visit all blocks in a theap
bool mi_theap_visit_blocks(const mi_theap_t* theap, bool visit_blocks, mi_block_visit_fun* visitor, void* arg) {
  mi_visit_blocks_args_t args = { visit_blocks, visitor, arg };
  return mi_theap_visit_areas(theap, &mi_theap_area_visitor, &args);
}

