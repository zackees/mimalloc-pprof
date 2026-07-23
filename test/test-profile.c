#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "mimalloc/profile.h"

static void profile_worker(void) {
  for (size_t i = 0; i < 100000; i++) {
    void* p = mi_malloc(32);
    assert(p != NULL);
    mi_free(p);
  }
}

static char dump_text[65536];
static size_t dump_used;
static void dump_write(void* arg, const char* buf, size_t len) {
  (void)arg; assert(len <= sizeof(dump_text)-dump_used-1); memcpy(dump_text+dump_used,buf,len); dump_used+=len; dump_text[dump_used]=0;
  /* The writer is deliberately reentrant: this allocation would deadlock if
     mi_prof_dump_writer invoked client code while holding its table lock. */
  void* p = mi_malloc(16); assert(p != NULL); mi_free(p);
}

static void dump_profile(void) {
  dump_used = 0;
  assert(mi_prof_dump_writer(dump_write, NULL));
  assert(strstr(dump_text, "heap profile:") == dump_text);
  assert(strstr(dump_text, "@ heap_v2/") != NULL);
  assert(strstr(dump_text, "MAPPED_LIBRARIES:\n") != NULL);
#ifdef _WIN32
  assert(strstr(dump_text, "mimalloc-test-profile.exe") != NULL);
#else
  assert(strstr(dump_text, "mimalloc-test-profile") != NULL);
#endif
}

#ifdef _WIN32
#include <windows.h>
static DWORD WINAPI profile_thread(void* arg) { (void)arg; profile_worker(); return 0; }
static void profile_threads(void) {
  HANDLE threads[8];
  for (size_t i = 0; i < 8; i++) threads[i] = CreateThread(NULL, 0, profile_thread, NULL, 0, NULL);
  for (size_t i = 0; i < 10; i++) dump_profile();
  WaitForMultipleObjects(8, threads, TRUE, INFINITE);
  for (size_t i = 0; i < 8; i++) CloseHandle(threads[i]);
}
#else
#include <pthread.h>
static void* profile_thread(void* arg) { (void)arg; profile_worker(); return NULL; }
static void profile_threads(void) {
  pthread_t threads[8];
  for (size_t i = 0; i < 8; i++) assert(pthread_create(&threads[i], NULL, profile_thread, NULL) == 0);
  for (size_t i = 0; i < 10; i++) dump_profile();
  for (size_t i = 0; i < 8; i++) assert(pthread_join(threads[i], NULL) == 0);
}
#endif

int main(void) {
  enum { count = 1000, size = 512 };
  void* blocks[count];
  size_t records = 0, bytes = 0, stacks = 0;
  if (getenv("MIMALLOC_PROF") != NULL) { void* probe = mi_malloc(1); assert(probe != NULL); mi_free(probe); assert(mi_prof_is_enabled()); }
  if (mi_prof_is_enabled()) mi_prof_stop();  /* Keep the test's seeded runs deterministic. */
  assert(mi_prof_start_seeded(4096, 17));
  assert(!mi_prof_start(4096));
  for (size_t i = 0; i < count; i++) blocks[i] = mi_malloc(size);
  mi_prof_debug_stats(&records, &bytes, &stacks);
  assert(records >= 60 && records <= 190);
  assert(bytes >= records * size);
  assert(stacks > 0);
  dump_profile();
  assert(strstr(dump_text, "@ heap_v2/4096") != NULL);
  for (size_t i = 0; i < count; i++) mi_free(blocks[i]);
  mi_prof_debug_stats(&records, &bytes, &stacks);
  assert(records == 0 && bytes == 0);
  if (mi_option_is_enabled(mi_option_prof_accum)) assert(stacks > 0); else assert(stacks == 0);
  mi_prof_stop();
  assert(!mi_prof_is_enabled());
  assert(mi_prof_start_seeded(1, 23));
  void* p = mi_malloc(128);
  mi_prof_debug_stats(&records, &bytes, NULL);
  assert(records == 1 && bytes == 128);
  assert(mi_realloc(p, 96) == p);
  mi_prof_debug_stats(&records, &bytes, NULL);
  assert(records == 1 && bytes == 96);
  mi_free(p);
  mi_prof_debug_stats(&records, &bytes, NULL);
  assert(records == 0 && bytes == 0);
  mi_prof_stop();
  assert(mi_prof_start_seeded(1, 31));
  profile_threads();
  mi_collect(true);  /* Drains delayed cross-thread frees before checking samples. */
  mi_prof_debug_stats(&records, &bytes, NULL);
  /* Thread runtimes may retain small per-thread allocations; the concurrency
     contract here is parseable dumps and no lock inversion, not an empty heap. */
  mi_prof_stop();
  assert(mi_prof_start_seeded(524288, 43));
  enum { estimate_count = 25600, estimate_size = 4096 };
  void* estimate_blocks[estimate_count];
  for (size_t i = 0; i < estimate_count; i++) { estimate_blocks[i] = mi_malloc(estimate_size); assert(estimate_blocks[i] != NULL); }
  mi_prof_debug_stats(&records, &bytes, NULL);
  /* pprof's scaleHeapSample estimates the allocation stream from the
     geometric sample rate; this simple bound verifies the raw counterpart. */
  assert(records >= 100 && records <= 400);
  assert(records * (size_t)524288 >= 50 * 1024 * 1024 && records * (size_t)524288 <= 200 * 1024 * 1024);
  for (size_t i = 0; i < estimate_count; i++) mi_free(estimate_blocks[i]);
  mi_prof_stop();
  assert(mi_prof_start_seeded(1, 29));
  enum { accum_count = 64 };
  void* accum_blocks[accum_count];
  for (size_t i = 0; i < accum_count; i++) accum_blocks[i] = mi_malloc(256);
  dump_profile();
  unsigned long long live0, livebytes0, accum0, accumbytes0;
  assert(sscanf(dump_text, "heap profile: %llu: %llu [%llu: %llu]", &live0, &livebytes0, &accum0, &accumbytes0) == 4);
  for (size_t i = 0; i < accum_count / 2; i++) mi_free(accum_blocks[i]);
  dump_profile();
  unsigned long long live1, livebytes1, accum1, accumbytes1;
  assert(sscanf(dump_text, "heap profile: %llu: %llu [%llu: %llu]", &live1, &livebytes1, &accum1, &accumbytes1) == 4);
  if (mi_option_is_enabled(mi_option_prof_accum)) {
    assert(accum1 >= accum0 && accum1 >= live1 && accumbytes1 >= livebytes1);
  } else {
    assert(accum0 == 0 && accumbytes0 == 0 && accum1 == 0 && accumbytes1 == 0);
  }
  for (size_t i = accum_count / 2; i < accum_count; i++) mi_free(accum_blocks[i]);
  mi_prof_debug_stats(&records, &bytes, &stacks);
  assert(records == 0 && bytes == 0);
  if (mi_option_is_enabled(mi_option_prof_accum)) { assert(stacks > 0); mi_prof_reset(); mi_prof_debug_stats(NULL, NULL, &stacks); assert(stacks == 0); }
  else { assert(stacks == 0); }
  mi_prof_stop();
  if (getenv("MIMALLOC_PROF_DUMP_AT_EXIT") != NULL) {
    assert(mi_prof_start_seeded(1, 47));
    assert(mi_malloc(4096) != NULL);  /* Preserve one real sample for pprof validation. */
    mi_process_done();                 /* Exercise mimalloc's process-done dump path in this test binary. */
  }
  puts("profile tests passed");
  return 0;
}
