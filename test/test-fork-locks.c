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
   the corresponding subsystem is enabled/active. */

#ifdef _WIN32
#include <stdio.h>
int main(void) { fprintf(stderr, "test-fork-locks: skipped on Windows (POSIX-only, #270)\n"); return 0; }
#else

#include <mimalloc.h>
#include <mimalloc/dhat.h>
#include <pthread.h>
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

int main(void) {
  pthread_t th;
  if (pthread_create(&th, NULL, churn_thread, NULL) != 0) {
    fprintf(stderr, "FAIL: could not start churn thread\n");
    return 1;
  }

  struct sigaction sa;
  memset(&sa, 0, sizeof(sa));
  sa.sa_handler = on_alarm;
  sigaction(SIGALRM, &sa, NULL);

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

  fprintf(stderr, "test-fork-locks: %d/%d forks failed, %d/%d hung, dump-in-child %s\n",
          failures, N_FORKS, hangs, N_FORKS, dump_rc == 0 ? "ok" : "FAILED");

  if (failures > 0 || hangs > 0 || dump_rc != 0) {
    fprintf(stderr, "FAIL: test-fork-locks saw %d failures and %d hangs across %d forks (dump-in-child %s)\n",
            failures, hangs, N_FORKS, dump_rc == 0 ? "ok" : "failed");
    return 1;
  }
  printf("ok: test-fork-locks: %d forks, 0 failures, 0 hangs, dump-in-child ok\n", N_FORKS);
  return 0;
}

#endif // !_WIN32
