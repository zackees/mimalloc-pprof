#include "allocator_adapter.h"

#include <cerrno>

#include <tcmalloc/tcmalloc.h>

#ifndef BENCH_ALLOCATOR_VERSION
#error "BENCH_ALLOCATOR_VERSION must be a string literal supplied by the benchmark build"
#endif

namespace {

int bench_prepare_aligned_alloc(void** out, size_t alignment) {
  if (out == nullptr) {
    return EINVAL;
  }
  *out = nullptr;

  /* Normalize all adapters to posix_memalign's alignment contract. */
  if (alignment < sizeof(void*) || (alignment & (alignment - 1)) != 0) {
    return EINVAL;
  }
  return 0;
}

}  // namespace

extern "C" const char* bench_allocator_id(void) {
  return "tcmalloc";
}

extern "C" const char* bench_allocator_version(void) {
  return BENCH_ALLOCATOR_VERSION;
}

extern "C" void* bench_alloc(size_t size) {
  return TCMallocInternalMalloc(size);
}

extern "C" void* bench_calloc(size_t count, size_t size) {
  return TCMallocInternalCalloc(count, size);
}

extern "C" void* bench_realloc(void* ptr, size_t size) {
  return TCMallocInternalRealloc(ptr, size);
}

extern "C" int bench_aligned_alloc(void** out, size_t alignment, size_t size) {
  const int error = bench_prepare_aligned_alloc(out, alignment);
  if (error != 0) {
    return error;
  }

  const int result = TCMallocInternalPosixMemalign(out, alignment, size);
  if (result != 0) {
    *out = nullptr;
  }
  return result;
}

extern "C" void bench_free(void* ptr) {
  TCMallocInternalFree(ptr);
}

extern "C" size_t bench_usable_size(void* ptr) {
  return (ptr == nullptr ? 0 : TCMallocInternalMallocSize(ptr));
}
