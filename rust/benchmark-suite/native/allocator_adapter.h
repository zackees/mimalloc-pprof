/*
  A deliberately small, allocator-neutral ABI for benchmark children.

  Each child links one implementation of these symbols.  `NULL` from a
  pointer-returning function is an allocation failure and invalidates the
  sample; the workload must not substitute a second allocator.  The aligned
  form returns POSIX-style error codes and accepts an arbitrary requested size
  (it deliberately does not round the size up to the alignment).
*/
#ifndef MIMALLOC_PPROF_BENCHMARK_SUITE_ALLOCATOR_ADAPTER_H
#define MIMALLOC_PPROF_BENCHMARK_SUITE_ALLOCATOR_ADAPTER_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

const char* bench_allocator_id(void);
const char* bench_allocator_version(void);

void* bench_alloc(size_t size);
void* bench_calloc(size_t count, size_t size);
void* bench_realloc(void* ptr, size_t size);
int bench_aligned_alloc(void** out, size_t alignment, size_t size);
void bench_free(void* ptr);
size_t bench_usable_size(void* ptr);

#ifdef __cplusplus
}  /* extern "C" */
#endif

#endif  /* MIMALLOC_PPROF_BENCHMARK_SUITE_ALLOCATOR_ADAPTER_H */
