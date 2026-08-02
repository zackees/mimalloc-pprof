/* Characterization test for the zero-fill boundary of mi_rezalloc (issue #78).

   `mi_rezalloc(p, newsize)` is documented as zero-initializing the extension. In
   practice `_mi_theap_realloc_zero` starts its zero-fill from the block's OLD USABLE
   size rather than from the size the caller originally requested:

     size = _mi_usable_size(p, page);
     ...
     if (zero && newsize > size) {
       const size_t start = (size >= sizeof(intptr_t) ? size - sizeof(intptr_t) : 0);
       _mi_memzero((uint8_t*)newp + start, newsize - start);
     }

   It cannot do better -- the requested size is not tracked in release builds -- so the
   slack between the requested size and the bin size is copied across verbatim. That
   slack was never initialized by anyone, so it carries whatever the previous tenant of
   the page left behind.

   This test pins that behaviour down rather than asserting it is wrong, because it is
   not fixable inside the allocator. Its value is twofold:

     - it documents, with numbers, WHY src/threadlocal.c must zero its new slot range
       explicitly (imported from oven-sh/mimalloc @ d078ad06, MIT). Anyone who deletes
       that memzero as redundant should read this test first.
     - if a future change ever does make mi_rezalloc zero from the requested size, this
       test fails and tells us the workaround can go.

   Deliberately NOT a correctness assertion about mi_rezalloc: it asserts the current,
   deliberate behaviour, and says so.

   The other reason this file exists: #78's C.2 rule is to import only with a test that
   fails without the import. That could not be met for the threadlocal fix itself --
   mi_thread_locals_expand is static, the corrupted state is only observable through a
   heap->theap lookup returning garbage, and Bun's own reproducer is probabilistic
   ("crashes roughly once per several hundred process runs") and pthread-only. So the
   mechanism is pinned here deterministically instead, and the deviation from C.2 is
   recorded rather than papered over. */

#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <mimalloc.h>
#include <stdio.h>
#include <string.h>

int main(void) {
  /* A request whose size class has slack. 40 -> 48 on every configuration we build,
     but the test derives the numbers rather than assuming them. */
  const size_t requested = 40;
  unsigned char* p = (unsigned char*)mi_malloc(requested);
  assert(p != NULL);

  const size_t usable = mi_usable_size(p);
  printf("requested=%zu usable=%zu slack=%zu\n", requested, usable, usable - requested);

  if (usable <= requested) {
    /* No slack in this configuration (e.g. a padding/debug build can report usable as
       the requested size). The mechanism cannot arise, so there is nothing to pin. */
    printf("ok: no slack in this build; mechanism cannot apply\n");
    mi_free(p);
    return 0;
  }

  /* Stand in for a previous tenant's bytes. Deliberately a volatile byte loop and NOT
     memset: mi_malloc carries mi_attr_alloc_size(1), so with _FORTIFY_SOURCE glibc's
     fortified memset sees an object of `requested` bytes and aborts with
     "*** buffer overflow detected ***" -- which is exactly what happened on the first
     CI run. Writing up to mi_usable_size is legal for mimalloc; the compiler just has
     no way to know that. */
  {
    volatile unsigned char* vp = p;
    for (size_t i = 0; i < usable; i++) vp[i] = 0xAA;
  }

  const size_t newsize = usable + 64;
  unsigned char* q = (unsigned char*)mi_rezalloc(p, newsize);
  assert(q != NULL);

  /* Everything at or beyond the OLD USABLE size must be zero -- that part of the
     contract does hold. */
  for (size_t i = usable; i < newsize; i++) {
    assert(q[i] == 0);
  }

  /* And the slack [requested, usable) is where the stale bytes survive. Count rather
     than assert a fixed number, so this stays valid across size-class changes. */
  size_t stale = 0;
  for (size_t i = requested; i < usable; i++) {
    if (q[i] != 0) stale++;
  }
  printf("stale bytes carried into [%zu,%zu): %zu\n", requested, usable, stale);

  /* The ENTIRE slack survives, because `newsize > usable` takes the reallocate-and-move
     path rather than the in-place one: `_mi_theap_malloc_zero` hands back a zeroed block
     and then `min(newsize, old_usable)` bytes are copied over the top of it, slack
     included. (The in-place branch has a different boundary -- it zeroes from
     `old_usable - sizeof(intptr_t)` -- which is what makes it easy to predict the wrong
     number here. This test originally asserted that one and failed, correctly.) */
  const size_t expected_stale = usable - requested;
  if (stale != expected_stale) {
    fprintf(stderr,
            "FAIL: expected %zu stale bytes, saw %zu.\n"
            "If mi_rezalloc now zeroes from the REQUESTED size, this is good news: the\n"
            "explicit memzero in src/threadlocal.c (imported for #78) can be removed.\n",
            expected_stale, stale);
    return 1;
  }

  printf("ok: zero-fill boundary is the old usable size, as expected\n");
  mi_free(q);
  return 0;
}
