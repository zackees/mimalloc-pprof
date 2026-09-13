// #374: deterministic ownership regressions for diagnostic heap walks.
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "mimalloc.h"
#include "mimalloc-stats.h"
#include "mimalloc/internal.h"
#include "mimalloc/prim.h"
#include "mimalloc/prim-tls.h"
#include "mimalloc/memory-events.h"

#ifdef _WIN32
#include <windows.h>
typedef HANDLE thread_t;
#define THREAD_RET DWORD WINAPI
#define THREAD_OK 0
static void thread_start(thread_t* t, LPTHREAD_START_ROUTINE fn, void* arg) {
  *t = CreateThread(NULL, 0, fn, arg, 0, NULL);
  assert(*t != NULL);
}
static void thread_join(thread_t t) {
  DWORD code = 1;
  assert(WaitForSingleObject(t, INFINITE) == WAIT_OBJECT_0);
  assert(GetExitCodeThread(t, &code) && code == 0);
  CloseHandle(t);
}
#else
#include <pthread.h>
typedef pthread_t thread_t;
#define THREAD_RET void*
#define THREAD_OK NULL
static void thread_start(thread_t* t, void* (*fn)(void*), void* arg) {
  assert(pthread_create(t, NULL, fn, arg) == 0);
}
static void thread_join(thread_t t) { assert(pthread_join(t, NULL) == 0); }
#endif

#if MI_OWNER_GATE && MI_DEBUG > 0
static _Atomic(uintptr_t) report_phase;
static mi_tld_t* reporter_tld;

static THREAD_RET report_worker(void* arg) {
  MI_UNUSED(arg);
  void* keep = mi_malloc(1024);
  assert(keep != NULL);
  reporter_tld = _mi_theap_default()->tld;
  mi_atomic_store_release(&mi_debug_stall_in_holes_report, (uintptr_t)1);
  mi_holes_report_t rep;
  _mi_purge_holes_report_collect(&rep);
  mi_atomic_store_release(&report_phase, (uintptr_t)1);
  while (mi_atomic_load_acquire(&report_phase) == 1) { _mi_prim_thread_yield(); }
  mi_free(keep);
  return THREAD_OK;
}

static void test_report_gate(void) {
  thread_t worker;
  thread_start(&worker, &report_worker, NULL);
  while (mi_atomic_load_acquire(&mi_debug_stall_in_holes_report) != 2) { _mi_prim_thread_yield(); }
  // RED before the fix: the reporter is PARKED at its first free-list walk.
  assert(mi_atomic_load_acquire(&reporter_tld->park_state) == MI_PARK_RUNNING);
  mi_purge_all_report_t during;
  (void)mi_purge_all_ex(MI_PURGE_FORCE, 0, &during);
  assert(during.theaps_pending > 0);
  mi_atomic_store_release(&mi_debug_stall_in_holes_report, (uintptr_t)0);
  while (mi_atomic_load_acquire(&report_phase) != 1) { _mi_prim_thread_yield(); }
  assert(mi_atomic_load_acquire(&reporter_tld->park_state) == MI_PARK_PARKED);
  mi_purge_all_report_t after;
  (void)mi_purge_all_ex(MI_PURGE_FORCE, 0, &after);
  // The same actual purge must now reach the reporter; not a vacuous no-sweeper test.
  assert(after.theaps_pending == 0 && after.theaps_swept >= 2);
  mi_atomic_store_release(&report_phase, (uintptr_t)2);
  thread_join(worker);
  puts("report-gate: excluded while reporting, foreign sweep completed afterward");
}
#endif

static _Atomic(uintptr_t) dump_phase;
static mi_heap_t* dump_heap;

static THREAD_RET dump_owner(void* arg) {
  MI_UNUSED(arg);
  mi_thread_init();
  mi_theap_t* self = _mi_theap_default();
  MI_GATE_ENTER(self);
  void* keep[32];
  for (size_t i = 0; i < 32; i++) {
    keep[i] = mi_heap_malloc(dump_heap, 1024);
    assert(keep[i] != NULL);
  }
  mi_atomic_store_release(&dump_phase, (uintptr_t)1);
  while (mi_atomic_load_acquire(&dump_phase) == 1) { _mi_prim_thread_yield(); }
  MI_GATE_LEAVE(self->tld);
  MI_UNUSED(self);
  // Synthetic cooperative park for deterministic tests with the scavenger OFF.
  // The public idle_start deliberately declines handoff without a live scavenger.
  // No parked_count increment: pair this with the gate-form acquire below.
  mi_atomic_store_release(&self->tld->park_state, (size_t)MI_PARK_PARKED);
  mi_atomic_store_release(&dump_phase, (uintptr_t)3);
  while (mi_atomic_load_acquire(&dump_phase) == 3) { _mi_prim_thread_yield(); }
  _mi_park_leave_gate(self->tld);
  for (size_t i = 0; i < 32; i++) { mi_free(keep[i]); }
  return THREAD_OK;
}

static void test_dump_coverage(void) {
  dump_heap = mi_heap_new();
  assert(dump_heap != NULL);
  thread_t worker;
  thread_start(&worker, &dump_owner, NULL);
  while (mi_atomic_load_acquire(&dump_phase) != 1) { _mi_prim_thread_yield(); }
  char* busy = mi_heap_dump_json(true, false);
  assert(busy != NULL);
  // RED on the old unsafe RUNNING fallback, even when that race happens not to crash.
  assert(strstr(busy, "\"complete\": false") != NULL);
  mi_free(busy);
  mi_atomic_store_release(&dump_phase, (uintptr_t)2);
  while (mi_atomic_load_acquire(&dump_phase) != 3) { _mi_prim_thread_yield(); }
  char* parked = mi_heap_dump_json(true, false);
  assert(parked != NULL);
  if (strstr(parked, "\"complete\": true") == NULL) {
    const size_t len = strlen(parked);
    fprintf(stderr, "parked dump tail: %s\n", parked + (len > 256 ? len - 256 : 0));
  }
  assert(strstr(parked, "\"complete\": true") != NULL);
  char needle[64];
  snprintf(needle, sizeof(needle), "\"seq\": %zu,", mi_heap_get_seq(dump_heap));
  const char* begin = strstr(parked, needle);
  assert(begin != NULL);
  const char* end = strstr(begin + strlen(needle), "\"seq\":");
  if (end == NULL) end = parked + strlen(parked);
  size_t total_used = 0;
  for (const char* p = begin; (p = strstr(p, "\"used\": ")) != NULL && p < end; p++) {
    size_t used = 0;
    assert(sscanf(p, "\"used\": %zu", &used) == 1);
    total_used += used;
  }
  assert(total_used == 32);
  mi_free(parked);
  mi_atomic_store_release(&dump_phase, (uintptr_t)4);
  thread_join(worker);
  mi_heap_destroy(dump_heap);
  puts("dump-coverage: busy owner reported, cooperative owner captured");
}

static size_t dump_resizes;
static void on_dump_resize(const mi_memory_change_t* change, void* arg) {
  MI_UNUSED(change); MI_UNUSED(arg);
  dump_resizes++;
}

static void test_dump_growth(void) {
  // Hundreds of kilobytes of JSON: exercise many raw-OS scratch chunks and the
  // partial-page bitmap path that asserted in the plain Rust unit test (#374).
  enum { N = 40000 };
  void** blocks = (void**)mi_unwrapped_malloc(N * sizeof(void*), 0);
  assert(blocks != NULL);
  for (size_t i = 0; i < N; i++) { blocks[i] = mi_malloc(32); assert(blocks[i] != NULL); }
  mi_memory_callbacks_t callbacks = { { NULL }, { NULL } };
  callbacks.handlers[MI_MEMORY_RESIZE] = &on_dump_resize;
  assert(mi_memory_set_callbacks(&callbacks));
  assert(mi_memory_tracking_set_enabled(true));
  dump_resizes = 0;
  char* json = mi_heap_dump_json(true, false);
  assert(json != NULL && strlen(json) > 500000);
  assert(dump_resizes == 0); // the old mi_rezalloc-backed callback fails this
  assert(mi_memory_set_callbacks(NULL));
  assert(mi_memory_tracking_set_enabled(false));
  mi_free(json);
  for (size_t i = 0; i < N; i++) mi_free(blocks[i]);
  mi_unwrapped_free(blocks);
  puts("dump-growth: large capture, no hooked buffer resize");
}

enum { ABANDONED_N = 70 };
static void* abandoned_blocks[ABANDONED_N];
static THREAD_RET abandon_owner(void* arg) {
  mi_heap_t* heap = (mi_heap_t*)arg;
  for (size_t i = 0; i < ABANDONED_N; i++) {
    // Singleton pages: >64 pages exercises more than one abandoned OS batch.
    abandoned_blocks[i] = mi_heap_malloc(heap, MI_MiB);
    assert(abandoned_blocks[i] != NULL);
    assert(_mi_ptr_page(abandoned_blocks[i])->capacity == 1);
  }
  // Natural thread-exit teardown abandons these pages before the join returns.
  return THREAD_OK;
}

static void test_dump_abandoned(void) {
  mi_heap_t* heap = mi_heap_new();
  assert(heap != NULL);
  thread_t worker;
  thread_start(&worker, &abandon_owner, heap);
  thread_join(worker);
  char* json = mi_heap_dump_json(true, false);
  assert(json != NULL && strstr(json, "\"complete\": true") != NULL);
  for (size_t i = 0; i < ABANDONED_N; i++) {
    char needle[64];
    // Guarded user pointers are offset from the reported block base.
    snprintf(needle, sizeof(needle), "[%zu,", (size_t)mi_page_start(_mi_ptr_page(abandoned_blocks[i])));
    assert(strstr(json, needle) != NULL);
  }
  mi_free(json);
  #if MI_DEBUG > 0
  // Under forced OS allocation, request 3 fails after the first 64 abandoned
  // pages were claimed. All earlier claims must be released even on that path.
  for (uintptr_t fail = 1; fail <= 12; fail++) {
    mi_atomic_store_relaxed(&mi_debug_dump_fail_after, fail);
    assert(mi_heap_dump_json(true, false) == NULL);
    mi_atomic_store_relaxed(&mi_debug_dump_fail_after, (uintptr_t)0);
    json = mi_heap_dump_json(true, false);
    assert(json != NULL && strstr(json, "\"complete\": true") != NULL);
    mi_free(json);
  }
  mi_atomic_store_relaxed(&mi_debug_dump_fail_after, UINTPTR_MAX);
  assert(mi_heap_dump_json(true, false) == NULL);
  mi_atomic_store_relaxed(&mi_debug_dump_fail_after, (uintptr_t)0);
  #endif
  // A leaked claim makes these cross-thread frees or heap teardown hang.
  for (size_t i = 0; i < ABANDONED_N; i++) mi_free(abandoned_blocks[i]);
  mi_heap_destroy(heap);
  puts("dump-abandoned: all 70 pages captured; capture/serialization OOM cleanup");
}

typedef struct many_owner_s {
  _Atomic(uintptr_t) phase;
  void* block;
} many_owner_t;

static THREAD_RET many_owner(void* arg) {
  many_owner_t* owner = (many_owner_t*)arg;
  owner->block = mi_malloc(128);
  assert(owner->block != NULL);
  mi_tld_t* tld = _mi_theap_default()->tld;
  mi_atomic_store_release(&tld->park_state, (size_t)MI_PARK_PARKED);
  mi_atomic_store_release(&owner->phase, (uintptr_t)1);
  while (mi_atomic_load_acquire(&owner->phase) == 1) _mi_prim_thread_yield();
  _mi_park_leave_gate(tld);
  mi_free(owner->block);
  return THREAD_OK;
}

static void test_many_owners(void) {
  enum { N = 70 };
  many_owner_t owners[N];
  thread_t threads[N];
  for (size_t i = 0; i < N; i++) {
    mi_atomic_store_relaxed(&owners[i].phase, (uintptr_t)0);
    owners[i].block = NULL;
    thread_start(&threads[i], &many_owner, &owners[i]);
    while (mi_atomic_load_acquire(&owners[i].phase) != 1) _mi_prim_thread_yield();
  }
  char* json = mi_heap_dump_json(true, false);
  assert(json != NULL && strstr(json, "\"complete\": true") != NULL);
  for (size_t i = 0; i < N; i++) {
    char needle[64];
    mi_page_t* page = _mi_ptr_page(owners[i].block);
    const uintptr_t start = (uintptr_t)mi_page_start(page);
    const size_t bsize = mi_page_block_size(page);
    const uintptr_t base = start + (((uintptr_t)owners[i].block - start) / bsize) * bsize;
    snprintf(needle, sizeof(needle), "[%zu,", (size_t)base);
    assert(strstr(json, needle) != NULL);
    mi_atomic_store_release(&owners[i].phase, (uintptr_t)2);
  }
  mi_free(json);
  for (size_t i = 0; i < N; i++) thread_join(threads[i]);
  puts("dump-many-owners: 70 parked owners captured without a fixed claim cap");
}

static _Atomic(uintptr_t) churn_phase;
static _Atomic(uintptr_t) churn_count;
static THREAD_RET churn_owner(void* arg) {
  MI_UNUSED(arg);
  void* slots[64] = { NULL };
  size_t count = 0;
  mi_atomic_store_release(&churn_phase, (uintptr_t)1);
  while (mi_atomic_load_acquire(&churn_phase) == 1) _mi_prim_thread_yield();
  do {
    const size_t slot = count % 64;
    mi_free(slots[slot]);
    slots[slot] = mi_malloc(count % 7 == 0 ? MI_MiB : 1024);
    assert(slots[slot] != NULL);
    count++;
    mi_atomic_store_release(&churn_count, (uintptr_t)count);
  } while (mi_atomic_load_acquire(&churn_phase) == 2);
  for (size_t i = 0; i < 64; i++) mi_free(slots[i]);
  return THREAD_OK;
}

static void test_retirement_churn(void) {
  thread_t worker;
  thread_start(&worker, &churn_owner, NULL);
  while (mi_atomic_load_acquire(&churn_phase) != 1) _mi_prim_thread_yield();
  mi_atomic_store_release(&churn_phase, (uintptr_t)2);
  while (mi_atomic_load_acquire(&churn_count) < 128) _mi_prim_thread_yield();
  for (size_t i = 0; i < 100; i++) {
    char* json = mi_heap_dump_json(true, false);
    assert(json != NULL && strstr(json, "\"complete\":") != NULL);
    mi_free(json);
    _mi_prim_thread_yield();
  }
  mi_atomic_store_release(&churn_phase, (uintptr_t)3);
  thread_join(worker);
  const size_t count = mi_atomic_load_acquire(&churn_count);
  assert(count >= 128); // page-retiring frees really ran, not just initial allocs
  printf("dump-retirement: 100 captures during %zu allocation/free cycles\n", count);
}

int main(void) {
  mi_thread_init();
#if MI_OWNER_GATE && MI_DEBUG > 0
  test_report_gate();
#endif
  test_dump_coverage();
  test_dump_growth();
  test_dump_abandoned();
  test_many_owners();
  test_retirement_churn();
  puts("test-diagnostic-walks: ok");
  return 0;
}
