/* ----------------------------------------------------------------------------
Copyright (c) 2026 mimalloc-pprof contributors
This is free software; you can redistribute it and/or modify it under the
terms of the MIT license. A copy of the license can be found in the file
"LICENSE" at the root of this distribution.
-----------------------------------------------------------------------------*/

/* #339 tier A+B: in-process malloc-zone introspection.

   `leaks`, `heap`, `vmmap` and `malloc_history` all reach a zone's memory through
   `zone->introspect->enumerator`. Upstream mimalloc ships that as an empty stub, so
   those tools see a zone that owns nothing. This test drives the same entry point
   the tools call, in-process, with the identity reader libmalloc uses for the
   local case, and checks that:

     1. every live block we hold is reported as an in-use range with at least the
        requested size, exactly once;
     2. a block we freed is NOT reported (free-list decoding works);
     3. every reported in-use range is inside memory mimalloc owns;
     4. region and admin ranges are reported and non-empty;
     5. `statistics` reports a non-zero size_in_use (Bun parity).

   Runs in macOS Recovery: libsystem_malloc and its introspection table are in the
   Recovery dyld image; only the `leaks`/`vmmap` CLIs are absent. Labelled `macos`
   (ci/check_macos_labels.py enforces it) so the selective lane executes it. */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#if !defined(__APPLE__)
int main(void) {
  printf("test-osx-zone-introspect: not macOS, nothing to test\n");
  return 0;
}
#else

#include <malloc/malloc.h>
#include <mach/mach.h>
#include <mimalloc.h>

#define NBLOCKS 512

static void* blocks[NBLOCKS];
static size_t sizes[NBLOCKS];
static unsigned hits[NBLOCKS];
static unsigned long in_use_ranges, region_ranges, admin_ranges, out_of_heap;
static int failures = 0;

#define CHECK(cond, ...) do { if (!(cond)) { failures++; printf("FAIL: " __VA_ARGS__); printf("\n"); } } while (0)

static kern_return_t identity_reader(task_t task, vm_address_t addr, vm_size_t size, void** out) {
  (void)task; (void)size;
  *out = (void*)addr;
  return KERN_SUCCESS;
}

static void recorder(task_t task, void* ctx, unsigned type, vm_range_t* ranges, unsigned count) {
  (void)task; (void)ctx;
  for (unsigned i = 0; i < count; i++) {
    const vm_range_t r = ranges[i];
    if (type == MALLOC_PTR_IN_USE_RANGE_TYPE) {
      in_use_ranges++;
      if (!mi_is_in_heap_region((void*)r.address)) out_of_heap++;
      for (size_t b = 0; b < NBLOCKS; b++) {
        if (blocks[b] != NULL && (vm_address_t)blocks[b] == r.address) {
          hits[b]++;
          if (r.size < sizes[b]) {
            failures++;
            printf("FAIL: block %zu reported size %lu < requested %zu\n", b, (unsigned long)r.size, sizes[b]);
          }
        }
      }
    }
    else if (type == MALLOC_PTR_REGION_RANGE_TYPE) region_ranges++;
    else if (type == MALLOC_ADMIN_REGION_RANGE_TYPE) admin_ranges++;
  }
}

static void** g_freed; static size_t g_nfreed; static unsigned long g_freed_hits;
static void rec_freed(task_t task, void* ctx, unsigned type, vm_range_t* ranges, unsigned count) {
  (void)task; (void)ctx; (void)type;
  for (unsigned i = 0; i < count; i++) {
    for (size_t f = 0; f < g_nfreed; f++) {
      if ((vm_address_t)g_freed[f] == ranges[i].address) g_freed_hits++;
    }
  }
}

static malloc_zone_t* find_mimalloc_zone(void) {
  // Prefer the zone by name: with the static lib the constructor makes it the default
  // zone; under interpose `malloc_get_all_zones` is stubbed to zero zones (Bun parity),
  // so fall back to the default zone in that case.
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

int main(void) {
  malloc_zone_t* zone = find_mimalloc_zone();
  if (zone == NULL) {
    printf("FAIL: no zone named \"mimalloc\" is registered (default zone: %s)\n",
           malloc_default_zone() && malloc_default_zone()->zone_name ? malloc_default_zone()->zone_name : "?");
    return 1;
  }
  printf("zone \"%s\" version %u, introspect=%p enumerator=%p\n",
         zone->zone_name, zone->version, (void*)zone->introspect, (void*)(zone->introspect ? zone->introspect->enumerator : NULL));
  CHECK(zone->introspect != NULL && zone->introspect->enumerator != NULL, "no enumerator");
  if (failures) return 1;

  // A mix of small, medium and large blocks, some freed again so the free-list
  // decoding is exercised on pages that are neither empty nor full.
  for (size_t i = 0; i < NBLOCKS; i++) {
    sizes[i] = (i % 7 == 0) ? 64 * 1024 + i : (i % 3 == 0) ? 4096 + i : 16 + (i * 13) % 200;
    blocks[i] = mi_malloc(sizes[i]);
    CHECK(blocks[i] != NULL, "mi_malloc(%zu) failed", sizes[i]);
  }
  void* freed[NBLOCKS / 4]; size_t nfreed = 0;
  for (size_t i = 1; i < NBLOCKS; i += 4) {   // free every 4th: pages become partially used
    freed[nfreed++] = blocks[i];
    mi_free(blocks[i]);
    blocks[i] = NULL;
  }

  kern_return_t kr = zone->introspect->enumerator(mach_task_self(), NULL,
      MALLOC_PTR_IN_USE_RANGE_TYPE | MALLOC_PTR_REGION_RANGE_TYPE | MALLOC_ADMIN_REGION_RANGE_TYPE,
      (vm_address_t)zone, identity_reader, recorder);
  CHECK(kr == KERN_SUCCESS, "enumerator returned %d", (int)kr);
  printf("in-use ranges: %lu, region ranges: %lu, admin ranges: %lu, outside heap: %lu\n",
         in_use_ranges, region_ranges, admin_ranges, out_of_heap);

  CHECK(in_use_ranges > 0, "no in-use ranges reported");
  CHECK(region_ranges > 0, "no region ranges reported");
  CHECK(admin_ranges > 0, "no admin (arena) ranges reported");
  CHECK(out_of_heap == 0, "%lu in-use ranges outside mimalloc's heap region", out_of_heap);
  size_t missed = 0, dup = 0;
  for (size_t i = 0; i < NBLOCKS; i++) {
    if (blocks[i] == NULL) continue;
    if (hits[i] == 0) missed++;
    else if (hits[i] > 1) dup++;
  }
  CHECK(missed == 0, "%zu live blocks were not reported as in-use", missed);
  CHECK(dup == 0, "%zu live blocks were reported more than once", dup);

  // A freed block must not be reported: second pass with a recorder that only looks
  // for the freed pointers (and a NULL recorder must be a no-op success).
  kr = zone->introspect->enumerator(mach_task_self(), NULL, MALLOC_PTR_IN_USE_RANGE_TYPE,
                                     (vm_address_t)zone, NULL, NULL);
  CHECK(kr == KERN_SUCCESS, "enumerator with a NULL recorder must be a no-op success, got %d", (int)kr);
  g_freed = freed; g_nfreed = nfreed; g_freed_hits = 0;
  kr = zone->introspect->enumerator(mach_task_self(), NULL, MALLOC_PTR_IN_USE_RANGE_TYPE,
                                     (vm_address_t)zone, NULL /* NULL reader = identity */, rec_freed);
  CHECK(kr == KERN_SUCCESS, "second enumerator pass returned %d", (int)kr);
  CHECK(g_freed_hits == 0, "%lu freed blocks were reported as in-use", g_freed_hits);

  // Tier B: statistics (Bun parity) -- a lower bound, but must not be zero with 384 live blocks.
  if (zone->introspect->statistics != NULL) {
    malloc_statistics_t st; memset(&st, 0, sizeof(st));
    zone->introspect->statistics(zone, &st);
    printf("statistics: blocks_in_use=%u size_in_use=%zu max_size_in_use=%zu size_allocated=%zu\n",
           st.blocks_in_use, st.size_in_use, st.max_size_in_use, st.size_allocated);
    CHECK(st.size_allocated > 0, "statistics.size_allocated is 0");
    // size_in_use comes from the malloc_normal/malloc_huge counters, which mimalloc only
    // maintains with MI_STAT >= 1 -- i.e. in MI_DEBUG builds (types.h: MI_STAT defaults
    // to 2 when MI_DEBUG>0, else 0). A release build reports 0 here, same as Bun; the
    // Recovery lane measured exactly that (release 0, debug-full non-zero, PR #349).
#if defined(MI_DEBUG) && (MI_DEBUG > 0)
    CHECK(st.size_in_use > 0, "statistics.size_in_use is 0 with live blocks (MI_DEBUG build tracks it)");
#endif
    CHECK(st.size_in_use <= st.size_allocated, "size_in_use %zu > size_allocated %zu", st.size_in_use, st.size_allocated);
  }

  for (size_t i = 0; i < NBLOCKS; i++) { if (blocks[i]) mi_free(blocks[i]); }
  if (failures == 0) printf("test-osx-zone-introspect: OK\n");
  return failures == 0 ? 0 : 1;
}
#endif
