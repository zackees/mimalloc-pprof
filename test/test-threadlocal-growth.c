/* Lifecycle and growth-boundary regression for the dynamic heap/theap TLS map.

   This is deliberately public-API-only. It covers every geometric boundary,
   the transition to linear growth, thread exit, and sub-process teardown. */

#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "mimalloc.h"

#ifdef _WIN32
#include <windows.h>
typedef HANDLE thread_t;
typedef DWORD (WINAPI *thread_fun_t)(void*);
#define THREAD_RET DWORD WINAPI
#define THREAD_OK 0
static void thread_start(thread_t* thread, thread_fun_t fn, void* arg) {
  *thread = CreateThread(NULL, 0, fn, arg, 0, NULL);
  assert(*thread != NULL);
}
static void thread_join(thread_t thread) {
  assert(WaitForSingleObject(thread, INFINITE) == WAIT_OBJECT_0);
  CloseHandle(thread);
}
#else
#include <pthread.h>
typedef pthread_t thread_t;
typedef void* (*thread_fun_t)(void*);
#define THREAD_RET void*
#define THREAD_OK NULL
static void thread_start(thread_t* thread, thread_fun_t fn, void* arg) {
  assert(pthread_create(thread, NULL, fn, arg) == 0);
}
static void thread_join(thread_t thread) {
  assert(pthread_join(thread, NULL) == 0);
}
#endif

#define BOUNDARY_HEAPS 1050
#define WORKER_HEAPS 65
#define WORKERS 4

static mi_heap_t* boundary_heaps[BOUNDARY_HEAPS];
static mi_theap_t* boundary_theaps[BOUNDARY_HEAPS];

static bool is_growth_boundary(size_t count) {
  return (count == 16 || count == 32 || count == 64 || count == 128 ||
          count == 256 || count == 512 || count == 1024 || count == 1025);
}

static void exercise_heap_set(size_t count) {
  mi_heap_t* heaps[WORKER_HEAPS];
  mi_theap_t* theaps[WORKER_HEAPS];
  assert(count <= WORKER_HEAPS);
  for (size_t i = 0; i < count; i++) {
    heaps[i] = mi_heap_new();
    assert(heaps[i] != NULL);
    theaps[i] = mi_heap_theap(heaps[i]);
    assert(theaps[i] != NULL);
    void* const p = mi_heap_malloc(heaps[i], 17 + i);
    assert(p != NULL);
    memset(p, (int)i, 17 + i);
  }
  for (size_t i = 0; i < count; i++) {
    assert(mi_heap_theap(heaps[i]) == theaps[i]);
    mi_heap_destroy(heaps[i]);
  }
}

static THREAD_RET thread_worker(void* arg) {
  (void)arg;
  exercise_heap_set(WORKER_HEAPS);
  return THREAD_OK;
}

typedef struct subproc_arg_s {
  mi_subproc_id_t subproc;
} subproc_arg_t;

static THREAD_RET subproc_worker(void* raw) {
  subproc_arg_t* const arg = (subproc_arg_t*)raw;
  mi_subproc_add_current_thread(arg->subproc);
  exercise_heap_set(WORKER_HEAPS);
  return THREAD_OK;
}

int main(void) {
  /* Keep every heap live while checking previous bindings. This forces the TLS
     table across 16..1024 doubling and the first 1024-slot linear increment. */
  for (size_t i = 0; i < BOUNDARY_HEAPS; i++) {
    boundary_heaps[i] = mi_heap_new();
    assert(boundary_heaps[i] != NULL);
    boundary_theaps[i] = mi_heap_theap(boundary_heaps[i]);
    assert(boundary_theaps[i] != NULL);
    if (is_growth_boundary(i + 1)) {
      void* const p = mi_heap_malloc(boundary_heaps[i], i + 1);
      assert(p != NULL);
      memset(p, 0x5A, i + 1);
    }
    for (size_t j = 0; j <= i; j++) {
      assert(mi_heap_theap(boundary_heaps[j]) == boundary_theaps[j]);
    }
  }
  for (size_t i = 0; i < BOUNDARY_HEAPS; i++) {
    mi_heap_destroy(boundary_heaps[i]);
  }

  thread_t workers[WORKERS];
  for (size_t i = 0; i < WORKERS; i++) {
    thread_start(&workers[i], thread_worker, NULL);
  }
  for (size_t i = 0; i < WORKERS; i++) {
    thread_join(workers[i]);
  }

  subproc_arg_t arg;
  arg.subproc = mi_subproc_new();
  assert(arg.subproc._mi_subproc_id != NULL);
  thread_t subproc_thread;
  thread_start(&subproc_thread, subproc_worker, &arg);
  thread_join(subproc_thread);
  mi_subproc_destroy(arg.subproc);

  puts("ok: TLS growth boundaries, thread exit, and subproc teardown");
  return 0;
}
