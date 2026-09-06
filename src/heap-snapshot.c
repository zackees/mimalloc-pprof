/* ----------------------------------------------------------------------------
Copyright (c) 2026, Microsoft Research, Daan Leijen
This is free software; you can redistribute it and/or modify it under the
terms of the MIT license. A copy of the license can be found in the file
"LICENSE" at the root of this distribution.
-----------------------------------------------------------------------------*/

// Heap snapshot: write a compact binary description of all arenas and pages
// to a file descriptor for offline analysis (see tools/mi-heapview.c).
//
// mimalloc-pprof: imported verbatim from oven-sh/mimalloc @ b20b60d9 (MIT), issue #338
// (Bun parity). Format version 1 is a parity contract -- a snapshot from either allocator
// must open in either viewer -- so this file carries no fork extensions; the reference
// reader in examples/heap-snapshot/mi_snapshot.py is the executable format spec. Two
// local deviations, both outside the format: the two arena.c helpers below are declared
// here because this tree's internal.h does not export them, and the exit message goes
// through `_mi_verbose_message` (this tree has no ungated `_mi_message`).
//
// Allocation discipline (CLAUDE.md rule 4 spirit): the writer allocates nothing -- a
// 16 KiB stack buffer and a 512-byte stack free-map -- so it is safe to run from
// `mi_process_done` and from inside hooked allocation paths. Keep it that way.
//
// The snapshot is a point-in-time, best-effort view intended for answering
// "why is this process using so much memory". It does not stop other threads,
// so counts for pages owned by other threads may be slightly stale. All reads
// of shared state are done through atomics or const fields; no page state is
// mutated except for pages owned by the calling thread when MI_SNAPSHOT_BLOCKS
// is requested (those pages have their free lists collected).

#include "mimalloc.h"
#include "mimalloc/internal.h"
#include "mimalloc/atomic.h"
#include "mimalloc/prim.h"
#include "mimalloc/prim-tls.h"
#include "bitmap.h"

size_t   mi_arenas_get_count(mi_subproc_t* subproc);              // arena.c (not in internal.h here)
uint8_t* mi_arena_slice_start(mi_arena_t* arena, size_t slice_index);  // arena.c (not in internal.h here)

#if defined(_WIN32)
#include <io.h>
#include <fcntl.h>
#include <process.h>
#define mi_snap_write(fd,buf,n)  _write(fd,buf,(unsigned)(n))
#define mi_snap_open(p)          _open(p, _O_WRONLY|_O_CREAT|_O_TRUNC|_O_BINARY, 0644)
#define mi_snap_close(fd)        _close(fd)
#define mi_snap_getpid()         _getpid()
#else
#include <unistd.h>
#include <fcntl.h>
#define mi_snap_write(fd,buf,n)  write(fd,buf,n)
#define mi_snap_open(p)          open(p, O_WRONLY|O_CREAT|O_TRUNC, 0644)
#define mi_snap_close(fd)        close(fd)
#define mi_snap_getpid()         getpid()
#endif

// ---------------------------------------------------------------------------
// Binary format (little-endian). Keep in sync with tools/mi-heapview.c.
// ---------------------------------------------------------------------------

#define MI_SNAPSHOT_MAGIC    0x5348494Du   // 'MIHS'
#define MI_SNAPSHOT_VERSION  1

// section tags
#define MI_SNAP_SEC_ARENA    0x414E5241u   // 'ARNA'
#define MI_SNAP_SEC_HEAP     0x50414548u   // 'HEAP'
#define MI_SNAP_SEC_PAGE     0x45474150u   // 'PAGE'
#define MI_SNAP_SEC_END      0x444E4520u   // ' END'

// ---------------------------------------------------------------------------
// Buffered fd writer
// ---------------------------------------------------------------------------

#define MI_SNAP_BUFSIZE  (16*1024)

typedef struct mi_snap_out_s {
  int     fd;
  bool    err;
  size_t  pos;
  size_t  total;
  uint8_t buf[MI_SNAP_BUFSIZE];
} mi_snap_out_t;

static void mi_snap_flush(mi_snap_out_t* out) {
  if (out->err || out->pos == 0) return;
  size_t off = 0;
  while (off < out->pos) {
    long n = (long)mi_snap_write(out->fd, out->buf + off, out->pos - off);
    if (n <= 0) { out->err = true; return; }
    off += (size_t)n;
  }
  out->pos = 0;
}

static void mi_snap_put(mi_snap_out_t* out, const void* p, size_t n) {
  if (out->err) return;
  out->total += n;
  const uint8_t* src = (const uint8_t*)p;
  while (n > 0) {
    if (out->pos == MI_SNAP_BUFSIZE) mi_snap_flush(out);
    size_t avail = MI_SNAP_BUFSIZE - out->pos;
    size_t take = (n < avail ? n : avail);
    _mi_memcpy(out->buf + out->pos, src, take);
    out->pos += take; src += take; n -= take;
  }
}

static void mi_snap_u8 (mi_snap_out_t* o, uint8_t  v) { mi_snap_put(o, &v, 1); }
static void mi_snap_u32(mi_snap_out_t* o, uint32_t v) { mi_snap_put(o, &v, 4); }
static void mi_snap_u64(mi_snap_out_t* o, uint64_t v) { mi_snap_put(o, &v, 8); }

// ---------------------------------------------------------------------------
// Bitmap emission: raw chunk bytes, little-endian (host order; we only
// support reading snapshots on the same endianness as the writer).
// ---------------------------------------------------------------------------

static void mi_snap_emit_bitmap(mi_snap_out_t* out, const mi_bchunk_t* chunks, size_t chunk_count) {
  mi_snap_u32(out, (uint32_t)chunk_count);
  mi_snap_u32(out, (uint32_t)MI_BCHUNK_SIZE);
  for (size_t i = 0; i < chunk_count; i++) {
    mi_bfield_t fields[MI_BCHUNK_FIELDS];   // the bitmaps are live: read them a word at a time
    for (size_t j = 0; j < MI_BCHUNK_FIELDS; j++) { fields[j] = mi_atomic_load_relaxed(&((mi_bchunk_t*)chunks)[i].bfields[j]); }
    mi_snap_put(out, fields, sizeof(fields));
  }
}

static void mi_snap_emit_mi_bitmap(mi_snap_out_t* out, mi_bitmap_t* bm) {
  if (bm == NULL) { mi_snap_u32(out, 0); mi_snap_u32(out, (uint32_t)MI_BCHUNK_SIZE); return; }
  mi_snap_emit_bitmap(out, bm->chunks, mi_bitmap_chunk_count(bm));
}

static void mi_snap_emit_mi_bbitmap(mi_snap_out_t* out, mi_bbitmap_t* bm) {
  if (bm == NULL) { mi_snap_u32(out, 0); mi_snap_u32(out, (uint32_t)MI_BCHUNK_SIZE); return; }
  mi_snap_emit_bitmap(out, bm->chunks, mi_bbitmap_chunk_count(bm));
}

// ---------------------------------------------------------------------------
// Per-page free-block bitmap (1 = free, 0 = used). Only safe to call on a
// page owned by the current thread (we collect the free lists first).
// ---------------------------------------------------------------------------

static void mi_snap_emit_page_freemap(mi_snap_out_t* out, mi_page_t* page) {
  _mi_page_free_collect_no_unpurge(page, true);   // inspection must not fault a hole back in
  const uint16_t cap = page->capacity;
  const size_t bsize = mi_page_block_size(page);
  uint8_t* const pstart = mi_page_start(page);
  const size_t nbytes = ((size_t)cap + 7) / 8;

  // build on stack in fixed chunks to avoid large stack frames
  uint8_t map[512];
  size_t emitted = 0;

  // First pass: build the full bitmap by walking the free list once and
  // setting bits as we go, flushing in 512-byte windows.
  // Because the free list is unordered, do it in a single buffer if it fits;
  // otherwise fall back to a simple per-block scan (rare: cap > 4096).
  if (nbytes <= sizeof(map)) {
    _mi_memzero(map, nbytes);
    uint64_t magic; size_t shift;
    {
      // fast division setup (replicates mi_get_fast_divisor)
      size_t d = bsize;
      shift = MI_SIZE_BITS - mi_clz(d - 1);
      magic = ((((uint64_t)1 << 32) * (((uint64_t)1 << shift) - d)) / d + 1);
    }
    for (mi_block_t* b = page->free; b != NULL; b = mi_block_next(page, b)) {
      size_t off = (size_t)((uint8_t*)b - pstart);
      size_t hi = ((uint64_t)off * magic) >> 32;
      size_t idx = (hi + off) >> shift;
      if (idx < cap) { map[idx >> 3] |= (uint8_t)(1u << (idx & 7)); }
    }
    // purged blocks are free as well, but held off the free list (see the hole purging
    // section in `page.c`): a block is purged when it overlaps a discarded OS page.
    if (mi_page_has_purged(page)) {
      for (size_t idx = 0; idx < cap; idx++) {
        if (mi_page_block_index_is_purged(page, idx)) { map[idx >> 3] |= (uint8_t)(1u << (idx & 7)); }
      }
    }
    mi_snap_u32(out, (uint32_t)nbytes);
    mi_snap_put(out, map, nbytes);
    return;
  }

  // Slow path for very large pages: emit byte-by-byte using is-free test.
  // (cap > 4096 is uncommon; correctness over speed here.)
  mi_snap_u32(out, (uint32_t)nbytes);
  while (emitted < nbytes) {
    size_t take = nbytes - emitted; if (take > sizeof(map)) take = sizeof(map);
    _mi_memzero(map, take);
    // mark every block index whose address appears in the free list within this window
    for (mi_block_t* b = page->free; b != NULL; b = mi_block_next(page, b)) {
      size_t off = (size_t)((uint8_t*)b - pstart);
      size_t idx = off / bsize;
      size_t byte = idx >> 3;
      if (byte >= emitted && byte < emitted + take) {
        map[byte - emitted] |= (uint8_t)(1u << (idx & 7));
      }
    }
    // and the purged blocks in this window (free, but held off the free list)
    if (mi_page_has_purged(page)) {
      for (size_t idx = (emitted << 3); idx < cap && (idx >> 3) < emitted + take; idx++) {
        if (mi_page_block_index_is_purged(page, idx)) { map[(idx >> 3) - emitted] |= (uint8_t)(1u << (idx & 7)); }
      }
    }
    mi_snap_put(out, map, take);
    emitted += take;
  }
}

// ---------------------------------------------------------------------------
// Page record
// ---------------------------------------------------------------------------

static uint8_t mi_snap_page_kind(const mi_page_t* page) {
  if (mi_page_is_singleton(page)) return (uint8_t)MI_PAGE_SINGLETON;
  const size_t bsize = mi_page_block_size(page);
  if (bsize <= MI_SMALL_MAX_OBJ_SIZE)  return (uint8_t)MI_PAGE_SMALL;
  if (bsize <= MI_MEDIUM_MAX_OBJ_SIZE) return (uint8_t)MI_PAGE_MEDIUM;
  return (uint8_t)MI_PAGE_LARGE;
}

static void mi_snap_emit_page(mi_snap_out_t* out, mi_page_t* page, int32_t arena_idx,
                              unsigned flags, mi_threadid_t self_tid)
{
  const mi_memid_t memid = page->memid;
  const mi_threadid_t tid = mi_page_thread_id(page);
  const bool own_thread = (tid == self_tid && tid > MI_THREADID_ABANDONED_MAPPED);
  const bool want_blocks = (flags & MI_SNAPSHOT_BLOCKS) != 0 && own_thread;

  uint32_t slice_index = 0, slice_count = 0;
  if (memid.memkind == MI_MEM_ARENA) {
    slice_index = memid.mem.arena.slice_index;
    slice_count = memid.mem.arena.slice_count;
  }

  mi_snap_u64(out, (uint64_t)(uintptr_t)mi_page_start(page));
  mi_snap_u64(out, (uint64_t)(uintptr_t)mi_page_slice_start(page));
  mi_snap_u64(out, (uint64_t)mi_page_block_size(page));
  mi_snap_u32(out, (uint32_t)page->reserved);
  mi_snap_u32(out, (uint32_t)page->capacity);
  mi_snap_u32(out, (uint32_t)page->used);
  mi_snap_u64(out, (uint64_t)mi_page_committed(page));
  mi_snap_u64(out, (uint64_t)tid);
  mi_snap_u64(out, (uint64_t)(page->heap != NULL ? page->heap->heap_seq : 0));
  mi_snap_u32(out, (uint32_t)(int32_t)arena_idx);
  mi_snap_u32(out, slice_index);
  mi_snap_u32(out, slice_count);
  mi_snap_u8 (out, (uint8_t)memid.memkind);
  mi_snap_u8 (out, mi_snap_page_kind(page));
  mi_snap_u8 (out, mi_page_is_abandoned(page) ? 1 : 0);
  mi_snap_u8 (out, mi_page_is_full(page) ? 1 : 0);
  mi_snap_u8 (out, want_blocks ? 1 : 0);
  mi_snap_u8 (out, 0); mi_snap_u8(out, 0); mi_snap_u8(out, 0); // pad to 4-byte boundary

  if (want_blocks) {
    mi_snap_emit_page_freemap(out, page);
  }
}

// ---------------------------------------------------------------------------
// Arena walk: emit arena record, then every page that starts in this arena.
// ---------------------------------------------------------------------------

typedef struct mi_snap_ctx_s {
  mi_snap_out_t* out;
  unsigned       flags;
  mi_threadid_t  self_tid;
  size_t         page_count;
} mi_snap_ctx_t;

static void mi_snap_emit_arena_header(mi_snap_out_t* out, mi_arena_t* arena, size_t idx) {
  mi_snap_u32(out, MI_SNAP_SEC_ARENA);
  mi_snap_u32(out, (uint32_t)idx);
  mi_snap_u64(out, (uint64_t)(uintptr_t)arena);             // base address
  mi_snap_u64(out, (uint64_t)mi_size_of_slices(arena->slice_count));
  mi_snap_u32(out, (uint32_t)arena->slice_count);
  mi_snap_u32(out, (uint32_t)arena->info_slices);
  mi_snap_u32(out, (uint32_t)(int32_t)arena->numa_node);
  mi_snap_u8 (out, arena->memid.is_pinned ? 1 : 0);
  mi_snap_u8 (out, arena->is_exclusive ? 1 : 0);
  mi_snap_u8 (out, 0); mi_snap_u8(out, 0);
  mi_snap_emit_mi_bitmap (out, arena->slices_committed);
  mi_snap_emit_mi_bbitmap(out, arena->slices_free);
  mi_snap_emit_mi_bitmap (out, arena->slices_purge);
}

static void mi_snap_walk_arena_pages(mi_snap_ctx_t* ctx, mi_arena_t* arena, int32_t arena_idx) {
  // Walk slices; emit a page when a slice is the first slice of a page.
  // Mirrors the iteration in mi_debug_show_page_bfield.
  size_t slice = arena->info_slices;
  const size_t end = arena->slice_count;
  while (slice < end) {
    void* start = mi_arena_slice_start(arena, slice);
    mi_page_t* page = _mi_safe_ptr_page(start);
    if (page != NULL && start == mi_page_slice_start(page)) {
      mi_snap_emit_page(ctx->out, page, arena_idx, ctx->flags, ctx->self_tid);
      ctx->page_count++;
      size_t pslices = (page->memid.memkind == MI_MEM_ARENA ? page->memid.mem.arena.slice_count : 1);
      slice += (pslices > 0 ? pslices : 1);
    }
    else {
      slice++;
    }
  }
}

// ---------------------------------------------------------------------------
// Heap walk: emit heap records, and any OS-backed (non-arena) abandoned pages.
// ---------------------------------------------------------------------------

static void mi_snap_emit_heap(mi_snap_out_t* out, mi_heap_t* heap) {
  mi_snap_u32(out, MI_SNAP_SEC_HEAP);
  mi_snap_u64(out, (uint64_t)heap->heap_seq);
  mi_snap_u32(out, (uint32_t)(int32_t)heap->numa_node);
  mi_snap_u64(out, (uint64_t)(uintptr_t)heap->exclusive_arena);
}

static void mi_snap_walk_heap_os_pages(mi_snap_ctx_t* ctx, mi_heap_t* heap) {
  // os_abandoned_pages is lock-protected; take the lock to walk it.
  mi_lock(&heap->os_abandoned_pages_lock) {
    for (mi_page_t* p = heap->os_abandoned_pages; p != NULL; p = p->next) {
      mi_snap_emit_page(ctx->out, p, -1, ctx->flags, ctx->self_tid);
      ctx->page_count++;
    }
  }
}

// Walk page queues of theaps owned by the calling thread and emit any
// non-arena pages. (Arena pages are already covered by the arena walk.)
// This catches OS-direct pages created during preloading when no arena
// exists yet (common with dynamic override on macOS).
static void mi_snap_walk_own_theaps(mi_snap_ctx_t* ctx) {
  mi_theap_t* th0 = _mi_theap_default();
  if (th0 == NULL || th0->tld == NULL) return;
  for (mi_theap_t* th = th0->tld->theaps; th != NULL; th = th->tnext) {
    if (th->page_count == 0) continue;
    for (size_t bin = 0; bin < MI_BIN_COUNT; bin++) {
      for (mi_page_t* page = th->pages[bin].first; page != NULL; page = page->next) {
        if (page->memid.memkind != MI_MEM_ARENA) {
          mi_snap_emit_page(ctx->out, page, -1, ctx->flags, ctx->self_tid);
          ctx->page_count++;
        }
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Public entry point
// ---------------------------------------------------------------------------

static int mi_heap_snapshot_inner(int fd, unsigned flags);

// #366: owner-gate site -- the walk folds the thread frees of this thread's OWN pages
// (`mi_snap_emit_page_freemap` -> `_mi_page_free_collect_no_unpurge`), an owner-private write;
// in a gated build a caller between allocator calls is PARKED and could be swept under the
// walk. Other threads' pages are read as they are (see `_mi_page_free_collect_no_unpurge`).
// A thread with no theap (process exit on some platforms) has nothing of its own to protect.
int mi_heap_snapshot(int fd, unsigned flags) mi_attr_noexcept {
  mi_theap_t* self = _mi_theap_default();
  #if MI_OWNER_GATE
  if (!mi_theap_is_initialized(self)) { return mi_heap_snapshot_inner(fd, flags); }
  #endif
  MI_GATE_ENTER(self);
  const int rc = mi_heap_snapshot_inner(fd, flags);
  MI_GATE_LEAVE(self->tld);
  #if !MI_OWNER_GATE
  MI_UNUSED(self);
  #endif
  return rc;
}

static int mi_heap_snapshot_inner(int fd, unsigned flags) {
  if (fd < 0) return -1;
  // Use the main subproc (and walk siblings) rather than `_mi_subproc()`: at
  // process-exit time on some platforms TLS may already point at an empty theap,
  // making `_mi_subproc()` return a subproc with no arenas.
  mi_subproc_t* subproc = _mi_subproc_main();
  if (subproc == NULL) return -1;

  mi_snap_out_t out;
  _mi_memzero(&out, sizeof(out));
  out.fd = fd;

  mi_snap_ctx_t ctx;
  ctx.out = &out;
  ctx.flags = flags;
  ctx.self_tid = _mi_prim_thread_id();
  ctx.page_count = 0;

  // --- header ---
  mi_snap_u32(&out, MI_SNAPSHOT_MAGIC);
  mi_snap_u32(&out, MI_SNAPSHOT_VERSION);
  mi_snap_u32(&out, (uint32_t)MI_INTPTR_SIZE);
  mi_snap_u32(&out, (uint32_t)MI_ARENA_SLICE_SIZE);
  mi_snap_u32(&out, (uint32_t)flags);
  mi_snap_u32(&out, 0);  // reserved
  mi_snap_u64(&out, (uint64_t)_mi_clock_now());
  mi_snap_u64(&out, (uint64_t)ctx.self_tid);

  // --- arenas + their pages (across all subprocs) ---
  size_t total_arenas = 0;
  for (mi_subproc_t* sp = subproc; sp != NULL; sp = sp->next) {
    total_arenas += mi_arenas_get_count(sp);
  }
  mi_snap_u32(&out, (uint32_t)total_arenas);
  for (mi_subproc_t* sp = subproc; sp != NULL; sp = sp->next) {
    const size_t arena_count = mi_arenas_get_count(sp);
    for (size_t i = 0; i < arena_count; i++) {
      mi_arena_t* arena = mi_atomic_load_ptr_acquire(mi_arena_t, &sp->arenas[i]);
      if (arena == NULL) continue;
      mi_snap_emit_arena_header(&out, arena, i);
      mi_snap_u32(&out, MI_SNAP_SEC_PAGE);
      mi_snap_walk_arena_pages(&ctx, arena, (int32_t)i);
      mi_snap_u64(&out, 0);  // sentinel page_start == 0 ends this arena's page list
    }
  }

  // --- own-thread non-arena pages (covers preload-time OS-direct pages) ---
  mi_snap_u32(&out, MI_SNAP_SEC_PAGE);
  mi_snap_walk_own_theaps(&ctx);
  mi_snap_u64(&out, 0);  // sentinel

  // --- heaps + os-backed abandoned pages ---
  for (mi_subproc_t* sp = subproc; sp != NULL; sp = sp->next) {
    mi_lock(&sp->heaps_lock) {
      for (mi_heap_t* h = sp->heaps; h != NULL; h = h->next) {
        mi_snap_emit_heap(&out, h);
        mi_snap_u32(&out, MI_SNAP_SEC_PAGE);
        mi_snap_walk_heap_os_pages(&ctx, h);
        mi_snap_u64(&out, 0);  // sentinel
      }
    }
  }

  // --- footer ---
  mi_snap_u32(&out, MI_SNAP_SEC_END);
  mi_snap_u64(&out, (uint64_t)ctx.page_count);
  mi_snap_flush(&out);

  return (out.err ? -1 : 0);
}

int mi_heap_snapshot_to_file(const char* path, unsigned flags) mi_attr_noexcept {
  if (path == NULL) return -1;
  int fd = mi_snap_open(path);
  if (fd < 0) return -1;
  int rc = mi_heap_snapshot(fd, flags);
  mi_snap_close(fd);
  return rc;
}

// Called from mi_process_done when mi_option_snapshot_on_exit > 0.
// Path resolution: MIMALLOC_SNAPSHOT_PATH if set, else "mimalloc-snapshot.<pid>.bin".
void _mi_heap_snapshot_on_exit(void) {
  const long opt = mi_option_get(mi_option_snapshot_on_exit);
  if (opt <= 0) return;
  unsigned flags = (opt >= 2 ? MI_SNAPSHOT_BLOCKS : 0);

  char path[512];
  // mimalloc-pprof: this tree's `_mi_getenv` returns an errno-style code (0 = found), not
  // Bun's bool -- keep the semantics (env path wins, else "mimalloc-snapshot.<pid>.bin").
  if (_mi_getenv("MIMALLOC_SNAPSHOT_PATH", path, sizeof(path)) != 0) {
    _mi_snprintf(path, sizeof(path), "mimalloc-snapshot.%d.bin", (int)mi_snap_getpid());
  }
  if (mi_heap_snapshot_to_file(path, flags) == 0) {
    _mi_verbose_message("heap snapshot written to %s\n", path);
  }
  else {
    _mi_warning_message("failed to write heap snapshot to %s\n", path);
  }
}
