/* Platform stack capture for sampled allocations.  This routine allocates
   nothing: the caller supplies its profiler-arena backed PC buffer. */
#include "mimalloc.h"
#include "mimalloc/internal.h"
#include <string.h>
#include <stdio.h>

#if MI_PPROF

#define MI_PROF_BT_MAX_LIMIT 128
#define MI_PROF_STACK_CAP 65536
struct mi_prof_stack_s { uint64_t hash; uint32_t depth; uint32_t refcount; uint32_t pin; size_t slot; size_t curobjs, curbytes, accumobjs, accumbytes; void* pcs[]; };
static mi_prof_stack_t** stack_table;
static size_t stack_capacity;
static size_t stack_count;
static inline size_t stack_min(size_t x, size_t y) { return (x < y ? x : y); }

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
  if (stack_count * 4 < stack_capacity * 3) return true;
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
  if (stack_count >= MI_PROF_STACK_CAP) return NULL;
  mi_prof_stack_t* stack = (mi_prof_stack_t*)_mi_prof_arena_alloc(sizeof(*stack) + depth*sizeof(void*));
  if (stack == NULL) return NULL;
  stack->hash=hash; stack->depth=(uint32_t)depth; stack->refcount=1; stack->pin=0; stack->curobjs=stack->curbytes=stack->accumobjs=stack->accumbytes=0;
  for (size_t i=0;i<depth;i++) stack->pcs[i]=pcs[i];
  stack_table[index]=stack; stack->slot=index; stack_count++;
  return stack;
}
void _mi_prof_stack_release(mi_prof_stack_t* stack) {
  if (stack == NULL || stack->refcount == 0) return;
  if (--stack->refcount != 0 || stack->pin != 0 || mi_option_is_enabled(mi_option_prof_accum)) return;
  size_t index=stack->slot; stack_table[index]=NULL; stack_count--;
  for (index=(index+1)&(stack_capacity-1); stack_table[index] != NULL; index=(index+1)&(stack_capacity-1)) {
    mi_prof_stack_t* moved=stack_table[index]; stack_table[index]=NULL; stack_place(moved);
  }
}
size_t _mi_prof_stack_count(void) { return stack_count; }
void _mi_prof_stack_visit(_mi_prof_stack_visit_fun* visit, void* arg) { if (visit != NULL) for (size_t i=0;i<stack_capacity;i++) if (stack_table[i] != NULL) visit(stack_table[i],arg); }
void _mi_prof_stack_counts(const mi_prof_stack_t* stack, size_t* curobjs, size_t* curbytes, size_t* accumobjs, size_t* accumbytes) {
  if (curobjs) *curobjs = (stack == NULL ? 0 : stack->curobjs);
  if (curbytes) *curbytes = (stack == NULL ? 0 : stack->curbytes);
  if (accumobjs) *accumobjs = (stack == NULL ? 0 : stack->accumobjs);
  if (accumbytes) *accumbytes = (stack == NULL ? 0 : stack->accumbytes);
}
void _mi_prof_stack_reset(void) {
  if (stack_table == NULL) return;
  for (size_t i = 0; i < stack_capacity; i++) {
    mi_prof_stack_t* stack = stack_table[i];
    if (stack != NULL) { stack->accumobjs = 0; stack->accumbytes = 0; }
  }
  bool removed = true;
  while (removed) {
    removed = false;
    for (size_t i = 0; i < stack_capacity; i++) {
      mi_prof_stack_t* stack = stack_table[i];
      if (stack != NULL && stack->refcount == 0 && stack->pin == 0) {
        stack_table[i] = NULL; stack_count--;
        for (size_t j = (i + 1) & (stack_capacity - 1); stack_table[j] != NULL; j = (j + 1) & (stack_capacity - 1)) {
          mi_prof_stack_t* moved = stack_table[j]; stack_table[j] = NULL; stack_place(moved);
        }
        removed = true;
        break;
      }
    }
  }
}
void _mi_prof_stack_done(void) { stack_table = NULL; stack_capacity = 0; stack_count = 0; }
void _mi_prof_stack_format(const mi_prof_stack_t* stack, char* buf, size_t capacity, size_t* written) {
  if (stack == NULL || buf == NULL || capacity == 0) { if (written) *written=0; return; }
  int n = snprintf(buf, capacity, "%7llu: %llu [%7llu: %llu] @", (unsigned long long)stack->curobjs, (unsigned long long)stack->curbytes, (unsigned long long)stack->accumobjs, (unsigned long long)stack->accumbytes);
  size_t used = (n > 0 ? stack_min((size_t)n, capacity - 1) : 0);
  for (size_t i=0; i<stack->depth && used < capacity - 1; i++) { n=snprintf(buf+used,capacity-used," 0x%llx",(unsigned long long)(uintptr_t)stack->pcs[i]); used += (n>0 ? stack_min((size_t)n,capacity-used-1) : 0); }
  if (used < capacity - 1) { buf[used++]='\n'; }
  buf[used] = 0;
  if (written) *written=used;
}
void _mi_prof_stack_alloc(mi_prof_stack_t* stack, size_t size) { if (stack) { stack->curobjs++; stack->curbytes += size; if (mi_option_is_enabled(mi_option_prof_accum)) { stack->accumobjs++; stack->accumbytes += size; } } }
void _mi_prof_stack_free(mi_prof_stack_t* stack, size_t size) { if (stack) { stack->curobjs--; stack->curbytes -= size; } }
void _mi_prof_stack_resize(mi_prof_stack_t* stack, size_t oldsize, size_t newsize) { if (stack) { stack->curbytes = stack->curbytes - oldsize + newsize; if (mi_option_is_enabled(mi_option_prof_accum) && newsize > oldsize) stack->accumbytes += newsize - oldsize; } }
#else
size_t _mi_prof_stack_capture(void** pcs, size_t capacity) { MI_UNUSED(pcs); MI_UNUSED(capacity); return 0; }
#endif
