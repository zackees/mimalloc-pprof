/* The macOS VM tag must be an application-specific one (issue #78).

   `mi_option_os_tag` feeds `VM_MAKE_TAG` in `unix_mmap_fd()`, which is how every
   anonymous mapping mimalloc makes is labelled for `vmmap`, `Instruments`, and
   `footprint`. Upstream's default is 100, chosen when "all up to 98 are taken
   officially but LLVM sanitizers had taken 99". Apple has assigned more tags since,
   and 100 is no longer free -- so a mimalloc process reports its entire heap under
   someone else's subsystem. For a fork whose purpose is memory profiling, having the
   platform's own tools misattribute our arenas is a self-inflicted wound.

   This test exists because the claim "240 is the right number" cannot be checked from a
   non-Apple machine: it depends on constants in <mach/vm_statistics.h>. So rather than
   assert a bare literal and hope, on Apple it compares the configured default against
   Apple's own `VM_MEMORY_APPLICATION_SPECIFIC_1` symbol. If Apple ever renumbers, or
   someone changes the default, this fails on the macOS CI job rather than silently
   going wrong somewhere nobody looks.

   Elsewhere the option is inert (`unix_mmap_fd` returns -1 without `VM_MAKE_TAG`, and
   Windows does not use it at all), so the test only checks the value is in the range
   `unix_mmap_fd` would accept without falling back to its 254 default. */

#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <mimalloc.h>
#include <stdio.h>

#if defined(__APPLE__)
#include <TargetConditionals.h>
#if !defined(TARGET_OS_OSX) || TARGET_OS_OSX
#include <mach/vm_statistics.h>
#endif
#endif

int main(void) {
  const long tag = mi_option_get(mi_option_os_tag);
  printf("mi_option_os_tag = %ld\n", tag);

  /* unix_mmap_fd() silently substitutes 254 outside this range, so a default that fell
     outside it would be quietly ignored. */
  if (tag < 100 || tag > 255) {
    fprintf(stderr, "FAIL: os_tag %ld is outside [100,255]; unix_mmap_fd would replace "
                    "it with 254 and the setting would do nothing.\n", tag);
    return 1;
  }

#if defined(__APPLE__) && defined(VM_MEMORY_APPLICATION_SPECIFIC_1)
  printf("VM_MEMORY_APPLICATION_SPECIFIC_1 = %d\n", (int)VM_MEMORY_APPLICATION_SPECIFIC_1);
  if (tag != (long)VM_MEMORY_APPLICATION_SPECIFIC_1) {
    fprintf(stderr,
            "FAIL: os_tag is %ld but VM_MEMORY_APPLICATION_SPECIFIC_1 is %d.\n"
            "The tag must sit in Apple's application-specific range, or vmmap and\n"
            "Instruments attribute mimalloc's arenas to whichever subsystem owns %ld.\n",
            tag, (int)VM_MEMORY_APPLICATION_SPECIFIC_1, tag);
    return 1;
  }
  printf("ok: os_tag matches VM_MEMORY_APPLICATION_SPECIFIC_1\n");
#else
  printf("ok: os_tag in range (Apple-specific check skipped on this platform)\n");
#endif
  return 0;
}
