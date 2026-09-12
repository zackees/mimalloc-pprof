#pragma once

// Temporary #396 instrumentation. No allocator/CRT calls or owner-private reads.
#if defined(MI_TLD_TRACE) && defined(_WIN32)
#include <windows.h>
static void mi_tld_trace(const char* event, const mi_tld_t* tld) {
  const DWORD saved_error = GetLastError();
  char buf[512]; size_t n = 0;
  const char* prefix = "TRACE396 ";
  while (*prefix) buf[n++] = *prefix++;
  while (*event) buf[n++] = *event++;
  const char* names[] = {" osid=", " caller=", " tld=", " owner=", " seq=", " state=", " flags=", " sweeper="};
  const uintptr_t vals[] = {GetCurrentThreadId(), (uintptr_t)_mi_thread_id(), (uintptr_t)tld,
    tld ? (uintptr_t)tld->thread_id : 0, tld ? tld->thread_seq : 0,
    tld ? mi_atomic_load_relaxed((_Atomic(size_t)*)&tld->park_state) : 0,
    tld ? mi_atomic_load_relaxed((_Atomic(size_t)*)&tld->gate_flags) : 0,
    tld ? mi_atomic_load_relaxed((_Atomic(uintptr_t)*)&tld->sweeper) : 0};
  for (size_t j = 0; j < sizeof(vals)/sizeof(vals[0]); ++j) {
    const char* s = names[j]; while (*s) buf[n++] = *s++;
    for (int k = (int)(sizeof(uintptr_t)*2)-1; k >= 0; --k) buf[n++] = "0123456789abcdef"[(vals[j] >> (k*4)) & 15];
  }
  buf[n++] = '\n'; DWORD written;
  WriteFile(GetStdHandle(STD_ERROR_HANDLE), buf, (DWORD)n, &written, NULL);
  SetLastError(saved_error);
}
#else
#define mi_tld_trace(event,tld) ((void)0)
#endif
