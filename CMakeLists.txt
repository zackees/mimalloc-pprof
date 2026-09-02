cmake_minimum_required(VERSION 3.18)
project(libmimalloc C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)

option(MI_SECURE            "Use security mitigations (like meta data guard pages, allocation randomization, double-free mitigation, and free-list corruption detection)" OFF)
option(MI_SECURE_FULL       "Use full security mitigations including guard pages at the end of each mimalloc page (may be expensive)" OFF)
option(MI_OVERRIDE          "Override the standard malloc interface (i.e. define entry points for 'malloc', 'free', etc)" ON)
option(MI_XMALLOC           "Enable abort() call on memory allocation failure by default" OFF)
option(MI_SHOW_ERRORS       "Show error and warning messages by default (only enabled by default in DEBUG mode)" OFF)
option(MI_GUARDED           "Build with guard pages behind certain object allocations (enabled by default in a debug build)" OFF)
option(MI_USE_CXX           "Use the C++ compiler to compile the library (instead of the C compiler)" OFF)
option(MI_OPT_ARCH          "Only for optimized builds: turn on architecture specific optimizations (for x64: '-march=haswell;-mavx2' (2013), for arm64: '-march=armv8.1-a' (2016))" OFF)
option(MI_OPT_SIMD          "Use SIMD instructions (requires MI_OPT_ARCH to be enabled)" OFF)
option(MI_FREE_IS_CHECKED   "Always check for invalid pointers in 'free' (always enabled in debug and secure builds, or when MI_OVERRIDE is ON)" OFF)

option(MI_DEBUG             "Enable assertion checks (enabled by default in a debug build)" OFF)
option(MI_DEBUG_INTERNAL    "Enable assertion and internal invariant checks (enabled by default in a debug build)" OFF)
option(MI_DEBUG_FULL        "Enable assertion checks and expensive internal heap invariant checking" OFF)

option(MI_DEBUG_TSAN        "Build with thread sanitizer (needs clang)" OFF)
option(MI_DEBUG_UBSAN       "Build with undefined-behavior sanitizer (needs clang++)" OFF)
option(MI_TRACK_VALGRIND    "Compile with Valgrind support (adds a small overhead)" OFF)
option(MI_TRACK_ASAN        "Compile with address sanitizer support (adds a small overhead)" OFF)
option(MI_TRACK_ETW         "Compile with Windows event tracing (ETW) support (adds a small overhead)" OFF)

option(MI_BUILD_SHARED      "Build shared library" ON)
option(MI_BUILD_STATIC      "Build static library" ON)
option(MI_BUILD_OBJECT      "Build object library" ON)
option(MI_BUILD_TESTS       "Build test executables" ON)

# OS specific
option(MI_OSX_INTERPOSE     "Use interpose to override standard malloc on macOS" ON)
option(MI_OSX_ZONE          "Use malloc zone to override standard malloc on macOS" ON)
option(MI_WIN_REDIRECT      "Use redirection module ('mimalloc-redirect') on Windows if compiling mimalloc as a DLL" ON)
option(MI_MINGW_REDIRECT_FIRST "Emit 'mimalloc-redirect.dll' as the first import descriptor of mimalloc.dll (MinGW; see #277)" ON)
option(MI_WIN_DIRECT_TLS    "Use only direct TLS slots on Windows to avoid extra tests in the malloc fast path (only works if the program uses less than 64 TlsAlloc'd slots in total)" OFF)
option(MI_LOCAL_DYNAMIC_TLS "Use local-dynamic-tls, a slightly slower but dlopen-compatible thread local storage mechanism (Unix)" OFF)
option(MI_LIBC_MUSL         "Enable this when linking with musl libc" OFF)

# advanced
option(MI_OPT_FREE_SMALL    "Use optimized `mi_free_small` without pagemap lookup (can only be used if SECURE or GUARDED mode are off)" OFF)
option(MI_PADDING           "Enable padding to detect heap block overflow (always on in DEBUG or SECURE mode, or with Valgrind/ASAN)" OFF)
option(MI_NO_PADDING        "Force no use of padding even in DEBUG mode etc." OFF)
option(MI_SKIP_COLLECT_ON_EXIT "Skip collecting memory on program exit" OFF)
# imported from oven-sh/mimalloc @ 942b8342, MIT -- see #268.
option(MI_NO_PROCESS_DETACH "Skip automatic mi_process_done at process exit (embedder calls it manually or not at all)" OFF)
option(MI_NO_THP            "Disable transparent huge pages support on Linux/Android for the mimalloc process only" OFF)
option(MI_EXTRA_CPPDEFS     "Extra pre-processor definitions (use as `-DMI_EXTRA_CPPDEFS=\"opt1=val1;opt2=val2\"`)" "")
option(MI_SEE_ASM           "Generate assembly files" OFF)
option(MI_INSTALL_TOPLEVEL  "Install directly into $CMAKE_INSTALL_PREFIX instead of PREFIX/lib/mimalloc-version" OFF)

# negated options for vcpkg features
option(MI_NO_USE_CXX        "Use plain C compilation (has priority over MI_USE_CXX)" OFF)
option(MI_NO_OPT_ARCH       "Do not use architecture specific optimizations (like '-march=armv8.1-a' for example) (has priority over MI_OPT_ARCH)" OFF)
option(MI_FUZZ              "Build the libFuzzer harnesses in test/fuzz (requires clang; see issue #87)" OFF)
option(MI_FUZZ_PLANT_BUG    "Plant a deliberate bug in the fuzz harness so CI can prove the fuzzer finds it" OFF)
option(MI_NO_DEBUG          "Disable all assertion checks even in debug builds" OFF)

# experimental
option(MI_WIN_INIT_USE_RAW_DLLMAIN "Use the raw DLL main entry point for mimalloc initialization; can be more robust but can also lead to link errors with other libraries" OFF)
option(MI_WIN_INIT_USE_TLS_DLLMAIN "Use legacy TLS entries with DllMain for mimalloc initialization" OFF)

option(MI_TLS_MODEL_LOCAL    "Force TLS model based on thread local variables" OFF)
option(MI_TLS_MODEL_PTHREADS "Force TLS model based on pthreads" OFF)
option(MI_TLS_MODEL_FIXED    "Force TLS model based on fixed thread local slots" OFF)
option(MI_TLS_RECURSE_GUARD  "Force a recursion check on thread local access (for MI_TLS_MODEL_LOCAL only)" OFF)

# deprecated options
option(MI_WIN_USE_FLS       "Use Fiber local storage on Windows to detect thread termination (deprecated)" OFF)
option(MI_CHECK_FULL        "Use full internal invariant checking in DEBUG mode (deprecated, use MI_DEBUG_FULL instead)" OFF)
option(MI_USE_LIBATOMIC     "Explicitly link with -latomic (on older systems) (deprecated and detected automatically)" OFF)
option(MI_PPROF             "Enable the allocation sampling profiler" ON)

include(CheckLinkerFlag)    # requires cmake 3.18
include(CheckIncludeFiles)
include(GNUInstallDirs)
include("cmake/mimalloc-config-version.cmake")

set(mi_sources
    src/alloc.c
    src/alloc-aligned.c
    src/alloc-posix.c
    src/arena.c
    src/bitmap.c
    src/heap.c
    src/heap-dump.c
    src/init.c
    src/libc.c
    src/memory-events.c
    src/dhat.c
    src/dhat-stack.c
    src/options.c
    src/os.c
    src/page.c
    src/page-map.c
    src/page-holes.c
    src/profile.c
    src/random.c
    src/scavenger.c
    src/stats.c
    src/subproc.c
    src/theap.c
    src/threadlocal.c
    src/prim/prim.c
    src/prim/prim-tls.c)

if(MI_PPROF)
  list(APPEND mi_sources
    src/profile-stack.c
    src/profile-maps.c)
endif()

set(mi_cflags "")
set(mi_cflags_static "")            # extra flags for a static library build
set(mi_cflags_dynamic "")           # extra flags for a shared-object library build
set(mi_libraries "")

if(MI_EXTRA_CPPDEFS)
 set(mi_defines ${MI_EXTRA_CPPDEFS})
else()
 set(mi_defines "")
endif()

# pass git revision as a define
if(EXISTS "${CMAKE_SOURCE_DIR}/.git/index")
  find_package(Git)
  if(GIT_FOUND)
    execute_process(COMMAND ${GIT_EXECUTABLE} "describe" OUTPUT_VARIABLE mi_git_describe RESULT_VARIABLE mi_git_res ERROR_QUIET OUTPUT_STRIP_TRAILING_WHITESPACE)
    if(mi_git_res EQUAL "0")
      list(APPEND mi_defines "MI_GIT_DESCRIBE=${mi_git_describe}")
      # add to dependencies so we rebuild if the git head commit changes
      set_property(GLOBAL APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${CMAKE_SOURCE_DIR}/.git/index")
    endif()
  endif()
endif()

# -----------------------------------------------------------------------------
# Convenience: set default build type and compiler depending on the build directory
# -----------------------------------------------------------------------------

message(STATUS "")
if(NOT CMAKE_BUILD_TYPE)
  if ("${CMAKE_BINARY_DIR}" MATCHES ".*((D|d)ebug|asan|tsan|ubsan|valgrind)$")
    message(STATUS "No build type selected, default to 'Debug'")
    set(CMAKE_BUILD_TYPE "Debug")
  else()
    message(STATUS "No build type selected, default to 'Release'")
    set(CMAKE_BUILD_TYPE "Release")
  endif()
endif()

if(CMAKE_GENERATOR MATCHES "^Visual Studio.*$")
  message(STATUS "Note: when building with Visual Studio the build type is specified when building.")
  message(STATUS "For example: 'cmake --build . --config=Release")
endif()

if(NOT MI_SECURE AND NOT MI_SECURE_FULL AND "${CMAKE_BINARY_DIR}" MATCHES ".*(S|s)ecure$")
  message(STATUS "Default to secure build")
  set(MI_SECURE "ON")
endif()


# Determine architecture
set(MI_OPT_ARCH_FLAGS "")
set(MI_ARCH "unknown")
if(CMAKE_SYSTEM_PROCESSOR MATCHES "^(x86|i[3456]86)$" OR CMAKE_GENERATOR_PLATFORM MATCHES "^(x86|Win32)$")
  set(MI_ARCH "x86")
elseif((CMAKE_SYSTEM_PROCESSOR MATCHES "^(x86_64|x64|amd64|AMD64)$" OR CMAKE_GENERATOR_PLATFORM STREQUAL "x64" OR "x86_64" IN_LIST CMAKE_OSX_ARCHITECTURES) AND NOT CMAKE_GENERATOR_PLATFORM STREQUAL "ARM64") # must be before arm64
  set(MI_ARCH "x64")
elseif(CMAKE_SYSTEM_PROCESSOR MATCHES "^(aarch64|arm64|armv[89].?|ARM64)$" OR CMAKE_GENERATOR_PLATFORM STREQUAL "ARM64" OR "arm64" IN_LIST CMAKE_OSX_ARCHITECTURES)
  set(MI_ARCH "arm64")
elseif(CMAKE_SYSTEM_PROCESSOR MATCHES "^(arm|armv[34567].?|ARM)$")
  set(MI_ARCH "arm32")
elseif(CMAKE_SYSTEM_PROCESSOR MATCHES "^(riscv|riscv32|riscv64)$")
  if(CMAKE_SIZEOF_VOID_P==4)
    set(MI_ARCH "riscv32")
  else()
    set(MI_ARCH "riscv64")
  endif()
else()
  set(MI_ARCH ${CMAKE_SYSTEM_PROCESSOR})
endif()
message(STATUS "Architecture: ${MI_ARCH}") # (${CMAKE_SYSTEM_PROCESSOR}, ${CMAKE_GENERATOR_PLATFORM}, ${CMAKE_GENERATOR})")

# negative overrides (mainly to support vcpkg features)
if(MI_NO_USE_CXX)
  set(MI_USE_CXX "OFF")
endif()

if(MI_NO_OPT_ARCH)
  set(MI_OPT_ARCH "OFF")
elseif(MI_ARCH STREQUAL "arm64")
  set(MI_OPT_ARCH "ON")  # enable armv8.1-a by default on arm64 unless MI_NO_OPT_ARCH is set
endif()

if(DEFINED ENV{MI_TEST_LIGHT})
  list(APPEND mi_defines MI_TEST_LIGHT=1)  # to lighten test load in test integration
endif()

# -----------------------------------------------------------------------------
# Enable the C++ compiler early on if needed
# -----------------------------------------------------------------------------

# clang-cl detection on windows
if(CMAKE_C_COMPILER_ID STREQUAL "Clang" AND CMAKE_C_COMPILER_FRONTEND_VARIANT STREQUAL "MSVC")
  set(MI_CLANG_CL "ON")
endif()

# force C++ compilation with msvc or clang-cl to use modern C++ atomics
if(CMAKE_C_COMPILER_ID MATCHES "MSVC|Intel" OR MI_CLANG_CL)
  set(MI_USE_CXX "ON")
elseif(MI_DEBUG_UBSAN AND CMAKE_BUILD_TYPE MATCHES "Debug")  # ubsan needs C++
  set(MI_USE_CXX "ON")
endif()

# enable C++ ?
if(MI_USE_CXX)
  enable_language(CXX)
  message(STATUS "Use the C++ compiler to compile (MI_USE_CXX=ON)")
  if(CMAKE_CXX_COMPILER_ID MATCHES "Intel" AND NOT CMAKE_CXX_COMPILER_ID MATCHES "IntelLLVM")
    list(APPEND mi_cflags -Kc++)
  endif()
endif()


# -----------------------------------------------------------------------------
# Process options
# -----------------------------------------------------------------------------

# put -Wall early so other warnings can be disabled selectively
if(CMAKE_C_COMPILER_ID MATCHES "AppleClang|Clang")
  if (MI_CLANG_CL)
    list(APPEND mi_cflags -W)
  else()
    list(APPEND mi_cflags -Wall -Wextra -Wpedantic)
  endif()
endif()
if(CMAKE_C_COMPILER_ID MATCHES "GNU")
    list(APPEND mi_cflags -Wall -Wextra)
endif()
if(CMAKE_C_COMPILER_ID MATCHES "Intel")
    list(APPEND mi_cflags -Wall)
endif()

if(CMAKE_CXX_COMPILER_ID MATCHES "AppleClang|Clang")
  list(APPEND mi_cflags -Wno-deprecated)
endif()

if(MI_OVERRIDE)
  message(STATUS "Override standard malloc (MI_OVERRIDE=ON)")
  if(APPLE)
    if(MI_OSX_ZONE)
      # use zone's on macOS
      message(STATUS "  Use malloc zone to override malloc (MI_OSX_ZONE=ON)")
      list(APPEND mi_sources src/prim/osx/alloc-override-zone.c)
      list(APPEND mi_defines MI_OSX_ZONE=1)
      if (NOT MI_OSX_INTERPOSE)
        message(STATUS "  WARNING: zone overriding usually also needs interpose (use -DMI_OSX_INTERPOSE=ON)")
      endif()
    endif()
    if(MI_OSX_INTERPOSE)
      # use interpose on macOS
      message(STATUS "  Use interpose to override malloc (MI_OSX_INTERPOSE=ON)")
      list(APPEND mi_defines MI_OSX_INTERPOSE=1)
      if (NOT MI_OSX_ZONE)
        message(STATUS "  WARNING: interpose usually also needs zone overriding (use -DMI_OSX_ZONE=ON)")
      endif()
    endif()
    if(MI_USE_CXX AND MI_OSX_INTERPOSE)
      message(STATUS "  WARNING: if dynamically overriding malloc/free, it is more reliable to build mimalloc as C code (use -DMI_USE_CXX=OFF)")
    endif()
  endif()
endif()

if(WIN32)
  if (NOT MI_WIN_REDIRECT)
    # use a negative define for backward compatibility
    list(APPEND mi_defines MI_WIN_NOREDIRECT=1)
  endif()
endif()

if(MI_SECURE_FULL)
  set(MI_SECURE ON)
  message(STATUS "Set full secure build (MI_SECURE_FULL=ON)")
  list(APPEND mi_defines MI_SECURE=5)
elseif(MI_SECURE)
  message(STATUS "Set secure build (MI_SECURE=ON)")
  list(APPEND mi_defines MI_SECURE=4)
endif()

if(MI_TRACK_VALGRIND)
  CHECK_INCLUDE_FILES("valgrind/valgrind.h;valgrind/memcheck.h" MI_HAS_VALGRINDH)
  if (NOT MI_HAS_VALGRINDH)
    set(MI_TRACK_VALGRIND OFF)
    message(WARNING "Cannot find the 'valgrind/valgrind.h' and 'valgrind/memcheck.h' -- install valgrind first?")
    message(STATUS  "Disabling Valgrind support (MI_TRACK_VALGRIND=OFF)")
  else()
    message(STATUS "Compile with Valgrind support (MI_TRACK_VALGRIND=ON)")
    list(APPEND mi_defines MI_TRACK_VALGRIND=1)
  endif()
endif()

if(MI_TRACK_ASAN)
  if (APPLE AND MI_OVERRIDE)
    set(MI_TRACK_ASAN OFF)
    message(WARNING "Cannot enable address sanitizer support on macOS if MI_OVERRIDE is ON (MI_TRACK_ASAN=OFF)")
  endif()
  if (MI_TRACK_VALGRIND)
    set(MI_TRACK_ASAN OFF)
    message(WARNING "Cannot enable address sanitizer support with also Valgrind support enabled (MI_TRACK_ASAN=OFF)")
  endif()
  if(MI_TRACK_ASAN)
    CHECK_INCLUDE_FILES("sanitizer/asan_interface.h" MI_HAS_ASANH)
    if (NOT MI_HAS_ASANH)
      set(MI_TRACK_ASAN OFF)
      message(WARNING "Cannot find the 'sanitizer/asan_interface.h' -- install address sanitizer support first")
      message(STATUS  "Compile **without** address sanitizer support (MI_TRACK_ASAN=OFF)")
    else()
      message(STATUS "Compile with address sanitizer support (MI_TRACK_ASAN=ON)")
      list(APPEND mi_defines MI_TRACK_ASAN=1)
      list(APPEND mi_cflags -fsanitize=address)
      list(APPEND mi_libraries -fsanitize=address)
    endif()
  endif()
endif()

if(MI_TRACK_ETW)
  if(NOT WIN32)
    set(MI_TRACK_ETW OFF)
    message(WARNING "Can only enable ETW support on Windows (MI_TRACK_ETW=OFF)")
  endif()
  if (MI_TRACK_VALGRIND OR MI_TRACK_ASAN)
    set(MI_TRACK_ETW OFF)
    message(WARNING "Cannot enable ETW support with also Valgrind or ASAN support enabled (MI_TRACK_ETW=OFF)")
  endif()
  if(MI_TRACK_ETW)
    message(STATUS "Compile with Windows event tracing support (MI_TRACK_ETW=ON)")
    list(APPEND mi_defines MI_TRACK_ETW=1)
  endif()
endif()

if(MI_SEE_ASM)
  message(STATUS "Generate assembly listings (MI_SEE_ASM=ON)")
  if(CMAKE_C_COMPILER_ID MATCHES "MSVC" OR MI_CLANG_CL)
    list(APPEND mi_cflags -FA)
  else()
    list(APPEND mi_cflags -save-temps)
    if(CMAKE_C_COMPILER_ID MATCHES "AppleClang|Clang" AND CMAKE_C_COMPILER_VERSION VERSION_GREATER 14)
      message(STATUS "No GNU Line marker")
      list(APPEND mi_cflags -Wno-gnu-line-marker)
    endif()
  endif()
endif()

if(MI_CHECK_FULL)
  message(STATUS "The MI_CHECK_FULL option is deprecated, use MI_DEBUG_FULL instead")
  set(MI_DEBUG_FULL "ON")
endif()

if (MI_SKIP_COLLECT_ON_EXIT)
  message(STATUS "Skip collecting memory on program exit (MI_SKIP_COLLECT_ON_EXIT=ON)")
  list(APPEND mi_defines MI_SKIP_COLLECT_ON_EXIT=1)
endif()

# imported from oven-sh/mimalloc @ 942b8342, MIT -- see #268.
if (MI_NO_PROCESS_DETACH)
  message(STATUS "Skip automatic mi_process_done at process exit (MI_NO_PROCESS_DETACH=ON)")
  list(APPEND mi_defines MI_NO_PROCESS_DETACH=1)
endif()

if(MI_NO_DEBUG)
  message(STATUS "Set debug level zero, all assertions are disabled (MI_NO_DEBUG=ON)")
  set(MI_DEBUG OFF)
  list(APPEND mi_defines MI_DEBUG=0)
elseif(MI_DEBUG_FULL)
  message(STATUS "Set debug level to full assertion and internal invariant checking (MI_DEBUG_FULL=ON, expensive)")
  set(MI_DEBUG ON)
  list(APPEND mi_defines MI_DEBUG=3)   # full invariant checking (mi_assert, mi_assert_internal, and mi_assert_expensive)
elseif(MI_DEBUG_INTERNAL)
  message(STATUS "Set debug level to internal assertion and invariant checking (MI_DEBUG_INTERNAL=ON)")
  set(MI_DEBUG ON)
  list(APPEND mi_defines MI_DEBUG=2)   # invariant checking (mi_assert and mi_assert_internal)
elseif(MI_DEBUG)
  message(STATUS "Set debug level to assertion checking (MI_DEBUG=ON)")
  list(APPEND mi_defines MI_DEBUG=1)   # assertion checking (mi_assert)
elseif(CMAKE_BUILD_TYPE MATCHES "Debug")
  message(STATUS "Set debug level to internal assertion and invariant checking (CMAKE_BUILD_TYPE=Debug)")
  set(MI_DEBUG_INTERNAL ON)
  set(MI_DEBUG ON)
  list(APPEND mi_defines MI_DEBUG=2)   # invariant checking (mi_assert and mi_assert_internal)
endif()

# Debug-only lock diagnostics (#167). Keep this source out of release libraries
# entirely; src/static.c includes it under the equivalent preprocessor guard.
if(MI_DEBUG_FULL)
  list(APPEND mi_sources src/diagnostic.c)
endif()

# pthread_atfork fork-safety handlers (#270, Bun parity P5). POSIX-only: the file body
# is wrapped in `#if !defined(_WIN32) && !defined(__wasi__)` and src/static.c includes it
# under the same guard, so keep it out of the Windows source list entirely.
if(NOT WIN32)
  list(APPEND mi_sources src/fork.c)
endif()

# NOTE: `!` is NOT negation in CMake -- `!MI_GUARDED` parses as a variable name, and an
# undefined variable is false, so the original `if(MI_DEBUG AND !MI_GUARDED)` was ALWAYS
# false. And `set(MI_GUARDED=ON)` created a variable literally named `MI_GUARDED=ON`.
# Either defect alone disabled this block, so MI_GUARDED was never on despite being
# documented as default-on in debug builds (issue #116).
if(MI_DEBUG AND NOT MI_GUARDED)
  message(STATUS "Enable MI_GUARDED (since MI_DEBUG=ON)")
  set(MI_GUARDED ON)
endif()
if(MI_GUARDED)
  message(STATUS "Compile guard pages behind certain object allocations (MI_GUARDED=ON)")
  list(APPEND mi_defines MI_GUARDED=1)
endif()

if(MI_NO_PADDING)
  message(STATUS "Suppress any padding of heap blocks (MI_NO_PADDING=ON)")
  list(APPEND mi_defines MI_PADDING=0)
else()
  if(MI_PADDING)
    message(STATUS "Enable explicit padding of heap blocks (MI_PADDING=ON)")
    list(APPEND mi_defines MI_PADDING=1)
  endif()
endif()

if(MI_DEBUG OR MI_SECURE)
  set(MI_FREE_IS_CHECKED ON)
endif()
if(MI_FREE_IS_CHECKED)
  message(STATUS "Check validity of pointers passed to 'mi_free' (MI_FREE_IS_CHECKED=ON)")
  list(APPEND mi_defines MI_FREE_IS_CHECKED=1)
elseif(MI_OVERRIDE)
  message(STATUS "Check validity of pointers passed to 'mi_free' only in the dynamic link library (MI_OVERRIDE=ON, MI_FREE_IS_CHECKED=OFF)")
  list(APPEND mi_cflags_dynamic -DMI_FREE_IS_CHECKED=1)
endif()

if(NOT MI_OPT_FREE_SMALL AND NOT MI_DEBUG AND NOT MI_GUARDED AND NOT MI_SECURE) 
set(MI_OPT_FREE_SMALL ON)
endif()
if(MI_OPT_FREE_SMALL)
    if(MI_GUARDED OR MI_SECURE)
    message(STATUS "Cannot enable MI_OPT_FREE_SMALL in secure or guarded mode (MI_OPT_FREE_SMALL=OFF)")
  else()
    message(STATUS "Optimize `mi_free_small` to not need a pagemap lookup (MI_OPT_FREE_SMALL=ON)")
    list(APPEND mi_defines MI_OPT_FREE_SMALL=1)
  endif()
endif()

if(MI_XMALLOC)
  message(STATUS "Enable abort() calls on memory allocation failure (MI_XMALLOC=ON)")
  list(APPEND mi_defines MI_XMALLOC=1)
endif()

if(MI_SHOW_ERRORS)
  message(STATUS "Enable printing of error and warning messages by default (MI_SHOW_ERRORS=ON)")
  list(APPEND mi_defines MI_SHOW_ERRORS=1)
endif()

if(MI_PPROF)
  message(STATUS "Enable the allocation sampling profiler (MI_PPROF=ON)")
  list(APPEND mi_defines MI_PPROF=1)
  if(NOT WIN32)
    list(APPEND mi_cflags -fno-omit-frame-pointer)
  endif()
else()
  list(APPEND mi_defines MI_PPROF=0)
endif()

if(MI_DEBUG_TSAN)
  if(CMAKE_C_COMPILER_ID MATCHES "Clang")
    message(STATUS "Build with thread sanitizer (MI_DEBUG_TSAN=ON)")
    list(APPEND mi_defines MI_TSAN=1)
    list(APPEND mi_cflags -fsanitize=thread -g -O1)
    list(APPEND mi_libraries -fsanitize=thread)
  else()
    message(WARNING "Can only use thread sanitizer with clang (MI_DEBUG_TSAN=ON but ignored)")
  endif()
endif()

if(MI_DEBUG_UBSAN)
  if(CMAKE_BUILD_TYPE MATCHES "Debug")
    if(MI_USE_CXX)
      if(CMAKE_CXX_COMPILER_ID MATCHES "Clang")
        message(STATUS "Build with undefined-behavior sanitizer (MI_DEBUG_UBSAN=ON)")
        list(APPEND mi_defines MI_UBSAN=1)
        list(APPEND mi_cflags -fsanitize=undefined -g -fno-sanitize-recover=undefined)
        list(APPEND mi_libraries -fsanitize=undefined)
      else()
        message(WARNING "Can only use undefined-behavior sanitizer with clang++ (MI_DEBUG_UBSAN=ON but ignored)")
      endif()
    else()
      message(WARNING "Can only use undefined-behavior sanitizer with a C++ build (MI_USE_CXX=ON)")
    endif()
  else()
    message(WARNING "Can only use undefined-behavior sanitizer with a debug build (CMAKE_BUILD_TYPE=${CMAKE_BUILD_TYPE})")
  endif()
endif()

if(CMAKE_SYSTEM_NAME MATCHES "Linux|Android")
  if(MI_NO_THP)
    message(STATUS "Disable transparent huge pages support (MI_NO_THP=ON)")
    list(APPEND mi_defines MI_DEFAULT_ALLOW_THP=0)
  endif()
endif()

if(MI_LIBC_MUSL)
  message(STATUS "Assume using musl libc (MI_LIBC_MUSL=ON)")
  list(APPEND mi_defines MI_LIBC_MUSL=1)
endif()

if(MI_TLS_MODEL_PTHREADS)
  message(STATUS "Use TLS model based on pthreads (MI_TLS_MODEL_PTHREADS=ON)")
  list(APPEND mi_defines MI_TLS_MODEL_PTHREADS=1)
elseif(MI_TLS_MODEL_FIXED)
  message(STATUS "Use TLS model based on fixed thread local slots (MI_TLS_MODEL_FIXED=ON)")
  message(STATUS "Note: fixed TLS slots do not allow multiple mimalloc instances in a single process.")
  list(APPEND mi_defines MI_TLS_MODEL_FIXED=1)
elseif(MI_TLS_MODEL_LOCAL)
  message(STATUS "Use TLS model based on thread local (MI_TLS_MODEL_LOCAL=ON)")
  if(APPLE)
  message(WARNING "On macOS the MI_TLS_MODEL_LOCAL does not work reliably when dynamically overriding mimalloc. MI_TLS_MODEL_PTHREADS is recommended instead.")
  endif()
  list(APPEND mi_defines MI_TLS_MODEL_LOCAL=1)
endif()

if(MI_TLS_RECURSE_GUARD)
  message(STATUS "Use TLS recursion guard (for MI_TLS_MODEL_LOCAL only)")
  list(APPEND mi_defines MI_TLS_RECURSE_GUARD=1)
endif()

# MI_MINGW_UCRT64 may also be set by the caller (a cache entry or a toolchain file):
# $ENV{MSYSTEM} is set only by an MSYS2 shell, so it cannot detect a mingw-w64 UCRT
# toolchain used from anywhere else -- notably a cross build on Linux, where mingw-w64 13
# configured `--with-default-msvcrt=ucrt` is UCRT while MSYSTEM is unset (#277 phase C).
if(MI_MINGW_UCRT64 OR "$ENV{MSYSTEM}" STREQUAL "UCRT64")
  if(MI_MINGW_UCRT64)
    message(STATUS "Using the mingw-w64 UCRT runtime (MI_MINGW_UCRT64=ON)")
  else()
    message(STATUS "Detected MSYS2 UCRT64 environment")
  endif()
  set(MI_MINGW_UCRT64 "ON")
  list(APPEND mi_defines MI_MINGW_UCRT64=1)
elseif("$ENV{MSYSTEM}" STREQUAL "MSYS")
  message(STATUS "Detected MSYS2 MSYS/Cygwin environment")
  set(MI_CYGWIN "ON")
endif()

if(MI_WIN_DIRECT_TLS)
  message(STATUS "Use only direct TLS slots on Windows to avoid extra tests in the malloc fast path -- this only works if the program uses less than 64 TlsAlloc'd slots in total! (MI_WIN_USE_ONLY_DIRECT_TLS=ON)")
  list(APPEND mi_defines MI_WIN_DIRECT_TLS=1)
endif()

if(MI_WIN_INIT_USE_RAW_DLLMAIN)
  message(STATUS "Use raw Dll main entry on Windows to initialize and finalize mimalloc (MI_WIN_INIT_USE_RAW_DLLMAIN=ON)")
  message(STATUS "Note: this can lead to link errors if other libraries also try to define a raw Dll main entry point")
  list(APPEND mi_defines MI_WIN_INIT_USE_RAW_DLLMAIN=1)
elseif(MI_WIN_INIT_USE_TLS_DLLMAIN)
  message(STATUS "Use Dll main entry with TLS entries on Windows to initialize and finalize mimalloc (MI_WIN_INIT_USE_TLS_DLLMAIN=ON)")
  list(APPEND mi_defines MI_WIN_INIT_USE_TLS_DLLMAIN=1)
elseif(MI_WIN_USE_FLS)
  message(STATUS "Deprecated: Use the Fiber API to detect thread termination (deprecated) (MI_WIN_USE_FLS=ON)")
  list(APPEND mi_defines MI_WIN_INIT_USE_FLS=1)
else()
  # default uses CRT with TLS entries for initialization and finalization
  # list(APPEND mi_defines MI_WIN_INIT_USE_CRT_TLS=1)
endif()

if(MI_ARCH MATCHES "riscv")
  CHECK_INCLUDE_FILES("asm/hwprobe.h" MI_HAS_ASM_HWPROBEH)
  if (MI_HAS_ASM_HWPROBEH)
    list(APPEND mi_defines MI_HAS_ASM_HWPROBEH=1)
  endif()
  CHECK_INCLUDE_FILES("sys/hwprobe.h" MI_HAS_SYS_HWPROBEH)
  if (MI_HAS_SYS_HWPROBEH)
    list(APPEND mi_defines MI_HAS_SYS_HWPROBEH=1)
  endif()
endif()

# On Haiku use `-DCMAKE_INSTALL_PREFIX` instead, issue #788
# if(CMAKE_SYSTEM_NAME MATCHES "Haiku")
#   SET(CMAKE_INSTALL_LIBDIR ~/config/non-packaged/lib)
#   SET(CMAKE_INSTALL_INCLUDEDIR ~/config/non-packaged/headers)
# endif()

# Compiler flags
if(CMAKE_C_COMPILER_ID MATCHES "AppleClang|Clang|GNU" AND NOT MI_CLANG_CL)
  list(APPEND mi_cflags -Wno-unknown-pragmas -fvisibility=hidden)
  if(NOT MI_USE_CXX)
    list(APPEND mi_cflags -Wstrict-prototypes)
  endif()
  if(CMAKE_C_COMPILER_ID MATCHES "AppleClang|Clang")
    list(APPEND mi_cflags -Wno-static-in-inline)
  endif()
endif()

if(CMAKE_C_COMPILER_ID MATCHES "Intel")
  list(APPEND mi_cflags -fvisibility=hidden)
endif()

if(CMAKE_C_COMPILER_ID MATCHES "AppleClang|Clang|GNU|Intel" AND NOT CMAKE_SYSTEM_NAME MATCHES "Haiku" AND NOT MI_CLANG_CL)
  if(MI_LOCAL_DYNAMIC_TLS)
    list(APPEND mi_cflags -ftls-model=local-dynamic)
  else()
    if(MI_LIBC_MUSL)
      # with musl we use local-dynamic for the static build, see issue #644
      list(APPEND mi_cflags_static  -ftls-model=local-dynamic)
      list(APPEND mi_cflags_dynamic -ftls-model=initial-exec)
      message(STATUS "Use local dynamic TLS for the static build (since MI_LIBC_MUSL=ON)")
    else()
      list(APPEND mi_cflags -ftls-model=initial-exec)
    endif()
  endif()
endif()

if(CMAKE_C_COMPILER_ID MATCHES "AppleClang|Clang|GNU|Intel")
  if(MI_OVERRIDE)
    list(APPEND mi_cflags -fno-builtin-malloc)
  endif()
endif()

# Compiler and architecture specific flags
if(CMAKE_C_COMPILER_ID MATCHES "AppleClang|Clang|GNU|Intel" AND NOT CMAKE_SYSTEM_NAME MATCHES "Haiku")
  if(MI_OPT_ARCH)
    if(APPLE AND CMAKE_C_COMPILER_ID MATCHES "AppleClang|Clang" AND CMAKE_OSX_ARCHITECTURES)   # to support multi-arch binaries (#999)
      if("arm64" IN_LIST CMAKE_OSX_ARCHITECTURES)
        list(APPEND MI_OPT_ARCH_FLAGS "-Xarch_arm64;-march=armv8.1-a")
      endif()
      if("x86_64" IN_LIST CMAKE_OSX_ARCHITECTURES)
        list(APPEND MI_OPT_ARCH_FLAGS "-Xarch_x86_64;-march=haswell;-Xarch_x86_64;-mavx2")
      endif()
    elseif(MI_ARCH STREQUAL "x64")
      set(MI_OPT_ARCH_FLAGS "-march=haswell;-mavx2")    # fast bit scan (since 2013)
    elseif(MI_ARCH STREQUAL "arm64")
      set(MI_OPT_ARCH_FLAGS "-march=armv8.1-a")         # fast atomics (since 2016)
    endif()
  endif()
endif()

if (MSVC AND MSVC_VERSION GREATER_EQUAL 1914) # vs2017+
  list(APPEND mi_cflags /Zc:__cplusplus)
  if(MI_OPT_ARCH AND NOT MI_CLANG_CL)
    if(MI_ARCH STREQUAL "x64")
      set(MI_OPT_ARCH_FLAGS "/arch:AVX2")
    elseif(MI_ARCH STREQUAL "arm64")
      set(MI_OPT_ARCH_FLAGS "/arch:armv8.1")           # fast atomics (LSE)
    endif()
  endif()
endif()

if(MINGW)
  add_definitions(-D_WIN32_WINNT=0x600)                # issue #976
endif()

if(MI_OPT_ARCH_FLAGS)
  list(APPEND mi_cflags ${MI_OPT_ARCH_FLAGS})
  message(STATUS "Architecture specific optimization is enabled (with ${MI_OPT_ARCH_FLAGS}) (MI_OPT_ARCH=ON)")
  if (MI_OPT_SIMD)
    list(APPEND mi_defines "MI_OPT_SIMD=1")
    message(STATUS "SIMD instructions are enabled (MI_OPT_SIMD=ON)")
  endif()
elseif(MI_OPT_SIMD)
  message(STATUS "SIMD instructions are not enabled (either MI_OPT_ARCH=OFF or this architecture has no SIMD support)")
endif()

# extra needed libraries

# we prefer -l<lib> test over `find_library` as sometimes core libraries
# like `libatomic` are not on the system path (see issue #898)
function(find_link_library libname outlibname)
  check_linker_flag(C "-l${libname}" mi_has_lib${libname})
  if (mi_has_lib${libname})
    message(VERBOSE "link library: -l${libname}")
    set(${outlibname} ${libname} PARENT_SCOPE)
  else()
    find_library(MI_LIBPATH_${libname} ${libname})
    if (MI_LIBPATH_${libname})
      message(VERBOSE "link library ${libname} at ${MI_LIBPATH_${libname}}")
      set(${outlibname} ${MI_LIBPATH_${libname}} PARENT_SCOPE)
    else()
      message(VERBOSE "link library not found: ${libname}")
      set(${outlibname} "" PARENT_SCOPE)
    endif()
  endif()
endfunction()

if(WIN32)
  # `synchronization` is `WaitOnAddress`/`WakeByAddressSingle` (src/scavenger.c, #272). Needed for
  # EVERY Windows toolchain: src/scavenger.c's `#pragma comment(lib, "synchronization")` reaches
  # only MSVC/clang-cl, so MinGW (ld/lld) needs the explicit `-lsynchronization` this adds.
  list(APPEND mi_libraries psapi shell32 user32 advapi32 bcrypt synchronization)
else()
  find_link_library("pthread" MI_LIB_PTHREAD)
  if(MI_LIB_PTHREAD)
    list(APPEND mi_libraries "${MI_LIB_PTHREAD}")
  endif()
  find_link_library("rt" MI_LIB_RT)
  if(MI_LIB_RT)
    list(APPEND mi_libraries "${MI_LIB_RT}")
  endif()
  find_link_library("atomic" MI_LIB_ATOMIC)
  if(MI_LIB_ATOMIC)
    list(APPEND mi_libraries "${MI_LIB_ATOMIC}")
  endif()
endif()

# set language for source files now
if(MI_USE_CXX)
  set_source_files_properties(${mi_sources} PROPERTIES LANGUAGE CXX )
  set_source_files_properties(src/static.c test/test-api.c test/test-api-fill.c test/test-stress.c test/test-stress-heaps.c test/test-stress-subprocs.c test/test-lock-reentrancy.c test/test-tls-controls.c PROPERTIES LANGUAGE CXX )
endif()


# -----------------------------------------------------------------------------
# Install and output names
# -----------------------------------------------------------------------------

# dynamic/shared library and symlinks always go to /usr/local/lib equivalent
# we use ${CMAKE_INSTALL_BINDIR} and ${CMAKE_INSTALL_LIBDIR}.

# static libraries and object files, includes, and cmake config files
# are either installed at top level, or use versioned directories for side-by-side installation (default)
if (MI_INSTALL_TOPLEVEL)
  set(mi_install_objdir     "${CMAKE_INSTALL_LIBDIR}")
  set(mi_install_incdir     "${CMAKE_INSTALL_INCLUDEDIR}")
  set(mi_install_cmakedir   "${CMAKE_INSTALL_LIBDIR}/cmake/mimalloc")
else()
  set(mi_install_objdir     "${CMAKE_INSTALL_LIBDIR}/mimalloc-${mi_version}")       # for static library and object files
  set(mi_install_incdir     "${CMAKE_INSTALL_INCLUDEDIR}/mimalloc-${mi_version}")   # for includes
  set(mi_install_cmakedir   "${CMAKE_INSTALL_LIBDIR}/cmake/mimalloc-${mi_version}") # for cmake package info
endif()

set(mi_libname "mimalloc")
if(MI_SECURE)
  set(mi_libname "${mi_libname}-secure")
endif()
if(MI_TRACK_VALGRIND)
  set(mi_libname "${mi_libname}-valgrind")
endif()
if(MI_TRACK_ASAN)
  set(mi_libname "${mi_libname}-asan")
endif()
string(TOLOWER "${CMAKE_BUILD_TYPE}" CMAKE_BUILD_TYPE_LC)
list(APPEND mi_defines "MI_CMAKE_BUILD_TYPE=${CMAKE_BUILD_TYPE_LC}")  #todo: multi-config project needs $<CONFIG> ?
if(CMAKE_BUILD_TYPE_LC MATCHES "^(release|relwithdebinfo|minsizerel|none)$")
  list(APPEND mi_defines MI_BUILD_RELEASE)
else()
  set(mi_libname "${mi_libname}-${CMAKE_BUILD_TYPE_LC}") #append build type (e.g. -debug) if not a release version
endif()
string(REGEX REPLACE "^mimalloc[-]?" "" mi_libsuffix "${mi_libname}")


if(MI_BUILD_SHARED)
  list(APPEND mi_build_targets "shared")
endif()
if(MI_BUILD_STATIC)
  list(APPEND mi_build_targets "static")
endif()
if(MI_BUILD_OBJECT)
  list(APPEND mi_build_targets "object")
endif()
if(MI_BUILD_TESTS)
  list(APPEND mi_build_targets "tests")
endif()

message(STATUS "")
message(STATUS "Library name     : ${mi_libname}")
message(STATUS "Version          : ${mi_version}.${mi_version_patch}")
message(STATUS "Build type       : ${CMAKE_BUILD_TYPE_LC}")
if(MI_USE_CXX)
  message(STATUS "C++ Compiler     : ${CMAKE_CXX_COMPILER}")
else()
  message(STATUS "C Compiler       : ${CMAKE_C_COMPILER}")
endif()
message(STATUS "Compiler flags   : ${mi_cflags}")
message(STATUS "Compiler defines : ${mi_defines}")
message(STATUS "Link libraries   : ${mi_libraries}")
message(STATUS "Build targets    : ${mi_build_targets}")
message(STATUS "")

# -----------------------------------------------------------------------------
# Main targets
# -----------------------------------------------------------------------------

# shared library
if(MI_BUILD_SHARED)
  add_library(mimalloc SHARED ${mi_sources})
  set_target_properties(mimalloc PROPERTIES VERSION ${mi_version} SOVERSION ${mi_version_major} OUTPUT_NAME ${mi_libname} )
  target_compile_definitions(mimalloc PRIVATE ${mi_defines} MI_SHARED_LIB MI_SHARED_LIB_EXPORT)
  target_compile_options(mimalloc PRIVATE ${mi_cflags} ${mi_cflags_dynamic})
  target_link_libraries(mimalloc PRIVATE ${mi_libraries})
  target_include_directories(mimalloc PUBLIC
      $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
      $<INSTALL_INTERFACE:${mi_install_incdir}>
  )
  install(TARGETS mimalloc EXPORT mimalloc ARCHIVE DESTINATION ${CMAKE_INSTALL_LIBDIR} RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR} LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR})
  install(EXPORT mimalloc DESTINATION ${mi_install_cmakedir})

  if(WIN32 AND NOT MINGW)
    # On windows, the import library name for the dll would clash with the static mimalloc.lib library
    # so we postfix the dll import library with `.dll.lib` (and also the .pdb debug file)
    set_property(TARGET mimalloc PROPERTY ARCHIVE_OUTPUT_NAME "${mi_libname}.dll" )
    install(FILES "$<TARGET_FILE_DIR:mimalloc>/${mi_libname}.dll.lib" DESTINATION ${CMAKE_INSTALL_LIBDIR}/mimalloc-${mi_version})
    set_property(TARGET mimalloc PROPERTY PDB_NAME "${mi_libname}.dll")
    # don't try to install the pdb since it may not be generated depending on the configuration
    # install(FILES "$<TARGET_FILE_DIR:mimalloc>/${mi_libname}.dll.pdb" DESTINATION ${CMAKE_INSTALL_LIBDIR})
  endif()
  if(WIN32 AND MI_WIN_REDIRECT)
    if(MINGW)
      set_property(TARGET mimalloc PROPERTY PREFIX "")
    endif()
    # On windows, link and copy the mimalloc redirection dll too.
    if(CMAKE_GENERATOR_PLATFORM STREQUAL "arm64ec")
      set(MIMALLOC_REDIRECT_SUFFIX "-arm64ec")
      set(MINJECT_SUFFIX "")
    elseif(MI_ARCH STREQUAL "x64")
      set(MIMALLOC_REDIRECT_SUFFIX "")
      set(MINJECT_SUFFIX "")
      if(CMAKE_SYSTEM_PROCESSOR STREQUAL "ARM64")
        message(STATUS "Note: x64 code emulated on Windows for arm64 should use an arm64ec build of 'mimalloc.dll'")
        message(STATUS "      together with 'mimalloc-redirect-arm64ec.dll'. See the 'bin\\readme.md' for more information.")
      endif()
    elseif(MI_ARCH STREQUAL "x86")
      set(MIMALLOC_REDIRECT_SUFFIX "32")
      set(MINJECT_SUFFIX "32")
    else()
      set(MIMALLOC_REDIRECT_SUFFIX "-${MI_ARCH}")  # -arm64 etc.
      set(MINJECT_SUFFIX "-${MI_ARCH}")
    endif()

    if(MINGW AND MI_MINGW_REDIRECT_FIRST)
      # `mimalloc-redirect.dll` only redirects if its DllMain runs BEFORE the C runtime's:
      # it reads `ucrtbase.dll`'s LDR_DATA_TABLE_ENTRY.Flags and bails out with
      #   "mimalloc-redirect.dll seems to be initialized after ucrtbase.dll"
      # as soon as LDRP_PROCESS_ATTACH_CALLED (0x00080000) is set there. The loader
      # initialises a module's dependencies in THAT module's import-descriptor order, so
      # the redirect module has to precede `api-ms-win-crt-*` in *this DLL's* import
      # table -- the executable importing `mimalloc.dll` first is necessary but not
      # sufficient. MSVC gets this for free (explicit libs are emitted before
      # /DEFAULTLIB ones); `ld` sorts `.idata$2` by the input file path as spelled on the
      # link line, and CMake spells this one as an absolute source path, which wins or
      # loses against the absolute sysroot path purely by where the checkout lives.
      # A copy spelled `./…` (0x2e) sorts ahead of every absolute path (0x2f).
      #
      # ON by default: this is the only layout in which the override works at all, and
      # the load-time crash it used to expose (GCC emulated TLS re-entering the redirected
      # `malloc`) is fixed in `src/threadlocal.c` and `src/options.c`. #277.
      configure_file(
        "${CMAKE_CURRENT_SOURCE_DIR}/bin/mimalloc-redirect${MIMALLOC_REDIRECT_SUFFIX}.lib"
        "${CMAKE_CURRENT_BINARY_DIR}/mimalloc-redirect${MIMALLOC_REDIRECT_SUFFIX}.lib"
        COPYONLY)
      # The link runs from CMAKE_BINARY_DIR, not from this project's binary dir, so under
      # add_subdirectory()/FetchContent a bare `./` would name the wrong directory. Spell
      # the relative path from the top of the build tree (empty for a standalone build,
      # which gives plain `-L.`).
      file(RELATIVE_PATH _mi_redirect_ldir "${CMAKE_BINARY_DIR}" "${CMAKE_CURRENT_BINARY_DIR}")
      set(_mi_redirect_ldir "-L./${_mi_redirect_ldir}")
      target_link_libraries(mimalloc PRIVATE
        "${_mi_redirect_ldir}" "-l:mimalloc-redirect${MIMALLOC_REDIRECT_SUFFIX}.lib")  # the DLL import library
    else()
      target_link_libraries(mimalloc PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/bin/mimalloc-redirect${MIMALLOC_REDIRECT_SUFFIX}.lib)  # the DLL import library
    endif()
    add_custom_command(TARGET mimalloc POST_BUILD
      COMMAND "${CMAKE_COMMAND}" -E copy "${CMAKE_CURRENT_SOURCE_DIR}/bin/mimalloc-redirect${MIMALLOC_REDIRECT_SUFFIX}.dll" $<TARGET_FILE_DIR:mimalloc>
      COMMENT "Copy mimalloc-redirect${MIMALLOC_REDIRECT_SUFFIX}.dll to output directory")
    install(FILES "$<TARGET_FILE_DIR:mimalloc>/mimalloc-redirect${MIMALLOC_REDIRECT_SUFFIX}.dll" DESTINATION ${CMAKE_INSTALL_BINDIR})
  endif()
endif()


# static library
if (MI_BUILD_STATIC)
  add_library(mimalloc-static STATIC ${mi_sources})
  set_property(TARGET mimalloc-static PROPERTY OUTPUT_NAME ${mi_libname})
  set_property(TARGET mimalloc-static PROPERTY POSITION_INDEPENDENT_CODE ON)
  target_compile_definitions(mimalloc-static PRIVATE ${mi_defines} MI_STATIC_LIB)
  target_compile_options(mimalloc-static PRIVATE ${mi_cflags} ${mi_cflags_static})
  target_link_libraries(mimalloc-static PRIVATE ${mi_libraries})
  target_include_directories(mimalloc-static PUBLIC
      $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
      $<INSTALL_INTERFACE:${mi_install_incdir}>
  )
  install(TARGETS mimalloc-static EXPORT mimalloc DESTINATION ${mi_install_objdir} LIBRARY)
  install(EXPORT mimalloc DESTINATION ${mi_install_cmakedir})
endif()

# install include files
install(FILES include/mimalloc.h DESTINATION ${mi_install_incdir})
install(FILES include/mimalloc-override.h DESTINATION ${mi_install_incdir})
install(FILES include/mimalloc-new-delete.h DESTINATION ${mi_install_incdir})
install(FILES include/mimalloc-stats.h DESTINATION ${mi_install_incdir})
install(FILES include/mimalloc/profile.h DESTINATION ${mi_install_incdir}/mimalloc)
install(FILES include/mimalloc/memory-events.h DESTINATION ${mi_install_incdir}/mimalloc)
install(FILES include/mimalloc/dhat.h DESTINATION ${mi_install_incdir}/mimalloc)
install(FILES cmake/mimalloc-config.cmake DESTINATION ${mi_install_cmakedir})
install(FILES cmake/mimalloc-config-version.cmake DESTINATION ${mi_install_cmakedir})


# single object file for more predictable static overriding
if (MI_BUILD_OBJECT)
  add_library(mimalloc-obj OBJECT src/static.c)
  set_property(TARGET mimalloc-obj PROPERTY POSITION_INDEPENDENT_CODE ON)
  # set_property(TARGET mimalloc-obj APPEND PROPERTY OBJECT_DEPENDS ${mi_sources})
  target_compile_definitions(mimalloc-obj PRIVATE ${mi_defines})
  target_compile_options(mimalloc-obj PRIVATE ${mi_cflags} ${mi_cflags_static})
  target_include_directories(mimalloc-obj PUBLIC
      $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
      $<INSTALL_INTERFACE:${mi_install_incdir}>
  )

  # Copy the generated object file (`static.o`) to the output directory (as `mimalloc.o`)
  if(CMAKE_GENERATOR MATCHES "^Visual Studio.*$")
    set(mimalloc-obj-static "${CMAKE_CURRENT_BINARY_DIR}/mimalloc-obj.dir/$<CONFIG>/static${CMAKE_C_OUTPUT_EXTENSION}")
  else()
    set(mimalloc-obj-static "${CMAKE_CURRENT_BINARY_DIR}/CMakeFiles/mimalloc-obj.dir/src/static.c${CMAKE_C_OUTPUT_EXTENSION}")
  endif()
  set(mimalloc-obj-out "${CMAKE_CURRENT_BINARY_DIR}/${mi_libname}${CMAKE_C_OUTPUT_EXTENSION}")
  add_custom_command(OUTPUT ${mimalloc-obj-out} DEPENDS mimalloc-obj mimalloc-static COMMAND "${CMAKE_COMMAND}" -E copy "${mimalloc-obj-static}" "${mimalloc-obj-out}")
  add_custom_target(mimalloc-obj-target ALL DEPENDS ${mimalloc-obj-out})


  # the following seems to lead to cmake warnings/errors on some systems, disable for now :-(
  # install(TARGETS mimalloc-obj EXPORT mimalloc DESTINATION ${mi_install_objdir})

  # the FILES expression can also be: $<TARGET_OBJECTS:mimalloc-obj>
  # but that fails cmake versions less than 3.10 so we leave it as is for now
  install(FILES ${mimalloc-obj-static}
          DESTINATION ${mi_install_objdir}
          RENAME ${mi_libname}${CMAKE_C_OUTPUT_EXTENSION} )
endif()


# pkg-config file support
set(mi_pc_libraries "")
foreach(item IN LISTS mi_libraries)
  if(item MATCHES " *[-].*")
    set(mi_pc_libraries "${mi_pc_libraries} ${item}")
  else()
    set(mi_pc_libraries "${mi_pc_libraries} -l${item}")
  endif()
endforeach()

include("cmake/JoinPaths.cmake")
join_paths(mi_pc_includedir "\${prefix}" "${CMAKE_INSTALL_INCLUDEDIR}")
join_paths(mi_pc_libdir "\${prefix}" "${CMAKE_INSTALL_LIBDIR}")

configure_file(mimalloc.pc.in mimalloc.pc @ONLY)
install(FILES "${CMAKE_CURRENT_BINARY_DIR}/mimalloc.pc"
        DESTINATION "${CMAKE_INSTALL_LIBDIR}/pkgconfig/")



# -----------------------------------------------------------------------------
# API surface testing
# -----------------------------------------------------------------------------

if (MI_BUILD_TESTS)
  enable_testing()

  # static link tests
  foreach(TEST_NAME api api-fill stress-heaps stress-subprocs stress)
    add_executable(mimalloc-test-${TEST_NAME} test/test-${TEST_NAME}.c)
    target_compile_definitions(mimalloc-test-${TEST_NAME} PRIVATE ${mi_defines})
    target_compile_options(mimalloc-test-${TEST_NAME} PRIVATE ${mi_cflags})
    target_include_directories(mimalloc-test-${TEST_NAME} PRIVATE include)
    if(MI_BUILD_STATIC AND NOT MI_DEBUG_TSAN)
      target_link_libraries(mimalloc-test-${TEST_NAME} PRIVATE mimalloc-static ${mi_libraries})
    elseif(MI_BUILD_SHARED)
      target_link_libraries(mimalloc-test-${TEST_NAME} PRIVATE mimalloc ${mi_libraries})
    else()
      message(STATUS "cannot build TSAN tests without MI_BUILD_SHARED being enabled")
    endif()
    if(MI_GUARDED)
    add_test(NAME test-${TEST_NAME} COMMAND ${CMAKE_COMMAND} -E env MIMALLOC_GUARDED_SAMPLE_RATE=1 $<TARGET_FILE:mimalloc-test-${TEST_NAME}>)
    elseif(TEST_NAME STREQUAL "stress-heaps")
    add_test(NAME test-${TEST_NAME} COMMAND ${CMAKE_COMMAND} -E env MIMALLOC_ARENA_EAGER_COMMIT=0 MIMALLOC_PAGE_COMMIT_ON_DEMAND=1 $<TARGET_FILE:mimalloc-test-${TEST_NAME}>)
    else()
    add_test(NAME test-${TEST_NAME} COMMAND mimalloc-test-${TEST_NAME})
    endif()
  endforeach()

  if(MI_PPROF)
    add_executable(mimalloc-test-profile test/test-profile.c)
    target_compile_definitions(mimalloc-test-profile PRIVATE ${mi_defines})
    target_compile_options(mimalloc-test-profile PRIVATE ${mi_cflags})
    target_include_directories(mimalloc-test-profile PRIVATE include)
    if(MI_BUILD_STATIC AND NOT MI_DEBUG_TSAN)
      target_link_libraries(mimalloc-test-profile PRIVATE mimalloc-static ${mi_libraries})
    else()
      target_link_libraries(mimalloc-test-profile PRIVATE mimalloc ${mi_libraries})
    endif()
    add_test(NAME test-profile COMMAND mimalloc-test-profile)
    add_test(NAME test-profile-accum COMMAND mimalloc-test-profile)
    set_tests_properties(test-profile-accum PROPERTIES ENVIRONMENT "MIMALLOC_PROF_ACCUM=1")
    add_test(NAME test-profile-auto COMMAND mimalloc-test-profile)
    set_tests_properties(test-profile-auto PROPERTIES ENVIRONMENT "MIMALLOC_PROF=1")

    # Adversarial concurrency coverage for the v3 engine (issue #43): thread
    # bootstrap race, cross-thread free, snapshot stability under mutation.
    add_executable(mimalloc-test-profile-race test/test-profile-race.c)
    target_compile_definitions(mimalloc-test-profile-race PRIVATE ${mi_defines})
    target_compile_options(mimalloc-test-profile-race PRIVATE ${mi_cflags})
    target_include_directories(mimalloc-test-profile-race PRIVATE include)
    if(MI_BUILD_STATIC AND NOT MI_DEBUG_TSAN)
      target_link_libraries(mimalloc-test-profile-race PRIVATE mimalloc-static ${mi_libraries})
      # issue #272 (P7b): scenario 5's purged-block assertions need `_mi_ptr_page` /
      # `mi_page_block_is_purged`, which a shared build hides. With the static library
      # they are available; without it the scenario still runs (it is a concurrency test
      # first) minus those two assertions -- see the test source.
      # MI_TEST_LINKS_STATIC turns scenario 5's purged-block assertions on; MI_STATIC_LIB is
      # what makes the internal declarations they use link on MSVC (see test-purge-holes above).
      target_compile_definitions(mimalloc-test-profile-race PRIVATE MI_TEST_LINKS_STATIC=1 MI_STATIC_LIB)
    else()
      target_link_libraries(mimalloc-test-profile-race PRIVATE mimalloc ${mi_libraries})
    endif()
    if(NOT WIN32)
      find_package(Threads REQUIRED)
      target_link_libraries(mimalloc-test-profile-race PRIVATE Threads::Threads)
    endif()
    add_test(NAME test-profile-race COMMAND mimalloc-test-profile-race)
    set_tests_properties(test-profile-race PROPERTIES TIMEOUT 300)
    # issue #272: the same source with the purge delay forced to its minimum, so the
    # background scavenger is continuously decommitting arena slices underneath scenario 4's
    # mi_prof_visit / mi_prof_snapshot_new walks (and sweeping the parked worker threads).
    add_test(NAME test-profile-race-scavenger COMMAND mimalloc-test-profile-race)
    set_tests_properties(test-profile-race-scavenger PROPERTIES TIMEOUT 300
                         ENVIRONMENT "MIMALLOC_PURGE_DELAY=1;MIMALLOC_SCAVENGER=1")
  endif()

  # Memory + counter regression gate (issue #63). Emits JSON for CI baseline
  # comparison; MI_BENCH_INJECT_LEAK is a build-time-only positive control.
  add_executable(mimalloc-test-memory-gate test/test-memory-gate.c)
  target_compile_definitions(mimalloc-test-memory-gate PRIVATE ${mi_defines})
  if(MI_BENCH_INJECT_LEAK)
    target_compile_definitions(mimalloc-test-memory-gate PRIVATE MI_BENCH_INJECT_LEAK=${MI_BENCH_INJECT_LEAK})
  endif()
  target_compile_options(mimalloc-test-memory-gate PRIVATE ${mi_cflags})
  target_include_directories(mimalloc-test-memory-gate PRIVATE include)
  if(MI_BUILD_STATIC AND NOT MI_DEBUG_TSAN)
    target_link_libraries(mimalloc-test-memory-gate PRIVATE mimalloc-static ${mi_libraries})
  else()
    target_link_libraries(mimalloc-test-memory-gate PRIVATE mimalloc ${mi_libraries})
  endif()
  if(NOT WIN32)
    find_package(Threads REQUIRED)
    target_link_libraries(mimalloc-test-memory-gate PRIVATE Threads::Threads)
  endif()
  add_test(NAME test-memory-gate COMMAND mimalloc-test-memory-gate)

  # Subproc lifecycle leak (issue #128 B1). mi_subproc_destroy skipped mi_heap_free for
  # the subproc's own heap_main, because _mi_is_heap_main resolves via heap->subproc and
  # is therefore true for it -- leaking one mi_heap_t per destroy (~7.3 KB measured).
  add_executable(mimalloc-test-subproc-lifecycle test/test-subproc-lifecycle.c)
  target_compile_definitions(mimalloc-test-subproc-lifecycle PRIVATE ${mi_defines})
  target_compile_options(mimalloc-test-subproc-lifecycle PRIVATE ${mi_cflags})
  target_include_directories(mimalloc-test-subproc-lifecycle PRIVATE include)
  if(MI_BUILD_STATIC AND NOT MI_DEBUG_TSAN)
    target_link_libraries(mimalloc-test-subproc-lifecycle PRIVATE mimalloc-static ${mi_libraries})
  else()
    target_link_libraries(mimalloc-test-subproc-lifecycle PRIVATE mimalloc ${mi_libraries})
  endif()
  add_test(NAME test-subproc-lifecycle COMMAND mimalloc-test-subproc-lifecycle)
  set_tests_properties(test-subproc-lifecycle PROPERTIES TIMEOUT 600)

  # Commit-fail fault injection (issue #271, Bun parity P6): imported from
  # oven-sh/mimalloc @ 942b8342, MIT. Needs the MI_DEBUG-only mi_debug_fail_os_commit_after
  # hook (src/os.c), so it only builds in a debug configuration -- mirrors Bun's own
  # CMakeLists gating ("MI_DEBUG_FULL OR CMAKE_BUILD_TYPE MATCHES Debug"), simplified to
  # the MI_DEBUG cmake variable this tree already resolves that from (see MI_DEBUG=0/1/2/3
  # above). Exercises the arena_pages->pages-bit / page-map cleanup on a commit failure
  # during mi_arenas_page_alloc_fresh; this tree's fresh-page-alloc path at the 6def7be9
  # pin already sets the bit only after a successful commit (see the port's provenance
  # note in MIMALLOC_FORKS.md), so a pass here is expected -- kept as regression coverage
  # per the issue's "if everything passes, still import the test" instruction.
  if(MI_DEBUG)
    add_executable(mimalloc-test-commit-fail test/test-commit-fail.c)
    target_compile_definitions(mimalloc-test-commit-fail PRIVATE ${mi_defines})
    target_compile_options(mimalloc-test-commit-fail PRIVATE ${mi_cflags})
    target_include_directories(mimalloc-test-commit-fail PRIVATE include)
    if(MI_BUILD_STATIC AND NOT MI_DEBUG_TSAN)
      target_link_libraries(mimalloc-test-commit-fail PRIVATE mimalloc-static ${mi_libraries})
    else()
      target_link_libraries(mimalloc-test-commit-fail PRIVATE mimalloc ${mi_libraries})
    endif()
    add_test(NAME test-commit-fail COMMAND mimalloc-test-commit-fail)
    set_tests_properties(test-commit-fail PROPERTIES TIMEOUT 120)
  endif()

  # Bun's heap delete/destroy teardown regression corpus (issue #271, Bun parity P6):
  # imported from oven-sh/mimalloc @ 942b8342, MIT, verbatim except for provenance headers
  # and, until #272 (Bun parity P7a) landed the background scavenger and mi_on_thread_idle_*,
  # an `#if 0 // Phase 7` around test-heap-teardown.c's `parked` case -- now enabled.
  # test-theap-sentinel.c is deliberately NOT imported: it white-box-reproduces a
  # `(mi_theap_t*)1` sentinel-reservation bug in Bun's `mi_heap_init_theap` (heap.c) that
  # only exists because Bun reserves the thread-local slot with a sentinel before creating
  # the theap; this tree's `mi_heap_init_theap` (src/heap.c) never writes a sentinel -- it
  # sets the slot to the real theap pointer exactly once, after `_mi_theap_create` succeeds
  # -- so there is no equivalent bug to regress-test, and the test needs internal symbols
  # (`_mi_theap_default_safe`) this fork's differently-shaped internals do not have. See the
  # PR description's hunk-classification table.
  # Bun's own CMakeLists (942b8342) registers this corpus with no explicit ctest TIMEOUT;
  # bounded here instead so a still-open race (or the intentionally-unwired
  # mi_debug_stall_in_heap_delete_claim hook, pre-protocol-port) fails the run rather than
  # hanging it forever.
  #
  # test-heap-teardown itself is registered separately below, gated on MI_DEBUG: its `pin`
  # case needs the MI_DEBUG-only mi_debug_stall_in_heap_delete_claim hook (arena.c), and the
  # source now `#error`s (like test-commit-fail.c) if built with MI_DEBUG==0 rather than
  # silently diverging between an undefined-symbol link failure (Debug + MI_NO_DEBUG=ON,
  # where MI_DEBUG=0 but the old `!defined(NDEBUG)` guard still referenced the symbol) and a
  # silently-skipped pin case (Release + MI_DEBUG_FULL=ON, where MI_DEBUG>0 but NDEBUG is
  # still defined by the Release build type) -- both reproduced by CI review of this PR.
  foreach(TEST_NAME heap-mt heap-churn heap-aba heap-delete-race)
    add_executable(mimalloc-test-${TEST_NAME} test/test-${TEST_NAME}.c)
    target_compile_definitions(mimalloc-test-${TEST_NAME} PRIVATE ${mi_defines})
    target_compile_options(mimalloc-test-${TEST_NAME} PRIVATE ${mi_cflags})
    target_include_directories(mimalloc-test-${TEST_NAME} PRIVATE include)
    if(MI_BUILD_STATIC AND NOT MI_DEBUG_TSAN)
      target_link_libraries(mimalloc-test-${TEST_NAME} PRIVATE mimalloc-static ${mi_libraries})
    else()
      target_link_libraries(mimalloc-test-${TEST_NAME} PRIVATE mimalloc ${mi_libraries})
    endif()
    if(NOT WIN32)
      find_package(Threads REQUIRED)
      target_link_libraries(mimalloc-test-${TEST_NAME} PRIVATE Threads::Threads)
    endif()
    add_test(NAME test-${TEST_NAME} COMMAND mimalloc-test-${TEST_NAME})
    set_tests_properties(test-${TEST_NAME} PROPERTIES TIMEOUT 300)
  endforeach()

  if(MI_DEBUG)
    add_executable(mimalloc-test-heap-teardown test/test-heap-teardown.c)
    target_compile_definitions(mimalloc-test-heap-teardown PRIVATE ${mi_defines})
    target_compile_options(mimalloc-test-heap-teardown PRIVATE ${mi_cflags})
    target_include_directories(mimalloc-test-heap-teardown PRIVATE include)
    if(MI_BUILD_STATIC AND NOT MI_DEBUG_TSAN)
      target_link_libraries(mimalloc-test-heap-teardown PRIVATE mimalloc-static ${mi_libraries})
    else()
      target_link_libraries(mimalloc-test-heap-teardown PRIVATE mimalloc ${mi_libraries})
    endif()
    if(NOT WIN32)
      find_package(Threads REQUIRED)
      target_link_libraries(mimalloc-test-heap-teardown PRIVATE Threads::Threads)
    endif()
    add_test(NAME test-heap-teardown COMMAND mimalloc-test-heap-teardown)
    set_tests_properties(test-heap-teardown PROPERTIES TIMEOUT 300)
  endif()

  # pthread_atfork fork-safety handlers (issue #270, Bun parity P5). POSIX-only --
  # both sources compile to a Windows-skip stub via #ifdef _WIN32, but there is no
  # pthread/fork() to link against on MSVC/MinGW, so only build+register them off
  # Windows (matching src/init.c's own `#if !defined(_WIN32) && !defined(__wasi__)`
  # guard around the pthread_atfork registration these tests exercise).
  if(NOT WIN32)
    find_package(Threads REQUIRED)

    # test-fork-locks: the repro from #270's Step 1 -- see the file's header comment
    # for how to reproduce the pre-#270 hang (revert the subproc.c/init.c fork-handler
    # changes and re-run). TIMEOUT is generous: 200 forks x a 5s per-child watchdog is
    # the worst case if every single fork hangs, which should never happen once the
    # handlers are in place (a clean run finishes in well under a second).
    add_executable(mimalloc-test-fork-locks test/test-fork-locks.c)
    target_compile_definitions(mimalloc-test-fork-locks PRIVATE ${mi_defines})
    target_compile_options(mimalloc-test-fork-locks PRIVATE ${mi_cflags})
    target_include_directories(mimalloc-test-fork-locks PRIVATE include)
    if(MI_BUILD_STATIC AND NOT MI_DEBUG_TSAN)
      target_link_libraries(mimalloc-test-fork-locks PRIVATE mimalloc-static ${mi_libraries})
    else()
      target_link_libraries(mimalloc-test-fork-locks PRIVATE mimalloc ${mi_libraries})
    endif()
    target_link_libraries(mimalloc-test-fork-locks PRIVATE Threads::Threads)
    add_test(NAME test-fork-locks COMMAND mimalloc-test-fork-locks)
    set_tests_properties(test-fork-locks PROPERTIES TIMEOUT 300)

    # #270 review follow-up: MIMALLOC_PROF/MIMALLOC_DHAT/MIMALLOC_MEMORY_EVENTS each
    # activate an alloc/free HOOK lock (prof_lock/dhat_lock/memevt_cb_lock) that a
    # default run of this test never touches. An earlier version of subproc.c's lock
    # order took those hook locks before the heap/arena locks (porting Bun's stated
    # rule unmodified) and deadlocked deterministically under MIMALLOC_PROF=1 in a
    # Release build (see the #270 PR discussion) -- these variants keep that reproducer
    # registered so a future reordering regresses loudly in CI instead of silently.
    add_test(NAME test-fork-locks-prof-env COMMAND mimalloc-test-fork-locks)
    set_tests_properties(test-fork-locks-prof-env PROPERTIES TIMEOUT 300 ENVIRONMENT "MIMALLOC_PROF=1")
    add_test(NAME test-fork-locks-dhat-env COMMAND mimalloc-test-fork-locks)
    set_tests_properties(test-fork-locks-dhat-env PROPERTIES TIMEOUT 300 ENVIRONMENT "MIMALLOC_DHAT=1")
    add_test(NAME test-fork-locks-memevt-env COMMAND mimalloc-test-fork-locks)
    set_tests_properties(test-fork-locks-memevt-env PROPERTIES TIMEOUT 300 ENVIRONMENT "MIMALLOC_MEMORY_EVENTS=1")

    # #270 review follow-up (F1): the lock-ORDER stress variant. `MI_TEST_FORK_SPAWN=1`
    # makes the test start short-lived threads continuously while it forks; each thread
    # start allocates its own tld/theap through `_mi_meta_zalloc`, which holds
    # `theap_meta_lock` across a full allocation on `heap_main` and therefore nests it
    # OUTSIDE `heap_main->arena_pages_lock` / `arena_reserve_lock` / the page-map lock.
    # A prepare() that took any of those first deadlocks the parent inside fork() here.
    # Paired with MIMALLOC_PROF=1 so the alloc/free hook locks are live at the same time.
    add_test(NAME test-fork-locks-spawn-prof-env COMMAND mimalloc-test-fork-locks)
    set_tests_properties(test-fork-locks-spawn-prof-env PROPERTIES TIMEOUT 300
                         ENVIRONMENT "MIMALLOC_PROF=1;MI_TEST_FORK_SPAWN=1")

    # test-fork-user-heap: imported from oven-sh/mimalloc @ 942b8342, MIT (see the
    # file's header comment for provenance and for which cases are disabled here as
    # were `#if 0 // Phase 7` pending the scavenger/tld registry and are re-enabled by #272).
    add_executable(mimalloc-test-fork-user-heap test/test-fork-user-heap.c)
    target_compile_definitions(mimalloc-test-fork-user-heap PRIVATE ${mi_defines})
    target_compile_options(mimalloc-test-fork-user-heap PRIVATE ${mi_cflags})
    target_include_directories(mimalloc-test-fork-user-heap PRIVATE include)
    if(MI_BUILD_STATIC AND NOT MI_DEBUG_TSAN)
      target_link_libraries(mimalloc-test-fork-user-heap PRIVATE mimalloc-static ${mi_libraries})
    else()
      target_link_libraries(mimalloc-test-fork-user-heap PRIVATE mimalloc ${mi_libraries})
    endif()
    target_link_libraries(mimalloc-test-fork-user-heap PRIVATE Threads::Threads)
    add_test(NAME test-fork-user-heap COMMAND mimalloc-test-fork-user-heap)
    set_tests_properties(test-fork-user-heap PROPERTIES TIMEOUT 300)

    # test-park-handoff: imported from oven-sh/mimalloc @ 942b8342, MIT (issue #272 /
    # Bun parity P7a). Exercises `mi_on_thread_idle_start`/`_end` -- the scavenger claiming
    # and sweeping a parked thread's theaps -- including park-then-exit, park-then-cancel,
    # fork() while parked, a third thread freeing into pages under sweep, and
    # `mi_scavenger_stop`. See the file header for the two Phase-7b adaptations.
    add_executable(mimalloc-test-park-handoff test/test-park-handoff.c)
    target_compile_definitions(mimalloc-test-park-handoff PRIVATE ${mi_defines})
    target_compile_options(mimalloc-test-park-handoff PRIVATE ${mi_cflags})
    target_include_directories(mimalloc-test-park-handoff PRIVATE include)
    if(MI_BUILD_STATIC AND NOT MI_DEBUG_TSAN)
      target_link_libraries(mimalloc-test-park-handoff PRIVATE mimalloc-static ${mi_libraries})
    else()
      target_link_libraries(mimalloc-test-park-handoff PRIVATE mimalloc ${mi_libraries})
    endif()
    target_link_libraries(mimalloc-test-park-handoff PRIVATE Threads::Threads)
    add_test(NAME test-park-handoff COMMAND mimalloc-test-park-handoff)
    set_tests_properties(test-park-handoff PROPERTIES TIMEOUT 600)
    # ... and with the scavenger disabled: every case must take its "nobody to hand off to"
    # branch and still leave the heap correct (`mi_on_thread_idle` sweeping inline).
    add_test(NAME test-park-handoff-no-scavenger COMMAND mimalloc-test-park-handoff)
    set_tests_properties(test-park-handoff-no-scavenger PROPERTIES TIMEOUT 600
                         ENVIRONMENT "MIMALLOC_SCAVENGER=0")
    # ... and with the P7b sweep rate limit removed, so every park is due the moment it is
    # claimed: `test_park_inside_window_gets_swept` takes its "no window" branch and
    # `test_parks_get_swept` stops spacing its parks (the file calls this the `-eager` variant).
    add_test(NAME test-park-handoff-eager COMMAND mimalloc-test-park-handoff)
    set_tests_properties(test-park-handoff-eager PROPERTIES TIMEOUT 600
                         ENVIRONMENT "MIMALLOC_PURGE_HOLES_MIN_INTERVAL=0")

    # test-purge-holes: imported from oven-sh/mimalloc @ 942b8342, MIT (issue #272 /
    # Bun parity P7b). The hole engine's own test: survivors stay byte-for-byte intact
    # across a discard, churn/aligned/lifecycle/abandoned/large-page cases, the unformed
    # tail, the OS-page arithmetic, the read-only hole report, and the `swept_state` skip
    # (including its `purge_holes_full_every` bound). It reaches internal headers, so it
    # links the static library and is built with the library's own defines.
    # It walks `mi_page_t` and calls `_mi_ptr_page` / `_mi_page_purged_count` /
    # `_mi_page_purge_os_page_blocks` directly, and those are hidden in a shared build
    # (`-fvisibility=hidden`), so this one is STATIC-ONLY -- the `ctest-shared` lane
    # (`MI_BUILD_STATIC=OFF`) skips it rather than failing to link.
    if(MI_BUILD_STATIC AND NOT MI_DEBUG_TSAN)
      add_executable(mimalloc-test-purge-holes test/test-purge-holes.c)
      target_compile_definitions(mimalloc-test-purge-holes PRIVATE ${mi_defines})
      target_compile_options(mimalloc-test-purge-holes PRIVATE ${mi_cflags})
      target_include_directories(mimalloc-test-purge-holes PRIVATE include)
      # MI_STATIC_LIB: `mimalloc-static` is compiled with it (see the target above), and
      # without it here MSVC resolves the internal declarations this test uses through
      # `__declspec(dllimport)` and the link fails with LNK2019 on `_mi_os_page_size` /
      # `__mi_page_map`. Same pattern as `mimalloc-test-tls-controls`.
      target_compile_definitions(mimalloc-test-purge-holes PRIVATE MI_STATIC_LIB)
      target_link_libraries(mimalloc-test-purge-holes PRIVATE mimalloc-static ${mi_libraries})
      target_link_libraries(mimalloc-test-purge-holes PRIVATE Threads::Threads)
      add_test(NAME test-purge-holes COMMAND mimalloc-test-purge-holes)
      set_tests_properties(test-purge-holes PROPERTIES TIMEOUT 600)
      # ... and with the feature off: every data-integrity check still runs, and the test
      # additionally asserts that nothing is ever discarded (the option's off-path).
      add_test(NAME test-purge-holes-off COMMAND mimalloc-test-purge-holes)
      set_tests_properties(test-purge-holes-off PROPERTIES TIMEOUT 600
                           ENVIRONMENT "MIMALLOC_PURGE_HOLES=0")
    endif()

    # test-purge-zero: imported from oven-sh/mimalloc @ 942b8342, MIT (issue #272 /
    # Bun parity P7b). The purge path may hand a recycled slice back as "reads zero";
    # this poisons everything, churns until slices are purged and recycled, and verifies
    # every byte of every recycled mi_zalloc/mi_calloc allocation is zero. (Distinct from
    # test-zero-tracking, which covers the -- since #80 dead -- `purge_zeroes` option.)
    add_executable(mimalloc-test-purge-zero test/test-purge-zero.c)
    target_compile_definitions(mimalloc-test-purge-zero PRIVATE ${mi_defines})
    target_compile_options(mimalloc-test-purge-zero PRIVATE ${mi_cflags})
    target_include_directories(mimalloc-test-purge-zero PRIVATE include)
    if(MI_BUILD_STATIC AND NOT MI_DEBUG_TSAN)
      target_link_libraries(mimalloc-test-purge-zero PRIVATE mimalloc-static ${mi_libraries})
    else()
      target_link_libraries(mimalloc-test-purge-zero PRIVATE mimalloc ${mi_libraries})
    endif()
    add_test(NAME test-purge-zero COMMAND mimalloc-test-purge-zero)
    set_tests_properties(test-purge-zero PROPERTIES TIMEOUT 600)
  endif()

  # MI_NO_PROCESS_DETACH (issue #268, Bun parity P3): imported from
  # oven-sh/mimalloc @ 942b8342, MIT. The same test source is built and run twice --
  # once against a detach-*enabled* static library (the automatic exit path runs and
  # "process done" must appear under MIMALLOC_VERBOSE=1), once against a dedicated
  # static library compiled with MI_NO_PROCESS_DETACH=1 (the automatic exit path is
  # skipped and "process done" must NOT appear). The define has to be baked into the
  # library build itself -- it guards src/init.c's _mi_auto_process_done and
  # src/prim/prim.c's destructor registration, not anything in the test source -- so
  # this needs its own static-lib target rather than just a per-test compile
  # definition. Requires MI_BUILD_STATIC (skipped, e.g., under the
  # MI_BUILD_STATIC=OFF shared-lib CI job; every other c-unit job builds static by
  # default and covers this).
  if(MI_BUILD_STATIC AND NOT MI_DEBUG_TSAN)
    # Positive control (REQUIRE): needs a detach-*enabled* mimalloc-static. If the
    # top-level -DMI_NO_PROCESS_DETACH=ON option is set, mi_defines (see the
    # MI_SKIP_COLLECT_ON_EXIT-adjacent block above) already carries
    # MI_NO_PROCESS_DETACH=1 into mimalloc-static itself, and this test would fail by
    # construction -- "process done" can never appear no matter what the test does.
    # No c-unit job configures the top-level option today, but silently mispassing
    # (or, here, silently misfailing) if that changes is exactly what issue #277's
    # bundle format refuses to do for ctest properties; do the same for this.
    if(NOT MI_NO_PROCESS_DETACH)
      add_executable(mimalloc-test-no-process-detach test/test-no-process-detach.c)
      target_compile_definitions(mimalloc-test-no-process-detach PRIVATE ${mi_defines})
      target_compile_options(mimalloc-test-no-process-detach PRIVATE ${mi_cflags})
      target_include_directories(mimalloc-test-no-process-detach PRIVATE include)
      target_link_libraries(mimalloc-test-no-process-detach PRIVATE mimalloc-static ${mi_libraries})
      # ctest's PASS_REGULAR_EXPRESSION/FAIL_REGULAR_EXPRESSION have no bundle
      # equivalent (ci/bundle_tests.py's SUPPORTED_PROPERTIES refuses them outright,
      # issue #277) -- use the run-negative.cmake-style `cmake -P` harness instead,
      # which the bundler already knows how to lower. ENVIRONMENT is set via the
      # ctest property, not a script variable: ctest sets it on the `cmake -P`
      # process, execute_process() inherits it, and the bundler already lowers
      # ENVIRONMENT for any command shape.
      add_test(
        NAME test-no-process-detach-runs
        COMMAND ${CMAKE_COMMAND}
          -DTEST_EXE=$<TARGET_FILE:mimalloc-test-no-process-detach>
          "-DEXPECTED_TEXT=process done"
          -DMODE=REQUIRE
          -P ${CMAKE_CURRENT_SOURCE_DIR}/test/run-text-check.cmake)
      set_tests_properties(test-no-process-detach-runs PROPERTIES ENVIRONMENT "MIMALLOC_VERBOSE=1")
    endif()

    # Negative control (FORBID): always built, independent of the top-level option,
    # against a dedicated static library compiled from src/static.c -- the single-TU
    # amalgamation, i.e. exactly the shape a Bun-style consumer compiles (see the
    # `c++ -x c++ -c src/static.c ... -DMI_NO_PROCESS_DETACH=1` acceptance check in
    # #268) -- with MI_NO_PROCESS_DETACH=1 explicit regardless of what mi_defines
    # already carries.
    add_library(mimalloc-static-no-detach STATIC EXCLUDE_FROM_ALL src/static.c)
    set_property(TARGET mimalloc-static-no-detach PROPERTY POSITION_INDEPENDENT_CODE ON)
    target_compile_definitions(mimalloc-static-no-detach PRIVATE ${mi_defines} MI_STATIC_LIB MI_NO_PROCESS_DETACH=1)
    target_compile_options(mimalloc-static-no-detach PRIVATE ${mi_cflags} ${mi_cflags_static})
    target_link_libraries(mimalloc-static-no-detach PRIVATE ${mi_libraries})
    target_include_directories(mimalloc-static-no-detach PUBLIC
        $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
    )

    add_executable(mimalloc-test-no-process-detach-skip test/test-no-process-detach.c)
    target_compile_definitions(mimalloc-test-no-process-detach-skip PRIVATE ${mi_defines} MI_NO_PROCESS_DETACH=1)
    target_compile_options(mimalloc-test-no-process-detach-skip PRIVATE ${mi_cflags})
    target_include_directories(mimalloc-test-no-process-detach-skip PRIVATE include)
    target_link_libraries(mimalloc-test-no-process-detach-skip PRIVATE mimalloc-static-no-detach ${mi_libraries})
    add_test(
      NAME test-no-process-detach-skipped
      COMMAND ${CMAKE_COMMAND}
        -DTEST_EXE=$<TARGET_FILE:mimalloc-test-no-process-detach-skip>
        "-DEXPECTED_TEXT=process done"
        -DMODE=FORBID
        -P ${CMAKE_CURRENT_SOURCE_DIR}/test/run-text-check.cmake)
    set_tests_properties(test-no-process-detach-skipped PROPERTIES ENVIRONMENT "MIMALLOC_VERBOSE=1")
  endif()

  # libFuzzer harnesses (issue #87). Off by default and clang-only: -fsanitize=fuzzer
  # is a clang feature, and these are driven by the fuzz CI job rather than ctest --
  # a fuzz run has no fixed duration, so it does not belong in the normal suite.
  # The oracle is ASan (#86); without a sanitizer, fuzzing an allocator mostly proves
  # it does not segfault.
  if(MI_FUZZ)
    if(NOT CMAKE_C_COMPILER_ID MATCHES "Clang")
      message(FATAL_ERROR "MI_FUZZ=ON requires clang (-fsanitize=fuzzer); got ${CMAKE_C_COMPILER_ID}")
    endif()
    foreach(FUZZ_NAME alloc-ops prof-dump)
      string(REPLACE "-" "_" FUZZ_SRC ${FUZZ_NAME})
      add_executable(mimalloc-fuzz-${FUZZ_NAME} test/fuzz/fuzz_${FUZZ_SRC}.c)
      target_compile_definitions(mimalloc-fuzz-${FUZZ_NAME} PRIVATE ${mi_defines})
      target_compile_options(mimalloc-fuzz-${FUZZ_NAME} PRIVATE ${mi_cflags} -fsanitize=fuzzer,address)
      target_link_options(mimalloc-fuzz-${FUZZ_NAME} PRIVATE -fsanitize=fuzzer,address)
      target_include_directories(mimalloc-fuzz-${FUZZ_NAME} PRIVATE include)
      if(MI_BUILD_STATIC AND NOT MI_DEBUG_TSAN)
        target_link_libraries(mimalloc-fuzz-${FUZZ_NAME} PRIVATE mimalloc-static ${mi_libraries})
      else()
        target_link_libraries(mimalloc-fuzz-${FUZZ_NAME} PRIVATE mimalloc ${mi_libraries})
      endif()
    endforeach()
    # Positive control: a planted, easily-reachable bug the fuzzer must find. A fuzz
    # harness that has never found anything is indistinguishable from one that is not
    # running the code under test.
    if(MI_FUZZ_PLANT_BUG)
      target_compile_definitions(mimalloc-fuzz-alloc-ops PRIVATE MI_FUZZ_PLANT_BUG=1)
    endif()
  endif()

  # Seeded-sampling determinism across processes (issue #91). Spawns itself, so it
  # cannot be an in-process loop: the bug was ASLR-dependent, and two runs inside one
  # process share an address layout and would have agreed even while broken.
  # MI_PPROF-only: with the profiler compiled out, mi_prof_start_seeded is a stub that
  # returns false, so the test would fail for a reason that is not a defect.
  if(MI_PPROF)
  add_executable(mimalloc-test-prof-seed-determinism test/test-prof-seed-determinism.c)
  target_compile_definitions(mimalloc-test-prof-seed-determinism PRIVATE ${mi_defines})
  target_compile_options(mimalloc-test-prof-seed-determinism PRIVATE ${mi_cflags})
  target_include_directories(mimalloc-test-prof-seed-determinism PRIVATE include)
  if(MI_BUILD_STATIC AND NOT MI_DEBUG_TSAN)
    target_link_libraries(mimalloc-test-prof-seed-determinism PRIVATE mimalloc-static ${mi_libraries})
  else()
    target_link_libraries(mimalloc-test-prof-seed-determinism PRIVATE mimalloc ${mi_libraries})
  endif()
  add_test(NAME test-prof-seed-determinism COMMAND mimalloc-test-prof-seed-determinism)
  set_tests_properties(test-prof-seed-determinism PROPERTIES TIMEOUT 300)
  endif()

  # Positive control for the ASan job (issue #86). Only built when the sanitizer is
  # actually enabled -- and MI_TRACK_ASAN is the *resolved* value here, since
  # CMakeLists turns it OFF above if sanitizer/asan_interface.h is missing. It is
  # deliberately NOT an add_test: it must abort, so CI runs it directly and fails if
  # it exits cleanly.
  if(MI_TRACK_ASAN)
    add_executable(mimalloc-test-asan-control test/test-asan-control.c)
    target_compile_definitions(mimalloc-test-asan-control PRIVATE ${mi_defines})
    target_compile_options(mimalloc-test-asan-control PRIVATE ${mi_cflags})
    target_include_directories(mimalloc-test-asan-control PRIVATE include)
    if(MI_BUILD_STATIC AND NOT MI_DEBUG_TSAN)
      target_link_libraries(mimalloc-test-asan-control PRIVATE mimalloc-static ${mi_libraries})
    else()
      target_link_libraries(mimalloc-test-asan-control PRIVATE mimalloc ${mi_libraries})
    endif()
  endif()
  set_tests_properties(test-memory-gate PROPERTIES TIMEOUT 900)

  # Zero-tracking bench + correctness harness (issue #67). Registered as a ctest so the
  # *correctness* half runs on every commit; the timing halves are only meaningful on a
  # quiet runner and are consumed by the zero-tracking CI job.
  add_executable(mimalloc-test-zero-tracking test/bench-zero-tracking.c)
  target_compile_definitions(mimalloc-test-zero-tracking PRIVATE ${mi_defines})
  target_compile_options(mimalloc-test-zero-tracking PRIVATE ${mi_cflags})
  target_include_directories(mimalloc-test-zero-tracking PRIVATE include)
  if(MI_BUILD_STATIC AND NOT MI_DEBUG_TSAN)
    target_link_libraries(mimalloc-test-zero-tracking PRIVATE mimalloc-static ${mi_libraries})
  else()
    target_link_libraries(mimalloc-test-zero-tracking PRIVATE mimalloc ${mi_libraries})
  endif()
  add_test(NAME test-zero-tracking COMMAND mimalloc-test-zero-tracking)
  set_tests_properties(test-zero-tracking PROPERTIES TIMEOUT 900)
  # The feature is off by default, so the default run would never exercise it. This is
  # the "any flag that ships must have a job running the suite with it ON" rule from #67.
  add_test(NAME test-zero-tracking-enabled COMMAND mimalloc-test-zero-tracking)
  set_tests_properties(test-zero-tracking-enabled PROPERTIES
    TIMEOUT 900 ENVIRONMENT "MIMALLOC_PURGE_ZEROES=1;MIMALLOC_PURGE_DELAY=0")

  # Degenerate memory patterns and memory-bound regression guards (issue #47).
  # Independent of MI_PPROF: it guards allocator behavior, not the profiler.
  add_executable(mimalloc-test-degenerate test/test-degenerate.c)
  target_compile_definitions(mimalloc-test-degenerate PRIVATE ${mi_defines})
  target_compile_options(mimalloc-test-degenerate PRIVATE ${mi_cflags})
  target_include_directories(mimalloc-test-degenerate PRIVATE include)
  if(MI_BUILD_STATIC AND NOT MI_DEBUG_TSAN)
    target_link_libraries(mimalloc-test-degenerate PRIVATE mimalloc-static ${mi_libraries})
  else()
    target_link_libraries(mimalloc-test-degenerate PRIVATE mimalloc ${mi_libraries})
  endif()
  if(NOT WIN32)
    find_package(Threads REQUIRED)
    target_link_libraries(mimalloc-test-degenerate PRIVATE Threads::Threads)
  endif()
  add_test(NAME test-degenerate COMMAND mimalloc-test-degenerate)
  set_tests_properties(test-degenerate PROPERTIES TIMEOUT 600)

  # memory-events (issue #20): independent of MI_PPROF, always built/registered.
  add_executable(mimalloc-test-memory-events test/test-memory-events.c)
  target_compile_definitions(mimalloc-test-memory-events PRIVATE ${mi_defines})
  target_compile_options(mimalloc-test-memory-events PRIVATE ${mi_cflags})
  target_include_directories(mimalloc-test-memory-events PRIVATE include)
  if(MI_BUILD_STATIC AND NOT MI_DEBUG_TSAN)
    target_link_libraries(mimalloc-test-memory-events PRIVATE mimalloc-static ${mi_libraries})
  else()
    target_link_libraries(mimalloc-test-memory-events PRIVATE mimalloc ${mi_libraries})
  endif()
  add_test(NAME test-memory-events COMMAND mimalloc-test-memory-events)
  add_test(NAME test-memory-events-env-enabled COMMAND mimalloc-test-memory-events --env-enabled-check)
  set_tests_properties(test-memory-events-env-enabled PROPERTIES ENVIRONMENT "MIMALLOC_MEMORY_EVENTS=1")

  # Exact DHAT v2 observer (issue #238): intentionally independent of MI_PPROF.
  add_executable(mimalloc-test-dhat test/test-dhat.c)
  target_compile_definitions(mimalloc-test-dhat PRIVATE ${mi_defines})
  target_compile_options(mimalloc-test-dhat PRIVATE ${mi_cflags})
  target_include_directories(mimalloc-test-dhat PRIVATE include)
  if(MI_BUILD_STATIC AND NOT MI_DEBUG_TSAN)
    target_link_libraries(mimalloc-test-dhat PRIVATE mimalloc-static ${mi_libraries})
  else()
    target_link_libraries(mimalloc-test-dhat PRIVATE mimalloc ${mi_libraries})
  endif()
  add_test(NAME test-dhat COMMAND mimalloc-test-dhat)

  # mi_heap_dump_json / mi_heap_get_seq (issue #269, Bun parity P4): backs bun:jsc
  # heapStats({dump:true|"blocks"}).mimallocDump. Independent of MI_PPROF, same as
  # memory-events/dhat above.
  add_executable(mimalloc-test-heap-dump-json test/test-heap-dump-json.c)
  target_compile_definitions(mimalloc-test-heap-dump-json PRIVATE ${mi_defines})
  target_compile_options(mimalloc-test-heap-dump-json PRIVATE ${mi_cflags})
  target_include_directories(mimalloc-test-heap-dump-json PRIVATE include)
  if(MI_BUILD_STATIC AND NOT MI_DEBUG_TSAN)
    target_link_libraries(mimalloc-test-heap-dump-json PRIVATE mimalloc-static ${mi_libraries})
  else()
    target_link_libraries(mimalloc-test-heap-dump-json PRIVATE mimalloc ${mi_libraries})
  endif()
  if(NOT WIN32)
    find_package(Threads REQUIRED)
    target_link_libraries(mimalloc-test-heap-dump-json PRIVATE Threads::Threads)
  endif()
  add_test(NAME test-heap-dump-json COMMAND mimalloc-test-heap-dump-json)

  # thread-local slot array provenance (issue #128 B3): the array must not be
  # allocated from whatever heap the application happens to have set as its default,
  # or mi_heap_destroy on that heap frees it while it is still live.
  add_executable(mimalloc-test-tls-slots-heap test/test-tls-slots-heap.c)
  target_compile_definitions(mimalloc-test-tls-slots-heap PRIVATE ${mi_defines})
  target_compile_options(mimalloc-test-tls-slots-heap PRIVATE ${mi_cflags})
  target_include_directories(mimalloc-test-tls-slots-heap PRIVATE include)
  if(MI_BUILD_STATIC AND NOT MI_DEBUG_TSAN)
    target_link_libraries(mimalloc-test-tls-slots-heap PRIVATE mimalloc-static ${mi_libraries})
  else()
    target_link_libraries(mimalloc-test-tls-slots-heap PRIVATE mimalloc ${mi_libraries})
  endif()
  add_test(NAME test-tls-slots-heap COMMAND mimalloc-test-tls-slots-heap)

  # mi_rezalloc zero-fill boundary (issue #78): pins the behaviour that makes the
  # explicit slot zeroing in src/threadlocal.c necessary.
  add_executable(mimalloc-test-rezalloc-slack test/test-rezalloc-slack.c)
  target_compile_definitions(mimalloc-test-rezalloc-slack PRIVATE ${mi_defines})
  target_compile_options(mimalloc-test-rezalloc-slack PRIVATE ${mi_cflags})
  target_include_directories(mimalloc-test-rezalloc-slack PRIVATE include)
  if(MI_BUILD_STATIC AND NOT MI_DEBUG_TSAN)
    target_link_libraries(mimalloc-test-rezalloc-slack PRIVATE mimalloc-static ${mi_libraries})
  else()
    target_link_libraries(mimalloc-test-rezalloc-slack PRIVATE mimalloc ${mi_libraries})
  endif()
  add_test(NAME test-rezalloc-slack COMMAND mimalloc-test-rezalloc-slack)

  # Public-API regression for the full TLS growth schedule and lifetime edges
  # that motivated issue #167 (including upstream microsoft/mimalloc#1358).
  add_executable(mimalloc-test-threadlocal-growth test/test-threadlocal-growth.c)
  target_compile_definitions(mimalloc-test-threadlocal-growth PRIVATE ${mi_defines})
  target_compile_options(mimalloc-test-threadlocal-growth PRIVATE ${mi_cflags})
  target_include_directories(mimalloc-test-threadlocal-growth PRIVATE include)
  if(MI_BUILD_STATIC AND NOT MI_DEBUG_TSAN)
    target_link_libraries(mimalloc-test-threadlocal-growth PRIVATE mimalloc-static ${mi_libraries})
  else()
    target_link_libraries(mimalloc-test-threadlocal-growth PRIVATE mimalloc ${mi_libraries})
  endif()
  if(NOT WIN32)
    find_package(Threads REQUIRED)
    target_link_libraries(mimalloc-test-threadlocal-growth PRIVATE Threads::Threads)
  endif()
  add_test(NAME test-threadlocal-growth COMMAND mimalloc-test-threadlocal-growth)
  set_tests_properties(test-threadlocal-growth PROPERTIES TIMEOUT 60)

  # MI_DEBUG_FULL-only positive control for the internal lock diagnostics (#167).
  # The program deliberately reacquires a non-recursive lock. The CMake wrapper
  # requires an immediate diagnostic failure with the expected message; a deadlock
  # times out and fails the test instead of wedging the suite.
  if(MI_DEBUG_FULL)
    add_executable(mimalloc-test-lock-reentrancy test/test-lock-reentrancy.c)
    target_compile_definitions(mimalloc-test-lock-reentrancy PRIVATE ${mi_defines})
    target_compile_options(mimalloc-test-lock-reentrancy PRIVATE ${mi_cflags})
    target_include_directories(mimalloc-test-lock-reentrancy PRIVATE include)
    if(MI_BUILD_STATIC AND NOT MI_DEBUG_TSAN)
      target_link_libraries(mimalloc-test-lock-reentrancy PRIVATE mimalloc-static ${mi_libraries})
    else()
      target_link_libraries(mimalloc-test-lock-reentrancy PRIVATE mimalloc ${mi_libraries})
    endif()
    add_test(
      NAME test-lock-reentrancy
      COMMAND ${CMAKE_COMMAND}
        -DTEST_EXE=$<TARGET_FILE:mimalloc-test-lock-reentrancy>
        -DTEST_ARG=reentrant
        -DEXPECTED_TEXT=reentrant_internal_lock_acquisition
        -P ${CMAKE_CURRENT_SOURCE_DIR}/test/run-negative.cmake)
    add_test(
      NAME test-lock-uncleared-owner
      COMMAND ${CMAKE_COMMAND}
        -DTEST_EXE=$<TARGET_FILE:mimalloc-test-lock-reentrancy>
        -DTEST_ARG=uncleared
        -DEXPECTED_TEXT=internal_lock_owner_not_cleared
        -P ${CMAKE_CURRENT_SOURCE_DIR}/test/run-negative.cmake)
    add_test(
      NAME test-lock-nonowner-release
      COMMAND ${CMAKE_COMMAND}
        -DTEST_EXE=$<TARGET_FILE:mimalloc-test-lock-reentrancy>
        -DTEST_ARG=nonowner-release
        -DEXPECTED_TEXT=internal_lock_release_by_non_owner
        -P ${CMAKE_CURRENT_SOURCE_DIR}/test/run-negative.cmake)
    add_test(
      NAME test-lock-destroy-owned
      COMMAND ${CMAKE_COMMAND}
        -DTEST_EXE=$<TARGET_FILE:mimalloc-test-lock-reentrancy>
        -DTEST_ARG=destroy-owned
        -DEXPECTED_TEXT=destroying_owned_internal_lock
        -P ${CMAKE_CURRENT_SOURCE_DIR}/test/run-negative.cmake)
    set_tests_properties(
      test-lock-reentrancy test-lock-uncleared-owner
      test-lock-nonowner-release test-lock-destroy-owned
      PROPERTIES TIMEOUT 15)

    # Compile a private amalgamation with deterministic fault/poison controls.
    # MI_TEST_TLS_CONTROL is absent from all library targets and releases.
    add_executable(mimalloc-test-tls-controls src/static.c test/test-tls-controls.c)
    target_compile_definitions(mimalloc-test-tls-controls PRIVATE
      ${mi_defines} MI_STATIC_LIB MI_TEST_TLS_CONTROL=1)
    target_compile_options(mimalloc-test-tls-controls PRIVATE ${mi_cflags} ${mi_cflags_static})
    target_include_directories(mimalloc-test-tls-controls PRIVATE include)
    target_link_libraries(mimalloc-test-tls-controls PRIVATE ${mi_libraries})

    add_test(
      NAME test-tls-control-owner
      COMMAND ${CMAKE_COMMAND}
        -DTEST_EXE=$<TARGET_FILE:mimalloc-test-tls-controls>
        -DTEST_ARG=owner
        -DEXPECTED_TEXT=internal_tls_storage_not_main_owned
        -P ${CMAKE_CURRENT_SOURCE_DIR}/test/run-negative.cmake)
    add_test(
      NAME test-tls-control-zero
      COMMAND ${CMAKE_COMMAND}
        -DTEST_EXE=$<TARGET_FILE:mimalloc-test-tls-controls>
        -DTEST_ARG=zero
        -DEXPECTED_TEXT=internal_tls_new_slots_not_zero
        -P ${CMAKE_CURRENT_SOURCE_DIR}/test/run-negative.cmake)
    add_test(NAME test-tls-control-failure COMMAND mimalloc-test-tls-controls failure)
    set_tests_properties(
      test-tls-control-owner test-tls-control-zero test-tls-control-failure
      PROPERTIES TIMEOUT 15)
  endif()

  # mi_process_info's current_rss must be RSS, not committed bytes (issue #78).
  add_executable(mimalloc-test-process-rss test/test-process-rss.c)
  target_compile_definitions(mimalloc-test-process-rss PRIVATE ${mi_defines})
  target_compile_options(mimalloc-test-process-rss PRIVATE ${mi_cflags})
  target_include_directories(mimalloc-test-process-rss PRIVATE include)
  if(MI_BUILD_STATIC AND NOT MI_DEBUG_TSAN)
    target_link_libraries(mimalloc-test-process-rss PRIVATE mimalloc-static ${mi_libraries})
  else()
    target_link_libraries(mimalloc-test-process-rss PRIVATE mimalloc ${mi_libraries})
  endif()
  add_test(NAME test-process-rss COMMAND mimalloc-test-process-rss)

  # issue #272 (Bun parity P7a) acceptance test. A C++ TU that declares
  # `extern "C" void mi_on_thread_idle(void) noexcept;` by hand, exactly as Bun's consumer
  # code does (no mimalloc header), so a mangling or noexcept mismatch in the public header
  # is a link error here. It then asserts the behaviour: RSS drops by at least half of a
  # freed 256 MiB within one second of a worker thread going idle, with neither thread
  # allocating in the meantime (so it measures the scavenger, not an inline purge).
  # A C++ compiler is not required by this project (`project(libmimalloc C)`), so only add
  # the target where one can actually be enabled.
  #
  # The Apple CROSS toolchain is the exception that has to be spelled out:
  # `enable_language(CXX)` there dies with "Could not find install_name_tool, please check
  # your installation" (CMakeFindBinUtils via CMakeDetermineCXXCompiler) -- CMake's Apple
  # C++ path insists on that tool and soldr's Linux-hosted Apple SDK toolchain does not
  # provide it under that name, while the C language never asks for it. Nothing is lost:
  # macos-bundles is compile-only for arm64 and its x64 run is gated on #283's golden
  # image, so this test would not be RUN there in any case, and its actual claim -- that
  # `extern "C" void mi_on_thread_idle(void) noexcept;` written by hand, as Bun writes it,
  # links against our library -- is covered by the native Linux jobs, the native `cl` job,
  # the MinGW bundle lane, and the clang-cl bundle lane (where CMakeLists turns MI_USE_CXX
  # on by itself, so CXX is already loaded by the time we get here).
  set(mi_have_cxx_for_tests FALSE)
  if(CMAKE_CXX_COMPILER_LOADED)
    set(mi_have_cxx_for_tests TRUE)
  elseif(NOT (APPLE AND CMAKE_CROSSCOMPILING))
    include(CheckLanguage)
    check_language(CXX)
    if(CMAKE_CXX_COMPILER)
      enable_language(CXX)
      set(mi_have_cxx_for_tests TRUE)
    endif()
  endif()
  if(mi_have_cxx_for_tests AND MI_BUILD_STATIC AND NOT MI_DEBUG_TSAN)
    add_executable(mimalloc-test-thread-idle-rss test/test-thread-idle-rss.cpp)
    target_compile_definitions(mimalloc-test-thread-idle-rss PRIVATE ${mi_defines})
    target_compile_options(mimalloc-test-thread-idle-rss PRIVATE ${mi_cflags})
    target_include_directories(mimalloc-test-thread-idle-rss PRIVATE include)
    target_link_libraries(mimalloc-test-thread-idle-rss PRIVATE mimalloc-static ${mi_libraries})
    if(NOT WIN32)
      find_package(Threads REQUIRED)
      target_link_libraries(mimalloc-test-thread-idle-rss PRIVATE Threads::Threads)
    endif()
    add_test(NAME test-thread-idle-rss COMMAND mimalloc-test-thread-idle-rss)
    set_tests_properties(test-thread-idle-rss PROPERTIES TIMEOUT 120)
  endif()

  # macOS VM tag must be application-specific (issue #78).
  add_executable(mimalloc-test-os-tag test/test-os-tag.c)
  target_compile_definitions(mimalloc-test-os-tag PRIVATE ${mi_defines})
  target_compile_options(mimalloc-test-os-tag PRIVATE ${mi_cflags})
  target_include_directories(mimalloc-test-os-tag PRIVATE include)
  if(MI_BUILD_STATIC AND NOT MI_DEBUG_TSAN)
    target_link_libraries(mimalloc-test-os-tag PRIVATE mimalloc-static ${mi_libraries})
  else()
    target_link_libraries(mimalloc-test-os-tag PRIVATE mimalloc ${mi_libraries})
  endif()
  add_test(NAME test-os-tag COMMAND mimalloc-test-os-tag)

  # dynamic override test
  if(MI_BUILD_SHARED AND NOT (MI_CYGWIN OR MI_TRACK_ASAN OR MI_DEBUG_TSAN OR MI_DEBUG_UBSAN)) # AND NOT (APPLE AND MI_USE_CXX))
    add_executable(mimalloc-test-stress-dynamic test/test-stress.c)
    target_compile_definitions(mimalloc-test-stress-dynamic PRIVATE ${mi_defines} "USE_STD_MALLOC=1")
    target_compile_options(mimalloc-test-stress-dynamic PRIVATE ${mi_cflags})
    target_include_directories(mimalloc-test-stress-dynamic PRIVATE include)
    if(WIN32)
      target_compile_definitions(mimalloc-test-stress-dynamic PRIVATE "MI_LINK_VERSION=1")  # link mi_version
      if(MINGW)
        # `mimalloc.dll` must come first in this exe's import table, or the redirection
        # module (which `mimalloc.dll` pulls in, and which imports only `ntdll.dll`) is
        # initialised after the CRT it is meant to patch and `mi_is_redirected()` stays
        # false. That is what upstream's "mingw always links mimalloc after system
        # libraries" note below is about.
        #
        # The mechanism is `ld`'s PE script, which emits the import descriptors under
        # `SORT(*)(.idata$2)` -- sorted by the *input file path as spelled on the command
        # line*, archive path first and member name only as a tie-break within one
        # archive. CMake names this import library relative to the build directory
        # (`libmimalloc.dll.a`), while every CRT archive arrives as an absolute path from
        # the sysroot, and `/` (0x2f) sorts before `l`. Spelling the same file `./…`
        # (0x2e) puts it ahead of every absolute path instead. Verified with
        # `objdump -p`: byte-identical archives give `mimalloc.dll` last when named
        # `libmimalloc.dll.a` and first when named `./libmimalloc.dll.a`.
        #
        # It has to sit in the *libraries* position, after the objects -- an archive named
        # before anything needs it contributes nothing -- hence a raw `-l:` here rather
        # than target_link_options(), with an explicit dependency since this is no longer
        # a target reference.
        #
        # Same relative-path caveat as the DLL's own redirect import above: the link runs
        # from CMAKE_BINARY_DIR, so name this project's binary dir relative to that.
        file(RELATIVE_PATH _mi_mimalloc_ldir "${CMAKE_BINARY_DIR}" "${CMAKE_CURRENT_BINARY_DIR}")
        set(_mi_mimalloc_ldir "-L./${_mi_mimalloc_ldir}")
        add_dependencies(mimalloc-test-stress-dynamic mimalloc)
        target_link_libraries(mimalloc-test-stress-dynamic PRIVATE
          "${_mi_mimalloc_ldir}" "-l:$<TARGET_LINKER_FILE_NAME:mimalloc>" ${mi_libraries})
      else()
        target_link_libraries(mimalloc-test-stress-dynamic PRIVATE mimalloc ${mi_libraries})  # link mi_version
      endif()
      # No `minject` step. Upstream runs one here because "mingw always links mimalloc
      # after system libraries", but that is fixed at the link now -- above for the
      # executable, and in MI_MINGW_REDIRECT_FIRST for `mimalloc.dll`'s own table, which
      # is the half that actually decides whether the override engages. Running minject
      # on top would only rewrite an import table that is already correct, and it is a
      # Windows PE utility, so it cannot run on a cross build's host at all (#277).
      add_test(NAME test-stress-dynamic COMMAND ${CMAKE_COMMAND} -E env MIMALLOC_VERBOSE=1 $<TARGET_FILE:mimalloc-test-stress-dynamic>)
    else()
      target_link_libraries(mimalloc-test-stress-dynamic PRIVATE ${mi_libraries}) # pthreads, issue 1158
      if(APPLE)
        set(LD_PRELOAD "DYLD_INSERT_LIBRARIES")
      else()
        set(LD_PRELOAD "LD_PRELOAD")
      endif()
      add_test(NAME test-stress-dynamic COMMAND ${CMAKE_COMMAND} -E env MIMALLOC_VERBOSE=1 ${LD_PRELOAD}=$<TARGET_FILE:mimalloc> $<TARGET_FILE:mimalloc-test-stress-dynamic>)
    endif()
  endif()

  # Behavioural override probe (mimalloc-pprof #277). `mi_is_redirected()` reports what
  # the redirection module believed it did; this asks whether a CRT `malloc` pointer
  # actually lands in a mimalloc region, which is the claim that matters and the one the
  # msvcrt lane fails while reporting success. Windows + shared library only: everywhere
  # else there is no redirection module to probe.
  if(WIN32 AND MI_BUILD_SHARED AND MI_WIN_REDIRECT AND NOT (MI_CYGWIN OR MI_TRACK_ASAN OR MI_DEBUG_TSAN OR MI_DEBUG_UBSAN))
    add_executable(mimalloc-test-redirect-probe test/test-redirect-probe.c)
    target_compile_definitions(mimalloc-test-redirect-probe PRIVATE ${mi_defines})
    target_compile_options(mimalloc-test-redirect-probe PRIVATE ${mi_cflags})
    target_include_directories(mimalloc-test-redirect-probe PRIVATE include)
    if(MINGW)
      # `mimalloc.dll` must be this exe's first import descriptor, for the same reason and
      # by the same means as `mimalloc-test-stress-dynamic` above.
      file(RELATIVE_PATH _mi_probe_ldir "${CMAKE_BINARY_DIR}" "${CMAKE_CURRENT_BINARY_DIR}")
      set(_mi_probe_ldir "-L./${_mi_probe_ldir}")
      add_dependencies(mimalloc-test-redirect-probe mimalloc)
      target_link_libraries(mimalloc-test-redirect-probe PRIVATE
        "${_mi_probe_ldir}" "-l:$<TARGET_LINKER_FILE_NAME:mimalloc>" ${mi_libraries})
    else()
      target_link_libraries(mimalloc-test-redirect-probe PRIVATE mimalloc ${mi_libraries})
    endif()
    add_test(NAME test-redirect-probe COMMAND mimalloc-test-redirect-probe)
  endif()

  # imported from oven-sh/mimalloc @ 942b8342 (commit 7ac561ab), MIT -- see #266.
  # free(NULL) before any constructor has run (glibc 2.44's __newlocale does this from the
  # loader) must not dereference the page map's initial empty submap. The test's explicit
  # mi_free(NULL)/mi_malloc(64) calls are what exercise the bug directly; its plain
  # free(NULL)/malloc(64) calls only reach mi_free/mi_malloc when malloc is separately
  # overridden (e.g. via mimalloc-override.h or LD_PRELOAD), which this target does not
  # set up -- MI_OVERRIDE is a build-time default (option MI_OVERRIDE, line 9), not
  # something a directly-linked consumer gets automatically.
  # Link the static lib (not the MI_OVERRIDE=ON shared one): the dynamic target compiles
  # with MI_FREE_IS_CHECKED=1 (line ~419), which makes mi_free NULL-safe unconditionally
  # via _mi_checked_ptr_page and would never exercise the unchecked page-map path the bug
  # is in.
  # MSVC (cl.exe) is excluded: the pre-init hook is `__attribute__((section(...)))` /
  # `__attribute__((constructor))`, neither of which cl.exe supports, so a pure-MSVC build
  # would define no hook at all and the test would fail for the wrong reason (the hook
  # never running) rather than testing anything. MinGW (GCC) is unaffected and still runs
  # it via the constructor branch. Unverified on MSVC; see #266.
  if(NOT MI_DEBUG_TSAN AND NOT MSVC)
    add_executable(mimalloc-test-free-before-init test/test-free-before-init.c)
    target_compile_definitions(mimalloc-test-free-before-init PRIVATE ${mi_defines})
    target_compile_options(mimalloc-test-free-before-init PRIVATE ${mi_cflags})
    target_include_directories(mimalloc-test-free-before-init PRIVATE include)
    if(MI_BUILD_STATIC)
      target_link_libraries(mimalloc-test-free-before-init PRIVATE mimalloc-static ${mi_libraries})
    else()
      target_link_libraries(mimalloc-test-free-before-init PRIVATE mimalloc ${mi_libraries})
    endif()
    add_test(NAME test-free-before-init COMMAND mimalloc-test-free-before-init)
  endif()
endif()

# -----------------------------------------------------------------------------
# Set override properties
# -----------------------------------------------------------------------------
if (MI_OVERRIDE)
  if (MI_BUILD_SHARED)
    target_compile_definitions(mimalloc PRIVATE MI_MALLOC_OVERRIDE)
  endif()
  if (MI_BUILD_STATIC)
    target_compile_definitions(mimalloc-static PRIVATE MI_MALLOC_OVERRIDE)
  endif()
  if (MI_BUILD_OBJECT)
    target_compile_definitions(mimalloc-obj PRIVATE MI_MALLOC_OVERRIDE)
  endif()
endif()
