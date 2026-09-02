/* #270 (Bun parity P5): POSIX repro for the fork()-vs-allocator-lock hazard that
   `pthread_atfork` fork-safety handlers (src/init.c registration, src/fork.c handlers)
   exist to close: fork() from a multithreaded process only clones the calling thread;
   every other thread simply vanishes in the child, taking whatever mimalloc-internal
   lock it happened to hold with it. The child then inherits that lock permanently
   locked, and its first allocation that touches it hangs forever.

   This is the "repro first" test #270's Step 1 asked for: run it on a tree WITHOUT the
   fork handlers (revert src/fork.c's `_mi_process_fork_prepare/parent/child` and
   src/init.c's `pthread_atfork` registration -- or check out this repo before this PR)
   to see it hang/fail some fraction of the time; run it WITH them (this tree) to see it
   pass cleanly, `N_FORKS` forks x N runs, 0 failures, 0 hangs.

   Three complementary workloads run against the same 200-fork loop:

   1. `churn_thread` (default): continuously creates and destroys heaps
      (`mi_heap_new`/`mi_heap_delete`, contends `heaps_lock`) and does large allocations
      (contends `arena_reserve_lock`). This is the PROBABILISTIC repro, and on a fast,
      lightly loaded machine it has near-zero discriminating power: its real critical
      sections are microseconds, so a fork() landing inside one is rare even across
      hundreds of iterations (measured 0/200 on both the pre- and post-fix tree here --
      see the #270 PR discussion). It stays because it costs nothing and a slower or
      more loaded machine does hit it.

   2. `spawn_thread` (opt-in, `MI_TEST_FORK_SPAWN=1`): keeps up to `SPAWN_MAX_LIVE`
      threads alive at once, starting new ones continuously. Thread start is the
      interesting path for the lock ORDER specifically: `_mi_thread_init_with_heap`
      allocates the new thread's own `mi_tld_t`/`mi_theap_t` through `_mi_meta_zalloc`,
      which holds `subproc->theap_meta_lock` across a full allocation on `heap_main` --
      so it nests `theap_meta_lock` OUTSIDE `heap_main->arena_pages_lock`,
      `arena_reserve_lock` and the page-map lock, the inversion the first two revisions
      of this PR had. Threads are held live (rather than joined immediately) so the meta
      theap has to keep allocating instead of recycling one freed slot. Combine with
      `MIMALLOC_PROF=1` for the CI variant.

      HONEST SCOPE: this is a TIMING-based workload, not a proof. The meta theap only
      reaches `mi_heap_ensure_arena_pages` / `mi_arena_reserve` / page-map growth on a
      cold page, so those nestings are rare; running this variant against the OLD
      (inverted) order did not hang here in 200 forks. The deterministic evidence for
      the order is the MI_DEBUG>2 observed-edge checker in src/fork.c, which reports an
      inversion the moment the process performs one -- see its comment there. This
      variant is the cheap, always-on complement.

   3. `deterministic_hold_repro` (MI_DEBUG>0): the DETERMINISTIC complement, using the
      test hooks in src/fork.c. A holder thread takes the main subprocess's `heaps_lock`
      and, while holding it, POISONS the list that lock guards (detaches `sp->heaps`,
      restoring it before release). A correct prepare() blocks on `heaps_lock` and can
      only fork after the restore, so the child never sees the poison; a prepare() that
      skipped that acquire forks straight through the poisoned window and the child
      observes `sp->heaps == NULL`, which `_mi_test_heaps_lock_poison_observed()`
      reports and this test fails on (exit code 3 from the child). Verified by
      deliberately no-op'ing prepare's `heaps_lock` acquire: 20/20 children observe the
      poison; with the acquire restored, 0/20. Skipped with a message in a Release
      build, where the hooks do not exist.

   A final extra fork (`check_dump_in_child`) exercises #270 step 5's requirement that
   `mi_prof_dump`/`mi_dhat_dump` keep working in the child under the profiler/DHAT
   "continue" child-side policy (see profile.c's/dhat.c's `_mi_prof_fork_child`/
   `_mi_dhat_fork_child` comments). */

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

// Exit code a child uses to report that it caught the deterministic poison window; see
// `deterministic_hold_repro`.
#define CHILD_EXIT_POISONED 3

static volatile int stop_flag = 0;

// Workload 1: contends `heaps_lock` (mi_heap_new/mi_heap_delete) and
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

// Workload 2 (opt-in): each spawned thread's own tld/theap is allocated through
// `_mi_meta_zalloc`, i.e. under `theap_meta_lock` and across a full allocation on
// `heap_main` -- the nesting the documented lock order has to get right. The large
// allocation on top makes that inner allocation actually reach the arena/page-map
// growth paths rather than a warm free list.
#define SPAWN_MAX_LIVE 64
static volatile int spawn_live = 0;
static pthread_mutex_t spawn_mutex = PTHREAD_MUTEX_INITIALIZER;

static void* spawned_worker(void* arg) {
  (void)arg;
  void* p1 = mi_malloc(64);
  void* p2 = mi_malloc(8 * 1024 * 1024);   // large: pushes the arenas to grow
  if (p1 != NULL) mi_free(p1);
  if (p2 != NULL) mi_free(p2);
  usleep(2000);   // stay alive a moment: many concurrent live tld/theaps mean the meta
                  // theap has to allocate FRESH pages instead of recycling one slot
  pthread_mutex_lock(&spawn_mutex);
  spawn_live--;
  pthread_mutex_unlock(&spawn_mutex);
  return NULL;
}

static void* spawn_thread(void* arg) {
  (void)arg;
  while (!stop_flag) {
    pthread_mutex_lock(&spawn_mutex);
    const int live = spawn_live;
    pthread_mutex_unlock(&spawn_mutex);
    if (live >= SPAWN_MAX_LIVE) { usleep(200); continue; }
    pthread_attr_t attr;
    pthread_attr_init(&attr);
    pthread_attr_setdetachstate(&attr, PTHREAD_CREATE_DETACHED);
    pthread_attr_setstacksize(&attr, 128 * 1024);
    pthread_t w;
    pthread_mutex_lock(&spawn_mutex);
    spawn_live++;
    pthread_mutex_unlock(&spawn_mutex);
    if (pthread_create(&w, &attr, spawned_worker, NULL) != 0) {
      pthread_mutex_lock(&spawn_mutex);
      spawn_live--;
      pthread_mutex_unlock(&spawn_mutex);
      usleep(1000);
    }
    pthread_attr_destroy(&attr);
  }
  // let the detached workers drain before the test tears down
  for (int i = 0; i < 1000; i++) {
    pthread_mutex_lock(&spawn_mutex);
    const int live = spawn_live;
    pthread_mutex_unlock(&spawn_mutex);
    if (live == 0) break;
    usleep(2000);
  }
  return NULL;
}

static volatile sig_atomic_t alarm_fired = 0;
static void on_alarm(int sig) { (void)sig; alarm_fired = 1; }

// Wait for `pid` with a `CHILD_TIMEOUT_SECS` watchdog. Returns 0 on a clean exit(0),
// 1 on a hang (kills the child), 2 on any other failure (waitpid error, non-zero
// exit, or a signal). `pexit_code` (optional) receives the child's exit status when it
// exited normally, or -1 otherwise.
static int wait_child_with_timeout(pid_t pid, const char* what, int* pexit_code) {
  if (pexit_code != NULL) { *pexit_code = -1; }
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
  if (WIFEXITED(status) && pexit_code != NULL) { *pexit_code = WEXITSTATUS(status); }
  if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
    fprintf(stderr, "FAIL: %s exited abnormally (status=0x%x)\n", what, status);
    return 2;
  }
  return 0;
}

// #270 step 5: mi_prof_dump/mi_dhat_dump must keep working in the child. The thing under
// test is that the call returns at all, rather than hanging on prof_lock/dhat_lock left
// stuck by a thread that vanished across the fork -- but where the subsystem was
// actually started in the parent, the dump must also SUCCEED in the child (that is what
// the "continue" child-side policy means: the records are ordinary process memory that
// survives fork by copy-on-write, so only the lock needed resetting). Where a subsystem
// is off (e.g. an MI_PPROF=0 build, so `mi_prof_start` returned false), `false` is the
// correct answer and is not asserted.
static int check_dump_in_child(void) {
  const bool started_prof = mi_prof_start(0);   // no-op / returns false when MI_PPROF=0; harmless either way
  const bool started_dhat = mi_dhat_start();
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
    unlink(path);
    if (started_prof && !prof_ok) {
      fprintf(stderr, "FAIL: mi_prof_dump failed in the child although profiling was started in the parent\n");
      _exit(4);
    }
    if (started_dhat && !dhat_ok) {
      fprintf(stderr, "FAIL: mi_dhat_dump failed in the child although DHAT was started in the parent\n");
      _exit(5);
    }
    _exit(0);
  }
  return (wait_child_with_timeout(pid, "dump-check child", NULL) != 0) ? 1 : 0;
}

#if (MI_DEBUG>0)
// Workload 3 -- see the file header comment. The holder always releases normally (via a
// timed releaser thread, independent of the forking thread): the scenario being proven
// is that prepare's acquire is REAL, not the (non-fork-specific) scenario of an orphaned
// lock with no live holder at all.
#define DET_ITERS 20
#define DET_RELEASE_DELAY_USEC (20 * 1000)  // 20ms: long enough that a real acquire would block on it

static void* det_holder_thread(void* arg) {
  (void)arg;
  _mi_test_hold_heaps_lock();  // blocks here, holding heaps_lock with sp->heaps detached, until released below
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
  int poisoned = 0;

  for (int i = 0; i < DET_ITERS; i++) {
    pthread_t holder, releaser;
    if (pthread_create(&holder, NULL, det_holder_thread, NULL) != 0) {
      fprintf(stderr, "FAIL: deterministic_hold_repro: could not start holder thread (iter %d)\n", i);
      failures++;
      continue;
    }
    // Wait for the holder to actually acquire heaps_lock and poison the list it guards
    // (not just have been scheduled) before starting the releaser / forking.
    while (!_mi_test_heaps_lock_is_held()) { /* spin */ }
    if (pthread_create(&releaser, NULL, det_releaser_thread, NULL) != 0) {
      fprintf(stderr, "FAIL: deterministic_hold_repro: could not start releaser thread (iter %d)\n", i);
      failures++;
      _mi_test_release_heaps_lock();
      pthread_join(holder, NULL);
      continue;
    }

    // fork() blocks inside _mi_process_fork_prepare's acquire of heaps_lock until the
    // releaser thread (running concurrently, independent of this thread) releases it --
    // by which time the holder has already restored sp->heaps.
    const pid_t pid = fork();
    if (pid < 0) {
      fprintf(stderr, "FAIL: deterministic_hold_repro: fork() failed at iter %d: %s\n", i, strerror(errno));
      failures++;
      pthread_join(releaser, NULL);
      pthread_join(holder, NULL);
      continue;
    }
    if (pid == 0) {
      // Child: single-threaded now. If prepare really acquired `heaps_lock`, this fork
      // cannot have happened inside the holder's poisoned window.
      if (_mi_test_heaps_lock_poison_observed()) { _exit(CHILD_EXIT_POISONED); }
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
    int exit_code = -1;
    const int rc = wait_child_with_timeout(pid, what, &exit_code);
    if (rc == 1) { hangs++; }
    else if (rc == 2) {
      failures++;
      if (exit_code == CHILD_EXIT_POISONED) {
        poisoned++;
        fprintf(stderr, "FAIL: child %d forked inside the poisoned heaps_lock window -- "
                        "_mi_process_fork_prepare did not acquire subproc->heaps_lock\n", i);
      }
    }
  }

  fprintf(stderr, "deterministic_hold_repro: %d/%d failed (%d saw the poisoned window), %d/%d hung\n",
          failures, DET_ITERS, poisoned, hangs, DET_ITERS);
  return (failures > 0 || hangs > 0) ? 1 : 0;
}
#endif // MI_DEBUG>0

int main(void) {
  struct sigaction sa;
  memset(&sa, 0, sizeof(sa));
  sa.sa_handler = on_alarm;
  sigaction(SIGALRM, &sa, NULL);

  const char* spawn_env = getenv("MI_TEST_FORK_SPAWN");
  const bool spawn_mode = (spawn_env != NULL && spawn_env[0] == '1');

  int det_rc = 0;
  #if (MI_DEBUG>0)
  det_rc = deterministic_hold_repro();
  #else
  fprintf(stderr, "deterministic_hold_repro: skipped (needs MI_DEBUG>0 -- this is a Release build)\n");
  #endif

  // Spawn mode replaces the heap-churn thread rather than running alongside it: heap
  // create/destroy churn CONCURRENT with sustained thread starts trips a pre-existing,
  // fork-unrelated assertion (`mi_theap_is_valid`, theap.c:71) that reproduces with zero
  // fork() involved (3/5 runs of an equivalent standalone churn+spawn program on this
  // tree, and on the pre-#270 tree) -- see the #270 PR discussion. Running the two
  // workloads in separate ctest variants keeps this test's signal about fork safety.
  pthread_t th;
  void* (*workload)(void*) = (spawn_mode ? spawn_thread : churn_thread);
  if (pthread_create(&th, NULL, workload, NULL) != 0) {
    fprintf(stderr, "FAIL: could not start %s thread\n", spawn_mode ? "spawner" : "churn");
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
      // child: single-threaded now. If any lock this touches was left stuck by a thread
      // that did not survive the fork, this hangs.
      void* p = mi_malloc(64);
      if (p == NULL) { _exit(2); }
      mi_free(p);
      _exit(0);
    }
    char what[32];
    snprintf(what, sizeof(what), "child %d", i);
    const int rc = wait_child_with_timeout(pid, what, NULL);
    if (rc == 1) { hangs++; }
    else if (rc == 2) { failures++; }
  }

  stop_flag = 1;
  pthread_join(th, NULL);

  const int dump_rc = check_dump_in_child();

  fprintf(stderr, "test-fork-locks: %d/%d forks failed, %d/%d hung, spawn-mode %s, dump-in-child %s, deterministic-hold %s\n",
          failures, N_FORKS, hangs, N_FORKS, spawn_mode ? "on" : "off",
          dump_rc == 0 ? "ok" : "FAILED", det_rc == 0 ? "ok" : "FAILED");

  if (failures > 0 || hangs > 0 || dump_rc != 0 || det_rc != 0) {
    fprintf(stderr, "FAIL: test-fork-locks saw %d failures and %d hangs across %d forks (dump-in-child %s, deterministic-hold %s)\n",
            failures, hangs, N_FORKS, dump_rc == 0 ? "ok" : "failed", det_rc == 0 ? "ok" : "failed");
    return 1;
  }
  printf("ok: test-fork-locks: %d forks, 0 failures, 0 hangs, spawn-mode %s, dump-in-child ok, deterministic-hold ok\n",
         N_FORKS, spawn_mode ? "on" : "off");
  return 0;
}

#endif // !_WIN32
