/* Allocation-free stack capture for the exact DHAT observer. Kept separate from
   profile-stack.c because DHAT is deliberately available when MI_PPROF=OFF. */
#include "mimalloc.h"
#include "mimalloc/internal.h"

// #371: only built (and only declared) when the observer is compiled in.
#if MI_DHAT

#define MI_DHAT_STACK_MAX 128

#ifdef _WIN32
#include <windows.h>
size_t _mi_dhat_stack_capture(void** pcs, size_t capacity) {
  if (capacity > MI_DHAT_STACK_MAX) capacity = MI_DHAT_STACK_MAX;
  return (size_t)RtlCaptureStackBackTrace(2, (ULONG)capacity, pcs, NULL);
}
#elif defined(__APPLE__)
#include <execinfo.h>
size_t _mi_dhat_stack_capture(void** pcs, size_t capacity) {
  if (capacity > MI_DHAT_STACK_MAX) capacity = MI_DHAT_STACK_MAX;
  if (capacity == 0) return 0;
  void* frames[MI_DHAT_STACK_MAX + 1];
  const int count = backtrace(frames, (int)(capacity + 1));
  if (count <= 1) return 0;
  size_t n = (size_t)(count - 1);
  if (n > capacity) n = capacity;
  for (size_t i = 0; i < n; i++) pcs[i] = frames[i + 1];
  return n;
}
#else
size_t _mi_dhat_stack_capture(void** pcs, size_t capacity) {
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

#endif // MI_DHAT
