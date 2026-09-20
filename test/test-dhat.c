/* Focused exact-DHAT smoke and composition test (issue #238).

   #414: memory-events is compiled out unless MI_MEMEVT=1, while DHAT dispatches through
   the memory-events hook SITES (which survive whenever `MI_MEMEVT || MI_DHAT`). So the
   DHAT half of this test runs in both shapes, and the composition half -- the public
   `mi_memory_*` callback table observing the same events -- is asserted only where that
   API is real. The `MI_MEMEVT=0 MI_DHAT=1` build is exactly where the shared hook-site
   guard is load-bearing, which is why it is a CI row of its own (`dhat-on`). */
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
#if MI_MEMEVT
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
#else
/* memory-events compiled out: its public API must still LINK, and must report itself off. */
static void assert_memevt_is_stubbed(void) {
  mi_memory_callbacks_t callbacks;
  memset(&callbacks, 0, sizeof(callbacks));
  assert(!mi_memory_tracking_set_enabled(true));
  assert(!mi_memory_tracking_is_enabled());
  assert(!mi_memory_set_callbacks(&callbacks));
  mi_memory_snapshot_t snap; memset(&snap, 0, sizeof(snap));
  snap.size = sizeof(snap); snap.version = MI_MEMORY_SNAPSHOT_VERSION;
  assert(!mi_memory_snapshot(&snap));
}
#endif

int main(void) {
  callback_counts_t callbacks = { 0, 0, 0 };
  #if MI_MEMEVT
  assert(mi_memory_tracking_set_enabled(true));
  install_callbacks(&callbacks);
  #else
  assert_memevt_is_stubbed();
  #endif
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
  #if MI_MEMEVT
  assert(callbacks.alloc == 2 && callbacks.free == 1 && callbacks.resize == 1);
  #endif
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
  #if MI_MEMEVT
  assert(mi_memory_set_callbacks(NULL));
  #endif
  puts("DHAT tests passed");
  return 0;
}
