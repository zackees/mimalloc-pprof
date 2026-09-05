/* ----------------------------------------------------------------------------
Copyright (c) 2026 mimalloc-pprof contributors
This is free software; you can redistribute it and/or modify it under the
terms of the MIT license. A copy of the license can be found in the file
"LICENSE" at the root of this distribution.
-----------------------------------------------------------------------------*/

/* #338: the exit-time snapshot (`MIMALLOC_SNAPSHOT_ON_EXIT=2`, `MIMALLOC_SNAPSHOT_PATH`).

   Pins two decisions: (1) `_mi_heap_snapshot_on_exit` runs from `mi_process_done` after
   the scavenger stopped and before any teardown, so the file is complete and flag 2's
   free-list collection still finds live theaps; (2) format version 1's record layout, by
   parsing the file with an independent reader (this one; the Python reader in
   examples/heap-snapshot/ is the third).

   Runs itself as a child with the environment set, then parses what the child wrote:
   magic/version, at least one page, at least one page with a free map (flag 2), and the
   END footer whose page_count matches the records seen. */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <mimalloc.h>

#if defined(_WIN32)
#include <process.h>
#include <windows.h>
#else
#include <unistd.h>
#include <sys/wait.h>
#include <pthread.h>
#endif

#define MAGIC 0x5348494Du
#define SEC_ARENA 0x414E5241u
#define SEC_HEAP  0x50414548u
#define SEC_PAGE  0x45474150u
#define SEC_END   0x444E4520u

/* ---- child: allocate on two threads, exit normally ---- */
static void* churn(void* arg) {
  (void)arg;
  void* keep[256];
  for (int i = 0; i < 256; i++) { keep[i] = mi_malloc(64 + (i % 900)); memset(keep[i], 1, 8); }
  for (int i = 0; i < 256; i += 2) mi_free(keep[i]);   /* leave the pages partially used: free lists to collect */
  return NULL;
}
static int child_main(void) {
  static void* hold[64];
  for (int i = 0; i < 64; i++) { hold[i] = mi_malloc(4096 * (1 + i % 5)); memset(hold[i], 2, 16); }
  for (int i = 1; i < 64; i += 3) mi_free(hold[i]);
  #if defined(_WIN32)
  HANDLE t = (HANDLE)_beginthreadex(NULL, 0, (unsigned (__stdcall*)(void*))churn, NULL, 0, NULL);
  WaitForSingleObject(t, INFINITE); CloseHandle(t);
  #else
  pthread_t t; pthread_create(&t, NULL, churn, NULL); pthread_join(t, NULL);
  #endif
  return 0;   /* normal exit -> mi_process_done -> _mi_heap_snapshot_on_exit */
}

/* ---- parent: a second, independent reader of format v1 ---- */
typedef struct { FILE* f; long pos; int err; } rd_t;
static uint32_t rd_u32(rd_t* r) { uint32_t v = 0; if (fread(&v, 4, 1, r->f) != 1) r->err = 1; return v; }
static uint64_t rd_u64(rd_t* r) { uint64_t v = 0; if (fread(&v, 8, 1, r->f) != 1) r->err = 1; return v; }
static uint8_t  rd_u8 (rd_t* r) { uint8_t  v = 0; if (fread(&v, 1, 1, r->f) != 1) r->err = 1; return v; }
static void rd_skip(rd_t* r, size_t n) { if (fseek(r->f, (long)n, SEEK_CUR) != 0) r->err = 1; }
static void rd_bitmap(rd_t* r) { uint32_t chunks = rd_u32(r); uint32_t chunk_bytes = rd_u32(r); rd_skip(r, (size_t)chunks * chunk_bytes); }   /* second word is MI_BCHUNK_SIZE in BYTES */

/* Reads one page record (the caller already consumed the non-zero page_start). */
static int rd_page_after_start(rd_t* r, int* has_freemap) {
  (void)rd_u64(r); (void)rd_u64(r);                      /* slice_start, block_size */
  (void)rd_u32(r); uint32_t capacity = rd_u32(r); (void)rd_u32(r);  /* reserved, capacity, used */
  (void)rd_u64(r); (void)rd_u64(r); (void)rd_u64(r);   /* committed, tid, heap_seq */
  (void)rd_u32(r); (void)rd_u32(r); (void)rd_u32(r);   /* arena_idx, slice_index, slice_count */
  (void)rd_u8(r); (void)rd_u8(r); (void)rd_u8(r); (void)rd_u8(r);   /* memkind, kind, abandoned, full */
  uint8_t fm = rd_u8(r); (void)rd_u8(r); (void)rd_u8(r); (void)rd_u8(r);
  (void)capacity;
  if (fm) { *has_freemap = 1; uint32_t nbytes = rd_u32(r); rd_skip(r, nbytes); }   /* u32 byte count, then the map (bit=1 means free) */
  return r->err == 0;
}
static uint64_t rd_pages(rd_t* r, int* has_freemap) {
  uint64_t n = 0;
  if (rd_u32(r) != SEC_PAGE) { fprintf(stderr, "FAIL: expected PAGE section\n"); r->err = 1; return 0; }
  for (;;) {
    uint64_t start = rd_u64(r);
    if (r->err) return n;
    if (start == 0) return n;              /* sentinel */
    if (!rd_page_after_start(r, has_freemap)) return n;
    n++;
  }
}

static int parent_check(const char* path) {
  rd_t r = { fopen(path, "rb"), 0, 0 };
  if (r.f == NULL) { fprintf(stderr, "FAIL: child wrote no snapshot at %s\n", path); return 1; }
  if (rd_u32(&r) != MAGIC) { fprintf(stderr, "FAIL: bad magic\n"); return 1; }
  if (rd_u32(&r) != 1)     { fprintf(stderr, "FAIL: unexpected version\n"); return 1; }
  (void)rd_u32(&r); (void)rd_u32(&r);                  /* ptr_size, slice_size */
  uint32_t flags = rd_u32(&r); (void)rd_u32(&r);       /* flags, reserved */
  (void)rd_u64(&r); (void)rd_u64(&r);                  /* clock, tid */
  if (!(flags & MI_SNAPSHOT_BLOCKS)) { fprintf(stderr, "FAIL: option value 2 must set MI_SNAPSHOT_BLOCKS (flags=%u)\n", flags); return 1; }
  uint32_t arenas = rd_u32(&r);
  uint64_t pages = 0; int has_freemap = 0;
  for (uint32_t a = 0; a < arenas; a++) {
    if (rd_u32(&r) != SEC_ARENA) { fprintf(stderr, "FAIL: expected ARNA\n"); return 1; }
    (void)rd_u32(&r); (void)rd_u64(&r); (void)rd_u64(&r); (void)rd_u32(&r); (void)rd_u32(&r); (void)rd_u32(&r);
    (void)rd_u8(&r); (void)rd_u8(&r); (void)rd_u8(&r); (void)rd_u8(&r);
    rd_bitmap(&r); rd_bitmap(&r); rd_bitmap(&r);       /* committed, free, purge */
    pages += rd_pages(&r, &has_freemap);
  }
  pages += rd_pages(&r, &has_freemap);                   /* writer thread's non-arena pages */
  for (;;) {                                             /* heaps until END */
    uint32_t tag = rd_u32(&r);
    if (r.err) { fprintf(stderr, "FAIL: truncated before END\n"); return 1; }
    if (tag == SEC_END) break;
    if (tag != SEC_HEAP) { fprintf(stderr, "FAIL: unexpected section 0x%08x\n", tag); return 1; }
    (void)rd_u64(&r); (void)rd_u32(&r); (void)rd_u64(&r);
    pages += rd_pages(&r, &has_freemap);
  }
  uint64_t footer = rd_u64(&r);
  fclose(r.f);
  printf("snapshot: arenas=%u pages=%llu footer_page_count=%llu freemap_seen=%d\n",
         arenas, (unsigned long long)pages, (unsigned long long)footer, has_freemap);
  if (r.err) { fprintf(stderr, "FAIL: read error\n"); return 1; }
  if (pages == 0) { fprintf(stderr, "FAIL: no pages\n"); return 1; }
  if (footer != pages) { fprintf(stderr, "FAIL: footer page_count %llu != records %llu\n", (unsigned long long)footer, (unsigned long long)pages); return 1; }
  if (!has_freemap) { fprintf(stderr, "FAIL: flag 2 set but no page carried a free map -- exit ordering lost the calling thread's pages?\n"); return 1; }
  printf("test-snapshot-exit: OK\n");
  return 0;
}

int main(int argc, char** argv) {
  if (argc > 1 && strcmp(argv[1], "--child") == 0) return child_main();
  if (argc < 2) { fprintf(stderr, "usage: %s <snapshot-path>\n", argv[0]); return 2; }
  const char* path = argv[1];
  remove(path);
  #if defined(_WIN32)
  _putenv("MIMALLOC_SNAPSHOT_ON_EXIT=2");
  { char buf[1024]; snprintf(buf, sizeof buf, "MIMALLOC_SNAPSHOT_PATH=%s", path); _putenv(buf); }
  const char* args[] = { argv[0], "--child", NULL };
  intptr_t rc = _spawnv(_P_WAIT, argv[0], args);
  if (rc != 0) { fprintf(stderr, "FAIL: child exited %d\n", (int)rc); return 1; }
  #else
  setenv("MIMALLOC_SNAPSHOT_ON_EXIT", "2", 1);
  setenv("MIMALLOC_SNAPSHOT_PATH", path, 1);
  pid_t pid = fork();
  if (pid == 0) { execl(argv[0], argv[0], "--child", (char*)NULL); _exit(127); }
  int status = 0; waitpid(pid, &status, 0);
  if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) { fprintf(stderr, "FAIL: child status %d\n", status); return 1; }
  #endif
  const int rc = parent_check(path);
  remove(path);   /* leave nothing behind */
  return rc;
}
