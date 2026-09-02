# CMake toolchain: build this repository for arm64 macOS, on Linux, through soldr.
#
# Issue #277 phase B. The macOS gates are cross-compiled on a Linux runner into a portable
# test bundle (ci/bundle_tests.py) and executed on one macOS runner, instead of one fresh
# macOS VM per job -- the Apple rows in cross.yml execute in 8-14 s and were measured
# waiting up to 5h26m for a runner.
#
# This file consumes ONLY the environment `soldr prepare --target aarch64-apple-darwin`
# exports; it hardcodes no path, no SDK version and no compiler version, so a soldr
# upgrade that moves the SDK (14.5 here, 15.5 in the #277 review) changes nothing here.
#
#   soldr prepare --target aarch64-apple-darwin --github-env "$GITHUB_ENV"
#   cmake -B build-macos-arm64 -G Ninja -DCMAKE_BUILD_TYPE=Release -DMI_PPROF=ON \
#         --toolchain cmake/toolchains/soldr-aarch64-apple-darwin.cmake
#
# soldr exports (verified with soldr 0.9.11):
#   SDKROOT                        the Apple SDK it materialised
#   CC_aarch64_apple_darwin        "clang --target=arm64-apple-darwin -isysroot <sdk> -mmacosx-version-min=11.0"
#   CXX_aarch64_apple_darwin       the same plus -stdlib=libc++
#   CFLAGS_aarch64_apple_darwin    those flags without the driver, plus -fuse-ld=lld
#   CXXFLAGS_aarch64_apple_darwin  likewise
#   AR_aarch64_apple_darwin        llvm-ar
#   RANLIB_aarch64_apple_darwin    llvm-ranlib
# and prepends its LLVM 21 bin directory to PATH, which is where `clang` comes from.

set(CMAKE_SYSTEM_NAME Darwin)
set(CMAKE_SYSTEM_PROCESSOR arm64)

set(_soldr_triple "aarch64_apple_darwin")

# Refuse to guess. Without `soldr prepare` in the same shell this would otherwise
# configure against the *host* clang and produce ELF objects that fail much later, in the
# link or (worse) on the macOS runner.
foreach(_required SDKROOT CC_${_soldr_triple} CXX_${_soldr_triple} CFLAGS_${_soldr_triple})
  if(NOT DEFINED ENV{${_required}})
    message(FATAL_ERROR
      "$ENV{${_required}} is not set: run `soldr prepare --target aarch64-apple-darwin` "
      "(with --github-env \"$GITHUB_ENV\" on CI) before configuring with this toolchain.")
  endif()
endforeach()

# soldr's CC_/CXX_ are full driver command lines. CMake wants the program in
# CMAKE_<LANG>_COMPILER and the flags in CMAKE_<LANG>_FLAGS, and the CFLAGS_/CXXFLAGS_
# exports are exactly "the same command line minus the driver, plus -fuse-ld=lld", so the
# driver is taken from the first token of CC_/CXX_ and everything else from CFLAGS_/CXXFLAGS_.
separate_arguments(_soldr_cc NATIVE_COMMAND "$ENV{CC_${_soldr_triple}}")
separate_arguments(_soldr_cxx NATIVE_COMMAND "$ENV{CXX_${_soldr_triple}}")
list(GET _soldr_cc 0 CMAKE_C_COMPILER)
list(GET _soldr_cxx 0 CMAKE_CXX_COMPILER)

set(CMAKE_C_FLAGS_INIT "$ENV{CFLAGS_${_soldr_triple}}")
set(CMAKE_CXX_FLAGS_INIT "$ENV{CXXFLAGS_${_soldr_triple}}")
# -fuse-ld=lld lives in those flags and is what selects ld64.lld; a link driven without
# them would look for Apple's `ld`, which does not exist on the Linux runner.
set(CMAKE_EXE_LINKER_FLAGS_INIT "$ENV{CFLAGS_${_soldr_triple}}")
set(CMAKE_SHARED_LINKER_FLAGS_INIT "$ENV{CFLAGS_${_soldr_triple}}")
set(CMAKE_MODULE_LINKER_FLAGS_INIT "$ENV{CFLAGS_${_soldr_triple}}")

if(DEFINED ENV{AR_${_soldr_triple}})
  find_program(CMAKE_AR NAMES "$ENV{AR_${_soldr_triple}}" REQUIRED)
endif()
if(DEFINED ENV{RANLIB_${_soldr_triple}})
  find_program(CMAKE_RANLIB NAMES "$ENV{RANLIB_${_soldr_triple}}" REQUIRED)
endif()

# Naming the sysroot and the deployment target explicitly keeps CMake's Darwin platform
# module from shelling out to `xcrun`/`sw_vers`, neither of which exists here. The minimum
# version is read back out of soldr's own flags rather than repeated, so the two can never
# disagree.
set(CMAKE_OSX_SYSROOT "$ENV{SDKROOT}" CACHE PATH "Apple SDK provisioned by soldr")
if("$ENV{CFLAGS_${_soldr_triple}}" MATCHES "-mmacosx-version-min=([0-9][0-9.]*)")
  set(CMAKE_OSX_DEPLOYMENT_TARGET "${CMAKE_MATCH_1}" CACHE STRING "macOS deployment target")
endif()
# Deliberately NOT setting CMAKE_OSX_ARCHITECTURES: it is the switch CMakeLists.txt reads
# to decide between a universal-binary `-Xarch_arm64 -march=armv8.1-a` and the plain
# `-march=armv8.1-a` this single-architecture build wants (CMakeLists.txt ~601-615).

# The load-bearing line. CMakeLists.txt's find_link_library() falls back to find_library()
# when check_linker_flag() rejects `-l<name>`, and on a Darwin link `-lrt` and `-latomic`
# are both rejected -- so without this the host's /usr/lib/x86_64-linux-gnu/librt.so and
# libatomic.so are found and handed to a Mach-O link. Confining library and header lookup
# to the SDK leaves Darwin linking `pthread` only, which is correct.
set(CMAKE_FIND_ROOT_PATH "$ENV{SDKROOT}")
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)

unset(_soldr_cc)
unset(_soldr_cxx)
unset(_soldr_triple)
