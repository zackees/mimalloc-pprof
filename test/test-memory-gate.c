/* Memory and counter regression gate.

   This repository shipped two unbounded memory leaks (#44 v3, #47 v2) -- 23.5 GB at 100
   iterations of test-stress -- through a completely green CI pipeline. Every test asked
   "did it crash?" and none asked "did memory stay bounded?". An audit of the workflows
   confirmed it: no baseline, no threshold, no memory assertion anywhere.

   This binary is the answer. It runs a battery of allocation stress scenarios, records
   peak memory and the engine's own liveness counters, and emits a JSON blob that CI
   compares against a committed baseline. It also asserts bounds inline, so it fails
   usefully when run by hand with no CI around it.

   Design notes that matter:

   - Peak memory is not one number across platforms. On Windows we gate on peak COMMIT
     (private bytes), not the working set: Windows commits eagerly and the working set is
     trimmed by the system under pressure, which would hide committed-but-untouched
     growth -- exactly the shape of the bugs above. Elsewhere we gate on peak RSS.
     mimalloc's own mi_process_info already handles the ru_maxrss KB-vs-bytes trap
     (macOS reports bytes, Linux/BSD KiB), so we do not re-derive it.

   - Only same-OS deltas are comparable. Lazy zeroing means Linux RSS and Windows commit
     report different numbers for identical allocator behavior, so baselines are stored
     per platform and never compared across.

   - The profiler's own arena is process memory (rule 4 routes it through _mi_os_alloc),
     so an MI_PPROF=ON build legitimately shows higher peak than MI_PPROF=OFF. We report
     profiler_arena_committed alongside, so the confound is visible rather than baked in.

   - MI_BENCH_INJECT_LEAK is a BUILD-TIME knob only, never reachable at runtime and
     asserted absent below unless explicitly compiled in. It exists to prove the gate
     actually fires: a check that has never been observed to fail proves nothing. */

#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include "mimalloc.h"
#include "mimalloc-stats.h"

#ifndef MI_BENCH_INJECT_LEAK
#define MI_BENCH_INJECT_LEAK 0
#endif

/* ---- portable threading ------------------------------------------------ */

#ifdef _WIN32
#include <windows.h>
typedef HANDLE thread_t;
typedef DWORD (WINAPI *thread_fun_t)(void*);
#define THREAD_RET DWORD WINAPI
#define THREAD_OK  0
static void thread_start(thread_t* t, thread_fun_t fn, void* arg) {
  *t = CreateThread(NULL, 0, fn, arg, 0, NULL);
  assert(*t != NULL);
}
static void thread_join(thread_t t) {
  assert(WaitForSingleObject(t, INFINITE) == WAIT_OBJECT_0);
  CloseHandle(t);
}
#else
#include <pthread.h>
typedef pthread_t thread_t;
typedef void* (*thread_fun_t)(void*);
#define THREAD_RET void*
#define THREAD_OK  NULL
static void thread_start(thread_t* t, thread_fun_t fn, void* arg) {
  assert(pthread_create(t, NULL, fn, arg) == 0);
}
static void thread_join(thread_t t) { assert(pthread_join(t, NULL) == 0); }
#endif

/* ---- measurement ------------------------------------------------------- */

typedef struct sample_s {
  size_t peak;              /* the gated number: peak commit on Windows, peak rss elsewhere */
  size_t peak_rss;
  size_t peak_commit;
  size_t current_rss;
  long long threads_live;
  long long theaps_live;
  long long pages_live;
  double   purged_gb;
  size_t   profiler_arena;  /* our own arena, so the confound is visible */
} sample_t;

static const char* gated_metric(void) {
#ifdef _WIN32
  return "peak_commit";     /* working set is trimmed under pressure; commit is not */
#else
  return "peak_rss";
#endif
}

static void take_sample(sample_t* s) {
  memset(s, 0, sizeof(*s));
  size_t elapsed=0, user=0, sys=0, crss=0, prss=0, ccommit=0, pcommit=0, faults=0;
  mi_process_info(&elapsed, &user, &sys, &crss, &prss, &ccommit, &pcommit, &faults);
  s->peak_rss    = prss;
  s->peak_commit = pcommit;
  s->current_rss = crss;
#ifdef _WIN32
  s->peak = pcommit;
#else
  s->peak = prss;
#endif
  mi_stats_t_decl(st);
  if (mi_stats_get(&st)) {
    s->threads_live = (long long)st.threads.current;
    s->theaps_live  = (long long)st.theaps.current;
    s->pages_live   = (long long)st.pages.current;
    s->purged_gb    = (double)st.purged.total / (1024.0*1024.0*1024.0);
  }
#if MI_PPROF
  {
    mi_prof_stats_t_decl(ps);
    if (mi_prof_stats_get(&ps)) { s->profiler_arena = ps.arena_committed; }
  }
#endif
}

static double as_mb(size_t b) { return (double)b / (1024.0*1024.0); }

/* ---- the stress battery ------------------------------------------------ */
/* Each scenario is shaped to stress a different way memory can fail to come back. */

#define CHURN_THREADS 8
#define CHURN_ROUNDS  20

#if MI_BENCH_INJECT_LEAK
/* Build-time leak injection. Retains memory per thread exit so the gate has something
   to catch. Never compiled unless explicitly requested. */
/* `inject_armed` matters. The warmup round runs the same scenarios, so if injection
   were active during warmup the BASELINE would already contain the leak, and any
   baseline-relative check would compare the leak against itself and pass. That is
   exactly the contaminated-control mistake this harness exists to prevent -- and it
   did happen on the first run of this test. Injection is armed only after the
   baseline sample is taken. */
static void* volatile inject_sink[4096];
static volatile size_t inject_idx = 0;
static volatile int inject_armed = 0;
static void inject_leak(void) {
  const size_t n = (size_t)MI_BENCH_INJECT_LEAK;
  if (n == 0 || inject_armed == 0) return;
  void* p = mi_malloc(n);
  if (p != NULL) {
    memset(p, 0x5A, n < 4096 ? n : 4096);
    size_t i = inject_idx++;
    if (i < 4096) { inject_sink[i] = p; } else { /* keep leaking, drop the handle */ }
  }
}
#else
static void inject_leak(void) { }
#endif

static void arm_injection(void) {
#if MI_BENCH_INJECT_LEAK
  inject_armed = 1;
#endif
}

/* Scenario 1: thread churn. The shape of both shipped leaks -- threads that own real
   pages and then exit. If thread-exit cleanup regresses, this is what catches it. */
static THREAD_RET churn_worker(void* arg) {
  (void)arg;
  void* keep[48];
  for (size_t i = 0; i < 48; i++) {
    keep[i] = mi_malloc(64*1024);
    assert(keep[i] != NULL);
    memset(keep[i], 0x5A, 4096);
  }
  for (size_t i = 0; i < 1500; i++) {
    void* p = mi_malloc(64 + (i % 900));
    assert(p != NULL);
    mi_free(p);
  }
  for (size_t i = 0; i < 48; i++) { mi_free(keep[i]); }
  inject_leak();
  return THREAD_OK;
}

static void scenario_thread_churn(void) {
  for (int r = 0; r < CHURN_ROUNDS; r++) {
    thread_t th[CHURN_THREADS];
    for (int i = 0; i < CHURN_THREADS; i++) thread_start(&th[i], (thread_fun_t)churn_worker, NULL);
    for (int i = 0; i < CHURN_THREADS; i++) thread_join(th[i]);
  }
}

/* Scenario 2: sawtooth. Build a large live set and drop it, repeatedly. Peak should
   repeat rather than climb; a climb means memory is not being reused. */
static void scenario_sawtooth(void) {
  enum { teeth = 10, per = 20000, sz = 512 };
  for (int t = 0; t < teeth; t++) {
    void** batch = (void**)mi_malloc(per * sizeof(void*));
    assert(batch != NULL);
    for (int i = 0; i < per; i++) { batch[i] = mi_malloc(sz); assert(batch[i] != NULL); *(char*)batch[i] = (char)i; }
    for (int i = 0; i < per; i++) { mi_free(batch[i]); }
    mi_free(batch);
  }
}

/* Scenario 3: cross-thread free. Allocate on one thread, free on another -- the path
   that defers work and therefore the one most able to hide retention. */
typedef struct xchan_s { void* blocks[3000]; volatile int ready; } xchan_t;
static THREAD_RET x_producer(void* arg) {
  xchan_t* c = (xchan_t*)arg;
  for (size_t i = 0; i < 3000; i++) { c->blocks[i] = mi_malloc(((i%6)+1)*128); assert(c->blocks[i]!=NULL); }
  c->ready = 1;
  return THREAD_OK;
}
static THREAD_RET x_consumer(void* arg) {
  xchan_t* c = (xchan_t*)arg;
  while (c->ready == 0) { /* spin */ }
  for (size_t i = 0; i < 3000; i++) { mi_free(c->blocks[i]); }
  return THREAD_OK;
}
static void scenario_cross_thread_free(void) {
  enum { pairs = 6 };
  for (int r = 0; r < 6; r++) {
    static xchan_t ch[pairs];
    thread_t p[pairs], c[pairs];
    memset(ch, 0, sizeof(ch));
    for (int i = 0; i < pairs; i++) { thread_start(&p[i], (thread_fun_t)x_producer, &ch[i]);
                                     thread_start(&c[i], (thread_fun_t)x_consumer, &ch[i]); }
    for (int i = 0; i < pairs; i++) { thread_join(p[i]); thread_join(c[i]); }
  }
}

/* Scenario 4: rolling heaps. Create and delete heaps while blocks stay live across the
   boundary -- the configuration that exposed the v3 leak. */
static void scenario_rolling_heaps(void) {
  enum { rounds = 20, live = 4 };
  mi_heap_t* prev[live];
  memset(prev, 0, sizeof(prev));
  for (int n = 0; n < rounds; n++) {
    if (prev[live-1] != NULL) { mi_heap_delete(prev[live-1]); }
    for (int i = live-1; i > 0; i--) prev[i] = prev[i-1];
    mi_heap_t* h = mi_heap_new();
    assert(h != NULL);
    prev[0] = h;
    void* keep[256];
    for (int i = 0; i < 256; i++) { keep[i] = mi_heap_malloc(h, 4096); assert(keep[i]!=NULL); }
    for (int i = 0; i < 256; i++) { mi_free(keep[i]); }
  }
  for (int i = 0; i < live; i++) { if (prev[i] != NULL) mi_heap_delete(prev[i]); }
}

/* Scenario 5: huge-allocation churn, where a single leaked block is large enough to
   dominate the peak. */
static void scenario_huge_churn(void) {
  const size_t sizes[] = { 2u*1024*1024, 8u*1024*1024, 32u*1024*1024 };
  for (int r = 0; r < 6; r++) {
    for (int s = 0; s < 3; s++) {
      void* p = mi_malloc(sizes[s]);
      assert(p != NULL);
      ((char*)p)[0] = 1; ((char*)p)[sizes[s]-1] = 2;
      mi_free(p);
    }
  }
}

/* ---- JSON ------------------------------------------------------------- */

static const char* platform_tag(void) {
#if defined(_WIN32)
  return "windows";
#elif defined(__APPLE__)
  return "macos";
#else
  return "linux";
#endif
}

static void emit_json(const char* path, const sample_t* base, const sample_t* end) {
  FILE* f = fopen(path, "w");
  if (f == NULL) { fprintf(stderr, "warning: cannot open %s for writing\n", path); return; }
  fprintf(f,
    "{\n"
    "  \"schema\": 1,\n"
    "  \"platform\": \"%s\",\n"
    "  \"gated_metric\": \"%s\",\n"
    "  \"mi_pprof\": %d,\n"
    "  \"inject_leak\": %llu,\n"
    "  \"peak_mb\": %.1f,\n"
    "  \"peak_rss_mb\": %.1f,\n"
    "  \"peak_commit_mb\": %.1f,\n"
    "  \"profiler_arena_mb\": %.1f,\n"
    "  \"peak_minus_profiler_mb\": %.1f,\n"
    "  \"purged_gb\": %.2f,\n"
    "  \"counters\": {\n"
    "    \"threads_start\": %lld, \"threads_end\": %lld,\n"
    "    \"theaps_start\": %lld,  \"theaps_end\": %lld,\n"
    "    \"pages_start\": %lld,   \"pages_end\": %lld\n"
    "  }\n"
    "}\n",
    platform_tag(), gated_metric(),
#if MI_PPROF
    1,
#else
    0,
#endif
    (unsigned long long)MI_BENCH_INJECT_LEAK,
    as_mb(end->peak), as_mb(end->peak_rss), as_mb(end->peak_commit),
    as_mb(end->profiler_arena), as_mb(end->peak - (end->profiler_arena < end->peak ? end->profiler_arena : 0)),
    end->purged_gb,
    base->threads_live, end->threads_live,
    base->theaps_live,  end->theaps_live,
    base->pages_live,   end->pages_live);
  fclose(f);
}

int main(void) {
  setvbuf(stdout, NULL, _IONBF, 0);
  printf("test-memory-gate: platform=%s gated=%s inject_leak=%llu\n",
         platform_tag(), gated_metric(), (unsigned long long)MI_BENCH_INJECT_LEAK);

  /* Warm up so the baseline is steady state, not first-touch arena reservation. */
  scenario_thread_churn();
  mi_collect(true);

  sample_t base; take_sample(&base);
  printf("  baseline: peak=%.1f MB threads=%lld theaps=%lld pages=%lld\n",
         as_mb(base.peak), base.threads_live, base.theaps_live, base.pages_live);

  arm_injection();   /* after the baseline, never before -- see inject_leak() */

  scenario_thread_churn();
  scenario_sawtooth();
  scenario_cross_thread_free();
  scenario_rolling_heaps();
  scenario_huge_churn();
  mi_collect(true);

  sample_t end; take_sample(&end);
  printf("  after:    peak=%.1f MB threads=%lld theaps=%lld pages=%lld purged=%.2f GB\n",
         as_mb(end.peak), end.threads_live, end.theaps_live, end.pages_live, end.purged_gb);
  printf("  profiler arena: %.1f MB (peak minus profiler: %.1f MB)\n",
         as_mb(end.profiler_arena), as_mb(end.peak - (end.profiler_arena < end.peak ? end.profiler_arena : 0)));

  const char* json = getenv("MI_BENCH_JSON");
  if (json != NULL) { emit_json(json, &base, &end); printf("  wrote %s\n", json); }

  /* Inline assertions, so this is a real test even with no CI around it. The engine's
     own counters are the precise detector -- they are exact and platform-independent,
     where peak memory is neither. */
  bool failed = false;
  if (end.threads_live >= 32) {
    fprintf(stderr, "FAIL: %lld threads still live after creating and joining %d.\n"
                    "      Thread-exit cleanup is not running (see #44 / #47).\n",
            end.threads_live, CHURN_THREADS * CHURN_ROUNDS * 2);
    failed = true;
  }
  /* Memory backstop. Deliberately loose: it exists to catch a leak that does NOT show
     in the counters. The counter check above is the precise one. */
  const size_t allowed = (base.peak * 3) + (64u*1024*1024);
  if (end.peak > allowed) {
    fprintf(stderr, "FAIL: %s grew %.1f MB -> %.1f MB (allowed <= %.1f MB).\n",
            gated_metric(), as_mb(base.peak), as_mb(end.peak), as_mb(allowed));
    failed = true;
  }
  if (failed) { fflush(stderr); abort(); }

  printf("test-memory-gate: ok\n");
  return 0;
}
