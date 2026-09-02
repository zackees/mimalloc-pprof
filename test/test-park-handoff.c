/* ----------------------------------------------------------------------------
Copyright (c) 2026, Microsoft Research, Daan Leijen
This is free software; you can redistribute it and/or modify it under the
terms of the MIT license. A copy of the license can be found in the file
"LICENSE" at the root of this distribution.
-----------------------------------------------------------------------------*/

// imported from oven-sh/mimalloc @ 942b8342, MIT (issue #272 / Bun parity P7a).
//
// TWO DELIBERATE ADAPTATIONS for this tree, both marked `Phase 7b` inline:
//  1. Bun's "did a sweep run?" observable is `mi_purge_holes_stats_get().discard_calls`, which
//     belongs to hole purging (Phase 7b of #272). Here it is `_mi_test_idle_work_count()`
//     (src/scavenger.c): the number of completed idle-work passes, which is the same signal for
//     everything 7a can do and is in fact stricter (it counts passes, not syscalls). 7b adds the
//     discard-count assertions on top.
//  2. The two cases about `mi_option_purge_holes_min_interval` sweep pacing
//     (`test_park_inside_window_gets_swept`, and the spacing inside `test_parks_get_swept`) need
//     that option and are `#if 0 // Phase 7b` until it exists.

// `mi_on_thread_idle_start`/`mi_on_thread_idle_end`: a thread that is about to block hands its
// theaps to the scavenger, which sweeps them while it is in the kernel.
//
// Holes need scattered survivors pinning a page, so every case here keeps one block every
// KEEP_EVERY: two whole OS pages of blocks between survivors, whatever the OS page size (4KB or
// the 16KB of Apple Silicon), so free runs cover whole OS pages inside a page that is still
// used. Freeing a contiguous run instead would empty whole mimalloc pages, which go back through
// the arena and never exercise hole punching at all.

#if defined(_WIN32)
#include <stdio.h>
int main(void) { printf("test-park-handoff: skipped on Windows (uses pthreads/fork)\n"); return 0; }
#else

#include "mimalloc.h"
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sys/wait.h>
#include <stdatomic.h>

static int failures = 0;

static void check(const char* name, bool ok) {
  fprintf(stderr, "test: %s...  %s\n", name, ok ? "ok." : "FAILED");
  if (!ok) failures++;
}

#if defined(MI_GUARDED)
#define LIVE   (2000)     // every sampled allocation gets a guard page (its own mapping): stay under vm.max_map_count
#else
#define LIVE   (20000)
#endif

// `test_park_then_exit`, `test_exit_while_swept_with_dyn_tls`, and `test_exit_while_swept_stress`
// (below) each deliberately leave every churn/`RACE_CLASSES` survivor allocated for the life of
// the process -- see the "on purpose" comments at their call sites; the pages/holes have to stay
// live for the race they are hunting. Under MI_GUARDED every survivor is its own mapping, so
// those permanent leaks are VMAs, not just bytes, and they stack across rounds and across all
// three tests: at the FULL counts below and 1-in-1 guarding they leak ~134k mappings total --
// well past ctest-guarded's CI `vm.max_map_count` of 65,530 (passes locally, where the default
// is 1,048,576; measured pre-fix peak ~304,606, from these leaks plus transient churn/DTOR_BLOCKS
// concurrency on top).
//
// MI_GUARDED alone does NOT mean every allocation is guarded, and it is compiled into every
// MI_DEBUG build regardless of this test's needs (CMakeLists.txt: "Enable MI_GUARDED (since
// MI_DEBUG=ON)") -- the default `guarded_sample_rate` only guards 1-in-4000, so `ctest-debug-full`
// (the job whose MI_DEBUG_FULL assertions can actually catch the #272 race this corpus hunts,
// per the comment on `_mi_park_leave_if_parked`) barely triggers it. Only `ctest-guarded`'s
// second pass, which forces `MIMALLOC_GUARDED_SAMPLE_RATE=1`, guards everything and hits the VMA
// cap. So gate on the actual runtime sample rate, not just the compile flag: `ctest-debug-full`
// keeps the FULL counts (its discriminating power for #272 is unchanged), and only a forced
// near-1-in-1 sample rate scales down to the BOUND counts. Measured: reverting
// `_mi_park_leave_if_parked`'s two call sites (src/page.c, src/free.c) and running the FULL-count
// build with the default sample rate 50 times still catches the regression -- see the PR body
// for the count.
#define PARK_THEN_EXIT_THREADS_FULL   (8)
#define PARK_THEN_EXIT_ROUNDS_FULL    (10)
#define DYN_TLS_THREADS_FULL          (8)
#define DYN_TLS_ROUNDS_FULL           (12)
#define SWEPT_STRESS_THREADS_FULL     (12)
#define SWEPT_STRESS_ROUNDS_FULL      (60)

#define PARK_THEN_EXIT_THREADS_BOUND  (4)
#define PARK_THEN_EXIT_ROUNDS_BOUND   (4)
#define DYN_TLS_THREADS_BOUND         (4)
#define DYN_TLS_ROUNDS_BOUND          (4)
#define SWEPT_STRESS_THREADS_BOUND    (4)
#define SWEPT_STRESS_ROUNDS_BOUND     (8)

// true only when (almost) every in-range allocation is actually being guarded right now, not
// merely when MI_GUARDED was compiled in (see the comment above).
static bool guard_every_alloc(void) {
#if defined(MI_GUARDED)
  const long rate = mi_option_get(mi_option_guarded_sample_rate);
  return (rate > 0 && rate <= 8);   // ctest-guarded's forced pass uses 1; the default is ~4000
#else
  return false;
#endif
}
#define BSZ    (512)
// two OS pages of BSZ blocks between survivors (+2 for the margin), as in test-purge-holes.c
static size_t keep_every(void) {
  return ((((size_t)2 * (size_t)sysconf(_SC_PAGESIZE)) + BSZ - 1) / BSZ) + 2;
}

// A per-(block,byte) pattern so that a purge, a free-list rewrite, or a hole punched one OS page
// too far in either direction shows up as a byte mismatch and not just as a crash.
static uint8_t pattern_byte(size_t id, size_t off) {
  return (uint8_t)((id * 131u) ^ (off * 7u) ^ (off >> 8));
}
static void pattern_fill(void* p, size_t size, size_t id) {
  uint8_t* b = (uint8_t*)p;
  for (size_t i = 0; i < size; i++) { b[i] = pattern_byte(id, i); }
}
// returns the offset of the first corrupt byte, or `size` when intact
static size_t pattern_check(const void* p, size_t size, size_t id) {
  const uint8_t* b = (const uint8_t*)p;
  for (size_t i = 0; i < size; i++) { if (b[i] != pattern_byte(id, i)) return i; }
  return size;
}

static void churn(void** p) {
  const size_t ke = keep_every();
  for (int i = 0; i < LIVE; i++) { if (p[i] == NULL) { p[i] = mi_malloc(BSZ); memset(p[i], 1, BSZ); } }
  for (int i = 0; i < LIVE; i++) { if (((size_t)i % ke) != 0 && p[i] != NULL) { mi_free(p[i]); p[i] = NULL; } }
}

// churn, but with a checkable pattern in every survivor
static void churn_pattern(void** p) {
  const size_t ke = keep_every();
  for (int i = 0; i < LIVE; i++) {
    if (p[i] == NULL) { p[i] = mi_malloc(BSZ); pattern_fill(p[i], BSZ, (size_t)i); }
  }
  for (int i = 0; i < LIVE; i++) { if (((size_t)i % ke) != 0 && p[i] != NULL) { mi_free(p[i]); p[i] = NULL; } }
}

// index of the first survivor with a corrupt byte, or -1 when every survivor is intact
static long first_corrupt_survivor(void** p) {
  for (int i = 0; i < LIVE; i++) {
    if (p[i] != NULL && pattern_check(p[i], BSZ, (size_t)i) != BSZ) return (long)i;
  }
  return -1;
}

// Phase 7b adaptation (see the file header): Bun reads `mi_purge_holes_stats_get().discard_calls`.
extern size_t _mi_test_idle_work_count(void);
static size_t discards(void) {
  return _mi_test_idle_work_count();
}

// wait (bounded) for the handoff to have actually done a discard, standing in for a syscall
static bool wait_for_discard_after(size_t before) {
  for (int i = 0; i < 20000; i++) { if (discards() > before) return true; usleep(100); }
  return discards() > before;
}

// ---------------------------------------------------------------------------
// The scavenger thread exists only once there is something for it to do (a second thread, or a
// park), and it must not block the signals a fault on it raises, or a crash during a sweep it does
// for a parked thread ends the process without the host's crash report. Linux only: both are read
// from /proc.
// ---------------------------------------------------------------------------
#if defined(__linux__)
#include <dirent.h>
#include <signal.h>
// the blocked-signal mask of the thread named "mi-scavenger" (0 if there is none yet)
static unsigned long long scavenger_sigblk(int* count) {
  unsigned long long mask = 0;
  *count = 0;
  DIR* d = opendir("/proc/self/task");
  if (d == NULL) return 0;
  struct dirent* e;
  while ((e = readdir(d)) != NULL) {
    if (e->d_name[0] == '.') continue;
    char path[300]; char line[256];
    snprintf(path, sizeof(path), "/proc/self/task/%s/comm", e->d_name);
    FILE* f = fopen(path, "r");
    if (f == NULL) continue;
    const bool is_scav = (fgets(line, sizeof(line), f) != NULL && strncmp(line, "mi-scavenger", 12) == 0);
    fclose(f);
    if (!is_scav) continue;
    (*count)++;
    snprintf(path, sizeof(path), "/proc/self/task/%s/status", e->d_name);
    f = fopen(path, "r");
    if (f == NULL) continue;
    while (fgets(line, sizeof(line), f) != NULL) {
      if (strncmp(line, "SigBlk:", 7) == 0) { mask = strtoull(line + 7, NULL, 16); break; }
    }
    fclose(f);
  }
  closedir(d);
  return mask;
}
static void test_lazy_start_and_signals(void) {
  if (!mi_option_is_enabled(mi_option_scavenger)) { fprintf(stderr, "test: scavenger lazy start...  skipped (scavenger disabled)\n"); return; }
  void* q = mi_malloc(100); mi_free(q);
  int n = 0;
  scavenger_sigblk(&n);
  check("no scavenger thread for a process that only ever had one thread", n == 0);
  if (mi_on_thread_idle_start()) { mi_on_thread_idle_end(); }
  // it names itself and sets its mask first thing; wait for that rather than assume it
  unsigned long long blk = 0;
  for (int i = 0; i < 20000 && (blk = scavenger_sigblk(&n)) == 0; i++) { usleep(100); }
  check("the first park starts it", n == 1);
  const unsigned long long segv = 1ULL << (SIGSEGV - 1), bus = 1ULL << (SIGBUS - 1), term = 1ULL << (SIGTERM - 1);
  check("the scavenger blocks process-directed signals", (blk & term) != 0);
  check("but not the ones a fault on it raises", (blk & (segv | bus)) == 0);
}
#else
static void test_lazy_start_and_signals(void) { fprintf(stderr, "test: scavenger lazy start / signal mask...  skipped (needs /proc)\n"); }
#endif

// ---------------------------------------------------------------------------
// The handoff does the same work as the inline sweep -- and when there is nobody to hand off to
// it says so rather than sweeping inline behind the caller's back.
// ---------------------------------------------------------------------------
static void test_handoff_sweeps(void) {
  void** p = (void**)calloc(LIVE, sizeof(void*));
  if (p == NULL) return;
  churn(p);
  const size_t before = discards();
  const bool parked = mi_on_thread_idle_start();
  if (parked) {
    // Stand in for a blocking syscall: the sweep is asynchronous, so wait for it rather than
    // assuming it happened. Bounded so a broken handoff fails instead of hanging.
    for (int i = 0; i < 20000 && discards() == before; i++) { usleep(100); }
    mi_on_thread_idle_end();
    check("handoff sweeps the parked thread's heaps", discards() > before);
  }
  else {
    // No scavenger: `_start` must be a no-op, NOT an inline sweep. A caller parks far more often
    // than it is idle, so sweeping here is the between-task sweep it is trying to avoid.
    check("_start does not sweep inline when it cannot hand off", discards() == before);
    mi_on_thread_idle();   // what such a caller does instead, when it decides it is idle
    check("the caller can still sweep for itself", discards() > before);
  }
  for (int i = 0; i < LIVE; i++) { if (p[i] != NULL) mi_free(p[i]); }
  free(p);
}

// ---------------------------------------------------------------------------
// _end with no _start, and _start twice, must not corrupt the park state.
// ---------------------------------------------------------------------------
static void test_unbalanced(void) {
  mi_on_thread_idle_end();          // no matching start
  (void)mi_on_thread_idle_start();
  (void)mi_on_thread_idle_start();  // twice
  mi_on_thread_idle_end();
  mi_on_thread_idle_end();          // and one too many
  void* q = mi_malloc(64);     // the thread must still be able to allocate
  check("unbalanced start/end leaves the thread usable", q != NULL);
  mi_free(q);
}

// ---------------------------------------------------------------------------
// A thread that parks and then EXITS without ever calling `mi_on_thread_idle_end`. `epoll_wait` is
// a pthread_cancel cancellation point, and pthread_exit and unwinding leave the same way. Teardown
// then frees the tld -- and destroys `theaps_lock` -- which the scavenger may be walking right now.
// Without `_mi_park_leave` in `_mi_thread_done` this is a use-after-free: it reports races under
// the thread sanitizer and trips the assertion in `mi_tld_unregister`.
// ---------------------------------------------------------------------------
static void* park_then_exit(void* arg) {
  (void)arg;
  void** p = (void**)calloc(LIVE, sizeof(void*));
  if (p == NULL) return NULL;
  churn(p);
  free(p);                     // the mi blocks stay allocated on purpose: the pages must stay live
  (void)mi_on_thread_idle_start();
  usleep(200);                 // let the scavenger claim and start sweeping
  pthread_exit(NULL);          // ...and leave without _end
}

static void* park_then_cancel(void* arg) {
  (void)arg;
  void** p = (void**)calloc(LIVE, sizeof(void*));
  if (p == NULL) return NULL;
  churn(p);
  free(p);
  (void)mi_on_thread_idle_start();
  for (;;) { pthread_testcancel(); usleep(50); }   // cancelled mid-park, as at a blocking syscall
}

static void test_park_then_exit(void) {
  enum { THREADS_MAX = PARK_THEN_EXIT_THREADS_FULL };
  const bool bound = guard_every_alloc();
  const int THREADS = bound ? PARK_THEN_EXIT_THREADS_BOUND : PARK_THEN_EXIT_THREADS_FULL;
  const int ROUNDS  = bound ? PARK_THEN_EXIT_ROUNDS_BOUND  : PARK_THEN_EXIT_ROUNDS_FULL;
  for (int r = 0; r < ROUNDS; r++) {
    pthread_t t[THREADS_MAX];
    for (int i = 0; i < THREADS; i++) {
      if (pthread_create(&t[i], NULL, ((i % 2) != 0 ? &park_then_exit : &park_then_cancel), NULL) != 0) return;
    }
    usleep(500);
    for (int i = 0; i < THREADS; i++) { if ((i % 2) == 0) pthread_cancel(t[i]); }
    for (int i = 0; i < THREADS; i++) { pthread_join(t[i], NULL); }
  }
  check("a thread may exit while still parked", true);   // reaching here without a crash IS the test
}

// ---------------------------------------------------------------------------
// Many threads parking and waking at randomized moments, so the reclaim lands both before and in
// the middle of a sweep.
// ---------------------------------------------------------------------------
static int stress_corrupt = 0;   // set by a worker on the first corrupt survivor (racy write is fine: it only latches)

static void* park_stress(void* arg) {
  unsigned seed = (unsigned)(uintptr_t)arg * 2654435761u;
  void** p = (void**)calloc(LIVE, sizeof(void*));
  if (p == NULL) return NULL;
  for (int r = 0; r < 100; r++) {
    churn_pattern(p);
    (void)mi_on_thread_idle_start();
    if ((rand_r(&seed) % 4) == 0) { usleep(rand_r(&seed) % 200); }
    mi_on_thread_idle_end();
    void* q = mi_malloc(64); mi_free(q);   // allocate immediately on wake: must be safe
    // a wake that raced a sweep (an aborted reclaim) must still leave every survivor intact
    if (first_corrupt_survivor(p) >= 0) { stress_corrupt = 1; break; }
  }
  for (int i = 0; i < LIVE; i++) { if (p[i] != NULL) mi_free(p[i]); }
  free(p);
  return NULL;
}

// #272 (this fork): `mi_collect(true)` now WAITS for `_mi_arenas_try_purge`'s
// one-purger-at-a-time guard instead of skipping when it is held, because the scavenger sits
// in that same function on a timer and a forced purge that silently does nothing broke
// `test-degenerate`'s thread-churn bound. Turning a non-blocking guard into a blocking one
// deserves a test that hammers it: a thread doing nothing but forced collects, against the
// park/wake stress below.
static volatile int collect_stop;

static void* forced_collect_loop(void* arg) {
  (void)arg;
  while (collect_stop == 0) { mi_collect(true); usleep(200); }
  return NULL;
}

static void test_park_stress(void) {
  enum { THREADS = 4 };
  pthread_t t[THREADS];
  pthread_t collector;
  bool have_collector = false;
  stress_corrupt = 0;
  collect_stop = 0;
  if (pthread_create(&collector, NULL, &forced_collect_loop, NULL) == 0) { have_collector = true; }
  for (long i = 0; i < THREADS; i++) {
    if (pthread_create(&t[i], NULL, &park_stress, (void*)i) != 0) {
      collect_stop = 1;
      if (have_collector) pthread_join(collector, NULL);
      return;
    }
  }
  for (int i = 0; i < THREADS; i++) { pthread_join(t[i], NULL); }
  collect_stop = 1;
  if (have_collector) pthread_join(collector, NULL);   // a hang here IS the deadlock this guards
  check("concurrent park/wake keeps every survivor intact", stress_corrupt == 0);
  check("forced mi_collect against a live scavenger neither hangs nor corrupts", true);
}

// ---------------------------------------------------------------------------
// A handoff sweep does the same work as an inline one, so the survivors must come back byte-for-
// byte intact -- a free-list rewrite or a hole punched over a live block corrupts, not crashes.
// This is the check that turns every other test's "did not crash" into "the heap is intact".
// ---------------------------------------------------------------------------
static void test_survivors_intact(void) {
  void** p = (void**)calloc(LIVE, sizeof(void*));
  if (p == NULL) return;
  churn_pattern(p);
  const size_t before = discards();
  const bool parked = mi_on_thread_idle_start();
  if (parked) { wait_for_discard_after(before); }
  mi_on_thread_idle_end();
  if (!parked) { mi_on_thread_idle(); }   // no scavenger: do the sweep ourselves so it is not vacuous
  // a sweep that never ran would leave the survivors trivially intact -- assert it did run
  check("a sweep ran before checking survivors", discards() > before);
  const long bad = first_corrupt_survivor(p);
  if (bad >= 0) { fprintf(stderr, "\n  CORRUPT survivor block=%ld byte=%zu\n", bad, pattern_check(p[bad], BSZ, (size_t)bad)); }
  check("survivor bytes intact after a sweep", bad < 0);
  // and the swept pages must still allocate correctly afterwards
  for (int i = 0; i < LIVE; i++) { if (p[i] == NULL) { p[i] = mi_malloc(BSZ); pattern_fill(p[i], BSZ, (size_t)i); } }
  check("refill after sweep is intact", first_corrupt_survivor(p) < 0);
  for (int i = 0; i < LIVE; i++) { if (p[i] != NULL) mi_free(p[i]); }
  free(p);
}

// ---------------------------------------------------------------------------
// A THIRD thread frees a parked thread's blocks while the scavenger sweeps them: cross-thread
// frees land on the page's xthread list, which the sweep folds. Every block must end up freed
// exactly once and no survivor may be corrupted.
// ---------------------------------------------------------------------------
typedef struct third_free_args_s {
  void** p;
  atomic_int go;
  atomic_int done;
} third_free_args_t;

static void* third_thread_freer(void* varg) {
  third_free_args_t* a = (third_free_args_t*)varg;
  while (!atomic_load(&a->go)) { usleep(50); }
  // free every survivor at an even index; odd survivors stay live for the owner to verify
  for (int i = 0; i < LIVE; i++) {
    if ((i % 2) == 0 && a->p[i] != NULL) { mi_free(a->p[i]); a->p[i] = NULL; }
  }
  atomic_store(&a->done, 1);
  return NULL;
}

static void test_third_thread_frees_during_sweep(void) {
  enum { ROUNDS = 20 };
  bool intact = true;
  for (int r = 0; r < ROUNDS && intact; r++) {
    void** p = (void**)calloc(LIVE, sizeof(void*));
    if (p == NULL) return;
    churn_pattern(p);
    third_free_args_t args = { .p = p };
    atomic_init(&args.go, 0); atomic_init(&args.done, 0);
    pthread_t t;
    if (pthread_create(&t, NULL, &third_thread_freer, &args) != 0) { free(p); return; }
    const bool parked = mi_on_thread_idle_start();
    atomic_store(&args.go, 1);                  // the frees race the (possibly running) sweep
    while (!atomic_load(&args.done)) { usleep(50); }
    mi_on_thread_idle_end();
    if (!parked) { mi_on_thread_idle(); }
    pthread_join(t, NULL);
    // the odd survivors are still live and must be intact
    for (int i = 1; i < LIVE; i += 2) {
      if (p[i] != NULL && pattern_check(p[i], BSZ, (size_t)i) != BSZ) { intact = false; break; }
    }
    for (int i = 0; i < LIVE; i++) { if (p[i] != NULL) mi_free(p[i]); }
    free(p);
  }
  check("third-thread frees during a sweep keep survivors intact", intact);
}

// ---------------------------------------------------------------------------
// A single thread parks repeatedly: each park with fresh holes must be swept within a deadline far
// under the scavenger's 30s safety timeout. Sweeps of one thread are rate-limited to
// `purge_holes_min_interval` (100ms by default), so space the parks past it (the in-window case
// is `test_park_inside_window_gets_swept`); the `-eager` ctest variant sets the interval to 0.
// ---------------------------------------------------------------------------
static void test_parks_get_swept(void) {
  enum { ROUNDS = 20 };
  const long interval_ms = 0;   // Phase 7b: `mi_option_get(mi_option_purge_holes_min_interval)`
  void** p = (void**)calloc(LIVE, sizeof(void*));
  if (p == NULL) return;
  int missed = 0;
  int handed_off = 0;
  for (int r = 0; r < ROUNDS; r++) {
    churn(p);   // fresh holes to punch, so a swept park is observable
    if (interval_ms > 0) { usleep((useconds_t)(interval_ms * 1000 + 5000)); }   // clear the rate window
    const size_t before = discards();
    const bool parked = mi_on_thread_idle_start();
    if (parked) {
      handed_off++;
      bool swept = false;
      for (int i = 0; i < 3000 && !swept; i++) { swept = (discards() > before); if (!swept) usleep(1000); }
      if (!swept) missed++;
    }
    mi_on_thread_idle_end();
    for (int i = 0; i < LIVE; i++) { if (p[i] == NULL) { p[i] = mi_malloc(BSZ); memset(p[i], 3, BSZ); } }
  }
  for (int i = 0; i < LIVE; i++) { if (p[i] != NULL) mi_free(p[i]); }
  free(p);
  fprintf(stderr, "  parks handed off: %d, unswept: %d (min_interval=%ldms)\n", handed_off, missed, interval_ms);
  check("every spaced park gets swept promptly", missed == 0);
}

// ---------------------------------------------------------------------------
// The common idle shape: swept, woken briefly by a timer, parked again inside `min_interval` --
// this time for long. That second park is passed over when it starts, and must be swept once the
// window ends rather than at the scavenger's next unrelated wake (up to its 30s safety timeout).
// ---------------------------------------------------------------------------
#if 0 // Phase 7b (#272): needs `mi_option_purge_holes_min_interval`
static void test_park_inside_window_gets_swept(void) {
  const long interval_ms = mi_option_get(mi_option_purge_holes_min_interval);
  if (interval_ms <= 0) return;   // no window (the `-eager` variant)
  void** p = (void**)calloc(LIVE, sizeof(void*));
  if (p == NULL) return;
  // first park: spaced, so it is swept and stamps `holes_sweep_last`
  churn(p);
  usleep((useconds_t)(interval_ms * 1000 + 5000));
  size_t before = discards();
  bool parked = mi_on_thread_idle_start();
  bool first_swept = parked && wait_for_discard_after(before);
  mi_on_thread_idle_end();
  if (!parked) { for (int i = 0; i < LIVE; i++) { mi_free(p[i]); } free(p); return; }   // nobody to hand off to (no scavenger)
  if (!first_swept) { free(p); check("park inside the rate window is swept when the window ends", false); return; }
  // second park: straight away, inside the window, with fresh holes
  for (int i = 0; i < LIVE; i++) { if (p[i] == NULL) { p[i] = mi_malloc(BSZ); memset(p[i], 3, BSZ); } }
  churn(p);
  before = discards();
  struct timespec t0, t1;
  clock_gettime(CLOCK_MONOTONIC, &t0);
  parked = mi_on_thread_idle_start();
  long waited_ms = -1;
  if (parked) {
    const long deadline_ms = interval_ms * 10 + 1000;   // far under 30s, generous over `interval_ms`
    for (;;) {
      clock_gettime(CLOCK_MONOTONIC, &t1);
      const long elapsed = (long)(t1.tv_sec - t0.tv_sec) * 1000 + (long)(t1.tv_nsec - t0.tv_nsec) / 1000000;
      if (discards() > before) { waited_ms = elapsed; break; }
      if (elapsed > deadline_ms) break;
      usleep(1000);
    }
  }
  mi_on_thread_idle_end();
  for (int i = 0; i < LIVE; i++) { if (p[i] != NULL) mi_free(p[i]); }
  free(p);
  fprintf(stderr, "  in-window park swept after %ldms (min_interval=%ldms)\n", waited_ms, interval_ms);
  check("park inside the rate window is swept when the window ends", parked && waited_ms >= 0);
}

#endif // Phase 7b
static void test_park_inside_window_gets_swept(void) {
  fprintf(stderr, "test: park inside the rate window is swept when the window ends...  skipped (Phase 7b)\n");
}

// ---------------------------------------------------------------------------
// fork() by a thread that is between _start and _end -- fork does not allocate, so the contract
// permits it, and the scavenger may be part-way through rewriting this thread's page free lists.
// The damage is in those *freed* holes, not the survivors: the child must be able to allocate
// straight out of the churned pages' free lists without meeting a corrupt entry (which aborts) and
// without two allocations aliasing. Refilling exactly the freed slots forces exactly that path.
// ---------------------------------------------------------------------------
static void test_fork_while_parked(void) {
  enum { ROUNDS = 16 };
  bool all_ok = true;
  for (int r = 0; r < ROUNDS && all_ok; r++) {
    void** p = (void**)calloc(LIVE, sizeof(void*));
    if (p == NULL) return;
    churn_pattern(p);
    (void)mi_on_thread_idle_start();
    usleep(150 + (unsigned)((r * 37) % 400));   // land the clone before and inside a sweep
    const pid_t pid = fork();
    if (pid == 0) {
      // child: allocate out of the inherited free lists and check for aliasing
      const size_t ke = keep_every();
      int bad = 0;
      for (int i = 0; i < LIVE; i++) {
        if (p[i] == NULL) { p[i] = mi_malloc(BSZ); if (p[i] == NULL) { bad = 1; break; } memset(p[i], (int)(i & 0xFF), BSZ); }
      }
      for (int i = 0; !bad && i < LIVE; i++) {
        if (((size_t)i % ke) == 0) {
          // a survivor, still holding churn's pattern -- must be untouched by the interrupted sweep
          if (pattern_check(p[i], BSZ, (size_t)i) != BSZ) { bad = 2; }
        }
        else {
          // a slot the child just refilled: if two mallocs aliased, an earlier fill got overwritten
          const uint8_t* b = (const uint8_t*)p[i];
          if (b[0] != (uint8_t)(i & 0xFF) || b[BSZ - 1] != (uint8_t)(i & 0xFF)) { bad = 3; }
        }
      }
      _exit(bad);
    }
    mi_on_thread_idle_end();
    if (pid < 0) { all_ok = false; }
    else {
      int status = 0;
      // a corrupt free-list entry aborts the child (a signal), which is a failure too
      if (waitpid(pid, &status, 0) < 0 || !WIFEXITED(status) || WEXITSTATUS(status) != 0) { all_ok = false; }
      if (first_corrupt_survivor(p) >= 0) { all_ok = false; }   // and the parent stays intact
    }
    for (int i = 0; i < LIVE; i++) { if (p[i] != NULL) mi_free(p[i]); }
    free(p);
  }
  check("fork while parked leaves parent and child heaps consistent", all_ok);
}

// ---------------------------------------------------------------------------
// A thread that used a NON-main heap (so it has dynamic thread-locals) parks and then exits while
// a sweep is in flight. Its teardown frees the thread-locals array with mi_free: that free must not
// race the scavenger's rewrite of the same pages -- the park has to be left before any teardown
// free. Also registers a pthread key destructor that frees, as an application would.
// ---------------------------------------------------------------------------
#define DTOR_BLOCKS  (500)
static pthread_key_t dtor_key;
static void dtor_frees(void* blocks_v) {
  void** blocks = (void**)blocks_v;
  if (blocks == NULL) return;
  for (int i = 0; i < DTOR_BLOCKS; i++) { if (blocks[i] != NULL) mi_free(blocks[i]); }
  free(blocks);
}

static void* park_then_exit_with_dyn_tls(void* arg) {
  (void)arg;
  // touch a non-main heap so this thread gets dynamic thread-locals (tls->count > 0)
  mi_heap_t* h = mi_heap_new();
  if (h != NULL) {
    void* q = mi_heap_malloc(h, 64);
    if (q != NULL) mi_free(q);
    // deliberately keep `h`: mi_heap_delete at teardown is part of the exit path under test
  }
  // an app-level destructor that frees on this thread as it exits
  void** dblocks = (void**)calloc(500, sizeof(void*));
  if (dblocks != NULL) {
    for (int i = 0; i < 500; i++) { dblocks[i] = mi_malloc(96); if (dblocks[i] == NULL) break; }
    pthread_setspecific(dtor_key, dblocks);
  }
  void** p = (void**)calloc(LIVE, sizeof(void*));
  if (p != NULL) { churn(p); free(p); }   // holes to punch; the mi blocks stay live on purpose
  (void)mi_on_thread_idle_start();
  usleep(200);        // scavenger claims us and starts sweeping
  pthread_exit(NULL); // leave while parked/swept: teardown frees run against the sweep
}

static void test_exit_while_swept_with_dyn_tls(void) {
  enum { THREADS_MAX = DYN_TLS_THREADS_FULL };
  const bool bound = guard_every_alloc();
  const int THREADS = bound ? DYN_TLS_THREADS_BOUND : DYN_TLS_THREADS_FULL;
  const int ROUNDS  = bound ? DYN_TLS_ROUNDS_BOUND  : DYN_TLS_ROUNDS_FULL;
  if (pthread_key_create(&dtor_key, &dtor_frees) != 0) return;
  for (int r = 0; r < ROUNDS; r++) {
    pthread_t t[THREADS_MAX];
    for (int i = 0; i < THREADS; i++) {
      if (pthread_create(&t[i], NULL, &park_then_exit_with_dyn_tls, NULL) != 0) return;
    }
    for (int i = 0; i < THREADS; i++) { pthread_join(t[i], NULL); }
  }
  pthread_key_delete(dtor_key);
  // reaching here without a crash is the assertion; under TSAN the teardown-free race reports
  check("exit while swept, with dtor frees and dynamic thread-locals, is race-free", true);
}

// ---------------------------------------------------------------------------
// #272 regression for the parked-thread sweep race. The park contract is "the owner does not
// allocate or free between `_start` and `_end`" -- but a thread that EXITS while parked breaks
// it through no fault of the caller: pthread-key destructors (and C++ `thread_local`
// destructors) run in an unspecified order and free ON THE OWNER THREAD before mimalloc's own
// thread-done hook gets to leave the park. The scavenger is mid-sweep of exactly those theaps,
// so the owner mutates page queues under the walk and `mi_theap_visit_pages`'s `count == total`
// (or `mi_page_is_valid_init`'s block-conservation check) fires. Long queues make the walk long
// and the jittered sleep lands the exit inside it. Asserts only under MI_DEBUG_FULL.
// ---------------------------------------------------------------------------
// Spread over every small size class and keep survivors in each, so this thread's theap has a
// page in (almost) every bin: `mi_theap_visit_pages(include_full)` then walks a long list and
// the destructor's frees have a wide window to land inside it.
#define RACE_CLASSES  (48)
#define RACE_PER_CLASS (24)

static void* park_then_exit_racing_dtor(void* arg) {
  const unsigned id = (unsigned)(uintptr_t)arg;
  void** dblocks = (void**)calloc(DTOR_BLOCKS, sizeof(void*));
  if (dblocks != NULL) {
    for (int i = 0; i < DTOR_BLOCKS; i++) {
      dblocks[i] = mi_malloc(16 + (size_t)(i % RACE_CLASSES) * 40);   // hits many bins on free
      if (dblocks[i] == NULL) break;
    }
    pthread_setspecific(dtor_key, dblocks);
  }
  // populate every bin and keep one survivor per class so the page stays in its queue
  for (int c = 0; c < RACE_CLASSES; c++) {
    const size_t sz = 16 + (size_t)c * 40;
    void* keep = NULL;
    for (int k = 0; k < RACE_PER_CLASS; k++) {
      void* q = mi_malloc(sz);
      if (q == NULL) break;
      if (k == 0) { keep = q; } else { mi_free(q); }
    }
    (void)keep;   // deliberately leaked into this thread's teardown
  }
  void** p = (void**)calloc(LIVE, sizeof(void*));
  if (p != NULL) { churn_pattern(p); free(p); }   // more holes, more pages, a longer sweep
  (void)mi_on_thread_idle_start();
  usleep(20 + (id * 13) % 250);   // land the exit INSIDE the scavenger's walk
  pthread_exit(NULL);
}

static void test_exit_while_swept_stress(void) {
  enum { THREADS_MAX = SWEPT_STRESS_THREADS_FULL };
  const bool bound = guard_every_alloc();
  const int THREADS = bound ? SWEPT_STRESS_THREADS_BOUND : SWEPT_STRESS_THREADS_FULL;
  const int ROUNDS  = bound ? SWEPT_STRESS_ROUNDS_BOUND  : SWEPT_STRESS_ROUNDS_FULL;
  if (pthread_key_create(&dtor_key, &dtor_frees) != 0) return;
  for (int r = 0; r < ROUNDS; r++) {
    pthread_t t[THREADS_MAX];
    int made = 0;
    for (int i = 0; i < THREADS; i++) {
      if (pthread_create(&t[i], NULL, &park_then_exit_racing_dtor,
                         (void*)(uintptr_t)(unsigned)(r * THREADS + i)) != 0) break;
      made++;
    }
    for (int i = 0; i < made; i++) { pthread_join(t[i], NULL); }
  }
  pthread_key_delete(dtor_key);
  check("exit while swept, racing an app destructor's frees, is race-free", true);
}

// ---------------------------------------------------------------------------
// Stopping the scavenger joins the thread: a park after it has nobody to hand off to and reports
// false, and the process stays fully usable. Runs last, since it takes the scavenger away.
// ---------------------------------------------------------------------------
static void test_scavenger_stop(void) {
  mi_scavenger_stop();
  check("no handoff once the scavenger is stopped", !mi_on_thread_idle_start());
  mi_scavenger_stop();   // a second stop is a no-op
  void* q = mi_malloc(64);
  check("the thread still allocates after the stop", q != NULL);
  mi_free(q);
  mi_on_thread_idle();   // and can still sweep for itself
}

int main(void) {
  test_lazy_start_and_signals();   // first: nothing may have started the scavenger yet
  test_handoff_sweeps();
  test_survivors_intact();
  test_unbalanced();
  test_third_thread_frees_during_sweep();
  test_parks_get_swept();
  test_park_inside_window_gets_swept();
  test_fork_while_parked();
  test_park_then_exit();
  test_exit_while_swept_with_dyn_tls();
  test_exit_while_swept_stress();
  test_park_stress();
  test_scavenger_stop();
  fprintf(stderr, "\n%s\n", failures == 0 ? "all tests passed." : "SOME TESTS FAILED.");
  return failures == 0 ? 0 : 1;
}

#endif  // !_WIN32
