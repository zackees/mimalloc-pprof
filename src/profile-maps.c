/* Module-map serialization for heap_v2 profiles. This deliberately uses
   platform and raw file APIs only; callers invoke it after releasing the
   profiler table lock. */
#include "mimalloc.h"
#include "mimalloc/internal.h"
#include <stdio.h>

#if MI_PPROF

static inline size_t maps_min(size_t x, size_t y) { return (x < y ? x : y); }

#if defined(_WIN32)
  #define PSAPI_VERSION 2
  #include <windows.h>
  #include <psapi.h>
#elif defined(__APPLE__)
  #include <mach-o/dyld.h>
  #include <mach-o/loader.h>
#else
  #include <fcntl.h>
  #include <unistd.h>
#endif

bool _mi_prof_maps_append(_mi_prof_dump_append_fun* append, void* arg) {
  if (append == NULL) return false;
#if defined(_WIN32)
  HMODULE modules[1024]; DWORD needed = 0;
  if (!K32EnumProcessModulesEx(GetCurrentProcess(), modules, sizeof(modules), &needed, LIST_MODULES_ALL)) return false;
  const size_t count = maps_min((size_t)(needed / sizeof(modules[0])), sizeof(modules) / sizeof(modules[0]));
  for (size_t i = 0; i < count; i++) {
    MODULEINFO info; char path[MAX_PATH];
    if (!K32GetModuleInformation(GetCurrentProcess(), modules[i], &info, sizeof(info))) continue;
    DWORD len = GetModuleFileNameA(modules[i], path, (DWORD)sizeof(path));
    if (len == 0) continue;
    path[sizeof(path) - 1] = 0;
    char line[MAX_PATH + 96];
    int n = snprintf(line, sizeof(line), "%llx-%llx r-xp 00000000 00:00 0    %s\n", (unsigned long long)(uintptr_t)info.lpBaseOfDll, (unsigned long long)(uintptr_t)info.lpBaseOfDll + info.SizeOfImage, path);
    if (n < 0 || !append(arg, line, maps_min((size_t)n, sizeof(line) - 1))) return false;
  }
#elif defined(__APPLE__)
  const uint32_t count = _dyld_image_count();
  for (uint32_t i = 0; i < count; i++) {
    const struct mach_header* header = _dyld_get_image_header(i); const char* path = _dyld_get_image_name(i);
    if (header == NULL || path == NULL) continue;
    uintptr_t start = UINTPTR_MAX, end = 0;
    const intptr_t slide = _dyld_get_image_vmaddr_slide(i);
    const uint8_t* p = (const uint8_t*)header + ((header->magic == MH_MAGIC_64 || header->magic == MH_CIGAM_64) ? sizeof(struct mach_header_64) : sizeof(struct mach_header));
    for (uint32_t j = 0; j < header->ncmds; j++) {
      const struct load_command* cmd = (const struct load_command*)p;
      if (cmd->cmd == LC_SEGMENT) {
        const struct segment_command* seg = (const struct segment_command*)cmd;
        start = maps_min(start, (uintptr_t)(slide + (intptr_t)seg->vmaddr));
        end = (end > (uintptr_t)(slide + (intptr_t)seg->vmaddr + seg->vmsize) ? end : (uintptr_t)(slide + (intptr_t)seg->vmaddr + seg->vmsize));
      }
      else if (cmd->cmd == LC_SEGMENT_64) {
        const struct segment_command_64* seg = (const struct segment_command_64*)cmd;
        start = maps_min(start, (uintptr_t)(slide + (intptr_t)seg->vmaddr));
        end = (end > (uintptr_t)(slide + (intptr_t)seg->vmaddr + seg->vmsize) ? end : (uintptr_t)(slide + (intptr_t)seg->vmaddr + seg->vmsize));
      }
      if (cmd->cmdsize < sizeof(*cmd)) break;
      p += cmd->cmdsize;
    }
    if (start == UINTPTR_MAX) start = (uintptr_t)header;
    if (end <= start) end = start + 1;
    char line[4096]; int n = snprintf(line, sizeof(line), "%llx-%llx r-xp 00000000 00:00 0    %s\n", (unsigned long long)start, (unsigned long long)end, path);
    if (n < 0 || !append(arg, line, maps_min((size_t)n, sizeof(line) - 1))) return false;
  }
#else
  const int fd = open("/proc/self/maps", O_RDONLY);
  if (fd < 0) return false;
  char buf[4096]; ssize_t n;
  while ((n = read(fd, buf, sizeof(buf))) > 0) { if (!append(arg, buf, (size_t)n)) { close(fd); return false; } }
  close(fd);
  if (n < 0) return false;
#endif
  return true;
}

#endif
