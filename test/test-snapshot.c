// Exercise mi_heap_snapshot: allocate a varied size mix, write a snapshot,
// and let the CLI verify it parses. Intended to be run as:
//   ./mimalloc-test-snapshot /tmp/snap.bin && ./mi-heapview /tmp/snap.bin summary

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#if defined(_WIN32)
#include <io.h>
#include <process.h>   // getpid
#else
#include <unistd.h>
#endif
#include "mimalloc.h"

static int write_snapshot(const char* out) {
  int fd;
  #if defined(_WIN32)
  fd = _open(out, _O_WRONLY|_O_CREAT|_O_TRUNC|_O_BINARY, 0644);
  #else
  fd = open(out, O_WRONLY|O_CREAT|O_TRUNC, 0644);
  #endif
  if (fd < 0) { perror(out); return -1; }
  int rc = mi_heap_snapshot(fd, MI_SNAPSHOT_BLOCKS);
  #if defined(_WIN32)
  _close(fd);
  #else
  close(fd);
  #endif
  return rc;
}

int main(int argc, char** argv) {
  const char* out = (argc > 1 ? argv[1] : "heap-snapshot.bin");
  const char* out2 = (argc > 2 ? argv[2] : NULL);

  // varied allocations across size classes; keep some, free some to create fragmentation
  void* keep[4096]; size_t nk = 0;
  size_t sizes[] = { 16, 24, 48, 96, 160, 320, 640, 1280, 4096, 16*1024, 128*1024, 1024*1024 };
  for (size_t s = 0; s < sizeof(sizes)/sizeof(sizes[0]); s++) {
    for (int i = 0; i < 200; i++) {
      void* p = mi_malloc(sizes[s]);
      if (p == NULL) continue;
      memset(p, (int)(sizes[s] & 0xFF), sizes[s] > 64 ? 64 : sizes[s]);
      if ((i % 3) == 0 && nk < 4096) keep[nk++] = p;
      else mi_free(p);
    }
  }
  // one huge allocation
  void* huge = mi_malloc(8*1024*1024);
  (void)huge;

  if (write_snapshot(out) != 0) { fprintf(stderr, "mi_heap_snapshot failed\n"); return 1; }
  printf("wrote %s\n", out);

  if (out2 != NULL) {
    // simulate a "leak": allocate more 320-byte blocks with a recognizable header
    static const uint64_t fake_vtable = 0xC0DEFACE00112233ull;
    for (int i = 0; i < 5000; i++) {
      void* p = mi_malloc(300);
      if (p) { memcpy(p, &fake_vtable, 8); memset((char*)p+8, 'B', 16); }
    }
    if (write_snapshot(out2) != 0) { fprintf(stderr, "second snapshot failed\n"); return 1; }
    printf("wrote %s\n", out2);
  }

  if (argc > 3 && strcmp(argv[3], "--pause") == 0) {
    printf("pid %d\n", (int)getpid()); fflush(stdout);
    // wait for SIGTERM/SIGKILL after coredump
    #if !defined(_WIN32)
    pause();
    #endif
  }

  for (size_t i = 0; i < nk; i++) mi_free(keep[i]);
  // mimalloc-pprof: as a ctest this must leave nothing behind (the viewer is exercised on
  // committed fixtures instead, see ci/tests/test_heap_snapshot_example.py).
  if (argc <= 3 || strcmp(argv[3], "--keep") != 0) { remove(out); if (out2 != NULL) remove(out2); }
  return 0;
}
