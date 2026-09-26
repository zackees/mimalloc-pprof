/* Small allocator A/B workload for ci/perf_ab.py (#479). One run = one process.

   usage: perf_ab <threads> <generations> <min_size> <max_size> <ops_per_thread> <pause_ms> <table_slots> <release_bound_ms>

   Each thread replays a seeded stream over 8 live slots (allocate 8 / free-oldest 6 /
   free-random 2, one write per 4 KiB). With generations > 1 each thread runs its stream as
   that many short-lived threads, each exiting while it still owns live slots that the next
   one frees. With pause_ms > 0 each thread instead runs its stream in BURSTS bursts, freeing
   everything after each and idling pause_ms before the next (bursty reuse, #486). With
   table_slots > 0 the threads instead run the Larson & Krishnan server workload (#506, the shape of
   the README's larson chart, rust/benchmark-suite ScalingPattern::Larson): one shared table of
   table_slots blocks per thread, each draw frees a random slot's block and allocates a new one into
   it, and every LARSON_ROUNDS-th of the draws the tables rotate to the next thread, so later frees
   are of another thread's blocks; at the end each thread frees the table it started with. Like the
   chart's harness (its planner's VecDeque, which the override routes through the allocator under
   test), each table also appends every draw's slot to a log that grows by doubling mi_realloc from
   table_slots entries: the growing-buffer pattern of any Rust Vec. Prints one line: ops/s, process cpu seconds, the workers' own cpu seconds (the
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
#define LARSON_ROUNDS 8   /* as the benchmark suite's LarsonRotation { rounds: 8 } */
#define DRAIN_SHORT_MS      500
#define RELEASE_SAMPLE_MS   10
#define RELEASE_TOLERANCE   (1L << 20)   /* 1 MiB: "released" = within this of RSS at twice the bound */

typedef struct { uint64_t rng; size_t lo, hi; long ops; void* slot[SLOTS]; int fifo[4096]; size_t head, tail; double cpu; int index; } stream_t;

/* #506: one Larson table; a round of draws on it runs on one thread at a time (the round barrier) */
typedef struct { uint64_t rng; void** slot; size_t* log; size_t log_len, log_cap; } table_t;

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

static double cpu_of(const struct rusage* ru) {
  return (double)(ru->ru_utime.tv_sec + ru->ru_stime.tv_sec) + (double)(ru->ru_utime.tv_usec + ru->ru_stime.tv_usec) * 1e-6;
}

/* the calling thread's cpu so far: a worker adds it to its stream just before it exits or idles */
static double thread_cpu(void) { struct rusage ru; getrusage(RUSAGE_THREAD, &ru); return cpu_of(&ru); }

static int generations;
static long pause_ms;
static int threads, table_slots;
static table_t* tables;
static pthread_barrier_t round_barrier;
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

/* one round of Larson draws on `tb`: free a random slot's block, allocate a new one into it */
static void larson_round(table_t* tb, size_t lo, size_t hi, long n) {
  for (long i = 0; i < n; i++) {
    const size_t k = (size_t)(next(&tb->rng) % (uint64_t)table_slots);
    const size_t size = lo + (size_t)(next(&tb->rng) % (hi - lo + 1));
    mi_free(tb->slot[k]);
    char* p = (char*)mi_malloc(size);
    if (p == NULL) { fprintf(stderr, "allocation failed\n"); exit(1); }
    p[0] = (char)size;
    tb->slot[k] = p;
    if (tb->log_len == tb->log_cap) {   /* as the harness's VecDeque::push_back: double */
      tb->log_cap *= 2;
      tb->log = (size_t*)mi_realloc(tb->log, tb->log_cap * sizeof(size_t));
      if (tb->log == NULL) { fprintf(stderr, "allocation failed\n"); exit(1); }
    }
    tb->log[tb->log_len++] = k;
  }
}

static void* worker_main(void* arg) {
  stream_t* st = (stream_t*)arg;
  if (table_slots > 0) {
    for (int r = 0; r < LARSON_ROUNDS; r++) {
      larson_round(&tables[(st->index + r) % threads], st->lo, st->hi, st->ops / LARSON_ROUNDS);
      pthread_barrier_wait(&round_barrier);
    }
    table_t* own = &tables[st->index];
    for (int k = 0; k < table_slots; k++) { mi_free(own->slot[k]); own->slot[k] = NULL; }
    mi_free(own->log); own->log = NULL;
  }
  else if (pause_ms > 0) {
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
  st->cpu += thread_cpu();
  atomic_fetch_add(&drained, 1);
  while (!atomic_load(&release_workers)) usleep(1000);   /* idle, but alive */
  return NULL;
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
  if (argc != 9) { fprintf(stderr, "usage: perf_ab threads generations min max ops pause_ms table_slots release_bound_ms\n"); return 2; }
  pause_ms = atol(argv[6]);
  table_slots = atoi(argv[7]);
  const long bound_ms = atol(argv[8]);
  const long samples = 2 * bound_ms / RELEASE_SAMPLE_MS + 1;   /* RSS every RELEASE_SAMPLE_MS up to twice the bound */
  long* rss_at = (long*)calloc((size_t)samples, sizeof(long));
  threads = atoi(argv[1]);
  generations = atoi(argv[2]);
  stream_t* st = (stream_t*)calloc((size_t)threads, sizeof(stream_t));
  pthread_t* t = (pthread_t*)calloc((size_t)threads, sizeof(pthread_t));
  for (int i = 0; i < threads; i++) {
    st[i].rng = 0x5eed0000ull + (uint64_t)i;
    st[i].lo = (size_t)atol(argv[3]); st[i].hi = (size_t)atol(argv[4]); st[i].ops = atol(argv[5]);
    st[i].index = i;
  }
  if (table_slots > 0) {   /* allocated before the clock starts, and by libc: not the allocator under test */
    tables = (table_t*)calloc((size_t)threads, sizeof(table_t));
    for (int i = 0; i < threads; i++) {
      tables[i].rng = 0x1a750000ull + (uint64_t)i;
      tables[i].slot = (void**)calloc((size_t)table_slots, sizeof(void*));
      tables[i].log_cap = (size_t)table_slots;
      tables[i].log = (size_t*)mi_malloc(tables[i].log_cap * sizeof(size_t));
    }
    pthread_barrier_init(&round_barrier, NULL, (unsigned)threads);
  }
  const double start = now_s();
  for (int i = 0; i < threads; i++) pthread_create(&t[i], NULL, &worker_main, &st[i]);
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
  if (table_slots > 0) {
    for (int i = 0; i < threads; i++) free(tables[i].slot);
    free(tables);
    pthread_barrier_destroy(&round_barrier);
  }
  free(st); free(t); free(rss_at);
  return 0;
}
