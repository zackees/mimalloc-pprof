#include "allocator_adapter.h"

#include <errno.h>

#include "mimalloc.h"

/*
  The same source builds every mimalloc child.  build.rs supplies string
  literals for the lockfile ID (upstream-mimalloc, bun-mimalloc or
  mimalloc-pprof) and source identity, making a stale/wrong adapter build fail
  at compile time instead of silently changing a measurement's identity.
*/
#ifndef BENCH_ALLOCATOR_ID
#error "BENCH_ALLOCATOR_ID must be a string literal supplied by the benchmark build"
#endif

#ifndef BENCH_ALLOCATOR_VERSION
#error "BENCH_ALLOCATOR_VERSION must be a string literal supplied by the benchmark build"
#endif

static int bench_prepare_aligned_alloc(void** out, size_t alignment) {
  if (out == NULL) {
    return EINVAL;
  }
  *out = NULL;

  /* This is the portable posix_memalign domain used by every adapter. */
  if (alignment < sizeof(void*) || (alignment & (alignment - 1)) != 0) {
    return EINVAL;
  }
  return 0;
}

const char* bench_allocator_id(void) {
  return BENCH_ALLOCATOR_ID;
}

const char* bench_allocator_version(void) {
  return BENCH_ALLOCATOR_VERSION;
}

void* bench_alloc(size_t size) {
  return mi_malloc(size);
}

void* bench_calloc(size_t count, size_t size) {
  return mi_calloc(count, size);
}

void* bench_realloc(void* ptr, size_t size) {
  return mi_realloc(ptr, size);
}

int bench_aligned_alloc(void** out, size_t alignment, size_t size) {
  const int error = bench_prepare_aligned_alloc(out, alignment);
  if (error != 0) {
    return error;
  }

  void* const ptr = mi_malloc_aligned(size, alignment);
  if (ptr == NULL) {
    return ENOMEM;
  }
  *out = ptr;
  return 0;
}

void bench_free(void* ptr) {
  mi_free(ptr);
}

size_t bench_usable_size(void* ptr) {
  return (ptr == NULL ? 0 : mi_usable_size(ptr));
}
