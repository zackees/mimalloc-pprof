/* Tests for the opt-in global-heap allocation-change accounting/callback API (issue #20).
   See include/mimalloc/memory-events.h for the full contract this exercises. Follows this
   codebase's test-profile.c style: numbered T-series functions, assert()-based, a single
   main() driver calling each in sequence. */
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include "mimalloc.h"
#include "mimalloc/memory-events.h"

/* ---------------------------------------------------------------------------------------------
   Shared callback-counting harness: one handler function for all three kinds, dispatching on
   change->kind, writing into a caller-owned evt_ctx_t. */

typedef struct evt_ctx_s {
  int                    counts[MI_MEMORY_CHANGE_COUNT];
  mi_memory_change_t     last[MI_MEMORY_CHANGE_COUNT];
} evt_ctx_t;

static void evt_ctx_reset(evt_ctx_t* ctx) { memset(ctx, 0, sizeof(*ctx)); }

static void on_change(const mi_memory_change_t* change, void* arg) {
  evt_ctx_t* ctx = (evt_ctx_t*)arg;
  ctx->counts[change->kind]++;
  ctx->last[change->kind] = *change;
}

static void evt_install(evt_ctx_t* ctx) {
  mi_memory_callbacks_t cbs;
  for (int i = 0; i < MI_MEMORY_CHANGE_COUNT; i++) { cbs.handlers[i] = on_change; cbs.args[i] = ctx; }
  assert(mi_memory_set_callbacks(&cbs));
}

/* ---- T1: disabled by default -------------------------------------------------------------
   Must run first, before any explicit mi_memory_tracking_set_enabled call, so the module's
   activation state is still MEMEVT_UNINIT / resolves lazily via the (unset) env var. */
static void test_disabled_by_default(void) {
  assert(!mi_memory_tracking_is_enabled());
  evt_ctx_t ctx; evt_ctx_reset(&ctx);
  evt_install(&ctx);
  enum { count = 500 };
  void* blocks[count];
  for (size_t i = 0; i < count; i++) { blocks[i] = mi_malloc(32 + (i % 64)); assert(blocks[i] != NULL); }
  for (size_t i = 0; i < count; i++) mi_free(blocks[i]);
  /* Also exercise a realloc while disabled. */
  void* p = mi_malloc(16); assert(p != NULL);
  void* p2 = mi_realloc(p, 4096); assert(p2 != NULL);
  mi_free(p2);
  assert(!mi_memory_tracking_is_enabled());
  assert(ctx.counts[MI_MEMORY_ALLOCATE] == 0);
  assert(ctx.counts[MI_MEMORY_FREE] == 0);
  assert(ctx.counts[MI_MEMORY_RESIZE] == 0);
  assert(mi_memory_set_callbacks(NULL));
}

/* ---- T3: explicit API enable/disable, both before and after allocation; API is
   authoritative even though the lazy env-read already resolved the once (in T1). */
static void test_explicit_enable_disable(void) {
  assert(!mi_memory_tracking_is_enabled());
  assert(mi_memory_tracking_set_enabled(true));
  assert(mi_memory_tracking_is_enabled());

  evt_ctx_t ctx; evt_ctx_reset(&ctx);
  evt_install(&ctx);

  void* p = mi_malloc(48); assert(p != NULL);
  assert(ctx.counts[MI_MEMORY_ALLOCATE] == 1);

  assert(mi_memory_tracking_set_enabled(false));
  assert(!mi_memory_tracking_is_enabled());
  evt_ctx_reset(&ctx);
  void* q = mi_malloc(48); assert(q != NULL);
  assert(ctx.counts[MI_MEMORY_ALLOCATE] == 0);  /* disabled: no accounting, no dispatch */

  mi_free(p);
  mi_free(q);

  /* Re-enable and leave enabled for the rest of the test suite. */
  assert(mi_memory_tracking_set_enabled(true));
  assert(mi_memory_tracking_is_enabled());
  assert(mi_memory_set_callbacks(NULL));
}

/* ---- T4: callback registration/replacement/clearing after tracking is already enabled. */
static void test_callback_registration_timing(void) {
  assert(mi_memory_tracking_is_enabled());

  evt_ctx_t ctx1; evt_ctx_reset(&ctx1);
  evt_install(&ctx1);
  void* p = mi_malloc(32); assert(p != NULL);
  assert(ctx1.counts[MI_MEMORY_ALLOCATE] == 1);
  mi_free(p);
  assert(ctx1.counts[MI_MEMORY_FREE] == 1);

  /* Replace with a different table -- old ctx1 must stop receiving events. */
  evt_ctx_t ctx2; evt_ctx_reset(&ctx2);
  evt_install(&ctx2);
  evt_ctx_reset(&ctx1);
  void* q = mi_malloc(32); assert(q != NULL);
  mi_free(q);
  assert(ctx1.counts[MI_MEMORY_ALLOCATE] == 0 && ctx1.counts[MI_MEMORY_FREE] == 0);
  assert(ctx2.counts[MI_MEMORY_ALLOCATE] == 1 && ctx2.counts[MI_MEMORY_FREE] == 1);

  /* Clear -- delivery stops entirely. */
  assert(mi_memory_set_callbacks(NULL));
  evt_ctx_reset(&ctx2);
  void* r = mi_malloc(32); assert(r != NULL);
  mi_free(r);
  assert(ctx2.counts[MI_MEMORY_ALLOCATE] == 0 && ctx2.counts[MI_MEMORY_FREE] == 0);
}

/* ---- T5/T6: event correctness (ALLOCATE/FREE/RESIZE shapes) and failed-op/double-free
   no-event guarantees. */
static void test_event_correctness(void) {
  assert(mi_memory_tracking_is_enabled());
  evt_ctx_t ctx; evt_ctx_reset(&ctx);
  evt_install(&ctx);

  /* T5a: ALLOCATE -- correct kind/delta/total/request_size.
     Note: delta_bytes is deliberately NOT compared against the public mi_usable_size(p)
     here. The accounting hooks use mi_page_usable_block_size (the internal "size class"
     usable size, excluding the fixed padding header) while mi_usable_size(p) -- when
     MI_PADDING/MI_PADDING_CHECK are compiled in (default for MI_DEBUG>=1 debug builds) --
     decodes the padding canary and returns something closer to the originally *requested*
     size instead. The two are only guaranteed equal in a non-padded (release-style,
     MI_DEBUG==0) build. So this test treats the event's own reported delta as ground
     truth and cross-checks it against itself (T5b below) and against mi_memory_snapshot,
     rather than against mi_usable_size(). */
  evt_ctx_reset(&ctx);
  void* p = mi_malloc(100); assert(p != NULL);
  assert(ctx.counts[MI_MEMORY_ALLOCATE] == 1);
  assert(ctx.counts[MI_MEMORY_FREE] == 0 && ctx.counts[MI_MEMORY_RESIZE] == 0);
  int64_t p_delta = 0;
  {
    const mi_memory_change_t* c = &ctx.last[MI_MEMORY_ALLOCATE];
    assert(c->kind == MI_MEMORY_ALLOCATE);
    assert(c->request_size == 100);
    assert(c->delta_bytes >= 100);  /* usable size is always >= the requested size */
    p_delta = c->delta_bytes;
    mi_memory_snapshot_t_decl(snap);
    assert(mi_memory_snapshot(&snap));
    assert(c->total_bytes == snap.live_bytes);
  }

  /* T5b: FREE -- correct kind/negative delta/total; the freed delta must exactly negate
     the allocate delta for the same block (same page, no resize in between). */
  evt_ctx_reset(&ctx);
  mi_free(p);
  assert(ctx.counts[MI_MEMORY_FREE] == 1);
  assert(ctx.counts[MI_MEMORY_ALLOCATE] == 0 && ctx.counts[MI_MEMORY_RESIZE] == 0);
  {
    const mi_memory_change_t* c = &ctx.last[MI_MEMORY_FREE];
    assert(c->kind == MI_MEMORY_FREE);
    assert(c->request_size == 0);
    assert(c->delta_bytes == -p_delta);
    mi_memory_snapshot_t_decl(snap);
    assert(mi_memory_snapshot(&snap));
    assert(c->total_bytes == snap.live_bytes);
  }

  /* T5c: grow-realloc forced to move (far past the current size class) -- exactly one
     RESIZE, zero net ALLOCATE/FREE from this call. */
  {
    void* a = mi_malloc(8); assert(a != NULL);
    evt_ctx_reset(&ctx);
    void* a2 = mi_realloc(a, 1024 * 1024); assert(a2 != NULL);
    assert(ctx.counts[MI_MEMORY_RESIZE] == 1);
    assert(ctx.counts[MI_MEMORY_ALLOCATE] == 0 && ctx.counts[MI_MEMORY_FREE] == 0);
    const mi_memory_change_t* c = &ctx.last[MI_MEMORY_RESIZE];
    assert(c->kind == MI_MEMORY_RESIZE);
    assert(c->request_size == 1024 * 1024);
    assert(c->delta_bytes > 0);
    mi_free(a2);
  }

  /* T5d: shrink-realloc forced to move (shrink past the in-place half-waste threshold) --
     exactly one RESIZE with a negative delta, zero net ALLOCATE/FREE. */
  {
    void* a = mi_malloc(1024 * 1024); assert(a != NULL);
    evt_ctx_reset(&ctx);
    void* a2 = mi_realloc(a, 8); assert(a2 != NULL);
    assert(ctx.counts[MI_MEMORY_RESIZE] == 1);
    assert(ctx.counts[MI_MEMORY_ALLOCATE] == 0 && ctx.counts[MI_MEMORY_FREE] == 0);
    const mi_memory_change_t* c = &ctx.last[MI_MEMORY_RESIZE];
    assert(c->kind == MI_MEMORY_RESIZE);
    assert(c->request_size == 8);
    assert(c->delta_bytes < 0);
    mi_free(a2);
  }

  /* T5e: same-size-class realloc (in-place path) -- delta must be exactly 0, but a RESIZE
     event still fires. */
  {
    void* a = mi_malloc(100); assert(a != NULL);
    const size_t u = mi_usable_size(a);
    evt_ctx_reset(&ctx);
    void* a2 = mi_realloc(a, u);  /* newsize == usable size: satisfies the in-place condition */
    assert(a2 == a);              /* in-place realloc must return the same pointer */
    assert(ctx.counts[MI_MEMORY_RESIZE] == 1);
    assert(ctx.counts[MI_MEMORY_ALLOCATE] == 0 && ctx.counts[MI_MEMORY_FREE] == 0);
    const mi_memory_change_t* c = &ctx.last[MI_MEMORY_RESIZE];
    assert(c->kind == MI_MEMORY_RESIZE);
    assert(c->request_size == u);
    assert(c->delta_bytes == 0);
    mi_free(a2);
  }

  /* T6: failed allocation emits no event. */
  {
    evt_ctx_reset(&ctx);
    void* huge = mi_malloc(SIZE_MAX / 2);
    assert(huge == NULL);
    assert(ctx.counts[MI_MEMORY_ALLOCATE] == 0);
  }

  /* T6: failed realloc emits no event (and the original pointer remains valid/untouched). */
  {
    void* a = mi_malloc(16); assert(a != NULL);
    evt_ctx_reset(&ctx);
    void* a2 = mi_realloc(a, SIZE_MAX / 2);
    assert(a2 == NULL);
    assert(ctx.counts[MI_MEMORY_RESIZE] == 0 && ctx.counts[MI_MEMORY_ALLOCATE] == 0 && ctx.counts[MI_MEMORY_FREE] == 0);
    mi_free(a);  /* a must still be valid per the realloc(3) failure contract */
  }

  /* T6: double free emits no event. Only reachable when the double-free check is actually
     compiled in (MI_DEBUG!=0 or MI_SECURE>=4 -- see src/free.c's mi_check_is_double_free);
     in a Release-style build (this file's MI_DEBUG is undefined/0 and MI_SECURE<4 by
     default) the check is entirely compiled out and a real double free is undefined
     behavior, so this sub-case is skipped in that configuration rather than risking a
     crash/corruption. Build with -DCMAKE_BUILD_TYPE=Debug (or -DMI_DEBUG=ON) to exercise it. */
#if defined(MI_DEBUG) && (MI_DEBUG > 0)
  {
    void* a = mi_malloc(64); assert(a != NULL);
    mi_free(a);
    evt_ctx_reset(&ctx);
    mi_free(a);  /* double free: mi_check_is_double_free must detect and no-op before the
                    _mi_memevt_on_free hook is ever reached. _mi_error_message(EAGAIN, ...)
                    does not abort (mi_error_default only aborts on EFAULT/ENOMEM/EOVERFLOW
                    depending on build mode; EAGAIN is a silent-by-default diagnostic here). */
    assert(ctx.counts[MI_MEMORY_FREE] == 0);
  }
#else
  fprintf(stderr, "test-memory-events: skipping double-free sub-case (MI_DEBUG not enabled in this build)\n");
#endif

  assert(mi_memory_set_callbacks(NULL));
}

/* ---- T7: concurrency -- self-consistent live/accum counters, no underflow, no crash.
   Follows test-profile.c's portable CreateThread/pthread_create pattern.

   Correctness is checked via a callback that tallies events into per-thread counters
   (each worker thread only ever touches its own slot -- no atomics needed), rather than
   via a before/after mi_memory_snapshot() diff. This matters specifically on Linux: this
   test binary links mimalloc-static, which statically overrides the process's malloc/free
   (see CMakeLists' MI_MALLOC_OVERRIDE), so glibc/pthread's own internal bookkeeping
   allocations (e.g. per-thread stack/TCB housekeeping done by pthread_create/pthread_join,
   observed empirically as ~320-byte blocks allocated and only partially freed -- within
   the measurement window -- on the *calling* thread, consistent with glibc's dead-stack
   cache deferring some frees) are also captured by these hooks. A raw global snapshot
   diff conflates that unrelated libc-internal traffic with the test's own allocations and
   is not a reliable equality check once malloc is globally overridden; scoping the count
   to only events whose delta magnitude matches this test's own known block-size class
   (t7_usable) sidesteps that entirely and keeps the check to its actual intent: verifying
   the accounting hooks never lose or duplicate an event under concurrent hot-path access. */
enum { T7_THREADS = 8, T7_PER_THREAD = 2000 };
static size_t t7_block_size = 96;
static size_t t7_usable;

#if defined(_MSC_VER)
#define T7_THREAD_LOCAL __declspec(thread)
#else
#define T7_THREAD_LOCAL __thread
#endif

static T7_THREAD_LOCAL uint64_t t7_local_alloc_count;
static T7_THREAD_LOCAL uint64_t t7_local_alloc_bytes;
static T7_THREAD_LOCAL uint64_t t7_local_free_count;
static T7_THREAD_LOCAL uint64_t t7_local_free_bytes;

typedef struct t7_result_s { uint64_t alloc_count, alloc_bytes, free_count, free_bytes; } t7_result_t;
static t7_result_t t7_results[T7_THREADS];

static void t7_change_handler(const mi_memory_change_t* change, void* arg) {
  (void)arg;
  if (change->kind == MI_MEMORY_ALLOCATE && (uint64_t)change->delta_bytes == (uint64_t)t7_usable) {
    t7_local_alloc_count++;
    t7_local_alloc_bytes += (uint64_t)change->delta_bytes;
  }
  else if (change->kind == MI_MEMORY_FREE && (uint64_t)(-change->delta_bytes) == (uint64_t)t7_usable) {
    t7_local_free_count++;
    t7_local_free_bytes += (uint64_t)(-change->delta_bytes);
  }
}

static void t7_worker(int idx) {
  t7_local_alloc_count = t7_local_alloc_bytes = t7_local_free_count = t7_local_free_bytes = 0;
  for (int i = 0; i < T7_PER_THREAD; i++) {
    void* p = mi_malloc(t7_block_size);
    assert(p != NULL);
    mi_free(p);
  }
  t7_results[idx].alloc_count = t7_local_alloc_count;
  t7_results[idx].alloc_bytes = t7_local_alloc_bytes;
  t7_results[idx].free_count  = t7_local_free_count;
  t7_results[idx].free_bytes  = t7_local_free_bytes;
}

#ifdef _WIN32
#include <windows.h>
static DWORD WINAPI t7_thread(void* arg) { t7_worker((int)(intptr_t)arg); return 0; }
static void t7_run_threads(void) {
  HANDLE threads[T7_THREADS];
  for (int i = 0; i < T7_THREADS; i++) threads[i] = CreateThread(NULL, 0, t7_thread, (void*)(intptr_t)i, 0, NULL);
  WaitForMultipleObjects(T7_THREADS, threads, TRUE, INFINITE);
  for (int i = 0; i < T7_THREADS; i++) CloseHandle(threads[i]);
}
#else
#include <pthread.h>
static void* t7_thread(void* arg) { t7_worker((int)(intptr_t)arg); return NULL; }
static void t7_run_threads(void) {
  pthread_t threads[T7_THREADS];
  for (int i = 0; i < T7_THREADS; i++) assert(pthread_create(&threads[i], NULL, t7_thread, (void*)(intptr_t)i) == 0);
  for (int i = 0; i < T7_THREADS; i++) assert(pthread_join(threads[i], NULL) == 0);
}
#endif

static void test_concurrency(void) {
  assert(mi_memory_tracking_is_enabled());
  /* Probe the per-block accounting delta for the fixed block size every worker thread
     uses. Captured from the ALLOCATE event's own delta_bytes (not mi_usable_size(p) --
     see the T5a comment above on why those two can differ in a padded/debug build) so
     the expectation below matches exactly what the accounting hooks themselves compute. */
  evt_ctx_t probe_ctx; evt_ctx_reset(&probe_ctx);
  evt_install(&probe_ctx);
  void* probe = mi_malloc(t7_block_size); assert(probe != NULL);
  assert(probe_ctx.counts[MI_MEMORY_ALLOCATE] == 1);
  t7_usable = (size_t)probe_ctx.last[MI_MEMORY_ALLOCATE].delta_bytes;
  mi_free(probe);

  memset(t7_results, 0, sizeof(t7_results));
  mi_memory_callbacks_t cbs;
  memset(&cbs, 0, sizeof(cbs));
  cbs.handlers[MI_MEMORY_ALLOCATE] = t7_change_handler;
  cbs.handlers[MI_MEMORY_FREE] = t7_change_handler;
  assert(mi_memory_set_callbacks(&cbs));

  mi_memory_snapshot_t_decl(before);
  assert(mi_memory_snapshot(&before));

  t7_run_threads();
  mi_collect(true);  /* drain delayed cross-thread frees before checking counters */

  mi_memory_snapshot_t_decl(after);
  assert(mi_memory_snapshot(&after));
  assert(mi_memory_set_callbacks(NULL));

  /* No underflow: unsigned/atomic subtraction gone wrong would wrap to a huge value. */
  assert(after.live_bytes < ((uint64_t)1 << 48));
  assert(after.live_count < ((uint64_t)1 << 48));

  /* Every thread's allocations were paired locally (alloc then free): sum the per-thread
     tallies (scoped to this test's own block-size class, see the comment above) and check
     against the exact serially-computed expectation -- no lost or duplicated events. */
  uint64_t total_alloc_count = 0, total_alloc_bytes = 0, total_free_count = 0, total_free_bytes = 0;
  for (int i = 0; i < T7_THREADS; i++) {
    total_alloc_count += t7_results[i].alloc_count;
    total_alloc_bytes += t7_results[i].alloc_bytes;
    total_free_count  += t7_results[i].free_count;
    total_free_bytes  += t7_results[i].free_bytes;
  }
  assert(total_alloc_count == (uint64_t)T7_THREADS * T7_PER_THREAD);
  assert(total_free_count == (uint64_t)T7_THREADS * T7_PER_THREAD);
  assert(total_alloc_bytes == (uint64_t)T7_THREADS * T7_PER_THREAD * t7_usable);
  assert(total_free_bytes == (uint64_t)T7_THREADS * T7_PER_THREAD * t7_usable);
}

/* ---- T8: a callback may call mi_malloc/mi_free without deadlocking (no allocator locks
   held while the handler runs). The reentrant inner alloc/free must itself be suppressed
   (not double-dispatched) per the documented reentrancy-guard semantics. */
typedef struct t8_ctx_s { int outer_calls; bool completed; } t8_ctx_t;
static void t8_reentrant_handler(const mi_memory_change_t* change, void* arg) {
  (void)change;
  t8_ctx_t* ctx = (t8_ctx_t*)arg;
  ctx->outer_calls++;
  void* tmp = mi_malloc(16);
  assert(tmp != NULL);
  mi_free(tmp);
  ctx->completed = true;
}

static void test_reentrant_callback(void) {
  assert(mi_memory_tracking_is_enabled());
  t8_ctx_t ctx = { 0, false };
  mi_memory_callbacks_t cbs;
  memset(&cbs, 0, sizeof(cbs));
  cbs.handlers[MI_MEMORY_ALLOCATE] = t8_reentrant_handler;
  cbs.args[MI_MEMORY_ALLOCATE] = &ctx;
  assert(mi_memory_set_callbacks(&cbs));

  void* p = mi_malloc(64); assert(p != NULL);
  assert(ctx.completed);
  assert(ctx.outer_calls == 1);  /* the inner mi_malloc/mi_free must not have re-dispatched */
  mi_free(p);

  assert(mi_memory_set_callbacks(NULL));
}

/* ---- T9: snapshot accuracy across an uninterrupted enabled session -- allocate/free a
   known pattern and check the four counters against hand-computed deltas exactly. */
static void test_snapshot_accuracy(void) {
  assert(mi_memory_tracking_is_enabled());

  evt_ctx_t ctx; evt_ctx_reset(&ctx);
  evt_install(&ctx);

  mi_memory_snapshot_t_decl(before);
  assert(mi_memory_snapshot(&before));

  /* Hand-computed expectation is derived from the ALLOCATE events' own delta_bytes (not
     mi_usable_size(p) -- see the T5a comment for why those can differ in a padded/debug
     build), so this test is checking snapshot self-consistency against the accounting
     hooks' own reported deltas, exactly. */
  enum { N = 50 };
  void* blocks[N];
  size_t usable = 0;
  for (int i = 0; i < N; i++) {
    evt_ctx_reset(&ctx);
    blocks[i] = mi_malloc(200);
    assert(blocks[i] != NULL);
    assert(ctx.counts[MI_MEMORY_ALLOCATE] == 1);
    const size_t u = (size_t)ctx.last[MI_MEMORY_ALLOCATE].delta_bytes;
    if (i == 0) usable = u; else assert(u == usable);  /* same request => same size class */
  }

  mi_memory_snapshot_t_decl(mid);
  assert(mi_memory_snapshot(&mid));
  assert(mid.live_count == before.live_count + N);
  assert(mid.live_bytes == before.live_bytes + (uint64_t)N * usable);
  assert(mid.accum_count == before.accum_count + N);
  assert(mid.accum_bytes == before.accum_bytes + (uint64_t)N * usable);

  for (int i = 0; i < N / 2; i++) mi_free(blocks[i]);

  mi_memory_snapshot_t_decl(after);
  assert(mi_memory_snapshot(&after));
  assert(after.live_count == mid.live_count - N / 2);
  assert(after.live_bytes == mid.live_bytes - (uint64_t)(N / 2) * usable);
  assert(after.accum_count == mid.accum_count);   /* frees never advance accum_* */
  assert(after.accum_bytes == mid.accum_bytes);

  for (int i = N / 2; i < N; i++) mi_free(blocks[i]);

  assert(mi_memory_set_callbacks(NULL));  /* ctx is about to go out of scope */
}

/* ---- T10: runtime disable/re-enable -- allocations made while disabled are never counted,
   even after re-enabling (no reconstruction of the disabled interval). Free the untracked
   allocation while still disabled too, so the disabled interval nets to zero and later
   delta-based assertions elsewhere in this file stay valid. */
static void test_disable_reenable_partial(void) {
  assert(mi_memory_tracking_set_enabled(true));

  evt_ctx_t ctx; evt_ctx_reset(&ctx);
  evt_install(&ctx);

  mi_memory_snapshot_t_decl(s0);
  assert(mi_memory_snapshot(&s0));

  evt_ctx_reset(&ctx);
  void* p1 = mi_malloc(64); assert(p1 != NULL);
  assert(ctx.counts[MI_MEMORY_ALLOCATE] == 1);
  const size_t u1 = (size_t)ctx.last[MI_MEMORY_ALLOCATE].delta_bytes;

  mi_memory_snapshot_t_decl(s1);
  assert(mi_memory_snapshot(&s1));
  assert(s1.live_count == s0.live_count + 1);
  assert(s1.live_bytes == s0.live_bytes + u1);
  assert(s1.accum_count == s0.accum_count + 1);
  assert(s1.accum_bytes == s0.accum_bytes + u1);

  assert(mi_memory_tracking_set_enabled(false));
  void* p2 = mi_malloc(64); assert(p2 != NULL);  /* allocated while disabled: NOT counted */
  mi_free(p2);                                    /* freed while still disabled: NOT counted either */

  mi_memory_snapshot_t_decl(s2);
  assert(mi_memory_snapshot(&s2));
  assert(s2.live_count == s1.live_count);
  assert(s2.live_bytes == s1.live_bytes);
  assert(s2.accum_count == s1.accum_count);
  assert(s2.accum_bytes == s1.accum_bytes);

  assert(mi_memory_tracking_set_enabled(true));
  evt_ctx_reset(&ctx);
  void* p3 = mi_malloc(64); assert(p3 != NULL);  /* resumes counting fresh, from where it left off */
  assert(ctx.counts[MI_MEMORY_ALLOCATE] == 1);
  const size_t u3 = (size_t)ctx.last[MI_MEMORY_ALLOCATE].delta_bytes;

  mi_memory_snapshot_t_decl(s3);
  assert(mi_memory_snapshot(&s3));
  assert(s3.live_count == s2.live_count + 1);
  assert(s3.live_bytes == s2.live_bytes + u3);
  assert(s3.accum_count == s2.accum_count + 1);
  assert(s3.accum_bytes == s2.accum_bytes + u3);

  mi_free(p1);
  mi_free(p3);
  assert(mi_memory_set_callbacks(NULL));
}

/* ---- T11: mi_memory_visit_live_allocations -- known blocks observed with correct
   usable_size, early-stop via returning false, freed blocks never reported. */
enum { T11_N = 20 };
typedef struct t11_track_s { void* addr; size_t want_size; bool seen; } t11_track_t;
static t11_track_t t11_tracked[T11_N];

static bool t11_full_visitor(void* allocation, size_t usable_size, void* arg) {
  (void)arg;
  for (int i = 0; i < T11_N; i++) {
    if (t11_tracked[i].addr == allocation) {
      assert(usable_size == t11_tracked[i].want_size);
      t11_tracked[i].seen = true;
      break;
    }
  }
  return true;
}

static int t11_stop_calls;
static bool t11_stop_visitor(void* allocation, size_t usable_size, void* arg) {
  (void)allocation; (void)usable_size; (void)arg;
  t11_stop_calls++;
  return false;
}

static bool t11_freed_visitor(void* allocation, size_t usable_size, void* arg) {
  (void)usable_size;
  bool* found_freed = (bool*)arg;
  for (int i = 0; i < T11_N / 2; i++) {
    if (t11_tracked[i].addr == allocation) *found_freed = true;
  }
  return true;
}

static void test_visit_live_allocations(void) {
  /* mi_heap_visit_blocks (which the visitor is built on) reports mi_page_usable_block_size,
     the same internal usable-size notion the memory-change hooks use -- not the public,
     possibly-padding-adjusted mi_usable_size(p) (see the T5a comment). Capture the expected
     size the same way, from an ALLOCATE event's own delta_bytes, so this comparison is
     apples-to-apples regardless of build configuration. */
  assert(mi_memory_tracking_is_enabled());
  evt_ctx_t ctx; evt_ctx_reset(&ctx);
  evt_install(&ctx);
  for (int i = 0; i < T11_N; i++) {
    evt_ctx_reset(&ctx);
    void* p = mi_malloc(64 + (size_t)i * 8);
    assert(p != NULL);
    assert(ctx.counts[MI_MEMORY_ALLOCATE] == 1);
    t11_tracked[i].addr = p;
    t11_tracked[i].want_size = (size_t)ctx.last[MI_MEMORY_ALLOCATE].delta_bytes;
    t11_tracked[i].seen = false;
  }
  assert(mi_memory_set_callbacks(NULL));

  assert(mi_memory_visit_live_allocations(t11_full_visitor, NULL));
  for (int i = 0; i < T11_N; i++) assert(t11_tracked[i].seen);

  /* Early stop: the visitor returns false after its first invocation. */
  t11_stop_calls = 0;
  assert(mi_memory_visit_live_allocations(t11_stop_visitor, NULL));
  assert(t11_stop_calls == 1);

  /* Free the first half, then confirm the visitor never reports them again. */
  for (int i = 0; i < T11_N / 2; i++) { mi_free(t11_tracked[i].addr); }
  bool found_freed = false;
  assert(mi_memory_visit_live_allocations(t11_freed_visitor, &found_freed));
  assert(!found_freed);

  for (int i = T11_N / 2; i < T11_N; i++) mi_free(t11_tracked[i].addr);
}

/* ---- T12: mi_unwrapped_malloc/_free/_realloc -- round trip, data preservation, and
   confirmation that these do NOT trigger memory-change events even while tracking is
   enabled. Also exercises the magic-mismatch error path (mi_unwrapped_free on a regular
   mi_malloc pointer) -- confirmed safe because _mi_error_message(EINVAL, ...) is a
   non-fatal diagnostic in this build (mi_error_default only aborts for EFAULT under
   MI_DEBUG/MI_SECURE, or ENOMEM/EOVERFLOW under MI_XMALLOC -- none of which apply to
   EINVAL). Not done: mixing the families the other direction (mi_free on an unwrapped
   pointer) -- header explicitly documents that as forbidden/UB-adjacent territory beyond
   the one safe, diagnostic-only direction exercised here. */
static void test_unwrapped_family(void) {
  assert(mi_memory_tracking_is_enabled());
  evt_ctx_t ctx; evt_ctx_reset(&ctx);
  evt_install(&ctx);

  void* p = mi_unwrapped_malloc(64, 16);
  assert(p != NULL);
  assert(((uintptr_t)p % 16) == 0);
  memset(p, 0xAB, 64);

  void* p2 = mi_unwrapped_realloc(p, 256, 16);
  assert(p2 != NULL);
  for (int i = 0; i < 64; i++) assert(((unsigned char*)p2)[i] == 0xAB);
  memset((unsigned char*)p2 + 64, 0xCD, 256 - 64);

  void* p3 = mi_unwrapped_realloc(p2, 32, 16);
  assert(p3 != NULL);
  for (int i = 0; i < 32; i++) assert(((unsigned char*)p3)[i] == 0xAB);

  mi_unwrapped_free(p3);

  assert(ctx.counts[MI_MEMORY_ALLOCATE] == 0);
  assert(ctx.counts[MI_MEMORY_FREE] == 0);
  assert(ctx.counts[MI_MEMORY_RESIZE] == 0);

  /* Safe error-path check: magic-number mismatch, not a crash. Note q itself is a regular
     mi_malloc allocation, so it IS tracked normally -- only the failed mi_unwrapped_free
     call on it must emit nothing. */
  evt_ctx_reset(&ctx);
  void* q = mi_malloc(16); assert(q != NULL);
  assert(ctx.counts[MI_MEMORY_ALLOCATE] == 1);

  evt_ctx_reset(&ctx);
  mi_unwrapped_free(q);  /* expected to fail its magic check and print a diagnostic; must not abort */
  assert(ctx.counts[MI_MEMORY_ALLOCATE] == 0);
  assert(ctx.counts[MI_MEMORY_FREE] == 0);
  assert(ctx.counts[MI_MEMORY_RESIZE] == 0);

  mi_free(q);  /* q was never touched by the failed call above; free it properly */
  assert(ctx.counts[MI_MEMORY_FREE] == 1);

  assert(mi_memory_set_callbacks(NULL));
}

/* ---- Env-var lazy activation (acceptance criterion #2) ------------------------------------
   MIMALLOC_MEMORY_EVENTS=1 is read lazily, exactly once, on the first allocation hook. This
   cannot be exercised by toggling the env var mid-process (the once-guard only ever resolves
   once), so this is driven as a *separate process*: this same test binary re-invoked with
   argv[1] == "--env-enabled-check" and MIMALLOC_MEMORY_EVENTS=1 set, registered as its own
   CTest case (mirrors this repo's existing test-profile-auto pattern of re-running the same
   binary with a CTest-level ENVIRONMENT property). */
static int run_env_enabled_check(void) {
  if (getenv("MIMALLOC_MEMORY_EVENTS") == NULL) {
    fprintf(stderr, "test-memory-events --env-enabled-check: MIMALLOC_MEMORY_EVENTS not set in environment\n");
    return 1;
  }
  /* Nothing must have touched the allocator yet in this fresh process; the very first
     allocation hook below is what triggers the lazy env resolution. */
  void* p = mi_malloc(64);
  if (p == NULL) { fprintf(stderr, "alloc failed\n"); return 2; }
  if (!mi_memory_tracking_is_enabled()) { fprintf(stderr, "tracking not enabled after lazy env read\n"); mi_free(p); return 3; }

  evt_ctx_t ctx; evt_ctx_reset(&ctx);
  evt_install(&ctx);
  void* q = mi_malloc(32);
  if (q == NULL) { fprintf(stderr, "second alloc failed\n"); mi_free(p); return 4; }
  if (ctx.counts[MI_MEMORY_ALLOCATE] != 1) { fprintf(stderr, "callback did not fire after lazy env activation\n"); mi_free(p); mi_free(q); return 5; }

  mi_free(p);
  mi_free(q);
  return 0;
}

int main(int argc, char** argv) {
  if (argc > 1 && strcmp(argv[1], "--env-enabled-check") == 0) {
    return run_env_enabled_check();
  }

  test_disabled_by_default();
  test_explicit_enable_disable();
  test_callback_registration_timing();
  test_event_correctness();
  test_concurrency();
  test_reentrant_callback();
  test_snapshot_accuracy();
  test_disable_reenable_partial();
  test_visit_live_allocations();
  test_unwrapped_family();

  puts("memory-events tests passed");
  return 0;
}
