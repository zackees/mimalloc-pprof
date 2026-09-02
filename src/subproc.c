/* ----------------------------------------------------------------------------
Copyright (c) 2018-2026, Microsoft Research, Daan Leijen
This is free software; you can redistribute it and/or modify it under the
terms of the MIT license. A copy of the license can be found in the file
"LICENSE" at the root of this distribution.
-----------------------------------------------------------------------------*/

#include "mimalloc.h"
#include "mimalloc/internal.h"
#include "mimalloc/prim-tls.h"

/* -----------------------------------------------------------
  #270 (Bun parity P5): the `pthread_atfork` fork-safety handlers, the lock order they
  implement, and their MI_DEBUG-only self-checks and test hooks all live in `src/fork.c`
  (rule 6: new logic in new files). This file keeps only the two accessors below, which
  give fork.c access to the sub-process registry it has to walk.
----------------------------------------------------------- */
// pre-allocate the main subprocess structure.
static mi_decl_cache_align mi_subproc_t mi_process_subproc_main = mi_init_struct_zero;
static mi_subproc_t* mi_subprocs = NULL;
static mi_lock_t     mi_subprocs_lock = MI_LOCK_INITIALIZER;

// #270: the fork handlers (src/fork.c) walk the sub-process registry and quiesce its
// lock; both are file-static here. Callers must hold `mi_subprocs_lock` while walking.
mi_subproc_t* _mi_subprocs_head(void) { return mi_subprocs; }
mi_lock_t*    _mi_subprocs_lock(void) { return &mi_subprocs_lock; }

// imported from oven-sh/mimalloc @ 942b8342, MIT (issue #271 / Bun parity P6): defined here
// (not in src/fork.c, which is compiled only under `#if !defined(_WIN32) && !defined(__wasi__)`)
// because `mi_heap_visit_page_claim` (arena.c) and `mi_heap_detach_theaps` (heap.c) read it
// unconditionally on every platform -- a Windows/wasi build that never sets it still needs
// the symbol to link. Only the setter (`_mi_process_fork_child`) lives in fork.c's
// POSIX-only block; see its definition there for the sticky-flag limitation.
mi_decl_hidden bool _mi_process_is_forked_child = false;


/* -----------------------------------------------------------
  Meta-data allocation
  We allocate thread local data and theaps through a dedicated
  theap `subproc.theap_meta` which uses a detached tld with
  a detached thread id. The initial theap_meta is statically 
  allocated and can thus be used to allocate on an as yet
  uninitialized thread or process.
  We need to take a lock though to allocate safely on the
  detached `theap_meta`.
----------------------------------------------------------- */

void* _mi_meta_zalloc( mi_subproc_t* subproc, size_t size, mi_memid_t* memid ) {
  mi_assert_internal(subproc->theap_meta != NULL);
  void* p = NULL;
  mi_lock(&subproc->theap_meta_lock) {
    p = mi_theap_zalloc(subproc->theap_meta, size);
    if (memid != NULL) { *memid = (p==NULL ? _mi_memid_none() : _mi_memid_create_malloc(p,size,true) ); }
  }
  return p;
}

void* _mi_meta_zalloc_aligned( mi_subproc_t* subproc, size_t size, size_t aligned, mi_memid_t* memid ) {
  mi_assert_internal(subproc->theap_meta != NULL);
  void* p = NULL;
  mi_lock(&subproc->theap_meta_lock) {
    p = mi_theap_zalloc_aligned(subproc->theap_meta, size, aligned);
    if (memid != NULL) { *memid = (p==NULL ? _mi_memid_none() : _mi_memid_create_malloc(p,size,true) ); }
  }
  return p;
}

void* _mi_meta_rezalloc( mi_subproc_t* subproc, void* oldp, size_t newsize, mi_memid_t* memid ) {
  mi_assert_internal(subproc->theap_meta != NULL);
  // note: since we take a meta lock we cannot use `mi_theap_rezalloc` as that could call `mi_free` which
  // can call `mi_stat_free` which would try to take the meta lock again. See issue #1358.
  void* p = NULL;  
  mi_lock(&subproc->theap_meta_lock) {
    p = mi_theap_zalloc(subproc->theap_meta, newsize);
  }
  if (p!=NULL) {
    if (oldp!=NULL) {
      const size_t oldsize  = mi_usable_size(oldp);
      const size_t copysize = (newsize < oldsize ? newsize : oldsize);
      _mi_memcpy(p,oldp,copysize);
      if (memid!=NULL) { _mi_meta_free(subproc,oldp,*memid); } 
                  else { mi_free(oldp); }
    }
    if (memid!=NULL) { *memid = _mi_memid_create_malloc(p,newsize,true); }
  }
  else {
    if (memid!=NULL) { *memid = _mi_memid_none(); }  
  }
  return p;
}

void _mi_meta_free(mi_subproc_t* subproc, void* p, mi_memid_t memid) {
  if (p==NULL || mi_memid_needs_no_free(memid)) return;
  if (memid.memkind == MI_MEM_MALLOC) {
    mi_free(p);
  }
  else {
    mi_assert_internal(subproc!=NULL);  
    _mi_arenas_free(subproc, p, _mi_memid_size(memid), memid);
  }
}

bool _mi_meta_is_meta_page(const mi_subproc_t* subproc, const mi_page_t* page) {
  if (page==NULL) return false;
  mi_theap_t* theap = page->theap;
  return (theap != NULL && theap == subproc->theap_meta);
}


/* -----------------------------------------------------------
  Sub process helpers
----------------------------------------------------------- */

mi_subproc_t* _mi_subproc_main(void) {
  return &mi_process_subproc_main;
}

bool _mi_subproc_is_main(mi_subproc_t* subproc) {
  return (subproc == &mi_process_subproc_main);
}

mi_subproc_t* _mi_subproc(void) {
  mi_theap_t* theap = _mi_theap_default();
  if (theap == NULL || theap->tld == NULL) {  // see issue #1289
    return _mi_subproc_main();
  }
  else {
    return theap->tld->subproc;
  }
}

mi_heap_t* mi_heap_main(void) {
  return _mi_subproc_heap_main(_mi_subproc()); // don't use mi_theap_main_init_get() so this call works during process_init
}


mi_subproc_t* _mi_subproc_from_id(mi_subproc_id_t subproc_id) {
  return (mi_subproc_t*)(subproc_id._mi_subproc_id);
}

mi_subproc_id_t _mi_subproc_to_id(mi_subproc_t* subproc) {
  mi_subproc_id_t id = { subproc };
  return id;
}

mi_subproc_id_t mi_subproc_main(void) {
  return _mi_subproc_to_id(_mi_subproc_main());
}

mi_subproc_id_t mi_subproc_current(void) {
  return _mi_subproc_to_id(_mi_subproc());
}


/* -----------------------------------------------------------
  Sub process creation
----------------------------------------------------------- */


static mi_subproc_t* mi_subproc_init(mi_subproc_t* subproc, mi_subproc_t* parent) {
  static _Atomic(size_t) subproc_total_count;
  subproc->parent = parent;
  subproc->subproc_seq = mi_atomic_increment_relaxed(&subproc_total_count);
  mi_stats_header_init(&subproc->stats);
  mi_lock_init(&subproc->arena_reserve_lock);
  mi_lock_init(&subproc->heaps_lock);
  mi_lock_init(&subproc->theap_meta_lock);
  mi_lock(&mi_subprocs_lock) {
    // push on subproc list
    subproc->next = mi_subprocs;
    if (mi_subprocs!=NULL) { mi_subprocs->prev = subproc; }
    mi_subprocs = subproc;
  }
  return subproc;
}

mi_subproc_id_t mi_subproc_new(void) {
  mi_thread_init();
  mi_subproc_t* const parent = _mi_subproc();
  mi_memid_t memid;
  mi_subproc_t* const subproc = (mi_subproc_t*)_mi_meta_zalloc(parent, sizeof(mi_subproc_t), &memid);
  if (subproc == NULL) { return _mi_subproc_to_id(NULL); }
  subproc->memid  = memid;  
  
  mi_memid_t theap_memid;
  mi_theap_t* const theap_meta = (mi_theap_t*)_mi_meta_zalloc(parent, sizeof(mi_theap_t), &theap_memid);
  if (theap_meta==NULL) { 
    _mi_meta_free(parent, subproc, memid); 
    return _mi_subproc_to_id(NULL); 
  }
  theap_meta->memid = memid;
  
  // init subproc
  mi_subproc_init(subproc,parent);
  
  // init main heap
  mi_heap_t* heap_main = _mi_heap_new_for_subproc(subproc,0,true);
  if (heap_main==NULL) {
    _mi_meta_free(parent, theap_meta, theap_meta->memid);
    mi_subproc_destroy(_mi_subproc_to_id(subproc));
    return _mi_subproc_to_id(NULL);
  }
  mi_assert_internal(subproc->heap_main == heap_main);

  // init meta theap
  mi_assert_internal(parent->theap_meta!=NULL);
  mi_assert_internal(parent->theap_meta->tld!=NULL);
  mi_assert_internal(parent->theap_meta->tld->thread_id == MI_THREADID_DETACHED);
  _mi_theap_init(theap_meta,heap_main,parent->theap_meta->tld /* detached tld */);
  #if MI_GUARDED
  // See the matching comment in init.c's process-main theap_meta bootstrap (#266):
  // internal allocator bookkeeping must never be guarded.
  theap_meta->guarded_sample_rate = 0;
  #endif
  subproc->theap_meta = theap_meta;

  return _mi_subproc_to_id(subproc);
}


/* -----------------------------------------------------------
  Sub process destruction
----------------------------------------------------------- */

// destroy all subproc resources including arena's, heap's etc.
static void mi_subproc_unsafe_destroy(mi_subproc_t* subproc, bool acquire_subprocs_lock)
{
  if (subproc==NULL) return;

  // remove from the subproc list
  mi_lock_maybe(&mi_subprocs_lock, acquire_subprocs_lock) {
    if (subproc->next!=NULL) { subproc->next->prev = subproc->prev;  }
    if (subproc->prev!=NULL) { subproc->prev->next = subproc->next;  }
                        else { mi_assert_internal(mi_subprocs==subproc);  mi_subprocs = subproc->next; }
  }

  // destroy all subproc heaps
  mi_lock(&subproc->heaps_lock) {
    mi_heap_t* heap = subproc->heaps;
    while (heap != NULL) {
      mi_heap_t* next = heap->next;
      if (heap!=subproc->heap_main) { _mi_heap_force_destroy(heap, false /* don't re-acquire the heaps_lock */); }
      heap = next;
    }
    mi_assert_internal(subproc->heap_main==NULL || subproc->heaps == subproc->heap_main);
    if (subproc->heap_main!=NULL) {
      _mi_thread_locals_thread_done(); // release thread locals that may have been allocated (safe as the main heap uses the fast key)
      if (_mi_subproc_is_main(subproc)) {
        _mi_thread_locals_done();      
      }
      _mi_heap_force_destroy(subproc->heap_main, false /* don't re-acquire the heaps_lock */);  // no warning if destroying the main heap
    }
  }

  subproc->theap_meta = NULL; // theap meta stats are merged during heap_destroy of the main heap

  if (!_mi_subproc_is_main(subproc)) {
    // merge stats back into the main subproc  
    _mi_stats_merge_into(&mi_process_subproc_main.stats, &subproc->stats);
  }

  // remove associated arenas
  _mi_arenas_unsafe_destroy_all(subproc);

  // show stats of the main process (at process end) before releasing the heaps lock
  if (_mi_subproc_is_main(subproc)) {
    if (mi_option_is_enabled(mi_option_show_stats) || mi_option_is_enabled(mi_option_verbose)) {
      mi_subproc_stats_print_out(mi_subproc_main(), NULL, NULL);
    }
  }

  // todo: should we refcount subprocesses?
  mi_lock_done(&subproc->arena_reserve_lock);
  mi_lock_done(&subproc->heaps_lock);
  mi_lock_done(&subproc->theap_meta_lock);  
  _mi_meta_free( subproc->parent, subproc, subproc->memid);  
  if (_mi_subproc_is_main(subproc)) {
    // for the main subproc, also release the global page map
    _mi_page_map_unsafe_destroy();
  }
}

void mi_subproc_destroy(mi_subproc_id_t subproc_id) {
  mi_subproc_t* subproc = _mi_subproc_from_id(subproc_id);
  if (subproc==NULL || subproc==&mi_process_subproc_main) return;
  mi_subproc_unsafe_destroy(subproc, true /* take lock */);
}

void _mi_subprocs_unsafe_destroy_all(void) {
  mi_lock(&mi_subprocs_lock) {
    mi_subproc_t* subproc = mi_subprocs;
    while (subproc!=NULL) {
      mi_subproc_t* next = subproc->next;
      if (subproc!=&mi_process_subproc_main) {
        mi_subproc_unsafe_destroy(subproc, false /* take mi_subprocs lock */);
      }
      subproc = next;
    }
  }
  mi_subproc_unsafe_destroy(&mi_process_subproc_main, true /* take mi_subprocs lock */);
}


/* -----------------------------------------------------------
  Sub process various
----------------------------------------------------------- */

void mi_subproc_add_current_thread(mi_subproc_id_t subproc_id) {
  mi_subproc_t* subproc = _mi_subproc_from_id(subproc_id);
  mi_assert_internal(subproc!=NULL);
  if (subproc==NULL) return;
  mi_assert_internal(subproc->heap_main!=NULL);
  if (subproc->heap_main==NULL) return;
  mi_theap_t* theap = _mi_theap_default();
  if (mi_theap_is_initialized(theap)) {
    if (theap->tld!=NULL && theap->tld->subproc != subproc) {
      _mi_warning_message("unable to add thread to the subprocess as it was already in another subprocess (at %p)\n", theap->tld->subproc);
    }
    return;
  }

  // initialize this thread tld & theap
  _mi_thread_init_with_heap(subproc->heap_main);
}


bool mi_subproc_visit_heaps(mi_subproc_id_t subproc_id, mi_heap_visit_fun* visitor, void* arg) {
  mi_subproc_t* subproc = _mi_subproc_from_id(subproc_id);
  if (subproc==NULL) return false;
  bool ok = true;
  mi_lock(&subproc->heaps_lock) {
    for (mi_heap_t* heap = subproc->heaps; heap!=NULL && ok; heap = heap->next) {
      ok = (*visitor)(heap, arg);
    }
  }
  return ok;
}

#if MI_PPROF
// #267: called from mi_prof_start_seeded/mi_prof_stop (profile.c), deliberately OUTSIDE
// prof_lock so this never adds a new lock-ordering constraint against it. Walks every live
// theap of every subprocess (mi_subprocs -> heap->next -> theap->hnext) and syncs
// `prof_force_slow` -- Bun's zero-cost-when-off strategy: poisoning `pages_free_direct`
// (via `_mi_theap_pages_free_direct_poison`, page-queue.c) forces every malloc on that
// theap through `_mi_malloc_generic`, where the sampling countdown lives, for as long as
// profiling is running.
//
// Deliberately takes no `enable` argument and instead reads `mi_prof_is_enabled()` fresh
// for each theap, inside the lock that also guards a concurrently-created theap's own read
// of the same flag (see `_mi_theap_init`'s comment, theap.c). A start-then-stop (or
// stop-then-start) from two racing threads no longer has a "which walk wins" ordering
// hazard: mi_prof_start_seeded and mi_prof_stop both flip the global `prof_enabled` flag
// under `prof_lock` BEFORE either one's walk begins, so whichever walk actually visits a
// given theap last always writes that theap's CURRENT global state, not a value captured
// stale at the time its caller was invoked. (An earlier version took `enable` as a
// snapshot argument; a start-walk finishing after a chronologically-later stop-walk could
// then leave every theap poisoned while profiling was already off, or vice versa.)
//
// Lock order: mi_subprocs_lock -> subproc->heaps_lock -> heap->theaps_lock. Nothing else in
// this file nests `heap->theaps_lock` under `subproc->heaps_lock` today (`_mi_theap_init`,
// theap.c, takes them one after another, never nested), so this does not introduce a cycle.
//
// The poisoning write into `pages_free_direct` (only when the fresh read says enabled) is
// a plain cross-thread pointer write that the owning thread may concurrently read
// lock-free on its fast path -- imported from oven-sh/mimalloc @ 942b8342's
// `prof_force_slow` design (MIT). It is always safe regardless of timing: it only ever
// writes the static, immutable empty page, never a real (potentially soon-to-be-freed)
// one, so a stale or reordered read of it is never a dangling pointer, only a delayed
// poison (one more fast-path allocation slips through unsampled). It is also not the only
// thing keeping `pages_free_direct` correct while poisoned -- see
// `mi_theap_queue_first_update`'s matching comment (page-queue.c) for why the owning
// thread's OWN updates, not just this cross-thread write, are what make a poisoned entry
// safe to leave stale for a while rather than a use-after-free waiting to happen. Clearing
// the flag (fresh read says disabled) never touches `pages_free_direct` itself -- see
// `mi_prof_stop`'s comment (profile.c) for why.
void _mi_subproc_prof_sync_force_slow(void) {
  mi_lock(&mi_subprocs_lock) {
    for (mi_subproc_t* subproc = mi_subprocs; subproc != NULL; subproc = subproc->next) {
      mi_lock(&subproc->heaps_lock) {
        for (mi_heap_t* heap = subproc->heaps; heap != NULL; heap = heap->next) {
          mi_lock(&heap->theaps_lock) {
            for (mi_theap_t* theap = heap->theaps; theap != NULL; theap = theap->hnext) {
              const bool enable = mi_prof_is_enabled();
              theap->prof_force_slow = enable;
              if (enable) { _mi_theap_pages_free_direct_poison(theap); }
            }
          }
        }
      }
    }
  }
}
#endif


mi_subproc_t* _mi_subproc_main_init(void) {
  mi_lock_init(&mi_subprocs_lock);
  mi_memid_t memid = _mi_memid_create_static(&mi_process_subproc_main,sizeof(mi_subproc_t));
  mi_process_subproc_main.memid = memid;
  mi_subproc_init(&mi_process_subproc_main,NULL);
  return &mi_process_subproc_main;
}

void _mi_subproc_main_done(void) {
  mi_lock_done(&mi_subprocs_lock);
}

