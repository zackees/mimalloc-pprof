/* mi_process_info's current_rss must be RSS, not committed bytes (issue #78).

   On Linux, `_mi_prim_process_info` used to set `peak_rss` but never `current_rss`, so
   the field kept the default `mi_process_info()` assigns it -- `current_commit`,
   mimalloc's own committed counter. Every consumer (`mi_process_info`,
   `mi_stats_print`, and the `rss_current` field of `mi_stats_as_json`) therefore
   reported committed bytes under the name RSS. Bun measured 4.91 GB vs 0.56 GB on a
   real workload.

   The test allocates a large region and touches only one byte per OS page in a small
   prefix of it. Committed then greatly exceeds resident, so a build that confuses the
   two reports the two values as equal and fails here.

   Deliberately tolerant about the exact numbers -- RSS depends on the OS, the page
   size, and what else the process has faulted in. On Windows, equality between the
   independent WorkingSetSize and PagefileUsage counters is possible and does not prove
   that RSS fell back to mimalloc's committed counter. There we bracket mi_process_info
   with direct PROCESS_MEMORY_COUNTERS samples and compare against that OS oracle. */

#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <mimalloc.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>

#if defined(_WIN32)
#include <windows.h>
#include <psapi.h>
#endif

#define BLOCKS     64
#define BLOCKSZ    (4u << 20)      /* 4 MiB each -> 256 MiB committed */
#define TOUCH_FRAC 16u             /* touch only 1/16th of each block */

static void* keep[BLOCKS];

#if defined(_WIN32)

/* 1.6% of the 256 MiB workload: enough for a short-lived trim between the three
   adjacent samples without accepting the old hundreds-of-MiB fallback error. */
#define ORACLE_TOLERANCE (4u << 20)

typedef BOOL (WINAPI *test_get_process_memory_info_t)(HANDLE, PPROCESS_MEMORY_COUNTERS, DWORD);

typedef struct test_process_counters_s {
  size_t rss;
  size_t commit;
  DWORD error;
  bool ok;
} test_process_counters_t;

static test_get_process_memory_info_t test_get_process_memory_info = NULL;
static DWORD test_psapi_error = ERROR_SUCCESS;

static bool test_process_oracle_init(void) {
  HMODULE psapi = LoadLibraryW(L"psapi.dll");
  if (psapi == NULL) {
    test_psapi_error = GetLastError();
    return false;
  }
  test_get_process_memory_info =
      (test_get_process_memory_info_t)(void (*)(void))GetProcAddress(psapi, "GetProcessMemoryInfo");
  if (test_get_process_memory_info == NULL) {
    test_psapi_error = GetLastError();
    return false;
  }
  return true;
}

static test_process_counters_t test_process_oracle_query(void) {
  test_process_counters_t counters;
  PROCESS_MEMORY_COUNTERS info;
  memset(&counters, 0, sizeof(counters));
  memset(&info, 0, sizeof(info));
  info.cb = sizeof(info);
  if (test_get_process_memory_info == NULL) {
    counters.error = test_psapi_error;
    return counters;
  }
  if (!test_get_process_memory_info(GetCurrentProcess(), &info, sizeof(info))) {
    counters.error = GetLastError();
    return counters;
  }
  counters.rss = (size_t)info.WorkingSetSize;
  counters.commit = (size_t)info.PagefileUsage;
  counters.ok = true;
  return counters;
}

static bool test_value_matches_bracket(size_t value, size_t before, size_t after,
                                       size_t tolerance) {
  const size_t lower_value = (before < after ? before : after);
  const size_t upper_value = (before > after ? before : after);
  const size_t lower = (lower_value > tolerance ? lower_value - tolerance : 0);
  const size_t size_max = (size_t)-1;
  const size_t upper = (upper_value > size_max - tolerance ? size_max : upper_value + tolerance);
  return (value >= lower && value <= upper);
}

static bool test_report_matches_oracle(size_t rss, size_t commit,
                                       const test_process_counters_t* before,
                                       const test_process_counters_t* after,
                                       size_t tolerance) {
  return (before->ok && after->ok &&
          test_value_matches_bracket(rss, before->rss, after->rss, tolerance) &&
          test_value_matches_bracket(commit, before->commit, after->commit, tolerance));
}

static void test_windows_oracle_positive_control(void) {
  const test_process_counters_t before = { 64u << 20, 128u << 20, ERROR_SUCCESS, true };
  const test_process_counters_t after = before;
  assert(test_report_matches_oracle(before.rss, before.commit, &before, &after, 0));
  assert(!test_report_matches_oracle(before.commit, before.commit, &before, &after, 0));
}

#endif

int main(void) {
#if defined(_WIN32)
  test_windows_oracle_positive_control();
  const bool os_init_ok = test_process_oracle_init();
#endif

  size_t elapsed, user, sys, rss0, prss, commit0, pcommit, faults;
  mi_process_info(&elapsed, &user, &sys, &rss0, &prss, &commit0, &pcommit, &faults);

  for (int i = 0; i < BLOCKS; i++) {
    keep[i] = mi_malloc(BLOCKSZ);
    assert(keep[i] != NULL);
    /* Touch one byte every 4 KiB, but only across the first 1/TOUCH_FRAC of the block,
       so the rest stays committed-but-not-resident. */
    unsigned char* b = (unsigned char*)keep[i];
    for (size_t off = 0; off < BLOCKSZ / TOUCH_FRAC; off += 4096) {
      b[off] = (unsigned char)i;
    }
  }

  size_t rss, commit;
#if defined(_WIN32)
  const test_process_counters_t os_before = test_process_oracle_query();
#endif
  mi_process_info(&elapsed, &user, &sys, &rss, &prss, &commit, &pcommit, &faults);
#if defined(_WIN32)
  const test_process_counters_t os_after = test_process_oracle_query();
#endif

  printf("rss=%zu KiB  commit=%zu KiB  (baseline rss=%zu commit=%zu)\n",
         rss / 1024, commit / 1024, rss0 / 1024, commit0 / 1024);

#if !defined(_WIN32)
  if (commit == 0) {
    printf("ok: no commit accounting on this platform; nothing to compare\n");
    for (int i = 0; i < BLOCKS; i++) mi_free(keep[i]);
    return 0;
  }
#endif

#if defined(_WIN32)
  if (!os_before.ok || !os_after.ok) {
    fprintf(stderr,
            "FAIL: Windows RSS oracle unavailable: init_ok=%d error=%lu; "
            "before_ok=%d error=%lu rss=%zu KiB commit=%zu KiB; "
            "after_ok=%d error=%lu rss=%zu KiB commit=%zu KiB; "
            "mimalloc rss=%zu KiB commit=%zu KiB\n",
            (int)os_init_ok, (unsigned long)test_psapi_error,
            (int)os_before.ok, (unsigned long)os_before.error,
            os_before.rss / 1024, os_before.commit / 1024,
            (int)os_after.ok, (unsigned long)os_after.error,
            os_after.rss / 1024, os_after.commit / 1024,
            rss / 1024, commit / 1024);
    return 1;
  }

  if (!test_report_matches_oracle(rss, commit, &os_before, &os_after, ORACLE_TOLERANCE)) {
    fprintf(stderr,
            "FAIL: mi_process_info differs from the Windows OS oracle: "
            "mimalloc rss=%zu KiB commit=%zu KiB; "
            "before_ok=%d error=%lu rss=%zu KiB commit=%zu KiB; "
            "after_ok=%d error=%lu rss=%zu KiB commit=%zu KiB; "
            "tolerance=%u KiB\n",
            rss / 1024, commit / 1024,
            (int)os_before.ok, (unsigned long)os_before.error,
            os_before.rss / 1024, os_before.commit / 1024,
            (int)os_after.ok, (unsigned long)os_after.error,
            os_after.rss / 1024, os_after.commit / 1024,
            (unsigned)(ORACLE_TOLERANCE / 1024));
    return 1;
  }

  if (rss == commit) {
    printf("ok: Windows OS oracle confirms equal RSS and commit counters\n");
  }
  else {
    printf("ok: Windows RSS and commit match the bracketing OS counters\n");
  }
#else
  if (rss == commit) {
    fprintf(stderr,
            "FAIL: current_rss == current_commit (%zu). RSS is being reported as\n"
            "committed bytes -- see _mi_prim_process_info. %u MiB was committed and\n"
            "only about 1/%u of it touched, so these must differ.\n",
            rss, (unsigned)((BLOCKS * (size_t)BLOCKSZ) >> 20), TOUCH_FRAC);
    return 1;
  }

  /* Deliberately NO assertion that rss < commit.
     `commit` counts only what MIMALLOC committed; `rss` is the whole process, including
     the binary, stacks, libc, and any sanitizer's shadow memory. Under AddressSanitizer
     the shadow and redzones push RSS above mimalloc's own commit counter as a matter of
     course -- the asan job measured rss=321 MiB against commit=285 MiB. An earlier
     version of this test asserted rss <= commit and failed there, contradicting the
     comment at the top of this file about RSS depending on "what else the process has
     faulted in". The only thing actually guaranteed is that the two are not the SAME
     number, which is what the check above tests. */
  printf("ok: rss and commit differ (rss is %.1f%% of commit)\n",
         100.0 * (double)rss / (double)commit);
#endif

  for (int i = 0; i < BLOCKS; i++) mi_free(keep[i]);
  return 0;
}
