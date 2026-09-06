#!/usr/bin/env python3
"""Measure "does this allocator give memory back after the process goes idle?" across
the four allocators the benchmark lockfile pins.

`ci/bench_hole_purging.py` answers a narrower question -- what does *this* tree's hole
purging add over its own scavenger, `MIMALLOC_PURGE_HOLES=0` vs `=1` in one binary. It
cannot answer "and how does that compare to what everyone else does", because a
competitor allocator has no `MIMALLOC_PURGE_HOLES`. This script runs the *same* churn
workload -- 150k x 512 B + 100k x 1 KiB + 50k x 2 KiB, a scattered 1-in-20 kept alive,
the rest freed, then 10 s idle -- once per allocator, each linked against the static
library the benchmark lockfile builds, and charts resident set size over that idle
window.

Series (five charted):

    mimalloc-pprof              this tree, default config (scavenger + hole purging on)
    Bun mimalloc                oven-sh/mimalloc at the lockfile pin, default config
    Microsoft mimalloc          microsoft/mimalloc at the lockfile pin, default config
    jemalloc                    default config -- nothing is called during idle
    jemalloc + explicit purge   the same jemalloc, with an explicit
                                `mallctl("arena.<all>.purge")` every 100 ms

The two jemalloc series exist because the honest claim is narrow: jemalloc *can*
return this memory when an embedder asks it to, it just does not do it on its own
when the process stops allocating. jemalloc's decay is advanced by allocation
activity (or by a `background_thread` that is off by default), so an idle process
never fires it. Both series are charted in the same hue -- one allocator, two
conditions -- so the chart shows the difference between "returns memory after idle"
and "returns memory when asked", not a rigged comparison.

Fairness rules baked into the driver (`CHURN_C_SOURCE`):

  * the block table is `mmap`ed and RSS is read with `open`/`read`/`close` into a
    stack buffer, and output goes through `write(2)`. Nothing in the measurement
    harness itself calls `malloc`. This matters: `libmimalloc.a` carries
    `alloc-override.c`, so in the three mimalloc children *every* libc `malloc`
    in the process is mimalloc's, while in the jemalloc children (built with
    `--with-jemalloc-prefix=je_`, no override) it is glibc's. A `printf` per idle
    tick would be allocator traffic in one arm and not the other -- and allocator
    traffic during "idle" is exactly what jemalloc's decay needs to fire.
  * peak RSS is `VmHWM` (the kernel's own high-water mark, taken at the end), not
    `max(sample)`. Sampling only starts at the top of the idle window, so a sampled
    maximum would be post-free RSS, not the real peak.
  * `taskset -c 0-3`, identical workload constants, identical `-O2` driver build; the
    only difference between children is which static library they link.
  * runs are repeated (default 3) and the run with the LOWEST after-idle RSS is
    charted -- the same rule for every series, generous to every allocator,
    including the ones this fork is being compared against. Every run's numbers go
    into the report JSON.

Two extra, uncharted diagnostic series are measured and recorded in the JSON so a
reviewer can check the obvious objections without re-running anything:
`mimalloc-pprof` with *no* idle hook at all (does the background scavenger alone do
it?) and `upstream-mimalloc` calling `mi_collect(false)` on the same tick (is
upstream's flat line just a missing API?).

The sizing run (`--busy-threads N`, #365 §6 / #366)
----------------------------------------------------

Everything above is single-threaded: the one worker calls the idle mechanism from
inside its own idle loop, so it says nothing about the case that matters for a
process-wide purge -- N threads that are BUSY and never idle, and a purge issued by a
thread that is not one of them. `--busy-threads N` (default 4 when given) runs the same
churn workload split across N worker threads which then stay in a hot-set
`malloc`/`free` loop for the whole 10 s window, never calling any idle hook, while the
main thread -- which allocates nothing -- issues the purge every 100 ms tick and
samples RSS. One row per (allocator, purge call):

    mimalloc-pprof                    mi_collect(true)            caller-only, by design
    mimalloc-pprof                    mi_purge_all(true)          default build: arenas + parked
    mimalloc-pprof, MI_OWNER_GATE=ON  mi_purge_all(true)          gated build: every thread
    Bun mimalloc / Microsoft mimalloc  mi_collect(true)
    jemalloc                          mallctl("arena.<all>.purge")
    glibc                             malloc_trim(0)              no allocator linked at all

plus a "nothing" control per fork build (does the background scavenger alone reach a
busy thread?). The gated fork is this tree built a second time with
`-DMI_OWNER_GATE=ON`; glibc is the driver linked against nothing. For the `mi_purge_all`
rows the driver also reports the last return status and the largest `theaps_pending`
the call ever returned, so an ungated `PARTIAL, 4 pending` is on the table next to its
number rather than hidden behind it. These are the numbers the README feature-table
cell "Process-wide eager purge, from any thread" cites.

Usage:
    bench_hole_purging_allocators.py --build-root <scratch dir> [--jobs N] [--table]
    bench_hole_purging_allocators.py --check      # re-render committed data, no runs
    bench_hole_purging_allocators.py --build-root <dir> --busy-threads 4   # sizing run
    bench_hole_purging_allocators.py --check --busy-threads 4              # its --check

Outputs (under --out-dir, default .github/assets):
    allocator-idle-rss.csv                   the charted run's (series, t, rss_mb) samples
    allocator-idle-report.json               every run's numbers + machine/pins/idle hooks
    allocator-idle-rss-{light,dark}.svg      the RSS-over-idle line chart (default action)
    allocator-idle-table-{light,dark}.svg    peak / after-idle / % returned (--table)
    allocator-purge-any-thread-report.json   the sizing run (--busy-threads)
    allocator-purge-any-thread-table-{light,dark}.svg   its table
"""

from __future__ import annotations

# `build_benchmark_allocators` is a sibling script in ci/, not an installed package;
# it is importable because this file's own directory is on sys.path when run as
# `uv run ci/bench_hole_purging_allocators.py`, and because pyproject's
# `pythonpath = ["ci"]` puts it there under pytest. No sys.path surgery.
# ruff: noqa: I001

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, TypedDict

import build_benchmark_allocators as builder

REPO_ROOT = Path(__file__).resolve().parent.parent

# tcmalloc is in the same lockfile but needs bazel, which is not part of this
# measurement's toolchain; it is skipped by name rather than silently dropped.
NEEDED_ALLOCATORS = ("jemalloc", "upstream-mimalloc", "bun-mimalloc", "mimalloc-pprof")
SKIPPED_ALLOCATORS = ("tcmalloc",)


# ---------------------------------------------------------------------------
# The workload driver. Embedded rather than living under test/ (CLAUDE.md rules
# 2 and 6: this is a benchmark driver, not a correctness test).
#
# One source, compiled once per series. The build supplies:
#   BENCH_FAMILY_MIMALLOC  1 for the mimalloc children, 0 for jemalloc
#   BENCH_IDLE_MODE        0 nothing, 1 mi_on_thread_idle, 2 mi_collect, 3 je purge
# ---------------------------------------------------------------------------
CHURN_C_SOURCE = r"""
#include <fcntl.h>
#include <stdbool.h>
#include <stddef.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

#if BENCH_FAMILY_MIMALLOC
#include <mimalloc.h>
#define BENCH_MALLOC(n) mi_malloc(n)
#define BENCH_FREE(p)   mi_free(p)
#else
#include <jemalloc/jemalloc.h>
#define BENCH_MALLOC(n) je_malloc(n)
#define BENCH_FREE(p)   je_free(p)
#endif

#define BENCH_STRINGIFY2(x) #x
#define BENCH_STRINGIFY(x) BENCH_STRINGIFY2(x)

/* Idle mechanisms. 0 is "a normal idle process": the driver blocks in nanosleep
   and calls nothing at all. Anything else is an explicit cooperation point an
   embedder with an event loop would place right before blocking. */
#define BENCH_IDLE_NOTHING       0
#define BENCH_IDLE_ON_THREAD_IDLE 1
#define BENCH_IDLE_MI_COLLECT    2
#define BENCH_IDLE_JE_PURGE      3
#define BENCH_IDLE_MI_COLLECT_FORCE 4

static void bench_on_idle(void) {
#if BENCH_IDLE_MODE == BENCH_IDLE_ON_THREAD_IDLE
  mi_on_thread_idle();
#elif BENCH_IDLE_MODE == BENCH_IDLE_MI_COLLECT
  mi_collect(false);
#elif BENCH_IDLE_MODE == BENCH_IDLE_MI_COLLECT_FORCE
  /* `true` forces the purge instead of honouring purge_delay, which is 1000 ms on
     upstream. Without this row, "upstream returns 18% even when told to collect"
     invites the obvious objection that upstream simply lost to its own delay. */
  mi_collect(true);
#elif BENCH_IDLE_MODE == BENCH_IDLE_JE_PURGE
  /* MALLCTL_ARENAS_ALL is jemalloc's own "every arena" pseudo-index; building the
     name by stringifying the macro means a jemalloc that renumbers it cannot leave
     this pointed at a real arena index without the name failing to resolve. */
  (void)je_mallctl("arena." BENCH_STRINGIFY(MALLCTL_ARENAS_ALL) ".purge",
                   NULL, NULL, NULL, 0);
#else
  /* BENCH_IDLE_NOTHING: deliberately empty. */
#endif
}

/* ---- allocation-free process instrumentation ---------------------------- */

static long status_kb(const char* key, size_t key_len) {
  const int fd = open("/proc/self/status", O_RDONLY);
  if (fd < 0) return -1;
  char buf[8192];
  const ssize_t n = read(fd, buf, sizeof(buf) - 1);
  close(fd);
  if (n <= 0) return -1;
  buf[n] = '\0';
  const char* p = buf;
  while (*p != '\0') {
    if (strncmp(p, key, key_len) == 0) {
      p += key_len;
      while (*p == ' ' || *p == '\t') p++;
      long kb = 0;
      while (*p >= '0' && *p <= '9') { kb = kb * 10 + (*p - '0'); p++; }
      return kb;
    }
    while (*p != '\0' && *p != '\n') p++;
    if (*p == '\n') p++;
  }
  return -1;
}

static size_t fmt_long(char* out, long v) {
  char tmp[24];
  size_t n = 0;
  size_t o = 0;
  if (v < 0) { out[o++] = '-'; v = -v; }
  if (v == 0) tmp[n++] = '0';
  while (v > 0) { tmp[n++] = (char)('0' + (v % 10)); v /= 10; }
  while (n > 0) out[o++] = tmp[--n];
  return o;
}

static void emit_row(const char* tag, long a, long b, int two) {
  char line[96];
  size_t o = 0;
  while (*tag != '\0') line[o++] = *tag++;
  line[o++] = ',';
  o += fmt_long(line + o, a);
  if (two) { line[o++] = ','; o += fmt_long(line + o, b); }
  line[o++] = '\n';
  ssize_t written = write(1, line, o);
  (void)written;
}

static void sleep_ms(long ms) {
  struct timespec ts;
  ts.tv_sec = ms / 1000;
  ts.tv_nsec = (ms % 1000) * 1000000L;
  nanosleep(&ts, NULL);
}

/* ---- the churn workload ------------------------------------------------- */

typedef struct { size_t size; size_t count; } size_class_t;

int main(int argc, char** argv) {
  int seconds = 10;
  if (argc > 1) {
    seconds = 0;
    for (const char* p = argv[1]; *p >= '0' && *p <= '9'; p++) seconds = seconds * 10 + (*p - '0');
    if (seconds <= 0) seconds = 10;
  }
  const long tick_ms = 100;

  static const size_class_t classes[] = {
    { 512,  150000 },
    { 1024, 100000 },
    { 2048,  50000 },
  };
  const size_t n_classes = sizeof(classes) / sizeof(classes[0]);

  size_t total = 0;
  for (size_t c = 0; c < n_classes; c++) total += classes[c].count;

  /* mmap, not malloc: the block table must not land inside the allocator under
     test, or the three mimalloc children would carry ~2.4 MB the jemalloc
     children do not. */
  const size_t table_bytes = total * sizeof(void*);
  void** blocks = (void**)mmap(NULL, table_bytes, PROT_READ | PROT_WRITE,
                               MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
  if (blocks == MAP_FAILED) return 1;

  /* Allocate every block, size classes interleaved so survivors land scattered
     across pages of every class rather than clustered by allocation order. */
  size_t idx = 0;
  size_t remaining[3];
  for (size_t c = 0; c < n_classes; c++) remaining[c] = classes[c].count;
  size_t class_cursor = 0;
  while (idx < total) {
    size_t tries = 0;
    while (remaining[class_cursor] == 0 && tries < n_classes) {
      class_cursor = (class_cursor + 1) % n_classes;
      tries++;
    }
    blocks[idx] = BENCH_MALLOC(classes[class_cursor].size);
    if (blocks[idx] != NULL) memset(blocks[idx], 0xAB, classes[class_cursor].size);
    remaining[class_cursor]--;
    class_cursor = (class_cursor + 1) % n_classes;
    idx++;
  }

  /* Keep 1-in-20 alive (scattered survivors); free the rest. */
  for (size_t i = 0; i < total; i++) {
    if (i % 20 != 0) {
      BENCH_FREE(blocks[i]);
      blocks[i] = NULL;
    }
  }

  /* Idle window. The only thing that happens here is nanosleep, one RSS read,
     one write(2) and whichever idle mechanism this child was built with. */
  long elapsed_ms = 0;
  while (elapsed_ms <= (long)seconds * 1000) {
    emit_row("CSV", elapsed_ms, status_kb("VmRSS:", 6), 1);
    sleep_ms(tick_ms);
    bench_on_idle();
    elapsed_ms += tick_ms;
  }

  /* VmHWM is monotonic, so reading it last still reports the allocation-phase
     peak -- which sampling, starting only at the top of the idle window, misses. */
  emit_row("HWM", status_kb("VmHWM:", 6), 0, 0);

#if !BENCH_FAMILY_MIMALLOC
  /* Report what jemalloc actually ended up configured with, not what the caller
     believed it asked for. A prefixed jemalloc reads JE_MALLOC_CONF, not
     MALLOC_CONF, and it ignores an unknown variable in complete silence -- so a
     diagnostic row can otherwise claim to have enabled a knob that never turned on.
     The Python side treats a mismatch as fatal. */
  {
    bool background_thread = false;
    size_t size = sizeof(background_thread);
    (void)je_mallctl("background_thread", &background_thread, &size, NULL, 0);
    emit_row("BACKGROUND_THREAD", (long)background_thread, 0, 0);
  }
#endif

  /* Keep survivors reachable until here so no optimizer can decide the table is
     dead and free them early; then release everything. */
  for (size_t i = 0; i < total; i += 20) BENCH_FREE(blocks[i]);
  munmap(blocks, table_bytes);
  return 0;
}
"""


# ---------------------------------------------------------------------------
# The sizing-run driver (#365 §6): N busy worker threads, purge from main.
#
# A second source rather than more #ifs in the first: the single-thread driver is
# what the committed chart was measured with, and its bytes stay put. The build
# supplies:
#   BENCH_FAMILY        0 jemalloc (je_ prefix), 1 mimalloc, 2 glibc (nothing linked)
#   BENCH_PURGE_MODE    what the main thread calls every tick (PURGE_* below)
#   BENCH_THREADS       N
# Same fairness rules as above: mmap'ed tables, /proc read into a stack buffer,
# write(2) output. The only allocator traffic on the main thread is the purge call.
# ---------------------------------------------------------------------------
BUSY_C_SOURCE = r"""
#include <fcntl.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stddef.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

#if BENCH_FAMILY == 1
#include <mimalloc.h>
#define BENCH_MALLOC(n) mi_malloc(n)
#define BENCH_FREE(p)   mi_free(p)
#elif BENCH_FAMILY == 0
#include <jemalloc/jemalloc.h>
#define BENCH_MALLOC(n) je_malloc(n)
#define BENCH_FREE(p)   je_free(p)
#else
#include <malloc.h>
#include <stdlib.h>
#define BENCH_MALLOC(n) malloc(n)
#define BENCH_FREE(p)   free(p)
#endif

#define BENCH_STRINGIFY2(x) #x
#define BENCH_STRINGIFY(x) BENCH_STRINGIFY2(x)

#define BENCH_PURGE_NOTHING          0
#define BENCH_PURGE_MI_COLLECT_FORCE 1
#define BENCH_PURGE_MI_PURGE_ALL     2
#define BENCH_PURGE_JE_PURGE         3
#define BENCH_PURGE_MALLOC_TRIM      4

/* What the purge reported back, for the row's own cell: the last status and the
   largest pending count any tick returned. Only the mi_purge_all mode fills them. */
static long purge_last_status = -1;
static long purge_max_pending = 0;

static void bench_purge_from_main(void) {
#if BENCH_PURGE_MODE == BENCH_PURGE_MI_COLLECT_FORCE
  mi_collect(true);
#elif BENCH_PURGE_MODE == BENCH_PURGE_MI_PURGE_ALL
  mi_purge_all_report_t r;
  purge_last_status = mi_purge_all_ex(MI_PURGE_FORCE, 100, &r);
  if ((long)r.theaps_pending > purge_max_pending) purge_max_pending = (long)r.theaps_pending;
#elif BENCH_PURGE_MODE == BENCH_PURGE_JE_PURGE
  (void)je_mallctl("arena." BENCH_STRINGIFY(MALLCTL_ARENAS_ALL) ".purge",
                   NULL, NULL, NULL, 0);
#elif BENCH_PURGE_MODE == BENCH_PURGE_MALLOC_TRIM
  (void)malloc_trim(0);
#else
  /* BENCH_PURGE_NOTHING: deliberately empty. */
#endif
}

/* ---- allocation-free process instrumentation (as in the single-thread driver) */

static long status_kb(const char* key, size_t key_len) {
  const int fd = open("/proc/self/status", O_RDONLY);
  if (fd < 0) return -1;
  char buf[8192];
  const ssize_t n = read(fd, buf, sizeof(buf) - 1);
  close(fd);
  if (n <= 0) return -1;
  buf[n] = '\0';
  const char* p = buf;
  while (*p != '\0') {
    if (strncmp(p, key, key_len) == 0) {
      p += key_len;
      while (*p == ' ' || *p == '\t') p++;
      long kb = 0;
      while (*p >= '0' && *p <= '9') { kb = kb * 10 + (*p - '0'); p++; }
      return kb;
    }
    while (*p != '\0' && *p != '\n') p++;
    if (*p == '\n') p++;
  }
  return -1;
}

static size_t fmt_long(char* out, long v) {
  char tmp[24];
  size_t n = 0;
  size_t o = 0;
  if (v < 0) { out[o++] = '-'; v = -v; }
  if (v == 0) tmp[n++] = '0';
  while (v > 0) { tmp[n++] = (char)('0' + (v % 10)); v /= 10; }
  while (n > 0) out[o++] = tmp[--n];
  return o;
}

static void emit_row(const char* tag, long a, long b, int two) {
  char line[96];
  size_t o = 0;
  while (*tag != '\0') line[o++] = *tag++;
  line[o++] = ',';
  o += fmt_long(line + o, a);
  if (two) { line[o++] = ','; o += fmt_long(line + o, b); }
  line[o++] = '\n';
  ssize_t written = write(1, line, o);
  (void)written;
}

static void sleep_ms(long ms) {
  struct timespec ts;
  ts.tv_sec = ms / 1000;
  ts.tv_nsec = (ms % 1000) * 1000000L;
  nanosleep(&ts, NULL);
}

/* ---- the workers ---------------------------------------------------------- */

typedef struct { size_t size; size_t count; } size_class_t;

/* The single-thread workload, split evenly: every worker gets 1/N of each class,
   so the process-wide peak is the same 300k blocks the chart above measured. */
static const size_class_t classes[] = {
  { 512,  150000 / BENCH_THREADS },
  { 1024, 100000 / BENCH_THREADS },
  { 2048,  50000 / BENCH_THREADS },
};
#define N_CLASSES (sizeof(classes) / sizeof(classes[0]))

/* The hot set: a small ring the busy loop keeps re-allocating. Sizes cycle through
   the same classes as the churn so the loop draws from the pages that hold the
   survivors -- a thread that is genuinely using its heap, not one spinning on a
   cache. 64 slots x <= 2 KiB is ~100 KiB per thread: noise against a 280 MB peak. */
#define HOT_SLOTS 64
static const size_t hot_sizes[] = { 64, 128, 256, 512, 1024, 2048 };
#define N_HOT_SIZES (sizeof(hot_sizes) / sizeof(hot_sizes[0]))

static atomic_int churned;    /* workers that have finished their churn phase */
static atomic_int stop_flag;  /* main sets it at the end of the window */

static void* worker(void* arg) {
  (void)arg;
  size_t total = 0;
  for (size_t c = 0; c < N_CLASSES; c++) total += classes[c].count;
  const size_t table_bytes = total * sizeof(void*);
  void** blocks = (void**)mmap(NULL, table_bytes, PROT_READ | PROT_WRITE,
                               MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
  if (blocks == MAP_FAILED) return NULL;

  size_t idx = 0;
  size_t remaining[N_CLASSES];
  for (size_t c = 0; c < N_CLASSES; c++) remaining[c] = classes[c].count;
  size_t class_cursor = 0;
  while (idx < total) {
    size_t tries = 0;
    while (remaining[class_cursor] == 0 && tries < N_CLASSES) {
      class_cursor = (class_cursor + 1) % N_CLASSES;
      tries++;
    }
    blocks[idx] = BENCH_MALLOC(classes[class_cursor].size);
    if (blocks[idx] != NULL) memset(blocks[idx], 0xAB, classes[class_cursor].size);
    remaining[class_cursor]--;
    class_cursor = (class_cursor + 1) % N_CLASSES;
    idx++;
  }
  for (size_t i = 0; i < total; i++) {
    if (i % 20 != 0) {
      BENCH_FREE(blocks[i]);
      blocks[i] = NULL;
    }
  }
  atomic_fetch_add(&churned, 1);

  /* Busy for the whole window: no idle hook, no sleep, no yield -- just
     malloc/free/touch. In a gated build the owner gate is released between every
     one of these calls; in the default build this thread is RUNNING throughout. */
  void* ring[HOT_SLOTS];
  memset(ring, 0, sizeof(ring));
  size_t slot = 0;
  size_t size_cursor = 0;
  while (!atomic_load_explicit(&stop_flag, memory_order_relaxed)) {
    if (ring[slot] != NULL) BENCH_FREE(ring[slot]);
    const size_t size = hot_sizes[size_cursor];
    ring[slot] = BENCH_MALLOC(size);
    if (ring[slot] != NULL) memset(ring[slot], 0xCD, size);
    slot = (slot + 1) % HOT_SLOTS;
    size_cursor = (size_cursor + 1) % N_HOT_SIZES;
  }
  for (size_t i = 0; i < HOT_SLOTS; i++) if (ring[i] != NULL) BENCH_FREE(ring[i]);
  for (size_t i = 0; i < total; i += 20) BENCH_FREE(blocks[i]);
  munmap(blocks, table_bytes);
  return NULL;
}

int main(int argc, char** argv) {
  int seconds = 10;
  if (argc > 1) {
    seconds = 0;
    for (const char* p = argv[1]; *p >= '0' && *p <= '9'; p++) seconds = seconds * 10 + (*p - '0');
    if (seconds <= 0) seconds = 10;
  }
  const long tick_ms = 100;

  pthread_t threads[BENCH_THREADS];
  for (int i = 0; i < BENCH_THREADS; i++) {
    if (pthread_create(&threads[i], NULL, worker, NULL) != 0) return 1;
  }
  while (atomic_load(&churned) < BENCH_THREADS) sleep_ms(1);

  /* The window. The main thread has allocated nothing so far and allocates nothing
     here either: RSS read, sleep, the purge call under test, repeat. */
  long elapsed_ms = 0;
  while (elapsed_ms <= (long)seconds * 1000) {
    emit_row("CSV", elapsed_ms, status_kb("VmRSS:", 6), 1);
    sleep_ms(tick_ms);
    bench_purge_from_main();
    elapsed_ms += tick_ms;
  }
  emit_row("HWM", status_kb("VmHWM:", 6), 0, 0);
  emit_row("PURGE", purge_last_status, purge_max_pending, 1);

  atomic_store(&stop_flag, 1);
  for (int i = 0; i < BENCH_THREADS; i++) pthread_join(threads[i], NULL);

#if BENCH_FAMILY == 0
  {
    bool background_thread = false;
    size_t size = sizeof(background_thread);
    (void)je_mallctl("background_thread", &background_thread, &size, NULL, 0);
    emit_row("BACKGROUND_THREAD", (long)background_thread, 0, 0);
  }
#endif
  return 0;
}
"""


# ---------------------------------------------------------------------------
# Series definitions
# ---------------------------------------------------------------------------

#: A jemalloc built `--with-jemalloc-prefix=je_` reads `JE_MALLOC_CONF`, not
#: `MALLOC_CONF`, and ignores the unprefixed name without a word of complaint.
JEMALLOC_CONF_ENV = "JE_MALLOC_CONF"

IDLE_NOTHING = 0
IDLE_ON_THREAD_IDLE = 1
IDLE_MI_COLLECT = 2
IDLE_JE_PURGE = 3
IDLE_MI_COLLECT_FORCE = 4

IDLE_DESCRIPTIONS = {
    IDLE_NOTHING: "nothing (a plain idle process)",
    IDLE_ON_THREAD_IDLE: "mi_on_thread_idle() every 100 ms",
    IDLE_MI_COLLECT: "mi_collect(false) every 100 ms",
    IDLE_JE_PURGE: 'mallctl("arena.<all>.purge") every 100 ms',
    IDLE_MI_COLLECT_FORCE: "mi_collect(true) every 100 ms",
}

#: The same mechanisms, for a row that also has to name a non-default knob or window.
IDLE_SHORT = {
    IDLE_NOTHING: "nothing",
    IDLE_ON_THREAD_IDLE: "mi_on_thread_idle()",
    IDLE_MI_COLLECT: "mi_collect(false)",
    IDLE_JE_PURGE: 'mallctl("arena.<all>.purge")',
    IDLE_MI_COLLECT_FORCE: "mi_collect(true)",
}


@dataclass(frozen=True)
class SeriesSpec:
    """One measured child: an allocator plus the idle mechanism it is given."""

    key: str
    allocator_id: str
    label: str
    #: `IDLE_ON_THREAD_IDLE` degrades to `IDLE_NOTHING` when the allocator's own
    #: header does not declare `mi_on_thread_idle` -- resolved from the extracted
    #: source, never assumed (upstream mimalloc at the pinned commit has no such API).
    idle: int
    charted: bool
    dashed: bool = False
    #: Index into `SLOT_COLORS`; the two jemalloc series deliberately share one.
    slot: int = 0
    note: str = ""
    #: Extra child environment, as pairs so the spec stays hashable. Only ever used
    #: by uncharted diagnostics: every charted series runs the allocator's own
    #: compiled-in defaults, which is what "default config" has to mean.
    env: tuple[tuple[str, str], ...] = ()
    #: Idle-window override in seconds. `None` means the run-wide `--seconds`.
    seconds: int | None = None


SERIES: tuple[SeriesSpec, ...] = (
    SeriesSpec(
        key="mimalloc-pprof",
        allocator_id="mimalloc-pprof",
        label="mimalloc-pprof",
        idle=IDLE_ON_THREAD_IDLE,
        charted=True,
        slot=0,
    ),
    SeriesSpec(
        key="jemalloc",
        allocator_id="jemalloc",
        label="jemalloc",
        idle=IDLE_NOTHING,
        charted=True,
        slot=1,
    ),
    SeriesSpec(
        key="jemalloc-purge",
        allocator_id="jemalloc",
        label="jemalloc + explicit purge",
        idle=IDLE_JE_PURGE,
        charted=True,
        dashed=True,
        slot=1,
    ),
    SeriesSpec(
        key="bun-mimalloc",
        allocator_id="bun-mimalloc",
        label="Bun mimalloc",
        idle=IDLE_ON_THREAD_IDLE,
        charted=True,
        slot=2,
    ),
    SeriesSpec(
        key="upstream-mimalloc",
        allocator_id="upstream-mimalloc",
        label="Microsoft mimalloc",
        idle=IDLE_ON_THREAD_IDLE,
        charted=True,
        slot=3,
    ),
    # Diagnostics: measured and reported, never charted. They exist to answer the
    # two questions a reviewer asks first.
    SeriesSpec(
        key="mimalloc-pprof-no-idle-hook",
        allocator_id="mimalloc-pprof",
        label="mimalloc-pprof, no idle hook",
        idle=IDLE_NOTHING,
        charted=False,
        note="does the background scavenger alone return memory, with no cooperation?",
    ),
    SeriesSpec(
        key="upstream-mimalloc-collect",
        allocator_id="upstream-mimalloc",
        label="Microsoft mimalloc, mi_collect(false)",
        idle=IDLE_MI_COLLECT,
        charted=False,
        note="is upstream's flat line only a missing API, or a missing mechanism?",
    ),
    SeriesSpec(
        key="upstream-mimalloc-collect-force",
        allocator_id="upstream-mimalloc",
        label="Microsoft mimalloc, mi_collect(true)",
        idle=IDLE_MI_COLLECT_FORCE,
        charted=False,
        note="did upstream only lose to its own 1000 ms purge_delay, rather than to page "
        "granularity?",
    ),
    SeriesSpec(
        key="jemalloc-background-thread",
        allocator_id="jemalloc",
        label="jemalloc, background_thread:true",
        idle=IDLE_NOTHING,
        charted=False,
        env=((JEMALLOC_CONF_ENV, "background_thread:true"),),
        note="jemalloc's opt-in decay thread, at the same 10 s window as the chart; the "
        "only series whose runs do not agree with each other -- see the run-consistency "
        "footnote the table renders from the run records",
    ),
    SeriesSpec(
        key="jemalloc-background-thread-30s",
        allocator_id="jemalloc",
        label="jemalloc, background_thread:true, 30 s",
        idle=IDLE_NOTHING,
        charted=False,
        env=((JEMALLOC_CONF_ENV, "background_thread:true"),),
        seconds=30,
        note="the same, past jemalloc's 10 s default dirty_decay_ms -- its best case",
    ),
)

CHARTED: tuple[SeriesSpec, ...] = tuple(s for s in SERIES if s.charted)


# ---------------------------------------------------------------------------
# Sizing-run series (#365 §6): what the non-allocating main thread calls every tick
# while N workers stay busy. Mirrors BENCH_PURGE_* in BUSY_C_SOURCE.
# ---------------------------------------------------------------------------

PURGE_NOTHING = 0
PURGE_MI_COLLECT_FORCE = 1
PURGE_MI_PURGE_ALL = 2
PURGE_JE_PURGE = 3
PURGE_MALLOC_TRIM = 4

PURGE_DESCRIPTIONS = {
    PURGE_NOTHING: "nothing (scavenger only)",
    PURGE_MI_COLLECT_FORCE: "mi_collect(true) every 100 ms",
    PURGE_MI_PURGE_ALL: "mi_purge_all(true) every 100 ms",
    PURGE_JE_PURGE: 'mallctl("arena.<all>.purge") every 100 ms',
    PURGE_MALLOC_TRIM: "malloc_trim(0) every 100 ms",
}

#: This tree built a second time with the owner gate on. Not a lockfile record: it is
#: the same source and the same recipe as `mimalloc-pprof` plus one CMake option, and
#: it exists only for the sizing run.
GATED_ALLOCATOR = "mimalloc-pprof-gated"
GATED_CMAKE_ARGS = ("-DMI_OWNER_GATE=ON",)
GATED_BASE = "mimalloc-pprof"

#: The driver linked against nothing: the C library's own malloc. Not a build at all.
GLIBC_ALLOCATOR = "glibc"

#: BENCH_FAMILY for BUSY_C_SOURCE, by allocator id; everything else is a mimalloc.
BUSY_FAMILY = {"jemalloc": 0, GLIBC_ALLOCATOR: 2}


@dataclass(frozen=True)
class BusySeriesSpec:
    """One sizing-run row: an allocator build plus what the main thread calls."""

    key: str
    allocator_id: str
    label: str
    purge: int
    note: str = ""


BUSY_SERIES: tuple[BusySeriesSpec, ...] = (
    BusySeriesSpec(
        key="mimalloc-pprof-busy-collect",
        allocator_id="mimalloc-pprof",
        label="mimalloc-pprof",
        purge=PURGE_MI_COLLECT_FORCE,
        note="mi_collect is caller-only by design; this is the ceiling of the pre-#366 API",
    ),
    BusySeriesSpec(
        key="mimalloc-pprof-busy-purge-all",
        allocator_id="mimalloc-pprof",
        label="mimalloc-pprof",
        purge=PURGE_MI_PURGE_ALL,
        note="default build: arenas, abandoned pages and parked threads; busy owners pending",
    ),
    BusySeriesSpec(
        key="mimalloc-pprof-gated-busy-purge-all",
        allocator_id=GATED_ALLOCATOR,
        label="mimalloc-pprof, MI_OWNER_GATE=ON",
        purge=PURGE_MI_PURGE_ALL,
        note="gated build: every registered thread is claimed between its allocator calls",
    ),
    BusySeriesSpec(
        key="bun-mimalloc-busy-collect",
        allocator_id="bun-mimalloc",
        label="Bun mimalloc",
        purge=PURGE_MI_COLLECT_FORCE,
    ),
    BusySeriesSpec(
        key="upstream-mimalloc-busy-collect",
        allocator_id="upstream-mimalloc",
        label="Microsoft mimalloc",
        purge=PURGE_MI_COLLECT_FORCE,
    ),
    BusySeriesSpec(
        key="jemalloc-busy-purge",
        allocator_id="jemalloc",
        label="jemalloc",
        purge=PURGE_JE_PURGE,
        note="the measurement behind its documented any-thread purge",
    ),
    BusySeriesSpec(
        key="glibc-busy-trim",
        allocator_id=GLIBC_ALLOCATOR,
        label="glibc malloc",
        purge=PURGE_MALLOC_TRIM,
        note="the second any-thread reference: malloc_trim walks every arena",
    ),
    BusySeriesSpec(
        key="mimalloc-pprof-busy-nothing",
        allocator_id="mimalloc-pprof",
        label="mimalloc-pprof",
        purge=PURGE_NOTHING,
        note="control: the background scavenger alone, default build",
    ),
    BusySeriesSpec(
        key="mimalloc-pprof-gated-busy-nothing",
        allocator_id=GATED_ALLOCATOR,
        label="mimalloc-pprof, MI_OWNER_GATE=ON",
        purge=PURGE_NOTHING,
        note="control: in a gated build the scavenger's timed sweep reaches busy threads too",
    ),
)


# ---------------------------------------------------------------------------
# Report shapes
# ---------------------------------------------------------------------------


class Sample(NamedTuple):
    t_ms: int
    rss_kb: int


class RunRecordMeasurements(TypedDict):
    """The three numbers every repetition of every series produces."""

    peak_rss_mb: float
    idle_start_rss_mb: float
    after_idle_rss_mb: float


class RunRecord(RunRecordMeasurements, total=False):
    """One repetition of one series, plus whatever that allocator can report back."""

    #: jemalloc children only: the live `background_thread` setting they read back.
    background_thread: bool


class SeriesSummary(TypedDict):
    """One series' entry in allocator-idle-report.json."""

    label: str
    allocator_id: str
    pin: str
    idle_mechanism: str
    idle_seconds: int
    env: dict[str, str]
    charted: bool
    note: str
    runs: list[RunRecord]
    peak_rss_mb: float
    idle_start_rss_mb: float
    after_idle_rss_mb: float
    percent_returned: float


class ReportJson(TypedDict):
    """The full shape of allocator-idle-report.json."""

    commit: str
    cpu: str
    kernel: str
    runs_per_series: int
    idle_seconds: int
    selection_rule: str
    skipped_allocators: list[str]
    series: dict[str, SeriesSummary]


class BusySeriesSummary(SeriesSummary):
    """One sizing-run row. A `SeriesSummary` (so the table renderer draws it unchanged)
    plus what the purge call itself reported back."""

    #: Extra CMake arguments over the lockfile recipe; empty for a lockfile build.
    build_flags: list[str]
    #: The last `mi_purge_all_ex` status the picked run saw (0 OK, 1 PARTIAL, 2 BUSY);
    #: -1 for every other purge mechanism, which has no status to report.
    purge_status: int
    #: The largest `theaps_pending` any tick of the picked run returned.
    purge_max_pending: int


class BusyReportJson(TypedDict):
    """The full shape of allocator-purge-any-thread-report.json."""

    commit: str
    cpu: str
    kernel: str
    runs_per_series: int
    idle_seconds: int
    busy_threads: int
    selection_rule: str
    series: dict[str, BusySeriesSummary]


@dataclass
class RunResult:
    samples: list[Sample]
    peak_rss_kb: int
    #: jemalloc's live `background_thread` setting; `None` for the mimalloc children,
    #: which have no such knob to read back.
    background_thread: bool | None = None
    #: The busy driver's PURGE row: last status and max pending (see BusySeriesSummary).
    purge_status: int = -1
    purge_max_pending: int = 0

    @property
    def after_idle_rss_kb(self) -> int:
        """RSS at the end of the idle window -- the run-ranking scalar."""
        return self.samples[-1].rss_kb if self.samples else 0

    @property
    def idle_start_rss_kb(self) -> int:
        return self.samples[0].rss_kb if self.samples else 0


# ---------------------------------------------------------------------------
# Building the pinned allocators, then one driver per series
# ---------------------------------------------------------------------------


def select_records(lockfile: Path) -> list[Mapping[str, object]]:
    """Read the whole lockfile (so its own id validation runs) and keep what we build."""
    records = builder.read_lockfile(lockfile)
    by_id = {builder.require_string(r.get("id"), "allocator.id"): r for r in records}
    missing = [name for name in NEEDED_ALLOCATORS if name not in by_id]
    if missing:
        raise SystemExit(f"lockfile {lockfile} is missing allocator records: {missing}")
    return [by_id[name] for name in NEEDED_ALLOCATORS]


@dataclass
class AllocatorBuild:
    allocator_id: str
    pin: str
    library: Path
    include_dirs: list[Path]
    has_on_thread_idle: bool


def build_allocators(
    records: Sequence[Mapping[str, object]], build_root: Path, jobs: int
) -> dict[str, AllocatorBuild]:
    """Run each record's own locked build commands, then locate its library/headers.

    This deliberately reuses `build_benchmark_allocators`' source acquisition,
    checksum verification and command expansion rather than re-implementing them, but
    stops short of `build_records`, which also cargo-builds a Rust `benchmark-child`
    per allocator that nothing here would run.
    """
    logs = build_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    sources, _patches, _tree_sha256s = builder.prepare_sources(records, build_root, logs)

    builds: dict[str, AllocatorBuild] = {}
    for record in records:
        allocator_id = builder.require_string(record.get("id"), "allocator.id")
        source_dir = sources[allocator_id]
        build_dir = build_root / "build" / allocator_id
        build_dir.mkdir(parents=True, exist_ok=True)
        build = builder.require_mapping(record.get("build"), f"{allocator_id}.build")
        commands = builder.require_commands(build.get("commands"), f"{allocator_id}.build.commands")
        with (logs / f"{allocator_id}.log").open("w", encoding="utf-8") as log:
            for command in commands:
                resolved = builder.expand_command(command, source_dir, build_dir, jobs)
                log.write("$ " + " ".join(resolved) + "\n")
                log.flush()
                subprocess.run(
                    resolved,
                    cwd=source_dir,
                    env=builder.command_environment(),
                    check=True,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
        include_dirs = builder.adapter_include_directories(allocator_id, source_dir, build_dir)
        library = builder.find_primary_library(record, source_dir, build_dir)
        builds[allocator_id] = AllocatorBuild(
            allocator_id=allocator_id,
            pin=builder.require_string(record.get("pin"), f"{allocator_id}.pin"),
            library=library,
            include_dirs=include_dirs,
            has_on_thread_idle=header_declares_on_thread_idle(include_dirs),
        )
        print(f"built {allocator_id}: {library}", flush=True)
    return builds


def build_gated_fork(
    records: Sequence[Mapping[str, object]],
    builds: Mapping[str, AllocatorBuild],
    build_root: Path,
    jobs: int,
) -> AllocatorBuild:
    """Build this tree a second time with `-DMI_OWNER_GATE=ON`.

    The lockfile's own `mimalloc-pprof` recipe, verbatim, with the option appended to
    its configure command and a build directory of its own -- so the gated row differs
    from the default row by exactly that one option and nothing else.
    """
    record = next(r for r in records if builder.require_string(r.get("id"), "id") == GATED_BASE)
    base = builds[GATED_BASE]
    source_dir = builder.repository_root()
    build_dir = build_root / "build" / GATED_ALLOCATOR
    build_dir.mkdir(parents=True, exist_ok=True)
    build = builder.require_mapping(record.get("build"), f"{GATED_BASE}.build")
    commands = builder.require_commands(build.get("commands"), f"{GATED_BASE}.build.commands")
    logs = build_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / f"{GATED_ALLOCATOR}.log").open("w", encoding="utf-8") as log:
        for command in commands:
            resolved = builder.expand_command(command, source_dir, build_dir, jobs)
            if resolved[:1] == ["cmake"] and "-S" in resolved:
                resolved = [*resolved, *GATED_CMAKE_ARGS]
            log.write("$ " + " ".join(resolved) + "\n")
            log.flush()
            subprocess.run(
                resolved,
                cwd=source_dir,
                env=builder.command_environment(),
                check=True,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
    cache = (build_dir / "CMakeCache.txt").read_text(encoding="utf-8")
    if "MI_OWNER_GATE:BOOL=ON" not in cache:
        raise SystemExit(f"{GATED_ALLOCATOR}: MI_OWNER_GATE did not reach the CMake cache")
    library = builder.find_primary_library(record, source_dir, build_dir)
    print(f"built {GATED_ALLOCATOR}: {library}", flush=True)
    return AllocatorBuild(
        allocator_id=GATED_ALLOCATOR,
        pin=base.pin,
        library=library,
        include_dirs=base.include_dirs,
        has_on_thread_idle=base.has_on_thread_idle,
    )


def glibc_pseudo_build() -> AllocatorBuild:
    """The C library's malloc: nothing to build, nothing to link, no header of ours."""
    return AllocatorBuild(
        allocator_id=GLIBC_ALLOCATOR,
        pin=glibc_version(),
        library=Path("/dev/null"),  # never linked; see compile_busy_driver
        include_dirs=[],
        has_on_thread_idle=False,
    )


def glibc_version() -> str:
    try:
        return "glibc " + platform.libc_ver()[1]
    except (OSError, ValueError):
        return "glibc"


def header_declares(include_dirs: Sequence[Path], name: str) -> bool:
    for directory in include_dirs:
        header = directory / "mimalloc.h"
        if header.is_file() and name in header.read_text(encoding="utf-8"):
            return True
    return False


def header_declares_on_thread_idle(include_dirs: Sequence[Path]) -> bool:
    """Is `mi_on_thread_idle` actually in this allocator's public header?

    Read, never assumed: upstream mimalloc at the pinned commit has no such API, and a
    driver that hardcoded the call would simply fail to link -- or worse, a driver that
    hardcoded "upstream has none" would silently keep saying so after upstream added it.
    """
    for directory in include_dirs:
        header = directory / "mimalloc.h"
        if header.is_file() and "mi_on_thread_idle" in header.read_text(encoding="utf-8"):
            return True
    return False


def resolve_idle(spec: SeriesSpec, build: AllocatorBuild) -> int:
    if spec.idle == IDLE_ON_THREAD_IDLE and not build.has_on_thread_idle:
        return IDLE_NOTHING
    return spec.idle


def compile_driver(spec: SeriesSpec, build: AllocatorBuild, idle: int, work_dir: Path) -> Path:
    src = work_dir / f"churn_{spec.key}.c"
    src.write_text(CHURN_C_SOURCE, encoding="utf-8")
    exe = work_dir / f"churn_{spec.key}"
    family = 1 if build.allocator_id != "jemalloc" else 0
    cmd = [
        "cc",
        "-O2",
        "-g",
        f"-DBENCH_FAMILY_MIMALLOC={family}",
        f"-DBENCH_IDLE_MODE={idle}",
    ]
    for directory in build.include_dirs:
        cmd += ["-I", str(directory)]
    cmd += [
        str(src),
        str(build.library),
        "-lpthread",
        "-ldl",
        "-lm",
        "-lrt",
        "-latomic",
        "-o",
        str(exe),
    ]
    subprocess.run(cmd, check=True)
    return exe


def compile_busy_driver(
    spec: BusySeriesSpec, build: AllocatorBuild, threads: int, work_dir: Path
) -> Path:
    """Build BUSY_C_SOURCE for one sizing-run row. Refuses, rather than degrades, a
    purge call the allocator's header does not declare: a row that silently fell back
    to "nothing" would be reported as that allocator returning nothing."""
    if spec.purge == PURGE_MI_PURGE_ALL and not header_declares(build.include_dirs, "mi_purge_all"):
        raise SystemExit(f"{spec.key}: {build.allocator_id} declares no mi_purge_all")
    if spec.purge == PURGE_MI_COLLECT_FORCE and not header_declares(
        build.include_dirs, "mi_collect"
    ):
        raise SystemExit(f"{spec.key}: {build.allocator_id} declares no mi_collect")
    src = work_dir / f"busy_{spec.key}.c"
    src.write_text(BUSY_C_SOURCE, encoding="utf-8")
    exe = work_dir / f"busy_{spec.key}"
    family = BUSY_FAMILY.get(build.allocator_id, 1)
    cmd = [
        "cc",
        "-O2",
        "-g",
        "-pthread",
        f"-DBENCH_FAMILY={family}",
        f"-DBENCH_PURGE_MODE={spec.purge}",
        f"-DBENCH_THREADS={threads}",
    ]
    for directory in build.include_dirs:
        cmd += ["-I", str(directory)]
    cmd.append(str(src))
    if build.allocator_id != GLIBC_ALLOCATOR:
        cmd.append(str(build.library))
    cmd += ["-lpthread", "-ldl", "-lm", "-lrt", "-latomic", "-o", str(exe)]
    subprocess.run(cmd, check=True)
    return exe


def child_environment(extra: Sequence[tuple[str, str]] = ()) -> dict[str, str]:
    """A clean environment: no inherited MIMALLOC_*/MALLOC_CONF can retune a child.

    "Default config" has to mean the allocator's compiled-in defaults, not this
    shell's -- so the base environment is built up rather than filtered down, and the
    only tuning any child ever sees is what a diagnostic series asked for by name.
    """
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": "C",
    }
    environment.update(dict(extra))
    return environment


def run_once(exe: Path, seconds: int, extra_env: Sequence[tuple[str, str]] = ()) -> RunResult:
    cmd = ["taskset", "-c", "0-3", str(exe), str(seconds)]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, check=True, env=child_environment(extra_env)
    )
    samples: list[Sample] = []
    peak_kb = 0
    background_thread: bool | None = None
    purge_status = -1
    purge_max_pending = 0
    for line in proc.stdout.splitlines():
        if line.startswith("CSV,"):
            _, t_ms, rss_kb = line.split(",")
            samples.append(Sample(int(t_ms), int(rss_kb)))
        elif line.startswith("HWM,"):
            peak_kb = int(line.split(",")[1])
        elif line.startswith("BACKGROUND_THREAD,"):
            background_thread = line.split(",")[1] == "1"
        elif line.startswith("PURGE,"):
            _, status, pending = line.split(",")
            purge_status, purge_max_pending = int(status), int(pending)
    if not samples:
        raise SystemExit(f"{exe} produced no samples")
    if peak_kb <= 0:
        raise SystemExit(f"{exe} produced no VmHWM reading")
    # VmHWM and VmRSS are both maintained from per-CPU caches that are only flushed
    # periodically, so they disagree by a few hundred kB and VmHWM can legitimately read
    # BELOW a VmRSS sample taken moments earlier -- which it did in 13 of 27 runs while
    # this was being written. A "peak" under the first post-free sample is nonsense to a
    # reader, so the peak is the larger of the two.
    peak_kb = max(peak_kb, samples[0].rss_kb)
    return RunResult(
        samples=samples,
        peak_rss_kb=peak_kb,
        background_thread=background_thread,
        purge_status=purge_status,
        purge_max_pending=purge_max_pending,
    )


def assert_env_took_effect(spec: SeriesSpec, attempts: Sequence[RunResult]) -> None:
    """Refuse to publish a diagnostic whose knob never turned on.

    This is not hypothetical: the first version of the `background_thread` row set
    `MALLOC_CONF`, which a `je_`-prefixed jemalloc ignores entirely, and it produced a
    perfectly plausible 0% that would have been reported as "even jemalloc's decay
    thread returns nothing".
    """
    wanted = any(
        name == JEMALLOC_CONF_ENV and "background_thread:true" in value for name, value in spec.env
    )
    for run in attempts:
        if run.background_thread is None:
            continue
        if run.background_thread != wanted:
            raise SystemExit(
                f"{spec.key}: asked for background_thread={wanted} via {list(spec.env)}, but "
                f"jemalloc reports background_thread={run.background_thread}"
            )


def measure_series(
    spec: SeriesSpec, build: AllocatorBuild, work_dir: Path, seconds: int, runs: int
) -> tuple[RunResult, list[RunResult], int]:
    idle = resolve_idle(spec, build)
    exe = compile_driver(spec, build, idle, work_dir)
    window = spec.seconds if spec.seconds is not None else seconds
    attempts = [run_once(exe, window, spec.env) for _ in range(runs)]
    assert_env_took_effect(spec, attempts)
    # Lowest after-idle RSS wins, for every series alike: the most generous run each
    # allocator managed, so no arm is charted at its worst.
    picked = min(attempts, key=lambda r: r.after_idle_rss_kb)
    return picked, attempts, idle


def measure_busy_series(
    spec: BusySeriesSpec,
    build: AllocatorBuild,
    work_dir: Path,
    seconds: int,
    runs: int,
    threads: int,
) -> tuple[RunResult, list[RunResult]]:
    exe = compile_busy_driver(spec, build, threads, work_dir)
    attempts = [run_once(exe, seconds) for _ in range(runs)]
    for run in attempts:
        # A jemalloc child must be running its compiled-in default here too.
        if run.background_thread:
            raise SystemExit(f"{spec.key}: jemalloc reports background_thread=True")
    picked = min(attempts, key=lambda r: r.after_idle_rss_kb)
    return picked, attempts


# ---------------------------------------------------------------------------
# Chart rendering. Self-contained SVG in the same house style as
# ci/bench_hole_purging.py and ci/star_history.py: no external assets, viewBox,
# ink-colored text, recessive grid.
# ---------------------------------------------------------------------------

WIDTH = 1000
HEIGHT = 537
PAD_LEFT = 64
PAD_RIGHT = 210
PAD_TOP = 169
PAD_BOTTOM = 48

# The dataviz skill's validated categorical slots 1-4, taken verbatim from
# references/palette.md. `node` is not available in this environment, so
# validate_palette.js could not be re-run here -- which is why the slots are used
# in the documented fixed order rather than hand-picked. Slot 4 (yellow) is below
# 3:1 on the light surface, so the relief rule applies: every series is directly
# labeled at its end AND listed in the legend, never identified by color alone.
SLOT_COLORS_LIGHT = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100")
SLOT_COLORS_DARK = ("#3987e5", "#d95926", "#199e70", "#c98500")


@dataclass
class Theme:
    name: str
    background: str
    grid: str
    text: str
    muted: str
    slots: tuple[str, ...]


LIGHT = Theme("light", "#ffffff", "#d8dee4", "#1f2328", "#59636e", SLOT_COLORS_LIGHT)
DARK = Theme("dark", "#0d1117", "#30363d", "#e6edf3", "#8b949e", SLOT_COLORS_DARK)


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def nice_ticks(maximum: float) -> list[float]:
    if maximum <= 0:
        return [0.0, 1.0]
    for step in (5, 10, 20, 25, 50, 100, 200, 250, 500):
        if maximum / step <= 6:
            top = ((maximum // step) + 1) * step
            return [i * step for i in range(int(top / step) + 1)]
    step = 500
    top = ((maximum // step) + 1) * step
    return [i * step for i in range(int(top / step) + 1)]


def percent_returned(peak_mb: float, after_mb: float) -> float:
    return (peak_mb - after_mb) / peak_mb * 100 if peak_mb else 0.0


#: A run "returned" if it gave back at least half of peak. The measured population is
#: strongly bimodal -- every run so far has landed either near 0% or near 74% -- so any
#: threshold in the middle separates them; half is the one that needs no explanation.
RETURN_THRESHOLD_PCT = 50.0


def runs_returned(summary: SeriesSummary) -> tuple[int, int]:
    """How many of a series' repetitions actually returned memory, and how many ran."""
    runs = summary["runs"]
    returned = sum(
        1
        for run in runs
        if percent_returned(run["peak_rss_mb"], run["after_idle_rss_mb"]) >= RETURN_THRESHOLD_PCT
    )
    return returned, len(runs)


def inconsistent_series(
    summaries: Mapping[str, SeriesSummary],
) -> list[tuple[SeriesSummary, int, int]]:
    """Series whose repetitions disagree about whether memory came back at all.

    The charted figure is the best of 3, so a series that returned memory in only some
    of its runs would otherwise be reported by its lucky run alone. Computing this from
    the run records means the caveat cannot go stale the way a hand-written "2 of 3"
    would on the next measurement.
    """
    out: list[tuple[SeriesSummary, int, int]] = []
    keys = [s.key for s in SERIES] + [s.key for s in BUSY_SERIES]
    for key in keys:
        summary = summaries.get(key)
        if summary is None:
            continue
        returned, total = runs_returned(summary)
        if 0 < returned < total:
            out.append((summary, returned, total))
    return out


def chart_title(summaries: Mapping[str, SeriesSummary]) -> str:
    """State the takeaway, with the measured numbers in it -- not just the axes.

    "At the next idle point", not "after idling": every number on this chart is what
    the allocator did when the process reached an idle point and called that
    allocator's own idle entry point (or, for the plain jemalloc and plain upstream
    lines, called nothing, because there is nothing to call). Saying "after 10 s idle"
    would imply the mimalloc figures come for free from the passage of time, and the
    `mimalloc-pprof, no idle hook` diagnostic row in the table shows they do not.
    """
    fork = summaries["mimalloc-pprof"]
    bun = summaries["bun-mimalloc"]
    jem = summaries["jemalloc"]
    return (
        f"At the first idle tick mimalloc-pprof returns {fork['percent_returned']:.0f}% of peak "
        f"RSS, Bun's mimalloc {bun['percent_returned']:.0f}%, jemalloc "
        f"{jem['percent_returned']:.0f}% by default"
    )


def caption_lines(summaries: Mapping[str, SeriesSummary]) -> list[str]:
    """The six sentences without which this chart would be misleading.

    An SVG travels: it gets pasted into an issue, a slide, a chat. Every caveat that
    would change a reader's conclusion has to be on the image itself, not only in the
    README around it -- that the jemalloc figure is a default-configuration figure and
    its opt-in decay thread does return the memory, that the mimalloc figure is
    cooperative rather than something time alone produces, and how many repetitions of a
    disagreeing series actually returned anything.
    """
    upstream = summaries["upstream-mimalloc"]
    purge = summaries["jemalloc-purge"]
    background = summaries["jemalloc-background-thread"]
    no_hook = summaries["mimalloc-pprof-no-idle-hook"]
    collect = summaries["upstream-mimalloc-collect"]

    background_long = summaries["jemalloc-background-thread-30s"]

    # Phrased so that it reads correctly whichever side of the boundary the short
    # window lands on: jemalloc's default dirty_decay_ms is 10 s and this window is
    # 10 s, so that row has been measured at both 0% and 73% in different sessions.
    # Both windows are always stated, and a session whose repetitions disagreed says so.
    returned, total = runs_returned(background)
    consistency = "" if returned in (0, total) else f" (in {returned} of {total} runs)"
    return [
        "300k blocks in three small size classes, a scattered 1-in-20 kept alive, the rest "
        "freed, then a 10 s idle window.",
        "The mimalloc forks call mi_on_thread_idle() every 100 ms, punching holes in "
        f"still-used pages; upstream has no such API ({upstream['percent_returned']:.0f}%).",
        "jemalloc's decay is advanced by allocation, not by idling: nothing on its own, "
        f"{purge['percent_returned']:.0f}% via an explicit "
        'mallctl("arena.<all>.purge") (dashed).',
        "Its opt-in background_thread:true returns "
        f"{background['percent_returned']:.0f}% inside this same 10 s window{consistency}, "
        f"and {background_long['percent_returned']:.0f}% over "
        f"{background_long['idle_seconds']} s.",
        "jemalloc's default dirty_decay_ms is 10 s -- exactly this window -- so that row "
        "sits on the boundary, not past it.",
        "Cooperative, not passive: mimalloc-pprof with no idle hook returns "
        f"{no_hook['percent_returned']:.0f}%, upstream given mi_collect() "
        f"{collect['percent_returned']:.0f}%. See the table.",
    ]


#: How close two end-of-window figures have to be before the chart calls them
#: overlapping. Wide enough to catch the real cluster, narrow enough that a series
#: which genuinely separated is not swept into the sentence.
COINCIDENT_TOLERANCE_MB = 2.0


def final_mb_of(series: Mapping[str, list[Sample]], spec: SeriesSpec) -> float:
    return series[spec.key][-1].rss_kb / 1024.0


def lowest_mb(series: Mapping[str, list[Sample]], charted: Sequence[SeriesSpec]) -> float:
    return min(final_mb_of(series, spec) for spec in charted)


def render_line_chart(
    series: Mapping[str, list[Sample]],
    summaries: Mapping[str, SeriesSummary],
    theme: Theme,
    source_line: str,
) -> str:
    charted = [s for s in CHARTED if series.get(s.key)]
    max_t = max(series[s.key][-1].t_ms for s in charted)
    max_rss = max(max(p.rss_kb for p in series[s.key]) for s in charted) / 1024.0
    ticks = nice_ticks(max_rss)
    top = ticks[-1]

    plot_w = WIDTH - PAD_LEFT - PAD_RIGHT
    plot_h = HEIGHT - PAD_TOP - PAD_BOTTOM

    def x_of(t_ms: int) -> float:
        return PAD_LEFT + (t_ms / max_t) * plot_w

    def y_of(rss_mb: float) -> float:
        return PAD_TOP + plot_h - (rss_mb / top) * plot_h

    def path_of(samples: list[Sample]) -> str:
        pts = [f"M {x_of(samples[0].t_ms):.2f} {y_of(samples[0].rss_kb / 1024.0):.2f}"]
        for s in samples[1:]:
            pts.append(f"L {x_of(s.t_ms):.2f} {y_of(s.rss_kb / 1024.0):.2f}")
        return " ".join(pts)

    title = chart_title(summaries)
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'width="{WIDTH}" height="{HEIGHT}" role="img" '
        f'aria-label="{escape(title)}">',
        f'<rect width="{WIDTH}" height="{HEIGHT}" fill="{theme.background}"/>',
        '<g font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif">',
        f'<text x="{PAD_LEFT}" y="26" font-size="17" font-weight="600" fill="{theme.text}">'
        f"{escape(title)}</text>",
        f'<text x="{PAD_LEFT}" y="44" font-size="12" fill="{theme.muted}">'
        f"{escape(source_line)}</text>",
    ]
    for index, line in enumerate(caption_lines(summaries)):
        parts.append(
            f'<text x="{PAD_LEFT}" y="{61 + index * 17}" font-size="12" fill="{theme.muted}">'
            f"{escape(line)}</text>"
        )

    for tick in ticks:
        y = y_of(tick)
        parts.append(
            f'<line x1="{PAD_LEFT}" y1="{y:.2f}" x2="{WIDTH - PAD_RIGHT}" y2="{y:.2f}" '
            f'stroke="{theme.grid}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{PAD_LEFT - 10}" y="{y + 4:.2f}" font-size="12" text-anchor="end" '
            f'fill="{theme.muted}">{int(tick)}</text>'
        )
    parts.append(
        f'<text x="{PAD_LEFT - 44}" y="{PAD_TOP - 8:.2f}" font-size="11" '
        f'fill="{theme.muted}">MB</text>'
    )

    for t_ms in (0, max_t):
        label = f"{t_ms / 1000:.0f}s"
        anchor = "start" if t_ms == 0 else "end"
        parts.append(
            f'<text x="{x_of(t_ms):.2f}" y="{HEIGHT - PAD_BOTTOM + 22:.2f}" font-size="12" '
            f'text-anchor="{anchor}" fill="{theme.muted}">{label}</text>'
        )

    for spec in charted:
        dash = ' stroke-dasharray="6,4"' if spec.dashed else ""
        parts.append(
            f'<path d="{path_of(series[spec.key])}" fill="none" '
            f'stroke="{theme.slots[spec.slot]}" stroke-width="2" stroke-linejoin="round" '
            f'stroke-linecap="round"{dash}/>'
        )

    # The whole drop happens between the first two samples. Say so, or the near-vertical
    # segment reads as a gap in the data rather than as one measured idle tick.
    elbow_x = x_of(series[charted[0].key][1].t_ms)
    parts.append(
        f'<text x="{elbow_x + 14:.2f}" y="{PAD_TOP + plot_h * 0.42:.2f}" font-size="11" '
        f'fill="{theme.muted}">the drop is one measured 100 ms idle tick, not a gap in the '
        "data</text>"
    )

    # Lines that genuinely coincide have to be called out, or a reader counts four.
    # The tolerance and the number in the sentence are the same quantity, computed:
    # a hand-written "within 1 MB" beside a 2 MB predicate is how that sentence goes
    # quietly wrong on the next measurement.
    lowest = lowest_mb(series, charted)
    coincident = [s for s in charted if final_mb_of(series, s) - lowest < COINCIDENT_TOLERANCE_MB]
    if len(coincident) > 1:
        names = ", ".join(spec.label for spec in coincident)
        spread = max(final_mb_of(series, s) for s in coincident) - lowest
        parts.append(
            f'<text x="{elbow_x + 14:.2f}" '
            f'y="{y_of(lowest) - 12:.2f}" font-size="11" '
            f'fill="{theme.muted}">{escape(names)} overlap here, within '
            f"{spread:.1f} MB of each other</text>"
        )

    # Direct end-of-line labels, each with its own line swatch: with five series the
    # labels ARE the legend, so the swatch has to carry the dash pattern too. Sorted by
    # final RSS and nudged apart, so label order matches line order on screen.
    label_rows = sorted(
        ((final_mb_of(series, s), s) for s in charted),
        key=lambda row: row[0],
    )
    placed: list[float] = []
    for final_mb, spec in label_rows:
        y = y_of(final_mb)
        while any(abs(y - other) < 16 for other in placed):
            y -= 16
        placed.append(y)
        swatch_x = x_of(max_t) + 8
        dash = ' stroke-dasharray="5,3"' if spec.dashed else ""
        parts.append(
            f'<line x1="{swatch_x:.2f}" y1="{y:.2f}" x2="{swatch_x + 18:.2f}" y2="{y:.2f}" '
            f'stroke="{theme.slots[spec.slot]}" stroke-width="2"{dash}/>'
        )
        parts.append(
            f'<text x="{swatch_x + 25:.2f}" y="{y + 4:.2f}" font-size="12" '
            f'fill="{theme.text}">{escape(spec.label)}</text>'
        )

    parts.append("</g></svg>")
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# Table rendering: one row per allocator, same ink tokens as the line chart.
# ---------------------------------------------------------------------------

TABLE_WIDTH = 1000
ROW_H = 26
HEADER_H = 34
TITLE_H = 74
COL_LABEL_X = 24
COL_IDLE_X = 290
COL_PEAK_X = 740
COL_AFTER_X = 880
COL_PCT_X = 976
TABLE_FONT_PX = 12
#: Rough advance width of one character of the table's 12px sans stack. Used only to
#: keep the "what runs during idle" cell from growing into the "peak RSS" column --
#: an SVG has no layout engine, so nothing else would notice until someone looked at
#: a rendered PNG. Deliberately generous.
CHAR_WIDTH_PX = 6.3
#: How wide that cell may get before it collides with the right-aligned peak value.
MAX_IDLE_TEXT_PX = COL_PEAK_X - COL_IDLE_X - 70


def approx_text_width(text: str, font_px: int = TABLE_FONT_PX) -> float:
    return len(text) * CHAR_WIDTH_PX * font_px / TABLE_FONT_PX


@dataclass(frozen=True)
class TableText:
    """The words on a table SVG that differ between the idle table and the sizing-run
    table. The defaults are the idle table's, byte for byte."""

    aria: str = "Peak RSS, RSS after 10 s idle, and percent returned, per allocator"
    title: str = "Memory returned after idle, by allocator"
    subtitle: str = (
        "Peak is VmHWM; after-idle is VmRSS at the end of the idle window (10 s unless the "
        "row says otherwise). Both are kernel counters, good to a few hundred kB."
    )
    mechanism_header: str = "what runs during idle"
    after_header: str = "after idle"
    footnote: str = "Rows below the dashed rule are diagnostics, not part of the chart."


IDLE_TABLE_TEXT = TableText()


def busy_table_text(threads: int) -> TableText:
    return TableText(
        aria=(
            f"Peak RSS, RSS after 10 s with {threads} busy threads and a purge from another "
            "thread, and percent returned, per allocator"
        ),
        title=f"Memory returned with {threads} busy threads, purge from another thread",
        subtitle=(
            f"{threads} workers churn, then stay in a malloc/free loop and never idle; the "
            "main thread allocates nothing and calls the purge every 100 ms for 10 s."
        ),
        mechanism_header="what the purging thread calls",
        after_header="after 10 s",
        footnote=(
            "Same workload split across the workers. Rows below the dashed rule are "
            "controls: nobody calls anything, only the background scavenger runs."
        ),
    )


def render_table_svg(
    summaries: Mapping[str, SeriesSummary],
    theme: Theme,
    source_line: str,
    order: Sequence[tuple[str, bool]] = (),
    text_: TableText = IDLE_TABLE_TEXT,
) -> str:
    """One row per series. `order` is (key, charted) pairs; charted rows come first,
    the rest sit below a dashed rule in muted ink. Empty means the idle table's order."""
    if not order:
        order = [(s.key, s.charted) for s in SERIES]
    rows = [summaries[key] for key, _ in order if key in summaries]
    n_charted = sum(1 for key, charted in order if charted and key in summaries)
    sep_gap = ROW_H // 2
    footnote_count = 1 + len(inconsistent_series(summaries))
    height = TITLE_H + HEADER_H + ROW_H * len(rows) + sep_gap + 25 + 15 * footnote_count

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {TABLE_WIDTH} {height}" '
        f'width="{TABLE_WIDTH}" height="{height}" role="img" '
        f'aria-label="{escape(text_.aria)}">',
        f'<rect width="{TABLE_WIDTH}" height="{height}" fill="{theme.background}"/>',
        '<g font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif">',
        f'<text x="{COL_LABEL_X}" y="26" font-size="17" font-weight="600" fill="{theme.text}">'
        f"{escape(text_.title)}</text>",
        f'<text x="{COL_LABEL_X}" y="44" font-size="12" fill="{theme.muted}">'
        f"{escape(source_line)}</text>",
        f'<text x="{COL_LABEL_X}" y="61" font-size="12" fill="{theme.muted}">'
        f"{escape(text_.subtitle)}</text>",
    ]

    header_y = TITLE_H + 14
    for x, text, anchor in (
        (COL_LABEL_X, "allocator", "start"),
        (COL_IDLE_X, text_.mechanism_header, "start"),
        (COL_PEAK_X, "peak RSS", "end"),
        (COL_AFTER_X, text_.after_header, "end"),
        (COL_PCT_X, "returned", "end"),
    ):
        parts.append(
            f'<text x="{x}" y="{header_y}" font-size="12" font-weight="600" '
            f'text-anchor="{anchor}" fill="{theme.muted}">{escape(text)}</text>'
        )
    rule_y = TITLE_H + HEADER_H
    parts.append(
        f'<line x1="{COL_LABEL_X}" y1="{rule_y}" x2="{TABLE_WIDTH - COL_LABEL_X}" y2="{rule_y}" '
        f'stroke="{theme.grid}" stroke-width="1"/>'
    )

    top = rule_y
    for index, row in enumerate(rows):
        if index == n_charted:
            top += sep_gap
            sep_y = top - sep_gap // 2
            parts.append(
                f'<line x1="{COL_LABEL_X}" y1="{sep_y}" '
                f'x2="{TABLE_WIDTH - COL_LABEL_X}" y2="{sep_y}" stroke="{theme.grid}" '
                f'stroke-width="1" stroke-dasharray="2,3"/>'
            )
        bottom = top + ROW_H
        text_y = bottom - 8
        if index % 2 == 1:
            parts.append(
                f'<rect x="{COL_LABEL_X - 8}" y="{top}" '
                f'width="{TABLE_WIDTH - 2 * (COL_LABEL_X - 8)}" height="{ROW_H}" '
                f'fill="{theme.grid}" opacity="0.25"/>'
            )
        ink = theme.text if index < n_charted else theme.muted
        for x, text, anchor in (
            (COL_LABEL_X, row["label"], "start"),
            (COL_IDLE_X, row["idle_mechanism"], "start"),
            (COL_PEAK_X, f"{row['peak_rss_mb']:.1f} MB", "end"),
            (COL_AFTER_X, f"{row['after_idle_rss_mb']:.1f} MB", "end"),
            (COL_PCT_X, f"{row['percent_returned']:.0f}%", "end"),
        ):
            parts.append(
                f'<text x="{x}" y="{text_y}" font-size="12" text-anchor="{anchor}" '
                f'fill="{ink}">{escape(text)}</text>'
            )
        top = bottom

    footnotes = [text_.footnote]
    # A series whose repetitions disagree is reported by its best run like every other
    # series, so the disagreement itself has to be on the image.
    for summary, returned, total in inconsistent_series(summaries):
        # The sizing run has two rows per fork build, so a shared label names the row by
        # its mechanism as well; the idle table's labels are unique and read as before.
        name = summary["label"]
        if sum(1 for row in rows if row["label"] == name) > 1:
            name = f"{name}, {summary['idle_mechanism']}"
        footnotes.append(
            f"{name} returned memory in only {returned} of {total} runs; "
            "the figure above is the best of them, as it is for every row."
        )
    for index, footnote in enumerate(footnotes):
        parts.append(
            f'<text x="{COL_LABEL_X}" y="{top + 22 + index * 15}" font-size="11" '
            f'fill="{theme.muted}">{escape(footnote)}</text>'
        )
    parts.append("</g></svg>")
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# Data files
# ---------------------------------------------------------------------------


def write_csv(path: Path, series: Mapping[str, list[Sample]]) -> None:
    lines = ["series,t_seconds,rss_mb"]
    for spec in CHARTED:
        for sample in series[spec.key]:
            lines.append(f"{spec.key},{sample.t_ms / 1000:.1f},{sample.rss_kb / 1024.0:.3f}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_csv(path: Path) -> dict[str, list[Sample]]:
    out: dict[str, list[Sample]] = {}
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:
        key, t_s, rss_mb = line.split(",")
        out.setdefault(key, []).append(
            Sample(round(float(t_s) * 1000), round(float(rss_mb) * 1024))
        )
    return out


def load_report_json(path: Path) -> ReportJson:
    result: ReportJson = json.loads(path.read_text(encoding="utf-8"))
    return result


def probe_machine() -> tuple[str, str]:
    cpu = ""
    try:
        with Path("/proc/cpuinfo").open() as handle:
            for line in handle:
                if line.startswith("model name"):
                    cpu = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    return cpu or platform.machine(), platform.release()


def format_source_line(commit: str, cpu: str, kernel: str, runs: int) -> str:
    return f"measured at {commit}, {cpu}, {kernel}, taskset -c 0-3, best of {runs} runs"


def git_commit(repo_root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--short=8", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return out.stdout.strip()


def selection_rule(runs: int) -> str:
    return (
        f"per series, the run with the lowest after-idle VmRSS of {runs} repetitions; the "
        "same rule for every allocator"
    )


def record_run(run: RunResult) -> RunRecord:
    record: RunRecord = {
        "peak_rss_mb": run.peak_rss_kb / 1024.0,
        "idle_start_rss_mb": run.idle_start_rss_kb / 1024.0,
        "after_idle_rss_mb": run.after_idle_rss_kb / 1024.0,
    }
    if run.background_thread is not None:
        record["background_thread"] = run.background_thread
    return record


def idle_mechanism_text(spec: SeriesSpec, idle: int, window: int) -> str:
    """What the "what runs during idle" column says: the hook, then anything unusual.

    A diagnostic that tunes the allocator or lengthens its window has to say so in the
    same cell as its number, not in a footnote somebody can read past.
    """
    qualifiers = [f"{name}={value}" for name, value in spec.env]
    if spec.seconds is not None:
        qualifiers.append(f"{window} s window")
    if not qualifiers:
        return IDLE_DESCRIPTIONS[idle]
    # The qualifiers are the point of a diagnostic row, so they get the column width:
    # the base description drops to its short form rather than the cell overflowing.
    return "; ".join([IDLE_SHORT[idle], *qualifiers])


def summarize(
    spec: SeriesSpec,
    build: AllocatorBuild,
    picked: RunResult,
    attempts: list[RunResult],
    idle: int,
    window: int,
) -> SeriesSummary:
    peak_mb = picked.peak_rss_kb / 1024.0
    after_mb = picked.after_idle_rss_kb / 1024.0
    return {
        "label": spec.label,
        "allocator_id": spec.allocator_id,
        "pin": build.pin,
        "idle_mechanism": idle_mechanism_text(spec, idle, window),
        "idle_seconds": window,
        "env": dict(spec.env),
        "charted": spec.charted,
        "note": spec.note,
        "runs": [record_run(run) for run in attempts],
        "peak_rss_mb": peak_mb,
        "idle_start_rss_mb": picked.idle_start_rss_kb / 1024.0,
        "after_idle_rss_mb": after_mb,
        "percent_returned": percent_returned(peak_mb, after_mb),
    }


PURGE_STATUS_NAMES = {0: "OK", 1: "PARTIAL", 2: "BUSY"}


def purge_mechanism_text(spec: BusySeriesSpec, picked: RunResult) -> str:
    """The "what the purging thread calls" cell: the call, then -- for mi_purge_all --
    what it reported, so an ungated `PARTIAL, 4 pending` sits beside its number."""
    text = PURGE_DESCRIPTIONS[spec.purge]
    if spec.purge == PURGE_MI_PURGE_ALL and picked.purge_status >= 0:
        status = PURGE_STATUS_NAMES.get(picked.purge_status, str(picked.purge_status))
        text += f" -> {status}, {picked.purge_max_pending} pending"
    return text


def summarize_busy(
    spec: BusySeriesSpec,
    build: AllocatorBuild,
    picked: RunResult,
    attempts: list[RunResult],
    window: int,
) -> BusySeriesSummary:
    peak_mb = picked.peak_rss_kb / 1024.0
    after_mb = picked.after_idle_rss_kb / 1024.0
    return {
        "label": spec.label,
        "allocator_id": spec.allocator_id,
        "pin": build.pin,
        "build_flags": list(GATED_CMAKE_ARGS) if spec.allocator_id == GATED_ALLOCATOR else [],
        "idle_mechanism": purge_mechanism_text(spec, picked),
        "idle_seconds": window,
        "env": {},
        "charted": spec.purge != PURGE_NOTHING,
        "note": spec.note,
        "runs": [record_run(run) for run in attempts],
        "peak_rss_mb": peak_mb,
        "idle_start_rss_mb": picked.idle_start_rss_kb / 1024.0,
        "after_idle_rss_mb": after_mb,
        "percent_returned": percent_returned(peak_mb, after_mb),
        "purge_status": picked.purge_status,
        "purge_max_pending": picked.purge_max_pending,
    }


def busy_order() -> list[tuple[str, bool]]:
    return [(s.key, s.purge != PURGE_NOTHING) for s in BUSY_SERIES]


def load_busy_report_json(path: Path) -> BusyReportJson:
    result: BusyReportJson = json.loads(path.read_text(encoding="utf-8"))
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def measure_all(
    build_root: Path, jobs: int, seconds: int, runs: int, lockfile: Path
) -> tuple[dict[str, list[Sample]], dict[str, SeriesSummary]]:
    records = select_records(lockfile)
    builds = build_allocators(records, build_root, jobs)
    series: dict[str, list[Sample]] = {}
    summaries: dict[str, SeriesSummary] = {}
    with tempfile.TemporaryDirectory(prefix="allocator-idle-bench-") as tmp:
        work_dir = Path(tmp)
        for spec in SERIES:
            build = builds[spec.allocator_id]
            picked, attempts, idle = measure_series(spec, build, work_dir, seconds, runs)
            window = spec.seconds if spec.seconds is not None else seconds
            summaries[spec.key] = summarize(spec, build, picked, attempts, idle, window)
            if spec.charted:
                series[spec.key] = picked.samples
            summary = summaries[spec.key]
            print(
                f"{spec.key}: peak {summary['peak_rss_mb']:.1f} MB, "
                f"after idle {summary['after_idle_rss_mb']:.1f} MB "
                f"({summary['percent_returned']:.0f}% returned; "
                f"idle = {summary['idle_mechanism']})",
                flush=True,
            )
    return series, summaries


def measure_all_busy(
    build_root: Path, jobs: int, seconds: int, runs: int, threads: int, lockfile: Path
) -> dict[str, BusySeriesSummary]:
    records = select_records(lockfile)
    builds = dict(build_allocators(records, build_root, jobs))
    builds[GATED_ALLOCATOR] = build_gated_fork(records, builds, build_root, jobs)
    builds[GLIBC_ALLOCATOR] = glibc_pseudo_build()
    summaries: dict[str, BusySeriesSummary] = {}
    with tempfile.TemporaryDirectory(prefix="allocator-purge-any-thread-") as tmp:
        work_dir = Path(tmp)
        for spec in BUSY_SERIES:
            build = builds[spec.allocator_id]
            picked, attempts = measure_busy_series(spec, build, work_dir, seconds, runs, threads)
            summaries[spec.key] = summarize_busy(spec, build, picked, attempts, seconds)
            summary = summaries[spec.key]
            print(
                f"{spec.key}: peak {summary['peak_rss_mb']:.1f} MB, "
                f"after {seconds} s {summary['after_idle_rss_mb']:.1f} MB "
                f"({summary['percent_returned']:.0f}% returned; "
                f"purge = {summary['idle_mechanism']})",
                flush=True,
            )
    return summaries


def format_busy_source_line(commit: str, cpu: str, kernel: str, runs: int, threads: int) -> str:
    return (
        f"measured at {commit}, {cpu}, {kernel}, taskset -c 0-3, {threads} busy threads, "
        f"best of {runs} runs"
    )


def main_busy(args: argparse.Namespace, out_dir: Path) -> int:
    """The sizing run: JSON + table SVGs, or --check against the committed ones."""
    threads: int = args.busy_threads
    json_path = out_dir / "allocator-purge-any-thread-report.json"
    if args.check or args.from_data:
        if not json_path.exists():
            print(f"error: {json_path} missing; run without --check first", file=sys.stderr)
            return 1
        report = load_busy_report_json(json_path)
        threads = report["busy_threads"]
        source_line = format_busy_source_line(
            report["commit"], report["cpu"], report["kernel"], report["runs_per_series"], threads
        )
        summaries = report["series"]
    else:
        if args.build_root is None:
            print("error: --build-root is required unless --check/--from-data", file=sys.stderr)
            return 1
        if shutil.which("taskset") is None:
            print("error: taskset is required to pin the workload", file=sys.stderr)
            return 1
        commit = git_commit(REPO_ROOT)
        cpu, kernel = probe_machine()
        source_line = format_busy_source_line(commit, cpu, kernel, args.runs, threads)
        summaries = measure_all_busy(
            args.build_root.resolve(), args.jobs, args.seconds, args.runs, threads, args.lockfile
        )
        payload: BusyReportJson = {
            "commit": commit,
            "cpu": cpu,
            "kernel": kernel,
            "runs_per_series": args.runs,
            "idle_seconds": args.seconds,
            "busy_threads": threads,
            "selection_rule": selection_rule(args.runs),
            "series": summaries,
        }
        json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    text_ = busy_table_text(threads)
    outputs = {
        out_dir / f"allocator-purge-any-thread-table-{theme.name}.svg": render_table_svg(
            summaries, theme, source_line, busy_order(), text_
        )
        for theme in (LIGHT, DARK)
    }
    if args.check:
        stale = [
            path
            for path, body in outputs.items()
            if not path.exists() or path.read_text(encoding="utf-8") != body
        ]
        if stale:
            print("error: stale: " + ", ".join(str(path) for path in stale), file=sys.stderr)
            return 1
        print("allocator-purge-any-thread artifacts are current")
        return 0
    for path, body in outputs.items():
        path.write_text(body, encoding="utf-8")
    print("wrote " + " and ".join(str(path) for path in outputs))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--build-root", type=Path, help="scratch dir for pinned allocator sources and builds"
    )
    parser.add_argument("--lockfile", type=Path, default=builder.default_lockfile())
    parser.add_argument("--out-dir", type=Path, default=REPO_ROOT / ".github" / "assets")
    parser.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--seconds", type=int, default=10)
    parser.add_argument(
        "--table", action="store_true", help="render the per-allocator table instead of the chart"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="re-render from committed data; fail if stale; do not re-run the workload",
    )
    parser.add_argument(
        "--from-data",
        action="store_true",
        help="render from the already-committed CSV/JSON instead of re-measuring",
    )
    parser.add_argument(
        "--busy-threads",
        type=int,
        nargs="?",
        const=4,
        default=0,
        metavar="N",
        help="the sizing run instead: N busy worker threads, purge from the main thread "
        "(default N=4 when given); writes/checks the allocator-purge-any-thread-* outputs",
    )
    args = parser.parse_args(argv)

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.busy_threads:
        return main_busy(args, out_dir)
    csv_path = out_dir / "allocator-idle-rss.csv"
    json_path = out_dir / "allocator-idle-report.json"

    if args.check or args.from_data:
        if not csv_path.exists() or not json_path.exists():
            print(
                f"error: {csv_path} / {json_path} missing; run without --check first",
                file=sys.stderr,
            )
            return 1
        series = load_csv(csv_path)
        report = load_report_json(json_path)
        # Read the committed machine/commit fields back rather than re-probing this
        # machine: otherwise the caption drifts from the SVG on every rebase or on a
        # different box, and --check could never pass anywhere but where it was made.
        source_line = format_source_line(
            report["commit"], report["cpu"], report["kernel"], report["runs_per_series"]
        )
        summaries = report["series"]
    else:
        if args.build_root is None:
            print("error: --build-root is required unless --check/--from-data", file=sys.stderr)
            return 1
        if shutil.which("taskset") is None:
            print("error: taskset is required to pin the workload", file=sys.stderr)
            return 1
        commit = git_commit(REPO_ROOT)
        cpu, kernel = probe_machine()
        source_line = format_source_line(commit, cpu, kernel, args.runs)
        series, summaries = measure_all(
            args.build_root.resolve(), args.jobs, args.seconds, args.runs, args.lockfile
        )
        payload: ReportJson = {
            "commit": commit,
            "cpu": cpu,
            "kernel": kernel,
            "runs_per_series": args.runs,
            "idle_seconds": args.seconds,
            "selection_rule": selection_rule(args.runs),
            "skipped_allocators": list(SKIPPED_ALLOCATORS),
            "series": summaries,
        }
        write_csv(csv_path, series)
        json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if args.table:
        light_path = out_dir / "allocator-idle-table-light.svg"
        dark_path = out_dir / "allocator-idle-table-dark.svg"
        light_svg = render_table_svg(summaries, LIGHT, source_line)
        dark_svg = render_table_svg(summaries, DARK, source_line)
    else:
        light_path = out_dir / "allocator-idle-rss-light.svg"
        dark_path = out_dir / "allocator-idle-rss-dark.svg"
        light_svg = render_line_chart(series, summaries, LIGHT, source_line)
        dark_svg = render_line_chart(series, summaries, DARK, source_line)

    if args.check:
        stale = [
            path
            for path, body in ((light_path, light_svg), (dark_path, dark_svg))
            if not path.exists() or path.read_text(encoding="utf-8") != body
        ]
        if stale:
            print("error: stale: " + ", ".join(str(path) for path in stale), file=sys.stderr)
            return 1
        print("allocator-idle artifacts are current")
        return 0

    light_path.write_text(light_svg, encoding="utf-8")
    dark_path.write_text(dark_svg, encoding="utf-8")
    print(f"wrote {light_path} and {dark_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
