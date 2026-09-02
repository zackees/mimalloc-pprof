# CMake toolchain: build this repository for Intel (x86_64) macOS, on Linux, through soldr.
#
# Issue #277 phase B2. The sibling of cmake/toolchains/soldr-aarch64-apple-darwin.cmake;
# read that file's header for why the macOS gates are cross-compiled at all. This file
# exists because the owner requires that BOTH Apple architectures be produced without any
# native macOS runner, and because x86_64 is the arch the dockurr/macos guest can execute
# (there is no arm64 dockur image).
#
# soldr 0.9.11 provisions this lane the same way it does arm64
# (`soldr prepare --target x86_64-apple-darwin` reports `dispatch=blessed-darwin`):
# clang 21 + ld64.lld + an Apple SDK. This file hardcodes no path, no SDK version and no
# compiler version.
#
#   soldr prepare --target x86_64-apple-darwin --github-env "$GITHUB_ENV"
#   cmake -B build-macos-x64 -G Ninja -DCMAKE_BUILD_TYPE=Release -DMI_PPROF=ON \
#         --toolchain cmake/toolchains/soldr-x86_64-apple-darwin.cmake
#
# soldr exports (verified with soldr 0.9.11):
#   SDKROOT                       the Apple SDK it materialised
#   CC_x86_64_apple_darwin        "clang --target=x86_64-apple-darwin -isysroot <sdk> -mmacosx-version-min=..."
#   CXX_x86_64_apple_darwin       the same plus -stdlib=libc++
#   CFLAGS_x86_64_apple_darwin    those flags without the driver, plus -fuse-ld=lld
#   CXXFLAGS_x86_64_apple_darwin  likewise
#   AR_x86_64_apple_darwin        llvm-ar
#   RANLIB_x86_64_apple_darwin    llvm-ranlib
# and prepends its LLVM 21 bin directory to PATH, which is where `clang` comes from.

set(CMAKE_SYSTEM_NAME Darwin)
set(CMAKE_SYSTEM_PROCESSOR x86_64)

set(_soldr_triple "x86_64_apple_darwin")

# Refuse to guess. Without `soldr prepare` in the same shell this would otherwise
# configure against the *host* clang and produce ELF objects that fail much later, in the
# link or (worse) in the guest.
foreach(_required SDKROOT CC_${_soldr_triple} CXX_${_soldr_triple} CFLAGS_${_soldr_triple})
  if(NOT DEFINED ENV{${_required}})
    message(FATAL_ERROR
      "${_required} is not set: run `soldr prepare --target x86_64-apple-darwin` "
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
# Deliberately NOT setting CMAKE_OSX_ARCHITECTURES, exactly as the arm64 file does not:
# it is the switch CMakeLists.txt reads to decide between a universal-binary
# `-Xarch_x86_64 -march=haswell -mavx2` and the plain flags (CMakeLists.txt ~611-624).
# On this lane it also decides nothing else: CMakeLists.txt (~186-192) leaves MI_OPT_ARCH
# OFF for MI_ARCH=x64 and only force-enables it for arm64, so an Intel build emits no
# -march at all and the binary stays runnable on any x86-64 macOS -- including the
# dockurr/macos guest, whose emulated CPU is not guaranteed to carry AVX2.

# The load-bearing line. CMakeLists.txt's find_link_library() falls back to find_library()
# when check_linker_flag() rejects `-l<name>`, and on a Darwin link `-lrt` and `-latomic`
# are both rejected -- so without this the host's /usr/lib/x86_64-linux-gnu/librt.so and
# libatomic.so are found and handed to a Mach-O link. This bites harder on this lane than
# on arm64: host and target share an architecture here, so a leaked host ELF is not even
# rejected for the obvious reason. Confining library and header lookup to the SDK leaves
# Darwin linking `pthread` only, which is correct.
set(CMAKE_FIND_ROOT_PATH "$ENV{SDKROOT}")
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)

unset(_soldr_cc)
unset(_soldr_cxx)
unset(_soldr_triple)
