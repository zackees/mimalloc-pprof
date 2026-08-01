/* Seeded sampling must be reproducible across processes (issue #91).

   This has to spawn a child rather than loop in-process: the bug it guards against
   was that the PRNG seeded from `prof_seed ^ (uintptr_t)tld`, and the tld address is
   randomised by ASLR. Two runs *inside one process* share the same address layout, so
   they would have agreed even while the guarantee was broken. Only separate processes
   vary the layout.

   Protocol: with no argv the program runs itself twice as a child (argv[0] plus the
   "child" flag), parses each child's reported sample count, and requires them to match.
   With "child" it runs one seeded workload and prints the count. */

#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "mimalloc.h"
#include "mimalloc/profile.h"

#define WORKLOAD_BLOCKS 4096
#define WORKLOAD_SIZE   4096
#define SEED            0x5eedu
#define RATE            4096

static int run_child(void) {
  if (!mi_prof_start_seeded(RATE, SEED)) { fprintf(stderr, "start failed\n"); return 2; }
  void** blocks = (void**)malloc(sizeof(void*) * WORKLOAD_BLOCKS);
  if (blocks == NULL) { fprintf(stderr, "oom\n"); return 2; }
  for (size_t i = 0; i < WORKLOAD_BLOCKS; i++) {
    blocks[i] = mi_malloc(WORKLOAD_SIZE);
    if (blocks[i] == NULL) { fprintf(stderr, "oom\n"); return 2; }
  }
  mi_prof_stats_t_decl(stats);
  if (!mi_prof_stats_get(&stats)) { fprintf(stderr, "stats failed\n"); return 2; }
  /* Printed on its own line so the parent can parse it without matching anything else. */
  printf("SAMPLES %llu\n", (unsigned long long)stats.live_samples);
  fflush(stdout);
  for (size_t i = 0; i < WORKLOAD_BLOCKS; i++) mi_free(blocks[i]);
  free(blocks);
  mi_prof_stop();
  return 0;
}

/* Runs `exe child`, returns its reported sample count, or (unsigned long long)-1. */
static unsigned long long sample_count_of_child(const char* exe) {
  char command[4096];
  /* Quoted: the path can contain spaces on Windows runners. */
  snprintf(command, sizeof(command), "\"%s\" child", exe);
  FILE* pipe =
#ifdef _WIN32
    _popen(command, "r");
#else
    popen(command, "r");
#endif
  if (pipe == NULL) { fprintf(stderr, "failed to spawn: %s\n", command); return (unsigned long long)-1; }
  char line[256];
  unsigned long long samples = (unsigned long long)-1;
  while (fgets(line, sizeof(line), pipe) != NULL) {
    unsigned long long value;
    if (sscanf(line, "SAMPLES %llu", &value) == 1) samples = value;
  }
#ifdef _WIN32
  const int rc = _pclose(pipe);
#else
  const int rc = pclose(pipe);
#endif
  if (rc != 0) { fprintf(stderr, "child exited %d\n", rc); return (unsigned long long)-1; }
  return samples;
}

int main(int argc, char** argv) {
  if (argc > 1 && strcmp(argv[1], "child") == 0) return run_child();

  const unsigned long long a = sample_count_of_child(argv[0]);
  const unsigned long long b = sample_count_of_child(argv[0]);
  if (a == (unsigned long long)-1 || b == (unsigned long long)-1) {
    fprintf(stderr, "could not obtain both sample counts\n");
    return 2;
  }
  printf("run1=%llu run2=%llu\n", a, b);

  /* A workload this size at rate 4096 must sample something; equal-but-zero would
     pass the comparison while proving nothing. */
  assert(a > 0);
  assert(a == b);
  printf("seeded sampling is reproducible across processes\n");
  return 0;
}
