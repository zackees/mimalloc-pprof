# CMake toolchain: build this repository for x86_64 Windows (MSVC ABI), on Linux, through
# soldr's clang-cl lane.
#
# Issue #277 phase D. The MSVC gates are cross-compiled on a Linux runner into portable
# test bundles (ci/bundle_tests.py) and executed on ONE Windows runner, alongside the
# windows-gnu bundles phase C already put there.
#
# This file consumes ONLY the environment `soldr prepare --target x86_64-pc-windows-msvc`
# exports; it hardcodes no path and no compiler version.
#
#   soldr prepare --target x86_64-pc-windows-msvc --github-env "$GITHUB_ENV"
#   cmake -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DMI_PPROF=ON \
#         --toolchain cmake/toolchains/soldr-x86_64-pc-windows-msvc.cmake
#
# soldr exports (verified with soldr 0.9.11, LLVM 21.1.5, xwin splat 2026-06-22):
#   CC_x86_64_pc_windows_msvc      `clang-cl`   -- a BARE NAME, resolved from the PATH
#   CXX_x86_64_pc_windows_msvc     `clang-cl`      soldr prepends, unlike the win-gnu lane
#   AR_x86_64_pc_windows_msvc      `llvm-lib`      whose CC_ is an absolute program path
#   CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_LINKER   `lld-link`
#   CFLAGS_x86_64_pc_windows_msvc  the six `/imsvc<dir>` header search paths
#   CXXFLAGS_x86_64_pc_windows_msvc  the same six
#   XWIN_CACHE_DIR                 the unpacked CRT + Windows SDK splat
#
# soldr exports NO linker search paths for CMake: the three `/LIBPATH:` arguments live
# only inside CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_RUSTFLAGS, which is a rustc command
# line. They are derived from XWIN_CACHE_DIR below, in the same layout `soldr prepare`
# prints -- crt/lib/<arch>, sdk/lib/um/<arch>, sdk/lib/ucrt/<arch>.

set(CMAKE_SYSTEM_NAME Windows)
set(CMAKE_SYSTEM_PROCESSOR AMD64)

set(_soldr_triple "x86_64_pc_windows_msvc")
set(_soldr_arch "x86_64")

# Refuse to guess. Without `soldr prepare` in the same shell, CC_<triple> is unset,
# CMAKE_C_COMPILER would be empty and CMake would fall back to the host `cc` -- producing
# ELF objects that only fail much later, with an error about the wrong thing.
foreach(_required CC_${_soldr_triple} CXX_${_soldr_triple} XWIN_CACHE_DIR)
  if(NOT DEFINED ENV{${_required}})
    message(FATAL_ERROR
      "${_required} is not set: run `soldr prepare --target x86_64-pc-windows-msvc` "
      "(with --github-env \"$GITHUB_ENV\" on CI) before configuring with this toolchain.")
  endif()
endforeach()

set(CMAKE_C_COMPILER "$ENV{CC_${_soldr_triple}}")
set(CMAKE_CXX_COMPILER "$ENV{CXX_${_soldr_triple}}")

# clang-cl accepts `--target=` but CMake drives the MSVC-like driver, which takes `-m64`
# from the platform module; naming the target triple explicitly keeps a host-native
# clang-cl (if one is ever ahead of soldr's on PATH) from emitting host code.
set(CMAKE_C_COMPILER_TARGET "x86_64-pc-windows-msvc")
set(CMAKE_CXX_COMPILER_TARGET "x86_64-pc-windows-msvc")

if(DEFINED ENV{AR_${_soldr_triple}})
  set(CMAKE_AR "$ENV{AR_${_soldr_triple}}")
endif()
# CMake's Windows-MSVC platform module defaults CMAKE_LINKER to `link`, which on a Linux
# host is either absent or (worse) GNU coreutils' `link`. soldr names the linker only in
# its cargo-shaped variable; that is still soldr's own export, so consume it rather than
# hardcoding a second copy of the string.
if(DEFINED ENV{CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_LINKER})
  set(CMAKE_LINKER "$ENV{CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_LINKER}")
else()
  set(CMAKE_LINKER "lld-link")
endif()

# THE /MD PIN. soldr's CRT splat carries the release import libraries ONLY --
# `msvcrt.lib` and `libcmt.lib`, with no `msvcrtd.lib`/`libcmtd.lib`, and the SDK side has
# `ucrt.lib`/`libucrt.lib` with no `ucrtd.lib`. CMake's default for a Debug configuration
# is MultiThreadedDebugDLL (`/MDd` -> `msvcrtd.lib`), which cannot link here. Pinning the
# release DLL runtime for every configuration is what makes a Debug-flavoured build
# possible at all on this lane; it is also the only runtime the `mimalloc-redirect`
# override can work against, since a statically-linked CRT has no `ucrtbase.dll` to patch.
# Consequence, recorded in docs/ci-gates.md: this lane cannot reproduce the native
# `ctest-debug-full (windows-latest)` job's `/MDd` compile, only its DEFINES (see the
# windows-msvc section there).
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreadedDLL")

# Headers. Passed as flags rather than CMAKE_C_STANDARD_INCLUDE_DIRECTORIES because
# `/imsvc` marks them as system headers, which is what suppresses the SDK's own warnings
# under this tree's `-W`.
set(CMAKE_C_FLAGS_INIT "$ENV{CFLAGS_${_soldr_triple}}")
set(CMAKE_CXX_FLAGS_INIT "$ENV{CXXFLAGS_${_soldr_triple}}")

# Libraries, plus the manifest opt-out.
#
# `/MANIFEST:NO` is load-bearing and is NOT a cosmetic choice. CMake's Windows-MSVC module
# wraps every exe/dll link in `cmake -E vs_link_exe --rc=<CMAKE_RC_COMPILER>
# --mt=<CMAKE_MT> --manifests`, which compiles and embeds a default side-by-side manifest.
# That needs a resource compiler and an `mt`, and soldr's LLVM ships NEITHER (`llvm-rc`
# and `llvm-mt` are absent from the bin directory it puts on PATH). Without this flag the
# lane's outcome depends on whether the host happens to have some other `llvm-rc` ahead on
# PATH: with one, CMake silently bakes that absolute host path into the cache; without
# one, `CMAKE_RC_COMPILER` falls back to `rc`, and configure dies inside
# CMakeTestCCompiler with `RC Pass 1: command "rc /fo .../manifest.rc" failed`. `cmVSLink`
# parses the link line for `/MANIFEST:NO` and skips the rc/mt pass entirely, so this
# removes the dependency instead of papering over it. The tree registers no `.rc` source
# and none of its test executables needs a manifest.
set(_soldr_msvc_link
  "/MANIFEST:NO \
/LIBPATH:$ENV{XWIN_CACHE_DIR}/crt/lib/${_soldr_arch} \
/LIBPATH:$ENV{XWIN_CACHE_DIR}/sdk/lib/um/${_soldr_arch} \
/LIBPATH:$ENV{XWIN_CACHE_DIR}/sdk/lib/ucrt/${_soldr_arch}")
set(CMAKE_EXE_LINKER_FLAGS_INIT "${_soldr_msvc_link}")
set(CMAKE_SHARED_LINKER_FLAGS_INIT "${_soldr_msvc_link}")
set(CMAKE_MODULE_LINKER_FLAGS_INIT "${_soldr_msvc_link}")

# The same confinement the other two soldr toolchains carry. CMakeLists.txt's
# find_link_library() (~648-660) falls back to find_library() when check_linker_flag()
# rejects a name, and an unconfined search on a Linux host can then hand
# /usr/lib/x86_64-linux-gnu/librt.so to a PE link. As on windows-gnu this is
# belt-and-braces rather than load-bearing -- CMake's Windows-MSVC
# CMAKE_FIND_LIBRARY_SUFFIXES is `.lib;.a`, so a host `.so` is not a candidate -- and the
# build job asserts on the RESOLVED link library list, which is what would notice if that
# ever changed.
set(CMAKE_FIND_ROOT_PATH "$ENV{XWIN_CACHE_DIR}")
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)

# NOT set here, on purpose:
#   MI_USE_CXX -- CMakeLists.txt (~205-212) detects clang-cl by
#     CMAKE_C_COMPILER_ID=Clang + CMAKE_C_COMPILER_FRONTEND_VARIANT=MSVC and turns it ON
#     itself, exactly as it does for `cl`. Passing it would be a second source of truth
#     for something the native jobs already get for free, and `/Zc:__cplusplus` comes
#     with it.
#   CMAKE_RC_COMPILER -- see the /MANIFEST:NO note above; nothing compiles a resource.

unset(_soldr_triple)
unset(_soldr_arch)
unset(_soldr_msvc_link)
