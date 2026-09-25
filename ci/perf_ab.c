/* Small allocator A/B workload for ci/perf_ab.py (#479). One run = one process.

   usage: perf_ab <threads> <generations> <min_size> <max_size> <ops_per_thread> <pause_ms> <release_bound_ms> <larson_slots>

   Each thread replays a seeded stream over 8 live slots (allocate 8 / free-oldest 6 /
   free-random 2, one write per 4 KiB). With generations > 1 each thread runs its stream as
   that many short-lived threads, each exiting while it still owns live slots that the next
   one frees. With pause_ms > 0 each thread instead runs its stream in BURSTS bursts, freeing
   everything after each and idling pause_ms before the next (bursty reuse, #486). With
   larson_slots > 0 the run is instead Larson & Krishnan's server workload (#506, mirroring
   ScalingPattern::Larson in rust/benchmark-suite/src/scaling.rs): one table of larson_slots blocks
   per thread, each filled by its thread, then LARSON_ROUNDS rounds of ops_per_thread / LARSON_ROUNDS
   random slot replacements; in round r thread i works on table (i + r) % threads, so later frees
   are mostly remote. generations and pause_ms are ignored in that mode. Prints one line: ops/s, process cpu seconds, the workers' own cpu seconds (the
   allocating threads, without the scavenger), minor page faults, peak RSS, and, after everything
   was freed while the worker threads stay alive and idle (a server between requests): RSS
   DRAIN_SHORT_MS later, RSS at the release bound (ci/release_ratchet.json, #491), and the release
   time -- the first sample within RELEASE_TOLERANCE of RSS at twice the bound.
   Linux only (getrusage + /proc/self/statm). */
#define _GNU_SOURCE   /* RUSAGE_THREAD */
#include <mimalloc.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <time.h>
#include <unistd.h>

#define SLOTS 8
#define BURSTS 8
#define DRAIN_SHORT_MS      500
#define RELEASE_SAMPLE_MS   10
#define RELEASE_TOLERANCE   (1L << 20)   /* 1 MiB: "released" = within this of RSS at twice the bound */
#define LARSON_ROUNDS       8             /* `rounds: 8` of ScalingPattern::Larson in rust/benchmark-suite/src/scaling.rs */
#define PAGE_STRIDE         4096          /* one write per this many bytes of a block */

typedef struct { uint64_t rng; size_t lo, hi; long ops; void* slot[SLOTS]; int fifo[4096]; size_t head, tail; double cpu; } stream_t;

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
    for (size_t off = 0; off < size; off += PAGE_STRIDE) p[off] = (char)off;
    st->slot[slot] = p;
    st->fifo[st->tail++ % 4096] = slot;
  }
}

static double cpu_of(const struct rusage* ru) {
  return (double)(ru->ru_utime.tv_sec + ru->ru_stime.tv_sec) + (double)(ru->ru_utime.tv_usec + ru->ru_stime.tv_usec) * 1e-6;
}

/* the calling thread's cpu so far: a worker adds it to its stream just before it exits or idles */
static double thread_cpu(void) { struct rusage ru; getrusage(RUSAGE_THREAD, &ru); return cpu_of(&ru); }

static int generations;
static long pause_ms;
static atomic_int drained, release_workers;

static void* generation_main(void* arg) {
  stream_t* st = (stream_t*)arg;
  run_ops(st, st->ops / generations);
  st->cpu += thread_cpu();
  return NULL;  /* exits still owning st->slot[] */
}

static void free_slots(stream_t* st) {
  for (int i = 0; i < SLOTS; i++) { mi_free(st->slot[i]); st->slot[i] = NULL; }
}

/* the end of every worker: count its cpu, report drained, then idle but alive until released */
static void* idle_worker(stream_t* st) {
  st->cpu += thread_cpu();
  atomic_fetch_add(&drained, 1);
  while (!atomic_load(&release_workers)) usleep(1000);   /* idle, but alive */
  return NULL;
}

static void* worker_main(void* arg) {
  stream_t* st = (stream_t*)arg;
  if (pause_ms > 0) {
    for (int b = 0; b < BURSTS; b++) {
      if (b > 0) usleep((useconds_t)(pause_ms * 1000));
      run_ops(st, st->ops / BURSTS);
      free_slots(st);
    }
  }
  else if (generations <= 1) { run_ops(st, st->ops); }
  else {
    for (int g = 0; g < generations; g++) {
      pthread_t t;
      pthread_create(&t, NULL, &generation_main, st);
      pthread_join(t, NULL);
    }
  }
  free_slots(st);
  return idle_worker(st);
}

/* Larson mode (#506): `threads` tables of larson_slots blocks (libc arrays), rotated per round */
static long larson_slots;
static int larson_threads;
static stream_t* larson_streams;
static void*** larson_tables;
static pthread_barrier_t larson_round_barrier;

static void* larson_block(stream_t* st) {
  const size_t size = st->lo + (size_t)(next(&st->rng) % (st->hi - st->lo + 1));
  char* p = (char*)mi_malloc(size);
  if (p == NULL) { fprintf(stderr, "allocation failed\n"); exit(1); }
  for (size_t off = 0; off < size; off += PAGE_STRIDE) p[off] = (char)off;
  return p;
}

static void* larson_main(void* arg) {
  stream_t* st = (stream_t*)arg;
  const int self = (int)(st - larson_streams);
  for (long s = 0; s < larson_slots; s++) larson_tables[self][s] = larson_block(st);   /* round 0 works on this table */
  for (int r = 0; r < LARSON_ROUNDS; r++) {
    void** table = larson_tables[(self + r) % larson_threads];
    for (long i = 0; i < st->ops / LARSON_ROUNDS; i++) {
      const long s = (long)(next(&st->rng) % (uint64_t)larson_slots);
      mi_free(table[s]);
      table[s] = larson_block(st);
    }
    pthread_barrier_wait(&larson_round_barrier);   /* nobody starts round r + 1 on a table still in round r */
  }
  /* as scaling.rs's larson_drain: free table `self`, whose blocks other threads mostly allocated */
  for (long s = 0; s < larson_slots; s++) { mi_free(larson_tables[self][s]); larson_tables[self][s] = NULL; }
  return idle_worker(st);
}

static long rss_bytes(void) {
  long pages = 0, resident = 0;
  FILE* f = fopen("/proc/self/statm", "r");
  if (f == NULL || fscanf(f, "%ld %ld", &pages, &resident) != 2) exit(1);
  fclose(f);
  return resident * sysconf(_SC_PAGESIZE);
}

static double now_s(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return (double)t.tv_sec + (double)t.tv_nsec * 1e-9; }

int main(int argc, char** argv) {
  if (argc != 9) { fprintf(stderr, "usage: perf_ab threads generations min max ops pause_ms release_bound_ms larson_slots\n"); return 2; }
  pause_ms = atol(argv[6]);
  const long bound_ms = atol(argv[7]);
  const long samples = 2 * bound_ms / RELEASE_SAMPLE_MS + 1;   /* RSS every RELEASE_SAMPLE_MS up to twice the bound */
  long* rss_at = (long*)calloc((size_t)samples, sizeof(long));
  const int threads = atoi(argv[1]);
  generations = atoi(argv[2]);
  stream_t* st = (stream_t*)calloc((size_t)threads, sizeof(stream_t));
  pthread_t* t = (pthread_t*)calloc((size_t)threads, sizeof(pthread_t));
  larson_slots = atol(argv[8]);
  if (larson_slots > 0) {
    larson_threads = threads;
    larson_streams = st;
    larson_tables = (void***)calloc((size_t)threads, sizeof(void**));
    for (int i = 0; i < threads; i++) larson_tables[i] = (void**)calloc((size_t)larson_slots, sizeof(void*));
    pthread_barrier_init(&larson_round_barrier, NULL, (unsigned)threads);
  }
  for (int i = 0; i < threads; i++) {
    st[i].rng = 0x5eed0000ull + (uint64_t)i;
    st[i].lo = (size_t)atol(argv[3]); st[i].hi = (size_t)atol(argv[4]); st[i].ops = atol(argv[5]);
  }
  const double start = now_s();
  for (int i = 0; i < threads; i++) pthread_create(&t[i], NULL, larson_slots > 0 ? &larson_main : &worker_main, &st[i]);
  while (atomic_load(&drained) < threads) usleep(100);
  const double elapsed = now_s() - start;
  struct rusage ru; getrusage(RUSAGE_SELF, &ru);   /* (re-read at DRAIN_SHORT_MS) */
  long rss_short = 0;
  const double drained_at = now_s();
  for (long i = 0; i < samples; i++) {   /* sample i at drained_at + i * RELEASE_SAMPLE_MS, without drift */
    const double wait = drained_at + (double)(i * RELEASE_SAMPLE_MS) * 1e-3 - now_s();
    if (wait > 0) usleep((useconds_t)(wait * 1e6));
    rss_at[i] = rss_bytes();
    if (i * RELEASE_SAMPLE_MS == DRAIN_SHORT_MS) { getrusage(RUSAGE_SELF, &ru); rss_short = rss_at[i]; }
  }
  const long rss_final = rss_at[samples - 1];
  long release_ms = 0;
  while (release_ms / RELEASE_SAMPLE_MS < samples - 1 && rss_at[release_ms / RELEASE_SAMPLE_MS] > rss_final + RELEASE_TOLERANCE) {
    release_ms += RELEASE_SAMPLE_MS;
  }
  double owner_cpu = 0;
  for (int i = 0; i < threads; i++) owner_cpu += st[i].cpu;
  printf("%.1f %.4f %.4f %ld %ld %ld %ld %ld\n", (double)threads * (double)st[0].ops / elapsed, cpu_of(&ru),
         owner_cpu, ru.ru_minflt, ru.ru_maxrss * 1024L, rss_short, rss_at[bound_ms / RELEASE_SAMPLE_MS], release_ms);
  atomic_store(&release_workers, 1);
  for (int i = 0; i < threads; i++) pthread_join(t[i], NULL);
  if (larson_slots > 0) {
    for (int i = 0; i < threads; i++) free(larson_tables[i]);
    free(larson_tables);
    pthread_barrier_destroy(&larson_round_barrier);
  }
  free(st); free(t); free(rss_at);
  return 0;
}
