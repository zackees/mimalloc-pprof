#!/usr/bin/env python3
"""Bun consumer-surface parity check (issue #274, Bun parity P9a).

`oven-sh/bun` never runs our CMake. Its `scripts/build/deps/mimalloc.ts` DirectBuild
compiles `src/static.c` *as C++*, with its own hardcoded define set, then links Bun's
Rust FFI declarations (`src/mimalloc_sys/mimalloc.rs`) and C++ consumers
(`src/jsc/bindings/MimallocWTFMalloc.h`, `src/jsc/modules/BunJSCModule.h`) straight
against the result. This script reproduces exactly that build outside CMake:

  1. compile `src/static.c` as C++ with Bun's exact defines,
  2. compile `test/test-bun-surface.cpp` (the symbol/ABI probe -- see that file's header
     comment for what it checks and why),
  3. link the two, run the result, and report cleanly which `mi_*` symbol(s) Bun would
     fail to link against today.

`mi_on_thread_idle` (Bun parity P7a, issue #299) merged to `main` in `1dbbb8df`, so as of
2026-09-02 step 3 reports zero missing symbols on both glibc and musl (alpine:3.20). The
CI job calling this script (`.github/workflows/c-unit.yml`, job `bun-surface`) is a hard
gate: this script exits 1 whenever any symbol is missing, an ABI static_assert fails, or
the resulting binary doesn't run clean, and that now blocks merge.

Usage:
    uv run ci/check_bun_surface.py             # glibc build (Bun's default Linux config)
    uv run ci/check_bun_surface.py --musl       # + MI_LIBC_MUSL=1 -ftls-model=local-dynamic
    uv run ci/check_bun_surface.py --cxx clang++
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_C = REPO_ROOT / "src" / "static.c"
TEST_TU = REPO_ROOT / "test" / "test-bun-surface.cpp"
INCLUDE_DIR = REPO_ROOT / "include"

# scripts/build/deps/mimalloc.ts, DirectBuild `defines` (Bun's exact set; re-verify at
# every refresh -- see docs/bun-gap-analysis-*.md section 2c).
BUN_DEFINES = [
    "-DMI_STATIC_LIB",
    "-DMI_SKIP_COLLECT_ON_EXIT=1",
    "-DMI_NO_PROCESS_DETACH=1",
    "-DMI_BUILD_RELEASE",
    "-DMI_MALLOC_OVERRIDE",
    "-DMI_DEFAULT_ALLOW_THP=0",
]
# -fPIC is Bun's (DirectBuild sets `pic: true`). -fno-omit-frame-pointer is NOT in
# mimalloc.ts today -- it is issue #274 9a step 3's own addition, matching the flag our
# CMakeLists.txt appends whenever MI_PPROF is on (profiler stack unwinding needs it).
# Bun doesn't build with MI_PPROF at all yet (see docs/bun-gap-analysis-*.md section
# 2c, "MI_PPROF under DirectBuild" -- their build silently compiles the profiler out),
# so this flag is here to keep the probe's own build shape consistent with what a real
# MI_PPROF=1 Bun integration would need, not because Bun already passes it.
BUN_CFLAGS = ["-fPIC", "-fno-omit-frame-pointer"]
BUN_MUSL_DEFINES = ["-DMI_LIBC_MUSL=1"]
BUN_MUSL_CFLAGS = ["-ftls-model=local-dynamic"]

# GNU ld / gold / lld on Linux ("undefined reference to `X'"), lld's "undefined symbol:
# X" shape, and ld64/Apple's "Undefined symbols ... \"_X\", referenced from:" shape --
# the latter two are kept for forward compatibility even though this script only runs
# on Linux (ubuntu glibc + alpine musl) today; a mach-o symbol name carries a leading
# `_` that C symbol names on Linux/Windows do not, hence the optional `_?` / literal
# `_` strip in each pattern below.
_UNDEFINED_REF_RE = re.compile(r"undefined reference to [`']([A-Za-z0-9_]+)'")
_UNDEFINED_SYMBOL_RE = re.compile(r"undefined symbol:\s*_?([A-Za-z0-9_]+)")
_MACHO_REFERENCED_FROM_RE = re.compile(r'"_?([A-Za-z0-9_]+)",\s*referenced from:')


def parse_missing_symbols(linker_output: str) -> list[str]:
    """Extract the set of undefined `mi_*` symbol names from linker stderr/stdout.

    Handles GNU ld/gold/lld's "undefined reference to `X'", lld's "undefined symbol: X",
    and ld64/Apple's "\"_X\", referenced from:" shapes. Returns a sorted, de-duplicated
    list restricted to `mi_`-prefixed names -- an undefined libc/libpthread symbol is a
    real build problem too, but it is not what this gate exists to report, and mixing it
    in would bury the signal this script's whole reason for existing is to surface
    clearly.
    """
    found: set[str] = set()
    for regex in (_UNDEFINED_REF_RE, _UNDEFINED_SYMBOL_RE, _MACHO_REFERENCED_FROM_RE):
        found.update(regex.findall(linker_output))
    return sorted(name for name in found if name.startswith("mi_"))


def run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)


def compile_object(
    cxx: str, source: Path, out: Path, extra_flags: list[str]
) -> subprocess.CompletedProcess[str]:
    command = [
        cxx,
        "-x",
        "c++",
        "-std=c++17",
        "-c",
        str(source),
        "-I",
        str(INCLUDE_DIR),
        *BUN_CFLAGS,
        *BUN_DEFINES,
        *extra_flags,
        "-o",
        str(out),
    ]
    return run(command, cwd=REPO_ROOT)


def link(
    cxx: str, objects: list[Path], out: Path, extra_flags: list[str]
) -> subprocess.CompletedProcess[str]:
    command = [cxx, *[str(o) for o in objects], "-lpthread", *extra_flags, "-o", str(out)]
    return run(command, cwd=REPO_ROOT)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--musl",
        action="store_true",
        help="add MI_LIBC_MUSL=1 -ftls-model=local-dynamic (run inside alpine)",
    )
    parser.add_argument("--cxx", default=None, help="C++ compiler to use (default: $CXX, then c++)")
    parser.add_argument(
        "--keep-tmp", action="store_true", help="do not delete the scratch build directory"
    )
    args = parser.parse_args()

    cxx = args.cxx or os.environ.get("CXX") or shutil.which("c++") or "c++"

    extra_flags = list(BUN_MUSL_DEFINES + BUN_MUSL_CFLAGS) if args.musl else []

    tmpdir = Path(tempfile.mkdtemp(prefix="bun-surface-"))
    try:
        static_o = tmpdir / "static.o"
        test_o = tmpdir / "test-bun-surface.o"
        binary = tmpdir / "bun-surface-probe"

        print(f"[check_bun_surface] compiler: {cxx}")
        print(f"[check_bun_surface] musl: {args.musl}")

        print("[check_bun_surface] compiling src/static.c as C++ with Bun's defines...")
        r = compile_object(cxx, STATIC_C, static_o, extra_flags)
        if r.returncode != 0:
            print("::error::src/static.c failed to compile under Bun's exact define set")
            print(r.stdout)
            print(r.stderr)
            return 1

        print("[check_bun_surface] compiling test/test-bun-surface.cpp...")
        r = compile_object(cxx, TEST_TU, test_o, extra_flags)
        if r.returncode != 0:
            # A compile failure here is almost always a failed static_assert -- i.e. an
            # ABI drift (MI_MAX_ALIGN_SIZE, mi_heap_area_t layout, or an mi_option_t
            # slot). Surface the compiler's own diagnostic, which already names the
            # exact static_assert and its message.
            print(
                "::error::test/test-bun-surface.cpp failed to compile -- likely an ABI static_assert failure"
            )
            print(r.stdout)
            print(r.stderr)
            return 1

        print("[check_bun_surface] linking...")
        r = link(cxx, [test_o, static_o], binary, extra_flags)
        if r.returncode != 0:
            missing = parse_missing_symbols(r.stdout + "\n" + r.stderr)
            print("::error::link failed -- Bun would fail to link against this tree")
            if missing:
                print("MISSING_SYMBOLS: " + " ".join(missing))
                for name in missing:
                    print(f"  - {name}")
            else:
                print("(could not parse a missing-symbol list from the linker output below)")
            print("--- linker output ---")
            print(r.stdout)
            print(r.stderr)
            return 1

        print("[check_bun_surface] running the linked probe...")
        r = run([str(binary)], cwd=tmpdir)
        if r.returncode != 0:
            print(f"::error::linked probe exited {r.returncode}, expected 0")
            print(r.stdout)
            print(r.stderr)
            return 1

        print("[check_bun_surface] nm -u sanity check (no unresolved mi_* symbols)...")
        nm = shutil.which("nm")
        if nm is not None:
            r = run([nm, "-u", str(binary)], cwd=tmpdir)
            unresolved_mi = [line for line in r.stdout.splitlines() if "mi_" in line]
            if unresolved_mi:
                print(
                    "::error::nm -u reports unresolved mi_* symbols in a binary that linked successfully"
                )
                for line in unresolved_mi:
                    print(f"  {line}")
                return 1
        else:
            print(
                "[check_bun_surface] nm not found -- skipping (link + run already prove resolution)"
            )

        print(
            "[check_bun_surface] PASS -- every symbol Bun links resolved, ran clean, ABI asserts held."
        )
        return 0
    finally:
        if not args.keep_tmp:
            shutil.rmtree(tmpdir, ignore_errors=True)
        else:
            print(f"[check_bun_surface] scratch dir kept: {tmpdir}")


if __name__ == "__main__":
    sys.exit(main())
