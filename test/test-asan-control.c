/* Positive control for the ASan job (issue #86).

   This program deliberately reads freed heap memory. It MUST abort with an
   AddressSanitizer report; CI fails if it exits cleanly.

   Why a dedicated program rather than trusting the job:

   1. `MI_TRACK_ASAN` silently turns itself OFF when `sanitizer/asan_interface.h`
      is missing (CMakeLists.txt: "Cannot find ... install address sanitizer
      support first"), so a misconfigured runner produces a green ASan job that
      sanitizes nothing.
   2. Linking ASan is not the same as mimalloc *using* it. With MI_TRACK_ASAN=1
      mimalloc poisons blocks on free; without it, freed blocks stay in mimalloc's
      own free list and a read-after-free is invisible to ASan because the
      allocator, not the OS, owns that memory. Only an allocator-mediated
      use-after-free proves the integration is live.

   So this is the check that distinguishes "ASan is on" from "ASan is on AND
   mimalloc is telling it about our allocations". A gate never observed to fire
   proves nothing, and this repository has shipped four that were silently
   checking nothing. */

#include <mimalloc.h>
#include <stdio.h>

int main(void) {
  /* Large enough to be a distinct block, small enough to stay in the normal
     small-object path that MI_TRACK_ASAN instruments. */
  volatile unsigned char* p = (volatile unsigned char*)mi_malloc(64);
  if (p == NULL) {
    fprintf(stderr, "control: allocation failed\n");
    return 2;
  }
  p[0] = 42;
  mi_free((void*)p);

  /* The read below is the point of this program. */
  fprintf(stderr, "control: reading freed memory -- ASan must report this\n");
  fflush(stderr);
  const unsigned char observed = p[0];

  /* Reached only if ASan did NOT trap, i.e. the sanitizer integration is not
     working. Report it as a failure rather than exiting 0. */
  fprintf(stderr,
          "control: FAILED -- read freed memory as 0x%02x with no ASan report.\n"
          "         Either MI_TRACK_ASAN silently turned OFF at configure time,\n"
          "         or mimalloc is not poisoning blocks on free.\n",
          observed);
  return 1;
}
