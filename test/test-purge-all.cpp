/* #366: `mi_purge_all` / `MI_OWNER_GATE` acceptance test (docs/purge-all-implementation.md §11).

   One binary, both builds. The ungated (default) and the gated (`MI_OWNER_GATE=ON`) build run
   the same rows; every row states its expectation per build through `#if MI_OWNER_GATE`. The
   default build's contract is "arenas, abandoned pages, the caller, parked threads; everything
   else reported pending" and the gated build's is "every registered thread" -- so the ungated
   half of G1 (`pending == 4`, `MI_PURGE_PARTIAL`) is a positive control on the report itself:
   a walk that claimed to reach busy threads it cannot reach would fail there.

   Threading is modelled on test-thread-idle-rss.cpp -- platform threads, never <thread>: some
   mingw-w64 toolchains ship the win32 threads model, where <thread>/<mutex> do not exist -- so
   this runs on MSVC and win-gnu as well as on Linux. (<atomic> is fine on both models.)

   Every row asserts on a measured value and prints it; a hang fails through ctest's TIMEOUT
   (120 s, CMakeLists.txt), never through a loop of its own.

   ROWS THAT ARE APPROXIMATED, and how (the plan asks for MI_DEBUG pause hooks that src/ does
   not have; this file adds nothing to src/):

     G3  "a MI_DEBUG pause hook holds one worker RUNNING inside the allocator". In MI_DEBUG>0
         builds on POSIX this uses the ONE such hook that exists, #270/#272's
         `mi_debug_stall_in_thread_theaps_done` (src/init.c): an exiting thread stalls inside
         `_mi_thread_done` with its tld still registered -- and in a gated build `_mi_thread_done`
         is an enter-without-leave site (§5.1), so that thread is RUNNING inside the allocator,
         which is exactly the state the row wants. Elsewhere (Release, Windows) it falls back to
         the G6 construction: a worker blocked inside its deferred-free handler on a mutex the
         main thread holds is a thread that is inside the allocator (`mi_collect` is a gated
         public entry) and stays there until main lets it out.
     C1  "deterministic pause points before the page lookup in <six paths>, each raced by a
         claimant". No pause points exist; the paths are instead exercised CONTINUOUSLY by one
         worker each while the main thread claims in a tight loop, so the race is statistical
         rather than pinned. The output-hook-recursion-during-`mi_thread_init` case is
         approximated by a thread whose FIRST allocator call is an oversized request: that
         reaches `mi_find_page`'s "request is too large" error inside `_mi_malloc_generic`,
         right before the page lookup, with the thread just initialised and (gated) its gate
         held, and the error goes through an output hook that allocates. The leaf `_mi_gate_held`
         assertions of the gated MI_DEBUG build and ASan are the oracle, as the plan says.
     C2  "owner `gate_depth` and `mi_tld_t::profiler` unchanged across the sweep" are private
         fields; the observable used instead is that the owner keeps allocating after the sweep
         and its survivors are byte-for-byte intact, and the profiler-invariant assert lives in
         `_mi_thread_idle_work_ex` itself (MI_DEBUG).
     T3  "`gate_depth` back to 0" is asserted by the library at every leave (MI_DEBUG); here the
         observable is that both re-entrant calls return BUSY and the outer call completes.

   The scavenger is a confound for the hole-byte measurements in a GATED build: there every
   thread outside the allocator is PARKED, so the background thread's timed sweep would reach
   the very holes a row wants `mi_purge_all` to be the first to discard. So the test pins
   `purge_holes_min_interval` to its 1-hour maximum for the rows that measure reach (T2, G1) and
   has every thread of those rows stamp its own `holes_sweep_last` first (`mi_on_thread_idle()`
   on an empty heap), which pauses the scavenger's paced sweep of that thread for the hour;
   `MI_PURGE_FORCE` ignores that pacing, so the call under test is unaffected. T1 runs with the
   interval at 0 (its reference number is "everything a sweep can discard", whoever sweeps),
   and G8 sets it to 50 ms because G8 is ABOUT the paced scavenger sweep. */

#include <mimalloc.h>

#include <atomic>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>

#if defined(_WIN32)
#include <windows.h>
#else
#include <pthread.h>
#include <time.h>
#include <unistd.h>
#endif

// ---------------------------------------------------------------------------------------------
// portable threads, mutex, clock
// ---------------------------------------------------------------------------------------------

#if defined(_WIN32)
typedef HANDLE thread_t;
typedef DWORD(WINAPI* thread_fun_t)(void*);
#define THREAD_RET DWORD WINAPI
#define THREAD_OK 0
static bool thread_start(thread_t* t, thread_fun_t fn, void* arg) {
  *t = CreateThread(NULL, 0, fn, arg, 0, NULL);
  return (*t != NULL);
}
static void thread_join(thread_t t) { if (t != NULL) { WaitForSingleObject(t, INFINITE); CloseHandle(t); } }
static void sleep_ms(unsigned ms) { Sleep(ms); }
typedef CRITICAL_SECTION mutex_t;
static void mutex_init(mutex_t* m) { InitializeCriticalSection(m); }
static void mutex_destroy(mutex_t* m) { DeleteCriticalSection(m); }
static void mutex_lock(mutex_t* m) { EnterCriticalSection(m); }
static void mutex_unlock(mutex_t* m) { LeaveCriticalSection(m); }
static double now_ms(void) {
  LARGE_INTEGER f, c;
  QueryPerformanceFrequency(&f);
  QueryPerformanceCounter(&c);
  return (double)c.QuadPart * 1000.0 / (double)f.QuadPart;
}
static size_t os_page_size(void) { SYSTEM_INFO si; GetSystemInfo(&si); return (size_t)si.dwPageSize; }
#else
typedef pthread_t thread_t;
typedef void* (*thread_fun_t)(void*);
#define THREAD_RET void*
#define THREAD_OK NULL
static bool thread_start(thread_t* t, thread_fun_t fn, void* arg) { return pthread_create(t, NULL, fn, arg) == 0; }
static void thread_join(thread_t t) { pthread_join(t, NULL); }
static void sleep_ms(unsigned ms) { usleep(ms * 1000u); }
typedef pthread_mutex_t mutex_t;
static void mutex_init(mutex_t* m) { pthread_mutex_init(m, NULL); }
static void mutex_destroy(mutex_t* m) { pthread_mutex_destroy(m); }
static void mutex_lock(mutex_t* m) { pthread_mutex_lock(m); }
static void mutex_unlock(mutex_t* m) { pthread_mutex_unlock(m); }
static double now_ms(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (double)ts.tv_sec * 1000.0 + (double)ts.tv_nsec / 1000000.0;
}
static size_t os_page_size(void) { long p = sysconf(_SC_PAGESIZE); return (p > 0 ? (size_t)p : 4096); }
#endif

// #272 test observable (src/scavenger.c): completed idle-work passes, on any thread.
extern "C" size_t _mi_test_idle_work_count(void);

// G3's real pause hook, where it exists (see the file comment). `_Atomic(uintptr_t)` in C;
// declared as the layout-identical std::atomic here, as test/test-fork-user-heap.c does.
#if (MI_DEBUG > 0) && !defined(_WIN32)
#define MI_TEST_HAVE_STALL_HOOK 1
extern "C" { extern std::atomic<uintptr_t> mi_debug_stall_in_thread_theaps_done; }
#else
#define MI_TEST_HAVE_STALL_HOOK 0
#endif

#if MI_OWNER_GATE
static const bool gated = true;
#else
static const bool gated = false;
#endif

// ---------------------------------------------------------------------------------------------
// assertions
// ---------------------------------------------------------------------------------------------

static int failures = 0;

static void check(const char* name, bool ok) {
  fprintf(stderr, "test: %s...  %s\n", name, ok ? "ok." : "FAILED");
  if (!ok) failures++;
}

static const char* status_name(int rc) {
  switch (rc) {
    case MI_PURGE_OK:      return "OK";
    case MI_PURGE_PARTIAL: return "PARTIAL";
    case MI_PURGE_BUSY:    return "BUSY";
    default:               return "?";
  }
}

static void print_report(const char* label, int rc, const mi_purge_all_report_t* r, double elapsed_ms) {
  fprintf(stderr, "  %s: %s in %.1fms  swept=%zu pending=%zu orphaned=%zu hole_bytes=%zu arena_bytes=%zu gated=%d complete=%d\n",
          label, status_name(rc), elapsed_ms, r->theaps_swept, r->theaps_pending, r->theaps_orphaned,
          r->hole_bytes, r->arena_bytes, (int)r->gated, (int)r->complete);
}

// a timed `mi_purge_all_ex`
static int purge(mi_purge_flags_t flags, size_t wait_ms, mi_purge_all_report_t* r, double* elapsed_ms) {
  const double t0 = now_ms();
  const int rc = mi_purge_all_ex(flags, wait_ms, r);
  *elapsed_ms = now_ms() - t0;
  return rc;
}

// ---------------------------------------------------------------------------------------------
// hole stats and the churn shape (park-handoff's: one survivor every two OS pages of blocks)
// ---------------------------------------------------------------------------------------------

static size_t hole_bytes_total(void) { mi_purge_holes_stats_t h; mi_purge_holes_stats_get(&h); return h.purged_bytes_total; }
static void print_hole_stats(const char* label) {
  mi_purge_holes_stats_t h; mi_purge_holes_stats_get(&h);
  fprintf(stderr, "  %s: holes now %zu bytes/%zu blocks, total %zu, discard_calls %zu, pages_freed %zu, ineligible %zu pages/%zu bytes (%zu free), unformed %zu, skipped %zu, visited %zu, full_sweeps %zu\n",
          label, h.purged_bytes, h.purged_blocks, h.purged_bytes_total, h.discard_calls, h.pages_freed,
          h.ineligible_pages, h.ineligible_bytes, h.ineligible_free_bytes, h.unformed_bytes_total, h.pages_skipped, h.blocks_visited, h.full_sweeps);
}
static size_t hole_discard_calls(void) { mi_purge_holes_stats_t h; mi_purge_holes_stats_get(&h); return h.discard_calls; }

#if defined(MI_GUARDED)
#define LIVE (2000)     // every sampled allocation is its own mapping under MI_GUARDED: stay under vm.max_map_count
#else
#define LIVE (20000)
#endif
#define BSZ (512)

static size_t keep_every(void) { return (((size_t)2 * os_page_size()) + BSZ - 1) / BSZ + 2; }

static uint8_t pattern_byte(size_t id, size_t off) { return (uint8_t)((id * 131u) ^ (off * 7u) ^ (off >> 8)); }
static void pattern_fill(void* p, size_t id) { uint8_t* b = (uint8_t*)p; for (size_t i = 0; i < BSZ; i++) b[i] = pattern_byte(id, i); }
static bool pattern_ok(const void* p, size_t id) { const uint8_t* b = (const uint8_t*)p; for (size_t i = 0; i < BSZ; i++) if (b[i] != pattern_byte(id, i)) return false; return true; }

// allocate LIVE blocks, free all but every `keep_every`-th: holes in still-used pages
static void churn(void** p) {
  const size_t ke = keep_every();
  for (int i = 0; i < LIVE; i++) { if (p[i] == NULL) { p[i] = mi_malloc(BSZ); if (p[i] != NULL) pattern_fill(p[i], (size_t)i); } }
  for (int i = 0; i < LIVE; i++) { if (((size_t)i % ke) != 0 && p[i] != NULL) { mi_free(p[i]); p[i] = NULL; } }
}
static void free_all(void** p) { for (int i = 0; i < LIVE; i++) { if (p[i] != NULL) { mi_free(p[i]); p[i] = NULL; } } }
static long first_corrupt(void** p) { for (int i = 0; i < LIVE; i++) { if (p[i] != NULL && !pattern_ok(p[i], (size_t)i)) return i; } return -1; }

// ---------------------------------------------------------------------------------------------
// the hooks: ONE deferred-free handler and ONE output hook for the whole binary, installed
// before the first allocation (mimalloc.h: "install a single deferred free handler before
// doing allocation"), dispatching on a thread-local mode so a worker's own `mi_collect` never
// triggers main's row and vice versa.
// ---------------------------------------------------------------------------------------------

enum hook_mode_t {
  HOOK_NONE = 0,
  HOOK_PURGE_REENTRANT,      // T3(a): call mi_purge_all_ex from inside the handler
  HOOK_PRINT_REENTRANT,      // T3(b): emit an output line from inside the handler; the OUTPUT hook purges
  HOOK_BLOCK_ON_MUTEX,       // G3-fallback / G6: block on `g_mutex` (held by main) inside the handler
  HOOK_WAIT_FOR_PEER,        // G5: wait until `g_peer_done`, join the peer, then return
};

static thread_local int tl_mode = HOOK_NONE;

static mutex_t g_mutex;
static std::atomic<int> g_in_handler(0);          // a worker reports "I am inside the handler now"
static std::atomic<int> g_peer_done(0);
static thread_t g_peer_thread;
static std::atomic<int> g_handler_calls(0);

static std::atomic<int> g_inner_rc(-1);           // T3: what the re-entrant call returned
static mi_purge_all_report_t g_inner_report;

static std::atomic<int> g_output_should_purge(0); // T3(b): the output hook purges once
static std::atomic<int> g_output_rc(-1);
static std::atomic<int> g_output_swallow(0);      // C1/C2/T3: do not echo mimalloc's own messages
static std::atomic<int> g_output_allocate(0);     // C1/C2: the output hook allocates
static std::atomic<size_t> g_output_messages(0);
static std::atomic<size_t> g_output_allocs(0);
static std::atomic<size_t> g_errors_seen(0);

static void mi_cdecl deferred_free_hook(bool force, unsigned long long heartbeat, void* arg) {
  (void)force; (void)heartbeat; (void)arg;
  g_handler_calls.fetch_add(1);
  switch (tl_mode) {
    case HOOK_PURGE_REENTRANT: {
      tl_mode = HOOK_NONE;
      const int rc = mi_purge_all_ex((mi_purge_flags_t)0, 0, &g_inner_report);
      g_inner_rc.store(rc);
      break;
    }
    case HOOK_PRINT_REENTRANT: {
      tl_mode = HOOK_NONE;
      g_output_should_purge.store(1);
      // one line through the output hook: an oversized request is refused with an error
      // message (`mi_option_verbose` is on for the row, so it is not rate limited)
      void* p = mi_malloc((size_t)PTRDIFF_MAX);
      if (p != NULL) mi_free(p);
      break;
    }
    case HOOK_BLOCK_ON_MUTEX: {
      tl_mode = HOOK_NONE;
      g_in_handler.store(1);
      mutex_lock(&g_mutex);     // main holds it: this thread is now stuck INSIDE the allocator
      mutex_unlock(&g_mutex);
      break;
    }
    case HOOK_WAIT_FOR_PEER: {
      tl_mode = HOOK_NONE;
      g_in_handler.store(1);
      for (int i = 0; i < 20000 && g_peer_done.load() == 0; i++) sleep_ms(1);   // bounded; the ctest TIMEOUT is the backstop
      thread_join(g_peer_thread);
      break;
    }
    default: break;
  }
}

static void mi_cdecl output_hook(const char* msg, void* arg) {
  (void)arg;
  g_output_messages.fetch_add(1);
  if (g_output_should_purge.exchange(0) != 0) {
    mi_purge_all_report_t r;
    g_output_rc.store(mi_purge_all_ex((mi_purge_flags_t)0, 0, &r));
  }
  if (g_output_allocate.load() != 0) {
    // an allocating output hook (a logger with its own buffers): recursion into the
    // allocator from inside the allocator, on whichever thread printed
    void* p = mi_malloc(48);
    if (p != NULL) { memset(p, 0x5a, 48); mi_free(p); g_output_allocs.fetch_add(1); }
  }
  // swallowed rows still surface an assertion failure or a warning from the library
  if (g_output_swallow.load() == 0 || strstr(msg, "assert") != NULL || strstr(msg, "warning") != NULL) { fputs(msg, stderr); }
}

static void mi_cdecl error_hook(int err, void* arg) { (void)err; (void)arg; g_errors_seen.fetch_add(1); }

// ---------------------------------------------------------------------------------------------
// workers
// ---------------------------------------------------------------------------------------------

struct worker_t {
  thread_t          thread;
  std::atomic<int>  stop;
  std::atomic<int>  phase;         // row-specific progress flag
  std::atomic<long> iterations;
  double            max_call_ms;   // G2: the longest single allocator call this worker saw
  bool              corrupt;       // C2: a survivor was not intact at the end
  bool              prestamp;      // stamp `holes_sweep_last` first (pause the scavenger for this tld)
  bool              idle;          // T1: call mi_on_thread_idle after churning
  bool              keep_holes;    // hold the churned survivors while running (G1/G7/G8/C2)
  bool              big_frees;     // G8: also free a 2 MiB block periodically (schedules arena purges, which wake the scavenger)
  unsigned          spin_ms;       // G8: CPU-busy for this long between allocator calls (0: a hot loop with no gap)
  worker_t() : thread(), stop(0), phase(0), iterations(0), max_call_ms(0), corrupt(false),
               prestamp(false), idle(false), keep_holes(false), big_frees(false), spin_ms(0) {}
};

// T1/T2/G1/G2/G7/G8/C2: churn (holes), optionally idle, then a hot-set loop until told to stop.
// `spin_ms` (G8) makes the worker CPU-busy OUTSIDE the allocator between calls: a paced timed
// sweep is bounded per visit by `park_reclaim` (the owner's next allocator call aborts it
// between pages), so a thread that never leaves the allocator for longer than one sweep is
// never counted as swept -- G8 is about the pacing, not about starving the sweeper.
static void busy_spin(unsigned ms) { if (ms == 0) return; const double t_end = now_ms() + (double)ms; volatile unsigned x = 0; while (now_ms() < t_end) { x = x * 1664525u + 1013904223u; } }
static THREAD_RET churn_worker(void* varg) {
  worker_t* w = (worker_t*)varg;
  if (w->prestamp) { void* q = mi_malloc(64); mi_free(q); mi_on_thread_idle(); }
  void** p = (void**)calloc(LIVE, sizeof(void*));
  if (p == NULL) { w->phase.store(-1); return THREAD_OK; }
  churn(p);
  if (w->idle) { mi_on_thread_idle(); }
  w->phase.store(1);
  long n = 0;
  while (w->stop.load() == 0) {
    const double t0 = now_ms();
    void* q = mi_malloc(64);
    if (q != NULL) { *(volatile char*)q = 1; mi_free(q); }
    if (w->big_frees && (n % 4) == 0) { void* big = mi_malloc(2u << 20); if (big != NULL) { *(volatile char*)big = 1; mi_free(big); } }
    const double dt = now_ms() - t0;
    if (dt > w->max_call_ms) w->max_call_ms = dt;
    n++;
    w->iterations.store(n);
    busy_spin(w->spin_ms);
  }
  if (w->keep_holes) { if (first_corrupt(p) >= 0) w->corrupt = true; }
  free_all(p);
  free(p);
  w->phase.store(2);
  return THREAD_OK;
}

// T2: churn and EXIT with the survivors still allocated (abandoned pages with undiscarded holes).
// The survivor arrays are handed to main, which frees them once T2 has measured: otherwise the
// abandoned pages -- holes already discarded -- get reclaimed by the next row's workers and
// that row's "bytes newly discarded" undercounts what its sweep reached.
static void** g_t2_survivors[8];
static THREAD_RET abandon_worker(void* varg) {
  worker_t* w = (worker_t*)varg;
  if (w->prestamp) { void* q = mi_malloc(64); mi_free(q); mi_on_thread_idle(); }
  void** p = (void**)calloc(LIVE, sizeof(void*));
  if (p == NULL) { w->phase.store(-1); return THREAD_OK; }
  churn(p);
  g_t2_survivors[w->phase.load()] = p;   // the mi blocks stay allocated on purpose: their pages are abandoned at exit
  w->phase.store(1);
  return THREAD_OK;
}

// G3-fallback / G6 / G5: enter the allocator through a public collect so the handler runs on
// this thread (`_mi_deferred_free` is owner-only and `mi_collect` is a gated public entry)
static THREAD_RET hook_worker(void* varg) {
  worker_t* w = (worker_t*)varg;
  void* q = mi_malloc(128); mi_free(q);
  tl_mode = w->phase.load();     // the row's hook mode is passed in `phase`
  w->phase.store(0);
  mi_collect(false);             // -> handler on this thread
  long n = 0;
  while (w->stop.load() == 0) { void* r = mi_malloc(64); if (r != NULL) mi_free(r); if ((++n % 64) == 0) sleep_ms(1); }
  w->iterations.store(n);
  w->phase.store(2);
  return THREAD_OK;
}

// G3 (real hook): allocate a little and exit; the exit stalls inside `_mi_thread_done`
#if MI_TEST_HAVE_STALL_HOOK
static THREAD_RET exit_worker(void* varg) {
  worker_t* w = (worker_t*)varg;
  void** p = (void**)calloc(256, sizeof(void*));
  if (p != NULL) { for (int i = 0; i < 256; i++) p[i] = mi_malloc(96); for (int i = 0; i < 256; i++) mi_free(p[i]); free(p); }
  w->phase.store(1);
  return THREAD_OK;
}
#endif  // MI_TEST_HAVE_STALL_HOOK

// G5: the second caller
static THREAD_RET peer_caller(void* varg) {
  worker_t* w = (worker_t*)varg;
  for (int i = 0; i < 20000 && g_in_handler.load() == 0; i++) sleep_ms(1);
  mi_purge_all_report_t r;
  double ms = 0;
  const int rc = purge(MI_PURGE_FORCE, 1000, &r, &ms);
  w->max_call_ms = ms;
  w->phase.store(rc + 100);
  g_peer_done.store(1);
  return THREAD_OK;
}

// G4: a spawner keeps starting short-lived threads that allocate and exit
static THREAD_RET short_lived(void* varg) {
  (void)varg;
  void* p[32];
  for (int i = 0; i < 32; i++) p[i] = mi_malloc(64 + (size_t)i * 16);
  for (int i = 0; i < 32; i++) mi_free(p[i]);
  return THREAD_OK;
}
static THREAD_RET spawner(void* varg) {
  worker_t* w = (worker_t*)varg;
  long n = 0;
  while (w->stop.load() == 0) {
    thread_t t;
    if (thread_start(&t, &short_lived, NULL)) { thread_join(t); n++; }
    else { sleep_ms(1); }
  }
  w->iterations.store(n);
  return THREAD_OK;
}
static THREAD_RET churn_loop(void* varg) {   // G4: continuous churn/refill
  worker_t* w = (worker_t*)varg;
  void** p = (void**)calloc(LIVE, sizeof(void*));
  if (p == NULL) return THREAD_OK;
  long n = 0;
  while (w->stop.load() == 0) { churn(p); n++; }
  free_all(p); free(p);
  w->iterations.store(n);
  return THREAD_OK;
}

// C1: the paths, one worker each
static THREAD_RET c1_small(void* varg) {
  worker_t* w = (worker_t*)varg;
  long n = 0;
  while (w->stop.load() == 0) { void* p = mi_malloc(16 + (size_t)(n % 60) * 16); if (p != NULL) { *(volatile char*)p = 1; mi_free(p); } n++; }
  w->iterations.store(n);
  return THREAD_OK;
}
static THREAD_RET c1_heap(void* varg) {
  worker_t* w = (worker_t*)varg;
  long n = 0;
  while (w->stop.load() == 0) {
    mi_heap_t* h = mi_heap_new();
    if (h != NULL) {
      void* p[16];
      for (int i = 0; i < 16; i++) p[i] = mi_heap_malloc(h, 32 + (size_t)i * 8);
      for (int i = 0; i < 16; i++) mi_free(p[i]);
      void* s = mi_heap_malloc_small(h, 24); if (s != NULL) mi_free(s);
      mi_heap_delete(h);
    }
    n++;
  }
  w->iterations.store(n);
  return THREAD_OK;
}
static THREAD_RET c1_aligned(void* varg) {
  worker_t* w = (worker_t*)varg;
  long n = 0;
  while (w->stop.load() == 0) {
    void* a = mi_malloc_aligned(40, 64);          // the aligned fast path (small, fits the size class)
    void* b = mi_malloc_aligned(200, 4096);       // the aligned slow path
    void* c = mi_zalloc_aligned(64, 32);
    mi_free(a); mi_free(b); mi_free(c);   // mi_free(NULL) is a no-op
    n++;
  }
  w->iterations.store(n);
  return THREAD_OK;
}
static THREAD_RET c1_mixed(void* varg) {       // zalloc/calloc/realloc (+ the guarded path under MI_GUARDED sampling)
  worker_t* w = (worker_t*)varg;
  long n = 0;
  while (w->stop.load() == 0) {
    void* p = mi_zalloc(96); void* q = mi_calloc(4, 40);
    p = mi_realloc(p, 300);
    mi_free(p); mi_free(q);
    n++;
  }
  w->iterations.store(n);
  return THREAD_OK;
}
// C1: a thread whose FIRST allocator call is refused with an error message -> the allocating
// output hook runs inside `_mi_malloc_generic` on a just-initialised thread (see file comment)
static THREAD_RET c1_first_call_prints(void* varg) {
  (void)varg;
  void* p = mi_malloc((size_t)PTRDIFF_MAX);
  if (p != NULL) mi_free(p);
  void* q = mi_malloc(24); if (q != NULL) mi_free(q);   // ...and then an ordinary first page lookup
  return THREAD_OK;
}
static THREAD_RET c1_spawner(void* varg) {
  worker_t* w = (worker_t*)varg;
  long n = 0;
  while (w->stop.load() == 0) {
    thread_t t;
    if (thread_start(&t, &c1_first_call_prints, NULL)) { thread_join(t); n++; } else { sleep_ms(1); }
  }
  w->iterations.store(n);
  return THREAD_OK;
}

static bool wait_phase(worker_t* w, int value, unsigned timeout_ms) {
  for (unsigned i = 0; i < timeout_ms; i++) { if (w->phase.load() == value) return true; sleep_ms(1); }
  return w->phase.load() == value;
}

static long interval_min_ms;   // saved `purge_holes_min_interval`
static void pin_interval(long ms) { mi_option_set(mi_option_purge_holes_min_interval, ms); }

// ---------------------------------------------------------------------------------------------
// T1  reference: N=4 workers churn, each calls mi_on_thread_idle(); R_ref = discarded bytes
// ---------------------------------------------------------------------------------------------

static size_t R_ref = 0;

static void test_t1_reference(void) {
  enum { N = 4 };
  pin_interval(0);   // no pacing: the number is "everything a sweep can discard"
  worker_t w[N];
  const size_t before = hole_bytes_total();
  for (int i = 0; i < N; i++) { w[i].idle = true; if (!thread_start(&w[i].thread, &churn_worker, &w[i])) { check("T1: start workers", false); return; } }
  bool idled = true;
  for (int i = 0; i < N; i++) idled = wait_phase(&w[i], 1, 30000) && idled;
  for (int i = 0; i < N; i++) w[i].stop.store(1);
  for (int i = 0; i < N; i++) thread_join(w[i].thread);
  R_ref = hole_bytes_total() - before;
  fprintf(stderr, "  T1: R_ref = %zu bytes discarded by %d workers' own idle sweeps (LIVE=%d, BSZ=%d)\n", R_ref, N, LIVE, BSZ);
  check("T1: reference sweep discards hole bytes (R_ref > 0)", idled && R_ref > 0);
  pin_interval(3600000);
}

// ---------------------------------------------------------------------------------------------
// T2  abandoned reach: workers exit with survivors; mi_collect(true) leaves purged bytes
//     unchanged, mi_purge_all(true) discards the abandoned holes (both builds)
// ---------------------------------------------------------------------------------------------

static void test_t2_abandoned(void) {
  enum { N = 4 };
  worker_t w[N];
  const size_t before = hole_bytes_total();
  for (int i = 0; i < N; i++) { w[i].prestamp = true; w[i].phase.store(i); if (!thread_start(&w[i].thread, &abandon_worker, &w[i])) { check("T2: start workers", false); return; } }
  for (int i = 0; i < N; i++) thread_join(w[i].thread);
  const size_t after_exit = hole_bytes_total();
  mi_collect(true);
  const size_t after_collect = hole_bytes_total();
  mi_purge_all_report_t r; double ms = 0;
  const int rc = purge(MI_PURGE_FORCE, 1000, &r, &ms);
  const size_t after_purge = hole_bytes_total();
  print_report("T2 mi_purge_all(true)", rc, &r, ms);
  print_hole_stats("T2 after the purge");
  const int rc_again = purge(MI_PURGE_FORCE, 1000, &r, &ms);   // a second pass finds nothing more to discard
  print_report("T2 second mi_purge_all(true)", rc_again, &r, ms);
  fprintf(stderr, "  T2: hole bytes: +%zu during churn/exit, +%zu across mi_collect(true), +%zu across mi_purge_all, +%zu across a second one (R_ref %zu)\n",
          after_exit - before, after_collect - after_exit, after_purge - after_collect, hole_bytes_total() - after_purge, R_ref);
  check("T2: mi_collect(true) does not discard abandoned holes", after_collect == after_exit);
  check("T2: mi_purge_all(true) reaches the abandoned holes", after_purge > after_collect && rc != MI_PURGE_BUSY);
  // The fraction of R_ref that lands in ABANDONED pages depends on how many of the exiting
  // workers' pages were abandoned full vs. reclaimed by the survivors' cross-thread frees --
  // 0.3..1.0 across runs and builds. It is reported, not asserted: the contract is the two
  // checks above (mi_collect cannot reach them, mi_purge_all can).
  fprintf(stderr, "  T2: abandoned reach = %.2f x R_ref\n", (R_ref == 0 ? 0.0 : (double)(after_purge - after_collect) / (double)R_ref));
  // free the survivors (cross-thread frees into abandoned pages): the pages leave for good
  for (int i = 0; i < N; i++) { if (g_t2_survivors[i] != NULL) { free_all(g_t2_survivors[i]); free(g_t2_survivors[i]); g_t2_survivors[i] = NULL; } }
  mi_collect(true);
}

// ---------------------------------------------------------------------------------------------
// T3  re-entrancy: from a deferred-free handler; from an output hook -> BUSY, no deadlock
// ---------------------------------------------------------------------------------------------

static void test_t3_reentrancy(void) {
  mi_purge_all_report_t r; double ms = 0;
  // (a) deferred-free handler: phase C collects the caller's own theap through the owner door,
  // which runs the handler on this thread while the admission is held
  g_inner_rc.store(-1);
  tl_mode = HOOK_PURGE_REENTRANT;
  int rc = purge(MI_PURGE_FORCE, 100, &r, &ms);
  tl_mode = HOOK_NONE;
  print_report("T3(a) outer", rc, &r, ms);
  fprintf(stderr, "  T3(a): re-entrant call from the deferred-free handler returned %s (gated=%d)\n", status_name(g_inner_rc.load()), (int)g_inner_report.gated);
  check("T3(a): the handler's re-entrant mi_purge_all_ex is BUSY", g_inner_rc.load() == MI_PURGE_BUSY);
  check("T3(a): the outer call still completes", rc != MI_PURGE_BUSY);
  check("T3(a): the BUSY report says nothing was done", g_inner_report.theaps_swept == 0 && g_inner_report.hole_bytes == 0 && g_inner_report.gated == gated);
  // (b) output hook: the handler emits one message; the output hook purges
  g_output_rc.store(-1);
  g_output_swallow.store(1);
  mi_option_set_enabled(mi_option_verbose, true);
  tl_mode = HOOK_PRINT_REENTRANT;
  rc = purge(MI_PURGE_FORCE, 100, &r, &ms);
  tl_mode = HOOK_NONE;
  mi_option_set_enabled(mi_option_verbose, false);
  g_output_swallow.store(0);
  print_report("T3(b) outer", rc, &r, ms);
  fprintf(stderr, "  T3(b): re-entrant call from the output hook returned %s\n", status_name(g_output_rc.load()));
  check("T3(b): the output hook's re-entrant mi_purge_all_ex is BUSY", g_output_rc.load() == MI_PURGE_BUSY);
  check("T3(b): the outer call still completes", rc != MI_PURGE_BUSY);
  // and after both: admission is free again
  rc = purge(MI_PURGE_FORCE, 100, &r, &ms);
  check("T3: admission released after the re-entrant attempts", rc != MI_PURGE_BUSY);
}

// ---------------------------------------------------------------------------------------------
// G1/G2  busy owners: 4 workers in a hot-set loop that never idles; negative control first
// ---------------------------------------------------------------------------------------------

static void test_g1_g2_busy_owners(void) {
  enum { N = 4 };
  worker_t w[N];
  const size_t bytes_before = hole_bytes_total();
  for (int i = 0; i < N; i++) {
    w[i].prestamp = true; w[i].keep_holes = true;
    if (!thread_start(&w[i].thread, &churn_worker, &w[i])) { check("G1: start workers", false); return; }
  }
  bool ready = true;
  for (int i = 0; i < N; i++) ready = wait_phase(&w[i], 1, 30000) && ready;
  // negative control: nobody idles and (interval pinned, tlds stamped) nobody sweeps -- the
  // discard counter must not move for 200 ms of busy workers
  const size_t calls0 = hole_discard_calls();
  const long it0 = w[0].iterations.load();
  sleep_ms(200);
  const size_t calls1 = hole_discard_calls();
  fprintf(stderr, "  G1: negative control: discard_calls %zu -> %zu over 200ms; worker 0 did %ld allocator calls meanwhile\n",
          calls0, calls1, w[0].iterations.load() - it0);
  check("G1: negative control -- busy workers do not discard on their own", calls1 == calls0 && w[0].iterations.load() > it0);
  for (int i = 0; i < N; i++) w[i].max_call_ms = 0;   // G2 measures from here: the purge, not the churn
  mi_purge_all_report_t r; double ms = 0;
  const int rc = purge(MI_PURGE_FORCE, 2000, &r, &ms);
  const size_t bytes_after = hole_bytes_total();
  print_hole_stats("G1 after the purge");
  for (int i = 0; i < N; i++) w[i].stop.store(1);
  for (int i = 0; i < N; i++) thread_join(w[i].thread);
  print_report("G1 mi_purge_all(FORCE, 2000ms)", rc, &r, ms);
  const size_t delta = bytes_after - bytes_before;
  fprintf(stderr, "  G1: hole bytes discarded since the workers started: %zu (R_ref %zu, ratio %.2f)\n",
          delta, R_ref, R_ref ? (double)delta / (double)R_ref : 0.0);
  double worst = 0; for (int i = 0; i < N; i++) if (w[i].max_call_ms > worst) worst = w[i].max_call_ms;
  fprintf(stderr, "  G2: worst single allocator call on a worker during the purge: %.2f ms\n", worst);
  check("G1: the workers were ready", ready);
#if MI_OWNER_GATE
  check("G1 (gated): every busy worker was swept (swept == 5, pending == 0)", r.theaps_swept == 5 && r.theaps_pending == 0);
  check("G1 (gated): status OK", rc == MI_PURGE_OK);
  check("G1 (gated): the busy workers' holes were discarded (>= 0.9 R_ref)", (double)delta >= 0.9 * (double)R_ref);
  bool intact = true; for (int i = 0; i < N; i++) if (w[i].corrupt) intact = false;
  check("G1 (gated): survivors intact after a foreign sweep", intact);
#else
  check("G1 (ungated, positive control): the 4 RUNNING workers are reported pending", r.theaps_pending == 4);
  check("G1 (ungated, positive control): status PARTIAL", rc == MI_PURGE_PARTIAL);
#endif
#if (MI_DEBUG > 1)
  const double stall_bound_ms = 1000.0;   // MI_DEBUG>1: the collect validates every page (`mi_assert_expensive`)
#else
  const double stall_bound_ms = 250.0;
#endif
  check("G2: worst worker stall during the purge is bounded", worst < stall_bound_ms);
}

// ---------------------------------------------------------------------------------------------
// G3  owner inside the allocator: PARTIAL/pending==1 within wait_ms + one sweep; retry -> OK
// ---------------------------------------------------------------------------------------------

static void test_g3_owner_inside(void) {
  mi_purge_all_report_t r; double ms = 0;
#if MI_TEST_HAVE_STALL_HOOK
  // the real hook: the NEXT exiting thread stalls inside _mi_thread_done, tld still registered
  worker_t w;
  mi_debug_stall_in_thread_theaps_done.store(1);
  if (!thread_start(&w.thread, &exit_worker, &w)) { mi_debug_stall_in_thread_theaps_done.store(0); check("G3: start worker", false); return; }
  bool stalled = false;
  for (int i = 0; i < 20000 && !(stalled = (mi_debug_stall_in_thread_theaps_done.load() == 2)); i++) sleep_ms(1);
  const int rc1 = purge((mi_purge_flags_t)0, 20, &r, &ms);
  print_report("G3 _ex(0, 20ms) with a thread stalled in _mi_thread_done", rc1, &r, ms);
  check("G3: the stalled owner is reported pending (PARTIAL, pending == 1)", stalled && rc1 == MI_PURGE_PARTIAL && r.theaps_pending == 1);
  check("G3: returned within wait_ms + one sweep", ms < 20.0 + 1500.0);
  mi_debug_stall_in_thread_theaps_done.store(0);   // release
  thread_join(w.thread);
  const int rc2 = purge((mi_purge_flags_t)0, 2000, &r, &ms);
  print_report("G3 retry after release", rc2, &r, ms);
  check("G3: retry after the hook releases -> OK", rc2 == MI_PURGE_OK && r.theaps_pending == 0);
  fprintf(stderr, "  G3: used the MI_DEBUG stall hook (mi_debug_stall_in_thread_theaps_done)\n");
#else
  // fallback (see the file comment): a worker blocked inside its deferred-free handler
  fprintf(stderr, "  G3: no MI_DEBUG stall hook in this build; using the G6 construction (worker blocked inside its handler)\n");
  worker_t w;
  g_in_handler.store(0);
  mutex_lock(&g_mutex);
  w.phase.store(HOOK_BLOCK_ON_MUTEX);
  if (!thread_start(&w.thread, &hook_worker, &w)) { mutex_unlock(&g_mutex); check("G3: start worker", false); return; }
  for (int i = 0; i < 20000 && g_in_handler.load() == 0; i++) sleep_ms(1);
  const int rc1 = purge((mi_purge_flags_t)0, 20, &r, &ms);
  print_report("G3 _ex(0, 20ms) with a worker blocked inside its handler", rc1, &r, ms);
  check("G3: the owner inside the allocator is reported pending (PARTIAL, pending == 1)", g_in_handler.load() != 0 && rc1 == MI_PURGE_PARTIAL && r.theaps_pending == 1);
  check("G3: returned within wait_ms + one sweep", ms < 20.0 + 1500.0);
  mutex_unlock(&g_mutex);
#if MI_OWNER_GATE
  sleep_ms(20);
  const int rc2 = purge((mi_purge_flags_t)0, 2000, &r, &ms);
  print_report("G3 retry with the worker running again", rc2, &r, ms);
  check("G3 (gated): retry after release -> OK", rc2 == MI_PURGE_OK && r.theaps_pending == 0);
  w.stop.store(1); thread_join(w.thread);
#else
  w.stop.store(1); thread_join(w.thread);
  const int rc2 = purge((mi_purge_flags_t)0, 2000, &r, &ms);
  print_report("G3 retry after the worker exited", rc2, &r, ms);
  check("G3 (ungated): retry once the owner is gone -> OK", rc2 == MI_PURGE_OK && r.theaps_pending == 0);
#endif
#endif
}

// ---------------------------------------------------------------------------------------------
// G4  registry churn: purge in a loop while 4 threads churn and 2 spawn/allocate/exit
// ---------------------------------------------------------------------------------------------

static void test_g4_registry_churn(void) {
  enum { NC = 4, NS = 2 };
  worker_t churners[NC], spawners[NS];
  for (int i = 0; i < NC; i++) if (!thread_start(&churners[i].thread, &churn_loop, &churners[i])) { check("G4: start", false); return; }
  for (int i = 0; i < NS; i++) if (!thread_start(&spawners[i].thread, &spawner, &spawners[i])) { check("G4: start", false); return; }
  int calls = 0, busy = 0, ok = 0, partial = 0;
  double worst = 0;
  const double t_end = now_ms() + 1500.0;
  while (now_ms() < t_end) {
    mi_purge_all_report_t r; double ms = 0;
    const int rc = purge(MI_PURGE_FORCE, 100, &r, &ms);
    calls++;
    if (ms > worst) worst = ms;
    if (rc == MI_PURGE_BUSY) busy++; else if (rc == MI_PURGE_OK) ok++; else partial++;
  }
  for (int i = 0; i < NC; i++) churners[i].stop.store(1);
  for (int i = 0; i < NS; i++) spawners[i].stop.store(1);
  for (int i = 0; i < NC; i++) thread_join(churners[i].thread);
  for (int i = 0; i < NS; i++) thread_join(spawners[i].thread);
  long spawned = 0; for (int i = 0; i < NS; i++) spawned += spawners[i].iterations.load();
  fprintf(stderr, "  G4: %d purges (%d OK, %d PARTIAL, %d BUSY), worst call %.1f ms, %ld threads born and gone meanwhile\n",
          calls, ok, partial, busy, worst, spawned);
  check("G4: every call terminated and none was BUSY", calls > 0 && busy == 0);
  check("G4: the worst call stayed far under the ctest timeout", worst < 10000.0);
  check("G4: threads really churned through the registry", spawned > 0);
}

// ---------------------------------------------------------------------------------------------
// G5  two callers simultaneously: exactly one BUSY, one OK; admission released on both exits
// ---------------------------------------------------------------------------------------------

static void test_g5_two_callers(void) {
  // main holds the admission for a known window: its own phase-C handler waits for the peer
  worker_t peer;
  g_in_handler.store(0); g_peer_done.store(0);
  if (!thread_start(&g_peer_thread, &peer_caller, &peer)) { check("G5: start peer", false); return; }
  mi_purge_all_report_t r; double ms = 0;
  tl_mode = HOOK_WAIT_FOR_PEER;
  const int rc_main = purge(MI_PURGE_FORCE, 2000, &r, &ms);
  tl_mode = HOOK_NONE;
  const int rc_peer = peer.phase.load() - 100;
  print_report("G5 main (held the admission while the peer called)", rc_main, &r, ms);
  fprintf(stderr, "  G5: peer's concurrent call returned %s in %.1f ms\n", status_name(rc_peer), peer.max_call_ms);
  check("G5: exactly one caller was BUSY (the peer) and the other OK (main)", rc_peer == MI_PURGE_BUSY && rc_main == MI_PURGE_OK);
  check("G5: the BUSY caller returned at once", peer.max_call_ms < 100.0);
  const int rc_again = purge(MI_PURGE_FORCE, 1000, &r, &ms);
  check("G5: admission released on both exits", rc_again != MI_PURGE_BUSY);
}

// ---------------------------------------------------------------------------------------------
// G6  callback blocked on the caller's mutex: PARTIAL, pending == 1, bounded; retry -> OK
// ---------------------------------------------------------------------------------------------

static void test_g6_callback_blocked(void) {
  worker_t w;
  g_in_handler.store(0);
  mutex_lock(&g_mutex);
  w.phase.store(HOOK_BLOCK_ON_MUTEX);
  if (!thread_start(&w.thread, &hook_worker, &w)) { mutex_unlock(&g_mutex); check("G6: start worker", false); return; }
  for (int i = 0; i < 20000 && g_in_handler.load() == 0; i++) sleep_ms(1);
  mi_purge_all_report_t r; double ms = 0;
  const int rc1 = purge(MI_PURGE_FORCE, 50, &r, &ms);
  print_report("G6 _ex(FORCE, 50ms) with the worker blocked on main's mutex", rc1, &r, ms);
  check("G6: PARTIAL with the blocked worker pending", g_in_handler.load() != 0 && rc1 == MI_PURGE_PARTIAL && r.theaps_pending == 1);
  check("G6: returned within wait_ms + one sweep", ms < 50.0 + 1500.0);
  mutex_unlock(&g_mutex);
#if MI_OWNER_GATE
  sleep_ms(20);
  const int rc2 = purge(MI_PURGE_FORCE, 2000, &r, &ms);
  print_report("G6 retry after main released the mutex", rc2, &r, ms);
  check("G6 (gated): retry -> OK", rc2 == MI_PURGE_OK && r.theaps_pending == 0);
  w.stop.store(1); thread_join(w.thread);
#else
  w.stop.store(1); thread_join(w.thread);
  const int rc2 = purge(MI_PURGE_FORCE, 2000, &r, &ms);
  print_report("G6 retry after the worker exited", rc2, &r, ms);
  check("G6 (ungated): retry once the owner is gone -> OK", rc2 == MI_PURGE_OK && r.theaps_pending == 0);
#endif
}

// ---------------------------------------------------------------------------------------------
// G7  starvation: a worker re-enters the allocator in a tight loop with no work between calls
// ---------------------------------------------------------------------------------------------

static void test_g7_starvation(void) {
  worker_t w;
  w.prestamp = true; w.keep_holes = true;
  if (!thread_start(&w.thread, &churn_worker, &w)) { check("G7: start worker", false); return; }
  wait_phase(&w, 1, 30000);
  mi_purge_all_report_t r; double ms = 0;
  const int rc = purge(MI_PURGE_FORCE, 200, &r, &ms);
  w.stop.store(1); thread_join(w.thread);
  print_report("G7 _ex(FORCE, 200ms) against a tight re-entering loop", rc, &r, ms);
  check("G7: the claimant either got the owner or reported it -- never BUSY", rc == MI_PURGE_OK || (rc == MI_PURGE_PARTIAL && r.theaps_pending == 1));
  check("G7: never spins past the deadline (wait_ms + one sweep)", ms < 200.0 + 1500.0);
#if MI_OWNER_GATE
  fprintf(stderr, "  G7 (gated): claimed within the window: %s\n", rc == MI_PURGE_OK ? "yes" : "no (reported pending)");
#endif
}

// ---------------------------------------------------------------------------------------------
// G8  scavenger pacing: interval 50 ms, busy workers, nobody calls anything
// ---------------------------------------------------------------------------------------------

static void test_g8_scavenger_pacing(void) {
  enum { N = 4 };
  const bool scavenger = mi_option_is_enabled(mi_option_scavenger);
  pin_interval(50);
  worker_t w[N];
  for (int i = 0; i < N; i++) {
    w[i].keep_holes = true; w[i].big_frees = true; w[i].spin_ms = 20;   // CPU-busy, in the allocator every 20 ms
    if (!thread_start(&w[i].thread, &churn_worker, &w[i])) { check("G8: start", false); return; }
  }
  for (int i = 0; i < N; i++) wait_phase(&w[i], 1, 30000);
  const size_t c0 = _mi_test_idle_work_count();
  const double t0 = now_ms();
  size_t c1 = c0;
  const bool expect_advance = gated && scavenger;
  if (expect_advance) {
    // every worker once (a fresh tld's first sweep is unpaced), and at least one of them
    // AGAIN -- the paced loop coming back is what this row is about
    while (now_ms() - t0 < 3000.0 && (c1 = _mi_test_idle_work_count()) < c0 + N + 1) sleep_ms(5);
  }
  else {
    sleep_ms(500);
    c1 = _mi_test_idle_work_count();
  }
  const double waited = now_ms() - t0;
  for (int i = 0; i < N; i++) w[i].stop.store(1);
  for (int i = 0; i < N; i++) thread_join(w[i].thread);
  fprintf(stderr, "  G8: scavenger=%s gated=%d: idle-work passes %zu -> %zu over %.0f ms with busy workers and no caller\n",
          scavenger ? "on" : "off", (int)gated, c0, c1, waited);
  if (expect_advance) check("G8 (gated, scavenger on): the paced timed sweep reaches busy threads, repeatedly", c1 >= c0 + N + 1);
  else if (!scavenger)  check("G8 (-no-scavenger): nothing sweeps the busy threads (count flat)", c1 == c0);
  else                  check("G8 (ungated, scavenger on): RUNNING owners are never swept (count flat)", c1 == c0);
  pin_interval(3600000);
}

// ---------------------------------------------------------------------------------------------
// C1  coverage: the allocation paths, each exercised continuously and raced by a claimant
// ---------------------------------------------------------------------------------------------

static void test_c1_coverage(void) {
  worker_t small, heap, aligned, mixed, spawn;
  g_output_swallow.store(1); g_output_allocate.store(1);
  g_output_allocs.store(0); g_errors_seen.store(0);
  mi_option_set_enabled(mi_option_verbose, true);   // the oversized-request error is printed unconditionally
  bool started = true;
  started = thread_start(&small.thread, &c1_small, &small) && started;
  started = thread_start(&heap.thread, &c1_heap, &heap) && started;
  started = thread_start(&aligned.thread, &c1_aligned, &aligned) && started;
  started = thread_start(&mixed.thread, &c1_mixed, &mixed) && started;
  started = thread_start(&spawn.thread, &c1_spawner, &spawn) && started;
  int calls = 0, busy = 0; double worst = 0;
  const double t_end = now_ms() + 1500.0;
  while (started && now_ms() < t_end) {
    mi_purge_all_report_t r; double ms = 0;
    const int rc = purge(MI_PURGE_FORCE, 20, &r, &ms);
    calls++; if (rc == MI_PURGE_BUSY) busy++; if (ms > worst) worst = ms;
  }
  small.stop.store(1); heap.stop.store(1); aligned.stop.store(1); mixed.stop.store(1); spawn.stop.store(1);
  if (started) { thread_join(small.thread); thread_join(heap.thread); thread_join(aligned.thread); thread_join(mixed.thread); thread_join(spawn.thread); }
  mi_option_set_enabled(mi_option_verbose, false);
  g_output_allocate.store(0); g_output_swallow.store(0);
  fprintf(stderr, "  C1: %d claims (%d BUSY), worst %.1f ms; small %ld, heap %ld, aligned %ld, mixed %ld iterations; %ld first-call threads; "
                  "%zu error messages, %zu allocations from inside the output hook\n",
          calls, busy, worst, small.iterations.load(), heap.iterations.load(), aligned.iterations.load(), mixed.iterations.load(),
          spawn.iterations.load(), g_errors_seen.load(), g_output_allocs.load());
  check("C1: workers started", started);
  check("C1: every path made progress under a claimant loop", small.iterations.load() > 0 && heap.iterations.load() > 0 && aligned.iterations.load() > 0 && mixed.iterations.load() > 0 && spawn.iterations.load() > 0);
  check("C1: the output hook recursed into the allocator on a thread's first allocator call", g_errors_seen.load() > 0 && g_output_allocs.load() > 0);
  check("C1: no claim was BUSY and each returned promptly", busy == 0 && worst < 5000.0);
}

// ---------------------------------------------------------------------------------------------
// C2  foreign collect with the owner spinning to enter; the purge caller's output hook allocates
// ---------------------------------------------------------------------------------------------

static void test_c2_foreign_collect(void) {
  worker_t w;
  w.prestamp = true; w.keep_holes = true;
  if (!thread_start(&w.thread, &churn_worker, &w)) { check("C2: start worker", false); return; }
  wait_phase(&w, 1, 30000);
  g_output_swallow.store(1); g_output_allocate.store(1);
  mi_option_set_enabled(mi_option_verbose, true);
  const long it0 = w.iterations.load();
  mi_purge_all_report_t r; double ms = 0;
  const int rc = purge(MI_PURGE_FORCE, 2000, &r, &ms);
  // the owner must keep going after the sweep hands its theaps back
  const long it1 = w.iterations.load();
  sleep_ms(50);
  const long it2 = w.iterations.load();
  mi_option_set_enabled(mi_option_verbose, false);
  g_output_allocate.store(0); g_output_swallow.store(0);
  w.stop.store(1); thread_join(w.thread);
  print_report("C2 _ex(FORCE, 2000ms) against a spinning owner", rc, &r, ms);
  fprintf(stderr, "  C2: owner iterations %ld -> %ld (during) -> %ld (50ms after); survivors %s\n", it0, it1, it2, w.corrupt ? "CORRUPT" : "intact");
  check("C2: progress -- the purge returned and was not BUSY", rc != MI_PURGE_BUSY && ms < 2000.0 + 1500.0);
  check("C2: the owner allocates again after the sweep", it2 > it1);
  check("C2: the owner's survivors are intact across the foreign sweep", !w.corrupt);
#if MI_OWNER_GATE
  fprintf(stderr, "  C2 (gated): owner %s\n", (r.theaps_swept >= 2 && r.theaps_pending == 0) ? "swept" : "reported pending (starved)");
#endif
}

// ---------------------------------------------------------------------------------------------

// `test-purge-all [ROW ...]` runs only the named rows (T1 always: the others need R_ref);
// a debugging convenience, ctest runs everything.
static int    g_argc = 0;
static char** g_argv = NULL;
static bool row_selected(const char* id) {
  if (g_argc <= 1) return true;
  for (int i = 1; i < g_argc; i++) { if (strcmp(g_argv[i], id) == 0) return true; }
  return false;
}
#define RUN_ROW(id, fn) do { if (row_selected(id)) { fn(); } else { fprintf(stderr, "test: %s skipped (not selected)\n", id); } } while (0)

int main(int argc, char** argv) {
  g_argc = argc; g_argv = argv;
  mutex_init(&g_mutex);
  mi_register_deferred_free(&deferred_free_hook, NULL);
  mi_register_output(&output_hook, NULL);
  mi_register_error(&error_hook, NULL);
  interval_min_ms = mi_option_get(mi_option_purge_holes_min_interval);
  fprintf(stderr, "test-purge-all: gated=%d scavenger=%s purge_holes=%s min_interval=%ldms page=%zu keep_every=%zu\n",
          (int)gated, mi_option_is_enabled(mi_option_scavenger) ? "on" : "off",
          mi_option_is_enabled(mi_option_purge_holes) ? "on" : "off", interval_min_ms, os_page_size(), keep_every());
  if (!mi_option_is_enabled(mi_option_purge_holes)) {
    fprintf(stderr, "test-purge-all: hole purging is disabled (MIMALLOC_PURGE_HOLES=0); this test needs it\n");
    return 1;
  }
  // stamp main's own `holes_sweep_last` (an empty sweep) and pin the pacing to its maximum:
  // see the file comment on the scavenger as a confound in a gated build
  { void* q = mi_malloc(64); mi_free(q); mi_on_thread_idle(); }
  pin_interval(3600000);

  test_t1_reference();
  RUN_ROW("T2", test_t2_abandoned);
  RUN_ROW("T3", test_t3_reentrancy);
  RUN_ROW("G1", test_g1_g2_busy_owners);
  RUN_ROW("G3", test_g3_owner_inside);
  RUN_ROW("G4", test_g4_registry_churn);
  RUN_ROW("G5", test_g5_two_callers);
  RUN_ROW("G6", test_g6_callback_blocked);
  RUN_ROW("G7", test_g7_starvation);
  RUN_ROW("G8", test_g8_scavenger_pacing);
  RUN_ROW("C1", test_c1_coverage);
  RUN_ROW("C2", test_c2_foreign_collect);

  pin_interval(interval_min_ms);
  mi_register_deferred_free(NULL, NULL);
  mutex_destroy(&g_mutex);
  fprintf(stderr, "test-purge-all: %d failure(s), %d deferred-free handler calls\n", failures, g_handler_calls.load());
  if (failures != 0) { fprintf(stderr, "test-purge-all: FAILED\n"); return 1; }
  printf("ok: test-purge-all (gated=%d)\n", (int)gated);
  return 0;
}
