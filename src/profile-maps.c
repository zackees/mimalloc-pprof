/* Module-map serialization for heap_v2 profiles. This deliberately uses
   platform and raw file APIs only; callers invoke it after releasing the
   profiler table lock. */
#include "mimalloc.h"
#include "mimalloc/internal.h"
#include <stdio.h>
#include <string.h>

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

/* Structured module enumeration for mi_prof_modules_visit and the profile.proto Mapping table.
   Deliberately independent of _mi_prof_maps_append above (which stays a byte-for-byte copy of
   /proc/self/maps / an equivalent legacy-format line per platform): the TEXT emitter must not
   change behavior since test/test-profile.c's T2 parses it, so on Linux this is a *second*,
   from-scratch structured parser rather than a refactor of the verbatim-copy path. */
bool _mi_prof_maps_visit(mi_prof_module_visit_fun* visitor, void* arg) {
  if (visitor == NULL) return false;
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
    mi_prof_module_info_t m; m.path = path; m.base = (uintptr_t)info.lpBaseOfDll; m.size = (size_t)info.SizeOfImage;
    if (!visitor(&m, arg)) break;
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
    mi_prof_module_info_t m; m.path = path; m.base = start; m.size = end - start;
    if (!visitor(&m, arg)) break;
  }
#else
  /* Parse /proc/self/maps ourselves and merge contiguous same-file regions into one module each
     (a shared object is typically mapped as several adjacent r--p/r-xp/rw-p VMAs for its ELF
     LOAD segments); a module is reported once at least one of its contiguous regions is
     executable. Streamed line-by-line with fixed stack buffers, no heap allocation. */
  const int fd = open("/proc/self/maps", O_RDONLY);
  if (fd < 0) return false;
  enum { MAPS_LINE_MAX = 1024 };
  char line[MAPS_LINE_MAX]; size_t line_len = 0; bool line_overflow = false;
  char run_path[MAPS_LINE_MAX]; uintptr_t run_start = 0, run_end = 0; bool run_has_exec = false, run_active = false;
  bool stop = false, failed = false;
  char rbuf[4096]; ssize_t n;
  while (!stop && (n = read(fd, rbuf, sizeof(rbuf))) > 0) {
    for (ssize_t i = 0; i < n && !stop; i++) {
      const char c = rbuf[i];
      if (c != '\n') { if (line_len < sizeof(line) - 1) line[line_len++] = c; else line_overflow = true; continue; }
      line[line_len] = 0;
      if (line_overflow || line_len == 0) { line_len = 0; line_overflow = false; continue; }
      unsigned long long start = 0, end = 0; char perms[8] = { 0 }; char path[MAPS_LINE_MAX] = { 0 };
      const int fields = sscanf(line, "%llx-%llx %7s %*x %*x:%*x %*u %1023s", &start, &end, perms, path);  // offset/dev/inode intentionally unassigned (%*..).
      const bool has_path = (fields >= 4 && path[0] == '/');
      if (has_path && run_active && strcmp(run_path, path) == 0 && (uintptr_t)start == run_end) {
        run_end = (uintptr_t)end;
        if (perms[2] == 'x') run_has_exec = true;
      } else {
        if (run_active && run_has_exec) { mi_prof_module_info_t m; m.path = run_path; m.base = run_start; m.size = run_end - run_start; if (!visitor(&m, arg)) stop = true; }
        run_active = false;
        if (has_path && !stop) {
          const size_t plen = maps_min(strlen(path), sizeof(run_path) - 1);
          memcpy(run_path, path, plen); run_path[plen] = 0;
          run_start = (uintptr_t)start; run_end = (uintptr_t)end; run_has_exec = (perms[2] == 'x'); run_active = true;
        }
      }
      line_len = 0; line_overflow = false;
    }
  }
  if (n < 0) failed = true;
  close(fd);
  if (!stop && !failed && run_active && run_has_exec) { mi_prof_module_info_t m; m.path = run_path; m.base = run_start; m.size = run_end - run_start; (void)visitor(&m, arg); /* last entry: nothing left to stop for */ }
  if (failed) return false;
#endif
  return true;
}

#endif
