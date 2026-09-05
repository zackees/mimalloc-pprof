/* Demo: produce a live heap snapshot from a small two-thread workload (issue #338).

   Build against this repo's build tree, e.g.
     cc -O2 -I include examples/heap-snapshot/demo.c build/libmimalloc.a -lpthread -o demo
   then
     ./demo /tmp/snap.bin
     uv run examples/heap-snapshot/heapview.py /tmp/snap.bin sizes --top 10
     ./build/mi-heapview /tmp/snap.bin sizes --top 10        # the C viewer, same rows

   Or skip the API entirely and let the allocator write one at exit:
     MIMALLOC_SNAPSHOT_ON_EXIT=2 MIMALLOC_SNAPSHOT_PATH=/tmp/snap.bin ./any-program */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <mimalloc.h>
#if defined(_WIN32)
#include <windows.h>
#include <process.h>
#else
#include <pthread.h>
#endif

static void* worker(void* arg) {
  (void)arg;
  void* keep[512];
  for (int i = 0; i < 512; i++) { keep[i] = mi_malloc(32 + (i * 37) % 2000); memset(keep[i], 1, 8); }
  for (int i = 0; i < 512; i += 3) mi_free(keep[i]);     /* leave holes: something for `frag` to show */
  return NULL;
}

int main(int argc, char** argv) {
  const char* out = (argc > 1 ? argv[1] : "mimalloc-snapshot.bin");
  static void* big[32];
  for (int i = 0; i < 32; i++) { big[i] = mi_malloc(64 * 1024 * (1 + i % 4)); memset(big[i], 2, 64); }
  #if defined(_WIN32)
  HANDLE t = (HANDLE)_beginthreadex(NULL, 0, (unsigned (__stdcall*)(void*))worker, NULL, 0, NULL);
  WaitForSingleObject(t, INFINITE); CloseHandle(t);
  #else
  pthread_t t; pthread_create(&t, NULL, worker, NULL); pthread_join(t, NULL);
  #endif
  /* MI_SNAPSHOT_BLOCKS adds per-block free maps for pages this thread owns (`blocks --addr`). */
  if (mi_heap_snapshot_to_file(out, MI_SNAPSHOT_BLOCKS) != 0) { fprintf(stderr, "snapshot failed\n"); return 1; }
  printf("wrote %s\n", out);
  return 0;
}
