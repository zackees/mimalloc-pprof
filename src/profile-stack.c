/* Platform stack capture for sampled allocations.  This routine allocates
   nothing: the caller supplies its profiler-arena backed PC buffer. */
#include "mimalloc.h"
#include "mimalloc/internal.h"
#include <string.h>

#if MI_PPROF

#define MI_PROF_BT_MAX_LIMIT 128
#define MI_PROF_STACK_CAP 65536
struct mi_prof_stack_s { uint64_t hash; uint32_t depth; uint32_t refcount; uint32_t pin; size_t slot; size_t curobjs, curbytes, accumobjs, accumbytes; void* pcs[]; };
static mi_prof_stack_t** stack_table;
static size_t stack_capacity;
static _Atomic(size_t) stack_count;
static _Atomic(size_t) stack_overflows;

static uint64_t stack_hash(void* const* pcs, size_t depth) {
  uint64_t hash = UINT64_C(1469598103934665603);
  for (size_t i = 0; i < depth; i++) { uintptr_t pc=(uintptr_t)pcs[i]; for (size_t b=0;b<sizeof(pc);b++) { hash ^= (uint8_t)(pc >> (b*8)); hash *= UINT64_C(1099511628211); } }
  return hash;
}

static bool stack_equal(const mi_prof_stack_t* stack, uint64_t hash, void* const* pcs, size_t depth) {
  if (stack == NULL || stack->hash != hash || stack->depth != depth) return false;
  for (size_t i=0; i<depth; i++) if (stack->pcs[i] != pcs[i]) return false;
  return true;
}

static bool stack_init(void) {
  if (stack_table != NULL) return true;
  stack_capacity = 1024;
  stack_table = (mi_prof_stack_t**)_mi_prof_arena_alloc(stack_capacity * sizeof(*stack_table));
  if (stack_table == NULL) return false;
  memset(stack_table, 0, stack_capacity * sizeof(*stack_table));
  return true;
}

static void stack_place(mi_prof_stack_t* stack) {
  size_t index = (size_t)stack->hash & (stack_capacity - 1);
  while (stack_table[index] != NULL) index = (index + 1) & (stack_capacity - 1);
  stack_table[index] = stack; stack->slot = index;
}

static bool stack_grow(void) {
  if (mi_atomic_load_relaxed(&stack_count) * 4 < stack_capacity * 3) return true;
  size_t old_capacity=stack_capacity; mi_prof_stack_t** old=stack_table;
  stack_capacity *= 2;
  stack_table = (mi_prof_stack_t**)_mi_prof_arena_alloc(stack_capacity*sizeof(*stack_table));
  if (stack_table == NULL) { stack_table=old; stack_capacity=old_capacity; return false; }
  memset(stack_table, 0, stack_capacity*sizeof(*stack_table));
  for (size_t i=0;i<old_capacity;i++) if (old[i] != NULL) stack_place(old[i]);
  return true;
}

#ifdef _WIN32
#include <windows.h>
size_t _mi_prof_stack_capture(void** pcs, size_t capacity) {
  if (capacity > 128) capacity = 128;
  return (size_t)RtlCaptureStackBackTrace(2, (ULONG)capacity, pcs, NULL);
}
#elif defined(__APPLE__)
/* Issue #35: on Apple Silicon (arm64e), system-library functions sign their return
   address (paciasp) before spilling it to the stack; a raw frame-pointer walk (as
   used on other Unix platforms below) reads that signed value verbatim, and the
   pointer-authentication bits it carries push the "PC" outside every real module
   range -- pprof can't symbolize it, and stack interning treats PAC-signature
   variants of the same logical stack as distinct entries, inflating the intern
   table. `backtrace()` (<execinfo.h>, part of libSystem -- no new dependency,
   satisfies this repo's no-new-deps rule) strips PAC bits from return addresses
   internally before returning them, requires no manual VA-width masking (fragile
   to hand-derive and get right without real arm64e hardware to test against), and
   allocates nothing given a caller-supplied buffer, matching this capture path's
   "no allocation" constraint. Frame 0 of backtrace()'s output is the PC inside
   this function itself; skip it so pcs[0] is the caller's return address, matching
   the semantics of RtlCaptureStackBackTrace(2, ...) above and the frame-pointer
   walk below (both of which also start at the caller, not this function). */
#include <execinfo.h>
size_t _mi_prof_stack_capture(void** pcs, size_t capacity) {
  if (capacity > 128) capacity = 128;
  if (capacity == 0) return 0;
  void* buf[130];  // capacity (<=128) + 1 extra for this function's own frame.
  const int want = (int)(capacity + 1);
  const int got = backtrace(buf, want);
  if (got <= 1) return 0;
  size_t n = (size_t)(got - 1);
  if (n > capacity) n = capacity;
  for (size_t i = 0; i < n; i++) pcs[i] = buf[i + 1];
  return n;
}
#else
size_t _mi_prof_stack_capture(void** pcs, size_t capacity) {
  void** fp = (void**)__builtin_frame_address(0);
  size_t n = 0;
  while (n < capacity && fp != NULL) {
    void* ret = fp[1];
    void** next = (void**)fp[0];
    if (ret == NULL) break;
    pcs[n++] = ret;
    if (next <= fp || (uintptr_t)next - (uintptr_t)fp > (8u << 20) ||
        (((uintptr_t)next & 0xF) != 0 && ((uintptr_t)next & 0x7) != 0)) break;
    fp = next;
  }
  return n;
}
#endif

mi_prof_stack_t* _mi_prof_stack_intern(void) {
  void* pcs[MI_PROF_BT_MAX_LIMIT];
  size_t max = (size_t)mi_option_get(mi_option_prof_bt_max);
  if (max > MI_PROF_BT_MAX_LIMIT) max = MI_PROF_BT_MAX_LIMIT;
  size_t depth = _mi_prof_stack_capture(pcs, max);
  if (depth == 0 || !stack_init()) return NULL;
  if (!stack_grow()) return NULL;
  uint64_t hash = stack_hash(pcs, depth);
  size_t index = (size_t)hash & (stack_capacity - 1);
  while (stack_table[index] != NULL) { if (stack_equal(stack_table[index],hash,pcs,depth)) { stack_table[index]->refcount++; return stack_table[index]; } index=(index+1)&(stack_capacity-1); }
  if (mi_atomic_load_relaxed(&stack_count) >= MI_PROF_STACK_CAP) { mi_atomic_increment_relaxed(&stack_overflows); return NULL; }
  mi_prof_stack_t* stack = (mi_prof_stack_t*)_mi_prof_arena_alloc(sizeof(*stack) + depth*sizeof(void*));
  if (stack == NULL) return NULL;
  stack->hash=hash; stack->depth=(uint32_t)depth; stack->refcount=1; stack->pin=0; stack->curobjs=stack->curbytes=stack->accumobjs=stack->accumbytes=0;
  for (size_t i=0;i<depth;i++) stack->pcs[i]=pcs[i];
  stack_table[index]=stack; stack->slot=index; mi_atomic_increment_relaxed(&stack_count);
  return stack;
}
void _mi_prof_stack_release(mi_prof_stack_t* stack) {
  if (stack == NULL || stack->refcount == 0) return;
  if (--stack->refcount != 0 || stack->pin != 0 || mi_option_is_enabled(mi_option_prof_accum)) return;
  size_t index=stack->slot; stack_table[index]=NULL; mi_atomic_decrement_relaxed(&stack_count);
  for (index=(index+1)&(stack_capacity-1); stack_table[index] != NULL; index=(index+1)&(stack_capacity-1)) {
    mi_prof_stack_t* moved=stack_table[index]; stack_table[index]=NULL; stack_place(moved);
  }
}
size_t _mi_prof_stack_count(void) { return mi_atomic_load_relaxed(&stack_count); }
size_t _mi_prof_stack_overflows(void) { return mi_atomic_load_relaxed(&stack_overflows); }
bool _mi_prof_stack_visit_info(mi_prof_visit_fun* fn, void* arg) {
  if (fn == NULL) return true;
  for (size_t i = 0; i < stack_capacity; i++) {
    mi_prof_stack_t* stack = stack_table[i];
    if (stack == NULL) continue;
    mi_prof_sample_info_t info;
    info.stack = (const void* const*)stack->pcs; info.depth = stack->depth;
    info.live_objects = stack->curobjs; info.live_bytes = stack->curbytes;
    info.accum_objects = stack->accumobjs; info.accum_bytes = stack->accumbytes;
    if (!fn(&info, arg)) return false;
  }
  return true;
}
void _mi_prof_stack_pin_all(void) { for (size_t i=0;i<stack_capacity;i++) if (stack_table[i] != NULL) stack_table[i]->pin++; }
static void stack_sweep(void) {
  bool removed = true;
  while (removed) {
    removed = false;
    for (size_t i = 0; i < stack_capacity; i++) {
      mi_prof_stack_t* stack = stack_table[i];
      if (stack != NULL && stack->refcount == 0 && stack->pin == 0) {
        stack_table[i] = NULL; mi_atomic_decrement_relaxed(&stack_count);
        for (size_t j = (i + 1) & (stack_capacity - 1); stack_table[j] != NULL; j = (j + 1) & (stack_capacity - 1)) {
          mi_prof_stack_t* moved = stack_table[j]; stack_table[j] = NULL; stack_place(moved);
        }
        removed = true;
        break;
      }
    }
  }
}
void _mi_prof_stack_unpin_all_and_sweep(void) {
  for (size_t i=0;i<stack_capacity;i++) if (stack_table[i] != NULL) stack_table[i]->pin--;
  /* accum mode keeps refcount-0 entries until mi_prof_reset; only reset may sweep them. */
  if (!mi_option_is_enabled(mi_option_prof_accum)) stack_sweep();
}
void _mi_prof_stack_reset(void) {
  if (stack_table == NULL) return;
  for (size_t i = 0; i < stack_capacity; i++) {
    mi_prof_stack_t* stack = stack_table[i];
    if (stack != NULL) { stack->accumobjs = 0; stack->accumbytes = 0; }
  }
  stack_sweep();
}
void _mi_prof_stack_done(void) { stack_table = NULL; stack_capacity = 0; mi_atomic_store_relaxed(&stack_count, (size_t)0); mi_atomic_store_relaxed(&stack_overflows, (size_t)0); }
void _mi_prof_stack_alloc(mi_prof_stack_t* stack, size_t size) { if (stack) { stack->curobjs++; stack->curbytes += size; if (mi_option_is_enabled(mi_option_prof_accum)) { stack->accumobjs++; stack->accumbytes += size; } } }
void _mi_prof_stack_free(mi_prof_stack_t* stack, size_t size) { if (stack) { stack->curobjs--; stack->curbytes -= size; } }
void _mi_prof_stack_resize(mi_prof_stack_t* stack, size_t oldsize, size_t newsize) { if (stack) { stack->curbytes = stack->curbytes - oldsize + newsize; if (mi_option_is_enabled(mi_option_prof_accum) && newsize > oldsize) stack->accumbytes += newsize - oldsize; } }
#else
size_t _mi_prof_stack_capture(void** pcs, size_t capacity) { MI_UNUSED(pcs); MI_UNUSED(capacity); return 0; }
#endif
