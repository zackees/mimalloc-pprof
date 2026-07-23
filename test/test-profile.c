#include <assert.h>
#include <stdio.h>
#include "mimalloc/profile.h"

static void profile_worker(void) {
  for (size_t i = 0; i < 100000; i++) {
    void* p = mi_malloc(32);
    assert(p != NULL);
    mi_free(p);
  }
}

#ifdef _WIN32
#include <windows.h>
static DWORD WINAPI profile_thread(void* arg) { (void)arg; profile_worker(); return 0; }
static void profile_threads(void) {
  HANDLE threads[4];
  for (size_t i = 0; i < 4; i++) threads[i] = CreateThread(NULL, 0, profile_thread, NULL, 0, NULL);
  WaitForMultipleObjects(4, threads, TRUE, INFINITE);
  for (size_t i = 0; i < 4; i++) CloseHandle(threads[i]);
}
#else
#include <pthread.h>
static void* profile_thread(void* arg) { (void)arg; profile_worker(); return NULL; }
static void profile_threads(void) {
  pthread_t threads[4];
  for (size_t i = 0; i < 4; i++) assert(pthread_create(&threads[i], NULL, profile_thread, NULL) == 0);
  for (size_t i = 0; i < 4; i++) assert(pthread_join(threads[i], NULL) == 0);
}
#endif

int main(void) {
  enum { count = 1000, size = 512 };
  void* blocks[count];
  size_t records = 0, bytes = 0;
  assert(mi_prof_start_seeded(4096, 17));
  assert(!mi_prof_start(4096));
  for (size_t i = 0; i < count; i++) blocks[i] = mi_malloc(size);
  mi_prof_debug_stats(&records, &bytes);
  assert(records >= 60 && records <= 190);
  assert(bytes >= records * size);
  for (size_t i = 0; i < count; i++) mi_free(blocks[i]);
  mi_prof_debug_stats(&records, &bytes);
  assert(records == 0 && bytes == 0);
  mi_prof_stop();
  assert(!mi_prof_is_enabled());
  assert(mi_prof_start_seeded(1, 23));
  void* p = mi_malloc(128);
  mi_prof_debug_stats(&records, &bytes);
  assert(records == 1 && bytes == 128);
  assert(mi_realloc(p, 96) == p);
  mi_prof_debug_stats(&records, &bytes);
  assert(records == 1 && bytes == 96);
  mi_free(p);
  mi_prof_debug_stats(&records, &bytes);
  assert(records == 0 && bytes == 0);
  mi_prof_stop();
  assert(mi_prof_start_seeded(1, 31));
  profile_threads();
  mi_prof_debug_stats(&records, &bytes);
  assert(records == 0 && bytes == 0);
  mi_prof_stop();
  puts("profile tests passed");
  return 0;
}
