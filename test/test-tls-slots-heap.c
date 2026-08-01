/* Regression test: the dynamic thread-local slot array must not live in a
   user-destroyable heap (issue #128 B3, upstream `a5650085`).

   `mi_thread_locals_expand` (src/threadlocal.c) grows the per-thread array that maps
   a heap's TLS key to that thread's theap. It used plain `mi_rezalloc`, which allocates
   from *the calling thread's current default theap*. That is whatever the application
   last passed to `mi_theap_set_default` -- not necessarily the main heap.

   So an application that sets a custom heap as its default and later calls
   `mi_heap_destroy` on it frees the slot array out from under the allocator, while the
   thread-local pointer still refers to it. Confirmed against our pin: after the destroy,
   the array's `count` field read back as 0xCDCDCDCDCDCDCDCD -- the pattern this test's
   own reuse loop writes.

   The failure is silent, which is why this test does not simply look for a crash. The
   corrupted `count` is enormous, so every bounds check passes; the version words no
   longer match, so every lookup returns NULL, and each heap quietly builds a *second*
   theap. The first one -- and every block already allocated through it -- is orphaned.
   Nothing reports an error and the process keeps running on a split heap.

   Hence the assertion below is on theap identity, not on liveness: a heap's theap for a
   given thread must be stable across the destruction of an unrelated heap.

   Upstream's fix is to allocate the array from the main heap, which no user API can
   destroy. */

#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <mimalloc.h>
#include <stdio.h>
#include <string.h>

/* Enough heaps to force the slot array past its initial 16 entries and through at
   least two reallocations, so the array that survives to the end is one that was
   allocated while the custom heap was the default. */
#define NHEAPS 40

static mi_heap_t*  heaps[NHEAPS];
static mi_theap_t* theaps_before[NHEAPS];

int main(void) {
  mi_heap_t* owner = mi_heap_new();
  assert(owner != NULL);

  /* Everything allocated from here until the reset below comes out of `owner` --
     including, before the fix, the slot array itself. */
  mi_theap_t* const prev = mi_theap_set_default(mi_heap_theap(owner));

  for (int i = 0; i < NHEAPS; i++) {
    heaps[i] = mi_heap_new();
    assert(heaps[i] != NULL);
    void* p = mi_heap_malloc(heaps[i], 32);   /* binds a theap into a slot */
    assert(p != NULL);
    theaps_before[i] = mi_heap_theap(heaps[i]);
    assert(theaps_before[i] != NULL);
  }

  mi_theap_set_default(prev);
  mi_heap_destroy(owner);   /* frees everything `owner` owns, slot array included */

  /* Hand the released memory back out and stamp it. Without the fix this is what
     lands on top of the slot array; with the fix it is inert. Sizes are varied
     because the array's size class depends on how far it grew. */
  for (int r = 0; r < 200; r++) {
    for (size_t sz = 16; sz <= 4096; sz *= 2) {
      void* q = mi_malloc(sz);
      assert(q != NULL);
      memset(q, 0xCD, sz);
    }
  }

  /* The destroyed heap was unrelated to these, so their theaps must be untouched. */
  int changed = 0;
  for (int i = 0; i < NHEAPS; i++) {
    mi_theap_t* const now = mi_heap_theap(heaps[i]);
    if (now != theaps_before[i]) {
      if (changed < 5) {
        fprintf(stderr,
                "heap %d: theap changed across an unrelated mi_heap_destroy "
                "(%p -> %p) -- the thread-local slot array was freed with the "
                "destroyed heap\n",
                i, (void*)theaps_before[i], (void*)now);
      }
      changed++;
    }
  }
  if (changed != 0) {
    fprintf(stderr, "FAIL: %d of %d heaps lost their theap binding\n", changed, NHEAPS);
    return 1;
  }

  /* And the heaps still work. */
  for (int i = 0; i < NHEAPS; i++) {
    void* p = mi_heap_malloc(heaps[i], 32);
    assert(p != NULL);
    memset(p, 1, 32);
  }

  printf("ok: %d heaps kept their theap across an unrelated mi_heap_destroy\n", NHEAPS);
  return 0;
}
