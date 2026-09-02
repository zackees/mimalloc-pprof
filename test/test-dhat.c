/* Focused exact-DHAT smoke and composition test (issue #238). */
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "mimalloc.h"
#include "mimalloc/memory-events.h"
#include "mimalloc/dhat.h"

typedef struct callback_counts_s { int alloc, free, resize; } callback_counts_t;
static void on_change(const mi_memory_change_t* change, void* arg) {
  callback_counts_t* counts = (callback_counts_t*)arg;
  if (change->kind == MI_MEMORY_ALLOCATE) counts->alloc++;
  else if (change->kind == MI_MEMORY_FREE) counts->free++;
  else if (change->kind == MI_MEMORY_RESIZE) counts->resize++;
}
static void install_callbacks(callback_counts_t* counts) {
  mi_memory_callbacks_t callbacks;
  memset(&callbacks, 0, sizeof(callbacks));
  callbacks.handlers[MI_MEMORY_ALLOCATE] = on_change; callbacks.args[MI_MEMORY_ALLOCATE] = counts;
  callbacks.handlers[MI_MEMORY_FREE] = on_change; callbacks.args[MI_MEMORY_FREE] = counts;
  callbacks.handlers[MI_MEMORY_RESIZE] = on_change; callbacks.args[MI_MEMORY_RESIZE] = counts;
  assert(mi_memory_set_callbacks(&callbacks));
}

int main(void) {
  /* MI_GUARDED (issue #266): this test's assertions are exact block/byte counts, which
     assume DHAT sees precisely the caller's requested sizes. Under
     MIMALLOC_GUARDED_SAMPLE_RATE forcing every allocation to be guarded, a guarded
     block's own single-block page gets returned to the arena the moment it is freed
     (src/page.c's _mi_page_free), which this test's exact-identity/in-place-resize
     assumptions (and, separately, its counts) are not written to tolerate. This test is
     about DHAT accounting precision, not the guarded allocator, so disable guarding for
     the whole run rather than chasing guarded-specific behavior it was never meant to
     exercise. */
#if MI_GUARDED
  mi_theap_guarded_set_sample_rate(mi_theap_get_default(), 0, 0);
#endif
  assert(mi_memory_tracking_set_enabled(true));
  callback_counts_t callbacks = { 0, 0, 0 };
  install_callbacks(&callbacks);
  assert(mi_dhat_start());
  /* Empty and budget-exhausted sessions still need a valid, fail-soft JSON dump. */
  assert(mi_dhat_dump("test-dhat-empty.json"));

  void* p = mi_malloc(16); assert(p != NULL);
  void* q = mi_malloc(32); assert(q != NULL);
  p = mi_realloc(p, 20); assert(p != NULL); /* exact identity whether this stays put or moves */
  mi_free(q);

  mi_dhat_stats_t_decl(mid);
  assert(mi_dhat_stats_get(&mid));
  assert(mid.enabled && !mid.incomplete);
  /* realloc is a second allocation call for DHAT totals while retaining p's
     identity/lifetime, so the 20-byte request adds one block and 20 bytes. */
  assert(mid.total_blocks == 3 && mid.total_bytes == 68);
  assert(mid.live_blocks == 1 && mid.live_bytes == 20);
  assert(mid.peak_bytes >= 48 && mid.peak_bytes >= mid.live_bytes);
  assert(callbacks.alloc == 2 && callbacks.free == 1 && callbacks.resize == 1);
  /* Dump while active: stdio itself may allocate, so this also verifies dump-time
     recursion suppression and that serialization never re-enters its own lock. */
  const callback_counts_t callbacks_before_dump = callbacks;
  assert(mi_dhat_dump("test-dhat-output.json"));
  assert(memcmp(&callbacks, &callbacks_before_dump, sizeof(callbacks)) == 0);

  mi_free(p);

  /* Force the over-aligned fallback, then exercise its in-place resize. DHAT
     must report caller requests (16 and 12), never the internal over-allocation. */
  void* aligned = mi_malloc_aligned(16, 64); assert(aligned != NULL);
  aligned = mi_realloc_aligned(aligned, 12, 64); assert(aligned != NULL);
  mi_free(aligned);

  mi_dhat_stats_t_decl(done);
  assert(mi_dhat_stats_get(&done));
  assert(done.live_blocks == 0 && done.live_bytes == 0);
  assert(done.total_blocks == 5 && done.total_bytes == 96);

  mi_dhat_stop();
  assert(!mi_dhat_is_enabled());
  assert(remove("test-dhat-empty.json") == 0);
  assert(mi_dhat_dump("test-dhat-output.json"));
  FILE* f = fopen("test-dhat-output.json", "rb"); assert(f != NULL);
  char json[8192]; const size_t n = fread(json, 1, sizeof(json) - 1, f); fclose(f); json[n] = 0;
  assert(strstr(json, "\"dhatFileVersion\": 2") != NULL);
  assert(strstr(json, "\"bklt\": true") != NULL);
  assert(strstr(json, "\"bkacc\": false") != NULL);
  assert(strstr(json, "\"pps\"") != NULL && strstr(json, "\"ftbl\"") != NULL);
  assert(remove("test-dhat-output.json") == 0);
  assert(mi_memory_set_callbacks(NULL));
  puts("DHAT tests passed");
  return 0;
}
