# CMake toolchain: build this repository for x86_64 Windows (MinGW-w64), on Linux, through
# soldr.
#
# Issue #277 phase C. The win-gnu gates are cross-compiled on a Linux runner into a
# portable test bundle (ci/bundle_tests.py) and executed on ONE Windows runner, instead of
# three MSYS2 VMs (`ctest-win-gnu`, `ctest-debug-full-win-gnu`, `ctest-shared-win-gnu`)
# plus `rust-native`'s `test-win-gnu` and `cross.yml`'s `build-win-gnu`.
#
# This file consumes ONLY the environment `soldr prepare --target x86_64-pc-windows-gnu`
# exports; it hardcodes no path and no compiler version.
#
#   soldr prepare --target x86_64-pc-windows-gnu --github-env "$GITHUB_ENV"
#   cmake -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DMI_PPROF=ON \
#         --toolchain cmake/toolchains/soldr-x86_64-pc-windows-gnu.cmake
#
# soldr exports (verified with the soldr on this repo's runners, mingw-w64-gcc 15.3.0):
#   MINGW_W64_CROSS_ROOT          the unpacked toolchain package
#   MINGW_W64_CROSS_BIN           its bin/ (also prepended to PATH)
#   CC_x86_64_pc_windows_gnu      .../bin/x86_64-w64-mingw32-gcc      (a program, not a
#   CXX_x86_64_pc_windows_gnu     .../bin/x86_64-w64-mingw32-g++       driver command line
#   AR_x86_64_pc_windows_gnu      .../bin/x86_64-w64-mingw32-ar        -- unlike the
#   RANLIB_x86_64_pc_windows_gnu  .../bin/x86_64-w64-mingw32-ranlib    Darwin lane, this
#   WINDRES_x86_64_pc_windows_gnu .../bin/x86_64-w64-mingw32-windres   lane exports no
#   DLLTOOL_x86_64_pc_windows_gnu .../bin/x86_64-w64-mingw32-dlltool   CFLAGS_<triple>)
#
# CRT: this toolchain is UCRT. mingw-w64 13 configured `--with-default-msvcrt=ucrt`, so
# `libmsvcrt.a` here is an archive of `lib64_libucrt_*.o` and `_mingw.h` defines `_UCRT`;
# a hello-world links `api-ms-win-crt-*-l1-1-0.dll`, not `msvcrt.dll`. MSYS2's MINGW64
# environment (what c-unit.yml uses today) is msvcrt. That is a deliberate coverage
# change, recorded in docs/ci-gates.md; see MI_MINGW_UCRT64 below.

set(CMAKE_SYSTEM_NAME Windows)
set(CMAKE_SYSTEM_PROCESSOR AMD64)

set(_soldr_triple "x86_64_pc_windows_gnu")

# Refuse to guess. Without `soldr prepare` in the same shell this would otherwise
# configure against the *host* gcc and produce ELF objects that fail much later.
foreach(_required MINGW_W64_CROSS_ROOT CC_${_soldr_triple} CXX_${_soldr_triple})
  if(NOT DEFINED ENV{${_required}})
    message(FATAL_ERROR
      "${_required} is not set: run `soldr prepare --target x86_64-pc-windows-gnu` "
      "(with --github-env \"$GITHUB_ENV\" on CI) before configuring with this toolchain.")
  endif()
endforeach()

set(CMAKE_C_COMPILER "$ENV{CC_${_soldr_triple}}")
set(CMAKE_CXX_COMPILER "$ENV{CXX_${_soldr_triple}}")

if(DEFINED ENV{AR_${_soldr_triple}})
  set(CMAKE_AR "$ENV{AR_${_soldr_triple}}")
endif()
if(DEFINED ENV{RANLIB_${_soldr_triple}})
  set(CMAKE_RANLIB "$ENV{RANLIB_${_soldr_triple}}")
endif()
# CMake's Windows platform module enables the RC language for any Windows target and
# probes for an unqualified `windres`, which on a Linux host is either absent or the
# host's. This tree registers no .rc source today, so nothing depends on it yet -- naming
# soldr's windres explicitly is what keeps that true if one is ever added.
if(DEFINED ENV{WINDRES_${_soldr_triple}})
  set(CMAKE_RC_COMPILER "$ENV{WINDRES_${_soldr_triple}}")
endif()
if(DEFINED ENV{DLLTOOL_${_soldr_triple}})
  set(CMAKE_DLLTOOL "$ENV{DLLTOOL_${_soldr_triple}}")
endif()

# The load-bearing line, same as the Darwin lane. CMakeLists.txt's find_link_library()
# (~648-660) falls back to find_library() when check_linker_flag() rejects `-l<name>`, and
# on a MinGW link `-lrt` and `-latomic` are both rejected -- so without this the host's
# /usr/lib/x86_64-linux-gnu/librt.so is found and handed to a PE link.
set(CMAKE_FIND_ROOT_PATH
  "$ENV{MINGW_W64_CROSS_ROOT}/x86_64-w64-mingw32"
  "$ENV{MINGW_W64_CROSS_ROOT}/x86_64-w64-mingw32/sysroot/usr")
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)

# CMakeLists.txt (~518) decides this from `$ENV{MSYSTEM}`, which can only ever be set by an
# MSYS2 shell -- it cannot fire in a cross build, so a UCRT cross build would otherwise be
# compiled *as if* it were msvcrt. It is load-bearing twice over:
#   * src/prim/windows/prim.c:794 registers the thread-exit callback through the
#     `.CRT$XLB` TLS-callback table instead of a constructor attribute, and
#   * CMakeLists.txt (~1281) post-processes mimalloc-test-stress-dynamic.exe with
#     `bin/minject.exe` so mimalloc.dll is first in its import order.
# The second one is a Windows PE utility and cannot run on this Linux host; the
# .github/workflows/windows-bundles.yml `run-windows-gnu` job runs it on the Windows
# runner instead, before replaying the bundle. See the guard in CMakeLists.txt.
set(MI_MINGW_UCRT64 ON CACHE BOOL "soldr's mingw-w64 defaults to UCRT (see this toolchain)")

unset(_soldr_triple)
