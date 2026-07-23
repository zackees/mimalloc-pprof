#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
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

/* ---- T12: mi_prof_dump_proto_writer -------------------------------------------------------
   A ~40-line mini protobuf decoder that walks only the top-level Profile fields (and, for
   Sample/Mapping, one level into their known submessage shape) -- not a general pprof reader. */
static bool t12_pb_varint(const unsigned char* buf, size_t len, size_t* pos, uint64_t* out) {
  uint64_t result = 0; int shift = 0;
  while (*pos < len) {
    unsigned char b = buf[(*pos)++];
    result |= ((uint64_t)(b & 0x7F)) << shift;
    if (!(b & 0x80)) { *out = result; return true; }
    shift += 7; if (shift >= 64) return false;
  }
  return false;
}
static bool t12_pb_next(const unsigned char* buf, size_t len, size_t* pos, uint32_t* field, uint32_t* wire, uint64_t* val, const unsigned char** bytes, size_t* blen) {
  if (*pos >= len) return false;
  uint64_t tag; if (!t12_pb_varint(buf, len, pos, &tag)) return false;
  *field = (uint32_t)(tag >> 3); *wire = (uint32_t)(tag & 7);
  if (*wire == 0) return t12_pb_varint(buf, len, pos, val);
  if (*wire == 2) { uint64_t l; if (!t12_pb_varint(buf, len, pos, &l)) return false; *bytes = buf + *pos; *blen = (size_t)l; *pos += (size_t)l; return true; }
  return false;
}
typedef struct t12_buf_s { unsigned char* data; size_t len, cap; } t12_buf_t;
static unsigned char t12_proto_buf[262144];
static void t12_write(void* arg, const char* buf, size_t len) {
  t12_buf_t* b = (t12_buf_t*)arg;
  assert(b->len + len <= b->cap);
  memcpy(b->data + b->len, buf, len); b->len += len;
}
static void test_proto_dump(void) {
  enum { count = 200, size = 4096, max_strings = 64, max_mappings = 16 };
  void* blocks[count];
  assert(mi_prof_start_seeded(4096, 71));
  for (size_t i = 0; i < count; i++) blocks[i] = mi_malloc(size);
  mi_prof_stats_t_decl(stats);
  assert(mi_prof_stats_get(&stats));

  t12_buf_t out = { t12_proto_buf, 0, sizeof(t12_proto_buf) };
  assert(mi_prof_dump_proto_writer(t12_write, &out));
  assert(out.len > 0);

  char strtab[max_strings][300]; size_t string_count = 0;
  size_t mapping_filename_idx[max_mappings]; size_t mapping_count = 0;
  size_t sample_type_count = 0, sample_count = 0;
  unsigned long long inuse_sum = 0;
  size_t pos = 0; uint32_t field, wire; uint64_t val; const unsigned char* bytes; size_t blen;
  while (t12_pb_next(out.data, out.len, &pos, &field, &wire, &val, &bytes, &blen)) {
    if (wire != 2) continue;  /* period/default_sample_type are top-level varints; not needed below. */
    if (field == 1) sample_type_count++;
    else if (field == 2) {
      sample_count++;
      size_t p2 = 0; uint32_t f2, w2; uint64_t v2; const unsigned char* b2; size_t bl2;
      while (t12_pb_next(bytes, blen, &p2, &f2, &w2, &v2, &b2, &bl2)) {
        if (f2 == 2 && w2 == 2) {  /* Sample.value: packed varints, no per-element tag. */
          size_t p3 = 0; int idx = 0;
          while (p3 < bl2) { uint64_t v; if (!t12_pb_varint(b2, bl2, &p3, &v)) break; if (idx == 2) inuse_sum += v; idx++; }
        }
      }
    }
    else if (field == 3 && mapping_count < max_mappings) {
      size_t p2 = 0; uint32_t f2, w2; uint64_t v2; const unsigned char* b2; size_t bl2; size_t fname_idx = SIZE_MAX;
      while (t12_pb_next(bytes, blen, &p2, &f2, &w2, &v2, &b2, &bl2)) if (f2 == 5 && w2 == 0) fname_idx = (size_t)v2;
      mapping_filename_idx[mapping_count++] = fname_idx;
    }
    else if (field == 6 && string_count < max_strings) {
      size_t n = (blen < sizeof(strtab[0]) - 1) ? blen : sizeof(strtab[0]) - 1;
      memcpy(strtab[string_count], bytes, n); strtab[string_count][n] = 0; string_count++;
    }
  }

  assert(sample_type_count >= 1);
  assert(sample_count > 0);
  /* mi_prof_dump_proto_writer pre-scales values with Go's protomem.go scaleHeapSample convention
     (see the comment above that function in src/profile.c), it does not emit raw sampled counts.
     With size==rate==4096 here the scale factor is 1/(1-exp(-1)) ~= 1.582, so the summed
     inuse_objects across samples should land strictly between 1x and 2x the raw live_samples
     count -- not exact, hence the 2x tolerance rather than an equality check. */
  assert(inuse_sum >= (unsigned long long)stats.live_samples);
  assert(inuse_sum <= (unsigned long long)stats.live_samples * 2);

  bool found_module = false;
  for (size_t i = 0; i < mapping_count; i++) {
    size_t idx = mapping_filename_idx[i];
    if (idx < string_count && strstr(strtab[idx], "mimalloc-test-profile") != NULL) found_module = true;
  }
  assert(found_module);

  for (size_t i = 0; i < count; i++) mi_free(blocks[i]);
  mi_prof_stop();
}

/* ---- T14-C: mi_prof_modules_visit ---------------------------------------------------------- */
typedef struct t14_ctx_s { bool found; } t14_ctx_t;
static bool t14_module_visitor(const mi_prof_module_info_t* info, void* arg) {
  t14_ctx_t* ctx = (t14_ctx_t*)arg;
  if (strstr(info->path, "mimalloc-test-profile") != NULL) ctx->found = true;
  return true;
}
static bool t14_guard_visitor(const mi_prof_sample_info_t* info, void* arg) {
  (void)info; (void)arg;
  t14_ctx_t modctx = { false };
  const bool inner_ok = mi_prof_modules_visit(t14_module_visitor, &modctx);
  assert(!inner_ok);  /* guard: mi_prof_modules_visit must fail fast from inside a mi_prof_visit callback. */
  return true;
}
static void test_modules_visit(void) {
  t14_ctx_t ctx = { false };
  assert(mi_prof_modules_visit(t14_module_visitor, &ctx));
  assert(ctx.found);

  assert(mi_prof_start_seeded(4096, 79));
  void* p = mi_malloc(65536); assert(p != NULL);  /* 64KiB at rate 4096 is always sampled (see test_visit_reentrancy). */
  assert(mi_prof_visit(t14_guard_visitor, NULL));
  mi_free(p);
  mi_prof_stop();
}

/* ---- T15: mi_prof_start_ex / mi_prof_config_t -----------------------------------------------
   Covers issue #32's struct-based sibling of mi_prof_start: default-equivalence, the
   FALLBACK/OVERRIDE env precedence contract, version/size rejection, and the
   max_profiler_bytes arena budget. */
#ifdef _WIN32
static void test_setenv(const char* name, const char* value) { _putenv_s(name, value); }
static void test_unsetenv(const char* name) { _putenv_s(name, ""); }
#else
static void test_setenv(const char* name, const char* value) { setenv(name, value, 1); }
static void test_unsetenv(const char* name) { unsetenv(name); }
#endif

/* T15a: a fully zeroed mi_prof_config_t (mi_prof_config_t_decl's initial state) must behave
   identically to mi_prof_start(0). */
static void test_start_ex_default(void) {
  test_unsetenv("MIMALLOC_PROF_SAMPLE_INTERVAL");
  test_unsetenv("MIMALLOC_PROF_SAMPLE_RATE");

  mi_prof_config_t_decl(cfg);
  assert(mi_prof_start_ex(&cfg));
  assert(mi_prof_is_enabled());
  mi_prof_stats_t_decl(ex_stats);
  assert(mi_prof_stats_get(&ex_stats));
  mi_prof_stop();
  assert(!mi_prof_is_enabled());

  assert(mi_prof_start(0));
  mi_prof_stats_t_decl(plain_stats);
  assert(mi_prof_stats_get(&plain_stats));
  mi_prof_stop();

  assert(ex_stats.enabled && plain_stats.enabled);
  assert(ex_stats.accum == plain_stats.accum);
  assert(ex_stats.sample_rate == plain_stats.sample_rate);
  assert(ex_stats.sample_rate == 524288);  /* documented default (profile.h). */
}

/* T15b: MI_PROF_CONFIG_OVERRIDE -- a non-zero struct field wins even though the matching
   env var is also set (explicit CLI/struct flags are authoritative). */
static void test_start_ex_override(void) {
  test_setenv("MIMALLOC_PROF_SAMPLE_INTERVAL", "1000");
  mi_prof_config_t_decl(cfg);
  cfg.mode = MI_PROF_CONFIG_OVERRIDE;
  cfg.sample_interval = 2000;
  assert(mi_prof_start_ex(&cfg));
  mi_prof_stats_t_decl(stats);
  assert(mi_prof_stats_get(&stats));
  assert(stats.sample_rate == 2000);  /* struct wins over env in OVERRIDE mode. */
  mi_prof_stop();
  test_unsetenv("MIMALLOC_PROF_SAMPLE_INTERVAL");
}

/* T15c: MI_PROF_CONFIG_FALLBACK (the default, mode == 0) -- the env var wins over the
   struct field; the struct only fills gaps where env is silent (ops can tune a shipped
   binary without a rebuild). */
static void test_start_ex_fallback(void) {
  test_setenv("MIMALLOC_PROF_SAMPLE_INTERVAL", "1000");
  mi_prof_config_t_decl(cfg);
  cfg.mode = MI_PROF_CONFIG_FALLBACK;
  cfg.sample_interval = 2000;
  assert(mi_prof_start_ex(&cfg));
  mi_prof_stats_t_decl(stats);
  assert(mi_prof_stats_get(&stats));
  assert(stats.sample_rate == 1000);  /* env wins over struct in FALLBACK mode. */
  mi_prof_stop();
  test_unsetenv("MIMALLOC_PROF_SAMPLE_INTERVAL");
}

/* T15d: a version or size mismatch must be rejected (false), not crash or silently start
   with defaults/garbage. */
static void test_start_ex_bad_version(void) {
  mi_prof_config_t_decl(bad_version);
  bad_version.version = 999;
  assert(!mi_prof_start_ex(&bad_version));
  assert(!mi_prof_is_enabled());

  mi_prof_config_t_decl(bad_size);
  bad_size.size = sizeof(mi_prof_config_t) - 1;
  assert(!mi_prof_start_ex(&bad_size));
  assert(!mi_prof_is_enabled());
}

/* T15e: max_profiler_bytes budgets the profiler's own persistent arena (records + stack
   intern table). Once the budget is exhausted, _mi_prof_arena_alloc refuses to grow with a
   new chunk and new samples are silently dropped -- the underlying mi_malloc/mi_free calls
   always still succeed; only profiler bookkeeping is skipped. This must be the last T15 test:
   mi_option_prof_max_bytes has no "unset" and stays budgeted for the rest of the process. */
static void test_start_ex_max_bytes(void) {
  mi_prof_config_t_decl(cfg);
  cfg.sample_interval = 1;                 /* sample every allocation so the budget fills fast. */
  cfg.max_profiler_bytes = 3 * 64 * 1024;  /* a few chunks; MI_PROF_CHUNK_SIZE is 64KiB. */
  assert(mi_prof_start_ex(&cfg));

  enum { count = 20000, size = 64 };
  void* blocks[count];
  for (size_t i = 0; i < count; i++) { blocks[i] = mi_malloc(size); assert(blocks[i] != NULL); }

  mi_prof_stats_t_decl(stats);
  assert(mi_prof_stats_get(&stats));
  assert(stats.arena_committed <= cfg.max_profiler_bytes);  /* budget never exceeded. */
  assert(stats.live_samples < count);                       /* budget forced drops -- not every
                                                                  allocation could be recorded. */

  for (size_t i = 0; i < count; i++) mi_free(blocks[i]);  /* frees must not crash even for
                                                               allocations whose sample was dropped. */
  mi_prof_stop();
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
  test_proto_dump();
  test_modules_visit();
  test_start_ex_default();
  test_start_ex_override();
  test_start_ex_fallback();
  test_start_ex_bad_version();
  test_start_ex_max_bytes();
  if (getenv("MIMALLOC_PROF_DUMP_AT_EXIT") != NULL) {
    assert(mi_prof_start_seeded(1, 47));
    assert(mi_malloc(4096) != NULL);  /* Preserve one real sample for pprof validation. */
    mi_process_done();                 /* Exercise mimalloc's process-done dump path in this test binary. */
  }
  puts("profile tests passed");
  return 0;
}
