/* ----------------------------------------------------------------------------
mi-heapview: query a mimalloc heap snapshot written by mi_heap_snapshot().

Standalone tool with no dependency on the mimalloc library; only depends on
the binary snapshot format (see src/heap-snapshot.c).

Usage:
  mi-heapview <snapshot> summary
  mi-heapview <snapshot> sizes   [--bytes] [--top N]
  mi-heapview <snapshot> frag    [--top N] [--min-waste BYTES]
  mi-heapview <snapshot> arenas
  mi-heapview <snapshot> pages   [--top N] [--size BYTES] [--sort waste|addr|used]
  mi-heapview <snapshot> blocks  --addr 0xADDR
  mi-heapview <snapshot> json

All sizes printed in bytes unless --human. Output is column-aligned plain text
for easy grepping.
-----------------------------------------------------------------------------*/

#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <inttypes.h>

#if defined(__APPLE__)
#include <mach-o/loader.h>
#elif defined(__linux__) || defined(__FreeBSD__)
#include <elf.h>
#endif

// keep in sync with src/heap-snapshot.c
#define MI_SNAPSHOT_MAGIC    0x5348494Du
#define MI_SNAPSHOT_VERSION  1
#define MI_SNAP_SEC_ARENA    0x414E5241u
#define MI_SNAP_SEC_HEAP     0x50414548u
#define MI_SNAP_SEC_PAGE     0x45474150u
#define MI_SNAP_SEC_END      0x444E4520u

#define MI_SNAPSHOT_BLOCKS   0x01

// ---------------------------------------------------------------------------
// Reader
// ---------------------------------------------------------------------------

typedef struct hv_reader_s {
  const uint8_t* data;
  size_t   size;
  size_t   pos;
  bool     err;
} hv_reader_t;

static bool hv_need(hv_reader_t* r, size_t n) {
  if (r->err || r->pos + n > r->size) { r->err = true; return false; }
  return true;
}
static uint8_t  hv_u8 (hv_reader_t* r) { if (!hv_need(r,1)) return 0; uint8_t  v; memcpy(&v, r->data+r->pos, 1); r->pos += 1; return v; }
static uint32_t hv_u32(hv_reader_t* r) { if (!hv_need(r,4)) return 0; uint32_t v; memcpy(&v, r->data+r->pos, 4); r->pos += 4; return v; }
static uint64_t hv_u64(hv_reader_t* r) { if (!hv_need(r,8)) return 0; uint64_t v; memcpy(&v, r->data+r->pos, 8); r->pos += 8; return v; }
static const uint8_t* hv_blob(hv_reader_t* r, size_t n) {
  if (!hv_need(r,n)) return NULL;
  const uint8_t* p = r->data + r->pos; r->pos += n; return p;
}

// ---------------------------------------------------------------------------
// Model
// ---------------------------------------------------------------------------

typedef struct hv_bitmap_s {
  uint32_t chunk_count;
  uint32_t chunk_size;
  const uint8_t* data;   // chunk_count * chunk_size bytes
} hv_bitmap_t;

typedef struct hv_arena_s {
  uint32_t  idx;
  uint64_t  base;
  uint64_t  size;
  uint32_t  slice_count;
  uint32_t  info_slices;
  int32_t   numa_node;
  uint8_t   is_pinned, is_exclusive;
  hv_bitmap_t committed, sfree, purge;
} hv_arena_t;

typedef struct hv_page_s {
  uint64_t page_start;
  uint64_t slice_start;
  uint64_t block_size;
  uint32_t reserved, capacity, used;
  uint64_t committed;
  uint64_t thread_id;
  uint64_t heap_seq;
  int32_t  arena_idx;
  uint32_t slice_index, slice_count;
  uint8_t  memkind, page_kind;
  uint8_t  is_abandoned, is_full, has_blocks;
  uint32_t freemap_bytes;
  const uint8_t* freemap;
} hv_page_t;

typedef struct hv_heap_s {
  uint64_t seq;
  int32_t  numa_node;
  uint64_t exclusive_arena;
} hv_heap_t;

typedef struct hv_snapshot_s {
  uint32_t version, ptr_size, slice_size, flags;
  uint64_t clock_ms, writer_tid;
  hv_arena_t* arenas; size_t arena_count;
  hv_heap_t*  heaps;  size_t heap_count, heap_cap;
  hv_page_t*  pages;  size_t page_count, page_cap;
} hv_snapshot_t;

// ---------------------------------------------------------------------------
// Parse
// ---------------------------------------------------------------------------

static void hv_read_bitmap(hv_reader_t* r, hv_bitmap_t* bm) {
  bm->chunk_count = hv_u32(r);
  bm->chunk_size  = hv_u32(r);
  bm->data = hv_blob(r, (size_t)bm->chunk_count * bm->chunk_size);
}

static bool hv_read_page(hv_reader_t* r, hv_page_t* p) {
  p->page_start = hv_u64(r);
  if (p->page_start == 0) return false;  // sentinel
  p->slice_start = hv_u64(r);
  p->block_size  = hv_u64(r);
  p->reserved    = hv_u32(r);
  p->capacity    = hv_u32(r);
  p->used        = hv_u32(r);
  p->committed   = hv_u64(r);
  p->thread_id   = hv_u64(r);
  p->heap_seq    = hv_u64(r);
  p->arena_idx   = (int32_t)hv_u32(r);
  p->slice_index = hv_u32(r);
  p->slice_count = hv_u32(r);
  p->memkind     = hv_u8(r);
  p->page_kind   = hv_u8(r);
  p->is_abandoned= hv_u8(r);
  p->is_full     = hv_u8(r);
  p->has_blocks  = hv_u8(r);
  hv_u8(r); hv_u8(r); hv_u8(r);  // padding
  p->freemap_bytes = 0; p->freemap = NULL;
  if (p->has_blocks) {
    p->freemap_bytes = hv_u32(r);
    p->freemap = hv_blob(r, p->freemap_bytes);
  }
  return !r->err;
}

static void hv_append_page(hv_snapshot_t* s, hv_page_t* p) {
  if (s->page_count == s->page_cap) {
    s->page_cap = (s->page_cap == 0 ? 1024 : s->page_cap * 2);
    s->pages = (hv_page_t*)realloc(s->pages, s->page_cap * sizeof(hv_page_t));
  }
  s->pages[s->page_count++] = *p;
}

static void hv_append_heap(hv_snapshot_t* s, hv_heap_t* h) {
  if (s->heap_count == s->heap_cap) {
    s->heap_cap = (s->heap_cap == 0 ? 8 : s->heap_cap * 2);
    s->heaps = (hv_heap_t*)realloc(s->heaps, s->heap_cap * sizeof(hv_heap_t));
  }
  s->heaps[s->heap_count++] = *h;
}

static bool hv_parse(hv_reader_t* r, hv_snapshot_t* s) {
  memset(s, 0, sizeof(*s));
  if (hv_u32(r) != MI_SNAPSHOT_MAGIC) { fprintf(stderr, "not a mimalloc snapshot\n"); return false; }
  s->version    = hv_u32(r);
  if (s->version != MI_SNAPSHOT_VERSION) { fprintf(stderr, "unsupported version %u\n", s->version); return false; }
  s->ptr_size   = hv_u32(r);
  s->slice_size = hv_u32(r);
  s->flags      = hv_u32(r);
  hv_u32(r); // reserved
  s->clock_ms   = hv_u64(r);
  s->writer_tid = hv_u64(r);

  uint32_t na = hv_u32(r);
  s->arenas = (hv_arena_t*)calloc(na, sizeof(hv_arena_t));
  s->arena_count = 0;
  for (uint32_t i = 0; i < na && !r->err; i++) {
    uint32_t tag = hv_u32(r);
    if (tag != MI_SNAP_SEC_ARENA) { fprintf(stderr, "expected ARNA tag, got 0x%x at +%zu\n", tag, r->pos-4); return false; }
    hv_arena_t* a = &s->arenas[s->arena_count++];
    a->idx         = hv_u32(r);
    a->base        = hv_u64(r);
    a->size        = hv_u64(r);
    a->slice_count = hv_u32(r);
    a->info_slices = hv_u32(r);
    a->numa_node   = (int32_t)hv_u32(r);
    a->is_pinned   = hv_u8(r);
    a->is_exclusive= hv_u8(r);
    hv_u8(r); hv_u8(r);
    hv_read_bitmap(r, &a->committed);
    hv_read_bitmap(r, &a->sfree);
    hv_read_bitmap(r, &a->purge);
    if (hv_u32(r) != MI_SNAP_SEC_PAGE) { fprintf(stderr, "expected PAGE tag\n"); return false; }
    hv_page_t p;
    while (hv_read_page(r, &p)) hv_append_page(s, &p);
  }

  // own-thread non-arena pages, then heaps + their os pages, then END
  while (!r->err) {
    uint32_t tag = hv_u32(r);
    if (tag == MI_SNAP_SEC_END) { hv_u64(r); break; }
    if (tag == MI_SNAP_SEC_PAGE) { hv_page_t p; while (hv_read_page(r, &p)) hv_append_page(s, &p); continue; }
    if (tag != MI_SNAP_SEC_HEAP) { fprintf(stderr, "expected HEAP/PAGE/END, got 0x%x at +%zu\n", tag, r->pos-4); return false; }
    hv_heap_t h;
    h.seq            = hv_u64(r);
    h.numa_node      = (int32_t)hv_u32(r);
    h.exclusive_arena= hv_u64(r);
    hv_append_heap(s, &h);
    if (hv_u32(r) != MI_SNAP_SEC_PAGE) { fprintf(stderr, "expected PAGE tag after heap\n"); return false; }
    hv_page_t p;
    while (hv_read_page(r, &p)) hv_append_page(s, &p);
  }
  return !r->err;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

static uint64_t hv_popcount_bytes(const uint8_t* p, size_t n) {
  uint64_t c = 0;
  for (size_t i = 0; i < n; i++) { uint8_t b = p[i]; while (b) { c += (b & 1); b >>= 1; } }
  return c;
}

static uint64_t hv_bitmap_popcount(const hv_bitmap_t* bm, uint32_t max_bits) {
  if (bm->data == NULL) return 0;
  size_t total_bytes = (size_t)bm->chunk_count * bm->chunk_size;
  size_t need_bytes  = ((size_t)max_bits + 7) / 8;
  if (need_bytes > total_bytes) need_bytes = total_bytes;
  return hv_popcount_bytes(bm->data, need_bytes);
}

static const char* hv_fmt_bytes(uint64_t n, char* buf, size_t bufsz, bool human) {
  if (!human) { snprintf(buf, bufsz, "%llu", (unsigned long long)n); return buf; }
  const char* u = "B"; double d = (double)n;
  if (d >= 1024) { d /= 1024; u = "KiB"; }
  if (d >= 1024) { d /= 1024; u = "MiB"; }
  if (d >= 1024) { d /= 1024; u = "GiB"; }
  snprintf(buf, bufsz, "%.1f %s", d, u);
  return buf;
}

static const char* hv_kind_str(uint8_t k) {
  switch (k) { case 0: return "small"; case 1: return "medium"; case 2: return "large"; case 3: return "single"; default: return "?"; }
}

// ---------------------------------------------------------------------------
// Sidecar metadata: <snapshot>.meta.json with {"threads":{"0xTID":"name",...}}
// Minimal parser; only extracts the "threads" map.
// ---------------------------------------------------------------------------

typedef struct hv_tidname_s { uint64_t tid; char name[48]; } hv_tidname_t;
typedef struct hv_meta_s { hv_tidname_t* threads; size_t nthreads; } hv_meta_t;

static const char* hv_tid_name(const hv_meta_t* m, uint64_t tid) {
  if (m == NULL) return NULL;
  for (size_t i = 0; i < m->nthreads; i++) if (m->threads[i].tid == tid) return m->threads[i].name;
  return NULL;
}

static const char* hv_tid_label(const hv_meta_t* m, uint64_t tid, char* buf, size_t bufsz) {
  const char* nm = hv_tid_name(m, tid);
  if (nm) snprintf(buf, bufsz, "0x%llx (%s)", (unsigned long long)tid, nm);
  else if (tid <= 4) snprintf(buf, bufsz, "0x%llx (abandoned)", (unsigned long long)tid);
  else snprintf(buf, bufsz, "0x%llx", (unsigned long long)tid);
  return buf;
}

static void hv_load_meta(const char* snapshot_path, hv_meta_t* m) {
  m->threads = NULL; m->nthreads = 0;
  char mpath[1024]; snprintf(mpath, sizeof(mpath), "%s.meta.json", snapshot_path);
  FILE* f = fopen(mpath, "rb"); if (!f) return;
  fseek(f, 0, SEEK_END); long sz = ftell(f); fseek(f, 0, SEEK_SET);
  char* buf = (char*)malloc((size_t)sz + 1); fread(buf, 1, (size_t)sz, f); buf[sz] = 0; fclose(f);
  char* p = strstr(buf, "\"threads\"");
  if (p) {
    p = strchr(p, '{');
    size_t cap = 0;
    while (p && *p && *p != '}') {
      char* k = strchr(p, '"'); if (!k) break; k++;
      char* ke = strchr(k, '"'); if (!ke) break;
      char* v = strchr(ke+1, '"'); if (!v) break; v++;
      char* ve = strchr(v, '"'); if (!ve) break;
      if (m->nthreads == cap) { cap = cap ? cap*2 : 16; m->threads = (hv_tidname_t*)realloc(m->threads, cap*sizeof(hv_tidname_t)); }
      hv_tidname_t* t = &m->threads[m->nthreads++];
      t->tid = strtoull(k, NULL, 0);
      size_t nlen = (size_t)(ve - v); if (nlen >= sizeof(t->name)) nlen = sizeof(t->name)-1;
      memcpy(t->name, v, nlen); t->name[nlen] = 0;
      p = ve + 1;
    }
  }
  free(buf);
}

// ---------------------------------------------------------------------------
// Coredump VA -> file-offset mapping (ELF and Mach-O)
// ---------------------------------------------------------------------------

typedef struct hv_seg_s { uint64_t vaddr, vsize, foff, fsize; } hv_seg_t;
typedef struct hv_core_s { FILE* f; hv_seg_t* segs; size_t nsegs; } hv_core_t;

static bool hv_core_open(hv_core_t* c, const char* path) {
  memset(c, 0, sizeof(*c));
  c->f = fopen(path, "rb"); if (!c->f) { perror(path); return false; }
  uint8_t hdr[64]; if (fread(hdr,1,64,c->f) < 16) return false;
  size_t cap = 0;
  #define ADDSEG(va,vs,fo,fs) do { \
    if (c->nsegs==cap) { cap=cap?cap*2:64; c->segs=(hv_seg_t*)realloc(c->segs,cap*sizeof(hv_seg_t)); } \
    c->segs[c->nsegs++] = (hv_seg_t){va,vs,fo,fs}; } while(0)

  if (hdr[0]==0x7f && hdr[1]=='E' && hdr[2]=='L' && hdr[3]=='F' && hdr[4]==2) {
    #if defined(__linux__) || defined(__FreeBSD__)
    Elf64_Ehdr eh; memcpy(&eh, hdr, sizeof(eh) < 64 ? sizeof(eh) : 64);
    fseek(c->f, 0, SEEK_SET); fread(&eh, sizeof(eh), 1, c->f);
    fseek(c->f, (long)eh.e_phoff, SEEK_SET);
    for (int i = 0; i < eh.e_phnum; i++) {
      Elf64_Phdr ph; fread(&ph, sizeof(ph), 1, c->f);
      if (ph.p_type == PT_LOAD && ph.p_filesz > 0) ADDSEG(ph.p_vaddr, ph.p_memsz, ph.p_offset, ph.p_filesz);
    }
    #else
    fprintf(stderr, "ELF core: rebuild mi-heapview on Linux to read this core\n");
    #endif
  }
  else if (*(uint32_t*)hdr == 0xfeedfacf) {
    #if defined(__APPLE__)
    struct mach_header_64 mh; fseek(c->f,0,SEEK_SET); fread(&mh,sizeof(mh),1,c->f);
    long off = sizeof(mh);
    for (uint32_t i = 0; i < mh.ncmds; i++) {
      struct load_command lc; fseek(c->f,off,SEEK_SET); fread(&lc,sizeof(lc),1,c->f);
      if (lc.cmd == LC_SEGMENT_64) {
        struct segment_command_64 sc; fseek(c->f,off,SEEK_SET); fread(&sc,sizeof(sc),1,c->f);
        if (sc.filesize > 0) ADDSEG(sc.vmaddr, sc.vmsize, sc.fileoff, sc.filesize);
      }
      off += lc.cmdsize;
    }
    #else
    fprintf(stderr, "Mach-O core: rebuild mi-heapview on macOS to read this core\n");
    #endif
  }
  else {
    fprintf(stderr, "unrecognized core file format\n");
    return false;
  }
  #undef ADDSEG
  return c->nsegs > 0;
}

static bool hv_core_read(hv_core_t* c, uint64_t va, void* dst, size_t n) {
  for (size_t i = 0; i < c->nsegs; i++) {
    hv_seg_t* s = &c->segs[i];
    if (va >= s->vaddr && va + n <= s->vaddr + s->fsize) {
      fseek(c->f, (long)(s->foff + (va - s->vaddr)), SEEK_SET);
      return fread(dst, 1, n, c->f) == n;
    }
  }
  return false;
}

// pick up to `want` live block addresses of the given block_size across all pages
static size_t hv_sample_blocks(hv_snapshot_t* s, uint64_t block_size, uint64_t tid_filter,
                               uint64_t* out, size_t want)
{
  size_t got = 0;
  uint64_t seed = 0x2545F4914F6CDD1Dull ^ block_size;
  for (size_t i = 0; i < s->page_count && got < want; i++) {
    hv_page_t* p = &s->pages[i];
    if (p->block_size != block_size) continue;
    if (tid_filter && p->thread_id != tid_filter) continue;
    if (p->used == 0) continue;
    // pick one block from this page
    uint32_t idx;
    if (p->has_blocks) {
      // find the (seed % used)'th used block
      uint32_t target = (uint32_t)(seed % p->used), seen = 0; idx = 0;
      for (uint32_t j = 0; j < p->capacity; j++) {
        if (!((p->freemap[j>>3] >> (j&7)) & 1)) { if (seen++ == target) { idx = j; break; } }
      }
    } else if (p->used == p->capacity) {
      idx = (uint32_t)(seed % p->capacity);
    } else {
      continue;  // can't know which slots are live
    }
    out[got++] = p->page_start + (uint64_t)idx * p->block_size;
    seed = seed * 6364136223846793005ull + 1;
  }
  return got;
}

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------

static void cmd_summary(hv_snapshot_t* s, bool human) {
  uint64_t arena_reserved = 0, arena_committed = 0, arena_free_committed = 0, arena_purgeable = 0;
  for (size_t i = 0; i < s->arena_count; i++) {
    hv_arena_t* a = &s->arenas[i];
    arena_reserved  += a->size;
    uint64_t cbits = hv_bitmap_popcount(&a->committed, a->slice_count);
    uint64_t fbits = hv_bitmap_popcount(&a->sfree,     a->slice_count);
    uint64_t pbits = hv_bitmap_popcount(&a->purge,     a->slice_count);
    arena_committed      += cbits * s->slice_size;
    arena_purgeable      += pbits * s->slice_size;
    // free & committed: a slice can be free but still committed (waiting for purge or reuse)
    // we don't have the AND here cheaply; approximate via fbits for now
    arena_free_committed += fbits * s->slice_size;  // note: this is "free", not "free AND committed"
    (void)arena_free_committed;
  }
  uint64_t page_committed = 0, page_used_bytes = 0, page_reserved_bytes = 0;
  size_t   pages_abandoned = 0, pages_full = 0;
  for (size_t i = 0; i < s->page_count; i++) {
    hv_page_t* p = &s->pages[i];
    page_committed     += p->committed;
    page_used_bytes    += (uint64_t)p->used * p->block_size;
    page_reserved_bytes+= (uint64_t)p->reserved * p->block_size;
    if (p->is_abandoned) pages_abandoned++;
    if (p->is_full) pages_full++;
  }
  uint64_t internal_frag = page_committed > page_used_bytes ? page_committed - page_used_bytes : 0;

  char b1[32],b2[32],b3[32],b4[32];
  printf("snapshot   version=%u  ptr=%u  slice=%u  flags=0x%x\n", s->version, s->ptr_size, s->slice_size, s->flags);
  printf("arenas     %zu   reserved=%s   committed=%s   purgeable=%s\n",
    s->arena_count, hv_fmt_bytes(arena_reserved,b1,32,human), hv_fmt_bytes(arena_committed,b2,32,human), hv_fmt_bytes(arena_purgeable,b3,32,human));
  printf("heaps      %zu\n", s->heap_count);
  printf("pages      %zu   (abandoned=%zu full=%zu)\n", s->page_count, pages_abandoned, pages_full);
  printf("page-mem   committed=%s   used=%s   reserved=%s\n",
    hv_fmt_bytes(page_committed,b1,32,human), hv_fmt_bytes(page_used_bytes,b2,32,human), hv_fmt_bytes(page_reserved_bytes,b3,32,human));
  printf("waste      internal-frag=%s   (%.1f%% of committed pages)\n",
    hv_fmt_bytes(internal_frag,b4,32,human), page_committed ? 100.0*(double)internal_frag/(double)page_committed : 0.0);
}

typedef struct hv_sizebin_s { uint64_t block_size; uint64_t tid; uint64_t pages; uint64_t used; uint64_t cap; uint64_t committed; } hv_sizebin_t;

static int hv_sizebin_cmp_committed(const void* a, const void* b) {
  uint64_t ca = ((const hv_sizebin_t*)a)->committed, cb = ((const hv_sizebin_t*)b)->committed;
  return (ca < cb) ? 1 : (ca > cb) ? -1 : 0;
}

typedef enum { HV_BY_NONE, HV_BY_TID, HV_BY_HEAP } hv_groupby_t;

static hv_sizebin_t* hv_aggregate(hv_snapshot_t* s, hv_groupby_t by, size_t* out_n) {
  hv_sizebin_t* bins = NULL; size_t nbins = 0, capb = 0;
  for (size_t i = 0; i < s->page_count; i++) {
    hv_page_t* p = &s->pages[i];
    uint64_t key_tid = (by == HV_BY_TID ? p->thread_id : (by == HV_BY_HEAP ? p->heap_seq : 0));
    size_t j = 0; for (; j < nbins; j++) if (bins[j].block_size == p->block_size && bins[j].tid == key_tid) break;
    if (j == nbins) {
      if (nbins == capb) { capb = capb ? capb*2 : 128; bins = (hv_sizebin_t*)realloc(bins, capb*sizeof(*bins)); }
      bins[nbins++] = (hv_sizebin_t){ p->block_size, key_tid, 0,0,0,0 };
    }
    bins[j].pages++; bins[j].used += p->used; bins[j].cap += p->capacity; bins[j].committed += p->committed;
  }
  *out_n = nbins;
  return bins;
}

static void cmd_sizes(hv_snapshot_t* s, hv_meta_t* meta, int top, hv_groupby_t by, bool human) {
  size_t nbins; hv_sizebin_t* bins = hv_aggregate(s, by, &nbins);
  qsort(bins, nbins, sizeof(*bins), hv_sizebin_cmp_committed);
  if (top <= 0 || (size_t)top > nbins) top = (int)nbins;
  const char* gcol = (by==HV_BY_TID?"thread":by==HV_BY_HEAP?"heap":"");
  if (by != HV_BY_NONE) printf("%-12s %-28s %8s %12s %14s %14s %7s\n", "block_size", gcol, "pages", "used_blks", "used_bytes", "committed", "frag%");
  else                  printf("%-12s %8s %12s %12s %14s %14s %7s\n", "block_size", "pages", "used_blks", "cap_blks", "used_bytes", "committed", "frag%");
  for (int i = 0; i < top; i++) {
    hv_sizebin_t* b = &bins[i];
    uint64_t used_b = b->used * b->block_size;
    double frag = b->committed ? 100.0 * (double)(b->committed - used_b) / (double)b->committed : 0.0;
    char c1[32], c2[32], tb[64];
    if (by == HV_BY_TID) {
      printf("%-12llu %-28s %8llu %12llu %14s %14s %6.1f%%\n",
        (unsigned long long)b->block_size, hv_tid_label(meta, b->tid, tb, sizeof(tb)),
        (unsigned long long)b->pages, (unsigned long long)b->used,
        hv_fmt_bytes(used_b,c1,32,human), hv_fmt_bytes(b->committed,c2,32,human), frag);
    } else if (by == HV_BY_HEAP) {
      printf("%-12llu heap#%-22llu %8llu %12llu %14s %14s %6.1f%%\n",
        (unsigned long long)b->block_size, (unsigned long long)b->tid,
        (unsigned long long)b->pages, (unsigned long long)b->used,
        hv_fmt_bytes(used_b,c1,32,human), hv_fmt_bytes(b->committed,c2,32,human), frag);
    } else {
      printf("%-12llu %8llu %12llu %12llu %14s %14s %6.1f%%\n",
        (unsigned long long)b->block_size, (unsigned long long)b->pages,
        (unsigned long long)b->used, (unsigned long long)b->cap,
        hv_fmt_bytes(used_b,c1,32,human), hv_fmt_bytes(b->committed,c2,32,human), frag);
    }
  }
  free(bins);
}

// diff: aggregate both by (size, tid), subtract, sort by |Δcommitted|
typedef struct hv_diffrow_s { uint64_t block_size, tid; int64_t d_used, d_committed, d_pages; } hv_diffrow_t;

static int hv_diffrow_cmp(const void* a, const void* b) {
  int64_t ca = ((const hv_diffrow_t*)a)->d_committed; if (ca<0) ca=-ca;
  int64_t cb = ((const hv_diffrow_t*)b)->d_committed; if (cb<0) cb=-cb;
  return (ca < cb) ? 1 : (ca > cb) ? -1 : 0;
}

static hv_diffrow_t* hv_diffrow_find(hv_diffrow_t** rows, size_t* nr, size_t* capr, uint64_t bs, uint64_t td) {
  for (size_t k = 0; k < *nr; k++) if ((*rows)[k].block_size == bs && (*rows)[k].tid == td) return &(*rows)[k];
  if (*nr == *capr) { *capr = *capr ? *capr*2 : 128; *rows = (hv_diffrow_t*)realloc(*rows, *capr * sizeof(**rows)); }
  (*rows)[*nr] = (hv_diffrow_t){ bs, td, 0, 0, 0 };
  return &(*rows)[(*nr)++];
}

static void cmd_diff(hv_snapshot_t* a, hv_snapshot_t* b, hv_meta_t* meta, int top, hv_groupby_t by, bool human) {
  size_t na, nb;
  hv_sizebin_t* ba = hv_aggregate(a, by, &na);
  hv_sizebin_t* bb = hv_aggregate(b, by, &nb);
  hv_diffrow_t* rows = NULL; size_t nr = 0, capr = 0;
  for (size_t i=0;i<nb;i++){ hv_diffrow_t* r=hv_diffrow_find(&rows,&nr,&capr,bb[i].block_size,bb[i].tid); r->d_used += (int64_t)bb[i].used; r->d_committed += (int64_t)bb[i].committed; r->d_pages += (int64_t)bb[i].pages; }
  for (size_t i=0;i<na;i++){ hv_diffrow_t* r=hv_diffrow_find(&rows,&nr,&capr,ba[i].block_size,ba[i].tid); r->d_used -= (int64_t)ba[i].used; r->d_committed -= (int64_t)ba[i].committed; r->d_pages -= (int64_t)ba[i].pages; }
  qsort(rows, nr, sizeof(*rows), hv_diffrow_cmp);
  if (top <= 0 || (size_t)top > nr) top = (int)nr;
  const char* gcol = (by==HV_BY_TID?"thread":by==HV_BY_HEAP?"heap":"");
  printf("%-12s %-28s %12s %12s %14s\n", "block_size", gcol, "Δblocks", "Δpages", "Δcommitted");
  for (int i=0;i<top;i++){
    hv_diffrow_t* r=&rows[i]; if (r->d_used==0 && r->d_committed==0 && r->d_pages==0) continue;
    char cb[32], tb[64];
    int64_t dc = r->d_committed; const char* sign = dc<0?"-":"+"; if (dc<0) dc=-dc;
    if (by==HV_BY_TID) hv_tid_label(meta,r->tid,tb,sizeof(tb));
    else if (by==HV_BY_HEAP) snprintf(tb,sizeof(tb),"heap#%llu",(unsigned long long)r->tid);
    else tb[0]=0;
    printf("%-12llu %-28s %+12lld %+12lld %s%13s\n",
      (unsigned long long)r->block_size, tb,
      (long long)r->d_used, (long long)r->d_pages, sign, hv_fmt_bytes((uint64_t)dc,cb,32,human));
  }
  free(rows); free(ba); free(bb);
}

static void hv_hexdump(const uint8_t* p, size_t n, uint64_t base) {
  for (size_t off = 0; off < n; off += 16) {
    printf("  %016llx: ", (unsigned long long)(base+off));
    for (size_t j=0;j<16;j++){ if(off+j<n) printf("%02x ", p[off+j]); else printf("   "); }
    printf(" |");
    for (size_t j=0;j<16 && off+j<n;j++){ uint8_t c=p[off+j]; printf("%c", (c>=32&&c<127)?c:'.'); }
    printf("|\n");
  }
}

static void cmd_peek(hv_snapshot_t* s, const char* core_path, uint64_t block_size, uint64_t tid_filter, int sample, size_t bytes) {
  hv_core_t core;
  if (!hv_core_open(&core, core_path)) { fprintf(stderr, "failed to open core %s\n", core_path); return; }
  uint64_t addrs[256]; if (sample <= 0) sample = 8; if (sample > 256) sample = 256;
  size_t n = hv_sample_blocks(s, block_size, tid_filter, addrs, (size_t)sample);
  if (n == 0) { fprintf(stderr, "no sampleable live blocks of size %llu (need full pages or freemap)\n", (unsigned long long)block_size); return; }
  if (bytes == 0 || bytes > 256) bytes = 64;
  uint8_t buf[256];
  uint64_t first_word[256]; size_t fw_n = 0;
  for (size_t i = 0; i < n; i++) {
    if (!hv_core_read(&core, addrs[i], buf, bytes)) {
      printf("0x%016llx: <not in core>\n", (unsigned long long)addrs[i]);
      continue;
    }
    printf("0x%016llx:\n", (unsigned long long)addrs[i]);
    hv_hexdump(buf, bytes, addrs[i]);
    if (bytes >= 8) { memcpy(&first_word[fw_n++], buf, 8); }
  }
  // vtable heuristic: if >=50% of samples share the same first 8 bytes, call it out
  if (fw_n >= 2) {
    for (size_t i = 0; i < fw_n; i++) {
      size_t c = 0; for (size_t j = 0; j < fw_n; j++) if (first_word[j]==first_word[i]) c++;
      if (c*2 >= fw_n) {
        printf("\nfirst-word constant in %zu/%zu samples: 0x%016llx  (likely vtable or type tag; try: nm <binary> | grep %llx)\n",
          c, fw_n, (unsigned long long)first_word[i], (unsigned long long)first_word[i]);
        break;
      }
    }
  }
  fclose(core.f);
}

static int hv_page_cmp_waste(const void* a, const void* b) {
  const hv_page_t* pa = *(const hv_page_t* const*)a; const hv_page_t* pb = *(const hv_page_t* const*)b;
  uint64_t wa = pa->committed - (uint64_t)pa->used * pa->block_size;
  uint64_t wb = pb->committed - (uint64_t)pb->used * pb->block_size;
  return (wa < wb) ? 1 : (wa > wb) ? -1 : 0;
}
static int hv_page_cmp_addr(const void* a, const void* b) {
  const hv_page_t* pa = *(const hv_page_t* const*)a; const hv_page_t* pb = *(const hv_page_t* const*)b;
  return (pa->page_start < pb->page_start) ? -1 : (pa->page_start > pb->page_start) ? 1 : 0;
}
static int hv_page_cmp_used(const void* a, const void* b) {
  const hv_page_t* pa = *(const hv_page_t* const*)a; const hv_page_t* pb = *(const hv_page_t* const*)b;
  uint64_t ua = (uint64_t)pa->used * pa->block_size, ub = (uint64_t)pb->used * pb->block_size;
  return (ua < ub) ? 1 : (ua > ub) ? -1 : 0;
}

static void cmd_pages(hv_snapshot_t* s, int top, uint64_t filter_size, const char* sort, uint64_t min_waste, bool human) {
  hv_page_t** idx = (hv_page_t**)malloc(s->page_count * sizeof(*idx));
  size_t n = 0;
  for (size_t i = 0; i < s->page_count; i++) {
    hv_page_t* p = &s->pages[i];
    if (filter_size && p->block_size != filter_size) continue;
    uint64_t waste = p->committed - (uint64_t)p->used * p->block_size;
    if (waste < min_waste) continue;
    idx[n++] = p;
  }
  int (*cmp)(const void*, const void*) = hv_page_cmp_waste;
  if (sort && strcmp(sort,"addr")==0) cmp = hv_page_cmp_addr;
  else if (sort && strcmp(sort,"used")==0) cmp = hv_page_cmp_used;
  qsort(idx, n, sizeof(*idx), cmp);
  if (top <= 0 || (size_t)top > n) top = (int)n;
  printf("%-18s %10s %6s %6s %6s %12s %12s %5s %4s %3s %s\n",
    "page_start", "block_sz", "used", "cap", "rsvd", "committed", "waste", "kind", "arn", "ab", "tid");
  for (int i = 0; i < top; i++) {
    hv_page_t* p = idx[i];
    uint64_t used_b = (uint64_t)p->used * p->block_size;
    uint64_t waste = p->committed > used_b ? p->committed - used_b : 0;
    char c1[32], c2[32];
    printf("0x%016llx %10llu %6u %6u %6u %12s %12s %5s %4d %3s 0x%llx\n",
      (unsigned long long)p->page_start, (unsigned long long)p->block_size,
      p->used, p->capacity, p->reserved,
      hv_fmt_bytes(p->committed,c1,32,human), hv_fmt_bytes(waste,c2,32,human),
      hv_kind_str(p->page_kind), p->arena_idx, p->is_abandoned?"y":"n",
      (unsigned long long)p->thread_id);
  }
  free(idx);
}

static void cmd_arenas(hv_snapshot_t* s, bool human) {
  printf("%-4s %-18s %12s %8s %12s %12s %12s %5s\n",
    "idx", "base", "size", "slices", "committed", "free", "purgeable", "numa");
  for (size_t i = 0; i < s->arena_count; i++) {
    hv_arena_t* a = &s->arenas[i];
    uint64_t c = hv_bitmap_popcount(&a->committed, a->slice_count) * (uint64_t)s->slice_size;
    uint64_t f = hv_bitmap_popcount(&a->sfree,     a->slice_count) * (uint64_t)s->slice_size;
    uint64_t p = hv_bitmap_popcount(&a->purge,     a->slice_count) * (uint64_t)s->slice_size;
    char b1[32],b2[32],b3[32],b4[32];
    printf("%-4u 0x%016llx %12s %8u %12s %12s %12s %5d%s%s\n",
      a->idx, (unsigned long long)a->base, hv_fmt_bytes(a->size,b1,32,human), a->slice_count,
      hv_fmt_bytes(c,b2,32,human), hv_fmt_bytes(f,b3,32,human), hv_fmt_bytes(p,b4,32,human),
      a->numa_node, a->is_pinned?" pinned":"", a->is_exclusive?" excl":"");
  }
}

static void cmd_blocks(hv_snapshot_t* s, uint64_t addr) {
  // find the page containing addr
  for (size_t i = 0; i < s->page_count; i++) {
    hv_page_t* p = &s->pages[i];
    uint64_t end = p->page_start + (uint64_t)p->reserved * p->block_size;
    if (addr >= p->page_start && addr < end) {
      printf("page 0x%llx  block_size=%llu  used=%u/%u  kind=%s  arena=%d\n",
        (unsigned long long)p->page_start, (unsigned long long)p->block_size,
        p->used, p->capacity, hv_kind_str(p->page_kind), p->arena_idx);
      if (!p->has_blocks) {
        printf("(no per-block freemap recorded for this page; "
               "snapshot was taken without MI_SNAPSHOT_BLOCKS, or page was owned by another thread)\n");
        return;
      }
      printf("%-18s %-6s\n", "addr", "state");
      for (uint32_t j = 0; j < p->capacity; j++) {
        bool is_free = (p->freemap[j>>3] >> (j&7)) & 1;
        uint64_t baddr = p->page_start + (uint64_t)j * p->block_size;
        if (!is_free) {
          printf("0x%016llx used%s\n", (unsigned long long)baddr,
                 (addr >= baddr && addr < baddr + p->block_size) ? "  <-- query" : "");
        }
      }
      return;
    }
  }
  fprintf(stderr, "no page contains 0x%llx\n", (unsigned long long)addr);
}

static void cmd_json(hv_snapshot_t* s) {
  printf("{\"version\":%u,\"ptr_size\":%u,\"slice_size\":%u,\"flags\":%u,\n", s->version, s->ptr_size, s->slice_size, s->flags);
  printf(" \"arenas\":[");
  for (size_t i = 0; i < s->arena_count; i++) {
    hv_arena_t* a = &s->arenas[i];
    printf("%s{\"idx\":%u,\"base\":%llu,\"size\":%llu,\"slices\":%u,\"committed_slices\":%llu,\"free_slices\":%llu,\"purge_slices\":%llu}",
      i?",":"", a->idx, (unsigned long long)a->base, (unsigned long long)a->size, a->slice_count,
      (unsigned long long)hv_bitmap_popcount(&a->committed,a->slice_count),
      (unsigned long long)hv_bitmap_popcount(&a->sfree,a->slice_count),
      (unsigned long long)hv_bitmap_popcount(&a->purge,a->slice_count));
  }
  printf("],\n \"pages\":[");
  for (size_t i = 0; i < s->page_count; i++) {
    hv_page_t* p = &s->pages[i];
    printf("%s{\"addr\":%llu,\"bs\":%llu,\"used\":%u,\"cap\":%u,\"rsvd\":%u,\"cmt\":%llu,\"tid\":%llu,\"heap\":%llu,\"arn\":%d,\"kind\":%u,\"ab\":%u}",
      i?",":"", (unsigned long long)p->page_start, (unsigned long long)p->block_size,
      p->used, p->capacity, p->reserved, (unsigned long long)p->committed,
      (unsigned long long)p->thread_id, (unsigned long long)p->heap_seq, p->arena_idx, p->page_kind, p->is_abandoned);
  }
  printf("]}\n");
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

static void usage(void) {
  fprintf(stderr,
    "usage: mi-heapview <snapshot> <cmd> [options]\n"
    "  summary                              overall stats\n"
    "  sizes   [--top N] [--by-tid|--by-heap]  histogram by block size, sorted by committed bytes\n"
    "  frag    [--top N] [--min-waste B]    pages sorted by wasted (committed - used) bytes\n"
    "  arenas                               per-arena commit/free/purge\n"
    "  pages   [--top N] [--size B] [--sort waste|addr|used] [--min-waste B]\n"
    "  blocks  --addr 0xADDR                list live block addresses in the page containing ADDR\n"
    "  diff    <snapshot2> [--top N] [--by-tid|--by-heap]   per-(size,group) deltas: snapshot2 - snapshot\n"
    "  peek    --core FILE --size B [--tid T] [--sample N] [--bytes N]\n"
    "                                       hexdump N sample blocks from a coredump; flags likely vtable ptr\n"
    "  json                                 full dump as JSON\n"
    "common: --human                        print sizes as KiB/MiB/GiB\n"
    "If <snapshot>.meta.json exists with {\"threads\":{\"0xTID\":\"name\"}}, thread names are shown.\n");
}

static uint8_t* hv_slurp(const char* path, size_t* out_sz) {
  FILE* f = fopen(path, "rb"); if (!f) { perror(path); return NULL; }
  fseek(f, 0, SEEK_END); long sz = ftell(f); fseek(f, 0, SEEK_SET);
  uint8_t* data = (uint8_t*)malloc((size_t)sz);
  if (fread(data, 1, (size_t)sz, f) != (size_t)sz) { perror("fread"); free(data); fclose(f); return NULL; }
  fclose(f); *out_sz = (size_t)sz; return data;
}

int main(int argc, char** argv) {
  if (argc < 3) { usage(); return 1; }
  const char* path = argv[1];
  const char* cmd  = argv[2];
  int argi = 3;

  size_t sz; uint8_t* data = hv_slurp(path, &sz); if (!data) return 1;
  hv_reader_t r = { data, sz, 0, false };
  hv_snapshot_t s;
  if (!hv_parse(&r, &s)) { fprintf(stderr, "%s: parse failed at offset %zu\n", path, r.pos); return 1; }

  hv_meta_t meta; hv_load_meta(path, &meta);

  // diff takes a second positional arg
  hv_snapshot_t s2; uint8_t* data2 = NULL; bool have_s2 = false;
  if (strcmp(cmd, "diff") == 0) {
    if (argi >= argc) { fprintf(stderr, "diff requires a second snapshot\n"); return 1; }
    size_t sz2; data2 = hv_slurp(argv[argi++], &sz2); if (!data2) return 1;
    hv_reader_t r2 = { data2, sz2, 0, false };
    if (!hv_parse(&r2, &s2)) { fprintf(stderr, "%s: parse failed at offset %zu\n", argv[argi-1], r2.pos); return 1; }
    have_s2 = true;
  }

  // options
  int top = 0, sample = 0; uint64_t filter_size = 0, min_waste = 0, addr = 0, tid = 0; size_t bytes = 0;
  const char* sort = "waste"; const char* core = NULL; bool human = false; hv_groupby_t by = HV_BY_NONE;
  for (int i = argi; i < argc; i++) {
    if (strcmp(argv[i],"--top")==0 && i+1<argc) top = atoi(argv[++i]);
    else if (strcmp(argv[i],"--size")==0 && i+1<argc) filter_size = strtoull(argv[++i],NULL,0);
    else if (strcmp(argv[i],"--sort")==0 && i+1<argc) sort = argv[++i];
    else if (strcmp(argv[i],"--min-waste")==0 && i+1<argc) min_waste = strtoull(argv[++i],NULL,0);
    else if (strcmp(argv[i],"--addr")==0 && i+1<argc) addr = strtoull(argv[++i],NULL,0);
    else if (strcmp(argv[i],"--tid")==0 && i+1<argc) tid = strtoull(argv[++i],NULL,0);
    else if (strcmp(argv[i],"--core")==0 && i+1<argc) core = argv[++i];
    else if (strcmp(argv[i],"--sample")==0 && i+1<argc) sample = atoi(argv[++i]);
    else if (strcmp(argv[i],"--bytes")==0 && i+1<argc) bytes = (size_t)strtoull(argv[++i],NULL,0);
    else if (strcmp(argv[i],"--human")==0) human = true;
    else if (strcmp(argv[i],"--by-tid")==0) by = HV_BY_TID;
    else if (strcmp(argv[i],"--by-heap")==0) by = HV_BY_HEAP;
    else { fprintf(stderr, "unknown option: %s\n", argv[i]); usage(); return 1; }
  }

  if      (strcmp(cmd,"summary")==0) cmd_summary(&s, human);
  else if (strcmp(cmd,"sizes")==0)   cmd_sizes(&s, &meta, top, by, human);
  else if (strcmp(cmd,"frag")==0)    cmd_pages(&s, top?top:30, 0, "waste", min_waste, human);
  else if (strcmp(cmd,"arenas")==0)  cmd_arenas(&s, human);
  else if (strcmp(cmd,"pages")==0)   cmd_pages(&s, top, filter_size, sort, min_waste, human);
  else if (strcmp(cmd,"blocks")==0)  { if (!addr) { fprintf(stderr,"--addr required\n"); return 1; } cmd_blocks(&s, addr); }
  else if (strcmp(cmd,"diff")==0)    { if (!have_s2) return 1; cmd_diff(&s, &s2, &meta, top, by, human); }
  else if (strcmp(cmd,"peek")==0)    { if (!core||!filter_size) { fprintf(stderr,"peek needs --core and --size\n"); return 1; } cmd_peek(&s, core, filter_size, tid, sample, bytes); }
  else if (strcmp(cmd,"json")==0)    cmd_json(&s);
  else { usage(); return 1; }

  free(data); if (data2) free(data2);
  return 0;
}
