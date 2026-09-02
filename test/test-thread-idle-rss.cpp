/* Acceptance test for issue #272 (Bun parity P7a): `mi_on_thread_idle` as Bun actually
   consumes it.

   Bun calls it from C++ (`src/jsc/bindings/BunJSCEventLoop.cpp`) and from Rust
   (`src/threading/ThreadPool.rs`, `packages/bun-usockets/src/eventing/epoll_kqueue.c`), with
   no mimalloc header in sight -- just a hand-written declaration. So this TU deliberately does
   NOT include <mimalloc.h> for it: it declares the three entry points exactly the way a
   consumer does, which is the only way to catch a name-mangling or `noexcept`-mismatch
   regression in the public header.

   The behavioural half: a worker thread allocates 256 MiB, frees it, calls `mi_on_thread_idle`,
   and then just idles. Within one second the process RSS must have dropped by at least half of
   what the worker was holding. Without the scavenger this is the case oven-sh/bun#39844 is
   about -- the memory sits in the arena until some other allocation happens to run a purge,
   which on an idle process is never. */

#include <cstdio>
#include <cstring>
#include <cstdlib>

/* platform threads rather than <thread>/<chrono>: some mingw-w64 toolchains ship the win32
   threads model, where <thread> is not available at all. The rest of this tree's tests do the
   same (see test-stress.c's portable threading block). */
#if defined(_WIN32)
#include <windows.h>
typedef HANDLE mi_test_thread_t;
static void sleep_ms(unsigned ms) { Sleep(ms); }
#else
#include <pthread.h>
#include <unistd.h>
typedef pthread_t mi_test_thread_t;
static void sleep_ms(unsigned ms) { usleep(ms * 1000u); }
#endif

// exactly as a consumer declares it (see the file comment)
extern "C" void mi_on_thread_idle(void) noexcept;
extern "C" bool mi_on_thread_idle_start(void) noexcept;
extern "C" void mi_on_thread_idle_end(void) noexcept;

// ... and the two ordinary entry points this test needs, likewise by declaration only
extern "C" void* mi_malloc(size_t size) noexcept;
extern "C" void  mi_free(void* p) noexcept;
extern "C" void  mi_process_info(size_t* elapsed_msecs, size_t* user_msecs, size_t* system_msecs,
                                 size_t* current_rss, size_t* peak_rss,
                                 size_t* current_commit, size_t* peak_commit,
                                 size_t* page_faults) noexcept;

static size_t rss_bytes(void) {
  size_t elapsed = 0, user = 0, sys = 0, cur_rss = 0, peak_rss = 0, cur_commit = 0, peak_commit = 0, faults = 0;
  // `current_rss`, never `peak_rss`: peak is ru_maxrss, which by construction never comes back down
  mi_process_info(&elapsed, &user, &sys, &cur_rss, &peak_rss, &cur_commit, &peak_commit, &faults);
  return cur_rss;
}

#define BLOCK      (1u << 20)     // 1 MiB
#define BLOCKS     (256)          // 256 MiB total
#define TARGET_MB  (256)

static volatile int    phase = 0;          // 0: allocating, 1: freed+idled, 2: main is done measuring
static volatile size_t rss_live_bytes = 0;

static void worker_body(void) {
  void** p = (void**)calloc(BLOCKS, sizeof(void*));
  if (p == nullptr) { phase = 2; return; }
  for (int i = 0; i < BLOCKS; i++) {
    p[i] = mi_malloc(BLOCK);
    if (p[i] == nullptr) { phase = 2; free(p); return; }
    memset(p[i], i & 0xFF, BLOCK);   // touch it, so it is really resident
  }
  rss_live_bytes = rss_bytes();
  for (int i = 0; i < BLOCKS; i++) { mi_free(p[i]); }
  free(p);
  mi_on_thread_idle();              // <- the entry point under test
  phase = 1;
  // ... and then genuinely idle: no allocation of any kind, which is what makes this a test of
  // the scavenger rather than of the next allocation happening to run a purge inline.
  while (phase != 2) { sleep_ms(5); }
}

#if defined(_WIN32)
static DWORD WINAPI worker_main(LPVOID a) { (void)a; worker_body(); return 0; }
static void thread_start(mi_test_thread_t* t) { *t = CreateThread(NULL, 0, &worker_main, NULL, 0, NULL); }
static void thread_join(mi_test_thread_t t) { if (t != NULL) { WaitForSingleObject(t, INFINITE); CloseHandle(t); } }
#else
static void* worker_main(void* a) { (void)a; worker_body(); return NULL; }
static void thread_start(mi_test_thread_t* t) { pthread_create(t, NULL, &worker_main, NULL); }
static void thread_join(mi_test_thread_t t) { pthread_join(t, NULL); }
#endif

int main(void) {
  const size_t rss0 = rss_bytes();
  mi_test_thread_t t;
  thread_start(&t);
  while (phase == 0) { sleep_ms(2); }
  const size_t live = rss_live_bytes;
  if (live == 0) {
    fprintf(stderr, "test-thread-idle-rss: worker could not allocate; skipping\n");
    phase = 2; thread_join(t);
    return 0;
  }
  const size_t held = (live > rss0 ? live - rss0 : 0);
  fprintf(stderr, "rss: start %zu MiB, live %zu MiB (held %zu MiB)\n",
          rss0 / (1024 * 1024), live / (1024 * 1024), held / (1024 * 1024));
  if (held < (size_t)(TARGET_MB / 2) * 1024 * 1024) {
    fprintf(stderr, "test-thread-idle-rss: only %zu MiB became resident; skipping (nothing to reclaim)\n",
            held / (1024 * 1024));
    phase = 2; thread_join(t);
    return 0;
  }

  // poll for up to one second; the MAIN thread must not allocate either, or an inline purge
  // would do the scavenger's job for it and the test would prove nothing.
  size_t best = live;
  long waited_ms = -1;
  for (int i = 0; i < 200; i++) {
    const size_t now = rss_bytes();
    if (now < best) best = now;
    if (best <= rss0 + held / 2) { waited_ms = (long)i * 5; break; }
    sleep_ms(5);
  }
  phase = 2;
  thread_join(t);

  const size_t dropped = (live > best ? live - best : 0);
  fprintf(stderr, "rss after idle: %zu MiB (dropped %zu MiB of %zu MiB held) after %ldms\n",
          best / (1024 * 1024), dropped / (1024 * 1024), held / (1024 * 1024), waited_ms);
  if (waited_ms < 0) {
    fprintf(stderr, "test-thread-idle-rss: FAILED (RSS did not drop by half of the freed 256 MiB within 1s)\n");
    return 1;
  }
  fprintf(stderr, "test-thread-idle-rss: ok\n");
  return 0;
}
