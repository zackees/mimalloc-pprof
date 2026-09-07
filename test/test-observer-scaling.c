/* ----------------------------------------------------------------------------
Copyright (c) 2026, Microsoft Research, Daan Leijen
This is free software; you can redistribute it and/or modify it under the
terms of the MIT license. A copy of the license can be found in the file
"LICENSE" at the root of this distribution.
-----------------------------------------------------------------------------*/

/* The behavioural half of #371's regression gate.

   `ci/check_fastpath_identity.py` asks whether an atomic read-modify-write is INLINED into
   one of the five fast-path symbols. #371's actual regression was not that shape:
   `dhat_prepare`'s two global RMWs lived in a function the observer hook called
   unconditionally, so the fast path held one extra call instruction and all the damage was
   behind it. No disassembly check of those five bodies would have caught it. What it did
   do is unmistakable in behaviour -- the allocator stopped scaling, and got SLOWER with
   more threads (0.69x from 1 to 8 on the published sweep) -- and that is what this test
   asserts.

   It asserts a RATIO, never a rate. Absolute throughput is a property of the machine, and
   a hosted runner's is neither stable nor knowable in advance; a threshold on Mops/s would
   be a flake generator. Aggregate throughput at N threads divided by aggregate throughput
   at one is a property of the ALLOCATOR, and the regression drove it below 1.0 while every
   other allocator on the same runner measured ~2.6x.

   The bar is deliberately far below what a healthy allocator does: upstream mimalloc, Bun's
   fork, jemalloc and tcmalloc all measure 2.5-2.6x on the 4-vCPU runner, and this fork
   measured 5.3x locally after the fix, so 2.0x leaves a wide margin for a noisy shared
   machine while still failing a regression that lands anywhere near 1.0x. A test that only
   passes on a quiet machine belongs in the RUN_SERIAL group, which is where this is
   registered -- never behind a retry.
*/
#include <mimalloc.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#include <windows.h>
#else
#include <pthread.h>
#include <unistd.h>
#endif
#include <time.h>


/* ---- minimal portable threading and timing (same pattern as test-profile-race.c) ---- */
#ifdef _WIN32
typedef HANDLE thread_t;
static int test_hardware_threads(void) {
  SYSTEM_INFO info; GetSystemInfo(&info); return (int)info.dwNumberOfProcessors;
}
static double test_now_seconds(void) {
  LARGE_INTEGER f, t; QueryPerformanceFrequency(&f); QueryPerformanceCounter(&t);
  return (double)t.QuadPart / (double)f.QuadPart;
}
#else
typedef pthread_t thread_t;
static int test_hardware_threads(void) {
  const long n = sysconf(_SC_NPROCESSORS_ONLN);
  return (n > 0 ? (int)n : 1);
}
static double test_now_seconds(void) {
  struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
  return (double)ts.tv_sec + 1e-9 * (double)ts.tv_nsec;
}
#endif

#define OPS_PER_THREAD   2000000
#define LIVE_SLOTS       256
/* Chosen from measurement, not instinct. Observed aggregate speedup from 1 to 4 threads:
     0.69x  the #371 regression, on a quiet 16-core box
     1.76x  healthy, `pprof-off` bundle on a SHARED 4-vCPU GitHub runner (the worst honest
            reading seen: four threads on four contended vCPUs, and this crude loop has
            none of the published sweep's calibration or paired blocks)
     3.82x  healthy, quiet 16-core box
   The defining symptom of the regression is that adding threads does not help AT ALL --
   it lands below 1.0 -- so a floor of 1.20x fails it with a wide margin while staying well
   under the worst honest reading. This is the coarse, always-runnable canary; the sensitive
   instrument is ci/check_scaling_parity.py, which compares the fork against the other
   mimallocs in the same published run. */
#define MIN_SPEEDUP      1.20

typedef struct { long iterations; } worker_arg_t;

static void tiny_hot_worker(void* argument) {
  worker_arg_t* const arg = (worker_arg_t*)argument;
  void* live[LIVE_SLOTS];
  memset(live, 0, sizeof(live));
  uint64_t x = 0x9E3779B97F4A7C15ull ^ (uintptr_t)&live;
  for (long i = 0; i < arg->iterations; i++) {
    x ^= x << 13; x ^= x >> 7; x ^= x << 17;
    const size_t slot = (size_t)((x >> 3) & (LIVE_SLOTS - 1));
    const size_t size = 16 + (size_t)((x >> 11) & 47);
    mi_free(live[slot]);
    live[slot] = mi_malloc(size);
    if (live[slot] != NULL) { ((char*)live[slot])[0] = (char)i; }
  }
  for (size_t i = 0; i < LIVE_SLOTS; i++) { mi_free(live[i]); }
}


/* Aggregate operations per second across `threads` workers, each doing `iterations`
   malloc/free pairs. Aggregate, not per-thread: the regression showed up as total
   throughput failing to grow with cores. */
static void* tiny_hot_entry(void* argument);

static double test_run_workers(int threads, void (*body)(void*), long iterations) {
  worker_arg_t args[64];
  thread_t handles[64];
  if (threads > 64) { threads = 64; }
  (void)body;
  const double started = test_now_seconds();
  for (int i = 0; i < threads; i++) {
    args[i].iterations = iterations;
#ifdef _WIN32
    handles[i] = CreateThread(NULL, 0, (LPTHREAD_START_ROUTINE)tiny_hot_entry, &args[i], 0, NULL);
    if (handles[i] == NULL) { return -1.0; }
#else
    if (pthread_create(&handles[i], NULL, tiny_hot_entry, &args[i]) != 0) { return -1.0; }
#endif
  }
  for (int i = 0; i < threads; i++) {
#ifdef _WIN32
    WaitForSingleObject(handles[i], INFINITE); CloseHandle(handles[i]);
#else
    pthread_join(handles[i], NULL);
#endif
  }
  const double elapsed = test_now_seconds() - started;
  if (elapsed <= 0.0) { return -1.0; }
  /* one malloc and one free per iteration */
  return ((double)iterations * (double)threads * 2.0) / elapsed;
}

static void* tiny_hot_entry(void* argument) {
  tiny_hot_worker(argument);
  return NULL;
}

int main(void) {
#if defined(MI_OWNER_GATE) && MI_OWNER_GATE
  /* #366: with the owner gate compiled in, every allocator call takes the thread's own
     gate so that `mi_purge_all` can sweep every thread rather than only parked ones. That
     deliberately trades fast-path scaling away -- `windows-gnu-x64-gated` measures 0.91x
     here, which is the gate working as designed, not #371 returning. Asserting scaling
     against a build whose whole point is to serialise would make this test a liar. */
  printf("test-observer-scaling: SKIP (MI_OWNER_GATE trades fast-path scaling by design)\n");
  return 0;
#else
  /* MI_GUARDED with a sample rate of 1 gives EVERY allocation its own guard page, so the
     workload stops exercising the allocator's fast path and becomes a measurement of the
     kernel's mmap/munmap, which serialises on the process's address-space lock. The
     `guarded [sample-rate-1]` bundle measures 0.2 Mops/s and 0.70x here -- 1500x below a
     fast-path rate -- which says nothing about #371 either way. */
  if (mi_option_get(mi_option_guarded_sample_rate) == 1) {
    printf("test-observer-scaling: SKIP (every allocation is guarded; this measures mmap, "
           "not the allocator fast path)\n");
    return 0;
  }
  const int cpus = test_hardware_threads();
  if (cpus < 4) {
    printf("test-observer-scaling: SKIP (needs 4 hardware threads, found %d)\n", cpus);
    return 0;
  }
  const int threads = (cpus < 4 ? cpus : 4);

  double one = test_run_workers(1, tiny_hot_worker, OPS_PER_THREAD);
  double many = test_run_workers(threads, tiny_hot_worker, OPS_PER_THREAD);
  if (one <= 0.0 || many <= 0.0) {
    printf("test-observer-scaling: FAILED -- a measured interval was not positive\n");
    return 1;
  }
  /* A general backstop for a configuration that is not exercising the allocator at all.
     Deliberately LOW. A debug build runs this loop at ~4.5 Mops/s against release's ~370,
     and its ratio is just as meaningful -- silencing every debug configuration would gut
     the gate. What this excludes is the pathological case: guarded sampling measures
     0.2 Mops/s because each allocation is an mmap, and no ratio over that says anything
     about the allocator. The explicit guarded-sampling check above catches the known one;
     this is the belt-and-braces for a future configuration nobody thought of. */
  const double MIN_FASTPATH_RATE = 1e6;
  if (one < MIN_FASTPATH_RATE) {
    printf("test-observer-scaling: SKIP (%.2f Mops/s single-threaded is not a fast-path "
           "rate; this configuration is not exercising it)\n", one / 1e6);
    return 0;
  }
  const double speedup = many / one;
  printf("test-observer-scaling: 1 thread %.1f Mops/s, %d threads %.1f Mops/s, speedup %.2fx\n",
         one / 1e6, threads, many / 1e6, speedup);
  if (speedup < MIN_SPEEDUP) {
    printf("test-observer-scaling: FAILED -- aggregate throughput scaled %.2fx from 1 to %d "
           "threads, below the %.2fx floor.\n"
           "  This is #371: an observer hook doing shared-memory work on every allocation "
           "serialises the allocator.\n"
           "  The flags must be tested BEFORE any shared-memory traffic -- see "
           "MI_OBSERVERS_INITIAL in include/mimalloc/internal.h.\n",
           speedup, threads, MIN_SPEEDUP);
    return 1;
  }
  printf("test-observer-scaling: OK\n");
  return 0;
#endif
}
