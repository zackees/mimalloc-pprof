/* Small allocator A/B workload for ci/perf_ab.py (#479). One run = one process.

   usage: perf_ab <threads> <generations> <min_size> <max_size> <ops_per_thread>

   Each thread replays a seeded stream over 8 live slots (allocate 8 / free-oldest 6 /
   free-random 2, one write per 4 KiB). With generations > 1 each thread runs its stream as
   that many short-lived threads, each exiting while it still owns live slots that the next
   one frees. Prints one line: ops/s, cpu seconds, peak RSS, and RSS 500 ms after everything
   was freed. Linux only (getrusage + /proc/self/statm). */
#include <mimalloc.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <time.h>
#include <unistd.h>

#define SLOTS 8

typedef struct { uint64_t rng; size_t lo, hi; long ops; void* slot[SLOTS]; int fifo[4096]; size_t head, tail; } stream_t;

static uint64_t next(uint64_t* s) {
  uint64_t z = (*s += 0x9e3779b97f4a7c15ull);
  z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ull;
  z = (z ^ (z >> 27)) * 0x94d049bb133111ebull;
  return z ^ (z >> 31);
}

static void run_ops(stream_t* st, long n) {
  for (long i = 0; i < n; i++) {
    const uint64_t choice = next(&st->rng) % 16;
    int slot = (int)(next(&st->rng) % SLOTS);
    if (choice >= 8 && choice < 14 && st->head != st->tail) slot = st->fifo[st->head++ % 4096];  /* free oldest */
    if (choice >= 8) { mi_free(st->slot[slot]); st->slot[slot] = NULL; continue; }
    mi_free(st->slot[slot]);
    const size_t size = st->lo + (size_t)(next(&st->rng) % (st->hi - st->lo + 1));
    char* p = (char*)mi_malloc(size);
    if (p == NULL) { fprintf(stderr, "allocation failed\n"); exit(1); }
    for (size_t off = 0; off < size; off += 4096) p[off] = (char)off;
    st->slot[slot] = p;
    st->fifo[st->tail++ % 4096] = slot;
  }
}

static int generations;

static void* generation_main(void* arg) {
  stream_t* st = (stream_t*)arg;
  run_ops(st, st->ops / generations);
  return NULL;  /* exits still owning st->slot[] */
}

static void* worker_main(void* arg) {
  stream_t* st = (stream_t*)arg;
  if (generations <= 1) { run_ops(st, st->ops); }
  else {
    for (int g = 0; g < generations; g++) {
      pthread_t t;
      pthread_create(&t, NULL, &generation_main, st);
      pthread_join(t, NULL);
    }
  }
  for (int i = 0; i < SLOTS; i++) { mi_free(st->slot[i]); st->slot[i] = NULL; }
  return NULL;
}

static double now_s(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return (double)t.tv_sec + (double)t.tv_nsec * 1e-9; }

int main(int argc, char** argv) {
  if (argc != 6) { fprintf(stderr, "usage: perf_ab threads generations min max ops\n"); return 2; }
  const int threads = atoi(argv[1]);
  generations = atoi(argv[2]);
  stream_t* st = (stream_t*)calloc((size_t)threads, sizeof(stream_t));
  pthread_t* t = (pthread_t*)calloc((size_t)threads, sizeof(pthread_t));
  for (int i = 0; i < threads; i++) {
    st[i].rng = 0x5eed0000ull + (uint64_t)i;
    st[i].lo = (size_t)atol(argv[3]); st[i].hi = (size_t)atol(argv[4]); st[i].ops = atol(argv[5]);
  }
  const double start = now_s();
  for (int i = 0; i < threads; i++) pthread_create(&t[i], NULL, &worker_main, &st[i]);
  for (int i = 0; i < threads; i++) pthread_join(t[i], NULL);
  const double elapsed = now_s() - start;
  usleep(500 * 1000);  /* let the deferred purge run */
  struct rusage ru; getrusage(RUSAGE_SELF, &ru);
  long pages = 0, resident = 0;
  FILE* f = fopen("/proc/self/statm", "r");
  if (f == NULL || fscanf(f, "%ld %ld", &pages, &resident) != 2) return 1;
  fclose(f);
  const double cpu = (double)(ru.ru_utime.tv_sec + ru.ru_stime.tv_sec) + (double)(ru.ru_utime.tv_usec + ru.ru_stime.tv_usec) * 1e-6;
  printf("%.1f %.4f %ld %ld\n", (double)threads * (double)st[0].ops / elapsed, cpu,
         ru.ru_maxrss * 1024L, resident * sysconf(_SC_PAGESIZE));
  free(st); free(t);
  return 0;
}
