/* Small allocator A/B workload for ci/perf_ab.py (#479). One run = one process.

   usage: perf_ab <threads> <generations> <min_size> <max_size> <ops_per_thread> <pause_ms> <table_slots> <sizes> <slots> <release_bound_ms>
          perf_ab probe <size>...

   <sizes> is how a request size is drawn from [min_size, max_size]: "uniform" (every byte count
   equally likely; min == max is an exact-size row) or "log" (#527: log-uniform, the benchmark
   suite's sparse-large-buffers draw -- an octave uniformly, then a uniform offset inside it,
   clamped to the range; see draw_size).

   Each thread replays a seeded stream over <slots> live slots -- 8 in every row but the #422
   fixed-budget ones (#529), at most MAX_SLOTS -- (allocate 8 / free-oldest 6 / free-random 2, one
   write per 4 KiB). With generations > 1 each thread runs its stream as
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
   table_slots entries: the growing-buffer pattern of any Rust Vec. The default build prints one line: ops/s, process cpu seconds, the workers' own cpu seconds (the
   allocating threads, without the scavenger), minor page faults, peak RSS (VmHWM: see
   peak_rss_bytes), and, after everything
   was freed while the worker threads stay alive and idle (a server between requests): RSS
   DRAIN_SHORT_MS later, RSS at the release bound (ci/release_ratchet.json, #491), and the release
   time -- the first sample within RELEASE_TOLERANCE of RSS at twice the bound.
   With PERF_AB_HOLES_REPORT set in the environment (#529, #422 E4; perf_ab.py --holes-report runs
   it untimed, in an MI_DIAGNOSTICS=ON build) every worker, once all of them have finished their
   stream and while each still holds its live slots, prints to stderr in turn its live bytes, the
   process RSS, the mi_purge_holes_stats_get counters and mi_purge_holes_report() (its own pages,
   the arena slack, and the arena layout walk).
   `perf_ab probe` (#527) allocates nothing timed: for each size it prints the bin and page kind the
   linked allocator gives that request, so a size-class edge is confirmed, not assumed (see probe).
   The separate -DPERF_AB_DIAGNOSTIC=1 build emits one JSON object instead, with pre-worker
   measured-work and post-work drain process snapshots, worker CPU and a deterministic trace
   checksum. The normal perf-ab build has none of that per-operation checksum work.
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

#define MAX_SLOTS 64   /* the #422 fixed aggregate budget (#529): all 64 slots on one worker */
#define BURSTS 8
#define LARSON_ROUNDS 8   /* as the benchmark suite's LarsonRotation { rounds: 8 } */
#define DRAIN_SHORT_MS      500
#define RELEASE_SAMPLE_MS   10
#define RELEASE_TOLERANCE   (1L << 20)   /* 1 MiB: "released" = within this of RSS at twice the bound */
/* the page sizes of include/mimalloc/types.h (MI_SMALL/MEDIUM/LARGE_PAGE_SIZE, 64-bit): a probe
   names the smallest that holds its page's blocks. Mirrored, not included: this file uses the
   public header only, so it links against the base arm's library too. */
#define PROBE_SMALL_PAGE    (64L << 10)
#define PROBE_MEDIUM_PAGE   (512L << 10)
#define PROBE_LARGE_PAGE    (4L << 20)
#ifndef PERF_AB_STREAM_SEED_BASE
#define PERF_AB_STREAM_SEED_BASE 0x5eed0000ull
#endif
#ifndef PERF_AB_LARSON_TABLE_SEED_BASE
#define PERF_AB_LARSON_TABLE_SEED_BASE 0x1a750000ull
#endif
#define PERF_AB_PHASE_DEFINITION "work=pre-worker-start..all-workers-drained; drain=then..end-of-2x-bound-RSS-window"

#if defined(PERF_AB_DIAGNOSTIC)
typedef struct {
  unsigned uid, euid, gid, egid;
  unsigned long long cap_eff, cap_amb;
  int valid;
} perf_credential_t;

/* Read each Linux thread's own credentials, not the process leader's copy. This is enabled
   only for untimed perf replays and happens after the worker has finished measured work. */
static perf_credential_t perf_credentials(void) {
  perf_credential_t result = {0};
  FILE* f = fopen("/proc/thread-self/status", "r");
  if (f == NULL) return result;
  char line[256];
  int got_uid = 0, got_gid = 0, got_eff = 0, got_amb = 0;
  while (fgets(line, sizeof(line), f) != NULL) {
    unsigned ignored1, ignored2;
    if (sscanf(line, "Uid:\t%u\t%u\t%u\t%u", &result.uid, &result.euid, &ignored1, &ignored2) == 4) got_uid = 1;
    if (sscanf(line, "Gid:\t%u\t%u\t%u\t%u", &result.gid, &result.egid, &ignored1, &ignored2) == 4) got_gid = 1;
    if (sscanf(line, "CapEff:\t%llx", &result.cap_eff) == 1) got_eff = 1;
    if (sscanf(line, "CapAmb:\t%llx", &result.cap_amb) == 1) got_amb = 1;
  }
  fclose(f);
  result.valid = got_uid && got_gid && got_eff && got_amb;
  return result;
}

static void print_perf_credentials(const perf_credential_t* credential) {
  printf("{\"uid\":%u,\"euid\":%u,\"gid\":%u,\"egid\":%u,"
         "\"cap_eff\":\"%016llx\",\"cap_amb\":\"%016llx\",\"valid\":%s}",
         credential->uid, credential->euid, credential->gid, credential->egid,
         credential->cap_eff, credential->cap_amb, credential->valid ? "true" : "false");
}
#endif

typedef struct {
  uint64_t rng;
#if defined(PERF_AB_DIAGNOSTIC)
  long completed;
  perf_credential_t credential;
#endif
  size_t lo, hi; int log_sizes; long ops; int slots; void* slot[MAX_SLOTS]; size_t size[MAX_SLOTS];
  int fifo[4096]; size_t head, tail; double cpu; int index;
} stream_t;

/* #506: one Larson table; a round of draws on it runs on one thread at a time (the round barrier) */
typedef struct { uint64_t rng; void** slot; size_t* log; size_t log_len, log_cap; } table_t;

static uint64_t next(uint64_t* s) {
  uint64_t z = (*s += 0x9e3779b97f4a7c15ull);
  z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ull;
  z = (z ^ (z >> 27)) * 0x94d049bb133111ebull;
  return z ^ (z >> 31);
}

static int bit_length(size_t x) { int n = 0; while (x != 0) { n++; x >>= 1; } return n; }

/* the next request size. Log-uniform is rust/benchmark-suite ScalingStream::draw_size with
   log_uniform (sparse-large-buffers, #527): an octave uniformly between the bit lengths of lo and
   hi, then a uniform offset inside it, clamped to [lo, hi] -- so 64 KiB-4 MiB puts 1 in 7 draws
   at exactly 4 MiB. The same distribution, not the suite's seeded stream. */
static size_t draw_size(stream_t* st) {
  if (!st->log_sizes) return st->lo + (size_t)(next(&st->rng) % (st->hi - st->lo + 1));
  const int low = bit_length(st->lo), high = bit_length(st->hi);
  const int octave = low + (int)(next(&st->rng) % (uint64_t)(high - low + 1));
  const size_t base = (size_t)1 << (octave - 1);
  const size_t size = base + (size_t)(next(&st->rng) % base);
  return size < st->lo ? st->lo : (size > st->hi ? st->hi : size);
}

static void run_ops(stream_t* st, long n) {
  for (long i = 0; i < n; i++) {
    const uint64_t choice = next(&st->rng) % 16;
    int slot = (int)(next(&st->rng) % (uint64_t)st->slots);
    if (choice >= 8 && choice < 14 && st->head != st->tail) slot = st->fifo[st->head++ % 4096];  /* free oldest */
    if (choice >= 8) {
      mi_free(st->slot[slot]); st->slot[slot] = NULL; st->size[slot] = 0;
      continue;
    }
    mi_free(st->slot[slot]);
    const size_t size = draw_size(st);
    char* p = (char*)mi_malloc(size);
    if (p == NULL) { fprintf(stderr, "allocation failed\n"); exit(1); }
    for (size_t off = 0; off < size; off += 4096) p[off] = (char)off;
    st->slot[slot] = p;
    st->size[slot] = size;
    st->fifo[st->tail++ % 4096] = slot;
  }
#if defined(PERF_AB_DIAGNOSTIC)
  st->completed += n;  /* one batch update, no per-operation instrumentation */
#endif
}

static double cpu_of(const struct rusage* ru) {
  return (double)(ru->ru_utime.tv_sec + ru->ru_stime.tv_sec) + (double)(ru->ru_utime.tv_usec + ru->ru_stime.tv_usec) * 1e-6;
}

static long rss_bytes(void);
static long peak_rss_bytes(void);
static double now_s(void);

#if defined(PERF_AB_DIAGNOSTIC)
typedef struct { struct rusage usage; double monotonic_s; long rss_bytes, peak_rss_bytes; } phase_snapshot_t;

/* At work start, finish /proc reads before the boundary so their CPU/fault cost is not charged
   to the allocator workload. The end snapshot takes the clock/rusage first so its /proc reads
   likewise do not inflate measured-work CPU. */
static phase_snapshot_t phase_snapshot_start(void) {
  phase_snapshot_t out;
  out.rss_bytes = rss_bytes();
  out.peak_rss_bytes = peak_rss_bytes();
  if (getrusage(RUSAGE_SELF, &out.usage) != 0) { perror("getrusage"); exit(1); }
  out.monotonic_s = now_s();
  return out;
}

static phase_snapshot_t phase_snapshot(void) {
  phase_snapshot_t out;
  out.monotonic_s = now_s();
  if (getrusage(RUSAGE_SELF, &out.usage) != 0) { perror("getrusage"); exit(1); }
  out.rss_bytes = rss_bytes();
  out.peak_rss_bytes = peak_rss_bytes();
  return out;
}

static void print_phase_snapshot(const phase_snapshot_t* s) {
  printf("{\"monotonic_s\":%.9f,\"user_s\":%.6f,\"system_s\":%.6f,"
         "\"minor_faults\":%ld,\"major_faults\":%ld,\"voluntary_context_switches\":%ld,"
         "\"involuntary_context_switches\":%ld,\"rss_bytes\":%ld,\"peak_rss_bytes\":%ld}",
         s->monotonic_s,
         (double)s->usage.ru_utime.tv_sec + (double)s->usage.ru_utime.tv_usec * 1e-6,
         (double)s->usage.ru_stime.tv_sec + (double)s->usage.ru_stime.tv_usec * 1e-6,
         s->usage.ru_minflt, s->usage.ru_majflt, s->usage.ru_nvcsw, s->usage.ru_nivcsw,
         s->rss_bytes, s->peak_rss_bytes);
}
#endif

/* the calling thread's cpu so far: a worker adds it to its stream just before it exits or idles */
static double thread_cpu(void) { struct rusage ru; getrusage(RUSAGE_THREAD, &ru); return cpu_of(&ru); }

static int generations;
static long pause_ms;
static int threads, table_slots;
static table_t* tables;
static pthread_barrier_t round_barrier;
static atomic_int drained, release_workers;
#if defined(PERF_AB_DIAGNOSTIC)
static atomic_int credentials_ready;
static int credentials_enabled;
static perf_credential_t process_credentials;
#endif
static int holes_report;   /* PERF_AB_HOLES_REPORT (#529) */
static pthread_barrier_t report_barrier;
static pthread_mutex_t report_lock = PTHREAD_MUTEX_INITIALIZER;

static void* generation_main(void* arg) {
  stream_t* st = (stream_t*)arg;
  run_ops(st, st->ops / generations);
  st->cpu += thread_cpu();
  return NULL;  /* exits still owning st->slot[] */
}

static void free_slots(stream_t* st) {
  for (int i = 0; i < st->slots; i++) { mi_free(st->slot[i]); st->slot[i] = NULL; st->size[i] = 0; }
}

/* one round of Larson draws on `tb`: free a random slot's block, allocate a new one into it */
static void larson_round(table_t* tb, stream_t* st, size_t lo, size_t hi, long n) {
#if !defined(PERF_AB_DIAGNOSTIC)
  (void)st;  /* the default perf-ab workload does not compute a trace checksum */
#endif
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
#if defined(PERF_AB_DIAGNOSTIC)
  st->completed += n;
#endif
}

/* #529 (#422 E4): the allocator-internal snapshot. Every worker first waits until all have finished
   their stream, so nothing churns while one looks; then, one at a time, each prints what it still
   holds and mi_purge_holes_report() -- which reads only the calling thread's own pages (plus the
   arenas, the same for every worker), hence one report per worker. Untimed: perf_ab.py runs it
   in a separate replay. */
/* #575: the kernel's own residency numbers for the whole process, next to the allocator's split */
static void smaps_rollup_line(void) {
  FILE* f = fopen("/proc/self/smaps_rollup", "r");
  if (f == NULL) return;
  char line[256];
  long rss = -1, anon_huge = -1, anon = -1;
  while (fgets(line, sizeof(line), f) != NULL) {
    sscanf(line, "Rss: %ld kB", &rss);
    sscanf(line, "Anonymous: %ld kB", &anon);
    sscanf(line, "AnonHugePages: %ld kB", &anon_huge);
  }
  fclose(f);
  fprintf(stderr, "smaps_rollup: Rss %ld kB, Anonymous %ld kB, AnonHugePages %ld kB\n", rss, anon, anon_huge);
}

static void report_holes(stream_t* st) {
  size_t live = 0;
  int held = 0;
  for (int i = 0; i < st->slots; i++) { if (st->slot[i] != NULL) { live += st->size[i]; held++; } }
  pthread_barrier_wait(&report_barrier);
  pthread_mutex_lock(&report_lock);
  mi_purge_holes_stats_t hs;
  mi_purge_holes_stats_get(&hs);
  fprintf(stderr, "\n=== worker %d of %d: %d live slots, %zu live requested bytes; process RSS %ld bytes\n"
          "purge_holes stats: discarded now %zu B in %zu blocks, ever %zu B, %zu discard calls, %zu pages freed, "
          "unformed discarded now %zu B (ever %zu B), %zu pages skipped, %zu full sweeps\n",
          st->index, threads, held, live, rss_bytes(),
          hs.purged_bytes, hs.purged_blocks, hs.purged_bytes_total, hs.discard_calls, hs.pages_freed,
          hs.unformed_bytes, hs.unformed_bytes_total, hs.pages_skipped, hs.full_sweeps);
  smaps_rollup_line();   /* #575 */
  fflush(stderr);
  mi_purge_holes_report();
  fflush(stderr);
  pthread_mutex_unlock(&report_lock);
  pthread_barrier_wait(&report_barrier);
}

static void* worker_main(void* arg) {
  stream_t* st = (stream_t*)arg;
  if (table_slots > 0) {
    for (int r = 0; r < LARSON_ROUNDS; r++) {
      larson_round(&tables[(st->index + r) % threads], st, st->lo, st->hi, st->ops / LARSON_ROUNDS);
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
  if (holes_report) report_holes(st);
  free_slots(st);
  st->cpu += thread_cpu();
  atomic_fetch_add(&drained, 1);
#if defined(PERF_AB_DIAGNOSTIC)
  if (credentials_enabled) {
    st->credential = perf_credentials();
    atomic_fetch_add(&credentials_ready, 1);
  }
#endif
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

/* The process's own peak RSS: VmHWM, the high-water mark of this process image. Not getrusage's
   ru_maxrss: Linux carries that across execve from the image it replaces -- the forked
   perf_ab.py interpreter, ~16 MiB -- so every row whose true peak is below that read the parent's
   RSS as its peak (#529: all the 1-worker exact-size rows showed 15.69 MiB). */
static long peak_rss_bytes(void) {
  char line[256];
  long kib = -1;
  FILE* f = fopen("/proc/self/status", "r");
  if (f == NULL) exit(1);
  while (fgets(line, sizeof(line), f) != NULL) {
    if (sscanf(line, "VmHWM: %ld kB", &kib) == 1) break;
  }
  fclose(f);
  if (kib < 0) exit(1);
  return kib * 1024L;
}

static double now_s(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return (double)t.tv_sec + (double)t.tv_nsec * 1e-9; }

/* #527: the bin index of a request. Not in the public header, but an ordinary function of the
   static library (src/page-queue.c) on every revision perf-ab compares, so the probe reports the
   linked build's own answer -- MI_EXTRA_CPPDEFS included -- instead of a copy of its formula. */
extern size_t _mi_bin(size_t size);

typedef struct { size_t block_size, blocks, used; } probe_area_t;

static bool probe_visit(const mi_heap_t* heap, const mi_heap_area_t* area, void* block, size_t block_size, void* arg) {
  (void)heap; (void)block; (void)block_size;
  probe_area_t* found = (probe_area_t*)arg;
  if (area->used > 0 && area->full_block_size > 0) {
    found->block_size = area->full_block_size;
    found->blocks = area->reserved / area->full_block_size;
    found->used = area->used;
  }
  return true;
}

/* One line per size: size, bin, the page's block size, its blocks per page, and the page kind.
   Each size is allocated alone in a fresh heap, so the only page a heap walk finds is the one that
   request landed in. The kind is read off that page: one block is a singleton page, otherwise the
   smallest page size (types.h) that holds all its blocks -- small 64 KiB, medium 512 KiB, large
   4 MiB. */
static int probe(int count, char** sizes) {
  for (int i = 0; i < count; i++) {
    const size_t size = (size_t)atol(sizes[i]);
    mi_heap_t* heap = mi_heap_new();
    void* p = (heap == NULL ? NULL : mi_heap_malloc(heap, size));
    if (p == NULL) { fprintf(stderr, "probe: allocation of %zu failed\n", size); return 1; }
    probe_area_t found = { 0, 0, 0 };
    mi_heap_visit_blocks(heap, false, &probe_visit, &found);
    if (found.used != 1) { fprintf(stderr, "probe: %zu: expected one page with one block\n", size); return 1; }
    const size_t span = found.block_size * found.blocks;
    const char* kind = (found.blocks == 1 ? "singleton"
                      : span <= (size_t)PROBE_SMALL_PAGE ? "small"
                      : span <= (size_t)PROBE_MEDIUM_PAGE ? "medium"
                      : span <= (size_t)PROBE_LARGE_PAGE ? "large" : "unknown");
    printf("%zu %zu %zu %zu %s\n", size, _mi_bin(size), found.block_size, found.blocks, kind);
    mi_free(p);
    mi_heap_delete(heap);
  }
  return 0;
}

int main(int argc, char** argv) {
  if (argc >= 2 && strcmp(argv[1], "probe") == 0) return probe(argc - 2, argv + 2);
  if (argc != 11) { fprintf(stderr, "usage: perf_ab threads generations min max ops pause_ms table_slots uniform|log slots release_bound_ms\n       perf_ab probe size...\n"); return 2; }
  pause_ms = atol(argv[6]);
  table_slots = atoi(argv[7]);
  if (strcmp(argv[8], "uniform") != 0 && strcmp(argv[8], "log") != 0) { fprintf(stderr, "sizes: uniform or log, got %s\n", argv[8]); return 2; }
  const int log_sizes = (strcmp(argv[8], "log") == 0);
  const int slots = atoi(argv[9]);
  if (slots < 1 || slots > MAX_SLOTS) { fprintf(stderr, "slots: 1..%d, got %s\n", MAX_SLOTS, argv[9]); return 2; }
  const long bound_ms = atol(argv[10]);
  const long samples = 2 * bound_ms / RELEASE_SAMPLE_MS + 1;   /* RSS every RELEASE_SAMPLE_MS up to twice the bound */
  long* rss_at = (long*)calloc((size_t)samples, sizeof(long));
  threads = atoi(argv[1]);
  generations = atoi(argv[2]);
#if defined(PERF_AB_DIAGNOSTIC)
  credentials_enabled = getenv("PERF_AB_PERF_CREDENTIALS") != NULL;
  if (credentials_enabled) process_credentials = perf_credentials();
#endif
  holes_report = (getenv("PERF_AB_HOLES_REPORT") != NULL);
  if (holes_report) pthread_barrier_init(&report_barrier, NULL, (unsigned)threads);
  stream_t* st = (stream_t*)calloc((size_t)threads, sizeof(stream_t));
  pthread_t* t = (pthread_t*)calloc((size_t)threads, sizeof(pthread_t));
  for (int i = 0; i < threads; i++) {
    st[i].rng = PERF_AB_STREAM_SEED_BASE + (uint64_t)i;
    st[i].lo = (size_t)atol(argv[3]); st[i].hi = (size_t)atol(argv[4]); st[i].ops = atol(argv[5]);
    st[i].log_sizes = log_sizes;
    st[i].slots = slots;
    st[i].index = i;
  }
  if (table_slots > 0) {   /* allocated before the clock starts, and by libc: not the allocator under test */
    tables = (table_t*)calloc((size_t)threads, sizeof(table_t));
    for (int i = 0; i < threads; i++) {
      tables[i].rng = PERF_AB_LARSON_TABLE_SEED_BASE + (uint64_t)i;
      tables[i].slot = (void**)calloc((size_t)table_slots, sizeof(void*));
      tables[i].log_cap = (size_t)table_slots;
      tables[i].log = (size_t*)mi_malloc(tables[i].log_cap * sizeof(size_t));
    }
    pthread_barrier_init(&round_barrier, NULL, (unsigned)threads);
  }
#if defined(PERF_AB_DIAGNOSTIC)
  const phase_snapshot_t work_start = phase_snapshot_start();
#else
  const double start = now_s();
#endif
  const long baseline_rss = rss_bytes();   /* #573: the RSS floor is this plus the live requested bytes */
  for (int i = 0; i < threads; i++) pthread_create(&t[i], NULL, &worker_main, &st[i]);
  while (atomic_load(&drained) < threads) usleep(100);
#if defined(PERF_AB_DIAGNOSTIC)
  const phase_snapshot_t work_end = phase_snapshot();
#else
  const double elapsed = now_s() - start;
  struct rusage ru; getrusage(RUSAGE_SELF, &ru);   /* (re-read at DRAIN_SHORT_MS) */
#endif
#if !defined(PERF_AB_DIAGNOSTIC)
  long rss_short = 0;
  long rss_peak = 0;
#endif
  const double drained_at = now_s();
  for (long i = 0; i < samples; i++) {   /* sample i at drained_at + i * RELEASE_SAMPLE_MS, without drift */
    const double wait = drained_at + (double)(i * RELEASE_SAMPLE_MS) * 1e-3 - now_s();
    if (wait > 0) usleep((useconds_t)(wait * 1e6));
    rss_at[i] = rss_bytes();
#if !defined(PERF_AB_DIAGNOSTIC)
    if (i * RELEASE_SAMPLE_MS == DRAIN_SHORT_MS) {
      rss_short = rss_at[i];
      getrusage(RUSAGE_SELF, &ru);
      rss_peak = peak_rss_bytes();
    }
#endif
  }
  long live_bytes = 0;   /* every worker is done and holds its live slots (`drained` was released) */
  for (int i = 0; i < threads; i++) {
    for (int k = 0; k < st[i].slots; k++) { if (st[i].slot[k] != NULL) live_bytes += (long)st[i].size[k]; }
  }
  const long ideal_rss = baseline_rss + live_bytes;
  (void)ideal_rss;   /* (printed by the default output only; the diagnostic JSON schema is fixed) */
  const long rss_final = rss_at[samples - 1];
  long release_ms = 0;
  while (release_ms / RELEASE_SAMPLE_MS < samples - 1 && rss_at[release_ms / RELEASE_SAMPLE_MS] > rss_final + RELEASE_TOLERANCE) {
    release_ms += RELEASE_SAMPLE_MS;
  }
  double owner_cpu = 0;
  for (int i = 0; i < threads; i++) owner_cpu += st[i].cpu;
#if defined(PERF_AB_DIAGNOSTIC)
  {
    const phase_snapshot_t drain_end = phase_snapshot();
    uint64_t checksum = 0;
    long completed = 0;
    for (int i = 0; i < threads; i++) {
      completed += st[i].completed;
      /* A deterministic stream's final PRNG state, seed, parameters and operation count identify
         its draw sequence. Hash them after work, so checksum collection adds no hot-loop cost. */
      uint64_t state = st[i].rng ^ (uint64_t)st[i].completed ^ (uint64_t)st[i].lo
                     ^ ((uint64_t)st[i].hi << 1) ^ ((uint64_t)st[i].slots << 32) ^ (uint64_t)i;
      if (table_slots > 0) state ^= tables[i].rng ^ (uint64_t)table_slots;
      checksum ^= next(&state);
    }
    printf("{\"protocol_version\":\"perf-ab-child-diagnostic-v1\",\"phase_definition\":\"%s\",\"work_start\":",
           PERF_AB_PHASE_DEFINITION);
    print_phase_snapshot(&work_start);
    printf(",\"work_end\":");
    print_phase_snapshot(&work_end);
    printf(",\"drain_start\":");
    print_phase_snapshot(&work_end);
    printf(",\"drain_end\":");
    print_phase_snapshot(&drain_end);
    printf(",\"worker_cpu_s\":%.6f,\"completed_operations\":%ld,\"trace_checksum\":\"%016llx\","
           "\"stream_seed_base\":\"%016llx\",\"larson_table_seed_base\":\"%016llx\","
           "\"peak_work_rss_bytes\":%ld,\"rss_after_drain_bytes\":%ld,\"rss_at_release_bound_bytes\":%ld,"
           "\"release_ms\":%ld", owner_cpu, completed, (unsigned long long)checksum,
           (unsigned long long)PERF_AB_STREAM_SEED_BASE, (unsigned long long)PERF_AB_LARSON_TABLE_SEED_BASE,
           work_end.peak_rss_bytes,
           rss_final, /* full drain-window end; the default publication still uses rss_short */
           rss_at[bound_ms / RELEASE_SAMPLE_MS], release_ms);
    if (credentials_enabled) {
      while (atomic_load(&credentials_ready) < threads) usleep(100);
      printf(",\"perf_credentials\":{\"process\":");
      print_perf_credentials(&process_credentials);
      printf(",\"workers\":[");
      for (int i = 0; i < threads; i++) {
        if (i != 0) printf(",");
        print_perf_credentials(&st[i].credential);
      }
      printf("]}");
    }
    printf("}\n");
  }
#else
  {
    printf("%.1f %.4f %.4f %ld %ld %ld %ld %ld %ld\n", (double)threads * (double)st[0].ops / elapsed, cpu_of(&ru),
           owner_cpu, ru.ru_minflt, rss_peak, rss_short, rss_at[bound_ms / RELEASE_SAMPLE_MS], release_ms, ideal_rss);
  }
#endif
  atomic_store(&release_workers, 1);
  for (int i = 0; i < threads; i++) pthread_join(t[i], NULL);
  if (table_slots > 0) {
    for (int i = 0; i < threads; i++) free(tables[i].slot);
    free(tables);
    pthread_barrier_destroy(&round_barrier);
  }
  if (holes_report) pthread_barrier_destroy(&report_barrier);
  free(st); free(t); free(rss_at);
  return 0;
}
