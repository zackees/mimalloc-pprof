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
   `_mi_dhat_fork_child` comments).

   4. `purge_all_fork_cases` (#366 row F1, docs/purge-all-implementation.md §8/§11): the
      `mi_purge_all` driver and the owner gate across fork(). Four forks: with a live
      registered sibling thread, a forced purge in the child before any new thread exists
      (the sibling's inherited tld is an ORPHAN: `theaps_orphaned == 1`, `theaps_pending == 0`,
      never swept, never waited for) and again after the child has started a worker of its
      own (that worker is swept in a gated build, reported pending in a default one); a fork
      landing DURING a purge (the sibling is blocked inside its deferred-free handler while it
      holds the purge admission -- the child's reset must clear it, or the child is BUSY
      forever); and a fork from INSIDE an allocator hook on the forking thread (the
      deferred-free handler of a `mi_collect`), which in a gated build is a fork with the
      forking thread's `gate_depth` live -- the child must still purge, and the parent's
      depth must balance so it can keep allocating and purging afterwards. Runs in both
      builds; the MI_DEBUG>2 observed-edge checker in src/fork.c covers the walk's lock
      edges while these run. */

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

// ---------------------------------------------------------------------------------------------
// Workload 4 (#366, row F1): `mi_purge_all` across fork() -- see the file header comment.
// ---------------------------------------------------------------------------------------------

#if MI_OWNER_GATE
#define F1_GATED 1
#else
#define F1_GATED 0
#endif

enum { F1_MODE_NONE = 0, F1_MODE_BLOCK = 1, F1_MODE_FORK = 2 };

static volatile int       f1_mode = F1_MODE_NONE;   // what the deferred-free handler does...
static pthread_t          f1_mode_thread;           // ...when it runs on THIS thread
static pthread_mutex_t    f1_mutex = PTHREAD_MUTEX_INITIALIZER;
static volatile int       f1_in_handler = 0;
static volatile pid_t     f1_forked_pid = -1;       // F1_MODE_FORK: the child's pid, as seen by the parent
static volatile int       f1_sibling_ready = 0;
static volatile int       f1_sibling_stop = 0;
static volatile int       f1_sibling_purge_rc = -1;

static void f1_print_report(const char* label, int rc, const mi_purge_all_report_t* r) {
  fprintf(stderr, "  F1 %s: rc=%d swept=%zu pending=%zu orphaned=%zu hole_bytes=%zu arena_bytes=%zu gated=%d complete=%d\n",
          label, rc, r->theaps_swept, r->theaps_pending, r->theaps_orphaned, r->hole_bytes, r->arena_bytes,
          (int)r->gated, (int)r->complete);
}

// child side of the "fork inside an allocator hook" case: a purge from inside the handler
// (the child is still inside `mi_collect`'s deferred-free callback, with the gate held in
// a gated build) must run, not be BUSY, and see no orphan (no sibling was alive)
static void f1_child_purge_in_hook(void) {
  mi_purge_all_report_t r;
  const int rc = mi_purge_all_ex(MI_PURGE_FORCE, 100, &r);
  f1_print_report("child (forked inside the deferred-free hook)", rc, &r);
  if (rc == MI_PURGE_BUSY) { _exit(21); }
  if (r.theaps_orphaned != 0 || r.theaps_pending != 0) { _exit(22); }
  void* p = mi_malloc(200);   // and the child can still allocate from inside the hook
  if (p == NULL) { _exit(23); }
  mi_free(p);
  _exit(0);
}

static void f1_deferred_free(bool force, unsigned long long heartbeat, void* arg) {
  (void)force; (void)heartbeat; (void)arg;
  if (f1_mode == F1_MODE_NONE || !pthread_equal(pthread_self(), f1_mode_thread)) return;
  const int mode = f1_mode;
  f1_mode = F1_MODE_NONE;
  if (mode == F1_MODE_BLOCK) {
    f1_in_handler = 1;
    pthread_mutex_lock(&f1_mutex);    // main holds it: this thread is stuck inside the allocator
    pthread_mutex_unlock(&f1_mutex);
  }
  else if (mode == F1_MODE_FORK) {
    const pid_t pid = fork();
    if (pid == 0) { f1_child_purge_in_hook(); }
    f1_forked_pid = pid;
  }
}

// a registered sibling: allocates (so it has a tld with pages) and stays alive until told to stop
static void* f1_sibling(void* arg) {
  (void)arg;
  void* keep[64];
  for (int i = 0; i < 64; i++) { keep[i] = mi_malloc(96 + (size_t)i); }
  f1_sibling_ready = 1;
  while (!f1_sibling_stop) { usleep(500); }
  for (int i = 0; i < 64; i++) { mi_free(keep[i]); }
  return NULL;
}

// a sibling that is INSIDE a purge when main forks: its own phase-C collect runs the handler,
// which blocks on `f1_mutex` while the purge admission is held
static void* f1_sibling_purging(void* arg) {
  (void)arg;
  void* q = mi_malloc(64); mi_free(q);
  f1_mode_thread = pthread_self();
  f1_mode = F1_MODE_BLOCK;
  mi_purge_all_report_t r;
  f1_sibling_purge_rc = mi_purge_all_ex(MI_PURGE_FORCE, 5000, &r);
  f1_print_report("sibling's purge (the one the fork landed in)", f1_sibling_purge_rc, &r);
  return NULL;
}

// the child's own worker (case B): allocates, then stays out of the allocator until stopped
static volatile int f1_child_worker_ready = 0;
static volatile int f1_child_worker_stop = 0;
static void* f1_child_worker(void* arg) {
  (void)arg;
  void* keep[32];
  for (int i = 0; i < 32; i++) { keep[i] = mi_malloc(80); }
  f1_child_worker_ready = 1;
  while (!f1_child_worker_stop) { usleep(500); }
  for (int i = 0; i < 32; i++) { mi_free(keep[i]); }
  return NULL;
}

// Child of the "live registered sibling" fork: case A then case B, exit codes name the failure.
static void f1_child_with_orphan(void) {
  mi_purge_all_report_t r;
  // A. before any new thread: the sibling's tld is an orphan -- counted, never touched
  int rc = mi_purge_all_ex(MI_PURGE_FORCE, 100, &r);
  f1_print_report("child A (no new thread yet)", rc, &r);
  if (rc == MI_PURGE_BUSY) { _exit(11); }
  if (r.theaps_orphaned != 1) { _exit(12); }
  if (r.theaps_pending != 0 || rc != MI_PURGE_OK) { _exit(13); }
  if (r.complete) { _exit(14); }   // an orphan means the report is not complete
  // B. after the child has a worker of its own
  pthread_t w;
  if (pthread_create(&w, NULL, f1_child_worker, NULL) != 0) { _exit(15); }
  for (int i = 0; i < 20000 && !f1_child_worker_ready; i++) { usleep(100); }
  usleep(2000);   // let the worker's last allocator call return (gated: it parks on return)
  rc = mi_purge_all_ex(MI_PURGE_FORCE, 1000, &r);
  f1_print_report("child B (with a child worker)", rc, &r);
  f1_child_worker_stop = 1;
  pthread_join(w, NULL);
  if (rc == MI_PURGE_BUSY) { _exit(16); }
  if (r.theaps_orphaned != 1) { _exit(17); }
  #if F1_GATED
  if (!(r.theaps_swept == 2 && r.theaps_pending == 0 && rc == MI_PURGE_OK)) { _exit(18); }
  #else
  if (!(r.theaps_swept == 1 && r.theaps_pending == 1 && rc == MI_PURGE_PARTIAL)) { _exit(18); }
  #endif
  _exit(0);
}

static void f1_child_during_purge(void) {
  mi_purge_all_report_t r;
  const int rc = mi_purge_all_ex(MI_PURGE_FORCE, 100, &r);
  f1_print_report("child C (forked while the sibling held the purge admission)", rc, &r);
  if (rc == MI_PURGE_BUSY) { _exit(31); }      // the child inherited a held admission
  if (r.theaps_orphaned != 1) { _exit(32); }   // the purging sibling is the one orphan
  if (r.theaps_pending != 0) { _exit(33); }
  _exit(0);
}

static int purge_all_fork_cases(void) {
  int failures = 0;
  fprintf(stderr, "purge_all_fork_cases (F1, gated=%d):\n", F1_GATED);
  mi_register_deferred_free(&f1_deferred_free, NULL);

  // ---- A + B: fork with a live registered sibling ------------------------------------
  pthread_t sib;
  f1_sibling_ready = 0; f1_sibling_stop = 0;
  if (pthread_create(&sib, NULL, f1_sibling, NULL) != 0) {
    fprintf(stderr, "FAIL: F1: could not start the sibling thread\n");
    mi_register_deferred_free(NULL, NULL);
    return 1;
  }
  for (int i = 0; i < 20000 && !f1_sibling_ready; i++) { usleep(100); }
  usleep(2000);
  pid_t pid = fork();
  if (pid < 0) { fprintf(stderr, "FAIL: F1: fork failed: %s\n", strerror(errno)); failures++; }
  else if (pid == 0) { f1_child_with_orphan(); }
  else {
    int code = -1;
    const int rc = wait_child_with_timeout(pid, "F1 child A/B (orphaned sibling)", &code);
    if (rc != 0) { fprintf(stderr, "FAIL: F1 A/B child (exit code %d: 1x orphan count, 13 pending/status, 14 complete flag, 18 child-worker reach)\n", code); failures++; }
    else { fprintf(stderr, "  F1 A/B: ok (orphaned == 1, pending == 0 before a new thread; child worker %s)\n", F1_GATED ? "swept" : "reported pending (default build)"); }
  }
  f1_sibling_stop = 1;
  pthread_join(sib, NULL);

  // ---- C: fork DURING a purge --------------------------------------------------------
  f1_in_handler = 0; f1_sibling_purge_rc = -1;
  pthread_mutex_lock(&f1_mutex);
  pthread_t purger;
  if (pthread_create(&purger, NULL, f1_sibling_purging, NULL) != 0) {
    pthread_mutex_unlock(&f1_mutex);
    fprintf(stderr, "FAIL: F1: could not start the purging sibling\n");
    failures++;
  }
  else {
    for (int i = 0; i < 20000 && !f1_in_handler; i++) { usleep(100); }
    if (!f1_in_handler) { fprintf(stderr, "FAIL: F1 C: the sibling never reached its deferred-free handler\n"); failures++; }
    pid = fork();
    if (pid < 0) { fprintf(stderr, "FAIL: F1 C: fork failed: %s\n", strerror(errno)); failures++; }
    else if (pid == 0) { f1_child_during_purge(); }
    else {
      int code = -1;
      const int rc = wait_child_with_timeout(pid, "F1 child C (fork during a purge)", &code);
      if (rc != 0) { fprintf(stderr, "FAIL: F1 C child (exit code %d: 31 BUSY -- admission not reset in the child, 32 orphan count, 33 pending)\n", code); failures++; }
      else { fprintf(stderr, "  F1 C: ok (child admission clear, orphaned == 1)\n"); }
    }
    pthread_mutex_unlock(&f1_mutex);
    pthread_join(purger, NULL);
    if (f1_sibling_purge_rc == MI_PURGE_BUSY || f1_sibling_purge_rc < 0) {
      fprintf(stderr, "FAIL: F1 C: the parent's in-flight purge did not complete normally (rc=%d)\n", f1_sibling_purge_rc);
      failures++;
    }
  }

  // ---- D: fork from INSIDE an allocator hook -----------------------------------------
  f1_forked_pid = -1;
  f1_mode_thread = pthread_self();
  f1_mode = F1_MODE_FORK;
  mi_collect(false);   // -> handler on this thread -> fork() inside it
  f1_mode = F1_MODE_NONE;
  if (f1_forked_pid < 0) { fprintf(stderr, "FAIL: F1 D: the handler did not run / fork failed\n"); failures++; }
  else {
    int code = -1;
    const int rc = wait_child_with_timeout(f1_forked_pid, "F1 child D (fork inside the deferred-free hook)", &code);
    if (rc != 0) { fprintf(stderr, "FAIL: F1 D child (exit code %d: 21 BUSY, 22 orphan/pending, 23 cannot allocate)\n", code); failures++; }
  }
  // survivor depth balanced: the parent left the hook, and can allocate and purge as usual
  {
    void* p = mi_malloc(300);
    mi_purge_all_report_t r;
    const int rc = mi_purge_all_ex(MI_PURGE_FORCE, 1000, &r);
    f1_print_report("parent after the in-hook fork", rc, &r);
    mi_free(p);
    if (p == NULL || rc != MI_PURGE_OK || r.theaps_pending != 0 || r.theaps_orphaned != 0) {
      fprintf(stderr, "FAIL: F1 D: the parent is not balanced after forking inside the hook\n");
      failures++;
    }
    else { fprintf(stderr, "  F1 D: ok (child purged from inside the hook; parent balanced)\n"); }
  }

  mi_register_deferred_free(NULL, NULL);
  fprintf(stderr, "purge_all_fork_cases: %d failure(s)\n", failures);
  return failures;
}

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

  // #366 row F1 -- before the churn/spawn workload below, so the only registered threads
  // are the ones each case creates (the orphan counts are exact).
  const int f1_rc = purge_all_fork_cases();

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

  fprintf(stderr, "test-fork-locks: %d/%d forks failed, %d/%d hung, spawn-mode %s, dump-in-child %s, deterministic-hold %s, purge-all-fork %s\n",
          failures, N_FORKS, hangs, N_FORKS, spawn_mode ? "on" : "off",
          dump_rc == 0 ? "ok" : "FAILED", det_rc == 0 ? "ok" : "FAILED", f1_rc == 0 ? "ok" : "FAILED");

  if (failures > 0 || hangs > 0 || dump_rc != 0 || det_rc != 0 || f1_rc != 0) {
    fprintf(stderr, "FAIL: test-fork-locks saw %d failures and %d hangs across %d forks (dump-in-child %s, deterministic-hold %s, purge-all-fork %s)\n",
            failures, hangs, N_FORKS, dump_rc == 0 ? "ok" : "failed", det_rc == 0 ? "ok" : "failed", f1_rc == 0 ? "ok" : "failed");
    return 1;
  }
  printf("ok: test-fork-locks: %d forks, 0 failures, 0 hangs, spawn-mode %s, dump-in-child ok, deterministic-hold ok, purge-all-fork ok\n",
         N_FORKS, spawn_mode ? "on" : "off");
  return 0;
}

#endif // !_WIN32
