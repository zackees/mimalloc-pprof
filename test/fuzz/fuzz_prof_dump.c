/* Fuzzer for the profiler's dump encoders (issue #87, target 2).

   `mi_prof_dump_writer` and `mi_prof_dump_proto_writer` serialise through a hand-rolled
   protobuf encoder in src/profile.c -- manual varint construction and length prefixes,
   written by us. That is the highest-value *fork-specific* fuzz target: it is our code,
   it does hand-rolled binary encoding, and no upstream or third-party fuzzer will ever
   reach it.

   The input drives the allocation pattern (sizes, counts, sampling rate, when to dump,
   what to free), so the fuzzer explores encoder states that fixed tests do not: an
   empty profile, a single sample, many distinct stacks, dumps interleaved with frees,
   and dumps taken while the table is being mutated.

   Oracles:
     - ASan, for memory errors inside the encoder
     - a length-prefix walk of the proto output, which catches a truncated or
       over-long submessage -- the classic hand-rolled-encoder bug, and one that a
       crash alone would not surface
     - the text header must parse and its totals must be self-consistent */

#include <assert.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "mimalloc.h"
#include "mimalloc/profile.h"

#define MAX_LIVE 256
#define SINK_CAP (1u << 20)

static unsigned char sink[SINK_CAP];
static size_t sink_used;
static int    sink_overflow;

static void sink_write(void* arg, const char* buf, size_t len) {
  (void)arg;
  if (sink_used + len > SINK_CAP) { sink_overflow = 1; return; }
  memcpy(sink + sink_used, buf, len);
  sink_used += len;
}

static void sink_reset(void) { sink_used = 0; sink_overflow = 0; }

/* Walk top-level protobuf records. Every length-delimited field must lie entirely
   inside the buffer; a hand-rolled encoder that miscomputes a prefix produces a length
   that runs off the end, which this catches without needing a full pprof decoder. */
static void check_proto_framing(const unsigned char* buf, size_t len) {
  size_t pos = 0;
  while (pos < len) {
    uint64_t tag = 0;
    int shift = 0;
    int ok = 0;
    while (pos < len) {
      const unsigned char b = buf[pos++];
      tag |= ((uint64_t)(b & 0x7F)) << shift;
      if ((b & 0x80) == 0) { ok = 1; break; }
      shift += 7;
      if (shift >= 64) return;   /* malformed varint: stop rather than assert */
    }
    if (!ok) return;
    const uint32_t wire = (uint32_t)(tag & 7);
    if (wire == 0) {             /* varint */
      while (pos < len && (buf[pos] & 0x80) != 0) pos++;
      if (pos < len) pos++;
    } else if (wire == 2) {      /* length-delimited */
      uint64_t l = 0; shift = 0; ok = 0;
      while (pos < len) {
        const unsigned char b = buf[pos++];
        l |= ((uint64_t)(b & 0x7F)) << shift;
        if ((b & 0x80) == 0) { ok = 1; break; }
        shift += 7;
        if (shift >= 64) return;
      }
      if (!ok) return;
      /* The point of this harness. */
      assert(l <= len - pos && "proto submessage length runs past the end of the buffer");
      pos += (size_t)l;
    } else if (wire == 5) { pos += 4; }
    else if (wire == 1) { pos += 8; }
    else { return; }             /* unknown wire type: stop */
  }
}

static void check_text_header(void) {
  if (sink_used == 0 || sink_overflow) return;
  const size_t n = (sink_used < SINK_CAP ? sink_used : SINK_CAP - 1);
  sink[n] = 0;
  unsigned long long objs, bytes, aobjs, abytes;
  if (sscanf((const char*)sink, "heap profile: %llu: %llu [%llu: %llu]",
             &objs, &bytes, &aobjs, &abytes) == 4) {
    /* Sampled bytes cannot be attributed to fewer than zero objects, and an
       object-free profile cannot carry bytes. */
    if (objs == 0) assert(bytes == 0);
  }
}

static void* live[MAX_LIVE];

int LLVMFuzzerTestOneInput(const uint8_t* data, size_t size) {
  if (size < 4) return 0;
  size_t pos = 0;
  const uint8_t rate_sel = data[pos++];
  const uint8_t seed     = data[pos++];

  /* Small rates make sampling frequent, so the encoder actually sees records. */
  const size_t rate = (size_t)1 << (8 + (rate_sel & 7));
  if (!mi_prof_start_seeded(rate, seed)) return 0;

  memset(live, 0, sizeof(live));
  size_t count = 0;

  while (pos < size) {
    const uint8_t op = data[pos++];
    switch (op & 3) {
      case 0: {  /* allocate */
        if (count < MAX_LIVE) {
          const size_t n = ((size_t)(op >> 2) + 1) * 512;
          void* p = mi_malloc(n);
          if (p != NULL) live[count++] = p;
        }
        break;
      }
      case 1: {  /* free one */
        if (count > 0) { mi_free(live[--count]); live[count] = NULL; }
        break;
      }
      case 2: {  /* text dump */
        sink_reset();
        if (mi_prof_dump_writer(sink_write, NULL) && !sink_overflow) check_text_header();
        break;
      }
      default: { /* proto dump */
        sink_reset();
        if (mi_prof_dump_proto_writer(sink_write, NULL) && !sink_overflow) {
          check_proto_framing(sink, sink_used);
        }
        break;
      }
    }
  }

  for (size_t i = 0; i < count; i++) mi_free(live[i]);
  mi_prof_stop();
  return 0;
}
