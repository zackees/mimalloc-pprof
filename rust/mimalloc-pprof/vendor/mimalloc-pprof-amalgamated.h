/* GENERATED FILE -- DO NOT EDIT. Produced by rust/xtask from commit f96cc337 of the three public headers (mimalloc.h, mimalloc/profile.h, mimalloc/memory-events.h). Regenerate with: cargo run -p xtask -- amalgamate-h */

/* ---- begin inlined: include/mimalloc.h ---- */
/* ----------------------------------------------------------------------------
Copyright (c) 2018-2026, Microsoft Research, Daan Leijen
This is free software; you can redistribute it and/or modify it under the
terms of the MIT license. A copy of the license can be found in the file
"LICENSE" at the root of this distribution.
-----------------------------------------------------------------------------*/
#pragma once
#ifndef MIMALLOC_H
#define MIMALLOC_H

#define MI_MALLOC_VERSION 20401  // major + 2 digits minor + 2 digits patch

// ------------------------------------------------------
// Compiler specific attributes
// ------------------------------------------------------

#ifdef __cplusplus
  #if (__cplusplus >= 201103L) || (_MSC_VER > 1900)  // C++11
    #define mi_attr_noexcept   noexcept
  #else
    #define mi_attr_noexcept   throw()
  #endif
#else
  #define mi_attr_noexcept
#endif

#if defined(__cplusplus) && (__cplusplus >= 201703)
  #define mi_decl_nodiscard    [[nodiscard]]
#elif (defined(__GNUC__) && (__GNUC__ >= 4)) || defined(__clang__)  // includes clang, icc, and clang-cl
  #define mi_decl_nodiscard    __attribute__((warn_unused_result))
#elif defined(_HAS_NODISCARD)
  #define mi_decl_nodiscard    _NODISCARD
#elif (_MSC_VER >= 1700)
  #define mi_decl_nodiscard    _Check_return_
#else
  #define mi_decl_nodiscard
#endif

#if defined(_MSC_VER) || defined(__MINGW32__)
  #if !defined(MI_SHARED_LIB)
    #define mi_decl_export
  #elif defined(MI_SHARED_LIB_EXPORT)
    #define mi_decl_export              __declspec(dllexport)
  #else
    #define mi_decl_export              __declspec(dllimport)
  #endif
  #if defined(__MINGW32__)
    #define mi_decl_restrict
    #define mi_attr_malloc              __attribute__((malloc))
  #else
    #if (_MSC_VER >= 1900) && !defined(__EDG__)
      #define mi_decl_restrict          __declspec(allocator) __declspec(restrict)
    #else
      #define mi_decl_restrict          __declspec(restrict)
    #endif
    #define mi_attr_malloc
  #endif
  #define mi_cdecl                      __cdecl
  #define mi_attr_alloc_size(s)
  #define mi_attr_alloc_size2(s1,s2)
  #define mi_attr_alloc_align(p)
#elif defined(__GNUC__)                 // includes clang and icc
  #if defined(MI_SHARED_LIB) && defined(MI_SHARED_LIB_EXPORT)
    #define mi_decl_export              __attribute__((visibility("default")))
  #else
    #define mi_decl_export
  #endif
  #define mi_cdecl                      // leads to warnings... __attribute__((cdecl))
  #define mi_decl_restrict
  #define mi_attr_malloc                __attribute__((malloc))
  #if (defined(__clang_major__) && (__clang_major__ < 4)) || (__GNUC__ < 5)
    #define mi_attr_alloc_size(s)
    #define mi_attr_alloc_size2(s1,s2)
    #define mi_attr_alloc_align(p)
  #elif defined(__INTEL_COMPILER)
    #define mi_attr_alloc_size(s)       __attribute__((alloc_size(s)))
    #define mi_attr_alloc_size2(s1,s2)  __attribute__((alloc_size(s1,s2)))
    #define mi_attr_alloc_align(p)
  #else
    #define mi_attr_alloc_size(s)       __attribute__((alloc_size(s)))
    #define mi_attr_alloc_size2(s1,s2)  __attribute__((alloc_size(s1,s2)))
    #define mi_attr_alloc_align(p)      __attribute__((alloc_align(p)))
  #endif
#else
  #define mi_cdecl
  #define mi_decl_export
  #define mi_decl_restrict
  #define mi_attr_malloc
  #define mi_attr_alloc_size(s)
  #define mi_attr_alloc_size2(s1,s2)
  #define mi_attr_alloc_align(p)
#endif

// ------------------------------------------------------
// Includes
// ------------------------------------------------------

#include <stddef.h>     // size_t, wchar_t
#include <stdbool.h>    // bool
#include <stdint.h>     // INTPTR_MAX

#ifdef __cplusplus
extern "C" {
#endif

// ------------------------------------------------------
// Standard malloc interface
// ------------------------------------------------------

mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_malloc(size_t size)  mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size(1);
mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_calloc(size_t count, size_t size)  mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size2(1,2);
mi_decl_nodiscard mi_decl_export void* mi_realloc(void* p, size_t newsize)      mi_attr_noexcept mi_attr_alloc_size(2);
mi_decl_export void* mi_expand(void* p, size_t newsize)                         mi_attr_noexcept mi_attr_alloc_size(2);

mi_decl_export void mi_free(void* p) mi_attr_noexcept;
mi_decl_nodiscard mi_decl_export mi_decl_restrict char* mi_strdup(const char* s) mi_attr_noexcept mi_attr_malloc;
mi_decl_nodiscard mi_decl_export mi_decl_restrict char* mi_strndup(const char* s, size_t n) mi_attr_noexcept mi_attr_malloc;
mi_decl_nodiscard mi_decl_export mi_decl_restrict char* mi_realpath(const char* fname, char* resolved_name) mi_attr_noexcept;

// ------------------------------------------------------
// Extended functionality
// ------------------------------------------------------
#define MI_SMALL_WSIZE_MAX  (128)
#define MI_SMALL_SIZE_MAX   (MI_SMALL_WSIZE_MAX*sizeof(void*))

mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_malloc_small(size_t size) mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size(1);
mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_zalloc_small(size_t size) mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size(1);
mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_zalloc(size_t size)       mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size(1);

mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_mallocn(size_t count, size_t size) mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size2(1,2);
mi_decl_nodiscard mi_decl_export void* mi_reallocn(void* p, size_t count, size_t size)        mi_attr_noexcept mi_attr_alloc_size2(2,3);
mi_decl_nodiscard mi_decl_export void* mi_reallocf(void* p, size_t newsize)                   mi_attr_noexcept mi_attr_alloc_size(2);

mi_decl_nodiscard mi_decl_export size_t mi_usable_size(const void* p) mi_attr_noexcept;
mi_decl_nodiscard mi_decl_export size_t mi_good_size(size_t size)     mi_attr_noexcept;

// for compat with v3.
mi_decl_export void mi_free_small(void* p) mi_attr_noexcept;

// ------------------------------------------------------
// Internals
// ------------------------------------------------------

typedef void (mi_cdecl mi_deferred_free_fun)(bool force, unsigned long long heartbeat, void* arg);
mi_decl_export void mi_register_deferred_free(mi_deferred_free_fun* deferred_free, void* arg) mi_attr_noexcept;

typedef void (mi_cdecl mi_output_fun)(const char* msg, void* arg);
mi_decl_export void mi_register_output(mi_output_fun* out, void* arg) mi_attr_noexcept;

typedef void (mi_cdecl mi_error_fun)(int err, void* arg);
mi_decl_export void mi_register_error(mi_error_fun* fun, void* arg);

mi_decl_export void mi_collect(bool force)    mi_attr_noexcept;
mi_decl_export int  mi_version(void)          mi_attr_noexcept;
mi_decl_export void mi_stats_reset(void)      mi_attr_noexcept;
mi_decl_export void mi_stats_merge(void)      mi_attr_noexcept;
mi_decl_export void mi_stats_print(void* out) mi_attr_noexcept;  // backward compatibility: `out` is ignored and should be NULL
mi_decl_export void mi_stats_print_out(mi_output_fun* out, void* arg) mi_attr_noexcept;
mi_decl_export void mi_thread_stats_print_out(mi_output_fun* out, void* arg) mi_attr_noexcept;
mi_decl_export void mi_options_print(void)    mi_attr_noexcept;

mi_decl_export void mi_process_info(size_t* elapsed_msecs, size_t* user_msecs, size_t* system_msecs,
                                    size_t* current_rss, size_t* peak_rss,
                                    size_t* current_commit, size_t* peak_commit, size_t* page_faults) mi_attr_noexcept;


// Generally do not use the following as these are usually called automatically
mi_decl_export void mi_process_init(void)     mi_attr_noexcept;
mi_decl_export void mi_cdecl mi_process_done(void) mi_attr_noexcept;
mi_decl_export void mi_thread_init(void)      mi_attr_noexcept;
mi_decl_export void mi_thread_done(void)      mi_attr_noexcept;


// -------------------------------------------------------------------------------------
// Aligned allocation
// Note that `alignment` always follows `size` for consistency with unaligned
// allocation, but unfortunately this differs from `posix_memalign` and `aligned_alloc`.
// -------------------------------------------------------------------------------------

mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_malloc_aligned(size_t size, size_t alignment) mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size(1) mi_attr_alloc_align(2);
mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_malloc_aligned_at(size_t size, size_t alignment, size_t offset) mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size(1);
mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_zalloc_aligned(size_t size, size_t alignment) mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size(1) mi_attr_alloc_align(2);
mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_zalloc_aligned_at(size_t size, size_t alignment, size_t offset) mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size(1);
mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_calloc_aligned(size_t count, size_t size, size_t alignment) mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size2(1,2) mi_attr_alloc_align(3);
mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_calloc_aligned_at(size_t count, size_t size, size_t alignment, size_t offset) mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size2(1,2);
mi_decl_nodiscard mi_decl_export void* mi_realloc_aligned(void* p, size_t newsize, size_t alignment) mi_attr_noexcept mi_attr_alloc_size(2) mi_attr_alloc_align(3);
mi_decl_nodiscard mi_decl_export void* mi_realloc_aligned_at(void* p, size_t newsize, size_t alignment, size_t offset) mi_attr_noexcept mi_attr_alloc_size(2);


// -----------------------------------------------------------------
// Return allocated block size (if the return value is not NULL)
// -----------------------------------------------------------------

mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_umalloc(size_t size, size_t* block_size)  mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size(1);
mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_ucalloc(size_t count, size_t size, size_t* block_size)  mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size2(1,2);
mi_decl_nodiscard mi_decl_export void* mi_urealloc(void* p, size_t newsize, size_t* block_size_pre, size_t* block_size_post) mi_attr_noexcept mi_attr_alloc_size(2);
mi_decl_export void mi_ufree(void* p, size_t* block_size) mi_attr_noexcept;

mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_umalloc_aligned(size_t size, size_t alignment, size_t* block_size) mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size(1) mi_attr_alloc_align(2);
mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_uzalloc_aligned(size_t size, size_t alignment, size_t* block_size) mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size(1) mi_attr_alloc_align(2);

mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_umalloc_small(size_t size, size_t* block_size) mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size(1);
mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_uzalloc_small(size_t size, size_t* block_size) mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size(1);


// -------------------------------------------------------------------------------------
// Heaps: first-class, but can only allocate from the same thread that created it.
// -------------------------------------------------------------------------------------

struct mi_heap_s;
typedef struct mi_heap_s mi_heap_t;

mi_decl_nodiscard mi_decl_export mi_heap_t* mi_heap_new(void);
mi_decl_export void       mi_heap_delete(mi_heap_t* heap);
mi_decl_export void       mi_heap_destroy(mi_heap_t* heap);
mi_decl_export mi_heap_t* mi_heap_set_default(mi_heap_t* heap);
mi_decl_export mi_heap_t* mi_heap_get_default(void);
mi_decl_export mi_heap_t* mi_heap_get_backing(void);
mi_decl_export void       mi_heap_collect(mi_heap_t* heap, bool force) mi_attr_noexcept;

mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_heap_malloc(mi_heap_t* heap, size_t size) mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size(2);
mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_heap_zalloc(mi_heap_t* heap, size_t size) mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size(2);
mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_heap_calloc(mi_heap_t* heap, size_t count, size_t size) mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size2(2, 3);
mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_heap_mallocn(mi_heap_t* heap, size_t count, size_t size) mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size2(2, 3);
mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_heap_malloc_small(mi_heap_t* heap, size_t size) mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size(2);
mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_heap_zalloc_small(mi_heap_t* heap, size_t size) mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size(2);

mi_decl_nodiscard mi_decl_export void* mi_heap_realloc(mi_heap_t* heap, void* p, size_t newsize)              mi_attr_noexcept mi_attr_alloc_size(3);
mi_decl_nodiscard mi_decl_export void* mi_heap_reallocn(mi_heap_t* heap, void* p, size_t count, size_t size)  mi_attr_noexcept mi_attr_alloc_size2(3,4);
mi_decl_nodiscard mi_decl_export void* mi_heap_reallocf(mi_heap_t* heap, void* p, size_t newsize)             mi_attr_noexcept mi_attr_alloc_size(3);

mi_decl_nodiscard mi_decl_export mi_decl_restrict char* mi_heap_strdup(mi_heap_t* heap, const char* s)            mi_attr_noexcept mi_attr_malloc;
mi_decl_nodiscard mi_decl_export mi_decl_restrict char* mi_heap_strndup(mi_heap_t* heap, const char* s, size_t n) mi_attr_noexcept mi_attr_malloc;
mi_decl_nodiscard mi_decl_export mi_decl_restrict char* mi_heap_realpath(mi_heap_t* heap, const char* fname, char* resolved_name) mi_attr_noexcept;

mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_heap_malloc_aligned(mi_heap_t* heap, size_t size, size_t alignment) mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size(2) mi_attr_alloc_align(3);
mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_heap_malloc_aligned_at(mi_heap_t* heap, size_t size, size_t alignment, size_t offset) mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size(2);
mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_heap_zalloc_aligned(mi_heap_t* heap, size_t size, size_t alignment) mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size(2) mi_attr_alloc_align(3);
mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_heap_zalloc_aligned_at(mi_heap_t* heap, size_t size, size_t alignment, size_t offset) mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size(2);
mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_heap_calloc_aligned(mi_heap_t* heap, size_t count, size_t size, size_t alignment) mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size2(2, 3) mi_attr_alloc_align(4);
mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_heap_calloc_aligned_at(mi_heap_t* heap, size_t count, size_t size, size_t alignment, size_t offset) mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size2(2, 3);
mi_decl_nodiscard mi_decl_export void* mi_heap_realloc_aligned(mi_heap_t* heap, void* p, size_t newsize, size_t alignment) mi_attr_noexcept mi_attr_alloc_size(3) mi_attr_alloc_align(4);
mi_decl_nodiscard mi_decl_export void* mi_heap_realloc_aligned_at(mi_heap_t* heap, void* p, size_t newsize, size_t alignment, size_t offset) mi_attr_noexcept mi_attr_alloc_size(3);


// --------------------------------------------------------------------------------
// Zero initialized re-allocation.
// Only valid on memory that was originally allocated with zero initialization too.
// e.g. `mi_calloc`, `mi_zalloc`, `mi_zalloc_aligned` etc.
// see <https://github.com/microsoft/mimalloc/issues/63#issuecomment-508272992>
// --------------------------------------------------------------------------------

mi_decl_nodiscard mi_decl_export void* mi_rezalloc(void* p, size_t newsize)                mi_attr_noexcept mi_attr_alloc_size(2);
mi_decl_nodiscard mi_decl_export void* mi_recalloc(void* p, size_t newcount, size_t size)  mi_attr_noexcept mi_attr_alloc_size2(2,3);

mi_decl_nodiscard mi_decl_export void* mi_rezalloc_aligned(void* p, size_t newsize, size_t alignment) mi_attr_noexcept mi_attr_alloc_size(2) mi_attr_alloc_align(3);
mi_decl_nodiscard mi_decl_export void* mi_rezalloc_aligned_at(void* p, size_t newsize, size_t alignment, size_t offset) mi_attr_noexcept mi_attr_alloc_size(2);
mi_decl_nodiscard mi_decl_export void* mi_recalloc_aligned(void* p, size_t newcount, size_t size, size_t alignment) mi_attr_noexcept mi_attr_alloc_size2(2,3) mi_attr_alloc_align(4);
mi_decl_nodiscard mi_decl_export void* mi_recalloc_aligned_at(void* p, size_t newcount, size_t size, size_t alignment, size_t offset) mi_attr_noexcept mi_attr_alloc_size2(2,3);

mi_decl_nodiscard mi_decl_export void* mi_heap_rezalloc(mi_heap_t* heap, void* p, size_t newsize)                mi_attr_noexcept mi_attr_alloc_size(3);
mi_decl_nodiscard mi_decl_export void* mi_heap_recalloc(mi_heap_t* heap, void* p, size_t newcount, size_t size)  mi_attr_noexcept mi_attr_alloc_size2(3,4);

mi_decl_nodiscard mi_decl_export void* mi_heap_rezalloc_aligned(mi_heap_t* heap, void* p, size_t newsize, size_t alignment) mi_attr_noexcept mi_attr_alloc_size(3) mi_attr_alloc_align(4);
mi_decl_nodiscard mi_decl_export void* mi_heap_rezalloc_aligned_at(mi_heap_t* heap, void* p, size_t newsize, size_t alignment, size_t offset) mi_attr_noexcept mi_attr_alloc_size(3);
mi_decl_nodiscard mi_decl_export void* mi_heap_recalloc_aligned(mi_heap_t* heap, void* p, size_t newcount, size_t size, size_t alignment) mi_attr_noexcept mi_attr_alloc_size2(3,4) mi_attr_alloc_align(5);
mi_decl_nodiscard mi_decl_export void* mi_heap_recalloc_aligned_at(mi_heap_t* heap, void* p, size_t newcount, size_t size, size_t alignment, size_t offset) mi_attr_noexcept mi_attr_alloc_size2(3,4);


// ------------------------------------------------------
// Analysis
// ------------------------------------------------------

mi_decl_export bool mi_heap_contains_block(mi_heap_t* heap, const void* p);
mi_decl_export bool mi_heap_check_owned(mi_heap_t* heap, const void* p);
mi_decl_export bool mi_check_owned(const void* p);

// An area of heap space contains blocks of a single size.
typedef struct mi_heap_area_s {
  void*  blocks;      // start of the area containing heap blocks
  size_t reserved;    // bytes reserved for this area (virtual)
  size_t committed;   // current available bytes for this area
  size_t used;        // number of allocated blocks
  size_t block_size;  // size in bytes of each block
  size_t full_block_size; // size in bytes of a full block including padding and metadata.
  int    heap_tag;    // heap tag associated with this area
} mi_heap_area_t;

typedef bool (mi_cdecl mi_block_visit_fun)(const mi_heap_t* heap, const mi_heap_area_t* area, void* block, size_t block_size, void* arg);

mi_decl_export bool mi_heap_visit_blocks(const mi_heap_t* heap, bool visit_blocks, mi_block_visit_fun* visitor, void* arg);

// Experimental
mi_decl_nodiscard mi_decl_export bool mi_is_in_heap_region(const void* p) mi_attr_noexcept;
mi_decl_nodiscard mi_decl_export bool mi_is_redirected(void) mi_attr_noexcept;

mi_decl_export int   mi_reserve_huge_os_pages_interleave(size_t pages, size_t numa_nodes, size_t timeout_msecs) mi_attr_noexcept;
mi_decl_export int   mi_reserve_huge_os_pages_at(size_t pages, int numa_node, size_t timeout_msecs) mi_attr_noexcept;

mi_decl_export int   mi_reserve_os_memory(size_t size, bool commit, bool allow_large) mi_attr_noexcept;
mi_decl_export bool  mi_manage_os_memory(void* start, size_t size, bool is_committed, bool is_large, bool is_zero, int numa_node) mi_attr_noexcept;

mi_decl_export void  mi_debug_show_arenas(void) mi_attr_noexcept;
mi_decl_export void  mi_arenas_print(void) mi_attr_noexcept;

// Experimental: heaps associated with specific memory arena's
typedef int mi_arena_id_t;
mi_decl_export void* mi_arena_area(mi_arena_id_t arena_id, size_t* size);
mi_decl_export int   mi_reserve_huge_os_pages_at_ex(size_t pages, int numa_node, size_t timeout_msecs, bool exclusive, mi_arena_id_t* arena_id) mi_attr_noexcept;
mi_decl_export int   mi_reserve_os_memory_ex(size_t size, bool commit, bool allow_large, bool exclusive, mi_arena_id_t* arena_id) mi_attr_noexcept;
mi_decl_export bool  mi_manage_os_memory_ex(void* start, size_t size, bool is_committed, bool is_large, bool is_zero, int numa_node, bool exclusive, mi_arena_id_t* arena_id) mi_attr_noexcept;

#if MI_MALLOC_VERSION >= 182
// Create a heap that only allocates in the specified arena
mi_decl_nodiscard mi_decl_export mi_heap_t* mi_heap_new_in_arena(mi_arena_id_t arena_id);
#endif


// Experimental: allow sub-processes whose memory areas stay separated (and no reclamation between them)
// Used for example for separate interpreters in one process.
typedef void* mi_subproc_id_t;
mi_decl_export mi_subproc_id_t mi_subproc_main(void);
mi_decl_export mi_subproc_id_t mi_subproc_new(void);
mi_decl_export void mi_subproc_delete(mi_subproc_id_t subproc);
mi_decl_export void mi_subproc_add_current_thread(mi_subproc_id_t subproc); // this should be called right after a thread is created (and no allocation has taken place yet)

// Experimental: visit abandoned heap areas (that are not owned by a specific heap)
mi_decl_export bool mi_abandoned_visit_blocks(mi_subproc_id_t subproc_id, int heap_tag, bool visit_blocks, mi_block_visit_fun* visitor, void* arg);

// Experimental: objects followed by a guard page.
// A sample rate of 0 disables guarded objects, while 1 uses a guard page for every object.
// A seed of 0 uses a random start point. Only objects within the size bound are eligable for guard pages.
mi_decl_export void mi_heap_guarded_set_sample_rate(mi_heap_t* heap, size_t sample_rate, size_t seed);
mi_decl_export void mi_heap_guarded_set_size_bound(mi_heap_t* heap, size_t min, size_t max);

// Experimental: communicate that the thread is part of a threadpool
mi_decl_export void mi_thread_set_in_threadpool(void) mi_attr_noexcept;

// Experimental: create a new heap with a specified heap tag. Set `allow_destroy` to false to allow the thread
// to reclaim abandoned memory (with a compatible heap_tag and arena_id) but in that case `mi_heap_destroy` will
// fall back to `mi_heap_delete`.
mi_decl_nodiscard mi_decl_export mi_heap_t* mi_heap_new_ex(int heap_tag, bool allow_destroy, mi_arena_id_t arena_id);

// Experimental and unsafe: assumes the page of `p` is only accessed by the calling thread
mi_decl_nodiscard mi_decl_export bool mi_unsafe_heap_page_is_under_utilized(mi_heap_t* heap, void* p, size_t perc_threshold) mi_attr_noexcept;

// deprecated
mi_decl_export int mi_reserve_huge_os_pages(size_t pages, double max_secs, size_t* pages_reserved) mi_attr_noexcept;
mi_decl_export void mi_collect_reduce(size_t target_thread_owned) mi_attr_noexcept;



// ------------------------------------------------------
// Convenience
// ------------------------------------------------------

#define mi_malloc_tp(tp)                ((tp*)mi_malloc(sizeof(tp)))
#define mi_zalloc_tp(tp)                ((tp*)mi_zalloc(sizeof(tp)))
#define mi_calloc_tp(tp,n)              ((tp*)mi_calloc(n,sizeof(tp)))
#define mi_mallocn_tp(tp,n)             ((tp*)mi_mallocn(n,sizeof(tp)))
#define mi_reallocn_tp(p,tp,n)          ((tp*)mi_reallocn(p,n,sizeof(tp)))
#define mi_recalloc_tp(p,tp,n)          ((tp*)mi_recalloc(p,n,sizeof(tp)))

#define mi_heap_malloc_tp(hp,tp)        ((tp*)mi_heap_malloc(hp,sizeof(tp)))
#define mi_heap_zalloc_tp(hp,tp)        ((tp*)mi_heap_zalloc(hp,sizeof(tp)))
#define mi_heap_calloc_tp(hp,tp,n)      ((tp*)mi_heap_calloc(hp,n,sizeof(tp)))
#define mi_heap_mallocn_tp(hp,tp,n)     ((tp*)mi_heap_mallocn(hp,n,sizeof(tp)))
#define mi_heap_reallocn_tp(hp,p,tp,n)  ((tp*)mi_heap_reallocn(hp,p,n,sizeof(tp)))
#define mi_heap_recalloc_tp(hp,p,tp,n)  ((tp*)mi_heap_recalloc(hp,p,n,sizeof(tp)))


// ------------------------------------------------------
// Compatibility with v3
// ------------------------------------------------------

typedef mi_heap_t  mi_theap_t;

#define mi_theap_get_default()            mi_heap_get_default()      
#define mi_theap_set_default(hp)          mi_heap_set_default(hp)      
#define mi_theap_collect(hp,force)        mi_heap_collect(hp,force)
#define mi_theap_malloc(hp,sz)            mi_heap_malloc(hp,sz)
#define mi_theap_zalloc(hp,sz)            mi_heap_zalloc(hp,sz)
#define mi_theap_calloc(hp,cnt,sz)        mi_heap_calloc(hp,cnt,sz)
#define mi_theap_malloc_small(hp,sz)      mi_heap_malloc_small(hp,sz)
#define mi_theap_malloc_aligned(hp,sz,a)  mi_heap_malloc_aligned(hp,sz,a)
#define mi_theap_realloc(hp,p,newsz)      mi_heap_realloc(hp,p,newsz)


// ------------------------------------------------------
// Options
// ------------------------------------------------------

typedef enum mi_option_e {
  // stable options
  mi_option_show_errors,                // print error messages
  mi_option_show_stats,                 // print statistics on termination
  mi_option_verbose,                    // print verbose messages
  // advanced options
  mi_option_eager_commit,               // eager commit segments? (after `eager_commit_delay` segments) (=1)
  mi_option_arena_eager_commit,         // eager commit arenas? Use 2 to enable just on overcommit systems (=2)
  mi_option_purge_decommits,            // should a memory purge decommit? (=1). Set to 0 to use memory reset on a purge (instead of decommit)
  mi_option_allow_large_os_pages,       // allow use of large (2 or 4 MiB) OS pages, implies eager commit.
  mi_option_reserve_huge_os_pages,      // reserve N huge OS pages (1GiB pages) at startup
  mi_option_reserve_huge_os_pages_at,   // reserve huge OS pages at a specific NUMA node
  mi_option_reserve_os_memory,          // reserve specified amount of OS memory in an arena at startup (internally, this value is in KiB; use `mi_option_get_size`)
  mi_option_deprecated_segment_cache,
  mi_option_deprecated_page_reset,
  mi_option_abandoned_page_purge,       // immediately purge delayed purges on thread termination
  mi_option_deprecated_segment_reset,
  mi_option_eager_commit_delay,         // the first N segments per thread are not eagerly committed (but per page in the segment on demand)
  mi_option_purge_delay,                // memory purging is delayed by N milli seconds; use 0 for immediate purging or -1 for no purging at all. (=10)
  mi_option_use_numa_nodes,             // 0 = use all available numa nodes, otherwise use at most N nodes.
  mi_option_disallow_os_alloc,          // 1 = do not use OS memory for allocation (but only programmatically reserved arenas)
  mi_option_os_tag,                     // tag used for OS logging (macOS only for now) (=100)
  mi_option_max_errors,                 // issue at most N error messages
  mi_option_max_warnings,               // issue at most N warning messages
  mi_option_max_segment_reclaim,        // max. percentage of the abandoned segments can be reclaimed per try (=10%)
  mi_option_destroy_on_exit,            // if set, release all memory on exit; sometimes used for dynamic unloading but can be unsafe
  mi_option_arena_reserve,              // initial memory size for arena reservation (= 1 GiB on 64-bit) (internally, this value is in KiB; use `mi_option_get_size`)
  mi_option_arena_purge_mult,           // multiplier for `purge_delay` for the purging delay for arenas (=10)
  mi_option_purge_extend_delay,
  mi_option_abandoned_reclaim_on_free,  // allow to reclaim an abandoned segment on a free (=1)
  mi_option_disallow_arena_alloc,       // 1 = do not use arena's for allocation (except if using specific arena id's)
  mi_option_retry_on_oom,               // retry on out-of-memory for N milli seconds (=400), set to 0 to disable retries. (only on windows)
  mi_option_visit_abandoned,            // allow visiting heap blocks from abandoned threads (=0)
  mi_option_guarded_min,                // only used when building with MI_GUARDED: minimal rounded object size for guarded objects (=0)
  mi_option_guarded_max,                // only used when building with MI_GUARDED: maximal rounded object size for guarded objects (=0)
  mi_option_guarded_precise,            // disregard minimal alignment requirement to always place guarded blocks exactly in front of a guard page (=0)
  mi_option_guarded_sample_rate,        // 1 out of N allocations in the min/max range will be guarded (=1000)
  mi_option_guarded_sample_seed,        // can be set to allow for a (more) deterministic re-execution when a guard page is triggered (=0)
  mi_option_target_segments_per_thread, // experimental (=0)
  mi_option_generic_collect,            // collect heaps every N (=10000) generic allocation calls
  mi_option_allow_thp,                  // allow transparent huge pages? (=1) (on Android =0 by default). Set to 0 to disable THP for the process.
  mi_option_prof,
  mi_option_prof_sample_rate,
  mi_option_prof_bt_max,
  mi_option_prof_accum,
  mi_option_prof_seed,
  mi_option_prof_max_bytes,             // budget (in bytes) for profiler-internal arena memory; 0 = unbudgeted (=0)
  mi_option_memory_events,              // enable opt-in allocation-change accounting/callbacks (MIMALLOC_MEMORY_EVENTS) (=0)
  _mi_option_last,
  // legacy option names
  mi_option_large_os_pages = mi_option_allow_large_os_pages,
  mi_option_eager_region_commit = mi_option_arena_eager_commit,
  mi_option_reset_decommits = mi_option_purge_decommits,
  mi_option_reset_delay = mi_option_purge_delay,
  mi_option_abandoned_page_reset = mi_option_abandoned_page_purge,
  mi_option_limit_os_alloc = mi_option_disallow_os_alloc
} mi_option_t;


mi_decl_nodiscard mi_decl_export bool mi_option_is_enabled(mi_option_t option);
mi_decl_export void mi_option_enable(mi_option_t option);
mi_decl_export void mi_option_disable(mi_option_t option);
mi_decl_export void mi_option_set_enabled(mi_option_t option, bool enable);
mi_decl_export void mi_option_set_enabled_default(mi_option_t option, bool enable);

mi_decl_nodiscard mi_decl_export long   mi_option_get(mi_option_t option);
mi_decl_nodiscard mi_decl_export long   mi_option_get_clamp(mi_option_t option, long min, long max);
mi_decl_nodiscard mi_decl_export size_t mi_option_get_size(mi_option_t option);
mi_decl_export void mi_option_set(mi_option_t option, long value);
mi_decl_export void mi_option_set_default(mi_option_t option, long value);


// -------------------------------------------------------------------------------------------------------
// "mi" prefixed implementations of various posix, Unix, Windows, and C++ allocation functions.
// (This can be convenient when providing overrides of these functions as done in `mimalloc-override.h`.)
// note: we use `mi_cfree` as "checked free" and it checks if the pointer is in our heap before free-ing.
// -------------------------------------------------------------------------------------------------------

mi_decl_export void  mi_cfree(void* p) mi_attr_noexcept;
mi_decl_export void* mi__expand(void* p, size_t newsize) mi_attr_noexcept;
mi_decl_nodiscard mi_decl_export size_t mi_malloc_size(const void* p)        mi_attr_noexcept;
mi_decl_nodiscard mi_decl_export size_t mi_malloc_good_size(size_t size)     mi_attr_noexcept;
mi_decl_nodiscard mi_decl_export size_t mi_malloc_usable_size(const void *p) mi_attr_noexcept;

mi_decl_export int mi_posix_memalign(void** p, size_t alignment, size_t size); // mi_attr_noexcept;
mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_memalign(size_t alignment, size_t size) mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size(2) mi_attr_alloc_align(1);
mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_valloc(size_t size)  mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size(1);
mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_pvalloc(size_t size) mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size(1);
mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_aligned_alloc(size_t alignment, size_t size) mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size(2) mi_attr_alloc_align(1);

mi_decl_nodiscard mi_decl_export void* mi_reallocarray(void* p, size_t count, size_t size) mi_attr_noexcept mi_attr_alloc_size2(2,3);
mi_decl_nodiscard mi_decl_export int   mi_reallocarr(void* ptrp, size_t count, size_t size) mi_attr_noexcept;
mi_decl_nodiscard mi_decl_export void* mi_aligned_recalloc(void* p, size_t newcount, size_t size, size_t alignment) mi_attr_noexcept;
mi_decl_nodiscard mi_decl_export void* mi_aligned_offset_recalloc(void* p, size_t newcount, size_t size, size_t alignment, size_t offset) mi_attr_noexcept;

mi_decl_export void mi_free_size(void* p, size_t size)                           mi_attr_noexcept;
mi_decl_export void mi_free_size_aligned(void* p, size_t size, size_t alignment) mi_attr_noexcept;
mi_decl_export void mi_free_aligned(void* p, size_t alignment)                   mi_attr_noexcept;
mi_decl_export int  mi_dupenv_s(char** buf, size_t* size, const char* name)      mi_attr_noexcept;

// wide characters
mi_decl_export int mi_wdupenv_s(wchar_t** buf, size_t* size, const wchar_t* name)       mi_attr_noexcept;
mi_decl_nodiscard mi_decl_export mi_decl_restrict wchar_t* mi_wcsdup(const wchar_t* s)  mi_attr_noexcept mi_attr_malloc;
mi_decl_nodiscard mi_decl_export mi_decl_restrict unsigned char* mi_mbsdup(const unsigned char* s)  mi_attr_noexcept mi_attr_malloc;

// The `mi_new` wrappers implement C++ semantics on out-of-memory instead of directly returning `NULL`.
// (and call `std::get_new_handler` and potentially raise a `std::bad_alloc` exception).
mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_new(size_t size)                   mi_attr_malloc mi_attr_alloc_size(1);
mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_new_aligned(size_t size, size_t alignment) mi_attr_malloc mi_attr_alloc_size(1) mi_attr_alloc_align(2);
mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_new_nothrow(size_t size)           mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size(1);
mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_new_aligned_nothrow(size_t size, size_t alignment) mi_attr_noexcept mi_attr_malloc mi_attr_alloc_size(1) mi_attr_alloc_align(2);
mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_new_n(size_t count, size_t size)   mi_attr_malloc mi_attr_alloc_size2(1, 2);
mi_decl_nodiscard mi_decl_export void* mi_new_realloc(void* p, size_t newsize)                mi_attr_alloc_size(2);
mi_decl_nodiscard mi_decl_export void* mi_new_reallocn(void* p, size_t newcount, size_t size) mi_attr_alloc_size2(2, 3);

mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_heap_alloc_new(mi_heap_t* heap, size_t size)                mi_attr_malloc mi_attr_alloc_size(2);
mi_decl_nodiscard mi_decl_export mi_decl_restrict void* mi_heap_alloc_new_n(mi_heap_t* heap, size_t count, size_t size) mi_attr_malloc mi_attr_alloc_size2(2, 3);

#ifdef __cplusplus
}
#endif

/* ---- begin inlined: include/mimalloc/profile.h ---- */
/* Public, allocation-only sampling profiler API.

   ## Bounds (all profiler-internal memory; see rule 4 below)

   | Vector | Bound | Worst case |
   |---|---|---|
   | Live sample records (~48-64 B, freelist-recycled) | live_heap / sample_interval | 10 GiB heap @ 512 KiB -> ~20k records ~= 1 MiB |
   | Intern table entries (hdr + 8 B/frame, purge-at-zero) | hard cap MI_PROF_STACK_CAP = 65536 | ~20-25 MiB at cap with deep stacks; normally = live unique stacks |
   | Accum mode | pins entries until mi_prof_reset | same cap; documented cost of accum |
   | Arena chunks | high-water retention (never shrinks until mi_prof_stop) | by design; superseded rehash arrays <= 2x final table size |
   | Snapshots / dump buffers / proto scratch | transient _mi_os_alloc, freed per call; failure -> NULL/false | graceful |

   ## Failure policy: the app never pays

   When the raw-OS-layer arena refuses memory in the sampling path, the sample is dropped
   and the application's allocation succeeds normally. Profiling degrades; the app does not.
   Same policy as Go/tcmalloc. Every drop cause (record-alloc failure, stack-intern failure,
   including the MI_PROF_STACK_CAP cap) is counted in mi_prof_stats_t.dropped_samples (v2);
   cap overflows are additionally broken out in stack_table_overflows, so
   dropped_samples >= stack_table_overflows always. */
#pragma once
#ifndef MIMALLOC_PROFILE_H
#define MIMALLOC_PROFILE_H


#ifdef __cplusplus
extern "C" {
#endif

mi_decl_nodiscard mi_decl_export bool mi_prof_start(size_t sample_rate) mi_attr_noexcept;
mi_decl_nodiscard mi_decl_export bool mi_prof_start_seeded(size_t sample_rate, uint64_t seed) mi_attr_noexcept;
mi_decl_export void mi_prof_stop(void) mi_attr_noexcept;

/* Structured start configuration (see mi_prof_start_ex below). `mode` selects how
   non-default (non-zero/non-NULL) struct fields interact with the environment:
     MI_PROF_CONFIG_FALLBACK (=0): the struct is a fallback -- if the corresponding
       env var (or, for accum/max_stack_depth/max_profiler_bytes, its mi_option_*)
       is present, the env/option value wins and the struct field is ignored; the
       struct field only applies where the env var is absent.
     MI_PROF_CONFIG_OVERRIDE (=1): non-zero/non-NULL struct fields always win
       (applied via mi_option_set where applicable), regardless of env; zeroed
       fields fall back to env-then-default, same as FALLBACK.
   In both modes a fully zeroed config (mi_prof_config_t_decl's initial state)
   behaves exactly like mi_prof_start(0). Note that because 0/NULL doubles as
   "field not set", `accum == false`, `dump_format == MI_PROF_FORMAT_TEXT`, and
   `max_profiler_bytes == 0` cannot be distinguished from "unset" in OVERRIDE
   mode -- they always fall back to env-then-default rather than forcing the
   off/default value. This matches their existing defaults, so it only matters
   if you need OVERRIDE to force off a value enabled via the environment. */
#define MI_PROF_FORMAT_TEXT  0
#define MI_PROF_FORMAT_PROTO 1
#define MI_PROF_CONFIG_VERSION 1
typedef enum mi_prof_config_mode_e { MI_PROF_CONFIG_FALLBACK = 0, MI_PROF_CONFIG_OVERRIDE = 1 } mi_prof_config_mode_t;
typedef struct mi_prof_config_s {
  size_t size; int version;
  int mode;                     // mi_prof_config_mode_t
  size_t sample_interval;       // avg bytes between samples; 0 = env/default (512 KiB)
  /* Budget (bytes) for profiler-internal *persistent sampling state* only (sample
     records, the stack intern table, and interned stack entries -- everything
     backed by _mi_prof_arena_alloc). 0 = unbudgeted (cap-bounded only). Dump,
     snapshot, and profile.proto scratch buffers are transient and always use
     _mi_os_alloc directly, never this arena, so they are never counted here. */
  size_t max_profiler_bytes;
  uint64_t seed;                // 0 = nondeterministic
  bool accum;
  size_t max_stack_depth;       // 0 = default (32); compile cap 128
  const char* dump_at_exit;     // NULL = none; copied into the internal buffer
  int dump_format;              // MI_PROF_FORMAT_TEXT / _PROTO, for the exit dump
} mi_prof_config_t;
#define mi_prof_config_t_decl(name)  mi_prof_config_t name = { 0 }; name.size = sizeof(mi_prof_config_t); name.version = MI_PROF_CONFIG_VERSION
mi_decl_nodiscard mi_decl_export bool mi_prof_start_ex(const mi_prof_config_t* config) mi_attr_noexcept;

mi_decl_nodiscard mi_decl_export bool mi_prof_is_enabled(void) mi_attr_noexcept;
/* deprecated: use mi_prof_stats_get */
mi_decl_export void mi_prof_debug_stats(size_t* records, size_t* bytes, size_t* unique_stacks) mi_attr_noexcept;
/* With MIMALLOC_PROF_ACCUM=1 stack entries stay interned until mi_prof_reset;
   this preserves cumulative counters at the cost of profiler arena memory.
   mi_prof_stats_get and the visitor API below report raw sampled counts;
   multiply by sample_rate to estimate the un-sampled allocation stream. */
typedef void (mi_prof_write_fun)(void* arg, const char* buf, size_t len);
mi_decl_nodiscard mi_decl_export bool mi_prof_dump(const char* path) mi_attr_noexcept;
mi_decl_nodiscard mi_decl_export bool mi_prof_dump_writer(mi_prof_write_fun* write, void* arg) mi_attr_noexcept;
/* profile.proto (google/pprof) writer: same sample/period/mapping semantics as
   mi_prof_dump/mi_prof_dump_writer above but encoded as an uncompressed, binary
   pprof Profile message instead of the legacy "heap profile:" text. */
mi_decl_nodiscard mi_decl_export bool mi_prof_dump_proto(const char* path) mi_attr_noexcept;
mi_decl_nodiscard mi_decl_export bool mi_prof_dump_proto_writer(mi_prof_write_fun* write, void* arg) mi_attr_noexcept;
mi_decl_export void mi_prof_reset(void) mi_attr_noexcept;

#define MI_PROF_STAT_VERSION 2
typedef struct mi_prof_stats_s {
  size_t size; int version;
  bool   enabled; bool accum;
  size_t sample_rate;
  size_t live_samples;   size_t live_bytes;
  size_t accum_samples;  size_t accum_bytes;
  size_t unique_stacks;
  size_t arena_committed;
  size_t stack_table_overflows;
  /* v2. Count of ALL dropped samples: record-alloc failure, stack-intern failure
     (capture failure, arena-alloc failure inside intern, or the intern table hitting
     MI_PROF_STACK_CAP -- the latter is a subset also counted separately above in
     stack_table_overflows, so dropped_samples >= stack_table_overflows always).
     mi_prof_stats_get accepts a v1-sized struct (size == offsetof(mi_prof_stats_t,
     dropped_samples), version == 1) for old callers; this field is left untouched
     in that case. */
  size_t dropped_samples;
} mi_prof_stats_t;
#define mi_prof_stats_t_decl(name) mi_prof_stats_t name = { 0 }; name.size = sizeof(mi_prof_stats_t); name.version = MI_PROF_STAT_VERSION
mi_decl_nodiscard mi_decl_export bool mi_prof_stats_get(mi_prof_stats_t* stats) mi_attr_noexcept;

typedef struct mi_prof_sample_info_s {
  const void* const* stack;
  size_t depth;
  size_t live_objects;  size_t live_bytes;
  size_t accum_objects; size_t accum_bytes;
} mi_prof_sample_info_t;
typedef bool (mi_prof_visit_fun)(const mi_prof_sample_info_t* info, void* arg);
mi_decl_nodiscard mi_decl_export bool mi_prof_visit(mi_prof_visit_fun* visitor, void* arg) mi_attr_noexcept;

typedef struct mi_prof_snapshot_s mi_prof_snapshot_t;
mi_decl_nodiscard mi_decl_export mi_prof_snapshot_t* mi_prof_snapshot_new(void) mi_attr_noexcept;
mi_decl_nodiscard mi_decl_export bool mi_prof_snapshot_visit(const mi_prof_snapshot_t* snap, mi_prof_visit_fun* visitor, void* arg) mi_attr_noexcept;
mi_decl_export void mi_prof_snapshot_free(mi_prof_snapshot_t* snap) mi_attr_noexcept;

/* Structured module (mapping) enumeration, e.g. to build pprof Mapping entries
   yourself. No profiler lock is taken: module lists are OS-owned, not part of
   the sampled-allocation table. `info` (and `info->path`) are valid only for
   the duration of the callback. */
typedef struct mi_prof_module_info_s { const char* path; uintptr_t base; size_t size; } mi_prof_module_info_t;
typedef bool (mi_prof_module_visit_fun)(const mi_prof_module_info_t* info, void* arg);
mi_decl_nodiscard mi_decl_export bool mi_prof_modules_visit(mi_prof_module_visit_fun* visitor, void* arg) mi_attr_noexcept;

#ifdef __cplusplus
}
#endif
#endif
/* ---- end inlined: include/mimalloc/profile.h ---- */
/* ---- begin inlined: include/mimalloc/memory-events.h ---- */
/* Public, allocation-change accounting/monitoring API. Independent of MI_PPROF: this
   module (src/memory-events.c) is always compiled in, opt-in only via the
   `MIMALLOC_MEMORY_EVENTS` environment variable or the `mi_memory_tracking_set_enabled`
   API below.

   ## Activation

   - `MIMALLOC_MEMORY_EVENTS=1` is read lazily, exactly once, the first time any
     allocation/free/realloc hook runs -- never during process startup. The result is
     cached; later allocator operations never re-read the environment.
   - `mi_memory_tracking_set_enabled` can also enable/disable tracking at any time,
     including before the first allocation. An explicit API call is always authoritative:
     if it runs before the first allocation, the later lazy environment read is skipped
     entirely; if it runs after, it simply overrides the cached flag.
   - Disabling tracking stops all accounting immediately. Re-enabling does NOT
     reconstruct allocations made while tracking was disabled -- the running totals
     silently omit that interval. Callers that need an exact total must enable tracking
     before their first allocation and leave it enabled for the life of the process.
   - When tracking is disabled (the default), every allocate/free/realloc pays for
     exactly one relaxed flag check on the hot path -- no atomic accounting update, no
     callback-table lock, no callback-table lookup.

   ## Callback contract

   - Callbacks are invoked with no mimalloc allocator locks held, and may themselves
     call `mi_malloc`/`mi_free`/etc. without deadlocking (the callback-table lock is
     acquired only to snapshot the handler pointer, then released before the handler
     runs). Callbacks must still be short and non-blocking.
   - A memory-change hook invoked while another hook's callback is already running on
     the same thread (including as a side effect of that callback allocating/freeing)
     is suppressed: no accounting update and no nested callback invocation. This bounds
     stack depth and avoids double-counting/re-entrant surprises; it also means bytes
     allocated or freed *from inside* a callback are not reflected in the running totals.
   - `arg` pointers in `mi_memory_callbacks_t` are caller-owned: the caller must keep
     them valid for as long as the callback might still be invoked (i.e. until a
     subsequent `mi_memory_set_callbacks` call replaces/clears them, or tracking is
     permanently torn down at process exit).
*/
#pragma once
#ifndef MIMALLOC_MEMORY_EVENTS_H
#define MIMALLOC_MEMORY_EVENTS_H


#ifdef __cplusplus
extern "C" {
#endif

typedef enum mi_memory_change_kind_e {
  MI_MEMORY_ALLOCATE,
  MI_MEMORY_FREE,
  MI_MEMORY_RESIZE,
  MI_MEMORY_CHANGE_COUNT,
} mi_memory_change_kind_t;

typedef struct mi_memory_change_s {
  // Successful allocation, free, or realloc.
  mi_memory_change_kind_t kind;

  // Tracked global live usable bytes after this operation.
  uint64_t total_bytes;

  // Signed change in tracked live usable bytes caused by this operation.
  // Positive for allocation/growth; negative for free/shrink; zero for a
  // same-size-class resize.
  int64_t delta_bytes;

  // Caller-requested size for allocation and resize; zero for free.
  uint64_t request_size;
} mi_memory_change_t;

typedef void (mi_memory_change_fun)(
  const mi_memory_change_t* change,
  void* arg
);

typedef struct mi_memory_callbacks_s {
  mi_memory_change_fun* handlers[MI_MEMORY_CHANGE_COUNT];
  void*                 args[MI_MEMORY_CHANGE_COUNT];
} mi_memory_callbacks_t;

mi_decl_export bool mi_memory_tracking_set_enabled(bool enabled) mi_attr_noexcept;
mi_decl_nodiscard mi_decl_export bool mi_memory_tracking_is_enabled(void) mi_attr_noexcept;

// Installs, replaces, or (with callbacks == NULL) clears the callback table.
// Safe to call at any time; synchronized against concurrent dispatch.
mi_decl_export bool mi_memory_set_callbacks(const mi_memory_callbacks_t* callbacks) mi_attr_noexcept;

// Versioned/sized struct, following this codebase's mi_prof_stats_t idiom, so future
// fields can be added without breaking already-compiled callers (see
// mi_memory_snapshot_t_decl below). All counters are maintained only while tracking is
// enabled: they are not reconstructed for time spent disabled.
#define MI_MEMORY_SNAPSHOT_VERSION 1
typedef struct mi_memory_snapshot_s {
  size_t   size; int version;
  uint64_t live_bytes;         // tracked live usable bytes right now
  uint64_t accum_bytes;        // cumulative usable bytes ever allocated (grows on ALLOCATE and on growing RESIZE)
  uint64_t live_count;         // tracked live allocation count right now
  uint64_t accum_count;        // cumulative count of successful ALLOCATE events
} mi_memory_snapshot_t;
#define mi_memory_snapshot_t_decl(name) mi_memory_snapshot_t name = { 0 }; name.size = sizeof(mi_memory_snapshot_t); name.version = MI_MEMORY_SNAPSHOT_VERSION

mi_decl_nodiscard mi_decl_export bool mi_memory_snapshot(mi_memory_snapshot_t* out) mi_attr_noexcept;

/* ---------------------------------------------------------------------------------------------
   Best-effort live-allocation visitor (diagnostics only; not a consistent global snapshot).
   Built on top of this codebase's existing per-heap block-visitation facility
   (mi_heap_visit_blocks); see memory-events.c for the exact scope this walks. */

// Return false to stop the visit early.
typedef bool (mi_memory_allocation_visit_fun)(
  void*  allocation,   // Address of a live allocation observed during the walk.
  size_t usable_size,  // Its usable mimalloc size at the moment it was observed.
  void*  arg           // Caller-owned visit context.
);

// Best effort, not a consistent global snapshot: another thread may free `allocation`
// immediately after the callback begins. Do not dereference, retain, or free it. The
// visitor must not allocate, free, or reenter mimalloc while the walk is active.
mi_decl_export bool mi_memory_visit_live_allocations(
  mi_memory_allocation_visit_fun* visitor,
  void* arg
) mi_attr_noexcept;

/* ---------------------------------------------------------------------------------------------
   Stable public "unwrapped" instrumentation allocation path: backed directly by the raw
   OS layer (_mi_os_alloc / _mi_os_free), never by the hooked mi_malloc family. Page
   granular. Pointers returned here are only valid for mi_unwrapped_free/realloc -- never
   pass them to mi_free or vice versa. Excluded from normal mimalloc allocation stats and
   from the memory-change accounting above. Intended for low-level instrumentation and
   recursion avoidance (e.g. a memory-change callback that needs scratch storage without
   recursively entering mimalloc). */

mi_decl_export void* mi_unwrapped_malloc(size_t size, size_t alignment) mi_attr_noexcept;
mi_decl_export void  mi_unwrapped_free(void* p) mi_attr_noexcept;
mi_decl_export void* mi_unwrapped_realloc(void* p, size_t new_size, size_t alignment) mi_attr_noexcept;

#ifdef __cplusplus
}
#endif
#endif
/* ---- end inlined: include/mimalloc/memory-events.h ---- */

// ---------------------------------------------------------------------------------------------
// Implement the C++ std::allocator interface for use in STL containers.
// (note: see `mimalloc-new-delete.h` for overriding the new/delete operators globally)
// ---------------------------------------------------------------------------------------------
#ifdef __cplusplus

#include <cstddef>     // std::size_t
#include <cstdint>     // PTRDIFF_MAX
#if (__cplusplus >= 201103L) || (_MSC_VER > 1900)  // C++11
#include <type_traits> // std::true_type
#include <utility>     // std::forward
#endif

template<class T> struct _mi_stl_allocator_common {
  typedef T                 value_type;
  typedef std::size_t       size_type;
  typedef std::ptrdiff_t    difference_type;
  typedef value_type&       reference;
  typedef value_type const& const_reference;
  typedef value_type*       pointer;
  typedef value_type const* const_pointer;

  #if ((__cplusplus >= 201103L) || (_MSC_VER > 1900))  // C++11
  using propagate_on_container_copy_assignment = std::true_type;
  using propagate_on_container_move_assignment = std::true_type;
  using propagate_on_container_swap            = std::true_type;
  template <class U, class ...Args> void construct(U* p, Args&& ...args) { ::new(p) U(std::forward<Args>(args)...); }
  template <class U> void destroy(U* p) mi_attr_noexcept { p->~U(); }
  #else
  void construct(pointer p, value_type const& val) { ::new(p) value_type(val); }
  void destroy(pointer p) { p->~value_type(); }
  #endif

  size_type     max_size() const mi_attr_noexcept { return (PTRDIFF_MAX/sizeof(value_type)); }
  pointer       address(reference x) const        { return &x; }
  const_pointer address(const_reference x) const  { return &x; }
};

template<class T> struct mi_stl_allocator : public _mi_stl_allocator_common<T> {
  using typename _mi_stl_allocator_common<T>::size_type;
  using typename _mi_stl_allocator_common<T>::value_type;
  using typename _mi_stl_allocator_common<T>::pointer;
  template <class U> struct rebind { typedef mi_stl_allocator<U> other; };

  mi_stl_allocator()                                             mi_attr_noexcept = default;
  mi_stl_allocator(const mi_stl_allocator&)                      mi_attr_noexcept = default;
  template<class U> mi_stl_allocator(const mi_stl_allocator<U>&) mi_attr_noexcept { }
  mi_stl_allocator  select_on_container_copy_construction() const { return *this; }
  void              deallocate(T* p, size_type) { mi_free(p); }

  #if (__cplusplus >= 201703L)  // C++17
  mi_decl_nodiscard T* allocate(size_type count) { return static_cast<T*>(mi_new_n(count, sizeof(T))); }
  mi_decl_nodiscard T* allocate(size_type count, const void*) { return allocate(count); }
  #else
  mi_decl_nodiscard pointer allocate(size_type count, const void* = 0) { return static_cast<pointer>(mi_new_n(count, sizeof(value_type))); }
  #endif

  #if ((__cplusplus >= 201103L) || (_MSC_VER > 1900))  // C++11
  using is_always_equal = std::true_type;
  #endif
};

template<class T1,class T2> bool operator==(const mi_stl_allocator<T1>& , const mi_stl_allocator<T2>& ) mi_attr_noexcept { return true; }
template<class T1,class T2> bool operator!=(const mi_stl_allocator<T1>& , const mi_stl_allocator<T2>& ) mi_attr_noexcept { return false; }


#if (__cplusplus >= 201103L) || (_MSC_VER >= 1900)  // C++11
#define MI_HAS_HEAP_STL_ALLOCATOR 1

#include <memory>      // std::shared_ptr

// Common base class for STL allocators in a specific heap
template<class T, bool _mi_destroy> struct _mi_heap_stl_allocator_common : public _mi_stl_allocator_common<T> {
  using typename _mi_stl_allocator_common<T>::size_type;
  using typename _mi_stl_allocator_common<T>::value_type;
  using typename _mi_stl_allocator_common<T>::pointer;

  _mi_heap_stl_allocator_common(mi_heap_t* hp) : heap(hp, [](mi_heap_t*) {}) {}    /* will not delete nor destroy the passed in heap */

  #if (__cplusplus >= 201703L)  // C++17
  mi_decl_nodiscard T* allocate(size_type count) { return static_cast<T*>(mi_heap_alloc_new_n(this->heap.get(), count, sizeof(T))); }
  mi_decl_nodiscard T* allocate(size_type count, const void*) { return allocate(count); }
  #else
  mi_decl_nodiscard pointer allocate(size_type count, const void* = 0) { return static_cast<pointer>(mi_heap_alloc_new_n(this->heap.get(), count, sizeof(value_type))); }
  #endif

  #if ((__cplusplus >= 201103L) || (_MSC_VER > 1900))  // C++11
  using is_always_equal = std::false_type;
  #endif

  void collect(bool force) { mi_heap_collect(this->heap.get(), force); }
  template<class U> bool is_equal(const _mi_heap_stl_allocator_common<U, _mi_destroy>& x) const { return (this->heap == x.heap); }

protected:
  std::shared_ptr<mi_heap_t> heap;
  template<class U, bool D> friend struct _mi_heap_stl_allocator_common;

  _mi_heap_stl_allocator_common() {
    mi_heap_t* hp = mi_heap_new();
    this->heap.reset(hp, (_mi_destroy ? &heap_destroy : &heap_delete));  /* calls heap_delete/destroy when the refcount drops to zero */
  }
  _mi_heap_stl_allocator_common(const _mi_heap_stl_allocator_common& x) mi_attr_noexcept : heap(x.heap) { }
  template<class U> _mi_heap_stl_allocator_common(const _mi_heap_stl_allocator_common<U, _mi_destroy>& x) mi_attr_noexcept : heap(x.heap) { }

private:
  static void heap_delete(mi_heap_t* hp)  { if (hp != NULL) { mi_heap_delete(hp); } }
  static void heap_destroy(mi_heap_t* hp) { if (hp != NULL) { mi_heap_destroy(hp); } }
};

// STL allocator allocation in a specific heap
template<class T> struct mi_heap_stl_allocator : public _mi_heap_stl_allocator_common<T, false> {
  using typename _mi_heap_stl_allocator_common<T, false>::size_type;
  mi_heap_stl_allocator() : _mi_heap_stl_allocator_common<T, false>() { } // creates fresh heap that is deleted when the destructor is called
  mi_heap_stl_allocator(mi_heap_t* hp) : _mi_heap_stl_allocator_common<T, false>(hp) { }  // no delete nor destroy on the passed in heap
  template<class U> mi_heap_stl_allocator(const mi_heap_stl_allocator<U>& x) mi_attr_noexcept : _mi_heap_stl_allocator_common<T, false>(x) { }

  mi_heap_stl_allocator select_on_container_copy_construction() const { return *this; }
  void deallocate(T* p, size_type) { mi_free(p); }
  template<class U> struct rebind { typedef mi_heap_stl_allocator<U> other; };
};

template<class T1, class T2> bool operator==(const mi_heap_stl_allocator<T1>& x, const mi_heap_stl_allocator<T2>& y) mi_attr_noexcept { return (x.is_equal(y)); }
template<class T1, class T2> bool operator!=(const mi_heap_stl_allocator<T1>& x, const mi_heap_stl_allocator<T2>& y) mi_attr_noexcept { return (!x.is_equal(y)); }


// STL allocator allocation in a specific heap, where `free` does nothing and
// the heap is destroyed in one go on destruction -- use with care!
template<class T> struct mi_heap_destroy_stl_allocator : public _mi_heap_stl_allocator_common<T, true> {
  using typename _mi_heap_stl_allocator_common<T, true>::size_type;
  mi_heap_destroy_stl_allocator() : _mi_heap_stl_allocator_common<T, true>() { } // creates fresh heap that is destroyed when the destructor is called
  mi_heap_destroy_stl_allocator(mi_heap_t* hp) : _mi_heap_stl_allocator_common<T, true>(hp) { }  // no delete nor destroy on the passed in heap
  template<class U> mi_heap_destroy_stl_allocator(const mi_heap_destroy_stl_allocator<U>& x) mi_attr_noexcept : _mi_heap_stl_allocator_common<T, true>(x) { }

  mi_heap_destroy_stl_allocator select_on_container_copy_construction() const { return *this; }
  void deallocate(T*, size_type) { /* do nothing as we destroy the heap on destruct. */ }
  template<class U> struct rebind { typedef mi_heap_destroy_stl_allocator<U> other; };
};

template<class T1, class T2> bool operator==(const mi_heap_destroy_stl_allocator<T1>& x, const mi_heap_destroy_stl_allocator<T2>& y) mi_attr_noexcept { return (x.is_equal(y)); }
template<class T1, class T2> bool operator!=(const mi_heap_destroy_stl_allocator<T1>& x, const mi_heap_destroy_stl_allocator<T2>& y) mi_attr_noexcept { return (!x.is_equal(y)); }

#endif // C++11

#endif // __cplusplus

#endif
/* ---- end inlined: include/mimalloc.h ---- */
