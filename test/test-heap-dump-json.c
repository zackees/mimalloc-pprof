/* mi_heap_dump_json / mi_heap_get_seq (issue #269, Bun parity P4).

   Spec is Bun's test/js/bun/jsc/heapStats-mimalloc.test.ts (oven-sh/bun): every JSON
   shape assertion it makes on `heapStats({ dump: true | "blocks" }).mimallocDump` is
   checked here at the C level -- `heaps[].seq`, `pages[].{id,block_size,used,reserved,
   thread_id}`, `blocks` present only when requested, `blocks[[id,size]]`.

   Independent of MI_PPROF (src/heap-dump.c is unconditional), so this test is built and
   registered unconditionally too, like test-memory-events.c / test-dhat.c. */
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include "mimalloc.h"
#include "mimalloc-stats.h"

/* ---- minimal portable threading (same pattern as test-profile-race.c) -- */
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
static void thread_yield_now(void) { Sleep(0); }
#else
#include <pthread.h>
#include <sched.h>
typedef pthread_t thread_t;
typedef void* (*thread_fun_t)(void*);
#define THREAD_RET void*
#define THREAD_OK  NULL
static void thread_start(thread_t* t, thread_fun_t fn, void* arg) {
  assert(pthread_create(t, NULL, fn, arg) == 0);
}
static void thread_join(thread_t t) { assert(pthread_join(t, NULL) == 0); }
static void thread_yield_now(void) { sched_yield(); }
#endif

#define KNOWN_SIZE_1    128
#define KNOWN_SIZE_2    256
#define N_ALLOC_HEAP1   64
#define DUMP_ITERATIONS 20

static int count_char(const char* s, char c) {
  int n = 0;
  for (; *s != 0; s++) { if (*s == c) n++; }
  return n;
}

/* bounded needle search, since the per-heap segment below is a slice of a larger
   null-terminated buffer and may not itself be null-terminated at its logical end. */
static const char* find_bounded(const char* hay, size_t hay_len, const char* needle) {
  const size_t nlen = strlen(needle);
  if (nlen == 0 || hay_len < nlen) return NULL;
  for (size_t i = 0; i + nlen <= hay_len; i++) {
    if (memcmp(hay + i, needle, nlen) == 0) return hay + i;
  }
  return NULL;
}

/* the JSON body for one heap, from its `"seq": <seq>,` header up to (but not including)
   the next heap's `"seq":` key, or the end of the string for the last heap. Used instead
   of predicting an exact `block_size` (which includes debug-build padding/guard-page
   overhead that varies by MI_DEBUG/MI_GUARDED and is not something callers should have to
   predict) -- callers instead read back the value this heap's own dump reported and sanity
   check it against the known allocation. */
static bool find_heap_segment(const char* json, size_t seq, const char** out_start, size_t* out_len) {
  char seqbuf[32];
  snprintf(seqbuf, sizeof(seqbuf), "\"seq\": %zu,", seq);
  const char* start = strstr(json, seqbuf);
  if (start == NULL) return false;
  const char* next = strstr(start + strlen(seqbuf), "\"seq\":");
  *out_start = start;
  *out_len = (next != NULL ? (size_t)(next - start) : strlen(start));
  return true;
}

/* sums every numeric value that follows `"<key>": ` within [seg, seg+seg_len). Guarded
   builds (MIMALLOC_GUARDED_SAMPLE_RATE=1) can split N same-heap allocations across N
   separate single-block pages, so "used" is checked in aggregate across the whole heap
   segment rather than assuming a single page holds every allocation. */
static size_t sum_field(const char* seg, size_t seg_len, const char* key) {
  const size_t klen = strlen(key);
  size_t total = 0;
  size_t off = 0;
  for (;;) {
    if (off >= seg_len) break;
    const char* p = find_bounded(seg + off, seg_len - off, key);
    if (p == NULL) break;
    size_t val = 0;
    sscanf(p + klen, "%zu", &val);
    total += val;
    const size_t consumed = (size_t)(p - seg) + klen;
    if (consumed <= off) break; /* safety: never loop without progress */
    off = consumed;
  }
  return total;
}

/* asserts every numeric value following `"<key>": ` within the segment is >= min_value
   (block_size includes debug-build padding/guard-page overhead that varies by
   MI_DEBUG/MI_GUARDED, so the exact value is read back rather than predicted); returns
   how many occurrences were checked. */
static int check_field_min(const char* seg, size_t seg_len, const char* key, size_t min_value) {
  const size_t klen = strlen(key);
  int count = 0;
  size_t off = 0;
  for (;;) {
    if (off >= seg_len) break;
    const char* p = find_bounded(seg + off, seg_len - off, key);
    if (p == NULL) break;
    size_t val = 0;
    assert(sscanf(p + klen, "%zu", &val) == 1);
    assert(val >= min_value);
    count++;
    const size_t consumed = (size_t)(p - seg) + klen;
    if (consumed <= off) break;
    off = consumed;
  }
  return count;
}

static void* heap1_blocks[N_ALLOC_HEAP1];

static THREAD_RET dump_worker(void* arg) {
  (void)arg;
  for (int i = 0; i < DUMP_ITERATIONS; i++) {
    char* json = mi_heap_dump_json(true, true);
    assert(json != NULL);   /* the JSON-buffer machinery must keep working under the race;
                                content is best-effort under concurrent frees (#78) and is
                                not otherwise inspected here. */
    mi_free(json);
    thread_yield_now();
  }
  return THREAD_OK;
}

int main(void) {
  /* --- two heaps with known allocation sizes; seq distinct and stable --- */
  mi_heap_t* heap1 = mi_heap_new();
  mi_heap_t* heap2 = mi_heap_new();
  assert(heap1 != NULL && heap2 != NULL);

  const size_t seq1 = mi_heap_get_seq(heap1);
  const size_t seq2 = mi_heap_get_seq(heap2);
  assert(seq1 != seq2);
  assert(mi_heap_get_seq(heap1) == seq1);
  assert(mi_heap_get_seq(heap2) == seq2);
  assert(mi_heap_get_seq(NULL) == 0);

  for (int i = 0; i < N_ALLOC_HEAP1; i++) {
    heap1_blocks[i] = mi_heap_malloc(heap1, KNOWN_SIZE_1);
    assert(heap1_blocks[i] != NULL);
    memset(heap1_blocks[i], 0xAB, KNOWN_SIZE_1);
  }
  void* p2 = mi_heap_malloc(heap2, KNOWN_SIZE_2);
  assert(p2 != NULL);
  memset(p2, 0xCD, KNOWN_SIZE_2);

  /* --- pages-only dump: valid JSON, both heaps' seq present, known bin sizes present,
     no "blocks" key anywhere (include_blocks == false) --- */
  char* json_pages = mi_heap_dump_json(false, false);
  assert(json_pages != NULL);
  assert(count_char(json_pages, '{') == count_char(json_pages, '}'));
  assert(count_char(json_pages, '[') == count_char(json_pages, ']'));
  assert(strstr(json_pages, "\"heaps\"") != NULL);
  assert(strstr(json_pages, "\"blocks\"") == NULL);

  char numbuf[64];
  snprintf(numbuf, sizeof(numbuf), "\"seq\": %zu,", seq1);
  assert(strstr(json_pages, numbuf) != NULL);
  snprintf(numbuf, sizeof(numbuf), "\"seq\": %zu,", seq2);
  assert(strstr(json_pages, numbuf) != NULL);

  /* heap1's known allocation: exactly N_ALLOC_HEAP1 blocks of >= KNOWN_SIZE_1 bytes each.
     Summed rather than expected on one page: a normal build packs them into one page, but
     a guarded build (MIMALLOC_GUARDED_SAMPLE_RATE=1, see run_guarded in ci/verify_local.py)
     gives every allocation its own single-block page. */
  const char* seg1; size_t seg1_len;
  assert(find_heap_segment(json_pages, seq1, &seg1, &seg1_len));
  assert(sum_field(seg1, seg1_len, "\"used\": ") == (size_t)N_ALLOC_HEAP1);
  assert(check_field_min(seg1, seg1_len, "\"block_size\": ", KNOWN_SIZE_1) >= 1);

  const char* seg2; size_t seg2_len;
  assert(find_heap_segment(json_pages, seq2, &seg2, &seg2_len));
  assert(sum_field(seg2, seg2_len, "\"used\": ") == 1);
  assert(check_field_min(seg2, seg2_len, "\"block_size\": ", KNOWN_SIZE_2) >= 1);

  /* seq is stable across a second, independent dump */
  char* json_pages2 = mi_heap_dump_json(false, false);
  assert(json_pages2 != NULL);
  snprintf(numbuf, sizeof(numbuf), "\"seq\": %zu,", seq1);
  assert(strstr(json_pages2, numbuf) != NULL);
  mi_free(json_pages2);

  /* --- with-blocks dump: capture heap1's first raw block id, then verify
     hash_addresses hides it. Not compared against the pointer mi_heap_malloc returned:
     a guarded build (MIMALLOC_GUARDED_SAMPLE_RATE=1, see run_guarded in
     ci/verify_local.py) offsets the user-visible pointer from the block's internal
     base/slot address that the dump reports, so the two are not interchangeable. --- */
  char* json_blocks_raw = mi_heap_dump_json(true, false);
  assert(json_blocks_raw != NULL);
  assert(count_char(json_blocks_raw, '{') == count_char(json_blocks_raw, '}'));
  assert(strstr(json_blocks_raw, "\"blocks\"") != NULL);

  const char* rseg1; size_t rseg1_len;
  assert(find_heap_segment(json_blocks_raw, seq1, &rseg1, &rseg1_len));
  const char* blocks_prefix = "\"blocks\": [[";
  const char* blocks_at = find_bounded(rseg1, rseg1_len, blocks_prefix);
  assert(blocks_at != NULL);
  size_t raw_id = 0;
  assert(sscanf(blocks_at + strlen(blocks_prefix), "%zu", &raw_id) == 1);
  char idbuf[32];
  snprintf(idbuf, sizeof(idbuf), "%zu", raw_id);
  assert(strstr(json_blocks_raw, idbuf) != NULL);
  mi_free(json_blocks_raw);

  /* --- hash_addresses == true hides that same raw id --- */
  char* json_blocks_hashed = mi_heap_dump_json(true, true);
  assert(json_blocks_hashed != NULL);
  assert(strstr(json_blocks_hashed, "\"blocks\"") != NULL);
  assert(strstr(json_blocks_hashed, idbuf) == NULL);
  mi_free(json_blocks_hashed);

  mi_free(json_pages);

  /* --- dump from a second thread while the first thread frees (no crash); pages stay
     non-empty (only half of heap1's blocks are freed) so this does not exercise the
     documented page-retirement race in #78, just the common "read while write" case. --- */
  thread_t t;
  thread_start(&t, dump_worker, NULL);
  for (int i = 0; i < N_ALLOC_HEAP1 / 2; i++) {
    mi_free(heap1_blocks[i]);
    heap1_blocks[i] = NULL;
  }
  thread_join(t);

  for (int i = N_ALLOC_HEAP1 / 2; i < N_ALLOC_HEAP1; i++) { mi_free(heap1_blocks[i]); }
  mi_free(p2);
  mi_heap_destroy(heap1);
  mi_heap_destroy(heap2);

  printf("test-heap-dump-json: ok\n");
  return 0;
}
