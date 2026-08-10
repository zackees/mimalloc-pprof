#include "allocator_adapter.h"

#include <errno.h>

#include <jemalloc/jemalloc.h>

/* The static jemalloc build is configured with --with-jemalloc-prefix=je_. */
#ifndef BENCH_ALLOCATOR_VERSION
#error "BENCH_ALLOCATOR_VERSION must be a string literal supplied by the benchmark build"
#endif

static int bench_prepare_aligned_alloc(void** out, size_t alignment) {
  if (out == NULL) {
    return EINVAL;
  }
  *out = NULL;

  /* Normalize all adapters to posix_memalign's alignment contract. */
  if (alignment < sizeof(void*) || (alignment & (alignment - 1)) != 0) {
    return EINVAL;
  }
  return 0;
}

const char* bench_allocator_id(void) {
  return "jemalloc";
}

const char* bench_allocator_version(void) {
  return BENCH_ALLOCATOR_VERSION;
}

void* bench_alloc(size_t size) {
  return je_malloc(size);
}

void* bench_calloc(size_t count, size_t size) {
  return je_calloc(count, size);
}

void* bench_realloc(void* ptr, size_t size) {
  return je_realloc(ptr, size);
}

int bench_aligned_alloc(void** out, size_t alignment, size_t size) {
  const int error = bench_prepare_aligned_alloc(out, alignment);
  if (error != 0) {
    return error;
  }

  const int result = je_posix_memalign(out, alignment, size);
  if (result != 0) {
    *out = NULL;
  }
  return result;
}

void bench_free(void* ptr) {
  je_free(ptr);
}

size_t bench_usable_size(void* ptr) {
  return (ptr == NULL ? 0 : je_malloc_usable_size(ptr));
}
