/* ----------------------------------------------------------------------------
Copyright (c) 2026 mimalloc-pprof contributors
This is free software; you can redistribute it and/or modify it under the
terms of the MIT license. A copy of the license can be found in the file
"LICENSE" at the root of this distribution.
-----------------------------------------------------------------------------*/

/* #339 tier C: OUT-of-process zone enumeration -- what `leaks <pid>`, `heap <pid>`
   and `malloc_history` actually do. A child allocates a known set of blocks and
   parks; the parent obtains the child's task port (`task_for_pid`), and runs OUR
   enumerator against the CHILD's zone address with a `memory_reader_t` backed by
   `mach_vm_read_overwrite`, so every pointer the walk follows is a remote one.

   Checks:
     1. every block the child holds is reported, exactly once, with >= its size;
     2. a block the child freed and then OVERWROTE with garbage (a torn free-list
        link) neither crashes the walk nor is reported as live -- the bounds check
        in the free-list decoder is what `leaks` on a corrupted heap relies on;
     3. an unreadable root (a bogus zone address) returns KERN_FAILURE, not a crash.

   `task_for_pid` needs root or the debugging entitlement. macOS Recovery runs as
   root with SIP not enforced, so this may pass there; on a locked-down developer
   Mac it is denied. A denial is reported as a FAILURE with a distinct message and
   exit code 3, never a skip: the Recovery lane's ci/recovery_expected_failures.py
   waives it by name if and only if the guest really cannot do it, and that waiver
   turns red the day it starts passing (see that file's header). */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#if !defined(__APPLE__)
int main(void) {
  printf("test-osx-zone-introspect-remote: not macOS, nothing to test\n");
  return 0;
}
#else

#include <errno.h>
#include <signal.h>
#include <unistd.h>
#include <sys/wait.h>
#include <malloc/malloc.h>
#include <mach/mach.h>
#include <mach/mach_vm.h>
#include <mimalloc.h>

#define NBLOCKS 300
#define EXIT_TFP_DENIED 3

typedef struct { uintptr_t zone; uintptr_t blocks[NBLOCKS]; size_t sizes[NBLOCKS]; uintptr_t torn; } child_msg_t;

static malloc_zone_t* find_mimalloc_zone(void) {
  vm_address_t* zones = NULL; unsigned count = 0;
  if (malloc_get_all_zones(mach_task_self(), NULL, &zones, &count) == KERN_SUCCESS) {
    for (unsigned i = 0; i < count; i++) {
      malloc_zone_t* z = (malloc_zone_t*)zones[i];
      if (z != NULL && z->zone_name != NULL && strcmp(z->zone_name, "mimalloc") == 0) return z;
    }
  }
  malloc_zone_t* z = malloc_default_zone();
  if (z != NULL && z->zone_name != NULL && strcmp(z->zone_name, "mimalloc") == 0) return z;
  return NULL;
}

/* ---- child ---- */
static int child_main(int fd_out, int fd_in) {
  child_msg_t m; memset(&m, 0, sizeof(m));
  malloc_zone_t* z = find_mimalloc_zone();
  if (z == NULL) { fprintf(stderr, "child: no mimalloc zone\n"); return 1; }
  m.zone = (uintptr_t)z;
  for (size_t i = 0; i < NBLOCKS; i++) {
    m.sizes[i] = (i % 5 == 0) ? 8192 + i : 24 + (i * 17) % 300;
    void* p = mi_malloc(m.sizes[i]);
    memset(p, 0xA5, m.sizes[i]);
    m.blocks[i] = (uintptr_t)p;
  }
  // Free every 3rd (they stay on this thread's free lists), and tear ONE freed
  // block's free-list link: overwrite its first word with garbage. The enumerator
  // must bound the walk on that page instead of chasing the garbage pointer.
  for (size_t i = 2; i < NBLOCKS; i += 3) {
    mi_free((void*)m.blocks[i]);
    m.blocks[i] = 0;
  }
  {
    void* tear = mi_malloc(24 + 5 * 17 % 300);   // same size class as some freed ones
    mi_free(tear);
    memset(tear, 0xEE, sizeof(void*));           // deliberate use-after-free: torn link
    m.torn = (uintptr_t)tear;
  }
  if (write(fd_out, &m, sizeof(m)) != (ssize_t)sizeof(m)) return 1;
  // park until the parent is done (it closes the pipe or writes a byte)
  char c; (void)read(fd_in, &c, 1);
  return 0;
}

/* ---- parent ---- */
static task_t g_task;
static child_msg_t g_msg;
static unsigned g_hits[NBLOCKS];
static unsigned long g_ranges, g_torn_hits, g_reads;
static int failures = 0;
#define CHECK(cond, ...) do { if (!(cond)) { failures++; printf("FAIL: " __VA_ARGS__); printf("\n"); } } while (0)

static kern_return_t remote_reader(task_t task, vm_address_t addr, vm_size_t size, void** out) {
  // libmalloc's readers hand back a pointer that stays valid until the next read;
  // a growable scratch buffer reproduces that contract.
  static void* buf = NULL; static vm_size_t cap = 0;
  if (size > cap) {
    void* nb = realloc(buf, (size_t)size);
    if (nb == NULL) return KERN_FAILURE;
    buf = nb; cap = size;
  }
  mach_vm_size_t got = 0;
  kern_return_t kr = mach_vm_read_overwrite(task, (mach_vm_address_t)addr, (mach_vm_size_t)size,
                                            (mach_vm_address_t)buf, &got);
  g_reads++;
  if (kr != KERN_SUCCESS || got != (mach_vm_size_t)size) return KERN_FAILURE;
  *out = buf;
  return KERN_SUCCESS;
}

static void recorder(task_t task, void* ctx, unsigned type, vm_range_t* ranges, unsigned count) {
  (void)task; (void)ctx;
  if (type != MALLOC_PTR_IN_USE_RANGE_TYPE) return;
  for (unsigned i = 0; i < count; i++) {
    g_ranges++;
    if (ranges[i].address == g_msg.torn) g_torn_hits++;
    for (size_t b = 0; b < NBLOCKS; b++) {
      if (g_msg.blocks[b] != 0 && g_msg.blocks[b] == ranges[i].address) {
        g_hits[b]++;
        if (ranges[i].size < g_msg.sizes[b]) {
          failures++;
          printf("FAIL: block %zu reported size %lu < requested %zu\n", b, (unsigned long)ranges[i].size, g_msg.sizes[b]);
        }
      }
    }
  }
}

int main(void) {
  int to_parent[2], to_child[2];
  if (pipe(to_parent) != 0 || pipe(to_child) != 0) { perror("pipe"); return 1; }
  pid_t pid = fork();
  if (pid < 0) { perror("fork"); return 1; }
  if (pid == 0) {
    close(to_parent[0]); close(to_child[1]);
    int rc = child_main(to_parent[1], to_child[0]);
    _exit(rc);
  }
  close(to_parent[1]); close(to_child[0]);
  if (read(to_parent[0], &g_msg, sizeof(g_msg)) != (ssize_t)sizeof(g_msg)) {
    printf("FAIL: child did not report its blocks\n");
    kill(pid, SIGKILL); return 1;
  }

  kern_return_t kr = task_for_pid(mach_task_self(), pid, &g_task);
  if (kr != KERN_SUCCESS) {
    printf("FAIL: task_for_pid(%d) denied: kr=%d (%s); euid=%d. Out-of-process enumeration "
           "cannot be exercised here (needs root or the debugging entitlement).\n",
           (int)pid, (int)kr, mach_error_string(kr), (int)geteuid());
    (void)write(to_child[1], "x", 1); waitpid(pid, NULL, 0);
    return EXIT_TFP_DENIED;
  }

  // We are the same binary, so our own zone's introspection table is the child's.
  malloc_zone_t* zone = find_mimalloc_zone();
  CHECK(zone != NULL && zone->introspect != NULL && zone->introspect->enumerator != NULL, "no local mimalloc zone/enumerator");
  if (failures) { (void)write(to_child[1], "x", 1); waitpid(pid, NULL, 0); return 1; }

  // 1 + 2: enumerate the CHILD's zone through the remote reader.
  kr = zone->introspect->enumerator(g_task, NULL, MALLOC_PTR_IN_USE_RANGE_TYPE,
                                     (vm_address_t)g_msg.zone, remote_reader, recorder);
  printf("remote enumeration: kr=%d, %lu in-use ranges, %lu remote reads\n", (int)kr, g_ranges, g_reads);
  CHECK(kr == KERN_SUCCESS, "remote enumerator returned %d", (int)kr);
  CHECK(g_ranges > 0, "no in-use ranges from the child");
  size_t missed = 0, dup = 0, live = 0;
  for (size_t i = 0; i < NBLOCKS; i++) {
    if (g_msg.blocks[i] == 0) continue;
    live++;
    if (g_hits[i] == 0) missed++; else if (g_hits[i] > 1) dup++;
  }
  printf("child holds %zu live blocks: %zu missed, %zu duplicated, torn block reported %lu times\n", live, missed, dup, g_torn_hits);
  CHECK(missed == 0, "%zu of the child's live blocks were not reported", missed);
  CHECK(dup == 0, "%zu blocks reported more than once", dup);
  CHECK(g_torn_hits == 0, "the freed+torn block was reported as live");

  // 3: an unreadable root must fail cleanly.
  g_ranges = 0;
  kr = zone->introspect->enumerator(g_task, NULL, MALLOC_PTR_IN_USE_RANGE_TYPE,
                                     (vm_address_t)0x10, remote_reader, recorder);
  CHECK(kr != KERN_SUCCESS, "enumerating an unreadable zone address must return an error, got KERN_SUCCESS");
  CHECK(g_ranges == 0, "an unreadable root still produced %lu ranges", g_ranges);

  (void)write(to_child[1], "x", 1);
  int status = 0; waitpid(pid, &status, 0);
  CHECK(WIFEXITED(status) && WEXITSTATUS(status) == 0, "child exited abnormally (status %d)", status);
  mach_port_deallocate(mach_task_self(), g_task);
  if (failures == 0) printf("test-osx-zone-introspect-remote: OK\n");
  return failures == 0 ? 0 : 1;
}
#endif
