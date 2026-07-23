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

typedef struct sum_info_s { size_t objs; size_t bytes; } sum_info_t;
static bool sum_visitor(const mi_prof_sample_info_t* info, void* arg) {
  sum_info_t* s = (sum_info_t*)arg;
  s->objs += info->live_objects; s->bytes += info->live_bytes;
  return true;
}

static void test_visit_and_snapshot(void) {
  enum { count = 200, size = 4096 };
  void* blocks[count];
  assert(mi_prof_start_seeded(4096, 53));
  for (size_t i = 0; i < count; i++) blocks[i] = mi_malloc(size);
  sum_info_t visit_sum = { 0, 0 };
  assert(mi_prof_visit(sum_visitor, &visit_sum));
  mi_prof_snapshot_t* snap = mi_prof_snapshot_new();
  assert(snap != NULL);
  sum_info_t snap_sum = { 0, 0 };
  assert(mi_prof_snapshot_visit(snap, sum_visitor, &snap_sum));
  dump_profile();
  unsigned long long dump_objs, dump_bytes, dump_accum_objs, dump_accum_bytes;
  assert(sscanf(dump_text, "heap profile: %llu: %llu [%llu: %llu]", &dump_objs, &dump_bytes, &dump_accum_objs, &dump_accum_bytes) == 4);
  assert(visit_sum.objs == snap_sum.objs && visit_sum.bytes == snap_sum.bytes);
  assert(visit_sum.objs == dump_objs && visit_sum.bytes == dump_bytes);
  mi_prof_snapshot_free(snap);
  for (size_t i = 0; i < count; i++) mi_free(blocks[i]);
  mi_prof_stop();
}

static void test_stats_get(void) {
  mi_prof_stats_t_decl(idle);
  assert(mi_prof_stats_get(&idle));
  assert(!idle.enabled);
  assert(mi_prof_start_seeded(4096, 59));
  enum { count = 100, size = 4096 };
  void* blocks[count];
  for (size_t i = 0; i < count; i++) blocks[i] = mi_malloc(size);
  mi_prof_stats_t_decl(running);
  assert(mi_prof_stats_get(&running));
  assert(running.enabled);
  assert(running.sample_rate == 4096);
  assert(running.live_samples > 0);
  assert(running.live_bytes >= running.live_samples * size);
  for (size_t i = 0; i < count; i++) mi_free(blocks[i]);
  mi_prof_stats_t_decl(freed);
  assert(mi_prof_stats_get(&freed));
  assert(freed.live_samples == 0 && freed.live_bytes == 0);
  mi_prof_stop();
  mi_prof_stats_t_decl(stopped);
  assert(mi_prof_stats_get(&stopped));
  assert(!stopped.enabled);
  mi_prof_stats_t_decl(bad_version);
  bad_version.version = 99;
  assert(!mi_prof_stats_get(&bad_version));
  mi_prof_stats_t_decl(bad_size);
  bad_size.size = sizeof(mi_prof_stats_t) - 1;
  assert(!mi_prof_stats_get(&bad_size));
}

static void noop_writer(void* arg, const char* buf, size_t len) { (void)arg; (void)buf; (void)len; }
typedef struct t10_ctx_s { void* p; bool done; bool dump_ok; } t10_ctx_t;
static bool t10_callback(const mi_prof_sample_info_t* info, void* arg) {
  (void)info;
  t10_ctx_t* ctx = (t10_ctx_t*)arg;
  void* tmp = mi_malloc(1024); assert(tmp != NULL); mi_free(tmp);
  if (!ctx->done) { mi_free(ctx->p); ctx->dump_ok = mi_prof_dump_writer(noop_writer, NULL); ctx->done = true; }
  return true;
}

static void test_visit_reentrancy(void) {
  assert(mi_prof_start_seeded(4096, 61));
  mi_prof_stats_t_decl(before);
  assert(mi_prof_stats_get(&before));
  void* p = mi_malloc(65536);
  assert(p != NULL);
  mi_prof_stats_t_decl(after_alloc);
  assert(mi_prof_stats_get(&after_alloc));
  assert(after_alloc.live_samples > before.live_samples);  /* 64KiB is almost certainly sampled at rate 4096. */
  t10_ctx_t ctx = { p, false, true };
  assert(mi_prof_visit(t10_callback, &ctx));
  assert(!ctx.dump_ok);  /* mi_prof_dump_writer must fail fast from inside a visitor callback. */
  mi_prof_stats_t_decl(after_visit);
  assert(mi_prof_stats_get(&after_visit));
  assert(after_visit.live_samples == before.live_samples);
  mi_prof_snapshot_t* snap = mi_prof_snapshot_new();
  assert(snap != NULL);
  sum_info_t snap_sum_before = { 0, 0 };
  assert(mi_prof_snapshot_visit(snap, sum_visitor, &snap_sum_before));
  mi_prof_stop();
  sum_info_t snap_sum_after = { 0, 0 };
  assert(mi_prof_snapshot_visit(snap, sum_visitor, &snap_sum_after));
  assert(snap_sum_before.objs == snap_sum_after.objs && snap_sum_before.bytes == snap_sum_after.bytes);
  mi_prof_snapshot_free(snap);
}

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
  test_visit_and_snapshot();
  test_stats_get();
  test_visit_reentrancy();
  if (getenv("MIMALLOC_PROF_DUMP_AT_EXIT") != NULL) {
    assert(mi_prof_start_seeded(1, 47));
    assert(mi_malloc(4096) != NULL);  /* Preserve one real sample for pprof validation. */
    mi_process_done();                 /* Exercise mimalloc's process-done dump path in this test binary. */
  }
  puts("profile tests passed");
  return 0;
}
