/* #270 (Bun parity P5): POSIX repro for the fork()-vs-allocator-lock hazard that
   `pthread_atfork` fork-safety handlers (src/init.c registration, src/subproc.c
   handlers) exist to close: fork() from a multithreaded process only clones the
   calling thread; every other thread simply vanishes in the child, taking whatever
   mimalloc-internal lock it happened to hold with it. The child then inherits that
   lock permanently locked, and its first allocation that touches it hangs forever.

   This is the "repro first" test #270's Step 1 asked for: run it on a tree WITHOUT
   the fork handlers (revert src/subproc.c's `_mi_process_fork_prepare/parent/child`
   and src/init.c's `pthread_atfork` registration -- or check out this repo before
   this PR) to see it hang/fail some fraction of the time; run it WITH them (this
   tree) to see it pass cleanly, `N_FORKS` forks x N runs, 0 failures, 0 hangs.

   Thread A continuously creates and destroys heaps (`mi_heap_new`/`mi_heap_delete`,
   contends `heaps_lock`) and does large allocations (contends `arena_reserve_lock`)
   for the entire test. The main thread forks `N_FORKS` times; each child does one
   `mi_malloc(64)`/`mi_free`/`_exit(0)` and nothing else -- if any lock its allocation
   touches was left locked by a thread that did not survive the fork, that call hangs.
   The parent enforces a per-child watchdog (`alarm()` + `waitpid()`) so a hang fails
   the test instead of wedging CI.

   A final extra fork (`check_dump_in_child`) exercises #270 step 5's requirement that
   `mi_prof_dump`/`mi_dhat_dump` keep working in the child under the profiler/DHAT
   "continue" child-side policy (see profile.c's/dhat.c's `_mi_prof_fork_child`/
   `_mi_dhat_fork_child` comments): the call must return (not hang), whether or not
   the corresponding subsystem is enabled/active.

   The churn-thread repro above is PROBABILISTIC: on a fast, lightly loaded machine
   its real critical sections are microseconds, so a fork() landing inside one is rare
   even across hundreds of iterations (measured 0/200 on both the pre- and post-fix
   tree here -- see the #270 PR discussion). `deterministic_hold_repro` below is the
   DETERMINISTIC complement: it uses MI_DEBUG-only test hooks (`_mi_test_hold_heaps_lock`
   etc, src/subproc.c) to GUARANTEE `heaps_lock` is held by a thread that then vanishes
   across the fork, giving this test real discriminating power regardless of timing.
   Only available in MI_DEBUG>0 builds (the hooks do not exist otherwise); skipped with
   a message in a Release build, where the probabilistic repro above still runs. */

#ifdef _WIN32
#include <stdio.h>
int main(void) { fprintf(stderr, "test-fork-locks: skipped on Windows (POSIX-only, #270)\n"); return 0; }
#else

#include <mimalloc.h>
#include <mimalloc/dhat.h>
#include <pthread.h>
#if (MI_DEBUG>0)
#include <mimalloc/internal.h>  // _mi_test_hold_heaps_lock etc (deterministic repro)
#endif
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdbool.h>
#include <unistd.h>
#include <signal.h>
#include <errno.h>
#include <sys/wait.h>

#define N_FORKS             200
#define CHILD_TIMEOUT_SECS  5

static volatile int stop_flag = 0;

// Thread A: contends `heaps_lock` (mi_heap_new/mi_heap_delete) and
// `arena_reserve_lock` (a large allocation, likely to reserve/grow an arena) for as
// long as the test runs.
static void* churn_thread(void* arg) {
  (void)arg;
  while (!stop_flag) {
    mi_heap_t* h = mi_heap_new();
    if (h != NULL) {
      void* p1 = mi_heap_malloc(h, 64);
      void* p2 = mi_heap_malloc(h, 8 * 1024 * 1024); // large: exercises arena_reserve_lock
      if (p1 != NULL) mi_free(p1);
      if (p2 != NULL) mi_free(p2);
      mi_heap_delete(h);
    }
  }
  return NULL;
}

static volatile sig_atomic_t alarm_fired = 0;
static void on_alarm(int sig) { (void)sig; alarm_fired = 1; }

// Wait for `pid` with a `CHILD_TIMEOUT_SECS` watchdog. Returns 0 on a clean exit(0),
// 1 on a hang (kills the child), 2 on any other failure (waitpid error, non-zero
// exit, or a signal).
static int wait_child_with_timeout(pid_t pid, const char* what) {
  alarm_fired = 0;
  alarm(CHILD_TIMEOUT_SECS);
  int status = 0;
  const pid_t w = waitpid(pid, &status, 0);
  alarm(0);
  if (w != pid) {
    if (alarm_fired) {
      fprintf(stderr, "HANG: %s (pid %d) did not exit within %ds\n", what, (int)pid, CHILD_TIMEOUT_SECS);
      kill(pid, SIGKILL);
      waitpid(pid, &status, 0);
      return 1;
    }
    fprintf(stderr, "FAIL: waitpid failed for %s: %s\n", what, strerror(errno));
    return 2;
  }
  if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
    fprintf(stderr, "FAIL: %s exited abnormally (status=0x%x)\n", what, status);
    return 2;
  }
  return 0;
}

// #270 step 5: mi_prof_dump/mi_dhat_dump must keep working in the child. Not asserting
// their boolean return (both legitimately return false when their subsystem is
// off/inactive, e.g. an MI_PPROF=0 build) -- the thing under test is that the call
// returns at all, rather than hanging on prof_lock/dhat_lock left stuck by a thread
// that vanished across the fork.
static int check_dump_in_child(void) {
  const bool started_prof = mi_prof_start(0);   // no-op / returns false when MI_PPROF=0; harmless either way
  const bool started_dhat = mi_dhat_start();
  (void)started_prof; (void)started_dhat;
  for (int i = 0; i < 64; i++) { void* p = mi_malloc(256 + (size_t)i); mi_free(p); }

  const pid_t pid = fork();
  if (pid < 0) {
    fprintf(stderr, "FAIL: fork() failed before dump check: %s\n", strerror(errno));
    return 1;
  }
  if (pid == 0) {
    char path[96];
    snprintf(path, sizeof(path), "/tmp/test-fork-locks-dump-%d.txt", (int)getpid());
    const bool prof_ok = mi_prof_dump(path);
    const bool dhat_ok = mi_dhat_dump(path);
    (void)prof_ok; (void)dhat_ok;
    unlink(path);
    _exit(0);
  }
  return (wait_child_with_timeout(pid, "dump-check child") != 0) ? 1 : 0;
}

#if (MI_DEBUG>0)
// Deterministic complement to the probabilistic churn-thread repro above -- see the
// file header comment.
//
// IMPORTANT design note: a WORKING `_mi_process_fork_prepare` (this tree, with the
// #270 handlers) does not let a held lock "vanish" across fork() at all -- prepare()
// runs in the still-multi-threaded parent, BEFORE the real fork() syscall, and simply
// BLOCKS until every documented lock is free, including one a live holder thread is
// deliberately holding. A first version of this test tried to fork() while a holder
// thread held `heaps_lock` and only released it from the PARENT after fork() returned
// -- that is a self-deadlock by construction (the parent thread calling fork() blocks
// forever inside `_mi_process_fork_prepare` waiting for the very release that can only
// happen after fork() returns), not a #270 regression. What this test actually proves,
// deterministically, is the other half of the same correctness property: that
// `_mi_process_fork_prepare` truly ACQUIRES `heaps_lock` (i.e. really blocks against a
// live, guaranteed holder) rather than racing past it -- if a future change turned
// that acquire into a no-op, this test would fork() while the holder still logically
// "owns" the lock and the child's `mi_malloc` would very likely observe inconsistent
// heap-list state. The holder here always releases normally (via a timed releaser
// thread, independent of the forking thread) -- this test is about proving the
// acquire is real, not about simulating a thread that never releases at all (that
// scenario -- an orphaned lock with no live holder -- hangs identically with or
// without fork() involved, since it is not fork-specific).
#define DET_ITERS 20
#define DET_RELEASE_DELAY_USEC (20 * 1000)  // 20ms: long enough that a real acquire would block on it

static void* det_holder_thread(void* arg) {
  (void)arg;
  _mi_test_hold_heaps_lock();  // blocks here, holding heaps_lock, until released below
  return NULL;
}

static void* det_releaser_thread(void* arg) {
  (void)arg;
  usleep(DET_RELEASE_DELAY_USEC);
  _mi_test_release_heaps_lock();
  return NULL;
}

static int deterministic_hold_repro(void) {
  int failures = 0;
  int hangs = 0;

  for (int i = 0; i < DET_ITERS; i++) {
    pthread_t holder, releaser;
    if (pthread_create(&holder, NULL, det_holder_thread, NULL) != 0) {
      fprintf(stderr, "FAIL: deterministic_hold_repro: could not start holder thread (iter %d)\n", i);
      failures++;
      continue;
    }
    // Wait for the holder to actually acquire heaps_lock (not just have been
    // scheduled) before starting the releaser / forking.
    while (!_mi_test_heaps_lock_is_held()) { /* spin */ }
    if (pthread_create(&releaser, NULL, det_releaser_thread, NULL) != 0) {
      fprintf(stderr, "FAIL: deterministic_hold_repro: could not start releaser thread (iter %d)\n", i);
      failures++;
      _mi_test_release_heaps_lock();
      pthread_join(holder, NULL);
      continue;
    }

    // fork() blocks inside _mi_process_fork_prepare's acquire of heaps_lock until the
    // releaser thread (running concurrently, independent of this thread) releases it.
    const pid_t pid = fork();
    if (pid < 0) {
      fprintf(stderr, "FAIL: deterministic_hold_repro: fork() failed at iter %d: %s\n", i, strerror(errno));
      failures++;
      pthread_join(releaser, NULL);
      pthread_join(holder, NULL);
      continue;
    }
    if (pid == 0) {
      // Child: single-threaded now, past a fork() that had to genuinely wait out a
      // real lock holder. If prepare's acquire were a no-op, this mi_malloc would be
      // touching heap-list state a "concurrent" mutator (the vanished holder thread,
      // from the child's perspective) could have left inconsistent.
      void* p = mi_malloc(64);
      if (p == NULL) { _exit(2); }
      mi_free(p);
      _exit(0);
    }
    // Parent: both helper threads have done their job (the releaser already ran, the
    // holder already returned once released) by the time fork() returns here, since
    // _mi_process_fork_prepare's acquire only unblocks after the real release.
    pthread_join(releaser, NULL);
    pthread_join(holder, NULL);
    char what[48];
    snprintf(what, sizeof(what), "deterministic-hold child %d", i);
    const int rc = wait_child_with_timeout(pid, what);
    if (rc == 1) { hangs++; }
    else if (rc == 2) { failures++; }
  }

  fprintf(stderr, "deterministic_hold_repro: %d/%d failed, %d/%d hung\n", failures, DET_ITERS, hangs, DET_ITERS);
  return (failures > 0 || hangs > 0) ? 1 : 0;
}
#endif // MI_DEBUG>0

int main(void) {
  struct sigaction sa;
  memset(&sa, 0, sizeof(sa));
  sa.sa_handler = on_alarm;
  sigaction(SIGALRM, &sa, NULL);

  int det_rc = 0;
  #if (MI_DEBUG>0)
  det_rc = deterministic_hold_repro();
  #else
  fprintf(stderr, "deterministic_hold_repro: skipped (needs MI_DEBUG>0 -- this is a Release build)\n");
  #endif

  pthread_t th;
  if (pthread_create(&th, NULL, churn_thread, NULL) != 0) {
    fprintf(stderr, "FAIL: could not start churn thread\n");
    return 1;
  }

  int failures = 0;
  int hangs = 0;

  for (int i = 0; i < N_FORKS; i++) {
    const pid_t pid = fork();
    if (pid < 0) {
      fprintf(stderr, "FAIL: fork() failed at iteration %d: %s\n", i, strerror(errno));
      failures++;
      continue;
    }
    if (pid == 0) {
      // child: single-threaded now. If any lock this touches was left stuck by the
      // churn thread (which did not survive the fork), this hangs.
      void* p = mi_malloc(64);
      if (p == NULL) { _exit(2); }
      mi_free(p);
      _exit(0);
    }
    char what[32];
    snprintf(what, sizeof(what), "child %d", i);
    const int rc = wait_child_with_timeout(pid, what);
    if (rc == 1) { hangs++; }
    else if (rc == 2) { failures++; }
  }

  stop_flag = 1;
  pthread_join(th, NULL);

  const int dump_rc = check_dump_in_child();

  fprintf(stderr, "test-fork-locks: %d/%d forks failed, %d/%d hung, dump-in-child %s, deterministic-hold %s\n",
          failures, N_FORKS, hangs, N_FORKS, dump_rc == 0 ? "ok" : "FAILED", det_rc == 0 ? "ok" : "FAILED");

  if (failures > 0 || hangs > 0 || dump_rc != 0 || det_rc != 0) {
    fprintf(stderr, "FAIL: test-fork-locks saw %d failures and %d hangs across %d forks (dump-in-child %s, deterministic-hold %s)\n",
            failures, hangs, N_FORKS, dump_rc == 0 ? "ok" : "failed", det_rc == 0 ? "ok" : "failed");
    return 1;
  }
  printf("ok: test-fork-locks: %d forks, 0 failures, 0 hangs, dump-in-child ok, deterministic-hold ok\n", N_FORKS);
  return 0;
}

#endif // !_WIN32
